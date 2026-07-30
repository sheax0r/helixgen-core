"""Device discovery + the #74 IP-resolution chain (0.24.0).

Offline coverage: mDNS wire parsing (synthetic packets — no network), the
resolution chain (--ip > $HELIXGEN_HELIX_IP > persisted record > fail fast),
persisted-record round-trips through the observations store, multi-device
determinism, the `device discover` CLI verb (mocked discovery + client),
and the no-hardcoded-IP regression sweep of src/.
"""
from __future__ import annotations

import ipaddress
import json
import struct
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import home
from helixgen.cli import cli
from helixgen.device import discovery, observations

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# mDNS wire parsing (pure, offline)
# ---------------------------------------------------------------------------

def _mdns_response(instance: str = "p35x1", ip: str = "192.168.7.42",
                   port: int = 2001, *, compressed: bool = True) -> bytes:
    """A synthetic Stadium mDNS response: PTR + SRV + A (the exact record
    set observed live from the hardware), with DNS name compression."""
    svc = discovery.MDNS_SERVICE
    inst_fqdn = f"{instance}.{svc}"
    host = f"{instance}.local."

    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 3, 0, 0)
    out = bytearray(header)

    # PTR: service -> instance
    svc_off = len(out)
    out += discovery.encode_dns_name(svc)
    ptr_rdata = (bytes([len(instance)]) + instance.encode()
                 + struct.pack(">H", 0xC000 | svc_off)) if compressed \
        else discovery.encode_dns_name(inst_fqdn)
    out += struct.pack(">HHIH", 12, 1, 120, len(ptr_rdata)) + ptr_rdata

    # SRV: instance -> host:port
    inst_off = len(out)
    out += (bytes([len(instance)]) + instance.encode()
            + struct.pack(">H", 0xC000 | svc_off)) if compressed \
        else discovery.encode_dns_name(inst_fqdn)
    srv_rdata = struct.pack(">HHH", 0, 0, port) + discovery.encode_dns_name(host)
    out += struct.pack(">HHIH", 33, 1, 120, len(srv_rdata)) + srv_rdata

    # A: host -> ip
    out += discovery.encode_dns_name(host)
    out += struct.pack(">HHIH", 1, 1, 120, 4)
    out += bytes(int(x) for x in ip.split("."))
    return bytes(out)


class TestMdnsParsing:
    def test_full_stadium_response_yields_candidate(self):
        recs = discovery.parse_mdns_response(_mdns_response())
        cands = discovery.candidates_from_records(recs)
        assert len(cands) == 1
        c = cands[0]
        assert c.ip == "192.168.7.42"
        assert c.instance == "p35x1"
        assert c.hostname == "p35x1.local."
        assert c.via == "mdns"

    def test_uncompressed_names_also_parse(self):
        recs = discovery.parse_mdns_response(_mdns_response(compressed=False))
        assert discovery.candidates_from_records(recs)[0].ip == "192.168.7.42"

    def test_standard_srv_port_yields_no_rpc_override(self):
        recs = discovery.parse_mdns_response(_mdns_response(port=2001))
        assert discovery.candidates_from_records(recs)[0].rpc_port is None

    def test_nonstandard_srv_port_derives_rpc_port(self):
        recs = discovery.parse_mdns_response(_mdns_response(port=3001))
        assert discovery.candidates_from_records(recs)[0].rpc_port == 3002

    def test_unrelated_service_yields_nothing(self):
        recs = discovery.parse_mdns_response(_mdns_response())
        assert discovery.candidates_from_records(
            recs, service="_other._tcp.local.") == []

    def test_srv_without_a_record_yields_nothing(self):
        recs = [r for r in discovery.parse_mdns_response(_mdns_response())
                if r[1] != "A"]
        assert discovery.candidates_from_records(recs) == []

    def test_garbage_packets_return_empty(self):
        assert discovery.parse_mdns_response(b"") == []
        assert discovery.parse_mdns_response(b"\x00" * 5) == []
        assert discovery.parse_mdns_response(b"\xff" * 64) == []

    def test_compression_pointer_loop_is_safe(self):
        # name at offset 12 points at itself
        pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c" + b"\x00" * 12
        assert discovery.parse_mdns_response(pkt) in ([], [(".", "PTR", ".")],)

    def test_query_has_qu_bit_and_ptr_type(self):
        q = discovery.build_mdns_query()
        assert q[:2] == b"\x00\x00"
        assert q.endswith(struct.pack(">HH", 12, 0x8001))
        assert b"\x0e_stadiumserver" in q


# ---------------------------------------------------------------------------
# the resolution chain
# ---------------------------------------------------------------------------

