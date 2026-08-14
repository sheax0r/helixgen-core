"""CLI surface for `helixgen device to-hsp`.

The verb's own contract: how a SOURCE is classified (local path vs device CID),
what it does with input it cannot read, and what `--verify` actually claims.
Fully offline — every test uses a `.sbe` built in-process, so nothing here
needs a device, a fixture, or a block library.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("msgpack")

from helixgen.cli import cli  # noqa: E402
from helixgen.device import content, transcode  # noqa: E402
from helixgen.hsp import read_hsp  # noqa: E402

AMP = "HD2_AmpBritPlexiNrm"


@pytest.fixture()
def sbe(tmp_path: Path) -> Path:
    blob = content.encode_content_data(transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}))
    path = tmp_path / "Some Tone.sbe"
    path.write_bytes(blob)
    return path


def _run(*args):
    return CliRunner().invoke(cli, ["device", "to-hsp", *args])


def test_converts_a_local_sbe(sbe, tmp_path):
    out = tmp_path / "out.hsp"
    res = _run(str(sbe), "-o", str(out))
    assert res.exit_code == 0, res.output
    assert "byte-exact round trip" in res.output
    body = read_hsp(out)
    assert body["meta"]["name"] == "Some Tone"   # defaults to the file stem
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["model"] == AMP


def test_name_and_author_options(sbe, tmp_path):
    out = tmp_path / "out.hsp"
    assert _run(str(sbe), "-o", str(out), "--name", "Mine",
                "--author", "me").exit_code == 0
    body = read_hsp(out)
    assert body["meta"]["name"] == "Mine" and body["meta"]["author"] == "me"


# --- SOURCE classification ----------------------------------------------------

def test_a_directory_is_a_clean_error_not_a_traceback(tmp_path):
    res = _run(str(tmp_path), "-o", str(tmp_path / "out.hsp"))
    assert res.exit_code != 0
    assert "is a directory" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_a_missing_path_is_a_clean_error(tmp_path):
    res = _run(str(tmp_path / "nope.sbe"), "-o", str(tmp_path / "out.hsp"))
    assert res.exit_code != 0 and "no such file" in res.output


@pytest.mark.parametrize("source", ["²", "٤٢", "1.0", "0x2a"])
def test_non_ascii_digits_are_not_device_cids(source, tmp_path):
    """`str.isdigit()` is True for superscripts and Arabic-Indic digits; the
    first makes `int()` raise, the second silently parses as a DIFFERENT
    number. Neither may reach the network."""
    res = _run(source, "-o", str(tmp_path / "out.hsp"))
    assert res.exit_code != 0
    assert "no such file" in res.output
    assert not isinstance(res.exception, ValueError)


def test_an_all_digit_filename_is_refused_rather_than_guessed(tmp_path, sbe):
    """A file named `42` and device cid 42 are both plausible readings of the
    same word. Guessing either way silently does the wrong thing — one goes to
    the network ignoring the file, the other never reaches the device."""
    with CliRunner().isolated_filesystem(temp_dir=tmp_path) as fs:
        Path(fs, "42").write_bytes(sbe.read_bytes())
        res = CliRunner().invoke(cli, ["device", "to-hsp", "42",
                                       "-o", str(Path(fs, "out.hsp"))])
    assert res.exit_code != 0
    assert "both a device CID and a local file" in res.output
    assert "./42" in res.output


def test_an_explicit_relative_path_disambiguates(tmp_path, sbe):
    with CliRunner().isolated_filesystem(temp_dir=tmp_path) as fs:
        Path(fs, "42").write_bytes(sbe.read_bytes())
        res = CliRunner().invoke(cli, ["device", "to-hsp", "./42",
                                       "-o", str(Path(fs, "out.hsp"))])
    assert res.exit_code == 0, res.output


# --- unreadable input ---------------------------------------------------------

@pytest.mark.parametrize("blob", [b"", b"not msgpack at all", b"\xc1\xc1\xc1"])
def test_unreadable_content_is_a_clean_error(blob, tmp_path):
    src = tmp_path / "junk.sbe"
    src.write_bytes(blob)
    res = _run(str(src), "-o", str(tmp_path / "out.hsp"))
    assert res.exit_code != 0
    assert "could not transcode" in res.output
    assert not (tmp_path / "out.hsp").exists()


# --- verify -------------------------------------------------------------------

def test_verify_can_be_switched_off(sbe, tmp_path):
    res = _run(str(sbe), "-o", str(tmp_path / "out.hsp"), "--no-verify")
    assert res.exit_code == 0 and "verify:" not in res.output


def test_verify_distinguishes_a_fixed_point_from_a_real_divergence(tmp_path):
    """The verify line must not assert a cause it has not checked. A non-exact
    round trip that RE-CONVERTS to the same .hsp is canonicalization; anything
    else is a divergence the user must not be told to ignore."""
    doc = transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})
    # Reorder a ctrl-free map's keys: content-identical, byte-different — the
    # same shape the device's own re-save produces.
    doc["pm__"] = list(reversed(doc["pm__"]))
    src = tmp_path / "resaved.sbe"
    src.write_bytes(content.encode_content_data(doc))
    res = _run(str(src), "-o", str(tmp_path / "out.hsp"))
    assert res.exit_code == 0, res.output
    assert "FIXED POINT" in res.output
    assert "NOT a fixed point" not in res.output


def test_help_documents_the_lock_behaviour_and_the_drops():
    """Per-verb help is the agent-facing contract (tests/test_cli_parity.py)."""
    res = CliRunner().invoke(cli, ["device", "to-hsp", "--help"])
    assert res.exit_code == 0
    help_text = " ".join(res.output.split())
    for phrase in ("offline when SOURCE is a .sbe path",
                   "HELIXGEN_LOCK_TOKEN",
                   "Command Center commands and MIDI CC controller bindings",
                   "reported on stderr",
                   "./42"):
        assert phrase in help_text, phrase
