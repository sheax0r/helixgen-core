Here's a handoff brief for Claude Code:

Helix Preset Generator — Project Brief
Goal
Build a tool that programmatically generates Line 6 Helix .hlx preset files from high-level tone descriptions (e.g., "Goldfinger 'Superman' rhythm tone").
File Format Essentials
.hlx files are JSON text with this top-level structure:
json{
  "version": 6,
  "schema": "L6Preset",
  "data": {
    "device": {...},
    "meta": {"name": "Preset Name", ...},
    "tone": {
      "dsp0": { "blocks": {...} },
      "dsp1": { "blocks": {...} }
    }
  }
}
Helix has two DSP paths (dsp0, dsp1). Each contains blocks for amp/cab/effects with model-specific parameter keys.
Critical gotcha: Line 6 doesn't publish a schema. Internal model IDs (e.g., for "Brit 2204") and parameter names must be extracted from real exported presets — they cannot be guessed.
Recommended Architecture
Template + mutation pattern, NOT generation from scratch:

Export a working preset from Helix/Helix Native with the desired block layout
Parse it as JSON, identify parameter keys per block
Load template → swap parameter values → update meta.name → write new .hlx
Validate by importing into HX Edit

Prior Art (clone/reference these)

sensorium/phelix (https://github.com/sensorium/phelix) — has a blocks/ folder of pre-extracted block JSON exported via HXedit. Use this as a parts library.
HackLabsGuitar/helix-py-api (https://github.com/HackLabsGuitar/helix-py-api) — fuller Python API for Helix files.
dbagchee/helix-preset-viewer (https://github.com/dbagchee/helix-preset-viewer) — web viewer for understanding structure.
frankdeath/hx-tools (https://github.com/frankdeath/hx-tools) — parsing scripts. ⚠️ Author warns its outputs differ subtly from HX Edit and may not load reliably.
AntonyCorbett/HelixBackupFiles (https://github.com/AntonyCorbett/HelixBackupFiles) — for .hxb backup files (zlib-compressed, contains JSON + WAV IRs).

File Extensions Reference

.hlx — single preset (JSON)
.hls — set list
.hlb — bundle (multiple set lists)
.hxb — full backup (binary, zlib-compressed JSON + IRs)

Reference Tone Spec (test case)
Use this as the first preset to generate — "Goldfinger 'Superman' rhythm" for a Strandberg Boden Essential 6 (bright/tight humbuckers, 25.5" scale):
Signal chain: Gate → Scream 808 → Brit 2204 → 4x12 Greenback 25 (57 mic) → EQ → Plate Reverb
Block parameters:
BlockParameterValueNoise GateThreshold-52 dBNoise GateDecay30 msScream 808Drive1Scream 808Tone0.5 (noon)Scream 808Level6Brit 2204Drive6Brit 2204Bass5Brit 2204Mid7.5Brit 2204Treble5.5Brit 2204Presence5.5Brit 2204Master6Brit 2204Ch Vol54x12 Greenback 25Mic57 Dynamic4x12 Greenback 25Distance1"4x12 Greenback 25Axis12° off4x12 Greenback 25High Cut8000 Hz4x12 Greenback 25Low Cut80 HzParametric EQ350 Hz-2 dBParametric EQ2800 Hz+2 dBParametric EQ7500 Hz-2 dBPlate ReverbMix10%Plate ReverbDecay1.2 sPlate ReverbPre-delay10 ms
Note: actual parameter key names and value scales (e.g., 0–1 normalized vs. 0–10 display) must be confirmed from real exported preset JSON.
Stretch Goals

High-level DSL: Preset(amp="Brit 2204", drive=6, mids=7.5, ...) → .hlx
Tone library by genre/artist with parameterized variations
A/B comparison: load two presets, diff parameters
Snapshot variation generation (rhythm/lead from same base tone)

Known Risks

Model IDs may change between Helix firmware versions
Output won't be byte-identical to HX Edit; aim for "loads correctly" not "diff-clean"
Save your existing presets before testing imports — corrupted slots are annoying
Bricking the device is unlikely but theoretically possible

First Steps for Claude Code

Clone sensorium/phelix for the block library
Have user export a baseline .hlx from their Helix as a working template
Parse and pretty-print the JSON to understand actual parameter keys
Build minimal template_loader.py that loads, mutates one parameter, saves, and verifies it loads in HX Edit
Iterate from there


Good luck — should be a fun build. The JSON format being human-readable is the win; the lack of official schema is the wall to climb.