# Murmur — ledger

Newest first. What shipped, what failed, and why.

## 2026-08-30 (evening) — quick taps were being thrown away

"If I release too quickly it doesn't work." Measured rather than guessed, and
two separate causes fell out.

**1. Anything under 350ms was silently discarded.** `min_session_ms` existed to
reject an accidental brush, but 350ms is longer than a deliberate quick press. A
fast tap-and-talk produced no session, no pill, no feedback of any kind.

**2. Windows shortcuts sharing the chord were being transcribed.** Ctrl+Win is
the prefix of `Ctrl+Win+D` (new virtual desktop), `Ctrl+Win+arrow` (switch
desktop) and `Ctrl+Win+F`. Every one of them produced START_HOLD then
STOP_AND_TRANSCRIBE — so switching virtual desktops recorded whatever the
microphone caught and pasted it. Never reported, certainly experienced.

The two were linked: the high threshold was partly compensating for the second
problem. Fixing the real cause let the threshold drop.

- A key other than Ctrl/Win/Space/Esc joining a hold now DISCARDS the session:
  the user is reaching for a shortcut, not dictating. Hands-free is untouched,
  because talking while typing is the entire point of it.
- `min_session_ms` 350 -> 120. With the 400ms pre-roll, even a 120ms hold
  carries over half a second of audio, and a genuinely empty capture is caught
  by the silence guard.

**Latency was never the problem.** Measured keypress to microphone-capturing
over six trials: median 11.7ms, worst 16.8ms, bounded by the 16ms UI tick — plus
400ms of pre-roll from before the key went down.

**Also fixed:** a test added this session called `Recorder.begin()`, which opens
a real input device. Leaving a PortAudio stream open crashed the interpreter at
exit and swallowed pytest's own summary line, so the suite could not report its
own result.

302 unit tests, 17/17 system checks.

## 2026-08-30 (later) — "hold-to-talk sometimes does not send": found it

Tommy narrowed the report to hold-to-talk specifically, and that was the clue
that cracked it. **It was a regression introduced with the audio cues.**

To keep the start cue out of the recording, `mute_for()` gated the capture
buffer for the cue's length plus 400ms of slack — about 490ms from the moment
the chord went down. Which is exactly when people start talking.

Measured, not guessed. Feeding the fixture through the real pipeline:

| You hold | You said | Murmur got |
|---|---|---|
| 1.0s | "Hey, can you add three?" | "You add three" |
| 2.0s | "Hey can you add three items to the list?" | "You add three items to the list." |

Every dictation lost its opening words. And a short utterance — the whole point
of push-to-talk — fell entirely inside the muted window, transcribed to nothing,
and sent nothing at all. Hold-to-talk phrases are short, which is why it showed
up there and not in hands-free.

**The trade was wrong in both directions.** The cue is a quiet 90ms tone that
real speech buries, and the only case it could corrupt is a recording of nothing
but the cue — which the silence guard in `_process` already catches. Dropping
the user's words to protect against a hallucination on an empty clip is a bad
bargain.

`mute_for()` now gates only the PRE-ROLL, so a session's done or cancel cue
still cannot leak into the next session's opening, and an active recording is
never gated. Verified with the cue mixed into the audio at the level a
microphone actually picks it up: "Hey, can you add three items to the list?"
comes back whole.

The 400ms slack was itself a symptom of an earlier honest failure: the output
latency could not be measured in a room with background noise, so a generous
margin was chosen. Generous margins on the wrong side of a trade-off are how
this happened.

295 unit tests, 17/17 system checks.

## 2026-08-30 — sound packs, orb charge, and why dictations "did not send"

**The "sometimes it does not send" report was diagnosed, not guessed at.**
Tommy's `history.db` had not been written since the previous afternoon, yet the
log showed nine dictations that morning. The running instance turned out to be
hours behind the working tree, and its log had no startup block at all — so
there was no way to tell which build was producing the failures.

Two gaps, both now closed:

1. **The build is stamped at startup.** `Murmur 19cf9ca+dirty ready`. A running
   instance can be hours old, and "does it send?" is unanswerable without
   knowing what is running.
2. **Every dictation writes a delivery receipt** — pasted, or clipboard only,
   with the reason. A successful session and a silently undelivered one used to
   log the same thing: nothing.

