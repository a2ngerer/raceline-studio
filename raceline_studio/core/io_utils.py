"""Map / CSV / sidecar IO and the velocity wrapper shared by all frontends.

Extracted from the original in-repo editor so the HTTP server, the tests and
the Pyodide (browser demo) build all run the exact same maths.
"""
from __future__ import annotations

import base64
import io
import json
import os

import numpy as np

from .reference_line import arc_length, discrete_curvature, smooth_and_resample
from .velocity_profile import velocity_profile


def _parse_yaml(path):
    """Minimal map.yaml parser (avoids a hard pyyaml dependency)."""
    meta = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("["):
            meta[k] = [float(x) for x in v.strip("[]").split(",")]
        else:
            try:
                meta[k] = float(v)
            except ValueError:
                meta[k] = v
    return meta


def load_map(yaml_path):
    from PIL import Image
    meta = _parse_yaml(yaml_path)
    img_path = os.path.join(os.path.dirname(os.path.abspath(yaml_path)),
                            meta["image"])
    res = float(meta["resolution"])
    origin = (float(meta["origin"][0]), float(meta["origin"][1]))
    occ_th = float(meta.get("occupied_thresh", 0.65))
    free_th = float(meta.get("free_thresh", 0.196))
    negate = int(meta.get("negate", 0))

    img = np.asarray(Image.open(img_path).convert("L"), dtype=np.float64)
    img = np.flipud(img)  # ROS map_server: grid row 0 = bottom of image
    p = img / 255.0 if negate else (255.0 - img) / 255.0
    raw = np.full(img.shape, -1, dtype=np.int16)
    raw[p > occ_th] = 100
    raw[p < free_th] = 0
    # Re-encode as PNG for the browser. The source map may be a .pgm (the
    # default map_saver_cli format), which browsers cannot decode from a
    # data:image/png URI; transcoding via PIL makes any on-disk format render.
    _buf = io.BytesIO()
    Image.open(img_path).convert("L").save(_buf, format="PNG")
    b64 = base64.b64encode(_buf.getvalue()).decode("ascii")
    H, W = img.shape
    return raw, res, origin, W, H, b64


def load_csv(path):
    if not path or not os.path.isfile(path):
        return None
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    return data[:, :2].astype(float).tolist()


def load_v(path):
    """Return the per-point speed column of a 3-column CSV, else None."""
    if not path or not os.path.isfile(path):
        return None
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    if data.ndim == 2 and data.shape[1] >= 3:
        return data[:, 2].astype(float).tolist()
    return None


