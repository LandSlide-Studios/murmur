"""System tray icon and menu.

The tray is the app's only persistent surface — there is no main window — so the
icon has to communicate state on its own: filled when armed, hollow when paused.
"""

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (QAction, QActionGroup, QColor, QIcon, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..platform.win import autostart
from ..sound import PACKS
from .theme import ACCENT, INK_3

log = logging.getLogger(__name__)


def _icon(active: bool) -> QIcon:
    """Draw the icon rather than shipping a .ico: it stays crisp at every DPI
    and there is no asset to lose."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    colour = QColor(ACCENT if active else INK_3)

    # Three bars: a waveform at a glance, matching the pill.
    heights = (0.34, 0.62, 0.44)
    bar_w, gap = 9.0, 8.0
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (size - total) / 2
    if active:
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
    else:
        p.setPen(QPen(colour, 4))
        p.setBrush(Qt.NoBrush)
    for h in heights:
        bh = size * h
        p.drawRoundedRect(QRectF(x, (size - bh) / 2, bar_w, bh), 4, 4)
        x += bar_w + gap
    p.end()
    return QIcon(pm)


class Tray(QSystemTrayIcon):
    LABELS = {
        "sotto": "Sotto — wood, felt and a swell",
        "velvet_thud": "Velvet Thud — sub pulses you feel",
        "warm_glass": "Warm Glass — muted mallet tones",
        "wood_bar": "Wood Bar — round marimba notes",
        "breath": "Breath — pure air, almost silent",
        "heartbeat": "Heartbeat — lub-dub pulses",
    }

    def __init__(self, app, on_history, on_vocab, on_quit,
                 autostart_command=None, on_listening=None, cfg=None):
        super().__init__(_icon(True))
        self.app = app
        self._autostart_command = autostart_command
        self._on_listening = on_listening
        self._cfg = cfg

        menu = QMenu()

        self.enabled_action = QAction("Listening", menu, checkable=True)
        self.enabled_action.setChecked(True)
        self.enabled_action.toggled.connect(self._toggle_enabled)
        menu.addAction(self.enabled_action)
        menu.addSeparator()

        history_action = QAction("History…", menu)
        history_action.triggered.connect(on_history)
        menu.addAction(history_action)

        vocab_action = QAction("Vocabulary…", menu)
        vocab_action.triggered.connect(on_vocab)
        menu.addAction(vocab_action)
        menu.addSeparator()

        sounds_menu = menu.addMenu("Sounds")
        self._sound_group = QActionGroup(sounds_menu)
        self._sound_group.setExclusive(True)
        current = getattr(app.sounds, "pack", "sotto")
        for pack in PACKS:
            act = QAction(self.LABELS.get(pack, pack), sounds_menu, checkable=True)
            act.setChecked(pack == current)
            act.setData(pack)
            act.triggered.connect(
                lambda _checked, p=pack: self._choose_pack(p))
            self._sound_group.addAction(act)
            sounds_menu.addAction(act)
        sounds_menu.addSeparator()
        self.mute_action = QAction("Mute", sounds_menu, checkable=True)
        self.mute_action.setChecked(not app.sounds.enabled)
        self.mute_action.toggled.connect(self._toggle_mute)
        sounds_menu.addAction(self.mute_action)
        menu.addSeparator()

        self.autostart_action = QAction("Launch at login", menu, checkable=True)
        self.autostart_action.setChecked(autostart.is_enabled())
        self.autostart_action.toggled.connect(self._toggle_autostart)
        menu.addAction(self.autostart_action)
        menu.addSeparator()

        quit_action = QAction("Quit Murmur", menu)
        quit_action.triggered.connect(on_quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self._refresh_tooltip(True)
        self.show()

    def _refresh_tooltip(self, active: bool) -> None:
        self.setToolTip(
            "Murmur — hold Ctrl+Win to dictate, Ctrl+Win+Space hands-free"
            if active else "Murmur — paused")

    def _toggle_enabled(self, on: bool) -> None:
        try:
            if on:
                self.app.hotkeys.start()
            else:
                self.app.hotkeys.stop()
        except Exception:
            log.exception("could not toggle the keyboard hook")
            return
        self.setIcon(_icon(on))
        self._refresh_tooltip(on)
        if self._on_listening is not None:
            self._on_listening(on)

    def _choose_pack(self, pack: str) -> None:
        """Switch packs and play the new ack, so the choice is audible at the
        moment it is made rather than on the next dictation."""
        if not self.app.sounds.set_pack(pack):
            return
        if self._cfg is not None:
            try:
                self._cfg.set("sound.pack", pack)
                self._cfg.save()
            except Exception:
                log.warning("could not persist the sound pack", exc_info=True)
        self.app.sounds.play("start")

    def _toggle_mute(self, muted: bool) -> None:
        self.app.sounds.enabled = not muted
        if self._cfg is not None:
            try:
                self._cfg.set("sound.enabled", not muted)
                self._cfg.save()
            except Exception:
                log.warning("could not persist the mute setting", exc_info=True)
        if not muted:
            self.app.sounds.play("start")

    def _toggle_autostart(self, on: bool) -> None:
        if not autostart.set_enabled(on, self._autostart_command):
            # Reflect reality rather than the click that failed.
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(autostart.is_enabled())
            self.autostart_action.blockSignals(False)
            self.showMessage("Murmur",
                             "Could not change launch-at-login.",
                             QSystemTrayIcon.Warning, 4000)
