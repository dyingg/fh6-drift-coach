#!/usr/bin/env python3
"""Generate the neural-TTS coaching voice pack for the Forza drift coach.

Produces 12 short WAV clips (5 live cues + 7 post-drift verdicts) using
Microsoft Edge's neural TTS (edge-tts), trims the trailing/leading silence
that edge-tts bakes into its output so the cues fire snappily mid-drift, and
verifies every clip loads through ``pygame.mixer.Sound``.

Re-runnable: ``python scripts/generate_audio.py`` regenerates everything.

Pipeline per clip:
  1. edge-tts synthesizes an mp3 in a temp dir.
  2. pygame decodes it to PCM samples (pygame.sndarray).
  3. Leading/trailing silence is trimmed (small guard pad kept).
  4. Samples are written to ``assets/audio/<key>.wav`` via stdlib ``wave``.
  5. Each WAV is re-loaded with pygame.mixer.Sound and its duration checked.

WAV is chosen as the delivery format because it loads in pygame with no
external decoder (no ffmpeg on this machine) and trimming requires PCM anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

# Keep pygame quiet before it is imported anywhere.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# --- Clip contract -----------------------------------------------------------
# EXACT keys. Another agent codes against these; do not rename.
CLIPS: dict[str, str] = {
    # Live cues (punchy, <= ~1.2 s)
    "counter": "Counter!",
    "unwind": "Unwind",
    "throttle": "Throttle!",
    "ease_off": "Ease off",
    "back_off": "Back off!",
    # Post-drift verdicts (<= ~2 s)
    "snap_lifted": "Snap — you lifted",
    "snap_over": "Snap — over-corrected",
    "snap_commit": "Snap — commit more",
    "spin_throttle": "Spin — too much throttle",
    "spin_counter": "Spin — counter faster",
    "faded": "Faded — stay on it",
    "held": "Held it!",
}

# --- Defaults ----------------------------------------------------------------
# en-US-GuyNeural: energetic, clear American male -> best "urgent racing
# spotter" fit and the tightest timing of the candidates tested. +25% rate
# keeps cues snappy without slurring.
DEFAULT_VOICE = "en-US-GuyNeural"
DEFAULT_RATE = "+25%"

SAMPLE_RATE = 44100          # pygame.mixer default
MIN_DURATION_S = 0.2         # contract: every clip must exceed this
SILENCE_FLOOR = 0.02         # amplitude fraction (of peak) treated as silence
GUARD_MS = 40                # silence kept either side of trimmed speech

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = REPO_ROOT / "assets" / "audio"
MANIFEST_PATH = AUDIO_DIR / "manifest.json"


async def _synthesize_mp3(text: str, voice: str, rate: str, out_path: Path) -> None:
    """Render ``text`` to an mp3 at ``out_path`` via edge-tts."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(str(out_path))


def _init_mixer() -> None:
    import pygame

    if pygame.mixer.get_init() is None:
        pygame.mixer.init(frequency=SAMPLE_RATE)


def _trim_silence(samples, sample_rate: int):
    """Trim leading/trailing near-silence, keeping a short guard pad.

    ``samples`` is an int16 numpy array shaped (n,) or (n, channels).
    Returns a contiguous int16 array of the same channel layout.
    """
    import numpy as np

    if samples.ndim == 1:
        amplitude = np.abs(samples.astype(np.int32))
    else:
        amplitude = np.abs(samples.astype(np.int32)).max(axis=1)

    peak = int(amplitude.max()) if amplitude.size else 0
    if peak == 0:
        return samples  # all silence; leave as-is (will fail duration check)

    threshold = peak * SILENCE_FLOOR
    loud = np.where(amplitude > threshold)[0]
    if loud.size == 0:
        return samples

    guard = int(sample_rate * GUARD_MS / 1000)
    start = max(0, int(loud[0]) - guard)
    end = min(len(samples), int(loud[-1]) + 1 + guard)
    return np.ascontiguousarray(samples[start:end])


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    """Write an int16 numpy array (mono or stereo) to a WAV file."""
    import numpy as np

    samples = np.ascontiguousarray(samples.astype(np.int16))
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # int16
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def _mp3_to_trimmed_wav(mp3_path: Path, wav_path: Path) -> None:
    """Decode an mp3 with pygame, trim silence, write a WAV."""
    import pygame
    import pygame.sndarray

    _init_mixer()
    sound = pygame.mixer.Sound(str(mp3_path))
    samples = pygame.sndarray.array(sound)  # int16, (n,) or (n, ch)
    sample_rate = pygame.mixer.get_init()[0]
    trimmed = _trim_silence(samples, sample_rate)
    _write_wav(wav_path, trimmed, sample_rate)


def generate(voice: str, rate: str) -> dict[str, dict[str, str]]:
    """Generate every clip. Returns the manifest mapping."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, str]] = {}

    with tempfile.TemporaryDirectory(prefix="forza_tts_") as tmp:
        tmp_dir = Path(tmp)
        for key, text in CLIPS.items():
            mp3_tmp = tmp_dir / f"{key}.mp3"
            asyncio.run(_synthesize_mp3(text, voice, rate, mp3_tmp))

            wav_out = AUDIO_DIR / f"{key}.wav"
            _mp3_to_trimmed_wav(mp3_tmp, wav_out)

            manifest[key] = {"file": wav_out.name, "text": text}
            print(f"  generated {key:<14} -> {wav_out.name}")

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  wrote manifest -> {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return manifest


def verify(manifest: dict[str, dict[str, str]]) -> bool:
    """Load every clip via pygame.mixer.Sound and print a duration table."""
    import pygame

    _init_mixer()
    print("\nVerification (pygame.mixer.Sound):")
    print(f"  {'key':<14} {'file':<20} {'duration':>10}   status")
    print(f"  {'-'*14} {'-'*20} {'-'*10}   {'-'*6}")

    all_ok = True
    for key, entry in manifest.items():
        path = AUDIO_DIR / entry["file"]
        try:
            sound = pygame.mixer.Sound(str(path))
            duration = sound.get_length()
            ok = duration > MIN_DURATION_S
            status = "OK" if ok else f"TOO SHORT (<= {MIN_DURATION_S}s)"
        except Exception as exc:  # noqa: BLE001 - report any load failure
            duration = 0.0
            ok = False
            status = f"LOAD FAILED: {type(exc).__name__}: {exc}"
        all_ok = all_ok and ok
        print(f"  {key:<14} {entry['file']:<20} {duration:>9.3f}s   {status}")

    print()
    if all_ok:
        print(f"All {len(manifest)} clips passed the pygame.mixer.Sound load check.")
    else:
        print("One or more clips FAILED the load check.")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the Forza drift-coach TTS voice pack.",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"edge-tts voice (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--rate",
        default=DEFAULT_RATE,
        help=f"edge-tts speech rate, e.g. +25%% (default: {DEFAULT_RATE})",
    )
    args = parser.parse_args(argv)

    print(f"Voice: {args.voice}   Rate: {args.rate}")
    print(f"Output: {AUDIO_DIR}\n")
    print(f"Generating {len(CLIPS)} clips:")

    manifest = generate(args.voice, args.rate)
    ok = verify(manifest)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
