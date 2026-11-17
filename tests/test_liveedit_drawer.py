from __future__ import annotations

from helixgen.liveedit.drawer import LibraryDrawer
from helixgen.tone_meta import ToneMeta, Variant


def _meta(tmp_path):
    hsp = tmp_path / "tones" / "a.hsp"
    hsp.parent.mkdir(parents=True, exist_ok=True)
    hsp.write_text("{}")
    return ToneMeta(
        artist=None, song=None, descriptor="Crunch", tags=[],
        description_md=None,
        variants={"ibanez-prestige": Variant(hsp="tones/a.hsp",
                                              preset_name="Crunch - Prestige")},
        created="2026-01-01", updated="2026-01-01")


def _kw(tmp_path):
    return dict(tone_loader=lambda: [_meta(tmp_path)],
                guitar_loader=lambda: [], ir_local_loader=lambda: [])


def test_drawer_public_state_offline(tmp_path):
    d = LibraryDrawer(tmp_path, None, client_factory=None, cache=None,
                      **_kw(tmp_path))
    st = d.public_state()
    assert st["connected"] is False
    variants = [v for t in st["tones"] for v in t["variants"]]
    assert variants and variants[0]["presence"] == "computer"


def test_drawer_action_dispatch(tmp_path):
    _meta(tmp_path)  # writes tones/a.hsp
    closed, seen = [], {}

    class Client:
        def close(self):
            closed.append(1)

    client = Client()

    def copy_fn(h, hsp_path, *, setlist, pos):
        seen.update(h=h, hsp=str(hsp_path), setlist=setlist)
        return {"ok": True}

    d = LibraryDrawer(tmp_path, None, client_factory=lambda: client,
                      cache=None, copy_fn=copy_fn, **_kw(tmp_path))
    res = d.action("copy", {"hsp": "tones/a.hsp", "to": "Rock"})
    assert res == {"ok": True}
    assert seen["h"] is client and seen["setlist"] == "Rock"
    assert not closed
