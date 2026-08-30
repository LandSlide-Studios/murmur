"""Adversarial edge cases for the Ctrl+Win chord state machine.

Written air-gapped: the expectations below are derived only from
``murmur/platform/win/chord.py``, ``murmur/platform/win/hotkey.py`` and their
docstrings, plus the stated product intent:

  * holding Ctrl+Win is push-to-talk, release transcribes;
  * Space while Ctrl+Win are held promotes to a hands-free session that keeps
    recording after the keys go up, and the chord again stops it;
  * Esc cancels;
  * Ctrl+Win is the prefix of real Windows shortcuts (Ctrl+Win+D, +arrows, +F)
    and those must never produce a dictation.

Tests named ``test_..._must_...`` assert the *intended* contract and are the
ones expected to expose defects. Tests named ``test_..._documents_...`` pin
down current behaviour that is surprising but arguably by design.
"""

import ctypes
import itertools
import sys

import pytest

from murmur.platform.win.chord import CHORD_KEYS, MODIFIERS, Act, ChordFSM, Ev, St

ENDERS = (Act.STOP_AND_TRANSCRIBE, Act.DISCARD, Act.CANCEL)


def D(key, t):
    return Ev("down", key, t)


def U(key, t):
    return Ev("up", key, t)


def drive(f, evs):
    """Feed a list of events, returning the flat list of actions emitted."""
    out = []
    for e in evs:
        out.extend(f.feed(e))
    return out


def press_chord(f, t=0):
    """Ctrl then Win down. Returns the actions."""
    return drive(f, [D("ctrl", t), D("win", t + 5)])


# ---------------------------------------------------------------------------
# Controls. If these fail, the rest of the file means nothing.
# ---------------------------------------------------------------------------

def test_control_push_to_talk_happy_path():
    f = ChordFSM(min_session_ms=120)
    assert f.feed(D("ctrl", 0)) == []
    assert f.feed(D("win", 10)) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 500)) == [Act.STOP_AND_TRANSCRIBE]
    assert f.feed(U("win", 505)) == []
    assert f.state is St.IDLE


def test_control_promotion_happy_path():
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 0), D("win", 5), D("space", 40)]) == [
        Act.START_HOLD,
        Act.PROMOTE_TOGGLE,
    ]
    # Hands come off the keyboard; the session must survive.
    assert drive(f, [U("space", 60), U("ctrl", 70), U("win", 75)]) == []
    assert f.state is St.REC_TOGGLE
    # Chord again stops it.
    assert drive(f, [D("ctrl", 9000), D("win", 9005), D("space", 9010)]) == [
        Act.STOP_AND_TRANSCRIBE
    ]
    assert f.state is St.IDLE


def test_control_esc_cancels_a_hold():
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(D("esc", 300)) == [Act.CANCEL]
    assert f.state is St.IDLE


# ---------------------------------------------------------------------------
# Events out of order, unmatched ups, keys held from before.
# ---------------------------------------------------------------------------

def test_win_before_ctrl_starts_on_the_second_modifier():
    f = ChordFSM(min_session_ms=120)
    assert f.feed(D("win", 0)) == []
    assert f.feed(D("ctrl", 3)) == [Act.START_HOLD]
    assert f.feed(U("win", 400)) == [Act.STOP_AND_TRANSCRIBE]


def test_key_up_with_no_matching_key_down_is_inert():
    f = ChordFSM(min_session_ms=120)
    for k in ("ctrl", "win", "space", "esc", "x"):
        assert f.feed(U(k, 0)) == []
    assert f.state is St.IDLE
    assert f.held == set()
    # ...and the machine still works afterwards.
    assert press_chord(f, 100) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 900)) == [Act.STOP_AND_TRANSCRIBE]


def test_space_already_held_before_the_chord_does_not_promote():
    """Space physically down first: no space keydown arrives during the hold,
    so the session stays a hold and ends on release."""
    f = ChordFSM(min_session_ms=120)
    assert f.feed(D("space", 0)) == []
    assert press_chord(f, 100) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 800)) == [Act.STOP_AND_TRANSCRIBE]


