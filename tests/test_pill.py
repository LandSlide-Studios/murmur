"""Pill sizing and state transitions.

Headless-safe: a QApplication is created once, and nothing here shows a window.
"""
import pytest

from PySide6.QtWidgets import QApplication

from murmur.ui.pill import IDLE_SIZE, REC_SIZE, Pill


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def pill(qapp):
    p = Pill(show_when_idle=True)
    yield p
    p.hide()


def settle(p, frames=400):
    for _ in range(frames):
        p._tick()


def test_the_armed_indicator_is_small_and_dim(pill):
    pill.set_state("armed")
    settle(pill)
    assert (round(pill.width_s.value), round(pill.height_s.value)) == IDLE_SIZE
    assert 0.1 < pill.opacity.value < 0.6, "the idle indicator must be unobtrusive"


def test_recording_is_slimmer_than_the_old_pill(pill):
    """The old capsule was 140x36; Wispr-style is a good deal slimmer."""
    pill.set_state("recording", "hold")
    settle(pill)
    assert round(pill.height_s.value) == REC_SIZE[1] <= 26
    assert round(pill.width_s.value) == REC_SIZE[0] <= 110


def test_idle_maps_to_the_armed_indicator_not_to_hidden(pill):
    """'Can I dictate right now?' has to have a visible answer."""
    pill.set_state("idle")
    assert pill.state == "armed"
    assert pill.opacity.target > 0


def test_idle_hides_entirely_when_the_indicator_is_switched_off(qapp):
    p = Pill(show_when_idle=False)
    p.set_state("idle")
    assert p.state == "off"
    assert p.opacity.target == 0.0
    p.hide()


def test_labelled_states_size_themselves_to_their_text(pill):
    pill.set_state("transcribing")
    transcribing = pill._target_size()[0]
    pill.set_state("copied")
    copied = pill._target_size()[0]
    assert copied > transcribing, "a longer label needs a wider capsule"
    assert transcribing > REC_SIZE[0], "a label needs more room than bars alone"


def test_hands_free_is_wider_than_plain_recording(pill):
    pill.set_state("recording", "hold")
    plain = pill._target_size()[0]
    pill.set_state("recording", "toggle")
    assert pill._target_size()[0] > plain


def test_a_finished_session_returns_to_the_armed_indicator(pill):
    pill.set_state("recording", "hold")
    settle(pill, 30)
    pill.set_state("done")
    settle(pill, 300)
    assert pill.state == "armed"
    assert (round(pill.width_s.value), round(pill.height_s.value)) == IDLE_SIZE


def test_a_cancelled_session_also_returns_to_armed(pill):
    pill.set_state("recording", "hold")
    settle(pill, 30)
    pill.set_state("cancelled")
    settle(pill, 300)
    assert pill.state == "armed"


def test_off_hides_the_pill(pill):
    pill.set_state("armed")
    settle(pill, 60)
    pill.set_state("off")
    settle(pill, 300)
    assert pill.opacity.value < 0.02
    assert not pill.isVisible()


def test_the_size_change_is_animated_not_instant(pill):
    """The armed indicator grows into the recording pill; it must not jump."""
    pill.set_state("armed")
    settle(pill)
    pill.set_state("recording", "hold")
    pill._tick()
    w = pill.width_s.value
    assert IDLE_SIZE[0] < w < REC_SIZE[0], f"width jumped straight to {w}"


def test_the_pill_never_accepts_focus(pill):
    from PySide6.QtCore import Qt

    assert pill.focusPolicy() == Qt.NoFocus
    assert pill.testAttribute(Qt.WA_ShowWithoutActivating)
    assert pill.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert bool(pill.windowFlags() & Qt.WindowTransparentForInput)
