"""Opening a SQLite store without letting a bad file stop the app.

`config.py` states the house rule for this application: nothing may raise on bad
input, because this is a background tray app with no console — a traceback and a
silent failure look identical from the outside. The stores did not follow it. A
half-written `history.db` after a power cut raised at CONSTRUCTION, not on a
later call, so Murmur simply did not start and nothing said why.

Losing the history is bad. Not starting is worse, and it is not a trade the user
gets to make, so the file is moved aside rather than deleted and the log says
exactly where it went.
"""

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


def open_store(path: Path, schema: str) -> sqlite3.Connection:
    """Connect and apply `schema`, quarantining the file if it is unusable.

    Raises only if even a freshly created database cannot be opened — at that
    point the directory itself is the problem and there is nothing to salvage.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        return _connect(path, schema)
    except sqlite3.Error as e:
        quarantined = _quarantine(path, e)
        if quarantined is None:
            raise
    return _connect(path, schema)


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(schema)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _quarantine(path: Path, error: Exception) -> Path | None:
    """Move an unusable store aside. Returns where it went, or None."""
    if not path.exists():
        return None
    # A stamped name rather than one fixed suffix, so a second bad start does
    # not overwrite the first casualty.
    aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        path.replace(aside)
    except OSError:
        log.exception("store at %s is unusable and could not be moved aside", path)
        return None
    log.error("store at %s was unusable (%s); moved to %s and started a fresh one",
              path, error, aside.name)
    return aside
