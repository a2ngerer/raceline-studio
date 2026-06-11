/* UI wiring: toolbar, inspector, legend, drawers (settings / optimizer /
   upload), status bar, SSE job feedback, crash-restore banner, keyboard,
   GSAP boot + micro-animations. */

import { api, connectSSE, onConn } from "./api.js";
import { COL, fmt } from "./geometry.js";
import {
  S, bus, clearLocal, clonePts, historySizes, lapEstimate, loadLocal,
  markDirty, markSaved, applySession, pushHistory, redo, savePayload,
  setGhost, setMode, setProfile, setPts, undo,
} from "./store.js";
import {
  applyOptimize, cancelDraft, clearRegion, clearZones, computeProfile,
  deleteZone, finishDraft, runCenterlineNow, runFeasible, runOptimize,
  scheduleCenterline, setToast, smoothLine,
} from "./tools.js";
import { fit, mapPng, renderStrip, requestRender } from "./view.js";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};

/* ---------------- toast ---------------- */
let toastTimer = null;
export function toast(msg, type = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = type === "err" ? "err" : type === "ok" ? "okk" : "";
  clearTimeout(toastTimer);
  if (window.gsap) {
    gsap.fromTo(t, { opacity: 0, y: 14, scale: 0.97 },
      { opacity: 1, y: 0, scale: 1, duration: 0.32, ease: "back.out(1.6)" });
  } else t.style.opacity = 1;
  toastTimer = setTimeout(() => {
    if (window.gsap) gsap.to(t, { opacity: 0, y: 8, duration: 0.3 });
    else t.style.opacity = 0;
  }, type === "err" ? 4200 : 2200);
}
setToast(toast);

/* ---------------- mode switching ---------------- */
const MODES = ["shape", "speed", "carpet", "cert", "map"];
function selectMode(m) {
  setMode(m);
  document.querySelectorAll("#modes button").forEach((b) =>
    b.classList.toggle("sel", b.dataset.mode === m));
  buildCtx();
  buildLegend();
  buildInspector();
  if (m === "speed" && (!S.V || S.vStale)) {
    computeProfile({ silent: true });
  }
}

/* ---------------- contextual tools ---------------- */
function buildCtx() {
  const c = $("ctx");
  c.innerHTML = "";
  const brushSlider = (min, max, get, set) => {
    const wrap = el("div", "ctx");
    wrap.append(el("label", "mini", "BRUSH"));
    const sl = el("input");
    sl.type = "range"; sl.min = min; sl.max = max; sl.value = get();
    sl.addEventListener("input", () => set(+sl.value));
    wrap.append(sl);
    return wrap;
  };

  if (S.mode === "shape") {
    const sm = el("button", "btn small", "SMOOTH");
    sm.onclick = smoothLine;
    c.append(sm, brushSlider(1, 40, () => S.brush, (v) => S.brush = v));
  } else if (S.mode === "speed") {
    const seg = el("div", "seg");
    for (const [dir, name] of [[1, "BOOST"], [-1, "REDUCE"]]) {
      const b = el("button", dir === S.speedDir ? "sel" : "", name);
      b.onclick = () => {
        S.speedDir = dir;
        seg.querySelectorAll("button").forEach((x) =>
          x.classList.toggle("sel", x === b));
        requestRender();
      };
      seg.append(b);
    }
    c.append(seg, brushSlider(1, 40, () => S.brush, (v) => S.brush = v));
  } else if (S.mode === "carpet" || S.mode === "cert") {
    const fin = el("button", "btn small", "FINISH ZONE");
    fin.onclick = finishDraft;
    const can = el("button", "btn small", "CANCEL");
    can.onclick = cancelDraft;
    const clr = el("button", "btn small danger", "CLEAR ALL");
    clr.onclick = () => {
      if (confirm("Delete ALL zones of this mode?")) clearZones();
    };
    c.append(fin, can, clr);
  } else if (S.mode === "map") {
    const seg = el("div", "seg");
    for (const [tool, name] of [["wall", "WALL"], ["free", "FREE"],
      ["unknown", "GREY"], ["fill", "FILL"], ["region", "REGION"]]) {
      const b = el("button", tool === S.mapTool ? "sel" : "", name);
      b.onclick = () => {
        S.mapTool = tool;
        seg.querySelectorAll("button").forEach((x) =>
          x.classList.toggle("sel", x === b));
      };
      seg.append(b);
    }
    const clrRgn = el("button", "btn small", "CLR RGN");
    clrRgn.title = "Remove the centerline region — compute on the full map";
    clrRgn.onclick = clearRegion;
    const live = el("button", "btn small" + (S.liveCenterline ? " primary" : ""),
      "LIVE CL");
    live.title = "Recompute the centerline automatically after every map edit";
    live.onclick = () => {
      S.liveCenterline = !S.liveCenterline;
      live.classList.toggle("primary", S.liveCenterline);
      if (S.liveCenterline) scheduleCenterline(50);
    };
    const now = el("button", "btn small", "CL NOW");
    now.title = "Recompute the centerline now";
    now.onclick = runCenterlineNow;
    const use = el("button", "btn small", "USE CL");
    use.title = "Replace the working line with the live centerline";
    use.onclick = () => {
      const cl = S.lines["centerline (live)"];
      if (!cl) { toast("No live centerline yet"); return; }
      pushHistory();
      setPts(clonePts(cl));
      S.activeLine = "centerline (live)";
      refreshLineSel();
      toast("Working line = live centerline");
    };
    const saveM = el("button", "btn small", "SAVE MAP");
    saveM.title = "Write the edited map PNG (+yaml) next to the original";
    saveM.onclick = async () => {
      const overwrite = confirm(
        "OK = overwrite the original map.png\nCancel = save a *_edited.png copy");
      try {
        const r = await api.saveMap(mapPng(), overwrite);
        toast(`Map saved → ${r.path.split("/").pop()}`, "ok");
      } catch (e) { toast(`Map save failed: ${e.message}`, "err"); }
    };
    c.append(seg, brushSlider(2, 30, () => S.mapBrushPx,
      (v) => S.mapBrushPx = v), clrRgn, live, now, use, saveM);
  }
}

