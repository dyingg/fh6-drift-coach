"""Streaming drift-event detection.

The detector is fed one CoachSample at a time (works identically on a live
stream and on a recording) and emits a completed DriftEvent when a drift
ends. Hysteresis on the slip angle avoids chattering at the threshold.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .conventions import CoachSample

ENTER_BETA = 12.0     # deg - |beta| to open an event
EXIT_BETA = 8.0       # deg - |beta| below this counts toward closing
EXIT_HOLD_S = 0.4     # must stay under EXIT_BETA this long to close
MIN_SPEED_KMH = 25.0  # ignore parking-lot noise
END_SPEED_KMH = 15.0  # drift is over if the car is this slow
SPIN_BETA = 75.0      # past this the car is spinning, not drifting
SPUN_BETA = 130.0     # fully spun through - close immediately
MIN_DURATION_S = 0.7  # shorter blips are discarded
MIN_PEAK_BETA = 15.0  # events that never reach this are discarded
MAX_GAP_S = 0.5       # telemetry gap - close whatever was open
PREROLL_S = 2.0       # entry context kept from before the drift threshold
COOLDOWN_GRIP_S = 0.5 # grip needed after a spin before re-arming


@dataclass
class DriftEvent:
    samples: list[CoachSample] = field(default_factory=list)
    pre: list[CoachSample] = field(default_factory=list)  # entry context
    spun: bool = False

    @property
    def t0(self) -> float:
        return self.samples[0].t

    @property
    def t1(self) -> float:
        return self.samples[-1].t

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    @property
    def peak_beta(self) -> float:
        return max(abs(s.beta_deg) for s in self.samples)


class DriftDetector:
    def __init__(self):
        self._event: DriftEvent | None = None
        self._grip_since: float | None = None
        self._preroll: deque[CoachSample] = deque(maxlen=240)
        self._cooldown = False          # set after a spin closes
        self._cool_grip_since: float | None = None

    @property
    def active(self) -> bool:
        return self._event is not None

    def feed(self, s: CoachSample) -> DriftEvent | None:
        """Advance the state machine; returns a DriftEvent when one closes."""
        if self._event is None:
            self._preroll.append(s)
            # After a spin the car slides backwards through the entry
            # threshold repeatedly - stay disarmed until grip is truly back.
            if self._cooldown:
                if abs(s.beta_deg) < EXIT_BETA and s.speed_kmh > 5:
                    if self._cool_grip_since is None:
                        self._cool_grip_since = s.t
                    elif s.t - self._cool_grip_since >= COOLDOWN_GRIP_S:
                        self._cooldown = False
                else:
                    self._cool_grip_since = None
                return None
            if (
                abs(s.beta_deg) >= ENTER_BETA
                and s.speed_kmh >= MIN_SPEED_KMH
                and s.gear != 0  # not reversing
                and abs(s.beta_deg) < SPUN_BETA
            ):
                pre = [p for p in self._preroll if s.t - p.t <= PREROLL_S
                       and p.t < s.t]
                self._event = DriftEvent(samples=[s], pre=pre)
                self._grip_since = None
            return None

        ev = self._event
        if s.t - ev.samples[-1].t > MAX_GAP_S:
            return self._close(trailing_grip=False)

        ev.samples.append(s)
        if abs(s.beta_deg) >= SPIN_BETA:
            ev.spun = True

        if abs(s.beta_deg) >= SPUN_BETA or s.speed_kmh < END_SPEED_KMH:
            return self._close(trailing_grip=False)

        if abs(s.beta_deg) < EXIT_BETA:
            if self._grip_since is None:
                self._grip_since = s.t
            elif s.t - self._grip_since >= EXIT_HOLD_S:
                return self._close(trailing_grip=True)
        else:
            self._grip_since = None
        return None

    def flush(self) -> DriftEvent | None:
        """Close any open event (end of recording / stream)."""
        return self._close(trailing_grip=False) if self._event else None

    def _close(self, trailing_grip: bool) -> DriftEvent | None:
        ev, self._event = self._event, None
        grip_since, self._grip_since = self._grip_since, None
        if trailing_grip and grip_since is not None:
            ev.samples = [s for s in ev.samples if s.t <= grip_since] or ev.samples
        if ev.spun:
            self._cooldown = True
            self._cool_grip_since = None
        if ev.duration < MIN_DURATION_S or ev.peak_beta < MIN_PEAK_BETA:
            return None  # a twitch, not a drift
        return ev
