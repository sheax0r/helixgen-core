"""Per-device tables for input endpoints and controller (FS/EXP) source IDs.

These tables are empirically derived from real .hsp exports. The outer key
is the chassis's `meta.device_id` (with `stadium_xl` as the canonical alias
used as fallback when the device_id is missing or unrecognized). Inner keys
are the logical names the spec uses.
"""
from __future__ import annotations

import sys


class ControllerError(ValueError):
    """Raised when a logical input/FS/EXP name cannot be resolved."""


INPUT_MODELS: dict[str, dict[str, str]] = {
    "stadium_xl": {
        "inst1": "P35_InputInst1",
        "inst2": "P35_InputInst2",
        "both":  "P35_InputInst1_2",
        "none":  "P35_InputNone",
    },
}


# Device-accurate controller metadata, keyed by identifier. This is the single
# source of truth: the flat CONTROLLER_SOURCE_IDS table below is DERIVED from it.
#
# Hardware layout (Line 6 Helix Stadium XL): 12 capacitive footswitches in
# 2 rows × 6 columns, numbered left-to-right — top row FS1–FS6, bottom row
# FS7–FS12. Only FS1–FS5 and FS7–FS11 are ASSIGNABLE; FS6 = MODE and
# FS12 = TAP/Tuner are reserved (see RESERVED below). Source index = FS# − 1,
# i.e. source 0x010101NN with NN = FS# − 1. Corroborated by 211 real .hsp
# exports: FS11 (0x0101010a) appears 109×; FS6 (0x01010105) appears 0×.
#
# Each record carries:
#   source_id       device controller source id (int)
#   kind            "footswitch" | "expression" | "toe"
#   row, col        physical grid position ("top"/"bottom", 1-based col) or None
#   canonical_name  human name ("Footswitch 5", "Expression Pedal 1", ...)
#   position_phrase clean directional phrase, un-nested ("top row, 5th from left")
#   aliases         free-text synonyms seeding the English→identifier sub-agent
#                   (includes the secondary "2nd from right" / "top-left" hints)
CONTROLLER_META: dict[str, dict[str, dict]] = {
    "stadium_xl": {
        "FS1": {"source_id": 0x01010100, "kind": "footswitch", "row": "top", "col": 1,
                "canonical_name": "Footswitch 1", "position_phrase": "top row, 1st from left",
                "aliases": ["top-left", "top left switch", "top left", "first from left top"]},
        "FS2": {"source_id": 0x01010101, "kind": "footswitch", "row": "top", "col": 2,
                "canonical_name": "Footswitch 2", "position_phrase": "top row, 2nd from left",
                "aliases": ["second from left top", "top row second"]},
        "FS3": {"source_id": 0x01010102, "kind": "footswitch", "row": "top", "col": 3,
                "canonical_name": "Footswitch 3", "position_phrase": "top row, 3rd from left",
                "aliases": ["third from left top", "top row middle", "top middle"]},
        "FS4": {"source_id": 0x01010103, "kind": "footswitch", "row": "top", "col": 4,
                "canonical_name": "Footswitch 4", "position_phrase": "top row, 4th from left",
                "aliases": ["fourth from left top", "top row fourth"]},
        "FS5": {"source_id": 0x01010104, "kind": "footswitch", "row": "top", "col": 5,
                "canonical_name": "Footswitch 5", "position_phrase": "top row, 5th from left",
                "aliases": ["2nd from right top", "top row second from right",
                            "top-right stomp", "fifth from left top"]},
        "FS7": {"source_id": 0x01010106, "kind": "footswitch", "row": "bottom", "col": 1,
                "canonical_name": "Footswitch 7", "position_phrase": "bottom row, 1st from left",
                "aliases": ["bottom-left", "bottom left switch", "bottom left",
                            "first from left bottom"]},
        "FS8": {"source_id": 0x01010107, "kind": "footswitch", "row": "bottom", "col": 2,
                "canonical_name": "Footswitch 8", "position_phrase": "bottom row, 2nd from left",
                "aliases": ["second from left bottom", "bottom row second"]},
        "FS9": {"source_id": 0x01010108, "kind": "footswitch", "row": "bottom", "col": 3,
                "canonical_name": "Footswitch 9", "position_phrase": "bottom row, 3rd from left",
                "aliases": ["third from left bottom", "bottom row middle", "bottom middle"]},
        "FS10": {"source_id": 0x01010109, "kind": "footswitch", "row": "bottom", "col": 4,
                 "canonical_name": "Footswitch 10", "position_phrase": "bottom row, 4th from left",
                 "aliases": ["fourth from left bottom", "bottom row fourth"]},
        "FS11": {"source_id": 0x0101010a, "kind": "footswitch", "row": "bottom", "col": 5,
                 "canonical_name": "Footswitch 11", "position_phrase": "bottom row, 5th from left",
                 "aliases": ["bottom right stomp", "second from right bottom",
                             "bottom row second from right", "2nd from right bottom",
                             "fifth from left bottom"]},
        # Expression pedals: source IDs in the 0x010201NN range (distinct from
        # the 0x010101NN FS range). 0x01020102 (likely EXP3) is out of scope.
        "EXP1": {"source_id": 0x01020100, "kind": "expression", "row": None, "col": None,
                 "canonical_name": "Expression Pedal 1",
                 "position_phrase": "onboard pedal, EXP 1 (violet LED)",
                 "aliases": ["the expression pedal", "wah pedal sweep", "expression pedal",
                             "exp 1", "onboard pedal"]},
        "EXP2": {"source_id": 0x01020101, "kind": "expression", "row": None, "col": None,
                 "canonical_name": "Expression Pedal 2",
                 "position_phrase": "onboard pedal, EXP 2 (teal LED)",
                 "aliases": ["exp 2", "second expression pedal"]},
        # The onboard expression pedal's toe switch (the click switch under the
        # pedal, engaged by pushing it fully forward). This is the standard wah
        # auto-engage: bypass toggles here while EXP1 sweeps the pedal. Source
        # 0x01010500 is observed on ~all real wah exports; it sits in its own
        # 0x010105NN bank, distinct from both the FS range (0x010101NN) and the
        # EXP-position range (0x010201NN). Identifier retained for back-compat.
        "EXP1Toe": {"source_id": 0x01010500, "kind": "toe", "row": None, "col": None,
                    "canonical_name": "Expression pedal toe switch",
                    "position_phrase": "the toe switch under the expression pedal "
                                       "(push the pedal fully forward to click it); "
                                       "standard wah auto-engage",
                    "aliases": ["toe switch", "wah engage", "pedal toe", "exp toe",
                                "wah auto-engage"]},
    },
}


