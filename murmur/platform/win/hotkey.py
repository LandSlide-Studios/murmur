"""Global keyboard hook.

Owns a WH_KEYBOARD_LL hook on a dedicated thread with its own message pump.

The callback ONLY enqueues. A low-level hook callback that exceeds
LowLevelHooksTimeout (~300ms, HKCU\\Control Panel\\Desktop) is silently
unhooked by Windows: the hotkey simply stops working, with no error and no
exception. Anything slow here is a bug that presents as "it worked this morning".
"""

import ctypes
import ctypes.wintypes as w
import logging
import queue
import threading
import time

from .chord import Act, ChordFSM, Ev

log = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10

VK_MAP = {
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",     # CONTROL, LCONTROL, RCONTROL
    0x5B: "win", 0x5C: "win",                     # LWIN, RWIN
    0x20: "space",
    0x1B: "esc",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HOOKPROC_T = ctypes.WINFUNCTYPE(w.LPARAM, ctypes.c_int, w.WPARAM, w.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC_T, w.HINSTANCE, w.DWORD]
user32.SetWindowsHookExW.restype = w.HHOOK
user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.CallNextHookEx.restype = w.LPARAM
user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, ctypes.c_uint, ctypes.c_uint]
user32.PostThreadMessageW.argtypes = [w.DWORD, ctypes.c_uint, w.WPARAM, w.LPARAM]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", w.DWORD),
        ("scanCode", w.DWORD),
        ("flags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.POINTER(w.ULONG)),
    ]


HOOKPROC = HOOKPROC_T


class HotkeyListener:
    def __init__(self, min_session_ms: int = 350, accept_injected: bool = False):
        # accept_injected is for the self-test probe only. In production it must
        # stay False: inject.py releases modifiers via keybd_event, and feeding
        # those synthetic events back would desync the chord FSM.
        self.accept_injected = accept_injected
        self.fsm = ChordFSM(min_session_ms=min_session_ms)
        self.actions: queue.Queue[Act] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._hook = None
        self._tid = None
        self._cb = None            # must outlive the hook or it is GC'd mid-call
        self._t0 = time.perf_counter()
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def _resync_modifiers(self) -> None:
        """Drop any modifier the FSM thinks is held that the OS says is not.

        The virtual-key codes come from VK_MAP rather than a second hand-written
        list, so a key added there is covered here automatically and the two
        cannot drift apart.
        """
        try:
            for name in ("ctrl", "win"):
                if name not in self.fsm.held:
                    continue
                vks = [vk for vk, n in VK_MAP.items() if n == name]
                if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
                    log.debug("resync: %s was held in the FSM but is not down", name)
                    self.fsm.held.discard(name)
        except Exception:
            log.debug("modifier resync failed", exc_info=True)

    def _ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def _proc(self, nCode, wParam, lParam):
        if nCode != 0:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        try:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

            # Ignore our own synthetic keys: inject.py releases modifiers via
            # keybd_event, and re-feeding those would desync the chord FSM.
            if kb.flags & LLKHF_INJECTED and not self.accept_injected:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            key = VK_MAP.get(kb.vkCode)
            if key is None:
                # A key outside the chord still MATTERS during a push-to-talk
                # hold: Ctrl+Win is the prefix of Ctrl+Win+D, Ctrl+Win+arrow and
                # Ctrl+Win+F, and the FSM discards a hold that one of them joins.
                #
                # That discard branch existed and was unreachable, because this
                # filter dropped every such key before `feed` ever saw it. The
                # guard shipped, passed its own tests -- which drove the FSM
                # directly -- and never executed once in production.
                #
                # Asking the FSM keeps the cost off the common path: while idle
                # this is one attribute check per keystroke, not an FSM step.
                if not self.fsm.wants_other_keys():
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                key = "other"

            kind = "down" if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) else "up"

            # Reconcile against what the OS actually reports before deciding.
            # A key-up can be eaten by focus theft, a UAC prompt or an RDP
            # session, and the FSM then believes that modifier is held forever
            # -- so a single later press of the OTHER modifier starts a session
            # on its own.
            self._resync_modifiers()
            suppress = self.fsm.should_suppress(kind, key)
            for act in self.fsm.feed(Ev(kind, key, self._ms())):
                self.actions.put(act)      # never do work inline
            if suppress:
                return 1                   # swallow: Win+Space switches language
        except Exception:
            # A raising callback would be unhooked by Windows. Swallow, log,
            # and keep the hook alive.
            log.exception("hook callback error (suppressed to keep hook alive)")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        try:
            self._cb = HOOKPROC(self._proc)
            self._tid = kernel32.GetCurrentThreadId()
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._cb, None, 0)
            if not self._hook:
                raise ctypes.WinError(ctypes.get_last_error())
            log.info("keyboard hook installed")
        except BaseException as e:
            self._error = e
            self._ready.set()
            return
        self._ready.set()

        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        log.info("keyboard hook message loop exited")

    def start(self, timeout_s: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="murmur-hook")
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise RuntimeError("keyboard hook did not install within %ss" % timeout_s)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        # A stopped listener must not resume mid-chord when restarted.
        self.fsm = ChordFSM(min_session_ms=self.fsm.min_session_ms)

    @property
    def running(self) -> bool:
        return self._hook is not None
