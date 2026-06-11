"""Pyodide bridge — the browser demo backend.

Runs the identical :mod:`raceline_studio.core` maths inside a Web Worker.
Every function takes/returns JSON strings (no proxy lifetimes to manage)
and mirrors one HTTP endpoint of :mod:`raceline_studio.server`. Progress
and preview callbacks are plain JS functions passed in by the worker.

No threads, no filesystem persistence: SAVE returns the CSV text for the
browser to download, sessions stay in localStorage, upload is unavailable.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import time

import numpy as np

from .core.centerline import fast_centerline
from .core.io_utils import (
    _grid_from_png_bytes, _parse_yaml, carpet_grip_scale, compute_velocity,
    load_centerline_file, load_csv, load_map, load_v, points_certainty,
)
from .core.optimize import run_optimization
from .core.reference_line import smooth_and_resample
from .core.vehicle_params import VehicleParams

MAP_YAML = "/maps/map.yaml"
RACELINE = "/maps/demo_raceline.csv"

STATE: dict = {}


def studio_init() -> str:
    raw, res, origin, W, H, b64 = load_map(MAP_YAML)
    ym = _parse_yaml(MAP_YAML)
    yaml_meta = {"res": res, "origin": origin,
                 "occ_th": float(ym.get("occupied_thresh", 0.65)),
                 "free_th": float(ym.get("free_thresh", 0.196)),
                 "negate": int(ym.get("negate", 0))}
    veh = VehicleParams()
    R_kin = veh.wheelbase / np.tan(veh.max_steering)
    closed = True

    lines: dict = {}
    saved = load_csv(RACELINE)
    if saved is not None:
        lines["edited (saved)"] = saved
    cl_file = load_centerline_file(MAP_YAML, closed)
    if cl_file is not None:
        lines["centerline"] = cl_file
    if not lines:
        lines["centerline (skeleton)"] = fast_centerline(
            raw, res, origin, closed).tolist()

    STATE.update(
        meta={"origin_x": origin[0], "origin_y": origin[1], "res": res,
              "W": W, "H": H, "R_min": float(R_kin),
              "R_safe": float(1.8 * R_kin), "closed": closed,
              "track": "demo",
              "v_max": float(veh.v_max), "v_min": float(veh.v_min),
              "mu": round(float(veh.a_lat_max) / 9.81, 3),
              "mu_pp": round(8.0 / 9.81, 3),
              "mu_carpet": 0.9,
              "carpet_zones": [], "certainty_zones": [],
              "cert_neutral": 0.5,
              "negate": yaml_meta["negate"], "occ_th": yaml_meta["occ_th"],
              "free_th": yaml_meta["free_th"],
              "g": 9.81, "spacing": 0.10},
        raw=raw, yaml_meta=yaml_meta, closed=closed, veh=veh,
        lines=lines, carpet={"mu": 0.9, "zones": []},
        certainty={"neutral": 0.5, "zones": []},
        centerline_xy=lines.get("centerline")
        or lines.get("centerline (skeleton)") or lines.get("edited (saved)"),
        cl_region=None,
    )
    return json.dumps({
        "meta": STATE["meta"], "map_b64": b64, "lines": lines,
        "loaded_v": load_v(RACELINE), "out": "demo_raceline.csv",
        "session": None,
        "upload": {"host": "", "user": "", "port": 22, "dest": ""},
        "cl_region": None,
    })


def _vehicle_params(body) -> VehicleParams:
    veh = STATE["veh"]
    mu = float(body.get("mu", STATE["meta"]["mu"]))
    v_max = float(body.get("v_max", STATE["meta"]["v_max"]))
    a = mu * 9.81
    return dataclasses.replace(veh, a_lat_max=a, a_long_max=a,
                               a_brake_max=a, v_max=v_max)


def _grip_scale(body, pts):
    carpet = body.get("carpet")
    if carpet is None:
        carpet = STATE.get("carpet")
    mu_base = float(body.get("mu", STATE["meta"]["mu"]))
    return carpet_grip_scale(pts, carpet, mu_base)


def _update_grid_from_png(png_data: str) -> None:
    raw_b = base64.b64decode(
        png_data.split(",", 1)[1] if "," in png_data else png_data)
    STATE["raw"] = _grid_from_png_bytes(raw_b, STATE["yaml_meta"])


def studio_velocity(body_json: str) -> str:
    body = json.loads(body_json)
    pts = smooth_and_resample(
        np.asarray(body["pts"], dtype=float), closed=STATE["closed"],
        spacing=STATE["meta"]["spacing"], smoothing=0.0)
    gs = _grip_scale(body, pts)
    v = compute_velocity(pts, STATE["closed"], _vehicle_params(body),
                         v_targets=None, grip_scale=gs)
    return json.dumps({"pts": pts.astype(float).tolist(), "v": v})


def studio_feasible(body_json: str) -> str:
    body = json.loads(body_json)
    gs = _grip_scale(body, body["pts"])
    v = compute_velocity(
        body["pts"], STATE["closed"], _vehicle_params(body),
        v_targets=body["v_targets"],
        respect_lateral=not body.get("unrestricted", False),
        grip_scale=gs)
    return json.dumps({"v": v})


def studio_save(body_json: str) -> str:
    """Build the raceline CSV text; the browser downloads it as a file."""
    body = json.loads(body_json)
    closed = STATE["closed"]
    pts = np.asarray(body["pts"], dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or pts.shape[0] < 3:
        raise ValueError("pts must be an (N>=3, 2) array")
    vt = body.get("v_targets")
    if vt is not None and len(vt) != len(pts):
        raise ValueError("v_targets length mismatch — recompute the profile")

    certainty = body.get("certainty") or STATE.get("certainty")
    if body.get("certainty") is not None:
        STATE["certainty"] = body["certainty"]
    if body.get("carpet") is not None:
        STATE["carpet"] = body["carpet"]
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

    saved = pts.tolist()
    lines = {k: val for k, val in STATE["lines"].items()
             if k != "edited (saved)"}
    STATE["lines"] = {"edited (saved)": saved, **lines}
    return json.dumps({"ok": True, "path": "demo_raceline.csv", "csv": text,
                       "vmin": (min(v) if v else None),
                       "vmax": (max(v) if v else None)})


def studio_centerline(body_json: str, progress=None) -> str:
    body = json.loads(body_json)
    if body.get("png"):
        _update_grid_from_png(body["png"])
    if "region" in body:
        STATE["cl_region"] = body["region"]

    def prog(p, msg):
        if progress is not None:
            progress(float(p), str(msg))

    t0 = time.time()
    ref = fast_centerline(STATE["raw"], STATE["yaml_meta"]["res"],
                          STATE["yaml_meta"]["origin"], STATE["closed"],
                          hint_xy=STATE.get("centerline_xy"),
                          region_xy=STATE.get("cl_region"), progress=prog)
    pts = ref.tolist()
    STATE["centerline_xy"] = pts
    STATE["lines"]["centerline (live)"] = pts
    return json.dumps({"pts": pts, "elapsed": round(time.time() - t0, 2)})


def studio_optimize(body_json: str, progress=None, preview=None) -> str:
    body = json.loads(body_json)
    method = body.get("method", "mincurv")
    if body.get("png"):
        _update_grid_from_png(body["png"])
    if "region" in body:
        STATE["cl_region"] = body["region"]
    cline = body.get("centerline") or STATE.get("centerline_xy")

    def on_progress(frac, msg):
        if progress is not None:
            progress(float(frac), str(msg))

    def on_preview(d):
        if preview is not None:
            preview(json.dumps(d))

    result = run_optimization(
        method, STATE["raw"], STATE["yaml_meta"]["res"],
        STATE["yaml_meta"]["origin"], STATE["closed"], cline,
        STATE["veh"], _vehicle_params(body),
        grip_scale_fn=lambda pts: _grip_scale(body, pts),
        region_xy=STATE.get("cl_region"),
        on_progress=on_progress, on_preview=on_preview)
    return json.dumps(result)
