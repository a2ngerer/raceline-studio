/* Central state, undo history, crash-safe autosave.

   Autosave layers (cheapest first):
     1. localStorage  — every 1.5 s while dirty (survives tab crash)
     2. POST /session — every 6 s while dirty (survives browser loss,
                        restored even from another machine)
     3. sendBeacon    — on pagehide/visibility-hidden (covers tab close)
   A successful SAVE clears all three. */

import { api, beaconSession } from "./api.js";
import { lapTimeOf, radiiOf } from "./geometry.js";

export const S = {
  meta: null, out: "", lines: {}, activeLine: "",
  pts: [], V: null, vT: null, vStale: false,
  radii: new Float64Array(0),
  mode: "shape", speedDir: 1, brush: 12,
  mapTool: "wall", mapBrushPx: 6, liveCenterline: true,
  clRegion: null,           // world polygon limiting centerline computation
  mu: 0.45, vmax: 7, mupp: 0.82, muCarpet: 0.9, unrestricted: false,
  carpet: { mu: 0.9, zones: [] },
  certainty: { neutral: 0.5, zones: [] },
  selZone: null,            // {kind:'carpet'|'cert', idx}
  draft: null,              // polygon being drawn [[x,y],...]
  upload: { host: "", user: "", port: 22, dest: "" },
  hover: -1,
  ghosts: {},               // name -> {pts, color, dash, label}
  optResult: null,
  job: {},                  // kind -> {status, progress, message}
  dirty: false,             // unsaved vs CSV on disk
  sessionDirty: false,
  mapEdited: false,
  savedPts: null,           // last state written to the CSV (for revert)
  savedV: null,
};

/* ---- tiny event bus ---- */
const handlers = {};
export const bus = {
  on(ev, cb) { (handlers[ev] = handlers[ev] || []).push(cb); },
  emit(ev, ...a) {
    for (const cb of handlers[ev] || []) {
      try { cb(...a); } catch (e) { console.error(`[bus:${ev}]`, e); }
    }
    for (const cb of handlers["*"] || []) {
      try { cb(ev, ...a); } catch (e) { console.error("[bus:*]", e); }
    }
  },
};

export const clonePts = (a) => a.map((p) => [p[0], p[1]]);

/* ---- derived ---- */
export function recomputeRadii() {
  S.radii = radiiOf(S.pts, S.meta ? S.meta.closed : true);
}
export function lapEstimate() {
  return lapTimeOf(S.pts, S.vStale ? null : S.V, S.meta ? S.meta.closed : true);
}

/* ---- history (undo/redo) ---- */
const hist = [];
const redoStack = [];
const HIST_MAX = 200;

function snapshot() {
  return {
    pts: clonePts(S.pts),
    V: S.V ? S.V.slice() : null,
    vT: S.vT ? S.vT.slice() : null,
    vStale: S.vStale,
    carpet: JSON.parse(JSON.stringify(S.carpet)),
    certainty: JSON.parse(JSON.stringify(S.certainty)),
  };
}
function applySnap(s) {
  S.pts = s.pts; S.V = s.V; S.vT = s.vT; S.vStale = s.vStale;
  S.carpet = s.carpet; S.certainty = s.certainty;
  S.selZone = null;
  recomputeRadii();
  markDirty();
  bus.emit("pts"); bus.emit("V"); bus.emit("zones");
}
export function pushHistory(pre) {
  hist.push(pre || snapshot());
  if (hist.length > HIST_MAX) hist.shift();
  redoStack.length = 0;
}
export function captureSnapshot() { return snapshot(); }
export function undo() {
  if (!hist.length) return false;
  redoStack.push(snapshot());
  applySnap(hist.pop());
  return true;
}
export function redo() {
  if (!redoStack.length) return false;
  hist.push(snapshot());
  applySnap(redoStack.pop());
  return true;
}
export function historySizes() {
  return { undo: hist.length, redo: redoStack.length };
}

/* ---- mutations ---- */
export function setPts(pts, { stale = true } = {}) {
  S.pts = pts;
  if (stale && S.V) S.vStale = true;
  recomputeRadii();
  markDirty();
  bus.emit("pts");
}
export function setProfile(pts, v) {
  S.pts = pts; S.V = v.slice(); S.vT = v.slice(); S.vStale = false;
  recomputeRadii();
  markDirty();
  bus.emit("pts"); bus.emit("V");
}
export function setV(v) {
  S.V = v.slice();
  if (!S.vT || S.vT.length !== v.length) S.vT = v.slice();
  S.vStale = false;
  markDirty();
  bus.emit("V");
}
export function setMode(m) {
  if (S.mode === m) return;
  S.mode = m;
  S.draft = null;
  S.selZone = null;
  bus.emit("mode");
}
export function setHover(i) {
  if (S.hover === i) return;
  S.hover = i;
  bus.emit("hover");
}
export function setGhost(name, g) {
  if (g) S.ghosts[name] = g; else delete S.ghosts[name];
  bus.emit("ghosts");
}
export function markDirty() {
  S.dirty = true;
  S.sessionDirty = true;
  bus.emit("dirty");
}
export function markSaved() {
  S.dirty = false;
  S.sessionDirty = false;
  // Remember exactly what hit the disk, so REVERT can restore it later.
  S.savedPts = clonePts(S.pts);
  S.savedV = (S.V && !S.vStale && S.V.length === S.pts.length)
    ? S.V.slice() : null;
  clearLocal();
  bus.emit("dirty");
  bus.emit("autosave", "saved to disk");
}

