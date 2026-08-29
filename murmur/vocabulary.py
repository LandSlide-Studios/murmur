"""Learned vocabulary.

The requirement in the user's words: "if I replace the words after I paste it,
it should learn that."

The learner is SUPERVISED, deliberately. An automatic observation must be seen
twice before it applies; a manual edit is explicit intent and is trusted at
once. Every term is visible in the Vocabulary panel with its hit count and an
enable toggle. An unsupervised learner that quietly corrupts transcripts is
worse than no learner at all.

Learned terms are used in three places:
  1. hotwords passed to faster-whisper, biasing decoding
  2. a deterministic substitution applied to the raw transcript
  3. a glossary injected into the polish prompt
"""

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

GLOSSARY_LIMIT = 40

# The key is the PAIR. Keying on the term alone counted sightings of the right
# form, so two DIFFERENT mishearings ("teh" and "hte" both -> "the") promoted a
# wrong form that had only ever been seen once.
SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
  wrong_form TEXT NOT NULL,
  term TEXT NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0,
  promoted INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  first_seen REAL,
  last_seen REAL,
  PRIMARY KEY (wrong_form, term)
);
"""

# \b does not work next to punctuation like "c++", so guard with lookarounds
# that only require a non-word character (or string edge) on either side.
_LEFT = r"(?<![\w])"
_RIGHT = r"(?![\w])"


class Vocabulary:
    def __init__(self, path, promote_after_hits: int = 2):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.promote_after_hits = promote_after_hits
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def observe(self, wrong: str, right: str, source: str = "auto") -> bool:
        """Record a (wrong -> right) correction. Returns True if now promoted."""
        wrong, right = (wrong or "").strip(), (right or "").strip()
        # Compare exactly, NOT case-insensitively: "halvorsen" -> "Halvorsen" is a
        # real correction, and capitalising proper nouns is most of what this
        # feature is for.
        if not wrong or not right or wrong == right:
            return False

        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT hit_count, promoted FROM terms WHERE wrong_form=? AND term=?",
                (wrong, right)).fetchone()
            hits = (row["hit_count"] if row else 0) + 1
            promoted = 1 if (source == "manual"
                             or hits >= self.promote_after_hits) else 0
            self._conn.execute(
                "INSERT INTO terms (wrong_form, term, hit_count, promoted,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(wrong_form, term) DO UPDATE SET"
                "   hit_count=excluded.hit_count,"
                "   promoted=MAX(terms.promoted, excluded.promoted),"
                "   last_seen=excluded.last_seen",
                (wrong, right, hits, promoted, now, now))
            self._conn.commit()
            effective = promoted or (row["promoted"] if row else 0)
        if effective:
            log.info("vocabulary: %r -> %r active (%s, %d hits)",
                     wrong, right, source, hits)
        return bool(effective)

    # --- reads ------------------------------------------------------------

    def _active(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM terms WHERE promoted=1 AND enabled=1"
                " ORDER BY LENGTH(wrong_form) DESC, hit_count DESC"))

    def hotwords(self) -> list[str]:
        seen, out = set(), []
        for r in self._active():
            if r["term"] not in seen:
                seen.add(r["term"])
                out.append(r["term"])
        return out

    def glossary(self) -> list[str]:
        return self.hotwords()[:GLOSSARY_LIMIT]

    def apply(self, text: str) -> str:
        """Substitute learned terms in ONE pass.

        Two things here are deliberate and were both bugs before:

        * One combined pattern, not a loop of re.sub calls. Sequential passes
          re-substitute into their own output, so `cat->dog` plus `dog->wolf`
          turned "cat" into "wolf", and any replacement containing its own wrong
          form grew every pass ("vantage" -> "Vantage Labs" -> "Vantage Labs Labs").
        * Case-SENSITIVE. Matching case-insensitively meant a correction like
          `mark->Marc` also rewrote the ordinary word "mark", and `us->US`
          rewrote every "us" in the sentence. We replace exactly the form that
          was observed to be wrong, nothing else.

        Longest wrong-form first, so a term containing another wins.
        """
        if not text:
            return text
        rows = self._active()
        if not rows:
            return text

        replacements = {}
        alternatives = []
        for r in rows:
            wrong = r["wrong_form"]
            if wrong in replacements:      # first (longest) wins
                continue
            replacements[wrong] = r["term"]
            alternatives.append(re.escape(wrong))

        pattern = _LEFT + "(?:" + "|".join(alternatives) + ")" + _RIGHT
        try:
            return re.sub(pattern,
                          lambda m: replacements.get(m.group(0), m.group(0)),
                          text)
        except re.error:
            log.debug("bad vocabulary pattern; leaving text unchanged")
            return text

    def all_terms(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM terms ORDER BY hit_count DESC, term ASC, wrong_form ASC").fetchall()
        return [dict(r) for r in rows]

    # --- supervision ------------------------------------------------------

    def set_enabled(self, term: str, enabled: bool,
                    wrong_form: str | None = None) -> None:
        with self._lock:
            if wrong_form is None:
                self._conn.execute("UPDATE terms SET enabled=? WHERE term=?",
                                   (1 if enabled else 0, term))
            else:
                self._conn.execute(
                    "UPDATE terms SET enabled=? WHERE term=? AND wrong_form=?",
                    (1 if enabled else 0, term, wrong_form))
            self._conn.commit()

    def forget(self, term: str, wrong_form: str | None = None) -> None:
        with self._lock:
            if wrong_form is None:
                self._conn.execute("DELETE FROM terms WHERE term=?", (term,))
            else:
                self._conn.execute(
                    "DELETE FROM terms WHERE term=? AND wrong_form=?",
                    (term, wrong_form))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
