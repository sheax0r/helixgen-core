# `/CreateContent` status-code-1 anomaly — investigation findings (backlog #38)

**Date:** 2026-07-15
**Device:** Helix Stadium **XL**, firmware **1.3.2 build 1340** (`vers.majo/mino/patc = 1/3/2`, `buld 1340`), serial `47292244582131381`, IP `192.168.4.84`.
**Reference capture:** the 2026-07-14 parity capture ran against desktop app
`v1.3.2.9805` (same 1.3.2 line) — see `2026-07-14-parity-capture-findings.md`.

## TL;DR

- **The code==1 anomaly has CLEARED.** On 2026-07-15 the device returns the
  documented `code == 0` for **every** `/CreateContent` — a single raw create,
  five rapid create/delete cycles, and a full create→SetContentData→readback
  install all returned `0`. The 2026-07-14 code==1 was **transient device/session
  state** (the device was power-cycled/settled between sessions); **root cause of
  why it returned 1 that day is unconfirmed and not reproducible now.**
- **The #38 report's "the stub entries were empty — `blck=-1, flow=-1`" was a
  MISDIAGNOSIS.** In the **pool** container listing, **every** preset reports
  `blck=-1, flow=-1` — including freshly + successfully installed presets and all
  29 of the user's real library tones (each with 11–20 KB of real
  `/GetContentData` content). `blck`/`flow` in the pool listing are **not** a
  populated/empty signal. There were **zero** actual orphan stubs on the device.
- **The real orphan mechanism is a client bug, and it is now fixed.** When
  `/CreateContent` returns a non-zero code, the device *may* still allocate the
  pool entry as a side effect. The old `_create_content` **discarded the
  allocated cid** (returned `None`), so nothing could ever clean it up. The
  client is now hardened to surface that cid and clean up the stub — see the fix.

## What was reproduced (raw evidence, 2026-07-15)

Baseline: `device info` healthy (fw 1.3.2/1340), 4 setlists (Throwaway,
helixgen, Sarah, Mike), pool = 31 presets.

**Test 1 — single raw `/CreateContent` into the pool** (`container=-2, pos=1,
ctype=2, {name}`), reply captured before any client success/failure logic:

```
addr=/status args=[1002, 1237, 0]     # [reqid, newCid=1237, code=0]
```

→ **code 0.** Field 2 = new cid (1237), field 3 = ok-code (0) — exactly as
documented. Pool grew 31→32; entry `cid=1237 posi=1 blck=-1 flow=-1`.

**Test 2 — rapid create/delete ×5** (hypothesis: rapid cycling wedges a
device-side index → code 1):

```
iter 0 pos=1 -> newcid=1238 code=0
iter 1 pos=1 -> newcid=1239 code=0
iter 2 pos=1 -> newcid=1240 code=0
iter 3 pos=1 -> newcid=1241 code=0
iter 4 pos=1 -> newcid=1242 code=0
```

→ all **code 0**. Wedging **not** reproduced.

**Test 3 — full install** (`/CreateContent` → `/SetContentData` real blob →
readback):

```
create pos=1 -> newcid=1243 code=0
SetContentData status: [1020, 0, 0]              # code 0 ok
AFTER install cid=1243 blck=-1 flow=-1           # STILL -1 despite success!
GetContentData: real content returned
```

→ **A successfully installed preset ALSO lists `blck=-1, flow=-1`.** This is the
key correction: `blck`/`flow` are not a validity signal in the pool listing.

**Test 4 — content-size sweep of the 29 suspected "orphan stubs":** every entry
flagged by the old `blck/flow == -1` heuristic returned **11 000–20 000 bytes**
of real `/GetContentData` content, with names matching the user's tone library
(`Tornado of Souls — EC-1000`, `Warm Jazz Clean — Ibanez Prestige`, …). **None
were empty.** They are the user's real presets, not debris.

**Test 5 — live end-to-end through the hardened public path**
(`install_into_pool` of a 29 977-byte fixture): returned a valid cid, readback
= 29 971 bytes, cleaned up, pool restored to 31. No regression.

## Hypotheses, discriminated

- **(a) "code 1 = success-with-caveat" (a second success code we misread):**
  **not adopted.** Unprovable now (anomaly cleared); accepting `code != 0` as
  success has blast radius across every install/save/sync verb and would, if
  wrong, silently keep a bad allocation. The protocol doc still lists only
  `code == 0` as confirmed-OK. We fail loudly on non-zero instead.
- **(b) rapid create/delete cycling wedges the device index:** **not
  reproduced** (Test 2). Cannot be confirmed or ruled out as the 07-14 trigger.
- **(c) protocol misread (status tuple layout differs):** **ruled out.** Field 2
  is the new cid, field 3 the code, exactly as documented (Test 1).
- **Winning explanation:** **transient device/session state on 2026-07-14**,
  resolved by the intervening power-cycle/settle. The visible "orphan stub"
  symptom that made it look worse was the separate `blck/flow` misread (they
  were real presets) compounded by the client discarding the allocated cid.

## The fix (client hardening — safe regardless of root cause)

`src/helixgen/device/client.py`:

- **`_create_content_status(container, pos, name, ctype) -> (cid, code)`** — new;
  returns **both** the allocated cid (field 2) and the code (field 3), so a
  non-zero code no longer throws the cid away. `(None, None)` when no `/status`.
- **`_create_content`** — now a thin wrapper: returns `cid if code == 0 else
  None` (historic happy-path contract preserved; `test_create_content_none_on_nonzero_code`
  still passes).
- **`_delete_created_stub(container, name, pos)`** — new **verify-before-delete**
  helper: re-lists the container and deletes the entry matching **name AND
  `posi == pos`** by its *listed* `cid_` — never the unreliable create-reply cid.
  (This also fixes a latent bug: the old cleanup deleted the create-reply cid,
  which the codebase itself documents as unreliable — it could delete the wrong
  entry or miss the real one.)
- **`_push_to_slot` / `_save_edit_buffer_to`** — on a non-zero create code, call
  `_create_status_error`, which cleans up the orphan stub (verify-before-delete)
  and **raises a `HelixError` naming the code and the allocated cid** so
  callers/users can recover; the message says to power-cycle + retry if it
  persists. On a `SetContentData`/`SavePresetWithCID` failure they now clean up
  via `_delete_created_stub` (by name+pos), not the reply cid.

All three call sites — CLI `device install`/`save`, `setlist_sync`, `hss` import
— already catch `HelixError` (per-tone in the loops, → `ClickException` in the
CLI), so the raise surfaces cleanly and does not abort a batch sync.

**Not changed:** `_create_content`'s `code == 0` success semantics (no evidence
supports accepting `code 1`), and the protocol doc's status-code table beyond a
clarifying note.

Tests: `tests/test_device_client.py` — happy path, create-status-error
raise+cleanup, SetContentData-failure cleanup-by-relist (asserts 777 deleted,
not the reply cid 930), and `_create_content_status` shape. Full suite:
**1501 passed, 65 skipped.**

## Remaining open question

*Why* the device returned `code 1` on 2026-07-14 is still unknown and is not
reproducible on fw 1.3.2/1340 after the power-cycle. If it recurs, capture the
raw `/CreateContent` `/status` frame plus the 2001/2003 streams at that moment
and compare device state (uptime, free storage, pending watched-dir scan). The
client now degrades safely either way: it never orphans a stub and it tells the
user the allocated cid + to power-cycle and retry.
