"""Tier-2 adversarial gate.

Attacks five claims made about a change to pill.py / inject.py / app.py.
Nothing here touches the real clipboard, the real keyboard, %APPDATA% or an
audio device: user32 is faked, pyperclip is faked, the recorder is a stub and
the stores live in tmp_path.
"""

import logging
import sys
import threading
import time
import types

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from murmur import inject as inject_mod
from murmur.inject import (KEYEVENTF_KEYUP, MODIFIERS, VK_CONTROL, VK_MENU,
                           VK_SHIFT, VK_V, Injector)
from murmur.ui.pill import (BUTTON_H, BUTTON_MIN_H, IDLE_SIZE, TOGGLE_SIZE,
                            Pill)


# ----------------------------------------------------------------------------
# safety scaffolding
# ----------------------------------------------------------------------------

class FakeUser32:
    """Records every Win32 call. Nothing reaches the real keyboard.

    `sticky` models a key the hardware still reports down: a synthetic KEYUP
    does not clear it. `transient_clear` models the other possibility -- the
    injected KEYUP clears the async state for exactly one poll before the
    hardware re-asserts it.
    """

    def __init__(self, held=(), sticky=True, transient_clear=False):
        self.held = set(held)
        self.sticky = sticky
        self.transient_clear = transient_clear
        self._suppress_once = set()
        self.calls = []
        self.release_after = None      # (vk, perf_counter deadline)

    def GetAsyncKeyState(self, vk):
        self.calls.append(("get", vk))
        if self.release_after is not None:
            rvk, when = self.release_after
            if vk == rvk and time.perf_counter() >= when:
                self.held.discard(rvk)
        if vk in self._suppress_once:
            self._suppress_once.discard(vk)
            return 0
        return -32768 if vk in self.held else 0

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(("key", vk, flags))
        if flags & KEYEVENTF_KEYUP:
            if not self.sticky:
                self.held.discard(vk)
            elif self.transient_clear:
                self._suppress_once.add(vk)

    def keyups(self):
        return [c[1] for c in self.calls
                if c[0] == "key" and c[2] & KEYEVENTF_KEYUP]

    def pasted(self):
        return any(c[0] == "key" and c[1] == VK_V and c[2] == 0
                   for c in self.calls)


class FakePyperclip(types.ModuleType):
    def __init__(self):
        super().__init__("pyperclip")
        self.value = "PREVIOUS-CLIPBOARD"
        self.copies = []

    def copy(self, text):
        self.copies.append(text)
        self.value = text

    def paste(self):
        return self.value


@pytest.fixture(autouse=True)
def _airgap(monkeypatch):
    """Hard safety net: no real keystrokes, no real clipboard, ever."""
    fake_clip = FakePyperclip()
    monkeypatch.setitem(sys.modules, "pyperclip", fake_clip)
    fake = FakeUser32()
    monkeypatch.setattr(inject_mod, "user32", fake)
    yield types.SimpleNamespace(user32=fake, clipboard=fake_clip)


@pytest.fixture
def u32(_airgap):
    return _airgap.user32


@pytest.fixture
def clip(_airgap):
    return _airgap.clipboard


def injector(u32, **kw):
    kw.setdefault("clipboard_settle_s", 0.0)
    kw.setdefault("restore_delay_s", 0.0)
    return Injector(**kw)


# ----------------------------------------------------------------------------
# app scaffolding
# ----------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self, *a, **kw):
        self.pcm = [0.0] * 16000
        self.begun = 0
        self.ended = 0
        self.muted = []

    def begin(self):
        self.begun += 1

    def end(self):
        self.ended += 1
        return self.pcm

    def open(self):
        pass

    def close(self):
        pass

    def mute_for(self, ms):
        self.muted.append(ms)


class FakeSounds:
    def __init__(self, *a, **kw):
        self.played = []

    def duration_ms(self, name):
        return 0

    def play(self, name):
        self.played.append(name)