/* ---------------- legend ---------------- */
function buildLegend() {
  const L = $("legend");
  const sw = (c) => `<span class="sw" style="background:${c}"></span>`;
  const ring = (c) => `<span class="ring" style="box-shadow:0 0 0 2px ${c} inset"></span>`;
  const dot = (c) => `<span class="dot" style="background:${c}"></span>`;
  let rows = [];
  if (S.mode === "shape" || S.mode === "map") {
    rows = [
      [sw(COL.bad), "below R<sub>min</sub> · undrivable"],
      [sw(COL.warn), "tight for pure pursuit"],
      [sw(COL.ok), "safe margin"],
    ];
    if (S.mode === "map") {
      rows.push([sw(COL.acc), "live centerline (ghost)"]);
      rows.push([sw("rgba(255,179,71,.85)"),
        "compute region (REGION tool)"]);
    }
  } else if (S.mode === "speed") {
    rows = [
      [sw(COL.ok), "slow"], [sw("#ffe14d"), "medium"], [sw(COL.bad), "fast"],
      [ring(COL.warn), "over grip µ"], [ring(COL.bad), "over PP cap"],
    ];
  } else if (S.mode === "carpet") {
    rows = [[dot("rgba(143,123,255,.7)"), "carpet zone (stronger µ)"],
      [ring(COL.bad), "✕ deletes a zone"]];
  } else if (S.mode === "cert") {
    rows = [[dot("rgba(255,91,122,.8)"), "low certainty → reactive"],
      [dot("rgba(120,126,134,.8)"), "neutral"],
      [dot("rgba(91,140,255,.8)"), "high certainty → pure pursuit"],
      [dot("transparent"), "dashed = hard PP lock"]];
  }
  L.innerHTML = rows.map(([i, t]) => `<div class="it">${i}${t}</div>`).join("");
}

/* ---------------- inspector ---------------- */
function buildInspector() {
  const I = $("inspector");
  I.innerHTML = "";
  if (S.mode === "shape" || S.mode === "map") {
    I.innerHTML = `
      <div class="ins-k">MIN RADIUS</div>
      <div class="ins-big"><span id="insMinR">—</span><span class="u">m</span></div>
      <div class="ins-row"><span>below R_min</span><b id="insBad">0</b></div>
      <div class="ins-row"><span>length</span><b id="insLen">—</b></div>
      <div class="ins-row"><span>points</span><b id="insN">—</b></div>
      ${S.mode === "map" ? `<div class="ins-row"><span>centerline</span><b id="insCl">—</b></div>
      <div class="ins-row"><span>region</span><b id="insRgn">—</b></div>` : ""}`;
  } else if (S.mode === "speed") {
    I.innerHTML = `
      <div class="ins-k">SPEED @ CURSOR</div>
      <div class="ins-big"><span id="insVcur">—</span><span class="u">m/s</span></div>
      <div class="ins-row"><span>range</span><b id="insVrng">—</b></div>
      <div class="ins-row"><span>over grip</span><b id="insOg">0</b></div>
      <div class="ins-row"><span>over PP cap</span><b id="insOp">0</b></div>
      <div class="ins-row"><span>est. lap</span><b id="insLap">—</b></div>`;
  } else if (S.mode === "carpet") {
    I.innerHTML = `
      <div class="ins-k">CARPET ZONES</div>
      <div class="ins-big"><span id="insZn">0</span></div>
      <div class="ins-row"><span>carpet µ</span><b id="insCmu">—</b></div>
      <div class="ins-row"><span>grip ratio</span><b id="insRatio">—</b></div>
      <div class="d-note" style="margin-top:10px">Click the map to outline a
      higher-grip patch. Close the loop on its first point or press Enter.</div>`;
  } else if (S.mode === "cert") {
    I.innerHTML = `
      <div class="ins-k">CERTAINTY</div>
      <div id="certZoneBox"></div>`;
  }
  updateInspector();
}

