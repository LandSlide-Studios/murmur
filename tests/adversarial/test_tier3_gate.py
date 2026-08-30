"""Tier 3 adversarial gate: config hardening + store quarantine.

Scope under attack:
  * murmur/config.py  -- load must never raise; range + finiteness checks;
                         wholesale section repair; diff-only atomic save
  * murmur/store.py   -- quarantine an unusable SQLite file instead of raising
  * murmur/history.py, murmur/vocabulary.py -- construction path only

HARD SAFETY RULE: every store and settings file created here lives under pytest
`tmp_path`. `_appdata_is_untouched` proves it: it snapshots %APPDATA%\\Murmur
(the user's live dictation history and vocabulary) around every single test and
fails the test if one byte moved.
"""

import copy
import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path

import pytest

from murmur import config as cfgmod
from murmur import store as storemod
from murmur.config import DEFAULTS, Config
from murmur.history import SCHEMA as HISTORY_SCHEMA
from murmur.history import History
from murmur.store import open_store
from murmur.vocabulary import SCHEMA as VOCAB_SCHEMA
from murmur.vocabulary import Vocabulary

# --------------------------------------------------------------------------
# Safety net: the live user data must never be touched.
# --------------------------------------------------------------------------

_LIVE_DIR = Path(os.environ.get("APPDATA", "")) / "Murmur"


def _snapshot_live():
    if not _LIVE_DIR.is_dir():
        return None
    out = {}
    for p in sorted(_LIVE_DIR.rglob("*")):
        try:
            st = p.stat()
        except OSError:  # pragma: no cover - transient
            out[str(p)] = "unstat-able"
            continue
        out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(autouse=True)
