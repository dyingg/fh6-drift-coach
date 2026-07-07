"""Live coaching state machine - fed from the telemetry thread, read by the UI.

Two outputs, matching how you can actually absorb information mid-drift:
  - cue: ONE short instruction about right now ("MORE COUNTER-STEER +8°"),
    rate-limited so it doesn't flicker
  - verdict: what went wrong/right with the LAST drift, posted the moment
    the event closes (the closed loop)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .calibration import DEFAULT_BAND, DEFAULT_BETA_MAX, Calibration
from .conventions import PEAK_SLIP_DEG, CoachSample, enrich
from .events import DriftDetector
from .metrics import STEER_SAT, EventReport, analyze

CUE_HOLD_S = 0.5       # minimum time a cue stays up
CS_EMA_TAU = 0.15      # smoothing for the counter-steer error signal
CS_CUE_ON = 0.9        # normalized slip error that triggers a steering cue

# Default sustain-phase throttle target for the HUD bar; superseded per car
# and per angle once calibration has learned from the driver's own drifts.
THROTTLE_BAND = DEFAULT_BAND

PIN_CUE_S = 1.0        # throttle pinned this long with angle growing -> cue
PREDICT_S = 0.7        # look-ahead horizon for the spin warning
GROWTH_TAU = 0.20      # smoothing for d|beta|/dt

MODES = ("free", "roundabout", "corner", "s-bend")

# How hard to pan the counter-steer cue toward the steering direction: strong
# enough to point the ear, short of hard-panning so the word stays intelligible.
COUNTER_PAN = 0.8


def _verdict_key(report: EventReport) -> str | None:
    """Audio key for a closed event, from its (outcome, cause). Returns None
    when there's no clip for the pairing (so playback is skipped, not guessed)."""
    outcome, cause = report.outcome, report.cause
    if outcome == "held":
        return "held"
    if outcome == "faded":
        return "faded"
    if outcome == "snap":
        return {"lift": "snap_lifted", "over-correction": "snap_over",
                "regrip": "snap_commit"}.get(cause)
    if outcome == "spin":
        return {"pinned": "spin_throttle",
                "throttle": "spin_throttle",       # root-cause: too much power
                "beyond-lock": "spin_counter",     # counter was late, not weak
                "no-counter": "spin_counter",
                "late-counter": "spin_counter"}.get(cause)
    return None


@dataclass
class LiveView:
    in_drift: bool
    beta_deg: float
    throttle: float     # 0..1, current pedal
    thr_target: tuple[float, float]  # sustainable band at the current angle
    cue: str            # "" when nothing to say
    cue_level: str      # "ok" | "warn" | "danger"
    verdict: str        # last event headline, "" until first drift ends
    verdict_level: str
    did: str            # what the driver did (last event)
    fix: str            # what to do instead (last event)
    last_report: EventReport | None
    events: int
    mode: str


