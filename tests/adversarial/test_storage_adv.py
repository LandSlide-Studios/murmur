"""Adversarial scenarios for Murmur's persistence and learning layer.

Air-gapped: written from murmur/history.py, murmur/vocabulary.py and
murmur/corrections.py alone -- no engineering log, no locked-decision list, no
sight of the existing unit tests.

SAFETY
------
Every store in this file is constructed through hist() / vocab(), which refuse
any path that is not inside pytest's tmp_path and explicitly refuse anything
under %APPDATA%/Murmur. The user's live dictation history is never opened.
"""

import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from murmur.corrections import (
    EXPIRE_AFTER_S,
    MIN_SIMILARITY,
    READBACK_AFTER_S,
    Corrections,
    diff_terms,
)
from murmur.history import History
from murmur.vocabulary import GLOSSARY_LIMIT, Vocabulary


# --------------------------------------------------------------------------
# safety guard + builders
# --------------------------------------------------------------------------

def _guarded(tmp_path, name):
    """Return tmp_path/name, refusing anything outside the pytest sandbox."""
    root = Path(tmp_path).resolve()
    p = (root / name)
    assert str(p.resolve()).startswith(str(root)), f"escaped tmp_path: {p}"
    appdata = os.environ.get("APPDATA")
    if appdata:
        live = str((Path(appdata) / "Murmur").resolve()).lower()
        assert not str(p.resolve()).lower().startswith(live), f"live store: {p}"
    return p


def hist(tmp_path, name="h.db") -> History:
    return History(_guarded(tmp_path, name))


def vocab(tmp_path, name="v.db", **kw) -> Vocabulary:
    return Vocabulary(_guarded(tmp_path, name), **kw)


def add(h, raw="raw text", polished=None, final=None, mode="hold",
        ms=1000, app="notepad.exe", title="Untitled", status="ok"):
    return h.add(raw,
                 raw if polished is None else polished,
                 raw if final is None else final,
                 mode, ms, app, title, status)


class Cfg:
    """Minimal stand-in for murmur.config.Config."""

    def __init__(self, **over):
        self.d = {"learning.enabled": True, "learning.uia_readback": True}
        self.d.update(over)

    def get(self, dotted, default=None):
        return self.d.get(dotted, default)


class FakeUIA:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    def snapshot(self):
        return {"handle": 1}

    def read(self, snap):
        self.reads += 1
        return self.text


def backdate(corr, seconds):
    with corr._lock:
        for p in corr._pending:
            p["t"] -= seconds


def test_guard_refuses_live_appdata_store(tmp_path):
    """The guard itself must reject the user's real database path."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        pytest.skip("no APPDATA on this machine")
    with pytest.raises(AssertionError):
        _guarded(Path(appdata) / "Murmur", "history.db")


# ==========================================================================
# HISTORY
# ==========================================================================

UNICODE_SAMPLES = {
    "emoji_zwj": "family \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d"
                 "\U0001f466 shipped \U0001f680",
    "rtl": "\u0642\u0627\u0644 \u0645\u0631\u062d\u0628\u0627 and "
           "\u05e9\u05dc\u05d5\u05dd world",
    "cjk": "\u65e5\u672c\u8a9e\u306e\u30c6\u30b9\u30c8 \u4e2d\u6587\u6d4b"
           "\u8bd5 \ud55c\uad6d\uc5b4",
    "combining": "cafe\u0301 versus caf\u00e9 and n\u0303 vs \u00f1",
    "zero_width": "a\u200bb\u200dc\ufeffd\u2060e",
    "astral": "\U0001d518\U0001d52b\U0001d526\U0001d520\U0001d52c"
              "\U0001d521\U0001d522",
    "control": "tab\tnewline\ncr\rvt\x0bff\x0c",
}


@pytest.mark.parametrize("name", sorted(UNICODE_SAMPLES))
def test_h_unicode_roundtrip(tmp_path, name):
    """Emoji/ZWJ, RTL, CJK, combining marks, zero-width, astral, controls."""
    text = UNICODE_SAMPLES[name]
    h = hist(tmp_path, f"u_{name}.db")
    try:
        rid = add(h, raw=text)
        got = h.get(rid)
        assert got["raw_text"] == text
        assert got["final_text"] == text
        # and it survives a round trip through the search path too
        assert [r["id"] for r in h.search(text)] == [rid]
    finally:
        h.close()


def test_h_nul_byte_roundtrip(tmp_path):
    """A NUL inside a transcript must not truncate or corrupt the row."""
    text = "before\x00after"
    h = hist(tmp_path)
    try:
        rid = add(h, raw=text)
        got = h.get(rid)
        assert got["raw_text"] == text, repr(got["raw_text"])
    finally:
        h.close()


def test_h_sql_metacharacters_are_inert(tmp_path):
    """Quotes, semicolons and a DROP payload are data, never statements."""
    payloads = [
        "'; DROP TABLE sessions; --",
        '" OR 1=1 --',
        "Robert'); DROP TABLE sessions;--",
        "back\\slash and 'single' and \"double\"",
        "-- comment /* block */",
    ]
    h = hist(tmp_path)
    try:
        ids = [add(h, raw=p) for p in payloads]
        assert len(h.recent()) == len(payloads)
        for rid, p in zip(ids, payloads):
            assert h.get(rid)["raw_text"] == p
        # table still exists and still holds everything
        assert len(h.search("DROP TABLE")) == 2
    finally:
        h.close()


def test_h_huge_transcript_roundtrip(tmp_path):
    """A 1.5 MB transcript stores and reads back byte-for-byte."""
    text = ("the quick brown fox jumps over the lazy dog. " * 35000)
    assert len(text) > 1_500_000
    h = hist(tmp_path)
    try:
        rid = add(h, raw=text)
        got = h.get(rid)
        assert len(got["raw_text"]) == len(text)
        assert got["raw_text"] == text
        assert len(h.recent(1)) == 1
    finally:
        h.close()


def test_h_empty_whitespace_and_none_fields(tmp_path):
    """Cancelled/failed sessions still get a row; empty is not lost."""
    h = hist(tmp_path)
    try:
        a = add(h, raw="")
        b = add(h, raw="   \t\n  ")
        c = h.add(None, None, None, "toggle", 0, None, None, status="failed")
        rows = {r["id"]: r for r in h.recent()}
        assert rows[a]["raw_text"] == ""
        assert rows[b]["raw_text"] == "   \t\n  "
        assert rows[c]["raw_text"] is None
        assert rows[c]["status"] == "failed"
        assert len(rows) == 3
    finally:
        h.close()


def test_h_recent_ordering_and_limit(tmp_path):
    """Newest first, even when several rows share a timestamp."""
    h = hist(tmp_path)
    try:
        ids = [add(h, raw=f"row {i}") for i in range(25)]
        got = [r["id"] for r in h.recent()]
        assert got == list(reversed(ids))
        assert [r["id"] for r in h.recent(5)] == list(reversed(ids))[:5]
        assert h.recent(0) == []
    finally:
        h.close()


def test_h_recent_negative_limit_returns_everything(tmp_path):
    """Documents SQLite's LIMIT -1 == unlimited leaking through the API."""
    h = hist(tmp_path)
    try:
        for i in range(12):
            add(h, raw=f"row {i}")
        assert len(h.recent(-1)) == 12       # not clamped, not empty
        assert len(h.search("row", limit=-1)) == 12
    finally:
        h.close()


