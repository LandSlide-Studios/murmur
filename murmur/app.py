"""Session lifecycle and wiring.

Thread boundaries are the architecture here, and violating them produces the
exact bugs listed in CLAUDE.md:

  hook thread   enqueues actions, does no work
  audio thread  fills the ring buffer, reports levels, never touches Qt
  worker thread runs STT, polish and injection, never blocks the UI
  UI thread     drains the action queue on a timer and paints

`on_state` is called from all three non-UI threads, so whatever the UI passes in
must marshal to the Qt thread itself.
"""

import logging
import os
import queue
import threading
import time
from pathlib import Path

from .audio import Recorder, peak_rms, rms
from .config import Config
from .corrections import Corrections
from .history import History
from .inject import Injector
from .platform.win.chord import Act
from .platform.win.focus import foreground_window
from .platform.win.hotkey import HotkeyListener
from .polish import Polisher
from .sound import Sounds
from .vad import SilenceMonitor
from .vocabulary import Vocabulary

log = logging.getLogger(__name__)


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Murmur"


class Session:
    """One dictation, with its own cancel flag.

    A single process-wide cancel Event was wrong in both directions: cancelling
    session A and starting B cleared the flag, so A's worker finished and pasted
    into B's app; and cancelling B set the flag that A's worker was still
    checking, so a completed dictation was discarded. Ownership of the flag has
    to travel with the session.
    """

    __slots__ = ("id", "mode", "cancelled", "aim")

    def __init__(self, session_id: int, mode: str):
        self.id = session_id
        self.mode = mode
        self.cancelled = False
        # Where the cursor was when THIS session stopped. It used to be one
        # slot on the app, written by every stop, so a dictation still
        # transcribing when the next one ended flew to the newer session's
        # cursor -- contradicting the whole point of a ballistic aim.
        self.aim: tuple[int, int] | None = None


