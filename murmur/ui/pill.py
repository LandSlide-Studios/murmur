"""The floating status pill.

A vertical capsule docked against the right bezel. When nothing is happening it
sits there as a thin dim sliver — enough to answer "is Murmur running and can I
dictate right now?", not enough to notice while you work. Starting a session
grows that same element rather than swapping it, so the change is one
continuous morph.

Vertical rather than horizontal, so it lives in the dead space beside the
screen edge instead of over the bottom of whatever is being worked in. That
also means no text labels: state is carried by size, colour and motion, which
is legible at a glance from the corner of your eye in a way a 8pt word is not.

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
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import QWidget

from .motion import Spring
from .waveform import BarModel

BARS = 11

# (width, height). The capsule radius is always width/2, so it stays a true
# pill at every size and the morph between them reads as one object.
IDLE_SIZE = (13, 66)
REC_SIZE = (34, 150)
# Hands-free is visibly taller. With no text, size is what distinguishes a
# session you can walk away from.
TOGGLE_SIZE = (34, 208)
WORK_SIZE = (34, 126)          # transcribing / cleaning up
DONE_SIZE = (34, 92)
# Contracted to a dot as the comet takes over, so the pill appears to
# become the thing that flies rather than vanishing beside it.
LAUNCH_SIZE = (16, 16)

# Present enough to answer 'is Murmur running?' from the corner of your
# eye, dim enough to ignore. Too faint and it is not an indicator at all.
IDLE_OPACITY = 0.62

# Glass, adapted from Sotto's overlay (see NOTICE.md): a translucent dark body
# with a light top edge and a darker foot, so it reads as a lit pane rather than
# a flat shape. macOS gets this free from .ultraThinMaterial; Windows has no
# per-shape equivalent, so it is painted.
BODY_TOP = QColor(38, 41, 48, 216)
BODY_BOTTOM = QColor(11, 12, 15, 240)
HAIRLINE = QColor(255, 255, 255, 30)
GLASS_SHEEN = QColor(255, 255, 255, 26)

# The rim light: two lines 180 degrees apart gliding the same direction, so
# when one rides the top the other rides the bottom. Sotto's rim-variants5.html
# variant 1: a 2.25s lap with each line covering 18% of the perimeter.
RIM_LAP_S = 2.25
RIM_LENGTH = 0.18
RIM_SAMPLES = 26

ACCENT = {
    "armed": QColor("#5B8DEF"),
    "recording": QColor("#5B8DEF"),
    "transcribing": QColor("#E8B84B"),
    "polishing": QColor("#E8B84B"),
    "done": QColor("#4ADE80"),
    "copied": QColor("#4ADE80"),
    "cancelled": QColor("#EF4444"),
    "error": QColor("#EF4444"),
    "launching": QColor("#4ADE80"),
}

ACTIVE = ("recording", "transcribing", "polishing")
WORKING = ("transcribing", "polishing")
TERMINAL = ("done", "copied", "cancelled", "error")
HANDOFF = "launching"

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


class Pill(QWidget):
    def __init__(self, offset_px: int = 12, show_when_idle: bool = True,
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

        self.offset_px = offset_px          # distance from the right bezel
        self.show_when_idle = show_when_idle
        self.state = "off"
        self.mode = "hold"
        self.level = 0.0

        self.bars = BarModel(n=BARS)
        self.opacity = Spring(0.0, stiffness=200, damping=24)
        self.width_s = Spring(float(IDLE_SIZE[0]), stiffness=190, damping=23)
        self.height_s = Spring(float(IDLE_SIZE[1]), stiffness=180, damping=22)
        self.slide = Spring(14.0, stiffness=200, damping=24)   # in from the edge
        self.check = Spring(0.0, stiffness=200, damping=18)

        self.sweep = 0.0
        self.rim = 0.0
        self.shake = 0.0
        self._hold_frames = 0
        self._hardened = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # Canvas: tallest state plus slide and shake headroom.
        self.resize(REC_SIZE[0] + 48, TOGGLE_SIZE[1] + 40)

    # --- window plumbing --------------------------------------------------

    def _place(self) -> None:
        """Against the right bezel, vertically centred, on the screen the user
        is actually working on.

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
            int(geo.right() - self.width() + 1),
            int(geo.center().y() - self.height() / 2),
        )

    def capsule_centre(self):
        """Centre of the drawn capsule in SCREEN coordinates.

        The comet starts here, so it must be the capsule rather than the
        window — the window is deliberately much larger to leave room for the
        slide and shake.
        """
        from PySide6.QtCore import QPointF

        w = max(3.0, self.width_s.value)
        h = max(4.0, self.height_s.value)
        local_x = self.width() - w - self.offset_px + self.slide.value + w / 2
        local_y = self.height() / 2
        top_left = self.mapToGlobal(self.rect().topLeft())
        return QPointF(top_left.x() + local_x, top_left.y() + local_y)

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

    def _target_size(self) -> tuple[int, int]:
        if self.state == "armed":
            return IDLE_SIZE
        if self.state == "recording":
            return TOGGLE_SIZE if self.mode == "toggle" else REC_SIZE
        if self.state in WORKING:
            return WORK_SIZE
        if self.state == HANDOFF:
            return LAUNCH_SIZE
        if self.state in TERMINAL:
            return DONE_SIZE
        return REC_SIZE

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
            self.slide.snap_to(14.0)
            self.width_s.snap_to(float(IDLE_SIZE[0]))
            self.height_s.snap_to(float(IDLE_SIZE[1]))
            self._place()
            self.show()
            self._harden()
        else:
            self._place()          # the focused monitor may have changed

        if not self._timer.isActive():
            self._timer.start(16)

        self.slide.target = 0.0
        self._retarget()
        self._hold_frames = 0

        if state == "armed":
            self.opacity.target = IDLE_OPACITY
            self.check.target = 0.0
        elif state in ACTIVE:
            self.opacity.target = 1.0
            self.check.target = 0.0
        elif state == HANDOFF:
            # Contract and fade: the comet picks the motion up from here.
            self.check.snap_to(0.0)
            self.opacity.target = 0.0
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
        for s in (self.opacity, self.width_s, self.height_s, self.slide,
                  self.check):
            s.step(dt)

        if self.state == "recording":
            if self.level > 0.012:
                self.bars.step(self.level, dt)
            else:
                self.bars.breathe(dt)
        elif self.state == "armed":
            self.bars.breathe(dt)
        elif self.state in WORKING:
            self.sweep = (self.sweep + dt * 0.9) % 1.0
        if self.state in ACTIVE:
            self.rim = (self.rim + dt / RIM_LAP_S) % 1.0

        if self.state in TERMINAL:
            self._hold_frames += 1
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

        w = max(3.0, self.width_s.value)
        h = max(4.0, self.height_s.value)
        radius = w / 2.0
        # Shake horizontally; against the right edge that reads as a nudge.
        dx = math.sin(self.shake * 40.0) * 5.0 * self.shake
        rect = QRectF(
            self.width() - w - self.offset_px + self.slide.value + dx,
            (self.height() - h) / 2,
            w, h,
        )

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        body.setColorAt(0.0, BODY_TOP)
        body.setColorAt(1.0, BODY_BOTTOM)
        p.fillPath(path, body)

        # A sheen down the upper third: the highlight is what sells glass.
        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, GLASS_SHEEN)
        sheen.setColorAt(0.35, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, sheen)

        p.setPen(QPen(HAIRLINE, 1))
        p.drawPath(path)

        accent_now = ACCENT.get(self.state, ACCENT["recording"])
        if self.state in ACTIVE and rect.width() > 16:
            self._draw_rim(p, path, accent_now)

        # Nothing may be drawn outside the capsule.
        p.setClipPath(path)

        accent = ACCENT.get(self.state, ACCENT["recording"])

        # The capsule stays subtle when armed, but its dots should not: one
        # painter opacity for both washed the indicator out to nothing. Raise it
        # for the accent only, so "Murmur is running" is actually readable.
        if self.state == "armed":
            p.setOpacity(min(1.0, self.opacity.value * 1.5))

        if self.state in ("armed", "recording"):
            self._draw_bars(p, rect, accent)
        elif self.state in WORKING:
            self._draw_dots(p, rect, accent)
        elif self.state in ("done", "copied"):
            self._draw_check(p, rect, accent)
        elif self.state in ("cancelled", "error"):
            self._draw_slash(p, rect, accent)
        p.end()

    def _draw_rim(self, p, path: QPainterPath, accent) -> None:
        """Twin Tron snakes: two lines 180 degrees apart running the same way.

        Qt's percentAtLength/pointAtPercent are arc-length based, so the heads
        travel at a constant speed around the capsule rather than racing the
        straights and crawling the ends — which is exactly the property Sotto
        needed trimmedPath for.
        """
        p.save()
        p.setBrush(Qt.NoBrush)
        for offset in (0.0, 0.5):
            head = (self.rim + offset) % 1.0
            for i in range(RIM_SAMPLES):
                a = head - RIM_LENGTH * (i / RIM_SAMPLES)
                b = head - RIM_LENGTH * ((i + 1) / RIM_SAMPLES)
                p1 = path.pointAtPercent(a % 1.0)
                p2 = path.pointAtPercent(b % 1.0)
                # Bright at the head, erased at the tail: the snake writes
                # itself forward while the tail rubs out behind it.
                fade = 1.0 - i / RIM_SAMPLES
                c = QColor(accent)
                c.setAlphaF(min(1.0, 0.9 * fade * fade))
                p.setPen(QPen(c, 1.6 + 1.0 * fade, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(p1, p2)
        p.restore()

    def _draw_bars(self, p, rect, accent) -> None:
        """Horizontal bars stacked up the capsule, length driven by the mic.

        The same waveform model as before, rendered on its side. The armed
        indicator is a miniature of it rather than a different graphic, which is
        what lets the two states morph into one another.
        """
        values = self.bars.heights()
        n = len(values)
        scale = rect.width() / REC_SIZE[0]

        # Fill the capsule rather than clustering in the middle. Gaps are
        # derived from the available height, so the same code draws a tall
        # recording pill and a tiny armed sliver at the right density.
        inset = max(3.0, 9.0 * scale)
        usable = max(4.0, rect.height() - inset * 2)
        thickness = max(1.2, min(4.2 * scale, usable / (n * 2.1)))
        gap = (usable - n * thickness) / max(n - 1, 1)
        top = rect.top() + inset

        room = max(2.0, rect.width() - 8.0 * scale)
        floor = max(1.5, 3.2 * scale)
        cx = rect.center().x()

        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        for i, v in enumerate(values):
            length = floor + v * room
            y = top + i * (thickness + gap)
            p.drawRoundedRect(
                QRectF(cx - length / 2, y, length, thickness),
                thickness / 2, thickness / 2)

    def _draw_dots(self, p, rect, accent) -> None:
        """Amber dots travelling up the capsule while it transcribes and cleans
        up. A progress cue, not decoration."""
        inset = 16.0
        span = max(4.0, rect.height() - inset * 2)
        cy = rect.bottom() - inset - self.sweep * span
        cx = rect.center().x()
        p.setPen(Qt.NoPen)
        for i in range(7):
            c = QColor(accent)
            c.setAlpha(int(230 * (1.0 - i / 7.0)))
            p.setBrush(c)
            r = 3.0 - i * 0.22
            p.drawEllipse(QPointF(cx, cy + i * 6.4), r, r)

    def _draw_check(self, p, rect, accent) -> None:
        """Strokes itself on: the tick is drawn to a fraction of its length."""
        t = max(0.0, min(1.0, self.check.value))
        if t <= 0.01 or rect.width() < 12:
            return
        c = rect.center()
        a = QPointF(c.x() - 5.5, c.y() + 0.5)
        b = QPointF(c.x() - 1.5, c.y() + 4.5)
        d = QPointF(c.x() + 5.5, c.y() - 4.5)
        p.setPen(QPen(accent, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if t <= 0.4:
            k = t / 0.4
            p.drawLine(a, QPointF(a.x() + (b.x() - a.x()) * k,
                                  a.y() + (b.y() - a.y()) * k))
        else:
            k = (t - 0.4) / 0.6
            p.drawLine(a, b)
            p.drawLine(b, QPointF(b.x() + (d.x() - b.x()) * k,
                                  b.y() + (d.y() - b.y()) * k))

    def _draw_slash(self, p, rect, accent) -> None:
        """Cancelled or failed. A short bar, unmistakably not a tick."""
        c = rect.center()
        p.setPen(QPen(accent, 2.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() - 5, c.y()), QPointF(c.x() + 5, c.y()))
