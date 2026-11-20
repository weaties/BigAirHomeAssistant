---
name: influx-schema
description: InfluxDB 2.7 schema conventions for BigAir — buckets and retention, measurement/tag/field naming with unit suffixes, the single-writer rule, InfluxQL via DBRP mapping, downsampling, idempotent writes, and the checklist for adding a new signal. TRIGGER when adding or renaming a measurement, field or tag, touching src/bigair/influx_io.py, retention or downsampling tasks, or writing InfluxQL for a dashboard. DO NOT trigger for Grafana layout work that doesn't change queries (use /grafana-dashboards).
---

# InfluxDB schema

The measurement table in CLAUDE.md → **Data model** is canonical. This skill
holds the rules behind it and the procedure for changing it. Schema changes
are Critical tier.

## Rules

- **Single writer:** only bigair writes to InfluxDB. HA's InfluxDB
  integration stays off. HA-only devices come in over MQTT via
  `mqtt_statestream`, and bigair records them.
- **Buckets:** `van_raw` (400 d) holds native-rate signals. `van_history` (∞)
  holds 1-min aggregates, `track`, `fill_up`, `trip` and other events.
- **Names:** `snake_case`. Measurement = a domain noun (`engine`, `fuel`).
  **Field names end in their unit** (`_gal`, `_mi`, `_mph`, `_mpg`, `_c`,
  `_kpa`, `_v`, `_a`, `_w`, `_pct`, `_ratio`, `_g_s`, `_lph`, `_s`). Booleans are
  bare adjectives (`running`, `cruise_active`).
- **Tags are low-cardinality:** source, method, device, circuit, fill_type,
  status. Never put IDs, coordinates, prices or free text in tags. `trip_id`
  and `fill_id` are fields.
- **One field, one type, forever.** Influx rejects type changes per shard.
  Write floats as floats (`12.0`, never `12`). If you need a new type,
  create a new field name.
- **UTC nanosecond timestamps** from the source sample time, not the write
  time.
- **Idempotent events:** an event's timestamp is deterministic (detection
  time, or the historical fill's time), so re-running an import overwrites
  points instead of duplicating them.

## The writer (`influx_io.py`)

- `Point` → `to_line()`: tags sorted, fields in mapper order, `None`/NaN/inf
  fields dropped, floats in full precision without exponents, ints with
  `i`, millisecond timestamps, naive datetimes rejected.
- Bucket routing by measurement: `track`, `fill_up`, `trip` →
  `van_history`; everything else → `van_raw`.
- One mapper per measurement (`track_point`, `vehicle_point`, `fuel_point`,
  …). `test_mappers_only_write_fields_in_the_data_model` parses the
  CLAUDE.md table, so a mapper can't write a field the table doesn't list.
- `InfluxWriter` is stdlib-only. It buffers up to 50k lines and drops the
  oldest when full (counted in `dropped`). A failed bucket stays buffered.
  It writes nothing while `clock_ok()` is false (Decision #24; #22 supplies
  the chrony check).

## Querying: InfluxQL, not Flux

Create DBRP mappings once per bucket during provisioning (M1 deploy task):

```
influx v1 dbrp create --db van_raw     --rp autogen --bucket-id <id> --default
influx v1 dbrp create --db van_history --rp autogen --bucket-id <id> --default
```

Grafana's datasources use InfluxQL against those DB names. Don't write Flux
in dashboards or code: it's in maintenance mode, and InfluxQL keeps an
InfluxDB 3 migration cheap.

## Downsampling (M1)

An Influx task writes 1-min `mean`, `min` and `max` of the numeric fields in
`engine`, `vehicle`, `fuel`, `electrical`, `load` and `heater` from
`van_raw` into `van_history`, with the same measurement and field names plus
a `_min`/`_max` suffix for extremes. Long-range dashboards (> 30 days) query
`van_history`.

## Adding a new signal — checklist

1. Its source is VERIFIED in `docs/pid-register.md` (vehicle signals), or the
   device row is filled in (Victron, heater, sensors).
2. Choose the measurement (an existing domain, if it fits) and the unit
   suffix. Check that the name doesn't collide.
3. Add a row or field to the CLAUDE.md Data model table **in the same PR**.
4. Test-first: decoder → dataclass field → line-protocol serialisation
   (`influx_io` tests assert the exact line protocol).
5. If it's numeric and long-lived, add it to the downsampling task.
6. Add it to a dashboard if it's user-facing (`/grafana-dashboards`).
