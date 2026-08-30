"""Entry point.

The Qt event loop owns the UI thread. Session state arrives from the audio and
worker threads, so every UI call is marshalled across — touching a QWidget from
another thread is undefined behaviour, and the symptoms look like random paint
corruption rather than a threading bug.
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from .app import MurmurApp, data_dir
from .config import Config
from .platform.win import autostart, single_instance
from .version import build_id
from .ui.comet import Comet
from .ui.pill import Pill

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = data_dir() / "murmur.log"


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stream = logging.StreamHandler()
    # pythonw has no console; a stray non-cp1252 glyph must never raise
    # inside logging and take the app down.
    if getattr(stream.stream, "reconfigure", None):
        try:
            stream.stream.reconfigure(errors="replace")
        except Exception:
            pass
    handlers = [stream]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    for noisy in ("faster_whisper", "httpx", "huggingface_hub", "urllib3",
                  "comtypes", "comtypes.client._code_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class UiBridge(QObject):
    """Marshals session state from worker threads onto the Qt thread.

    A Qt signal rather than invokeMethod, because the comet needs to carry a
    screen coordinate across the thread boundary as well as a state name.
    """

    changed = Signal(str, str, int, int)

    def __init__(self, pill: Pill, comet=None, injector=None, sounds=None):
        super().__init__()
        self.pill = pill
        self.comet = comet
        self.injector = injector
        self.sounds = sounds
        self.changed.connect(self._on_changed, Qt.QueuedConnection)

    def __call__(self, state: str, **kw) -> None:
        if state == "level":
            # 60 events a second from the audio thread. A plain float write is
            # read by the paint timer on the UI thread; queueing each one would
            # be pure overhead.
            self.pill.set_level(kw.get("level", 0.0))
            return
        aim = kw.get("aim") or (-1, -1)
        self.changed.emit(state, kw.get("mode") or "", int(aim[0]), int(aim[1]))

    def _on_changed(self, state: str, mode: str, ax: int, ay: int) -> None:
        """UI thread."""
        if state == "flying" and self.comet is not None and ax >= 0:
            self._fly(QPointF(float(ax), float(ay)))
            return
        if state == "flying":
            # No comet available; deliver immediately rather than not at all.
            # The return value is the ONLY signal that the text was not
            # delivered. Discarding it painted "done" over a refused paste, and
            # done and copied render identically -- so a stranded transcript
            # looked exactly like a successful one.
            pasted = self.injector.paste() if self.injector is not None else False
            self.pill.apply_state("done" if pasted else "copied", mode)
            return
        self.pill.apply_state(state, mode)

    def _fly(self, aim: QPointF) -> None:
        from murmur.ui.pill import ACCENT

        start = self.pill.capsule_centre()
        self.pill.apply_state("launching", "")

        def landed():
            # The keystroke and the splash fire the instant the comet arrives.
            if self.sounds is not None:
                self.sounds.play("arrive")
            pasted = False
            if self.injector is not None:
                try:
                    pasted = self.injector.paste()
                except Exception:
                    logging.getLogger("murmur").exception("paste on landing failed")
            # "done" and "copied" paint identically; the hold is the only
            # difference. Painting "done" over a refused paste made a stranded
            # transcript look exactly like a delivered one.
            self.pill.apply_state("done" if pasted else "copied", "")

        self.comet.launch(start, aim,
                          colour=ACCENT["done"].name(), on_land=landed)


class ClipboardWatcher:
    """Capture path C: notice when the user copies a corrected version of
    something we pasted."""

    def __init__(self, app, corrections):
        self.corrections = corrections
        self.clipboard = app.clipboard()
        self._last = None

    def poll(self) -> None:
        try:
            text = self.clipboard.text()
        except Exception:
            return
        if not text or text == self._last:
            return
        self._last = text
        try:
            learned = self.corrections.offer_clipboard(text)
            if learned:
                logging.getLogger("murmur").info(
                    "learned %d term(s) from a corrected copy", learned)
        except Exception:
            logging.getLogger("murmur").debug("clipboard learning failed",
                                              exc_info=True)


def main() -> int:
    _configure_logging()
    log = logging.getLogger("murmur")

    if not single_instance.acquire():
        log.info("already running; asking the live instance to start a session")
        single_instance.signal_existing()
        return 0

    cfg = Config.load(ROOT / "settings.json")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Murmur")

    pill = Pill(offset_px=cfg.get("ui.pill_offset_px"),
                show_when_idle=cfg.get("ui.idle_indicator", True))
    # One source of truth. The pill used to carry its own copy of this number,
    # which drifted above his actual speaking voice and silently stopped the
    # meter from ever animating for him.
    pill.speech_threshold = cfg.get("audio.speech_rms_threshold")
    comet = Comet() if cfg.get("ui.comet", True) else None
    bridge = UiBridge(pill, comet=comet)
    murmur = MurmurApp(cfg, on_state=bridge)
    bridge.injector = murmur.injector
    bridge.sounds = murmur.sounds

    try:
        murmur.start()
    except Exception:
        log.exception("could not install the global hotkey")
        QMessageBox.critical(
            None, "Murmur",
            "Could not install the global keyboard hook.\n\n"
            "Another copy of Murmur may already be running.")
        return 1

    # Panels are created on first use: building two windows at startup would
    # slow the launch of something that spends most of its life invisible.
    windows = {}

    def show_history():
        if "history" not in windows:
            from .ui.history_win import HistoryWindow

            windows["history"] = HistoryWindow(
                murmur.history, murmur.injector, murmur.corrections)
        w = windows["history"]
        w.reload()
        w.show()
        w.raise_()
        w.activateWindow()

    def show_vocab():
        if "vocab" not in windows:
            from .ui.vocab_win import VocabWindow

            windows["vocab"] = VocabWindow(murmur.vocab)
        w = windows["vocab"]
        w.reload()
        w.show()
        w.raise_()
        w.activateWindow()

    def quit_app():
        pill.set_state("off")
        murmur.stop()
        app.quit()

    # The tick and cross on the pill.
    pill.accepted.connect(murmur._stop_and_transcribe)
    pill.cancelled_by_user.connect(murmur._cancel_session)

    def on_listening(active: bool):
        # The indicator is the honest answer to "can I dictate right now?", so
        # it must disappear the moment the hotkeys are paused.
        pill.set_state("armed" if active else "off")

    from .ui.tray import Tray

    tray = Tray(murmur, show_history, show_vocab, quit_app,
                autostart_command=autostart.default_command(),
                on_listening=on_listening, cfg=cfg)

    # First run honours the configured default; after that the tray checkbox
    # is the source of truth and the config is not re-applied.
    if cfg.get("autostart") and not autostart.is_enabled():
        autostart.set_enabled(True, autostart.default_command())
        tray.autostart_action.blockSignals(True)
        tray.autostart_action.setChecked(True)
        tray.autostart_action.blockSignals(False)

    single_instance.listen(lambda: murmur._start("toggle", external=True))
    murmur.preload()

    pump = QTimer(app)
    pump.timeout.connect(murmur.pump)
    pump.start(16)

    watcher = ClipboardWatcher(app, murmur.corrections)
    slow = QTimer(app)
    slow.timeout.connect(murmur.corrections.poll)
    slow.timeout.connect(watcher.poll)
    slow.start(2000)

    pill.set_state("armed")

    log.info("Murmur %s ready - hold Ctrl+Win to dictate, "
             "Ctrl+Win+Space for hands-free, Esc to cancel", build_id())
    try:
        return app.exec()
    finally:
        murmur.stop()


if __name__ == "__main__":
    sys.exit(main())
