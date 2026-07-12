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
- Installing/syncing an already-authored preset onto the physical Helix over
  the LAN — that's the `device` skill (install / slots / backup), not a
  generation pass. A quick device-model check (step 1) is still worth it if
  this is the session's first exchange.

## Editing an existing preset (direct edits)

Not every "modify" request is a full tone-design pass. If the user wants a
*targeted* change to a preset that already exists — change one param, disable
a block, swap a model, add/remove a block — that's the surgical-edit path, not
the `tone` skill: CLI `set-param`/`enable`/`disable`/`add-block`/
`remove-block`/`swap-model`/`view`, or the MCP `patch_preset`/
`view_preset` tools. See CLAUDE.md's **"Surgical edits"** section for the
full verb list, disambiguation flags (`--path`/`--lane`/`--pos`,
`--snapshot`), and worked examples. Still worth a quick device-model check
(step 1 below) if this is the first exchange of the session; skip the rest of
setup (IR library location, IR preferences) unless the edit itself touches an
IR block.

## Before generating or modifying any preset

In order, every session:

### -1. Verify `uv` is on PATH

The helixgen MCP server launches via `uv run --with mcp --with click`, which
auto-provisions its Python deps in an ephemeral env — but `uv` itself must be
installed. If the `mcp__helixgen__*` tools aren't showing up, this is the
first thing to check (before assuming a helixgen install problem in step 0).

Run `which uv` (or `command -v uv`) via Bash. If it resolves, proceed — no
need to mention it to the user. If missing, tell them in one line:

> "The helixgen MCP server needs `uv` to launch. Install it with `brew
> install uv` (macOS) or `curl -LsSf https://astral.sh/uv/install.sh | sh`,
> then restart Claude Code so the MCP server can start."

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

### 0.5. Load user preferences

Before checking device/IR details, load `~/.helixgen/preferences.json`
(override the whole-file location with `$HELIXGEN_PREFS`; override a single
key with `HELIXGEN_<KEY>` env, e.g. `HELIXGEN_FAVOR_IRS=1`). Precedence per
key: env var > file value > Claude-memory seed > built-in default.

- **File absent (first run):** scaffold it. Seed `device.model` from
  `user_device.md` and `instruments` from `user_guitars.md` if those memories
  exist; otherwise leave `device.model: null` and `instruments: []` (step 1
  will ask). `guard_paid_irs_in_git` and `reveal_in_finder` seed `true`
  (matching the existing feedback-memory defaults); `favor_irs` seeds `true`
  only if a "prefer IRs" feedback memory exists, else `false`. Tell the user
  in one line: "Created `~/.helixgen/preferences.json` — edit it any time to
  change these defaults (device model, favor_irs, instruments, …)." If
  `reveal_in_finder` resolves true and this is macOS, `open -R` the new file.
- **File present:** read it and apply each setting for the rest of the
  session. The file is now the authority — memory (`user_device.md`,
  `user_guitars.md`, the feedback memories) becomes a fallback/seed only;
  don't re-derive a setting from memory once the file carries an explicit
  value for it.
