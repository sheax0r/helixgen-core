# Structural review + refactor plan — backlog #28

**Date:** 2026-07-15
**Scope:** `src/helixgen/` (+ `src/helixgen/device/`) and `mcp_server/`.
**Goal:** a structured review of the whole codebase for structure, readability,
and maintainability (module boundaries, duplicated mapping logic per the
resolver pattern #14, `cli.py` size, dead code), followed by behavior-preserving,
test-pinned refactors. Also absorbs the three #14 resolver residuals #51/#52/#53.

This doc is Phase 1 (findings) + Phase 2 (sequenced plan). Phase 3 (execution of
the safe subset) is recorded inline as each item ships. The full suite
(`PYTHONPATH=$PWD/src python -m pytest -q`, **1659 passed / 11 skipped**
baseline) plus the 211-export decompile-acceptance net gate every step.

---

## Phase 1 — Findings

### F0. Module map + sizes (survey baseline)

`src/helixgen` is 18.5 KLOC across 40 modules. The heavy modules:

| Module | LOC | Notes |
|---|---|---|
| `cli.py` | **2800** | one file; ~78% is the `device` verb group (see F1) |
| `device/transcode.py` | 1597 | `.hsp`→`_sbepgsm`; cohesive, single-purpose |
| `mutate.py` | 1342 | in-place `.hsp` edit verbs |
| `device/client.py` | 1333 | OSC-over-ZeroMQ client + `_RawOps` |
| `spec.py` | 1125 | recipe parser/validator |
| `view.py` | 1009 | `.hsp`→recipe projection |
| `generate.py` | 875 | `.hsp` builders + legacy `.hlx` |
| `device/hss.py` | 789 | `.hss` framing |
| `mcp_server/tools.py` | 1591 | MCP tool bodies |
| `mcp_server/server.py` | 1054 | MCP tool registration |

Test suite: **82 files, 22 KLOC**, well-partitioned one-file-per-surface.

### F1. `cli.py` is oversized and cleanly bisectable **[highest value]**

`cli.py` is 2800 lines. Lines **1–584** are the *core* CLI (ingest, generate,
view, the surgical edit verbs, list/show-block, controllers, bootstrap, the IR
verbs, `ir-cache`) — ~585 lines. Lines **585–2800** are a single self-contained
section headed `# --- device: network control of a Line 6 Helix Stadium ---`:
10 module-level device helpers (`_device_option`, `_setlist_container`,
`_auto_upload_irs`, `_utc_now`, `_tone_by_cid`, `_record_placement`,
`_slot_from_posi`, `_ledger_rename`, `_ledger_remove`, `_install_hsp_open`)
followed by the `device` click group and its sub-groups (`settings`, `globaleq`,
`setlist`, `slots`) and ~65 verbs.

**The seam is clean, verified both directions:**
- No core command (< 585) references any device-section helper.
- No device command references any core helper (`_resolved_library`,
  `_run_mutation`, `_coerce_cli_value`, `_auto_register_tone`, …).
- The device section's only dependencies on `cli.py`'s top-level imports are
  `json`, `Path`, `click`, `read_hsp`, `IrHashCache` — all trivially importable.
- External references to `helixgen.cli.*`: tests import `cli` (the group) and
  `_auto_upload_irs`; `@patch` targets only `helixgen.cli.bootstrap` (core).
  So a move must **re-export `_auto_upload_irs`** from `cli.py`.

→ Extract the device section verbatim into `src/helixgen/cli_device.py`
(defining `device` as `@click.group(...)`), and in `cli.py` do
`from helixgen.cli_device import device, _auto_upload_irs` +
`cli.add_command(device)`. Pure move; click registration unchanged;
`helixgen.cli:cli` entry point unchanged. **Plan step S6.**

### F2. Resolver residuals #51/#52/#53 (from the #14 audit)

Confirmed all three against the code; each is a real semantic-difference
reconciliation (why #14 filed rather than forced them). Resolutions in Phase 2
(steps S3–S5). Summary of the divergences:

- **#51** `client.slot_label` (canonical, uncapped, `""`-for-None formula) vs
  `manifest._posi_to_slot` (independent 512-entry `_SLOT_LABELS` table, 128-bank
  cap, `None`-for-OOR). The reverse (label→posi) already single-sources off
  `manifest._SLOT_LABELS`. Neither module imports the other and no cycle exists
  (client's deps — osc/content/settings/globaleq/defs/irmd — never import
  manifest), so the table can be **derived from** `client.slot_label`.
- **#52** `reorder.py`'s literal-cid branch re-implements the casefold
  name-match that `resolve_setlist_cid` owns, because it needs the *full* set of
  name-matches plus a separate cid-membership test — and it reuses **one**
  `list_setlists(strict=True)` fetch for both, so a naive route-through would
  double the RPC.
- **#53** `client._hex_hash` (string branch: lowercase, no length check) vs
  `sftp._addcontent_hash` (string branch: exact `len==32`, case-preserving).
  Bytes branches already consolidated onto `irmd.irmd_to_irhash` (#14). The
  string branch is **defensive** — device hashes arrive as 16 raw msgpack bytes,
  never as strings — so reconciling it is behavior-preserving in practice.

### F3. Dead code **[safe, verified]**

Six module-level symbols + one unused import, each verified tree-wide (src,
mcp_server, tests, .claude, docs) to have **zero references** beyond its own def:

| Symbol | Location | Note |
|---|---|---|
| `_utc_now` | `cli.py:653` | device helper, no callers |
| `_assemble_flow` | `device/transcode.py:652` | docstring claims "kept for callers/tests" — none exist; live path is `_canonical_flow`/`synthesize_sfg` |
| `color_name` | `device/maintenance.py:78` | inverse of the used `color_index`; this direction unused |
| `SetlistManifest._read_json` | `device/manifest.py:184` | staticmethod, never called |
| `SetlistManifest.delete_tone` | `device/manifest.py:432` | public but unreferenced (used path is `remove_from_setlist`) |
| `HelixSFTP.list_ir_files` | `device/sftp.py:138` | EXPERIMENTAL sftp path; sibling `download_ir`/`ir_file_exists` used, this isn't |
| `ParamValidationError` (import) | `mutate.py:29` | imported from `generate`, only appears again in a docstring; every real consumer imports it from `generate` directly |

`delete_tone`/`list_ir_files` are *public* methods — removed here because
git history preserves them and no in-repo (CLI/MCP/test/skill) surface uses
them. **Plan step S1.** No commented-out code or scaffolding found.

### F4. Duplication not covered by #14 (lower value, mostly defer)

- **65 repeated lazy `from helixgen.device import HelixClient, HelixError, …`**
  inside cli device verbs. They are lazy on purpose (isolate the optional
  `device` extra's import-failure surface to device commands). Consolidating to
  a module-level import in the extracted `cli_device.py` would move an
  ImportError from command-time to CLI-startup — an observable change — so this
  is **deferred** (S7, not executed).
- `hss.slot_label` is an unrelated function sharing a name with
  `client.slot_label` — a readability trap noted by #51. A rename touches
  `hss.py` + `test_hss.py`; low value, **deferred** (S8).

### F5. Oversized functions (readability, defer — rewrites are not behavior-preserving-cheap)

Longest bodies: `transcode._new_midi_ctrl` (190 lines), `mutate.wire_footswitch`
(146), `transcode._hrns_for` (131), `transcode.synthesize_sfg` (127),
`generate._to_hsp_bnn` (122), `cli.device_setlist_import_hss` (111). These are
mostly flat table/branch construction, not tangled logic; decomposition is a
judgment rewrite with real regression risk. **Deferred** (S9) — left as a
numbered plan item, not executed this pass.

### F6. `mcp_server` (out of primary scope, note only)

`tools.py` (1591) + `server.py` (1054) mirror the CLI surface. The tool bodies
duplicate result-shaping already available in the engines; a consolidation pass
belongs in its own review. **Deferred** (S10).

---

## Phase 2 — Sequenced plan (lowest-risk first)

Each step is behavior-preserving and independently test-pinned; the full suite +
211-export acceptance net run after every commit.

| Step | Item | Risk | Test pin | This session |
|---|---|---|---|---|
| **S1** | Delete F3 dead code (6 symbols + 1 import) | very low | full suite green (nothing referenced them) | **EXECUTE** |
| **S3** | #52 `list_setlists_by_name` seam | low | new `test_list_setlists_by_name`; existing reorder/resolve tests | **EXECUTE** |
| **S4** | #53 unify IR-hash string normalizer | low (defensive path) | existing `test_hex_hash_*`/`test_addcontent_hash_*` + new len-reject test | **EXECUTE** |
| **S5** | #51 single-source posi→slot formula | low | existing `test_slot_label`/`test_manifest_v2` slot asserts + new derivation test | **EXECUTE** |
| **S6** | Extract `cli_device.py` (pure move + re-export) | medium (large move) | full device CLI test files + `helixgen --help` / `device --help` smoke | **EXECUTE** |
| S7 | Consolidate 65 lazy device imports | medium (moves ImportError surface) | — | defer |
| S8 | Rename `hss.slot_label` → `hss_slot_label` | low | test_hss | defer |
| S9 | Decompose oversized functions (F5) | medium–high (rewrites) | per-function golden | defer |
| S10 | `mcp_server` tools/result-shape consolidation | medium | MCP tool tests | defer |

Deferred steps S7–S10 need **no user input** and are safely executable in a
follow-up session; filed as a single backlog residual entry.

### #51/#52/#53 — deliberate reconciliations (which behavior wins)

- **#51:** `client.slot_label` is declared **the single source** of the forward
  formula. `manifest._SLOT_LABELS` is rebuilt as
  `tuple(slot_label(i) for i in range(_SLOT_BANKS*4))` (byte-identical to the old
  comprehension), and `_posi_to_slot` keeps its capped/`None` contract unchanged.
  *Winner:* the formula lives once (client); both callers' contracts (uncapped
  `""`-for-None; capped `None`-for-OOR) are preserved exactly.
- **#52:** new `HelixClient.list_setlists_by_name(name, *, strict, setlists=None)`
  owns the casefold match. `resolve_setlist_cid` returns the first match's cid;
  the reorder branch passes its single pre-fetched listing as `setlists=` (no
  extra RPC) for the clash set and keeps the full listing for the cid-membership
  test. *Winner:* `resolve_setlist_cid`'s `strip().casefold()`-both-sides
  semantics — the reorder clash check gains stored-name stripping (a
  whitespace-in-setlist-name edge case, strictly more consistent).
- **#53:** new `irmd.normalize_hash_string(s)` = `s.lower() if len(s)==32 else
  None`. Both string branches route through it. *Winner:* length-validation
  (from `_addcontent_hash`) **and** lowercasing (from `_hex_hash`) — the safer
  union. Observable only on the defensive string path, which device traffic
  never exercises (hashes arrive as 16 raw bytes).

---

## Phase 3 — execution record (2026-07-15)

Executed the safe subset, one commit per step, full suite + 211-export
acceptance net green after every commit:

| Step | Commit topic | Result | Suite |
|---|---|---|---|
| S1 | remove 6 dead symbols + 1 unused import | done | 1659 pass |
| S3 | #52 `list_setlists_by_name` | done | 1662 pass (+3) |
| S4 | #53 `irmd.normalize_hash_string` | done | 1664 pass (+2) |
| S5 | #51 derive `_SLOT_LABELS` from `client.slot_label` | done | 1665 pass (+1) |
| S6 | extract `cli_device.py` (pure move) | done — `cli.py` 2792→649; command tree byte-identical | 1665 pass |

**Deferred → backlog #54:** S7 (fold 65 lazy device imports — moves the
optional-extra ImportError surface, needs a lazy accessor), S8 (rename
`hss.slot_label`), S9 (decompose F5 oversized functions), S10 (`mcp_server`
result-shape consolidation). All behavior-preserving, no user input needed.
