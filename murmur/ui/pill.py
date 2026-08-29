"""The floating status pill.

Small and sleek: ~140x36px, bottom-centre, above the taskbar.

Two hard constraints, both from CLAUDE.md:

  * It must NEVER take focus. If it activates, Ctrl+V lands in the pill instead
    of the user's app and the dictation goes nowhere. Hence WS_EX_NOACTIVATE
    plus Qt.Tool plus WA_ShowWithoutActivating plus click-through.
  * No gradient, no glow. Checked against the design library: gradient is
    refused by 100 independent critiques, glow by 19. Motion and a single
    accent colour carry the design; the waveform is the visual interest.

Everything animates off ONE 60fps timer through ONE spring integrator, so a
state change that interrupts another blends instead of snapping.
"""

import ctypes
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .motion import Spring
from .waveform import BarModel

W, H, RADIUS = 140, 36, 18
TOGGLE_EXTRA = 34            # hands-free pill is wider, to fit the label

BODY = QColor(14, 14, 16, 235)
HAIRLINE = QColor(255, 255, 255, 20)

ACCENT = {
    "recording": QColor("#5B8DEF"),
    "transcribing": QColor("#E8B84B"),
    "polishing": QColor("#E8B84B"),
    "done": QColor("#4ADE80"),
    "copied": QColor("#4ADE80"),
    "cancelled": QColor("#EF4444"),
    "error": QColor("#EF4444"),
}
LABEL = {
    "copied": "copied — press Ctrl+V",
    "transcribing": "transcribing",
    "polishing": "cleaning up",
    "cancelled": "cancelled",
    "error": "failed",
}

ACTIVE = ("recording", "transcribing", "polishing")
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