class FakeStt:
    def __init__(self, text="hello world"):
        self.text = text

    def transcribe(self, pcm, hotwords=None):
        return self.text


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    from murmur import app as app_mod
    from murmur.config import Config

    built = []

    def _build(**overrides):
        monkeypatch.setenv("APPDATA", str(tmp_path / f"appdata{len(built)}"))
        monkeypatch.setattr(app_mod, "Recorder", FakeRecorder)
        monkeypatch.setattr(app_mod, "foreground_window",
                            lambda: ("editor.exe", "Untitled"))
        monkeypatch.setattr(app_mod, "peak_rms", lambda pcm, sr: 1.0)
        cfg = Config.load(tmp_path / "settings-does-not-exist.json")
        cfg.set("learning.uia_readback", False)
        cfg.set("sound.enabled", False)
        for k, v in overrides.items():
            cfg.set(k, v)
        states = []
        a = app_mod.MurmurApp(cfg,
                              on_state=lambda s, **kw: states.append((s, kw)))
        a.sounds = FakeSounds()
        a._stt = FakeStt()
        a.polisher.polish = lambda raw, glossary=None: raw
        a.states = states
        a.cursor_points = [(100, 100)]
        a._cursor_point = lambda: (a.cursor_points.pop(0)
                                   if a.cursor_points else None)
        built.append(a)
        return a

    yield _build
    for a in built:
        try:
            a.history.close()
            a.vocab.close()
        except Exception:
            pass


def run_one(app, cursor=(100, 100)):
    """Start, stop and hand back the queued job without running the worker."""
    app.cursor_points = [cursor] if cursor is not None else []
    app._start("toggle")
    app._stop_and_transcribe()
    return app._jobs.get_nowait()


def states_of(app):
    return [s for s, _ in app.states]


# ----------------------------------------------------------------------------
# CLAIM 1 -- the tick/cross are hittable exactly where they are painted
# ----------------------------------------------------------------------------

def _settled(pill, state, mode, frames=400):
    pill.set_state(state, mode)
    for _ in range(frames):
        pill._tick()
    return pill


