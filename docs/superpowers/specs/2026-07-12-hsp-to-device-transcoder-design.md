# `.hsp` → device transcoder (template-free install) — design spec

Status: **approved design, building** (2026-07-12).

## 1. Why

A `.hsp` **is** a complete Line 6 Stadium preset — but in Line 6's **JSON file
format** (`rpshnosj` magic). The device stores/accepts presets in a **different
Line 6 serialization**, `_sbepgsm` **msgpack** (numeric model/param ids, a flat
per-slot block grid, top-level `cg__`/`pm__`/`sfg_`). HX Edit transcodes
`.hsp` → `_sbepgsm` on import. helixgen can *decode* `_sbepgsm` (for
`view`/`backup`) but has never *written* it from a `.hsp` — so the network-install
path used a shortcut: clone an existing device preset and overwrite its
same-category block slots (the "template"). That shortcut is the sole source of
the template precondition, the coverage failures, the dual-amp flattening, and
the skill's unwanted opinions about device presets.

**Goal:** write the faithful `.hsp` → `_sbepgsm` transcoder so device install is
just *encode the preset into the device's format and `/SetContentData` it into
the pool.* No template, no coverage limits, full fidelity (snapshots, dual-amp,
controllers). The device skill becomes pure library management: get the `.hsp`
on the device, manage it across setlists. Authoring stays in `tone`/`setup`.

## 2. What we know (hardware-confirmed 2026-07-12)

- `_sbepgsm` top level: `{cg__, hist, pm__, sfg_}`. `sfg_.flow` is a 2-element
  list (one per DSP path). Each flow: `{bcnt, blks, bmap, cid_, enbl, snap,
  tid_}`. `blks` is a flat alternating `[idx, blockdict, …]`.
- A block dict: `{cid_, enbl, favo, hasb, hrns, id__, mdls, snap, tid_, type}`.
  `type` is the block-slot kind int (8=input, 9=output, 1=fx, 5=amp, 3=preamp,
  6=cab, 2=looper, 4/… split/join). `mdls[0]` = `{id__ (numeric model id), parm:
  [{pid_, valu, …}], …}`.
- **`hrns` (harness) is a constant scaffold** — byte-identical across blocks
  (`{cid_:0, enbl:1, id__:420, lbid:-1, parm:[{pid_:11,valu:false},
  {pid_:12,valu:true},{pid_:13,valu:-1}], snap:false, tid_:0, vers:0}`). Not
  model-specific for the serial case.
- `cg__` = `{asnp (active snapshot), entt:{cmnd, ctm_, ctrl, sm__, snps
  (8-element snapshot array), srcs, trgs}, nxtc/nxti/nxtm/nxts/nxtt (next-id
  counters)}`. Volatile across save; NOT part of the fidelity comparison.
- `pm__` = list of `{key_, type, val_}` preset params (e.g.
  `preset.clip.end`). Stable; part of the fidelity comparison.
- Flow routing is **per-preset**: `bmap`, per-slot `tid_`, and split/join blocks
  encode the real signal graph (verified: 152 "Double Double" is dual-amp with
  `type=3`/`type=4` split/join). The transcoder must emit this, not assume a
  fixed grid.
- The `.hsp` (helixgen JSON) already carries the full graph: `generate` builds a
  `.hsp` by mutating a real chassis, so authored `.hsp` files inherit complete
  routing/harness structure. So the mapping is a **transcode**, not synthesis.
- The device round-trip already proven at the codec layer:
  `content.decode_any` ↔ `content.encode_content_data` (dict ↔ bytes);
  `to_content_data` swaps edit-buffer↔stored magic + drops `hist`. Install uses
  the stored encoding (`\xff\xff\xff\xff pgsm`).

## 3. Design

The transcoder is the **device-format sibling of `generate`**: a `recipe`/`.hsp`
→ `_sbepgsm` **dict** builder, then `content.encode_content_data` serializes it.
`view` (existing) is the inverse on the `.hsp` side; we add the `_sbepgsm` side
of the same mapping.

Two functions, sharing one field-mapping table:
- `sbepgsm_to_recipe(doc) -> recipe` — decode a `_sbepgsm` dict into helixgen's
  recipe shape, carrying unmodeled leaves verbatim in `raw` (harness, tid_,
  bmap, split/join topology, controllers, snapshot arrays). Mirrors `view`.
