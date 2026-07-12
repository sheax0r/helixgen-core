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
from typing import Any, Dict, List, Optional, Sequence

from .osc import osc_encode, parse_osc_message
from . import content as _content

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

_SLOT_LETTERS = "ABCD"


class HelixError(Exception):
    """A device RPC failed or the network client could not be used."""


def slot_label(posi: Optional[int]) -> str:
    """Device ``posi`` -> Helix bank/slot label, e.g. 0 -> '1A', 5 -> '2B'."""
    if posi is None:
        return ""
    return f"{posi // 4 + 1}{_SLOT_LETTERS[posi % 4]}"


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
                     blob: bytes) -> Optional[int]:
        return self._c._push_to_slot(container, pos, name, blob)


class HelixClient:
    def __init__(self, ip: str = "192.168.4.84", port: int = 2002,
                 *, connect_settle: float = 0.6, rpc_timeout: float = 2.0,
                 reconnect_tries: int = 3, reconnect_backoff: float = 0.5):
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
    def list_container(self, cid: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for _addr, args in self._rpc("/GetContainerContents", [("i", cid)]):
            for a in args:
                if isinstance(a, list):
                    items.extend(x for x in a if isinstance(x, dict))
                elif isinstance(a, dict):
                    items.append(a)
        return items

    def list_presets(self, container: int = USER) -> List[Dict[str, Any]]:
        presets = [m for m in self.list_container(container)
                   if m.get("cctp") == CT_PRESET]
        presets.sort(key=lambda m: m.get("posi", 1 << 30))
        return presets

    def list_setlists(self) -> List[Dict[str, Any]]:
        """Return the device's real user setlists.

        A setlist is an item of type ``cctp==1001`` living inside the setlists
        root container ``-5``. (The old implementation swept a hard-coded
        ``(FACTORY, USER, THROWAWAY)`` list of container ids — but ``-5`` is the
        *root*, not a setlist, so that was wrong.) Each returned dict carries at
        least ``cid_``, ``name``, ``posi``.
        """
        out = []
        for m in self.list_container(Container.SETLISTS_ROOT):
            if m.get("cctp") == Cctp.SETLIST:
                out.append(dict(m))
        out.sort(key=lambda m: m.get("posi", 1 << 30))
        return out

    # spec uses both names; ``list_setlists`` is canonical.
    def list_user_setlists(self) -> List[Dict[str, Any]]:
        """Alias of :meth:`list_setlists` — enumerate the real user setlists."""
        return self.list_setlists()

    def resolve_setlist_cid(self, name: str) -> Optional[int]:
        """Case-insensitively match a user setlist by ``name`` and return its
        ``cid_`` (the positive setlist container id), or ``None`` if no setlist
        with that name exists on the device."""
        want = name.strip().casefold()
        for m in self.list_setlists():
            if str(m.get("name", "")).strip().casefold() == want:
                return m.get("cid_")
        return None

    @staticmethod
    def _hex_hash(h: Any) -> Optional[str]:
        """Normalize a device IR hash (raw 16 bytes) to a 32-char hex string
        (== helixgen's ``irhash``)."""
        if isinstance(h, (bytes, bytearray)):
            return bytes(h).hex()
        if isinstance(h, str):
            return h
        return None

    def list_irs(self) -> List[Dict[str, Any]]:
        """Return the device's user IRs: ``{cid_, name, hash, mono, posi}``.

        ``hash`` is normalized to the 32-hex Stadium IR hash (== helixgen
        ``irhash``).
        """
        irs = []
        for m in self.list_container(USER_IRS):
            hh = self._hex_hash(m.get("hash"))
            if hh is None:
                continue
            m = dict(m)
            m["hash"] = hh
            irs.append(m)
        irs.sort(key=lambda m: m.get("posi", 1 << 30))
        return irs

    def device_ir_hashes(self) -> set:
        """The set of IR hashes (hex) present on the device."""
        return {m["hash"] for m in self.list_irs()}

    def ir_path_for_hash(self, hash_hex: str) -> Optional[str]:
        """Return the device's on-disk path for an IR ``hash`` (hex), or ``None``
        if the device doesn't have it registered.

        This is the **reliable** registration check — it reflects a newly
        imported IR immediately, unlike ``list_irs``/``/GetContainerContents``
        (whose container listing lags after a write). Uses the editor's own
        ``/IrPathForHashGet`` (16-byte blob arg)."""
        try:
            blob = bytes.fromhex(hash_hex)
        except ValueError:
            return None
        for _addr, args in self._rpc("/IrPathForHashGet", [("b", blob)]):
            # reply /xxxIrxPathForHash1 [reqid, path]; empty path == not present
            if len(args) >= 2 and isinstance(args[1], str):
                return args[1] or None
        return None

    def get_ref(self, cid: int) -> Optional[Dict[str, Any]]:
        for _addr, args in self._rpc("/GetContentRef", [("i", cid)]):
            for a in args:
                if isinstance(a, dict):
                    return a
        return None

    def find_by_pos(self, container: int, pos: int) -> Optional[Dict[str, Any]]:
        for m in self.list_container(container):
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

    # -- writes (proven commands) -----------------------------------------
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

    def set_param(self, path: int, block: int, param_id: int, value: float) -> bool:
        """Set a param in the edit buffer: /ParamValueSet [_, path, block, 0, paramId, value, -1]."""
        return self._ok(self._rpc(
            "/ParamValueSet",
            [("i", path), ("i", block), ("i", 0), ("i", param_id),
             ("f", float(value)), ("i", -1)]))

    def set_model(self, model_id: int) -> bool:
        """Set the selected block's model: /ModelSet [127, 0, 1, 0, modelId].

        Note: /ModelSet does not take our request id; it is a fixed-shape
        command.  Sent without reqid correlation.
        """
        if self.sock is None:
            raise HelixError("client is not connected; call connect() first")
        self.sock.send(osc_encode(
            "/ModelSet",
            [("i", 127), ("i", 0), ("i", 1), ("i", 0), ("i", model_id)]))
        # best-effort: drain any immediate reply
        self.poller.poll(int(self.rpc_timeout * 1000))
        return True

    def _find_by_pos_retry(self, container: int, pos: int,
                           tries: int = 4, delay: float = 0.25
                           ) -> Optional[Dict[str, Any]]:
        """find_by_pos with a few retries — the device may re-index the
        container slightly after a write lands."""
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
    def _create_content(self, container: int, pos: int, name: str,
                        ctype: int = 2) -> Optional[int]:
        """Create an empty preset entry (`/CreateContent`); return its new CID.

        Unlike other writes, ``/CreateContent`` replies ``/status [reqid,
        newCid, code]`` — the new CID is in the second field, the ok-code in the
        third.

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
            if addr == "/status" and len(args) >= 3 and args[2] == 0:
                return args[1]
        return None

    def _save_preset_with_cid(self, cid: int, block_count: int = 0) -> bool:
        """Persist the current edit buffer into an existing CID (`/SavePresetWithCID`)."""
        return self._ok(self._rpc(
            "/SavePresetWithCID", [("i", cid), ("i", 0), ("i", block_count)]))

    def _save_edit_buffer_to(self, container: int, pos: int, name: str) -> Optional[int]:
        """Save the current edit buffer as a new preset at ``pos``; return its CID.

        Mirrors the editor's "Save Preset As -> Save As New": CreateContent then
        SavePresetWithCID.
        """
        cid = self._create_content(container, pos, name)
        if cid is None:
            return None
        if not self._save_preset_with_cid(cid):
            # don't leave an orphaned empty entry occupying the slot
            try:
                self._delete(container, [cid])
            except HelixError:
                pass
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
                     blob: bytes) -> Optional[int]:
        """Create a new preset at ``pos`` and write ``blob`` into it (restore a
        backup / clone / install authored content).  Returns the new CID."""
        cid = self._create_content(container, pos, name)
        if cid is None:
            return None
        if not self._set_content_data(cid, blob):
            try:
                self._delete(container, [cid])
            except HelixError:
                pass
            return None
        return cid

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
        """Lowest ``posi`` not currently occupied in ``container``."""
        used = {m.get("posi") for m in self.list_container(container)}
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
            if pos is None:
                pos = self._lowest_empty_posi(Container.POOL)
            cid = self._push_to_slot(Container.POOL, pos, name, blob)
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

    def mirror_setlist(self, setlist_cid: int,
                       ordered_pool_cids: Sequence[int]) -> Dict[str, list]:
        """Reconcile ``setlist_cid``'s references to exactly ``ordered_pool_cids``
        in order.

        Lists the current references (``cctp==1003``, with ``rcid`` + ``posi``),
        removes every reference whose ``(rcid, posi)`` isn't in the desired
        sequence, then adds the desired references at their target positions.
        Pool presets are NEVER deleted (no orphaning). Returns
        ``{"added": [ref_cid, ...], "removed": [ref_cid, ...]}``.
        """
        with self.mutating():
            current = [m for m in self.list_container(setlist_cid)
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
