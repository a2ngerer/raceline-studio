"""Occupancy-grid preprocessing and centerline extraction.

Pipeline:
  1. Binarize the OccupancyGrid (>=50 = wall) and flood-fill the
     driveable area starting from the car spawn.
  2. Skeletonize the driveable region (Lee-style topological skeleton
     via scikit-image) to obtain the medial axis.
  3. Order skeleton pixels into a single contiguous loop. Short spurs
     are pruned and the largest connected component is kept; the loop
     is walked nearest-neighbour starting at the pixel closest to the
     car spawn.
  4. Compute per-waypoint half-widths via the Euclidean distance
     transform of the driveable area.

Output: ordered centerline in pixel coords, plus a per-waypoint
half-width array. Conversion to world coordinates is a one-liner
applied by the orchestrating node.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.ndimage import distance_transform_edt, label
from skimage.morphology import skeletonize, binary_erosion
from skimage.segmentation import flood_fill


def binarize_grid(grid: np.ndarray, occupied_threshold: int = 50) -> np.ndarray:
    """Return a boolean wall mask (True = wall)."""
    return grid >= occupied_threshold


def safe_seed(walls: np.ndarray, rc: Tuple[int, int]) -> Tuple[int, int]:
    """Snap the seed cell to the nearest free pixel if it lands on a wall."""
    r, c = rc
    h, w = walls.shape
    r = max(0, min(h - 1, r))
    c = max(0, min(w - 1, c))
    if not walls[r, c]:
        return r, c
    free_yx = np.argwhere(~walls)
    if free_yx.size == 0:
        raise RuntimeError("Map has no free cells.")
    d2 = (free_yx[:, 0] - r) ** 2 + (free_yx[:, 1] - c) ** 2
    yr, xc = free_yx[int(np.argmin(d2))]
    return int(yr), int(xc)


def driveable_mask(walls: np.ndarray, seed_rc: Tuple[int, int]) -> np.ndarray:
    """Flood-fill the free region reachable from seed_rc.

    Returns a boolean mask with True = driveable.
    """
    flooded = flood_fill(walls.astype(np.uint8), seed_rc, 1)
    return np.logical_and(flooded == 1, ~walls)


def _neighbours(r: int, c: int, h: int, w: int):
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w:
                yield nr, nc


def _prune_spurs(skel: np.ndarray, max_spur_len: int = 16) -> np.ndarray:
    """Iteratively remove endpoint pixels of short spurs."""
    sk = skel.copy()
    for _ in range(max_spur_len):
        endpoints = []
        h, w = sk.shape
        for r, c in np.argwhere(sk):
            n = sum(int(sk[nr, nc]) for nr, nc in _neighbours(r, c, h, w))
            if n == 1:
                endpoints.append((r, c))
        if not endpoints:
            break
        for r, c in endpoints:
            sk[r, c] = False
    return sk


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """Return only the largest connected component (8-connectivity)."""
    lab, n = label(mask, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    keep = int(np.argmax(sizes))
    return lab == keep


def extract_centerline_pixels(
    drivable: np.ndarray,
    seed_rc: Tuple[int, int],
    erode_px: int = 0,
) -> np.ndarray:
    """Return ordered centerline pixels (Nx2 array, [row, col]).

    Steps: optional pre-erosion -> skeletonize -> prune spurs -> keep
    largest component -> nearest-neighbour walk from seed.
    """
    region = drivable
    if erode_px > 0:
        region = binary_erosion(
            region,
            footprint=np.ones((2 * erode_px + 1, 2 * erode_px + 1)),
        )

    skel = skeletonize(region)
    skel = _prune_spurs(skel, max_spur_len=16)
    skel = _largest_component(skel)

    if not skel.any():
        raise RuntimeError("Skeleton is empty — track too thin or map degenerate.")

    sk_rc = np.argwhere(skel)
    d2 = (sk_rc[:, 0] - seed_rc[0]) ** 2 + (sk_rc[:, 1] - seed_rc[1]) ** 2
    start = tuple(int(v) for v in sk_rc[int(np.argmin(d2))])

    h, w = skel.shape
    visited = np.zeros_like(skel)
    order = [start]
    visited[start] = True
    cur = start
    while True:
        best = None
        for nr, nc in _neighbours(*cur, h, w):
            if skel[nr, nc] and not visited[nr, nc]:
                # prefer 4-neighbours over diagonals
                w_step = 1 if (nr == cur[0] or nc == cur[1]) else 2
                if best is None or w_step < best[0]:
                    best = (w_step, (nr, nc))
        if best is None:
            break
        cur = best[1]
        visited[cur] = True
        order.append(cur)

    return np.asarray(order, dtype=int)


def clearance_field(drivable: np.ndarray, resolution: float) -> np.ndarray:
    """Euclidean distance (m) from every cell to the nearest non-drivable cell.

    This is the wall-clearance field the raceline optimizer queries to keep
    the line inside the track on every IQP pass (not just at the centerline).
    """
    return distance_transform_edt(drivable) * resolution


def sample_clearance(
    field: np.ndarray,
    xy: np.ndarray,
    origin_xy: Tuple[float, float],
    resolution: float,
) -> np.ndarray:
    """Nearest-cell clearance (m) at world (x, y) points.

    Points falling outside the grid return 0 clearance so the optimizer
    treats them as already at a wall and refuses to shift further there.
    """
    h, w = field.shape
    cols = np.round((xy[:, 0] - origin_xy[0]) / resolution).astype(int)
    rows = np.round((xy[:, 1] - origin_xy[1]) / resolution).astype(int)
    inb = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    out = np.zeros(xy.shape[0], dtype=float)
    out[inb] = field[rows[inb], cols[inb]]
    return out


def corridor_halfwidths(
    mask: np.ndarray,
    xy: np.ndarray,
    normals: np.ndarray,
    origin_xy: Tuple[float, float],
    resolution: float,
    max_dist: float = 4.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Free distance (m) to the corridor edge along +/- the unit normal.

    Returns ``(left, right)`` per-point: how far each point may move along
    ``+normal`` (left) and ``-normal`` (right) before leaving ``mask``.

    This is the *directional* room the raceline actually has. The isotropic
    distance transform (``clearance_field``) measures the nearest wall in
    ANY direction, so at a hairpin the inside tip a few cm away clamps the
    lateral-shift bound and the min-curvature optimizer cannot round the
    apex even though the corridor is metres wide across the racing
    direction. Casting along the normal recovers that width and keeps the
    bound asymmetric (left != right where the line runs off-centre).

    Marched at half-pixel steps; points already outside the mask return 0.
    """
    h, w = mask.shape
    ox, oy = origin_xy
    step = 0.5 * resolution
    n_steps = int(np.ceil(max_dist / step))
    xy = np.asarray(xy, dtype=float)

    def march(direction: np.ndarray) -> np.ndarray:
        room = np.full(xy.shape[0], max_dist, dtype=float)
        blocked = np.zeros(xy.shape[0], dtype=bool)
        for k in range(1, n_steps + 1):
            p = xy + direction * (k * step)
            cols = np.round((p[:, 0] - ox) / resolution).astype(int)
            rows = np.round((p[:, 1] - oy) / resolution).astype(int)
            inb = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
            free = np.zeros(xy.shape[0], dtype=bool)
            free[inb] = mask[rows[inb], cols[inb]]
            newly = (~free) & (~blocked)
            room[newly] = (k - 1) * step
            blocked |= ~free
            if blocked.all():
                break
        return room

    return march(normals), march(-normals)


