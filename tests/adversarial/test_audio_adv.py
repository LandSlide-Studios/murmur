"""Adversarial scenarios for murmur/audio.py.

Written air-gapped: derived from audio.py's own docstrings and from first
principles about what a correct capture buffer must do. The engineering log,
the locked-decision list and tests/test_audio.py were deliberately not read.

SAFETY
------
No test in this file may open a real audio device. A leaked PortAudio stream
crashes the interpreter at exit and eats the pytest summary. Two belts:

  1. `_no_real_device` (autouse) shoves a fake `sounddevice` module into
     sys.modules, so `Recorder.open()`'s function-local `import sounddevice`
     can never reach PortAudio even by accident.
  2. Capture is driven by calling `Recorder._callback` directly.

Nothing here touches %APPDATA%. audio.py performs no I/O.
"""

import collections
import gc
import sys
import threading
import time
import tracemalloc
import types

import numpy as np
import pytest

from murmur.audio import DEFAULT_PREROLL_MS, Recorder, RingBuffer, peak_rms, rms


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class _FakeStream:
    """Stands in for sd.InputStream. Never touches hardware."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _no_real_device(monkeypatch):
    fake = types.ModuleType("sounddevice")
    fake.InputStream = _FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    yield


def blk(value, n, dtype=np.float32):
    return np.full(n, value, dtype=dtype)


def deliver(rec, block):
    """One PortAudio callback with `block` as mono float32 input."""
    arr = np.asarray(block, dtype=np.float32)
    rec._callback(arr, arr.size, None, None)


def model_ring(cap, blocks):
    """Reference ring buffer: append everything, keep the newest `cap`."""
    d = collections.deque(maxlen=cap)
    for b in blocks:
        d.extend(np.asarray(b, dtype=np.float32).ravel().tolist())
    return np.array(d, dtype=np.float32)


# ==========================================================================
# rms()
# ==========================================================================

def test_01_rms_of_empty_block_is_zero():
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_02_rms_of_a_single_sample_is_its_magnitude():
    assert rms(np.array([-0.75], dtype=np.float32)) == pytest.approx(0.75)


def test_03_rms_is_sign_blind():
    """RMS squares; an all-negative signal must measure the same as its mirror."""
    neg = np.linspace(-0.9, -0.1, 4096).astype(np.float32)
    assert rms(neg) == pytest.approx(rms(-neg), rel=1e-9)


def test_04_rms_promotes_to_float64_so_denormals_do_not_vanish():
    """float32 squaring would underflow 1e-25 to exactly 0. float64 does not."""
    tiny = blk(1e-25, 1024)
    assert rms(tiny) == pytest.approx(1e-25, rel=1e-6)
    assert float(np.sqrt(np.mean(np.square(tiny)))) == 0.0  # the trap avoided


def test_05_rms_promotes_to_float64_so_loud_input_does_not_overflow():
    loud = blk(3.0e20, 512)
    assert np.isfinite(rms(loud))
    assert rms(loud) == pytest.approx(3.0e20, rel=1e-6)


def test_06_rms_reports_dc_offset_as_signal():
    """No mean removal: a rail-stuck mic with zero AC energy measures exactly
    as loud as speech at the same amplitude. Recorded, not claimed as a bug --
    the threshold that would make it matter lives outside this module."""
    dc = blk(0.02, 16000)
    assert rms(dc) == pytest.approx(0.02, rel=1e-6)
    assert rms(dc) == pytest.approx(rms(dc * -1), rel=1e-6)


def test_07_rms_of_int16_counts_is_not_normalised():
    """int16 PCM is measured in raw counts, four orders of magnitude above the
    float32 scale every caller threshold is written against."""
    assert rms(np.full(1024, 15000, dtype=np.int16)) == pytest.approx(15000.0)


def test_08_rms_does_not_mutate_its_input():
    src = np.linspace(-1, 1, 777).astype(np.float32)
    copy = src.copy()
    rms(src)
    assert np.array_equal(src, copy)


def test_09_rms_of_a_2d_block_uses_every_channel():
    """rms() does no channel selection; only _callback does."""
    stereo = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    assert rms(stereo) == pytest.approx(np.sqrt(0.5))


def test_10_rms_rejects_a_plain_list_while_peak_rms_accepts_one():
    """Asymmetry: peak_rms() coerces with asarray, rms() requires .size."""
    with pytest.raises(AttributeError):
        rms([0.1, 0.2, 0.3])
    assert peak_rms([0.1, 0.2, 0.3]) > 0.0


def test_11_rms_propagates_nan_and_inf():
    with_nan = np.array([0.5, np.nan, 0.5], dtype=np.float32)
    assert np.isnan(rms(with_nan))
    assert np.isinf(rms(np.array([0.0, np.inf], dtype=np.float32)))
    assert np.isinf(rms(np.array([0.0, -np.inf], dtype=np.float32)))


# ==========================================================================
# peak_rms()
# ==========================================================================

SR = 16000
WMS = 400
WIN = int(SR * WMS / 1000)      # 6400


def test_12_peak_rms_of_empty_is_zero():
    assert peak_rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_13_peak_rms_takes_the_max_not_the_mean():
    """The stated purpose: a long quiet clip with one loud window is speech."""
    pcm = np.zeros(SR * 40, dtype=np.float32)
    pcm[WIN * 3:WIN * 4] = 0.5
    assert peak_rms(pcm, SR, WMS) == pytest.approx(0.5, rel=1e-5)
    # the whole-clip mean is 10x quieter -- that is the bug peak_rms exists for
    assert rms(pcm) < peak_rms(pcm, SR, WMS) / 5


def test_14_peak_rms_window_longer_than_clip_falls_back_to_whole_clip_mean():
    pcm = np.zeros(WIN // 2, dtype=np.float32)
    pcm[:100] = 1.0
    assert peak_rms(pcm, SR, WMS) == pytest.approx(rms(pcm))


def test_15_peak_rms_at_exactly_one_window_uses_the_whole_clip():
    pcm = blk(0.4, WIN)
    assert peak_rms(pcm, SR, WMS) == pytest.approx(0.4, rel=1e-5)


def test_16_peak_rms_one_sample_short_of_a_window():
    pcm = blk(0.4, WIN - 1)
    assert peak_rms(pcm, SR, WMS) == pytest.approx(0.4, rel=1e-5)


def test_17_speech_confined_to_a_short_tail_is_reported_as_silence():
    """DEFECT CLAIM. 199.9ms of full-scale audio at the end of a clip is
    dropped on the floor: any tail shorter than half a window is discarded
    without ever being measured. This function's whole reason to exist is
    "is there ANY speech in here?", and here the answer is demonstrably yes
    while it returns 0.0."""
    tail = WIN // 2 - 1                       # 3199 samples = 199.9ms
    pcm = np.zeros(WIN + tail, dtype=np.float32)
    pcm[WIN:] = 0.5
    assert peak_rms(pcm, SR, WMS) > 0.0


def test_18_one_extra_sample_flips_the_verdict_from_silent_to_loud():
    """DEFECT CLAIM, sharpened. Two clips differing by a single sample of
    zero padding get answers that are not within an order of magnitude."""
    short = np.zeros(WIN + WIN // 2 - 1, dtype=np.float32)
    short[WIN:] = 0.5
    longer = np.zeros(WIN + WIN // 2, dtype=np.float32)
    longer[WIN:] = 0.5
    assert peak_rms(short, SR, WMS) == pytest.approx(
        peak_rms(longer, SR, WMS), rel=0.1)


def test_19_a_burst_straddling_a_window_boundary_is_underestimated():
    """DEFECT CLAIM (low severity). The docstring says "the loudest short
    window in a recording". The windows are non-overlapping, so the loudest
    window is only found when the speech happens to be aligned to a multiple
    of 400ms. The same burst shifted by half a window measures 1/sqrt(2)
    lower -- a 29% sensitivity loss whose worst case is the short utterance."""
    aligned = np.zeros(WIN * 4, dtype=np.float32)
    aligned[0:WIN] = 0.5
    straddling = np.zeros(WIN * 4, dtype=np.float32)
    straddling[WIN // 2:WIN // 2 + WIN] = 0.5
    assert peak_rms(straddling, SR, WMS) == pytest.approx(
        peak_rms(aligned, SR, WMS), rel=0.05)


def test_20_a_single_nan_sample_makes_a_loud_clip_read_as_not_speech():
    """DEFECT CLAIM. np.max over an array containing NaN returns NaN, and NaN
    fails every "> threshold" comparison, so one bad sample anywhere in a clip
    of full-scale speech makes the whole clip test as silence. np.nanmax would
    cost nothing."""
    pcm = blk(0.4, WIN * 5)
    pcm[123] = np.nan
    assert peak_rms(pcm, SR, WMS) > 0.01


def test_21_positive_infinity_survives_as_infinity():
    pcm = np.zeros(WIN * 3, dtype=np.float32)
    pcm[10] = np.inf
    assert np.isinf(peak_rms(pcm, SR, WMS))


def test_22_peak_rms_handles_sample_rates_that_do_not_divide_the_window():
    for sr in (11025, 22050, 44100, 48000):
        win = int(sr * WMS / 1000)
        pcm = np.zeros(win * 3 + 7, dtype=np.float32)
        pcm[win:win * 2] = 0.6
        assert peak_rms(pcm, sr, WMS) == pytest.approx(0.6, rel=1e-4)


def test_23_window_ms_zero_degenerates_to_the_peak_sample():
    pcm = np.zeros(1000, dtype=np.float32)
    pcm[500] = -0.9
    assert peak_rms(pcm, SR, window_ms=0) == pytest.approx(0.9, rel=1e-5)


def test_24_sample_rate_zero_does_not_divide_by_zero():
    pcm = np.zeros(1000, dtype=np.float32)
    pcm[7] = 0.3
    assert peak_rms(pcm, sample_rate=0, window_ms=WMS) == pytest.approx(
        0.3, rel=1e-5)


def test_25_peak_rms_accepts_2d_and_non_contiguous_input():
    base = np.zeros((WIN * 3, 2), dtype=np.float32)
    base[WIN:WIN * 2, 0] = 0.5
    strided = base[:, 0]
    assert not strided.flags["C_CONTIGUOUS"]
    assert peak_rms(strided, SR, WMS) == pytest.approx(0.5, rel=1e-4)
    assert peak_rms(base, SR, WMS) > 0.0          # ravel interleaves, no crash


def test_26_peak_rms_of_int16_counts_is_not_normalised():
    pcm = np.full(WIN * 3, 15000, dtype=np.int16)
    assert peak_rms(pcm, SR, WMS) == pytest.approx(15000.0, rel=1e-3)


def test_27_peak_rms_does_not_mutate_its_input():
    src = np.linspace(-1, 1, WIN * 3 + 13).astype(np.float32)
    copy = src.copy()
    peak_rms(src, SR, WMS)
    assert np.array_equal(src, copy)


def test_28_peak_rms_is_stable_under_window_aligned_shifts():
    """Sliding a loud window later in the clip must not change the answer --
    as long as it stays window-aligned."""
    answers = []
    for lead in range(0, 5):
        pcm = np.zeros(WIN * 8, dtype=np.float32)
        pcm[WIN * lead:WIN * (lead + 1)] = 0.5
        answers.append(peak_rms(pcm, SR, WMS))
    assert max(answers) == pytest.approx(min(answers), rel=1e-6)


def test_29_peak_rms_quadruples_peak_memory():
    """DEFECT CLAIM (resource). blocks.astype(float64) and then np.square()
    each materialise a full double-width copy of the clip. Measured 4.00x at
    the app's own 1800s ceiling: 461MB transient for a 115MB recording, on a
    machine that is also holding a local STT model. A running max needs
    O(window) extra, not O(clip)."""
    pcm = np.zeros(SR * 400, dtype=np.float32)
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    peak_rms(pcm, SR, WMS)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ratio = (peak - base) / pcm.nbytes
    del pcm
    gc.collect()
    assert ratio < 2.0, f"peak_rms allocated {ratio:.2f}x the clip"


# ==========================================================================
# RingBuffer
# ==========================================================================

def test_30_write_of_an_empty_block_is_a_no_op():
    rb = RingBuffer(8)
    rb.write(blk(1.0, 3))
    rb.write(np.zeros(0, dtype=np.float32))
    assert np.array_equal(rb.read_all(), blk(1.0, 3))


def test_31_single_sample_writes_accumulate_in_order():
    rb = RingBuffer(4)
    for i in range(1, 8):
        rb.write(np.array([i], dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.array([4, 5, 6, 7], dtype=np.float32))


def test_32_zero_capacity_buffer_swallows_everything_without_crashing():
    rb = RingBuffer(0)
    rb.write(blk(1.0, 100))
    out = rb.read_all()
    assert out.size == 0 and out.dtype == np.float32
    rb.reset()


def test_33_block_exactly_capacity_replaces_the_contents_in_order():
    rb = RingBuffer(5)
    rb.write(blk(9.0, 3))
    rb.write(np.arange(1, 6, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(1, 6, dtype=np.float32))


def test_34_block_larger_than_capacity_keeps_the_newest_tail_in_order():
    rb = RingBuffer(5)
    rb.write(np.arange(1, 21, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(16, 21, dtype=np.float32))


def test_35_block_of_capacity_plus_one():
    rb = RingBuffer(5)
    rb.write(blk(7.0, 2))
    rb.write(np.arange(1, 7, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(2, 7, dtype=np.float32))


def test_36_exact_capacity_then_one_more_sample():
    """The off-by-one that ring buffers die on."""
    rb = RingBuffer(6)
    rb.write(np.arange(1, 7, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(1, 7, dtype=np.float32))
    rb.write(np.array([7], dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(2, 8, dtype=np.float32))


def test_37_block_of_capacity_minus_one_against_a_full_buffer():
    rb = RingBuffer(5)
    rb.write(np.arange(1, 6, dtype=np.float32))
    rb.write(np.arange(6, 10, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(5, 10, dtype=np.float32))


def test_38_repeated_wraps_never_lose_write_order():
    """40 wraps of a buffer whose capacity is coprime with the block size."""
    cap, step = 97, 13
    rb = RingBuffer(cap)
    seq = np.arange(1, 40 * cap, dtype=np.float32)
    for i in range(0, seq.size, step):
        rb.write(seq[i:i + step])
    out = rb.read_all()
    assert out.size == cap
    assert np.array_equal(out, seq[-cap:])
    assert np.all(np.diff(out) == 1)


def test_39_differential_against_a_deque_over_randomised_writes():
    """Strongest single check: 17 capacities x 120 randomised writes each,
    compared against collections.deque(maxlen=cap) after every write."""
    rng = np.random.default_rng(20260830)
    for cap in list(range(1, 12)) + [16, 17, 31, 64, 65, 100]:
        rb = RingBuffer(cap)
        history = []
        for _ in range(120):
            n = int(rng.integers(0, 2 * cap + 3))
            b = rng.standard_normal(n).astype(np.float32)
            history.append(b)
            rb.write(b)
            expected = model_ring(cap, history)
            got = rb.read_all()
            assert np.array_equal(got, expected), f"cap={cap} n={n}"


def test_40_interleaved_write_read_reset():
    rb = RingBuffer(6)
    rb.write(np.arange(1, 5, dtype=np.float32))
    assert rb.read_all().size == 4
    rb.reset()
    assert rb.read_all().size == 0
    rb.write(np.arange(10, 20, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(14, 20, dtype=np.float32))
    rb.reset()
    rb.write(np.array([1.0], dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.array([1.0], dtype=np.float32))


def test_41_reset_makes_stale_audio_unreachable():
    """reset() leaves the backing store dirty; it must never surface again."""
    rb = RingBuffer(8)
    rb.write(blk(0.5, 8))
    rb.reset()
    rb.write(blk(0.1, 3))
    out = rb.read_all()
    assert out.size == 3
    assert not np.any(out == np.float32(0.5))


def test_42_read_all_returns_a_snapshot_not_a_view():
    rb = RingBuffer(4)
    rb.write(blk(1.0, 4))
    out = rb.read_all()
    out[:] = 99.0
    assert np.array_equal(rb.read_all(), blk(1.0, 4))


def test_43_write_copies_the_callers_buffer_immediately():
    """PortAudio reuses `indata` the moment the callback returns. If write()
    kept a reference, every recording would be garbage."""
    rb = RingBuffer(16)
    scratch = blk(0.25, 8)
    rb.write(scratch)
    scratch[:] = -1.0                      # PortAudio recycles the block
    assert np.array_equal(rb.read_all(), blk(0.25, 8))


def test_44_write_does_not_mutate_the_callers_buffer():
    rb = RingBuffer(4)
    src = np.arange(1, 11, dtype=np.float32)
    copy = src.copy()
    rb.write(src)
    assert np.array_equal(src, copy)


def test_45_write_accepts_2d_and_non_contiguous_blocks():
    rb = RingBuffer(8)
    rb.write(np.array([[1.0, 9.0], [2.0, 9.0]], dtype=np.float32))
    assert np.array_equal(rb.read_all(),
                          np.array([1, 9, 2, 9], dtype=np.float32))
    rb.reset()
    stereo = np.zeros((4, 2), dtype=np.float32)
    stereo[:, 0] = [1, 2, 3, 4]
    left = stereo[:, 0]
    assert not left.flags["C_CONTIGUOUS"]
    rb.write(left)
    assert np.array_equal(rb.read_all(), np.array([1, 2, 3, 4], dtype=np.float32))


def test_46_write_coerces_dtypes_and_preserves_nan_and_inf():
    rb = RingBuffer(6)
    rb.write(np.array([1, 2, 3], dtype=np.int16))
    assert rb.read_all().dtype == np.float32
    rb.reset()
    rb.write(np.array([np.nan, np.inf, -np.inf], dtype=np.float64))
    out = rb.read_all()
    assert np.isnan(out[0]) and np.isposinf(out[1]) and np.isneginf(out[2])


def test_47_two_threads_writing_lose_no_samples():
    """Every block from both writers must survive whole; the buffer is large
    enough that nothing should be evicted."""
    cap = 400_000
    rb = RingBuffer(cap)
    per_thread, size = 200, 500

    def writer(value):
        b = blk(value, size)
        for _ in range(per_thread):
            rb.write(b)

    ts = [threading.Thread(target=writer, args=(v,)) for v in (1.0, 2.0)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
        assert not t.is_alive()
    out = rb.read_all()
    assert out.size == 2 * per_thread * size
    assert int(np.sum(out == 1.0)) == per_thread * size
    assert int(np.sum(out == 2.0)) == per_thread * size


def test_48_read_all_during_concurrent_writes_returns_a_contiguous_run():
    """A ring buffer read must be a snapshot of a contiguous stretch of the
    write stream -- never a torn block, never a gap, never a reordering."""
    cap = 4096
    rb = RingBuffer(cap)
    stop = threading.Event()
    n = [1]

    def writer():
        while not stop.is_set() and n[0] < 200_000:
            rb.write(np.arange(n[0], n[0] + 64, dtype=np.float32))
            n[0] += 64

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(400):
            out = rb.read_all()
            if out.size > 1:
                d = np.diff(out)
                assert np.all(d == 1), f"non-contiguous read: {np.unique(d)}"
    finally:
        stop.set()
        t.join(30)
        assert not t.is_alive()


def test_49_read_all_quadruples_peak_memory():
    """DEFECT CLAIM (resource). read_all() builds an int64 index array for
    every sample -- an arange, an add and a modulo, each twice the width of
    the audio itself -- then fancy-indexes and copies again. Measured 4.00x at
    the app's own 1800s ceiling: 461MB transient to hand back a 115MB
    recording, all of it inside the lock the audio callback needs. Two
    concatenated slices would be one copy."""
    cap = 16000 * 400
    rb = RingBuffer(cap)
    rb.write(np.zeros(cap + 5, dtype=np.float32))     # force a wrapped start
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    out = rb.read_all()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ratio = (peak - base) / out.nbytes
    del out, rb
    gc.collect()
    assert ratio < 2.0, f"read_all allocated {ratio:.2f}x the result"


# ==========================================================================
# Recorder
# ==========================================================================

def make_rec(**kw):
    kw.setdefault("sample_rate", 1000)
    kw.setdefault("max_seconds", 10)
    return Recorder(**kw)


def test_50_open_is_idempotent_and_close_is_safe_to_repeat():
    rec = make_rec()
    assert rec.running is False
    rec.open()
    first = rec._stream
    rec.open()
    assert rec._stream is first and rec.running is True
    rec.close()
    assert rec.running is False and first.closed is True
    rec.close()                       # must not raise on an already-closed rec
    rec.open()
    assert rec.running is True
    rec.close()


def test_51_close_still_clears_the_stream_when_the_device_errors():
    rec = make_rec()
    rec.open()

    def boom():
        raise RuntimeError("device gone")

    rec._stream.stop = boom
    rec.close()
    assert rec.running is False and rec.capturing is False


def test_52_capture_records_blocks_in_arrival_order():
    rec = make_rec(preroll_ms=0)
    rec.begin()
    deliver(rec, np.arange(1, 11, dtype=np.float32))
    deliver(rec, np.arange(11, 21, dtype=np.float32))
    out = rec.end()
    assert np.array_equal(out, np.arange(1, 21, dtype=np.float32))
    assert rec.capturing is False


def test_53_preroll_seeds_the_recording_with_audio_from_before_the_chord():
    rec = make_rec(preroll_ms=400)          # 400 samples at 1kHz
    deliver(rec, blk(0.11, 400))            # idle: fills the pre-roll
    rec.begin()
    deliver(rec, blk(0.22, 100))
    out = rec.end()
    assert out.size == 500
    assert np.all(out[:400] == np.float32(0.11))


def test_54_the_previous_sessions_preroll_is_replayed_into_the_next_one():
    """DEFECT CLAIM. begin() reads the pre-roll but never drains it, and the
    callback stops feeding the pre-roll for the whole time capture is on. So
    the pre-roll still holds the same 400ms after the session ends, and the
    next begin() prepends it a second time. Audio recorded before session 1
    is transcribed again at the head of session 2 -- with no idle audio in
    between, every sample of session 2's pre-roll is stale."""
    rec = make_rec(preroll_ms=400)
    deliver(rec, blk(0.11, 400))            # idle audio before session 1
    rec.begin()
    deliver(rec, blk(0.22, 1000))
    first = rec.end()
    assert np.any(first == np.float32(0.11))    # correctly used once

    rec.begin()                              # second session, no idle gap
    deliver(rec, blk(0.33, 1000))
    second = rec.end()
    assert not np.any(second == np.float32(0.11)), (
        f"{int(np.sum(second == np.float32(0.11)))} stale samples replayed "
        f"into the next session")