class TestResolveIp:
    def test_explicit_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.2")
        observations.record_device_ip("S1", "10.0.0.3")
        assert discovery.resolve_ip("10.0.0.1") == "10.0.0.1"

    def test_env_wins_over_record(self, monkeypatch):
        monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.2")
        observations.record_device_ip("S1", "10.0.0.3")
        assert discovery.resolve_ip() == "10.0.0.2"

    def test_persisted_record_used_when_no_flag_or_env(self):
        observations.record_device_ip("S1", "10.0.0.3")
        assert discovery.resolve_ip() == "10.0.0.3"

    def test_fail_fast_when_nothing_configured(self):
        with pytest.raises(discovery.IPResolutionError) as ei:
            discovery.resolve_ip()
        msg = str(ei.value)
        assert "helixgen device discover" in msg
        assert "--ip" in msg and "HELIXGEN_HELIX_IP" in msg

    def test_most_recently_discovered_wins(self, capsys):
        observations.record_device_ip("OLD", "10.0.0.1", updated_at=100.0)
        observations.record_device_ip("NEW", "10.0.0.2", updated_at=200.0)
        assert discovery.resolve_ip() == "10.0.0.2"
        assert "2 discovered devices" in capsys.readouterr().err

    def test_timestamp_tie_breaks_to_highest_serial(self):
        observations.record_device_ip("AAA", "10.0.0.1", updated_at=100.0)
        observations.record_device_ip("ZZZ", "10.0.0.2", updated_at=100.0)
        assert discovery.resolve_ip(warn=False) == "10.0.0.2"

    def test_single_device_no_warning(self, capsys):
        observations.record_device_ip("S1", "10.0.0.3")
        discovery.resolve_ip()
        assert capsys.readouterr().err == ""


class TestResolvePort:
    def test_explicit_port_wins(self):
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        assert discovery.resolve_port("10.0.0.3", explicit=2002) == 2002

    def test_persisted_port_reused_for_matching_ip(self):
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        assert discovery.resolve_port("10.0.0.3") == 9002

    def test_default_when_record_has_no_port(self):
        observations.record_device_ip("S1", "10.0.0.3")
        assert discovery.resolve_port("10.0.0.3") == discovery.RPC_PORT

    def test_default_when_ip_not_recorded(self):
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        assert discovery.resolve_port("10.0.0.99") == discovery.RPC_PORT

    def test_resolves_ip_itself_when_none_given(self):
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        assert discovery.resolve_port() == 9002

    def test_default_when_nothing_configured(self):
        assert discovery.resolve_port() == discovery.RPC_PORT


# ---------------------------------------------------------------------------
# persisted-record round-trips
# ---------------------------------------------------------------------------

class TestRecordPersistence:
    def test_record_and_reload(self):
        path = observations.record_device_ip(
            "47292244582131381", "192.168.7.42",
            model="stadium", firmware="1.3.2")
        assert path == home.devices_dir() / "47292244582131381.json"
        data = json.loads(path.read_text())
        assert data["ip"] == "192.168.7.42"
        assert data["model"] == "stadium"
        assert data["firmware"] == "1.3.2"
        assert isinstance(data["ip_updated_at"], float)

    def test_sync_style_load_save_preserves_ip(self):
        """A sync rebuild (load -> mutate placements -> save) must not drop
        the discovered address record."""
        observations.record_device_ip("S1", "10.0.0.9", model="stadium")
        obs = observations.load_observations("S1")
        obs.record_pool("Tone", cid=1234, posi=5, synced_hash="sha256:x")
        observations.save_observations(obs)
        again = observations.load_observations("S1")
        assert again.ip == "10.0.0.9"
        assert again.model == "stadium"
        assert again.pool["Tone"]["cid"] == 1234

    def test_record_preserves_existing_placements(self):
        obs = observations.DeviceObservations(serial="S1")
        obs.record_pool("Tone", cid=7, posi=1)
        observations.save_observations(obs)
        observations.record_device_ip("S1", "10.0.0.9")
        again = observations.load_observations("S1")
        assert again.tones["Tone"]["cid"] == 7
        assert again.ip == "10.0.0.9"

    def test_rename_tone_rewrite_preserves_ip(self):
        observations.record_device_ip("S1", "10.0.0.9", firmware="1.3.2")
        obs = observations.load_observations("S1")
        obs.record_pool("Old", cid=7, posi=1)
        observations.save_observations(obs)
        observations.rename_tone("Old", "New")
        again = observations.load_observations("S1")
        assert "New" in again.tones and "Old" not in again.tones
        assert again.ip == "10.0.0.9"
        assert again.firmware == "1.3.2"

    def test_devices_with_ips_ordering(self):
        observations.record_device_ip("A", "10.0.0.1", updated_at=50.0)
        observations.record_device_ip("B", "10.0.0.2", updated_at=150.0)
        obs = observations.DeviceObservations(serial="noip")  # no ip field
        observations.save_observations(obs)
        rows = observations.devices_with_ips()
        assert [r["serial"] for r in rows] == ["B", "A"]

    def test_nonstandard_rpc_port_round_trips(self):
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        obs = observations.load_observations("S1")
        assert obs.port == 9002
        rows = observations.devices_with_ips()
        assert rows[0]["port"] == 9002

    def test_standard_port_not_written(self):
        """A record with no discovered port stays portless (the default 2002
        is implied, not stored) and reports port None."""
        observations.record_device_ip("S1", "10.0.0.3")
        data = json.loads((home.devices_dir() / "S1.json").read_text())
        assert "port" not in data
        assert observations.devices_with_ips()[0]["port"] is None

    def test_port_survives_sync_rebuild(self):
        observations.record_device_ip("S1", "10.0.0.9", port=9002)
        obs = observations.load_observations("S1")
        obs.record_pool("Tone", cid=1, posi=2)
        observations.save_observations(obs)
        assert observations.load_observations("S1").port == 9002

    def test_rediscover_on_standard_port_clears_stale_port(self):
        """Discovery is authoritative for the port: re-recording a device that
        reverted to the standard 2002 (port=None) heals a stale nonstandard
        record rather than keeping the old port (#77)."""
        observations.record_device_ip("S1", "10.0.0.3", port=9002)
        assert observations.load_observations("S1").port == 9002
        observations.record_device_ip("S1", "10.0.0.3")  # re-discover, standard
        assert observations.load_observations("S1").port is None
        assert observations.devices_with_ips()[0]["port"] is None
        assert discovery.resolve_port("10.0.0.3") == discovery.RPC_PORT


