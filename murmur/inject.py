"""Insert text at the cursor in whatever app has focus.

Clipboard + Ctrl+V rather than synthesized character keystrokes: it is instant
regardless of length, and it survives IMEs, dead keys and non-ASCII text that
per-character injection mangles.

Every step below exists because of a specific observed failure. See CLAUDE.md.
"""

import ctypes
import logging
import time

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
# GetAsyncKeyState returns SHORT. Without a declared restype ctypes
# interprets 32 bits of a 16-bit return value.
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                               ctypes.c_uint, ctypes.c_void_p]

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN, VK_V = 0x5B, 0x5C, 0x56
KEYEVENTF_KEYUP = 0x0002
_HELD = 0x8000

# Order matters: Win last, so the Start menu's "was Win pressed alone" tracking
# sees the other modifiers release first and never fires.
MODIFIERS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)


class Injector:
    def __init__(
        self,
        restore_previous: bool = False,
        clipboard_settle_s: float = 0.06,
        restore_delay_s: float = 0.3,
        release_timeout_s: float = 0.5,
    ):
        self.restore_previous = restore_previous
        self.clipboard_settle_s = clipboard_settle_s
        self.restore_delay_s = restore_delay_s
        self.release_timeout_s = release_timeout_s

    # --- clipboard -------------------------------------------------------

    def _get_clipboard(self) -> str:
        import pyperclip

        return pyperclip.paste()

    def _set_clipboard(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)

    # --- keyboard --------------------------------------------------------

    def _release_modifiers(self) -> bool:
        """Force every modifier up before pasting. True if they are all clear.

        The user is physically holding Ctrl+Win at the moment a hold-to-talk
        session ends. Sending Ctrl+V then reads as Ctrl+Win+V, which opens
        Clipboard History instead of pasting.

        keybd_event(KEYUP) cannot lift a key the hardware still reports down —
        which is exactly the case when the user is already pressing the chord
        for their next dictation. Returning False there and NOT pasting is the
        safe answer: the text is on the clipboard and in history either way,
        whereas pasting blind is the failure CLAUDE.md forbids.
        """
        for vk in MODIFIERS:
            if user32.GetAsyncKeyState(vk) & _HELD:
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

        deadline = time.perf_counter() + self.release_timeout_s
        while time.perf_counter() < deadline:
            if not any(user32.GetAsyncKeyState(vk) & _HELD for vk in MODIFIERS):
                return True
            time.sleep(0.005)
        log.warning("modifiers still held after %.0fms; leaving the text on the "
                    "clipboard rather than sending Ctrl+Win+V",
                    self.release_timeout_s * 1000)
        return False

    def _send_paste(self) -> None:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    # --- public ----------------------------------------------------------

    def copy(self, text: str | None) -> bool:
        """Put the text on the clipboard and clear the modifiers, WITHOUT
        pasting. Returns False if a modifier is still physically held.

        Split from the paste so an animation can run between the two: the
        transcript is safely on the clipboard before anything moves, and the
        keystroke fires the instant the comet lands.
        """
        if not text:
            return False
        released = self._release_modifiers()
        self._set_clipboard(text)
        return released

    def paste(self) -> None:
        """Send Ctrl+V. Assumes copy() already cleared the modifiers."""
        self._send_paste()

    def inject(self, text: str | None) -> bool:
        """Copy and paste in one go. Returns True if the text was pasted,
        False if it was only copied."""
        if not text:
            return False

        previous = None
        if self.restore_previous:
            try:
                previous = self._get_clipboard()
            except Exception as e:
                # Losing the old clipboard is acceptable. Losing the dictation
                # is not, so this must never abort the paste.
                log.warning("could not read previous clipboard: %s", e)

        released = self._release_modifiers()

        # Deliberately not guarded: if the text never reached the clipboard,
        # Ctrl+V would paste whatever was there before. Failing loudly beats
        # silently pasting the wrong thing into the user's document.
        self._set_clipboard(text)

        if not released:
            # Text is on the clipboard; the caller tells the user to paste it.
            return False

        if self.clipboard_settle_s:
            time.sleep(self.clipboard_settle_s)
        self._send_paste()

        if self.restore_previous and previous is not None:
            if self.restore_delay_s:
                time.sleep(self.restore_delay_s)
            try:
                self._set_clipboard(previous)
            except Exception as e:
                log.warning("could not restore previous clipboard: %s", e)
        return True
