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

## mpg Geomap recipe

- Query: `SELECT "lat", "lon", "mpg_30s", "speed_mph" FROM "track" WHERE
  $timeFilter` against `influx-van-history`. Format as a table.
- Layer: markers (or route), location mode "coords" with lat/lon fields.
  Colour = field `mpg_30s`, continuous scheme red→yellow→green, with a
  fixed min/max (e.g. 8–25 mpg) so maps compare across trips. Use a small
  marker size.
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
