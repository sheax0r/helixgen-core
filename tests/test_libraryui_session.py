"""Task 1: LibrarySession read model + computer<->device presence diff."""
from __future__ import annotations

import datetime as _dt

import pytest

from helixgen.tone_meta import ToneMeta, Variant


def _meta(slug_parts, variants, tags=None):
    artist, song, descriptor = slug_parts
    return ToneMeta(
        artist=artist, song=song, descriptor=descriptor,
        tags=tags or [], description_md=None, variants=variants,
        created="2026-01-01", updated="2026-01-01",
    )


def _variant(hsp, preset_name, venue=None, venue_detail=None):
    return Variant(hsp=hsp, preset_name=preset_name, venue=venue,
                   venue_detail=venue_detail)


class _FakeClient:
    """A stand-in HelixClient context manager with canned listings."""

    def __init__(self, pool, setlists=None, containers=None, irs=None,
                 raise_on_enter=False):
        self._pool = pool
        self._setlists = setlists or []
        self._containers = containers or {}
        self._irs = irs or []
        self._raise = raise_on_enter

    def __enter__(self):
        if self._raise:
            raise OSError("no Helix Stadium answered")
        return self

    def __exit__(self, *a):
        return False

    def list_presets(self, container=-2, **kw):
        return list(self._pool)

    def list_setlists(self, **kw):
        return list(self._setlists)

    def list_container(self, cid, **kw):
        return list(self._containers.get(cid, []))

    def list_irs(self, **kw):
        return list(self._irs)


def _session(metas, *, client=None, library_root, irs_local=None):
    from helixgen.libraryui.session import LibrarySession
    return LibrarySession(
        client_factory=(lambda: client) if client is not None else None,
        tone_loader=lambda: metas,
        guitar_loader=lambda: [],
        ir_local_loader=lambda: (irs_local or []),
        library_root=library_root,
    )


def test_offline_lists_computer_tones(tmp_path):
    hsp = tmp_path / "tones" / "a.hsp"
    hsp.parent.mkdir(parents=True)
    hsp.write_text("{}")
    m = _meta((None, None, "Crunch"),
              {"ibanez-prestige": _variant("tones/a.hsp", "Crunch - Prestige")})
    st = _session([m], library_root=tmp_path)
    state = st.public_state()
    assert state["connected"] is False
    assert len(state["tones"]) == 1
    row = state["tones"][0]
    assert row["presence"] == "computer"
    assert row["variants"][0]["available"] is True
    assert row["variants"][0]["presence"] == "computer"


def test_presence_both_when_name_on_device(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    m = _meta((None, None, "Crunch"),
              {"ibanez-prestige": _variant("tones/a.hsp", "Crunch - Prestige")})
    client = _FakeClient(pool=[
        {"cid_": 2037, "name": "Crunch - Prestige", "cctp": 1001, "posi": 0},
        {"cid_": 2040, "name": "Only On Device", "cctp": 1001, "posi": 1},
    ])
    st = _session([m], client=client, library_root=tmp_path)
    state = st.public_state()
    assert state["connected"] is True
    var = state["tones"][0]["variants"][0]
    assert var["presence"] == "both"
    assert var["device_cid"] == 2037
    device_only = [t for t in state["tones"] if t["presence"] == "device"]
    assert any(t["name"] == "Only On Device" for t in device_only)


def test_variant_keyed_guitar_at_venue(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    m = _meta((None, None, "Crunch"), {
        "ibanez-prestige@amp-in": _variant(
            "tones/a.hsp", "Crunch - Prestige - amp in",
            venue="amp-in", venue_detail={"treatment": "room-ir"}),
    })
    st = _session([m], library_root=tmp_path)
    var = st.public_state()["tones"][0]["variants"][0]
    assert var["guitar"] == "ibanez-prestige"
    assert var["venue"] == "amp-in"
    assert var["treatment"] == "room-ir"


def test_missing_hsp_flagged_unavailable(tmp_path):
    m = _meta((None, None, "Gone"),
              {"generic": _variant("tones/missing.hsp", "Gone")})
    st = _session([m], library_root=tmp_path)
    var = st.public_state()["tones"][0]["variants"][0]
    assert var["available"] is False


def test_facets_collected(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    for n in ("a", "b"):
        (tmp_path / "tones" / f"{n}.hsp").write_text("{}")
    m1 = _meta((None, None, "One"), {
        "ibanez-prestige": _variant("tones/a.hsp", "One - Prestige",
                                    venue="headphones",
                                    venue_detail={"treatment": "room-ir"})},
        tags=["lead", "genre=rock"])
    m2 = _meta((None, None, "Two"),
               {"esp-ltd-ec-1000": _variant("tones/b.hsp", "Two - EC-1000")})
    st = _session([m1, m2], library_root=tmp_path)
    facets = st.public_state()["facets"]
    assert "ibanez-prestige" in facets["guitars"]
    assert "esp-ltd-ec-1000" in facets["guitars"]
    assert "headphones" in facets["venues"]
    assert "room-ir" in facets["treatments"]
    assert "lead" in facets["tags"]


def test_device_error_leaves_computer_side_live(tmp_path):
    (tmp_path / "tones").mkdir(parents=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    m = _meta((None, None, "Crunch"),
              {"generic": _variant("tones/a.hsp", "Crunch")})
    client = _FakeClient(pool=[], raise_on_enter=True)
    st = _session([m], client=client, library_root=tmp_path)
    state = st.public_state()
    assert state["connected"] is False
    assert len(state["tones"]) == 1  # computer side intact despite device error