def test_space_held_from_before_documents_promotion_on_autorepeat():
    """Flip side of the above: the repeat of an already-held Space promotes."""
    f = ChordFSM(min_session_ms=120)
    f.feed(D("space", 0))
    assert press_chord(f, 100) == [Act.START_HOLD]
    assert f.feed(D("space", 600)) == [Act.PROMOTE_TOGGLE]


def test_esc_up_alone_never_emits():
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(U("esc", 300)) == []
    assert f.state is St.REC_HOLD


def test_space_up_during_a_hold_is_inert():
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(U("space", 200)) == []
    assert f.feed(U("win", 400)) == [Act.STOP_AND_TRANSCRIBE]


# ---------------------------------------------------------------------------
# Timestamps: zero, boundary, repeated, backwards, enormous.
# ---------------------------------------------------------------------------

def test_all_timestamps_zero_discards_a_hold():
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 0), D("win", 0), U("ctrl", 0), U("win", 0)]) == [
        Act.START_HOLD,
        Act.DISCARD,
    ]


def test_all_timestamps_zero_with_zero_minimum_transcribes():
    f = ChordFSM(min_session_ms=0)
    assert drive(f, [D("ctrl", 0), D("win", 0), U("ctrl", 0)]) == [
        Act.START_HOLD,
        Act.STOP_AND_TRANSCRIBE,
    ]


def test_elapsed_exactly_min_session_ms_transcribes():
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 0), D("win", 1000)]) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 1120)) == [Act.STOP_AND_TRANSCRIBE]


def test_elapsed_one_ms_below_min_session_ms_discards():
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 0), D("win", 1000)]) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 1119)) == [Act.DISCARD]


def test_hold_is_timed_from_the_second_modifier_documents_rule():
    """started_at is the chord-completion event, so a slow two-finger press
    does not bank time."""
    f = ChordFSM(min_session_ms=120)
    f.feed(D("ctrl", 0))
    assert f.feed(D("win", 5000)) == [Act.START_HOLD]
    assert f.feed(U("win", 5100)) == [Act.DISCARD]


def test_backwards_timestamp_documents_discard():
    """A clock that goes backwards makes elapsed negative, which reads as
    'too short'. Silent loss, but the safe direction."""
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 10_000), D("win", 10_005)]) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 9_000)) == [Act.DISCARD]


def test_enormous_timestamps_still_transcribe():
    big = 2 ** 53
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", big), D("win", big)]) == [Act.START_HOLD]
    assert f.feed(U("ctrl", big + 121)) == [Act.STOP_AND_TRANSCRIBE]


def test_dword_wraparound_documents_discard():
    """hotkey.py feeds perf_counter ms so this cannot happen there, but the FSM
    accepts any int: a wrap discards rather than transcribing garbage."""
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 2 ** 32 - 5), D("win", 2 ** 32 - 5)])
    assert f.feed(U("ctrl", 3)) == [Act.DISCARD]


# ---------------------------------------------------------------------------
# Duplicate events and auto-repeat storms.
# ---------------------------------------------------------------------------

def test_duplicate_chord_down_emits_exactly_one_start():
    f = ChordFSM(min_session_ms=120)
    acts = drive(f, [D("ctrl", 0), D("win", 5), D("ctrl", 6), D("win", 7)])
    assert acts == [Act.START_HOLD]


def test_the_very_same_event_object_twice_is_idempotent():
    f = ChordFSM(min_session_ms=120)
    e = D("win", 5)
    f.feed(D("ctrl", 0))
    assert f.feed(e) == [Act.START_HOLD]
    assert f.feed(e) == []
    assert f.state is St.REC_HOLD


def test_autorepeat_storm_during_a_hold_emits_nothing_extra():
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    storm = [D(k, 500 + i) for i, k in enumerate(("ctrl", "win") * 200)]
    assert drive(f, storm) == []
    assert f.state is St.REC_HOLD
    assert f.feed(U("ctrl", 2000)) == [Act.STOP_AND_TRANSCRIBE]


