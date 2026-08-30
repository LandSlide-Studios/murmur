"""The user's words are not rewritten, duplicated, or lost.

Tier 1 of the adversarial audit remediation. Each of these reproduces a defect
that reached the document: text corrupted by a learned correction, a rule
invented from a single event, a transcript pasted twice, a dictation dropped at
shutdown.
"""
import threading
import time

import pytest

from murmur.corrections import Corrections
from murmur.vocabulary import Vocabulary


def vocab(tmp_path, name="v.db"):
    return Vocabulary(tmp_path / name)


class FakeCfg:
    def __init__(self, **over):
        self._d = {"learning.enabled": True, "learning.uia_readback": False}
        self._d.update(over)

    def get(self, key, default=None):
        return self._d.get(key, default)


# --- corrections must not corrupt text that is already correct --------------

@pytest.mark.parametrize("wrong,right,already", [
    ("Labs", "Labs Inc", "welcome to Labs Inc"),
    ("Inc", "Labs Inc", "welcome to Labs Inc"),
    ("vantage", "Vantage Labs", "we use Vantage Labs"),
    ("Point", "Point Break", "watch Point Break tonight"),
    ("core", "core team", "tell the core team"),
])
def test_applying_a_correction_to_correct_text_changes_nothing(
        tmp_path, wrong, right, already):
    """`Labs -> Labs Inc` turned "welcome to Labs Inc" into "Labs Inc Inc".
    The wrong form can sit anywhere inside the right one, including at its end,
    so the guard cannot just look forward from the match."""
    v = vocab(tmp_path, f"{wrong}.db")
    v.observe(wrong, right, "manual")
    assert v.apply(already) == already
    v.close()


@pytest.mark.parametrize("wrong,right,text", [
    ("Labs", "Labs Inc", "welcome to Labs"),
    ("Inc", "Labs Inc", "welcome to Inc"),
    ("teh", "the", "teh cat and teh dog"),
    ("halvorsen", "Halvorsen", "call halvorsen"),
])
def test_a_correction_that_is_still_needed_still_fires(tmp_path, wrong, right, text):
    """The counterweight: the guard must not disable the feature."""
    v = vocab(tmp_path, f"n{wrong}.db")
    v.observe(wrong, right, "manual")
    assert v.apply(text) != text
    assert right in v.apply(text)
    v.close()


def test_apply_is_idempotent(tmp_path):
    v = vocab(tmp_path)
    for wrong, right in [("Labs", "Labs Inc"), ("teh", "the"),
                         ("kubernetes", "Kubernetes"), ("Inc", "Labs Inc")]:
        v.observe(wrong, right, "manual")
    for text in ["welcome to Labs", "teh Labs Inc", "deploy kubernetes to Labs",
                 "Labs Inc and Kubernetes", ""]:
        once = v.apply(text)
        assert v.apply(once) == once, f"not idempotent for {text!r}"
    v.close()


def test_a_correction_containing_its_own_wrong_form_does_not_grow(tmp_path):
    """The original growth defect, still guarded."""
    v = vocab(tmp_path)
    v.observe("vantage", "Vantage Labs", "manual")
    out = v.apply("vantage vantage vantage")
    assert "Labs Labs" not in out
    v.close()


# --- a one-off case fix must not become a global rewrite --------------------

def test_a_case_fix_on_a_common_word_needs_a_second_sighting(tmp_path):
    """Learning `us -> US` from one edit rewrote the pronoun in every later
    transcript. A manual edit is normally trusted at once; this one is not."""
    v = vocab(tmp_path)
    v.observe("us", "US", "manual")
    assert v.apply("that works for us") == "that works for us"
    v.observe("us", "US", "manual")
    assert v.apply("that works for us") == "that works for US"
    v.close()


@pytest.mark.parametrize("wrong,right", [
    ("halvorsen", "Halvorsen"),
    ("kubernetes", "Kubernetes"),
    ("mabel", "Mabel"),
    ("postgres", "Postgres"),
])
def test_an_ordinary_capitalisation_is_still_trusted_at_once(tmp_path, wrong, right):
    """Capitalising proper nouns is most of what this feature is for. Only
    common function words lose the instant trust."""
    v = vocab(tmp_path, f"{wrong}.db")
    v.observe(wrong, right, "manual")
    assert right in v.apply(f"about {wrong} today")
    v.close()


