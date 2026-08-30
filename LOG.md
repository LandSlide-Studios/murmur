# Murmur — ledger

Newest first. What shipped, what failed, and why.

## 2026-08-29 (night) — the comet, adapted from Sotto

Tommy found https://github.com/kingbootoshi/sotto and wanted its UI on Windows.

**License first.** Sotto is MIT, same as Murmur, so adapting it is legal with
attribution. Had it been GPL or unlicensed this would have stopped here — a
public MIT repo cannot absorb either. `NOTICE.md` records what was taken.

**What ports and what does not.** Sotto is Swift/AppKit/CoreAnimation on Apple
Silicon, running Parakeet TDT on the Neural Engine. None of that code runs on
Windows and none of it is used. What ports is the *motion design*, which is the
part that took judgement, and their README plus `CometFlight.swift` state it
exactly:

- 110ms pull-back, 260ms flight
- ease-out cubic, so it arrives rather than drifts
- elongation peaking at mid-flight — this is what makes a moving dot read as a
  comet rather than a sliding circle
- ballistic: aimed at where the pointer was the instant you stopped talking,
  never corrected

That last one is the real insight. Chasing a live cursor would read as the
animation following the user; not chasing reads as a delivery.

**The paste now fires on impact.** `Injector` split into `copy()` and `paste()`,
so the transcript reaches the clipboard before anything moves and the Ctrl+V
fires exactly as the comet lands. The animation and the paste are one event
rather than one decorating the other. If a modifier is stuck, `copy()` returns
False and the pill says "copied" instead of flying — the text is never at risk.

**Cost:** ~370ms between the transcript being ready and the text appearing. That
is the feature, not a regression; `ui.comet` turns it off.

**A false alarm worth recording.** Verification failed on "cleanup runs on
localhost" twice. It was not a code defect — Ollama had exited. Diagnosed by
running the polish call directly and reading `WinError 10061` rather than
adjusting working code to make a red check go green.

**Real fix found along the way:** `Polisher.warm()` now uses a 90s timeout. The
old warm-up used the normal adaptive timeout, so a cold model load could fail
the warm-up itself and leave the first real dictation cold — exactly what the
17:33 log line showed.

283 unit tests, 17/17 system checks.

## 2026-08-29 (evening) — vertical pill on the right bezel

Tommy reported no running-indicator at all. **It was not a bug in the feature —
his instance had launched at 13:39 and the indicator shipped at 17:55.** He was
running a build from before it existed. Worth recording because the obvious
diagnosis (the indicator is too subtle) would have led to changing working code.

Reoriented the pill to a vertical capsule against the right bezel:

| State | Size |
|---|---|
| Armed | 13 x 66, dim, breathing dots |
| Listening | 34 x 150, eleven bars |
| Hands-free | 34 x 208 |
| Transcribing / cleaning up | 34 x 126, amber dots travelling up |
| Done | 34 x 92 |

**Text labels are gone.** A vertical capsule cannot hold horizontal text, and
rotating it would be worse than useless. State is now size, colour and motion —
hands-free is simply taller than push-to-talk, which is legible peripherally in
a way an 8pt word never was.

**The waveform is deliberately exaggerated.** Modulation went from 0.72±0.42 to
0.58±0.62 and gain from 6 to 9, so neighbouring bars differ by roughly 4x at any
frame. The previous swing read as a shimmer rather than as something responding
to a voice.

**Two things the renders caught:**

1. *Bars clustered in the middle of a tall capsule*, leaving dead space at both
   ends. Spacing is now derived from the available height, so the same code
   fills a 208px hands-free pill and a 66px sliver at the right density.
2. *The armed indicator was washed out.* One painter opacity dimmed the accent
   along with the capsule. The accent is now lifted above it — checked against
   light, mid and dark backgrounds, since dark-on-dark is the worst case and a
   dark test background had been flattering it.

**Also fixed from the log:** a real dictation at 17:33 hit
`polish failed after 5.2s ... timed out`. Ollama unloads an idle model, so the
first dictation was paying the load cost inside the timeout and falling back to
the raw transcript. Both models are now warmed at startup.

