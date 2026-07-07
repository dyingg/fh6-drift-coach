# Forza Horizon 6 — Drift Coach

A drift coach driven by Forza's **Data Out** UDP telemetry. An always-on-top
overlay shows live coaching cues while you drift (counter-steer error,
throttle faults, spin warnings) and posts a verdict the moment each drift
ends. Recorded sessions get full post-session report cards.

Pure Python standard library; `pygame` is optional for direct G29 wheel
capture (`pip install pygame`).

## Quick start

```
python main.py
```

The overlay appears in the top-left corner (drag it anywhere, ✕ to close).
It listens on UDP port **5300** by default (`--port` to change).

### In-game setup (Forza Horizon 6)

1. Settings → **HUD and Gameplay** → scroll to **Data Out**
2. Data Out: **ON**
3. IP address: **127.0.0.1** (same PC) or your PC's LAN IP (console)
4. Port: **5300**

The game must run in **windowed or borderless** mode for the overlay to be
visible on top of it.

### Test without the game

```
python scripts/send_fake_telemetry.py
```

This plays a scripted 26-second loop (clean drift → lift snap → pinned-throttle
spin) in the Horizon packet format, exercising every live cue and verdict.
Don't run it while the game is also streaming to the same port — the listener
merges every packet it receives, so the two streams would interleave.

## The coach

Live, on the overlay:

- **Mode chips** — FREE / ROUND / CORNER / S-BEND. Tells the analyzer your
  intent; stored in the session metadata.
- **Drift-angle bar** — fills left/right with the body slip angle; amber past
  45°, red past 55°.
- **Cue line** — one instruction about right now: `MORE COUNTER-STEER +8°`,
  `UNWIND STEERING`, `STAY ON THROTTLE`, `EASE OFF — ABOUT TO SPIN`.
- **Last-drift verdict** — posted the instant a drift ends:
  `SNAP - you lifted at 30°`, `SPIN - throttle pinned past 89°`,
  `HELD 4.2s @ 27° ✓`.

Every live cue is also **spoken** (neural TTS, panned toward the direction
you need to steer for the counter cue), because reading mid-drift is
impossible. Verdicts are read aloud as the drift ends. The ♪ pill next to
REC mutes; `--no-audio` disables entirely. Regenerate the voice pack with
`python scripts/generate_audio.py` (needs `pip install edge-tts` + internet;
clips in `assets/audio/` are committed so users never have to).

Post-session, with per-drift report cards and recurring-fault summary:

```
python scripts/analyze_session.py                    # latest session
python scripts/analyze_session.py recordings/<dir>
```

The same detector/metrics power both paths (`forza_coach/coach/`), so the
recordings you make are exactly what tunes the live coaching.

### How it reads the telemetry

Steering is judged by the **front tire slip angle** (fronts near zero =
counter-steer correct; dragged with the slide = add counter-steer; flipped
past it = unwind). Throttle is judged by **rear slip** and its history
(full lifts mid-drift cause snaps; pinned throttle past ~40° causes spins).
Reaction time comes from cross-correlating steering rate against slip rate.
Conventions were validated against real FH6 captures — see
`forza_coach/coach/conventions.py`.

### Root-cause analysis, not symptom blame

Failed drifts are diagnosed by walking the timeline (including ~2 s of
pre-drift entry context) against the car's **recoverability envelope** and
blaming the FIRST input that left it: entry with throttle pinned, throttle
above what sustains the current angle (an integral, so 85% held too long
counts, not just 100%), steering saturated while fronts drag, or counter
that never came. Past the recoverable angle everything is symptom and is
excluded — and the coach never asks for more counter-steer when you were
already at full lock (live it says `FULL LOCK — EASE THROTTLE` instead).
Report cards print the timeline with the root cause and the point of no
return.

The envelope is **calibrated per car from your own driving**
(`recordings/calibration.json`): every moment the angle holds steady
teaches the sustainable throttle for that angle (the HUD throttle band
slides with your current angle accordingly), and every recovered slide can
raise the known recoverable angle. Feed old sessions in once with
`python scripts/analyze_session.py <session> --calibrate`; live sessions
learn automatically.

## Recording

Hit **START RECORDING** on the overlay. Each session lands in `recordings/`
(gitignored):

| File              | Contents                                                        |
| ----------------- | --------------------------------------------------------------- |
| `raw.fzd`         | Raw datagrams: `FZDUMP01` magic, then `<dH` (timestamp, length) + payload per packet |
| `telemetry.jsonl` | One parsed packet per line with a `t` unix timestamp            |
| `wheel.jsonl`     | (with a wheel connected) raw DirectInput axes/buttons at ~100 Hz |
| `meta.json`       | Session summary: duration, packet count, mode, wheel device     |
| `analysis.json`   | (after `analyze_session.py`) machine-readable event reports     |

`raw.fzd` is the ground truth — if FH6's packet layout turns out to differ
from what we assume, the raw dumps stay fully reusable.

Sanity-check a session with:

```
python scripts/inspect_recording.py            # latest session
python scripts/inspect_recording.py recordings/<session>
```

## Packet format notes

Forza titles send one fixed-size little-endian packet per physics tick:

- **232 B** "Sled" — physics core (RPM, velocity, slip angles, ...)
- **311 B** "Dash" — Sled + dash block (speed, gear, inputs) at offset 232
- **324 B** "Horizon" — Sled + 12 unknown bytes + dash block at offset 244

**FH6 confirmed (real captures):** 324 B Horizon layout at a variable
60–80 Hz (appears frame-rate tied — never assume a fixed rate), velocity in
local space, yaw rate on `ang_vel_y`, gear 11 = neutral. Full conventions in
`forza_coach/coach/conventions.py`. Unknown packet sizes are still accepted
and recorded raw.

## Project layout

```
main.py                          entry point (listener + coach + wheel + overlay)
forza_coach/
  config.py                      defaults (port, recordings dir)
  telemetry/
    packet.py                    Data Out packet parsing
    listener.py                  background UDP listener thread
    recorder.py                  session dumps (raw + jsonl + wheel + meta)
    wheel.py                     direct G29 capture via DirectInput (optional)
  coach/
    conventions.py               validated FH6 axes/signs/units + CoachSample
    events.py                    streaming drift-event detector
    metrics.py                   per-event scoring, faults, verdicts
    live.py                      live cue/verdict state machine for the HUD
  overlay/
    theme.py                     FH6-inspired palette/fonts
    app.py                       tkinter overlay window
scripts/
  send_fake_telemetry.py         scripted drift scenario for testing
  inspect_recording.py           summarize/validate a recorded session
  analyze_session.py             post-session drift report cards
recordings/                      session dumps (gitignored)
```

## Roadmap

- [x] Telemetry in, verified visually, recorded to disk
- [x] Validate real FH6 packet layout against a live capture
- [x] Drift detection + per-event scoring (same engine live and offline)
- [x] Live coaching cues + instant verdicts on the overlay
- [x] Direct G29 wheel capture as a second stream
- [x] Root-cause (causal) failure analysis with per-car envelope calibration
- [ ] Per-car steering-lock/peak-slip calibration for exact degrees
- [ ] Mode-specific scoring (roundabout radius fit, S-bend transition timing)
- [ ] Hand-technique coaching from the wheel stream (self-centering flicks)
- [ ] Session-over-session progress tracking
