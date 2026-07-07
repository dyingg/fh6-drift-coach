"""FH6 telemetry conventions, validated against real captures (2026-07-07).

Findings from live game data (car ordinal 3434, S1 828, RWD):

- Packets are the 324-byte Horizon layout at a VARIABLE 60-80 Hz (appears
  tied to frame rate) - always integrate with packet timestamps.
- velocity is LOCAL space (X right, Y up, Z forward). Position-derivative
  test: world = [vx*cos(yaw)+vz*sin(yaw), vz*cos(yaw)-vx*sin(yaw)].
- body slip  beta = atan2(vel_x, vel_z).
  beta > 0: nose left of travel (left-hand drift); beta < 0: right-hand.
- yaw rate = ang_vel_y  (corr +0.93 with d(yaw)/dt).
- steer: -127 full left ... +127 full right. CORRECT counter-steer has the
  SAME sign as beta (front wheels pointed at the velocity vector).
- tire slip angles are normalized (|1| = grip limit) and the REAR axle's
  sign is OPPOSITE to beta while sliding. Fronts sit near 0 when
  counter-steer is correct:
      front sign == -sign(beta)  ->  under-counter-steered (dragged)
      front sign == +sign(beta)  ->  over-counter-steered
- slip ratios explode at near-zero speed (burnouts) - clamp below ~15 km/h.
- gear: 0 = reverse, 11 = neutral.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Approximate peak slip angle of a road tire; converts normalized slip
# into "degrees off". Good to ~+-2-3 deg; refine with per-car calibration.
PEAK_SLIP_DEG = 6.0

# Below this speed slip channels are meaningless (burnouts, standstill).
MIN_SLIP_SPEED_KMH = 15.0


@dataclass
class CoachSample:
    """One telemetry packet reduced to the channels coaching needs."""

    t: float
    speed_kmh: float
    beta_deg: float          # body slip angle, sign per module docstring
    yaw_rate: float          # rad/s, ang_vel_y
    steer: float             # -1 (full left) .. +1 (full right)
    throttle: float          # 0..1
    brake: float             # 0..1
    handbrake: bool
    gear: int
    front_slip: float        # signed avg normalized slip angle, front axle
    rear_slip: float         # signed avg normalized slip angle, rear axle
    rear_sat: float          # avg |combined slip| rear axle (0 = grip)
    car_ordinal: int         # which car - keys the per-car calibration

    @property
    def cs_error(self) -> float:
        """Counter-steer error in normalized slip units.

        Positive: under-counter-steered (add counter-steer).
        Negative: over-counter-steered (unwind).
        Near zero: fronts tracking - steering is correct.
        """
        if abs(self.beta_deg) < 5 or self.speed_kmh < MIN_SLIP_SPEED_KMH:
            return 0.0
        return -self.front_slip * _sign(self.beta_deg)

    @property
    def cs_error_deg(self) -> float:
        return self.cs_error * PEAK_SLIP_DEG


def _sign(x: float) -> float:
    return 1.0 if x >= 0 else -1.0


def enrich(p: dict) -> CoachSample | None:
    """Convert a parsed packet into a CoachSample; None if unusable."""
    if not p.get("is_race_on") or "steer" not in p:
        return None
    speed = p["speed_kmh"]
    slips_ok = speed >= MIN_SLIP_SPEED_KMH
    return CoachSample(
        t=p["t"] if "t" in p else 0.0,
        speed_kmh=speed,
        beta_deg=math.degrees(math.atan2(p["vel_x"], p["vel_z"])),
        yaw_rate=p["ang_vel_y"],
        steer=p["steer"] / 127.0,
        throttle=p["throttle"] / 255.0,
        brake=p["brake"] / 255.0,
        handbrake=p["handbrake"] > 0,
        gear=p["gear"],
        front_slip=(p["tire_slip_angle_fl"] + p["tire_slip_angle_fr"]) / 2
        if slips_ok else 0.0,
        rear_slip=(p["tire_slip_angle_rl"] + p["tire_slip_angle_rr"]) / 2
        if slips_ok else 0.0,
        rear_sat=(abs(p["tire_combined_slip_rl"]) + abs(p["tire_combined_slip_rr"])) / 2
        if slips_ok else 0.0,
        car_ordinal=p.get("car_ordinal", 0),
    )