class Pill(QWidget):
    def __init__(self, offset_px: int = 48, parent=None):
        super().__init__(
            parent,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

        self.offset_px = offset_px
        self.state = "idle"
        self.mode = "hold"
        self.level = 0.0

        self.bars = BarModel(n=5)
        self.opacity = Spring(0.0, stiffness=200, damping=24)
        self.scale = Spring(0.86, stiffness=220, damping=20)
        self.rise = Spring(12.0, stiffness=200, damping=24)
        self.width_s = Spring(float(W), stiffness=190, damping=24)
        self.check = Spring(0.0, stiffness=200, damping=18)

        self.sweep = 0.0
        self.shake = 0.0
        self._done_frames = 0
        self._hardened = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # Generous canvas: the pill scales, shakes and rises inside it.
        self.resize(W * 2, H * 3)

    # --- window plumbing --------------------------------------------------

    def _place(self) -> None:
        """Bottom-centre of the screen the user is actually working on.

        A top-level widget that has never been moved reports the PRIMARY screen,
        so a pill placed from self.screen() always appeared on monitor 1 no
        matter where the focused window was. Follow the cursor instead, which is
        where the user is looking, and re-place on every appearance.
        """
        screen = None
        try:
            from PySide6.QtGui import QCursor, QGuiApplication

            screen = QGuiApplication.screenAt(QCursor.pos())
        except Exception:
            pass
        if screen is None:
            screen = self.screen()
        if screen is None:
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            int(geo.center().x() - self.width() / 2),
            int(geo.bottom() - self.height() - self.offset_px),
        )

    def _harden(self) -> None:
        """WS_EX_NOACTIVATE at the Win32 level. Qt's flags alone still allow the
        window to be activated in some cases, and an activated pill eats the paste."""
        if self._hardened:
            return
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                             ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            self._hardened = True
        except Exception:
            pass

    # --- state ------------------------------------------------------------

    def set_level(self, level: float) -> None:
        """Called from the audio thread 60x/second. Deliberately a plain float
        write rather than a queued signal — the paint timer reads it on the UI
        thread and 60 queued events a second would be pure overhead."""
        self.level = level

    @Slot(str, str)
    def apply_state(self, state: str, mode: str) -> None:
        """Queued entry point from worker threads. Qt requires an invokable
        slot; calling set_state directly across threads corrupts painting."""
        self.set_state(state, mode or None)

    def set_state(self, state: str, mode: str | None = None) -> None:
        if mode:
            self.mode = mode
        if state == self.state:
            self._retarget_width()
            return
        self.state = state

        if state in ACTIVE:
            self._place()          # the focused monitor may have changed
            if not self.isVisible():
                # Fresh appearance: start small, low and transparent so the
                # springs have somewhere to travel from.
                self.opacity.snap_to(0.0)
                self.scale.snap_to(0.86)
                self.rise.snap_to(12.0)
                self._place()
                self.show()
                self._harden()
            self.opacity.target = 1.0
            self.scale.target = 1.0
            self.rise.target = 0.0
            self.check.target = 0.0
            self._retarget_width()
            if not self._timer.isActive():
                self._timer.start(16)
        elif state == "done":
            # Hold full opacity while the tick strokes itself on; _tick starts
            # the fade once it has actually been drawn. Fading concurrently
            # made the confirmation almost invisible.
            self.check.snap_to(0.0)
            self.check.target = 1.0
            self.opacity.target = 1.0
            self.scale.target = 1.0
        elif state == "copied":
            # Modifiers were still held so the paste was withheld. Hold this
            # longer than the tick: the user has to act on it.
            self.opacity.target = 1.0
            self.scale.target = 1.0
            self.check.snap_to(0.0)
            self._retarget_width()
        elif state in ("cancelled", "error"):
            self.shake = 1.0
            self.opacity.target = 0.0
            self.scale.target = 0.94
        else:                                   # idle
            self.opacity.target = 0.0
            self.scale.target = 0.94

    def _retarget_width(self) -> None:
        wide = self.state in ACTIVE and (
            self.mode == "toggle" or self.state in LABEL)
        self.width_s.target = float(W + TOGGLE_EXTRA if wide else W)

    # --- frame ------------------------------------------------------------

    def _tick(self) -> None:
        dt = 1 / 60
        for s in (self.opacity, self.scale, self.rise, self.width_s, self.check):
            s.step(dt)

        if self.state == "recording":
            if self.level > 0.012:
                self.bars.step(self.level, dt)
            else:
                self.bars.breathe(dt)
        elif self.state in ("transcribing", "polishing"):
            self.sweep = (self.sweep + dt * 1.4) % 1.0

        if self.state == "copied":
            self._done_frames += 1
            if self._done_frames > 210:            # ~3.5s to read and act
                self.opacity.target = 0.0
                self.scale.target = 0.94
        elif self.state == "done":
            self._done_frames += 1
            # ~0.55s of visible confirmation once the tick has finished drawing.
            if self.check.value > 0.92 and self._done_frames > 33:
                self.opacity.target = 0.0
                self.scale.target = 0.94
        else:
            self._done_frames = 0

        if self.shake > 0.0:
            self.shake = max(0.0, self.shake - dt * 5.0)

        self.update()

        if self.opacity.target == 0.0 and self.opacity.value < 0.01:
            self._timer.stop()
            self.hide()

    # --- painting ---------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, self.opacity.value)))

        w = self.width_s.value * self.scale.value
        h = H * self.scale.value
        dx = math.sin(self.shake * 40.0) * 6.0 * self.shake
        rect = QRectF(
            (self.width() - w) / 2 + dx,
            (self.height() - h) / 2 + self.rise.value,
            w, h,
        )

        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS, RADIUS)
        p.fillPath(path, BODY)
        p.setPen(QPen(HAIRLINE, 1))
        p.drawPath(path)

        accent = ACCENT.get(self.state, ACCENT["recording"])
        label = LABEL.get(self.state) or (
            "hands-free" if self.state == "recording" and self.mode == "toggle"
            else "")

        if self.state == "recording":
            self._draw_bars(p, rect, accent, shifted=bool(label))
        elif self.state in ("transcribing", "polishing"):
            self._draw_sweep(p, rect, accent)
        elif self.state == "done":
            self._draw_check(p, rect, accent)

        if label:
            self._draw_label(p, rect, label, accent)
        p.end()

    def _draw_bars(self, p, rect, accent, shifted: bool) -> None:
        heights = self.bars.heights()
        bw, gap = 4.0, 5.0
        total = len(heights) * bw + (len(heights) - 1) * gap
        cx = (rect.left() + 16) if shifted else (rect.center().x() - total / 2)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        for i, hv in enumerate(heights):
            bh = 5.0 + hv * (rect.height() - 15.0)
            p.drawRoundedRect(
                QRectF(cx + i * (bw + gap), rect.center().y() - bh / 2, bw, bh),
                2.0, 2.0)

    def _draw_sweep(self, p, rect, accent) -> None:
        """A travelling highlight — a progress cue, not decoration. Drawn as a
        trail of fading dots rather than a gradient band."""
        cx = rect.left() + 16 + self.sweep * 28.0
        p.setPen(Qt.NoPen)
        for i in range(6):
            c = QColor(accent)
            c.setAlpha(int(200 * (1.0 - i / 6.0)))
            p.setBrush(c)
            p.drawEllipse(QPointF(cx - i * 4.5, rect.center().y()), 2.2, 2.2)

    def _draw_check(self, p, rect, accent) -> None:
        """Strokes itself on: the tick is drawn to a fraction of its length."""
        t = max(0.0, min(1.0, self.check.value))
        if t <= 0.01:
            return
        c = rect.center()
        a = QPointF(c.x() - 8, c.y() + 1)
        b = QPointF(c.x() - 2.5, c.y() + 6)
        d = QPointF(c.x() + 8, c.y() - 6)
        p.setPen(QPen(accent, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if t <= 0.4:
            k = t / 0.4
            p.drawLine(a, QPointF(a.x() + (b.x() - a.x()) * k,
                                  a.y() + (b.y() - a.y()) * k))
        else:
            k = (t - 0.4) / 0.6
            p.drawLine(a, b)
            p.drawLine(b, QPointF(b.x() + (d.x() - b.x()) * k,
                                  b.y() + (d.y() - b.y()) * k))

    def _draw_label(self, p, rect, text: str, accent) -> None:
        f = QFont("Segoe UI", 8)
        f.setWeight(QFont.Medium)
        p.setFont(f)
        c = QColor(accent)
        c.setAlpha(220)
        p.setPen(c)
        p.drawText(rect.adjusted(70, 0, -12, 0),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
