/* Backend client: JSON endpoints, SSE stream, connection health.
   Every call rejects with a readable Error; callers toast it. The
   connection watcher flips a global online/offline state so the UI can
   show a banner instead of silently failing. */

const connListeners = [];
let connState = "init"; // 'ok' | 'down' | 'init'
let pingTimer = null;

function setConn(state) {
  if (state === connState) return;
  connState = state;
  for (const cb of connListeners) cb(state);
  if (state === "down" && !pingTimer) {
    pingTimer = setInterval(async () => {
      try {
        const r = await fetch("/ping", { cache: "no-store" });
        if (r.ok) { clearInterval(pingTimer); pingTimer = null; setConn("ok"); }
      } catch { /* keep pinging */ }
    }, 2000);
  }
}

export function onConn(cb) { connListeners.push(cb); cb(connState); }
export function connOk() { return connState === "ok"; }

async function req(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { cache: "no-store", ...opts });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    setConn("down");
    throw new Error("backend unreachable");
  }
  setConn("ok");
  let data = null;
  try { data = await r.json(); } catch { /* non-JSON error page */ }
  if (!r.ok) {
    throw new Error((data && data.error) || `${path} -> HTTP ${r.status}`);
  }
  return data;
}

export function get(path, opts) { return req(path, opts); }

export function post(path, body, opts = {}) {
  return req(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    ...opts,
  });
}

/* ---- single-flight wrappers: a newer call aborts the in-flight one ---- */
const flights = {};
export function postLatest(key, path, body) {
  if (flights[key]) flights[key].abort();
  const ctrl = new AbortController();
  flights[key] = ctrl;
  return post(path, body, { signal: ctrl.signal }).finally(() => {
    if (flights[key] === ctrl) delete flights[key];
  });
}

/* ---- API surface ---- */
export const api = {
  init: () => get("/init"),
  velocity: (b) => postLatest("velocity", "/velocity", b),
  feasible: (b) => postLatest("feasible", "/feasible", b),
  save: (b) => post("/save", b),
  session: (b) => post("/session", b),
  sessionClear: () => post("/session", { clear: true }),
  centerline: (b) => post("/centerline", b),
  optimize: (b) => post("/optimize", b),
  cancel: (kind) => post("/cancel", { kind }),
  upload: (b) => post("/upload", b),
  saveMap: (png, overwrite) => post("/save_map", { png, overwrite }),
};

/* Crash-flush: best-effort session push when the tab closes mid-edit. */
export function beaconSession(payload) {
  try {
    const blob = new Blob([JSON.stringify(payload)],
      { type: "application/json" });
    return navigator.sendBeacon("/session", blob);
  } catch { return false; }
}

/* ---- Server-Sent Events with auto-reconnect ---- */
export function connectSSE(onEvent) {
  let es;
  const open = () => {
    es = new EventSource("/events");
    es.onopen = () => setConn("ok");
    es.onerror = () => setConn("down"); // EventSource retries on its own
    es.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)); } catch { /* ignore bad frame */ }
    };
  };
  open();
  return { close: () => es && es.close() };
}
