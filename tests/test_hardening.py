"""Tier 6: the demonstrated-but-untriggered holes, closed.

The first section matters most. The Windows-shortcut guard shipped, passed its
own tests, and never executed once in production — because those tests drove the
state machine directly while the real code path filtered the key three layers
earlier. These drive the hook.
"""
import ctypes
import queue
import threading

import pytest

from murmur.platform.win import hotkey as H
from murmur.platform.win.chord import Act, ChordFSM


# --- the hook actually delivers what the FSM needs ---------------------------

class FakeKb(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


@pytest.fixture
def hook(monkeypatch):
    """A real Listener driven through its real callback, with only the Win32
    edges stubbed."""
    held = set()

    class FakeUser32:
        def CallNextHookEx(self, *a):
            return 0

        def GetAsyncKeyState(self, vk):
            return 0x8000 if vk in held else 0

    monkeypatch.setattr(H, "user32", FakeUser32())

    # The REAL constructor, not a hand-built stand-in: a fake listener would
    # be testing the harness rather than the code path that was broken.
    listener = H.HotkeyListener(min_session_ms=0, accept_injected=True)
    listener.held = held

    def press(vk, down=True):
        held.add(vk) if down else held.discard(vk)
        kb = FakeKb(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=None)
        monkeypatch.setattr(
            ctypes, "cast",
            lambda p, t: type("P", (), {"contents": kb})())
        listener._proc(0, H.WM_KEYDOWN if down else H.WM_KEYUP, 0)

    listener.press = press
    return listener


def drain(listener):
    out = []
    while not listener.actions.empty():
        out.append(listener.actions.get_nowait())
    return out


CTRL, WIN, SPACE, D, RIGHT = 0xA2, 0x5B, 0x20, 0x44, 0x27


@pytest.mark.parametrize("other", [D, RIGHT, 0x46, 0x09])
def test_a_windows_shortcut_does_not_dictate_through_the_real_hook(hook, other):
    """THE test that was missing. `chord.py` discards a hold that a foreign key
    joins, and the tests for it drove the FSM directly — while `hotkey.py`
    resolved every key through a map of four names and returned early on
    anything else, so no foreign key ever reached `feed`. The guard was
    unreachable in production for its entire life."""
    hook.press(CTRL)
    hook.press(WIN)
    hook.press(other)
    hook.press(other, down=False)
    hook.press(WIN, down=False)
    hook.press(CTRL, down=False)

    acts = drain(hook)
    assert Act.STOP_AND_TRANSCRIBE not in acts, f"Ctrl+Win+{other:#x} dictated"
    assert acts[-1] is Act.DISCARD


def test_an_ordinary_hold_still_transcribes_through_the_real_hook(hook):
    """The counterweight — proving the harness is real and not a silent no-op."""
    hook.press(CTRL)
    hook.press(WIN)
    hook.press(WIN, down=False)
    assert Act.STOP_AND_TRANSCRIBE in drain(hook)


def test_typing_while_idle_does_not_reach_the_state_machine(hook):
    """The cost has to stay off the common path: while idle this is one
    attribute check per keystroke, not a state-machine step."""
    for _ in range(20):
        hook.press(0x41)
        hook.press(0x41, down=False)
    assert drain(hook) == []
    assert hook.fsm.held == set()


def test_typing_during_hands_free_does_not_end_the_session(hook):
    """Hands-free is for talking WHILE typing. Only a hold treats a stray key
    as a shortcut."""
    hook.press(CTRL)
    hook.press(WIN)
    hook.press(SPACE)
    hook.press(SPACE, down=False)
    hook.press(WIN, down=False)
    hook.press(CTRL, down=False)
    drain(hook)
    for vk in (0x41, 0x42, 0x43):
        hook.press(vk)
        hook.press(vk, down=False)
    assert drain(hook) == []


def test_a_lost_key_release_does_not_leave_a_phantom_chord(hook):
    """Focus theft, a UAC prompt or an RDP session can eat a key-up. The state
    machine then believed that modifier was held forever, so a single later
    press of the OTHER one started a session on its own."""
    hook.press(CTRL)
    hook.held.discard(CTRL)              # the release never arrives
    hook.press(WIN)
    assert Act.START_HOLD not in drain(hook), "a phantom chord started a session"


# --- the overlay -------------------------------------------------------------

def test_the_overlay_refuses_focus_outright(pill):
    """WS_EX_NOACTIVATE blocks activation by CLICK; it does not block a direct
    foreground request, and an activated overlay eats the paste."""
    from PySide6.QtCore import Qt

    assert bool(pill.windowFlags() & Qt.WindowDoesNotAcceptFocus)
    assert pill.focusPolicy() == Qt.NoFocus


def test_an_unknown_state_does_not_wedge_the_overlay(pill):
    """It fell through every branch to a default that sets full opacity, and
    was in no terminal list, so nothing ever timed it out."""
    pill.set_state("bogus_state_from_a_typo")
    assert pill.state in pill.KNOWN_STATES
    for _ in range(600):
        pill._tick()
    assert pill.opacity.target == 0.0 or not pill._timer.isActive()


def test_hardening_is_applied_even_if_the_window_is_already_visible(pill):
    """`_harden` sat inside the not-yet-visible branch, so a pill already on
    screen when its first state arrived never got WS_EX_NOACTIVATE."""
    pill.show()
    pill._hardened = False
    pill.set_state("armed")
    assert pill._hardened


# --- motion ------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_target_is_refused(bad):
    """One NaN written here was permanent: the integrator carried it into value
    and velocity, retargeting never restored them, and at_rest was False
    forever — so the overlay's hide condition could never fire."""
    from murmur.ui.motion import Spring

    s = Spring(value=1.0)
    s.target = bad
    assert s.target == 1.0
    for _ in range(120):
        s.step(1 / 60)
    assert s.value == s.value                    # not NaN
    s.target = 5.0
    for _ in range(300):
        s.step(1 / 60)
    assert abs(s.value - 5.0) < 0.1


# --- clipboard ---------------------------------------------------------------

def test_a_nul_never_reaches_the_clipboard():
    """The Win32 clipboard sizes its buffer with a length that stops at the
    first NUL, so everything after it is dropped with no error — content loss
    presenting as success. The cleanup pass strips these, but a raw transcript
    bypasses cleanup on every fallback path."""
    from murmur.inject import Injector

    written = []
    inj = Injector()
    import murmur.inject as I

    class FakePyperclip:
        @staticmethod
        def copy(t):
            written.append(t)

        @staticmethod
        def paste():
            return ""

    import sys
    sys.modules["pyperclip"] = FakePyperclip
    inj._set_clipboard("keep this" + chr(0) + "and this too")
    assert written == ["keep thisand this too"]


def test_newlines_and_tabs_survive_the_clipboard():
    from murmur.inject import Injector

    written = []

    class FakePyperclip:
        @staticmethod
        def copy(t):
            written.append(t)

    import sys
    sys.modules["pyperclip"] = FakePyperclip
    Injector()._set_clipboard("line one\n\tline two")
    assert written == ["line one\n\tline two"]


# --- audio -------------------------------------------------------------------

def test_a_short_cue_does_not_cut_a_longer_mute_short():
    """It assigned rather than extended, so a 10ms cue after a 500ms one ended
    the mute 490ms early and the first cue's tail landed in the next session's
    pre-roll."""
    import time

    from murmur.audio import Recorder

    r = Recorder(sample_rate=16000)
    r.mute_for(500)
    long_deadline = r._muted_until
    r.mute_for(10)
    assert r._muted_until >= long_deadline


def test_a_float_sample_rate_does_not_crash_construction():
    from murmur.audio import Recorder

    r = Recorder(sample_rate=16000.0)
    assert r.sample_rate == 16000


# --- the worker's own last resort --------------------------------------------

def test_a_raising_ui_callback_does_not_kill_the_worker(app, monkeypatch):
    """The handler that exists so one bad session cannot kill the worker called
    back into foreign UI code unguarded. A Qt callback into a deleted object
    raises, escapes the loop, and ends the thread — after which every dictation
    is queued and never processed, with the pill still reading transcribing."""
    import numpy as np

    import murmur.app as A

    def boom(state, **kw):
        raise RuntimeError("wrapped C/C++ object has been deleted")

    monkeypatch.setattr(app, "on_state", boom)
    monkeypatch.setattr(app, "_process",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))

    worker = threading.Thread(target=app._run_worker, daemon=True)
    worker.start()
    app._jobs.put((np.zeros(16000, dtype=np.float32), A.Session(1, "hold"), 100))
    app._jobs.put((np.zeros(16000, dtype=np.float32), A.Session(2, "hold"), 100))
    app._jobs.put(None)
    worker.join(timeout=3.0)

    assert not worker.is_alive(), "the worker died and took all dictation with it"
