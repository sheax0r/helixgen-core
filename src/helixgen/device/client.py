"""HelixClient — network control of a Line 6 Helix Stadium over the LAN.

Speaks the editor's own protocol: OSC messages over ZeroMQ (ZMTP 3.0), msgpack
blob payloads.  Connects a DEALER to the device's ROUTER on port 2002 for
request/response RPC.  See ``docs/helix-protocol.md`` for the wire format.

``pyzmq`` and ``msgpack`` are imported lazily so importing helixgen without the
``device`` extra never fails; construct/connect raises a clear error instead.
"""
from __future__ import annotations

import itertools
import time
from typing import Any, Dict, List, Optional, Sequence

from .osc import osc_encode, parse_osc_message
from . import content as _content

# Virtual setlist container slots.
FACTORY = -1
USER = -2
THROWAWAY = -5
USER_IRS = -11

# Content types (cctp).
CT_PRESET = 1000
CT_SETLIST = 1001
CT_TEMPLATE = 1002

_SLOT_LETTERS = "ABCD"


class HelixError(Exception):
    """A device RPC failed or the network client could not be used."""


def slot_label(posi: Optional[int]) -> str:
    """Device ``posi`` -> Helix bank/slot label, e.g. 0 -> '1A', 5 -> '2B'."""
    if posi is None:
        return ""
    return f"{posi // 4 + 1}{_SLOT_LETTERS[posi % 4]}"


class HelixClient:
    def __init__(self, ip: str = "192.168.4.84", port: int = 2002,
                 *, connect_settle: float = 0.6, rpc_timeout: float = 2.0):
        self.ip = ip
        self.port = port
        self.connect_settle = connect_settle
        self.rpc_timeout = rpc_timeout
        self._rid = itertools.count(1000)
        self._zmq = None
        self._ctx = None
        self.sock = None
        self.poller = None

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

    def connect(self, verify: bool = True) -> "HelixClient":
        """Open the DEALER socket. If ``verify``, confirm a device actually
        answers (a lazily-connected socket to a dead/wrong host never errors on
        its own, which would otherwise make every read look like an empty
        result)."""
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
        if verify and self.get_ref(USER) is None:
            self.close()
            raise HelixError(
                f"no Helix Stadium answered at {self.ip}:{self.port} "
                "(wrong IP, device off, or Remote Access disabled?)")
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
        # zmq's exception type, or an empty tuple when a fake socket is injected
        zmq_error = getattr(self._zmq, "ZMQError", ()) if self._zmq else ()
        rid = next(self._rid)
        try:
            self.sock.send(osc_encode(addr, [("i", rid)] + list(args)))
        except zmq_error as exc:
            raise HelixError(f"device send failed: {exc}") from exc
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
                raise HelixError(f"device recv failed: {exc}") from exc
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
        """Return the known virtual setlist containers that resolve."""
        out = []
        for slot in (FACTORY, USER, THROWAWAY):
            ref = self.get_ref(slot)
            if ref:
                ref = dict(ref)
                ref.setdefault("cid_", slot)
                out.append(ref)
        return out

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

    def create_copy(self, container: int, src_cids: Sequence[int], pos: int) -> bool:
        """Copy preset(s) by CID into ``container`` at slot ``pos`` (CREATE)."""
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

    def delete(self, container: int, cids: Sequence[int]) -> bool:
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
    def create_from(self, src_cid: int, container: int, pos: int) -> Optional[int]:
        """Copy ``src_cid`` into ``container`` at ``pos``; return the new CID."""
        if not self.create_copy(container, [src_cid], pos):
            return None
        m = self._find_by_pos_retry(container, pos)
        return m.get("cid_") if m else None

    # -- write current edit buffer to a new preset slot --------------------
    def create_content(self, container: int, pos: int, name: str,
                       ctype: int = 2) -> Optional[int]:
        """Create an empty preset entry (`/CreateContent`); return its new CID.

        Unlike other writes, ``/CreateContent`` replies ``/status [reqid,
        newCid, code]`` — the new CID is in the second field, the ok-code in the
        third.
        """
        msgpack = self._load_msgpack()
        for addr, args in self._rpc(
                "/CreateContent",
                [("i", container), ("i", pos), ("i", ctype),
                 ("b", msgpack.packb({"name": name}))]):
            if addr == "/status" and len(args) >= 3 and args[2] == 0:
                return args[1]
        return None

    def save_preset_with_cid(self, cid: int, block_count: int = 0) -> bool:
        """Persist the current edit buffer into an existing CID (`/SavePresetWithCID`)."""
        return self._ok(self._rpc(
            "/SavePresetWithCID", [("i", cid), ("i", 0), ("i", block_count)]))

    def save_edit_buffer_to(self, container: int, pos: int, name: str) -> Optional[int]:
        """Save the current edit buffer as a new preset at ``pos``; return its CID.

        Mirrors the editor's "Save Preset As -> Save As New": CreateContent then
        SavePresetWithCID.
        """
        cid = self.create_content(container, pos, name)
        if cid is None:
            return None
        if not self.save_preset_with_cid(cid):
            # don't leave an orphaned empty entry occupying the slot
            try:
                self.delete(container, [cid])
            except HelixError:
                pass
            return None
        return cid

    # -- push arbitrary content to a preset (restore / clone / author) ------
    def set_content_data(self, cid: int, blob: bytes) -> bool:
        """Write preset content to an existing CID (`/SetContentData`).

        ``blob`` may be either an edit-buffer (`_sbepgsm`) blob or an already
        stored-content blob; it is converted as needed.
        """
        blob = _content.to_content_data(bytes(blob))
        return self._ok(self._rpc("/SetContentData", [("i", cid), ("b", blob)]))

    def push_to_slot(self, container: int, pos: int, name: str,
                     blob: bytes) -> Optional[int]:
        """Create a new preset at ``pos`` and write ``blob`` into it (restore a
        backup / clone / install authored content).  Returns the new CID."""
        cid = self.create_content(container, pos, name)
        if cid is None:
            return None
        if not self.set_content_data(cid, blob):
            try:
                self.delete(container, [cid])
            except HelixError:
                pass
            return None
        return cid
