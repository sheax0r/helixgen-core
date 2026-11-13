import pytest

from helixgen import venue


def test_validate_venue_accepts_vocabulary_and_none():
    assert venue.validate_venue("headphones") == "headphones"
    assert venue.validate_venue(None) is None
    with pytest.raises(ValueError, match="amp-in"):
        venue.validate_venue("stadium")


from pathlib import Path

from helixgen.ir import IrMapping
from tests.test_ir_cli import _libsndfile_available, _write_synth_wav

needs_sf = pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile missing")


def _wav(path: Path, amp: float, n: int = 100) -> Path:
    v = int(amp * (2**23 - 1))
    return _write_synth_wav(path, n, samples_per_channel=[[v] * n])


@pytest.fixture
def irs_fixture(tmp_path):
    root = tmp_path / "irs"
    irs = IrMapping(irs_dir=root)
    layout = {
        "a": ("YA MRSH 412 T75/natural-phase/YA MRSH 412 T75 ROOM 2.wav", 0.5),
        "b": ("YA MRSH 412 T75/natural-phase/YA MRSH 412 T75 ROOM 1.wav", 0.2),
        "c": ("YA MRSH 412 T75/YA MRSH 412 T75 ROOM 3.wav", 0.9),  # louder, not natural-phase
        "d": ("YA MRSH 412 T75/other/YA MRSH 412 T75 ROOM 2.wav", 0.1),  # same basename as a
        "e": ("YA MRSH 412 T75/YA MRSH 412 T75 Mix 12.wav", 0.5),
        "f": ("YA DXVB 112/YA DXVB 112 Mix 01.wav", 0.5),
    }
    for h, (rel, amp) in layout.items():
        irs.register(h, _wav(root / rel, amp))
    return irs


def _close(tmp_path, name):
    return Path(tmp_path / "close" / name)


@needs_sf
def test_find_room_ir_matches_prefix_prefers_natural_phase_then_energy(irs_fixture, tmp_path):
    h, p = venue.find_room_ir(_close(tmp_path, "YA MRSH 412 T75 Mix 12.wav"), irs_fixture)
    assert h == "a" and p.name == "YA MRSH 412 T75 ROOM 2.wav" and "natural-phase" in str(p)


@needs_sf
def test_find_room_ir_none_when_pack_has_no_room(irs_fixture, tmp_path):
    assert venue.find_room_ir(_close(tmp_path, "YA DXVB 112 Mix 01.wav"), irs_fixture) is None


@needs_sf
def test_override_by_hash_and_ambiguous_basename(irs_fixture, tmp_path):
    close = _close(tmp_path, "YA MRSH 412 T75 Mix 12.wav")
    assert venue.find_room_ir(close, irs_fixture, override="b")[0] == "b"
    with pytest.raises(venue.VenueError, match="hash"):
        venue.find_room_ir(close, irs_fixture, override="YA MRSH 412 T75 ROOM 2.wav")
    with pytest.raises(venue.VenueError):
        venue.find_room_ir(close, irs_fixture, override="nope.wav")


def test_room_level_reproduces_reference(monkeypatch):
    energies = {"close.wav": 7.3, "room.wav": 15.2}
    monkeypatch.setattr(venue, "ir_energy_db", lambda p: energies[Path(p).name])
    assert venue.room_level_db(-18.0, Path("close.wav"), Path("room.wav")) == pytest.approx(-29.9, abs=0.1)


@needs_sf
def test_ir_energy_db_and_room_level_from_real_wavs(tmp_path):
    close, room = _wav(tmp_path / "c.wav", 0.5), _wav(tmp_path / "r.wav", 0.5, n=400)
    assert venue.ir_energy_db(close) == pytest.approx(10 * __import__("math").log10(100 * 0.25), abs=0.01)
    # room has 6.02 dB more energy: -18 - 6.0 - 4
    assert venue.room_level_db(-18.0, close, room) == pytest.approx(-28.0, abs=0.1)


def test_missing_room_wav_is_a_clear_error():
    with pytest.raises(venue.VenueError, match="ROOM 2.wav"):
        venue.ir_energy_db(Path("/nope/YA MRSH 412 T75 ROOM 2.wav"))
