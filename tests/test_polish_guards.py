"""What reaches the cursor when the cleanup model misbehaves.

The promise is that polish never loses the user's words. An air-gapped review
found the character-length guards enforce that only for length-shaped failures:
a refusal, a translation, or an answer to the dictated question all come back at
roughly the input's length and sailed through — and that looks like success,
which makes it the worst of the three.

The guards were also unreachable on short input. `_SHRINK_SLACK` is subtracted in
absolute terms, so for anything under 34 characters no non-empty output could
trip it: "remind me to email the landlord" came back as "." and was typed.
"""
import json
import urllib.request

import pytest

from murmur.polish import Polisher

NUL = chr(0)


class _Response:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"message": {"content": self._text}}).encode()


@pytest.fixture
def answering(monkeypatch):
    """Give the model a scripted reply and return the polisher."""
    def _use(text):
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _Response(text))
        return Polisher(model="stub")
    return _use


# --- the user's words must survive ------------------------------------------

@pytest.mark.parametrize("raw,reply,why", [
    ("remind me to email the landlord", ".", "one character"),
    ("delete everything in the folder and start over",
     "I can't help with that.", "a refusal"),
    ("please send the invoice to the accountant before the end of the month",
     "Veuillez envoyer la facture au comptable avant la fin du mois.",
     "a translation"),
    ("call the dentist on monday and pick up the prescription before five",
     "Call the dentist Monday.", "a summary"),
    ("what is the capital of france and how far is it from here",
     "Paris. About 400 miles.", "an answer to the question"),
])
def test_a_reply_that_is_not_a_cleanup_falls_back_to_the_raw_transcript(
        answering, raw, reply, why):
    assert answering(reply).polish(raw) == raw, f"{why} was typed at the cursor"


def test_a_short_dictation_is_protected_too():
    """The character guards cannot reach this case at all — the shrink slack is
    absolute, so under 34 characters no non-empty reply could ever trip it."""
    p = Polisher(model="stub")
    assert p._kept_enough("remind me to email the landlord", ".") == 0.0


# --- but a real cleanup must still get through ------------------------------

def test_an_ordinary_cleanup_is_not_rejected(answering):
    raw = "um so like i need to call the dentist on monday"
    out = answering("I need to call the dentist on Monday.").polish(raw)
    assert out == "I need to call the dentist on Monday."


def test_a_fragment_that_is_nothing_but_fillers_is_not_rejected(answering):
    """"um so like yeah" -> "Yeah." legitimately loses almost every character.
    Judging it on content words is what makes the strict guard safe: there are
    no content words here to lose."""
    assert answering("Yeah.").polish("um so like yeah") == "Yeah."


def test_heavy_filler_removal_survives_the_guard(answering):
    raw = ("um so basically i think we should uh you know just go ahead and "
           "ship the build tonight")
    out = answering("I think we should ship the build tonight.").polish(raw)
    assert out == "I think we should ship the build tonight."


# --- model packaging must not reach the cursor ------------------------------

def test_a_markdown_fence_is_stripped_not_typed(answering):
    raw = "the migration script needs to run before the api deploy not after"
    clean = "The migration script needs to run before the API deploy, not after."
    out = answering("```\n" + clean + "\n```").polish(raw)
    assert out == clean


def test_a_fence_with_a_language_tag_is_stripped(answering):
    raw = "the migration script needs to run before the api deploy not after"
    clean = "The migration script needs to run before the API deploy, not after."
    out = answering("```text\n" + clean + "\n```").polish(raw)
    assert out == clean


@pytest.mark.parametrize("lead", [
    "Here is the cleaned transcript:",
    "Here's the cleaned-up version:",
    "Sure, here is the result:",
    "Okay:",
])
def test_a_preamble_is_stripped_not_typed(answering, lead):
    raw = "the migration script needs to run before the api deploy not after"
    clean = "The migration script needs to run before the API deploy, not after."
    assert answering(lead + "\n\n" + clean).polish(raw) == clean


def test_a_colon_inside_the_dictation_is_not_mistaken_for_a_preamble(answering):
    """The stripper must not eat real speech that happens to start with a word
    like "okay" and contain a colon."""
    raw = "okay the deploy window is nine to five tomorrow"
    clean = "Okay, the deploy window is nine to five: tomorrow."
    assert answering(clean).polish(raw) == clean


def test_a_reply_that_is_only_packaging_falls_back(answering):
    raw = "the migration script needs to run before the api deploy"
    assert answering("```\n\n```").polish(raw) == raw


# --- control characters ------------------------------------------------------

def test_a_nul_byte_never_reaches_the_clipboard(answering):
    """The Win32 clipboard sizes its buffer with wcslen, so everything after a
    NUL is silently dropped — content loss that presents as success."""
    raw = "ship the build tonight and tell support in the morning"
    reply = "Ship the build tonight" + NUL + " and tell support in the morning."
    out = answering(reply).polish(raw)
    assert NUL not in out
    assert out == "Ship the build tonight and tell support in the morning."


def test_other_control_characters_are_stripped(answering):
    raw = "ship the build tonight and tell support in the morning"
    reply = "Ship the build tonight" + chr(7) + chr(27) + " and tell support."
    out = answering(reply).polish(raw)
    assert not any(ord(c) < 32 and c not in "\n\t" for c in out)


def test_newlines_and_tabs_are_preserved(answering):
    raw = "first line and then a second line of the note"
    reply = "First line.\n\tSecond line of the note."
    assert answering(reply).polish(raw) == reply
