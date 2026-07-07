"""Writes telemetry recording sessions to disk.

Each session is a directory under recordings/:

    recordings/2026-07-07_18-45-12/
        raw.fzd          raw datagrams: 8-byte magic "FZDUMP01", then per
                         packet <dH> (unix timestamp float64, payload length)
                         followed by the payload bytes
        telemetry.jsonl  one parsed packet per line, with "t" timestamp
        meta.json        session summary (written on stop)

raw.fzd is the ground truth - even if the FH6 layout turns out to differ
from what packet.py assumes, the raw dumps stay reusable.
"""

from __future__ import annotations

import json
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

RAW_MAGIC = b"FZDUMP01"
RAW_RECORD_HEADER = struct.Struct("<dH")

_FLUSH_EVERY = 60  # packets (~1 s at 60 Hz)


class Recorder:
    """Not thread-safe by itself; the listener serialises write() calls."""

    def __init__(self, root_dir: Path, host: str, port: int):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = Path(root_dir) / stamp
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._raw = open(self.session_dir / "raw.fzd", "wb")
        self._raw.write(RAW_MAGIC)
        self._jsonl = open(self.session_dir / "telemetry.jsonl", "w", encoding="utf-8")

        self.started_at = time.time()
        self.packets = 0
        self.bytes = 0
        self.formats: dict[str, int] = {}
        self._meta = {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "host": host,
            "port": port,
        }

    def write(self, ts: float, raw: bytes, parsed: dict) -> None:
        self._raw.write(RAW_RECORD_HEADER.pack(ts, len(raw)))
        self._raw.write(raw)
        self._jsonl.write(json.dumps({"t": ts, **parsed}) + "\n")

        self.packets += 1
        self.bytes += len(raw)
        fmt = parsed.get("format", "?")
        self.formats[fmt] = self.formats.get(fmt, 0) + 1
        if self.packets % _FLUSH_EVERY == 0:
            self._raw.flush()
            self._jsonl.flush()

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def close(self) -> dict:
        self._raw.close()
        self._jsonl.close()
        summary = {
            **self._meta,
            "ended_utc": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(self.elapsed, 3),
            "packets": self.packets,
            "bytes": self.bytes,
            "formats": self.formats,
        }
        with open(self.session_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        summary["session_dir"] = str(self.session_dir)
        return summary
