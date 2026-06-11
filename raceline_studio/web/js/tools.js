/* Pointer tools per mode + the actions the UI triggers.
   shape: elastic Gaussian drag · speed: dwell brush + feasibility pass
   carpet/cert: polygon zones · map: paint walls/free + flood fill with
   live centerline scheduling. */

import { api } from "./api.js";
import {
  clamp, gaussianApply, laplacian, nearestPoint, pointInPoly,
} from "./geometry.js";
import {
  S, bus, captureSnapshot, clonePts, markDirty, physPayload, pushHistory,
  setGhost, setHover, setPts, setProfile, setV,
} from "./store.js";
import {
  brushPxRadius, canvasToImg, canvasToWorld, eventCanvasPos, getMapCanvas,
  getMapCtx, mapPng, panBy, requestRender, setCursor, view, zoneHandles,
  zoomAt,
} from "./view.js";

let toast = (m) => console.log("[toast]", m);
export function setToast(fn) { toast = fn; }

/* ============================== actions ============================== */

let computing = false;
export async function computeProfile({ silent = false } = {}) {
  if (computing) return;
  computing = true;
  bus.emit("busy", "computing profile…");
  try {
    const res = await api.velocity({ pts: S.pts, ...physPayload() });
    pushHistory();
    setProfile(res.pts, res.v);
    if (!silent) toast("Profile computed — line resampled");
  } catch (e) {
    if (e.name !== "AbortError") toast(`Compute failed: ${e.message}`, "err");
  } finally {
    computing = false;
    bus.emit("busy", null);
  }
}

export function smoothLine() {
  pushHistory();
  setPts(laplacian(S.pts, S.meta.closed, 0.35));
  toast("Smoothed");
}

let feasTimer = null;
export function runFeasible(delay = 150) {
  if (!S.vT || S.vT.length !== S.pts.length) return;
  clearTimeout(feasTimer);
  feasTimer = setTimeout(async () => {
    try {
      const res = await api.feasible(
        { pts: S.pts, v_targets: S.vT, ...physPayload() });
      setV(res.v);
    } catch (e) {
      if (e.name !== "AbortError") toast(`Feasibility failed: ${e.message}`, "err");
    }
  }, delay);
}

/* live centerline ----------------------------------------------------- */
let clTimer = null;
export function scheduleCenterline(delay = 400) {
  if (!S.liveCenterline) return;
  clearTimeout(clTimer);
  clTimer = setTimeout(runCenterlineNow, delay);
}
export async function runCenterlineNow() {
  clearTimeout(clTimer);
  try {
    // "region" is always sent — null clears the server-side region too.
    const body = { region: S.clRegion };
    if (S.mapEdited) body.png = mapPng();
    await api.centerline(body);
  } catch (e) {
    toast(`Centerline failed: ${e.message}`, "err");
  }
}

export function clearRegion() {
  if (!S.clRegion) { toast("No centerline region set"); return; }
  S.clRegion = null;
  S.sessionDirty = true;
  bus.emit("region");
  toast("Region cleared — centerline uses the full map");
  runCenterlineNow();
}

/* optimizer ------------------------------------------------------------ */
export async function runOptimize(method) {
  try {
    const body = { method, region: S.clRegion, ...physPayload() };
    if (S.mapEdited) body.png = mapPng();
    await api.optimize(body);
  } catch (e) {
    toast(`Optimize failed: ${e.message}`, "err");
  }
}
export function applyOptimize(result) {
  if (!result || !result.pts) return;
  pushHistory();
  setProfile(clonePts(result.pts), result.v);
  const name = result.method === "mintime"
    ? "optimized (min time)" : "optimized (min curvature)";
  S.lines[name] = clonePts(result.pts);
  S.activeLine = name;
  setGhost("optimize", null);
  bus.emit("lines");
  toast(`Applied — est. lap ${result.lap_time?.toFixed(2)}s`);
}

/* zones ----------------------------------------------------------------- */
export function finishDraft() {
  if (!S.draft || S.draft.length < 3) { S.draft = null; requestRender(); return; }
  if (S.mode === "map") {
    // Region polygon: a compute setting, not artwork — kept out of undo.
    S.clRegion = S.draft;
    S.draft = null;
    S.sessionDirty = true;
    bus.emit("region");
    toast("Centerline region set — computing inside it");
    runCenterlineNow();
    return;
  }
  pushHistory();
  if (S.mode === "carpet") {
    S.carpet.zones.push(S.draft);
    S.selZone = { kind: "carpet", idx: S.carpet.zones.length - 1 };
    runFeasible();
  } else if (S.mode === "cert") {
    S.certainty.zones.push(
      { score: 0.85, lock: false, force: false, polygon: S.draft });
    S.selZone = { kind: "cert", idx: S.certainty.zones.length - 1 };
  }
  S.draft = null;
  markDirty();
  bus.emit("zones");
}
export function cancelDraft() {
  if (S.draft) { S.draft = null; requestRender(); return true; }
  return false;
}
export function deleteZone(kind, idx) {
  pushHistory();
  if (kind === "carpet") {
    S.carpet.zones.splice(idx, 1);
    runFeasible();
  } else {
    S.certainty.zones.splice(idx, 1);
  }
  S.selZone = null;
  markDirty();
  bus.emit("zones");
}
export function clearZones() {
  pushHistory();
  if (S.mode === "carpet") { S.carpet.zones = []; runFeasible(); }
  if (S.mode === "cert") S.certainty.zones = [];
  S.selZone = null;
  markDirty();
  bus.emit("zones");
}

