---
name: using-helixgen
description: Use when the user wants to design, generate, or modify a Helix Stadium preset (.hsp) via the helixgen CLI. Confirms device model, locates the IR library, and recalls IR-related preferences and gitignore rules before any helixgen invocation. Complements the `tone` skill (which handles the actual block / param choices) — this skill is the setup pass, `tone` is the tone-design pass.
---

# Using helixgen

## Overview

helixgen is a Python CLI that generates Line 6 Helix Stadium `.hsp` presets
from JSON specs. This skill is the *setup* pass for any helixgen session: it
ensures the right device-model, IR library, and user-preference context is
in hand before the agent calls the helixgen CLI or asks the `tone` skill to
choose blocks.

## When to use

- User asks to design, generate, or modify a Helix preset
- User mentions an IR (impulse response) by name
- User wants to register IRs (`helixgen register-irs` / `ir-scan`)
- A previously generated preset isn't loading on the device correctly

When NOT to use:
- Read-only questions ("what blocks do I have?") — just run `helixgen list-blocks`
- Tone-design questions where the device and IR setup is already established
  in this session — defer to the `tone` skill

## Before generating or modifying any preset

In order, every session that involves generating or modifying a preset:

### 1. Confirm the device model

Look up the existing user memory `user_device.md`. There are three cases:

- **Memory present and recent (≤ ~3 months old):** trust it; no need to ask.
- **Memory present but older:** confirm once with a one-liner: "Still on
  Stadium XL?" If yes, move on. If no, update memory.
- **Memory absent:** ask: "Which Helix do you have? Stadium, Stadium XL, or
  something else?" Record under `user_device.md`.

If the answer is *not* Stadium or Stadium XL, tell the user this project
supports the Stadium family only for now and stop — don't generate something
that won't load on their device.

### 2. Locate IR library if applicable

If the user mentions IRs or `With Pan`/IR cab blocks:

- Check memory for `user_ir_directory.md`.
- **If absent**, ask: "Where do your impulse responses live? (Provide a
  directory path.)" Record. If the directory has many IRs (>50), suggest
  `helixgen ir-scan <dir>` to bulk-cache hashes once.
- **If present**, proceed; don't re-ask. The user can edit the memory if
  they reorganize.

(On the hosted claude.ai deployment in the future, this step is replaced by
asking the user to drag IRs into the chat. That path doesn't run in local
Claude Code today; ignore for now.)

### 3. Recall IR preferences

Check memory for `project_ir_notes.md` (if present). Use those one-line
tonal notes when choosing which IR to suggest. Examples of useful entries
the agent should be writing here over time:

- `- YA DXVB 112 Mix 03 — vintage Marshall-leaning, bright top; user reaches
  for it on clean tones`
- `- OH SLO V30 Cap 02 — modern high-gain; sits well in thrash rhythm`

### 4. Check the no-paid-IRs rule

Always recall `feedback_no_paid_irs_in_repo.md`. The user's IR collection
includes commercial packs that **must never be committed** to the repo or
pasted into test fixtures. Tests use synthesized WAVs from `_write_synth_wav`.

## When the user mentions an IR you haven't seen before

### 1. Try web research — only for known commercial-pack prefixes

If the basename matches a known commercial pack prefix:

| Prefix | Pack |
|--------|------|
| `YA `  | York Audio |
| `OH `  | Ownhammer |
| `3SP ` | 3 Sigma |
| `CTC ` | Celestion |
| `MJ `  | Mikko Jaakkola |

…web-search `<pack name> <basename> tonal description` to find what
amp/cab/mic combination it models and its character.

### 2. NEVER invent tonal descriptions from basename pattern-matching

`DXVB` does not "suggest a Diezel VH4" just because it starts with D. If
web research returns nothing high-confidence, **do not describe the IR
from the filename alone**. Ask the user: "What's `<basename>` meant for?
Any specific tones you reach for it for?"

### 3. Record findings

Add a one-line entry to `project_ir_notes.md` keyed by basename. Keep
each entry to one sentence; the file should stay scannable.

## Interaction with the `tone` skill

`tone` covers tone-design choices (anti-fizz baseline, drive stacking,
cab/mic selection, snapshot strategy). This skill is the *setup* — model
verification, IR-library awareness, fixture-policy awareness.

Order when both apply:

1. `using-helixgen` (this skill) runs first: confirm model + locate IR
   library + recall preferences + remember no-paid-IRs.
2. `tone` runs second: actually pick blocks, params, snapshots.

Both skills can write `project_ir_notes.md` — this skill on first
encounter, `tone` when it learns something new about an IR's use.

## After generating a preset that uses user IRs

Tell the user, in one sentence:

> "Make sure these IRs are loaded on your Stadium via the Librarian → Cab
> IRs → Import before you load this preset, or the IR block will show
> 'No Model'."

…then list the IR basenames the preset references so the user can verify.
Use `open -R "<path-to-hsp>"` to reveal the generated preset in Finder
(per the `feedback_reveal_file_in_finder.md` rule).

## What this skill does NOT enforce

This skill is **advisory**. The agent can technically skip these steps if
the user pushes for speed ("just generate it"). When that happens:

- Still surface the upload-to-device reminder after generation
- Still respect the no-paid-IRs rule (hard requirement, not advisory)
- Do not silently fill in `user_device.md` with a guess; better to ask
  even when rushed

The `model` parameter on future MCP tools backs the device check on the
hosted side; locally, this skill is the only check. Do not promise the
user something this skill cannot deliver.
