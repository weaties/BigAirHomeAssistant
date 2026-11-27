---
name: electrical
description: House electrical telemetry — Victron integration (BMV-712 Smart, SmartSolar MPPT and Orion DC-DC decoded by bigair from BLE Instant Readout), per-circuit INA228 current sensing on an ESP32, charging-by-source vs consumption-by-load accounting, and how to add a load. TRIGGER when working on electrical/load measurements, esphome/loads*.yaml, VE.Direct or Victron BLE ingestion, or energy-budget dashboards. DO NOT trigger for the starter battery voltage from OBD (that's /pid-discovery) or heater fuel (use /heater-accounting).
---

# Electrical

## Device inventory (confirmed 2026-11-26, #109)

| Device | Interface | Record | Status |
|---|---|---|---|
| **BMV-712 Smart** battery monitor (model id 0xA383) | BLE Instant Readout, built in | 0x02 battery monitor | RESPONDS |
| **SmartSolar MPPT 100/30** | BLE Instant Readout | 0x01 solar charger | RESPONDS |
| **Orion-Tr Smart 12/12-30 Isolated** DC-DC | BLE Instant Readout | 0x04 DC/DC converter | RESPONDS |
| Shore charger | ? | — | not seen in BLE scans |

It's a BMV-712, not a BMV-700: no VE.Direct cable is needed. Evidence and the
VERIFIED criteria are in `docs/pid-register.md`.

## How the data gets in (Decision #45)

- **bigair decodes Instant Readout itself.** `victron_io.py` (bleak, using the host's
  BlueZ over the read-only `/run/dbus` mount) passes manufacturer data for company
  0x02E1 from the configured MACs to `victron_ble.py`. HA's Bluetooth stays off,
  so there's no `mqtt_statestream` and the XGPS160 keeps the radio.
- **Format:** Victron's "Extra manufacturer data" spec
  (wiki.victronenergy.com/rend/ble/extra_manufacturer_data). Byte 0 is `0x10`, byte 4 is
  the record type, bytes 5–6 are the nonce (LSB first), and byte 7 is key[0], used as
  a check. The data is AES-128-CTR with counter block `nonce ‖ 14 zero bytes`, then an
  LSB-first bitstream. Code offsets are the spec's start bits minus 32. NA values are
  raw bit patterns: compare them before sign conversion. (Battery current NA is
  0x3FFFFF, not −1.) Length-check every field, because records may grow.
- **Other packets:** devices also send non-`0x10` advertisements. Those are skipped
  silently (`NotInstantReadout`).
- **Keys:** VictronConnect → device → gear → Product info → Instant readout details →
  Show. They go in the Pi's `deploy/.env` as `VICTRON_<NAME>_MAC` / `VICTRON_<NAME>_KEY`,
  and **never** in the repo or test fixtures. Tests encrypt synthetic records with a
  made-up key. **Changing a device's Bluetooth PIN changes its key**, and bigair then
  logs a key mismatch once per device.
- **Output:** `ElectricalTracker` keeps the latest reading per device. It publishes
  `bigair/electrical/state` (retained, every 5 s; HA sensors "House battery", "Solar
  power", …) and writes `electrical` points every 10 s per device. Freshness uses
  monotonic time (#107). After 2 min of silence a device's values go null (unknown).
- **Adding a Victron device:** add its MAC and key pair to `.env`. If it uses a record
  type other than 0x01, 0x02 or 0x04, add that layout from the spec to
  `victron_ble.decode` with a synthetic test. Then add its keys to `ElectricalTracker._MAP`
  and `ha_discovery.ELECTRICAL_SENSORS`.

## Per-circuit loads: INA228 on ESP32 (Dan's choice)

- One INA228 plus one shunt per circuit: fan, Webasto, lights, internet
  gear, inverter, and the Pi itself (`pi`). The Pi is always on from the house
  battery (CLAUDE.md Decision #21), so its draw counts in the daily budget. I²C addresses 0x40–0x4F allow up to 16 per bus.
- **Shunt sizing:** pick the shunt so the circuit's max current gives close
  to (not over) the INA228's full-scale shunt voltage range. The inverter
  needs a high-current shunt, possibly its own Victron SmartShunt instead.
  Fuse the sense leads.
- High-side sensing, so the INA228 also reports bus voltage. Common ground
  with the house battery negative.
- ESPHome: check the current ESPHome docs for the INA2xx component that
  supports INA228 and pin to a version that has it. Publish `current_a`,
  `voltage_v` and `power_w` per circuit **straight to MQTT** with ESPHome's
  `mqtt` component (Decision #23), so energy accounting doesn't depend on HA.
  bigair records them as `load` tagged with `circuit`, and HA picks them up
  through MQTT discovery for display.
- Calibrate each channel against a clamp meter at a known load. Record the
  shunt resistance and calibration factor in the node YAML.

## Accounting (M3)

- **Charging by source:** solar = MPPT battery current × voltage (or `pv_power_w`);
  alternator = Orion output (its record has no current: estimate it from the BMV's
  current while the Orion is on, or add a shunt); shore = charger output.
- **Consumption by load:** Σ INA228 circuits. "Other" = BMV net minus known
  loads minus charge sources, which shows unmetered loads.
- **Daily energy budget:** Wh in by source vs Wh out by load per local day,
  plus SOC history from the BMV. Integrate W to Wh in bigair, not in
  Grafana.

## Adding a load

1. Pick a free INA228 address and a shunt size; wire it and fuse the sense
   leads.
2. Add a sensor block in `esphome/loads.yaml` with `circuit` named in
   `snake_case`.
3. Add the circuit to the `circuit` tag list in CLAUDE.md → Data model.
4. Calibrate against a clamp meter, then add it to the energy-budget
   dashboard.