# ---------------------------------------------------------------------------
# CLI: fail-fast + `device discover`
# ---------------------------------------------------------------------------

class _FakeClient:
    """Stands in for HelixClient in discover's confirmation handshake."""

    infos = {}

    def __init__(self, ip, port=2002, **kw):
        self.ip = ip

    def __enter__(self):
        if self.ip not in self.infos:
            raise OSError("connect refused")
        return self

    def __exit__(self, *a):
        return False

    def product_info(self):
        return self.infos[self.ip]


class TestCli:
    def test_device_verb_fails_fast_without_any_ip(self):
        start = time.monotonic()
        r = CliRunner().invoke(cli, ["device", "info"])
        elapsed = time.monotonic() - start
        assert r.exit_code != 0
        assert "helixgen device discover" in r.output
        assert elapsed < 5, "fail-fast must not stall on a network connect"

    def test_device_verb_uses_persisted_record(self, monkeypatch):
        """With only a persisted record, --ip resolves to it (verified via
        the fake client the verb then connects with)."""
        observations.record_device_ip("S1", "10.9.9.9")
        seen = {}

        def fake_client():
            class C(_FakeClient):
                infos = {"10.9.9.9": {"serial": "S1", "model": "stadium",
                                      "firmware": "1.3.2"}}

                def product_info(self):
                    seen["ip"] = self.ip
                    return super().product_info()
            return C, discovery.IPResolutionError
        monkeypatch.setattr("helixgen.cli_device._client", fake_client)
        r = CliRunner().invoke(cli, ["device", "info"])
        assert r.exit_code == 0, r.output
        assert seen["ip"] == "10.9.9.9"

    def test_empty_ip_rejected(self):
        """--ip "" is a mistake (usually an unset shell var expanded to
        nothing), not a request to fall back to the record — reject it
        loudly with a nonzero exit instead of silently resolving on (#77)."""
        r = CliRunner().invoke(cli, ["device", "info", "--ip", ""])
        assert r.exit_code != 0
        assert "--ip" in r.output
        assert "empty" in r.output.lower()

    def test_whitespace_ip_rejected(self):
        r = CliRunner().invoke(cli, ["device", "info", "--ip", "   "])
        assert r.exit_code != 0
        assert "--ip" in r.output

    def test_discover_persists_and_reports(self, monkeypatch):
        cand = discovery.Candidate(ip="192.168.7.42", hostname="p35x1.local.",
                                   instance="p35x1", via="mdns")
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [cand])
        _FakeClient.infos = {"192.168.7.42": {
            "serial": "SER42", "model": "stadium",
            "helixgen_model": "stadium-xl", "firmware": "1.3.2"}}
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--json"])
        assert r.exit_code == 0, r.output
        rows = json.loads(r.stdout)
        assert rows[0]["ip"] == "192.168.7.42"
        assert rows[0]["serial"] == "SER42"
        assert rows[0]["default"] is True
        # persisted — and the resolver now finds it
        assert discovery.resolve_ip() == "192.168.7.42"

    def test_discover_report_names_effective_home(self, monkeypatch):
        # $HELIXGEN_HOME is redirected to tmp_path by the autouse fixture, so
        # the persisted-record report must name that effective devices/ dir,
        # not a hardcoded ``~/.helixgen/devices/`` (#77 / #73).
        cand = discovery.Candidate(ip="192.168.7.42", hostname="p35x1.local.",
                                   instance="p35x1", via="mdns")
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [cand])
        _FakeClient.infos = {"192.168.7.42": {
            "serial": "SER42", "model": "stadium", "firmware": "1.3.2"}}
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover"])
        assert r.exit_code == 0, r.output
        assert str(home.devices_dir()) in r.output
        assert "~/.helixgen/devices/" not in r.output

    def test_discover_persists_nonstandard_rpc_port(self, monkeypatch):
        cand = discovery.Candidate(ip="192.168.7.42", hostname="p35x1.local.",
                                   instance="p35x1", via="mdns", rpc_port=9002)
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [cand])
        _FakeClient.infos = {"192.168.7.42": {
            "serial": "SER42", "model": "stadium", "firmware": "1.3.2"}}
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--json"])
        assert r.exit_code == 0, r.output
        rows = json.loads(r.stdout)
        assert rows[0]["port"] == 9002
        # a later verb resolves the persisted nonstandard port
        assert discovery.resolve_port() == 9002

    def test_verb_connects_on_persisted_nonstandard_port(self, monkeypatch):
        observations.record_device_ip("S1", "10.9.9.9", port=9002)
        seen = {}

        def fake_client():
            class C(_FakeClient):
                infos = {"10.9.9.9": {"serial": "S1", "model": "stadium",
                                      "firmware": "1.3.2"}}

                def __init__(self, ip, port=2002, **kw):
                    seen["ip"] = ip
                    seen["port"] = port
                    super().__init__(ip, port, **kw)
            return C, discovery.IPResolutionError
        monkeypatch.setattr("helixgen.cli_device._client", fake_client)
        r = CliRunner().invoke(cli, ["device", "info"])
        assert r.exit_code == 0, r.output
        assert seen == {"ip": "10.9.9.9", "port": 9002}

    def test_explicit_ip_picks_that_records_port_not_the_default(self, monkeypatch):
        """Pins the --ip/--port callback ordering: an explicit --ip to a
        NON-default device on a nonstandard port must resolve THAT record's
        port, not the newest (default) record's. Would fail if _device_option
        stopped processing --ip before --port (ctx.params.get("ip") is None)."""
        # older target on a nonstandard port; newer default on the standard one
        observations.record_device_ip("TARGET", "10.0.0.3", port=9002,
                                      updated_at=100.0)
        observations.record_device_ip("DEFAULT", "10.0.0.4", updated_at=200.0)
        seen = {}

        def fake_client():
            class C(_FakeClient):
                infos = {"10.0.0.3": {"serial": "TARGET", "model": "stadium",
                                      "firmware": "1.3.2"}}

                def __init__(self, ip, port=2002, **kw):
                    seen["ip"] = ip
                    seen["port"] = port
                    super().__init__(ip, port, **kw)
            return C, discovery.IPResolutionError
        monkeypatch.setattr("helixgen.cli_device._client", fake_client)
        r = CliRunner().invoke(cli, ["device", "info", "--ip", "10.0.0.3"])
        assert r.exit_code == 0, r.output
        assert seen == {"ip": "10.0.0.3", "port": 9002}

    def test_discover_multi_device_default_is_deterministic(self, monkeypatch):
        cands = [discovery.Candidate(ip="10.0.0.5", via="mdns"),
                 discovery.Candidate(ip="10.0.0.6", via="mdns")]
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: cands)
        _FakeClient.infos = {
            "10.0.0.5": {"serial": "AAA", "model": "stadium", "firmware": "1"},
            "10.0.0.6": {"serial": "ZZZ", "model": "stadium", "firmware": "1"},
        }
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--json"])
        assert r.exit_code == 0, r.output
        rows = {d["serial"]: d for d in json.loads(r.stdout)}
        assert rows["ZZZ"]["default"] is True   # highest serial on a tie
        assert rows["AAA"]["default"] is False
        assert "multiple devices" in r.output
        assert discovery.resolve_ip(warn=False) == "10.0.0.6"

    def test_discover_falls_back_to_probe(self, monkeypatch):
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [])
        monkeypatch.setattr(discovery, "probe_subnet", lambda **kw: ["10.0.0.7"])
        _FakeClient.infos = {"10.0.0.7": {"serial": "P1", "model": "stadium",
                                          "firmware": "1.3.2"}}
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover"])
        assert r.exit_code == 0, r.output
        assert "via probe" in r.stdout
        assert "falling back" in r.output

    def test_discover_no_probe_flag_skips_probe(self, monkeypatch):
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [])

        def boom(**kw):
            raise AssertionError("probe must not run with --no-probe")
        monkeypatch.setattr(discovery, "probe_subnet", boom)
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--no-probe"])
        assert r.exit_code != 0
        assert "no Helix Stadium found" in r.output

    def test_discover_unconfirmed_candidate_is_skipped(self, monkeypatch):
        cand = discovery.Candidate(ip="10.0.0.66", via="mdns")
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [cand])
        _FakeClient.infos = {}  # handshake fails
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--no-probe"])
        assert r.exit_code != 0
        assert "did not pass" in r.output
        with pytest.raises(discovery.IPResolutionError):
            discovery.resolve_ip()  # nothing was persisted