def _appdata_is_untouched():
    """Fail loudly if a test writes anywhere near the real dictation history."""
    before = _snapshot_live()
    yield
    after = _snapshot_live()
    assert after == before, (
        "A test touched the LIVE user data at %s. before=%r after=%r"
        % (_LIVE_DIR, before, after)
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def write_settings(tmp_path: Path, text: str, name="settings.json",
                   encoding="utf-8") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


def settings_json(dotted: str, raw_value_text: str) -> str:
    """Build a settings.json body that sets one dotted key to a raw JSON token."""
    parts = dotted.split(".")
    body = raw_value_text
    for part in reversed(parts):
        body = '{"%s": %s}' % (part, body)
    return body


def make_store(path: Path, schema: str, rows=()):
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    for sql, args in rows:
        conn.execute(sql, args)
    conn.commit()
    conn.close()


def dir_names(p: Path):
    return sorted(x.name for x in p.iterdir())


def corrupt_names(p: Path):
    return sorted(x.name for x in p.iterdir() if ".corrupt-" in x.name)


HISTORY_ROW = (
    "INSERT INTO sessions (ts, duration_ms, mode, status, raw_text, final_text)"
    " VALUES (?,?,?,?,?,?)",
    (1700000000.0, 4200, "hold", "ok", "a year of dictations", "A year of dictations."),
)


# ==========================================================================
# CLAIM 1 -- Config.load() must never raise, whatever the file contains.
# ==========================================================================

BAD_FILES = {
    "empty": "",
    "whitespace": "   \n\t  ",
    "truncated_object": '{"audio": {"sample_rate": 480',
    "trailing_comma": '{"audio": {"sample_rate": 48000,}}',
    "single_quotes": "{'audio': {'sample_rate': 48000}}",
    "bare_word": "not json at all",
    "json_array": '[1, 2, 3]',
    "json_string": '"just a string"',
    "json_number": "42",
    "json_null": "null",
    "json_true": "true",
    "nul_bytes": "\x00\x00\x00\x00",
    "html": "<html><body>oops</body></html>",
    "duplicate_keys": '{"autostart": true, "autostart": false}',
    "huge_int_literal": '{"audio": {"sample_rate": %s}}' % ("9" * 6000),
    "nan_literal": '{"audio": {"speech_rms_threshold": NaN}}',
    "inf_literal": '{"polish": {"timeout_s": Infinity}}',
    "neg_inf_literal": '{"polish": {"timeout_s": -Infinity}}',
    "overflow_float": '{"polish": {"timeout_s": 1e400}}',
    "lone_surrogate": '{"hotkeys": {"hold": "\\ud800"}}',
    "control_chars": '{"hotkeys": {"hold": "a\\u0000b"}}',
    "deep_objects_5000": "".join('{"a":' for _ in range(5000)) + "1" + "}" * 5000,
    "deep_arrays_5000": "[" * 5000 + "]" * 5000,
    "deep_section_5000": '{"hotkeys": ' + "[" * 5000 + "]" * 5000 + "}",
}


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_load_never_raises_on_hostile_file(tmp_path, name):
    """S01 -- every hostile settings file must yield a usable Config."""
    p = write_settings(tmp_path, BAD_FILES[name])
    cfg = Config.load(p)
    assert isinstance(cfg.data, dict)
    # A usable config: every declared section is a dict, every typed key sane.
    for section, default in DEFAULTS.items():
        if isinstance(default, dict):
            assert isinstance(cfg.data.get(section), dict), (
                "section %r is %r after loading %s"
                % (section, cfg.data.get(section), name))
    for dotted, types in cfgmod._TYPES.items():
        v = cfg.get(dotted, cfgmod._MISSING)
        assert v is not cfgmod._MISSING, "%s vanished after %s" % (dotted, name)
        assert isinstance(v, types), "%s is %r after %s" % (dotted, v, name)


@pytest.mark.parametrize("depth", [50, 200, 900, 2000, 2900, 2990, 2994,
                                  2996, 2997, 2998, 2999, 3000, 3200, 20000])
def test_load_never_raises_on_nested_section(tmp_path, depth):
    """S02 -- a section replaced by nesting deep enough to exhaust recursion.

    The repair path logs `%r` of what replaced the section, so a value too deep
    to repr would escape as a RecursionError out of the logging handler.
    """
    p = write_settings(
        tmp_path, '{"hotkeys": ' + "[" * depth + "]" * depth + "}")
    cfg = Config.load(p)
    assert isinstance(cfg.data["hotkeys"], dict)
    assert cfg.get("hotkeys.hold") == DEFAULTS["hotkeys"]["hold"]


@pytest.mark.parametrize("depth", [900, 2900, 2997, 2998, 3000, 20000])
def test_load_never_raises_on_nested_typed_key(tmp_path, depth):
    """S03 -- same, via the typed-key repair log rather than the section log."""
    p = write_settings(
        tmp_path,
        '{"polish": {"timeout_s": ' + "[" * depth + "]" * depth + "}}")
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == DEFAULTS["polish"]["timeout_s"]


def test_load_never_raises_on_missing_file(tmp_path):
    """S04 -- no file at all is the first-run case."""
    cfg = Config.load(tmp_path / "nope" / "settings.json")
    assert cfg.data == DEFAULTS
    assert cfg.data is not DEFAULTS  # must be a copy, not the module global


def test_load_does_not_mutate_the_module_defaults(tmp_path):
    """S05 -- a broken file must not poison DEFAULTS for the next load."""
    frozen = copy.deepcopy(DEFAULTS)
    p = write_settings(tmp_path, '{"audio": "gone", "autostart": "yes"}')
    cfg = Config.load(p)
    cfg.data["audio"]["sample_rate"] = 999999
    cfg.data["hotkeys"]["hold"] = "mangled"
    assert DEFAULTS == frozen


def test_load_never_raises_when_path_is_a_directory(tmp_path):
    """S06 -- settings.json is a directory (a botched restore)."""
    d = tmp_path / "settings.json"
    d.mkdir()
    cfg = Config.load(d)
    assert cfg.get("autostart") is True


def test_load_never_raises_on_utf16(tmp_path):
    """S07 -- Notepad 'Save as Unicode' writes UTF-16."""
    p = tmp_path / "settings.json"
    p.write_bytes('{"autostart": false}'.encode("utf-16"))
    cfg = Config.load(p)
    assert isinstance(cfg.get("autostart"), bool)


def test_load_never_raises_on_invalid_utf8(tmp_path):
    """S08 -- random bytes from a half-written / bit-rotted file."""
    p = tmp_path / "settings.json"
    p.write_bytes(bytes(range(256)) * 40)
    cfg = Config.load(p)
    assert cfg.get("autostart") is True


def test_utf8_bom_file_still_honours_the_user(tmp_path):
    """S09 -- Notepad's BOM must not cost the user their override."""
    p = tmp_path / "settings.json"
    p.write_bytes(b"\xef\xbb\xbf" + b'{"audio": {"sample_rate": 48000}}')
    cfg = Config.load(p)
    assert cfg.get("audio.sample_rate") == 48000


# ==========================================================================
# CLAIM 2 -- values are range- and finiteness-checked, every repair logged.
# ==========================================================================

HOSTILE_VALUES = {
    "string": '"nope"',
    "empty_string": '""',
    "numeric_string": '"4"',
    "list": "[1, 2]",
    "object": '{"value": 4}',
    "null": "null",
    "nan": "NaN",
    "inf": "Infinity",
    "neg_inf": "-Infinity",
    "overflow": "1e400",
    "neg_overflow": "-1e400",
    "zero": "0",
    "negative": "-1",
    "huge_int": "1" + "0" * 30,
    "neg_huge_int": "-1" + "0" * 30,
    "true": "true",
    "false": "false",
    "float_point_five": "0.5",
}


@pytest.mark.parametrize("dotted", sorted(cfgmod._TYPES))
@pytest.mark.parametrize("bad", sorted(HOSTILE_VALUES))
def test_validation_never_raises_and_always_lands_in_range(tmp_path, dotted, bad):
    """S10 -- the full hostile matrix: no raise, right type, inside bounds."""
    p = write_settings(tmp_path, settings_json(dotted, HOSTILE_VALUES[bad]))
    cfg = Config.load(p)
    value = cfg.get(dotted, cfgmod._MISSING)
    types = cfgmod._TYPES[dotted]
    assert value is not cfgmod._MISSING
    assert isinstance(value, types), "%s = %r (%s)" % (dotted, value, bad)
    if bool not in types:
        assert not isinstance(value, bool)
    if isinstance(value, float):
        assert math.isfinite(value)
    bounds = cfgmod._RANGES.get(dotted)
    if bounds:
        low, high = bounds
        assert low <= value <= high, "%s = %r out of %r" % (dotted, value, bounds)


@pytest.mark.parametrize("dotted", sorted(cfgmod._RANGES))
def test_value_exactly_on_each_bound_survives(tmp_path, dotted):
    """S11 -- an inclusive bound must be inclusive at both ends."""
    low, high = cfgmod._RANGES[dotted]
    types = cfgmod._TYPES[dotted]
    for edge in (low, high):
        if int in types and float not in types:
            edge = int(edge)
        p = write_settings(tmp_path, settings_json(dotted, json.dumps(edge)))
        cfg = Config.load(p)
        assert cfg.get(dotted) == edge, (
            "%s: the documented bound %r was rejected, got %r"
            % (dotted, edge, cfg.get(dotted)))


@pytest.mark.parametrize("dotted", sorted(cfgmod._RANGES))
def test_every_range_repair_is_logged(tmp_path, dotted, caplog):
    """S12 -- claim 2 says every repair is written to the log."""
    low, _high = cfgmod._RANGES[dotted]
    below = low - 1 if isinstance(low, int) else low - 1.0
    p = write_settings(tmp_path, settings_json(dotted, json.dumps(below)))
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        cfg = Config.load(p)
    assert cfg.get(dotted) == cfgmod._default_for(dotted)
    assert any(dotted in r.getMessage() for r in caplog.records), (
        "no log line named %s; records=%r"
        % (dotted, [r.getMessage() for r in caplog.records]))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_is_repaired_and_logged(tmp_path, literal, caplog):
    """S13 -- NaN threshold makes every comparison false; must not survive."""
    p = write_settings(
        tmp_path, '{"audio": {"speech_rms_threshold": %s}}' % literal)
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        cfg = Config.load(p)
    v = cfg.get("audio.speech_rms_threshold")
    assert math.isfinite(v) and v == DEFAULTS["audio"]["speech_rms_threshold"]
    assert any("speech_rms_threshold" in r.getMessage() for r in caplog.records)


def test_bool_is_not_accepted_where_a_number_belongs(tmp_path):
    """S14 -- bool is an int subclass; True must not become a timeout."""
    p = write_settings(tmp_path, '{"polish": {"timeout_s": true},'
                                 ' "audio": {"sample_rate": false}}')
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == DEFAULTS["polish"]["timeout_s"]
    assert cfg.get("audio.sample_rate") == DEFAULTS["audio"]["sample_rate"]


def test_int_typed_setting_rejects_a_float(tmp_path):
    """S15 -- int-typed keys reject floats..."""
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 44100.5},'
                                 ' "ui": {"pill_offset_px": 12.5}}')
    cfg = Config.load(p)
    assert cfg.get("audio.sample_rate") == 16000
    assert cfg.get("ui.pill_offset_px") == 12


