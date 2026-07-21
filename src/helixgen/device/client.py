"""HelixClient — network control of a Line 6 Helix Stadium over the LAN.

Speaks the editor's own protocol: OSC messages over ZeroMQ (ZMTP 3.0), msgpack
blob payloads.  Connects a DEALER to the device's ROUTER on port 2002 for
request/response RPC.  See ``docs/helix-protocol.md`` for the wire format.

``pyzmq`` and ``msgpack`` are imported lazily so importing helixgen without the
``device`` extra never fails; construct/connect raises a clear error instead.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import time
from enum import IntEnum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .osc import osc_encode, parse_osc_message
from . import content as _content
from . import settings as _settings
from . import globaleq as _globaleq
from . import defs as _defs
from . import irmd as _irmd

logger = logging.getLogger(__name__)


class _ConnectionDropped(Exception):
    """Internal signal: a send/recv raised the zmq error type mid-RPC — a likely
    connection drop that _rpc should try to recover from by reconnecting."""


class Container(IntEnum):
    """Top-level device content containers (addressed by negative cid)."""

    FACTORY = -1
    POOL = -2            # the preset pool; the ONLY container /CreateContent accepts
    SETLISTS_ROOT = -5   # holds the setlist items (cctp==1001); NOT a setlist itself
    USER_IRS = -11


class Cctp(IntEnum):
    """Content-type tags (the ``cctp`` field on a container item)."""

    PRESET = 1000
    SETLIST = 1001
    TEMPLATE = 1002
    REFERENCE = 1003   # a setlist entry pointing (rcid) at a pool preset


# -- backward-compat module aliases -----------------------------------------
# Existing call sites import these bare names; keep them pointing at the enum
# ints so nothing breaks in one commit.
FACTORY = Container.FACTORY
USER = Container.POOL
SETLISTS_ROOT = Container.SETLISTS_ROOT
# DEPRECATED: ``-5`` is really the setlists ROOT, not the "throwaway" setlist
# (Throwaway is a child setlist with its own positive cid under -5). Kept as an
# alias (== SETLISTS_ROOT) only so old imports still resolve.
THROWAWAY = Container.SETLISTS_ROOT
USER_IRS = Container.USER_IRS

# Content types (cctp).
CT_PRESET = Cctp.PRESET
CT_SETLIST = Cctp.SETLIST
CT_TEMPLATE = Cctp.TEMPLATE

# ``/CreateContent`` ctype values. A container item's ``type`` field carries
# the ctype it was created with: presets are ``2`` (long-observed) and setlist
# items are ``1003`` (live-verified 2026-07-14 — /CreateContent(-5, pos, 1003,
# {name}) creates a working setlist; see the IR + library polish design spec).
CTYPE_PRESET = 2
CTYPE_SETLIST = 1003

_SLOT_LETTERS = "ABCD"

# The named ``--setlist`` keywords the CLI accepts, mapped to their container
# constant. Canonical keyword->container resolver (resolver pattern, #14) — the
# CLI callers wrap this for their own exception type instead of cloning the
# dict.
_SETLIST_KEYWORDS = {
    "user": Container.POOL,
    "factory": Container.FACTORY,
    "throwaway": Container.SETLISTS_ROOT,
}


def container_for_setlist_keyword(name: str) -> int:
    """Map a ``--setlist`` keyword (``user``/``factory``/``throwaway``) to its
    container constant. Case/whitespace-insensitive.

    Raises ``ValueError`` (naming the valid keywords) for anything else, so a
    typo reports itself rather than silently targeting the wrong container.
    """
    key = (name or "").strip().lower()
    try:
        return _SETLIST_KEYWORDS[key]
    except KeyError as e:
        raise ValueError(
            f"unknown setlist {name!r}; valid: {sorted(_SETLIST_KEYWORDS)}"
        ) from e


class HelixError(Exception):
    """A device RPC failed or the network client could not be used."""


def slot_label(posi: Optional[int]) -> str:
    """Device ``posi`` -> Helix bank/slot label, e.g. 0 -> '1A', 5 -> '2B'."""
    if posi is None:
        return ""
    return f"{posi // 4 + 1}{_SLOT_LETTERS[posi % 4]}"


#: The device's per-DSP block grid size (slots 0..27; inputs sit at 0/14,
#: outputs at 13/27 on a Stadium XL).
GRID_SLOTS = 28

#: The device property carrying the ACTIVE preset's cid (discovered via
#: ``/MatchingPropertyDefinitionsGet``, live-verified 2026-07-15 fw 1.3.2:
#: it reflected the player's own panel selection before any network load,
#: and tracks ``/LoadPresetWithCID``).
ACTIVE_PRESET_KEY = "server.active.preset.id"


def _grid_slot(block: int) -> int:
    """Validate a public block coordinate (the device's DSP **grid slot**).

    The live-ops wire commands (``/BlockEnableSet``, ``/ModelSet``,
    ``/ParamValueSet``, ``/ParamValueGet``) address a block by its grid
    slot — the int PAIRED with each block dict in the ``sfg_.flow[dsp].blks``
    flat list (0..27; e.g. inputs 0/14, outputs 13/27) — which is exactly
    what ``device blocks`` / :meth:`HelixClient.edit_buffer_blocks` now
    report, passed through unchanged.

    ERRATUM (2026-07-15, fw 1.3.2) to the 2026-07-14 ``(key-1)/2`` finding:
    that formula translated the block's *flat-list position* and only
    coincided with the true slot while a chain occupies contiguous slots
    from 0. HW-proof: ``/ParamValueGet`` answers at the paired key (output
    block ``gain`` pid 2 read 6.0 dB at slot 13) and a ``/ParamValueSet``
    at slot 13 landed and read back, while the formula index addressed the
    wrong slot whenever the grid had gaps (the root cause of the "output
    block set-param always fails" live finding).
    """
    k = int(block)
    if not 0 <= k < GRID_SLOTS:
        raise HelixError(
            f"block {block!r} is not a DSP grid slot (int 0..{GRID_SLOTS - 1})"
            " — use the coordinates printed by `device blocks`")
    return k


class _RawOps:
    """The raw, model-blind protocol primitives, namespaced off a client as
    ``client._raw``.

    These are composable but easy to misuse (they will happily orphan a
    referenced pool preset, or try a /CreateContent the device rejects). Prefer
    the model-correct public methods on :class:`HelixClient`
    (``install_into_pool`` / ``reference_into_setlist`` / ``remove_reference`` /
    ``mirror_setlist``). Each op here just delegates to the ``_``-prefixed
    method that holds the real body.
    """

    def __init__(self, client: "HelixClient"):
        self._c = client

    def create_content(self, container: int, pos: int, name: str,
                       ctype: int = 2) -> Optional[int]:
        return self._c._create_content(container, pos, name, ctype)

    def create_copy(self, container: int, src_cids: Sequence[int], pos: int) -> bool:
        return self._c._create_copy(container, src_cids, pos)

    def create_from(self, src_cid: int, container: int, pos: int) -> Optional[int]:
        return self._c._create_from(src_cid, container, pos)

    def set_content_data(self, cid: int, blob: bytes) -> bool:
        return self._c._set_content_data(cid, blob)

    def delete(self, container: int, cids: Sequence[int]) -> bool:
        return self._c._delete(container, cids)

    def save_preset_with_cid(self, cid: int, block_count: int = 0) -> bool:
        return self._c._save_preset_with_cid(cid, block_count)

    def save_edit_buffer_to(self, container: int, pos: int, name: str) -> Optional[int]:
        return self._c._save_edit_buffer_to(container, pos, name)

    def push_to_slot(self, container: int, pos: int, name: str,
                     blob: bytes, *,
                     prechecked_empty: bool = False) -> Optional[int]:
        return self._c._push_to_slot(container, pos, name, blob,
                                     prechecked_empty=prechecked_empty)


class HelixClient:
    def __init__(self, ip: Optional[str] = None, port: int = 2002,
                 *, connect_settle: float = 0.6, rpc_timeout: float = 2.0,
                 reconnect_tries: int = 3, reconnect_backoff: float = 0.5):
        if ip is None:
            # #74 resolution chain ($HELIXGEN_HELIX_IP > persisted device
            # record) — there is NO hardcoded default IP. Fails fast with
            # an instructive HelixError instead of stalling on a connect.
            from helixgen.device import discovery

            try:
                ip = discovery.resolve_ip()
            except discovery.IPResolutionError as e:
                raise HelixError(str(e)) from e
        self.ip = ip
        self.port = port
        self.connect_settle = connect_settle
        self.rpc_timeout = rpc_timeout
        # bounded auto-reconnect on a mid-RPC connection drop (the Stadium's
        # network stack is flaky). Backoff grows per attempt.
        self.reconnect_tries = reconnect_tries
        self.reconnect_backoff = reconnect_backoff
        self._rid = itertools.count(1000)
        self._zmq = None
        self._ctx = None
        self.sock = None
        self.poller = None
        # raw protocol primitives live behind this namespace (see _RawOps).
        self._raw = _RawOps(self)
        # nesting depth of an active mutating() 2001-subscription context.
        self._mutating = 0
        # settle time after opening the mutating() subscriber (see mutating()).
        self.mutate_settle = 0.6
        # bounded settle between the retries of the confirming re-list that
        # decides whether a /CreateContent actually landed (#38; the container
        # index lags a just-completed write). Budgeted to match the other
        # lag-absorbing loop over the same index (maintenance.
        # resolve_device_ir_live) — under-waiting here turns a landed write
        # into a confident "it failed", and a retry then duplicates content.
        self.create_confirm_delay = 0.4
        self.create_confirm_tries = 5

    # -- lifecycle ---------------------------------------------------------
    def _load_zmq(self):
        try:
            import zmq
        except ImportError as exc:
            raise HelixError(
                "the device feature needs pyzmq; install with "
                "`pip install 'helixgen[device]'`"
            ) from exc
        return zmq

    def _load_msgpack(self):
        try:
            import msgpack
        except ImportError as exc:
            raise HelixError(
                "the device feature needs msgpack; install with "
                "`pip install 'helixgen[device]'`"
            ) from exc
        return msgpack

    def _open_socket(self) -> None:
        """Create the DEALER socket + poller and settle for the ZMTP handshake.

        Shared by :meth:`connect` and :meth:`reconnect`. Deliberately performs
        NO ``verify`` read — that lives in ``connect`` so a reconnect can't
        recurse back through ``_rpc`` while it is mid-recovery.
        """
        zmq = self._load_zmq()
        try:
            self._zmq = zmq
            self._ctx = zmq.Context.instance()
            self.sock = self._ctx.socket(zmq.DEALER)
            self.sock.setsockopt(zmq.LINGER, 0)
            self.sock.connect(f"tcp://{self.ip}:{self.port}")
            self.poller = zmq.Poller()
            self.poller.register(self.sock, zmq.POLLIN)
        except zmq.ZMQError as exc:
            raise HelixError(f"could not open device socket: {exc}") from exc
        time.sleep(self.connect_settle)  # let the ZMTP handshake complete

    def connect(self, verify: bool = True) -> "HelixClient":
        """Open the DEALER socket. If ``verify``, confirm a device actually
        answers (a lazily-connected socket to a dead/wrong host never errors on
        its own, which would otherwise make every read look like an empty
        result)."""
        self._open_socket()
        if verify and self.get_ref(USER) is None:
            self.close()
            raise HelixError(
                f"no Helix Stadium answered at {self.ip}:{self.port} "
                "(wrong IP, device off, or Remote Access disabled?)")
        return self

    def reconnect(self) -> "HelixClient":
        """Tear down and re-open the DEALER socket/poller after a suspected
        connection drop. Re-creates the socket and re-settles WITHOUT the
        ``verify`` read-loop (which would re-enter ``_rpc`` mid-recovery)."""
        self.close()
        self._open_socket()
        return self

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "HelixClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- core RPC ----------------------------------------------------------
    def _rpc(self, addr: str, args: Sequence, *,
             first_wait: Optional[float] = None, settle: float = 0.4,
             raw_blobs: bool = False) -> List[tuple]:
        """Send a command (a request id is prepended) and gather reply frames.

        Returns a list of ``(reply_addr, decoded_args)`` whose first arg matches
        our request id.  Blob args are msgpack-decoded unless ``raw_blobs`` is
        set (then they stay as raw ``bytes`` — used for the ``_sbepgsm`` blob).
        """
        if self.sock is None:
            raise HelixError("client is not connected; call connect() first")
        if first_wait is None:
            first_wait = self.rpc_timeout
        # Bounded auto-reconnect: only an ACTUAL send/recv exception (the zmq
        # error type, surfaced as _ConnectionDropped) triggers a reconnect+retry
        # cycle. An empty reply is NOT treated as a drop (some commands reply
        # with nothing) — it just returns [] as before. This conservative rule
        # avoids false retries while still recovering from a dead socket.
        attempt = 0
        while True:
            try:
                return self._rpc_send_recv(
                    addr, args, first_wait=first_wait, settle=settle,
                    raw_blobs=raw_blobs)
            except _ConnectionDropped as drop:
                attempt += 1
                if attempt > self.reconnect_tries or self.sock is None:
                    raise HelixError(
                        f"device connection lost after {self.reconnect_tries} "
                        "reconnect attempts; if this persists, reboot the Helix"
                    ) from drop.__cause__
                logger.warning(
                    "device connection dropped mid-RPC (%s); reconnect attempt "
                    "%d/%d", drop.__cause__, attempt, self.reconnect_tries)
                time.sleep(self.reconnect_backoff * attempt)
                self.reconnect()

    def _rpc_send_recv(self, addr: str, args: Sequence, *,
                       first_wait: float, settle: float,
                       raw_blobs: bool) -> List[tuple]:
        """One send + reply-gather pass. Raises :class:`_ConnectionDropped` if a
        send/recv raises the zmq error type (so :meth:`_rpc` can reconnect and
        retry); a malformed reply still raises :class:`HelixError` directly (it
        is a protocol fault, not a connection drop, and must not retry)."""
        # zmq's exception type, or an empty tuple when a fake socket is injected
        zmq_error = getattr(self._zmq, "ZMQError", ()) if self._zmq else ()
        rid = next(self._rid)
        try:
            self.sock.send(osc_encode(addr, [("i", rid)] + list(args)))
        except zmq_error as exc:
            raise _ConnectionDropped() from exc
        replies: List[tuple] = []
        got = False
        while True:
            # Keep the full first_wait window until OUR reply lands; an
            # unsolicited frame must not shrink it to the settle window.
            events = dict(self.poller.poll(int((settle if got else first_wait) * 1000)))
            if not events:
                break
            try:
                raw = self.sock.recv()
            except zmq_error as exc:
                raise _ConnectionDropped() from exc
            i = raw.find(b"/")
            if i < 0:
                continue
            try:
                raddr, rargs, _ = parse_osc_message(raw, i)
                if raw_blobs:
                    dec = [v for _t, v in rargs]
                else:
                    dec = [_content.decode_blob(v) if t == "b" else v
                           for t, v in rargs]
            except (ValueError, IndexError, KeyError, RuntimeError) as exc:
                raise HelixError(f"malformed device reply: {exc}") from exc
            if dec and dec[0] == rid:
                replies.append((raddr, dec))
                got = True  # only our matching reply narrows the poll window
        return replies

    def _ok(self, replies: List[tuple]) -> bool:
        for addr, args in replies:
            if addr == "/status" and len(args) >= 2:
                return args[1] == 0
        return False

    # -- reads -------------------------------------------------------------
    def list_container(self, cid: int, *, strict: bool = False) -> List[Dict[str, Any]]:
        """List a container's items.

        With ``strict=False`` (the legacy default) a timeout or an undecodable
        reply silently reads as an empty/partial list — fine for interactive
        browsing, **catastrophic for destructive planning** (an "empty pool"
        makes every user IR look like an orphan to ``ir-prune``).
        ``strict=True`` distinguishes the failure modes:

        * zero reply frames (timeout / connection drop) → :class:`HelixError`;
        * a blob argument that failed msgpack decoding (truncated chunked
          reply) → :class:`HelixError`;
        * a genuine empty-array reply → ``[]``.
        """
        replies = self._rpc("/GetContainerContents", [("i", cid)])
        if strict and not replies:
            raise HelixError(
                f"no reply listing container {cid} (timeout or connection "
                "drop); refusing to treat it as empty — retry, and reboot the "
                "Helix if it persists")
        items: List[Dict[str, Any]] = []
        for _addr, args in replies:
            for a in args:
                if isinstance(a, list):
                    items.extend(x for x in a if isinstance(x, dict))
                elif isinstance(a, dict):
                    items.append(a)
                elif strict and isinstance(a, (bytes, bytearray)):
                    raise HelixError(
                        f"undecodable listing blob for container {cid} "
                        "(truncated chunked reply?); refusing a partial "
                        "listing — retry")
        return items

    def list_presets(self, container: int = USER, *,
                     strict: bool = False) -> List[Dict[str, Any]]:
        presets = [m for m in self.list_container(container, strict=strict)
                   if m.get("cctp") == CT_PRESET]
        presets.sort(key=lambda m: m.get("posi", 1 << 30))
        return presets

    def list_setlists(self, *, strict: bool = False) -> List[Dict[str, Any]]:
        """Return the device's real user setlists.

        A setlist is an item of type ``cctp==1001`` living inside the setlists
        root container ``-5``. (The old implementation swept a hard-coded
        ``(FACTORY, USER, THROWAWAY)`` list of container ids — but ``-5`` is the
        *root*, not a setlist, so that was wrong.) Each returned dict carries at
        least ``cid_``, ``name``, ``posi``.
        """
        out = []
        for m in self.list_container(Container.SETLISTS_ROOT, strict=strict):
            if m.get("cctp") == Cctp.SETLIST:
                out.append(dict(m))
        out.sort(key=lambda m: m.get("posi", 1 << 30))
        return out

    # spec uses both names; ``list_setlists`` is canonical.
    def list_user_setlists(self) -> List[Dict[str, Any]]:
        """Alias of :meth:`list_setlists` — enumerate the real user setlists."""
        return self.list_setlists()

    def list_setlists_by_name(
        self, name: str, *, strict: bool = True,
        setlists: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Every user setlist whose display name matches ``name``
        case-insensitively (``strip().casefold()`` on both sides), in device
        (posi) order.

        This is the single home for the setlist name-match (#52):
        ``resolve_setlist_cid`` takes the first result's cid, and the
        ``device reorder`` numeric-argument clash check needs the *full* match
        set (to warn/raise on a digit-named collision). Pass a pre-fetched
        listing as ``setlists=`` to filter it without a second RPC (the reorder
        path already holds one strict listing it also scans for cid membership);
        otherwise the listing is fetched via ``list_setlists(strict=strict)``.
        """
        want = name.strip().casefold()
        source = setlists if setlists is not None else self.list_setlists(strict=strict)
        return [m for m in source
                if str(m.get("name", "")).strip().casefold() == want]

    def resolve_setlist_cid(self, name: str, *, strict: bool = True) -> Optional[int]:
        """Case-insensitively match a user setlist by ``name`` and return its
        ``cid_`` (the positive setlist container id), or ``None`` if no setlist
        with that name exists on the device.

        ``strict`` (default ``True``) is threaded straight through to
        ``list_setlists`` — a timeout or an undecodable listing raises
        :class:`HelixError` instead of silently reading as "no setlist named
        that" (backlog #39). Every auto-creating caller (``device setlist
        create``'s pre-check, ``duplicate``'s destination check, ``sync``'s
        resolve step, ``import-hss``) depends on this: with a lenient read, a
        network hiccup could make an *existing* setlist look absent, and the
        caller would then mint a second, duplicate-named one. ``None`` from
        this method now means "definitively absent" (a clean listing that
        genuinely doesn't contain ``name``) — never "couldn't tell". Pass
        ``strict=False`` only for a deliberately best-effort/retry lookup
        (e.g. re-resolving a cid moments after a create, where the caller
        already has its own retry loop and a documented fallback)."""
        matches = self.list_setlists_by_name(name, strict=strict)
        return matches[0].get("cid_") if matches else None

    @staticmethod
    def _hex_hash(h: Any) -> Optional[str]:
        """Normalize a device IR hash (raw 16 bytes) to a 32-char hex string
        (== helixgen's ``irhash``). The string branch shares the reconciled
        normalizer with ``sftp._addcontent_hash`` (#53: validate len 32 +
        lowercase)."""
        if isinstance(h, (bytes, bytearray)):
            return _irmd.irmd_to_irhash(h)
        if isinstance(h, str):
            return _irmd.normalize_hash_string(h)
        return None

    def list_irs(self, *, strict: bool = False,
                 settle: bool = True) -> List[Dict[str, Any]]:
        """Return the device's user IRs: ``{cid_, name, hash, mono, posi}``.

        ``hash`` is normalized to the 32-hex Stadium IR hash (== helixgen
        ``irhash``).

        The read happens under :meth:`mutating` (``settle=True``, the default):
        the ``-11`` container index only propagates promptly to a client that
        holds a 2001 change-stream subscription, so an unsubscribed read can
        under-report for **minutes** after an IR upload — the reported symptom
        of 24 entries listed while a 25th was genuinely present and resolving
        through :meth:`ir_path_for_hash` (#38 Task 4). That is index lag, not
        truncation. ``settle=False`` skips the subscribe for a caller that
        already holds one (nesting is cheap, so passing it is rarely needed)
        or deliberately wants a bare read.

        This listing is still **not** authoritative about absence — nothing
        forces the index to have caught up. A caller that needs a definitive
        "is this hash on the device?" must cross-check the point lookup; see
        :meth:`device_ir_hashes`'s ``verify``.
        """
        ctx = self.mutating() if settle else contextlib.nullcontext()
        with ctx:
            listing = self.list_container(USER_IRS, strict=strict)
        irs = []
        for m in listing:
            hh = self._hex_hash(m.get("hash"))
            if hh is None:
                continue
            m = dict(m)
            m["hash"] = hh
            irs.append(m)
        irs.sort(key=lambda m: m.get("posi", 1 << 30))
        return irs

    def device_ir_hashes(self, *, verify: Optional[Sequence[str]] = None) -> set:
        """The set of IR hashes (hex) present on the device.

        ``verify`` — hashes the caller is about to declare **missing**. Each
        one absent from the (lag-prone, #38 Task 4) container listing is
        re-checked against :meth:`ir_path_for_hash`, the authoritative point
        lookup that reflects an import immediately. A hash the point lookup
        resolves is present: it is added to the result and the stale listing
        is logged as a warning rather than silently believed.

        Caveat — the point lookup answers "the backing file is on the device",
        which the **wedged** state (file + path index resolving, no ``-11``
        registry entry; see ``maintenance.delete_device_ir``'s
        ``force_wedge``) satisfies without the IR being usable by a preset.
        A wedged IR is therefore reported present here and won't be
        re-uploaded, so the warning names the possibility. The lag case is far
        commoner and its false "missing" is the one that misleads users, hence
        the trade.

        The listing is **strict** (#40): a dropped or truncated ``-11`` reply
        must not decode as "the device has no IRs" and send every referenced
        hash down the point-lookup path — if the transport dropped the listing
        it is likely to drop the lookups too, and the caller would then be told
        the preset's IRs are all missing. A failed listing raises instead; both
        call paths (``bridge.check_irs`` under ``_install_hsp_open``, and the
        CLI) already handle :class:`HelixError`.
        """
        hashes = {m["hash"] for m in self.list_irs(strict=True)}
        for hh in (verify or ()):
            if hh in hashes:
                continue
            try:
                path = self.ir_path_for_hash(hh, strict=True)
            except HelixError as exc:
                logger.warning(
                    "IR %s is missing from the device's IR listing and the "
                    "path lookup failed (%s) — reporting it missing "
                    "unverified; re-run to confirm before acting on it", hh, exc)
                continue
            if path:
                logger.warning(
                    "IR %s is missing from the device's IR listing but "
                    "resolves to %s — the container index is stale; treating "
                    "the IR as present (backlog #38). If a preset using it is "
                    "silent, the IR may instead be wedged (file present, never "
                    "re-listed): see device delete-ir --force-wedge", hh, path)
                hashes.add(hh)
        return hashes

    def ir_path_for_hash(self, hash_hex: str, *,
                         strict: bool = False) -> Optional[str]:
        """Return the device's on-disk path for an IR ``hash`` (hex), or ``None``
        if the device doesn't have it registered.

        This is the **reliable** registration check — it reflects a newly
        imported IR immediately, unlike ``list_irs``/``/GetContainerContents``
        (whose container listing lags after a write). Uses the editor's own
        ``/IrPathForHashGet`` (16-byte blob arg).

        ``strict`` (#40, same contract as :meth:`list_container`) raises
        :class:`HelixError` when the device answered nothing **usable** — no
        reply at all, or replies none of which carried a path (a truncated or
        undecodable frame) — instead of collapsing either into the same
        ``None`` that means "not registered". Callers that use this lookup to *overturn* a missing
        verdict must pass it: the flaky transport is exactly the condition the
        cross-check exists to survive, and a silent false "missing" there
        re-uploads an IR the device already has."""
        try:
            blob = _irmd.irhash_to_irmd(hash_hex)
        except ValueError:
            return None
        replies = self._rpc("/IrPathForHashGet", [("b", blob)])
        for _addr, args in replies:
            # reply /xxxIrxPathForHash1 [reqid, path]; empty path == not present
            if len(args) >= 2 and isinstance(args[1], str):
                return args[1] or None
        if strict:
            # No reply at all AND a reply that carried no decodable path are the
            # same thing to a caller overturning a "missing" verdict: neither
            # answers the question, and collapsing either into the ``None`` that
            # means "not registered" re-uploads an IR the device already has. A
            # truncated/undecodable frame is the *likelier* transport failure,
            # so it must raise too — not just the empty-reply case.
            raise HelixError(
                f"/IrPathForHashGet for {hash_hex} returned no usable reply "
                f"({len(replies)} frame(s), none carrying a path) — cannot "
                f"tell whether the device has the IR")
        return None

    def get_ref(self, cid: int) -> Optional[Dict[str, Any]]:
        for _addr, args in self._rpc("/GetContentRef", [("i", cid)]):
            for a in args:
                if isinstance(a, dict):
                    return a
        return None

    def find_by_pos(self, container: int, pos: int, *,
                    strict: bool = False) -> Optional[Dict[str, Any]]:
        """Return the item occupying ``pos`` in ``container``, or ``None``.

        ``strict=False`` (the default) keeps the legacy behavior of
        ``list_container`` — a timeout/undecodable listing silently reads as
        "container empty", so an occupied slot can look free.  That is fine
        for a best-effort lookup, but every real caller of this method uses
        it to gate a **write**: "is slot ``pos`` empty, so it is safe to
        ``/CreateContent``/``/SetContentData`` into it?" (``device
        install``/``save``/``push``/``slots restore``).
        Under the lenient default, a silently-truncated listing would make an
        **occupied** slot look empty and let the write through — a positional
        collision, the same failure class backlog #40 fixed in
        ``_lowest_empty_posi``. Every such call site passes ``strict=True``.
        The one caller that intentionally wants the lenient default is
        ``_find_by_pos_retry`` (post-write cid recovery, see below) — it
        already has its own retry loop and doesn't need a listing failure to
        raise.
        """
        for m in self.list_container(container, strict=strict):
            if m.get("posi") == pos:
                return m
        return None

    def get_edit_buffer(self) -> bytes:
        """Return the current edit buffer as a raw ``_sbepgsm`` content blob."""
        for _addr, args in self._rpc("/EditBufferStateGet", [], raw_blobs=True):
            for v in args:
                if isinstance(v, (bytes, bytearray)) and bytes(v[:8]) == _content.MAGIC:
                    return bytes(v)
        raise HelixError("no edit-buffer blob in /getEditBufferState reply")

    def read_edit_buffer(self) -> Any:
        """Decode the current edit buffer into a nested dict (4CC string keys)."""
        return _content.decode_content(self.get_edit_buffer())

    def get_content(self, cid: int) -> bytes:
        """Return preset ``cid``'s stored content blob **without activating it**.

        Sends ``/GetContentData [reqid, cid]`` — the non-activating GET
        counterpart to ``/SetContentData [cid, blob]`` — and returns the raw
        content blob. Unlike :meth:`get_edit_buffer` (which reads only the
        *active* edit buffer, and so requires a preceding :meth:`load_preset`
        that changes the musician's live tone), this reads any preset by cid and
        does NOT change the device's active preset.

        The device returns the **stored** content form
        (``\\xff\\xff\\xff\\xffpgsm``); the ``.sbe`` consumers
        (``device push`` / ``device restore``) accept it unchanged via
        :func:`content.to_content_data`. The large (~14 KB) reply is reassembled
        over multiple socket frames by the same ``_rpc`` reply-reader that
        :meth:`get_edit_buffer` relies on.
        """
        for _addr, args in self._rpc(
                "/GetContentData", [("i", int(cid))], raw_blobs=True):
            for v in args:
                if isinstance(v, (bytes, bytearray)) and bytes(v[:8]) in (
                        _content.MAGIC, _content.CONTENT_DATA_MAGIC):
                    return bytes(v)
        raise HelixError(
            f"no content blob in /GetContentData reply for cid {cid}")

    def product_info(self) -> Dict[str, Any]:
        """Device identity + firmware + storage (``/ProductInfoGet``).

        Read-only and side-effect free (part of the editor's connect
        handshake). The reply is a 4CC-keyed map; the full decoded map is
        returned under ``"raw"`` alongside curated fields:

        ``model`` (device's own name, e.g. ``"stadium"``), ``device_id``
        (numeric — 2490368 = Stadium XL), ``helixgen_model`` (helixgen's
        chassis key when recognized), ``serial``, ``firmware``
        (``"major.minor.patch"``), ``firmware_build``, ``firmware_date``
        (ISO date), ``sd_total_bytes`` / ``sd_available_bytes``.
        """
        import datetime as _dt

        from helixgen.controllers import STADIUM_XL_DEVICE_IDS

        for addr, args in self._rpc("/ProductInfoGet", []):
            if addr != "/getProductInfo" or len(args) < 2 or not isinstance(args[1], dict):
                continue
            raw = _content._keys_to_str(args[1])
            host = raw.get("host")
            if not isinstance(host, dict):
                host = {}
            vers = host.get("vers")
            if not isinstance(vers, dict):
                vers = {}
            device_id = host.get("id__")
            fw = None
            if all(vers.get(k) is not None for k in ("majo", "mino", "patc")):
                fw = f"{vers.get('majo')}.{vers.get('mino')}.{vers.get('patc')}"
            fw_date = None
            if isinstance(vers.get("date"), int):
                fw_date = _dt.datetime.fromtimestamp(
                    vers["date"], _dt.timezone.utc).date().isoformat()
            return {
                "model": host.get("name"),
                "device_id": device_id,
                "helixgen_model": (
                    "stadium_xl" if device_id in STADIUM_XL_DEVICE_IDS else None),
                "serial": host.get("snum"),
                "firmware": fw,
                "firmware_build": vers.get("buld"),
                "firmware_date": fw_date,
                "sd_total_bytes": host.get("sdts"),
                "sd_available_bytes": host.get("sdas"),
                "raw": raw,
            }
        raise HelixError("no /getProductInfo reply from device")

    # -- global settings / properties (reads) ------------------------------
    def _property_blob(self, addr: str, reply: str, key: str) -> bytes:
        """Send ``addr [reqid, key]`` and return the blob from ``reply``."""
        for raddr, args in self._rpc(addr, [("s", key)], raw_blobs=True):
            if raddr == reply:
                for v in args:
                    if isinstance(v, (bytes, bytearray)):
                        return bytes(v)
            if raddr == "/error":
                raise HelixError(
                    f"device rejected {addr} for {key!r}: "
                    f"{args[-1] if args else '?'}")
        raise HelixError(f"no blob in {reply} reply for property {key!r}")

    def get_property(self, key: str) -> _settings.PropertyValue:
        """Read a property's **current** value (``/PropertyValueGet``)."""
        return _settings.decode_value_blob(
            self._property_blob("/PropertyValueGet", "/getPropertyValue", key))

    def get_property_def(self, key: str) -> _settings.PropertyDef:
        """Read a property's definition — name, type, range, enum, default
        (``/PropertyDefWithKeyGet``). Self-describing catalog straight from the
        device."""
        return _settings.decode_property_def(
            self._property_blob(
                "/PropertyDefWithKeyGet", "/keyPropertyDefinition", key))

    # -- writes (proven commands) -----------------------------------------
    def set_property(self, key: str, typ: str, value: Any) -> bool:
        """Write a property value (``/PropertyValueSet [reqid, ctx=0, blob]``).

        ``typ`` is ``'f'``/``'i'`` (from the property's definition). Returns
        ``True`` when the device replies ``/success [reqid, 0]``. Refuses keys
        whose write would sever this control channel (see
        :data:`settings.DANGEROUS_KEYS`).
        """
        _settings.guard_key(key)
        blob = _settings.encode_value_blob(key, typ, value)
        for addr, args in self._rpc("/PropertyValueSet", [("i", 0), ("b", blob)]):
            if addr == "/success":
                return len(args) >= 2 and args[1] == 0
            if addr == "/error":
                raise HelixError(
                    f"device rejected /PropertyValueSet for {key!r}: "
                    f"{args[-1] if args else '?'}")
        return False

    def set_globaleq(self, output: str, band: str, param: str,
                     value: Any) -> bool:
        """Write one **Global EQ** band parameter over the network.

        ``output`` ∈ ``qtr``/``xlr``/``pho``; ``band`` ∈ the seven band names
        (or ``""`` with ``param="level"`` for the output level); ``param`` ∈
        ``enable``/``freq``/``gain``/``q``/``slope``/``level``. Sends the
        variant ``/PropertyValueSet`` blob (see :mod:`helixgen.device.globaleq`).
        Returns ``True`` on ``/success`` code 0.

        Global EQ is **write-only** over the network — the device does not answer
        ``/PropertyValueGet`` for ``dsp.globaleq.*`` keys, so there is no
        read-back.
        """
        blob = _globaleq.encode_value_blob(output, band, param, value)
        for addr, args in self._rpc(
                "/PropertyValueSet", [("i", 0), ("b", blob)]):
            if addr == "/success":
                return len(args) >= 2 and args[1] == 0
            if addr == "/error":
                raise HelixError(
                    f"device rejected Global EQ write "
                    f"{output}.{band}.{param}: {args[-1] if args else '?'}")
        return False

    # -- live edit-buffer control (args decoded from the 2026-07-14 capture) --
    # These mutate the CURRENTLY ACTIVE tone. `_rpc` prepends the request id, so
    # the wire is exactly the decoded `[cmd, …]` shape; these commands reply on
    # the 2001 PUB stream (not a reqid-correlated /status), so `_rpc` returns []
    # and we report best-effort success once the frame is sent.

    def activate_snapshot(self, index: int) -> bool:
        """Recall a snapshot (0-based, 0..7) on the live device.

        ``/activateSnapshot [reqid, index]`` — the index is absolute.
        """
        i = int(index)
        if not 0 <= i <= 7:
            raise ValueError(f"snapshot index {index} out of range 0..7")
        self._rpc("/activateSnapshot", [("i", i)])
        return True

    def set_block_enable(self, path: int, block: int, enable: bool) -> bool:
        """Bypass/enable a block in the live edit buffer.

        ``/BlockEnableSet [reqid, dsp, grid_slot, enable]``. ``path`` = DSP
        index (0/1), ``block`` = the grid slot printed by ``device blocks`` /
        :meth:`edit_buffer_blocks`, sent unchanged (see :func:`_grid_slot`
        for the 2026-07-15 indexing erratum).
        """
        self._rpc("/BlockEnableSet",
                  [("i", int(path)), ("i", _grid_slot(block)),
                   ("i", 1 if enable else 0)])
        return True

    def set_block_model(self, path: int, block: int, model_id: int) -> bool:
        """Set a block's model in the live edit buffer.

        ``/ModelSet [reqid, dsp, grid_slot, sub=0, modelId]``. ``block`` is
        the grid slot printed by ``device blocks`` (sent unchanged; see
        :func:`_grid_slot`). ``model_id`` is the numeric model id (see
        :mod:`helixgen.device.defs`). The device rejects a cross-category swap;
        the app also re-attaches controllers + pushes the new model's param
        defaults (not replayed here).
        """
        self._rpc("/ModelSet",
                  [("i", int(path)), ("i", _grid_slot(block)), ("i", 0),
                   ("i", int(model_id))])
        return True

    @staticmethod
    def _blks_pairs(dsp: Any):
        """Yield ``(grid_slot, block_dict)`` pairs from a flow entry's
        ``blks``.

        On the wire ``blks`` is a FLAT alternating list ``[int, dict, …]``
        whose int is the block's **grid slot** (0..27 — NOT its list
        position; outputs sit at 13/27 with a gap before them). A dict-shaped
        ``blks`` (synthetic/decoded variants) yields its items as-is.
        """
        blks = dsp.get("blks") if isinstance(dsp, dict) else None
        if isinstance(blks, dict):
            yield from blks.items()
            return
        if not isinstance(blks, list):
            return
        i = 0
        while i + 1 < len(blks):
            key, b = blks[i], blks[i + 1]
            if isinstance(key, int) and isinstance(b, dict):
                yield key, b
                i += 2
            else:
                i += 1

    def edit_buffer_blocks(self) -> List[Dict[str, Any]]:
        """List the live edit buffer's modeled blocks as
        ``[{path, block, model_id, model, enabled}]`` — ``block`` is the DSP
        **grid slot** (the int paired with the block in ``blks``), the exact
        coordinate :meth:`set_block_enable` / :meth:`set_block_model` /
        :meth:`set_param` / :meth:`get_param` send on the wire."""
        eb = self.read_edit_buffer()
        flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
        out: List[Dict[str, Any]] = []
        if not isinstance(flow, list):
            return out
        for path, dsp in enumerate(flow):
            for pos, b in self._blks_pairs(dsp):
                mdls = b.get("mdls")
                m0 = mdls[0] if isinstance(mdls, list) and mdls else {}
                mid = m0.get("id__") if isinstance(m0, dict) else None
                if not isinstance(mid, int):
                    continue
                out.append({
                    "path": path, "block": int(pos), "model_id": mid,
                    "model": _defs.model_name_for(mid),
                    "enabled": bool(b.get("enbl", 1))})
        return out

    def edit_buffer_params(self, path: int, block: int) -> Dict[str, Any]:
        """The params of one edit-buffer block, with their numeric pids.

        Returns ``{path, block, model_id, model, enabled, params}`` where
        ``params`` is a pid-sorted list of ``{pid, name, value, type, min,
        max, default}``: the union of the model's defs table (names/types/
        ranges from the vendored modeldefs) and the block's stored ``parm``
        entries (``value`` = the stored ``valu``, in the param's RAW units —
        dB/Hz/enum-int, the same units ``/ParamValueSet`` takes; ``None``
        when the buffer stores no explicit entry for that pid). A pid the
        defs don't know keeps ``name=None``.

        Raises :class:`HelixError` when no modeled block sits at
        ``(path, block)`` — coordinates come from :meth:`edit_buffer_blocks`.
        """
        slot = _grid_slot(block)
        eb = self.read_edit_buffer()
        flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
        dsp = flow[path] if isinstance(flow, list) and 0 <= int(path) < len(flow) else None
        if dsp is None:
            raise HelixError(f"no DSP path {path} in the edit buffer")
        b = next((blk for pos, blk in self._blks_pairs(dsp)
                  if int(pos) == slot), None)
        mdls = b.get("mdls") if isinstance(b, dict) else None
        m0 = mdls[0] if isinstance(mdls, list) and mdls else None
        mid = m0.get("id__") if isinstance(m0, dict) else None
        if not isinstance(mid, int):
            raise HelixError(
                f"no block at path {path} block {slot} — use the coordinates "
                "printed by `device blocks`")
        stored: Dict[int, Any] = {}
        for p in (m0.get("parm") or []):
            if isinstance(p, dict) and isinstance(p.get("pid_"), int):
                stored[p["pid_"]] = p.get("valu")
        rows: Dict[int, Dict[str, Any]] = {}
        for name, meta in _defs.model_params_for(mid).items():
            pid = meta.get("id")
            if not isinstance(pid, int):
                continue
            rows[pid] = {"pid": pid, "name": name,
                         "value": stored.get(pid),
                         "type": meta.get("type"), "min": meta.get("min"),
                         "max": meta.get("max"), "default": meta.get("def")}
        for pid, valu in stored.items():
            rows.setdefault(pid, {"pid": pid, "name": None, "value": valu,
                                  "type": None, "min": None, "max": None,
                                  "default": None})
        return {"path": int(path), "block": slot, "model_id": mid,
                "model": _defs.model_name_for(mid),
                "enabled": bool(b.get("enbl", 1)),
                "params": [rows[k] for k in sorted(rows)]}

    def get_param(self, path: int, block: int, param_id: int) -> Any:
        """Read one edit-buffer param's CURRENT value, in RAW units.

        ``/ParamValueGet [reqid, dsp, grid_slot, 0, paramId]`` →
        ``/getParamValue [reqid, dsp, grid_slot, 0, paramId, value]``
        (live-verified 2026-07-15). A reply without the value field means no
        block/param answers at that coordinate — raises :class:`HelixError`.
        """
        replies = self._rpc(
            "/ParamValueGet",
            [("i", int(path)), ("i", _grid_slot(block)), ("i", 0),
             ("i", int(param_id))])
        for addr, args in replies:
            if addr == "/getParamValue" and len(args) >= 6:
                return args[5]
        raise HelixError(
            f"no value in /getParamValue reply for path {path} block {block} "
            f"pid {param_id} — is there a block with that pid at that "
            "coordinate? (see `device blocks` / `device params`)")

    def active_preset(self) -> Dict[str, Any]:
        """The device's ACTIVE preset: ``{cid, name, posi, slot, ccid}``.

        Reads the live device property ``server.active.preset.id`` (an int
        cid; see :data:`ACTIVE_PRESET_KEY`) and resolves it with the
        read-only ``/GetContentRef``. ``name``/``posi``/``slot``/``ccid``
        are ``None``/empty when the cid doesn't resolve to a content ref.
        Read-only — never touches presets or the edit buffer.
        """
        raw = self.get_property(ACTIVE_PRESET_KEY).value
        try:
            cid = int(raw)
        except (TypeError, ValueError) as e:
            raise HelixError(
                f"unexpected {ACTIVE_PRESET_KEY} value {raw!r} from the "
                "device (malformed property reply?)") from e
        ref = self.get_ref(cid) or {}
        return {"cid": cid, "name": ref.get("name"), "posi": ref.get("posi"),
                "slot": slot_label(ref.get("posi")), "ccid": ref.get("ccid")}

    def load_preset(self, cid: int) -> bool:
        return self._ok(self._rpc("/LoadPresetWithCID", [("i", cid)]))

    def _create_copy(self, container: int, src_cids: Sequence[int], pos: int) -> bool:
        """Add preset(s) by CID into ``container`` at slot ``pos``
        (``/AddContentsToContainer``).

        WARNING: inside a setlist container this creates a **reference**
        (``cctp==1003``, ``rcid`` pointing at the pool preset), *not* a copy.
        Deleting the referenced pool preset while the reference is alive orphans
        it (device error ``-21``). Use :meth:`reference_into_setlist` /
        :meth:`remove_reference` for the model-correct reference lifecycle.
        """
        msgpack = self._load_msgpack()
        return self._ok(self._rpc(
            "/AddContentsToContainer",
            [("i", container), ("b", msgpack.packb(list(src_cids))),
             ("i", pos), ("i", 0), ("i", 0)]))

    def set_attrs(self, cid: int, attrs: Dict[str, Any]) -> bool:
        msgpack = self._load_msgpack()
        return self._ok(self._rpc(
            "/SetContentAttrs", [("i", cid), ("b", msgpack.packb(dict(attrs)))]))

    def rename(self, cid: int, name: str) -> bool:
        return self.set_attrs(cid, {"name": name})

    def _delete(self, container: int, cids: Sequence[int]) -> bool:
        msgpack = self._load_msgpack()
        return self._ok(self._rpc(
            "/RemoveContent", [("i", container), ("b", msgpack.packb(list(cids)))]))

    def reorder_container(self, container: int, moved_cids: Sequence[int],
                          new_pos: int) -> List[Dict[str, Any]]:
        """Move item(s) already inside ``container`` to ``new_pos``.

        ``/ReorderContainerContent [reqid, containerCID, msgpack[movedCIDs],
        newPos]`` (decoded 2026-07-14, e.g. ``[306, -2, [1206], 5]``). Works on
        a setlist's preset **references** (``container`` = the setlist's cid;
        ``moved_cids`` are the references' own cids, NOT the pool preset
        cids), the pool (``-2``) directly, and the setlists root (``-5``)
        itself (reordering the setlists) — the device dispatches on
        ``container`` alone, so the same op serves all three. See
        ``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` §1/§9.

        The device confirms with ``/updateContainerContent`` carrying the
        container's full re-ordered listing. A ``/error`` reply — or a
        ``/status`` whose code field is non-zero (the :meth:`_ok` convention)
        — raises :class:`HelixError` instead of being mistaken for the
        "confirmation landed on the 2001 PUB stream" case. When *some* reply
        arrived but none of it was the ``/updateContainerContent`` confirmation,
        the container is re-listed (non-strict — #40 audit) to recover the
        confirmed order, mirroring the "reply unreliable, re-list to confirm"
        pattern used elsewhere in this client (``_create_from``,
        ``create_setlist``, …); a reply frame having arrived at all means the
        device processed the request (the earlier ``/error``/non-zero-``/status``
        checks would have already raised otherwise), so this re-list is pure
        post-write bookkeeping, not a write gate. A **total** timeout (zero
        reply frames — the RPC-level ``/status``/``/error``/confirmation are
        all equally absent, so the write's actual outcome is genuinely
        unknown) is a *different* case and raises instead of silently reading
        as "it must have worked" (#40 review finding — the non-strict re-list
        alone can't distinguish a confirmed reorder from one the device never
        even received).
        """
        msgpack = self._load_msgpack()
        replies = self._rpc(
            "/ReorderContainerContent",
            [("i", int(container)),
             ("b", msgpack.packb([int(c) for c in moved_cids])),
             ("i", int(new_pos))])
        items: List[Dict[str, Any]] = []
        seen_update = False
        for addr, args in replies:
            if addr == "/error":
                raise HelixError(
                    f"device rejected /ReorderContainerContent for container "
                    f"{container}: {args[-1] if args else '?'}")
            if addr == "/status" and len(args) >= 2 and args[1] != 0:
                raise HelixError(
                    f"device refused to reorder container {container} "
                    f"(status {args[1:]})")
            if addr != "/updateContainerContent":
                continue
            seen_update = True
            for a in args:
                if isinstance(a, list):
                    items.extend(x for x in a if isinstance(x, dict))
                elif isinstance(a, dict):
                    items.append(a)
        if not seen_update:
            if not replies:
                # Total timeout: no /error, no /status, no confirmation — the
                # device may never have received/processed the request at
                # all. Unlike the "some reply, just not the confirmation
                # frame" case below, there is nothing here to indicate the
                # write happened, so raise rather than silently re-listing
                # and returning a possibly-unchanged order as if it were
                # confirmed (#40 review finding).
                raise HelixError(
                    f"no reply to /ReorderContainerContent for container "
                    f"{container} (timeout or connection drop); the reorder's "
                    "outcome is unknown — retry, and check `device list`/"
                    "`device setlist list` before assuming it didn't happen")
            # Some reply arrived (so the device did process the request — the
            # /error / non-zero-/status checks above would have already
            # raised otherwise) but none of it was the /updateContainerContent
            # confirmation. Deliberately non-strict re-list (#40 audit): this
            # is pure post-write bookkeeping to recover the confirmed order
            # for the return value, same as the post-write reference listing
            # #39 left lenient in setlist_sync.py.
            items = self.list_container(container)
        items.sort(key=lambda m: m.get("posi", 1 << 30))
        return items

    def set_param(self, path: int, block: int, param_id: int, value: float) -> bool:
        """Set a param in the edit buffer:
        ``/ParamValueSet [_, path, grid_slot, 0, paramId, value, -1]``.

        ``block`` is the grid slot printed by ``device blocks`` (sent
        unchanged; see :func:`_grid_slot` for the 2026-07-15 indexing
        erratum); ``value`` is in the param's RAW units (e.g. dB for the
        output block's ``gain`` pid 2 — HW-proof 2026-07-15: slot 13 gain
        6.0→3.0→6.0, each write acked ``/status 0`` and read back via
        :meth:`get_param`), not normalized.
        """
        return self._ok(self._rpc(
            "/ParamValueSet",
            [("i", path), ("i", _grid_slot(block)), ("i", 0), ("i", param_id),
             ("f", float(value)), ("i", -1)]))

    def _find_by_pos_retry(self, container: int, pos: int,
                           tries: int = 4, delay: float = 0.25
                           ) -> Optional[Dict[str, Any]]:
        """find_by_pos with a few retries — the device may re-index the
        container slightly after a write lands.

        Deliberately calls ``find_by_pos`` with its lenient default
        (``strict=False``, #40 audit): this runs *after* the write it's
        recovering a cid for (``_create_from``'s ``/CreateContent`` already
        succeeded), so a transient listing failure here means "not yet
        re-indexed, keep polling" — exactly like a clean listing that doesn't
        have the entry yet — not "collision risk" (there's nothing left to
        gate)."""
        for i in range(tries):
            m = self.find_by_pos(container, pos)
            if m is not None:
                return m
            if i < tries - 1:
                time.sleep(delay)
        return None

    # convenience alias for the create-by-copy flow
    def _create_from(self, src_cid: int, container: int, pos: int) -> Optional[int]:
        """Copy ``src_cid`` into ``container`` at ``pos``; return the new CID."""
        if not self._create_copy(container, [src_cid], pos):
            return None
        m = self._find_by_pos_retry(container, pos)
        return m.get("cid_") if m else None

    # -- write current edit buffer to a new preset slot --------------------
    def _create_content_status(self, container: int, pos: int, name: str,
                               ctype: int = 2) -> tuple:
        """Send ``/CreateContent`` and return ``(allocated_cid, code)``.

        ``/CreateContent`` replies ``/status [reqid, newCid, code]`` — the new
        CID is in the **second** field, the ok-code in the **third** (unlike
        other writes). ``code == 0`` is OK.

        This returns **both** fields (``(None, None)`` if no ``/status`` frame
        came back) — but a non-zero ``code`` is NOT by itself a failure. Live
        A/B against a Stadium XL (fw 1.3.2/1340, 2026-07-19) established that
        field 3 tracks the device's **edit-buffer dirty flag** (``hist`` in
        ``/EditBufferStateGet``), not an error: with an edited active preset
        every create answers ``1`` while creating the content at the exact
        requested ``posi``; with a freshly loaded/saved preset the same code
        path answers ``0``. Callers must therefore decide by
        :meth:`_confirm_created` (re-list), never by this code alone — see
        :meth:`_create_content_checked`.

        Only the preset **pool** (``-2``) accepts /CreateContent; setlists reject
        it (device error ``-47``). Guard against the misuse up front.
        """
        if int(container) != int(Container.POOL):
            raise HelixError(
                "setlists reject CreateContent -> -47; use reference_into_setlist")
        msgpack = self._load_msgpack()
        for addr, args in self._rpc(
                "/CreateContent",
                [("i", container), ("i", pos), ("i", ctype),
                 ("b", msgpack.packb({"name": name}))]):
            if addr == "/status" and len(args) >= 3:
                return args[1], args[2]
        return None, None

    def _confirm_created(self, container: int, name: str,
                         pos: int) -> Tuple[Optional[int], bool, bool]:
        """Re-list ``container`` looking for the entry we just created (matched
        by ``name`` **and** ``posi == pos``).

        Returns ``(cid, listed_cleanly, saw_cidless_match)``: the cid when the
        entry was found (``None`` otherwise), whether the **final** attempt got
        a clean listing back, and whether some attempt matched ``(name, pos)``
        but carried no ``cid_``. All three matter — a ``(None, False, _)``
        means "the last read of the container failed", which is NOT evidence
        the create failed, and ``saw_cidless_match`` means the content WAS
        observed but its cid never resolved, so the error must not tell the
        user the listing never showed it (see :meth:`_create_status_error`).

        This is the authority on whether a ``/CreateContent`` landed — the
        status code is not (see :meth:`_create_content_status`). The match is
        the same one :meth:`_delete_created_stub` uses: the callers that create
        into a slot they checked was empty (``device save``/``push``/
        ``install``) can read a same-name entry now sitting at ``pos`` as
        unambiguously ours. ``slots restore --force`` deliberately skips that
        precheck, so there the match may be a pre-existing occupant — which is
        the slot the caller asked to overwrite anyway. The returned cid is the
        **listed** one; the create-reply cid stays documented-unreliable (see
        ``_pool_cid_by_name``).

        The listing is **strict** (#40): a timeout or truncated reply must not
        decode as "container empty" and count as a clean "not there". A failed
        listing is retried, never raised — the create already happened, so
        there is no write left to gate.

        Retries are bounded (``create_confirm_tries`` / ``create_confirm_delay``)
        because the container index is known to lag a just-completed write. The
        loop runs under :meth:`mutating` for the same reason ``list_irs`` does:
        the index only propagates promptly to a client holding a 2001
        subscription. Nesting is cheap, so the callers that already hold one
        pay nothing.
        """
        listed_cleanly = False
        saw_cidless = False
        with self.mutating():
            for i in range(self.create_confirm_tries):
                try:
                    listing = self.list_container(container, strict=True)
                except HelixError:
                    listing = None
                # Deliberately NOT sticky: only the LAST attempt's outcome may
                # license the confident "it really did not land" diagnosis. A
                # clean-but-empty read early in the loop is the expected shape
                # of a lagging container index, so letting it latch True would
                # let one early read plus a run of dropped listings assert
                # absence for content that is on the device (the #38 failure
                # mode, just moved). See :meth:`_create_status_error`.
                listed_cleanly = listing is not None
                if listing is not None:
                    match = next(
                        (m for m in listing
                         if m.get("name") == name and m.get("posi") == pos), None)
                    if match is not None:
                        if match.get("cid_") is not None:
                            return match.get("cid_"), True, saw_cidless
                        saw_cidless = True
                if i < self.create_confirm_tries - 1:
                    time.sleep(self.create_confirm_delay)
        return None, listed_cleanly, saw_cidless

    def _create_content_checked(self, container: int, pos: int, name: str,
                                ctype: int = 2) -> Optional[int]:
        """``/CreateContent`` + **verify by re-list**; return the created cid.

        The status code alone can't tell success from failure (it reports the
        edit-buffer dirty flag, #38), so a non-zero code is resolved by
        :meth:`_confirm_created`:

        * content present → SUCCESS, returning the **re-listed** cid;
        * content genuinely absent after the bounded retries → raise
          :meth:`_create_status_error`, which deletes **nothing** (#38):
          cleanup belongs to the callers that created a stub and then failed
          to write into it, never to this path.

        ``code == 0`` keeps the historic fast path — the callers already
        re-list by name to recover the real cid — and returns the reply cid.

        **No ``/status`` frame at all** (``code is None``) is resolved the same
        way as a non-zero code, not treated as failure: on the documented-flaky
        Stadium stack a dropped reply says nothing about whether the create
        landed, and returning ``None`` there made the callers report a silent
        failure for content that is really on the device (#38).
        """
        cid, code = self._create_content_status(container, pos, name, ctype)
        if code != 0:
            confirmed, listed_cleanly, saw_cidless = self._confirm_created(
                container, name, pos)
            if confirmed is not None:
                return confirmed
            raise self._create_status_error(
                name, pos, cid, code,
                listed_cleanly=listed_cleanly, saw_cidless=saw_cidless)
        return cid

    def _create_content(self, container: int, pos: int, name: str,
                        ctype: int = 2) -> Optional[int]:
        """Create an empty preset entry (`/CreateContent`); return its new CID,
        or ``None`` when the create genuinely didn't land.

        Low-level escape hatch (exposed as ``_raw.create_content``) that keeps
        the historic ``Optional[int]`` contract: it verifies by re-list like
        :meth:`_create_content_checked` but reports a genuine failure as
        ``None`` rather than raising.
        """
        cid, code = self._create_content_status(container, pos, name, ctype)
        if code != 0:
            return self._confirm_created(container, name, pos)[0]
        return cid

    def _delete_created_stub(self, container: int, name: str,
                             pos: int) -> Optional[int]:
        """Verify-before-delete cleanup for a just-attempted /CreateContent.

        The create-reply cid is unreliable (see ``_pool_cid_by_name``), so to
        remove a stub we just created we **re-list** ``container`` and match the
        entry by ``name`` **and** ``posi == pos`` (the slot was empty before our
        create, so a same-name entry now sitting at ``pos`` is unambiguously
        ours). We never blind-delete the create-reply cid, which could be stale
        or point at an unrelated preset. Returns the cid actually deleted, or
        ``None`` if nothing matched / the delete failed.

        Only for the genuine create-then-write-failed case — the confirmed-create
        path must never reach it (#38; see :meth:`_create_status_error`).

        A no-op is **reported**, never swallowed: the container index is known to
        lag, so "nothing matched" may well mean the stub is really there and just
        not listed yet. The old silence is why the orphan accounting looked
        clean while presets were being left behind.
        """
        def _no_op(why: str) -> None:
            logger.warning(
                "cleanup of the just-created entry %r at slot %d in container "
                "%s did not happen (%s) — the container index lags writes, so a "
                "stub may be left behind; re-list to check", name, pos,
                container, why)

        try:
            # strict (#40): a dropped or truncated reply must not decode as an
            # empty container and report as "no entry matched" — that reads as
            # "nothing to clean up" when the stub may well be sitting there
            listing = self.list_container(container, strict=True)
        except HelixError as exc:
            _no_op(f"the listing failed: {exc}")
            return None
        match = next(
            (m for m in listing
             if m.get("name") == name and m.get("posi") == pos), None)
        if match is None:
            _no_op("no entry matched (stale listing?)")
            return None
        cid = match.get("cid_")
        if cid is None:
            _no_op("the matching entry carried no cid")
            return None
        try:
            if self._delete(container, [cid]):
                return cid
            _no_op(f"the device refused to delete cid {cid}")
        except HelixError as exc:
            _no_op(f"the delete failed: {exc}")
        return None

    def _save_preset_with_cid(self, cid: int, block_count: int = 0) -> bool:
        """Persist the current edit buffer into an existing CID (`/SavePresetWithCID`)."""
        return self._ok(self._rpc(
            "/SavePresetWithCID", [("i", cid), ("i", 0), ("i", block_count)]))

    def _save_edit_buffer_to(self, container: int, pos: int, name: str) -> Optional[int]:
        """Save the current edit buffer as a new preset at ``pos``; return its CID.

        Mirrors the editor's "Save Preset As -> Save As New": CreateContent then
        SavePresetWithCID.

        The whole create → confirm → save → cleanup sequence runs under
        :meth:`mutating`: the confirming re-list is only prompt for a client
        holding a 2001 subscription, and ``device save``'s CLI path doesn't
        open one of its own (unlike ``device install``). Nesting is cheap for
        the callers that do.
        """
        with self.mutating():
            cid = self._create_content_checked(container, pos, name)
            if cid is None:
                return None
            if not self._save_preset_with_cid(cid):
                # don't leave an orphaned empty entry occupying the slot; delete
                # the entry we just created by (name, pos), not the unreliable
                # reply cid
                self._delete_created_stub(container, name, pos)
                return None
            return cid

    # -- push arbitrary content to a preset (restore / clone / author) ------
    def _set_content_data(self, cid: int, blob: bytes) -> bool:
        """Write preset content to an existing CID (`/SetContentData`).

        ``blob`` may be either an edit-buffer (`_sbepgsm`) blob or an already
        stored-content blob; it is converted as needed.
        """
        blob = _content.to_content_data(bytes(blob))
        return self._ok(self._rpc("/SetContentData", [("i", cid), ("b", blob)]))

    def _push_to_slot(self, container: int, pos: int, name: str,
                     blob: bytes, *,
                     prechecked_empty: bool = False) -> Optional[int]:
        """Create a new preset at ``pos`` and write ``blob`` into it (restore a
        backup / clone / install authored content).  Returns the new CID.

        Runs under :meth:`mutating` for the whole create → confirm → write →
        cleanup sequence — the confirming re-list needs a subscription to be
        prompt (see :meth:`_confirm_created`). Nesting is cheap for callers
        that already hold one (``install_into_pool``, ``device install``,
        ``slots restore``, ``setlist sync``).

        ``prechecked_empty`` says the caller confirmed ``pos`` was empty before
        calling, which is what makes a same-name entry at ``pos`` afterwards
        unambiguously **ours** and therefore safe to clean up on a failed write.
        It **defaults to False** — deleting is the destructive answer, so a
        caller has to opt in deliberately rather than inherit permission.
        ``slots restore --force`` skips the precheck (#25), so there the entry
        may be a **pre-existing occupant**: neither the create-reply cid nor
        :meth:`_confirm_created`'s match can distinguish it from a fresh stub,
        and deleting it would destroy content we never created — the very thing
        #38 was about. Left False, the write failure is reported without any
        cleanup."""
        with self.mutating():
            cid = self._create_content_checked(container, pos, name)
            if cid is None:
                return None
            if not self._set_content_data(cid, blob):
                if prechecked_empty:
                    # cleanup: delete the entry we just created by (name, pos) —
                    # the create-reply cid is unreliable, so never blind-delete it
                    self._delete_created_stub(container, name, pos)
                else:
                    logger.warning(
                        "writing content to %r at slot %d in container %s "
                        "failed, and the slot was not checked empty beforehand "
                        "(--force), so the entry there may predate this call — "
                        "leaving it alone rather than risk deleting a preset we "
                        "did not create; re-list to check", name, pos, container)
                return None
            return cid

    def _create_status_error(self, name: str, pos: int,
                             reply_cid: Optional[int], code: Optional[int], *,
                             what: str = "pool",
                             verify_cmd: str = "helixgen device list",
                             listed_cleanly: bool = True,
                             saw_cidless: bool = False,
                             entry_is_empty_stub: bool = True,
                             ) -> "HelixError":
        """Build the error for a ``/CreateContent`` that could not be confirmed.

        Reached **only** after :meth:`_confirm_created` exhausted its bounded
        retries without finding the content, i.e. the genuine not-created case.
        It therefore **must not delete anything** (#38): an entry that would
        show up in a listing taken after that point is the create landing late
        against a lagging container index, and removing it is exactly the
        data loss this path used to cause. Cleanup stays with the callers that
        genuinely created something and then failed to write into it
        (:meth:`_push_to_slot` / :meth:`_save_edit_buffer_to`).

        ``what``/``verify_cmd`` label the container kind for the message. The
        preset paths (via :meth:`_create_content_checked`) take the defaults;
        only ``create_setlist`` overrides them, since it reuses this error for
        the setlists root (#66 residual).

        ``listed_cleanly`` is :meth:`_confirm_created`'s second return value:
        ``False`` means the **final** confirming listing failed, so the newest
        answer we have is "unreadable". The message must say "could not verify"
        there rather than assert the content is absent — on the documented-flaky
        Stadium stack a run of dropped listings would otherwise produce a
        confident, wrong diagnosis for a write that landed. It is deliberately
        the last attempt and not "any attempt": the container index lags a
        just-completed write, so an early clean-but-empty read is the normal
        shape of a create still propagating, and only a clean read at the end
        of the retry budget is evidence of absence.

        ``saw_cidless`` means a listing DID show ``(name, pos)`` but without a
        cid, so the entry is on the device and only its cid is unresolved.
        Telling the user the listing "never showed it" there would send them to
        re-create an entry that is already present.

        ``entry_is_empty_stub`` says what such a surviving entry actually
        CONTAINS, which differs by caller and changes the advice materially.
        This error is raised from :meth:`_create_content_checked`, i.e. before
        ``_set_content_data``/``_save_preset_with_cid`` has run — so on the
        preset paths the entry is an **empty stub** and must be deleted before
        retrying, even though ``device list`` will happily show its name.
        Saying "the content is on the device, don't retry" there would leave an
        empty preset squatting the slot and the user believing the write
        succeeded. ``create_setlist`` passes ``False``: an empty setlist
        container IS its deliverable, so a surviving entry needs no cleanup.

        ``code`` is ``None`` when no ``/status`` frame came back at all; the
        message says so rather than printing "status code None".
        """
        if entry_is_empty_stub:
            survivor = (f"an EMPTY {what} entry named {name!r} is most likely "
                        f"sitting at slot {pos} with no content in it: the "
                        f"create landed but the content write never ran. "
                        f"`{verify_cmd}` WILL show the name — that is the stub, "
                        f"not a saved tone. Delete it before retrying, or the "
                        f"retry duplicates the name")
        else:
            survivor = (f"the {what} appears to be on the device, so do NOT "
                        f"retry blindly: check with `{verify_cmd}` first, or a "
                        f"retry may duplicate it")
        reported = (f"returned status code {code}" if code is not None
                    else "sent no /status reply")
        if saw_cidless:
            return HelixError(
                f"/CreateContent for {name!r} at slot {pos} {reported}, and the "
                f"{what} listing DID show an entry at that slot but never "
                f"reported a cid for it "
                f"({self.create_confirm_tries} attempts) — {survivor}. A code "
                f"of 1 on its own is not an error — it reports that the active "
                f"preset has unsaved edits (backlog #38).")
        if not listed_cleanly:
            return HelixError(
                f"/CreateContent for {name!r} at slot {pos} {reported}, "
                f"and the {what} listing could not be read on the final attempt "
                f"({self.create_confirm_tries} attempts) — so "
                f"whether the entry was created is UNKNOWN. Do not assume it "
                f"failed: check with `{verify_cmd}` before retrying, or a retry "
                f"may duplicate an entry that is already there"
                + (f" (and anything `{verify_cmd}` does show at slot {pos} is "
                   f"an EMPTY stub, not a saved tone — delete it first)"
                   if entry_is_empty_stub else "")
                + ". A code of 1 on its own is not an error — it reports that "
                "the active preset has unsaved edits (backlog #38).")
        if reply_cid is not None:
            detail = (f"the device reported new cid {reply_cid} but the {what} "
                      f"listing never showed it — verify with `{verify_cmd}` "
                      f"and delete it manually if it does turn up")
        else:
            detail = (f"no cid was reported and the {what} listing never showed "
                      f"it — verify with `{verify_cmd}`")
        return HelixError(
            f"/CreateContent for {name!r} at slot {pos} {reported}, and the "
            f"{what} listing was read cleanly but never showed the entry "
            f"({self.create_confirm_tries} attempts) — so the create really "
            f"did not land; {detail}. A code of 1 on its own is not an error — "
            f"it reports that the active preset has unsaved edits, not the "
            f"cause of this failure (backlog #38).")

    # -- mutating() context: activate prompt propagation for writes --------
    @contextlib.contextmanager
    def mutating(self):
        """Hold a 2001 subscription open for the duration of a write batch.

        The device only propagates external content changes promptly while a
        client is subscribed to its 2001 change stream (that activates its
        watched-index monitor; without it, writes land against a lagging
        container index). This is the ``push_ir`` pattern, generalized: open a
        :class:`~helixgen.device.subscribe.HelixSubscriber` on 2001, settle
        ~``mutate_settle`` s, yield, and close it on exit.

        Nesting is safe — only the outermost ``with`` opens/closes the
        subscriber; inner ops that also call ``mutating()`` just bump a depth
        counter. This lets a caller wrap a whole batch
        (``with client.mutating(): ...``) while individual model-correct ops
        still guarantee a subscription when called on their own.
        """
        if self._mutating > 0:
            self._mutating += 1
            try:
                yield self
            finally:
                self._mutating -= 1
            return
        from .subscribe import HelixSubscriber
        # The 2001 subscriber is a prompt-propagation optimization, not
        # correctness-critical: if the flaky device refuses the subscribe socket,
        # log and carry on rather than aborting the whole write batch. A dropped
        # MAIN socket mid-mutation still surfaces via _rpc's retry/raise.
        sub = None
        try:
            sub = HelixSubscriber(self.ip, ports=(2001,))
            sub.connect()
            if self.mutate_settle:
                time.sleep(self.mutate_settle)
        except Exception as exc:  # noqa: BLE001 - optimization, never fatal
            logger.warning(
                "could not open 2001 change-stream subscriber (%s); continuing "
                "without prompt propagation", exc)
            sub = None
        self._mutating += 1
        try:
            yield self
        finally:
            self._mutating -= 1
            if sub is not None:
                try:
                    sub.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("error closing 2001 subscriber (%s)", exc)

    # -- model-correct write surface (pool + reference lifecycle) ----------
    def _lowest_empty_posi(self, container: int) -> int:
        """Lowest ``posi`` not currently occupied in ``container``.

        Feeds ``install_into_pool``/``create_setlist`` whenever the caller
        doesn't pin an explicit ``pos`` — i.e. it picks the slot that the very
        next ``/CreateContent`` will target. Listed **strictly** (backlog
        #40): with the legacy non-strict default, a timed-out/undecodable
        listing reads as "container empty" and this would return posi 0 even
        when the container is full — the subsequent ``/CreateContent`` then
        targets an already-occupied slot, a *positional* collision distinct
        from the *name* duplication #39 fixed (that one made an existing
        setlist look absent by name; this one makes an existing occupant at a
        given posi look absent by position). A strict failure here raises
        ``HelixError`` before any create is attempted, so both callers abort
        cleanly instead of writing into a real occupant.

        What the device does on an actual posi collision is unconfirmed: the
        protocol's non-zero `/status` error taxonomy is uncatalogued (see
        ``docs/helix-protocol.md`` §9 "Known-unknowns / TODO"), and the one
        non-zero code caught live so far — #38's ``code == 1``
        (``docs/superpowers/specs/2026-07-15-createcontent-status1-findings.md``)
        — turned out **not** to be an error at all: field 3 is the edit-buffer
        dirty flag (root-caused 2026-07-19), which is why the reproduction
        attempt against a freshly loaded preset returned ``code == 0`` 5/5.
        So no non-zero code has ever been observed to *mean* a rejection, and
        nothing here assumes an occupied-slot write would surface as one.
        Either way, an actually-occupied posi is exactly what this strict
        listing now prevents from being chosen in the first place.
        """
        used = {m.get("posi") for m in self.list_container(container, strict=True)}
        p = 0
        while p in used:
            p += 1
        return p

    def _pool_cid_by_name(self, name: str, pos: Optional[int] = None) -> Optional[int]:
        """Recover a pool preset's real ``cid_`` by ``name`` (create replies
        return an unreliable cid). If ``pos`` is given and several presets share
        the name, prefer the one at that slot."""
        matches = [m for m in self.list_presets(Container.POOL)
                   if m.get("name") == name]
        if not matches:
            return None
        if pos is not None:
            for m in matches:
                if m.get("posi") == pos:
                    return m.get("cid_")
        return matches[0].get("cid_")

    def install_into_pool(self, blob: bytes, name: str, *,
                          template_blob: Optional[bytes] = None,
                          pos: Optional[int] = None) -> Optional[int]:
        """Install preset ``blob`` as a new preset in the POOL (``-2``).

        Creates an empty preset in the pool then SetContentData's ``blob`` into
        it. If ``pos`` is ``None`` the lowest empty pool slot is chosen. The cid
        in the create reply is unreliable, so the pool is re-listed **by name**
        to recover the true cid, which is returned (``None`` on failure).

        ``template_blob`` is accepted for call-site symmetry with the authoring
        flow but is unused here — ``blob`` is already the full content to write.
        """
        with self.mutating():
            prechecked = pos is None
            if pos is None:
                pos = self._lowest_empty_posi(Container.POOL)
            # only the self-chosen posi above is known-empty; a caller-supplied
            # ``pos`` was never checked here, so it must not authorize the
            # failed-write cleanup to delete by (name, pos) — that entry could
            # be a pre-existing occupant (#38).
            cid = self._push_to_slot(Container.POOL, pos, name, blob,
                                     prechecked_empty=prechecked)
            if cid is None:
                return None
            real = self._pool_cid_by_name(name, pos=pos)
            return real if real is not None else cid

    def reference_into_setlist(self, setlist_cid: int, pool_cid: int,
                               pos: int) -> Optional[int]:
        """Add a **reference** to pool preset ``pool_cid`` into setlist
        ``setlist_cid`` at slot ``pos`` (a ``cctp==1003`` entry whose ``rcid``
        points at the pool preset). The reference's own cid is recovered by
        re-listing the setlist (match ``rcid``/``posi``) and returned; ``None``
        on failure."""
        with self.mutating():
            if not self._create_copy(setlist_cid, [pool_cid], pos):
                return None
            refs = [m for m in self.list_container(setlist_cid)
                    if m.get("cctp") == Cctp.REFERENCE]
            # Prefer an exact (rcid, posi) match, then rcid, then posi.
            for m in refs:
                if m.get("rcid") == pool_cid and m.get("posi") == pos:
                    return m.get("cid_")
            for m in refs:
                if m.get("rcid") == pool_cid:
                    return m.get("cid_")
            for m in refs:
                if m.get("posi") == pos:
                    return m.get("cid_")
            return None

    def remove_reference(self, setlist_cid: int, ref_cid: int) -> bool:
        """Remove a setlist **reference** (``RemoveContent`` of ``ref_cid`` from
        ``setlist_cid``). ``ref_cid`` MUST be a reference cid — never a pool
        preset cid; removing the reference leaves the pool preset untouched."""
        with self.mutating():
            return self._delete(setlist_cid, [ref_cid])

    def delete_irs(self, cids: Sequence[int]) -> bool:
        """Delete user IRs by cid from the device (``/RemoveContent`` on the
        USER_IRS container ``-11`` — the same command preset delete uses).
        Callers resolve name/hash → cid first (``maintenance.resolve_device_ir``)."""
        with self.mutating():
            return self._delete(Container.USER_IRS, list(cids))

    def create_setlist(self, name: str, pos: Optional[int] = None) -> Optional[int]:
        """Create a new (empty) setlist on the device; return its cid.

        Sends ``/CreateContent`` under the setlists root ``-5`` with
        ``ctype=1003`` (live-verified 2026-07-14 — closes backlog #8). Like
        preset creation, the cid in the create reply can be unreliable, so the
        root is re-listed by name to recover the real cid.

        A **non-zero status code is not a failure** (#38, root-caused
        2026-07-19): field 3 of the ``/status`` reply carries the device's
        edit-buffer dirty flag, not an error code. It is resolved the same way
        the push/save paths resolve it — :meth:`_confirm_created` re-lists the
        setlists root, and only a setlist that is **genuinely absent** after
        the bounded retries raises. That path deletes **nothing**: the old
        self-cleaning cleanup is exactly what destroyed creates that had
        landed.
        """
        with self.mutating():
            if pos is None:
                pos = self._lowest_empty_posi(Container.SETLISTS_ROOT)
            msgpack = self._load_msgpack()
            reply_cid: Optional[int] = None
            code: Optional[int] = None
            for addr, args in self._rpc(
                    "/CreateContent",
                    [("i", int(Container.SETLISTS_ROOT)), ("i", int(pos)),
                     ("i", CTYPE_SETLIST), ("b", msgpack.packb({"name": name}))]):
                if addr == "/status" and len(args) >= 3:
                    reply_cid, code = args[1], args[2]
            created = reply_cid
            if code != 0:
                # #38: a non-zero code only reports the edit-buffer dirty flag,
                # so confirm by re-listing the setlists root before calling it
                # a failure — the container is usually right there. Only a
                # genuinely absent container raises, and that path deletes
                # nothing, matching _push_to_slot / _save_edit_buffer_to.
                # ``code is None`` (no /status frame at all) routes here too,
                # matching _create_content_checked: on the flaky Stadium stack
                # a dropped reply says nothing about whether the create landed,
                # and reporting failure for a setlist that IS on the device is
                # the same #38 false negative — worse here, because
                # `setlist duplicate`'s auto-create aborts on it without
                # cleanup and leaks the setlist it claims not to have made.
                created, listed_cleanly, saw_cidless = self._confirm_created(
                    int(Container.SETLISTS_ROOT), name, int(pos))
                if created is None:
                    raise self._create_status_error(
                        name, int(pos), reply_cid,
                        code, what="setlist",
                        verify_cmd="helixgen device setlists",
                        listed_cleanly=listed_cleanly, saw_cidless=saw_cidless,
                        # an empty setlist container is the deliverable here,
                        # unlike the preset paths' contentless stub
                        entry_is_empty_stub=False)
            if created is None:
                return None
            # The create-reply cid is unreliable (same as preset creation), so
            # the root is re-listed by name — with retries, since listings lag
            # briefly after a write. The reply cid is only a last-resort
            # fallback. This lookup is deliberately non-strict: we already KNOW
            # the device just accepted the create (status 0 above), so a
            # transient listing failure here means "not yet visible, keep
            # polling" — the same as a clean listing that doesn't have it yet
            # — not "duplicate risk" (there's nothing left to auto-create).
            for i in range(4):
                real = self.resolve_setlist_cid(name, strict=False)
                if real is not None:
                    return real
                if i < 3:
                    time.sleep(0.25)
            logger.warning(
                "setlist %r created but not yet listed under -5; falling back "
                "to the create-reply cid %s (unreliable — re-list to confirm)",
                name, created)
            return created

    def delete_setlist(self, cid: int) -> bool:
        """Delete a setlist container (``/RemoveContent`` from the root ``-5``).

        The setlist's references die with it; the pool presets they pointed at
        are untouched (live-verified — never-orphan holds). ``cid`` MUST be a
        setlist cid, never a pool preset cid.
        """
        with self.mutating():
            return self._delete(Container.SETLISTS_ROOT, [int(cid)])

    def duplicate_setlist_refs(self, src_cid: int, dst_cid: int) -> int:
        """Copy setlist ``src_cid``'s references into ``dst_cid`` (which must
        currently hold none), preserving order. Returns the number copied.

        References are copies of *pointers* (``rcid``) — the pool presets are
        shared, not duplicated. Raises :class:`HelixError` if the destination
        already has references (a partial merge is a different operation) or a
        copy fails partway.
        """
        with self.mutating():
            dst_refs = [m for m in self.list_container(dst_cid, strict=True)
                        if m.get("cctp") == Cctp.REFERENCE]
            if dst_refs:
                raise HelixError(
                    f"destination setlist cid {dst_cid} is not empty "
                    f"({len(dst_refs)} references); duplicate needs an empty "
                    "target")
            src_refs = [m for m in self.list_container(src_cid, strict=True)
                        if m.get("cctp") == Cctp.REFERENCE]
            src_refs.sort(key=lambda m: m.get("posi", 0))
            copied = 0
            for i, r in enumerate(src_refs):
                if self.reference_into_setlist(dst_cid, r.get("rcid"), i) is None:
                    raise HelixError(
                        f"failed to copy reference {r.get('name')!r} "
                        f"(rcid {r.get('rcid')}) at position {i}; destination "
                        "setlist is partially filled — delete it and retry")
                copied += 1
            return copied

    def mirror_setlist(self, setlist_cid: int,
                       ordered_pool_cids: Sequence[int]) -> Dict[str, list]:
        """Reconcile ``setlist_cid``'s references to exactly ``ordered_pool_cids``
        in order.

        Lists the current references (``cctp==1003``, with ``rcid`` + ``posi``),
        removes every reference whose ``(rcid, posi)`` isn't in the desired
        sequence, then adds the desired references at their target positions.
        Pool presets are NEVER deleted (no orphaning). Returns
        ``{"added": [ref_cid, ...], "removed": [ref_cid, ...]}``.

        STRICT listing (#39 audit): this is the add/remove reconciliation gate
        — a truncated/timed-out read would make a reference that's actually
        present look absent, and the "add" pass below would then create a
        **second** reference to the same pool preset at that position (a
        duplicate, the same failure class #39 fixed for setlist names).
        """
        with self.mutating():
            current = [m for m in self.list_container(setlist_cid, strict=True)
                       if m.get("cctp") == Cctp.REFERENCE]
            desired = list(enumerate(ordered_pool_cids))  # (pos, pool_cid)
            desired_set = {(pos, pool_cid) for pos, pool_cid in desired}
            keep: set = set()
            removed: list = []
            for m in current:
                key = (m.get("posi"), m.get("rcid"))
                if key in desired_set:
                    keep.add(key)
                else:
                    if self.remove_reference(setlist_cid, m.get("cid_")):
                        removed.append(m.get("cid_"))
            added: list = []
            for pos, pool_cid in desired:
                if (pos, pool_cid) in keep:
                    continue
                ref = self.reference_into_setlist(setlist_cid, pool_cid, pos)
                if ref is not None:
                    added.append(ref)
            return {"added": added, "removed": removed}
