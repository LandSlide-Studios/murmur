"""Adversarial scenarios for murmur.inject and murmur.config.

Written air-gapped from the engineering log and the existing tests: every
assertion here comes from the two modules' own docstrings (their stated
contract) and from first principles about what a dictation app must never do.

A failure in this file is a CLAIM, not a verdict.

SAFETY
------
Nothing in this file may reach a real Win32 API or the real clipboard. A
synthetic Ctrl+V lands in whatever window the developer has focused, and a real
pyperclip.copy() destroys whatever they had on the clipboard. Two guards:

  * `_hard_safety` (autouse) swaps `murmur.inject.user32` and both pyperclip
    entry points for objects that RAISE if called. A test that forgets to
    install its fakes fails loudly instead of firing keystrokes.
  * Every config test writes only under pytest's `tmp_path`; `_settings_path`
    refuses any other location.

No test opens a window, calls FindWindow, or touches the user's settings file.
"""

import ctypes
import json
import os
import stat
import sys
import threading

import pytest

from murmur import config as configmod
from murmur import inject as injectmod
from murmur.config import DEFAULTS, Config
from murmur.inject import Injector

VK_SHIFT = injectmod.VK_SHIFT
VK_CONTROL = injectmod.VK_CONTROL
VK_MENU = injectmod.VK_MENU
VK_LWIN = injectmod.VK_LWIN
VK_RWIN = injectmod.VK_RWIN
VK_V = injectmod.VK_V
KEYUP = injectmod.KEYEVENTF_KEYUP

# GetAsyncKeyState is declared restype=c_short, so a held key comes back as a
# NEGATIVE python int. Model that faithfully -- a fake returning 0x8000 would
# hide a whole class of sign bug.
HELD_SHORT = -32768


# --------------------------------------------------------------------------
# safety net
# --------------------------------------------------------------------------

class _Poisoned:
    """Stands in for a real OS handle. Any attribute access explodes."""

    def __init__(self, what):
        self._what = what

    def __getattr__(self, name):
        raise AssertionError(
            f"UNSAFE: test reached the real {self._what}.{name}(). "
            "Install the fakes before exercising the injector."
        )


def _poisoned_call(*a, **k):
    raise AssertionError("UNSAFE: test reached the real system clipboard.")


@pytest.fixture(autouse=True)
def _hard_safety(monkeypatch):
    import pyperclip

    monkeypatch.setattr(injectmod, "user32", _Poisoned("user32"))
    monkeypatch.setattr(pyperclip, "copy", _poisoned_call)
    monkeypatch.setattr(pyperclip, "paste", _poisoned_call)
    yield


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeUser32:
    """Records keystrokes instead of sending them.

    `held` is the PHYSICAL key state the user's hands produce. keybd_event(KEYUP)
    clears it only when `release_on_keyup` is True -- the real API cannot lift a
    key the hardware still reports down, which is the whole reason
    _release_modifiers exists.
    """

    def __init__(self, held=(), release_on_keyup=True, clear_after_polls=None,
                 raise_on_keys=()):
        self.held = set(held)
        self.release_on_keyup = release_on_keyup
        self.clear_after_polls = clear_after_polls
        self.raise_on_keys = set(raise_on_keys)
        self.polls = 0
        self.keys = []                 # [("down"|"up", vk), ...]
        self.v_sent_while_held = []    # physical modifiers down at each Ctrl+V
        self.clipboard_at_paste = []   # what that paste would actually have pulled
        self.clip = None               # set by the fixture, for the above

    def GetAsyncKeyState(self, vk):
        self.polls += 1
        if self.clear_after_polls is not None and self.polls > self.clear_after_polls:
            self.held.clear()
        return HELD_SHORT if vk in self.held else 0

    def keybd_event(self, vk, scan, flags, extra):
        if vk in self.raise_on_keys:
            raise OSError(f"keybd_event failed for vk={vk:#x}")
        up = bool(flags & KEYUP)
        self.keys.append(("up" if up else "down", vk))
        if not up and vk == VK_V:
            self.v_sent_while_held.append(frozenset(self.held))
            if self.clip is not None:
                self.clipboard_at_paste.append(self.clip.text)
        if up and self.release_on_keyup:
            self.held.discard(vk)

    # -- helpers ----------------------------------------------------------
    @property
    def pasted(self):
        return ("down", VK_V) in self.keys

    def keyups(self):
        return [vk for kind, vk in self.keys if kind == "up"]


