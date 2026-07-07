"""Writes telemetry recording sessions to disk.

Each session is a directory under recordings/:

    recordings/2026-07-07_18-45-12/
        raw.fzd          raw datagrams: 8-byte magic "FZDUMP01", then per
                         packet <dH> (unix timestamp float64, payload length)
                         followed by the payload bytes
        telemetry.jsonl  one parsed packet per line, with "t" timestamp
        wheel.jsonl      (only with a wheel connected) raw DirectInput axes
                         and buttons at ~100 Hz: {"t", "axes", "buttons"}
        meta.json        session summary (written on stop)

raw.fzd is the ground truth - even if the FH6 layout turns out to differ
from what packet.py assumes, the raw dumps stay reusable.
"""

from __future__ import annotations

import json
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

RAW_MAGIC = b"FZDUMP01"
RAW_RECORD_HEADER = struct.Struct("<dH")

_FLUSH_EVERY = 60  # packets (<1 s of telemetry)


class Recorder:
    """Thread-safe: telemetry writes come from the listener thread,
    wheel writes from the wheel thread."""

    def __init__(self, root_dir: Path, host: str, port: int,
                 extra_meta: dict | None = None):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = Path(root_dir) / stamp
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._closed = False
        self._raw = open(self.session_dir / "raw.fzd", "wb")
        self._raw.write(RAW_MAGIC)
        self._jsonl = open(self.session_dir / "telemetry.jsonl", "w", encoding="utf-8")
        self._wheel = None  # opened lazily on the first wheel sample

        self.started_at = time.time()
        self.packets = 0
        self.bytes = 0
        self.wheel_samples = 0
        self.formats: dict[str, int] = {}
        self._meta = {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "host": host,
            "port": port,
            **(extra_meta or {}),
        }

    def write(self, ts: float, raw: bytes, parsed: dict) -> None:
        with self._lock:
            if self._closed:
                return
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

    def write_wheel(self, ts: float, axes: list[float], buttons: list[int]) -> None:
        with self._lock:
            if self._closed:
                return
            if self._wheel is None:
                self._wheel = open(self.session_dir / "wheel.jsonl", "w",
                                   encoding="utf-8")
            self._wheel.write(json.dumps(
                {"t": ts, "axes": axes, "buttons": buttons}) + "\n")
            self.wheel_samples += 1
            if self.wheel_samples % 100 == 0:
                self._wheel.flush()

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def close(self) -> dict:
        with self._lock:
            self._closed = True
            self._raw.close()
            self._jsonl.close()
            if self._wheel is not None:
                self._wheel.close()
            summary = {
                **self._meta,
                "ended_utc": datetime.now(timezone.utc).isoformat(),
                "duration_s": round(self.elapsed, 3),
                "packets": self.packets,
                "bytes": self.bytes,
                "wheel_samples": self.wheel_samples,
                "formats": self.formats,
            }
        with open(self.session_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        summary["session_dir"] = str(self.session_dir)
        return summary
