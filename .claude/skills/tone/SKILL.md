---
name: tone
description: Use when the user asks for a guitar/bass tone targeted at a specific artist, song, genre, or feel (e.g. "lead in White Limo by Foo Fighters", "warm jazz clean", "thrash rhythm"). Drives the helixgen MCP server (auto-spawned via the plugin's bundled `.mcp.json`) to design and generate a Helix Stadium `.hsp` preset.
---

# Tone

## Overview

Turn a tone description into a `.hsp` Helix Stadium preset that's ready to load on the device. Drive the helixgen MCP server: survey blocks, pick a chain, verify exact param names, build a spec dict, call `generate_preset`, deliver the file.

## When to Use

- User describes a target tone (artist, song, section, genre, vibe)
- User wants a starting point to A/B against a reference
- User mentions a guitar/bass and a role (rhythm, lead, clean, pad, solo boost)

When NOT to use: editing an existing `.hsp` (load and modify directly outside this skill); ingesting new blocks (CLI's `helixgen ingest`); answering "what blocks do I have?" — just call `list_blocks` directly without the rest of the workflow; **putting an authored preset onto the physical Helix over the LAN, or syncing a library to the device** — that's the `device` skill (install / slots / backup), which picks up where this skill's saved `.hsp` leaves off.

## Prerequisites

- A helixgen MCP server is reachable. The plugin's bundled `.mcp.json` spawns it via `python -m mcp_server` over stdio, which requires the `helixgen` Python package to be importable in that Python env (see the `setup` skill's verify-installed step).
- The server's library must be populated. Verify quickly with `list_blocks(category="amp")` — empty result means no blocks ingested and the server's deployer needs to fix that before tone work is possible.

## MCP tool surface

| Tool | Args | Returns |
|---|---|---|
| `list_blocks` | `category?` (amp/cab/drive/delay/reverb/modulation/filter/eq/dynamics/pitch/volume/send) | text, grouped by category, one `<display_name>  [<model_id>]` per line |
| `show_block` | `name_or_id` (display name, model id, or alias) | text: header, category, aliases, params with types/defaults/ranges |
| `generate_preset` | `model`, `recipe` (inline JSON dict — full helixgen schema), `out_path` | `{path, warnings}` — the `.hsp` is written to `out_path` |
| `list_irs` | — | text, one `<hash>  <wav-path>` per registered IR; empty on the public deploy |

## Workflow

### 1. Clarify only what's missing

Ask at most 3 short questions, and only the ones the request didn't already answer. Common gaps:

- **Guitar** (single-coil / humbucker / acoustic / bass; specific model if mentioned)
- **Role(s)** — single role (rhythm / lead / clean / pad / solo boost), or multiple. If multiple, **ask the family question** (see 1a below).
- **Reference specifics** (which section of a song; live vs studio version)

If the request implies an answer ("lead in X" → role known; "Strat" → single-coil known), skip that question.

#### 1a. Multi-part disambiguation (only when there are 2+ roles/sections)

When the user wants multiple parts of one song, multiple roles, or multiple sections, first pin down **how many distinct sounds** are actually in play. The unit is a distinct guitar **sound**, not a song section — a six-section song played on three tones is three parts, not six. Signals that mark a new part: a gain/saturation shift (clean → edge-of-breakup → crunch → high-gain — the most reliable boundary), a rhythm/lead role shift, an effect that switches on/off with the section (chorus on a clean verse, a bigger delay on the solo), or a channel/amp swap in the source rig. Merge any two candidate sections that would be reached by the same amp+cab with only knob/effect-bypass differences. If the true count still exceeds 8, keep the 8 the player actually needs and push the overflow to a separate preset.

Then ask one focused question:

> "Do these parts share an amp/cab family (e.g. all British crunch, just different gain/effects per part), or are they fundamentally different sounds (e.g. clean Fender for verse, high-gain Mesa for chorus)?"

Then pick the path:

| Answer | Approach |
|--------|----------|
| Same family | **One preset, multiple snapshots.** Pick a chain that fits all parts, vary gain/EQ/effect bypass per snapshot. (See 5.5.) |
| Different families | **One preset, layered amps + snapshot bypass — the default.** Place both amps (and both cabs, if different) in the chain; each snapshot enables one amp+cab pair and disables the other. Capped at 2 amp+cab pairs, 12 blocks/lane, 8 snapshots. (See 5.5.) |

Default to the **layered-snapshot preset** for "different sounds" — even when the user hasn't said they need instant mid-song switching — because it delivers every part in one file the player can recall live. Fall back to **multiple presets** (one `.hsp` per part, named `<song>-<part>`) only when the layered approach won't fit the budget: more than 2 amp+cab pairs needed, the lane would exceed 12 blocks, or you'd need more than 8 snapshots.

#### 1b. Research the reference sound — REQUIRED for artist/song/specific-gear targets

When the target is a named artist, song, section, or specific piece of gear, **do web research before sketching the chain** (step 2). Don't rely on model memory alone — signature tones often hinge on one non-obvious detail (a specific pedal, an octave generator, an unusual amp pairing, a studio trick) that memory gets wrong or omits, and getting it wrong wastes the whole generation.

Use `WebSearch` / `WebFetch` (or dispatch a research subagent for deep cases). Search for the rig and the *sound*, e.g. `"<artist> <song> guitar tone gear amp pedals"`. Extract and note:

- **Amps / cabs** actually used (model + which channel if known)
- **Pedals / effects** in the signal path — especially anything that defines the character (fuzz, octave/POG, Whammy, modulation)
- **Guitar / pickups** if it shapes the tone (single-coil vs humbucker, specific instrument)
- **Tonal adjectives** from how people describe it (bright/biting, woolly, scooped, saggy, etc.)
- **Signature technique** that's part of the sound (palm-muting, octave riffing, heavy vibrato)

State a one-line summary of what you found back to the user before committing to the chain, and **cite sources** (markdown links) so they can verify. Fold the findings into the chain sketch in step 2 and the IR/cab choice in step 3.

**Skip research only when** there's nothing specific to research — a generic vibe target ("warm jazz clean", "thrash rhythm" with no artist) — or the user already named the exact gear/chain they want. When skipping, it's because the target is generic, not because you "probably know it."

### 2. Sketch the chain in one line

Based on the reference AND the user's guitar, pick a slot shape. The guitar shapes choices upstream of EQ — e.g. a Strat into a Plexi needs less treble-pull at the amp than a Les Paul into the same Plexi.

State your call briefly so the user can redirect before you commit:

- Classic rock: light drive → plexi-style amp → 4x12 → tape echo → plate
- Modern metal: tube-screamer boost → high-gain amp → 4x12 V30 → noise gate front
- Clean: comp → clean amp (AC15/Twin/Deluxe-style) → 1x12 → optional chorus → plate/spring
- Lead: stack drive higher → less compression → longer delay → bigger verb
- Bass: comp → bass amp → 4x10/8x10 → optional drive parallel

### 3. Pick blocks from the library

For each slot, call `list_blocks(category=<cat>)` and scan the output for display names that read closest to the reference gear. Categories are amp / cab / drive / delay / reverb / modulation / filter / eq / dynamics / pitch / volume / send.

Cab pick matters a lot for "is this fizzy or musical":

- **V30-style cabs** (`4x12 V30`, `Cali V30`, etc.) are bright, aggressive, and great for tight modern rhythm — but harsh-by-default for cleans, leads, and classic-rock. **Greenback** or **Silver Bell** variants are smoother and feel more like "amp in the room." Prefer them for clean, blues, classic-rock, and lead chains when the library has them.
- Cab variants with a **ribbon mic** in the name (`R121`, `R84`, `121 Ribbon`, `160 Ribbon`) or with `Off-Axis` / `Edge` in the position are much smoother than the default `SM57 On-Axis Cap` rendering. Prefer them for anything that should sound polished.
- The fine-grained Hi Cut / Low Cut / mic moves live in step 5 — picking the right cab here saves you from fighting it later.

**Check for user IRs (preference-gated).** Call `list_irs()`. If the result is non-empty, check whether the user prefers IRs over stock cabs: read `favor_irs` from `~/.helixgen/preferences.json` if that file exists; if the file or the key is absent, fall back to the existing feedback-memory check (a saved memory saying the user prefers IRs over stock cabs). When either source says yes, look for an IR that matches the chain's tonal target:

- Parse the wav filenames in the output — commercial IR packs encode cab + mic + position (e.g. `YA VX30 212 BLU Mix 01.wav` → Vox AC30-style 2x12 Blue, mix-position).
- If a match exists, use an IR block instead of a stock cab:
  ```json
  {"block": "With Pan", "ir": "YA VX30 212 BLU Mix 01.wav",
   "params": {"HighCut": 6500, "LowCut": 90, "Mix": 1.0}}
  ```
- Anti-fizz baseline (Hi Cut 6500–7000, Low Cut 80–100) still applies — set on the IR block itself.
- New users (no `favor_irs` preference and no feedback memory) get stock cabs by default. The preference flips on when the user explicitly says "from now on, prefer IRs when I have them" — record it in `~/.helixgen/preferences.json`'s `favor_irs` key if you can write there, otherwise as a feedback memory.

### 4. Get exact param names — REQUIRED step

For each chosen block, call `show_block(name_or_id="<display name>")`.

Skipping this is the #1 way to waste a generation cycle. Param names are case-sensitive (`Treble` vs `Tone`), tone-stack labels vary by amp, and the generator rejects unknown keys with a list of valid ones. If `generate_preset` later returns an error containing `Unknown param(s)`, the tool description tells you the recovery: call `show_block` on the offending block, fix the spec, retry.

### 5. Build the spec dict

Construct the spec inline as a Python/JSON dict — no temp files involved. The schema is the same as the helixgen CLI spec.json (see CLAUDE.md at repo root).

Minimal shape:

```json
{
  "name": "Preset Display Name",
  "author": "...",
  "paths": [
    {
      "blocks": [
        {"block": "Compulsive Drive", "params": {"Gain": 0.45}},
        {"block": "Brit Plexi Brt",   "params": {"Drive": 0.7, "Master": 0.5}},
        {"block": "Mic Ir_4x12 Greenback 25 With Pan", "params": {"HighCut": 6800, "LowCut": 90}},
        {"block": "Tape Echo Stereo", "params": {"Mix": 0.18}},
        {"block": "Plate Stereo",     "params": {"Mix": 0.12}}
      ]
    }
  ]
}
```

#### Name the preset for its target guitar

Presets are named for the guitar they're voiced for. Append the **target
guitar** (resolved in step 6) to **both** the spec `"name"` (the display title)
**and** the save slug (step 7a filename), in the format `"<Tone Name> —
<Guitar>"` for the title and a sanitized equivalent for the `.hsp`/`.md`
filename — e.g. title `"White Limo Lead — Les Paul Jr"`, slug
`white-limo-lead-les-paul-jr`. Use a concise, recognizable guitar label — the
name as the user/prefs refer to it (`"Les Paul Jr"`, `"EC-1000"`,
`"Strandberg"`, `"Ibanez Prestige"`), not the full catalog name.

**Omit the guitar** (no suffix, plain tone name) **only** when the tone is
explicitly *not* targeted at a specific guitar — e.g. the user asked for a
guitar-agnostic or generic patch. In that case note in the report and the `.md`
that it's not guitar-specific.

(Guitar resolution happens in step 6; you can build the spec in step 5 with a
placeholder name and stamp the final `"<Tone> — <Guitar>"` title once the guitar
is settled — just make sure the generated preset and both files carry the
guitar-suffixed name.)

#### Anti-fizz baseline — bake these into nearly every preset

The Helix gives raw modeling and trusts you to voice it. A Spark/JC-120/etc. sounds "nice" out of the box because it's doing fixed cab voicing, EQ-curve baking, and mild compression for you. Without those, default Helix presets sound fizzy and thin compared to a real amp pushing real air. The cab block is where you fix this — verify exact param names with `show_block` (older cabs may use `Hi Cut` / `Lo Cut`; newer ones `High Cut` / `Low Cut`).

- **Cab `Hi Cut`** at **6500–7000 Hz** for amped tones; 7500–8000 Hz for sparkling cleans. Real V30s/Greenbacks have nothing above ~6 kHz; modeled cabs let fizz through to 10 kHz+. This single move kills ~70% of "modeller harshness."
- **Cab `Low Cut`** at **80–100 Hz** to clear out flub (60 Hz for bass / 7-string).
- **Mic choice** (cab `Mic` param): the default is usually `57 Dynamic` on-axis at the cap — engineered to slice through a live mix, not to sound pleasant solo. For "amp in the room" smoothness, prefer a ribbon (`121 Ribbon`, `160 Ribbon`) or any cab variant whose display name calls out a ribbon mic or an off-axis position.
- **Optional Parametric EQ** cutting **2–4 dB around 3–4 kHz** (medium Q) if Hi Cut alone doesn't kill the "ice pick" zone. A small cut around 800 Hz–1 kHz helps with boxiness.
- **Optional front-of-chain comp** (LA Studio Comp, light setting — only ~1–2 dB of gain reduction, **before** the amp) gives the "polished, baked-in" feel modeled presets often lack. Skip if the user wants pure raw dynamics.

If the cab the user picked has no Hi/Low Cut params (rare on Stadium), do the cuts with a Simple EQ block placed right after the cab.

**"It sounds fine while I play but harsh in the recording."** Common and not a patch-specific bug. Many interfaces (e.g. Focusrite 2i2) have a *direct monitor* that feeds the pre-A/D analog signal to the player's ears — it flatters and smooths the source. The recorded track is the honest signal; direct monitoring was hiding harshness that was always there. Don't chase it as a recording/DAW problem — **bake the fix into the patch**: apply the anti-fizz baseline (Hi Cut 6500–7000, prefer pre-balanced **Mix** IRs over Singles/Raw and over bright stock cabs), and judge by the recording, not the live monitor. A patch sitting at ~10 kHz Hi Cut with a stock V30 is the classic offender.

#### Tuning heuristics (good starting points, not laws)

| Knob | Range | Notes |
|------|-------|-------|
| Drive `Gain` (pedal as boost) | 0.30–0.50 | Pushes amp into more saturation |
| Drive `Gain` (pedal as distortion) | 0.60–0.85 | Drive does most of the work |
| Amp `Drive` | 0.40–0.60 rhythm clean-edge, 0.60–0.80 crunch, 0.80+ lead | |
| Amp `Master` | 0.40–0.60 | Higher = more power-amp sag |
| Cab `Hi Cut` / `High Cut` | 6500–7000 Hz amped, 7500–8000 Hz clean | The single biggest anti-fizz move; see baseline above |
| Cab `Low Cut` / `Lo Cut` | 80–100 Hz (60 for bass / 7-string) | Clears flub without thinning the body |
| Cab `Mic` | ribbon for smooth, `57 Dynamic` for cut | Default 57 on-axis is the harshest sane choice |
| Delay `Mix` | 0.10–0.20 rhythm, 0.20–0.35 lead | |
| Delay `Feedback` | 0.20–0.35 | Higher = longer repeats |
| Reverb `Mix` | 0.08–0.15 (up to 0.20 for sterile DI-feel rescues) | Stadium plates sit louder than they look |
| Comp before amp (optional) | ~1–2 dB gain reduction | Polished/Spark-like feel; skip for raw dynamics |

Amp-EQ tweaks for the user's specific guitar (apply to whichever amp params actually exist — check `show_block` first):

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

Stadium presets support 8 snapshots — named scenes that override block bypass and param values without leaving the preset. Use them when the user asks for "rhythm + lead", "verse + chorus + solo", "clean + crunch + lead", etc., or when 1a's part-count derivation turned up 2+ distinct sounds (same family or different).

**Keep the count lean.** Aim for the biggest **≤4** distinct parts — the ones the player actually needs to recall live. Only go up to the 8-snapshot hardware max when the user explicitly asks for more or the song genuinely has that many distinct sounds; don't pad to 8 by default.

**Name snapshots by sound, not song section** — `Clean` / `Crunch` / `Lead`, not `Verse` / `Chorus` / `Bridge`. Names are what shows on the Stadium scribble strip, and a player recalling a scene live thinks in tone, not arrangement.

**A solo boost is its own snapshot, not a footswitch.** It recalls a full lead voice — gain, EQ, delay, and reverb all moving together — which is exactly what a snapshot is for. Reserve footswitches (5.6) for single in-scene toggles.

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
- Param names in `params` are validated like base params — run `show_block` if unsure.

Common patterns:
- **Rhythm/Lead**: lead = higher amp `Drive` + `Master`, +0.10 reverb `Mix`, +0.15 delay `Mix`
- **Clean/Crunch/Lead**: clean = `disable` drive(s), back amp `Drive` to ~0.25; crunch = base; lead = stack as above
- **Clean/Crunch/Solo**: same as above, with the solo snapshot as the dedicated lead-boost scene (raise amp `Drive` 0.10–0.15 and delay `Mix` 0.20→0.35) rather than a footswitch

**Different amps across snapshots — the default when families differ (see 1a).** A single snapshot can't swap the amp model — only override knobs and bypass — so place both amps (and matching cabs) in the chain and have each snapshot enable one amp+cab pair while disabling the other. Keep this to 2 amp+cab pairs max so the chain stays under the 12-slot cap.

**Disable-only limitation — author every layered block base-ENABLED.** A snapshot can only `disable` a block; there is no `enable` field, so a block that's base-bypassed (`enabled: false`) can never be turned back on in a later snapshot. For a layered (different-amps) preset, place every amp/cab/drive it needs **base-enabled** in the path, and have each snapshot `disable` the complement — the pair(s) it isn't using that moment. Never set `enabled: false` at the base level on a block some snapshot needs lit up.

If the user doesn't ask for snapshots, skip this section — omitting the field leaves the device's snapshot slots named "Snap 1..8" with no per-scene variation.

### 5.6. Auto-wire controls (footswitches + expression)

By default, wire the chain for live use: give every toggle-able effect a footswitch and route any sweep-able pedal to an expression pedal. Shipping a preset with no live control is a miss, not a safe default. All of this is **research-overridable** — if step 1b turned up something that dictates a different set (e.g. "this tone only ever uses the one drive live, not the others"), follow the tone over the defaults below.

**Footswitches — chain order, top row then bottom:**
- Assign a **latching** footswitch to every drive/fuzz/boost, modulation, delay, reverb, and non-wah pitch/filter toggle, in signal-chain order. Walk the assignable switches in order: `FS1 → FS2 → FS3 → FS4 → FS5` (top row), then `FS7 → FS8 → FS9 → FS10 → FS11` (bottom row). **Skip `FS6` — it is the reserved MODE switch, not assignable** (and `FS12` is TAP/Tuner). This puts dirt near the low switches and time-based effects up top — the conventional live layout falls out for free.
- Skip amp, cab, EQ, comp/dynamics, and other always-on/utility blocks — they never get a footswitch. Tonal boosts belong in a snapshot (5.5), not a stomp.
- Cap at **10 assignable switches** (FS1–FS5, FS7–FS11). If more than 10 toggle-able blocks exist, wire the first 10 in chain order and tell the user in the report which ones were left un-switched.
- Use `momentary` only when the user explicitly asks for a hold gesture (e.g. a boost or pitch dive you only want while your foot is down); everything else is `latching`.

**Expression pedals — wah/whammy → EXP1, volume → EXP2:**
- Detect a pedal-controllable block by calling `show_block` and checking for a **`Pedal`** float param (0..1) — that's the real sweep param for every wah, `Pitch Wham`, and volume pedal in the library (e.g. `Teardrop 310 Mono`). Wah/expression blocks have **no `Position` param** (don't confuse it with the mic-`Position` knob on IR-cab `With Pan` blocks) — always confirm with `show_block` before writing the spec. Poly-pitch/int-`Interval` blocks are out of EXP v1 scope.
- Route a wah or whammy's `Pedal` to **EXP1**; route a volume block's `Pedal` to **EXP2**. If only a volume pedal is present (no wah/whammy), put it on EXP1 instead. Full `min: 0.0, max: 1.0` sweep by default.
- **Wah ships bypassed, engaged by the toe switch** — set `"enabled": false` on the wah block and assign its bypass to `"switch": "EXP1Toe"` (the real expression-pedal toe switch — push the pedal fully forward to click it on, then sweep with EXP1). This is the standard Helix wah behavior. Do **not** spend a regular `FS` slot on the wah, and do not count it against the FS budget. Unless research says the reference keeps the wah always inline.
- If the user already claimed a pedal (e.g. "EXP2 sweeps amp Master"), that wins; auto-routing only fills what's left, and skips a target it can't place — telling the user — rather than overriding the user's mapping.

