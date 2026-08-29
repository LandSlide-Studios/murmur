# Murmur — system-wide AI dictation for Windows 11

**Date:** 2026-08-29
**Status:** Approved for planning
**Owner:** Tommy Maglietto / Landslide Studios
**Target:** Windows 11 Pro (26200), NVIDIA RTX 5060 8GB, 16 cores, 32GB RAM

---

## 1. What this is

A background dictation app. Press a chord, talk, and cleaned-up text appears at the cursor in
whatever app has focus. Runs fully offline. No accounts, no telemetry, no network calls unless
the user explicitly flips a config flag to a cloud STT provider.

Reference product is Wispr Flow (trialled by the user 2026-08-29). This is not a clone of its
code; it is a rebuild of its behaviour on a local-only model stack.

### Platform decision

The original request said macOS. The user is on Windows 11, named the Windows key explicitly,
and has no Swift/Xcode toolchain on this machine. **Windows is the build target.** Platform-specific
concerns (hotkey hook, paste injection, tray, autostart) are isolated behind `platform/` so a
macOS port later replaces four modules rather than the app.

---

## 2. Hotkeys

Two distinct chords with different hand semantics.

| Chord | Mode | Behaviour |
|---|---|---|
| `Ctrl + Win` (held) | **Hands-on / push-to-talk** | Recording starts the moment both are down. Releasing either key stops, transcribes, pastes. |
| `Ctrl + Win + Space` | **Hands-off / toggle** | Tap to start. Hands come off the keyboard entirely. Tap again to stop, transcribe, paste. |
| `Esc` | **Cancel** | Drops the recording, or aborts an in-flight transcription. Always passed through to the focused app. |

### The containment problem

`Ctrl+Win+Space` contains `Ctrl+Win`. Pressing the toggle chord necessarily passes through the
hold chord. Resolved by **promotion, not by a timing window**:

- `Ctrl+Win` down: start capturing immediately, so push-to-talk has zero perceived latency
- `Space` arrives while both are still held: **promote** the live session to toggle mode. The
  buffer is kept; only the stop condition changes
- Release without `Space`: it was push-to-talk, so stop and transcribe

No grace period, no dropped leading audio, no guessing at intent from timing.

### State machine

```
IDLE
 |- Ctrl+Win down --------------> REC_HOLD  (capture starts)

REC_HOLD
 |- Space down -----------------> REC_TOGGLE   (promoted; release no longer stops)
 |- Ctrl|Win up, elapsed<350ms -> IDLE         (discard: accidental tap)
 |- Ctrl|Win up, elapsed>=350ms-> TRANSCRIBING

REC_TOGGLE
 |- Ctrl|Win up ----------------> REC_TOGGLE   (no-op: hands are off)
 |- Ctrl+Win down again --------> REC_TOGGLE[armed_for_stop]
 |- [armed] Space down ---------> TRANSCRIBING
 |- silence >= 90s -------------> TRANSCRIBING

TRANSCRIBING -> POLISHING -> INJECTING -> IDLE

ANY STATE + Esc down ----------> CANCELLED -> IDLE
```

`armed_for_stop` exists so that pressing the toggle chord to STOP a session is not read as
`Ctrl+Win` starting a new one.

### Auto-stop

Silence-based, not wall-clock. **Applies to toggle sessions only** — in hold mode the user's finger
is the stop condition and silence is irrelevant.

The timer measures *continuous* silence since the last frame whose RMS exceeded the speech
threshold, and resets on every such frame. Roughly 90s of it ends the session. No hard cap: a long
dictation is never guillotined mid-sentence, but a forgotten session cannot run all afternoon.
Threshold and duration live in settings.

---

## 3. Pipeline

```
mic -> ring buffer -> [VAD] -> STT -> vocabulary substitution -> LLM polish -> inject -> history
                                |                                    |
                           hotwords <------- vocabulary store -------+ glossary
```

### 3.1 Capture — `audio.py`

