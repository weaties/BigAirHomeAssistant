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

## Trip planner page on the Pi (#75)

The `planner` Compose service runs `bigair planner` on port 8092 (host
networking). Open it:

- on the van Wi-Fi: `http://bigair-ha.local:8092/`
- in the Companion app: **Van** dashboard → **Trip** (a full-screen webpage card)
- over the tailnet: `http://bigair-ha:8092/`

The page has a form (from, which can be **📍 Here** using the van's GPS through
host gpsd; to; via, separated by `;`; round trip; leave; leave for home;
reserve depths) and shows:

- **Fuel stops by reserve depth:** one chip per depth, with its stops, tank
  levels, cost, and savings against the most cautious depth and against
  filling at ¼ tank.
- **Weather on the way:** the route coloured clear / rain / snow-ice / severe
  at your ETA, with labels about every 75 mi and every snowy or severe spot.
  Round trips have a Going / Coming home toggle. The **leave slider** (−12 h
  to +72 h) re-times the weather in the browser without a new request. Fuel
  prices aren't re-planned.
- **Weather alerts** (NWS) near the route, the **along-the-way** table, and
  **Open Roads stations without a price**.

Weather comes from Open-Meteo (free, no key): hourly forecasts for 16 days at
points every 25 mi, so the far days are less certain. Alerts come from NWS (US
only). Each plan is about 20–30 billed Google searches plus a few free
weather calls. Plans run one at a time, up to `PLANNER_DAILY_LIMIT` (20) a day.
The last plan is cached in `data/bigair/trip-plan.json` and shown when the page
opens, with a banner once it's more than 6 h old.

Offline, the page still opens and shows the last plan. The map library and
tiles need internet, so the map shows a notice instead, but the stops and
weather tables still work.

The command line runs the same plan, weather included:
`bigair fuel-plan --from … --to … --round-trip --live --depart … --return-depart …`.

### Points of interest near the route (#88)

With POIs synced to the Pi ([syncthing-poi.md](hardware/syncthing-poi.md)), the
page adds a **Points of interest near the route** card and orange squares on the
map. The **Show POIs** selector sets how far to look:

- **within a round-trip detour of N hours**: driving from the route to the POI
  and back takes at most N hours (the default is 3);
- **within a drive (each way) of N hours**: the POI is at most N hours from the
  route;
- **coming up in the next N hours**: the turn-off is in the first N hours of the
  route, and the POI is at most 90 min off it.

**Update** re-runs the search on the cached route without a new plan. Each POI
shows where it leaves the route, the detour and the visit time from its note
(`visit_min`). You can change the visit time on the page, and it's remembered
on that device. Tick POIs to see detour + visit time added, then **Add selected
as via & re-plan**.

How it works (`poi_near.py`): a straight-line prefilter at 70 mph rules out POIs
that can't be in reach, without asking Google. The rest get exact drive times
from Google Routes (duration only), nearest first, at most 25 per search, and
are cached while the planner runs. The detour is twice the one-way drive from
the nearest point on the route, which errs long (the safe side for a 3pm
arrival). The planner never fills in a number it doesn't know:

- POIs past the cap show **not checked**;
- failed lookups show **unknown**;
- a missing visit time shows **?**.

None of these are counted in the totals. POIs are only matched to the outbound
route.

### Get there slow (#90)

The **Get there quick / Get there slow** switch sits under the form. Quick is
Google's normal route. Slow follows Dan's travel rules:

- no interstates (Google's `avoidHighways`);
- at most 300 mi a day, detours included (**a ceiling, never a target**: short
  days are good);
- leave at 10:00 unless you set a time;
- in by 15:00 and before sunset at the destination;
- then stay 3 nights.

A **Today's drive** card shows the window (leave → in by 15:00 or sunset,
whichever comes first), the drive time and the miles. If the drive alone
doesn't fit, it says so plainly ("6h40 of driving vs a 5h00 window", "340 mi is
over the 300 mi daily ceiling") and proposes nothing.

Each POI gets a **score**. Hover over it to see the terms:

| Term | Value |
|---|---|
| Base (any POI) | 1 |
| Dan's rating | 0.5 × (rating − 3), so ★5 = +1 and ★1 = −1 |
| In / out of `best_months` at the ETA | ±0.5 |
| Tag weights from the rules file | as set |
| Forecast at the ETA inside / outside the 70–80°F band | ±0.25 |

Distance isn't in the score, so a longer day never scores higher.

