# PID / signal register

The single source of truth for **which vehicle and device signals are trusted**.
Nothing is decoded in production code unless it appears here, and nothing is
**VERIFIED** without evidence from *this* van (2017 Sprinter W906, US, OM642
3.0 V6, 4x4) or *this* device.

Procedure for adding and promoting rows: `/pid-discovery` (OBD/UDS) and
`/can-sniffing` (raw CAN). Evidence files live in `docs/scans/` as redacted
summaries. Raw captures stay out of git (`scans/raw/` is gitignored) because
they can contain the VIN.

## Status values

| Status | Meaning |
|---|---|
| `UNVERIFIED` | Nothing on this van yet. Comes from the SAE standard, another vehicle, a forum or a guess. Never shown to the driver as fact. |
| `RESPONDS` | The van answers the request, but the scaling hasn't been checked against a reference. |
| `VERIFIED` | Scaling checked against an independent reference (dash, Xentry/scan tool, GPS, multimeter, pump receipt). Evidence is linked. |
| `ABSENT` | Requested on the van and got no answer, or the bit isn't set in the supported-PIDs bitmap. Evidence is linked. |

## Standard OBD-II PIDs (mode 01, SAE J1979)

The formulas are the published SAE J1979 definitions. Whether the OM642 ECU
answers them, and whether the values are sane, is **not known yet**.

| Signal | Field | PID | Formula (SAE) | Status | Evidence | Notes |
|---|---|---|---|---|---|---|
| Supported PIDs 01–20 / 21–40 / 41–60 / 61–80 | — | `00` `20` `40` `60` | bitmap | UNVERIFIED | — | Read these first. They decide most of the rows below. |
| Engine load | `engine.engine_load_pct` | `04` | A·100/255 | UNVERIFIED | — | |
| Coolant temp | `engine.coolant_temp_c` | `05` | A − 40 | UNVERIFIED | — | History dashboard #2 |
| Intake MAP (boost) | `engine.boost_kpa` | `0B` | A kPa (absolute) | UNVERIFIED | — | Boost = MAP − baro (`33`) |
| RPM | `engine.rpm` | `0C` | (256A+B)/4 | UNVERIFIED | — | |
| Vehicle speed | `vehicle.speed_mph` | `0D` | A km/h → ×0.621371 | UNVERIFIED | — | Cross-check against GPS |
| Intake air temp | `engine.intake_air_temp_c` | `0F` | A − 40 | UNVERIFIED | — | |
| MAF | `engine.maf_g_s` | `10` | (256A+B)/100 g/s | UNVERIFIED | — | Fuel-rate fallback. #520 saw it on a 2006 EU W906. |
| Throttle position | `driver.throttle_pct` | `11` | A·100/255 | UNVERIFIED | — | On a diesel this may be the intake throttle valve, not the pedal. Prefer `49`. |
| Run time since start | — | `1F` | 256A+B s | UNVERIFIED | — | Engine-hours helper |
| O2 sensor 1, equivalence ratio | `engine.faer_ratio` | `24` | 2·(256A+B)/65536 | UNVERIFIED | — | Needed for the MAF×FAER fuel method. Seen in #520 (EU). |
| Fuel tank level | — | `2F` | A·100/255 % | UNVERIFIED | — | Absent on #520's EU van. Even if present, it reads the stock sender, so it is not a 47-gal truth. |
| Barometric pressure | — | `33` | A kPa | UNVERIFIED | — | |
| Control module voltage | `vehicle.battery_v` | `42` | (256A+B)/1000 V | UNVERIFIED | — | History dashboard #3 |
| Ambient air temp | — | `46` | A − 40 | UNVERIFIED | — | |
| Accelerator pedal D | `driver.throttle_pct` | `49` | A·100/255 | UNVERIFIED | — | Preferred driver-throttle signal |
| Engine fuel rate | `engine.fuel_rate_lph` | `5E` | (256A+B)/20 L/h | UNVERIFIED | — | **Primary fuel-model input if present.** Absent on #520's EU van. |
| Odometer | `vehicle.odometer_mi` | `A6` | (A·2²⁴+B·2¹⁶+C·2⁸+D)/10 km | UNVERIFIED | — | Usually only on 2019+ ECUs. Fallback: integrate speed and correct at each fill from the dash reading. |

## Mercedes-specific UDS DIDs (mode 22) for OM642

None known. The only published W906 DID set (`mcgyver78/WiCAN_W906`) is for
the OM651 (Delphi CRD3), and its author says OM642 is not covered. Treat its
DIDs as hints for what to look for, never as values to copy.

| Signal | Field | ECU req→resp | DID | Formula | Status | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| Injection quantity | — | TBD | TBD | TBD | UNVERIFIED | — | Would enable fuel-rate method 2 |
| Fuel tank level | — | TBD | TBD | TBD | UNVERIFIED | — | OM651 analogue is `6038` (L, stock sender). Not transferable. |
| Odometer | — | TBD | TBD | TBD | UNVERIFIED | — | OM651 `601F` is only the five-digit part of the reading (see the fork's docs) |
| DPF soot load / regen | — | TBD | TBD | TBD | UNVERIFIED | — | Later dashboard candidate |

## Raw CAN broadcast signals

| Signal | Field | Bus / pins | CAN ID | Bytes / bits | Scaling | Status | Evidence |
|---|---|---|---|---|---|---|---|
| Cruise active | `driver.cruise_active` | TBD | TBD | TBD | bool | UNVERIFIED | — |
| Cruise set speed | `driver.cruise_set_mph` | TBD | TBD | TBD | TBD | UNVERIFIED | — |
| Brake pedal | `driver.brake_active` | TBD | TBD | TBD | bool | UNVERIFIED | — |

## Devices

| Device | Signal | Source | Status | Evidence | Notes |
|---|---|---|---|---|---|
| WiCAN | model (OBD or PRO), firmware | label / web UI | UNVERIFIED | — | PRO adds KWP/MS-CAN and better ELM support |
| USB GPS | model, NMEA rate | `gpsd` | UNVERIFIED | — | |
| Webasto Air Top | variant, part number | heater label | UNVERIFIED | — | Burn rates come from that variant's datasheet |
| Webasto | W-Bus pump pulse volume | datasheet / bench | UNVERIFIED | — | `/heater-accounting` |
| Victron BMV-700 | VE.Direct fields | VE.Direct | UNVERIFIED | — | `/electrical` |
| Victron MPPT, DC-DC, shore charger | model, ports, BLE instant readout | label / VictronConnect | UNVERIFIED | — | |
| Dash display | LilyGo board and panel revision | silkscreen | UNVERIFIED | — | Decides the ESPHome `model:` |
