/* Viewport + renderer: main canvas (map, line, zones, ghosts, brush)
   and the bottom telemetry strip. Pointer logic lives in tools.js. */

import {
  COL, arcLengthData, certColor, clamp, fmt, polyCentroid, radiusColor,
  speedColor,
} from "./geometry.js";
import { S, bus, setHover } from "./store.js";

/* Layout heights come from the CSS variables so media queries (mobile)
   can reshape the chrome without touching JS. */
function cssVarPx(name, fallback) {
  const v = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue(name));
  return Number.isFinite(v) ? v : fallback;
}

let cv, ctx, stripCv, stripCtx, stripCursorEl;
let mapCanvas = null, mapCtx = null;
export const view = { scale: 1, ox: 0, oy: 0 };
let cssW = 0, cssH = 0, dpr = 1;
let cursor = null;            // {x, y} canvas px (brush preview)
let needsRender = true;
let dashPhase = 0;

/* ---------- transforms ---------- */
export function worldToImg(x, y) {
  const m = S.meta;
  return [(x - m.origin_x) / m.res, m.H - (y - m.origin_y) / m.res];
}
export function imgToWorld(ix, iy) {
  const m = S.meta;
  return [ix * m.res + m.origin_x, (m.H - iy) * m.res + m.origin_y];
}
export function imgToCanvas(ix, iy) {
  return [view.ox + ix * view.scale, view.oy + iy * view.scale];
}
export function canvasToImg(cx, cy) {
  return [(cx - view.ox) / view.scale, (cy - view.oy) / view.scale];
}
export function worldToCanvas(x, y) {
  const [ix, iy] = worldToImg(x, y);
  return imgToCanvas(ix, iy);
}
export function canvasToWorld(cx, cy) {
  const [ix, iy] = canvasToImg(cx, cy);
  return imgToWorld(ix, iy);
}
export function eventCanvasPos(e) {
  const r = cv.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}
/* brush sigma (index units) -> canvas px radius for the preview ring */
export function brushPxRadius() {
  const m = S.meta;
  return Math.max(8, S.brush * (m.spacing / m.res) * view.scale);
}

export function setCursor(c) { cursor = c; requestRender(); }

/* direction pulse: dots run along the working line in driving direction
   for a few seconds — triggered after REVERSE so the new direction is
   unmistakable (the static chevron at the start point is easy to miss). */
let flowUntil = 0;
export function showDirectionPulse(ms = 4000) {
  flowUntil = performance.now() + ms;
  requestRender();
}

/* ---------- map canvas (editable surface) ---------- */
export function getMapCanvas() { return mapCanvas; }
export function mapPng() { return mapCanvas.toDataURL("image/png"); }

function makeMapCanvas(img) {
  mapCanvas = document.createElement("canvas");
  mapCanvas.width = S.meta.W;
  mapCanvas.height = S.meta.H;
  mapCtx = mapCanvas.getContext("2d", { willReadFrequently: true });
  mapCtx.imageSmoothingEnabled = false;
  mapCtx.drawImage(img, 0, 0);
}
export function getMapCtx() { return mapCtx; }

/* ---------- sizing / fit ---------- */
function resize() {
  const BAR = cssVarPx("--bar-h", 56);
  const STRIP = cssVarPx("--strip-h", 118);
  const STATUS = cssVarPx("--status-h", 32);
  cssW = window.innerWidth;
  cssH = window.innerHeight - BAR - STRIP - STATUS;
  dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(cssW * dpr);
  cv.height = Math.round(cssH * dpr);
  cv.style.width = cssW + "px";
  cv.style.height = cssH + "px";

  const sw = window.innerWidth;
  const sh = STRIP - 24;
  stripCv.width = Math.round(sw * dpr);
  stripCv.height = Math.round(sh * dpr);
  requestRender();
  renderStrip();
}

