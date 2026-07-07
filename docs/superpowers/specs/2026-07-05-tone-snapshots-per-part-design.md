# Snapshots per guitar part

**Date:** 2026-07-05
**Status:** Design (brainstormed against existing snapshot support).
**Branch:** `worktree-agent-a3d997f7ec71aa3db`
**Scope:** the `tone` skill's research/analysis + mapping behavior. Almost entirely
skill-level (`.claude/skills/tone/SKILL.md`); the spec/generate layer already
carries the mechanism (`snapshots` array — `disable` refs + `params` overrides).
**Sibling backlog item (not designed here):** `auto-control-wiring`
(footswitch + wah/expression assignment). Interaction noted in §6.

## Goal

When the tone skill researches a target that has **more than one guitar part**
— a song with verse/chorus/lead, a request that implies rhythm **and** lead, a
"clean + crunch + solo" ask — it should encode those parts as Stadium
**snapshots** inside **one preset**, so the player recalls each part instantly
on-device without changing presets. Today the skill *can* emit snapshots (step
5.5), but the trigger, the part-count derivation, and the choice between the two
mapping strategies are underspecified and left to improvisation. This spec makes
the part-identification and mapping deterministic, and states exactly what the
spec/generate layer lacks for the richer strategy.

Non-goals: designing footswitch/EXP wiring (sibling item), adding new
snapshot machinery to `spec.py`/`generate.py` beyond the one gap called out in
§4, or authoring dual-amp presets from a friendlier surface than the existing
block list.

## Hard limits this design must respect (verified in code)

| Limit | Value | Source |
|---|---|---|
| Snapshots per preset | **8** | `spec.SNAPSHOT_MAX`, enforced in `_parse_snapshots` |
| DSP paths | **2** (one per DSP) | `parse_spec` rejects `len(paths) > 2` |
| User blocks per lane | **12** (`b01..b12`) | `generate._HSP_BNN_RANGE`; `GenerateError` at `generate.py:800` |
| Amp model swap inside a snapshot | **not possible** | snapshots override params + bypass only, never `model` |

## 1. Identifying the distinct parts (the research/analysis step)

This extends step 1b ("Research the reference sound") and its output feeds the
mapping decision in §2. The unit is a **distinct guitar sound**, not a song
section. A six-section song with three sounds is **three** snapshots, not six.

### Signals to extract during research

Pull these from the web research (artist/song target) or from the user's own
words (genre/feel target), and record them in the working notes:

- **Song sections with a guitar-treatment change** — intro / verse / pre-chorus /
  chorus / bridge / solo / breakdown / outro. Only sections where the guitar
  *sound* changes count; a verse and a chorus played on the same tone are one
  part.
- **Gain / saturation shifts** — clean → edge-of-breakup → crunch → high-gain.
  The most reliable part boundary; a clean verse and a driven chorus are always
  two parts.
- **Role shifts** — rhythm vs lead. Lead typically = more gain or a boost, a
  midrange lift for cut, and more delay/reverb. "Rhythm + lead" is the canonical
  two-part request.
- **Effect changes tied to a section** — chorus/modulation on a clean verse, a
  long delay or bigger reverb on the solo, a phaser on the bridge. An effect that
  switches on/off per section is a snapshot boundary even when the amp is
  unchanged.
- **Amp / channel switching in the source rig** — research often reveals the
  artist used a clean channel for verses and a lead channel (or an always-on
  overdrive) for the chorus/solo. A **channel** change signals a possible
  *amp-model* change → pushes toward strategy (b) in §2.
- **Signature technique per part** — palm-muted tight rhythm vs open ringing
  leads shape the EQ/gain even on one amp.

### Collapse rule (part count)

1. List every candidate part from the signals above.
2. **Merge** any two parts that would be reached by the same amp+cab with only
   knob/effect-bypass differences into a note ("chorus = verse + more delay"),
   but keep them as separate snapshots **only if** the player actually needs to
   recall them separately mid-song. If two sections are sonically identical,
   collapse to one snapshot.
3. If distinct sounds **> 8**, you are past the hardware cap: keep the 8 the
   player switches to most, and fall back to **multiple presets** for the
   overflow (see §2). This is rare for real songs.

### Ordering & the load default

- **Snapshot index 0 loads on hardware boot** (`snapshots[0]`, per `spec.py` and
  the Category-4 dense-array work). Make index 0 the part the song **starts on**
  (usually the intro/verse rhythm), or the user's stated "default" sound.