# Controller response-curve vocabulary, in the device's own enum order.
# Source of evidence: the Stadium app binary's serializer string table (1.3.2)
# lists exactly these 11 names contiguously with the other controller enums
# (`targetbypass`, `latching`, `continuous`, ...); the device content format's
# `ctrl.curv` int is the 0-based index into this table (every real controller
# observed carries curv=5 == "linear", the only value in the 211-export
# corpus). Non-"linear" values are EXPERIMENTAL: vocabulary-evidenced, not
# corpus-observed.
CURVES = (
    "slow5", "slow4", "slow3", "slow2", "slow1",
    "linear",
    "fast1", "fast2", "fast3", "fast4", "fast5",
)

# Footswitch scribble-strip color palette: `.hsp` name -> device `pm__`
# `preset.floorboard.stomp.*.color` int. Anchored by live device pulls paired
# with the same presets' .hsp exports (auto=1, red=2, dkorange=3, ltorange=4,
# purple=9, white=11); the remaining names are inferred from the app binary's
# palette-order string table (EXPERIMENTAL: none, yellow, green, turquoise,
# blue, pink).
FS_COLORS = {
    "none": 0, "auto": 1, "red": 2, "dkorange": 3, "ltorange": 4,
    "yellow": 5, "green": 6, "turquoise": 7, "blue": 8, "purple": 9,
    "pink": 10, "white": 11,
}

# The device stores at most 12 scribble-strip characters (a 13-char .hsp
# label was observed truncated to 12 on the hardware).
FS_LABEL_MAX = 12


def curve_index(name: str) -> int:
    """0-based device enum index for a curve name. Raises ControllerError."""
    try:
        return CURVES.index(name)
    except ValueError:
        raise ControllerError(
            f"Unknown curve {name!r}. Valid curves: {list(CURVES)}."
        ) from None


def color_int(name: str) -> int:
    """Device palette int for a scribble-strip color name. Raises ControllerError."""
    try:
        return FS_COLORS[name]
    except KeyError:
        raise ControllerError(
            f"Unknown footswitch color {name!r}. "
            f"Valid colors: {sorted(FS_COLORS)}."
        ) from None


