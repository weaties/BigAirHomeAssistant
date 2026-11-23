# Fuel-stop planner

`bigair fuel-plan` works out where to buy diesel along a fixed route, and how
much, so the trip costs as little as possible without dipping below a reserve.
It prints one plan per reserve depth, so you can see what each mile of reserve
you give up actually saves. Issues: #70 (planner, this), #71 (live prices and
routes), #72 (HA dashboard).

```bash
uv run bigair fuel-plan trip.json --tank-gal 43.7 --mpg 15 --calibrating \
    --reserves 150,100,75,50
```

| Flag | Meaning |
|---|---|
| `--tank-gal` | Fuel on board at the start (bigair's `tank_gal`) |
| `--mpg` | Expected mpg. Be conservative: highway, loaded, into wind |
| `--calibrating` | Tank still calibrating: plan at `mpg × 0.93` |
| `--reserves` | Reserve depths to compare, in miles of range at the planning mpg |
| `--stop-penalty` | Dollar cost of the hassle of one stop (default $10). Stops pointless top-ups |
| `--home-price` | Value of fuel left at the end, in $/gal (overrides the trip file) |
| `--capacity` | Tank size in gallons (default 47) |

## Trip file

Real trips stay out of git, because this repo is public. Keep them on the Pi
or in `data/`.

```json
{
  "route_mi": 300,
  "home_price_per_gal": 6.50,
  "stations": [
    {"name": "Town A", "mile": 50, "price_per_gal": 4.00},
    {"name": "Town B", "mile": 150, "price_per_gal": 3.00,
     "detour_mi": 2, "detour_min": 6, "diesel": true}
  ]
}
```

- `mile` is the distance along the route. For a round trip, list the return-leg
  stations again at `out_mi + local_mi + (out_mi − m)`.
- `price_per_gal` is the expected price on arrival.
- `detour_mi` and `detour_min` are the round-trip cost of leaving the route.
- `home_price_per_gal` values the fuel left at the end. Without it the planner
  would aim to arrive nearly empty. It defaults to the price at the last
  station.

## How it decides

The planner is a dynamic program over (station, gallons on board) on a
0.5-gal grid. It minimises fuel bought, plus stop penalties, plus detour time,
minus the value of the fuel left at the end. **Fuel never drops below the
reserve.** The start tank rounds down and fuel use rounds up, so the grid never
overstates the fuel on board. Stale fuel data is refused, and an infeasible
route says why rather than guessing. The `fill at 1/4` row is a baseline: what
filling to full at a quarter tank would cost.

Expect the reserve depth to matter little on interstates, where stations are
close together, and a lot on remote stretches.