263 unit tests, 17/17 system checks.

## 2026-08-29 (later still) — slimmer pill, and a persistent armed indicator

The capsule was 140x36, which read as chunky next to Wispr Flow's. Now:

| State | Was | Now |
|---|---|---|
| Idle | nothing | 58 x 12, dim |
| Recording | 140 x 36 | 104 x 24 |
| Labelled | 174 x 36 fixed | sized to the text (114-166) |

**The armed indicator is the substantive change.** Between dictations the pill
stays on screen as a small dim sliver rather than disappearing, so the question
"can I dictate right now?" always has a visible answer. It is the *same* widget,
so starting a session grows it rather than swapping graphics — the bars are the
same waveform at a smaller scale, and the springs already animate size, so the
transition came almost free. Pausing Listening from the tray hides it, which
keeps the indicator honest.

**Two defects the render caught:**

1. *The sweep trail escaped the capsule.* The transcribing dots were drawn from
   `left + 12` backwards, so the tail sat outside the pill as loose dots on the
   desktop. Everything is now clipped to the capsule path, which makes that class
   of bug impossible rather than just fixing this instance.
2. *Labelled states had a third of the capsule empty.* A fixed width cannot suit
   both "hands-free" and "copied - press Ctrl+V", so labelled states now measure
   their own text.

The pill also had **no tests at all** before this — it had only ever been checked
by looking at renders. It now has 11 covering sizing, the idle mapping, the
morph being animated rather than instant, the return to armed after every
terminal state, and the focus guards.

258 unit tests, 17/17 system checks.

## 2026-08-29 (later still) — audio cues, and scrubbed for a public repo

**Cues added:** a rising tone on start, a two-note resolve on delivery, a falling
tone on cancel. Generated by `scripts/make_sounds.py` rather than shipped as
opaque assets, with fast attack and exponential decay so they do not click.

**The non-obvious part.** The microphone is open when they play, and Whisper
invents words from non-speech: a recording of nothing but the cue tones
transcribed to **"Thanks."** Capture is now muted for each cue's duration plus a
margin.

Setting that margin honestly was harder than expected. In a room with ordinary
background noise, broadband RMS and even the cue's own frequency band are
dominated by ambient sound — three attempts to locate the cue by its 480-850Hz
signature put it at +540ms, +860ms and +1640ms, which is speech-band room noise,
not a 90ms tone. What IS proven is that the mute drops every block inside its
window (deterministic unit test). The one clean sweep showed 60ms of slack
leaking at 2.2x the floor and 400ms indistinguishable from it, so 400ms is the
margin. A silence guard in `_process` is the backstop: a capture whose RMS never
reaches half the speech threshold is not transcribed at all.

**Also noted:** the cleanup model occasionally varies how it handles a leading
clause — one run prepended "Hey Rob," and another dropped the opening entirely.
The pipeline is deterministic in isolation (3/3 identical), so this is LLM
variance at the margins rather than a regression. `verify.py` now asserts on
substance rather than exact wording.

**Scrubbed for publication.** Real client names had been used as test data.
Every one replaced with a neutral equivalent, the speech fixture regenerated, and
history squashed so the names do not survive in earlier commits.

247 unit tests, 17/17 system checks.

## 2026-08-29 (later) — second adversarial review; four critical fixes

The final review found **four critical defects that all the green gates missed**,
and its sharpest point was about the harness rather than the code.

**1. The first ~550ms of every dictation was never recorded.** The audio device
takes that long to deliver its first block after `InputStream.start()` returns —
measured 0.527–0.576s, consistent across MME, DirectSound, every blocksize and
every latency setting. So `_start` returned in 20ms, the pill said "talk", and
the microphone was not live yet. A 0.4s push-to-talk captured **zero samples**
while still clearing the 350ms threshold, producing a real session with no audio.
Fixed by opening the stream once and keeping it open, with sessions gating the
ring buffer, plus a 400ms pre-roll so words spoken *as* the chord goes down
survive. A 0.4s hold now captures 0.799s. This also removed a 2.0s UI freeze:
the silence auto-stop had been closing the device from inside the audio callback
while holding the session lock.