- **Learning a new value** (the user states their device model for the first
  time, or says "prefer IRs" / "favor cabs"): confirm before writing it back
  the *first* time a given key is set this way — e.g. "I'll set `favor_irs:
  true` in preferences.json — ok?" Once the user has confirmed that key once,
  later updates to it can be written silently.

Keys this skill owns: `device.model`, `favor_irs`, `reveal_in_finder`,
`guard_paid_irs_in_git`, `instruments`, `default_guitar`. (`preset_output_dir`
and `author` are consumed by the `tone` skill.) `ir_library_dir` is
deliberately **not** in this file — the IR directory stays env-only via
`$HELIXGEN_IRS`; see step 2.

#### Instruments

`instruments` is an array recording the user's confirmed guitars/basses,
seeded on first scaffold from `user_guitars.md` if present. Record shape:

```json
{
  "name": "Gibson Les Paul Junior",
  "type": "guitar",
  "pickups": "one bridge P-90 (single-coil soapbar)",
  "selector": "none",
  "genres": ["punk", "garage", "raw rock", "blues"],
  "notes": "breaks up early; vol + tone only"
}
```

Fields: `name`, `type` (`"guitar"`|`"bass"`) required; `pickups` (free text),
`selector` (`"none"`|`"3-way"`|`"5-way"`|string), `active` (bool — active vs
passive pickups), `genres` (array of style hints used to auto-pick an
instrument when the user doesn't name one), `notes` (one-liner) all optional.
This feeds the `tone` skill's instrument recommendations — picking a guitar by
`genres` when none is named, and phrasing pickup/selector guidance from
`selector`/`pickups`.

Seed the user's four confirmed instruments on first scaffold:

- **LP Jr** — P-90 (single bridge pickup), no selector (`"none"`).
- **ESP LTD EC-1000** — active EMG HH, 3-way selector.
- **Strandberg Boden Essential 6** — HSS, 5-way selector.
- **Ibanez Prestige** — HSH, 5-way selector.

`default_guitar` is a string naming which of the user's `instruments` to
default to when a tone request doesn't name a guitar — it feeds tone-naming
(the preset title, `.hsp`/`.md` filename, and description are named for the
target guitar). If it's unset (`null`) and the `tone` skill needs a guitar, the
tone skill asks the user which guitar to use and offers to save their choice
here (confirm-first-then-silent, matching the other prefs).

There's no `helixgen prefs` CLI yet — the file is plain JSON
(`json.load`/atomic tmp+rename write), so read or hand-edit it directly. Edit
it by hand or let this skill write it back per the confirm-first-then-silent
rule above.

### 1. Confirm the device model

Read `device.model` from `preferences.json` (loaded in step 0.5):

- **Set:** trust it — a file doesn't go stale, so there's no memory-age check
  to do here (unlike the old `user_device.md`-only flow).
- **Unset (`null`):** ask: "Which Helix do you have? Stadium, Stadium XL, or
  something else?" Write the answer back to `device.model` in the
  preferences file (confirm-first-then-silent, per 0.5); it's fine to also
  note it in memory as a convenience, but the file is the control now.

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

`favor_irs` in `preferences.json` (loaded in step 0.5) is now the authority
for "prefer a matching user IR block over a stock cab" — true only once the
user has set it or confirmed it via the step-0.5 write-back. Older "prefer
IRs" feedback-memory notes were the prior mechanism; they only matter now as
the one-time seed value used the first time the file was scaffolded.

**First, check for a local cab-pack catalog** at `<ir-library>/_catalog/`
(e.g. `~/git/helixgen/irs/_catalog/`). If present it's the authoritative tonal
reference — grep it to pick an IR by character. It has an index `README.md`
(controlled tag vocabulary + mic legend) and one file per pack with per-mix mic
combos and tags. Examples:

```bash
grep -rin 'high-gain' ~/git/helixgen/irs/_catalog/*.md | grep tight  # tight modern-metal
grep -n beefy ~/git/helixgen/irs/_catalog/kw.md                       # beefiest Greenback
grep -rin vintage ~/git/helixgen/irs/_catalog/*.md | grep clean       # vintage clean
```

Then check memory for `project_ir_notes.md` (if present) for any user-specific
one-line preferences layered on top. Examples:

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

**If a whole new commercial cab pack was added to the IR library** (not just one
stray WAV), catalog it in `<ir-library>/_catalog/`: read the pack's
`*Manual*.pdf`, `ls` its `Mixes/` folder for exact basenames, optionally
FFT-measure each mix's band energy for bright/dark/beefy/tight tags, and write
`_catalog/<slug>.md` from the template + controlled vocabulary in
`_catalog/README.md` (its "Adding a new pack" section is the full procedure).
This keeps "which IR is beefiest/brightest/best-for-X" answerable by grep.

## After generating a preset that uses user IRs

Tell the user, in one sentence:

> "Make sure these IRs are loaded on your Stadium via the Librarian → Cab
> IRs → Import before you load this preset, or the IR block will show
> 'No Model'."

…then list the IR basenames the preset references so the user can verify.
Use `open -R "<path-to-hsp>"` to reveal the generated preset in Finder
(per the `feedback_reveal_file_in_finder.md` rule).
