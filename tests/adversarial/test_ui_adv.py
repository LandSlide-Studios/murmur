"""Adversarial scenarios for the UI layer: motion, waveform, pill, comet.

Written air-gapped from the engineering log and the existing test suite. Each
scenario states the invariant it believes the module owes its callers; a failure
here is a claim to be triaged, not a verdict.

Scenarios that sweep a set (all states, all transition pairs, a range of hostile
levels) loop internally rather than parametrising, so one scenario is one test.
"""

import ctypes
import math
import random

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from murmur.ui import waveform as wf
from murmur.ui.comet import Comet
from murmur.ui.motion import _MAX_DT, Spring, ease_out_expo
from murmur.ui.pill import (WORKING, WS_EX_NOACTIVATE, WS_EX_TRANSPARENT, Pill,
                            TERMINAL)
from murmur.ui.waveform import BarModel

ALL_STATES = ("off", "armed", "recording", "transcribing", "polishing",
              "done", "copied", "launching", "cancelled", "error")

FRAME = 1 / 60


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def pill(qapp):
    p = Pill()
    yield p
    p.hide()
    p.close()
    p.deleteLater()
    qapp.processEvents()


@pytest.fixture
def comet(qapp):
    c = Comet()
    yield c
    c.hide()
    c.close()
    c.deleteLater()
    qapp.processEvents()


def native_ex(widget) -> int:
    """The real Win32 extended style on the widget's HWND."""
    return ctypes.windll.user32.GetWindowLongW(int(widget.winId()), -20)


def finite(xs) -> bool:
    return all(math.isfinite(x) for x in xs)


def in_unit(xs) -> bool:
    return all(0.0 <= x <= 1.0 for x in xs)


def settle(p, frames=400):
    for _ in range(frames):
        p._tick()


def render(w):
    pm = QPixmap(w.size())
    pm.fill(Qt.transparent)
    w.render(pm)


# ==========================================================================
# 1. motion.Spring — the integrator                              (13)
# ==========================================================================

def test_spring_non_positive_dt_is_a_no_op():
    for dt in (0.0, -0.0, -1e-9, -0.5, float("-inf")):
        s = Spring(0.0)
        s.target = 1.0
        s.step(dt)
        assert (s.value, s.velocity) == (0.0, 0.0), f"dt={dt}"


def test_spring_enormous_dt_is_clamped_to_max_dt():
    """A stalled UI thread resuming must not integrate a 5-second frame."""
    a, b = Spring(0.0), Spring(0.0)
    a.target = b.target = 1.0
    a.step(5.0)
    b.step(_MAX_DT)
    assert a.value == pytest.approx(b.value)
    assert a.velocity == pytest.approx(b.velocity)


def test_spring_infinite_dt_stays_finite():
    s = Spring(0.0)
    s.target = 1.0
    s.step(float("inf"))
    assert math.isfinite(s.value) and math.isfinite(s.velocity)


def test_spring_repeated_stall_frames_converge_not_diverge():
    s = Spring(0.0)
    s.target = 1.0
    peak = 0.0
    for _ in range(2000):
        s.step(3.0)
        peak = max(peak, abs(s.value))
    assert math.isfinite(s.value)
    assert peak < 10.0, f"diverged to {peak}"
    assert s.value == pytest.approx(1.0, abs=1e-3)


def test_spring_nan_dt_does_not_poison_the_value():
    """`dt = min(dt, _MAX_DT)` returns NaN unchanged and `if dt <= 0` is False
    for NaN, so a NaN frame time walks straight into the integrator and the
    spring is NaN for the rest of the process."""
    s = Spring(0.0)
    s.target = 1.0
    s.step(float("nan"))
    assert math.isfinite(s.value), "NaN dt poisoned the spring"


def test_spring_nan_target_is_recoverable():
    """A bad target must not permanently wedge the spring: retargeting to a
    real number afterwards should bring the value back."""
    s = Spring(0.0)
    s.target = float("nan")
    for _ in range(10):
        s.step(FRAME)
    s.target = 1.0
    for _ in range(600):
        s.step(FRAME)
    assert math.isfinite(s.value), "never recovered from a NaN target"
    assert s.at_rest