def test_float_typed_setting_accepts_an_int(tmp_path):
    """S16 -- ...and float-typed keys still accept a plain int."""
    p = write_settings(tmp_path, '{"polish": {"timeout_s": 8,'
                                 ' "max_growth_ratio": 2}}')
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == 8
    assert cfg.get("polish.max_growth_ratio") == 2


def test_valid_overrides_at_every_typed_key_survive(tmp_path):
    """S17 -- a fully-populated, entirely legitimate settings file."""
    user = {
        "hotkeys": {"hold": "ctrl+alt", "toggle": "ctrl+alt+space", "cancel": "esc"},
        "audio": {"device": "eMeet C96", "silence_stop_seconds": 120,
                  "min_session_ms": 200, "sample_rate": 48000,
                  "speech_rms_threshold": 0.006},
        "stt": {"backend": "local", "local_model": "small", "device": "cuda",
                "language": "en"},
        "polish": {"enabled": False, "provider": "ollama", "model": "llama3",
                   "timeout_s": 12.5, "max_growth_ratio": 2.0,
                   "min_shrink_ratio": 0.4},
        "sound": {"enabled": False, "pack": "sotto"},
        "clipboard": {"restore_previous": True},
        "learning": {"enabled": False, "promote_after_hits": 5,
                     "uia_readback": False},
        "ui": {"pill_position": "left-center", "pill_offset_px": 40,
               "idle_indicator": False, "comet": False},
        "autostart": False,
    }
    p = write_settings(tmp_path, json.dumps(user))
    cfg = Config.load(p)
    for dotted, expected in [
        ("hotkeys.hold", "ctrl+alt"), ("audio.device", "eMeet C96"),
        ("audio.silence_stop_seconds", 120), ("audio.min_session_ms", 200),
        ("audio.sample_rate", 48000), ("audio.speech_rms_threshold", 0.006),
        ("stt.local_model", "small"), ("stt.device", "cuda"),
        ("polish.enabled", False), ("polish.model", "llama3"),
        ("polish.timeout_s", 12.5), ("polish.max_growth_ratio", 2.0),
        ("polish.min_shrink_ratio", 0.4), ("sound.enabled", False),
        ("clipboard.restore_previous", True), ("learning.enabled", False),
        ("learning.promote_after_hits", 5), ("learning.uia_readback", False),
        ("ui.pill_position", "left-center"), ("ui.pill_offset_px", 40),
        ("ui.idle_indicator", False), ("ui.comet", False),
        ("autostart", False),
    ]:
        assert cfg.get(dotted) == expected, "%s was overridden" % dotted