def load_centerline_file(map_yaml, closed):
    """Load a hand-made centerline sitting next to the map (``centerline.csv``
    or ``centreline.csv``). Accepts the TUM 4-column format
    (``x_m, y_m, w_tr_right_m, w_tr_left_m`` with a ``#`` header) or a plain
    ``x_m,y_m`` CSV; only x,y are used. Returns resampled points, else None."""
    d = os.path.dirname(os.path.abspath(map_yaml))
    for name in ("centerline.csv", "centreline.csv"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            # Tolerate a plain "x_m,y_m" header row (loadtxt only skips
            # "#" comments on its own).
            with open(p) as f:
                first = f.readline()
            skip = 1 if any(c.isalpha() for c in first.split(",")[0]) else 0
            data = np.loadtxt(p, delimiter=",", comments="#", skiprows=skip)
            if data.ndim != 2 or data.shape[1] < 2:
                continue
            xy = data[:, :2].astype(float)
            ref = smooth_and_resample(xy, closed=closed, spacing=0.10,
                                      smoothing=0.5)
            return ref.tolist()
    return None


def _ds_closed(pts, closed):
    """Per-point arc-length spacing along the line."""
    s = arc_length(pts, closed=closed)
    if closed:
        total = s[-1] + float(np.linalg.norm(pts[0] - pts[-1]))
        return np.concatenate([np.diff(s), [total - s[-1]]])
    tail = float(s[-1] - s[-2]) if len(s) > 1 else 0.1
    return np.concatenate([np.diff(s), [tail]])


def compute_velocity(pts, closed, params, v_targets=None, respect_lateral=True,
                     grip_scale=None):
    """Run the friction-circle velocity profile on a line. With v_targets
    given, the forward/backward passes treat those as upper bounds
    (feasibility smoothing of hand-edited speeds); without, the
    friction-circle cap is used.

    ``respect_lateral=False`` (unrestricted mode) drops the lateral grip limit
    so a corner can be tuned above a_lat_max, while the a_long/a_brake
    coupling between neighbouring points stays intact.

    ``grip_scale`` is an optional (N,) per-point multiplier on the friction
    limits, used to apply hand-drawn carpet zones (stronger local µ)."""
    pts = np.asarray(pts, dtype=float)
    kappa = discrete_curvature(pts, closed=closed)
    ds = _ds_closed(pts, closed)
    vt = None if v_targets is None else np.asarray(v_targets, dtype=float)
    v = velocity_profile(kappa, ds, params, closed=closed, v_init=vt,
                         respect_lateral=respect_lateral,
                         grip_scale=grip_scale)
    return v.astype(float).tolist()


def _points_in_polygon(pts, poly):
    """Boolean mask of which (x, y) points fall inside a polygon.

    Even-odd ray casting, fully vectorised over ``pts``. ``poly`` is an
    (M, 2) ring of world-frame vertices (open; the closing edge is implied)."""
    pts = np.asarray(pts, dtype=float)
    poly = np.asarray(poly, dtype=float)
    if poly.shape[0] < 3 or pts.shape[0] == 0:
        return np.zeros(pts.shape[0], dtype=bool)
    x, y = pts[:, 0], pts[:, 1]
    inside = np.zeros(pts.shape[0], dtype=bool)
    xj, yj = poly[-1]
    for xi, yi in poly:
        cond = (yi > y) != (yj > y)
        # x coordinate of the edge at height y, guarding the horizontal case
        denom = np.where(yj != yi, yj - yi, 1.0)
        x_cross = (xj - xi) * (y - yi) / denom + xi
        inside ^= cond & (x < x_cross)
        xj, yj = xi, yi
    return inside


def carpet_grip_scale(pts, carpet, mu_base):
    """Per-point friction multiplier from hand-drawn carpet zones.

    ``carpet`` is ``{"mu": <carpet_mu>, "zones": [[[x, y], ...], ...]}``.
    Points inside any zone polygon get ``mu_carpet / mu_base`` (the stronger
    grip); everything else stays at 1.0. Returns ``None`` when no usable zone
    exists so the caller falls back to the single-grip path unchanged."""
    if not carpet:
        return None
    zones = carpet.get("zones") or []
    if not zones or mu_base <= 0:
        return None
    mu_carpet = float(carpet.get("mu", mu_base))
    scale = mu_carpet / mu_base
    pts = np.asarray(pts, dtype=float)
    inside = np.zeros(pts.shape[0], dtype=bool)
    for poly in zones:
        poly = np.asarray(poly, dtype=float)
        if poly.ndim == 2 and poly.shape[0] >= 3:
            inside |= _points_in_polygon(pts, poly)
    if not inside.any():
        return None
    gs = np.ones(pts.shape[0], dtype=float)
    gs[inside] = scale
    return gs


def carpet_sidecar_path(out_path):
    """``racelines/<map>_raceline.csv`` -> ``racelines/<map>_carpet.json``."""
    root, _ = os.path.splitext(out_path)
    if root.endswith("_raceline"):
        root = root[: -len("_raceline")]
    return root + "_carpet.json"


def points_certainty(pts, certainty, neutral=0.5):
    """Per-point certainty score, lock flag and force-reactive flag.

    ``certainty`` is ``{"neutral": 0.5, "zones": [{"score": float,
    "lock": bool, "force": bool, "polygon": [[x, y], ...]}, ...]}``.

    Overlap resolution: the zone whose score is *farthest* from ``neutral``
    (max |score - neutral|) wins — deterministic and order-independent.
    ``lock`` (hard pure-pursuit) and ``force`` (hard reactive) are each the
    OR over all zones containing the point.

    Returns ``(scores, locks, forces)`` as float64 / bool / bool arrays of
    shape ``(N,)``. Returns all-neutral / all-False when no zones."""
    pts = np.asarray(pts, dtype=float)
    N = pts.shape[0]
    scores = np.full(N, neutral, dtype=float)
    locks = np.zeros(N, dtype=bool)
    forces = np.zeros(N, dtype=bool)
    if not certainty:
        return scores, locks, forces
    zones = certainty.get("zones") or []
    if not zones:
        return scores, locks, forces
    best_dist = np.zeros(N, dtype=float)
    for z in zones:
        poly = z.get("polygon") or []
        score = float(z.get("score", neutral))
        lock = bool(z.get("lock", False))
        force = bool(z.get("force", False))
        poly = np.asarray(poly, dtype=float)
        if poly.ndim != 2 or poly.shape[0] < 3:
            continue
        inside = _points_in_polygon(pts, poly)
        dist = abs(score - neutral)
        overwrite = inside & (dist > best_dist)
        scores[overwrite] = score
        best_dist[overwrite] = dist
        if lock:
            locks[inside] = True
        if force:
            forces[inside] = True
    return scores, locks, forces


def certainty_sidecar_path(out_path):
    """``racelines/<map>_raceline.csv`` -> ``racelines/<map>_certainty.json``."""
    root, _ = os.path.splitext(out_path)
    if root.endswith("_raceline"):
        root = root[: -len("_raceline")]
    return root + "_certainty.json"


def load_certainty(out_path):
    """Load persisted certainty zones for a map, or an empty default."""
    p = certainty_sidecar_path(out_path)
    if os.path.isfile(p):
        try:
            with open(p) as f:
                data = json.load(f)
            zones = []
            for z in data.get("zones", []):
                poly = [[float(x), float(y)] for x, y in z.get("polygon", [])]
                zones.append({"score": float(z.get("score", 0.5)),
                              "lock": bool(z.get("lock", False)),
                              "force": bool(z.get("force", False)),
                              "polygon": poly})
            return {"neutral": float(data.get("neutral", 0.5)), "zones": zones}
        except Exception as exc:  # noqa: BLE001
            print(f"[info] could not read certainty zones ({exc});"
                  " starting empty")
    return {"neutral": 0.5, "zones": []}


def save_certainty(out_path, certainty):
    """Persist certainty zones next to the raceline. An empty zone list
    removes the sidecar so a cleared certainty map does not silently linger."""
    p = certainty_sidecar_path(out_path)
    zones = (certainty or {}).get("zones") or []
    if not zones:
        if os.path.isfile(p):
            os.remove(p)
        return None
    payload = {
        "neutral": float((certainty or {}).get("neutral", 0.5)),
        "zones": [
            {"score": float(z.get("score", 0.5)),
             "lock": bool(z.get("lock", False)),
             "force": bool(z.get("force", False)),
             "polygon": [[float(x), float(y)]
                         for x, y in z.get("polygon", [])]}
            for z in zones
        ],
    }
    with open(p, "w") as f:
        json.dump(payload, f, indent=2)
    return p


def load_carpet(out_path):
    """Load persisted carpet zones for a map, or an empty default."""
    p = carpet_sidecar_path(out_path)
    if os.path.isfile(p):
        try:
            with open(p) as f:
                data = json.load(f)
            zones = [[[float(x), float(y)] for x, y in z]
                     for z in data.get("zones", [])]
            return {"mu": float(data.get("mu", 0.9)), "zones": zones}
        except Exception as exc:  # noqa: BLE001
            print(f"[info] could not read carpet zones ({exc}); starting empty")
    return {"mu": 0.9, "zones": []}


def save_carpet(out_path, carpet):
    """Persist carpet zones next to the raceline. An empty zone list removes
    the sidecar so a cleared carpet does not silently linger."""
    p = carpet_sidecar_path(out_path)
    zones = (carpet or {}).get("zones") or []
    if not zones:
        if os.path.isfile(p):
            os.remove(p)
        return None
    payload = {"mu": float((carpet or {}).get("mu", 0.9)),
               "zones": [[[float(x), float(y)] for x, y in z] for z in zones]}
    with open(p, "w") as f:
        json.dump(payload, f, indent=2)
    return p


def _grid_from_png_bytes(png_bytes, ym):
    """Binarize an in-memory PNG into an occupancy grid, mirroring load_map."""
    from PIL import Image
    img = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("L"),
                     dtype=np.float64)
    img = np.flipud(img)  # ROS map_server: grid row 0 = bottom of image
    p = img / 255.0 if ym["negate"] else (255.0 - img) / 255.0
    raw = np.full(img.shape, -1, dtype=np.int16)
    raw[p > ym["occ_th"]] = 100
    raw[p < ym["free_th"]] = 0
    return raw


def map_name_from_path(map_path):
    """The map name a raceline file is auto-named by (``<map>_raceline.csv``).

    Mirrors the ``maps/<map>/map.yaml`` layout: a generic ``map.yaml``
    basename means the map name is the parent directory (e.g.
    ``maps/demo/map.yaml`` -> ``demo``). A named ``<map>.yaml`` keeps its
    basename."""
    base = os.path.splitext(os.path.basename(map_path))[0]
    if base in ("map", ""):
        parent = os.path.basename(os.path.dirname(os.path.abspath(map_path)))
        return parent or base
    return base
