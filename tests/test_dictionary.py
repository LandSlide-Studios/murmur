"""Words can be added by hand, not only learned from an observed correction.

The learning path could only ever fire when Murmur happened to see the user fix
a paste — and someone dictating into a chat window does not go back and edit the
text. So a vocabulary that biases the recogniser for free stayed at four terms,
and every proper noun kept coming back misspelt.
"""
import pytest

from murmur.vocabulary import Vocabulary


@pytest.fixture
def vocab(tmp_path):
    v = Vocabulary(tmp_path / "v.db")
    yield v
    v.close()


def test_a_typed_term_is_active_immediately(vocab):
    """Typed in means intended. There is nothing to supervise."""
    assert vocab.add_term("Jim's Steakout") is True
    assert "Jim's Steakout" in vocab.hotwords()


def test_a_typed_term_needs_no_misheard_form(vocab):
    """The term alone biases the decoder, which is what fixes a misheard proper
    noun. Requiring the user to guess how Whisper will mangle it first would
    make the feature useless for the case it exists for."""
    vocab.add_term("Sidance")
    assert vocab.hotwords() == ["Sidance"]


def test_a_hotword_only_term_never_rewrites_the_transcript(vocab):
    """It has no substitution rule, and an empty alternative in the pattern
    would match at every position and splice the term across the whole text."""
    vocab.add_term("Sidance")
    for text in ["the invoice is ready", "", "a b c", "Sidance already correct"]:
        assert vocab.apply(text) == text


def test_a_term_can_carry_a_misheard_form_when_one_is_known(vocab):
    vocab.add_term("Peds MD", sounds_like="peds m d")
    assert vocab.apply("send it to peds m d please") == "send it to Peds MD please"
    assert "Peds MD" in vocab.hotwords()


def test_a_whole_list_goes_in_at_once(vocab):
    added = vocab.add_many([
        "Jim's Steakout",
        "Sidance",
        "Peds MD = peds m d",
        "",
        "   ",
        "Breckpoint",
    ])
    assert added == 4
    assert set(vocab.hotwords()) == {
        "Jim's Steakout", "Sidance", "Peds MD", "Breckpoint"}


def test_adding_the_same_term_twice_does_not_duplicate_it(vocab):
    vocab.add_term("Sidance")
    vocab.add_term("Sidance")
    assert vocab.hotwords().count("Sidance") == 1


def test_adding_a_term_reactivates_one_the_user_had_turned_off(vocab):
    vocab.add_term("Sidance")
    vocab.set_enabled("Sidance", False)
    assert "Sidance" not in vocab.hotwords()
    vocab.add_term("Sidance")
    assert "Sidance" in vocab.hotwords()


@pytest.mark.parametrize("junk", ["", "   ", "\n", "\t"])
def test_blank_input_adds_nothing(vocab, junk):
    assert vocab.add_term(junk) is False
    assert vocab.hotwords() == []


def test_a_typed_term_is_sanitised_like_any_other(vocab):
    """It reaches two different prompts, so it is untrusted input the same way
    a learned term is."""
    vocab.add_term("Acme\nCorp\x07")
    assert vocab.hotwords() == ["Acme Corp"]


def test_typed_terms_respect_the_hotword_cap(vocab):
    """Whisper conditions on ~224 tokens; past that the decoder truncates and
    the list starts costing accuracy instead of adding it."""
    from murmur.vocabulary import HOTWORD_LIMIT

    vocab.add_many([f"Term{i}" for i in range(500)])
    assert len(vocab.hotwords()) <= HOTWORD_LIMIT


def test_the_panel_adds_a_pasted_list_in_one_go(qapp, tmp_path):
    from murmur.ui.vocab_win import VocabWindow

    v = Vocabulary(tmp_path / "panel.db")
    w = VocabWindow(v)
    w.entry.setText("Jim's Steakout\nSidance\nPeds MD = peds m d")
    w._add()

    assert w.table.rowCount() == 3
    assert w.entry.text() == "", "the box should clear once the words are in"
    assert set(v.hotwords()) == {"Jim's Steakout", "Sidance", "Peds MD"}
    w.close()
    v.close()
