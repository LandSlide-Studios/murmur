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

from .store import open_store

log = logging.getLogger(__name__)

GLOSSARY_LIMIT = 40

# Whisper conditions on a prompt of roughly 224 tokens. Beyond that the decoder
# silently truncates, so an unbounded list does not just fail to help -- it
# costs accuracy at the earliest point in the pipeline, where nothing
# downstream can recover it. The learned set grows monotonically, so "unbounded"
# is what it becomes.
HOTWORD_LIMIT = 64

# A term is user-derived text spliced into two different prompts. `polish.py`
# already treated its copy as untrusted and sanitised it; the transcriber joined
# the same list raw. Sanitising at the source is what stops the two diverging
# again.
_TERM_MAX_CHARS = 60

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



# Words where a case-only "correction" is far more likely to be a one-off than a
# rule. Learning `us -> US` from a single edit rewrote the pronoun in every later
# transcript, and because a manual edit is trusted at once there was no second
# sighting to catch it.
#
# These are NOT refused -- US, IT and IN are all real corrections someone might
# want. They are demoted to the ordinary supervised path, so they need the same
# two sightings an automatic guess needs. Teaching Murmur `US` still works; it
# just takes saying it twice, which is the whole design of this module.
_CASE_ONLY_NEEDS_PROOF = frozenset("""
a an the and or but not so as at by for from in into of off on out to up with
i me my we us our you your he him his she her it its they them their this that
these those is am are was were be been being do does did have has had can could
may might must shall should will would if then than when where who whom whose
why how all any both each few more most no nor now one only other own same some
such too very just also even still back down over under again once here there
""".split())

# Deliberately NOT in the set above: "may" and "will", which are also the month
# and the name. A word list can only ever approximate this, and those two were
# costing an ordinary capitalisation its instant trust.
_CASE_ONLY_NEEDS_PROOF = _CASE_ONLY_NEEDS_PROOF - {"may", "will"}

_WORD_EDGES = " \t\n.,;:!?\"'()[]{}<>-–—/\\"


def _all_words_need_proof(wrong: str) -> bool:
    """Every word in the changed span is a common function word.

    Splitting on whitespace alone meant the token was `us,` with its comma
    attached, which is not in the set -- so one edit to "send it to us, then..."
    was trusted instantly and rewrote the pronoun in every later punctuated
    transcript. Transcripts are punctuated, so that is the COMMON form of the
    edit, not an edge case. Adjacent changed words ("it was" -> "IT WAS") arrive
    as one multi-word span and slipped past for the same reason.
    """
    words = [w.strip(_WORD_EDGES).casefold() for w in wrong.split()]
    words = [w for w in words if w]
    return bool(words) and all(w in _CASE_ONLY_NEEDS_PROOF for w in words)


def _is_case_only(wrong: str, right: str) -> bool:
    return wrong != right and wrong.casefold() == right.casefold()


def _already_correct(text: str, start: int, wrong: str, right: str) -> bool:
    """Is this match already sitting inside its own corrected form?

    `Labs -> Labs Inc` fires on the "Labs" inside an already-correct "Labs Inc"
    and produces "Labs Inc Inc". The single-pass rewrite stops runaway growth
    WITHIN one call, but does nothing when the input already contains the wrong
    form as a whole word -- which is exactly what corrected output looks like,
    so any transcript repeating a phrase the user had corrected got mangled.

    The wrong form can sit anywhere inside the right form, not just at its
    start: `Inc -> Labs Inc` matches the "Inc" at the END of "Labs Inc", so the
    window has to be anchored at each offset where `wrong` occurs in `right`.
    """
    at = right.find(wrong)
    while at >= 0:
        lo = start - at
        if lo >= 0 and text[lo:lo + len(right)] == right:
            return True
        at = right.find(wrong, at + 1)
    return False

class Vocabulary:
    def __init__(self, path, promote_after_hits: int = 2):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.promote_after_hits = promote_after_hits
        self._lock = threading.Lock()
        # A half-written database after a power cut used to raise here, at
        # construction, so Murmur did not start and nothing said why. The bad
        # file is moved aside rather than deleted, and the log names it.
        self._conn = open_store(self.path, SCHEMA)

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
            # A case-only change to a common word does not get the instant trust
            # a manual edit normally earns. `us -> US` from one edit rewrote the
            # pronoun everywhere; demoting it to the supervised path means it
            # still works, it just has to be seen twice.
            trusted = source == "manual" and not (
                _is_case_only(wrong, right) and _all_words_need_proof(wrong))
            promoted = 1 if (trusted or hits >= self.promote_after_hits) else 0
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

    @staticmethod
    def _clean_term(term: str) -> str:
        """Collapse whitespace, drop control characters, bound the length.

        Newlines matter most: a term carrying one can open a new line inside a
        prompt and stop looking like a term at all.
        """
        cleaned = "".join(
            " " if c in "\r\n\t" else c
            for c in (term or "")
            if c == " " or not (ord(c) < 32 or ord(c) == 127))
        return " ".join(cleaned.split())[:_TERM_MAX_CHARS]

    def hotwords(self, limit: int = HOTWORD_LIMIT) -> list[str]:
        """Sanitised, de-duplicated, and capped. Highest hit count first, so a
        cap drops the terms the user has confirmed least."""
        seen, out = set(), []
        for r in self._active():
            term = self._clean_term(r["term"])
            if not term or term in seen:
                continue
            seen.add(term)
            out.append(term)
            if limit is not None and len(out) >= limit:
                break
        return out

    def glossary(self) -> list[str]:
        return self.hotwords(limit=GLOSSARY_LIMIT)

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

        def substitute(m):
            wrong = m.group(0)
            right = replacements.get(wrong, wrong)
            if right == wrong or _already_correct(text, m.start(), wrong, right):
                return wrong
            return right

        pattern = _LEFT + "(?:" + "|".join(alternatives) + ")" + _RIGHT
        try:
            return re.sub(pattern, substitute, text)
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