function updateInspector() {
  const m = S.meta;
  if (!m) return;
  if (S.mode === "shape" || S.mode === "map") {
    let minR = Infinity, bad = 0;
    for (const r of S.radii) { if (r < minR) minR = r; if (r < m.R_min) bad++; }
    const len = (() => {
      let L = 0;
      for (let i = 1; i < S.pts.length; i++) {
        L += Math.hypot(S.pts[i][0] - S.pts[i - 1][0],
          S.pts[i][1] - S.pts[i - 1][1]);
      }
      return L;
    })();
    const e1 = $("insMinR");
    if (e1) {
      e1.textContent = fmt(Math.min(minR, 99), 2);
      e1.style.color = minR < m.R_min ? COL.bad
        : minR < m.R_safe ? COL.warn : COL.ok;
    }
    if ($("insBad")) {
      $("insBad").textContent = bad;
      $("insBad").className = bad > 0 ? "bad" : "ok";
    }
    if ($("insLen")) $("insLen").textContent = fmt(len, 1) + " m";
    if ($("insN")) $("insN").textContent = S.pts.length;
    if ($("insCl")) {
      const j = S.job.centerline;
      $("insCl").textContent = j
        ? (j.status === "running" ? "computing…" : j.status) : "idle";
    }
    if ($("insRgn")) {
      $("insRgn").textContent = S.clRegion ? "custom" : "full map";
      $("insRgn").className = S.clRegion ? "warn" : "";
    }
  } else if (S.mode === "speed") {
    const n = S.pts.length;
    const have = S.V && S.V.length === n && !S.vStale;
    if ($("insVcur")) {
      $("insVcur").textContent = have && S.hover >= 0
        ? fmt(S.V[S.hover], 2) : "—";
    }
    if (have) {
      let lo = Infinity, hi = -Infinity, og = 0, op = 0;
      const g = m.g || 9.81;
      for (let i = 0; i < n; i++) {
        const v = S.V[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
        if (v > Math.sqrt(S.mupp * g * S.radii[i]) * 1.03) op++;
        else if (v > Math.sqrt(S.mu * g * S.radii[i]) * 1.03) og++;
      }
      if ($("insVrng")) $("insVrng").textContent = `${fmt(lo, 1)}–${fmt(hi, 1)}`;
      if ($("insOg")) {
        $("insOg").textContent = og;
        $("insOg").className = og ? "warn" : "ok";
      }
      if ($("insOp")) {
        $("insOp").textContent = op;
        $("insOp").className = op ? "bad" : "ok";
      }
      const lap = lapEstimate();
      if ($("insLap")) $("insLap").textContent = lap ? fmt(lap, 2) + " s" : "—";
    } else if ($("insVrng")) {
      $("insVrng").textContent = S.vStale ? "stale — COMPUTE" : "—";
    }
  } else if (S.mode === "carpet") {
    if ($("insZn")) $("insZn").textContent = S.carpet.zones.length;
    if ($("insCmu")) $("insCmu").textContent = fmt(S.muCarpet, 2);
    if ($("insRatio")) {
      $("insRatio").textContent = `${fmt(S.mu, 2)} → ${fmt(S.muCarpet, 2)}`;
    }
  } else if (S.mode === "cert") {
    const box = $("certZoneBox");
    if (!box) return;
    if (S.selZone && S.selZone.kind === "cert"
        && S.certainty.zones[S.selZone.idx]) {
      const z = S.certainty.zones[S.selZone.idx];
      box.innerHTML = `
        <div class="ins-big"><span>${fmt(z.score, 2)}</span></div>
        <div class="z-field">
          <label><span>score (0=reactive · 1=PP)</span><b>${fmt(z.score, 2)}</b></label>
          <input type="range" id="zScore" min="0" max="1" step="0.01" value="${z.score}">
        </div>
        <label class="z-check"><input type="checkbox" id="zLock"
          ${z.lock ? "checked" : ""}> Hard PP lock</label>
        <label class="z-check"><input type="checkbox" id="zForce"
          ${z.force ? "checked" : ""}> Force reactive</label>
        <div class="d-row"><button class="btn small danger" id="zDel">DELETE ZONE</button></div>`;
      $("zScore").addEventListener("input", (e) => {
        z.score = +e.target.value;
        markDirty(); bus.emit("zones");
      });
      $("zLock").addEventListener("change", (e) => {
        z.lock = e.target.checked; markDirty(); bus.emit("zones");
      });
      $("zForce").addEventListener("change", (e) => {
        z.force = e.target.checked; markDirty(); bus.emit("zones");
      });
      $("zDel").addEventListener("click", () => {
        deleteZone("cert", S.selZone.idx);
      });
    } else {
      box.innerHTML = `<div class="ins-big"><span>${S.certainty.zones.length}</span>
        <span class="u">zones</span></div>
        <div class="d-note" style="margin-top:8px">Click the map to draw a zone,
        click a zone to edit its score. &gt;0.5 prefers pure pursuit,
        &lt;0.5 prefers the reactive controller.</div>`;
    }
  }
}

/* ---------------- line selector ---------------- */
export function refreshLineSel() {
  const sel = $("lineSel");
  const names = Object.keys(S.lines);
  sel.innerHTML = names.map((n) =>
    `<option value="${n}"${n === S.activeLine ? " selected" : ""}>${n}</option>`)
    .join("");
}

/* ---------------- status bar ---------------- */
function updateJobUI(ev) {
  const txt = $("jobText"), prog = $("jobProg");
  const running = ev && ev.status === "running";
  if (ev) txt.textContent = ev.message || "";
  prog.classList.toggle("on", !!running);
  if (running && window.gsap) {
    gsap.to(prog.firstElementChild,
      { width: `${Math.round((ev.progress || 0) * 100)}%`, duration: 0.25 });
  }
  if (ev && ev.status !== "running") {
    setTimeout(() => {
      const cur = S.job[ev.kind];
      if (cur && cur.id === ev.id && cur.status !== "running") {
        txt.textContent = "";
        prog.classList.remove("on");
      }
    }, 2600);
  }
}

/* ---------------- lap chip ---------------- */
const lapState = { v: 0 };
function updateLap() {
  const lap = lapEstimate();
  const elv = $("lapVal");
  if (!lap) { elv.textContent = "—"; return; }
  if (window.gsap) {
    gsap.to(lapState, {
      v: lap, duration: 0.5, ease: "power2.out",
      onUpdate: () => { elv.textContent = lapState.v.toFixed(2) + " s"; },
    });
  } else elv.textContent = lap.toFixed(2) + " s";
}

/* ---------------- drawers ---------------- */
function closeDrawers(except) {
  for (const id of ["settingsDrawer", "optimizeDrawer", "uploadDrawer"]) {
    if (id !== except) $(id).classList.remove("open");
  }
}
function toggleDrawer(id) {
  const d = $(id);
  const willOpen = !d.classList.contains("open");
  closeDrawers(id);
  d.classList.toggle("open", willOpen);
  if (willOpen && window.gsap) {
    gsap.fromTo(d, { x: 26, opacity: 0 },
      { x: 0, opacity: 1, duration: 0.32, ease: "power3.out" });
  }
  return willOpen;
}

function buildSettingsDrawer() {
  const d = $("settingsDrawer");
  d.innerHTML = `
    <h3>VEHICLE / PHYSICS <span class="x" data-x>✕</span></h3>
    <div class="d-field"><label><span>Grip µ</span><b id="muVal">${fmt(S.mu, 2)}</b></label>
      <input type="range" id="muSld" min="0.30" max="1.20" step="0.01" value="${S.mu}"></div>
    <div class="d-field"><label><span>Max speed</span><b id="vmaxVal">${fmt(S.vmax, 1)} m/s</b></label>
      <input type="range" id="vmaxSld" min="2" max="12" step="0.5" value="${S.vmax}"></div>
    <div class="d-field"><label><span>PP grip cap µ</span><b id="muppVal">${fmt(S.mupp, 2)}</b></label>
      <input type="range" id="muppSld" min="0.50" max="1.50" step="0.01" value="${S.mupp}"></div>
    <div class="d-field"><label><span>Carpet grip µ</span><b id="mucVal">${fmt(S.muCarpet, 2)}</b></label>
      <input type="range" id="mucSld" min="0.30" max="1.50" step="0.01" value="${S.muCarpet}"></div>
    <label class="z-check"><input type="checkbox" id="unrChk"
      ${S.unrestricted ? "checked" : ""}> Unrestricted lateral (expert)</label>
    <div class="d-note">a = µ·g feeds the velocity profile. The PP cap is
    display-only: it marks where the car clips speed at runtime. Carpet µ
    applies inside carpet zones only.</div>`;
  d.querySelector("[data-x]").onclick = () => closeDrawers();
  const wire = (sld, val, fmt2, set) => {
    $(sld).addEventListener("input", (e) => {
      const v = +e.target.value;
      $(val).textContent = fmt2(v);
      set(v);
      markDirty();
      bus.emit("settings");
      runFeasible(250);
    });
  };
  wire("muSld", "muVal", (v) => fmt(v, 2), (v) => S.mu = v);
  wire("vmaxSld", "vmaxVal", (v) => fmt(v, 1) + " m/s", (v) => S.vmax = v);
  wire("muppSld", "muppVal", (v) => fmt(v, 2), (v) => S.mupp = v);
  wire("mucSld", "mucVal", (v) => fmt(v, 2), (v) => {
    S.muCarpet = v; S.carpet.mu = v;
  });
  $("unrChk").addEventListener("change", (e) => {
    S.unrestricted = e.target.checked;
    markDirty(); runFeasible(150);
  });
}

let optMethod = "mincurv";
let optAutoApply = false;
function buildOptimizeDrawer() {
  const d = $("optimizeDrawer");
  const r = S.optResult;
  d.innerHTML = `
    <h3>RACELINE OPTIMIZER <span class="x" data-x>✕</span></h3>
    <div class="opt-methods">
      <div class="opt-m ${optMethod === "mincurv" ? "sel" : ""}" data-m="mincurv">
        <b>MIN CURVATURE</b><span>flattest line — highest apex speed (IQP)</span></div>
      <div class="opt-m ${optMethod === "mintime" ? "sel" : ""}" data-m="mintime">
        <b>MIN TIME</b><span>sweeps curvature/length blends, scores lap time</span></div>
    </div>
    <div class="d-row">
      <button class="btn primary" id="optRun">RUN</button>
      <button class="btn" id="optCancel">CANCEL</button>
    </div>
    <label class="z-check"><input type="checkbox" id="optAuto"
      ${optAutoApply ? "checked" : ""}> Auto-apply result to working line</label>
    <div class="d-status" id="optStatus"></div>
    <div class="opt-cands" id="optCands"></div>
    <div class="d-row" id="optApplyRow" style="display:${r ? "flex" : "none"}">
      <button class="btn primary" id="optApply">APPLY</button>
      <button class="btn" id="optDiscard">DISCARD</button>
    </div>
    <div class="d-note">The optimizer always starts from the current
    centerline corridor. Edit the map (mode 5) and the corridor follows.</div>`;
  d.querySelector("[data-x]").onclick = () => closeDrawers();
  d.querySelectorAll(".opt-m").forEach((c) => {
    c.onclick = () => {
      optMethod = c.dataset.m;
      d.querySelectorAll(".opt-m").forEach((x) =>
        x.classList.toggle("sel", x === c));
    };
  });
  $("optRun").onclick = () => {
    S.optResult = null;
    $("optCands").innerHTML = "";
    $("optApplyRow").style.display = "none";
    $("optStatus").className = "d-status busy";
    $("optStatus").textContent = "starting…";
    runOptimize(optMethod);
  };
  $("optCancel").onclick = () => api.cancel("optimize").catch(() => {});
  $("optAuto").onchange = (e) => optAutoApply = e.target.checked;
  $("optApply").onclick = () => S.optResult && applyOptimize(S.optResult);
  $("optDiscard").onclick = () => {
    S.optResult = null;
    setGhost("optimize", null);
    $("optApplyRow").style.display = "none";
    $("optStatus").textContent = "";
    $("optCands").innerHTML = "";
  };
}

function updateOptimizeDrawer(ev) {
  const st = $("optStatus");
  if (!st) return;
  if (ev.status === "running") {
    st.className = "d-status busy";
    st.textContent = ev.message;
  } else if (ev.status === "error") {
    st.className = "d-status bad";
    st.textContent = ev.message;
  } else if (ev.status === "cancelled") {
    st.className = "d-status";
    st.textContent = "cancelled";
  } else if (ev.status === "done" && ev.result) {
    st.className = "d-status ok";
    st.textContent = ev.result.method === "mintime"
      ? `done — lap ${ev.result.lap_time}s @ w=${ev.result.weight}`
      : `done — lap ${ev.result.lap_time}s`;
    if (ev.result.candidates) {
      $("optCands").innerHTML = ev.result.candidates.map((c) =>
        `<div class="opt-cand${c.w === ev.result.weight ? " best" : ""}">
          <span>w=${c.w}</span><span>${c.lap_time}s</span></div>`).join("");
    }
    $("optApplyRow").style.display = "flex";
  }
}

function buildUploadDrawer() {
  const d = $("uploadDrawer");
  const u = S.upload;
  d.innerHTML = `
    <h3>UPLOAD TO CAR <span class="x" data-x>✕</span></h3>
    <div class="d-field"><label><span>Car IP / host</span></label>
      <input type="text" id="upHost" value="${u.host || ""}" placeholder="192.168.104.10"></div>
    <div class="d-field"><label><span>SSH user</span></label>
      <input type="text" id="upUser" value="${u.user || ""}" placeholder="username"></div>
    <div class="d-field"><label><span>SSH port</span></label>
      <input type="number" id="upPort" value="${u.port || 22}"></div>
    <div class="d-field"><label><span>Target folder on car</span></label>
      <input type="text" id="upDest" value="${u.dest || ""}"
        placeholder="~/robot_ws/src/optimized_raceline_node/racelines/"></div>
    <label class="z-check"><input type="checkbox" id="upSide" checked>
      Include zone sidecars (carpet / certainty)</label>
    <label class="z-check"><input type="checkbox" id="upMap">
      Include map (png + yaml)</label>
    <div class="d-row">
      <button class="btn" id="upTest">TEST SSH</button>
      <button class="btn primary" id="upGo">SAVE + UPLOAD</button>
    </div>
    <div class="d-status" id="upStatus"></div>
    <div class="d-note">Uses scp with your SSH key (BatchMode). The line is
    saved to disk first, so the car gets exactly what you see. Rebuild /
    restart the node on the car to load the new raceline.</div>`;
  d.querySelector("[data-x]").onclick = () => closeDrawers();
  const read = () => {
    S.upload = {
      host: $("upHost").value.trim(),
      user: $("upUser").value.trim(),
      port: parseInt($("upPort").value, 10) || 22,
      dest: $("upDest").value.trim(),
    };
    S.sessionDirty = true;
    return S.upload;
  };
  for (const id of ["upHost", "upUser", "upPort", "upDest"]) {
    $(id).addEventListener("change", read);
  }
  $("upTest").onclick = async () => {
    const st = $("upStatus");
    st.className = "d-status busy"; st.textContent = "testing ssh…";
    try {
      const r = await api.upload({ test: true, ...read() });
      st.className = r.ok ? "d-status ok" : "d-status bad";
      st.textContent = r.detail;
    } catch (e) {
      st.className = "d-status bad"; st.textContent = e.message;
    }
  };
  $("upGo").onclick = async () => {
    const st = $("upStatus");
    st.className = "d-status busy"; st.textContent = "saving + uploading…";
    try {
      const r = await api.upload({
        ...read(),
        ...savePayload(),
        include_sidecars: $("upSide").checked,
        include_map: $("upMap").checked,
      });
      st.className = r.ok ? "d-status ok" : "d-status bad";
      st.textContent = r.detail;
      if (r.ok) {
        markSaved();
        toast("Uploaded to car ✓", "ok");
      }
    } catch (e) {
      st.className = "d-status bad"; st.textContent = e.message;
    }
  };
}

/* ---------------- history / revert ---------------- */
function updateHistButtons() {
  const h = historySizes();
  $("undoBtn").disabled = h.undo === 0;
  $("redoBtn").disabled = h.redo === 0;
  $("revertBtn").disabled = !S.savedPts || !S.dirty;
}

function revertToSaved() {
  if (!S.savedPts) { toast("Nothing saved yet"); return; }
  pushHistory(); // revert itself stays undoable
  if (S.savedV && S.savedV.length === S.savedPts.length) {
    setProfile(clonePts(S.savedPts), S.savedV);
  } else {
    setPts(clonePts(S.savedPts), { stale: false });
    S.V = null; S.vT = null; S.vStale = false;
    bus.emit("V");
  }
  S.activeLine = "edited (saved)";
  refreshLineSel();
  toast("Reverted to last saved CSV — undo to go back", "ok");
}

/* ---------------- save ---------------- */
async function doSave() {
  try {
    const res = await api.save(savePayload());
    markSaved();
    const file = (res.path || "").split("/").pop();
    toast(res.vmin != null
      ? `Saved ${file} (v ${fmt(res.vmin, 1)}–${fmt(res.vmax, 1)} m/s)`
      : `Saved ${file} (geometry only)`, "ok");
  } catch (e) {
    toast(`Save failed: ${e.message} — draft is autosaved`, "err");
  }
}

/* ---------------- SSE ---------------- */
function handleEvent(ev) {
  if (ev.type !== "job") return;
  S.job[ev.kind] = ev;
  updateJobUI(ev);
  bus.emit("job", ev);

  if (ev.kind === "centerline") {
    if (ev.status === "done" && ev.result) {
      S.lines["centerline (live)"] = ev.result.pts;
      setGhost("centerline", {
        pts: ev.result.pts, color: "rgba(69,227,255,.8)", dash: [6, 7],
        glow: "rgba(69,227,255,.35)", width: 1.8,
      });
      refreshLineSel();
      if (S.mode === "map") updateInspector();
    } else if (ev.status === "error") {
      toast(`Centerline: ${ev.message}`, "err");
    }
  }

  if (ev.kind === "optimize") {
    if (ev.preview && ev.preview.pts) {
      setGhost("optimize", {
        pts: ev.preview.pts, color: "rgba(255,255,255,.85)", dash: [9, 6],
        glow: "rgba(255,255,255,.3)", width: 2,
        label: ev.preview.lap_time
          ? `${ev.preview.label || ""} ${ev.preview.lap_time}s` : ev.message,
      });
    }
    updateOptimizeDrawer(ev);
    if (ev.status === "done" && ev.result) {
      S.optResult = ev.result;
      setGhost("optimize", {
        pts: ev.result.pts, color: COL.acc, dash: [9, 6],
        glow: "rgba(69,227,255,.45)", width: 2.2,
        label: `LAP ${ev.result.lap_time}s`,
      });
      toast(`Optimizer done — est. lap ${ev.result.lap_time}s`, "ok");
      if (optAutoApply) applyOptimize(ev.result);
    } else if (ev.status === "error") {
      toast(`Optimizer: ${ev.message}`, "err");
    }
  }
}

/* ---------------- restore banner ---------------- */
function offerRestore(serverSess) {
  const local = loadLocal();
  let sess = null, src = "";
  const sTs = serverSess && serverSess.ts
    ? serverSess.ts * (serverSess.ts < 1e12 ? 1000 : 1) : 0;
  const lTs = local && local.ts ? local.ts : 0;
  if (sTs >= lTs && serverSess && Array.isArray(serverSess.pts)) {
    sess = serverSess; src = "server";
  } else if (local && Array.isArray(local.pts)) {
    sess = local; src = "browser";
  }
  // Only offer drafts that actually carry unsaved work — a session written
  // for upload-config persistence alone must not nag on every boot.
  if (!sess || !sess.pts || sess.pts.length < 3 || sess.dirty === false) return;

  const when = new Date(sTs >= lTs ? sTs : lTs);
  const hh = `${when.getHours()}`.padStart(2, "0");
  const mm = `${when.getMinutes()}`.padStart(2, "0");
  const b = $("restoreBanner");
  b.innerHTML = `<span>Unsaved draft from <b>${hh}:${mm}</b> (${src}) found
    — continue where you left off?</span>
    <button class="btn small primary" id="rbYes">RESTORE</button>
    <button class="btn small" id="rbNo">DISCARD</button>`;
  b.classList.add("on");
  $("rbYes").onclick = () => {
    if (applySession(sess)) {
      refreshLineSel();
      toast("Draft restored", "ok");
      selectMode(sess.mode && MODES.includes(sess.mode) ? sess.mode : "shape");
    } else toast("Could not restore draft", "err");
    b.classList.remove("on");
  };
  $("rbNo").onclick = () => {
    api.sessionClear().catch(() => {});
    clearLocal();
    b.classList.remove("on");
  };
}

/* ---------------- keyboard ---------------- */
function initKeys() {
  const KEYS = [
    ["1–5", "switch mode"], ["F", "fit view"], ["C", "compute profile"],
    ["O", "optimizer"], ["U", "upload"], ["⌘/Ctrl+S", "save CSV"],
    ["⌘/Ctrl+Z", "undo"], ["⇧⌘Z", "redo"], ["Enter", "finish zone/region"],
    ["Esc", "cancel / close"], ["B / R", "boost / reduce"],
    ["[ / ]", "brush size"], ["?", "this overlay"],
  ];
  $("keysOverlay").innerHTML = `<div class="keys-card">
    <h2>KEYBOARD</h2><div class="keys-grid">${KEYS.map(([k, t]) =>
      `<div class="key-row"><span>${t}</span><kbd>${k}</kbd></div>`).join("")}
    </div></div>`;
  $("keysOverlay").onclick = () => $("keysOverlay").classList.remove("on");

  window.addEventListener("keydown", (e) => {
    const tag = (document.activeElement || {}).tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "s") {
      e.preventDefault(); doSave(); return;
    }
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
      return;
    }
    switch (e.key) {
      case "1": case "2": case "3": case "4": case "5":
        selectMode(MODES[+e.key - 1]); break;
      case "f": case "F": fit(true); break;
      case "c": case "C": computeProfile(); break;
      case "o": case "O": toggleDrawer("optimizeDrawer"); break;
      case "u": case "U": toggleDrawer("uploadDrawer"); break;
      case "b": case "B":
        if (S.mode === "speed") { S.speedDir = 1; buildCtx(); } break;
      case "r": case "R":
        if (S.mode === "speed") { S.speedDir = -1; buildCtx(); } break;
      case "[": S.brush = Math.max(1, S.brush - 2); buildCtx(); break;
      case "]": S.brush = Math.min(40, S.brush + 2); buildCtx(); break;
      case "Enter": finishDraft(); break;
      case "Escape":
        if (!cancelDraft()) { closeDrawers(); $("keysOverlay").classList.remove("on"); }
        break;
      case "?": $("keysOverlay").classList.toggle("on"); break;
      default: return;
    }
  });
}

