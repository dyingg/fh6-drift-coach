"""Scoring and fault diagnosis for a completed drift event.

Diagnosis is causal, not end-state: the event timeline (including ~2 s of
pre-drift entry context) is walked forward against the car's recoverability
envelope, and blame lands on the FIRST input that left it - everything the
driver did after that moment is symptom. In particular, once the slip angle
passes the recoverable maximum, or the steering is already at full lock,
"counter more/earlier" is no longer valid advice and the verdict must point
upstream (throttle, entry).

Produces an EventReport with a short verdict (fits the HUD), what-you-did /
what-to-do lines, the causal timeline, and a full report card for the CLI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .calibration import DEFAULT_BAND, DEFAULT_BETA_MAX, Calibration
from .conventions import PEAK_SLIP_DEG, CoachSample
from .events import DriftEvent

SUSTAIN_BETA = 12.0    # deg - samples past this count as "in the drift"
SCRUB_LIMIT = 1.0      # normalized front slip beyond this = scrubbing
LIFT_DROP = 0.5        # throttle drop (0..1) within LIFT_WINDOW = a lift
LIFT_WINDOW_S = 0.4
SNAP_COLLAPSE_S = 0.6  # peak -> grip faster than this = abrupt regrip

STEER_SAT = 0.95       # |steer| past this = the steering channel is spent
PIN_THR = 0.90         # throttle past this counts as pinned
DEBT_TRIGGER = 0.30    # integral of (throttle - band_top) dt that flags
                       # over-throttle, e.g. 30% over for 1 s
COUNTER_MIN = 0.30     # less correct-direction steer than this = no counter


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
    root_cause: str         # first envelope violation ("" = none found)
    ponr_s: float | None    # seconds into the event past which recovery
                            # was impossible (spins only)
    timeline: list[str] = field(default_factory=list)
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
        if self.timeline:
            lines.append("  timeline")
            lines += [f"    {t}" for t in self.timeline]
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


def _causal_walk(ev: DriftEvent, band_fn, beta_max: float):
    """Walk entry + event forward and find envelope violations in order.

    Returns (timeline, root, detail): timeline is printable "t+X.Xs  ..."
    lines; root is the code of the FIRST violation that happened before the
    point of no return (or "" if none); detail carries its numbers.
    """
    samples = ev.samples
    t0 = samples[0].t
    found: list[tuple[float, str, str, dict]] = []

    # entry: was the throttle already pinned coming into the drift?
    entry_win = [s for s in ev.pre if t0 - s.t <= 0.4] + \
                [s for s in samples if s.t - t0 <= 0.3]
    if entry_win:
        entry_thr = sum(s.throttle for s in entry_win) / len(entry_win)
        if entry_thr >= PIN_THR:
            found.append((0.0, "entry_throttle",
                          f"entered at {samples[0].speed_kmh:.0f} km/h with "
                          f"throttle already {entry_thr:.0%}",
                          {"thr": entry_thr,
                           "speed": samples[0].speed_kmh}))

    # throttle debt: integral of (throttle - sustainable top) while the
    # angle keeps growing - catches "too much for this angle", not just 100%
    debt, debt_start = 0.0, None
    for a, b in zip(samples, samples[1:]):
        dt = b.t - a.t
        if not 0.0 < dt < 0.1 or abs(a.beta_deg) < SUSTAIN_BETA:
            continue
        growing = abs(b.beta_deg) >= abs(a.beta_deg) - 0.5
        over = a.throttle - band_fn(a.beta_deg)[1]
        if over > 0.05 and growing:
            if debt_start is None:
                debt_start = a
            debt += over * dt
            if debt >= DEBT_TRIGGER:
                found.append((debt_start.t - t0, "throttle_debt",
                              f"throttle {debt_start.throttle:.0%} at "
                              f"{abs(debt_start.beta_deg):.0f}° - above what "
                              f"~{band_fn(debt_start.beta_deg)[1]:.0%} sustains",
                              {"thr": debt_start.throttle,
                               "beta": abs(debt_start.beta_deg),
                               "target": band_fn(debt_start.beta_deg)[1]}))
                break
        elif over <= 0:
            debt, debt_start = 0.0, None

    # steering exhausted: at full lock and the fronts still dragged
    sat_since = None
    for s in samples:
        if abs(s.steer) >= STEER_SAT and s.cs_error > 0.5:
            if sat_since is None:
                sat_since = s
            elif s.t - sat_since.t >= 0.2:
                found.append((sat_since.t - t0, "steer_saturated",
                              f"full counter-lock at "
                              f"{abs(sat_since.beta_deg):.0f}° - steering "
                              "had nothing left",
                              {"beta": abs(sat_since.beta_deg)}))
                break
        else:
            sat_since = None

    # counter never applied: rotation established, steer stays near zero
    first_rot = next((s for s in samples if abs(s.beta_deg) >= 15), None)
    if first_rot is not None:
        window = [s for s in samples if 0 <= s.t - first_rot.t <= 0.6]
        best = max((s.steer * _sign(s.beta_deg) for s in window), default=0.0)
        if window and best < COUNTER_MIN:
            found.append((first_rot.t - t0, "counter_missing",
                          f"no real counter-steer by "
                          f"{abs(first_rot.beta_deg):.0f}° "
                          f"(max {best:.0%} of lock)",
                          {"beta": abs(first_rot.beta_deg)}))

    # point of no return: past the recoverable angle and still rotating
    ponr = None
    for a, b in zip(samples, samples[1:]):
        if abs(b.beta_deg) >= beta_max and abs(b.beta_deg) > abs(a.beta_deg):
            ponr = b.t - t0
            break

    found.sort(key=lambda x: x[0])
    causal = [f for f in found if ponr is None or f[0] <= ponr]
    root_code = causal[0][1] if causal else ""
    root_detail = causal[0][3] if causal else {}

    timeline = [f"t+{t:4.1f}s  {text}" + ("   <- ROOT CAUSE"
                if code == root_code and code else "")
                for t, code, text, _ in found]
    if ponr is not None:
        timeline.append(f"t+{ponr:4.1f}s  crossed ~{beta_max:.0f}° - past "
                        "recoverable, everything after is symptom")
        timeline.sort(key=lambda line: float(line[2:line.index("s")]))
    return timeline, root_code, root_detail


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


def analyze(ev: DriftEvent, mode: str = "free",
            calib: Calibration | None = None) -> EventReport:
    samples = ev.samples
    car = samples[0].car_ordinal
    if calib is not None:
        band_fn = lambda b: calib.throttle_band(car, b)
        beta_max = calib.beta_max(car)
    else:
        band_fn = lambda b: DEFAULT_BAND
        beta_max = DEFAULT_BETA_MAX

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

    timeline, root, detail = _causal_walk(ev, band_fn, beta_max)
    ponr_s = None
    for line in timeline:
        if "past recoverable" in line:
            ponr_s = float(line[2:line.index("s")])

    outcome, cause, verdict, info = _classify(ev, samples, cs_err, thr_mean)
    did, fix = _hud_detail(outcome, cause, info, cs_err * PEAK_SLIP_DEG,
                           scrub, lag, std_beta, thr_mean, thr_std,
                           ev.duration)
    if outcome == "spin" and root:
        cause, verdict, did, fix = _spin_from_root(root, detail)

    faults = _faults(outcome, cause, cs_err, scrub, lag, std_beta,
                     lifts, pct_full, pct_zero, mean_beta, thr_mean)
    faults = _reconcile_faults(faults, root, timeline, detail)
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
        score=score, verdict=verdict, did=did, fix=fix,
        root_cause=root, ponr_s=ponr_s, timeline=timeline, faults=faults,
    )


def _spin_from_root(root: str, d: dict):
    """Spin verdict/did/fix rewritten from the FIRST envelope violation."""
    if root == "entry_throttle":
        return ("throttle",
                "SPIN - throttle pinned from entry",
                f"entered {d['speed']:.0f} km/h at {d['thr']:.0%} throttle",
                "enter at ~70% and trim as angle builds")
    if root == "throttle_debt":
        return ("throttle",
                f"SPIN - too much throttle for {d['beta']:.0f}°",
                f"held {d['thr']:.0%} where ~{d['target']:.0%} "
                f"sustains {d['beta']:.0f}°",
                f"trim toward {d['target']:.0%} passing {d['beta']:.0f}°")
    if root == "steer_saturated":
        return ("beyond-lock",
                f"SPIN - beyond full lock at {d['beta']:.0f}°",
                "full counter, rotation still grew",
                "counter sooner + trim throttle earlier")
    return ("no-counter",
            "SPIN - counter never came",
            f"barely any counter by {d.get('beta', 0):.0f}°",
            "counter the instant rotation starts")


def _reconcile_faults(faults: list[str], root: str, timeline: list[str],
                      detail: dict) -> list[str]:
    """Symptom faults must not contradict the root cause: never ask for
    more counter-steer when the driver was already at full lock."""
    saturated = any("full counter-lock" in line for line in timeline)
    if saturated:
        faults = [f for f in faults
                  if not f.startswith(("Add ~", "Counter-steer was essentially"))]
        faults.insert(0, "You DID reach full lock - the answer isn't more "
                         "counter, it's less throttle / less entry speed.")
    if root in ("entry_throttle", "throttle_debt"):
        target = detail.get("target", DEFAULT_BAND[1])
        faults.insert(0, f"Throttle is the root cause here: hold near "
                         f"~{target:.0%} once the angle is set, and change "
                         "it 10-15% at a time.")
    return faults


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
