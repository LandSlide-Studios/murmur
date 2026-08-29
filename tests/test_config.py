import json

from murmur.config import DEFAULTS, Config


def test_missing_file_yields_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.json")
    assert cfg.data == DEFAULTS
    assert cfg.get("hotkeys.hold") == "ctrl+win"
    assert cfg.get("hotkeys.toggle") == "ctrl+win+space"


def test_partial_file_deep_merges_over_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"stt": {"local_model": "small"}}))
    cfg = Config.load(p)
    assert cfg.get("stt.local_model") == "small"          # overridden
    assert cfg.get("stt.backend") == "local"              # sibling default survives
    assert cfg.get("audio.silence_stop_seconds") == 90    # other branch untouched


def test_dotted_get_returns_default_for_unknown_key(tmp_path):
    cfg = Config.load(tmp_path / "nope.json")
    assert cfg.get("does.not.exist", "fallback") == "fallback"


def test_get_on_a_scalar_midpath_does_not_raise(tmp_path):
    cfg = Config.load(tmp_path / "nope.json")
    # "autostart" is a bool, so descending into it must return the default
    assert cfg.get("autostart.nested.key", "fallback") == "fallback"


def test_set_then_save_then_reload_roundtrips(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("polish.model", "custom:latest")
    cfg.set("brand.new.branch", 7)
    cfg.save()
    again = Config.load(p)
    assert again.get("polish.model") == "custom:latest"
    assert again.get("brand.new.branch") == 7
    assert again.get("stt.backend") == "local"


def test_loading_does_not_mutate_the_defaults_dict(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"stt": {"backend": "cloud"}}))
    Config.load(p)
    assert DEFAULTS["stt"]["backend"] == "local"


def test_autostart_defaults_on(tmp_path):
    # Tommy's call 2026-08-29: launch at login is on by default.
    assert Config.load(tmp_path / "nope.json").get("autostart") is True


# --- C2: a broken settings file must degrade, never crash the app ---

import pytest


@pytest.mark.parametrize("body", [
    '{"stt": {"backend": "clo',                 # truncated (crash mid-write)
    "",                                          # empty file
    '﻿{"stt": {"backend": "cloud"}}',       # Notepad's UTF-8 BOM
    '{"stt": {"backend": "cloud"},}',            # trailing comma
    "[1, 2, 3]",                                 # valid JSON, wrong shape
    "not json at all",
])
def test_malformed_settings_falls_back_to_defaults(tmp_path, body):
    p = tmp_path / "settings.json"
    p.write_text(body, encoding="utf-8")
    cfg = Config.load(p)                          # must not raise
    assert cfg.get("stt.backend") in ("local", "cloud")
    assert cfg.get("audio.sample_rate") == 16000


def test_bom_file_is_still_parsed_not_just_survived(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('﻿{"stt": {"backend": "cloud"}}', encoding="utf-8")
    assert Config.load(p).get("stt.backend") == "cloud"


def test_unreadable_file_falls_back_to_defaults(tmp_path):
    d = tmp_path / "settings.json"
    d.mkdir()                                     # a directory, not a file
    assert Config.load(d).get("stt.backend") == "local"


# --- I6: validation ---

def test_wrong_type_reverts_to_default(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"polish": {"timeout_s": "four", "enabled": "yes"}}))
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == 4       # a str timeout disables polish forever
    assert cfg.get("polish.enabled") is True      # truthy str could never be turned off


def test_a_list_where_a_dict_belongs_does_not_wipe_the_branch(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"stt": ["local"]}))
    cfg = Config.load(p)
    assert cfg.get("audio.sample_rate") == 16000
    assert cfg.get("polish.timeout_s") == 4


def test_valid_values_survive_validation(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"polish": {"timeout_s": 12}, "autostart": False}))
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == 12
    assert cfg.get("autostart") is False


def test_bool_is_not_accepted_where_a_number_belongs(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"ui": {"pill_offset_px": True}}))
    assert Config.load(p).get("ui.pill_offset_px") == 12


# --- I7: save() must not freeze today's defaults into the user's file ---

def test_save_writes_only_the_diff(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("polish.model", "custom:latest")
    cfg.save()
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == {"polish": {"model": "custom:latest"}}
    assert "stt" not in on_disk                   # untouched branch not frozen


def test_saved_file_still_picks_up_a_new_default(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("autostart", False)
    cfg.save()
    monkeypatch.setitem(DEFAULTS["stt"], "local_model", "large-v4")
    again = Config.load(p)
    assert again.get("stt.local_model") == "large-v4"   # new default flows through
    assert again.get("autostart") is False              # user choice preserved


def test_save_is_atomic_leaving_no_temp_files(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("autostart", False)
    cfg.save()
    assert [f.name for f in tmp_path.iterdir()] == ["settings.json"]


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("polish.model", "custom:latest")
    cfg.set("brand.new.branch", 7)
    cfg.save()
    again = Config.load(p)
    assert again.get("polish.model") == "custom:latest"
    assert again.get("brand.new.branch") == 7
    assert again.get("stt.backend") == "local"
