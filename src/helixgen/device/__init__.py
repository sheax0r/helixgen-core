"""helixgen.device — network control of a Line 6 Helix Stadium over the LAN.

Optional feature: requires the ``device`` extra (``pip install 'helixgen[device]'``)
for ``pyzmq`` + ``msgpack``.  Importing this package is cheap and dependency-free;
the third-party imports happen lazily when you connect or decode content.
"""
from .client import (
    HelixClient,
    HelixError,
    slot_label,
    FACTORY,
    USER,
    THROWAWAY,
    USER_IRS,
    CT_PRESET,
    CT_SETLIST,
    CT_TEMPLATE,
)

__all__ = [
    "HelixClient",
    "HelixError",
    "slot_label",
    "FACTORY",
    "USER",
    "THROWAWAY",
    "USER_IRS",
    "CT_PRESET",
    "CT_SETLIST",
    "CT_TEMPLATE",
]