export function fit(animated = false) {
  const m = S.meta;
  const pad = cssW < 520 ? 14 : 40;  // phones: let the map breathe less
  const s = Math.min((cssW - pad * 2) / m.W, (cssH - pad * 2) / m.H);
  const target = {
    scale: s,
    ox: (cssW - m.W * s) / 2,
    oy: (cssH - m.H * s) / 2,
  };
  if (animated && window.gsap) {
    gsap.to(view, {
      ...target, duration: 0.6, ease: "power3.out", onUpdate: requestRender,
    });
  } else {
    Object.assign(view, target);
    requestRender();
  }
}

export function zoomAt(cx, cy, factor) {
  const ns = clamp(view.scale * factor, 0.05, 400);
  const k = ns / view.scale;
  view.ox = cx - (cx - view.ox) * k;
  view.oy = cy - (cy - view.oy) * k;
  view.scale = ns;
  requestRender();
}
export function panBy(dx, dy) {
  view.ox += dx; view.oy += dy;
  requestRender();
}

/* ---------- render ---------- */
export function requestRender() { needsRender = true; }

function drawLineSegments(pts, colorFn, width, closed) {
  const n = pts.length;
  if (n < 2) return;
  ctx.lineCap = "round";
  ctx.lineWidth = width;
  let [px, py] = worldToCanvas(pts[0][0], pts[0][1]);
  for (let i = 1; i <= (closed ? n : n - 1); i++) {
    const j = i % n;
    const [x, y] = worldToCanvas(pts[j][0], pts[j][1]);
    ctx.strokeStyle = colorFn(j);
    ctx.beginPath();
    ctx.moveTo(px, py);
    ctx.lineTo(x, y);
    ctx.stroke();
    px = x; py = y;
  }
}

function drawGhost(g) {
  const pts = g.pts;
  if (!pts || pts.length < 2) return;
  ctx.save();
  ctx.lineWidth = g.width || 2;
  ctx.strokeStyle = g.color || "rgba(255,255,255,.75)";
  ctx.setLineDash(g.dash || [7, 7]);
  ctx.lineDashOffset = -dashPhase * (g.dashSpeed ?? 1);
  ctx.shadowColor = g.glow || "rgba(255,255,255,.25)";
  ctx.shadowBlur = 8;
  ctx.beginPath();
  const [x0, y0] = worldToCanvas(pts[0][0], pts[0][1]);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < pts.length; i++) {
    const [x, y] = worldToCanvas(pts[i][0], pts[i][1]);
    ctx.lineTo(x, y);
  }
  if (S.meta.closed) ctx.closePath();
  ctx.stroke();
  ctx.restore();
  if (g.label) {
    const [lx, ly] = worldToCanvas(pts[0][0], pts[0][1]);
    ctx.save();
    ctx.font = "600 10px 'IBM Plex Mono', monospace";
    ctx.fillStyle = g.color || "#fff";
    ctx.fillText(g.label, lx + 8, ly - 8);
    ctx.restore();
  }
}

function drawZonePoly(poly, fill, stroke, dashed, selected) {
  if (poly.length < 2) return;
  ctx.save();
  ctx.beginPath();
  const [x0, y0] = worldToCanvas(poly[0][0], poly[0][1]);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < poly.length; i++) {
    const [x, y] = worldToCanvas(poly[i][0], poly[i][1]);
    ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.lineWidth = selected ? 2.5 : 1.5;
  ctx.strokeStyle = stroke;
  if (dashed) ctx.setLineDash([6, 4]);
  ctx.stroke();
  ctx.restore();
}