def test_duplicate_modifier_up_emits_one_stop():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(U("ctrl", 900)) == [Act.STOP_AND_TRANSCRIBE]
    assert f.feed(U("ctrl", 901)) == []
    assert f.feed(U("win", 902)) == []


def test_both_modifiers_up_in_the_same_millisecond_emits_one_stop():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert drive(f, [U("ctrl", 900), U("win", 900)]) == [Act.STOP_AND_TRANSCRIBE]


def test_both_modifiers_up_same_ms_below_min_emits_one_discard():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert drive(f, [U("win", 50), U("ctrl", 50)]) == [Act.DISCARD]


@pytest.mark.parametrize("order", list(itertools.permutations(("ctrl", "win"))))
def test_every_release_permutation_of_a_hold_yields_one_stop(order):
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    acts = drive(f, [U(k, 900 + i) for i, k in enumerate(order)])
    assert acts == [Act.STOP_AND_TRANSCRIBE]
    assert f.state is St.IDLE


@pytest.mark.parametrize("order", list(itertools.permutations(("ctrl", "win", "space"))))
def test_every_release_permutation_after_promotion_keeps_the_session(order):
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40)])
    acts = drive(f, [U(k, 100 + i) for i, k in enumerate(order)])
    assert acts == []
    assert f.state is St.REC_TOGGLE


# ---------------------------------------------------------------------------
# Esc from every reachable state.
# ---------------------------------------------------------------------------

def test_esc_in_idle_emits_nothing():
    f = ChordFSM(min_session_ms=120)
    assert f.feed(D("esc", 0)) == []
    assert f.state is St.IDLE


def test_esc_cancels_a_promoted_session_with_keys_still_down():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40)])
    assert f.feed(D("esc", 60)) == [Act.CANCEL]
    assert f.state is St.IDLE


def test_esc_cancels_a_hands_free_session_after_the_keys_are_released():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    assert f.feed(D("esc", 30_000)) == [Act.CANCEL]
    assert f.state is St.IDLE


def test_esc_cancels_an_adopted_session():
    f = ChordFSM(min_session_ms=120)
    f.adopt_toggle_session()
    assert f.state is St.REC_TOGGLE
    assert f.feed(D("esc", 500)) == [Act.CANCEL]
    assert f.state is St.IDLE


def test_esc_twice_emits_once():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(D("esc", 300)) == [Act.CANCEL]
    assert f.feed(D("esc", 320)) == []


def test_esc_while_the_chord_is_still_held_blocks_an_immediate_restart():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(D("esc", 300)) == [Act.CANCEL]
    # Auto-repeat of the still-held chord must not start a new session.
    assert drive(f, [D("ctrl", 320), D("win", 330), D("ctrl", 340)]) == []
    assert f.state is St.IDLE


def test_esc_does_not_poison_the_next_session():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    f.feed(D("esc", 300))
    drive(f, [U("esc", 310), U("ctrl", 320), U("win", 330)])
    assert press_chord(f, 1000) == [Act.START_HOLD]
    assert f.feed(U("win", 2000)) == [Act.STOP_AND_TRANSCRIBE]


# ---------------------------------------------------------------------------
# Windows-shortcut prefixes and stray modifiers (FSM level).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["d", "f", "left", "right", "shift", "alt", "tab"])
def test_any_foreign_key_joining_a_hold_discards(key):
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(D(key, 200)) == [Act.DISCARD]
    assert f.state is St.IDLE


def test_foreign_key_up_only_during_a_hold_is_inert():
    """Only a keydown means the user reached for a shortcut."""
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(U("d", 200)) == []
    assert f.state is St.REC_HOLD


def test_typing_during_a_hands_free_session_never_ends_it():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    typing = []
    for i, k in enumerate("hello world this is a test"):
        typing += [D(k, 1000 + i * 10), U(k, 1005 + i * 10)]
    assert drive(f, typing) == []
    assert f.state is St.REC_TOGGLE


