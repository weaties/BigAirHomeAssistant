# 2026-11-20 — First OBD scan: supported-PID bitmaps and a warm-up idle sample

## Setup

| | |
|---|---|
| Vehicle | 2017 Sprinter W906, US, OM642 3.0 V6, 4x4 (VIN not read) |
| Adapter | WiCAN-OBD-PRO (MeatPi), firmware 4.50 (git v4.50p); ELM327 v2.3 emulation over TCP 35000 |
| Path | WiCAN joined the van router `bigair`; requests sent from the Pi (`bigair-ha`) with a read-only Python script |
| Protocol | `ATSP6` → ISO 15765-4, CAN 11-bit, 500 kbit/s (the WiCAN auto-detected the same) |
| Engine state | Running, parked, warming up (about 16 min after start) |
| Ambient | Cold morning; the van's own ambient PID read 6 °C |
| Commands sent | `AT` setup, mode 01 only. No mode 09 (VIN), no mode 22, no writes, resets or DTC clears. |

Raw responses: `scans/raw/bitmaps-2026-11-20.json` and `scans/raw/idle-2026-11-20.json` (gitignored).

## Which ECUs answered

Functional request `01 00` (to `7DF`) got three answers:

| Response ID | Probably | Notes |
|---|---|---|
| `7E8` | Engine ECU | Answers through PID `8B` (bitmaps `00`–`80`). All samples below come from it. |
| `7E9` | Transmission (inferred, UNVERIFIED) | Supports a small standard set only |
| `7EC` | Unknown (UNVERIFIED) | Small standard set, similar to `7E9` |

The "probably" column is a guess from the conventional address layout. It isn't
confirmed for this van.

## Bitmaps (raw)

```
0100  7E8 06 41 00 98 18 A0 13   7EC 06 41 00 98 18 00 01   7E9 06 41 00 98 18 00 01
0120  7E8 06 41 20 90 03 A0 01   7EC 06 41 20 80 01 A0 01   7E9 06 41 20 80 01 80 01
0140  7E8 06 41 40 C4 C0 80 1D   7EC 06 41 40 44 80 00 00   7E9 06 41 40 40 80 00 00
0160  7E8 06 41 60 EF BB A3 43
0180  7E8 06 41 80 E9 A0 00 00
```

Engine ECU (`7E8`) supports: `01 04 05 0C 0D 11 13 1C 1F 20 21 24 2F 30 31 33 40 41 42
46 49 4A 51 5C 5D 5E 60 61 62 63 65 66 67 68 69 6B 6C 6D 6F 70 71 73 77 78 7A 7F 80 81
82 83 85 88 89 8B`.

Not supported by any ECU, among the rows in the register: `0B` (MAP), `0F` (IAT),
`10` (MAF), `A6` (odometer; the `80` bitmap's last bit is clear, so nothing above `A0`).

**Headline:** unlike the EU W906 in wican-fw #520, this US OM642 supports both
**`5E` engine fuel rate** and **`2F` fuel tank level**.

## Warm-up idle sample (engine ECU, `ATSH7E0`)

Decoded with the SAE J1979 formulas. These are **not verified scalings**: each one
needs an independent reference before it can be trusted (`/pid-discovery`).

| PID | Raw | SAE decode | Sanity / reference |
|---|---|---|---|
| `5E` fuel rate | `00 1F` | 31/20 = **1.55 L/h** (0.41 gph) | Plausible for a warm-up idle. Needs a full-to-full pump comparison. |
| `2F` fuel level | `3F` | 63·100/255 = **24.7 %** | Stock sender in the 47-gal tank: delta detector only (Decision #31) |
| `0C` RPM | `0D 78` | 3448/4 = 862 rpm | Plausible warm-up idle; compare with the tach |
| `0D` speed | `00` | 0 km/h | Parked ✓ |
| `05` coolant | `5E` | 94 − 40 = 54 °C | Warming up after 16 min ✓ plausible |
| `04` load | `40` | 25.1 % | |
| `66` MAF (sensor A) | `01 02 F9 00 00` | 761/32 = 23.8 g/s | The standard MAF `10` is absent; `66` is the extended MAF PID |
| `46` ambient | `2E` | 6 °C | Plausible for the morning |
| `5C` oil temp | `5A` | 50 °C | Below coolant while warming ✓ plausible |
| `51` fuel type | `04` | **Diesel** ✓ | Matches the vehicle |
| `33` baro | `64` | 100 kPa | ✓ plausible near sea level |
| `1F` run time | `03 CD` | 973 s = 16.2 min | **Consistent with the engine start**: the WiCAN woke when the van started and reported 8 min 25 s of uptime at 08:41 local, i.e. it started about 08:32:35; the sample was taken at 08:48:44, which gives 16.2 min |
| `31` distance since DTC clear | `15 D4` | 5588 km | |
| `21` distance with MIL on | `00 00` | 0 km | |
| `49` pedal D | `0D` | 5.1 % | Foot off the pedal; many pedals rest above 0 % |
| `24` O2 sensor 1 | `FF FF 2A 53` | λ = 2.0 (saturated), 1.32 V | **Saturated at the top of the range**, so it can't serve as FAER for the MAF method at idle |
| `5D` injection timing | `66 C7` | −4.45° | |

## Conclusions

- Fuel-rate method 1 (`5E`) is available. #18's MAF×FAER fallback may never be
  needed, and `24` is saturated at idle anyway.
- `2F` is available for fill detection (#10), mapped through a learned table.
- `0D` (speed) responds. Verify it against GPS at steady 30 and 60 mph.
- `A6` odometer is absent, so distance comes from speed or GPS (#23).

## Next

1. Drive: log `0D`, `0C`, `5E`, `2F` and GPS speed at 1 Hz at steady 30 and 60 mph to
   verify speed, and record the dash odometer at the start and end.
2. Fill up (full) at the start and end of at least 100 mi to verify `5E`.
3. Update the WiCAN firmware (v6.0.0 offered) only after this baseline, then re-run the bitmaps to confirm nothing changed.
