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
                   '"/snapshot/globals"', "Snapshot settings", "global.snapshot.edits",
                   '"/snapshot/copy"', '"/snapshot/swap"', "Paste here", "Swap here",
                   '"/snapshot/color"', "sp-swatch", "SNAP_COLORS"):
        assert marker in html, marker


def test_snapshot_colour_palette_stays_aligned_with_the_device_palette():
    # the page hand-copies the colr palette; the swatch's array index IS the
    # colr it posts, so a drift from maintenance.PRESET_COLORS would store one
    # colour while showing another. Pin the names + length to the source.
    import re
    from helixgen.device.maintenance import COLOR_LABELS, PRESET_COLORS
    html = PAGE.read_text()
    names_src = re.search(r"const SNAP_COLOR_NAMES = \[([^\]]*)\]", html)
    hexes_src = re.search(r"const SNAP_COLORS = \[([^\]]*)\]", html)
    assert names_src and hexes_src
    names = [s.strip().strip('"') for s in names_src.group(1).split(",") if s.strip()]
    hexes = [s for s in hexes_src.group(1).split(",") if s.strip()]
    expected = [COLOR_LABELS.get(t, t.title()) for t in PRESET_COLORS]
    assert names == expected, (names, expected)
    assert len(hexes) == len(PRESET_COLORS)


def test_page_registers_the_device_pane_and_endpoints():
    # 81v.5: the Device pane mounts via addPane and drives /device/*.
    html = PAGE.read_text()
    for marker in ('id="devicepane"', 'addPane({ id: "device"',
                   "/device/state", "/device/values", "/device/set"):
        assert marker in html, marker


def test_page_registers_the_controllers_pane_and_endpoint():
    # 81v.3: the Controllers pane mounts via addPane, reads /controllers/state,
    # and assigns / clears MIDI-CC controllers via /controllers/*.
    html = PAGE.read_text()
    for marker in ('id="ctlpane"', 'id="ctlbody"', 'id="ctlassign"',
                   'addPane({ id: "controllers"', "/controllers/state",
                   "/controllers/assign", "/controllers/clear"):
        assert marker in html, marker


def test_page_registers_the_globaleq_pane_and_endpoints():
    # 81v.6: the Global EQ pane mounts via addPane, reads the connect-time
    # snapshot (GET /globaleq/state) and writes one control at a time
    # (POST /globaleq/set).
    html = PAGE.read_text()
    for marker in ('id="eqpane"', 'id="eqtabs"', 'id="eqbody"',
                   'addPane({ id: "globaleq"', "/globaleq/state", "/globaleq/set"):
        assert marker in html, marker


def test_page_registers_the_command_center_pane_and_endpoint():
    # 81v.4: the Command Center pane mounts via addPane and reads /commands/state.
    html = PAGE.read_text()
    for marker in ('id="cmdpane"', 'id="cmdbody"',
                   'addPane({ id: "commands"', "/commands/state"):
        assert marker in html, marker


def test_page_registers_the_tuner_pane_and_endpoint():
    # 81v.10: the Tuner pane mounts via addPane and polls /tuner/reading.
    html = PAGE.read_text()
    for marker in ('id="tunerpane"', 'id="tuner-note"', 'id="tuner-pointer"',
                   'addPane({ id: "tuner"', "/tuner/reading"):
        assert marker in html, marker


def test_page_registers_the_device_info_pane_and_endpoint():
    # 81v.11: the Device Info pane mounts via addPane and reads /device/info.
    html = PAGE.read_text()
    for marker in ('id="deviceinfopane"', 'id="dinfobody"',
                   'addPane({ id: "deviceinfo"', "/device/info"):
        assert marker in html, marker
