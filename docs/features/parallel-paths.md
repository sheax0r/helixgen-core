# Deferred feature: parallel A/B paths within a chain

## Status

Deferred from v1. v1 generates serial chains only. The Goldfinger reference
preset is fully serial, so parallel routing is not on the MVP critical path.

## What's missing

Helix lets you split the signal inside a single DSP path into two parallel
sub-chains (A and B) and merge them back together. Common uses:

- Two amps in parallel (e.g., clean + dirty, blended at the merge)
- Wet/dry split with different effects on each side
- Stereo widening with different cabs L/R

v1 only supports a straight serial chain per DSP path. Top-level multiple
chains (`paths[0]` on dsp0, `paths[1]` on dsp1) **are** supported in v1 —
the deferred piece is parallel routing **inside** one chain.

## Proposed spec extension (forward-compatible with v1)

A pure-serial spec stays unchanged. To express a parallel section, replace a
single block entry with a `parallel` entry whose value is two sub-chains:

```json
{
  "name": "Wet/Dry rig",
  "paths": [
    {
      "blocks": [
        { "block": "Noise Gate" },
        { "block": "Scream 808" },
        {
          "parallel": [
            [ { "block": "Brit 2204" }, { "block": "4x12 Greenback 25" } ],
            [ { "block": "US Deluxe Nrm" }, { "block": "1x12 Field Coil" } ]
          ]
        },
        { "block": "Plate Reverb" }
      ]
    }
  ]
}
```

The `parallel` entry implies the split before and the merge after. Mixer
parameters (pan, level, polarity per side) can be added later as a sibling
field, e.g. `"merge": { "A": { "pan": -100, "level": 0 }, "B": { "pan": 100, "level": 0 } }`.

The v1 generator should reject any spec containing a `parallel` entry with a
clear "parallel paths not yet supported in v1" error, so old code fails loudly
rather than silently flattening.

## What needs to be discovered

The `.hlx` JSON shape for a split/merge is not yet documented in this repo.
Before implementing, ingest at least one preset that uses A/B split and study:

- How the split point is encoded (a dedicated `split` block? a flag on the
  block where the split occurs? path-position metadata?)
- How A vs B membership is recorded for blocks inside the parallel section
- What the merge block looks like and what parameters it carries (pan, level,
  polarity, low cut, high cut)
- Whether dsp0 and dsp1 use the same split/merge encoding

The chassis extracted in v1 is a serial-only example, so a separate
"parallel chassis" exemplar will likely be needed.

## Implementation notes for a future session

1. Extend the spec validator to accept `parallel` entries.
2. Add a "parallel chassis" exemplar to the library (extracted from a real
   exported preset that uses A/B split).
3. Teach the generator to emit split markers, place blocks into the correct
   sub-path, and emit a merge block with mixer params.
4. Add a CLI test using a real exported parallel preset round-trip:
   ingest → re-generate from spec → diff blocks (not byte-identical, but
   structurally equivalent).
5. Update the v1 design spec to reflect that parallel is now supported, or
   write a new spec for the extension.

## Related

- `sensorium/phelix` `blocks/` directory — may already include split/merge block exemplars worth studying.
