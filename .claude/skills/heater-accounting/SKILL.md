---
name: heater-accounting
description: Webasto Air Top diesel heater fuel accounting — W-Bus interface from an ESP32, fallbacks (current sensor, exhaust temperature), converting pump pulses or run time to gallons, subtracting heater fuel from the tank, and calibrating burn rates from fill-ups. TRIGGER when working on src/bigair/heater.py, esphome/webasto*.yaml, the heater measurement, or heater terms in the tank equation. DO NOT trigger for engine fuel rate (use /fuel-model) or general electrical loads (use /electrical).
---

# Heater accounting (Webasto Air Top)

The heater draws from a fuel tap on the main tank. Engine fuel rate from
OBD doesn't include it, so `tank_gal` must subtract `heater_used_gal`
(`/fuel-model`). Critical tier.

## Unknowns to resolve first

| Item | Status | How |
|---|---|---|
| Variant (e.g. Air Top 2000 STC, Evo 40) and part number | UNVERIFIED | Photo of the heater's label → `docs/pid-register.md` Devices |
| Burn rate per power level | UNVERIFIED | That variant's datasheet only. Never another variant's numbers. |
| Fuel pump volume per stroke | UNVERIFIED | Datasheet, or a bench measurement (pump N strokes into a graduated cylinder) |
| W-Bus messages for status, power and pump frequency | UNVERIFIED | Community W-Bus docs and ESPHome projects as leads; confirm on this heater |

## Measurement options (preferred first)

1. **W-Bus from an ESP32 (ESPHome).** W-Bus is a single-wire, K-line-style
   bus. The ESP32 UART needs a K-line/LIN transceiver, not a direct
   connection. Read on/off, operating state, power level and ideally the
   **fuel pump frequency**. Publish straight to MQTT with ESPHome's `mqtt`
   component (Decision #23), so heater fuel keeps counting even if HA is down.
   Read-only polling first. Commanding the heater
   (on/off from HA or CarPlay) comes later, because a write that goes wrong
   on a combustion appliance is a safety issue.
2. **Current sensor on the heater feed** (an INA228 channel, `/electrical`).
   Glow-plug start current, then fan current, which scales with power level.
   Map it to power level by observing alongside the heater controller
   display.
3. **Exhaust temperature sensor:** run time only, no power level. Weakest.

## Conversion

```
pulses:   heater_gph = pump_hz × ml_per_stroke × 3600 ÷ 3785.411784
runtime:  heater_gph = datasheet_burn_rate_gph[power_level]   (or interpolated by %)
used_gal += heater_gph × Δt_h   (trapezoidal, same gap rules as /fuel-model)
```

- Startup and purge phases burn differently. With pulses that's automatic;
  with runtime, model startup as its own datasheet phase if one is given,
  otherwise count it at max level.
- Log to `heater` (raw bucket), tagged with `method` (wbus_pulses,
  runtime_datasheet, current): `running`, `power_level`, `pump_hz`,
  `fuel_gph`, `used_gal`. Heater fuel is its own series and never mixed into
  `engine_used_gal`.

## Calibration from fill-ups

The heater runs mostly while parked, so a full-to-full interval contains
engine fuel E and heater fuel H, both raw model values. Over the last N
intervals:

```
pump_gal ≈ k_e·E + k_h·H        (least squares, N ≥ 4, require Σ H ≥ 2 gal)
```

- Until there's enough heater-heavy data, hold `k_h = 1.0` and solve for
  `k_e` alone (`/fuel-model`).
- Reject `k_h` outside 0.5–2.0 and flag it. That points to a wrong
  variant, datasheet or pulse volume, not just drift.
- A winter trip parked with the heater on (and engine off) is the best
  calibration interval. Note it in the fill-up record.

## Edge cases

- Heater running with the ignition off: the WiCAN is asleep but the ESP32 is
  not, so the tank still decreases while parked. The fill detector must not
  read a heater-driven level drop as anything (`/fill-up-events`).
- ESP32 offline: mark `heater_used_gal` stale and show a stale tank level
  rather than silently stopping the subtraction.
