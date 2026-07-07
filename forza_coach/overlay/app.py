"""Always-on-top overlay window (tkinter, no external dependencies).

Borderless, draggable, with a keyed-out background color so the rounded
panel floats over the game. The game must run in windowed or borderless
fullscreen mode for any overlay to be visible.
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path

from ..telemetry.listener import Snapshot, TelemetryListener
from . import theme

W, H = 340, 300
PAD = 16
UPDATE_MS = 100


def _rrect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle via a smoothed polygon."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class OverlayApp:
    def __init__(self, listener: TelemetryListener, recordings_dir: Path):
        self.listener = listener
        self.recordings_dir = recordings_dir
        self._drag: tuple[int, int] | None = None
        self._blink = False
        self._toast_until = 0.0

        root = tk.Tk()
        self.root = root
        root.title("Forza Drift Coach")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{W}x{H}+48+48")
        root.configure(bg=theme.TRANSPARENT_KEY)
        try:
            root.attributes("-transparentcolor", theme.TRANSPARENT_KEY)
        except tk.TclError:
            pass  # non-Windows fallback: square corners

        self.canvas = tk.Canvas(
            root, width=W, height=H, bg=theme.TRANSPARENT_KEY,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

        self._build()

    # -- static layout --------------------------------------------------------

    def _build(self) -> None:
        c = self.canvas
        font = theme.font

        _rrect(c, 0, 0, W, H, 18, fill=theme.BG, outline=theme.BORDER)

        # Header: accent slash + wordmark
        c.create_polygon(
            PAD + 6, 18, PAD + 14, 18, PAD + 6, 44, PAD - 2, 44,
            fill=theme.MAGENTA, outline="",
        )
        c.create_text(
            PAD + 22, 22, anchor="w", text="FORZA HORIZON 6 // TELEMETRY",
            fill=theme.CYAN, font=font(11, "bold", "italic"),
        )
        c.create_text(
            PAD + 22, 40, anchor="w", text="DRIFT COACH",
            fill=theme.TEXT, font=font(23, "bold", "italic"),
        )
        c.create_text(
            W - PAD - 4, 26, text="✕", fill=theme.MUTED,
            font=font(15, "bold"), tags=("close",),
        )

        # Connection card
        _rrect(c, PAD, 60, W - PAD, 116, 12, fill=theme.PANEL, outline="")
        c.create_oval(PAD + 14, 80, PAD + 30, 96, fill=theme.RED,
                      outline="", tags=("dot",))
        c.create_text(PAD + 42, 78, anchor="w", text="TELEMETRY",
                      fill=theme.MUTED, font=font(10, "bold"))
        c.create_text(PAD + 42, 96, anchor="w", text="NO SIGNAL",
                      fill=theme.RED, font=font(15, "bold", "italic"),
                      tags=("status",))
        c.create_text(W - PAD - 14, 78, anchor="e", text="0 HZ",
                      fill=theme.MUTED, font=font(12, mono=True), tags=("hz",))
        c.create_text(W - PAD - 14, 96, anchor="e", text="—",
                      fill=theme.MUTED, font=font(12, mono=True), tags=("fmt",))

        # Live stat cards: SPEED / RPM / GEAR / DRIFT
        self._stat_tags = []
        labels = [("SPEED", "KM/H"), ("ENGINE", "RPM"),
                  ("GEAR", ""), ("DRIFT", "DEG")]
        gap = 8
        cw = (W - 2 * PAD - 3 * gap) / 4
        y1, y2 = 126, 190
        for i, (label, unit) in enumerate(labels):
            x = PAD + i * (cw + gap)
            _rrect(c, x, y1, x + cw, y2, 10, fill=theme.PANEL, outline="")
            cx = x + cw / 2
            c.create_text(cx, y1 + 13, text=label, fill=theme.MUTED,
                          font=font(10, "bold"))
            tag = f"stat{i}"
            c.create_text(cx, y1 + 34, text="–", fill=theme.TEXT,
                          font=font(19, "bold", mono=True), tags=(tag,))
            c.create_text(cx, y1 + 53, text=unit, fill=theme.MUTED,
                          font=font(9))
            self._stat_tags.append(tag)

        # Record button
        _rrect(c, PAD, 202, W - PAD, 246, 12, fill=theme.MAGENTA,
               outline="", tags=("rec", "recbg"))
        c.create_oval(PAD + 16, 216, PAD + 32, 232, fill=theme.TEXT,
                      outline="", tags=("rec", "recdot"))
        c.create_text(W / 2 + 10, 224, text="START RECORDING",
                      fill=theme.TEXT, font=font(15, "bold", "italic"),
                      tags=("rec", "reclabel"))

        # Footer
        c.create_text(PAD + 2, 262, anchor="w",
                      text=f"UDP {self.listener.host}:{self.listener.port}",
                      fill=theme.MUTED, font=font(11, mono=True))
        c.create_text(W - PAD - 2, 262, anchor="e", text="DRAG TO MOVE",
                      fill=theme.MUTED, font=font(10, "bold"))
        c.create_text(W / 2, 282, text="", fill=theme.CYAN,
                      font=font(11, "bold"), tags=("toast",))

    # -- interaction -----------------------------------------------------------

    def _on_press(self, event: tk.Event) -> None:
        # Hit-test by coordinates: the "current" tag is only set after a
        # motion event, which a click straight onto the window doesn't get.
        items = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
        tags = {tag for item in items for tag in self.canvas.gettags(item)}
        if "close" in tags:
            self.close()
            return
        if "rec" in tags:
            self._toggle_recording()
            return
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag:
            dx, dy = self._drag
            self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _toggle_recording(self) -> None:
        if self.listener.snapshot().recording:
            summary = self.listener.stop_recording()
            if summary:
                path = Path(summary["session_dir"])
                try:
                    path = path.relative_to(Path.cwd())
                except ValueError:
                    pass  # keep absolute if outside the project
                self._toast(f"SAVED {summary['packets']} PKTS → {path}")
        else:
            self.listener.start_recording(self.recordings_dir)

    def _toast(self, text: str, seconds: float = 8.0) -> None:
        self.canvas.itemconfigure("toast", text=text)
        self._toast_until = time.time() + seconds

    # -- refresh loop ----------------------------------------------------------

    def _tick(self) -> None:
        snap = self.listener.snapshot()
        self._blink = not self._blink
        self._update_connection(snap)
        self._update_stats(snap)
        self._update_rec_button(snap)

        if self._toast_until and time.time() > self._toast_until:
            self.canvas.itemconfigure("toast", text="")
            self._toast_until = 0.0

        self.root.after(UPDATE_MS, self._tick)

    def _update_connection(self, snap: Snapshot) -> None:
        c = self.canvas
        receiving = snap.age is not None and snap.age < 1.0
        if not receiving:
            dot = theme.RED if self._blink else theme.MAGENTA_DIM
            c.itemconfigure("dot", fill=dot)
            c.itemconfigure("status", text="NO SIGNAL", fill=theme.RED)
            c.itemconfigure("hz", text="0 HZ")
            c.itemconfigure("fmt", text="—")
            return

        latest = snap.latest or {}
        if latest.get("is_race_on"):
            c.itemconfigure("dot", fill=theme.GREEN)
            c.itemconfigure("status", text="RECEIVING", fill=theme.GREEN)
        else:
            c.itemconfigure("dot", fill=theme.AMBER)
            c.itemconfigure("status", text="LINKED · PAUSED", fill=theme.AMBER)
        c.itemconfigure("hz", text=f"{snap.pps:.0f} HZ")
        c.itemconfigure(
            "fmt", text=f"{snap.packet_size} B · {latest.get('format', '?')}"
        )

    def _update_stats(self, snap: Snapshot) -> None:
        latest = snap.latest if snap.age is not None and snap.age < 1.0 else None
        if not latest or not latest.get("is_race_on"):
            values = ["–"] * 4
        else:
            gear = latest.get("gear")
            gear_txt = "–" if gear is None else ("R" if gear == 0 else str(gear))
            values = [
                f"{latest['speed_kmh']:.0f}",
                f"{latest['engine_rpm']:.0f}",
                gear_txt,
                f"{latest['drift_angle_deg']:+.1f}",
            ]
        for tag, value in zip(self._stat_tags, values):
            self.canvas.itemconfigure(tag, text=value)

    def _update_rec_button(self, snap: Snapshot) -> None:
        c = self.canvas
        if snap.recording:
            mins, secs = divmod(int(snap.rec_elapsed), 60)
            c.itemconfigure("recbg", fill=theme.PANEL_HI,
                            outline=theme.MAGENTA)
            c.itemconfigure("recdot",
                            fill=theme.RED if self._blink else theme.MAGENTA_DIM)
            c.itemconfigure(
                "reclabel",
                text=f"STOP  {mins}:{secs:02d} · {snap.rec_packets} PKTS",
            )
        else:
            c.itemconfigure("recbg", fill=theme.MAGENTA, outline="")
            c.itemconfigure("recdot", fill=theme.TEXT)
            c.itemconfigure("reclabel", text="START RECORDING")

    # -- lifecycle ---------------------------------------------------------------

    def run(self) -> None:
        self.root.after(UPDATE_MS, self._tick)
        self.root.mainloop()

    def close(self) -> None:
        self.listener.stop()
        self.root.destroy()
