# tone3000-integration — handoff

Context dump for a fresh agent picking up the design of a Tone3000 / NAM integration that sits alongside the existing Helix Stadium preset generator. Written 2026-05-31.

---

## 1. Mission

Add a new capability to `~/git/helixgen` (or a sibling repo, TBD): take a natural-language tone description and produce an **Ableton-ready artifact** backed by NAM captures from [Tone3000](https://www.tone3000.com).

Today, the existing `/tone` skill in this repo designs `.hsp` presets for the user's Helix Stadium XL. The new work would give the user a parallel path: instead of (or in addition to) a Helix preset, produce something they can drop into Ableton on a track running the **NAM Gateway** plugin.

The user is exploratory on scope — they wrote: *"create ableton ... fiels? I'm not sure quite what, but at least look up the tone"*. So **looking up and downloading the right `.nam` captures from a description** is the floor; generating actual Ableton file artifacts (`.adv` / `.adg` / `.als`) is a stretch goal whose feasibility depends on reverse-engineering the formats. **Do not assume v1 needs to emit Ableton files** — clarify with the user before committing engineering to it.

---

## 2. What this session established about the Tone3000 API

**Base URL:** `https://www.tone3000.com/api/v1` · **Auth:** OAuth 2.0 with PKCE (no personal-access-token option — apps must register) · **Rate limit:** 100 req/min default; search is "heavily rate-limited" and production tier requires emailing `support@tone3000.com`.

### Endpoints (verified from https://tone3000.com/api and https://github.com/tone-3000/api )

| Endpoint | Notes |
|---|---|
| `GET /oauth/authorize` | PKCE init. Params: `client_id`, `redirect_uri`, `response_type`, `code_challenge`, `code_challenge_method`, `state`, plus optional `prompt` / `gears` / `architecture` / `tone_id` for "load this tone" flows. |
| `POST /oauth/token` | Code exchange + refresh. Returns `access_token`, `refresh_token`, `expires_in`, `scope`. |
| `GET /user` | Current user profile. |
| `GET /tones/search` | Library browse. Query params: `query`, `gears` (underscore-joined), `sizes`, `platform`, `architecture` (a1/a2/a2_full/a2_lite), `sort`. **`page_size` max 25.** |
| `GET /tones/{id}` | Tone metadata (creator, gear, license, description). Accepts `architecture` filter. |
| `GET /tones/created` · `/tones/favorited` | User's own. `page_size` max 100. |
| `GET /models?tone_id={id}` | List of models attached to a tone. A single "tone" may include A1, A2-Full, A2-Lite variants. |
| `GET /models/{id}` | Returns **`model_url`** — the actual downloadable `.nam` URL. |
| `GET /users` | Public user search. |

### Bulk-download verdict from this session

The user originally asked about scraping the whole library. We declined for ToS + creator-IP reasons and steered toward targeted API use. **A blanket "download everything" pull is still off the table** without first contacting Tone3000 support — the rate-limit cliff (~2 hours just for metadata at the default limit, then thousands more model fetches) plus their own "contact support for production use" language make that the right gate.

The right shape for this integration is **targeted retrieval driven by a tone description**: 1 search call + a handful of model fetches per request.

### Architecture (NAM "A2") quick reference

- **A2 is the capture file format**, not a plugin name. The plugin is **NAM Gateway** (download at `neuralampmodeler.com/users`), requires NeuralAmpModelerCore ≥ 0.5.2 for A2 support.
- A2-Full = best accuracy, heavier CPU — right pick for DAW work.
- A2-Lite = leaner, for live/CPU-constrained rigs.
- Both load in the same Gateway plugin.

---

## 3. Existing helixgen patterns to mirror

The repo already has the right shapes for what this integration needs. Read these before designing anything new.

### Skill pattern

- `~/git/helixgen/.claude/skills/setup/SKILL.md` — prereq-check skill. Verifies MCP tools registered, recalls device-model memory, locates IR library, recalls preferences. **Run-before-anything pattern.** A Tone3000 skill should have a sibling `setup` step that verifies OAuth is established and the access token is fresh.
- `~/git/helixgen/.claude/skills/tone/SKILL.md` — the natural-language → `.hsp` workflow. Has a great structure to copy: clarify-what's-missing → sketch chain → pick blocks → verify params → build spec → generate → report. The Tone3000 skill should follow this skeleton: clarify → search → pick captures → download → report.

### MCP server pattern

- `~/git/helixgen/mcp_server/server.py` + `tools.py` — the stdio MCP server bundled by the plugin's `.mcp.json`. Current tools (`list_blocks`, `show_block`, `generate_preset`, `list_irs`) follow a clean "thin wrapper over Python lib" pattern. **Add Tone3000 tools here**, not as a separate server, so the existing plugin auto-spawn still works.
- Suggested new tools:
  - `t3k_search_tones(query, gear?, architecture?, sort?, limit?)` → trimmed list of `{id, name, creator, gear_list, license, score}`
  - `t3k_get_tone(id)` → metadata + list of models
  - `t3k_download_model(model_id, dest_dir?)` → returns absolute path to downloaded `.nam` (default dest: `~/.helixgen/nam-captures/<creator>/<tone-name>/`)
  - `t3k_list_favorites()` → user's favorites
  - `t3k_login_status()` → `{logged_in: bool, expires_at?, user?}`

### Local-state pattern

Existing helixgen stashes state at `~/.helixgen/` (library, IRs, mapping.json). Mirror this:
- `~/.helixgen/tone3000/token.json` — `{access_token, refresh_token, expires_at, client_id}`, mode 0600
- `~/.helixgen/nam-captures/` — downloaded `.nam` files, organized by creator/tone
- `~/.helixgen/tone3000/cache/` — short-TTL JSON cache of search responses, optional

### CLI pattern

helixgen exposes `helixgen <subcommand>` via click (see `src/helixgen/cli.py`). Add subcommands rather than a new binary:
- `helixgen tone3000-login` — runs PKCE OAuth flow, opens browser, captures redirect, stashes token
- `helixgen tone3000-search <query>` — same as the MCP tool but for terminal use
- `helixgen tone3000-download <model-id>` — manual fetch

---

## 4. Open design questions (resolve with the user before coding)

### Q1. What's the Ableton artifact? (biggest open question)

Four options, ranked by user value vs. engineering cost:

| Option | What it is | Effort | Risk |
|---|---|---|---|
| **A. Folder + rig sheet** | Download N `.nam` files into a labeled folder + a markdown "rig sheet" telling the user how to load Gateway, which slots get which captures, suggested settings | Low | Low — no Ableton-format reverse engineering |
| **B. `.adv` Gateway preset** | Generate a Gateway device preset (`.adv`, gzipped XML) that references a specific `.nam` path | Medium | Medium — `.adv` format is well-documented but Gateway's plugin-state blob may be opaque or path-dependent |
| **C. `.adg` rack** | Multi-device rack (e.g. Gateway → EQ Eight → reverb), saved as `.adg` | Medium-High | Higher — chains multiple Ableton stock effects, need to know their preset XML |
| **D. Full `.als` session** | A whole template Live Set ready to open | High | Highest — `.als` is a complex format and overkill for the user value |

**Recommended starting point: Option A.** Ship targeted lookup + download + a markdown rig sheet first. Only escalate to B/C once the user knows from real use whether file generation is worth the engineering. The Helix `.hsp` flow took multiple iterations to nail; the Ableton flow will too.

### Q2. Skill organization

Three options:
1. **One skill, two modes** — extend `/tone` to ask "Helix preset or NAM/Ableton?" up front
2. **Sibling skill** — new `/nam-tone` skill, parallel to `/tone`, shares the `/setup` prereq pattern
3. **Composed skill** — `/tone` calls into both, returning a Helix preset *and* a recommended NAM capture

The user's framing ("integrating with `~/git/helixgen`") suggests sibling, not extension. **Recommend option 2** unless the user pushes back.

### Q3. OAuth app registration

The Tone3000 API requires a registered OAuth app. PKCE means no client secret, but a `client_id` is mandatory. **Who registers, and as what?**
- Option: the user registers a personal app at tone3000.com (if their dev portal supports self-registration — verify) and gives the CLI their client_id via env var or config file. Simple, scales to one user.
- Option: helixgen registers a shared "helixgen" app whose `client_id` ships with the code. Cleaner UX. Requires Tone3000 approving the app for distribution and trusting their rate limits aggregate sanely across users.

For a one-user proof-of-concept, **start with the personal-app route**; revisit if the integration becomes something other people use.

### Q4. Caching policy

Search responses don't change minute-to-minute. Cache `tones/search` for ~1 hour (TTL keyed on full param set) and `tones/{id}` / `models?tone_id=...` for ~1 day. Lets the skill iterate freely on a single tone request without burning rate-limit budget. **Open question:** should this cache live in `~/.helixgen/tone3000/cache/` (per-user) or be ephemeral in-process? Lean toward on-disk so re-runs in the same skill session are free.

### Q5. License handling

Tone3000 hosts captures with varying licenses (some Creative Commons, some pay-to-download). The `Tone` object includes a license field. **Recommend the skill surface license info in the rig sheet** so the user knows what they're using. Out of scope to enforce.

---

## 5. Recommended v1 slice

If the user agrees with the design above, here's the minimum-viable first slice:

1. **OAuth login** — `helixgen tone3000-login` CLI subcommand. Opens browser, captures redirect on `http://localhost:<port>/callback`, exchanges code → stashes token at `~/.helixgen/tone3000/token.json` with refresh handling.
2. **MCP tools** — add `t3k_search_tones`, `t3k_get_tone`, `t3k_download_model`, `t3k_login_status` to `mcp_server/tools.py`. Auto-refresh on 401.
3. **Skill** — new `.claude/skills/nam-tone/SKILL.md`. Workflow:
   - Run setup checks (verify login, otherwise tell user to `helixgen tone3000-login`)
   - Clarify what's missing (guitar, role, target reference) — same pattern as `/tone`
   - Search Tone3000 with sensible gear filters derived from the description
   - Pick top 1-3 captures, fetch their `.nam` URLs, download to `~/.helixgen/nam-captures/<creator>/<tone>/`
   - Write a markdown rig sheet: chain (Gateway slot → effects → settings), guitar settings, license info, Finder-reveal the folder
4. **Out of scope for v1:** generating `.adv` / `.adg` / `.als` files. Park those as v2.

Test plan: end-to-end on a known target ("Slash-style lead, Les Paul, Marshall JCM800") → verify the rig sheet is actionable and the `.nam` files load in Gateway in Ableton.

---

## 6. Files to read before designing

- `~/git/helixgen/CLAUDE.md` — repo conventions
- `~/git/helixgen/docs/handoff.md` — original project handoff, sets the v1 ground rules
- `~/git/helixgen/.claude/skills/tone/SKILL.md` — pattern to copy for the new skill's workflow shape
- `~/git/helixgen/.claude/skills/setup/SKILL.md` — pattern for the prereq-check
- `~/git/helixgen/mcp_server/server.py` and `tools.py` — where the new MCP tools land
- `~/git/helixgen/src/helixgen/cli.py` — where the new CLI subcommands land
- `https://github.com/tone-3000/api` — reference client showing OAuth + endpoint usage in practice

---

## 7. Memory context worth knowing

The user already has memory entries the new skill should respect:

- **Stadium XL device** (`user_device.md`) — they play through Stadium XL. NAM/Gateway in Ableton is *parallel* to that, not a replacement. The user might use the Helix for live and NAM for studio.
- **Guitar library** (`user_guitars.md`) — Les Paul Jr (P-90), ESP LTD EC-1000, Strandberg Boden Essential 6, Ibanez Prestige. Same guitar-side EQ heuristics from `/tone` apply.
- **IR preference and library** (`user_ir_directory.md`, `feedback_no_paid_irs_in_repo.md`) — no paid content committed to the repo. The `.nam` captures Tone3000 hosts have similar IP concerns; downloaded files belong in `~/.helixgen/nam-captures/`, **not** in the repo, and should be `.gitignore`'d.
- **Finder reveal default** (`feedback_reveal_file_in_finder.md`) — when the skill produces output files, run `open -R "<path>"` to pre-select them.

---

## 8. References

- Tone3000 API blog: https://www.tone3000.com/blog/introducing-the-tone3000-api
- Tone3000 API docs: https://tone3000.com/api
- Tone3000 demo client (auth flow + endpoint usage): https://github.com/tone-3000/api
- NAM A2 architecture announcement: https://www.tone3000.com/blog/introducing-neural-amp-modeler-nam-architecture-2-a2
- NAM A2 complete guide: https://www.tone3000.com/guides/nam-a2-the-complete-guide
- NAM Gateway plugin download: https://www.neuralampmodeler.com/users
