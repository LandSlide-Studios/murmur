"""Audio-reactive bar model.

Each bar is its own spring with slightly different constants, so they overshoot
and settle at different rates and read as a wave rather than a bar chart. The
model is deliberately separate from the painting so it can be tested without Qt.
"""

import math

from .motion import Spring

FLOOR = 0.08          # idle height, so the pill never looks dead
_BREATHE_HZ = 0.4

# The level a typical dictating voice peaks at. `audio.speech_rms_threshold` is
# 0.012, so the band that matters — quiet talking up to loud talking — is
# roughly 0.012..0.09, and this sits inside it rather than somewhere out past
# shouting, which is where a linear gain of 9.0 effectively put it.
_REFERENCE = 0.06

# ...but a fixed reference is a guess about someone else's voice and someone
# else's microphone. Tommy is on a webcam at arm's length, not a headset. So the
# meter also tracks the loudest thing it has heard recently and fits itself to
# that, the way any level meter with auto-gain does.
#
# The two are blended in log space — the geometric mean, so the meter moves
# HALF way toward whatever voice is in front of it. Full adaptation would make
# a murmur and a shout look identical; none of it leaves a soft speaker stuck at
# the bottom. Half keeps the dynamics and still fits the person.
_PEAK_DECAY_S = 4.0            # how fast it forgets a loud moment
_PEAK_MIN = 0.020              # never adapt below this, or silence gets amplified
_PEAK_MAX = 0.200

# Loudness is logarithmic, so a linear map spends most of its range on volumes
# nobody dictates at. Measured with the old linear gain, the tallest bar reached
# 10.8% at the speech threshold and 28.3% at a normal speaking voice: the useful
# band was squashed into the bottom third and quiet talking looked like silence.
# The square root pulls it up without costing headroom at the top.
_CURVE = 0.5

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
    (2.3, 1.6, 0.24),
    (1.4, 0.7, 0.14),
)

# The wave modulates AROUND this, and the shares above are deliberately small
# enough that bias + shares lands at ~1.0. The oscillator used to average 0.55
# and multiply the level, so it could only ever scale bars down — even a
# clipping input averaged 40% height and the meter could not look full by
# construction.
#
# Most of the height the meter gained came from _CURVE above, not from here.
# Raising this too far is a trap: at 0.72 the row was tall and had stopped
# reading as a voice at all, because the troughs went with it.
_OSC_BIAS = 0.64


class BarModel:
    def __init__(self, n: int = 5, reference: float = _REFERENCE):
        self.n = n
        self.reference = reference
        self._peak = _PEAK_MIN
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

        # Rise instantly to a new peak, fall back slowly. Instant on the way up
        # means a sudden loud syllable never overshoots the top of the meter.
        decayed = self._peak - (self._peak - _PEAK_MIN) * min(1.0, dt / _PEAK_DECAY_S)
        self._peak = min(_PEAK_MAX, max(_PEAK_MIN, level, decayed))

        full = math.sqrt(self.reference * self._peak)
        norm = max(0.0, min(1.0, level / full)) ** _CURVE
        mid = (self.n - 1) / 2 if self.n > 1 else 1.0
        span = self.n or 1
        for i, s in enumerate(self.springs):
            # Centre bars run taller, as on a real level meter. Eased back from
            # 0.34: the taper is a shape, and with fifteen packed bars instead of
            # nine spread ones it was visibly costing the row height at the edges.
            # Not eased further — past this it stops looking like a level meter.
            centre = 1.0 - 0.30 * (abs(i - mid) / mid if mid else 0.0)
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