def test_55_stale_preroll_is_unbounded_in_age():
    """DEFECT CLAIM, second face. The staleness is not a millisecond race:
    the pre-roll is frozen for the entire length of the recording, so a long
    dictation followed by a prompt restart prepends audio that is minutes
    old."""
    rec = Recorder(sample_rate=1000, max_seconds=600, preroll_ms=400)
    deliver(rec, blk(0.11, 400))
    rec.begin()
    for _ in range(300):                     # 300 seconds of capture
        deliver(rec, blk(0.22, 1000))
    rec.end()
    rec.begin()
    deliver(rec, blk(0.33, 100))
    second = rec.end()
    assert not np.any(second == np.float32(0.11)), (
        "audio from 300s ago prepended to the new session")


def test_56_mute_for_never_gates_an_active_recording():
    """The module's loudest invariant: a cue must never cost a syllable."""
    rec = make_rec(preroll_ms=400)
    rec.begin()
    rec.mute_for(5000)
    deliver(rec, blk(0.42, 500))
    out = rec.end()
    assert out.size == 500 and np.all(out == np.float32(0.42))


def test_57_mute_for_keeps_idle_audio_out_of_the_preroll():
    rec = make_rec(preroll_ms=400)
    rec.mute_for(5000)
    deliver(rec, blk(0.9, 400))              # the cue
    rec.begin()
    out = rec.end()
    assert out.size == 0


