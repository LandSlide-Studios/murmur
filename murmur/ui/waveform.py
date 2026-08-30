"""Audio-reactive bar model.

Each bar is its own spring with slightly different constants, so they overshoot
and settle at different rates and read as a wave rather than a bar chart. The
model is deliberately separate from the painting so it can be tested without Qt.
"""

import math

from .motion import Spring

FLOOR = 0.08          # idle height, so the pill never looks dead
_BREATHE_HZ = 0.4

# The level a dictating voice peaks at ON HIS MICROPHONE. This was 0.06, chosen
# for a close mic, and it is the third constant in this app to have been set
# above his actual speaking voice — the other two being
# `audio.speech_rms_threshold` and `_PEAK_MIN` below. At 0.06 the meter reached
# 35% for him no matter what else was tuned.
#
# Measured from his own dictations once level logging was added: the loudest
# 400ms window of a real one is 0.0195-0.0268. (An earlier figure of 0.008 was
# derived rather than measured, and was the average across a whole clip — the
# meter sees per-block peaks two to three times that. Corrected here so nobody
# recalibrates against the wrong number.)
#
# Not pushed all the way down to his 0.008 either: that put his normal voice at
# 94% and left the meter with nothing to say about loudness, since everything
# from the speech threshold upward pinned at the top. 0.020 lands him near 80%
# with room above.
_REFERENCE = 0.020

# ...but a fixed reference is a guess about someone else's voice and someone
# else's microphone. Tommy is on a webcam at arm's length, not a headset. So the
# meter also tracks the loudest thing it has heard recently and fits itself to
# that, the way any level meter with auto-gain does.
#
# The two are blended in log space — the geometric mean, so the meter moves
# HALF way toward whatever voice is in front of it. Full adaptation would make
# a murmur and a shout look identical; none of it leaves a soft speaker stuck at
# the bottom. Half keeps the dynamics and still fits the person.
# What the row shows while it is listening and hearing nothing: flat, uniform,
# about half the length of a bar carrying a normal speaking voice. Tommy asked
# for this over the breathing floor it used to show — flat versus wave is a
# binary anyone can read at a glance, where a slow pulse at 8% just looked like
# a quieter version of the same thing.
FLAT = 0.30

_PEAK_DECAY_S = 4.0            # how fast it forgets a loud moment
# Never adapt below this. It belongs just at the speech threshold, NOT above a
# speaking voice: at 0.020 it sat above his, so the adaptation could never track
# down to him and the fixed reference won every time.
_PEAK_MIN = 0.004
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

    def _decay_peak(self, dt: float) -> None:
        """Let the adaptive gain forget, whatever the row is doing.

        The decay lived only in `step()`, which runs while the user is TALKING
        -- so the gain only forgot during speech, which is the opposite of the
        model. Silence is exactly what the four-second constant exists to
        consume: a cough pinned the peak, and the first word after a pause then
        read about 56% of its correct height.
        """
        if dt > 0:
            self._peak -= (self._peak - _PEAK_MIN) * min(1.0, dt / _PEAK_DECAY_S)
            self._peak = max(_PEAK_MIN, self._peak)

    def flat(self, dt: float) -> None:
        self._decay_peak(dt)
        self._flat(dt)

    def breathe(self, dt: float) -> None:
        self._decay_peak(dt)
        self._breathe(dt)

    def _breathe(self, dt: float) -> None:
        """Silence: a slow pulse instead of a dead flat row."""
        if dt <= 0:
            return
        self.t += dt
        pulse = FLOOR + 0.04 * (1.0 + math.sin(self.t * _BREATHE_HZ * 2 * math.pi))
        for s in self.springs:
            s.target = pulse
            s.step(dt)
            s.value = max(0.0, min(1.0, s.value))

    def _flat(self, dt: float) -> None:
        """Listening, hearing nothing. Deliberately motionless: the contrast
        with the travelling wave is the whole signal."""
        if dt <= 0:
            return
        for s in self.springs:
            s.target = FLAT
            s.step(dt)
            s.value = max(0.0, min(1.0, s.value))
