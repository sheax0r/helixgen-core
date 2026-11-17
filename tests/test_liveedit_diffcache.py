from helixgen.liveedit.diffcache import DiffCache


def _e(h="h1", v="same"):
    return {"device_fp": "fp", "hsp_hash": h, "verdict": v, "ts": 1.0}


def test_cache_roundtrip_and_hsp_invalidation(tmp_path):
    c = DiffCache(tmp_path / "c.json")
    c.put("cid1", _e())
    assert c.verdict("cid1", "h1") == "same"
    assert c.verdict("cid1", "other") is None
    assert c.verdict("nope", "h1") is None
    c.invalidate("cid1")
    assert c.get("cid1") is None


def test_cache_survives_reload(tmp_path):
    p = tmp_path / "d" / "c.json"
    c = DiffCache(p)
    c.put("cid1", _e())
    c.save()
    assert DiffCache(p).get("cid1") == _e()


def test_cache_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert DiffCache(p).get("x") is None
