"""Chord state machine.

Pure function of key events — no Windows API here, so the promotion rule is
fully testable without a keyboard. The hook layer feeds it events and acts on
the actions it returns.

The design problem this solves: `Ctrl+Win+Space` contains `Ctrl+Win`, so pressing
the toggle chord necessarily passes through the hold chord. Rather than guessing
intent from a timing window, recording starts immediately on `Ctrl+Win` and is
*promoted* to a toggle session if `Space` arrives while they are still held. The
buffer is kept either way; only the stop condition changes.
"""

from dataclasses import dataclass
from enum import Enum, auto

MODIFIERS = ("ctrl", "win")
# Keys that legitimately appear during a hold. Anything else means the user
# is reaching for a Windows shortcut, not dictating.
CHORD_KEYS = ("ctrl", "win", "space", "esc")


class Act(Enum):
    START_HOLD = auto()
    PROMOTE_TOGGLE = auto()
    STOP_AND_TRANSCRIBE = auto()
    DISCARD = auto()
    CANCEL = auto()


class St(Enum):
    IDLE = auto()
    REC_HOLD = auto()
    REC_TOGGLE = auto()


@dataclass(frozen=True)
class Ev:
    kind: str   # "down" | "up"
    key: str    # "ctrl" | "win" | "space" | "esc" | anything else
    t_ms: int


class ChordFSM:
    def __init__(self, min_session_ms: int = 120):
        self.min_session_ms = min_session_ms
        self.state = St.IDLE
        self.held: set[str] = set()
        self.started_at = 0
        self.armed_for_stop = False
        # Set when a session ends while the chord is still physically down.
        # Windows auto-repeats keydown for held keys, which would otherwise
        # immediately start a new session. Cleared once the chord is released.
        self._blocked_until_release = False

    def _chord_down(self) -> bool:
        return all(m in self.held for m in MODIFIERS)

    def should_suppress(self, kind: str, key: str) -> bool:
        """Swallow Space, both down and up, only while Ctrl+Win are held — so
        Win+Space never reaches the input-language switcher, and the target app
        never sees a dangling keyup. Esc and everything else always pass through.

        The keyup is decided by whether we swallowed its keydown, NOT by whether
        the chord is still held. Reading the live chord state meant releasing
        Ctrl before Space sent the target app a Space release with no press.
        """
        if key != "space":
            return False
        if kind == "down":
            self._space_swallowed = self._chord_down()
            return self._space_swallowed
        if self._space_swallowed:
            self._space_swallowed = False
            return True
        return False

    def wants_other_keys(self) -> bool:
        """Whether a key outside the chord is worth delivering.

        Only a push-to-talk hold cares: a key joining it means the user reached
        for a Windows shortcut. The hook consults this so it does not pay for an
        FSM step on every keystroke the user ever types.
        """
        return self.state is St.REC_HOLD

    def adopt_toggle_session(self) -> None:
        """Mark a session started outside the chords as live.

        Pressing the desktop shortcut while Murmur is running begins a hands-free
        session directly. Without this the FSM stays IDLE, so Esc emits nothing
        and the session can only be stopped by the chord or the 90s silence
        timeout.
        """
        self.state = St.REC_TOGGLE
        self.armed_for_stop = False
        self._blocked_until_release = False

    def release_session(self) -> None:
        """Mark the live session over when something OTHER than a chord ended it.

        The counterpart to `adopt_toggle_session`. There was one on the way in
        and none on the way out, so a hands-free session ended by the silence
        auto-stop -- or by the tick on the pill -- left the FSM believing it was
        still recording. Every later Esc then emitted a real CANCEL into an app
        with nothing recording, which is what made the cancel fallback in
        `app.py` reachable from the keyboard during an ordinary session.

        Emits nothing: the session has already been dealt with by the caller.
        Idempotent, so the chord path (which has already ended it through
        `_end`) can call it too without caring.
        """
        if self.state not in (St.REC_HOLD, St.REC_TOGGLE):
            return
        self.state = St.IDLE
        self.armed_for_stop = False
        if self._chord_down():
            self._blocked_until_release = True

    def _end(self, act: Act) -> list[Act]:
        self.state = St.IDLE
        self.armed_for_stop = False
        if self._chord_down():
            self._blocked_until_release = True
        return [act]

    def feed(self, ev: Ev) -> list[Act]:
        if ev.kind == "down":
            self.held.add(ev.key)
        else:
            self.held.discard(ev.key)

        if not self._chord_down():
            self._blocked_until_release = False
            # An arm belongs to the chord press that created it. It used to
            # outlive the release, so pressing Ctrl+Win during a hands-free
            # session — reaching for Ctrl+Win+arrow, say — left the session
            # armed, and the next Space he typed ended it and pasted the
            # transcript into whatever had focus. Hands-free exists for talking
            # WHILE typing; Space is the most-typed key there is.
            self.armed_for_stop = False

        # Esc cancels from any active state and is never suppressed.
        if ev.kind == "down" and ev.key == "esc":
            if self.state in (St.REC_HOLD, St.REC_TOGGLE):
                return self._end(Act.CANCEL)
            return []

        if self.state is St.IDLE:
            if (
                ev.kind == "down"
                and ev.key in MODIFIERS
                and self._chord_down()
                and not self._blocked_until_release
            ):
                self.state = St.REC_HOLD
                self.started_at = ev.t_ms
                return [Act.START_HOLD]
            return []

        if self.state is St.REC_HOLD:
            if ev.kind == "down" and ev.key not in CHORD_KEYS:
                # Ctrl+Win is the prefix of real Windows shortcuts: Ctrl+Win+D
                # for a new virtual desktop, Ctrl+Win+arrow to switch between
                # them, Ctrl+Win+F to find PCs. Every one of those used to start
                # and then transcribe a dictation. A key joining the chord means
                # the user wants the shortcut, so drop the session.
                return self._end(Act.DISCARD)
            if ev.kind == "down" and ev.key == "space":
                self.state = St.REC_TOGGLE
                self.armed_for_stop = False
                # The chord is still physically down at the moment of promotion,
                # and Windows keeps sending auto-repeat keydowns for held keys.
                # Without this, a repeat could arm and a second Space stop the
                # session it had just started. IDLE guards the same hazard the
                # same way.
                self._blocked_until_release = True
                return [Act.PROMOTE_TOGGLE]
            if ev.kind == "up" and ev.key in MODIFIERS:
                elapsed = ev.t_ms - self.started_at
                if elapsed < self.min_session_ms:
                    return self._end(Act.DISCARD)
                return self._end(Act.STOP_AND_TRANSCRIBE)
            return []

        if self.state is St.REC_TOGGLE:
            # Hands are off the keyboard: modifier releases mean nothing.
            if (ev.kind == "down" and ev.key in MODIFIERS
                    and self._chord_down() and not self._blocked_until_release):
                self.armed_for_stop = True
            elif (ev.kind == "down" and ev.key == "space"
                    and self.armed_for_stop and self._chord_down()):
                # _chord_down() as well as the arm: the stop gesture is a chord
                # being HELD as Space lands, not an arm left over from earlier.
                return self._end(Act.STOP_AND_TRANSCRIBE)
            return []

        return []
