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
  console.log(fails ? `\n${fails} FAILED` : "\nall page scenarios pass");
  process.exit(fails ? 1 : 0);
})();
