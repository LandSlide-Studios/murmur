"""Settings load/save with deep-merged defaults.

A partial settings.json overrides only the keys it names; every sibling and
every other branch keeps its default. That means a user file written against an
older version never loses access to new settings — which is also why `save()`
writes only the diff against DEFAULTS rather than the merged snapshot. Writing
the snapshot would freeze today's defaults into the user's file forever.

Nothing here may raise on bad input. This is a background tray app with no
console: a settings file the user broke in Notepad must degrade to defaults,
not silently fail to start.
"""

import copy
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    "hotkeys": {"hold": "ctrl+win", "toggle": "ctrl+win+space", "cancel": "esc"},
    "audio": {
        "device": None,
        "silence_stop_seconds": 90,
        "min_session_ms": 350,
        "sample_rate": 16000,
        "speech_rms_threshold": 0.012,
    },
    "stt": {
        "backend": "local",
        "local_model": "large-v3-turbo",
        "device": "auto",
        "language": "en",
    },
    # qwen2.5:7b-instruct measured 2026-08-29 on this machine: p50 583ms.
    # Fastest AND highest quality of four candidates. See LOG.md.
    "polish": {
        "enabled": True,
        "provider": "ollama",
        "model": "qwen2.5:7b-instruct",
        "timeout_s": 4,
        "max_growth_ratio": 1.4,
        "min_shrink_ratio": 0.6,
    },
    "sound": {"enabled": True, "pack": "sotto"},
    "clipboard": {"restore_previous": False},
    "learning": {"enabled": True, "promote_after_hits": 2, "uia_readback": True},
    "ui": {"pill_position": "right-center", "pill_offset_px": 12,
           "idle_indicator": True,
           "comet": True},
    # Tommy's decision 2026-08-29: launch at login is on by default.
    "autostart": True,
}

# Keys whose type matters downstream. A string where a number belongs does not
# fail loudly — it silently disables the feature (a str timeout makes every
# urlopen raise, which polish() catches, turning cleanup off forever).
_TYPES = {
    "audio.silence_stop_seconds": (int, float),
    "audio.min_session_ms": (int, float),
    "audio.sample_rate": (int,),
    "audio.speech_rms_threshold": (int, float),
    "polish.enabled": (bool,),
    "polish.timeout_s": (int, float),
    "polish.max_growth_ratio": (int, float),
    "polish.min_shrink_ratio": (int, float),
    "clipboard.restore_previous": (bool,),
    "learning.enabled": (bool,),
    "learning.promote_after_hits": (int,),
    "learning.uia_readback": (bool,),
    "ui.pill_offset_px": (int,),
    "ui.idle_indicator": (bool,),
    "ui.comet": (bool,),
    "sound.enabled": (bool,),
    "autostart": (bool,),
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _diff(current, defaults):
    """Only what actually differs from DEFAULTS, so new defaults keep flowing
    through on upgrade."""
    out = {}
    for k, v in current.items():
        d = defaults.get(k) if isinstance(defaults, dict) else None
        if isinstance(v, dict) and isinstance(d, dict):
            sub = _diff(v, d)
            if sub:
                out[k] = sub
        elif k not in defaults or v != d:
            out[k] = v
    return out


def _dotted_items(node, prefix=""):
    for k, v in node.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _dotted_items(v, f"{path}.")
        else:
            yield path, v


class Config:
    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = Path(path)

    @classmethod
    def load(cls, path) -> "Config":
        path = Path(path)
        if not path.exists():
            return cls(copy.deepcopy(DEFAULTS), path)
        try:
            # utf-8-sig: Notepad writes a BOM, and plain utf-8 chokes on it.
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            log.error("settings file unreadable (%s); using defaults: %s", path, e)
            return cls(copy.deepcopy(DEFAULTS), path)
        if not isinstance(user, dict):
            log.error("settings file is not a JSON object; using defaults")
            return cls(copy.deepcopy(DEFAULTS), path)
        cfg = cls(_deep_merge(DEFAULTS, user), path)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        """Revert any key whose type would break a consumer. Logs each revert
        so a broken settings file is diagnosable from the log."""
        for dotted, types in _TYPES.items():
            value = self.get(dotted, _MISSING)
            if value is _MISSING:
                default = _default_for(dotted)
                log.warning("settings: %s missing or shadowed; using %r",
                            dotted, default)
                self.set(dotted, default)
                continue
            # bool is a subclass of int; keep them distinct.
            ok = isinstance(value, types) and not (
                bool not in types and isinstance(value, bool)
            )
            if not ok:
                default = _default_for(dotted)
                log.warning("settings: %s is %r (%s), expected %s; using %r",
                            dotted, value, type(value).__name__,
                            "/".join(t.__name__ for t in types), default)
                self.set(dotted, default)

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def save(self) -> None:
        """Atomic, and diff-only.

        Atomic because a crash mid-write leaves a truncated file that the next
        launch cannot parse — the app would brick itself.
        """
        payload = json.dumps(_diff(self.data, DEFAULTS), indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


def _default_for(dotted: str):
    node = DEFAULTS
    for part in dotted.split("."):
        node = node[part]
    return node
