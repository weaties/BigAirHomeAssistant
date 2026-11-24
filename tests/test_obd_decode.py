"""Decoders from raw ELM327 responses captured on the van (docs/scans/2026-11-20-*)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bigair.obd_decode import (
    LITRES_PER_GAL,
    POLLED_PIDS,
    SLOW_PIDS,
    decode_fuel_level_pct,
    decode_fuel_rate_lph,
    decode_speed_kmh,
    payload,
    sample_from_responses,
)

TS = datetime(2026, 11, 20, 17, 50, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "pid", "expected"),
    [
        ("410D00", "0D", b"\x00"),
        ("41 0D 3F", "0D", b"\x3f"),  # spaces on
        ("7E8 03 41 0D 00", "0D", b"\x00"),  # headers on (scan write-up format)
        ("415E001F", "5E", b"\x00\x1f"),
        ("410D00\n", "0D", b"\x00"),
    ],
)
def test_payload_strips_header_and_mode(raw: str, pid: str, expected: bytes) -> None:
    assert payload(raw, pid) == expected


@pytest.mark.parametrize(
    "raw", ["NO DATA", "SEARCHING...", "?", "", "CAN ERROR", "410C0D78", "7F0112", "410D"]
)
def test_payload_is_none_for_anything_else(raw: str) -> None:
    assert payload(raw, "0D") is None


def test_speed_is_whole_kmh() -> None:
    assert decode_speed_kmh(b"\x40") == 64.0


def test_fuel_rate_from_the_idle_scan() -> None:
    # 2026-11-20 warm-up idle: 41 5E 00 1F → 31/20 = 1.55 L/h
    assert decode_fuel_rate_lph(b"\x00\x1f") == pytest.approx(1.55)


def test_fuel_level_from_the_idle_scan() -> None:
    # 41 2F 3F → 63·100/255 = 24.7 %
    assert decode_fuel_level_pct(b"\x3f") == pytest.approx(24.706, abs=1e-3)


@pytest.mark.parametrize(
    ("fn", "data"),
    [(decode_speed_kmh, b""), (decode_fuel_rate_lph, b"\x00"), (decode_fuel_level_pct, b"")],
)
def test_short_payloads_are_rejected(fn: object, data: bytes) -> None:
    with pytest.raises(ValueError):
        fn(data)  # type: ignore[operator]


def test_sample_converts_units() -> None:
    s = sample_from_responses(TS, {"0D": "410D40", "5E": "415E004B", "2F": "412F3F"})
    assert s.ts == TS
    assert s.ignition
    assert s.speed_mph == pytest.approx(64 * 0.621371)
    assert s.fuel_rate_gph == pytest.approx(75 / 20 / LITRES_PER_GAL)
    assert s.fuel_level_pct == pytest.approx(24.706, abs=1e-3)
    assert s.odometer_mi is None  # A6 is ABSENT on this van


def test_missing_values_stay_none_not_zero() -> None:
    s = sample_from_responses(TS, {"0D": "410D00", "5E": "NO DATA", "2F": "?"})
    assert s.ignition
    assert s.speed_mph == 0.0
    assert s.fuel_rate_gph is None
    assert s.fuel_level_pct is None


def test_ecu_silent_means_ignition_off() -> None:
    s = sample_from_responses(TS, dict.fromkeys(POLLED_PIDS, "NO DATA"))
    assert not s.ignition
    assert s.speed_mph is None and s.fuel_rate_gph is None


def test_only_register_signals_are_polled() -> None:
    """0D VERIFIED; 5E and 2F RESPONDS, used under Decisions #31/#32. Nothing else."""
    assert set(POLLED_PIDS) == {"0D", "5E", "2F"}
    # Temperatures (#79): RESPONDS in the 2026-11-20 scan, history only.
    assert set(SLOW_PIDS) == {"05", "5C", "46"}


def test_temperatures_are_a_minus_40() -> None:
    """Values from the 2026-11-20 scan: coolant 5E → 54 °C, oil 5A → 50 °C, ambient 2E → 6 °C."""
    s = sample_from_responses(TS, {"0D": "410D00", "05": "41055E", "5C": "415C5A", "46": "41462E"})
    assert (s.coolant_temp_c, s.oil_temp_c, s.ambient_temp_c) == (54.0, 50.0, 6.0)


def test_temperatures_absent_from_fast_rounds_stay_none() -> None:
    s = sample_from_responses(TS, {"0D": "410D00", "5C": "NO DATA"})
    assert (s.coolant_temp_c, s.oil_temp_c, s.ambient_temp_c) == (None, None, None)


def test_a_temperature_alone_means_the_ecu_is_awake() -> None:
    s = sample_from_responses(TS, {"0D": "NO DATA", "05": "41055E"})
    assert s.ignition