`sounddevice` at 16kHz mono float32 into a pre-allocated numpy ring buffer. Emits RMS level at
60Hz for the waveform. Capture runs on its own thread; the UI never blocks it and it never blocks
the UI.

### 3.2 STT — `stt/`

`Transcriber` protocol: `transcribe(pcm: np.ndarray, hotwords: list[str]) -> str`.

| Backend | Model | Notes |
|---|---|---|
| `local` (default) | faster-whisper `large-v3-turbo`, int8 | ~1.5GB VRAM. More accurate AND faster than the `small`/`medium` originally specced. |
| `local` fallback | faster-whisper `small`, int8, CPU | 16 cores available. Used when CUDA is unavailable. |
| `cloud` | Groq `whisper-large-v3` / OpenAI | Only when `stt.backend = "cloud"` AND a key exists in `.env`. Never automatic. |

**Blackwell risk — must be verified, not assumed.** The RTX 5060 is sm_120. CTranslate2 CUDA
wheels may not ship sm_120 kernels. The build must *run* a GPU transcription and confirm it,
falling back to CPU int8 otherwise. No claim of GPU acceleration without an observed run.

### 3.3 Polish — `polish.py`

Ollama HTTP at `127.0.0.1:11434`. No API key, no account, works offline. This is what makes the
"no accounts / offline" requirement literally true rather than aspirational.

Available locally: `auto-variable-2b` (1.9B), `phi3.5` (3.8B), `qwen2.5:7b-instruct`,
`deckard-4b`, `cold-fusion-9b`, `heretic-instruct-9b`, `glm-flash-21b`, `nous-hermes:13b`,
`dolphin-llama3`, `dolphin-llama3:70b`.

**Model choice is a benchmark, not an opinion.** This step sits between the user finishing a
sentence and text appearing, so p50/p95 latency decides it. Candidates `auto-variable-2b`,
`phi3.5` and `qwen2.5:7b-instruct` are timed on identical real transcripts; the fastest that
clears a quality bar wins. `dolphin-llama3:70b` is excluded — 39GB against 8GB VRAM is unusable
here.

Prompt: strip filler words, fix punctuation and capitalisation, preserve meaning and the user's
own wording, never answer or continue the text, never add content. Learned vocabulary is injected
as a glossary. Output is the corrected text only.

Guardrails: if polish output exceeds the raw transcript by more than 40% in length, or the model
times out (>4s), the raw transcript is injected instead. A polish step is never allowed to lose
the user's words.

### 3.4 Inject — `inject.py`

Clipboard plus `Ctrl+V` via `SendInput`.

Order of operations. Every step exists because of a specific failure:

1. Snapshot the existing clipboard (text and format list)
2. **Force-release held modifiers.** Sending `Ctrl+V` while `Ctrl+Win` is still physically down
   reads as `Ctrl+Win+V` and opens Clipboard History instead of pasting. Synthesize keyup for
   Win/Ctrl/Shift/Alt and confirm via `GetAsyncKeyState` before proceeding.
3. Set the clipboard, wait ~60ms. Targets read a stale clipboard if pasted immediately.
4. `SendInput` Ctrl+V
5. Per the `clipboard.restore_previous` flag: leave the transcript on the clipboard (default) or
   restore the prior contents after ~300ms

---

## 4. History — every dictation is kept

**Requirement:** if the user forgets to paste it, or the paste lands in the wrong place, the text
is still in the app.

SQLite at `%APPDATA%\Murmur\history.db`:

| Column | Purpose |
|---|---|
| `id`, `ts`, `duration_ms` | Identity and timing |
| `raw_text` | Straight from STT, before any processing |
| `polished_text` | After the LLM |
| `final_text` | What was actually injected |
| `mode` | `hold` or `toggle` |
| `target_app`, `target_window_title` | Where it went |
| `corrected_text` | Populated if the user later edits it; feeds learning |

