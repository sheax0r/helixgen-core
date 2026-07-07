# Volume-normalization pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a final level-balancing pass to the `tone` skill (relative between-snapshot leveling + a consistent across-preset baseline anchor), with two preference toggles to opt out of either.

**Architecture:** Two typed opt-out keys added to the existing `preferences.py` module (mirroring `favor_irs`), plus a new prose step 5.7 in the `tone` skill that reads those prefs and applies a three-force heuristic (anchor → gain-compensation → intended dynamics) via the amp's channel-volume param.

**Tech Stack:** Python 3 stdlib (`preferences.py`); Markdown skill prose. Tests: `pytest`, run with `PYTHONPATH=$PWD/src`.

## Global Constraints

- Run tests with `PYTHONPATH=$PWD/src pytest`.
- Pure stdlib + `click` only — no new dependencies.
- TDD for the code task; prose task has no tests.
- Work stays on branch `feature/volume-normalization` (already created). Never commit to `main`.
- Both preference keys default `true` (normalization on by default).
- Spec of record: `docs/superpowers/specs/2026-07-06-volume-normalization-design.md`.

---

### Task 1: Preference opt-out keys (`volume_normalize_snapshots`, `volume_normalize_baseline`)

Add two typed boolean preferences (both default `True`), following the exact pattern of the existing `favor_irs` / `guard_paid_irs_in_git` keys: dataclass field, `load_preferences` file read, per-key env override, and scaffold default.

**Files:**
- Modify: `src/helixgen/preferences.py` — `Preferences` dataclass (~line 116-127), `load_preferences` file-read block (~line 183-192) and env-override block (~line 194-211), `_default_scaffold_dict` (~line 216-230)
- Test: `tests/test_preferences.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Preferences.volume_normalize_snapshots: bool = True` and `Preferences.volume_normalize_baseline: bool = True`; JSON keys `volume_normalize_snapshots` / `volume_normalize_baseline`; env overrides `HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS` / `HELIXGEN_VOLUME_NORMALIZE_BASELINE` (parsed via the existing `_parse_bool_env`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_preferences.py` (mirror the existing `favor_irs` tests in that file for fixture/monkeypatch style — use `tmp_path` for the file and `monkeypatch.delenv(..., raising=False)` to isolate env):

```python
def test_volume_normalize_defaults_true(tmp_path):
    from helixgen.preferences import load_preferences
    prefs = load_preferences(tmp_path / "nope.json")  # missing file -> defaults
    assert prefs.volume_normalize_snapshots is True
    assert prefs.volume_normalize_baseline is True


def test_volume_normalize_from_file(tmp_path):
    import json
    from helixgen.preferences import load_preferences
    p = tmp_path / "preferences.json"
    p.write_text(json.dumps({
        "volume_normalize_snapshots": False,
        "volume_normalize_baseline": False,
    }))
    prefs = load_preferences(p)
    assert prefs.volume_normalize_snapshots is False
    assert prefs.volume_normalize_baseline is False


def test_volume_normalize_env_overrides_file(tmp_path, monkeypatch):
    import json
    from helixgen.preferences import load_preferences
    p = tmp_path / "preferences.json"
    p.write_text(json.dumps({"volume_normalize_snapshots": True}))
    monkeypatch.setenv("HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS", "0")
    monkeypatch.delenv("HELIXGEN_VOLUME_NORMALIZE_BASELINE", raising=False)
    prefs = load_preferences(p)
    assert prefs.volume_normalize_snapshots is False   # env beat the file
    assert prefs.volume_normalize_baseline is True      # default


def test_volume_normalize_in_scaffold(tmp_path):
    import json
    from helixgen.preferences import scaffold_default
    path = scaffold_default(tmp_path / "preferences.json")
    data = json.loads(path.read_text())
    assert data["volume_normalize_snapshots"] is True
    assert data["volume_normalize_baseline"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_preferences.py -k volume_normalize -v`
Expected: FAIL — `Preferences` has no `volume_normalize_*` attributes; scaffold lacks the keys.

- [ ] **Step 3: Add the dataclass fields**

In `src/helixgen/preferences.py`, add to the `Preferences` dataclass (after `instruments`):

```python
    instruments: list[Instrument] = field(default_factory=list)
    volume_normalize_snapshots: bool = True
    volume_normalize_baseline: bool = True
```

- [ ] **Step 4: Read the keys in `load_preferences`**

In the `Preferences(...)` construction inside `load_preferences`, add the two keys (after `instruments=...`):

```python
        instruments=_parse_instruments(data.get("instruments")),
        volume_normalize_snapshots=bool(data.get("volume_normalize_snapshots", True)),
        volume_normalize_baseline=bool(data.get("volume_normalize_baseline", True)),
    )
```

Then add the env overrides (after the `HELIXGEN_AUTHOR` block, before the instruments comment):

```python
    if "HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS" in os.environ:
        prefs.volume_normalize_snapshots = _parse_bool_env(
            "HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS",
            os.environ["HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS"],
        )
    if "HELIXGEN_VOLUME_NORMALIZE_BASELINE" in os.environ:
        prefs.volume_normalize_baseline = _parse_bool_env(
            "HELIXGEN_VOLUME_NORMALIZE_BASELINE",
            os.environ["HELIXGEN_VOLUME_NORMALIZE_BASELINE"],
        )
```

- [ ] **Step 5: Add the keys to the scaffold default**

In `_default_scaffold_dict`, add both keys (after `instruments`):

```python
        "instruments": [],
        "volume_normalize_snapshots": True,
        "volume_normalize_baseline": True,
    }
