"""Nothing degrades the transcript without saying so.

Tier 4 of the audit remediation. Everything here kept working; it just worked
less well, with nothing to tell the user.
"""
from murmur.history import History
from murmur.vocabulary import HOTWORD_LIMIT, Vocabulary


def promoted(v, wrong, right):
    v.observe(wrong, right, "manual")


# --- the learned term set reaches its consumers sanitised and bounded --------

def test_hotwords_are_capped(tmp_path):
    """Whisper conditions on ~224 tokens. Past that the decoder silently
    truncates, so an unbounded list does not merely fail to help — it costs
    accuracy at the earliest point in the pipeline, where nothing downstream
    can recover it."""
    v = Vocabulary(tmp_path / "v.db")
    for i in range(500):
        promoted(v, f"wrong{i}", f"Term{i}")
    assert len(v.hotwords()) <= HOTWORD_LIMIT
    v.close()


def test_hotwords_carry_no_newlines_or_control_characters(tmp_path):
    """`polish.py` documented its copy of this list as untrusted and sanitised
    it; the transcriber joined the same list raw."""
    v = Vocabulary(tmp_path / "v.db")
    promoted(v, "acme", "Acme\nCorp")
    promoted(v, "beta", "Beta\tLabs")
    promoted(v, "gamma", "Gamma" + chr(0) + "Inc")
    for term in v.hotwords():
        assert "\n" not in term and "\t" not in term
        assert not any(ord(c) < 32 or ord(c) == 127 for c in term)
    v.close()


def test_a_very_long_term_is_bounded(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    promoted(v, "x", "Y" * 500)
    assert all(len(t) <= 60 for t in v.hotwords())
    v.close()


def test_the_glossary_is_a_subset_of_the_hotwords(tmp_path):
    """Two lists sanitised separately are how they diverged in the first place."""
    v = Vocabulary(tmp_path / "v.db")
    for i in range(100):
        promoted(v, f"w{i}", f"Term{i}")
    assert set(v.glossary()) <= set(v.hotwords())
    assert len(v.glossary()) <= 40
    v.close()


def test_ordinary_terms_survive_sanitising(tmp_path):
    """The counterweight: the cleaner must not eat real vocabulary."""
    v = Vocabulary(tmp_path / "v.db")
    for wrong, right in [("halvorsen", "Halvorsen"), ("c plus plus", "C++"),
                         ("dana smith", "Dana Smith"), ("postgres", "PostgreSQL")]:
        promoted(v, wrong, right)
    words = v.hotwords()
    for expected in ("Halvorsen", "C++", "Dana Smith", "PostgreSQL"):
        assert expected in words
    v.close()


def test_the_cleanup_prompt_cannot_be_grown_without_limit():
    """Each term was capped and the COUNT was not, so five thousand learned
    terms grew the system prompt to 49,782 characters — on the latency path of
    every dictation."""
    from murmur.polish import Polisher

    p = Polisher(model="stub")
    prompt = p._system_prompt([f"Term{i}" for i in range(5000)])
    assert len(prompt) < 8000, f"system prompt grew to {len(prompt)} chars"


# --- the meter forgets while it is listening, not only while talking ---------

def test_the_adaptive_gain_decays_during_silence():
    """The decay lived only in step(), which runs while the user is TALKING —
    so the gain only forgot during speech, the opposite of the model. A cough
    pinned it, and the first word after a pause read ~56% of its height."""
    from murmur.ui.waveform import BarModel

    m = BarModel(n=15)
    for _ in range(60):
        m.step(level=0.20, dt=1 / 60)          # a cough pins the peak
    pinned = m._peak
    for _ in range(60 * 30):                   # thirty seconds of silence
        m.flat(dt=1 / 60)
    assert m._peak < pinned / 2, f"peak still {m._peak:.4f}, was {pinned:.4f}"


def test_the_gain_also_decays_while_breathing():
    from murmur.ui.waveform import BarModel

    m = BarModel(n=15)
    for _ in range(60):
        m.step(level=0.20, dt=1 / 60)
    pinned = m._peak
    for _ in range(60 * 30):
        m.breathe(dt=1 / 60)
    assert m._peak < pinned / 2


def test_the_silent_row_still_looks_the_same_after_the_decay():
    """Decaying the gain must not change what silence renders as."""
    from murmur.ui.waveform import FLAT, BarModel

    m = BarModel(n=15)
    for _ in range(300):
        m.flat(dt=1 / 60)
    h = m.heights()
    assert max(h) - min(h) < 0.02
    assert abs(sum(h) / len(h) - FLAT) < 0.03


# --- history search reaches every column it stores ---------------------------

def test_search_finds_text_held_only_in_the_cleaned_column(tmp_path):
    """It searched raw, final and corrected. Anything held only in the polished
    column was unreachable through the one search API the panel has."""
    h = History(tmp_path / "h.db")
    h.add(raw="raw words", polished="a distinctive cleaned phrase",
          final=None, mode="hold", duration_ms=100, app="x", title="y",
          status="ok")
    assert len(h.search("distinctive cleaned")) == 1
    h.close()


def test_search_still_finds_the_other_columns(tmp_path):
    h = History(tmp_path / "h.db")
    h.add(raw="alpha raw", polished="beta polished", final="gamma final",
          mode="hold", duration_ms=100, app="x", title="y", status="ok")
    for needle in ("alpha", "beta", "gamma"):
        assert len(h.search(needle)) == 1, needle
    assert h.search("nothing here at all") == []
    h.close()
