"""Forza Horizon 6 drift coach - proof of concept entry point.

Starts the UDP telemetry listener and the overlay window.

    python main.py [--port 5300] [--host 0.0.0.0]
"""

import argparse
import ctypes
import sys
from pathlib import Path

from forza_coach import config
from forza_coach.overlay.app import OverlayApp
from forza_coach.telemetry.listener import TelemetryListener


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                        help="UDP port set in the game's Data Out settings")
    parser.add_argument("--host", default=config.DEFAULT_HOST,
                        help="interface to bind (0.0.0.0 = all)")
    parser.add_argument("--recordings", type=Path, default=config.RECORDINGS_DIR,
                        help="directory for recording sessions")
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
    listener.start()

    app = OverlayApp(listener, recordings_dir=args.recordings)
    try:
        app.run()
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