def test_ctrl_win_arrow_repeated_never_transcribes():
    """Switching virtual desktops several times in a row: no dictation, ever."""
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    acts = []
    for i in range(4):
        acts += drive(f, [D("left", 200 + i * 100), U("left", 250 + i * 100)])
    acts += drive(f, [U("ctrl", 900), U("win", 905)])
    assert acts == [Act.DISCARD]


def test_discard_then_chord_autorepeat_does_not_restart():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(D("d", 200)) == [Act.DISCARD]
    storm = [D(k, 300 + i) for i, k in enumerate(("ctrl", "win") * 50)]
    assert drive(f, storm) == []
    assert f.state is St.IDLE


def test_block_clears_only_once_the_chord_is_broken():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    f.feed(D("d", 200))                       # DISCARD, blocked
    assert f.feed(U("d", 210)) == []          # d up does not unblock
    assert f.feed(D("ctrl", 220)) == []       # still blocked
    assert f.feed(U("ctrl", 300)) == []       # chord broken -> unblocked
    assert f.feed(D("ctrl", 400)) == [Act.START_HOLD]


def test_a_never_released_chord_stays_recording():
    """No wall-clock cap in hold mode: the finger is the stop."""
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert drive(f, [D("ctrl", 1000 + i * 500) for i in range(200)]) == []
    assert f.state is St.REC_HOLD


# ---------------------------------------------------------------------------
# Promotion / toggle-stop edge cases. The suspected defects live here.
# ---------------------------------------------------------------------------

def test_promoting_space_does_not_immediately_stop():
    f = ChordFSM(min_session_ms=120)
    assert drive(f, [D("ctrl", 0), D("win", 5), D("space", 40)]) == [
        Act.START_HOLD,
        Act.PROMOTE_TOGGLE,
    ]
    assert f.state is St.REC_TOGGLE


def test_autorepeat_of_the_held_chord_must_not_stop_a_just_promoted_session():
    """Nothing has been released since the promotion, so the user has not
    performed the 'press the chord again' gesture. IDLE has
    ``_blocked_until_release`` for exactly this reason; REC_TOGGLE has no
    equivalent, so a repeat of Ctrl or Win re-arms the stop and the next Space
    repeat ends the session with ~0 audio."""
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40)])
    storm = [D(k, 600 + i * 30) for i, k in enumerate(("ctrl", "win", "space") * 3)]
    acts = drive(f, storm)
    assert acts == [], "auto-repeat of the still-held chord ended the session: %r" % acts


def test_arm_must_not_survive_the_release_of_the_chord():
    """Ctrl+Win pressed during a hands-free session (a Windows shortcut, or a
    change of mind) arms the stop permanently: ``armed_for_stop`` is never
    cleared on release. The next Space the user *types* then ends the session
    and injects a transcript."""
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    # Ctrl+Win+arrow mid-session: switch desktops, no Space involved.
    drive(f, [D("ctrl", 5000), D("win", 5005), D("left", 5010), U("left", 5020),
              U("ctrl", 5030), U("win", 5035)])
    # Much later, in the app being dictated into, the user types a space.
    acts = f.feed(D("space", 30_000))
    assert acts == [], "a bare Space ended the hands-free session: %r" % acts
    assert f.state is St.REC_TOGGLE


def test_second_space_while_holding_the_chord_documents_a_race():
    """Whether 'press the chord again' works without releasing anything depends
    entirely on whether an OS auto-repeat of Ctrl or Win happened to land in
    between. Same physical gesture, two different outcomes."""
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40), U("space", 50)])
    assert f.feed(D("space", 60)) == []          # no repeat yet: inert

    g = ChordFSM(min_session_ms=120)
    drive(g, [D("ctrl", 0), D("win", 5), D("space", 40), U("space", 50)])
    assert g.feed(D("ctrl", 55)) == []           # one repeat of a held modifier
    assert g.feed(D("space", 60)) == [Act.STOP_AND_TRANSCRIBE]


def test_bare_space_without_arming_is_inert_control():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    assert f.feed(D("space", 30_000)) == []
    assert f.state is St.REC_TOGGLE


