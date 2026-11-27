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

The formulas are the published SAE J1979 definitions. The first scan
(2026-11-20) shows which ones the engine ECU answers; none is VERIFIED yet.

| Signal | Field | PID | Formula (SAE) | Status | Evidence | Notes |
|---|---|---|---|---|---|---|
| Supported PIDs 01–20 / 21–40 / 41–60 / 61–80 / 81–A0 | — | `00` `20` `40` `60` `80` | bitmap | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Engine ECU `7E8` answers through `8B`; `7E9` and `7EC` answer small sets |
| Engine load | `engine.engine_load_pct` | `04` | A·100/255 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 25 % at warm-up idle |
| Coolant temp | `engine.coolant_temp_c` | `05` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 54 °C after 16 min idle. Logged for history every ~10 s (#79); Engine history → Engine temperatures |
| Intake MAP (boost) | `engine.boost_kpa` | `0B` | A kPa (absolute) | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear; `NO DATA`. Look for an extended boost PID (`70`/`87` are in the `7E8` bitmap) or a DID |
| RPM | `engine.rpm` | `0C` | (256A+B)/4 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 862 rpm at warm-up idle; compare with the tach |
| Vehicle speed | `vehicle.speed_mph` | `0D` | A km/h → ×0.621371 | VERIFIED | [2026-11-20 drive](scans/2026-11-20-drive-speed.md) | Within ±2 % of GPS at steady ~25 mph (+0.6 %) and ~40 mph (−0.2 %); integrated distance −0.06 % over 5 mi. Whole-km/h resolution: compare means, not single samples |
| Intake air temp | `engine.intake_air_temp_c` | `0F` | A − 40 | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear. Extended IAT `68` is in the `7E8` bitmap |
| MAF | `engine.maf_g_s` | `10` | (256A+B)/100 g/s | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear. Extended MAF `66` responds instead (row below) |
| MAF (extended, sensor A) | `engine.maf_g_s` | `66` | (256B+C)/32 g/s, A = sensor-support bits | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 23.8 g/s at warm-up idle |
| Throttle position | `driver.throttle_pct` | `11` | A·100/255 | UNVERIFIED | — | On a diesel this may be the intake throttle valve, not the pedal. Prefer `49`. |
| Run time since start | — | `1F` | 256A+B s | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 973 s, consistent with the WiCAN's wake time. Engine-hours helper |
| O2 sensor 1, equivalence ratio | `engine.faer_ratio` | `24` | 2·(256A+B)/65536 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Saturated (`FF FF` → 2.0) at idle: no use as FAER there. Moot while `5E` works |
| Fuel tank level | — | `2F` | A·100/255 % | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 24.7 %. Stock sender moved to the 47-gal tank: delta detector for #10 only, through a learned table (Decision #31) |
| Barometric pressure | — | `33` | A kPa | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 100 kPa |
| Control module voltage | `vehicle.battery_v` | `42` | (256A+B)/1000 V | UNVERIFIED | — | In the `7E8` bitmap; not sampled yet. History dashboard #3 |
| Ambient air temp | `vehicle.ambient_temp_c` | `46` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 6 °C on a cold morning. Logged for history (#79) |
| Accelerator pedal D | `driver.throttle_pct` | `49` | A·100/255 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 5.1 % foot off. Preferred driver-throttle signal |
| Engine fuel rate | `engine.fuel_rate_lph` | `5E` | (256A+B)/20 L/h | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | **Present on this van.** 1.55 L/h at warm-up idle. VERIFIED only after a full-to-full pump comparison within 5 % over ≥ 100 mi |
| Fuel type | — | `51` | enum | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | `04` = diesel ✓ (matches the vehicle) |
| Oil temp | `engine.oil_temp_c` | `5C` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 50 °C while warming. Logged for history (#79) |
| Odometer | `vehicle.odometer_mi` | `A6` | (A·2²⁴+B·2¹⁶+C·2⁸+D)/10 km | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | `80` bitmap's last bit clear: nothing above `A0`. Integrate speed and correct at each fill from the dash (#23) |

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
| WiCAN | **WiCAN-OBD-PRO**, firmware 4.50 (git v4.50p); ELM327 v2.3 emulation on TCP 35000 | web UI About page, 2026-11-20 | VERIFIED | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Sleeps with the engine off (woke on start). v6.0.0 offered; update after a baseline on 4.50 |
| WiCAN | Battery voltage at OBD pin 16 (`ATRV`, adapter command) → `vehicle.battery_v` | ELM327 over TCP | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 14.1–14.2 V engine running. Check against a multimeter (±0.2 V) at rest and charging before VERIFIED. Power debugging and history only |
| GPS receiver | Dual XGPS160, Bluetooth SPP ch 1, NMEA 10 Hz, GPS+GLONASS | host `gpsd -b -n` on `/dev/rfcomm0` | RESPONDS | `docs/scans/2026-11-19-xgps160-trial.md` | Adopted (Decision #28). Reboot test passed. VERIFIED after the 24 h and drive tests. Fallback: u-blox M8 USB with an active antenna. |
| Webasto Air Top | variant, part number | heater label | UNVERIFIED | — | Burn rates come from that variant's datasheet |
| Webasto | W-Bus pump pulse volume | datasheet / bench | UNVERIFIED | — | `/heater-accounting` |
| Victron BMV-712 Smart (model id 0xA383) | Instant Readout, battery monitor record 0x02: voltage, current, SOC, consumed Ah, TTG, aux | BLE advertisement (company 0x02E1), AES-CTR | RESPONDS | Victron "Extra manufacturer data" spec (2022-12-14); 2026-11-26 decode 13.27 V / −3.663 A / 99.2 % vs VictronConnect 13.30 V / −3.53 A / 99 % (minutes apart) | VERIFIED after a same-moment side-by-side with VictronConnect, including while charging. Aux input is off (mode 3) |
| Victron SmartSolar MPPT 100/30 | Instant Readout, solar charger record 0x01: state, battery V/A, yield today, PV W | BLE advertisement | RESPONDS | Victron spec; 2026-11-26 live decode bulk / 13.28 V / 60 Wh matches VictronConnect | VERIFIED after a side-by-side in full sun |
| Victron Orion-Tr Smart 12/12-30 Isolated | Instant Readout, DC/DC record 0x04: state, input V, output V, off reason | BLE advertisement | RESPONDS | Victron spec; 2026-11-26 decode off / 12.71 V in vs VictronConnect 12.72 V | VERIFIED after a side-by-side while charging (engine running) |
| Victron shore charger | model, BLE instant readout | label / VictronConnect | UNVERIFIED | — | Not seen in BLE scans |
| Dash display | LilyGo T5 ePaper S3 PRO: ESP32-S3 rev v0.2, 16 MB flash, 8 MB PSRAM, 960×540 parallel e-paper | esptool `flash_id`, boot log | VERIFIED | esptool + eink-screen boot log, 2026-11-19 | Runs the eink-screen firmware (epdiy V7), Decision #29 |
