"""End-to-end pipeline proof: real audio -> STT -> polish -> paste into a real app.

Feeds the recorded speech fixture through the real MurmurApp worker path, so
every stage except the microphone itself is production code. Then reads the text
back out of the target window via UI Automation to confirm what actually landed.

SAFETY: this opens its OWN uniquely-named scratch file and targets that window
by exact title. An earlier version called FindWindow("Notepad") and pasted into
whatever Notepad happened to be open first — which was a real file the user had
open. Never target a window this script did not create.
"""

import ctypes
import ctypes.wintypes as w
import difflib
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from murmur.app import MurmurApp
from murmur.config import Config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("faster_whisper", "httpx", "huggingface_hub", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

u = ctypes.WinDLL("user32", use_last_error=True)
ROOT = Path(__file__).resolve().parent.parent

REFERENCE = ("Hey can you add three items to the list. First one is check the DNS "
             "records for the Northgate site. Second is follow up with Priya about "
             "the photos. And the third thing is just make sure the invoice went out.")


def find_window_by_exact_title(needle: str):
    """Only ever returns a window whose title contains our unique scratch name."""
    hits = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

    def cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, b, n + 1)
        if needle in b.value:
            hits.append(hwnd)
        return True

    u.EnumWindows(proc(cb), 0)
    return hits[0] if hits else None


def read_window_text(hwnd):
    """UI Automation, because Windows 11 Notepad is WinUI and exposes no classic
    Edit child to SendMessage."""
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
                tp = pat.QueryInterface(mod.IUIAutomationTextPattern)
                return tp.DocumentRange.GetText(-1)
    return None


def main():
    with wave.open(str(ROOT / "tests/fixtures/speech16k.wav"), "rb") as wav:
        pcm = np.frombuffer(wav.readframes(wav.getnframes()),
                            dtype=np.int16).astype(np.float32) / 32768.0

    cfg = Config.load(ROOT / "settings.json")
    states = []
    app = MurmurApp(cfg, on_state=lambda s, **k: s != "level" and states.append(s))

    print("loading model...")
    t0 = time.perf_counter()
    _ = app.stt
    print(f"model ready in {time.perf_counter() - t0:.1f}s on {app.stt.device}")

    scratch = Path(tempfile.gettempdir()) / f"murmur_probe_{os.getpid()}.txt"
    scratch.write_text("", encoding="utf-8")
    print(f"opening our own scratch file: {scratch.name}")
    proc = subprocess.Popen(["notepad.exe", str(scratch)])
    try:
        hwnd = None
        for _ in range(40):
            time.sleep(0.25)
            hwnd = find_window_by_exact_title(scratch.stem)
            if hwnd:
                break
        if not hwnd:
            print("FAIL: could not find our scratch window")
            return 1

        u.SetForegroundWindow(hwnd)
        time.sleep(0.6)
        before = read_window_text(hwnd) or ""
        print(f"target window text before: {before.strip()!r}")

        print("running the real worker path...")
        t0 = time.perf_counter()
        app._process(pcm, "hold", int(len(pcm) / 16000 * 1000))
        elapsed = time.perf_counter() - t0
        time.sleep(0.8)

        after = (read_window_text(hwnd) or "").strip()
        print(f"\nstates: {states}")
        print(f"elapsed: {elapsed:.2f}s for {len(pcm)/16000:.1f}s of audio "
              f"({len(pcm)/16000/elapsed:.1f}x realtime)")
        print(f"\nPASTED INTO THE FOCUSED APP:\n  {after!r}")

        ok = bool(after) and "three items" in after.lower()
        ratio = difflib.SequenceMatcher(
            None, REFERENCE.lower(), after.lower()).ratio() if after else 0.0
        print(f"\nsimilarity to what was spoken: {ratio:.0%}")
        print(f"{'PASS' if ok else 'FAIL'}: text reached the focused app")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(0.4)
        scratch.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
