"""Core regression tests on the bundled demo map."""
import os

import numpy as np
import pytest

from raceline_studio.core.centerline import fast_centerline
from raceline_studio.core.io_utils import (
    compute_velocity, load_centerline_file, load_csv, load_map,
)
from raceline_studio.core.optimize import lap_time, run_optimization
from raceline_studio.core.vehicle_params import VehicleParams

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_YAML = os.path.join(ROOT, "maps", "icra2026_map", "map.yaml")
RACELINE = os.path.join(ROOT, "maps", "icra2026_map",
                        "icra2026_map_raceline.csv")


@pytest.fixture(scope="module")
def demo_map():
    raw, res, origin, W, H, _ = load_map(MAP_YAML)
    return raw, res, origin


def _length(pts, closed=True):
    pts = np.asarray(pts, dtype=float)
    loop = np.vstack([pts, pts[:1]]) if closed else pts
    return float(np.linalg.norm(np.diff(loop, axis=0), axis=1).sum())


def test_centerline_no_hint_finds_a_loop(demo_map):
    # The map's free space extends beyond the course, so without a hint or
    # region only "some closed loop" is guaranteed — not full coverage.
    raw, res, origin = demo_map
    cl = fast_centerline(raw, res, origin, closed=True)
    assert _length(cl) > 30.0, "centerline collapsed"


def test_centerline_full_track_with_region(demo_map):
    # The shipped configuration: centerline hint + a region around the
    # raceline must recover the full course and stay inside the region.
    raw, res, origin = demo_map
    hint = load_centerline_file(MAP_YAML, True)
    rl = np.asarray(load_csv(RACELINE), dtype=float)
    lo, hi = rl.min(axis=0) - 1.0, rl.max(axis=0) + 1.0
    region = [[lo[0], lo[1]], [hi[0], lo[1]],
              [hi[0], hi[1]], [lo[0], hi[1]]]
    cl = np.asarray(fast_centerline(raw, res, origin, closed=True,
                                    hint_xy=hint, region_xy=region))
    assert _length(cl) > 0.85 * _length(rl)
    assert (np.ptp(cl, axis=0) > 0.9 * np.ptp(rl, axis=0)).all()
    assert (cl >= lo - 0.2).all() and (cl <= hi + 0.2).all()


def test_centerline_rejects_empty_region(demo_map):
    raw, res, origin = demo_map
    with pytest.raises(RuntimeError):
        fast_centerline(raw, res, origin, closed=True,
                        region_xy=[[0, 0], [0.05, 0], [0.05, 0.05]])


def test_velocity_profile_bounds():
    pts = load_csv(RACELINE)
    veh = VehicleParams()
    v = compute_velocity(pts, True, veh)
    v = np.asarray(v)
    assert len(v) == len(pts)
    assert (v > 0).all() and (v <= veh.v_max + 1e-6).all()
    assert lap_time(np.asarray(pts), v, True) > 1.0


def test_optimizer_respects_vehicle_geometry(demo_map):
    # A wider car with a larger safety margin must end up farther from the
    # walls than a narrow one — geometry is a user-facing setting now.
    import dataclasses

    from scipy.ndimage import distance_transform_edt

    raw, res, origin = demo_map
    hint = load_centerline_file(MAP_YAML, True)
    rl = np.asarray(load_csv(RACELINE), dtype=float)
    lo, hi = rl.min(axis=0) - 1.0, rl.max(axis=0) + 1.0
    region = [[lo[0], lo[1]], [hi[0], lo[1]],
              [hi[0], hi[1]], [lo[0], hi[1]]]
    cl = fast_centerline(raw, res, origin, closed=True,
                         hint_xy=hint, region_xy=region).tolist()
    clearance_m = distance_transform_edt(raw == 0) * res

    def min_clearance(veh):
        result = run_optimization("mincurv", raw, res, origin, True, cl,
                                  veh, veh, region_xy=region)
        pts = np.asarray(result["pts"])
        cols = np.clip(np.round((pts[:, 0] - origin[0]) / res).astype(int),
                       0, raw.shape[1] - 1)
        rows = np.clip(np.round((pts[:, 1] - origin[1]) / res).astype(int),
                       0, raw.shape[0] - 1)
        return float(clearance_m[rows, cols].min())

    narrow = dataclasses.replace(VehicleParams(), width=0.20,
                                 safety_margin=0.0)
    wide = dataclasses.replace(VehicleParams(), width=0.45,
                               safety_margin=0.20)
    c_narrow, c_wide = min_clearance(narrow), min_clearance(wide)
    assert c_wide > c_narrow + 0.1, (c_narrow, c_wide)


def test_optimize_mincurv(demo_map):
    raw, res, origin = demo_map
    cl = fast_centerline(raw, res, origin, closed=True)
    veh = VehicleParams()
    result = run_optimization("mincurv", raw, res, origin, True,
                              cl.tolist(), veh, veh)
    assert result["method"] == "mincurv"
    assert len(result["pts"]) == len(result["v"])
    assert result["lap_time"] > 1.0
    # optimized line stays a closed loop of plausible length
    assert 0.7 * _length(cl) < _length(result["pts"]) < 1.3 * _length(cl)
