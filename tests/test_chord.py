"""The chord FSM is the highest-risk logic in the app, so it is a pure function
of key events with no Windows API involved — the whole promotion rule is
testable without a keyboard."""

from murmur.platform.win.chord import Act, ChordFSM, Ev

CTRL, WIN, SPACE, ESC = "ctrl", "win", "space", "esc"


def drive(events, min_session_ms=350):
    """events: list of (kind, key, dt_ms). Returns the flat list of actions."""
    fsm = ChordFSM(min_session_ms=min_session_ms)
    acts, t = [], 0
    for kind, key, dt in events:
        t += dt
        acts += fsm.feed(Ev(kind, key, t))
    return acts


HOLD_DOWN = [("down", CTRL, 0), ("down", WIN, 10)]


def test_hold_then_release_starts_and_stops():
    assert drive(HOLD_DOWN + [("up", WIN, 900)]) == \
        [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_release_of_ctrl_also_stops():
    assert drive(HOLD_DOWN + [("up", CTRL, 900)]) == \
        [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_accidental_tap_below_threshold_discards():
    assert drive(HOLD_DOWN + [("up", WIN, 100)]) == [Act.START_HOLD, Act.DISCARD]


def test_win_pressed_before_ctrl_also_starts():
    assert drive([("down", WIN, 0), ("down", CTRL, 10), ("up", WIN, 900)]) == \
        [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_space_promotes_hold_to_toggle_and_release_does_not_stop():
    assert drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60),
        ("up", WIN, 70), ("up", CTRL, 80),
    ]) == [Act.START_HOLD, Act.PROMOTE_TOGGLE]


def test_second_chord_stops_a_toggle_session():
    assert drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 10), ("down", SPACE, 20),
    ]) == [Act.START_HOLD, Act.PROMOTE_TOGGLE, Act.STOP_AND_TRANSCRIBE]


def test_ctrl_win_during_toggle_does_not_start_a_second_session():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 10),
    ])
    assert acts.count(Act.START_HOLD) == 1


def test_bare_space_during_toggle_without_the_chord_does_not_stop():
    """Typing a space in a hands-free session must not end it."""
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", SPACE, 3000), ("up", SPACE, 60),
    ])
    assert Act.STOP_AND_TRANSCRIBE not in acts


def test_toggle_session_survives_many_stray_keys():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", "a", 100), ("up", "a", 50), ("down", CTRL, 50), ("up", CTRL, 50),
    ])
    assert acts == [Act.START_HOLD, Act.PROMOTE_TOGGLE]


def test_esc_cancels_from_hold_recording():
    assert drive(HOLD_DOWN + [("down", ESC, 500)]) == [Act.START_HOLD, Act.CANCEL]


def test_esc_cancels_from_toggle_recording():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", ESC, 2000),
    ])
    assert acts[-1] == Act.CANCEL


def test_esc_while_idle_emits_nothing():
    assert drive([("down", ESC, 0)]) == []


