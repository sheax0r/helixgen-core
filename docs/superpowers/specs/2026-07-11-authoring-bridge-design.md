# Authoring bridge: helixgen recipe/.hsp → device preset

**Goal:** install a helixgen-authored preset directly onto a device slot so
`/tone "…"` can produce a preset that's immediately playable — no editor, no file
import.

## What we have (unblocked)
- **Write:** `/CreateContent(container,pos,type,{name})` → new cid, then
  `/SetContentData(cid, storedBlob)` installs arbitrary content (byte-faithful,
  proven). `client.push_to_slot()` wraps this.
- **Content schema (decoded):** stored blob = `\xff\xff\xff\xffpgsm` + msgpack
  `{cg__ (config/snapshots/controllers), pm__ (global params), sfg_ (signal
  flow)}`. `sfg_.flow[dsp].blks` = flat `[int,dict,…]`; each block dict has
  `mdls[0]` = `{id__ (model id), parm:[{pid_, valu}]}`.
- **Rosetta:** `defs.py` — model-name↔id (`model_id_for`) and per-model params
  (`param_id_for`, `param_meta` with default/min/max). 801 models / 7065 params.
- **helixgen recipe/.hsp:** blocks with model-id strings + named params (0..1
  floats). `recipe.apply_recipe` already builds a full `.hsp` body from a recipe.

## Chosen approach: **mutate a device template** (not build-from-scratch)
Building a valid `_sbepgsm` from nothing means reproducing harness (`hrns`),
routing, `bmap`/`bcnt`, snapshot/controller scaffolding — brittle. Instead:

1. Start from a **template content blob** taken from a real on-device preset
   (pull an empty/simple preset, or ship a vendored template blob). It already
   has valid structure, harness, routing, N block slots.
2. **Rewrite blocks** to match the recipe (serial chain, v1 scope):
   for recipe block *i* at flow position *p*:
   - `blk.mdls[0].id__` = device model id (`defs.model_id_for(translate(model_id))`).
   - Rebuild `blk.mdls[0].parm` = for each param of that model in `defs`
     (id/default), a `{pid_, valu}` with recipe overrides applied; leave
     non-overridden at model default.
   - `blk.enbl = 1`; carry the template's `hrns`/`type` where compatible.
   - Bypass/empty the template's remaining block slots.
3. Set preset `name`; re-encode with `content.encode_content_data`; write via
   `/CreateContent` + `/SetContentData`.

### v1 scope (match helixgen v1)
- Single serial chain per DSP (helixgen v1 already only emits that).
- Amp/drive/cab(IR)/delay/reverb/mod/filter/eq/dyn blocks by model + params.
- IR blocks: set `irhash` (reuse helixgen's irhash); IR file upload is a separate
  backlog item — for now reference IRs already on the device.
- Snapshots/footswitches/expression: pass through template defaults in v1;
  proper mapping is a follow-up.

## Validation strategy (live, incremental)
- **P0 mechanism:** mutate one param in a pulled blob → SetContentData → reload →
  verify. (Already effectively proven by push/restore + set-param.)
- **P1 one block:** swap one block's model + params via blob mutation → verify the
  reloaded edit buffer shows the new model id + parm valus.
- **P2 full recipe:** author a known simple recipe (e.g. drive→amp→cab→reverb) →
  install → reload → assert each block's model/params match, and it loads without
  error. Cross-check against `list`/`read`.
- Always author into an **empty slot** (2D) and delete after tests.

## Surfaces
- `client`/`bridge.py`: `recipe_to_content(recipe, template_blob) -> bytes` and
  `install_recipe(client, recipe, container, pos, name) -> cid`.
- CLI: `helixgen device install <recipe.json> <name> --pos N` (and later a
  `tone --to-device` path).
- MCP: `device_install_preset(model, recipe, ip, pos, name)`.

## Status: mechanism PROVEN (live)
- **P1 (single block):** built a Screamer 808 (id 310) from `defs` (model + full
  parm list, Gain override), swapped it into a template block, `SetContentData`
  → reloaded as model 310 with Gain=0.85. **PASS.**
- **P2 (multi-block):** rewrote two blocks (drive→Screamer, reverb→Glitz 73) from
  `defs` in one blob → installed → both reloaded faithfully (model + params).
  **PASS.** (The amp swap was skipped: `HD2_AmpBrit2204Custom` didn't resolve via
  `defs.model_id_for` — a name-mapping gap, see remaining work.)

Conclusion: constructing blocks from `defs` and installing them via
`/SetContentData` works; same-category swaps into a template keep valid
scaffolding. The bridge is an engineering task now, not a research one.

## Remaining work for a production `device install`
1. **Model-name resolution gap** — some `.hsp`/helixgen model strings don't match
   `defs` keys directly (e.g. `HD2_AmpBrit2204Custom`). Build a resolver that
   applies helixgen's ingest translation and falls back on catalog/alias lookup;
   fail loudly on unresolved models.
2. **Category ↔ template position** — map each recipe block to a template slot of
   the matching category, or curate per-category template blocks (with correct
   `type`/`hrns`) to assemble from. Bypass unused template slots.
3. **Param coverage** — Hz/int/enum params (not just 0..1 floats) via `defs`
   `type`; IR blocks (`irhash`); snapshots/controllers (pass-through v1).
4. **Surfaces + tests** — `bridge.py` (`recipe_to_content`, `install_recipe`),
   CLI `device install`, MCP `device_install_preset`; unit tests (mock) + a
   live-gated test authoring a known chain and asserting model/params.
5. **Template source** — ship a vendored empty/neutral template blob so authoring
   doesn't depend on a specific on-device preset.

## Risks / unknowns
- Model swap may need more than `id__`+`parm` (e.g. `type`, `hrns` fields tied to
  the model/category). Mitigation: pick template slots of the same category, or
  copy `mdls[0]` scaffolding from a device preset that already uses the target
  model.
- Param set completeness: some models have Hz/int/enum params — map by `defs`
  type; keep 0..1 floats first.
- `bcnt`/`bmap` and dual-DSP: keep template's; only edit within existing slots.