class ClipboardError(RuntimeError):
    """Stands in for pyperclip.PyperclipWindowsException (OpenClipboard failed:
    another process is holding the clipboard open)."""


class FakeClipboard:
    """Faithful to pyperclip's Windows backend.

    * paste() returns "" when the clipboard holds a non-text format (an image,
      a file list) or nothing at all -- paste_windows() returns "" whenever
      GetClipboardData(CF_UNICODETEXT) hands back NULL.
    * copy_windows() sizes its buffer with wcslen(), so a NUL byte truncates.
      Off by default so it cannot silently distort unrelated tests.
    """

    def __init__(self, initial="", nul_truncates=False):
        self.text = initial
        self.nul_truncates = nul_truncates
        self.sets = []          # every value handed to copy(), pre-truncation
        self.gets = 0
        self.fail_set_on = ()   # 1-based call indexes that raise
        self.fail_get_on = ()
        self.on_set = None

    def copy(self, text):
        self.sets.append(text)
        n = len(self.sets)
        if n in self.fail_set_on:
            raise ClipboardError("OpenClipboard failed (Access is denied)")
        self.text = text.split("\x00")[0] if self.nul_truncates else text
        if self.on_set:
            self.on_set(n)

    def paste(self):
        self.gets += 1
        if self.gets in self.fail_get_on:
            raise ClipboardError("OpenClipboard failed (Access is denied)")
        return self.text


class FakeClock:
    """Deterministic stand-in for `time`. Every reading advances, so the release
    loop terminates without a real 500ms wall-clock wait."""

    def __init__(self, tick=0.0005):
        self.now = 1000.0
        self.tick = tick
        self.slept = []
        self.on_sleep = None

    def perf_counter(self):
        self.now += self.tick
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep:
            hook, self.on_sleep = self.on_sleep, None   # fires once
            hook(seconds)


@pytest.fixture
def clock(monkeypatch, _hard_safety):
    c = FakeClock()
    monkeypatch.setattr(injectmod, "time", c)
    return c


@pytest.fixture
def rig(monkeypatch, _hard_safety, clock):
    """Factory: rig(clip=..., injector_kw=..., **user32kw) -> (inj, u32, clip)."""
    import pyperclip

    made = {}

    def build(clip=None, injector_kw=None, **u32kw):
        clip = clip if clip is not None else FakeClipboard()
        u32 = FakeUser32(**u32kw)
        u32.clip = clip
        monkeypatch.setattr(injectmod, "user32", u32)
        monkeypatch.setattr(pyperclip, "copy", clip.copy)
        monkeypatch.setattr(pyperclip, "paste", clip.paste)
        kw = {"release_timeout_s": 0.5, "clipboard_settle_s": 0.06,
              "restore_delay_s": 0.3}
        kw.update(injector_kw or {})
        inj = Injector(**kw)
        made.update(injector=inj, u32=u32, clip=clip, clock=clock)
        return inj, u32, clip

    build.made = made
    return build


# ==========================================================================
# INJECTOR
# ==========================================================================

def test_empty_text_never_touches_the_clipboard_or_the_keyboard(rig):
    inj, u32, clip = rig()
    assert inj.inject("") is False
    assert clip.sets == []
    assert u32.keys == []


def test_none_text_never_touches_the_clipboard_or_the_keyboard(rig):
    inj, u32, clip = rig()
    assert inj.inject(None) is False
    assert clip.sets == []
    assert u32.keys == []


def test_whitespace_only_text_is_still_injected(rig):
    """A dictation that transcribed to a single space is not "nothing"; the user
    pressed the chord and expects something to happen."""
    inj, u32, clip = rig()
    assert inj.inject("   ") is True
    assert clip.text == "   "
    assert u32.pasted


