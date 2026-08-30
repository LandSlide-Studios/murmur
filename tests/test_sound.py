"""Audio cues, and the reason they must not reach the microphone."""
import time
import wave
from pathlib import Path

import numpy as np

from murmur.audio import Recorder
from murmur.sound import CUES, Sounds

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def test_every_cue_file_exists():
    for cue in CUES:
        assert (ASSETS / f"{cue}.wav").exists(), f"{cue}.wav is missing"


# Punctuation cues are brief; the two that cover a wait are deliberately longer.
SUSTAINED = {"charge", "launch"}


def test_cues_are_short_enough_to_stay_out_of_the_way():
    for cue in CUES:
        with wave.open(str(ASSETS / f"{cue}.wav"), "rb") as w:
            ms = w.getnframes() / w.getframerate() * 1000
        limit = 700 if cue in SUSTAINED else 400
        assert 40 <= ms <= limit, f"{cue} is {ms:.0f}ms"


def test_the_tonal_cues_keep_their_energy_low():
    """Sotto's principle: felt, not heard. Energy at 40-600Hz, nothing pierces.
    The noise cues (launch, arrive) are textures and are exempt."""
    import numpy as np

    for cue in ("start", "charge", "merge", "cancel"):
        with wave.open(str(ASSETS / f"{cue}.wav"), "rb") as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(np.float64) / 32768
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1 / sr)
        total = spec.sum() or 1.0
        assert spec[(freqs >= 40) & (freqs <= 600)].sum() / total > 0.8, cue
        assert spec[freqs > 2000].sum() / total < 0.05, f"{cue} pierces"


def test_the_anchor_tones_are_f_and_c():
    """Cues anchor to F and C so repeated use never sounds out of tune with
    itself. Straight from Sotto's certified set."""
    import numpy as np

    def peak_hz(cue):
        with wave.open(str(ASSETS / f"{cue}.wav"), "rb") as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(np.float64) / 32768
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return np.fft.rfftfreq(len(x), 1 / sr)[int(np.argmax(spec))]

    assert abs(peak_hz("start") - 175) < 12, "ack should sit on F3"
    assert abs(peak_hz("merge") - 131) < 12, "merge should sit on C3"


def test_cues_do_not_clip():
    for cue in CUES:
        with wave.open(str(ASSETS / f"{cue}.wav"), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        assert np.abs(pcm).max() < 32767, f"{cue} clips"


def test_cues_start_and_end_near_zero_so_they_do_not_click():
    for cue in CUES:
        with wave.open(str(ASSETS / f"{cue}.wav"), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        assert abs(int(pcm[0])) < 800, f"{cue} starts with a click"
        assert abs(int(pcm[-1])) < 800, f"{cue} ends with a click"


def test_durations_are_reported_for_the_mute_window():
    s = Sounds(enabled=True)
    if not s.enabled:                      # non-Windows
        return
    for cue in CUES:
        assert s.duration_ms(cue) > 0


def test_disabled_sounds_never_play():
    s = Sounds(enabled=False)
    s.play("start")                        # must not raise
    assert s.duration_ms("start") == 0


def test_an_unknown_cue_is_ignored():
    Sounds(enabled=True).play("nope")      # must not raise


# --- the reason the mute exists ---------------------------------------------

def test_muted_audio_is_not_recorded():
    """The speakers are audible to the microphone, and Whisper hallucinates
    words from tones — cue-only audio transcribed to "Thanks."."""
    r = Recorder(sample_rate=16000)
    r._capturing = True
    r.mute_for(200)
    loud = np.full(1600, 0.5, dtype=np.float32)
    r._callback(loud.reshape(-1, 1), 1600, None, None)
    assert r.buffer.read_all().size == 0, "audio was recorded while muted"


def test_capture_resumes_after_the_mute_window():
    r = Recorder(sample_rate=16000)
    r._capturing = True
    r.mute_for(10)
    time.sleep(0.05)
    block = np.full(1600, 0.5, dtype=np.float32)
    r._callback(block.reshape(-1, 1), 1600, None, None)
    assert r.buffer.read_all().size == 1600


def test_mute_also_keeps_cues_out_of_the_preroll():
    """The pre-roll is always running, so a cue would land in the NEXT
    session's audio if it were not muted too."""
    r = Recorder(sample_rate=16000)
    r._capturing = False
    r.mute_for(200)
    block = np.full(1600, 0.5, dtype=np.float32)
    r._callback(block.reshape(-1, 1), 1600, None, None)
    assert r.preroll.read_all().size == 0
