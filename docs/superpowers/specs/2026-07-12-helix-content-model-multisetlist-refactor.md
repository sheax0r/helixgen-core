# Helix content model, multi-setlist sync, and device-client refactor

Status: **design notes / handoff** (2026-07-12). No code shipped from this
investigation — the setlist-sync attempt was invalidated by the reverse-
engineered content model below and backed out. This doc is the reference for a
fresh design session.

## 1. The device content model (reverse-engineered, hardware-confirmed)

Containers (virtual, negative cids):

| cid | role |
|---|---|
| `-1` | factory setlists/presets |
| `-2` | **the USER preset pool** — the only container that accepts `/CreateContent` |
| `-5` | **setlists root** — holds setlist items (`cctp==1001`), e.g. `Throwaway`, `helixgen` |
| `-11` | user IRs |

Content types (`cctp`):

| cctp | meaning |
|---|---|
| `1000` | preset |
| `1001` | setlist (a container, lives under -5) |
| `1003` | **reference** — a setlist entry that points at a pool preset via `rcid` |

**The key fact:** a **setlist does not contain presets — it contains
references** (`cctp==1003`) whose `rcid` names an actual preset (`cctp==1000`)
living in the `-2` pool. So the device's real model is exactly the multi-setlist
idea: **one library pool + named setlists that are reference-lists into it; a
preset can be referenced by more than one setlist.**

Hardware-observed setlist reference entry:
```
{cctp:1003, cid_:1000, rcid:999, ccid:988, name:'…', posi:1, blck:-1, flow:-1}
```

## 2. Operation gotchas (each one bit us this session)

1. **`/CreateContent` only works in the `-2` pool.** Into a setlist container it
   returns `/error [-47, 'CreateContent: failed']`. You cannot create a preset
   inside a setlist.
2. **`/AddContentsToContainer(setlistCid, [poolCid], pos, 0, 0)` creates a
   REFERENCE, not a copy.** It makes a `cctp==1003` entry with `rcid=poolCid`.
   The reference's edit buffer reads back byte-identical to the pool preset
   (because it *is* the pool preset), which misleads you into thinking it copied.
3. **Deleting the pool preset orphans the reference.** After that,
   `/RemoveContent(setlist, [refCid])` → `/error [-21, 'RemoveContent: failed']`
   and the dangling reference is **invisible in the Stadium app UI** but still in
   the container metadata. (One such orphan — `helixgen` ref 1000 → dead pool
   999 — was left on the device 2026-07-12; benign, clears on a setlist rebuild.)
4. **Prompt propagation needs a 2001 subscription.** Rapid create/copy/delete on
   the 2002 RPC socket without an open `HelixSubscriber(ip, ports=(2001,))` see a
   **lagging container index** (a mirror reported 2 installed but only 1 was
   listed). This is the same device behaviour as the IR-registration-delay fix
   (2.9.0, `sftp.py::push_ir`): open the 2001 SUB first, sleep ~0.6 s, then
   mutate. **Only `push_ir` does this today; the install/sync path does not.**
5. **Returned cids ≠ materialized cids.** `/CreateContent`'s reply cid and
   `install_recipe`'s return did not match the preset's real on-device cid;
   trusting them for deletes orphaned presets. Always re-list by pos/name.

## 3. Multi-setlist feature (the real design)

Model it as the device does:

- **Library pool** = all authored tones, installed once into `-2` (`cctp 1000`).
- **Setlists** = named reference-lists. A local manifest maps
  `setlist-name → [tone names]`; a tone may appear in several.
- **`device sync <setlist>`** rebuilds that setlist's references to match the
  manifest: ensure each tone exists in the pool (author if missing), then set the
  setlist's `1003` references (add missing, remove extra — by removing the
  reference *only*, never the pool preset unless it's referenced nowhere).
- Choosing which setlist to sync is a first-class arg (this is where the
  `device.setlist` preference + "ask & remember" belongs).
- Never delete a pool preset that another setlist still references.

