"""Google Routes edge: the POI drive-time lookup (#88). _post is faked."""

from __future__ import annotations

from typing import Any

import pytest

from bigair import google_io


def test_drive_leg_asks_for_duration_only_between_two_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def fake_post(url: str, key: str, fields: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append((url, fields, body))
        return {"routes": [{"duration": "1500s", "distanceMeters": 32187}]}

    monkeypatch.setattr(google_io, "_post", fake_post)
    secs, miles = google_io.drive_leg("k", (41.0, -120.0), (41.0, -120.2))
    assert secs == 1500.0
    assert miles == pytest.approx(20.0, abs=0.01)
    url, fields, body = sent[0]
    assert url == google_io.ROUTES_URL
    assert fields == "routes.duration,routes.distanceMeters"
    assert body["origin"] == {"location": {"latLng": {"latitude": 41.0, "longitude": -120.0}}}
    assert body["destination"] == {"location": {"latLng": {"latitude": 41.0, "longitude": -120.2}}}


def test_drive_leg_without_a_route_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_io, "_post", lambda *a: {})
    with pytest.raises(ValueError, match="no route"):
        google_io.drive_leg("k", (41.0, -120.0), (10.0, 10.0))


def test_compute_route_can_avoid_highways(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(google_io, "_post", lambda u, k, f, body: sent.append(body) or {})
    google_io.compute_route("k", "A", "B", [])
    google_io.compute_route("k", "A", "B", [], avoid_highways=True)
    assert "routeModifiers" not in sent[0]
    assert sent[1]["routeModifiers"] == {"avoidHighways": True}
