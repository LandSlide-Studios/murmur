# Murmur — agent context

Folder-scoped rules for a system-wide dictation app. Read `LOG.md` first, then `PLAN.md`.

## Purpose

Press a chord, talk, and cleaned text appears at the cursor in whatever app has focus.
Rebuild of Wispr Flow's behaviour on a local-only stack. Tommy's own tool, not client work.

## Constraints (MUST NOT)

- **Never break the offline guarantee.** STT and polish are local. Cloud STT exists but is
  opt-in via `stt.backend = "cloud"` and is never selected automatically, never as a fallback.
- **Never add telemetry, analytics, accounts, or auto-update.** No outbound request the user
  did not configure.
- **Never open the microphone per session.** The device needs ~550ms to deliver
  its first block, so a short push-to-talk captured zero samples and every
  dictation lost its opening word. The stream stays open; sessions gate the ring
  buffer. Closing it from inside the audio callback also froze the UI for 2s.
- **Never share one cancel flag across sessions.** Cancellation belongs to the
  `Session` object that owns it. A process-wide Event leaked cancelled text into
  the next app AND discarded completed dictations.
- **Never put `-m murmur` in the Run key.** A Run entry has no working directory;
  it must invoke `run_murmur.pyw` by absolute path.
- **Never substitute a whole component in `verify.py`.** The harness stubbed the
  Recorder and was blind to the worst defect in the app while reporting 15/15.
- **Never change the comet's timings without reading NOTICE.md.** 110ms pull,
  260ms flight, mid-flight stretch and a ballistic aim are adapted from Sotto
  and are what make it read as a throw. The aim must never be re-read
  mid-flight.
- **The transcript reaches the clipboard BEFORE the comet flies.** A failed
  animation must never be able to cost the user their words.
- **Never gate an active recording.** `Recorder.mute_for()` touches the
  pre-roll only. It once gated the capture buffer to keep a cue out of the
  audio, and cost every dictation its opening words — short utterances sent
  nothing at all. No cue is worth a syllable of the user's speech.
- **Ctrl+Win is a Windows shortcut prefix.** Ctrl+Win+D, Ctrl+Win+arrow and
  Ctrl+Win+F are real system shortcuts. A key other than Ctrl/Win/Space/Esc
  joining a HOLD discards the session. Hands-free must never do this:
  talking while typing is the point of it.
- **Never open a real audio device in a unit test.** A leaked PortAudio
  stream crashes the interpreter at exit and swallows pytest's summary.
- **Never do work inside the keyboard hook callback.** A `WH_KEYBOARD_LL` callback exceeding
  `LowLevelHooksTimeout` (~300ms) is silently unhooked by Windows and the hotkey dies with no
  error. The callback enqueues and returns. Nothing else.
- **Never swallow `Esc`.** It is watched passively and always passed through, so it still
  reaches the app the user is typing in. The only key ever suppressed is `Space`, and only
  while `Ctrl+Win` are held.
- **Never paste without releasing held modifiers first.** `Ctrl+V` sent while `Ctrl+Win` are
  still physically down reads as `Ctrl+Win+V` and opens Clipboard History instead of pasting.
- **Never let the pill take focus.** `WS_EX_NOACTIVATE` + `Qt.Tool` + `WA_ShowWithoutActivating`
  + click-through. If it activates, the paste lands in the pill.
- **Never let polish lose the user's words.** On timeout, runaway length, or empty output, inject
  the raw transcript. Losing a dictation is worse than an unpolished one.
- **Never simplify the polish prompt.** Two earlier versions failed measurably — see `LOG.md`.
  The delimiter and the worked example are both load-bearing.
- **The pill's click-through is dynamic, not fixed.** `WS_EX_TRANSPARENT` is
  removed only while the tick/cross are on screen and restored the moment they
  leave. `WS_EX_NOACTIVATE` stays on throughout — the pill takes clicks without
  ever taking focus, or the paste lands in the pill.
- **One session writes exactly one history row.** The silence guard used to write
  its own row and then return, while the `finally` block wrote a second one
  claiming success with no text in it.
- **Never judge "is there speech in this recording" with a mean.** The whole-clip
  average falls as a dictation gets longer, because pauses count against it. A
  42.4s dictation he spoke all the way through was discarded as silent. Use
  `peak_rms` — the question is a maximum, not an average.
- **Never hardcode a copy of `audio.speech_rms_threshold`.** `pill.py` carried its
  own `0.012` and it drifted above his speaking voice, which silently stopped the
  meter animating at all.
- **Calibrate levels against HIS microphone, not a close mic.** eMeet C96 at arm's
  length: ambient floor 0.00086, speaking voice ~0.008. Three separate constants
  were set above his speaking voice — `speech_rms_threshold` at 0.012, the
  meter's `_REFERENCE` at 0.06, and `_PEAK_MIN` at 0.020 — and each one made the
  app behave as though he were not talking.
- **Screenshots must be rendered at real microphone levels.** `probe_pill.py` drove
  0.30 where his voice is 0.008. Every meter review was done against a saturated
  picture, which is why it passed inspection and failed in his hand.
- **Never claim GPU acceleration, or any verification, without an observed run.**

## Locked decisions

Dated. Do not re-litigate without a reason that is new.

- **2026-08-29 · Windows 11, not macOS.** Evidence in `LOG.md`. Platform code stays behind
  `platform/` so a Mac port swaps modules rather than rewriting the app.
- **2026-08-29 · Two chords.** `Ctrl+Win` held = push-to-talk. `Ctrl+Win+Space` = hands-free
  toggle. Resolved by promotion, not a timing window.
- **2026-08-29 · Auto-stop is silence-based (~90s continuous), toggle sessions only.** No wall
  clock cap. In hold mode the finger is the stop.
- **2026-08-29 · Polish model `qwen2.5:7b-instruct`**, measured p50 583ms against three
  alternatives. Prompt v3.
- **2026-08-29 · Learning is supervised.** An automatic correction needs two observations before
  it applies; a manual edit is trusted immediately. Every term is visible and disableable.
  An unsupervised learner that quietly corrupts transcripts is worse than none.
- **2026-08-29 · No gradient, no glow in the pill.** Checked against the design library:
  gradient refused by 100 critiques, glow by 19. Motion and one accent carry it.

## Conventions

- TDD per `PLAN.md`: failing test, run it, minimal implementation, run it, commit.
- Thread boundaries are the architecture. Hook thread enqueues only. Audio thread never touches
  Qt. UI thread never blocks on STT or HTTP. Marshal to the UI thread with `QTimer.singleShot`.
- Every session writes a history row, including cancelled and failed ones.
- Probe scripts live in `scripts/` and print measurements. When a decision rests on a number,
  the script that produced it is committed alongside.
- Run tests: `.venv/Scripts/python.exe -m pytest tests/ -v`. Live tests needing Ollama are
  marked `@pytest.mark.live`.

## State

**Complete and in daily use.** 342 unit tests, 2 live tests, 17/17 system checks
via `scripts/verify.py`. Desktop shortcut, tray and launch-at-login in place.

Two adversarial review passes have run against this code; their findings are fixed
and pinned with regression tests. Do not undo a guard without reading why it exists —
`LOG.md` records what each one caught.

Public at github.com/LandSlide-Studios/murmur (MIT). The remote exists, which makes
the rule sharper rather than looser: push needs Tommy's explicit go per change set.
