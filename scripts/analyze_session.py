"""Post-session drift analysis: report cards for every drift in a recording.

    python scripts/analyze_session.py [recordings/<session>]

With no argument, analyzes the most recent session under recordings/.
Writes analysis.json into the session directory and prints the report.
Uses the exact same detector/metrics as the live HUD coach.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forza_coach.coach.conventions import enrich
from forza_coach.coach.events import DriftDetector
from forza_coach.coach.metrics import EventReport, analyze


def load_reports(session: Path, mode: str) -> list[EventReport]:
    detector = DriftDetector()
    reports: list[EventReport] = []
    with open(session / "telemetry.jsonl", encoding="utf-8") as f:
        for line in f:
            sample = enrich(json.loads(line))
            if sample is None:
                continue
            event = detector.feed(sample)
            if event is not None:
                reports.append(analyze(event, mode))
    event = detector.flush()
    if event is not None:
        reports.append(analyze(event, mode))
    return reports


def summarize(reports: list[EventReport]) -> list[str]:
    lines = []
    outcomes = Counter(r.outcome for r in reports)
    total = len(reports)
    avg = sum(r.score for r in reports) / total
    lines.append(
        f"{total} drifts - avg score {avg:.0f}/100 - "
        + ", ".join(f"{n}x {o}" for o, n in outcomes.most_common())
    )

    # Recurring faults, most frequent first
    fault_counts = Counter()
    for r in reports:
        for f in r.faults:
            fault_counts[f.split(" - ")[0].split(",")[0]] += 1
    top = [f"{msg}  ({n}/{total} drifts)"
           for msg, n in fault_counts.most_common(3) if n >= 2]
    if top:
        lines.append("Work on, in order:")
        lines += [f"  {i + 1}. {t}" for i, t in enumerate(top)]

    held = [r for r in reports if r.outcome == "held"]
    if held:
        best = max(held, key=lambda r: r.score)
        lines.append(
            f"Best drift: {best.duration:.1f}s at {best.mean_beta:.0f}° "
            f"(±{best.std_beta:.0f}°), score {best.score}."
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", type=Path,
                        help="session directory (default: latest)")
    parser.add_argument("--mode", default=None,
                        help="override the mode stored in meta.json")
    args = parser.parse_args()

    session = args.session
    if session is None:
        sessions = sorted(p for p in Path("recordings").glob("*") if p.is_dir())
        if not sessions:
            print("No recordings found.", file=sys.stderr)
            return 1
        session = sessions[-1]

    meta = json.loads((session / "meta.json").read_text())
    mode = args.mode or meta.get("mode", "free")

    reports = load_reports(session, mode)
    print(f"Session {session}  (mode: {mode})")
    if not reports:
        print("No drift events found - is this a driving recording?")
        return 0

    print("=" * 64)
    for i, r in enumerate(reports, 1):
        print(r.card(i))
        print("-" * 64)
    for line in summarize(reports):
        print(line)

    out = session / "analysis.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"mode": mode,
                   "events": [dataclasses.asdict(r) for r in reports]},
                  f, indent=2)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
