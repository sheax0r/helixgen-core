"""LiveSession — the state behind `helixgen device live`.

Holds ONE structure parsed from the device's live edit buffer
(:mod:`helixgen.liveedit.chain`) and keeps it true: page writes go straight to
the device (fast-ack, clamped, never queued here), and the device's 2001 change
stream updates it in place. The device is the only source of truth — there is
no `.hsp` in this session.

Locking (lock order: ``devlock`` then ``cond``, never the reverse):

* ``devlock`` serializes device I/O — the client is not thread-safe.
* ``cond`` guards the in-memory state and the change log; it is only ever held
  for microseconds, so a slow device read never stalls the page's event stream.

Changes reach the page as a log of small messages (``param`` / ``bypass`` /
``meta``); a structural change (preset or snapshot switch, re-read) bumps
``rev`` and the page gets the full state instead.
"""
from __future__ import annotations

import collections
import contextlib
import logging
import math
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from helixgen.device import content as _content, defs
from helixgen.device.client import HelixError
from helixgen.liveedit import chain as _chain
from helixgen.liveedit.events import DIRTY_KEY, decode_event

logger = logging.getLogger("helixgen.liveedit")

# A snapshot recall is announced as /activateSnapshot followed by the batch of
# values it applied. When our re-read for the first is slow, the second is
# still queued when it finishes — and what it describes is already in that
# read. Within this window after a snapshot re-read, the batch is not news.
SNAPSHOT_SETTLE = 1.0
DEAD_AFTER = 6.0      # s of total silence on the PUB streams = disconnected
RETRY_EVERY = 3.0     # s between reconnect attempts while disconnected
_OWN_MAX = 256        # own write echoes remembered
_SEQ_MOD = 1 << 32    # the 2001 header sequence is a uint32; wraps at 2**32
TUNER_STALE = 1.5     # s since the last 2003 pitch event = stale -> no-signal
# Endpoint / routing / harness categories that are NOT user-addable as chain
# blocks — adding one on row 0 corrupts the grid or routing (swap_model refuses
# the same set for in-place swaps).
_NOT_ADDABLE = frozenset({"input", "output", "split", "join", "none", "harness"})
# Dual-cab (a 2nd cab at sub=1) applies to a cab block only — an IR loader
# ("ir") is a different thing. Keep in step with defs.category_for.
_CAB_CATEGORIES = frozenset({"cab_ir_interp"})


class LiveError(Exception):
    """A request the session refuses or could not complete. ``status`` is the
    HTTP status the server answers with; ``value`` (when set) is what the
    control should show instead — the device's last known value."""

    def __init__(self, msg: str, *, status: int = 400, value: Any = None):
        super().__init__(msg)
        self.status = status
        self.value = value


def decode_blob(blob: bytes) -> Any:
    """A pushed ``/setEditBuffer`` blob -> the same dict ``read_edit_buffer``
    returns."""
    return _content.decode_any(blob)


class _NoLease:
    def ensure(self) -> None:
        pass

    def done(self) -> None:
        pass

    def transient(self, scopes):
        return contextlib.nullcontext()

    def tick(self) -> None:
        pass


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-4 * max(1.0, abs(a), abs(b))


def _controller_cids_for(eb: Any, path: int, grid_slot: int, pid: int) -> List[int]:
    """Device controller ids (``cg__.entt.ctrl.cid_``) whose target is param
    ``pid`` on the block at grid ``grid_slot`` of DSP ``path``.

    The target block's entity id is its ``blks`` ``id__``; a ``trgs`` leaf links
    ``tid_`` -> ``(eID_=id__, pid_, slot=dsp)``, and ``ctrl.tid_ == trgs.id__``.
    Returns every matching controller (a param may carry more than one)."""
    if not isinstance(eb, dict):
        return []
    flow = (eb.get("sfg_") or {}).get("flow")
    if not (isinstance(flow, list) and 0 <= path < len(flow)):
        return []
    eid = None
    for s, raw in _chain._blks_pairs(flow[path]):
        if s == grid_slot:
            eid = raw.get("id__")
            break
    if not isinstance(eid, int) or isinstance(eid, bool):
        return []
    entt = (eb.get("cg__") or {}).get("entt") or {}
    tids = {t.get("id__") for t in (entt.get("trgs") or [])
            if isinstance(t, dict) and t.get("eID_") == eid
            and t.get("pid_") == pid and t.get("slot") == path
            and t.get("type") == 2}
    return [c["cid_"] for c in (entt.get("ctrl") or [])
            if isinstance(c, dict) and c.get("tid_") in tids
            and isinstance(c.get("cid_"), int) and not isinstance(c.get("cid_"), bool)]


def _local_source_wire(source: str, device_id: Any = "stadium_xl"):
    """Resolve a controller identifier (``FS1``, ``EXP1``, ``EXP1Toe``, …) to the
    ``(locl, ctxt, behv)`` the live source-create cascade (:meth:`LiveSession.
    assign_local_source`) sends, or raise ``LiveError(400)`` for an unknown or
    unmappable source.

    ``(locl, ctxt)`` is the device source code, from ``transcode``'s own
    ``.hsp``-source → device mapping (the single source of truth for both this
    and preset install). ``behv`` is ``transcode._BEHV_INDEX``: 2 (continuous)
    for an expression pedal, 0 (latching) for a footswitch or toe switch."""
    from helixgen import controllers as _ctl
    from helixgen.device import transcode as _tx
    rows = {r["id"]: r for r in _ctl.controller_mapping(device_id)}
    row = rows.get(source)
    if row is None:
        raise LiveError(f"unknown controller source {source!r}; valid: "
                        f"{sorted(rows)}", status=400)
    lc = _tx._controller_locl_ctxt(row["source_id"])
    if lc is None:
        raise LiveError(f"controller source {source} has no device code",
                        status=400)
    locl, ctxt = lc
    behv = 2 if row["kind"] == "expression" else 0
    return locl, ctxt, behv


def _group_controllers(recipe: dict, labels: Dict[str, str]) -> dict:
    """Shape a ``view`` recipe into the Controllers pane's read-only groups,
    riding each footswitch/pedal with its English ``*_label`` (spec / CLAUDE.md:
    never a bare ``FS#``). ``labels`` maps a logical id (``FS3``, ``EXP1``) to
    its English string; a missing one falls back to the raw id."""
    fs = [{**e, "switch_label": labels.get(e.get("switch"), e.get("switch"))}
          for e in recipe.get("footswitches", [])]
    exp = [{**p, "pedal_label": labels.get(p.get("pedal"), p.get("pedal"))}
           for p in recipe.get("expression", [])]
    return {
        "footswitches": fs,
        "expression": exp,
        "midi": recipe.get("midi", []),
        "unknown": recipe.get("unknown_controllers", []),
    }


# --- Command Center pane (81v.4): read-only projection ------------------------
# The ``.hsp`` MIDI ``Command`` subtype -> label. Note/MMC are swapped against
# the editor's ``cc_midi_command`` vocab (untranscode ``_DEVICE_TO_HSP_MIDI_FUNC``
# documents the swap), so this is an EXPLICIT map, not an index into that enum.
_MIDI_SUBTYPE_LABEL = {0: "Bank/Program", 1: "CC", 2: "MMC", 3: "Note On"}


def _enum_label(controls: dict, name: str, idx: Any) -> Optional[str]:
    """``controls[name].labels[idx]`` when in range, else ``None`` (the caller
    falls back to the raw int rather than guessing)."""
    try:
        labels = (controls.get(name) or {}).get("labels") or []
        i = int(idx)
    except (TypeError, ValueError):
        return None
    return labels[i] if 0 <= i < len(labels) else None


def _instant_label(switch: Optional[str]) -> Optional[str]:
    """``Instant7`` -> ``Instant 7`` (controller_mapping has no Instant rows, so
    the English label map never covers these)."""
    if isinstance(switch, str) and switch.startswith("Instant"):
        return "Instant " + switch[len("Instant"):]
    return None


def _command_fields(typ: str, p: dict, controls: dict) -> List[List[str]]:
    """The meaningful, per-family fields of one command, as ``[name, value]``
    display pairs. Shows only the slots a family actually uses (the decoder
    zero-fills the rest), so a legitimate value of 0 is never hidden and an
    unused slot is never shown. Enum values resolve to labels where the device
    vocab is known; an out-of-range index falls back to the raw int."""
    def s(name: str) -> str:
        return str(p.get(name, 0))

    def off(name: str) -> str:
        # The forward path writes -1 into an unset Bank/Program slot; the manual
        # shows that as "Off", not a raw -1 (a real PC/MSB/LSB is 0..127).
        return "Off" if p.get(name, 0) == -1 else str(p.get(name, 0))

    if typ == "MIDI":
        cmd = p.get("Command")
        ch = ["Channel", s("MIDI Ch")]
        if cmd == 0:        # Bank/Program
            return [ch, ["PC", off("PC")], ["MSB(CC00)", off("MSB")],
                    ["LSB(CC32)", off("LSB")]]
        if cmd == 1:        # CC
            return [ch, ["CC#", s("CC#")], ["Value", s("Value")]]
        if cmd == 2:        # MMC
            msg = _enum_label(controls, "cc_midi_message", p.get("Message"))
            return [ch, ["Message", msg or s("Message")]]
        if cmd == 3:        # Note On
            note = _enum_label(controls, "midi_note", p.get("Note"))
            return [ch, ["Note", note or s("Note")], ["Velocity", s("Velocity")]]
        return [ch]
    if typ == "PresetSnapshot":
        cmd = p.get("Command")
        if cmd == 4:        # "Preset" (cc_presetsnapshot_command index)
            return [["Setlist", s("Setlist")], ["Preset", s("Preset")]]
        if cmd == 7:        # "Snapshot"
            return [["Snapshot", s("Snapshot")]]
        return []
    return []


def _command_display(rec: dict, controls: dict) -> dict:
    """One flattened ``bridge.hsp_commands`` record -> the inspector row the
    Command Center pane renders: family, an English headline (the command /
    MIDI subtype), the Action, per-footswitch behaviour, and the family's
    fields. Read-only — no device vocab is invented, unknown families pass
    through under their raw type."""
    p = rec.get("params") or {}
    typ = rec.get("type")
    action = _enum_label(controls, "cc_action", p.get("Action")) or ""
    if typ == "MIDI":
        sub = _MIDI_SUBTYPE_LABEL.get(p.get("Command"))
        family, headline = "MIDI", ("MIDI " + sub if sub else "MIDI")
    elif typ == "PresetSnapshot":
        family = "Preset/Snapshot"
        headline = (_enum_label(controls, "cc_presetsnapshot_command",
                                p.get("Command")) or family)
    else:
        family = headline = typ or "Command"
    return {"family": family, "headline": headline, "action": action,
            "behavior": rec.get("behavior", ""), "toggle": bool(rec.get("toggle")),
            "fields": _command_fields(typ, p, controls)}