Every session writes a row, **including cancelled and failed ones**, so a crashed transcription
never silently costs the user their words. History panel: searchable list, click to copy, click to
re-paste, edit in place. Retention unlimited by default, with a configurable cap.

---

## 5. Vocabulary learning

**Requirement:** "if I replace the words after I paste it, it should learn that."

### Capture paths, most to least reliable

**A. Manual edit — guaranteed.** The user edits a transcript in the history panel. Diff old to new,
propose the substitution. Always works.

**B. UI Automation read-back — best-effort, primary automatic path.** After injecting, grab the
focused control's UIA `TextPattern` and snapshot its text. Re-read at T+20s and T+90s, then diff
the region that was inserted. Works for Win32 edits, RichEdit, UWP, and Chromium/Electron apps
with accessibility enabled (VS Code, Slack, Discord, browsers). Does not work everywhere —
canvas-rendered editors expose nothing. Degrades silently to path C.

**C. Clipboard similarity — fallback.** If within 120s the user copies text that is at least 60%
similar (difflib ratio) to what was injected, treat it as a corrected version and diff it.

### Store and application

`vocab.db`: `term`, `wrong_forms` (json), `hit_count`, `first_seen`, `last_seen`, `enabled`.

A correction is promoted to the active vocabulary only after being seen **twice**, or immediately
when it came from path A where intent is explicit. This stops a one-off typo from permanently
corrupting transcripts.

Learned terms are applied in three places:

1. `hotwords` / `initial_prompt` to faster-whisper, biasing decoding toward the user's vocabulary
2. A deterministic substitution map applied to raw STT output
3. A glossary injected into the polish prompt

**The learner is supervised.** Tray > *Vocabulary* opens the list, showing each entry's hit count
with an enable/disable toggle. An unsupervised learner that quietly corrupts transcripts is worse
than no learner at all, so nothing enters the active set invisibly.

---

## 6. The pill

Small and sleek, matching the Wispr Flow silhouette. **Roughly 140x36px**, bottom-centre, ~48px
above the taskbar. Frameless, translucent, always-on-top, click-through.

**Critical:** `WS_EX_NOACTIVATE` plus `Qt.Tool`, `WA_ShowWithoutActivating` and
`WA_TransparentForMouseEvents`. If the pill takes focus, the paste lands in the pill instead of
the user's app.

### Visual language

Checked against the design library before speccing: **gradient is refused by 100 independent
critiques and glow by 19** — both library-wide patterns rather than one page's taste. So no
gradient-and-glow decoration. Motion and a single accent colour carry the design; the waveform is
the visual interest and needs nothing behind it.

Solid near-black capsule (`#0E0E10` at 92% opacity), 18px radius, 1px `#FFFFFF14` hairline, and one
accent per state: `#5B8DEF` listening, `#E8B84B` transcribing, `#4ADE80` done, `#EF4444` cancelled.

### Animation

One 60fps `QTimer` driving one critically-damped spring integrator. Every state change retargets
the springs rather than starting a new animation, so an interrupted transition **blends** instead
of snapping. This is the entire difference between fancy and janky.

| State | Motion |
|---|---|
| Materialize | Spring scale 0.8 to 1.0, rise 12px, fade in, on `cubic-bezier(0.16, 1, 0.3, 1)` — the expo-out curve harvested from the design library |
| Listening | 5 bars driven by real RMS, each with its own spring damping so they overshoot and settle like physical objects rather than a bar chart |
| Silence | Bars collapse to a slow breathing line at ~0.5Hz |
| Mode promote (hold to toggle) | Pill morphs width on the same spring, label cross-fades |
| Transcribing | Bars dissolve into a single travelling highlight. Shimmer survives only where it means "working" |
| Done | Pill contracts, checkmark strokes itself on via dash-offset, dissolves |
| Cancelled | 3-cycle 60ms horizontal shake, then fade |

The waveform is hand-rolled with QPainter. No external animation asset and no network dependency,
which keeps the offline guarantee intact.

---

## 7. Tray and desktop launcher

