"""A long dictation costs proportionally, not quadratically.

Tier 5 of the audit remediation. Everything here is worst exactly when the user
has said the most, which is the wrong way round.
"""
import time
import tracemalloc

import numpy as np
import pytest

from murmur.audio import RingBuffer, peak_rms


def peak_alloc(fn):
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# --- reading the buffer ------------------------------------------------------

def test_read_all_makes_one_copy_not_four():
    """It built an int64 index for every sample — an arange, an add and a
    modulo, each twice the width of the audio — then fancy indexed with it and
    copied an array fancy indexing had already copied. 461MB transient for a
    115MB recording, on the chord-release path."""
    cap = 16_000 * 100
    rb = RingBuffer(cap)
    rb.write(np.zeros(cap + 5, dtype=np.float32))
    result_bytes = rb.read_all().nbytes
    assert peak_alloc(rb.read_all) < result_bytes * 1.6


def test_reading_a_full_buffer_is_quick():
    """The module docstring's justification for keeping the stream open is that
    ending a session is instant. Measured at 235-300ms on the shipped ceiling."""
    cap = 16_000 * 600
    rb = RingBuffer(cap)
    rb.write(np.zeros(cap + 5, dtype=np.float32))
    rb.read_all()                                   # warm any lazy allocation
    start = time.perf_counter()
    rb.read_all()
    assert (time.perf_counter() - start) < 0.05


@pytest.mark.parametrize("cap,writes", [
    (10, [3, 4, 5]), (10, [10]), (10, [11]), (10, [7, 7]),
    (16, [5, 5, 5, 5]), (7, [1] * 20), (5, [4, 3, 4]),
])
def test_the_rewritten_read_returns_the_same_samples_in_the_same_order(cap, writes):
    """A differential check against a reference deque, because a faster read
    that returns the wrong samples is far worse than a slow one."""
    from collections import deque

    rb = RingBuffer(cap)
    ref = deque(maxlen=cap)
    value = 0.0
    for n in writes:
        block = np.arange(value, value + n, dtype=np.float32)
        value += n
        rb.write(block)
        ref.extend(block.tolist())
        assert rb.read_all().tolist() == list(ref), f"cap={cap} writes={writes}"


def test_an_empty_buffer_still_reads_empty():
    assert RingBuffer(10).read_all().size == 0


def test_a_buffer_that_never_wrapped_reads_correctly():
    rb = RingBuffer(100)
    rb.write(np.arange(30, dtype=np.float32))
    assert rb.read_all().tolist() == list(range(30))


# --- scanning the level ------------------------------------------------------

def test_peak_rms_does_not_copy_the_clip_at_double_width():
    """It promoted the whole clip to float64 and then squared it — two
    full-size temporaries at double width, immediately after read_all had done
    the same thing on the same path."""
    clip = np.zeros(16_000 * 200, dtype=np.float32)
    assert peak_alloc(lambda: peak_rms(clip, 16_000)) < clip.nbytes * 1.2


@pytest.mark.parametrize("build,expected", [
    (lambda w: np.concatenate([np.zeros(w), np.full(w, 0.5), np.zeros(w)]), 0.5),
    (lambda w: np.full(w * 3, 0.25), 0.25),
    (lambda w: np.zeros(w * 3), 0.0),
])
def test_the_chunked_scan_finds_the_same_peak(build, expected):
    win = 6400
    clip = build(win).astype(np.float32)
    assert peak_rms(clip, 16_000) == pytest.approx(expected, abs=0.01)


def test_a_peak_spanning_a_chunk_boundary_is_still_found():
    """The scan runs in chunks of many windows; a burst landing on the seam
    must not be missed or halved."""
    win = 6400
    clip = np.zeros(win * 1100, dtype=np.float32)
    clip[win * 512:win * 513] = 0.5              # exactly on the chunk edge
    assert peak_rms(clip, 16_000) == pytest.approx(0.5, abs=0.01)


def test_nan_handling_survives_the_rewrite():
    loud = np.full(32_000, 0.4, dtype=np.float32)
    loud[123] = np.nan
    assert peak_rms(loud, 16_000) > 0.3
    assert peak_rms(np.full(32_000, np.nan, dtype=np.float32), 16_000) == 0.0
    inf = np.full(32_000, 0.4, dtype=np.float32)
    inf[7] = np.inf
    assert peak_rms(inf, 16_000) > 0.3


def test_a_short_tail_is_still_measured():
    win = 6400
    clip = np.zeros(win + 3199, dtype=np.float32)
    clip[win:] = 0.5
    assert peak_rms(clip, 16_000) > 0.05


# --- the resting overlay stops working ---------------------------------------

def test_the_armed_pill_stops_repainting_once_it_has_settled(pill):
    """Armed draws only its capsule, but the timer kept stepping fifteen
    springs and repainting a translucent always-on-top window sixty times a
    second, indefinitely."""
    pill.set_state("armed")
    for _ in range(600):
        pill._tick()
        if not pill._timer.isActive():
            break
    assert not pill._timer.isActive(), "still ticking with a static image"


def test_a_new_state_starts_the_timer_again(pill):
    pill.set_state("armed")
    for _ in range(600):
        pill._tick()
    pill.set_state("recording", "hold")
    assert pill._timer.isActive()


def test_an_animating_pill_keeps_ticking(pill):
    """The counterweight: stopping too eagerly would freeze the meter."""
    pill.set_state("recording", "hold")
    pill.set_level(0.02)
    for _ in range(120):
        pill._tick()
    assert pill._timer.isActive()
