"""Scoring and fault diagnosis for a completed drift event.

The drift is judged as FOUR PHASES, in causal order:

  entry    how the car was thrown in (speed, throttle on initiation)
  catch    the first steering response to rotation (direction, reaction)
  sustain  holding the angle - throttle vs what actually sustains it, and
           whether the steering channel still had headroom
  exit     the release - throttle dumps, hanging on the counter through
           the snap-back

Each phase is marked correct or wrong from the telemetry, and the FIRST
wrong phase is the root cause: its numbers become the HUD verdict and
everything downstream is symptom. Once the slip angle passes the
recoverable maximum, or the steering is at full lock, "counter more" is no
longer valid advice and the verdict must point upstream (throttle, entry).

Reaction time is measured directly - rotation onset to the first
correct-direction counter-steer - never inferred from correlation (which
decouples and reports nonsense once the wheel saturates in a spin).
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
ROT_ESTABLISHED = 15.0 # deg - rotation is undeniably happening
ROT_ONSET = 9.0        # deg - where the rotation run is traced back to
REACTION_LATE_MS = 300 # slower than this to counter = late catch
SPIN_CAP_BETA = 110.0  # readings past this are post-spin junk, not drift

# Mode-specific hold expectations: a roundabout is one long continuous
# sustain; a corner drift is naturally short.
HOLD_TARGET_S = {"roundabout": 6.0, "s-bend": 4.0, "corner": 2.5}
HOLD_TARGET_DEFAULT_S = 3.0


@dataclass
class Phase:
    name: str            # entry | catch | sustain | exit
    ok: bool | None      # None = phase never happened (e.g. exit of a spin)
    text: str            # one line with the numbers, for the report card
    fault: str = ""      # root-cause code ("" when ok)
    did: str = ""        # HUD detail when this phase is the root
    fix: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class EventReport:
    t0: float
    duration: float
    direction: str          # "left" / "right" / "linked x2" ...
    outcome: str            # held | snap | spin | faded
    cause: str              # lift | over-correction | regrip | throttle | ...
    avg_speed: float
    peak_beta: float
    mean_beta: float        # mean |beta| while sustaining
    std_beta: float
    transitions: int
    cs_err_deg: float       # signed mean counter-steer error, deg (+ = add more)
    scrub_pct: float        # fraction of sustain with fronts saturated
    reaction_ms: float | None  # rotation onset -> first real counter-steer
    thr_mean: float
    thr_std: float
    thr_full_lifts: int
    thr_pct_full: float
    thr_pct_zero: float
    score: int
    verdict: str            # headline for the HUD
    did: str                # what the driver actually did, with numbers
    fix: str                # what to do instead
    root_cause: str         # fault code of the first wrong phase ("" = none)
    ponr_s: float | None    # seconds into the event past which recovery
                            # was impossible (spins only)
    phases: list[Phase] = field(default_factory=list)
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
            + (f", countered in {self.reaction_ms:.0f} ms"
               if self.reaction_ms is not None else ""),
            f"  throttle       {self.thr_mean * 100:.0f}% avg, "
            f"{self.thr_full_lifts} full lifts, "
            f"{self.thr_pct_full * 100:.0f}% pinned / "
            f"{self.thr_pct_zero * 100:.0f}% off",
            f"  outcome        {self.verdict}",
            f"  you            {self.did}",
            f"  score          {self.score}/100",
        ]
        if self.phases:
            lines.append("  phases")
            for p in self.phases:
                mark = "-" if p.ok is None else ("OK " if p.ok else "XX ")
                lines.append(f"    {p.name:<8}{mark} {p.text}")
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


def _outcome(ev: DriftEvent, thr_mean: float) -> tuple[str, dict]:
    """How the drift ENDED (mechanics only - blame comes from the phases)."""
    samples = ev.samples
    if ev.spun:
        # quote the drift's own peak, not the post-spin ±150° readings
        drift = [abs(s.beta_deg) for s in samples
                 if abs(s.beta_deg) < SPIN_CAP_BETA]
        return "spin", {"peak": max(drift) if drift else ev.peak_beta}

    # how fast did the angle collapse after the LAST significant peak?
    # (a long clean drift can still end in a snap - measure the ending)
    hi = [s for s in samples if abs(s.beta_deg) >= 18]
    if hi:
        peak_s = hi[-1]
        after = [s for s in samples if s.t > peak_s.t and abs(s.beta_deg) < 10]
        if after and after[0].t - peak_s.t <= SNAP_COLLAPSE_S:
            return "snap", {"peak": max(abs(s.beta_deg) for s in samples),
                            "peak_t": peak_s.t}

    if ev.duration < 2.0 and thr_mean < 0.35:
        return "faded", {"thr": thr_mean}
    return "held", {}


def _phase_analysis(ev: DriftEvent, band_fn, beta_max: float, mode: str,
                    outcome: str):
    """Segment the drift into entry/catch/sustain/exit and judge each.

    Returns (phases, reaction_ms, timeline, ponr_s).
    """
    samples, pre = ev.samples, ev.pre
    t0 = samples[0].t
    chain = pre + samples
    events_tl: list[tuple[float, str, str]] = []  # (t_offset, code, text)

    # point of no return: past the recoverable angle and still rotating
    ponr_t = None
    for a, b in zip(samples, samples[1:]):
        if abs(b.beta_deg) >= beta_max and abs(b.beta_deg) > abs(a.beta_deg):
            ponr_t = b.t
            break

    # ---- ENTRY: was the car thrown in with the throttle already pinned? ----
    entry_win = [s for s in pre if t0 - s.t <= 0.4] + \
                [s for s in samples if s.t - t0 <= 0.3]
    entry_thr = (sum(s.throttle for s in entry_win) / len(entry_win)
                 if entry_win else samples[0].throttle)
    speed0 = samples[0].speed_kmh
    if entry_thr >= PIN_THR:
        entry = Phase(
            "entry", False,
            f"thrown in at {speed0:.0f} km/h with throttle pinned "
            f"{entry_thr:.0%}",
            fault="entry_throttle",
            did=f"entered {speed0:.0f} km/h at {entry_thr:.0%} throttle",
            fix="enter at ~70% and trim as angle builds",
            data={"thr": entry_thr, "speed": speed0})
        events_tl.append((0.0, "entry_throttle",
                          f"entered at {speed0:.0f} km/h with throttle "
                          f"already {entry_thr:.0%}"))
    else:
        entry = Phase("entry", True,
                      f"{speed0:.0f} km/h in, throttle {entry_thr:.0%}")

    # ---- CATCH: first steering response to the rotation ----
    rot = next((s for s in samples if abs(s.beta_deg) >= ROT_ESTABLISHED),
               None)
    reaction_ms: float | None = None
    if rot is None:
        catch = Phase("catch", None, "rotation never fully established")
        d = _sign(samples[0].beta_deg)
        t_rot = t0
    else:
        d = _sign(rot.beta_deg)
        t_rot = rot.t
        # trace the rotation run back to its onset (may start in the preroll)
        i = next(j for j, s in enumerate(chain) if s is rot)
        while (i > 0 and abs(chain[i - 1].beta_deg) >= ROT_ONSET
               and chain[i].t - chain[i - 1].t < 0.2):
            i -= 1
        onset = chain[i].t
        win = [s for s in chain if onset <= s.t <= onset + 1.2
               and (ponr_t is None or s.t <= ponr_t)]
        first = next((s for s in win if s.steer * d >= COUNTER_MIN), None)
        ww_win = [s for s in win if s.t <= onset + 0.8]
        wrongway = (sum(1 for s in ww_win if s.steer * d <= -0.15)
                    / len(ww_win) if ww_win else 0.0)
        if first is not None:
            reaction_ms = (first.t - onset) * 1000
        if wrongway > 0.25:
            catch = Phase(
                "catch", False,
                f"steered WITH the slide for {wrongway:.0%} of the catch "
                "window",
                fault="counter_wrong",
                did="steered into the slide, not against it",
                fix="counter = steer where the car is GOING",
                data={})
            events_tl.append((onset - t0, "counter_wrong",
                              "steered with the slide as rotation built"))
        elif first is None:
            beta_by = max((abs(s.beta_deg) for s in win),
                          default=abs(rot.beta_deg))
            catch = Phase(
                "catch", False,
                f"no counter-steer while rotation built to {beta_by:.0f}°",
                fault="counter_missing",
                did=f"barely any counter by {beta_by:.0f}°",
                fix="counter the instant rotation starts",
                data={"beta": beta_by})
            events_tl.append((t_rot - t0, "counter_missing",
                              f"no real counter-steer by {beta_by:.0f}°"))
        elif reaction_ms > REACTION_LATE_MS:
            catch = Phase(
                "catch", False,
                f"counter came {reaction_ms:.0f} ms after rotation started",
                fault="counter_late",
                did=f"counter came ~{reaction_ms:.0f} ms late",
                fix="be on the counter as the rear steps out",
                data={"ms": reaction_ms})
            events_tl.append((onset - t0, "counter_late",
                              f"rotation started; counter took "
                              f"{reaction_ms:.0f} ms"))
        else:
            catch = Phase("catch", True,
                          f"countered in {reaction_ms:.0f} ms, "
                          "right direction")

    # ---- SUSTAIN: throttle discipline vs the calibrated envelope ----
    sus_end_t = samples[-1].t
    if ev.spun:
        if ponr_t is not None:
            sus_end_t = ponr_t
        else:
            drift_part = [s for s in samples
                          if abs(s.beta_deg) < SPIN_CAP_BETA]
            if drift_part:
                sus_end_t = drift_part[-1].t
    else:
        hi = [s for s in samples if abs(s.beta_deg) >= 18]
        if hi:
            sus_end_t = hi[-1].t

    S = [s for s in samples if t_rot <= s.t <= sus_end_t
         and SUSTAIN_BETA <= abs(s.beta_deg) < SPIN_CAP_BETA]
    if len(S) < 5:
        sustain = Phase("sustain", None, "over before it settled")
    else:
        betas_s = [abs(s.beta_deg) for s in S]
        mean_b = sum(betas_s) / len(betas_s)
        std_b = math.sqrt(sum((b - mean_b) ** 2 for b in betas_s) / len(betas_s))
        thr_S = sum(s.throttle for s in S) / len(S)
        band_lo = sum(band_fn(s.beta_deg)[0] for s in S) / len(S)
        band_hi = sum(band_fn(s.beta_deg)[1] for s in S) / len(S)

        # throttle debt: integral of (throttle - sustainable top) while the
        # angle keeps growing - catches "too much for this angle", not just
        # 100%. For spun events the bar is lower: the spin itself proves the
        # throttle was unsustainable, so any meaningful TIME spent above the
        # band while the angle grew takes the blame (over_time), even when
        # the integral stays under the trigger.
        debt, debt_start, debt_hit = 0.0, None, None
        over_time, over_samples = 0.0, []
        for a, b in zip(S, S[1:]):
            dt = b.t - a.t
            if not 0.0 < dt < 0.1:
                continue
            growing = abs(b.beta_deg) >= abs(a.beta_deg) - 0.5
            over = a.throttle - band_fn(a.beta_deg)[1]
            if over > 0.05 and growing:
                if debt_start is None:
                    debt_start = a
                over_samples.append((over, a))
                over_time += dt
                debt += over * dt
                if debt >= DEBT_TRIGGER and debt_hit is None:
                    debt_hit = debt_start
            elif over <= 0:
                debt, debt_start = 0.0, None
        if debt_hit is None and ev.spun and over_time >= 0.5:
            debt_hit = over_samples[0][1]

        # steering exhausted: at full lock and the fronts still dragged
        sat, sat_since = None, None
        for s in S:
            if abs(s.steer) >= STEER_SAT and s.cs_error > 0.5:
                if sat_since is None:
                    sat_since = s
                elif s.t - sat_since.t >= 0.2:
                    sat = sat_since
                    break
            else:
                sat_since = None

        if debt_hit is not None:
            # anchor the numbers on the WORST moment (biggest overage), not
            # the first sample over band - "68% where 48% sustains 40°" is
            # the causal picture, "49% vs 46%" undersells it
            worst = max(over_samples, key=lambda x: x[0])[1]
            run_thr = (sum(s.throttle for _, s in over_samples)
                       / len(over_samples))
            beta_at = abs(worst.beta_deg)
            target = band_fn(worst.beta_deg)[1]
            sustain = Phase(
                "sustain", False,
                f"{run_thr:.0%} throttle while the angle grew "
                f"{min(betas_s):.0f}->{max(betas_s):.0f}° - "
                f"~{target:.0%} sustains {beta_at:.0f}°",
                fault="throttle",
                did=f"held {run_thr:.0%} where ~{target:.0%} "
                    f"sustains {beta_at:.0f}°",
                fix=f"trim toward {target:.0%} passing {beta_at:.0f}°",
                data={"thr": run_thr, "beta": beta_at, "target": target})
            events_tl.append((debt_hit.t - t0, "throttle",
                              f"throttle {debt_hit.throttle:.0%} at "
                              f"{abs(debt_hit.beta_deg):.0f}° - above what "
                              f"~{band_fn(debt_hit.beta_deg)[1]:.0%} "
                              "sustains"))
            if sat is not None:
                events_tl.append((sat.t - t0, "steer_saturated",
                                  f"full counter-lock at "
                                  f"{abs(sat.beta_deg):.0f}° - steering had "
                                  "nothing left (symptom)"))
        elif sat is not None:
            sustain = Phase(
                "sustain", False,
                f"hit full counter-lock at {abs(sat.beta_deg):.0f}° - "
                "steering had nothing left",
                fault="steer_saturated",
                did="full counter, rotation still grew",
                fix="less throttle in - save the lock for the catch",
                data={"beta": abs(sat.beta_deg)})
            events_tl.append((sat.t - t0, "steer_saturated",
                              f"full counter-lock at {abs(sat.beta_deg):.0f}° "
                              "- steering had nothing left"))
        elif (not ev.spun and thr_S < band_lo - 0.10
              and betas_s[-1] <= 0.7 * max(betas_s)):
            sustain = Phase(
                "sustain", False,
                f"throttle faded to {thr_S:.0%} - the angle needs "
                f"~{band_lo:.0%}+ to stay lit",
                fault="starved",
                did=f"only {thr_S:.0%} throttle at {mean_b:.0f}°",
                fix=f"hold {band_lo:.0%}-{band_hi:.0%} to keep the rear loose",
                data={"thr": thr_S, "lo": band_lo, "hi": band_hi})
        elif not ev.spun and std_b > (7.0 if mode == "roundabout" else 9.0):
            # (a spun event's swings are recovery attempts - symptoms,
            # never the root)
            sustain = Phase(
                "sustain", False,
                f"angle swung ±{std_b:.0f}° at {thr_S:.0%} throttle",
                fault="wobble",
                did=f"angle swung ±{std_b:.0f}°",
                fix="halve the corrections - two small catches, not one big",
                data={"std": std_b})
        else:
            sustain = Phase(
                "sustain", True,
                f"{mean_b:.0f}° at {thr_S:.0%} throttle "
                f"(band ~{band_lo:.0%}-{band_hi:.0%}), ±{std_b:.0f}°")

    # ---- EXIT: the release ----
    if ev.spun:
        exit_p = Phase("exit", None, "spun - never reached the exit")
    elif outcome == "faded":
        exit_p = Phase("exit", None, "angle faded before a real exit")
    elif outcome == "held":
        exit_p = Phase("exit", True, "released cleanly")
    else:  # snap - judge how it was dropped
        tail = [s for s in samples if s.t >= sus_end_t]
        lift = _lift_before(samples, sus_end_t + SNAP_COLLAPSE_S)
        over = [s for s in tail if s.cs_error < -1.0]
        d_end = _sign(next((s.beta_deg for s in reversed(samples)
                            if s.t <= sus_end_t), d))
        reverse_lock = any(
            s.beta_deg * d_end < -6
            and s.steer * _sign(s.beta_deg) < -0.3
            for s in tail)
        if lift is not None:
            near = [s for s in samples
                    if lift.t <= s.t <= lift.t + LIFT_WINDOW_S + 0.2]
            to = min((s.throttle for s in near), default=0.0)
            exit_p = Phase(
                "exit", False,
                f"dumped throttle {lift.throttle:.0%}->{to:.0%} at "
                f"{abs(lift.beta_deg):.0f}°",
                fault="lift",
                did=f"cut throttle {lift.throttle * 100:.0f}%->"
                    f"{to * 100:.0f}% at {abs(lift.beta_deg):.0f}°",
                fix="trim 10-15% instead - never dump it",
                data={"from": lift.throttle, "to": to,
                      "beta": abs(lift.beta_deg)})
            events_tl.append((lift.t - t0, "lift",
                              f"dumped throttle at {abs(lift.beta_deg):.0f}° "
                              "- rear regripped and snapped"))
        elif reverse_lock or len(over) >= max(2, len(tail) // 4):
            exit_p = Phase(
                "exit", False,
                "held the counter through the snap-back - car whipped the "
                "other way",
                fault="over-correction",
                did="kept the counter on as the car straightened",
                fix="unwind as the nose comes back - hands lead the regrip",
                data={})
            events_tl.append((sus_end_t - t0, "over-correction",
                              "counter held past the regrip - snapped the "
                              "other way"))
        else:
            exit_p = Phase(
                "exit", False,
                "rear hooked up abruptly - drift ran out of commitment",
                fault="regrip",
                did="rear regripped with throttle in band",
                fix="a touch more entry speed or angle next time",
                data={})

    phases = [entry, catch, sustain, exit_p]

    # timeline: violations in time order + the point of no return
    root_fault = next((p.fault for p in phases if p.ok is False), "")
    if ponr_t is not None:
        events_tl.append((ponr_t - t0, "",
                          f"crossed ~{beta_max:.0f}° - past recoverable, "
                          "everything after is symptom"))
    events_tl.sort(key=lambda x: x[0])
    timeline = [f"t{t:+5.1f}s  {text}"
                + ("   <- ROOT CAUSE" if code and code == root_fault else "")
                for t, code, text in events_tl]
    ponr_s = None if ponr_t is None else ponr_t - t0
    return phases, reaction_ms, timeline, ponr_s


def _verdict_from_phases(outcome: str, phases: list[Phase], info: dict,
                         ev: DriftEvent, mode: str, mean_beta: float,
                         std_beta: float, thr_mean: float, thr_std: float):
    """(cause, verdict, did, fix) - headline from the first wrong phase."""
    root = next((p for p in phases if p.ok is False), None)

    if outcome == "spin":
        peak = info["peak"]
        if root is None:
            return ("throttle", f"SPIN - lost past {peak:.0f}°",
                    f"rotation outran the car at {peak:.0f}°",
                    "less speed or throttle into this one")
        headline = {
            "entry_throttle": "SPIN - throttle pinned from entry",
            "throttle": f"SPIN - too much throttle for "
                        f"{root.data.get('beta', peak):.0f}°",
            "steer_saturated": f"SPIN - beyond full lock at "
                               f"{root.data.get('beta', peak):.0f}°",
            "counter_missing": "SPIN - counter never came",
            "counter_late": "SPIN - counter too late",
            "counter_wrong": "SPIN - steered the wrong way",
        }.get(root.fault, f"SPIN at {peak:.0f}°")
        cause = {
            "entry_throttle": "throttle", "throttle": "throttle",
            "steer_saturated": "beyond-lock",
            "counter_missing": "no-counter", "counter_late": "late-counter",
            "counter_wrong": "no-counter",
        }.get(root.fault, "throttle")
        return cause, headline, root.did, root.fix

    if outcome == "snap":
        peak = info["peak"]
        if root is None:
            return ("regrip", f"SNAP at {peak:.0f}° - regripped",
                    "rear hooked up mid-drift",
                    "a touch more entry speed or angle")
        headline = {
            "lift": f"SNAP - you lifted at {root.data.get('beta', peak):.0f}°",
            "over-correction": "SNAP - over-corrected, unwind less",
            "regrip": f"SNAP at {peak:.0f}° - ran out of commitment",
            "starved": f"SNAP at {peak:.0f}° - commit: more throttle",
            "throttle": f"SNAP - too much throttle for "
                        f"{root.data.get('beta', peak):.0f}°",
            "counter_wrong": "SNAP - steered the wrong way",
            "counter_missing": "SNAP - no counter, car straightened",
            "counter_late": "SNAP - counter too late",
        }.get(root.fault, f"SNAP at {peak:.0f}°")
        cause = {"lift": "lift", "over-correction": "over-correction",
                 "regrip": "regrip", "starved": "regrip",
                 "throttle": "throttle",
                 "counter_wrong": "over-correction"}.get(root.fault, "regrip")
        return cause, headline, root.did, root.fix

    if outcome == "faded":
        did = (root.did if root is not None
               else f"throttle faded to {info.get('thr', thr_mean):.0%} avg")
        fix = (root.fix if root is not None
               else "stay on it - the angle needs power")
        return "lift", "FADED - stay on the throttle", did, fix

    # held
    target_s = HOLD_TARGET_S.get(mode, HOLD_TARGET_DEFAULT_S)
    mark = " ✓" if ev.duration >= target_s else ""
    mean = sum(abs(s.beta_deg) for s in ev.samples) / len(ev.samples)
    verdict = f"HELD {ev.duration:.1f}s @ {mean:.0f}°{mark}"
    if root is not None:
        return "", verdict, root.did, root.fix
    did = (f"±{std_beta:.0f}° swing · throttle {thr_mean * 100:.0f}%"
           f"±{thr_std * 100:.0f}")
    if ev.duration < target_s:
        fix = f"good shape - now hold it {target_s:.0f}s+"
    else:
        fix = "clean - push for more angle"
    return "", verdict, did, fix


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

    # stats over the drift itself - post-spin junk readings excluded
    sustain = [s for s in samples if SUSTAIN_BETA <= abs(s.beta_deg)
               and abs(s.beta_deg) < SPIN_CAP_BETA]
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

    thr = [s.throttle for s in core]
    thr_mean = sum(thr) / len(thr)
    pct_full = sum(1 for x in thr if x > 0.95) / len(thr)
    pct_zero = sum(1 for x in thr if x < 0.05) / len(thr)
    lifts = 0
    for a, b in zip(core, core[1:]):
        if a.throttle - b.throttle >= LIFT_DROP and b.t - a.t <= LIFT_WINDOW_S:
            lifts += 1
    thr_std = math.sqrt(sum((x - thr_mean) ** 2 for x in thr) / len(thr))

    outcome, info = _outcome(ev, thr_mean)
    phases, reaction_ms, timeline, ponr_s = _phase_analysis(
        ev, band_fn, beta_max, mode, outcome)
    root = next((p for p in phases if p.ok is False), None)
    cause, verdict, did, fix = _verdict_from_phases(
        outcome, phases, info, ev, mode, mean_beta, std_beta,
        thr_mean, thr_std)

    band_ref = band_fn(max(20.0, mean_beta))
    faults = _faults(outcome, cause, cs_err, scrub, reaction_ms, std_beta,
                     lifts, pct_full, pct_zero, mean_beta, thr_mean,
                     band_ref, mode, transitions)
    faults = _reconcile_faults(faults, root, timeline)
    score = _score(outcome, std_beta, scrub, reaction_ms, lifts, ev.duration)

    peak_beta = info["peak"] if outcome == "spin" else ev.peak_beta
    return EventReport(
        t0=ev.t0, duration=ev.duration, direction=direction,
        outcome=outcome, cause=cause,
        avg_speed=sum(s.speed_kmh for s in core) / len(core),
        peak_beta=peak_beta, mean_beta=mean_beta, std_beta=std_beta,
        transitions=transitions, cs_err_deg=cs_err * PEAK_SLIP_DEG,
        scrub_pct=scrub, reaction_ms=reaction_ms,
        thr_mean=thr_mean, thr_std=thr_std, thr_full_lifts=lifts,
        thr_pct_full=pct_full, thr_pct_zero=pct_zero,
        score=score, verdict=verdict, did=did, fix=fix,
        root_cause=root.fault if root is not None else "",
        ponr_s=ponr_s, phases=phases, timeline=timeline, faults=faults,
    )


def _reconcile_faults(faults: list[str], root: Phase | None,
                      timeline: list[str]) -> list[str]:
    """Symptom faults must not contradict the root cause: never ask for more
    counter-steer when the driver was already at full lock, or when the root
    cause was throttle (the catch phase already cleared the steering - the
    high cs-error is just the fronts washed out by an angle throttle built)."""
    counter_blame = ("Add ~", "Counter-steer was essentially",
                     "Counter-steer is late")
    saturated = any("full counter-lock" in line for line in timeline)
    throttle_root = (root is not None
                     and root.fault in ("entry_throttle", "throttle"))
    if saturated or throttle_root:
        faults = [f for f in faults if not f.startswith(counter_blame)]
    if saturated:
        faults.insert(0, "You DID reach full lock - the answer isn't more "
                         "counter, it's less throttle / less entry speed.")
    if throttle_root:
        target = root.data.get("target", DEFAULT_BAND[1])
        faults.insert(0, f"Throttle is the root cause here: hold near "
                         f"~{target:.0%} once the angle is set, and change "
                         "it 10-15% at a time.")
    return faults


def _faults(outcome, cause, cs_err, scrub, reaction_ms, std_beta,
            lifts, pct_full, pct_zero, mean_beta, thr_mean,
            band: tuple[float, float], mode: str,
            transitions: int) -> list[str]:
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
    if reaction_ms is not None and reaction_ms > REACTION_LATE_MS:
        faults.append(
            f"Counter-steer is late by ~{reaction_ms:.0f} ms - be on it as "
            "the rear steps out, not after the nose is past your line."
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
            f"Throttle is 0<->100% pumping ({lifts} full lifts) - hold a "
            f"{band[0]:.0%}-{band[1]:.0%} base and make ±10% adjustments."
        )
    if outcome == "spin" and pct_full > 0.4:
        faults.append(
            f"You held full throttle {pct_full * 100:.0f}% of the drift - "
            "past ~40° the rear can't recover; breathe the pedal the moment "
            "rotation keeps growing."
        )
    if outcome == "held" and mean_beta < 18 and thr_mean < band[0]:
        faults.append(
            "Shallow and tentative - commit to more entry angle and more "
            "throttle; 25-35° is easier to hold than 15°."
        )
    if mode == "roundabout" and transitions > 0:
        faults.append(
            "Direction flipped mid-roundabout - that's a lost-and-recaught "
            "drift; keep the arc one-directional."
        )
    return faults


def _score(outcome, std_beta, scrub, reaction_ms, lifts, duration) -> int:
    score = 100
    score -= {"spin": 50, "snap": 30, "faded": 15}.get(outcome, 0)
    score -= min(20, max(0.0, std_beta - 6) * 2)
    score -= scrub * 20
    if reaction_ms is not None and reaction_ms > REACTION_LATE_MS:
        score -= 15
    score -= min(15, lifts * 5)
    score += min(10, duration)  # reward long drifts
    return max(5, min(100, round(score)))
