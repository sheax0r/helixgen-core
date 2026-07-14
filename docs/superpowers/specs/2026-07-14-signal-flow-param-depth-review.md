# Signal-flow param depth (parity #18) — adversarial review + resolutions

**Date:** 2026-07-14 · **Branch:** `feat-signal-flow-params` · An independent
review subagent was prompted to *break* the change (per the project's
adversarial-review rule). 8 findings confirmed; all resolved or explicitly
scoped below. Feature design:
`2026-07-14-signal-flow-param-depth-design.md`.

## Findings → resolutions

1. **HIGH — branch based on stale main.** `main` gained PR #34 (managed-mirror
   sync fix) + PR #36 (device-polarity snapshot-bypass transcode fixes,
   hardware-validated) + 2.21.0 while this branch was in flight; the diff
   overlapped in `device/transcode.py` / `bridge.py`. **Fixed:** rebased onto
   `b1f7070` (one conflict in `_synthesize` resolved keeping BOTH main's
   `_bind_snapshot_targets` and this branch's `inst_z` plumbing); full suite
   re-run green on the rebased branch (1153 passed).
2. **MED — non-dict split/join `params` crashed `parse_spec` with
   TypeError/ValueError** instead of `SpecError`. **Fixed** (type check before
   `dict()`), regression tests added.
3. **MED — unhashable `impedance` value (list) crashed `parse_spec` with
   TypeError.** **Fixed** (`validate_impedance` now requires `str` first),
   regression tests added.
4. **MED — the new CLI docs' own examples failed:** `set-param output level
   -3` is rejected by the option parser. **Fixed:** docs now use the `--`
   sentinel (`set-param t.hsp output level -- -3`, flags before `--`), and
   the sentinel path was executed end-to-end. Follow-on catch from the same
   probe: an int CLI value for a float wire param (`join "A Level" -- -5`)
   was written as an int — **fixed** with `flowparams.coerce_wire_params`
   (schema-kind coercion, the same int-for-float device-corruption guard
   block params already had), applied at parse time and in
   `set_flow_param`; tests added.
5. **LOW/MED — CLAUDE.md overclaimed "chassis input state never leaks".**
   **Fixed:** claim scoped in CLAUDE.md + design spec §3.1 — normalization
   covers spec-path `b00` params + used-jack impedance; an unused jack's
   `instNZ` and an unused chassis flow's input *model* keep chassis values.
6. **LOW — matrix FX-Loop ✅ not exercisable against any real library** (no
   corpus export carries an `HD2_FXLoop*` block, so no exemplar exists in an
   ingested library). **Resolved as a documented caveat** on the matrix row:
   the authoring path is code-complete + test-pinned (synthetic exemplar),
   but a user must first ingest a preset containing an FX Loop. Not a code
   change — library coverage is inherently export-driven for every model.
7. **LOW — pseudo-block `set_param` silently ignored `lane`.** **Fixed:**
   `lane` on `input`/`output`/`split`/`join`/`merge` now raises a clear
   MutateError (address with `path`/`pos`); test added; CLAUDE.md notes it.
8. **LOW — comment overpromised "stays verbatim" for a malformed stereo b00
   channel** (regenerate actually normalizes it to defaults). **Fixed:**
   comment corrected to describe the real behavior.

## Reviewer's did-not-break list (highlights)

Full 211-export corpus: `parse_spec(view(body))` never raises;
`compose∘view` idempotent; no Mic/unknown b00 models, no per-channel b13
params, all `instNZ` values in-enum. Real split presets + snapshot-tracked
b13 gains round-trip and transcode. Stereo authoring → transcode lands
`Pad.1/.2` and per-jack `preset.instN.z` correctly. Synthesized OutputMatrix
is byte-identical to the captured template at defaults. All validation walls
(range/kind/unknown-key/per-channel/jack-scope/split-type agreement) hold in
spec, mutate, and MCP `patch_preset`.

## Round 2 — independent PR #39 review (FIX-FIRST), resolutions

0. **Rebase**: main moved again (PR #37 → 2.22.0); rebased cleanly, suite
   re-run green.
1. **F1 (med)** — `_resolve_jack_impedances` treated an OMITTED impedance as
   an explicit `FirstEnabled` request, so explicit-vs-omitted on a shared
   jack hard-errored (incl. the common default-`both` + explicit-`inst1`
   dual-path shape). **Fixed:** per-jack explicitness tracking — explicit
   wins over omission (both orders); only differing **explicit** values
   conflict. Tests for explicit↔omitted (both orders), default-both +
   explicit-inst1, and per-jack-dict omission.
2. **F2 (med)** — `view._lift_input` lifted `Pad`/`StereoLink` unscoped by
   input mode, so `parse_spec(view(x))` could raise (pad-with-none /
   link-with-mono, reachable via `mutate.set_input`). **Fixed:** lifts now
   mirror parse scoping (pad only with an instrument source, link only with
   `both`); regression tests assert `parse_spec(view(x))` holds.
3. **F3 (low)** — `JOIN_PARAM_SCHEMA` documented the merge master `Level`
   default as 0.0; the device default is **+3 dB**. **Fixed:** constant
   corrected to 3.0; CLAUDE.md + tone skill now warn that an omitted
   `"Level"` comes out 3 dB hot (transcode fills device defaults).
4. **F4 (low)** — `impedance_device_int` silently defaulted unknown strings.
   **Fixed:** stderr warning (mirrors `view`'s unknown-instNZ warning) + test.
5. **F5 (low)** — matrix Output row overclaimed: destination isn't
   authorable. **Fixed:** row split — level/pan ✅, destination 🟡 (verbatim
   structural, deliberate scope).
6. **F6 (docs)** — tone skill "defaults to a Y split" corrected (type/model
   required); CLAUDE.md documents gate-object ⇒ `enabled: true`; the
   impedance-conflict sentence now matches F1's explicit-only semantics
   (CLAUDE.md + design spec §2.1).
