# Stadium-app parity — capture session plan (RESUME HERE)

**Status:** ready to run · **Created:** 2026-07-14 · **Owner action required**
(you drive the app; the assistant decodes afterward).

This is a self-contained handoff so a **fresh session started in the main repo
dir** (`~/git/helixgen`) can run the capture and hand the results back for
decoding. No prior context needed beyond this file.

## Goal

Capture — **do not implement** — the OSC argument shapes and telemetry schemas
still unknown after releases 2.20–2.24, so the remaining capture-gated parity
backlog items can be implemented later from recorded evidence. The command
*names* are already known from the app binary; this session pins **argument
layout / value typing / stream schema** and confirms which command the app
actually emits per action. Recordings get committed and referenced from the
backlog items.

## What each step feeds (backlog cross-reference)

| Capture steps | Unknown being pinned | Backlog item |
|---|---|---|
| 10–13 snapshot recall/copy, block bypass, model set | `/ActiveSnapshotIndexSet`, `/CopySnapshot`, `/BlockEnableSet`, `/ModelSet`(+`/ModelEnableSet`) arg shapes | **#19** live device ops |
| 14–15 tempo / time sig | `/SetTempo`, `/SetTimeSignature` args | **#19** (matrix §10) |
| 16–18 tuner engage/exit, meters | tuner engage cmd + 2001/2003 pitch/cents readout schema; `/meter` stream schema | **#19** (matrix §9), tuner |
| 20–22 Global EQ bypass / band / copy-reset | `/GraphEnableSet` + the `dsp.globaleq.*` write path (ParamValueSet vs PropertyValueSet vs other) | **Global EQ follow-up** (matrix §8) |
| 30–35 matrix mixer fader/pan/mute/solo/link/layer/commit | per-output-layer mixer param writes + `/MixerSave` | **#17** Matrix Mixer |
| 40–47 Command Center (all families) + save `ZZCAP-CC` | `commanddefs` families arg shapes, `/ExecuteCommand`/`/CommandTypeSet`, instant-command + EXP-MIDI, and the command-graph **content** layout (decoded from the saved preset) | **#16** Command Center |
| 50–52 MIDI CC/Note controller-assign + XY + save `ZZCAP-CTRL` | incoming-MIDI controller source encoding + XY source shape in `.hsp`/content (decoded from the saved preset) | **#33** MIDI CC/Note, **#34** XY |
| 60 active select | `/LoadPresetAtContainerPosition` vs `/LoadPresetWithCID` — is there a distinct active-index set? | **#1** active-preset select |
| 61–62 reorder preset / setlist | `/ReorderContainerContent` args + setlist-reorder cmd | matrix §1/§2 reorder |
| 63–64 .hss export / import | the `.hss` bundle **file format** (the exported file is half the finding) + its content read/write path | **#31** `.hss` bundles |

Steps 00/05 are baseline + load-an-expendable-preset. A step you perform that
emits **0 frames is itself a finding** (function is device-touchscreen-only, or
uses a path we already have) — the script records the count either way.

## Prerequisites (fresh session)

1. **Start the session in the main repo dir:** `cd ~/git/helixgen` (NOT a
   worktree — this session's weirdness came from being anchored in the stale
   `.claude/worktrees/stadium-app-parity` worktree).
2. **Helix Stadium app** open and **connected** to the device (`192.168.4.84`).
3. **frida** available (`python3 -c "import frida"` — v17 already present on
   this machine; else `pip install -r tools/requirements-re.txt`).
4. An **expendable preset** loaded (throwaway setlist or a slot you don't mind
   editing) for the live-edit steps.
5. ~20 minutes.

## Run it

```bash
python3 tools/re_capture_parity.py
```

The script (already committed at `tools/re_capture_parity.py`) attaches frida to
the running app, walks you through ~34 labelled steps, and tags every captured
OSC frame with the active step. Press **ENTER** after doing each action; press
ENTER with nothing done to **skip** a step the app doesn't expose. Output:
`captures/parity_capture_<epoch>.jsonl` (raw payloads base64, one row per
non-noise frame). Noise (`/dspEvent /trigger /heartbeat /meter`) is filtered
**except** during the tuner/meter steps, where that telemetry is the finding.

### Distinct values to use (make the argument bytes unambiguous)

- Tempo **123 BPM**, time sig **6/8**
- Command Center MIDI: **CC#74, value 64, channel 5**; **PC 12**; a **Note**;
  EXP → **CC#11, ch 5**; **Snapshot 2** on a footswitch; **instant command 1**
- Controller-assign MIDI: block bypass ← **CC#80 ch 3**, param ← **CC#81 ch 3**
- Save the two capture presets **exactly** as `ZZCAP-CC` and `ZZCAP-CTRL` in
  empty slots (the `ZZCAP-` prefix = safe-to-delete; note their slots).

## When it finishes — hand back to the assistant

Tell the (fresh) assistant:
1. the **capture file path** the script prints
   (`captures/parity_capture_*.jsonl`);
2. the **`.hss` filename** you exported to the Desktop (and keep the file);
3. that **`ZZCAP-CC` and `ZZCAP-CTRL`** were saved, and **their slots**;
4. any steps you **skipped** (or that showed 0 frames).

## What the assistant does next (decode + record, NO implementation)

1. Decode the JSONL per step (`tools/osc.py` unpacks the OSC/msgpack payloads),
   attributing each argument layout to its action.
2. Non-activating-pull the device content of `ZZCAP-CC` / `ZZCAP-CTRL`
   (`helixgen device pull` / `get_content`) to read the command-graph and
   controller-source **content** shapes; reverse the `.hss` file bytes.
3. Write curated, per-feature findings docs under **`docs/captures/`** (one per
   backlog item: what command, exact arg layout, value typing, examples,
   open questions), commit them, and add a "capture recorded: docs/captures/…"
   line to each backlog item (#1, #16, #17, #19, #31, #33, #34, Global EQ).
4. **Clean up:** delete `ZZCAP-CC` / `ZZCAP-CTRL` from the device (they're
   `ZZCAP-` prefixed = safe), and the imported duplicate `.hss` setlist if you
   made one. Then implementation happens later, per item, in its own PR.

## Notes / gotchas

- Captures dir (`captures/`) is **gitignored**; the raw JSONL stays local, but
  the **decoded findings docs** in `docs/captures/` are committed (they carry
  no secrets — just protocol arg shapes). If a raw capture is worth keeping,
  copy the relevant decoded frames into the findings doc, don't commit the
  JSONL.
- The protocol is **cleartext OSC-over-ZeroMQ**; a `tcpdump`/`tshark` of ports
  2001–2003 is an alternative to frida if the app hooks ever break — but frida
  gives per-action attribution the packet capture can't.
- This does **device writes** (live edits, saves). All confined to an
  expendable preset + `ZZCAP-`-prefixed saves; never touches other presets.
