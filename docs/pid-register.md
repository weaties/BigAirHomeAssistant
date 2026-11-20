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
| Coolant temp | `engine.coolant_temp_c` | `05` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 54 °C after 16 min idle. History dashboard #2 |
| Intake MAP (boost) | `engine.boost_kpa` | `0B` | A kPa (absolute) | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear; `NO DATA`. Look for an extended boost PID (`70`/`87` are in the `7E8` bitmap) or a DID |
| RPM | `engine.rpm` | `0C` | (256A+B)/4 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 862 rpm at warm-up idle; compare with the tach |
| Vehicle speed | `vehicle.speed_mph` | `0D` | A km/h → ×0.621371 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 0 parked. Verify against GPS at steady 30 and 60 mph |
| Intake air temp | `engine.intake_air_temp_c` | `0F` | A − 40 | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear. Extended IAT `68` is in the `7E8` bitmap |
| MAF | `engine.maf_g_s` | `10` | (256A+B)/100 g/s | ABSENT | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Bitmap bit clear. Extended MAF `66` responds instead (row below) |
| MAF (extended, sensor A) | `engine.maf_g_s` | `66` | (256B+C)/32 g/s, A = sensor-support bits | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 23.8 g/s at warm-up idle |
| Throttle position | `driver.throttle_pct` | `11` | A·100/255 | UNVERIFIED | — | On a diesel this may be the intake throttle valve, not the pedal. Prefer `49`. |
| Run time since start | — | `1F` | 256A+B s | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 973 s, consistent with the WiCAN's wake time. Engine-hours helper |
| O2 sensor 1, equivalence ratio | `engine.faer_ratio` | `24` | 2·(256A+B)/65536 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | Saturated (`FF FF` → 2.0) at idle: no use as FAER there. Moot while `5E` works |
| Fuel tank level | — | `2F` | A·100/255 % | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 24.7 %. Stock sender moved to the 47-gal tank: delta detector for #10 only, through a learned table (Decision #31) |
| Barometric pressure | — | `33` | A kPa | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 100 kPa |
| Control module voltage | `vehicle.battery_v` | `42` | (256A+B)/1000 V | UNVERIFIED | — | In the `7E8` bitmap; not sampled yet. History dashboard #3 |
| Ambient air temp | — | `46` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 6 °C on a cold morning |
| Accelerator pedal D | `driver.throttle_pct` | `49` | A·100/255 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 5.1 % foot off. Preferred driver-throttle signal |
| Engine fuel rate | `engine.fuel_rate_lph` | `5E` | (256A+B)/20 L/h | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | **Present on this van.** 1.55 L/h at warm-up idle. VERIFIED only after a full-to-full pump comparison within 5 % over ≥ 100 mi |
| Fuel type | — | `51` | enum | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | `04` = diesel ✓ (matches the vehicle) |
| Oil temp | — | `5C` | A − 40 | RESPONDS | [2026-11-20 scan](scans/2026-11-20-pid-bitmaps.md) | 50 °C while warming |
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
| GPS receiver | Dual XGPS160, Bluetooth SPP ch 1, NMEA 10 Hz, GPS+GLONASS | host `gpsd -b -n` on `/dev/rfcomm0` | RESPONDS | `docs/scans/2026-11-19-xgps160-trial.md` | Adopted (Decision #28). Reboot test passed. VERIFIED after the 24 h and drive tests. Fallback: u-blox M8 USB with an active antenna. |
| Webasto Air Top | variant, part number | heater label | UNVERIFIED | — | Burn rates come from that variant's datasheet |
| Webasto | W-Bus pump pulse volume | datasheet / bench | UNVERIFIED | — | `/heater-accounting` |
| Victron BMV-700 | VE.Direct fields | VE.Direct | UNVERIFIED | — | `/electrical` |
| Victron MPPT, DC-DC, shore charger | model, ports, BLE instant readout | label / VictronConnect | UNVERIFIED | — | |
| Dash display | LilyGo T5 ePaper S3 PRO: ESP32-S3 rev v0.2, 16 MB flash, 8 MB PSRAM, 960×540 parallel e-paper | esptool `flash_id`, boot log | VERIFIED | esptool + eink-screen boot log, 2026-11-19 | Runs the eink-screen firmware (epdiy V7), Decision #29 |