```

- [ ] **Step 6: Run the new tests + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_preferences.py -k volume_normalize -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add src/helixgen/preferences.py tests/test_preferences.py
git commit -m "feat(preferences): volume_normalize_snapshots/baseline opt-out keys

Two typed bool prefs (default true) let the user skip the tone skill's
between-snapshot relative leveling and/or the across-preset baseline anchor,
with HELIXGEN_VOLUME_NORMALIZE_* env overrides. Backlog item 6.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 2: Tone skill — step 5.7 volume-normalization pass

Add the prose step to `.claude/skills/tone/SKILL.md` that reads the two prefs and applies the three-force heuristic, plus the report line and a Common Mistakes row.

**Files:**
- Modify: `.claude/skills/tone/SKILL.md` — insert a new `### 5.7` after the existing `### 5.6` (auto-wiring); update step 8 (report) and step 7a (companion `.md`); add a Common Mistakes row.

**Interfaces:**
- Consumes: the prefs keys from Task 1 (read from `~/.helixgen/preferences.json`: `volume_normalize_snapshots`, `volume_normalize_baseline`).
- Produces: nothing code-facing (prose guidance).

- [ ] **Step 1: Insert step 5.7**

After the `### 5.6` section in `.claude/skills/tone/SKILL.md`, insert:

