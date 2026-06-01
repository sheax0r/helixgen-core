# Usage modes — CLI, local Claude Code, hosted MCP

helixgen can be used three ways. They share the same library, the same
spec format, and the same generated `.hsp` output — they differ in how
the workflow is driven and where state lives.

| | **A. Bare CLI** | **B. Local Claude Code** | **C. Hosted MCP (claude.ai)** |
|---|---|---|---|
| **How you invoke it** | shell — `helixgen generate …` | open the repo in Claude Code; say `/tone …` (or just describe a tone) | claude.ai web/desktop with the custom connector configured |
| **What runs where** | CLI on your machine, library at `~/.helixgen/library/`, IR cache at `~/.helixgen/irs/mapping.json` | CLI + an MCP server auto-spawned via `.mcp.json` (stdio transport); same local library + IR cache | only an MCP server on Render; no access to your filesystem |
| **State persistence** | persistent on your disk | persistent on your disk | per-conversation only; nothing carries across sessions |
| **User-IR workflow** | `helixgen ir-scan ~/IRs/` once; reference by basename in specs | same as A; the `using-helixgen` skill prompts you to ir-scan on first use | drag WAV into chat → `compute_irhash` returns hash → agent embeds hex in spec; **re-drag each session** |
| **Library** | whatever you've ingested locally | whatever you've ingested locally | bundled snapshot baked into the Render deploy |
| **Output** | a file on disk wherever you point `-o` | a file at `/tmp/<slug>.hsp` (the default the `tone` skill uses) | base64 `EmbeddedResource` rendered by claude.ai as a downloadable file |
| **Best for** | scripting, batch generation, no-Claude workflows | day-to-day tone design on your own machine; preferred for non-trivial work | trying helixgen without installing anything; demoing |

## A. Bare CLI

```bash
helixgen list-blocks --category amp
helixgen show-block "Brit Plexi Brt"
helixgen ir-scan ~/path/to/IRs/
helixgen generate my-tone.json -o my-tone.hsp
```

No Claude involved. You write the spec JSON yourself. See the main
[README](../README.md) for the spec format, [`docs/ir-hash-algorithm.md`](ir-hash-algorithm.md)
for how IR hashing works under the hood, and the project's `CLAUDE.md`
for the full schema (snapshots, footswitches, expression).

## B. Local Claude Code

Open this repo in Claude Code; the `.mcp.json` at the repo root
auto-spawns the MCP server over stdio. Two skills load with the project:

- `using-helixgen` — the setup pass. Confirms your device model
  (Stadium / Stadium XL), locates your IR library, recalls your IR
  preferences, and remembers the no-paid-IRs rule before generating
  anything.
- `tone` — the tone-design pass. Takes a natural-language description
  ("Dream On chorus on my Strandberg"), drafts the spec, calls
  `generate_preset`, reports back with chain + guitar-side settings +
  the file path.

State lives where the CLI would put it: `~/.helixgen/library/`,
`~/.helixgen/irs/mapping.json`. The IR cache persists across sessions —
once you've `ir-scan`'d your library, you can reference IRs by basename
from any session.

If `helixgen list-blocks` from the CLI works, B works too — the MCP
server is just a wire wrapper around the same handlers.

## C. Hosted MCP (claude.ai)

Set up by following [`mcp_server/DEPLOY.md`](../mcp_server/DEPLOY.md):
deploy `render.yaml` to Render, configure the
`MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` env vars in the dashboard,
and add the resulting `https://…/mcp` URL as a custom connector in
claude.ai.

Six tools exposed: `list_blocks`, `show_block`, `generate_preset`,
`list_irs`, `compute_irhash`, `discover_irs`. Every tool takes a
required `model` parameter (`"stadium"` or `"stadium_xl"`).

The user-IR workflow on hosted is intentionally different:

1. **You drag a WAV into the chat.** Claude has the bytes.
2. **Agent calls `compute_irhash(model, wav_b64)`.** Returns
   `{irhash, reminder}` — the hex hash plus a reminder that the WAV
   must also be loaded onto the device for the hash to resolve.
3. **Agent embeds the returned hex hash literally** in the spec's `ir`
   field (`"ir": "f42b15f3…"` — the field accepts hash or basename).
4. **Agent calls `generate_preset(model, spec)`** and surfaces the
   returned `.hsp` blob as a download.
5. **You import the WAV onto your device** via the Helix Stadium app's
   Librarian, **and** load the `.hsp` into a preset slot. Both sides
   need to know about the IR.

No IR cache, no `mapping.json`, no cross-session memory of WAVs you
dragged before. Each new conversation that wants user IRs needs them
re-dragged. (The `using-helixgen` skill does not load on claude.ai —
only Claude Code loads `.claude/skills/`.)

`discover_irs` is disabled on the hosted deploy: the
`HELIXGEN_HOSTED=1` env var in `render.yaml` makes it refuse
filesystem-walk requests with a clear redirect to `compute_irhash`.

## When to pick which

- **You're scripting or want no Claude involvement** → A.
- **You're on your own machine for serious tone work** → B. The
  combined `using-helixgen` + `tone` skills + persistent IR cache make
  iteration cheap, and the agent has access to your real library.
- **You're demoing helixgen or testing from a phone/tablet** → C. No
  install required, but with less power: no persistent IRs, no skills
  beyond what's baked into tool descriptions, no access to your IR
  collection.

Modes A and B can coexist on the same machine — they share state. A
and C can't share state (different filesystems); pick one as your
canonical source of truth.

## What's the same across all three

The spec schema, the generator, the library format, the IR hash
algorithm, and the produced `.hsp` are identical. Any preset you
generate via one mode loads on the same device the same way. The
modes differ only in driver and persistence — not in output
correctness.
