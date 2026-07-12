---
name: device
description: Use when the user wants to put helixgen presets ONTO their Helix Stadium over the network — install a tone, sync a whole tone library to the device, or back up / restore. Drives the `helixgen device` CLI and the `device_*` MCP tools (including the bulk `device sync` / `device_sync_library`). Runs after `tone` has authored the `.hsp` file(s) on disk. Triggers on "put this on my Helix", "sync my library to the device", "install these presets".
---

# device

## Overview

This is the bridge from `.hsp` files **on disk** to **playable presets in a
device slot**, over the LAN (no editor app). The `setup` and `tone` skills stop
at writing `.hsp`/`.md` to disk; this skill drives the physical Stadium — install
one tone, **bulk-sync a whole library**, and back up / restore.

## The default path: sync FIRST, then read `errors[]`

For "get my tones onto the Helix," your **first real action is a single
`device_sync_library` call** (MCP) or `helixgen device sync` (CLI). It **mirrors
your library onto the `user` setlist**: it deletes every preset already in that
setlist and installs the library fresh, uploads each tone's IRs, records the
ledger, and **collects per-tone failures without aborting the run**. The result
is `{deleted, installed, errors}`.

> ⚠️ **Destructive — say so.** Sync makes the `user` setlist match your library
> *exactly*: any preset on it that isn't one of your library `.hsp` tones is
> **deleted**, with **no backup**. Only the `user` setlist is touched. This is
> the intended behavior — just tell the user plainly ("this replaces everything
> in the user setlist with your library") before/when you run it, so a deleted
> on-device preset is never a surprise.

**The sync run IS your analysis.** You do not study the tones or the device to
predict what will fit — you run the sync and read `errors[]`, which names exactly
which tones failed and why. Then you fix that subset and re-run (the mirror
converges in 1–3 passes) and waste zero work on tones that install fine.

**Do NOT front-load analysis before the first sync.** Everything you'd "analyze"
is either done for you or reported by `errors[]`. Concretely:

- **Never read or parse `.hsp` bytes** (no `json.loads` on the file, no
  magic-stripping script). If you ever need a tone's contents, use `view_preset` —
  but you do **not** need it before the first sync.
- **Do not `view_preset` every tone up front** to bucket them. Run the sync;
  `errors[]` is the only bucket that matters (the tones that didn't fit).
- **Do not enumerate, read, or load factory presets to assess template coverage.**
  You cannot verify coverage that way (no such API — see below); the sync tells you.
- The **CLI not being on `PATH` is normal** (helixgen ships as a bundled MCP
  server). That's not a blocker and not a reason to improvise — call
  `device_sync_library` and the other `device_*` MCP tools.

### Pre-flight is ONE step, not a study
1. **List the target setlist once** (`device_list_presets(setlist="user")`) — so
   your final slot map is real (don't assume empty even if the user said so).
2. **Pick a template in one glance:** list a setlist, grab a **full rock-rig**
   factory preset by name (drive→amp→cab→delay→reverb — covers the common case),
   pass its cid as `--template`. Omit `--template` only if you know the edit
   buffer is already a full rig. Do **not** assess per-category coverage here.
3. **Run `device_sync_library`.** Then read `deleted` / `installed` / `errors`.
   *Now* — and only now — analyze: `errors[]` is the exact list that needs work.

## When to use

- User wants authored preset(s) **on the device** ("put White Limo on my Helix",
  "sync my tone library to the Stadium", "load these onto the device").
- User wants to **back up** or **restore** device slots.
- A generated preset "isn't loading on the device" and you need to (re)install it.

When NOT to use:
- Designing or editing a tone — that's `tone` / the surgical-edit verbs. Author
  the `.hsp` first, then come here to push it.
- Read-only device questions ("what's on my Helix?") — just call
  `device_list_presets` / `device_list_setlists` directly.

## Red flags — STOP, you are going off the rails

If you catch yourself doing any of these **before your first `device_sync_library`
call**, stop and just run the sync:

- Writing a script that reads/parses `.hsp` files (`open(...).read()`,
  `json.loads`, stripping the `rpshnosj` magic). **Never parse `.hsp` bytes.**
- Calling `view_preset` on many/all tones to classify them.
- Listing, reading, or loading factory presets "to find a template that covers
  everything" / "to check coverage."
- Building Simple/Rich/Quarantine buckets, or a per-tone template plan.
- Treating "the `helixgen` CLI isn't on PATH" as a problem to solve instead of
  reaching for the `device_*` MCP tools.

All of these mean: **you are predicting failures you should be reading.** Run the
sync; its `errors[]` is the analysis, and it costs one call.

## Why tones land in `errors[]` (the template precondition)

You don't need this to run the first sync — it's how you *read the results*.

Installing a tone maps its blocks onto the run's **template** (an existing device
preset used as a slot skeleton), which **must already contain a free slot for
every block category in the tone**. When it doesn't, that tone — not the run —
fails with:

```
no free template slot for model <N> (category 'dynamics'); choose a template with that block
```

**There is no API to ask "which template has a compressor slot."** That's exactly
why you don't pre-select a perfect template or probe: you *can't*. You run once
with a full rock-rig template (covers the Simple case: drive/amp/cab/delay/reverb),
then `errors[]` tells you which tones need a richer template or a dropped block.
Fix that subset (playbook below) and re-run. A past session ignored this and
probed template after template on an empty device, landing nothing — the sync's
non-aborting `errors[]` is what makes "run then react" strictly safer than predict.

Two hard limits `errors[]` (or a thin-looking install) will surface:

- **Parallel / dual-amp routing is flattened to a single serial chain** (only
  DSP-path 0 is read; the second lane is dropped). A tone that relies on a
  parallel split will not reproduce — **quarantine it for manual HX Edit import.**
  You'll see it as an error or an install with far fewer blocks than the `.hsp`.
- **Categories commonly missing from factory rig templates:** `dynamics`
  (comp/gate), and often `modulation`, `pitch`, `eq`. `amp` / `cab` / `drive` /
  `delay` / `reverb` are covered by almost any full-rig template.

## The tools

**Bulk sync (the primary path) — `device sync` / `device_sync_library`:**

```bash
helixgen device sync [<dir>] [--setlist user] [--exclude-irs] [--template <cid>]
```
- Globs `*.hsp` in `<dir>` (default: the `preset_output_dir` preference).
- **Mirrors** the setlist to the library: **deletes every preset already in the
  setlist**, then installs each `.hsp` fresh into empty slots (arbitrary order).
  The library on disk is the source of truth. **No backup is taken.**
- **Only the target setlist is touched** (default `user`); others are untouched.
- **Guardrail:** an empty or all-unreadable library deletes **nothing**.
- **Uploads each tone's referenced IRs first** (via instant `push_ir`) unless
  `--exclude-irs` — so cabs resolve immediately.
- **Replaces this setlist's ledger entries** with the new placements.
- **Per-tone failures are collected and reported, and never abort the run** —
  the result is `{ok, deleted:[…], installed:[…], errors:[…]}`. Read `errors`.

The MCP mirror `device_sync_library(model, directory?, setlist?, exclude_irs?,
template_cid?)` does the same (delete + install + IRs + ledger).

**Single tone — `device install` (CLI) or `device_install_preset` (MCP):**
Use for one-off placement into a chosen slot. **Prefer the CLI**
`helixgen device install <hsp> <name> --pos N [--template <cid>] [--auto-irs]`:
it uploads IRs (`--auto-irs`) and records the ledger. The **MCP**
`device_install_preset` does **neither** — no IR upload, no ledger entry (so a
later `slots sync` won't see it). Reserve the other `device_*` MCP tools for
reads / interactive single ops (`device_list_presets`, `device_read_preset`,
`device_load_preset`, `device_set_param`).

## Workflow

### 1. Run the sync (lean pre-flight, then go)

Don't study the tones first. Two cheap steps, then run:

1. **List the target setlist once** (`device_list_presets(setlist="user")`) so the
   slot map you report is real — don't assume empty even if the user said so.
2. **Pick any full rock-rig template in one glance** — list a setlist, grab a
   drive→amp→cab→delay→reverb factory preset by name, pass its cid as `--template`.
   Don't assess per-category coverage. (Omit `--template` only if you know the
   edit buffer is already a full rig.)

```bash
helixgen device sync <dir> --template <cid>
# MCP: device_sync_library(model, directory=<dir>, setlist="user", template_cid=<cid>)
```

It **deletes everything in the `user` setlist**, then installs every `.hsp` into
the freed slots in **arbitrary order**, uploads each tone's IRs, and records the
placements in the ledger. **Ordering is not this skill's job** — tones land
wherever there's room; imposing an order is a separate, planned reorder skill
(see the backlog).

### 2. Read the result, fix `errors[]`, re-run

The result is `{deleted, installed, errors}`. This is your analysis:

- **`deleted`** — presets removed to make the setlist match the library (mention
  the count to the user; these are gone with no backup).
- **`installed`** — what landed and where (`slot`, `cid`). Your slot map.
- **`errors[]`** — the only work left. Each entry is one tone and why it failed.
  Fix that tone and re-run (re-syncing re-mirrors — safe to repeat):
  - **`no free template slot for model N (category 'dynamics'/'modulation'/…)`** —
    the template lacks that block. Either **drop the inessential block** from that
    tone (`remove-block` / `patch_preset` — a front-of-chain comp is usually subtle
    polish) and re-sync, or pass a **richer `--template`** that has the slot (build
    one in HX Edit if no factory preset does). **Do not probe templates** — if no
    device preset has that slot, no template will satisfy it.
  - **parallel / dual-amp tone** (errors, or installs with far fewer blocks than
    the `.hsp`) — the installer flattens to one serial chain. **Quarantine it for
    HX Edit import**; keep it out of the synced dir.
  - **unregistered IR** (cab silent / "No Model") — `register-irs` the WAV, re-sync.
- **If you delegate the run to a subagent, keep it tight:** sync *this* dir with
  *this* template; report `deleted`/`installed`/`errors` verbatim; no template
  probing, no improvising. Then check the device yourself.

### 3. IRs — usually automatic

`device sync` uploads each tone's referenced IRs first (instant registration
under the tone's exact hash), so **you normally do nothing**. Two caveats:

- An IR that isn't in your local `mapping.json` can't be resolved — it shows up
  as a per-IR note in the result and the cab will be silent. Register it first
  (`helixgen register-irs`) or import it in HX Edit, then re-sync.
- `--exclude-irs` skips IR upload entirely (use only if the IRs are already known
  to be on the device and you want a faster run).

### 4. Ordering — out of scope (deferred)

`device sync` places tones in arbitrary fill-empty order and records where each
landed in the ledger. **This skill does not reorder slots.** Imposing a desired
order is a separate, planned reorder skill (backlog item #7); don't fold it into
the install flow. If the user asks for a specific order now, install first, tell
them the arbitrary slot map, and note that explicit reordering is coming.

### 5. Back up / restore

- **Back up before any destructive reorg:** `helixgen device backup` pulls a whole
  setlist to local `.sbe` files + `manifest.json` (then works offline via
  `device local-list`).
- **Put a recorded tone back:** `helixgen device slots restore <name-or-slot>` —
  re-authors an `.hsp`-sourced entry or re-pushes an `.sbe`-sourced one. Tones
  recorded from `save` (edit buffer) or `create` (on-device copy) have no local
  source and can't be restored this way — back them up first.

### 6. Report back

Tightly:
1. **What landed and where** — the slot map from `installed` / `device slots
   list` (`1A White Limo LP · 1B …`), noting the order is arbitrary.
2. **What was removed** — the `deleted` count (presets the mirror wiped to match
   the library; no backup), so the user isn't surprised.
3. **What errored and the fix** — each `errors[]` entry with its remedy (richer
   template, dropped block, or HX Edit for parallel presets).
4. **IRs** — uploaded vs any that couldn't be resolved (so the user registers
   them).

## Failure playbook — the exact errors

| Error / symptom | What it means | Do |
|---|---|---|
| `no free template slot for model N (category 'dynamics')` (in `errors[]`) | the run's template has no comp/gate slot | pass a `--template` that covers it, drop the block, or quarantine — **do not probe templates** |
| `could not resolve helixgen model 'X'` | a block model doesn't bridge to the device | that tone isn't installable as-is; report it |
| `no empty slot left in setlist` | the library has >128 tones for one setlist | split the library or target another setlist (the mirror already frees the whole setlist first) |
| `user slot N is not empty` (single `device install` only) | chosen slot occupied | `device sync` mirrors the whole setlist; for a one-off `install`, pick an empty `--pos` |
| an installed tone has far fewer blocks than the `.hsp`, or a parallel tone errors | parallel routing flattened / unsupported | HX Edit import that tone; keep it out of the synced dir |
| cab silent / "No Model" after sync | referenced IR not in local `mapping.json` | `helixgen register-irs` the WAV, then re-sync (or import in HX Edit) |
| a later `device slots sync` does nothing after installing via **MCP `device_install_preset`** | that MCP tool records no ledger entry | install via `device sync` or the CLI `device install` (both record the ledger) |

## Common Mistakes

| Mistake | Fix |
|---|---|
| Parsing `.hsp` files (`json.loads`, magic-strip) to classify tones | Never parse `.hsp` bytes — just run the sync; `errors[]` is the classification |
| `view_preset`-ing every tone / listing factory presets **before** the first sync | The sync is safe and reports failures — run it first, analyze `errors[]` after |
| Probing template after template for a compressor tone | No factory template may have a dynamics slot — run once, then drop the block or pass a richer `--template` for the ones in `errors[]` |
| Hand-rolling a per-preset install loop | Use `device sync` — it mirrors the setlist, uploads IRs, and records the ledger in one call |
| Ignoring the `errors[]` in the sync result | That list *is* the remaining work — read it, fix each, re-sync (re-mirroring is safe to repeat) |
| Treating "CLI not on PATH" as a blocker | Expected — helixgen ships as a bundled MCP server; use the `device_*` MCP tools |
| Delegating the sync to a background agent and not watching | Give it a tight, no-probing mandate and check the device yourself |
| Syncing a dual-amp / parallel-split tone | It flattens to one serial chain (second lane lost) — quarantine for HX Edit |
| Using the **MCP** `device_install_preset` for anything you want tracked | It uploads no IRs and records no ledger — use `device sync` or the CLI `device install --auto-irs` |
| Trying to order slots in this flow | Ordering is out of scope — sync places tones arbitrarily + records the ledger; a reorder skill is planned |
| Forgetting sync is destructive | It **deletes** every `user`-setlist preset not in the library, no backup — tell the user before running |
| Pointing sync at the wrong/empty directory | It mirrors *that* dir; an empty one is caught by the guardrail (deletes nothing), but a wrong non-empty dir would wipe + replace — confirm the directory |
