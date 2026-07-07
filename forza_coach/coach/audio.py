"""Spoken coaching - the cues and verdicts read aloud, panned to the slide.

Reading a cue off the HUD costs the exact half-second of attention you can't
spare mid-drift, so the same instruction is spoken instead. The counter-steer
cue is panned toward the direction you need to steer, so the ear points the
hands before the words land.

Clips are produced out-of-band into assets/audio/ (see the manifest contract);
this class only discovers and plays them. Everything degrades to a silent
no-op when pygame, an audio device, or the clips themselves are missing - the
overlay and coach never depend on audio being present.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# Live-cue and verdict keys the coach can ask for; anything outside this set
# is ignored so a stray key can never raise from the telemetry thread.
CUE_KEYS = ("counter", "unwind", "throttle", "ease_off", "back_off")
VERDICT_KEYS = ("snap_lifted", "snap_over", "snap_commit",
                "spin_throttle", "spin_counter", "faded", "held")

CUE_GAP_S = 1.5        # minimum spacing between cue playbacks
DEFAULT_VOLUME = 0.9


class AudioCoach:
    """Plays coaching clips on dedicated channels, safe to call per-packet.

    Cues share one channel (only the newest matters, and a danger cue may cut
    off whatever is speaking); verdicts get their own channel so the post-drift
    read isn't swallowed by a lingering cue and vice versa.
    """

    def __init__(self, assets_dir: Path, enabled: bool = True):
        self.available = False
        self._muted = False
        self.volume = DEFAULT_VOLUME
        self._clips: dict[str, object] = {}
        self._cue_channel = None
        self._verdict_channel = None
        self._last_cue_t = 0.0

        if not enabled:
            return

        pygame = _import_pygame()
        if pygame is None:
            return
        try:
            pygame.mixer.init(frequency=44100)
        except Exception:
            return  # no audio device (headless / no output)

        self._pygame = pygame
        self._clips = _load_clips(pygame, Path(assets_dir))
        if not self._clips:
            pygame.mixer.quit()
            return

        # Reserve two channels so cue and verdict playback never steal each
        # other's voice the way pygame's default channel pool would.
        pygame.mixer.set_num_channels(max(8, pygame.mixer.get_num_channels()))
        self._cue_channel = pygame.mixer.Channel(0)
        self._verdict_channel = pygame.mixer.Channel(1)
        self.available = True

    # -- called from the telemetry thread -------------------------------------

    def on_cue(self, key: str | None, level: str, pan: float) -> None:
        """Speak a live cue. Rate-limited to CUE_GAP_S unless it's a danger
        cue, which interrupts whatever is currently playing."""
        if not self.available or self.muted or key is None:
            return
        clip = self._clips.get(key)
        if clip is None:
            return

        # time.monotonic, not pygame.time.get_ticks: get_ticks reads 0 until
        # pygame.init() runs, which the mixer alone doesn't guarantee - the
        # rate limiter would then swallow every non-danger cue.
        now = time.monotonic()
        danger = level == "danger"
        if not danger and now - self._last_cue_t < CUE_GAP_S:
            return
        if danger:
            self._cue_channel.stop()

        self._last_cue_t = now
        self._cue_channel.play(clip)
        left, right = _pan_gains(pan, self.volume)
        self._cue_channel.set_volume(left, right)

    def on_verdict(self, key: str) -> None:
        """Speak the closed-event verdict on its own channel; a cue may still
        be finishing and that overlap is fine - they carry different news."""
        if not self.available or self.muted:
            return
        clip = self._clips.get(key)
        if clip is None:
            return
        self._verdict_channel.play(clip)
        self._verdict_channel.set_volume(self.volume, self.volume)

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        """Muting also silences whatever is mid-playback, so the toggle
        takes effect instantly rather than after the current clip."""
        self._muted = value
        if value and self.available:
            self._cue_channel.stop()
            self._verdict_channel.stop()


def _pan_gains(pan: float, volume: float) -> tuple[float, float]:
    """Constant-ish stereo split for pan in [-1, 1] scaled by master volume.
    pan=-1 -> full left, +1 -> full right, 0 -> centered."""
    pan = max(-1.0, min(1.0, pan))
    left = volume * min(1.0, 1.0 - pan)
    right = volume * min(1.0, 1.0 + pan)
    return left, right


def _import_pygame():
    """Import pygame quietly, or None if it isn't installed. Mirrors the
    guard in telemetry/wheel.py: hide the support banner and the deprecation
    warning pygame emits on import."""
    import os

    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pygame's pkg_resources noise
            import pygame
        return pygame
    except ImportError:
        return None


def _load_clips(pygame, assets_dir: Path) -> dict[str, object]:
    """Discover and preload every known clip. The manifest names the file for
    each key when present; otherwise the key is globbed directly (<key>.*).
    A key that fails to load is simply absent - callers no-op on a miss."""
    if not assets_dir.is_dir():
        return {}

    files: dict[str, Path] = {}
    manifest = assets_dir / "manifest.json"
    if manifest.is_file():
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
            for key, meta in entries.items():
                name = meta.get("file") if isinstance(meta, dict) else None
                if name and (assets_dir / name).is_file():
                    files[key] = assets_dir / name
        except (ValueError, OSError):
            pass  # unreadable manifest - fall back to globbing

    for key in (*CUE_KEYS, *VERDICT_KEYS):
        if key not in files:
            hits = sorted(assets_dir.glob(f"{key}.*"))
            hits = [p for p in hits if p.suffix.lower() in (".wav", ".mp3", ".ogg")]
            if hits:
                files[key] = hits[0]

    clips: dict[str, object] = {}
    for key, path in files.items():
        try:
            clips[key] = pygame.mixer.Sound(str(path))
        except Exception:
            pass  # unsupported codec / corrupt file - skip this one clip
    return clips
