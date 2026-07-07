"""Scoring and fault diagnosis for a completed drift event.

Produces an EventReport with a short verdict (fits the HUD), a fault list
with concrete numbers, and a full multi-line report card for the CLI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .conventions import PEAK_SLIP_DEG, CoachSample
from .events import DriftEvent

SUSTAIN_BETA = 12.0    # deg - samples past this count as "in the drift"
SCRUB_LIMIT = 1.0      # normalized front slip beyond this = scrubbing
LIFT_DROP = 0.5        # throttle drop (0..1) within LIFT_WINDOW = a lift
LIFT_WINDOW_S = 0.4
SNAP_COLLAPSE_S = 0.6  # peak -> grip faster than this = abrupt regrip


@dataclass
class EventReport:
    t0: float
    duration: float
    direction: str          # "left" / "right" / "linked x2" ...
    outcome: str            # held | snap | spin | faded
    cause: str              # lift | over-correction | regrip | pinned | late-counter | ""
    avg_speed: float
    peak_beta: float
    mean_beta: float        # mean |beta| while sustaining
    std_beta: float
    transitions: int
    cs_err_deg: float       # signed mean counter-steer error, deg (+ = add more)
    scrub_pct: float        # fraction of sustain with fronts saturated
    steer_lag_ms: float | None
    thr_mean: float
    thr_std: float
    thr_full_lifts: int
    thr_pct_full: float
    thr_pct_zero: float
    score: int
    verdict: str            # headline for the HUD
    did: str                # what the driver actually did, with numbers
    fix: str                # what to do instead
    faults: list[str] = field(default_factory=list)

    def card(self, index: int) -> str:
        lines = [
            f"Drift #{index} - {self.direction} - {self.duration:.1f}s, "
            f"avg {self.avg_speed:.0f} km/h",
            f"  angle          {self.mean_beta:.0f}° held, "
            f"±{self.std_beta:.0f}° swing, peak {self.peak_beta:.0f}°",
            f"  counter-steer  err {self.cs_err_deg:+.1f}° avg, "
            f"fronts scrubbing {self.scrub_pct * 100:.0f}% of the time"
            + (f", reaction ~{self.steer_lag_ms:.0f} ms"
               if self.steer_lag_ms is not None else ""),
            f"  throttle       {self.thr_mean * 100:.0f}% avg, "
            f"{self.thr_full_lifts} full lifts, "
            f"{self.thr_pct_full * 100:.0f}% pinned / "
            f"{self.thr_pct_zero * 100:.0f}% off",
            f"  outcome        {self.verdict}",
            f"  you            {self.did}",
            f"  score          {self.score}/100",
        ]
        if self.faults:
            lines.append("  fix")
            lines += [f"    - {f}" for f in self.faults]
        return "\n".join(lines)


def _sign(x: float) -> float:
    return 1.0 if x >= 0 else -1.0


def _transitions(samples: list[CoachSample]) -> int:
    """Direction switches: beta zero-crossings flanked by real angle."""
    count, last_peak_sign = 0, 0.0
    for s in samples:
        if abs(s.beta_deg) >= SUSTAIN_BETA:
            sgn = _sign(s.beta_deg)
            if last_peak_sign and sgn != last_peak_sign:
                count += 1
            last_peak_sign = sgn
    return count


def _steer_lag_ms(samples: list[CoachSample]) -> float | None:
    """Cross-correlate steering rate against slip rate.

    Returns the lag (ms) at which the driver's steering best tracks the
    car's rotation. Experts are near 0; late catchers are 250 ms+.
    """
    if len(samples) < 40:
        return None
    dt = (samples[-1].t - samples[0].t) / (len(samples) - 1)
    dbeta = [b.beta_deg - a.beta_deg for a, b in zip(samples, samples[1:])]
    dsteer = [b.steer - a.steer for a, b in zip(samples, samples[1:])]
    if _rms(dbeta) < 1e-3 or _rms(dsteer) < 1e-4:
        return None
    best_lag, best_corr = None, 0.0
    max_shift = min(int(0.6 / dt), len(dbeta) - 10)
    for k in range(0, max_shift):
        c = _corr(dbeta[: len(dbeta) - k], dsteer[k:])
        if abs(c) > abs(best_corr):
            best_corr, best_lag = c, k
    if best_lag is None or abs(best_corr) < 0.30:
        return None
    return best_lag * dt * 1000


def _rms(xs) -> float:
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def _corr(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _lift_before(samples: list[CoachSample], t_end: float) -> CoachSample | None:
    """Find a throttle dump shortly before t_end."""
    window = [s for s in samples if t_end - 1.2 <= s.t <= t_end]
    for i, a in enumerate(window):
        for b in window[i + 1:]:
            if b.t - a.t > LIFT_WINDOW_S:
                break
            if a.throttle - b.throttle >= LIFT_DROP:
                return a
    return None


def analyze(ev: DriftEvent, mode: str = "free") -> EventReport:
    samples = ev.samples
    sustain = [s for s in samples if abs(s.beta_deg) >= SUSTAIN_BETA]
    core = sustain or samples

    betas = [abs(s.beta_deg) for s in core]
    mean_beta = sum(betas) / len(betas)
    std_beta = math.sqrt(sum((b - mean_beta) ** 2 for b in betas) / len(betas))
    transitions = _transitions(samples)
    if transitions:
        direction = f"linked x{transitions + 1}"
    else:
        direction = "left" if sum(s.beta_deg for s in core) >= 0 else "right"

    cs_err = sum(s.cs_error for s in core) / len(core)
    scrub = sum(1 for s in core if abs(s.front_slip) > SCRUB_LIMIT) / len(core)
    lag = _steer_lag_ms(samples)

    thr = [s.throttle for s in core]
    thr_mean = sum(thr) / len(thr)
    pct_full = sum(1 for x in thr if x > 0.95) / len(thr)
    pct_zero = sum(1 for x in thr if x < 0.05) / len(thr)
    lifts = 0
    for a, b in zip(core, core[1:]):
        if a.throttle - b.throttle >= LIFT_DROP and b.t - a.t <= LIFT_WINDOW_S:
            lifts += 1

    thr_std = math.sqrt(sum((x - thr_mean) ** 2 for x in thr) / len(thr))

    outcome, cause, verdict, info = _classify(ev, samples, cs_err, thr_mean)
    did, fix = _hud_detail(outcome, cause, info, cs_err * PEAK_SLIP_DEG,
                           scrub, lag, std_beta, thr_mean, thr_std,
                           ev.duration)
    faults = _faults(outcome, cause, cs_err, scrub, lag, std_beta,
                     lifts, pct_full, pct_zero, mean_beta, thr_mean)
    score = _score(outcome, std_beta, scrub, lag, lifts, ev.duration)

    return EventReport(
        t0=ev.t0, duration=ev.duration, direction=direction,
        outcome=outcome, cause=cause,
        avg_speed=sum(s.speed_kmh for s in core) / len(core),
        peak_beta=ev.peak_beta, mean_beta=mean_beta, std_beta=std_beta,
        transitions=transitions, cs_err_deg=cs_err * PEAK_SLIP_DEG,
        scrub_pct=scrub, steer_lag_ms=lag,
        thr_mean=thr_mean, thr_std=thr_std, thr_full_lifts=lifts,
        thr_pct_full=pct_full, thr_pct_zero=pct_zero,
        score=score, verdict=verdict, did=did, fix=fix, faults=faults,
    )


def _classify(ev: DriftEvent, samples, cs_err, thr_mean):
    """Outcome + root cause + short HUD verdict + numbers for the detail."""
    last = samples[-1]

    if ev.spun:
        # quote the drift's own peak, not the post-spin ±150° readings
        drift_betas = [abs(s.beta_deg) for s in samples if abs(s.beta_deg) < 110]
        peak = max(drift_betas) if drift_betas else ev.peak_beta
        recent = [s for s in samples if s.t >= last.t - 1.0]
        thr_recent = sum(s.throttle for s in recent) / len(recent)
        info = {"peak": peak, "thr": thr_recent}
        if thr_recent > 0.85:
            return ("spin", "pinned",
                    f"SPIN - throttle pinned past {peak:.0f}°", info)
        return ("spin", "late-counter",
                f"SPIN at {peak:.0f}° - counter earlier", info)

    # how fast did the angle collapse after the LAST significant peak?
    # (a long clean drift can still end in a snap - measure the ending)
    hi = [s for s in samples if abs(s.beta_deg) >= 18]
    if hi:
        peak_s = hi[-1]
        after = [s for s in samples if s.t > peak_s.t and abs(s.beta_deg) < 10]
        collapse_time = after[0].t - peak_s.t if after else None
        if collapse_time is not None and collapse_time <= SNAP_COLLAPSE_S:
            near = [s for s in samples
                    if peak_s.t - 0.5 <= s.t <= peak_s.t + SNAP_COLLAPSE_S]
            lift = _lift_before(samples, peak_s.t + SNAP_COLLAPSE_S)
            if lift is not None:
                info = {
                    "from": lift.throttle,
                    "to": min(s.throttle for s in near),
                    "beta": abs(lift.beta_deg),
                }
                return ("snap", "lift",
                        f"SNAP - you lifted at {info['beta']:.0f}°", info)
            tail = [s for s in samples if s.t >= peak_s.t]
            over = [s for s in tail if s.cs_error < -1.0]
            if len(over) >= max(2, len(tail) // 4):
                return ("snap", "over-correction",
                        "SNAP - over-corrected, unwind less", {})
            info = {"peak": abs(peak_s.beta_deg),
                    "thr": sum(s.throttle for s in near) / len(near)}
            return ("snap", "regrip",
                    f"SNAP at {info['peak']:.0f}° - commit: more throttle", info)

    if ev.duration < 2.0 and thr_mean < 0.35:
        return ("faded", "lift", "FADED - stay on the throttle",
                {"thr": thr_mean})

    mark = " ✓" if ev.duration >= 3.0 else ""
    mean = sum(abs(s.beta_deg) for s in samples) / len(samples)
    return ("held", "", f"HELD {ev.duration:.1f}s @ {mean:.0f}°{mark}", {})


def _hud_detail(outcome, cause, info, cs_err_deg, scrub, lag,
                std_beta, thr_mean, thr_std, duration) -> tuple[str, str]:
    """(what you did, what to do) - short lines with numbers for the HUD."""
    if outcome == "spin" and cause == "pinned":
        return (f"kept {info['thr'] * 100:.0f}% throttle past {info['peak']:.0f}°",
                "breathe the pedal as the angle grows")
    if outcome == "spin":
        if cs_err_deg > 15 or scrub > 0.9:
            did = f"fronts dragged {scrub * 100:.0f}% - no real counter"
        elif lag is not None and lag > 250:
            did = f"counter came ~{lag:.0f} ms late"
        else:
            did = f"rotation outran the counter ({info['peak']:.0f}°)"
        return (did, "counter the instant rotation starts")
    if cause == "lift":
        if outcome == "snap":
            return (f"cut throttle {info['from'] * 100:.0f}%→"
                    f"{info['to'] * 100:.0f}% at {info['beta']:.0f}°",
                    "trim 10-15% instead - never dump it")
        return (f"throttle faded to {info['thr'] * 100:.0f}% avg",
                "stay on it - the angle needs power")
    if cause == "over-correction":
        return (f"steered ~{abs(cs_err_deg):.0f}° past the slide",
                "smaller catch - unwind as rotation peaks")
    if cause == "regrip":
        return (f"only {info['thr'] * 100:.0f}% throttle at {info['peak']:.0f}°",
                "commit - more angle, hold 55-70%")
    # held
    did = (f"±{std_beta:.0f}° swing · throttle {thr_mean * 100:.0f}%"
           f"±{thr_std * 100:.0f}")
    if cs_err_deg > 2.5:
        fix = f"add ~{cs_err_deg:.0f}° counter to kill the wobble"
    elif std_beta > 9:
        fix = "smooth it - two small catches, not one big"
    elif duration < 3.0:
        fix = "good shape - now hold it longer"
    else:
        fix = "clean - push for more angle"
    return did, fix


def _faults(outcome, cause, cs_err, scrub, lag, std_beta,
            lifts, pct_full, pct_zero, mean_beta, thr_mean) -> list[str]:
    faults = []
    cs_deg = cs_err * PEAK_SLIP_DEG
    if cs_deg > 15:
        faults.append(
            "Counter-steer was essentially absent - the fronts never caught "
            f"the slide ({scrub * 100:.0f}% saturated). Steer INTO the "
            "direction the car is travelling the moment it rotates."
        )
    elif cs_deg > 2.5:
        faults.append(
            f"Add ~{cs_deg:.0f}° more counter-steer - fronts were dragged "
            f"({scrub * 100:.0f}% of the drift scrubbing)."
        )
    elif cs_deg < -2.5:
        faults.append(
            f"Unwind ~{-cs_deg:.0f}° - you counter-steer past the "
            "velocity vector, which straightens then snaps the car."
        )
    if lag is not None and lag > 250:
        faults.append(
            f"Counter-steer is late by ~{lag:.0f} ms - begin unwinding as "
            "rotation peaks, not after the nose is past your line."
        )
    if std_beta > 9:
        faults.append(
            f"Angle swung ±{std_beta:.0f}° - halve your correction size; "
            "two small catches beat one big one."
        )
    if cause == "lift" and outcome in ("snap", "faded"):
        faults.append(
            "Never dump the throttle mid-drift - trim angle with small "
            "10-15% pedal changes and let the steering breathe."
        )
    elif lifts >= 3 or (pct_full > 0.4 and pct_zero > 0.2):
        faults.append(
            f"Throttle is 0↔100% pumping ({lifts} full lifts) - hold a "
            "55-70% base and make ±10% adjustments."
        )
    if cause == "pinned":
        faults.append(
            f"You held {pct_full * 100:.0f}% full throttle - past ~40° the rear "
            "can't recover; breathe the pedal the moment rotation keeps growing."
        )
    if outcome == "held" and mean_beta < 18 and thr_mean < 0.45:
        faults.append(
            "Shallow and tentative - commit to more entry angle and more "
            "throttle; 25-35° is easier to hold than 15°."
        )
    return faults


def _score(outcome, std_beta, scrub, lag, lifts, duration) -> int:
    score = 100
    score -= {"spin": 50, "snap": 30, "faded": 15}.get(outcome, 0)
    score -= min(20, max(0.0, std_beta - 6) * 2)
    score -= scrub * 20
    if lag is not None and lag > 250:
        score -= 15
    score -= min(15, lifts * 5)
    score += min(10, duration)  # reward long drifts
    return max(5, min(100, round(score)))
