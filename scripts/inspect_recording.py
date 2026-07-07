"""Prints a summary of a recording session and sanity-checks the files.

    python scripts/inspect_recording.py [recordings/2026-07-07_18-45-12]

With no argument, inspects the most recent session under recordings/.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forza_coach.telemetry.recorder import RAW_MAGIC, RAW_RECORD_HEADER


def count_raw_packets(path: Path) -> int:
    count = 0
    with open(path, "rb") as f:
        if f.read(len(RAW_MAGIC)) != RAW_MAGIC:
            raise ValueError(f"{path} has a bad magic header")
        while True:
            header = f.read(RAW_RECORD_HEADER.size)
            if not header:
                break
            _ts, length = RAW_RECORD_HEADER.unpack(header)
            if len(f.read(length)) != length:
                raise ValueError(f"{path} is truncated at packet {count}")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", type=Path,
                        help="session directory (default: latest)")
    args = parser.parse_args()

    session = args.session
    if session is None:
        sessions = sorted(p for p in Path("recordings").glob("*") if p.is_dir())
        if not sessions:
            print("No recordings found.", file=sys.stderr)
            return 1
        session = sessions[-1]

    meta = json.loads((session / "meta.json").read_text())
    raw_count = count_raw_packets(session / "raw.fzd")

    rows = []
    speeds, drifts = [], []
    with open(session / "telemetry.jsonl", encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    for row in rows:
        speeds.append(row.get("speed_kmh", 0.0))
        drifts.append(abs(row.get("drift_angle_deg", 0.0)))

    duration = meta.get("duration_s", 0.0)
    print(f"Session       {session}")
    print(f"Recorded      {meta.get('started_utc')} -> {meta.get('ended_utc')}")
    print(f"Duration      {duration:.1f}s")
    print(f"Packets       {meta.get('packets')} (raw file: {raw_count}, "
          f"jsonl: {len(rows)})")
    if duration:
        print(f"Rate          {len(rows) / duration:.1f} Hz")
    print(f"Formats       {meta.get('formats')}")
    if speeds:
        print(f"Speed         {min(speeds):.0f}-{max(speeds):.0f} km/h")
        print(f"Max |drift|   {max(drifts):.1f} deg")

    ok = meta.get("packets") == raw_count == len(rows)
    print("Integrity     OK" if ok else "Integrity     MISMATCH", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