def test_clipboard_set_failure_aborts_before_any_paste(rig):
    """If the transcript never reached the clipboard, Ctrl+V would paste
    whatever was there before -- the user's previous copy, into their document."""
    clip = FakeClipboard(initial="PREVIOUS SECRET")
    clip.fail_set_on = (1,)
    inj, u32, clip = rig(clip=clip)
    with pytest.raises(ClipboardError):
        inj.inject("the dictation")
    assert not u32.pasted, "sent Ctrl+V after the clipboard write failed"
    assert clip.text == "PREVIOUS SECRET"


def test_clipboard_read_failure_does_not_abort_the_paste(rig):
    """Losing the old clipboard is acceptable; losing the dictation is not."""
    clip = FakeClipboard(initial="old")
    clip.fail_get_on = (1,)
    inj, u32, clip = rig(clip=clip, injector_kw={"restore_previous": True})
    assert inj.inject("hello there") is True
    assert u32.pasted
    assert clip.text == "hello there"


def test_clipboard_failure_only_on_the_restore_is_non_destructive(rig):
    """Set #1 (the transcript) succeeds, set #2 (the restore) fails."""
    clip = FakeClipboard(initial="old")
    clip.fail_set_on = (2,)
    inj, u32, clip = rig(clip=clip, injector_kw={"restore_previous": True})
    assert inj.inject("hello there") is True
    assert u32.pasted
    assert clip.text == "hello there", "transcript was destroyed by a failed restore"


def test_previous_clipboard_is_restored_after_a_successful_paste(rig):
    clip = FakeClipboard(initial="user's earlier copy")
    inj, u32, clip = rig(clip=clip, injector_kw={"restore_previous": True})
    assert inj.inject("hello there") is True
    assert clip.sets == ["hello there", "user's earlier copy"]
    assert clip.text == "user's earlier copy"


def test_restore_disabled_never_reads_the_clipboard(rig):
    inj, u32, clip = rig(clip=FakeClipboard(initial="untouched"))
    assert inj.inject("hello") is True
    assert clip.gets == 0


def test_non_text_previous_clipboard_is_not_written_back_as_empty(rig):
    """The user had an image (or a file list) on the clipboard. pyperclip reads
    it as "". Restoring that "" cannot bring the image back, and it costs the
    user the one thing that WAS recoverable: the transcript."""
    clip = FakeClipboard(initial="")        # image on the clipboard
    inj, u32, clip = rig(clip=clip, injector_kw={"restore_previous": True})
    inj.inject("the dictation")
    assert clip.text == "the dictation", (
        "restored an empty string over the transcript; Ctrl+V now yields nothing"
    )


def test_enormous_text_is_not_truncated(rig):
    inj, u32, clip = rig()
    big = "word " * 400_000          # ~2M chars
    assert inj.inject(big) is True
    assert clip.text == big
    assert len(clip.text) == len(big)


@pytest.mark.parametrize("text, label", [
    ("line one\r\nline two", "crlf"),
    ("line one\nline two", "lf"),
    ("line one\rline two", "lone-cr"),
    ("col one\tcol two", "tab"),
    ("a\r\n\r\n\r\nb", "blank-lines"),
])
def test_line_endings_and_tabs_reach_the_clipboard_verbatim(rig, text, label):
    inj, u32, clip = rig()
    assert inj.inject(text) is True
    assert clip.text == text


def test_astral_plane_characters_survive(rig):
    inj, u32, clip = rig()
    text = "ship it \U0001F680 \U0001D11E \U0001F1FA\U0001F1F8 café"
    assert inj.inject(text) is True
    assert clip.text == text
    assert clip.sets[0] == text


def test_nul_byte_silently_truncates_the_dictation(rig):
    """pyperclip's copy_windows() sizes its buffer with wcslen(), which stops at
    the first NUL. Everything after it is dropped, with no error anywhere. The
    injector hands any str straight through."""
    try:
        wcslen = ctypes.cdll.msvcrt.wcslen
        wcslen.argtypes = [ctypes.c_wchar_p]
        wcslen.restype = ctypes.c_size_t
    except (OSError, AttributeError):        # pragma: no cover
        pytest.skip("msvcrt unavailable")
    text = "the part you keep\x00the part you lose"
    assert wcslen(text) == 17, "premise check: wcslen stops at the NUL"

    inj, u32, clip = rig(clip=FakeClipboard(nul_truncates=True))
    inj.inject(text)
    assert clip.text == text, (
        "clipboard holds only the text before the NUL; the rest of the "
        "dictation is gone and nothing reported it"
    )


