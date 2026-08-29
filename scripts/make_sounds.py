"""Generate the audio cues.

Short, quiet, and clearly not speech. Three constraints shaped them:

* The microphone is open when they play, so the start cue lands in the recording.
  Pure tones survive Whisper's VAD as non-speech and transcribe to nothing, but
  they are kept brief and low anyway.
* No click at the edges. A raw sine cut at a zero-crossing still pops; every tone
  gets a fast attack and an exponential decay.
* Distinguishable without being looked at: start rises, done resolves upward a
  fifth, cancel falls.
"""

import struct
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "assets"


def tone(freq_from: float, freq_to: float, ms: int, gain: float = 0.22,
         harmonic: float = 0.18) -> np.ndarray:
    n = int(RATE * ms / 1000)
    t = np.linspace(0.0, ms / 1000.0, n, endpoint=False)
    # Glide in log space so the sweep sounds linear to the ear.
    freq = np.exp(np.linspace(np.log(freq_from), np.log(freq_to), n))
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    wave_ = np.sin(phase) + harmonic * np.sin(2 * phase)

    attack = int(RATE * 0.004)
    env = np.exp(-np.linspace(0.0, 4.2, n))
    env[:attack] *= np.linspace(0.0, 1.0, attack)
    return (wave_ * env * gain).astype(np.float32)


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(RATE * ms / 1000), dtype=np.float32)


def write(name: str, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples))) or 1.0
    pcm = (np.clip(samples / peak * 0.85, -1.0, 1.0) * 32767).astype(np.int16)
    path = OUT / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"  {name:<12} {len(pcm) / RATE * 1000:5.0f}ms  {path.stat().st_size:>6} bytes")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("writing cues:")

    # Start: a quick rise. "Go."
    write("start.wav", np.concatenate([
        tone(523.25, 783.99, 90),          # C5 -> G5
    ]))

    # Done: two notes resolving upward. "Landed."
    write("done.wav", np.concatenate([
        tone(783.99, 783.99, 70, gain=0.18),   # G5
        silence(8),
        tone(1046.50, 1046.50, 120, gain=0.22),  # C6
    ]))

    # Cancel: a short fall. Unmistakably not a success.
    write("cancel.wav", np.concatenate([
        tone(440.00, 293.66, 130, gain=0.18),  # A4 -> D4
    ]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
