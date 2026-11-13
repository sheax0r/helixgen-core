# Venue variants — design

**Date:** 2026-11-11
**Status:** draft, awaiting user approval
**Repos:** engine → `helixgen-core`; skill + synced docs → `helixgen` (this repo);
library migration → the user's `~/.helixgen` (one-off, not shipped)

## Problem

A tone is voiced for one listening situation and nothing records which. The
same `.hsp` is used through headphones, into the front of a rehearsal-room
amp, and into a DAW, and each of those wants a different end of chain.

The trigger: the user reported that a $130 Blackstar Beam Solo headphone amp
sounded "realer" through headphones than every helixgen tone and every factory
preset on a Helix Stadium XL. The Beam runs its cab sim through a room stage
("In The Room"); Kemper has an equivalent headphone-only "Space" control. Helix
leaves that step to the preset author, and helixgen has never done it: of 126
library tones, none had a room stage after the cab.

Adding a room-mic IR in parallel with the close-mic IR on one tone (My Chemical
Romance — Thank You for the Venom, Ibanez Prestige) closed the gap to the
user's satisfaction; they kept it. **Evidence is one tone and one listener.**
The blend defaults below are a starting point to be tuned, not a measured
optimum.

Three situation-specific habits already exist, all ad hoc:

- `guitar-training/tools/band-variant.py` builds "amp in" copies (cab removed,
  EQ added) as separate logical tones named `<slug>-amp-in`.
- "Doubled" presets (split → two hard-panned IRs + 35 ms Haas delay). The user
  now says these were solving the same headphone problem and wants them gone.
- The headphone room treatment above, built by hand once.

## Goals

1. A variant records its **venue**: what the sound comes out of.
2. One engine operation turns a tone into its form for a given venue,
   deterministically, including the structural edits (split / second IR /
   merge, cab removal) that are error-prone for an agent to do by hand.
3. A library-level **default venue**, so new tones come out right for the
   owner's normal situation without being asked.
4. The `tone` skill knows when to apply a venue, what each one does, and how
   to translate ear feedback about the room into parameter moves.
5. Doubling is removed: from the user's library and device, and from skill
   guidance.

## Non-goals

- Retrofitting every existing tone. A variant with no venue is "unspecified"
  and stays untouched until someone asks.
- Propagating tweaks between sibling variants. See "Sibling policy".
- Stereo room (two room IRs panned L/R). v1 is one mono room mic, centred.
- Per-output solutions (Phones Global EQ and the like). Removing a cab or
  adding an IR needs a different chain, so a venue is a separate preset.
- Auto re-deriving a venue copy when its source changes.

## Anti-goals

- A preset explosion. Venue copies are made **on demand**, never eagerly for
  every tone × guitar × venue.
- A skill that asks questions on every tweak. Defaults are silent.
- Overwriting a hand-tuned variant. Re-running a venue transform onto an
  existing variant is an error, exactly as `library fork` already behaves.

## Decisions made by the user

- Separate copy per situation, not one preset with switches.
- Copies are made on demand.
- Headphones is this user's default, and the default is **configurable at
  the library level**, not hard-coded.
- Metadata records venue and derivation.
- Tweaks do **not** propagate to siblings unless the user explicitly asks. The
  skill does not offer or ask.
- Doubled tones are replaced by room tones and doubling is removed.
- Deterministic rules are acceptable; the interactive tweak loop afterwards
  is expected and is ordinary `set-param` work.

Everything below this line that is not listed above is the author's call and
is open to change.

## Venue vocabulary (v1)

| Venue | Meaning | End-of-chain form |
|---|---|---|
| `direct` | Full-range playback where the room is real or added later: PA, FRFR, studio monitors, DAW recording | Close-mic cab/IR, no room stage. This is what helixgen produces today |
| `headphones` | Headphones from the Helix phones jack | `direct` + room stage |
| `amp-in` | Front input of a real guitar amp | No cab/IR; fizz-taming EQ after the last amp |

`null` (absent) means unspecified and is treated as `direct` when used as a
transform source.

Deliberately left out of v1: a separate `recording` venue (differs from
`direct` only in judgement calls the skill can describe), and power-amp-into-
guitar-cab (cab removed but amp model kept on; add when someone needs it). The
vocabulary is a validated closed set in the engine so adding one is a
deliberate change.

## Data model

Per variant, in `library/tones/<logical-slug>.json` (schema stays 1; both
fields optional and advisory, same rules as `normalized`):

```json
"venue": "headphones",
"venue_detail": {
  "room_ir": "24c8d4c338a11b9d200bcc865e6a965d",
  "room_level_db": -30.0,
  "room_switch": "FS4",
  "ambience_block": "Tile",
  "ambience_switch": "FS5"
}
```

- `venue` — one of the vocabulary values, or absent.
- `venue_detail` — what the transform added, so it can be found again and
  removed. Shape is per venue; for `amp-in` it records the EQ block added.
- Derivation uses the **existing** `forked_from` record. `library fork`
  already writes source tone / variant / `.hsp` / version / date; a venue
  fork adds `"venue_from": "<source venue or null>"` to it. No new
  provenance structure.

