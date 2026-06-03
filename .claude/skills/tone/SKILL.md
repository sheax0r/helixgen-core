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

When NOT to use: editing an existing `.hsp` (load and modify directly outside this skill); ingesting new blocks (CLI's `helixgen ingest`); answering "what blocks do I have?" — just call `list_blocks` directly without the rest of the workflow.

## Prerequisites

- A helixgen MCP server is reachable. The plugin's bundled `.mcp.json` spawns it via `python -m mcp_server` over stdio, which requires the `helixgen` Python package to be importable in that Python env (see the `setup` skill's verify-installed step).
- The server's library must be populated. Verify quickly with `list_blocks(category="amp")` — empty result means no blocks ingested and the server's deployer needs to fix that before tone work is possible.

## MCP tool surface

| Tool | Args | Returns |
|---|---|---|
| `list_blocks` | `category?` (amp/cab/drive/delay/reverb/modulation/filter/eq/dynamics/pitch/volume/send) | text, grouped by category, one `<display_name>  [<model_id>]` per line |
| `show_block` | `name_or_id` (display name, model id, or alias) | text: header, category, aliases, params with types/defaults/ranges |
| `generate_preset` | `spec` (inline JSON dict — full helixgen schema) | `EmbeddedResource` with base64-encoded `.hsp` blob |
| `list_irs` | — | text, one `<hash>  <wav-path>` per registered IR; empty on the public deploy |

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
| Different families, OK to switch presets between parts | **Multiple presets** — generate one `.hsp` per part, name them clearly (e.g. `<song>-verse`, `<song>-chorus`). Switch presets on-device between parts. |
| Different families, need instant switching mid-song | **One preset with layered amps + snapshot bypass.** Place both amps (and both cabs, if different) in the chain; each snapshot enables one amp+cab pair and bypasses the other. Limited by the 12-slot per-path cap — don't go past 2 amps + 2 cabs. |

Default to "multiple presets" when the user says "different sounds" and doesn't specify needing instant switching — it's the simpler spec and the device's preset-switching is fast enough for between-song or between-section transitions in most material.

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

**Check for user IRs (memory-gated).** Call `list_irs()`. If the result is non-empty AND a feedback memory says the user prefers IRs over stock cabs when available, look for an IR that matches the chain's tonal target:

- Parse the wav filenames in the output — commercial IR packs encode cab + mic + position (e.g. `YA VX30 212 BLU Mix 01.wav` → Vox AC30-style 2x12 Blue, mix-position).
- If a match exists, use an IR block instead of a stock cab:
  ```json
  {"block": "With Pan", "ir": "YA VX30 212 BLU Mix 01.wav",
   "params": {"HighCut": 6500, "LowCut": 90, "Mix": 1.0}}
  ```
- Anti-fizz baseline (Hi Cut 6500–7000, Low Cut 80–100) still applies — set on the IR block itself.
- New users (no preference memory) get stock cabs by default. The preference flips on when the user explicitly says "from now on, prefer IRs when I have them" (and you save a feedback memory).

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

#### Anti-fizz baseline — bake these into nearly every preset

The Helix gives raw modeling and trusts you to voice it. A Spark/JC-120/etc. sounds "nice" out of the box because it's doing fixed cab voicing, EQ-curve baking, and mild compression for you. Without those, default Helix presets sound fizzy and thin compared to a real amp pushing real air. The cab block is where you fix this — verify exact param names with `show_block` (older cabs may use `Hi Cut` / `Lo Cut`; newer ones `High Cut` / `Low Cut`).

- **Cab `Hi Cut`** at **6500–7000 Hz** for amped tones; 7500–8000 Hz for sparkling cleans. Real V30s/Greenbacks have nothing above ~6 kHz; modeled cabs let fizz through to 10 kHz+. This single move kills ~70% of "modeller harshness."
- **Cab `Low Cut`** at **80–100 Hz** to clear out flub (60 Hz for bass / 7-string).
- **Mic choice** (cab `Mic` param): the default is usually `57 Dynamic` on-axis at the cap — engineered to slice through a live mix, not to sound pleasant solo. For "amp in the room" smoothness, prefer a ribbon (`121 Ribbon`, `160 Ribbon`) or any cab variant whose display name calls out a ribbon mic or an off-axis position.
- **Optional Parametric EQ** cutting **2–4 dB around 3–4 kHz** (medium Q) if Hi Cut alone doesn't kill the "ice pick" zone. A small cut around 800 Hz–1 kHz helps with boxiness.
- **Optional front-of-chain comp** (LA Studio Comp, light setting — only ~1–2 dB of gain reduction, **before** the amp) gives the "polished, baked-in" feel modeled presets often lack. Skip if the user wants pure raw dynamics.

