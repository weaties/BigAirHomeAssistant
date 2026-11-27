"""Read-only InfluxQL edge (#94), with urlopen faked."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from bigair import influxql_io

if TYPE_CHECKING:
    from urllib.request import Request

    import pytest


def test_query_posts_db_and_query_with_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Resp(io.BytesIO):
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(req: Request, timeout: float) -> Resp:
        assert isinstance(req.data, bytes)
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["form"] = parse_qs(req.data.decode())
        return Resp(json.dumps({"results": [{}]}).encode())

    monkeypatch.setattr(influxql_io, "urlopen", fake_urlopen)
    assert influxql_io.query("tok", "van_raw", "SHOW MEASUREMENTS") == {"results": [{}]}
    assert seen["url"] == "http://localhost:8086/query"
    assert seen["auth"] == "Token tok"
    assert seen["form"] == {"db": ["van_raw"], "q": ["SHOW MEASUREMENTS"], "epoch": ["s"]}
