"""Open the live chain editor in a standalone, chromeless app window instead
of the user's default browser (hgc-yoq).

When an installed Chromium-family browser is found, we launch it in app mode
(``--app=<url>``) against a dedicated ``--user-data-dir`` under the helixgen
home, so no 127.0.0.1 URL lands in the user's real browser profile or
history, and the window has no tabs or address bar. Pure subprocess — no new
dependency. Callers fall back to ``webbrowser.open`` when :func:`launch`
returns ``None``.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading

from helixgen import home

logger = logging.getLogger("helixgen.liveedit")

# macOS app-bundle executables, in preference order.
_MAC_BINARIES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
# PATH names for Linux / other platforms.
_PATH_NAMES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge", "microsoft-edge-stable",
]


def find_chromium(*, is_macos=None, exists=os.path.exists, which=shutil.which):
    """Path to an installed Chromium-family browser, or ``None``.

    ``exists``/``which`` are injectable so discovery is testable without
    touching the real filesystem or PATH.
    """
    if is_macos is None:
        is_macos = sys.platform == "darwin"
    if is_macos:
        for path in _MAC_BINARIES:
            if exists(path):
                return path
    for name in _PATH_NAMES:
        found = which(name)
        if found:
            return found
    return None


def user_data_dir() -> str:
    """Dedicated, isolated browser profile dir under the helixgen home."""
    return str(home.helixgen_home() / "live-window")


def app_command(binary: str, url: str, data_dir: str) -> list[str]:
    """argv for a chromeless, profile-isolated app window."""
    return [binary, f"--app={url}", f"--user-data-dir={data_dir}"]


def launch(url: str, *, on_exit=None, binary=None):
    """Open ``url`` in a Chromium app window. Returns the ``Popen`` child, or
    ``None`` if no Chromium-family browser is installed (caller then falls
    back to the default browser). When ``on_exit`` is given, a daemon thread
    calls it once the window closes, so the caller can stop the server."""
    binary = binary or find_chromium()
    if not binary:
        return None
    data_dir = user_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    proc = subprocess.Popen(app_command(binary, url, data_dir),  # noqa: S603
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if on_exit is not None:
        def _watch():
            try:
                proc.wait()
            except Exception:  # noqa: BLE001 — a reaped child must still stop us
                logger.debug("live editor: app-window wait failed", exc_info=True)
            on_exit()
        threading.Thread(target=_watch, daemon=True).start()
    return proc
