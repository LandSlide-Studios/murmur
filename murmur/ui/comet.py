"""The comet: your words flying to where they are about to land.

Adapted from Sotto (https://github.com/kingbootoshi/sotto), MIT licensed,
Copyright (c) 2026 Sotto contributors. Their Swift/CoreAnimation implementation
does not port to Windows, but the motion design does, and it is theirs — the
timings and the shape of the movement below are taken from `CometFlight.swift`:

    110ms pull-back, 260ms streak, ballistic to where the mouse was the instant
    you stopped talking. It never chases the cursor.

Why it works: the pull-back loads the throw, and the mid-flight stretch is what
makes a moving dot read as a comet rather than a sliding circle. Both are the
whole trick, so both are preserved.

The window is click-through and never activates — the same constraint as the
pill. If it took focus, the paste it is announcing would land in it.
"""

import ctypes
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget

# Sotto's measured values. Changing them changes the feel, so they are named.
PULL_MS = 110
FLIGHT_MS = 260
BURST_MS = 450
PULL_DIST = 14.0        # px the orb draws back before launching
PULL_SCALE = 0.12       # how much it swells while loading
FLIGHT_SHRINK = 0.55    # shrinks to 45% by arrival
FLIGHT_STRETCH = 2.6    # elongates up to 3.6x at mid-flight
ORB_RADIUS = 11.0
FRAME_MS = 8            # 120fps, as theirs is

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020


def _ease_out_cubic(t: float) -> float:
    return 1.0 - pow(1.0 - t, 3)


class Comet(QWidget):
    """One-shot flight from the pill to a captured screen point."""

    def __init__(self, colour: str = "#E8B84B", parent=None):
        super().__init__(
            parent,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

        self.colour = QColor(colour)
        self._start = QPointF()
        self._aim = QPointF()
        self._origin = QPointF()      # window top-left in screen coords
        self._elapsed = 0.0
        self._phase = "idle"
        self._on_land = None
        self._hardened = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # --- window plumbing --------------------------------------------------

    def _harden(self) -> None:
        if self._hardened:
            return
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT)
            self._hardened = True
        except Exception:
            pass

    @staticmethod
    def cursor_point() -> QPointF:
        """Where the mouse is RIGHT NOW, in screen coordinates.

        Captured once, at the moment the user stops talking. The flight is
        ballistic: aiming at a live cursor would make it chase, which reads as
        the animation following the user rather than delivering something.
        """
        try:
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return QPointF(float(pt.x), float(pt.y))
        except Exception:
            from PySide6.QtGui import QCursor

            p = QCursor.pos()
            return QPointF(float(p.x()), float(p.y()))

    # --- flight -----------------------------------------------------------

    def launch(self, start: QPointF, aim: QPointF, colour: str | None = None,
               on_land=None) -> None:
        if colour:
            self.colour = QColor(colour)
        self._start = QPointF(start)
        self._aim = QPointF(aim)
        self._on_land = on_land

        # A window just big enough for the whole flight, plus room for the
        # burst and the mid-flight stretch.
        pad = 120.0
        left = min(start.x(), aim.x()) - pad
        top = min(start.y(), aim.y()) - pad
        right = max(start.x(), aim.x()) + pad
        bottom = max(start.y(), aim.y()) + pad
        self._origin = QPointF(left, top)
        self.setGeometry(int(left), int(top), int(right - left), int(bottom - top))

        self._elapsed = 0.0
        self._phase = "pull"
        self.show()
        self._harden()
        self._timer.start(FRAME_MS)

    def _tick(self) -> None:
        self._elapsed += FRAME_MS
        if self._phase == "pull" and self._elapsed >= PULL_MS:
            self._phase = "flight"
            self._elapsed = 0.0
        elif self._phase == "flight" and self._elapsed >= FLIGHT_MS:
            self._phase = "burst"
            self._elapsed = 0.0
            if self._on_land is not None:
                cb, self._on_land = self._on_land, None
                try:
                    cb()
                except Exception:
                    pass
        elif self._phase == "burst" and self._elapsed >= BURST_MS:
            self._phase = "idle"
            self._timer.stop()
            self.hide()
            return
        self.update()

    @property
    def flying(self) -> bool:
        return self._phase != "idle"

    # --- painting ---------------------------------------------------------

    def paintEvent(self, _event) -> None:
        if self._phase == "idle":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        start = self._start - self._origin
        aim = self._aim - self._origin
        angle = math.atan2(aim.y() - start.y(), aim.x() - start.x())

        if self._phase == "pull":
            # Load the throw: draw back along the reverse of the aim, and swell.
            prog = min(self._elapsed / PULL_MS, 1.0)
            d = PULL_DIST * min(prog / 0.8, 1.0)
            pos = QPointF(start.x() - math.cos(angle) * d,
                          start.y() - math.sin(angle) * d)
            self._draw_orb(p, pos, angle, 1.0 + PULL_SCALE * prog, 1.0, 0.95)
        elif self._phase == "flight":
            raw = min(self._elapsed / FLIGHT_MS, 1.0)
            t = _ease_out_cubic(raw)
            # Triangle peaking mid-flight: the stretch is what makes it a comet
            # rather than a circle sliding across the screen.
            mid = 1.0 - abs(raw - 0.5) * 2.0
            pos = QPointF(start.x() + (aim.x() - start.x()) * t,
                          start.y() + (aim.y() - start.y()) * t)
            self._draw_orb(p, pos, angle, 1.0 - FLIGHT_SHRINK * t,
                           1.0 + FLIGHT_STRETCH * mid, 0.95)
        else:  # burst
            prog = min(self._elapsed / BURST_MS, 1.0)
            eased = 1.0 - pow(1.0 - prog, 2)
            self._draw_burst(p, aim, 0.4 + 4.6 * eased, 0.95 * (1.0 - eased))
        p.end()

    def _draw_orb(self, p, pos: QPointF, angle: float, scale: float,
                  stretch: float, alpha: float) -> None:
        p.save()
        p.translate(pos)
        p.rotate(math.degrees(angle))
        p.scale(stretch, 1.0)        # elongate along the direction of travel

        r = ORB_RADIUS * scale
        # Two soft halos then the core. Sotto glows deliberately; the design
        # library's no-glow rule is about marketing surfaces, and here the glow
        # IS the object.
        for mult, a in ((2.6, 0.16), (1.7, 0.30)):
            c = QColor(self.colour)
            c.setAlphaF(a * alpha)
            p.setBrush(c)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(0, 0), r * mult, r * mult)

        grad = QRadialGradient(QPointF(-r * 0.24, -r * 0.32), r * 1.6)
        hot = QColor(self.colour).lighter(135)
        hot.setAlphaF(alpha)
        cool = QColor(self.colour).darker(160)
        cool.setAlphaF(alpha * 0.9)
        grad.setColorAt(0.0, hot)
        grad.setColorAt(1.0, cool)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(0, 0), r, r)
        p.restore()

    def _draw_burst(self, p, pos: QPointF, scale: float, alpha: float) -> None:
        if alpha <= 0.01:
            return
        r = ORB_RADIUS * scale
        c = QColor(self.colour)
        c.setAlphaF(max(0.0, alpha) * 0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(Qt.NoBrush)
        from PySide6.QtGui import QPen

        pen = QPen(c, max(1.0, 3.0 * (1.0 - scale / 5.0)))
        p.setPen(pen)
        p.drawEllipse(QRectF(pos.x() - r, pos.y() - r, r * 2, r * 2))
