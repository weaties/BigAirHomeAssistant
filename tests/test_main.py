from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


from bigair.main import main


def test_main_returns_zero() -> None:
    assert main([]) == 0


def test_fuel_plan_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    trip = tmp_path / "trip.json"
    trip.write_text(
        json.dumps(
            {
                "route_mi": 300,
                "stations": [
                    {"name": "Town A", "mile": 50, "price_per_gal": 4.0},
                    {"name": "Town B", "mile": 150, "price_per_gal": 3.0},
                ],
            }
        )
    )
    rc = main(
        [
            "fuel-plan",
            str(trip),
            "--tank-gal",
            "10",
            "--mpg",
            "10",
            "--capacity",
            "20",
            "--reserves",
            "20,0",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "20 mi" in out
    assert "Town B" in out


def test_fuel_plan_live_wiring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--from/--to/--live wiring with every edge faked (no network)."""
    import json

    from bigair import eia_io, google_io, mqtt_io
    from bigair.fuel_sources import encode_polyline

    poly = encode_polyline([(40.0 + i * 0.1, -120.0) for i in range(21)])
    route = {
        "routes": [
            {
                "distanceMeters": 160934 * 3,
                "duration": "18000s",
                "polyline": {"encodedPolyline": poly},
            }
        ]
    }
    place = {
        "id": "p1",
        "displayName": {"text": "Love's Travel Stop"},
        "location": {"latitude": 41.0, "longitude": -120.0},
        "formattedAddress": "1 Main St, Town, OR 97000, USA",
        "fuelOptions": {
            "fuelPrices": [
                {
                    "type": "DIESEL",
                    "price": {"currencyCode": "USD", "units": "6", "nanos": 0},
                    "updateTime": "2099-01-01T00:00:00Z",
                }
            ]
        },
    }
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "K")
    monkeypatch.setattr(google_io, "compute_route", lambda *a: route)
    monkeypatch.setattr(google_io, "search_along", lambda *a: {"places": [place]})
    monkeypatch.setattr(eia_io, "fetch_diesel", lambda *a: {"response": {"data": []}})

    async def fake_read(*a: object) -> bytes:
        return json.dumps(
            {
                "tank_gal": 30.0,
                "mpg_rolling": 15.0,
                "calibrating": True,
                "stale": False,
                "fill_pending": False,
            }
        ).encode()

    monkeypatch.setattr(mqtt_io, "read_retained", fake_read)
    saved = tmp_path / "trip.json"
    rc = main(
        [
            "fuel-plan",
            "--from",
            "A",
            "--to",
            "B",
            "--round-trip",
            "--live",
            "--reserves",
            "50",
            "--save",
            str(saved),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Love's Travel Stop, Town, OR ↑" in out
    assert "Open Roads −$0.30" in out
    assert "calibrating" in out
    trip = json.loads(saved.read_text())
    assert trip["route_mi"] == pytest.approx(620.0, abs=0.01)
    assert trip["stations"][0]["price_per_gal"] == 5.7


def test_fuel_plan_refuses_stale_live_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from bigair import mqtt_io

    async def fake_read(*a: object) -> bytes:
        return json.dumps({"tank_gal": 30.0, "mpg_rolling": 15.0, "stale": True}).encode()

    monkeypatch.setattr(mqtt_io, "read_retained", fake_read)
    trip = tmp_path / "t.json"
    trip.write_text(json.dumps({"route_mi": 100, "stations": []}))
    assert main(["fuel-plan", str(trip), "--live"]) == 2
    assert "refused" in capsys.readouterr().out
