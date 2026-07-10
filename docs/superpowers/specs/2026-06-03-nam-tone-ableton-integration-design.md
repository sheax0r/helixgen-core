# /nam-tone — Tone3000 → NAM → Ableton integration (design)

Status: approved design, ready for implementation plan. Written 2026-06-03.

Supersedes an earlier exploratory integration handoff note (since removed from the repo)
where they conflict (notably: the output artifact is now decided — option C, the Ableton
`.adg` rack — not the handoff's recommended option A).

---

## 1. Goal

Give the user a parallel path to the existing `/tone` Helix flow: describe a guitar
tone in natural language and receive an **Ableton-ready `.adg` rack** that loads a
[Tone3000](https://www.tone3000.com) NAM capture + cab IR into the **NAM Gateway**
plugin, with the plugin's knobs set from the description — plus a markdown "rig sheet"
documenting the choices and license.

The skill is the curation brain. The retrieval (Tone3000 API) and the DSP (Gateway's
model + IR + chaining) already exist; what does not exist anywhere is the
description → right-capture → paired-IR → ready-to-load-rack pipeline. That is the value.

## 2. Feasibility (settled — see `data/ableton-probe/FINDINGS.md`)

We probed real files saved from the user's machine (`.aupreset`, `.vstpreset`, `.adg`).
The NAM plugin state is byte-identical across all three wrappers:

```
[u32 len=22] "###NeuralAmpModeler###"
[u32 len=5 ] "1.0.1"
[u32 len   ] <absolute .nam model path>     ← plaintext, swappable
[u32 len   ] <absolute .wav IR path>        ← plaintext, swappable
<float64[] knob values>
```

Ableton's `.adg` is gzipped XML; the plugin state is stored as **inline hex** inside
`<ProcessorState>...</ProcessorState>` and decodes to exactly the blob above (no extra
VST3 header). `<Uid><Fields.0..3>` encode the VST3 class GUID
(`F2AEE70D00DE4F4E41414D6137325247`, plugin "Gateway", vendor "Atkinson Advanced
Modeling, LLC") as four int32s.

**Generation = clone a template `.adg` → rewrite the two paths + knob doubles in the
`<ProcessorState>` hex → re-gzip.** No opaque blob, no embedded weights, no
version-fragile binary. Zero reverse-engineering risk remains for the file format.

## 3. Architecture — layering (mirrors existing helixgen)

Same dual-wrapper pattern as `list_blocks`/`generate_preset` today: **all logic lives in
a core Python library; the CLI and the MCP server are thin wrappers that call the same
functions.** The skill drives the MCP tools; humans use the CLI.

```
core lib:   src/helixgen/tone3000/   (API client, auth)
            src/helixgen/namrack/    (.adg clone + ProcessorState surgery, knob map)
                       │
            ┌──────────┴───────────┐
        CLI subcommands         MCP tools           ← both thin, no business logic
       (helixgen ...)         (mcp_server/tools.py)
                                     │
                              /nam-tone skill         ← orchestration + agent judgment
```

### 3.1 `src/helixgen/tone3000/` — API client + auth
- OAuth 2.0 PKCE flow (no client secret; `client_id` from the user's personal app).
- Endpoints used: `GET /tones/search`, `GET /tones/{id}`, `GET /models?tone_id=`,
  `GET /models/{id}` (→ `model_url`), `GET /user`. Base `https://www.tone3000.com/api/v1`.
- Token persisted at `~/.helixgen/tone3000/token.json` (mode 0600):
  `{access_token, refresh_token, expires_at, client_id}`. Auto-refresh on 401.
- Search-response cache at `~/.helixgen/tone3000/cache/` (~1h TTL, keyed on param set)
  so the skill can iterate on one request without burning the rate limit.
- Downloads land in `~/.helixgen/nam-captures/<creator>/<tone>/`.

### 3.2 `src/helixgen/namrack/` — the `.adg` generator
- `parse_processor_state(bytes) -> {model_path, ir_path, knobs: [float]}`
- `build_processor_state(model_path, ir_path, knobs) -> bytes` (recomputes length prefixes)
- `clone_rack(template_adg_path, model_path, ir_path, knobs, out_path)` —
  gunzip → locate `<ProcessorState>` → hex-decode → rebuild → hex-encode → splice → gzip.
- **Knob map** (`KNOB_INDEX`): index→name table for the float64 array (Gain, Bass, Mid,
  Treble, Output, NoiseGate threshold, EQ on/off, IR on/off, …). Established by the
  precursor probe in §6. Pure stdlib.

### 3.3 MCP tools (added to existing `mcp_server/tools.py` + `server.py`)
- `t3k_login_status()` → `{logged_in, expires_at?, user?}`
- `t3k_search_tones(query, gear?, architecture?, sort?, limit?)` → trimmed list
  `{id, name, creator, gear_list, license, is_full_rig, score}`
- `t3k_get_tone(id)` → metadata + attached models (A1/A2-Full/A2-Lite variants)
- `t3k_download_model(model_id, dest_dir?)` → absolute path to the downloaded `.nam`
- `generate_nam_rack(spec)` → the `.adg` (and writes the rig sheet); `spec` carries
  template path, model path, ir path (or none), and knob values.
- Existing `list_irs` is reused for the local-IR survey.

### 3.4 CLI subcommands (added to `src/helixgen/cli.py`)
- `helixgen tone3000-login` — runs PKCE flow, opens browser, captures redirect on
  `http://localhost:<port>/callback`, stashes token.
- `helixgen tone3000-search <query> [--gear ...] [--limit N]`
- `helixgen tone3000-download <model-id>`
- `helixgen nam-rack <spec.json> -o <out.adg>` — manual generation (mirrors `generate`).

### 3.5 Skill `.claude/skills/nam-tone/SKILL.md`
Workflow (mirrors `/tone`'s skeleton):
1. **Setup check** — verify login (`t3k_login_status`); if not, tell the user to run
   `helixgen tone3000-login`. Recall device/guitar memory. Confirm a base template `.adg`
   exists (path configured; see §4).
2. **Clarify** only what's missing — guitar, role, reference — same heuristics as `/tone`.
3. **Search** Tone3000 with gear filters derived from the description.
4. **IR curation (agent judgment):** survey three sources and recommend with reasoning —
   (a) the user's local `~/.helixgen/irs/` library (via `list_irs` + `/tone` cab
   heuristics), (b) Tone3000-hosted IRs, (c) detect **full-rig captures** that bake the
   cab in and need no IR. Present the pick; user confirms or redirects.
5. **Knob values** — derive Gain/EQ/Output from the description (like `/tone` sets params).
6. **Generate** — download the `.nam`, call `generate_nam_rack`, get `.adg` + rig sheet.
7. **Deliver** — `open -R` the output folder; summarize chain, settings, and license.

## 4. The base template `.adg`

The user authors **one** base rack by hand in Ableton (NAM Gateway VST3 + any EQ/reverb
they like), saved as `.adg`. The skill clones it per tone — it does not synthesize racks
from scratch. Template path is configured (env var `HELIXGEN_NAM_TEMPLATE`, default
`~/.helixgen/tone3000/template.adg`). The setup step verifies it exists and contains a
`<ProcessorState>` with the NAM blob; otherwise it instructs the user how to save one.

## 5. Decisions / boundaries

- **v1: VST3 only.** AU is a cheap later add — identical inner surgery, second template.
- **OAuth: the user's personal Tone3000 app.** User registers it, supplies `client_id`
  (env var `TONE3000_CLIENT_ID` or `~/.helixgen/tone3000/config.json`). Setup docs the steps.
- **v1 sets knobs** from the description (requires §6 precursor).
- **No bulk download.** Targeted retrieval only: 1 search + a handful of model fetches per
  request. Honors Tone3000 ToS / creator IP.
- **License surfaced, not enforced** — shown in the rig sheet.
- **Out of git:** downloaded `.nam`, generated `.adg`, IRs, tokens — all under
  `~/.helixgen/`, machine-specific (absolute paths). `.gitignore` covers them, consistent
  with the existing IR/data policy.

## 6. Precursor: knob-index map (required before knob-setting)

We have the float64 array but not the index→knob mapping. Establish it deterministically,
same style as the IR-hash crack:
1. In Ableton, set Gateway knobs to a known baseline; save `.adg`; record the doubles.
2. Move exactly one knob to a known value; re-save; diff to bind that index→knob.
3. Repeat for each knob and each toggle (gate on/off, EQ on/off, IR on/off).
4. Capture results as `namrack.KNOB_INDEX` + a fixture, with a round-trip test.

This is a small, bounded probe gated behind the user producing the saved files (like the
format probe already done). If it stalls, v1 can degrade to "load model+IR, leave knobs at
template defaults" without blocking the rest.

## 7. Error handling

- Unauthenticated / expired token → auto-refresh; on hard failure, a clear
  "run `helixgen tone3000-login`" message.
- Rate limit (429) → surface remaining budget; rely on the search cache to avoid re-hits.
- Missing/invalid template `.adg` → setup step explains how to save one.
- Capture is amp-only but no IR chosen → skill warns and recommends an IR before generating.
- Non-48kHz / unusual IR → reuse existing helixgen IR validation messaging.

## 8. Testing

- **namrack round-trip:** build_processor_state → parse_processor_state returns inputs;
  knob length-prefix integrity.
- **clone golden test:** clone the probe `template.adg` with new paths/knobs → gunzip →
  assert the new paths + doubles appear and the XML still parses; assert untouched bytes
  are unchanged.
- **knob map:** fixture-backed assertions that named knobs land at expected indices.
- **API client:** mocked HTTP for search/tone/model + token refresh on 401; PKCE
  challenge/verifier correctness.
- **Live Tone3000 calls:** behind skip-if-no-token guards (mirrors the real-export fixture
  pattern so a clean clone stays green).
- TDD throughout, per repo convention.

## 9. Out of scope (v2+)

- AU `.adg` output (second template).
- `.adv` single-device and full `.als` session output.
- Multi-NAM chaining inside one Gateway instance (drive capture → amp capture); v1 is one
  model + one IR. The format supports it; the curation/UX does not yet.
- Generating racks from scratch (no template).
- Sharing one hosted "helixgen" OAuth app across users.
