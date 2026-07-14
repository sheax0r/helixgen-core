# Frida capture checklist (stream 3)

Built from bundle inventory (stream 2) + `docs/helix-protocol.md` + `device-re-findings.md`.
Only functions whose **arg shape is still unknown** after the bundle dump are listed —
the command *names* are known from the binary; capture pins their **argument layout**
and confirms which the app actually emits.

Device at 192.168.4.84. Use an **empty/expendable preset slot** for any write.
Format below matches `tools/re_capture.py` STEPS tuples.

## Session A — Global settings / tempo / global EQ (highest value: the "app-only" surface)

```python
STEPS_A = [
  ("A00_idle", "Baseline: do nothing 5s."),
  ("A01_global_get", "Open any global-settings / preferences panel the app exposes. Watch for /PropertyValueGet bursts over global.* keys (confirms read path)."),
  ("A02_tempo_set", "Open the tempo panel; set BPM to a distinct value like 123. Capture /SetTempo arg shape (and whether global.tempo.bpm PropertyValueSet also fires)."),
  ("A03_timesig_set", "Change time signature (e.g. 6/8). Capture /SetTimeSignature args."),
  ("A04_globaleq_toggle", "Open Global EQ; toggle its bypass and move one band's gain. Capture /GraphEnableSet + how dsp.globaleq.* params are written (ParamValueSet vs PropertyValueSet)."),
  ("A05_pref_change", "In Preferences, change one device-backed pref (e.g. Auto HW Assign, or an FX pedal jack). Capture the /PropertyValueSet [key,value] arg shape — THE key discovery for global settings."),
  ("A06_input_output", "If the app exposes any Ins/Outs / output-level control, change it. Capture PropertyValueSet key + value type (float dB? int?)."),
]
```

Primary unknowns to resolve: **`/PropertyValueSet` arg layout** (key id vs string? value typing), `/SetTempo`, `/SetTimeSignature`, global-EQ write path.

## Session B — Live control / monitoring (tuner, looper, transport, LEDs)

```python
STEPS_B = [
  ("B00_idle", "Baseline 5s."),
  ("B01_tuner_engage", "Engage the tuner (however the app does it). Capture the activation command/property AND watch 2001/2003 for the pitch/cents readout stream."),
  ("B02_tuner_ref", "Change tuner reference pitch (e.g. 440->442). Capture the global.tuner.reference.pitch write."),
  ("B03_tuner_exit", "Exit tuner. Capture the disengage."),
  ("B04_looper_record", "If a looper/Showcase is reachable, press Record then Play then Overdub then Stop. Capture /ActivateLooper + /ExecuteCommand(Looper family) + /Transport* arg shapes and the state stream."),
  ("B05_meters", "Observe input/output meters if shown. Note the 2001/2003 /meter stream addr+shape (currently NOISE-filtered — un-filter for this step)."),
]
```

Primary unknowns: tuner engage command + readout stream shape, looper/transport command args, meter stream schema.

## Session C — Library gaps the in-flight agent may not cover (verify, don't duplicate)

```python
STEPS_C = [
  ("C00_idle", "Baseline 5s."),
  ("C01_create_setlist", "Create a NEW setlist in the app (backlog #8 — currently undiscovered). Capture the /CreateContent (or /AddContentsToContainer) arg shape that makes a setlist container under -5."),
  ("C02_select_active", "Click a preset to make it ACTIVE (not just load-to-edit-buffer). Capture /LoadPresetAtContainerPosition vs /LoadPresetWithCID — is there a distinct active-index set? (backlog #1)."),
  ("C03_reorder_preset", "Drag a preset to a new slot within a setlist. Capture /ReorderContainerContent args."),
  ("C04_reorder_setlist", "Reorder two setlists. Capture the reorder command for setlist containers."),
  ("C05_ir_delete", "Delete a user IR from the IR browser. Capture /RemoveContent on -11 (backlog #11 ir-prune primitive)."),
  ("C06_ir_rename", "Rename a user IR. Capture the rename/SetContentInfo arg shape."),
]
```

Primary unknowns: **create-setlist** command (#8), **active-preset select** (#1), reorder args, IR delete/rename on container -11.

## Notes
- Command *names* are all known (binary strings). Capture is purely to pin **argument order/typing** and confirm which command the app actually uses per action.
- Un-filter `/meter` in `hook_sockets.js`/`re_capture.py` NOISE set only for step B05, then restore.
- If an action emits no 2002 command (pure on-device, e.g. some global settings), record `no-command-observed` — that function may be device-touchscreen-only, and helixgen would reach it via PropertyValueSet regardless.