def test_no_repair_is_logged_for_a_clean_file(tmp_path, caplog):
    """S18 -- a legitimate file must produce no warnings at all."""
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 48000}}')
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        Config.load(p)
    noisy = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], noisy


def test_a_near_zero_speech_gate_is_still_reachable(tmp_path):
    """S19 -- the lower bound of 1e-6 rejects a literal 0, so check that the
    user can still express 'gate effectively off' with the bound itself."""
    p = write_settings(tmp_path, '{"audio": {"speech_rms_threshold": 1e-6}}')
    assert Config.load(p).get("audio.speech_rms_threshold") == 1e-6


def test_out_of_range_clamps_to_the_declared_bound(tmp_path):
    """S20 -- JUDGMENT CALL. A declared bound is a supported maximum. Asking
    for more than the maximum should give the maximum; instead it gives the
    DEFAULT, which is 40x smaller and cuts the user off mid-sentence."""
    p = write_settings(tmp_path, '{"audio": {"silence_stop_seconds": 7200}}')
    cfg = Config.load(p)
    got = cfg.get("audio.silence_stop_seconds")
    assert got >= 3600, (
        "asked for 7200s of silence tolerance, declared max is 3600, got %r"
        % got)


# ==========================================================================
# CLAIM 3 -- a section replaced by a scalar/null is restored wholesale,
#            with a log line naming what replaced it.
# ==========================================================================

SECTIONS = [s for s, d in DEFAULTS.items() if isinstance(d, dict)]
REPLACEMENTS = {"string": '"ctrl+alt+q"', "int": "7", "float": "1.5",
                "null": "null", "true": "true", "list": '["a", "b"]',
                "empty_list": "[]", "empty_string": '""'}


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize("kind", sorted(REPLACEMENTS))
def test_scalar_replacing_a_section_restores_it_wholesale(tmp_path, section, kind,
                                                          caplog):
    """S21 -- including hotkeys and stt, which have no typed keys at all."""
    p = write_settings(
        tmp_path, "{%s: %s}" % (json.dumps(section), REPLACEMENTS[kind]))
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        cfg = Config.load(p)
    assert cfg.data[section] == DEFAULTS[section]
    msgs = [r.getMessage() for r in caplog.records]
    assert any(section in m for m in msgs), (
        "nothing logged for section %r replaced by %s: %r" % (section, kind, msgs))


@pytest.mark.parametrize("section", SECTIONS)
def test_section_repair_log_names_the_replacement(tmp_path, section, caplog):
    """S22 -- the claim is the log NAMES what replaced it, not just that a
    repair happened."""
    p = write_settings(tmp_path, '{%s: "ctrl+alt+q"}' % json.dumps(section))
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        Config.load(p)
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "ctrl+alt+q" in msgs, msgs
    assert "str" in msgs, msgs


def test_partial_section_is_not_destroyed(tmp_path):
    """S23 -- REGRESSION GUARD. One key set inside a section the user
    otherwise left alone: siblings keep their defaults, the key is kept."""
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 48000}}')
    cfg = Config.load(p)
    assert cfg.get("audio.sample_rate") == 48000
    assert cfg.get("audio.silence_stop_seconds") == 90
    assert cfg.get("audio.min_session_ms") == 120
    assert cfg.get("audio.speech_rms_threshold") == 0.004
    assert cfg.get("audio.device") is None