def _white_glyph_pixels(pill):
    """Count opaque near-white pixels inside the two button bands.

    The tick and cross are the only near-white strokes in the capsule (the
    waveform is the blue accent, the body near-black). An independent read of
    what the painter actually drew.
    """
    img = QImage(pill.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    pill.render(img)
    r = pill._capsule_rect()
    bands = [(int(r.top()), int(r.top() + BUTTON_H)),
             (int(r.bottom() - BUTTON_H), int(r.bottom()))]
    n = 0
    for y0, y1 in bands:
        for y in range(max(0, y0), min(img.height(), y1)):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if (c.alpha() > 200 and c.red() > 140 and c.green() > 140
                        and c.blue() > 140 and abs(c.red() - c.blue()) < 30):
                    n += 1
    return n


@pytest.mark.parametrize("state,mode", [
    ("recording", "toggle"),
    ("recording", "hold"),
    ("armed", "toggle"),
    ("transcribing", "toggle"),
    ("polishing", "toggle"),
    ("done", "toggle"),
    ("copied", "toggle"),
    ("cancelled", "toggle"),
    ("error", "toggle"),
    ("launching", "toggle"),
])
def test_c1_painted_glyphs_match_the_hit_rects(qapp, state, mode):
    """Independent pixel read: glyphs on screen <=> _button_rects() non-None."""
    pill = Pill()
    _settled(pill, state, mode)
    hittable = pill._button_rects()[0] is not None
    glyphs = _white_glyph_pixels(pill)
    assert (glyphs > 0) == hittable, (
        f"{state}/{mode}: glyph pixels={glyphs} hittable={hittable}")
    pill.set_state("off")


def test_c1_drawn_implies_clickable_every_frame(qapp):
    """A control that is painted must be reachable by the mouse."""
    pill = Pill()
    bad = []
    script = [("armed", "toggle"), ("recording", "toggle"),
              ("transcribing", "toggle"), ("polishing", "toggle"),
              ("done", "toggle"), ("armed", "toggle")]
    for state, mode in script:
        pill.set_state(state, mode)
        for f in range(120):
            pill._tick()
            if pill._controls_visible() and pill._click_through:
                bad.append((state, f, round(pill.width_s.value, 1),
                            round(pill.height_s.value, 1)))
    assert not bad, f"painted but click-through (unclickable) at: {bad[:6]}"
    pill.set_state("off")


def test_c1_clickable_implies_something_to_click_every_frame(qapp):
    """The opposite half: a window that swallows clicks must do something with
    them, or it steals clicks from the app underneath."""
    pill = Pill()
    bad = []
    script = [("armed", "toggle"), ("recording", "toggle"),
              ("transcribing", "toggle"), ("done", "toggle"),
              ("armed", "toggle"), ("recording", "hold"),
              ("transcribing", "hold"), ("cancelled", "hold"),
              ("armed", "hold")]
    for state, mode in script:
        pill.set_state(state, mode)
        for f in range(120):
            pill._tick()
            if not pill._click_through and pill._button_rects()[0] is None:
                bad.append((state, mode, f, round(pill.height_s.value, 1)))
    assert not bad, f"clickable with no live control at: {bad[:6]}"
    pill.set_state("off")


def test_c1_restart_from_hidden_does_not_leave_a_dead_clickable_window(qapp):
    """A toggle session, the tray switching the pill off, then a new session.

    set_state() decides click-through BEFORE it snaps the springs back to the
    idle size, so it decides against the previous session's geometry.
    """
    pill = Pill()
    _settled(pill, "recording", "toggle")
    assert pill._controls_visible()
    pill.set_state("off")
    for _ in range(400):
        pill._tick()
    assert not pill.isVisible()
    assert pill.height_s.value > BUTTON_MIN_H   # still the old geometry

    pill.set_state("recording", "toggle")       # new session, no tick yet
    assert pill._click_through or pill._controls_visible(), (
        "window is clickable but nothing is drawn: click_through="
        f"{pill._click_through} controls_visible={pill._controls_visible()} "
        f"h={pill.height_s.value:.1f} w={pill.width_s.value:.1f}")
    pill.set_state("off")


def test_c1_dead_clickable_window_recurs_when_the_idle_indicator_is_off(qapp):
    """`ui.idle_indicator = false` makes the previous case the normal case.

    _settle_to_armed() parks the pill in state "off", and _target_size() has no
    branch for "off" -- it falls through to REC_SIZE (30x138), which is above
    BUTTON_MIN_H. So the pill hides at a controls-sized geometry after EVERY
    dictation, and the next hands-free session opens clickable-but-blank.
    """
    pill = Pill(show_when_idle=False)
    _settled(pill, "recording", "toggle")
    pill.set_state("done", "toggle")
    for _ in range(600):                    # terminal hold, settle, fade out
        pill._tick()
    assert pill.state == "off" and not pill.isVisible()
    assert pill.height_s.value >= BUTTON_MIN_H, (
        f"parked at h={pill.height_s.value:.1f}")

    pill.set_state("recording", "toggle")
    assert pill._click_through or pill._controls_visible(), (
        "every hands-free session now opens with a clickable, blank window: "
        f"click_through={pill._click_through} "
        f"controls_visible={pill._controls_visible()} "
        f"h={pill.height_s.value:.1f}")
    pill.set_state("off")


def test_c1_tick_and_cross_still_fire(qapp):
    """The pill must still accept the clicks it exists for."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    pill = Pill()
    _settled(pill, "recording", "toggle")
    accept, cancel = pill._button_rects()
    assert accept is not None

    fired = []
    pill.accepted.connect(lambda: fired.append("accept"))
    pill.cancelled_by_user.connect(lambda: fired.append("cancel"))

    def release(pt):
        pill.mouseReleaseEvent(
            QMouseEvent(QEvent.MouseButtonRelease, pt, Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier))

    release(accept.center())
    release(cancel.center())
    release(pill._capsule_rect().center())      # the waveform: no control
    assert fired == ["accept", "cancel"], fired
    pill.set_state("off")


def test_c1_hold_mode_has_no_controls_at_full_size(qapp):
    pill = Pill()
    _settled(pill, "recording", "hold")
    assert pill.height_s.value >= BUTTON_MIN_H
    assert pill._button_rects() == (None, None)
    assert pill._click_through
    pill.set_state("off")


def test_c1_no_hits_while_the_capsule_shrinks_after_recording(qapp):
    """The regression the change is supposed to have fixed."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    pill = Pill()
    _settled(pill, "recording", "toggle")
    hot = pill._button_rects()[0].center()
    fired = []
    pill.accepted.connect(lambda: fired.append("accept"))

    pill.set_state("transcribing", "toggle")
    for _ in range(40):
        pill._tick()
        pill.mouseReleaseEvent(
            QMouseEvent(QEvent.MouseButtonRelease, hot, Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier))
    assert fired == [], f"stop-and-paste fired {len(fired)}x while shrinking"
    pill.set_state("off")


def test_c1_hover_only_where_the_buttons_are(qapp):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    pill = Pill()
    _settled(pill, "recording", "toggle")
    accept, cancel = pill._button_rects()

    def move(pt):
        pill.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pt, Qt.NoButton,
                                        Qt.NoButton, Qt.NoModifier))

    move(accept.center())
    assert pill._hover == "accept"
    move(cancel.center())
    assert pill._hover == "cancel"
    move(pill._capsule_rect().center())
    assert pill._hover is None
    pill.set_state("off")


