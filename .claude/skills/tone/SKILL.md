---
name: tone
description: Use when the user asks for a guitar/bass tone targeted at a specific artist, song, genre, or feel (e.g. "lead in White Limo by Foo Fighters", "warm jazz clean", "thrash rhythm") in a project that uses the helixgen CLI.
---

# Tone

## Overview

Turn a tone description into a `helixgen`-generated `.hsp` (or `.hlx`) preset that's ready to load on a Line 6 Helix device. Drive the helixgen CLI: survey blocks, pick a chain, verify exact param names, write a spec, generate, suggest tweaks.

## When to Use

- User describes a target tone (artist, song, section, genre, vibe)
- User wants a starting point to A/B against a reference
- User mentions a guitar/bass and a role (rhythm, lead, clean, pad, solo boost)

When NOT to use: editing an existing `.hsp` (load and modify directly); ingesting new blocks (`helixgen ingest`); answering "what blocks do I have?" (`helixgen list-blocks`).

## Prerequisites

- `helixgen` on PATH and a populated library at `~/.helixgen/library/` (check with `helixgen list-blocks | head` — empty = run `helixgen ingest <path-to-exports>` first).
- CLAUDE.md in the repo root has the CLI vocabulary and spec.json shape; read it once at the start of the session, not every step.

## Workflow

### 1. Clarify only what's missing

Ask at most 3 short questions, and only the ones the request didn't already answer. Common gaps:

- **Guitar** (single-coil / humbucker / acoustic / bass; specific model if mentioned)
- **Role(s)** — single role (rhythm / lead / clean / pad / solo boost), or multiple. If multiple, **ask the family question** (see 1a below).
- **Reference specifics** (which section of a song; live vs studio version)

If the request implies an answer ("lead in X" → role known; "Strat" → single-coil known), skip that question.

#### 1a. Multi-part disambiguation (only when there are 2+ roles/sections)

When the user wants multiple parts of one song, multiple roles, or multiple sections, ask one focused question:

> "Do these parts share an amp/cab family (e.g. all British crunch, just different gain/effects per part), or are they fundamentally different sounds (e.g. clean Fender for verse, high-gain Mesa for chorus)?"

Then pick the path:

| Answer | Approach |
|--------|----------|
| Same family | **One preset, multiple snapshots.** Pick a chain that fits all parts, vary gain/EQ/effect bypass per snapshot. (See 5.5.) |
| Different families, OK to switch presets between parts | **Multiple presets** — generate one `.hsp` per part, name them clearly (e.g. `<song>-verse.hsp`, `<song>-chorus.hsp`). Switch presets on-device between parts. |
| Different families, need instant switching mid-song | **One preset with layered amps + snapshot bypass.** Place both amps (and both cabs, if different) in the chain; each snapshot enables one amp+cab pair and bypasses the other. Limited by the 12-slot per-path cap — don't go past 2 amps + 2 cabs. |

Default to "multiple presets" when the user says "different sounds" and doesn't specify needing instant switching — it's the simpler spec and the device's preset-switching is fast enough for between-song or between-section transitions in most material.

### 2. Sketch the chain in one line

Based on the reference AND the user's guitar, pick a slot shape. The guitar
shapes choices upstream of EQ — e.g. a Strat into a Plexi needs less
treble-pull at the amp than a Les Paul into the same Plexi; an Ibanez
Prestige with HBs sits differently in a stoner-rock chain than an SG.

State your call briefly so the user can redirect before you commit:

- Classic rock: light drive → plexi-style amp → 4x12 → tape echo → plate
- Modern metal: tube-screamer boost → high-gain amp → 4x12 V30 → noise gate front
- Clean: comp → clean amp (AC15/Twin/Deluxe-style) → 1x12 → optional chorus → plate/spring
- Lead: stack drive higher → less compression → longer delay → bigger verb
- Bass: comp → bass amp → 4x10/8x10 → optional drive parallel

### 3. Pick blocks from the library

For each slot:
```bash
helixgen list-blocks --category amp     | grep -i "<keyword>"
helixgen list-blocks --category drive   | grep -i "<keyword>"
# ...etc for cab, delay, reverb, modulation
```

Prefer block display names that read closest to the reference gear. The library is built from the user's personal exports — what isn't there isn't available.

### 4. Get exact param names — REQUIRED step

