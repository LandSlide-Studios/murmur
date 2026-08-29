"""Entry point.

The Qt event loop owns the UI thread. Session state arrives from the audio and
worker threads, so every UI call is marshalled across — touching a QWidget from
another thread is undefined behaviour, and the symptoms look like random paint
corruption rather than a threading bug.
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Q_ARG, QMetaObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .app import MurmurApp, data_dir
from .config import Config
from .platform.win import autostart, single_instance
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


class UiBridge:
    """Marshals session state from worker threads onto the Qt thread."""

    def __init__(self, pill: Pill):
        self.pill = pill

    def __call__(self, state: str, **kw) -> None:
        if state == "level":
            # 60 events a second from the audio thread. A plain float write is
            # read by the paint timer on the UI thread; queueing each one would
            # be pure overhead.
            self.pill.set_level(kw.get("level", 0.0))
            return
        QMetaObject.invokeMethod(
            self.pill, "apply_state", Qt.QueuedConnection,
            Q_ARG(str, state), Q_ARG(str, kw.get("mode") or ""))


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

    pill = Pill(offset_px=cfg.get("ui.pill_offset_px"))
    murmur = MurmurApp(cfg, on_state=UiBridge(pill))

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
        murmur.stop()
        app.quit()

    from .ui.tray import Tray

    tray = Tray(murmur, show_history, show_vocab, quit_app,
                autostart_command=autostart.default_command())

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

    log.info("Murmur ready - hold Ctrl+Win to dictate, "
             "Ctrl+Win+Space for hands-free, Esc to cancel")
    try:
        return app.exec()
    finally:
        murmur.stop()


if __name__ == "__main__":
    sys.exit(main())
