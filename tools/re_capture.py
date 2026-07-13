#!/usr/bin/env python3
"""Interactive Helix RE capture — attaches Frida to the running Helix Stadium
app, records OSC traffic (full payloads) tagged per action step, and walks you
through a checklist. Between steps you press ENTER, which advances the label so
every captured frame is attributed to the action you were doing.

Goals of this session:
  1. Find a NON-ACTIVATING content read (HX Edit exporting a preset it did not
     load) — for the backup/pull non-activating-read feature.
  2. See whether an ACTIVE-PRESET query exists.
  3. Capture FOOTSWITCH / EXP assignment commands — to pin the .hsp source-id
     -> device srcs.locl/ctxt mapping for controller synthesis.

Usage:
    pip install frida          # one-time, if not already installed
    python3 tools/re_capture.py

Output: captures/re_capture_<epoch>.jsonl  (one JSON object per non-noise frame;
raw payload base64 in "b64"; step label in "step"). Tell me the path + the
name/slot of the preset you save in the last step and I'll decode it.
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

# High-volume telemetry we never care about here — dropped so the capture stays
# readable and the interesting 2002 RPC traffic isn't buried.
NOISE = {"/dspEvent", "/trigger", "/heartbeat", "/meter"}

# The action checklist. Each entry: (step_id, instruction shown to you).
# Do the action, THEN press ENTER — frames arriving while the step is active are
# tagged with its id. HX Edit menu labels vary slightly by version; the intent
# is what matters, adapt the exact clicks.
STEPS = [
    ("00_idle",
     "Baseline. Do NOTHING for ~5 seconds (captures idle chatter / any "
     "background active-preset poll)."),
    ("01_export_no_load",
     "NON-ACTIVATING READ. Without loading it, EXPORT a preset that is NOT the "
     "currently-active one: right-click a DIFFERENT preset in the setlist and "
     "choose Export / 'Save Preset As…', or drag that preset to the Desktop. "
     "Do NOT left-click it first (that loads it)."),
    ("02_load_for_contrast",
     "CONTRAST. Now LEFT-CLICK that same preset to LOAD it (make it active). "
     "This shows what activation looks like, so I can tell it apart from the "
     "pure read in step 1."),
    ("03_assign_fs5",
     "FOOTSWITCH 5. On the active preset, assign FOOTSWITCH 5 (top row, 5th "
     "switch) to toggle a specific block's BYPASS — e.g. right-click an "
     "overdrive/amp block → Assign Controller → Footswitch 5 (or use the "
     "Bypass/Controller Assign view). Remember which block."),
    ("04_assign_fs7",
     "FOOTSWITCH 7. Assign FOOTSWITCH 7 (bottom row, leftmost) to a DIFFERENT "
     "block's BYPASS. (A second data point pins the FS#→locl rule.)"),
    ("05_assign_exp1",
     "EXP 1. Assign EXP Pedal 1 to sweep a PARAMETER — e.g. an amp Volume or a "
     "delay Mix."),
    ("06_save",
     "SAVE. Save the preset to its slot so the assignments persist (so I can "
     "pull + decode it). NOTE its name and slot to tell me afterward."),
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
    out_path = os.path.join(CAPDIR, "re_capture_%d.jsonl" % int(time.time()))
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
        if addr in NOISE:
            return
        rec = {
            "ts": time.time(), "step": state["step"], "dir": p.get("dir"),
            "port": p.get("port"), "addr": addr, "len": p.get("len"),
            "b64": base64.b64encode(data).decode() if data else "",
        }
        with lock:
            outf.write(json.dumps(rec) + "\n")
            counts[state["step"]] = counts.get(state["step"], 0) + 1
        # live one-liner so you can see traffic landing
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
    print("Per-step frame counts:")
    for step_id, _ in STEPS:
        print("   %-22s %d" % (step_id, counts.get(step_id, 0)))
    print("\nTell me this file path and the name/slot of the preset you saved "
          "in step 06, and I'll decode it.")


if __name__ == "__main__":
    main()
