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

# Each bar gets its own oscillator frequency and phase. Sharing one frequency
# (with only a phase offset) makes the row move in lockstep and read as a
# barcode; mutually non-harmonic rates keep the bars visibly independent at
# every instant, which is what makes it look like a voice.
_FREQS = (7.3, 11.9, 9.1, 13.7, 8.3, 12.1, 10.7, 9.7, 12.9)
_PHASES = (0.0, 2.1, 4.3, 1.2, 5.4, 3.3, 0.7, 2.8, 4.9)


class BarModel:
    def __init__(self, n: int = 5, gain: float = _GAIN):
        self.n = n
        self.gain = gain
        self.t = 0.0
        # Staggered constants: identical springs settle identically and look
        # mechanical. Spreading stiffness and damping makes the row feel alive.
        self.springs = [
            Spring(value=FLOOR, stiffness=210.0 + i * 26.0, damping=14.0 + i * 1.3)
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
        for i, s in enumerate(self.springs):
            freq = _FREQS[i % len(_FREQS)]
            phase = _PHASES[i % len(_PHASES)]
            # Centre bars run taller, as on a real level meter.
            centre = 1.0 - 0.34 * (abs(i - mid) / mid if mid else 0.0)
            # Deep modulation: neighbouring bars differ by roughly 4x at any
            # given frame. A shallower swing reads as a shimmer rather than as
            # something responding to a voice.
            osc = 0.58 + 0.62 * math.sin(self.t * freq + phase)
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
