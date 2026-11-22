"""`helixgen library upgrade` — the registry driver (Task 4).

Scans the managed tone library, reports exact tones/variants/blocks affected,
and applies in place only under --apply (idempotent, consent-gated).
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from helixgen import home, tone_meta
from helixgen.cli import cli
from helixgen.generate import HSP_SNAPSHOT_SLOTS
from helixgen.hsp import dumps_hsp, read_hsp


def _write_tone(slug: str, enabled: dict) -> Path:
    """Create a one-variant tone whose single block carries ``enabled`` as its
    ``@enabled`` wrapper; returns the .hsp path."""
    body = {"meta": {"name": slug, "info": ""},
            "preset": {"flow": [{"b01": {"@enabled": enabled,
                                         "slot": {"0": {"model": "x"}}}}]}}
    hsp_path = home.tones_dir() / f"{slug}.hsp"
    hsp_path.parent.mkdir(parents=True, exist_ok=True)
    hsp_path.write_bytes(dumps_hsp(body, apply_prefs=False))
    meta = tone_meta.ToneMeta(
        artist=None, song=None, descriptor=slug, tags=[], description_md=None,
        variants={"generic": tone_meta.Variant(hsp=f"tones/{slug}.hsp",
                                                preset_name=slug)},
        created="2026-01-01", updated="2026-01-01", schema=1)
    tone_meta.save_tone_meta(meta)
    return hsp_path


def _run(*args):
    return CliRunner().invoke(cli, ["library", "upgrade", *args])


def _seed_stale_and_clean():
    stale = _write_tone("stale", {"value": False, "controller": {"source": 5}})
    _write_tone("clean", {"value": True, "controller": {"source": 6},
                          "snapshots": [True] * HSP_SNAPSHOT_SLOTS})
    return stale


def test_dry_run_reports_counts_and_writes_nothing(tmp_home):
    stale = _seed_stale_and_clean()
    before = stale.read_bytes()
    res = _run()
    assert res.exit_code == 0, res.output
    assert "1 tone" in res.output and "1 variant" in res.output and "1 block" in res.output
    assert stale.read_bytes() == before  # dry run wrote nothing


def test_apply_migrates_then_is_idempotent(tmp_home):
    stale = _seed_stale_and_clean()
    res = _run("--apply")
    assert res.exit_code == 0, res.output
    assert read_hsp(stale)["preset"]["flow"][0]["b01"]["@enabled"]["snapshots"] == \
        [False] * HSP_SNAPSHOT_SLOTS
    # second apply: nothing left to do
    res2 = _run("--apply")
    assert res2.exit_code == 0, res2.output
    assert "0 tone" in res2.output


def test_json_shape(tmp_home):
    _seed_stale_and_clean()
    res = _run("--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["apply"] is False
    assert payload["affected"] == {"tones": 1, "variants": 1, "blocks": 1}
    assert any(r["change"] == "fs-bypass-snapshot-assign" for r in payload["rows"])


def test_only_unknown_id_errors(tmp_home):
    _seed_stale_and_clean()
    res = _run("--only", "no-such-change")
    assert res.exit_code != 0
    assert "no-such-change" in res.output


def test_empty_library_is_clean(tmp_home):
    res = _run()
    assert res.exit_code == 0, res.output
    assert "0 tone" in res.output


def test_corrupt_hsp_is_skipped_and_other_tones_still_repaired(tmp_home):
    stale = _seed_stale_and_clean()  # valid stale (b-slug) + clean
    # a tone whose .hsp is unreadable (valid magic, junk JSON)
    bad = home.tones_dir() / "broken.hsp"
    bad.write_bytes(b"rpshnosj{not json")
    tone_meta.save_tone_meta(tone_meta.ToneMeta(
        artist=None, song=None, descriptor="broken", tags=[], description_md=None,
        variants={"generic": tone_meta.Variant(hsp="tones/broken.hsp",
                                                preset_name="broken")},
        created="2026-01-01", updated="2026-01-01", schema=1))

    res = _run("--apply")
    assert res.exit_code == 0, res.output              # one bad tone must not abort
    assert "broken" in res.output and "skipped" in res.output.lower()
    # the healthy stale tone was still repaired
    assert read_hsp(stale)["preset"]["flow"][0]["b01"]["@enabled"]["snapshots"] == \
        [False] * HSP_SNAPSHOT_SLOTS


def test_entry_exception_during_apply_degrades_to_skipped(tmp_home, monkeypatch):
    _seed_stale_and_clean()
    from helixgen import changes

    def _boom(ctx):
        raise KeyError("room block not in this library")

    boom = changes.ChangeEntry(id="boom", landed_in="0.99.0", kind="bugfix",
                               summary="x", research="y",
                               detect=_boom, apply=_boom)
    monkeypatch.setattr(changes, "REGISTRY", changes.REGISTRY + [boom])
    res = _run("--apply")
    assert res.exit_code == 0, res.output
    assert "skipped" in res.output.lower()


def test_missing_hsp_is_skipped_not_crashed(tmp_home):
    _write_tone("stale", {"value": False, "controller": {"source": 5}})
    (home.tones_dir() / "stale.hsp").unlink()  # meta points at a now-missing .hsp
    res = _run()
    assert res.exit_code == 0, res.output
    assert "missing" in res.output.lower()
