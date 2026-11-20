# CarPlay via the Home Assistant Companion app — what it can show

Researched 2026-11-16 from companion.home-assistant.io/docs/carplay/ and its
subpages (configuration, controls, assist, troubleshooting). Re-check on each
major Companion release. These pages carry no last-updated date.

## What CarPlay offers

- **Tabs:** Quick access, Areas, Control, Settings, plus custom tabs. The
  vehicle limits how many tabs are visible.
- **Layout:** List, or Grid on iOS 26+.
- **Supported entity domains:** `automation`, `button`, `cover`, `fan`,
  `humidifier`, `input_boolean`, `input_button`, `light`, `lock`, `scene`,
  `script`, `switch`, `valve`, plus control screens for `climate` and
  `vacuum`.
- **State text:** Areas and Control show a state line for supported domains
  (e.g. "Open", "Heat · 21.5°").
- **Assist** (iOS 26.4+): conversational queries from the car.
- **Requirements:** iPhone with iOS 16.4+. The phone, not the car, must reach
  HA. Our in-van hotspot handles that.

## What it can't do (for us)

- **No `sensor` domain.** A `sensor.tank_gal` or `sensor.range_mi` cannot be
  listed, so fuel level and range can't be shown as live values. This is an
  inference from the domain list; the docs never mention sensors.
- No charts, gauges or custom UI.
- **Notifications on CarPlay are not documented** in the CarPlay pages.
  Assume the fill-up prompt is answered on the phone. That's fine: the
  prompt fires while parked at the pump.

## How we use it

| Need | CarPlay mechanism | Status |
|---|---|---|
| Log a fill-up manually | `script.log_fill_up` in Quick access → actionable notification on the phone (reply under Full/Partial with `22.4 4.19`) | Built (#12). Add it under Companion app → Settings → CarPlay → Quick access |
| Heater on/off | `switch`/`climate` for the Webasto (W-Bus node) | M2 |
| "What's my range?" | Assist custom sentence answering from `sensor.range_mi` | UNVERIFIED: needs Assist working offline in the van (local STT/TTS) |
| Fuel/range at a glance | Not possible in CarPlay. Use the **dash display** (`/dash-display`). | — |

**Rejected hack:** exposing tank gallons as a fake `climate` entity's
current temperature so it appears as "Heat · 23.4°". It's misleading and
fragile, and it would put a fuel number in front of the driver labelled as
something else.