class LiveCoach:
    def __init__(self, mode: str = "free",
                 calibration: Calibration | None = None):
        self._lock = threading.Lock()
        self._detector = DriftDetector()
        self._calibration = calibration
        self._mode = mode
        self._beta = 0.0
        self._throttle = 0.0
        self._thr_target = DEFAULT_BAND
        self._growth = 0.0              # d|beta|/dt, smoothed
        self._pinned_since: float | None = None
        self._cs_ema = 0.0
        self._last_t: float | None = None
        self._cue = ""
        self._cue_level = "ok"
        self._cue_since = 0.0
        self._verdict = ""
        self._verdict_level = "ok"
        self._last_report: EventReport | None = None
        self._events = 0

        # Audio hooks, wired externally (like listener.on_packet). Fired from
        # feed() AFTER the lock is released - a callback that reached back into
        # coach state while _advance held the lock would deadlock.
        self.on_cue: Callable[[str | None, str, float], None] | None = None
        self.on_verdict: Callable[[str], None] | None = None

    # -- called from the telemetry thread -------------------------------------

    def feed(self, parsed: dict) -> None:
        s = enrich(parsed)
        if s is None:
            return
        with self._lock:
            cue_note, verdict_note, done = self._advance(s)
        if cue_note is not None and self.on_cue is not None:
            self.on_cue(*cue_note)
        if verdict_note is not None and self.on_verdict is not None:
            self.on_verdict(verdict_note)
        if done is not None and self._calibration is not None:
            self._calibration.observe_event(
                s.car_ordinal, done.samples, done.spun)

    def _advance(self, s: CoachSample):
        """Update state and return (cue_note, verdict_note, done_event) for
        feed() to act on outside the lock. cue_note is (audio_key, level, pan)
        on a cue transition, verdict_note the audio key of a just-closed event."""
        dt = 0.0 if self._last_t is None else max(0.0, s.t - self._last_t)
        prev_beta = self._beta
        self._last_t = s.t
        self._beta = s.beta_deg
        self._throttle = s.throttle
        self._thr_target = self._band(s)

        alpha = min(1.0, dt / CS_EMA_TAU) if dt else 1.0
        self._cs_ema += alpha * (s.cs_error - self._cs_ema)
        if dt > 0:
            inst = (abs(s.beta_deg) - abs(prev_beta)) / dt
            inst = max(-300.0, min(300.0, inst))
            g_alpha = min(1.0, dt / GROWTH_TAU)
            self._growth += g_alpha * (inst - self._growth)

        verdict_note = None
        done = self._detector.feed(s)
        if done is not None:
            report = analyze(done, self._mode, self._calibration)
            self._events += 1
            self._last_report = report
            self._verdict = report.verdict
            self._verdict_level = (
                "danger" if report.outcome in ("snap", "spin")
                else "warn" if report.outcome == "faded" else "ok"
            )
            verdict_note = _verdict_key(report)

        cue_note = None
        cue, level, key = self._pick_cue(s)
        if cue != self._cue and s.t - self._cue_since >= CUE_HOLD_S:
            self._cue, self._cue_level, self._cue_since = cue, level, s.t
            cue_note = (key, level, self._cue_pan(s, key))
        return cue_note, verdict_note, done

    def _band(self, s: CoachSample) -> tuple[float, float]:
        """Sustainable throttle band at the current angle (calibrated when
        the car has enough history, default otherwise)."""
        beta_ref = abs(s.beta_deg) if abs(s.beta_deg) >= 12 else 20.0
        if self._calibration is not None:
            return self._calibration.throttle_band(s.car_ordinal, beta_ref)
        return DEFAULT_BAND

    def _cue_pan(self, s: CoachSample, key: str | None) -> float:
        """Pan only the counter cue toward the steering direction. beta > 0 is
        a left-hand drift (counter-steer left) -> pan LEFT (negative); beta < 0
        pans RIGHT. Other cues stay centered."""
        if key != "counter":
            return 0.0
        return -COUNTER_PAN if s.beta_deg > 0 else COUNTER_PAN

    def _pick_cue(self, s: CoachSample) -> tuple[str, str, str | None]:
        """History-aware cue priority. Warnings fire on the TREND (projected
        angle, time spent pinned), not only the instantaneous state - by the
        time the old thresholds tripped, the spin was usually unavoidable."""
        if not self._detector.active:
            self._pinned_since = None
            return "", "ok", None

        beta = abs(s.beta_deg)
        band = self._thr_target
        beta_max = (self._calibration.beta_max(s.car_ordinal)
                    if self._calibration is not None else DEFAULT_BETA_MAX)
        cs_deg = self._cs_ema * PEAK_SLIP_DEG

        if s.throttle >= 0.90 and beta > 15 and self._growth > -5:
            if self._pinned_since is None:
                self._pinned_since = s.t
        else:
            self._pinned_since = None

        # predictive: where will the angle be in PREDICT_S at this trend?
        # Only past 30° - entry flicks legitimately build angle fast, and
        # warning during a normal initiation would train the driver to
        # ignore the danger cue.
        projected = beta + max(0.0, self._growth) * PREDICT_S
        if projected >= beta_max and beta >= 30:
            return "BACK OFF — SPINNING OUT", "danger", "back_off"
        # steering channel exhausted: never ask for counter that isn't there
        if abs(s.steer) >= STEER_SAT and self._cs_ema > 0.6:
            return "FULL LOCK — EASE THROTTLE", "danger", "ease_off"
        if (self._pinned_since is not None
                and s.t - self._pinned_since >= PIN_CUE_S):
            return "EASE THROTTLE — TOO MUCH", "warn", "ease_off"
        if self._cs_ema > CS_CUE_ON:
            return f"MORE COUNTER-STEER  +{cs_deg:.0f}°", "warn", "counter"
        if self._cs_ema < -CS_CUE_ON:
            return f"UNWIND STEERING  {cs_deg:.0f}°", "warn", "unwind"
        if (s.throttle < max(0.20, band[0] - 0.15) and 15 < beta < 45
                and self._growth < 0 and not s.handbrake):
            return "MORE THROTTLE — ANGLE DYING", "warn", "throttle"
        return "BALANCED — HOLD IT", "ok", None

    # -- called from the UI thread ---------------------------------------------

    def view(self) -> LiveView:
        with self._lock:
            report = self._last_report
            return LiveView(
                in_drift=self._detector.active,
                beta_deg=self._beta,
                throttle=self._throttle,
                thr_target=self._thr_target,
                cue=self._cue,
                cue_level=self._cue_level,
                verdict=self._verdict,
                verdict_level=self._verdict_level,
                did=report.did if report else "",
                fix=report.fix if report else "",
                last_report=report,
                events=self._events,
                mode=self._mode,
            )

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode
