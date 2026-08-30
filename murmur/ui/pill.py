"""The floating status pill.

A narrow vertical capsule against the right bezel. Idle, it is a thin dim sliver
— enough to answer "is Murmur running and can I dictate right now?", not enough
to notice. Starting a session grows that same element rather than swapping it,
so the change is one continuous morph.

While recording it carries two controls: a tick at the top (stop and paste) and
a cross at the bottom (cancel). The window becomes clickable ONLY then, and even
then it never activates — an activated overlay would eat the very paste the tick
just asked for.

Two hard constraints, both from CLAUDE.md:

  * It must NEVER take focus. WS_EX_NOACTIVATE lets a window receive clicks
    without activating, which is what makes the controls possible at all.
  * No gradient, no glow on the capsule itself. Motion and one accent carry it;
    the waveform is the visual interest.

Everything animates off ONE 60fps timer through ONE spring integrator, so a
state change that interrupts another blends instead of snapping.
"""

import ctypes
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import QWidget

from .motion import Spring
from .waveform import BarModel

# Packed tight: the bars nearly touch, so the row reads as one waveform rather
# than a row of separate ticks.
BARS = 15
BAR_GAP = 1.4

# (width, height). The capsule radius is always width/2, so it stays a true pill
# at every size and the morph between them reads as one object.
IDLE_SIZE = (11, 52)
REC_SIZE = (30, 138)
TOGGLE_SIZE = (30, 172)
WORK_SIZE = (26, 26)           # contracted to a charging orb
DONE_SIZE = (30, 76)

BUTTON_H = 22                  # the tick and cross caps at each end
BUTTON_MIN_H = 96              # below this the capsule is too short for controls

IDLE_OPACITY = 0.62

BODY_TOP = QColor(38, 41, 48, 216)
BODY_BOTTOM = QColor(11, 12, 15, 240)
HAIRLINE = QColor(255, 255, 255, 30)
GLASS_SHEEN = QColor(255, 255, 255, 26)
CONTROL = QColor(255, 255, 255, 150)
CONTROL_HOT = QColor(255, 255, 255, 245)

RIM_LAP_S = 2.25
RIM_LENGTH = 0.18
RIM_SAMPLES = 26
ORB_PULSE_S = 0.48

ACCENT = {
    "armed": QColor("#5B8DEF"),
    "recording": QColor("#5B8DEF"),
    "transcribing": QColor("#E8B84B"),
    "polishing": QColor("#E8B84B"),
    "done": QColor("#4ADE80"),
    "copied": QColor("#4ADE80"),
    "launching": QColor("#4ADE80"),
    "cancelled": QColor("#EF4444"),
    "error": QColor("#EF4444"),
}

ACTIVE = ("recording", "transcribing", "polishing")
WORKING = ("transcribing", "polishing")
TERMINAL = ("done", "copied", "cancelled", "error")
HANDOFF = "launching"

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020


class Pill(QWidget):
    accepted = Signal()        # tick: stop and paste
    cancelled_by_user = Signal()   # cross: throw it away

    def __init__(self, offset_px: int = 12, show_when_idle: bool = True,
                 parent=None):
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)

        self.offset_px = offset_px
        self.show_when_idle = show_when_idle
        self.state = "off"
        self.mode = "hold"
        self.level = 0.0

        self.bars = BarModel(n=BARS)
        self.opacity = Spring(0.0, stiffness=200, damping=24)
        self.width_s = Spring(float(IDLE_SIZE[0]), stiffness=190, damping=23)
        self.height_s = Spring(float(IDLE_SIZE[1]), stiffness=180, damping=22)
        self.slide = Spring(14.0, stiffness=200, damping=24)
        self.check = Spring(0.0, stiffness=200, damping=18)

        self.sweep = 0.0
        self.rim = 0.0
        self.orb = 0.0
        self.shake = 0.0
        self._hold_frames = 0
        self._hardened = False
        self._hover = None          # "accept" | "cancel" | None
        # Starts click-through and stays that way until the controls appear.
        # The attribute has to be set here as well as tracked, or the first
        # _set_click_through(True) is a no-op against an unset attribute.
        self._click_through = True
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.resize(REC_SIZE[0] + 44, TOGGLE_SIZE[1] + 36)

    # --- window plumbing --------------------------------------------------

    def _place(self) -> None:
        """Against the right bezel, vertically centred, on the screen the user
        is actually working on. A widget that has never moved reports the
        PRIMARY screen, so follow the cursor instead."""
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
        self.move(int(geo.right() - self.width() + 1),
                  int(geo.center().y() - self.height() / 2))

    def _ex_style(self, add: int = 0, remove: int = 0) -> None:
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | add) & ~remove)
        except Exception:
            pass

    def _harden(self) -> None:
        """WS_EX_NOACTIVATE is what lets the controls exist: the window can be
        clicked without ever being activated, so the paste still lands in the
        user's app."""
        if self._hardened:
            return
        self._ex_style(add=WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self._hardened = True

    def _set_click_through(self, through: bool) -> None:
        """Clicks pass straight through unless the controls are on screen.

        An always-clickable overlay would swallow clicks on whatever sits
        behind it, and it sits over the edge of a real window.
        """
        if through == self._click_through:
            return
        self._click_through = through
        self.setAttribute(Qt.WA_TransparentForMouseEvents, through)
        if through:
            self._ex_style(add=WS_EX_TRANSPARENT)
            self._hover = None
        else:
            self._ex_style(remove=WS_EX_TRANSPARENT)

    # --- geometry ---------------------------------------------------------

    def _capsule_rect(self) -> QRectF:
        w = max(3.0, self.width_s.value)
        h = max(4.0, self.height_s.value)
        dx = math.sin(self.shake * 40.0) * 5.0 * self.shake
        return QRectF(self.width() - w - self.offset_px + self.slide.value + dx,
                      (self.height() - h) / 2, w, h)

    def _controls_visible(self) -> bool:
        return (self.state in ACTIVE
                and self.height_s.value >= BUTTON_MIN_H
                and self.width_s.value >= 20)

    def _button_rects(self):
        """(accept, cancel) in widget coordinates, or (None, None)."""
        if not self._controls_visible():
            return None, None
        r = self._capsule_rect()
        return (QRectF(r.left(), r.top(), r.width(), BUTTON_H),
                QRectF(r.left(), r.bottom() - BUTTON_H, r.width(), BUTTON_H))

    def capsule_centre(self):
        r = self._capsule_rect()
        tl = self.mapToGlobal(self.rect().topLeft())
        return QPointF(tl.x() + r.center().x(), tl.y() + r.center().y())

    # --- mouse ------------------------------------------------------------

    def mouseMoveEvent(self, event):
        accept, cancel = self._button_rects()
        pos = event.position()
        hover = None
        if accept and accept.contains(pos):
            hover = "accept"
        elif cancel and cancel.contains(pos):
            hover = "cancel"
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, _event):
        if self._hover is not None:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, event):
        accept, cancel = self._button_rects()
        pos = event.position()
        if accept and accept.contains(pos):
            self.accepted.emit()
        elif cancel and cancel.contains(pos):
            self.cancelled_by_user.emit()

    # --- state ------------------------------------------------------------

    def set_level(self, level: float) -> None:
        self.level = level

    @Slot(str, str)
    def apply_state(self, state: str, mode: str) -> None:
        self.set_state(state, mode or None)

    def _target_size(self) -> tuple[int, int]:
        if self.state == "armed":
            return IDLE_SIZE
        if self.state == HANDOFF:
            return (16, 16)
        if self.state == "recording":
            return TOGGLE_SIZE if self.mode == "toggle" else REC_SIZE
        if self.state in WORKING:
            return WORK_SIZE
        if self.state in TERMINAL:
            return DONE_SIZE
        return REC_SIZE

    def _retarget(self) -> None:
        w, h = self._target_size()
        self.width_s.target = float(w)
        self.height_s.target = float(h)

    def set_state(self, state: str, mode: str | None = None) -> None:
        if state == "idle":
            state = "armed" if self.show_when_idle else "off"
        if mode:
            self.mode = mode
        if state == self.state:
            self._retarget()
            return
        self.state = state

        # Only clickable while recording, when the controls are on screen.
        self._set_click_through(state not in ACTIVE)

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
            self._place()

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
            self.check.snap_to(0.0)
            self.opacity.target = 0.0
        elif state in ("done", "copied"):
            self.check.snap_to(0.0)
            self.check.target = 1.0
            self.opacity.target = 1.0
        else:
            self.shake = 1.0
            self.opacity.target = 1.0

    # --- frame ------------------------------------------------------------

    def _settle_to_armed(self) -> None:
        self.state = "armed" if self.show_when_idle else "off"
        self.check.target = 0.0
        self._hold_frames = 0
        self._set_click_through(True)
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
            self.orb = (self.orb + dt / ORB_PULSE_S) % 1.0
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

        rect = self._capsule_rect()
        radius = rect.width() / 2.0

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        body.setColorAt(0.0, BODY_TOP)
        body.setColorAt(1.0, BODY_BOTTOM)
        p.fillPath(path, body)

        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, GLASS_SHEEN)
        sheen.setColorAt(0.35, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, sheen)

        p.setPen(QPen(HAIRLINE, 1))
        p.drawPath(path)

        accent = ACCENT.get(self.state, ACCENT["recording"])
        if self.state in ACTIVE and rect.width() > 16:
            self._draw_rim(p, path, accent)

        p.setClipPath(path)

        if self.state == "armed":
            self._draw_bars(p, rect, accent, rect)
        elif self.state == "recording":
            self._draw_controls(p, rect, accent)
        elif self.state in WORKING:
            self._draw_orb(p, rect, accent)
        elif self.state in ("done", "copied"):
            self._draw_check(p, rect, accent)
        elif self.state in ("cancelled", "error"):
            self._draw_slash(p, rect, accent)
        p.end()

    def _draw_controls(self, p, rect, accent) -> None:
        """Waveform between a tick and a cross.

        The controls only appear once the capsule is tall enough to hold them,
        so the morph out of the armed sliver does not flash cramped glyphs on
        its way up.
        """
        accept, cancel = self._button_rects()
        if accept is None:
            self._draw_bars(p, rect, accent, rect)
            return

        wave = QRectF(rect.left(), accept.bottom(), rect.width(),
                      cancel.top() - accept.bottom())
        self._draw_bars(p, rect, accent, wave)

        for which, r in (("accept", accept), ("cancel", cancel)):
            hot = self._hover == which
            colour = CONTROL_HOT if hot else CONTROL
            if hot:
                bg = QColor(accent)
                bg.setAlphaF(0.22)
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 8, 8)
            c = r.center()
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(colour, 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if which == "accept":
                p.drawPolyline([QPointF(c.x() - 4.5, c.y()),
                                QPointF(c.x() - 1.2, c.y() + 3.4),
                                QPointF(c.x() + 4.5, c.y() - 3.4)])
            else:
                p.drawLine(QPointF(c.x() - 3.6, c.y() - 3.6),
                           QPointF(c.x() + 3.6, c.y() + 3.6))
                p.drawLine(QPointF(c.x() + 3.6, c.y() - 3.6),
                           QPointF(c.x() - 3.6, c.y() + 3.6))

    def _draw_bars(self, p, rect, accent, area: QRectF) -> None:
        """Horizontal bars packed tight, length driven by the microphone.

        A small gap and many bars is what makes this read as one waveform
        rather than a row of separate ticks.
        """
        values = self.bars.heights()
        n = len(values)
        scale = rect.width() / REC_SIZE[0]
        inset = max(2.0, 4.0 * scale)
        usable = max(4.0, area.height() - inset * 2)
        gap = BAR_GAP * scale
        thickness = max(1.0, (usable - gap * (n - 1)) / n)
        top = area.top() + inset

        room = max(2.0, rect.width() - 8.0 * scale)
        floor = max(1.5, 3.0 * scale)
        cx = rect.center().x()

        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        for i, v in enumerate(values):
            length = floor + v * room
            y = top + i * (thickness + gap)
            p.drawRoundedRect(QRectF(cx - length / 2, y, length, thickness),
                              thickness / 2, thickness / 2)

    def _draw_rim(self, p, path: QPainterPath, accent) -> None:
        """Twin Tron snakes: two lines 180 degrees apart running the same way.
        Qt's pointAtPercent is arc-length based, so the heads travel at constant
        speed rather than racing the straights."""
        p.save()
        p.setBrush(Qt.NoBrush)
        for offset in (0.0, 0.5):
            head = (self.rim + offset) % 1.0
            for i in range(RIM_SAMPLES):
                a = head - RIM_LENGTH * (i / RIM_SAMPLES)
                b = head - RIM_LENGTH * ((i + 1) / RIM_SAMPLES)
                fade = 1.0 - i / RIM_SAMPLES
                c = QColor(accent)
                c.setAlphaF(min(1.0, 0.9 * fade * fade))
                p.setPen(QPen(c, 1.6 + 1.0 * fade, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(path.pointAtPercent(a % 1.0),
                           path.pointAtPercent(b % 1.0))
        p.restore()

    def _draw_orb(self, p, rect, accent) -> None:
        """The charge: a glowing orb that breathes while the model works. Not a
        smaller waveform — the shape has to be one you cannot mistake for
        'still listening'."""
        phase = abs(self.orb * 2.0 - 1.0)
        breathe = 0.72 + 0.28 * phase
        r = min(rect.width(), rect.height()) * 0.30 * breathe
        c = rect.center()

        p.setPen(Qt.NoPen)
        for mult, alpha in ((2.5, 0.14), (1.7, 0.26)):
            halo = QColor(accent)
            halo.setAlphaF(alpha * breathe)
            p.setBrush(halo)
            p.drawEllipse(c, r * mult, r * mult)

        grad = QRadialGradient(QPointF(c.x() - r * 0.25, c.y() - r * 0.32), r * 1.7)
        grad.setColorAt(0.0, QColor(accent).lighter(140))
        grad.setColorAt(1.0, QColor(accent).darker(150))
        p.setBrush(grad)
        p.drawEllipse(c, r, r)

    def _draw_check(self, p, rect, accent) -> None:
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
        c = rect.center()
        p.setPen(QPen(accent, 2.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() - 5, c.y()), QPointF(c.x() + 5, c.y()))
