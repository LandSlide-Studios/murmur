"""Every dictation, kept.

The requirement in the user's words: if you forget to paste it, or the paste
lands in the wrong place, the text is still in the app. So a row is written for
EVERY session — including cancelled and failed ones. A crashed transcription
must never silently cost the user their words.

Written from the worker thread, read from the UI thread, so every statement runs
under a lock with check_same_thread disabled.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .store import open_store

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  duration_ms INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  raw_text TEXT,
  polished_text TEXT,
  final_text TEXT,
  corrected_text TEXT,
  target_app TEXT,
  target_window_title TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts DESC);
"""


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # A half-written database after a power cut used to raise here, at
        # construction, so Murmur did not start and nothing said why. The bad
        # file is moved aside rather than deleted, and the log names it.
        self._conn = open_store(self.path, SCHEMA)

    def add(self, raw, polished, final, mode, duration_ms, app, title,
            status="ok") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (ts, duration_ms, mode, status, raw_text,"
                " polished_text, final_text, target_app, target_window_title)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), duration_ms, mode, status, raw, polished, final,
                 app, title),
            )
            self._conn.commit()
            return cur.lastrowid

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, q: str, limit: int = 200) -> list[dict]:
        # ESCAPE so a query containing % or _ is matched literally rather than
        # behaving as a wildcard that returns everything.
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE raw_text LIKE ? ESCAPE '\\'"
                "   OR final_text LIKE ? ESCAPE '\\'"
                "   OR corrected_text LIKE ? ESCAPE '\\'"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_correction(self, row_id: int, corrected: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET corrected_text=? WHERE id=?",
                (corrected, row_id),
            )
            self._conn.commit()

    def get(self, row_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row else None

    def purge(self, keep: int) -> None:
        """Trim to the newest `keep` rows. Retention is unlimited by default."""
        if keep <= 0:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE id NOT IN ("
                "  SELECT id FROM sessions ORDER BY ts DESC, id DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
