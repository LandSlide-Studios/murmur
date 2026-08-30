"""Adversarial scenarios for MurmurApp's session lifecycle.

Written air-gapped from the engineering log and from the existing suite: every
assertion here is derived from app.py's own docstrings and from first
principles about what a dictation app must never do.

Safety rules honoured throughout:
  * ``murmur.app.Recorder`` is replaced by a fake CLASS before any app is
    constructed, so no PortAudio stream is ever created (and no 115MB ring
    buffer is allocated per app).
  * ``murmur.app.data_dir`` is redirected into tmp_path, so the live
    ``%APPDATA%\\Murmur`` store is never touched.
  * ``learning.uia_readback`` is off (no COM on the test thread), polish is
    disabled (no Ollama), the injector is a fake (the real Windows clipboard is
    never written), and sounds are a fake (no PlaySound).
  * ``app.start()`` is never called: it installs a real low-level keyboard hook
    and opens the microphone. The worker thread is started directly instead.

The invariants under test, stated once:

  I1  Exactly one history row per session that was stopped or cancelled.
      Zero for a discarded sub-threshold tap.
  I2  Text from a cancelled session is never delivered.
  I3  A session the user did not cancel is never destroyed by a cancel.
  I4  No session is ever delivered twice.
  I5  The UI is never left in "recording" with no live session, and the app
      never acknowledges a cancel it did not perform.
"""

import copy
import itertools
import queue
import random
import threading
import time

import numpy as np
import pytest

import murmur.app as app_mod
from murmur.app import MurmurApp
from murmur.config import DEFAULTS, Config

# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------

SR = 16000
LOUD = np.full(SR, 0.05, dtype=np.float32)      # 1.0s well above the threshold
QUIET = np.full(SR, 0.0001, dtype=np.float32)   # 1.0s below threshold/2


class FakeRecorder:
    """Stands in for murmur.audio.Recorder. Opens nothing."""

    def __init__(self, sample_rate=SR, device=None, on_level=None, **kw):
        self.sample_rate = sample_rate
        self.on_level = on_level
        self.pcm = LOUD
        self.capturing = False
        self.opened = False
        self.closed = False
        self.begins = 0
        self.ends = 0
        self.muted_ms = []
        self.begin_error = None
        self.begin_hook = None

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True
        self.capturing = False

    def begin(self):
        self.begins += 1
        if self.begin_hook is not None:
            self.begin_hook()
        if self.begin_error is not None:
            raise self.begin_error
        self.capturing = True

    def end(self):
        self.ends += 1
        self.capturing = False
        return self.pcm

    def mute_for(self, ms):
        self.muted_ms.append(ms)


class FakeSounds:
    def __init__(self):
        self.played = []
        self.hooks = {}
        self._lock = threading.Lock()

    def duration_ms(self, cue):
        return 0

    def play(self, cue):
        with self._lock:
            self.played.append(cue)
        hook = self.hooks.get(cue)
        if hook is not None:
            hook()

    def stop(self):
        pass


class FakeInjector:
    """Records what was 'delivered'. Never touches the real clipboard."""

    def __init__(self):
        self._lock = threading.Lock()
        self.copied = []      # text that reached the clipboard
        self.pasted = []      # text that was actually keystroked in
        self.released = True  # modifiers clear?
        self.error = None
        self.on_copy = None

    def copy(self, text):
        if not text:
            return False
        if self.error is not None:
            raise self.error
        with self._lock:
            self.copied.append(text)
        if self.on_copy is not None:
            self.on_copy(text)
        return self.released

    def paste(self):
        pass

    def inject(self, text):
        if not text:
            return False
        if self.error is not None:
            raise self.error
        with self._lock:
            self.copied.append(text)
            if self.released:
                self.pasted.append(text)
        return self.released

    def delivered(self):
        with self._lock:
            return list(self.copied)


class FakeSTT:
    """Deterministic transcriber with an optional gate and error."""

    def __init__(self, text="hello world"):
        self._lock = threading.Lock()
        self.text = text          # str, or a list consumed in order, or callable
        self.calls = 0
        self.gate = None          # threading.Event the call waits on
        self.delay = 0.0
        self.error = None
        self.hotwords_seen = []
        self.entered = threading.Event()

    def transcribe(self, pcm, hotwords=None):
        with self._lock:
            n = self.calls
            self.calls += 1
            self.hotwords_seen.append(hotwords)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10.0), "STT gate never released"
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if callable(self.text):
            return self.text(n)
        if isinstance(self.text, list):
            return self.text[min(n, len(self.text) - 1)]
        return self.text


