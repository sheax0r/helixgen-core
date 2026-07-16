"""Tests for helixgen.naming: slugify + display-name/slug schema (spec §4).

Pure-function module -- no filesystem, no home isolation needed.
"""
from __future__ import annotations

import pytest

from helixgen import naming


class TestSlugify:
    def test_exact_assertion_from_plan(self):
        assert naming.slugify("Foo Fighters — White Limo!") == "foo-fighters-white-limo"

    def test_spaces_to_dashes(self):
        assert naming.slugify("Warm Jazz Clean") == "warm-jazz-clean"

    def test_underscores_to_dashes(self):
        assert naming.slugify("warm_jazz_clean") == "warm-jazz-clean"

    def test_em_dash_to_dash(self):
        assert naming.slugify("Foo—Bar") == "foo-bar"

    def test_en_dash_to_dash(self):
        assert naming.slugify("Foo–Bar") == "foo-bar"

    def test_strips_other_punctuation(self):
        assert naming.slugify("Let's Go! (Live)") == "lets-go-live"

    def test_collapses_repeated_dashes(self):
        assert naming.slugify("Foo   --  Bar") == "foo-bar"

    def test_strips_leading_and_trailing_dashes(self):
        assert naming.slugify("-Foo Bar-") == "foo-bar"
        assert naming.slugify("!!!Foo Bar!!!") == "foo-bar"

    def test_lowercases(self):
        assert naming.slugify("GIBSON Les Paul JR") == "gibson-les-paul-jr"

    def test_mixed_separators(self):
        assert naming.slugify("Gibson_Les Paul—Junior") == "gibson-les-paul-junior"


class TestDisplayName:
    def test_artist_song_guitar(self):
        assert (
            naming.display_name(artist="Foo Fighters", song="White Limo", guitar_short="Les Paul Jr")
            == "Foo Fighters - White Limo - Les Paul Jr"
        )

    def test_descriptor_guitar(self):
        assert (
            naming.display_name(descriptor="Warm Jazz Clean", guitar_short="Les Paul Jr")
            == "Warm Jazz Clean - Les Paul Jr"
        )

    def test_guitar_omitted_when_none(self):
        assert (
            naming.display_name(artist="Foo Fighters", song="White Limo", guitar_short=None)
            == "Foo Fighters - White Limo"
        )
        assert naming.display_name(descriptor="Warm Jazz Clean", guitar_short=None) == "Warm Jazz Clean"

    def test_guitar_omitted_by_default(self):
        assert naming.display_name(descriptor="Warm Jazz Clean") == "Warm Jazz Clean"

    def test_raises_when_neither_provided(self):
        with pytest.raises(ValueError):
            naming.display_name(guitar_short="Les Paul Jr")

    def test_raises_when_both_song_and_descriptor(self):
        with pytest.raises(ValueError):
            naming.display_name(artist="Foo Fighters", song="White Limo", descriptor="Warm Jazz Clean")

    def test_raises_when_artist_without_song(self):
        with pytest.raises(ValueError):
            naming.display_name(artist="Foo Fighters")

    def test_raises_when_song_without_artist(self):
        with pytest.raises(ValueError):
            naming.display_name(song="White Limo")


class TestLogicalSlug:
    def test_artist_song(self):
        assert naming.logical_slug(artist="Foo Fighters", song="White Limo") == "foo-fighters-white-limo"

    def test_descriptor(self):
        assert naming.logical_slug(descriptor="Warm Jazz Clean") == "warm-jazz-clean"

    def test_no_guitar_segment(self):
        # logical_slug never includes a guitar segment; it only takes artist/song/descriptor.
        assert "les-paul" not in naming.logical_slug(artist="Foo Fighters", song="White Limo")

    def test_raises_when_neither_provided(self):
        with pytest.raises(ValueError):
            naming.logical_slug()

    def test_raises_when_both_provided(self):
        with pytest.raises(ValueError):
            naming.logical_slug(artist="Foo Fighters", song="White Limo", descriptor="Warm Jazz Clean")

    def test_raises_when_artist_without_song(self):
        with pytest.raises(ValueError):
            naming.logical_slug(artist="Foo Fighters")

    def test_raises_when_song_without_artist(self):
        with pytest.raises(ValueError):
            naming.logical_slug(song="White Limo")


class TestVariantSlug:
    def test_none_guitar_slug_is_unchanged(self):
        logical = naming.logical_slug(artist="Foo Fighters", song="White Limo")
        assert naming.variant_slug(logical, None) == logical

    def test_guitar_slug_appended(self):
        logical = naming.logical_slug(artist="Foo Fighters", song="White Limo")
        assert (
            naming.variant_slug(logical, "gibson-les-paul-junior")
            == "foo-fighters-white-limo-gibson-les-paul-junior"
        )
