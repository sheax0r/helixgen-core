# Deploying helixgen-mcp

Public, unauthenticated MCP server wrapping the helixgen CLI. Hosts on
Render's free tier; integrates with claude.ai as a custom connector.

## What you get

Three tools exposed to any Claude client that connects to your server URL:

- `list_blocks(category?)` — browse the block catalog.
- `show_block(name_or_id)` — inspect a block's params.
- `generate_preset(spec)` — turn an inline JSON tone spec into a `.hsp`
  Stadium preset, returned as a binary `EmbeddedResource`.

The full spec schema is in `CLAUDE.md` at the repo root (paths, snapshots,
footswitches, expression). The `ir` field on IR blocks is ignored
server-side — this deployment ships only canonical IRs from the bundled
chassis, no user-IR registry.

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

## Step 2: Add as a custom connector in claude.ai

1. claude.ai → **Settings** → **Connectors** → **Add custom connector**.
2. Name: `helixgen`. URL: `https://helixgen-mcp-xxxx.onrender.com/mcp`
   (your URL from Step 1).
3. Save. Claude should report the connector handshake succeeded and list
   three available tools.

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
- **No IR support.** The `ir` field in specs is silently ignored. IR
  blocks use whatever canonical hash the bundled library carries.
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
