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
        if _is_readable(path):
            # The file is FINE; the schema script is what failed. This is the
            # difference between "damaged" and "a different shape", and getting
            # it wrong here destroys data rather than saving it.
            #
            # `CREATE TABLE IF NOT EXISTS` no-ops against an existing table, so
            # an older column set survives a schema bump untouched -- and the
            # next `CREATE INDEX ... ON sessions(ts)` then raises "no such
            # column". Quarantining on that would displace every existing
            # user's history the first time a column is ever added.
            log.error("store at %s is intact but its schema did not apply (%s); "
                      "keeping the file and continuing without the change",
                      path, e)
            return _connect_without_schema(path)
        quarantined = _quarantine(path, e)
        if quarantined is None:
            raise
    return _connect(path, schema)


def _is_readable(path: Path) -> bool:
    """Whether SQLite considers this file a healthy database.

    Deliberately conservative: anything that stops us answering the question --
    including a lock held by another process -- counts as NOT damaged, because
    the only action gated on a False here is moving the user's data aside.
    """
    conn = None
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError as e:
        # "database is locked" says nothing about the file's health, and the
        # rename only failed on Windows by luck of the sharing flags.
        return "locked" in str(e).lower() or "busy" in str(e).lower()
    except Exception:
        return True
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _connect_without_schema(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
    # not overwrite the first casualty — and a counter after that, because two
    # bad starts inside the same second would otherwise collide and `replace`
    # would silently destroy the file we are trying to preserve.
    stamp = int(time.time())
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    try:
        path.replace(aside)
    except OSError:
        log.exception("store at %s is unusable and could not be moved aside", path)
        return None
    log.error("store at %s was unusable (%s); moved to %s and started a fresh one",
              path, error, aside.name)
    return aside