/* ---------------- boot ---------------- */
export function initUI(initData) {
  $("trackName").textContent = S.meta.track || "—";
  document.title = `Raceline Studio — ${S.meta.track || ""}`;
  refreshLineSel();
  buildSettingsDrawer();
  buildOptimizeDrawer();
  buildUploadDrawer();
  buildCtx();
  buildLegend();
  buildInspector();
  initKeys();

  $("lineSel").addEventListener("change", (e) => {
    const name = e.target.value;
    if (!S.lines[name]) return;
    pushHistory();
    S.activeLine = name;
    setPts(clonePts(S.lines[name]));
    S.V = null; S.vT = null; S.vStale = false;
    bus.emit("V");
    toast(`Line: ${name}`);
  });
  $("fitBtn").onclick = () => fit(true);
  $("undoBtn").onclick = () =>
    undo() ? requestRender() : toast("Nothing to undo");
  $("redoBtn").onclick = () =>
    redo() ? requestRender() : toast("Nothing to redo");
  $("revertBtn").onclick = revertToSaved;
  $("computeBtn").onclick = () => computeProfile();
  $("optBtn").onclick = () => toggleDrawer("optimizeDrawer");
  $("uploadBtn").onclick = () => toggleDrawer("uploadDrawer");
  $("saveBtn").onclick = doSave;
  $("gearBtn").onclick = () => toggleDrawer("settingsDrawer");
  document.querySelectorAll("#modes button").forEach((b) =>
    b.addEventListener("click", () => selectMode(b.dataset.mode)));

  // connection status (banner debounced — a one-off SSE reconnect flap
  // during boot must not flash "offline" at the user)
  let connBannerTimer = null;
  onConn((state) => {
    $("connLed").className = "led " + (state === "ok" ? "ok" : "down");
    $("connText").textContent = state === "ok" ? "backend live" : "offline";
    clearTimeout(connBannerTimer);
    if (state === "down") {
      connBannerTimer = setTimeout(() =>
        $("connBanner").classList.add("on"), 4000);
    } else {
      $("connBanner").classList.remove("on");
    }
  });
  connectSSE(handleEvent);

  // bus → ui refreshers
  bus.on("pts", () => { updateInspector(); updateLap(); updateHistButtons(); });
  bus.on("V", () => { updateInspector(); updateLap(); });
  bus.on("zones", updateHistButtons);
  bus.on("dirty", updateHistButtons);
  updateHistButtons();
  bus.on("zones", () => { updateInspector(); requestRender(); });
  bus.on("region", updateInspector);
  bus.on("hover", updateInspector);
  bus.on("settings", updateInspector);
  bus.on("autosave", (msg) => {
    const a = $("autosaveText");
    a.textContent = msg;
    a.classList.toggle("dirty", /browser only|draft/.test(msg));
  });
  bus.on("dirty", () => {
    const a = $("autosaveText");
    if (!S.dirty) { a.textContent = "all changes saved"; a.classList.remove("dirty"); }
  });
  bus.on("geomEdited", () => {
    if (S.V && S.vStale) {
      toast("Line moved — speed profile stale, press COMPUTE");
    }
  });
  bus.on("lines", refreshLineSel);

  // global error → toast (never silently break the session)
  window.addEventListener("error", (e) => {
    toast(`UI error: ${e.message}`, "err");
  });
  window.addEventListener("unhandledrejection", (e) => {
    const m = e.reason && e.reason.message ? e.reason.message : "promise error";
    if (!/AbortError/.test(m)) toast(m, "err");
  });

  offerRestore(initData.session);
  updateLap();

  // reveal animation
  if (window.gsap) {
    const tl = gsap.timeline();
    tl.to("#bootBar", { width: "100%", duration: 0.35, ease: "power1.in" })
      .to("#boot", { opacity: 0, duration: 0.45, ease: "power2.out" })
      .set("#boot", { display: "none" })
      .from("#bar", { y: -56, opacity: 0, duration: 0.5, ease: "power3.out" }, "-=0.25")
      .from("#inspector", { x: 36, opacity: 0, duration: 0.45, ease: "power3.out" }, "-=0.3")
      .from("#legend", { x: 36, opacity: 0, duration: 0.45, ease: "power3.out" }, "-=0.35")
      .from("#strip", { y: 60, opacity: 0, duration: 0.45, ease: "power3.out" }, "-=0.4")
      .from("#status", { opacity: 0, duration: 0.4 }, "-=0.3");
  } else {
    $("boot").style.display = "none";
  }
}

export { selectMode };
