"""Capturing corrections, so the app learns what you actually said.

Three routes, most to least reliable:

  A. Manual  — you edit a transcript in the History panel. Always works, and
     is trusted immediately because the intent is explicit.
  B. UIA     — after pasting, the focused control's text is read back at ~20s
     and ~90s and diffed against what was inserted. Works for Win32 edits,
     RichEdit, UWP, and Chromium/Electron apps with accessibility on. Canvas
     editors expose nothing, so it degrades silently to C.
  C. Clipboard — if you copy text within 2 minutes that is a near-match to
     what was pasted, treat it as the corrected version.

B and C are best-effort and only ever feed the two-sighting automatic path.
Nothing here can promote a term on its own.
"""

import difflib
import logging
import threading
import time

log = logging.getLogger(__name__)

MIN_SIMILARITY = 0.6      # below this it is a rewrite, not a correction
MAX_TERM_WORDS = 4
READBACK_AFTER_S = 20
EXPIRE_AFTER_S = 120


def diff_terms(original: str, edited: str) -> list[tuple[str, str]]:
    """Extract (wrong, right) substitutions from an edit.

    Only 'replace' opcodes count. A pure insertion is the user adding a
    thought, not correcting a word, and learning it would corrupt every later
    transcript that happens to contain the neighbouring words.
    """
    if not original or not edited:
        return []
    a, b = original.split(), edited.split()
    if not a or not b:
        return []
    if difflib.SequenceMatcher(None, original, edited).ratio() < MIN_SIMILARITY:
        return []

    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "replace":
            continue
        if (i2 - i1) > MAX_TERM_WORDS or (j2 - j1) > MAX_TERM_WORDS:
            continue
        out.append((" ".join(a[i1:i2]), " ".join(b[j1:j2])))
    return out


class Corrections:
    def __init__(self, vocab, cfg, uia=None):
        self.vocab = vocab
        self.cfg = cfg
        self.uia = uia
        # Appended from the worker thread, rebuilt on the UI thread.
        self._lock = threading.Lock()
        self._pending: list[dict] = []

    # --- path A -----------------------------------------------------------

    def learn_from_edit(self, original: str, edited: str) -> int:
        """Manual correction from the History panel. Trusted immediately."""
        return sum(
            1 for wrong, right in diff_terms(original, edited)
            if self.vocab.observe(wrong, right, source="manual")
        )

    # --- paths B and C ----------------------------------------------------

    def learn_from_auto(self, original: str, observed: str) -> int:
        return sum(
            1 for wrong, right in diff_terms(original, observed)
            if self.vocab.observe(wrong, right, source="auto")
        )

    def watch(self, row_id: int, injected: str) -> None:
        """Register a paste so it can be read back later."""
        if not self.cfg.get("learning.enabled") or not injected:
            return
        snap = None
        if self.cfg.get("learning.uia_readback") and self.uia is not None:
            try:
                snap = self.uia.snapshot()
            except Exception:
                log.debug("UIA snapshot failed", exc_info=True)
        with self._lock:
            self._pending.append(
                {"id": row_id, "text": injected, "t": time.time(), "snap": snap,
                 "read": False})

    def poll(self) -> int:
        """Called on a slow timer. Re-reads pending pastes, drops expired ones."""
        learned, now, keep = 0, time.time(), []
        with self._lock:
            pending = list(self._pending)
        for p in pending:
            age = now - p["t"]
            if age > EXPIRE_AFTER_S:
                continue
            if self.uia is not None and p["snap"] and age > READBACK_AFTER_S:
                try:
                    current = self.uia.read(p["snap"])
                    if current and current.strip() != p["text"].strip():
                        learned += self.learn_from_auto(p["text"], current)
                except Exception:
                    log.debug("UIA read-back failed", exc_info=True)
            keep.append(p)
        with self._lock:
            # Anything registered while we were reading must survive.
            known = {id(p) for p in pending}
            self._pending = keep + [p for p in self._pending if id(p) not in known]
        return learned

    def offer_clipboard(self, clip_text: str) -> int:
        """Path C. The user copied something close to what we pasted.

        The obvious loop — diffing a paste against itself — is excluded by the
        `< 1.0` bound. The subtle one is not: Murmur puts every transcript on
        the clipboard itself, so session 2's paste would be diffed against
        session 1's still-pending entry. Two ordinary consecutive dictations
        ("...to Dana on Monday" then "...to Ryan on Monday") taught a permanent
        Dana -> Ryan substitution the user never asked for.

        So: if the clipboard holds text WE injected, it is not a correction.
        """
        if not clip_text or not self.cfg.get("learning.enabled"):
            return 0

        clean = clip_text.strip()
        with self._lock:
            pending = list(self._pending)
        if any(p["text"].strip() == clean for p in pending):
            return 0                       # our own paste, not the user's edit

        learned = 0
        for p in pending:
            ratio = difflib.SequenceMatcher(None, p["text"], clip_text).ratio()
            if MIN_SIMILARITY <= ratio < 1.0:
                learned += self.learn_from_auto(p["text"], clip_text)
        return learned
