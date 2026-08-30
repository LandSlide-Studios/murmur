from murmur.vad import SilenceMonitor


def test_silence_accumulates_and_fires():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    assert m.feed(0.0, dt=0.5) is False
    assert m.feed(0.0, dt=0.6) is True          # 1.1s of continuous silence


def test_speech_resets_the_timer():
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    m.feed(0.0, dt=0.9)
    # Sustained, not a single frame. One block above the threshold used to
    # reset the whole counter, which is what let a door or a cough keep a
    # forgotten session alive indefinitely. Real speech easily clears this.
    for _ in range(5):
        m.feed(0.5, dt=0.1)
    assert m.feed(0.0, dt=0.9) is False


def test_someone_talking_with_natural_pauses_is_never_cut_off():
    """The protection this module exists for. Half a second of silence between
    phrases must never end a session someone is still using."""
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    for _ in range(50):
        assert m.feed(0.0, dt=0.5) is False
        for _ in range(5):                       # ~0.5s of actual talking
            assert m.feed(0.9, dt=0.1) is False


def test_a_room_that_is_merely_not_silent_does_not_keep_a_session_alive():
    """The defect the test above used to encode. A single frame above the
    threshold reset the counter, so a door, a cough or an HVAC cycle once
    every ninety seconds kept a forgotten session recording forever — while
    this module promises exactly the opposite."""
    m = SilenceMonitor(threshold=0.01, stop_after_s=1.0)
    fired = False
    for _ in range(50):
        fired = fired or m.feed(0.0, dt=0.5)
        fired = fired or m.feed(0.9, dt=0.05)    # one short transient
    assert fired, "four hours of an empty but noisy room would never auto-stop"


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
