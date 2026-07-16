"""Pin the optional-`device`-extra ImportError *surface* (backlog #54 / S7).

S7 folded the ~65 copy-pasted lazy `from helixgen.device import ...` statements
in ``cli_device`` into two lazy accessors. That refactor must NOT move where the
missing-extra (pyzmq / msgpack) error appears:

  * ``helixgen --help``, ``device --help``, per-verb ``--help``, and every
    non-device command must keep working with pyzmq/msgpack absent;
  * a device verb that actually connects must still fail at *invocation* with
    the friendly ``pip install 'helixgen[device]'`` message.

We simulate the extra being absent with the PR #63 reviewer's technique:
poison ``sys.modules['zmq']`` / ``['msgpack']`` to ``None`` so any *fresh*
``import zmq`` / ``import msgpack`` raises ImportError, then drive the CLI.
"""
import sys

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _configured_device_ip(monkeypatch):
    """#74: device verbs no longer have a built-in default IP. These tests
    exercise verb logic against fakes, so simulate a configured user."""
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


@pytest.fixture
def no_device_extra(monkeypatch):
    """Make ``import zmq`` and ``import msgpack`` raise ImportError process-wide
    for the duration of the test (restored automatically by monkeypatch)."""
    monkeypatch.setitem(sys.modules, "zmq", None)
    monkeypatch.setitem(sys.modules, "msgpack", None)
    # sanity: the poison actually bites
    with pytest.raises(ImportError):
        import zmq  # noqa: F401
    with pytest.raises(ImportError):
        import msgpack  # noqa: F401
    yield


def _cli():
    from helixgen.cli import cli

    return cli


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],            # top-level help
        ["device", "--help"],  # device group help
        ["device", "list", "--help"],       # a device verb's help
        ["device", "setlists", "--help"],   # another device verb's help
        ["generate", "--help"],             # a non-device command's help
        ["view", "--help"],                 # another non-device command's help
    ],
)
def test_help_paths_work_without_device_extra(no_device_extra, args):
    """Help never imports the device extra, so it must exit 0 even absent it."""
    result = CliRunner().invoke(_cli(), args)
    assert result.exit_code == 0, (args, result.output)


def test_non_device_command_runs_without_device_extra(no_device_extra, tmp_path):
    """A real (non-help) non-device command must not be affected by the missing
    extra — `list-blocks` runs the library, never the device layer."""
    result = CliRunner().invoke(_cli(), ["list-blocks"])
    assert result.exit_code == 0, result.output
    # zmq/msgpack were never needed
    assert "helixgen[device]" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["device", "setlists"],
        ["device", "info"],
        ["device", "list"],
    ],
)
def test_device_verb_errors_with_friendly_message(no_device_extra, args):
    """A device verb that actually connects must fail at invocation with the
    friendly install-the-extra message — the surface stays at command time."""
    result = CliRunner().invoke(_cli(), args)
    assert result.exit_code != 0, result.output
    assert "pip install 'helixgen[device]'" in result.output
