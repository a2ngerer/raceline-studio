"""Raceline optimization — minimum curvature and minimum time.

Min curvature is a linearised IQP against the live corridor (asymmetric
normal-cast bounds). Min time sweeps curvature/length blend weights, scores
each candidate by friction-circle lap time and refines the winner.

Pure compute with progress/preview callbacks — the HTTP job system and the
browser (Pyodide) build both wrap :func:`run_optimization`.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import (
    binary_erosion, distance_transform_edt, minimum_filter1d,
    uniform_filter1d,
)
from scipy.optimize import lsq_linear

from .centerline import JobCancelled, region_mask_from_polygon
from .io_utils import _ds_closed
from .map_processing import (
    corridor_halfwidths, corridor_mask_from_centerline, pixels_to_world,
    world_to_pixels,
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


def _drivable_snapper(drivable, origin, res, margin):
    """Return a function pulling off-corridor points back into the corridor.

    The spline resample between IQP iterations can cut a hairpin corner
    through a thin interior wall; once a point sits on a wall the normal
    cast collapses to zero bounds and the line never recovers. Off-corridor
    points snap to the nearest pixel of the margin-eroded corridor core —
    landing a proper wall clearance away, not hugging the wall — with the
    plain corridor as fallback where the core is locally out of reach
    (genuine pinch narrower than the vehicle)."""
    depth = distance_transform_edt(drivable)
    core = drivable & (depth >= max(1.0, margin / res))
    if not core.any():
        core = drivable
    dist_core, ind_core = distance_transform_edt(~core, return_indices=True)
    _, ind_any = distance_transform_edt(~drivable, return_indices=True)
    core_reach = 2.0 * margin / res + 4.0

    def snap(pts):
        rc = world_to_pixels(pts, origin, res)
        h, w = drivable.shape
        rc[:, 0] = np.clip(rc[:, 0], 0, h - 1)
        rc[:, 1] = np.clip(rc[:, 1], 0, w - 1)
        bad = ~drivable[rc[:, 0], rc[:, 1]]
        if not bad.any():
            return pts
        use_core = bad & (dist_core[rc[:, 0], rc[:, 1]] <= core_reach)
        pts = pts.copy()
        for mask, ind in ((use_core, ind_core), (bad & ~use_core, ind_any)):
            if mask.any():
                nearest = np.stack([ind[0][rc[mask, 0], rc[mask, 1]],
                                    ind[1][rc[mask, 0], rc[mask, 1]]], axis=1)
                pts[mask] = pixels_to_world(nearest, origin, res)
        return pts

    return snap


def optimize_line(ref, drivable, origin, res, veh, closed, w_len=0.0,
                  iters=4, spacing=0.10, cancel=None, progress=None,
                  margin=None):
    """Blend-IQP against the live corridor (asymmetric normal-cast bounds).

    ``margin=None`` derives the wall margin from the vehicle; pass an
    explicit (smaller) value when the margin is already baked into the
    ``drivable`` mask via erosion."""
    pts = np.asarray(ref, dtype=float)
    if margin is None:
        margin = max(0.0, 0.5 * veh.width + veh.safety_margin)
    snap = _drivable_snapper(drivable, origin, res, margin)
    for it in range(max(1, iters)):
        if cancel is not None and cancel.is_set():
            raise JobCancelled
        _, nvec = heading_and_normals(pts, closed)
        left, right = corridor_halfwidths(drivable, pts, nvec, origin, res)
        # The per-point pixel cast is noisy on narrow corridors and the IQP
        # solution inherits that noise as a jagged line. Smooth the widths
        # along the line — conservatively: a min filter first (never widen a
        # narrow spot), then a short mean.
        mode = "wrap" if closed else "nearest"
        left = uniform_filter1d(
            minimum_filter1d(left, 3, mode=mode), 5, mode=mode)
        right = uniform_filter1d(
            minimum_filter1d(right, 3, mode=mode), 5, mode=mode)
        a_hi = left - margin
        a_lo = -(right - margin)
        cross = a_lo > a_hi
        mid = 0.5 * (a_lo + a_hi)
        a_hi = np.where(cross, mid, a_hi)
        a_lo = np.where(cross, mid, a_lo)
        alpha = solve_blend(pts, nvec, (a_lo, a_hi), closed, w_len)
        pts = pts + alpha[:, np.newaxis] * nvec
        pts = snap(smooth_and_resample(pts, closed=closed, spacing=spacing,
                                       smoothing=0.1))
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
    # Bake most of the vehicle margin into the corridor mask itself: erode
    # the free space by the half width + safety margin before flooding.
    # This (a) closes gaps narrower than the vehicle (cone rows, dotted
    # dividers) so neither the flood nor the normal cast can leak through,
    # and (b) prevents the cast from jumping hairline diagonal walls.
    margin = max(0.0, 0.5 * veh_geo.width + veh_geo.safety_margin)
    margin_px = max(1, int(margin / res))
    eroded = binary_erosion(free, iterations=margin_px)
    if eroded.any():
        free = eroded
        margin_resid = max(0.0, margin - margin_px * res)
    else:
        margin_resid = margin  # ultra-narrow map: keep the soft margin only
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
                            w_len=0.0, iters=total_iters, margin=margin_resid,
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
                            margin=margin_resid, cancel=cancel)
        v, t = score(opt)
        candidates.append((t, w_len))
        preview({"pts": opt.tolist(), "lap_time": round(t, 3),
                 "label": f"w={w_len:g}"})
    best_t, best_w = min(candidates)
    prog(nW / (nW + 1), f"min time — refining winner (w={best_w:g})")
    opt = optimize_line(ref, drivable, origin, res, veh_geo, closed,
                        w_len=best_w, iters=5, spacing=0.10,
                        margin=margin_resid, cancel=cancel)
    v, t = score(opt)
    return {"method": "mintime", "pts": opt.tolist(),
            "v": [float(x) for x in v], "lap_time": round(t, 3),
            "weight": best_w,
            "candidates": [{"w": wt, "lap_time": round(tt, 3)}
                           for tt, wt in candidates]}
