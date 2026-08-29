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
