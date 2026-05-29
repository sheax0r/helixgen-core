# IR registration — design

**Date:** 2026-05-28
**Status:** Approved (pending user review of this written spec)
**Source brief:** conversation 2026-05-28 (use user-imported IRs from `irs/` directory instead of stock cabs when appropriate)

## Goal

Let `helixgen` reference and generate presets that use user-imported impulse
responses (IRs). The Helix Stadium identifies user IRs in `.hsp` presets by an
opaque slot-level `irhash` (a 32-char hex value; appears to be MD5 over a
device-internal normalized representation we cannot replicate without HX Edit /
SDK help). Build a manual hash↔file mapping that helixgen reads and writes,
and teach the generator to emit `irhash` on IR slots.

Today helixgen:
- does not know about IR blocks at all beyond happening to ingest their model
  ID and params,
- strips `irhash` on generate (the field lives at slot level, alongside
  `model`, not inside `params`, so the schema misses it),
- offers no spec syntax for "use IR file X".

The user has imported 26 IRs (10 York Audio DXVB Mixes, 16 York Audio VX30
BLUE Mixes) and re-exported two registration presets that give us a complete
known-good (hash, wav) Rosetta stone for those 26 IRs.

## Non-goals (this feature)

- **Cracking the hash algorithm.** Tracked separately; until cracked, users
  must round-trip through a registration preset to learn new (hash, file)
  pairs.
- **`show-ir` command.** `mapping.json` is small enough that `cat
  $HELIXGEN_IRS/mapping.json` is sufficient.
- **Category / tag metadata.** Filenames in commercial IR packs encode the
  cab, mic, and position; the tone skill can parse those. Revisit if usage
  proves it's needed.
- **Auto-discovery / fuzzy match beyond exact basename.**
- **Migration / mapping rewrites if the device's IRs change.** Out of scope.
- **Devices other than Stadium / Stadium XL.** Legacy `.hlx` presets use a
  slot-number model, not a hash; their IR handling is its own problem and not
  addressed here.

## Storage layout

A new IRs directory, parallel in spirit to the existing block library:

| location       | default                  | override                              |
|----------------|--------------------------|---------------------------------------|
| IRs directory  | `~/.helixgen/irs/`       | `HELIXGEN_IRS` env var (absolute path)|
| Mapping file   | `<irs-dir>/mapping.json` | implicit (always relative to IRs dir) |

`mapping.json` shape — flat hash → path:

```json
{
  "ad8182e1ebe9fd95dffde5dd54b6d89c": "York Audio DXVB Verb Deluxe V1.01/YA DXVB 112/48k (Fractal, Line 6, Suhr, etc.)/Mixes/YA DXVB 112 Mix 01.wav",
  "830b491472c195b7f572c1157df01e05": "York Audio DXVB Verb Deluxe V1.01/YA DXVB 112/48k (Fractal, Line 6, Suhr, etc.)/Mixes/YA DXVB 112 Mix 02.wav"
}
```

- Keys are lowercase 32-char hex (the canonical `irhash`).
- Values are paths interpreted **relative to the IRs directory** unless absolute.
- The file is created on first `register-irs` call; missing file is treated as
  "no IRs registered yet."
- The user is expected to keep `irs/` and `mapping.json` in version control or
  not — that's their call. helixgen does not commit anything.
- For the user's helixgen repo, the convention is
  `export HELIXGEN_IRS=$PWD/irs` so the existing `./irs/` directory becomes
  the IRs directory.

## New CLI commands

### `helixgen register-irs`

```
helixgen register-irs <preset.hsp> <wav1> <wav2> ... <wavN>
```

- Reads `<preset.hsp>`, enumerates IR blocks (model `HX2_ImpulseResponseWithPan`
  and any future IR-model variants) in deterministic order: **path index
  ascending, then block position ascending**.
