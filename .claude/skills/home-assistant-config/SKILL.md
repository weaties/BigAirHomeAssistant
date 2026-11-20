---
name: home-assistant-config
description: Home Assistant configuration conventions for BigAir — YAML packages, MQTT discovery vs hand-written MQTT sensors, mqtt_statestream for HA-only devices, notification and actionable-notification patterns for the Companion app, offline/local-only setup, and what CarPlay can and cannot show. TRIGGER when editing anything under homeassistant/, adding an entity, automation, script or notification, or deciding how something appears on the phone or CarPlay. DO NOT trigger for ESPHome node YAML (use /dash-display, /heater-accounting, /electrical) or Grafana (use /grafana-dashboards).
---

# Home Assistant config

HA is the **display and input layer**. It shows bigair's sensors, collects
fill-up answers and runs the phone and CarPlay UI. It contains no fuel
logic. Templates may format values, never compute them.

## Layout

```
homeassistant/
  configuration.yaml        # explicit integrations (no default_config, Decision #27) + packages: !include_dir_named packages
  packages/
    mqtt_backbone.yaml      # M3, not present yet: mqtt_statestream for Victron BLE
    fill_up.yaml            # helpers, form script, notification automations (M1)
  secrets.yaml.example      # committed; the real secrets.yaml is gitignored
```

One package per feature. A package owns its helpers, scripts and
automations. `yamllint` runs in CI; a real HA config check runs on the dev
stack (`docker compose run --rm homeassistant python -m homeassistant
--script check_config -c /config`).

## Entities from bigair: MQTT discovery

bigair publishes HA MQTT discovery configs (`homeassistant/sensor/bigair/
<object>/config`, retained) for every derived sensor: `tank_gal`,
`range_mi`, `mpg_rolling`, `mpg_trip_spark`, the stale flags and so on.
**Don't hand-write YAML for bigair's sensors.** Discovery keeps the unit,
device class and availability in one place, in Python, next to the logic.
Availability uses a bigair LWT topic, so HA shows "unavailable" when bigair
dies.

WiCAN raw values: until the WiCAN AutoPID's own HA discovery is verified,
HA doesn't need them. bigair consumes the raw topics and publishes clean,
unit-converted sensors.

## HA-only devices → MQTT

Victron BLE (`/electrical`) and any HA integration bigair can't reach
directly: list their entities in `mqtt_statestream` (`include.entities`).

Two gotchas (both hit on the first Pi bring-up, 2026-11-19):

- **An empty include list publishes every entity.** HA's entity filter treats
  "nothing included" as "include all". `tests/test_ha_config.py` fails on a
  statestream block without a non-empty include list. Don't add the package
  until there's a real entity to forward.
- **It needs the MQTT integration to exist at startup.** MQTT is configured
  in the UI, so on a fresh install HA starts before MQTT exists, and
  statestream fails with "MQTT integration is not available". After adding
  MQTT (or the package), restart HA.
bigair subscribes and records them. This keeps the single-Influx-writer
rule.

## Notifications (Companion app)

- Target `notify.mobile_app_<device>`. The device name is in `secrets.yaml`
  or a substitution, never hard-coded.
- Actionable: `data.actions` with `behavior: textInput` for the fill-up
  "gallons price" field. Carry `fill_id` in the action identifier
  (`FILL_FULL_<fill_id>`) so replies map back. Handle
  `mobile_app_notification_action` → publish to `bigair/fill_up/response`.
  Parsing happens in Python (`/fill-up-events`).
- Use `tag:` set to the `fill_id` so a re-prompt replaces the old
  notification.

## Networking gotcha

HA runs with `network_mode: host` (for mDNS, Bluetooth and Companion
discovery), so it **can't resolve Compose service names**. From HA, the broker
is `localhost:1883` and InfluxDB is `localhost:8086`. Bridge-network
containers (bigair, Grafana) use `mosquitto` and `influxdb`.

## Integrations: explicit, minimal

There's no `default_config`. `configuration.yaml` lists what loads, and each
line says which feature needs it. Before adding an integration, check it
works offline and isn't only there because HA discovered something on the
network. Integrations added in the UI create config entries in
`.storage/core.config_entries`. Remove unneeded ones in **Settings →
Devices & services**, or via HA's REST API with a token, never by editing
`.storage` by hand: the registry format changes between releases.

## Local and offline

- No Nabu Casa or cloud integrations in the critical path. The Companion app
  uses the hotspot SSID with `internal_url`, e.g. `http://bigair-ha.local:8123`.
- The van must work with no internet for weeks. Check every new integration
  for a cloud dependency before adding it.

## CarPlay

From `docs/carplay.md`: CarPlay lists actions and toggles (script, button,
switch, light, …) and climate/vacuum screens. It **can't list sensors**, so
fuel and range don't go there. Expose `script.log_fill_up` in Quick access,
and later the heater. Readouts belong on the dash display (`/dash-display`).
