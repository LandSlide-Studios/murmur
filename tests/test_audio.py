import numpy as np

from murmur.audio import RingBuffer, rms


def test_ring_buffer_returns_written_audio_in_order():
    rb = RingBuffer(capacity=10)
    rb.write(np.array([1, 2, 3], dtype=np.float32))
    rb.write(np.array([4, 5], dtype=np.float32))
    assert rb.read_all().tolist() == [1, 2, 3, 4, 5]


def test_ring_buffer_drops_oldest_when_full():
    rb = RingBuffer(capacity=4)
    rb.write(np.arange(6, dtype=np.float32))
    assert rb.read_all().tolist() == [2, 3, 4, 5]


def test_write_larger_than_capacity_keeps_the_tail():
    rb = RingBuffer(capacity=3)
    rb.write(np.arange(10, dtype=np.float32))
    assert rb.read_all().tolist() == [7, 8, 9]


def test_wraparound_across_many_writes_preserves_order():
    rb = RingBuffer(capacity=5)
    for i in range(20):
        rb.write(np.array([i], dtype=np.float32))
    assert rb.read_all().tolist() == [15, 16, 17, 18, 19]


def test_write_exactly_filling_capacity():
    rb = RingBuffer(capacity=4)
    rb.write(np.arange(4, dtype=np.float32))
    assert rb.read_all().tolist() == [0, 1, 2, 3]


def test_reset_empties_buffer():
    rb = RingBuffer(capacity=4)
    rb.write(np.arange(3, dtype=np.float32))
    rb.reset()
    assert rb.read_all().size == 0


def test_read_all_on_empty_returns_empty_float32():
    rb = RingBuffer(capacity=4)
    out = rb.read_all()
    assert out.size == 0 and out.dtype == np.float32


def test_read_all_returns_a_copy_not_a_view():
    rb = RingBuffer(capacity=4)
    rb.write(np.array([1, 2], dtype=np.float32))
    out = rb.read_all()
    out[0] = 99
    assert rb.read_all().tolist() == [1, 2]


def test_write_accepts_2d_block_from_sounddevice():
    """sounddevice hands back shape (frames, channels)."""
    rb = RingBuffer(capacity=4)
    rb.write(np.array([[1.0], [2.0]], dtype=np.float32))
    assert rb.read_all().tolist() == [1.0, 2.0]


def test_empty_write_is_a_no_op():
    rb = RingBuffer(capacity=4)
    rb.write(np.array([1, 2], dtype=np.float32))
    rb.write(np.zeros(0, dtype=np.float32))
    assert rb.read_all().tolist() == [1, 2]


def test_rms_of_silence_is_zero():
    assert rms(np.zeros(100, dtype=np.float32)) == 0.0


def test_rms_of_constant_signal_is_its_magnitude():
    assert abs(rms(np.full(100, 0.5, dtype=np.float32)) - 0.5) < 1e-6


def test_rms_of_empty_block_is_zero_not_nan():
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0


# --- the reason a 42-second dictation was thrown away ------------------------

def test_peak_rms_finds_speech_that_the_average_buries():
    """THE bug this function exists for.

    A 42.4s hold with the user talking through it was logged as "recording is
    silent" and discarded. The guard averaged RMS over the whole clip, so every
    thinking pause counted against it: the longer the dictation, the more likely
    it was thrown away. Backwards from how it should behave.
    """
    from murmur.audio import peak_rms, rms

    sr = 16000
    rng = np.random.default_rng(0)
    clip = rng.normal(0, 0.00086, sr * 42).astype(np.float32)   # his measured floor
    clip[:sr * 12] = rng.normal(0, 0.008, sr * 12)              # 12s of real speech

    assert rms(clip) < 0.006, "precondition: the old guard discarded this"
    assert peak_rms(clip, sr) > 0.006, "speech is plainly in there"


def test_a_longer_pause_does_not_make_a_recording_look_emptier():
    """The average punishes length. The peak must not."""
    from murmur.audio import peak_rms

    sr = 16000
    rng = np.random.default_rng(1)
    speech = rng.normal(0, 0.008, sr * 5).astype(np.float32)

    levels = []
    for pause_s in (0, 10, 30, 60):
        clip = np.concatenate(
            [speech, rng.normal(0, 0.00086, sr * pause_s).astype(np.float32)])
        levels.append(peak_rms(clip, sr))
    assert max(levels) - min(levels) < 0.001, f"length changed the verdict: {levels}"


def test_peak_rms_still_calls_a_truly_empty_room_silent():
    """It must not become so permissive that a cue or a cough sends a dictation."""
    from murmur.audio import peak_rms

    sr = 16000
    room = np.random.default_rng(2).normal(0, 0.00086, sr * 20).astype(np.float32)
    assert peak_rms(room, sr) < 0.006


def test_peak_rms_handles_clips_shorter_than_one_window():
    from murmur.audio import peak_rms

    assert peak_rms(np.zeros(0, dtype=np.float32), 16000) == 0.0
    loud = np.full(800, 0.3, dtype=np.float32)
    assert peak_rms(loud, 16000) > 0.2
