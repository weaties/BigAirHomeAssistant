"""Decode ELM327 mode 01 responses from the WiCAN into ObdSample — High tier.

Only signals in docs/pid-register.md are decoded:

- `0D` vehicle speed: VERIFIED (2026-11-20 drive, ±2 % vs GPS).
- `5E` engine fuel rate: RESPONDS. Used while calibrating (Decision #32): tank
  and range are published flagged `calibrating` until a full-to-full pump
  comparison verifies it.
- `2F` fuel level: RESPONDS. Stock sender, a fill-up delta detector only
  (Decision #31), never shown as the tank reading.
- `ATRV`: the WiCAN's own battery-voltage reading (an adapter command, not a
  vehicle PID). RESPONDS: 14.1–14.2 V with the engine running; not yet checked
  against a multimeter. Used for power debugging and history only.

Pure functions: raw response text in, unit-suffixed values out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bigair.signals import ObdSample

if TYPE_CHECKING:
    from datetime import datetime

LITRES_PER_GAL = 3.785411784
MPH_PER_KMH = 0.621371
POLLED_PIDS: tuple[str, ...] = ("0D", "5E", "2F")


def payload(raw: str, pid: str) -> bytes | None:
    """Data bytes of a positive `41 <pid>` response, or None (NO DATA, errors, other PIDs).

    Accepts the compact headers-off form we poll with (`410D00`) and the spaced
    form, with or without CAN headers (`7E8 03 41 0D 00`), as in the scan logs.
    """
    pid = pid.upper()
    for line in raw.upper().splitlines():
        line = line.strip()
        if " " in line:
            tokens = line.split()
            for i in range(len(tokens) - 1):
                if tokens[i] == "41" and tokens[i + 1] == pid:
                    data = "".join(tokens[i + 2 :])
                    break
            else:
                continue
        elif line.startswith("41" + pid):
            data = line[4:]
        else:
            continue
        if not data or len(data) % 2:
            return None
        try:
            return bytes.fromhex(data)
        except ValueError:
            return None
    return None


ADAPTER_V_RANGE = (5.0, 20.0)  # anything outside is a garbled reply, not a 12 V system


def decode_adapter_volts(raw: str) -> float | None:
    """`ATRV` reply like "14.2V" → volts, or None."""
    text = raw.strip().upper().rstrip("V").strip()
    try:
        v = float(text)
    except ValueError:
        return None
    lo, hi = ADAPTER_V_RANGE
    return v if lo <= v <= hi else None


def _need(data: bytes, n: int, pid: str) -> None:
    if len(data) < n:
        raise ValueError(f"PID {pid}: expected {n} data bytes, got {len(data)}")


def decode_speed_kmh(data: bytes) -> float:
    """`0D`: A km/h (SAE J1979)."""
    _need(data, 1, "0D")
    return float(data[0])


def decode_fuel_rate_lph(data: bytes) -> float:
    """`5E`: (256A+B)/20 L/h (SAE J1979)."""
    _need(data, 2, "5E")
    return (data[0] * 256 + data[1]) / 20


def decode_fuel_level_pct(data: bytes) -> float:
    """`2F`: A·100/255 % (SAE J1979). Stock-sender scale, not tank gallons."""
    _need(data, 1, "2F")
    return data[0] * 100 / 255


def _value(raw: str | None, pid: str) -> float | None:
    data = payload(raw or "", pid)
    if data is None:
        return None
    try:
        if pid == "0D":
            return decode_speed_kmh(data) * MPH_PER_KMH
        if pid == "5E":
            return decode_fuel_rate_lph(data) / LITRES_PER_GAL
        if pid == "2F":
            return decode_fuel_level_pct(data)
    except ValueError:
        return None
    raise KeyError(pid)


def sample_from_responses(ts: datetime, responses: dict[str, str]) -> ObdSample:
    """One ObdSample from a polling round. The ECU answering at all means ignition on.

    Missing values stay None, never 0, so the fuel model can tell "no data"
    from "zero".
    """
    speed = _value(responses.get("0D"), "0D")
    rate_gph = _value(responses.get("5E"), "5E")
    level = _value(responses.get("2F"), "2F")
    # The adapter answers ATRV even with the ECU asleep, so it never implies ignition.
    ignition = any(v is not None for v in (speed, rate_gph, level))
    return ObdSample(
        ts=ts,
        ignition=ignition,
        speed_mph=speed,
        fuel_rate_gph=rate_gph,
        fuel_level_pct=level,
        battery_v=decode_adapter_volts(responses.get("ATRV", "")),
    )