def test_toggle_stop_must_require_the_chord_to_be_held_at_the_moment_of_space():
    """The stop gesture is Ctrl+Win+Space. Arming and then releasing both
    modifiers before the Space is not that gesture."""
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    drive(f, [D("ctrl", 9000), D("win", 9005), U("ctrl", 9100), U("win", 9105)])
    acts = f.feed(D("space", 9200))
    assert acts == [], "Space alone stopped the session after the chord was released"


def test_partial_chord_does_not_arm_a_hands_free_session():
    f = ChordFSM(min_session_ms=120)
    f.adopt_toggle_session()
    drive(f, [D("ctrl", 100), U("ctrl", 120)])   # ctrl alone, never a chord
    assert f.feed(D("space", 200)) == []
    assert f.state is St.REC_TOGGLE


def test_toggle_stop_documents_no_minimum_session():
    """min_session_ms guards only the hold path; an armed toggle stops at 0ms."""
    f = ChordFSM(min_session_ms=100_000)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    acts = drive(f, [D("ctrl", 70), D("win", 71), D("space", 72)])
    assert acts == [Act.STOP_AND_TRANSCRIBE]


def test_promotion_then_stop_then_restart_needs_a_release_first():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40),
              U("space", 50), U("ctrl", 60), U("win", 65)])
    drive(f, [D("ctrl", 9000), D("win", 9005)])
    assert f.feed(D("space", 9010)) == [Act.STOP_AND_TRANSCRIBE]
    # Chord still physically down: no new session until it is released.
    assert drive(f, [D("ctrl", 9020), D("win", 9030)]) == []
    drive(f, [U("space", 9040), U("ctrl", 9050), U("win", 9060)])
    assert press_chord(f, 10_000) == [Act.START_HOLD]


def test_adopt_toggle_session_clears_a_pending_block():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    f.feed(D("d", 200))                      # DISCARD, blocked, chord still down
    f.adopt_toggle_session()
    assert f.state is St.REC_TOGGLE
    assert f._blocked_until_release is False
    assert f.armed_for_stop is False


def test_adopted_session_is_stoppable_by_the_chord():
    f = ChordFSM(min_session_ms=120)
    f.adopt_toggle_session()
    acts = drive(f, [D("ctrl", 100), D("win", 105), D("space", 110)])
    assert acts == [Act.STOP_AND_TRANSCRIBE]


# ---------------------------------------------------------------------------
# Stuck / lost key-ups.
# ---------------------------------------------------------------------------

def test_lost_modifier_up_documents_a_phantom_chord():
    """If a keyup is lost (focus change, UAC prompt, RDP), `held` never clears
    and a *single* later modifier press completes the chord on its own."""
    f = ChordFSM(min_session_ms=120)
    f.feed(D("win", 0))          # win up never arrives
    assert f.feed(D("ctrl", 60_000)) == [Act.START_HOLD]


def test_hands_free_survives_a_stuck_modifier_and_is_still_stoppable():
    f = ChordFSM(min_session_ms=120)
    drive(f, [D("ctrl", 0), D("win", 5), D("space", 40), U("space", 50)])
    # ctrl up lost; win released normally.
    f.feed(U("win", 60))
    assert f.state is St.REC_TOGGLE
    assert drive(f, [D("win", 5000), D("space", 5010)]) == [Act.STOP_AND_TRANSCRIBE]


# ---------------------------------------------------------------------------
# should_suppress.
# ---------------------------------------------------------------------------

def test_space_is_suppressed_only_while_the_chord_is_held():
    f = ChordFSM(min_session_ms=120)
    assert f.should_suppress("down", "space") is False
    press_chord(f)
    assert f.should_suppress("down", "space") is True
    assert f.should_suppress("up", "space") is True
    f.feed(U("ctrl", 500))
    assert f.should_suppress("down", "space") is False


@pytest.mark.parametrize("key", ["esc", "ctrl", "win", "d", "left"])
def test_nothing_but_space_is_ever_suppressed(key):
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.should_suppress("down", key) is False
    assert f.should_suppress("up", key) is False