def test_modifiers_never_released_gives_up_without_pasting(rig):
    """Ctrl+V while Ctrl+Win are physically down is Ctrl+Win+V: it opens
    Clipboard History instead of pasting."""
    inj, u32, clip = rig(held=(VK_CONTROL, VK_LWIN), release_on_keyup=False)
    assert inj.inject("hello there") is False
    assert not u32.pasted, "sent Ctrl+V with Ctrl+Win still physically down"
    assert u32.v_sent_while_held == []


def test_modifiers_never_released_still_leaves_the_text_on_the_clipboard(rig):
    """The refusal to paste is only safe because the words survive."""
    inj, u32, clip = rig(held=(VK_CONTROL, VK_LWIN), release_on_keyup=False)
    inj.inject("hello there")
    assert clip.text == "hello there"


def test_refusing_to_paste_does_not_restore_over_the_transcript(rig):
    clip = FakeClipboard(initial="earlier copy")
    inj, u32, clip = rig(clip=clip, held=(VK_LWIN,), release_on_keyup=False,
                         injector_kw={"restore_previous": True})
    assert inj.inject("hello there") is False
    assert clip.text == "hello there", (
        "restored the old clipboard over a transcript the user still has to "
        "paste by hand"
    )


def test_modifiers_released_midway_through_the_wait_proceeds_to_paste(rig):
    inj, u32, clip = rig(held=(VK_CONTROL, VK_LWIN), release_on_keyup=False,
                         clear_after_polls=12)
    assert inj.inject("hello there") is True
    assert u32.pasted
    assert u32.v_sent_while_held == [frozenset()]


def test_release_sends_keyup_for_every_held_modifier_with_win_last(rig):
    """Win must go up after the others or the Start menu's "was Win pressed
    alone" tracking fires and the Start menu opens over the paste target."""
    inj, u32, clip = rig(held=(VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))
    inj.inject("hello")
    ups = u32.keyups()
    for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN):
        assert vk in ups, f"never released {vk:#x}"
    assert ups.index(VK_LWIN) > ups.index(VK_CONTROL)
    assert ups.index(VK_RWIN) > ups.index(VK_MENU)


def test_no_keyup_is_sent_for_a_modifier_that_is_not_held(rig):
    """Synthesising a keyup for a key the user never pressed can cancel a real
    chord in the app underneath."""
    inj, u32, clip = rig(held=())
    inj.inject("hello")
    assert VK_SHIFT not in u32.keyups()
    assert VK_MENU not in u32.keyups()


def test_held_modifier_reported_as_a_negative_short_is_detected(rig):
    """restype=c_short means a held key reads as -32768, not 0x8000."""
    inj, u32, clip = rig(held=(VK_CONTROL,), release_on_keyup=False)
    assert inj._release_modifiers() is False
    assert ("up", VK_CONTROL) in u32.keys


def test_recently_pressed_low_bit_is_not_mistaken_for_held(rig):
    """Bit 0 means "pressed since the last call", not "down now". Treating it as
    held would make the injector refuse to paste after every dictation."""
    inj, u32, clip = rig()
    u32.GetAsyncKeyState = lambda vk: 1
    assert inj._release_modifiers() is True


def test_zero_release_timeout_refuses_to_paste_with_no_modifiers_held(rig):
    """The key state is only ever sampled INSIDE the timed loop, so a zero (or
    already-exhausted) budget returns False before checking even once -- with
    the user's hands off the keyboard entirely."""
    inj, u32, clip = rig(held=(), injector_kw={"release_timeout_s": 0.0})
    assert inj._release_modifiers() is True, (
        "gave up without ever sampling the key state; no modifier was held"
    )


def test_paste_failure_leaves_the_text_on_the_clipboard(rig):
    inj, u32, clip = rig(raise_on_keys=(VK_V,))
    with pytest.raises(OSError):
        inj.inject("hello there")
    assert clip.text == "hello there"


def test_paste_failure_does_not_restore_over_the_transcript(rig):
    clip = FakeClipboard(initial="earlier copy")
    inj, u32, clip = rig(clip=clip, raise_on_keys=(VK_V,),
                         injector_kw={"restore_previous": True})
    with pytest.raises(OSError):
        inj.inject("hello there")
    assert clip.text == "hello there"


