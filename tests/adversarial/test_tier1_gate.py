"""Tier-1 adversarial gate.

Attacks five claims made about a recent change:

  1. Vocabulary.apply() is idempotent.
  2. A case-only correction of a common function word needs two sightings.
  3. The learner counts one observation exactly once.
  4. Injector serialises injections behind a re-entrant lock.
  5. MurmurApp.stop() drains an in-flight dictation before closing the stores.

SAFETY: nothing in this file touches the real clipboard, sends a real
keystroke, opens an audio device, or writes to the live %APPDATA% store. Every
Win32 and pyperclip entry point is overridden on a subclass; stores live in
tmp_path.
"""

import sqlite3
import threading
import time
import unicodedata

import pytest

from murmur.corrections import Corrections
from murmur.inject import Injector
from murmur.vocabulary import Vocabulary


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class FakeCfg:
    def __init__(self, **kw):
        self._d = {"learning.enabled": True, "learning.uia_readback": False}
        self._d.update(kw)

    def get(self, key, default=None):
        return self._d.get(key, default)


def vocab(tmp_path, name="v.db", promote_after_hits=2):
    return Vocabulary(tmp_path / name, promote_after_hits=promote_after_hits)


def taught(tmp_path, pairs, name="v.db"):
    """A Vocabulary with `pairs` already promoted, without going through the
    supervision rules under test."""
    v = vocab(tmp_path, name)
    for wrong, right in pairs:
        # two automatic sightings: the ordinary promotion path, no manual trust
        v.observe(wrong, right, source="auto")
        v.observe(wrong, right, source="auto")
    return v


def active_pairs(v):
    return {(r["wrong_form"], r["term"]) for r in v.all_terms()
            if r["promoted"] and r["enabled"]}


