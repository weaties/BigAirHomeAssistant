"""Fuel-stop planner: where to fill, and how much, along a fixed route (#70).

Pure logic, with frozen dataclasses in and out. Prices and routes come from the
caller (a trip file today; live edges in #71).

The planner is a dynamic program over (station, gallons on board) on a grid of
``step_gal``. It minimises:

    Σ price × gallons bought  +  stop penalty  +  detour time
        −  value of the fuel left at the destination

subject to one hard rule: fuel on board never drops below the reserve, at any
station or at the destination. Rounding is always conservative. The start tank
rounds down to the grid and every leg's fuel use rounds up.

Safety:
- stale fuel data raises ``PlanRefusedError``; a wrong plan is worse than none
- while the tank is ``calibrating``, plans use ``mpg × calibrating_mpg_factor``
- an infeasible route returns ``feasible=False`` with a reason, never a guess

``plan_options`` runs the planner once per reserve depth, so the driver can see
what each mile of reserve given up saves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

_EPS = 1e-9


class PlanRefusedError(ValueError):
    """The inputs can't support a trustworthy plan (e.g. stale fuel data)."""


@dataclass(frozen=True)
class Station:
    name: str
    mile: float
    """Distance along the route where the station's exit is."""
    price_per_gal: float
    """Expected price when the van gets there (drift is applied by the caller)."""
    detour_mi: float = 0.0
    """Extra miles, round trip, to get from the route to the pump and back."""
    detour_min: float = 0.0
    """Extra minutes, round trip, including any queue."""
    diesel: bool = True
    note: str = ""
    """Shown beside the stop, e.g. member discount and price age."""


@dataclass(frozen=True)
class PlanInput:
    route_mi: float
    stations: tuple[Station, ...]
    start_gal: float
    mpg: float
    capacity_gal: float = 47.0
    reserve_mi: float = 150.0
    stop_penalty_usd: float = 10.0
    """Cost of the hassle of one stop. It stops the plan making pointless top-ups."""
    end_value_per_gal: float = 0.0
    """What a gallon left in the tank at the destination is worth (usually the home price)."""
    time_value_usd_per_hr: float = 20.0
    min_buy_gal: float = 3.0
    step_gal: float = 0.5
    calibrating: bool = False
    calibrating_mpg_factor: float = 0.93
    stale: bool = False

    @property
    def planning_mpg(self) -> float:
        return self.mpg * self.calibrating_mpg_factor if self.calibrating else self.mpg

    @property
    def reserve_gal(self) -> float:
        return self.reserve_mi / self.planning_mpg


@dataclass(frozen=True)
class Stop:
    station: Station
    arrive_gal: float
    """Fuel on board at the pump, before buying."""
    buy_gal: float

    @property
    def cost_usd(self) -> float:
        return self.buy_gal * self.station.price_per_gal


@dataclass(frozen=True)
class Plan:
    reserve_mi: float
    feasible: bool
    stops: tuple[Stop, ...] = ()
    end_gal: float = 0.0
    min_gal: float = 0.0
    """Lowest fuel on board anywhere on the route."""
    net_cost_usd: float = math.inf
    """Fuel bought + stop penalties + detour time − value of the fuel left at the end."""
    reason: str = ""
    extras: dict[str, float] = field(default_factory=dict)

    @property
    def fuel_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.stops)

    @property
    def fuel_bought_gal(self) -> float:
        return sum(s.buy_gal for s in self.stops)


@dataclass(frozen=True)
class _Choice:
    prev_level: int
    stop: bool
    arrive: int = 0
    """Units at the pump before buying, when stopping."""
    bought: int = 0


def _check(inp: PlanInput) -> None:
    if inp.stale:
        raise PlanRefusedError("fuel data is stale; refusing to plan")
    if inp.mpg <= 0 or inp.capacity_gal <= 0 or inp.step_gal <= 0:
        raise PlanRefusedError("mpg, capacity and step must be positive")


def _route_stations(inp: PlanInput) -> list[Station]:
    return sorted(
        (s for s in inp.stations if s.diesel and 0.0 <= s.mile <= inp.route_mi),
        key=lambda s: s.mile,
    )


def _offer(
    nxt: dict[int, float], choice: dict[int, _Choice], level: int, cost: float, c: _Choice
) -> None:
    """Keep the cheapest way to leave a station with ``level`` units on board."""
    if cost < nxt.get(level, math.inf) - _EPS:
        nxt[level] = cost
        choice[level] = c


