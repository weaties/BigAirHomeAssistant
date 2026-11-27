"""Edge: read-only InfluxQL over HTTP for "ask the van" (#94).

Uses InfluxDB 2.7's v1-compatible ``/query`` endpoint (DBRP mappings van_raw and
van_history) with the **read-only** Grafana token, so even a query that got past
``telemetry.validate_influxql`` couldn't write. The token is never logged.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TIMEOUT_S = 15.0


def query(token: str, db: str, q: str, base: str = "http://localhost:8086") -> dict[str, Any]:
    req = Request(  # noqa: S310 — fixed local http URL
        f"{base}/query",
        data=urlencode({"db": db, "q": q, "epoch": "s"}).encode(),
        headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
        body: dict[str, Any] = json.loads(resp.read())
        return body
