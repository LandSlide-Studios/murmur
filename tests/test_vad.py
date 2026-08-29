from murmur.vad import SilenceMonitor


def test_silence_accumulates_and_fires():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    assert m.feed(0.0, dt=0.5) is False
    assert m.feed(0.0, dt=0.6) is True          # 1.1s of continuous silence


def test_speech_resets_the_timer():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    m.feed(0.0, dt=0.9)
    m.feed(0.5, dt=0.1)                          # speech resets
    assert m.feed(0.0, dt=0.9) is False


def test_intermittent_speech_never_fires():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    for _ in range(50):
        assert m.feed(0.0, dt=0.5) is False
        assert m.feed(0.9, dt=0.1) is False


def test_reset_clears_accumulated_silence():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    m.feed(0.0, dt=0.9)
    m.reset()
    assert m.feed(0.0, dt=0.9) is False


def test_level_exactly_at_threshold_counts_as_silence():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    assert m.feed(0.01, dt=1.5) is True          # not strictly above -> silent


def test_fires_exactly_at_the_boundary():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    assert m.feed(0.0, dt=1.0) is True


def test_keeps_firing_once_past_the_threshold():
    """The caller may not stop instantly; the monitor must not flip back."""
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    assert m.feed(0.0, dt=1.0) is True
    assert m.feed(0.0, dt=0.1) is True


def test_zero_dt_does_not_advance():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    for _ in range(100):
        assert m.feed(0.0, dt=0.0) is False
