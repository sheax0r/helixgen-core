# Coordinate-aware snapshot references (round-trip residual #1)

**Date:** 2026-07-04
**Status:** Approved — implementation pending
**Parent:** `2026-07-03-decompiler-round-trip-residuals.md` (category #1, the biggest bucket)
**Baseline:** real-preset round-trip 127/211. This category accounts for ~44 of the
84 remaining failures (`GenerateError: Block '<name>' matches multiple placed blocks`).

## Problem

FS/EXP block references became coordinate-aware (`path`/`lane`/`pos`) in the
parallel-routing effort; **snapshot references did not**. `Snapshot.disable` is a
`list[str]` and `Snapshot.params` is a `dict[str, dict]` keyed by bare block name.
When a snapshot references a block whose `display_name` is ambiguous — many real
blocks humanize to generic names like "Stereo" / "Mono" / "Parametric Mono" —
`generate._resolve_spec_block` raises "matches multiple placed blocks".

The decompiler already has the machinery: `_name_index` builds a
display-name → `[(path, lane, pos), …]` index, and `_ref(name, pi, lane, pos, idx)`
returns `{"block": name}` plus `lane`/`pos`/`path` **only when the name is
ambiguous**. FS/EXP recovery use it. Snapshot recovery still emits bare
`block.display_name`.

## Design

"Clean unless it has to carry a coordinate" — the same ethos `_ref` already uses.

### Spec model (`spec.py`)

New internal ref type, normalized at parse time so `generate` sees one shape:

```python
@dataclass
class SnapshotBlockRef:
    block: str
    path: int | None = None
    lane: int | None = None
    pos:  int | None = None

@dataclass
class SnapshotParamOverride:
    ref: SnapshotBlockRef
    params: dict[str, Any]

@dataclass
class Snapshot:
    name: str
    disable: list[SnapshotBlockRef]        = field(default_factory=list)
    params:  list[SnapshotParamOverride]   = field(default_factory=list)
```

Parser accepts, and normalizes to the above:

- **`disable`**: a list whose entries are each `str` **or**
  `{"block": name, "lane"?: L, "pos"?: P, "path"?: pi}`. A bare string →
  `SnapshotBlockRef(block=s)`.
- **`params`**: **either**
  - the existing name-keyed dict `{"<block>": {"<param>": v, …}, …}` (unambiguous,
    backward-compatible), normalized to one `SnapshotParamOverride` per key with a
    bare `SnapshotBlockRef(block=name)`; **or**
  - a list `[{"block": name, "lane"?, "pos"?, "path"?, "params": {…}}, …]`,
    normalized one-to-one.
  Mixing the two JSON forms in a single snapshot is not allowed (a `params` value
  is a dict or a list, not both).

Validation errors mirror the existing FS/EXP wording (`"lane" must be an integer`,
`"params" must be an object`, etc.) with `snapshots[i]` source paths.

### Generate (`_build_snapshot_overrides`)

Iterate the normalized lists and thread coordinates into the already
coordinate-capable resolver:

```python
for ref in snap.disable:
    path_idx, chain_idx, _ = _resolve_spec_block(
        ref.block, resolved, spec=spec, path=ref.path, lane=ref.lane, pos=ref.pos)
    ...
for ov in snap.params:
    r = ov.ref
    path_idx, chain_idx, block = _resolve_spec_block(
        r.block, resolved, spec=spec, path=r.path, lane=r.lane, pos=r.pos)
    validate_params(block, ov.params)
    ...
```

No new resolution logic — `_resolve_spec_block` already filters `matches` by
`lane`/`pos` when given. Ambiguous refs that carry coordinates now resolve.

### Decompile (`_recover_snapshots`)

Pass the `idx` from `_name_index` (already built in `decompile_body` for FS/EXP)
into `_recover_snapshots`. Accumulate per-block overrides keyed by
`(pi, lane, pos, name)`, then emit **per snapshot**:

- **`disable`**: for each block, `d = _ref(name, pi, lane, pos, idx)`; append the
  bare `name` string when `d == {"block": name}` (unambiguous), else the full dict.
- **`params`**: if **every** param-referenced block in that snapshot is
  unambiguous (`len(idx[name]) <= 1`), emit the current name-keyed dict form
  (keeps existing output byte-for-byte, so existing decompile tests stay green);
  if **any** is ambiguous, emit the whole snapshot's params as the list form
  `[{**_ref(...), "params": {…}}, …]` (uniform within a snapshot).

### Docs

Add the snapshot list form + coordinate-carrying `disable` entries to the
`snapshots` section of `CLAUDE.md`, noting they are only needed to disambiguate
duplicate-named blocks.

## Testing (TDD)

1. `parse_spec` — dict-form params (existing), list-form params, mixed
   disable (`str` + dict), and each malformed-input error.
2. `_build_snapshot_overrides` — a list-form param override on a duplicate-named
   block resolves to the right `(path, chain)`; the wrong coordinate raises
   "no such block".
3. `decompile._recover_snapshots` — unambiguous snapshot still emits the dict
   form; a synthetic duplicate-named preset emits the list form with lane/pos.
4. Round-trip: synthetic preset with two "Stereo"-named blocks, one disabled and
   one param-overridden per snapshot → decompile → generate → identical placement.
5. Re-measure `tests/test_decompile_acceptance.py` real-export scoreboard.

## Out of scope

- Categories #2 (P35 branch-lane I/O) and #3 (IR-no-assignment) — separate specs.
- Tightening the acceptance test to a full-body `strip_provenance` compare — done
  after all three categories shrink.
