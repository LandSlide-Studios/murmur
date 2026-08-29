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
    def __init__(self, min_session_ms: int = 350):
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
        never sees a dangling keyup. Esc and everything else always pass through."""
        return key == "space" and self._chord_down()

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
            if ev.kind == "down" and ev.key == "space":
                self.state = St.REC_TOGGLE
                self.armed_for_stop = False
                return [Act.PROMOTE_TOGGLE]
            if ev.kind == "up" and ev.key in MODIFIERS:
                elapsed = ev.t_ms - self.started_at
                if elapsed < self.min_session_ms:
                    return self._end(Act.DISCARD)
                return self._end(Act.STOP_AND_TRANSCRIBE)
            return []

        if self.state is St.REC_TOGGLE:
            # Hands are off the keyboard: modifier releases mean nothing.
            if ev.kind == "down" and ev.key in MODIFIERS and self._chord_down():
                self.armed_for_stop = True
            elif ev.kind == "down" and ev.key == "space" and self.armed_for_stop:
                return self._end(Act.STOP_AND_TRANSCRIBE)
            return []

        return []