def test_58_mute_expires_and_the_preroll_refills():
    rec = make_rec(preroll_ms=400)
    rec.mute_for(30)
    deliver(rec, blk(0.9, 400))              # dropped
    time.sleep(0.06)
    deliver(rec, blk(0.4, 400))              # admitted
    rec.begin()
    out = rec.end()
    assert out.size == 400 and np.all(out == np.float32(0.4))


def test_59_mute_is_decided_once_per_block_not_per_sample():
    """A block that starts while muted is dropped whole, even the part of it
    that arrived after expiry. Bounded by one blocksize (~16.7ms at the
    shipped sample_rate/60), so recorded rather than claimed as a bug."""
    rec = make_rec(preroll_ms=1000)
    rec.mute_for(20)
    deliver(rec, blk(0.5, 400))              # spans the expiry
    time.sleep(0.05)
    assert rec.preroll.read_all().size == 0


def test_60_mute_for_zero_and_negative_do_not_mute():
    rec = make_rec(preroll_ms=400)
    rec.mute_for(0)
    deliver(rec, blk(0.3, 100))
    rec.mute_for(-1000)
    deliver(rec, blk(0.3, 100))
    assert rec.preroll.read_all().size == 200


def test_61_a_shorter_mute_cancels_a_longer_one_already_running():
    """Recorded, not claimed as a defect: mute_for() is an assignment, not a
    max. A second cue shorter than the first un-mutes early, which would leak
    the tail of the first cue into the pre-roll. Whether two cues can ever
    overlap is decided outside this module."""
    rec = make_rec(preroll_ms=400)
    rec.mute_for(5000)
    rec.mute_for(0)
    deliver(rec, blk(0.7, 100))
    assert rec.preroll.read_all().size == 100