def test_copy_leaves_the_text_even_when_it_returns_false(rig):
    inj, u32, clip = rig(held=(VK_CONTROL, VK_LWIN), release_on_keyup=False)
    assert inj.copy("hello there") is False
    assert clip.text == "hello there"
    assert not u32.pasted


def test_copy_of_empty_text_leaves_the_clipboard_untouched(rig):
    clip = FakeClipboard(initial="user's own copy")
    inj, u32, clip = rig(clip=clip)
    assert inj.copy("") is False
    assert clip.text == "user's own copy"
    assert clip.sets == []


def test_paste_rechecks_the_modifiers_it_was_told_were_clear(rig):
    """copy() and paste() are split so an animation can run between them. During
    that window the user can start their next dictation, putting Ctrl+Win back
    down -- and paste() sends Ctrl+V with no check of its own."""
    inj, u32, clip = rig()
    assert inj.copy("hello there") is True
    u32.held = {VK_CONTROL, VK_LWIN}          # user re-presses the chord
    inj.paste()
    assert all(not held for held in u32.v_sent_while_held), (
        "sent Ctrl+V with Ctrl+Win physically down: that is Ctrl+Win+V, which "
        "opens Clipboard History instead of pasting"
    )


def test_reentrant_injection_during_the_settle_window_pastes_the_right_text(rig):
    """A second dictation lands between this one's clipboard write and its
    Ctrl+V. Nothing serialises the two, so the first paste emits the second
    transcript."""
    inj, u32, clip = rig()
    clock = rig.made["clock"]

    def second_dictation(_seconds):
        inj.inject("SECOND transcript")

    clock.on_sleep = second_dictation
    inj.inject("FIRST transcript")

    # Two dictations, two Ctrl+V presses. The inner one fires first (the outer
    # is still in its settle sleep) and correctly pastes SECOND; the outer then
    # pastes whatever the clipboard now holds -- SECOND again.
    assert sorted(u32.clipboard_at_paste) == ["FIRST transcript", "SECOND transcript"], (
        f"two dictations produced two Ctrl+V presses but pasted "
        f"{u32.clipboard_at_paste!r}: the first transcript never reached the screen"
    )


def test_overlapping_injections_from_two_threads_do_not_cross(rig):
    """The same race across threads. Injection A's settle sleep is used as the
    interleaving point, so the test is deterministic rather than timing-lucky."""
    inj, u32, clip = rig()
    clock = rig.made["clock"]
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def worker_b():
        started.wait(timeout=5)
        try:
            inj.inject("BBBB")
        except Exception as e:            # pragma: no cover
            errors.append(repr(e))
        finally:
            finished.set()

    t = threading.Thread(target=worker_b, daemon=True)
    t.start()

    def handoff(_seconds):
        started.set()
        finished.wait(timeout=5)

    clock.on_sleep = handoff
    inj.inject("AAAA")
    t.join(timeout=5)

    assert not errors, errors
    assert sorted(u32.clipboard_at_paste) == ["AAAA", "BBBB"], (
        f"two threads produced two Ctrl+V presses but pasted "
        f"{u32.clipboard_at_paste!r}: one transcript never reached the screen"
    )


# ==========================================================================
# CONFIG
# ==========================================================================

def _settings_path(tmp_path, content, name="settings.json", encoding="utf-8"):
    """Write a settings file. Refuses to write anywhere but pytest's tmp_path."""
    p = tmp_path / name
    low = str(p).lower()
    assert "pytest" in low or "\\temp\\" in low or "/tmp" in low, \
        f"UNSAFE: refusing to write settings outside tmp_path ({p})"
    if isinstance(content, bytes):
        p.write_bytes(content)
    elif isinstance(content, str):
        p.write_text(content, encoding=encoding)
    else:
        p.write_text(json.dumps(content), encoding=encoding)
    return p


def _assert_healthy(cfg):
    """The app can start: every branch a consumer reads is still reachable."""
    assert cfg.get("hotkeys.hold") == DEFAULTS["hotkeys"]["hold"]
    assert cfg.get("stt.backend") == DEFAULTS["stt"]["backend"]
    assert cfg.get("audio.sample_rate") == DEFAULTS["audio"]["sample_rate"]
    assert cfg.get("polish.model") == DEFAULTS["polish"]["model"]


