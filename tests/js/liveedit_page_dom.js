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
  set textContent(v) { this.children.forEach((c) => c.parent = null); this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.L[t] = this.L[t] || []).push(f); }
  removeEventListener(t, f) { this.L[t] = (this.L[t] || []).filter((x) => x !== f); }
  fire(t, ev = {}) { ev.preventDefault = ev.preventDefault || (() => {}); ev.type = t; (this.L[t] || []).slice().forEach((f) => f(ev)); return ev; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  focus() { El.active = this; } setPointerCapture() {} get offsetWidth() { return 0; }
  get connected() { let n = this; while (n.parent) n = n.parent; return n === El.body; }
  get value() { return this._value; }
  set value(v) { if (this.tagName === "select") { const o = this.children.find((c) => String(c._value) === String(v)); this._value = o ? String(v) : ""; } else this._value = v; }
  find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.find && c.find(pred, out)); return out; }
}
function boot() {
  const ids = {}; El.body = new El("body");
  ["led", "preset", "snap", "undo", "redo", "reread", "save", "banner", "rig", "toasts"].forEach((i) => { ids[i] = new El("div"); El.body.append(ids[i]); });
  const document = { get body2(){return El.body}, getElementById: (i) => ids[i], createElement: (t) => new El(t), createElementNS: (n, t) => new El(t),
    createTextNode: (s) => { const t = new El("#text"); t._text = s; return t; }, body: El.body, title: "" };
  const win = {}; const posts = []; const toasts = ids.toasts; let es = null;
  const fetch = (url, opt) => new Promise((resolve, reject) => { posts.push({ url, body: JSON.parse(opt.body), reject,
    reply: (status, j) => resolve({ ok: status >= 200 && status < 300, status, json: async () => j }) }); });
  class EventSource { constructor() { es = this; } }
  const src = fs.readFileSync(PAGE, "utf8").split("<script>")[1].split("</script>")[0];
  const api = new Function("document", "addEventListener", "fetch", "EventSource", "setTimeout", "console",
    src + "\n;return {P,B,pending,onMsg,undo,redo,get undoS(){return undoS},get redoS(){return redoS},get dragging(){return dragging},get down(){return down},drained,get inflight(){return inflight}};")(
    document, (t, f) => (win[t] = f), fetch, EventSource, () => {}, console);
  const tick = () => new Promise((r) => setImmediate(r));
  const sse = (m) => es.onmessage({ data: JSON.stringify(m) });
  const knobOf = (key) => { const root = ids.rig.find((e) => e.dataset && e.dataset.k === key)[0]; return { root, svg: root.children[0], val: root.children[1] }; };
  return { api, ids, win, posts, sse, tick, knobOf, toasts, document, body: El.body };
}
const P_ = (o) => Object.assign({ pid: 1, name: "Drive", label: "Drive", type: "f", ctl: "knob", value: 0.5, min: 0, max: 1, default: 0.5,
  unit: "", scale: 10, display_min: null, display_max: null, labels: null, internal: false, snap: false }, o);
const Blk = (o, params) => Object.assign({ path: 0, block: 2, row: 0, model: "HD2_X", name: "X", category: "amp", enabled: true, xlink: null, params }, o);
const State = (blocks, o) => Object.assign({ t: "state", rev: 1, version: 1, preset: { cid: 7, name: "T" }, snapshot: 0, snapshot_name: "S1", snapshots: ["S1"],
  dirty: false, connected: true, paths: [{ path: 0, segments: blocks.map((b) => ({ kind: "block", block: b })), extra: [] }] }, o);
module.exports = { boot, P_, Blk, State, El };
