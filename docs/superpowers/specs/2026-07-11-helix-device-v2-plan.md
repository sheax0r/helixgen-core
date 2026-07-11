# helixgen device control (network CRUD) — v2.0.0 plan

**Date:** 2026-07-11
**Goal (session):** full CRUD implementation drivable by CLI **and** MCP, shipped
as a new **major release (2.0.0)**, reviewed + tested (spec tests + live device).

## Decisions (locked with the user)
- **Integrate into helixgen** (not standalone): new `helixgen device …` CLI verbs
  + `device_*` MCP tools.
- **Full content round-trip** in scope.
- **Live device, serialized tests** — one physical Stadium XL; only one agent
  touches hardware at a time. Code work parallelizes; live tests serialize.

## What we know (reverse-engineered + corroborated)
- Transport: **ZeroMQ (ZMTP 3.0)**, cleartext. `2002` ROUTER (RPC, DEALER),
  `2001`/`2003` PUB streams. Device `192.168.4.84` / `p35x1.local`.
- Messages: **OSC** (addr + typetags + args); blob args are **msgpack**.
- Content model: presets addressed by integer **CID**; setlists = virtual
  containers (`-1` FACTORY, `-2` USER, `-5` Throwaway). `cctp` 1000 preset /
  1001 setlist / 1002 template·IR; `posi` slot.
- **Proven commands:** `/GetContainerContents` (list), `/GetContentRef` (read
  meta), `/LoadPresetWithCID` (load), `/AddContentsToContainer` (create/copy),
  `/SetContentAttrs` (rename), `/RemoveContent` (delete), `/ParamValueSet`
  (param), `/status` (ack). Also (from public RE + command defs): `/ModelSet`
  `,iiiii [127,0,1,0,modelId]`, `/SetSnapshotName`, `/EditBufferStateGet`.
- **Preset content** over the wire = `_sbepgsm` blob = 8-byte magic + msgpack
  with uint32-packed 4CC keys. Parses cleanly; **disjoint schema from `.hsp`**
  (no shared vocabulary, no existing converter).
- **Rosetta Stone — bundled in the editor app** (`…/Contents/Resources/`):
  `P35ModelCatalog.json` (model id↔name), `modeldefs/p35md-*.bin` (msgpack:
  per-model param name↔id↔type↔range), `commanddefs/P35EditCommandDefs.json`,
  `P35Controls.json`, `P35ModelUIDefs.json`. These give the name→numeric-id maps
  needed to translate a helixgen recipe into device commands.

## Architecture
- New package `src/helixgen/device/`:
  - `osc.py` — OSC encode/decode + msgpack blob helpers (moved from `tools/osc.py`).
  - `client.py` — `HelixClient` (ZMQ DEALER RPC): connect, list, read, load,
    copy, rename, remove, move, get/set edit buffer, set_param, set_model.
    Lazy-import `zmq`/`msgpack`; raise a clear error if `helixgen[device]` missing.
  - `defs.py` — load the bundled model/param/command defs → name↔id maps
    (shipped as a small vendored JSON asset so runtime doesn't need the app).
  - `content.py` — `_sbepgsm` decode/encode (msgpack + magic) and structured read.
- Dependencies: new optional extra `device = ["pyzmq>=25","msgpack>=1"]`; core
  helixgen stays stdlib+click. Update `.mcp.json` to `--with pyzmq --with msgpack`.
- CLI: `@cli.group("device")` → `list, read, pull, push, create, rename, delete,
  move, load, set-param`. `--ip`/`--port` option (envvar `HELIXGEN_HELIX_IP`),
  `--json` on reads. Wrap errors in `click.ClickException`.
- MCP: `device_*` thin `@app.tool()` wrappers in `server.py` → handlers in
  `tools.py` (each gated by `_validate_model`).

## Scope tiers
**Tier 1 — ship this (proven, low risk):** module + CRUD verbs (list/read/create-
copy/rename/delete/move/load), **backup/restore** (`pull`/`push` the exact
`_sbepgsm` blob → device-faithful clone), **structured read** (`_sbepgsm`→JSON),
**live `set-param`** (`/ParamValueSet`), CLI + MCP surfaces, spec tests +
live-gated test, docs (`/docs/helix-protocol.md`), release **2.0.0**.

**Tier 2 — stretch (higher value, medium risk):** semantic authoring bridge —
push a helixgen recipe/`.hsp` to the device by copying a template into a slot
then replaying `/ModelSet` + `/ParamValueSet` from the recipe using the
name→id maps from `defs.py`. Start param-only (safe), extend to model swaps.

## Parallelization (single-device aware)
1. **Foundation (serial, owner: lead):** build `src/helixgen/device/` module +
   `defs.py` asset; lock the `HelixClient` API. Touches the device (reads only).
2. **Fan-out (parallel, distinct files):**
   - CLI agent → `cli.py` device group + `tests/test_device_cli.py` (mock client).
   - MCP agent → `server.py`/`tools.py` device tools + handler tests.
   - Defs agent → `defs.py` extraction (model/param name↔id) + unit tests.
   - Doc agent (persistent) → `/docs/helix-protocol.md` (fed protocol facts).
   - Bridge agent (Tier 2) → `content.py` recipe→commands, depends on defs+client.
3. **Integration + live tests (serial, owner: lead):** run spec tests; run
   live device CRUD against Throwaway/2D slot; fix.
4. **Release (serial, owner: lead):** bump `plugin.json`+`marketplace.json`→2.0.0
   (+ lib line), update `.mcp.json`/`plugin.json` desc, commit `release 2.0.0 …`,
   PR, merge → workflow tags + fast-forwards `stable`.

## Test strategy
- Spec/unit: mock the ZMQ socket (record real frames as fixtures) so tests run
  without hardware. Codec round-trip tests. Defs mapping tests.
- Live: `tests/test_device_live.py`, `@skipif(not os.environ.get('HELIXGEN_LIVE_DEVICE'))`,
  runs the create→rename→read→delete cycle on USER slot 2D (reversible).
