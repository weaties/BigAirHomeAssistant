---
name: pid-discovery
description: How to scan the Sprinter's OBD-II port with the WiCAN, record what responds, and promote a PID or UDS DID from UNVERIFIED → RESPONDS → VERIFIED in docs/pid-register.md. TRIGGER when adding or changing a decoder/scaling for any OBD PID or Mercedes DID, configuring WiCAN AutoPID, planning or writing up a scan session, or when code wants to read a vehicle signal not yet in the register. DO NOT trigger for raw broadcast-CAN reverse engineering (use /can-sniffing), GPS, Victron, or heater signals.
---

# PID discovery — WiCAN on the W906 OM642

`docs/pid-register.md` is the source of truth. This skill covers how
rows get into it and how they get promoted. It deliberately contains **no
PID values for this van**, because none are known yet.

## What we know going in (and what we don't)

- **Our van:** 2017 W906, **US-market OM642 3.0 V6**, 4x4. Every community
  W906 result so far is from **EU OM651 or OM646** vans:
  - wican-fw #520 (2006 EU W906): a mode 01 auto-scan found MAF `10` and FAER
    `24`, but no fuel level `2F` and no fuel rate `5E`.
  - wican-fw #947 / `mcgyver78/WiCAN_W906` (OM651, Delphi CRD3): the engine
    ECU ignores mode 01, and all the useful values are UDS `22 xxxx` DIDs. The
    diagnostic CAN on pins 6/14 stays silent behind the EZS gateway until a
    tester frame wakes it, which needs the fork's `ATWU…` wake-up commands.
    Its author says OM642 is not covered.
- **So:** the US OM642 may answer mode 01 directly, because US OBD rules
  apply, but that is unverified. It may also need the wake-up behaviour, or
  have an entirely different ECU and DID set. **Scan before you assume.**
- If the OBD (non-PRO) WiCAN can't talk to it, the PRO's broader protocol
  support is the first hardware change to try.

## Scan session procedure

1. **Prepare.** Note the date, the WiCAN model and firmware, the engine state
   (KOEO, idle, driving) and the ambient temperature. Have independent
   references ready: dash gauges, a GPS speed source, a multimeter on the
   battery and, ideally, a scan tool or Xentry.
2. **Supported-PID bitmaps first.** Request `0100`, `0120`, `0140`, `0160`,
   `0180` and `01A0`. Save the raw responses, including which ECU answered
   (`7E8`/`7E9`/…). The bitmap decides most register rows (supported → can
   go to `RESPONDS`, unsupported → `ABSENT`).
3. **Sample each register row** at KOEO, warm idle and a steady cruise. Record
   the raw bytes, not only decoded values.
4. **Mercedes DIDs (mode 22)** only by systematic, read-only exploration:
   `22 xxxx` reads. **Never** send a write (`2E`), routine control (`31`),
   ECU reset (`11`), DTC clear (`14`) or session change beyond the extended
   diagnostic session while driving. A DID is a lead until its scaling
   matches a reference.
5. **Write up** in `docs/scans/YYYY-MM-DD-<topic>.md`. Include the setup, the
   bitmaps and a per-signal table of raw values, decoded values and the
   reference reading. Strip the VIN (mode `09 02`) and any GPS positions. Raw
   logs go to `scans/raw/`, which is gitignored.

## Promotion rules

| From → To | Requires |
|---|---|
| UNVERIFIED → RESPONDS | Van answers; raw bytes saved in a scan write-up |
| UNVERIFIED → ABSENT | Bitmap bit clear **or** no answer in 2 sessions |
| RESPONDS → VERIFIED | Decoded value matches an independent reference in **≥ 2 operating conditions**, within a stated tolerance (e.g. speed ±2 % vs GPS at steady 30 and 60 mph; battery V ±0.2 V vs multimeter) |
| VERIFIED → demoted | Any contradicting observation. Demote first, investigate second. |

Fuel rate (`5E` or any DID) is special: it's VERIFIED only after a
**full-to-full pump-receipt comparison** within 5 % over at least 100 mi
(`/fuel-model`).

## Wiring a verified signal in

1. Register row → `VERIFIED` with an evidence link.
2. Add the WiCAN AutoPID entry. Its config JSON goes in `wican/` once that
   directory exists (M1).
3. Write a test-first decoder (`/tdd`): a raw-bytes fixture in, a
   unit-suffixed dataclass field out.
4. Add the field to the Data model in CLAUDE.md and to `/influx-schema`.

## Don't rationalize

| Excuse | Rebuttal |
|---|---|
| "The OM651 fork uses DID X for this, so I'll use it." | Different engine, ECU and market. It's a lead. Scan our van. |
| "SAE defines the formula, so it's verified." | The formula is standard; whether this ECU fills the PID sensibly is not. |
| "Values look plausible." | Plausible isn't verified. Compare against a reference. |
| "I'll mark it VERIFIED and fix it if it's wrong." | Downstream fuel and range numbers would silently be wrong. Leave it RESPONDS. |
