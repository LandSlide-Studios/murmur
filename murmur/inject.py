"""Insert text at the cursor in whatever app has focus.

Clipboard + Ctrl+V rather than synthesized character keystrokes: it is instant
regardless of length, and it survives IMEs, dead keys and non-ASCII text that
per-character injection mangles.

Every step below exists because of a specific observed failure. See CLAUDE.md.
"""

import ctypes
import logging
import threading
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
        # One injection at a time.
        #
        # There was no lock here at all, and copy()/paste() are deliberately
        # split so an animation can run between them. A second dictation that
        # set the clipboard inside the first one's settle window meant BOTH
        # Ctrl+V presses pasted the second transcript -- two presses, one
        # transcript delivered twice, the other lost.
        #
        # Re-entrant because inject() calls the same primitives. This is also
        # the seam the modifier re-check and the clipboard guards belong
        # inside, so they cannot race the thing they guard.
        self._lock = threading.RLock()
        # What copy() last put on the clipboard, so paste() can tell whether
        # anything overwrote it in between.
        self._staged: str | None = None

    # --- clipboard -------------------------------------------------------

    def _get_clipboard(self) -> str:
        import pyperclip

        return pyperclip.paste()

    # C0 controls except tab and newline. NUL is the dangerous one: the Win32
    # clipboard sizes its buffer with a string length that stops at the first
    # NUL, so everything after it is dropped with no error anywhere -- content
    # loss that presents as success. The cleanup pass strips these too, but a
    # raw transcript bypasses cleanup on every fallback path.
    _CONTROLS = "".join(chr(c) for c in
                        [*range(0, 9), 11, 12, *range(14, 32), 127])
    _CONTROL_MAP = str.maketrans("", "", _CONTROLS)

    def _set_clipboard(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text.translate(self._CONTROL_MAP))

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

        # Sample once BEFORE the wait. The check used to live only inside a
        # timed loop whose condition is evaluated first, so a zero timeout --
        # or any stall longer than the timeout before the loop was entered --
        # reported "still held" having polled nothing at all, and refused to
        # paste when nothing was actually down. This wants do-while semantics.
        if not any(user32.GetAsyncKeyState(vk) & _HELD for vk in MODIFIERS):
            return True

        deadline = time.perf_counter() + self.release_timeout_s
        while time.perf_counter() < deadline:
            if not any(user32.GetAsyncKeyState(vk) & _HELD for vk in MODIFIERS):
                return True
            time.sleep(0.005)
        log.warning("modifiers still held after %.0fms; leaving the text on the "
                    "clipboard rather than sending Ctrl+Win+V",
                    self.release_timeout_s * 1000)
        return False

    def _paste_blocking_modifiers(self) -> bool:
        """Whether a modifier is down that would corrupt Ctrl+V into something
        else. Checked, never forced: this runs mid-animation on the UI thread."""
        try:
            return any(user32.GetAsyncKeyState(vk) & _HELD
                       for vk in (VK_CONTROL, VK_LWIN, VK_RWIN))
        except Exception:
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
        # The modifier spin is OUTSIDE the lock. It waits up to
        # release_timeout_s for a physically-held key, and holding the lock
        # across it stalled the UI thread's paste() for half a second on the
        # ordinary hold-to-talk path -- a new block on a thread that is not
        # supposed to block at all.
        released = self._release_modifiers()
        with self._lock:
            self._set_clipboard(text)
            self._staged = text
        return released

    def paste(self) -> bool:
        """Send Ctrl+V. Returns False if a modifier was still held.

        It used to assume copy() had already cleared them. The split exists so
        an animation can run in between, and the user can re-press the chord
        inside that window -- which turns the paste into Ctrl+Win+V and opens
        Clipboard History instead of pasting. The text is already on the
        clipboard, so refusing costs nothing.
        """
        # Only the modifiers that actually break a paste, and only a single
        # sample -- no spin. This runs on the UI thread when the comet lands,
        # and waiting half a second there freezes the animation it is part of.
        # The documented hazard is Ctrl+Win+V opening Clipboard History; Shift
        # or Alt being down is the user typing again, and forcing those up would
        # break their selection to no purpose.
        if self._paste_blocking_modifiers():
            log.warning("Ctrl or Win still held at paste time; leaving the "
                        "text on the clipboard")
            return False
        with self._lock:
            # copy() and paste() are deliberately split so the animation can run
            # between them, and the lock is released in that gap -- so a lock
            # alone never closed this window. Anything can write the clipboard
            # in it: a second dictation, or one click on Copy in the history
            # panel, which would otherwise paste a history row into the user's
            # document instead of the dictation.
            staged = self._staged
            if staged is not None:
                try:
                    if self._get_clipboard() != staged:
                        log.info("clipboard changed since the transcript was "
                                 "staged; restoring it before pasting")
                        self._set_clipboard(staged)
                except Exception:
                    log.debug("could not verify the clipboard", exc_info=True)
            self._send_paste()
            self._staged = None
            return True

    def inject(self, text: str | None) -> bool:
        """Copy and paste in one go. Returns True if the text was pasted,
        False if it was only copied."""
        if not text:
            return False
        with self._lock:
            return self._inject_locked(text)

    def _inject_locked(self, text: str) -> bool:
        previous = None
        if self.restore_previous:
            try:
                previous = self._get_clipboard()
            except Exception as e:
                # Losing the old clipboard is acceptable. Losing the dictation
                # is not, so this must never abort the paste.
                log.warning("could not read previous clipboard: %s", e)
            if not previous:
                # An image or a file list on the clipboard reads back as an
                # empty string, and the restore then wrote that empty string
                # OVER the transcript -- costing the user the one thing that
                # was still recoverable. The image was unrecoverable either way.
                previous = None

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