- Order the rest in song order (verse → chorus → solo) so the on-device snapshot
  list reads like the arrangement.

## 2. Mapping strategy + decision rule

Two strategies. Both use the **same** `snapshots` array; they differ in how many
blocks the chain carries.

### (a) One chain, snapshots toggle pedals + params — the cheap default

One amp + one cab shared by every part. Each snapshot is a delta: bypass a drive
here, raise amp `Drive`/`Master` there, add delay `Mix`. This is exactly step
5.5 today.

- **Cost:** fewest blocks (a typical chain is 5–6 slots, well under 12).
- **Reach:** anything within knob range of one amp — clean-to-crunch on a pedal
  platform, rhythm→lead via a boost + more delay, verse/chorus/solo where the
  core voice is constant.
- **Cannot do:** a genuinely different **amp model or cab** per part (a snapshot
  can't swap `model`).

### (b) Layered chains, snapshots enable one and bypass the others

Place **two** amp+cab pairs (optionally each with its own pre/post effects) in a
single lane, **in series**. Every block is base-enabled; each snapshot
`disable`s the blocks belonging to the parts it is **not**. Because Helix bypass
is true passthrough, only the enabled amp+cab colors the signal — the bypassed
amp is acoustically absent even though it sits in the series chain. **No
split/join is needed** (see §3), which keeps the routing simple and dodges the
parallel-routing edge cases.

- **Cost:** 2 amps + 2 cabs = 4 slots before any effects; with shared delay/verb
  and one or two drives you are at ~7–9 slots. **Cap at 2 amp+cab pairs** — a
  third pair plus effects breaches the 12-slot lane limit.
- **Reach:** fundamentally different voices with instant in-preset switching —
  clean Fender verse + high-gain Mesa chorus.
- **Watch:** keep effects **shared** where possible (one delay both parts use)
  rather than duplicating, to stay under 12. Put the two cabs' `Hi/Low Cut`
  per the anti-fizz baseline independently.

### Decision rule

Ask the family question from SKILL §1a ("same amp/cab family, or fundamentally
different sounds?"), then:

```
Do the parts share ONE amp+cab family, differing only in gain / EQ / effects?
├─ YES → strategy (a): one chain, snapshots toggle pedals + params.   [DEFAULT]
└─ NO  → Does any part need a different AMP MODEL or CAB unreachable by knobs?
         └─ YES → Does the player need to switch INSTANTLY mid-song
                  (not OK to change presets between parts)?
                  ├─ YES → strategy (b): layered chains, ≤ 2 amp+cab pairs,
                  │        each snapshot disables the complement.
                  └─ NO  → MULTIPLE PRESETS: one .hsp per family, named
                           <song>-<part>. (Out of snapshot scope; the device's
                           preset switching is fast enough between sections.)
```

Tie-breakers:
- Prefer (a) whenever it can reach all parts — it is fewer blocks and the
  best-trodden path.
- Prefer **multiple presets** over (b) when the user says "different sounds" and
  does **not** demand instant switching — simpler, no 12-slot pressure.
- Reserve (b) for the genuine "instant switch between two very different amps"
  case, capped at two pairs.

## 3. How it expresses in the existing spec

Both strategies use the top-level `snapshots` array already parsed by
`spec._parse_snapshots` (→ `Snapshot` / `SnapshotBlockRef` /
`SnapshotParamOverride`). Snapshot 0 is the load default; each snapshot is a delta
from the path-level base.

### Strategy (a) — toggle example

```json
"snapshots": [
  {"name": "Verse",  "disable": ["Compulsive Drive"],
                     "params": {"Brit Plexi Brt": {"Drive": 0.30}}},
  {"name": "Chorus"},
  {"name": "Solo",   "params": {"Brit Plexi Brt": {"Drive": 0.85, "Master": 0.7},
                                "Tape Echo Stereo": {"Mix": 0.32}}}
]
```

`Verse` (index 0, loads on boot) bypasses the boost and softens the amp; `Chorus`
is the base voice; `Solo` stacks gain + delay. All within one amp+cab.

### Strategy (b) — layered chains via per-snapshot disable

Chain (one lane, series): `[Comp, Screamer, US Deluxe Clean, Cab-Clean, Cali Rectifire, Cab-Hi, Tape Echo Stereo, Plate Stereo]`.
Every block base-enabled. Each snapshot disables the amp+cab (and any
part-specific drive) it does not use:

```json
"snapshots": [
  {"name": "Clean",
   "disable": ["Screamer", "Cali Rectifire", "Cab-Hi"],
   "params": {"US Deluxe Clean": {"Drive": 0.25}}},
  {"name": "Heavy",
   "disable": ["US Deluxe Clean", "Cab-Clean"],
   "params": {"Cali Rectifire": {"Drive": 0.80}, "Screamer": {"Gain": 0.4}}}
]
```

`Clean` (index 0) runs the Deluxe + clean cab, muting the Rectifire path; `Heavy`
mutes the clean amp+cab and runs the Rectifire. The shared Tape Echo + Plate stay
live in both (or are themselves disabled/tuned per snapshot).

**This is fully expressible today.** `Snapshot.disable` is a `list` of refs
(`spec.py:82`), so a single snapshot can bypass an arbitrary number of blocks —
i.e. "toggle a whole sub-chain off" already works. When two cabs humanize to the
same display name, disambiguate with the coordinate ref form the recent work
added: `{"block": "With Pan", "lane": 0, "pos": 4}` in `disable`, or the list
form in `params`. No parser change needed.

## 4. What the spec/generate layer LACKS for strategy (b)

The mechanism is present; the following are the concrete gaps and why (b) is
still safe today if authored with the stated convention.

1. **No per-snapshot *enable* override — only disable.** `Snapshot` carries
   `disable` but there is no `enable`. Documented in
   `2026-07-05-category5-bypass-and-dualcab-design.md` as the "Case B" gap: a
   block that is **base-bypassed** (`enabled: false` at block level, or the
   device's `null`-recall state) **cannot be turned back on** in any snapshot.
   - **Consequence for (b):** you must author the layered chain with **every
     block base-enabled** and let each snapshot disable the *complement*. Never
     set `enabled: false` on an amp/cab you intend to light up in some snapshot —
     there is no way to re-enable it. The disable-the-complement convention in §3
     sidesteps the gap entirely.
   - **When it bites:** if a future author wants a block off in the load-default
     snapshot **and** in base, but on in a later snapshot, they cannot express it.
     Closing this needs an `enable: [...]` list on `Snapshot` (mirror of
     `disable`) threaded into `generate`'s per-snapshot `@enabled.snapshots`
     array — a separate cycle, already flagged out-of-scope in the Category-5 and
     dense-snapshot specs.

2. **No "activate whole chain" grouping.** There is no concept of a named
   *scene chain*; each snapshot must **enumerate every block** it disables. For
   `N` layered parts and `M` snapshots the author writes `M × (blocks-not-in-this-part)`
   disable entries. Verbose but correct and fully expressible. A future ergonomic
   layer (e.g. tagging blocks with a `part:` label and letting a snapshot name the
   active part) is **skill/spec sugar, not a hardware limitation** — out of scope
   here.

3. **Serial-only placement is fine, but understand why.** helixgen lanes are
   serial (split/join aside). Two amps in series with one bypassed is
   acoustically clean (true bypass = passthrough), so (b) needs **no split/join**.
   Split/join would only help if the two parts had to **sum** simultaneously
   (not a per-part snapshot use case) or to buy lane headroom against the 12-slot
   cap — neither applies to the ≤ 2-pair design. Keep (b) single-lane series.

4. **12-slot cap is enforced, snapshot count is enforced — no new validation
   needed.** `generate` already raises `GenerateError` when a lane exceeds 12
   blocks (`generate.py:800`) and `parse_spec` rejects > 8 snapshots. The skill
   must **pre-check** these before calling `generate_preset` so it fails in the
   skill's reasoning, not in a wasted generation round-trip: count blocks per lane
   for (b), count distinct parts for the snapshot array.

**Net:** strategy (b) ships on the current spec/generate with zero code changes,
*provided* the skill follows the base-enabled + disable-complement convention.
The only genuine missing capability is per-snapshot **enable**, which this design
routes around and which the Category-5 spec already tracks as future work.

## 5. UX — how the skill drives it

1. **Detect multi-part.** Multiple roles/sections in the request, or research
   surfacing distinct-sound sections → enter this flow (SKILL §1a already asks the
   family question; keep it).
2. **Derive and *confirm* the part count.** After research, state the distinct
   sounds back to the user before generating: *"I found 3 distinct guitar sounds
   — clean verse, driven chorus, lead solo — I'll encode them as 3 snapshots."*
   Let the user merge or split. This is where the collapse rule (§1) surfaces.
3. **Pick the strategy** via the §2 decision rule (driven by the family answer).
   Tell the user which one and why in one clause: *"Same Plexi for all three, so
   one chain with snapshots"* or *"Clean Fender + Mesa need different amps, so I'm
   layering both and each snapshot lights up one."*
4. **Name the snapshots** by musical role/section, not tech: `Verse`, `Chorus`,
   `Solo`, `Clean`, `Heavy`, `Lead`. Keep names short (~≤ 10 chars) so they read
   on the Stadium scribble strips. Names are the on-device labels.
5. **Set the load default** = `snapshots[0]` = where the song starts (or the
   user's stated default). Say which one loads first.
6. **Communicate the layout** in the report (SKILL step 8 item 2 and the
   companion `.md`, step 7a): one line per snapshot summarizing the delta, and for
   strategy (b) note **which amp+cab each snapshot lights up**. Example:
   `Clean (loads first): US Deluxe + clean cab, Mesa muted · Heavy: Mesa + V30, clean muted, Screamer in front`.
7. **Pre-flight the limits** before `generate_preset`: ≤ 8 snapshots, and for (b)
   ≤ 12 blocks/lane and ≤ 2 amp+cab pairs. If over, drop to multiple presets for
   the overflow and tell the user.

## 6. Interaction with `auto-control-wiring` (noted, not designed here)

Snapshots and footswitches/EXP are **both performance controls**, and they
overlap:

- **Snapshots recall a whole part** — many things change at once (amp bypass +
  gain + EQ + delay + reverb). This is the right tool for **parts**.
- **A footswitch toggles one block live**; **EXP sweeps one param**. Right for a
  *single* in-part move (kick a boost, swell a volume) that shouldn't cost a
  whole snapshot.
- **Rule of thumb for the sibling feature:** a change that touches ≥ 2 blocks/
  params → snapshot; a single live on/off or sweep → footswitch/EXP.
- **Handoff constraint:** when a preset uses snapshots-per-part, `auto-control-
  wiring` should map **snapshot recall** to the device's snapshot switches and
  reserve remaining FS for per-block stomps, and it must respect that the snapshot
  count fits the available snapshot switches on the Stadium XL. A "solo boost" is
  the classic ambiguous case — it can be a `Solo` snapshot **or** a momentary FS
  on a drive; the two features must not both claim it. Resolving that ownership is
  `auto-control-wiring`'s job, not this spec's.

## Open questions for the user

1. **Default when parts differ but instant switching isn't stated.** The rule
   defaults "different families, switching-need unstated" to **multiple presets**
   (simpler) rather than layered snapshots. Is that the right default for how you
   actually play, or would you rather always get one preset with layered snapshots
   so everything lives in a single patch even if it costs blocks?
2. **Cap on snapshots the skill will auto-create.** 8 is the hardware max, but
   for a busy song the skill could reasonably stop at, say, 3–4 "big" parts and
   fold the rest in. Do you want it to use all 8 when the song warrants, or keep
   presets lean (≤ 4 snapshots) unless you ask for more?
3. **Layered-chain block budget.** Strategy (b) is capped at 2 amp+cab pairs to
   stay under 12 slots, which means duplicated effects get squeezed. When both
   parts want their own delay/reverb and it won't fit, should the skill (a) share
   one delay/reverb across both parts, or (b) fall back to multiple presets? Which
   compromise do you prefer?
4. **Is the per-snapshot *enable* gap worth closing?** Today (b) works only via
   base-enabled + disable-the-complement. If you foresee wanting a block that's
   off at base and in the first snapshot but on later, that needs a new `enable`
   snapshot field (a separate implementation cycle). Do you want that on the
   roadmap, or is disable-only sufficient for your material?
5. **Snapshot naming source.** Should names follow **song sections** (`Verse`,
   `Chorus`, `Solo`) or **sound descriptions** (`Clean`, `Crunch`, `Lead`)? They
   read differently on the scribble strips depending on whether you think in
   arrangement or in tone.
6. **Ownership of "solo boost" vs `auto-control-wiring`.** When a song has a solo,
   do you want it as its own **snapshot** (recalls a full lead voice) or as a
   **momentary footswitch** boost layered on the current part? This decides how
   the two features split the control surface.
