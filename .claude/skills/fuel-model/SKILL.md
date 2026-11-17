---
name: fuel-model
description: The tank, gallons-used, range and mpg maths for the 47-gallon tank — integration rules, fuel-rate source fallback order for a diesel, rolling and instant mpg definitions, and calibration against pump receipts. TRIGGER when working on src/bigair/fuel_model.py, tests/test_fuel_model*.py, the drive_synthetic fixture, range/mpg sensors in Home Assistant, or anything that changes how gallons are counted. DO NOT trigger for fill-up detection or prompting (use /fill-up-events) or heater burn rates (use /heater-accounting), except where they feed the tank equation.
---

# Fuel model

Critical tier. TDD only, against `tests/fixtures/drive_synthetic/`. If you
change a definition here, change `expected.json` (hand-derived) and the
generator in the same PR, and include a worked example in the PR body.

## The equation

```
tank_gal = gal_after_last_fill − engine_used_gal − heater_used_gal
           (both "used" terms counted since the last fill event)

gal_after_last_fill = capacity_gal                    if the last fill was "full"
                    = tank_before_gal + fill_gallons  if "partial"
range_mi = max(tank_gal, 0) × mpg_rolling
```

- `capacity_gal` = **47.0 nominal**. Usable capacity is UNVERIFIED. Tanks
  rarely match the label, and pump shutoff varies about ±0.5 gal.
  Calibration (below) estimates it over time, so keep it a config value.
- Partial fills chain error forward until the next full fill. Show the
  "since last full" age in HA.
- `tank_gal` is never displayed confidently when its inputs are stale. If
  the fuel-rate source has been missing for more than 60 s with the ignition
  on, publish `stale=true`.

## Fuel-rate sources (diesel), in order

Use the first one that is **VERIFIED** in `docs/pid-register.md`. Tag the
`fuel` measurement with `method`.

| # | `method` | Formula | Notes |
|---|---|---|---|
| 1 | `fuel_rate` | PID `5E` L/h → gph = L/h ÷ 3.785411784 | Best if present. Absent on EU W906 scans. |
| 2 | `injection_qty` | mg/stroke × rpm/2 × cylinders × 60 ÷ ρ | Needs a Mercedes DID. None known for OM642. |
| 3 | `maf_faer` | fuel_g_s = MAF_g_s × φ ÷ AFR_st; gph = fuel_g_s × 3600 ÷ (ρ_g_L × 3.785411784) | φ = FAER (PID `24`). Diesel AFR_st ≈ 14.5. |
| 4 | `maf_fixed` | as #3 with a fixed φ | **Poor.** Diesels run lean at a load-dependent AFR. Last resort, and only with calibration. |

Assumed constants, documented and corrected by calibration: ULSD density
ρ = 840 g/L (seasonal range is about 820–850), and AFR_st = 14.5. They
belong in one constants block, never as magic numbers.

## Integration rules

- **Trapezoidal** between consecutive samples when Δt ≤ 5 s.
- Gap > 5 s with the ignition on: don't bridge with the last rate. Estimate
  `gap_gal = gap_distance_mi ÷ mpg_rolling` (GPS distance), add it to
  `engine_used_gal`, and count it in a separate `gap_gal` field so its share
  is visible.
- Ignition off → engine rate 0. Heater fuel continues (`/heater-accounting`).
- Negative or absurd rates (> 40 gph) → drop the sample and log a warning.
- State (`gal_after_last_fill`, `engine_used_gal`, `heater_used_gal`, last
  fill metadata) goes to a local state file with an atomic write (temp file,
  `fsync`, rename): at least every 10 s while driving and immediately on every
  fill. At most 10 s of fuel is lost on a power cut. Never rely on retained
  MQTT for this. Mosquitto saves retained messages to disk on an interval, so
  a power cut can roll the tank back by the whole interval.

## mpg definitions

- **`mpg_instant_30s`**: Σdistance ÷ Σengine fuel over the trailing 30 s.
  It is `None` when distance < 0.05 mi in the window (idle or parked).
  Capped at 50.0, because overrun fuel cut gives near-zero fuel. Used for
  the Geomap and the throttle-vs-mpg views.
- **`mpg_rolling`**: distance ÷ engine fuel over the most recent **150 mi of
  driving**, a distance window that does **not** reset at fills. Below 50 mi
  of history (fresh install), use the latest full-to-full mpg, else a
  configured prior. Heater fuel is excluded because it doesn't move the van.
- **`mpg_since_full`** (true mpg, on a full fill only): miles since the
  previous full fill ÷ Σ pump gallons of every fill in that interval,
  partials included. It's written on the `fill_up` point.

## Calibration against pump receipts

For each full-to-full interval of at least 100 mi:

```
r = pump_gal_total ÷ (engine_used_gal_raw + heater_used_gal_raw)
```

- Keep `k_method` = the median of the last 5 `r` values for the active
  method. Apply it as `engine_used_gal = raw × k_method`.
- Alarm if `r` is outside 0.85–1.15 for the `fuel_rate` method, which
  suggests the PID is wrong or the method changed. MAF methods can need a
  larger k at first.
- When the heater ran for a meaningful share of an interval, solve
  `pump = k_e·E + k_h·H` jointly over recent intervals (`/heater-accounting`).
- Historical fills (`source=historical`) give a pre-install baseline mpg. Use
  it as the initial prior, not as calibration of the live sensors.

## Worked example (matches the fixture)

Last full fill puts the tank at 47.0 gal. Drive 105 mi using 7.05 gal:
idle 0.05, city 1.2, highway 4.2, idle 0.1, rural 1.5.

- `tank_gal` = 39.95, `mpg_rolling` = 105 ÷ 7.05 = 14.894, `range_mi` ≈ 595.0
- Full fill of 7.05 gal → `mpg_since_full` = 14.894, tank back to 47.0
- Drive 10 mi on 0.7 gal → `tank_gal` = 46.3, `mpg_rolling` (150 mi window
  holds all 115 mi) = 115 ÷ 7.75 = 14.839, `range_mi` ≈ 687.0