```markdown
### 5.7. Volume-normalization pass

A final level pass so the preset's loudness is sane and — especially when
replicating a reference — the **relative** loudness between parts/snapshots
tracks the source. helixgen never renders audio, so this sets **starting**
levels by rule of thumb; the user fine-tunes by ear on the device.

**Read the preferences first** (`~/.helixgen/preferences.json`). Two toggles,
both default on:
- `volume_normalize_baseline: false` → skip force 1 (the across-preset anchor).
- `volume_normalize_snapshots: false` → skip forces 2–3 (between-snapshot
  leveling). If both are false, skip this step and say so in the report.

**The knob:** `show_block` the amp and use its channel-volume param (`ChVol`, or
the amp's `Level` — the name varies, so confirm). Do **not** use `Master` (it
also changes power-amp sag/feel). Only if the amp has no channel-volume param,
add one end-of-chain volume block (from `list_blocks(category="volume")`) and
automate that. In a layered two-amp preset, level whichever amp is active in
each snapshot via that amp's own channel volume.

Apply three forces, in order:

1. **Anchor** (force 1, `volume_normalize_baseline`): set the reference part
   (usually rhythm) to a standard channel-volume anchor, default `~0.5` (leaves
   headroom, no clipping; adjust if `show_block` shows an unusual taper). Every
   preset anchoring its main part to the same value keeps presets at a
   consistent baseline. If research says the source should sit hotter/softer
   relative to its material, offset the anchor.
2. **Gain compensation** (force 2, `volume_normalize_snapshots`): more gain →
   more compression → louder *perceived* level at the same knob. So push
   **lower-gain parts up** to sit even — a clean/edge-of-breakup part usually
   needs its channel volume raised to match a high-gain rhythm; a very hot,
   highly-compressed rhythm may need a small trim.
3. **Intended dynamics** (force 3, `volume_normalize_snapshots`), relative to the
   rhythm anchor: **lead/solo ~+2–3 dB** (to cut through), **crunch ~= rhythm**,
   **clean = perceptually matched** (via force 2). When step-1b research reveals
   the source's actual part-to-part dynamics, those override these conventions.

**dB → param:** the knobs are 0–1 and we can't measure — use *a small channel-vol
nudge (~0.05–0.10) ≈ a couple dB* to turn intended dB deltas into starting
values. Per-snapshot moves become `params` overrides on the channel-volume param
(alongside the gain/EQ/effect deltas from 5.5); a base preset gets the anchor on
its base amp params.
```

- [ ] **Step 2: Add the "Levels" line to the report (step 8) and companion `.md` (step 7a)**

In `### 8. Report back`, add a bullet after the Snapshots bullet:

```markdown
- **Levels** — one line on the *intended* relative balance, e.g. `Levels: rhythm
  anchor; lead +~2 dB; clean bumped to match (fine-tune by ear)`. If
  normalization was skipped by preference, say so (`Levels: normalization off per
  preferences`).
```

In `#### 7a. Write a companion markdown description`, add to its bullet list:

```markdown
- **Levels** — the intended relative balance line from step 8 (or that
  normalization was off per preferences)
```

- [ ] **Step 3: Add a Common Mistakes row**

In the `## Common Mistakes` table, add:

```markdown
| Leaving a clean/low-gain part at the same level knob as a high-gain part and calling it balanced | High gain reads louder (more compression); push the lower-gain part's channel volume up to sit even — the volume-normalization pass (5.7), gain-compensation force |
```

- [ ] **Step 4: Sanity-check the skill reads cleanly**

Run: `grep -n "5.7\|volume_normalize\|Levels" .claude/skills/tone/SKILL.md`
Expected: the new 5.7 heading, both pref keys, and the Levels report/companion lines are present; step numbering (5.5 → 5.6 → 5.7 → 6) is contiguous.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/tone/SKILL.md
git commit -m "feat(tone-skill): step 5.7 volume-normalization pass

Anchor + gain-compensation + intended-dynamics leveling via the amp channel
volume; reads volume_normalize_snapshots/baseline prefs to opt out; adds a
Levels report line and a Common Mistakes row. Backlog item 6.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

## Self-Review

**Spec coverage:**
- Two targets (relative + absolute) → forces in Task 2 step 1. ✓
- No-render constraint / starting-values-by-ear → stated in 5.7 + the report line. ✓
- Knob = channel volume via show_block, not Master; volume block fallback → Task 2 step 1. ✓
- Three forces (anchor / gain-comp / intended dynamics), research override → Task 2 step 1. ✓
- dB→param rough mapping → Task 2 step 1. ✓
- Preference opt-out keys (both default true, env-overridable, scaffolded) → Task 1. ✓
- Step 5.7 placement + report/companion "Levels" line + Common Mistakes row → Task 2. ✓
- Out-of-scope (code estimator, device loop, Master) → not built. ✓

**Placeholder scan:** every code step shows full code; commands + expected results given. No TBD/handle-edge-cases. ✓

**Type consistency:** `volume_normalize_snapshots` / `volume_normalize_baseline` (bool, default True) used identically in dataclass, load, env, scaffold (Task 1) and referenced by the same names in the skill prose (Task 2). Env names `HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS/BASELINE` consistent. ✓
