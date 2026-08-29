# Murmur

System-wide AI dictation for Windows 11. Press a chord, talk, and cleaned-up text
appears at the cursor in whatever app has focus.

Runs entirely on your machine. No accounts, no telemetry, and no network calls at
all unless you deliberately switch to a cloud transcription backend.

---

## Using it

| Chord | What it does |
|---|---|
| **Hold `Ctrl + Win`** | Push-to-talk. Recording starts the instant both keys are down; let go and it transcribes and pastes. |
| **`Ctrl + Win + Space`** | Hands-free. Tap once, take your hands off the keyboard, talk as long as you like. Tap again to stop. |
| **`Esc`** | Cancel. Drops the recording, or aborts a transcription already running. Esc still reaches the app you are typing in. |

The two chords overlap on purpose. `Ctrl+Win+Space` contains `Ctrl+Win`, so pressing
the hands-free chord passes through the hold chord on its way. Rather than guessing
from timing, recording starts immediately on `Ctrl+Win` and is *promoted* to a
hands-free session if `Space` arrives while they are still held. Nothing you said in
those first few hundred milliseconds is lost either way.

A hands-free session stops itself after 90 seconds of continuous silence. There is no
wall-clock limit, so a long dictation is never cut off mid-sentence, but a session you
forget about cannot record all afternoon.

### The pill

A small capsule at the bottom of the screen. While Murmur is running but idle it
sits there as a **58x12 dim sliver** — enough to answer "can I dictate right
now?", not enough to notice while you work. Starting a session grows that same
element to 104x24 rather than swapping it, so the change is one continuous morph.

- **Dim sliver** — armed and waiting.
- **Blue bars** — listening. The bars track your voice.
- **Slow pulse** — listening, but hearing silence.
- **Amber dots** — transcribing, then cleaning up.
- **Green tick** — done, text pasted.
- **Red shake** — cancelled.

Labelled states size themselves to their text, so nothing sits in a half-empty
capsule. It is click-through and never takes focus, so it cannot swallow the paste.

Set `ui.idle_indicator` to `false` if you would rather see nothing between
dictations. Pausing **Listening** from the tray hides it too — the indicator is
the honest answer to whether the hotkeys are live.

### Sounds

A short rising tone when a dictation starts, a two-note resolve when the text
lands, and a falling tone if you cancel. Turn them off with `sound.enabled`.

The speakers are audible to your microphone, and Whisper invents words from
non-speech — a recording of nothing but the cue tones transcribed to "Thanks."
So capture is muted for the length of each cue plus a margin. The 400ms pre-roll
already holds everything said before the chord went down, so the mute only
covers the moment the keys are being pressed.

---

## Install

