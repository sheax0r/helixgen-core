---
name: device
description: Use when the user wants to put helixgen presets ONTO their Helix Stadium over the network — install a tone, sync a whole setlist of tones to the device, or back up / restore. Drives the `helixgen device` CLI and the `device_*` MCP tools (including the reference-based `device sync <setlist>` / `device_sync_setlist` / `device_sync_all`). Also covers on-device library housekeeping — create/rename/delete/duplicate setlists, delete/rename/prune IRs, preset color + notes. Runs after `tone` has authored the `.hsp` file(s) on disk. Triggers on "put this on my Helix", "sync my library to the device", "install these presets", "clean up my IRs", "delete/duplicate a setlist".
---

# device

## Overview

This is the bridge from `.hsp` files **on disk** to **playable presets in a
device setlist**, over the LAN (no editor app). The `setup` and `tone` skills
stop at writing `.hsp`/`.md` to disk; this skill drives the physical Stadium —
install one tone, **sync a whole setlist**, and back up / restore.

## The device model: a preset POOL + reference SETLISTS

The Stadium does not store a preset "inside" a setlist. It keeps a single
**preset pool** (container `-2`) plus named **setlists** (under `-5`) that hold
**references** into the pool. One authored tone lives once in the pool and can
be **referenced by many setlists** at once. Editing a pool preset changes it
everywhere it's referenced; removing a tone from one setlist just drops that
reference — the pool preset (and any other setlist that references it) is
untouched.

helixgen mirrors this with one local manifest, `~/.helixgen/setlists.json`
(override `$HELIXGEN_SETLISTS`) — the **tone library**. Each tone is a record
(content `.hsp` + name + management state): a desired **user slot** (`null` =
off device, `"auto"`, or `"1A".."8D"`), ordered **setlist memberships**, and
observed device placement. **"On the device" ⟺ the tone has a slot.** There is
**no separate slot ledger** — this one manifest is the single source of truth for
"which of my tones goes where." Every generated tone **auto-registers** here
(off-device by default); `device add`/`unsync` set the slot; `device sync` is a
**managed-set mirror** (installs/updates/reorders/deletes only helixgen-managed
tones, never touches untracked device presets). **Never hand-edit it** — manage
it through the `register` / `device add` / `device unsync` / `device setlist`
verbs, the `device_setlist_*` MCP tools, or the `tone` skill.

## How a tone becomes a device preset: the transcoder (no template)

helixgen installs a tone by **transcoding** its `.hsp` straight into the
device's native content format (`_sbepgsm`) and writing that into an empty pool
slot. The `.hsp` **is** a complete Line 6 preset; the transcoder just
re-serializes it — models, params, and IR references — into the on-device
encoding. **There is no template, no slot skeleton, and no coverage
precondition.** Any block chain installs at full fidelity; you never pick a
`--template` and never worry about whether some factory preset "has a
compressor slot."

