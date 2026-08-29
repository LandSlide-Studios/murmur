"""Self-test the hook end to end by synthesizing the real chords.

Verifies: the hook installs, the callback fires, the FSM integration produces
the right actions, and suppression claims Space only while Ctrl+Win are held.
Run scripts/probe_hotkey_manual.py to test with real fingers.
"""
import ctypes
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from murmur.platform.win.chord import Act
from murmur.platform.win.hotkey import HotkeyListener

u = ctypes.WinDLL("user32", use_last_error=True)
VK_CONTROL, VK_LWIN, VK_SPACE, VK_ESCAPE = 0x11, 0x5B, 0x20, 0x1B
UP = 0x0002


def tap(vk, down=True):
    u.keybd_event(vk, 0, 0 if down else UP, 0)
    time.sleep(0.02)


def drain(q, wait=0.35):
    time.sleep(wait)
    out = []
    while True:
        try:
            out.append(q.get_nowait().name)
        except queue.Empty:
            return out


def main():
    hk = HotkeyListener(min_session_ms=350, accept_injected=True)
    hk.start()
    print(f"hook installed: {hk.running}")
    failures = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}")
        if not ok:
            failures.append(f"{label}: got {got}, want {want}")

    print("\n1. hold-to-talk (Ctrl+Win held ~500ms, released)")
    tap(VK_CONTROL); tap(VK_LWIN)
    time.sleep(0.5)
    tap(VK_LWIN, False); tap(VK_CONTROL, False)
    check("hold", drain(hk.actions), ["START_HOLD", "STOP_AND_TRANSCRIBE"])

    print("\n2. accidental tap (under 350ms)")
    tap(VK_CONTROL); tap(VK_LWIN)
    tap(VK_LWIN, False); tap(VK_CONTROL, False)
    check("tap", drain(hk.actions), ["START_HOLD", "DISCARD"])

    print("\n3. hands-free toggle (Ctrl+Win+Space, hands off, chord again)")
    tap(VK_CONTROL); tap(VK_LWIN); tap(VK_SPACE)
    tap(VK_SPACE, False); tap(VK_LWIN, False); tap(VK_CONTROL, False)
    first = drain(hk.actions)
    time.sleep(0.4)
    tap(VK_CONTROL); tap(VK_LWIN); tap(VK_SPACE)
    tap(VK_SPACE, False); tap(VK_LWIN, False); tap(VK_CONTROL, False)
    check("toggle", first + drain(hk.actions),
          ["START_HOLD", "PROMOTE_TOGGLE", "STOP_AND_TRANSCRIBE"])

    print("\n4. Esc cancels a live session")
    tap(VK_CONTROL); tap(VK_LWIN)
    time.sleep(0.4)
    tap(VK_ESCAPE); tap(VK_ESCAPE, False)
    tap(VK_LWIN, False); tap(VK_CONTROL, False)
    check("cancel", drain(hk.actions), ["START_HOLD", "CANCEL"])

    print("\n5. Esc while idle is ignored (must still reach the focused app)")
    tap(VK_ESCAPE); tap(VK_ESCAPE, False)
    check("idle esc", drain(hk.actions), [])

    print("\n6. bare Space is never claimed")
    tap(VK_SPACE); tap(VK_SPACE, False)
    check("bare space", drain(hk.actions), [])

    hk.stop()
    print(f"\nhook stopped: running={hk.running}")
    print("\n" + ("ALL PASS" if not failures else "FAILURES:\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
