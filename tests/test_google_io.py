"""Google Routes edge: the POI drive-time lookup (#88). _post is faked."""

from __future__ import annotations

from typing import Any

import pytest

from bigair import google_io


def test_drive_seconds_asks_for_duration_only_between_two_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def fake_post(url: str, key: str, fields: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append((url, fields, body))
        return {"routes": [{"duration": "1500s"}]}

    monkeypatch.setattr(google_io, "_post", fake_post)
    assert google_io.drive_seconds("k", (41.0, -120.0), (41.0, -120.2)) == 1500.0
    url, fields, body = sent[0]
    assert url == google_io.ROUTES_URL
    assert fields == "routes.duration"
    assert body["origin"] == {"location": {"latLng": {"latitude": 41.0, "longitude": -120.0}}}
    assert body["destination"] == {"location": {"latLng": {"latitude": 41.0, "longitude": -120.2}}}


def test_drive_seconds_without_a_route_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_io, "_post", lambda *a: {})
    with pytest.raises(ValueError, match="no route"):
        google_io.drive_seconds("k", (41.0, -120.0), (10.0, 10.0))
