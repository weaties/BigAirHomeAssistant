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
| `?screen=<id>` | Picks the screen: `fuel` (default), `trip`, `economy`, `van` (`SCREENS` in `display_render.py`). **Unknown ids get the fuel screen, never 404.** The device remembers the last screen across networks, so on the van it may send HelmLog's `numbers`, and a 404 would blank the gauge. |
| `?invert=1` | Polarity escape hatch |
| `GET /api/display.png` | Preview, same key, same `?screen=` |
| `GET /api/display/menu` | Same key → JSON `[{x, y, w, h, id, name}]` tap regions in 960×540 space (`menu_regions()`). |
| `GET /api/display/menu.raw` | Same key → the menu frame (`render_menu()`); its tiles must match the regions. |
| `GET /health` | No key |

The key: the firmware compiles in **one** Bearer key (`HELMLOG_API_KEY` in
eink-screen's gitignored `include/config.h`) and sends it to every server.
So `DISPLAY_API_KEY` in the Pi's `deploy/.env` must be that same value.
Never print it or commit it.

## Touch screens (#62)

A tap on the panel makes the firmware fetch the menu (regions plus frame),
draw it, and wait. A tap inside a tile picks that screen; outside, or a
timeout, cancels. The firmware saves the choice in NVS and sends it as
`?screen=` from then on. All of that is firmware behaviour from HelmLog. BigAir
only serves the menu and renders by id. To add a screen: a `render_<id>`
function, a `SCREENS` entry, a `_SCREEN_HINTS` line, and the dispatch in
`render_screen`. The menu is a 2×2 grid, so a fifth screen means changing the
layout in `menu_regions()`.

## Browser preview (tailnet only, Decision #37)

`bigair display --preview-port 8091` also runs a keyless, read-only preview:
a page at `/` with a button per screen and the touch menu (clicking a tile
works like a tap), PNGs at `/api/display.png?screen=<id>`, and no `.raw`
frames. Compose publishes it on `127.0.0.1` only, and bootstrap runs
`tailscale serve --bg --http=8091 http://127.0.0.1:8091`, so it's reachable at
`http://bigair-ha.<tailnet>.ts.net:8091/` (or `http://bigair-ha:8091/` with
MagicDNS) from tailnet devices and never on the van
LAN. Serve matches on the Host header, so the bare tailnet IP gets a 404:
use the name. Never publish 8091 on `0.0.0.0`.

## Changing what's shown

1. Edit `src/bigair/display_render.py`, test-first (`tests/test_display_render.py`,
   `tests/test_display_screens.py`).
2. Preview locally: `uv run bigair display --preview-port 8091` with
   `DISPLAY_API_KEY=x` and open http://localhost:8091/, or use the Pi's tailnet
   preview.
3. Deploy (`git pull && ./deploy/bootstrap.sh`, which rebuilds the display
   image). The device picks it up on its next poll. **No reflash.**

Legibility: nothing smaller than 33 px. The panel sits on the dash and is read
at a glance (Dan asked for 1.5× the original 22–32 px labels). The tank level is
an analog arc (E at 9 o'clock, F at 3 o'clock, `needle_angle()`) with no needle
when the level is unknown. The footer's screen name is drawn only when it fits
beside the status.

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

## Van screen and the host debug feed

`DISPLAY_DEBUG_FILE` points the display server at the host's
`/run/bigair/debug.json` (`deploy/host/bigair-debug.service`). Its Pi supply
voltage, under-voltage flags and uptime fill the Van screen's `Pi supply` and
`Pi uptime` rows (`STALE since hh:mm` after 30 s, `--` without the file). It
used to replace the fuel screen's sparkline as a debug strip. Since #62 the
fuel screen always shows the sparkline. The Van screen's engine row says
`NO DATA since hh:mm` when the last WiCAN sample is more than 60 s old, and the
GPS row says `NO FIX since` after 30 s, so an old `ON` is never shown. Setup
steps are in `docs/hardware/pi-setup.md`.

## Live data: the gauge feed (#17)

`bigair run` writes `/feed/gauge.json` every 5 s (Compose volume `gauge-feed`,
not fsynced, display copy only). It holds `ts` (when the service wrote it),
`tank_gal`, `range_mi`, `capacity_gal`, `stale`, `calibrating` and
`mpg_spark` (current-trip buckets, every 2 mi of measured driving; a new trip
starts after a 30 min park), `van_battery_v`, and the economy numbers
`mpg_rolling`, `mpg_instant_30s`, `mpg_since_full` and `trip_mpg`. For the
Trip and Van screens (#62) it also holds `trip_mi`, `trip_gal`, `trip_start`,
`obd_ts`, `ignition`, `gps_ts`, `gps_hdop`, `gps_lat` and `gps_lon` (shown to 3 decimals on the Van screen); the renderer judges freshness
from those times.
`mpg_rolling` is `null` until the 150 mi window holds at least 50 measured
miles: below that the model's value is the configured prior, which must never
be drawn as a measurement. The dash shows `--` and "avg after 50 mi" instead. `bigair display` reads it on every frame
(`DISPLAY_GAUGE_FILE`).

The gauge is stale (numbers struck through) if the service says so (moving
with no data) **or** the feed is more than 5 min old (service down). Parked
with the engine off is *not* stale, because the tank is still right then.
While `5E` is unverified, the footer reads `LIVE · CALIBRATING` (Decision #32).
Every screen shares that footer (clock, screen name, fuel status). Trip and
Economy numbers go grey when the feed is stale.
