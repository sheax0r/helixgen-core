# Reverse transcoder: `.sbe` (`_sbepgsm`) → `.hsp`

**Bead:** `hgc-zbl` · **Date:** 2026-08-12 · **Module:** `src/helixgen/device/untranscode.py` ·
**Verb:** `helixgen device to-hsp` · **Tests:** `tests/test_untranscode.py`

## The problem

The transcoder was one-way. `device/transcode.py` turned an authored `.hsp` into device
content; nothing came back. A `.sbe` blob from `device pull` / `device backup` was opaque:
`device sync` re-pushed it verbatim, `ir-prune` decoded only its `irmd` hashes, and
`setlist import-hss` transcoded sbepgsm payloads *onto* the device without ever producing a
`.hsp`. So a preset authored on the hardware or in HX Edit could not be viewed, patched,
diffed, or round-tripped — it entered the library as a dead end.

## The approach: invert the forward path, exactly

The forward transcoder was hardware-validated byte-for-byte against HX Edit's own import
(2.18.0). That makes it the **spec to invert**, and it makes `.sbe → .hsp → .sbe` a **free
test oracle** — no device, no hand-written expectations.

Every mapping in `untranscode.py` is the inverse of a named piece of the forward path:

| device | `.hsp` | forward counterpart |
|---|---|---|
| `sfg_.flow[i]` | `preset.flow[i]` | `transcode.synthesize_sfg` |
| grid position `gp` | the `bNN` key (lane 1 at `14 + pos`) | `bridge._lane_pos` |
| `mdls[0].id__` | `slot[0].model` | `bridge._default_resolve_model` / `modelmap` |
| `parm[].pid_` | `slot[0].params` key | `bridge.param_name_map` |
| `mdls[0].irmd` | `slot[0].irhash` | `transcode._make_user_block` |
| block `enbl` | `@enabled.value` | `bridge.hsp_to_paths` (`spec["enabled"]`) |
| `cg__` trgs + `snps.tamv` | `@enabled.snapshots` / param `snapshots` | `transcode._synth_cg_from_recipe` |
| `cg__` ctrl + `srcs` | `@enabled.controller` / param `controller` | `transcode._controller_locl_ctxt` |
| `pm__` | `preset.params` / `preset.sources` / `clip` / `xyctrl` | `transcode._synth_pm` |
| `cg__.asnp` | `preset.params.activesnapshot` | *(new — see below)* |

Emission is **direct to an `.hsp` body**, not via helixgen's recipe/`generate` path. Going
through `generate` would drag in a chassis template, block-library resolution and foreign
`meta`, and would lose exactly the fidelity this feature exists to keep. The `.hsp` body
shape is regular enough that direct emission is both smaller and more faithful.

### Two inversions that are subtler than they look

**Param order is load-bearing.** The forward transcoder allocates `cg__` target ids while
walking a block's snapshot/controller params in `.hsp` dict order. Emitting params in
device-pid order instead shuffles every downstream target id. So `_param_plan` reproduces
the **library block's** param order — corpus-observed on `HD2_AmpBritJ45Brt`, whose library
order visits pids 6, 1, 7, 3, 5. Without a library block the device's own names and order
are used; those normalize onto themselves, so the round trip still holds and only the
human-facing names in `view` / `set-param` differ.

**A snapshot's identity is its `si__`, not its array position.** When the *device* saves a
preset it writes the `snps` array in **descending** `si__` order. Reading it positionally
rotates every snapshot's name and every `tamv` scene onto the wrong slot — a silent,
tone-changing bug. `_Cg` sorts by `si__` on the way in.

## A forward-path bug the oracle caught