def test_unknown_key_inside_a_known_section_survives(tmp_path):
    """S24 -- forward compatibility: a harmless extra key is not a reason to
    throw the section away."""
    p = write_settings(
        tmp_path,
        '{"audio": {"sample_rate": 48000, "future_knob": "keep me"},'
        ' "ui": {"theme": "dark"}}')
    cfg = Config.load(p)
    assert cfg.get("audio.future_knob") == "keep me"
    assert cfg.get("ui.theme") == "dark"
    assert cfg.get("audio.sample_rate") == 48000
    assert cfg.get("ui.pill_offset_px") == 12


def test_unknown_top_level_section_survives(tmp_path):
    """S25 -- a whole unknown branch must survive load and save."""
    p = write_settings(tmp_path, '{"experimental": {"a": 1, "b": [1, 2]}}')
    cfg = Config.load(p)
    assert cfg.data["experimental"] == {"a": 1, "b": [1, 2]}
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8"))["experimental"] == \
        {"a": 1, "b": [1, 2]}


def test_one_broken_section_does_not_take_the_others_with_it(tmp_path):
    """S26 -- blast radius: only the broken branch is reset."""
    p = write_settings(
        tmp_path,
        '{"hotkeys": "ctrl+alt+q", "audio": {"sample_rate": 48000},'
        ' "autostart": false}')
    cfg = Config.load(p)
    assert cfg.data["hotkeys"] == DEFAULTS["hotkeys"]
    assert cfg.get("audio.sample_rate") == 48000
    assert cfg.get("autostart") is False


def test_nested_dict_where_a_scalar_belongs_is_repaired(tmp_path):
    """S27 -- a key replaced by a whole sub-object."""
    p = write_settings(
        tmp_path,
        '{"polish": {"timeout_s": {"value": {"deep": 4}}, "model": "llama3"}}')
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == 4
    assert cfg.get("polish.model") == "llama3"


def test_scalar_default_replaced_by_a_section_is_repaired(tmp_path, caplog):
    """S28 -- `autostart` is a bare bool, not a section; a dict must not stick."""
    p = write_settings(tmp_path, '{"autostart": {"enabled": true}}')
    with caplog.at_level(logging.DEBUG, logger="murmur.config"):
        cfg = Config.load(p)
    assert cfg.get("autostart") is True
    assert any("autostart" in r.getMessage() for r in caplog.records)


def test_every_section_replaced_at_once(tmp_path):
    """S29 -- worst case: the whole file is scalars."""
    body = json.dumps({s: "broken" for s in SECTIONS} | {"autostart": "yes"})
    p = write_settings(tmp_path, body)
    cfg = Config.load(p)
    assert cfg.data == DEFAULTS


# ==========================================================================
# Load -> save round trip: diff-only, atomic, defaults not frozen in.
# ==========================================================================


def test_save_writes_only_the_diff(tmp_path):
    """S30 -- writing the merged snapshot would freeze today's defaults."""
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 48000}}')
    cfg = Config.load(p)
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == \
        {"audio": {"sample_rate": 48000}}


def test_save_of_an_untouched_config_writes_an_empty_object(tmp_path):
    """S31 -- nothing changed means nothing pinned."""
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_save_after_repair_drops_only_the_broken_keys(tmp_path):
    """S32 -- self-healing must not eat the good overrides beside them."""
    p = write_settings(
        tmp_path,
        '{"hotkeys": 5, "audio": {"sample_rate": 48000},'
        ' "polish": {"timeout_s": "slow", "model": "llama3"}}')
    cfg = Config.load(p)
    cfg.save()
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == {"audio": {"sample_rate": 48000},
                       "polish": {"model": "llama3"}}


def test_round_trip_is_stable(tmp_path):
    """S33 -- load -> save -> load must be a fixed point."""
    p = write_settings(
        tmp_path,
        '{"audio": {"sample_rate": 48000}, "ui": {"comet": false},'
        ' "autostart": false}')
    first = Config.load(p)
    first.save()
    second = Config.load(p)
    assert second.data == first.data
    second.save()
    assert Config.load(p).data == first.data


def test_save_leaves_no_temp_file_behind(tmp_path):
    """S34 -- atomic write must not litter the settings directory."""
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("audio.sample_rate", 48000)
    cfg.save()
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "settings.json"]
    assert leftovers == [], leftovers


