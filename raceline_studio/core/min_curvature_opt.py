"""Minimum-curvature QP optimization for global racelines.

Based on Heilmeier et al. (2020), "Minimum curvature trajectory planning
and control for an autonomous race car" and the TUM Roborace open-source
implementation. Discrete formulation:

    pts(alpha) = ref + diag(alpha) @ nvec
    x'' = M @ x   (central second-difference, periodic for closed loops)

Plugging the parameterisation into x'' / y'':
    x''(alpha) = M @ ref_x + M @ (Nx * alpha) = b_x + Px @ alpha
    y''(alpha) = M @ ref_y + M @ (Ny * alpha) = b_y + Py @ alpha
    Nx = diag(nvec_x), Ny = diag(nvec_y),
    Px = M @ Nx,        Py = M @ Ny

Objective (proportional to summed squared curvature on a uniform grid):
    minimise   ||Px alpha + b_x||^2 + ||Py alpha + b_y||^2
    subject to alpha_min <= alpha <= alpha_max

We solve it as a bounded linear least-squares with
`scipy.optimize.lsq_linear` (TRF method). For iterative refinement we
shift the reference line by the previous solution and re-solve. This is
the IQP loop ("mincurv_iqp") which significantly reduces the
linearisation error in tight corners.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.optimize import lsq_linear

from .reference_line import heading_and_normals, smooth_and_resample


def _second_difference_matrix(n: int, closed: bool) -> np.ndarray:
    """Tridiagonal central second-difference matrix (periodic if closed)."""
    M = -2.0 * np.eye(n)
    if closed:
        idx = np.arange(n)
        M[idx, (idx - 1) % n] += 1.0
        M[idx, (idx + 1) % n] += 1.0
    else:
        for i in range(n):
            if i > 0:
                M[i, i - 1] = 1.0
            if i < n - 1:
                M[i, i + 1] = 1.0
    return M


def solve_min_curvature(
    ref: np.ndarray,
    nvec: np.ndarray,
    bounds: Tuple[np.ndarray, np.ndarray],
    closed: bool,
) -> np.ndarray:
    """One linearised QP solve for lateral shifts `alpha`.

    Parameters
    ----------
    ref : (N, 2) reference line points.
    nvec : (N, 2) unit normals at each ref point.
    bounds : (alpha_min, alpha_max) length-N arrays in metres.
    closed : whether the line forms a closed loop.

    Returns
    -------
    alpha : (N,) lateral shifts minimising summed squared curvature.
    """
    n = ref.shape[0]
    M = _second_difference_matrix(n, closed)
    Nx = nvec[:, 0]
    Ny = nvec[:, 1]
    Px = M * Nx[np.newaxis, :]
    Py = M * Ny[np.newaxis, :]
    A = np.vstack([Px, Py])
    b = np.concatenate([M @ ref[:, 0], M @ ref[:, 1]])

    a_lo, a_hi = bounds
    # Avoid degenerate bounds.
    a_hi = np.maximum(a_hi, a_lo + 1e-6)

    res = lsq_linear(
        A,
        -b,
        bounds=(a_lo, a_hi),
        method="trf",
        lsmr_tol="auto",
        max_iter=200,
    )
    return np.asarray(res.x, dtype=float)


def optimize_iqp(
    ref: np.ndarray,
    half_widths: np.ndarray,
    closed: bool,
    vehicle_width: float,
    safety_margin: float,
    iters: int = 6,
    spacing: float = 0.10,
    clearance_fn=None,
    bounds_fn=None,
) -> np.ndarray:
    """Iterative QP min-curvature optimization.

    On each iteration the reference is updated to `ref + alpha * n`
    and a fresh QP is solved against the *new* normals. Around six
    iterations are needed for the corner apexes to converge once the
    lateral bound reflects the true corridor (see ``bounds_fn``); the old
    isotropic bound stalled the apex regardless of iteration count.

    Lateral-shift bound (``alpha``), in priority order:

    * ``bounds_fn`` — callable ``(line, normals) -> (left, right)`` giving
      the free distance to the corridor edge *along the normal* on each
      side. This is the correct, asymmetric bound: it uses the full width
      across the racing direction instead of the nearest wall in any
      direction, so the optimizer can round a hairpin apex whose inside
      tip sits a few cm away laterally. Preferred.
    * ``clearance_fn`` — callable ``line -> clearance`` (isotropic distance
      transform). Symmetric and conservative; under-uses wide corridors at
      tight corners. Retained for backward compatibility.
    * neither — static seed from ``half_widths``, resampled each pass.

    ``alpha`` is an *incremental* shift from the current line, so the bound
    is the remaining room from *there* and must be re-evaluated on the
    shifted line every pass; otherwise shifts accumulate across passes and
    drive the line through the walls.

    Returns
    -------
    optimized_pts : (M, 2) optimized raceline, spline-resampled.
    """
    pts = np.asarray(ref, dtype=float)
    widths = np.asarray(half_widths, dtype=float)
    margin = max(0.0, 0.5 * vehicle_width + safety_margin)
    # Floor of 1 mm keeps the QP feasible in pinch points without letting the
    # line reach the wall (margin >> floor, so true barriers are never hit).
    bound = np.maximum(widths - margin, 1e-3)

    for _ in range(max(1, iters)):
        _, nvec = heading_and_normals(pts, closed)
        if bounds_fn is not None:
            left, right = bounds_fn(pts, nvec)
            # Full margin from BOTH walls. These are deliberately NOT clamped to
            # >= 0: if the line already sits inside the margin (e.g. the apex
            # hugging a hairpin's inside wall), the bound goes negative and
            # *pushes the line back out* to restore clearance, rather than only
            # forbidding it from creeping closer. Where the corridor is narrower
            # than twice the margin the two bounds cross — centre the line there.
            a_hi = left - margin
            a_lo = -(right - margin)
            cross = a_lo > a_hi
            mid = 0.5 * (a_lo + a_hi)
            a_hi = np.where(cross, mid, a_hi)
            a_lo = np.where(cross, mid, a_lo)
        else:
            a_hi = bound
            a_lo = -bound
        alpha = solve_min_curvature(pts, nvec, (a_lo, a_hi), closed)
        pts = pts + alpha[:, np.newaxis] * nvec

        new_pts = smooth_and_resample(pts, closed=closed, spacing=spacing, smoothing=0.1)
        if bounds_fn is None:
            if clearance_fn is not None:
                # Re-measure the actual wall clearance at the shifted line;
                # the next pass shifts from here, so this is its real room.
                bound = np.maximum(clearance_fn(new_pts) - margin, 1e-3)
            else:
                bound = _resample_bound(pts, bound, new_pts, closed=closed)
        pts = new_pts

    return pts


def _resample_bound(
    old_pts: np.ndarray,
    old_bound: np.ndarray,
    new_pts: np.ndarray,
    closed: bool,
) -> np.ndarray:
    """Interpolate the per-point bound onto a new sample grid by
    cumulative arc length. Crude but sufficient because widths vary
    slowly relative to typical resampling spacing.
    """
    if closed:
        old_loop = np.vstack([old_pts, old_pts[0]])
        old_b = np.concatenate([old_bound, old_bound[:1]])
    else:
        old_loop = old_pts
        old_b = old_bound
    seg = np.linalg.norm(np.diff(old_loop, axis=0), axis=1)
    s_old = np.concatenate([[0.0], np.cumsum(seg)])
    total = s_old[-1] if s_old[-1] > 0 else 1.0

    new_loop = np.vstack([new_pts, new_pts[0]]) if closed else new_pts
    new_seg = np.linalg.norm(np.diff(new_loop, axis=0), axis=1)
    s_new = np.concatenate([[0.0], np.cumsum(new_seg)])
    if closed:
        s_new = s_new[:-1]
    if s_new[-1] > 0:
        s_new = s_new / s_new[-1] * total
    return np.interp(s_new, s_old, old_b)