Needs **Python 3.11+**, and [Ollama](https://ollama.com) for the cleanup step.

```bash
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen2.5:7b-instruct
```

Then create the desktop shortcut:

```bash
powershell -ExecutionPolicy Bypass -File scripts/make_shortcut.ps1
```

Double-click **Murmur** on your Desktop. It starts in the system tray. Press the
shortcut again while it is running and it begins a hands-free session immediately
rather than launching a second copy.

The speech model (~1.5 GB) downloads on first run and is cached. After that Murmur
never contacts the network again.

### The microphone stays open

Murmur holds the input stream open the whole time it is running, and captures
only while a session is active.

This is not an optimisation. The audio device takes about **550ms** to deliver
its first block after being started, so opening it per session meant a short
push-to-talk captured *nothing at all* and every longer dictation lost its
opening word. Holding the stream open removes that gap entirely.

A rolling **400ms pre-roll** is also kept, so the words you say as you press the
chord are included rather than clipped. That buffer is 400ms long, lives only in
memory, is overwritten continuously, and is never written to disk or sent
anywhere.

The practical consequence: Windows will show its microphone-in-use indicator
while Murmur is running. If you would rather it did not, quit Murmur from the
tray when you are not dictating — the trade is the lost half-second on every
dictation.

### Permissions

**None.** This is the one place Windows is easier than macOS: there is no
Accessibility or Input Monitoring grant to approve. A low-level keyboard hook and
`SendInput` work for an ordinary user process.

One caveat: Windows will not let a normal process send keystrokes to an **elevated**
window. If you dictate into an app running as administrator, the text will reach the
clipboard but the paste will not land. Run Murmur elevated too, or paste manually.

---

## Tray menu

- **Listening** — pause and resume the hotkeys without quitting.
- **History…** — every dictation you have made.
- **Vocabulary…** — the words Murmur has learned from your corrections.
- **Launch at login** — on by default.
- **Quit Murmur**

---

## History

Every session is recorded, **including cancelled and failed ones**. If you forget to
paste, or a paste lands somewhere unexpected, the text is still there. Search it, copy
it, or paste it again.

Stored at `%APPDATA%\Murmur\history.db`.

## Learning your words

Fix a word in the History panel and click **Save correction**. Murmur diffs your edit
and remembers the substitution, which then feeds three places: the speech model's
hotword bias, a direct substitution on the transcript, and the cleanup model's glossary.

It also watches for corrections you make elsewhere:

- **Read-back** — after pasting, it re-reads the text from the app you pasted into and
  notices if you changed a word. Works in most apps; canvas-based editors expose
  nothing, in which case it falls back to the next route.
- **Clipboard** — if you copy a near-identical corrected version within two minutes,
  it learns from the difference.

**The learner is supervised.** A correction spotted automatically has to be seen
**twice** before it applies. A correction you make by hand is trusted immediately,
because you meant it. Every learned term is listed in the Vocabulary panel with its
hit count and a switch to turn it off. Nothing enters the active set invisibly — a
learner that quietly rewrites your words is worse than none at all.

---

## Settings

`settings.json`, beside the app. Anything you leave out keeps its default, and only
what you changed is written back, so upgrades keep reaching you.

| Key | Default | What it does |
|---|---|---|
| `stt.backend` | `local` | `local` or `cloud`. Cloud is never selected automatically. |
| `stt.local_model` | `large-v3-turbo` | Whisper model. `small` uses less VRAM. |
| `stt.device` | `cuda` | `auto`, `cuda`, or `cpu`. `auto` verifies the GPU with a real transcription before using it. |
| `polish.enabled` | `true` | Turn off to paste the raw transcript. |
| `polish.model` | `qwen2.5:7b-instruct` | Any Ollama model. |
| `polish.timeout_s` | `4` | Base timeout; scales automatically with transcript length. |
| `audio.silence_stop_seconds` | `90` | Hands-free auto-stop. |
| `audio.min_session_ms` | `350` | Shorter presses are treated as a mis-tap and discarded. |
| `clipboard.restore_previous` | `false` | `true` puts your old clipboard back after pasting. |
| `learning.enabled` | `true` | Vocabulary learning. |
| `learning.promote_after_hits` | `2` | Sightings before an automatic correction applies. |
| `sound.enabled` | `true` | Start / done / cancel tones. |
| `ui.idle_indicator` | `true` | Show the dim sliver between dictations. |
| `ui.pill_offset_px` | `48` | Height above the taskbar. |
| `autostart` | `true` | Launch at login. |

### Using a cloud backend instead

Set `stt.backend` to `"cloud"` and put a key in `.env`:

```
GROQ_API_KEY=...
# or
OPENAI_API_KEY=...
```

This is the only thing that will make Murmur talk to the internet.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Hotkey stops working after a while | The hook callback blocked and Windows silently unhooked it | Restart Murmur, then report it — the callback is supposed to do no work at all |
| Keyboard language flips when you press the chord | `Space` is not being swallowed while `Ctrl+Win` are held | Check the log for hook errors |
| Clipboard History opens instead of pasting | Modifiers were still held when `Ctrl+V` was sent | Check the log for "modifiers still reported held" |
| Text goes to the clipboard but never pastes | The target window is elevated (UIPI) | Run Murmur as administrator, or paste manually |
| Pill says "copied — press Ctrl+V" | You were still holding a modifier, so pasting would have sent Ctrl+Win+V and opened Clipboard History | The text is on your clipboard; press Ctrl+V |
| Nothing happens at login | The Run key entry is stale | Toggle **Launch at login** off and on from the tray to rewrite it |
| Transcription is slow | It fell back to CPU | Run `scripts/probe_stt.py`; if CUDA is unavailable, set `stt.local_model` to `small` |
| Nothing is transcribed | Wrong microphone | Set `audio.device` to a device index |
| Cleanup stopped working | Ollama is not running | `ollama serve`, or set `polish.enabled` to `false` |

Logs are at `%APPDATA%\Murmur\murmur.log`.

---

## Development

```bash
.venv\Scripts\python.exe -m pytest tests/ -q          # unit tests
.venv\Scripts\python.exe -m pytest tests/ -q -m live  # needs Ollama running
```

Probes in `scripts/` produce measurements rather than assertions — they are how the
defaults were chosen, and they are committed alongside the decisions they justify:

| Script | Answers |
|---|---|
| `probe_hotkey.py` | Do both chords produce the right actions? |
| `probe_pill.py` | Renders every pill state to `docs/shots/`, and checks it cannot take focus |
| `probe_pipeline.py` | End-to-end: recorded speech in, text in a real app out |
| `bench_stt.py` | Model speed and VRAM on this machine |
| `probe_panels.py` | Renders the History and Vocabulary panels |
| `make_sounds.py` | Regenerates the audio cues |
| `make_icon.py` | Regenerates the app icon |

### Layout

`murmur/platform/win/` holds everything OS-specific — the keyboard hook, paste
injection, UI Automation, autostart, single-instance. A macOS port replaces those
modules; the audio, transcription, cleanup and UI layers above them are portable.

See `CLAUDE.md` for the locked decisions and the failure modes not to reintroduce.
