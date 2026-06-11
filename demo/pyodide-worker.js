/* Pyodide worker: boots the scientific Python stack, installs the
   raceline_studio core into the virtual filesystem and serves RPCs from
   api-pyodide.js. Python calls run synchronously here — the UI thread
   stays free, and progress callbacks stream out as job events. */

const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
importScripts(PYODIDE_URL + "pyodide.js");

let pyodide = null;
let api = null;
let jobSeq = 0;

function boot(msg) { self.postMessage({ type: "boot", msg }); }
function jobEvent(ev) { self.postMessage({ type: "job", event: ev }); }

function mkdirs(dir) {
  const parts = dir.split("/").filter(Boolean);
  let cur = "";
  for (const p of parts) {
    cur += "/" + p;
    try { pyodide.FS.mkdir(cur); } catch { /* exists */ }
  }
}

async function fetchToFS(url, path) {
  // no-cache: always revalidate with the server (cheap 304s) so a deploy
  // with a new map or core never pairs with stale cached data files.
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`fetch ${url} -> HTTP ${r.status}`);
  const data = new Uint8Array(await r.arrayBuffer());
  mkdirs(path.split("/").slice(0, -1).join("/"));
  pyodide.FS.writeFile(path, data);
}

async function init() {
  boot("loading python runtime…");
  pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
  boot("loading numpy / scipy / scikit-image… (~60 MB, cached after first visit)");
  await pyodide.loadPackage(["numpy", "scipy", "scikit-image", "pillow"]);
  boot("installing raceline core…");
  const manifest = await (await fetch("py/manifest.json",
                                      { cache: "no-cache" })).json();
  for (const f of manifest) await fetchToFS("py/" + f, "/" + f);
  for (const f of ["map.yaml", "map.png", "icra2026_map_raceline.csv",
                   "centerline.csv"]) {
    try { await fetchToFS("maps/" + f, "/maps/" + f); }
    catch { /* optional file */ }
  }
  boot("starting studio…");
  pyodide.runPython('import sys; sys.path.insert(0, "/")');
  api = pyodide.pyimport("raceline_studio.wasm_api");
  const data = JSON.parse(api.studio_init());
  self.postMessage({ type: "ready" });
  return data;
}

/* centerline / optimize emit the same event shape as the desktop SSE. */
function runKind(kind, body) {
  const id = `demo-${++jobSeq}`;
  const base = { type: "job", id, kind, status: "running",
                 progress: 0, message: "queued" };
  jobEvent({ ...base });
  const progress = (frac, msg) =>
    jobEvent({ ...base, progress: frac, message: msg });
  try {
    if (kind === "centerline") {
      const result = JSON.parse(
        api.studio_centerline(JSON.stringify(body || {}), progress));
      jobEvent({ ...base, status: "done", progress: 1,
                 message: `centerline ready (${result.elapsed}s)`, result });
      return { ok: true, job: id };
    }
    const preview = (pjson) =>
      jobEvent({ ...base, preview: JSON.parse(pjson) });
    const result = JSON.parse(
      api.studio_optimize(JSON.stringify(body || {}), progress, preview));
    const message = result.method === "mintime"
      ? `min time ready — est. lap ${result.lap_time}s (w=${result.weight})`
      : `min curvature ready — est. lap ${result.lap_time}s`;
    jobEvent({ ...base, status: "done", progress: 1, message, result });
    return { ok: true, job: id };
  } catch (err) {
    const message = pyErrorMessage(err);
    jobEvent({ ...base, status: "error", message });
    throw new Error(message);
  }
}

/* Pyodide wraps python exceptions in long tracebacks — keep the last line. */
function pyErrorMessage(err) {
  const s = String(err.message || err);
  const lines = s.trim().split("\n");
  return lines[lines.length - 1].replace(/^\w+Error: /, "");
}

self.onmessage = async (e) => {
  const { id, cmd, body } = e.data;
  try {
    let data;
    if (cmd === "init") data = await init();
    else if (cmd === "velocity") {
      data = JSON.parse(api.studio_velocity(JSON.stringify(body)));
    } else if (cmd === "feasible") {
      data = JSON.parse(api.studio_feasible(JSON.stringify(body)));
    } else if (cmd === "save") {
      data = JSON.parse(api.studio_save(JSON.stringify(body)));
    } else if (cmd === "centerline" || cmd === "optimize") {
      data = runKind(cmd, body);
    } else {
      throw new Error(`unknown command ${cmd}`);
    }
    self.postMessage({ type: "rpc", id, ok: true, data });
  } catch (err) {
    self.postMessage({ type: "rpc", id, ok: false,
                       error: pyErrorMessage(err) });
  }
};
