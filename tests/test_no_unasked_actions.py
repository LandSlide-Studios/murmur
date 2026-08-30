"""Murmur never takes an action the user did not ask for.

Tier 2 of the audit remediation. Nothing here loses a transcript; each one fires
something at the wrong moment or against the wrong target.
"""
import numpy as np
import pytest

import murmur.app as A


# --- the aim belongs to the session that stopped -----------------------------

def test_each_session_remembers_its_own_cursor(app):
    """One slot on the app, written by every stop, meant a dictation still
    transcribing when the next one ended flew to the NEWER session's cursor."""
    points = iter([(100, 100), (900, 900)])
    app._cursor_point = lambda: next(points)

    app._start("hold")
    first = app._session
    app._stop_and_transcribe()
    app._start("hold")
    second = app._session
    app._stop_and_transcribe()

    assert first.aim == (100, 100)
    assert second.aim == (900, 900)
    assert first.aim != second.aim


def test_a_fresh_session_starts_with_no_aim(app):
    app._start("hold")
    assert app._session.aim is None


# --- cancelling between the clipboard and the paste --------------------------

def test_a_cancel_after_the_clipboard_does_not_paste(app, monkeypatch):
    """The clipboard is not the document. Cancellation was checked once before
    the copy and never again, so an Esc landing in that window was
    acknowledged and then overruled by the paste."""
    class FakeStt:
        def transcribe(self, pcm, hotwords):
            return "do not paste this"

    monkeypatch.setattr(app, "_stt", FakeStt())
    monkeypatch.setattr(app.polisher, "enabled", False)

    session = A.Session(51, "hold")

    def copy_then_cancel(text):
        session.cancelled = True          # Esc lands right here
        return True

    monkeypatch.setattr(app.injector, "copy", copy_then_cancel)

    states = []
    app.on_state = lambda state, **kw: states.append(state)
    app._process(np.full(16000, 0.3, dtype=np.float32), session, 1000)

    assert "flying" not in states, "pasted into the document after a cancel"
    rows = app.history.recent()
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"


def test_an_uncancelled_session_still_flies(app, monkeypatch):
    """The counterweight."""
    class FakeStt:
        def transcribe(self, pcm, hotwords):
            return "paste this"

    monkeypatch.setattr(app, "_stt", FakeStt())
    monkeypatch.setattr(app.polisher, "enabled", False)
    monkeypatch.setattr(app.injector, "copy", lambda t: True)

    states = []
    app.on_state = lambda state, **kw: states.append(state)
    app._process(np.full(16000, 0.3, dtype=np.float32), A.Session(52, "hold"), 1000)

    assert "flying" in states


# --- the pill never offers a target it has not drawn -------------------------

@pytest.mark.parametrize("state", [
    "armed", "recording", "transcribing", "polishing",
    "done", "copied", "cancelled", "error",
])
def test_hit_testing_and_painting_agree_in_every_state(pill, state):
    """Hit-testing gated on the active GROUP while the painter drew only while
    recording. For the frames after a hands-free recording ended, the tick and
    cross were hittable and undrawn, so a click aimed at the editor was taken
    and reinterpreted as stop-and-paste."""
    pill.set_state(state, "toggle")
    pill.width_s.value, pill.height_s.value = 30.0, 180.0

    drawn = pill.state == "recording"
    accept, cancel = pill._button_rects()
    hittable = accept is not None

    assert hittable <= drawn, f"{state}: clickable buttons that are never drawn"


def test_the_working_states_do_not_accept_clicks(pill):
    """The specific ~100ms window: a hands-free recording has just ended and the
    capsule is still large enough to hold the controls."""
    for state in ("transcribing", "polishing"):
        pill.set_state(state, "toggle")
        pill.width_s.value, pill.height_s.value = 30.0, 180.0
        assert pill._controls_visible() is False, state


# --- the paste checks the modifiers itself -----------------------------------

def test_paste_refuses_while_a_modifier_is_held():
    """It assumed copy() had already cleared them. The split exists so an
    animation can run in between, and the chord can be re-pressed inside that
    window -- which turns the paste into Ctrl+Win+V and opens Clipboard
    History."""
    from murmur.inject import Injector

    inj = Injector()
    sent = []
    inj._send_paste = lambda: sent.append(1)
    inj._paste_blocking_modifiers = lambda: True

    assert inj.paste() is False
    assert sent == [], "sent Ctrl+V with Ctrl or Win physically down"


def test_paste_does_not_refuse_for_a_harmless_modifier():
    """The documented hazard is Ctrl+Win+V opening Clipboard History. Shift or
    Alt being down is the user typing again — refusing there strands the
    transcript, and forcing those keys up would break their selection."""
    from murmur import inject as I

    held = {I.VK_SHIFT, I.VK_MENU}

    class FakeUser32:
        def GetAsyncKeyState(self, vk):
            return 0x8000 if vk in held else 0

        def keybd_event(self, *a):
            raise AssertionError("forced a modifier up during a paste")

    inj = I.Injector()
    import unittest.mock as mock
    with mock.patch.object(I, "user32", FakeUser32()):
        assert inj._paste_blocking_modifiers() is False


def test_paste_proceeds_when_the_modifiers_are_clear():
    from murmur.inject import Injector

    inj = Injector()
    sent = []
    inj._send_paste = lambda: sent.append(1)
    inj._paste_blocking_modifiers = lambda: False

    assert inj.paste() is True
    assert sent == [1]


def test_the_modifier_wait_samples_before_it_waits(monkeypatch):
    """The check lived only inside a timed loop whose condition is evaluated
    first, so a zero timeout reported "still held" having polled nothing."""
    from murmur import inject as I

    polls = []

    class FakeUser32:
        def GetAsyncKeyState(self, vk):
            polls.append(vk)
            return 0                       # nothing held

        def keybd_event(self, *a):
            pass

    monkeypatch.setattr(I, "user32", FakeUser32())
    inj = I.Injector(release_timeout_s=0.0)

    assert inj._release_modifiers() is True
    assert polls, "gave up without ever sampling the key state"