def widths_along_path(
    drivable: np.ndarray,
    centerline_rc: np.ndarray,
    resolution: float,
) -> np.ndarray:
    """Half-width (distance to nearest wall) per centerline waypoint, in metres.

    Uses the Euclidean distance transform of the driveable mask. The
    QP optimizer treats left/right symmetrically; an asymmetric estimate
    via normal-direction ray-casting would be a strict superset and is
    deferred.
    """
    dt = clearance_field(drivable, resolution)
    return dt[centerline_rc[:, 0], centerline_rc[:, 1]]


def pixels_to_world(
    rc: np.ndarray,
    origin_xy: Tuple[float, float],
    resolution: float,
) -> np.ndarray:
    """Convert (row, col) pixel coords to (x, y) world coords in metres."""
    x = rc[:, 1] * resolution + origin_xy[0]
    y = rc[:, 0] * resolution + origin_xy[1]
    return np.column_stack([x, y])


def world_to_pixels(
    xy: np.ndarray,
    origin_xy: Tuple[float, float],
    resolution: float,
) -> np.ndarray:
    """Convert (x, y) world coords in metres to (row, col) pixel coords."""
    cols = np.round((xy[:, 0] - origin_xy[0]) / resolution).astype(int)
    rows = np.round((xy[:, 1] - origin_xy[1]) / resolution).astype(int)
    return np.column_stack([rows, cols])


def corridor_mask_from_centerline(
    free: np.ndarray,
    centerline_rc: np.ndarray,
) -> np.ndarray:
    """Flood-fill the driveable corridor starting from a centerline pixel.

    Used by the centerline-CSV path instead of seeding the flood at the
    car spawn (world origin). The seed is the first centerline pixel that
    lands on a free cell, so the flood is guaranteed to fill the racing
    corridor rather than an unrelated free region (e.g. the infield when
    the map origin sits inside the loop).

    Parameters
    ----------
    free : (H, W) bool mask, True = free (driveable) cell.
    centerline_rc : (N, 2) int array of (row, col) centerline pixels.

    Returns
    -------
    (H, W) bool mask of the connected corridor reachable from the seed.
    """
    h, w = free.shape
    seed = None
    for r, c in centerline_rc:
        if 0 <= r < h and 0 <= c < w and free[r, c]:
            seed = (int(r), int(c))
            break
    if seed is None:
        raise RuntimeError(
            "No centerline pixel lies on a free cell — check that the "
            "centerline CSV matches the map origin/resolution."
        )
    filled = flood_fill(free.astype(np.uint8), seed, 2)
    return filled == 2
