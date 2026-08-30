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


def test_the_armed_indicator_is_the_least_prominent_state(pill):
    """It has to read as 'running', not as 'something is happening'. The orb is
    square rather than tall, so compare area, not both dimensions."""
    from murmur.ui.pill import REC_SIZE, TOGGLE_SIZE, WORK_SIZE

    idle_area = IDLE_SIZE[0] * IDLE_SIZE[1]
    for other in (REC_SIZE, TOGGLE_SIZE, WORK_SIZE):
        assert IDLE_SIZE[0] < other[0], "the sliver must be the narrowest"
        assert idle_area <= other[0] * other[1]


def test_working_contracts_to_an_orb(pill):
    """A shape you cannot mistake for 'still listening' is what tells you the
    recording has stopped and the machine has taken over."""
    from murmur.ui.pill import REC_SIZE, WORK_SIZE

    w, h = WORK_SIZE
    assert abs(w - h) <= 4, "the working state should read as round"
    assert h < REC_SIZE[1] / 2, "it must be obviously shorter than listening"


def test_the_orb_pulses(pill):
    pill.set_state("transcribing")
    seen = set()
    for _ in range(60):
        pill._tick()
        seen.add(round(pill.orb, 3))
    assert len(seen) > 10, "the orb should breathe, not sit still"


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
    """The controls mean the window now takes clicks, so it can no longer be
    input-transparent. What must NOT change is that it never activates — an
    activated overlay eats the paste the tick just asked for."""
    from PySide6.QtCore import Qt

    assert pill.focusPolicy() == Qt.NoFocus
    assert pill.testAttribute(Qt.WA_ShowWithoutActivating)


def test_clicks_pass_through_unless_the_controls_are_showing(pill):
    """An always-clickable overlay would swallow clicks on the window behind
    it, and it sits over the edge of one."""
    from PySide6.QtCore import Qt

    pill.set_state("armed")
    assert pill.testAttribute(Qt.WA_TransparentForMouseEvents)
    pill.set_state("recording", "toggle")
    settle(pill)                      # controls only exist once it has grown
    assert not pill.testAttribute(Qt.WA_TransparentForMouseEvents)
    pill.set_state("done")
    assert pill.testAttribute(Qt.WA_TransparentForMouseEvents)


def test_the_controls_appear_only_once_the_capsule_can_hold_them(pill):
    """Growing out of the sliver must not flash cramped glyphs on the way up."""
    pill.set_state("armed")
    settle(pill)
    pill.set_state("recording", "toggle")
    pill._tick()
    assert pill._button_rects() == (None, None), "controls drawn mid-morph"
    settle(pill)
    accept, cancel = pill._button_rects()
    assert accept is not None and cancel is not None


def test_the_tick_is_at_the_top_and_the_cross_at_the_bottom(pill):
    pill.set_state("recording", "toggle")
    settle(pill)
    accept, cancel = pill._button_rects()
    assert accept.top() < cancel.top()
    capsule = pill._capsule_rect()
    assert accept.top() == capsule.top()
    assert cancel.bottom() == capsule.bottom()
    assert not accept.intersects(cancel)


def test_clicking_the_tick_asks_to_stop_and_paste(pill):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    fired = []
    pill.accepted.connect(lambda: fired.append("accept"))
    pill.cancelled_by_user.connect(lambda: fired.append("cancel"))
    pill.set_state("recording", "toggle")
    settle(pill)
    accept, cancel = pill._button_rects()

    for point, expected in ((accept.center(), "accept"), (cancel.center(), "cancel")):
        pill.mouseReleaseEvent(QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(point), Qt.LeftButton,
            Qt.LeftButton, Qt.NoModifier))
    assert fired == ["accept", "cancel"]


def test_clicking_the_waveform_between_them_does_nothing(pill):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    fired = []
    pill.accepted.connect(lambda: fired.append("accept"))
    pill.cancelled_by_user.connect(lambda: fired.append("cancel"))
    pill.set_state("recording", "toggle")
    settle(pill)
    pill.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(pill._capsule_rect().center()),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert fired == []


def test_the_bars_are_packed_tight(pill):
    """'Closer together, basically next to each other.' A visible gap between
    every bar reads as separate ticks rather than one waveform."""
    from murmur.ui.pill import BAR_GAP, BARS

    assert BARS >= 14, "too few bars to read as a waveform"
    assert BAR_GAP <= 2.0, "the bars are still spread apart"


def test_the_pill_got_smaller(pill):
    """It was 34x150 and read as chunky."""
    assert REC_SIZE[0] <= 30
    assert REC_SIZE[1] <= 140
    assert IDLE_SIZE[0] <= 12


