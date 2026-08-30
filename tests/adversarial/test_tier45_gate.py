"""Tier 4/5 adversarial gate.

Attacks the seven claims made about the ring buffer rewrite, the chunked
loudness scan, the sanitised term list, the continuous-sound silence detector,
the bar model's gain decay, the overlay's repaint timer, and history search.

Hard rules honoured here:
  * No real audio device is ever opened. `Recorder.open()` / `begin()` are never
    called; the callback is driven directly and `RingBuffer` / `peak_rms` are
    exercised in isolation.
  * Nothing is written outside pytest's `tmp_path`.
"""

import collections
import math
import random
import sqlite3
import threading

import numpy as np
import pytest

from murmur.audio import Recorder, RingBuffer, peak_rms, rms
from murmur.history import History
from murmur.ui.waveform import (_PEAK_DECAY_S, _PEAK_MAX, _PEAK_MIN,
                                BarModel)
from murmur.vad import SilenceMonitor
from murmur.vocabulary import GLOSSARY_LIMIT, HOTWORD_LIMIT, Vocabulary

# ---------------------------------------------------------------------------
# 1. RingBuffer.read_all -- the rewrite that must return identical samples
# ---------------------------------------------------------------------------


def _ref_state(cap, blocks):
    """collections.deque(maxlen=cap) is the reference semantic."""
    d = collections.deque(maxlen=cap)
    for b in blocks:
        d.extend(b)
    return np.array(d, dtype=np.float32)


def _run_and_compare(cap, sizes):
    """Write `sizes` blocks of unique ascending samples, checking after EVERY
    write that read_all() equals the deque reference exactly."""
    rb = RingBuffer(cap)
    ref = collections.deque(maxlen=cap) if cap else collections.deque(maxlen=1)
    written = []
    nxt = 1.0
    for k, n in enumerate(sizes):
        block = np.arange(nxt, nxt + n, dtype=np.float32)
        nxt += n
        rb.write(block)
        if cap:
            ref.extend(block.tolist())
            expect = np.array(ref, dtype=np.float32)
        else:
            expect = np.zeros(0, dtype=np.float32)
        got = rb.read_all()
        written.append(n)
        assert got.dtype == np.float32, f"dtype drifted after writes {written}"
        assert got.shape == expect.shape, (
            f"cap={cap} after writes {written}: length {got.size} != {expect.size}")
        assert np.array_equal(got, expect), (
            f"cap={cap} after writes {written} (write #{k}, size {n}):\n"
            f"  got    {got.tolist()[:12]}...{got.tolist()[-6:]}\n"
            f"  expect {expect.tolist()[:12]}...{expect.tolist()[-6:]}")


_CAPS = [1, 2, 3, 4, 5, 7, 8, 13, 16, 17, 64, 100]


