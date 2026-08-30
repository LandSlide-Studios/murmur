"""Audio-reactive bar model.

Each bar is its own spring with slightly different constants, so they overshoot
and settle at different rates and read as a wave rather than a bar chart. The
model is deliberately separate from the painting so it can be tested without Qt.
"""

import math

from .motion import Spring

FLOOR = 0.08          # idle height, so the pill never looks dead
_GAIN = 9.0           # mic RMS is small; scale it into 0..1
_BREATHE_HZ = 0.4

# Two travelling waves, not one oscillator per bar.
#
# The bars used to run on mutually non-harmonic per-bar frequencies, which read
# as alive when there were nine of them spaced well apart. Packed to fifteen at
# a 1.4px gap the row is visually continuous, and that same independence read as
# a barcode: measured, every neighbouring pair disagreed in direction, twelve
# sign changes out of a possible twelve.
#
# The fix is not a shallower swing — that gives a shimmer. It is correlating the
# neighbours, which is what a waveform actually is. Each wave travels ALONG the
# row (hence the `- i * k` term); their spatial periods don't divide evenly, so
# the crests drift in and out of alignment and the shape never repeats.
_WAVES = (
    # speed (Hz), cycles across the row, share of the swing
    (2.3, 1.6, 0.30),
    (1.4, 0.7, 0.19),
)
_OSC_BIAS = 0.55


class BarModel:
    def __init__(self, n: int = 5, gain: float = _GAIN):
        self.n = n
        self.gain = gain
        self.t = 0.0
        # Staggered constants: identical springs settle identically and look
        # mechanical. Spreading stiffness and damping makes the row feel alive.
        # Staggered, but far less than when each bar was its own oscillator:
        # the waves now carry the liveliness, and a wide stagger would just
        # decorrelate the neighbours the waves exist to correlate.
        self.springs = [
            Spring(value=FLOOR, stiffness=210.0 + i * 8.0, damping=14.0 + i * 0.4)
            for i in range(n)
        ]

    def heights(self) -> list[float]:
        return [s.value for s in self.springs]

    def step(self, level: float, dt: float) -> None:
        if dt <= 0:
            return
        self.t += dt
        norm = max(0.0, min(1.0, level * self.gain))
        mid = (self.n - 1) / 2 if self.n > 1 else 1.0
        span = self.n or 1
        for i, s in enumerate(self.springs):
            # Centre bars run taller, as on a real level meter.
            centre = 1.0 - 0.34 * (abs(i - mid) / mid if mid else 0.0)
            osc = _OSC_BIAS
            for speed, cycles, share in _WAVES:
                k = 2 * math.pi * cycles / span
                osc += share * math.sin(self.t * speed * 2 * math.pi - i * k)
            s.target = max(FLOOR, min(1.0, norm * centre * osc))
            s.step(dt)
            s.value = max(0.0, min(1.0, s.value))

    def breathe(self, dt: float) -> None:
        """Silence: a slow pulse instead of a dead flat row."""
        if dt <= 0:
            return
        self.t += dt
        pulse = FLOOR + 0.04 * (1.0 + math.sin(self.t * _BREATHE_HZ * 2 * math.pi))
        for s in self.springs:
            s.target = pulse
            s.step(dt)
            s.value = max(0.0, min(1.0, s.value))