def test_spring_inf_target_is_recoverable():
    s = Spring(0.0)
    s.target = float("inf")
    for _ in range(10):
        s.step(FRAME)
    s.target = 1.0
    for _ in range(600):
        s.step(FRAME)
    assert math.isfinite(s.value), "never recovered from an inf target"


def test_spring_retargeted_every_frame_stays_bounded():
    s = Spring(0.0)
    worst = 0.0
    for i in range(5000):
        s.target = 0.0 if i % 2 else 1.0        # worst-case square wave
        s.step(FRAME)
        worst = max(worst, abs(s.value))
    assert math.isfinite(s.value)
    assert worst < 3.0, f"square-wave retargeting reached {worst}"


def test_spring_constant_target_comes_to_rest():
    s = Spring(0.0)
    s.target = 1.0
    for _ in range(300):                        # 5 s
        s.step(FRAME)
    assert s.at_rest


def test_spring_does_not_oscillate_forever():
    """Energy must decay: the ring after settling has to be gone, not just
    small on the frame we happen to sample."""
    s = Spring(0.0)
    s.target = 1.0
    for _ in range(300):
        s.step(FRAME)
    tail = [abs(s.step(FRAME) - 1.0) for _ in range(600)]
    assert max(tail) < 1e-3, f"still ringing at {max(tail)}"


def test_every_spring_the_pill_uses_is_stable_at_max_dt():
    """Constants taken from Pill.__init__ and BarModel. All of them must
    survive a run of maximum-dt frames without the integrator exploding."""
    constants = [(200.0, 24.0), (190.0, 23.0), (180.0, 22.0), (200.0, 18.0),
                 (210.0, 14.0), (322.0, 19.6)]   # bar 0 and bar 14 of 15
    for k, c in constants:
        s = Spring(0.0, stiffness=k, damping=c)
        s.target = 1.0
        worst = 0.0
        for _ in range(1000):
            s.step(_MAX_DT)
            worst = max(worst, abs(s.value))
        assert worst < 5.0, f"k={k} c={c} reached {worst}"


def test_ease_out_expo_is_clamped_outside_the_unit_range():
    assert ease_out_expo(-5.0) == 0.0
    assert ease_out_expo(0.0) == 0.0
    assert ease_out_expo(1.0) == 1.0
    assert ease_out_expo(1e9) == 1.0
    assert 0.0 <= ease_out_expo(0.5) <= 1.0


# ==========================================================================
# 2. waveform.BarModel — the bar model                           (18)
# ==========================================================================

def test_barmodel_zero_bars_does_not_crash():
    m = BarModel(n=0)
    m.step(0.01, FRAME)
    m.breathe(FRAME)
    m.flat(FRAME)
    assert m.heights() == []


def test_barmodel_one_bar_can_reach_full_scale():
    """A single bar IS the centre bar. `mid` falls back to 1.0 when n <= 1, so
    the centre taper treats the only bar in the row as an edge bar."""
    m = BarModel(n=1)
    peak = 0.0
    for _ in range(600):
        m.step(1.0, FRAME)
        peak = max(peak, m.springs[0].target)
    assert peak > 0.9, f"lone bar tops out at target {peak:.3f}"


def test_barmodel_thousand_bars_heights_stay_in_range():
    m = BarModel(n=1000)
    for _ in range(600):
        m.step(0.02, FRAME)
    assert finite(m.heights()) and in_unit(m.heights())


def test_barmodel_thousand_bars_velocities_stay_finite():
    """Per-bar stiffness and damping scale linearly with the bar index, so a
    long row walks the springs past the explicit-integrator stability limit."""
    m = BarModel(n=1000)
    for _ in range(600):
        m.step(0.02, FRAME)
    v = [s.velocity for s in m.springs]
    bad = sum(1 for x in v if not math.isfinite(x))
    assert bad == 0, f"{bad}/1000 spring velocities diverged to inf/NaN"


def test_barmodel_negative_level_stays_in_range():
    m = BarModel(n=15)
    for _ in range(300):
        m.step(-1.0, FRAME)
    assert finite(m.heights()) and in_unit(m.heights())


def test_barmodel_nan_level_stays_finite():
    m = BarModel(n=15)
    for _ in range(120):
        m.step(float("nan"), FRAME)
    assert finite(m.heights()), "NaN level leaked into the bar heights"