# ----------------------------------------------------------------------------
# CLAIM 2 -- a cancel between clipboard and paste prevents the paste
# ----------------------------------------------------------------------------

def test_c2_clipboard_write_happens_even_when_cancelled(make_app, clip):
    """The half of the claim that must hold: losing the text is worse."""
    app = make_app()
    pcm, session, dur = run_one(app)
    session.cancelled = True
    app._process(pcm, session, dur)
    rows = app.history.recent(10)
    assert rows[0]["status"] == "cancelled"
    assert not any(c[0] == "key" and c[1] == VK_V
                   for c in inject_mod.user32.calls), "pasted after a cancel"


def test_c2_cancel_at_the_clipboard_prevents_the_paste(make_app, clip):
    """Esc pressed at the exact instant the transcript reaches the clipboard."""
    app = make_app()
    pcm, session, dur = run_one(app)
    real_copy = app.injector.copy

    def copy_then_esc(text):
        r = real_copy(text)
        app._cancel_session()          # <- the user presses Esc
        return r

    app.injector.copy = copy_then_esc
    app._process(pcm, session, dur)

    assert "hello world" in clip.copies, "the clipboard write must happen first"
    assert "flying" not in states_of(app), (
        "the comet launched (and will paste) after a cancel: "
        f"states={states_of(app)} cancelled={session.cancelled}")


def test_c2_post_clipboard_guard_is_correct_when_the_flag_is_set(make_app, clip):
    """The guard itself works -- if something can set the flag there."""
    app = make_app()
    pcm, session, dur = run_one(app)
    real_copy = app.injector.copy

    def copy_then_flag(text):
        r = real_copy(text)
        session.cancelled = True           # set directly, not via Esc
        return r

    app.injector.copy = copy_then_flag
    app._process(pcm, session, dur)
    assert "hello world" in clip.copies
    assert "flying" not in states_of(app), states_of(app)
    assert app.history.recent(10)[0]["status"] == "cancelled"


def test_c2_a_pending_session_is_still_cancellable_at_the_clipboard(make_app):
    """Why the guard cannot fire: _unpend() runs before the clipboard write,
    so Esc can no longer find the session."""
    app = make_app()
    pcm, session, dur = run_one(app)
    seen = {}

    real_copy = app.injector.copy

    def copy_then_look(text):
        r = real_copy(text)
        with app._pending_lock:
            seen["pending"] = list(app._pending)
        return r

    app.injector.copy = copy_then_look
    app._process(pcm, session, dur)
    assert seen["pending"] == [session], (
        "the session is no longer in the cancellable set at the moment the "
        f"transcript reaches the clipboard: _pending={seen['pending']}")