@pytest.mark.parametrize("cap", _CAPS)
def test_ringbuffer_matches_deque_for_every_structural_blocksize(cap):
    """0, 1, cap-1, cap, cap+1, 2*cap+2 -- in every order, checked after each."""
    sizes = [0, 1, max(1, cap - 1), cap, cap + 1, 2 * cap + 2, 1, 0,
             max(1, cap // 2), cap, 3, cap + 1, max(1, cap - 1), 2 * cap + 2]
    _run_and_compare(cap, sizes)


@pytest.mark.parametrize("seed", range(40))
def test_ringbuffer_matches_deque_under_randomised_sequences(seed):
    rng = random.Random(seed)
    cap = rng.randint(1, 40)
    sizes = [rng.choice([0, 1, 2, cap - 1, cap, cap + 1, 2 * cap + 2,
                         rng.randint(0, 3 * cap)]) for _ in range(40)]
    sizes = [max(0, s) for s in sizes]
    _run_and_compare(cap, sizes)


@pytest.mark.parametrize("cap", [1, 2, 5, 8, 16, 64])
def test_ringbuffer_walks_every_start_offset(cap):
    """Advance the write head one sample at a time so `_start` visits every
    residue mod cap, reading the whole buffer at each offset."""
    rb = RingBuffer(cap)
    ref = collections.deque(maxlen=cap)
    for i in range(4 * cap + 3):
        rb.write(np.array([float(i)], dtype=np.float32))
        ref.append(float(i))
        got = rb.read_all()
        assert np.array_equal(got, np.array(ref, dtype=np.float32)), (
            f"cap={cap} start-offset walk failed at sample {i}: "
            f"got {got.tolist()} expect {list(ref)}")


@pytest.mark.parametrize("cap", [2, 3, 8, 16, 64])
def test_ringbuffer_exact_wrap_boundary(cap):
    """Fill to exactly capacity (end == cap, no wrap), then cross by one."""
    rb = RingBuffer(cap)
    rb.write(np.arange(1, cap + 1, dtype=np.float32))
    got = rb.read_all()
    assert np.array_equal(got, np.arange(1, cap + 1, dtype=np.float32)), (
        f"exactly-full buffer wrong: {got.tolist()}")
    rb.write(np.array([999.0], dtype=np.float32))
    expect = np.append(np.arange(2, cap + 1, dtype=np.float32), 999.0)
    assert np.array_equal(rb.read_all(), expect), (
        f"first wrapped sample wrong: {rb.read_all().tolist()} != {expect.tolist()}")


def test_ringbuffer_giant_write_keeps_the_newest_tail():
    rb = RingBuffer(10)
    rb.write(np.arange(1, 8, dtype=np.float32))
    rb.write(np.arange(100, 135, dtype=np.float32))       # 35 >> cap
    assert np.array_equal(rb.read_all(),
                          np.arange(125, 135, dtype=np.float32))


def test_ringbuffer_write_of_exactly_capacity_after_partial():
    rb = RingBuffer(6)
    rb.write(np.arange(1, 5, dtype=np.float32))           # start moves to 4
    rb.write(np.arange(50, 56, dtype=np.float32))         # size == cap
    assert np.array_equal(rb.read_all(),
                          np.arange(50, 56, dtype=np.float32))


def test_ringbuffer_zero_capacity_is_inert():
    rb = RingBuffer(0)
    rb.write(np.arange(5, dtype=np.float32))
    out = rb.read_all()
    assert out.size == 0 and out.dtype == np.float32


def test_ringbuffer_read_all_is_a_detached_copy_on_both_paths():
    # non-wrapping path
    rb = RingBuffer(8)
    rb.write(np.arange(1, 5, dtype=np.float32))
    out = rb.read_all()
    out[:] = -1.0
    assert np.array_equal(rb.read_all(), np.arange(1, 5, dtype=np.float32)), (
        "non-wrapping read_all aliases the internal buffer")
    # wrapping path
    rb = RingBuffer(8)
    rb.write(np.arange(1, 13, dtype=np.float32))
    out = rb.read_all()
    out[:] = -1.0
    assert np.array_equal(rb.read_all(), np.arange(5, 13, dtype=np.float32)), (
        "wrapping read_all aliases the internal buffer")


def test_ringbuffer_repeated_reads_are_identical():
    rb = RingBuffer(9)
    rb.write(np.arange(1, 15, dtype=np.float32))
    a, b, c = rb.read_all(), rb.read_all(), rb.read_all()
    assert np.array_equal(a, b) and np.array_equal(b, c)


def test_ringbuffer_reset_then_reuse():
    rb = RingBuffer(6)
    rb.write(np.arange(1, 10, dtype=np.float32))          # wrapped
    rb.reset()
    assert rb.read_all().size == 0
    rb.write(np.arange(70, 74, dtype=np.float32))
    assert np.array_equal(rb.read_all(), np.arange(70, 74, dtype=np.float32))


def test_ringbuffer_accepts_2d_and_non_contiguous_input():
    rb = RingBuffer(6)
    rb.write(np.arange(1, 5, dtype=np.float32).reshape(4, 1))
    rb.write(np.arange(10, 20, dtype=np.float32)[::3])    # 10,13,16,19
    assert np.array_equal(rb.read_all(),
                          np.array([3, 4, 10, 13, 16, 19], dtype=np.float32))


def test_ringbuffer_read_concurrent_with_writes_never_tears():
    """A read taken while the audio thread is writing must still be a
    contiguous, in-order run -- never a torn or shuffled one."""
    cap = 256
    rb = RingBuffer(cap)
    stop = threading.Event()
    errors = []

    def writer():
        nxt = 1.0
        rng = random.Random(7)
        try:
            for _ in range(4000):
                n = rng.choice([1, 3, 16, 64, 255, 256, 257, 600])
                rb.write(np.arange(nxt, nxt + n, dtype=np.float32))
                nxt += n
        except Exception as exc:                      # pragma: no cover
            errors.append(exc)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                out = rb.read_all()
                if out.size:
                    assert out.size <= cap, f"read {out.size} > capacity {cap}"
                    d = np.diff(out.astype(np.float64))
                    assert np.all(d == 1.0), (
                        "read_all returned a non-contiguous run during a "
                        f"concurrent write: {out[:8].tolist()} ... "
                        f"{out[-8:].tolist()}")
        except Exception as exc:
            errors.append(exc)

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_r.start()
    t_w.start()
    t_w.join(30)
    stop.set()
    t_r.join(10)
    assert not errors, errors[0]


def test_recorder_callback_to_end_roundtrip_wraps_correctly():
    """The real capture path (callback -> ring buffer -> end()) with no device.

    open()/begin() are never called: `_capturing` is set directly.
    """
    rec = Recorder(sample_rate=100, max_seconds=1, preroll_ms=100)
    rec._capturing = True
    nxt = 1.0
    for _ in range(30):                      # 30 * 8 = 240 samples into cap 100
        block = np.arange(nxt, nxt + 8, dtype=np.float32).reshape(8, 1)
        rec._callback(block, 8, None, None)
        nxt += 8
    out = rec.end()
    assert np.array_equal(out, np.arange(141, 241, dtype=np.float32)), (
        f"capture path lost/ reordered samples: {out[:5].tolist()} .. "
        f"{out[-5:].tolist()}")
    assert rec.capturing is False


# ---------------------------------------------------------------------------
# 2. peak_rms -- the chunked scan
# ---------------------------------------------------------------------------


def _ref_peak_rms(pcm, sample_rate=16000, window_ms=400):
    """Naive one-window-at-a-time reference, cleaning non-finite samples."""
    x = np.asarray(pcm, dtype=np.float32).ravel()
    if x.size == 0:
        return 0.0
    win = max(1, int(sample_rate * window_ms / 1000))
    if x.size <= win:
        return rms(x)
    n = x.size // win
    best = 0.0
    for k in range(n):
        w = np.nan_to_num(x[k * win:(k + 1) * win].astype(np.float64),
                          nan=0.0, posinf=0.0, neginf=0.0)
        best = max(best, float(np.sqrt(np.square(w).mean())))
    tail = x[n * win:]
    if tail.size:
        p = np.zeros(win, dtype=np.float64)
        p[:tail.size] = np.nan_to_num(tail.astype(np.float64),
                                      nan=0.0, posinf=0.0, neginf=0.0)
        best = max(best, float(np.sqrt(np.square(p).mean())))
    return best


_SR, _WMS = 100, 100          # win = 10 samples, so 64 windows = 640 samples


@pytest.mark.parametrize("size", [
    1, 5, 9, 10, 11, 19, 20, 21, 99, 100, 101,
    639, 640, 641, 649, 650, 651, 1279, 1280, 1281, 1283, 2000, 2561,
])
def test_peak_rms_matches_naive_reference(size):
    rng = np.random.default_rng(size)
    pcm = (rng.standard_normal(size) * 0.05).astype(np.float32)
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    want = _ref_peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert got == pytest.approx(want, rel=1e-9, abs=1e-12), (
        f"size={size}: chunked scan {got} != reference {want}")


@pytest.mark.parametrize("window_index", [0, 1, 62, 63, 64, 65, 126, 127, 128])
def test_peak_rms_finds_a_peak_in_any_window_including_chunk_edges(window_index):
    """_SCAN_WINDOWS is 64, so windows 63/64 straddle the chunk boundary."""
    win = 10
    total = 130 * win + 3
    pcm = np.full(total, 0.001, dtype=np.float32)
    pcm[window_index * win:(window_index + 1) * win] = 0.5
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert got == pytest.approx(0.5, rel=1e-6), (
        f"peak in window {window_index} came back as {got}")


def test_peak_rms_finds_a_peak_in_the_last_partial_chunk():
    win = 10
    pcm = np.full(70 * win, 0.001, dtype=np.float32)     # chunks: 64 + 6
    pcm[69 * win:70 * win] = 0.4
    assert peak_rms(pcm, sample_rate=_SR, window_ms=_WMS) == pytest.approx(0.4, rel=1e-6)


def test_peak_rms_finds_a_peak_in_the_zero_padded_tail():
    win = 10
    pcm = np.full(70 * win + 4, 0.0, dtype=np.float32)
    pcm[70 * win:] = 1.0                                  # 4 samples of full scale
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert got == pytest.approx(math.sqrt(4 / win), rel=1e-6), got


def test_peak_rms_clip_shorter_than_one_window():
    pcm = np.full(7, 0.3, dtype=np.float32)
    assert peak_rms(pcm, sample_rate=_SR, window_ms=_WMS) == pytest.approx(0.3, rel=1e-6)


def test_peak_rms_exact_multiple_of_window_and_of_chunk():
    win = 10
    for windows in (64, 128, 192):
        pcm = np.full(windows * win, 0.02, dtype=np.float32)
        pcm[(windows - 1) * win] = 0.9                    # last window, first sample
        got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
        want = _ref_peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
        assert got == pytest.approx(want, rel=1e-9), f"{windows} windows: {got} != {want}"


def test_peak_rms_empty_clip_is_silence():
    assert peak_rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_peak_rms_at_real_settings_across_a_chunk_boundary():
    """25.6s is one chunk at 16kHz/400ms. Put the only speech just past it."""
    sr, win = 16000, 6400
    pcm = np.full(70 * win, 0.0005, dtype=np.float32)     # 28s of room tone
    pcm[64 * win:65 * win] = 0.25                          # first window of chunk 2
    got = peak_rms(pcm, sample_rate=sr)
    assert got == pytest.approx(0.25, rel=1e-5), got


def test_peak_rms_accumulates_at_float64_not_float32():
    """One loud sample plus 6399 tiny ones: a float32 accumulator loses the
    tail of that sum, a float64 one keeps it."""
    sr, win = 16000, 6400
    pcm = np.full(2 * win, 1e-4, dtype=np.float32)
    pcm[win // 2] = 1.0
    got = peak_rms(pcm, sample_rate=sr)
    want = _ref_peak_rms(pcm, sample_rate=sr)
    assert got == pytest.approx(want, rel=1e-12), (
        f"chunked scan {got!r} differs from a float64 reference {want!r}")


def test_peak_rms_does_not_overflow_on_out_of_range_samples():
    """float32 squares overflow above ~1.8e19; the float64 accumulation the
    docstring claims must survive it."""
    sr, win = 16000, 6400
    pcm = np.full(2 * win, 1e20, dtype=np.float32)
    got = peak_rms(pcm, sample_rate=sr)
    assert np.isfinite(got) and got == pytest.approx(1e20, rel=1e-6), got


def test_peak_rms_ignores_a_nan_burst_but_still_hears_the_speech():
    win = 10
    pcm = np.full(70 * win, 0.001, dtype=np.float32)
    pcm[5 * win:5 * win + 3] = np.nan                     # device fault
    pcm[30 * win:31 * win] = 0.3                          # real speech
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert np.isfinite(got), f"NaN leaked into the verdict: {got}"
    assert got == pytest.approx(0.3, rel=1e-6), got


def test_peak_rms_ignores_an_inf_burst_but_still_hears_the_speech():
    win = 10
    pcm = np.full(70 * win, 0.001, dtype=np.float32)
    pcm[5 * win + 2] = np.inf
    pcm[6 * win + 1] = -np.inf
    pcm[30 * win:31 * win] = 0.3
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert np.isfinite(got), f"Inf leaked into the verdict: {got}"
    assert got == pytest.approx(0.3, rel=1e-6), got


def test_peak_rms_all_nan_long_clip_reads_as_silence():
    pcm = np.full(70 * 10, np.nan, dtype=np.float32)
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert got == 0.0, f"all-NaN clip returned {got}"


def test_peak_rms_all_nan_short_clip_reads_as_silence():
    """A clip at or under one window skips every non-finite guard."""
    pcm = np.full(10, np.nan, dtype=np.float32)           # exactly one window
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert not np.isnan(got), (
        "peak_rms returned NaN for a short all-NaN clip; NaN compares False "
        "against every threshold, which is the exact failure the module says "
        "nan_to_num exists to prevent")
    assert got == 0.0, got


def test_peak_rms_short_speech_clip_with_one_nan_still_reads_as_speech():
    """400ms of full-scale speech plus one faulty sample must not vanish."""
    pcm = np.full(10, 0.4, dtype=np.float32)              # size == win
    pcm[4] = np.nan
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert not np.isnan(got), (
        "one NaN sample turned a clip of full-scale speech into NaN on the "
        "short-clip path")
    assert got > 0.1, got


def test_peak_rms_inf_in_the_tail_stays_finite():
    """The chunk path zeroes Inf; the tail path must not report infinity."""
    win = 10
    pcm = np.full(70 * win + 4, 0.001, dtype=np.float32)
    pcm[70 * win + 1] = np.inf
    got = peak_rms(pcm, sample_rate=_SR, window_ms=_WMS)
    assert np.isfinite(got), (
        f"an Inf sample in the final partial window returned {got}; the same "
        "sample one window earlier is cleaned to 0.0")


# ---------------------------------------------------------------------------
# 3. The learned term list -- sanitising and the count cap
# ---------------------------------------------------------------------------


@pytest.fixture()
def vocab(tmp_path):
    v = Vocabulary(tmp_path / "vocab.db")
    yield v
    v.close()


def _stock(v, n, prefix="misheard phrase number"):
    """n promoted one-off corrections whose wrong forms are long."""
    for i in range(n):
        v.observe(f"{prefix} {i:03d} spoken", f"Correction {i:03d}",
                  source="manual")


def test_hotword_cap_keeps_the_term_the_user_confirmed_most(vocab):
    """The cap must drop the least-confirmed terms, not the shortest ones."""
    for _ in range(9):                       # nine sightings: his most-used term
        vocab.observe("murmer", "Murmur", source="auto")
    _stock(vocab, 80)                        # eighty one-off long mishearings
    hot = vocab.hotwords()
    assert len(hot) == HOTWORD_LIMIT
    assert "Murmur" in hot, (
        "the 9-hit term the user relies on was dropped by the cap while 64 "
        "one-hit corrections survived, because the list is ordered by "
        "LENGTH(wrong_form) DESC, not by hit count as hotwords() documents. "
        f"Kept instead: {hot[:3]}")


def test_hotwords_are_ordered_by_hit_count_as_documented(vocab):
    vocab.observe("aye eye", "AI", source="manual")
    for _ in range(6):
        vocab.observe("aye eye", "AI", source="auto")
    vocab.observe("a much longer misheard phrase", "Short", source="manual")
    hot = vocab.hotwords()
    assert hot[0] == "AI", (
        "hotwords() promises 'Highest hit count first' but returned "
        f"{hot} -- a 1-hit term outranks a 7-hit one purely on wrong-form length")


def test_glossary_cap_keeps_the_term_the_user_confirmed_most(vocab):
    for _ in range(9):
        vocab.observe("halvorsen", "Halvorsen", source="auto")
    _stock(vocab, 60)
    gloss = vocab.glossary()
    assert len(gloss) == GLOSSARY_LIMIT
    assert "Halvorsen" in gloss, (
        f"the most-confirmed term is missing from the polish glossary: {gloss[:3]}")


def test_hotwords_are_capped_in_count(vocab):
    _stock(vocab, 200)
    assert len(vocab.hotwords()) == HOTWORD_LIMIT
    assert len(vocab.glossary()) == GLOSSARY_LIMIT


def test_hotwords_carry_no_control_characters_or_newlines(vocab):
    vocab.observe("acme", "Acme\nCorp", source="manual")
    vocab.observe("beta", "Be\x07ta\x1b[31m", source="manual")
    vocab.observe("gamma", "Ga\r\nmma", source="manual")
    for term in vocab.hotwords():
        assert not any(ord(c) < 32 or ord(c) == 127 for c in term), repr(term)
        assert "\n" not in term and "\r" not in term, repr(term)


def test_hotwords_are_length_bounded(vocab):
    vocab.observe("longone", "L" + "o" * 400 + "ng", source="manual")
    assert all(len(t) <= 60 for t in vocab.hotwords())


def test_hotwords_deduplicate_terms(vocab):
    vocab.observe("wun", "one", source="manual")
    vocab.observe("won", "one", source="manual")
    assert vocab.hotwords().count("one") == 1


@pytest.mark.parametrize("term", [
    "caf\u00e9", "Se\u00e1n O'Brien", "co-op", "C++", "\u65e5\u672c\u8a9e",
    "Bj\u00f6rk", "n8n", "GHL", "Landslide Studios", "re\u0301sume\u0301",
    "\u00c5ngstr\u00f6m", "x86-64", ".NET", "3.5\"",
])
def test_sanitising_leaves_a_legitimate_term_untouched(term):
    assert Vocabulary._clean_term(term) == term, (
        f"legitimate term mangled: {term!r} -> {Vocabulary._clean_term(term)!r}")


def test_sanitising_turns_a_newline_into_a_space_not_a_glue_point():
    """The function maps \\r\\n\\t to ' ', but the filter drops them first, so
    two words are welded into one token."""
    assert Vocabulary._clean_term("Vantage\nLabs") == "Vantage Labs", (
        "a newline inside a term joins the words instead of separating them: "
        f"{Vocabulary._clean_term('Vantage\nLabs')!r}")
    assert Vocabulary._clean_term("Vantage\tLabs") == "Vantage Labs"


def test_substitution_consumer_also_gets_a_sanitised_term(vocab):
    """`apply()` is the third consumer of the learned list and reads the same
    source; an unsanitised term goes straight into the user's pasted text."""
    dirty = "Acme\nCorp\x07 " + "z" * 90
    vocab.observe("acme", dirty, source="manual")
    hot = vocab.hotwords()
    assert hot and "\n" not in hot[0], "precondition: hotwords() sanitises"
    out = vocab.apply("we work with acme today")
    assert "\n" not in out and "\x07" not in out and len(out) < 120, (
        "apply() spliced the raw term into the transcript while hotwords() "
        f"sanitised the same term: {out!r}")


# ---------------------------------------------------------------------------
# 4. SilenceMonitor -- can a real person be cut off?
# ---------------------------------------------------------------------------

_DT = 1 / 60.0            # blocksize is sample_rate/60, so one frame is 16.7ms
_LOUD = 0.03              # a speaking voice on his mic (floor 0.00086)
_QUIET = 0.0005           # ambient


def _talk(vad, loud_s, quiet_s, minutes=30.0):
    """Repeat a (sound, silence) pattern. Returns the wall-clock second the
    monitor called for an auto-stop, or None if it never did."""
    t = 0.0
    limit = minutes * 60.0
    while t < limit:
        for _ in range(int(round(loud_s / _DT))):
            if vad.feed(_LOUD, _DT):
                return t
            t += _DT
        for _ in range(int(round(quiet_s / _DT))):
            if vad.feed(_QUIET, _DT):
                return t
            t += _DT
    return None


def test_unbroken_speech_is_never_cut_off():
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    assert _talk(vad, loud_s=4.0, quiet_s=0.5) is None


def test_a_speaker_pausing_between_sentences_is_never_cut_off():
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    assert _talk(vad, loud_s=3.0, quiet_s=2.0) is None


def test_reading_a_list_aloud_is_never_cut_off():
    """Single words with pauses: each word is 250ms of voiced energy -- under
    the 300ms the detector now demands before it believes the session is live."""
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    stopped = _talk(vad, loud_s=0.25, quiet_s=0.70)
    assert stopped is None, (
        f"auto-stop fired {stopped:.0f}s into an active dictation. The user is "
        "speaking a word every 950ms; because no single word sustains 300ms of "
        "continuous energy, silent_for is never reset and accumulates the gaps.")


def test_a_slow_deliberate_speaker_is_never_cut_off():
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    stopped = _talk(vad, loud_s=0.28, quiet_s=0.25)
    assert stopped is None, (
        f"auto-stop fired {stopped:.0f}s into continuous dictation with only "
        "250ms between phrases")


def test_quiet_trailing_syllables_do_not_end_the_session():
    """Talking 80% of the time, with 50ms stop-consonant closures."""
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    stopped = _talk(vad, loud_s=0.20, quiet_s=0.05)
    assert stopped is None, (
        f"auto-stop fired {stopped:.0f}s into a dictation that was voiced 80% "
        "of the time")


def test_a_forgotten_session_in_a_silent_room_stops_on_time():
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    t = 0.0
    for _ in range(int(200 / _DT)):
        if vad.feed(_QUIET, _DT):
            break
        t += _DT
    assert 89.0 <= t <= 91.0, t


def test_a_single_frame_of_noise_does_not_revive_a_forgotten_session():
    """The intended fix: one loud block every 89s must no longer reset."""
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    stopped = _talk(vad, loud_s=_DT, quiet_s=89.0, minutes=10.0)
    assert stopped is not None and stopped < 200.0, stopped


def test_a_forgotten_session_cannot_be_kept_alive_by_room_noise():
    """A third of a second of noise per 90s window -- a door, a passing car, an
    HVAC compressor -- is 0.35% duty cycle."""
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    stopped = _talk(vad, loud_s=0.35, quiet_s=89.0, minutes=60.0)
    assert stopped is not None, (
        "a session left running was still recording after an hour; 350ms of "
        "sound every 89s is enough to keep it alive indefinitely")


def test_silence_counter_is_continuous_not_cumulative():
    """The module's stated contract: 'CONTINUOUS silence since the last frame
    above the speech threshold'."""
    vad = SilenceMonitor(threshold=0.004, stop_after_s=90.0)
    for _ in range(int(60.0 / _DT)):          # 60s of silence
        vad.feed(_QUIET, _DT)
    vad.feed(_LOUD, _DT)                      # one frame of speech
    assert vad.silent_for == 0.0, (
        f"after a frame above the threshold, {vad.silent_for:.1f}s of silence "
        "is still on the clock")


# ---------------------------------------------------------------------------
# 5. BarModel -- the adaptive gain decay
# ---------------------------------------------------------------------------


def _full(model):
    return math.sqrt(model.reference * model._peak)


def _forgotten(loud_peak, model):
    """How much of a loud moment the adaptive gain is still carrying, 0..1."""
    return (model._peak - _PEAK_MIN) / (loud_peak - _PEAK_MIN)


def test_gain_decays_while_flat():
    m = BarModel()
    m.step(0.15, 1 / 60)
    assert m._peak == pytest.approx(0.15, rel=1e-6)
    for _ in range(int(20 * 60)):             # 20s at 60fps = 5 time constants
        m.flat(1 / 60)
    assert _forgotten(0.15, m) < 0.01, m._peak


def test_gain_decays_while_breathing():
    m = BarModel()
    m.step(0.15, 1 / 60)
    for _ in range(int(20 * 60)):
        m.breathe(1 / 60)
    assert _forgotten(0.15, m) < 0.01, m._peak


def test_idle_decay_matches_the_speaking_decay_exactly():
    a, b = BarModel(), BarModel()
    a._peak = b._peak = 0.15
    for _ in range(240):
        a.flat(1 / 60)
        b.step(0.0, 1 / 60)
    assert a._peak == pytest.approx(b._peak, rel=1e-12)


def test_decay_respects_the_four_second_constant():
    m = BarModel()
    m._peak = _PEAK_MAX
    for _ in range(int(_PEAK_DECAY_S * 60)):
        m.flat(1 / 60)
    # exponential with tau = 4s: ~63% of the way down after one tau
    remaining = (m._peak - _PEAK_MIN) / (_PEAK_MAX - _PEAK_MIN)
    assert 0.30 < remaining < 0.42, remaining


def test_decay_never_goes_below_the_floor_or_runs_with_zero_dt():
    m = BarModel()
    m._peak = 0.05
    for _ in range(10000):
        m.flat(1 / 60)
    assert m._peak >= _PEAK_MIN
    before = m._peak
    m.flat(0.0)
    m.breathe(-1.0)
    assert m._peak == before


def test_extra_decay_cannot_make_the_meter_read_low_mid_speech():
    """The gain decays in more paths now; a steady voice must never read
    quieter as a result."""
    m = BarModel()
    first = None
    for cycle in range(120):
        for _ in range(12):                   # 200ms of voice
            m.step(0.02, 1 / 60)
            norm = min(1.0, 0.02 / _full(m)) ** 0.5
            if first is None:
                first = norm
            assert norm >= first - 1e-9, (
                f"cycle {cycle}: a steady 0.02 voice now reads {norm:.3f} "
                f"where it first read {first:.3f}")
        for _ in range(12):                   # 200ms gap -> the idle path
            m.flat(1 / 60)


def test_a_loud_transient_does_not_flatten_the_next_word_forever():
    m = BarModel()
    m.step(_PEAK_MAX, 1 / 60)                 # a cough
    for _ in range(int(10 * 60)):             # 10s of silence
        m.flat(1 / 60)
    norm = min(1.0, 0.02 / _full(m)) ** 0.5
    assert norm > 0.9, (
        f"after 10s of silence a normal 0.02 voice still reads {norm:.2f}")


# ---------------------------------------------------------------------------
# 6. The overlay's repaint timer
# ---------------------------------------------------------------------------


def _drive(pill, frames=4000):
    """Deliver frames the way the QTimer does -- only while it is running."""
    n = 0
    while n < frames and pill._timer.isActive():
        pill._tick()
        n += 1
    return n


def _settle_armed(pill):
    pill.show_when_idle = True
    pill.set_state("armed")
    _drive(pill)
    assert not pill._timer.isActive(), "the pill never settled into rest"
    return pill


def test_repaint_timer_stops_once_the_armed_pill_has_settled(qapp, pill):
    _settle_armed(pill)
    assert pill._at_rest()


def test_repaint_timer_restarts_on_a_new_state(qapp, pill):
    _settle_armed(pill)
    pill.set_state("recording", "toggle")
    assert pill._timer.isActive(), "a new state did not restart the repaint timer"
    assert not pill._at_rest()
    pill.set_level(0.05)
    before = pill.bars.heights()[2]
    for _ in range(20):
        pill._tick()
    assert pill.bars.heights()[2] != before, "the meter is frozen while recording"


def test_switching_the_pill_off_from_rest_actually_hides_it(qapp, pill):
    """Pausing the hotkeys calls set_state('off'); the indicator must go away."""
    _settle_armed(pill)
    pill.set_state("off")
    delivered = _drive(pill, frames=900)      # 15s of frames, if any run
    assert not pill.isVisible(), (
        "the pill is still on screen after being switched off: "
        f"timer_active={pill._timer.isActive()} frames_delivered={delivered} "
        f"opacity={pill.opacity.value:.3f} -> {pill.opacity.target:.3f}. "
        "set_state('off') returns before the timer is restarted, so nothing "
        "ever steps the fade-out or calls hide().")


def test_the_off_path_leaves_something_running_to_finish_the_fade(qapp, pill):
    _settle_armed(pill)
    pill.set_state("off")
    assert pill._timer.isActive() or not pill.isVisible(), (
        "set_state('off') set opacity.target=0 with the repaint timer stopped; "
        "no frame will ever run to complete the fade")


def test_idle_gain_still_forgets_a_loud_moment_between_dictations(qapp, pill):
    """Claim 5 says the gain decays in the idle paths. Claim 6 stops the clock
    that drives them."""
    pill.show_when_idle = True
    pill.set_state("recording", "toggle")
    pill.set_level(0.15)
    for _ in range(30):
        pill._tick()
    assert pill.bars._peak > 0.1, pill.bars._peak
    pill.set_state("done")
    _drive(pill)                              # terminal hold, settle, then rest
    assert not pill._timer.isActive()
    loud = pill.bars._peak
    _drive(pill, frames=2400)                 # 40s of idle -- if frames arrive
    left = _forgotten(loud, pill.bars)
    assert left < 0.01, (
        f"the adaptive gain is frozen at {pill.bars._peak:.4f} ({left:.0%} of a "
        "loud moment still on the clock after 40s of idle): the repaint timer "
        "stopped, so the idle decay never runs. The first word of the next "
        "dictation reads "
        f"{min(1.0, 0.02 / math.sqrt(pill.bars.reference * pill.bars._peak)) ** 0.5:.0%} "
        "of its correct height.")


# ---------------------------------------------------------------------------
# 7. history.search / count / purge
# ---------------------------------------------------------------------------


@pytest.fixture()
def hist(tmp_path):
    h = History(tmp_path / "history.db")
    yield h
    h.close()


def _add(h, raw=None, polished=None, final=None, status="ok"):
    return h.add(raw, polished, final, "toggle", 1000, "app.exe", "Title",
                 status=status)


def test_search_reaches_all_four_text_columns(hist):
    _add(hist, raw="alpha zebra one")
    _add(hist, polished="beta zebra two")
    _add(hist, final="gamma zebra three")
    rid = _add(hist, raw="nothing here")
    hist.set_correction(rid, "delta zebra four")
    rows = hist.search("zebra")
    assert len(rows) == 4, [r["id"] for r in rows]


def test_search_finds_text_held_only_in_the_cleaned_column(hist):
    _add(hist, polished="the quick brown fox")
    assert len(hist.search("brown fox")) == 1


def test_search_escapes_percent(hist):
    _add(hist, final="discount 100% off")
    _add(hist, final="discount 1000 off")
    rows = hist.search("100%")
    assert len(rows) == 1 and "100%" in rows[0]["final_text"], rows


def test_search_escapes_underscore(hist):
    _add(hist, final="value a_b here")
    _add(hist, final="value axb here")
    rows = hist.search("a_b")
    assert len(rows) == 1 and "a_b" in rows[0]["final_text"], rows


def test_search_escapes_backslash(hist):
    _add(hist, final=r"open C:\Users\magli now")
    _add(hist, final="open C:/Users/magli now")
    rows = hist.search("\\")
    assert len(rows) == 1, [r["final_text"] for r in rows]
    rows = hist.search(r"C:\Users")
    assert len(rows) == 1, [r["final_text"] for r in rows]


def test_a_bare_wildcard_query_does_not_return_everything(hist):
    _add(hist, final="plain text")
    _add(hist, final="90% done")
    assert len(hist.search("%")) == 1
    assert len(hist.search("_")) == 0


@pytest.mark.parametrize("q", [
    "", "%", "_", "\\", "\\%", "%_\\", "'", "''", "; DROP TABLE sessions--",
    "\u65e5\u672c\u8a9e", "a" * 500, "line\nbreak", "tab\there",
])
def test_search_binds_the_right_number_of_parameters(hist, q):
    _add(hist, raw="something", polished="else", final="entirely")
    try:
        hist.search(q)
    except sqlite3.ProgrammingError as exc:
        pytest.fail(f"parameter/placeholder mismatch for {q!r}: {exc}")
    assert hist.count() == 1, "search mutated the table"


def test_search_limit_is_still_the_last_parameter(hist):
    for i in range(10):
        _add(hist, final=f"row {i} zebra")
    assert len(hist.search("zebra", limit=3)) == 3
    assert len(hist.search("zebra")) == 10


def test_search_orders_newest_first(hist):
    ids = [_add(hist, final=f"zebra {i}") for i in range(5)]
    rows = hist.search("zebra")
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_count_tracks_inserts(hist):
    assert hist.count() == 0
    for i in range(7):
        _add(hist, final=str(i))
    assert hist.count() == 7


def test_purge_keeps_the_newest_rows(hist):
    ids = [_add(hist, final=f"row {i}") for i in range(10)]
    hist.purge(3)
    assert hist.count() == 3
    assert sorted(r["id"] for r in hist.recent()) == ids[-3:]


def test_purge_breaks_ts_ties_by_id(hist):
    ids = [_add(hist, final=f"row {i}") for i in range(20)]
    # rows written in the same millisecond share ts; id must break the tie
    hist.purge(5)
    assert sorted(r["id"] for r in hist.recent()) == ids[-5:]


def test_purge_of_zero_or_negative_keeps_everything(hist):
    for i in range(4):
        _add(hist, final=str(i))
    hist.purge(0)
    hist.purge(-1)
    assert hist.count() == 4


def test_purge_larger_than_the_table_is_a_no_op(hist):
    for i in range(3):
        _add(hist, final=str(i))
    hist.purge(100)
    assert hist.count() == 3


def test_purge_then_search_still_works(hist):
    for i in range(10):
        _add(hist, polished=f"zebra {i}")
    hist.purge(4)
    assert len(hist.search("zebra")) == 4