def test_barmodel_infinite_level_stays_in_range():
    m = BarModel(n=15)
    for _ in range(120):
        m.step(float("inf"), FRAME)
    assert finite(m.heights()) and in_unit(m.heights())


def test_barmodel_level_far_above_full_scale():
    m = BarModel(n=15)
    for _ in range(300):
        m.step(1e6, FRAME)
    assert in_unit(m.heights())


def test_barmodel_level_alternating_zero_and_huge_every_frame():
    m = BarModel(n=15)
    for i in range(1200):
        m.step(0.0 if i % 2 else 1e6, FRAME)
    assert finite(m.heights()) and in_unit(m.heights())


def test_barmodel_dt_zero_changes_nothing():
    m = BarModel(n=15)
    before, t0 = m.heights(), m.t
    m.step(0.5, 0.0)
    m.breathe(0.0)
    m.flat(0.0)
    assert m.heights() == before and m.t == t0


def test_barmodel_negative_dt_changes_nothing():
    m = BarModel(n=15)
    before, t0 = m.heights(), m.t
    m.step(0.5, -1.0)
    m.breathe(-1.0)
    m.flat(-1.0)
    assert m.heights() == before and m.t == t0


def test_barmodel_enormous_dt_stays_in_range():
    m = BarModel(n=15)
    for _ in range(200):
        m.step(0.02, 5.0)               # 200 stalled frames in a row
    assert finite(m.heights()) and in_unit(m.heights())


def test_barmodel_peak_never_leaves_its_own_bounds():
    m = BarModel(n=15)
    for i in range(3000):
        m.step((i % 7) * 0.05 - 0.05, FRAME)
        assert wf._PEAK_MIN <= m._peak <= wf._PEAK_MAX


def test_barmodel_adaptive_gain_recovers_after_a_loud_burst():
    """A clipping burst must not leave the meter permanently deaf to a normal
    speaking voice."""
    m = BarModel(n=15)
    for _ in range(60):
        m.step(1e6, FRAME)
    for _ in range(int(20 / FRAME)):            # 20 s of normal speech
        m.step(0.020, FRAME)
    assert max(m.heights()) > 0.5, f"meter stuck at {max(m.heights()):.3f}"


def test_barmodel_gain_decays_while_listening_to_silence():
    """`flat()` is what runs during in-recording silence, and silence is
    exactly when the meter has time to forget a loud moment. The peak only
    decays inside `step()`, so the auto-gain is frozen for the whole quiet
    stretch and the next word is measured against the old loud reference."""
    m = BarModel(n=15)
    m.step(wf._PEAK_MAX, FRAME)
    loud = m._peak
    for _ in range(int(30 / FRAME)):            # 30 s of silence
        m.flat(FRAME)
    assert m._peak < loud * 0.5, (
        f"peak frozen at {m._peak:.4f} after 30 s of silence "
        f"(the documented decay constant is {wf._PEAK_DECAY_S} s)")


def test_barmodel_heights_never_escape_unit_range_under_fuzz():
    rng = random.Random(1234)
    m = BarModel(n=15)
    for _ in range(5000):
        lvl = rng.choice([0.0, 1e-6, 0.004, 0.02, 0.5, 12.0, 1e5, -3.0])
        dt = rng.choice([FRAME, 1 / 30, 0.25, 2.0])
        rng.choice([m.step, lambda l, d: m.flat(d),
                    lambda l, d: m.breathe(d)])(lvl, dt)
        assert in_unit(m.heights())


def test_barmodel_flat_settles_without_overshooting_the_top():
    m = BarModel(n=15)
    for _ in range(60):
        m.step(1.0, FRAME)              # drive it high first
    worst = 0.0
    for _ in range(300):
        m.flat(FRAME)
        worst = max(worst, max(m.heights()))
    assert worst <= 1.0
    assert all(abs(v - wf.FLAT) < 0.02 for v in m.heights())


def test_barmodel_flat_is_actually_flat():
    """The module makes motionlessness the whole signal of this state."""
    m = BarModel(n=15)
    for _ in range(300):
        m.flat(FRAME)
    h = m.heights()
    assert max(h) - min(h) < 1e-6


# ==========================================================================
# 3. Pill — focus and activation                                  (5)
# ==========================================================================