class FakePolisher:
    def __init__(self):
        self.enabled = False
        self.error = None
        self.transform = None
        self.calls = 0

    def polish(self, raw, glossary=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.transform(raw) if self.transform else raw

    def warm(self):
        return False


class States:
    """Thread-safe on_state recorder, with an optional raising hook."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.raise_on = {}     # state -> exception
        self.hooks = {}        # state -> callable

    def __call__(self, state, **kw):
        with self._lock:
            self.events.append((state, kw))
        hook = self.hooks.get(state)
        if hook is not None:
            hook(**kw)
        err = self.raise_on.get(state)
        if err is not None:
            raise err

    def names(self):
        with self._lock:
            return [e[0] for e in self.events]

    def kwargs_for(self, state):
        with self._lock:
            return [kw for s, kw in self.events if s == state]

    def count(self, state):
        return self.names().count(state)

    def last(self):
        names = self.names()
        return names[-1] if names else None


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------

_counter = itertools.count()


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Builds fully-stubbed MurmurApps and tears every one of them down."""
    monkeypatch.setattr(app_mod, "Recorder", FakeRecorder)
    monkeypatch.setattr(app_mod, "foreground_window", lambda: ("test.exe", "Test"))
    built = []

    def _make(worker=True, **overrides):
        store = tmp_path / f"app{next(_counter)}"
        monkeypatch.setattr(app_mod, "data_dir", lambda s=store: s)
        cfg = Config(copy.deepcopy(DEFAULTS), store / "settings.json")
        cfg.set("learning.uia_readback", False)   # no COM
        cfg.set("polish.enabled", False)          # no Ollama
        cfg.set("sound.enabled", False)
        for dotted, value in overrides.items():
            cfg.set(dotted.replace("__", "."), value)

        states = States()
        app = MurmurApp(cfg, on_state=states)
        app.states = states
        app.injector = FakeInjector()
        app.sounds = FakeSounds()
        app.polisher = FakePolisher()
        app.stt_fake = FakeSTT()
        app._stt = app.stt_fake
        built.append(app)
        if worker:
            start_worker(app)
        return app

    yield _make

    for app in built:
        teardown(app)


def start_worker(app):
    t = threading.Thread(target=app._run_worker, daemon=True,
                         name="adv-worker")
    app._worker = t
    t.start()
    return t


def teardown(app):
    # Release any gate the test left closed, or the worker never exits.
    if getattr(app, "stt_fake", None) is not None and app.stt_fake.gate is not None:
        app.stt_fake.gate.set()
    app.states.raise_on.clear()
    app.states.hooks.clear()
    try:
        app._jobs.put(None)
    except Exception:
        pass
    w = app._worker
    if w is not None:
        w.join(timeout=3.0)
    for store in (app.history, app.vocab):
        try:
            store.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def wait_for(pred, timeout=5.0, interval=0.002):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def rows(app):
    return app.history.recent(limit=1000)


def rows_by_id(app):
    return sorted(rows(app), key=lambda r: r["id"])


def wait_rows(app, n, timeout=5.0):
    ok = wait_for(lambda: len(rows(app)) >= n, timeout)
    wait_for(lambda: app._inflight is None, 1.0)
    return ok


def statuses(app):
    return [r["status"] for r in rows_by_id(app)]


def settle(seconds=0.12):
    """Give a stray thread a chance to write the row that must not exist."""
    time.sleep(seconds)


def run_session(app, mode="hold", pcm=None):
    if pcm is not None:
        app.recorder.pcm = pcm
    app._start(mode)
    app._stop_and_transcribe()


# ==========================================================================
# A. Terminal states: exactly one history row per session (I1)
# ==========================================================================

def test_01_ok_session_writes_exactly_one_row_and_delivers_once(make_app):
    app = make_app()
    app.stt_fake.text = "the quick brown fox"
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["ok"]
    assert app.injector.delivered() == ["the quick brown fox"]
    assert rows(app)[0]["final_text"] == "the quick brown fox"


def test_02_silent_audio_writes_one_empty_row_and_delivers_nothing(make_app):
    app = make_app()
    run_session(app, pcm=QUIET)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["empty"], "the silence guard must not also write an 'ok' row"
    assert app.injector.delivered() == []
    assert app.stt_fake.calls == 0, "silent audio must never reach the model"


def test_03_empty_transcript_writes_one_empty_row(make_app):
    app = make_app()
    app.stt_fake.text = ""
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["empty"]
    assert app.injector.delivered() == []


def test_04_whitespace_only_transcript_is_empty_not_ok(make_app):
    app = make_app()
    app.stt_fake.text = "   \n\t  "
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["empty"]
    assert app.injector.delivered() == []


def test_05_stt_returning_none_still_writes_exactly_one_row(make_app):
    app = make_app()
    app.stt_fake.text = lambda n: None
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert len(rows(app)) == 1
    assert statuses(app) == ["error"]
    assert app.injector.delivered() == []


def test_06_stt_raising_writes_one_error_row_and_delivers_nothing(make_app):
    app = make_app()
    app.stt_fake.error = RuntimeError("cuda out of memory")
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["error"]
    assert app.injector.delivered() == []
    assert "error" in app.states.names()


def test_07_cancel_while_recording_writes_one_cancelled_row_and_no_job(make_app):
    app = make_app()
    app._start("hold")
    app._cancel_session()
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["cancelled"]
    assert app.stt_fake.calls == 0
    assert app.injector.delivered() == []
    assert app._jobs.empty()


def test_08_cancel_in_flight_writes_exactly_one_row_and_delivers_nothing(make_app):
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = "secret words"
    run_session(app)
    assert app.stt_fake.entered.wait(3.0)
    assert wait_for(lambda: app._inflight is not None, 2.0)
    app._cancel_session()
    app.stt_fake.gate.set()
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["cancelled"], "one row, and it says cancelled"
    assert app.injector.delivered() == [], "I2: cancelled text must never be delivered"


def test_09_discard_writes_no_row_at_all(make_app):
    app = make_app()
    app._start("hold")
    app._discard()
    settle(0.2)
    assert rows(app) == []
    assert app._jobs.empty()
    assert app._session is None
    assert app.states.last() == "idle"


def test_10_injector_failure_keeps_the_words_in_history(make_app):
    app = make_app()
    app.stt_fake.text = "words worth keeping"
    app.injector.error = OSError("clipboard busy")
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert len(rows(app)) == 1
    r = rows(app)[0]
    assert r["status"] == "error"
    assert r["final_text"] == "words worth keeping", (
        "a failed paste must not cost the user their words")


def test_11_modifier_still_held_is_still_one_ok_row(make_app):
    app = make_app()
    app.injector.released = False       # copy() reports "modifier held"
    app.stt_fake.text = "held down"
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["ok"]
    assert app.injector.delivered() == ["held down"]
    assert "copied" in app.states.names()
    assert "flying" not in app.states.names()


def test_12_non_comet_path_pastes_and_writes_one_row(make_app):
    app = make_app(ui__comet=False)
    app.stt_fake.text = "direct paste"
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    assert statuses(app) == ["ok"]
    assert app.injector.pasted == ["direct paste"]
    assert app.states.names().count("done") == 1


def test_13_polish_raising_writes_one_error_row_with_the_raw_text(make_app):
    app = make_app()
    app.polisher.error = TimeoutError("ollama gone")
    app.stt_fake.text = "raw survives"
    run_session(app)
    assert wait_rows(app, 1)
    settle()
    r = rows(app)[0]
    assert len(rows(app)) == 1
    assert r["status"] == "error"
    assert r["raw_text"] == "raw survives", "the raw transcript must be preserved"


def test_14_vocabulary_substitution_raising_still_writes_one_row(make_app):
    app = make_app()
    app.stt_fake.text = "vocab boom"
    original_apply = app.vocab.apply
    app.vocab.apply = lambda text: (_ for _ in ()).throw(ValueError("bad pattern"))
    try:
        run_session(app)
        assert wait_rows(app, 1)
        settle()
        assert len(rows(app)) == 1
        assert rows(app)[0]["status"] == "error"
        assert rows(app)[0]["raw_text"] == "vocab boom"
    finally:
        app.vocab.apply = original_apply


def test_15_history_write_failure_does_not_kill_the_worker(make_app):
    app = make_app()
    boom = [True]
    real_add = app.history.add

    def flaky_add(*a, **kw):
        if boom[0]:
            boom[0] = False
            raise RuntimeError("disk full")
        return real_add(*a, **kw)

    app.history.add = flaky_add
    app.stt_fake.text = ["first", "second"]
    run_session(app)
    assert wait_for(lambda: app.stt_fake.calls == 1, 3.0)
    assert wait_for(lambda: app._inflight is None, 3.0)
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0), (
        "the worker must survive a failed history write and run the next job")
    assert rows(app)[0]["final_text"] == "second"