/* map painting ----------------------------------------------------------- */
const MAP_COLORS = { wall: "#000000", free: "#fefefe", unknown: "#cdcdcd" };

function paintStroke(imgA, imgB) {
  const ctx = getMapCtx();
  ctx.strokeStyle = MAP_COLORS[S.mapTool] || "#000";
  ctx.lineWidth = S.mapBrushPx;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(imgA[0], imgA[1]);
  ctx.lineTo(imgB[0], imgB[1]);
  ctx.stroke();
  S.mapEdited = true;
  bus.emit("mapPainted");
}

function floodFill(ix, iy, tol = 40) {
  const canvas = getMapCanvas();
  const W = canvas.width, H = canvas.height;
  ix = Math.round(ix); iy = Math.round(iy);
  if (ix < 0 || iy < 0 || ix >= W || iy >= H) return;
  const ctx = getMapCtx();
  const img = ctx.getImageData(0, 0, W, H);
  const d = img.data;
  const at = (x, y) => d[(y * W + x) * 4];
  const target = at(ix, iy);
  const fillHex = MAP_COLORS[S.mapTool] || "#000";
  const fv = parseInt(fillHex.slice(1, 3), 16);
  if (Math.abs(target - fv) <= 0) return;
  const match = (x, y) => Math.abs(at(x, y) - target) <= tol;
  const set = (x, y) => {
    const o = (y * W + x) * 4;
    d[o] = d[o + 1] = d[o + 2] = fv; d[o + 3] = 255;
  };
  // scanline flood
  const stack = [[ix, iy]];
  let guard = W * H; // hard cap — never loop forever on odd data
  while (stack.length && guard-- > 0) {
    const [sx, sy] = stack.pop();
    let x0 = sx;
    while (x0 >= 0 && match(x0, sy)) x0--;
    x0++;
    let above = false, below = false;
    for (let x = x0; x < W && match(x, sy); x++) {
      set(x, sy);
      if (sy > 0) {
        if (match(x, sy - 1)) {
          if (!above) { stack.push([x, sy - 1]); above = true; }
        } else above = false;
      }
      if (sy < H - 1) {
        if (match(x, sy + 1)) {
          if (!below) { stack.push([x, sy + 1]); below = true; }
        } else below = false;
      }
    }
  }
  ctx.putImageData(img, 0, 0);
  S.mapEdited = true;
  bus.emit("mapPainted");
}

/* ============================== pointer FSM ============================== */

let drag = null; // {kind, ...}
let speedRaf = null;
let lastSpeed = null; // {t, cx, cy}

function pickThresholdWorld() {
  return Math.max(0.12, 10 / view.scale * S.meta.res);
}

function startSpeedBrush() {
  lastSpeed = { t: performance.now() };
  const RATE = 3.0; // m/s per held second at the brush centre
  const step = () => {
    if (!drag || drag.kind !== "speed") return;
    const now = performance.now();
    const dt = Math.min(0.05, (now - lastSpeed.t) / 1000);
    lastSpeed.t = now;
    if (drag.cx !== undefined && S.vT && S.vT.length === S.pts.length) {
      const [wx, wy] = canvasToWorld(drag.cx, drag.cy);
      const { idx, dist } = nearestPoint(S.pts, wx, wy);
      if (idx >= 0 && dist < pickThresholdWorld() * 4) {
        const dir = S.speedDir;
        gaussianApply(S.pts.length, idx, S.brush, S.meta.closed, (j, w) => {
          S.vT[j] = clamp(S.vT[j] + dir * RATE * dt * w, 0.3, S.vmax);
        });
        S.V = S.vT.slice(); // optimistic preview until /feasible lands
        markDirty();
        bus.emit("V");
      }
    }
    speedRaf = requestAnimationFrame(step);
  };
  speedRaf = requestAnimationFrame(step);
}

function zoneClick(cx, cy, wx, wy) {
  // 1) delete handle hit?
  for (const h of zoneHandles) {
    if (Math.hypot(h.x - cx, h.y - cy) < 11) {
      deleteZone(h.kind, h.idx);
      return;
    }
  }
  // 2) drafting in progress: extend / close
  if (S.draft) {
    const first = canvasFromWorld(S.draft[0]);
    const closePx = Math.hypot(first[0] - cx, first[1] - cy);
    if (S.draft.length >= 3 && closePx < 12) finishDraft();
    else S.draft.push([wx, wy]);
    requestRender();
    return;
  }
  // 3) select an existing zone under the cursor
  const zones = S.mode === "carpet"
    ? S.carpet.zones.map((z) => ({ poly: z }))
    : S.certainty.zones.map((z) => ({ poly: z.polygon }));
  for (let i = zones.length - 1; i >= 0; i--) {
    if (pointInPoly(wx, wy, zones[i].poly)) {
      S.selZone = { kind: S.mode === "carpet" ? "carpet" : "cert", idx: i };
      bus.emit("zones");
      return;
    }
  }
  // 4) start a new draft
  S.selZone = null;
  S.draft = [[wx, wy]];
  bus.emit("zones");
}