The transcoder synthesizes the **full signal graph** onto the device's real
28-slot grid: serial chains, **dual-amp / dual-DSP**, **intra-flow parallel
splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP
assignments** all transcode faithfully (hardware-validated byte-for-byte vs HX
Edit's own import). There is no serial-only limit any more.

## The default path: manage membership, then `device sync <setlist>`

For "get my tones onto the Helix":

1. **Make sure each tone is in a setlist** (in the manifest) — `device setlist
   add <setlist> <tone.hsp>` (the `tone` skill may have done this already).
2. **Make sure the setlist exists on the device.** If it doesn't, create it
   right there: `helixgen device setlist create <name>` (MCP
   `device_setlist_create`) — device-side creation shipped (#8); no Stadium
   app needed. The sync's missing-setlist error names this verb too.
3. **Sync:** `helixgen device sync <setlist>` (CLI) / `device_sync_setlist`
   (MCP) for one setlist, or `device sync --all` / `device_sync_all` for the
   whole manifest. The engine reconciles the **pool first** (install missing /
   update changed / skip unchanged — idempotent by content hash), then
   **rebuilds that setlist's references** to match manifest order. The result
   is the engine dict `{ok, setlists, pool, references, gc, irs, errors}`.

**Not a destructive mirror.** Unlike the retired directory-mirror sync, this
never wipes a setlist. It adds/updates only the pool presets the sync needs and
adds/removes/reorders only the references for the setlists being synced. It
**never orphans** a pool preset that another setlist still references. Pool
garbage-collection happens **only** on `device sync --all --gc`, and even then
only deletes pool presets that **no** manifest setlist references.

**The sync run IS your analysis.** You don't study the tones or the device to
predict what will fit — you run the sync and read `errors[]`, which names exactly
which tones failed and why. Fix that subset, re-run (re-syncing skips the tones
that already installed — it's idempotent), and waste zero work on tones that
install fine.

**Do NOT front-load analysis before the first sync.** Everything you'd "analyze"
is either done for you or reported by `errors[]`. Concretely:

- **Never read or parse `.hsp` bytes** (no `json.loads` on the file, no
  magic-stripping script). If you ever need a tone's contents, use `view_preset` —
  but you do **not** need it before a sync.
- **Do not `view_preset` every tone up front** to bucket them. Run the sync;
  `errors[]` is the only bucket that matters (the tones that didn't fit).
- The **CLI not being on `PATH` is normal** (helixgen ships as a bundled MCP
  server). That's not a blocker and not a reason to improvise — call
  `device_sync_setlist` / `device_sync_all` and the other `device_*` MCP tools.

## When the device gets flaky — re-run, then reboot

The Helix Stadium's network stack drops connections intermittently — a sync may
fail partway or the device may stop responding mid-run. This is expected and the
sync is built for it: it **auto-reconnects (bounded)** on a dropped RPC and is
**idempotent**, so:

> **If a sync fails or the device stops responding, just re-run the exact same
> sync.** Tones already in the pool are skipped, so a re-run picks up where it
> left off and converges. **If it keeps dropping across several re-runs, tell
> the user to REBOOT the Helix** (power-cycle / restart it) — that reliably
> clears the wedged network stack. Then re-run the sync once more.

Don't treat a dropped connection as a tone failure or start diagnosing the
protocol — re-run first, reboot second.

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

If you catch yourself doing any of these **before running the sync**, stop and
just run it:

- Writing a script that reads/parses `.hsp` files (`open(...).read()`,
  `json.loads`, stripping the `rpshnosj` magic). **Never parse `.hsp` bytes.**
- Calling `view_preset` on many/all tones to classify them.
- Listing, reading, or loading factory presets "to find a template" or "to check
  coverage" — **there are no templates anymore** (the transcoder is
  template-free); this is pure wasted work.
- Building Simple/Rich/Quarantine buckets, or a per-tone install plan.
- Treating "the `helixgen` CLI isn't on PATH" as a problem to solve instead of
  reaching for the `device_*` MCP tools.
- Diagnosing a dropped connection instead of just re-running (then rebooting).

All of these mean: **you are predicting failures you should be reading.** Run the
sync; its `errors[]` is the analysis, and it costs one call.

## Why a tone lands in `errors[]`

You don't need this to run the first sync — it's how you *read the results*.
Because install is a faithful, template-free transcode, most tones just install.
A tone lands in `errors[]` for one of a small, concrete set of reasons:

- **`could not resolve helixgen model 'X'`** — a block model has no device
  equivalent in the bridge. That tone isn't installable as-is; report it.
- **unregistered IR** (cab silent / "No Model" after install) — the referenced
  IR isn't on the device and isn't in your local `mapping.json`, so it can't be
  uploaded. `register-irs` the WAV (or import it in HX Edit), then re-sync.
- **dropped connection / device unresponsive** — not a tone failure at all; the
  flaky network stack. Re-run the sync; reboot the Helix if it persists.

(Dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all
synthesize faithfully as of 2.18.0 — no quarantine needed.)

## The tools

### Manage setlist membership (local manifest)

```bash
helixgen device setlist list                       # setlists + their tones
helixgen device setlist add <setlist> <tone.hsp>   # append a tone (auto-creates the setlist locally)
helixgen device setlist add <setlist> <tone.hsp> --pos N   # insert at position N
helixgen device setlist remove <setlist> "<tone name>"     # drop membership (keeps the tone if other setlists use it)
helixgen device setlist create-local <setlist>     # empty setlist in the manifest only
```

- These touch only `~/.helixgen/setlists.json` — no device. A tone's identity is
  its **display name** (`meta.name`). **The same tone can be in as many setlists
  as you want** — it's referenced once in the device pool and shared — so adding
  a tone that's already in another setlist is expected, and re-adding within one
  setlist is a harmless no-op. `add` only errors when a name is already
  registered to a *different* `.hsp` file (names must be unique). You never need
  to pre-check membership or read the manifest to add safely.
- `create-local` (and `add` auto-creating a setlist) only add it to the
  *manifest*. To also create it **on the device**, run `device setlist create
  <name>` / `device_setlist_create` (#8 shipped) — then `sync` can push to it.
- MCP mirrors: `device_setlist_list`, `device_setlist_add(model, setlist,
  hsp_path, pos?)`, `device_setlist_remove(model, setlist, tone_name)`.

### Device-side setlist management (create / rename / delete / duplicate)

```bash
helixgen device setlist create <name>          # new empty setlist ON the device
helixgen device setlist rename <old> <new>     # device + local manifest record
helixgen device setlist delete <name> --yes    # references die; pool presets NEVER deleted
helixgen device setlist duplicate <src> <dst>  # copies references; auto-creates <dst>
```

- MCP mirrors: `device_setlist_create` / `device_setlist_rename` /
  `device_setlist_delete` / `device_setlist_duplicate`.
- **Delete never orphans:** removing a setlist kills only its references —
  every pool preset stays, still available to other setlists. Confirm with the
  user before a delete (no undo).
- **Duplicate shares, it doesn't copy:** both setlists reference the same pool
  presets, so editing a tone changes it in both.

### Importing a `.hss` setlist-bundle export (EXPERIMENTAL)

```bash
helixgen device setlist import-hss export.hss --list          # offline: what's inside?
helixgen device setlist import-hss export.hss --dry-run       # preview the device write
helixgen device setlist import-hss export.hss                 # install + reference into a setlist
helixgen device setlist import-hss export.hss --setlist Gigs  # override the destination setlist name
```

- A `.hss` is the Stadium **app's** "export setlist" file — a different input
  than anything else this skill covers (not an authored `.hsp`). `--list` is
  always safe to run first (fully offline). The write path installs each
  filled slot into the pool and references it into a device setlist (created
  if absent) in bundle order. Imported presets **are recorded in the tone
  library** as *pathless* tones (source `import-hss`) with membership in the
  destination setlist — that record is what keeps a later `device sync
  <setlist>` from stripping the imported references. Having no local `.hsp`,
  they can't be restored by `device slots restore`. Flip side: if the
  destination setlist held references helixgen does NOT track, a later
  targeted `device sync <setlist>` (now plausible, since the setlist is
  manifest-tracked) will strip those untracked references — inherent
  managed-mirror semantics; prefer importing into a fresh setlist when the
  destination has untracked members you want to keep.
- **NOT idempotent on retry.** Re-running an import after a partial failure
  installs + references the already-succeeded slots AGAIN (duplicate pool
  presets + references). Before retrying, delete the setlist and the
  orphaned pool presets — or import into a fresh setlist. (Dedupe-on-retry
  is future work; backlog #31.)
- The filled-slot byte framing is an **inferred assumption** (backlog #31) —
  only an empty `.hss` sample has been captured so far. Treat an import as
  provisional until a real non-empty export confirms it; verify the result
  with `device setlist list` / `device list --setlist <name>` afterward.
- MCP mirror: `device_import_hss(model, hss_path, setlist?, list_only?,
  dry_run?)`.

### IR maintenance (delete / rename / prune) + preset info

```bash
helixgen device delete-ir <name-or-hash> --yes       # registry entry + backing .wav
helixgen device rename-ir <name-or-hash> <new-name>  # display name only; hash keeps resolving
helixgen device ir-prune                             # DRY-RUN report: referenced / protected / orphans
helixgen device ir-prune --yes [--force] [--ignore-warnings] [--only <name-or-hash>]
helixgen device set-info <cid>... --color green --notes "..."   # batch color + notes
```

- MCP mirrors: `device_delete_ir`, `device_rename_ir`, `device_ir_prune`
  (`execute`/`force`/`ignore_warnings`/`only` args), `device_set_info`.
- **`ir-prune` is dry-run by default.** Always run the dry-run first and show
  the user the `orphans` / `protected` lists — and any `warnings` (local
  tones whose recorded `.hsp` couldn't be read) — before executing.
  `protected` IRs are referenced by local off-device tones — they need
  `--force` and a deliberate user choice. Proceeding despite `warnings` is a
  **separate** consent, `--ignore-warnings` (don't reach for `--force` for
  that). A prune that aborts naming a **dangling** setlist reference means a
  setlist still points at a deleted preset — re-sync that setlist (or drop the
  entry) and retry.
- An IR referenced by any preset ON the device (or by the live edit buffer)
  is never a prune candidate. Execute mode re-verifies the plan right before
  deleting and aborts if the device listings changed — just re-run.
- **`delete-ir --force-wedge`** exists only for the wedged file-only state (a
  hash whose file still resolves but has no registry entry, after a delete →
  quick re-import). Never use it on an IR you just imported — the listing may
  merely be lagging. If a plain delete-ir errors suggesting the flag, wait a
  minute and retry without it first.
- `set-info` colors: `auto, white, red, dark orange, light orange, yellow,
  green, turquoise, blue, violet, pink, off` (or a raw index 0-11). Notes are
  written without activating the preset.

### Sync a setlist onto the device (pool + references)

```bash
helixgen device sync <setlist> [--exclude-irs]
helixgen device sync --all [--gc] [--exclude-irs]
```

- **Resolves the setlist by name** under `-5`. If the device doesn't have it,
  the run errors clearly, naming the fix: `helixgen device setlist create
  '<name>'`, then re-sync.
- **Pool-first, idempotent:** installs tones missing from the pool (transcoded,
  template-free), re-pushes ones whose `.hsp` content hash changed, skips
  unchanged ones.
- **Rebuilds references:** adds/removes/reorders the setlist's references to
  match manifest order — **never orphaning** a pool preset another setlist still
  references.
- **Uploads each tone's referenced IRs first** (instant `push_ir`) unless
  `--exclude-irs`, so cabs resolve immediately.
- **`--gc` (only with `--all`)** deletes pool presets that no manifest setlist
  references any more. A single-setlist sync never garbage-collects.
- **Per-tone failures are collected and never abort the run.** Result:
  `{ok, setlists, pool:{installed,updated,skipped}, references:{added,removed},
  gc:{deleted}, irs:[…], errors:[…]}`. Read `errors`.
- MCP mirrors: `device_sync_setlist(model, setlist, exclude_irs?)`
  and `device_sync_all(model, gc?, exclude_irs?)`. Path-based like
  the rest (no base64).

> The old directory-mirror `device sync [dir]` and the `device_sync_library` MCP
> tool are **gone**. Sync is now manifest- and setlist-driven; membership is
> managed with `device setlist`, not by globbing a directory.

### Single tone — `device install` (CLI) or `device_install_preset` (MCP)

Use for one-off placement into a chosen pool slot. **Prefer the CLI**
`helixgen device install <hsp> <name> --pos N [--auto-irs]`:
it uploads IRs (`--auto-irs`) and records the tone library. The **MCP**
`device_install_preset` now **records the tone library** (registers the tone +
its slot) but still uploads **no IRs** — use `device sync` or the CLI `install
--auto-irs` when you want IRs uploaded too.
Reserve the other `device_*` MCP tools for reads / interactive single ops
(`device_list_presets`, `device_read_preset`, `device_load_preset`,
`device_set_param`).

### Git-commit local artifact changes

Most of this skill only talks to the device, but two paths write **local**
files: registering an IR to fix an `errors[]`/`irs[]` unregistered-IR entry
(changes `mapping.json` in the IR library) and `device slots restore`
re-authoring a tone's `.hsp` in the preset output dir. When either happens,
commit the changed file(s) if the containing directory is git-managed:

1. **Detect per-directory** — `git -C <dir> rev-parse --is-inside-work-tree`
   on the specific directory that changed (IR library or preset output dir),
   not whatever repo you happen to be running in. Skip silently if it errors
   or prints `false`.
2. **Honor `git_commit_tones`** from `preferences.json` (`"auto"`/`"true"`/
   `"false"` — same vocabulary as the `tone`/`setup` skills; default `"auto"`
   commits whenever step 1 says yes).
3. **Respect `guard_paid_irs_in_git`** — never force-add a gitignored paid IR
   `.wav`; commit `mapping.json` only.
4. **Stage exactly the changed path(s)** — `git -C <dir> add -- <changed
   files>`, never `-A`/`.`. Check `git -C <dir> status` first: if the repo
   already has unrelated staged changes, warn the user and skip.
5. **Commit locally, never push** — `git -C <dir> commit -m "<message>"` with
   a short message, e.g. `ir: register missing IR for White Limo Lead sync`
   or `device: refresh restored tone <name>.hsp`.

Keep every git command scoped with `-C <dir>` (as in step 1) — your shell's
cwd is usually **not** the directory that changed, so an unscoped
`git add`/`commit` targets the wrong repo.

This is separate from `device sync` itself, which only ever touches the
device — it applies just to these two local-write side paths.

## Workflow

### 1. Get the tones into a setlist, confirm it exists on the device

1. **Membership:** for each tone the user wants, `device setlist add <setlist>
   <tone.hsp>` (skip any the `tone` skill already added). `device setlist list`
   shows the current membership.
2. **Device-side setlist:** helixgen can't create a setlist (#8). If the target
   setlist isn't already on the Stadium, ask the user to create it by hand in the
   Stadium app now. (Syncing an existing setlist like a factory `user` setlist
   needs no creation step.)

### 2. Sync

```bash
helixgen device sync <setlist>
# MCP: device_sync_setlist(model, setlist="<setlist>")
```

The engine reconciles the pool (install/update/skip), rebuilds the setlist's
references in manifest order, and uploads each tone's IRs. **Order comes from the
manifest** — `device setlist add --pos` / the manifest order sets it; a later
sync will reorder the device right back to that recorded order. For a direct,
immediate device-side move that bypasses the manifest entirely — e.g. reordering
an *untracked* preset, or a quick one-off nudge you don't want `sync` to
remember — use `helixgen device reorder <setlist> <target> --to <N>` (+ MCP
`device_reorder`) instead.

### 3. Read the result, fix `errors[]`, re-run

The result dict's `errors[]` is your analysis. Fix that subset and re-run
(re-syncing is idempotent — installed tones are skipped):

- **`could not resolve helixgen model 'X'`** — a block model doesn't bridge to
  the device; that tone isn't installable as-is. Report it.
- **unregistered IR** (cab silent / "No Model") — `register-irs` the WAV, re-sync (see **Git-commit local artifact changes** above).
- **dropped connection / device unresponsive** — not a tone failure; **re-run**
  the sync, and if it keeps dropping, **reboot the Helix** and re-run.
- **If you delegate the run to a subagent, keep it tight:** sync *this* setlist;
  report `pool`/`references`/`errors` verbatim; no improvising. Then check the
  device yourself.

### 4. IRs — usually automatic

`device sync` uploads each tone's referenced IRs first (instant registration
under the tone's exact hash), so **you normally do nothing**. Two caveats:

- An IR that isn't in your local `mapping.json` can't be resolved — it shows up
  as a per-IR note in the result (`irs[]`) and the cab will be silent. Register it
  first (`helixgen register-irs`) or import it in HX Edit, then re-sync.
- `--exclude-irs` skips IR upload entirely (use only if the IRs are already known
  to be on the device and you want a faster run).

### 5. Back up / restore

- **Back up a whole setlist:** `helixgen device backup` pulls a setlist to local
  `.sbe` files + `manifest.json` (then works offline via `device local-list`).
- **Put a recorded tone back:** `helixgen device slots restore <name-or-slot>` —
  re-authors an `.hsp`-sourced entry or re-pushes an `.sbe`-sourced one. Tones
  recorded from `save` (edit buffer) or `create` (on-device copy) have no local
  source and can't be restored this way — back them up first. A re-authored
  `.hsp` is a local file change — see **Git-commit local artifact changes**
  above.

### 6. Report back

Tightly:
1. **What's on the device now** — the setlist and its tones in order (from the
   result's `references` / `device setlist list`).
2. **Pool changes** — installed / updated / skipped counts (and any `gc` deletions
   if you ran `--all --gc`).
3. **What errored and the fix** — each `errors[]` entry with its remedy
   (unresolvable model, register an IR, or
   re-run/reboot for a dropped connection).
4. **IRs** — uploaded vs any that couldn't be resolved (so the user registers
   them).

## Failure playbook — the exact errors

| Error / symptom | What it means | Do |
|---|---|---|
| setlist not found on device (`create it with \`helixgen device setlist create ...\``) | the named setlist isn't on the device yet | run `device setlist create <name>` (or MCP `device_setlist_create`), then re-sync |
| `could not resolve helixgen model 'X'` | a block model doesn't bridge to the device | that tone isn't installable as-is; report it |
| cab silent / "No Model" after sync | referenced IR not in local `mapping.json` | `helixgen register-irs` the WAV, then re-sync (or import in HX Edit) |
| sync fails partway / device stops responding | the Stadium's flaky network stack dropped the connection | **re-run** the same sync (idempotent); if it persists, **reboot the Helix**, then re-run |
| `device setlist add` raises a name-collision error | the tone's `meta.name` is already registered to a **different** `.hsp` file (unique-name rule) — NOT triggered by adding the same tone to another setlist | rename one tone, or point at the already-registered file |

## Common Mistakes

| Mistake | Fix |
|---|---|
| Parsing `.hsp` files (`json.loads`, magic-strip) to classify tones | Never parse `.hsp` bytes — just run the sync; `errors[]` is the classification |
| `view_preset`-ing every tone / listing factory presets **before** the sync | The sync reports failures — run it, analyze `errors[]` after |
| Looking for a "template" or checking factory-preset "coverage" | There are no templates — install is a faithful, template-free transcode; just sync |
| Hand-rolling a per-preset install loop | Use `device sync <setlist>` — it reconciles the pool, rebuilds references, and uploads IRs in one call |
| Telling the user to create a setlist in the Stadium app | Not needed any more — `device setlist create <name>` creates it on the device (#8 shipped) |
| Hand-editing `~/.helixgen/setlists.json` | Manage it with `register` / `device add` / `device unsync` / `device setlist add/remove` (or the MCP tools / `tone` skill) |
| Expecting `device sync` to touch presets helixgen didn't place | It won't — sync is a managed-set mirror keyed by tone name; untracked device presets are never moved, deleted, or overwritten |
| Pre-checking whether a tone is already in a setlist before adding it | Don't — a tone belongs in as many setlists as you want (shared, referenced once in the pool). `device setlist add` is idempotent within a setlist and only errors on a name/different-file collision. Just add it |
| Reading helixgen **source** (`SetlistManifest`, `setlists.json` schema, engine internals) to confirm behavior or guard against "version drift" | Don't source-dive. The running MCP is the **bundled** plugin (loaded from `${CLAUDE_PLUGIN_ROOT}`), **not** any checkout in the working directory — so reading cwd source can *mislead* about the actual version/schema. The `device_*` tool descriptions, `device setlist list`, and the sync **result dict** are the authoritative contract; operate through them |
| Expecting sync to wipe the setlist like the old mirror | It doesn't — it reconciles pool + references and never orphans; GC only on `--all --gc` |
| Diagnosing a dropped connection as a coverage failure | It's the flaky network stack — re-run the sync, reboot the Helix if it persists |
| Ignoring the `errors[]` in the sync result | That list *is* the remaining work — read it, fix each, re-sync |
| Treating "CLI not on PATH" as a blocker | Expected — helixgen ships as a bundled MCP server; use the `device_*` MCP tools |
| Using the **MCP** `device_install_preset` for anything you want tracked | It uploads no IRs and records no ledger — use `device sync` or the CLI `device install --auto-irs` |
