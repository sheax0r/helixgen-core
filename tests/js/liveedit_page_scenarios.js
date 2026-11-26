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
    // Non-focused blocks carry their emphasis on the TILE: only the selected
    // block's knobs are rendered (canvas + focus reframe), so the across-chain
    // "lit / de-emphasised / bypassed" checks read the tiles, not the knobs.
    const til = (key) => x.tileOf(key).classList;
    const tcn = (key) => x.tileOf(key).className;
    ok("S19 the selected amp's tile is lit (it has matching knobs)",
      til("0/2").contains("hl-up") || til("0/2").contains("hl-down") || til("0/2").contains("hl-on"), tcn("0/2"));
    ok("S19 a non-affected block (reverb) tile is de-emphasised, not lit",
      til("0/3").contains("hl-off") && !til("0/3").contains("hl-on"), tcn("0/3"));
    ok("S19 a BYPASSED matching block's tile is not lit",
      !til("0/4").contains("hl-on") && !til("0/4").contains("hl-up") && !til("0/4").contains("hl-down"), tcn("0/4"));
    // un-bypass the second amp from the hardware -> its tile must now light, with no re-render
    x.sse({ t: "bypass", path: 0, block: 4, enabled: true });
    ok("S19 un-bypassing a block re-evaluates the tile highlight",
      til("0/4").contains("hl-down") || til("0/4").contains("hl-up") || til("0/4").contains("hl-on"), tcn("0/4"));
    // clearing the box removes every highlight (knob + tile)
    x.ids.adjsearch.value = ""; x.ids.adjsearch.fire("input");
    ok("S19 clearing the search removes all highlighting",
      !hl("0/2/1").contains("hl-on") && !hl("0/2/1").contains("hl-down") && !hl("0/2/2").contains("hl-up")
      && !hl("0/2/1").contains("hl-off") && !til("0/3").contains("hl-off") && !til("0/4").contains("hl-down"),
      cn("0/2/1") + " | " + tcn("0/3"));
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
  { // S20b: library browse filter (client-side, no device I/O)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.ids.libtoggle.onclick();
    const st = x.posts.find((p) => p.url === "/library/state");
    st.reply(200, { connected: true, setlists: [], irs: [], tones: [
      { slug: "a", name: "Alpha", tags: [], variants: [{ key: "k", guitar: "Les Paul", preset_name: "Alpha" }] },
      { slug: "b", name: "Beta", tags: ["metal"], variants: [{ key: "k", guitar: "Tele", preset_name: "Beta" }] }] });
    await x.tick(); await x.tick();
    const boxes = () => x.ids.drawerbody.find((e) => e._cls && e._cls.has("ltone"));
    const search = x.ids.drawerbody.find((e) => e.tagName === "input" && e._cls && e._cls.has("lsearch"))[0];
    ok("S20b the Tones filter input renders", !!search, String(!!search));
    ok("S20b both tones show before filtering", boxes()[0].hidden === false && boxes()[1].hidden === false,
      JSON.stringify(boxes().map((b) => b.hidden)));
    search.value = "tele"; search.oninput();
    ok("S20b filtering by a variant's guitar hides non-matches (Alpha hidden, Beta shown)",
      boxes()[0].hidden === true && boxes()[1].hidden === false, JSON.stringify(boxes().map((b) => b.hidden)));
    search.value = "metal"; search.oninput();
    ok("S20b the filter also matches a tag", boxes()[0].hidden === true && boxes()[1].hidden === false,
      JSON.stringify(boxes().map((b) => b.hidden)));
    search.value = "zzz"; search.oninput();
    const empty = x.ids.drawerbody.find((e) => e._cls && e._cls.has("lempty"))[0];
    ok("S20b a no-match query shows the empty state", empty && empty.hidden === false && empty.textContent.includes("zzz"),
      empty && JSON.stringify([empty.hidden, empty.textContent]));
    search.value = ""; search.oninput();
    ok("S20b clearing the filter shows every tone again and hides the empty state",
      boxes()[0].hidden === false && boxes()[1].hidden === false && empty.hidden === true,
      JSON.stringify([boxes().map((b) => b.hidden), empty.hidden]));
  }
  { // S20c: IR folders — grouping + (re)assignment (library-first, disk-only)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    x.ids.libtoggle.onclick();
    const st = x.posts.find((p) => p.url === "/library/state");
    st.reply(200, { connected: true, setlists: [], tones: [],
      ir_headroom: { used: 2, cap: 2048 },
      irs: [{ hash: "aaa", name: "A cab", presence: "both", folder: "Marshall" },
            { hash: "bbb", name: "B cab", presence: "computer", folder: null }] });
    await x.tick(); await x.tick();
    const hds = x.ids.drawerbody.find((e) => e._cls && e._cls.has("lfolderhd")).map((e) => e.textContent);
    ok("S20c IRs render a folder header + an Unfiled group",
      hds.indexOf("Marshall") !== -1 && hds.indexOf("Unfiled") !== -1, JSON.stringify(hds));
    const fbtns = x.ids.drawerbody.find((e) => e.tagName === "button" && e.textContent === "Folder…");
    fbtns[0].onclick();                                 // the filed IR (aaa) is rendered first
    const inp = x.ids.drawerbody.find((e) => e.tagName === "input" && e._cls && e._cls.has("lfolderin"))[0];
    ok("S20c Folder… reveals an input seeded with the IR's current folder", inp && inp.value === "Marshall", inp && inp.value);
    inp.value = "Vox"; inp.onkeydown({ key: "Enter", preventDefault() {} });
    const post = x.posts.find((p) => p.url === "/library/set_ir_folder");
    ok("S20c committing posts set_ir_folder with the hash + new folder",
      post && post.body.hash === "aaa" && post.body.folder === "Vox", JSON.stringify(post && post.body));
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

  { // S30: controllers pane — the EXP/FS source picker assigns a local source (81v.3)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Controllers")[0];
    ok("S30 a Controllers tab appears", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();
    const srcReq = x.posts.find((p) => p.url === "/controllers/sources");
    ok("S30 opening the pane fetches the source catalogue", !!srcReq,
      JSON.stringify(x.posts.map((p) => p.url)));
    srcReq.reply(200, { sources: [
      { id: "EXP1", name: "Expression Pedal 1", kind: "expression" },
      { id: "FS1", name: "Footswitch 1", kind: "footswitch" }] });
    await x.tick(); await x.tick();
    const stReq = x.posts.find((p) => p.url === "/controllers/state");
    if (stReq) { stReq.reply(200, { footswitches: [], expression: [], midi: [], unknown: [] }); await x.tick(); await x.tick(); }
    const ssel = x.ids.ctlassign.find((e) => e.tagName === "select"
      && e.getAttribute("aria-label") === "Controller source")[0];
    const asrc = x.ids.ctlassign.find((e) => e.tagName === "button"
      && e.textContent === "Assign source")[0];
    ok("S30 the source picker + Assign source button render", !!ssel && !!asrc,
      JSON.stringify([!!ssel, !!asrc]));
    // pick the block/param/source (a real <select> defaults to its first option;
    // the fake one needs an explicit value)
    const psel = x.ids.ctlassign.find((e) => e.tagName === "select"
      && e.getAttribute("aria-label") === "Parameter")[0];
    psel.value = 1; ssel.value = "EXP1";
    asrc.onclick(); await x.tick();
    const post = x.posts.find((p) => p.url === "/controllers/assign-source");
    ok("S30 Assign source posts the chosen source for that exact param",
      post && post.body.source === "EXP1" && post.body.path === 0 &&
      post.body.block === 2 && post.body.pid === 1,
      JSON.stringify(post && post.body));
  }

  { // S29: per-param Snapshot Control toggle, OFF -> ON (81v.2)
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5, snap: false })])]));
    const tog = x.knobOf(K).root.find((e) => e._cls && e._cls.has("tog"))[0];
    ok("S29 an uncontrolled param shows a hollow snapshot toggle",
      tog && tog._cls.has("off"), tog && [...(tog._cls || [])]);
    tog.onclick({ preventDefault() {}, stopPropagation() {} }); await x.tick();
    ok("S29 clicking it posts snapshot-control ON for that exact param",
      x.posts.length === 1 && x.posts[0].url === "/snapshot/param-control" &&
      x.posts[0].body.on === true && x.posts[0].body.path === 0 &&
      x.posts[0].body.block === 2 && x.posts[0].body.pid === 1 &&
      x.posts[0].body.rev === 1,
      JSON.stringify(x.posts[0] && x.posts[0].body));
  }

  { // S29b: a snapshot-controlled param's toggle turns it OFF
    const x = boot();
    x.sse(State([Blk({}, [P_({ value: 0.5, snap: true })])]));
    const tog = x.knobOf(K).root.find((e) => e._cls && e._cls.has("tog"))[0];
    ok("S29b a controlled param shows a filled (not hollow) toggle",
      tog && !tog._cls.has("off"), tog && [...(tog._cls || [])]);
    tog.onclick({ preventDefault() {}, stopPropagation() {} }); await x.tick();
    ok("S29b clicking it posts snapshot-control OFF",
      x.posts.length === 1 && x.posts[0].body.on === false,
      JSON.stringify(x.posts[0] && x.posts[0].body));
  }

  { // S31: Mixer pane — read-only Matrix Mixer, layer tabs + strips (81v.8)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Mixer")[0];
    ok("S31 a Mixer tab appears once the pane registers", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();
    const req = x.posts.find((p) => p.url === "/mixer/state");
    ok("S31 opening the pane fetches the mixer state", !!req,
      JSON.stringify(x.posts.map((p) => p.url)));
    const strip = (name, o = {}) => ({ group: "home", name,
      vol_db: o.vol == null ? 0 : o.vol, pan: o.pan == null ? 0 : o.pan,
      mute: !!o.mute, solo: !!o.solo, enabled: o.enabled !== false });
    req.reply(200, {
      layers: [
        { output: '1/4"', dst: 1, master_db: -18.5, master_mute: false, mute: false, solo: false,
          channels: [strip("Path 1A", { vol: -3.2, pan: -0.7, mute: true }), strip("AUX In", { enabled: false })] },
        { output: "XLR", dst: 2, master_db: -22, master_mute: false, mute: false, solo: false,
          channels: [strip("Path 1A", { solo: true })] }],
      link_mix: { output: "Link Mix", dst: 0, master_db: 0, master_mute: false, mute: false, solo: false,
        channels: [strip("Path 1A")] },
    });
    await x.tick(); await x.tick();
    const tabs = x.ids.mxtabs.children.map((c) => c.textContent);
    ok("S31 a tab per output layer plus Link Mix", tabs.join(",") === '1/4",XLR,Link Mix', tabs.join(","));
    ok("S31 the first layer's master level shows", x.ids.mxbody.textContent.includes("-18.5 dB"),
      x.ids.mxbody.textContent);
    ok("S31 a strip renders its name and level",
      x.ids.mxbody.textContent.includes("Path 1A") && x.ids.mxbody.textContent.includes("-3.2 dB"),
      x.ids.mxbody.textContent);
    ok("S31 a non-zero pan renders L/R without collapsing to 0",
      x.ids.mxbody.textContent.includes("L0.7"), x.ids.mxbody.textContent);
    ok("S31 a muted strip lights its M flag",
      x.ids.mxbody.find((e) => e._cls && e._cls.has("on-m")).length >= 1, "no muted flag");
    ok("S31 a disabled (greyed) strip gets .off",
      x.ids.mxbody.find((e) => e._cls && e._cls.has("off")).length >= 1, "no off row");
    const linkTab = x.ids.mxtabs.find((e) => e.tagName === "button" && e.textContent === "Link Mix")[0];
    linkTab.onclick();
    ok("S31 switching to Link Mix re-renders off cached state (no refetch)",
      x.ids.mxbody.textContent.includes("Link Mix")
      && x.posts.filter((p) => p.url === "/mixer/state").length === 1,
      x.ids.mxbody.textContent);
  }

  { // S32: Global EQ pane — snapshot read + live write, shadow tagging (81v.6)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Global EQ")[0];
    ok("S32 a Global EQ tab appears once the pane registers", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();
    const req = x.posts.find((p) => p.url === "/globaleq/state");
    ok("S32 opening the pane fetches the state", !!req, JSON.stringify(x.posts.map((p) => p.url)));
    req.reply(200, { outputs: {
      qtr: { enable: false, level: 0, bands: {
        lowcut: { enable: true, freq: 20, slope: 1 },
        low: { enable: true, freq: 150, gain: 0, q: 0.707 },
        highcut: { enable: true, freq: 20000, slope: 1 } } },
      xlr: { enable: true, level: -2, bands: { low: { enable: true, freq: 150, gain: 0, q: 0.707 } } },
      pho: { enable: false, level: 0, bands: { low: { enable: true, freq: 150, gain: 0, q: 0.707 } } } } });
    await x.tick(); await x.tick();
    const tabs = x.ids.eqtabs.children.map((c) => c.textContent);
    ok("S32 a tab per output", tabs.join(",") === '1/4",XLR,Phones', tabs.join(","));
    ok("S32 band names render", x.ids.eqbody.textContent.includes("Low Cut")
      && x.ids.eqbody.textContent.includes("High Cut"), x.ids.eqbody.textContent);
    const gainInp = x.ids.eqbody.find((e) => e.tagName === "input" && e.attrs["aria-label"] === "Low Gain")[0];
    ok("S32 a peaking band shows a Gain control", !!gainInp, "no gain input");
    gainInp.value = "4.5"; gainInp.onchange();
    const w = x.posts.find((p) => p.url === "/globaleq/set");
    ok("S32 moving a band control writes /globaleq/set",
      !!w && w.body.output === "qtr" && w.body.band === "low" && w.body.param === "gain" && w.body.value === 4.5,
      JSON.stringify(w && w.body));
    w.reply(200, { ok: true, output: "qtr", band: "low", param: "gain", value: 4.5 }); await x.tick();
    ok("S32 the written control is marked set", gainInp._cls.has("eq-set"), [...gainInp._cls].join(","));
    const masterCb = x.ids.eqbody.find((e) => e.tagName === "input" && e.attrs["aria-label"] === "Global EQ enabled")[0];
    ok("S32 the master enable reflects the live read (Off)", !!masterCb && masterCb.checked === false, "master cb");
    masterCb.checked = true; masterCb.onchange();
    const mw = x.posts.filter((p) => p.url === "/globaleq/set").slice(-1)[0];
    ok("S32 the master toggle writes band='' param='enable'",
      mw.body.band === "" && mw.body.param === "enable" && mw.body.value === true, JSON.stringify(mw.body));
    mw.reply(200, { ok: true, output: "qtr", band: "", param: "enable", value: true }); await x.tick();
    x.ids.eqrefresh.onclick(); await x.tick();
    const req2 = x.posts.filter((p) => p.url === "/globaleq/state").slice(-1)[0];
    req2.reply(200, { outputs: {
      qtr: { enable: false, level: 0, bands: { low: { enable: true, freq: 150, gain: 0, q: 0.707 } } },
      xlr: { enable: false, level: 0, bands: {} }, pho: { enable: false, level: 0, bands: {} } } });
    await x.tick(); await x.tick();
    const gainInp2 = x.ids.eqbody.find((e) => e.tagName === "input" && e.attrs["aria-label"] === "Low Gain")[0];
    ok("S32 a session edit survives Refresh over the static snapshot",
      gainInp2 && String(gainInp2.value) === "4.5" && gainInp2._cls.has("eq-set"), gainInp2 && gainInp2.value);
    // a band-enable toggle is write-only too, so it gets the same "set" marker
    const lowEnWrap = x.ids.eqbody.find((e) => e.tagName === "label"
      && e.find((c) => c.attrs && c.attrs["aria-label"] === "Low enabled").length)[0];
    ok("S32 a band enable is a labelled checkbox", !!lowEnWrap, "no low-enable label");
    const lowEnCb = lowEnWrap.find((c) => c.tagName === "input")[0];
    lowEnCb.checked = false; lowEnCb.onchange();
    const ew = x.posts.filter((p) => p.url === "/globaleq/set").slice(-1)[0];
    ok("S32 toggling a band enable writes band enable",
      ew.body.band === "low" && ew.body.param === "enable" && ew.body.value === false, JSON.stringify(ew.body));
    ew.reply(200, { ok: true, output: "qtr", band: "low", param: "enable", value: false }); await x.tick();
    ok("S32 a written band enable is marked set (provenance parity with numbers)",
      lowEnWrap._cls.has("eq-set"), [...lowEnWrap._cls].join(","));
    // Reset EQ: one click flattens the output to factory defaults
    const resetBtn = x.ids.eqbody.find((e) => e.tagName === "button" && e.textContent === "Reset EQ")[0];
    ok("S32 a Reset EQ button is present", !!resetBtn, "no reset button");
    resetBtn.onclick();
    resetBtn.onclick();   // a double-click must not fan a 2nd batch at the flaky device
    ok("S32 Reset is debounced while one batch is in flight",
      x.posts.filter((p) => p.url === "/globaleq/reset").length === 1,
      String(x.posts.filter((p) => p.url === "/globaleq/reset").length));
    const rr = x.posts.filter((p) => p.url === "/globaleq/reset").slice(-1)[0];
    ok("S32 Reset posts the output", rr && rr.body.output === "qtr", JSON.stringify(rr && rr.body));
    rr.reply(200, { output: "qtr", bands: { low: { enable: true, freq: 150, gain: 0, q: 0.707 } } }); await x.tick();
    const gainAfterReset = x.ids.eqbody.find((e) => e.tagName === "input" && e.attrs["aria-label"] === "Low Gain")[0];
    ok("S32 Reset folds factory values back into the control", gainAfterReset && String(gainAfterReset.value) === "0",
      gainAfterReset && gainAfterReset.value);
    // Copy qtr → Paste onto xlr
    const copyBtn = x.ids.eqbody.find((e) => e.tagName === "button" && e.textContent === "Copy")[0];
    copyBtn.onclick(); await x.tick();
    const xlrTab = x.ids.eqtabs.find((e) => e.tagName === "button" && e.textContent === "XLR")[0];
    xlrTab.onclick(); await x.tick();
    const pasteBtn = x.ids.eqbody.find((e) => e.tagName === "button" && e.textContent === "Paste")[0];
    ok("S32 Paste is enabled after a Copy", pasteBtn && !pasteBtn.disabled, "paste disabled");
    pasteBtn.onclick();
    const pp = x.posts.filter((p) => p.url === "/globaleq/apply").slice(-1)[0];
    ok("S32 Paste applies the copied EQ to the current output",
      pp && pp.body.output === "xlr" && pp.body.bands && pp.body.bands.low, JSON.stringify(pp && pp.body));
    pp.reply(200, { output: "xlr", bands: { low: { gain: 0, q: 0.707, enable: true, freq: 150 } } }); await x.tick();
  }

  { // S33: Command Center pane — read-only commands view (81v.4)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Command Center")[0];
    ok("S33 a Command Center tab appears once the pane registers", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();
    const req = x.posts.find((p) => p.url === "/commands/state");
    ok("S33 opening the pane fetches the command state", !!req,
      JSON.stringify(x.posts.map((p) => p.url)));
    req.reply(200, { sources: [
      { id: "FS3", label: "Footswitch 3 (top row, 3rd from left)", named: true, commands: [
        { family: "MIDI", headline: "MIDI CC", action: "", behavior: "momentary",
          toggle: false, fields: [["Channel", "1"], ["CC#", "11"], ["Value", "0"]] }] },
      { id: "0x09990000", label: "source 0x09990000", named: false, commands: [
        { family: "Preset/Snapshot", headline: "Snapshot", action: "Press", behavior: "latching",
          toggle: false, fields: [["Snapshot", "2"]] }] }] });
    await x.tick(); await x.tick();
    const txt = x.ids.cmdbody.textContent;
    ok("S33 a named source shows its English label", txt.includes("Footswitch 3 (top row, 3rd from left)"), txt);
    ok("S33 the command headline renders", txt.includes("MIDI CC"), txt);
    ok("S33 a PresetSnapshot Action chip renders", txt.includes("Press"), txt);
    ok("S33 a legitimate zero value is not hidden", txt.includes("Value") && txt.includes("0"), txt);
    ok("S33 the has-commands marker is drawn", txt.includes("▶"), txt);
    ok("S33 an unnameable source is shown, not dropped",
      txt.includes("Snapshot") && txt.includes("helixgen can't name"), txt);
    // empty tone -> the empty-state line, no crash
    const y = boot(); y.sse(State([Blk({}, [P_({})])]));
    const t2 = y.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Command Center")[0];
    t2.onclick(); await y.tick();
    y.posts.find((p) => p.url === "/commands/state").reply(200, { sources: [] });
    await y.tick(); await y.tick();
    ok("S33 an empty tone shows the empty-state line",
      y.ids.cmdbody.textContent.includes("No commands assigned"), y.ids.cmdbody.textContent);
  }

  { // S34: Tuner pane — read-only live pitch needle (81v.10)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const ptrX = () => x.ids["tuner-pointer"].style.transform;   // translateX(N px)
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Tuner")[0];
    ok("S34 a Tuner tab appears once the pane registers", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();                     // showPane -> onShow(true) -> immediate poll + interval
    ok("S34 opening the pane starts a poll timer", x.intervals.size === 1, "no interval");
    const req = x.posts.find((p) => p.url === "/tuner/reading");
    ok("S34 the pane polls /tuner/reading", !!req, JSON.stringify(x.posts.map((p) => p.url)));
    req.reply(200, { signal: true, name: "E2", note: "E", octave: 2, cents: -8, hz: 81.5, connected: true });
    await x.tick();
    ok("S34 a played note shows its name", x.ids["tuner-note"].textContent === "E2", x.ids["tuner-note"].textContent);
    ok("S34 the cents readout is signed", x.ids["tuner-cents"].textContent.startsWith("-8"), x.ids["tuner-cents"].textContent);
    ok("S34 the needle slides left (negative translateX) when flat", ptrX() === "translateX(-22.4px)", ptrX());
    ok("S34 off by 8 cents is not flagged in-tune", !x.ids["tuner-pointer"].getAttribute("class").includes("in-tune"),
      x.ids["tuner-pointer"].getAttribute("class"));
    // near-zero cents -> green + very close to centre (the needle tracks real
    // cents, so +1 cent sits 2.8px off; "in tune" is the ±3-cent colour flag)
    const fire = [...x.intervals.values()][0]; fire(); await x.tick();
    x.posts.filter((p) => p.url === "/tuner/reading").slice(-1)[0]
      .reply(200, { signal: true, name: "A2", note: "A", octave: 2, cents: 1, hz: 110.0, connected: true });
    await x.tick();
    ok("S34 near-zero cents flags in-tune", x.ids["tuner-pointer"].getAttribute("class").includes("in-tune")
      && x.ids["tuner-note"].className === "tuned", x.ids["tuner-pointer"].getAttribute("class"));
    ok("S34 +1 cent sits just off centre", ptrX() === "translateX(2.8px)", ptrX());
    // a partial signal:true reading (missing cents) must not paint NaN (D2 guard)
    fire(); await x.tick();
    x.posts.filter((p) => p.url === "/tuner/reading").slice(-1)[0]
      .reply(200, { signal: true, name: "A2", note: "A", octave: 2, connected: true });
    await x.tick();
    ok("S34 a partial reading recentres instead of painting NaN",
      ptrX() === "translateX(0px)" && x.ids["tuner-cents"].textContent === "", ptrX() + "|" + x.ids["tuner-cents"].textContent);
    // no signal -> dash + centred, not in-tune
    fire(); await x.tick();
    x.posts.filter((p) => p.url === "/tuner/reading").slice(-1)[0].reply(200, { signal: false, name: "—", connected: true });
    await x.tick();
    ok("S34 no signal shows a dash and recentres", x.ids["tuner-note"].textContent === "—"
      && ptrX() === "translateX(0px)"
      && !x.ids["tuner-pointer"].getAttribute("class").includes("in-tune"), x.ids["tuner-note"].textContent);
    // leaving the pane stops the poll timer
    const flowTab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Signal flow")[0];
    flowTab.onclick();
    ok("S34 leaving the tuner pane clears its poll (the flow pane now runs the I/O meter poll)", x.intervals.size === 1, "timer leaked: " + x.intervals.size);
  }

  { // S35: Device Info pane — read-only identity strip (81v.11)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const tab = x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Device Info")[0];
    ok("S35 a Device Info tab appears once the pane registers", !!tab,
      x.ids.tabs.children.map((c) => c.textContent).join(","));
    tab.onclick(); await x.tick();
    const req = x.posts.find((p) => p.url === "/device/info");
    ok("S35 opening the pane reads /device/info", !!req, JSON.stringify(x.posts.map((p) => p.url)));
    req.reply(200, { model: "stadium", helixgen_model: "stadium_xl", device_id: 2490368,
      serial: "47292244582131381", firmware: "1.4.1", firmware_build: 1410,
      firmware_date: "2026-05-01", sd_total_bytes: 32e9, sd_available_bytes: 28.5e9 });
    await x.tick();
    const txt = x.ids.dinfobody.textContent;
    ok("S35 the model reads as Stadium XL", txt.includes("Stadium XL"), txt);
    ok("S35 the serial shows", txt.includes("47292244582131381"), txt);
    ok("S35 firmware shows version + build", txt.includes("1.4.1") && txt.includes("build 1410"), txt);
    ok("S35 storage is formatted free-of-total in GB", txt.includes("28.5 GB free of 32 GB"), txt);
    // a sparse reply (only firmware) must not crash or print empty rows
    const y = boot(); y.sse(State([Blk({}, [P_({})])]));
    y.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Device Info")[0].onclick();
    await y.tick();
    y.posts.find((p) => p.url === "/device/info").reply(200, { firmware: "1.4.1" });
    await y.tick();
    ok("S35 a sparse reply shows what it has and omits the rest",
      y.ids.dinfobody.textContent.includes("1.4.1") && !y.ids.dinfobody.textContent.includes("Serial"),
      y.ids.dinfobody.textContent);
  }

  { // S36: Command Center — author / edit / remove MIDI commands on a footswitch (81v.4 write)
    const aria = (host, a) => host.find((e) => e.getAttribute && e.getAttribute("aria-label") === a)[0];
    const btn = (host, t) => host.find((e) => e.tagName === "button" && e.textContent === t)[0];
    // Open the pane, serve the authoring catalogue + one editable MIDI CC on FS1.
    const openCmd = async () => {
      const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
      x.ids.tabs.find((e) => e.tagName === "button" && e.textContent === "Command Center")[0].onclick();
      await x.tick();
      x.posts.find((p) => p.url === "/commands/sources").reply(200, { sources: [
        { id: "FS1", label: "Footswitch 1 (top row, 1st from left)" },
        { id: "FS3", label: "Footswitch 3 (top row, 3rd from left)" }] });
      await x.tick(); await x.tick();
      x.posts.find((p) => p.url === "/commands/state").reply(200, { sources: [
        { id: "FS1", label: "Footswitch 1 (top row, 1st from left)", named: true, commands: [
          { family: "MIDI", headline: "MIDI CC", action: "", behavior: "latching", toggle: false,
            fields: [["Channel", "1"], ["CC#", "11"], ["Value", "0"]],
            func: 1, params: { "MIDI Ch": 1, "CC#": 11, "Value": 0 },
            slot: 5, source: "FS1", editable: true }] }] });
      await x.tick(); await x.tick();
      return x;
    };

    // --- author ---
    const xa = await openCmd();
    const srcReq = xa.posts.find((p) => p.url === "/commands/sources");
    ok("S36 the pane fetches the authoring catalogue", !!srcReq, "no /commands/sources");
    const form = xa.ids.cmdassign;
    const fsel = aria(form, "Footswitch"), tsel = aria(form, "MIDI command type");
    ok("S36 the Add form renders a footswitch picker", !!fsel && fsel.children.length === 2,
      fsel ? String(fsel.children.length) : "no picker");
    ok("S36 the Add form offers the four MIDI subtypes", !!tsel && tsel.children.length === 4, "subtypes");
    fsel.value = "FS3"; tsel.value = "1"; tsel.onchange();     // CC subtype -> Ch/CC#/Value fields
    aria(form, "Ch").value = "2"; aria(form, "CC#").value = "7"; aria(form, "Value").value = "100";
    btn(form, "Add command").onclick(); await xa.tick();
    const addReq = xa.posts.find((p) => p.url === "/commands/add");
    ok("S36 Add posts the command to the device", !!addReq, "no /commands/add");
    ok("S36 Add carries the switch, subtype and field values",
      addReq && addReq.body.switch === "FS3" && addReq.body.func === 1
        && addReq.body.values["MIDI Ch"] === 2 && addReq.body.values["CC#"] === 7
        && addReq.body.values.Value === 100, JSON.stringify(addReq && addReq.body));

    // --- remove ---
    const xr = await openCmd();
    const rm = btn(xr.ids.cmdbody, "Remove");
    ok("S36 an editable command shows a Remove button", !!rm, "no Remove");
    rm.onclick(); await xr.tick();
    const rmReq = xr.posts.find((p) => p.url === "/commands/remove");
    ok("S36 Remove posts the switch + device slot",
      rmReq && rmReq.body.switch === "FS1" && rmReq.body.slot === 5, JSON.stringify(rmReq && rmReq.body));

    // --- edit (clear the old, author the new) ---
    const xe = await openCmd();
    const ed = btn(xe.ids.cmdbody, "Edit");
    ok("S36 an editable command shows an Edit button", !!ed, "no Edit");
    ed.onclick(); await xe.tick();
    const form2 = xe.ids.cmdassign;
    const ccPrefill = aria(form2, "CC#");
    ok("S36 Edit prefills the form and arms Save changes",
      !!btn(form2, "Save changes") && ccPrefill && String(ccPrefill.value) === "11",
      ccPrefill ? String(ccPrefill.value) : "no prefill");
    ccPrefill.value = "20";                               // nudge the CC number
    btn(form2, "Save changes").onclick(); await xe.tick();
    // Add goes FIRST (so a failed re-author never loses the original); the old
    // command is cleared by slot only after the add succeeds.
    const eAdd = xe.posts.find((p) => p.url === "/commands/add");
    ok("S36 Save authors the edited command first",
      eAdd && eAdd.body.switch === "FS1" && eAdd.body.func === 1 && eAdd.body.values["CC#"] === 20,
      JSON.stringify(eAdd && eAdd.body));
    ok("S36 Save does not clear the original before the add succeeds",
      !xe.posts.find((p) => p.url === "/commands/remove"), "remove fired before add reply");
    eAdd.reply(200, { ok: true }); await xe.tick();
    const eRm = xe.posts.find((p) => p.url === "/commands/remove");
    ok("S36 Save then clears the old command by its slot",
      eRm && eRm.body.switch === "FS1" && eRm.body.slot === 5, JSON.stringify(eRm && eRm.body));
  }
  { // S37: undo/redo work for a block that is NOT currently focused. Only the
    //       focused block registers controls (canvas+focus reframe), so the
    //       undo entry must re-focus its block and still apply — never be lost.
    const x = boot();
    x.sse(State([Blk({ block: 1, model: "AmpA", name: "A" }, [P_({ value: 0.5 })]),
                 Blk({ block: 2, model: "AmpB", name: "B" }, [P_({ value: 0.5 })])]));
    const drain = async () => { for (let i = 0; i < 30 && (x.api.inflight || x.api.pending.size); i++) {
      const p = x.posts.find((q) => !q._r); if (p) { p._r = 1; p.reply(200, p.body); } await x.tick(); } };
    const k = x.knobOf("0/1/1"); drag(k, 1, 300, 250); k.svg.fire("pointerup", { pointerId: 1 });
    await drain();
    ok("S37 editing the focused block records one undo entry", x.api.undoS.length === 1, x.api.undoS.length);
    x.tileOf("0/2").onclick({});                       // switch focus to B → A leaves P
    ok("S37 the previously-edited block is no longer registered", !x.api.P.get("0/1/1"), [...x.api.P.keys()].join());
    const p0 = x.posts.length; x.api.undo(); await drain();
    const rev = x.posts.slice(p0).find((p) => p.url === "/param" && p.body.block === 1);
    ok("S37 undo of the non-focused block reverts it on the device", !!rev && rev.body.value === 0.5, JSON.stringify(rev && rev.body));
    ok("S37 the undo entry survives (redo is available), not discarded", x.api.redoS.length === 1, "redoS=" + x.api.redoS.length);
    ok("S37 undo re-focuses the affected block", (() => { const f = x.ids.rig.find((e) => e.id === "focus")[0]; const n = f && f.find((e) => e._cls.has("nm"))[0]; return n && n.textContent === "A"; })(), "focus not on A");
  }
  { // S38: a queued write is NOT dropped when focus switches before it drains.
    const x = boot();
    x.sse(State([Blk({ block: 1, model: "AmpA", name: "A" }, [P_({ value: 0.5 })]),
                 Blk({ block: 2, model: "AmpB", name: "B" }, [P_({ value: 0.5 })])]));
    const k = x.knobOf("0/1/1"); drag(k, 1, 300, 250, 200); k.svg.fire("pointerup", { pointerId: 1 });
    ok("S38 one write in flight, the newest value queued", x.posts.length === 1 && x.api.pending.size === 1, x.posts.length + "/" + x.api.pending.size);
    const finalVal = x.api.P.get("0/1/1").p.value;
    x.tileOf("0/2").onclick({});                        // switch focus mid-flight → A leaves P
    x.posts[0].reply(200, { value: x.posts[0].body.value }); await x.tick();   // in-flight resolves → queued drains
    const sent = x.posts.filter((p) => p.url === "/param" && p.body.block === 1).map((p) => p.body.value);
    ok("S38 the released value still reaches the device after the switch", sent.includes(finalVal), "device got " + JSON.stringify(sent) + "; final=" + finalVal);
  }
  { // S39: a bypass change keeps the FOCUSED block's tile in sync, not just the focus panel.
    const x = boot();
    x.sse(State([Blk({ block: 1, model: "AmpA", name: "A" }, [P_({ value: 0.5 })]),
                 Blk({ block: 2, model: "AmpB", name: "B" }, [P_({ value: 0.5 })])]));
    ok("S39 block A is the focused block (registered in B)", !!x.api.B.get("0/1"), [...x.api.B.keys()].join());
    x.sse({ t: "bypass", path: 0, block: 1, enabled: false });   // device bypasses the focused block
    ok("S39 the focused block's tile shows bypassed (.off)", x.tileOf("0/1")._cls.has("off"), [...x.tileOf("0/1")._cls].join(" "));
    x.sse({ t: "bypass", path: 0, block: 2, enabled: false });
    ok("S39 a non-focused block's tile also shows bypassed", x.tileOf("0/2")._cls.has("off"), [...x.tileOf("0/2")._cls].join(" "));
  }
  { // S40: after a user drag-reorder, selection follows the dragged block to its new slot.
    const mk = (s, n) => Blk({ block: s, model: "M" + n, name: n }, [P_({ value: 0.5 })]);
    const x = boot();
    x.sse(State([mk(1, "A"), mk(2, "B"), mk(3, "C")]));
    const focusName = () => { const f = x.ids.rig.find((e) => e.id === "focus")[0]; const n = f && f.find((e) => e._cls.has("nm"))[0]; return n && n.textContent; };
    ok("S40 focus starts on the first block A", focusName() === "A", focusName());
    x.tileOf("0/1").fire("dragstart", { dataTransfer: { setData() {} } });
    x.tileOf("0/3").fire("drop", { preventDefault() {} });        // drag A onto slot 3
    x.sse(State([mk(1, "B"), mk(2, "C"), mk(3, "A")]));           // device re-sends the reorder (A now at slot 3)
    ok("S40 focus follows the dragged block to its new slot", focusName() === "A", "focus shows " + focusName());
  }

  { // S41: I/O metering — the flow pane polls /meters/reading and drives the
    //       input/output tiles' level bars (read-only telemetry, no writes).
    const x = boot();
    x.sse(State([Blk({ block: 0, category: "input", name: "In" }, []),
                 Blk({ block: 13, category: "output", name: "Out" }, [])]));
    const mi = [...x.intervals.values()][0];            // the flow meter poll
    mi(); await x.tick();
    const req = x.posts.find((p) => p.url === "/meters/reading");
    ok("S41 the flow pane polls /meters/reading", !!req, JSON.stringify(x.posts.map((p) => p.url)));
    req.reply(200, { connected: true, input: 0.5, output: 0.8, clip_in: false, clip_out: true });
    await x.tick();
    const inFill = x.tileOf("0/0").find((e) => e._cls.has("ti-meter-fill"))[0];
    const outMeter = x.tileOf("0/13").find((e) => e._cls.has("ti-meter"))[0];
    ok("S41 the input meter bar fills to the level", inFill.style.width === "50%", inFill.style.width);
    ok("S41 the output meter flags clip", outMeter._cls.has("clip"), [...outMeter._cls].join(" "));
    // a stale reading drops the bars to zero (no frozen meter)
    mi(); await x.tick();
    x.posts.filter((p) => p.url === "/meters/reading").slice(-1)[0].reply(200, { connected: true, stale: true });
    await x.tick();
    ok("S41 a stalled stream recentres the bars", inFill.style.width === "0%" && !outMeter._cls.has("clip"), inFill.style.width);
  }

  { // S42: drag-to-split — dropping a splittable lane-A block on its path's
    //       parallel drop zone calls /add-split (the Stadium "drag to go
    //       parallel" gesture; cross-lane MOVE between existing lanes is engine-gated).
    const x = boot();
    x.sse(State([Blk({ block: 2, category: "amp", name: "Amp" }, [P_({ value: 0.5 })]),
                 Blk({ block: 3, category: "distortion", name: "Dist" }, [P_({ value: 0.5 })])]));
    const zone = x.ids.rig.find((e) => e._cls.has("splitzone"))[0];
    ok("S42 a parallel drop zone exists for a non-split path", !!zone, "no splitzone");
    ok("S42 the zone is hidden until a split-drag", !zone._cls.has("armed"), [...zone._cls].join(" "));
    x.tileOf("0/2").fire("dragstart", { dataTransfer: { setData() {} } });
    ok("S42 dragging a splittable block arms the zone", zone._cls.has("armed"), [...zone._cls].join(" "));
    zone.fire("drop", { preventDefault() {} });
    const req = x.posts.find((p) => p.url === "/add-split");
    ok("S42 dropping on the zone splits that block onto a parallel lane",
      !!req && req.body.path === 0 && req.body.block === 2, JSON.stringify(req && req.body));
    // a non-splittable block (lane-A slot 12) must NOT arm the zone or split
    const y = boot();
    y.sse(State([Blk({ block: 2, category: "amp", name: "Amp" }, [P_({ value: 0.5 })]),
                 Blk({ block: 12, category: "cab_ir_interp", name: "Cab" }, [P_({ value: 0.5 })])]));
    const yz = y.ids.rig.find((e) => e._cls.has("splitzone"))[0];
    ok("S42 the zone still renders (a splittable block exists on the path)", !!yz, "no zone");
    y.tileOf("0/12").fire("dragstart", { dataTransfer: { setData() {} } });
    ok("S42 dragging a slot-12 block does not arm the zone", !yz._cls.has("armed"), [...yz._cls].join(" "));
    yz.fire("drop", { preventDefault() {} });
    ok("S42 dropping a slot-12 block does not split", !y.posts.find((p) => p.url === "/add-split"), "unexpected /add-split");
  }

  { // S43: cross-lane MOVE — on a single-split path whose lane B is 1-wide, a
    //      lane-A block AFTER the join drags onto the lane-B drop zone →
    //      /move-into-split (server derives the split/join + commits the bundle).
    //      The lane-B block lives in the parallel segment's `b` (extra stays
    //      empty), exactly as build_chain emits it — the earlier S43 wrongly put
    //      it in `extra`, which hid that the zone never rendered on a real split.
    const splitState = () => State([], { paths: [{ path: 0, segments: [
        { kind: "block", block: Blk({ block: 1, category: "distortion", name: "Dist" }, [P_()]) },  // before split — NOT a source
        { split: Blk({ block: 2, category: "split", name: "Split" }, []), a: [],
          b: [{ kind: "block", block: Blk({ block: 16, row: 1, category: "cab_ir_interp", name: "Cab" }, [P_()]) }],
          join: Blk({ block: 3, category: "join", name: "Join" }, []) },
        { kind: "block", block: Blk({ block: 4, category: "amp", name: "Amp" }, [P_()]) }],  // AFTER join — a valid source
      extra: [] }] });
    const x = boot();
    x.sse(splitState());
    const lz = x.ids.rig.find((e) => e._cls.has("lanebzone"))[0];
    ok("S43 a lane-B drop zone renders on a real split (extra empty)", !!lz, "no lanebzone");
    ok("S43 the zone is hidden until a lane-B drag", !lz._cls.has("armed"), [...lz._cls].join(" "));
    x.tileOf("0/4").fire("dragstart", { dataTransfer: { setData() {} } });   // amp, after the join
    ok("S43 dragging an after-join block arms the lane-B zone", lz._cls.has("armed"), [...lz._cls].join(" "));
    lz.fire("drop", { preventDefault() {} });
    const req = x.posts.find((p) => p.url === "/move-into-split");
    ok("S43 dropping sends path+src (server derives the split)",
      !!req && req.body.path === 0 && req.body.src === 4
        && req.body.split_col === undefined && req.body.join_col === undefined, JSON.stringify(req && req.body));
    // a BEFORE-join block is not a valid source: dragging it doesn't arm or move
    const y = boot();
    y.sse(splitState());
    const yz = y.ids.rig.find((e) => e._cls.has("lanebzone"))[0];
    y.tileOf("0/1").fire("dragstart", { dataTransfer: { setData() {} } });
    ok("S43 a before-join block does not arm the zone", !yz._cls.has("armed"), [...yz._cls].join(" "));
    yz.fire("drop", { preventDefault() {} });
    ok("S43 and sends no move", !y.posts.find((p) => p.url === "/move-into-split"), "unexpected /move-into-split");
  }

  { // S44: dual-cab 2nd-cab param is EDITABLE — its knob writes /second-cab-param
    //      (sub=1), keyed path/block/s1/pid so it never collides with the 1st cab's
    //      identical pids (previously the 2nd cab was shown read-only, hgc-5gh).
    const x = boot();
    x.sse(State([Blk({ block: 1, category: "cab", name: "Cab",
      second_model: { name: "Cab B", model: "HD2_X", model_id: 453, read_only: false,
        params: [{ pid: 4, name: "Angle", label: "Angle", type: "f", ctl: "knob",
                   value: 45, min: 0, max: 90, default: 45, internal: false }] } },
      [{ pid: 4, name: "Level", label: "Level", type: "f", ctl: "knob",
         value: 5, min: 0, max: 10, default: 5, internal: false }])]));
    const k = x.knobOf("0/1/s1/4");
    ok("S44 the 2nd cab renders an editable knob (not read-only)", !!(k && k.root), "no 2nd-cab knob");
    k.root.fire("keydown", { key: "ArrowUp" });
    const req = x.posts.find((p) => p.url === "/second-cab-param");
    ok("S44 the 2nd-cab knob writes /second-cab-param with its own pid",
      !!req && req.body.path === 0 && req.body.block === 1 && req.body.pid === 4, JSON.stringify(req && req.body));
    ok("S44 the 1st cab's identical pid 4 is untouched (no /param leak)",
      !x.posts.find((p) => p.url === "/param" && p.body.pid === 4), "leaked to the 1st cab");
    // undo regression (applyHist fix): undoing the scparam edit must revert the
    // 2nd-cab knob, NOT fall through to the 1st cab's identical pid.
    if (req) req.reply(200, { ok: true, value: req.body.value });
    await x.tick();
    ok("S44 the scparam edit left one scparam undo entry",
      x.api.undoS.length === 1 && x.api.undoS[0].kind === "scparam", JSON.stringify(x.api.undoS));
    x.api.undo();
    ok("S44 undo reverts the 2nd-cab knob to 45", x.api.P.get("0/1/s1/4").p.value === 45,
      x.api.P.get("0/1/s1/4").p.value);
    ok("S44 undo did NOT touch the 1st cab's pid-4 knob (still 5)",
      x.api.P.get("0/1/4").p.value === 5, x.api.P.get("0/1/4").p.value);
    ok("S44 the undo write went to /second-cab-param",
      x.posts.filter((p) => p.url === "/second-cab-param").length === 2, "undo wrote elsewhere");
  }

  { // S45: slider view renders a horizontal slider for continuous params, and a
    // horizontal drag writes + records exactly as the knob does (Stadium parity)
    const x = boot(); x.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    ok("S45 default view renders a knob (.k)", x.knobOf(K).root._cls.has("k"), [...x.knobOf(K).root._cls]);
    x.api.knobview = "sliders";            // flip and re-render the focused block
    const root = x.ids.rig.find((e) => e.dataset && e.dataset.k === K)[0];
    ok("S45 slider view re-renders the control as a .sld", !!root && root._cls.has("sld"), root && [...root._cls]);
    const svg = root.children[0];
    svg.fire("pointerdown", { button: 0, clientX: 300, pointerId: 1 });
    svg.fire("pointermove", { clientX: 360, pointerId: 1 });
    const moved = x.api.P.get(K).p.value;
    svg.fire("pointerup", { pointerId: 1 });
    ok("S45 a rightward drag raises the value", moved > 0.5, moved);
    ok("S45 the whole slider drag is ONE undo step", x.api.undoS.length === 1 && x.api.undoS[0].before === 0.5, JSON.stringify(x.api.undoS));
    ok("S45 the write carries the structure revision", x.posts.length >= 1 && x.posts[0].body.rev === 1, JSON.stringify(x.posts[0] && x.posts[0].body));
    // a click that moves nothing writes nothing (same contract as the knob)
    const x2 = boot(); x2.api.knobview = "sliders"; x2.sse(State([Blk({}, [P_({ value: 0.5 })])]));
    const s2 = x2.ids.rig.find((e) => e.dataset && e.dataset.k === K)[0].children[0];
    s2.fire("pointerdown", { button: 0, clientX: 300, pointerId: 1 }); s2.fire("pointerup", { pointerId: 1 });
    ok("S45 a slider click that moves nothing writes nothing", x2.posts.length === 0 && x2.api.undoS.length === 0, x2.posts.length);
  }

  { // S46: the horizontal canvas scrolls INTERNALLY now (the window scrolled
    //       before). render() rebuilds #flowcanvas on every full state push, so
    //       it must save+restore scrollLeft — else a device-follow or any edit
    //       while scrolled right on a long chain snaps the view back to block 1.
    const x = boot();
    x.sse(State([Blk({ block: 1 }, [P_({})]), Blk({ block: 2 }, [P_({})]), Blk({ block: 3 }, [P_({})])]));
    const cv1 = x.document.getElementById("flowcanvas");
    ok("S46 the canvas exists to scroll", !!cv1, "no flowcanvas");
    cv1.scrollLeft = 500;
    x.sse(State([Blk({ block: 1 }, [P_({})]), Blk({ block: 2 }, [P_({})]), Blk({ block: 3 }, [P_({})])], { rev: 2 }));
    const cv2 = x.document.getElementById("flowcanvas");
    ok("S46 a full re-render is a NEW canvas element", cv2 && cv2 !== cv1, "same element");
    ok("S46 a full re-render restores the horizontal scroll position", cv2.scrollLeft === 500, "scrollLeft=" + cv2.scrollLeft);
  }

  { // S47: above the width breakpoint the library becomes a PERSISTENT left
    //       pane (Stadium's preset column), reusing the drawer renderer; the
    //       now-redundant Library toggle hides. Narrowing restores the drawer.
    const x = boot();
    x.sse(State([Blk({}, [P_({})])]));
    ok("S47 narrow by default: left pane hidden, toggle shown",
      x.ids.leftpane.hidden && !x.ids.libtoggle.hidden, "leftHidden=" + x.ids.leftpane.hidden + " toggleHidden=" + x.ids.libtoggle.hidden);
    x.setWide(true);                                     // cross the breakpoint
    const req = x.posts.find((p) => p.url === "/library/state");
    ok("S47 going wide fetches the library for the left pane", !!req, "no /library/state fetch");
    req.reply(200, { tones: [{ cid: 5, name: "My Tone", hsp: "x.hsp", setlists: [] }], setlists: [], irs: [] });
    await x.tick();
    ok("S47 the left pane is visible when wide", !x.ids.leftpane.hidden, "leftpane hidden");
    ok("S47 the preset list renders into the left pane",
      x.ids.leftbody.find((e) => e._cls && e._cls.has("ltones")).length === 1, "no ltones in leftbody");
    ok("S47 the Library toggle hides when the pane is persistent", x.ids.libtoggle.hidden, "toggle still shown");
    x.setWide(false);                                    // narrow again -> drawer fallback
    ok("S47 narrowing hides the left pane and restores the toggle",
      x.ids.leftpane.hidden && !x.ids.libtoggle.hidden, "leftHidden=" + x.ids.leftpane.hidden + " toggleHidden=" + x.ids.libtoggle.hidden);
  }

  { // S48: a library mutation from the WIDE left pane must refresh THAT pane,
    //       never pop the drawer open (the one-mount-at-a-time invariant — the
    //       S2 review's C1, where the refresh callbacks hardcoded openDrawer).
    const x = boot();
    x.sse(State([Blk({}, [P_({})])]));
    x.setWide(true);
    x.posts.find((p) => p.url === "/library/state").reply(200,
      { tones: [], setlists: [{ cid: 1, name: "Live", members: [] }], irs: [] });
    await x.tick();
    const del = x.ids.leftbody.find((e) => e.tagName === "button" && e.textContent === "Delete")[0];
    ok("S48 the left pane shows a setlist Delete control", !!del, "no Delete button");
    del.onclick();                                               // delete the setlist from the left pane
    x.posts.find((p) => p.url === "/library/setlist_delete").reply(200, { ok: true });
    await x.tick();
    const f2 = x.posts.filter((p) => p.url === "/library/state")[1];
    ok("S48 the mutation re-fetches the library", !!f2, "no refresh fetch");
    f2.reply(200, { tones: [{ slug: "t", name: "T", variants: [] }], setlists: [], irs: [] });
    await x.tick();
    const leftTones = x.ids.leftbody.find((e) => e._cls && e._cls.has("ltones")).length;
    const drawerTones = x.ids.drawerbody.find((e) => e._cls && e._cls.has("ltones")).length;
    ok("S48 the refresh renders into the LEFT pane, not the drawer",
      leftTones === 1 && drawerTones === 0, "left=" + leftTones + " drawer=" + drawerTones);
  }

  { // S49: the persistent model pane (wide / 4-pane) ADDS a model to the selected
    //       block's path, and SWAPS when the picked model is the selected block's
    //       own category — reusing the existing /add-block + /swap endpoints.
    const x = boot();
    x.sse(State([Blk({ block: 1, category: "amp", model: "HD2_A", name: "Amp" }, [P_({})])]));
    x.setWide(true);                                  // the harness mq flips both breakpoints -> has-panes
    const mreq = x.posts.find((p) => p.url === "/models");
    ok("S49 the model pane fetches the catalog", !!mreq, "no /models fetch");
    mreq.reply(200, { amp: [{ id: "HD2_B", name: "Brit Plexi" }], delay: [{ id: "HD2_D", name: "Tape" }] });
    await x.tick();
    const models = x.ids.modelpane.find((e) => e.tagName === "button" && e._cls && e._cls.has("pz"));
    ok("S49 the model pane lists models", models.length >= 1, "no model buttons");
    // selected block is an amp; the pane opens on the amp category -> picking an amp SWAPS it
    const brit = models.find((b) => b.textContent === "Brit Plexi");
    brit.onclick();
    const swap = x.posts.find((p) => p.url === "/swap");
    ok("S49 a same-category pick swaps the selected block", !!swap && swap.body.block === 1 && swap.body.model_id === "HD2_B", JSON.stringify(swap && swap.body));
    swap.reply(200, { ok: true }); await x.tick();   // clear structBusy (structural edits are serialized)
    // switch to the delay category and pick -> ADD to the selected block's path (0)
    const delayTab = x.ids.modelpane.find((e) => e._cls && e._cls.has("cat-tab")).find((t) => t.textContent.toLowerCase().includes("delay"));
    delayTab.onclick();
    const tape = x.ids.modelpane.find((e) => e.tagName === "button" && e._cls && e._cls.has("pz")).find((b) => b.textContent === "Tape");
    tape.onclick();
    const add = x.posts.find((p) => p.url === "/add-block");
    ok("S49 a different-category pick adds to the selected block's path", !!add && add.body.path === 0 && add.body.model_id === "HD2_D", JSON.stringify(add && add.body));
  }

  { // S50: the persistent model pane serializes structural edits (structBusy) —
    //       an accidental double-click must not append TWO blocks. Every other
    //       structural edit routes through structSend; the pane must too.
    const x = boot();
    x.sse(State([Blk({ block: 1, category: "amp", name: "Amp" }, [P_({})])]));
    x.setWide(true);
    x.posts.find((p) => p.url === "/models").reply(200, { delay: [{ id: "HD2_D", name: "Tape" }] });
    await x.tick();
    const tape = x.ids.modelpane.find((e) => e.tagName === "button" && e._cls && e._cls.has("pz")).find((b) => b.textContent === "Tape");
    tape.onclick(); tape.onclick();               // double-click before the first add's re-render lands
    const adds = x.posts.filter((p) => p.url === "/add-block");
    ok("S50 a double-click in the model pane fires only ONE add", adds.length === 1, "add count=" + adds.length);
  }

  console.log(fails ? `\n${fails} FAILED` : "\nall page scenarios pass");
  process.exit(fails ? 1 : 0);
})();