def test_suppressed_space_keydown_must_not_leave_a_dangling_keyup():
    """chord.py: 'Swallow Space, both down and up, ... so the target app never
    sees a dangling keyup.' Suppression is decided from the *current* held set,
    so releasing the modifiers before Space lets the keyup through even though
    the keydown was swallowed."""
    f = ChordFSM(min_session_ms=120)
    seq = [D("ctrl", 0), D("win", 5), D("space", 40),
           U("ctrl", 60), U("win", 65), U("space", 70)]
    passed_through = []
    for ev in seq:
        # hotkey.py asks first, then feeds.
        if not f.should_suppress(ev.kind, ev.key):
            passed_through.append((ev.kind, ev.key))
        f.feed(ev)
    downs = [k for kind, k in passed_through if kind == "down"]
    ups = [k for kind, k in passed_through if kind == "up"]
    assert "space" not in ups or "space" in downs, (
        "app receives a space keyup with no keydown: %r" % passed_through
    )


# ---------------------------------------------------------------------------
# Hook layer: what the FSM actually receives in production.
# ---------------------------------------------------------------------------

pytestmark_win = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")


def _listener(min_session_ms=120):
    from murmur.platform.win import hotkey

    lst = hotkey.HotkeyListener(min_session_ms=min_session_ms)
    clock = {"t": 0}
    lst._ms = lambda: clock["t"]
    return lst, clock


def _hook(lst, vk, kind, flags=0):
    """Drive one key through HotkeyListener._proc, as Windows would."""
    from murmur.platform.win import hotkey

    kb = hotkey.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0,
                                dwExtraInfo=None)
    wparam = hotkey.WM_KEYDOWN if kind == "down" else hotkey.WM_KEYUP
    lparam = ctypes.cast(ctypes.pointer(kb), ctypes.c_void_p).value
    return lst._proc(0, wparam, lparam)


def _drain(lst):
    out = []
    while not lst.actions.empty():
        out.append(lst.actions.get_nowait())
    return out


VK_CTRL, VK_WIN, VK_SPACE, VK_ESC = 0xA2, 0x5B, 0x20, 0x1B
VK_D, VK_LEFT, VK_SHIFT, VK_ALT, VK_F = 0x44, 0x25, 0xA0, 0xA4, 0x46


@pytestmark_win
def test_hook_control_a_real_hold_reaches_the_fsm():
    """Harness check: if this fails, every hook test below is meaningless."""
    lst, clock = _listener()
    _hook(lst, VK_CTRL, "down")
    _hook(lst, VK_WIN, "down")
    assert _drain(lst) == [Act.START_HOLD]
    clock["t"] = 900
    _hook(lst, VK_CTRL, "up")
    assert _drain(lst) == [Act.STOP_AND_TRANSCRIBE]


@pytestmark_win
def test_hook_injected_events_are_ignored():
    from murmur.platform.win import hotkey

    lst, _ = _listener()
    _hook(lst, VK_CTRL, "down", flags=hotkey.LLKHF_INJECTED)
    _hook(lst, VK_WIN, "down", flags=hotkey.LLKHF_INJECTED)
    assert _drain(lst) == []


@pytestmark_win
def test_hook_suppresses_space_only_inside_the_chord():
    lst, _ = _listener()
    assert _hook(lst, VK_SPACE, "down") != 1
    _hook(lst, VK_SPACE, "up")
    _hook(lst, VK_CTRL, "down")
    _hook(lst, VK_WIN, "down")
    assert _hook(lst, VK_SPACE, "down") == 1
    assert _hook(lst, VK_ESC, "down") != 1