import { worldToCanvas } from "./view.js";
function canvasFromWorld(p) { return worldToCanvas(p[0], p[1]); }

function onPointerDown(e) {
  const cv = e.currentTarget;
  try { cv.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
  const [cx, cy] = eventCanvasPos(e);

  if (e.button === 2 || e.button === 1) {
    drag = { kind: "pan", x: cx, y: cy };
    return;
  }
  if (e.button !== 0) return;
  const [wx, wy] = canvasToWorld(cx, cy);

  if (S.mode === "shape") {
    const { idx, dist } = nearestPoint(S.pts, wx, wy);
    if (idx >= 0 && dist < pickThresholdWorld() * 3) {
      drag = { kind: "shape", idx, pre: captureSnapshot(),
               lx: wx, ly: wy, moved: false };
    } else {
      drag = { kind: "pan", x: cx, y: cy };
    }
  } else if (S.mode === "speed") {
    if (!S.V || S.vStale) {
      computeProfile({ silent: true }).then(() => {});
      return;
    }
    drag = { kind: "speed", cx, cy, pre: captureSnapshot(), painted: false };
    startSpeedBrush();
  } else if (S.mode === "carpet" || S.mode === "cert") {
    zoneClick(cx, cy, wx, wy);
  } else if (S.mode === "map") {
    if (S.mapTool === "region") {
      // Polygon clicks like the zone tools: extend, close on first point.
      if (S.draft) {
        const first = canvasFromWorld(S.draft[0]);
        if (S.draft.length >= 3
            && Math.hypot(first[0] - cx, first[1] - cy) < 12) finishDraft();
        else S.draft.push([wx, wy]);
      } else {
        S.draft = [[wx, wy]];
      }
      requestRender();
      return;
    }
    const [ix, iy] = canvasToImg(cx, cy);
    if (S.mapTool === "fill") {
      floodFill(ix, iy);
      scheduleCenterline();
    } else {
      drag = { kind: "paint", last: [ix, iy] };
      paintStroke([ix, iy], [ix, iy]);
    }
  }
}

function onPointerMove(e) {
  const [cx, cy] = eventCanvasPos(e);
  setCursor({ x: cx, y: cy });

  if (drag) {
    if (drag.kind === "pan") {
      panBy(cx - drag.x, cy - drag.y);
      drag.x = cx; drag.y = cy;
      return;
    }
    if (drag.kind === "shape") {
      const [wx, wy] = canvasToWorld(cx, cy);
      const dx = wx - drag.lx, dy = wy - drag.ly;
      drag.lx = wx; drag.ly = wy;
      if (dx || dy) {
        drag.moved = true;
        const pts = S.pts;
        gaussianApply(pts.length, drag.idx, S.brush, S.meta.closed,
          (j, w) => { pts[j][0] += dx * w; pts[j][1] += dy * w; });
        setPts(pts);
        setHover(drag.idx);
      }
      return;
    }
    if (drag.kind === "speed") {
      drag.cx = cx; drag.cy = cy; drag.painted = true;
      return;
    }
    if (drag.kind === "paint") {
      const [ix, iy] = canvasToImg(cx, cy);
      paintStroke(drag.last, [ix, iy]);
      drag.last = [ix, iy];
      return;
    }
  }

  // hover pick (no drag)
  if (S.mode === "shape" || S.mode === "speed") {
    const [wx, wy] = canvasToWorld(cx, cy);
    const { idx, dist } = nearestPoint(S.pts, wx, wy);
    setHover(dist < pickThresholdWorld() * 5 ? idx : -1);
  }
}

function onPointerUp(e) {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (speedRaf) { cancelAnimationFrame(speedRaf); speedRaf = null; }

  if (d.kind === "shape" && d.moved) {
    pushHistory(d.pre);
    bus.emit("geomEdited");
  } else if (d.kind === "speed" && d.painted) {
    pushHistory(d.pre);
    runFeasible(120);
  } else if (d.kind === "paint") {
    scheduleCenterline();
  }
}

function onWheel(e) {
  e.preventDefault();
  const [cx, cy] = eventCanvasPos(e);
  zoomAt(cx, cy, Math.exp(-e.deltaY * 0.0016));
}

export function initTools() {
  const cv = document.getElementById("cv");
  cv.addEventListener("pointerdown", onPointerDown);
  cv.addEventListener("pointermove", onPointerMove);
  cv.addEventListener("pointerup", onPointerUp);
  cv.addEventListener("pointercancel", onPointerUp);
  cv.addEventListener("mouseleave", () => { setCursor(null); setHover(-1); });
  cv.addEventListener("wheel", onWheel, { passive: false });
  cv.addEventListener("contextmenu", (e) => e.preventDefault());
}