- For each IR block, reads its slot-level `irhash`.
- Pairs each hash with the next wav arg in order. The number of wav args MUST
  equal the number of IR blocks in the preset; otherwise exit non-zero with a
  helpful message ("preset has 10 IR blocks, got 7 wav args").
- Writes / updates `mapping.json`:
  - New hash → record the (hash, file) entry.
  - Existing hash → if `file` matches existing value, idempotent no-op (silent
    or one-line "already registered"). If `file` differs, error with
    "hash X is already mapped to Y; use --force to overwrite."
- `--force` overrides conflicts.
- Wav paths on the command line are converted to canonical form before
  storage: if a path is under the IRs directory, store it as relative;
  otherwise store as absolute.
- Each wav arg is validated for existence before any write; missing file →
  error with the offending path, no partial mapping update.
- If the IRs directory does not exist, `register-irs` creates it (and the
  enclosing parent for `~/.helixgen/irs/`). `mapping.json` is written
  atomically (tmp file + rename) to avoid corruption on interrupt.

### `helixgen list-irs`

```
helixgen list-irs
```

Prints, one IR per line: `<hash>  <relative-or-absolute-wav-path>`. No header
row (mirrors `list-blocks` style). Exit code 0 even when no IRs are registered
(prints nothing). Pipeable to `wc -l`, `grep`, etc.

## Spec sugar

A new optional block-level field `ir` on IR blocks:

```json
{
  "block": "With Pan",
  "ir": "YA DXVB 112 Mix 01.wav",
  "params": {"HighCut": 6500.0, "LowCut": 90.0, "Mix": 1.0, "Pan": 0.5}
}
```

| field   | type   | required | default | notes |
|---------|--------|----------|---------|-------|
| `block` | str    | yes      | —       | The IR-block display name (currently `"With Pan"` for `HX2_ImpulseResponseWithPan`). |
| `ir`    | str    | no       | —       | Wav basename (looked up in `mapping.json` values) OR a 32-char hex hash (looked up in keys). |
| `params`| obj    | no       | block defaults | `HighCut`, `LowCut`, `Mix`, `Level`, `Delay`, `Pan`, `Polarity` (per `show-block "With Pan"`). |

Resolution of the `ir` field:

1. If `ir` is exactly 32 hex chars, treat as a hash and look up in mapping keys.
   Error "unknown IR hash <hash>" if not found.
2. Otherwise treat as a wav basename. Scan mapping values; for each value V,
   compare `os.path.basename(V) == ir` (case-sensitive). If exactly one match,
   use that entry. If multiple, error with "ambiguous IR basename <ir>;
   matches: <list of full paths>". If none, error "no registered IR matches
   basename <ir>".
3. If `ir` is omitted entirely, fall through to the canonical ingested hash
   for the block (see Generator behavior).

The `ir` field is rejected on non-IR-category blocks with a clear error
("block <name> is not an IR block; remove the 'ir' field or change the block").

## Generator behavior

The existing irhash-strip-on-generate bug is fixed as part of this work — the
two are coupled.

- **Ingest** (`ingest.py`): when extracting an IR block (category `cab`,
  model starts with `HX2_ImpulseResponse`), additionally record the observed
  `irhash` value as a canonical slot-level default on the block JSON. The
  default is preserved across re-ingests (last write wins — same as params).
- **Generate** (`generate.py` / `_compose_preset_hsp`): when emitting an IR
  slot, set the slot-level `irhash` field from:
  1. the spec's resolved `ir` field, if present, OR
  2. the canonical ingested `irhash` carried on the block, otherwise.
  3. If neither is available — fail generation with "IR block requires an
     `ir` field (no canonical default available); see `helixgen list-irs`."
- The legacy `.hlx` chassis path does not get this change; `.hlx` IR blocks
  use a different identity model and are out of scope.

This means presets generated **without** the `ir` sugar continue to work for
already-ingested IR-bearing presets (they pick up the canonical hash) — same
fallback behavior we got from the manual irhash patch on the registration
presets, but built in.

