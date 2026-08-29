"""Pill sizing and state transitions.

Headless-safe: a QApplication is created once, and nothing here shows a window.
"""
import pytest

from PySide6.QtWidgets import QApplication

from murmur.ui.pill import (DONE_SIZE, IDLE_SIZE, REC_SIZE,
                            TOGGLE_SIZE, WORK_SIZE, Pill)


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
    assert 0.3 < pill.opacity.value < 0.8, "visible at a glance, easy to ignore"


def test_the_armed_indicator_is_the_smallest_state(pill):
    """It has to read as 'running', not as 'something is happening'."""
    from murmur.ui.pill import REC_SIZE, TOGGLE_SIZE, WORK_SIZE

    for other in (REC_SIZE, TOGGLE_SIZE, WORK_SIZE):
        assert IDLE_SIZE[0] < other[0]
        assert IDLE_SIZE[1] < other[1]


def test_the_pill_is_vertical(pill):
    """Docked against the right bezel, so it is taller than it is wide."""
    pill.set_state("recording", "hold")
    settle(pill)
    assert round(pill.width_s.value) == REC_SIZE[0]
    assert round(pill.height_s.value) == REC_SIZE[1]
    assert REC_SIZE[1] > REC_SIZE[0] * 3, "should read as a vertical bar"


def test_the_armed_sliver_is_vertical_too(pill):
    assert IDLE_SIZE[1] > IDLE_SIZE[0] * 3
    assert IDLE_SIZE[0] < REC_SIZE[0], "the sliver must be narrower than active"


def test_it_sits_against_the_right_edge(pill, qapp):
    from PySide6.QtGui import QGuiApplication

    pill.set_state("armed")
    settle(pill, 60)
    geo = QGuiApplication.primaryScreen().availableGeometry()
    # The window spans the gap; the capsule is drawn at its right edge.
    assert pill.x() + pill.width() >= geo.right() - 2


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


def test_hands_free_is_taller_than_push_to_talk(pill):
    """With no text label, size is what distinguishes a session you can walk
    away from."""
    pill.set_state("recording", "hold")
    plain = pill._target_size()[1]
    pill.set_state("recording", "toggle")
    assert pill._target_size()[1] > plain
    assert TOGGLE_SIZE[1] > REC_SIZE[1]


def test_working_states_have_their_own_size(pill):
    pill.set_state("transcribing")
    assert pill._target_size() == WORK_SIZE
    pill.set_state("polishing")
    assert pill._target_size() == WORK_SIZE


def test_the_cleaning_up_state_exists_and_is_amber(pill):
    """Tommy asked for the cleaning-up dots specifically."""
    from murmur.ui.pill import ACCENT

    assert "polishing" in ACCENT
    assert ACCENT["polishing"].name().lower() == "#e8b84b"
    assert ACCENT["transcribing"] == ACCENT["polishing"]


def test_terminal_states_shrink(pill):
    pill.set_state("done")
    assert pill._target_size() == DONE_SIZE


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
