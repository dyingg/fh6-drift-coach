"""Parsing for the Forza "Data Out" UDP telemetry stream.

Forza titles broadcast one fixed-size little-endian packet per physics tick
(60 Hz). Known layouts:

    232 bytes  "Sled"     physics core (Forza Motorsport)
    311 bytes  "Dash"     Sled + dash block at offset 232 (FM7/FM8)
    324 bytes  "Horizon"  Sled + 12 mystery bytes + dash block at offset 244
                          (FH4/FH5 - FH6 is expected to match)

Unknown sizes are tolerated: anything >= 232 bytes gets the sled portion
parsed, and the recorder always keeps the raw bytes so new layouts can be
decoded after the fact.
"""

from __future__ import annotations

import math
import struct

WHEELS = ("fl", "fr", "rl", "rr")


def _per_wheel(name: str, code: str) -> list[tuple[str, str]]:
    return [(f"{name}_{w}", code) for w in WHEELS]


# (field name, struct format char) in wire order.
SLED_SPEC: list[tuple[str, str]] = (
    [
        ("is_race_on", "i"),
        ("timestamp_ms", "I"),
        ("engine_max_rpm", "f"),
        ("engine_idle_rpm", "f"),
        ("engine_rpm", "f"),
        ("accel_x", "f"),  # local space: X = right, Y = up, Z = forward
        ("accel_y", "f"),
        ("accel_z", "f"),
        ("vel_x", "f"),
        ("vel_y", "f"),
        ("vel_z", "f"),
        ("ang_vel_x", "f"),
        ("ang_vel_y", "f"),
        ("ang_vel_z", "f"),
        ("yaw", "f"),
        ("pitch", "f"),
        ("roll", "f"),
    ]
    + _per_wheel("susp_travel_norm", "f")
    + _per_wheel("tire_slip_ratio", "f")
    + _per_wheel("wheel_rotation_speed", "f")
    + _per_wheel("wheel_on_rumble_strip", "i")
    + _per_wheel("wheel_in_puddle", "f")
    + _per_wheel("surface_rumble", "f")
    + _per_wheel("tire_slip_angle", "f")
    + _per_wheel("tire_combined_slip", "f")
    + _per_wheel("susp_travel_m", "f")
    + [
        ("car_ordinal", "i"),
        ("car_class", "i"),
        ("car_pi", "i"),
        ("drivetrain", "i"),
        ("num_cylinders", "i"),
    ]
)

DASH_SPEC: list[tuple[str, str]] = (
    [
        ("pos_x", "f"),
        ("pos_y", "f"),
        ("pos_z", "f"),
        ("speed_mps", "f"),
        ("power_w", "f"),
        ("torque_nm", "f"),
    ]
    + _per_wheel("tire_temp", "f")
    + [
        ("boost", "f"),
        ("fuel", "f"),
        ("distance_m", "f"),
        ("best_lap_s", "f"),
        ("last_lap_s", "f"),
        ("current_lap_s", "f"),
        ("race_time_s", "f"),
        ("lap_number", "H"),
        ("race_position", "B"),
        ("throttle", "B"),
        ("brake", "B"),
        ("clutch", "B"),
        ("handbrake", "B"),
        ("gear", "B"),
        ("steer", "b"),
        ("driving_line", "b"),
        ("ai_brake_diff", "b"),
    ]
)


def _struct_for(spec: list[tuple[str, str]]) -> struct.Struct:
    return struct.Struct("<" + "".join(code for _, code in spec))

_SLED_STRUCT = _struct_for(SLED_SPEC)
_DASH_STRUCT = _struct_for(DASH_SPEC)

SLED_SIZE = _SLED_STRUCT.size          # 232
DASH_SIZE = SLED_SIZE + _DASH_STRUCT.size   # 311
HORIZON_SIZE = 324                     # FH4/FH5, dash block at offset 244
HORIZON_DASH_OFFSET = 244


def _unpack(spec: list[tuple[str, str]], packed: struct.Struct, data: bytes, offset: int) -> dict:
    return dict(zip((name for name, _ in spec), packed.unpack_from(data, offset)))


def parse_packet(data: bytes) -> dict:
    """Parse one UDP datagram into a flat dict, tolerating unknown layouts."""
    n = len(data)
    if n < SLED_SIZE:
        return {"format": f"unknown-{n}B"}

    out = _unpack(SLED_SPEC, _SLED_STRUCT, data, 0)

    if n == SLED_SIZE:
        out["format"] = "sled"
    elif n == DASH_SIZE:
        out.update(_unpack(DASH_SPEC, _DASH_STRUCT, data, SLED_SIZE))
        out["format"] = "dash"
    elif n >= HORIZON_SIZE:
        out.update(_unpack(DASH_SPEC, _DASH_STRUCT, data, HORIZON_DASH_OFFSET))
        out["format"] = "horizon" if n == HORIZON_SIZE else f"horizon-{n}B"
    else:
        out["format"] = f"unknown-{n}B"

    _add_derived(out)
    return out


def _add_derived(out: dict) -> None:
    """Convenience fields for the overlay and later coaching logic."""
    vx, vy, vz = out["vel_x"], out["vel_y"], out["vel_z"]
    speed = out.get("speed_mps")
    if speed is None:
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        out["speed_mps"] = speed
    out["speed_kmh"] = speed * 3.6

    # Body slip: angle between where the car points (+Z) and where it moves.
    # Velocity is in local space, so this is the drift angle.
    if speed > 4.0:
        out["drift_angle_deg"] = math.degrees(math.atan2(vx, vz))
    else:
        out["drift_angle_deg"] = 0.0
