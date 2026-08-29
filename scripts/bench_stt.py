import sys, time, wave, logging, warnings, subprocess
from pathlib import Path

# Running a script from scripts/ puts scripts/ on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
logging.disable(logging.INFO); warnings.filterwarnings("ignore")
from murmur.stt.local import LocalTranscriber

model, device = sys.argv[1], sys.argv[2]

def vram():
    try:
        return int(subprocess.run(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],
                                  capture_output=True,text=True).stdout.strip().split("\n")[0])
    except Exception:
        return -1

with wave.open("tests/fixtures/speech16k.wav","rb") as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768.0
dur = len(pcm)/16000
base = vram()
tr = LocalTranscriber(model=model, device=device)
tr.transcribe(pcm[:16000], hotwords=[])
peak = vram()
t = time.perf_counter(); out = tr.transcribe(pcm, hotwords=[]); el = time.perf_counter()-t
print(f"{model:<16} {device:<4} {el:6.2f}s {dur/el:5.1f}x  vram+{peak-base:>4}MiB")
print(f"    {out}")
