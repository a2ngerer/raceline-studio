/* Demo backend: drop-in replacement for js/api.js with the same exported
   surface, but the "server" is a Web Worker running the identical Python
   core via Pyodide. No network calls after boot; SAVE downloads the CSV;
   the scp upload is unavailable in a browser. */

const connListeners = [];
let connState = "init"; // 'ok' | 'down' | 'init'

function setConn(state) {
  if (state === connState) return;
  connState = state;
  for (const cb of connListeners) cb(state);
}

export function onConn(cb) { connListeners.push(cb); cb(connState); }
export function connOk() { return connState === "ok"; }

/* ---- worker RPC ---- */
const worker = new Worker("pyodide-worker.js");
let seq = 0;
const pending = new Map();
const sseHandlers = [];

worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === "boot") {
    const el = document.getElementById("bootMsg");
    if (el) el.textContent = m.msg;
    return;
  }
  if (m.type === "ready") { setConn("ok"); return; }
  if (m.type === "job") {
    for (const h of sseHandlers) h(m.event);
    return;
  }
  if (m.type === "rpc") {
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.ok) p.resolve(m.data);
    else p.reject(new Error(m.error || "demo backend error"));
  }
};
worker.onerror = (e) => {
  setConn("down");
  const el = document.getElementById("bootMsg");
  if (el) el.textContent = `worker failed: ${e.message || "unknown error"}`;
};

function rpc(cmd, body) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, cmd, body });
  });
}

/* jobs: latest-wins per kind, mirroring the desktop job system. The worker
   runs python synchronously, so while a job computes we only remember the
   newest queued request and fire it when the current one finishes. */
const jobBusy = {};
const jobQueued = {};

function startJob(kind, body) {
  if (jobBusy[kind]) {
    jobQueued[kind] = body;
    return Promise.resolve({ ok: true, job: "queued" });
  }
  jobBusy[kind] = true;
  rpc(kind, body).catch(() => {}).finally(() => {
    jobBusy[kind] = false;
    const q = jobQueued[kind];
    if (q) {
      delete jobQueued[kind];
      startJob(kind, q);
    }
  });
  return Promise.resolve({ ok: true, job: "demo" });
}

/* ---- file downloads replace disk writes ---- */
function downloadText(name, text, mime = "text/csv") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: mime }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ---- compat exports (unused by the app, kept for parity) ---- */
export function get(path) { return Promise.reject(new Error(`no HTTP in demo (${path})`)); }
export function post(path) { return Promise.reject(new Error(`no HTTP in demo (${path})`)); }
export function postLatest(key, path, body) { return rpc(key, body); }

const UPLOAD_MSG = "Upload needs the desktop app — a browser cannot open "
  + "SSH connections. Install: uvx raceline-studio";

/* ---- API surface (same shape as js/api.js) ---- */
export const api = {
  init: () => rpc("init"),
  velocity: (b) => rpc("velocity", b),
  feasible: (b) => rpc("feasible", b),
  save: async (b) => {
    const res = await rpc("save", b);
    downloadText(res.path, res.csv);
    return res;
  },
  session: () => Promise.resolve({ ok: true }),   // localStorage covers drafts
  sessionClear: () => Promise.resolve({ ok: true }),
  centerline: (b) => startJob("centerline", b),
  optimize: (b) => startJob("optimize", b),
  cancel: () => Promise.resolve({ ok: true, cancelled: 0 }),
  upload: () => Promise.resolve({ ok: false, detail: UPLOAD_MSG }),
  saveMap: (png) => {
    const a = document.createElement("a");
    a.href = png;
    a.download = "map_edited.png";
    a.click();
    return Promise.resolve({ ok: true, path: "map_edited.png", yaml: null });
  },
};

export function beaconSession() { return true; }

export function connectSSE(onEvent) {
  sseHandlers.push(onEvent);
  return { close: () => {} };
}
