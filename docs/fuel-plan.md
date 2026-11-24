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

## Live mode (#71)

```bash
export GOOGLE_MAPS_API_KEY=...      # Routes API + Places API (New) enabled
export EIA_API_KEY=...              # optional; DEMO_KEY is used if unset (rate-limited)
uv run bigair fuel-plan --from "Seattle, WA" --to "Sonoma Raceway, CA" --round-trip \
    --live --save data/trips/sonoma.json
```

- **Route:** Google Routes `computeRoutes`. `--via` adds waypoints. `--round-trip`
  lists each station again for the drive home, at its mirrored mile, plus
  `--local-mi` (default 20) of driving at the far end.
- **Stations:** Places API (New) Text Search along the route, in 100-mi
  stretches. Each stretch gets a "diesel" search (up to 60 results) and a
  "truck stop" search (20), because truck stops can rank below the many gas
  stations in a busy stretch. Open Roads stations that Google has no price for
  (often Petro and TA) are listed above the plan so you can check them in the
  app. Only stations that post a `DIESEL` (or
  `TRUCK_DIESEL`) price count. Prices older than `--max-age-h` (72) are dropped.
  Google returns the standard (highest) posted price.
- **Detour:** origin → station → stretch end, minus the stretch length, from
  `routingSummaries` (derived, not documented). The time is costed at 30 mph.
- **Open Roads:** Love's, TA and Petro are priced at posted minus `--discount`
  (default `loves=0.30,ta=0.30,petro=0.30`, the low end of the usual 30–50¢).
  The real member price varies by station and is in the TSD Open Roads app.
  `--discount none` turns it off.
- **Trend:** EIA weekly retail diesel (`EPD2D`): California (`SCA`), West Coast
  except California (`R5XCA`, because WA and OR have no series of their own)
  and the US (`NUS`) elsewhere. The 4-week slope, capped at ±2¢/day, is applied
  up to each station's ETA from `--depart`. If EIA can't be reached, the plan
  goes ahead without the trend.
- **Tank:** `--live` reads bigair's retained `bigair/fuel/state` from
  `--mqtt-host` (default `bigair-ha` on the tailnet): `tank_gal`, `mpg_rolling`
  and the `calibrating`/`stale` flags. A stale tank refuses to plan.
  `--tank-gal` and `--mpg` override it.
- **Cost:** each search is a Text Search Enterprise + Atmosphere request,
  because `fuelOptions` lives in that SKU. A 780-mi route is about 8–24
  requests. Set a budget alert on the Cloud project.
- `--save` writes the assembled trip file, so you can re-plan with no signal:
  `bigair fuel-plan data/trips/sonoma.json --live`.

### Getting a Google key

1. Google Cloud console: create a project and attach billing.
2. Enable the **Routes API** and the **Places API (New)**.
3. Credentials → Create API key → restrict it to those two APIs.
4. Billing → Budgets: set an alert (for example $10/month).
5. Keep the key out of git: put it in your shell profile on the Mac, or in
   `deploy/.env` on the Pi.

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
