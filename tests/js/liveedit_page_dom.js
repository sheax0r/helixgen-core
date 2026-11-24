// Minimal fake DOM + fake fetch/EventSource for the `device live` page (hgc-58a).
// Loads the REAL <script> of page.html unmodified, so the scenarios in
// liveedit_page_scenarios.js exercise the shipped code. Driven by
// tests/test_liveedit_page.py (skipped when node is not installed).
const fs = require("fs");
const PAGE = require("path").join(__dirname, "..", "..", "src", "helixgen", "liveedit", "page.html");
class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.style = {};
    this._cls = new Set(); this.L = {}; this.parent = null; this._text = ""; this.hidden = false; this._value = ""; }
  get className() { return [...this._cls].join(" "); }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() { const s = this._cls; return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
    toggle: (c, f) => { if (f === undefined) f = !s.has(c); f ? s.add(c) : s.delete(c); return f; } }; }
  append(...ns) { for (let n of ns) { if (typeof n === "string") { const t = new El("#text"); t._text = n; n = t; } n.parent = this; this.children.push(n); } }
  get lastChild() { return this.children[this.children.length - 1]; }
  // Real browsers fire blur when the focused element is removed from the DOM.
  _blurOnTeardown() { const a = El.active; if (!a) return;
    for (let n = a; n; n = n.parent) if (n === this) { El.active = null; if (a.onblur) a.onblur({ preventDefault() {} }); return; } }
  set textContent(v) { this.children.forEach((c) => { c._blurOnTeardown(); c.parent = null; }); this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.L[t] = this.L[t] || []).push(f); }
  removeEventListener(t, f) { this.L[t] = (this.L[t] || []).filter((x) => x !== f); }
  fire(t, ev = {}) { ev.preventDefault = ev.preventDefault || (() => {}); ev.type = t; (this.L[t] || []).slice().forEach((f) => f(ev)); return ev; }
  remove() { this._blurOnTeardown(); if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  focus() { El.active = this; } select() {} setPointerCapture() {} scrollIntoView() {} get offsetWidth() { return 0; }
  get connected() { let n = this; while (n.parent) n = n.parent; return n === El.body; }
  get value() { return this._value; }
  set value(v) { if (this.tagName === "select") { const o = this.children.find((c) => String(c._value) === String(v)); this._value = o ? String(v) : ""; } else this._value = v; }
  find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.find && c.find(pred, out)); return out; }
  // Minimal CSS selector support (real browsers have these; the page uses them).
  querySelectorAll(sel) { return _qsa(this, sel); }
  querySelector(sel) { return _qsa(this, sel)[0] || null; }
  matches(sel) { return String(sel).split(",").some((s) => _matchCompound(this, _parseCompound(s.trim()))); }
}
function _parseCompound(c) {
  const o = { tag: null, id: null, cls: [], attrs: [] };
  (c.match(/[.#]?[\w-]+|\[[^\]]+\]/g) || []).forEach((t) => {
    if (t[0] === ".") o.cls.push(t.slice(1));
    else if (t[0] === "#") o.id = t.slice(1);
    else if (t[0] === "[") { const m = /\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/.exec(t); if (m) o.attrs.push([m[1], m[2]]); }
    else o.tag = t;
  });
  return o;
}
function _dsKey(a) { return a.replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
function _matchCompound(el, c) {
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  if (c.cls.some((cl) => !el._cls.has(cl))) return false;
  for (const [k, v] of c.attrs) {
    const av = k.indexOf("data-") === 0 ? (el.dataset ? el.dataset[_dsKey(k)] : undefined) : el.getAttribute(k);
    if (v == null) { if (av == null) return false; } else if (String(av) !== v) return false;
  }
  return true;
}
function _qsa(root, sel) {
  const chain = String(sel).trim().split(/\s+/).map(_parseCompound);
  const last = chain[chain.length - 1];
  const hits = root.find((e) => e !== root && _matchCompound(e, last));
  if (chain.length === 1) return hits;
  const anc = chain.slice(0, -1);
  return hits.filter((e) => {
    let i = anc.length - 1;
    for (let n = e.parent; n && n !== root.parent; n = n.parent) { if (i >= 0 && _matchCompound(n, anc[i])) i--; if (i < 0) break; }
    return i < 0;
  });
}
function boot() {
  const ids = {}; El.body = new El("body");
  ["led", "preset", "snap", "undo", "redo", "reread", "save", "load", "banner", "snaprow", "rig", "toasts",
   "adjsearch", "adjlist", "adjhint", "libtoggle", "drawer", "drawerbody", "tabs",
   "devicepane", "devtabs", "devnote", "devbody", "devrefresh",
   "ctlpane", "ctlbody", "ctlrefresh", "ctlassign",
   "mixerpane", "mxtabs", "mxbody", "mxrefresh",
   "eqpane", "eqtabs", "eqnote", "eqbody", "eqrefresh",
   "cmdpane", "cmdbody", "cmdrefresh", "cmdassign",
   "tunerpane", "tunerbody", "tuner-note", "tuner-needle", "tuner-pointer",
   "tuner-readout", "tuner-cents", "tuner-hz", "tuner-hint",
   "deviceinfopane", "dinfobody", "dinforefresh", "dinfonote",
   "snappanel", "snapbody", "snapclose"].forEach((i) => { ids[i] = new El("div"); El.body.append(ids[i]); });
  const document = { get body2(){return El.body}, getElementById: (i) => ids[i] || El.body.find((e) => e.id === i)[0] || null, createElement: (t) => new El(t), createElementNS: (n, t) => new El(t),
    createTextNode: (s) => { const t = new El("#text"); t._text = s; return t; }, body: El.body, title: "" };
  const win = {}; const posts = []; const toasts = ids.toasts; let es = null;
  const fetch = (url, opt) => new Promise((resolve, reject) => { posts.push({ url, body: (opt && opt.body) ? JSON.parse(opt.body) : null, reject,
    reply: (status, j) => resolve({ ok: status >= 200 && status < 300, status, json: async () => j }) }); });
  class EventSource { constructor() { es = this; } }
  // Fake interval timers (the Tuner pane polls via setInterval): record the
  // callback so a scenario can fire it deterministically; no real timer.
  const intervals = new Map(); let ivId = 0;
  const setInterval_ = (fn) => { const id = ++ivId; intervals.set(id, fn); return id; };
  const clearInterval_ = (id) => { intervals.delete(id); };
  const src = fs.readFileSync(PAGE, "utf8").split("<script>")[1].split("</script>")[0];
  const api = new Function("document", "addEventListener", "fetch", "EventSource", "setTimeout", "setInterval", "clearInterval", "console",
    src + "\n;return {P,B,pending,onMsg,undo,redo,get undoS(){return undoS},get redoS(){return redoS},get dragging(){return dragging},get down(){return down},drained,get inflight(){return inflight}};")(
    document, (t, f) => (win[t] = f), fetch, EventSource, () => {}, setInterval_, clearInterval_, console);
  const tick = () => new Promise((r) => setImmediate(r));
  const sse = (m) => es.onmessage({ data: JSON.stringify(m) });
  const knobOf = (key) => { const root = ids.rig.find((e) => e.dataset && e.dataset.k === key)[0]; return { root, svg: root.children[0], val: root.children[1] }; };
  const tileOf = (key) => ids.rig.find((e) => e._cls.has("tile") && e.dataset && e.dataset.b === key)[0] || null;
  return { api, ids, win, posts, sse, tick, knobOf, tileOf, toasts, document, body: El.body, intervals };
}
const P_ = (o) => Object.assign({ pid: 1, name: "Drive", label: "Drive", type: "f", ctl: "knob", value: 0.5, min: 0, max: 1, default: 0.5,
  unit: "", scale: 10, display_min: null, display_max: null, labels: null, internal: false, snap: false }, o);
const Blk = (o, params) => Object.assign({ path: 0, block: 2, row: 0, model: "HD2_X", name: "X", category: "amp", enabled: true, xlink: null, params }, o);
const State = (blocks, o) => Object.assign({ t: "state", rev: 1, version: 1, preset: { cid: 7, name: "T" }, snapshot: 0, snapshot_name: "S1", snapshots: ["S1"],
  dirty: false, connected: true, paths: [{ path: 0, segments: blocks.map((b) => ({ kind: "block", block: b })), extra: [] }] }, o);
module.exports = { boot, P_, Blk, State, El };
