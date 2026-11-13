import pytest

from helixgen import venue


def test_validate_venue_accepts_vocabulary_and_none():
    assert venue.validate_venue("headphones") == "headphones"
    assert venue.validate_venue(None) is None
    with pytest.raises(ValueError, match="amp-in"):
        venue.validate_venue("stadium")