@pytest.mark.parametrize("blob, label", [
    ('{"audio": {"sample_rate": 16000,}}', "trailing-comma"),
    ('{"audio": {"sample_rate": 16000}', "unclosed-brace"),
    ("// my settings\n{}", "comment"),
    ("{'autostart': false}", "single-quotes"),
    ("not json at all", "prose"),
    ("", "empty-file"),
    ("   \n\t  \n", "whitespace-only"),
    ("\x00\x00\x00\x00", "nul-bytes"),
    ("null", "json-null"),
    ("[1, 2, 3]", "json-array"),
    ('"just a string"', "json-string"),
    ("42", "json-number"),
    ("true", "json-bool"),
])
def test_a_broken_settings_file_starts_on_defaults(tmp_path, blob, label):
    p = _settings_path(tmp_path, blob)
    cfg = Config.load(p)
    _assert_healthy(cfg)
    assert cfg.path == p


def test_utf8_bom_from_notepad_is_accepted(tmp_path):
    p = _settings_path(tmp_path, b'\xef\xbb\xbf{"autostart": false}')
    cfg = Config.load(p)
    assert cfg.get("autostart") is False
    assert cfg.get("hotkeys.hold") == "ctrl+win"


def test_utf16_from_notepads_unicode_option_starts_on_defaults(tmp_path):
    p = _settings_path(tmp_path, '{"autostart": false}'.encode("utf-16-le"))
    cfg = Config.load(p)
    _assert_healthy(cfg)


def test_cp1252_bytes_start_on_defaults(tmp_path):
    p = _settings_path(tmp_path, b'{"stt": {"language": "espa\xf1ol"}}')
    cfg = Config.load(p)
    _assert_healthy(cfg)


def test_a_directory_where_the_settings_file_belongs_starts_on_defaults(tmp_path):
    d = tmp_path / "settings.json"
    d.mkdir()
    cfg = Config.load(d)
    _assert_healthy(cfg)


def test_a_missing_file_starts_on_defaults_and_keeps_the_path(tmp_path):
    p = tmp_path / "nested" / "deeper" / "settings.json"
    cfg = Config.load(p)
    _assert_healthy(cfg)
    assert cfg.path == p


@pytest.mark.parametrize("dotted, bad", [
    ("audio.sample_rate", "16000"),
    ("audio.sample_rate", True),
    ("polish.timeout_s", "4"),
    ("polish.enabled", "false"),
    ("autostart", "true"),
    ("ui.pill_offset_px", None),
    ("learning.promote_after_hits", 2.5),
    ("sound.enabled", 1),
])
def test_a_wrong_typed_value_is_reverted_to_the_default(tmp_path, dotted, bad):
    cfg_in = Config(json.loads(json.dumps(DEFAULTS)), tmp_path / "x")
    cfg_in.set(dotted, bad)
    p = _settings_path(tmp_path, cfg_in.data)
    cfg = Config.load(p)
    assert cfg.get(dotted) == configmod._default_for(dotted)


@pytest.mark.parametrize("dotted, bad, why", [
    ("audio.sample_rate", 0, "a zero sample rate cannot open a stream"),
    ("audio.sample_rate", -16000, "a negative sample rate cannot open a stream"),
    ("audio.silence_stop_seconds", 0, "auto-stop fires before a word is said"),
    ("polish.timeout_s", 0, "every request times out; polish is off forever"),
    ("learning.promote_after_hits", 0,
     "promotes on zero observations: an unsupervised learner"),
    ("audio.speech_rms_threshold", float("nan"),
     "every comparison against NaN is False; the app never hears speech"),
])
def test_an_impossible_value_is_rejected(tmp_path, dotted, bad, why):
    """_TYPES checks the type and stops. The module's own rationale for that
    check -- "a string where a number belongs silently disables the feature" --
    applies verbatim to 0, to negatives, and to NaN."""
    cfg_in = Config(json.loads(json.dumps(DEFAULTS)), tmp_path / "x")
    cfg_in.set(dotted, bad)
    p = _settings_path(tmp_path, json.dumps(cfg_in.data))   # NaN -> literal NaN
    cfg = Config.load(p)
    assert cfg.get(dotted) == configmod._default_for(dotted), why