`cg__.asnp` (the preset's on-load snapshot) was **hardcoded to 0** in both
`_synth_cg` and `_synth_cg_from_recipe`. Every `device install` / `device sync` silently
reset the preset to snapshot 1. It is now lifted from `preset.params.activesnapshot`
(`transcode._active_snapshot`, range-checked to 0–7). This is a forward-direction fidelity
fix that only became visible because there was finally something to compare against.

## Validation

Corpus: 66 real device blobs from `device backup` (gitignored, dropped into
`tests/fixtures/device_content/`), spanning serial chains, dual-DSP, parallel split/join,
dual-cab, IR cabs, snapshots, footswitch + EXP controllers, a looper, and — critically —
five presets the **device itself** re-saved (identified by the `hist`/`selb`/`self`
edit-buffer keys and, in one case, a `bmap` permutation from a hardware reorder).

Three assertions, all in `tests/test_untranscode.py`:

1. **Byte-exact round trip** — `61/66`. Every preset helixgen itself installed comes back
   bit-identical. Pinned as a ≥85% ratio so a regression that starts perturbing ordinary
   presets fails loudly even though the corpus is gitignored and varies by machine.
2. **Fixed point** — `66/66`, in both directions. The second pass reproduces the first
   exactly (`.hsp` identical, `.sbe` identical). The conversion is a canonicalization, not
   a lossy step that keeps eroding.
3. **Semantic equivalence** — `66/66`. An identity-free projection (blocks addressed by
   grid slot, params by pid, snapshot scenes and controller assignments resolved *through*
   the target graph so id renumbering cancels) is identical before and after. This is what
   makes "sonically null" an assertion rather than a claim: any real change to a model, a
   value, a scene or a controller assignment still fails the test.

Plus self-contained round-trip tests (recipe → `.sbe` → `.hsp` → `.sbe`) that need no
fixtures and no library, covering serial, dual-DSP, split/join, IR, base bypass, snapshot
deltas, footswitch bypass, EXP sweeps, momentary behavior, and the EXP source-bypass flag.

`view` was checked against the paired library `.hsp` for three converted presets
(dual-cab split, dual-amp, controller-heavy): input mode, output section, snapshot scenes,
footswitch assignments and EXP sweeps all render identically.

## Residual diffs on device-re-saved content, and why each is null

| diff | why it is not tone |
|---|---|
| `pm__` list order (device sorts keys lexicographically, helixgen numerically) | a key/value list; order is not semantic — the same keys carry the same values |
| `snps[].si__` array order (device writes descending) | each record carries its own `si__`; we read and write by it |
| msgpack map-key order inside `ctrl` records | msgpack maps are unordered; the decoded documents are equal |
| `cg__` target / source / controller id numbering | pure identity. Every reference (`tamv`, `stid`, `ptid`, `scid`, leaf `tid_`) is renumbered consistently |
| block `id__` + the `bmap` permutation | a hardware reorder leaves stable-but-shuffled ids; the forward path renumbers canonically. `bmap[gp]` still names the block at `gp` |
| block/leaf `cid_` back-pointers to the driving controller | device-side denormalization. helixgen has never written them and controllers work on hardware regardless (61 byte-exact presets with live controllers prove it) |
| `hist` / `selb` / `self` top-level keys | edit-buffer scratch state, not preset content |
| float widening (`0.15` → `0.15000000596046448`) | the device only ever held float32; it re-encodes to the identical float32 |

## Deliberately out of scope

- **Command Center commands (#16)** and **MIDI CC controller bindings (#33)** are dropped
  with a warning naming how many. Both are EXPERIMENTAL in the forward direction, both have
  uncaptured slot layouts, and neither appears anywhere in the 66-blob corpus — so there is
  nothing to validate an inverse against. Filed as a follow-up bead.
- **Per-block `harness` state.** The forward path ignores `.hsp` `harness` entirely and
  synthesizes a canonical `hrns` from `_HRNS_BY_CATEGORY`; since the round trip is
  byte-exact, the synthesized harness already matches the device's. Emitting it would add
  noise with no fidelity gain.
- **Preserving device instance ids through the forward path.** This would close the last
  structural diff on hardware-reordered presets, but it means changing the
  hardware-validated forward transcoder's output for every preset to fix a cosmetic
  difference on one. Not worth the risk; the semantic-equivalence test covers the gap.