What derivation is used for: telling a derived copy from an authored one,
refusing to clobber, and listing a tone's siblings in `describe`. It is not
used to trigger anything.

**Variant keys.** Today a variant is keyed by guitar slug. The key is an
address, not a statement of venue; the `venue` field is the truth. Rule, per
tone and per guitar: the **first** variant on a guitar holds the bare
`<guitar-slug>` key whatever its venue; any later venue copy on that guitar
is keyed `<guitar-slug>@<venue>`. The `.hsp` filename gains `-<venue>` and the
display name a short suffix — ` (HP)`, ` (Direct)`, ` (Amp In)` — only for
those later copies, so a tone's first variant keeps today's clean name.
Existing keys never change. In-place conversion (`library venue`) changes the
`venue` field and the chain only: key, filename and display name stay put.

Engine code that assumes key == guitar slug and must change (found by the
builder check against core 0.58.1): `tone_meta.validate_tone_meta` (rejects
non-guitar keys), `upsert_variant` (derives key and `preset_name` from the
guitar slug alone), `fork_cmd` (`_resolve_guitar(src_key)`), `library doc
--variant` and `guitar_settings_warnings` (profile lookup by key), and
`naming.variant_slug` / `display_name` (no venue parameter). `slugify` cannot
emit `@`, so the new keys cannot collide with a guitar slug. `venue` and
`venue_detail` become real `Variant` fields, like `normalized`.

## Preference

`preferences.json`: `"default_venue": "headphones" | "direct" | "amp-in" | null`,
plus `HELIXGEN_DEFAULT_VENUE`. Validated like `default_guitar`. Absent or null
means today's behaviour: nothing is applied.

## Engine

One transform function, three entry points.

```
helixgen generate <recipe> ... [--venue V]        # author straight into a venue
helixgen library fork <source> --venue V [...]     # new copy for another venue
helixgen library venue <variant> V                 # convert in place
```

- `generate` with no `--venue` uses `default_venue`; `--venue direct` opts out
  explicitly. The recipe itself is always written in `direct` form — the
  transform runs after the chain is built — so recipes stay venue-neutral and
  `view` output of a `direct` tone is unchanged.
- `library fork --venue` composes with `--guitar`. An existing target variant
  is an error.
- `library venue` (in place) exists for the migration and for "make this one
  a headphone tone". It refuses when the variant already has that venue.

### Transform: → `headphones`

Preconditions, on the instrument path (the path carrying the amp; the mic
path is never touched): exactly one cab/IR block downstream of the last amp,
enabled at base and not bypassed by any snapshot, and no split **other than
the USB-tap split** (every tapped tone has a Y split at the end of lane 0
feeding the USB output; that one is expected and must be preserved). There
must also be room in lane 0 for the split and merge ahead of the tap.
Otherwise fall back (below). If Tile alone does not fit, omit it with a
warning.

`mutate.py` has no primitive for this today: `add_block` / `remove_block`
refuse any parallel-routed path. The transform needs new split / lane-1 /
merge insertion and removal code. Because headphones is a default venue,
that refusal would otherwise lock every new tone out of `add-block` /
`remove-block`, so **in scope for v1:** those verbs must work on lane 0
outside the split region of a room-treated path. Editing inside the split
stays refused.

Where the close IR's `Level` (or anything else the transform writes) is held
in per-snapshot arrays, write all eight slots.

1. Insert a Y split immediately before the close cab/IR and a merge
   immediately after it. Merge: `A Level 0`, `B Level 0`, both pans centre,
   master `Level 0.0` (the device default is +3 dB; unity must be written).
2. Lane B gets one `IR` block loaded with the **room IR**.
3. Set the room IR's `Level` so its energy sits `room_offset_db` under the
   close IR's: `close_level − (E_room − E_close) − room_offset_db`, where `E`
   is `10·log10(Σx²)` of each wav. Default `room_offset_db` = 4. (On the
   reference tone: close −18, energies 7.3 vs 15.2 dB, result −30.)
4. Wire the first free assignable footswitch as a **param toggle** on the room
   IR's `Level`, `min −60` / `max <computed>`, label `ROOM MIC`. Never a
   bypass toggle: bypassing the IR passes uncabbed amp into the merge.
5. Append `Tile` at the end of the chain, **disabled**, `Decay 0.2`,
   `Mix 0.2`, on the next free footswitch, label `TILE`.
6. No free footswitch → skip that wiring with a warning; the block/level is
   still written.

Room IR selection, deterministic:

- Candidates are registered IRs whose filename shares the close IR's pack/cab
  prefix (everything before the mic/mix token) and contains `ROOM`.
- Prefer the natural-phase copy (keeps the mic's distance delay; room IRs are
  not minimum-phase). Address it by **hash**, because the same basename exists
  in several phase folders.
- Several candidates → the highest-energy one. `--room-ir <hash|basename>`
  overrides.

Fallback (stock cab, no matching room IR, a split already present, or
`--room tile`): skip steps 1–4 and add `Tile` **enabled**. Print which
fallback ran and why. This is the weaker form — it adds stereo ambience but
no room-mic depth — and `venue_detail` records it.