def test_pill_never_becomes_focusable_in_any_state(pill):
    for state in ALL_STATES:
        pill.set_state(state, "toggle")
        settle(pill, 120)
        assert pill.focusPolicy() == Qt.NoFocus, state
        assert pill.focusWidget() is None, state
        assert pill.testAttribute(Qt.WA_ShowWithoutActivating), state


def test_pill_never_becomes_the_active_window_in_any_state(pill, qapp):
    for state in ALL_STATES:
        pill.set_state(state, "toggle")
        settle(pill, 120)
        qapp.processEvents()
        assert not pill.isActiveWindow(), state
        assert QApplication.activeWindow() is not pill, state


def test_pill_never_becomes_active_across_rapid_transitions(pill, qapp):
    seq = ["armed", "recording", "transcribing", "polishing", "done",
           "recording", "cancelled", "error", "launching", "armed", "off"]
    for s in seq:
        pill.set_state(s, "toggle")
        qapp.processEvents()
        assert not pill.isActiveWindow(), s
        assert pill.focusPolicy() == Qt.NoFocus, s


def test_pill_refuses_activation_even_when_asked(pill, qapp):
    """The hardest version of the rule: not "nothing happens to activate it"
    but "it cannot be activated". WS_EX_NOACTIVATE only blocks activation by
    CLICK; it does not stop SetForegroundWindow, which is what
    `activateWindow()` reaches. If the pill can end up holding the OS keyboard
    focus by any route, the paste lands in the pill."""
    u = ctypes.windll.user32
    for state in ("armed", "recording", "transcribing", "done"):
        pill.set_state(state, "toggle")
        settle(pill, 200)
        qapp.processEvents()
        hwnd = int(pill.winId())
        pill.raise_()
        pill.activateWindow()
        qapp.processEvents()
        assert u.GetForegroundWindow() != hwnd, f"{state}: pill took foreground"
        assert u.GetFocus() != hwnd, f"{state}: pill took keyboard focus"
        assert not pill.isActiveWindow(), f"{state}: Qt made the pill active"


def test_pill_visible_window_always_carries_ws_ex_noactivate(pill):
    """WS_EX_NOACTIVATE is the only thing stopping a click on the pill from
    stealing the focus the paste needs."""
    for state in ("armed", "recording", "transcribing", "done", "error"):
        pill.set_state(state, "toggle")
        settle(pill, 30)
        if pill.isVisible():
            assert native_ex(pill) & WS_EX_NOACTIVATE, f"unhardened in {state}"


def test_pill_shown_before_its_first_state_is_still_hardened(pill):
    """`_harden()` runs only on the not-visible branch of `set_state`. A pill
    that is already on screen when its first state arrives never gets
    WS_EX_NOACTIVATE at all, and can then be activated by a click."""
    pill.show()
    pill.set_state("recording", "toggle")
    settle(pill, 30)
    assert native_ex(pill) & WS_EX_NOACTIVATE, "visible pill can be activated"


# ==========================================================================
# 4. Pill — click-through                                         (6)
# ==========================================================================

def test_pill_is_click_through_in_every_steady_state(pill):
    """A pill that swallows clicks with nothing to click is stealing them from
    the window underneath. Only `recording` in toggle mode draws controls."""
    offenders = []
    for state in ALL_STATES:
        for mode in ("hold", "toggle"):
            pill.set_state("armed", mode)       # common baseline
            settle(pill, 300)
            pill.set_state(state, mode)
            settle(pill, 300)
            draws_controls = (pill.state == "recording" and mode == "toggle"
                              and pill._button_rects()[0] is not None)
            if not draws_controls and not pill._click_through:
                offenders.append((state, mode))
    assert not offenders, f"opaque with nothing to click: {offenders}"


def test_pill_click_through_flag_matches_the_native_transparent_bit(pill):
    """`_set_click_through` early-returns when the requested value already
    equals the tracked one, and the tracked one starts True — so on the freshly
    shown pill `_ex_style(add=WS_EX_TRANSPARENT)` is never reached and the
    OS-level bit is never applied, even though the object reports
    click-through."""
    pill.set_state("armed", "hold")
    settle(pill, 60)
    assert pill._click_through
    assert native_ex(pill) & WS_EX_TRANSPARENT, (
        f"click-through claimed but WS_EX_TRANSPARENT absent "
        f"(ex style = 0x{native_ex(pill):08X})")