def test_after_cancel_the_chord_can_start_a_fresh_session():
    acts = drive(HOLD_DOWN + [
        ("down", ESC, 500), ("up", CTRL, 10), ("up", WIN, 10),
        ("down", CTRL, 2000), ("down", WIN, 10), ("up", WIN, 900),
    ])
    assert acts == [Act.START_HOLD, Act.CANCEL, Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_space_alone_without_chord_does_nothing():
    assert drive([("down", SPACE, 0), ("up", SPACE, 50)]) == []


def test_ctrl_alone_does_not_start():
    assert drive([("down", CTRL, 0), ("up", CTRL, 500)]) == []


def test_win_alone_does_not_start():
    assert drive([("down", WIN, 0), ("up", WIN, 500)]) == []


def test_suppression_claims_space_only_while_chord_held():
    fsm = ChordFSM()
    assert fsm.should_suppress("down", SPACE) is False      # nothing held
    fsm.feed(Ev("down", CTRL, 0))
    assert fsm.should_suppress("down", SPACE) is False      # only ctrl
    fsm.feed(Ev("down", WIN, 10))
    assert fsm.should_suppress("down", SPACE) is True       # chord held
    assert fsm.should_suppress("down", ESC) is False        # never swallow esc
    assert fsm.should_suppress("down", "a") is False        # never swallow letters
    fsm.feed(Ev("up", WIN, 20))
    assert fsm.should_suppress("down", SPACE) is False      # released


def test_suppression_covers_the_space_keyup_too():
    """If we swallow the keydown but not the keyup, the target app sees a
    dangling release."""
    fsm = ChordFSM()
    fsm.feed(Ev("down", CTRL, 0))
    fsm.feed(Ev("down", WIN, 10))
    assert fsm.should_suppress("down", SPACE) is True
    assert fsm.should_suppress("up", SPACE) is True


def test_the_space_keyup_is_swallowed_even_if_ctrl_was_released_first():
    """The keyup used to be decided by the live chord state, so letting go of
    Ctrl before Space sent the target app a Space release with no press."""
    fsm = ChordFSM()
    fsm.feed(Ev("down", CTRL, 0))
    fsm.feed(Ev("down", WIN, 10))
    assert fsm.should_suppress("down", SPACE) is True
    fsm.feed(Ev("up", CTRL, 20))                  # chord broken first
    assert fsm.should_suppress("up", SPACE) is True


def test_a_space_we_did_not_swallow_passes_its_keyup_through():
    fsm = ChordFSM()
    assert fsm.should_suppress("down", SPACE) is False
    assert fsm.should_suppress("up", SPACE) is False


def test_an_arm_does_not_survive_letting_go_of_the_chord():
    """Pressing Ctrl+Win during a hands-free session — reaching for
    Ctrl+Win+arrow — used to leave it armed, so the next Space he typed ended
    the session and pasted into whatever had focus. Hands-free is for talking
    WHILE typing."""
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 5005),
        ("up", CTRL, 5010), ("up", WIN, 5015),
        ("down", SPACE, 99999),
    ])
    assert Act.STOP_AND_TRANSCRIBE not in acts
    assert acts == [Act.START_HOLD, Act.PROMOTE_TOGGLE]


def test_autorepeat_of_the_held_chord_cannot_stop_a_just_promoted_session():
    """The chord is still down at the moment of promotion and Windows repeats
    held keys, so a repeat could arm and a second Space stop the session it had
    only just started."""
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50),
        ("down", CTRL, 600), ("down", SPACE, 630),
    ])
    assert acts == [Act.START_HOLD, Act.PROMOTE_TOGGLE]


def test_the_real_stop_chord_still_works_after_a_full_release():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 5005), ("down", SPACE, 5020),
    ])
    assert acts == [Act.START_HOLD, Act.PROMOTE_TOGGLE, Act.STOP_AND_TRANSCRIBE]


def test_repeated_keydown_from_auto_repeat_does_not_restart():
    """Holding the chord generates repeat keydowns; only one session may start."""
    acts = drive([
        ("down", CTRL, 0), ("down", WIN, 10),
        ("down", CTRL, 30), ("down", WIN, 30), ("down", CTRL, 30),
        ("up", WIN, 900),
    ])
    assert acts == [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_space_autorepeat_during_hold_promotes_only_once():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("down", SPACE, 30), ("down", SPACE, 30),
    ])
    assert acts.count(Act.PROMOTE_TOGGLE) == 1


def test_space_autorepeat_while_armed_stops_only_once():
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 10),
        ("down", SPACE, 20), ("down", SPACE, 30), ("down", SPACE, 30),
    ])
    assert acts.count(Act.STOP_AND_TRANSCRIBE) == 1


def test_exactly_at_threshold_transcribes_rather_than_discards():
    # START_HOLD fires at t=10, so dt=350 puts elapsed at exactly the threshold.
    assert drive(HOLD_DOWN + [("up", WIN, 350)], min_session_ms=350) == \
        [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE]


