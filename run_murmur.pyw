"""Launcher for Murmur.

Exists so the app can be started from anywhere. `pythonw.exe -m murmur` only
resolves when the working directory happens to be the project root, which is
never true at login — the Run key entry launched from System32 and failed
silently, with no console to show the error.

Referencing this file by absolute path makes the working directory irrelevant.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from murmur.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
