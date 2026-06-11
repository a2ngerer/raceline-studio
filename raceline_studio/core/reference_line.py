"""Reference-line construction: periodic spline smoothing, arc-length
resampling, heading and normal-vector computation, curvature estimation.

The TUM min-curvature formulation operates on a discrete reference line
with uniform arc-length spacing and pre-computed unit normal vectors;
this module produces exactly that representation.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.interpolate import splev, splprep


def smooth_and_resample(
    pts_xy: np.ndarray,
    closed: bool,
    spacing: float,
    smoothing: float = 0.5,
) -> np.ndarray:
    """Spline-smooth a polyline and resample at fixed arc-length spacing.

    Parameters
    ----------
    pts_xy : (N, 2) float array of input points.
    closed : whether the polyline forms a closed loop.
    spacing : target distance between consecutive resampled points [m].
    smoothing : SciPy spline smoothing factor `s`. 0 = exact interpolation,
                larger = smoother. ~ 0.5 m^2 removes pixel-grid jitter
                without flattening real corners.

    Returns
    -------
    (M, 2) resampled points along the smoothed spline.
    """
    pts = np.asarray(pts_xy, dtype=float)

    # Drop consecutive duplicates that destabilise splprep.
    diffs = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    keep = np.concatenate([[True], diffs > 1e-9])
    pts = pts[keep]

    if closed:
        if np.linalg.norm(pts[0] - pts[-1]) > 1e-9:
            pts = np.vstack([pts, pts[0]])
        tck, _ = splprep([pts[:, 0], pts[:, 1]], s=smoothing, per=True, k=3)
    else:
        tck, _ = splprep([pts[:, 0], pts[:, 1]], s=smoothing, per=False, k=3)

    u_dense = np.linspace(0.0, 1.0, 4096)
    xs, ys = splev(u_dense, tck)
    seg = np.hypot(np.diff(xs), np.diff(ys))
    total_len = float(seg.sum())

    n_target = max(8, int(np.round(total_len / spacing)))
    u_uniform = np.linspace(0.0, 1.0, n_target + (0 if closed else 1))
    if closed:
        u_uniform = u_uniform[:-1]
    xs, ys = splev(u_uniform, tck)
    return np.column_stack([xs, ys])


def heading_and_normals(pts: np.ndarray, closed: bool) -> Tuple[np.ndarray, np.ndarray]:
    """Per-point tangent heading (rad) and left-hand unit normal.

    The normal points 90 deg counter-clockwise from the tangent — positive
    alpha shifts the raceline to the left (TUM convention).
    """
    if closed:
        prev = np.roll(pts, 1, axis=0)
        nxt = np.roll(pts, -1, axis=0)
    else:
        prev = np.vstack([pts[0], pts[:-1]])
        nxt = np.vstack([pts[1:], pts[-1]])

    tangent = nxt - prev
    psi = np.arctan2(tangent[:, 1], tangent[:, 0])
    nvec = np.column_stack([-np.sin(psi), np.cos(psi)])
    return psi, nvec


def discrete_curvature(pts: np.ndarray, closed: bool) -> np.ndarray:
    """Signed curvature at each point via three-point fit."""
    if closed:
        prev = np.roll(pts, 1, axis=0)
        nxt = np.roll(pts, -1, axis=0)
    else:
        prev = np.vstack([pts[0], pts[:-1]])
        nxt = np.vstack([pts[1:], pts[-1]])

    v1 = pts - prev
    v2 = nxt - pts
    l1 = np.linalg.norm(v1, axis=1)
    l2 = np.linalg.norm(v2, axis=1)
    cross = v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0]
    dot = v1[:, 0] * v2[:, 0] + v1[:, 1] * v2[:, 1]
    dtheta = np.arctan2(cross, dot)
    ds = 0.5 * (l1 + l2)
    return np.where(ds > 1e-9, dtheta / ds, 0.0)


def arc_length(pts: np.ndarray, closed: bool) -> np.ndarray:
    """Cumulative arc length per waypoint, starting at 0."""
    if closed:
        seg = np.linalg.norm(np.diff(np.vstack([pts, pts[0]]), axis=0), axis=1)
    else:
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    return s[: pts.shape[0]]
