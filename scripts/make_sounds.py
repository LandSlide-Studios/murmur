"""Generate the audio cues.

The synthesis model and all five packs are adapted from Sotto
(https://github.com/kingbootoshi/sotto), MIT licensed — see NOTICE.md. Their
`SoundPlayer.swift` is the source for the voice model, the envelope, the sweep
law, the master ceiling and every recipe below.

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


def _heartbeat_charge():
    return [v for i, delay in enumerate((0.0, 0.17, 0.3, 0.4))
            for v in thud(f0=60 + i * 4, f1=42, dur=0.09,
                          gain=0.25 + i * 0.07, delay=delay, knock=0)]


# --- the five packs, verbatim from Sotto's SoundPlayer.swift -----------------
#
# Their own descriptions:
#   sotto        the certified set (F/C, wood + felt + swell)
#   velvet_thud  sub pulses you feel
#   warm_glass   muted mallet tones
#   wood_bar     round marimba notes
#   breath       pure air, almost silent
#   heartbeat    lub-dub pulses
#
# Murmur adds a `cancel` cue to each, written in that pack's own voice, because
# Murmur has a cancelled state Sotto has no use for.

PACKS = {
    "sotto": {
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
        "cancel": [
            Voice("sine", 147, 110, dur=0.22, gain=0.26, lp=500, attack=0.004),
            Voice("noise", 300, 120, dur=0.06, gain=0.05, attack=0.003),
        ],
    },
    "velvet_thud": {
        "ack": thud(95, 50, gain=0.55) + thud(78, 44, gain=0.40, delay=0.1),
        "merge": thud(82, 40, dur=0.16, gain=0.5),
        "charge": [Voice("sine", 46, 88, dur=0.5, gain=0.34, lp=240, attack=0.18)],
        "launch": [Voice("noise", 520, 130, dur=0.48, gain=0.50, attack=0.01)],
        "arrive": thud(66, 36, dur=0.18, gain=0.65, knock=0.1),
        "cancel": thud(70, 34, dur=0.2, gain=0.42)
                  + thud(52, 30, dur=0.18, gain=0.28, delay=0.11),
    },
    "warm_glass": {
        "ack": [
            Voice("sine", 392, dur=0.30, gain=0.12, lp=1_100),
            Voice("sine", 587, dur=0.34, gain=0.07, delay=0.07, lp=1_100),
        ] + thud(70, 48, gain=0.2, knock=0),
        "merge": [Voice("sine", 523, 349, dur=0.22, gain=0.1, lp=950)]
                 + thud(75, 45, gain=0.22, knock=0),
        "charge": [Voice("triangle", 131, 196, dur=0.5, gain=0.12, lp=700, attack=0.16)],
        "launch": [Voice("noise", 900, 260, dur=0.45, gain=0.30, attack=0.01)],
        "arrive": [
            Voice("sine", 659, dur=0.20, gain=0.09, lp=1_300),
            Voice("sine", 494, dur=0.26, gain=0.07, delay=0.02, lp=1_100),
        ] + thud(64, 40, gain=0.4, knock=0),
        "cancel": [Voice("sine", 466, 349, dur=0.26, gain=0.10, lp=950)]
                  + thud(62, 38, gain=0.24, knock=0),
    },
    "wood_bar": {
        "ack": [
            Voice("sine", 220, dur=0.20, gain=0.22, lp=900),
            Voice("sine", 440, dur=0.07, gain=0.06, lp=900),
            Voice("sine", 262, dur=0.20, gain=0.18, delay=0.11, lp=900),
        ],
        "merge": [Voice("sine", 175, dur=0.18, gain=0.2, lp=800)]
                 + thud(70, 46, gain=0.2, knock=0),
        "charge": [Voice("triangle", 98, 147, dur=0.5, gain=0.16, lp=500, attack=0.15)],
        "launch": [Voice("noise", 700, 200, dur=0.46, gain=0.34, attack=0.01)],
        "arrive": [
            Voice("sine", 147, dur=0.24, gain=0.28, lp=700),
            Voice("sine", 294, dur=0.07, gain=0.07, lp=800),
        ],
        "cancel": [
            Voice("sine", 196, dur=0.18, gain=0.22, lp=800),
            Voice("sine", 147, dur=0.22, gain=0.20, delay=0.10, lp=700),
        ],
    },
    "breath": {
        "ack": [
            Voice("noise", 650, 280, dur=0.16, gain=0.30, attack=0.01),
            Voice("noise", 800, 350, dur=0.10, gain=0.20, delay=0.11, attack=0.01),
        ],
        "merge": [Voice("noise", 460, 160, dur=0.20, gain=0.32, attack=0.01)],
        "charge": [Voice("noise", 180, 520, dur=0.5, gain=0.22, attack=0.2)],
        "launch": [Voice("noise", 1_000, 170, dur=0.5, gain=0.42, attack=0.01)],
        "arrive": [Voice("noise", 380, 200, dur=0.08, gain=0.30, attack=0.01)]
                  + thud(58, 38, dur=0.12, gain=0.4, knock=0),
        "cancel": [Voice("noise", 300, 110, dur=0.24, gain=0.28, attack=0.02)],
    },
    "heartbeat": {
        "ack": thud(62, 44, dur=0.1, gain=0.5, knock=0)
               + thud(54, 40, dur=0.1, gain=0.34, delay=0.15, knock=0),
        "merge": thud(58, 40, dur=0.13, gain=0.4, knock=0),
        "charge": _heartbeat_charge(),
        "launch": [
            Voice("sawtooth", 190, 55, dur=0.45, gain=0.16, lp=280),
            Voice("noise", 380, 110, dur=0.45, gain=0.30, attack=0.01),
        ],
        "arrive": thud(68, 34, dur=0.2, gain=0.7, knock=0.08),
        "cancel": thud(58, 32, dur=0.16, gain=0.4, knock=0)
                  + thud(46, 28, dur=0.18, gain=0.28, delay=0.13, knock=0),
    },
}

DEFAULT_PACK = "sotto"

DESCRIPTIONS = {
    "sotto": "Sotto — wood, felt and a swell",
    "velvet_thud": "Velvet Thud — sub pulses you feel",
    "warm_glass": "Warm Glass — muted mallet tones",
    "wood_bar": "Wood Bar — round marimba notes",
    "breath": "Breath — pure air, almost silent",
    "heartbeat": "Heartbeat — lub-dub pulses",
}


# Sotto's master mixer sits at 0.9. Applying one fixed scale rather than
# normalising each file is the whole point: per-file normalisation would make
# "Breath — almost silent" exactly as loud as "Heartbeat — pulses you feel",
# flattening the design it was ported from.
MASTER = 0.9


def write(pack: str, name: str, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples)))
    pcm = (np.clip(samples * MASTER, -1.0, 1.0) * 32767).astype(np.int16)
    folder = OUT / "sounds" / pack
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"    {name + '.wav':<12} {len(pcm) / SR * 1000:5.0f}ms  peak {peak:.3f}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for pack, cues in PACKS.items():
        print(f"{pack}  ({DESCRIPTIONS[pack]})")
        for name, voices in cues.items():
            write(pack, name, render(voices))
        # Murmur's cue names map onto theirs.
        folder = OUT / "sounds" / pack
        for ours, theirs in (("start", "ack"), ("done", "arrive")):
            (folder / f"{ours}.wav").write_bytes((folder / f"{theirs}.wav").read_bytes())
    print(f"\n{len(PACKS)} packs x {len(PACKS[DEFAULT_PACK])} cues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
