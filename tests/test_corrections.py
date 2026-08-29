from murmur.corrections import Corrections, diff_terms
from murmur.vocabulary import Vocabulary


# --- diffing -----------------------------------------------------------------

def test_single_word_substitution_is_extracted():
    assert diff_terms("i called halvorsen today", "i called Halvorsen today") == \
        [("halvorsen", "Halvorsen")]


def test_multiword_replacement_is_extracted():
    assert diff_terms("we use land slide studios", "we use Landslide Studios") == \
        [("land slide studios", "Landslide Studios")]


def test_identical_text_yields_nothing():
    assert diff_terms("same text", "same text") == []


def test_pure_insertion_is_ignored():
    """Adding a thought is not correcting a word; learning it would corrupt
    later transcripts."""
    assert diff_terms("hello there", "hello there friend") == []


def test_pure_deletion_is_ignored():
    assert diff_terms("hello there friend", "hello there") == []


def test_wholesale_rewrite_is_rejected_as_too_dissimilar():
    assert diff_terms("alpha bravo charlie", "completely different words here") == []


def test_a_very_long_replacement_is_rejected():
    a = "one two three four five"
    b = "alpha beta gamma delta epsilon zeta eta theta"
    assert diff_terms(a, b) == []


def test_empty_inputs_yield_nothing():
    assert diff_terms("", "something") == []
    assert diff_terms("something", "") == []


def test_multiple_substitutions_are_all_extracted():
    got = diff_terms("call halvorsen about priya", "call Halvorsen about Priya")
    assert ("halvorsen", "Halvorsen") in got
    assert ("priya", "Priya") in got


# --- capture paths -----------------------------------------------------------

class FakeCfg:
    def __init__(self, **kw):
        self.data = {"learning.enabled": True, "learning.uia_readback": True}
        self.data.update(kw)

    def get(self, key, default=None):
        return self.data.get(key, default)


def test_manual_edit_learns_immediately(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    assert c.learn_from_edit("i called halvorsen", "i called Halvorsen") == 1
    assert "Halvorsen" in v.hotwords()


def test_auto_capture_needs_two_sightings(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.learn_from_auto("i called halvorsen", "i called Halvorsen")
    assert v.hotwords() == []
    c.learn_from_auto("i called halvorsen", "i called Halvorsen")
    assert "Halvorsen" in v.hotwords()


def test_clipboard_path_learns_from_a_near_identical_copy(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.watch(1, "i called halvorsen today about the photos")
    c.offer_clipboard("i called Halvorsen today about the photos")
    c.watch(2, "i called halvorsen today about the photos")
    c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" in v.hotwords()


def test_clipboard_path_ignores_unrelated_text(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.watch(1, "i called halvorsen today about the photos")
    assert c.offer_clipboard("a totally unrelated shopping list") == 0


def test_clipboard_path_ignores_an_identical_copy(tmp_path):
    """Copying what we pasted is not a correction."""
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    text = "i called halvorsen today"
    c.watch(1, text)
    assert c.offer_clipboard(text) == 0


def test_watch_is_a_noop_when_learning_is_disabled(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg(**{"learning.enabled": False}))
    c.watch(1, "anything")
    assert c.offer_clipboard("anything else") == 0


def test_pending_entries_expire(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    c.watch(1, "i called halvorsen today")
    c._pending[0]["t"] -= 999          # pretend it is old
    c.poll()
    assert c._pending == []


def test_uia_readback_learns_from_the_focused_control(tmp_path):
    v = Vocabulary(tmp_path / "v.db")

    class FakeUia:
        def snapshot(self):
            return {"id": 1}

        def read(self, _snap):
            return "i called Halvorsen today"

    c = Corrections(v, FakeCfg(), uia=FakeUia())
    for _ in range(2):
        c.watch(1, "i called halvorsen today")
        c._pending[-1]["t"] -= 30      # past the 20s read-back point
        c.poll()
    assert "Halvorsen" in v.hotwords()


def test_uia_failure_degrades_silently(tmp_path):
    v = Vocabulary(tmp_path / "v.db")

    class BrokenUia:
        def snapshot(self):
            return {"id": 1}

        def read(self, _snap):
            raise RuntimeError("control is gone")

    c = Corrections(v, FakeCfg(), uia=BrokenUia())
    c.watch(1, "i called halvorsen today")
    c._pending[-1]["t"] -= 30
    assert c.poll() == 0               # must not raise


# --- regressions from the final adversarial review ---

def test_our_own_paste_is_never_treated_as_a_correction(tmp_path):
    """Murmur puts every transcript on the clipboard itself. Session 2's paste
    was being diffed against session 1's pending entry, teaching a permanent
    substitution the user never asked for."""
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    s1 = "send the invoice to Dana on Monday morning"
    s2 = "send the invoice to Ryan on Monday morning"
    c.watch(1, s1)
    c.watch(2, s2)
    assert c.offer_clipboard(s2) == 0        # this is our own paste
    assert c.offer_clipboard(s1) == 0
    assert v.all_terms() == []


def test_two_consecutive_similar_dictations_teach_nothing(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    for i, text in enumerate(("call Dana about the roof",
                              "call Ryan about the roof")):
        c.watch(i, text)
        c.offer_clipboard(text)
    assert v.apply("Dana signed it") == "Dana signed it"


def test_a_genuine_user_edit_is_still_learned(tmp_path):
    """The guard must not block the real path it protects."""
    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    for _ in range(2):
        c.watch(1, "i called halvorsen today about the photos")
        c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" in v.hotwords()


def test_watch_from_another_thread_while_polling_is_safe(tmp_path):
    import threading

    v = Vocabulary(tmp_path / "v.db")
    c = Corrections(v, FakeCfg())
    errors = []

    def writer():
        try:
            for i in range(200):
                c.watch(i, f"text number {i}")
        except Exception as e:      # pragma: no cover
            errors.append(e)

    def poller():
        try:
            for _ in range(200):
                c.poll()
        except Exception as e:      # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer), threading.Thread(target=poller)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