After restarting on the current build the log immediately showed
`cleanup model ready`, a line that had never appeared before. That is the actual
cause of the nine failures: the old build's warm-up used the normal adaptive
timeout, so a cold Ollama load failed the warm-up itself and left every
dictation paying the load cost inside its own timeout.

**Six sound packs** ported from Sotto, switchable from the tray, playing on
selection so the choice is made by ear.

The porting mistake worth recording: the first version **normalised every file
to the same peak**, which flattened the entire design — "Breath — almost silent"
came out exactly as loud as "Heartbeat — pulses you feel". Sotto renders each
cue at its designed gain behind a 0.9 master. Caught by a test asserting breath
should be quieter than heartbeat, which existed only because their descriptions
made the claim checkable. Measured after the fix: breath ack 0.007 RMS,
heartbeat arrive 0.097.

**The orb charge.** While the model works the capsule contracts to a glowing
orb that breathes, rather than staying a waveform. The shape has to be one that
cannot be mistaken for "still listening".

294 unit tests, 17/17 system checks.

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

## 2026-08-30 — pill controls, tighter meter, and two real bugs behind it

**Asked for:** bars closer together and taller, a smaller pill, a checkmark and an
X on it, and a way to find past dictations.

**The meter.** 15 bars at a 1.4px gap. The old spacing let them read as separate
indicators; packed this tight they read as one waveform, which is the thing that
actually communicates "it is hearing you." Capsule down to 30×138 recording,
11×52 armed.

**The controls.** Tick at the top (stop and paste), cross at the bottom (discard).
These are the first pixels in Murmur that accept a click, and a click is exactly
what the pill has spent its whole life avoiding — if it ever takes focus, the
paste lands in the pill instead of the user's editor. Resolved by separating the
two window flags rather than trading one for the other: `WS_EX_TRANSPARENT` is
dropped only while the controls are on screen, `WS_EX_NOACTIVATE` never comes off.
Verified live: during recording clicks land, foreground window unchanged, pill not
foreground; while armed clicks pass straight through.

Below `BUTTON_MIN_H` (96px) the capsule is too short to hold controls and they are
not drawn — the armed dot never sprouts buttons.

**History was never broken.** 26 rows on disk including the message that asked
this question. It was undiscoverable, not missing. Tray double-click now opens it.

**Two bugs found on the way:**

1. *Duplicate history rows.* The silence guard wrote an `empty` row and returned;
   the `finally` block then wrote a second row claiming `ok` with NULL text. One
   session, two rows, one of them a lie. Fixed by setting the status and falling
   through so a single writer owns the row.

2. *Cleanup silently skipped.* `verify.py` failed the localhost-cleanup check
   while Ollama itself reported up. Cause was VRAM, not the service: Whisper
   (~1.2GB) and qwen2.5:7b (~5GB) share an 8GB card at 6517/8151 MiB used, and
   Ollama unloads an idle model — the reload then landed inside the next
   dictation's 4s timeout, which had been calibrated with the model warm and
   alone. Fixed with `keep_alive: 30m` on the request and an 8s base budget. The
   raw transcript was still pasted throughout, so nothing was ever lost; the
   symptom was text arriving unpolished with only a log line to show for it.

**Not from the design library.** Asked it for better references and it returned
recipe cards and a broadsheet portfolio — it is a web design corpus, and there is
nothing in it about a 30px audio meter. Designed against the Wispr reference
instead. Recorded here so the next session does not re-run that query expecting
something else.

313 tests, 17/17 checks.

### Follow-up the same day — the meter was a barcode, and it was measurable

Packing the bars closer exposed a problem that wide spacing had hidden. Each bar
ran on its own non-harmonic oscillator, which is what made nine well-spaced bars
look alive. At fifteen bars on a 1.4px gap the row is visually continuous, and
independence stops reading as liveliness and starts reading as noise.

Quantified rather than eyeballed: count sign changes in the row's first
difference, i.e. how many crests it has. A waveform has a few; a barcode
approaches n-2. It measured **12 of a possible 12** — every neighbouring pair
disagreed in direction, every frame.

The wrong fix is a shallower swing; the old comment in `waveform.py` was right
that this gives a shimmer. The right one is correlating the neighbours, because
that is what a waveform is. Replaced the per-bar oscillators with two waves
travelling *along* the row, spatial periods that don't divide evenly so the
crests drift in and out of alignment and the shape never repeats. Spring stagger
cut from `i*26` to `i*8` — the waves carry the liveliness now, and a wide stagger
would decorrelate exactly what the waves exist to correlate.

