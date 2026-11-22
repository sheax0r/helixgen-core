"""Registry entry: factory meta.info clear (Task 5).

Folds `library info-backfill` into the registry: a tone whose `meta.info`
exactly matches the chassis donor string is factory leakage; clear it. A note
the user set is left alone.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from helixgen import changes, changes_entries, home, tone_meta  # noqa: F401
from helixgen.cli import cli
from helixgen.hsp import dumps_hsp, read_hsp

DONOR = "Factory Rig 12 — do not edit"


def _entry():
    (e,) = [e for e in changes.REGISTRY if e.id == "factory-info-clear"]
    return e


def _seed(slug: str, info: str) -> Path:
    body = {"meta": {"name": slug, "info": info}, "preset": {"flow": [{}]}}
    hsp = home.tones_dir() / f"{slug}.hsp"
    hsp.parent.mkdir(parents=True, exist_ok=True)
    hsp.write_bytes(dumps_hsp(body, apply_prefs=False))
    tone_meta.save_tone_meta(tone_meta.ToneMeta(
        artist=None, song=None, descriptor=slug, tags=[], description_md=None,
        variants={"generic": tone_meta.Variant(hsp=f"tones/{slug}.hsp",
                                                preset_name=slug)},
        created="2026-01-01", updated="2026-01-01", schema=1))
    return hsp


def _seed_chassis():
    home.library_dir().mkdir(parents=True, exist_ok=True)
    (home.library_dir() / "chassis.json").write_text(
        json.dumps({"meta": {"info": DONOR}}))


def test_entry_registered():
    assert _entry().kind == "cosmetic"


def test_upgrade_clears_donor_info_leaves_user_note(tmp_home):
    _seed_chassis()
    donor = _seed("leaked", DONOR)
    user = _seed("mine", "my own setup notes")

    dry = CliRunner().invoke(cli, ["library", "upgrade", "--only", "factory-info-clear"])
    assert dry.exit_code == 0, dry.output
    assert "leaked" in dry.output and "would-clear" in dry.output

    res = CliRunner().invoke(
        cli, ["library", "upgrade", "--only", "factory-info-clear", "--apply"])
    assert res.exit_code == 0, res.output
    assert read_hsp(donor)["meta"]["info"] == ""
    assert read_hsp(user)["meta"]["info"] == "my own setup notes"

    # idempotent
    again = CliRunner().invoke(
        cli, ["library", "upgrade", "--only", "factory-info-clear", "--apply"])
    assert "0 tone" in again.output
