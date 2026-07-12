"""Phase 2: local reorder + sync-plan computation (pure, no device)."""
from helixgen.device.ledger import SlotLedger


def _seed(tmp_path, rows):
    """rows: list of (setlist, posi, name, cid) in placement order."""
    led = SlotLedger.load(tmp_path / "l.json")
    for i, (sl, posi, name, cid) in enumerate(rows):
        led.record(setlist=sl, posi=posi, name=name, cid=cid,
                   source_kind="hsp", source_path=f"/x/{name}.hsp",
                   now=f"2026-07-12T00:{i:02d}:00+00:00")
    return led


def _dev(setlist, posi, cid, name=""):
    return {"setlist": setlist, "posi": posi, "cid_": cid, "name": name}


# -- reorder (local only) -----------------------------------------------------

def test_reorder_moves_entry_within_setlist(tmp_path):
    led = _seed(tmp_path, [
        ("user", 0, "A", 1), ("user", 1, "B", 2), ("user", 2, "C", 3)])
    assert led.reorder(name="C", to_index=0) is True
    assert [e["name"] for e in led.entries_in_order()] == ["C", "A", "B"]


def test_reorder_preserves_order_value_set(tmp_path):
    led = _seed(tmp_path, [
        ("user", 0, "A", 1), ("user", 1, "B", 2), ("user", 2, "C", 3)])
    before = sorted(e["order"] for e in led.entries)
    led.reorder(name="B", to_index=2)
    after = sorted(e["order"] for e in led.entries)
    assert before == after == [0, 1, 2]  # same order-values, redistributed


def test_reorder_clamps_index(tmp_path):
    led = _seed(tmp_path, [("user", 0, "A", 1), ("user", 1, "B", 2)])
    assert led.reorder(name="A", to_index=99) is True
    assert [e["name"] for e in led.entries_in_order()] == ["B", "A"]


def test_reorder_only_touches_its_setlist(tmp_path):
    led = _seed(tmp_path, [
        ("user", 0, "A", 1), ("factory", 0, "X", 9),
        ("user", 1, "B", 2), ("user", 2, "C", 3)])
    led.reorder(name="C", to_index=0)
    user_seq = [e["name"] for e in led.entries_in_order() if e["setlist"] == "user"]
    assert user_seq == ["C", "A", "B"]
    # factory entry still present and unmoved relative to user entries it isn't in
    assert led.find(name="X")["setlist"] == "factory"


def test_reorder_unknown_name_false(tmp_path):
    led = _seed(tmp_path, [("user", 0, "A", 1)])
    assert led.reorder(name="Nope", to_index=0) is False


# -- sync_plan (pure) ---------------------------------------------------------

def test_sync_plan_empty_when_already_in_order(tmp_path):
    led = _seed(tmp_path, [
        ("user", 0, "A", 1), ("user", 1, "B", 2), ("user", 2, "C", 3)])
    device = [_dev("user", 0, 1), _dev("user", 1, 2), _dev("user", 2, 3)]
    assert led.sync_plan(device) == []


def test_sync_plan_rearranges_among_occupied_slots(tmp_path):
    led = _seed(tmp_path, [
        ("user", 5, "A", 1), ("user", 2, "B", 2), ("user", 7, "C", 3)])
    # device currently: A@5, B@2, C@7 ; ledger order A,B,C ; occupied sorted [2,5,7]
    device = [_dev("user", 5, 1), _dev("user", 2, 2), _dev("user", 7, 3)]
    moves = led.sync_plan(device)
    by_cid = {m["cid"]: (m["from"], m["to"]) for m in moves}
    assert by_cid[1] == (5, 2)  # A -> first occupied slot
    assert by_cid[2] == (2, 5)  # B -> second
    assert 3 not in by_cid       # C already at last occupied slot (7)


def test_sync_plan_reflects_reorder(tmp_path):
    led = _seed(tmp_path, [
        ("user", 0, "A", 1), ("user", 1, "B", 2), ("user", 2, "C", 3)])
    led.reorder(name="C", to_index=0)  # desired order C, A, B
    device = [_dev("user", 0, 1), _dev("user", 1, 2), _dev("user", 2, 3)]
    moves = led.sync_plan(device)
    by_cid = {m["cid"]: (m["from"], m["to"]) for m in moves}
    assert by_cid[3] == (2, 0)  # C -> slot 0
    assert by_cid[1] == (0, 1)  # A -> slot 1
    assert by_cid[2] == (1, 2)  # B -> slot 2


def test_sync_plan_ignores_untracked_and_missing(tmp_path):
    led = _seed(tmp_path, [
        ("user", 3, "A", 1), ("user", 9, "B", 2), ("user", 1, "C", 3)])
    # C (cid 3) is NOT on the device -> skipped; an untracked preset sits at 6
    device = [_dev("user", 3, 1), _dev("user", 9, 2), _dev("user", 6, 77, "Untracked")]
    moves = led.sync_plan(device)
    cids = {m["cid"] for m in moves}
    assert 3 not in cids and 77 not in cids  # missing + untracked excluded
    # A,B present at [3,9]; ledger order A,B -> A@3(ok), B@9(ok) -> no moves
    assert moves == []
