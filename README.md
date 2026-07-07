# Forza Horizon 6 — Drift Coach

Proof of concept for a drift coach driven by Forza's **Data Out** UDP telemetry.
Current scope: an always-on-top overlay that shows whether telemetry is being
received correctly, plus a **record** button that dumps the stream to disk.
Those dumps become the test data for the coaching logic later.

No external dependencies — pure Python standard library (tkinter, sockets).

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

This simulates a drifting car at 60 Hz in the Horizon packet format. The
overlay should flip to RECEIVING, show live speed/RPM/gear/drift angle, and
recording should work exactly as it will with the real game.

## Recording

Hit **START RECORDING** on the overlay. Each session lands in `recordings/`
(gitignored):

| File              | Contents                                                        |
| ----------------- | --------------------------------------------------------------- |
| `raw.fzd`         | Raw datagrams: `FZDUMP01` magic, then `<dH` (timestamp, length) + payload per packet |
| `telemetry.jsonl` | One parsed packet per line with a `t` unix timestamp            |
| `meta.json`       | Session summary: duration, packet count, formats seen           |

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
- **324 B** "Horizon" — Sled + 12 unknown bytes + dash block at offset 244 (FH4/FH5)

FH6 is expected to use the Horizon layout. Unknown packet sizes are still
accepted: the sled portion is parsed and the raw bytes are recorded, and the
overlay displays the observed size/format so we can adapt
`forza_coach/telemetry/packet.py` if needed.

Drift angle shown on the overlay is the body slip angle,
`atan2(local_vel_x, local_vel_z)` — the angle between where the car points
and where it is actually travelling.

## Project layout

```
main.py                          entry point (listener + overlay)
forza_coach/
  config.py                      defaults (port, recordings dir)
  telemetry/
    packet.py                    Data Out packet parsing
    listener.py                  background UDP listener thread
    recorder.py                  session dumps (raw + jsonl + meta)
  overlay/
    theme.py                     FH6-inspired palette/fonts
    app.py                       tkinter overlay window
scripts/
  send_fake_telemetry.py         fake 60 Hz drift stream for testing
  inspect_recording.py           summarize/validate a recorded session
recordings/                      session dumps (gitignored)
```

## Roadmap

- [x] Telemetry in, verified visually, recorded to disk
- [ ] Validate real FH6 packet layout against a live capture
- [ ] Drift detection (slip angle, angle sustain, transitions) on recordings
- [ ] Live coaching cues on the overlay (entry speed, angle, throttle timing)
- [ ] Session scoring and progress tracking