def test_pill_transparent_bit_does_appear_after_a_toggle_cycle(pill):
    """Isolates the cause of the previous scenario: the ctypes call works, it
    is simply never reached until something has first turned click-through
    off. Only a hands-free recording ever does that."""
    pill.set_state("armed", "hold")
    settle(pill, 60)
    pill._set_click_through(False)
    pill._set_click_through(True)
    assert native_ex(pill) & WS_EX_TRANSPARENT


def test_pill_working_states_do_not_expose_invisible_buttons(pill):
    """`paintEvent` draws the tick and cross only while `recording`, but
    `_button_rects()` keys off ACTIVE, which also holds the working states.
    Every frame where those two disagree is an invisible, clickable button
    sitting over the user's window."""
    pill.set_state("recording", "toggle")
    settle(pill, 400)
    assert pill._button_rects()[0] is not None
    pill.set_state("transcribing", "toggle")
    bad = 0
    for _ in range(400):
        pill._tick()
        if pill.state in WORKING and pill._button_rects()[0] is not None:
            bad += 1
    assert bad == 0, f"{bad} frames with a hittable but undrawn tick/cross"


def test_pill_click_during_transcribing_does_not_emit_accepted(pill):
    """The concrete consequence: a click in dead space over a working pill
    fires the 'stop and paste' signal."""
    fired = []
    pill.accepted.connect(lambda: fired.append(1))
    pill.set_state("recording", "toggle")
    settle(pill, 400)
    pill.set_state("transcribing", "toggle")
    pill._tick()
    accept, _ = pill._button_rects()
    if accept is None:
        pytest.skip("no hittable rect in transcribing — nothing to click")
    pos = accept.center()
    pill.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, pos, pos, Qt.LeftButton, Qt.LeftButton,
        Qt.NoModifier))
    assert not fired, "accepted emitted from a click on nothing"


def test_pill_hold_mode_recording_never_becomes_clickable(pill):
    """Push-to-talk draws no controls, so it must never take a click."""
    pill.set_state("recording", "hold")
    for _ in range(600):
        pill._tick()
        assert pill._click_through, "push-to-talk pill swallowed a click"


# ==========================================================================
# 5. Pill — state machine                                         (9)
# ==========================================================================

def test_pill_every_transition_pair_survives(pill):
    for a in ALL_STATES:
        for b in ALL_STATES:
            pill.set_state(a, "toggle")
            settle(pill, 10)
            pill.set_state(b, "toggle")
            settle(pill, 10)
            assert pill.state in ALL_STATES, (a, b)
            assert math.isfinite(pill.width_s.value), (a, b)
            assert math.isfinite(pill.height_s.value), (a, b)
            assert math.isfinite(pill.opacity.value), (a, b)


def test_pill_same_state_repeats_are_idempotent(pill):
    for state in ALL_STATES:
        pill.set_state(state, "toggle")
        settle(pill, 20)                # under the shortest terminal hold (42)
        w, h = pill.width_s.target, pill.height_s.target
        for _ in range(5):
            pill.set_state(state, "toggle")
        assert (pill.width_s.target, pill.height_s.target) == (w, h), state
        assert pill.focusPolicy() == Qt.NoFocus
        assert not pill.isActiveWindow()


def test_pill_done_before_any_recording(pill):
    pill.set_state("done", "toggle")
    settle(pill, 20)
    assert pill.state == "done"
    assert pill.isVisible()
    render(pill)


def test_pill_cancel_with_nothing_recording(pill):
    pill.set_state("cancelled", "hold")
    settle(pill, 20)
    assert pill.state == "cancelled"
    assert pill.shake >= 0.0
    render(pill)


def test_pill_terminal_states_always_return_to_rest(pill):
    """A terminal state must not park the overlay on screen forever."""
    for state in TERMINAL:
        pill.set_state(state, "toggle")
        settle(pill, 600)               # 10 s, past the longest hold (210)
        assert pill.state in ("armed", "off"), f"{state} never settled"


def test_pill_unknown_state_still_returns_to_rest(pill):
    """An unrecognised state name falls through every bucket: it is not in
    TERMINAL, so nothing times it out, and the capsule stays lit at full
    opacity forever."""
    pill.set_state("bogus_state", "toggle")
    settle(pill, 1200)                  # 20 s
    assert pill.state in ("armed", "off"), (
        f"wedged in {pill.state!r} at opacity target {pill.opacity.target}")