def plan(inp: PlanInput) -> Plan:
    """The cheapest plan that never enters the reserve, or ``feasible=False`` with a reason."""
    _check(inp)
    step = inp.step_gal
    mpg = inp.planning_mpg

    def burn(miles: float) -> int:  # conservative: round fuel use up
        return math.ceil(miles / mpg / step - _EPS) if miles > 0 else 0

    def burn_between(a: float, b: float) -> int:
        # Round the distance from the start, not each leg, so rounding never
        # piles up across many stations. Grid fuel stays ≤ real fuel everywhere:
        # grid use from mile 0 to m is ceil(m), which is ≥ the real use.
        return burn(b) - burn(a)

    cap = math.floor(inp.capacity_gal / step + _EPS)
    reserve = math.ceil(inp.reserve_gal / step - _EPS)
    min_buy = max(1, math.ceil(inp.min_buy_gal / step - _EPS))
    start = min(math.floor(inp.start_gal / step + _EPS), cap)
    if start < reserve:
        return Plan(
            inp.reserve_mi,
            False,
            reason=f"starts with {start * step:.1f} gal, already inside the "
            f"{inp.reserve_gal:.1f} gal reserve",
        )

    stations = _route_stations(inp)
    # Cost so far, by fuel level on departure from the current node.
    best: dict[int, float] = {start: 0.0}
    trail: list[dict[int, _Choice]] = []
    pos = 0.0
    for st in stations:
        leg = burn_between(pos, st.mile)
        half = burn(st.detour_mi / 2)
        stop_cost = inp.stop_penalty_usd + st.detour_min / 60.0 * inp.time_value_usd_per_hr
        nxt: dict[int, float] = {}
        choice: dict[int, _Choice] = {}

        for dep, cost in best.items():
            arrive = dep - leg
            if arrive < reserve:
                continue
            _offer(nxt, choice, arrive, cost, _Choice(dep, stop=False))
            at_pump = arrive - half
            if at_pump < reserve:
                continue
            for level in range(at_pump + min_buy, cap + 1):
                bought = level - at_pump
                leave = level - half
                c = cost + stop_cost + bought * step * st.price_per_gal
                _offer(
                    nxt, choice, leave, c, _Choice(dep, stop=True, arrive=at_pump, bought=bought)
                )
        if not nxt:
            return Plan(
                inp.reserve_mi,
                False,
                reason=f"can't reach {st.name} (mile {st.mile:.0f}) without entering the reserve",
            )
        best, pos = nxt, st.mile
        trail.append(choice)

    last = burn_between(pos, inp.route_mi)
    finish = {
        dep: cost - (dep - last) * step * inp.end_value_per_gal
        for dep, cost in best.items()
        if dep - last >= reserve
    }
    if not finish:
        where = stations[-1].name if stations else "the start"
        return Plan(
            inp.reserve_mi,
            False,
            reason=f"can't reach the destination from {where} without entering the reserve",
        )
    level = min(finish, key=lambda k: finish[k])
    net = finish[level]
    end_units = level - last

    stops: list[Stop] = []
    lows = [end_units]
    for st, choice in zip(reversed(stations), reversed(trail), strict=True):
        pick = choice[level]
        if pick.stop:
            stops.append(Stop(st, pick.arrive * step, pick.bought * step))
            lows.append(pick.arrive)
        level = pick.prev_level
    stops.reverse()
    return Plan(
        inp.reserve_mi,
        True,
        stops=tuple(stops),
        end_gal=end_units * step,
        min_gal=min(lows) * step,
        net_cost_usd=net,
    )


def plan_options(inp: PlanInput, reserves_mi: tuple[float, ...]) -> list[Plan]:
    """One plan per reserve depth, in the order given."""
    return [plan(replace(inp, reserve_mi=r)) for r in reserves_mi]


def naive_plan(inp: PlanInput) -> Plan:
    """Baseline: fill to full at the first station at or below a quarter tank.

    Ignores the reserve and prices, which is how a driver without a plan fuels.
    """
    _check(inp)
    mpg = inp.planning_mpg
    fuel = min(inp.start_gal, inp.capacity_gal)
    pos = 0.0
    low = fuel
    stops: list[Stop] = []
    penalties = 0.0
    for st in _route_stations(inp):
        fuel -= (st.mile - pos) / mpg
        pos = st.mile
        low = min(low, fuel)
        if fuel < 0:
            return Plan(inp.reserve_mi, False, tuple(stops), reason=f"runs dry before {st.name}")
        if fuel <= inp.capacity_gal / 4 + _EPS:
            fuel -= st.detour_mi / 2 / mpg
            stops.append(Stop(st, fuel, inp.capacity_gal - fuel))
            penalties += inp.stop_penalty_usd + st.detour_min / 60 * inp.time_value_usd_per_hr
            fuel = inp.capacity_gal - st.detour_mi / 2 / mpg
    fuel -= (inp.route_mi - pos) / mpg
    low = min(low, fuel)
    if fuel < 0:
        return Plan(inp.reserve_mi, False, tuple(stops), reason="runs dry before the destination")
    net = sum(s.cost_usd for s in stops) + penalties - fuel * inp.end_value_per_gal
    return Plan(inp.reserve_mi, True, tuple(stops), end_gal=fuel, min_gal=low, net_cost_usd=net)


