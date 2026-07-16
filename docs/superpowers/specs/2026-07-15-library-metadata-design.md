# Library metadata design — guitar profiles, tone metadata, IR metadata (backlog #22/#35/#36)

**Date:** 2026-07-15
**Status:** approved design (brainstormed with the user; every decision below was
explicitly confirmed)
**Supersedes:** the `"<Tone Name> — <Guitar>"` naming convention; the manifest
`doc` sidecar-path field; `preferences.instruments`; `preset_output_dir`;
#35's original `$PLUGIN_DATA_DIR` idea (predates the MCP removal, #63 — core
owns the metadata home now).

## 1. Summary

Three coupled backlog items become one feature: a real **artifact library** at
`~/.helixgen/library/` that owns tones, IRs, and guitar profiles as files, with
per-entity JSON metadata linking them. The user's `~/.helixgen` becomes a git
repo (intent committed, observations and paid IR audio not). Tone naming
adopts `$artist - $song - $guitar` with a descriptor fallback. The manifest
splits into committed **intent** (`setlists/manifest.json`) and per-device
**observed** state (`devices/<serial>.json`), motivated by multi-device
support: observations come from a *specific* Helix.

## 2. Homes and git topology

```
~/.helixgen/                    ← THE git repo (auto-init if git installed)
  .gitignore                    ← devices/, cache/, tone3000/, *.bak*, library/irs/**/*.wav
  preferences.json              ← committed
  library/                      ← artifacts ($HELIXGEN_LIBRARY, unchanged env var)
    blocks/ chassis.json index.json   ← existing block library, untouched
    tones/    <logical-slug>.json + <variant-slug>.hsp
    guitars/  <slug>.json
    irs/      mapping.json + <pack>/<name>.wav (ignored) + <pack>/<name>.json
  setlists/
    manifest.json               ← v3 intent-only manifest ($HELIXGEN_SETLISTS)
  devices/                      ← observed state, one file per Helix serial; NOT committed
    <serial>.json
  cache/                        ← unchanged (irhash cache etc.); not committed
```

Decisions:

- **One git repo at `~/.helixgen`** (not per-subdir repos). On first library
  write, if `git` is installed and the dir is not already inside a repo:
  `git init`, write the `.gitignore`, initial commit. If git is absent, skip
  quietly (one-line notice).
- **`.gitignore` excludes IR WAV *audio* only** (`library/irs/**/*.wav`), not
  the `irs/` dir — per-IR metadata JSON and `mapping.json` stay committed;
  paid IR audio never lands in git. The user may delete the line to track WAVs.
- **Core owns commits now.** Every library-mutating verb auto-commits with a
  descriptive message, gated by the existing `git_commit_tones` preference
  (default `auto`). This deliberately moves #26's commit responsibility from
  the skills into core — correct in the CLI-only world. Commit failure warns
  and never fails the operation (same advisory posture as auto-registration).
- **Importing moves things in.** Importing a tone moves its `.hsp` into
  `library/tones/` (`--keep-source` copies). Importing/registering an IR
  **copies** the WAV into `library/irs/<pack>/` — copy, not move, because the
  user's paid packs live in a curated tree (`irs/` + `_catalog/`) that must
  stay intact as the browsing source.

## 3. Manifest split: intent vs observed

`setlists/manifest.json` (**MANIFEST_VERSION 3**) holds only **intent** — the
fields the user (or skills acting for them) writes:

- per-tone: `path`, `content_hash`, `source`, `slot` (desired placement)
- `setlists`: name → ordered `tones` list + `synced` flag

Removed from the manifest relative to v2:

- `doc` — retired; the tone metadata JSON replaces it.
- per-tone `device: {cid, posi}` and the top-level `observed` section — these
  are **observations from a specific device**, rebuilt wholesale by every
  sync. They move to `devices/<serial>.json`, keyed by the serial reported by
  `device info` at connect time. A second or replacement Helix simply gets its
  own file. Losing a devices file costs nothing — the next sync rebuilds it.

Loading a v2 manifest migrates automatically (split + move + `.bak` backup,
matching the existing backup pattern). Desired `slot` stays single-valued in
intent for now; **per-device slot intent is explicitly future work** if a
second Helix materializes.

## 4. Identity and naming

- **Naming schema:** display name `"$Artist - $Song - $Guitar"` (guitar =
  profile `short_name`). Tones without an artist/song use the descriptor
  fallback `"$Descriptor - $Guitar"` (e.g. `"Warm Jazz Clean - Les Paul Jr"`).
  The guitar segment is omitted only for explicitly guitar-agnostic tones.
  Filenames are the same schema slugged lowercase-with-dashes
  (`foo-fighters-white-limo-les-paul-jr.hsp`).
