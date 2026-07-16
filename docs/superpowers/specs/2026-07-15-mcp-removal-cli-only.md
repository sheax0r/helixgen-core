# MCP server removal — the CLI is the only engine surface (0.20.0)

Design + inventory record for backlog #63 (mirrored in the coordination
workspace's authoritative backlog). Shipped 2026-07-15.

## Rationale

helixgen carried **three** agent-facing surfaces describing the same engine:
the CLI (`--help` text), the MCP tool descriptions in `mcp_server/`, and the
docs (`docs/CLI.md`, `CLAUDE.md`). Every behavior change had to land on all
three in the same PR ("agent-facing surfaces ship in sync"), and drift between
them was a recurring bug class. The MCP server added no capability the CLI
lacked — every tool was a thin wrapper over the same library calls — while
costing a `mcp` dependency, a FastMCP wiring layer, a parallel test tree, and
the tri-surface sync burden.

**Decision:** delete the MCP server. The CLI is the single engine surface.
Agents start at `helixgen --help` (which orients: verb groups + mental
models) and read per-verb `--help` as the behavioral contract — exactly the
role MCP tool descriptions used to play. Verbs whose output agents consume
programmatically expose `--json`.

Breaking change → **0.20.0** (the `[mcp]` extra and the `mcp_server` package
are gone from the wheel). Consumers pinned to released versions (e.g. the
plugin repo's `helixgen[mcp,device]==0.19.1`) are unaffected; the plugin must
drop its `.mcp.json` + `[mcp]` extra when it bumps to >=0.20.0 (tracked as a
cross-repo residual under backlog #63/#58).

## Inventory: every removed MCP tool → CLI verb

All 50 tools. "already" = the CLI verb predates this change; "NEW" = added
here; "flag" = a `--json`/help gap closed here. Parity is pinned by
`tests/test_cli_parity.py` (verb existence via click introspection + key
contract phrases in help + `--json` shape checks).

| MCP tool | CLI verb | Resolution |
|---|---|---|
| `list_blocks` | `helixgen list-blocks` | already; NEW `--json` |
| `show_block` | `helixgen show-block` | already; NEW `--json`; help gained the case-sensitivity contract |
| `generate_preset` | `helixgen generate` | already; help gained recipe shape, `Unknown param(s)` recovery, IR reminder |
| `list_irs` | `helixgen list-irs` | already; NEW `--json`; help gained empty→stock-cab guidance |
| `compute_irhash` | `helixgen irhash <wav>` | **NEW verb** (stateless hash; 48 kHz/left-channel/upload-reminder contract in help) |
| `discover_irs` | `helixgen irhash <dir>` | **NEW verb** (directory walk; per-file failures warn + continue) |
| `register_ir` | `helixgen register-irs <wav>` | already |
| `register_irs` | `helixgen ir-scan <dir>` | already (richer: cache validity, `--rescan`, `--remove`) |
| `view_preset` | `helixgen view` | already; stdout is JSON by default (no flag needed) |
| `controller_mapping` | `helixgen controllers --json` | already |
| `patch_preset` | `helixgen patch <hsp> <ops.json\|->` | **NEW verb** — see "the patch decision" below |
| `device_list_presets` | `device list` | already (`--json`) |
| `device_list_setlists` | `device setlists` | already (`--json`) |
| `device_read_preset` | `device read` | already (`--json`) |
| `device_load_preset` | `device load` | already |
| `device_create_preset` | `device create` | already |
| `device_rename_preset` | `device rename` | already |
| `device_delete_preset` | `device delete` | already |
| `device_set_param` | `device set-param` | already; help gained the RAW-units + coordinates contract |
| `device_info` | `device info` | already (`--json`) |
| `device_settings_list` | `device settings list` | already (`--json`, `--values`, `--page`) |
| `device_settings_get` | `device settings get` | already (`--json`) |
| `device_settings_set` | `device settings set` | already |
| `device_globaleq_list` | `device globaleq list` | already (`--json`) |
| `device_globaleq_set` | `device globaleq set` | already |
| `device_tuner` | `device tuner` | already (`--json` = one reading/line) |
| `device_snapshot` | `device snapshot` | already |
| `device_blocks` | `device blocks` | already (`--json`) |
| `device_bypass` | `device bypass` | already |
| `device_model` | `device model` | already |
| `device_save_preset` | `device save` | already |
| `device_install_preset` | `device install` | already; help now pushes `--auto-irs` (the MCP default was auto_irs=True; the CLI flag stays opt-in but the silent-cab consequence is spelled out) |
| `device_import_hss` | `device setlist import-hss` | already (`--list`, `--dry-run`; NOT-idempotent warning in help) |
| `device_export_hss` | `device setlist export-hss` | already |
| `device_setlist_list` | `device setlist list` | already (`--json`) |
| `device_setlist_add` | `device setlist add` | already (multi-membership semantics in help) |
| `device_setlist_remove` | `device setlist remove` | already; help gained the implicit-mark semantics |
| `device_sync_setlist` | `device sync <setlist>` | already (`--json`, `--repush`, `--exclude-irs`) |
| `device_sync_all` | `device sync --all [--gc]` | already |
| `device_delete_ir` | `device delete-ir` | already (`--force-wedge` contract in help) |
| `device_rename_ir` | `device rename-ir` | already |
| `device_ir_prune` | `device ir-prune` | already (`--json`; dry-run default, two consents) |
| `device_set_info` | `device set-info` | already (batch CIDs, color palette, non-activating notes) |
| `device_setlist_create` | `device setlist create` | already |
| `device_setlist_rename` | `device setlist rename` | already |
| `device_setlist_delete` | `device setlist delete` | already (never-orphan in help) |
| `device_setlist_duplicate` | `device setlist duplicate` | already (references-shared-not-copied in help) |
| `device_reorder` | `device reorder` | already (cid-first + device-side-vs-manifest in help) |
| `device_meters` | `device meters` | already (`--json` = one reading/line) |
| `device_measure` | `device measure` | already (`--json`; gain_db level-matching contract in help) |

Dropped without replacement: the `model` soft-gate parameter every MCP tool
took (`"stadium"`/`"stadium_xl"`). It was an agent-honesty check, not a
behavioral input (every handler treated both values identically); the `setup`
skill remains the real device-confirmation gate. Also dropped: the MCP-only
stderr→`warnings[]` capture in `generate_preset` — the CLI's stderr IS the
warning channel, which agents running a subprocess can read directly.

## The `patch` decision (batch vs sequential single-op verbs)

`patch_preset` applied a LIST of ops atomically. We added a batch verb —
`helixgen patch <preset.hsp> <ops.json|->` — rather than documenting
sequential `set-param`/`enable`/... calls as the equivalent, because:

1. **Atomicity is a real contract, not a convenience.** The MCP tool applied
   all ops in memory and wrote once; an invalid op left the file untouched.
   A sequence of single-op verbs that fails at op N leaves a half-patched
   preset on disk — a worse failure mode for an agent that then has to
   reason about partial state. `helixgen patch` preserves the
   apply-all-then-write-once semantics (engine:
   `helixgen.mutate.apply_operations`).
2. **Agent ergonomics.** One subprocess spawn per edit session instead of
   one per knob; ops arrive as JSON (stdin supported), which is the shape
   agents already produce; existing `patch_preset` call sites in skills port
   1:1.
3. **The single-op verbs stay** for humans and for one-off tweaks — `patch`
   reuses their exact engine functions, so the two paths cannot diverge.

## Help-as-contract convention

- **Top-level `helixgen --help`** orients an agent: verb groups, then the
  mental models (show-block before writing params / case-sensitivity; .hsp
  is the source of truth, no sidecar; device write-vs-read split + ACTIVE-
  tone live ops; flaky network → re-run, idempotent; --json availability),
  then pointers to `docs/CLI.md` / `docs/recipe-reference.md`.
- **`helixgen device --help`** carries the device-specific models: the full
  read-vs-write verb split, the tone-library/pool/reference model, and the
  flaky-network rule.
- **Per-verb `--help` must suffice to use the verb correctly without reading
  source.** Agent-critical gotchas (RAW units, cid-first parsing, dry-run
  defaults, NOT-idempotent retry warnings, silent-cab consequences, consent
  flags) live in the verb's help; exhaustive narrative (protocol history,
  HW-validation notes) stays in `docs/CLI.md`, referenced as SEE ALSO.
- **The regression guard** is `tests/test_cli_parity.py`: a table of
  (removed tool → verb path → key contract phrases) asserted against the raw
  click help strings (whitespace-normalized), plus top-level/device-group
  orientation phrase checks. Rewording help requires updating the table in
  the same commit — that's the point.

## `--json` conventions

- `--json` means: stdout is a single machine-readable JSON document
  (warnings/diagnostics stay on stderr). Exceptions: the streaming verbs
  (`device tuner`/`device meters` emit one JSON reading per line — they are
  live streams), and `view`, which prints JSON by default.
- Shapes reuse the existing output-shaping code paths; new ones added here:
  `list-blocks --json` = array of `{display_name, model_id, category}`;
  `show-block --json` = `{display_name, model_id, category, aliases,
  params}`; `list-irs --json` = array of `{hash, path}`; `irhash --json` =
  array of `{hash, path, basename}`; `patch --json` = `{path, warnings}`.
- Every agent-consumed verb's `--json` validity + top-level keys are pinned
  in `tests/test_cli_parity.py` (offline verbs run for real; networked verbs
  run against a canned fake client).

## What was removed

- `mcp_server/` (server.py, tools.py, __main__.py, data/) and
  `tests/mcp_server/` (5 modules).
- The `[mcp]` extra and `mcp_server*` packaging from `pyproject.toml`
  (packages.find now `src`-only).
- Two stray `mcp_server`-importing tests in core test modules
  (`test_cli_device_measure.py`, `test_device_settings.py`).
- Accidentally-committed stale build artifacts (`build/`,
  `helixgen.egg-info/` — both embedded the 0.19.1 mcp_server tree); now
  gitignored.
- MCP mentions across living docs (`CLAUDE.md`, `docs/CLI.md` incl. its
  stale not-on-PyPI install section, `README.md`, `docs/BACKLOG.md`
  preamble, `docs/stadium-app-parity.md` MCP columns,
  `docs/recipe-reference.md`, `docs/ir-hash-algorithm.md`,
  `docs/helix-protocol.md`, `docs/SPECULATIVE-BACKLOG.md`,
  `docs/features/controller-identifier-english-mapping.md`) and stale
  MCP references in src/tests comments. Dated design/review docs under
  `docs/superpowers/` keep their historical MCP references — they are
  records of past decisions, not living surfaces.