def test_pill_rapid_transitions_within_one_frame(pill):
    seq = ["armed", "recording", "transcribing", "polishing", "done",
           "recording", "cancelled", "error", "launching", "armed",
           "recording", "polishing", "copied", "off", "armed"]
    for s in seq:
        pill.set_state(s, "toggle")     # no _tick between any of them
    settle(pill, 300)
    assert pill.state in ALL_STATES
    assert math.isfinite(pill.opacity.value)
    assert not pill.isActiveWindow()


def test_pill_idle_alias_maps_to_armed_or_off(pill):
    pill.set_state("idle")
    assert pill.state == "armed"
    pill.show_when_idle = False
    pill.set_state("recording", "hold")
    pill.set_state("idle")
    assert pill.state == "off"


def test_pill_armed_stops_animating_once_at_rest(pill):
    """Armed paints a static capsule — no bars, no rim, no orb. Holding a
    60fps repaint loop open forever for an image that cannot change is pure
    burn on a window that is always on top."""
    pill.set_state("armed", "hold")
    settle(pill, 900)                   # 15 s
    assert all(s.at_rest for s in
               (pill.opacity, pill.width_s, pill.height_s, pill.slide))
    assert not pill._timer.isActive(), (
        "armed pill still ticking at 60fps with a static image")


def test_pill_off_state_eventually_hides(pill):
    pill.set_state("recording", "hold")
    settle(pill, 60)
    pill.set_state("off")
    settle(pill, 600)
    assert not pill.isVisible()


# ==========================================================================
# 6. Pill — levels, geometry, painting                            (8)
# ==========================================================================

def test_pill_set_level_outside_a_recording_is_harmless(pill):
    pill.set_level(0.9)                 # before anything starts
    pill.set_state("armed", "hold")
    settle(pill, 60)
    assert finite(pill.bars.heights())
    pill.set_state("recording", "hold")
    settle(pill, 60)
    pill.set_state("done", "hold")
    for _ in range(30):                 # and after it ends
        pill.set_level(0.9)
        pill._tick()
    assert finite(pill.bars.heights())


def test_pill_hostile_levels_during_recording(pill):
    for level in (-1.0, 0.0, float("nan"), float("inf"), float("-inf"), 1e9):
        pill.set_state("armed", "hold")
        settle(pill, 60)
        pill.set_state("recording", "hold")
        for _ in range(120):
            pill.set_level(level)
            pill._tick()
        h = pill.bars.heights()
        assert finite(h), f"level={level} produced {h[:3]}"
        assert in_unit(h), f"level={level} produced {h[:3]}"


def test_pill_meter_state_does_not_leak_into_the_next_session(pill):
    """Two carried-over pieces of state, both visible on frame one of a
    back-to-back dictation: `level` is a field holding the last value written
    to it, and the bar springs are not stepped at all outside recording/armed,
    so they freeze at the previous session's waveform. Reproduces the case
    where the user re-triggers inside the ~700ms `done` hold and so never
    passes through `armed`, whose breathe() would have pulled the bars down."""
    pill.set_state("recording", "hold")
    pill.set_level(0.9)
    settle(pill, 120)
    pill.set_state("done", "hold")
    settle(pill, 20)                    # still inside the done hold
    pill.set_state("recording", "hold")
    pill._tick()                        # first frame of the NEW session
    assert max(pill.bars.heights()) < 0.5, (
        f"new session opened at {max(pill.bars.heights()):.2f} with "
        f"pill.level still {pill.level}")


def test_pill_place_keeps_the_widget_on_screen(pill):
    """`_place()` centres by subtracting half the widget height with no clamp,
    so a widget taller than the usable area is pushed off the top edge."""
    pill.resize(4000, 4000)             # stands in for a very small screen
    pill._place()
    geo = QGuiApplication.primaryScreen().availableGeometry()
    assert pill.y() >= geo.top() - 1, (
        f"pill top at y={pill.y()} vs screen top {geo.top()}")


def test_pill_place_pins_the_right_edge(pill):
    pill.set_state("armed", "hold")
    settle(pill, 30)
    geo = pill.screen().availableGeometry()
    assert pill.x() + pill.width() - 1 == geo.right()