# ==========================================================================
# B. Worker robustness and queue ordering
# ==========================================================================

def test_16_worker_survives_an_stt_exception_and_runs_the_next_job(make_app):
    app = make_app()
    calls = {"n": 0}

    def text(n):
        calls["n"] = n
        if n == 0:
            raise RuntimeError("first one explodes")
        return "second one is fine"

    app.stt_fake.text = text
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0)
    run_session(app)
    assert wait_rows(app, 2, timeout=3.0)
    settle()
    assert statuses(app) == ["error", "ok"]
    assert app.injector.delivered() == ["second one is fine"]


def test_17_on_state_raising_inside_process_still_writes_the_row(make_app):
    app = make_app()
    app.states.raise_on["polishing"] = RuntimeError(
        "Internal C++ object already deleted")
    app.stt_fake.text = "qt is gone"
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0)
    settle()
    assert len(rows(app)) == 1
    assert rows(app)[0]["status"] == "error"


def test_18_on_state_raising_in_the_worker_last_resort_must_not_kill_the_worker(make_app):
    """_run_worker's except clause calls on_state unguarded.

    A Qt callback that raises (a deleted C++ object during shutdown is the
    common case) escapes the last-resort handler and takes the worker thread
    with it. Every later dictation is then silently lost forever.
    """
    app = make_app()
    # Force _process itself to raise: on_state("idle") is outside its try.
    app.states.raise_on["idle"] = RuntimeError("Internal C++ object already deleted")
    app.states.raise_on["error"] = RuntimeError("Internal C++ object already deleted")
    app.stt_fake.text = ["", "second dictation"]
    run_session(app)                       # -> status empty -> on_state("idle") raises
    assert wait_rows(app, 1, timeout=3.0)
    app.states.raise_on.clear()

    run_session(app)                       # the next dictation
    assert wait_rows(app, 2, timeout=3.0), (
        "worker thread died: a raising UI callback silently ended all dictation")
    assert app.injector.delivered() == ["second dictation"]