### Transform: → `direct`

From `headphones`: remove exactly what `venue_detail` recorded (room IR lane,
split, merge, Tile, their footswitches). A `headphones` variant with no
`venue_detail` cannot be reversed mechanically: error, and say so. The same
applies when the chain no longer matches `venue_detail` (room IR swapped,
switches re-wired, blocks added inside the split): error naming the mismatch,
no best-effort removal. The round-trip test asserts the same blocks, params
and order, not identical grid positions.
From `amp-in`: not supported in v1 (the cab is gone); error naming the source
to fork from instead.

### Transform: → `amp-in`

A port of `band-variant.py`: remove every cab/IR block (and, coming from
`headphones`, the room stage first), then add `Parametric Mono` after the last
amp with `LowCut 90`, `HighCut 5500`, 18 dB/oct slopes.

### USB taps

The transform must preserve USB recording taps structurally. It cannot rely
on the `usb_taps` preference to re-apply them: that key is not set in the
user's `preferences.json`. A venue copy with broken taps is a regression.

The legacy `generate -o` path writes no metadata and gets no venue treatment;
`default_venue` applies only to the library flow.

## Sibling policy

A tweak lands on the variant named and nowhere else. The skill does not
offer to copy it to siblings and does not ask. If the user says "do that to
the amp-in one too", the skill applies the same patch op to that variant.
`describe` lists siblings so the agent can resolve "the amp-in one".

## Skill changes (`tone`)

A new "Venue" section, replacing all doubling guidance. Content, distilled
from the session that produced this spec:

- **Why.** A close-mic IR is a near-field capture; through headphones it
  sounds like a recording, not an amp. Other modelers add a room stage for
  headphones. Helix does not.
- **When.** Resolve venue like guitar: named by the user → `default_venue` →
  unspecified. State it in one clause in the report. Never ask on a routine
  tone request.
- **How.** Author the recipe in `direct` form and let `generate --venue` do
  the transform. Never hand-build the split.
- **What to listen for / ear-language table:**
  - "too roomy / washy / distant" → room IR `Level` down 2–3 dB
  - "can't hear the difference" → room IR `Level` up 2–3 dB
  - "low end thins out when the room is on" → phase cancellation against the
    close mic; swap to the minimum-phase copy of the same room IR
  - "wants width, not depth" → enable Tile; the room mic is mono
  - "Tile is fatiguing" → lower `Mix`, or leave it off; some players dislike it
- **Do not** raise `HighCut`, add plate/hall, or reach for a different amp to
  fix "doesn't sound real in headphones". That is the room stage's job.
- **Other venues.** `amp-in`: cab off, EQ on, real amp set clean and flat.
  `direct` for a PA: check it in mono, tighten the low cut, pull reverb and
  delay mix back because the real room adds its own. `direct` for recording:
  judge by the recorded track, not an interface's direct monitor.
- **Siblings.** The policy above, stated as a rule.
- The "Common mistakes" table gains: hand-building a room split; bypass-
  toggling a parallel IR; applying a room stage to a tone bound for a PA.

Doubling: delete the technique wherever the plugin's skills or docs describe
it. (The user's own notes carry it too; that is handled outside this repo.)

`docs/CLI.md` and `docs/recipe-reference.md` are re-synced from core.

## Library migration (user's `~/.helixgen`, after the engine ships)

1. Set `default_venue: "headphones"`.
2. Mark the hand-built Venom Ibanez variant `venue: headphones` with its
   `venue_detail` (room IR `24c8d4c3…`, Level −30, FS4, Tile FS5).
3. For each of the 10 doubled presets (7 songs; every one has a plain
   counterpart on the same guitar): run `library venue <plain variant>
   headphones` in place, push it to the `Mike` setlist, then remove the
   doubled preset from the device (`device rm --also-pool`) and delete its
   `.hsp` and metadata. Venom Ibanez plain is already done; only its doubled
   twin needs deleting.
4. Retire `guitar-training/tools/band-variant.py` in favour of
   `library fork --venue amp-in`. Existing `<slug>-amp-in` logical tones stay
   as they are; converting them into venue variants is optional follow-up.

## Testing

- Core: transform unit tests per venue; `direct → headphones → direct`
  round-trips to an identical chain; fallback paths; refusal cases (existing
  split, missing `venue_detail`, existing target variant); room-IR selection
  with duplicate basenames; USB taps survive; preference parsing; CLI parity
  for the new flags and verb.
- Plugin: skill-doc content checks updated; no remaining doubling references.
- Hardware: one `headphones` tone copied to the device and confirmed to load
  with the room IR present (`device list-irs`), footswitch toggling audibly.

## Left to the builder

- Exact `venue_detail` field names and how blocks are addressed in it.
- Whether `library venue` is its own verb or a flag on an existing one.
- How the pack/cab prefix is parsed from IR filenames (IR metadata under
  `library/irs` may already carry it).
- Release sequencing across the two repos, per each repo's CLAUDE.md.