def test_the_pill_does_not_carry_its_own_copy_of_the_speech_threshold(qapp):
    """It hardcoded 0.012 while the config held the real value. That copy sat
    above his speaking voice, so the bars ran breathe() the whole time he talked
    and no amount of tuning the meter could have shown up."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "murmur" / "ui" / "pill.py"
    body = src.read_text(encoding="utf-8")
    gate = [ln for ln in body.splitlines() if "self.level >" in ln]
    assert gate, "the speech gate moved; check this test still points at it"
    for ln in gate:
        assert not re.search(r"self\.level\s*>\s*0\.\d+", ln), \
            f"threshold hardcoded again: {ln.strip()}"


def test_main_feeds_the_configured_threshold_into_the_pill():
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "murmur" / "__main__.py"
    body = src.read_text(encoding="utf-8")
    assert "pill.speech_threshold = cfg.get(\"audio.speech_rms_threshold\")" in body


def test_the_tick_and_cross_are_hands_free_only(qapp):
    """In a push-to-talk hold his fingers are already on the keys that stop it.
    A button he must release the chord to reach is worse than the chord."""
    from murmur.ui.pill import Pill

    pill = Pill()
    pill.set_state("recording", mode="hold")
    pill.width_s.value, pill.height_s.value = 30.0, 180.0
    assert pill._controls_visible() is False
    assert pill._button_rects() == (None, None)

    pill.set_state("recording", mode="toggle")
    pill.width_s.value, pill.height_s.value = 30.0, 180.0
    assert pill._controls_visible() is True
    accept, cancel = pill._button_rects()
    assert accept is not None and cancel is not None
    assert accept.top() < cancel.top(), "tick on top, cross on the bottom"
    pill.close()


def test_a_hold_session_stays_click_through(qapp):
    """The controls are what forced the pill to accept clicks at all. With them
    gone from hold mode, a hold must not be taking clicks either."""
    from murmur.ui.pill import Pill
    from PySide6.QtCore import Qt

    pill = Pill()
    pill.set_state("recording", mode="hold")
    pill.width_s.value, pill.height_s.value = 30.0, 180.0
    pill._set_click_through(not pill._controls_visible())
    assert pill.testAttribute(Qt.WA_TransparentForMouseEvents) is True
    pill.close()


def test_a_full_scale_bar_very_nearly_spans_the_capsule(qapp):
    """The side margin capped a full bar at 83% of the capsule width, so the
    meter could not reach the edges even with the model pinned at 1.0."""
    from murmur.ui.pill import REC_SIZE, Pill

    w = float(REC_SIZE[0])
    assert Pill.bar_length(1.0, w, 1.0) / w > 0.92


def test_bar_length_spreads_his_speaking_range_across_the_capsule(qapp):
    """At his measured 0.008 the model runs roughly 0.15..0.83. Those must be
    visibly different lengths, not two similar stubs."""
    from murmur.ui.pill import REC_SIZE, Pill

    w = float(REC_SIZE[0])
    short = Pill.bar_length(0.15, w, 1.0)
    tall = Pill.bar_length(0.83, w, 1.0)
    assert tall / short > 3.0
    assert tall / w > 0.75


def test_a_silent_bar_is_about_half_a_speaking_one(qapp):
    """What he asked for: flat marks at roughly half the size of the bars."""
    from murmur.ui.pill import REC_SIZE, Pill
    from murmur.ui.waveform import FLAT

    w = float(REC_SIZE[0])
    silent = Pill.bar_length(FLAT, w, 1.0)
    speaking = Pill.bar_length(0.66, w, 1.0)     # a typical bar at his voice
    assert 0.35 < silent / speaking < 0.62


def test_the_resting_pill_is_empty(pill):
    """Armed is the resting indicator: Murmur is loaded, not listening. Bars in
    it read as listening."""
    from PySide6.QtGui import QImage

    pill.set_state("armed")
    settle(pill)
    img = QImage(pill.size(), QImage.Format_ARGB32)
    img.fill(0)
    pill.render(img)

    from murmur.ui.pill import ACCENT
    accent = ACCENT["armed"]
    hits = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 40 and abs(c.red() - accent.red()) < 30 \
                    and abs(c.blue() - accent.blue()) < 30 \
                    and abs(c.green() - accent.green()) < 30:
                hits += 1
    assert hits < 12, f"{hits} accent-coloured pixels inside the resting pill"


def test_recording_still_draws_its_bars(pill):
    """The counterweight — proving the test above is measuring the right thing."""
    from PySide6.QtGui import QImage

    pill.set_state("recording", "hold")
    pill.set_level(0.02)
    settle(pill)
    img = QImage(pill.size(), QImage.Format_ARGB32)
    img.fill(0)
    pill.render(img)

    from murmur.ui.pill import ACCENT
    accent = ACCENT["recording"]
    hits = sum(
        1
        for y in range(img.height())
        for x in range(img.width())
        if (lambda c: c.alpha() > 40 and abs(c.red() - accent.red()) < 40
            and abs(c.blue() - accent.blue()) < 40)(img.pixelColor(x, y))
    )
    assert hits > 100, f"only {hits} accent pixels while recording"
