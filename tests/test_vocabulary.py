from murmur.vocabulary import Vocabulary


def vocab(tmp_path, hits=2):
    return Vocabulary(tmp_path / "v.db", promote_after_hits=hits)


def test_first_automatic_observation_does_not_promote(tmp_path):
    """A one-off mishearing must not permanently rewrite later transcripts."""
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="auto")
    assert v.hotwords() == []


def test_second_automatic_observation_promotes(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="auto")
    v.observe("halvorsen", "Halvorsen", source="auto")
    assert "Halvorsen" in v.hotwords()


def test_manual_edit_promotes_immediately(tmp_path):
    """A manual correction is explicit intent, so it is trusted at once."""
    v = vocab(tmp_path)
    v.observe("land slide studios", "Landslide Studios", source="manual")
    assert "Landslide Studios" in v.hotwords()


def test_apply_substitutes_the_exact_form_that_was_wrong(tmp_path):
    """Case-SENSITIVE on purpose. Matching case-insensitively meant a
    correction like mark->Marc also rewrote the ordinary word 'mark'."""
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    assert v.apply("i called halvorsen today") == "i called Halvorsen today"
    assert v.apply("i called HALVORSEN today") == "i called HALVORSEN today"


def test_a_correction_does_not_clobber_an_ordinary_word(tmp_path):
    v = vocab(tmp_path)
    v.observe("mark", "Marc", source="manual")
    assert v.apply("please Mark the file") == "please Mark the file"
    assert v.apply("please mark the file") == "please Marc the file"


def test_a_short_term_does_not_rewrite_every_occurrence(tmp_path):
    v = vocab(tmp_path)
    v.observe("us", "US", source="manual")
    assert v.apply("US folks should call us") == "US folks should call US"


def test_apply_ignores_unpromoted_terms(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="auto")
    assert v.apply("i called halvorsen") == "i called halvorsen"


def test_apply_ignores_disabled_terms(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    v.set_enabled("Halvorsen", False)
    assert v.apply("i called halvorsen today") == "i called halvorsen today"


def test_disabled_terms_are_not_sent_as_hotwords(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    v.set_enabled("Halvorsen", False)
    assert v.hotwords() == []


def test_apply_does_not_match_inside_a_longer_word(tmp_path):
    v = vocab(tmp_path)
    v.observe("cat", "Cat", source="manual")
    assert v.apply("concatenate the cat") == "concatenate the Cat"


def test_apply_handles_multiword_terms(tmp_path):
    v = vocab(tmp_path)
    v.observe("north gate", "Northgate", source="manual")
    assert v.apply("the north gate site") == "the Northgate site"


def test_apply_is_safe_with_regex_metacharacters(tmp_path):
    v = vocab(tmp_path)
    v.observe("c++", "C++", source="manual")
    assert "C++" in v.apply("i write c++ daily")


def test_observe_ignores_an_identical_pair(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("Halvorsen", "Halvorsen", source="manual") is False
    assert v.all_terms() == []


def test_a_case_only_correction_is_learned(tmp_path):
    """Capitalising a proper noun is the single most common correction, so
    'halvorsen' -> 'Halvorsen' must count as a real change."""
    v = vocab(tmp_path)
    assert v.observe("halvorsen", "Halvorsen", source="manual") is True
    assert v.apply("i called halvorsen") == "i called Halvorsen"


def test_observe_ignores_blank_input(tmp_path):
    v = vocab(tmp_path)
    assert v.observe("", "Halvorsen", source="manual") is False
    assert v.observe("halvorsen", "   ", source="manual") is False


def test_forget_removes_a_term(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    v.forget("Halvorsen")
    assert v.all_terms() == []


def test_all_terms_exposes_hit_count_for_the_panel(tmp_path):
    """The learner is supervised: every term must be visible and disableable."""
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="auto")
    v.observe("halvorsen", "Halvorsen", source="auto")
    row = v.all_terms()[0]
    assert row["term"] == "Halvorsen"
    assert row["hit_count"] == 2
    assert row["promoted"] == 1
    assert row["enabled"] == 1


def test_terms_persist_across_reopen(tmp_path):
    Vocabulary(tmp_path / "v.db").observe("halvorsen", "Halvorsen", source="manual")
    assert "Halvorsen" in Vocabulary(tmp_path / "v.db").hotwords()


def test_glossary_is_bounded_so_the_prompt_cannot_grow_forever(tmp_path):
    v = vocab(tmp_path)
    for i in range(80):
        v.observe(f"wrong{i}", f"Right{i}", source="manual")
    assert len(v.glossary()) <= 40


def test_longer_terms_are_applied_before_shorter_overlapping_ones(tmp_path):
    v = vocab(tmp_path)
    v.observe("northgate", "Northgate", source="manual")
    v.observe("north gate", "Northgate", source="manual")
    assert v.apply("the north gate site") == "the Northgate site"


# --- regressions from the final adversarial review ---

def test_substitutions_do_not_cascade(tmp_path):
    """Sequential re.sub passes re-substitute into their own output:
    cat->dog plus dog->wolf turned 'cat' into 'wolf'."""
    v = vocab(tmp_path)
    v.observe("cat", "dog", source="manual")
    v.observe("dog", "wolf", source="manual")
    assert v.apply("i saw a cat") == "i saw a dog"
    assert v.apply("i saw a dog") == "i saw a wolf"


def test_a_replacement_containing_its_own_wrong_form_is_stable(tmp_path):
    """'vantage' -> 'Vantage Labs' grew by one word on every pass."""
    v = vocab(tmp_path)
    v.observe("vantage", "Vantage Labs", source="manual")
    once = v.apply("i talked to vantage today")
    assert once == "i talked to Vantage Labs today"
    assert v.apply(once) == once          # idempotent


def test_apply_is_idempotent_for_any_term(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen Law", source="manual")
    v.observe("north gate", "Northgate", source="manual")
    text = "call halvorsen about north gate"
    once = v.apply(text)
    assert v.apply(once) == once


def test_two_different_wrong_forms_do_not_promote_each_other(tmp_path):
    """hit_count keyed on the right form alone counted sightings of 'the',
    so 'hte' went live having been seen exactly once."""
    v = vocab(tmp_path, hits=2)
    assert v.observe("teh", "the", source="auto") is False
    assert v.observe("hte", "the", source="auto") is False
    assert v.apply("hte cat") == "hte cat"
    assert v.apply("teh cat") == "teh cat"


def test_the_same_pair_seen_twice_still_promotes(tmp_path):
    v = vocab(tmp_path, hits=2)
    assert v.observe("teh", "the", source="auto") is False
    assert v.observe("teh", "the", source="auto") is True
    assert v.apply("teh cat") == "the cat"


def test_one_wrong_form_mapped_to_two_right_forms_applies_once(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    v.observe("halvorsen", "Halvorson", source="manual")
    out = v.apply("call halvorsen")
    assert out in ("call Halvorsen", "call Halvorson")
    assert v.apply(out) == out            # never double-substituted


def test_hotwords_do_not_repeat_a_term_with_several_wrong_forms(tmp_path):
    v = vocab(tmp_path)
    v.observe("halvorsen", "Halvorsen", source="manual")
    v.observe("halvorson", "Halvorsen", source="manual")
    assert v.hotwords().count("Halvorsen") == 1