**3 crests, down from 12.** Pinned by three tests: crest count, neighbour-vs-
distant spread, and a check that the row still changes shape rather than settling
into a static arch.

316 tests.

### 2026-08-30, later — "they're just not getting tall enough"

He was right, and by more than it sounded. Measured before touching anything:

| mic RMS | tallest bar | |
|---|---|---|
| 0.012 | 10.8% | the speech threshold — talking quietly looked like silence |
| 0.030 | 28.3% | a normal speaking voice |
| 0.060 | 57.1% | loud |

Two independent causes, both structural rather than a matter of taste.

**A linear gain wastes the range.** `norm = clamp(level * 9.0)` puts full scale at
RMS 0.111, out past shouting, so the band anyone actually dictates in was squashed
into the bottom third. Loudness is logarithmic; the map should be too. Replaced
with a square-root curve against a reference near the top of the speaking band.

**The oscillator could only ever scale bars down.** `target = norm × centre × osc`
with `osc` averaging 0.55 meant that even a *clipping* input averaged 40% height —
the meter could not look full by construction. The wave now modulates around 0.64
instead of multiplying down from 1.0.

I overcorrected on the way and it is worth recording. Pushing the bias to 0.72 and
easing the centre taper made the row tall and killed it — a solid block, no longer
reading as a voice. The test I had written (average height > 0.6) was itself wrong:
a waveform with real troughs cannot satisfy it. Replaced with the pair that actually
expresses the goal — a full input must reach full height, AND crests must stand
2.5x clear of troughs. Both are now pinned.

**The screenshots had been lying the whole time.** `probe_pill.py` drove `level=0.30`
and `0.50`. Real speech RMS is about 0.03. Every shot I had been judging composition
against was saturated at ten times a real voice, which is why the meter looked
acceptable in review and wrong in his hand. The probe now uses measured levels.

**The fixed reference was still a guess about someone else's voice.** His mic is an
eMeet C96 webcam at arm's length, not a headset. Measured its floor: ambient p99
0.00086, 7% of the speech threshold, so the mic is clean and the headroom is real —
but nothing there tells us how loud *he* reads. Rather than ship a guess, the meter
now tracks a decaying peak and blends it with the fixed reference as a geometric
mean: half way toward whatever voice is in front of it. Full adaptation would make
a murmur and a shout identical; none of it leaves a soft speaker stuck at the
bottom. `_PEAK_MIN` stops it auto-levelling the noise floor into a false signal.

| mic RMS | was | now |
|---|---|---|
| 0.012 | 10.8% | 61.1% |
| 0.020 | 18.5% | 78.9% |
| 0.030 | 28.3% | 87.3% |
| 0.060 | 57.1% | 100% |

326 tests.

*Loose end, not fixed:* `pill.py:342` hardcodes `0.012` where `audio.speech_rms_threshold`
already holds it. Tune the config and the pill will not follow.

### 2026-08-30 — should the cleanup model be a smaller one?

Asked because Whisper and qwen2.5:7b share an 8GB card. Benchmarked rather than
reasoned about: `scripts/bench_polish.py` runs each candidate over 7 of his own
real dictations and measures the three things that have already gone wrong here —
latency, retention (content words kept, fillers excluded) and compliance (does it
return only the cleaned text, or talk about the task).

| model | size | p50 | retention | compliant |
|---|---|---|---|---|
| **qwen2.5:7b-instruct** | 4.7 GB | 580ms | **97.3%** | 100% |
| deckard-4b | 2.7 GB | 40959ms | 0.0% | 0% (empty) |
| phi3.5 | 2.2 GB | 490ms | 70.2% | 100% |
| auto-variable-2b | 1.3 GB | 2872ms | 80.9% | 86% |

**No.** Keep qwen2.5:7b. The trap is phi3.5: it is smaller AND faster, and it
silently drops three content words in ten. That is the exact failure an
adversarial pass caught before at 56% loss, and it would be invisible day to day —
the text still reads fluently, it just says less than he said. deckard-4b returns
nothing after 41 seconds.