# Reserved footswitches: physically present, addressable-looking, but NOT
# assignable to a block. Resolving one raises a tailored ControllerError.
# Keyed identifier → (source_id, human label).
RESERVED: dict[str, dict[str, tuple[int, str]]] = {
    "stadium_xl": {
        "FS6":  (0x01010105, "MODE"),
        "FS12": (0x0101010b, "TAP/Tuner"),
    },
}


# Flat name→source-id table, DERIVED from CONTROLLER_META (single source of
# truth). Kept for the forward/reverse resolvers and existing callers.
CONTROLLER_SOURCE_IDS: dict[str, dict[str, int]] = {
    device: {cid: rec["source_id"] for cid, rec in meta.items()}
    for device, meta in CONTROLLER_META.items()
}


# Command Center "Instant" command slots (backlog #16). These are command-only
# footswitch-mode slots — they carry MIDI/Preset-Snapshot commands, they do NOT
# bypass a block, so they live outside CONTROLLER_META (which is block-targeting
# FS/EXP). Source ids anchored by `Epic Lots of EQ.hsp` (Instant 1/2 =
# 0x04040100/0x04040101) + the ZZCAP-CC device capture (Instant 1 = device
# locl 0, srcs type 4). Instant N = 0x04040100 + (N-1), N in 1..6.
INSTANT_SOURCE_IDS: dict[str, dict[str, int]] = {
    "stadium_xl": {f"Instant{n}": 0x04040100 + (n - 1) for n in range(1, 7)},
}


def resolve_command_source(device_id, switch: str) -> int:
    """Resolve a Command Center ``switch`` identifier to its ``.hsp`` source id
    (backlog #16). Accepts the assignable footswitches ``FS1``–``FS5`` /
    ``FS7``–``FS11`` (same source ids as controllers) and ``Instant1``–
    ``Instant6``. Reserved ``FS6``/``FS12`` raise the tailored error. EXP
    continuous commands are out of scope (no ``.hsp`` source anchored), and are
    rejected with a clear message rather than silently accepted."""
    device = _resolve_device(device_id)
    instants = INSTANT_SOURCE_IDS.get(device, {})
    if switch in instants:
        return instants[switch]
    if switch in ("EXP1", "EXP2", "EXP1Toe"):
        raise ControllerError(
            f"{switch} continuous/expression commands are out of scope for "
            f"Command Center authoring; assignable command slots are "
            f"FS1–FS5, FS7–FS11 and Instant1–Instant6."
        )
    # Footswitches (and reserved-switch tailored errors) reuse the controller
    # resolver's table + messages.
    return resolve_controller_source(device_id, switch)


# Observed `meta.device_id` values that identify Stadium XL hardware. Real
# exports carry a numeric id (e.g. 2490368), not the canonical string —
# this set lets us recognise those without warning. Add new values as they
# are observed in the field.
STADIUM_XL_DEVICE_IDS: frozenset = frozenset({"stadium_xl", 2490368})


_warned_devices: set = set()  # de-dup warnings within a single process


def _resolve_device(device_id) -> str:
    """Pick the active device table key, falling back to stadium_xl.

    Both INPUT_MODELS and CONTROLLER_SOURCE_IDS use the same outer keys
    (currently only "stadium_xl"). _resolve_device is shared by both
    resolve_input_model and resolve_controller_source — keep the outer
    keys of both tables in sync when adding new device support.

    Accepts both the canonical string key and the numeric `meta.device_id`
    values real chassis exports carry; numeric aliases for known hardware
    are listed in STADIUM_XL_DEVICE_IDS.
    """
    if isinstance(device_id, str) and device_id in INPUT_MODELS and device_id in CONTROLLER_SOURCE_IDS:
        return device_id
    if device_id in STADIUM_XL_DEVICE_IDS:
        return "stadium_xl"
    if device_id is not None and device_id not in _warned_devices:
        print(
            f"warning: chassis device_id {device_id!r} not in controller tables; "
            f"assuming stadium_xl.",
            file=sys.stderr,
        )
        _warned_devices.add(device_id)
    return "stadium_xl"