def _format_commands(cmds: List[dict], device_id: Any, controls: dict,
                     labels: Dict[str, str]) -> dict:
    """Flattened commands -> the pane's ``{"sources": [...]}`` shape, grouped by
    source in first-seen order. Each source rides its English label (CLAUDE.md:
    never a bare ``FS#``); a source helixgen can't name is still shown (never
    silently dropped) under its raw id. ``device_id`` None falls back to the XL
    source table, matching the Controllers pane's source catalogue."""
    from helixgen import controllers as _ctl
    dev = device_id or "stadium_xl"
    by_source: Dict[str, dict] = {}
    order: List[str] = []
    for rec in cmds:
        sid = rec.get("source")
        try:
            switch = _ctl.command_switch_for_source(dev, sid)
        except Exception:  # noqa: BLE001 — an unknown device must not sink the pane
            switch = None
        key = switch or (f"{sid:#010x}" if isinstance(sid, int) else str(sid))
        if key not in by_source:
            label = (labels.get(switch) or _instant_label(switch) or switch
                     or ("source " + key))
            by_source[key] = {"id": switch or key, "label": label,
                              "named": switch is not None, "commands": []}
            order.append(key)
        by_source[key]["commands"].append(_command_display(rec, controls))
    return {"sources": [by_source[k] for k in order]}