def test_62_idle_audio_never_reaches_the_capture_buffer():
    rec = make_rec(preroll_ms=400)
    deliver(rec, blk(0.5, 100))
    assert rec.buffer.read_all().size == 0
    assert rec.preroll.read_all().size == 100


def test_63_on_level_is_silent_while_idle():
    seen = []
    rec = make_rec(preroll_ms=400, on_level=seen.append)
    deliver(rec, blk(0.5, 100))
    assert seen == []
    rec.begin()
    deliver(rec, blk(0.5, 100))
    assert seen == [pytest.approx(0.5)]


def test_64_an_exploding_on_level_never_costs_the_user_audio():
    def boom(_level):
        raise ValueError("meter blew up")

    rec = make_rec(preroll_ms=0, on_level=boom)
    rec.begin()
    deliver(rec, blk(0.5, 100))
    deliver(rec, blk(0.6, 100))
    out = rec.end()
    assert out.size == 200


def test_65_on_level_runs_outside_the_ring_buffer_lock():
    """threading.Lock is not reentrant. If write() still held it when
    on_level fired, any meter that reads the buffer would deadlock the audio
    thread forever. Run in a worker so a regression times out instead of
    hanging the suite."""
    rec = make_rec(preroll_ms=0)
    rec.on_level = lambda _lvl: rec.buffer.read_all()
    rec.begin()
    done = threading.Event()

    def run():
        deliver(rec, blk(0.5, 100))
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(5), "on_level deadlocked against the ring buffer lock"


