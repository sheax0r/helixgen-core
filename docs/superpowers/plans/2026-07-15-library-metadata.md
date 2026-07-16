# Library Metadata Implementation Plan (backlog #22/#35/#36)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the approved library-metadata design (`docs/superpowers/specs/2026-07-15-library-metadata-design.md`): an artifact library at `~/.helixgen/library/` with per-entity JSON metadata (tones, guitar profiles, IRs), one git repo at `~/.helixgen`, intent/observed manifest split, new naming schema, migration — released as helixgen 0.21.0 plus a plugin companion PR.

**Architecture:** Three sequential core PRs. PR 1 lays foundations (home paths, git plumbing, manifest v3 + per-device observations). PR 2 builds tone metadata + naming + the `library` verb group + `generate` integration + migration. PR 3 adds guitar profiles and IR metadata. Each new concern is its own module (`home`, `gitops`, `naming`, `tone_meta`, `guitars`, `ir_meta`, `cli_library`, `device/observations`); `cli.py` stays thin.

**Tech Stack:** Python stdlib + `click` only (hard repo rule — no numpy, no GitPython; git via `subprocess`). pytest + tmp_path/monkeypatch for tests.

## Global Constraints

- **Read the spec first**: `docs/superpowers/specs/2026-07-15-library-metadata-design.md` is the authoritative design. Read `CLAUDE.md` too.
- **Pure stdlib + click** for runtime deps. Git operations shell out to `git` and degrade gracefully when it's absent.
- **TDD**: failing test first, minimal implementation, then commit. Run targeted tests per step; the full suite (`PYTHONPATH=$PWD/src python -m pytest`) must be green before each PR is opened, plus the 211-export acceptance net (`tests/test_decompile_acceptance.py`) and CLI parity (`tests/test_cli_parity.py`).
- **Help is the agent contract**: every new/changed verb ships agent-grade `--help` text in the same commit, and `tests/test_cli_parity.py` is updated to pin it.
- **Docs ship in sync per PR**: any CLI-visible change updates `CLAUDE.md` + `docs/CLI.md` (and `docs/recipe-reference.md` if recipe-visible) in the same PR.
- **Never commit paid IR WAVs** anywhere; the library `.gitignore` must exclude `library/irs/**/*.wav`.
- **Env vars**: existing overrides keep working (`$HELIXGEN_LIBRARY`, `$HELIXGEN_IRS`, `$HELIXGEN_SETLISTS`, `$HELIXGEN_PREFS`, `$HELIXGEN_CACHE`). New: `$HELIXGEN_HOME` (default `~/.helixgen`) anchors the git repo, `devices/`, and the new defaults for setlists (`$HELIXGEN_HOME/setlists/manifest.json`) and IRs (`$HELIXGEN_HOME/library/irs`).
- **All tests isolate the home**: every test touching these features sets `HELIXGEN_HOME` (and per-area vars as needed) to tmp_path via monkeypatch — never the real `~/.helixgen`.
- **Schema constants**: all three metadata kinds use `"schema": 1`. Exactly one of `song`/`descriptor` is set on a tone. Variant key = guitar profile slug or `"generic"`.
- **Auto-commit is advisory**: gated by `git_commit_tones` preference (default `"auto"`); failures warn to stderr, never fail the operation.
- **Dates**: metadata `created`/`updated` are ISO dates from `datetime.date.today().isoformat()`.
- PR mechanics: worktree branched from freshly-fetched `github/main` (remote is `github`), adversarial review subagent before merge, merge via `gh pr merge --squash --delete-branch`. Merging + releasing are user-preapproved for this work.

---

## PR 1 — foundations: home, git plumbing, manifest v3 + observations

Branch: reuse `worktree-library-metadata-design` (already carries the spec commit).

### Task 1: `home.py` — canonical paths

**Files:**
- Create: `src/helixgen/home.py`
- Test: `tests/test_home.py`

