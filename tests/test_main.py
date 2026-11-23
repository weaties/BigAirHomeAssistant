from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