def test_save_does_not_clobber_the_file_when_serialisation_fails(tmp_path):
    """S35 -- an unserialisable value must not truncate a good settings file."""
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 48000}}')
    cfg = Config.load(p)
    cfg.set("audio.device", {1, 2, 3})  # a set is not JSON
    with pytest.raises(Exception):
        cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == \
        {"audio": {"sample_rate": 48000}}
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "settings.json"]
    assert leftovers == [], leftovers


# ==========================================================================
# CLAIM 4 -- an unusable SQLite file is quarantined, never a healthy one.
# ==========================================================================

CORRUPTIONS = {
    "garbage": b"this is not a database" * 500,
    "truncated_header": b"SQLite format 3\x00" + b"\x00" * 20,
    "html_error_page": b"<html>404</html>" * 300,
    "random_bytes": bytes(range(256)) * 100,
}


@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
def test_corrupt_store_is_quarantined_not_raised(tmp_path, kind, caplog):
    """S36 -- the headline claim."""
    p = tmp_path / "history.db"
    p.write_bytes(CORRUPTIONS[kind])
    with caplog.at_level(logging.DEBUG, logger="murmur.store"):
        conn = open_store(p, HISTORY_SCHEMA)
    try:
        conn.execute(*HISTORY_ROW)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()
    aside = corrupt_names(tmp_path)
    assert len(aside) == 1, dir_names(tmp_path)
    assert (tmp_path / aside[0]).read_bytes() == CORRUPTIONS[kind], \
        "the casualty was not preserved byte for byte"
    assert any("corrupt-" in r.getMessage() for r in caplog.records)


def test_quarantine_name_is_timestamped(tmp_path):
    """S37 -- the name must carry a stamp, not a single fixed suffix."""
    p = tmp_path / "history.db"
    p.write_bytes(b"garbage" * 500)
    before = int(time.time())
    open_store(p, HISTORY_SCHEMA).close()
    after = int(time.time())
    name = corrupt_names(tmp_path)[0]
    stamp = int(name.rsplit("corrupt-", 1)[1].split("-")[0])
    assert before <= stamp <= after


def test_second_corrupt_start_in_the_same_second_keeps_both(tmp_path):
    """S38 -- a stamped name that collides would destroy the first casualty."""
    p = tmp_path / "history.db"
    for _attempt in range(5):
        for f in tmp_path.iterdir():
            f.unlink()
        t0 = int(time.time())
        p.write_bytes(b"FIRST CASUALTY" * 500)
        open_store(p, HISTORY_SCHEMA).close()
        p.write_bytes(b"SECOND CASUALTY" * 500)
        open_store(p, HISTORY_SCHEMA).close()
        if int(time.time()) == t0:
            break
    else:  # pragma: no cover
        pytest.skip("could not get two quarantines inside one second")
    aside = corrupt_names(tmp_path)
    assert len(aside) == 2, "one casualty was overwritten: %r" % dir_names(tmp_path)
    blobs = [(tmp_path / n).read_bytes() for n in aside]
    assert sorted(b[:5] for b in blobs) == [b"FIRST", b"SECON"]


def test_monotonic_clock_collision_keeps_both(tmp_path, monkeypatch):
    """S39 -- same, with the clock pinned, so it cannot pass by luck."""
    monkeypatch.setattr(storemod.time, "time", lambda: 1_700_000_000.0)
    p = tmp_path / "history.db"
    p.write_bytes(b"FIRST CASUALTY" * 500)
    open_store(p, HISTORY_SCHEMA).close()
    p.write_bytes(b"SECOND CASUALTY" * 500)
    open_store(p, HISTORY_SCHEMA).close()
    p.write_bytes(b"THIRD CASUALTY" * 500)
    open_store(p, HISTORY_SCHEMA).close()
    aside = corrupt_names(tmp_path)
    assert len(aside) == 3, dir_names(tmp_path)
    blobs = {(tmp_path / n).read_bytes()[:14] for n in aside}
    assert blobs == {b"FIRST CASUALTY", b"SECOND CASUALT", b"THIRD CASUALTY"}


def test_empty_file_is_not_quarantined(tmp_path):
    """S40 -- a zero-byte file is a valid empty SQLite database."""
    p = tmp_path / "history.db"
    p.touch()
    open_store(p, HISTORY_SCHEMA).close()
    assert corrupt_names(tmp_path) == []


def test_healthy_store_is_never_quarantined(tmp_path):
    """S41 -- the load-bearing negative: reopening a good store 5x."""
    p = tmp_path / "history.db"
    make_store(p, HISTORY_SCHEMA, [HISTORY_ROW])
    for _ in range(5):
        conn = open_store(p, HISTORY_SCHEMA)
        assert conn.execute("SELECT raw_text FROM sessions").fetchone()[0] == \
            "a year of dictations"
        conn.close()
    assert corrupt_names(tmp_path) == []


