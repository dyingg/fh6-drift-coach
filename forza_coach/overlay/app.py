"""Always-on-top overlay window (tkinter, no external dependencies).

Borderless, draggable, with a keyed-out background color so the rounded
panel floats over the game. The game must run in windowed or borderless
fullscreen mode for any overlay to be visible.

Coaching-first layout: header, slim status strip, live stats, then the
coach panel as the hero (mode chips, angle bar, throttle bar with target
band, large cue, and a structured last-drift verdict: headline / what you
did / the fix). Recording lives in a small footer pill.
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path

from ..coach.live import THROTTLE_BAND, LiveCoach
from ..telemetry.listener import Snapshot, TelemetryListener
from ..telemetry.wheel import WheelReader
from . import theme

W, H = 340, 428
PAD = 16
UPDATE_MS = 100

BAR_X0, BAR_X1 = 30, 310          # drift-angle bar extents
BAR_CX = (BAR_X0 + BAR_X1) // 2
BAR_RANGE_DEG = 65.0

THR_X0, THR_X1 = 56, 284          # throttle bar extents

MODE_CHIPS = [                    # (chip label, coach mode key)
    ("FREE", "free"),
    ("ROUND", "roundabout"),
    ("CORNER", "corner"),
    ("S-BEND", "s-bend"),
]

_LEVEL_COLOR = {"ok": theme.GREEN, "warn": theme.AMBER, "danger": theme.RED}


def _rrect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle via a smoothed polygon."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class OverlayApp:
    def __init__(self, listener: TelemetryListener, coach: LiveCoach,
                 wheel: WheelReader | None, recordings_dir: Path,
                 scale: float | None = None, audio=None,
                 record_button: int = -1):
        self.listener = listener
        self.coach = coach
        self.wheel = wheel
        self.audio = audio
        self.record_button = record_button
        self.recordings_dir = recordings_dir
        self.mode = coach.mode
        self._drag: tuple[int, int] | None = None
        self._blink = False
        self._toast_until = 0.0

        root = tk.Tk()
        self.root = root

        # UI scale: explicit --scale, else sized from the screen height
        # (design layout targets a ~960px-tall display).
        if scale is None:
            scale = max(1.0, min(3.0, root.winfo_screenheight() / 960))
        self.k = scale
        theme.set_scale(scale)

        root.title("Forza Drift Coach")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{round(W * scale)}x{round(H * scale)}+48+48")
        root.configure(bg=theme.TRANSPARENT_KEY)
        try:
            root.attributes("-transparentcolor", theme.TRANSPARENT_KEY)
        except tk.TclError:
            pass  # non-Windows fallback: square corners

        self.canvas = tk.Canvas(
            root, width=round(W * scale), height=round(H * scale),
            bg=theme.TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

        self._build()
        # Layout is drawn in design units; scale every item once. Fonts are
        # already scaled via theme.font, and _coords() scales dynamic updates.
        if scale != 1.0:
            self.canvas.scale("all", 0, 0, scale, scale)
        self._select_mode(self.mode)

    def _coords(self, tag: str, *pts: float) -> None:
        """canvas.coords() taking design-unit points."""
        self.canvas.coords(tag, *[p * self.k for p in pts])

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

        # Slim status strip
        _rrect(c, PAD, 58, W - PAD, 86, 10, fill=theme.PANEL, outline="")
        c.create_oval(PAD + 12, 66, PAD + 24, 78, fill=theme.RED,
                      outline="", tags=("dot",))
        c.create_text(PAD + 34, 72, anchor="w", text="NO SIGNAL",
                      fill=theme.RED, font=font(13, "bold", "italic"),
                      tags=("status",))
        c.create_text(W - PAD - 12, 72, anchor="e", text="—",
                      fill=theme.MUTED, font=font(11, mono=True), tags=("hz",))

        # Live stat cards: SPEED / RPM / GEAR / DRIFT
        self._stat_tags = []
        labels = [("SPEED", "KM/H"), ("ENGINE", "RPM"),
                  ("GEAR", ""), ("DRIFT", "DEG")]
        gap = 8
        cw = (W - 2 * PAD - 3 * gap) / 4
        y1, y2 = 96, 152
        for i, (label, unit) in enumerate(labels):
            x = PAD + i * (cw + gap)
            _rrect(c, x, y1, x + cw, y2, 10, fill=theme.PANEL, outline="")
            cx = x + cw / 2
            c.create_text(cx, y1 + 12, text=label, fill=theme.MUTED,
                          font=font(10, "bold"))
            tag = f"stat{i}"
            c.create_text(cx, y1 + 30, text="–", fill=theme.TEXT,
                          font=font(18, "bold", mono=True), tags=(tag,))
            c.create_text(cx, y1 + 47, text=unit, fill=theme.MUTED,
                          font=font(9))
            self._stat_tags.append(tag)

        # Coach panel (hero)
        _rrect(c, PAD, 162, W - PAD, 368, 12, fill=theme.PANEL, outline="")

        self._chips: dict[str, tuple[int, int]] = {}
        for i, (label, key) in enumerate(MODE_CHIPS):
            x = 24 + i * 74
            rect = _rrect(c, x, 172, x + 68, 192, 10, fill=theme.PANEL_HI,
                          outline="", tags=("chip", f"chip:{key}"))
            text = c.create_text(x + 34, 182, text=label, fill=theme.MUTED,
                                 font=font(10, "bold"),
                                 tags=("chip", f"chip:{key}"))
            self._chips[key] = (rect, text)

        # Drift-angle bar: fills left for left-hand drift, right for right
        _rrect(c, BAR_X0, 202, BAR_X1, 218, 8, fill=theme.PANEL_HI, outline="")
        c.create_rectangle(BAR_CX, 204, BAR_CX, 216, fill=theme.CYAN,
                           outline="", tags=("betafill",))
        c.create_line(BAR_CX, 198, BAR_CX, 222, fill=theme.BORDER)
        c.create_text(BAR_X0 - 10, 210, text="L", fill=theme.MUTED, font=font(10))
        c.create_text(BAR_X1 + 10, 210, text="R", fill=theme.MUTED, font=font(10))

        # Throttle bar with the sustain target band
        c.create_text(THR_X0 - 8, 236, anchor="e", text="THR",
                      fill=theme.MUTED, font=font(9, "bold"))
        _rrect(c, THR_X0, 228, THR_X1, 244, 8, fill=theme.PANEL_HI, outline="")
        bx0 = THR_X0 + (THR_X1 - THR_X0) * THROTTLE_BAND[0]
        bx1 = THR_X0 + (THR_X1 - THR_X0) * THROTTLE_BAND[1]
        c.create_rectangle(bx0, 228, bx1, 244, fill=theme.BORDER, outline="",
                           tags=("thrband",))
        c.create_rectangle(THR_X0, 230, THR_X0, 242, fill=theme.CYAN,
                           outline="", tags=("thrfill",))
        c.create_text(THR_X1 + 10, 236, anchor="w", text="0%",
                      fill=theme.MUTED, font=font(11, mono=True), tags=("thrpct",))

        # Big live cue
        c.create_text(W / 2, 262, text="DRIFT TO GET LIVE CUES",
                      fill=theme.MUTED, font=font(22, "bold", "italic"),
                      tags=("cue",))

        # Last-drift verdict block: headline / YOU / FIX
        c.create_text(24, 286, anchor="w", text="LAST DRIFT",
                      fill=theme.MUTED, font=font(9, "bold"))
        c.create_text(W - 24, 286, anchor="e", text="",
                      fill=theme.MUTED, font=font(9, "bold"), tags=("events",))
        c.create_text(W / 2, 304, text="—", fill=theme.MUTED,
                      font=font(18, "bold", "italic"), tags=("verdict",))
        # Phase strip: entry / catch / sustain / exit judged ✓ or ✗
        for i, x in enumerate((24, 96, 168, 254)):
            c.create_text(x, 322, anchor="w", text="",
                          font=font(10, "bold"), tags=(f"phase{i}",))
        c.create_text(24, 340, anchor="w", text="", fill=theme.MUTED,
                      font=font(10, "bold"), tags=("didlabel",))
        c.create_text(56, 340, anchor="w", text="", fill=theme.TEXT,
                      font=font(13), tags=("did",))
        c.create_text(24, 358, anchor="w", text="", fill=theme.MUTED,
                      font=font(10, "bold"), tags=("fixlabel",))
        c.create_text(56, 358, anchor="w", text="", fill=theme.CYAN,
                      font=font(13), tags=("fixline",))

        # Footer: status text + small record pill
        c.create_text(PAD + 2, 392, anchor="w",
                      text=f"UDP {self.listener.host}:{self.listener.port}",
                      fill=theme.MUTED, font=font(11, mono=True), tags=("foot",))
        _rrect(c, W - PAD - 74, 380, W - PAD, 404, 12, fill=theme.MAGENTA,
               outline="", tags=("rec", "recbg"))
        c.create_text(W - PAD - 37, 392, text="● REC", fill=theme.TEXT,
                      font=font(11, "bold"), tags=("rec", "reclabel"))

        # Mute pill, left of REC - only when audio is actually usable.
        if self.audio is not None and self.audio.available:
            _rrect(c, W - PAD - 118, 380, W - PAD - 82, 404, 12,
                   fill=theme.PANEL_HI, outline="", tags=("mute", "mutebg"))
            c.create_text(W - PAD - 100, 392, text="♪", fill=theme.CYAN,
                          font=font(11, "bold"), tags=("mute", "mutelabel"))
            self._restyle_mute()

        c.create_text(W / 2, 416, text="", fill=theme.CYAN,
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
        if "mute" in tags and self.audio is not None:
            self.audio.muted = not self.audio.muted
            self._restyle_mute()
            return
        for tag in tags:
            if tag.startswith("chip:"):
                self._select_mode(tag.split(":", 1)[1])
                return
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag:
            dx, dy = self._drag
            self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _select_mode(self, key: str) -> None:
        self.mode = key
        self.coach.set_mode(key)
        for k, (rect, text) in self._chips.items():
            active = k == key
            self.canvas.itemconfigure(
                rect, fill=theme.MAGENTA if active else theme.PANEL_HI)
            self.canvas.itemconfigure(
                text, fill=theme.TEXT if active else theme.MUTED)

    def _restyle_mute(self) -> None:
        """Reflect the current mute state on the pill: a struck note in the
        muted color, a plain note in cyan when audio is live."""
        if self.audio is None or not self.audio.available:
            return
        muted = self.audio.muted
        self.canvas.itemconfigure(
            "mutelabel",
            text="♪ ✕" if muted else "♪",
            fill=theme.MUTED if muted else theme.CYAN,
        )
        self.canvas.itemconfigure("mutebg", fill=theme.PANEL_HI)

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
            extra = {"mode": self.mode}
            if self.wheel is not None and self.wheel.device_name:
                extra["wheel_device"] = self.wheel.device_name
            self.listener.start_recording(self.recordings_dir, extra)
            # confirmation you can catch in peripheral vision - the wheel
            # hotkey means eyes are usually on the road, not the pill
            self._toast(f"● RECORDING ({self.mode.upper()})", seconds=3)

    def _toast(self, text: str, seconds: float = 8.0) -> None:
        self.canvas.itemconfigure("toast", text=text)
        self._toast_until = time.time() + seconds

    # -- refresh loop ----------------------------------------------------------

    def _tick(self) -> None:
        snap = self.listener.snapshot()
        self._blink = not self._blink
        # Wheel hotkey: presses are collected on the wheel thread and
        # consumed here so all tkinter work stays on the UI thread.
        if self.wheel is not None and self.record_button >= 0:
            if self.record_button in self.wheel.consume_presses():
                self._toggle_recording()
        self._update_connection(snap)
        self._update_stats(snap)
        self._update_coach()
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
            c.itemconfigure("hz", text="—")
            return

        latest = snap.latest or {}
        if latest.get("is_race_on"):
            c.itemconfigure("dot", fill=theme.GREEN)
            c.itemconfigure("status", text="RECEIVING", fill=theme.GREEN)
        else:
            c.itemconfigure("dot", fill=theme.AMBER)
            c.itemconfigure("status", text="LINKED · PAUSED", fill=theme.AMBER)
        c.itemconfigure(
            "hz",
            text=f"{snap.pps:.0f} HZ · {snap.packet_size} B",
        )
        if self.wheel is not None and self.wheel.available:
            c.itemconfigure(
                "foot",
                text=f"UDP {self.listener.host}:{self.listener.port} · WHEEL ✓",
            )

    def _update_stats(self, snap: Snapshot) -> None:
        latest = snap.latest if snap.age is not None and snap.age < 1.0 else None
        if not latest or not latest.get("is_race_on"):
            values = ["–"] * 4
        else:
            gear = latest.get("gear")
            gear_txt = {None: "–", 0: "R", 11: "N"}.get(gear, str(gear))
            values = [
                f"{latest['speed_kmh']:.0f}",
                f"{latest['engine_rpm']:.0f}",
                gear_txt,
                f"{latest['drift_angle_deg']:+.1f}",
            ]
        for tag, value in zip(self._stat_tags, values):
            self.canvas.itemconfigure(tag, text=value)

    def _update_coach(self) -> None:
        c = self.canvas
        view = self.coach.view()

        # Angle bar: beta > 0 = left-hand drift = fill to the left
        beta = view.beta_deg
        frac = min(1.0, abs(beta) / BAR_RANGE_DEG)
        half = (BAR_X1 - BAR_X0) / 2 - 2
        width = frac * half
        if beta >= 0:
            x1, x2 = BAR_CX - width, BAR_CX
        else:
            x1, x2 = BAR_CX, BAR_CX + width
        if abs(beta) > 55:
            fill = theme.RED
        elif abs(beta) > 45:
            fill = theme.AMBER
        else:
            fill = theme.CYAN
        self._coords("betafill", x1, 204, x2, 216)
        c.itemconfigure("betafill", fill=fill)

        # Throttle bar: the target band tracks the calibrated sustainable
        # range for the CURRENT angle, so it slides as the drift deepens
        band = view.thr_target
        self._coords("thrband",
                     THR_X0 + (THR_X1 - THR_X0) * band[0], 228,
                     THR_X0 + (THR_X1 - THR_X0) * band[1], 244)
        thr = max(0.0, min(1.0, view.throttle))
        tx = THR_X0 + (THR_X1 - THR_X0) * thr
        if view.in_drift:
            if band[0] <= thr <= band[1]:
                tfill = theme.GREEN
            elif thr > band[1] and abs(beta) > 40:
                tfill = theme.RED
            else:
                tfill = theme.AMBER
        else:
            tfill = theme.CYAN
        self._coords("thrfill", THR_X0, 230, tx, 242)
        c.itemconfigure("thrfill", fill=tfill)
        c.itemconfigure("thrpct", text=f"{thr * 100:.0f}%")

        if view.cue:
            c.itemconfigure("cue", text=view.cue,
                            fill=_LEVEL_COLOR[view.cue_level])
        elif view.in_drift:
            c.itemconfigure("cue", text="…", fill=theme.MUTED)
        else:
            c.itemconfigure("cue", text="DRIFT TO GET LIVE CUES",
                            fill=theme.MUTED)

        if view.verdict:
            c.itemconfigure("verdict", text=view.verdict,
                            fill=_LEVEL_COLOR[view.verdict_level])
            c.itemconfigure("didlabel", text="YOU")
            c.itemconfigure("did", text=view.did.upper())
            c.itemconfigure("fixlabel", text="FIX")
            c.itemconfigure("fixline", text=view.fix.upper())
        report = view.last_report
        if report is not None and report.phases:
            for i, p in enumerate(report.phases[:4]):
                if p.ok is None:
                    mark, color = "—", theme.MUTED
                elif p.ok:
                    mark, color = "✓", theme.GREEN
                else:
                    mark, color = "✗", theme.RED
                c.itemconfigure(f"phase{i}",
                                text=f"{p.name.upper()} {mark}", fill=color)
        c.itemconfigure(
            "events",
            text=f"{view.events} EVENTS" if view.events else "")

    def _update_rec_button(self, snap: Snapshot) -> None:
        c = self.canvas
        if snap.recording:
            mins, secs = divmod(int(snap.rec_elapsed), 60)
            c.itemconfigure("recbg", fill=theme.PANEL_HI,
                            outline=theme.MAGENTA)
            dot = "●" if self._blink else "○"
            c.itemconfigure("reclabel", text=f"{dot} {mins}:{secs:02d}",
                            fill=theme.RED)
        else:
            c.itemconfigure("recbg", fill=theme.MAGENTA, outline="")
            c.itemconfigure("reclabel", text="● REC", fill=theme.TEXT)

    # -- lifecycle ---------------------------------------------------------------

    def run(self) -> None:
        self.root.after(UPDATE_MS, self._tick)
        self.root.mainloop()

    def close(self) -> None:
        if self.wheel is not None:
            self.wheel.stop()
        self.listener.stop()
        self.root.destroy()
