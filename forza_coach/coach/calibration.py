"""Per-car envelope calibration, learned from the driver's own telemetry.

Two limits drive root-cause analysis and both are learnable:

- beta_max: the largest slip angle this car has actually been recovered
  from. Past it, no steering input can arrest the rotation, so blame for a
  spin must lie earlier in the timeline.
- sustainable throttle by angle: whenever the angle holds steady for a
  moment (|d(beta)/dt| small), the throttle at that instant is, by
  definition, what sustains that angle in this car. Binned by angle, these
  equilibrium samples become the throttle target the coach quotes -
  numbers from the driver's own car, not a hard-coded band.

Stored in recordings/calibration.json keyed by car ordinal; conservative
defaults apply until enough samples accumulate.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULT_BETA_MAX = 48.0
DEFAULT_BAND = (0.55, 0.70)

ANGLE_BINS = ((12, 20), (20, 28), (28, 36), (36, 45))
MIN_BIN_SAMPLES = 40      # equilibrium samples before a bin overrides default
BAND_HALF_WIDTH = 0.07    # band drawn around the learned mean
EQ_DBETA_DEG_S = 8.0      # |d(beta)/dt| below this counts as equilibrium
MIN_RECOVERIES = 2        # recoveries before beta_max overrides default


def _bin_key(beta: float) -> str | None:
    b = abs(beta)
    for lo, hi in ANGLE_BINS:
        if lo <= b < hi:
            return f"{lo}-{hi}"
    return None


def _band_of(slot: dict) -> tuple[float, float]:
    mean = slot["sum"] / slot["n"]
    return (max(0.15, mean - BAND_HALF_WIDTH), min(1.0, mean + BAND_HALF_WIDTH))


class Calibration:
    """Thread-safe (observed from the telemetry thread, read from anywhere)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cars: dict[str, dict] = {}
        try:
            self._cars = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    # -- queries ---------------------------------------------------------------

    def beta_max(self, car: int) -> float:
        with self._lock:
            entry = self._cars.get(str(car), {})
            if entry.get("recoveries", 0) >= MIN_RECOVERIES:
                # a small margin past the best recovery seen so far
                return min(70.0, entry["beta_recovered"] + 3.0)
        return DEFAULT_BETA_MAX

    def throttle_band(self, car: int, beta: float) -> tuple[float, float]:
        """Sustainable throttle range for this car at this slip angle.

        When the exact bin hasn't accumulated enough samples, fall back to
        the closest LEARNED bin below this angle: sustainable throttle only
        drops as the angle grows, so a lower-angle band is a valid ceiling -
        far closer to the truth than the loose default. This matters most
        past 36°, exactly where spins develop and bins are thinnest.
        """
        key = _bin_key(beta)
        with self._lock:
            bins = self._cars.get(str(car), {}).get("throttle_bins", {})
            b = bins.get(key) if key else None
            if b and b["n"] >= MIN_BIN_SAMPLES:
                return _band_of(b)
            best = None
            for k, slot in bins.items():
                if slot["n"] < MIN_BIN_SAMPLES:
                    continue
                lo, hi = k.split("-")
                center = (float(lo) + float(hi)) / 2
                if center <= abs(beta) and (best is None or center > best[0]):
                    best = (center, slot)
            if best is not None:
                return _band_of(best[1])
        return DEFAULT_BAND

    # -- learning ----------------------------------------------------------------

    def observe_event(self, car: int, samples, spun: bool) -> None:
        """Feed one closed drift event: equilibrium samples update the
        throttle map; a recovery (no spin) can push beta_max up."""
        if not samples or car == 0:
            return
        with self._lock:
            entry = self._cars.setdefault(
                str(car), {"beta_recovered": 0.0, "recoveries": 0,
                           "throttle_bins": {}})

            if not spun:
                peak = max(abs(s.beta_deg) for s in samples)
                if peak > entry["beta_recovered"]:
                    entry["beta_recovered"] = round(peak, 1)
                entry["recoveries"] += 1

            bins = entry["throttle_bins"]
            for a, b in zip(samples, samples[1:]):
                dt = b.t - a.t
                if not 0.001 < dt < 0.1:
                    continue
                dbeta = abs(b.beta_deg - a.beta_deg) / dt
                if dbeta > EQ_DBETA_DEG_S or abs(a.beta_deg) < 12:
                    continue
                key = _bin_key(a.beta_deg)
                if key is None:
                    continue
                slot = bins.setdefault(key, {"n": 0, "sum": 0.0})
                slot["n"] += 1
                slot["sum"] += a.throttle
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = json.dumps(self._cars, indent=2)
            self.path.write_text(payload, encoding="utf-8")
        except OSError:
            pass  # calibration is an optimization, never fatal