Tray menu: Enabled toggle, History…, Vocabulary…, Settings…, Launch at login, Quit.

`Murmur.lnk` on the Desktop with a real icon. Not running: launches to tray. Already running:
signals the live instance to start a dictation session immediately, via a single-instance mutex
plus a named pipe.

Launch at login: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, toggled from the tray.

---

## 8. Module layout

Each module is independently testable and has one job.

```
murmur/
  app.py            wiring plus the state machine
  config.py         settings.json and .env, defaults, hot-reload
  audio.py          capture, ring buffer, RMS stream
  vad.py            silence detection
  stt/base.py       Transcriber protocol
  stt/local.py      faster-whisper, CUDA probe and CPU fallback
  stt/cloud.py      Groq / OpenAI
  polish.py         Ollama client, prompt, guardrails
  vocabulary.py     learning store, promotion rules, hotwords and glossary
  corrections.py    capture paths A, B and C
  history.py        SQLite store and queries
  inject.py         clipboard, modifier release, paste, restore
  platform/win/hotkey.py     WH_KEYBOARD_LL hook, chord FSM, suppression
  platform/win/uia.py        UI Automation read-back
  platform/win/autostart.py  registry Run key
  ui/pill.py        overlay window
  ui/waveform.py    QPainter bars
  ui/motion.py      spring integrator and easing
  ui/tray.py        tray icon and menu
  ui/history_win.py history panel
  ui/vocab_win.py   vocabulary panel
```

---

## 9. Known failure modes designed around

1. **`LowLevelHooksTimeout`.** A `WH_KEYBOARD_LL` callback taking more than ~300ms is silently
   unhooked by Windows. The hotkey simply stops working, with no error. The hook thread therefore
   does nothing but push to a queue; all work happens elsewhere.
2. **`Win+Space` is the input-language switcher.** The hook must swallow `Space` while `Ctrl+Win`
   are held, or every toggle flips the keyboard language.
3. **A lone `Win` keyup opens Start.** Holding Ctrl normally suppresses this, but if the user
   releases Ctrl before Win, Start pops. Mitigated by injecting a benign VK before the Win keyup
   lands, cancelling Start's lone-Win tracking.
4. **Held modifiers poison the paste.** See 3.4 step 2.
5. **A pill that steals focus breaks the paste.** See section 6.
6. **sm_120 / Blackwell CUDA support.** See 3.2.
7. **Esc must never be swallowed.** It is watched passively and always passed through, so it still
   reaches the app the user is typing in.

---

## 10. Config

`settings.json` beside the app:

```json
{
  "hotkeys": { "hold": "ctrl+win", "toggle": "ctrl+win+space", "cancel": "esc" },
  "audio":   { "device": null, "silence_stop_seconds": 90, "min_session_ms": 350 },
  "stt":     { "backend": "local", "local_model": "large-v3-turbo", "device": "auto", "language": "en" },
  "polish":  { "enabled": true, "provider": "ollama", "model": "BENCHMARK_WINNER", "timeout_s": 4 },
  "clipboard": { "restore_previous": false },
  "learning":  { "enabled": true, "promote_after_hits": 2, "uia_readback": true },
  "ui":        { "pill_position": "bottom-center", "pill_offset_px": 48 },
  "autostart": false
}
```

`.env`, optional and only for cloud backends: `GROQ_API_KEY`, `OPENAI_API_KEY`.

---

## 11. Out of scope

Multi-language switching, custom wake words, streaming partial results, speaker diarisation,
mobile, cloud sync, and any form of telemetry.

---

## 12. Done means

- Both chords work in a real third-party app (Notepad, VS Code, a browser) — observed, not assumed
- Text lands at the cursor with modifiers correctly released
- Esc cancels and still reaches the focused app
- The pill renders at 60fps and never takes focus, verified by screenshot
- Every session appears in history, including failures
- A manual correction promotes to vocabulary and changes a later transcription
- The whole pipeline works with the network off
- `preflight.mjs` returns 0 FAIL
