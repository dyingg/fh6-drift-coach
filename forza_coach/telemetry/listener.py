"""Background UDP listener for the Forza Data Out stream."""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .packet import parse_packet
from .recorder import Recorder


@dataclass
class Snapshot:
    """Point-in-time view of the stream, consumed by the overlay."""

    latest: dict | None          # last parsed packet (None until first packet)
    age: float | None            # seconds since last packet
    pps: float                   # packets received in the last second
    packet_size: int             # size of the last datagram in bytes
    total_packets: int
    recording: bool
    rec_elapsed: float           # 0.0 when not recording
    rec_packets: int


class TelemetryListener(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(name="forza-telemetry", daemon=True)
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))  # raises OSError if the port is taken
        self._sock.settimeout(0.5)

        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._last_ts: float | None = None
        self._packet_size = 0
        self._total = 0
        self._recent: deque[float] = deque(maxlen=240)  # for the pps counter
        self._recorder: Recorder | None = None
        self._running = True

        # Wired by main.py; both may be None.
        self.on_packet: Callable[[dict], None] | None = None
        self.on_recorder_change: Callable[[Recorder | None], None] | None = None

    # -- thread body ---------------------------------------------------------

    def run(self) -> None:
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed during shutdown

            ts = time.time()
            parsed = parse_packet(data)
            parsed["t"] = ts
            with self._lock:
                self._latest = parsed
                self._last_ts = ts
                self._packet_size = len(data)
                self._total += 1
                self._recent.append(ts)
                if self._recorder is not None:
                    self._recorder.write(ts, data, parsed)
            if self.on_packet is not None:
                self.on_packet(parsed)

    # -- control (called from the UI thread) ---------------------------------

    def snapshot(self) -> Snapshot:
        now = time.time()
        with self._lock:
            rec = self._recorder
            return Snapshot(
                latest=self._latest,
                age=None if self._last_ts is None else now - self._last_ts,
                pps=float(sum(1 for t in self._recent if now - t <= 1.0)),
                packet_size=self._packet_size,
                total_packets=self._total,
                recording=rec is not None,
                rec_elapsed=rec.elapsed if rec else 0.0,
                rec_packets=rec.packets if rec else 0,
            )

    def start_recording(self, root_dir: Path,
                        extra_meta: dict | None = None) -> Path:
        recorder = Recorder(root_dir, self.host, self.port, extra_meta)
        with self._lock:
            self._recorder = recorder
        if self.on_recorder_change is not None:
            self.on_recorder_change(recorder)
        return recorder.session_dir

    def stop_recording(self) -> dict | None:
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if self.on_recorder_change is not None:
            self.on_recorder_change(None)
        return recorder.close() if recorder else None

    def stop(self) -> None:
        self._running = False
        self._sock.close()
        if self.is_alive():
            self.join(timeout=1.0)
        self.stop_recording()
