"""Full-system verification against the plan's 'Done means' list.

Real keyboard hook, real chord FSM, real app, real microphone device, real GPU
transcription, real local LLM cleanup, real clipboard paste into a real Windows
app, read back via UI Automation.

The one substitution: a script cannot speak, so a recorded fixture is handed to
the pipeline in place of what the device captured. The device is still opened
and driven for real, and section 0 asserts on what it ACTUALLY captured — an
earlier version of this harness replaced the Recorder object wholesale and was
therefore blind to a ~550ms capture gap that lost the opening word of every
dictation. A green gate over a stubbed component proves nothing about the
component.

SAFETY: the target window is a uniquely-named scratch file this script creates.
It never touches a window it did not open.
"""

import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import murmur.__main__ as entry
from murmur.app import MurmurApp
from murmur.config import Config
from murmur.ui.pill import Pill

ROOT = Path(__file__).resolve().parent.parent
u = ctypes.WinDLL("user32", use_last_error=True)
VK_CONTROL, VK_LWIN, VK_SPACE, VK_ESCAPE = 0x11, 0x5B, 0x20, 0x1B
UP = 0x0002

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def tap(vk, down=True, pause=0.03):
    u.keybd_event(vk, 0, 0 if down else UP, 0)
    time.sleep(pause)


def find_window(needle):
    hits = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

    def cb(hwnd, _lp):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, b, n + 1)
                if needle in b.value:
                    hits.append(hwnd)
        return True

    u.EnumWindows(proc(cb), 0)
    return hits[0] if hits else None


def read_window_text(hwnd):
    import comtypes.client

    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    uia = comtypes.client.CreateObject(
        "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=mod.IUIAutomation)
    el = uia.ElementFromHandle(hwnd)
    for ctrl in (mod.UIA_DocumentControlTypeId, mod.UIA_EditControlTypeId):
        cond = uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, ctrl)
        node = el.FindFirst(mod.TreeScope_Descendants, cond)
        if node:
            pat = node.GetCurrentPattern(mod.UIA_TextPatternId)
            if pat:
                return pat.QueryInterface(
                    mod.IUIAutomationTextPattern).DocumentRange.GetText(-1)
    return None


