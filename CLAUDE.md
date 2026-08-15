# helixgen-core

Core library + CLI. Generates Line 6 Helix Stadium `.hsp` presets (plus legacy `.hlx`) from JSON tone specs, controls Stadium over LAN. Block library lives at `~/.helixgen/library/` (override `$HELIXGEN_LIBRARY`), built by ingesting real device exports.

**Repo family (all under `sheax0r`):** this repo (`helixgen-core`) = Python package `helixgen` — libs + CLI (**CLI is only engine surface**; MCP server removed 0.20.0 — per-verb `--help` text is agent-facing behavioral contract, pinned by `tests/test_cli_parity.py`); [`helixgen`](https://github.com/sheax0r/helixgen) = Claude Code plugin/marketplace repo carrying `setup`/`tone`/`device` skills; [`helixgen-tui`](https://github.com/sheax0r/helixgen-tui) = terminal UI. Plugin + TUI consume this repo as PyPI dependency (package name `helixgen`, on PyPI since 0.19.1).

**How this file works:** carries mental models + behavioral rules that must stay in front of agent, plus concise verb indexes. Reference detail lives one pointer away, authoritative there: [`docs/CLI.md`](docs/CLI.md) (every verb, flag, gotcha), [`docs/recipe-reference.md`](docs/recipe-reference.md) (every recipe field), each verb's `--help` (pinned contract). Read pointed-at doc before scripting against verb not used this session.

**Project backlog lives in beads** — `bd ready` before starting new work, `bd create` for anything deferred (including punted review findings). Not TodoWrite, not a markdown TODO list, not a TODO comment. [`docs/BACKLOG.md`](docs/BACKLOG.md) is the **legacy archive**: its "corrected mental models" preamble is still required reading, and existing `#N` references throughout this file resolve there, but nothing new gets appended to it.

## Home directory and git plumbing (`~/.helixgen`)

Artifact library carries three artifact kinds: **tones** (`library/tones/<logical-slug>.json` + per-variant `.hsp`), **guitar profiles** (`library/guitars/<slug>.json`), **per-IR metadata** (`library/irs/<pack>/<name>.json` sidecars next to copied WAVs — WAV bytes stay gitignored — plus `library/irs/mapping.json`, the `irhash →
wav-path` registry). Design: `docs/superpowers/specs/2026-07-15-library-metadata-design.md`.

- **`$HELIXGEN_HOME`** (`src/helixgen/home.py`) = root of everything helixgen persists — default `~/.helixgen`. Covers preferences (`preferences.json`) and the IR-hash cache (`cache/irhash.json`) too — both anchor under `$HELIXGEN_HOME` by default. Per-area overrides (`$HELIXGEN_LIBRARY`, `$HELIXGEN_IRS`, `$HELIXGEN_SETLISTS`, `$HELIXGEN_PREFS`, `$HELIXGEN_CACHE`, `$HELIXGEN_IRHASH_CACHE`, `$HELIXGEN_LOCKS`) keep working, always win over `$HELIXGEN_HOME`-derived default.
- **Home auto-`git init`s on first write** (`src/helixgen/libinit.py` + `gitops.py`) whenever `git` on PATH — unconditional, not preference-gated (its `.gitignore` excludes `devices/`, `cache/`, `tone3000/`, `*.bak*`, IR audio). Library-mutating operations **auto-commit** after, gated by `git_commit_tones` preference (default `"auto"`). Auto-commits carry the user's **configured git identity** (author + committer, from local/global/system git config); the hardcoded `helixgen <helixgen@localhost>` is used only as a fallback when no usable identity is configured — both `user.name` and `user.email` must be set (#79(i)). All advisory: missing git binary or failed commit warns to stderr, never fails triggering operation.
- **Manifest lives at `~/.helixgen/setlists/manifest.json`** (override `$HELIXGEN_SETLISTS`) — manifest v3, **intent-only** (see "The tone library" below). Legacy `~/.helixgen/setlists.json` (v1/v2) auto-migrates up on first load (backup written first, legacy file renamed so re-runs never re-migrate). First `device sync` after v2-to-v3 migration re-pushes every managed tone once — harmless, idempotent (device serial observed nothing under own file yet).
- **Per-device observed state in `~/.helixgen/devices/<serial>.json`** (`src/helixgen/device/observations.py`) — observed placement (`cid`/`posi`) plus, since 0.24.0, device's **discovered address record** (`ip`, `model`, `firmware`, and — #77 — `port` when nonstandard). NOT manifest, NOT committed (`devices/` gitignored): placement rebuilt wholesale by every `device sync`, so losing file costs one re-`discover`.

## CLI (core verbs)

**Full per-verb reference: [`docs/CLI.md`](docs/CLI.md) "Commands" and "IR commands".** Verb index: `list-blocks`, `show-block`, `generate`, `view`, `ingest`, `register-irs`, `irhash`, `ir-scan`, `list-irs`, `ir-cache`, `analyze-audio`, `controllers`, surgical edit verbs + `patch` ("Surgical edits" below), `describe` + `library …` + `device …` (own sections below).

Rules that must stay in front of you:

- **Run `helixgen show-block "<name>"` before writing or editing spec** — param names case-sensitive, generator rejects unknown ones. Validator fails with list of valid names: run `show-block`, correct — don't guess. **Read the UNIT it prints, never assume 0.0–1.0** (hgc-285): it resolves each param's real min/max, DEVICE default, unit, display scale and enum labels at read time (`device/paraminfo.py`, over the vendored `_defs_data.json` + `_param_ui.json`), so one amp's `ChVol float 0..1` and the next amp's `Level float -40..10 dB` are visibly different things — 0.5 is half-up on one, +0.5 dB on the other. `sighted` = the single value one ingested preset carried (not a range, not a recommendation — the old `observed=[v, v]` was always that same one sample); `[internal]` = no editor control, plumbing, don't set it. Block-library files still store only that one sample; the overlay is read-time, `ingest` unchanged.
- **A param a recipe doesn't mention is authored at the MODEL DEFAULT, not the `sighted` value** (hgc-x7i). One baseline, `generate.authoring_defaults(block)` = device `def` from `_defs_data.json`, rounded through **float32** (the precision the device stores and exports, so a default compares equal to the same knob read back off a real export). Two carve-outs keep the sighting: a param the defs never heard of, and an `[internal]` one (no editor control ⇒ nobody moved it, the sighting IS the device's value, and the asset's `def` for the AmpCab gains is a blanket 0 across all 26 Agoura amps while the matching frequencies/Qs are per-model). Every `.hsp` writer (`_to_hsp_bnn`, `mutate.swap_model` — so `add-block`/`swap-model` too) and the reader (`view`, which omits params equal to it) go through it, so `view` → `generate` stays a round-trip; the legacy `.hlx` writer deliberately does NOT (Stadium defs, and no reader to stay symmetric with). The exemplar is still the block's STRUCTURE — which params exist, `@model`/`@type`/`@enabled`. Practical consequence: omitting a knob genuinely means "factory position", and `view` output reads as the list of knobs actually moved (bar a handful of params whose exporter wrote a truncated decimal).
- Verbs whose output agents consume take **`--json`** for machine-readable stdout; `view` prints JSON by default.
- `generate` with no `-o` writes into tone library, authors tone metadata — name via `--artist`/`--song` (paired) or `--descriptor` (mutually exclusive), plus optional `--guitar`. Explicit `-o <out.hsp>` = legacy path: writes there, auto-registers, naming flags ignored, **no metadata JSON**. Extension picks format (`.hsp` Stadium, `.hlx` legacy Helix).
- IR registration (`register-irs`, `ir-scan`) **copies WAVs into `library/irs/<pack>/` with metadata sidecars** by default (`--no-copy` opts out). Direct hashing needs libsndfile + **48 kHz sources** — helixgen input constraint, not device's (device normalizes any rate on own import, so non-48k IR still works once imported onto hardware). A **wedged** IR (backing file resolves, absent from device registry) is detected by the nudged-listing check (#93): a hash still absent from a CONFIRMED-refreshed `-11` listing is wedged and reads **missing**, so the auto-upload paths (`install --auto-irs`, `device sync`) re-push it and `push-ir` heals it (removes the orphan, re-imports). The one blind spot is an unconfirmable refresh — empty or failed listing (the single-wedged-IR case) — where the wedge still reads as present and the cab stays silent with only a stderr warning; `device delete-ir --force-wedge` is the sure clear there.

### `helixgen device` — network control of a Helix Stadium

Talks to **Stadium** over LAN directly (OSC-over-ZeroMQ; no editor app; needs `pip install 'helixgen[device]'`). Run **`helixgen device
discover`** once to find + persist Stadium address; every verb then resolves IP as `--ip` > `$HELIXGEN_HELIX_IP` > persisted record — **no built-in default**; none set, verbs fail fast pointing at `device discover`. Empty/whitespace-only `--ip` is rejected (nonzero exit; omit flag to fall back), #77. `--port` likewise defaults to the record's persisted RPC port (2002 unless discovery saw a nonstandard advertised port, #77) — explicit `--port` wins. `device discover --forget <serial-or-ip>` prunes a stale persisted record (no network; clear error, not traceback, on unknown target or absent records). Discovery used once; sessions stay direct-to-IP. **Stadium-only.**

**Full per-verb reference — every flag + gotcha — lives in [`docs/CLI.md`](docs/CLI.md) "Device commands".** Verb index:

- **Preset + edit buffer:** `list` / `setlists` / `info` / `active` (ACTIVE preset — save/restore player's selection) / `read` / `load` / `create` / `save` / `rename` / `delete` / `set-param` / `blocks` / `params` (numeric pids + CURRENT raw values — run before `set-param`; block coordinates = DSP **grid slots**, 0-27) / `pull` / `push` / `restore` / `backup` / `local-list` / `watch` / `set-info` / `install` (transcodes helixgen `.hsp` straight into device content — no template, full fidelity) / `to-hsp` (the REVERSE transcoder — device content `.sbe`/cid back into a real `.hsp`, so a preset authored on the hardware or in HX Edit becomes viewable/patchable/re-installable; offline for a `.sbe` path, a non-activating read for a cid). `--setlist` takes `user` (pool, default), `factory`, or real device setlist name (entries = references to pool presets).
- **Live ops (mutate ACTIVE tone):** `snapshot` / `bypass` / `model` / `reorder` (direct DEVICE-side reorder — distinct from local-manifest `slots reorder`; numeric args **cid-first**) / `tuner` / `meters` / `measure` (read-only 2003 telemetry) / `calibrate` (source-level calibration for a recorded stimulus — nulls the playback level against the jack level of your OWN playing, `input_db` NEVER `gain_db`, and persists it to the `normalization` prefs block; a run that does not converge writes nothing) / `normalize` (level-matching loop over `measure`: DRY-RUN by default, `--yes` writes dB trims into **local `.hsp` only** — device follows via `sync` — records telemetry on library variants; holds `editbuffer` even in dry-run. `--measure-via capture` swaps the telemetry meters for a sox capture of the Stadium's USB output reduced to BS.1770 integrated LUFS — needs `[analyze]` + `sox` + `--capture-input NAME`, all checked BEFORE the first capture; default metric deliberately unchanged, hc-3kg owns that call. BOTH measurement paths are DOWNSTREAM of the output gain, so a measured value already contains the trim in force and re-measuring CONFIRMS a trim. Its settings resolve **flag > `normalization` prefs block > option default**, and it preflights REACHABILITY: a target can only reach `measured − level in force + 20`, and anything above that is in-chain gain staging, not a level move).
- **Global Settings + Global EQ:** `settings list|get|set`, `globaleq
  list|set` (**write-only** — no network read-back).
- **IRs on device:** `list-irs` (distinct from local `helixgen
  list-irs`) / `push-ir` / `pull-ir` / `delete-ir` / `rename-ir` / `ir-prune` (dry-run by default).
- **Setlists + sync:** `setlist create|rename|delete|duplicate` (device-side; never orphan pool presets), `setlist
  list|add|remove|create-local` (local manifest membership), `setlist
  import-hss` / `export-hss` (EXPERIMENTAL), `sync <setlist>` / `sync
  --all [--gc]`. Plain `sync` recomputes each pool tone's `.hsp` hash at sync time, so an in-place edit is detected and re-pushed even though mutating verbs never refresh the manifest's cached hash (#92). `--repush` forces content re-push of tones whose `.hsp` bytes are **unchanged** since last sync — use once after transcoder upgrade (a byte-hash comparison can't see transcoder-output change for an unchanged `.hsp`).
- **Tone library / slots:** `helixgen register`, `device add` / `unsync` / `library` / `slots [list|restore|reorder] [--verify]`, `device setlist
  sync-on|sync-off`.

**Device-write awareness.** Read/list verbs safe — e.g. `info`, `active`, `read`, `list`, `list-irs`, `blocks`, `params`, `settings list`/`get`, `tuner`, `meters`, `measure`, `watch`, `backup`, `pull`/`pull-ir`, plus offline verbs (`local-list`, `library`, `slots list`, `globaleq list`, `--list`/`--dry-run` variants). Anything writing content, properties, files **mutates device** — live-ops verbs change ACTIVE tone immediately. Unsure: check verb's entry in [`docs/CLI.md`](docs/CLI.md). Posture for device writes: prefer empty/expendable slot when testing, take upfront `device backup`, tear down test artifacts after (slot-writing verbs fail safe on occupied slot). **#38 was root-caused 2026-07-19 and fixed in 0.30.0** — the /CreateContent "failures" were never flaky: field 3 of the `/status` reply is the device's edit-buffer dirty flag, not an error code, and the writes were landing (the old client then deleted them). Re-running was never the fix. Creates now confirm by re-list, so a `/CreateContent` error today is never raised on a create the listing confirmed — but read the message before retrying: it distinguishes "genuinely absent" (safe to retry) from "could not be read at all" and "listed but no cid reported" (content may well be there; verify first or a retry duplicates it).

**Machine-local advisory device locks.** Every device-mutating verb auto-acquires lease file (`~/.helixgen/locks/<ip>/<scope>.lock`) for duration, so concurrent helixgen processes on this machine never collide on device; read-only verbs take nothing. Scopes: `editbuffer`, `library`, `irs`, `globals`, `all`. Hold scopes across calls with `device lock
--scope all --label <who>` (export printed `HELIXGEN_LOCK_TOKEN` so own verbs pass through; same-shell calls pass through automatically); inspect with `device lock --status`, release with `device unlock`. Contended verbs wait `$HELIXGEN_LOCK_TIMEOUT` s (default 30) then error naming holder; stale leases reclaimed, live ones never. Run `device lock` from long-lived shell, not wrapper script (session leases get only 120 s dead-pid grace). **Agent driving multi-call device work: take `helixgen device lock --scope all --pid $PPID --label "<who>"`** (0.33.0, workspace #97b) — binds lease to process YOU name that spans whole workflow (`$PPID` from a tool call = long-lived `claude` process), not the tool-call shell that exits when the call returns; plain session lease records that shell's pid and contender reclaims it 120 s later, mid-workflow (observed on hardware 2026-07-27). `kind: "pid"` liveness is DECIDABLE, so **no 120 s dead-pid grace** (dead owner reclaimable at once); TTL still bounds an IDLE lease (keeps ordinary 900 s default, renewed by every token-carrying verb) and is the ONLY reclaim path where liveness can't be probed (other host, or Windows — where a `--pid` lease is TTL-bounded like a detached one and a dead `--pid` is not refused). A `--pid` lease passes through by TOKEN, not by shell (recorded pid is the one YOU named, not the caller) — export it. `--pid` whose process is not alive right now is REFUSED (lease for dead owner is stale on arrival); mutually exclusive with `--detach`. Lease identity is the **`(pid, pid_start)` pair** — pid numbers recycle, so a pid whose start time no longer matches the recorded one reads DEAD, never immortal. **`--detach` is for work with NO owning process** (cron, CI): records NO pid, TTL-only, default 300 s, `--ttl 0` refused with `--detach` (no pid AND no expiry = only `--force` clears it); any `device lock --ttl`, detached or not, also refuses a positive TTL under 10 s (too short for renewal to keep alive), a NEGATIVE one (plausible typo for the positive; "no expiry" has exactly one spelling, `--ttl 0`), or a non-finite one. Both kinds renewed by every verb run with token exported (reads included), released by `device unlock`. **Dangling token fails loudly, reads included** (0.33.0, workspace #97): `$HELIXGEN_LOCK_TOKEN` set but opening **no live lease at all** on device errors naming current holder — on read-only verbs too (`measure`/`meters`/`tuner`/`blocks`/`params`/`active`/`watch`, `info`/`list`/`setlists`/`read`/`pull`/`backup`/`export-hss`/`slots list --verify`, `list-irs`/`pull-ir`, `settings list --values`/`settings get`, `ir-prune` dry run); no token = unchanged, unlocked reads stay free; `lock`/`unlock`/`discover` + offline verbs exempt so recovery never locked out. Scope outside a NARROW lease is not a lost session — holding `library` and running `editbuffer` verb acquires that scope transiently as before; EXCEPT a **read** of a scope a live foreign lease holds right now, which errors too (mutating verb contends visibly, read has no such fallback — and a multi-scope lease that lost one scope is indistinguishable from never having held it once its lease file is gone). A lost scope whose **expired lease file is still on disk** is proof, so it errors for mutating verbs too; so does a live lease within 2 s of expiry (nothing renews that close to the boundary). Lease lost mid-call (long `measure`/`watch`/`normalize`): heartbeat prints a `lapsed DURING this call` stderr warning — treat as a lock error (fires on losing ANY one lease held at entry, not only the last). Leases keyed by **device ADDRESS**, check too: token whose only live lease is under another address (second Stadium, or same one as `helix.local` vs dotted-quad) is NOT a lost session — no error, but no lease held there either, so **reads warn on stderr that the call is UNLOCKED** (naming the address the lease sits under); keep one address spelling for whole session. **Operating rule for agent multi-call device work:** take `--pid $PPID` lease up front, export token, `device unlock` at end (including on failure); every verb that takes or checks a device scope renews EVERY lease that token owns (reads too, not just the verb's own scopes), and a verb longer than the TTL keeps renewing in flight via a background heartbeat, so only an IDLE stretch longer than TTL loses it — size `--ttl` to cover longest gap. **The exempt verbs renew NOTHING** — `device lock --status`, `unlock`, `discover`, and every offline verb (`slots list` without `--verify`, `settings list` without `--values`, `library`, `local-list`, …): polling `lock --status` is NOT a keepalive, and a stretch of only those verbs ages the lease out exactly as idleness does. `device unlock` can't unset `$HELIXGEN_LOCK_TOKEN` in your shell: unset it after (verb says so on stderr) or every later verb refuses. **Any lock error = stop and re-establish state, never retry-the-call-and-continue**: device may have been driven by someone else, so re-take lease and re-read active preset/snapshot/block state before acting. `--no-lock` opts out (dangerous) — MUTATING verbs only, and it skips the dangling-token check too; a read carries no such flag, its only opt-out is `unset HELIXGEN_LOCK_TOKEN`. Advisory + machine-local only — other hosts + Stadium desktop editor NOT covered. **Locking landed in 0.22.0: pre-0.22.0 clients take no leases and ignore existing ones — running one against the device in parallel with any other client is unsafe (collides as if no locks). Upgrade every helixgen on the machine to ≥0.22.0 before relying on locks.** Full verb → scope table: [`docs/CLI.md`](docs/CLI.md) "Device locks".

**Stadium network stack flaky — sync/verb drops or stalls: re-run. `sync` + live-ops verbs idempotent + auto-reconnecting; slot-writing verbs (install/save/push/create) fail safe on occupied slot instead — but a `/CreateContent` failure can leave an EMPTY stub, so read that error before re-running (see above); `setlist import-hss` is the one NOT-idempotent retry. Keeps dropping: reboot Helix.**

**Tone library = single management record.** Every tone helixgen generates auto-registers into manifest (`~/.helixgen/setlists/manifest.json`). **Tone** = content + identity + management **intent**: desired **user slot** (`null` = off device, `"auto"` = wants device, or `"1A".."128D"` — a **manifest-only** vocabulary since 0.30.0: `device add --slot` accepts only `auto` and rejects explicit labels, because sync never converted a label into a device address, #30. Place with `auto`, then move with `device reorder`) plus **setlist memberships**. **"On device" ⟺ tone has slot.** No separate slot ledger. Presets addressed by integer **CID**; preset lives once in **pool** (`-2`), referenced by **setlists** under setlists root `-5`. **Sync = managed-set mirror** — installs/updates/reorders/deletes only tones helixgen manages, **never touches untracked device presets**. Specific Helix's **observed** placement not part of manifest — see "Home directory" above.

**Pushing tones to device driven by `device` skill** (plugin repo, `sheax0r/helixgen`) — runs after `tone` authored `.hsp`, centers on `device sync <setlist>`. Read before scripting setlist sync. Design + protocol refs: [`docs/CLI.md`](docs/CLI.md), `docs/helix-protocol.md`, `docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`.

## IR cab-pack catalog (character reference)

IR library at `irs/` (gitignored — paid packs stay local) carries grep-first tonal catalog at `irs/_catalog/`. Answers "which IR beefiest / brightest / best for vintage clean / tightest for modern metal" without re-analysing WAVs. Start at `irs/_catalog/README.md` (index + controlled tag vocabulary + mic legend + example greps); one file per pack holds per-mix mic combos + character tags.

**New IR pack added to `irs/` — catalog before moving on:**
1. Read pack's `*Manual*.pdf` — cab/speaker/amp, mic legend, per-mix mic combos, artist/usage notes.
2. `ls` pack's `Mixes/` folder for exact WAV basenames (what preset's cab block references via `mapping.json`).
3. Optionally FFT-analyse each Mix WAV (stdlib `wave` + `numpy`, 5 guitar bands) for measured bright/dark/beefy/tight tags — relative *within* pack.
4. Write `irs/_catalog/<slug>.md` from template in catalog README, ONLY controlled vocabulary; add row to README index table.

Don't invent character manual doesn't state; well-established general knowledge fine (Greenback = classic-rock, V30 = modern metal, ribbon = warm top, SM7 = fat). Catalog README "Adding a new pack" section = authoritative procedure + self-documenting template.

## Architecture: `.hsp` is the source of truth

`.hsp` file = 8-byte magic `rpshnosj` followed by JSON document — **is** canonical, editable artifact. No persisted intermediary spec, **no `.spec.json` sidecar**. Two flows act on it:

- **Author** new preset by feeding transient **recipe** (JSON shape below) to `generate`; helixgen clones chassis template, replays recipe as in-place mutations. Recipe input-only — not written to disk, never read back as truth.
- **Edit** existing `.hsp` with surgical verbs (`set-param`, `enable`, `add-block`, …); each reads `.hsp`, mutates body in place, writes `.hsp` back. No recompile, no sidecar.
- **Import** a preset NOT authored here — one made on the hardware or in HX Edit — with **`helixgen device to-hsp <file.sbe|cid> -o out.hsp`**: the REVERSE transcoder (`device/untranscode.py`) turns device content (`_sbepgsm`) back into a real `.hsp`, after which it is an ordinary helixgen artifact and both flows above apply. Device content is no longer a dead end. `.sbe` → `.hsp` → `.sbe` is BYTE-EXACT for content helixgen installed; content the DEVICE re-saved differs only in device-side serialization conventions + `cg__` id numbering, and the conversion is a fixed point after one pass. Detail: [`docs/CLI.md`](docs/CLI.md) "Device commands".

Read `.hsp` back into recipe shape (inspection or hand-authoring similar preset): `helixgen view <preset.hsp>` — read-only projection.

## recipe shape (author input to `generate`)

**Recipe** = JSON author-input to `generate`. Input-only — never written to disk, never read back as truth. Base shape:

```json
{
  "name": "Preset Display Name",
  "author": "you",
  "paths": [
    {
      "blocks": [
        {"block": "Compulsive Drive", "params": {"Gain": 0.45, "Tone": 0.55}},
        {"block": "Brit Plexi Brt",   "params": {"Drive": 0.7, "Master": 0.5}},
        {"block": "4x12 Greenback 25"},
        {"block": "Tape Echo", "params": {"Mix": 0.18}},
        {"block": "Plate",     "params": {"Mix": 0.12}}
      ]
    }
  ]
}
```

- `paths` = 1–2 entries (each maps to one DSP).
- `block` = display_name from `list-blocks` (case-sensitive) **or** the model_id written plainly (`"HD2_AmpBritPlexiBrt"` — no brackets); both work everywhere a block is named, placement and every snapshot/footswitch/expression/MIDI reference. Display names are the EDITOR's own and unique per library, resolved at read time from the vendored `model_names` table so an existing library self-corrects on upgrade (hgc-3ll) — they are no longer the device's short `@name`, which truncated (`ressor LAStudio Comp Mono`) and collided (`Stereo` named six models). **model_id is the stable handle**: prefer it when scripting. Same-name models are split deterministically (`Woody Blue` / `Woody Blue (Preamp)`, `Ping Pong` / `Ping Pong (Legacy)`); a pre-fix library's old name survives as a **legacy alias**, so old recipes still resolve.
- `params` values are in each param's OWN units — many knobs are 0.0–1.0, but plenty are dB, Hz, seconds, or enum ints, and the same name (`Level`) is 0..1 on one block and dB on the next. **Never assume; read the range + unit `show-block` prints** (hgc-285).

**Exhaustive per-field reference — every optional section, full schema, defaults, ranges, examples — lives in [`docs/recipe-reference.md`](docs/recipe-reference.md).** Optional sections by name: per-path `input` (jack routing + Input-block params) + `output` (level/pan); `split`/`join` in `blocks` (parallel splits + merge-mixer wire params); top-level `snapshots` (≤8 named scenes: per-scene `disable` + `params` deltas + per-snapshot `output` level/pan), `footswitches` (FS1–FS5/FS7–FS11/EXP1Toe; FS6/FS12 reserved), `expression` (EXP1/EXP2 sweeps), `midi` (EXPERIMENTAL #33), `commands` (Command Center; EXPERIMENTAL #16); per-block `ir` (registered user IR by wav basename or 32-hex hash), `trails`, `raw` (verbatim unmodeled state — emitted by `view`, consumed by `generate`; editing existing `.hsp` never needs it). All recipe fields **Stadium-only** unless reference notes otherwise (legacy `.hlx` chassis ignores them).

**One-controller-per-param.** `(block, param)` driven by at most one of footswitch-param / expression / MIDI across whole spec (block's *bypass* may have several sources).

**Controller vocabulary & English rendering (agent behavior).** Reporting tone to human: render controllers in English (via `helixgen
controllers`), never bare `FS#` (e.g. `Footswitch 5 (top row, 5th from
left)`). Human *describes* control in plain language: translate to canonical identifier with dedicated small-model sub-agent fed `helixgen controllers --json` — returns exactly one identifier (or `AMBIGUOUS`/`NONE`); validate against canonical set before writing into recipe. `view` never drops controls it can't map — unmapped ones land in `unknown_controllers` (round-trip safe). Full detail: [`docs/recipe-reference.md`](docs/recipe-reference.md).

## User preferences (`preferences.json`)

`setup` / `tone` skills (plugin repo) read explicit settings from `~/.helixgen/preferences.json` (whole-file override `$HELIXGEN_PREFS`; per-key override `HELIXGEN_<KEY>`, e.g. `HELIXGEN_FAVOR_IRS=1`). Loaded by `src/helixgen/preferences.py`; precedence env var > file value > built-in default. Keys include `device.model`, `favor_irs`, `reveal_in_finder`, `guard_paid_irs_in_git`, `author`, `git_commit_tones` (default `"auto"`), **`default_guitar`** — guitar profile used when tone request doesn't name one (unset: `tone` skill asks, offers to save answer). **`normalization`** (he-xth) is the loudness-protocol block: `mode` — **defaults to `sample`**, which replays the stimulus BUNDLED IN THE PACKAGE (`helixgen/assets/helix-cal-loop.wav`) so a fresh install measures unattended; `play` = you hand-play a window per target (the fallback when no player binary exists); `looper` = an on-device looper, which implies `--source loop`, `target_db` (the shipped 17.5 dB reference; written into a profile by the `setup` skill or by `scaffold_default`, NOT defaulted inside normalize — an unset target still self-anchors) + `target_source` provenance, `seconds`, `tolerance_db`, `measure_via`, `capture_input`, plus nested `sample` (stimulus path, loop length, playback command, output device, volume) and `calibration` (reference/achieved `input_db`, the guitar it was taken with, the date). ABSENT = today's behavior; `device normalize` reads it for defaults and `device calibrate` writes it. Scalars take `HELIXGEN_NORMALIZE_*` env overrides; the nested blocks are file-only. Keys `instruments` + `preset_output_dir` **deprecated** (replaced by guitar profiles + `library/tones/` default write location): still parsed for back-compat, warned once per process, removed by `library migrate`.

## Tone naming and the library

**Naming schema (supersedes old `"<Tone Name> — <Guitar>"` convention).** Tone display name = `"$Artist - $Song - $Guitar"`, or `"$Descriptor -
$Guitar"` when no artist/song (guitar = target guitar's short name). Guitar segment omitted only for explicitly guitar-agnostic tone (generic patch). Filenames same schema, slugged lowercase-with-dashes (e.g. `foo-fighters-white-limo-les-paul-jr.hsp`). Guitar resolution order in `tone` skill: user-named guitar wins; else `default_guitar`; else skill asks, offers to save choice as `default_guitar`.

**Logical tone vs. variant.** **Logical tone** — one artist+song, or one descriptor — owns exactly one metadata JSON at `library/tones/<logical-slug>.json`, plus one or more **variants**, each real `.hsp` targeting single guitar, keyed by guitar's profile slug (or `"generic"` for guitar-agnostic variant). Manifest + device still key by *variant's* display name — that's what device preset is; metadata JSON just groups variants sharing identity. New variant of existing tone = `generate --guitar <other-guitar>` against same artist/song/descriptor. Tone write-up lives in metadata's `description_md` (authored via `helixgen library doc` — **no `.md` sidecar files**); per-variant notes in variant's `notes_md`.

## Guitar profiles

**Guitar profile** at `library/guitars/<slug>.json` (schema 1) = single source of truth for one guitar user owns — replaces `preferences.instruments`. Fields: `name`, `short_name` (appears in preset display names / filename slugs), `type`, `active`, `pickups`, `construction`, `character_md` (what guitar is *for* — read by `tone` skill to adapt params), `genres[]`, `controls[]` (control inventory variant's `guitar_settings` keys validate against). `--guitar <label>` resolves by slug / name / short_name, case-insensitive, most-specific tier first — ambiguity + unknown labels error; with **no** profiles yet, literal `slugify(label)` fallback keeps pre-migration authoring working (full resolution rules: [`docs/CLI.md`](docs/CLI.md) "Guitar profiles / resolution"). Profiles seeded from `preferences.instruments` by `library migrate`; scaffold new one with `helixgen library add-guitar` (also auto-commits); create/edit details via `setup` skill.

## The `helixgen library` verb group

Verb group over artifact library — tones, guitar profiles, per-IR metadata. Every library-mutating verb auto-commits home repo after (advisory, gated by `git_commit_tones`). **Full per-verb reference: [`docs/CLI.md`](docs/CLI.md) "Library commands".** Verb index: `library
list` (all three sections; `--tones`/`--guitars`/`--irs` narrows), `library
show <name>` (tone-first resolution, then guitar profile), `describe
<tone>` (longer human-oriented counterpart), `library doc` (author `description_md` / variant's `notes_md`), `library validate` (shape + cross-link checks: **problems** exit 1, **warnings** don't), `library
add-guitar`, `library import` (bring external `.hsp` under naming schema; never overwrites), `library migrate` (one-shot idempotent pre-library-to-library migration; `--dry-run` first), `library ir-backfill` (copy + scaffold metadata for IRs registered before library layout).

## Surgical edits

Preset exists: don't re-author to change one setting — use edit verbs. Each reads `.hsp`, mutates body **in place**, writes back, reusing all helixgen validation, model-id translation, IR injection. Works on ANY `.hsp` — helixgen-authored or raw device export — no decompile step, no sidecar. Fields helixgen doesn't model (dual-cab slots, harness, `xyctrl`, …) preserved untouched by construction. **Grid LAYOUT preserved too** (hgc-hhp): an edit no longer re-packs the row onto `b01..bn`, so a device-authored preset — 64 of Line 6's 66 factory presets ship a gapped row — keeps every block on the slot the user sees on the hardware. `remove-block` leaves a hole; `add-block` slides only the run of blocks in the way one slot into the nearest gap (either direction). A run boxed in both ways still re-packs, and says so.

**Run `helixgen show-block "<block>"` first** to confirm exact case-sensitive param name — same guardrail `generate` enforces.

Verbs — full signatures + per-flag detail in [`docs/CLI.md`](docs/CLI.md) "Commands":

- `set-param <preset> <block> <param> <value>` — one param, auto-coerced. **Negative** value needs `--` sentinel (`helixgen set-param t.hsp
  output level -- -3`). Block names `input` / `output` / `split` / `join` (`merge` alias) = **signal-flow pseudo-blocks** addressing path's endpoints / split / merge mixer.
- `enable` / `disable <preset> <block>` — un-bypass / bypass at base level.
- `add-block`, `remove-block`, `swap-model` (same-category only; carries over shared params, warns on dropped ones — surface those warnings).
- `view <preset.hsp>` — read-only `.hsp` → recipe projection.

`--snapshot NAME-or-INDEX` on `set-param`/`enable`/`disable` targets ONE snapshot's slot instead of base. **Gotcha:** once param's per-snapshot array varies, device applies it on every snapshot — later plain base edit of that param inaudible on-device (`set-param` warns). `--path`/`--lane`/`--pos` disambiguate duplicate block names — block addressing = `(path, lane, pos)`; no `--index`.

Multi-edit session: **`helixgen patch <preset.hsp> <ops.json|->`** applies JSON **list** of ops (`set_param`, `set_enabled`, `add_block`, `remove_block`, `swap_model`) in one atomic invocation — invalid op anywhere leaves `.hsp` untouched. Op fields mirror single-op verbs' flags. Agent edit loop = single `patch` call on file — no decompile/regenerate round-trip:

```bash
echo '[{"op": "set_param", "block": "Tape Echo", "param": "Mix", "value": 0.3},
       {"op": "set_enabled", "block": "Plate", "enabled": false}]' \
  | helixgen patch MyTone.hsp -
```

## Generation notes

- Chassis = whatever first ingested. Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from originating export currently expected.
- Some Stadium model IDs translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.

## Project layout

- `src/helixgen/` — `cli` (core verbs + entry point), `cli_device` (`helixgen device` verb group, imported back into `cli`), `audio_metrics` (offline BS.1770 / crest / FFT-band DSP), `audio_capture` (sox capture of the device's USB output + middle-segment analysis), `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `ir`, `irhash_cache`, `locks` (machine-local advisory device locks), `home`/`libinit`/`gitops` (`~/.helixgen` home root, auto-init, advisory auto-commit), `naming`, `tone_meta`, `guitars` (guitar profiles), `ir_meta` (per-IR sidecars), `migrate` (library migration), `cli_library` (`helixgen library` verb group)
- `src/helixgen/device/` — network device control (OSC-over-ZeroMQ client, `transcode` = `.hsp` -> device content, `untranscode` = device content -> `.hsp`, `modelmap`, `defs`, `paraminfo` = read-time param ranges/units/enum labels for `show-block`, setlist manifest)
- `docs/` — `BACKLOG.md` (legacy backlog archive; live backlog is beads), `CLI.md` (full CLI + per-verb **device** reference), `recipe-reference.md` (exhaustive recipe field reference), `superpowers/specs/` (design docs + review findings), `superpowers/plans/` (implementation plans), `features/` (per-feature deep dives), protocol references (`helix-protocol.md`, `helix-format-reference.md`, `helix-sftp-access.md`, `ir-hash-algorithm.md`)
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); golden-output contract (`tests/golden/`) + 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity. **Runs parallel by default** — `addopts = -ra -n auto` (pytest-xdist, a `[dev]` dep; needs `pip install -e '.[dev]'`, else plain `pytest` errors `unrecognized arguments: -n`). Force serial with `-n0` (debugging: prints/pdb/order-sensitive). Live suite forces itself serial (`tests/live/conftest.py` `pytest_configure`).
- `tests/live/` — **opt-in live integration suite** (backlog #66): drives real CLI via subprocess against real library + real Stadium. Skipped unless `HELIXGEN_LIVE=1` (device tests also need device reachable). Impact-area markers registered in `pyproject.toml`; after targeted change run its blast radius, e.g. `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and sync" tests/live`. Safety = fixtures (scratch env for ALL local state, upfront `device backup`, before/after device-state diff, `HGTEST`-prefixed artifacts with teardown, session check that real `~/.helixgen` byte-identical after); **`tests/live/conftest.py` documents full safety model**, deliberately excluded verbs, known-gotcha xfails.
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — user's personal `.hsp` exports
- `irs/` (gitignored) — paid commercial IR packs; character catalog at `irs/_catalog/`

## Development workflow

- **Worktrees, branched from fresh `origin/main`.** All non-trivial work in git worktree whose branch starts from freshly-fetched `origin/main` (GitHub remote is named **`origin`** — renamed from `github` when beads landed, since beads bootstraps its Dolt data from `refs/dolt/data` on `origin`) — never commit directly on local `main`; may be stale. Fetch again before picking release version number (concurrent PR once released 2.10.0 mid-flight, collided with in-progress bump).
- **Adversarial review before shipping.** Before merging PR, dispatch at least one independent review subagent prompted to *break* change (find bugs, regressions, spec violations — not summarize). Confirmed findings fixed or explicitly deferred as a bead (`bd create`). Major changes also get committed review doc in `docs/superpowers/specs/` (see PR #31 review for shape).
- **Agent-facing surfaces ship in sync.** CLI = only engine surface; per-verb `--help` text = agent contract (pinned by `tests/test_cli_parity.py`). Any change to CLI-visible behavior updates, same PR, every surface in this repo describing it: verb's help text, this CLAUDE.md, `docs/CLI.md`. Drift between code + these surfaces = bug, not docs chore. **Division of labor:** reference detail (flags, semantics, gotchas) belongs in `docs/CLI.md` / `docs/recipe-reference.md`; this CLAUDE.md carries mental models, behavioral rules, verb indexes — don't grow per-verb prose here when reference doc is right home. Behavior changes skills describe also need companion PR in plugin repo (`sheax0r/helixgen`, `.claude/skills/*`) — land two together, note cross-repo pairing in both PR descriptions.
- **Backlog discipline.** Beads = the single project backlog. `bd ready` to pick work, `bd update <id> --claim` before starting, `bd create` for anything deferred — never a TodoWrite list, a markdown TODO list, a TODO comment, or a new entry in `docs/BACKLOG.md` (archive only).
- TDD throughout: failing test first, then minimal implementation. See existing test files for established pattern.
- Pure stdlib + `click` for CLI; no other runtime deps.
- Real-export fixtures in `tests/fixtures/presets/`, loaded under skip-if-not-present guards so suite stays green on clean clone.

## Releasing

This repo releases **`helixgen` PyPI package** (version in `pyproject.toml` + `src/helixgen/__init__.py` — bump both together; version feeds generated presets' `meta`). Publishing via OIDC trusted-publisher workflow (`.github/workflows/publish.yml`) on `vX.Y.Z` tags pushed to `main` (first publish 0.19.1).

Plugin releases (`stable` branch + `helixgen--vX.Y.Z` tags) live in **plugin repo** (`sheax0r/helixgen`), owned by its release workflow — nothing in this repo moves those refs. Core release changes behavior skill depends on: cut core release first, then bump plugin's pinned `helixgen` version in its own PR.

## ralphex

Implementation tasks driven from helix coordination workspace run via [ralphex](https://github.com/umputun/ralphex) plan files in `docs/plans/` (scaffold: `docs/plans/TEMPLATE.md`); completed plans move to `docs/plans/completed/`. Config = tracked `.ralphex/config` (`default_branch` pinned `main`); runtime dirs `.ralphex/worktrees/` + `.ralphex/progress/` gitignored. Launcher syncs local `main` from `origin/main` before run. Review = ralphex built-in pipeline (`external_review_tool = none`).


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