def test_pill_paints_without_crashing_in_every_state(pill):
    for state in ALL_STATES:
        pill.set_state(state, "toggle")
        for _ in range(40):
            pill._tick()
            render(pill)


def test_pill_paints_at_a_degenerate_capsule_size(pill):
    """Springs undershoot; the painter must survive a frame where the capsule
    has collapsed to nothing."""
    pill.set_state("recording", "toggle")
    pill.width_s.snap_to(0.0)
    pill.height_s.snap_to(0.0)
    render(pill)


def test_pill_bar_length_is_bounded_for_hostile_values(pill):
    for v in (-5.0, 0.0, 1.0, 12.0, float("inf")):
        L = Pill.bar_length(v, 30.0, 1.0)
        assert math.isfinite(L) and 0.0 < L <= 30.0, f"v={v} -> {L}"


def test_pill_capsule_centre_is_finite_in_every_state(pill):
    for state in ALL_STATES:
        pill.set_state(state, "toggle")
        settle(pill, 20)
        c = pill.capsule_centre()
        assert math.isfinite(c.x()) and math.isfinite(c.y()), state


# ==========================================================================
# 7. Comet                                                        (6)
# ==========================================================================

def test_comet_zero_length_flight_completes(comet):
    """start == aim: atan2(0, 0) is legal, and the window still needs a size."""
    p = QPointF(600.0, 400.0)
    landed = []
    comet.launch(p, p, on_land=lambda: landed.append(1))
    assert comet.width() > 0 and comet.height() > 0
    for _ in range(10):
        comet._tick()
        render(comet)
    for _ in range(400):
        comet._tick()
    assert landed == [1]
    assert not comet.flying


def test_comet_retarget_mid_flight_keeps_the_first_callback(comet):
    """`launch()` overwrites `_on_land` outright. Relaunching before the first
    flight lands silently drops whatever the first caller asked to happen."""
    calls = []
    comet.launch(QPointF(100, 100), QPointF(800, 500),
                 on_land=lambda: calls.append("first"))
    for _ in range(20):
        comet._tick()
    assert comet._phase == "flight"
    comet.launch(QPointF(100, 100), QPointF(200, 900),
                 on_land=lambda: calls.append("second"))
    for _ in range(400):
        comet._tick()
    assert "first" in calls, f"first callback dropped; got {calls}"


def test_comet_retarget_mid_flight_still_completes(comet):
    comet.launch(QPointF(100, 100), QPointF(800, 500))
    for _ in range(20):
        comet._tick()
    comet.launch(QPointF(300, 300), QPointF(400, 400))
    for _ in range(400):
        comet._tick()
    assert not comet.flying
    assert not comet.isVisible()


def test_comet_never_becomes_focusable_or_active(comet, qapp):
    comet.launch(QPointF(100, 100), QPointF(800, 500))
    for _ in range(5):
        comet._tick()
        qapp.processEvents()
        assert comet.focusPolicy() == Qt.NoFocus
        assert not comet.isActiveWindow()
    assert native_ex(comet) & WS_EX_NOACTIVATE
    assert native_ex(comet) & WS_EX_TRANSPARENT


def test_comet_non_finite_start_is_rejected_cleanly(comet):
    """`capsule_centre()` supplies the start point and is derived from springs
    that can go non-finite. `int(nan)` inside `setGeometry` is a hard crash on
    the path that announces a completed dictation."""
    nan = float("nan")
    try:
        comet.launch(QPointF(nan, nan), QPointF(500.0, 500.0))
    except ValueError as e:
        pytest.fail(f"launch crashed on a non-finite start: {e}")


def test_comet_refuses_activation_even_when_asked(comet, qapp):
    """Same rule as the pill, and the comet is on screen at exactly the moment
    the paste is going out."""
    u = ctypes.windll.user32
    comet.launch(QPointF(100, 100), QPointF(800, 500))
    for _ in range(5):
        comet._tick()
    qapp.processEvents()
    hwnd = int(comet.winId())
    comet.raise_()
    comet.activateWindow()
    qapp.processEvents()
    assert u.GetForegroundWindow() != hwnd, "comet took foreground mid-paste"
    assert u.GetFocus() != hwnd, "comet took keyboard focus mid-paste"
    assert not comet.isActiveWindow()
