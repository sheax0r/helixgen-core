# Venue Variants (engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tone variant records its venue (`headphones` / `direct` / `amp-in`), and one engine transform converts a preset to a venue's end-of-chain form from `generate`, `library fork`, and a new in-place `library venue` verb.

**Architecture:** A new `helixgen/venue.py` holds the vocabulary, room-IR selection, level maths, and the transforms. Transforms run at the **recipe level**: `view()` the `.hsp` to a recipe dict, edit the block list (insert split / room IR / join, or drop cabs), regenerate with `compose_preset`, and carry the original `meta` across. A no-op round-trip check guards against presets `view` cannot represent losslessly. Metadata, naming and the CLI learn a `venue` dimension; `mutate.add_block` / `remove_block` are relaxed just enough to keep working on a room-treated path.

**Tech Stack:** Python, click, pytest (`python3 -m pytest`), libsndfile via the existing `helixgen.ir._load_libsndfile` for reading IR wavs.

**Spec:** `helixgen` plugin repo, `docs/superpowers/specs/2026-11-11-venue-variants-design.md`. Read it first; this plan argues from it. Companion plan (skill, docs sync, library migration): plugin repo `docs/plans/2026-11-12-venue-variants-plugin.md`.

## Global Constraints

- Work in a git worktree branched from freshly fetched `origin/main` (repo CLAUDE.md). Never commit on local `main`.
- Tone metadata `schema` stays `1`. `venue` and `venue_detail` are optional and advisory, like `normalized`.
- Venue vocabulary is a closed set: `headphones`, `direct`, `amp-in`. Absent/`null` = unspecified, treated as `direct` as a transform source.
- Existing variant keys, filenames and display names never change. In-place conversion never renames.
- A transform never overwrites an existing target variant and never best-effort-edits a chain that does not match its `venue_detail`: it errors.
- Room IR is addressed by **hash**, never basename (same basename exists in several phase folders).
- Merge master `Level` is written as `0.0` (device default is +3 dB).
- The room footswitch is a **param toggle** on the room IR `Level` (`min -60.0`), never a bypass toggle.
- USB-tap split and outputs survive every transform.
- Default `room_offset_db` = `4.0`. Tile defaults: `Decay 0.2`, `Mix 0.2`. Amp-in EQ: `Parametric Mono`, `LowCut 90`, `LowCutSlope 2`, `HighCut 5500`, `HighCutSlope 2`.
- Every new flag/verb gets `--help` text that is its behavioural contract, and `tests/test_cli_parity.py` + `docs/CLI.md` are updated in the same task.

## Review Focus

