---
name: dash-display
description: The dash fuel gauge — a LilyGo T5 ePaper S3 PRO running Dan's eink-screen firmware as a thin client of BigAir's display server (`bigair display`, port 8090). Covers the frame contract shared with HelmLog, the layout in display_render.py, the boat-and-van dual use, flashing gotchas, and how to change what's shown. TRIGGER when editing src/bigair/display_render.py or display_server.py, the display Compose service, DISPLAY_* settings, or when flashing or debugging the LilyGo. DO NOT trigger for Grafana or HA dashboards.
---

# Dash display

The stock gauge doesn't know about the 47-gal tank, and CarPlay can't show
sensors (`docs/carplay.md`). This is the in-cab fuel readout (CLAUDE.md
Decision #29).

## The pieces

```
LilyGo T5 ePaper S3 PRO ──GET /api/display.raw (Bearer key)──▶ bigair display (Pi :8090)
  eink-screen firmware    ◀── 259200 bytes, 4-bit, 960×540 ───   display_render.py (Pillow)
  (~/src/eink-screen)     ◀── X-Refresh-Seconds / X-Full-… ───   display_server.py (stdlib HTTP)
```

- **Device:** LilyGo **T5 ePaper S3 PRO**: ESP32-S3 (16 MB flash, 8 MB
  PSRAM), 4.7" 960×540 16-grey **parallel** e-paper, GT911 touch, LoRa, GPS,
  battery. Verified with esptool on 2026-11-19. ESPHome can't drive this
  panel.
- **Firmware:** `~/src/eink-screen`, a separate repo with its own
  `AGENTS.md`. **Read its Gotchas before touching firmware.** Firmware
  changes are only for transport, power, Wi-Fi or refresh behaviour, never
  layout.
- **Server:** `bigair display`, the `display` Compose service. It serves the
  **same contract as HelmLog's** `/api/display.raw`, so one unit works on the
  boat (HelmLog) and in the van (BigAir). The firmware tries its network list
  in order and locks onto the first server that answers.

## Contract (keep identical to HelmLog's)

| Endpoint | Behaviour |
|---|---|
| `GET /api/display.raw` | Bearer key → exactly **259,200 bytes**. Top nibble per pixel, even-x pixel = low nibble, rows in order, 0x0 black, 0xF white. Headers `X-Refresh-Seconds`, `X-Full-Refresh-Seconds`, `Cache-Control: no-store`. |
| `?screen=<id>` | **Ignored, never 404.** The device remembers HelmLog's last screen (`numbers`), and a 404 would blank the van gauge. |
| `?invert=1` | Polarity escape hatch |
| `GET /api/display.png` | Preview, same key |
| `/api/display/menu*` | Not offered (404). The firmware treats a failed tap as a no-op. |
| `GET /health` | No key |

The key: the firmware compiles in **one** Bearer key (`HELMLOG_API_KEY` in
eink-screen's gitignored `include/config.h`) and sends it to every server.
So `DISPLAY_API_KEY` in the Pi's `deploy/.env` must be that same value.
Never print it or commit it.

## Changing what's shown

1. Edit `src/bigair/display_render.py`, test-first (`tests/test_display_render.py`).
2. Preview locally: `uv run python -c "…render_gauge(…).save('x.png')"`, or on
   the Pi with `curl -H "Authorization: Bearer $KEY" http://bigair-ha.local:8090/api/display.png -o x.png`.
3. Deploy (`git pull && ./deploy/bootstrap.sh`, which rebuilds the display
   image). The device picks it up on its next poll. **No reflash.**

Rules: a stale or missing reading shows dashes or a strike-through plus
`STALE since hh:mm` / `NO FUEL DATA`, never a confident old number. Low fuel
is drawn inverted, because the B/W panel has no colour.

## Data source

Until the fuel model exists (M1), `bigair display` serves the no-data state.
Issue #17 wires it to the fuel model's state on MQTT: tank, range, trip-mpg
buckets and timestamp.

## Flashing (summary; full runbook in eink-screen's `/flash` skill)

```bash
cd ~/src/eink-screen
pio run -t upload --upload-port /dev/cu.usbmodem2101   # ALWAYS pass the port
```

- The native USB port drops after a reset. Recover with BOOT held + tap RST,
  or a replug.
- Serial capture: a reconnecting pyserial reader. Don't toggle DTR/RTS.
- A healthy boot shows `[epd] 960x540`, `[wifi] … joined`, `[http] frame ok`.

## Debug strip (drive testing)

`DISPLAY_DEBUG_FILE` points the display server at the host's
`/run/bigair/debug.json` (`deploy/host/bigair-debug.service`). When it's set
and readable, the trip sparkline is replaced by a power status line:
`PI 5.01V OK | VAN 14.2V | UP 12m`. Data older than 30 s is prefixed
`DEBUG STALE`. A missing or garbled file just means no strip. Setup steps are
in `docs/hardware/pi-setup.md`.

## Live data: the gauge feed (#17)

`bigair run` writes `/feed/gauge.json` every 5 s (Compose volume `gauge-feed`,
not fsynced, display copy only). It holds `ts` (when the service wrote it),
`tank_gal`, `range_mi`, `capacity_gal`, `stale`, `calibrating` and
`mpg_spark` (current-trip buckets, every 2 mi of measured driving; a new trip
starts after a 30 min park). `bigair display` reads it on every frame
(`DISPLAY_GAUGE_FILE`).

The gauge is stale (numbers struck through) if the service says so (moving
with no data) **or** the feed is more than 5 min old (service down). Parked
with the engine off is *not* stale, because the tank is still right then.
While `5E` is unverified, the footer reads `LIVE · CALIBRATING` (Decision #32).
The debug strip, when on, takes the sparkline's place.