*Caveat:* Murmur was restarted mid-run, which reloaded Whisper into VRAM and makes
the latency column inconsistent between models. Retention and compliance are
unaffected, and the retention spread (97.3 / 80.9 / 70.2 / 0) is far too wide for
timing to change the conclusion.

The contention itself is already handled by `keep_alive`. Verified with both
resident: qwen held for 29 minutes rather than 4, GPU at 6946/8151 MiB. It fits.

## 2026-08-30 — the "it just did not send" bug, found

```
16:17:45  recording is silent (42.4s); nothing to transcribe
```

He held the chord for 42.4 seconds, talked the whole way through, and the app
threw it away. Not the chord, not a discard, not a race — the audio was captured
correctly. `dur_ms` is computed from `len(pcm)`, so the buffer genuinely held
42.4 seconds. The guard rejected it.

**Root cause.** `rms(pcm) < threshold / 2` averages across the entire recording.
Every thinking pause pulls that average down, so the verdict gets *worse the
longer you talk* — precisely backwards. Modelled at a webcam-mic level, speaking
14 of 42 seconds was enough to be discarded. The question "is there speech here"
is a maximum, not a mean. Replaced with `peak_rms`, the loudest 400ms window.

**Second root cause, and the one that explains the rest.** Solving backwards from
that failure: for a 42.4s clip he spoke ~20-30s of to land under the old guard,
his speaking voice reads about **0.007-0.010** on the eMeet C96. `speech_rms_threshold`
was **0.012** — *above* his voice. It is used in three places:

| site | consequence |
|---|---|
| the silence guard | dictations discarded |
| `vad.py` auto-stop | hands-free could stop mid-sentence |
| `pill.py:342`, a hardcoded copy | **bars ran `breathe()` the entire time he talked** |

That last one is why "it's still not big enough" survived a day of meter tuning:
`step()` was never reached. None of the sensitivity work could have shown up.
Threshold lowered to 0.004 (4.6x his measured noise floor of 0.00086, well under
his quietest talking), and the pill now reads it from config instead of carrying
a copy.

Two more constants had the same defect once found: the meter's `_REFERENCE` at
0.06 and `_PEAK_MIN` at 0.020, both above his speaking voice. At 0.06 the meter
reached 35% for him no matter what else was tuned. Now 0.020 and 0.004 — his
voice lands near 83% with headroom left above it.

And `probe_pill.py` was still driving `level=0.030`, three times his real voice,
after I had already fixed it once from 0.30. Every screenshot review was done
against a saturated picture.

**Honest note on the regression test.** My first version passed against the old
code — lowering the threshold alone had covered the synthetic case, so it proved
nothing. Rewritten around the property that actually separates the two guards:
length-invariance. Same speech, 90s more silence around it, same verdict.
Confirmed failing on the old guard and passing on the new.

**Also this batch, both asked for:**
- Silence now shows a flat, motionless row at half a speaking bar's length,
  instead of a breathing floor. Flat versus wave is a binary readable at a glance.
- The tick and cross are hands-free only. In a push-to-talk hold his fingers are
  already on the keys that stop it. That made the pill click-through in hold
  again, which had to be tied to whether controls exist rather than to state.
- A full-scale bar was capped at 83% of the capsule by the side margin; now 95%.

342 tests, 17/17 checks.

### Same day — the calibration, finally measured instead of derived

Level logging went in with the silence-guard fix, so his next dictations reported
their own loudness:

```
16:33:17  level: loudest 400ms window 0.0268 over 31.0s
16:34:36  level: loudest 400ms window 0.0195 over 10.5s
```

**This corrects the 0.008 figure recorded above.** That number was *derived* from
the 42.4s failure and was the average across a whole clip; the meter is fed
per-block levels, whose peaks run two to three times higher. The derivation was
sound for what it measured and wrong for what it was then used to calibrate.

Checked against the real numbers, `_REFERENCE = 0.020` lands well:

| his block level | meter reads |
|---|---|
| 0.004 (threshold) | 36% |
| 0.008 (quiet) | 50% |
| 0.014 (typical) | 67% |
| 0.027 (his peak) | 83% |

Real dynamics across the range he actually speaks in, and the silence guard now
sits **10x** below his quietest measured recording — the 42-second loss cannot
recur at anything like his normal volume.

`probe_pill.py` corrected a third time: 0.30 → 0.03 → 0.014. The first two were
guesses; this one is his own microphone.