def format_options(inp: PlanInput, options: list[Plan], naive: Plan | None = None) -> str:
    """Plain-text table plus per-option stop lists, for the CLI."""
    mpg = inp.planning_mpg
    head = (
        f"Route {inp.route_mi:,.0f} mi · tank {inp.start_gal:.1f} / {inp.capacity_gal:.0f} gal"
        f" · planning at {mpg:.1f} mpg"
    )
    if inp.calibrating:
        head += f" ({inp.mpg:.1f} × {inp.calibrating_mpg_factor:g}, tank still calibrating)"
    lines = [head, ""]

    def usd(x: float) -> str:
        return f"${x:,.0f}"

    lines.append(
        f"{'Reserve':<12}{'Stops':>5}  {'Fuel':>7}  {'Net':>7}  {'Saves':>6}  Lowest point"
    )
    ref = next((o.net_cost_usd for o in options if o.feasible), None)
    for o in options:
        label = f"{o.reserve_mi:.0f} mi"
        if not o.feasible:
            lines.append(f"{label:<12}infeasible: {o.reason}")
            continue
        saves = "—" if ref is None or o.net_cost_usd == ref else usd(ref - o.net_cost_usd)
        lines.append(
            f"{label:<12}{len(o.stops):>5}  {usd(o.fuel_cost_usd):>7}  {usd(o.net_cost_usd):>7}"
            f"  {saves:>6}  {o.min_gal:.1f} gal ({o.min_gal * mpg:.0f} mi)"
        )
    if naive is not None:
        if naive.feasible:
            lines.append(
                f"{'fill at 1/4':<12}{len(naive.stops):>5}  {usd(naive.fuel_cost_usd):>7}"
                f"  {usd(naive.net_cost_usd):>7}  {'':>6}  {naive.min_gal:.1f} gal"
                f" ({naive.min_gal * mpg:.0f} mi)"
            )
        else:
            lines.append(f"{'fill at 1/4':<12}{naive.reason}")
    lines.append("")
    lines.append(
        "Net = fuel bought + stop penalties + detour time − value of fuel left at the end"
        f" (${inp.end_value_per_gal:.2f}/gal). Saves is relative to the first row."
    )
    for o in options:
        if not o.feasible:
            continue
        lines.append("")
        lines.append(f"{o.reserve_mi:.0f} mi reserve:")
        for s in o.stops:
            lines.append(
                f"  mi {s.station.mile:>6,.0f}  {s.station.name:<28} arrive {s.arrive_gal:>5.1f}"
                f"  buy {s.buy_gal:>5.1f} gal @ ${s.station.price_per_gal:.2f}"
                f"  = ${s.cost_usd:,.0f}" + (f"  ({s.station.note})" if s.station.note else "")
            )
        lines.append(f"  arrive with {o.end_gal:.1f} gal")
    return "\n".join(lines)


def trip_from_dict(data: dict[str, object]) -> tuple[float, tuple[Station, ...], float]:
    """Parse a trip file: ``(route_mi, stations, end_value_per_gal)``.

    ``home_price_per_gal`` values the fuel left at the end; when absent, the
    price at the station nearest the destination is used.
    """
    raw = data.get("stations", [])
    if not isinstance(raw, list):
        raise ValueError("trip file: 'stations' must be a list")
    stations = tuple(
        Station(
            name=str(s["name"]),
            mile=float(s["mile"]),
            price_per_gal=float(s["price_per_gal"]),
            detour_mi=float(s.get("detour_mi", 0.0)),
            detour_min=float(s.get("detour_min", 0.0)),
            diesel=bool(s.get("diesel", True)),
            note=str(s.get("note", "")),
        )
        for s in raw
    )
    route_mi = float(data["route_mi"])  # type: ignore[arg-type]
    home = data.get("home_price_per_gal")
    if home is None:
        home = default_home_price(stations, route_mi)
    return route_mi, stations, float(home)  # type: ignore[arg-type]


def default_home_price(stations: tuple[Station, ...], route_mi: float) -> float:
    """Price at the diesel station nearest the destination (0 if there are none)."""
    diesel = [s for s in stations if s.diesel and s.mile <= route_mi]
    return max(diesel, key=lambda s: s.mile).price_per_gal if diesel else 0.0


def trip_to_dict(
    route_mi: float, stations: tuple[Station, ...], home_price_per_gal: float
) -> dict[str, object]:
    """Inverse of ``trip_from_dict``: save a live trip for re-planning offline."""
    return {
        "route_mi": route_mi,
        "home_price_per_gal": home_price_per_gal,
        "stations": [
            {
                "name": s.name,
                "mile": round(s.mile, 1),
                "price_per_gal": s.price_per_gal,
                "detour_mi": s.detour_mi,
                "detour_min": s.detour_min,
                "diesel": s.diesel,
                "note": s.note,
            }
            for s in stations
        ],
    }
