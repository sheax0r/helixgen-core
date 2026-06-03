---
name: setup
description: Use when the user wants to design, generate, or modify a Helix Stadium preset (.hsp) via helixgen, or when they want to register IRs. Verifies the helixgen package is importable, confirms device model (Stadium / Stadium XL only), locates the IR library, and recalls IR-related preferences. Runs before the `tone` skill picks blocks or params.
---

# helixgen setup

## Overview

This skill is the *setup* pass for any helixgen session. It makes sure the
agent has the right device-model, working helixgen install, IR library, and
IR-preference context before the `tone` skill starts picking blocks (or
before the agent invokes any `helixgen` MCP tool).

## When to use

- User asks to design, generate, or modify a Helix preset
- User mentions an IR (impulse response) by name
- User wants to register IRs
- A previously generated preset isn't loading on the device

When NOT to use:
- Read-only questions ("what blocks do I have?") — just call the
  `list_blocks` MCP tool directly.

## Before generating or modifying any preset

In order, every session:

### 0. Verify helixgen is installed

Check whether the `mcp__helixgen__*` tools appear in the agent's tool list.
If the MCP server didn't register tools, the helixgen package isn't
importable in the MCP server's Python env. Tell the user, in one line:

> "helixgen isn't installed in the MCP server's Python env. Run
> `pip install git+https://github.com/sheax0r/helixgen.git@stable`
> to install it (or let me run it for you after granting Bash permission)."

If they grant permission, run the pip install via Bash. The MCP server
needs to restart before the tools appear — tell them to `/restart` (or
quit and reopen Claude Code). Don't auto-install silently — `pip install`
is a system-affecting action and the user should see what's happening.

### 1. Confirm the device model

Look up the existing user memory `user_device.md`. Three cases:

- **Memory present and recent (≤ ~3 months old):** trust it; no need to ask.
- **Memory present but older:** confirm once with a one-liner: "Still on
  Stadium XL?" If yes, move on. If no, update memory.
- **Memory absent:** ask: "Which Helix do you have? Stadium, Stadium XL, or
  something else?" Record under `user_device.md`.

If the answer is *not* Stadium or Stadium XL, tell the user helixgen
supports the Stadium family only for now and stop — don't generate
something that won't load on their device.

### 2. Locate IR library if applicable

If the user mentions IRs or `With Pan`/IR cab blocks:

- Check memory for `user_ir_directory.md`.
- **If absent**, ask: "Where do your impulse responses live? (Provide a
  directory path.)" Record. If the directory has many IRs (>50),
  bulk-cache hashes in one round-trip via the `register_irs` MCP tool.
- **If present**, proceed; don't re-ask. The user can edit the memory if
  they reorganize.

### Registering a single IR mid-conversation

If the user names one specific WAV, call the `register_ir` MCP tool — one
round-trip, no Bash permission prompt.

### 3. Recall IR preferences

Check memory for `project_ir_notes.md` (if present). Use those one-line
tonal notes when choosing which IR to suggest. Examples:

- `- YA DXVB 112 Mix 03 — vintage Marshall-leaning, bright top; user reaches
  for it on clean tones`
- `- OH SLO V30 Cap 02 — modern high-gain; sits well in thrash rhythm`

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

Add a one-line entry to `project_ir_notes.md` keyed by basename. Keep each
entry to one sentence; the file should stay scannable.

## After generating a preset that uses user IRs

Tell the user, in one sentence:

> "Make sure these IRs are loaded on your Stadium via the Librarian → Cab
> IRs → Import before you load this preset, or the IR block will show
> 'No Model'."

…then list the IR basenames the preset references so the user can verify.
Use `open -R "<path-to-hsp>"` to reveal the generated preset in Finder
(per the `feedback_reveal_file_in_finder.md` rule).