def resolve_input_model(device_id: str, mode: str) -> str:
    """Look up the Stadium model_id for a logical input mode.

    Raises ControllerError listing valid modes if the mode is unknown.
    """
    table = INPUT_MODELS[_resolve_device(device_id)]
    if mode not in table:
        raise ControllerError(
            f"Unknown input mode {mode!r}. Valid modes: {sorted(table.keys())}."
        )
    return table[mode]


def resolve_controller_source(device_id: str, logical_name: str) -> int:
    """Look up a controller source ID for a logical FS/EXP name.

    Reserved switches (`FS6` = MODE, `FS12` = TAP/Tuner) raise a *tailored*
    ControllerError explaining they are not assignable. Any other unknown name
    raises the generic error listing the valid canonical set.
    """
    device = _resolve_device(device_id)
    table = CONTROLLER_SOURCE_IDS[device]
    if logical_name not in table:
        reserved = RESERVED.get(device, {})
        if logical_name in reserved:
            _sid, label = reserved[logical_name]
            raise ControllerError(
                f"{logical_name} is the {label} switch and is not assignable; "
                f"assignable switches are FS1–FS5, FS7–FS11 (plus EXP1, EXP2, EXP1Toe)."
            )
        raise ControllerError(
            f"Unknown controller name {logical_name!r}. "
            f"Valid names: {sorted(table.keys())}."
        )
    return table[logical_name]


def english_for_controller(device_id, identifier: str) -> str:
    """Render a controller identifier as English name + physical position.

    E.g. ``english_for_controller("stadium_xl", "FS5")`` →
    ``"Footswitch 5 (top row, 5th from left)"``. Raises ControllerError (with
    the tailored reserved / valid-set message) for a non-canonical identifier.
    """
    device = _resolve_device(device_id)
    meta = CONTROLLER_META[device]
    if identifier not in meta:
        # Reuse resolve_controller_source's tailored / generic error message.
        resolve_controller_source(device_id, identifier)
    rec = meta[identifier]
    return f"{rec['canonical_name']} ({rec['position_phrase']})"


def controller_mapping(device_id) -> list[dict]:
    """Return the full canonical controller table as a JSON-serialisable list.

    One dict per assignable identifier, ordered as in CONTROLLER_META. Each row
    carries the identifier, hex + int source id, kind, grid position, canonical
    name, position phrase, the rendered English string, and aliases — the data
    the English→identifier translation sub-agent and the MCP tool consume.
    """
    device = _resolve_device(device_id)
    meta = CONTROLLER_META[device]
    out: list[dict] = []
    for cid, rec in meta.items():
        out.append({
            "id": cid,
            "source": f"0x{rec['source_id']:08x}",
            "source_id": rec["source_id"],
            "kind": rec["kind"],
            "row": rec["row"],
            "col": rec["col"],
            "name": rec["canonical_name"],
            "position": rec["position_phrase"],
            "english": f"{rec['canonical_name']} ({rec['position_phrase']})",
            "aliases": list(rec["aliases"]),
        })
    return out


def is_position_switch(logical_name: str) -> bool:
    """True for expression-pedal toe/position switches (e.g. "EXP1Toe").

    Unlike a digital footswitch, a position switch bound to a block's bypass
    needs explicit min/max/threshold on its targetbypass controller for the
    device to honor the toggle — see _build_fs_controller. Digital footswitches
    work with null bounds.
    """
    return logical_name.endswith("Toe")


def input_mode_for_model(device_id, model: str) -> str | None:
    """Reverse of resolve_input_model: Stadium input model_id → logical mode."""
    table = INPUT_MODELS[_resolve_device(device_id)]
    for mode, model_id in table.items():
        if model_id == model:
            return mode
    return None


def controller_name_for_source(device_id, source_id: int) -> str | None:
    """Reverse of resolve_controller_source: source id → logical FS/EXP name."""
    table = CONTROLLER_SOURCE_IDS[_resolve_device(device_id)]
    for name, sid in table.items():
        if sid == source_id:
            return name
    return None


def command_switch_for_source(device_id, source_id: int) -> str | None:
    """Reverse of resolve_command_source: source id → command switch name
    (``FS*`` or ``Instant*``). Returns None for an unrecognised source."""
    device = _resolve_device(device_id)
    for name, sid in INSTANT_SOURCE_IDS.get(device, {}).items():
        if sid == source_id:
            return name
    name = controller_name_for_source(device_id, source_id)
    # Only footswitches carry commands in scope (EXP continuous out of scope).
    if name is not None and name.startswith("FS"):
        return name
    return None
