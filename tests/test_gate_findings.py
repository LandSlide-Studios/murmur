"""Defects the air-gapped gate reviewers found in the remediation itself.

These are the ones that matter most in the whole suite: every one is a bug I
introduced while fixing a bug, and every one was invisible to me and obvious to
someone reading the code without my reasoning.
"""
import sqlite3
import threading
import time

import pytest

from murmur.corrections import Corrections
from murmur.history import History
from murmur.vocabulary import Vocabulary


class FakeCfg:
    def get(self, key, default=None):
        return {"learning.enabled": True, "learning.uia_readback": False}.get(key, default)


# --- the learner: the fix moved the bug rather than removing it -------------

def test_a_repeated_offer_cannot_fall_through_to_another_pending_entry(tmp_path):
    """Excluding already-counted entries BEFORE choosing the best match meant a
    repeated offer was merely barred from the entry it had used, and fell
    through to the next one — which yielded the same pair, promoted it on a
    single user action after all, and invented a rule from an unrelated paste."""
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about the roof")
    c.watch(2, "email dana about the shed")
    for _ in range(5):
        c.offer_clipboard("email Dana about the roof")

    assert "Dana" not in v.hotwords(), "promoted from one user action"
    learned = {(t["wrong_form"], t["term"]) for t in v.all_terms()}
    assert ("shed", "roof") not in learned, f"invented a rule: {learned}"
    v.close()


def test_a_repeat_cannot_invent_a_rule_from_an_unrelated_dictation(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.watch(1, "call dan today")
    c.watch(2, "call dana today")
    for _ in range(5):
        c.offer_clipboard("call Dana today")
    learned = {(t["wrong_form"], t["term"]) for t in v.all_terms()}
    assert ("dan", "Dana") not in learned, f"invented a rule: {learned}"
    v.close()


def test_two_genuine_sightings_still_promote(tmp_path):
    """The counterweight. A new paste between offers is a new event."""
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    for row in (1, 2):
        c.watch(row, "i called halvorsen today about the photos")
        c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" in v.hotwords()
    v.close()


# --- the case rule saw neither punctuation nor multi-word edits -------------

@pytest.mark.parametrize("wrong,right", [
    ("us,", "US,"),
    ("us.", "US."),
    ("it was", "IT WAS"),
    ("to us,", "to US,"),
    ('"us"', '"US"'),
])
def test_punctuation_and_multi_word_edits_do_not_bypass_the_case_rule(
        tmp_path, wrong, right):
    """Splitting on whitespace alone left the comma attached, so `us,` was not
    in the set and was trusted instantly — rewriting the pronoun in every later
    punctuated transcript. Transcripts are punctuated, so that is the common
    form of the edit, not an edge case."""
    v = Vocabulary(tmp_path / f"{abs(hash(wrong))}.db")
    assert v.observe(wrong, right, "manual") is False
    v.close()


@pytest.mark.parametrize("wrong,right", [
    ("may", "May"),          # the month
    ("will", "Will"),        # the name
    ("halvorsen", "Halvorsen"),
    ("dana smith", "Dana Smith"),
])
def test_an_ordinary_capitalisation_is_still_trusted_at_once(tmp_path, wrong, right):
    """A word list can only approximate this, and two of its entries were
    costing real capitalisations their instant trust."""
    v = Vocabulary(tmp_path / f"{abs(hash(wrong))}.db")
    assert v.observe(wrong, right, "manual") is True
    v.close()


# --- the copy/paste window a lock could never close -------------------------

def test_a_clipboard_write_between_copy_and_paste_does_not_hijack_the_paste():
    """copy() and paste() are split so the animation can run between them, and
    the lock is released in that gap — so a lock alone never closed this. One
    click on Copy in the history panel would otherwise paste a history row into
    the user's document instead of the dictation."""
    from murmur.inject import Injector

    clip = {"text": ""}
    pasted = []
    inj = Injector()
    inj._release_modifiers = lambda: True
    inj._get_clipboard = lambda: clip["text"]
    inj._set_clipboard = lambda t: clip.__setitem__("text", t)
    inj._send_paste = lambda: pasted.append(clip["text"])

    inj.copy("the dictation")
    clip["text"] = "a history row the user clicked copy on"   # the interloper
    inj.paste()

    assert pasted == ["the dictation"]


def test_two_overlapping_copy_paste_pairs_each_deliver_their_own_text():
    from murmur.inject import Injector

    clip = {"text": ""}
    pasted = []
    inj = Injector()
    inj._release_modifiers = lambda: True
    inj._get_clipboard = lambda: clip["text"]
    inj._set_clipboard = lambda t: clip.__setitem__("text", t)
    inj._send_paste = lambda: pasted.append(clip["text"])

    inj.copy("alpha")
    inj.paste()
    inj.copy("beta")
    inj.paste()
    assert pasted == ["alpha", "beta"]


def test_copying_does_not_block_another_thread_for_the_modifier_spin():
    """copy() held the lock across a wait of up to release_timeout_s for a
    physically-held key — the ordinary hold-to-talk case — stalling the UI
    thread's paste() behind it. That thread is not supposed to block at all."""
    from murmur.inject import Injector

    inj = Injector(release_timeout_s=0.5)
    started = threading.Event()

    def slow_release():
        started.set()
        time.sleep(0.4)
        return True

    inj._release_modifiers = slow_release
    inj._set_clipboard = lambda t: None
    inj._get_clipboard = lambda: ""
    inj._send_paste = lambda: None

    t = threading.Thread(target=lambda: inj.copy("x"), daemon=True)
    t.start()
    started.wait(1.0)
    time.sleep(0.05)

    began = time.perf_counter()
    with inj._lock:
        pass
    assert time.perf_counter() - began < 0.2, "blocked on the modifier wait"
    t.join(timeout=2.0)


# --- the quarantine must never take a healthy database ----------------------

def test_a_healthy_database_with_an_older_shape_is_never_quarantined(tmp_path):
    """CREATE TABLE IF NOT EXISTS no-ops against an existing table, so an older
    column set survives a schema bump — and the next CREATE INDEX on a new
    column raises. Quarantining on that would displace every existing user's
    history the first time a column is ever added."""
    path = tmp_path / "history.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, "
                "timestamp REAL, mode TEXT, text TEXT)")
    con.executemany("INSERT INTO sessions (timestamp, mode, text) VALUES (?,?,?)",
                    [(1.0, "hold", f"row {i}") for i in range(500)])
    con.commit()
    con.close()

    h = History(path)
    assert list(tmp_path.glob("*.corrupt-*")) == [], "displaced a healthy database"
    kept = h._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert kept == 500
    h.close()


def test_a_genuinely_corrupt_database_is_still_quarantined(tmp_path):
    """The counterweight — the health check must not disable the quarantine."""
    path = tmp_path / "history.db"
    path.write_bytes(b"definitely not a database" * 200)
    h = History(path)
    assert len(list(tmp_path.glob("*.corrupt-*"))) == 1
    h.close()


def test_a_locked_database_is_not_treated_as_damaged(tmp_path):
    """A lock says nothing about the file's health, and the rename only failed
    on Windows by luck of the sharing flags — the same path elsewhere would
    move a live, healthy store aside."""
    from murmur.store import _is_readable

    path = tmp_path / "h.db"
    History(path).close()
    holder = sqlite3.connect(str(path))
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert _is_readable(path) is True
    finally:
        holder.rollback()
        holder.close()
