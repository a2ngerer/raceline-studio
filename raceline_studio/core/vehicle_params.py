"""F1Tenth physical parameters and grip limits.

Defaults follow the platform spec used by the F1TENTH gym simulator:
mass 3.74 kg, wheelbase 0.31 m, max steering 24 deg, drag coeff 0.075.
All three acceleration limits default to mu = 0.45 (a = mu*g = 4.41 m/s^2),
the friction coefficient measured on the real track, for a grip-consistent
friction circle. The motor may deliver less forward accel in practice, but
never more than grip allows. Raise them together (e.g. 8.0 for mu ~ 0.82)
only for a verified high-grip surface.

All values are SI. Override via ROS parameters at runtime.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class VehicleParams:
    # Geometry
    wheelbase: float = 0.31           # [m] front-rear axle distance
    width: float = 0.296              # [m] vehicle width (incl. tires)
    length: float = 0.568             # [m] bumper-to-bumper

    # Mass / inertia
    mass: float = 3.74                # [kg]
    yaw_inertia: float = 0.128        # [kg*m^2]

    # Limits
    v_max: float = 14.0               # [m/s] capped well below sim 15 m/s
    v_min: float = 1.0                # [m/s] floor used in velocity profile
    a_long_max: float = 4.41          # [m/s^2] traction-limited fwd accel (mu*g, mu=0.45)
    a_brake_max: float = 4.41         # [m/s^2] braking decel, grip-limited (mu*g, mu=0.45)
    a_lat_max: float = 4.41           # [m/s^2] mu*g with mu = 0.45 (measured track grip)
    drag_coeff: float = 0.075         # [N*s^2/m^2] longitudinal drag
    max_steering: float = 0.4189      # [rad] ~24 deg

    # Safety margin baked into raceline width bound
    safety_margin: float = 0.10       # [m] extra clearance to wall on each side


def default_params() -> VehicleParams:
    return VehicleParams()
