"""Compare cleanup models on Tommy's OWN dictations.

Answers one question: is a smaller model good enough to stop Whisper and the
polisher competing for an 8GB card?

Three things are measured, because all three have already gone wrong here:

  latency      -- what he waits for after speaking.
  retention    -- fraction of the raw transcript's content words that survive.
                  An adversarial pass once caught the polisher deleting 56% of
                  a long dictation. Fillers are excluded, since removing those
                  is the job.
  compliance   -- whether the model returns ONLY the cleaned text. A model that
                  answers the dictation, or prefixes "Here's the cleaned
                  version:", is unusable no matter how fast it is.

Run:  .venv/Scripts/python.exe scripts/bench_polish.py
"""

import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from murmur.polish import OLLAMA_URL, PROMPT  # noqa: E402

CANDIDATES = [
    ("qwen2.5:7b-instruct", "4.7 GB", "current"),
    ("deckard-4b", "2.7 GB", ""),
    ("phi3.5:latest", "2.2 GB", ""),
    ("auto-variable-2b", "1.3 GB", ""),
]

# Removing these is the whole point of the cleanup, so they must not count
# against retention.
FILLERS = {
    "um", "uh", "erm", "ah", "like", "so", "basically", "actually", "really",
    "just", "yeah", "okay", "ok", "right", "well", "kinda", "sorta", "gonna",
    "i", "mean", "you", "know", "a", "the", "and", "of", "to", "that", "it",
    "is", "was", "in", "on", "s", "t", "we", "my", "me", "be", "have", "do",
}

# Telltales of a model talking ABOUT the task instead of doing it.
PREAMBLE = re.compile(
    r"^\s*(here\s+is|here's|sure|certainly|cleaned|corrected|revised|"
    r"okay,?\s+here|i've|i have|below is|the cleaned)", re.I)


def words(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in FILLERS]


def retention(raw, out):
    src = words(raw)
    if not src:
        return 1.0
    kept = set(words(out))
    return sum(1 for w in src if w in kept) / len(src)


def compliant(raw, out):
    if not out.strip():
        return False, "empty"
    if PREAMBLE.match(out):
        return False, "preamble"
    if out.strip()[0] in "\"'" and out.strip()[-1] in "\"'":
        return False, "wrapped in quotes"
    if len(out) > len(raw) * 1.6:
        return False, "added commentary"
    return True, ""


def call(model, raw, timeout_s=90):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": PROMPT},
                     {"role": "user", "content": raw}],
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.0, "num_predict": 4096},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        out = json.loads(r.read())["message"]["content"].strip()
    return out, (time.monotonic() - t0) * 1000


def samples(limit=8):
    db = os.path.join(os.environ["APPDATA"], "Murmur", "history.db")
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT raw_text FROM sessions WHERE raw_text IS NOT NULL "
        "AND length(raw_text) > 80 ORDER BY length(raw_text) DESC LIMIT ?",
        (limit,)).fetchall()
    con.close()
    return [r[0] for r in rows]


def main():
    texts = samples()
    if not texts:
        print("no dictations long enough to judge on; talk to it a few times first")
        return
    print(f"{len(texts)} real dictations, {sum(len(t) for t in texts)} chars total\n")

    print(f"{'model':<24}{'size':<9}{'p50':>8}{'p95':>8}{'retention':>11}"
          f"{'compliant':>11}")
    print("-" * 71)
    for model, size, note in CANDIDATES:
        lat, ret, bad = [], [], []
        try:
            call(model, "warm up", timeout_s=180)      # load it before timing
        except Exception as e:
            print(f"{model:<24}{size:<9}{'unavailable — ' + str(e)[:30]:>38}")
            continue
        for raw in texts:
            try:
                out, ms = call(model, raw)
            except Exception as e:
                bad.append(type(e).__name__)
                continue
            lat.append(ms)
            ret.append(retention(raw, out))
            ok, why = compliant(raw, out)
            if not ok:
                bad.append(why)
        if not lat:
            print(f"{model:<24}{size:<9}{'all calls failed':>38}")
            continue
        p50 = statistics.median(lat)
        p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]
        flag = "  <- " + note if note else ""
        print(f"{model:<24}{size:<9}{p50:>7.0f}m{p95:>7.0f}m"
              f"{statistics.mean(ret) * 100:>10.1f}%"
              f"{(len(lat) - len(bad)) / len(lat) * 100:>10.0f}%{flag}")
        if bad:
            print(f"{'':<33}failures: {', '.join(sorted(set(bad)))}")
    print("\nretention = content words kept (fillers excluded). A cleanup that")
    print("drops content is the failure mode that already bit us once.")


if __name__ == "__main__":
    main()