def test_c2_esc_during_the_comet_flight_prevents_the_paste(make_app, qapp):
    """End to end. The paste really happens ~370ms later, when the comet lands."""
    from murmur.__main__ import UiBridge

    app = make_app()
    pill = Pill()
    landings = {}

    class FakeComet:
        def launch(self, start, aim, colour=None, on_land=None):
            landings["on_land"] = on_land

    pastes = []

    def fake_paste():
        pastes.append(True)
        return True

    bridge = UiBridge(pill, comet=FakeComet(),
                      injector=types.SimpleNamespace(paste=fake_paste),
                      sounds=FakeSounds())

    pcm, session, dur = run_one(app)
    app._process(pcm, session, dur)
    assert "flying" in states_of(app)

    bridge._on_changed("flying", "toggle", 100, 100)
    assert "on_land" in landings

    app._cancel_session()              # <- Esc while the comet is in the air
    landings["on_land"]()

    assert pastes == [], (
        "Esc during the flight did not stop the paste; the cancel window "
        "closes before the clipboard write, not after it")
    pill.set_state("off")


def test_c2_a_cancel_belonging_to_another_session_does_not_suppress_this_one(
        make_app):
    """Two overlapping dictations: cancelling B must not eat A."""
    app = make_app()
    pcm_a, sess_a, dur_a = run_one(app, cursor=(100, 100))
    pcm_b, sess_b, dur_b = run_one(app, cursor=(900, 900))
    app._cancel_session()              # takes _pending[-1] == B
    assert sess_b.cancelled and not sess_a.cancelled
    app.states.clear()
    app._process(pcm_a, sess_a, dur_a)
    assert "flying" in states_of(app), states_of(app)


def test_c2_cancelled_session_writes_exactly_one_cancelled_row(make_app):
    app = make_app()
    pcm, session, dur = run_one(app)
    session.cancelled = True
    app._process(pcm, session, dur)
    rows = app.history.recent(10)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "cancelled", rows[0]


def test_c2_no_launch_cue_when_nothing_launches(make_app, u32):
    """copy() reports a held modifier: no comet flies and nothing is pasted,
    but the launch cue is played anyway."""
    app = make_app()
    u32.held = {VK_CONTROL}
    app.injector.release_timeout_s = 0.0
    pcm, session, dur = run_one(app)
    app._process(pcm, session, dur)
    assert "copied" in states_of(app), states_of(app)
    assert "launch" not in app.sounds.played, (
        f"launch cue played with no launch: {app.sounds.played}")


# ----------------------------------------------------------------------------
# CLAIM 3 -- the aim belongs to the session, not a shared slot
# ----------------------------------------------------------------------------

def test_c3_overlapping_dictations_each_keep_their_own_aim(make_app):
    app = make_app()
    pcm_a, sess_a, _ = run_one(app, cursor=(100, 100))
    pcm_b, sess_b, _ = run_one(app, cursor=(900, 900))
    assert sess_a.aim == (100, 100) and sess_b.aim == (900, 900)
    app.states.clear()
    app._process(pcm_a, sess_a, 1000)
    flying = [kw for s, kw in app.states if s == "flying"]
    assert flying and flying[0]["aim"] == (100, 100), flying


def test_c3_unreadable_cursor_must_not_borrow_a_newer_sessions_aim(make_app):
    """A's cursor could not be read. A must not fly at B's cursor."""
    app = make_app()
    pcm_a, sess_a, _ = run_one(app, cursor=None)     # GetCursorPos failed
    assert sess_a.aim is None
    pcm_b, sess_b, _ = run_one(app, cursor=(900, 900))
    assert app._aim == (900, 900)
    app.states.clear()
    app._process(pcm_a, sess_a, 1000)
    flying = [kw for s, kw in app.states if s == "flying"]
    assert flying, states_of(app)
    assert flying[0]["aim"] != (900, 900), (
        "session A flew at session B's cursor via the shared _aim slot: "
        f"{flying[0]['aim']}")


