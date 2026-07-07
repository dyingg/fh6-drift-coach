"""Sends fake Horizon-format telemetry so the overlay and recorder can be
tested end to end without launching the game.

    python scripts/send_fake_telemetry.py [--port 5300] [--duration 0]

Simulates a car holding ~100 km/h while swinging through drift transitions.
Packet layout matches what packet.py parses (324-byte FH4/FH5 Horizon
format), built from the same field specs so the two can't drift apart.
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forza_coach.telemetry import packet as P

_DEFAULTS = {"f": 0.0, "i": 0, "I": 0, "H": 0, "B": 0, "b": 0}


def _pack_into(buf: bytearray, offset: int, spec: list[tuple[str, str]],
               values: dict) -> None:
    fmt = "<" + "".join(code for _, code in spec)
    ordered = [values.get(name, _DEFAULTS[code]) for name, code in spec]
    struct.pack_into(fmt, buf, offset, *ordered)


def build_packet(t: float) -> bytes:
    speed = 28.0 + 4.0 * math.sin(t * 0.30)            # ~100-115 km/h
    slip = math.radians(30.0) * math.sin(t * 0.70)      # drift angle swings
    rpm = 4200.0 + 2600.0 * (0.5 + 0.5 * math.sin(t * 2.2))
    transition = abs(math.sin(t * 0.70)) > 0.92         # handbrake flicks

    sled = {
        "is_race_on": 1,
        "timestamp_ms": int(t * 1000) & 0xFFFFFFFF,
        "engine_max_rpm": 7500.0,
        "engine_idle_rpm": 900.0,
        "engine_rpm": rpm,
        "accel_x": 9.0 * math.sin(slip),
        "accel_z": 1.5,
        # local space: forward = Z, right = X -> body slip is atan2(vx, vz)
        "vel_x": speed * math.sin(slip),
        "vel_z": speed * math.cos(slip),
        "ang_vel_y": 0.8 * math.cos(t * 0.70),
        "yaw": t * 0.1,
        "tire_slip_angle_rl": 1.4 * math.sin(t * 0.70),
        "tire_slip_angle_rr": 1.4 * math.sin(t * 0.70),
        "car_ordinal": 2077,
        "car_class": 6,
        "car_pi": 815,
        "drivetrain": 1,  # RWD
        "num_cylinders": 6,
    }
    dash = {
        "pos_x": 100.0 * math.cos(t * 0.05),
        "pos_z": 100.0 * math.sin(t * 0.05),
        "speed_mps": speed,
        "power_w": 250_000.0,
        "torque_nm": 420.0,
        "tire_temp_fl": 78.0, "tire_temp_fr": 78.0,
        "tire_temp_rl": 96.0, "tire_temp_rr": 96.0,
        "fuel": 0.8,
        "distance_m": speed * t,
        "race_time_s": t,
        "throttle": 220,
        "brake": 0,
        "handbrake": 255 if transition else 0,
        "gear": 3,
        "steer": int(100 * math.sin(t * 0.70)),
    }

    buf = bytearray(P.HORIZON_SIZE)
    _pack_into(buf, 0, P.SLED_SPEC, sled)
    _pack_into(buf, P.HORIZON_DASH_OFFSET, P.DASH_SPEC, dash)
    return bytes(buf)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5300)
    parser.add_argument("--rate", type=float, default=60.0, help="packets per second")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="seconds to run (0 = until Ctrl+C)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / args.rate
    start = time.perf_counter()
    sent = 0
    print(f"Sending fake telemetry to {args.host}:{args.port} at {args.rate:.0f} Hz "
          f"(Ctrl+C to stop)")
    try:
        while True:
            t = time.perf_counter() - start
            if args.duration and t >= args.duration:
                break
            sock.sendto(build_packet(t), (args.host, args.port))
            sent += 1
            # schedule against the start time so the rate doesn't drift
            next_at = start + sent * interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    print(f"Sent {sent} packets in {time.perf_counter() - start:.1f}s")


if __name__ == "__main__":
    main()
