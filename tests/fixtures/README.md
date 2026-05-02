# Test fixtures

These files encode the *hypothesized* shape of Helix `.hlx` exports and
`sensorium/phelix` block-export JSON. They are synthetic, not real exports.

If real exports differ from these shapes, update the fixtures and the code
that consumes them together. The hot spots are:

- Block model identification: assumed at top-level key `"@model"`
- Block enabled flag: assumed at `"@enabled"`
- Param keys: assumed top-level on the block JSON, excluding any key
  starting with `@`
- Preset top-level: `version`, `schema`, `data.device`, `data.meta`,
  `data.tone.dsp0.blocks`, `data.tone.dsp1.blocks`
- Block position keys inside a `blocks` dict: assumed `dsp0_block_0`,
  `dsp0_block_1`, ... — but the chassis preserves whatever the source
  preset uses, so deviations are tolerated as long as the keys are stable.

Real exports go in `tests/fixtures/presets/real/` and are gitignored if
they contain identifying info. The Goldfinger spec lives at
`tests/fixtures/specs/goldfinger.json`.