class TestFailFastCoverage:
    """Review findings 1/2/4/8 (PR #12): every unconfigured path must
    surface the instructive fail-fast error — never a raw traceback — and
    read-only lock introspection must stay usable (exit 0)."""

    def test_ir_prune_dry_run_fails_fast_not_traceback(self):
        r = CliRunner().invoke(cli, ["device", "ir-prune"])
        assert r.exit_code == 1, r.output
        assert "helixgen device discover" in r.output
        assert "Traceback" not in r.output
        assert not isinstance(r.exception, discovery.IPResolutionError)

    def test_sync_no_lock_fails_fast_not_traceback(self):
        r = CliRunner().invoke(cli, ["device", "sync", "--all", "--no-lock"])
        assert r.exit_code == 1, r.output
        assert "helixgen device discover" in r.output
        assert "Traceback" not in r.output
        assert not isinstance(r.exception, discovery.IPResolutionError)

    def test_push_ir_no_lock_fails_fast(self, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"RIFF")
        r = CliRunner().invoke(
            cli, ["device", "push-ir", str(wav), "--no-lock"])
        assert r.exit_code == 1, r.output
        assert "helixgen device discover" in r.output

    def test_lock_status_unconfigured_exits_zero(self):
        r = CliRunner().invoke(cli, ["device", "lock", "--status"])
        assert r.exit_code == 0, r.output
        assert "no device IP configured" in r.output
        rj = CliRunner().invoke(
            cli, ["device", "lock", "--status", "--json"])
        assert rj.exit_code == 0, rj.output
        assert json.loads(rj.stdout) == []

    def test_unconfigured_exit_code_is_uniform(self):
        """One condition, one exit code (1) — parse-resolved verbs and
        client-resolving verbs alike."""
        for args in (["device", "info"], ["device", "unlock"],
                     ["device", "lock", "--scope", "all", "--label", "x"]):
            r = CliRunner().invoke(cli, args)
            assert r.exit_code == 1, (args, r.exit_code, r.output)
            assert "helixgen device discover" in r.output, args

    def test_forget_removes_record_by_serial(self):
        observations.record_device_ip("S1", "10.0.0.3")
        observations.record_device_ip("S2", "10.0.0.4")
        r = CliRunner().invoke(cli, ["device", "discover", "--forget", "S1"])
        assert r.exit_code == 0, r.output
        assert "forgot S1" in r.output
        # S1 gone, S2 kept
        assert [row["serial"] for row in observations.devices_with_ips()] == ["S2"]

    def test_forget_removes_record_by_ip(self):
        observations.record_device_ip("S1", "10.0.0.3")
        r = CliRunner().invoke(
            cli, ["device", "discover", "--forget", "10.0.0.3"])
        assert r.exit_code == 0, r.output
        assert observations.devices_with_ips() == []

    def test_forget_json_lists_removed_paths(self):
        observations.record_device_ip("S1", "10.0.0.3")
        r = CliRunner().invoke(
            cli, ["device", "discover", "--forget", "S1", "--json"])
        assert r.exit_code == 0, r.output
        removed = json.loads(r.stdout)
        assert len(removed) == 1 and removed[0].endswith("S1.json")

    def test_forget_unknown_target_errors_no_traceback(self):
        observations.record_device_ip("S1", "10.0.0.3")
        r = CliRunner().invoke(
            cli, ["device", "discover", "--forget", "NOPE"])
        assert r.exit_code != 0
        assert "no persisted record matches" in r.output
        assert "Traceback" not in r.output
        # nothing removed
        assert [row["serial"] for row in observations.devices_with_ips()] == ["S1"]

    def test_forget_absent_records_dir_errors_no_traceback(self, monkeypatch):
        # devices/ never created (no discovery has run yet)
        assert not home.devices_dir().exists()
        r = CliRunner().invoke(
            cli, ["device", "discover", "--forget", "S1"])
        assert r.exit_code != 0
        assert "no persisted device records yet" in r.output
        assert "Traceback" not in r.output

    def test_forget_does_not_hit_the_network(self, monkeypatch):
        def boom(**kw):
            raise AssertionError("--forget must not run discovery")
        monkeypatch.setattr(discovery, "mdns_discover", boom)
        observations.record_device_ip("S1", "10.0.0.3")
        r = CliRunner().invoke(cli, ["device", "discover", "--forget", "S1"])
        assert r.exit_code == 0, r.output

    def test_forget_device_returns_removed_paths(self):
        observations.record_device_ip("S1", "10.0.0.3")
        removed = observations.forget_device("S1")
        assert len(removed) == 1
        assert removed[0].name == "S1.json"
        assert not removed[0].exists()

    def test_forget_device_absent_dir_raises(self):
        with pytest.raises(FileNotFoundError):
            observations.forget_device("S1")

    def test_forget_device_permission_error_propagates(self, monkeypatch):
        """A non-benign OS error on unlink (e.g. PermissionError) must
        propagate, not be swallowed and reported as "no match" — only the
        benign FileNotFoundError race (file vanished between listing and
        unlink) is caught. (PR #30 review, Finding 1.)"""
        observations.record_device_ip("S1", "10.0.0.3")

        from pathlib import Path as _Path
        real_unlink = _Path.unlink

        def boom(self, *a, **kw):
            if self.name == "S1.json":
                raise PermissionError("read-only filesystem")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(_Path, "unlink", boom)
        with pytest.raises(PermissionError):
            observations.forget_device("S1")

    def test_forget_device_swallows_vanished_file_race(self, monkeypatch):
        """The benign race — the file disappeared between listing and unlink —
        stays a no-op: FileNotFoundError is caught, that record just isn't
        reported as removed."""
        observations.record_device_ip("S1", "10.0.0.3")

        from pathlib import Path as _Path
        real_unlink = _Path.unlink

        def vanished(self, *a, **kw):
            if self.name == "S1.json":
                raise FileNotFoundError(str(self))
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(_Path, "unlink", vanished)
        removed = observations.forget_device("S1")
        assert removed == []

    def test_discover_warns_when_env_overrides_record(self, monkeypatch):
        monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.1.1.1")
        cand = discovery.Candidate(ip="10.0.0.5", via="mdns")
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [cand])
        _FakeClient.infos = {"10.0.0.5": {"serial": "S1", "model": "stadium",
                                          "firmware": "1.3.2"}}
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover"])
        assert r.exit_code == 0, r.output
        assert "outranks" in r.output