@pytestmark_win
@pytest.mark.parametrize(
    "vk,name", [(VK_D, "Ctrl+Win+D"), (VK_LEFT, "Ctrl+Win+Left"),
                (VK_F, "Ctrl+Win+F"), (VK_SHIFT, "Ctrl+Win+Shift"),
                (VK_ALT, "Ctrl+Win+Alt")],
)
def test_hook_windows_shortcuts_must_not_produce_a_dictation(vk, name):
    """chord.py discards a hold when a foreign key joins, but hotkey._proc
    drops every vkCode missing from VK_MAP *before* feeding the FSM, so that
    branch can never fire in production and the shortcut transcribes."""
    lst, clock = _listener(min_session_ms=120)
    _hook(lst, VK_CTRL, "down")
    _hook(lst, VK_WIN, "down")
    clock["t"] = 300
    _hook(lst, vk, "down")
    clock["t"] = 400
    _hook(lst, vk, "up")
    clock["t"] = 900
    _hook(lst, VK_CTRL, "up")
    _hook(lst, VK_WIN, "up")
    acts = _drain(lst)
    assert Act.STOP_AND_TRANSCRIBE not in acts, "%s transcribed: %r" % (name, acts)


@pytestmark_win
def test_hook_vk_map_can_never_deliver_a_foreign_key():
    """The structural version of the test above."""
    from murmur.platform.win import hotkey

    delivered = set(hotkey.VK_MAP.values())
    assert delivered - set(CHORD_KEYS) == set()
    # ...therefore REC_HOLD's foreign-key DISCARD branch is unreachable.
    assert set(CHORD_KEYS) - delivered == set(), (
        "VK_MAP delivers only %r, so no key can ever trigger DISCARD" % delivered
    )


# ---------------------------------------------------------------------------
# Brute force: every short event sequence, universal invariants.
# ---------------------------------------------------------------------------

ALPHABET = [(kind, key)
            for key in ("ctrl", "win", "space", "esc", "x")
            for kind in ("down", "up")]


def _all_sequences(max_len):
    for n in range(1, max_len + 1):
        for combo in itertools.product(ALPHABET, repeat=n):
            yield combo


def _replay(f, combo, step=200):
    """Feed a sequence, returning (actions, per-event actions)."""
    per_event = []
    for i, (kind, key) in enumerate(combo):
        per_event.append(f.feed(Ev(kind, key, i * step)))
    return [a for step_acts in per_event for a in step_acts], per_event


def _recover(f, t0=10 ** 6):
    """Release everything, cancel any live session, then run a clean hold."""
    t = t0
    for k in ("x", "space", "esc", "ctrl", "win"):
        f.feed(U(k, t))
        t += 1
    f.feed(D("esc", t))
    t += 1
    f.feed(U("esc", t))
    t += 1
    acts = drive(f, [D("ctrl", t), D("win", t + 1)])
    acts += f.feed(U("ctrl", t + 10_000))
    return acts


@pytest.mark.parametrize("min_ms", [0, 120, 10_000])
def test_bruteforce_no_action_pair_violates_session_bookkeeping(min_ms):
    """Universal invariants over every sequence of <= 4 events:
      * at most one action per event;
      * no PROMOTE/STOP/DISCARD/CANCEL without a live session;
      * never two STARTs without an intervening end;
      * PROMOTE only from a hold, and at most once per session;
      * the action history and the state always agree.
    """
    for combo in _all_sequences(4):
        f = ChordFSM(min_session_ms=min_ms)
        _, per_event = _replay(f, combo)
        live = False
        promoted = False
        for acts, ev in zip(per_event, combo):
            assert len(acts) <= 1, (combo, ev, acts)
            for a in acts:
                if a is Act.START_HOLD:
                    assert not live, ("double start", combo)
                    live, promoted = True, False
                elif a is Act.PROMOTE_TOGGLE:
                    assert live, ("promote with no session", combo)
                    assert not promoted, ("double promote", combo)
                    promoted = True
                else:
                    assert a in ENDERS, (combo, a)
                    assert live, ("end with no session", combo, a)
                    live = False
        assert live == (f.state is not St.IDLE), (combo, f.state)
        if f.state is St.IDLE:
            assert f.armed_for_stop is False, combo