def test_66_callback_handles_2d_input_by_taking_channel_zero():
    rec = make_rec(preroll_ms=0)
    rec.begin()
    stereo = np.zeros((50, 2), dtype=np.float32)
    stereo[:, 0] = 0.4
    stereo[:, 1] = -0.9
    rec._callback(stereo, 50, None, None)
    out = rec.end()
    assert out.size == 50 and np.all(out == np.float32(0.4))


def test_67_callback_survives_a_zero_frame_block():
    """Some host APIs deliver an empty callback. It must not crash, and it
    must not put anything in the buffer."""
    seen = []
    rec = make_rec(preroll_ms=400, on_level=seen.append)
    rec.begin()
    rec._callback(np.zeros((0, 1), dtype=np.float32), 0, None, None)
    assert rec.end().size == 0
    assert seen == [0.0]                    # a spurious meter dip to zero


def test_68_callback_survives_a_truthy_status_flag():
    class Overflow:
        def __bool__(self):
            return True

        def __str__(self):
            return "input overflow"

    rec = make_rec(preroll_ms=0)
    rec.begin()
    rec._callback(blk(0.2, 50), 50, None, Overflow())
    assert rec.end().size == 50


def test_69_begin_discards_an_in_progress_recording():
    rec = make_rec(preroll_ms=0)
    rec.begin()
    deliver(rec, blk(0.5, 100))
    rec.begin()                              # re-entrant start
    deliver(rec, blk(0.6, 50))
    out = rec.end()
    assert out.size == 50 and np.all(out == np.float32(0.6))