# ---------------------------------------------------------------------------
# subnet-probe safety (no sockets — pure target-set checks)
# ---------------------------------------------------------------------------

class TestProbeSafety:
    def test_targets_are_local_slash24_only(self, monkeypatch):
        probed = []

        def fake_connect(addr, timeout=None):
            probed.append(addr)
            raise OSError("closed")
        monkeypatch.setattr(discovery.socket, "create_connection", fake_connect)
        assert discovery.probe_subnet(
            subnet_ip="192.168.7.10", prefixlen=24) == []
        ips = {a[0] for a in probed}
        assert len(ips) == 253  # /24 minus network/broadcast-ish minus self
        assert "192.168.7.10" not in ips
        assert all(ip.startswith("192.168.7.") for ip in ips)
        assert all(a[1] == discovery.RPC_PORT for a in probed)

    def test_no_local_ip_means_no_probe(self, monkeypatch):
        monkeypatch.setattr(discovery, "local_ipv4", lambda: None)

        def boom(*a, **kw):
            raise AssertionError("must not open sockets without a local ip")
        monkeypatch.setattr(discovery.socket, "create_connection", boom)
        assert discovery.probe_subnet() == []

    @pytest.mark.parametrize("public_ip", [
        "203.0.113.5",    # TEST-NET-3 (stands in for any public address)
        "100.64.0.7",     # CGNAT/tailnet — not RFC 1918, refused too
        "not-an-ip",      # unparseable — fail closed
    ])
    def test_public_range_is_refused_without_sockets(
            self, monkeypatch, capsys, public_ip):
        # backlog #77: never connect-scan a /24 that is not RFC 1918
        # private — that is a port scan of strangers, not LAN discovery.
        def boom(*a, **kw):
            raise AssertionError("must not open sockets on a public range")
        monkeypatch.setattr(discovery.socket, "create_connection", boom)
        assert discovery.probe_subnet(subnet_ip=public_ip) == []
        err = capsys.readouterr().err
        assert "private" in err and "refusing" in err

    def test_private_range_still_probes(self, monkeypatch):
        probed = []

        def fake_connect(addr, timeout=None):
            probed.append(addr)
            raise OSError("closed")
        monkeypatch.setattr(discovery.socket, "create_connection", fake_connect)
        assert discovery.probe_subnet(subnet_ip="10.1.2.3", prefixlen=24) == []
        assert len({a[0] for a in probed}) == 253


