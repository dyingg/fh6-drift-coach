"""Forza Horizon 6 drift coach - proof of concept entry point.

Starts the UDP telemetry listener and the overlay window.

    python main.py [--port 5300] [--host 0.0.0.0]
"""

import argparse
import ctypes
import sys
from pathlib import Path

from forza_coach import config
from forza_coach.coach.audio import AudioCoach
from forza_coach.coach.calibration import Calibration
from forza_coach.coach.live import LiveCoach
from forza_coach.overlay.app import OverlayApp
from forza_coach.telemetry.listener import TelemetryListener
from forza_coach.telemetry.wheel import WheelReader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                        help="UDP port set in the game's Data Out settings")
    parser.add_argument("--host", default=config.DEFAULT_HOST,
                        help="interface to bind (0.0.0.0 = all)")
    parser.add_argument("--recordings", type=Path, default=config.RECORDINGS_DIR,
                        help="directory for recording sessions")
    parser.add_argument("--scale", type=float, default=None,
                        help="UI scale factor (default: auto from screen size)")
    parser.add_argument("--no-audio", action="store_true",
                        help="disable spoken coaching cues and verdicts")
    parser.add_argument("--record-button", type=int, default=config.RECORD_BUTTON,
                        help="wheel button that toggles recording (-1 disables)")
    parser.add_argument("--no-wheel", action="store_true",
                        help="don't read the wheel at all (disables the "
                             "record hotkey and the wheel.jsonl stream)")
    args = parser.parse_args()

    # Crisp text on high-DPI displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    try:
        listener = TelemetryListener(args.host, args.port)
    except OSError as exc:
        print(f"Could not bind UDP {args.host}:{args.port} - {exc}", file=sys.stderr)
        print("Is another instance already running?", file=sys.stderr)
        return 1

    calibration = Calibration(args.recordings / "calibration.json")
    coach = LiveCoach(calibration=calibration)
    audio = AudioCoach(Path("assets/audio"), enabled=not args.no_audio)
    coach.on_cue = audio.on_cue
    coach.on_verdict = audio.on_verdict

    wheel = None if args.no_wheel else WheelReader()
    listener.on_packet = coach.feed
    if wheel is not None:
        listener.on_recorder_change = wheel.set_recorder
        wheel.start()
    listener.start()

    app = OverlayApp(listener, coach, wheel, recordings_dir=args.recordings,
                     scale=args.scale, audio=audio,
                     record_button=args.record_button)
    try:
        app.run()
    finally:
        if wheel is not None:
            wheel.stop()
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
