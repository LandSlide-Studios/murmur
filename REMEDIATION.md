# Remediation plan — the 37 open findings

From the air-gapped adversarial audit of 2026-08-30. Findings and evidence are in
`LOG.md`; this is the execution order and the reasoning behind it.

## Why this order

The tiers are ranked by what a defect costs the user, but they are also sequenced
so each one lays a seam the next one uses. Doing them out of order means building
the same seam twice.

| Tier | Establishes | Consumed by |
|---|---|---|
| 1 | An idempotent, context-aware correction pass. A serialised injector. Ordered shutdown. | 4 (the same term set), 2 (the same lock), 5 (the same worker path) |
| 2 | Per-session ownership of everything the worker touches. Guards live inside tier 1's lock. | 6 (clipboard guards land in the same place) |
| 3 | A "never raise on load — quarantine, repair, log" pattern for every persisted file. | 6 (save contention extends it) |
| 4 | One sanitiser and one cap, shared by both consumers of the vocabulary. | — |
| 5 | A single-copy buffer read. | 6 (dtype and mute guards touch the same module) |
| 6 | — | — |

**The gate between tiers is not optional.** After each tier: run the full suite,
the adversarial suite, `scripts/verify.py`, and then commission a *fresh*
air-gapped reviewer that has not seen this plan or the fixes, whose only job is
to find what the tier broke. A tier is not done until its gate is clean or its
findings are triaged.

---

## Tier 1 — corrupts or loses what you said (items 1–5)

**Goal:** nothing rewrites, duplicates or drops the user's text.

### Order of work

1. **Item 1 first — `apply()` idempotence.** Everything else in the vocabulary
   depends on the rewrite being safe to run. Guard: skip a match whose
   surrounding text already equals the corrected form. This must not weaken the
   existing single-pass guard against runaway growth; both tests stay.

2. **Item 2 — case-only learning.** Needs Tommy's decision on policy before code.
   Default recommendation: refuse to learn a correction that differs only in case
   when the wrong form is a common function word. That is a smaller feature than
   what exists now, which is why it is his call. Build the stop-list and the
   refusal path; leave the threshold configurable.

3. **Item 3 — supervision.** Three separate leaks into one promotion counter.
   Fix in this order, because each narrows the next: wire the dead `read` flag so
   a read-back counts once; key observations by (pending entry, observed text) so
   a repeated offer cannot re-score; then score every pending entry and learn only
   from the best match rather than all of them.

4. **Item 4 — injector serialisation.** One re-entrant lock around
   copy → settle → paste. This lock is the seam tier 2 items 9 and 10 and tier 6
   items 28 and 29 all live inside, so name it and document it as such.

5. **Item 5 — ordered shutdown.** Drain the queue, then close the stores. The
   worker already exits on a sentinel; the sentinel just needs to go in front of
   nothing rather than behind everything.

### Tests this tier owes

- `apply(apply(x)) == apply(x)` for every learned term shape.
- A correction whose wrong form is a substring of its right form.
- Two pending entries for the same phrase promote nothing from one event.
- A read-back polled twenty times counts once.
- Two concurrent injections deliver two distinct transcripts.
- A dictation queued at shutdown still gets a history row.

### Gate

Fresh reviewer, scope: `vocabulary.py`, `corrections.py`, `inject.py`, and
`app.py`'s shutdown path. Brief: *these were just changed to fix text corruption
and lost dictations; find what the changes broke.*

---

## Tier 2 — steals input or fires at the wrong moment (items 6–10)

**Goal:** no action the user did not ask for.

### Order of work

1. **Item 8 — aim on the Session.** Do this first: it is the same
   ownership-travels-with-the-session pattern already applied to cancellation and
   to `_pending`, and doing it first makes items 6 and 7 easier to reason about.
2. **Item 7 — re-check cancellation between copy and flight.** One check point,
   already free.
3. **Item 6 — pill hit-testing.** Gate on the condition the painter uses, so the
   two can never disagree again. Add a test asserting paint and hit-test agree
   across every state, not just the one that broke.
4. **Items 9 and 10 — modifier guards**, inside tier 1's injector lock. Item 10
   is do-while semantics; item 9 is a re-check at the point of use.

### Gate

Fresh reviewer, scope: `pill.py`, `inject.py`, `app.py` session ownership.
Brief: *focus, click routing and paste timing were just changed; find what
regressed.*

---

## Tier 3 — fails to start or disables itself silently (items 11–14)

**Goal:** Murmur always starts, and always says why when something was wrong.

### Order of work

1. **Item 14 first** — one exception class, unblocks testing the rest.
2. **Item 12 — range validation.** Reject non-finite outright; clamp or default
   out-of-range. This is where the config gains a repair-and-log helper.
3. **Item 11 — shadowed branches**, built on that helper. Every repair logs.
4. **Item 13 — store quarantine**, reusing the same pattern: move the unreadable
   file aside, start fresh, log where the old one went.

### Gate

Fresh reviewer, scope: `config.py`, `history.py`, `vocabulary.py` construction.
Brief: *hostile settings files and corrupt databases must never stop the app
starting; find an input that still does.*

---

## Tier 4 — degrades quality invisibly (items 15–19)

**Goal:** no silent quality loss.

Depends on tier 1: the vocabulary set is only well-defined once learning is
correct.

### Order of work

1. **Items 15 and 16 together** — one shared sanitiser and cap, applied at both
   consumers. Writing them separately is how they diverged in the first place.
2. **Item 17** — decay the meter's peak in every path.
3. **Item 18** — hysteresis on the silence reset. A wall-clock ceiling is a
   product decision; ask before adding one.
4. **Item 19** — include the polished column in search.

### Gate

Fresh reviewer, scope: `stt/`, `polish.py`, `vad.py`, `ui/waveform.py`.
Brief: *find a way to make the transcript worse without anything logging it.*

---

## Tier 5 — latency and resources (items 20–23)

**Goal:** the cost of a long dictation is proportional, not quadratic in memory.

### Order of work

1. **Items 20 and 21 are one change** — `read_all` as two concatenated slices,
   and a chunked level scan. Measure before and after; the claim is 248ms and
   4x, so the fix has to be shown against those numbers.
2. **Item 22** — stop the pill's timer at rest.
3. **Item 23** — retention. Needs a default value from Tommy.

### Gate

Fresh reviewer, scope: `audio.py`, `pill.py` timer.
Brief: *the ring buffer read was just rewritten for speed; prove it still returns
the same samples in the same order under wrap.*

---

## Tier 6 — hardening (items 24–37)

**Goal:** close the demonstrated-but-untriggered holes in one sweep.

Land as three commits, not fourteen, grouped by the seam each touches:

- **Window and overlay** (24, 25, 26, 27, 33) — all in `ui/`, all about the pill
  refusing focus and never wedging.
- **Clipboard and injection** (28, 29, 37) — inside tier 1's lock. Item 37 needs
  a decision first.
- **Persistence and audio** (30, 31, 32, 34, 35, 36) — retry, resync, dtype
  assertions, and the last-resort handler's own guard.

### Gate

Fresh reviewer, whole-app scope. Brief: *this codebase has just had six rounds of
fixes; find what the fixes broke.*

---

## Standing rules for every tier

1. Reproduce before fixing. A finding is a claim until it fails here.
2. Every fix gets a test verified to fail against the previous source.
3. Never widen a fix into a rewrite. If a tier's work exposes a redesign, log it
   and keep going.
4. The gate reviewer never sees this file, `LOG.md`, `CLAUDE.md`, or the tests
   for the code it is reviewing.
5. Nothing is pushed without Tommy's explicit go, per tier.