**Snapshot/footswitch relationship:** a change that touches ≥2 blocks/params is a snapshot (5.5, including the solo snapshot); a single live on/off or sweep is a footswitch/EXP (here). Auto-wire footswitches and EXP even on a preset that already has snapshots — they're complementary, not competing: the snapshot sets the scene's base bypass state, and the footswitch toggles from there.

If the user says "no footswitches" or "leave the controls alone," skip this step.

### 5.7. Volume-normalization pass

A final level pass so the preset's loudness is sane and — especially when
replicating a reference — the **relative** loudness between parts/snapshots
tracks the source. helixgen never renders audio, so this sets **starting**
levels by rule of thumb; the user fine-tunes by ear on the device.

**Read the preferences first** (`~/.helixgen/preferences.json`). Two toggles,
both default on:
- `volume_normalize_baseline: false` → skip force 1 (the across-preset anchor).
- `volume_normalize_snapshots: false` → skip forces 2–3 (between-snapshot
  leveling). If both are false, skip this step and say so in the report.

**The knob:** `show_block` the amp and use its channel-volume param (`ChVol`, or
the amp's `Level` — the name varies, so confirm). Do **not** use `Master` (it
also changes power-amp sag/feel). Only if the amp has no channel-volume param,
add one end-of-chain volume block (from `list_blocks(category="volume")`) and
automate that. In a layered two-amp preset, level whichever amp is active in
each snapshot via that amp's own channel volume.

