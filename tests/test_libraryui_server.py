"""Task 2: localhost server + page (offline-first browse)."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from helixgen.libraryui.server import make_server
from helixgen.libraryui.session import LibrarySession
from helixgen.tone_meta import ToneMeta, Variant


def _session(metas, library_root):
    return LibrarySession(client_factory=None, tone_loader=lambda: metas,
                          guitar_loader=lambda: [],
                          ir_local_loader=lambda: [], library_root=library_root)


def _serve(session):
    srv = make_server(session, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _get(port, path, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req) as r:  # noqa: S310 (localhost)
        return r.status, r.read()


def test_bad_host_rejected(tmp_path):
    srv, port = _serve(_session([], tmp_path))
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/state", host="evil.example.com")
        assert ei.value.code == 403
    finally:
        srv.shutdown()


def test_state_served_offline(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    m = ToneMeta(artist=None, song=None, descriptor="Crunch", tags=[],
                 description_md=None,
                 variants={"generic": Variant(hsp="tones/a.hsp",
                                              preset_name="Crunch")},
                 created="2026-01-01", updated="2026-01-01")
    srv, port = _serve(_session([m], tmp_path))
    try:
        status, body = _get(port, "/state")
        data = json.loads(body)
        assert status == 200
        assert data["connected"] is False
        assert data["tones"][0]["name"] == "Crunch"
    finally:
        srv.shutdown()


def test_name_with_special_chars_round_trips_as_data(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    nasty = 'a"b/<c>'
    m = ToneMeta(artist=None, song=None, descriptor=nasty, tags=[],
                 description_md=None,
                 variants={"generic": Variant(hsp="tones/a.hsp",
                                              preset_name=nasty)},
                 created="2026-01-01", updated="2026-01-01")
    srv, port = _serve(_session([m], tmp_path))
    try:
        _, body = _get(port, "/state")
        assert json.loads(body)["tones"][0]["name"] == nasty
    finally:
        srv.shutdown()


def test_page_renders_names_as_textcontent():
    # the page must inject tone/variant names via textContent, never by
    # building row HTML from the name string (XSS guard, Review-Focus).
    page = (Path(__file__).resolve().parents[1]
            / "src/helixgen/libraryui/page.html").read_text()
    assert "textContent" in page
    assert "innerHTML" not in page  # build DOM nodes, never interpolate names
