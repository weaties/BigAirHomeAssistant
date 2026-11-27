"""The tools Claude may use in the planner, and its system prompt (#89).

Every tool reads; only ``propose_route`` "writes", and all it does is record a
proposal that Dan accepts or dismisses on the page. Claude sees the POI notes,
the cached plan, the van's GPS position and the forecast (Dan approved those,
2026-11-25). It never sees his personal planning notes. The travel rules it
works to come from bigair's settings, in the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bigair.claude_agent import AgentContext, Tool
from bigair.destinations import Destination, crow_mi
from bigair.route_weather import conditions_at, decode_open_meteo, wmo_label
from bigair.telemetry import DBS, SCHEMA, shape_result, validate_influxql

if TYPE_CHECKING:
    from collections.abc import Callable

    from bigair.poi import Poi
    from bigair.travel_rules import TravelRules

MAX_POIS = 15
MAX_NOTE_CHARS = 4000


@dataclass(frozen=True)
class ToolDeps:
    position: Callable[[], tuple[float, float] | None]
    pois: Callable[[], list[Poi]]
    read_note: Callable[[str], str | None]
    """The body of a POI note (without frontmatter), or None."""
    last_plan: Callable[[], dict[str, Any] | None]
    forecast: Callable[[float, float, str], dict[str, Any] | None]
    """(lat, lon, ISO time) → conditions then, or None beyond the forecast."""
    places: Callable[[], list[Destination]] | None = None
    """iOverlander and public-land spots (#91), for find_destinations (#93)."""
    telemetry: Callable[[str, str], dict[str, Any]] | None = None
    """(database, InfluxQL) → InfluxQL JSON, over a read-only token (#94)."""


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _poi_brief(p: Poi, d: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": p.name,
        "lat": p.lat,
        "lon": p.lon,
        "tags": list(p.tags),
        "summary": p.summary,
        "visit_min": p.visit_min,
        "visit_what": p.visit_what,
        "best_months": list(p.best_months) if p.best_months else None,
        "destination": p.destination,
        "rating": p.rating,
    }
    if d is not None:
        out["distance_mi"] = round(d, 1)
    return out


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    day = plan.get("day") or {}
    prop = day.get("proposal") or {}
    req = plan.get("request") or {}
    route = plan.get("route") or {}
    return {
        "from": req.get("origin"),
        "to": req.get("destination"),
        "via": req.get("via"),
        "depart": plan.get("depart"),
        "mode": day.get("mode", "quick"),
        "route_mi": route.get("one_way_mi"),
        "drive_min": round(float(route.get("duration_s") or 0) / 60),
        "day_window_min": day.get("window_min"),
        "leg_fits": prop.get("fits"),
        "leg_problems": prop.get("problems"),
        "proposed_stops": prop.get("chosen"),
        "pois_near_route": [
            {k: h.get(k) for k in ("name", "mile", "detour_min", "visit_min", "score", "reasons")}
            for h in (plan.get("pois") or {}).get("hits", [])[:MAX_POIS]
        ],
        "warnings": plan.get("warnings"),
    }


def build_tools(deps: ToolDeps) -> list[Tool]:
    def where(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        pos = deps.position()
        return {"lat": pos[0], "lon": pos[1]} if pos else {"error": "no GPS fix right now"}

    def find(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        here = (float(a["lat"]), float(a["lon"]))
        tag = a.get("tag")
        found = []
        for p in deps.pois():
            if tag and tag not in p.tags:
                continue
            d = crow_mi(here, (p.lat, p.lon))
            if d <= float(a["radius_mi"]):
                found.append((d, p))
        found.sort(key=lambda x: x[0])
        return {"pois": [_poi_brief(p, d) for d, p in found[:MAX_POIS]], "total": len(found)}

    def details(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        name = str(a["name"])
        poi = next((p for p in deps.pois() if p.name == name), None)
        if poi is None:
            return {"error": f"no POI named {name!r}"}
        note = deps.read_note(name)
        return {**_poi_brief(poi), "note": (note or "")[:MAX_NOTE_CHARS]}

    def last_plan(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        plan = deps.last_plan()
        return _plan_summary(plan) if plan else {"error": "no plan yet"}

    def weather(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        out = deps.forecast(float(a["lat"]), float(a["lon"]), str(a["when"]))
        return out if out is not None else {"error": "beyond the forecast or unavailable"}

    def destinations(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        here = (float(a["lat"]), float(a["lon"]))
        vault = [
            Destination(p.name, p.lat, p.lon, "poi", None, p.summary, p)
            for p in deps.pois()
            if p.destination
        ]
        pool = [*vault, *(deps.places() if deps.places else [])]
        found = sorted(
            ((crow_mi(here, (d.lat, d.lon)), d) for d in pool),
            key=lambda x: x[0],
        )
        found = [(dist, d) for dist, d in found if dist <= float(a["radius_mi"])]
        out = []
        for dist, d in found[:MAX_POIS]:
            item: dict[str, Any] = {
                "name": d.name,
                "lat": round(d.lat, 4),
                "lon": round(d.lon, 4),
                "source": d.source,
                "kind": d.kind,
                "note": d.note,
                "distance_mi": round(dist, 1),
            }
            if d.poi is not None:
                item["rating"] = d.poi.rating
                item["best_months"] = list(d.poi.best_months) if d.poi.best_months else None
            out.append(item)
        return {"destinations": out, "total": len(found)}

    def propose(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        params = {"destination": a["destination"], "via": list(a["via"]), "mode": a["mode"]}
        pid = ctx.propose("route", str(a["title"]), params, str(a["reason"]))
        return {"status": "proposed; Dan decides on the planner page", "proposal_id": pid}

    def schema(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        return {"schema": SCHEMA}

    def query(ctx: AgentContext, a: dict[str, Any]) -> dict[str, Any]:
        assert deps.telemetry is not None
        db = str(a["db"])
        if db not in DBS:
            return {"error": f"db must be one of {', '.join(DBS)}"}
        try:
            q = validate_influxql(str(a["influxql"]))
        except ValueError as e:
            return {"error": str(e)}
        return shape_result(deps.telemetry(db, q))

    num = {"type": "number"}
    s = {"type": "string"}
    telemetry_tools = (
        [
            Tool(
                "telemetry_schema",
                "The van's InfluxDB measurements and fields (fuel, engine, heater, "
                "electrical, track, fill-ups, trips), with units.",
                _obj({}),
                schema,
            ),
            Tool(
                "query_telemetry",
                "Run one read-only InfluxQL SELECT or SHOW against van_raw or van_history. "
                "A SELECT needs a time range (e.g. WHERE time > now() - 1d) or a LIMIT; "
                "aggregate with GROUP BY time(...) to keep results small. Times are UTC.",
                _obj({"db": {"type": "string", "enum": list(DBS)}, "influxql": s}),
                query,
            ),
        ]
        if deps.telemetry is not None
        else []
    )
    return [
        *telemetry_tools,
        Tool("where_is_the_van", "The van's current GPS position.", _obj({}), where),
        Tool(
            "find_pois",
            "Dan's points of interest within radius_mi (straight line) of a point, nearest "
            "first, with visit time, best months, rating and whether it's a 3-night spot. "
            "tag filters by one tag (e.g. hot-springs, geology), or null for all.",
            _obj({"lat": num, "lon": num, "radius_mi": num, "tag": {"type": ["string", "null"]}}),
            find,
        ),
        Tool(
            "poi_details",
            "One POI's full note: camping, things to do, dog rules, services, what to verify.",
            _obj({"name": s}),
            details,
        ),
        Tool(
            "last_plan",
            "The plan currently on the planner page: route, day budget, POIs near the route "
            "with their scores, and the stops slow mode proposed.",
            _obj({}),
            last_plan,
        ),
        Tool(
            "find_destinations",
            "Places to spend a few nights within radius_mi (straight line) of a point, nearest "
            "first: Dan's POIs marked as 3-night spots, his iOverlander campsites, and BLM / "
            "Forest Service land. Road distance is longer; check weather_at for the forecast.",
            _obj({"lat": num, "lon": num, "radius_mi": num}),
            destinations,
        ),
        Tool(
            "weather_at",
            "The forecast at a point and time (ISO 8601 with offset): °F and chance of rain.",
            _obj({"lat": num, "lon": num, "when": s}),
            weather,
        ),
        Tool(
            "propose_route",
            "Suggest a route for Dan to accept or dismiss on the planner page. destination "
            "and via are places or 'lat,lon'; mode is 'slow' or 'quick'. Nothing changes "
            "until Dan accepts.",
            _obj(
                {
                    "title": s,
                    "destination": s,
                    "via": {"type": "array", "items": s},
                    "mode": {"type": "string", "enum": ["slow", "quick"]},
                    "reason": s,
                }
            ),
            propose,
        ),
    ]


def forecast_from(
    fetch: Callable[[list[tuple[float, float]]], list[dict[str, Any]]],
) -> Callable[[float, float, str], dict[str, Any] | None]:
    """A ``ToolDeps.forecast`` over the planner's Open-Meteo edge, one point at a time."""

    def at(lat: float, lon: float, when: str) -> dict[str, Any] | None:
        t = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        hourly = decode_open_meteo(fetch([(lat, lon)]))
        c = conditions_at(hourly[0], t) if hourly else None
        if c is None:
            return None
        return {
            "temp_f": round(c.temp_c * 9 / 5 + 32, 1),
            "precip_prob_pct": round(c.precip_prob_pct),
            "wind_mph": c.wind_mph,
            "gust_mph": c.gust_mph,
            "conditions": wmo_label(c.code),
        }

    return at


def system_prompt(rules: TravelRules) -> str:
    """Stable text (cache-friendly): who Claude is helping and the rules it plans to."""
    band = f"{rules.temp_band_f[0]:g}–{rules.temp_band_f[1]:g}°F"
    return f"""You help Dan plan drives in his camper van (a 4x4 Sprinter, usually with company \
and a dog). The trip is about beauty and interest, not speed.

Their travel rules:
- At most {rules.max_day_mi:.0f} mi of driving a day, detours included. That's a ceiling, \
never a target: short days are good, and a shorter, richer day beats a longer one.
- Leave around {rules.depart_h:02d}:{rules.depart_m:02d}, arrive by \
{rules.arrive_by_h:02d}:{rules.arrive_by_m:02d}, never drive after dark, then stay \
{rules.stay_nights} nights.
- Avoid interstates. Aim for {band} days, on public land where possible.

How to help:
- Use the tools to look things up; don't rely on memory for places, times or weather. \
Quote the numbers the tools give you. If something isn't known, say so plainly; never guess \
a number.
- You can't change the plan. To suggest a different route or stops, call propose_route \
once with a short title and reason; Dan accepts or dismisses it.
- Fuel and range come from the van's own tested model, not from you; never estimate them.
- Be brief and concrete: a few sentences or a short list, in plain words."""
