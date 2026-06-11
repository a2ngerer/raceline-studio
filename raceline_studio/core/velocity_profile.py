"""Forward / backward velocity profile under friction-circle constraints.

Algorithm (after TUM `calc_vel_profile`):

  1. Initial cornering-limited profile:  v_init = sqrt(a_lat_max / |kappa|)
     clipped at v_max.
  2. Forward pass: bound the velocity from above by what the previous
     step can reach under longitudinal traction available *after*
     paying for lateral acceleration on the friction circle:
       a_long_avail = a_long_max * sqrt(max(0, 1 - (a_lat / a_lat_max)^2))
       v[i+1] = min(v_init[i+1], sqrt(v[i]^2 + 2 a_long_avail ds))
  3. Backward pass: same, but bound from above by the *next* step's
     ability to decelerate (`a_brake_max`).
  4. Drag subtraction: a_drag(v) = c_d v^2 / m. Reduces available
     longitudinal acceleration in step 2 and adds to deceleration in
     step 3.

The result is a feasible velocity profile that respects tire grip
(friction circle), motor / braking limits, and drag, while the path
itself is fixed (we do not re-plan geometry to gain time).
"""

from __future__ import annotations

import numpy as np

from .vehicle_params import VehicleParams


def velocity_profile(
    kappa: np.ndarray,
    ds: np.ndarray,
    params: VehicleParams,
    closed: bool,
    v_init: np.ndarray | None = None,
    respect_lateral: bool = True,
    grip_scale: np.ndarray | None = None,
) -> np.ndarray:
    """Compute a feasible per-waypoint velocity profile.

    Parameters
    ----------
    kappa : (N,) signed curvature per waypoint [1/m].
    ds : (N,) arc-length spacing between waypoints [m].
    params : VehicleParams with grip and motor limits.
    closed : closed loop (wrap forward/backward passes once).
    v_init : optional (N,) per-waypoint upper bound [m/s]. When given it
        replaces the friction-circle cornering cap as the starting ceiling
        for the forward/backward passes. Used to smooth hand-edited target
        speeds into a feasible profile (manual values act as local caps,
        the passes only enforce a_long/a_brake between them). ``None``
        (default) keeps the original behaviour: the cap is derived from
        ``sqrt(a_lat_max / |kappa|)``.
    respect_lateral : when ``True`` (default) the lateral grip limit shapes
        the profile in two ways: it sets the cornering speed ceiling
        (``sqrt(a_lat_max / |kappa|)``, only when ``v_init`` is ``None``) and
        it reserves friction-circle budget so less longitudinal acceleration
        is available while cornering. When ``False`` (the editor's
        "unrestricted" mode) both effects are dropped: no cornering ceiling
        and the FULL ``a_long_max`` / ``a_brake_max`` is available regardless
        of lateral demand. The forward/backward coupling between neighbouring
        points is unchanged — speeds still ramp at the car's longitudinal
        limit — but a corner may be driven above the nominal ``a_lat_max``
        (e.g. on a higher-grip surface). The hard ``v_max`` clip still applies.
    grip_scale : optional (N,) per-waypoint multiplier on the friction limits
        (``a_lat_max``, ``a_long_max``, ``a_brake_max``). ``None`` (default) or
        an all-ones array reproduces the single-grip behaviour exactly. Values
        > 1 model locally higher-grip surfaces — the editor uses this for
        hand-drawn "carpet" zones where a stronger µ applies, so the car may
        corner, accelerate and brake harder inside those zones.

    Returns
    -------
    v : (N,) velocity per waypoint [m/s], clipped to [v_min, v_max].
    """
    k = np.maximum(np.abs(kappa), 1e-6)
    n = k.shape[0]
    if grip_scale is None:
        gs = np.ones(n, dtype=float)
    else:
        gs = np.broadcast_to(np.asarray(grip_scale, dtype=float), (n,)).astype(float)
    # Per-waypoint friction limits. With gs == 1 these collapse to the scalar
    # params, so the all-ones path is bit-for-bit the original computation.
    a_lat = params.a_lat_max * gs
    a_long_max = params.a_long_max * gs
    a_brake_max = params.a_brake_max * gs

    if v_init is None:
        if respect_lateral:
            v = np.minimum(params.v_max, np.sqrt(a_lat / k))
        else:
            v = np.full(k.shape, params.v_max, dtype=float)
    else:
        v = np.minimum(params.v_max, np.asarray(v_init, dtype=float))

    def passes(v: np.ndarray, forward: bool) -> np.ndarray:
        v = v.copy()
        rng = range(1, n) if forward else range(n - 2, -1, -1)
        a_cap = a_long_max if forward else a_brake_max
        wraps = 2 if closed else 1
        for _ in range(wraps):
            for i in rng:
                j = (i - 1) % n if forward else (i + 1) % n
                if respect_lateral:
                    a_lat_j = v[j] ** 2 * abs(kappa[j])
                    budget = max(0.0, 1.0 - (a_lat_j / a_lat[j]) ** 2)
                    a_long = a_cap[j] * np.sqrt(budget)
                else:
                    a_long = a_cap[j]
                drag = params.drag_coeff * v[j] ** 2 / params.mass
                a_long = a_long - drag if forward else a_long + drag
                a_long = max(a_long, 0.0)
                step = ds[j] if forward else ds[i]
                v_reach = np.sqrt(v[j] ** 2 + 2.0 * a_long * step)
                v[i] = min(v[i], v_reach)
        return v

    v = passes(v, forward=True)
    v = passes(v, forward=False)
    return np.clip(v, params.v_min, params.v_max)
