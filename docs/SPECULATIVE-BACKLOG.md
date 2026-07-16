# helixgen — speculative backlog (for review)

Forward-looking feature ideas, **not yet committed to**. These emerged from the
2026-07-14 Stadium-app parity capture and its findings
(`docs/superpowers/specs/2026-07-14-parity-capture-findings.md`). They are here
for the owner to triage into `docs/BACKLOG.md` (or discard). Ranked loosely by
value ÷ effort within each group. Nothing here is a commitment or a plan.

Legend: **[ripe]** = protocol fully known, could start now · **[needs-RE]** =
one more capture/dig required · **[idea]** = worth discussing before scoping.

## A. Live performance surface (the capture unlocked all the args)

- **S1 · Network tuner — `device tuner`** **[ripe]** — subscribe to 2003
  `/dspEvent {eid_:10, mid_:796}`, decode the single float as fractional-MIDI
  (note + cents), render a live in-terminal tuner (with a `--json` one-shot
  mode). *Unique selling point:* the pitch detector is **always live**, so
  this works with the Stadium app closed and needs no device-UI engage — a
  network tuner no other tool offers.
- **S2 · Network meters — `device meters`** **[ripe]** — same 2003 stream,
  `{eid_:1, mid_:796/800}` = 128-float grid level arrays. Surface input/output
  levels for clip/noise-floor checks; the app itself has no meter view.
- **S3 · Live ops verbs — `device snapshot <n>` / `device bypass <block> [on|off]`
  / `device model <block> <model>`** **[ripe]** — `/activateSnapshot`,
  `/BlockEnableSet`, `/ModelSet` args are all decoded. Hands-free / scripted
  performance control (e.g. a footswitch-less snapshot changer, or an automated
  A/B test harness).
- **S4 · `device reorder`** **[ripe]** — `/ReorderContainerContent [cmd,
  container, [cids], pos]`; reorder presets within a setlist or setlists
  themselves from the CLI.
- **S5 · Live dashboard — `device watch` TUI** **[idea]** — combine S1+S2 +
  current preset + tempo into one live 2003-subscribed dashboard (tuner, meters,
  active snapshot, BPM). A "cockpit" view of the device with the app closed.

## B. Global EQ beyond the basic set/list (now shipped)

- **S6 · Global EQ profiles — `device globaleq apply <profile.json>` /
  `save`** **[ripe]** — since Global EQ is a small write-only property set,
  helixgen can own named EQ curves locally ("small room", "loud stage",
  "headphones-flat") and push a whole profile in one command. A fast per-venue EQ
  switcher — genuinely useful because the device makes this fiddly.
- **S7 · `device globaleq reset [<out>]`** **[ripe]** — write all bands to
  factory defaults (flat gains, default freqs, enables) — a one-shot "un-mess my
  EQ" that's awkward on the hardware.
- **S8 · Global EQ read-back** **[needs-RE]** — the device won't answer
  `/PropertyValueGet dsp.globaleq.*`; find the connect-time bulk read (or the
  `globals.eq` read path) so `device globaleq get` can show current state.

## C. Authoring depth (`.hsp` → device transcoder)

- **S9 · Command Center authoring** **[ripe]** — recipe support for
  footswitch/EXP → command (Preset/Snapshot, MIDI CC/PC/Note/MMC, HotKey,
  Utility, Instant, EXP→MIDI), synthesized into `cg__.entt` (`srcs`/`cmnd`/
  `trgs`). The wire + storage encodings are decoded (#16). High value — command
  center is a big authoring gap.
- **S10 · MIDI controller assignment authoring** **[ripe]** — recipe support for
  "incoming CC# → block bypass / param", synthesized into `cg__/entt/ctrl` +
  `ctm_` (#33 decoded). Pairs naturally with S9.
- **S11 · XY morph authoring** **[needs-RE]** — the zone *activation* is known
  (`/SetBatchedParamValues`) but zone *storage* isn't in the `.sbe`; needs a
  follow-up capture to locate where zones persist (#34) before authoring.

## D. Setlist / library bundles

- **S12 · `.hss` import — `device setlist import-hss <file>`** **[ripe]** —
  read a shared `.hss` (gzip+tar container, decoded) into the tone library:
  bulk-ingest a whole setlist someone sent you. Reading is unblocked today.
- **S13 · `.hss` export — `device setlist export-hss <setlist>`** **[needs-RE]**
  — bundle a helixgen setlist to a shareable `.hss`. Needs one non-empty `.hss`
  capture to pin the filled-slot payload framing before a byte-faithful writer.

## E. Cross-cutting / smaller

- **S14 · Per-tone tempo on sync** **[idea]** — let a tone carry a target BPM and
  have `device sync` set `global.tempo.bpm` when it's made active (tempo is just
  a property now). Songs-style tempo without the Song machinery.
- **S15 · `device settings` presets/diff** **[idea]** — snapshot the whole
  `global.*` property set to a file, diff two devices, or restore a saved global
  config (extends the shipped settings verbs).
- **S16 · Tuning-assisted capture/verify** **[idea]** — use the always-on pitch
  stream (S1) as a helper: confirm the guitar is in tune before generating/
  auditioning a tone, or flag a flat string in `verify`.
- **S17 · Time-signature / Song authoring** **[needs-RE]** — time signature lives
  in the SFTP song file, not OSC; a deeper song-format RE would unlock authoring
  songs (tempo map, time sig, markers). Large; only if Songs become a goal.

---

*Provenance:* all Section A–D items trace to concrete decoded protocol in the
2026-07-14 parity findings spec. "[ripe]" means the wire format is proven and
(for the live ones) at least one command was accepted by the hardware.