# ---------------------------------------------------------------------------
# netmask-derived probe range (hc-3qw) — pure functions, no sockets
# ---------------------------------------------------------------------------

class TestNetmaskToPrefixlen:
    @pytest.mark.parametrize("mask,expected", [
        ("0xffffff00", 24),          # macOS/BSD hex form, /24
        ("0xfffffc00", 22),          # the observed /22 (192.168.4.0-7.255)
        ("0xffff0000", 16),
        ("255.255.255.0", 24),
        ("255.255.252.0", 22),
        ("255.255.0.0", 16),
        ("0xffffffff", 32),
        ("0x00000000", 0),
    ])
    def test_parses_both_forms(self, mask, expected):
        assert discovery.netmask_to_prefixlen(mask) == expected

    @pytest.mark.parametrize("mask", [
        "0xff00ff00",   # non-contiguous — not a legal netmask
        "255.0.255.0",
        "not-a-mask",
        "",
        "0x1ffffffff",  # out of range
    ])
    def test_rejects_junk(self, mask):
        assert discovery.netmask_to_prefixlen(mask) is None


_IFCONFIG_MACOS = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.4.98 netmask 0xfffffc00 broadcast 192.168.7.255
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.9.9.4 netmask 0xffffff00 broadcast 10.9.9.255
"""

_IP_ADDR_LINUX = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
2: eth0    inet 192.168.4.98/22 brd 192.168.7.255 scope global eth0\\  valid
3: eth1    inet 172.20.0.5/16 brd 172.20.255.255 scope global eth1\\   valid
"""


