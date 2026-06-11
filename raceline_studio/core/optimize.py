"""Raceline optimization — minimum curvature and minimum time.

Min curvature is a linearised IQP against the live corridor (asymmetric
normal-cast bounds). Min time sweeps curvature/length blend weights, scores
each candidate by friction-circle lap time and refines the winner.

Pure compute with progress/preview callbacks — the HTTP job system and the
browser (Pyodide) build both wrap :func:`run_optimization`.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import lsq_linear

from .centerline import JobCancelled, region_mask_from_polygon
from .io_utils import _ds_closed
from .map_processing import (
    corridor_halfwidths, corridor_mask_from_centerline, world_to_pixels,
)
from .min_curvature_opt import _second_difference_matrix
from .reference_line import (
    discrete_curvature, heading_and_normals, smooth_and_resample,
)
from .velocity_profile import velocity_profile

# Weights swept by the min-time search. 0 = pure min curvature; larger pulls
# the line shorter. The lap-time score picks the winner per track/µ.
MINTIME_WEIGHTS = (0.0, 0.03, 0.08, 0.15, 0.3, 0.6)


def _first_difference_matrix(n: int, closed: bool) -> np.ndarray:
    E = -np.eye(n)
    if closed:
        idx = np.arange(n)
        E[idx, (idx + 1) % n] += 1.0
    else:
        for i in range(n - 1):
            E[i, i + 1] = 1.0
        E[n - 1, n - 1] = 0.0
    return E


def solve_blend(ref: np.ndarray, nvec: np.ndarray, bounds, closed: bool,
                w_len: float) -> np.ndarray:
    """One linearised solve minimising curvature + ``w_len`` * path length.

    The curvature block mirrors the plain min-curvature IQP; the length block
    penalises first differences of the shifted line, which contracts the
    path. ``w_len=0`` reproduces pure minimum curvature.
    """
    n = ref.shape[0]
    M = _second_difference_matrix(n, closed)
    Nx, Ny = nvec[:, 0], nvec[:, 1]
    blocks_A = [M * Nx[np.newaxis, :], M * Ny[np.newaxis, :]]
    blocks_b = [M @ ref[:, 0], M @ ref[:, 1]]
    if w_len > 0:
        E = _first_difference_matrix(n, closed)
        s = np.sqrt(w_len)
        blocks_A += [s * (E * Nx[np.newaxis, :]), s * (E * Ny[np.newaxis, :])]
        blocks_b += [s * (E @ ref[:, 0]), s * (E @ ref[:, 1])]
    A = np.vstack(blocks_A)
    b = np.concatenate(blocks_b)
    a_lo, a_hi = bounds
    a_hi = np.maximum(a_hi, a_lo + 1e-6)
    res = lsq_linear(A, -b, bounds=(a_lo, a_hi), method="trf",
                     lsmr_tol="auto", max_iter=200)
    return np.asarray(res.x, dtype=float)


def optimize_line(ref, drivable, origin, res, veh, closed, w_len=0.0,
                  iters=4, spacing=0.10, cancel=None, progress=None):
    """Blend-IQP against the live corridor (asymmetric normal-cast bounds)."""
    pts = np.asarray(ref, dtype=float)
    margin = max(0.0, 0.5 * veh.width + veh.safety_margin)
    for it in range(max(1, iters)):
        if cancel is not None and cancel.is_set():
            raise JobCancelled
        _, nvec = heading_and_normals(pts, closed)
        left, right = corridor_halfwidths(drivable, pts, nvec, origin, res)
        a_hi = left - margin
        a_lo = -(right - margin)
        cross = a_lo > a_hi
        mid = 0.5 * (a_lo + a_hi)
        a_hi = np.where(cross, mid, a_hi)
        a_lo = np.where(cross, mid, a_lo)
        alpha = solve_blend(pts, nvec, (a_lo, a_hi), closed, w_len)
        pts = pts + alpha[:, np.newaxis] * nvec
        pts = smooth_and_resample(pts, closed=closed, spacing=spacing,
                                  smoothing=0.1)
        if progress is not None:
            progress(it + 1, pts)
    return pts


def lap_time(pts: np.ndarray, v, closed: bool) -> float:
    ds = _ds_closed(np.asarray(pts, dtype=float), closed)
    v = np.maximum(np.asarray(v, dtype=float), 1e-3)
    return float(np.sum(ds / v))


def run_optimization(method, raw, res, origin, closed, cline_xy,
                     veh_geo, veh_ui, grip_scale_fn=None, region_xy=None,
                     cancel=None, on_progress=None, on_preview=None) -> dict:
    """Full optimization pass shared by the desktop server and the demo.

    ``veh_geo`` supplies the geometry (width / safety margin) for the
    corridor bounds; ``veh_ui`` the grip/speed limits for the lap-time
    score. ``grip_scale_fn(pts)`` may return a per-point friction multiplier
    (carpet zones) or None. Callbacks: ``on_progress(frac, msg)`` and
    ``on_preview({"pts": ..., "lap_time": ..., "label": ...})``.
    """
    def prog(frac, msg):
        if on_progress is not None:
            on_progress(frac, msg)

    def preview(d):
        if on_preview is not None:
            on_preview(d)

    if not cline_xy or len(cline_xy) < 8:
        raise RuntimeError("no centerline yet — run the centerline first")

    cline_xy = np.asarray(cline_xy, dtype=float)
    cline_rc = world_to_pixels(cline_xy, origin, res)
    h, w = raw.shape
    ib = ((cline_rc[:, 0] >= 0) & (cline_rc[:, 0] < h)
          & (cline_rc[:, 1] >= 0) & (cline_rc[:, 1] < w))
    if ib.sum() < 8:
        raise RuntimeError("centerline barely intersects the map")
    free = raw == 0
    rmask = region_mask_from_polygon(region_xy, free.shape, origin, res)
    if rmask is not None:
        free = free & rmask  # optimizer respects the centerline region too
    drivable = corridor_mask_from_centerline(free, cline_rc[ib])
    ref = smooth_and_resample(cline_xy[ib], closed=closed, spacing=0.10,
                              smoothing=0.5)

    def score(pts):
        kappa = discrete_curvature(pts, closed=closed)
        ds = _ds_closed(pts, closed)
        gs = grip_scale_fn(pts) if grip_scale_fn is not None else None
        v = velocity_profile(kappa, ds, veh_ui, closed=closed, grip_scale=gs)
        return v, lap_time(pts, v, closed)

    if method == "mincurv":
        total_iters = 6

        def it_prog(it, pts):
            prog(it / total_iters, f"min curvature — pass {it}/{total_iters}")
            preview({"pts": np.asarray(pts).tolist()})

        opt = optimize_line(ref, drivable, origin, res, veh_geo, closed,
                            w_len=0.0, iters=total_iters,
                            cancel=cancel, progress=it_prog)
        v, t = score(opt)
        return {"method": "mincurv", "pts": opt.tolist(),
                "v": [float(x) for x in v], "lap_time": round(t, 3)}

    # --- min time: sweep curvature/length blends, score by lap time -------
    candidates = []
    nW = len(MINTIME_WEIGHTS)
    for i, w_len in enumerate(MINTIME_WEIGHTS):
        if cancel is not None and cancel.is_set():
            raise JobCancelled
        prog(i / (nW + 1), f"min time — candidate {i + 1}/{nW} (w={w_len:g})")
        # Coarser search pass keeps the sweep interactive; winner is refined.
        opt = optimize_line(ref, drivable, origin, res, veh_geo, closed,
                            w_len=w_len, iters=3, spacing=0.15,
                            cancel=cancel)
        v, t = score(opt)
        candidates.append((t, w_len))
        preview({"pts": opt.tolist(), "lap_time": round(t, 3),
                 "label": f"w={w_len:g}"})
    best_t, best_w = min(candidates)
    prog(nW / (nW + 1), f"min time — refining winner (w={best_w:g})")
    opt = optimize_line(ref, drivable, origin, res, veh_geo, closed,
                        w_len=best_w, iters=5, spacing=0.10, cancel=cancel)
    v, t = score(opt)
    return {"method": "mintime", "pts": opt.tolist(),
            "v": [float(x) for x in v], "lap_time": round(t, 3),
            "weight": best_w,
            "candidates": [{"w": wt, "lap_time": round(tt, 3)}
                           for tt, wt in candidates]}
