"""Launch at login, via the per-user Run key.

HKCU rather than HKLM: no elevation needed, and a dictation tool has no business
starting for other accounts on the machine.
"""

import logging
import sys
from pathlib import Path

import winreg

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Murmur"


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_command() -> str:
    """The command written to the Run key.

    Two things are load-bearing:

    * pythonw.exe, not python.exe — python.exe pops a console window at login.
    * The launcher script by ABSOLUTE PATH, not `-m murmur`. A Run key entry has
      no working directory, so `-m murmur` launched from System32 and died with
      "No module named murmur" — invisibly, because pythonw has no console.
    """
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    launcher = project_root() / "run_murmur.pyw"
    return f'"{interpreter}" "{launcher}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.debug("could not read the Run key", exc_info=True)
        return False


def set_enabled(enabled: bool, command: str | None = None) -> bool:
    """Returns True on success. Never raises — this is a tray checkbox."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                                  command or default_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.warning("could not update launch-at-login", exc_info=True)
        return False