def test_h_search_like_wildcards_are_literal(tmp_path):
    """% and _ in a query must match themselves, not everything."""
    h = hist(tmp_path)
    try:
        pct = add(h, raw="battery at 100% today")
        und = add(h, raw="the file is a_b.txt")
        add(h, raw="nothing special here")
        add(h, raw="also nothing")
        assert [r["id"] for r in h.search("%")] == [pct]
        assert [r["id"] for r in h.search("_")] == [und]
        assert [r["id"] for r in h.search("100%")] == [pct]
        assert [r["id"] for r in h.search("a_b")] == [und]
        # a wildcard-only query must not behave as "match all"
        assert len(h.search("%%")) == 0
    finally:
        h.close()


def test_h_search_backslash_is_literal(tmp_path):
    """The ESCAPE character itself must be searchable."""
    h = hist(tmp_path)
    try:
        rid = add(h, raw=r"path C:\Users\magli file")
        add(h, raw="unrelated")
        assert [r["id"] for r in h.search("\\")] == [rid]
        assert [r["id"] for r in h.search(r"C:\Users")] == [rid]
        assert len(h.search(r"\%")) == 0
    finally:
        h.close()


def test_h_search_empty_query_matches_all(tmp_path):
    h = hist(tmp_path)
    try:
        ids = [add(h, raw=f"r{i}") for i in range(4)]
        assert [r["id"] for r in h.search("")] == list(reversed(ids))
        assert [r["id"] for r in h.search("", limit=2)] == list(reversed(ids))[:2]
    finally:
        h.close()


def test_h_search_ignores_polished_text(tmp_path):
    """Documents a real coverage hole: polished_text is never searched."""
    h = hist(tmp_path)
    try:
        rid = h.add("alpha", "bravo", "charlie", "hold", 1, "a", "t")
        assert [r["id"] for r in h.search("alpha")] == [rid]
        assert [r["id"] for r in h.search("charlie")] == [rid]
        h.set_correction(rid, "delta")
        assert [r["id"] for r in h.search("delta")] == [rid]
        # bravo is stored but unreachable through the only search API
        assert h.search("bravo") == []
    finally:
        h.close()


def test_h_search_corrected_text_wins_after_edit(tmp_path):
    h = hist(tmp_path)
    try:
        rid = add(h, raw="call halvorsen tomorrow")
        h.set_correction(rid, "call Halvorsen tomorrow")
        assert [r["id"] for r in h.search("Halvorsen")] == [rid]
        assert h.get(rid)["corrected_text"] == "call Halvorsen tomorrow"
    finally:
        h.close()


def test_h_set_correction_on_unknown_row_is_silent(tmp_path):
    """No row, no error, no signal to the caller. Documented, not asserted OK."""
    h = hist(tmp_path)
    try:
        h.set_correction(999_999, "ghost")     # must not raise
        assert h.get(999_999) is None
        assert h.recent() == []
    finally:
        h.close()


