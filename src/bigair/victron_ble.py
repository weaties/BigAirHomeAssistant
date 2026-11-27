"""Victron Instant Readout (BLE advertisement) decoder (#109).

Source: Victron's "Extra manufacturer data" spec
(https://wiki.victronenergy.com/rend/ble/extra_manufacturer_data, 2022-12-14).

Manufacturer data for company 0x02E1, after the company id:

    byte 0     0x10 (product advertisement)
    bytes 1–3  prefix and model id (not needed to decode)
    byte 4     record type: 0x01 solar charger, 0x02 battery monitor, 0x04 DC/DC
    bytes 5–6  nonce, LSB first
    byte 7     byte 0 of the device's key (a check, not secret)
    bytes 8…   the record's data, AES-128-CTR encrypted

The CTR counter block is the two nonce bytes, then a counter starting at 0
(records are at most 16 bytes, so one block). The data is an LSB-first
bitstream. Field offsets below are the spec's start bits minus 32 (the
header's record type, nonce and key byte). The spec says to length-check every
field, because records may grow, so a field past the end is None, not an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bigair.signals import VictronSample

if TYPE_CHECKING:
    from datetime import datetime

COMPANY_ID = 0x02E1
PRODUCT_ADVERTISEMENT = 0x10
SOLAR_CHARGER = 0x01
BATTERY_MONITOR = 0x02
DCDC_CONVERTER = 0x04
HEADER_BYTES = 8

# VE_REG_DEVICE_STATE, as in Victron's VE.Direct protocol ("CS" field).
DEVICE_STATES = {
    0: "off",
    1: "low_power",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize",
    9: "inverting",
    11: "power_supply",
    245: "starting_up",
    246: "repeated_absorption",
    247: "auto_equalize",
    248: "battery_safe",
    252: "external_control",
}


class VictronDecodeError(ValueError):
    """Not a decodable Instant Readout advertisement."""


class NotInstantReadout(VictronDecodeError):
    """One of the device's other advertisement types: normal, and ignored."""


class VictronKeyMismatch(VictronDecodeError):
    """The advertisement's key byte doesn't match ours (key changed with the PIN?)."""


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.value = int.from_bytes(data, "little")
        self.nbits = len(data) * 8

    def get(self, off: int, width: int, na: int | None = None, signed: bool = False) -> int | None:
        if off + width > self.nbits:
            return None
        raw = (self.value >> off) & ((1 << width) - 1)
        if na is not None and raw == na:  # NA is a raw bit pattern, checked before sign
            return None
        if signed and raw >> (width - 1):
            raw -= 1 << width
        return raw


def _scaled(v: int | None, scale: float) -> float | None:
    return None if v is None else round(v * scale, 6)


def decrypt(adv: bytes, key: bytes) -> tuple[int, bytes]:
    """(record type, decrypted data) from the manufacturer data."""
    if adv[:1] != bytes([PRODUCT_ADVERTISEMENT]):
        raise NotInstantReadout(f"advertisement type {adv[:1].hex() or 'empty'}")
    if len(adv) <= HEADER_BYTES:
        raise VictronDecodeError(f"Instant Readout too short ({len(adv)} bytes)")
    if adv[7] != key[0]:
        raise VictronKeyMismatch(f"key byte {adv[7]:#04x} != {key[0]:#04x}")
    nonce = adv[5:7]
    dec = Cipher(algorithms.AES(key), modes.CTR(nonce + bytes(14))).decryptor()
    return adv[4], dec.update(adv[HEADER_BYTES:]) + dec.finalize()


def _state(code: int | None) -> str | None:
    return None if code is None else DEVICE_STATES.get(code, f"state_{code}")


def decode(adv: bytes, key: bytes, ts: datetime, mono_s: float) -> VictronSample:
    """One VictronSample from a device's manufacturer data and its 16-byte key."""
    record, data = decrypt(adv, key)
    b = _Bits(data)
    if record == BATTERY_MONITOR:
        volts = _scaled(b.get(16, 16, 0x7FFF, signed=True), 0.01)
        amps = _scaled(b.get(66, 22, 0x3FFFFF, signed=True), 0.001)
        aux_mode = b.get(64, 2, 0x3)
        aux_signed = b.get(48, 16, signed=True)
        aux = b.get(48, 16)
        consumed = b.get(88, 20, 0xFFFFF)
        ttg = b.get(0, 16, 0xFFFF)
        return VictronSample(
            ts=ts,
            mono_s=mono_s,
            device="bmv",
            voltage_v=volts,
            current_a=amps,
            power_w=None if volts is None or amps is None else round(volts * amps, 3),
            soc_pct=_scaled(b.get(108, 10, 0x3FF), 0.1),
            consumed_ah=None if consumed is None else -round(consumed * 0.1, 1),
            time_to_go_s=None if ttg is None else ttg * 60,
            alarm=b.get(32, 16),
            aux_voltage_v=_scaled(aux_signed, 0.01) if aux_mode == 0 else None,
            midpoint_voltage_v=_scaled(aux, 0.01) if aux_mode == 1 else None,
            temperature_c=(None if aux_mode != 2 or aux is None else round(aux * 0.01 - 273.15, 2)),
        )
    if record == SOLAR_CHARGER:
        yield_10wh = b.get(48, 16, 0xFFFF)
        return VictronSample(
            ts=ts,
            mono_s=mono_s,
            device="mppt",
            state=_state(b.get(0, 8, 0xFF)),
            charger_error=b.get(8, 8, 0xFF),
            voltage_v=_scaled(b.get(16, 16, 0x7FFF, signed=True), 0.01),
            current_a=_scaled(b.get(32, 16, 0x7FFF, signed=True), 0.1),
            yield_today_wh=None if yield_10wh is None else float(yield_10wh * 10),
            pv_power_w=b.get(64, 16, 0xFFFF),
            load_current_a=_scaled(b.get(80, 9, 0x1FF), 0.1),
        )
    if record == DCDC_CONVERTER:
        return VictronSample(
            ts=ts,
            mono_s=mono_s,
            device="dcdc",
            state=_state(b.get(0, 8, 0xFF)),
            charger_error=b.get(8, 8, 0xFF),
            input_voltage_v=_scaled(b.get(16, 16, 0xFFFF), 0.01),
            voltage_v=_scaled(b.get(32, 16, 0x7FFF, signed=True), 0.01),
            off_reason=b.get(48, 32),
        )
    raise VictronDecodeError(f"record type {record:#04x} isn't decoded")
