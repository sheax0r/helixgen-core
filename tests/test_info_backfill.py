"""hgc-jeo: backfill that clears factory donor ``meta.info`` from tones
authored before hgc-a8p, while preserving a real per-preset note.

The factory-donor string is sourced from the library's own ``chassis.json``
(``extract_chassis_from_hsp`` preserves the donor's top-level ``meta``), so a
tone whose ``meta.info`` matches it exactly is factory leakage; anything else
is a user note and is left untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from helixgen.cli_library import (
    _factory_donor_infos,
    _scan_tones_for_factory_info,
    library,
)
from helixgen.hsp import read_hsp, write_hsp

FACTORY = (
    "Instrument: Les Paul (all pickups) / Playback System: Studio headphones "
    "/ This is a 2-guitar starting point..."
)
USER_NOTE = "My lead tone for the bridge solo -- crank the mids."


def _make_library(root: Path, tones: dict[str, str]) -> None:
    """A library root with a chassis carrying FACTORY info and one tone per
    ``{filename: info}`` entry."""
    (root / "chassis.json").write_text(
        json.dumps({"meta": {"name": "donor", "info": FACTORY},
                    "preset": {"flow": []}, "_helixgen_chassis_shape": "hsp"})
    )
    tdir = root / "tones"
    tdir.mkdir()
    for fname, info in tones.items():
        write_hsp(tdir / fname, {"meta": {"name": fname, "info": info},
                                 "preset": {"flow": []}})


def test_factory_donor_infos_from_chassis(tmp_path: Path):
    _make_library(tmp_path, {})
    assert _factory_donor_infos(tmp_path) == {FACTORY}


def test_factory_donor_infos_empty_when_no_chassis(tmp_path: Path):
    (tmp_path / "tones").mkdir()
    assert _factory_donor_infos(tmp_path) == set()


def test_factory_info_cleared_user_note_and_empty_preserved(tmp_path: Path):
    _make_library(tmp_path, {
        "factory.hsp": FACTORY,     # (a) matches donor -> cleared
        "mine.hsp": USER_NOTE,      # (b) user note      -> preserved
        "blank.hsp": "",            # (c) already empty  -> no-op
    })
    tones = tmp_path / "tones"

    rows = _scan_tones_for_factory_info(
        tmp_path, _factory_donor_infos(tmp_path), apply=True)

    assert read_hsp(tones / "factory.hsp")["meta"]["info"] == ""
    assert read_hsp(tones / "mine.hsp")["meta"]["info"] == USER_NOTE
    assert read_hsp(tones / "blank.hsp")["meta"]["info"] == ""
    # factory.hsp is the only row that was cleared.
    cleared = [r for r in rows if r["status"] == "cleared"]
    assert [r["file"] for r in cleared] == ["factory.hsp"]


def test_dry_run_writes_nothing(tmp_path: Path):
    _make_library(tmp_path, {"factory.hsp": FACTORY, "mine.hsp": USER_NOTE})
    tones = tmp_path / "tones"
    before = {p.name: p.read_bytes() for p in tones.glob("*.hsp")}

    rows = _scan_tones_for_factory_info(
        tmp_path, _factory_donor_infos(tmp_path), apply=False)

    after = {p.name: p.read_bytes() for p in tones.glob("*.hsp")}
    assert after == before  # (d) dry run mutates no file
    assert any(r["status"] == "would-clear" and r["file"] == "factory.hsp"
               for r in rows)


def test_cli_dry_run_is_default_and_apply_writes(tmp_path: Path):
    _make_library(tmp_path, {"factory.hsp": FACTORY})
    tones = tmp_path / "tones"
    before = (tones / "factory.hsp").read_bytes()

    runner = CliRunner()
    res = runner.invoke(library, ["info-backfill", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tones / "factory.hsp").read_bytes() == before  # default = dry run

    res = runner.invoke(library, ["info-backfill", str(tmp_path), "--apply"])
    assert res.exit_code == 0, res.output
    assert read_hsp(tones / "factory.hsp")["meta"]["info"] == ""