def test_healthy_store_with_a_newer_schema_is_not_quarantined(tmp_path):
    """S42 -- a file written by a LATER version (extra columns, extra table)."""
    p = tmp_path / "history.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(HISTORY_SCHEMA)
    conn.execute("ALTER TABLE sessions ADD COLUMN confidence REAL")
    conn.execute("CREATE TABLE future_thing (id INTEGER PRIMARY KEY)")
    conn.execute(*HISTORY_ROW)
    conn.commit()
    conn.close()
    conn = open_store(p, HISTORY_SCHEMA)
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    conn.close()
    assert corrupt_names(tmp_path) == []


def test_healthy_store_with_an_older_schema_is_not_quarantined(tmp_path):
    """S43 -- a file written by an EARLIER version of this same app.

    `CREATE TABLE IF NOT EXISTS` silently no-ops against the old table, so the
    old shape survives, and the very next statement in the schema script --
    `CREATE INDEX ... ON sessions(ts DESC)` -- fails on a column that is not
    there. Nothing about this file is corrupt; PRAGMA integrity_check is 'ok'.
    """
    p = tmp_path / "history.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " timestamp REAL, mode TEXT, text TEXT)")
    for i in range(500):
        conn.execute("INSERT INTO sessions (timestamp, mode, text)"
                     " VALUES (?,?,?)", (1700000000.0 + i, "hold", "dictation %d" % i))
    conn.commit()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

    try:
        conn = open_store(p, HISTORY_SCHEMA)
        conn.close()
    except sqlite3.Error:
        pass
    assert corrupt_names(tmp_path) == [], (
        "a healthy, integrity-clean database with 500 rows was quarantined: %r"
        % dir_names(tmp_path))


