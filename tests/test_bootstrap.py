import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helixgen.bootstrap import bootstrap, clone_or_pull_phelix
from helixgen.library import Library


@patch("helixgen.bootstrap.subprocess.run")
def test_clone_when_cache_missing(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    assert not cache.exists()

    clone_or_pull_phelix(cache, ref="main")

    args = mock_run.call_args_list
    assert len(args) == 1
    cmd = args[0][0][0]
    assert cmd[0:2] == ["git", "clone"]
    assert "https://github.com/sensorium/phelix" in cmd
    assert str(cache) in cmd


@patch("helixgen.bootstrap.subprocess.run")
def test_pull_when_cache_exists(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    cache.mkdir()
    (cache / ".git").mkdir()

    clone_or_pull_phelix(cache, ref="main")

    cmds = [args[0][0] for args in mock_run.call_args_list]
    cmd_strs = [" ".join(c) for c in cmds]
    assert any("fetch" in c for c in cmd_strs)
    assert any("checkout" in c for c in cmd_strs)
    assert not any("clone" in c for c in cmd_strs)


@patch("helixgen.bootstrap.subprocess.run")
def test_uses_specified_ref(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    cache.mkdir()
    (cache / ".git").mkdir()

    clone_or_pull_phelix(cache, ref="v1.2.3")

    cmds = [args[0][0] for args in mock_run.call_args_list]
    checkout = next(c for c in cmds if "checkout" in c)
    assert "v1.2.3" in checkout


@patch("helixgen.bootstrap.clone_or_pull_phelix")
def test_bootstrap_clones_then_ingests_blocks(mock_clone, tmp_library, tmp_path, sample_amp_block):
    fake_phelix = tmp_path / "phelix"
    blocks_dir = fake_phelix / "blocks"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "amp.json").write_text(json.dumps(sample_amp_block))
    mock_clone.return_value = fake_phelix

    lib_obj = Library(tmp_library)
    summary = bootstrap(lib_obj, ref="main", cache_dir=fake_phelix)

    mock_clone.assert_called_once_with(fake_phelix, ref="main")
    assert summary.new == 1
    assert len(lib_obj.list_blocks()) == 1