def test_a_non_case_change_to_a_common_word_is_still_trusted(tmp_path):
    """The rule is about case-only edits, not about the word being common."""
    v = vocab(tmp_path)
    v.observe("teh", "the", "manual")
    assert v.apply("teh cat") == "the cat"
    v.close()


# --- supervision needs two real sightings, not one counted twice ------------

def test_one_clipboard_event_cannot_promote_itself(tmp_path):
    """Two pending entries for a phrase the user re-dictated after a bad paste
    meant a single clipboard event scored two hits and promoted itself."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "i called halvorsen today about the photos")
    c.watch(2, "i called halvorsen today about the photos")
    c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" not in v.hotwords()
    v.close()


def test_two_genuine_sightings_still_promote(tmp_path):
    """The counterweight, and the case that caught a flaw in the first fix:
    two identical pastes tie on score, so filtering the already-counted entry
    has to happen BEFORE the best is chosen, or the second sighting is lost."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    for row in (1, 2):
        c.watch(row, "i called halvorsen today about the photos")
        c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" in v.hotwords()
    v.close()


def test_offering_the_same_clipboard_text_repeatedly_counts_once(tmp_path):
    """A clipboard poller re-offering unchanged content, or the user pressing
    Ctrl+C twice, must not promote a guess on its own."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "i called halvorsen today about the photos")
    for _ in range(10):
        c.offer_clipboard("i called Halvorsen today about the photos")
    assert "Halvorsen" not in v.hotwords()
    v.close()


def test_one_event_does_not_teach_a_rule_against_every_pending_paste(tmp_path):
    """Two dictations sharing phrasing taught the correct `dana -> Dana` AND an
    invented `dan -> Dana` from the same single clipboard event."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about the roof")
    c.watch(2, "email dan about the roof")
    c.offer_clipboard("email Dana about the roof")
    learned = {(t["wrong_form"], t["term"]) for t in v.all_terms()}
    assert ("dan", "Dana") not in learned, f"invented a rule: {learned}"
    assert len(learned) <= 1
    v.close()


def test_a_read_back_polled_many_times_counts_once(tmp_path):
    """poll() re-reads for the whole 20s..120s window, and the `read` flag the
    pending entry carried was written and never read anywhere."""
    class FakeUia:
        def read(self, snap):
            return "i called Halvorsen today"

    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg(**{"learning.uia_readback": True}))
    c.uia = FakeUia()
    c.watch(1, "i called halvorsen today")
    for p in c._pending:
        p["t"] -= 60                      # push it past the read-back delay
        p["snap"] = object()              # anything truthy
    for _ in range(20):
        c.poll()
    assert "Halvorsen" not in v.hotwords()
    v.close()


# --- two dictations must not become one -------------------------------------

def test_overlapping_injections_each_paste_their_own_text():
    """There was no lock in the injector, and copy/paste are split so an
    animation can run between them. A second dictation setting the clipboard
    inside the first one's settle window made both presses paste the second
    transcript."""
    from murmur.inject import Injector

    pasted = []
    inj = Injector(clipboard_settle_s=0.02)
    inj._release_modifiers = lambda: True
    inj._get_clipboard = lambda: ""
    current = {"text": None}

    def set_clip(t):
        current["text"] = t
        time.sleep(0.01)

    inj._set_clipboard = set_clip
    inj._send_paste = lambda: pasted.append(current["text"])

    threads = [threading.Thread(target=inj.inject, args=(t,))
               for t in ("FIRST transcript", "SECOND transcript")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(pasted) == ["FIRST transcript", "SECOND transcript"], \
        f"a transcript never reached the screen: {pasted}"


def test_the_injector_lock_is_reentrant():
    """inject() calls the same primitives copy() and paste() take the lock for."""
    from murmur.inject import Injector

    inj = Injector()
    with inj._lock:
        with inj._lock:
            pass