class MurmurApp:
    def __init__(self, cfg: Config, on_state=None):
        self.cfg = cfg
        self.on_state = on_state or (lambda state, **kw: None)

        self.hotkeys = HotkeyListener(min_session_ms=cfg.get("audio.min_session_ms"))
        self.recorder = Recorder(
            sample_rate=cfg.get("audio.sample_rate"),
            device=cfg.get("audio.device"),
            on_level=self._on_level,
        )
        self.injector = Injector(
            restore_previous=cfg.get("clipboard.restore_previous"))
        self.polisher = Polisher(
            enabled=cfg.get("polish.enabled"),
            model=cfg.get("polish.model"),
            timeout_s=cfg.get("polish.timeout_s"),
            max_growth_ratio=cfg.get("polish.max_growth_ratio"),
            min_shrink_ratio=cfg.get("polish.min_shrink_ratio"),
        )
        self.sounds = Sounds(enabled=cfg.get("sound.enabled", True),
                             pack=cfg.get("sound.pack", "sotto"))
        self.vad = SilenceMonitor(
            threshold=cfg.get("audio.speech_rms_threshold"),
            stop_after_s=cfg.get("audio.silence_stop_seconds"),
        )

        store = data_dir()
        self.history = History(store / "history.db")
        self.vocab = Vocabulary(
            store / "vocab.db",
            promote_after_hits=cfg.get("learning.promote_after_hits"))
        uia = None
        if cfg.get("learning.enabled") and cfg.get("learning.uia_readback"):
            from .platform.win.uia import UIAReader

            uia = UIAReader()
        self.corrections = Corrections(self.vocab, cfg, uia=uia)

        self._stt = None
        self._stt_lock = threading.Lock()
        # Guards every session transition. The hotkey handler runs on the
        # UI thread and the silence auto-stop on the audio thread; both
        # mutate _mode.
        self._session_lock = threading.RLock()
        self._jobs: queue.Queue = queue.Queue()
        self._session: Session | None = None
        # Sessions that have been queued and can still be cancelled, oldest
        # first. This replaces a single `_inflight` slot that the worker
        # overwrote for whichever job it happened to be holding -- so which
        # session Esc hit was decided by worker timing, and it could destroy
        # one the user never cancelled while delivering one they did.
        self._pending: list[Session] = []
        self._pending_lock = threading.Lock()
        # DIAGNOSTIC ONLY -- what the worker is holding right now. Never
        # cancel through this. It used to be the cancel fallback, and
        # because the worker overwrites it per job, which session Esc hit
        # was decided by worker timing: cancels lost once it moved on, and
        # an older session destroyed in its place. Cancellation reads
        # `_pending` above, which is ordered and owned by the session.
        self._inflight: Session | None = None
        self._seq = 0
        self._last_level_t: float | None = None
        self._handlers = {
            Act.START_HOLD: lambda: self._start("hold"),
            Act.PROMOTE_TOGGLE: self._promote,
            Act.STOP_AND_TRANSCRIBE: self._stop_and_transcribe,
            Act.DISCARD: self._discard,
            Act.CANCEL: self._cancel_session,
        }
        self._apply_retention()
        self._worker: threading.Thread | None = None
        # How long quitting waits for a dictation already in flight. Long enough
        # for a transcription plus a cleanup pass; short enough that a wedged
        # worker cannot stop the app closing.
        self.shutdown_drain_s = 15.0

    # --- lazy STT: a model load is seconds, and must not delay app start ----

    @property
    def stt(self):
        with self._stt_lock:
            if self._stt is None:
                if self.cfg.get("stt.backend") == "cloud":
                    from .stt.cloud import CloudTranscriber

                    self._stt = CloudTranscriber(
                        provider=self.cfg.get("stt.cloud_provider", "groq"),
                        sample_rate=self.cfg.get("audio.sample_rate"),
                        language=self.cfg.get("stt.language"),
                    )
                else:
                    from .stt.local import LocalTranscriber

                    self._stt = LocalTranscriber(
                        model=self.cfg.get("stt.local_model"),
                        device=self.cfg.get("stt.device"),
                        language=self.cfg.get("stt.language"),
                    )
            return self._stt

    def preload(self) -> None:
        """Warm both models off the UI thread so the first dictation is not slow.

        The speech model is a multi-second load. The cleanup model is worse in a
        subtler way: Ollama unloads it when idle, so the first real dictation
        paid the load cost inside the polish timeout and fell back to the raw
        transcript. A tiny warm-up request keeps that off the user's first
        sentence.
        """
        def warm():
            try:
                _ = self.stt
            except Exception:
                log.exception("could not preload the speech model")
            try:
                self.polisher.warm()
            except Exception:
                log.debug("cleanup model warm-up failed", exc_info=True)

        threading.Thread(target=warm, daemon=True, name="murmur-preload").start()

    # --- session transitions (UI thread) -----------------------------------

    def _on_level(self, level: float) -> None:
        """Audio callback thread. Keep it cheap — anything slow is dropped audio."""
        now = time.perf_counter()
        last = self._last_level_t          # read once: _start may null it concurrently
        dt = 0.0 if last is None else now - last
        self._last_level_t = now
        self.on_state("level", level=level)
        session = self._session
        if session is None or session.mode != "toggle":
            return
        if self.vad.feed(level, dt):
            log.info("auto-stop after %.0fs of silence", self.vad.stop_after_s)
            self._stop_and_transcribe()

    def _start(self, mode: str, external: bool = False) -> None:
        with self._session_lock:
            if self._session is not None:
                return
            self._seq += 1
            session = Session(self._seq, mode)
            self._session = session
            self.vad.reset()
            self._last_level_t = None
            try:
                self.recorder.begin()
            except Exception:
                log.exception("could not open the microphone")
                self._session = None
                self.on_state("error")
                return
        self._cue("start")
        if external:
            # Started from the desktop shortcut rather than the chord, so the
            # FSM is still IDLE and would ignore Esc. Tell it a session is live.
            self.hotkeys.fsm.adopt_toggle_session()
        self.on_state("recording", mode=mode)

    # PlaySound(SND_ASYNC) returns before the audio reaches the speakers, so the
    # mute must outlast the cue by the output latency.
    #
    # What is proven: the mute itself drops every block inside its window
    # (tests/test_sound.py). What could NOT be measured is the exact output
    # latency — in a room with ordinary background noise, broadband level and
    # even the cue's own frequency band are dominated by ambient sound, and
    # repeated runs disagreed by more than the effect being measured. In the one
    # clean sweep, 60ms of slack still leaked at 2.2x the floor while 400ms was
    # indistinguishable from it, so 400ms is the margin.
    #
    # The cost is bounded: the 400ms pre-roll already holds everything said
    # before the chord went down, so this only mutes the moment the keys are
    # being pressed. The silence guard in _process is the backstop.
    CUE_MUTE_SLACK_MS = 400

    @staticmethod
    def _receipt(mode: str, text: str, delivery: str) -> None:
        """One line per dictation saying whether it actually landed.

        'It does not send sometimes' was impossible to diagnose because a
        successful session and a silently undelivered one logged the same
        thing: nothing.
        """
        preview = " ".join(text.split())
        if len(preview) > 70:
            preview = preview[:67] + "..."
        log.info("[%s] %s -> %s | %s", mode, delivery, preview,
                 f"{len(text)} chars")

    @staticmethod
    def _cursor_point():
        try:
            import ctypes
            import ctypes.wintypes

            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return (int(pt.x), int(pt.y))
        except Exception:
            return None

    def _cue(self, name: str) -> None:
        """Play a cue and mute capture while it sounds.

        The speakers are audible to the microphone and Whisper invents words
        from non-speech — a recording of nothing but cue tones transcribed to
        "Thanks."
        """
        ms = self.sounds.duration_ms(name)
        if ms:
            self.recorder.mute_for(ms + self.CUE_MUTE_SLACK_MS)
        self.sounds.play(name)

    def _promote(self) -> None:
        with self._session_lock:
            if self._session is None or self._session.mode != "hold":
                return
            self._session.mode = "toggle"
            self.vad.reset()
        self.on_state("recording", mode="toggle")

    def _stop_and_transcribe(self) -> None:
        """Called from BOTH the UI thread (hotkey) and the audio thread (silence
        auto-stop). Without the lock, a hands-free session whose auto-stop fires
        as the user taps the chord passes the None check twice and enqueues the
        same recording twice — pasting the transcript twice into the document.
        Reproduced 60/60 before the lock was added.

        Claiming the mode inside the lock is what makes it exclusive; the
        recorder is only stopped by whichever caller won.
        """
        with self._session_lock:
            if self._session is None:
                return
            session, self._session = self._session, None
            pcm = self.recorder.end()
        # Where the mouse is NOW. The comet is ballistic: it flies to where you
        # were when you stopped talking, not to wherever the cursor drifts to.
        session.aim = self._cursor_point()
        dur_ms = int(len(pcm) / self.cfg.get("audio.sample_rate") * 1000)
        self.on_state("transcribing")
        self._cue("charge")
        with self._pending_lock:
            self._pending.append(session)
        # Something other than the chord may have ended this (the silence
        # auto-stop runs on the audio thread, the tick lives on the pill), and
        # the FSM would otherwise still believe a session is recording.
        self._release_fsm()
        self._jobs.put((pcm, session, dur_ms))

    def _discard(self) -> None:
        """A sub-threshold tap is a mis-press, not a session, so it is not
        recorded — otherwise history fills with 100ms noise."""
        with self._session_lock:
            if self._session is None:
                return
            self._session = None
            self.recorder.end()
        self.on_state("idle")

    def _cancel_session(self) -> None:
        with self._session_lock:
            session, self._session = self._session, None
            if session is not None:
                session.cancelled = True
                pcm = self.recorder.end()
            else:
                # Nothing recording, but a transcription may still be in flight.
                # Take the most recent session that has not been delivered yet.
                # Reading the worker's current job instead meant Esc cancelled
                # whatever it happened to be holding: 200/200 cancels lost once
                # the worker had moved on, and an older session destroyed in its
                # place.
                pcm = None
                with self._pending_lock:
                    session = self._pending[-1] if self._pending else None
                if session is not None:
                    session.cancelled = True
        if session is not None and pcm is not None:
            dur_ms = int(len(pcm) / self.cfg.get("audio.sample_rate") * 1000)
            # A cancelled session still gets a row. The audio is dropped by
            # design, but "every session is recorded" is the whole promise of
            # the history panel — silently leaving no trace breaks it.
            self._record_cancelled(session.mode, dur_ms)
        self._release_fsm()
        if session is None:
            # Nothing was cancelled, so say nothing. This used to fire
            # unconditionally, confirming a cancel for text already sitting in
            # the user's document.
            return
        self._cue("cancel")
        self.on_state("cancelled")

    def _apply_retention(self) -> None:
        """Trim the history if the user asked for a cap, and log its size.

        `purge()` existed and nothing called it, so history grew without limit
        and a single transcript can exceed a megabyte. The default is still
        unlimited -- deleting someone's dictations is not a default to choose
        for them -- but the count is now in the log, so growth is visible.
        """
        try:
            keep = int(self.cfg.get("history.keep_rows", 0) or 0)
            if keep > 0:
                self.history.purge(keep)
            log.info("history holds %d dictations (retention: %s)",
                     self.history.count(),
                     f"newest {keep}" if keep > 0 else "unlimited")
        except Exception:
            log.debug("could not apply history retention", exc_info=True)

    def _unpend(self, session: "Session") -> None:
        """Drop a session from the cancellable set. Safe to call twice."""
        with self._pending_lock:
            if session in self._pending:
                self._pending.remove(session)

    def _release_fsm(self) -> None:
        """Tell the chord FSM the live session is over.

        Needed because sessions end by routes the FSM cannot see: the silence
        auto-stop on the audio thread, and the tick and cross on the pill.
        """
        fsm = getattr(getattr(self, "hotkeys", None), "fsm", None)
        if fsm is not None:
            fsm.release_session()

    def _record_cancelled(self, mode: str, dur_ms: int) -> None:
        try:
            app_name, title = foreground_window()
            self.history.add(
                raw=None, polished=None, final=None, mode=mode,
                duration_ms=dur_ms, app=app_name, title=title,
                status="cancelled")
        except Exception:
            log.exception("could not record the cancelled session")

    # --- worker thread -----------------------------------------------------

    def _run_worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            pcm, session, dur_ms = job
            self._inflight = session          # diagnostic; see __init__
            try:
                self._process(pcm, session, dur_ms)
            except Exception:
                # _process handles its own failures; this is the last resort so
                # one bad session cannot kill the worker thread for good.
                log.exception("worker loop error")
                try:
                    # ...which it could, because THIS call was unguarded.
                    # `on_state` is foreign UI code, and a Qt callback into a
                    # deleted object raises -- escaping the loop and ending the
                    # thread. Every dictation after that was queued and never
                    # processed: no paste, no history row, no error, and the
                    # pill still reading "transcribing".
                    self.on_state("error")
                except Exception:
                    log.exception("the error handler itself failed")
            finally:
                self._inflight = None
                self._unpend(session)

    def _process(self, pcm, session: "Session", dur_ms: int) -> None:
        """Every path through here writes a history row. A crashed or cancelled
        session must never silently cost the user their words.

        Cancellation is read from THIS session's own flag, never a shared one:
        a later session being cancelled must not discard this one's result.
        """
        mode = session.mode
        raw = polished = final = None
        status = "ok"
        try:
            # Whisper invents words from non-speech: a recording of nothing but
            # an audio cue transcribed to "Thanks." If the whole clip is below
            # the speech threshold there is nothing to transcribe.
            # peak_rms, NOT rms. The average of a whole recording gets quieter
            # the longer someone talks, because pauses count against it — a
            # 42.4s dictation he spoke all the way through was discarded as
            # silent. "Is there any speech in here" is a maximum, not a mean.
            loudest = peak_rms(pcm, self.cfg.get("audio.sample_rate"))
            log.info("level: loudest 400ms window %.4f over %.1fs",
                     loudest, dur_ms / 1000)
            if loudest < self.cfg.get("audio.speech_rms_threshold") / 2:
                log.info("recording is silent (%.1fs); nothing to transcribe",
                         dur_ms / 1000)
                # Set the status and fall through: the finally block writes the
                # row. Writing it here AND returning produced two rows for one
                # session — an "empty" one plus an "ok" one with no text at all.
                status = "empty"
            else:
                raw = self.stt.transcribe(pcm, hotwords=self.vocab.hotwords())
                if session.cancelled:
                    status = "cancelled"
                elif not raw.strip():
                    log.info("nothing transcribed (%.1fs of audio)", dur_ms / 1000)
                    status = "empty"
                else:
                    self.on_state("polishing")
                    # The glossary already gives the model the correct spellings,
                    # so substitution runs ONCE, after polish. Running it on both
                    # sides doubled any term whose replacement contained its own
                    # wrong form ("vantage" -> "Vantage Labs" -> "... Labs Labs").
                    polished = self.polisher.polish(
                        raw, glossary=self.vocab.glossary())
                    final = self.vocab.apply(polished)
                    if session.cancelled:
                        status = "cancelled"
                    elif not self.cfg.get("ui.comet", True):
                        pasted = self.injector.inject(final)
                        self._unpend(session)
                        self._cue("done")
                        self._receipt(mode, final,
                                      "pasted" if pasted else "clipboard only")
                        self.on_state("done" if pasted else "copied", text=final)
                    else:
                        # The transcript is on the clipboard BEFORE anything
                        # moves, so a failed animation can never cost the user
                        # their words.
                        released = self.injector.copy(final)
                        # Unpend HERE, not before the copy. Doing it first made
                        # the guard below unreachable from any real cancel
                        # route: once unpended, `_cancel_session` could not find
                        # the session at all, so the fix was inert.
                        self._unpend(session)
                        # The clipboard is not the document. Cancellation was
                        # checked once before this and never again, so an Esc
                        # landing in the window between the two was
                        # acknowledged and then overruled by the paste. The
                        # text stays on the clipboard either way, which is the
                        # point: the user can still have it if they want it.
                        if session.cancelled:
                            log.info("cancelled after the clipboard; not pasting")
                            status = "cancelled"
                            self.on_state("cancelled")
                        elif released:
                            self._cue("launch")
                            # "staged" not "pasted": the keystroke fires ~370ms
                            # later when the comet lands, and this line exists
                            # precisely because "it does not send sometimes"
                            # was undiagnosable. Claiming a paste that has not
                            # happened yet puts the lie back in.
                            self._receipt(mode, final, "staged for paste")
                            # session.aim only, with no fallback. It is None
                            # exactly when the cursor could not be read -- at
                            # which instant the shared slot was set to None too,
                            # so it can never hold THIS session's cursor. Only a
                            # later session's, which is the bug this replaced.
                            self.on_state("flying", text=final,
                                          aim=session.aim)
                        else:
                            self._cue("launch")
                            self._receipt(mode, final, "clipboard only "
                                          "(a modifier was still held)")
                            self.on_state("copied", text=final)
        except Exception:
            log.exception("dictation failed")
            status = "error"
            self.on_state("error")
        finally:
            app_name, title = foreground_window()
            try:
                row_id = self.history.add(
                    raw=raw, polished=polished, final=final, mode=mode,
                    duration_ms=dur_ms, app=app_name, title=title, status=status)
                if status == "ok" and final:
                    self.corrections.watch(row_id, final)
            except Exception:
                log.exception("could not write the history row")

        if status != "ok":
            self.on_state("idle")

    # --- lifecycle ---------------------------------------------------------

    def pump(self) -> None:
        """Drain hook actions without blocking. Driven by a timer on the UI thread."""
        while True:
            try:
                act = self.hotkeys.actions.get_nowait()
            except queue.Empty:
                return
            handler = self._handlers.get(act)
            if handler is not None:
                handler()

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run_worker, daemon=True,
                                        name="murmur-worker")
        self._worker.start()
        # Open the microphone now, not per session: the device needs ~550ms to
        # deliver its first block, which used to eat the start of every dictation.
        try:
            self.recorder.open()
        except Exception:
            log.exception("could not open the microphone at startup")
        self.hotkeys.start()

    def stop(self) -> None:
        self.hotkeys.stop()
        with self._session_lock:
            self._session = None
        self.recorder.close()
        self._jobs.put(None)
        # Wait for the worker to finish what it already has.
        #
        # The sentinel goes in behind any queued jobs, and the stores used to
        # close immediately after posting it. Those jobs then ran against closed
        # databases: the dictation was not delivered AND no history row was
        # written for it -- the one outcome this module's docstring rules out.
        # One dictation lost per quit that happened to be in flight.
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=self.shutdown_drain_s)
            if worker.is_alive():
                log.warning("worker still busy after %.1fs; closing anyway",
                            self.shutdown_drain_s)
        try:
            self.history.close()
            self.vocab.close()
        except Exception:
            log.debug("error closing stores", exc_info=True)
