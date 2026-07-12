---
name: device
description: Use when the user wants to put helixgen presets ONTO their Helix Stadium over the network — install a tone, sync a whole tone library to the device, reorder slots, or back up / restore. Drives the `helixgen device` CLI and the `device_*` MCP tools (including the bulk `device sync` / `device_sync_library`). Runs after `tone` has authored the `.hsp` file(s) on disk. Triggers on "put this on my Helix", "sync my library to the device", "install these presets".
---

# device

## Overview

This is the bridge from `.hsp` files **on disk** to **playable presets in a
device slot**, over the LAN (no editor app). The `setup` and `tone` skills stop
at writing `.hsp`/`.md` to disk; this skill drives the physical Stadium — install
one tone, **bulk-sync a whole library**, order the slots, and back up / restore.

There is **one command** for the common "get my tones onto the Helix" job —
`device sync` — and it does the tedious, error-prone parts for you (empty-slot
placement, IR upload, ledger recording, idempotent re-runs). **Do not hand-roll a
per-preset install loop.** Your job with this skill is the *judgment* around that
command: knowing which tones will install cleanly, choosing a template that covers
them, and handling the per-tone failures it reports — because the installer is
experimental and has one sharp edge (the **template precondition**, below) that
you must plan around, not discover mid-run.

## When to use

- User wants authored preset(s) **on the device** ("put White Limo on my Helix",
  "sync my tone library to the Stadium", "load these onto the device").
- User wants to **reorder**, **back up**, **restore**, or **verify** device slots.
- A generated preset "isn't loading on the device" and you need to (re)install it.

When NOT to use:
- Designing or editing a tone — that's `tone` / the surgical-edit verbs. Author
  the `.hsp` first, then come here to push it.
- Read-only device questions ("what's on my Helix?") — just call
  `device_list_presets` / `device_list_setlists` directly.

## Read this first: the template precondition (the one sharp edge)

Installing a tone does **not** build a chain from scratch. It maps the tone's
blocks onto a **template** — an existing device preset used as a slot skeleton —
and the template **must already contain a free slot for every block category in
the tone**. `device sync` uses **one template for the whole run** (the current
edit buffer, or the `--template <cid>` you pass, loaded once). So if the run's
template has no dynamics slot, every tone with a compressor fails with:

```
no free template slot for model <N> (category 'dynamics'); choose a template with that block
```

