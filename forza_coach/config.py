"""Central defaults for the proof of concept."""

from pathlib import Path

# The port you enter in the game under Settings > HUD and Gameplay > Data Out.
DEFAULT_PORT = 5300

# 0.0.0.0 also accepts telemetry sent from a console on the same network.
DEFAULT_HOST = "0.0.0.0"

# Where recording sessions are written (gitignored).
RECORDINGS_DIR = Path("recordings")
