# remove-block (`clrb`) reverse-engineering + ship plan

**Date:** 2026-11-22
**Status:** design-ready → BLOCKED on a safe rig window (see Preconditions)
**Context:** the last unshipped 81v.1 editor feature. `clrb` (clear/remove a
block) exists in `agenda.py` but a standalone `clrb` agenda did **not** apply on
hardware; the op is undecoded and "likely needs a `/setSelectedBlock`
prerequisite" whose wire format is NOT recorded (helix-protocol.md:560 lists
`/setSelectedBlock` only as observed UI-selection chatter, content undecoded).

## Why it is blocked right now (2026-11-22)

The rig IS reachable (`p35x1.local` / 192.168.4.84, 2001–2003 open), but:
1. **`clrb` is undecoded** — its exact trigger/behaviour is unknown, so an
   experiment can misbehave in ways I can't predict.
2. **The edit buffer is DIRTY** — `volatile.preset.edited == 1`; the user has
   unsaved edits (preset "Avenged Sevenfold – Hail to the King"). The only
   whole-buffer restore is a preset RELOAD, which would DISCARD those edits —
   there is no live whole-buffer SET verb to put the buffer back.
3. **remove-block is itself unsolved**, so a test block I add cannot be
   guaranteed-removed if `clrb` fails — on a dirty buffer that stray block is
   unrecoverable by me.

Together these remove every restore path. HW experimentation is safe ONLY on a
clean buffer (then a preset reload restores from anything).

## Preconditions to execute (any one unblocks)

- **Clean rig + go-ahead:** user saves or discards the unsaved edits
  (`edited == 0`), then authorises the experiment. A preset reload is then a
  perfect restore, so a throwaway test block is fully recoverable.
- **OR Frida capture + go-ahead:** user grants computer-use so I drive the
  Edit app's delete-block while the validated Frida harness
  (`frida_run.py` + `tools/hook_sockets.js`, USB/IPv6) captures the real op
  sequence (the thorough route; decodes `/setSelectedBlock`'s args directly).

## Experiment protocol (clean-buffer route)

1. Read + record the full edit buffer (reference). Confirm `edited == 0`.
2. Add a throwaway block at a FREE, non-routed lane-A slot (DSP 0 has 9/10/12
   free) via `set_block_model` — never touch existing/ routed blocks.
3. Try, re-reading `blocks` after each to see if the throwaway cleared:
   a. `do_agenda([agenda.clear(slot, flow)])` alone (expected: no-op).
   b. a new `/setSelectedBlock` RPC (try arg shapes `[dsp, grid_slot]`,
      `[grid_slot]`, `[reqid, dsp, grid_slot]`) THEN the `clrb` agenda.
   c. `clrb` as a granular RPC rather than an agenda, if (a)/(b) fail.
4. On the first variant that clears the slot: that is the decoded sequence.
5. Cleanup: remove the throwaway with the decoded sequence; if nothing worked,
   `load_preset(active_cid)` to wipe the buffer back to saved (safe BECAUSE the
   buffer was clean — the precondition).

## Ship (after decode)

Mirrors the sibling structural increments, all already built/reviewed:
- `client.remove_block(path, slot)`: `/setSelectedBlock` + `clrb` (decoded),
  trust the 2001 echo, verify from a re-read that the slot cleared → else raise.
  Guard the fixed I/O slots. HW-validate on DSP 0 (+ note DSP 1 like move_block).
- `LiveSession.remove_block(path, block)`: read-validate-write-verify under the
  devlock (the move_block/second_cab shape) — reject endpoints/routing, 400 on
  guard, 502 + offline on transport, verify the slot is empty after.
- `server.py`: `POST /remove-block {path, block}`.
- `page.html`: a "✕" header button on a removable block, through `structSend`.
- tests: delegate+reread, endpoint/routing reject, did-not-apply 502, transport
  502; `FakeClient.remove_block` records + mutates the buffer so the verify is
  real.

## Test
The session/route/UI/tests above are hypothesis-independent (they exercise
delegation, guards and error mapping against a fake), so they can be written
before the HW window; only `client.remove_block`'s exact bytes and the HW
validation wait on the decode.