def test_19_queue_order_is_preserved_under_load(make_app):
    app = make_app()
    n = 20
    app.stt_fake.text = lambda i: f"job-{i:02d}"
    app.stt_fake.delay = 0.001
    for _ in range(n):
        run_session(app)
    assert wait_rows(app, n, timeout=10.0)
    settle()
    finals = [r["final_text"] for r in rows_by_id(app)]
    assert finals == [f"job-{i:02d}" for i in range(n)], "FIFO order broken"
    assert app.injector.delivered() == finals
    assert len(rows(app)) == n, "I1: exactly one row per session"


def test_20_inflight_is_cleared_after_every_terminal_state(make_app):
    app = make_app()
    for pcm, text in ((LOUD, "ok text"), (QUIET, "unused"), (LOUD, "")):
        app.stt_fake.text = text
        run_session(app, pcm=pcm)
        assert wait_for(lambda: app._inflight is None, 3.0)
    app.stt_fake.error = RuntimeError("boom")
    run_session(app)
    assert wait_for(lambda: app._inflight is None, 3.0)
    assert wait_rows(app, 4, timeout=3.0)


def test_21_a_session_is_never_delivered_twice_under_a_stop_hammer(make_app):
    """Eight threads calling _stop_and_transcribe on one session."""
    for _ in range(30):
        app = make_app()
        app.stt_fake.text = "only once"
        app._start("hold")
        ready = threading.Barrier(8)

        def stopper():
            ready.wait(5)
            app._stop_and_transcribe()

        ts = [threading.Thread(target=stopper) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert wait_rows(app, 1, timeout=3.0)
        settle(0.05)
        assert len(rows(app)) == 1, "I1/I4: one stop, one row"
        assert app.injector.delivered() == ["only once"], "I4: delivered twice"
        assert app.stt_fake.calls == 1
        teardown(app)


def test_22_audio_thread_autostop_racing_the_ui_stop_yields_one_row(make_app):
    """The documented 60/60 duplicate-paste race, re-run from both threads."""
    for _ in range(30):
        app = make_app(audio__silence_stop_seconds=0.0)
        app.stt_fake.text = "hands free"
        app._start("toggle")
        ready = threading.Barrier(2)

        def audio_thread():
            ready.wait(5)
            app._on_level(0.0)      # silence -> vad fires -> _stop_and_transcribe

        def ui_thread():
            ready.wait(5)
            app._stop_and_transcribe()

        ts = [threading.Thread(target=audio_thread),
              threading.Thread(target=ui_thread)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert wait_rows(app, 1, timeout=3.0)
        settle(0.05)
        assert len(rows(app)) == 1
        assert app.injector.delivered().count("hands free") == 1
        teardown(app)


def test_23_hold_sessions_never_auto_stop_on_silence(make_app):
    app = make_app(audio__silence_stop_seconds=0.0)
    app._start("hold")
    for _ in range(50):
        app._on_level(0.0)
    settle(0.1)
    assert app._session is not None, "a hold session's stop is the finger, not silence"
    assert app._jobs.empty()
    assert rows(app) == []


# ==========================================================================
# C. Start / promote races
# ==========================================================================

def test_24_concurrent_starts_create_exactly_one_session(make_app):
    for _ in range(30):
        app = make_app(worker=False)
        ready = threading.Barrier(8)

        def starter():
            ready.wait(5)
            app._start("hold")

        ts = [threading.Thread(target=starter) for _ in range(8)]
        before = app._seq
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert app._seq == before + 1, "one session id burned per real start"
        assert app._session is not None
        assert app.recorder.begins == 1, "the microphone was begun more than once"
        teardown(app)


def test_25_start_that_loses_to_a_cancel_must_not_leave_the_ui_recording(make_app):
    """I5, forced: _start emits "recording" after releasing the lock.

    A cancel that lands in that window nulls the session, writes its row and
    emits "cancelled" -- and then the losing _start paints "recording" over it.
    The pill says recording, nothing is capturing, and no chord will stop it.
    """
    app = make_app(worker=False)
    entered_cue = threading.Event()
    cancel_done = threading.Event()

    def block_in_start_cue():
        entered_cue.set()
        cancel_done.wait(3.0)

    app.sounds.hooks["start"] = block_in_start_cue
    t = threading.Thread(target=lambda: app._start("hold"))
    t.start()
    assert entered_cue.wait(3.0)
    app._cancel_session()
    cancel_done.set()
    t.join(3.0)

    assert app._session is None
    assert app.states.last() != "recording", (
        "I5: UI left in 'recording' with no live session; "
        f"states={app.states.names()}")


def test_26_start_racing_cancel_free_running(make_app):
    """The same race with no help, to measure how often it lands naturally."""
    bad = 0
    for _ in range(50):
        app = make_app(worker=False)
        ready = threading.Barrier(2)

        def starter():
            ready.wait(5)
            app._start("hold")

        def canceller():
            ready.wait(5)
            app._cancel_session()

        ts = [threading.Thread(target=starter), threading.Thread(target=canceller)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        if app._session is None and app.states.last() == "recording":
            bad += 1
        teardown(app)
    assert bad == 0, f"I5: ended 'recording' with no session in {bad}/50 runs"


def test_27_failed_microphone_start_leaves_no_session_and_no_row(make_app):
    app = make_app()
    app.recorder.begin_error = OSError("device in use")
    app._start("hold")
    settle(0.1)
    assert app._session is None
    assert app.states.last() == "error"
    assert app._jobs.empty()
    assert rows(app) == []
    # and the app must still be usable afterwards
    app.recorder.begin_error = None
    app.stt_fake.text = "recovered"
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0)
    assert statuses(app) == ["ok"]


def test_28_promote_from_the_audio_thread_during_a_stop(make_app):
    """PROMOTE_TOGGLE and STOP_AND_TRANSCRIBE arriving together."""
    for _ in range(30):
        app = make_app()
        app.stt_fake.text = "promoted or not"
        app._start("hold")
        ready = threading.Barrier(2)

        def promoter():
            ready.wait(5)
            app._promote()

        def stopper():
            ready.wait(5)
            app._stop_and_transcribe()

        ts = [threading.Thread(target=promoter), threading.Thread(target=stopper)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert wait_rows(app, 1, timeout=3.0)
        settle(0.03)
        assert len(rows(app)) == 1
        assert rows(app)[0]["mode"] in ("hold", "toggle")
        assert app.injector.delivered() == ["promoted or not"]
        teardown(app)


def test_29_promote_after_the_session_is_claimed_is_a_no_op(make_app):
    app = make_app()
    app._start("hold")
    app._stop_and_transcribe()
    before = list(app.states.names())
    app._promote()
    assert app.states.names() == before, "promote must not repaint a dead session"
    assert wait_rows(app, 1, timeout=3.0)
    assert rows(app)[0]["mode"] == "hold"


def test_30_promote_on_a_toggle_session_is_a_no_op(make_app):
    app = make_app(worker=False)
    app._start("toggle")
    before = list(app.states.names())
    app._promote()
    assert app.states.names() == before
    assert app._session.mode == "toggle"


def test_31_promote_clears_silence_accumulated_during_the_hold(make_app):
    """A hold session that sat quiet must not auto-stop the instant it is
    promoted -- the user has only just asked for hands-free."""
    app = make_app(audio__silence_stop_seconds=1.0, worker=False)
    app._start("hold")
    app.vad.silent_for = 0.99          # nearly at the limit while holding
    app._promote()
    app._on_level(0.0)
    assert app._session is not None, "promotion inherited the hold phase's silence"


# ==========================================================================
# D. Cancel semantics (I2, I3)
# ==========================================================================

def test_32_cancel_between_enqueue_and_pickup_must_not_deliver(make_app):
    """I2, deterministic.

    The worker publishes _inflight only after it has pulled the job. A cancel
    that lands after _jobs.put but before that assignment finds _session None
    AND _inflight None, so it cancels nothing at all -- while telling the user
    'cancelled'. The transcript is then pasted into their document.
    """
    app = make_app(worker=False)          # worker not running == the window, held open
    app.stt_fake.text = "must not be pasted"
    run_session(app)
    assert not app._jobs.empty()
    app._cancel_session()
    assert app.states.names()[-1] == "cancelled"

    start_worker(app)                     # the worker now picks the job up
    assert wait_rows(app, 1, timeout=3.0)
    settle()
    assert app.injector.delivered() == [], (
        "I2: text from a cancelled session reached the clipboard")
    assert statuses(app) == ["cancelled"]


def test_33_cancel_with_a_backlog_hits_the_wrong_session(make_app):
    """I2 + I3, deterministic.

    Two dictations back to back: A is inside the model, B is queued behind it.
    Esc is meant for B, the one the user just stopped. _cancel_session finds no
    live session and cancels _inflight instead -- which is A. A, which the user
    never cancelled, is destroyed; B, which they did cancel, is pasted.
    """
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = ["alpha", "bravo"]

    run_session(app)                                  # A -> worker, blocks in STT
    assert app.stt_fake.entered.wait(3.0)
    assert wait_for(lambda: app._inflight is not None, 2.0)
    run_session(app)                                  # B -> queued behind A

    app._cancel_session()                             # the user presses Esc
    app.stt_fake.gate.set()
    assert wait_rows(app, 2, timeout=5.0)
    settle()

    delivered = app.injector.delivered()
    by_text = {r["raw_text"]: r["status"] for r in rows(app)}
    problems = []

    # I2 as originally written was `if delivered: ...` — no session may be
    # delivered once a cancel is acknowledged. That conflated "the session the
    # user cancelled" with "any session", which was invisible at the time
    # because the code cancelled the wrong one and both symptoms co-occurred.
    #
    # Esc here is meant for B. A was never cancelled, so A SHOULD be delivered.
    # The real invariant is that the cancelled session's text does not land.
    if "bravo" in delivered:
        problems.append(f"I2: the cancelled session was delivered anyway: {delivered}")
    if by_text.get("bravo") != "cancelled":
        problems.append(f"I2: Esc did not reach the session it was meant for: {by_text}")
    if by_text.get("alpha") == "cancelled":
        problems.append("I3: the cancel destroyed session A, which predates the Esc")
    assert not problems, "; ".join(problems)


def test_34_a_completed_session_is_not_destroyed_by_a_later_cancel(make_app):
    """I3: deliver session A, then press Esc. A's row must stay 'ok'."""
    app = make_app()
    app.stt_fake.text = "already delivered"
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0)
    assert wait_for(lambda: app._inflight is None, 2.0)
    app._cancel_session()
    settle()
    assert len(rows(app)) == 1, "the late Esc wrote a second row"
    assert rows(app)[0]["status"] == "ok", "a finished session was retro-cancelled"
    assert app.injector.delivered() == ["already delivered"]


def test_35_double_cancel_still_writes_exactly_one_row(make_app):
    for _ in range(30):
        app = make_app()
        app._start("hold")
        ready = threading.Barrier(2)

        def canceller():
            ready.wait(5)
            app._cancel_session()

        ts = [threading.Thread(target=canceller) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        settle(0.05)
        assert len(rows(app)) == 1, "I1: a double Esc wrote %d rows" % len(rows(app))
        assert statuses(app) == ["cancelled"]
        teardown(app)


def test_36_cancel_while_idle_is_harmless(make_app):
    app = make_app()
    app._cancel_session()
    app._cancel_session()
    settle(0.1)
    assert rows(app) == []
    assert app._session is None
    assert app.sounds.played == [], "no cue for a cancel with nothing to cancel"


def test_37_a_cancel_that_beats_transcription_is_never_lost(make_app):
    """I2, free-running: 40 runs of Esc landing on top of the chord release.

    The criterion is not "who won the lock" but "was Esc in time". If
    _cancel_session had already returned before the model was even called, the
    user cancelled in time and the transcript must not be delivered.
    """
    bad = []
    for i in range(40):
        app = make_app()
        text = f"race-{i:02d}"
        app.stt_fake.text = text
        app.stt_fake.delay = 0.002
        app._start("hold")
        ready = threading.Barrier(2)
        observed = {}

        def stopper():
            ready.wait(5)
            app._stop_and_transcribe()

        def canceller():
            ready.wait(5)
            app._cancel_session()
            # transcription had not started when Esc completed
            observed["in_time"] = app.stt_fake.calls == 0

        ts = [threading.Thread(target=stopper), threading.Thread(target=canceller)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        wait_for(lambda: app._jobs.empty() and app._inflight is None, 3.0)
        settle(0.05)
        got = rows(app)
        delivered = app.injector.delivered()
        if len(got) != 1:
            bad.append((i, "rows=%d" % len(got), statuses(app)))
        elif observed.get("in_time") and delivered:
            bad.append((i, "Esc completed before transcription began, "
                           "text delivered anyway", delivered, got[0]["status"]))
        elif got[0]["status"] == "cancelled" and delivered:
            bad.append((i, "row says cancelled but text was delivered", delivered))
        teardown(app)
    assert not bad, f"{len(bad)}/40 runs violated the cancel contract: {bad[:4]}"


def test_38_no_comet_is_launched_after_a_cancel_is_acknowledged(make_app):
    """The transcript reaches the clipboard before the comet flies, which is
    correct -- a failed animation must not cost the user their words. But the
    clipboard is not the document. If Esc lands in that window the app has one
    more chance to check `session.cancelled` before it launches the comet that
    pastes into the user's document. It never takes it."""
    app = make_app()
    app.stt_fake.text = "clipboard first"

    def cancel_inside_copy(text):
        app._cancel_session()

    app.injector.on_copy = cancel_inside_copy
    run_session(app)
    assert wait_rows(app, 1, timeout=3.0)
    settle()
    assert len(rows(app)) == 1, "I1"
    assert app.injector.delivered() == ["clipboard first"], "I4: delivered twice"
    names = app.states.names()
    assert "cancelled" in names
    after_cancel = names[names.index("cancelled") + 1:]
    assert "flying" not in after_cancel, (
        "the comet was launched into the document after the cancel was "
        f"acknowledged: {names}")


def test_39_cancelled_before_transcription_never_delivers(make_app):
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = "never mind"
    run_session(app)
    assert app.stt_fake.entered.wait(3.0)
    app._cancel_session()          # cancel lands while STT is still running
    app.stt_fake.gate.set()
    assert wait_rows(app, 1, timeout=3.0)
    settle()
    assert app.injector.delivered() == []
    assert statuses(app) == ["cancelled"]


def test_40_a_second_session_may_start_while_the_first_is_still_processing(make_app):
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = ["first", "second"]
    run_session(app)
    assert app.stt_fake.entered.wait(3.0)
    app._start("hold")                       # user starts talking again
    assert app._session is not None
    assert app._session.id == 2, "the new session must get its own id"
    app._stop_and_transcribe()
    app.stt_fake.gate.set()
    assert wait_rows(app, 2, timeout=5.0)
    settle()
    assert statuses(app) == ["ok", "ok"]
    assert app.injector.delivered() == ["first", "second"]


def test_41_ballistic_aim_belongs_to_the_session_that_stopped(make_app):
    """The comet 'flies to where you were when you stopped talking'. With a
    backlog, _aim is a single shared field that the second stop overwrites
    before the first session's comet is launched."""
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = ["first", "second"]
    points = iter([(100, 100), (900, 900)])
    app._cursor_point = lambda: next(points)

    run_session(app)                                  # stop 1 -> aim (100,100)
    assert app.stt_fake.entered.wait(3.0)
    run_session(app)                                  # stop 2 -> aim (900,900)
    app.stt_fake.gate.set()
    assert wait_rows(app, 2, timeout=5.0)
    settle()
    aims = [kw.get("aim") for kw in app.states.kwargs_for("flying")]
    assert aims == [(100, 100), (900, 900)], (
        f"the first comet was aimed at the second session's cursor: {aims}")


def test_42_shutdown_does_not_silently_drop_a_queued_dictation(make_app):
    """stop() closes the stores and posts the sentinel without draining. A
    dictation the user has already finished speaking is then lost with no row."""
    app = make_app()
    app.stt_fake.gate = threading.Event()
    app.stt_fake.text = ["in flight", "queued"]
    run_session(app)
    assert app.stt_fake.entered.wait(3.0)
    run_session(app)                       # queued behind the in-flight one
    app.stop()
    app.stt_fake.gate.set()
    time.sleep(0.3)
    app.history = app_mod.History(app_mod.data_dir() / "history.db")
    got = statuses(app)
    assert len(got) == 2, (
        f"I1: {len(got)} rows for 2 stopped sessions after stop(); got {got}")


def test_43_chaos_start_stop_cancel_promote_holds_every_invariant(make_app):
    """Random interleavings from four threads, 25 rounds.

    Checks only what must be true in every one of them: never more rows than
    sessions started, never a delivery of a session whose row says cancelled,
    and never the same transcript delivered twice.
    """
    rnd = random.Random(20260830)
    for run in range(25):
        app = make_app()
        app.stt_fake.text = lambda n, r=run: f"r{r}-{n}"
        app.stt_fake.delay = rnd.choice((0.0, 0.001))
        # Bound methods are re-created per attribute access, so capture once.
        ops = [("start", app._start),
               ("stop", app._stop_and_transcribe),
               ("cancel", app._cancel_session),
               ("promote", app._promote),
               ("discard", app._discard)]

        def churn(seed):
            local = random.Random(seed)
            for _ in range(12):
                name, op = local.choice(ops)
                try:
                    if name == "start":
                        op(local.choice(("hold", "toggle")))
                    else:
                        op()
                except Exception as exc:      # noqa: BLE001 - the point of the test
                    failures.append(("raised", name, repr(exc)))
                time.sleep(local.choice((0.0, 0.0005)))

        failures = []
        ts = [threading.Thread(target=churn, args=(rnd.random(),)) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        # settle: drain whatever is left in the pipeline
        wait_for(lambda: app._jobs.empty() and app._inflight is None, 5.0)
        settle(0.1)

        assert not failures, f"run {run}: a lifecycle call raised: {failures[:3]}"
        starts = app._seq
        got = rows(app)
        assert len(got) <= starts, (
            f"run {run}: {len(got)} rows for {starts} sessions -- a session "
            f"wrote more than one row")
        delivered = app.injector.delivered()
        assert len(delivered) == len(set(delivered)), (
            f"run {run}: I4, the same transcript was delivered twice: {delivered}")
        cancelled_finals = {r["final_text"] for r in got
                            if r["status"] == "cancelled" and r["final_text"]}
        leaked = cancelled_finals & set(delivered)
        assert not leaked, f"run {run}: I2, cancelled text delivered: {leaked}"
        teardown(app)


def test_44_session_ids_are_unique_across_a_start_stop_hammer(make_app):
    app = make_app(worker=False)
    seen = []
    lock = threading.Lock()

    def churn():
        for _ in range(20):
            app._start("hold")
            s = app._session
            if s is not None:
                with lock:
                    seen.append(s.id)
            app._stop_and_transcribe()

    ts = [threading.Thread(target=churn) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    jobs = []
    while True:
        try:
            jobs.append(app._jobs.get_nowait())
        except queue.Empty:
            break
    ids = [j[1].id for j in jobs if j is not None]
    assert ids, "nothing was enqueued at all"
    assert len(ids) == len(set(ids)), "I4: the same session was enqueued twice"
    assert len(ids) <= app._seq, "more jobs than sessions were ever created"
    assert len(set(seen)) == len(seen), "the same session id was handed out twice"


# ==========================================================================
# E. How a cancel reaches an app with no live session at all
# ==========================================================================

def test_45_the_silence_autostop_leaves_the_chord_fsm_believing_it_is_recording(make_app):
    """The audio thread ends the session; nothing tells the chord FSM.

    This is the bridge that makes the _inflight cancel fallback reachable: the
    FSM still reads REC_TOGGLE, so Esc keeps emitting CANCEL into an app that
    has no live session, for as long as the user leaves the keyboard alone.
    """
    from murmur.platform.win.chord import St

    app = make_app(audio__silence_stop_seconds=0.0)
    app.stt_fake.gate = threading.Event()
    app.hotkeys.fsm.adopt_toggle_session()          # hands-free session is live
    app._start("toggle")
    app._on_level(0.0)                              # audio thread auto-stops it
    assert app._session is None, "the auto-stop did not fire"
    app.stt_fake.gate.set()
    assert wait_rows(app, 1, timeout=3.0)

    assert app.hotkeys.fsm.state is not St.REC_TOGGLE, (
        "the FSM still thinks a session is recording after the audio thread "
        "auto-stopped it; every later Esc is dispatched into an empty app")


def test_46_pump_dispatched_cancel_must_not_acknowledge_a_cancel_it_cannot_do(make_app):
    """The faithful end-to-end replay, driven through pump().

    Hands-free session auto-stops on silence. The FSM never hears about it, so
    the user's Esc is delivered as a real Act.CANCEL. By then the worker has
    finished: _session is None and _inflight is None, so the cancel matches
    nothing -- yet the app still announces 'cancelled' to a user whose text has
    already gone into the document.
    """
    from murmur.platform.win.chord import Act

    app = make_app(audio__silence_stop_seconds=0.0)
    app.stt_fake.text = "already in your document"
    app.hotkeys.fsm.adopt_toggle_session()
    app._start("toggle")
    app._on_level(0.0)                              # auto-stop from the audio thread
    assert wait_rows(app, 1, timeout=3.0)
    assert wait_for(lambda: app._inflight is None, 2.0)
    assert app.injector.delivered() == ["already in your document"]

    app.hotkeys.actions.put(Act.CANCEL)             # the user presses Esc
    app.pump()
    settle()

    assert len(rows(app)) == 1, "I1: the late Esc wrote a second row"
    assert rows(app)[0]["status"] == "ok", "I3: a delivered session was retro-cancelled"
    assert app.states.last() != "cancelled", (
        "the app acknowledged a cancel it did not and could not perform; "
        f"states={app.states.names()}")