def test_c3_no_aim_falls_back_to_an_immediate_paste(qapp):
    from murmur.__main__ import UiBridge

    pill = Pill()
    pastes = []

    def fake_paste():
        pastes.append(True)
        return True

    bridge = UiBridge(pill, comet=None,
                      injector=types.SimpleNamespace(paste=fake_paste),
                      sounds=FakeSounds())
    bridge._on_changed("flying", "toggle", -1, -1)
    assert pastes == [True], "an unaimed transcript must still be delivered"
    pill.set_state("off")


# ----------------------------------------------------------------------------
# CLAIM 4 -- paste() re-checks the modifiers itself
# ----------------------------------------------------------------------------

def test_c4_paste_refuses_and_does_not_send_ctrl_v(u32):
    inj = injector(u32, release_timeout_s=0.0)
    u32.held = {VK_CONTROL}
    assert inj.paste() is False
    assert not u32.pasted()


def test_c4_paste_still_pastes_when_nothing_is_held(u32):
    inj = injector(u32)
    assert inj.paste() is True
    assert u32.pasted()


def test_c4_refusal_is_surfaced_to_the_user_on_the_comet_path(qapp):
    """The comet lands, paste() refuses, and the pill still says 'done'."""
    from murmur.__main__ import UiBridge

    pill = Pill()
    landings = {}

    class FakeComet:
        def launch(self, start, aim, colour=None, on_land=None):
            landings["on_land"] = on_land

    bridge = UiBridge(pill, comet=FakeComet(),
                      injector=types.SimpleNamespace(paste=lambda: False),
                      sounds=FakeSounds())
    bridge._on_changed("flying", "toggle", 100, 100)
    landings["on_land"]()
    assert pill.state == "copied", (
        f"paste() refused but the pill shows {pill.state!r}; 'done' vs "
        "'copied' is the only signal the user gets")
    pill.set_state("off")


def test_c4_refusal_is_surfaced_on_the_no_comet_path(qapp):
    from murmur.__main__ import UiBridge

    pill = Pill()
    bridge = UiBridge(pill, comet=None,
                      injector=types.SimpleNamespace(paste=lambda: False),
                      sounds=FakeSounds())
    bridge._on_changed("flying", "toggle", -1, -1)
    assert pill.state == "copied", (
        f"paste() refused but the pill shows {pill.state!r}")
    pill.set_state("off")


def test_c4_receipt_must_not_claim_pasted_when_the_paste_refuses(
        make_app, qapp, caplog):
    """`_receipt` exists to answer 'did it actually land'."""
    from murmur.__main__ import UiBridge

    app = make_app()
    pill = Pill()
    landings = {}

    class FakeComet:
        def launch(self, start, aim, colour=None, on_land=None):
            landings["on_land"] = on_land

    bridge = UiBridge(pill, comet=FakeComet(),
                      injector=types.SimpleNamespace(paste=lambda: False),
                      sounds=FakeSounds())

    pcm, session, dur = run_one(app)
    with caplog.at_level(logging.INFO, logger="murmur.app"):
        app._process(pcm, session, dur)
    bridge._on_changed("flying", "toggle", 100, 100)
    landings["on_land"]()

    receipts = [r.getMessage() for r in caplog.records
                if r.name == "murmur.app" and "->" in r.getMessage()]
    assert receipts, "no receipt written at all"
    assert not any("] pasted ->" in m for m in receipts), (
        f"receipt claims the text was pasted, but it was not: {receipts}")
    pill.set_state("off")


def test_c4_paste_does_not_refuse_for_a_harmless_held_modifier(u32):
    """Shift held for an unrelated reason (extending a selection).

    Ctrl+Win+V is the documented hazard. Refusing on Shift alone strands the
    transcript on the clipboard behind a green tick.
    """
    inj = injector(u32, release_timeout_s=0.05)
    u32.held = {VK_SHIFT}
    assert inj.paste() is True, (
        "paste refused because Shift was held; the transcript is now stranded")