Apply three forces, in order:

1. **Anchor** (force 1, `volume_normalize_baseline`): set the reference part
   (usually rhythm) to a standard channel-volume anchor, default `~0.5` (leaves
   headroom, no clipping; adjust if `show_block` shows an unusual taper). Every
   preset anchoring its main part to the same value keeps presets at a
   consistent baseline. If research says the source should sit hotter/softer
   relative to its material, offset the anchor.
2. **Gain compensation** (force 2, `volume_normalize_snapshots`): more gain →
   more compression → louder *perceived* level at the same knob. So push
   **lower-gain parts up** to sit even — a clean/edge-of-breakup part usually
   needs its channel volume raised to match a high-gain rhythm; a very hot,
   highly-compressed rhythm may need a small trim.
3. **Intended dynamics** (force 3, `volume_normalize_snapshots`), relative to the
   rhythm anchor: **lead/solo ~+2–3 dB** (to cut through), **crunch ~= rhythm**,
   **clean = perceptually matched** (via force 2). When step-1b research reveals
   the source's actual part-to-part dynamics, those override these conventions.

**dB → param:** the knobs are 0–1 and we can't measure — use *a small channel-vol
nudge (~0.05–0.10) ≈ a couple dB* to turn intended dB deltas into starting
values. Per-snapshot moves become `params` overrides on the channel-volume param
(alongside the gain/EQ/effect deltas from 5.5); a base preset gets the anchor on
its base amp params.

