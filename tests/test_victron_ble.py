"""Victron Instant Readout decoder (#109).

Layouts and scalings are from Victron's "Extra manufacturer data" spec
(wiki.victronenergy.com/rend/ble/extra_manufacturer_data, 2022-12-14). Records are
built here field by field from that table and encrypted with a made-up key. The
van's real keys never enter the repo; the on-van check against VictronConnect is
recorded in docs/pid-register.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bigair.victron_ble import (
    NotInstantReadout,
    VictronDecodeError,
    VictronKeyMismatch,
    decode,
)

TS = datetime(2026, 11, 26, 23, 40, tzinfo=UTC)
KEY = bytes.fromhex("0123456789abcdef0123456789abcdef")  # synthetic
BMV_MODEL = 0xA383  # BMV-712 Smart, as the van's advertises it


def _pack(fields: list[tuple[int, int]], total_bits: int) -> bytes:
    """(value, width) pairs into an LSB-first bitstream; unused bits set (spec)."""
    v = 0
    off = 0
    for value, width in fields:
        v |= (value & ((1 << width) - 1)) << off
        off += width
    v |= ((1 << (total_bits - off)) - 1) << off
    return v.to_bytes(total_bits // 8, "little")


def _advert(record_type: int, plain: bytes, nonce: int = 0xB935, key: bytes = KEY) -> bytes:
    nonce_b = nonce.to_bytes(2, "little")
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce_b + bytes(14))).encryptor()
    ct = enc.update(plain) + enc.finalize()
    return (
        bytes([0x10, 0x02])
        + BMV_MODEL.to_bytes(2, "little")
        + bytes([record_type])
        + nonce_b
        + key[:1]
        + ct
    )


def _signed(v: float, scale: float, width: int) -> int:
    return round(v / scale) & ((1 << width) - 1)


def _bmv(
    ttg_min: int = 3456,
    volts: float = 13.27,
    alarm: int = 0,
    aux: int = 0,
    aux_mode: int = 3,
    amps: float = -3.663,
    consumed_ah: float = 1.7,
    soc: float = 99.2,
) -> bytes:
    return _pack(
        [
            (ttg_min, 16),
            (_signed(volts, 0.01, 16), 16),
            (alarm, 16),
            (aux, 16),
            (aux_mode, 2),
            (_signed(amps, 0.001, 22), 22),
            (round(consumed_ah * 10), 20),
            (round(soc * 10), 10),
        ],
        128,
    )


def test_battery_monitor_record() -> None:
    """The van's own reading at 15:3x local on 2026-11-26, rebuilt from the spec."""
    s = decode(_advert(0x02, _bmv()), KEY, TS, 7.0)
    assert s.device == "bmv"
    assert s.ts == TS
    assert s.mono_s == 7.0
    assert s.voltage_v == pytest.approx(13.27)
    assert s.current_a == pytest.approx(-3.663)
    assert s.power_w == pytest.approx(13.27 * -3.663)
    assert s.soc_pct == pytest.approx(99.2)
    assert s.consumed_ah == pytest.approx(-1.7)  # spec: Consumed Ah = −record value
    assert s.time_to_go_s == 3456 * 60
    assert s.alarm == 0
    assert s.aux_voltage_v is None  # aux input 3 = none
    assert s.temperature_c is None


def test_battery_monitor_not_available_values_are_none() -> None:
    plain = _pack(
        [
            (0xFFFF, 16),
            (0x7FFF, 16),
            (0, 16),
            (0, 16),
            (3, 2),
            (0x3FFFFF, 22),  # NA is the all-ones pattern, not −1 mA
            (0xFFFFF, 20),
            (0x3FF, 10),
        ],
        128,
    )
    s = decode(_advert(0x02, plain), KEY, TS, 0.0)
    assert s.time_to_go_s is None
    assert s.voltage_v is None
    assert s.current_a is None
    assert s.power_w is None
    assert s.consumed_ah is None
    assert s.soc_pct is None