For each chosen block:
```bash
helixgen show-block "<display name>"
```

Skipping this is the #1 way to waste a generation cycle. Param names are case-sensitive (`Treble` vs `Tone`), tone-stack labels vary by amp, and the generator rejects unknown keys with a list of valid ones.

### 5. Write the spec

Save as `/tmp/<slug>.json` with the shape documented in CLAUDE.md.

Tuning heuristics (good starting points, not laws):

| Knob | Range | Notes |
|------|-------|-------|
| Drive `Gain` (pedal as boost) | 0.30–0.50 | Pushes amp into more saturation |
| Drive `Gain` (pedal as distortion) | 0.60–0.85 | Drive does most of the work |
| Amp `Drive` | 0.40–0.60 rhythm clean-edge, 0.60–0.80 crunch, 0.80+ lead | |
| Amp `Master` | 0.40–0.60 | Higher = more power-amp sag |
| Cab | usually no params (pre-baked IR) | |
| Delay `Mix` | 0.10–0.20 rhythm, 0.20–0.35 lead | |
| Delay `Feedback` | 0.20–0.35 | Higher = longer repeats |
| Reverb `Mix` | 0.08–0.15 | Stadium plates sit louder than they look |

Amp-EQ tweaks for the user's specific guitar (apply to whichever amp params actually exist — check `show-block` first):

| Guitar | Pickups | Typical adjustments |
|--------|---------|---------------------|
| Fender Strat / similar | bright SC | bump `Treble` to 0.65–0.75, `Presence` to 0.60–0.70; can run more amp gain (SCs compress less) |
| Fender Tele | bright SC, sharper | same as Strat but pull `Bass` to ~0.45 to avoid flubby low end |
| Gibson Les Paul / SG | warm HB | pull `Treble` to 0.55–0.60, `Presence` to 0.50–0.55; HBs already push the amp, back amp `Drive` off ~0.10 |
| Ibanez Prestige (RG/AZ/S) | hot HB, tight low-mids | as LP/SG but you can run `Treble` slightly higher (0.60–0.65); these excel at fast tight runs, keep `Mid` ~0.60 for cut |
| ES-335 / hollow / semi-hollow | warm HB, more body | pull `Bass` to ~0.45 to avoid boom; `Master` ~0.45 to control feedback |
| PRS / generic HB | balanced HB | midpoint of Strat and LP — start at amp defaults and adjust from ear |
| Bass guitar | varies | more `Bass`, less `Mid`; back `Master` off to keep cab tight |

### 5.5. Snapshots (when the user wants multiple scenes in one preset)

Stadium presets support 8 snapshots — named scenes that override block bypass and param values without leaving the preset. Use them when the user asks for "rhythm + lead", "verse + chorus + solo", "clean + crunch + lead", etc.

Spec extension (top-level `snapshots` array, up to 8 entries):

```json
"snapshots": [
  {"name": "Rhythm"},
  {"name": "Lead",  "params": {"Brit Plexi Brt": {"Drive": 0.85, "Master": 0.7},
                               "Tape Echo Stereo": {"Mix": 0.30}}},
  {"name": "Clean", "disable": ["Compulsive Drive"],
                    "params": {"Brit Plexi Brt": {"Drive": 0.30}}}
]
```

Rules:
- Each snapshot is a *delta* from the base path values. Plain `{"name": "X"}` means "use all base values" — that's snapshot 1 typically.
- `disable: [...]` bypasses a block in that snapshot (matched by display_name).
- `params: {block: {p: v}}` overrides param values in that snapshot.
- Snapshot 1 (index 0) is the one that loads on hardware boot.
- Block names in `disable` / `params` must already exist in the path's `blocks`.
- Param names in `params` are validated like base params — run `show-block` if unsure.

Common patterns:
- **Rhythm/Lead**: lead = higher amp `Drive` + `Master`, +0.10 reverb `Mix`, +0.15 delay `Mix`
- **Clean/Crunch/Lead**: clean = `disable` drive(s), back amp `Drive` to ~0.25; crunch = base; lead = stack as above
- **Verse/Chorus/Solo**: verse = light delay/verb; chorus = same; solo = boost (raise amp `Drive` 0.10–0.15 and delay `Mix` 0.20→0.35)