class TestParsePrefixlen:
    @pytest.mark.parametrize("output,ip,expected", [
        (_IFCONFIG_MACOS, "192.168.4.98", 22),
        (_IFCONFIG_MACOS, "10.9.9.4", 24),
        (_IFCONFIG_MACOS, "127.0.0.1", 8),
        (_IP_ADDR_LINUX, "192.168.4.98", 22),
        (_IP_ADDR_LINUX, "172.20.0.5", 16),
        (_IP_ADDR_LINUX, "127.0.0.1", 8),
    ])
    def test_finds_the_interface_carrying_the_address(
            self, output, ip, expected):
        assert discovery.parse_prefixlen(output, ip) == expected

    @pytest.mark.parametrize("output", [_IFCONFIG_MACOS, _IP_ADDR_LINUX, ""])
    def test_unknown_address_is_none(self, output):
        assert discovery.parse_prefixlen(output, "203.0.113.9") is None

    def test_prefix_match_does_not_leak(self):
        # "10.9.9.4" must not match the line for "10.9.9.40"
        out = "en0:\n\tinet 10.9.9.40 netmask 0xffff0000\n"
        assert discovery.parse_prefixlen(out, "10.9.9.4") is None


class TestProbeNetwork:
    @pytest.mark.parametrize("ip,prefixlen,expected", [
        ("192.168.4.98", 24, "192.168.4.0/24"),
        ("192.168.4.98", 22, "192.168.4.0/22"),   # the observed real case
        ("192.168.4.98", None, "192.168.4.0/24"),  # unknown mask -> /24
        ("10.1.2.3", 8, "10.1.0.0/22"),           # capped, anchored on us
        ("172.20.30.40", 16, "172.20.28.0/22"),   # capped, anchored on us
    ])
    def test_range_derivation(self, ip, prefixlen, expected):
        assert str(discovery.probe_network(ip, prefixlen)) == expected

    def test_cap_bounds_host_count(self):
        for prefixlen in (0, 8, 16, 20, 22):
            net = discovery.probe_network("10.1.2.3", prefixlen)
            assert net.num_addresses <= discovery.MAX_PROBE_HOSTS
            assert ipaddress.ip_address("10.1.2.3") in net

    def test_custom_cap(self):
        net = discovery.probe_network("10.1.2.3", 8, max_hosts=256)
        assert str(net) == "10.1.2.0/24"

    @pytest.mark.parametrize("bad", ["not-an-ip", "fe80::1", "", "1.2.3"])
    def test_bad_address_raises(self, bad):
        with pytest.raises(ValueError):
            discovery.probe_network(bad, 24)

    def test_bad_address_refuses_to_probe_without_sockets(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("must not open sockets on an unparseable ip")
        monkeypatch.setattr(discovery.socket, "create_connection", boom)
        assert discovery.probe_subnet(subnet_ip="fe80::1", prefixlen=24) == []

    def test_slash24_host_count_is_unchanged(self):
        assert len(list(discovery.probe_network("10.1.2.3", 24).hosts())) == 254


class TestProbeUsesDerivedRange:
    def _probed(self, monkeypatch, **kw):
        seen = []

        def fake_connect(addr, timeout=None):
            seen.append(addr)
            raise OSError("closed")
        monkeypatch.setattr(discovery.socket, "create_connection", fake_connect)
        assert discovery.probe_subnet(subnet_ip="192.168.4.98", **kw) == []
        return {a[0] for a in seen}

    def test_slash22_probes_the_whole_slash22(self, monkeypatch):
        ips = self._probed(monkeypatch, prefixlen=22)
        assert len(ips) == 1021          # 1022 usable minus our own address
        assert "192.168.4.98" not in ips
        assert {"192.168.4.1", "192.168.5.1", "192.168.6.1",
                "192.168.7.254"} <= ips

    def test_slash16_is_capped_not_65k(self, monkeypatch):
        ips = self._probed(monkeypatch, prefixlen=16)
        assert len(ips) == 1021
        assert all(ipaddress.ip_address(i)
                   in ipaddress.ip_network("192.168.4.0/22") for i in ips)

    def test_netmask_lookup_drives_the_default(self, monkeypatch):
        monkeypatch.setattr(discovery, "local_prefixlen", lambda ip: 22)
        assert len(self._probed(monkeypatch)) == 1021

    def test_unknown_netmask_falls_back_to_slash24(self, monkeypatch):
        monkeypatch.setattr(discovery, "local_prefixlen", lambda ip: None)
        assert len(self._probed(monkeypatch)) == 253

    def test_warning_names_the_range_actually_refused(
            self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise AssertionError("must not open sockets on a public range")
        monkeypatch.setattr(discovery.socket, "create_connection", boom)
        assert discovery.probe_subnet(
            subnet_ip="203.0.113.5", prefixlen=24) == []
        assert "203.0.113.0/24" in capsys.readouterr().err

    def test_local_probe_network_uses_the_interface_netmask(self, monkeypatch):
        monkeypatch.setattr(discovery, "local_ipv4", lambda: "192.168.4.98")
        monkeypatch.setattr(discovery, "local_prefixlen", lambda ip: 22)
        assert str(discovery.local_probe_network()) == "192.168.4.0/22"

    def test_local_probe_network_is_none_off_private_ranges(self, monkeypatch):
        monkeypatch.setattr(discovery, "local_ipv4", lambda: "203.0.113.5")
        assert discovery.local_probe_network() is None
        monkeypatch.setattr(discovery, "local_ipv4", lambda: None)
        assert discovery.local_probe_network() is None


class TestDiscoverNamesTheProbedRange:
    """The failure message must not assert more than it checked (hc-3qw)."""

    def test_messages_name_the_range(self, monkeypatch):
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [])
        monkeypatch.setattr(discovery, "local_probe_network",
                            lambda **kw: ipaddress.ip_network("192.168.4.0/22"))
        monkeypatch.setattr(discovery, "probe_subnet", lambda **kw: [])
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover"])
        assert r.exit_code != 0
        assert r.output.count("192.168.4.0/22") == 2  # fallback + failure
        assert "no Helix Stadium found" in r.output

    def test_no_probe_message_does_not_claim_a_probed_range(self, monkeypatch):
        monkeypatch.setattr(discovery, "mdns_discover", lambda **kw: [])
        monkeypatch.setattr("helixgen.cli_device._client",
                            lambda: (_FakeClient, discovery.IPResolutionError))
        r = CliRunner().invoke(cli, ["device", "discover", "--no-probe"])
        assert r.exit_code != 0
        assert "subnet probe" not in r.output


# ---------------------------------------------------------------------------
# regression: the fossilized default is gone
# ---------------------------------------------------------------------------

def test_no_hardcoded_device_ip_in_src():
    """The maintainer's old DHCP lease must not survive anywhere in src/
    (it was a guaranteed-wrong default for every other user, failing as a
    long connect stall)."""
    offenders = []
    for p in (REPO_ROOT / "src").rglob("*.py"):
        if "192.168.4.84" in p.read_text():
            offenders.append(str(p))
    assert offenders == [], f"hardcoded device IP resurfaced in: {offenders}"