class SafeInjector(Injector):
    """Injector with every OS boundary replaced. No clipboard, no keystrokes."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clipboard = ""
        self.pastes = []
        self.held = False
        self.copy_delay = 0.0
        self.fail_set = False

    def _get_clipboard(self):
        return self.clipboard

    def _set_clipboard(self, text):
        if self.fail_set:
            raise RuntimeError("clipboard unavailable")
        if self.copy_delay:
            time.sleep(self.copy_delay)
        self.clipboard = text

    def _release_modifiers(self):
        return not self.held

    def _send_paste(self):
        self.pastes.append(self.clipboard)


class StubStt:
    def __init__(self, text="hello world", block=None):
        self.text = text
        self.block = block
        self.calls = 0

    def transcribe(self, pcm, hotwords=None):
        self.calls += 1
        if self.block is not None:
            self.block.wait(10)
        return self.text


class StubSounds:
    def duration_ms(self, name):
        return 0

    def play(self, name):
        pass


class StubFsm:
    def release_session(self):
        pass


class StubHotkeys:
    def __init__(self):
        self.fsm = StubFsm()
        self.stopped = False

    def stop(self):
        self.stopped = True


def arm(app, monkeypatch, stt=None):
    """Make a real MurmurApp safe to drive: no audio device, no keystrokes,
    no sound, no Win32 window queries."""
    import murmur.app as appmod

    monkeypatch.setattr(appmod, "foreground_window", lambda: ("test.exe", "t"))
    monkeypatch.setattr(appmod, "peak_rms", lambda pcm, sr: 1.0)
    app.hotkeys = StubHotkeys()
    app.sounds = StubSounds()
    app.injector = SafeInjector()
    app.polisher.polish = lambda raw, glossary=None: raw
    app._stt = stt or StubStt()
    app._cursor_point = lambda: (0, 0)
    return app


def rows_in(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()


# ==========================================================================
# CLAIM 1 -- apply() is idempotent
# ==========================================================================

def test_A01_correction_still_fires_when_needed(tmp_path):
    v = taught(tmp_path, [("Labs", "Labs Inc")])
    assert v.apply("I work at Vantage Labs today") == "I work at Vantage Labs Inc today"


def test_A02_idempotent_on_already_corrected_text(tmp_path):
    v = taught(tmp_path, [("Labs", "Labs Inc")])
    out = v.apply("I work at Vantage Labs Inc today")
    assert out == "I work at Vantage Labs Inc today"


def test_A03_wrong_form_at_the_end_of_the_right_form(tmp_path):
    v = taught(tmp_path, [("Inc", "Labs Inc")])
    assert v.apply("Vantage Labs Inc filed") == "Vantage Labs Inc filed"
    assert v.apply("Vantage Inc filed") == "Vantage Labs Inc filed"


def test_A04_wrong_form_twice_inside_its_own_right_form(tmp_path):
    v = taught(tmp_path, [("Duran", "Duran Duran")])
    assert v.apply("Duran Duran played") == "Duran Duran played"
    assert v.apply("Duran played") == "Duran Duran played"


def test_A05_correction_at_the_very_start_of_the_text(tmp_path):
    v = taught(tmp_path, [("Labs", "Labs Inc")])
    assert v.apply("Labs Inc filed") == "Labs Inc filed"
    assert v.apply("Labs filed") == "Labs Inc filed"


def test_A06_correction_at_the_very_end_of_the_text(tmp_path):
    v = taught(tmp_path, [("Inc", "Labs Inc")])
    assert v.apply("we joined Labs Inc") == "we joined Labs Inc"
    assert v.apply("we joined Inc") == "we joined Labs Inc"


def test_A07_wrong_form_inside_a_longer_word_is_untouched(tmp_path):
    v = taught(tmp_path, [("cat", "cat and dog")])
    assert v.apply("please concatenate the cats") == "please concatenate the cats"


def test_A08_right_form_present_but_this_match_is_a_different_word(tmp_path):
    v = taught(tmp_path, [("Inc", "Vantage Inc")])
    out = v.apply("Vantage Inc bought Acme Inc")
    assert out == "Vantage Inc bought Acme Vantage Inc"


def test_A09_no_chaining_within_one_pass(tmp_path):
    v = taught(tmp_path, [("cat", "dog"), ("dog", "wolf")])
    assert v.apply("one cat") == "one dog"


def test_A10_fuzz_single_correction_is_idempotent(tmp_path):
    import itertools
    import random

    rnd = random.Random(20260830)
    words = ["a", "b", "ab", "x y", "b a", "ab b", "y", "x"]
    failures = []
    for i, (wrong, right) in enumerate(itertools.product(words, words)):
        if wrong == right:
            continue
        v = taught(tmp_path, [(wrong, right)], name=f"fz{i}.db")
        for _ in range(6):
            text = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 5)))
            once = v.apply(text)
            twice = v.apply(once)
            if once != twice:
                failures.append((wrong, right, text, once, twice))
        v.close()
    assert not failures, f"apply() not idempotent for {failures[:5]}"


def test_A11_unicode_already_correct_decomposed_form(tmp_path):
    v = taught(tmp_path, [("cafe", unicodedata.normalize("NFC", "café"))])
    decomposed = unicodedata.normalize("NFD", "café")     # c a f e + U+0301
    text = f"we met at the {decomposed} today"
    out = v.apply(text)
    assert out == text, (
        "already-correct (decomposed) text was rewritten to "
        f"{[hex(ord(ch)) for ch in out.split()[4]]}")


def test_A12_multi_word_wrong_form_idempotence(tmp_path):
    v = taught(tmp_path, [("New York", "New York, New York")])
    assert v.apply("live in New York, New York now") == "live in New York, New York now"
    assert v.apply("live in New York now") == "live in New York, New York now"


# ==========================================================================
# CLAIM 2 -- case-only corrections of function words need two sightings
# ==========================================================================

def test_B01_case_only_function_word_needs_two_sightings(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("us", "US", source="manual") is False
    assert v.apply("he told us today") == "he told us today"
    assert v.observe("us", "US", source="manual") is True
    assert v.apply("he told us today") == "he told US today"


def test_B02_proper_noun_capitalisation_still_trusted_at_once(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("halvorsen", "Halvorsen", source="manual") is True


def test_B03_technical_term_capitalisation_still_trusted_at_once(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("kubernetes", "Kubernetes", source="manual") is True
    assert v.observe("api", "API", source="manual") is True


def test_B04_case_plus_something_else_is_not_case_only(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("the", "They", source="manual") is True


def test_B05_multi_word_case_only_function_words_bypass_the_rule(tmp_path):
    """Two function words changed together are not in the guard list, so one
    manual edit rewrites them everywhere -- the exact harm the rule exists for."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.learn_from_edit("the meeting is at noon and it was fine",
                      "the meeting is at noon and IT WAS fine")
    assert ("it was", "IT WAS") not in active_pairs(v), (
        "a case-only change to two common function words was trusted after one "
        f"edit: {active_pairs(v)} -- and now rewrites unrelated text: "
        f"{v.apply('it was raining so it was cancelled')!r}")


