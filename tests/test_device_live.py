"""Live hardware test for the device network client.

Skipped unless a real Helix Stadium is present AND explicitly enabled:
    HELIXGEN_LIVE_DEVICE=1 HELIXGEN_HELIX_IP=192.168.4.84 \
        PYTHONPATH=$PWD/src python -m pytest tests/test_device_live.py -q

It exercises the full CRUD cycle on the USER setlist's slot 2D (posi 7), which
is empty on a stock device, and cleans up after itself so the device is left in
its original state.  It refuses to run if that slot is already occupied.
"""
import os

import pytest

LIVE = os.environ.get("HELIXGEN_LIVE_DEVICE")
IP = os.environ.get("HELIXGEN_HELIX_IP", "192.168.4.84")
POS = 7  # USER slot 2D — empty by default

pytestmark = pytest.mark.skipif(
    not LIVE, reason="live device test; set HELIXGEN_LIVE_DEVICE=1 to enable"
)


@pytest.fixture()
def client():
    from helixgen.device import HelixClient  # lazy: needs the `device` extra
    h = HelixClient(IP).connect()
    try:
        yield h
    finally:
        h.close()


def test_full_crud_cycle_on_empty_slot(client):
    from helixgen.device import USER

    presets = client.list_presets(USER)
    assert presets, "device returned no USER presets — is it connected?"

    # pick a real source preset and confirm the target slot is empty
    src = presets[0]
    src_cid = src["cid_"]
    assert client.find_by_pos(USER, POS) is None, (
        f"USER slot posi={POS} is not empty; refusing to clobber it"
    )

    new_cid = None
    try:
        # CREATE (copy src into the empty slot)
        new_cid = client.create_from(src_cid, USER, POS)
        assert new_cid is not None
        created = client.find_by_pos(USER, POS)
        assert created is not None and created["cid_"] == new_cid

        # READ
        ref = client.get_ref(new_cid)
        assert ref and ref.get("cid_") == new_cid

        # UPDATE (rename)
        assert client.rename(new_cid, "helixgen live test")
        renamed = client.find_by_pos(USER, POS)
        assert renamed and renamed.get("name") == "helixgen live test"
    finally:
        # DELETE (cleanup) — always restore the empty slot
        if new_cid is not None:
            client.delete(USER, [new_cid])

    assert client.find_by_pos(USER, POS) is None, "cleanup failed: slot still occupied"


def test_edit_buffer_roundtrips(client):
    from helixgen.device import content as C

    blob = client.get_edit_buffer()
    assert blob[:8] == C.MAGIC
    decoded = C.decode_content(blob)
    assert isinstance(decoded, dict)
    assert C.decode_content(C.encode_content(decoded)) == decoded


def test_authoring_bridge_installs_a_chain(client):
    """Author a device-native chain onto the current edit buffer as a template
    and install it, then verify + clean up. Requires the target slot empty."""
    from helixgen.device import USER, bridge, defs

    if client.find_by_pos(USER, POS) is not None:
        pytest.skip(f"USER slot {POS} not empty")
    template = client.get_edit_buffer()  # whatever's currently loaded
    # a minimal chain in device ids: distortion + reverb (categories most
    # templates contain). Skip if the template lacks those slots.
    chain = [(310, {"Gain": 0.7}), (63, {"Mix": 0.3})]
    cid = None
    try:
        try:
            cid = bridge.install_chain(client, USER, POS, "live bridge test",
                                       template, chain)
        except ValueError:
            pytest.skip("current template lacks the needed block categories")
        assert cid is not None
        client.load_preset(cid)
        doc = client.read_edit_buffer()
        enabled = {b["mdls"][0]["id__"]
                   for _p, b in bridge._user_blocks(doc) if b.get("enbl") == 1}
        assert 310 in enabled and 63 in enabled
    finally:
        if cid is not None:
            client.delete(USER, [cid])