function drawZoneHandle(poly) {
  const [wx, wy] = polyCentroid(poly);
  const [x, y] = worldToCanvas(wx, wy);
  ctx.save();
  ctx.fillStyle = "rgba(10,12,14,.85)";
  ctx.strokeStyle = COL.bad;
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(x, y, 8, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  ctx.strokeStyle = COL.bad;
  ctx.beginPath();
  ctx.moveTo(x - 3.2, y - 3.2); ctx.lineTo(x + 3.2, y + 3.2);
  ctx.moveTo(x + 3.2, y - 3.2); ctx.lineTo(x - 3.2, y + 3.2);
  ctx.stroke();
  ctx.restore();
  return [x, y];
}

export const zoneHandles = []; // [{kind, idx, x, y}] rebuilt each frame

function render() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  // map
  if (mapCanvas) {
    ctx.save();
    ctx.imageSmoothingEnabled = view.scale < 2;
    ctx.globalAlpha = S.mode === "map" ? 1 : 0.92;
    ctx.drawImage(mapCanvas, view.ox, view.oy,
      mapCanvas.width * view.scale, mapCanvas.height * view.scale);
    ctx.restore();
  }

  zoneHandles.length = 0;

  // carpet zones
  const showCarpet = S.mode === "carpet";
  for (let i = 0; i < S.carpet.zones.length; i++) {
    const sel = S.selZone && S.selZone.kind === "carpet" && S.selZone.idx === i;
    drawZonePoly(S.carpet.zones[i],
      `rgba(143,123,255,${showCarpet ? 0.26 : 0.10})`,
      `rgba(143,123,255,${showCarpet ? 0.85 : 0.3})`, false, sel);
    if (showCarpet) {
      const [x, y] = drawZoneHandle(S.carpet.zones[i]);
      zoneHandles.push({ kind: "carpet", idx: i, x, y });
    }
  }
  // certainty zones
  const showCert = S.mode === "cert";
  for (let i = 0; i < S.certainty.zones.length; i++) {
    const z = S.certainty.zones[i];
    const sel = S.selZone && S.selZone.kind === "cert" && S.selZone.idx === i;
    drawZonePoly(z.polygon, certColor(z.score, showCert ? 0.3 : 0.1),
      certColor(z.score, showCert ? 0.95 : 0.35), !!z.lock, sel);
    if (showCert) {
      const [x, y] = drawZoneHandle(z.polygon);
      zoneHandles.push({ kind: "cert", idx: i, x, y });
    }
  }

  // centerline compute region (manual REGION tool, map mode)
  if (S.clRegion && S.clRegion.length >= 3) {
    const on = S.mode === "map";
    ctx.save();
    ctx.beginPath();
    const [rx0, ry0] = worldToCanvas(S.clRegion[0][0], S.clRegion[0][1]);
    ctx.moveTo(rx0, ry0);
    for (let i = 1; i < S.clRegion.length; i++) {
      const [x, y] = worldToCanvas(S.clRegion[i][0], S.clRegion[i][1]);
      ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fillStyle = `rgba(255,179,71,${on ? 0.07 : 0.03})`;
    ctx.fill();
    ctx.strokeStyle = `rgba(255,179,71,${on ? 0.9 : 0.35})`;
    ctx.lineWidth = on ? 1.8 : 1.2;
    ctx.setLineDash([10, 6]);
    ctx.stroke();
    if (on) {
      ctx.setLineDash([]);
      ctx.font = "600 10px 'IBM Plex Mono', monospace";
      ctx.fillStyle = "rgba(255,179,71,.95)";
      ctx.fillText("CL REGION", rx0 + 8, ry0 - 8);
    }
    ctx.restore();
  }

  // ghosts (live centerline, optimizer preview)
  for (const name of Object.keys(S.ghosts)) drawGhost(S.ghosts[name]);

  // main line
  const n = S.pts.length;
  if (n >= 2) {
    const m = S.meta;
    const lw = clamp(view.scale * (m.spacing / m.res) * 0.55, 2, 6);
    const speedMode = S.mode === "speed" && S.V && !S.vStale
      && S.V.length === n;
    let vLo = Infinity, vHi = -Infinity;
    if (speedMode) {
      for (const v of S.V) { if (v < vLo) vLo = v; if (v > vHi) vHi = v; }
      if (vHi - vLo < 1e-6) vHi = vLo + 1e-6;
    }
    const colorFn = speedMode
      ? (j) => speedColor((S.V[j] - vLo) / (vHi - vLo))
      : (j) => radiusColor(S.radii[j], m.R_min, m.R_safe);

    // soft glow pass
    ctx.save();
    ctx.globalAlpha = 0.22;
    drawLineSegments(S.pts, colorFn, lw * 2.6, m.closed);
    ctx.restore();
    drawLineSegments(S.pts, colorFn, lw, m.closed);

    // grip rings in speed mode
    if (speedMode) {
      const g = m.g || 9.81;
      ctx.save();
      ctx.lineWidth = 1.6;
      for (let j = 0; j < n; j++) {
        const R = S.radii[j];
        const vGrip = Math.sqrt(S.mu * g * R);
        if (S.V[j] > vGrip * 1.03) ctx.strokeStyle = COL.warn;
        else continue;
        const [x, y] = worldToCanvas(S.pts[j][0], S.pts[j][1]);
        ctx.beginPath();
        ctx.arc(x, y, lw + 3.5, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }

    // points when zoomed in
    const ptPx = view.scale * (m.spacing / m.res);
    if (ptPx > 7 && (S.mode === "shape" || S.mode === "speed")) {
      ctx.save();
      ctx.fillStyle = "rgba(232,237,242,.5)";
      for (let j = 0; j < n; j++) {
        const [x, y] = worldToCanvas(S.pts[j][0], S.pts[j][1]);
        ctx.beginPath();
        ctx.arc(x, y, 1.7, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    // start marker + direction chevron
    {
      const [x, y] = worldToCanvas(S.pts[0][0], S.pts[0][1]);
      const p1 = S.pts[Math.min(2, n - 1)];
      const [x1, y1] = worldToCanvas(p1[0], p1[1]);
      const a = Math.atan2(y1 - y, x1 - x);
      ctx.save();
      ctx.fillStyle = "#0a0c0e";
      ctx.strokeStyle = COL.acc;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 6.5, 0, Math.PI * 2);
      ctx.fill(); ctx.stroke();
      ctx.translate(x + Math.cos(a) * 15, y + Math.sin(a) * 15);
      ctx.rotate(a);
      ctx.fillStyle = COL.acc;
      ctx.beginPath();
      ctx.moveTo(5, 0); ctx.lineTo(-4, -4.5); ctx.lineTo(-1.5, 0);
      ctx.lineTo(-4, 4.5); ctx.closePath();
      ctx.fill();
      ctx.restore();
    }

    // direction pulse dots (see showDirectionPulse)
    const flowLeft = flowUntil - performance.now();
    if (flowLeft > 0 && n > 4) {
      const { s, total } = arcLengthData(S.pts, m.closed);
      const gap = Math.max(1.2, total / 36);   // ~36 dots on a full lap
      const off = (performance.now() * 0.001 * 5.0) % gap; // 5 m/s march
      const alpha = Math.min(1, flowLeft / 600);           // fade out
      ctx.save();
      ctx.fillStyle = `rgba(255,255,255,${0.95 * alpha})`;
      ctx.shadowColor = "rgba(69,227,255,.8)";
      ctx.shadowBlur = 7;
      let j = 0;
      for (let d = off; d < total; d += gap) {
        while (j < n - 1 && s[j + 1] < d) j++;
        const j2 = (j + 1) % n;
        const s1 = j2 === 0 ? total : s[j2];
        const f = s1 > s[j] ? (d - s[j]) / (s1 - s[j]) : 0;
        const x = S.pts[j][0] + (S.pts[j2][0] - S.pts[j][0]) * f;
        const y = S.pts[j][1] + (S.pts[j2][1] - S.pts[j][1]) * f;
        const [dx, dy] = worldToCanvas(x, y);
        ctx.beginPath();
        ctx.arc(dx, dy, 2.6, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    // hover point
    if (S.hover >= 0 && S.hover < n) {
      const [x, y] = worldToCanvas(S.pts[S.hover][0], S.pts[S.hover][1]);
      ctx.save();
      ctx.strokeStyle = "rgba(255,255,255,.9)";
      ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = "rgba(255,255,255,.25)";
      ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }
  }

  // draft polygon
  if (S.draft && S.draft.length) {
    ctx.save();
    ctx.strokeStyle = COL.acc;
    ctx.fillStyle = "rgba(69,227,255,.10)";
    ctx.lineWidth = 1.6;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    const [x0, y0] = worldToCanvas(S.draft[0][0], S.draft[0][1]);
    ctx.moveTo(x0, y0);
    for (let i = 1; i < S.draft.length; i++) {
      const [x, y] = worldToCanvas(S.draft[i][0], S.draft[i][1]);
      ctx.lineTo(x, y);
    }
    if (cursor) ctx.lineTo(cursor.x, cursor.y);
    ctx.stroke();
    if (S.draft.length > 2) ctx.fill();
    ctx.setLineDash([]);
    for (let i = 0; i < S.draft.length; i++) {
      const [x, y] = worldToCanvas(S.draft[i][0], S.draft[i][1]);
      ctx.fillStyle = i === 0 ? COL.acc : "rgba(255,255,255,.85)";
      ctx.beginPath();
      ctx.arc(x, y, i === 0 ? 5 : 3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // brush cursor
  if (cursor && (S.mode === "shape" || S.mode === "speed")) {
    ctx.save();
    ctx.strokeStyle = S.mode === "speed"
      ? (S.speedDir > 0 ? "rgba(255,77,77,.65)" : "rgba(52,224,126,.65)")
      : "rgba(69,227,255,.55)";
    ctx.lineWidth = 1.2;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.arc(cursor.x, cursor.y, brushPxRadius(), 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
  if (cursor && S.mode === "map") {
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,.6)";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(cursor.x, cursor.y, Math.max(2, S.mapBrushPx * view.scale / 2),
      0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

/* ---------- telemetry strip ---------- */
let stripGeom = null; // cached mapping

export function renderStrip() {
  if (!stripCtx || !S.meta) return;
  const W = stripCv.width / dpr, H = stripCv.height / dpr;
  stripCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  stripCtx.clearRect(0, 0, W, H);
  const n = S.pts.length;
  if (n < 2) { stripGeom = null; return; }

  const { s, total } = arcLengthData(S.pts, S.meta.closed);
  stripGeom = { s, total, W, H };
  const X = (i) => 12 + (s[i] / (total || 1)) * (W - 24);

  // grid
  stripCtx.strokeStyle = "rgba(255,255,255,.05)";
  stripCtx.lineWidth = 1;
  for (let gx = 0; gx <= 4; gx++) {
    const x = 12 + (gx / 4) * (W - 24);
    stripCtx.beginPath(); stripCtx.moveTo(x, 4); stripCtx.lineTo(x, H - 4);
    stripCtx.stroke();
  }

  // radius trace (sqrt scale, capped) + danger shading below R_min
  const Rcap = Math.max(S.meta.R_safe * 4, 4);
  const rY = (r) => H - 6 - Math.sqrt(clamp(r, 0, Rcap) / Rcap) * (H - 14);
  stripCtx.save();
  for (let i = 0; i < n; i++) {
    if (S.radii[i] < S.meta.R_min) {
      stripCtx.fillStyle = "rgba(255,77,77,.18)";
      stripCtx.fillRect(X(i) - 1, 4, 2, H - 8);
    }
  }
  stripCtx.strokeStyle = "rgba(69,227,255,.8)";
  stripCtx.lineWidth = 1.2;
  stripCtx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = X(i), y = rY(S.radii[i]);
    if (i === 0) stripCtx.moveTo(x, y); else stripCtx.lineTo(x, y);
  }
  stripCtx.stroke();
  // R_min reference
  stripCtx.strokeStyle = "rgba(255,77,77,.4)";
  stripCtx.setLineDash([3, 4]);
  stripCtx.beginPath();
  stripCtx.moveTo(12, rY(S.meta.R_min));
  stripCtx.lineTo(W - 12, rY(S.meta.R_min));
  stripCtx.stroke();
  stripCtx.restore();

  // speed trace
  if (S.V && S.V.length === n) {
    const vY = (v) => H - 6 - clamp(v / (S.vmax || 1), 0, 1) * (H - 14);
    const stale = S.vStale;
    stripCtx.save();
    if (stale) stripCtx.globalAlpha = 0.35;
    let vLo = Infinity, vHi = -Infinity;
    for (const v of S.V) { if (v < vLo) vLo = v; if (v > vHi) vHi = v; }
    if (vHi - vLo < 1e-6) vHi = vLo + 1e-6;
    for (let i = 0; i < n - 1; i++) {
      stripCtx.strokeStyle =
        speedColor((S.V[i] - vLo) / (vHi - vLo));
      stripCtx.lineWidth = 1.8;
      stripCtx.beginPath();
      stripCtx.moveTo(X(i), vY(S.V[i]));
      stripCtx.lineTo(X(i + 1), vY(S.V[i + 1]));
      stripCtx.stroke();
    }
    stripCtx.restore();
  }

  // hover cursor
  if (S.hover >= 0 && S.hover < n) {
    const x = X(S.hover);
    stripCtx.strokeStyle = "rgba(255,255,255,.7)";
    stripCtx.lineWidth = 1;
    stripCtx.beginPath();
    stripCtx.moveTo(x, 2); stripCtx.lineTo(x, H - 2);
    stripCtx.stroke();
  }

  // cursor readout
  if (stripCursorEl) {
    if (S.hover >= 0 && S.hover < n) {
      const i = S.hover;
      const v = S.V && S.V.length === n ? fmt(S.V[i], 2) + " m/s" : "—";
      stripCursorEl.textContent =
        `s ${fmt(s[i], 1)} m · R ${fmt(Math.min(S.radii[i], 99), 2)} m · v ${v}`;
    } else {
      stripCursorEl.textContent = `lap ${fmt(total, 1)} m · ${n} pts`;
    }
  }
}

function stripPick(e) {
  if (!stripGeom) return;
  const r = stripCv.getBoundingClientRect();
  const x = e.clientX - r.left;
  const frac = clamp((x - 12) / (stripGeom.W - 24), 0, 1);
  const targetS = frac * stripGeom.total;
  // binary search on s
  const s = stripGeom.s;
  let lo = 0, hi = s.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (s[mid] < targetS) lo = mid + 1; else hi = mid;
  }
  setHover(lo);
}

/* ---------- boot ---------- */
export function initView(mapImg) {
  cv = document.getElementById("cv");
  ctx = cv.getContext("2d");
  stripCv = document.getElementById("stripCv");
  stripCtx = stripCv.getContext("2d");
  stripCursorEl = document.getElementById("stripCursor");
  makeMapCanvas(mapImg);
  window.addEventListener("resize", resize);
  resize();
  fit(false);

  stripCv.addEventListener("mousemove", stripPick);
  stripCv.addEventListener("mouseleave", () => setHover(-1));

  bus.on("pts", () => { requestRender(); renderStrip(); });
  bus.on("V", () => { requestRender(); renderStrip(); });
  bus.on("zones", requestRender);
  bus.on("region", requestRender);
  bus.on("ghosts", requestRender);
  bus.on("mode", () => { requestRender(); renderStrip(); });
  bus.on("hover", () => { requestRender(); renderStrip(); });
  bus.on("settings", () => { requestRender(); renderStrip(); });
  bus.on("mapPainted", requestRender);

  // render loop — redraw on demand; keep dashes marching while ghosts live
  const loop = () => {
    const animating = Object.keys(S.ghosts).length > 0
      || performance.now() < flowUntil;
    if (animating) dashPhase = (dashPhase + 0.45) % 1000;
    if (needsRender || animating) {
      needsRender = false;
      render();
    }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}
