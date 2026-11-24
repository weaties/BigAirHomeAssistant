"""Live fuel-plan inputs (#71): polyline maths, Places/EIA decoders, member pricing.

Payloads are synthetic but follow the documented shapes (Places API (New)
searchText, Routes API computeRoutes, EIA API v2 petroleum/pri/gnd).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigair.fuel_sources import (
    Candidate,
    RouteInfo,
    brand_of,
    build_stations,
    decode_eia,
    decode_places,
    decode_polyline,
    decode_route,
    drift_per_day,
    encode_polyline,
    mirror_return,
    money,
    parse_duration,
    region_for_state,
    segments,
)

# Google's documented polyline example.
GOOGLE_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
GOOGLE_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_decode_polyline_matches_google_example() -> None:
    got = decode_polyline(GOOGLE_EXAMPLE)
    assert [c for pt in got for c in pt] == pytest.approx([c for pt in GOOGLE_POINTS for c in pt])


def test_encode_round_trips() -> None:
    assert encode_polyline(GOOGLE_POINTS) == GOOGLE_EXAMPLE


def _straight_route(miles: float = 138.0) -> RouteInfo:
    # Due north along a meridian: 1° latitude ≈ 69.09 mi.
    pts = [(40.0 + i * 0.1, -120.0) for i in range(21)]  # 2° ≈ 138 mi
    return RouteInfo.from_points(pts, road_mi=miles, duration_s=7200.0)


def test_route_scales_to_road_distance() -> None:
    r = _straight_route(150.0)  # road is a bit longer than the straight line
    assert r.total_mi == pytest.approx(150.0)
    assert r.cum_mi[-1] == pytest.approx(150.0)


def test_project_point_onto_route() -> None:
    r = _straight_route(138.18)
    mile, off = r.project(41.0, -120.01)  # halfway up, ~0.5 mi west
    assert mile == pytest.approx(69.09, abs=0.3)
    assert off == pytest.approx(0.52, abs=0.05)


def test_decode_route() -> None:
    body = {
        "routes": [
            {
                "distanceMeters": 160934,
                "duration": "6000s",
                "polyline": {"encodedPolyline": encode_polyline([(40.0, -120.0), (41.0, -120.0)])},
            }
        ]
    }
    r = decode_route(body)
    assert r.total_mi == pytest.approx(100.0, abs=0.01)
    assert r.duration_s == 6000.0


def test_decode_route_without_routes_raises() -> None:
    with pytest.raises(ValueError, match="no route"):
        decode_route({})


def test_segments_cover_route() -> None:
    r = _straight_route(138.18)
    segs = segments(r, seg_mi=50.0)
    assert segs[0].start_mi == 0.0
    assert segs[-1].end_mi == pytest.approx(r.total_mi)
    assert all(a.end_mi == pytest.approx(b.start_mi) for a, b in zip(segs, segs[1:], strict=False))
    assert decode_polyline(segs[0].encoded)[0] == pytest.approx((40.0, -120.0))


def test_parse_duration_and_money() -> None:
    assert parse_duration("3.5s") == 3.5
    assert money({"currencyCode": "USD", "units": "6", "nanos": 790000000}) == pytest.approx(6.79)


@pytest.mark.parametrize(
    ("name", "brand"),
    [
        ("Love's Travel Stop", "loves"),
        ("Loves Travel Stop", "loves"),
        ("TA Corning", "ta"),
        ("TravelCenters of America", "ta"),
        ("TA Express", "ta"),
        ("Petro Stopping Center", "petro"),
        ("Pilot Travel Center", None),
        ("Petrol Plus", None),
        ("Tacos & Gas", None),
    ],
)
def test_brand_of(name: str, brand: str | None) -> None:
    assert brand_of(name) == brand


def _place(
    pid: str,
    name: str,
    lat: float,
    lon: float,
    prices: list[tuple[str, str, int, str]],
    address: str = "1 Main St, Town, OR 97000, USA",
) -> dict[str, object]:
    return {
        "id": pid,
        "displayName": {"text": name, "languageCode": "en"},
        "location": {"latitude": lat, "longitude": lon},
        "formattedAddress": address,
        "fuelOptions": {
            "fuelPrices": [
                {
                    "type": t,
                    "price": {"currencyCode": "USD", "units": u, "nanos": n},
                    "updateTime": ts,
                }
                for t, u, n, ts in prices
            ]
        },
    }


NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
FRESH = "2026-11-23T15:00:00Z"


def test_decode_places_prefers_diesel_and_computes_detour() -> None:
    r = _straight_route(138.18)
    seg = segments(r, seg_mi=200.0)[0]  # whole route, 138.18 mi
    body = {
        "places": [
            _place("p1", "Love's Travel Stop", 41.0, -120.0, [("DIESEL", "6", 490000000, FRESH)]),
            _place(
                "p2",
                "Corner Gas",
                40.5,
                -120.0,
                [("REGULAR_UNLEADED", "5", 0, FRESH), ("TRUCK_DIESEL", "6", 900000000, FRESH)],
            ),
            _place("p3", "Gas Only", 40.6, -120.0, [("REGULAR_UNLEADED", "5", 0, FRESH)]),
        ],
        "routingSummaries": [
            # origin → place + place → end = route + 2 mi
            {"legs": [{"distanceMeters": 112654}, {"distanceMeters": 112654}]},
            {"legs": []},
            {},
        ],
    }
    cands = decode_places(body, r, seg)
    assert [c.place_id for c in cands] == ["p1", "p2"]  # p3 sells no diesel
    loves, corner = cands
    assert loves.price_per_gal == pytest.approx(6.49)
    assert loves.price_kind == "DIESEL"
    assert loves.mile == pytest.approx(69.09, abs=0.3)
    assert loves.detour_mi == pytest.approx(2 * 70.0 - 138.18, abs=0.05)
    assert loves.state == "OR"
    assert loves.city == "Town"
    assert corner.price_kind == "TRUCK_DIESEL"
    assert corner.detour_mi == pytest.approx(0.0, abs=0.05)  # on the line, no summary


def test_decode_places_skips_places_without_fuel_options() -> None:
    r = _straight_route()
    seg = segments(r, seg_mi=200.0)[0]
    body = {"places": [{"id": "x", "displayName": {"text": "X"}, "location": {}}]}
    assert decode_places(body, r, seg) == []


EIA_BODY = {
    "response": {
        "data": [
            {"period": p, "duoarea": a, "product": "EPD2D", "value": v, "units": "$/GAL"}
            for p, a, v in [
                ("2026-11-16", "SCA", "7.733"),
                ("2026-11-09", "SCA", "7.897"),
                ("2026-11-02", "SCA", "7.95"),
                ("2026-10-26", "SCA", "8.0"),
                ("2026-10-19", "SCA", "8.013"),
                ("2026-11-16", "R5XCA", "6.269"),
                ("2026-11-09", "R5XCA", "6.429"),
            ]
        ]
    }
}


def test_decode_eia_and_drift() -> None:
    series = decode_eia(EIA_BODY)
    assert series["SCA"][0] == (datetime(2026, 11, 16, tzinfo=UTC).date(), 7.733)
    # 4 weeks: (7.733 − 8.013) / 28 days
    assert drift_per_day(series["SCA"]) == pytest.approx((7.733 - 8.013) / 28)
    # Only 1 week of history: uses what's there (−2.3c/day, so capped at −2c).
    assert drift_per_day(series["R5XCA"]) == pytest.approx(-0.02)
    assert drift_per_day([]) == 0.0


def test_drift_is_capped() -> None:
    d0 = datetime(2026, 11, 16, tzinfo=UTC).date()
    wild = [(d0, 9.0), (d0 - timedelta(days=7), 7.0)]
    assert drift_per_day(wild) == pytest.approx(0.02)  # cap


@pytest.mark.parametrize(
    ("state", "area"), [("CA", "SCA"), ("OR", "R5XCA"), ("WA", "R5XCA"), ("TX", "NUS")]
)
def test_region_for_state(state: str, area: str) -> None:
    assert region_for_state(state) == area


def _cand(**kw: object) -> Candidate:
    base: dict[str, object] = {
        "place_id": "p",
        "name": "Love's Travel Stop",
        "state": "OR",
        "city": "Bend",
        "mile": 50.0,
        "detour_mi": 1.0,
        "price_per_gal": 6.50,
        "price_kind": "DIESEL",
        "updated": NOW - timedelta(hours=3),
    }
    base.update(kw)
    return Candidate(**base)  # type: ignore[arg-type]


def test_build_stations_applies_member_discount_drift_and_notes() -> None:
    r = _straight_route(100.0)  # 7200 s for 100 mi
    stations = build_stations(
        [_cand(mile=50.0)],
        route=r,
        now=NOW,
        depart=NOW + timedelta(days=1),
        drifts={"R5XCA": -0.02},
        discounts={"loves": 0.30},
    )
    (s,) = stations
    # ETA = 1 day + 1 h after now → drift −0.02 × (25/24) d
    assert s.price_per_gal == pytest.approx(6.50 - 0.30 - 0.02 * 25 / 24, abs=0.001)
    assert s.mile == 50.0
    assert "Open Roads −$0.30" in s.note
    assert "3 h old" in s.note
    assert s.detour_min > 0


def test_build_stations_dedupes_and_drops_stale_prices() -> None:
    r = _straight_route(100.0)
    stations = build_stations(
        [
            _cand(place_id="a", name="Pilot"),
            _cand(place_id="a", name="Pilot"),  # same place from overlapping segments
            _cand(place_id="b", name="Old", updated=NOW - timedelta(hours=100)),
        ],
        route=r,
        now=NOW,
        depart=NOW,
        drifts={},
        discounts={"loves": 0.30},
        max_age_h=72.0,
    )
    assert [s.name for s in stations] == ["Pilot, Bend, OR"]
    assert stations[0].price_per_gal == pytest.approx(6.50)  # Pilot: no member discount


def test_mirror_return_adds_return_leg() -> None:
    r = _straight_route(100.0)
    out = build_stations([_cand(mile=30.0)], r, NOW, NOW, {}, {})
    both = mirror_return(out, out_mi=100.0, local_mi=10.0)
    assert [(s.name, s.mile) for s in both] == [
        ("Love's Travel Stop, Bend, OR ↓", 30.0),
        ("Love's Travel Stop, Bend, OR ↑", 180.0),
    ]


def test_collect_candidates_runs_each_search_on_each_segment() -> None:
    from bigair.fuel_sources import collect_candidates

    r = _straight_route(138.18)
    calls: list[tuple[str | None, str, str | None]] = []

    def search(seg: object, token: str | None, q: str, typ: str | None) -> dict[str, object]:
        calls.append((token, q, typ))
        if q == "diesel" and token is None:
            p = _place("p1", "Corner Gas", 40.5, -120.0, [("DIESEL", "6", 0, FRESH)])
            return {"places": [p], "nextPageToken": "t2"}
        if q == "truck stop":
            priced = _place("p2", "Love's Travel Stop", 40.8, -120.0, [("DIESEL", "6", 0, FRESH)])
            bare = _place("p3", "Petro Travel Center", 41.2, -120.0, [])
            return {"places": [priced, bare]}
        return {"places": []}

    found = collect_candidates(r, search, seg_mi=100.0)
    assert len(segments(r, 100.0)) == 2
    assert calls[:3] == [
        (None, "diesel", "gas_station"),
        ("t2", "diesel", "gas_station"),
        (None, "truck stop", None),
    ]
    assert len(calls) == 6
    assert {c.place_id for c in found.candidates} == {"p1", "p2"}
    # Unpriced Open Roads stations are reported once, not planned with.
    assert [u.name for u in found.unpriced] == ["Petro Travel Center"]
    assert found.unpriced[0].mile == pytest.approx(82.9, abs=0.5)


def test_unpriced_ignores_non_member_brands() -> None:
    from bigair.fuel_sources import decode_unpriced_members

    body = {"places": [_place("p", "Rest Area", 40.5, -120.0, [])]}
    assert decode_unpriced_members(body, _straight_route()) == []


def test_parse_discounts() -> None:
    from bigair.fuel_sources import parse_discounts

    assert parse_discounts("loves=0.30, Love's=0.4,ta=0.35") == {"loves": 0.4, "ta": 0.35}
    assert parse_discounts("none") == {}
    with pytest.raises(ValueError, match="unknown brand"):
        parse_discounts("pilot=0.1")


def test_live_fuel_from_state() -> None:
    from bigair.fuel_sources import live_fuel_from_state

    payload = (
        b'{"calibrating": true, "fill_pending": false, "mpg_rolling": 15.0, '
        b'"range_mi": 656.0, "stale": false, "tank_gal": 43.71}'
    )
    f = live_fuel_from_state(payload)
    assert (f.tank_gal, f.mpg, f.calibrating, f.stale) == (43.71, 15.0, True, False)


def test_unpriced_skips_repair_shops_and_priced_duplicates() -> None:
    from bigair.fuel_sources import collect_candidates

    r = _straight_route(138.18)

    def search(seg: object, token: str | None, q: str, typ: str | None) -> dict[str, object]:
        return {
            "places": [
                _place("a", "Love's Travel Stop", 41.0, -120.0, [("DIESEL", "6", 0, FRESH)]),
                _place("b", "Love's", 41.005, -120.0, []),  # same stop, second listing
                _place("c", "Love's Truck Care", 41.5, -120.0, []),
                _place("d", "TA Truck Service", 41.6, -120.0, []),
                _place("e", "TA Travel Center", 41.7, -120.0, []),
            ]
        }

    found = collect_candidates(r, search, seg_mi=200.0, searches=(("diesel", None, 1),))
    assert [u.name for u in found.unpriced] == ["TA Travel Center"]
