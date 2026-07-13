# Dual-amp / parallel synthesis for the `.hsp` → device transcoder — design spec

Status: **SHIPPED 2.18.0** (2026-07-13) — hardware-validated on Stadium XL vs HX Edit's own import. Capture-free (no device RE).

## 1. Problem

`transcode.py::hsp_to_sbepgsm` reads **only DSP path 0** (`bridge.hsp_to_chain_with_irs(body, dsp=0)`) and `synthesize_serial_sfg` emits **one serial chain** (flow 0 = chain, flow 1 = a fixed *empty* carrier). So any preset with a second amp/path installs missing half its signal. Two distinct `.hsp` parallelism mechanisms must be synthesized:

- **(a) Dual-DSP** — `preset.flow` is a 2-element list, one serial chain per DSP, summed at the output matrix. Example: `schism-dual-amp.hsp` (flow 0 = gate→drive→2 amps→cabs→time-fx; flow 1 = Plexi→cab).
- **(b) Intra-flow split/join** — within one flow, a `P35_AppDSPSplitY` (`type:"split"`) and `P35_AppDSPJoin` (`type:"join"`) bracket two lanes; lane-1 blocks live at `bNN` keys `14+pos` (`view.py:140`). Example: `the-grudge-dual-amp.hsp` (flow 0 splits amp A / amp B and rejoins; flow 1 empty).

Both are fully authorable by helixgen's `spec`/`recipe`/`view` stack and both are losslessly **decoded** today (fixture `preset_152` round-trips `== doc`). Only *synthesis* (authored `.hsp` → device, no device-origin `raw`) is missing.

## 2. Device `_sbepgsm` encoding (ground truth from `preset_152`)

- `sfg_ = {enbl, fcnt, flow:[…]}`; `fcnt` = DSP count (2). Each flow: `{bcnt, blks, bmap, cid_, enbl, snap, tid_}`.
- `blks` = flat alternating `[slot_index, block_dict, …]`. `bmap` = **global instance-id grid**, width `bcnt` (28 in 152), spanning both flows (flow 0 owns ids 0–27, flow 1 owns 28–55). `bmap` ordering encodes routing.
- Block dict: `{id__ (global instance id), tid_, type (int), cid_, enbl, favo, hasb, snap, hrns, mdls:[{id__:model_id, parm}]}`.
- `type` int ↔ category: `1`=fx, `2`=looper, **`3`=split**, **`4`=join**, `5`=amp, `6`=cab, `8`=input, `9`=output.
- **Split block** (`id__=24` in 152): `type:3`, model `id__:475` (3 parm: balance/…); **`bblk`/`bflw`** cross-reference its partner join's block id + flow. `hrns.id__=479`.
- **Join block** (`id__=31`): `type:4`, model `id__:478` (6 parm: levels/pans); `bblk`/`bflw` → partner split. `hrns.id__=479`.
- Intra-flow parallel also needs a **second endpoint group** inside the flow: a `P35_OutputPath2A` (model 779) for lane A's output plus a paired `InputNone`/`OutputNone`, as 152 flow 0 carries.

## 3. Design

Replace the serial-only synth with a **graph-driven** one. Two functions change in `transcode.py`:

### 3.1 `hsp_to_sbepgsm` — read the whole graph
- Stop hard-wiring `dsp=0`. Extract **per-DSP-path chains** for every populated flow, preserving each user block's `(lane, pos)` and any split/join structure. Reuse `view.py`'s existing lane/split reconstruction (`view.py:71-87,366-436`) rather than re-deriving it — it already turns a `.hsp` flow into ordered lanes + balanced split/join partners.
- Produce a recipe carrying: per-path block lists **plus** the routing skeleton (which blocks are split/join, lane membership, partner links, per-path `input` routing from the `.hsp` `input` field: `inst1`/`inst2`/`both`/`none`).

### 3.2 `synthesize_sfg` (replaces `synthesize_serial_sfg`)
- Emit **one populated `sfg_.flow` per DSP path** (not a fixed empty flow 1). Each flow gets its correct **live input endpoint** (`P35_InputInst1_2` / `P35_InputInst2` / `InputNone` per the path's `input`) and an `OutputMatrix` (both paths sum there).
- For an **intra-flow split**: emit the `type:3` split + `type:4` join blocks with `bblk`/`bflw` partner pointers, place lane-1 blocks per the `14+pos` convention, emit the paired `OutputPath2A` + second `InputNone`/`OutputNone` endpoint group, and attach split/join `hrns` (id 479) + model scaffolds (split 475 / join 478) captured from `preset_152`.
- Instance ids (`id__`/`tid_`) globally monotonic across flows; `bmap` = the global grid. **Keep the current sequential-`tid_` + ordered-`bmap` scheme** (hardware-tolerant for serial); whether it also validates for populated flow 1 + splits is the main hardware-validation risk (see §5).
- **Export the block-instance-id assignment** as a return value / recipe annotation: the snapshots/controllers synth (separate spec) needs each user block's assigned device instance id (`eID_`) to build its snapshot/controller targets. This is the coupling point — synth must expose `{(path,lane,pos) → device instance id}`.

### 3.3 Scaffold-table fixes (`transcode.py`)
- **Bug:** `_CATEGORY_TYPE` maps `preamp→3`, colliding with the split type-int. Re-map preamp to its real type and **add `split→3`, `join→4`** with their `hrns` (479) + model scaffolds.
- Add split/join to `_HRNS_BY_CATEGORY` and the endpoint-template set; capture the `OutputPath2A` endpoint dict from `preset_152` (verbatim, like the existing `_INPUT_INST1` etc.).

## 4. Testing
- **Offline structural synth test.** For `preset_152`: `sbepgsm_to_recipe`, **drop `raw`**, `recipe_to_sbepgsm` (synth path), `decode_any`, assert **both paths' modeled blocks + the split/join structure survive** (structural, not byte-exact — synth won't reproduce 152's exact `bmap`/`tid_` grid). Add `preset_152` to the synth-roundtrip parametrization it currently skips (`test_transcode.py:167-189`).
- **Hardware.** `device install` of `schism-dual-amp.hsp` (dual-DSP) then `the-grudge-dual-amp.hsp` (intra-flow split): read back, confirm both paths/lanes present with correct models/params/IRs, and audition. Split routing is the higher-risk case — iterate on `bmap`/`tid_` if the device mis-wires it.

## 5. Risks
- **Split-routing byte correctness.** The identity-`bmap`/sequential-`tid_` tolerance is proven only for a single serial chain. Populated flow 1 and especially split/join may require the real global-grid `bmap` ordering. Mitigation: the offline structural test + hardware read-back catch mis-wiring; fall back to reproducing 152's exact grid pattern if the identity scheme fails on device.
- **Coupling with snapshots spec.** Instance-id assignment must be stable and exposed. Both specs land in one worktree, routing first.

## 6. Rollout
1. `hsp_to_sbepgsm` multi-path read + `synthesize_sfg` (dual-DSP first, then split/join) + scaffold fixes.
2. Offline structural test (152) green.
3. Hardware-validate schism, then grudge.
4. Land with the snapshots/controllers work (shared worktree), then release.