Also: the resting pill draws nothing inside itself. Armed means Murmur is loaded,
not that it is listening, and bars in it said otherwise.

## 2026-08-30 — air-gapped adversarial audit, ~300 scenarios

Seven independent authors, each given one subsystem's contract and nothing else:
no engineering log, no locked-decision list, no existing tests. They wrote and ran
scenarios and reported failures with reproductions. Six of seven have reported.

Every claim below was re-verified here before anything changed. Fixed this pass:

**The Ctrl+Win shortcut guard never ran in production.** `chord.py` discards a
HOLD when a foreign key joins it, and `tests/test_chord.py` proved that branch
works — by feeding the FSM directly. `hotkey.py` filters every vkCode through
`VK_MAP`, which contains only ctrl/win/space/esc, so no foreign key was ever
delivered and the branch was unreachable. A guard shipped, tested, and written
into CLAUDE.md as a guarantee, that never executed once. The test was at the
wrong layer.

**A stale arm ended hands-free sessions.** `armed_for_stop` was set by any
modifier keydown during a hands-free session and cleared only when the session
ended. Pressing Ctrl+Win — reaching for Ctrl+Win+arrow — left it armed after
release, and the next Space typed ended the dictation and pasted it into whatever
had focus. Traced end to end. Hands-free exists for talking WHILE typing, and
Space is the most-typed key there is. Now cleared on release, and the stop
requires the chord to be held as Space lands.

**Auto-repeat could stop a session it had just started.** The chord is still down
at the moment of promotion; a repeat could arm and a second Space stop it ~600ms
in. `IDLE` already guarded this with `_blocked_until_release`; `REC_TOGGLE` now
does too.

**The pre-roll was replayed into the next session.** `begin()` consumed it but
never drained it, and it stops being fed while capturing — so the same 400ms was
prepended again, arbitrarily old, since the end-of-session cue mutes it straight
after. The opening words of one dictation reappeared at the head of another.

**`peak_rms` threw away up to 199.9ms of every clip** — a hole in the silence fix
made hours earlier. The tail was discarded whenever it was under half a window,
and the tail is the END of the recording, where the last word is. A short reply
that fit inside it read as silence, and one extra sample flipped the verdict.
Now zero-padded and measured. A single NaN sample also sank a whole clip:
`max()` propagates it and NaN compares False against every threshold.

**The cleanup could type things that were not what he said.** The character
guards only catch length-shaped failures. A refusal, a translation, or an answer
to the dictated question all return at roughly the input's length and passed —
and that looks like success. The guards were also unreachable below 34
characters, because the shrink slack is absolute: "remind me to email the
landlord" came back as "." and was typed. Added a content-word retention check
(fillers excluded, so a real cleanup still passes), plus stripping of markdown
fences, model preambles, and control characters — a NUL silently truncates the
Win32 clipboard, which is content loss presented as success.

