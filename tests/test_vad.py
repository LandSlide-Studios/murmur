import pytest
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


def _run(pattern, seconds, threshold=0.004, stop_after_s=90.0, dt=1 / 60):
    """Drive the monitor over a realistic pattern. Returns when it stopped, or
    None. Patterns are expressed as duty cycles because that is what actually
    separates a person from a room — see the note in the test below."""
    m = SilenceMonitor(threshold=threshold, stop_after_s=stop_after_s)
    t = 0.0
    while t < seconds:
        if m.feed(pattern(t), dt):
            return t
        t += dt
    return None


def _cycle(on_s, period_s, level=0.02):
    return lambda t: level if (t % period_s) < on_s else 0.0


@pytest.mark.parametrize("on_s,period_s,what", [
    (0.25, 0.95, "reading a list aloud"),
    (0.28, 0.53, "a slow, deliberate speaker"),
    (0.20, 0.25, "ordinary speech with 50ms stop-consonant closures"),
    (1.20, 3.00, "long thinking pauses between phrases"),
])
def test_a_live_speaker_is_never_cut_off(on_s, period_s, what):
    """The older and more important of this module's two promises.

    An earlier attempt at the forgotten-session guard required a SUSTAINED
    burst to clear the silence counter, which turned it into a cumulative
    measure — someone reading a list aloud banked every gap and was cut off
    122 seconds into an active dictation.
    """
    assert _run(_cycle(on_s, period_s), 900) is None, f"cut off {what}"


@pytest.mark.parametrize("on_s,period_s,what", [
    (0.35, 89.0, "a door once every ninety seconds"),
    (0.50, 20.0, "a fan cycling"),
    (0.05, 5.00, "a keyboard in the next room"),
])
def test_a_room_that_is_merely_not_silent_does_not_keep_a_session_alive(
        on_s, period_s, what):
    """Burst LENGTH is the wrong discriminator, and measurement says so: real
    words are short (0.25s) while room noise can be long (a door is 350ms).
    Duty cycle separates them — speech is a quarter to four fifths voiced, a
    room with an occasional noise is well under one percent."""
    assert _run(_cycle(on_s, period_s), 3600) is not None,         f"an hour of {what} never auto-stopped"


def test_a_genuinely_silent_room_still_stops_on_the_original_condition():
    assert _run(lambda t: 0.0, 300) == pytest.approx(90, abs=2)


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