**2 and 3. One cancel flag for the whole process, wrong in both directions.**
Cancelling session A then starting B cleared the flag, so A's worker finished and
pasted into B's app. And cancelling B set the flag A was still checking, so a
completed dictation was discarded. Cancellation now belongs to the `Session`
object that owns it.

**4. Launch at login was broken, silently.** A Run key entry has no working
directory, so `pythonw -m murmur` died with "No module named murmur" from
System32. `pythonw` has no console, so it failed invisibly. The Run key was live
on this machine with exactly that string. Replaced with an absolute-path
launcher, and the registry entry repaired.

**Also fixed:** vocabulary substitutions cascaded (`cat->dog` plus `dog->wolf`
turned "cat" into "wolf") and doubled any term containing its own wrong form
(`vantage -> Vantage Labs -> Vantage Labs Labs`); hit counts were keyed on the right form,
so two *different* mishearings promoted each other; case-insensitive matching
rewrote ordinary words (`mark->Marc` hit "mark it down"); the clipboard watcher
learned from Murmur's own pastes, teaching a permanent wrong substitution from
two ordinary consecutive dictations; a stuck modifier caused a 500ms spin
followed by pasting anyway; the panel paste blocked the UI thread; and Esc could
not cancel a session started from the desktop shortcut.

**The point worth keeping.** `verify.py` reported 15/15 while defect 1 was live,
because the harness replaced the whole `Recorder`. A green gate over a stubbed
component proves nothing about that component. It now drives the real device and
asserts on what it actually captured — and that check is what would have caught
the worst bug in the app.

**One reviewer claim was a false positive by omission:** the pill-on-primary-
monitor issue could not be reproduced (only one display here) but was fixed
anyway, since the reasoning was sound and the fix is cheap.

237 unit tests, 2 live, 17/17 system checks.

## 2026-08-29 — Complete. All six phases built and verified.

**It works.** Real speech in, cleaned text at the cursor in whatever app has
focus. 214 unit tests, 2 live tests, and a 15/15 full-system verification that
drives the real hook, the real chord FSM, real GPU transcription, real local
cleanup and a real clipboard paste into a real Windows app, reading the result
back out via UI Automation. Only the microphone is substituted, with a recorded
fixture, because a script cannot speak.

Measured end to end: **1.57s for 14.6s of audio**, 95% match to what was spoken.

**Bugs the verification found that tests did not:**

1. *A dictation could be pasted twice.* `_stop_and_transcribe` runs on the UI
   thread (hotkey) and the audio thread (silence auto-stop), with an unguarded
   check-then-act on `_mode`. When a hands-free session auto-stopped as the user
   tapped the chord, both callers enqueued the same recording. Reproduced 60/60
   once `recorder.stop()` released the GIL, which the real PortAudio close does —
   an earlier repro with an instant stub reported 0/200 and was misleading.
2. *Cancelling wrote no history row.* `_cancel_session` never reached `_process`,
   so a cancelled session vanished entirely. "Every session is recorded" is the
   whole promise of the history panel.
3. *A case-only correction could never be learned.* The no-op guard compared
   case-insensitively, so `halvorsen -> Halvorsen` was rejected as "no change" —
   rejecting exactly the correction the feature exists for.

**Bugs looking at the pill found that assertions did not:** the bars read as a
barcode because all five moved in near-lockstep; the hands-free label collided
with them; and the checkmark faded out while it was still drawing itself on, so
the confirmation was invisible. Screenshots caught all three.

**Phase 0 adversarial review** returned 3 critical and 9 important findings.
Eleven were true positives and are fixed with regression tests — the worst being
that polish guarded only *growth*, so generation truncation silently destroyed
56% of a long dictation. One was a false positive: it accused the build of
fabricating Tommy's authorization for `autostart: true`, which he had actually
given in conversation the reviewer could not see. Its underlying point (stale
docs) was true and was fixed.

**Shipped:** `Murmur.lnk` on the Desktop with a generated multi-size icon,
system tray, History and Vocabulary panels, launch-at-login on by default.

**Not pushed.** There is no remote, and push needs Tommy's explicit go.

