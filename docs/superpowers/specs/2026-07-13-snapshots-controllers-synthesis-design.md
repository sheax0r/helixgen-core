# Snapshots + controllers synthesis for the `.hsp` → device transcoder — design spec

Status: **approved design, building** (2026-07-13). Snapshot deltas are capture-free; FS/EXP controllers need the shared device-RE session (§4).

## 1. Problem

`transcode.py::_synth_cg` fabricates a **minimal `cg__`**: 8 blank generic snapshots (`"SNAPSHOT 1..8"`, empty `tamv`) and **zero controllers** (`srcs`/`trgs`/`ctrl` all `[]`). `bridge.hsp_to_chain_with_irs` reads only each param's `value` and **drops** every `@enabled.snapshots` / `params[…].snapshots` array and every `controller` dict. So a network-installed tone loses all scenes and footswitch/EXP assignments, even though the `.hsp` fully defines them.

This spec restores them by synthesizing `cg__.entt` from the `.hsp`. It has **two parts**: snapshot **deltas** (Part A, capture-free — the encoding is visible in fixtures) and **controller wiring** (Part B — needs the RE session).

## 2. Ground truth

### 2.1 `.hsp` side
- **Snapshot names/metadata:** `preset.snapshots` = 8× `{name, color, expsw, source, tempo, valid}`.
- **Per-block bypass per snapshot:** `bNN["@enabled"]["snapshots"]` = 8-elem bool list.
- **Per-param value per snapshot:** `bNN["slot"][0]["params"][P]["snapshots"]` = 8-elem list (densified on generate).
- **Footswitch bypass assignment:** `bNN["@enabled"]["controller"] = {type:"targetbypass", source:0x010101NN, behavior, ...}`.
- **EXP param sweep:** `params[P]["controller"] = {type:"param", source:0x010201NN, min, max, curve, ...}`.
- **Scribble strip:** the `preset.sources` map (`{fs_color, fs_label, fs_topidx}` per source id).
- Source-id table: `controllers.py::CONTROLLER_META` (FS1–11 = `0x010101(FS#-1)`, EXP1/2 = `0x010201`, EXP1Toe = `0x01010500`).

### 2.2 Device `cg__.entt` side (decoded from `preset_151/152/157` — all rich)
- **`snps[i].tamv`** = the snapshot delta: flat `[trg_id, value, trg_id, value, …]`, `trg_id` indexes `trgs[].id__`; value is that target's value **in snapshot i**. `snps` ordered by **descending `si__`** (7→0).
- **`trgs[]`** = target catalog. Two shapes: **bypass** `{type:1, enty:2, pid_:0, eID_:<block entity id>, mmid:<model id>}` (tamv value = bool); **param** `{type:2, enty:3, pid_:N, eID_, mmid, ppid}` (tamv value = float).
- **`ctm_.stid`** = list of snapshot-tracked trg ids (= the trg-id set present in every `tamv`). **`ctm_.ptid`** = packed `(eID_<<16 | pid_) → trg_id` for param targets.
- **`srcs[]`** = controller sources: `{id__, locl, ctxt, byps, type, mtyp, mtms, cnt1/2/3, cmds}`. **`locl`/`ctxt` do NOT contain the `.hsp` `0x010101NN` ids** — the correspondence is unknown (Part B RE).
- **`ctrl[]`** = wiring `{cid_, tid_ (→trg), trig (→src), type (1=bypass/3=param), behv, curv, min_, max_, thrs, togl, ...}`.
- **`sm__.scid`** = `[trg_id, [cid,…], …]` (which ctrl ids drive each target).
- Volatile counters `nxtt/nxtc/nxts` = next trg/ctrl/src id.

## 3. Design

### Part A — snapshot deltas (capture-free)
In a new `_synth_cg_from_recipe(recipe, block_instance_ids)`:
1. **Build the target catalog `trgs`** from the recipe's snapshot arrays. For each user block that has a per-snapshot bypass array → one `type:1/enty:2/pid_:0` trg keyed by that block's device entity id (`eID_`, supplied by the routing synth — the coupling point). For each param with a per-snapshot value array → one `type:2/enty:3/pid_:N` trg.
2. **Build `snps[0..7]`**: name/`exsw`/`bpm_` from `preset.snapshots`; `si__` descending; `tamv` = flat `[trg_id, value]` for every tracked target using that snapshot's value from the `.hsp` array.
3. **Build `ctm_.stid`** (all tracked trg ids) and **`ctm_.ptid`** (packed param index).
4. **Counters**: `nxtt = max trg id + 1`, `asnp = 0`, etc.
5. **Only emit a trg for a block/param that actually varies across snapshots** (matches device behavior — untracked params aren't in `stid`). A tone with no snapshot variation yields the current blank-8 result.

**Dependency:** `eID_` (block entity id) comes from the routing synthesis (dual-amp spec). The synth must expose `{(path,lane,pos) → device instance id}`; this spec consumes it. Same worktree, routing first.

### Part B — FS/EXP controllers (needs RE)
1. **RE step (shared session, §4):** pin the `.hsp` source id (`0x010101NN` FS, `0x010201NN` EXP) → device `srcs.locl`/`ctxt` correspondence and the `ctrl` field mapping (`.hsp` `behavior`/`curve`/`min`/`max` → `behv`/`curv`/`min_`/`max_`). Method: author a tone with a **known** FS→bypass and EXP→param, realize it on the device, `pull` + decode `srcs`/`ctrl`, diff. Repeat for ≥2 FS + 1 EXP to generalize the `locl`/`ctxt` rule.
2. **Synthesis:** from each `@enabled.controller` / `params[P].controller`, emit a `srcs` entry (with the derived `locl`/`ctxt`), its `trgs` entry (bypass or param, sharing the snapshot catalog where the same target is also snapshot-tracked), a `ctrl` entry linking them, and `sm__.scid`. Emit the scribble-strip config into `pm__.floorboard.stomp.{a,b}.N.{color,label,topidx}` from `preset.sources`.
3. **Fallback if RE inconclusive:** ship Part A alone this release; keep controllers dropped (documented) and file the RE as a follow-up. Part A is independently valuable (scenes change the sound).

## 4. Shared device-RE session
Runs once, feeds this spec (controller mapping) and the non-activating-read spec (GET command). See that spec's §3 for session mechanics (tcpdump/Frida on 2002 + HX Edit driving). Deliverable for this spec: a documented `FS#/EXP → locl/ctxt` table + `ctrl` field mapping, added to `controllers.py` (or a new `device/controllers_device.py`) with the fixture/capture evidence noted inline.

## 5. Testing
- **Offline (Part A):** author a `.hsp` with a 2-snapshot bypass + param delta → synth → `decode_any` → assert `snps[i].tamv` carries the right `(trg,value)` pairs and `ctm_.stid` matches. Round-trip an existing rich fixture through decode→synth-of-projected-recipe and assert snapshot deltas survive structurally.
- **Offline (Part B):** author a `.hsp` with a known FS + EXP → synth → assert `srcs`/`trgs`/`ctrl`/`sm__.scid` + `pm__` scribble entries match the RE-derived encoding.
- **Hardware:** install a snapshot-heavy tone (e.g. `always-with-me-always-with-you.hsp`), confirm the 8 scenes carry the authored names and that switching snapshots changes the sound as authored; confirm assigned footswitches toggle the right blocks and EXP sweeps the right param.

## 6. Rollout
1. Part A (snapshot deltas) — capture-free, lands with routing synth.
2. RE session → controller mapping.
3. Part B (controllers) — lands if RE succeeds, else deferred with Part A shipping.
4. Hardware-validate, release.
