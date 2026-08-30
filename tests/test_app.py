"""Session lifecycle, especially the places two threads meet."""
import threading
import time

import numpy as np
import pytest

import murmur.app as A
from murmur.config import Config


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "data_dir", lambda: tmp_path)
    cfg = Config.load(tmp_path / "nope.json")
    # No UI Automation in unit tests: constructing the reader initialises COM on
    # the test thread, which collides with other tests' worker threads.
    cfg.set("learning.uia_readback", False)
    mu = A.MurmurApp(cfg)

    # Substitute the device. `end()` is deliberately slowed: the real one is not
    # instant, and the concurrency bug this file guards only opens when the call
    # between claiming the session and returning the audio takes real time.
    mu.recorder.open = lambda: None
    mu.recorder.begin = lambda: None

    def slow_end():
        time.sleep(0.002)
        return np.zeros(16000, dtype=np.float32)

    mu.recorder.end = slow_end
    mu.recorder.close = lambda: None
    yield mu
    mu.history.close()
    mu.vocab.close()


def drain(app):
    while not app._jobs.empty():
        app._jobs.get_nowait()


# --- concurrency ------------------------------------------------------------

def test_concurrent_stops_enqueue_only_one_job(app):
    """The silence auto-stop fires on the audio thread while the hotkey handler
    stops the same session on the UI thread. Without a lock both pass the None
    check and the transcript is pasted twice."""
    for _ in range(25):
        drain(app)
        app._start("hold")
        barrier = threading.Barrier(2)

        def stop():
            barrier.wait()
            app._stop_and_transcribe()

        threads = [threading.Thread(target=stop) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert app._jobs.qsize() == 1


def test_concurrent_starts_only_open_one_session(app):
    opens = []

    def begin():
        opens.append(1)
        time.sleep(0.002)

    app.recorder.begin = begin
    barrier = threading.Barrier(2)

    def start():
        barrier.wait()
        app._start("hold")

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(opens) == 1
    assert app._session is not None


# --- session identity -------------------------------------------------------

def test_a_new_session_is_not_pre_cancelled(app):
    """The old code cleared one shared cancel Event on start, which is exactly
    what let a cancelled session's text paste into the next one."""
    app._start("hold")
    first = app._session
    app._cancel_session()
    assert first.cancelled is True
    app._start("hold")
    assert app._session is not first
    assert app._session.cancelled is False


def test_cancelling_a_later_session_does_not_discard_an_earlier_one(app):
    """Session A finishes recording and is in flight; the user cancels a LATER
    session B. A must still be delivered."""
    app._start("hold")
    a_session = app._session
    app._stop_and_transcribe()
    app._start("hold")
    app._cancel_session()
    assert a_session.cancelled is False, "cancelling B discarded A"


def test_cancelling_reaches_a_session_already_transcribing(app):
    """Esc during transcription must cancel the transcription that is running."""
    app._start("hold")
    session = app._session
    app._stop_and_transcribe()
    app._cancel_session()
    assert session.cancelled is True


def test_sessions_get_distinct_identities(app):
    app._start("hold")
    first = app._session
    app._stop_and_transcribe()
    app._start("hold")
    assert app._session.id != first.id


# --- transitions ------------------------------------------------------------

def test_stop_after_cancel_does_not_enqueue(app):
    app._start("hold")
    app._cancel_session()
    drain(app)
    app._stop_and_transcribe()
    assert app._jobs.qsize() == 0


def test_cancel_records_a_history_row(app):
    app._start("hold")
    app._cancel_session()
    rows = app.history.recent()
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"


def test_discard_records_nothing(app):
    app._start("hold")
    app._discard()
    assert app.history.recent() == []


def test_double_discard_is_harmless(app):
    app._start("hold")
    app._discard()
    app._discard()
    assert app._session is None


def test_promote_only_applies_to_a_hold_session(app):
    app._promote()                       # nothing recording
    assert app._session is None
    app._start("toggle")
    app._promote()
    assert app._session.mode == "toggle"


def test_promote_converts_a_hold_session(app):
    app._start("hold")
    app._promote()
    assert app._session.mode == "toggle"


def test_starting_while_already_recording_is_ignored(app):
    app._start("hold")
    first = app._session
    app._start("toggle")
    assert app._session is first
    assert app._session.mode == "hold"


def test_silence_autostop_only_applies_to_toggle_sessions(app):
    """In hold mode the user's finger is the stop condition."""
    app._start("hold")
    for _ in range(200):
        app._on_level(0.0)
    assert app._session is not None


def test_a_session_started_externally_can_be_cancelled_with_esc(app):
    """The desktop shortcut starts a session directly, bypassing the chord. The
    FSM has to be told, or Esc does nothing and it runs until the 90s timeout."""
    from murmur.platform.win.chord import Act, Ev

    app._start("toggle", external=True)
    acts = app.hotkeys.fsm.feed(Ev("down", "esc", 1000))
    assert Act.CANCEL in acts


def test_a_silent_recording_writes_exactly_one_history_row(app, monkeypatch):
    """The silence guard used to write its own 'empty' row and then return, but
    the finally block still ran — two rows for one session, the second claiming
    status 'ok' with no text in it."""
    import numpy as np

    monkeypatch.setattr(app, "_stt", object())      # must never be reached
    silence = np.zeros(16000, dtype=np.float32)
    app._process(silence, A.Session(1, "hold"), 1000)
    rows = app.history.recent()
    assert len(rows) == 1, f"{len(rows)} rows written for one session"
    assert rows[0]["status"] == "empty"
    assert rows[0]["final_text"] is None


def test_a_real_recording_also_writes_exactly_one_row(app, monkeypatch):
    import numpy as np

    class FakeStt:
        def transcribe(self, pcm, hotwords):
            return "hello there"

    monkeypatch.setattr(app, "_stt", FakeStt())
    monkeypatch.setattr(app.polisher, "enabled", False)
    monkeypatch.setattr(app.injector, "copy", lambda t: True)
    loud = np.full(16000, 0.3, dtype=np.float32)
    app._process(loud, A.Session(2, "hold"), 1000)
    rows = app.history.recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["final_text"] == "hello there"


def test_a_long_dictation_full_of_pauses_is_not_discarded_as_silent(app, monkeypatch):
    """The one he kept hitting.

    16:17:45  recording is silent (42.4s); nothing to transcribe

    He held the chord for 42.4 seconds, talked the whole way through, and the
    app threw it away. The guard averaged RMS across the entire recording, so
    every thinking pause dragged the number down: the longer the dictation, the
    more likely it vanished. Exactly backwards.

    Levels here are his measured ones — ambient floor 0.00086 on the eMeet C96,
    speech around 0.008.
    """
    import numpy as np

    class FakeStt:
        def transcribe(self, pcm, hotwords):
            return "the words he actually said"

    monkeypatch.setattr(app, "_stt", FakeStt())
    monkeypatch.setattr(app.polisher, "enabled", False)
    monkeypatch.setattr(app.injector, "copy", lambda t: True)

    # The property that actually separates the two guards is length-invariance.
    # Lowering the threshold alone moves the cliff; it does not remove it. Same
    # speech, far more silence around it — the verdict must not change.
    sr = 16000
    rng = np.random.default_rng(7)
    speech = rng.normal(0, 0.008, sr * 4).astype(np.float32)
    pcm = np.concatenate(
        [speech, rng.normal(0, 0.00086, sr * 90).astype(np.float32)])

    from murmur.audio import rms
    assert rms(pcm) < app.cfg.get("audio.speech_rms_threshold") / 2,         "precondition: the old whole-clip average would have discarded this"

    app._process(pcm, A.Session(9, "hold"), 94_000)

    rows = app.history.recent()
    assert len(rows) == 1
    assert rows[0]["status"] != "empty", "discarded a dictation he spoke through"
    assert rows[0]["final_text"] == "the words he actually said"


def test_the_guard_still_rejects_a_recording_of_an_empty_room(app, monkeypatch):
    """The counterweight: loosening the guard must not start feeding silence to
    Whisper, which invents words from it — a clip of nothing but an audio cue
    once transcribed to "Thanks."."""
    import numpy as np

    monkeypatch.setattr(app, "_stt", object())        # must never be reached
    room = np.random.default_rng(8).normal(0, 0.00086, 16000 * 20).astype(np.float32)
    app._process(room, A.Session(10, "hold"), 20_000)

    rows = app.history.recent()
    assert len(rows) == 1 and rows[0]["status"] == "empty"


# --- the two critical findings from the adversarial audit --------------------

def test_esc_cancels_the_session_the_user_was_looking_at(app):
    """Cancel used to fall back to whichever job the worker happened to be
    holding, so which session Esc hit was decided by worker timing: 200/200
    cancels lost once the worker had moved on, and an older session destroyed
    in its place."""
    app._start("hold")
    first = app._session
    app._stop_and_transcribe()
    app._start("hold")
    second = app._session
    app._stop_and_transcribe()

    app._cancel_session()

    assert second.cancelled is True, "the session on screen was not cancelled"
    assert first.cancelled is False, "cancelled a session the user never touched"


def test_esc_with_nothing_pending_cancels_nothing_and_says_nothing(app):
    """It announced a cancel unconditionally — confirming one for text already
    sitting in the user's document."""
    said = []
    app.on_state = lambda state, **kw: said.append(state)
    app._cancel_session()
    assert "cancelled" not in said


def test_a_delivered_session_is_not_cancelled_by_a_later_esc(app, monkeypatch):
    """Once the text is on the clipboard, Esc cannot take it back — and must not
    write a 'cancelled' row for a dictation that was delivered."""
    import numpy as np

    class FakeStt:
        def transcribe(self, pcm, hotwords):
            return "already in the document"

    monkeypatch.setattr(app, "_stt", FakeStt())
    monkeypatch.setattr(app.polisher, "enabled", False)
    monkeypatch.setattr(app.injector, "copy", lambda t: True)

    session = A.Session(41, "hold")
    with app._pending_lock:
        app._pending.append(session)
    app._process(np.full(16000, 0.3, dtype=np.float32), session, 1000)

    app._cancel_session()

    assert session.cancelled is False
    rows = app.history.recent()
    assert len(rows) == 1 and rows[0]["status"] == "ok"


def test_the_chord_fsm_is_told_when_the_audio_thread_stops_a_session(app):
    """There was an adopt on the way in and nothing on the way out, so a
    hands-free session ended by the silence auto-stop left the FSM believing it
    was still recording. Every later Esc then emitted a real cancel into an app
    with nothing recording — which is what made the fallback above reachable."""
    from murmur.platform.win.chord import St

    app._start("toggle", external=True)
    assert app.hotkeys.fsm.state is St.REC_TOGGLE
    app._stop_and_transcribe()
    assert app.hotkeys.fsm.state is St.IDLE, "the FSM still thinks it is recording"


def test_esc_after_an_auto_stop_does_not_emit_a_stray_cancel(app):
    """The two findings joined up: this is the end-to-end path."""
    from murmur.platform.win.chord import Act, Ev

    app._start("toggle", external=True)
    app._stop_and_transcribe()
    acts = app.hotkeys.fsm.feed(Ev("down", "esc", 9000))
    assert Act.CANCEL not in acts


def test_release_session_is_idempotent_and_safe_on_the_chord_path(app):
    """The chord path has already ended the session through _end, so the release
    must be a no-op there rather than disturbing the FSM."""
    from murmur.platform.win.chord import St

    app._start("hold")
    app._stop_and_transcribe()
    before = app.hotkeys.fsm.state
    app._release_fsm()
    app._release_fsm()
    assert app.hotkeys.fsm.state is before is St.IDLE
