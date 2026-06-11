"""Fast centerline extraction tuned for interactive latency.

Pipeline: flood the corridor (seeded from the previous centerline when
available, else the deepest free pixel), optionally 2x down-scale large grids
with a conservative min-pool, skeletonize, prune spurs (vectorised), keep the
main loop, walk and spline-resample.

Pure compute — no threads, no HTTP. ``cancel`` is any object with an
``is_set()`` method (a ``threading.Event`` on the desktop server, ``None`` in
the browser build).
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import binary_erosion, convolve, distance_transform_edt, label
from skimage.draw import polygon2mask
from skimage.morphology import skeletonize
from skimage.segmentation import flood_fill

from .map_processing import pixels_to_world, world_to_pixels
from .reference_line import smooth_and_resample


class JobCancelled(Exception):
    pass


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled


def _prune_spurs_fast(sk: np.ndarray, max_iter: int | None = 24,
                      cancel=None) -> np.ndarray:
    """Vectorised endpoint pruning.

    ``max_iter=None`` prunes until no endpoint remains. On a closed track
    that is the correct stop condition: loop pixels always keep two
    neighbours, so only spurs are consumed — a fixed iteration cap leaves
    long spurs behind and the loop walk dead-ends inside them.
    """
    K = np.ones((3, 3), dtype=np.uint8)
    K[1, 1] = 0
    sk = sk.copy()
    limit = max_iter if max_iter is not None else max(64, sk.size)
    for _ in range(limit):
        _check_cancel(cancel)
        nb = convolve(sk.astype(np.uint8), K, mode="constant")
        ends = sk & (nb <= 1)
        if not ends.any():
            break
        sk[ends] = False
    return sk


def _largest_component(mask: np.ndarray) -> np.ndarray:
    lab, n = label(mask, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return lab == int(np.argmax(sizes))


def _walk_loop(skel: np.ndarray, seed_rc) -> np.ndarray:
    """Order skeleton pixels into one path by nearest-neighbour walk."""
    sk_rc = np.argwhere(skel)
    d2 = (sk_rc[:, 0] - seed_rc[0]) ** 2 + (sk_rc[:, 1] - seed_rc[1]) ** 2
    cur = tuple(int(v) for v in sk_rc[int(np.argmin(d2))])
    h, w = skel.shape
    visited = np.zeros_like(skel)
    order = [cur]
    visited[cur] = True
    # 4-neighbours first, then diagonals, so the walk hugs the skeleton.
    steps4 = ((-1, 0), (1, 0), (0, -1), (0, 1))
    steps8 = ((-1, -1), (-1, 1), (1, -1), (1, 1))
    while True:
        nxt = None
        r, c = cur
        for dr, dc in steps4:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and skel[nr, nc] \
                    and not visited[nr, nc]:
                nxt = (nr, nc)
                break
        if nxt is None:
            for dr, dc in steps8:
                nr, nc = r + dr, c + dc
                if (0 <= nr < h and 0 <= nc < w and skel[nr, nc]
                        and not visited[nr, nc]):
                    nxt = (nr, nc)
                    break
        if nxt is None:
            break
        cur = nxt
        visited[cur] = True
        order.append(cur)
    return np.asarray(order, dtype=int)


def _best_walk(skel: np.ndarray, seed_rc) -> np.ndarray:
    """Walk the skeleton; if the seed walk covers too little (it started on
    a residual branch or a secondary cycle), retry from spread-out pixels
    and keep the longest path."""
    total = int(skel.sum())
    order = _walk_loop(skel, seed_rc)
    if order.shape[0] >= 0.7 * total:
        return order
    sk_rc = np.argwhere(skel)
    best = order
    for i in np.linspace(0, len(sk_rc) - 1, num=min(6, len(sk_rc)),
                         dtype=int):
        alt = _walk_loop(skel, tuple(int(v) for v in sk_rc[i]))
        if alt.shape[0] > best.shape[0]:
            best = alt
        if best.shape[0] >= 0.9 * total:
            break
    return best


def _order_by_hint(skel, hint_xy, origin, res, band_m=1.0):
    """Order the skeleton along an existing centerline hint.

    For every hint point take the nearest skeleton pixel within ``band_m``.
    The result inherits the hint's loop ordering, so it cannot dead-end in
    a residual branch or close the loop with a chord across the map — the
    failure modes of the greedy pixel walk on maps whose free space is
    larger than the course. Returns None when the hint barely overlaps the
    skeleton (e.g. the map was repainted far away from it)."""
    from scipy.spatial import cKDTree

    sk_rc = np.argwhere(skel)
    if sk_rc.shape[0] < 8:
        return None
    sk_xy = pixels_to_world(sk_rc, origin, res)
    dist, idx = cKDTree(sk_xy).query(np.asarray(hint_xy, dtype=float))
    keep = dist <= band_m
    if keep.sum() < max(8, 0.5 * len(hint_xy)):
        return None
    sel = idx[keep]
    # consecutive hint points often snap to the same pixel — deduplicate
    step = np.ones(sel.shape[0], dtype=bool)
    step[1:] = sel[1:] != sel[:-1]
    return sk_xy[sel[step]]


def region_mask_from_polygon(region_xy, shape, origin, res):
    """Rasterize a world-coordinate polygon to a pixel mask (None = no-op)."""
    if not region_xy or len(region_xy) < 3:
        return None
    rc = world_to_pixels(np.asarray(region_xy, dtype=float), origin, res)
    return polygon2mask(shape, rc.astype(float))


def fast_centerline(raw: np.ndarray, res: float, origin, closed: bool,
                    hint_xy=None, region_xy=None, inflate_px: int = 0,
                    cancel=None, progress=None) -> np.ndarray:
    """Centerline from the occupancy grid.

    ``region_xy`` (world-coordinate polygon) restricts the computation to
    the drawn area — free space outside it is treated as wall.

    ``inflate_px`` erodes the free space by the vehicle's half width +
    safety margin (in pixels). Gaps narrower than the vehicle — e.g. the
    space between cones in a dotted track divider — stop counting as
    drivable, so the corridor cannot leak through them.
    """
    def tick(p, msg):
        _check_cancel(cancel)
        if progress is not None:
            progress(p, msg)

    tick(0.05, "binarize")
    free = raw == 0
    rmask = region_mask_from_polygon(region_xy, free.shape, origin, res)
    if rmask is not None:
        free = free & rmask
        if not free.any():
            raise RuntimeError("no free space inside the region polygon")
    if inflate_px > 0:
        inflated = binary_erosion(free, iterations=int(inflate_px))
        if inflated.any():
            free = inflated  # too-narrow maps: fall back rather than fail
    if not free.any():
        raise RuntimeError("map has no free space")

    # Seed the corridor flood.
    seed = None
    if hint_xy is not None and len(hint_xy):
        rc = world_to_pixels(np.asarray(hint_xy, dtype=float), origin, res)
        h, w = free.shape
        for r, c in rc:
            if 0 <= r < h and 0 <= c < w and free[r, c]:
                seed = (int(r), int(c))
                break
    if seed is None:
        dt = distance_transform_edt(free)
        seed = tuple(int(v)
                     for v in np.unravel_index(int(np.argmax(dt)), dt.shape))

    tick(0.15, "flood corridor")
    corridor = flood_fill(free.astype(np.uint8), seed, 2) == 2

    # Conservative 2x down-scale for big grids: a coarse cell is free only if
    # all four fine cells are, so the skeleton never crosses a wall.
    scale = 2 if corridor.size > 1_200_000 else 1
    region = corridor
    if scale == 2:
        h2, w2 = (corridor.shape[0] // 2) * 2, (corridor.shape[1] // 2) * 2
        c = corridor[:h2, :w2]
        region = c[0::2, 0::2] & c[1::2, 0::2] & c[0::2, 1::2] & c[1::2, 1::2]

    erode_px = max(1, int(round(3 / scale)))
    tick(0.3, "erode + skeletonize")
    eroded = binary_erosion(region,
                            structure=np.ones((2 * erode_px + 1,) * 2))
    if not eroded.any():
        eroded = region  # ultra-thin track: skip erosion rather than fail
    skel = skeletonize(eroded)
    tick(0.6, "prune spurs")
    # Closed track: prune to convergence so only the loop survives. Open
    # track: a capped prune — full pruning would eat the path from its ends.
    skel = _prune_spurs_fast(skel, max_iter=None if closed else 24,
                             cancel=cancel)
    skel = _largest_component(skel)
    if not skel.any():
        raise RuntimeError("skeleton empty — track too thin or map degenerate")

    tick(0.8, "order loop")
    xy = None
    if hint_xy is not None and len(hint_xy) >= 8:
        xy = _order_by_hint(skel, hint_xy, origin, res * scale)
    if xy is None:
        # No usable hint: greedy walk over the largest skeleton cycle.
        skel = _largest_component(skel)
        seed_s = (seed[0] // scale, seed[1] // scale)
        rc = _best_walk(skel, seed_s)
        if rc.shape[0] < 8:
            raise RuntimeError("centerline too short — check the map walls")
        xy = pixels_to_world(rc, origin, res * scale)
    tick(0.92, "resample")
    return smooth_and_resample(xy, closed=closed, spacing=0.10, smoothing=0.5)