**Need different amps across snapshots?** A single snapshot can't swap the amp model — only override knobs and bypass. If the user needs fundamentally different amps (clean Fender + hi-gain Mesa) AND wants to switch instantly without leaving the preset, place both amps (and matching cabs) in the chain and have each snapshot enable one amp+cab pair while bypassing the other. Keep this to 2 amp+cab pairs max so the chain stays under the 12-slot cap.

If the user doesn't ask for snapshots, skip this section — `snapshots: []` (or omit the field) leaves the device's snapshot slots named "Snap 1..8" with no per-scene variation.

### 6. Pick guitar-side settings

For the report (next step), specify the user's hands-on guitar settings to match the tone goal. Pickup choice and rolled-back knobs are part of the tone — telling them just the amp settings isn't enough.

Defaults by tone goal:

| Tone goal | Selector | Volume | Tone |
|-----------|----------|--------|------|
| Aggressive rhythm/lead | bridge | 10 | 10 |
| Singing lead (Slash-style) | bridge | 10 | 7–8 (round off the edge) |
| Mellow / woman tone | neck | 10 | 4–6 |
| Clean breakup | bridge or neck | 6–8 (back off to clean it up) | 10 |
| Chimey clean (Strat) | middle or position 2/4 | 10 | 8–10 |
| Jazz / hollow body | neck | 7–9 | 5–7 |
| Funk single-note | bridge or position 2 | 10 | 10 |

If the user named a specific guitar, adjust:
- **Ibanez RG/Prestige with 5-way** → bridge is position 1 (HB), neck is position 5 (HB); positions 2/3/4 split-coil for SC-like tones if installed
- **Tele with 3-way** → bridge (back), middle (both), neck (front)
- **Les Paul/SG with 3-way** → rhythm (neck), middle (both), treble (bridge)

### 7. Generate

```bash
helixgen generate /tmp/<slug>.json -o /tmp/<slug>.hsp
```

If the validator errors with `Unknown param(s) [...]`, that's the signal to re-run `show-block` and fix the spec — never guess the corrected name.

### 8. Report back

Tell the user, in this order:
1. **The chain** — one short line per block (position, model, the 2–3 settings that matter for this tone)
2. **Snapshots** (only if the spec has them) — one line per snapshot summarizing what differs from base, e.g. `Lead: amp Drive 0.85, delay Mix 0.30; Clean: drive bypassed, amp Drive 0.30`
3. **Guitar settings** — one line: `Selector: <position> · Volume: <0–10> · Tone: <0–10>` plus a one-clause note if the goal requires a non-obvious knob move (e.g. "roll volume to 7 for the verse, 10 for the chorus")
4. **File path + how to load** — `/tmp/<slug>.hsp` (move it somewhere durable if you want to keep it), then: *"Open Line 6's HX Edit, connect your device via USB, and import the file."*
5. **One concrete tweak** they can try after loading (e.g. "if it's too dark, raise Treble to 0.65"; "for a thicker lead, push Tape Echo Mix to 0.25")

Don't hedge with a list of 5 things to maybe try; pick one.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Guessing param names | Always run `show-block` before writing the spec |
| Recommending a block not in the user's library | Always verify with `list-blocks` first |
| Stacking too much gain | Drive `Gain` + amp `Drive` compound; back one off |
| Forgetting a cab | Output is dry/fizzy without one; place after the amp |
| Heavy reverb defaults | Stadium plates run hot; start at 0.10 |
| Asking 5 clarifying questions | Cap at 3, only what's actually missing |
| Reporting only amp settings, not guitar settings | The selector + volume + tone knobs are part of the tone; include them in the report |
| Generic guitar advice that ignores the named guitar | If the user said "Strat", say "middle/position 4"; if "Les Paul", say "treble (bridge)" — match the actual switch language |
| Forcing one preset per role when snapshots fit | If the user wants "rhythm and lead" or "verse/chorus/solo", build ONE preset with snapshots, not multiple `.hsp` files |
| Snapshot referencing a block name that isn't in the path | `disable` / `params` only see blocks the path actually places; add the block to the path first (even if it'll be bypassed in some snapshots) |

## Quick Reference

```bash
# Survey
helixgen list-blocks --category <cat> | grep -i <kw>
helixgen show-block "<display name>"

# Generate
helixgen generate /tmp/spec.json -o /tmp/preset.hsp
```

Spec shape, full CLI vocabulary, and chassis/output-format notes are in CLAUDE.md.