def test_healthy_store_locked_by_another_connection(tmp_path):
    """S44 -- a second launch of the tray app while the first holds a write.

    Two things are asserted: the healthy file must not be moved aside, and
    construction must not raise -- the whole reason store.py exists.
    """
    p = tmp_path / "history.db"
    make_store(p, HISTORY_SCHEMA, [HISTORY_ROW])
    locker = sqlite3.connect(str(p), isolation_level=None, timeout=1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        raised = None
        try:
            conn = open_store(p, HISTORY_SCHEMA)
            conn.close()
        except BaseException as e:
            raised = e
        assert corrupt_names(tmp_path) == [], (
            "a healthy locked database was moved aside: %r" % dir_names(tmp_path))
        assert raised is None, (
            "open_store raised at construction on a HEALTHY database: %s: %s"
            % (type(raised).__name__, raised))
    finally:
        locker.execute("ROLLBACK")
        locker.close()


def test_wal_sidecars_do_not_survive_a_quarantine(tmp_path):
    """S45 -- a quarantine that leaves a hot -wal behind hands the 'fresh'
    store the old database's pages."""
    p = tmp_path / "history.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HISTORY_SCHEMA)
    for i in range(200):
        conn.execute("INSERT INTO sessions (ts, duration_ms, mode, status,"
                     " raw_text) VALUES (?,?,?,?,?)",
                     (1700000000.0 + i, 10, "hold", "ok", "secret %d" % i))
    conn.commit()
    import shutil
    live = tmp_path / "live"
    live.mkdir()
    for f in tmp_path.iterdir():
        if f.is_file():
            shutil.copy2(f, live / f.name)
    conn.close()

    target = live / "history.db"
    target.write_bytes(b"not a database" * 900)  # main file destroyed, wal hot
    assert (live / "history.db-wal").exists()

    try:
        c = open_store(target, HISTORY_SCHEMA)
    except sqlite3.Error as e:
        pytest.fail("open_store raised on a corrupt main file + hot wal: %s" % e)
    try:
        c.execute("INSERT INTO sessions (ts, duration_ms, mode, status)"
                  " VALUES (1,1,'hold','ok')")
        c.commit()
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()
    if corrupt_names(live):
        leftovers = [n for n in dir_names(live)
                     if n.startswith("history.db-") and ".corrupt-" not in n]
        assert leftovers == [], (
            "quarantine left the sidecars %r beside a fresh database" % leftovers)


def test_quarantine_works_with_unicode_and_spaces_in_the_path(tmp_path):
    """S46 -- non-ASCII profile names are ordinary on Windows."""
    d = tmp_path / "Mårmür Ünicöde 日本語"
    d.mkdir()
    p = d / "histörique dictée.db"
    p.write_bytes(b"garbage" * 500)
    conn = open_store(p, HISTORY_SCHEMA)
    conn.close()
    assert len(corrupt_names(d)) == 1, dir_names(d)


def test_quarantine_with_a_very_long_path(tmp_path):
    """S47 -- the stamped suffix lengthens the name; MAX_PATH is 260."""
    d = tmp_path
    while len(str(d)) < 200:
        d = d / ("x" * 40)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("filesystem refused the long directory")
    p = d / ("y" * 40 + ".db")
    try:
        p.write_bytes(b"garbage" * 500)
    except OSError:
        pytest.skip("filesystem refused the long file name")
    conn = open_store(p, HISTORY_SCHEMA)
    conn.close()
    assert len(corrupt_names(d)) == 1, dir_names(d)


def test_directory_in_place_of_the_store_does_not_raise(tmp_path):
    """S48 -- a botched sync can leave a directory where the db belongs."""
    p = tmp_path / "history.db"
    p.mkdir()
    conn = open_store(p, HISTORY_SCHEMA)
    conn.close()
    assert corrupt_names(tmp_path) == [] or len(corrupt_names(tmp_path)) == 1


# --- the real constructors -------------------------------------------------


@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
def test_history_constructor_survives_a_corrupt_file(tmp_path, kind):
    """S49 -- claim 4 through History(), not just open_store()."""
    p = tmp_path / "history.db"
    p.write_bytes(CORRUPTIONS[kind])
    h = History(p)
    rowid = h.add("raw", "polished", "final", "hold", 1200, "notepad.exe",
                  "Untitled")
    assert rowid == 1
    assert h.recent()[0]["final_text"] == "final"
    assert len(corrupt_names(tmp_path)) == 1


@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
def test_vocabulary_constructor_survives_a_corrupt_file(tmp_path, kind):
    """S50 -- and through Vocabulary()."""
    p = tmp_path / "vocab.db"
    p.write_bytes(CORRUPTIONS[kind])
    v = Vocabulary(p)
    v.observe("halvorsen", "Halvorsen", source="manual")
    assert len(corrupt_names(tmp_path)) == 1


def test_history_constructor_preserves_a_healthy_file(tmp_path):
    """S51 -- the negative for History: existing rows must still be there."""
    p = tmp_path / "history.db"
    make_store(p, HISTORY_SCHEMA, [HISTORY_ROW])
    h = History(p)
    assert h.recent()[0]["raw_text"] == "a year of dictations"
    assert corrupt_names(tmp_path) == []


def test_vocabulary_constructor_preserves_a_healthy_file(tmp_path):
    """S52 -- the negative for Vocabulary."""
    p = tmp_path / "vocab.db"
    make_store(p, VOCAB_SCHEMA, [(
        "INSERT INTO terms (wrong_form, term, hit_count, promoted)"
        " VALUES (?,?,?,?)", ("halvorsen", "Halvorsen", 3, 1))])
    Vocabulary(p)
    assert corrupt_names(tmp_path) == []
    conn = sqlite3.connect(str(p))
    assert conn.execute("SELECT count(*) FROM terms").fetchone()[0] == 1
    conn.close()


def test_old_schema_store_that_keeps_the_indexed_column_is_kept(tmp_path):
    """S54 -- the contrast case for S43: an equally 'old' table that happens
    to still carry `ts` opens without a murmur. The quarantine decision turns
    on one column name in a CREATE INDEX, not on the file being unusable."""
    p = tmp_path / "history.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, ts REAL,"
                 " text TEXT)")
    conn.execute("INSERT INTO sessions (ts, text) VALUES (1.0, 'kept')")
    conn.commit()
    conn.close()
    conn = open_store(p, HISTORY_SCHEMA)
    assert conn.execute("SELECT text FROM sessions").fetchone()[0] == "kept"
    conn.close()
    assert corrupt_names(tmp_path) == []


def test_save_over_a_read_only_settings_file_leaves_no_wreckage(tmp_path):
    """S55 -- a settings.json restored from a backup carries the read-only
    attribute; a failed save must not truncate it or litter .tmp files."""
    import stat
    p = write_settings(tmp_path, '{"audio": {"sample_rate": 48000}}')
    cfg = Config.load(p)
    cfg.set("autostart", False)
    os.chmod(p, stat.S_IREAD)
    try:
        try:
            cfg.save()
        except OSError:
            pass
        assert json.loads(p.read_text(encoding="utf-8")) == \
            {"audio": {"sample_rate": 48000}} or \
            json.loads(p.read_text(encoding="utf-8")) == \
            {"audio": {"sample_rate": 48000}, "autostart": False}
        leftovers = [x.name for x in tmp_path.iterdir()
                     if x.name != "settings.json"]
        assert leftovers == [], leftovers
    finally:
        os.chmod(p, stat.S_IWRITE)


def test_open_store_creates_missing_parent_directories(tmp_path):
    """S53 -- first run on a clean machine."""
    p = tmp_path / "a" / "b" / "c" / "history.db"
    conn = open_store(p, HISTORY_SCHEMA)
    conn.close()
    assert p.exists()