- **Logical tone vs variant:** a **logical tone** (artist+song, or descriptor)
  owns one metadata JSON and one or more **variants**, each a real `.hsp`
  targeting one guitar (backlog #35 part 3, confirmed default (b): per-variant
  presets, not snapshot replication). The manifest and the device keep keying
  by the *variant's* display name — that is what a device preset is. Metadata
  groups variants; nothing else changes identity-wise.
- Metadata records `artist` / `song` / `descriptor` as separate fields either
  way (exactly one of `song`/`descriptor` is set).
- Slug collisions error with a rename suggestion.

## 5. Schemas (all `"schema": 1`)

### 5.1 Tone — `library/tones/<logical-slug>.json`

```json
{ "schema": 1,
  "artist": "Foo Fighters", "song": "White Limo", "descriptor": null,
  "tags": ["hard rock", "lead"],
  "description_md": "…the full companion markdown, folded in…",
  "variants": {
    "gibson-les-paul-junior": {
      "hsp": "tones/foo-fighters-white-limo-les-paul-jr.hsp",
      "preset_name": "Foo Fighters - White Limo - Les Paul Jr",
      "guitar_settings": {"pickup": "bridge", "tone": "7"},
      "notes_md": null } },
  "created": "2026-07-15", "updated": "2026-07-15" }
```

- The companion `.md` is **folded into `description_md`** — no sidecar file,
  no sidecar path (supersedes the manifest `doc` field and skill step 7a).
- Variant key = guitar profile slug; the special key `"generic"` marks a
  guitar-agnostic tone (name omits the guitar segment).
- `guitar_settings` expresses how to set the target guitar's knobs/switches
  for this tone, keyed by control names from that guitar's profile control
  inventory; unknown control names **warn** (never fail).
- `notes_md` carries variant-specific notes; shared prose stays in
  `description_md`.

### 5.2 Guitar profile — `library/guitars/<slug>.json`

```json
{ "schema": 1,
  "name": "Gibson Les Paul Junior", "short_name": "Les Paul Jr",
  "type": "guitar", "active": false,
  "pickups": "one bridge P-90 (soapbar single-coil)",
  "construction": null,
  "character_md": "P-90 grind; raw rock rhythm; brighter than a humbucker LP…",
  "genres": ["punk", "garage", "raw rock", "blues"],
  "controls": [
    {"name": "volume", "kind": "knob"},
    {"name": "tone", "kind": "knob", "notes": "no coil split"} ] }
```

- **Replaces `preferences.instruments`** (single source of guitar truth).
  Migration seeds profiles from the user's 4 existing instruments entries.
  `default_guitar` stays in preferences and now names a profile (slug or name).
