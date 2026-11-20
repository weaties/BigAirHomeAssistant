# 2026-11-20 — Verification drive: OBD speed (`0D`) vs GPS

## Setup

| | |
|---|---|
| Adapter | WiCAN-OBD-PRO, firmware 4.50, ELM327 over TCP 35000, engine ECU addressed with `ATSH7E0` |
| Logger | Read-only mode 01 loop on the Pi (`0D 0C 5E 2F 05 49`, about 1 Hz) plus `gpsd` TPV speed from the XGPS160 (10 Hz NMEA, latest fix per row) |
| Reference | GPS horizontal speed (Doppler, from NMEA RMC). The XGPS160 was adopted in Decision #28 with about 2.9 m position accuracy |
| Roads | Local roads, two drives (17:03–17:16Z and 17:46–18:04Z), top speed about 40 mph |
| Data | `scans/raw/drive-2026-11-20.csv` (gitignored). The logger stores **no positions**. |

The Pi lost power three times on the van's 12 V → USB-C PD adapter: once in
the first drive, once at a port swap, and once mid-second-drive. That left gaps
in the log (17:16–17:26, 17:29–17:35, 17:51–17:57). It's on a power bank for now
(see #24). The first loss left the file with a run of zero bytes where unwritten
rows had been, so the logger now fsyncs every row.

## Method

Steady windows: 15 consecutive rows (≤ 20 s) with OBD speed above 10 mph and
varying by no more than 1.3 mph. Compare the mean OBD speed with the mean GPS
speed over each window. `0D` reports whole km/h (0.62 mph steps), so single
readings jitter and only window means are compared.

## Results

| Window (UTC) | OBD mph | GPS mph | OBD/GPS |
|---|---|---|---|
| 17:08:00 | 14.25 | 14.31 | 0.9962 |
| 17:10:23 | 29.25 | 29.22 | 1.0008 |
| 17:12:06 | 24.52 | 22.40 | 1.0948 ⚠ |
| 17:14:15 | 25.02 | 24.77 | 1.0100 |
| 17:14:30 | 24.56 | 24.34 | 1.0092 |
| 17:14:46 | 24.86 | 24.54 | 1.0130 |
| 17:15:01 | 24.52 | 24.54 | 0.9992 |
| 17:15:15 | 24.86 | 25.00 | 0.9943 |
| 17:15:30 | 24.73 | 24.64 | 1.0038 |
| 17:15:45 | 24.69 | 24.52 | 1.0068 |
| 17:16:00 | 24.69 | 24.47 | 1.0089 |
| 17:49:45 | 39.10 | 38.73 | 1.0097 |
| 17:50:01 | 39.73 | 39.48 | 1.0063 |
| 17:50:16 | 39.68 | 39.37 | 1.0080 |
| 17:50:31 | 39.60 | 39.37 | 1.0060 |
| 17:59:17 | 39.52 | 40.11 | 0.9853 |
| 17:59:31 | 39.89 | 40.51 | 0.9847 |
| 17:59:48 | 39.68 | 40.20 | 0.9872 |

- **About 25 mph** (8 windows, 17:14–17:16): OBD/GPS 0.994–1.013, mean **+0.6 %**.
- **About 40 mph** (7 windows, two directions): 0.985–1.010, mean **−0.2 %**.
- **Whole log, integrated:** OBD 5.04 mi vs GPS 5.04 mi (**−0.06 %**).
- ⚠ 17:12:06 is the one outlier (+9.5 %). OBD held steady while GPS read
  about 2 mph low. Its GPS speed spread (1.55 mph) is no worse than other
  windows, so the cause is unproven. GPS horizontal speed through a turn, or a
  brief GPS dropout, are the likely explanations. It's excluded from the means
  but reported here.
- The 40 mph windows differ by direction (+0.7 % one way, −1.4 % the other).
  Both are within tolerance. Grade, or GPS lag on slight speed changes, could
  account for it. Worth another look on a highway run.

**Dash odometer:** 80806 → 80814, trip 11.3 → 19.7 = **8.4 mi**. The log covers
only 5.04 mi of that; the rest was driven while the Pi was down. So the
odometer can't serve as a distance check this time.

## Verdict

`0D` → **VERIFIED**: within ±2 % of GPS at two steady speeds (about 25 and
about 40 mph), and the integrated distance agrees within 0.1 %. A 55+ mph
highway run would extend the check, but it isn't required by the promotion rule.

## Side observations (not verifications)

- **Fuel (`5E`):** 0.462 gal over the 5.04 logged miles, about **10.9 mpg** for
  cold-start local driving with idling. Plausible. `5E` stays RESPONDS until a
  full-to-full pump comparison over at least 100 mi.
- **Level (`2F`):** 24.7 % → 22.0 % while `5E` integrated 0.46 gal. On a linear
  47-gal scale that would be 1.27 gal, so the stock sender doesn't map linearly
  to this tank, as Decision #31 expected. Slosh affects single readings, so this
  is a lead only.
