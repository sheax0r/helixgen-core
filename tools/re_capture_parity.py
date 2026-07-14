#!/usr/bin/env python3
"""Parity-gap capture session — records the OSC argument shapes still unknown
after releases 2.20–2.24, one tagged step per app action.

Same harness as tools/re_capture.py (frida + hook_sockets.js, per-step frame
tagging); different checklist. Command NAMES are all known from the app binary
— this session pins ARGUMENT layouts and telemetry schemas for:

  backlog #16 Command Center · #17 Matrix Mixer · #19 live device ops
  (snapshots/bypass/model/tempo/tuner) · Global EQ write path · #1 active
  select · reorder args · #31 .hss bundle · #33 MIDI CC/Note · #34 XY

Prep (before running):
  * Helix Stadium app OPEN and CONNECTED to the device (192.168.4.84).
  * Load an EXPENDABLE preset for the live-edit steps (or be ready to discard).
  * ~20 minutes.

Usage:  python3 tools/re_capture_parity.py
Output: captures/parity_capture_<epoch>.jsonl
Skip any step the app doesn't expose with ENTER (0 frames is itself a finding
— record it; the function may be device-screen-only).
"""
import base64
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "hook_sockets.js")
CAPDIR = os.path.join(HERE, "..", "captures")

# High-volume telemetry dropped by default…
NOISE = {"/dspEvent", "/trigger", "/heartbeat", "/meter"}
# …EXCEPT during these steps, where the telemetry IS the finding.
UNFILTERED_STEPS = {"16_tuner_engage", "17_tuner_exit", "18_meters"}

STEPS = [
    ("00_idle",
     "Baseline. Do NOTHING for ~5 seconds."),

    # ---- live device ops (#19) — use an EXPENDABLE preset ------------------
    ("05_load_throwaway",
     "Load an EXPENDABLE preset (throwaway setlist or a slot you don't care "
     "about) so the live-edit steps below can't hurt anything."),
    ("10_snapshot_recall",
     "SNAPSHOTS: recall snapshot 2, then 3, then back to 1 (the snapshot "
     "buttons/dropdown). Pins /ActiveSnapshotIndexSet."),
    ("11_snapshot_copy",
     "Copy one snapshot onto another (right-click / snapshot menu → Copy, "
     "Paste). Pins /CopySnapshot."),
    ("12_block_bypass",
     "Click a block to BYPASS it live, then un-bypass it. Pins "
     "/BlockEnableSet."),
    ("13_model_set",
     "Replace one block's MODEL live from the model list (e.g. swap one drive "
     "for another). Pins /ModelSet (+ /ModelEnableSet). You can undo after."),
    ("14_tempo",
     "Open the tempo panel; set BPM to exactly 123. Pins /SetTempo."),
    ("15_timesig",
     "Change the time signature (e.g. to 6/8). Pins /SetTimeSignature."),
    ("16_tuner_engage",
     "TUNER: engage the tuner (noise filter is OFF for this step). Pluck a "
     "string a few times so the pitch/cents readout streams. Pins the engage "
     "command + the 2001/2003 readout schema."),
    ("17_tuner_exit",
     "Exit the tuner (filter still off — catches the disengage)."),
    ("18_meters",
     "METERS: if the app shows input/output meters, watch them ~5 s while "
     "playing (filter off — catches the /meter stream schema). Else ENTER."),

    # ---- Global EQ ----------------------------------------------------------
    ("20_globaleq_bypass",
     "GLOBAL EQ: open the Global EQ view; toggle its bypass off/on. Pins "
     "/GraphEnableSet."),
    ("21_globaleq_band",
     "Move ONE band's gain, then its frequency, on the 1/4\" EQ. Pins the "
     "dsp.globaleq.* write path (ParamValueSet vs PropertyValueSet vs other)."),
    ("22_globaleq_copy_reset",
     "If offered: copy the 1/4\" EQ to XLR, then reset one EQ to flat. Else "
     "ENTER."),

    # ---- Matrix Mixer (#17) -------------------------------------------------
    ("30_mixer_open",
     "MATRIX MIXER: open it (device Main Volume / mixer view). Captures any "
     "read/subscribe it does on open."),
    ("31_mixer_fader",
     "Move ONE path fader on the 1/4\" output layer to a distinct value."),
    ("32_mixer_pan_mute_solo",
     "On that same layer: pan something, then MUTE it, then SOLO it (and "
     "un-solo/un-mute)."),
    ("33_mixer_link",
     "Link (or unlink) two outputs if the app offers output linking."),
    ("34_mixer_layer",
     "Switch to the XLR (or Phones) layer and move one fader there."),
    ("35_mixer_commit",
     "Close/commit the mixer (however it persists — watch for /MixerSave)."),

    # ---- Command Center (#16) — still on the expendable preset -------------
    ("40_cc_open",
     "COMMAND CENTER: open the Command Center view."),
    ("41_cc_preset_snapshot",
     "Assign a footswitch to a PRESET/SNAPSHOT command (e.g. FS3 → Snapshot 2)."),
    ("42_cc_midi_cc",
     "Assign a footswitch to MIDI CC — pick distinct values: CC#74, value 64, "
     "MIDI channel 5."),
    ("43_cc_midi_pc_note",
     "Assign MIDI PC (program 12) to one switch, and MIDI NOTE (e.g. C3) to "
     "another (MMC too if quick)."),
    ("44_cc_hotkey_utility",
     "Assign a HOTKEY command to one switch and a UTILITY command to another."),
    ("45_cc_instant",
     "Set INSTANT command 1 to anything (fires on preset load)."),
    ("46_cc_exp_midi",
     "Give an EXP pedal a MIDI CC assignment (EXP → MIDI CC#11, ch 5)."),
    ("47_cc_save",
     "SAVE the preset AS a new preset named exactly 'ZZCAP-CC' in an empty "
     "slot (captures the command-graph content write; I'll pull + decode it)."),

    # ---- controller shapes helixgen lacks (#33/#34) -------------------------
    ("50_midi_ctrl_assign",
     "CONTROLLER ASSIGN (not Command Center): set a BLOCK BYPASS to be "
     "controlled by incoming MIDI CC#80 ch 3, and a PARAM (e.g. delay Mix) by "
     "MIDI CC#81 ch 3."),
    ("51_xy_assign",
     "XY controller: if the app exposes XY assignment on Stadium, assign X to "
     "one param and Y to another. Else ENTER (0 frames = finding)."),
    ("52_save_ctrl_preset",
     "SAVE AS a new preset named exactly 'ZZCAP-CTRL' in an empty slot."),

    # ---- library odds (#1, reorder, .hss) -----------------------------------
    ("60_active_select",
     "In the setlist sidebar, click a preset to make it the ACTIVE preset on "
     "the hardware (not just edit-buffer load). Pins "
     "/LoadPresetAtContainerPosition vs /LoadPresetWithCID (#1)."),
    ("61_reorder_preset",
     "Drag ONE preset to a different slot within the same setlist. Pins "
     "/ReorderContainerContent. (Drag it back after if you care.)"),
    ("62_reorder_setlist",
     "Drag ONE setlist to reorder it among setlists (drag back after)."),
    ("63_hss_export",
     ".hss EXPORT: File/export the 'throwaway' (or any small) setlist as a "
     ".hss to your Desktop. Note the filename. (Captures its content reads; "
     "the FILE is the other half of the finding — don't delete it.)"),
    ("64_hss_import",
     ".hss IMPORT: import that same .hss back (as a new setlist). Captures "
     "the write side. Delete the imported duplicate setlist after if you like."),
]


