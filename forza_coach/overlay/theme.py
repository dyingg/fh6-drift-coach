"""Horizon-festival inspired palette for the overlay.

FH6's neon-Tokyo look: deep purple-black panels with hot magenta and
electric cyan accents, bold italic display type.
"""

# Color that tkinter keys out to make the window shape transparent.
# Must not be used anywhere in the actual UI.
TRANSPARENT_KEY = "#010203"

BG = "#0e0a18"        # window body, deep purple-black
PANEL = "#181026"     # cards
PANEL_HI = "#221737"  # hovered / active cards
BORDER = "#2c2148"

MAGENTA = "#ff2d78"   # primary accent
MAGENTA_DIM = "#7a1d43"
CYAN = "#00e5ff"      # secondary accent
TEXT = "#f4f0ff"
MUTED = "#8f85ad"

GREEN = "#3dff8f"     # telemetry live
AMBER = "#ffb020"     # linked but paused / in menus
RED = "#ff3b4a"       # no signal / recording dot

FONT = "Segoe UI"     # display font, used bold + italic
MONO = "Consolas"     # numeric readouts


def font(px: int, *mods: str, mono: bool = False) -> tuple:
    """Pixel-sized tkinter font (negative size = pixels, immune to DPI
    scaling) so text can't outgrow the canvas layout on scaled displays."""
    return (MONO if mono else FONT, -px, " ".join(mods)) if mods \
        else (MONO if mono else FONT, -px)
