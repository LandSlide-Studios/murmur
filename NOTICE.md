# Third-party notices

## Sotto

The comet — the animation that carries your transcript from the pill to where
your cursor was — is adapted from **Sotto** by kingbootoshi and contributors:

> https://github.com/kingbootoshi/sotto
> Copyright (c) 2026 Sotto contributors
> MIT License

Sotto is a macOS dictation app written in Swift. None of its code is used here;
Windows and Qt share nothing with AppKit and CoreAnimation. What is taken is the
**motion design**, which is the part that took judgement:

- A 110ms pull-back before launch, so the throw is loaded rather than merely
  starting.
- A 260ms flight on an ease-out cubic, so it arrives rather than drifts.
- Elongation along the direction of travel, peaking at mid-flight — this is what
  makes a moving dot read as a comet instead of a sliding circle.
- Aiming at where the pointer was **the instant you stopped talking**, and never
  correcting. Sotto's README is explicit that it is ballistic and does not chase
  the cursor, and that restraint is what makes it feel like a delivery.

Those values live in `murmur/ui/comet.py`, named and commented, so it is obvious
what was borrowed and why changing it changes the feel.

Sotto's own design workbenches (`scripts/*.html` in their repository) document
how each was chosen. Credit for the choices is theirs.

## Sotto — the sound set

`scripts/make_sounds.py` reimplements Sotto's synthesis model and its "sotto"
pack. Their principle, in their words: **felt, not heard**. Every voice runs
through its own one-pole low-pass and then a master 2 kHz ceiling, so energy
lives at 40-600 Hz and nothing pierces. Cues anchor to F and C, so repeated use
never sounds out of tune with itself.

The recipe is theirs, verbatim from `SoundPlayer.swift`:

| Cue | Voices |
|---|---|
| ack | sine 175Hz (F3) + 350Hz (F4), lp 800 |
| merge | sine 131Hz (C3) + 262Hz (C4), lp 600 |
| charge | sine sweep 46 -> 88Hz over 550ms, lp 240, slow 200ms attack |
| launch | noise, low-pass sweeping 520 -> 130Hz over 480ms |
| arrive | noise 1800 -> 200Hz plus a sub sine 87 -> 44Hz |

So is the synthesis: linear attack into exponential decay, geometric frequency
sweeps, per-voice low-pass then the master ceiling, voices summed with delays.
Rewritten in numpy because AVAudioEngine has no Windows counterpart, but the
numbers are unchanged — they are the part that was tuned by ear.

Murmur adds one cue Sotto has no use for, a `cancel` falling minor third, built
to the same rules so it belongs to the set.

## Sotto — the glass pill and the rim

The overlay treatment is adapted from `Overlay.swift`: a translucent body lit
from the top, and the rim light — **two lines 180 degrees apart gliding the same
direction**, so when one rides the top edge the other rides the bottom. Sotto
picked a 2.25s lap with each line covering 18% of the perimeter, in
`scripts/rim-variants5.html` variant 1; both values are used here.

The glass itself is painted rather than a real backdrop blur. macOS gets that
from `.ultraThinMaterial`; Windows has no per-shape equivalent, and faking one
would mean clipping the window to the capsule with `SetWindowRgn` and resizing
it every frame through the morph — aliased edges for a worse result.

## Sotto — the sound packs

All six of Sotto's packs are ported, recipe for recipe, in
`scripts/make_sounds.py`: sotto, velvet_thud, warm_glass, wood_bar, breath and
heartbeat. Switchable from the tray.

One thing that had to be got right: the files are **not** normalised. Sotto
renders each cue at its designed gain behind a 0.9 master, and per-file
normalisation would have made "Breath — pure air, almost silent" exactly as loud
as "Heartbeat — pulses you feel", flattening the design into six variations on
the same volume. Measured on the shipped files, breath's ack sits at 0.007 RMS
against heartbeat's arrive at 0.097 — the order their descriptions imply.

## Sotto — the orb charge

While the model works, the capsule contracts to a glowing orb that breathes,
as Sotto's overlay does. The point is that the shape must be one you cannot
mistake for "still listening".

### What was not taken

Sotto's transcription runs Parakeet TDT on Apple's Neural Engine, which has no
Windows equivalent; Murmur uses faster-whisper on CUDA.