def test_70_end_called_twice_returns_the_same_audio():
    """end() does not drain. A double-release must not be able to inject the
    same words twice -- recorded so a caller change cannot do it silently."""
    rec = make_rec(preroll_ms=0)
    rec.begin()
    deliver(rec, blk(0.5, 100))
    assert np.array_equal(rec.end(), rec.end())


def test_71_a_block_arriving_after_end_is_not_in_the_returned_audio():
    rec = make_rec(preroll_ms=0)
    rec.begin()
    deliver(rec, blk(0.5, 100))
    out = rec.end()
    deliver(rec, blk(0.9, 100))              # late callback, capture is off
    assert out.size == 100 and not np.any(out == np.float32(0.9))


def test_72_capture_longer_than_max_seconds_keeps_the_newest_audio():
    rec = Recorder(sample_rate=100, max_seconds=1, preroll_ms=0)
    rec.begin()
    deliver(rec, np.arange(1, 151, dtype=np.float32))
    out = rec.end()
    assert out.size == 100
    assert np.array_equal(out, np.arange(51, 151, dtype=np.float32))


def test_73_preroll_ms_zero_disables_the_preroll_without_crashing():
    rec = make_rec(preroll_ms=0)
    assert rec.preroll._cap == 0
    deliver(rec, blk(0.5, 100))
    rec.begin()
    assert rec.end().size == 0


