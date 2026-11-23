"""The `device live` page's behaviour, exercised in Node against a fake DOM
(hgc-58a). The page is one self-contained HTML file with no build step, so
these scenarios load its real <script> and drive it with synthetic events and
server messages. Skipped where node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PAGE = HERE.parent / "src" / "helixgen" / "liveedit" / "page.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_scenarios_pass_in_node():
    r = subprocess.run(["node", str(HERE / "js" / "liveedit_page_scenarios.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all page scenarios pass" in r.stdout
    assert "FAIL" not in r.stdout


def test_page_never_builds_markup_from_server_strings():
    # every string from the device (preset, model, label, error text) is
    # untrusted — a preset name is user-editable on the hardware.
    js = PAGE.read_text().split("<script>")[1].split("</script>")[0]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, f"page script uses {sink}"


def test_page_sends_the_revision_and_its_own_id_with_every_write():
    js = PAGE.read_text()
    assert "body.client = ME; body.rev = S.rev;" in js


def test_page_has_library_drawer_and_endpoints():
    html = PAGE.read_text()
    for marker in ('id="libtoggle"', 'id="drawer"', "/library/state", '"/library/" + action', '"/preset"'):
        assert marker in html, marker


def test_page_has_snapshot_panel_and_endpoints():
    html = PAGE.read_text()
    for marker in ('id="snappanel"', 'id="snapbody"', '"/snapshot/rename"', '"/snapshot"',
                   '"/snapshot/globals"', "Snapshot settings", "global.snapshot.edits"):
        assert marker in html, marker


def test_page_registers_the_device_pane_and_endpoints():
    # 81v.5: the Device pane mounts via addPane and drives /device/*.
    html = PAGE.read_text()
    for marker in ('id="devicepane"', 'addPane({ id: "device"',
                   "/device/state", "/device/values", "/device/set"):
        assert marker in html, marker


def test_page_registers_the_controllers_pane_and_endpoint():
    # 81v.3: the Controllers pane mounts via addPane and reads /controllers/state.
    html = PAGE.read_text()
    for marker in ('id="ctlpane"', 'id="ctlbody"', 'addPane({ id: "controllers"',
                   "/controllers/state"):
        assert marker in html, marker
