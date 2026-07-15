# Loudness Measure (phases 0–1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the loudness spec's phase 0 findings + phase 1 `device measure` verb, including the live-ops wire-index bugfix the characterization uncovered.

**Architecture:** Pure decoder/summarizer modules (`meters.py` additions, new `measure.py`) unit-tested on synthetic `/dspEvent` payloads, wired to a thin CLI verb + MCP tool that subscribe to 2003. The live-ops coordinate fix converts public `(path, blks_key)` coords to the wire's `(key-1)/2` space inside `HelixClient`.

**Tech Stack:** Python stdlib + click; pyzmq/msgpack via the existing lazy `device` extra; pytest.

## Global Constraints

- Pure stdlib + `click` at core; pyzmq+msgpack only via existing lazy device imports.
- Run tests with `PYTHONPATH=$PWD/src python -m pytest`.
- TDD: failing test first, then minimal implementation.
- Agent-facing surfaces ship in sync: `docs/CLI.md`, `docs/helix-protocol.md`, MCP tool descriptions in `mcp_server/`.
- Public block coordinates everywhere remain the `device blocks` / `sfg_.flow[dsp].blks` position keys (odd ints); ONLY the wire encoding changes.

## Hardware facts this plan encodes (measured 2026-07-14, Stadium XL)

- `/BlockEnableSet` and `/ParamValueSet` (and by capture-example inference `/ModelSet`) address blocks by **wire index = (blks_key − 1) / 2**. Amp at blks key 7 = wire 3: bypass at wire 3 collapsed downstream meters −33 dB; at key 7 it toggled `P35_OutputNone` (silent no-op with a healthy-looking echo). `/ParamValueSet` at a blks key returns no ack (`set_param` → False); at the wire index it acks + echoes `/setParamValue [.., 0, 5, 0, 2, -60.0]`.
- Param values ride the wire in **raw units** (dB −60.0 accepted verbatim on output `gain` pid 2).
- Grid meters: **~10 Hz per mid**, linear amplitude envelopes (>1.0 legal). mid 796 = path chain nodes (cells 0–1 = instrument input; 8–9 == 26–27 = chain out). mid 800 populated cluster = output-send stereo pairs, value == chain out. **All taps are upstream of the output block's `gain`** (a landed −60 dB moved nothing).
- Hum defeats input-level gating (hum 0.01–0.07 overlaps playing 0.0005–1.0) but **not** pitch gating (hum reads `-1.0` no-pitch).

---

### Task 1: Live-ops wire-index fix in `HelixClient`

**Files:**
- Modify: `src/helixgen/device/client.py` (set_block_enable ~:707, set_block_model ~:718, set_param ~:873)
- Test: `tests/test_device_client_wirecoords.py` (new)

**Interfaces:**
- Produces: `_wire_block(blks_key:int) -> int` (module-level helper, raises `HelixError` on non-positive/even key); `set_block_enable/set_block_model/set_param` keep their signatures, convert internally.

- [x] **Step 1: Write the failing tests** — monkeypatch `HelixClient._rpc` to capture `(addr, args)`; assert `set_block_enable(0, 7, False)` sends block `3`, `set_param(0, 11, 2, -6.0)` sends block `5`, `set_block_model(0, 9, 468)` sends block `4`; assert even/zero keys raise `HelixError` mentioning `device blocks`.
- [x] **Step 2: Run to verify FAIL** (`pytest tests/test_device_client_wirecoords.py -v`).
- [x] **Step 3: Implement `_wire_block` + call it from the three verbs; update their docstrings** (public coords unchanged = blks keys).
- [x] **Step 4: Run to verify PASS + full suite.**
- [x] **Step 5: Commit** `fix(device): live-ops verbs translate blks keys to the wire's (key-1)/2 block index`.

### Task 2: Chain-level extraction in `meters.py`

**Files:**
- Modify: `src/helixgen/device/meters.py`
- Test: `tests/test_device_meters.py` (extend)

**Interfaces:**
- Produces: `input_level(reading: MeterReading) -> float` (mid-796 only: `max(values[0:2])`, else 0.0); `output_level(reading: MeterReading) -> float` (mid-800 only: median of cells > 1e-6, 0.0 if none); `to_db(v: float, floor_db: float = -140.0) -> float`.

- [x] **Step 1: Failing tests** — synthetic `MeterReading`s: 796 with cells 0–1 = 0.02/0.021 → `input_level == 0.021`; 800 with six pairs ~0.5 → `output_level == median`; all-zero 800 → 0.0; wrong-mid calls → 0.0; `to_db(1.0) == 0.0`, `to_db(0.5) ≈ -6.02`, `to_db(0.0) == -140.0`.
- [x] **Step 2: FAIL. Step 3: implement. Step 4: PASS + suite. Step 5: Commit** `feat(device): meters chain-level extraction (input/output cells, dB)`.