- `recipe_to_sbepgsm(recipe) -> doc` — the transcoder proper. Rebuilds `sfg_`
  (per-path `blks` with numeric `id__` + `parm` from `defs`, `hrns`, `type`,
  `tid_`, `bmap`), `pm__`, and a minimal valid `cg__` (snapshots + controllers
  from the recipe; next-id counters recomputed). Model/param name→id via
  `device/defs.py` (+ helixgen's ingest model-id translation, as the existing
  bridge does).

`.hsp` → recipe already exists (`view`/`spec` parser). So authored `.hsp`
install = `read_hsp` → recipe → `recipe_to_sbepgsm` → `encode_content_data` →
`install_into_pool` (existing). No template arg anywhere.

### Fidelity gate (offline, the safety net)
For every captured device preset blob: `decode_any(blob)` = ground-truth `D`;
`recipe_to_sbepgsm(sbepgsm_to_recipe(D))` = `D'`; **assert `D'.sfg_ == D.sfg_`
and `D'.pm__ == D.pm__`** (ignore volatile `hist`/`cg__`). This proves the
transcoder is a faithful inverse of the decoder without a device. Corpus must
include a serial preset (151), a dual-amp/parallel preset (152), and a
snapshot/controller-heavy preset. Extends the spirit of the 211-export `.hsp`
round-trip to the device format.

### Device validation
Transcode a handful of real authored `.hsp` tones → `install_into_pool` → load →
confirm block/param/IR fidelity and that it plays. Include a dual-amp tone
(`schism-dual-amp.hsp`) — the case the old bridge could not do.

## 4. Rollout

1. `sbepgsm_to_recipe` + `recipe_to_sbepgsm` + the offline round-trip test
   (serial first, then dual-amp, then snapshots/controllers). New module
   `src/helixgen/device/transcode.py`.
2. Repoint `install_into_pool`/`device install`/`setlist_sync` to transcode the
   `.hsp` directly. **Delete the template/bridge path** (`bridge.author_chain`,
   `content_from_template`, `--template`/`template_cid` args, the
   coverage/`_category_group` matching). `bridge`'s `.hsp`→chain + IR helpers
   stay only if still used; otherwise remove.
3. Device-validate (incl. dual-amp).
4. **Strip templates from the `device` skill + CLAUDE.md.** The skill becomes:
   author elsewhere → `device setlist add` → `device sync`. No template, no
   coverage buckets, no factory-preset opinions.
5. Ship.

## 4a. Requirement: device ops must NOT change the active tone

Normal skill/CLI/helixgen operations must **not** change which preset is active
on the device unless the user explicitly asks. Root cause today: reading OR
writing preset content has only one route — `load_preset(cid)`
(`/LoadPresetWithCID`, which makes the preset active) + `get_edit_buffer()`. So
sync (old bridge loads a *template*), `backup`/`pull` (loads each preset), and
fixture capture all switch the active tone as a side effect.

- **Write/sync:** the transcoder removes this entirely — `install_into_pool` is
  `CreateContent` + `SetContentData` (verified: no `load_preset`), and the
  transcoder builds the blob offline instead of reading a device template. So
  once install/sync stop loading a template, the write path is non-activating.
  **Do not add any trailing `load_preset` to the install/sync path.**
- **Read (`backup`/`pull`):** still reads via the edit buffer. Fix by (a)
  finding a **non-activating content-read** command — RE: Frida/tcpdump-capture
  what HX Edit sends when it exports/backs up a preset without loading it (look
  for a `/GetContentData`-style GET counterpart to `/SetContentData`) — or, if
  none exists, (b) **save-and-restore**: record the currently-active preset,
  do the reads, reload it at the end. Tracked as a device-backlog item.
- **Investigation vs product:** ad-hoc investigation may load presets freely;
  the shipped CLI/skill/MCP paths must preserve the active tone.

## 5. Risks / open items

- **Routing leaves** (`tid_` assignment, `bmap`, split/join reconstruction) are
  the hard RE. Mitigation: the round-trip gate catches any leaf we don't
  reproduce; carry unmodeled leaves verbatim in `raw` until modeled, exactly as
  the `.hsp` side does with `raw.harness`.
- **Authored `.hsp` without device-origin `raw`:** authored tones inherit chassis
  structure, so routing is present; confirm the recipe carries enough (else fall
  back to the chassis's structure for the graph, populating models/params).
- Fidelity corpus should grow; start with 151/152 + one snapshot-heavy preset.
- Snapshots/controllers (`snps`/`srcs`/`trgs`) fidelity is required for parity
  with HX Edit import; covered by the round-trip gate on a controller-heavy
  fixture.
