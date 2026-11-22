"""Registry entry: headphones room-mic High Cut retrofit (Task 6).

Folds `library venue-retrofit` into the registry. Mirrors the existing
venue-retrofit CLI test: an old wide-open headphones variant is clamped to the
High Cut ceiling under `library upgrade --apply`; a non-headphones variant is
left alone.
"""
from __future__ import annotations

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.hsp import read_hsp
# reuse the corpus-IR fixtures + helpers the venue-retrofit test already defines
from tests.test_venue import CLOSE, ROOM, _chain, irs, lib, needs_sf  # noqa: F401
from tests.test_cli_venue_headphones import _gen, _ir_hc, _meta, _write


def _upgrade(lib, irs, *extra):
    return CliRunner().invoke(cli, [
        "library", "upgrade", "--only", "headphones-highcut",
        "--library", str(lib.root), "--irs-dir", str(irs.irs_dir), *extra])


@needs_sf
def test_upgrade_retrofits_wide_open_headphones_dry_then_apply(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    rec = _write(tmp_path, _chain())
    # old-style headphones variant: room mic wide open
    assert _gen(lib, irs, rec, "--venue", "headphones",
                "--room-highcut", "off").exit_code == 0
    # a plain, non-headphones tone that must be left untouched
    assert _gen(lib, irs, _write(tmp_path, _chain(), "p2.json"),
                descriptor="Plain").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    assert _ir_hc(read_hsp(probe), ROOM) == 20100.0

    dry = _upgrade(lib, irs)
    assert dry.exit_code == 0, dry.output
    assert "1 variant" in dry.output
    assert _ir_hc(read_hsp(probe), ROOM) == 20100.0          # nothing written

    ap = _upgrade(lib, irs, "--apply")
    assert ap.exit_code == 0, ap.output
    assert _ir_hc(read_hsp(probe), ROOM) == 8000.0
    assert _ir_hc(read_hsp(probe), CLOSE) == 8000.0
    assert _meta("probe").variants["generic"].venue_detail["room_highcut"] == 8000.0

    # idempotent: nothing left, no spurious change
    again = _upgrade(lib, irs, "--apply")
    assert again.exit_code == 0, again.output
    assert "0 variant" in again.output
