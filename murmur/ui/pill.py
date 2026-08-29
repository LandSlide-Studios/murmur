"""The floating status pill.

Slim by default. When nothing is happening it sits at the bottom of the screen
as a small dim capsule — enough to tell you dictation is armed, not enough to
notice while you work. Starting a session grows the same element rather than
swapping it, so the transition is one continuous morph.

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

# (width, height) per state. The capsule radius is always height/2, so it stays
# a true pill at every size and the morph between them reads as one object.
IDLE_SIZE = (58, 12)
REC_SIZE = (104, 24)
# Labelled states size themselves to the text; these are the fixed parts.
LABEL_LEAD = 44      # space before the text, holding the bars or the sweep
LABEL_TRAIL = 14     # breathing room after it

IDLE_OPACITY = 0.34

BODY = QColor(14, 14, 16, 235)
HAIRLINE = QColor(255, 255, 255, 20)

ACCENT = {
    "armed": QColor("#5B8DEF"),
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
TERMINAL = ("done", "copied", "cancelled", "error")

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


class Pill(QWidget):
    def __init__(self, offset_px: int = 48, show_when_idle: bool = True,
                 parent=None):
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
        self.show_when_idle = show_when_idle
        self.state = "off"
        self.mode = "hold"
        self.level = 0.0

        self.bars = BarModel(n=5)
        self.opacity = Spring(0.0, stiffness=200, damping=24)
        self.width_s = Spring(float(IDLE_SIZE[0]), stiffness=190, damping=23)
        self.height_s = Spring(float(IDLE_SIZE[1]), stiffness=190, damping=23)
        self.rise = Spring(10.0, stiffness=200, damping=24)
        self.check = Spring(0.0, stiffness=200, damping=18)

        self.sweep = 0.0
        self.shake = 0.0
        self._hold_frames = 0
        self._hardened = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # Canvas big enough for the widest label plus shake and rise headroom.
        self.resize(280, REC_SIZE[1] + 48)

    # --- window plumbing --------------------------------------------------

    def _place(self) -> None:
        """Bottom-centre of the screen the user is actually working on.

        A top-level widget that has never been moved reports the PRIMARY screen,
        so placing from self.screen() put the pill on monitor 1 regardless of
        where the focused window was. Follow the cursor instead.
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

    def _label_for(self, state: str) -> str:
        if state in LABEL:
            return LABEL[state]
        if state == "recording" and self.mode == "toggle":
            return "hands-free"
        return ""

    def _target_size(self) -> tuple[int, int]:
        if self.state == "armed":
            return IDLE_SIZE
        text = self._label_for(self.state)
        if not text:
            return REC_SIZE
        # Size to the text rather than a fixed width, or "hands-free" leaves a
        # third of the capsule empty while "copied - press Ctrl+V" overflows it.
        from PySide6.QtGui import QFontMetrics

        f = QFont("Segoe UI", 8)
        f.setWeight(QFont.Medium)
        width = LABEL_LEAD + QFontMetrics(f).horizontalAdvance(text) + LABEL_TRAIL
        return (int(width), REC_SIZE[1])

    def _retarget(self) -> None:
        w, h = self._target_size()
        self.width_s.target = float(w)
        self.height_s.target = float(h)

    def set_state(self, state: str, mode: str | None = None) -> None:
        # "idle" from the session layer means "no dictation running", which is
        # the armed indicator rather than nothing at all.
        if state == "idle":
            state = "armed" if self.show_when_idle else "off"
        if mode:
            self.mode = mode
        if state == self.state:
            self._retarget()
            return
        self.state = state

        if state == "off":
            self.opacity.target = 0.0
            return

        if not self.isVisible():
            self.opacity.snap_to(0.0)
            self.rise.snap_to(10.0)
            self.width_s.snap_to(float(IDLE_SIZE[0]))
            self.height_s.snap_to(float(IDLE_SIZE[1]))
            self._place()
            self.show()
            self._harden()
        else:
            self._place()          # the focused monitor may have changed

        if not self._timer.isActive():
            self._timer.start(16)

        self.rise.target = 0.0
        self._retarget()
        self._hold_frames = 0

        if state == "armed":
            self.opacity.target = IDLE_OPACITY
            self.check.target = 0.0
        elif state in ACTIVE:
            self.opacity.target = 1.0
            self.check.target = 0.0
        elif state in ("done", "copied"):
            # Hold full opacity while the tick strokes itself on; _tick starts
            # the return to armed once it has actually been drawn.
            self.check.snap_to(0.0)
            self.check.target = 1.0
            self.opacity.target = 1.0
        else:                                   # cancelled / error
            self.shake = 1.0
            self.opacity.target = 1.0

    # --- frame ------------------------------------------------------------

    def _settle_to_armed(self) -> None:
        """After a terminal state, shrink back to the armed indicator rather
        than vanishing — the user needs to know dictation is still available."""
        self.state = "armed" if self.show_when_idle else "off"
        self.check.target = 0.0
        self._hold_frames = 0
        self._retarget()
        self.opacity.target = IDLE_OPACITY if self.show_when_idle else 0.0

    def _tick(self) -> None:
        dt = 1 / 60
        for s in (self.opacity, self.width_s, self.height_s, self.rise,
                  self.check):
            s.step(dt)

        if self.state == "recording":
            if self.level > 0.012:
                self.bars.step(self.level, dt)
            else:
                self.bars.breathe(dt)
        elif self.state == "armed":
            self.bars.breathe(dt)
        elif self.state in ("transcribing", "polishing"):
            self.sweep = (self.sweep + dt * 1.4) % 1.0

        if self.state in TERMINAL:
            self._hold_frames += 1
            # "copied" needs reading; the rest are just confirmation.
            hold = 210 if self.state == "copied" else 42
            if self._hold_frames > hold:
                self._settle_to_armed()

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

        w = max(4.0, self.width_s.value)
        h = max(3.0, self.height_s.value)
        radius = h / 2.0
        dx = math.sin(self.shake * 40.0) * 5.0 * self.shake
        rect = QRectF(
            (self.width() - w) / 2 + dx,
            (self.height() - h) / 2 + self.rise.value,
            w, h,
        )

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.fillPath(path, BODY)
        p.setPen(QPen(HAIRLINE, 1))
        p.drawPath(path)
        # Nothing may be drawn outside the capsule. The sweep trail used to
        # spill past the left edge as loose dots on the desktop.
        p.setClipPath(path)

        accent = ACCENT.get(self.state, ACCENT["recording"])
        label = LABEL.get(self.state) or (
            "hands-free" if self.state == "recording" and self.mode == "toggle"
            else "")
        # Only label once the capsule has actually grown enough to hold text.
        show_label = bool(label) and h > 18

        if self.state in ("armed", "recording"):
            self._draw_bars(p, rect, accent, shifted=show_label)
        elif self.state in ("transcribing", "polishing"):
            self._draw_sweep(p, rect, accent)
        elif self.state in ("done", "copied"):
            self._draw_check(p, rect, accent)

        if show_label:
            self._draw_label(p, rect, label, accent)
        p.end()

    def _draw_bars(self, p, rect, accent, shifted: bool) -> None:
        """Bar geometry scales with the capsule, so the armed indicator is a
        miniature of the same waveform rather than a different graphic."""
        heights = self.bars.heights()
        scale = rect.height() / REC_SIZE[1]
        bw = max(1.3, 2.6 * scale)
        gap = max(1.5, 3.2 * scale)
        total = len(heights) * bw + (len(heights) - 1) * gap
        inset = max(5.0, 11.0 * scale)
        cx = (rect.left() + inset) if shifted else (rect.center().x() - total / 2)

        room = max(2.0, rect.height() - 9.0 * scale)
        floor = max(1.4, 2.4 * scale)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        for i, hv in enumerate(heights):
            bh = floor + hv * room
            p.drawRoundedRect(
                QRectF(cx + i * (bw + gap), rect.center().y() - bh / 2, bw, bh),
                bw / 2, bw / 2)

    def _draw_sweep(self, p, rect, accent) -> None:
        """A travelling highlight — a progress cue, not decoration. Drawn as a
        trail of fading dots rather than a gradient band."""
        lead_in = 13.0 + 5 * 3.6           # room for the whole trail
        cx = rect.left() + lead_in + self.sweep * 16.0
        p.setPen(Qt.NoPen)
        for i in range(6):
            c = QColor(accent)
            c.setAlpha(int(200 * (1.0 - i / 6.0)))
            p.setBrush(c)
            p.drawEllipse(QPointF(cx - i * 3.6, rect.center().y()), 1.8, 1.8)

    def _draw_check(self, p, rect, accent) -> None:
        """Strokes itself on: the tick is drawn to a fraction of its length."""
        t = max(0.0, min(1.0, self.check.value))
        if t <= 0.01 or rect.height() < 12:
            return
        c = rect.center()
        a = QPointF(c.x() - 5.5, c.y() + 0.5)
        b = QPointF(c.x() - 1.5, c.y() + 4.0)
        d = QPointF(c.x() + 5.5, c.y() - 4.0)
        p.setPen(QPen(accent, 2.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
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
        p.drawText(rect.adjusted(44, 0, -10, 0),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
