"""One running instance, and a way to poke it.

Pressing the desktop shortcut while Murmur is already running should start a
dictation session, not launch a second copy fighting over the same keyboard hook.
"""

import ctypes
import logging
import threading

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Global\\MurmurSingleInstance"
PIPE_NAME = r"\\.\pipe\murmur-activate"

_mutex = None


def acquire() -> bool:
    """True if this is the first instance. The handle is deliberately kept in a
    module global so it lives as long as the process."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def signal_existing() -> bool:
    """Ask the running instance to start a hands-free session."""
    try:
        with open(PIPE_NAME, "w") as pipe:
            pipe.write("activate")
        return True
    except OSError as e:
        log.warning("could not signal the running instance: %s", e)
        return False


def listen(on_activate) -> None:
    """Serve the activation pipe on a daemon thread."""

    def loop():
        import win32file
        import win32pipe

        while True:
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    PIPE_NAME,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
                    1, 256, 256, 0, None,
                )
                win32pipe.ConnectNamedPipe(handle, None)
                win32file.ReadFile(handle, 64)
                on_activate()
            except Exception:
                log.debug("activation pipe error", exc_info=True)
            finally:
                if handle is not None:
                    try:
                        import win32file as _wf

                        _wf.CloseHandle(handle)
                    except Exception:
                        pass

    threading.Thread(target=loop, daemon=True, name="murmur-pipe").start()