def test_one_ms_below_threshold_discards():
    assert drive(HOLD_DOWN + [("up", WIN, 349)], min_session_ms=350) == \
        [Act.START_HOLD, Act.DISCARD]


def test_autorepeat_after_cancel_does_not_restart_while_chord_still_held():
    """Esc cancels while Ctrl+Win are still physically down. Windows keeps
    sending auto-repeat keydowns for held keys — those must not start a new
    session. The chord has to be fully released first."""
    acts = drive(HOLD_DOWN + [
        ("down", ESC, 500),
        ("down", CTRL, 30), ("down", WIN, 30), ("down", CTRL, 30),
    ])
    assert acts == [Act.START_HOLD, Act.CANCEL]


def test_autorepeat_after_toggle_stop_does_not_restart():
    """Same hazard on the stop path: the chord is held at the moment a
    hands-free session is stopped."""
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 60), ("up", WIN, 70), ("up", CTRL, 80),
        ("down", CTRL, 5000), ("down", WIN, 10), ("down", SPACE, 20),
        ("down", CTRL, 30), ("down", WIN, 30),
    ])
    assert acts.count(Act.START_HOLD) == 1


# --- a quick tap must work, and Windows shortcuts must not dictate ---

def test_a_quick_tap_is_transcribed_not_thrown_away():
    """350ms silently discarded every fast tap-and-talk. The pre-roll means
    even a very short hold carries real audio, so the threshold only has to
    reject a graze."""
    for ms in (120, 150, 200, 300):
        acts = drive(HOLD_DOWN + [("up", WIN, ms)], min_session_ms=120)
        assert acts[-1] is Act.STOP_AND_TRANSCRIBE, f"{ms}ms was discarded"


def test_a_graze_is_still_rejected():
    assert drive(HOLD_DOWN + [("up", WIN, 60)], min_session_ms=120)[-1] is Act.DISCARD


def test_windows_shortcuts_sharing_the_chord_do_not_dictate():
    """Ctrl+Win is the prefix of real Windows shortcuts. Every one of these used
    to start a session and then transcribe whatever the microphone caught."""
    for key in ("d", "right", "left", "f", "enter", "tab"):
        acts = drive(HOLD_DOWN + [
            ("down", key, 60), ("up", key, 60), ("up", WIN, 400), ("up", CTRL, 10),
        ])
        assert Act.STOP_AND_TRANSCRIBE not in acts, f"Ctrl+Win+{key} dictated"
        assert acts[-1] is Act.DISCARD


def test_a_shortcut_pressed_late_still_cancels_the_session():
    """Even past the minimum duration: the user reached for a shortcut."""
    acts = drive(HOLD_DOWN + [("down", "d", 900), ("up", WIN, 100)])
    assert Act.STOP_AND_TRANSCRIBE not in acts


def test_space_is_still_the_promotion_and_not_a_shortcut():
    acts = drive(HOLD_DOWN + [("down", SPACE, 60)])
    assert acts[-1] is Act.PROMOTE_TOGGLE


def test_esc_during_a_hold_still_cancels_rather_than_discards():
    acts = drive(HOLD_DOWN + [("down", ESC, 400)])
    assert acts[-1] is Act.CANCEL


def test_typing_during_a_hands_free_session_does_not_end_it():
    """Hands-free is for talking WHILE working. Only the hold state treats a
    stray key as a shortcut."""
    acts = drive(HOLD_DOWN + [
        ("down", SPACE, 50), ("up", SPACE, 20), ("up", WIN, 20), ("up", CTRL, 20),
        ("down", "a", 300), ("up", "a", 40),
        ("down", "e", 40), ("up", "e", 40),
    ])
    assert Act.DISCARD not in acts
    assert acts == [Act.START_HOLD, Act.PROMOTE_TOGGLE]