**Interfaces:**
- Produces:
  - `helixgen_home() -> Path` — `$HELIXGEN_HOME` or `~/.helixgen`
  - `library_dir() -> Path` — `$HELIXGEN_LIBRARY` or `helixgen_home()/"library"`
  - `tones_dir() -> Path` — `library_dir()/"tones"`
  - `guitars_dir() -> Path` — `library_dir()/"guitars"`
  - `library_irs_dir() -> Path` — `$HELIXGEN_IRS` or `library_dir()/"irs"` (NEW default; old default was `~/.helixgen/irs`)
  - `manifest_path() -> Path` — `$HELIXGEN_SETLISTS` or `helixgen_home()/"setlists"/"manifest.json"` (NEW default; old was `~/.helixgen/setlists.json`)
  - `legacy_manifest_path() -> Path` — `helixgen_home()/"setlists.json"` (for migration fallback)
  - `legacy_irs_dir() -> Path` — `helixgen_home()/"irs"`
  - `devices_dir() -> Path` — `helixgen_home()/"devices"`
- None of these create directories (pure path resolution); callers mkdir.

- [ ] **Step 1: Write failing tests** — `tests/test_home.py`:

```python
import helixgen.home as home

def test_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HELIXGEN_HOME", raising=False)
    assert home.helixgen_home() == Path.home() / ".helixgen"

def test_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    assert home.helixgen_home() == tmp_path
    assert home.library_dir() == tmp_path / "library"
    assert home.manifest_path() == tmp_path / "setlists" / "manifest.json"
    assert home.library_irs_dir() == tmp_path / "library" / "irs"
    assert home.devices_dir() == tmp_path / "devices"

def test_area_env_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.setenv("HELIXGEN_LIBRARY", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "m.json"))
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path / "myirs"))
    assert home.library_dir() == tmp_path / "elsewhere"
    assert home.manifest_path() == tmp_path / "m.json"
    assert home.library_irs_dir() == tmp_path / "myirs"
```

- [ ] **Step 2:** Run `PYTHONPATH=$PWD/src python -m pytest tests/test_home.py -v` — expect FAIL (module missing).
- [ ] **Step 3:** Implement `src/helixgen/home.py` (each function ~3 lines: read env var, else default). Find every existing hard-coded resolution of these paths (`grep -rn "setlists.json\|\.helixgen" src/`) and route them through `home.py` (library.py, ir.py, manifest.py, bootstrap.py, cli.py keep their own env-var names but delegate defaults here). **Do not change the manifest default location yet** — that flips in Task 3 (migration); for now `manifest_path()` exists but `SetlistManifest` still loads the legacy path if the new one is absent.
- [ ] **Step 4:** Run tests → PASS; run full suite → green (path routing is behavior-preserving).
- [ ] **Step 5:** Commit `feat: home.py canonical path resolution (HELIXGEN_HOME)`.

### Task 2: `gitops.py` — repo init + advisory auto-commit

**Files:**
- Create: `src/helixgen/gitops.py`
- Test: `tests/test_gitops.py`

**Interfaces:**
- Produces:
  - `GITIGNORE = "devices/\ncache/\ntone3000/\n*.bak*\nlibrary/irs/**/*.wav\n"`
  - `git_available() -> bool` (shutil.which)
  - `ensure_home_repo(home: Path) -> bool` — True iff home is (now) a git repo. If git missing → False. If `home/.git` exists or `git -C home rev-parse --is-inside-work-tree` succeeds → True (never re-init, never touch an existing .gitignore). Else: `git init`, write `.gitignore`, `git add -A && git commit -m "helixgen: initialize library"`. All subprocess failures → warn to stderr, return False.
  - `auto_commit(home: Path, message: str) -> None` — no-op unless `git_commit_tones` pref allows (import `load_preferences` lazily; `"auto"`/`True` → commit, `False` → skip) AND home is a repo. Runs `git -C home add -A` then `commit -m message`; "nothing to commit" is silent success; other failures warn, never raise.

- [ ] **Step 1: Failing tests** (skip whole module if `shutil.which("git") is None`):

