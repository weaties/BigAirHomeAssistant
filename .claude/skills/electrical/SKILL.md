---
name: electrical
description: House electrical telemetry — Victron integration (BMV-700 and MPPT over VE.Direct, DC-DC and shore chargers over Victron BLE instant readout), per-circuit INA228 current sensing on an ESP32, charging-by-source vs consumption-by-load accounting, and how to add a load. TRIGGER when working on electrical/load measurements, esphome/loads*.yaml, VE.Direct or Victron BLE ingestion, or energy-budget dashboards. DO NOT trigger for the starter battery voltage from OBD (that's /pid-discovery) or heater fuel (use /heater-accounting).
---

# Electrical

## Device inventory (confirm before designing)

Every row is **UNVERIFIED** until Dan confirms the model and ports
(`docs/pid-register.md` → Devices):

| Device | Expected interface | Open question |
|---|---|---|
| BMV-700 battery monitor | VE.Direct port → VE.Direct-USB cable → Pi | none (the BMV-700 has no built-in Bluetooth) |
| Solar controller (MPPT) | VE.Direct and/or BLE | Exact model; SmartSolar = BLE, BlueSolar = VE.Direct only |
| DC-DC charger | BLE instant readout? | Exact model. Not every Orion supports instant readout. |
| Shore power charger | BLE instant readout? | Exact model (Blue Smart IP22/IP65/…) |

## Paths into the system

1. **VE.Direct (BMV-700, MPPT):** a USB cable into the Pi. Two options:
   - **bigair reads VE.Direct text frames directly** (recommended: one Pi,
     no extra OS). The VE.Direct text protocol is documented by Victron:
     labels such as `V`, `I`, `P`, `SOC`, `CE`, `TTG`, and for MPPTs `VPV`,
     `PPV`, `CS`, `H20`. Scale per Victron's protocol document and test it
     with captured frames.
   - Venus OS (on a second Pi or a Cerbo GX), bridged to Mosquitto. It's
     richer, but it's a second OS to maintain. Choose it only if a Cerbo
     shows up in the inventory.
2. **Victron BLE instant readout (DC-DC, shore, SmartSolar):** an HA
   integration decrypts the advertisements with each device's encryption
   key, which lives in `secrets.yaml`. Its entities go to MQTT via
   `mqtt_statestream`, and bigair records them (`/home-assistant-config`).
   Confirm each device actually advertises instant readout before relying
   on it.

## Per-circuit loads: INA228 on ESP32 (Dan's choice)

- One INA228 plus one shunt per circuit: fan, Webasto, lights, internet
  gear, inverter. I²C addresses 0x40–0x4F allow up to 16 per bus.
- **Shunt sizing:** pick the shunt so the circuit's max current gives close
  to (not over) the INA228's full-scale shunt voltage range. The inverter
  needs a high-current shunt, possibly its own Victron SmartShunt instead.
  Fuse the sense leads.
- High-side sensing, so the INA228 also reports bus voltage. Common ground
  with the house battery negative.
- ESPHome: check the current ESPHome docs for the INA2xx component that
  supports INA228 and pin to a version that has it. Publish `current_a`,
  `voltage_v` and `power_w` per circuit to HA (native API). bigair records
  them as `load` tagged with `circuit`.
- Calibrate each channel against a clamp meter at a known load. Record the
  shunt resistance and calibration factor in the node YAML.

## Accounting (M3)

- **Charging by source:** solar = MPPT `PPV`/output power; alternator =
  DC-DC output; shore = charger output.
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