def test_infinity_from_an_overflowing_float_literal_is_rejected(tmp_path):
    """`1e400` is valid JSON to python's parser and lands as inf."""
    p = _settings_path(tmp_path, '{"polish": {"timeout_s": 1e400}}')
    cfg = Config.load(p)
    assert cfg.get("polish.timeout_s") == DEFAULTS["polish"]["timeout_s"]


def test_unknown_keys_do_not_break_anything(tmp_path):
    p = _settings_path(tmp_path, {"colour": "puce", "audio": {"gain": 3},
                                  "nested": {"a": {"b": {"c": [1, 2]}}}})
    cfg = Config.load(p)
    _assert_healthy(cfg)
    assert cfg.get("colour") == "puce"


@pytest.mark.parametrize("branch, junk", [
    ("hotkeys", "ctrl+alt+q"),
    ("hotkeys", None),
    ("stt", 5),
    ("polish", []),
    ("audio", "16000"),
    ("ui", True),
])
def test_a_scalar_shadowing_a_whole_branch_is_repaired(tmp_path, branch, junk):
    """A hand-edit that replaces a section with a scalar wipes every key under
    it. _validate repairs only the keys listed in _TYPES, so the untyped ones --
    hotkeys.hold, stt.backend, polish.model -- stay gone. The app starts, with
    no hotkey."""
    p = _settings_path(tmp_path, {branch: junk})
    cfg = Config.load(p)
    _assert_healthy(cfg)


def test_a_deeply_nested_settings_file_does_not_stop_the_app(tmp_path):
    """json.loads raises RecursionError, which is not in load()'s except clause.
    "Nothing here may raise on bad input" is the module's own contract."""
    depth = 60_000
    p = _settings_path(tmp_path, '{"a":' * depth + "1" + "}" * depth)
    try:
        cfg = Config.load(p)
    except RecursionError:
        pytest.fail("Config.load raised RecursionError: the app cannot start")
    _assert_healthy(cfg)


def test_moderately_nested_junk_round_trips_through_save(tmp_path):
    node = {"leaf": 1}
    for _ in range(60):
        node = {"n": node}
    p = _settings_path(tmp_path, {"junk": node, "autostart": False})
    cfg = Config.load(p)
    cfg.save()
    again = Config.load(p)
    assert again.get("autostart") is False
    assert again.data["junk"] == node


def test_save_creates_a_missing_parent_directory(tmp_path):
    p = tmp_path / "does" / "not" / "exist" / "settings.json"
    cfg = Config.load(p)
    cfg.set("autostart", False)
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {"autostart": False}


