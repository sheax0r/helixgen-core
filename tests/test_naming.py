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

    def test_transliterates_accented_characters(self):
        # Accents should fall back to their ASCII base letter (NFKD-normalize +
        # strip combining marks) rather than being truncated away entirely.
        assert naming.slugify("Café Amp") == "cafe-amp"
        assert naming.slugify("Über Drive") == "uber-drive"
        assert naming.slugify("Mötley Crüe") == "motley-crue"
        assert naming.slugify("Beyoncé") == "beyonce"

    def test_existing_ascii_behavior_unaffected_by_transliteration(self):
        assert naming.slugify("Foo Fighters — White Limo!") == "foo-fighters-white-limo"


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

    def test_raises_when_artist_blank_and_song_provided(self):
        with pytest.raises(ValueError):
            naming.display_name(artist="", song="White Limo")

    def test_raises_when_song_blank_and_artist_provided(self):
        with pytest.raises(ValueError):
            naming.display_name(artist="Foo Fighters", song="")

    def test_raises_when_artist_and_song_both_blank(self):
        with pytest.raises(ValueError):
            naming.display_name(artist="", song="")

    def test_raises_when_descriptor_whitespace_only(self):
        with pytest.raises(ValueError):
            naming.display_name(descriptor="   ")


class TestLogicalSlug:
    def test_artist_song(self):
        assert naming.logical_slug(artist="Foo Fighters", song="White Limo") == "foo-fighters-white-limo"

    def test_descriptor(self):
        assert naming.logical_slug(descriptor="Warm Jazz Clean") == "warm-jazz-clean"

    def test_logical_slug_excludes_guitar_segment_that_variant_slug_would_add(self):
        # logical_slug is the "no guitar segment" half of the identity/guitar split:
        # it must never contain a guitar's slug, while variant_slug (fed the same
        # logical slug) is what actually appends it.
        logical = naming.logical_slug(artist="A", song="B")
        assert logical == naming.slugify("A - B")
        assert "les-paul-jr" not in logical
        assert naming.variant_slug(logical, "les-paul-jr").endswith("-les-paul-jr")

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

    def test_raises_when_artist_and_song_both_blank(self):
        with pytest.raises(ValueError):
            naming.logical_slug(artist="", song="")


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
