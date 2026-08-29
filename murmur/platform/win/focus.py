"""Which window has focus, for the history record."""

import ctypes
import ctypes.wintypes as w
import logging

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_window() -> tuple[str, str]:
    """Returns (process_name, window_title). Empty strings if unavailable —
    this is metadata for the history row and must never break a dictation."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                size = w.DWORD(260)
                pbuf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                        handle, 0, pbuf, ctypes.byref(size)):
                    name = pbuf.value.rsplit("\\", 1)[-1]
            finally:
                kernel32.CloseHandle(handle)
        return (name, title)
    except Exception:
        log.debug("could not read foreground window", exc_info=True)
        return ("", "")