### 6. Pick the instrument, then resolve its controls

For the report (next step), the user's hands-on guitar settings are part of the tone — pickup choice and rolled-back knobs shape the sound as much as the amp settings do.

Resolve the target guitar in this order (first hit wins). The resolved guitar
is **the target guitar** used to name the preset (title + filename + description
— see step 5 naming and step 7a):

**(a) A user-named guitar always wins.** If the user named a specific guitar,
use it. If research (1b) or the tone target suggests it's a poor fit, give
**one** honest nudge — not an argument — e.g. "the EC-1000's scooped active EMGs
will fight this vintage-crunch voicing — if you have it handy, the LP Jr's P-90
nails it more directly" — then proceed with the guitar they asked for.

**(b) Else, use `default_guitar` from preferences.** If no guitar was named and
`~/.helixgen/preferences.json` has a `default_guitar` set, use it — state it
briefly ("using your default guitar, the <X>"). Still give the one-nudge from
(a) if it's a poor fit for the tone.

**(c) Else, ask which guitar to use — and offer to save it.** When no guitar was
named and `default_guitar` is unset, **ask the user which guitar to use.** Offer
a best-fit suggestion from their owned lineup (read `instruments` from
preferences.json if present; otherwise the user's guitar memory — Les Paul Jr,
ESP LTD EC-1000, Strandberg Boden Essential 6, Ibanez Prestige) using the
pickup-class table below, and **offer to save their choice as `default_guitar`
in preferences.json** (confirm-first, per the setup skill's write-back rule) so
you won't have to ask next time. Only fall through to the generic tone-goal
table (further down) plus a single clarifying question when the lineup is
entirely unknown — no preferences file and no memory.

Match tone character to pickup class (this is the best-fit suggestion in (c),
and the nudge check in (a)/(b)):

| Tone target | Wants | Pick |
|---|---|---|
| Punk, garage, raw blues, vintage rock, early breakup, gritty midrange bark | P-90: hot single-coil, breaks up early | **Les Paul Jr** (single bridge P-90, no selector) |
| Modern metal, djent, tight scooped high-gain rhythm | Active humbucker: tight, scooped, high-output | **ESP LTD EC-1000** (active EMGs, 3-way) |
| Prog/fusion clarity, pristine clean needing sparkle, technical lead | Coil-split humbucker: HB for gain, split for SC clarity | **Strandberg Boden Essential 6** (HSS, 5-way with splits) |
| Classic rock, versatile hard rock, ambiguous mid-gain | Versatile HSH, bridge HB for gain | **Ibanez Prestige** (HSH, 5-way) |

Research (1b) beats the table when it names the reference's actual pickup type — match that class first, the table is the fallback for a generic target. Only name a runner-up when the top two are a genuine toss-up (one clause: "or the Prestige if you want it tighter and less hairy").

**Resolve controls, then translate into the selected guitar's real switch language.** Start from the tone-goal defaults:

| Tone goal | Selector | Volume | Tone |
|-----------|----------|--------|------|
| Aggressive rhythm/lead | bridge | 10 | 10 |
| Singing lead (Slash-style) | bridge | 10 | 7–8 (round off the edge) |
| Mellow / woman tone | neck | 10 | 4–6 |
| Clean breakup | bridge or neck | 6–8 (back off to clean it up) | 10 |
| Chimey clean (Strat-style) | middle or position 2/4 | 10 | 8–10 |
| Jazz / hollow body | neck | 7–9 | 5–7 |
| Funk single-note | bridge or position 2 | 10 | 10 |

Then translate the generic position into the guitar's actual switches — never say "middle position" for a guitar that doesn't have one:

- **Les Paul Jr** — no selector; single bridge P-90. Nothing to move — note pick attack instead (digging in near the bridge is the "selector" here).
- **ESP LTD EC-1000** — 3-way: rhythm (neck) / middle (both) / treble (bridge).
- **Strandberg Boden Essential 6** — 5-way, HSS: position 1 = bridge humbucker … position 5 = neck single; positions 2–4 include coil-splits. Flag "confirm your wiring if it differs."
- **Ibanez Prestige** — 5-way, HSH: position 1 = bridge HB, positions 2/4 = split in-betweens, position 3 = middle single, position 5 = neck HB.

Round out the recommendation with, where relevant: a **coil-split** call for the Strandberg/Prestige ("split the bridge for the clean verse's glassy top, full humbucker for the chorus push"), a one-clause **pick-attack** note (P-90 rewards digging in near the bridge; active EMGs want a tight palm mute and let the pickup compress; single-coil/split positions want a lighter touch to avoid brittleness), and a one-clause **"why this guitar"** tying pickup class to the tone.

If nothing is known about the user's lineup (no preferences file, no memory, no named guitar), fall back to the generic tone-goal table above with generic switch language, and ask one clarifying question only if the guitar is genuinely load-bearing for the tone.

**Snapshots stay on one instrument.** For a snapshot preset (5.5), the recommendation names a single guitar — the player isn't swapping guitars mid-song — and expresses per-scene differences as control moves on that one guitar (e.g. "split (Strandberg pos 4) + volume 7 for the clean verse snapshot, full bridge (pos 1) + volume 10 for the lead snapshot").

### 7. Generate

Call `generate_preset(model, recipe=<the dict you built in step 5>, out_path="<dir>/<slug>.hsp")` (`model` is the device model string, e.g. `"stadium_xl"`; pick `out_path` per the **Save location** note below). It writes the `.hsp` directly to `out_path` (creating parent dirs) and returns `{"path": ..., "warnings": [...]}` — no base64, no manual file extraction. Surface any `warnings` to the user.

If the validator errors with `Unknown param(s) [...]`, re-run `show_block` on the offending block, fix the spec, retry. Never guess the corrected name.

#### 7a. Write a companion markdown description — REQUIRED

Whenever you save a `.hsp`, also write a sibling markdown file at the same path with the same slug (`<dir>/<slug>.md` next to `<dir>/<slug>.hsp`). This is the durable, human-readable record of the tone so it stands alone without the chat. It's effectively the step-8 report, persisted. Include:

- **Title + target** — the tone name, the guitar it's voiced for, and what it's aiming at (artist/song/section/genre/feel). State the target guitar clearly near the top (omit it only when the tone is explicitly not guitar-specific — then say so)
- **Reference notes & sources** — the key findings from step 1b research, with the source links (omit if research was skipped because the target was generic)
- **The chain** — one line per block: position, model, and the 2–3 settings that matter
- **IRs referenced** — basenames, so the user knows what must be loaded on the device
- **Snapshots** — one line each (only if the spec has them)
- **Levels** — the intended relative balance line from step 8 (or that normalization was off per preferences)
- **Footswitches** — one line per assigned switch, rendered in **English name + position**, not a bare identifier (`Footswitch 1 (top row, 1st from left) → Compulsive Drive`, …). Get the English string from the `controller_mapping` MCP tool (or `helixgen controllers`) / `controllers.english_for_controller`. Only if the spec has them.
- **Expression** — one line per pedal mapping, also in English (`Expression Pedal 1 (onboard pedal, EXP 1) → Teardrop 310 Mono Pedal`, …), only if the spec has them
- **Recommended instrument** — a `## Recommended instrument` section (see step 6): **Pick**, **Why**, **Controls** (selector / volume / tone / coil-split if applicable / pick attack), **Second choice** (only on a genuine toss-up), **Note** (any lineup caveat, e.g. active-vs-passive TBD)
- **Tweaks** — the one concrete tweak from step 8, plus any obvious alternates

Keep it tight and scannable — it's reference material, not a transcript. If you regenerate/iterate on the preset (step 9), update this `.md` in place alongside the `.hsp`.

> **Save location:** default to writing both files wherever the user's convention puts presets. If a project/user preference (memory or a stated rule) names a presets directory, write the `.hsp` **and** `.md` there; otherwise `/tmp/<slug>.{hsp,md}`. The `<slug>` includes the target guitar (sanitized `"<Tone Name> — <Guitar>"`, e.g. `white-limo-lead-les-paul-jr.{hsp,md}`) — omit the guitar from the slug only when the tone is explicitly not guitar-specific. Reveal the `.hsp` in Finder per step 8.


### 8. Report back

Tell the user, in this order:
1. **The chain** — one short line per block (position, model, the 2–3 settings that matter for this tone)
2. **Snapshots** (only if the spec has them) — one line per snapshot summarizing what differs from base, e.g. `Lead: amp Drive 0.85, delay Mix 0.30; Clean: drive bypassed, amp Drive 0.30`
3. **Levels** (from 5.7) — one line on the *intended* relative balance, e.g. `rhythm anchor; lead +~2 dB; clean bumped to match (fine-tune by ear)`. If normalization was skipped by preference, say `Levels: normalization off per preferences`.
4. **Instrument** — `<guitar> — <one-clause why>` (skip the "why" if the user named the guitar themselves), then `Selector: <position> · Volume: <0–10> · Tone: <0–10>` in that guitar's real switch language, plus a one-clause note for any non-obvious move (roll-off, coil-split, pick attack)
5. **Controls** (only if 5.6 wired any) — render every controller in **English (name + physical position)**, never a bare `FS#`: the footswitch map (`Footswitch 1 (top row, 1st from left) → Compulsive Drive`, …), the expression routing (`Expression Pedal 1 → wah Pedal`, …), and any toe-switch engage (`Expression pedal toe switch → Teardrop 310 Mono (bypass)`). Use `controllers.english_for_controller` / the `controller_mapping` tool for the exact strings. Conversely, if the **user** describes a switch in plain language, run it through the small-model controller-translation sub-agent (fed `controller_mapping(stadium_xl)`) to get the canonical identifier before wiring it, and validate the result against the canonical set.
6. **The files** — the `.hsp` saved locally (plus its companion `<slug>.md` description from step 7a). *"Open Line 6's HX Edit, connect your device via USB, and import that file."* Per user preference, run `open -R "<path>/<slug>.hsp"` so it's pre-selected in Finder. If the user instead wants it pushed **straight onto the Stadium over the LAN** (no HX Edit), hand off to the `device` skill — but read that skill's template-precondition warning first; a live install is more involved than a file drop.
7. **One concrete tweak** they can try after loading (e.g. "if it's too dark, raise Treble to 0.65"; "for a thicker lead, push Tape Echo Mix to 0.25")

Don't hedge with a list of 5 things to maybe try; pick one.

### 9. Iterate on feedback (when the user loads it and says it's not quite right)

After the user loads the preset and reports back ("the lead is too compressed", "verses are too dark", "swap that delay for something slappier", "clean snapshot needs a touch of reverb"), don't start over. The `.hsp` you saved is the source of truth — make the smallest edit that addresses the feedback with a single in-place `patch_preset` call (see **Adjusting an existing tone** above; do NOT regenerate from the spec dict), and tell the user what changed in one line so they can A/B.

Rules of thumb for translating ear-language to param moves:
- **"Too compressed"** on a lead → back amp `Drive` off ~0.10, raise `Master`; or back drive pedal `Gain` off ~0.10
- **"Too dark"** → raise `Treble` 0.05–0.10, raise `Presence` 0.05; or change to a brighter amp variant if the EQ is already at ceiling
- **"Too bright / harsh"** → mirror of above (drop Treble/Presence), or pull cab `Hi Cut` down (e.g. 8000 → 6500)
- **"Fizzy / digital / not amp-in-the-room"** → most common Helix-vs-Spark complaint. In order: (1) cab `Hi Cut` to 6500–7000 and `Low Cut` to 80–100 if not already there; (2) switch the cab `Mic` to a ribbon variant or pick a smoother cab (V30 → Greenback / Silver Bell); (3) add a Parametric EQ cutting 2–4 dB at 3–4 kHz medium Q; (4) add a subtle comp (~1–2 dB GR) at the front of the chain. Apply in that order — usually step 1 alone fixes most of it
- **"Not enough body"** → raise `Bass` 0.05–0.10 or `Mid` 0.05; consider cab `Low Cut` 80 → 60
- **"Boomy / flubby"** → raise cab `Low Cut` (60 → 100), back `Bass` off
- **"Lead doesn't sing / cut"** → raise `Mid` 0.05–0.10 in the lead snapshot, raise delay `Mix` 0.05
- **"Delay is washy / too long"** → drop `Mix` 0.05 OR drop `Time` 0.05
- **"Reverb feels too loud"** → drop `Mix` 0.03–0.05 (Stadium plates run hot, small moves matter)
- **"Swap X for something Y"** → call `list_blocks(category=<cat>)`, scan for candidates, `show_block` the chosen one, then `swap_model` (same category) via a `patch_preset` op

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Guessing param names | Always call `show_block` before writing params for a block |
| Recommending a block not in the user's library | Always verify with `list_blocks(category=<cat>)` first |
| Stacking too much gain | Drive `Gain` + amp `Drive` compound; back one off |
| Forgetting a cab | Output is dry/fizzy without one; place after the amp |
| Cab with no `Hi Cut` / `Low Cut` set | Default modeled cabs let fizz through to 10 kHz+; set Hi Cut 6500–7000 and Low Cut 80–100 on nearly every preset (see step 5 anti-fizz baseline) |
| Trusting the default cab mic (SM57 on-axis at the cap) | Engineered to slice a live mix, harsh solo; prefer ribbon-mic variants for smoothness |
| Heavy reverb defaults | Stadium plates run hot; start at 0.10 |
| Asking 5 clarifying questions | Cap at 3, only what's actually missing |
| Reporting only amp settings, not the instrument recommendation | Selector + volume + tone (+ coil-split/pick-attack where relevant) are part of the tone; include them in the report (step 6, step 8 item 4) |
| Leaving a clean/low-gain part at the same level knob as a high-gain part and calling it balanced | High gain reads louder (more compression); push the lower-gain part's channel volume up to sit even — the volume-normalization pass (5.7), gain-compensation force |
| Generic guitar advice that ignores the named or auto-selected guitar | If the user said "Strat", say "middle/position 4"; for the user's own lineup use its real switches — LP Jr has no selector, EC-1000 is a 3-way (not 5-way), Strandberg/Prestige are 5-way with specific split positions |
| Defaulting to multiple presets when amp families differ | Default to ONE preset with layered amps + snapshot bypass instead (1a, 5.5); fall back to multiple presets only when it won't fit the 2-pair/12-block/8-snapshot budget |
| Bypassing a block at the base level that a later snapshot needs lit up | Snapshots can only `disable`, never `enable` — author every layered block base-ENABLED and disable the complement (5.5) |
| Naming snapshots after song sections | Name by sound (`Clean`/`Crunch`/`Lead`), not arrangement (`Verse`/`Chorus`) — that's what reads on the scribble strip (5.5) |
| Giving a solo boost its own footswitch | A solo/lead boost changes gain + EQ + delay/reverb together — that's a snapshot (5.5), not a stomp |
| Forcing one preset per role when snapshots fit | If the user wants "rhythm and lead" or "verse/chorus/solo" on one amp family, build ONE preset with snapshots, not multiple files |
| Snapshot referencing a block name that isn't in the path | `disable` / `params` only see blocks the path actually places; add the block to the path first (even if it'll be bypassed in some snapshots) |
| Shipping a preset with no live control | By default wire toggle-able blocks to footswitches and sweep-able blocks to EXP (5.6) — don't ship silent presets unless the user asked for hands-off |
| Using `Position` as the wah/expression sweep param | The real param is `Pedal` (float 0..1) on blocks like `Teardrop 310 Mono`; wah/expression blocks have no `Position` param (that name is the IR-cab mic knob) — always confirm with `show_block` (5.6) |
| Building an artist/song tone from memory | Research the real rig from the web first (step 1b) — signature tones hinge on non-obvious details; cite sources |
| Saving the `.hsp` without a description | Always write the companion `<slug>.md` (step 7a) next to the preset so the tone is documented standalone |
| Naming a preset without its target guitar | Append the target guitar to the title AND the `.hsp`/`.md` filename (`<Tone> — <Guitar>`, step 5 naming); omit it only when the tone is explicitly guitar-agnostic |

