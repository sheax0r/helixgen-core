"""Legacy models that have a Stadium-native successor.

An EXPLICIT map, legacy ``model_id`` -> native ``model_id``. It is data, not a
rule: the ``HD2_`` prefix is NOT a legacy marker (every cab and nearly every
drive, delay and modulation block is ``HD2_`` with nothing newer), so a pair
is listed only when both sides model the same thing -- the same reverb type,
or the same real amplifier. A legacy model with no native counterpart
(``'63 Spring``, ``Tile``, ``Cali IV Lead``, every ``(Preamp)`` variant, ...)
is deliberately absent and is never flagged.

Surfaced by ``list-blocks`` / ``show-block`` (``superseded_by``), warned about
by ``generate``, and reported over the tone library by ``library audit``.
"""
from __future__ import annotations

from typing import Any, Iterable

SUPERSEDED_BY: dict[str, str] = {
    # --- reverbs: legacy HX algorithm -> Stadium "Dynamic" reverb, same type
    "HD2_ReverbPlateStereo": "VIC_DynPlateStereo",
    "HD2_ReverbHallStereo": "VIC_ReverbRotatingStereo",   # Dynamic Hall
    "HD2_ReverbRoomStereo": "VIC_ReverbDynRoomStereo",
    # --- amps: legacy HX model -> Agoura model of the same real amplifier.
    # Where the legacy set splits one amp into per-channel models, each maps
    # to the single Agoura model (it carries a Channel/Jack param instead).
    "HD2_AmpEVPanamaBlue": "Agoura_AmpEVPanamaBlue",      # EVH 5150III
    "HD2_AmpEVPanamaRed": "Agoura_AmpEVPanamaRed",
    "HD2_AmpGermanXtraBlue": "Agoura_AmpGermanXtraBlue",  # Bogner Ecstasy
    "HD2_AmpGermanXtraRed": "Agoura_AmpGermanXtraRed",
    "HD2_AmpRevvGenPurple": "Agoura_AmpRevvCh3Purple",    # Revv Generator 120
    "HD2_AmpRevvGenRed": "Agoura_AmpRevvCh4Red",
    "HD2_AmpUSDoubleNrm": "Agoura_AmpUSDoubleBlack",      # Fender Twin Reverb
    "HD2_AmpUSDoubleVib": "Agoura_AmpUSDoubleBlack",
    "HD2_AmpUSDeluxeNrm": "Agoura_AmpUSLuxeBlack",        # Fender Deluxe Reverb
    "HD2_AmpUSDeluxeVib": "Agoura_AmpUSLuxeBlack",
    "HD2_AmpUSSuperNorm": "Agoura_AmpUSSuperBlack",       # Fender Super Reverb
    "HD2_AmpUSSuperVib": "Agoura_AmpUSSuperBlack",
    "HD2_AmpUSPrincess": "Agoura_AmpUSPrincess76",        # Fender Princeton Reverb
    "HD2_AmpUSSmallTweed": "Agoura_AmpUS5WTweed",         # Fender tweed Champ
    "HD2_AmpTweedBluesNrm": "Agoura_AmpUSTweedman",       # Fender tweed Bassman
    "HD2_AmpTweedBluesBrt": "Agoura_AmpUSTweedman",
    "HD2_AmpBritPlexiNrm": "Agoura_AmpBritPlexi",         # Marshall Super Lead
    "HD2_AmpBritPlexiBrt": "Agoura_AmpBritPlexi",
    "HD2_AmpBritPlexiJump": "Agoura_AmpBritPlexi",
    "HD2_AmpBrit2203": "Agoura_AmpBrit2203MV",            # Marshall JCM800
    "HD2_AmpBrit2204": "Agoura_AmpBrit2203MV",
    "HD2_AmpEssexA30": "Agoura_AmpEssexTB30CC",           # Vox AC30 Top Boost
    "HD2_AmpMatchstickCh1": "Agoura_AmpMatchstick30",     # Matchless DC-30
    "HD2_AmpMatchstickCh2": "Agoura_AmpMatchstick30",
    "HD2_AmpMatchstickJump": "Agoura_AmpMatchstick30",
    "HD2_AmpMandarinRocker": "Agoura_AmpMandarinRockerMk3",  # Orange Rockerverb
    "HD2_AmpWhoWatt100": "Agoura_AmpWhoWatt103",          # Hiwatt DR103
    "HD2_AmpSoloLeadClean": "Agoura_AmpSolid100",         # Soldano SLO-100
    "HD2_AmpSoloLeadCrunch": "Agoura_AmpSolid100",
    "HD2_AmpSoloLeadOD": "Agoura_AmpSolid100",
    "HD2_AmpLine6Litigator": "Agoura_AmpLine6Prosecutor",  # Line 6 Original
    # bass
    "HD2_AmpTucknGo": "Agoura_AmpAmpegB15NF66",           # Ampeg B-15NF
    "HD2_AmpSVBeastNrm": "Agoura_AmpAmpegSVT",            # Ampeg SVT
    "HD2_AmpSVBeastBrt": "Agoura_AmpAmpegSVT",
    "HD2_AmpMandarinBass200": "Agoura_AmpMandarinPlus200",  # Orange AD200
}


def successor(library: Any, model_id: str) -> Any | None:
    """The native Block that supersedes ``model_id``, or None.

    None too when the successor is not in THIS library (an older block
    library): advice to use a block the caller cannot name is just noise.
    """
    native = SUPERSEDED_BY.get(model_id)
    if native is None:
        return None
    try:
        return library.load_block(native)
    except (KeyError, LookupError, OSError):
        return None


def successor_dict(library: Any, model_id: str) -> dict[str, str] | None:
    """``superseded_by`` as it appears in ``--json`` output."""
    b = successor(library, model_id)
    return None if b is None else {
        "display_name": b.display_name, "model_id": b.model_id}


def superseded_in(library: Any, model_ids: Iterable[str]) -> list[tuple[Any, Any]]:
    """``(legacy Block, native Block)`` for each distinct superseded model."""
    out = []
    for mid in dict.fromkeys(model_ids):
        native = successor(library, mid)
        if native is None:
            continue
        try:
            out.append((library.load_block(mid), native))
        except (KeyError, LookupError, OSError):
            continue
    return out
