# Deploying helixgen-mcp

Public, unauthenticated MCP server wrapping the helixgen CLI. Hosts on
Render's free tier; integrates with claude.ai as a custom connector.

> For a side-by-side of the three ways to use helixgen (bare CLI / local
> Claude Code with auto-spawned MCP / this hosted deploy), see
> [`docs/usage-modes.md`](../docs/usage-modes.md).

## What you get

Six tools exposed to any Claude client that connects to your server URL.
**Every tool takes a required `model` parameter** (`"stadium"` or
`"stadium_xl"`) — a soft device-confirmation gate; the
`using-helixgen` skill is what actually confirms the model with the user
per session.

- `list_blocks(model, category?)` — browse the block catalog.
- `show_block(model, name_or_id)` — inspect a block's params.
- `generate_preset(model, spec)` — turn an inline JSON tone spec into a
  `.hsp` Stadium preset, returned as a binary `EmbeddedResource`.
- `list_irs(model)` — list registered user IRs. On this hosted deploy
  always empty (the registry is local-only).
- `compute_irhash(model, wav_b64)` — compute the Stadium IR hash for a
  base64-encoded WAV (size ≤ 2 MB, RIFF/WAVE validated). Returns
  `{irhash, reminder}`. Drag-and-drop friendly for claude.ai users.
- `discover_irs(model, ir_directory)` — walk a server-side filesystem
  path and return per-file hashes. **Refused on hosted** (the hosted
  deploy has no access to the user's filesystem); the tool returns a
  clear error directing the agent to `compute_irhash` instead.

The full spec schema is in `CLAUDE.md` at the repo root (paths, snapshots,
footswitches, expression). The `ir` field on IR blocks **accepts a
literal 32-char hex hash** in addition to a basename — so hosted users
can call `compute_irhash` on a dragged WAV and embed the returned hash
directly into a subsequent `generate_preset` call. (The basename path
requires a server-side registry, which the hosted deploy does not have;
basenames will not resolve.)

## What's bundled

The deploy ships a snapshot of the maintainer's helixgen library under
`mcp_server/data/library/`:

- `chassis.json` — Stadium device chassis (scrubbed of user metadata).
- `blocks/<category>/*.json` — ~330 block schemas (scrubbed of
  `first_seen.preset` source filenames).

The Render build step copies this snapshot into `$HOME/.helixgen/library/`
and rebuilds the lookup index. No `helixgen bootstrap` is run at deploy
time — the bundled catalog is the source of truth for the deployed server.

## Step 1: Deploy to Render

1. Sign in to [render.com](https://render.com).
2. **New +** → **Web Service** → connect this GitHub repo.
3. Pick branch `main` (or whichever branch holds `render.yaml`).
4. Render detects `render.yaml` automatically. Confirm the suggested
   service name (`helixgen-mcp`) and click **Create Web Service**.
5. First build is fast (~30s, no network bootstrap). Watch the build log.
6. Once **Live**, copy the URL — something like
   `https://helixgen-mcp-xxxx.onrender.com`. Your MCP endpoint is that
   URL + `/mcp`.

### Bundled in `render.yaml` (committed)

These ship with the repo and don't need dashboard configuration:

- `aptPackages: [libsndfile1]` — required by `compute_irhash`, which
  loads libsndfile via `ctypes` to run Stadium's preprocessing pipeline.
  Without this the tool raises a "libsndfile not found" error.
- `envVars.HELIXGEN_HOSTED=1` — read by `discover_irs` to refuse
  filesystem-walk requests on the hosted deploy.

### Required env vars (set in the Render dashboard, not in `render.yaml`)

The MCP SDK ships with DNS-rebinding protection that rejects requests
whose Host or Origin header isn't on an allow-list. Set these on your
service before the first request, otherwise every call returns
`Invalid Host header`:

- `MCP_ALLOWED_HOSTS` — comma-separated public hostnames that should
  reach the server. Examples:
  - `helixgen-mcp-xxxx.onrender.com` (default Render hostname)
  - `mcp.example.com` (your custom domain)
  - `helixgen-mcp-xxxx.onrender.com,mcp.example.com` (both, while you
    transition)
- `MCP_ALLOWED_ORIGINS` — comma-separated origins (with scheme) that
  may call the server. For claude.ai connector use:
  - `https://claude.ai`

Render → your service → **Environment** → **Add Environment Variable**
for each. Save; Render redeploys.

These live as env vars (not in the committed `render.yaml`) so the repo
stays neutral — anyone deploying their own instance fills in their own
hostnames.

## Step 2: Add as a custom connector in claude.ai

1. claude.ai → **Settings** → **Connectors** → **Add custom connector**.
2. Name: `helixgen`. URL: `https://helixgen-mcp-xxxx.onrender.com/mcp`
   (your URL from Step 1).
3. Save. Claude should report the connector handshake succeeded and list
   six available tools.

## Step 3: Smoke test

In a new claude.ai chat:

> List the available helixgen amp blocks.

Claude should call `list_blocks(category="amp")` and return a categorized
list. If you see a timeout (~45s) on the first request after a quiet
period, that's Render's free-tier cold start — subsequent requests are fast.

Then:

> Generate a clean tone preset using one of those amps and a matching cab.

Claude should call `show_block` to look up param names, then
`generate_preset` with an inline spec. The result comes back as an
`EmbeddedResource` with a base64-encoded `.hsp` payload — claude.ai
renders it as a downloadable file.

## Known limitations (v1)

- **No auth.** Anyone with the URL can use it. If traffic gets noisy, take
  the service down via Render's dashboard.
- **No rate limiting.** Same caveat.
- **Cold starts.** Render free tier suspends after 15 min of idle; first
  request takes 30–60s to wake. Mitigations (UptimeRobot keepalive,
  upgrading off free tier) are out of scope for v1.
- **User IRs require per-session drag-and-drop.** Hosted has no
  persistent IR registry — IR resolution works via `compute_irhash` on
  a dragged WAV, then embedding the returned hex hash in the spec's
  `ir` field. There's no cross-session memory; the user re-drags IRs
  each conversation. (Local-Claude-Code users get a persistent
  `mapping.json` cache via `helixgen ir-scan`.)
- **`compute_irhash` is 48 kHz-only.** Same limitation as the local
  primitive — non-48 kHz sources raise an error. Stadium itself uses
  libsamplerate for resampling; porting that bit-exactly isn't done.
- **Stateless.** Every generate call rebuilds the library handle. No
  per-user storage, no preset history.
- **Library is a snapshot.** New blocks added to the maintainer's local
  library after a deploy aren't reflected until the next push.

## Updating the deployed library

The bundled library at `mcp_server/data/library/` is a snapshot. To
refresh it:

1. Locally, ensure `~/.helixgen/library/` is up to date (`helixgen ingest
   <new-preset.hsp>` for new blocks).
2. Re-run the bundling script from `docs/superpowers/plans/2026-05-30-helixgen-mcp-server.md`
   (Task 9, Step 1).
3. Commit the updated `mcp_server/data/library/`.
4. Push — Render auto-deploys on push to the connected branch.

## Updating the deployment (code, not data)

Render auto-deploys on push to the connected branch. To redeploy without
a code change (e.g. to retry a failed build), use Render's **Manual
Deploy** → **Clear build cache & deploy**.