## Adjusting an existing tone (surgical edits)

When the user asks to *tweak* a tone you already generated (e.g. "brighter
cab", "swap to a Plexi", "more delay", "kill the reverb"), do NOT regenerate
from a fresh description. The `.hsp` is the source of truth — edit it in place
with a single `patch_preset` call. There is **no** decompile→edit-spec→
regenerate round-trip.

1. You already have the `.hsp` file's path (the one you saved, or an orphan the
   user imported) — no recovery or base64 step needed.
2. Call `patch_preset(model, hsp_path="<dir>/<slug>.hsp", operations=[...])` with
   the smallest set of ops that expresses the change (batch multiple changes into
   one call):
   - "brighter" → `set_param` on the cab `HighCut` (raise it).
   - "swap to a Plexi" → `swap_model` (old → new amp; same category required).
   - "kill the reverb" → `set_enabled` with `enabled: false` on the reverb block.
   - "add a delay" → `add_block` with the delay block, `after` the amp/cab.
3. `patch_preset` edits the file **in place** and returns `{"path": ...,
   "warnings": [...]}` — the user just re-imports the same file. To inspect the
   result in recipe shape, call `view_preset(model, hsp_path)` (read-only) on the
   same path.
4. Surface any `warnings` from `patch_preset` (e.g. dropped params on a swap)
   to the user.

Prefer one `patch_preset` call with multiple `operations` over several edits.
The `.hsp` file is the thing you mutate — the recipe/spec dict is author-input
only and is not read back as truth.

### Addressing duplicate blocks

When a preset has two blocks with the same name (e.g. two IR "With Pan" blocks,
one per lane, or a volume block per split lane), reference the specific one by
its coordinate: add `"pos": N` (and `"lane": 0|1`, `"path": 0|1`) to the
`patch_preset` operation or the snapshot/footswitch/expression reference. A bare
name only works when it is unique in the preset.

If `patch_preset` or `view_preset` refuses a preset (more than two parallel
splits, or an unknown routing block), tell the user it's an unsupported routing
shape rather than editing it blindly. If `patch_preset` warns that an IR hash
was passed through unregistered, mention the user must `register-irs` that WAV
to edit it locally.
