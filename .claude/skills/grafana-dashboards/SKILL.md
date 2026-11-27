---
name: grafana-dashboards
description: Dashboards-as-code conventions for BigAir's provisioned Grafana — file layout, UIDs, datasource references, exporting from the UI without drift, the mpg-coloured Geomap, throttle-vs-mpg views, and the tests that guard dashboard JSON. TRIGGER when adding or editing anything under grafana/provisioning/, building a new panel or dashboard, or changing a query a dashboard depends on. DO NOT trigger for schema changes (use /influx-schema) or HA dashboards (use /home-assistant-config).
---

# Grafana dashboards

## Layout and identity

```
grafana/provisioning/
  datasources/influxdb.yaml     # InfluxQL datasources, fixed UIDs
  dashboards/dashboards.yaml    # file provider → /var/lib/grafana/dashboards
  dashboards/json/*.json        # one dashboard per file
```

- File name = `uid` = `bigair-<slug>` (e.g. `bigair-engine-history.json`).
- Datasource UIDs are fixed: `influx-van-raw` and `influx-van-history`.
  Panels reference `{"type": "influxdb", "uid": "influx-van-raw"}`, never
  by name and never with `${DS_...}` import placeholders.
- `"id": null`, a stable `uid`, `"editable": true`. The provider has
  `allowUiUpdates: false`, so the repo is the truth.
- Timezone is `browser`; storage is UTC.

## Editing workflow (no drift)

1. Edit in the Grafana UI on a dev stack (`docker compose up grafana influxdb`).
2. Share → Export → **"Export for sharing externally" OFF** → save the JSON
   over the file.
3. Normalise: `id: null`, keep the `uid`, check datasource refs, drop
   `version` churn.
4. `uv run pytest tests/test_grafana_dashboards.py`. It checks valid JSON, a
   unique `uid` matching the file name, only known datasource UIDs, and no
   `${DS_` placeholders.

## Queries

- InfluxQL only, using `$timeFilter` and `$__interval`. Use `GROUP BY
  time($__interval) fill(null)` so gaps show as gaps. Never use `fill(0)` for
  sensor data, because a zero coolant temperature is a lie.
- Ranges > 30 days → query `van_history` aggregates.
- Filter "moving" samples for mpg analytics: `speed_mph > 5`, mpg not null,
  and exclude braking (`brake_active = false`) once that signal is verified.

## First dashboards (M1)

| uid | Panels |
|---|---|
| `bigair-engine-history` | fuel rate, coolant temp, battery voltage (time series) |
| `bigair-fuel` | tank gal, range mi, mpg rolling, fill-up annotations |
| `bigair-mpg-map` | Geomap of `track`, coloured by `mpg_30s` |
| `bigair-phone` | Phone-width (#65): tank gauge, range, mpg now, status, last reading; tank, mpg, speed-vs-mpg, track map, battery |
| `bigair-energy` | House electrical (#113): now (SOC, net, time to go, solar, loads, alternator), today's Wh, power flows, SOC and voltage, daily bars and table from `energy`. Generated layout; InfluxQL `alias: $col` so `AS` names match overrides |

## Phone dashboard rules (`bigair-phone`)

- Open as `http://bigair-ha.local:3000/d/bigair-phone?kiosk` on the van Wi-Fi, or
  `http://bigair-ha.<tailnet>.ts.net:3000/d/bigair-phone?kiosk` over Tailscale.
  Anonymous Viewer access means no login either way (Decision #38). Grafana
  publishes 3000 on all interfaces, so the tailnet reaches it directly; unlike the
  loopback-only dash preview (Decision #37) it needs no `tailscale serve`.
- Cross-links between HA and Grafana use `bigair-ha.local`, which only resolves on
  the van Wi-Fi. The tailnet name can't go in this public repo.
- Panels stack in `gridPos` order (y, then x) below ~769 px, so lay out the "Now"
  row in reading order.
- **Live values never show stale numbers.** Query `last("tank_gal"), last("stale")`
  over a fixed window and drop the row with a *Filter data by values* transform
  when `stale` is true, so the panel shows its `noValue` ("No fresh data"). Show
  the reading's age with a stat on the `Time` field and unit `dateTimeFromNow`.
- Track panels downsample (`last(lat)`, `last(lon)`, `mean(mpg_30s)` `GROUP BY
  time($__interval)` with a panel `interval` and `maxDataPoints`); two days of
  1 Hz track is ~170k points, which is too much for a phone.

## mpg Geomap recipe

- Query: `SELECT "lat", "lon", "mpg_30s", "speed_mph" FROM "track" WHERE
  $timeFilter` against `influx-van-history`. Format as a table.
- Layer: markers (or route), location mode "coords" with lat/lon fields.
  Colour = field `mpg_30s`, continuous scheme red→yellow→green, with a
  fixed min/max (e.g. 8–25 mpg) so maps compare across trips. Use a small
  marker size.
- **View "fit": `zoom` is the maximum zoom**, so `zoom: 5` keeps the map at
  continent scale. Use `zoom: 16` with `allLayers: true`.
- Null mpg (idle) → grey or hidden. Don't colour idle as "0 mpg".
- **Offline:** the default basemap tiles come from the internet. In the van
  the map needs a local tile source, or it degrades to a blank basemap with
  the track still drawn. This is a tracked M2 decision.

## Throttle vs mpg (M2)

1. **Scatter first:** an XY chart with x = `throttle_pct`, y =
   `mpg_instant_30s`, colour = `speed_mph`. Moving samples only. Check that
   the installed Grafana version's XY chart supports colour-by-field before
   promising it.
2. Per-trip time-series overlay of throttle and mpg.
3. Heatmap: throttle bin × speed bin → mean mpg across trips (pre-binned in
   InfluxQL or in a bigair aggregate).