## 2026-08-29 — GPU verified, after a false positive

**CUDA works on the RTX 5060.** Measured on 14.6s of real speech (Windows SAPI
fixture at `tests/fixtures/speech16k.wav`):

| Model | Time | Speed | VRAM |
|---|---|---|---|
| `large-v3-turbo` | 0.33s warm | 43.9x realtime | +1200 MiB |
| `small` | 0.45s warm | 32.4x realtime | +463 MiB |

Both transcribe the fixture essentially word-perfect. `large-v3-turbo` kept as
default: faster once warm, and better on the hard audio a clean TTS fixture does
not represent.

**The failure that mattered: a false-positive GPU probe.** The first probe only
CONSTRUCTED a CUDA model, and construction succeeds even when CUDA is unusable —
CTranslate2 loads cuBLAS lazily on the first matrix multiply. Worse, the first
"successful" GPU run transcribed 10s of silence, VAD stripped every frame, zero
segments were encoded and no GPU math ever ran. It reported 0.19s and looked
like proof. It was not.

Two fixes:
1. The probe now runs a real inference on non-silent audio with VAD off.
2. `os.add_dll_directory` alone is not enough for the pip-installed CUDA libs —
   CTranslate2's lazy cuBLAS load uses a plain `LoadLibrary` that ignores the
   user-dirs list. The directories must also go on `PATH`.

**VRAM is the live constraint.** Ollama holds qwen2.5:7b resident at ~4.7GB of
8.1GB. With `large-v3-turbo` at +1.2GB plus the desktop compositor, the card sits
near ~6.9GB. It fits, but a load can fail under pressure, so `transcribe()` now
catches a GPU failure mid-session, rebuilds on CPU and retries once. Losing a
dictation is not an acceptable outcome.

**Also fixed.** faster-whisper contacted HuggingFace on every model load even
when cached, which broke the offline guarantee; it now loads `local_files_only`
first and only downloads when genuinely absent.

**Built.** Tasks 1-5 and 8: config, local STT, polish, audio ring buffer, chord
FSM, silence monitor. 78 unit tests plus 2 live tests. The chord FSM surfaced a
bug the plan missed: Windows auto-repeats keydown for held keys, so a session
ending while Ctrl+Win were still down restarted immediately. Latched until the
chord is fully released.

## 2026-08-29 — Spec, plan, and environment truth

**Decided.** Windows 11, not macOS. The prompt said macOS but `Wispr Flow.lnk` was dated the
same morning, the named chord contains the Windows key, and there is no Swift toolchain on
this box. Platform code isolated under `platform/` so a Mac port stays cheap.

**Decided.** Two chords, not one with a double-tap. `Ctrl+Win` held is push-to-talk;
`Ctrl+Win+Space` is the hands-free toggle. The containment problem (the toggle chord passes
through the hold chord) is solved by promotion, not a timing window — recording starts on
`Ctrl+Win` and converts to a toggle session if `Space` lands while they are still held.

**Measured.** Polish model benchmark, 4 candidates on real dictation samples:

| Model | p50 | Outcome |
|---|---|---|
| `qwen2.5:7b-instruct` | 583ms | **Chosen** |
| `auto-variable-2b` | 2309ms | Equal quality, 4x slower |
| `phi3.5` | 534ms | Rejected — appended meta-commentary |
| `deckard-4b` | 5047ms | Rejected — empty output |

**Measured — and this one changed the design.** The first prompt lost content. Given
*"hey can you add three items to the list first one is…"* both fast models **obeyed** the
transcript and emitted only a list, dropping the opening clause. Fixing it by demanding every
word be kept then broke punctuation entirely. v3 (delimited transcript + one worked example)
passes both. The few-shot example is load-bearing.

**Installed.** venv with PySide6 6.11.2, faster-whisper 1.2.1, ctranslate2 4.8.1,
sounddevice 0.5.6, comtypes 1.4.16.

**Pending.** faster-whisper CUDA probe on the RTX 5060 (Blackwell, sm_120) — plan Task 2.
No GPU claim until an observed run.

**Not started.** No implementation code. Awaiting queue approval.
