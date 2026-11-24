"""Edges for the live fuel plan (#71), with the network mocked at the import site."""

from __future__ import annotations

import asyncio
import io
import json
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from bigair import eia_io, google_io, mqtt_io
from bigair.fuel_sources import Segment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from urllib.request import Request


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def _capture(monkeypatch: pytest.MonkeyPatch, module: object, reply: dict[str, Any]) -> list[Any]:
    seen: list[Any] = []

    def fake(req: Request | str, timeout: float) -> _Resp:
        seen.append(req)
        return _Resp(json.dumps(reply).encode())

    monkeypatch.setattr(module, "urlopen", fake)
    return seen


def test_compute_route_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, google_io, {"routes": []})
    google_io.compute_route("KEY", "Seattle, WA", "Sonoma Raceway", ["Portland, OR"])
    req = seen[0]
    body = json.loads(req.data)
    assert req.full_url == google_io.ROUTES_URL
    assert req.get_header("X-goog-api-key") == "KEY"
    assert req.get_header("X-goog-fieldmask") == google_io.ROUTE_FIELDS
    assert body["origin"] == {"address": "Seattle, WA"}
    assert body["intermediates"] == [{"address": "Portland, OR"}]
    assert body["travelMode"] == "DRIVE"


def test_search_along_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, google_io, {"places": []})
    seg = Segment("abc", (40.0, -120.0), 0.0, 100.0)
    google_io.search_along("KEY", seg, "tok")
    body = json.loads(seen[0].data)
    assert body["searchAlongRouteParameters"] == {"polyline": {"encodedPolyline": "abc"}}
    assert body["routingParameters"]["origin"] == {"latitude": 40.0, "longitude": -120.0}
    assert body["pageToken"] == "tok"
    assert body["pageSize"] == 20
    assert "places.fuelOptions" in seen[0].get_header("X-goog-fieldmask")


def test_google_error_carries_body_not_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Request, timeout: float) -> _Resp:
        raise HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"API not enabled"}')
        )

    monkeypatch.setattr(google_io, "urlopen", boom)
    with pytest.raises(google_io.GoogleApiError, match="403.*API not enabled") as ei:
        google_io.compute_route("SECRET", "a", "b", [])
    assert "SECRET" not in str(ei.value)


def test_eia_url_and_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, eia_io, {"response": {"data": []}})
    assert eia_io.fetch_diesel("K", weeks=5) == {"response": {"data": []}}
    q = parse_qs(urlsplit(seen[0]).query)
    assert q["facets[product][]"] == ["EPD2D"]
    assert q["facets[duoarea][]"] == ["NUS", "R5XCA", "SCA"]
    assert q["length"] == ["15"]
    assert q["frequency"] == ["weekly"]


class _Msg:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _Client:
    def __init__(self, host: str, port: int, payloads: list[bytes]) -> None:
        self._payloads = payloads
        self.subscribed: list[str] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *a: object) -> None:
        pass

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> AsyncIterator[_Msg]:
        async def gen() -> AsyncIterator[_Msg]:
            for p in self._payloads:
                yield _Msg(p)
            await asyncio.sleep(10)

        return gen()


async def test_read_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mqtt_io.aiomqtt, "Client", lambda h, p: _Client(h, p, [b'{"x":1}']))
    assert await mqtt_io.read_retained("h", 1883, "t") == b'{"x":1}'


async def test_read_retained_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mqtt_io.aiomqtt, "Client", lambda h, p: _Client(h, p, []))
    assert await mqtt_io.read_retained("h", 1883, "t", timeout_s=0.05) is None