```python
def test_ensure_creates_repo_with_gitignore(tmp_path):
    assert gitops.ensure_home_repo(tmp_path) is True
    assert (tmp_path / ".git").is_dir()
    text = (tmp_path / ".gitignore").read_text()
    assert "library/irs/**/*.wav" in text and "devices/" in text

def test_ensure_idempotent_preserves_gitignore(tmp_path):
    gitops.ensure_home_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("custom\n")
    gitops.ensure_home_repo(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "custom\n"

def test_git_missing_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert gitops.ensure_home_repo(tmp_path) is False

def test_auto_commit_commits_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_PREFS", str(tmp_path / "prefs.json"))  # defaults: auto
    gitops.ensure_home_repo(tmp_path)
    (tmp_path / "f.txt").write_text("x")
    gitops.auto_commit(tmp_path, "test: change")
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "test: change" in log

def test_auto_commit_respects_pref_false(tmp_path, monkeypatch): ...  # write prefs git_commit_tones=false; assert no new commit
def test_auto_commit_never_raises(tmp_path): gitops.auto_commit(tmp_path / "norepo", "x")  # no error
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. Set `GIT_AUTHOR_NAME/EMAIL`+committer env fallbacks (`helixgen`/`helixgen@localhost`) on the commit calls so init works on machines without git identity. **Step 4:** Run → PASS. **Step 5:** Commit `feat: gitops — home repo init + advisory auto-commit`.

### Task 3: Manifest v3 + `device/observations.py`

**Files:**
- Modify: `src/helixgen/device/manifest.py` (v2→v3), `src/helixgen/device/setlist_sync.py`, `src/helixgen/cli_device.py` (observed-state writers/readers)
- Create: `src/helixgen/device/observations.py`
- Test: `tests/test_manifest_v3.py`, `tests/test_observations.py`; update existing `tests/test_manifest*.py` expectations

**Interfaces:**
- Produces (`observations.py`):
  - `@dataclass DeviceObservations: serial: str; tones: dict[str, dict]` (name → `{"cid": int, "posi": int}`) `; pool: dict; setlists: dict`
  - `load_observations(serial: str) -> DeviceObservations` (missing file → empty)
  - `save_observations(obs: DeviceObservations) -> None` (atomic write to `devices_dir()/f"{serial}.json"`, mkdir as needed)
  - `lookup_tone(name: str) -> dict | None` — search all `devices/*.json`, newest mtime first (replaces old per-tone `device` reads, e.g. the #25 slot-restore fallback)
- Manifest v3 (`MANIFEST_VERSION = 3`): per-tone record is `{path, content_hash, source, slot, auto_marked?}` — **no `doc`, no `device`**; document has **no `observed` section**. `register_tone` loses its `doc` kwarg (delete the `register --doc` CLI flag in the same commit).
- v2→v3 auto-migration on load: write `<file>.bak-v2` copy; strip `doc`/`device`/`observed`; old per-tone `device` + `observed` data is preserved into `devices/legacy.json` (serial `"legacy"`); if loaded from `legacy_manifest_path()`, save to the NEW `manifest_path()` location and leave the legacy file in place renamed `setlists.json.migrated-v2`.
- Sync/CLI writers: wherever sync recorded `observed`/`device` into the manifest (`record_observed_pool` and friends), it now builds a `DeviceObservations` for the connected device's serial (from the client's `/ProductInfoGet` info — reuse the `device info` plumbing; fall back to `f"ip-{ip}"` if the query fails) and `save_observations()`. Readers (`slots restore` fallback, `slots list --verify`, anything touching `tone["device"]`) switch to `observations.lookup_tone`.

- [ ] **Step 1: Failing tests.** Key cases:

```python
def test_v2_migrates_to_v3_and_new_location(tmp_home):
    legacy = tmp_home / "setlists.json"          # write a real v2 doc with doc/device/observed
    legacy.write_text(json.dumps(V2_DOC))
    m = SetlistManifest.load()
    assert m.version == 3
    saved = json.loads((tmp_home / "setlists" / "manifest.json").read_text())
    assert "observed" not in saved
    assert "device" not in saved["tones"]["ToneA"] and "doc" not in saved["tones"]["ToneA"]
    legacy_obs = json.loads((tmp_home / "devices" / "legacy.json").read_text())
    assert legacy_obs["tones"]["ToneA"] == {"cid": 1085, "posi": 0}
    assert (tmp_home / "setlists.json.migrated-v2").exists()

def test_v3_round_trip(tmp_home): ...            # save/load preserves intent fields exactly
def test_observations_save_load_lookup(tmp_home): ...
def test_lookup_prefers_newest_file(tmp_home): ...
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement manifest v3 + observations + rewire sync/CLI callers (grep `"observed"`, `record_observed`, `["device"]`, `.doc` across `src/`). Update every existing manifest test that asserted v2 shapes. **Step 4:** Full suite → green. **Step 5:** Commit `feat!: manifest v3 — intent only; per-device observations in devices/<serial>.json`.

### Task 4: Bootstrap wiring + PR 1 closeout

**Files:**
- Modify: `src/helixgen/bootstrap.py` (or wherever first-write init lives), `CLAUDE.md`, `docs/CLI.md`
- Test: `tests/test_home_repo_bootstrap.py`

- [ ] **Step 1:** Failing test: any manifest/library write path (e.g. `SetlistManifest.save()`, library ingest) on a fresh `HELIXGEN_HOME` triggers `ensure_home_repo` exactly once and the home becomes a repo (when git present). Verify no repo is created when `git_commit_tones=false`? — No: repo init is unconditional (git present ⇒ init); only *commits* are pref-gated. Assert that.
- [ ] **Step 2-4:** Implement a small `helixgen.libinit.ensure_initialized()` hook called from manifest save + ingest + (later) metadata saves; PASS; full suite + parity + acceptance green.
- [ ] **Step 5:** Update `CLAUDE.md` (library/home layout section, manifest location, devices/) + `docs/CLI.md` (affected verb notes). Commit `docs: home layout, manifest v3, devices/ observations`.
- [ ] **Step 6:** Push branch, `gh pr create --title "Library foundations: HELIXGEN_HOME, git plumbing, manifest v3 + per-device observations (#22/#35/#36 PR 1)" --body <summary + spec link>`.
- [ ] **Step 7:** Dispatch an adversarial review subagent (prompt: break the migration — data loss, path regressions, git edge cases). Fix confirmed findings or defer to backlog with entry numbers.
- [ ] **Step 8:** Merge (`gh pr merge --squash --delete-branch`) once suite green + review resolved.

---

## PR 2 — tone metadata, naming, generate, library verbs, migration

Branch: new worktree from freshly-fetched `github/main` (e.g. `library-metadata-pr2`).

### Task 5: `naming.py`

**Files:** Create `src/helixgen/naming.py`; Test `tests/test_naming.py`

**Interfaces:**
- `slugify(text: str) -> str` — lowercase; spaces/underscores/em-dashes → `-`; strip other punctuation; collapse repeats; strip leading/trailing `-`.
- `display_name(*, artist=None, song=None, descriptor=None, guitar_short=None) -> str` — `"Artist - Song - Guitar"` / `"Descriptor - Guitar"` / guitar omitted when `guitar_short is None`. Raises `ValueError` unless exactly one of (artist+song) | descriptor is provided (artist requires song and vice versa).
- `logical_slug(*, artist=None, song=None, descriptor=None) -> str`; `variant_slug(logical: str, guitar_slug: str | None) -> str` (`guitar_slug=None` → logical slug unchanged).

- [ ] Steps: failing tests (schema cases above + `slugify("Foo Fighters — White Limo!") == "foo-fighters-white-limo"`) → implement → PASS → commit `feat: naming schema + slugs`.

### Task 6: `tone_meta.py`

**Files:** Create `src/helixgen/tone_meta.py`; Test `tests/test_tone_meta.py`

**Interfaces:**
- `@dataclass Variant: hsp: str; preset_name: str; guitar_settings: dict[str, str] = field(default_factory=dict); notes_md: str | None = None`
- `@dataclass ToneMeta: artist: str | None; song: str | None; descriptor: str | None; tags: list[str]; description_md: str | None; variants: dict[str, Variant]; created: str; updated: str; schema: int = 1`
  - `logical_slug` property (via naming.py); `display_base` property (`"Artist - Song"` / descriptor)
- `meta_path(slug) -> Path` (= `tones_dir()/f"{slug}.json"`); `load_tone_meta(slug)`, `load_all_tone_metas()`, `save_tone_meta(meta)` (atomic, bumps `updated`, `ensure_initialized()` + `auto_commit`)
- `upsert_variant(meta_or_none, *, artist, song, descriptor, guitar_slug, guitar_short, hsp_path, tags) -> ToneMeta` — creates the ToneMeta if absent, adds/replaces the variant (key `guitar_slug or "generic"`), computes `preset_name` via `naming.display_name`.
- `validate_tone_meta(meta, *, tones_dir, manifest, guitar_slugs) -> list[str]` — returns problem strings: exactly-one-of song/descriptor; variant hsp exists on disk; variant key in `guitar_slugs | {"generic"}`; `preset_name` registered in manifest; unknown-control warnings are produced in Task 12 (guitar profiles) — here just shape checks.

- [ ] Steps: failing tests (round-trip, upsert-creates, upsert-replaces, validate catches missing hsp / both-song-and-descriptor / unknown variant key) → implement → PASS → commit `feat: per-tone JSON metadata (description folded in, variants)`.

### Task 7: `generate` into the library

**Files:** Modify `src/helixgen/cli.py` (the `generate` command + `_auto_register_tone` area, cli.py:150-200); Test `tests/test_generate_library.py`

**Interfaces:**
- `generate RECIPE [-o OUT] [--artist A --song S | --descriptor D] [--guitar GUITAR]`:
  - `-o` given → exactly today's behavior (write there, auto-register, **no metadata**).
  - No `-o` → resolve naming inputs: flags win; else recipe `name` becomes `descriptor`; `--guitar` resolves a guitar profile by slug/name/short_name **once Task 11 lands — in this PR accept the literal string and slugify it** (plan note: PR 3 tightens resolution to profiles; keep a `_resolve_guitar(label) -> (slug, short)` seam function so PR 3 swaps the internals only).
  - Write `.hsp` to `tones_dir()/<variant_slug>.hsp` with `meta.name = preset_name` (error if file exists), `upsert_variant`, auto-register (existing `_auto_register_tone`), `auto_commit`.
  - stdout: path written + preset name + logical slug.
- `--help` text updated (agent contract) + `tests/test_cli_parity.py` pin.

- [ ] Steps: failing CLI tests via `click.testing.CliRunner` with `HELIXGEN_HOME` isolated (cases: default write creates .hsp+json+manifest entry+commit exists when git present; `-o` path unchanged & writes no json; second `--guitar` adds a variant to the same logical json; both `--song` and `--descriptor` → error) → implement → PASS → full suite → commit `feat: generate writes into the library with naming flags`.

### Task 8: `cli_library.py` — list/show/describe/doc/validate

**Files:** Create `src/helixgen/cli_library.py` (pattern: `cli_device.py` — a `library` click group imported by `cli.py` via `cli.add_command(library)`; plus top-level `describe`); Test `tests/test_cli_library.py`

**Interfaces:**
- `library list [--tones|--guitars|--irs] [--json]` — default lists everything grouped; `--json` emits `{"tones": [...], "guitars": [...], "irs": [...]}` (guitars/irs sections empty lists until PR 3 — the flags exist now so help text is stable).
- `library show <name> [--json]` — resolve by logical slug, preset name, or metadata filename; human or raw-JSON dump.
- `describe <tone>` (top-level) — summary header (artist/song/descriptor, tags, variants table w/ guitar + preset name + guitar_settings) then `description_md` verbatim.
- `library doc <tone> [--variant <guitar-slug>] (--from-file <md> | -)` — set `description_md` (no `--variant`) or that variant's `notes_md`; `-` reads stdin; bumps `updated`; auto-commits.
- `library validate [--json]` — run `validate_tone_meta` across all metas (+ PR 3 extends); exit 1 if problems; `--json` emits `{"problems": [...]}`.

- [ ] Steps: failing CliRunner tests per verb (including `doc -` stdin, `validate` exit codes) → implement → PASS → update parity test → commit `feat: library verb group (list/show/doc/validate) + describe`.

### Task 9: `library import` + `library migrate`

**Files:** Modify `src/helixgen/cli_library.py`; create `src/helixgen/migrate.py`; Test `tests/test_library_import.py`, `tests/test_library_migrate.py`

**Interfaces (`migrate.py`, so logic is testable without the CLI):**
- `plan_migration() -> dict` — inspect manifest + prefs and emit the editable plan: `{"tones": [{"name": old, "path": ..., "artist": null, "song": null, "descriptor": <old name>, "guitar": null, "new_name": ..., "new_slug": ...}], "instruments": [...], "irs": [{"hash":..., "wav":...}]}`. Inference: split old names on `" - "`/`" — "`; a trailing segment matching an instrument name/short form → guitar; two leading segments → artist/song; else descriptor = whole old name. Never guesses beyond that.
- `run_migration(plan: dict, *, dry_run: bool=False) -> dict` — idempotent execution: `ensure_home_repo`; move each tone's `.hsp` into `tones_dir()` under `new_slug`, rewrite `meta.name` to `new_name` (via existing hsp load/save helpers in `mutate`/`hsp`), fold a sibling `.md` (same stem) into `description_md`, build ToneMeta, update manifest entry (key = new_name, path = new location, keep slot/source/hash-recompute); IR part: copy each `mapping.json` WAV into `library_irs_dir()/<source-dir-name-slug>/`, scaffold minimal IrMeta JSON (PR 3 enriches; in this PR write `{"schema":1,"irhash":...,"wav":...,"imported_from":...}` via a tiny helper that PR 3's `ir_meta.py` supersedes — put it in `migrate.py._scaffold_ir_stub`), rewrite mapping to library-relative paths; instruments → PR 3 (plan records them; `run_migration` calls a `migrate_instruments(plan)` hook that this PR implements as a no-op returning "deferred to PR 3" — PR 3 fills it); final `auto_commit("helixgen: library migration")`. Returns a summary dict of moves/renames/skips/errors. Re-running on a migrated home = all skips.
- CLI: `library migrate [--dry-run] [--plan FILE]` — `--dry-run` prints `plan_migration()` JSON to stdout (agent edits it, passes back via `--plan`); no flag → plan+run in one go.
- CLI: `library import <file.hsp|dir> [--artist/--song/--descriptor/--guitar] [--keep-source]` — same per-tone pipeline as migration for external files (move; `--keep-source` copies), sibling `.md` folded, registered, committed.

- [ ] Steps: failing tests — build a fake v2-era home in tmp_path (2 tones + docs + registered IR + prefs with instruments), assert: plan inference (one name `"Song Title - Satriani"` maps how?  → descriptor fallback since "Satriani" isn't an instrument; a name ending `" — Les Paul Jr"` maps guitar), run moves files + folds md + updates manifest + rewrites mapping + is idempotent; import moves vs `--keep-source` copies → implement → PASS → parity + docs (`CLAUDE.md` naming section swap, `docs/CLI.md` library group section) → commit(s).

### Task 10: PR 2 closeout

- [ ] Full suite + acceptance + parity green. Docs synced (CLAUDE.md: naming convention section replaced with new schema + library flow; docs/CLI.md: `library` group + `generate` changes; `docs/recipe-reference.md` untouched — recipe shape didn't change).
- [ ] Push, `gh pr create` (title `Tone metadata, naming schema, library verbs + migration (#22/#35/#36 PR 2)`), adversarial review (prompt: break migration idempotence, naming collisions, generate regressions incl. `-o` path), fix/defer, merge.

---

## PR 3 — guitar profiles + IR metadata + release

Branch: new worktree from freshly-fetched `github/main` (e.g. `library-metadata-pr3`).

### Task 11: `guitars.py` + preferences deprecation

**Files:** Create `src/helixgen/guitars.py`; Modify `src/helixgen/preferences.py`, `src/helixgen/migrate.py` (fill `migrate_instruments`), `src/helixgen/cli.py` (`generate` `_resolve_guitar` seam); Test `tests/test_guitars.py`

**Interfaces:**
- `@dataclass Control: name: str; kind: str; positions: list[str] | None = None; notes: str | None = None` (kind ∈ knob|switch|push-pull|other)
- `@dataclass GuitarProfile: name: str; short_name: str; type: str; active: bool | None; pickups: str | None; construction: str | None; character_md: str | None; genres: list[str]; controls: list[Control]; schema: int = 1` — `slug` property = `slugify(name)`.
- `load_profile(slug)`, `load_all_profiles()`, `save_profile(p)` (atomic + auto_commit), `find_profile(label) -> GuitarProfile | None` (match slug, name, short_name — case-insensitive).
- `profile_from_instrument(d: dict) -> GuitarProfile` — seeds from a prefs `instruments` entry (`short_name` = last two words of name heuristic? NO — YAGNI: `short_name` = name; migration plan lets the agent/user set better short names via the editable plan's `instruments` entries `{"name":..., "short_name":...}`).
- `preferences.py`: `instruments` key **deprecated** — still parsed (back-compat) but `load_preferences` emits a one-line stderr warning pointing at `library migrate`; `preset_output_dir` same treatment. `default_guitar` doc updated: names a profile.
- `generate`'s `_resolve_guitar` now: `find_profile(label)` → `(slug, short_name)`; unknown label → error listing known profiles (`--guitar` must reference a profile once profiles exist; if `guitars_dir()` is empty, fall back to literal slugify with a warning).
- `validate_tone_meta` extension: unknown `guitar_settings` keys vs the profile's control names → **warnings list** (separate from errors; `library validate` prints both, exits 1 only on errors).
- `library list --guitars` / `library show <guitar>` wired to profiles.

- [ ] Steps: failing tests (round-trip, find by short_name, migration converts the 4-instrument fixture into 4 profile files + removes `instruments` from prefs file, generate resolves `--guitar "Les Paul Jr"`, settings-vs-controls warning) → implement → PASS → commit(s).

### Task 12: `ir_meta.py` + IR import-by-copy + backfill

**Files:** Create `src/helixgen/ir_meta.py`; Modify `src/helixgen/cli.py` (`register-irs`, `ir-scan`), `src/helixgen/migrate.py` (swap `_scaffold_ir_stub` → `ir_meta.scaffold`); Test `tests/test_ir_meta.py`

**Interfaces:**
- `CONTROLLED_TAGS: frozenset[str]` — exactly the catalog vocabulary: tone `bright dark warm neutral scooped mid-forward beefy tight boomy boxy fizzy smooth articulate aggressive airy full chime`; gain `clean edge-of-breakup crunch high-gain`; era `vintage modern`; use `classic-rock blues metal thrash garage fuzz indie lead rhythm stereo room`.
- `@dataclass IrMeta: irhash: str; wav: str; imported_from: str | None; pack: dict | None; cab: str | None; speaker: str | None; mics: list[str]; mix: str | None; tags: list[str]; measured: dict | None; notes_md: str | None; schema: int = 1`
- `meta_path_for(wav_in_library: Path) -> Path` (same path, `.json` suffix); `load_ir_meta(path)`, `save_ir_meta(m, path)` (atomic + auto_commit), `load_all_ir_metas()`.
- `import_wav(src: Path, irhash: str, *, pack: str | None = None) -> tuple[Path, Path]` — copy `src` to `library_irs_dir()/<pack or slugify(src.parent.name)>/<src.name>` (skip copy if identical file already there), scaffold+save IrMeta (`mix` guessed from a `Mix NN` filename pattern; everything else None/empty for skill enrichment), return (wav_path, meta_path).
- `register-irs` / `ir-scan`: after each successful hash, call `import_wav` and register the **library copy's** path in `mapping.json` (source path recorded in `imported_from`). `ir-scan` of an already-in-library file is a no-op. `--no-copy` escape hatch on both (registers in place, no metadata) for callers who explicitly don't want library ownership.
- `library ir-backfill` — for every mapping entry whose WAV is outside `library_irs_dir()` or lacks a metadata sidecar: `import_wav` + rewrite mapping; idempotent; prints a summary. `library list --irs` / `show` / `validate` wired (validate: hash present in mapping, wav exists, tags ⊆ CONTROLLED_TAGS as warnings).

- [ ] Steps: failing tests (import_wav copies + scaffolds + mix-guess, register-irs places library copy in mapping, backfill idempotent, validate flags off-vocabulary tag) → implement → PASS → commit(s). (No FFT in core — `measured` is filled by the skill; stdlib-only rule.)

### Task 13: PR 3 closeout + release 0.21.0

- [ ] Docs sync: `CLAUDE.md` (guitar profiles, IR metadata, preferences deprecations, full library layout), `docs/CLI.md` (all new/changed verbs), parity test green.
- [ ] Full suite + acceptance green. Push, PR (`Guitar profiles + IR metadata + library completion (#22/#35/#36 PR 3)`), adversarial review (prompt: break profile resolution, IR copy/mapping rewrites, prefs back-compat), fix/defer, merge.
- [ ] Release: fetch `github/main` fresh; confirm no concurrent version bump; bump `pyproject.toml` + `src/helixgen/__init__.py` to `0.21.0` (same commit, via a small PR or direct on a release branch → PR → merge); tag `v0.21.0` on the merge commit; `git push github v0.21.0`; watch the publish workflow (`gh run list --workflow publish.yml`) until green; verify `pip index versions helixgen` (or PyPI JSON) shows 0.21.0.

---

## Plugin companion PR (repo `sheax0r/helixgen`)

### Task 14: skills update

**Files (plugin repo):** `.claude/skills/tone/SKILL.md`, `.claude/skills/setup/SKILL.md`, `.claude/skills/device/SKILL.md`, synced `docs/` copies, `.claude-plugin/plugin.json` + `marketplace.json` (version bump → automated release).

- [ ] Update the pinned core version to `helixgen[device]==0.21.0`.
- [ ] tone skill: naming flags flow (`--artist/--song/--descriptor/--guitar`), `generate` without `-o`, descriptions via `library doc` (drop step 7a sidecar `.md`), guitar-profile-driven param adaptation (`library show <guitar> --json`), per-guitar variant offer (multi-guitar case only), drop its own git commits for library paths.
- [ ] setup skill: guitar profiles replace `instruments` editing (scaffold controls inventory via structured questions); offer `library migrate` to existing users; drop "no prefs CLI" note where superseded.
- [ ] device skill: path/observed-state doc updates only.
- [ ] IR enrichment: import/backfill → fill IrMeta provenance/tags per the `_catalog` procedure + controlled vocabulary (skill computes optional FFT `measured` itself — not core).
- [ ] Cross-reference the three core PRs in the plugin PR body and vice versa (comment on merged core PRs). Adversarial review, merge; version bump releases automatically.
- [ ] Live validation (device skill contract): a subagent exercises `generate` → `library` verbs → `device sync` against the real Helix (user-preapproved device writes; prefer expendable setlist).

### Task 15: backlog + workspace closeout

- [ ] `~/git/helix/BACKLOG.md`: mark #22/#35/#36 ✅ SHIPPED with dates + PR links; file numbered entries for the deliberate deferrals: per-device slot intent; field-level metadata setter verbs; name-based addressing on surgical edit verbs; skill-side FFT `measured` backfill for pre-existing IRs.
- [ ] Update `~/git/helix/helixgen-core/CLAUDE.md` already done in PRs; verify workspace CLAUDE.md needs nothing (paths unchanged there).

## Self-review notes (done at write time)

- Spec coverage: §2 homes/git → Tasks 1-4; §3 manifest → Task 3; §4 naming → Task 5; §5.1 → Task 6; §5.2 → Task 11; §5.3 → Task 12; §6 CLI → Tasks 7-9, 11-12; §7 skills → Task 14; §8 errors → embedded per task; §9 testing → per task; §10 sequencing/release → Tasks 10, 13.
- Known intentional deviations: none from the spec; PR 2's guitar resolution is a temporary literal-string seam tightened in PR 3 (spec is end-state, sequencing requires the seam).
- Type consistency: `Variant.hsp` is a library-relative string in JSON; `validate_tone_meta` resolves against `tones_dir()`. `find_profile` returns the profile; `_resolve_guitar` derives `(slug, short_name)`.
