"""Generate the audio cues.

The synthesis model and the "sotto" pack recipe are adapted from Sotto
(https://github.com/kingbootoshi/sotto), MIT licensed — see NOTICE.md. Their
`SoundPlayer.swift` is the source for the voice model, the envelope, the sweep
law and the master ceiling.

The idea worth stealing, in their words: **felt, not heard**. Every voice runs
through its own low-pass and then a master 2 kHz ceiling, so energy lives at
40–600 Hz. Nothing pierces. Cues anchor to F and C so repeated use never sounds
out of tune with itself.

Reimplemented in numpy rather than copied — AVAudioEngine has no Windows
counterpart — but the numbers are theirs and are kept exactly, because they are
the part that was tuned by ear.
"""

import math
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 44_100
CEILING_HZ = 2_000.0        # the master "nothing pierces" ceiling
OUT = Path(__file__).resolve().parent.parent / "assets"


@dataclass
class Voice:
    shape: str = "sine"          # sine | triangle | sawtooth | noise
    f0: float = 0.0
    f1: float = 0.0              # 0 = no sweep. For noise this sweeps the low-pass.
    dur: float = 0.1
    gain: float = 0.3
    delay: float = 0.0
    lp: float = 12_000.0         # per-voice low-pass
    attack: float = 0.004


def _one_pole(x: np.ndarray, cutoff) -> np.ndarray:
    """One-pole low-pass, matching theirs: coef = 1 - exp(-2*pi*fc/sr).

    `cutoff` may be an array, which is how the noise voices sweep their filter.
    """
    coef = 1.0 - np.exp(-2.0 * math.pi * np.minimum(cutoff, 12_000.0) / SR)
    coef = np.broadcast_to(np.asarray(coef, dtype=np.float64), x.shape)
    out = np.empty_like(x)
    state = 0.0
    for i in range(x.size):
        state += coef[i] * (x[i] - state)
        out[i] = state
    return out


def render(voices: list[Voice]) -> np.ndarray:
    total = max((v.delay + v.dur for v in voices), default=0.1) + 0.05
    buf = np.zeros(int(total * SR), dtype=np.float64)
    rng = np.random.default_rng(0x9E3779B9)

    for v in voices:
        n = int(v.dur * SR)
        if n <= 0:
            continue
        t = np.arange(n) / SR
        progress = t / v.dur
        sweeps = v.f1 > 0 and v.f1 != v.f0

        # Envelope: linear attack, then exponential decay toward silence.
        amp = np.empty(n)
        atk = max(v.attack, 1e-6)
        rising = t < atk
        amp[rising] = v.gain * (t[rising] / atk)
        decay = (t[~rising] - atk) / max(v.dur - atk, 0.001)
        amp[~rising] = v.gain * np.power(0.0001 / v.gain, decay)

        if v.shape == "noise":
            sample = rng.uniform(-1.0, 1.0, n)
            # For noise, f0 -> f1 IS the low-pass sweep.
            cutoff = (v.f0 * np.power(v.f1 / v.f0, progress)) if sweeps else v.f0
        else:
            freq = (v.f0 * np.power(v.f1 / v.f0, progress)) if sweeps else np.full(n, v.f0)
            phase = np.cumsum(freq) / SR
            p = np.mod(phase, 1.0)
            if v.shape == "sine":
                sample = np.sin(2 * math.pi * p)
            elif v.shape == "triangle":
                sample = 4 * np.abs(p - 0.5) - 1
            else:
                sample = 2 * p - 1
            cutoff = v.lp

        # Per-voice low-pass, then the master ceiling. Order matters.
        filtered = _one_pole(sample, cutoff)
        filtered = _one_pole(filtered, CEILING_HZ)

        start = int(v.delay * SR)
        end = min(start + n, buf.size)
        buf[start:end] += filtered[: end - start] * amp[: end - start]

    return np.clip(buf, -1.0, 1.0)


def thud(f0=80.0, f1=42.0, dur=0.14, gain=0.5, knock=0.0, delay=0.0):
    """Sub sine drop plus a whisper of noise for the contact texture."""
    voices = [Voice("sine", f0, f1, dur, gain, delay, lp=300, attack=0.002)]
    if knock > 0:
        voices.append(Voice("noise", 420, 180, 0.04, knock, delay, attack=0.002))
    return voices


# --- the "sotto" pack, verbatim from their certified set ---------------------
#
# ack   F3 + F4   a wood-bar acknowledgement
# merge C3 + C4   felt piano, the transcript coming together
# charge          a sub swell while the model works
# launch          noise sweeping downward as the comet leaves
# arrive          noise splash plus a sub drop as it lands
PACK = {
    "ack": [
        Voice("sine", 175, dur=0.16, gain=0.30, lp=800, attack=0.002),
        Voice("sine", 350, dur=0.05, gain=0.07, lp=800, attack=0.002),
    ],
    "merge": [
        Voice("sine", 131, dur=0.25, gain=0.26, lp=600, attack=0.003),
        Voice("sine", 262, dur=0.10, gain=0.06, lp=600, attack=0.003),
    ],
    "charge": [Voice("sine", 46, 88, dur=0.55, gain=0.34, lp=240, attack=0.2)],
    "launch": [Voice("noise", 520, 130, dur=0.48, gain=0.50, attack=0.01)],
    "arrive": [
        Voice("noise", 1_800, 200, dur=0.13, gain=0.22, attack=0.004),
        Voice("sine", 87, 44, dur=0.12, gain=0.30, delay=0.02, lp=300, attack=0.003),
    ],
    # Murmur has a state Sotto does not: a cancelled session. A falling minor
    # third under the same ceiling, so it belongs to the set without being a
    # success sound.
    "cancel": [
        Voice("sine", 147, 110, dur=0.22, gain=0.26, lp=500, attack=0.004),
        Voice("noise", 300, 120, dur=0.06, gain=0.05, attack=0.003),
    ],
}


def write(name: str, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples))) or 1.0
    pcm = (np.clip(samples / peak * 0.82, -1.0, 1.0) * 32767).astype(np.int16)
    path = OUT / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"  {name + '.wav':<14} {len(pcm) / SR * 1000:5.0f}ms  "
          f"{path.stat().st_size:>6} bytes  peak {peak:.3f}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("writing cues (sotto pack, F/C anchors under a 2kHz ceiling):")
    for name, voices in PACK.items():
        write(name, render(voices))

    # Murmur's cue names map onto theirs.
    for ours, theirs in (("start", "ack"), ("done", "arrive")):
        src, dst = OUT / f"{theirs}.wav", OUT / f"{ours}.wav"
        dst.write_bytes(src.read_bytes())
        print(f"  {ours + '.wav':<14} <- {theirs}.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
