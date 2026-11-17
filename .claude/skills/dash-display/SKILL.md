---
name: dash-display
description: The ESPHome e-paper dash display that replaces the stock fuel gauge for the 47-gallon tank — current hardware (LilyGo e-paper + LoRa board, battery, deep sleep), the wake/fetch/render/sleep cycle, layout (fuel, range, mpg sparkline), stale-data and low-fuel states, and how to add a screen. TRIGGER when editing esphome/dash_display*.yaml, the mpg_trip_spark sensor, or display-related HA helpers. DO NOT trigger for other ESPHome nodes (W-Bus heater, INA228 loads) unless they share a common fragment.
---

# Dash display

The stock gauge doesn't know about the 47-gal tank, and CarPlay can't show
sensor values. This display is the in-cab fuel readout. It must work with
**no CarPlay and no internet**, only the van's Wi-Fi and HA.

## Hardware (v0)

- **Dan's LilyGo T5-family e-paper board with LoRa**, already on hand. A
  better device may replace it later. **Board and panel revision:
  UNVERIFIED.** Read the silkscreen and the panel's FPC label before
  choosing ESPHome's `waveshare_epaper` `model:`. ESPHome's tested 2.13"
  TTGO models are `2.13in-ttgo`, `-b73`, `-b74`, `-b1` and `-dke`, and the
  docs say to try `-b73` or `-b1` depending on revision.
- Battery powered and wireless, with deep sleep between refreshes. LoRa is
  unused in v0.
- B/W e-paper has no colour. The low-fuel "warning colour" is rendered as
  **inverted** (white on black) fuel figures with a heavy border. If a
  red-capable panel replaces it later, use red.
- Mounting: the 52 mm gauge pod fits a round display. This board is a
  rectangle, so v0 mounts on the dash. That's a known compromise.

## Cycle (one wake)

```
wake → Wi-Fi (static IP, fast connect) → HA native API → read sensors
     → render → full or partial refresh → deep sleep
```

- Read `sensor.tank_gal`, `sensor.range_mi`, `sensor.mpg_trip_spark`,
  `binary_sensor.ignition` and bigair's availability. Each is a
  `homeassistant` platform sensor.
- **Sleep length:** about 60 s when ignition is on, about 15 min when off.
  ESPHome `deep_sleep` with `run_duration` as a safety timeout (say 20 s) so
  a failed connection can't drain the battery.
- **OTA:** an HA `input_boolean.dash_display_stay_awake` blocks deep sleep
  (`deep_sleep.prevent`) so firmware can be flashed.
- e-paper `full_update_every` limits ghosting. Use a partial refresh for the
  number and a full refresh about every 10th wake.
- Alternative if API connects are too slow or power-hungry: retained MQTT
  topics from bigair (ESPHome `mqtt` subscribe). Measure first, then decide
  and log it.

## Layout (2.13"-class, landscape)

```
┌──────────────────────────────────────┐
│ 31.4 gal            ▕██████████▏ 67% │
│ 468 mi range                         │
│ ▁▂▃▅▅▆▅▃▄▅▆▆  trip 15.2 mpg           │
│ 14:32                       ● fresh  │
└──────────────────────────────────────┘
```

- **Sparkline:** `sensor.mpg_trip_spark` is a compact string bigair builds.
  It holds up to 32 comma-separated `mpg_30s` buckets for the current trip,
  well under the 255-char state limit. One small fetch, no history API call,
  no parsing beyond `split(',')` in a lambda.
- **Stale:** if a reading is older than 5 min, unavailable, or bigair is
  offline → draw the numbers struck through, with "STALE since hh:mm". Never
  show an old number as if it were current.
- **Low fuel:** `tank_gal` < a configurable threshold (default 8 gal) →
  inverted block.

## Adding a screen

1. Add a `page` to the display's `pages:` list, or a new lambda behind a
   `globals` page index.
2. Add any new data as an HA sensor, published by bigair via MQTT discovery.
   Keep it one compact state, not attributes, to keep wake time short.
3. Step pages with the board's button as a wakeup source (`wakeup_pin`).
4. Validate: `uvx esphome config esphome/dash_display.yaml`, and add a
   render screenshot to the PR.