def test_c4_paste_does_not_block_the_calling_thread(u32):
    """paste() runs on the Qt UI thread, from the comet's landing callback."""
    inj = injector(u32, release_timeout_s=0.5)
    u32.held = {VK_CONTROL}
    t0 = time.perf_counter()
    inj.paste()
    dt = time.perf_counter() - t0
    assert dt < 0.1, (
        f"paste() froze the UI thread for {dt*1000:.0f}ms; it used to be a "
        "bare Ctrl+V")


def test_c4_paste_does_not_stall_behind_a_concurrent_copy(u32):
    """A second dictation's copy() holds the injector lock across its own
    modifier wait; the UI thread's paste() queues behind it."""
    inj = injector(u32, release_timeout_s=0.4)
    holding = threading.Event()

    def worker():
        with inj._lock:
            holding.set()
            time.sleep(0.4)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    holding.wait(1.0)
    t0 = time.perf_counter()
    inj.paste()
    dt = time.perf_counter() - t0
    t.join()
    assert dt < 0.1, (
        f"paste() on the UI thread blocked {dt*1000:.0f}ms on the injector "
        "lock held by the worker thread")


def test_c4_reentrant_lock_does_not_deadlock(u32):
    inj = injector(u32)
    done = []

    def run():
        with inj._lock:
            inj.copy("a")
            inj.paste()
            inj.inject("b")
        done.append(True)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert done == [True], "re-entrant acquisition deadlocked"


def test_c4_paste_does_not_double_release_a_modifier(u32):
    """copy() already forced Shift up. paste() forces it up a second time,
    ~370ms later -- inside whatever the user has started doing since."""
    inj = injector(u32, release_timeout_s=0.0)
    u32.held = {VK_SHIFT}
    inj.copy("hello")
    first = u32.keyups().count(VK_SHIFT)
    inj.paste()
    second = u32.keyups().count(VK_SHIFT)
    assert second == first, (
        f"paste() forced Shift up again ({second - first} extra synthetic "
        "KEYUPs) after copy() had already done it")


# ----------------------------------------------------------------------------
# CLAIM 5 -- the modifier wait samples once before its timed loop
# ----------------------------------------------------------------------------

def test_c5_zero_timeout_samples_before_giving_up(u32):
    inj = injector(u32, release_timeout_s=0.0)
    assert inj._release_modifiers() is True


def test_c5_zero_timeout_still_refuses_a_key_that_is_genuinely_down(u32):
    inj = injector(u32, release_timeout_s=0.0)
    u32.held = {VK_CONTROL}
    assert inj._release_modifiers() is False


def test_c5_sample_is_taken_after_the_forced_release(u32):
    inj = injector(u32, release_timeout_s=0.0)
    u32.held = {VK_SHIFT}
    inj._release_modifiers()
    kinds = [c[0] for c in u32.calls]
    first_key = kinds.index("key")
    assert "get" in kinds[first_key:], "no sample after the forced release"


def test_c5_wait_still_waits_for_a_late_release(u32):
    inj = injector(u32, release_timeout_s=0.5)
    u32.held = {VK_CONTROL}
    u32.release_after = (VK_CONTROL, time.perf_counter() + 0.12)
    t0 = time.perf_counter()
    assert inj._release_modifiers() is True
    assert time.perf_counter() - t0 >= 0.1


def test_c5_transient_clear_after_an_injected_keyup_must_not_authorise_a_paste(
        u32):
    """Windows updates the async key state from injected input, so the forced
    KEYUP can read as 'clear' for one poll before the hardware re-asserts.
    The early sample lands inside exactly that window."""
    inj = injector(u32, release_timeout_s=0.0)
    u32.held = {VK_CONTROL}
    u32.transient_clear = True
    assert inj._release_modifiers() is False, (
        "the single early sample was taken while the injected KEYUP had "
        "momentarily cleared a physically held Ctrl")