class LiveSession:
    LOG_MAX = 512

    def __init__(self, client=None, *, lease=None, liveness: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.lease = lease if lease is not None else _NoLease()
        self.liveness = liveness
        self._clock = clock
        self.cond = threading.Condition()
        self.devlock = threading.Lock()
        self.version = 0          # bumps on every message
        self.rev = 0              # bumps on every structural change
        self._log: Deque[Tuple[int, dict]] = collections.deque(maxlen=self.LOG_MAX)
        self._chain: dict = {"snapshot": None, "snapshots": [], "dirty": False, "paths": []}
        self._params: Dict[Tuple[int, int, int], dict] = {}
        self._blocks: Dict[Tuple[int, int], dict] = {}
        # The raw decoded edit buffer behind _chain, kept so the Controllers
        # pane (81v.3) can project controller assignments off it without a
        # second device read. None until the first install.
        self._eb: Optional[dict] = None
        self._lib = None          # lazily-loaded block library for that projection
        self._preset: dict = {"cid": None, "name": None}
        self._dirty = False
        # Offline cold-start: with no client the page shows a disconnected,
        # empty chain until a discovery loop calls connect_device().
        self._connected = client is not None
        self._awaiting_ref = False
        # own writes awaiting their 2001 echo: request id -> (control, value),
        # and per control the request id of our NEWEST write to it
        self._own: "collections.OrderedDict[int, tuple]" = collections.OrderedDict()
        self._latest: Dict[tuple, int] = {}
        # the control a write is on the wire for, and the events for it that
        # arrived meanwhile (its echo can beat its ack to us)
        self._writing: Optional[tuple] = None
        self._deferred: List[tuple] = []
        self._gen = 0             # bumps on every install (stale-read guard)
        self._need_reread = False
        self._snapshot_reread_at = -math.inf
        self._last_event: Optional[float] = None
        self._last_retry = -math.inf
        self._seq: Dict[int, int] = {}   # port -> last 2001 header sequence seen
        # Tuner pane (81v.10): the latest pitch reading off the always-on 2003
        # /dspEvent stream, with the monotonic time it arrived (so a stalled
        # stream reads as no-signal rather than a frozen note). Read-only.
        self._tuner: Optional["TunerReading"] = None
        self._tuner_at = -math.inf

    @property
    def connected(self) -> bool:
        return self._connected

    # --- device bring-up ----------------------------------------------------
    def connect_device(self, client) -> None:
        """Attach a freshly connected device client and read its edit buffer
        into state (the eager sequence `device live` used to do at startup,
        now driven by the discovery loop). Idempotent at the call site: the
        caller only calls this while disconnected. Raises :class:`LiveError`
        (and leaves the session disconnected) if the first read fails — the
        discovery loop then keeps looking."""
        with self.cond:
            self.client = client
        self.load()

    # --- state (all under cond) ---------------------------------------------
    def _meta(self) -> dict:
        snap = self._chain.get("snapshot")
        names = self._chain.get("snapshots") or []
        name = names[snap] if isinstance(snap, int) and 0 <= snap < len(names) else None
        return {"dirty": self._dirty, "connected": self._connected,
                "preset": dict(self._preset), "snapshot": snap,
                "snapshot_name": name}

    def _emit(self, msg: dict) -> None:
        self.version += 1
        self._log.append((self.version, msg))
        self.cond.notify_all()

    def _emit_meta(self) -> None:
        self._emit({"t": "meta", **self._meta()})

    def emit_library(self, delta: dict) -> None:
        """Push a library-drawer delta onto the same SSE stream the page
        consumes for chain changes. Drawer deltas carry ``kind`` (not ``t``),
        so the page routes them to the library drawer. Used by the diff
        reconcile (badge settles) and by content-store events."""
        if not delta:
            return
        with self.cond:
            self._emit(dict(delta))

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self._emit_meta()

    def _set_connected(self, up: bool) -> None:
        with self.cond:
            if up != self._connected:
                self._connected = up
                self._emit_meta()

    def _install(self, eb: Any, preset: Optional[dict],
                 dirty: Optional[bool] = False, *,
                 expect_gen: Optional[int] = None,
                 awaiting_ref: bool = False) -> bool:
        """Swap in a freshly parsed buffer. ``preset`` / ``dirty`` of ``None``
        keep what the session has. With ``expect_gen``, the swap only happens
        if no OTHER install landed since that generation was read (a pushed
        buffer is newer than a read that was in flight when it arrived);
        returns whether it happened."""
        built = _chain.build_chain(eb)
        params, blocks = _chain.param_index(built), _chain.block_index(built)
        with self.cond:
            if expect_gen is not None and self._gen != expect_gen:
                return False
            self._chain, self._params, self._blocks = built, params, blocks
            self._eb = eb if isinstance(eb, dict) else None
            if preset is not None:
                self._preset = {"cid": preset.get("cid"), "name": preset.get("name")}
            if dirty is not None:
                self._dirty = dirty
            self._connected = True
            self._awaiting_ref = awaiting_ref
            self._need_reread = False
            self._own.clear()
            self._latest.clear()
            self._deferred = []       # they addressed the buffer just replaced
            self._gen += 1
            self.rev += 1
            self.version += 1
            self.cond.notify_all()
        return True

    def public_state(self) -> dict:
        with self.cond:
            return {"rev": self.rev, "version": self.version, **self._meta(),
                    "snapshots": list(self._chain.get("snapshots") or []),
                    "snapshot_colors": list(self._chain.get("snapshot_colors") or []),
                    "paths": self._chain["paths"]}

    def param(self, path: int, block: int, pid: int) -> dict:
        with self.cond:
            return dict(self._params[(path, block, pid)])

    def block(self, path: int, block: int) -> dict:
        with self.cond:
            return dict(self._blocks[(path, block)])

    # --- change log -----------------------------------------------------------
    def changes_since(self, version: int, rev: int) -> Tuple[int, int, List[dict]]:
        """Messages a page at ``(version, rev)`` is missing, as
        ``(version, rev, msgs)``. A structural change, or a page too far behind
        for the bounded log, gets one full ``state`` message instead."""
        with self.cond:
            if rev == self.rev and version == self.version:
                return self.version, self.rev, []
            overflow = not self._log or self._log[0][0] > version + 1
            if rev != self.rev or overflow:
                state = {"t": "state", "rev": self.rev, "version": self.version,
                         **self._meta(),
                         "snapshots": list(self._chain.get("snapshots") or []),
                         "snapshot_colors": list(self._chain.get("snapshot_colors") or []),
                         "paths": self._chain["paths"]}
                return self.version, self.rev, [state]
            return self.version, self.rev, [m for v, m in self._log if v > version]

    def wait(self, version: int, rev: int, timeout: float) -> None:
        """Block until something changed past ``(version, rev)`` or ``timeout``."""
        with self.cond:
            self.cond.wait_for(
                lambda: self.version != version or self.rev != rev, timeout)

    # --- device reads -----------------------------------------------------------
    def load(self, *, identity: bool = True) -> None:
        """ONE edit-buffer read -> state. With ``identity`` (startup, reconnect,
        an explicit re-read) the active preset's identity and the unsaved flag
        are read too; a snapshot switch or an in-place change of the SAME
        preset passes ``identity=False`` and costs only the buffer read.

        Raises :class:`LiveError` (502) and marks the session disconnected when
        the device cannot be read. If a pushed buffer is installed while the
        read is in flight, the read is stale and is taken again."""
        if self.client is None:
            self._set_connected(False)
            raise LiveError("no device connected", status=503)
        with self.devlock:
            for attempt in range(3):
                with self.cond:
                    gen = self._gen
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                    preset = self.client.active_preset() if identity else None
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"could not read the device: {e}", status=502) from e
                dirty: Optional[bool] = None
                if identity:
                    try:  # the unsaved-edits flag is a device property, not in the buffer
                        dirty = bool(self.client.get_property(DIRTY_KEY).value)
                    except Exception:  # noqa: BLE001 — cosmetic; never block a load on it
                        dirty = False
                try:
                    if self._install(eb, preset, dirty,
                                     expect_gen=gen if attempt < 2 else None):
                        return
                except LiveError:
                    raise
                except Exception as e:  # noqa: BLE001 — an unparseable buffer
                    raise LiveError(f"could not parse the edit buffer: {e!r}",
                                    status=502) from e

    def reread(self) -> None:
        """An explicit, full re-read (the page's Re-read control)."""
        self.load()

    def _reread_quiet(self, *, identity: bool = True) -> None:
        try:
            self.load(identity=identity)
        except LiveError:
            pass          # already marked disconnected; tick() keeps retrying

    # --- writes -------------------------------------------------------------------
    def _require_up(self) -> None:
        if not self._connected:
            raise LiveError("the device is disconnected — re-read to retry",
                            status=503)

    def _remember(self, rid: Optional[int], key: tuple, value: Any) -> None:
        if rid is None:
            return
        self._own[rid] = (key, value)
        self._latest[key] = rid
        while len(self._own) > _OWN_MAX:
            old, (k, _v) = self._own.popitem(last=False)
            if self._latest.get(k) == old:
                del self._latest[k]

    def _ensure_lease(self, current: Any) -> None:
        """Take the write lease for the write about to happen; the caller MUST
        call ``self.lease.done()`` when the write is over."""
        try:
            self.lease.ensure()
        except LiveError as e:
            e.value = current       # the page puts the control back
            raise

    def _begin_write(self, key: tuple) -> None:
        """(under ``cond``) Events for ``key`` are held from now until
        :meth:`_end_write`: the echo of the write we are about to send can
        reach the subscriber thread before its ack reaches us, i.e. before we
        know the request id that identifies it as ours."""
        self._writing = key

    def _end_write(self) -> None:
        """(under ``cond``) Release the held events, in arrival order."""
        held, self._deferred, self._writing = self._deferred, [], None
        for action in held:
            self._apply_locked(action)

    def _stale_commit(self, rev0: int, lookup) -> None:
        """Called under ``cond`` with the ack in hand. If the structure changed
        while the write was on the wire (a pushed preset does not wait for
        ``devlock``), the value we sent belongs to the OLD tone: do not write
        it into the new one's state, tell the page, and go read the truth."""
        if self.rev != rev0:
            self._need_reread = True
            now = lookup()
            raise LiveError("the tone changed while this change was on its way "
                            "— re-reading the device", status=409, value=now)

    def _require_rev(self, rev: Optional[int], current: Any) -> None:
        """Refuse a write from a page still showing an OLDER structure: its
        control may now sit over a different param (the preset, the snapshot or
        a model changed between its render and this request)."""
        if rev is not None and rev != self.rev:
            raise LiveError("the tone changed on the device — this control "
                            "was refreshed instead of written", status=409,
                            value=current)

    def set_param(self, path: int, block: int, pid: int, value: Any, *,
                  client: Optional[str] = None,
                  rev: Optional[int] = None) -> dict:
        """Write one param to the live edit buffer. The value is clamped to the
        range the model advertises (the device clamps nothing); returns
        ``{"value": <what was written>}``. ``client`` (the writing page's id)
        is echoed on the change message so that page can skip its own write."""
        try:
            v = float(value)
        except (TypeError, ValueError, OverflowError):
            raise LiveError("value must be a number") from None
        if not math.isfinite(v):
            raise LiveError("value must be a finite number")
        key = (int(path), int(block), int(pid))
        with self.devlock:
            with self.cond:
                self._require_up()
                p = self._params.get(key)
                self._require_rev(rev, p["value"] if p else None)
                if p is None:
                    raise LiveError("no such control on the loaded tone "
                                    "(did the preset change?)", status=404)
                v = _chain.clamp(p, v)
                current, ptype, rev0 = p["value"], p["type"], self.rev
            self._ensure_lease(current)
            with self.cond:
                self._begin_write(("param",) + key)
            committed = False
            try:
                try:
                    ok = self.client.set_param(key[0], key[1], key[2], v,
                                               force=True, fast=True, ptype=ptype)
                    rid = getattr(self.client, "last_request_id", None)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}", status=502,
                                    value=current) from e
                finally:
                    self.lease.done()
                if not ok:
                    raise LiveError("the device did not acknowledge the write",
                                    status=502, value=current)
                with self.cond:
                    self._stale_commit(
                        rev0, lambda: (self._params.get(key) or {}).get("value"))
                    self._remember(rid, ("param",) + key, v)
                    live = self._params.get(key)
                    if live is not None:
                        live["value"] = v
                    msg = {"t": "param", "path": key[0], "block": key[1],
                           "pid": key[2], "value": v}
                    if client:
                        msg["client"] = client
                    self._emit(msg)
                    self._set_dirty(True)
                    self._end_write()
                    committed = True
            finally:
                if not committed:
                    with self.cond:
                        self._end_write()
        return {"value": v}

    def swap_model(self, path: int, block: int, model_id: int) -> dict:
        """Swap a block's model in place (hgc-cc4).

        The device allows only a SAME-category swap (a cab for a cab, an amp for
        an amp), so this validates the category up front, sends ``/ModelSet``,
        then re-reads the whole edit buffer — the block comes back carrying the
        new model and the params the device applied. Returns the fresh state for
        the page to re-render. (The device's own import re-applies the new
        model's defaults; this reflects whatever the re-read reports, so confirm
        the result on the rig.)
        """
        pth, blk, mid = int(path), int(block), int(model_id)
        with self.cond:
            self._require_up()
            cur = self._blocks.get((pth, blk)) if self._blocks else None
        if cur is None:
            raise LiveError("no such block on the loaded tone "
                            "(did the preset change?)", status=404)
        if cur.get("category") in ("input", "output", "split", "join"):
            # endpoints + routing nodes are not model-swappable; /ModelSet on an
            # endpoint grid slot is unverified/unsafe (hgc-cc4 review MEDIUM).
            raise LiveError(
                f"an endpoint / routing block ({cur.get('category')!r}) is not "
                f"model-swappable — only effect blocks are.", status=400)
        if defs.model_name_for(mid) is None:
            raise LiveError(f"unknown model id {mid}", status=400)
        new_cat = defs.category_for(mid)
        if new_cat != cur.get("category"):
            raise LiveError(
                f"a swap must stay in the same category: a "
                f"{cur.get('category')!r} block cannot become a {new_cat!r} "
                f"model (the device rejects a cross-category swap).", status=400)
        if mid == cur.get("model_id"):
            return self.public_state()          # already that model: no-op
        self._ensure_lease(None)
        try:
            with self.devlock:
                try:
                    self.client.set_block_model(pth, blk, mid)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device model swap failed: {e}",
                                    status=502) from e
        finally:
            self.lease.done()
        self.load()                              # re-read: new model + its params
        return self.public_state()

    def set_second_cab(self, path: int, block: int, model_id: int) -> dict:
        """Place or replace the dual-cab SECOND cab at a cab block's slot — the
        app's "Dual" cab — via ``/ModelSet`` with ``sub=1`` (HW-confirmed
        2026-11-22).

        Only a cab block takes a 2nd cab, and the 2nd model must itself be a cab
        of the same category (the device rejects a cross-category swap). This
        sets only the model; the 2nd cab's params stay read-only (no known
        live-write address yet — hgc-5gh). Re-reads and returns fresh state;
        confirm the result on the rig. Volatile until Save.
        """
        pth, blk, mid = int(path), int(block), int(model_id)
        if defs.model_name_for(mid) is None:
            raise LiveError(f"unknown model id {mid}", status=400)
        if defs.category_for(mid) not in _CAB_CATEGORIES:
            raise LiveError("the 2nd cab must be a cab model.", status=400)
        err: Optional[Exception] = None
        applied = True
        skip = False
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                # Validate against the CURRENT buffer under the lock — the block
                # may have moved / the preset changed since the page rendered, and
                # sub=1 on a non-cab slot is unverified device behaviour. Mirrors
                # move_block / add_block (closes the TOCTOU swap_model still has).
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                cur = self._block_at(eb, pth, blk)
                if cur is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                if cur.get("category") not in _CAB_CATEGORIES:
                    raise LiveError("only a cab block can carry a 2nd cab.",
                                    status=400)
                if mid == (cur.get("second_model") or {}).get("model_id"):
                    skip = True                  # already that 2nd cab: no-op
                else:
                    try:
                        self.client.set_block_model(pth, blk, mid, sub=1)
                    except (HelixError, OSError) as e:
                        err = e
                    else:                        # verify the 2nd cab actually landed
                        try:
                            after = self._block_at(
                                self.client.read_edit_buffer(fast=True), pth, blk)
                        except (HelixError, OSError) as e:
                            err = e
                        else:
                            applied = bool(after and (after.get("second_model")
                                           or {}).get("model_id") == mid)
        finally:
            self.lease.done()
        if err is not None:
            self._set_connected(False)
            raise LiveError(f"device 2nd-cab set failed: {err}",
                            status=502) from err
        if skip:
            return self.public_state()
        if not applied:
            self._reread_quiet()
            raise LiveError("the device did not apply the 2nd cab", status=502)
        self.load()                              # re-read: the dual cab
        return self.public_state()

    @staticmethod
    def _block_at(eb: Any, path: int, slot: int):
        """The editor block dict at grid ``slot`` on ``path`` from a live buffer,
        or ``None`` — carries ``category`` and ``second_model`` (see chain._block)."""
        flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
        if not (isinstance(flow, list) and 0 <= path < len(flow)):
            return None
        for s, raw in _chain._blks_pairs(flow[path]):
            if s == slot:
                return _chain._block(path, slot, raw)
        return None

    def add_block(self, path: int, model_id: int) -> dict:
        """Append a new processing block to DSP ``path``'s main row (row 0).

        Places ``model_id`` at the next free row-0 grid slot — right after the
        last block, or the lowest free slot if the tail is full — before the
        output, with ``/ModelSet``, which the device honours as an ADD at an
        empty slot (not a swap). Only a real processing model is allowed (not an
        input/output/split/join/none/harness node). The free slot is read from
        the LIVE buffer under the device lock (atomic read-compute-write), so
        concurrent adds cannot collide. Then it re-reads and returns fresh
        state; the device applies the model's defaults — confirm on the rig. The
        edit is volatile until Save.
        """
        pth, mid = int(path), int(model_id)
        if defs.model_name_for(mid) is None:
            raise LiveError(f"unknown model id {mid}", status=400)
        cat = defs.category_for(mid)
        if cat in _NOT_ADDABLE:
            raise LiveError(
                f"a {cat!r} node cannot be added to the chain — add an effect, "
                "amp, cab, or other processing block.", status=400)
        out_slot = _chain.ROW1_INPUT - 1          # row-0 output lives here (13)
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                # Read the live buffer under the lock so the slot reflects the
                # CURRENT chain (not possibly-stale self._blocks), closing the
                # add/add race.
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
                if not (isinstance(flow, list) and 0 <= pth < len(flow)):
                    raise LiveError(f"no DSP path {path} to add a block to.",
                                    status=404)
                occupied = {s for s, _ in _chain._blks_pairs(flow[pth])}
                free = [s for s in range(1, out_slot) if s not in occupied]
                if not free:
                    raise LiveError(
                        "no room to add a block on this path's main row — it is "
                        "full up to the output.", status=400)
                content = sorted(s for s in occupied if 0 < s < out_slot)
                # append after the last block, else fall back to the lowest gap
                slot = (content[-1] + 1 if content and content[-1] + 1 < out_slot
                        else free[0])
                try:
                    self.client.set_block_model(pth, slot, mid)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device add-block failed: {e}",
                                    status=502) from e
        finally:
            self.lease.done()
        self.load()                              # re-read: the appended block
        return self.public_state()

    def split_block(self, path: int, block: int) -> dict:
        """Split the block at grid slot ``block`` onto a NEW parallel lane B —
        synthesized topology (the app's drag-down-to-parallel).

        Delegates to :meth:`HelixClient.add_split`, which moves the block to
        lane B, inserts a split + join, and confirms the device applied it. Its
        guards (a lane-A block in 1..11, lane B free, room for the nodes) surface
        as a 400 with the reason. Then re-reads and returns fresh state. Volatile
        until Save; confirm the routing on the rig.
        """
        pth, blk = int(path), int(block)
        err: Optional[Exception] = None
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                try:
                    self.client.add_split(pth, blk)
                except (HelixError, OSError) as e:
                    err = e
        finally:
            self.lease.done()
        if err is not None:
            self._raise_structural(err, "split")
        self.load()                              # re-read: the new parallel lane
        return self.public_state()

    def _raise_structural(self, err: Exception, verb: str) -> None:
        """Classify a failure from a structural edit (split/move) and raise.

        The engine raises for GUARD violations (bad slot, lane in use, no room)
        AND for device/transport failures. A transport failure can land AFTER
        the agenda write, so re-sync the chain best-effort first. Then: a guard
        violation blames the request (400); a transport failure marks the
        session disconnected like the sibling writes (502).
        """
        msg = str(err).lower()
        transport = isinstance(err, OSError) or any(
            k in msg for k in ("connection", "timed out", "read failed",
                               "did not apply", "device read"))
        self._reread_quiet()                     # the write may have mutated
        if transport:
            self._set_connected(False)
            raise LiveError(f"device error during {verb}: {err}",
                            status=502) from err
        raise LiveError(f"cannot {verb} here: {err}", status=400) from err

    def move_block(self, path: int, src: int, dst: int) -> dict:
        """Move/reorder the block at grid slot ``src`` to slot ``dst`` on DSP
        ``path`` (the app's drag-to-reorder), via ``/doAgenda [{movb}]``.

        Delegates to :meth:`HelixClient.move_block`, which relocates the block,
        EXCHANGING it with whatever occupies ``dst`` (an empty ``dst`` is a
        plain move) — so moving onto a neighbour swaps the two, which is
        reorder. Both slots must be lane-A processing slots (1..12); the LIVE
        buffer is read under the lock so a stale page or a direct HTTP caller
        cannot move a non-existent block, move onto a routing node (split/join),
        or push a block onto lane B and orphan it. The 2002 reply is
        meaningless, so success is verified from the buffer: if the block did
        not land on ``dst`` a 502 is raised. Then re-reads and returns fresh
        state. Volatile until Save; confirm order on the rig.

        ponytail: path-1 (DSP 1) movb is not yet HW-validated (only DSP 0 is);
        the applied-verify turns a silent path-1 failure into a 502 rather than
        chain corruption. Validate DSP 1 on hardware when next at the rig.
        """
        pth, s, d = int(path), int(src), int(dst)
        last = _chain.ROW1_INPUT - 1              # 13 = lane-A output slot
        if s == d:
            raise LiveError("source and destination slots are the same",
                            status=400)
        if not (1 <= s < last) or not (1 <= d < last):
            raise LiveError("reorder operates on lane-A processing slots 1..12",
                            status=400)
        err: Optional[Exception] = None
        applied = True
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                # Validate against the CURRENT buffer under the lock (closes the
                # stale-page / direct-caller hole; matches add_block/add_split).
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                real = self._lane_a_models(eb, pth)   # slot -> model_id (processing only)
                occupied = self._lane_a_occupied(eb, pth)
                if real is None:
                    raise LiveError(f"no DSP path {path} to reorder.", status=404)
                if s not in real:
                    raise LiveError("no movable block at the source slot "
                                    "(did the chain change?)", status=400)
                if d in occupied and d not in real:
                    raise LiveError("cannot reorder across a routing block",
                                    status=400)
                captured = real[s]
                try:
                    self.client.move_block(pth, s, d)
                except (HelixError, OSError) as e:
                    err = e
                else:
                    try:
                        after = self._lane_a_models(
                            self.client.read_edit_buffer(fast=True), pth) or {}
                    except (HelixError, OSError) as e:
                        err = e
                    else:
                        applied = after.get(d) == captured
        finally:
            self.lease.done()
        if err is not None:
            self._raise_structural(err, "move")
        if not applied:
            self._reread_quiet()
            raise LiveError("the device did not apply the move (chain unchanged)",
                            status=502)
        self.load()                              # re-read: the reordered chain
        return self.public_state()

    @staticmethod
    def _lane_a_occupied(eb: Any, path: int) -> set:
        """Grid slots occupied by ANY block on ``path``'s lane A (row 0)."""
        flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
        if not (isinstance(flow, list) and 0 <= path < len(flow)):
            return set()
        return {s for s, _ in _chain._blks_pairs(flow[path]) if s < _chain.ROW1_INPUT}

    @staticmethod
    def _lane_a_models(eb: Any, path: int):
        """Map ``slot -> model_id`` for real PROCESSING blocks on ``path``'s
        lane A — routing/endpoint nodes (split/join/input/output/none/harness)
        excluded. ``None`` if the path is absent."""
        flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb, dict) else None
        if not (isinstance(flow, list) and 0 <= path < len(flow)):
            return None
        out: dict = {}
        for slot, raw in _chain._blks_pairs(flow[path]):
            b = _chain._block(path, slot, raw)
            if b and b["row"] == 0 and b["category"] not in _NOT_ADDABLE:
                out[slot] = b["model_id"]
        return out

    def remove_block(self, path: int, block: int) -> dict:
        """Remove the block at grid slot ``block`` on DSP ``path`` from the live
        chain — the inverse of :meth:`add_block` (the app's block-delete).

        Delegates to :meth:`HelixClient.remove_block`, which sets the slot to
        the Null model (the device has no working ``clrb`` op — HW-disproven on
        fw 1.4.1) so the device drops the block, leaving a **hole** (nothing
        else shifts). The engine reads the LIVE buffer under the lock and
        **refuses** a removal that is not unambiguously safe — a stale page or
        direct HTTP caller cannot remove a non-existent block or a
        routing/endpoint node (input, output, split, join, none, harness); its
        guards surface as a 400 with the reason. The ``/ModelSet`` reply is
        meaningless, so success is verified from the buffer: if the slot is
        still occupied a 502 is raised (without marking the device offline —
        it answered, it just did not apply). Then re-reads and returns fresh
        state. Volatile until Save; confirm on the rig.
        """
        pth, blk = int(path), int(block)
        if blk in (0, 13, 14, 27):               # fixed input/output slots
            raise LiveError("the input and output blocks cannot be removed",
                            status=400)
        err: Optional[Exception] = None
        applied = True
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                # The engine reads+validates the LIVE buffer under this lock,
                # then writes (closes the stale-page / direct-caller hole, like
                # move_block/add_block).
                try:
                    self.client.remove_block(pth, blk)
                except (HelixError, OSError) as e:
                    err = e
                else:
                    try:
                        eb = self.client.read_edit_buffer(fast=True)
                    except (HelixError, OSError) as e:
                        err = e
                    else:
                        flow = (eb.get("sfg_") or {}).get("flow")
                        if isinstance(flow, list) and 0 <= pth < len(flow):
                            occ = {s for s, _ in _chain._blks_pairs(flow[pth])}
                            applied = blk not in occ
                        else:
                            applied = False      # a malformed re-read is not proof
        finally:
            self.lease.done()
        if err is not None:
            self._raise_structural(err, "remove")
        if not applied:
            self._reread_quiet()
            raise LiveError("the device did not apply the removal (chain "
                            "unchanged)", status=502)
        self.load()                              # re-read: the removed block
        return self.public_state()

    def set_bypass(self, path: int, block: int, enabled: Any, *,
                   client: Optional[str] = None,
                   rev: Optional[int] = None) -> dict:
        """Enable/bypass one block in the live edit buffer."""
        key = (int(path), int(block))
        on = bool(enabled)
        with self.devlock:
            with self.cond:
                self._require_up()
                b = self._blocks.get(key)
                self._require_rev(rev, b["enabled"] if b else None)
                if b is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                current, rev0 = b["enabled"], self.rev
            self._ensure_lease(current)
            with self.cond:
                self._begin_write(("bypass",) + key)
            committed = False
            try:
                try:
                    ok = self.client.set_block_enable(key[0], key[1], on, fast=True)
                    rid = getattr(self.client, "last_request_id", None)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}", status=502,
                                    value=current) from e
                finally:
                    self.lease.done()
                if not ok:
                    raise LiveError("the device did not acknowledge the write",
                                    status=502, value=current)
                with self.cond:
                    self._stale_commit(
                        rev0, lambda: (self._blocks.get(key) or {}).get("enabled"))
                    self._remember(rid, ("bypass",) + key, on)
                    live = self._blocks.get(key)
                    if live is not None:
                        live["enabled"] = on
                    msg = {"t": "bypass", "path": key[0], "block": key[1],
                           "enabled": on}
                    if client:
                        msg["client"] = client
                    self._emit(msg)
                    self._set_dirty(True)
                    self._end_write()
                    committed = True
            finally:
                if not committed:
                    with self.cond:
                        self._end_write()
        return {"enabled": on}

    def save(self) -> dict:
        """Persist the edit buffer into the ACTIVE tone's preset, in place — the
        hardware's own Save. The target is the user-pool preset that holds the
        tone (for one loaded from a setlist, the preset its setlist entry
        points at — so the save shows in every setlist holding that preset);
        a factory preset or an unresolvable id is refused. Returns the pool
        ``cid`` that was written."""
        with self.devlock:
            with self.cond:
                self._require_up()
            with self.lease.transient(("editbuffer", "library")):
                try:
                    pre = self.client.active_preset()
                    active = pre.get("cid")
                    # a tone loaded from a setlist is active under its setlist
                    # REFERENCE; the content lives in the pool preset behind it
                    cid = (self.client.resolve_pool_cid(active, pre.get("ccid"))
                           if active is not None else None)
                    if cid is None:
                        raise LiveError(
                            "only a user preset can be saved in place (active "
                            f"preset: {pre.get('name') or active!r} is a "
                            "factory preset or does not resolve). Use "
                            "`helixgen device save` to store the edit buffer "
                            "as a new preset.", status=409)
                    ok = self.client.save_edit_buffer(cid)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"save failed: {e}", status=502) from e
            if not ok:
                raise LiveError("the device refused the save", status=502)
            with self.cond:
                self._preset = {"cid": active, "name": pre.get("name")}
                self._dirty = False
                self._emit_meta()
        return {"saved": pre.get("name"), "cid": cid}

    # --- page-initiated switching ---------------------------------------------
    def activate_snapshot(self, index: Any) -> dict:
        """Recall a snapshot on the device (0-based, 0..7). Does NOT discard the
        edit buffer, so the page prompts for nothing. No local state change is
        made here: the device answers with ``/activateSnapshot`` +
        ``/setBatchedParamVals``, which :meth:`handle_events` applies in place
        (hgc-9qd), falling back to a re-read only when the batch cannot be
        decoded."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise LiveError("snapshot index must be an integer") from None
        if not 0 <= i <= 7:
            raise LiveError("snapshot index out of range 0..7")
        with self.devlock:
            with self.cond:
                self._require_up()
            self.lease.ensure()
            try:
                self.client.activate_snapshot(i)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not activate the snapshot: {e}",
                                status=502) from e
            finally:
                self.lease.done()
        return {"snapshot": i}

    def rename_snapshot(self, index: Any, name: Any) -> dict:
        """Rename snapshot ``index`` (0..7) on the live device via the granular
        ``/SetSnapshotName`` opcode, then re-read so the panel shows the new
        name. Volatile until Save, like every live edit."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise LiveError("snapshot index must be an integer") from None
        if not 0 <= i <= 7:
            raise LiveError("snapshot index out of range 0..7")
        if not isinstance(name, str) or not name.strip():
            raise LiveError("snapshot name must be a non-empty string")
        with self.devlock:
            with self.cond:
                self._require_up()
            self.lease.ensure()
            try:
                self.client.set_snapshot_name(i, name)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not rename the snapshot: {e}",
                                status=502) from e
            finally:
                self.lease.done()
        self.load()                              # re-read: the new snapshot name
        return self.public_state()

    def set_snapshot_color(self, index: Any, color: Any) -> dict:
        """Set snapshot ``index``'s colour on the live device via the granular
        ``/SnapshotColorSet`` opcode, then re-read so the panel shows it.
        ``color`` is a palette token/label or an int index (coerced through
        ``maintenance.color_index``). Volatile until Save."""
        from helixgen.device import maintenance
        i = self._snap_index(index, "snapshot index")
        try:
            c = maintenance.color_index(color)
        except ValueError as e:
            raise LiveError(str(e)) from None
        with self.devlock:
            with self.cond:
                self._require_up()
            self.lease.ensure()
            try:
                self.client.set_snapshot_color(i, c)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not set the snapshot colour: {e}",
                                status=502) from e
            finally:
                self.lease.done()
        self.load(identity=False)                # re-read: the new colour
        with self.cond:
            self._set_dirty(True)                # a colour change is an unsaved edit
        return self.public_state()

    @staticmethod
    def _is_snapshot_named(nm: Any, idx: int) -> bool:
        """Is ``nm`` a real, user-set snapshot name — not a default placeholder?
        The device labels an unconfigured slot ``"Snap N"``; a gap or empty
        entry decodes as ``"Snapshot N"`` (``chain._snapshot_names``). Both are
        placeholders (spec 81v.2 §8.1: configured iff ``name != "Snap N"``)."""
        return bool(nm) and nm not in (f"Snap {idx + 1}", f"Snapshot {idx + 1}")

    @staticmethod
    def _snap_index(index: Any, what: str) -> int:
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise LiveError(f"{what} must be an integer") from None
        if not 0 <= i <= 7:
            raise LiveError(f"{what} out of range 0..7")
        return i

    def _snapshot_scan(self, eb: Any):
        """Active param values + types and block-enable states from a decoded
        edit buffer, as flat maps keyed by ``(path, block[, pid])``, plus the
        slot-indexed snapshot names. What a copy/swap reads off each recalled
        snapshot."""
        built = _chain.build_chain(eb)
        pidx = _chain.param_index(built)
        vals = {k: v["value"] for k, v in pidx.items()}
        types = {k: v.get("type") for k, v in pidx.items()}
        enables = {k: v["enabled"] for k, v in _chain.block_index(built).items()}
        names = list(built.get("snapshots") or [])
        return vals, types, enables, names

    @staticmethod
    def _snapshot_writes(src_vals: dict, dst_vals: dict,
                         src_en: dict, dst_en: dict):
        """The param + bypass writes that make the DST snapshot equal SRC.

        ONLY targets whose value differs are written — and a value that differs
        between two snapshots is necessarily snapshot-assigned, so a param or
        bypass that is NOT snapshot-controlled (identical across every snapshot)
        is skipped automatically. This is exactly how the Stadium app batches a
        snapshot copy: there is no atomic copy opcode."""
        pw = [(k, src_vals[k]) for k in src_vals
              if k in dst_vals
              and not _close(float(src_vals[k]), float(dst_vals[k]))]
        bw = [(k, src_en[k]) for k in src_en
              if k in dst_en and bool(src_en[k]) != bool(dst_en[k])]
        return pw, bw

    def _replay(self, pw, bw, ptypes) -> None:
        """Apply a copy/swap's param + bypass writes into the ACTIVE snapshot.
        Caller holds ``devlock``; a snapshot-assigned write lands in the active
        snapshot only (HW-verified 2026-11-13)."""
        for (p, b, pid), val in pw:
            self.client.set_param(p, b, pid, val, force=True, fast=True,
                                  ptype=ptypes.get((p, b, pid)))
        for (p, b), en in bw:
            self.client.set_block_enable(p, b, bool(en), fast=True)

    def copy_snapshot(self, src: Any, dst: Any) -> dict:
        """Copy snapshot ``src``'s contents onto snapshot ``dst`` on the live
        device — the per-snapshot param values and block-bypass states, STATE
        ONLY (``dst`` keeps its own name and colour, like the app's
        Copy→Paste). No atomic copy opcode exists, so this recalls each
        snapshot and replays the differing values as granular writes, the same
        batch the Stadium app sends. Holds the edit-buffer lease across the
        whole op so no other helixgen process writes the wrong scene meanwhile
        (§8.6); a concurrent HARDWARE snapshot switch is the residual live-edit
        risk, as for every live write. Volatile until Save."""
        i = self._snap_index(src, "source snapshot")
        j = self._snap_index(dst, "target snapshot")
        if i == j:
            return self.public_state()               # copy onto itself: no-op
        with self.cond:
            self._require_up()
        self._ensure_lease(None)
        try:
            with self.devlock:
                try:
                    self.client.activate_snapshot(i)
                    sv, st, se, _ = self._snapshot_scan(self.client.read_edit_buffer())
                    self.client.activate_snapshot(j)
                    dv, _dt, de, _ = self._snapshot_scan(self.client.read_edit_buffer())
                    pw, bw = self._snapshot_writes(sv, dv, se, de)
                    self._replay(pw, bw, st)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"could not copy the snapshot: {e}",
                                    status=502) from e
        finally:
            self.lease.done()
        self.load(identity=False)        # re-read: dst holds src's state, active=dst
        with self.cond:
            self._set_dirty(True)        # the replayed writes dirtied the buffer
        return self.public_state()

    def swap_snapshot(self, a: Any, b: Any) -> dict:
        """Swap snapshots ``a`` and ``b`` on the live device — their
        per-snapshot state (params + bypass) AND their names. No atomic swap
        opcode exists, so each is recalled and the differing values replayed,
        then the two names are swapped via ``/SetSnapshotName``.

        NOTE: snapshot COLOUR is NOT swapped — the live colour opcode is not
        decoded yet (hgc-0g0.1) — so a swapped pair keeps its original slot
        colours until that lands. Same lease/active-index posture as
        :meth:`copy_snapshot`. Volatile until Save."""
        i = self._snap_index(a, "first snapshot")
        j = self._snap_index(b, "second snapshot")
        if i == j:
            return self.public_state()
        with self.cond:
            self._require_up()
        self._ensure_lease(None)
        try:
            with self.devlock:
                try:
                    self.client.activate_snapshot(i)
                    av, at, ae, names = self._snapshot_scan(self.client.read_edit_buffer())
                    self.client.activate_snapshot(j)
                    bv, bt, be, _ = self._snapshot_scan(self.client.read_edit_buffer())
                    name_i = names[i] if i < len(names) else None
                    name_j = names[j] if j < len(names) else None
                    # j is active (holding B): write A into it, so j becomes A
                    self._replay(*self._snapshot_writes(av, bv, ae, be), at)
                    # recall a (still holding A) and write B into it, so a becomes B
                    self.client.activate_snapshot(i)
                    self._replay(*self._snapshot_writes(bv, av, be, ae), bt)
                    # swap the names only when BOTH slots carry a real user name;
                    # if either is an unconfigured placeholder its state still
                    # swaps, but the placeholder label stays (renaming a
                    # configured slot to the other's placeholder literal would
                    # corrupt its identity, and /SetSnapshotName can't un-name a
                    # slot — it rejects an empty name).
                    if (self._is_snapshot_named(name_i, i)
                            and self._is_snapshot_named(name_j, j)
                            and name_i != name_j):
                        self.client.set_snapshot_name(i, name_j)
                        self.client.set_snapshot_name(j, name_i)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"could not swap the snapshots: {e}",
                                    status=502) from e
        finally:
            self.lease.done()
        self.load(identity=False)
        with self.cond:
            self._set_dirty(True)
        return self.public_state()

    def load_preset(self, cid: Any) -> dict:
        """Load a preset on the device by cid — a user-pool preset, or a setlist
        REFERENCE cid (what :meth:`list_presets` hands the page for a setlist
        entry; the device makes that reference active, see
        :meth:`HelixClient.resolve_pool_cid`). This DISCARDS the live edit
        buffer; the page guards unsaved work before calling it. The device's
        ``/setEditBuffer`` + ``/loadContentRef`` push re-reads and re-renders
        the page."""
        try:
            c = int(cid)
        except (TypeError, ValueError):
            raise LiveError("preset cid must be an integer") from None
        with self.devlock:
            with self.cond:
                self._require_up()
            self.lease.ensure()
            try:
                ok = self.client.load_preset(c)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not load the preset: {e}",
                                status=502) from e
            finally:
                self.lease.done()
        if not ok:
            raise LiveError("the device refused the preset load", status=502)
        return {"cid": c}

    def list_presets(self) -> dict:
        """The device's loadable presets, for the page's preset picker: each
        user setlist with the presets it references, then the user pool. A
        setlist entry's ``cid`` is its REFERENCE cid (what :meth:`load_preset`
        loads); ``active`` is the cid currently loaded, so the page can mark it.

        Read-only, serialized on ``devlock`` (the client's socket is not
        thread-safe) — it does NOT take the write lease: browsing presets must
        not lock other verbs out of the device. Fetched lazily when the picker
        is first opened, never at page load."""
        def _entries(items):
            out = []
            for m in items:
                if m.get("cid_") is None:
                    continue
                e = {"cid": m["cid_"], "name": m.get("name") or "(unnamed)"}
                # A setlist entry is a REFERENCE whose ``rcid`` is the pool
                # preset it points at. The device may report the loaded tone
                # under EITHER the reference cid or that pool cid, so carry the
                # pool cid to light the picker dot in both cases (hgc-6y1). Pool
                # presets are not references and have no ``rcid``.
                rcid = m.get("rcid")
                if rcid is not None:
                    e["pool_cid"] = rcid
                out.append(e)
            return out

        with self.cond:
            self._require_up()
        with self.devlock:
            try:
                pool = self.client.list_presets()
                groups = []
                for sl in self.client.list_setlists():
                    scid = sl.get("cid_")
                    if scid is None:
                        continue
                    groups.append({"name": sl.get("name") or "Setlist",
                                   "presets": _entries(self.client.list_container(scid))})
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not list the device's presets: {e}",
                                status=502) from e
        with self.cond:
            active = self._preset.get("cid")
        groups.append({"name": "Pool", "presets": _entries(pool)})
        # Mark the loaded entry: the device may report the active tone under a
        # setlist entry's reference cid OR its pool cid, so match either
        # (hgc-6y1). The page reads this flag instead of comparing cids itself.
        for g in groups:
            for e in g["presets"]:
                e["active"] = active is not None and active in (
                    e["cid"], e.get("pool_cid"))
        return {"active": active, "groups": groups}

    # --- device-global settings (81v.5 Device pane) ---------------------------------
    #
    # A read/write surface over the device-property channel (the same one
    # `device settings list/get/set` drives), for the Device pane. Device
    # globals live OUTSIDE the preset: they do not ride the edit buffer, do not
    # round-trip through .hsp, and do not reload on preset change — so this is a
    # read-on-demand surface, not part of the live chain stream.
    #
    # Page order follows the app's Global Settings menu (spec 81v.5 §7.1). A few
    # pages are shown but not editable, or carry a "takes effect on the device"
    # note (spec §8 won't-dos). The DANGEROUS_KEYS (wifi/remote access) are not
    # in the catalog at all, and `set_property` guards them regardless.
    _PAGE_META = {       # catalog id -> (label, readonly, note)
        "ins-outs": ("Ins / Outs", False, None),
        "switches-pedals": ("Switches / Pedals", False,
            "These configure the hardware's own footswitches and pedals — "
            "changes take effect on the device."),
        "displays": ("Displays", False,
            "These affect the device's own screens — changes take effect on "
            "the device."),
        "preferences": ("Preferences", False, None),
        "songs": ("Songs", False, None),
        "tempo-click": ("Tempo / Click", False, None),
        "midi": ("MIDI", False, None),
        "tuner": ("Tuner", False, None),
        "date-time": ("Date / Time", True,
            "The clock is set on the device's own screen; shown here read-only."),
    }

    def device_pages(self) -> dict:
        """The Device pane's offline catalog: every Global-Settings page in the
        app's menu order, each with its label, read-only flag, note, and keys.
        No device I/O — the page renders its tabs instantly, then reads each
        page's live values on demand via :meth:`device_values`."""
        from helixgen.device import settings as _settings
        catalog = _settings.pages()
        order = [p for p in self._PAGE_META if p in catalog]
        order += [p for p in sorted(catalog) if p not in self._PAGE_META]
        out = []
        for page in order:
            label, readonly, note = self._PAGE_META.get(
                page, (page.replace("-", " ").title(), False, None))
            out.append({"page": page, "label": label, "readonly": readonly,
                        "note": note, "keys": list(catalog[page])})
        return {"pages": out}

    def _read_keys(self, keys):
        """Read def + value for each key (serialized by the caller on
        ``devlock``). A key the firmware doesn't answer is returned with an
        ``error`` instead of a value, so one missing key never blanks a page.
        Stops and reports ``aborted_at`` if the socket dies mid-read — every
        later key would fast-fail (mirrors ``device settings list --values``)."""
        from helixgen.device import settings as _settings
        rows, aborted = [], None
        for key in keys:
            try:
                d = self.client.get_property_def(key)
                v = self.client.get_property(key)
                rows.append({"key": key, "name": d.name, "type": d.type,
                             "min": d.vmin, "max": d.vmax, "default": d.default,
                             "enum": list(d.enum), "value": v.value,
                             "display": _settings.render_value(d, v.value)})
            except (HelixError, OSError, ValueError) as e:
                rows.append({"key": key, "error": str(e)})
            if getattr(self.client, "sock", object()) is None:
                aborted = key
                break
        return rows, aborted

    # The snapshot panel surfaces a fixed slice of Global Settings (spec 81v.2
    # §8.5): the one writable toggle (Discard-Edits) plus read-only context for
    # per-snapshot tempo and MIDI Recall. Order is the panel's display order.
    _SNAPSHOT_GLOBAL_KEYS = (
        "global.snapshot.edits",      # Discard-Edits: Save(0) / Discard(1)
        "global.snapshot.reselect",   # Snapshot Reselect (context)
        "global.tempo.select",        # Snapshot / Per Preset / Global
        "global.tempo.bpm",           # active tempo (the active snapshot's, in Snapshot mode)
        "global.send.snapshot.cc",    # MIDI CC recalls a snapshot: Off/On
        "global.midi.channel",        # base MIDI channel (snapshot/preset recall)
        "global.midi.pc.send",        # PC recalls a preset: Off/MIDI/USB/MIDI+USB
    )

    def snapshot_globals(self) -> dict:
        """The snapshot-relevant Global Settings the snapshot panel surfaces:
        Discard-Edits (the one writable toggle — written through
        :meth:`device_write`), plus read-only context (tempo mode/bpm, MIDI
        Recall channel/CC/PC). Read-only and lease-free, exactly like
        :meth:`device_values`; a key this firmware doesn't carry comes back with
        an ``error`` rather than blanking the set."""
        with self.cond:
            self._require_up()
        with self.devlock:
            rows, aborted = self._read_keys(self._SNAPSHOT_GLOBAL_KEYS)
        out = {"keys": rows}
        if aborted is not None:
            out["aborted_at"] = aborted
        return out

    def device_values(self, page: str) -> dict:
        """Live def + value for one Global-Settings page's keys, for the Device
        pane. Read-only, serialized on ``devlock`` (the socket is not
        thread-safe) — it does NOT take the write lease: browsing globals must
        not lock other verbs out of the device."""
        from helixgen.device import settings as _settings
        try:
            keys = _settings.keys_for_page(page)
        except KeyError:
            raise LiveError(f"unknown settings page {page!r}", status=404)
        with self.cond:
            self._require_up()
        with self.devlock:
            rows, aborted = self._read_keys(keys)
        out = {"page": page, "keys": rows}
        if aborted is not None:
            out["aborted_at"] = aborted
        return out

    def mixer_state(self) -> dict:
        """The Matrix Mixer (device-global per-output monitor mix), read live
        over 2002. Read-only and lease-free, exactly like :meth:`device_values`
        — browsing the mixer must not lock other verbs out of the device. The
        mix lives outside the preset, so this does NOT consume the signal-flow
        state. Raises :class:`LiveError` (502) if the read fails."""
        with self.cond:
            self._require_up()
        with self.devlock:
            try:
                return self.client.get_mixer_state()
            except (HelixError, OSError, ValueError) as e:
                raise LiveError(f"could not read the mixer: {e}",
                                status=502) from e

    def device_write(self, key: str, value: Any) -> dict:
        """Write one Global-Settings value live, then read it back. ``value`` is
        a number, or an enum index — coerced and range-checked against the
        device's own definition before the write. Refuses an off-catalog key and
        a read-only page; ``set_property`` guards the DANGEROUS_KEYS regardless.
        Takes the ``globals`` scope transiently (mirrors `device settings set`)."""
        from helixgen.device import settings as _settings
        if not isinstance(key, str) or not key:
            raise LiveError("setting key must be a string")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise LiveError("setting value must be a number")
        page = _settings.page_for_key(key)
        if page is None:
            raise LiveError(f"{key!r} is not a settable Global Setting",
                            status=404)
        meta = self._PAGE_META.get(page)
        if meta and meta[1]:
            raise LiveError(f"the {meta[0]} page is read-only — change it on "
                            "the device's own screen", status=403)
        try:
            _settings.guard_key(key)
        except ValueError as e:
            raise LiveError(str(e), status=403) from e
        with self.cond:
            self._require_up()
        with self.devlock:
            with self.lease.transient(("globals",)):
                try:
                    d = self.client.get_property_def(key)
                    coerced = _settings.coerce_value(d, str(value))
                    ok = self.client.set_property(key, d.type, coerced)
                except ValueError as e:        # coerce / guard / bad def
                    raise LiveError(str(e), status=400) from e
                except HelixError as e:
                    # a device NAK or a key this firmware doesn't carry — the
                    # device said no, the connection is fine. Do NOT tear the
                    # editor down (matches _read_keys; a transport loss is the
                    # OSError below, which really did drop the connection).
                    raise LiveError(f"the device would not set {key}: {e}",
                                    status=502) from e
                except OSError as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}", status=502) from e
                if not ok:
                    raise LiveError(f"the device did not confirm the write to "
                                    f"{key}", status=502)
                # Readback is best-effort: the write already landed (ok=True), so
                # a failed readback reflects the value we wrote rather than
                # reverting the control or marking the device down.
                try:
                    rb = self.client.get_property(key)
                except (HelixError, OSError):
                    return {"key": key, "value": coerced,
                            "display": _settings.render_value(d, coerced)}
        return {"key": key, "value": rb.value,
                "display": _settings.render_value(d, rb.value)}

    # --- Global EQ pane (81v.6) -----------------------------------------------------
    def globaleq_state(self) -> dict:
        """The device's Global EQ connect-time snapshot (``global.eq.sync``),
        decoded, with each output's live master enable overlaid. Read-only and
        lease-free (like :meth:`mixer_state` / :meth:`device_values`) — browsing
        globals must not lock other verbs out of the device.

        The band/level values are the device's **connect-time** state:
        ``global.eq.sync`` is static and does NOT reflect writes
        (hardware-confirmed), so the pane shows them as a snapshot, not live
        truth. The three master enables are read fresh and are truly live."""
        with self.cond:
            self._require_up()
        with self.devlock:
            try:
                return self.client.get_globaleq_state()
            except (HelixError, OSError, ValueError) as e:
                raise LiveError(f"could not read the Global EQ: {e}",
                                status=502) from e

    def globaleq_write(self, output: str, band: str, param: str,
                       value: Any) -> dict:
        """Write one Global EQ control live. ``band==""`` with ``param=="enable"``
        is the output **master** enable (``global.eq.*.enable``, live-readable);
        ``band==""`` with ``param=="level"`` is the output level; anything else is
        a band parameter. Band/level writes go out as the write-only variant blob
        (no read-back). Takes the ``globals`` scope transiently, exactly like
        :meth:`device_write`."""
        from helixgen.device import globaleq as _globaleq
        if not (isinstance(output, str) and isinstance(band, str)
                and isinstance(param, str)):
            raise LiveError("output, band and param must be strings")
        master = band == "" and param == "enable"
        with self.cond:
            self._require_up()
        with self.devlock:
            with self.lease.transient(("globals",)):
                try:
                    if master:
                        on = bool(_globaleq.coerce_value("enable", value))
                        ok = self.client.set_globaleq_enable(
                            output.strip().lower(), on)
                        sent: Any = on
                    else:
                        o, b, p = _globaleq.normalize(output, band, param)
                        sent = _globaleq.coerce_value(p, value)
                        ok = self.client.set_globaleq(o, b, p, sent)
                except ValueError as e:                # bad output/band/param/value
                    raise LiveError(str(e), status=400) from e
                except HelixError as e:
                    raise LiveError(f"the device would not set the Global EQ: "
                                    f"{e}", status=502) from e
                except OSError as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}",
                                    status=502) from e
                if not ok:
                    raise LiveError("the device did not confirm the Global EQ "
                                    "write", status=502)
        return {"output": output, "band": band, "param": param, "value": sent}

    # --- Controllers pane (81v.3) ---------------------------------------------------
    def _library(self):
        """The block library, loaded once. Supplies the param-name vocabulary
        the untranscode + view read model needs; tests inject a fixture one."""
        if self._lib is None:
            from helixgen.library import Library, default_library_path
            self._lib = Library(default_library_path())
        return self._lib

    def controllers(self) -> dict:
        """Current controller assignments on the live buffer, in a read-only
        shape the Controllers pane renders.

        Pure projection over the cached edit buffer — NO device I/O and NO new
        wire behaviour: it reuses the shipped read model (``untranscode`` ->
        ``.hsp`` -> ``view`` recipe), then rides each footswitch/pedal with its
        English label (spec / CLAUDE.md: never show a bare ``FS#``). Grouped as
        ``footswitches`` (bypass + param-toggle), ``expression`` (EXP sweeps),
        ``midi`` (CC bindings), and ``unknown`` (controls v1 can't re-author,
        kept visible rather than silently dropped)."""
        empty = {"footswitches": [], "expression": [], "midi": [], "unknown": []}
        with self.cond:
            eb = self._eb
        if not isinstance(eb, dict):
            return empty
        from helixgen import controllers as _controllers
        from helixgen import view as _view
        from helixgen.device import untranscode as _untranscode
        lib = self._library()
        body = _untranscode.sbepgsm_to_hsp(eb, library=lib)
        recipe = _view.view(body, lib)
        # English labels keyed by logical id (FS3, EXP1, …). Best-effort: an
        # unresolved device keeps the raw switch id rather than failing the pane.
        labels: Dict[str, str] = {}
        try:
            device_id = _view._device_id(body)
            labels = {r["id"]: r["english"]
                      for r in _controllers.controller_mapping(device_id)}
        except Exception:  # noqa: BLE001 — labels are a nicety, never a hard dep
            labels = {}
        return _group_controllers(recipe, labels)

    def commands(self) -> dict:
        """Command Center commands on the live buffer, in a read-only shape the
        Command Center pane renders (81v.4).

        Pure projection over the cached edit buffer — NO device I/O: it reuses
        the shipped reverse-transcode read model (``untranscode`` -> ``.hsp``)
        and flattens ``preset.commands`` (``bridge.hsp_commands``), then groups
        by source with English switch labels. Each source carries its commands'
        family, headline, Action and fields. Follows the device: the SSE stream
        re-installs the buffer on any ``/setEditBuffer`` echo, and the pane
        re-reads on focus / Refresh."""
        empty = {"sources": []}
        with self.cond:
            eb = self._eb
        if not isinstance(eb, dict):
            return empty
        from helixgen import view as _view
        from helixgen.device import bridge as _bridge
        from helixgen.device import paraminfo as _paraminfo
        from helixgen.device import untranscode as _untranscode
        lib = self._library()
        body = _untranscode.sbepgsm_to_hsp(eb, library=lib)
        cmds = _bridge.hsp_commands(body)
        if not cmds:
            return empty
        device_id = None
        labels: Dict[str, str] = {}
        try:
            from helixgen import controllers as _controllers
            device_id = _view._device_id(body)
            labels = {r["id"]: r["english"]
                      for r in _controllers.controller_mapping(device_id)}
        except Exception:  # noqa: BLE001 — labels are a nicety, never a hard dep
            labels = {}
        controls = _paraminfo.load_param_ui().get("controls", {})
        return _format_commands(cmds, device_id, controls, labels)

    # --- Tuner pane (81v.10): read-only live pitch readout --------------------
    def _note_tuner(self, args: Any) -> None:
        """Cache the latest pitch reading off a 2003 ``/dspEvent`` (the always-on
        detector). A non-pitch 2003 event (meter / trigger / heartbeat) decodes
        to ``None`` and is ignored. No emit — pitch is high-rate telemetry the
        Tuner pane polls, not a discrete change for the SSE log."""
        from helixgen.device import tuner as _tuner
        reading = _tuner.reading_from_event_args(args)
        if reading is None:
            return
        with self.cond:
            self._tuner = reading
            self._tuner_at = self._clock()

    def tuner_reading(self) -> dict:
        """The latest live pitch reading for the Tuner pane. Pure in-memory
        projection of the cached 2003 reading — NO device I/O, no lease. Reports
        no-signal when nothing has arrived yet or the stream has stalled (older
        than ``TUNER_STALE`` s), so a dead stream never shows a frozen note."""
        with self.cond:
            reading, at, up = self._tuner, self._tuner_at, self._connected
        if reading is None or not reading.signal or (self._clock() - at) > TUNER_STALE:
            return {"signal": False, "name": "—", "connected": up}
        return {"signal": True, "name": reading.name, "note": reading.note,
                "octave": reading.octave, "cents": reading.cents,
                "hz": round(reading.hz, 2), "connected": up}

    # --- Device Info pane (81v.11): read-only device identity -----------------
    # device_id is deliberately omitted: the pane shows helixgen_model (derived
    # from it), never the raw numeric id, so forwarding it is dead payload.
    _INFO_FIELDS = ("model", "helixgen_model", "serial", "firmware",
                    "firmware_build", "firmware_date", "sd_total_bytes",
                    "sd_available_bytes")

    def device_info(self) -> dict:
        """The device's identity / firmware / storage (``/ProductInfoGet``), for
        the read-only Device Info strip (81v.11). Read-only and lease-free over
        2002, exactly like :meth:`mixer_state` — identity lives outside the
        preset, so this does NOT touch the signal-flow state. The large ``raw``
        map is dropped; only the curated fields the strip shows are returned.
        Raises :class:`LiveError` (502) if the read fails."""
        with self.cond:
            self._require_up()
        with self.devlock:
            try:
                info = self.client.product_info()
            except (HelixError, OSError, ValueError) as e:
                raise LiveError(f"could not read device info: {e}",
                                status=502) from e
        return {k: info.get(k) for k in self._INFO_FIELDS}

    def _param_exists(self, eb: Any, path: int, block: int, pid: int) -> bool:
        cur = self._block_at(eb, path, block)
        if cur is None:
            raise LiveError("no such block on the loaded tone "
                            "(did the preset change?)", status=404)
        return any(p.get("pid") == pid for p in cur.get("params", []))

    def _rollback_controller(self, cid_: int) -> None:
        """Best-effort removal of a just-attached controller whose source bind
        failed. Never raises (the caller is already surfacing the real error);
        a failure here is logged, not masked over."""
        try:
            self.client.remove_controller(cid_)
        except Exception:  # noqa: BLE001 — the real error is the bind failure
            logger.warning("could not roll back sourceless controller %s after "
                           "a failed MIDI assign; it is harmless until Save", cid_)

    def assign_midi_cc(self, path: int, block: int, pid: int, cc: int, *,
                       rev: Optional[int] = None) -> dict:
        """Assign MIDI CC ``cc`` (0..127) as a live controller of param ``pid``
        on the block at grid slot ``block`` (DSP ``path``) — the device-native
        two-step (attach the param controller, then bind the CC source), heard
        at once, no reload (81v.3).

        ``rev`` is the structure revision the page was showing; the write is
        refused (409) if the tone changed since, so a controller never lands on
        a block that moved into the slot (mirrors :meth:`set_param`). The two
        granular writes are atomic from the page's view: if the source bind
        fails, the dangling sourceless controller is rolled back. Re-reads and
        returns fresh state. Volatile until Save."""
        pth, blk, p, c = int(path), int(block), int(pid), int(cc)
        if not 0 <= c <= 127:
            raise LiveError("a MIDI CC must be 0..127", status=400)
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                    self._require_rev(rev, None)
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                cur = self._block_at(eb, pth, blk)
                if cur is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                if cur.get("category") in ("input", "output", "split", "join"):
                    raise LiveError("a controller attaches to a processing "
                                    "block's param, not an endpoint / routing "
                                    "node.", status=400)
                prm = next((pr for pr in cur.get("params", [])
                            if pr.get("pid") == p), None)
                if prm is None:
                    raise LiveError(f"block has no param {p}", status=400)
                if prm.get("internal"):
                    raise LiveError("that param has no editor control — it is "
                                    "plumbing, not a controllable knob.",
                                    status=400)
                try:
                    cid_ = self.client.attach_param_controller(pth, blk, p)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device controller attach failed: {e}",
                                    status=502) from e
                try:
                    ok = self.client.add_midi_cc_source(cid_, c)
                except (HelixError, OSError) as e:
                    self._rollback_controller(cid_)
                    self._set_connected(False)
                    raise LiveError(f"device MIDI source bind failed: {e}",
                                    status=502) from e
                if not ok:
                    self._rollback_controller(cid_)
                    raise LiveError("the device did not confirm the MIDI "
                                    "assignment", status=502)
        finally:
            self.lease.done()
        self.load()
        return self.public_state()

    def clear_param_controllers(self, path: int, block: int, pid: int, *,
                                rev: Optional[int] = None) -> dict:
        """Remove every controller assigned to param ``pid`` on the block at
        grid slot ``block`` (DSP ``path``), live — the per-param Clear
        Controllers (81v.3). A no-op (no error) when the param carries none.
        ``rev`` refuses a stale request (409), as :meth:`assign_midi_cc` does.
        Re-reads and returns fresh state. Volatile until Save."""
        pth, blk, p = int(path), int(block), int(pid)
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                    self._require_rev(rev, None)
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                if not self._param_exists(eb, pth, blk, p):
                    raise LiveError(f"block has no param {p}", status=400)
                for cid_ in _controller_cids_for(eb, pth, blk, p):
                    try:
                        ok = self.client.remove_controller(cid_)
                    except (HelixError, OSError) as e:
                        self._set_connected(False)
                        raise LiveError(f"device controller remove failed: {e}",
                                        status=502) from e
                    if not ok:
                        raise LiveError("the device did not confirm clearing "
                                        "the controller", status=502)
        finally:
            self.lease.done()
        self.load()
        return self.public_state()

    def set_snapshot_control(self, path: int, block: int, pid: int, on: Any, *,
                             rev: Optional[int] = None) -> dict:
        """Turn per-snapshot control ("Snapshot Ctrl") on/off for param ``pid``
        on the block at grid slot ``block`` (DSP ``path``), live (81v.2).

        ON seeds the param's CURRENT value into every snapshot and adds the
        snapshot target (``assign_snap_param``); OFF removes the target
        (``remove_snapshot_target``). Idempotent — a toggle that already matches
        the device state is a no-op, never an error. ``rev`` refuses a stale
        request (409), as :meth:`assign_midi_cc` does. Re-reads and returns
        fresh state. Volatile until Save."""
        pth, blk, p, want = int(path), int(block), int(pid), bool(on)
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                    self._require_rev(rev, None)
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                cur = self._block_at(eb, pth, blk)
                if cur is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                if cur.get("category") in ("input", "output", "split", "join"):
                    raise LiveError("snapshot control is per processing-block "
                                    "param, not an endpoint / routing node.",
                                    status=400)
                prm = next((pr for pr in cur.get("params", [])
                            if pr.get("pid") == p), None)
                if prm is None:
                    raise LiveError(f"block has no param {p}", status=400)
                if prm.get("internal"):
                    raise LiveError("that param has no editor control — it is "
                                    "plumbing, not a controllable knob.",
                                    status=400)
                tid = self.client.snapshot_param_target(pth, blk, p, doc=eb)
                if want and tid is None:
                    val = prm.get("value")
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        val = 1.0 if val else 0.0
                    try:
                        self.client.assign_snap_param(pth, blk, p, float(val))
                    except (HelixError, OSError) as e:
                        self._set_connected(False)
                        raise LiveError(f"device snapshot-control assign failed: "
                                        f"{e}", status=502) from e
                elif not want and tid is not None:
                    try:
                        ok = self.client.remove_snapshot_target(tid)
                    except (HelixError, OSError) as e:
                        self._set_connected(False)
                        raise LiveError(f"device snapshot-control remove failed: "
                                        f"{e}", status=502) from e
                    if not ok:
                        raise LiveError("the device did not confirm removing "
                                        "snapshot control", status=502)
                # want == (tid is not None): already in the desired state → no-op
        finally:
            self.lease.done()
        self.load()
        return self.public_state()

    def source_catalog(self) -> dict:
        """The assignable local controller sources (footswitches, expression
        pedals, toe switch) for the Controllers pane's source picker —
        identifier, canonical name and kind. Static, no device I/O (81v.3)."""
        from helixgen import controllers as _ctl
        return {"sources": [{"id": r["id"], "name": r["name"], "kind": r["kind"]}
                            for r in _ctl.controller_mapping("stadium_xl")]}

    def assign_local_source(self, path: int, block: int, pid: int, source: str,
                            *, rev: Optional[int] = None) -> dict:
        """Assign a LOCAL (non-MIDI) controller source — an expression pedal or
        footswitch — to param ``pid`` on the block at grid slot ``block`` (DSP
        ``path``), live (81v.3, hgc-r24).

        Mirrors the Stadium app's Controller Assign: if the param already has a
        controller its cid is REUSED (the app only re-sources, never re-attaches
        — so no orphan controller piles up); otherwise a controller is attached
        first. Then the local source is bound (``/ControllerSourceSet``) and the
        behaviour set (``/CidBehaviorSet``: continuous for an EXP pedal, latching
        for a footswitch). ``rev`` refuses a stale request (409), as
        :meth:`assign_midi_cc` does. Re-reads and returns fresh state. Volatile
        until Save."""
        pth, blk, p = int(path), int(block), int(pid)
        locl, ctxt, behv = _local_source_wire(source)   # raises 400 on unknown
        self._ensure_lease(None)
        try:
            with self.devlock:
                with self.cond:
                    self._require_up()
                    self._require_rev(rev, None)
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device read failed: {e}", status=502) from e
                cur = self._block_at(eb, pth, blk)
                if cur is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                if cur.get("category") in ("input", "output", "split", "join"):
                    raise LiveError("a controller attaches to a processing "
                                    "block's param, not an endpoint / routing "
                                    "node.", status=400)
                prm = next((pr for pr in cur.get("params", [])
                            if pr.get("pid") == p), None)
                if prm is None:
                    raise LiveError(f"block has no param {p}", status=400)
                if prm.get("internal"):
                    raise LiveError("that param has no editor control — it is "
                                    "plumbing, not a controllable knob.",
                                    status=400)
                existing = _controller_cids_for(eb, pth, blk, p)
                attached = not existing
                try:
                    cid_ = existing[0] if existing \
                        else self.client.attach_param_controller(pth, blk, p)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device controller attach failed: {e}",
                                    status=502) from e
                try:
                    src_ok = self.client.set_controller_source(cid_, locl, ctxt)
                    behv_ok = (self.client.set_cid_behavior(cid_, behv)
                               if src_ok else False)
                except (HelixError, OSError) as e:
                    if attached:
                        self._rollback_controller(cid_)
                    self._set_connected(False)
                    raise LiveError(f"device source bind failed: {e}",
                                    status=502) from e
                if not src_ok:
                    # nothing bound — a fresh attach is removed whole; a reuse
                    # left the existing controller untouched.
                    if attached:
                        self._rollback_controller(cid_)
                    raise LiveError("the device did not bind the controller "
                                    "source", status=502)
                if not behv_ok:
                    # the source IS bound; only the behaviour didn't confirm. A
                    # fresh attach rolls back whole; a reuse leaves the rebound
                    # source under its previous behaviour — say so precisely, a
                    # re-assign heals it (idempotent).
                    if attached:
                        self._rollback_controller(cid_)
                    raise LiveError("the controller source was set but the "
                                    "device did not confirm its behaviour; "
                                    "re-assign to finish", status=502)
        finally:
            self.lease.done()
        self.load()
        return self.public_state()

    # --- 2001 events ----------------------------------------------------------------
    def _own_echo(self, seq: int, key: tuple, value: Any) -> Optional[str]:
        """Is this event the echo of one of OUR writes?

        ``None``     — no: someone else's change.
        ``"stale"``  — the echo of an own write that a NEWER own write to the
                       same control has since replaced: drop it, or a late echo
                       drags the control backwards mid-gesture.
        ``"latest"`` — the echo of our newest write to that control. The
                       stream is the device's own ordering, so this is what the
                       device holds NOW — apply it like any event (it only
                       changes anything if a foreign change slipped in between
                       our ack and here).

        A remembered request id is consumed by its echo: ids are small
        counters that every helixgen process starts from the same number."""
        own = self._own.get(seq)
        if own is None or own[0] != key:
            return None
        if isinstance(value, bool) or isinstance(own[1], bool):
            same = bool(value) == bool(own[1])
        else:
            same = _close(float(value), float(own[1]))
        if not same:
            return None
        del self._own[seq]
        if self._latest.get(key) == seq:
            del self._latest[key]
            return "latest"
        return "stale"

    def _apply(self, action: tuple) -> None:
        with self.cond:
            kind = action[0]
            if kind == "param":
                target = ("param", action[2], action[3], action[4])
            elif kind == "bypass":
                target = ("bypass", action[2], action[3])
            else:
                target = None
            if target is not None and target == self._writing:
                self._deferred.append(action)   # judged once the write's ack is in
                return
            self._apply_locked(action)

    def _apply_locked(self, action: tuple) -> None:
        kind = action[0]
        if kind == "param":
            _, seq, path, block, pid, value = action
            key = (path, block, pid)
            if self._own_echo(seq, ("param",) + key, value) == "stale":
                return
            p = self._params.get(key)
            if p is None or _close(p["value"], value):
                return
            p["value"] = value
            self._emit({"t": "param", "path": path, "block": block,
                        "pid": pid, "value": value})
        elif kind == "bypass":
            _, seq, path, block, on = action
            key = (path, block)
            if self._own_echo(seq, ("bypass",) + key, on) == "stale":
                return
            b = self._blocks.get(key)
            if b is None or b["enabled"] == on:
                return
            b["enabled"] = on
            self._emit({"t": "bypass", "path": path, "block": block,
                        "enabled": on})
        elif kind == "dirty":
            self._set_dirty(action[1])

    def _apply_snapshot_batch(self, index: Optional[int],
                              deltas: List[tuple]) -> bool:
        """Apply a snapshot recall's ``/setBatchedParamVals`` deltas in place:
        move the active-snapshot indicator to ``index`` (when known) and set
        each changed param's value, with NO device read and NO page re-render
        (the page gets ``meta`` + ``param`` messages, not a fresh structure).

        Returns ``False`` — the caller then falls back to a full re-read — when
        ``index`` is out of range or a ``sub == 0`` delta names a control this
        page does not model, so a switch never leaves stale values on screen.
        Deltas with ``sub != 0`` (a dual-cab second model or harness param,
        hgc-5gh — no known live-write address) are skipped, not a failure."""
        with self.cond:
            resolved = []
            for dsp, block, sub, pid, value in deltas:
                if sub != 0:
                    continue
                p = self._params.get((dsp, block, pid))
                if p is None:
                    return False
                resolved.append((p, dsp, block, pid, value))
            if index is not None:
                if not (isinstance(index, int) and 0 <= index <= 7):
                    return False
                if self._chain.get("snapshot") != index:
                    self._chain["snapshot"] = index
                    self._emit_meta()
            for p, dsp, block, pid, value in resolved:
                if _close(p["value"], value):
                    continue
                p["value"] = value
                self._emit({"t": "param", "path": dsp, "block": block,
                            "pid": pid, "value": value})
        return True

    def _seq_gap(self, port: int, seq: Optional[int]) -> bool:
        """Record this frame's 2001 header sequence and report whether it skipped
        one. The first frame on a stream (no prior seq) is never a gap; a normal
        consecutive frame (``seq == last + 1``) is not; a uint32 wraparound
        (``0xFFFFFFFF`` -> ``0``) is continuity. Updating ``_seq`` to the latest
        on every frame makes a jump fire at most once: the next consecutive
        frame is measured against the new value, not the pre-gap one."""
        if seq is None:
            return False
        last = self._seq.get(port)
        self._seq[port] = seq
        return last is not None and (seq - last) % _SEQ_MOD != 1

    def handle_events(self, events: list) -> None:
        """Digest one poll batch from the device's PUB stream(s). Value changes
        apply in place; however many structural events a batch holds, at most
        ONE re-read follows (the slower the device, the more they coalesce).
        Never raises: a failed re-read marks the session disconnected."""
        if events:
            self._last_event = self._clock()
        blob = ref = batch = None
        snap_index: Optional[int] = None
        reread = snapshot = identity = False
        late: List[tuple] = []
        for ev in events:
            port = getattr(ev, "port", 2001)
            if port != 2001:
                if port == 2003:
                    self._note_tuner(getattr(ev, "args", None))
                continue
            if self._seq_gap(port, getattr(ev, "seq", None)):
                # frames were dropped (the SUB socket reconnected silently, or a
                # frame was unparseable) — whatever changed while we were deaf is
                # not in our state. Reconcile with the same full read the
                # disconnect-return uses, rather than wait for a per-control event.
                reread = identity = True
            action = decode_event(ev.addr, ev.args)
            if action is None:
                continue
            kind = action[0]
            if kind == "editbuffer":
                blob, ref, reread, late = action[1], None, False, []
                snapshot, batch, snap_index = False, None, None
            elif kind == "presetref":
                ref = (action[1], action[2])
            elif kind == "snapshot":
                snapshot = True
                snap_index = action[1]
            elif kind == "batch":
                batch = action[1]
            elif kind == "reread":
                straggler = (action[1] == "/setBatchedParamVals" and
                             self._clock() - self._snapshot_reread_at < SNAPSHOT_SETTLE)
                if not straggler:
                    reread = True
            else:
                late.append(action)

        installed = False
        if blob is not None:
            try:
                eb = decode_blob(blob)
                preset = ({"cid": ref[0], "name": ref[1]} if ref is not None
                          else {"cid": None, "name": None})
                self._install(eb, preset, awaiting_ref=ref is None)
                installed = True
            except Exception:  # noqa: BLE001 — an undecodable push: go read it
                reread = identity = True
        if ref is not None and not installed:
            with self.cond:
                same = self._awaiting_ref or ref[0] == self._preset.get("cid")
                if same:
                    self._preset = {"cid": ref[0], "name": ref[1]}
                    self._awaiting_ref = False
                    self._emit_meta()
            if not same:
                reread = identity = True   # another preset, and its blob never came
        # A snapshot recall carries its param deltas (/setBatchedParamVals):
        # apply them in place — no device read, no page re-render (hgc-9qd).
        # Fall back to a full re-read when the deltas are missing, could not be
        # decoded, or name a control we do not model, so a switch never shows
        # stale values. (Snapshot-bypass state is NOT in the batch and still
        # rides the re-read path — decoding it needs a hardware capture.)
        if blob is None and (snapshot or batch is not None):
            # Block-enable/bypass is NOT a param, so it can never ride inside the
            # /setBatchedParamVals batch ([dsp,block,sub,pid,value]) — a snapshot
            # that flips a block on/off sends that as a SEPARATE event (a
            # ("bypass", ...) action here). The in-place path drops every non-
            # "dirty" late action, so take it only when the batch applies cleanly
            # AND the poll carries nothing but params + an optional dirty flag;
            # anything else (a bypass, a straggler param) forces the proven
            # full re-read rather than a silent stale switch.
            droppable = all(a[0] == "dirty" for a in late)
            if (droppable and batch is not None and self._apply_snapshot_batch(
                    snap_index if snapshot else None, batch)):
                # the deltas ARE the post-switch values; value events earlier
                # in this poll are pre-switch and must not land on top
                late = [a for a in late if a[0] == "dirty"]
            else:
                reread = True
        if reread:
            self._reread_quiet(identity=identity)
            if snapshot:
                self._snapshot_reread_at = self._clock()
            # every value event in this batch was emitted BEFORE that read, so
            # the read already holds it — replaying one would put a pre-switch
            # value back on top of the new state
            late = [a for a in late if a[0] == "dirty"]
        for action in late:
            self._apply(action)

    # --- housekeeping ---------------------------------------------------------------
    def tick(self) -> None:
        """Called periodically by the server loop: release an idle write lease,
        notice a silent device, retry a lost connection."""
        self.lease.tick()
        with self.cond:
            pending = self._need_reread and self._connected
        if pending:
            self._reread_quiet()
        now = self._clock()
        heard = self._last_event is not None and now - self._last_event <= DEAD_AFTER
        if (self.liveness and self._connected and self._last_event is not None
                and not heard):
            self._set_connected(False)
        if (not self._connected and now - self._last_retry >= RETRY_EVERY
                and (heard or not self.liveness)):
            self._last_retry = now
            self._reread_quiet()
