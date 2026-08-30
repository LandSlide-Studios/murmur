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
import time
import threading
import math
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    "hotkeys": {"hold": "ctrl+win", "toggle": "ctrl+win+space", "cancel": "esc"},
    "audio": {
        "device": None,
        "silence_stop_seconds": 90,
        # Low on purpose. This only has to reject a graze; a Windows shortcut
        # sharing the chord is now discarded by the FSM instead, and a
        # genuinely empty capture is caught by the silence guard. At 350ms
        # every quick tap-and-talk was thrown away with no feedback at all.
        "min_session_ms": 120,
        "sample_rate": 16000,
        # Measured on his eMeet C96 webcam: ambient floor p99 0.00086, and
        # speech somewhere near 0.007-0.010 (solved back from a 42.4s dictation
        # that fell under the old guard while he was talking through it). 0.012
        # sat ABOVE his speaking voice, so his speech counted as silence in all
        # three places this is used. 0.004 is ~4.6x his noise floor and well
        # under his quietest talking.
        "speech_rms_threshold": 0.004,
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
    "history": {
        # Newest rows to keep, or 0 for unlimited. Defaults to unlimited
        # because deleting someone's dictation history is not a default any
        # tool gets to choose on their behalf -- but the mechanism now exists
        # and the size is logged at startup, so unbounded growth is visible
        # rather than merely undocumented.
        "keep_rows": 0,
    },
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
# Type alone is not enough. Zero, negative and NaN are all the right type and
# all break a consumer silently -- a NaN speech threshold makes every comparison
# false, so the app simply never hears anything. JSON also accepts the bare
# literals NaN and Infinity, and 1e400 parses to inf, so all three arrive as
# floats and sail past an isinstance check.
#
# (low, high) inclusive, or None for no bound on that side.
_RANGES = {
    "audio.silence_stop_seconds": (1.0, 3600.0),
    "audio.min_session_ms": (0.0, 60_000.0),
    "audio.sample_rate": (8_000, 192_000),
    "audio.speech_rms_threshold": (1e-6, 1.0),
    "polish.timeout_s": (0.1, 600.0),
    "polish.max_growth_ratio": (1.0, 100.0),
    "polish.min_shrink_ratio": (0.0, 1.0),
    "history.keep_rows": (0, 10_000_000),
    "learning.promote_after_hits": (1, 1000),
    "ui.pill_offset_px": (0, 10_000),
}

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
    "history.keep_rows": (int,),
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
        self._save_lock = threading.RLock()

    @classmethod
    def load(cls, path) -> "Config":
        path = Path(path)
        if not path.exists():
            return cls(copy.deepcopy(DEFAULTS), path)
        try:
            # utf-8-sig: Notepad writes a BOM, and plain utf-8 chokes on it.
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError,
                RecursionError, ValueError) as e:
            # RecursionError is not a ValueError and was not caught, so a file
            # of deeply nested objects stopped the app starting outright. The
            # contract at the top of this module is absolute: nothing here may
            # raise on bad input.
            log.error("settings file unreadable (%s); using defaults: %s", path, e)
            return cls(copy.deepcopy(DEFAULTS), path)
        if not isinstance(user, dict):
            log.error("settings file is not a JSON object; using defaults")
            return cls(copy.deepcopy(DEFAULTS), path)
        cfg = cls(_deep_merge(DEFAULTS, user), path)
        cfg._validate()
        return cfg

    def _repair_branches(self) -> None:
        """Restore any section a scalar has replaced.

        `{"hotkeys": "ctrl+alt+q"}` replaced the whole branch with a string, and
        repair only covered keys with a declared type. `hotkeys` and `stt` have
        none at all, so nothing was restored and NOT ONE LINE was logged: the
        app started with no hotkey and no way to find out why. For a tray app
        with no console that is indistinguishable from failing to start.
        """
        for section, default in DEFAULTS.items():
            if not isinstance(default, dict):
                continue
            current = self.data.get(section, _MISSING)
            if current is _MISSING or isinstance(current, dict):
                continue
            log.warning("settings: section %r was replaced by %r (%s); "
                        "restoring the whole section",
                        section, current, type(current).__name__)
            self.data[section] = copy.deepcopy(default)

    def _validate(self) -> None:
        """Revert any key whose type or value would break a consumer. Logs each
        revert so a broken settings file is diagnosable from the log."""
        self._repair_branches()
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
                continue

            if isinstance(value, float) and not math.isfinite(value):
                default = _default_for(dotted)
                log.warning("settings: %s is %r, which is not a finite number;"
                            " using %r", dotted, value, default)
                self.set(dotted, default)
                continue

            bounds = _RANGES.get(dotted)
            if bounds is not None:
                low, high = bounds
                if (low is not None and value < low) or \
                        (high is not None and value > high):
                    # Clamp to the bound, do not revert to the default.
                    # Declaring a range asserts that its ends are SUPPORTED, so
                    # someone asking for 7200s of silence tolerance meant "stop
                    # bothering me" -- and reverting them to 90s moved the
                    # setting 80x in the direction that cuts a session off
                    # mid-sentence, which is the opposite of what they asked.
                    clamped = min(max(value, low), high)
                    # Round to int only where the DECLARED type is int-only.
                    # Keying off the default's type truncated a fractional
                    # bound: polish.timeout_s defaults to 4, so clamping 0 up to
                    # its 0.1 floor then cast it straight back to 0 — landing
                    # outside the range the clamp had just enforced.
                    if types == (int,):
                        clamped = int(round(clamped))
                        clamped = min(max(clamped, int(low + 0.999)), int(high))
                    log.warning("settings: %s is %r, outside %r..%r; using %r",
                                dotted, value, low, high, clamped)
                    self.set(dotted, clamped)

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
        # Under the lock: `_diff` walks `self.data`, and another thread adding
        # or removing a key mid-walk raises "dictionary changed size during
        # iteration". Measured at 97% failure with a wide enough dict.
        with self._save_lock:
            payload = json.dumps(_diff(self.data, DEFAULTS), indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            self._replace_with_retry(tmp)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _replace_with_retry(self, tmp: str, attempts: int = 6) -> None:
        """Rename over the settings file, retrying briefly.

        On Windows a reader without FILE_SHARE_DELETE blocks the rename, so
        OneDrive, an antivirus scan or an open editor was enough to make every
        save fail outright -- and 12-18% failed under ordinary concurrency.
        The write-temp-then-replace pattern is right; it just needs to wait.
        """
        delay = 0.02
        for attempt in range(attempts):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


def _default_for(dotted: str):
    node = DEFAULTS
    for part in dotted.split("."):
        node = node[part]
    return node
