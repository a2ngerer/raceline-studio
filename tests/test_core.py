"""Core regression tests on the bundled demo map."""
import os

import numpy as np
import pytest

from raceline_studio.core.centerline import fast_centerline
from raceline_studio.core.io_utils import (
    compute_velocity, load_csv, load_map,
)
from raceline_studio.core.optimize import lap_time, run_optimization
from raceline_studio.core.vehicle_params import VehicleParams

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_YAML = os.path.join(ROOT, "maps", "demo", "map.yaml")
RACELINE = os.path.join(ROOT, "maps", "demo", "demo_raceline.csv")


@pytest.fixture(scope="module")
def demo_map():
    raw, res, origin, W, H, _ = load_map(MAP_YAML)
    return raw, res, origin


def _length(pts, closed=True):
    pts = np.asarray(pts, dtype=float)
    loop = np.vstack([pts, pts[:1]]) if closed else pts
    return float(np.linalg.norm(np.diff(loop, axis=0), axis=1).sum())


def test_centerline_spans_track(demo_map):
    raw, res, origin = demo_map
    cl = fast_centerline(raw, res, origin, closed=True)
    assert _length(cl) > 30.0, "centerline collapsed to a partial loop"
    # spans most of the free space, not a pocket
    free_rc = np.argwhere(raw == 0)
    span_free = np.ptp(free_rc, axis=0) * res
    span_cl = np.ptp(np.asarray(cl), axis=0)
    assert span_cl[0] > 0.7 * span_free[1]
    assert span_cl[1] > 0.6 * span_free[0]


def test_centerline_region_restricts(demo_map):
    raw, res, origin = demo_map
    # left half of the world bounding box
    H, W = raw.shape
    x0, y0 = origin
    x_mid = x0 + 0.45 * W * res
    region = [[x0, y0], [x_mid, y0],
              [x_mid, y0 + H * res], [x0, y0 + H * res]]
    cl = fast_centerline(raw, res, origin, closed=True, region_xy=region)
    assert np.asarray(cl)[:, 0].max() <= x_mid + 0.2


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
