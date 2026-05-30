import json
from unittest.mock import patch

from click.testing import CliRunner

from helixgen.chassis import extract_chassis
from helixgen.cli import cli
from helixgen.ingest import IngestSummary, block_from_raw
from helixgen.library import Library


def test_cli_help_lists_subcommands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ["ingest", "generate", "list-blocks", "show-block", "bootstrap", "register-irs", "list-irs"]:
        assert cmd in result.output


def test_cli_ingest_full_preset(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "p.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))

    result = CliRunner().invoke(
        cli,
        ["ingest", str(preset_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 0
    assert "+5 new blocks" in result.output or "new blocks" in result.output
    blocks_dir = tmp_library / "blocks"
    assert blocks_dir.exists()


def test_cli_ingest_uses_env_var(tmp_library, sample_amp_block, tmp_path, monkeypatch):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))

    monkeypatch.setenv("HELIXGEN_LIBRARY", str(tmp_library))
    result = CliRunner().invoke(cli, ["ingest", str(block_path)])
    assert result.exit_code == 0


def test_cli_ingest_missing_path_returns_user_error(tmp_library):
    result = CliRunner().invoke(
        cli,
        ["ingest", "/does/not/exist", "--library", str(tmp_library)],
    )
    # Click's argument exists=True validation returns exit code 2 before our handler runs
    assert result.exit_code != 0
    assert "not exist" in result.output.lower() or "no such" in result.output.lower() or "does not exist" in result.output.lower()


def test_cli_generate_writes_output(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    lib = Library(tmp_library)
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()
    lib.save_chassis(extract_chassis(sample_serial_preset))

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "From CLI",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    content = json.loads(out_path.read_text())
    assert content["data"]["meta"]["name"] == "From CLI"


def test_cli_generate_missing_block_user_error(tmp_library, sample_serial_preset, tmp_path):
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis(sample_serial_preset))
    lib.rebuild_index()

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_generate_invalid_spec_user_error(tmp_library, tmp_path):
    spec_path = tmp_path / "bad.json"
    spec_path.write_text(json.dumps({"paths": []}))
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 1
    assert "name" in result.output.lower()


def test_cli_list_blocks_groups_by_category(
    tmp_library, sample_amp_block, sample_cab_block
):
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["list-blocks", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "amp:" in result.output.lower()
    assert "cab:" in result.output.lower()
    assert "Brit 2204 Custom" in result.output
    assert "4x12 Greenback 25" in result.output


def test_cli_list_blocks_filters_by_category(
    tmp_library, sample_amp_block, sample_cab_block
):
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["list-blocks", "--category", "amp", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "Brit 2204 Custom" in result.output
    assert "4x12 Greenback 25" not in result.output


def test_cli_list_blocks_empty_library(tmp_library):
    result = CliRunner().invoke(
        cli, ["list-blocks", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "no blocks" in result.output.lower() or result.output.strip() == ""


def test_cli_show_block_prints_schema(tmp_library, sample_amp_block):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(
        sample_amp_block, {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    ))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["show-block", "Brit 2204 Custom", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "HD2_AmpBrit2204Custom" in result.output
    assert "Drive" in result.output
    assert "amp" in result.output.lower()


def test_cli_show_block_missing_user_error(tmp_library):
    result = CliRunner().invoke(
        cli, ["show-block", "Nope", "--library", str(tmp_library)]
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


@patch("helixgen.cli.bootstrap")
def test_cli_bootstrap_invokes_bootstrap(mock_bootstrap, tmp_library):
    mock_bootstrap.return_value = IngestSummary(new=12)

    result = CliRunner().invoke(
        cli, ["bootstrap", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    mock_bootstrap.assert_called_once()
    args, kwargs = mock_bootstrap.call_args
    assert kwargs.get("ref") == "main"
    assert "12 new blocks" in result.output or "+12" in result.output


@patch("helixgen.cli.bootstrap")
def test_cli_bootstrap_passes_ref(mock_bootstrap, tmp_library):
    mock_bootstrap.return_value = IngestSummary(new=0)

    CliRunner().invoke(
        cli, ["bootstrap", "--phelix-ref", "v2.0", "--library", str(tmp_library)]
    )
    _, kwargs = mock_bootstrap.call_args
    assert kwargs.get("ref") == "v2.0"