**There is no API to ask "which template has a compressor slot."** You cannot
discover it — you can only pick a template and read which tones fail. So the play
is to **predict** the failures up front (step 1) and **choose one covering
template** (step 2), *not* to guess-and-retry templates on the device. A past
session with no skill did exactly that — probed template after template for one
compressor preset and landed nothing on an empty device. `device sync` prevents
the "landed nothing" part (per-tone failures don't abort the run), but it can't
choose the template for you.

Two more hard limits of the installer:

- **Parallel / dual-amp routing is flattened to a single serial chain** (only
  DSP-path 0 is read; the second lane is dropped). A tone that relies on a
  parallel split will not reproduce on the device this way — it either errors or
  installs wrong. **Quarantine those for manual HX Edit import.**
- **Categories commonly missing from factory rig templates:** `dynamics`
  (comp/gate), and often `modulation`, `pitch`, `eq`. `amp` / `cab` / `drive` /
  `delay` / `reverb` are covered by almost any full-rig template.

## The tools

**Bulk sync (the primary path) — `device sync` / `device_sync_library`:**

```bash
helixgen device sync [<dir>] [--setlist user] [--exclude-irs] [--template <cid>]
```
- Globs `*.hsp` in `<dir>` (default: the `preset_output_dir` preference).
- Installs each into an **empty** slot of the setlist — **non-destructive, never
  overwrites** an occupied slot.
- **Idempotent:** a tone whose name already occupies a slot is **skipped**, so
  re-running only adds what's new.
- **Uploads each tone's referenced IRs first** (via instant `push_ir`) unless
  `--exclude-irs` — so cabs resolve immediately.
- **Records every placement in the slot ledger** (enables `slots
  verify`/`restore`/`reorder`/`sync` later).
- **Per-tone failures are collected and reported, and never abort the run** —
  the result is `{ok, installed:[…], skipped:[…], errors:[…]}`. Read `errors`.

The MCP mirror `device_sync_library(model, directory?, setlist?, exclude_irs?,
template_cid?)` does the same (IRs + ledger included).

**Single tone — `device install` (CLI) or `device_install_preset` (MCP):**
Use for one-off placement into a chosen slot. **Prefer the CLI**
`helixgen device install <hsp> <name> --pos N [--template <cid>] [--auto-irs]`:
it uploads IRs (`--auto-irs`) and records the ledger. The **MCP**
`device_install_preset` does **neither** — no IR upload, no ledger entry (so a
later `slots sync` won't see it). Reserve the other `device_*` MCP tools for
reads / interactive single ops (`device_list_presets`, `device_read_preset`,
`device_load_preset`, `device_set_param`).

## Workflow

### 1. Pre-flight — predict the outcome before you run

`device sync` is safe to run blind (non-destructive, idempotent), but running it
*informed* means you can pick the right template and tell the user what won't fit
**before** the run instead of explaining `errors[]` after. Do this first:

1. **Reachable + model.** Confirm the device model (from `preferences.json`, per
   `setup`) and that it answers: `helixgen device list --setlist user`.
2. **See what's already on the device.** `device sync` fills empty slots and skips
   by name — so existing presets shape where new ones land. List first.
3. **Classify the `.hsp` files** you intend to sync by block category — read each
   with `helixgen view <preset.hsp>` (or the `view_preset` MCP tool):

   | Bucket | Test | Outcome under `device sync` |
   |---|---|---|
   | **Simple** | categories ⊆ `{drive, amp, cab, delay, reverb}` | installs against any full rock-rig template |
   | **Rich** | also `dynamics` / `modulation` / `eq` / `pitch` | installs **only if the run's template covers those** (step 2) — else lands in `errors[]` |
   | **Quarantine** | parallel / dual-amp routing (2 DSP lanes, split/join) | **not installable this way** — flag for HX Edit, keep it out of the synced dir or expect it in `errors[]` |

4. **Tell the user the plan up front** — which tones will sync, and which are
   Quarantine (and why), *before* running.

### 2. Choose ONE covering template

`device sync` uses a single template for the whole run, so pick it to cover the
categories you found — don't rely on whatever happens to be in the edit buffer.

- **All Simple:** any full "rock rig" factory preset (drive→amp→cab→delay→reverb)
  covers them. Find one with `helixgen device list --setlist factory`, note its
  cid, pass `--template <cid>`. (Omit `--template` only if you know the current
  edit buffer already has that shape.)
- **Rich tones present (comp/gate/mod/eq):** the template must contain those
  blocks. If no factory preset does, pick per tone — do **not** probe:
  1. **Drop the inessential block** and re-sync. A front-of-chain compressor is
     usually subtle polish; removing it (`helixgen remove-block …` on the file)
     often makes the tone Simple with no audible loss. Note it in the report.
  2. **Build one covering template in HX Edit** — assemble a preset with one slot
     of every category you need, save it to the device, `device list` its cid,
     and pass that cid as `--template` so all the Rich tones map.
  3. **Quarantine to HX Edit import** if neither fits.
- Treating a `category 'dynamics'` error as "try another template" is the trap —
  if no device preset has that slot, no template will satisfy it.

### 3. IRs — usually automatic

`device sync` uploads each tone's referenced IRs first (instant registration
under the tone's exact hash), so **you normally do nothing**. Two caveats:

- An IR that isn't in your local `mapping.json` can't be resolved — it shows up
  as a per-IR note in the result and the cab will be silent. Register it first
  (`helixgen register-irs`) or import it in HX Edit, then re-sync.
- `--exclude-irs` skips IR upload entirely (use only if the IRs are already known
  to be on the device and you want a faster run).

### 4. Run it, then read the result

```bash
helixgen device sync ~/git/guitar-training/tones --template <cid>
```

- **Watch the run and read `errors[]` / `skipped[]`** — this is the whole point.
  `installed` tells you what landed and where (`slot`, `cid`); `skipped` is
  already-present tones (idempotent re-run); `errors` is the work left:
  template-coverage misses, unresolved models, parallel presets, unregistered
  IRs. Handle each per step 2/3, then re-run (it only adds what's missing).
- **Re-running is safe and expected** — fix the `errors`, sync again, converge.
  It never overwrites or reorders; it only fills empty slots with what's not yet
  on the device.
- **If you delegate the run to a subagent, keep it tight and watch it:** sync
  *this* dir with *this* template, report `installed`/`skipped`/`errors`
  verbatim — no per-preset template probing, no improvising. Then check the
  device yourself (`device list`). An unwatched agent left to "figure out
  templates" is how the user ends up staring at an empty device.
- **Verify** with `helixgen device slots list --verify` (ledger-aware:
  `ok`/`changed`/`missing`/`moved`/`untracked`) or `device list`.

### 5. Order the slots (optional)

`device sync` fills empty slots in order — it does not sort by any scheme. To
impose an order after the fact, use the ledger (populated by the sync):

```bash
helixgen device slots reorder "White Limo LP" --to 3   # local ledger only
helixgen device slots sync --dry-run                   # preview device moves
helixgen device slots sync                             # apply (confirms first)
```

- `reorder` rewrites **local** order; `slots sync` applies it to the device.
- **`slots sync` only *reorders* already-tracked presets among the slots they
  already occupy** — it never installs a tone and never touches untracked
  presets. (Different command from `device sync`, which installs.)
- It's destructive (pull → delete → re-push in order) but **backs up affected
  setlists first** (unless `--no-backup`) and **verifies every pull before any
  delete**, so an interruption is recoverable.

### 6. Back up / restore

- **Back up before any destructive reorg:** `helixgen device backup` pulls a whole
  setlist to local `.sbe` files + `manifest.json` (then works offline via
  `device local-list`).
- **Put a recorded tone back:** `helixgen device slots restore <name-or-slot>` —
  re-authors an `.hsp`-sourced entry or re-pushes an `.sbe`-sourced one. Tones
  recorded from `save` (edit buffer) or `create` (on-device copy) have no local
  source and can't be restored this way — back them up first.

### 7. Report back

Tightly:
1. **What landed and where** — the slot map from `installed` / `device slots
   list` (`1A White Limo LP · 1B …`).
2. **What was skipped** — already-present tones (so the user knows the re-run was
   idempotent, not broken).
3. **What errored and the fix** — each `errors[]` entry with its remedy
   (covering template, dropped comp block, or HX Edit for parallel presets).
4. **IRs** — uploaded vs any that couldn't be resolved (so the user registers
   them).
5. **One next step** — e.g. "reorder with `slots reorder … --to N` then `slots
   sync`," or "import the 2 dual-amp tones via HX Edit."

## Failure playbook — the exact errors

| Error / symptom | What it means | Do |
|---|---|---|
| `no free template slot for model N (category 'dynamics')` (in `errors[]`) | the run's template has no comp/gate slot | pass a `--template` that covers it, drop the block, or quarantine — **do not probe templates** |
| `could not resolve helixgen model 'X'` | a block model doesn't bridge to the device | that tone isn't installable as-is; report it |
| `no empty slot left in setlist` | the setlist filled up | free slots (delete) or target another setlist |
| `user slot N is not empty` (single `device install` only) | chosen slot occupied | `device sync` avoids this (fills empty); for `install`, pick an empty `--pos` |
| an installed tone has far fewer blocks than the `.hsp`, or a parallel tone errors | parallel routing flattened / unsupported | HX Edit import that tone; keep it out of the synced dir |
| cab silent / "No Model" after sync | referenced IR not in local `mapping.json` | `helixgen register-irs` the WAV, then re-sync (or import in HX Edit) |
| a later `device slots sync` does nothing after installing via **MCP `device_install_preset`** | that MCP tool records no ledger entry | install via `device sync` or the CLI `device install` (both record the ledger) |

## Common Mistakes

| Mistake | Fix |
|---|---|
| Hand-rolling a per-preset install loop | Use `device sync` — it does empty-slot placement, IR upload, ledger recording, and idempotent re-runs in one command |
| Probing template after template for a compressor tone | No factory template may have a dynamics slot — choose one covering `--template` up front, or drop the block |
| Relying on the edit buffer as the template | `device sync` uses one template for the whole run — pass `--template <cid>` chosen to cover your tones' categories |
| Ignoring the `errors[]` in the sync result | That list *is* the remaining work — read it, fix each, re-run (re-runs are safe/idempotent) |
| Delegating the sync to a background agent and not watching | Give it a tight, no-probing mandate and check the device yourself |
| Syncing a dual-amp / parallel-split tone | It flattens to one serial chain (second lane lost) — quarantine for HX Edit |
| Using the **MCP** `device_install_preset` for anything you want tracked | It uploads no IRs and records no ledger — use `device sync` or the CLI `device install --auto-irs` |
| Expecting `slots sync` to install tones | `slots sync` only **reorders** already-tracked presets — `device sync` is what installs |
| Assuming the device is empty because the user said so | `device sync` is non-destructive and fills around what's there — but still `device list` first so the plan and slot map are real |