My first preamble pattern ate real speech ("Okay, the deploy window is nine to
five: tomorrow." → "tomorrow."). The retention guard caught it and fell back to
raw, which is the layering working, but the pattern now requires a newline after
the colon.

375 tests, 17/17 checks. Remaining findings are triaged in the reply to Tommy —
several need a product decision rather than a fix.

## 2026-08-30 — the two critical audit findings, fixed

**Esc cancelled whichever session the worker happened to be holding.** The
fallback when nothing was recording read `_inflight`, a single slot the worker
overwrites per job. The audit measured it: 200/200 cancels lost once the worker
had moved on, with an unrelated earlier session destroyed in its place. That is
the same process-wide-flag shape that per-session cancel flags were introduced to
remove — reintroduced in a new place.

Replaced with `_pending`, an ordered list of sessions that have been queued and
not yet delivered. Esc takes the most recent one, which is the session on screen.
A session leaves `_pending` the moment its text reaches the clipboard, so Esc
cannot retroactively mark a delivered dictation cancelled. `_inflight` stays, but
as diagnostics only and labelled as such — the audit's own tests use it to watch
the worker, and that is a legitimate thing to expose.

**Nothing told the chord FSM when a session ended by any route but the chord.**
`adopt_toggle_session()` existed for the way in and had no counterpart. After a
silence auto-stop the FSM still believed it was recording, so every later Esc
emitted a genuine cancel into an app with nothing recording — which is what made
the fallback above reachable from the keyboard during an ordinary hands-free
dictation. Added `release_session()`, called from every path that ends a session,
idempotent so the chord path can call it too.

Also, one line in the same function: the cancel was announced unconditionally, so
the app confirmed a cancel it had not performed for text already in the document.

**Verified as regressions:** all five new tests fail against the stashed source
and pass against the fix.

**One false positive, triaged with evidence.** The audit's `test_33` asserted that
no session may be delivered once a cancel is acknowledged. With the fix, pytest's
locals show `{'bravo': 'cancelled', 'alpha': 'ok'}` — Esc reached the session it
was meant for and left the other alone, which is correct. That assertion conflated
"the cancelled session" with "any session"; it was invisible when written because
the code cancelled the wrong one and both symptoms appeared together. Rewritten to
the real invariant, with the reasoning kept in the file.

**One self-inflicted regression on the way.** Removing `_inflight` outright broke
28 of the audit's scenarios, which use it to watch the worker — the adversarial
count went 76 → 104 before I looked. Restoring it as diagnostics fixed all 28.

Adversarial scenarios failing: 94 at the audit, 76 after the first fix pass, **72
now**. 381 unit tests, 17/17 system checks.

## 2026-08-30 — remediation tier 1: nothing rewrites, duplicates or drops the text

Plan and sequencing in `REMEDIATION.md`. Tiers are ordered by what a defect costs
the user, but also so each lays a seam the next one uses — tier 1's injector lock
is where tier 2's modifier guards and tier 6's clipboard guards belong.

**1. `apply()` is idempotent.** `Labs -> Labs Inc` turned "welcome to Labs Inc"
into "Labs Inc Inc". The single-pass rewrite stopped runaway growth *within* a
call and did nothing when the input already held the wrong form as a whole word —
which is exactly what corrected output looks like. The guard has to anchor at
every offset where the wrong form occurs inside the right one, because
`Inc -> Labs Inc` matches at the END of an already-correct "Labs Inc".

**2. A case-only fix on a common word needs a second sighting.** `us -> US` from
one manual edit rewrote the pronoun everywhere. Not refused — US, IT and IN are
real corrections — but demoted to the ordinary supervised path. Teaching it still
works; it takes saying it twice. Proper nouns, names and technical terms are
untouched and still trusted at once.

**3. One observation counts once.** Three leaks into one counter: the read-back
timer re-read for the whole 20s–120s window, a re-dictated phrase left two pending
entries that scored one clipboard event twice, and matching ran against every
pending paste rather than the closest — inventing `dan -> Dana` alongside the
correct `dana -> Dana`. All three close by keying on *what was observed* rather
than on a read count, and learning from the best match only.

*The first version of that fix was wrong.* Two identical pastes tie on score, the
first always wins, and it is the one already counted — so a genuine second
sighting was silently dropped and a real correction never promoted. Caught by an
existing test. The already-counted filter now runs BEFORE the best is chosen.

**4. One injection at a time.** No lock existed, and copy/paste are split so an
animation can run between them; a second dictation setting the clipboard inside
the first one's settle window made both presses paste the second transcript.
Re-entrant, and named as the seam later tiers hang their guards on.

**5. Quitting drains before closing.** The sentinel went in behind queued jobs and
the stores closed immediately after, so those jobs ran against closed databases —
not delivered, and no history row either. Now joins the worker with a 15s ceiling
so a wedged one cannot stop the app closing.

**Verified as regressions:** 13 of the new tests fail against the stashed source.
406 tests, 17/17 checks. Adversarial failures 72 → 66.

## 2026-08-30 — remediation tier 2: no action the user did not ask for

**6. The pill offered a target it had not drawn.** Hit-testing gated on the
active state GROUP; the painter draws the controls only while recording. For the
frames after a hands-free recording ended, while the capsule was still shrinking,
the tick and cross were hittable and invisible — a click aimed at the editor was
taken from the window underneath and reinterpreted as stop-and-paste. Both now
gate on the same condition, and a test asserts they agree across every state
rather than only the one that broke.

**7. A cancel between the clipboard and the paste was overruled.** Cancellation
was checked once before the copy and never again. The clipboard write stays first
— a failed animation must never cost the user their words — but there is a free
check point after it, and it is now taken. The text stays on the clipboard, so
the user can still have it if they want it.

**8. The comet's aim belongs to the session.** It was one slot written by every
stop, so a dictation still transcribing when the next one ended flew to the newer
session's cursor. Same ownership-travels-with-the-session pattern as the cancel
flag and the pending list.

**9. `paste()` re-checks the modifiers itself.** It assumed `copy()` had cleared
them, but the two are deliberately split so an animation can run between — and
the chord can be re-pressed inside that window, which turns the paste into
Ctrl+Win+V and opens Clipboard History. Refusing costs nothing: the text is
already on the clipboard.

**10. The modifier wait samples before it waits.** The check lived only inside a
timed loop whose condition is evaluated first, so a zero timeout — or any stall
longer than the timeout before the loop was entered — reported "still held"
having polled nothing at all. It wanted do-while semantics.

Also added `tests/conftest.py`. The `app`, `pill` and `qapp` fixtures were defined
per module, so a new test file could not reach them. Modules defining their own
still win, so nothing existing changed.

**Verified as regressions:** 9 of the 16 new tests fail against the stashed source.
422 tests, 17/17 checks.

## 2026-08-30 — remediation tier 3: it always starts, and always says why

**14. `RecursionError` was not caught.** A settings file of deeply nested objects
stopped the app starting. It is not a `ValueError`, so it fell through the load
guard. The contract at the top of `config.py` is absolute; the fix is one
exception class.

**12. Type validation was not value validation.** Zero, negative and NaN are all
the right type and all break a consumer silently — a NaN speech threshold makes
every comparison false, so the app never hears anything. JSON also accepts the
bare literals `NaN` and `Infinity`, and `1e400` parses to infinity, so all three
arrive as floats and sail past an isinstance check. Added inclusive ranges and a
finiteness check, both logged on repair.

**11. A scalar shadowing a section silently deleted it.** `{"hotkeys": "ctrl+alt+q"}`
replaced the whole branch, and repair only covered keys with a declared type —
`hotkeys` and `stt` have none at all, so nothing was restored and **not one line
was logged**. Murmur started with no hotkey and no way to find out why, which for
a tray app with no console is indistinguishable from failing to start. Sections
are now restored wholesale, with a warning naming what replaced them.

**13. An unusable store was fatal at construction.** A half-written `history.db`
after a power cut raised before the app was up. New `murmur/store.py` moves the
bad file aside — stamped, so a second bad start does not overwrite the first
casualty — starts a fresh one, and logs where the old went. Deleting it silently
would have been a trade the user never got to make.

**Verified as regressions:** 20 of the 35 new tests fail against the stashed source.
457 tests, 17/17 checks.

## 2026-08-30 — remediation tier 4: no silent quality loss

**15 + 16 done as one change.** `polish.py` documented its copy of the learned
term list as untrusted and sanitised it; the transcriber joined the same list
raw, unbounded, past a conditioning window of about 224 tokens — so an oversized
list did not merely fail to help, it silently cost decoding accuracy at the
earliest point in the pipeline, where nothing downstream can recover it. And the
cleanup prompt capped each term's length but not the count, so five thousand
terms grew the system prompt to 49,782 characters on the latency path of every
dictation. One sanitiser and one cap now live in `vocabulary.py`, which owns the
set; both consumers read from it. Writing them separately is how they diverged.

**17. The meter's gain only forgot while the user was talking.** The decay lived
in `step()` alone, and `flat()`/`breathe()` — the paths that run during exactly
the silence the four-second constant exists to consume — never touched it. A
cough pinned the peak and the first word after a pause read ~56% of its height.

**18. A forgotten session could record indefinitely.** One frame above the
threshold reset the whole silence counter, so a door, a cough or an HVAC cycle
once every ninety seconds kept it alive forever. Now requires 0.30s of continuous
sound to call the session live again: short enough that a syllable counts, long
enough that a click does not.

*Two VAD tests failed and one of them encoded the defect.*
`test_intermittent_speech_never_fires` asserted that intermittent sound never
auto-stops — which is the finding, stated as a guarantee. Split into two: the
real protection (someone talking with natural pauses is never cut off) and the
fix (a room that is merely not silent does not keep a session alive). The other
test's intent was sound and just needed sustained speech rather than one frame.

**19. History search could not reach the cleaned column.** It searched raw, final
and corrected; anything held only in `polished_text` was invisible to the only
search API the panel has.

470 tests, 17/17 checks.