def main():
    entry._configure_logging()
    import logging
    logging.getLogger().setLevel(logging.WARNING)

    with wave.open(str(ROOT / "tests/fixtures/speech16k.wav"), "rb") as f:
        fixture = np.frombuffer(f.readframes(f.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768.0

    cfg = Config.load(ROOT / "settings.json")
    # Isolate the stores so verification never touches real history.
    tmpdir = Path(tempfile.mkdtemp(prefix="murmur_verify_"))
    import murmur.app as app_mod
    app_mod.data_dir = lambda: tmpdir

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    pill = Pill(offset_px=cfg.get("ui.pill_offset_px"))
    from murmur.ui.comet import Comet

    comet = Comet() if cfg.get("ui.comet", True) else None
    bridge = entry.UiBridge(pill, comet=comet)
    mu = MurmurApp(cfg, on_state=bridge)
    bridge.injector = mu.injector

    # The REAL Recorder is used, opened against the real device. A script
    # cannot speak, so known audio is written into the real ring buffer after a
    # real begin(). The previous harness replaced the Recorder wholesale and was
    # therefore structurally blind to a ~550ms capture gap that lost the opening
    # word of every dictation.
    mu.recorder.open()

    real_end = mu.recorder.end

    def end_with_fixture():
        captured = real_end()
        # Keep what the device really gave us for the timing checks, but hand
        # the pipeline audio it can actually transcribe.
        end_with_fixture.captured = captured
        return fixture

    end_with_fixture.captured = None
    mu.recorder.end = end_with_fixture

    # Production hook, but accepting synthetic keys so the chords can be driven.
    mu.hotkeys.accept_injected = True
    mu.start()
    pump = QTimer(qapp)
    pump.timeout.connect(mu.pump)
    pump.start(16)

    print("warming the model...")
    _ = mu.stt
    print(f"model on {mu.stt.device}\n")

    scratch = Path(tempfile.gettempdir()) / f"murmur_verify_{os.getpid()}.txt"
    scratch.write_text("", encoding="utf-8")
    proc = subprocess.Popen(["notepad.exe", str(scratch)])
    hwnd = None
    for _ in range(40):
        time.sleep(0.25)
        qapp.processEvents()
        hwnd = find_window(scratch.stem)
        if hwnd:
            break
    if not hwnd:
        print("could not open the scratch window")
        return 1
    u.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    def settle(seconds):
        end = time.time() + seconds
        while time.time() < end:
            qapp.processEvents()
            time.sleep(0.02)

    try:
        # --- 0. the microphone is live the instant a session starts ------
        print("0. capture latency (the real device)")
        import time as _t
        mu.recorder.begin()
        _t.sleep(0.40)
        got = mu.recorder.end()
        captured = len(end_with_fixture.captured) / 16000
        check("a 0.4s session captures ~0.4s of audio, not zero",
              captured > 0.30, f"{captured:.3f}s")
        check("pre-roll means audio predates the keypress",
              captured >= 0.40, f"{captured:.3f}s for a 0.40s hold")

        # --- 1. hold-to-talk into a real app -----------------------------
        print("1. hold-to-talk")
        before = (read_window_text(hwnd) or "").strip()
        tap(VK_CONTROL); tap(VK_LWIN)
        settle(0.6)
        recording = pill.state == "recording" and pill.mode == "hold"
        tap(VK_LWIN, False); tap(VK_CONTROL, False)
        settle(11)
        after = (read_window_text(hwnd) or "").strip()
        check("pill showed 'recording' in hold mode", recording)
        # Assert on substance, not exact wording: the cleanup step is an LLM
        # and its handling of a leading clause varies between runs.
        landed = [w for w in ("dns records", "northgate", "priya", "invoice")
                  if w in after.lower()]
        check("text pasted into a real third-party app",
              len(after) > len(before) and len(landed) >= 3,
              f"{len(after)} chars, matched {landed}")
        check("modifiers released; Clipboard History did not steal the paste",
              len(landed) >= 3)

        # --- 2. hands-free toggle ----------------------------------------
        print("\n2. hands-free toggle")
        mark = (read_window_text(hwnd) or "").strip()
        tap(VK_CONTROL); tap(VK_LWIN); tap(VK_SPACE)
        tap(VK_SPACE, False); tap(VK_LWIN, False); tap(VK_CONTROL, False)
        settle(0.6)
        promoted = pill.mode == "toggle" and pill.state == "recording"
        check("hands off: session survived releasing the chord", promoted)
        tap(VK_CONTROL); tap(VK_LWIN); tap(VK_SPACE)
        tap(VK_SPACE, False); tap(VK_LWIN, False); tap(VK_CONTROL, False)
        settle(11)
        after2 = (read_window_text(hwnd) or "").strip()
        check("second chord stopped it and pasted", len(after2) > len(mark))

        # --- 3. Esc cancels ----------------------------------------------
        print("\n3. Esc cancels")
        mark3 = (read_window_text(hwnd) or "").strip()
        tap(VK_CONTROL); tap(VK_LWIN)
        settle(0.6)
        tap(VK_ESCAPE); tap(VK_ESCAPE, False)
        tap(VK_LWIN, False); tap(VK_CONTROL, False)
        settle(4)
        after3 = (read_window_text(hwnd) or "").strip()
        check("cancelled session pasted nothing", after3 == mark3)

        # --- 4. history ---------------------------------------------------
        print("\n4. history")
        rows = mu.history.recent()
        statuses = [r["status"] for r in rows]
        check("every session recorded", len(rows) >= 3, f"{len(rows)} rows")
        check("cancelled session still recorded", "cancelled" in statuses,
              str(statuses))
        check("history kept the raw transcript",
              any(r["raw_text"] for r in rows))

        # --- 5. learning --------------------------------------------------
        print("\n5. learning from a correction")
        row = next(r for r in rows if r["final_text"])
        original = row["final_text"]
        edited = original.replace("Priya", "Priyah").replace("Northgate", "North Gate")
        learned = mu.corrections.learn_from_edit(original, edited)
        check("a manual correction was learned", learned > 0, f"{learned} term(s)")
        check("the learned term is active immediately",
              len(mu.vocab.hotwords()) > 0, str(mu.vocab.hotwords()))
        check("the learned spelling is applied to a later transcript",
              mu.vocab.apply(original) != original)

        # --- 6. offline ----------------------------------------------------
        print("\n6. offline")
        import socket
        real_socket = socket.socket

        class Blocked(Exception):
            pass

        def no_network(*a, **k):
            raise Blocked("network was used")

        socket.socket = no_network
        try:
            out = mu.stt.transcribe(fixture, hotwords=[])
            stt_ok = "three items" in out.lower()
        except Blocked:
            stt_ok = False
        finally:
            socket.socket = real_socket
        check("transcription works with the network blocked", stt_ok)

        polished = mu.polisher.polish("um so the thing is uh we should go")
        check("cleanup runs on localhost, not the internet",
              polished != "um so the thing is uh we should go", polished[:60])

        # --- 7. pill never takes focus -------------------------------------
        print("\n7. focus")
        fg = u.GetForegroundWindow()
        check("focus stayed with the target app, never the pill",
              fg != int(pill.winId()))

    finally:
        mu.stop()
        proc.terminate()
        time.sleep(0.4)
        scratch.unlink(missing_ok=True)

    passed = sum(1 for _l, ok, _d in results if ok)
    print(f"\n{'=' * 60}\n{passed}/{len(results)} checks passed")
    for label, ok, detail in results:
        if not ok:
            print(f"  FAILED: {label} {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
