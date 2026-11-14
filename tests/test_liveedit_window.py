"""`helixgen device live` standalone app window (hgc-yoq). The spawn itself
is not unit-testable, but Chromium discovery and the chromeless/isolated
argv ARE — pinned here."""
from helixgen.liveedit import window

_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"


def test_find_chromium_prefers_a_mac_app_bundle():
    got = window.find_chromium(is_macos=True,
                               exists=lambda p: p == _CHROME,
                               which=lambda n: "/should/not/win")
    assert got == _CHROME


def test_find_chromium_walks_mac_bundles_in_order():
    got = window.find_chromium(is_macos=True,
                               exists=lambda p: p == _BRAVE,
                               which=lambda n: None)
    assert got == _BRAVE


def test_find_chromium_uses_path_when_no_bundle():
    got = window.find_chromium(
        is_macos=False, exists=lambda p: False,
        which=lambda n: f"/usr/bin/{n}" if n == "chromium" else None)
    assert got == "/usr/bin/chromium"


def test_find_chromium_returns_none_when_absent():
    assert window.find_chromium(is_macos=True, exists=lambda p: False,
                                which=lambda n: None) is None


def test_app_command_is_chromeless_and_isolated():
    url = "http://127.0.0.1:8765/"
    ddir = "/home/u/.helixgen/live-window/8765"
    cmd = window.app_command(_CHROME, url, ddir)
    assert cmd[0] == _CHROME
    assert f"--app={url}" in cmd                 # chromeless: no tabs, no address bar
    assert f"--user-data-dir={ddir}" in cmd      # isolated: not the real profile


def test_user_data_dir_is_per_port_under_helixgen_home(monkeypatch, tmp_path):
    # keyed on the editor's HTTP port so two concurrent editors never share one
    # Chromium profile (hgc-yoq)
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    assert window.user_data_dir("http://127.0.0.1:8765/") == \
        str(tmp_path / "live-window" / "8765")
    assert window.user_data_dir("http://127.0.0.1:9100/") == \
        str(tmp_path / "live-window" / "9100")


def test_launch_returns_none_without_a_chromium(monkeypatch):
    monkeypatch.setattr(window, "find_chromium", lambda: None)
    assert window.launch("http://127.0.0.1:8765/") is None
