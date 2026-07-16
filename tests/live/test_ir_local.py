"""Live local IR verbs: irhash / register-irs / ir-scan / list-irs / ir-cache.

All state is scratch: HELIXGEN_IRS (mapping.json) and HELIXGEN_IRHASH_CACHE
both point into the session scratch dir — the cache is written by every IR
verb regardless of HELIXGEN_IRS (proven in the 2026-07-15 live run), which is
exactly why the suite redirects it. `ir-cache --clear` is therefore safe HERE
(scratch cache only); against a user's real cache it is destructive, which is
why only --stats/--prune touch default paths in normal use.
"""
from __future__ import annotations

import json
import re
import shutil

import pytest

from .conftest import HGTEST, write_test_wav

pytestmark = [pytest.mark.live, pytest.mark.ir]


def test_irhash_stateless(cli, scratch, hgtest_wav):
    # a dedicated wav no other test registers: irhash must NOT write mapping.json
    wav = scratch / "work" / f"{HGTEST}-stateless.wav"
    if not wav.exists():
        write_test_wav(wav, seed=1234)
    code, out, err = cli("irhash", wav, "--json")
    assert code == 0, err or out
    h = json.loads(out)[0]["hash"]
    code, out, err = cli("list-irs")
    assert code == 0, err or out
    assert h not in out


def test_irhash_json_shape(cli, hgtest_wav):
    code, out, err = cli("irhash", hgtest_wav, "--json")
    assert code == 0, err or out
    recs = json.loads(out)
    assert recs and set(recs[0]) >= {"hash", "path", "basename"}
    assert re.fullmatch(r"[0-9a-f]{32}", recs[0]["hash"])


def test_register_irs_wav_persists_mapping(cli, hgtest_wav, hgtest_wav_hash, scratch):
    code, out, err = cli("register-irs", hgtest_wav)
    assert code == 0, err or out
    code, out, err = cli("list-irs", "--json")
    assert code == 0, err or out
    mapping = json.loads(out)
    text = json.dumps(mapping)
    assert hgtest_wav_hash in text and hgtest_wav.name in text
    assert (scratch / "irs" / "mapping.json").exists()


def test_ir_scan_directory_and_remove(cli, scratch, hgtest_wav):
    scan_dir = scratch / "work" / "ir-scan"
    scan_dir.mkdir(exist_ok=True)
    wav = scan_dir / f"{HGTEST}-scan.wav"
    if not wav.exists():
        write_test_wav(wav, seed=99)
    code, out, err = cli("ir-scan", scan_dir)
    assert code == 0, err or out
    code, out, err = cli("list-irs")
    assert code == 0, err or out
    assert wav.name in out
    code, out, err = cli("ir-scan", "--remove", wav.name)
    assert code == 0, err or out
    code, out, err = cli("list-irs")
    assert code == 0, err or out
    assert wav.name not in out


def test_ir_scan_rescan_idempotent(cli, scratch, hgtest_wav):
    scan_dir = scratch / "work" / "ir-rescan"
    scan_dir.mkdir(exist_ok=True)
    shutil.copy(hgtest_wav, scan_dir / f"{HGTEST}-rescan.wav")
    for args in (("ir-scan", scan_dir), ("ir-scan", scan_dir),
                 ("ir-scan", "--rescan", scan_dir)):
        code, out, err = cli(*args)
        assert code == 0, err or out


def test_ir_cache_stats_and_scratch_clear(cli, scratch, hgtest_wav):
    # a hash compute above has warmed the scratch cache
    code, out, err = cli("ir-cache", "--stats")
    assert code == 0, err or out
    assert str(scratch / "irhash-cache.json") in out
    code, out, err = cli("ir-cache", "--prune")
    assert code == 0, err or out
    # --clear is safe here ONLY because the env points the cache at scratch
    code, out, err = cli("ir-cache", "--clear")
    assert code == 0, err or out
    code, out, err = cli("ir-cache", "--stats")
    assert code == 0, err or out
    assert "entries: 0" in out