### Task 3: `measure.py` — playing-gated loudness summarizer

**Files:**
- Create: `src/helixgen/device/measure.py`
- Test: `tests/test_device_measure.py` (new)

**Interfaces:**
- Produces:
  - `MeasureSample(NamedTuple)`: `t: float, input_level: float, output_level: float, pitch: Optional[float]`
  - `samples_from_events(events) -> Iterator[MeasureSample]` — consumes subscriber `Event`s; keeps last-seen pitch (tuner) + last mid-796 input; **emits one sample per mid-800 reading** stamped with `event.t` fallback `time.time()`.
  - `is_playing(s: MeasureSample, input_floor: float = 1e-4) -> bool` — `s.pitch is not None and s.pitch >= 0.0 and s.input_level > input_floor`.
  - `summarize(samples, seconds: float, min_playing: int = 40) -> MeasureResult` — `MeasureResult(NamedTuple)`: `seconds, n_samples, n_playing, playing_seconds (n_playing/10.0), input_db, output_db, output_db_p75, gain_db, ok, reason`. Medians over playing samples in dB (`to_db`), `gain_db = median(to_db(out) - to_db(inp))` per sample; `ok=False` + reason `"not enough playing (<N samples)"` under threshold; empty → reason `"no meter data"`.
- Consumes: Task 2's `input_level/output_level/to_db`; `tuner.reading_from_event_args`; `meters.readings_from_event_args`.

- [x] **Step 1: Failing tests** — synthetic event-arg dicts in the tuner/meters golden shape: interleaved pitch(40.0)+796+800 events → samples pair correctly; hum case (pitch −1.0) gated out; `summarize` on 60 playing samples of in=0.02/out=0.5 → `gain_db ≈ 27.96`, `ok=True`; 5 playing samples → `ok=False`.
- [x] **Step 2: FAIL. Step 3: implement. Step 4: PASS + suite. Step 5: Commit** `feat(device): playing-gated loudness measurement summarizer`.

### Task 4: CLI `device measure` + MCP `device_measure`

**Files:**
- Modify: `src/helixgen/cli_device.py` (next to `device_meters` ~:2124), `mcp_server/tools.py` (next to `device_meters_handler` ~:1485), `mcp_server/server.py` (tool registration)
- Test: `tests/test_cli_device_measure.py` (new; CliRunner with monkeypatched subscriber), MCP handler test alongside existing MCP tests

**Interfaces:**
- CLI: `helixgen device measure [--seconds N=20] [--json] [--min-playing N=40]` — human summary (levels in dB, playing coverage, ok/reason) or one JSON object; exit code 1 when `ok=False`.
- MCP: `device_measure(seconds=20)` → the `MeasureResult` as a dict; description states read-only, requires the player to play, ~10 Hz sampling, gain_db is the input-invariant metric.

- [x] **Steps: failing CLI test (synthetic stream via monkeypatch) → FAIL → implement verb + handler → PASS + suite → Commit** `feat(device): device measure verb + MCP mirror`.

### Task 5: Docs + spec sync (same PR)

**Files:**
- Modify: `docs/CLI.md` (measure verb entry; bypass/model/set-param entries note the wire translation is internal), `docs/helix-protocol.md` (§4 telemetry: 10 Hz, audio-envelope semantics, tap points, wire-index law; §6 PARAM SET / live-ops table correction), `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` (erratum note at §"live ops": block_id is NOT the blks key), `docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md` (phase-0 findings appendix; correct 2–3 Hz → 10 Hz), `docs/BACKLOG.md` #58 (phase 0+1 done, phase 2 + full grid map remaining), `CLAUDE.md` (verb index + device-write gating list gain `measure`)
- [x] **Commit** `docs: loudness phase-0 findings, wire-index erratum, measure verb docs`.

### Task 6 (DEFERRED — stays in backlog #58): phase 2 `device normalize` + skills

Needs per-snapshot `.hsp` param-override writes in `mutate` (only enable/disable are snapshot-aware today), an interactive play-prompt loop, and the plugin-repo skill updates (separate repo). Out of scope for this PR by design.

## Self-review notes

- Spec coverage: phase 0 (characterized empirically, encoded in docs Task 5), phase 1 (Tasks 2–4), wire-index bug (Task 1); phase 2/3 explicitly deferred (Task 6) — matches "spec + implement" scope agreed with the owner.
- Type consistency: `MeterReading` (existing), `MeasureSample`/`MeasureResult` defined once in Task 3 and consumed by Task 4.
