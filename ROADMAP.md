# Roadmap

Milestones are sequential. Each M1+ task is a GitHub issue labelled with its
milestone. Hardware-gated tasks carry `needs-van`, and tasks that depend on an
unconfirmed signal carry `unverified-signal`. Facts still needed from Dan are
tracked in #20.

## M0 — Foundations ✅ (this bootstrap)

CLAUDE.md, the skills, the PID register (every signal UNVERIFIED), the
repo scaffold (interfaces only), a synthetic drive fixture with hand-derived
answers, CI (test, lint, typecheck, config) and this roadmap. No feature
logic: fuel-model and fill-detection acceptance tests are
`xfail(strict=True)`.

## M1 — Live fuel model and fill-ups

Goal: in the van, with no internet, the dash display and HA show a tank
level and range Dan trusts. Fill-ups are prompted and logged, and every
signal used is VERIFIED.

| Order | Issue | Task | Depends on |
|---|---|---|---|
| 1 | #4 | On-van PID scan: bitmaps and first register update | — |
| 1 | #15 | Pi bring-up: SSD, hotspot, compose stack, live smoke test | — |
| 1 | #24 | Pi power from the house battery: supply, draw, low-battery shutdown | #15 |
| 2 | #22 | System time from GPS (chrony + gpsd), Influx write guard | #15 |
| 2 | #23 | Decide the distance source (OBD speed vs GPS vs odometer) | #4 |
| 2 | #6 | InfluxDB writer edge, buckets, DBRP provisioning | #15 |
| 2 | #5 | WiCAN MQTT ingest edge → `ObdSample` | #4 |
| 2 | #19 | Mosquitto auth and ACLs | #15 |
| 3 | #7 | Fuel model: integrator and tank level | — (fixture) |
| 3 | #8 | Fuel model: rolling mpg, range, instant mpg | #7 |
| 3 | #9 | Fuel model: fill reset, true mpg, persisted state | #7 |
| 3 | #18 | Fuel-rate fallback MAF × FAER + calibration (only if `5E` is absent) | #4, #7 |
| 4 | #10 | Fill-up detection state machine | — (fixture); live use needs a level source (#20) |
| 4 | #13 | HA MQTT discovery and availability | #5 |
| 4 | #12 | Manual log-fill-up flow | #9, #13 |
| 4 | #11 | Fill-up prompt: notification, parsing, re-prompt | #10, #13 |
| 5 | #14 | GPS logger: gpsd, track points with `mpg_30s` | #8, #15, #22 |
| 5 | #17 | Feed the dash display from the fuel model (tank, range, trip-mpg buckets) | #8 |
| 5 | #16 | Dash display: board identified (T5 S3 PRO), eink-screen firmware pointed at `bigair display`; measure battery | — |

The logic issues (#7–#10) can start immediately against the fixture, in
parallel with the hardware issues (#4, #15).

## M2 — History, driver inputs, heater

- Historical fill-up import: CSV parser (waits on the sample in #20), photo
  matching via osxphotos, review CSV, idempotent re-run.
- Historical dashboards: fill-up map, mpg and cost-per-mile history,
  before/after the tank install, baseline mpg prior.
- 1-min downsampling task into `van_history`; `trip` summaries.
- CAN sniffing for cruise state, cruise set speed and brake (`/can-sniffing`);
  may need a chassis-CAN tap, so ask before splicing.
- Driver views: throttle-vs-mpg scatter first, then per-trip overlay, then
  throttle × speed heatmap. Share of trip on cruise, brake events per mile,
  and mpg on cruise vs manual.
- Webasto: read the label, then the W-Bus ESP32 node (read-only), heater fuel
  series, and joint `k_e`/`k_h` calibration.
- Offline Geomap basemap tiles.
- Later history candidates: engine load, RPM, boost, IAT, DPF soot and
  regens, idle time and engine hours, fault codes.

## M3 — Electrical and polish

- Victron: VE.Direct reader for BMV-700 and MPPT; Victron BLE (DC-DC, shore)
  via HA → `mqtt_statestream`.
- INA228 per-circuit node (fan, Webasto, lights, internet, inverter) with
  clamp-meter calibration.
- Energy dashboards: charging by source vs consumption by load, daily
  budget, SOC history.
- Dash display hardware upgrade (round pod display or sunlight-readable
  colour e-paper).
- Skill evals (`evals/cases.yaml`) for the most-used skills (Decision #13).
- Heater control from HA and CarPlay (writes over W-Bus) after read-only
  W-Bus has run reliably.
