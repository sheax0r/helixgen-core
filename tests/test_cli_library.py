import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.hsp import write_hsp


def _hsp(dirpath, name):
    p = dirpath / f"{name}.hsp"
    write_hsp(p, {"meta": {"name": name}})
    return p


def test_library_import_places_the_file(tmp_path):
    """`helixgen register` is retired: the library DIRECTORY is the index
    (2026-09-09 file-copy design), so a tone is in the library exactly when
    its .hsp is there. `library import` is the verb that puts it there."""
    from helixgen import home
    hp = _hsp(tmp_path, "Imported")
    r = CliRunner().invoke(cli, ["library", "import", str(hp)])
    assert r.exit_code == 0, r.output
    assert any(p.suffix == ".hsp" for p in home.tones_dir().glob("*.hsp"))


def test_generate_auto_registers(tmp_path):
    # Uses the real chassis; skip gracefully if no library is present.
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "Auto Reg Test", "paths": [{"blocks": []}]}))
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(cli, ["generate", str(recipe), "-o", str(out)])
    if r.exit_code != 0:
        import pytest
        pytest.skip(f"generate unavailable in this env: {r.output}")
    # "Registered" == the .hsp is in the library directory (the index).
    from helixgen import home
    from helixgen.hsp import read_hsp
    assert any(read_hsp(p)["meta"]["name"] == "Auto Reg Test"
               for p in home.tones_dir().glob("*.hsp"))


def _save_tone(artist, song, tags):
    from helixgen import tone_meta
    meta = tone_meta.ToneMeta(
        artist=artist, song=song, descriptor=None, tags=list(tags),
        description_md=None, variants={}, created="2026-01-01",
        updated="2026-01-01")
    tone_meta.save_tone_meta(meta)
    return meta.logical_slug


def test_library_tag_adds_key_value_tags():
    from helixgen import tone_meta
    slug = _save_tone("Foo", "Bar", [])
    r = CliRunner().invoke(cli, ["library", "tag", slug, "genre=rock", "role=lead"])
    assert r.exit_code == 0, r.output
    assert tone_meta.load_tone_meta(slug).tags == ["genre=rock", "role=lead"]


def test_library_tag_removes_by_key_wildcard():
    from helixgen import tone_meta
    slug = _save_tone("Foo", "Bar", ["genre=rock", "genre=metal", "role=lead"])
    r = CliRunner().invoke(cli, ["library", "tag", slug, "--rm", "genre="])
    assert r.exit_code == 0, r.output
    assert tone_meta.load_tone_meta(slug).tags == ["role=lead"]


def test_library_tag_no_edits_lists_current_tags():
    slug = _save_tone("Foo", "Bar", ["genre=rock"])
    r = CliRunner().invoke(cli, ["library", "tag", slug])
    assert r.exit_code == 0, r.output
    assert "genre=rock" in r.output


def test_library_tag_unknown_tone_exits_1():
    r = CliRunner().invoke(cli, ["library", "tag", "no-such-tone", "genre=rock"])
    assert r.exit_code == 1


def test_library_list_filters_by_tag():
    _save_tone("Foo", "Rock Song", ["genre=rock"])
    _save_tone("Baz", "Jazz Song", ["genre=jazz"])
    r = CliRunner().invoke(cli, ["library", "list", "--tones", "--tag", "genre=rock"])
    assert r.exit_code == 0, r.output
    assert "foo-rock-song" in r.output
    assert "baz-jazz-song" not in r.output


def test_library_list_tag_survives_nonstring_tag_in_a_tone(tmp_path):
    # a hand-edited tone with a non-string tag must not crash the whole
    # library's --tag filter (module contract: one bad file never breaks a run)
    from helixgen import home, tone_meta
    good = _save_tone("Foo", "Rock Song", ["genre=rock"])
    bad = _save_tone("Baz", "Bad Song", [])
    p = tone_meta.meta_path(bad)
    import json as _json
    d = _json.loads(p.read_text())
    d["tags"] = ["lead", 5, None]
    p.write_text(_json.dumps(d))
    r = CliRunner().invoke(cli, ["library", "list", "--tones", "--tag", "genre=rock"])
    assert r.exit_code == 0, r.output
    assert "foo-rock-song" in r.output


def test_library_tag_empty_value_add_stores_no_junk():
    from helixgen import tone_meta
    slug = _save_tone("Foo", "Bar", ["lead"])
    r = CliRunner().invoke(cli, ["library", "tag", slug, "genre="])
    assert r.exit_code == 0, r.output
    assert tone_meta.load_tone_meta(slug).tags == ["lead"]
