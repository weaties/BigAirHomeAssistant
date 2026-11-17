# Scan write-ups

One file per session: `YYYY-MM-DD-<topic>.md` (e.g. `2026-12-02-mode01-bitmaps.md`).
Raw captures go in `scans/raw/` (gitignored — they can contain the VIN).

Template:

```markdown
# <topic> — YYYY-MM-DD

- WiCAN: OBD | PRO, firmware x.y.z, mode (AutoPID / ELM327 / SLCAN)
- Engine state(s): KOEO / warm idle / steady 60 mph; ambient °C
- References used: dash, GPS, multimeter, scan tool

## Raw results
| Request / ID | ECU | Raw bytes | Decoded | Reference | Δ |

## Conclusions
Register rows changed (UNVERIFIED → RESPONDS / VERIFIED / ABSENT) and why.
```