/* ---- settings payload shared by /save, /feasible, /upload ---- */
export function physPayload() {
  return {
    mu: S.mu, v_max: S.vmax, unrestricted: S.unrestricted,
    carpet: { mu: S.muCarpet, zones: S.carpet.zones },
  };
}
export function savePayload() {
  const haveProfile = S.V && !S.vStale && S.vT
    && S.vT.length === S.pts.length;
  return {
    pts: S.pts,
    v_targets: haveProfile ? S.vT : null,
    certainty: S.certainty,
    ...physPayload(),
  };
}

/* ---- session (crash recovery) ---- */
const lsKey = () => `rlstudio:${S.meta ? S.meta.track : "?"}`;

export function serializeSession() {
  return {
    pts: S.pts, v: S.V, v_targets: S.vT, vStale: S.vStale, dirty: S.dirty,
    settings: { mu: S.mu, v_max: S.vmax, mu_pp: S.mupp,
                mu_carpet: S.muCarpet, unrestricted: S.unrestricted },
    carpet: S.carpet, certainty: S.certainty,
    upload: S.upload, mode: S.mode, cl_region: S.clRegion, ts: Date.now(),
  };
}

export function applySession(sess) {
  if (!sess || !Array.isArray(sess.pts) || sess.pts.length < 3) return false;
  S.pts = clonePts(sess.pts);
  S.V = Array.isArray(sess.v) && sess.v.length === S.pts.length
    ? sess.v.slice() : null;
  S.vT = Array.isArray(sess.v_targets)
    && sess.v_targets.length === S.pts.length
    ? sess.v_targets.slice() : (S.V ? S.V.slice() : null);
  S.vStale = !!sess.vStale;
  const st = sess.settings || {};
  if (isFinite(st.mu)) S.mu = st.mu;
  if (isFinite(st.v_max)) S.vmax = st.v_max;
  if (isFinite(st.mu_pp)) S.mupp = st.mu_pp;
  if (isFinite(st.mu_carpet)) S.muCarpet = st.mu_carpet;
  S.unrestricted = !!st.unrestricted;
  if (sess.carpet && Array.isArray(sess.carpet.zones)) S.carpet = sess.carpet;
  if (sess.certainty && Array.isArray(sess.certainty.zones)) {
    S.certainty = sess.certainty;
  }
  if (sess.upload) S.upload = { ...S.upload, ...sess.upload };
  if ("cl_region" in sess) {
    S.clRegion = Array.isArray(sess.cl_region) ? sess.cl_region : null;
    bus.emit("region");
  }
  recomputeRadii();
  S.dirty = true;
  bus.emit("pts"); bus.emit("V"); bus.emit("zones"); bus.emit("settings");
  return true;
}

export function loadLocal() {
  try {
    const raw = localStorage.getItem(lsKey());
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function saveLocal() {
  try {
    localStorage.setItem(lsKey(), JSON.stringify(serializeSession()));
    return true;
  } catch { return false; }
}
export function clearLocal() {
  try { localStorage.removeItem(lsKey()); } catch { /* private mode */ }
}

let lastServerPush = 0;
export function startAutosave() {
  setInterval(() => {
    if (!S.sessionDirty) return;
    if (saveLocal()) bus.emit("autosave", "draft in browser");
    const now = Date.now();
    if (now - lastServerPush > 6000) {
      lastServerPush = now;
      api.session(serializeSession())
        .then(() => {
          // Only a confirmed server write clears the flag — a failed push
          // (backend down) must retry on the next tick, otherwise the draft
          // never reaches the server after a crash recovery.
          S.sessionDirty = false;
          bus.emit("autosave", "draft on server");
        })
        .catch(() => bus.emit("autosave", "draft in browser only"));
    }
  }, 1500);

  // Flush on tab close / hide. sendBeacon survives page teardown.
  const flush = () => {
    if (!S.dirty) return;
    saveLocal();
    beaconSession(serializeSession());
  };
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });

  // Native “unsaved changes” prompt on close.
  window.addEventListener("beforeunload", (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ""; }
  });
}

/* ---- boot ---- */
export function initStore(d) {
  S.meta = d.meta;
  S.out = d.out || "";
  S.lines = d.lines || {};
  S.mu = d.meta.mu; S.vmax = d.meta.v_max; S.mupp = d.meta.mu_pp;
  S.muCarpet = d.meta.mu_carpet ?? 0.9;
  S.carpet = { mu: S.muCarpet, zones: d.meta.carpet_zones || [] };
  S.certainty = { neutral: d.meta.cert_neutral ?? 0.5,
                  zones: d.meta.certainty_zones || [] };
  if (d.upload) S.upload = { ...S.upload, ...d.upload };
  S.clRegion = Array.isArray(d.cl_region) ? d.cl_region : null;

  const names = Object.keys(S.lines);
  S.activeLine = names.includes("edited (saved)") ? "edited (saved)" : names[0];
  S.pts = clonePts(S.lines[S.activeLine] || []);
  const lv = d.loaded_v;
  if (S.activeLine === "edited (saved)" && Array.isArray(lv)
      && lv.length === S.pts.length) {
    S.V = lv.slice(); S.vT = lv.slice(); S.vStale = false;
  }
  // Seed the revert target with the on-disk CSV (if one exists).
  const savedLine = S.lines["edited (saved)"];
  if (savedLine) {
    S.savedPts = clonePts(savedLine);
    S.savedV = Array.isArray(lv) && lv.length === savedLine.length
      ? lv.slice() : null;
  }
  recomputeRadii();
  S.dirty = false;
  S.sessionDirty = false;
}