@pytest.mark.parametrize("min_ms", [0, 120])
def test_bruteforce_every_state_is_recoverable(min_ms):
    """No reachable state is a dead end: after releasing every key and pressing
    Esc, a clean Ctrl+Win hold must start and transcribe again."""
    for combo in _all_sequences(4):
        f = ChordFSM(min_session_ms=min_ms)
        _replay(f, combo)
        assert _recover(f) == [Act.START_HOLD, Act.STOP_AND_TRANSCRIBE], combo


def test_bruteforce_stop_and_transcribe_always_follows_a_start():
    """The strongest single invariant: a transcription is never emitted for a
    session that never started."""
    for combo in _all_sequences(4):
        f = ChordFSM(min_session_ms=120)
        acts, _ = _replay(f, combo)
        seen_start = False
        for a in acts:
            if a is Act.START_HOLD:
                seen_start = True
            if a is Act.STOP_AND_TRANSCRIBE:
                assert seen_start, combo
                seen_start = False


def test_bruteforce_a_session_that_never_saw_space_never_becomes_hands_free():
    """Promotion is Space-only: no sequence without a space keydown may leave
    the machine recording after every key is up."""
    for combo in _all_sequences(4):
        if any(key == "space" and kind == "down" for kind, key in combo):
            continue
        f = ChordFSM(min_session_ms=120)
        _replay(f, combo)
        for k in ("ctrl", "win", "space", "esc", "x"):
            f.feed(U(k, 500_000))
        assert f.state is St.IDLE, combo


def test_bruteforce_hold_stops_always_respect_min_session_ms():
    """A push-to-talk session shorter than the minimum must never transcribe.
    (Promoted sessions are excluded: the toggle stop has no minimum.)"""
    min_ms, step = 300, 200
    for combo in _all_sequences(4):
        f = ChordFSM(min_session_ms=min_ms)
        started_at = None
        promoted = False
        for i, (kind, key) in enumerate(combo):
            t = i * step
            for a in f.feed(Ev(kind, key, t)):
                if a is Act.START_HOLD:
                    started_at, promoted = t, False
                elif a is Act.PROMOTE_TOGGLE:
                    promoted = True
                elif a is Act.STOP_AND_TRANSCRIBE and not promoted:
                    assert t - started_at >= min_ms, (combo, t, started_at)
                    started_at = None


def test_unknown_event_kind_documents_a_half_open_hold():
    """Ev.kind is documented as 'down' | 'up' and hotkey.py only ever produces
    those two, so this is hypothetical. But any other value drops the key from
    `held` while matching neither branch: the machine stays in REC_HOLD with the
    chord no longer down, recording until a real modifier keyup arrives."""
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(Ev("repeat", "ctrl", 200)) == []
    assert f.state is St.REC_HOLD
    assert f._chord_down() is False
    assert f.feed(U("win", 900)) == [Act.STOP_AND_TRANSCRIBE]


def test_rapid_consecutive_holds_are_not_blocked():
    """Releasing one modifier ends the session; pressing it again immediately
    must start a new one (nothing here should latch the block)."""
    f = ChordFSM(min_session_ms=120)
    assert press_chord(f) == [Act.START_HOLD]
    assert f.feed(U("win", 900)) == [Act.STOP_AND_TRANSCRIBE]
    assert f.feed(D("win", 901)) == [Act.START_HOLD]
    assert f.feed(U("win", 1800)) == [Act.STOP_AND_TRANSCRIBE]


def test_a_discarded_short_tap_does_not_block_the_next_hold():
    f = ChordFSM(min_session_ms=120)
    press_chord(f)
    assert f.feed(U("ctrl", 50)) == [Act.DISCARD]
    assert f.feed(D("ctrl", 60)) == [Act.START_HOLD]
    assert f.feed(U("ctrl", 900)) == [Act.STOP_AND_TRANSCRIBE]


def test_bruteforce_held_set_matches_the_events_fed():
    for combo in _all_sequences(3):
        f = ChordFSM(min_session_ms=120)
        expected = set()
        for kind, key in combo:
            if kind == "down":
                expected.add(key)
            else:
                expected.discard(key)
        _replay(f, combo)
        assert f.held == expected, combo