def _load_frida():
    try:
        import frida  # noqa: F401
    except ImportError:
        sys.exit("frida not installed — run:  pip install frida   (then re-run)")
    return __import__("frida")


def _find_helix(frida):
    for p in frida.get_local_device().enumerate_processes():
        if p.name.startswith("Helix"):
            return p.pid, p.name
    sys.exit("Helix process not found — is the Helix Stadium app running?")


def main():
    frida = _load_frida()
    pid, name = _find_helix(frida)
    os.makedirs(CAPDIR, exist_ok=True)
    out_path = os.path.join(CAPDIR, "parity_capture_%d.jsonl" % int(time.time()))
    outf = open(out_path, "a", buffering=1)

    state = {"step": "boot"}
    counts = {}
    lock = threading.Lock()

    def on_message(msg, data):
        if msg.get("type") == "error":
            print("  [frida error]", json.dumps(msg.get("description", msg)))
            return
        if msg.get("type") != "send":
            return
        p = msg["payload"]
        if p.get("dir") == "INFO":
            print("  [info]", p.get("msg"))
            return
        addr = p.get("addr", "?")
        if addr in NOISE and state["step"] not in UNFILTERED_STEPS:
            return
        rec = {
            "ts": time.time(), "step": state["step"], "dir": p.get("dir"),
            "port": p.get("port"), "addr": addr, "len": p.get("len"),
            "b64": base64.b64encode(data).decode() if data else "",
        }
        with lock:
            outf.write(json.dumps(rec) + "\n")
            counts[state["step"]] = counts.get(state["step"], 0) + 1
        print("    %-4s %-6s %-28s len=%s" % (
            rec["dir"], rec["port"], addr, rec["len"]))

    print("Attaching to %r (pid %d)…" % (name, pid))
    session = frida.attach(pid)
    with open(SCRIPT) as f:
        script = session.create_script(f.read())
    script.on("message", on_message)
    script.load()
    print("Hooks loaded. Writing to: %s\n" % out_path)
    time.sleep(0.5)

    try:
        for step_id, instruction in STEPS:
            state["step"] = step_id
            print("\n" + "=" * 72)
            print("STEP %s" % step_id)
            print("-" * 72)
            print(instruction)
            input("\n>>> Do it, then press ENTER to continue… ")
            print("   (captured %d frames in %s)" % (counts.get(step_id, 0), step_id))
        state["step"] = "done"
    except (KeyboardInterrupt, EOFError):
        print("\n(interrupted — partial capture saved)")
    finally:
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
        outf.close()

    print("\n" + "=" * 72)
    print("Capture complete → %s" % out_path)
    print("Per-step frame counts (0 on a step you performed = a finding too):")
    for step_id, _ in STEPS:
        print("   %-24s %d" % (step_id, counts.get(step_id, 0)))
    print("\nTell the assistant: this file path, the .hss filename on your "
          "Desktop, and that ZZCAP-CC / ZZCAP-CTRL were saved (+ their slots).")


if __name__ == "__main__":
    main()