@pytest.mark.parametrize(
    ("mode", "raw", "field", "expected"),
    [
        (0, _signed(12.71, 0.01, 16), "aux_voltage_v", 12.71),  # starter battery
        (1, 664, "midpoint_voltage_v", 6.64),
        (2, 29815, "temperature_c", 25.0),  # 298.15 K
    ],
)
def test_battery_monitor_aux_input(mode: int, raw: int, field: str, expected: float) -> None:
    s = decode(_advert(0x02, _bmv(aux=raw, aux_mode=mode)), KEY, TS, 0.0)
    assert getattr(s, field) == pytest.approx(expected)


def test_charging_current_is_positive() -> None:
    s = decode(_advert(0x02, _bmv(amps=21.5)), KEY, TS, 0.0)
    assert s.current_a == pytest.approx(21.5)


def test_solar_charger_record() -> None:
    """VictronConnect on the van: 13.28 V, 0.5 A, 9 W, bulk, 60 Wh today."""
    plain = _pack(
        [
            (3, 8),  # bulk
            (0, 8),
            (_signed(13.28, 0.01, 16), 16),
            (_signed(0.5, 0.1, 16), 16),
            (6, 16),  # 0.06 kWh
            (9, 16),
            (0x1FF, 9),  # no load output
        ],
        128,
    )
    s = decode(_advert(0x01, plain), KEY, TS, 0.0)
    assert s.device == "mppt"
    assert s.state == "bulk"
    assert s.charger_error == 0
    assert s.voltage_v == pytest.approx(13.28)
    assert s.current_a == pytest.approx(0.5)
    assert s.yield_today_wh == pytest.approx(60.0)
    assert s.pv_power_w == 9
    assert s.load_current_a is None


def test_dcdc_converter_record() -> None:
    """VictronConnect on the van: Orion off, 12.72 V in, engine off."""
    plain = _pack(
        [(0, 8), (0, 8), (1272, 16), (0x7FFF, 16), (0x01, 32)],
        128,
    )
    s = decode(_advert(0x04, plain), KEY, TS, 0.0)
    assert s.device == "dcdc"
    assert s.state == "off"
    assert s.input_voltage_v == pytest.approx(12.72)
    assert s.voltage_v is None
    assert s.off_reason == 0x01  # no input power


def test_unknown_device_state_keeps_its_code() -> None:
    plain = _pack([(200, 8), (0, 8), (1272, 16), (1300, 16), (0, 32)], 128)
    assert decode(_advert(0x04, plain), KEY, TS, 0.0).state == "state_200"


def test_short_record_leaves_missing_fields_none() -> None:
    """Spec: check the length for every item; a shorter record isn't an error."""
    plain = _bmv()[:6]  # TTG, voltage, alarm only
    s = decode(_advert(0x02, plain), KEY, TS, 0.0)
    assert s.voltage_v == pytest.approx(13.27)
    assert s.current_a is None
    assert s.soc_pct is None


def test_the_nonce_is_part_of_the_cipher() -> None:
    a = decode(_advert(0x02, _bmv(), nonce=1), KEY, TS, 0.0)
    b = decode(_advert(0x02, _bmv(), nonce=0xFFFF), KEY, TS, 0.0)
    assert a.voltage_v == b.voltage_v == pytest.approx(13.27)


def test_wrong_key_is_rejected_by_the_check_byte() -> None:
    other = bytes.fromhex("ff" + "00" * 15)
    with pytest.raises(VictronKeyMismatch):
        decode(_advert(0x02, _bmv()), other, TS, 0.0)


@pytest.mark.parametrize(
    "adv",
    [
        b"",
        bytes([0x10, 0x02, 0x83, 0xA3, 0x02, 0x35, 0xB9]),  # header only
        bytes([0x11]) + bytes(12),  # not a product advertisement
    ],
)
def test_malformed_adverts_raise(adv: bytes) -> None:
    with pytest.raises(VictronDecodeError):
        decode(adv, KEY, TS, 0.0)


def test_unsupported_record_type_raises() -> None:
    with pytest.raises(VictronDecodeError):
        decode(_advert(0x03, bytes(16)), KEY, TS, 0.0)  # inverter: not in the van


def test_other_advertisement_types_are_told_apart() -> None:
    """Seen on the van: the BMV also sends 16–27-byte adverts that don't start 0x10."""
    with pytest.raises(NotInstantReadout):
        decode(bytes([0x11]) + bytes(20), KEY, TS, 0.0)
