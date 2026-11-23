// Behavioural scenarios for the `device live` page (hgc-58a): the write queue,
// undo/redo, pointer handling and the page<->server contract. Each one is a
// failure an adversarial review reproduced against an earlier version.
const { boot, P_, Blk, State } = require("./liveedit_page_dom.js");
const K = "0/2/1";
let fails = 0;
const ok = (name, cond, detail) => { console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : "   <-- " + detail)); if (!cond) fails++; };
const drag = (k, id, ...ys) => { k.svg.fire("pointerdown", { button: 0, clientY: ys[0], pointerId: id });
  ys.slice(1).forEach((y) => k.svg.fire("pointermove", { clientY: y, pointerId: id })); };
(async () => {
  { // S1: nudge, re-render arrives while the write is out, server refuses the stale write
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.knobOf(K).root.fire("keydown", { key: "ArrowUp", shiftKey: true });
    ok("POST carries the structure revision", x.posts[0].body.rev === 1, JSON.stringify(x.posts[0].body));
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { rev: 2, version: 2 }));
    x.posts[0].reply(409, { error: "the tone changed", value: 0.5 }); await x.tick();
    ok("S1 page shows the device value after a refused stale write", x.api.P.get(K).p.value === 0.5 && x.knobOf(K).val.textContent === "5.0", x.knobOf(K).val.textContent);
    ok("S1 the change that never happened left no undo entry", x.api.undoS.length === 0, x.api.undoS.length);
  }
  { // S1b: same race, but the server DID apply it (old server / same rev): the own-client message must not be dropped
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.knobOf(K).root.fire("keydown", { key: "ArrowUp", shiftKey: true });
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { rev: 2, version: 2 }));
    x.sse({ t: "param", path: 0, block: 2, pid: 1, value: 0.6, client: x.posts[0].body.client });
    x.posts[0].reply(200, { ok: true, value: 0.6 }); await x.tick();
    ok("S1b own-client message after a re-render is applied", x.api.P.get(K).p.value === 0.6, x.api.P.get(K).p.value);
  }
  { // S2: release queued behind an in-flight write, then a re-render
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const k = x.knobOf(K); drag(k, 1, 300, 280, 220); k.svg.fire("pointerup", { pointerId: 1 });
    ok("S2 one write in flight, the release pending", x.posts.length === 1 && x.api.pending.size === 1, x.posts.length + "/" + x.api.pending.size);
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { rev: 2, version: 2 }));
    x.posts[0].reply(409, { error: "the tone changed", value: 0.5 }); await x.tick();
    ok("S2 page shows the device value, nothing pending, no phantom undo",
      x.api.P.get(K).p.value === 0.5 && x.api.pending.size === 0 && x.api.undoS.length === 0 && x.posts.length === 1,
      JSON.stringify([x.api.P.get(K).p.value, x.api.pending.size, x.api.undoS.length, x.posts.length]));
  }
  { // S3: plain click
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])])); const k = x.knobOf(K);
    k.svg.fire("pointerdown", { button: 0, clientY: 300, pointerId: 1 }); k.svg.fire("pointerup", { pointerId: 1 });
    ok("S3 a click that moves nothing writes nothing and records nothing", x.posts.length === 0 && x.api.undoS.length === 0, x.posts.length);
  }
  { // S4: float32 value + horizontal-only jitter
    const f32 = Math.fround(0.74); const x = boot(); x.sse(State([Blk({}, [P_({ value: f32 })])])); const k = x.knobOf(K);
    drag(k, 1, 300, 300, 300); k.svg.fire("pointerup", { pointerId: 1 });
    ok("S4 jitter with no vertical travel on a float32 value is not a change", x.posts.length === 0 && x.api.undoS.length === 0 && x.api.P.get(K).p.value === f32, x.posts.length);
  }
  { // S5: Shift pressed mid-drag
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.2 })])])); const k = x.knobOf(K);
    k.svg.fire("pointerdown", { button: 0, clientY: 300, pointerId: 1 });
    k.svg.fire("pointermove", { clientY: 200, pointerId: 1 });
    const coarse = x.api.P.get(K).p.value;
    k.svg.fire("pointermove", { clientY: 199, pointerId: 1, shiftKey: true });
    const fine = x.api.P.get(K).p.value;
    ok("S5 Shift mid-drag continues from where the knob is", Math.abs(coarse - 0.7) < 1e-9 && fine > coarse && fine - coarse < 0.01, coarse + " -> " + fine);
    k.svg.fire("pointerup", { pointerId: 1 });
    ok("S5 the whole drag is ONE undo step", x.api.undoS.length === 1 && x.api.undoS[0].before === 0.2, JSON.stringify(x.api.undoS));
  }
  { // S6: disconnected
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])])); const k = x.knobOf(K);
    k.root.fire("keydown", { key: "ArrowUp" }); x.posts[0].reply(200, { ok: true, value: x.posts[0].body.value }); await x.tick();
    x.sse({ t: "meta", dirty: true, connected: false, preset: { cid: 7, name: "T" }, snapshot: 0, snapshot_name: "S1" });
    const before = x.api.P.get(K).p.value, posts = x.posts.length;
    x.knobOf(K).root.fire("keydown", { key: "ArrowUp", shiftKey: true });
    x.knobOf(K).svg.fire("pointerdown", { button: 0, clientY: 300, pointerId: 1 });
    x.api.undo();
    ok("S6 while disconnected: rig inert, no key/pointer/undo write, value untouched",
      x.ids.rig.inert === true && x.posts.length === posts && x.api.P.get(K).p.value === before && x.api.undoS.length === 1 && x.api.dragging === null,
      JSON.stringify([x.ids.rig.inert, x.posts.length - posts, x.api.P.get(K).p.value, x.api.undoS.length]));
  }
  { // S7: failed write
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])])); const k = x.knobOf(K);
    k.root.fire("keydown", { key: "End" }); x.posts[0].reply(502, { error: "the device did not acknowledge the write", value: 0.5 }); await x.tick();
    ok("S7 a failed write reverts the control AND removes its undo entry", x.api.P.get(K).p.value === 0.5 && x.api.undoS.length === 0, JSON.stringify([x.api.P.get(K).p.value, x.api.undoS.length]));
    k.root.fire("keydown", { key: "End" }); x.posts[1].reply(503, { error: "disconnected" }); await x.tick();
    ok("S7b an error with NO value still reverts to the last device value", x.api.P.get(K).p.value === 0.5, x.api.P.get(K).p.value);
  }
  { // S8: key auto-repeat
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.2 })])])); const k = x.knobOf(K);
    for (let i = 0; i < 40; i++) k.root.fire("keydown", { key: "ArrowUp" });
    ok("S8 40 key repeats are one undo step", x.api.undoS.length === 1 && x.api.undoS[0].before === 0.2, x.api.undoS.length);
    k.root.fire("keydown", { key: "ArrowLeft", metaKey: true });
    ok("S8b Cmd/Ctrl/Alt+arrow is left to the browser", x.api.undoS.length === 1, x.api.undoS.length);
  }
  { // S9: capture lost without pointerup
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])])); const k = x.knobOf(K);
    drag(k, 1, 300, 290); k.svg.fire("lostpointercapture", { pointerId: 1 });
    const v = x.api.P.get(K).p.value;
    k.svg.fire("pointermove", { clientY: 100, pointerId: 1 });
    ok("S9 losing capture ends the drag; later hover does not write", x.api.dragging === null && x.api.P.get(K).p.value === v, JSON.stringify([x.api.dragging, x.api.P.get(K).p.value, v]));
  }
  { // S10: model swapped in place on the hardware (same preset, same snapshot)
    const x = boot(); x.sse(State([Blk({ model: "HD2_AmpA" }, [P_({ value: 0.3 })])]));
    x.knobOf(K).root.fire("keydown", { key: "End" }); x.posts[0].reply(200, { ok: true, value: 1 }); await x.tick();
    x.sse(State([Blk({ model: "HD2_DelayB", category: "delay" }, [P_({ name: "Time", label: "Time", value: 0.3 })])], { rev: 2, version: 2 }));
    const n = x.posts.length; x.api.undo(); await x.tick();
    ok("S10 history does not survive a model swap", x.api.undoS.length === 0 && x.posts.length === n, JSON.stringify([x.api.undoS.length, x.posts.length - n]));
  }
  { // S11b: second pointer
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])])); const k = x.knobOf(K);
    k.svg.fire("pointerdown", { button: 0, clientY: 300, pointerId: 1 });
    k.svg.fire("pointerdown", { button: 0, clientY: 100, pointerId: 2 });
    k.svg.fire("pointermove", { clientY: 100, pointerId: 2 });
    ok("S11b a second finger does not move the knob", x.api.P.get(K).p.value === 0.5 && x.posts.length === 0, x.api.P.get(K).p.value);
    k.svg.fire("pointerup", { pointerId: 2 });
    ok("S11c the other pointer's release does not end the drag", x.api.dragging === K, x.api.dragging);
    k.svg.fire("pointerup", { pointerId: 1 });
  }
  { // S15: remote message vs a queued local change
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 }), P_({ pid: 2, name: "Bass", label: "Bass", value: 0.5 })])]));
    x.knobOf(K).root.fire("keydown", { key: "ArrowUp" });               // POST #0 in flight
    x.knobOf("0/2/2").root.fire("keydown", { key: "End" });             // Bass = 1 queued
    x.sse({ t: "param", path: 0, block: 2, pid: 2, value: 0.2 });       // hardware touches Bass meanwhile
    x.posts[0].reply(200, { ok: true, value: x.posts[0].body.value }); await x.tick();
    ok("S15 a queued local change wins over a remote message", x.posts[1] && x.posts[1].body.value === 1, JSON.stringify(x.posts[1] && x.posts[1].body));
  }
  { // S17: save waits for the queue
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 }), P_({ pid: 2, name: "Bass", label: "Bass", value: 0.5 })])]));
    x.knobOf(K).root.fire("keydown", { key: "End" }); x.knobOf("0/2/2").root.fire("keydown", { key: "End" });
    x.ids.save.onclick(); await x.tick();
    const early = x.posts.map((p) => p.url);
    x.posts[0].reply(200, { ok: true, value: 1 }); await x.tick(); await x.tick();
    x.posts[1].reply(200, { ok: true, value: 1 }); await x.tick(); await x.tick(); await x.tick();
    ok("S17 Save goes out only after every queued tweak", early.join() === "/param" && x.posts.map((p) => p.url).join() === "/param,/param,/save", early.join() + " then " + x.posts.map((p) => p.url).join());
  }
  { // remote change flashes in and is not written back
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.sse({ t: "param", path: 0, block: 2, pid: 1, value: 0.8 });
    ok("a hardware change shows up and causes no write", x.api.P.get(K).p.value === 0.8 && x.posts.length === 0, x.api.P.get(K).p.value);
  }
  { // S18: in-place snapshot switch — the bar re-highlights, params move, no re-render
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshots: ["S1", "S2"], snapshot: 0 }));
    x.sse({ t: "meta", dirty: false, connected: true, preset: { cid: 7, name: "T" }, snapshot: 1, snapshot_name: "S2" });
    x.sse({ t: "param", path: 0, block: 2, pid: 1, value: 0.9 });
    const on = x.ids.snaprow.find((e) => e.tagName === "button" && e.classList.contains("on"));
    ok("S18 an in-place snapshot switch re-highlights the bar and moves the knob",
      x.api.P.get(K).p.value === 0.9 && on.length === 1 && on[0].textContent.indexOf("2") === 0,
      JSON.stringify([x.api.P.get(K).p.value, on.map((b) => b.textContent)]));
  }
  { // S19: adjective search highlights the affecting knobs in ACTIVE blocks only (hgc-nci)
    const x = boot();
    const amp = Blk({ path: 0, block: 2, model: "HD2_AmpA", category: "amp" },
      [P_({ pid: 1, name: "Drive", label: "Drive", value: 0.5 }),
       P_({ pid: 2, name: "Mid", label: "Mid", value: 0.5 })]);
    const rev = Blk({ path: 0, block: 3, model: "HD2_Rev", category: "reverb" },
      [P_({ pid: 1, name: "Mix", label: "Mix", value: 0.3 })]);
    const ampOff = Blk({ path: 0, block: 4, model: "HD2_AmpB", category: "amp", enabled: false },
      [P_({ pid: 1, name: "Drive", label: "Drive", value: 0.5 })]);
    x.sse(State([amp, rev, ampOff]));
    // lazy-load the adjective map on first focus, then choose "crunchier"
    x.ids.adjsearch.fire("focus");
    const ir = x.posts.find((p) => p.url === "/intents");
    ok("S19 the search box fetches the adjective map on first use", !!ir, JSON.stringify(x.posts.map((p) => p.url)));
    ir.reply(200, { aliases: ["Crunchier", "crunchy"], intents: [
      { id: "crunchier", label: "Crunchier", aliases: ["crunchy", "gritty"], meaning: "pedal-forward crunch",
        moves: [{ role: "drive_pedal", param: "Gain", dir: 1 },
                { role: "amp", param: "Drive", dir: -1 }, { role: "amp", param: "Mid", dir: 1 }] }] });
    await x.tick();
    x.ids.adjsearch.value = "crunchier"; x.ids.adjsearch.fire("input");
    const hl = (key) => x.knobOf(key).root.classList;
    const cn = (key) => x.knobOf(key).root.className;
    ok("S19 matching knobs in the active amp light up", hl("0/2/1").contains("hl-on") && hl("0/2/2").contains("hl-on"),
      cn("0/2/1") + " | " + cn("0/2/2"));
    ok("S19 the direction hint is set (amp Drive down)", x.knobOf("0/2/1").root.dataset.hldir === "↓", x.knobOf("0/2/1").root.dataset.hldir);
    ok("S19 a turn-down knob gets hl-down, not hl-up (amp Drive)",
      hl("0/2/1").contains("hl-down") && !hl("0/2/1").contains("hl-up"), cn("0/2/1"));
    ok("S19 a turn-up knob gets hl-up, not hl-down (amp Mid)",
      hl("0/2/2").contains("hl-up") && !hl("0/2/2").contains("hl-down"), cn("0/2/2"));
    ok("S19 a non-affected knob (reverb Mix) is de-emphasised, not lit",
      hl("0/3/1").contains("hl-off") && !hl("0/3/1").contains("hl-on"), cn("0/3/1"));
    ok("S19 a matching knob in a BYPASSED block is not lit",
      !hl("0/4/1").contains("hl-on"), cn("0/4/1"));
    // un-bypass the second amp from the hardware -> its Drive must now light, with no re-render
    x.sse({ t: "bypass", path: 0, block: 4, enabled: true });
    ok("S19 un-bypassing a block re-evaluates the highlight", hl("0/4/1").contains("hl-on"), cn("0/4/1"));
    // clearing the box removes every highlight
    x.ids.adjsearch.value = ""; x.ids.adjsearch.fire("input");
    ok("S19 clearing the search removes all highlighting",
      !hl("0/2/1").contains("hl-on") && !hl("0/2/1").contains("hl-down") && !hl("0/2/2").contains("hl-up")
      && !hl("0/2/1").contains("hl-off") && !hl("0/3/1").contains("hl-off"),
      cn("0/2/1") + " | " + cn("0/3/1"));
  }
  { // S20: library drawer
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.ids.libtoggle.onclick(); 
    const st = x.posts.find((p) => p.url === "/library/state");
    st.reply(200, { connected: true, setlists: [{ name: "SL", cid: 5, members: [] }], irs: [], tones: [
      { slug: "a", name: "Alpha", variants: [{ key: "k", device_cid: 42, hsp: "a.hsp", diff: "verifying" }] }] });
    await x.tick(); await x.tick();
    const txt = () => x.ids.drawerbody.textContent;
    ok("S20 drawer opens and renders tones from /library/state", x.ids.drawer.hidden === false && txt().includes("Alpha") && txt().includes("verifying"), txt());
    x.sse({ kind: "badge", cid: 42, diff: "changed" });
    ok("S20 a verifying badge settles to changed on an SSE delta", txt().includes("changed") && !txt().includes("verifying"), txt());
    x.sse({ kind: "setlist_members", setlist_cid: 5, members: [1, 2] });
    ok("S20 setlist membership updates in place", txt().includes("2 tones"), txt());
    const edit = x.ids.drawerbody.find((e) => e.tagName === "button" && e.textContent === "Edit")[0];
    edit.onclick();
    const pr = x.posts.find((p) => p.url === "/preset");
    ok("S20 Edit posts /preset with the device cid", pr && pr.body.cid === 42, JSON.stringify(pr && pr.body));
    pr.reply(200, { ok: true }); await x.tick(); await x.tick();
    ok("S20 Edit collapses the drawer", x.ids.drawer.hidden === true, x.ids.drawer.hidden);
    const cp = x.ids.drawerbody.find((e) => e.tagName === "button" && e.textContent === "Copy")[0];
    cp.onclick();
    ok("S20 Copy posts /library/copy", x.posts.some((p) => p.url === "/library/copy" && p.body.hsp === "a.hsp" && p.body.to === "SL"), JSON.stringify(x.posts.map((p) => p.url)));
  }
  { // S21: Device pane — global settings read, write, and read-only page
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const devTab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Device")[0];
    ok("S21 a Device tab appears once the pane registers", !!devTab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    devTab.onclick();                                   // showPane("device") -> onShow(true)
    const cat = x.posts.find((p) => p.url === "/device/state");
    ok("S21 opening the pane fetches the catalog", !!cat, JSON.stringify(x.posts.map((p) => p.url)));
    cat.reply(200, { pages: [
      { page: "ins-outs", label: "Ins / Outs", readonly: false, note: null, keys: [] },
      { page: "date-time", label: "Date / Time", readonly: true, note: "set on the device", keys: [] }] });
    await x.tick(); await x.tick();
    const vals = x.posts.find((p) => p.url === "/device/values");
    ok("S21 the first page's live values are requested", vals && vals.body.page === "ins-outs",
      JSON.stringify(vals && vals.body));
    vals.reply(200, { page: "ins-outs", keys: [
      { key: "global.out.level.qtr", name: "Qtr Out", type: "f", min: -30, max: 0, default: 0, enum: [], value: -3, display: "-3.0 dB" },
      { key: "global.tuner.type", name: "Tuner Type", type: "i", min: 0, max: 2, default: 0, enum: ["Fluid", "Strobe", "Needle"], value: 1, display: "Strobe (1)" }] });
    await x.tick(); await x.tick();
    ok("S21 settings rows render with name + display",
      x.ids.devbody.textContent.includes("Qtr Out") && x.ids.devbody.textContent.includes("Strobe"), x.ids.devbody.textContent);
    const sel = x.ids.devbody.find((e) => e.tagName === "select")[0];
    sel.value = 2; sel.onchange();
    const setp = x.posts.find((p) => p.url === "/device/set");
    ok("S21 a change posts /device/set with key + value",
      setp && setp.body.key === "global.tuner.type" && setp.body.value === 2, JSON.stringify(setp && setp.body));
    setp.reply(200, { ok: true, key: "global.tuner.type", value: 2, display: "Needle (2)" });
    await x.tick();
    ok("S21 the display updates to the device readback", x.ids.devbody.textContent.includes("Needle (2)"), x.ids.devbody.textContent);
    const dtTab = x.ids.devtabs.find((e) => e.tagName === "button" && e.textContent === "Date / Time")[0];
    dtTab.onclick();
    const dt = x.posts.filter((p) => p.url === "/device/values").pop();
    dt.reply(200, { page: "date-time", keys: [
      { key: "global.clock.year", name: "Year", type: "i", min: 2000, max: 2099, default: 2026, enum: [], value: 2026, display: "2026" }] });
    await x.tick(); await x.tick();
    ok("S21 the read-only page shows its note", !x.ids.devnote.hidden && x.ids.devnote.textContent.includes("set on the device"), x.ids.devnote.textContent);
    const yearCtl = x.ids.devbody.find((e) => e.tagName === "input")[0];
    ok("S21 a read-only page's control is disabled", yearCtl && yearCtl.disabled === true, yearCtl && yearCtl.disabled);
  }
  { // S22: the snapshot management panel — open, select, rename (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus", "Lead"] }));
    ok("S22 the panel starts hidden", x.ids.snappanel.hidden === true, x.ids.snappanel.hidden);
    x.ids.snap.onclick();                                  // open from the header chip
    ok("S22 clicking the chip opens the panel", x.ids.snappanel.hidden === false, x.ids.snappanel.hidden);
    const rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    ok("S22 a row per snapshot", rows.length === 3, rows.length);
    ok("S22 the active snapshot is marked", rows[0]._cls.has("active") && !rows[1]._cls.has("active"),
      [...rows[0]._cls].join(","));
    // Select snapshot 2 (index 1): a non-active row has a Select button
    const sel = rows[1].find((e) => e.tagName === "button" && e.textContent === "Select")[0];
    sel.onclick();
    const selPost = x.posts.filter((p) => p.url === "/snapshot").pop();
    ok("S22 Select recalls the snapshot by index", selPost && selPost.body.index === 1, JSON.stringify(selPost && selPost.body));
    ok("S22 the active snapshot has no Select button",
      rows[0].find((e) => e.tagName === "button" && e.textContent === "Select").length === 0, "active had Select");
    // Rename snapshot 1 (index 0): Rename -> input -> Enter
    const ren = rows[0].find((e) => e.tagName === "button" && e.textContent === "Rename")[0];
    ren.onclick();
    const inp = x.ids.snapbody.find((e) => e.tagName === "input")[0];
    ok("S22 Rename reveals an input seeded with the name", inp && inp.value === "Rhythm", inp && inp.value);
    inp.value = "Intro"; inp.onkeydown({ key: "Enter", preventDefault() {} });
    const renPost = x.posts.filter((p) => p.url === "/snapshot/rename").pop();
    ok("S22 Enter posts the rename with index + trimmed name",
      renPost && renPost.body.index === 0 && renPost.body.name === "Intro", JSON.stringify(renPost && renPost.body));
    // Close
    x.ids.snapclose.onclick();
    ok("S22 the close button hides the panel", x.ids.snappanel.hidden === true, x.ids.snappanel.hidden);
  }
  { // S23: rename edge cases — a torn-down focused input must NOT auto-commit
    const start = (x) => { x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus"] }));
      x.ids.snap.onclick();
      const rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
      rows[0].find((e) => e.tagName === "button" && e.textContent === "Rename")[0].onclick();
      return x.ids.snapbody.find((e) => e.tagName === "input")[0]; };
    const renames = (x) => x.posts.filter((p) => p.url === "/snapshot/rename");
    { // Escape cancels — no write, even though removing the input fires blur
      const x = boot(); const inp = start(x); inp.value = "Half";
      inp.onkeydown({ key: "Escape", preventDefault() {} });
      ok("S23 Escape writes nothing", renames(x).length === 0, JSON.stringify(renames(x).map((p) => p.body)));
    }
    { // a background snapshot switch mid-typing keeps the edit and writes nothing
      const x = boot(); const inp = start(x); inp.value = "Still typin";
      x.sse({ t: "meta", snapshot: 1, snapshot_name: "Chorus", connected: true, dirty: false });
      ok("S23 a meta mid-edit does not commit", renames(x).length === 0, JSON.stringify(renames(x).map((p) => p.body)));
      const live = x.ids.snapbody.find((e) => e.tagName === "input")[0];
      ok("S23 the in-progress rename survives the meta", live && live.value === "Still typin", live && live.value);
    }
    { // Enter commits exactly once, even though the server state re-render tears the input down
      const x = boot(); const inp = start(x); inp.value = "Intro";
      inp.onkeydown({ key: "Enter", preventDefault() {} });
      x.sse(State([Blk({}, [P_({ value: 0.5 })])], { rev: 2, version: 2, snapshot: 0, snapshots: ["Intro", "Chorus"] }));
      ok("S23 Enter + the re-render commit exactly once", renames(x).length === 1, renames(x).length);
    }
  }

  { // S24: snapshot settings — Discard-Edits toggle + read-only context (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus"] }));
    x.ids.snap.onclick();                                 // open -> reads /snapshot/globals
    const g = x.posts.filter((p) => p.url === "/snapshot/globals").pop();
    ok("S24 opening the panel reads the snapshot globals", !!g, JSON.stringify(x.posts.map((p) => p.url)));
    g.reply(200, { keys: [
      { key: "global.snapshot.edits", value: 0, enum: ["Save", "Discard"], display: "Save (0)" },
      { key: "global.snapshot.reselect", value: 0, display: "Reload (0)" },
      { key: "global.tempo.select", value: 1, display: "Per Preset (1)" },
      { key: "global.tempo.bpm", value: 120, display: "120.0" },
      { key: "global.send.snapshot.cc", value: 0, display: "Off (0)" },
      { key: "global.midi.channel", value: 1 },
      { key: "global.midi.pc.send", value: 3, display: "MIDI+USB (3)" },
    ] });
    await x.tick();
    const sect = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-sect"))[0];
    ok("S24 a Snapshot settings section appears", sect && sect.textContent === "Snapshot settings", sect && sect.textContent);
    const setRows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-set"));
    const editRow = setRows.find((r) => r.textContent.indexOf("Discard edits") === 0);
    const btn = editRow && editRow.find((e) => e.tagName === "button")[0];
    ok("S24 Discard-Edits reads Off from the device value", btn && btn.textContent === "Off", btn && btn.textContent);
    // a non-Snapshot tempo mode warns that per-snapshot tempo is gated
    ok("S24 read-only tempo context shows BPM + mode",
      setRows.some((r) => r.textContent.indexOf("Tempo") === 0 && r.textContent.indexOf("120") >= 0
        && r.textContent.indexOf("Per Preset") >= 0),
      setRows.map((r) => r.textContent).join(" | "));
    ok("S24 MIDI Recall context is shown read-only",
      setRows.some((r) => r.textContent.indexOf("MIDI Recall") === 0 && r.textContent.indexOf("ch 1") >= 0),
      setRows.map((r) => r.textContent).join(" | "));
    // toggling writes the opposite enum index via the shared /device/set route
    btn.onclick();
    const setPost = x.posts.filter((p) => p.url === "/device/set").pop();
    ok("S24 toggling posts /device/set snapshot.edits = 1 (Discard)",
      setPost && setPost.body.key === "global.snapshot.edits" && setPost.body.value === 1,
      JSON.stringify(setPost && setPost.body));
  }
  { // S24b: Discard-Edits ON surfaces the "Save twice to keep edits" hint
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus"] }));
    x.ids.snap.onclick();
    x.posts.filter((p) => p.url === "/snapshot/globals").pop().reply(200, { keys: [
      { key: "global.snapshot.edits", value: 1, enum: ["Save", "Discard"], display: "Discard (1)" },
    ] });
    await x.tick();
    const hint = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-hint"))
      .some((h) => h.textContent.indexOf("discarded") >= 0);
    ok("S24b Discard-Edits ON shows the keep-edits hint", hint, "no hint");
    const btn = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-toggle"))[0];
    ok("S24b the toggle reads On when editing is discarded", btn && btn.textContent === "On", btn && btn.textContent);
  }

  { // S25: a /snapshot/globals reply landing mid-rename must NOT clobber the input
    // (#166 bug class via the async re-render path — the asymmetry the review found)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus"] }));
    x.ids.snap.onclick();                                  // open -> globals GET pending
    const g = x.posts.filter((p) => p.url === "/snapshot/globals").pop();
    // start a rename and type BEFORE the (slow device) globals read returns
    const rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[0].find((e) => e.tagName === "button" && e.textContent === "Rename")[0].onclick();
    x.ids.snapbody.find((e) => e.tagName === "input")[0].value = "Verse";
    g.reply(200, { keys: [{ key: "global.snapshot.edits", value: 0, enum: ["Save", "Discard"], display: "Save (0)" }] });
    await x.tick();
    const live = x.ids.snapbody.find((e) => e.tagName === "input")[0];
    ok("S25 the in-progress rename survives a globals reply", live && live.value === "Verse", live && live.value);
    ok("S25 the globals re-render writes no rename",
      x.posts.filter((p) => p.url === "/snapshot/rename").length === 0, "unexpected rename POST");
  }

  { // S25b: an empty / all-error globals body shows no bare "Snapshot settings" header
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm"] }));
    x.ids.snap.onclick();
    x.posts.filter((p) => p.url === "/snapshot/globals").pop().reply(200, { keys: [] });
    await x.tick();
    const hasHeader = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-sect")).length > 0;
    ok("S25b no bare settings header on an empty globals body", !hasHeader, "bare header present");
  }

  { // S26: copy a snapshot — pick source, paste onto a target (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus", "Lead"] }));
    x.ids.snap.onclick();
    let rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[0].find((e) => e.tagName === "button" && e.textContent === "Copy")[0].onclick();
    const banner = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-clip"))[0];
    ok("S26 arming copy shows the picker banner",
      banner && banner.textContent.indexOf("Copy snapshot 1") >= 0, banner && banner.textContent);
    rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    const paste = rows[2].find((e) => e.tagName === "button" && e.textContent === "Paste here")[0];
    ok("S26 non-source rows offer Paste here", !!paste, "no paste button");
    ok("S26 the source row offers no Paste",
      rows[0].find((e) => e.tagName === "button" && e.textContent === "Paste here").length === 0, "source had paste");
    paste.onclick();
    const post = x.posts.filter((p) => p.url === "/snapshot/copy").pop();
    ok("S26 paste posts /snapshot/copy src->dst",
      post && post.body.src === 0 && post.body.dst === 2, JSON.stringify(post && post.body));
    ok("S26 the picker is dismissed after paste",
      x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-clip")).length === 0, "banner still present");
  }
  { // S27: swap two snapshots — pick first, swap with second (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["Rhythm", "Chorus"] }));
    x.ids.snap.onclick();
    let rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[0].find((e) => e.tagName === "button" && e.textContent === "Swap")[0].onclick();
    const banner = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-clip"))[0];
    ok("S27 the swap banner warns colours stay with the slot",
      banner && banner.textContent.indexOf("colours stay") >= 0, banner && banner.textContent);
    rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[1].find((e) => e.tagName === "button" && e.textContent === "Swap here")[0].onclick();
    const post = x.posts.filter((p) => p.url === "/snapshot/swap").pop();
    ok("S27 swap posts /snapshot/swap a<->b",
      post && post.body.a === 0 && post.body.b === 1, JSON.stringify(post && post.body));
  }
  { // S27b: Cancel abandons a pending copy/swap and restores the row actions
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])], { snapshot: 0, snapshots: ["A", "B"] }));
    x.ids.snap.onclick();
    let rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[0].find((e) => e.tagName === "button" && e.textContent === "Copy")[0].onclick();
    x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-clip"))[0]
      .find((e) => e.tagName === "button" && e.textContent === "Cancel")[0].onclick();
    ok("S27b Cancel clears the picker",
      x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-clip")).length === 0, "banner remains");
    ok("S27b Copy buttons return after cancel",
      x.ids.snapbody.find((e) => e.tagName === "button" && e.textContent === "Copy").length === 2, "copy buttons missing");
  }

  { // S28: snapshot colour picker — click the dot, choose a swatch (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])],
      { snapshot: 0, snapshots: ["Rhythm", "Chorus"], snapshot_colors: [2, 0] }));
    x.ids.snap.onclick();
    let rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    const dot = rows[0].find((e) => e._cls && e._cls.has("sp-dot"))[0];
    ok("S28 the dot carries the snapshot's colour", dot && dot.style.background === "#e23c3c", dot && dot.style.background);
    dot.onclick();                                       // open the picker on snapshot 1
    const swatches = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-swatch"));
    ok("S28 clicking the dot opens the 12-colour picker", swatches.length === 12, swatches.length);
    ok("S28 the current colour is marked", swatches[2]._cls.has("on") && !swatches[0]._cls.has("on"), "wrong current swatch");
    swatches[6].onclick();                               // choose Green (index 6)
    const post = x.posts.filter((p) => p.url === "/snapshot/color").pop();
    ok("S28 choosing a swatch posts /snapshot/color index+colour",
      post && post.body.index === 0 && post.body.color === 6, JSON.stringify(post && post.body));
    ok("S28 the picker closes after a choice",
      x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-swatch")).length === 0, "picker still open");
  }

  { // S28b: starting a rename dismisses an open colour picker (no dual-state)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5 })])],
      { snapshot: 0, snapshots: ["Rhythm", "Chorus"], snapshot_colors: [2, 0] }));
    x.ids.snap.onclick();
    let rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[0].find((e) => e._cls && e._cls.has("sp-dot"))[0].onclick();   // open picker on row 0
    ok("S28b the picker is open", x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-swatch")).length === 12,
      x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-swatch")).length);
    rows = x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-row"));
    rows[1].find((e) => e.tagName === "button" && e.textContent === "Rename")[0].onclick();
    ok("S28b starting a rename closes the colour picker",
      x.ids.snapbody.find((e) => e._cls && e._cls.has("sp-swatch")).length === 0, "picker lingered");
  }

  console.log(fails ? `\n${fails} FAILED` : "\nall page scenarios pass");
  process.exit(fails ? 1 : 0);
})();
