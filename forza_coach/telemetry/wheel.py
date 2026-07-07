"""Direct capture of the physical wheel (G29) via DirectInput.

The game's telemetry only knows the post-mapping steering value; reading
the device itself shows what the driver's HANDS did - wheel velocity,
whether they let it self-center through transitions, pedal overlap.
Recorded as a second stream (wheel.jsonl) alongside the telemetry.

pygame is optional: without it (or without a wheel) everything else works.
"""

from __future__ import annotations

import os
import threading
import time

POLL_HZ = 100.0


class WheelReader(threading.Thread):
    def __init__(self):
        super().__init__(name="wheel-reader", daemon=True)
        self._lock = threading.Lock()
        self._recorder = None
        self._running = True
        self.available = False
        self.device_name: str | None = None
        self._axes: list[float] = []
        self._buttons: list[int] = []
        self._presses: set[int] = set()  # down-edges since last consume

    def run(self) -> None:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # pygame's pkg_resources noise
                import pygame
        except ImportError:
            return
        try:
            pygame.init()
            pygame.joystick.init()
            if pygame.joystick.get_count() == 0:
                return
            # prefer a Logitech wheel, else take the first device
            js = None
            for i in range(pygame.joystick.get_count()):
                cand = pygame.joystick.Joystick(i)
                name = cand.get_name() or ""
                if any(k in name.upper() for k in ("G29", "G920", "G923", "LOGITECH")):
                    js = cand
                    break
            js = js or pygame.joystick.Joystick(0)
            js.init()
            self.device_name = js.get_name()
            self.available = True
        except Exception:
            return

        interval = 1.0 / POLL_HZ
        prev: list[int] = []
        while self._running:
            try:
                pygame.event.pump()
                ts = time.time()
                axes = [round(js.get_axis(i), 4) for i in range(js.get_numaxes())]
                buttons = [js.get_button(i) for i in range(js.get_numbuttons())]
            except Exception:
                break  # device unplugged or pygame torn down at shutdown
            edges = {i for i, b in enumerate(buttons)
                     if b and (i >= len(prev) or not prev[i])}
            prev = buttons
            with self._lock:
                self._axes, self._buttons = axes, buttons
                self._presses |= edges
                recorder = self._recorder
            if recorder is not None:
                recorder.write_wheel(ts, axes, buttons)
            time.sleep(interval)

    def set_recorder(self, recorder) -> None:
        """Called by the listener when recording starts (Recorder) / stops (None)."""
        with self._lock:
            self._recorder = recorder

    def snapshot(self) -> tuple[str | None, list[float]]:
        with self._lock:
            return self.device_name, list(self._axes)

    def consume_presses(self) -> set[int]:
        """Button indices that had a press (down-edge) since the last call.
        Polled by the UI tick - keeps tkinter interaction on the UI thread."""
        with self._lock:
            presses, self._presses = self._presses, set()
        return presses

    def stop(self) -> None:
        self._running = False