def test_74_default_geometry_matches_the_documented_constants():
    rec = Recorder(sample_rate=16000)
    assert rec.preroll._cap == int(16000 * DEFAULT_PREROLL_MS / 1000)
    assert rec.buffer._cap == 16000 * 1800


def test_75_a_float_sample_rate_breaks_construction():
    """Recorded, not claimed as a defect. The pre-roll capacity is built with
    an explicit int(), the capture buffer's is not -- so a sample rate that
    arrives as 16000.0 dies inside numpy rather than being coerced."""
    with pytest.raises(TypeError):
        Recorder(sample_rate=16000.0, max_seconds=1)


def test_76_end_on_a_full_buffer_is_not_instant():
    """DEFECT CLAIM (latency). The module docstring's stated reason for
    keeping the stream open is "Keeping the stream open also makes end()
    instant." end() is read_all(), which is O(n) with 4x-width index
    arithmetic and runs under the lock the audio callback needs. On a full
    1800s buffer that is a quarter of a second of dead UI on chord release."""
    rec = Recorder(sample_rate=16000, max_seconds=1800, preroll_ms=0)
    rec._capturing = True
    rec.buffer.write(np.zeros(rec.buffer._cap + 5, dtype=np.float32))
    t0 = time.perf_counter()
    out = rec.end()
    elapsed = time.perf_counter() - t0
    del out
    gc.collect()
    assert elapsed < 0.05, f"end() took {elapsed * 1000:.0f}ms"
