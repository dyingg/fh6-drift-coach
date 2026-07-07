"""Sends fake Horizon-format telemetry so the overlay, live coach and
recorder can be tested end to end without launching the game.

    python scripts/send_fake_telemetry.py [--port 5300] [--duration 0]

Plays a 26-second scripted loop that exercises the coach:
    0-2 s    straight driving
    2-10 s   clean right-hand drift, well balanced        -> HELD verdict
    10-11 s  full throttle lift mid-drift, car snaps      -> SNAP verdict
    11-13 s  straight
    13-20 s  left drift, throttle pinned, angle runs away -> cues + SPIN
    20-26 s  spun out, recover

Signals follow the validated FH6 conventions (see coach/conventions.py):
velocity local, counter-steer same sign as beta, rear slip sign opposite
to beta, fronts near zero when steering is correct.
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
LOOP_S = 26.0


def _pack_into(buf: bytearray, offset: int, spec: list[tuple[str, str]],
               values: dict) -> None:
    fmt = "<" + "".join(code for _, code in spec)
    ordered = [values.get(name, _DEFAULTS[code]) for name, code in spec]
    struct.pack_into(fmt, buf, offset, *ordered)


def _sign(x: float) -> float:
    return 1.0 if x >= 0 else -1.0


def scenario(t: float) -> dict:
    """Returns beta (deg), speed (m/s), steer (-1..1), throttle, front/rear
    normalized slip for time t in the scripted loop."""
    t = t % LOOP_S
    wob = math.sin(t * 2.2)

    if t < 2.0:  # straight
        return dict(beta=0.0, speed=18 + 4 * t, steer=0.0, throttle=0.9,
                    front=0.1, rear=0.3)
    if t < 10.0:  # clean right drift (beta < 0), correct counter-steer
        ramp = min(1.0, (t - 2.0) / 1.0)
        beta = -ramp * (27 + 4 * wob)
        return dict(beta=beta, speed=24, steer=-0.75 - 0.10 * wob,
                    throttle=0.62 + 0.08 * math.sin(t * 2.2 + 1.2),
                    front=0.25 * wob, rear=-_sign(beta) * 1.4)
    if t < 11.0:  # driver lifts -> rear regrips -> snap through zero
        k = (t - 10.0)
        beta = -25 * max(0.0, 1 - k / 0.45) + (14 * math.exp(-((k - 0.7) / 0.2) ** 2)
                                               if k > 0.45 else 0.0)
        return dict(beta=beta, speed=23, steer=-0.3 * max(0.0, 1 - k),
                    throttle=0.03, front=0.4, rear=-_sign(beta) * 0.8)
    if t < 13.5:  # straight
        return dict(beta=0.0, speed=20, steer=0.0, throttle=0.5,
                    front=0.1, rear=0.2)
    if t < 20.0:  # left drift, throttle pinned, not enough counter -> spin
        beta = min(18 + (t - 13.5) * 11, 95.0)
        return dict(beta=beta, speed=22 - (t - 13.5) * 0.8,
                    steer=0.45, throttle=1.0,
                    front=-_sign(beta) * 1.3, rear=-_sign(beta) * 2.4)
    if t < 22.0:  # spun out, sliding to a stop
        return dict(beta=150.0, speed=max(6 - (t - 20) * 3, 1.5), steer=0.0,
                    throttle=0.0, front=0.5, rear=0.5)
    return dict(beta=0.0, speed=5 + (t - 22) * 4, steer=0.0, throttle=0.6,
                front=0.1, rear=0.2)  # recover


def build_packet(t: float) -> bytes:
    s = scenario(t)
    beta_rad = math.radians(s["beta"])
    speed = s["speed"]
    rpm = 3800.0 + 2400.0 * s["throttle"]

    sled = {
        "is_race_on": 1,
        "timestamp_ms": int(t * 1000) & 0xFFFFFFFF,
        "engine_max_rpm": 7500.0,
        "engine_idle_rpm": 900.0,
        "engine_rpm": rpm,
        "accel_x": 9.0 * math.sin(beta_rad),
        "accel_z": 1.5,
        # local space: forward = Z, right = X -> beta = atan2(vx, vz)
        "vel_x": speed * math.sin(beta_rad),
        "vel_z": speed * math.cos(beta_rad),
        "ang_vel_y": 0.9 * math.sin(t * 0.9),
        "yaw": t * 0.1,
        "tire_slip_angle_fl": s["front"],
        "tire_slip_angle_fr": s["front"],
        "tire_slip_angle_rl": s["rear"],
        "tire_slip_angle_rr": s["rear"],
        "tire_slip_ratio_rl": s["rear"] * 0.8,
        "tire_slip_ratio_rr": s["rear"] * 0.8,
        "tire_combined_slip_fl": abs(s["front"]),
        "tire_combined_slip_fr": abs(s["front"]),
        "tire_combined_slip_rl": abs(s["rear"]) + 0.2,
        "tire_combined_slip_rr": abs(s["rear"]) + 0.2,
        "car_ordinal": 2077,
        "car_class": 5,
        "car_pi": 828,
        "drivetrain": 1,  # RWD
        "num_cylinders": 6,
    }
    dash = {
        "pos_x": 100.0 * math.cos(t * 0.05),
        "pos_z": 100.0 * math.sin(t * 0.05),
        "speed_mps": speed,
        "power_w": 250_000.0 * s["throttle"],
        "torque_nm": 420.0,
        "tire_temp_fl": 78.0, "tire_temp_fr": 78.0,
        "tire_temp_rl": 96.0, "tire_temp_rr": 96.0,
        "fuel": 0.8,
        "distance_m": speed * t,
        "race_time_s": t,
        "throttle": int(s["throttle"] * 255),
        "brake": 0,
        "handbrake": 0,
        "gear": 3,
        "steer": int(s["steer"] * 127),
    }

    buf = bytearray(P.HORIZON_SIZE)
    _pack_into(buf, 0, P.SLED_SPEC, sled)
    _pack_into(buf, P.HORIZON_DASH_OFFSET, P.DASH_SPEC, dash)
    return bytes(buf)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5300)
    parser.add_argument("--rate", type=float, default=80.0, help="packets per second")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="seconds to run (0 = until Ctrl+C)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / args.rate
    start = time.perf_counter()
    sent = 0
    print(f"Sending scripted drift telemetry to {args.host}:{args.port} at "
          f"{args.rate:.0f} Hz (Ctrl+C to stop)")
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