- `short_name` is what appears in preset names/slugs.
- `controls` is the **control inventory** (#22): named knobs/switches with
  kind, optional positions/notes — what `guitar_settings` keys validate
  against, and what profile-aware tone generation reads.
- `character_md` (tonal character, what the guitar is *for*) informs how the
  tone skill adapts params per guitar (e.g. brighter amp for humbuckers).

### 5.3 IR — `library/irs/<pack>/<name>.json` (sidecar next to the copied WAV)

```json
{ "schema": 1,
  "irhash": "553b0d…", "wav": "irs/york-audio-bogn/YA BOGN Mix 01.wav",
  "imported_from": "/Users/…/irs/YA BOGN/Mixes/….wav",
  "pack": {"name": "York Audio BOGN", "manual": "…pdf"},
  "cab": "Bogner 4x12", "speaker": "V30", "mics": ["57", "121"], "mix": "Mix 01",
  "tags": ["tight", "mid-forward", "modern"],
  "measured": null, "notes_md": null }
```

- **Per-IR, pack-informed** (#36 confirmed): provenance mined from the pack
  manual / `_catalog` when available; filename heuristics + optional FFT
  5-band pass (`measured`) when no docs exist.
- `tags` use the `_catalog` README's **controlled vocabulary** (fold that
  pipeline in — same vocabulary, same mic legend; do not duplicate it).
- `pack` subdir = source directory name, preserving pack grouping and
  avoiding basename collisions across packs.

## 6. CLI surface

New **`helixgen library`** verb group (all mutating verbs auto-commit):

- `library migrate [--dry-run | --plan <plan.json>]` — the one-shot migration:
  git-init `~/.helixgen`; move existing tones (`.hsp` + companion `.md`) into
  `library/tones/`, folding each `.md` into `description_md`; rename to the
  new schema; convert `instruments` → `guitars/*.json`; copy every registered
  IR WAV into `library/irs/<pack>/`, scaffold its metadata, rewrite
  `mapping.json` to the library copies; split the manifest v2 → v3 +
  `devices/<serial>.json`; update manifest paths; commit. `--dry-run` emits an
  editable **plan** (old name → artist/song/descriptor/guitar/new name) so the
  agent or user corrects inferences before executing; unresolvable names fall
  back to descriptor = old name. Idempotent; re-runnable.
- `library import <file.hsp|dir> [--artist … --song … --descriptor …
  --guitar …] [--keep-source]` — move an external tone in (copy with
  `--keep-source`), fold a sibling `.md`, register, commit.
- `library list [--tones|--guitars|--irs] [--json]`
- `library show <name> [--json]` — any entity by slug/name.
- `helixgen describe <tone>` — human-friendly: summary + `description_md` +
  per-variant guitar settings (the #35 part-2 verb).
- `library doc <tone> [--variant <guitar>] (--from-file <md> | -)` — set
  `description_md` / a variant's `notes_md`. **This is how the tone skill
  authors descriptions now** (no more `.md` sidecars).
- `library validate [--json]` — schema + cross-link checks: variant `.hsp`
  exists; guitar slug exists; `irhash` known to `mapping.json`; `preset_name`
  matches the manifest key; IR tags in the controlled vocabulary.
- `library ir-backfill` — scaffold metadata for library IRs lacking it
  (idempotent; the skill then enriches provenance/tags).

Changed verbs:

- **`generate`**: `-o` becomes optional. Default: write
  `library/tones/<slug>.hsp`, create/update the logical tone metadata (adding
  a variant entry), auto-register, commit. New flags `--artist --song
  --descriptor --guitar <profile-slug>` drive naming + metadata; a bare recipe
  `name` becomes the descriptor. Explicit `-o` outside the library preserves
  today's behavior exactly (no metadata authored).
- **`register-irs` / `ir-scan`**: copy each WAV into `library/irs/<pack>/`,
  scaffold its metadata JSON, point `mapping.json` at the library copy.
- `register --doc` retired. `preferences.instruments` and `preset_output_dir`
  deprecated: loading warns, migration removes them.
- `device sync`: unchanged except observed state writes to
  `devices/<serial>.json`.

Creating a **variant** = `generate --guitar <other-guitar>` against the same
artist/song — new `.hsp`, new variants entry. The tone skill reads the guitar
profile (`library show <guitar> --json`) to adapt params.

Deferred (not in v1): field-level metadata setter verbs (skills edit JSON +
`library validate` catches errors), name-based addressing on the surgical edit
verbs, per-device slot intent.

## 7. Cross-repo: plugin skills (companion PR in `sheax0r/helixgen`)

- **tone skill:** adopts the naming flags; authors descriptions via
  `library doc`; adapts params from guitar profiles (`character_md`,
  `pickups`); offers per-guitar variants via structured questions (only when
  multiple guitars are plausible; single guitar → don't ask, per #22); stops
  writing `.md` sidecars; stops git-committing library paths (core owns those
  commits).
- **setup skill:** scaffolds/edits guitar profiles (including the control
  inventory) instead of the `instruments` array; offers `library migrate` to
  existing users.
- **device skill:** unchanged except path/observed-state documentation.
- **IR enrichment:** the `_catalog` mining procedure (manual → mics/mix/tags)
  becomes the skill step that fills per-IR metadata after import/backfill,
  using the same controlled vocabulary.

## 8. Error handling

- git absent → skip commits, one-line notice. Commit failure → warn, never
  fail the operation.
- Migration: idempotent, `--dry-run`, backs up the v2 manifest (`.bak`).
- Slug collisions → error with rename suggestion.
- Missing `.md` on import → `description_md: null` + warning.
- Unknown `guitar_settings` control names → warning (profile may lag).
- `library validate` is the safety net for hand/skill-edited JSON.

## 9. Testing (all local, TDD)

- Schema validators; naming/slug rules (schema, fallback, agnostic, collisions).
- `library migrate` end-to-end on a fixture `~/.helixgen` (tmp_path + env
  overrides `HELIXGEN_LIBRARY`/`SETLISTS`/`PREFS`/`IRS`).
- Manifest v2 → v3 auto-migration (+ devices file extraction).
- Git plumbing: temp repos; git-absent path (PATH stub); gitignore contents.
- `generate` into the library (default + `-o` escape hatch + variant add).
- IR copy-in + metadata scaffold + mapping rewrite; `ir-backfill` idempotence.
- CLI list/show/describe/doc/validate; help-text parity per
  `tests/test_cli_parity.py`.

## 10. Sequencing and release

Three core PRs, each from a fresh `github/main` worktree with adversarial
review:

1. Library home + git plumbing + manifest v3 / `devices/` split.
2. Tone metadata + naming + `generate` flow + `library`
   import/migrate/describe/doc/validate.
3. Guitar profiles (+ preferences migration) + IR metadata + backfill.

One core release (**0.21.0**) after PR 3, then the plugin companion PR bumps
its pin and ships the skill changes. Backlog items #22/#35/#36 close when the
plugin PR lands; deferred items above get numbered backlog entries.