def test_h_concurrent_writes_many_threads(tmp_path):
    """24 threads x 40 inserts: every row lands, every id is unique."""
    h = hist(tmp_path)
    errors = []
    n_threads, per = 24, 40
    try:
        def writer(k):
            try:
                for i in range(per):
                    add(h, raw=f"t{k}-{i}")
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=writer, args=(k,))
              for k in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        rows = h.recent(limit=10_000)
        assert len(rows) == n_threads * per
        assert len({r["id"] for r in rows}) == n_threads * per
    finally:
        h.close()


def test_h_concurrent_read_while_write(tmp_path):
    """Readers must never see a torn row or raise while writers hammer."""
    h = hist(tmp_path)
    errors, stop = [], threading.Event()
    try:
        def writer():
            try:
                for i in range(300):
                    add(h, raw=f"payload {i} " + "x" * 500)
            except Exception as e:            # noqa: BLE001
                errors.append(e)
            finally:
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    for r in h.recent(50):
                        assert r["raw_text"].startswith("payload ")
                    h.search("payload", limit=10)
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ws = [threading.Thread(target=writer) for _ in range(3)]
        rs = [threading.Thread(target=reader) for _ in range(4)]
        for t in ws + rs:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in rs:
            t.join()
        assert errors == []
        assert len(h.recent(limit=10_000)) == 900
    finally:
        h.close()