1. **Tone with no cab at all** (DI bass, acoustic sim; 29 of the user's 126 presets have no IR) converted to `headphones`: expected is *no chain change*, a warning, `venue` recorded with `venue_detail.treatment = "none"`. Test in Task 4.
2. **Cab bypassed in some snapshot, or two cabs** (layered-amp presets): expected is the Tile fallback, never a split around one of two cabs. Test in Task 4.
3. **Room IR wav missing on disk** though its hash is registered (energy cannot be computed): expected is a clear error naming the file, nothing written. Test in Task 3.
4. **All ten assignable footswitches taken:** expected is the room stage is still added, with a warning and `room_switch: null`. Test in Task 4.
5. **Headphones variant hand-edited after conversion** (room IR swapped, Tile removed) then converted to `direct`: expected is an error naming the mismatch, file untouched. Test in Task 5.

---

### Task 1: `default_venue` preference

**Files:**
- Modify: `src/helixgen/preferences.py` (dataclass near line 505; loader near 729; env overrides near 747; defaults near 790)
- Create: `src/helixgen/venue.py`
- Test: `tests/test_preferences.py` (extend), `tests/test_venue.py` (create)

**Interfaces:**
- Produces: `venue.VENUES: tuple[str, ...] = ("headphones", "direct", "amp-in")`; `venue.validate_venue(value: str | None) -> str | None` (raises `ValueError` listing the valid set); `Preferences.default_venue: str | None`.

- [ ] **Step 1: Failing tests**

```python
def test_validate_venue_accepts_vocabulary_and_none():
    assert venue.validate_venue("headphones") == "headphones"
    assert venue.validate_venue(None) is None
    with pytest.raises(ValueError, match="amp-in"):
        venue.validate_venue("stadium")

def test_default_venue_from_file_and_env(tmp_prefs, monkeypatch):
    tmp_prefs({"default_venue": "headphones"})
    assert load_preferences().default_venue == "headphones"
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "direct")
    assert load_preferences().default_venue == "direct"

def test_default_venue_absent_is_none_and_bad_value_errors(tmp_prefs):
    tmp_prefs({})
    assert load_preferences().default_venue is None
    tmp_prefs({"default_venue": "bogus"})
    with pytest.raises(PreferencesError):
        load_preferences()
```

(Use whatever fixture `tests/test_preferences.py` already uses in place of `tmp_prefs`; mirror the `default_guitar` tests.)

- [ ] **Step 2:** Run `python3 -m pytest tests/test_venue.py tests/test_preferences.py -q`; expect failures on the new names.
- [ ] **Step 3:** Implement, mirroring `default_guitar` / `_validate_default_guitar` exactly, including the documented-defaults block.
- [ ] **Step 4:** Re-run; expect pass.
- [ ] **Step 5:** Commit `feat(prefs): default_venue preference + venue vocabulary`.

### Task 2: Venue in tone metadata and naming

**Files:**
- Modify: `src/helixgen/tone_meta.py` (`Variant` ~line 75, `_variant_to_dict`/`_variant_from_dict` ~191–222, `upsert_variant` ~457, `validate_tone_meta` ~558, `guitar_settings_warnings` ~615)
- Modify: `src/helixgen/naming.py` (`display_name`, `variant_slug`)
- Modify: `src/helixgen/cli_library.py` (`fork_cmd` source-key → guitar resolution; `doc_cmd --variant` lookup)
- Test: `tests/test_tone_meta.py`, `tests/test_naming.py` (extend)

**Interfaces:**
- Produces:
  - `Variant.venue: Optional[str] = None`, `Variant.venue_detail: Optional[Dict[str, Any]] = None` (round-tripped like `normalized`).
  - `naming.VENUE_SUFFIX = {"headphones": "HP", "direct": "Direct", "amp-in": "Amp In"}`.
  - `naming.display_name(..., venue_suffix: str | None = None)` → appends `" (HP)"` etc. when given.
  - `naming.variant_slug(logical, guitar_slug, venue: str | None = None)` → appends `-<venue>` when given.
  - `tone_meta.split_variant_key(key: str) -> tuple[str, str | None]` → `("ibanez-prestige", "amp-in")` for `"ibanez-prestige@amp-in"`, `(key, None)` otherwise.
  - `tone_meta.variant_key_for(meta: ToneMeta | None, guitar_slug: str | None, venue: str | None) -> tuple[str, bool]` → `(key, suffixed)`. Rule: bare `<guitar>` (or `"generic"`) if no variant on that guitar exists yet; else `<guitar>@<venue>` and `suffixed=True`. `venue=None` with the bare key taken keeps today's behaviour (upsert replaces).
  - `upsert_variant(..., venue: str | None = None, venue_detail: dict | None = None)` uses `variant_key_for`; when `suffixed`, passes `venue_suffix`/`venue` to the naming functions.

- [ ] **Step 1: Failing tests**

```python
def test_first_variant_keeps_bare_key_whatever_its_venue():
    meta = upsert_variant(None, descriptor="Probe", guitar_slug="ibanez-prestige",
                          guitar_short="Ibanez Prestige", hsp_path=P1, venue="headphones")
    v = meta.variants["ibanez-prestige"]
    assert v.venue == "headphones" and v.preset_name == "Probe - Ibanez Prestige"

def test_second_venue_on_same_guitar_gets_suffixed_key_name_and_file():
    meta = upsert_variant(meta1, descriptor="Probe", guitar_slug="ibanez-prestige",
                          guitar_short="Ibanez Prestige", hsp_path=P2, venue="amp-in")
    v = meta.variants["ibanez-prestige@amp-in"]
    assert v.preset_name == "Probe - Ibanez Prestige (Amp In)"
    assert naming.variant_slug("probe", "ibanez-prestige", "amp-in") == "probe-ibanez-prestige-amp-in"

def test_validate_accepts_at_venue_keys_and_rejects_unknown_venue():
    assert validate_tone_meta(meta_with("ibanez-prestige@amp-in"), ...) == []
    assert any("venue" in p for p in validate_tone_meta(meta_with("ibanez-prestige@moon"), ...))
    # venue field outside vocabulary is also a problem
    # key suffix and Variant.venue disagreeing is a problem

def test_venue_fields_round_trip_and_legacy_file_loads_with_none():
    ...  # save → load preserves venue + venue_detail; a dict without them loads as None

def test_split_variant_key():
    assert split_variant_key("ibanez-prestige@amp-in") == ("ibanez-prestige", "amp-in")
    assert split_variant_key("generic") == ("generic", None)
```

- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement. In `validate_tone_meta`, `guitar_settings_warnings`, `fork_cmd` and `doc_cmd`, replace every "variant key is a guitar slug" assumption with `split_variant_key(key)[0]`.
- [ ] **Step 4:** Run the whole metadata/library suite: `python3 -m pytest tests/test_tone_meta.py tests/test_naming.py tests/test_cli_library.py tests/test_cli_library_fork.py -q`; expect pass with no existing test changed.
- [ ] **Step 5:** Commit `feat(meta): venue + venue_detail on variants, venue-aware keys and names`.

### Task 3: Room IR selection and blend level

**Files:**
- Modify: `src/helixgen/venue.py`
- Test: `tests/test_venue.py`

**Interfaces:**
- Consumes: `helixgen.ir.IrMapping` (hash → wav path), `helixgen.ir._load_libsndfile`.
- Produces:
  - `venue.VenueError(ValueError)`.
  - `venue.ir_energy_db(wav: Path) -> float` → `10*log10(sum(x**2))` over channel 0. Missing/unreadable file raises `VenueError` naming the path.
  - `venue.find_room_ir(close_wav: Path, irs: IrMapping, override: str | None = None) -> tuple[str, Path] | None` → `(hash, path)`.
  - `venue.room_level_db(close_level: float, close_wav: Path, room_wav: Path, offset_db: float = 4.0) -> float` → `close_level - (E_room - E_close) - offset_db`, rounded to 0.1.

Selection rule (the spec's "same pack/cab prefix", made exact): a registered IR is a candidate when its basename contains `" ROOM"` and the text before `" ROOM"` followed by a space is a prefix of the close IR's basename (`"YA MRSH 412 T75"` matches `"YA MRSH 412 T75 Mix 12.wav"`). Among candidates keep those whose path contains `natural-phase` (case-insensitive, `-`/space agnostic) if any exist; then pick the highest `ir_energy_db`; ties → lowest basename. `override` is a hash or basename; a basename matching several hashes is a `VenueError` listing them.

- [ ] **Step 1: Failing tests** (build tiny wavs with the helper other IR tests use; name them like the real packs)

```python
def test_find_room_ir_matches_prefix_prefers_natural_phase_then_energy(irs_fixture):
    h, p = venue.find_room_ir(close("YA MRSH 412 T75 Mix 12.wav"), irs_fixture)
    assert p.name == "YA MRSH 412 T75 ROOM 2.wav" and "natural-phase" in str(p)

def test_find_room_ir_none_when_pack_has_no_room(irs_fixture):
    assert venue.find_room_ir(close("YA DXVB 112 Mix 01.wav"), irs_fixture) is None

def test_override_by_ambiguous_basename_errors(irs_fixture):
    with pytest.raises(venue.VenueError, match="hash"):
        venue.find_room_ir(close(...), irs_fixture, override="YA MRSH 412 T75 ROOM 2.wav")

def test_room_level_reproduces_reference():
    # energies 7.3 (close) and 15.2 (room), close Level -18, offset 4
    assert venue.room_level_db(-18.0, CLOSE_7_3, ROOM_15_2) == pytest.approx(-29.9, abs=0.1)

def test_missing_room_wav_is_a_clear_error(irs_fixture):
    with pytest.raises(venue.VenueError, match="ROOM 2.wav"):
        venue.ir_energy_db(Path("/nope/YA MRSH 412 T75 ROOM 2.wav"))
```

- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `feat(venue): room IR selection and energy-matched blend level`.

### Task 4: `headphones` transform

**Files:**
- Modify: `src/helixgen/venue.py`
- Test: `tests/test_venue.py`

**Interfaces:**
- Consumes: Task 3 functions; `helixgen.view.view(body, library, irs=...)`; `helixgen.spec.parse_spec`; `helixgen.generate.compose_preset`; `generate._is_amp` / `_is_cab`.
- Produces:
  - `@dataclass VenueResult: body: dict; venue: str; venue_detail: dict; warnings: list[str]`.
  - `venue.apply_venue(body: dict, target: str, library: Library, irs: IrMapping, *, source_venue: str | None = None, source_detail: dict | None = None, room_ir: str | None = None, room_mode: str = "auto", room_offset_db: float = 4.0) -> VenueResult`. `room_mode` is `"auto"` or `"tile"`. Dispatches on `target`; this task implements `headphones`, raising `VenueError` for the others until Task 5.
  - `venue._roundtrip(body, library, irs, edit: Callable[[dict], None]) -> dict`: `recipe = view(body)`; strip USB-tap structural entries and remember that taps were present; `edit(recipe)`; `compose_preset`; re-apply taps with the same code path the `usb-taps` verb uses; copy `body["meta"]` across unchanged. **Guard:** first run it with a no-op `edit` and require `view(result) == view(body)`; if not, raise `VenueError("preset cannot be transformed losslessly: <first differing key>")`. Also refuse when `view` reports `unknown_controllers`.

`venue_detail` for headphones (exact keys): `{"treatment": "room-ir" | "tile" | "none", "room_ir": <hash>|None, "room_level_db": float|None, "room_switch": "FS4"|None, "ambience_block": "Tile"|None, "ambience_switch": "FS5"|None, "fallback_reason": str|None}`.

Algorithm on the instrument path (the path containing an amp block; presets with none use path 0):

1. Collect cab-category blocks downstream of the last amp. Zero cabs → `treatment "none"`, no edit, warning.
2. `room-ir` requires: exactly one such cab, enabled at base and not in any snapshot's `disable`, it is an `IR` block with a resolvable wav, no `split`/`join` entry in the recipe (USB taps were already stripped), `find_room_ir` returns a match, and `room_mode == "auto"`. Otherwise → `tile` fallback with `fallback_reason` set.
3. `room-ir` edit: wrap the close IR as `{"split": {"type": "y"}}`, close IR, `{"block": "IR", "lane": 1, "ir": <hash>, "params": {"Level": <room_level_db>}}`, `{"join": {"params": {"A Level": 0.0, "B Level": 0.0, "A Pan": 0.5, "B Pan": 0.5, "Level": 0.0}}}`. Close `Level` is the IR block's base value, default `-18.0` when absent.
4. Append `{"block": "Tile", "params": {"Decay": 0.2, "Mix": 0.2}}` at the end of the path: `enabled: false` for `room-ir`, enabled for `tile`.
5. Footswitches: free switches are `FS1..FS5, FS7..FS11` not already in `recipe["footswitches"]`, in that order. `room-ir`: first free → `{"switch", "block": "IR", "lane": 1, "param": "Level", "min": -60.0, "max": <level>, "label": "ROOM MIC", "color": "purple"}`; next free → Tile bypass, label `TILE`, color `turquoise`. `tile`: one switch for Tile. None free → skip, warn, record `null`.
6. If `compose_preset` rejects the path for lane capacity: retry without Tile (warn); if it still fails, raise `VenueError`.

- [ ] **Step 1: Failing tests** (author fixtures via the CLI `generate` like `tests/test_cli_library_fork.py::seeded`; add a registered close + room IR pair)

```python
def test_headphones_adds_parallel_room_ir_and_disabled_tile(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    blocks = view(r.body, lib, irs=irs)["paths"][0]["blocks"]
    kinds = [("split" in b) or ("join" in b) or b.get("block") for b in blocks]
    # split, close IR lane 0, room IR lane 1 at computed level, join Level 0.0, Tile disabled last
    assert r.venue_detail["treatment"] == "room-ir"
    assert r.venue_detail["room_level_db"] == pytest.approx(expected_level, abs=0.1)

def test_room_switch_is_a_level_param_toggle_not_a_bypass(...):
    fs = [f for f in view(...)["footswitches"] if f.get("label") == "ROOM MIC"][0]
    assert fs["param"] == "Level" and fs["min"] == -60.0 and fs["lane"] == 1

def test_everything_else_is_unchanged(...):
    # blocks/params/snapshots/footswitches/expression that existed before are equal after

def test_usb_taps_survive(tapped_body, ...):
    # the USB output structural entries present before are present after

def test_no_cab_means_no_treatment(di_body, ...):            # Review Focus 1
    assert r.venue_detail["treatment"] == "none" and view(r.body) == view(di_body) and r.warnings

def test_two_cabs_or_snapshot_bypassed_cab_fall_back_to_tile(...):   # Review Focus 2
    assert r.venue_detail["treatment"] == "tile" and "fallback_reason" in r.venue_detail

def test_stock_cab_and_no_matching_room_ir_fall_back_to_tile(...)
def test_room_mode_tile_forces_fallback(...)
def test_no_free_footswitch_still_adds_room_and_warns(full_fs_body, ...):   # Review Focus 4
    assert r.venue_detail["room_switch"] is None and r.venue_detail["treatment"] == "room-ir"
def test_per_snapshot_values_survive(...)   # a param with snapshot overrides reads the same per snapshot after
def test_lossy_preset_is_refused(body_with_unknown_controller, ...):
    with pytest.raises(venue.VenueError, match="losslessly"): ...
```

- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `feat(venue): headphones transform (parallel room IR, Tile fallback)`.

### Task 5: `direct` reversal and `amp-in`

**Files:**
- Modify: `src/helixgen/venue.py`
- Test: `tests/test_venue.py`

**Interfaces:**
- Consumes/produces: `apply_venue` from Task 4, now complete for all targets.

Rules:
- `→ direct` from `headphones`: requires `source_detail`. Remove exactly what it records: the lane-1 IR whose hash equals `room_ir`, its split and join, the `ambience_block`, and the footswitch entries on `room_switch` / `ambience_switch`. Before editing, verify each recorded item is present as recorded (hash, block name, switch target); any mismatch → `VenueError` naming it. `treatment "none"` → no edit. `venue_detail` result: `{}`.
- `→ direct` from `amp-in`: `VenueError("cannot restore a cab from an amp-in variant; fork from the direct or headphones variant")`.
- `→ amp-in`: if `source_venue == "headphones"`, first apply the `direct` reversal. Then remove every cab-category block and insert `Parametric Mono` (constants in Global Constraints) immediately after the last amp. No amp → `VenueError`. `venue_detail`: `{"eq_block": "Parametric Mono"}`.
- `→ headphones` from `amp-in`: same error as `→ direct`.
- Same target as `source_venue` → `VenueError("already <venue>")`.

- [ ] **Step 1: Failing tests**

```python
def test_headphones_then_direct_restores_the_original_chain(...):
    a = venue.apply_venue(body, "headphones", lib, irs)
    b = venue.apply_venue(a.body, "direct", lib, irs, source_venue="headphones", source_detail=a.venue_detail)
    assert view(b.body) == view(body)          # blocks, params, order; not grid positions

def test_direct_without_detail_errors(...)
def test_direct_after_hand_edit_errors_and_names_mismatch(...):   # Review Focus 5
    # swap the room IR hash, or remove Tile, then expect VenueError matching "room_ir" / "Tile"
def test_amp_in_removes_cabs_and_adds_eq_after_last_amp(...):
    # no cab-category blocks remain; Parametric Mono follows the amp with LowCut 90 / HighCut 5500
def test_amp_in_from_headphones_also_drops_room_stage(...)
def test_from_amp_in_to_anything_errors(...)
def test_same_venue_errors(...)
```

- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `feat(venue): direct reversal and amp-in transform`.

### Task 6: CLI surface

**Files:**
- Modify: `src/helixgen/cli.py` (`generate_cmd` ~187; `describe`)
- Modify: `src/helixgen/cli_library.py` (`fork_cmd` ~1088; new `venue_cmd`; `show`)
- Modify: `docs/CLI.md`, `tests/test_cli_parity.py`
- Test: `tests/test_cli_venue.py` (create), `tests/test_cli_library_fork.py` (extend)

**Interfaces:**
- Consumes: `venue.apply_venue`, `upsert_variant(..., venue=, venue_detail=)`, `Preferences.default_venue`.
- Produces (user-facing):
  - `helixgen generate <recipe> ... [--venue V] [--room-ir X] [--room tile]`. Library flow only. No `--venue` → `default_venue`; neither → no transform and `venue` left `None`. `--venue direct` records `direct` with no transform. With legacy `-o`, `--venue` is a usage error and `default_venue` is ignored.
  - `helixgen library fork <source> --venue V [--guitar G] [--room-ir X] [--room tile] [--dry-run] [--json]`. Byte-copy as today, then `apply_venue` with the source variant's `venue`/`venue_detail`. `forked_from` gains `"venue_from": <source venue or null>`. `--venue` alone (same guitar, same identity) is now a valid fork; target key from `variant_key_for`. Existing target → error, nothing written.
  - `helixgen library venue <variant> V [--room-ir X] [--room tile] [--dry-run] [--json]`. In place: rewrites the `.hsp`, sets `venue`/`venue_detail`, leaves key, filename, `preset_name`, `forked_from` alone. `<variant>` resolves like `library show`; an ambiguous multi-variant slug errors listing preset names.
  - All three print `apply_venue` warnings on stderr and a one-line summary (`venue: headphones (room-ir, YA MRSH 412 T75 ROOM 2 @ -30.0 dB, FS4)`).
  - `describe` / `library show` print a `Venue` column in the variants table; `--json` already carries the fields.

- [ ] **Step 1: Failing tests** (CliRunner, `$HELIXGEN_HOME` redirected by conftest)

```python
def test_generate_uses_default_venue_and_records_it(...)       # prefs default_venue=headphones
def test_generate_venue_direct_opts_out(...)                   # no split in view; venue == "direct"
def test_generate_without_pref_or_flag_is_unchanged(...)       # byte-identical .hsp to pre-feature output; venue None
def test_generate_dash_o_with_venue_is_usage_error(...)
def test_fork_venue_same_guitar_makes_suffixed_variant(...):
    # key "ibanez-prestige@amp-in", file "...-ibanez-prestige-amp-in.hsp", name ends " (Amp In)",
    # forked_from["venue_from"] is None
def test_fork_venue_onto_existing_target_errors_and_writes_nothing(...)
def test_library_venue_in_place_keeps_key_file_and_name(...)
def test_library_venue_same_venue_errors(...)
def test_dry_run_writes_nothing(...)
def test_describe_shows_venue_per_variant(...)
```

- [ ] **Step 2–4:** Run (fail), implement, run (pass). Then `python3 -m pytest tests/test_cli_parity.py -q` and fix parity by documenting the new surface in `docs/CLI.md`.
- [ ] **Step 5:** Commit `feat(cli): --venue on generate and library fork; library venue verb`.

### Task 7: `add-block` / `remove-block` on a room-treated path

**Files:**
- Modify: `src/helixgen/mutate.py` (`_is_parallel_routed` ~647, `add_block` ~818, `remove_block` ~906; read the module docstring on `branch`/`endpoint` pointers first)
- Test: `tests/test_mutate.py` (extend)

**Interfaces:**
- Produces: `add_block` and `remove_block` succeed on a path that has a split/join when the edit is on lane 0 strictly before the split or strictly after the join, rewriting the split/join `branch`/`endpoint` `bNN` pointers (and the USB-tap split's) for every key they shift. Edits between split and join, or on lane 1, still raise `MutateError` with today's message.

- [ ] **Step 1: Failing tests**

```python
def test_add_block_after_join_on_room_treated_path(headphones_body, lib):
    key = add_block(headphones_body, "Simple Delay Mono", lib, after="Plate")
    v = view(headphones_body, lib)      # split/room IR/join intact, new block present, taps intact
def test_add_block_before_split_shifts_pointers_correctly(...):
    # after adding a drive before the amp, generate-equivalent recipe (same blocks built fresh) views identically
def test_remove_block_outside_split(...)
def test_edit_inside_split_or_on_lane_1_still_refused(...)
def test_resulting_body_copies_to_device_format(...)   # reuse the transcode check tests/test_device_copy.py uses
```

- [ ] **Step 2–4:** Run (fail), implement, run (pass). Run `python3 -m pytest tests/test_mutate.py tests/test_cli_patch.py -q`.
- [ ] **Step 5:** Commit `feat(mutate): add/remove blocks outside the split on parallel-routed paths`.

### Task 8: Full suite, docs, release

- [ ] **Step 1:** `python3 -m pytest -q -n auto`; expect all green.
- [ ] **Step 2:** Update `docs/recipe-reference.md` (venue is not a recipe field; say so in one paragraph and point at `generate --venue`) and `docs/CLI.md` if anything is still missing.
- [ ] **Step 3:** Manual check against the real library (read-only on the library: use `--dry-run`): `helixgen library venue "Metallica - Master of Puppets - Ibanez Prestige" headphones --dry-run` prints `room-ir` with a `YA MRSH 412 T75 ROOM` IR.
- [ ] **Step 4:** Version bump and release per this repo's release process; open the PR; adversarial review subagent before merge (repo CLAUDE.md).
