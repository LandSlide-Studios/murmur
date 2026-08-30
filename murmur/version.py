"""Which build is this?

Added after a day spent unable to answer that question from the log. A running
instance can be hours behind the working tree, and "it does not send sometimes"
is unanswerable without knowing which code is actually running.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_id() -> str:
    """Short commit plus dirty flag. Falls back gracefully outside a checkout."""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=3)
        if rev.returncode != 0:
            return "unknown"
        sha = rev.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=3)
        return sha + ("+dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"