def test_B06_punctuation_attached_function_word_bypasses_the_rule(tmp_path):
    """diff_terms splits on whitespace, so the token the user actually edits is
    'us,' -- which is not in the guard list."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.learn_from_edit("please send the report to us, then archive it",
                      "please send the report to US, then archive it")
    assert ("us,", "US,") not in active_pairs(v), (
        "the pronoun rewrite the rule blocks is reachable in one edit through a "
        f"trailing comma: {active_pairs(v)} -- unrelated text now becomes "
        f"{v.apply('he gave it to us, and left')!r}")


def test_B07_month_and_name_capitalisation_still_trusted_at_once(tmp_path):
    """'May' and 'Will' are proper nouns that happen to sit in the function-word
    list. The claim says ordinary capitalisations stay instant."""
    v = vocab(tmp_path)
    assert v.observe("may", "May", source="manual") is True, \
        "capitalising the month May now needs a second sighting"
    assert v.observe("will", "Will", source="manual") is True, \
        "capitalising the name Will now needs a second sighting"


def test_B08_auto_path_unchanged_by_the_case_rule(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("halvorsen", "Halvorsen", source="auto") is False
    assert v.observe("halvorsen", "Halvorsen", source="auto") is True


def test_B09_single_function_word_edit_does_not_rewrite_later_text(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.learn_from_edit("he told us the news", "he told US the news")
    assert v.apply("that suits us fine") == "that suits us fine"


# ==========================================================================
# CLAIM 3 -- one observation is counted once
# ==========================================================================

class StubUia:
    def __init__(self, reads):
        self.reads = list(reads)

    def snapshot(self):
        return {"h": 1}

    def read(self, snap):
        return self.reads.pop(0) if self.reads else None


def aged(c, seconds=30.0):
    with c._lock:
        for p in c._pending:
            p["t"] -= seconds


def test_C01_repeated_identical_readbacks_count_once(tmp_path):
    v = vocab(tmp_path)
    uia = StubUia(["email Dana about it"] * 3)
    c = Corrections(v, FakeCfg(**{"learning.uia_readback": True}), uia=uia)
    c.watch(1, "email dana about it")
    aged(c)
    for _ in range(3):
        c.poll()
    row = [r for r in v.all_terms() if r["wrong_form"] == "dana"]
    assert row and row[0]["hit_count"] == 1, f"{v.all_terms()}"
    assert not row[0]["promoted"]


def test_C02_two_genuinely_different_readbacks_still_promote(tmp_path):
    v = vocab(tmp_path)
    uia = StubUia(["email Dana about it", "email Dana about it now"])
    c = Corrections(v, FakeCfg(**{"learning.uia_readback": True}), uia=uia)
    c.watch(1, "email dana about it")
    aged(c)
    c.poll()
    c.poll()
    row = [r for r in v.all_terms() if r["wrong_form"] == "dana"]
    assert row and row[0]["promoted"], f"{v.all_terms()}"


def test_C03_repeated_identical_clipboard_offers_count_once(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about it")
    c.offer_clipboard("email Dana about it")
    c.offer_clipboard("email Dana about it")
    row = [r for r in v.all_terms() if r["wrong_form"] == "dana"]
    assert row and row[0]["hit_count"] == 1, f"{v.all_terms()}"


def test_C04_two_identical_pending_entries_promote_on_two_offers(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about it")
    c.watch(2, "email dana about it")
    c.offer_clipboard("email Dana about it")
    c.offer_clipboard("email Dana about it")
    assert ("dana", "Dana") in active_pairs(v), f"{v.all_terms()}"


def test_C05_repeat_offer_must_not_invent_a_correction_from_another_entry(tmp_path):
    """Two DIFFERENT pending dictations. One clipboard event, offered twice.

    The second offer must not fall through to the runner-up and manufacture a
    substitution the user never made."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "call dan today")            # a different, earlier dictation
    c.watch(2, "call dana today")           # the one the user corrected
    c.offer_clipboard("call Dana today")
    c.offer_clipboard("call Dana today")
    pairs = {(r["wrong_form"], r["term"]) for r in v.all_terms()}
    assert ("dan", "Dana") not in pairs, (
        "a repeated clipboard offer taught a substitution from an unrelated "
        f"pending dictation: {v.all_terms()}")


