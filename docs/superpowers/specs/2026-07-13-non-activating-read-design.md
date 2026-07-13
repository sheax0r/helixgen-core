# Non-activating device content read — design spec

Status: **approved design, building** (2026-07-13). Needs the shared device-RE session.

## 1. Problem (§4a of the transcoder design spec)

Reading a preset's content on the device is a two-step, **inherently activating** sequence: `client.load_preset(cid)` (`/LoadPresetWithCID` — makes the preset the active edit buffer) + `client.get_edit_buffer()` (`/EditBufferStateGet` — reads *only* the current edit buffer). So `backup`, `pull`, `sync`'s reorder Phase A, and fixture capture all **change the musician's active tone** as a side effect.

Confirmed facts (research):
- **No non-activating GET command** is known — no `/GetContentData`/`/getContent`/read-counterpart to `/SetContentData` in code or `docs/helix-protocol.md`. Tracked as backlog #13.
- **No active-preset query** — the device is silent on 2001 when idle; no property helper; `backup.py` today restores to `presets[0]` (slot 0), *not* the truly-active preset.
- Write path (install/sync) is already non-activating (`CreateContent`+`SetContentData`). `pull-ir` (SFTP) is unaffected.

## 2. Approach: RE the real GET, with a save-and-restore fallback

The user chose to **capture the real GET**. So the primary path is RE; a deterministic save-and-restore is the documented fallback if no such command exists.

## 3. Device-RE session (shared with the controllers spec)

**Goal 1 — find a non-activating content read.** Capture what HX Edit sends when it **exports / backs up a preset that is not the active one** (drag-to-desktop / "Export"). Look on port 2002 for a request that returns a `_sbepgsm` blob **without** a preceding `/LoadPresetWithCID`.
**Goal 2 — find an active-preset query.** Probe `/PropertyValueGet` for a property key yielding the active preset cid, and inspect the 2001 connect-time burst.
**Goal 3 (controllers spec)** — pin FS/EXP `locl`/`ctxt` (piggybacks the same session).

**Mechanics:** `tcpdump` on the LAN interface for tcp 2001–2003 (needs sudo — user runs it via `!` if prompted), or the repo's Frida tooling (`tools/frida_run.py`, `tools/frida_sftp_raw.py`). Drive HX Edit via computer-use where reliable; hand specific clicks to the user otherwise. Decode captured frames with the existing `osc`/`content` codecs. Deliverable: the exact OSC address + arg shape of the GET (and/or active-preset property), documented in `docs/helix-protocol.md`.

## 4. Design

### If a non-activating GET exists (primary)
- Add `client.get_content(cid) -> bytes` (send the captured GET, return the `_sbepgsm` blob) — a true non-activating read.
- Repoint all content reads: `backup.py::backup_setlist` (the per-preset loop), `cli.py` `device pull`, `cli.py` sync reorder Phase A. None of them call `load_preset` any more; the active tone is never disturbed.
- Fixture capture / any investigation script can use it too.

### Fallback — save-and-restore (if no GET is found)
- At connect (before any read), `get_edit_buffer()` returns the **active** blob without loading anything. Record it.
- Recover the active cid: via the captured active-preset property if Goal 2 succeeded; else **content-match** — after reading presets during the run, restore the cid whose content equals the recorded active blob (works for `backup`, which reads the whole setlist; for single `pull`, restore-by-match if the active blob is among reads, else best-effort).
- Restore once at the **end** (`load_preset(active_cid)`), matching `backup.py`'s existing once-at-end shape.
- **Documented limitation:** an *unsaved dirty edit buffer* (active state equal to no stored cid) cannot be restored via the current write surface (only `load_preset` by cid and `SetContentData` into a stored cid exist) — the run would reload the nearest stored preset and lose in-progress edits. Warn the user when the active blob matches no stored preset.

## 5. Testing
- **Hardware:** note the active preset, run `device backup <setlist>` and `device pull <cid>`, confirm the **same preset is still active** afterward (and, with the GET path, that nothing was loaded at all — verify via a 2001 watch showing no `/setEditBuffer` during the read).
- **Unit:** mock the client; assert `backup_setlist`/`pull` call the non-activating `get_content` (GET path) and never `load_preset`; or (fallback path) assert the once-at-end restore targets the recorded active cid, and that an unmatched active blob triggers the documented warning.

## 6. Rollout
1. RE session → GET command (or confirm none exists).
2. Implement GET path if found; else the save-and-restore fallback.
3. Repoint `backup`/`pull`/sync-Phase-A; hardware-validate active-tone preservation.
4. Land (independent worktree — `backup.py`/`client.py`/`cli.py`, no `transcode.py` overlap), then release with the others.
