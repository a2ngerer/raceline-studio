"""Raceline Studio — the desktop server.

One command starts everything: the HTTP backend binds a local port, the
browser opens on exactly that URL, and the canvas frontend in ``web/`` talks
to it over JSON + Server-Sent Events.

Usage
-----
    raceline-studio --map maps/demo/map.yaml
    raceline-studio --map map.yaml --line existing.csv --out raceline.csv

Compute lives in :mod:`raceline_studio.core` — this module only adds the
HTTP layer, the cancellable job system, crash-safe persistence and the
scp upload to the car.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .core.centerline import JobCancelled, fast_centerline
from .core.io_utils import (
    _grid_from_png_bytes, _parse_yaml, carpet_grip_scale, compute_velocity,
    load_carpet, load_centerline_file, load_certainty, load_csv, load_map,
    load_v, map_name_from_path, points_certainty, save_carpet, save_certainty,
)
from .core.optimize import run_optimization
from .core.reference_line import smooth_and_resample
from .core.vehicle_params import VehicleParams

_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_STATE: dict = {}
_STATE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Atomic file helpers — a crash mid-write must never corrupt work
# ---------------------------------------------------------------------------

def atomic_write_text(path: str, text: str, backup: bool = False) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if backup and os.path.isfile(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass  # backup is best-effort; the atomic rename below still holds
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_",
                               suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _sidecar(out_path: str, suffix: str) -> str:
    root, _ = os.path.splitext(out_path)
    if root.endswith("_raceline"):
        root = root[: -len("_raceline")]
    return root + suffix


def session_path(out_path: str) -> str:
    return _sidecar(out_path, "_session.json")


def carpet_path(out_path: str) -> str:
    return _sidecar(out_path, "_carpet.json")


def certainty_path(out_path: str) -> str:
    return _sidecar(out_path, "_certainty.json")


def load_session(out_path: str):
    p = session_path(out_path)
    if os.path.isfile(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"[info] unreadable session file ({exc}); ignoring")
    return None


def save_session(out_path: str, payload: dict) -> None:
    payload = dict(payload or {})
    payload["ts"] = time.time()
    atomic_write_text(session_path(out_path), json.dumps(payload))


def clear_session(out_path: str) -> None:
    p = session_path(out_path)
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Job system — one cancellable worker per kind, results pushed over SSE
# ---------------------------------------------------------------------------

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_CURRENT: dict = {}  # kind -> job id
_SSE_QUEUES: list = []
_SSE_LOCK = threading.Lock()


def publish(event: dict) -> None:
    msg = json.dumps(event)
    with _SSE_LOCK:
        for q in list(_SSE_QUEUES):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _job_event(job: dict, **extra) -> None:
    ev = {"type": "job", "id": job["id"], "kind": job["kind"],
          "status": job["status"], "progress": job["progress"],
          "message": job["message"]}
    ev.update(extra)
    publish(ev)


def start_job(kind: str, fn) -> dict:
    """Run ``fn(job)`` on a worker thread. A new job of the same kind cancels
    the previous one (latest wins — exactly what live editing needs)."""
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "status": "running",
           "progress": 0.0, "message": "queued", "result": None,
           "error": None, "cancel": threading.Event(), "ts": time.time()}
    with _JOBS_LOCK:
        prev = _CURRENT.get(kind)
        if prev and prev in _JOBS and _JOBS[prev]["status"] == "running":
            _JOBS[prev]["cancel"].set()
        _JOBS[job["id"]] = job
        _CURRENT[kind] = job["id"]
        # GC finished jobs so a long session cannot leak memory.
        done = [j for j in _JOBS.values() if j["status"] != "running"]
        for j in sorted(done, key=lambda x: x["ts"])[:-20]:
            _JOBS.pop(j["id"], None)

    def runner():
        try:
            fn(job)
            if job["cancel"].is_set():
                raise JobCancelled
            job["status"] = "done"
            job["progress"] = 1.0
            _job_event(job, result=job["result"])
        except JobCancelled:
            job["status"] = "cancelled"
            _job_event(job)
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)
            job["message"] = str(exc)
            _job_event(job)

    threading.Thread(target=runner, daemon=True).start()
    return job


def job_public(job: dict) -> dict:
    pub = {k: job[k] for k in ("id", "kind", "status", "progress", "message")}
    pub["result"] = job["result"]
    pub["error"] = job["error"]
    return pub


# ---------------------------------------------------------------------------
# Upload to car (scp) — strict validation, list-args only, never a shell
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new"]


def _check_target(host, user, port):
    if not _HOST_RE.match(host or ""):
        return "invalid host/IP"
    if not _USER_RE.match(user or ""):
        return "invalid user"
    try:
        if not (0 < int(port) < 65536):
            raise ValueError
    except (TypeError, ValueError):
        return "invalid port"
    return None


def upload_to_car(files, host, user, port, dest, timeout=25):
    """scp ``files`` to ``user@host:dest``. Returns ``(ok, detail)``."""
    err = _check_target(host, user, port)
    if err:
        return False, err
    dest = (dest or "").strip()
    if not dest or dest.startswith("-"):
        return False, "invalid destination path"
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return False, "nothing to upload — save the raceline first"
    try:
        mkdir = subprocess.run(
            ["ssh", "-p", str(int(port)), *_SSH_OPTS, f"{user}@{host}",
             f"mkdir -p {shlex.quote(dest)}"],
            capture_output=True, text=True, timeout=timeout)
        if mkdir.returncode != 0:
            return False, (mkdir.stderr.strip()
                           or "ssh mkdir failed — check IP / SSH key")
        scp = subprocess.run(
            ["scp", "-P", str(int(port)), *_SSH_OPTS, *files,
             f"{user}@{host}:{dest}"],
            capture_output=True, text=True, timeout=timeout)
        if scp.returncode != 0:
            return False, (scp.stderr.strip() or "scp failed")
    except subprocess.TimeoutExpired:
        return False, "upload timed out — is the car reachable on this network?"
    except FileNotFoundError:
        return False, "ssh/scp not installed on this machine"
    names = ", ".join(os.path.basename(f) for f in files)
    return True, f"uploaded {names} -> {user}@{host}:{dest}"


def test_car_connection(host, user, port, timeout=12):
    err = _check_target(host, user, port)
    if err:
        return False, err
    try:
        r = subprocess.run(
            ["ssh", "-p", str(int(port)), *_SSH_OPTS, f"{user}@{host}",
             "echo ok"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "connection timed out"
    except FileNotFoundError:
        return False, "ssh not installed on this machine"
    if r.returncode == 0 and "ok" in r.stdout:
        return True, "connection ok"
    return False, (r.stderr.strip() or "ssh failed")


# ---------------------------------------------------------------------------
# State bootstrap
# ---------------------------------------------------------------------------

def build_state(args) -> None:
    raw, res, origin, W, H, b64 = load_map(args.map)
    ym = _parse_yaml(args.map)
    map_png = os.path.join(os.path.dirname(os.path.abspath(args.map)),
                           ym["image"])
    yaml_meta = {"res": res, "origin": origin,
                 "occ_th": float(ym.get("occupied_thresh", 0.65)),
                 "free_th": float(ym.get("free_thresh", 0.196)),
                 "negate": int(ym.get("negate", 0))}
    veh = VehicleParams()
    R_kin = veh.wheelbase / np.tan(veh.max_steering)
    closed = not args.open_loop

    lines: dict = {}
    loaded = load_csv(args.line)
    if loaded is not None:
        lines[f"loaded: {os.path.basename(args.line)}"] = loaded
    cl_file = load_centerline_file(args.map, closed)
    if cl_file is not None:
        lines["centerline"] = cl_file
    saved = load_csv(args.out)
    if saved is not None:
        lines = {"edited (saved)": saved, **lines}

    # No start line yet? Try the fast skeleton once, synchronously, so the
    # tool can open on a bare map instead of refusing to start.
    if not lines:
        try:
            ref = fast_centerline(raw, res, origin, closed)
            lines["centerline (skeleton)"] = ref.tolist()
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"No start line available and centerline extraction failed "
                f"({exc}). Pass one with --line <centerline_or_raceline.csv>.")

    loaded_v = load_v(args.out) or load_v(args.line)
    carpet = load_carpet(args.out)
    certainty = load_certainty(args.out)
    session = load_session(args.out)

    a_lat_ppcap = 8.0
    upload_cfg = {"host": "", "user": "", "port": 22,
                  "dest": "~/racelines/"}
    if session and isinstance(session.get("upload"), dict):
        u = session["upload"]
        upload_cfg.update({k: u[k] for k in ("host", "user", "port", "dest")
                           if k in u})
    cl_region = None
    if session and isinstance(session.get("cl_region"), list):
        cl_region = session["cl_region"]

    with _STATE_LOCK:
        _STATE.update(
            meta={"origin_x": origin[0], "origin_y": origin[1], "res": res,
                  "W": W, "H": H, "R_min": float(R_kin),
                  "R_safe": float(1.8 * R_kin), "closed": closed,
                  "track": map_name_from_path(args.map),
                  "v_max": float(veh.v_max), "v_min": float(veh.v_min),
                  "mu": round(float(veh.a_lat_max) / 9.81, 3),
                  "mu_pp": round(float(a_lat_ppcap) / 9.81, 3),
                  "mu_carpet": float(carpet.get("mu", 0.9)),
                  "carpet_zones": carpet.get("zones", []),
                  "certainty_zones": certainty.get("zones", []),
                  "cert_neutral": float(certainty.get("neutral", 0.5)),
                  "negate": int(yaml_meta["negate"]),
                  "occ_th": float(yaml_meta["occ_th"]),
                  "free_th": float(yaml_meta["free_th"]),
                  "g": 9.81, "spacing": 0.10},
            map_b64=b64, lines=lines, out=args.out, loaded_v=loaded_v,
            closed=closed, veh=veh, raw=raw, yaml_meta=yaml_meta,
            map_png_path=map_png,
            centerline_xy=lines.get("centerline")
            or lines.get("centerline (skeleton)"),
            carpet=carpet, certainty=certainty, session=session,
            upload=upload_cfg, cl_region=cl_region,
        )
    print(f"map={args.map}  R_min={R_kin:.2f} m  start lines: {list(lines)}")
    print(f"saves to: {args.out}")
    if session:
        age = time.time() - float(session.get("ts", 0))
        print(f"[session] unsaved working state found ({age / 60:.0f} min old)"
              " — the browser will offer to restore it")


def _vehicle_params(body) -> VehicleParams:
    veh = _STATE["veh"]
    mu = float(body.get("mu", _STATE["meta"]["mu"]))
    v_max = float(body.get("v_max", _STATE["meta"]["v_max"]))
    a = mu * 9.81
    return dataclasses.replace(veh, a_lat_max=a, a_long_max=a,
                               a_brake_max=a, v_max=v_max)


def _grip_scale(body, pts):
    carpet = body.get("carpet")
    if carpet is None:
        carpet = _STATE.get("carpet")
    mu_base = float(body.get("mu", _STATE["meta"]["mu"]))
    return carpet_grip_scale(pts, carpet, mu_base)


def _update_grid_from_png(png_data: str) -> None:
    raw_b = base64.b64decode(
        png_data.split(",", 1)[1] if "," in png_data else png_data)
    with _STATE_LOCK:
        _STATE["raw"] = _grid_from_png_bytes(raw_b, _STATE["yaml_meta"])
        _STATE["map_b64"] = base64.b64encode(raw_b).decode("ascii")


# ---------------------------------------------------------------------------
# Job bodies
# ---------------------------------------------------------------------------

def run_centerline_job(job: dict, body: dict) -> None:
    if body.get("png"):
        _update_grid_from_png(body["png"])
    with _STATE_LOCK:
        # "region" present in the body (even null) overrides the stored one,
        # so the client can both set and clear it.
        if "region" in body:
            _STATE["cl_region"] = body["region"]
        raw = _STATE["raw"]
        ym = _STATE["yaml_meta"]
        closed = _STATE["closed"]
        hint = _STATE.get("centerline_xy")
        region = _STATE.get("cl_region")

    def prog(p, msg):
        job["progress"] = p
        job["message"] = msg
        _job_event(job)

    t0 = time.time()
    ref = fast_centerline(raw, ym["res"], ym["origin"], closed,
                          hint_xy=hint, region_xy=region,
                          cancel=job["cancel"], progress=prog)
    pts = ref.tolist()
    with _STATE_LOCK:
        _STATE["centerline_xy"] = pts
        _STATE["lines"]["centerline (live)"] = pts
    job["result"] = {"pts": pts, "elapsed": round(time.time() - t0, 2)}
    job["message"] = f"centerline ready ({time.time() - t0:.2f}s)"


def run_optimize_job(job: dict, body: dict) -> None:
    method = body.get("method", "mincurv")
    if body.get("png"):
        _update_grid_from_png(body["png"])
    with _STATE_LOCK:
        if "region" in body:
            _STATE["cl_region"] = body["region"]
        raw = _STATE["raw"]
        ym = _STATE["yaml_meta"]
        closed = _STATE["closed"]
        cline = body.get("centerline") or _STATE.get("centerline_xy")
        region = _STATE.get("cl_region")
    veh_ui = _vehicle_params(body)   # UI grip/speed for the lap-time score
    veh_geo = _STATE["veh"]          # geometry (width/margin) for the bounds

    def on_progress(frac, msg):
        job["progress"] = frac
        job["message"] = msg
        _job_event(job)

    def on_preview(d):
        _job_event(job, preview=d)

    result = run_optimization(
        method, raw, ym["res"], ym["origin"], closed, cline,
        veh_geo, veh_ui, grip_scale_fn=lambda pts: _grip_scale(body, pts),
        region_xy=region, cancel=job["cancel"],
        on_progress=on_progress, on_preview=on_preview)
    job["result"] = result
    if method == "mintime":
        job["message"] = (f"min time ready — est. lap {result['lap_time']}s "
                          f"(w={result['weight']:g})")
    else:
        job["message"] = f"min curvature ready — est. lap {result['lap_time']}s"


# ---------------------------------------------------------------------------
# CSV save (atomic write + .bak of the previous version)
# ---------------------------------------------------------------------------

def write_raceline_csv(body: dict) -> dict:
    closed = _STATE["closed"]
    pts = np.asarray(body["pts"], dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or pts.shape[0] < 3:
        raise ValueError("pts must be an (N>=3, 2) array")
    vt = body.get("v_targets")
    if vt is not None and len(vt) != len(pts):
        raise ValueError("v_targets length mismatch — recompute the profile")

    carpet = body.get("carpet")
    if carpet is not None:
        _STATE["carpet"] = carpet
        save_carpet(_STATE["out"], carpet)
    certainty = body.get("certainty")
    if certainty is not None:
        _STATE["certainty"] = certainty
        save_certainty(_STATE["out"], certainty)
    else:
        certainty = _STATE.get("certainty")
    cert_zones = (certainty or {}).get("zones") or []

    v = None
    if vt is None and not cert_zones:
        text = "x_m,y_m\n" + "".join(f"{x:.6f},{y:.6f}\n" for x, y in pts)
    else:
        gs = _grip_scale(body, pts)
        v = compute_velocity(
            pts, closed, _vehicle_params(body), v_targets=vt,
            respect_lateral=not body.get("unrestricted", False),
            grip_scale=gs)
        if not cert_zones:
            text = "x_m,y_m,v_mps\n" + "".join(
                f"{x:.6f},{y:.6f},{vi:.4f}\n" for (x, y), vi in zip(pts, v))
        else:
            neutral = float((certainty or {}).get("neutral", 0.5))
            sc, lk, fr = points_certainty(pts, certainty, neutral=neutral)
            text = ("x_m,y_m,v_mps,certainty,cert_lock,cert_force_reactive\n"
                    + "".join(
                        f"{x:.6f},{y:.6f},{vi:.4f},{s:.4f},{int(l)},{int(f)}\n"
                        for (x, y), vi, s, l, f in zip(pts, v, sc, lk, fr)))
    atomic_write_text(_STATE["out"], text, backup=True)

    saved = load_csv(_STATE["out"])
    if saved is not None:
        with _STATE_LOCK:
            lines = {k: val for k, val in _STATE["lines"].items()
                     if k != "edited (saved)"}
            _STATE["lines"] = {"edited (saved)": saved, **lines}
            _STATE["loaded_v"] = v
    return {"ok": True, "path": _STATE["out"],
            "vmin": (min(v) if v else None), "vmax": (max(v) if v else None)}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(b)

    _CT = {".html": "text/html; charset=utf-8",
           ".css": "text/css; charset=utf-8",
           ".js": "application/javascript; charset=utf-8",
           ".png": "image/png", ".json": "application/json",
           ".woff2": "font/woff2", ".svg": "image/svg+xml"}

    def _serve_static(self, rel):
        full = os.path.normpath(os.path.join(_WEB, rel.lstrip("/")))
        if not full.startswith(_WEB) or not os.path.isfile(full):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         self._CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",
                         "max-age=86400" if ext in (".woff2",) else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 64 * 1024 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n).decode() or "{}")

    # ------------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._serve_static("index.html")
            elif any(self.path.startswith(p) for p in
                     ("/js/", "/css/", "/vendor/", "/fonts/", "/fonts.css")):
                self._serve_static(self.path)
            elif self.path == "/init":
                with _STATE_LOCK:
                    payload = {"meta": _STATE["meta"],
                               "map_b64": _STATE["map_b64"],
                               "lines": _STATE["lines"],
                               "loaded_v": _STATE.get("loaded_v"),
                               "out": _STATE["out"],
                               "session": _STATE.get("session"),
                               "upload": _STATE.get("upload"),
                               "cl_region": _STATE.get("cl_region")}
                self._send(200, json.dumps(payload))
            elif self.path == "/ping":
                self._send(200, '{"ok":true}')
            elif self.path.startswith("/job/"):
                jid = self.path.rsplit("/", 1)[-1]
                with _JOBS_LOCK:
                    job = _JOBS.get(jid)
                self._send(200, json.dumps(job_public(job) if job else None))
            elif self.path == "/events":
                self._serve_events()
            else:
                self._send(404, "{}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(500, json.dumps({"ok": False, "error": str(exc)}))
            except OSError:
                pass

    def _serve_events(self):
        q: queue.Queue = queue.Queue(maxsize=256)
        with _SSE_LOCK:
            _SSE_QUEUES.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _SSE_LOCK:
                if q in _SSE_QUEUES:
                    _SSE_QUEUES.remove(q)

    # ----------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        try:
            body = self._read_json()
            closed = _STATE["closed"]

            if self.path == "/velocity":
                pts = smooth_and_resample(
                    np.asarray(body["pts"], dtype=float), closed=closed,
                    spacing=_STATE["meta"]["spacing"], smoothing=0.0)
                gs = _grip_scale(body, pts)
                v = compute_velocity(pts, closed, _vehicle_params(body),
                                     v_targets=None, grip_scale=gs)
                self._send(200, json.dumps(
                    {"pts": pts.astype(float).tolist(), "v": v}))

            elif self.path == "/feasible":
                gs = _grip_scale(body, body["pts"])
                v = compute_velocity(
                    body["pts"], closed, _vehicle_params(body),
                    v_targets=body["v_targets"],
                    respect_lateral=not body.get("unrestricted", False),
                    grip_scale=gs)
                self._send(200, json.dumps({"v": v}))

            elif self.path == "/save":
                out = write_raceline_csv(body)
                # A clean save supersedes the crash-recovery session.
                clear_session(_STATE["out"])
                with _STATE_LOCK:
                    _STATE["session"] = None
                self._send(200, json.dumps(out))

            elif self.path == "/session":
                if body.get("clear"):
                    clear_session(_STATE["out"])
                    with _STATE_LOCK:
                        _STATE["session"] = None
                else:
                    if isinstance(body.get("upload"), dict):
                        with _STATE_LOCK:
                            _STATE["upload"].update({
                                k: body["upload"][k]
                                for k in ("host", "user", "port", "dest")
                                if k in body["upload"]})
                    save_session(_STATE["out"], body)
                    with _STATE_LOCK:
                        _STATE["session"] = body
                self._send(200, '{"ok":true}')

            elif self.path == "/centerline":
                job = start_job("centerline",
                                lambda j: run_centerline_job(j, body))
                self._send(200, json.dumps({"ok": True, "job": job["id"]}))

            elif self.path == "/optimize":
                job = start_job("optimize",
                                lambda j: run_optimize_job(j, body))
                self._send(200, json.dumps({"ok": True, "job": job["id"]}))

            elif self.path == "/cancel":
                jid = body.get("id")
                kind = body.get("kind")
                with _JOBS_LOCK:
                    targets = [j for j in _JOBS.values()
                               if (jid and j["id"] == jid)
                               or (kind and j["kind"] == kind
                                   and j["status"] == "running")]
                for j in targets:
                    j["cancel"].set()
                self._send(200, json.dumps({"ok": True,
                                            "cancelled": len(targets)}))

            elif self.path == "/upload":
                cfg = {**_STATE.get("upload", {}),
                       **{k: body[k] for k in ("host", "user", "port", "dest")
                          if k in body}}
                with _STATE_LOCK:
                    _STATE["upload"] = cfg
                if body.get("test"):
                    ok, detail = test_car_connection(
                        cfg.get("host"), cfg.get("user"), cfg.get("port", 22))
                    self._send(200, json.dumps({"ok": ok, "detail": detail}))
                    return
                # Fresh save before upload when the client sent the line —
                # what lands on the car is exactly what is on screen.
                if body.get("pts"):
                    write_raceline_csv(body)
                files = [_STATE["out"]]
                if body.get("include_sidecars", True):
                    for fn in (carpet_path(_STATE["out"]),
                               certainty_path(_STATE["out"])):
                        if os.path.isfile(fn):
                            files.append(fn)
                if body.get("include_map"):
                    files.append(_STATE["map_png_path"])
                    ypath = (os.path.splitext(_STATE["map_png_path"])[0]
                             + ".yaml")
                    if os.path.isfile(ypath):
                        files.append(ypath)
                ok, detail = upload_to_car(
                    files, cfg.get("host"), cfg.get("user"),
                    cfg.get("port", 22), cfg.get("dest"))
                self._send(200, json.dumps(
                    {"ok": ok, "detail": detail,
                     "files": [os.path.basename(f) for f in files]}))

            elif self.path == "/save_map":
                data = body.get("png", "")
                raw_b = base64.b64decode(
                    data.split(",", 1)[1] if "," in data else data)
                overwrite = bool(body.get("overwrite"))
                dest = _STATE["map_png_path"]
                if not overwrite:
                    root, ext = os.path.splitext(dest)
                    dest = root + "_edited" + ext
                with open(dest, "wb") as f:
                    f.write(raw_b)
                with _STATE_LOCK:
                    _STATE["raw"] = _grid_from_png_bytes(
                        raw_b, _STATE["yaml_meta"])
                    _STATE["map_b64"] = base64.b64encode(raw_b).decode("ascii")
                yaml_path = None
                if body.get("write_yaml", True):
                    ym = _STATE["yaml_meta"]
                    ox, oy = ym["origin"]
                    yaml_path = os.path.splitext(dest)[0] + ".yaml"
                    atomic_write_text(yaml_path, (
                        f"image: {os.path.basename(dest)}\n"
                        f"resolution: {float(ym['res']):.6f}\n"
                        f"origin: [{float(ox):.6f}, {float(oy):.6f}, "
                        f"0.000000]\n"
                        f"negate: {int(ym['negate'])}\n"
                        f"occupied_thresh: {float(ym['occ_th']):.3f}\n"
                        f"free_thresh: {float(ym['free_th']):.3f}\n"))
                self._send(200, json.dumps({"ok": True, "path": dest,
                                            "yaml": yaml_path}))

            else:
                self._send(404, "{}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            # A handler bug must never kill the request thread silently —
            # report it as JSON so the UI can toast it.
            try:
                self._send(500, json.dumps({"ok": False, "error": str(exc)}))
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Raceline Studio")
    ap.add_argument("--map", required=True, help="path to map.yaml")
    ap.add_argument("--line", default="",
                    help="optional start raceline/centerline CSV")
    ap.add_argument("--out", default="", help="where Save writes")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (0.0.0.0 inside containers)")
    ap.add_argument("--port", type=int, default=8754)
    ap.add_argument("--open-loop", action="store_true",
                    help="treat track as open (not a loop)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open the browser automatically")
    args = ap.parse_args()
    if not args.out:
        map_base = map_name_from_path(args.map)
        args.out = os.path.join(os.getcwd(), "racelines",
                                f"{map_base}_raceline.csv")
    args.out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    build_state(args)

    # Frontend and backend are one unit: bind the server (falling back to
    # the next free port if another instance holds the requested one) and
    # open the browser on exactly that URL.
    srv = None
    port = args.port
    for cand in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer((args.host, cand), Handler)
            port = cand
            break
        except OSError as exc:
            if exc.errno not in (48, 98, 10048):  # EADDRINUSE per platform
                raise
    if srv is None:
        raise SystemExit(
            f"ports {args.port}-{args.port + 19} are all in use — "
            "close an old raceline studio instance or pass --port")
    if port != args.port:
        print(f"[info] port {args.port} busy — using {port} instead")
    srv.daemon_threads = True
    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{open_host}:{port}"
    print(f"\n  raceline studio:  {url}\n")
    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        # Slight delay so the server is accepting before the tab loads.
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
