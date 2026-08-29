import threading

from murmur.history import History


def add(h, **kw):
    base = dict(raw="um hello", polished="Hello.", final="Hello.", mode="hold",
                duration_ms=1200, app="notepad.exe", title="Untitled")
    base.update(kw)
    return h.add(**base)


def test_add_and_list_roundtrip(tmp_path):
    h = History(tmp_path / "h.db")
    add(h)
    rows = h.recent()
    assert len(rows) == 1
    assert rows[0]["final_text"] == "Hello."
    assert rows[0]["raw_text"] == "um hello"
    assert rows[0]["status"] == "ok"


def test_failed_sessions_are_still_recorded(tmp_path):
    """A crashed transcription must never silently cost the user their words."""
    h = History(tmp_path / "h.db")
    add(h, raw="lost words", polished=None, final=None, mode="toggle",
        status="error")
    row = h.recent()[0]
    assert row["raw_text"] == "lost words"
    assert row["status"] == "error"


def test_cancelled_sessions_are_recorded(tmp_path):
    h = History(tmp_path / "h.db")
    add(h, status="cancelled")
    assert h.recent()[0]["status"] == "cancelled"


def test_recent_is_newest_first(tmp_path):
    h = History(tmp_path / "h.db")
    add(h, raw="first", final="First.")
    add(h, raw="second", final="Second.")
    assert [r["final_text"] for r in h.recent()] == ["Second.", "First."]


def test_recent_respects_limit(tmp_path):
    h = History(tmp_path / "h.db")
    for i in range(10):
        add(h, raw=f"r{i}", final=f"F{i}")
    assert len(h.recent(limit=3)) == 3


def test_search_matches_raw_and_final(tmp_path):
    h = History(tmp_path / "h.db")
    add(h, raw="alpha bravo", final="Alpha bravo.")
    add(h, raw="charlie", final="Charlie.")
    assert len(h.search("bravo")) == 1
    assert len(h.search("charlie")) == 1


def test_search_is_case_insensitive(tmp_path):
    h = History(tmp_path / "h.db")
    add(h, raw="halvorsen", final="Halvorsen Law.")
    assert len(h.search("HALVORSEN")) == 1


def test_search_matches_the_corrected_text_too(tmp_path):
    h = History(tmp_path / "h.db")
    rid = add(h, raw="halvorsen", final="Halvorsen.")
    h.set_correction(rid, "Halvorsen Law Group.")
    assert len(h.search("Law Group")) == 1


def test_search_special_characters_are_literal_not_wildcards(tmp_path):
    h = History(tmp_path / "h.db")
    add(h, raw="one hundred percent", final="One hundred percent.")
    add(h, raw="fifty", final="Fifty.")
    assert h.search("%") == []            # % must not match everything


def test_set_correction_persists(tmp_path):
    h = History(tmp_path / "h.db")
    rid = add(h, raw="halvorsen", final="Halvorsen.")
    h.set_correction(rid, "Halvorsen Law.")
    assert h.recent()[0]["corrected_text"] == "Halvorsen Law."


def test_reopening_the_db_keeps_the_rows(tmp_path):
    p = tmp_path / "h.db"
    add(History(p), raw="persisted", final="Persisted.")
    assert History(p).recent()[0]["final_text"] == "Persisted."


def test_writes_from_several_threads_do_not_error(tmp_path):
    """The worker thread writes while the UI thread reads."""
    h = History(tmp_path / "h.db")
    errors = []

    def writer(n):
        try:
            for i in range(20):
                add(h, raw=f"t{n}-{i}", final=f"T{n}-{i}")
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(h.recent(limit=200)) == 80


def test_purge_keeps_only_the_newest(tmp_path):
    h = History(tmp_path / "h.db")
    for i in range(10):
        add(h, raw=f"r{i}", final=f"F{i}")
    h.purge(keep=4)
    rows = h.recent(limit=100)
    assert len(rows) == 4
    assert rows[0]["final_text"] == "F9"


def test_purge_with_zero_or_negative_keep_is_a_no_op(tmp_path):
    h = History(tmp_path / "h.db")
    add(h)
    h.purge(keep=0)
    assert len(h.recent()) == 1


# --- regression: a cancelled session must leave a trace ---

def test_a_cancelled_recording_is_recorded_even_with_no_text(tmp_path):
    """Cancelling drops the audio by design, but 'every session is recorded'
    is the whole promise of the history panel."""
    h = History(tmp_path / "h.db")
    h.add(raw=None, polished=None, final=None, mode="hold",
          duration_ms=4200, app="Code.exe", title="x", status="cancelled")
    row = h.recent()[0]
    assert row["status"] == "cancelled"
    assert row["duration_ms"] == 4200
    assert row["raw_text"] is None