def test_C11_one_clipboard_event_cannot_promote_past_the_two_sighting_rule(tmp_path):
    """One clipboard content, offered twice, against two pending dictations that
    share phrasing. The first offer takes the best match; the second is barred
    from it and falls to the runner-up, which yields the SAME pair -- so a
    single user action reaches two hits and promotes."""
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about the roof")
    c.watch(2, "email dana about the shed")
    c.offer_clipboard("email Dana about the roof")
    c.offer_clipboard("email Dana about the roof")
    assert ("dana", "Dana") not in active_pairs(v), (
        "one clipboard event promoted a term the user confirmed once: "
        f"{v.all_terms()}")


def test_C06_two_corrections_in_one_event_are_both_learned(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about the ruf")
    c.offer_clipboard("email Dana about the roof")
    pairs = {(r["wrong_form"], r["term"]) for r in v.all_terms()}
    assert ("dana", "Dana") in pairs and ("ruf", "roof") in pairs, f"{pairs}"


def test_C07_our_own_paste_is_not_a_correction(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about it")
    assert c.offer_clipboard("email dana about it") == 0
    assert v.all_terms() == []


def test_C08_poll_keeps_entries_registered_during_the_poll(tmp_path):
    v = vocab(tmp_path)
    holder = {}

    class RacingUia(StubUia):
        def read(self, snap):
            holder["c"].watch(99, "a later dictation")   # registered mid-poll
            return None

    c = Corrections(v, FakeCfg(**{"learning.uia_readback": True}),
                    uia=RacingUia([]))
    holder["c"] = c
    c.watch(1, "email dana about it")
    aged(c)
    c.poll()
    ids = {p["id"] for p in c._pending}
    assert ids == {1, 99}, f"{ids}"


def test_C09_expired_entries_are_dropped(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "old text")
    aged(c, 500.0)
    c.watch(2, "new text")
    c.poll()
    assert {p["id"] for p in c._pending} == {2}


def test_C10_concurrent_offers_of_one_event_count_once(tmp_path):
    v = vocab(tmp_path)
    c = Corrections(v, FakeCfg())
    c.watch(1, "email dana about it")
    start = threading.Barrier(4)

    def go():
        start.wait()
        c.offer_clipboard("email Dana about it")

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    row = [r for r in v.all_terms() if r["wrong_form"] == "dana"]
    assert row and row[0]["hit_count"] == 1, f"{v.all_terms()}"


# ==========================================================================
# CLAIM 4 -- Injector serialises injections
# ==========================================================================

def test_D01_two_overlapping_injects_each_paste_their_own_text():
    inj = SafeInjector(clipboard_settle_s=0.0)
    inj.copy_delay = 0.02
    threads = [threading.Thread(target=inj.inject, args=(t,))
               for t in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(inj.pastes) == ["alpha", "beta"], inj.pastes


def test_D02_split_copy_paste_still_interleaves():
    """The comet path is copy() ... 260ms of animation ... paste(). The lock is
    released between the two, so a second dictation landing inside that window
    replaces the first one's clipboard and BOTH Ctrl+V presses deliver it."""
    inj = SafeInjector()
    a_copied = threading.Event()
    b_copied = threading.Event()
    a_pasted = threading.Event()

    def worker_a():
        inj.copy("alpha")
        a_copied.set()
        b_copied.wait(5)
        inj.paste()
        a_pasted.set()

    def worker_b():
        a_copied.wait(5)
        inj.copy("beta")
        b_copied.set()
        a_pasted.wait(5)
        inj.paste()

    ta, tb = threading.Thread(target=worker_a), threading.Thread(target=worker_b)
    ta.start()
    tb.start()
    ta.join(5)
    tb.join(5)
    assert inj.pastes == ["alpha", "beta"], (
        f"each injection must paste its own text; got {inj.pastes}")


def test_D03_external_clipboard_write_inside_the_copy_paste_window():
    """The History panel's copy button calls injector._set_clipboard directly,
    outside the lock."""
    inj = SafeInjector()
    inj.copy("the dictation")
    inj._set_clipboard("a history row the user clicked copy on")
    inj.paste()
    assert inj.pastes == ["the dictation"], inj.pastes


def test_D04_lock_is_reentrant_and_does_not_deadlock():
    inj = SafeInjector(clipboard_settle_s=0.0)
    done = []

    def reentrant(text):
        # a callback under the lock that takes the lock again
        inj.clipboard = text
        inj.paste()
        done.append(text)

    inj._set_clipboard = reentrant
    t = threading.Thread(target=inj.inject, args=("x",), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "inject() deadlocked on its own lock"
    assert done == ["x"]


def test_D05_exception_inside_inject_does_not_leave_the_lock_held():
    inj = SafeInjector(clipboard_settle_s=0.0)
    inj.fail_set = True
    with pytest.raises(RuntimeError):
        inj.inject("boom")
    inj.fail_set = False
    ok = []
    t = threading.Thread(target=lambda: ok.append(inj.inject("after")),
                         daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "lock was left held by the raising call"
    assert ok == [True]


def test_D07_lock_does_not_stall_the_ui_thread_on_the_modifier_spin():
    """copy() runs on the worker and holds the lock across _release_modifiers,
    which spins up to release_timeout_s (0.5s by default). paste() runs on the
    Qt thread when the comet lands. Before the lock existed the UI thread never
    waited on the worker at all."""
    inj = SafeInjector(release_timeout_s=0.5)
    slow = threading.Event()

    calls = []

    def slow_release():
        calls.append(1)
        if len(calls) == 1:                    # only the worker's copy() spins
            slow.set()
            time.sleep(inj.release_timeout_s)  # what the real spin does
        return True

    inj._release_modifiers = slow_release
    worker = threading.Thread(target=inj.copy, args=("dictation",), daemon=True)
    worker.start()
    slow.wait(2)
    t0 = time.perf_counter()
    inj.paste()
    blocked = time.perf_counter() - t0
    worker.join(5)
    assert blocked < 0.15, f"UI thread blocked {blocked * 1000:.0f}ms on the injector lock"


def test_D06_held_modifier_refuses_to_paste_but_still_copies():
    inj = SafeInjector()
    inj.held = True
    assert inj.copy("text") is False
    assert inj.clipboard == "text"
    assert inj.paste() is False
    assert inj.pastes == []


# ==========================================================================
# CLAIM 5 -- stop() drains an in-flight dictation
# ==========================================================================

def _start_worker(app):
    app._worker = threading.Thread(target=app._run_worker, daemon=True,
                                   name="gate-worker")
    app._worker.start()
    return app._worker


def test_E01_queued_dictation_gets_a_history_row(app, monkeypatch):
    from murmur.app import Session

    arm(app, monkeypatch)
    db = app.history.path
    _start_worker(app)
    app._jobs.put((b"", Session(1, "hold"), 1200))
    app.stop()
    assert rows_in(db) == 1, "the queued dictation left no history row"


def test_E02_stop_without_start_does_not_raise(app, monkeypatch):
    arm(app, monkeypatch)
    app.stop()


def test_E03_stop_twice_does_not_raise(app, monkeypatch):
    from murmur.app import Session

    arm(app, monkeypatch)
    _start_worker(app)
    app._jobs.put((b"", Session(1, "hold"), 1200))
    app.stop()
    app.stop()


def test_E04_wedged_worker_does_not_block_the_quit(app, monkeypatch):
    from murmur.app import Session

    gate = threading.Event()
    arm(app, monkeypatch, stt=StubStt(block=gate))
    app.shutdown_drain_s = 0.3
    _start_worker(app)
    app._jobs.put((b"", Session(1, "hold"), 1200))
    time.sleep(0.15)
    t0 = time.perf_counter()
    try:
        app.stop()
        elapsed = time.perf_counter() - t0
    finally:
        gate.set()
    assert elapsed < 3.0, f"stop() took {elapsed:.1f}s"


def test_E05_drain_timeout_still_writes_the_history_row(app, monkeypatch):
    """When the drain expires the stores close under a live worker, so the
    dictation loses BOTH its delivery and its history row -- the outcome the
    drain was added to prevent."""
    from murmur.app import Session

    gate = threading.Event()
    arm(app, monkeypatch, stt=StubStt(block=gate))
    app.shutdown_drain_s = 0.3
    db = app.history.path
    _start_worker(app)
    app._jobs.put((b"", Session(1, "hold"), 1200))
    time.sleep(0.15)
    app.stop()
    gate.set()
    app._worker.join(5)
    assert rows_in(db) == 1, \
        "the dictation in flight at the drain deadline lost its history row"


def test_E06_quitting_mid_recording_leaves_a_history_row(app, monkeypatch):
    """CLAUDE.md: every session writes exactly one history row."""
    from murmur.app import Session

    arm(app, monkeypatch)
    db = app.history.path
    _start_worker(app)
    with app._session_lock:
        app._session = Session(1, "toggle")
    app.stop()
    assert rows_in(db) == 1, "quitting mid-dictation left no trace in history"