## Tone-skill update (separate file)

Edit `.claude/skills/tone/SKILL.md` to teach the skill about user IRs.

Behavior:
- When picking cabs, run `helixgen list-irs` early. Cache result for the
  session.
- If user-preference memory says "prefer IRs over stock cabs when available":
  - For each cab slot in the chain, parse the IR filenames in the mapping
    for cab/mic/position hints (e.g. `YA VX30 212 BLU Mix 01.wav` →
    Vox AC30-style 2x12 Blue, "Mix" position).
  - If a matching IR exists for the chain's tonal target, prefer the IR
    block over the stock cab.
  - Anti-fizz baseline (Hi Cut 6500–7000, Low Cut 80–100) still applies, set
    on the IR block itself.
- New users (no preference memory) get stock cabs as today, unchanged.

The memory entry that flips this behavior is created when the user explicitly
asks (e.g. "from now on, prefer IRs when I have them"). The skill does not
auto-set this preference.

## Module touch list

- New: `src/helixgen/ir.py` — `IrMapping` dataclass with `load`,
  `save`, `register`, `resolve_by_hash`, `resolve_by_basename`, plus
  `default_irs_path()` honoring `HELIXGEN_IRS`. Pure stdlib.
- Edits:
  - `cli.py` — add `register-irs` and `list-irs` subcommands.
  - `spec.py` — accept optional `ir` field on block entries; carry it through
    to the resolved-spec representation.
  - `ingest.py` — capture slot-level `irhash` on IR blocks alongside params.
  - `generate.py` — emit slot-level `irhash` on IR blocks via the resolution
    rules above. Error path when neither spec nor ingest provides a hash.
- Doc: `CLAUDE.md` — add `HELIXGEN_IRS`, the two new commands, and the spec's
  `ir` field. The exact diff is deferred to the implementation plan.
- Skill: `.claude/skills/tone/SKILL.md` — described above, done as a final
  separate edit after the codebase work lands.

## Tests (TDD throughout)

| file                          | covers                                                                 |
|-------------------------------|------------------------------------------------------------------------|
| `tests/test_ir_mapping.py`    | load empty / load existing / save round-trip / `register` idempotency / `register` conflict / `--force` overwrite / relative-path canonicalization |
| `tests/test_ir_register_cli.py` | happy path on a multi-IR preset / count mismatch / single-IR preset / mismatched hash error / `--force` flag |
| `tests/test_ir_list_cli.py`   | empty mapping / multi-entry mapping / output format stable             |
| `tests/test_ir_spec.py`       | spec parse with `ir`=basename / spec parse with `ir`=hash / `ir` on non-IR block rejected / ambiguous basename error / unknown hash error |
| `tests/test_ir_ingest.py`     | ingest preserves slot-level `irhash` on IR block schema                |
| `tests/test_ir_generate.py`   | generate emits irhash from spec sugar / generate emits irhash from canonical ingest / generate fails when neither available / round-trip (ingest → generate → re-ingest → same hashes) |

Real-fixture: the user's `IR Reg DXVB.hsp` and `IR Reg VX30.hsp` already-known
(hash, wav) Rosetta stone is the canonical integration fixture, gated behind
the existing skip-if-not-present pattern so the suite stays green on a clean
clone.

## Open questions resolved during brainstorming

- **CLI shape:** positional ordered (not interactive / not manifest).
- **Mapping location:** library pattern (`~/.helixgen/irs/` + `HELIXGEN_IRS`),
  not cwd-first auto-detect.
- **Mapping schema:** minimal `{hash: path}`, no display_name / tags.
- **Spec sugar:** block-level `ir` field on existing block syntax, not a
  dedicated `"block": "ir"` alias.
- **list-irs:** in v1 (small win, mirrors list-blocks).
- **Generator irhash fix:** rolled into this feature (coupled).
