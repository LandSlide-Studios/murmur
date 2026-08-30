"""Murmur always starts, and always says why when something was wrong.

Tier 3 of the audit remediation. For a background tray app with no console, a
silent failure and a crash look identical from the outside — so every repair
here also has to be visible in the log.
"""
import json

import pytest

from murmur.config import Config
from murmur.history import History
from murmur.vocabulary import Vocabulary


def written(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- hostile settings files --------------------------------------------------

@pytest.mark.parametrize("body,dotted,expected", [
    ('{"hotkeys": "ctrl+alt+q"}', "hotkeys.hold", "ctrl+win"),
    ('{"hotkeys": null}', "hotkeys.hold", "ctrl+win"),
    ('{"stt": 5}', "stt.backend", "local"),
    ('{"polish": ["nonsense"]}', "polish.model", "qwen2.5:7b-instruct"),
    ('{"audio": "not a section"}', "audio.sample_rate", 16000),
])
def test_a_scalar_shadowing_a_section_does_not_delete_it(
        tmp_path, caplog, body, dotted, expected):
    """A scalar replaced the whole branch, and repair only covered keys with a
    declared type. `hotkeys` and `stt` have none, so nothing was restored and
    NOT ONE line was logged — the app started with no hotkey and no way to find
    out why."""
    cfg = Config.load(written(tmp_path, "s.json", body))
    assert cfg.get(dotted) == expected
    assert any("section" in r.message or "shadowed" in r.message
               for r in caplog.records), "repaired silently"


@pytest.mark.parametrize("body,dotted", [
    ('{"audio": {"sample_rate": 0}}', "audio.sample_rate"),
    ('{"audio": {"sample_rate": -16000}}', "audio.sample_rate"),
    ('{"audio": {"speech_rms_threshold": NaN}}', "audio.speech_rms_threshold"),
    ('{"audio": {"speech_rms_threshold": 99}}', "audio.speech_rms_threshold"),
    ('{"polish": {"timeout_s": 1e400}}', "polish.timeout_s"),
    ('{"polish": {"timeout_s": 0}}', "polish.timeout_s"),
    ('{"learning": {"promote_after_hits": 0}}', "learning.promote_after_hits"),
    ('{"audio": {"silence_stop_seconds": Infinity}}', "audio.silence_stop_seconds"),
])
def test_an_impossible_value_is_replaced_by_its_default(tmp_path, body, dotted):
    """Type alone is not enough: zero, negative and NaN are all the right type
    and all break a consumer silently. A NaN speech threshold makes every
    comparison false, so the app never hears anything at all."""
    cfg = Config.load(written(tmp_path, "s.json", body))
    value = cfg.get(dotted)
    assert isinstance(value, (int, float))
    assert value == Config.load(tmp_path / "missing.json").get(dotted)


def test_a_legitimate_override_survives_validation(tmp_path):
    """The counterweight: validation must not flatten real settings."""
    cfg = Config.load(written(tmp_path, "s.json",
                              '{"audio": {"sample_rate": 48000}}'))
    assert cfg.get("audio.sample_rate") == 48000


def test_deeply_nested_settings_do_not_stop_the_app(tmp_path):
    """RecursionError is not a ValueError and was not caught, so a file of
    deeply nested objects stopped the app starting outright."""
    body = '{"a":' * 60_000 + "1" + "}" * 60_000
    cfg = Config.load(written(tmp_path, "deep.json", body))
    assert cfg.get("audio.sample_rate") == 16000


@pytest.mark.parametrize("body", [
    "", "   ", "null", "[]", "[1,2,3]", "{", '{"a": }', "not json at all",
    '﻿{"audio": {"sample_rate": 22050}}',
])
def test_no_settings_file_can_stop_the_app_starting(tmp_path, body):
    cfg = Config.load(written(tmp_path, "s.json", body))
    assert isinstance(cfg.get("audio.sample_rate"), int)


# --- unusable stores ---------------------------------------------------------

@pytest.mark.parametrize("store", [History, Vocabulary])
@pytest.mark.parametrize("content", [
    b"not a database at all" * 100,
    b"SQLite format 3\x00" + b"\x00" * 200,      # truncated header
    b"",
])
def test_an_unusable_store_is_quarantined_rather_than_fatal(
        tmp_path, store, content):
    """A half-written database after a power cut raised at CONSTRUCTION, so
    Murmur did not start and nothing said why."""
    path = tmp_path / "s.db"
    path.write_bytes(content)
    s = store(path)
    assert s is not None
    s.close()


@pytest.mark.parametrize("store", [History, Vocabulary])
def test_the_quarantined_file_is_kept_not_deleted(tmp_path, store):
    """Losing the history is bad; deleting it without asking is worse."""
    path = tmp_path / "s.db"
    path.write_bytes(b"definitely not a database" * 100)
    s = store(path)
    aside = list(tmp_path.glob("s.db.corrupt-*"))
    assert len(aside) == 1, f"nothing kept: {list(tmp_path.iterdir())}"
    assert aside[0].read_bytes().startswith(b"definitely not")
    s.close()


@pytest.mark.parametrize("store", [History, Vocabulary])
def test_a_healthy_store_is_never_quarantined(tmp_path, store):
    """The counterweight — and the one that would cost real data if wrong."""
    path = tmp_path / "s.db"
    first = store(path)
    first.close()
    second = store(path)
    second.close()
    assert list(tmp_path.glob("s.db.corrupt-*")) == []


def test_history_survives_the_quarantine_and_still_records(tmp_path):
    path = tmp_path / "h.db"
    path.write_bytes(b"junk" * 500)
    h = History(path)
    h.add(raw="hello", polished=None, final="hello", mode="hold",
          duration_ms=100, app="x", title="y", status="ok")
    assert len(h.recent()) == 1
    h.close()