def test_h_two_instances_same_file(tmp_path):
    """Two History objects over one file (e.g. two app instances)."""
    p = _guarded(tmp_path, "shared.db")
    a, b = History(p), History(p)
    errors = []
    try:
        def writer(store, tag):
            try:
                for i in range(60):
                    add(store, raw=f"{tag}-{i}")
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=writer, args=(a, "a")),
              threading.Thread(target=writer, args=(b, "b"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == [], errors
        assert len(a.recent(limit=1000)) == 120
        assert len(b.recent(limit=1000)) == 120
    finally:
        a.close()
        b.close()


def test_h_reopen_after_close_keeps_data(tmp_path):
    p = _guarded(tmp_path, "reopen.db")
    h = History(p)
    rid = add(h, raw="persisted \u65e5\u672c\u8a9e")
    h.close()

    h2 = History(p)
    try:
        assert h2.get(rid)["raw_text"] == "persisted \u65e5\u672c\u8a9e"
        rid2 = add(h2, raw="second run")
        assert rid2 > rid
    finally:
        h2.close()


def test_h_use_after_close_raises_not_corrupts(tmp_path):
    p = _guarded(tmp_path, "closed.db")
    h = History(p)
    add(h, raw="one")
    h.close()
    with pytest.raises(sqlite3.ProgrammingError):
        add(h, raw="two")
    with pytest.raises(sqlite3.ProgrammingError):
        h.recent()
    h.close()                                  # double close must be safe
    h2 = History(p)
    try:
        assert len(h2.recent()) == 1           # the lost write is really lost
    finally:
        h2.close()


def test_h_corrupt_database_file(tmp_path):
    """A garbage file at the store path: what does startup do?"""
    p = _guarded(tmp_path, "corrupt.db")
    p.write_bytes(b"this is definitely not a sqlite database" * 40)
    with pytest.raises(sqlite3.DatabaseError) as ei:
        History(p)
    assert "not a database" in str(ei.value).lower()


def test_h_truncated_database_file(tmp_path):
    """A half-written db (crash during write) at startup."""
    p = _guarded(tmp_path, "trunc.db")
    h = History(p)
    for i in range(300):
        add(h, raw=f"row {i} " + "y" * 400)
    h.close()
    size = p.stat().st_size
    assert size > 8192
    with open(p, "r+b") as f:
        f.truncate(size // 3)

    with pytest.raises(sqlite3.DatabaseError):
        h2 = History(p)
        h2.recent(limit=10_000)


def test_h_db_path_is_a_directory(tmp_path):
    d = _guarded(tmp_path, "history.db")
    d.mkdir()
    with pytest.raises(sqlite3.OperationalError) as ei:
        History(d)
    assert "unable to open" in str(ei.value).lower()


def test_h_disk_full(tmp_path):
    """SQLITE_FULL via max_page_count. The store must survive the failure."""
    h = hist(tmp_path, "full.db")
    try:
        add(h, raw="before the wall")
        pages = h._conn.execute("PRAGMA page_count").fetchone()[0]
        h._conn.execute(f"PRAGMA max_page_count = {pages}")

        with pytest.raises(sqlite3.OperationalError) as ei:
            add(h, raw="Z" * 400_000)
        assert "full" in str(ei.value).lower()

        # the failed write must not have half-landed
        assert len(h.recent()) == 1
        # and the store must still be usable once space is back
        h._conn.execute("PRAGMA max_page_count = 1073741823")
        rid = add(h, raw="after the wall")
        assert h.get(rid)["raw_text"] == "after the wall"
        assert len(h.recent()) == 2
    finally:
        h.close()


def test_h_purge_keeps_newest(tmp_path):
    h = hist(tmp_path)
    try:
        ids = [add(h, raw=f"row {i}") for i in range(30)]
        h.purge(0)                              # documented as "unlimited"
        assert len(h.recent(limit=1000)) == 30
        h.purge(-5)
        assert len(h.recent(limit=1000)) == 30
        h.purge(10)
        kept = [r["id"] for r in h.recent(limit=1000)]
        assert kept == list(reversed(ids[-10:]))
    finally:
        h.close()


# ==========================================================================
# VOCABULARY
# ==========================================================================

def test_v_case_only_duplicates_are_separate_terms(tmp_path):
    """`cat->Cat` and `Cat->CAT` are different rows, by design of the PK."""
    v = vocab(tmp_path)
    try:
        assert v.observe("cat", "Cat", "manual") is True
        assert v.observe("Cat", "CAT", "manual") is True
        assert v.observe("CAT", "cat", "manual") is True
        terms = v.all_terms()
        assert len(terms) == 3
        # and the three rules do not chain within one apply()
        assert v.apply("cat Cat CAT") == "Cat CAT cat"
    finally:
        v.close()


def test_v_promotion_requires_two_auto_sightings(tmp_path):
    v = vocab(tmp_path)
    try:
        assert v.observe("halvorsen", "Halvorsen", "auto") is False
        assert v.apply("halvorsen") == "halvorsen"
        assert v.observe("halvorsen", "Halvorsen", "auto") is True
        assert v.apply("halvorsen") == "Halvorsen"
    finally:
        v.close()


def test_v_manual_is_trusted_immediately(tmp_path):
    v = vocab(tmp_path)
    try:
        assert v.observe("teh", "the", "manual") is True
        assert v.apply("teh cat") == "the cat"
        row = v.all_terms()[0]
        assert row["hit_count"] == 1 and row["promoted"] == 1
    finally:
        v.close()


def test_v_promote_after_hits_zero_has_no_floor(tmp_path):
    """A 0 or negative setting silently disables the two-sighting guarantee."""
    v = vocab(tmp_path, "z.db", promote_after_hits=0)
    try:
        promoted = v.observe("dana", "Ryan", "auto")
        assert promoted is False, (
            "promote_after_hits=0 promoted an automatic guess on its first "
            "sighting; the supervision contract has no lower bound")
    finally:
        v.close()


def test_v_identity_and_blank_observations_rejected(tmp_path):
    v = vocab(tmp_path)
    try:
        assert v.observe("cat", "cat", "manual") is False
        assert v.observe("  cat  ", "cat", "manual") is False   # strip -> equal
        assert v.observe("", "cat", "manual") is False
        assert v.observe("cat", "", "manual") is False
        assert v.observe("   ", "  ", "manual") is False
        assert v.observe(None, "cat", "manual") is False
        assert v.observe("cat", None, "manual") is False
        assert v.all_terms() == []
    finally:
        v.close()


def test_v_apply_is_single_pass_no_cascade(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("cat", "dog", "manual")
        v.observe("dog", "wolf", "manual")
        assert v.apply("cat and dog") == "dog and wolf"
    finally:
        v.close()


def test_v_apply_replacement_containing_original(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("vantage", "Vantage Labs", "manual")
        assert v.apply("we met vantage today") == "we met Vantage Labs today"
    finally:
        v.close()


def test_v_apply_is_idempotent(tmp_path):
    """Text that is ALREADY correct must survive apply() untouched.

    Two separate ways in: a single call on already-correct input, and a second
    call on this function's own output. The single-pass design fixes neither,
    because the wrong form still occurs as a whole word inside the replacement.
    """
    v = vocab(tmp_path)
    try:
        v.observe("Labs", "Labs Inc", "manual")
        assert v.apply("welcome to Labs") == "welcome to Labs Inc"

        # (a) one call, on text the user already typed correctly
        assert v.apply("welcome to Labs Inc") == "welcome to Labs Inc", (
            "apply() re-corrected text that was already in the target form")

        # (b) two calls on the pipeline's own output
        once = v.apply("welcome to Labs")
        twice = v.apply(once)
        assert twice == once, (
            f"second apply() grew the text: {once!r} -> {twice!r}")
    finally:
        v.close()


def test_v_apply_word_boundaries(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("cat", "dog", "manual")
        assert v.apply("category") == "category"
        assert v.apply("cats") == "cats"
        assert v.apply("concat") == "concat"
        assert v.apply("scatter cat") == "scatter dog"
        assert v.apply("the cat.") == "the dog."
        assert v.apply("cat's bowl") == "dog's bowl"
        # hyphen and slash are non-word characters, so these DO fire
        assert v.apply("cat-scan") == "dog-scan"
        assert v.apply("cat/nap") == "dog/nap"
    finally:
        v.close()


def test_v_apply_regex_metacharacters_in_wrong_form(tmp_path):
    v = vocab(tmp_path)
    try:
        for wrong, right in [
            ("c++", "C++"),
            ("a.*b", "AB"),
            ("(paren)", "PAREN"),
            ("x[y]z", "XYZ"),
            ("dollar$", "DOLLAR"),
            ("back\\slash", "BACKSLASH"),
            ("q?", "Q"),
            ("^caret", "CARET"),
        ]:
            assert v.observe(wrong, right, "manual") is True
        assert v.apply("i write c++ daily") == "i write C++ daily"
        assert v.apply("a.*b here") == "AB here"
        assert v.apply("aXXb here") == "aXXb here"       # not a live regex
        assert v.apply("(paren) and x[y]z") == "PAREN and XYZ"
        assert v.apply("back\\slash") == "BACKSLASH"
        assert v.apply("^caret") == "CARET"
    finally:
        v.close()


def test_v_apply_longest_wrong_form_wins(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("new york", "New York", "manual")
        v.observe("york", "York", "manual")
        assert v.apply("i live in new york now") == "i live in New York now"
        assert v.apply("york alone") == "York alone"
    finally:
        v.close()


def test_v_apply_same_wrong_form_two_targets_uses_hit_count(tmp_path):
    v = vocab(tmp_path)
    try:
        for _ in range(5):
            v.observe("teh", "the", "auto")
        for _ in range(2):
            v.observe("teh", "The", "auto")
        assert v.apply("teh end") == "the end"
    finally:
        v.close()


def test_v_apply_never_fires_on_cjk(tmp_path):
    """Documents that the \\w boundary makes substitution impossible in CJK."""
    v = vocab(tmp_path)
    try:
        v.observe("\u6771\u4eac", "\u6771\u4eac\u90fd", "manual")
        # standalone, with a space, it works
        assert v.apply("\u6771\u4eac") == "\u6771\u4eac\u90fd"
        # embedded in a real Japanese sentence -- no spaces -- it never fires
        sentence = "\u79c1\u306f\u6771\u4eac\u306b\u884c\u304f"
        assert v.apply(sentence) == sentence
    finally:
        v.close()


def test_v_unicode_normalisation_forms_are_distinct(tmp_path):
    """NFC/NFD 'cafe' are separate terms and never match each other."""
    nfc = unicodedata.normalize("NFC", "caf\u00e9")
    nfd = unicodedata.normalize("NFD", "caf\u00e9")
    assert nfc != nfd
    v = vocab(tmp_path)
    try:
        v.observe(nfd, "Cafe", "manual")
        assert v.apply(nfd) == "Cafe"
        assert v.apply(nfc) == nfc            # visually identical, untouched
        v.observe(nfc, "Cafe", "manual")
        assert len(v.all_terms()) == 2        # one rule per byte sequence
    finally:
        v.close()


def test_v_unicode_terms_roundtrip_and_apply(tmp_path):
    v = vocab(tmp_path)
    try:
        pairs = [
            ("\U0001f600", "\U0001f603"),                     # emoji
            ("\u05e9\u05dc\u05d5\u05dd", "\u05e9\u05dc\u05d5\u05dd!"),  # RTL
            ("a\u200db", "AB"),                               # ZWJ inside
            ("na\u0308ive", "naive"),                         # combining
        ]
        for w, r in pairs:
            assert v.observe(w, r, "manual") is True
        stored = {(t["wrong_form"], t["term"]) for t in v.all_terms()}
        assert stored == set(pairs)
        assert v.apply("say \U0001f600 now") == "say \U0001f603 now"
        assert v.apply("x a\u200db y") == "x AB y"
    finally:
        v.close()


def test_v_nul_byte_in_term(tmp_path):
    v = vocab(tmp_path)
    try:
        assert v.observe("bad\x00word", "good", "manual") is True
        assert v.all_terms()[0]["wrong_form"] == "bad\x00word"
        assert v.apply("a bad\x00word here") == "a good here"
    finally:
        v.close()


def test_v_many_terms_pattern_does_not_silently_break(tmp_path):
    """5000 promoted terms: apply() must still substitute, not no-op."""
    v = vocab(tmp_path, "many.db")
    try:
        rows = [(f"wrong{i}", f"Right{i}", 5, 1, 1, 0.0, 0.0)
                for i in range(5000)]
        v._conn.executemany(
            "INSERT INTO terms (wrong_form, term, hit_count, promoted,"
            " enabled, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)", rows)
        v._conn.commit()
        out = v.apply("start wrong0 middle wrong4999 end")
        assert out == "start Right0 middle Right4999 end", (
            "large vocabulary silently stopped applying (re.error swallowed)")
    finally:
        v.close()


def test_v_forget_then_readd_resets_promotion(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("dana", "Dana", "manual")
        assert v.apply("dana") == "Dana"
        v.forget("Dana", "dana")
        assert v.all_terms() == []
        assert v.apply("dana") == "dana"
        # re-added automatically: back to needing two sightings
        assert v.observe("dana", "Dana", "auto") is False
        assert v.apply("dana") == "dana"
        assert v.observe("dana", "Dana", "auto") is True
        assert v.apply("dana") == "Dana"
    finally:
        v.close()


def test_v_forget_without_wrong_form_removes_every_source(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("teh", "the", "manual")
        v.observe("hte", "the", "manual")
        v.observe("teh", "The", "manual")
        v.forget("the")
        assert {(t["wrong_form"], t["term"]) for t in v.all_terms()} == {
            ("teh", "The")}
    finally:
        v.close()


def test_v_disabled_term_stays_disabled_through_reobservation(tmp_path):
    """A user's explicit opt-out must not be undone by more sightings."""
    v = vocab(tmp_path)
    try:
        v.observe("us", "US", "manual")
        assert v.apply("tell us") == "tell US"
        v.set_enabled("US", False, "us")
        assert v.apply("tell us") == "tell us"
        for _ in range(5):
            v.observe("us", "US", "auto")
        assert v.apply("tell us") == "tell us", "re-enabled behind the user"
        assert v.all_terms()[0]["hit_count"] == 6
    finally:
        v.close()


def test_v_concurrent_observe_hit_count_is_exact(tmp_path):
    """16 threads x 25 observes of one pair -> hit_count must be exactly 400."""
    v = vocab(tmp_path, "conc.db")
    errors = []
    try:
        def worker():
            try:
                for _ in range(25):
                    v.observe("dana", "Dana", "auto")
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=worker) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        rows = v.all_terms()
        assert len(rows) == 1
        assert rows[0]["hit_count"] == 400, rows[0]["hit_count"]
    finally:
        v.close()


def test_v_concurrent_distinct_pairs_all_persist(tmp_path):
    v = vocab(tmp_path, "conc2.db")
    errors = []
    try:
        def worker(k):
            try:
                for i in range(30):
                    v.observe(f"w{k}_{i}", f"R{k}_{i}", "manual")
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        assert len(v.all_terms()) == 360
    finally:
        v.close()


def test_v_apply_while_observing(tmp_path):
    """Read-while-write: apply() must never raise or return a partial pattern."""
    v = vocab(tmp_path, "rw.db")
    v.observe("anchor", "ANCHOR", "manual")
    errors, stop = [], threading.Event()
    try:
        def writer():
            try:
                for i in range(200):
                    v.observe(f"w{i}", f"R{i}", "manual")
            except Exception as e:            # noqa: BLE001
                errors.append(e)
            finally:
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    out = v.apply("keep anchor stable")
                    assert out == "keep ANCHOR stable", out
                    v.hotwords()
                    v.glossary()
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ws = [threading.Thread(target=writer) for _ in range(2)]
        rs = [threading.Thread(target=reader) for _ in range(3)]
        for t in ws + rs:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in rs:
            t.join()
        assert errors == [], errors
    finally:
        v.close()


def test_v_hotwords_dedup_and_glossary_limit(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("teh", "the", "manual")
        v.observe("hte", "the", "manual")      # same target, two mishearings
        assert v.hotwords().count("the") == 1
        for i in range(GLOSSARY_LIMIT + 20):
            v.observe(f"w{i}", f"Term{i}", "manual")
        assert len(v.glossary()) == GLOSSARY_LIMIT
        assert len(v.hotwords()) == GLOSSARY_LIMIT + 21
        # disabled and unpromoted terms never reach the decoder
        v.observe("only-once", "OnlyOnce", "auto")
        assert "OnlyOnce" not in v.hotwords()
    finally:
        v.close()


def test_v_all_terms_ordering(tmp_path):
    v = vocab(tmp_path)
    try:
        for _ in range(3):
            v.observe("aaa", "Zed", "auto")
        v.observe("bbb", "Alpha", "auto")
        v.observe("ccc", "Alpha", "auto")
        rows = v.all_terms()
        assert [r["hit_count"] for r in rows] == [3, 1, 1]
        assert rows[0]["term"] == "Zed"
        assert [r["wrong_form"] for r in rows[1:]] == ["bbb", "ccc"]
    finally:
        v.close()


def test_v_reopen_after_close_keeps_promotion(tmp_path):
    p = _guarded(tmp_path, "vre.db")
    v = Vocabulary(p)
    v.observe("dana", "Dana", "manual")
    v.close()
    v2 = Vocabulary(p)
    try:
        assert v2.apply("dana") == "Dana"
        assert v2.all_terms()[0]["promoted"] == 1
    finally:
        v2.close()
    with pytest.raises(sqlite3.ProgrammingError):
        v.observe("x", "Y", "manual")


def test_v_corrupt_and_directory_paths(tmp_path):
    bad = _guarded(tmp_path, "vcorrupt.db")
    bad.write_bytes(b"not sqlite at all" * 60)
    with pytest.raises(sqlite3.DatabaseError):
        Vocabulary(bad)
    d = _guarded(tmp_path, "vdir.db")
    d.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        Vocabulary(d)


def test_v_apply_empty_and_whitespace(tmp_path):
    v = vocab(tmp_path)
    try:
        v.observe("cat", "dog", "manual")
        assert v.apply("") == ""
        assert v.apply(None) is None
        assert v.apply("   ") == "   "
        assert v.apply("\n\t") == "\n\t"
    finally:
        v.close()


# ==========================================================================
# CORRECTIONS / LEARNING
# ==========================================================================

def _corr(tmp_path, uia=None, **cfg):
    v = vocab(tmp_path, "cv.db")
    return v, Corrections(v, Cfg(**cfg), uia=uia)


def test_c_diff_terms_only_learns_replacements(tmp_path):
    assert diff_terms("call dana today", "call dana today and ryan") == []
    assert diff_terms("call dana today", "call today") == []          # delete
    got = diff_terms("call dana today", "call Dana today")
    assert got == [("dana", "Dana")]
    # more than MAX_TERM_WORDS on either side is discarded
    assert diff_terms("a b c d e f g h", "a z y x w v u h") == []


def test_c_diff_terms_similarity_floor(tmp_path):
    assert diff_terms("the quick brown fox jumps over",
                      "completely different sentence entirely") == []
    assert diff_terms("", "anything") == []
    assert diff_terms("anything", "") == []
    assert diff_terms("   ", "   x") == []
    assert diff_terms("same", "same") == []


def test_c_manual_edit_promotes_at_once(tmp_path):
    v, c = _corr(tmp_path)
    try:
        assert c.learn_from_edit("call halvorsen at 3", "call Halvorsen at 3") == 1
        assert v.apply("halvorsen") == "Halvorsen"
    finally:
        v.close()


def test_c_manual_case_fix_becomes_a_global_rewrite(tmp_path):
    """A one-off capitalisation edit rewrites the ordinary word everywhere."""
    v, c = _corr(tmp_path)
    try:
        c.learn_from_edit("send it to us marketing", "send it to US marketing")
        assert v.apply("that works for us") == "that works for us", (
            "learning `us -> US` from one edit now rewrites the pronoun in "
            "every later transcript")
    finally:
        v.close()


def test_c_manual_sentence_case_fix_does_not_leak_mid_sentence(tmp_path):
    v, c = _corr(tmp_path)
    try:
        c.learn_from_edit("hello there team", "Hello there team")
        assert v.apply("she said hello to me") == "she said hello to me", (
            "a sentence-initial capitalisation was learned as an "
            "unconditional substitution")
    finally:
        v.close()


def test_c_auto_edit_needs_two_sightings(tmp_path):
    v, c = _corr(tmp_path)
    try:
        assert c.learn_from_auto("call dana at 3", "call Dana at 3") == 0
        assert v.apply("dana") == "dana"
        assert c.learn_from_auto("meet dana later", "meet Dana later") == 1
        assert v.apply("dana") == "Dana"
    finally:
        v.close()


def test_c_learning_disabled_blocks_capture(tmp_path):
    v, c = _corr(tmp_path, uia=FakeUIA("x"), **{"learning.enabled": False})
    try:
        c.watch(1, "call dana at 3")
        assert c._pending == []
        assert c.offer_clipboard("call Dana at 3") == 0
        assert v.all_terms() == []
    finally:
        v.close()


def test_c_clipboard_ignores_our_own_paste(tmp_path):
    v, c = _corr(tmp_path)
    try:
        c.watch(1, "call dana at 3")
        assert c.offer_clipboard("call dana at 3") == 0
        assert c.offer_clipboard("  call dana at 3  ") == 0
        assert v.all_terms() == []
    finally:
        v.close()


def test_c_clipboard_ignores_our_paste_from_another_session(tmp_path):
    """Two consecutive dictations; copying the newer one must teach nothing."""
    v, c = _corr(tmp_path)
    try:
        c.watch(1, "send it to dana on monday")
        c.watch(2, "send it to ryan on monday")
        assert c.offer_clipboard("send it to ryan on monday") == 0
        assert v.all_terms() == []
    finally:
        v.close()


def test_c_clipboard_learns_only_from_the_best_match(tmp_path):
    """One clipboard event must not teach a rule against an unrelated paste."""
    v, c = _corr(tmp_path)
    try:
        c.watch(1, "email dana about the roof")
        c.watch(2, "email dan about the roof")
        c.offer_clipboard("email Dana about the roof")
        pairs = {(t["wrong_form"], t["term"]) for t in v.all_terms()}
        assert pairs == {("dana", "Dana")}, (
            f"one clipboard event taught rules against every pending paste: "
            f"{sorted(pairs)}")
    finally:
        v.close()


def test_c_repeated_clipboard_offer_must_not_promote(tmp_path):
    """The clipboard poller re-offering the same text is ONE user action."""
    v, c = _corr(tmp_path)
    try:
        c.watch(1, "meet dana monday")
        c.offer_clipboard("meet Dana monday")
        c.offer_clipboard("meet Dana monday")
        c.offer_clipboard("meet Dana monday")
        row = v.all_terms()[0]
        assert row["promoted"] == 0, (
            f"a single clipboard correction was counted {row['hit_count']} "
            f"times and promoted itself past the two-sighting rule")
        assert v.apply("dana") == "dana"
    finally:
        v.close()


def test_c_one_clipboard_event_promotes_via_duplicate_pendings(tmp_path):
    """Re-dictating after a bad paste leaves two pending entries.

    A single clipboard correction is then diffed against both, scores two
    hits, and promotes itself. No assumption about how often the caller polls
    the clipboard is needed for this one.
    """
    v, c = _corr(tmp_path)
    try:
        c.watch(1, "meet dana monday")       # first attempt
        c.watch(2, "meet dana monday")       # user repeated the dictation
        c.offer_clipboard("meet Dana monday")
        row = v.all_terms()[0]
        assert row["promoted"] == 0, (
            f"one clipboard correction scored {row['hit_count']} hits against "
            f"two pending copies of the same paste and promoted itself")
    finally:
        v.close()


def test_c_multiword_term_never_matches_across_whitespace(tmp_path):
    """Documents that a learned multi-word term is space-joined and brittle."""
    v, c = _corr(tmp_path)
    try:
        c.learn_from_edit("call dana smith now", "call Dana Smith now")
        assert {(t["wrong_form"], t["term"]) for t in v.all_terms()} == {
            ("dana smith", "Dana Smith")}
        assert v.apply("call dana smith now") == "call Dana Smith now"
        # a line break or a double space in a later transcript defeats it
        assert v.apply("call dana\nsmith now") == "call dana\nsmith now"
        assert v.apply("call dana  smith now") == "call dana  smith now"
    finally:
        v.close()


def test_c_repeated_uia_readback_must_not_promote(tmp_path):
    """poll() on a slow timer re-reads the same control over and over."""
    uia = FakeUIA("call Dana at 3")
    v, c = _corr(tmp_path, uia=uia)
    try:
        c.watch(7, "call dana at 3")
        backdate(c, READBACK_AFTER_S + 5)
        c.poll()
        c.poll()
        c.poll()
        assert uia.reads == 3
        row = v.all_terms()[0]
        assert row["promoted"] == 0, (
            f"one read-back was counted {row['hit_count']} times; the "
            f"'read' flag on the pending entry is never set")
        assert v.apply("dana") == "dana"
    finally:
        v.close()


def test_c_poll_before_readback_window_does_nothing(tmp_path):
    uia = FakeUIA("call Dana at 3")
    v, c = _corr(tmp_path, uia=uia)
    try:
        c.watch(7, "call dana at 3")
        assert c.poll() == 0
        assert uia.reads == 0
        assert v.all_terms() == []
    finally:
        v.close()


def test_c_poll_expires_and_drops(tmp_path):
    uia = FakeUIA("call Dana at 3")
    v, c = _corr(tmp_path, uia=uia)
    try:
        c.watch(7, "call dana at 3")
        backdate(c, EXPIRE_AFTER_S + 5)
        assert c.poll() == 0
        assert c._pending == []
        assert uia.reads == 0
        assert v.all_terms() == []
    finally:
        v.close()


def test_c_poll_keeps_entries_registered_mid_poll(tmp_path):
    """Concurrent watch()/poll(): nothing lost, nothing duplicated."""
    uia = FakeUIA(None)
    v, c = _corr(tmp_path, uia=uia)
    errors, stop = [], threading.Event()
    try:
        def watcher():
            try:
                for i in range(200):
                    c.watch(i, f"paste number {i}")
            except Exception as e:            # noqa: BLE001
                errors.append(e)
            finally:
                stop.set()

        def poller():
            try:
                while not stop.is_set():
                    c.poll()
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        ws = [threading.Thread(target=watcher) for _ in range(2)]
        ps = [threading.Thread(target=poller) for _ in range(3)]
        for t in ws + ps:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in ps:
            t.join()
        c.poll()
        assert errors == [], errors
        ids = [id(p) for p in c._pending]
        assert len(ids) == len(set(ids)), "pending list duplicated entries"
        assert len(c._pending) == 400, len(c._pending)
    finally:
        v.close()


def test_c_end_to_end_cat_does_not_break_category(tmp_path):
    """The classic word-boundary trap, all the way through the learner."""
    v, c = _corr(tmp_path)
    try:
        c.learn_from_edit("i put the cat outside", "i put the dog outside")
        text = "category cats concat the cat scattered"
        assert v.apply(text) == "category cats concat the dog scattered"
    finally:
        v.close()


def test_c_learning_from_our_own_corrected_output(tmp_path):
    """Round two: the already-corrected text must not teach a reverse rule."""
    v, c = _corr(tmp_path)
    try:
        c.learn_from_edit("call dana at 3", "call Dana at 3")
        corrected = v.apply("call dana at 3")
        assert corrected == "call Dana at 3"
        # the app pastes `corrected`; a later read-back returns the same thing
        assert c.learn_from_auto(corrected, corrected) == 0
        assert len(v.all_terms()) == 1
        # and the reverse direction was never learned
        assert {(t["wrong_form"], t["term"]) for t in v.all_terms()} == {
            ("dana", "Dana")}
    finally:
        v.close()


def test_c_huge_paste_diff_is_bounded(tmp_path):
    """A 1 MB paste with one word changed must still learn exactly one term."""
    v, c = _corr(tmp_path)
    try:
        base = ("the quick brown fox jumps over the lazy dog " * 12000)
        assert len(base) > 500_000
        original = base + "call dana today"
        edited = base + "call Dana today"
        t0 = time.time()
        n = c.learn_from_edit(original, edited)
        elapsed = time.time() - t0
        assert n == 1, n
        assert v.apply("dana") == "Dana"
        assert elapsed < 60, f"diff of a 0.5 MB paste took {elapsed:.1f}s"
    finally:
        v.close()