If the cab the user picked has no Hi/Low Cut params (rare on Stadium), do the cuts with a Simple EQ block placed right after the cab.

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
- Param names in `params` are validated like base params — run `show_block` if unsure.

Common patterns:
- **Rhythm/Lead**: lead = higher amp `Drive` + `Master`, +0.10 reverb `Mix`, +0.15 delay `Mix`
- **Clean/Crunch/Lead**: clean = `disable` drive(s), back amp `Drive` to ~0.25; crunch = base; lead = stack as above
- **Verse/Chorus/Solo**: verse = light delay/verb; chorus = same; solo = boost (raise amp `Drive` 0.10–0.15 and delay `Mix` 0.20→0.35)

**Need different amps across snapshots?** A single snapshot can't swap the amp model — only override knobs and bypass. If the user needs fundamentally different amps (clean Fender + hi-gain Mesa) AND wants to switch instantly without leaving the preset, place both amps (and matching cabs) in the chain and have each snapshot enable one amp+cab pair while bypassing the other. Keep this to 2 amp+cab pairs max so the chain stays under the 12-slot cap.

If the user doesn't ask for snapshots, skip this section — omitting the field leaves the device's snapshot slots named "Snap 1..8" with no per-scene variation.

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

Call `generate_preset(spec=<the dict you built in step 5>)`. The return value is an MCP `EmbeddedResource` containing the `.hsp` bytes as a base64 blob.

If the validator errors with `Unknown param(s) [...]`, re-run `show_block` on the offending block, fix the spec, retry. Never guess the corrected name.

If you're running in Claude Code (local MCP server, the user expects a file on disk to load via HX Edit), extract the blob to `/tmp/<slug>.hsp` so the load step has a real path:

```bash
python3 -c "
import base64
b64 = '<the blob string from the EmbeddedResource>'
open('/tmp/<slug>.hsp', 'wb').write(base64.b64decode(b64))
"
```


### 8. Report back

Tell the user, in this order:
1. **The chain** — one short line per block (position, model, the 2–3 settings that matter for this tone)
2. **Snapshots** (only if the spec has them) — one line per snapshot summarizing what differs from base, e.g. `Lead: amp Drive 0.85, delay Mix 0.30; Clean: drive bypassed, amp Drive 0.30`
3. **Guitar settings** — one line: `Selector: <position> · Volume: <0–10> · Tone: <0–10>` plus a one-clause note if the goal requires a non-obvious knob move (e.g. "roll volume to 7 for the verse, 10 for the chorus")
4. **The file** — `/tmp/<slug>.hsp` saved locally. *"Open Line 6's HX Edit, connect your device via USB, and import that file."* Per user preference, run `open -R "/tmp/<slug>.hsp"` so it's pre-selected in Finder.
5. **One concrete tweak** they can try after loading (e.g. "if it's too dark, raise Treble to 0.65"; "for a thicker lead, push Tape Echo Mix to 0.25")

Don't hedge with a list of 5 things to maybe try; pick one.

### 9. Iterate on feedback (when the user loads it and says it's not quite right)

After the user loads the preset and reports back ("the lead is too compressed", "verses are too dark", "swap that delay for something slappier", "clean snapshot needs a touch of reverb"), don't start over. Keep the prior spec dict in mind, make the smallest edit that addresses the feedback, call `generate_preset` again with the updated spec, and tell the user what changed in one line so they can A/B.

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
- **"Swap X for something Y"** → call `list_blocks(category=<cat>)`, scan for candidates, `show_block` the chosen one, edit the spec, regenerate

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
| Reporting only amp settings, not guitar settings | The selector + volume + tone knobs are part of the tone; include them in the report |
| Generic guitar advice that ignores the named guitar | If the user said "Strat", say "middle/position 4"; if "Les Paul", say "treble (bridge)" — match the actual switch language |
| Forcing one preset per role when snapshots fit | If the user wants "rhythm and lead" or "verse/chorus/solo", build ONE preset with snapshots, not multiple files |
| Snapshot referencing a block name that isn't in the path | `disable` / `params` only see blocks the path actually places; add the block to the path first (even if it'll be bypassed in some snapshots) |