def test_save_writes_only_the_diff(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("ui.pill_offset_px", 40)
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {"ui": {"pill_offset_px": 40}}


def test_a_reverted_bad_value_is_scrubbed_from_the_file_on_the_next_save(tmp_path):
    p = _settings_path(tmp_path, {"audio": {"sample_rate": "16000"}})
    cfg = Config.load(p)
    cfg.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_an_override_survives_a_save_and_reload(tmp_path):
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    cfg.set("stt.language", "it")
    cfg.set("audio.sample_rate", 48000)
    cfg.save()
    again = Config.load(p)
    assert again.get("stt.language") == "it"
    assert again.get("audio.sample_rate") == 48000
    assert again.get("hotkeys.hold") == "ctrl+win"


def test_loading_never_mutates_the_module_defaults(tmp_path):
    snapshot = json.dumps(DEFAULTS, sort_keys=True)
    p = _settings_path(tmp_path, {"audio": {"sample_rate": 48000}, "stt": 5})
    cfg = Config.load(p)
    cfg.set("hotkeys.hold", "ctrl+shift+z")
    cfg.data["audio"]["sample_rate"] = 1
    assert json.dumps(DEFAULTS, sort_keys=True) == snapshot


def test_a_read_only_settings_file_is_never_left_damaged(tmp_path):
    original = '{"autostart": false}'
    p = _settings_path(tmp_path, original)
    os.chmod(p, stat.S_IREAD)
    try:
        cfg = Config.load(p)
        cfg.set("ui.pill_offset_px", 40)
        with pytest.raises(OSError):
            cfg.save()
        assert p.read_text(encoding="utf-8") == original
        assert list(tmp_path.glob("*.tmp")) == [], "left a temp file behind"
    finally:
        os.chmod(p, stat.S_IWRITE)


def test_a_failed_rename_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    original = '{"autostart": false}'
    p = _settings_path(tmp_path, original)
    cfg = Config.load(p)
    cfg.set("ui.pill_offset_px", 40)

    def boom(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(configmod.os, "replace", boom)
    with pytest.raises(OSError):
        cfg.save()
    assert p.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_an_unserializable_value_never_reaches_the_file(tmp_path):
    original = '{"autostart": false}'
    p = _settings_path(tmp_path, original)
    cfg = Config.load(p)
    cfg.set("ui.pill_position", {"a set"})
    with pytest.raises(TypeError):
        cfg.save()
    assert p.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def _race_saves(p, rounds=25, workers=8):
    """Eight Config objects on one path, all saving. Nothing reads the file
    during the race, so any failure is the writers colliding with each other."""
    cfgs = []
    for i in range(workers):
        c = Config.load(p)
        c.set("ui.pill_offset_px", 100 + i)
        cfgs.append(c)
    errors = []

    def run(c):
        for _ in range(rounds):
            try:
                c.save()
            except Exception as e:
                errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(c,)) for c in cfgs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


def test_concurrent_saves_never_corrupt_the_file(tmp_path):
    """Whatever else goes wrong, the file on disk is always one whole write."""
    p = tmp_path / "settings.json"
    _race_saves(p)
    final = json.loads(p.read_text(encoding="utf-8"))
    assert final["ui"]["pill_offset_px"] in range(100, 108)
    assert list(tmp_path.glob("*.tmp")) == [], "left temp files behind"


def test_concurrent_saves_do_not_fail(tmp_path):
    """os.replace onto a destination another thread is mid-replace of is denied
    on Windows. The settings change is lost and the exception reaches a tray app
    with no console to show it in."""
    p = tmp_path / "settings.json"
    errors = _race_saves(p)
    assert not errors, f"{len(errors)}/200 saves failed, e.g. {errors[:2]}"


def test_saving_while_the_file_is_open_elsewhere_does_not_fail(tmp_path):
    """A reader without FILE_SHARE_DELETE blocks MoveFileEx on Windows: an open
    editor, a sync client or an AV scanner touching settings.json is enough to
    make every save fail."""
    p = _settings_path(tmp_path, '{"ui": {"pill_offset_px": 7}}')
    cfg = Config.load(p)
    cfg.set("ui.pill_offset_px", 8)
    with open(p, "r", encoding="utf-8") as _reader:
        try:
            cfg.save()
        except OSError as e:
            pytest.fail(f"save() failed while the file was open for reading: {e!r}")
    assert json.loads(p.read_text(encoding="utf-8"))["ui"]["pill_offset_px"] == 8
    assert list(tmp_path.glob("*.tmp")) == []


def test_saving_while_another_thread_edits_the_config_does_not_crash(tmp_path):
    """save() walks self.data with no lock. Adding or removing a key under a
    known branch while a save is in flight is a plain dict-changed-size crash.

    (Updating an existing key does not resize the dict and will not trigger it;
    this needs a key being added or removed.)"""
    p = tmp_path / "settings.json"
    cfg = Config.load(p)
    for i in range(3000):                       # widen _diff's iteration window
        cfg.data["audio"][f"pad{i}"] = i
    errors = []
    stop = threading.Event()
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def mutator():
        i = 0
        while not stop.is_set():
            i += 1
            cfg.data["audio"][f"n{i % 800}"] = i
            cfg.data["audio"].pop(f"n{(i + 400) % 800}", None)

    def saver():
        for _ in range(150):
            try:
                cfg.save()
            except Exception as e:
                errors.append(repr(e))

    try:
        m = threading.Thread(target=mutator, daemon=True)
        m.start()
        saver()
        stop.set()
        m.join(timeout=10)
    finally:
        sys.setswitchinterval(old_interval)
    assert not errors, f"{len(errors)}/150 saves crashed, e.g. {errors[:2]}"
