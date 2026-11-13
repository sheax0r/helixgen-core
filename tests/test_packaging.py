"""Packaging guard: every data file the package loads at runtime must be
declared as package data, or it is silently missing from the wheel.

0.60.0 shipped without `liveedit/page.html` — the whole suite was green
because tests import from the source tree, where the file is always there;
`helixgen device live` installed from PyPI could not serve its page.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _package_data() -> dict[str, list[str]]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return cfg["tool"]["setuptools"]["package-data"]


def test_every_non_python_file_in_the_package_is_declared_package_data():
    # a pattern may reach into a subdirectory ("helixgen" = ["assets/*.wav"]),
    # so resolve each one the way setuptools does: a glob under its package
    shipped = set()
    for package, patterns in _package_data().items():
        folder = SRC.joinpath(*package.split("."))
        for pat in patterns:
            shipped.update(folder.glob(pat))
    missing = [
        str(path.relative_to(SRC))
        for path in sorted((SRC / "helixgen").rglob("*"))
        if path.is_file() and path.suffix not in (".py", ".pyc")
        and "__pycache__" not in path.parts and path.name != ".DS_Store"
        and path not in shipped
    ]
    assert not missing, (
        "data files in src/helixgen that [tool.setuptools.package-data] does "
        f"not ship in the wheel: {missing}")


def test_declared_package_data_exists():
    for package, patterns in _package_data().items():
        folder = SRC.joinpath(*package.split("."))
        for pat in patterns:
            assert list(folder.glob(pat)), f"{package}: pattern {pat!r} matches nothing"


def test_the_live_editor_page_is_shipped():
    assert "page.html" in _package_data().get("helixgen.liveedit", [])