Slow mode **proposes** the set of POIs with the highest total score whose
detours and visits fit in the time left after the drive, and whose detour miles
keep the day under 300. Ties go to the set that adds less time. Only POIs with
a known detour and visit time can be proposed. The proposed POIs arrive ticked.
Untick or tick others, and the totals line re-checks the day ("6h40 of 7h00,
226 of 300 mi ✓ fits"). **Add selected as via & re-plan** routes through them.

To change the rules or weight tags, put a YAML file at
`data/bigair/travel-rules.yaml` on the Pi:

```yaml
max_day_mi: 300        # ceiling
depart: "10:00"
arrive_by: "15:00"
no_driving_after_dark: true
avoid_interstates: true
temp_band_f: [70, 80]
tag_weights: {hot-springs: 0.5, geology: 0.25}
```

A bad file is logged and the defaults are used.

**Scenic byways (#100).** With `data/bigair/byways.geojson` on the Pi (National
Scenic Byways from USDOT's `US_Scenic_Byways` service, western states), slow mode
asks Google for alternative routes. Of the ones that fit the day (the window and
300 mi), it keeps the one with the most byway miles; ties go to the shorter. A
route stretch counts when it's within 0.5 mi of a byway. A byway never justifies
breaking the rules. The day card says which byways the route uses, or why none.
Google gives no alternatives once you add via points, so a planned via route is
taken as is. Claude's "Why this way?" is in [claude.md](claude.md).

### Where next? Destination discovery (#91)

The **Where next?** card (above the plan) answers "where should we spend the
next few nights?" from the **From** field (or 📍 Here). Candidates come from:

| Source | Where it comes from |
|---|---|
| Your POIs | vault notes with `destination: true` |
| iOverlander | GPX exports in `data/bigair/ioverlander/` (Wild Camping, Informal Campsite, campgrounds) |
| Public land | BLM / USFS units from `data/bigair/public-land.geojson`, e.g. a regional PAD-US extract |
| Campgrounds | Google Places, only when none of the above is in range |

Candidates must be within the 300 mi ceiling in a straight line and at least
5 mi away. Duplicates within 1 mi keep the vault one. The 8 most promising (own
score first, then the closest) are routed slow-mode style. Each option shows
its road miles, its arrival time, and its score: the destination's own score
(rating, season and weather on arrival; campgrounds −0.25) plus the stops S2
would propose on the way. Distance is never rewarded: a closer, richer place
beats a far one, and legs that don't fit sink to the bottom with the reason.
**Plan this leg** switches to slow mode and plans it.

Both files are optional and read on every search. A missing one adds nothing;
a bad one is a warning.

### Get there slow to a far destination (#117)

Pick **Get there slow**, type a destination that's several days away and tap
**Plan trip**. The planner first lays out the driving days in a **Days to …**
card, then plans day 1 in detail below it (fuel, weather, POIs, today's drive).
The **To** field keeps the far destination, so tapping **Plan trip** again
re-plans from wherever you are. The card replaces #92's date-driven sketch.

Every day leaves at 10:00 and must stay within **both** limits: at most 300 mi,
and arriving by 15:00 (or sunset, if that's earlier). On slow roads the time
limit usually binds first: at 45 mph a day is 225 mi. Where each day ends is
picked in this order:

1. **the destination**, if it fits today;
2. otherwise **the best spot in reach**: a vault destination, iOverlander site or
   public land within 40 mi of the route and at least 50 mi along. A spot counts
   only if it's a vault destination or has things to do (your POIs) within
   25 mi. It scores its own points plus each nearby POI's score (rating, season
   and tag weights; negatives count as 0). The highest score wins, so a day can
   stop short or run long to get somewhere good. Ties go to fewer miles, and
   distance never scores;
3. otherwise **the farthest Walmart** in reach (a Places search along the route,
   made only when the trip takes more than a day). Check that the store allows
   overnight parking;
4. otherwise an **open spot** as far along as the rules allow. Day 1's open spot
   gets a **Find a spot near here** button.

**Layover days** are set per stop: 0 (the default) means one night, then drive on
in the morning. Changing one re-plans the days at no extra cost, because the
route and the Walmart search are cached. The arrival date follows from the
layovers. **Arrive by** is optional and only warns.

Miles and times are estimates from the route's pace. A spot's distance off the
route counts going in and coming out, at 35 mph in straight-line miles. Day 1's
detailed plan routes it for real, and that's the number to trust.

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
  would aim to arrive nearly empty. It defaults to the median price within
  30 mi of the destination (or of the last five stations).

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
