"""The comet's flight.

Motion adapted from Sotto (MIT) — see NOTICE.md. These tests pin the values
that make it feel like a throw rather than a slide, because they are the whole
point of the borrowed design.
"""
import math

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from murmur.ui.comet import (BURST_MS, FLIGHT_MS, FLIGHT_SHRINK, FLIGHT_STRETCH,
                             PULL_DIST, PULL_MS, Comet, _ease_out_cubic)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def comet(qapp):
    c = Comet()
    yield c
    c.hide()


def test_the_timings_match_the_design_it_came_from():
    """Sotto's README and CometFlight.swift both state 110ms / 260ms."""
    assert PULL_MS == 110
    assert FLIGHT_MS == 260


def test_it_pulls_back_before_launching(comet):
    """The load is what makes it read as a throw."""
    assert PULL_DIST > 0
    comet.launch(QPointF(900, 500), QPointF(200, 200))
    assert comet._phase == "pull"
    comet.hide()


def test_it_stretches_at_mid_flight_and_not_at_the_ends():
    """Elongation is what turns a moving dot into a comet."""
    def stretch(raw):
        return 1.0 + FLIGHT_STRETCH * (1.0 - abs(raw - 0.5) * 2.0)

    assert stretch(0.5) == pytest.approx(1.0 + FLIGHT_STRETCH)
    assert stretch(0.0) == pytest.approx(1.0)
    assert stretch(1.0) == pytest.approx(1.0)
    assert stretch(0.5) > stretch(0.25) > stretch(0.0)


def test_it_shrinks_as_it_arrives():
    assert 0.0 < FLIGHT_SHRINK < 1.0
    assert 1.0 - FLIGHT_SHRINK == pytest.approx(0.45, abs=0.01)


def test_the_flight_easing_decelerates():
    """Ease-out cubic: most of the distance early, arriving rather than
    drifting. Equal time steps must cover shrinking distances."""
    steps = [_ease_out_cubic(i / 10) for i in range(11)]
    deltas = [b - a for a, b in zip(steps, steps[1:])]
    assert all(b <= a + 1e-9 for a, b in zip(deltas, deltas[1:]))
    assert _ease_out_cubic(0.0) == 0.0
    assert _ease_out_cubic(1.0) == pytest.approx(1.0)


def test_the_flight_is_ballistic_not_a_chase(comet):
    """Sotto is explicit that it aims where the pointer WAS. Re-reading the
    cursor mid-flight would make it follow the user instead of delivering."""
    aim = QPointF(200, 200)
    comet.launch(QPointF(900, 500), aim)
    before = QPointF(comet._aim)
    for _ in range(12):
        comet._tick()
    assert comet._aim == before, "the target moved mid-flight"
    comet.hide()


def test_it_lands_before_it_bursts(comet):
    """The paste fires on landing, so the callback must run at the end of the
    flight and not at the end of the burst."""
    landed = []
    comet.launch(QPointF(900, 500), QPointF(200, 200), on_land=lambda: landed.append(1))
    ticks = 0
    while comet._phase in ("pull", "flight") and ticks < 200:
        comet._tick()
        ticks += 1
    assert landed == [1]
    assert comet._phase == "burst"
    comet.hide()


def test_the_landing_callback_fires_exactly_once(comet):
    calls = []
    comet.launch(QPointF(900, 500), QPointF(200, 200), on_land=lambda: calls.append(1))
    for _ in range(400):
        comet._tick()
    assert calls == [1]
    comet.hide()


def test_a_raising_callback_does_not_break_the_flight(comet):
    def boom():
        raise RuntimeError("paste failed")

    comet.launch(QPointF(900, 500), QPointF(200, 200), on_land=boom)
    for _ in range(400):
        comet._tick()          # must not raise
    assert not comet.flying


def test_the_window_covers_the_whole_flight(comet):
    start, aim = QPointF(1800, 900), QPointF(120, 80)
    comet.launch(start, aim)
    g = comet.geometry()
    assert g.left() <= min(start.x(), aim.x())
    assert g.top() <= min(start.y(), aim.y())
    assert g.right() >= max(start.x(), aim.x())
    assert g.bottom() >= max(start.y(), aim.y())
    comet.hide()


def test_it_finishes_and_hides(comet):
    comet.launch(QPointF(900, 500), QPointF(200, 200))
    for _ in range(500):
        comet._tick()
    assert not comet.flying
    assert not comet.isVisible()


def test_total_time_to_land_is_under_400ms():
    """Long enough to read as motion, short enough not to be in the way."""
    assert PULL_MS + FLIGHT_MS < 400


def test_it_never_takes_focus(comet):
    from PySide6.QtCore import Qt

    assert comet.focusPolicy() == Qt.NoFocus
    assert comet.testAttribute(Qt.WA_ShowWithoutActivating)
    assert comet.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert bool(comet.windowFlags() & Qt.WindowTransparentForInput)
