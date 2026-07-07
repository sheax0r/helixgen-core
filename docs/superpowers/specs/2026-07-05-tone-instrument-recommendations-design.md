# Instrument-aware control recommendations — design

**Date:** 2026-07-05
**Status:** Draft (pending user review of this written spec)
**Source brief:** backlog item "Instrument-aware control recommendations."
The user owns four guitars with materially different pickups (P-90 single-coil,
active humbuckers, coil-splittable humbuckers, super-strat DiMarzios). The
`/tone` skill already emits *generic* guitar-side settings (Step 6) but never
picks **which** of the user's guitars best suits the target tone, nor tailors
the control moves to that specific instrument's switch and pickup layout.

## Goal

When `/tone` designs a preset and the user's instrument lineup is known, the
skill should:

1. **Select** the best-fitting owned instrument for the target tone (by pickup
   character → genre/tone fit), unless the user already named one.
2. **Recommend concrete controls** on that instrument — pickup selector
   position (in the guitar's own switch language), tone-knob and volume-knob
   roll-off, coil-split state where applicable — tuned to the tone, plus a
   short pick-attack / dynamics note.
3. **Record** these recommendations in the generated handoff docs (the
   companion `<slug>.md` and the spoken Step-8 report) alongside the preset.

This is a **skill-content + data-source** feature. It changes the `/tone`
workflow (Steps 6, 7a, 8) and defines how the skill reads the instrument
lineup. It does **not** change the helixgen Python library, the MCP surface, or
the `.hsp` bytes — the recommendation is human-facing metadata, not preset
state.

## Non-goals (this feature)

- **Designing the user-preferences file.** A sibling backlog item
  ("user-preferences file") will formalize where the instrument lineup lives
  and its schema. This spec defines only the *consumption contract* the tone
  skill uses, and a memory fallback for today. When the prefs file lands, this
  feature reads from it with no further design work here.
- **Auto-selecting a guitar the user didn't ask about, silently.** The
  selection is a *recommendation stated to the user*, never a hidden
  assumption that changes preset params.
- **Buying advice / gear the user doesn't own.** Recommendations only ever
  reference instruments in the user's own lineup. No "you'd want a Strat for
  this."
- **Amp/param changes driven by the selected guitar beyond what Step 5 already
  does.** The existing per-guitar amp-EQ table (Step 5) still applies; this
  feature does not add a second, competing EQ-adjustment path. It focuses on
  the *player's hands-on controls*, not the modeled amp.
- **Per-string / setup / string-gauge / tuning advice.** Out of scope; the
  recommendation covers selector, volume, tone, coil-split, and pick attack
  only.
- **Bass instrument selection.** The user's current lineup is all six-string
  guitars; the heuristic is written for guitars. Bass targets fall through to
  the existing generic Step-6 behavior until the lineup includes a bass.

## Where this sits in the `/tone` workflow

The skill already has the relevant seams:

- **Step 5** — per-guitar *amp-EQ* tweaks (Treble/Presence/Bass by pickup
  type). Unchanged; still keyed on whatever guitar is in play.
- **Step 6 — "Pick guitar-side settings."** Today this maps a *tone goal* to a
  generic selector/volume/tone triple, and has a small "if the user named a
  specific guitar, adjust switch language" note. **This feature promotes Step 6
  to first select the instrument, then resolve the controls on it.**
- **Step 7a** — companion `<slug>.md`. Gains a **Recommended instrument**
  section.
- **Step 8** — spoken report. The existing "Guitar settings" line is upgraded
  to name the selected guitar and the one-clause "why."

No new workflow step is added; Step 6 is rewritten and Steps 7a/8 gain a
sub-item.

## Data source — instrument lineup

### Consumption contract

The skill needs, per owned instrument, a small record:

| field | example | used for |
|-------|---------|----------|
| `name` | `"Gibson Les Paul Junior"` | display / report |
| `short` | `"LP Jr"` | terse report line |
| `pickups` | `"single bridge P-90"` | character matching |
| `pickup_class` | `"p90"` \| `"humbucker_active"` \| `"humbucker_passive"` \| `"single_coil"` \| `"humbucker_coilsplit"` | the primary selection key (see heuristic) |
| `selector` | `"none (single pickup)"`, `"3-way: rhythm(neck)/middle/treble(bridge)"`, `"5-way: 1=bridge HB … 5=neck HB"` | switch-language for recommendations |
| `coil_split` | `true` / `false` | whether a split recommendation is available |
| `tags` | `["punk","garage","raw-rock","early-breakup"]` | genre/feel fit scoring |
| `notes` | `"active EMG 81/60 — confirm active vs passive"` | caveats surfaced to the user |

This record is intentionally loose. The prefs-file spec owns the authoritative
schema; the tone skill maps whatever that file provides onto these fields, and
tolerates missing optional fields (`coil_split` defaults `false`, `tags`
defaults empty).

### Resolution order

The skill resolves the lineup in this precedence, stopping at the first hit:

1. **User-preferences file** (future). When it exists, read the instruments
   list from it. This is authoritative once shipped.
2. **Memory: `user_guitars.md`.** Today's source of truth. It already carries
   pickup config, per-guitar switch language, and a genre→guitar mapping — the
   heuristic below is derived directly from it. The skill parses the four
   bullet entries into the record shape above. Caveats already noted in memory
   (e.g. "confirm active vs passive on the EC-1000", "Prestige pickup config
   TBD — ask") flow through into the `notes` field and are surfaced.
3. **User just named a guitar in the request, with no stored lineup.** Build a
   one-item lineup from what they said; skip selection (there's nothing to
   choose between) and go straight to control recommendations, inferring
   `pickup_class` from the description.
4. **Nothing known.** Fall back to the existing generic Step-6 behavior (tone
   goal → selector/volume/tone by pickup type in the abstract) and add one
   clarifying question if a guitar is genuinely load-bearing for the tone.

Precedence 1 over 2 means that when the prefs file lands, it silently takes
over with no skill edit. Until then, memory drives it.

### User override always wins

If the user **named a specific instrument** in the request (or earlier in the
conversation), that instrument is used regardless of what the heuristic would
pick — the skill does not overrule a stated choice. When the named guitar is a
poor fit for the tone, the skill still honors it and adds a single honest line:
"the EC-1000's scooped active EMGs will fight this vintage-crunch voicing — if
you have it handy, the LP Jr's P-90 nails it more directly." One nudge, not an
argument.

## Instrument-selection heuristic

The goal is a *stated recommendation with a one-line rationale*, not a precise
score. The skill picks by matching the tone's character to pickup class, using
`tags` as a tie-breaker and Step-1b research findings as the strongest signal.

### Primary map: tone character → pickup class → the user's guitar

Derived from `user_guitars.md`'s own genre→guitar guidance so the two never
drift:

| Tone target (genre / feel / artist territory) | Wants pickup character | User's guitar (current lineup) |
|---|---|---|
| Punk, garage, raw blues, Stones/Replacements, vintage rock, early breakup, gritty midrange bark | P-90: hot single-coil, barks, breaks up early | **Les Paul Jr** (single bridge P-90) |
| Modern metal, metalcore, scooped-mid djent, tight chugga rhythm, high-output high-gain | Active humbucker: tight, scooped, high-output | **ESP LTD EC-1000** (active EMG 81/60) |
| Prog, djent, fusion, technical lead, 7-feel clarity, **pristine/clean that needs sparkle** | Coil-split humbucker: HB for gain, split for SC clarity | **Strandberg Boden Essential 6** (Suhr HBs, 5-way w/ splits) |
| Classic rock, Foo Fighters, shoegaze, hard rock with sustain, versatile fusion | Versatile passive HB / HSH DiMarzio | **Ibanez Prestige** (super-strat, HH or HSH) |

### Resolution rules

1. **Research beats the table.** When Step 1b established the reference
   artist's actual instrument (e.g. "this is a P-90 into a Plexi"), match to
   the pickup *class* the reference used and pick the user's guitar in that
   class. The table is the fallback when there's no specific reference.
2. **Clean-with-clarity routes to the coil-split guitar.** A pristine clean
   that needs sparkle and note separation favors the Strandberg's split
   positions over a full humbucker, even if genre tags are ambiguous —
   splitting is the concrete lever the recommendation then pulls.
3. **Early-breakup / raw grit routes to the P-90.** If the tone is about amp
   breakup and touch dynamics rather than tightness, the LP Jr wins; its P-90
   pushes an amp into breakup earlier and dirtier than the humbucker guitars.
4. **Tight/scooped high-gain routes to the active-humbucker guitar.** If the
   tone is modern, palm-muted, and low-noise-under-gain matters, the EC-1000's
   active EMGs are the pick.
5. **Ambiguous mid-gain hard rock routes to the Prestige** as the versatile
   default — it's the "when in doubt for rock" instrument in the lineup.
6. **Tie-break by `tags` overlap**, then prefer the more versatile instrument
   (Prestige > Strandberg > EC-1000 > LP Jr, by breadth) so a borderline call
   lands on the guitar most likely to also serve adjacent parts.

### Second choice

The skill always names a **runner-up** with one clause on when to reach for it
("or the Prestige if you want it tighter and less hairy"). This costs one line
and makes the recommendation feel like guidance rather than a verdict — and
covers the case where the user doesn't have the first pick to hand.

## Recommendation content

Once an instrument is selected, the skill resolves concrete controls by
combining the **tone goal** (existing Step-6 defaults-by-tone-goal table) with
the **selected instrument's switch layout** (`selector`, `coil_split`). The
output is specific, in the guitar's own language:

- **Pickup selector position** — named in the instrument's real switch
  language, not generic. Examples:
  - LP Jr: "bridge P-90 (only pickup)" — no selector move to give.
  - EC-1000: "treble (bridge)" / "rhythm (neck)" / "middle (both)" via the
    3-way.
  - Strandberg: "position 1 (bridge humbucker)" … "position 4 (neck
    inner-coil split)" — reference the 5-way positions from memory, and flag
    "confirm your wiring if it differs."
  - Prestige: "position 1 (bridge HB)" or "position 2 (split/inner coils)" —
    note the 5-way(HSH) vs 3-way(HH) ambiguity and ask if it's load-bearing.
- **Volume knob** — 0–10, with the *reason* when it's not 10 (e.g. "roll to 7
  to clean up the breakup for the verse; back to 10 for the chorus push").
- **Tone knob** — 0–10, with the reason (e.g. "4–6 for a rounded 'woman tone'
  lead"; "10 for maximum bite on the tight rhythm").
- **Coil-split state** — only when the instrument supports it (Strandberg,
  possibly the Prestige): "split the bridge (position 2) for the clean verse's
  glassy top, full humbucker (position 1) for the chorus." Omit the line
  entirely for non-splittable guitars rather than saying "n/a."
- **Pick attack / dynamics note** — one clause tying technique to the tone,
  since these guitars respond very differently to the hands: e.g. "the P-90
  rewards digging in — pick hard near the bridge for the bark"; "for the active
  EMGs, palm-mute tight and let the pickup do the compression"; "light pick
  attack keeps the split coils from sounding brittle."
- **Why this guitar** — one clause of rationale tied to the tone
  ("P-90 breaks up early and barks in the mids — exactly this garage-rock
  crunch").
- **Caveat passthrough** — any `notes` caveat from the data source that affects
  the recommendation (active-vs-passive, Prestige config TBD) is surfaced as a
  short conditional, not silently assumed away.

### Specificity guardrail

Recommendations must always be in the **selected guitar's actual switch
language** — the existing Common-Mistakes rule ("Generic guitar advice that
ignores the named guitar") is extended to cover the *auto-selected* guitar too.
Never emit "middle/position 4" for a guitar with no middle position (the LP Jr
has one pickup; the EC-1000 has a 3-way, not a 5-way).

### Snapshots and multi-part presets

When the preset carries snapshots (rhythm/lead, verse/chorus/solo), the
recommendation stays on **one instrument** (the user isn't swapping guitars
mid-song) and instead expresses the per-scene differences as **control moves on
that guitar**: which snapshot wants volume/tone rolled back, or a coil-split
toggled. Example: "Strandberg throughout — split (pos 4) + volume 7 for the
clean verse snapshot, full bridge (pos 1) + volume 10 for the lead snapshot."
This mirrors how snapshots already vary amp params per scene.

## Handoff docs format

### Companion `<slug>.md` (Step 7a) — new section

Add a **Recommended instrument** section to the companion markdown, placed just
after the existing "Guitar settings" content (or replacing it, since it
subsumes it). Shape:

```markdown
## Recommended instrument

**Pick:** Gibson Les Paul Junior (single bridge P-90)
**Why:** P-90 breaks up early and barks in the mids — nails this garage-rock
crunch more directly than a humbucker, which would sound too smooth and tight.

**Controls**
- Selector: bridge P-90 (only pickup)
- Volume: 10 (roll to 7 for the cleaner verse)
- Tone: 8 (back off the fizz without going dark)
- Coil-split: n/a (single P-90)
- Pick attack: dig in near the bridge — the P-90 rewards a hard, aggressive
  pick for the bark.

**Second choice:** Ibanez Prestige (bridge HB) if you want it tighter and less
hairy — trade some of the raw breakup for sustain.

**Note:** based on your recorded lineup. If you've re-strung or re-wired,
adjust the position language accordingly.
```

Rules for the section:
- Always includes **Pick**, **Why**, **Controls**, **Second choice**.
- **Coil-split** line is present only when the guitar supports it; otherwise
  omit the bullet (don't write "n/a") — *exception:* the LP Jr example keeps a
  short "single P-90" note because the absence of a selector is itself worth
  stating once.
- For snapshot presets, the **Controls** bullets carry the per-scene moves
  (see "Snapshots" above) instead of single values.
- Any data-source caveat becomes the trailing **Note**.

### Spoken report (Step 8) — upgraded line

The existing Step-8 "Guitar settings" line is replaced by two short lines:

3a. **Instrument:** `<guitar> — <one-clause why>` (e.g. "Les Paul Jr — its
    P-90 barks and breaks up early, which is the whole sound here").
3b. **Settings:** `Selector: <pos> · Volume: <n> · Tone: <n>` plus the
    one-clause non-obvious move (coil-split or volume roll-off), matching the
    existing terse format.

If the user explicitly named the guitar, line 3a collapses to just confirming
the settings (no "why I picked it," since they picked it) — but still in that
guitar's switch language.

## Interaction with existing skill text

- **Step 6** is rewritten from "map tone goal → generic settings" to "select
  instrument (heuristic) → resolve controls on it." The existing
  defaults-by-tone-goal table is retained as the *control-resolution* layer
  (it maps a tone goal to a selector/volume/tone starting point); the new
  layer sits above it, choosing the guitar and translating positions into that
  guitar's switch language. The per-guitar switch-language bullets already in
  Step 6 fold into the instrument records.
- **Step 5's** per-guitar amp-EQ table is unaffected and now has a defined
  input: once Step 6 selects a guitar, Step 5's EQ adjustments key off the same
  selection (today they key off whatever guitar was named, which may be
  nothing). Recommend the skill run the *selection* early enough that Step 5's
  amp-EQ can use it — i.e. do the instrument pick during Step 2's chain sketch
  ("based on the reference AND the user's guitar"), then finalize controls in
  Step 6. This is a wording change, not a reorder.
- **Common Mistakes** table gains one row: "Recommending controls in generic
  switch language for the auto-selected guitar → always use that specific
  guitar's real switch positions (LP Jr has no selector; EC-1000 is 3-way)."

## Open questions for the user

1. **Auto-select vs always-ask.** When you *don't* name a guitar, do you want
   the skill to pick one for you (with a stated rationale and a runner-up), or
   would you rather it list the top two and let you choose before generating?
   The design above auto-picks-and-states; say if you'd prefer a prompt.
2. **EC-1000 active vs passive.** Memory flags this as "confirm." Should the
   skill ask once and record the answer (so future tones skip the caveat), or
   keep surfacing the conditional each time? A one-time confirmation seems
   better — but that's really a prefs-file question.
3. **Strandberg / Prestige exact wiring.** The 5-way position map in memory is
   annotated "confirm if your wiring differs," and the Prestige is HH-or-HSH
   TBD. Do you want to pin these down once (ideally in the coming prefs file)
   so coil-split and position recommendations are exact rather than hedged?
4. **Second-choice always, or only when close?** Emitting a runner-up every
   time is one extra line and useful when you don't have the first pick to
   hand. Acceptable as a default, or only surface it when the top two are a
   genuine toss-up?
5. **Replace vs augment the current "Guitar settings" line.** The design
   subsumes today's generic guitar-settings output into the new section. Any
   reason to keep the old generic line as well (e.g. for tones where no lineup
   is known)? Current plan: keep the generic line only in the "nothing known"
   fallback path.
6. **Snapshot per-scene control moves — how granular?** For a
   verse/chorus/solo preset, do you want an explicit control line per snapshot,
   or just the one or two scenes where the guitar move actually matters
   (leaving the rest at the base setting)? Design leans to "only where it
   matters" to keep it scannable.
