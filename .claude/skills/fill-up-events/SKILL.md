---
name: fill-up-events
description: Fill-up auto-detection (fuel-level jump across a park, confirmed after ignition on), the Companion app prompt flow for gallons / price / full-or-partial, re-prompting, the manual "log fill-up" fallback, and exactly what is recorded in the fill_up measurement. TRIGGER when working on src/bigair/fill_detect.py, fill-up tests, homeassistant/packages/fill_up*.yaml, the fill-up MQTT topics, or the log-fill-up script. DO NOT trigger for tank/range maths (use /fuel-model) or the historical CSV/photo import (use /historical-import).
---

# Fill-up events

Critical tier. Detection is a pure state machine in `fill_detect.py` that
takes `signals.py` dataclasses. Notifications and user input live in HA,
and parsing lives in Python. HA templates never decide anything.

## Prerequisite: a level signal

Auto-detection needs a **VERIFIED** tank-level source. The van has **no
aftermarket sender: the stock sender was moved to the 47-gal tank**
(Decision #31). So the candidates are that sender's reading via OBD `2F`, a
Mercedes DID or the cluster, or an ESP32 ADC on the sender wire. None is
verified yet (`docs/pid-register.md`, #4).

The stock sender was made for a different tank shape, so its reading is
used **only as a delta detector, never as the tank reading**. Map it through
a calibration table learned from fill-ups (sender reading before and after
vs pump gallons and model gallons), and expect it to be non-linear and to
saturate near full or empty. Without a level source, only the manual flow
is available. That must work end-to-end on its own.

## Detection state machine

```
DRIVING ──(ignition off, or speed 0 for ≥ 60 s)──► PARKED
   PARKED: level_before = median of the last 30 s of level samples before the stop
PARKED ──(ignition on)──► SETTLING
   SETTLING: speed 0 for 45 s (slosh settles), level_after = median of the window
SETTLING ──(Δ = level_after − level_before ≥ max(2.0 gal, 5 % capacity))──► CANDIDATE
SETTLING ──(Δ below threshold, or vehicle moves before settling)──► DRIVING
```

- Level samples are converted to gallon-equivalents with the sender's
  calibration table before comparing (`FillDetectorConfig.level_table`:
  sender percent → fraction of capacity, piecewise linear, linear until
  learned). Thresholds are in gallons and are config values.
- `level_after` is the median of the last 30 s of the settle window, so
  slosh in the first seconds after the engine starts is ignored.
- A stop with the engine running (speed 0 for 60 s) also parks. Settling
  then starts at the next ignition on. Driving off from a running stop
  goes back to DRIVING with no check.
- Ignition off during SETTLING returns to PARKED and keeps the original
  `level_before`, so cycling the ignition at the pump gives one candidate.
- **Known gap:** driving off within 45 s of ignition on misses the fill.
  The manual flow covers it. Revisit with on-van data: if the sender reads
  steady at key-on, `settle_s` can shrink.
- The check happens **after ignition on** because the WiCAN and ECU are
  asleep while parked with the ignition off. The comparison is "last
  settled level before off" vs "first settled level after on".
- A candidate carries a new `fill_id` (UUID4), the estimated gallons from
  Δ, `odometer_mi`, GPS `lat`/`lon` (last fix while parked) and a timestamp.
- Published on `bigair/fill_up/candidate`.

## Prompt flow (HA, Companion app)

1. An automation on `bigair/fill_up/candidate` sends an actionable
   notification ("Fill-up detected (~22 gal)"), carrying the `fill_id` in
   the action data:
   - **Full**: text input `"<gallons> <price/gal>"`, e.g. `22.4 4.19`
   - **Partial**: same text input
   - **Later**: re-prompt
   - **Not a fill**: discard; the candidate is logged with `status=skipped`
   - Tapping the notification body opens the **fill-up form** view
     (`input_number` gallons and price, `input_select` full/partial, a submit
     `script`) for when typing in a notification is awkward.
2. The response goes to MQTT `bigair/fill_up/response` as `{fill_id, kind,
   text | gallons, price_per_gal}`. Python parses and validates it: gallons
   within 0.5–60, price within 0.5–15. If it's invalid, re-prompt with the
   reason.
3. **Re-prompt:** no answer within 10 min of ignition on, or "Later" →
   prompt again at the next park or ignition on, up to 3 times. Then
   `status=skipped`, plus a persistent HA notification so it can still be
   completed from the form.
4. CarPlay can't show these prompts (`docs/carplay.md`). Answer on the phone
   while parked.

## Manual logging (#12, built)

- **`script.log_fill_up`**, exposed in CarPlay Quick access and on the
  dashboard, sends an actionable notification. Its Full and Partial actions
  take a text reply (`22.4 4.19` = gallons, then price; the price is optional).
  The action identifier carries the `fill_id` (`BIGAIR_FILL_FULL::manual-<ts>`).
  An automation forwards the reply unchanged to `bigair/fill_up/response`.
- **`script.submit_fill_up_form`** sends the dashboard form (helpers
  `input_number.fill_up_gallons`, `input_number.fill_up_price_per_gal` and
  `input_select.fill_up_type`) to the same topic, then clears it.
- **bigair** (`fill_input.py`, `FuelService.handle_fill_response`):
  - parses and validates: gallons 0.5–60, price 0.5–15 or blank; the form's 0
    means not entered
  - applies a `source=manual` fill as of now and saves state immediately
  - writes a `fill_up` point
  - replies on `bigair/fill_up/result` (retained) with what it logged or why
    not. HA notifies the phone, and the "Last fill-up" sensor shows it.
- **Repeats are ignored:** the same `fill_id`, or the same kind, gallons and
  price within 10 min (a double-tap makes a new id).
- **Fresh install:** a full fill starts the tank. A partial needs a known tank
  first.
- The phone's notify action is `!secret mobile_app_notify`, so it never
  appears in the repo (a test enforces this). Odometer, lat and lon are blank
  until the GPS logger (#14) feeds the service.
- This path doesn't depend on detection.

## What is recorded (`fill_up`, bucket `van_history`)

Tags: `source` (live, manual, historical), `fill_type` (full, partial),
`status` (confirmed, pending, skipped).
Fields: `gallons`, `price_per_gal`, `total_cost`, `odometer_mi`, `lat`, `lon`,
`tank_before_gal` (model), `tank_after_gal`, `mpg_since_full` (full only),
`est_gallons_from_level`, `fill_id`.
Timestamp: the moment the candidate was detected (or the manual entry time).
Re-writing the same `fill_id` with the same timestamp and tags overwrites
the point, so it's idempotent.

## Effects on the fuel model

On `confirmed`: reset the counters (`/fuel-model`). Full → tank = capacity.
Partial → tank = `tank_before_gal + gallons`. Compute `mpg_since_full` and
the calibration ratio.
While `pending`: the fuel model **provisionally** applies the level-estimated
gallons and publishes `tank_estimated=true`, so range isn't wildly wrong
before Dan answers.

## Test cases the state machine must cover

- Fixture fill (Δ = 7.05 gal while parked) → exactly one candidate.
- A slosh spike on a hill while driving → no candidate.
- Parked overnight with the heater running (level drops) → no candidate.
- Ignition cycled at the pump twice → one candidate, not two.
- Moving off before settling → no candidate, and no stuck state.