Open sub-problems:
- **Setlist creation (#8)** is still uncaptured: `/CreateContent` ctype for a
  setlist is unknown. Capture it with `tcpdump` on **port 2002** while the Helix
  Stadium app creates a setlist (the 2001 stream only shows the `/addContent`
  result, not the command). Until then, the user creates setlists by hand and we
  resolve them by name.
- **Removing a reference cleanly** (the non-orphaned path) needs RE — capture the
  app removing a preset from a setlist on 2002.

## 4. Device-client refactor (raise the abstraction; stop the footguns)

The low-level client hands out composable primitives with no model attached,
which is how an agent wrote `CreateContent` into a setlist, composed
`AddContentsToContainer`+`delete` into an orphan, and skipped the 2001 sub.

Proposed:

1. **Privatize the primitives.** `create_content`, `create_copy`, `create_from`,
   `set_content_data`, `delete`, `save_*` become underscore-private (or a
   `client._raw.*` namespace). Agents must not reach them directly.
2. **Expose only model-correct high-level ops** that can't orphan a reference:
   `install_into_pool(body) -> pool_cid`, `reference_into_setlist(setlist,
   pool_cid)`, `remove_reference(setlist, pool_cid)`, `mirror_setlist(setlist,
   tones)`. These encode the pool+reference model.
3. **Bake the 2001 subscription into a mutation context:** `with
   client.mutating(): …` opens `HelixSubscriber(2001)` first (+ settle) so every
   write flow gets prompt propagation automatically — not re-implemented per
   feature (today only `push_ir`).
4. **Encode the model in types + a doc:** an enum for containers
   (`POOL=-2`, `SETLISTS_ROOT=-5`, `USER_IRS=-11`) and cctp
   (`PRESET=1000`, `SETLIST=1001`, `REFERENCE=1003`); this file (or a
   `docs/helix-content-model.md`) as the narrative, referenced from the code.
5. **Guardrails at the danger points:** `_create_content` raises if the target
   isn't the pool (“setlists reject CreateContent → ‑47; use
   reference_into_setlist”); `_create_copy` docstring/asserts “creates a
   REFERENCE not a copy — never delete the pool preset (orphans it → ‑21).”

## 5. Quick-wins to salvage (small, independent of the redesign)

- **`device.model` load fix.** The user's real `preferences.json` has
  `device.model: "stadium_xl"` (the MCP token), which the validator *rejected*,
  so `load_preferences()` **throws** on the real file. Fix = accept both display
  forms and MCP tokens case/separator-insensitively, normalizing to the display
  form (`stadium_xl` → `Stadium XL`). ~10 lines in `preferences.py`
  (`_validate_device_model`). Worth shipping on its own.
- **`device.setlist` preference + `client.list_user_setlists()` /
  `resolve_setlist_cid(name)`** (enumerate `cctp==1001` under -5) are correct and
  reusable for the multi-setlist work; keep them in the redesign.

## 6. Also on the device backlog (context)

- **#6** single-tone install/delete IR+ledger parity (MCP).
- **#7** explicit slot-reorder skill + tools.
- **#11 IR cleanup** — `device ir-prune`: diff device user IRs (‑11) vs irhashes
  referenced by all on-device presets; remove orphans (dry-run + confirm).

## 7. Fresh-session kickoff prompt

> We're designing **multi-setlist support** for helixgen's Helix Stadium device
> integration, plus a **device-client refactor** to prevent agents from misusing
> the raw protocol. Start by reading
> `docs/superpowers/specs/2026-07-12-helix-content-model-multisetlist-refactor.md`
> (the reverse-engineered content model + findings) and
> `docs/helix-protocol.md`. Then brainstorm the design with me before any code.
>
> Core model to honor: the device stores a **preset pool** in container `-2`
> and **setlists** (under `-5`) that hold **references** (`cctp 1003`,
> `rcid`→pool preset), so a tone can be in multiple setlists; `/CreateContent`
> only works in `-2`; deleting a referenced pool preset orphans the reference
> (`RemoveContent -21`); and prompt propagation needs an open `HelixSubscriber`
> on 2001 (the push_ir pattern) — the install/sync path doesn't do this yet.
>
> Scope to cover: (1) a local manifest `setlist-name → [tone names]` + a
> `device sync <setlist>` that rebuilds that setlist's references (pool-first,
> never orphaning); (2) the client refactor (privatize primitives, add
> model-correct high-level ops + a `client.mutating()` 2001-subscription context,
> container/cctp enums, guardrails); (3) setlist creation — needs a `tcpdump`
> on port 2002 while the Stadium app creates a setlist to capture the command.
> Also salvage the small `device.model` load-fix (accept `stadium_xl`) and keep
> `device.setlist` + `resolve_setlist_cid`. There is a benign orphaned reference
> in the `helixgen` setlist from prior RE — ignore it; it clears on rebuild.
> Nothing from the prior session was shipped; start clean from `main`.
