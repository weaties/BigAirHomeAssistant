# CLAUDE.md — BigAir

Van telemetry for a 2017 Mercedes Sprinter (W906, 170" wheelbase, 4x4, OM642
3.0 V6 diesel) fitted with a 47-gallon aftermarket tank. Goal: an accurate
fuel level, range and fuel economy the stock gauge can't give, plus a
long-term history of the van — on a dash display, in CarPlay via the Home
Assistant Companion app, and in Grafana. Everything runs **locally in the
van, offline**.

This file is the single project guide for humans and agents. `/name` refers
to a skill in `.claude/skills/`; each skill's behaviour is summarised where
it is referenced, so the rule stands without the skill.

## Top rules — read first

- **Always work in a git worktree.** Before any edit, check
  `git worktree list`; reuse one whose branch matches the task, otherwise
  `git worktree add .claude/worktrees/<issue>-<slug> -b <issue>-<slug>`
  (Claude Code: `EnterWorktree`). A `PreToolUse` hook
  (`.claude/hooks/require_worktree.py`) blocks Edit/Write on tracked files
  in the primary checkout — enter a worktree, don't route around it with a
  shell edit. Read-only work needs no worktree.
- **Never push to `main`.** Feature branch → PR → merge. PR body carries
  `Closes #N` (`Fixes #N` for bugs).
- **Label work in progress:** `gh issue edit <N> --add-label in-progress`
  plus a comment naming the branch.
- **Never invent a PID, DID, CAN ID, scaling or burn rate.** Every
  vehicle- or device-specific signal is **UNVERIFIED** until a scan or
  datasheet on *this* van/device confirms it and the evidence is recorded in
  `docs/pid-register.md` (`/pid-discovery`, `/can-sniffing`). Values from
  other vehicles — including other W906 engines — are leads, not facts.
- **This repo is public.** Never commit real GPS tracks, fill-up records,
  photos, receipts, VIN, Wi-Fi credentials or `secrets.yaml`. Real data lives
  on the Pi or in gitignored `data/`. Test data is synthetic.

### Judgment rules

- **Surface assumptions before building.** Underspecified → state the
  assumption you're proceeding on.
- **Genuinely ambiguous → ask. Trivially ambiguous → decide and add a line to
  Decisions below.**
- **Push back when warranted.** A worse plan you were handed is still worse.
- **Prefer boring solutions.** The fuel number must be right after a power
  cut at a remote trailhead; reliability beats cleverness.
- **Touch only what you're asked to touch.** No drive-by refactors.
- **A wrong fuel number is worse than no number.** When inputs are stale or
  unverified, show "unknown/stale", never a confident guess.

## Architecture

```
 OBD-II port          Bluetooth          Bluetooth (BLE)             ESP32 nodes (ESPHome, future)
 ┌──────────┐        ┌──────────┐       ┌──────────────────┐        ┌───────────────────────┐
 │ WiCAN    │        │ XGPS160  │       │ Victron BMV-712, │        │ W-Bus heater (Webasto)│
 │ (OBD/PRO)│        │ → gpsd*  │       │ SmartSolar MPPT, │        │ INA228 load sensors   │
 └────┬─────┘        └────┬─────┘       │ Orion DC-DC      │        └──────────┬────────────┘
      │ MQTT              │ TCP 2947    └───┬──────────────┘                   │ MQTT
      ▼                   ▼                 │ Instant Readout → bigair         ▼
 ┌──────────────────────────────────────────┴──────────────────────────────────────────────────┐
 │ Raspberry Pi (Pi OS, Docker Compose, van Wi-Fi, no internet required)                       │
 │                                                                                             │
 │   Mosquitto ◄── all signals flow through MQTT ──► Home Assistant ── Companion app / CarPlay  │
 │      ▲  │                                         (display, notifications, user input)      │
 │      │  ▼                                                                                   │
 │   bigair services (Python): ingest → decode → fuel model, fill detection, GPS track, heater │
 │      │   publishes derived sensors (tank_gal, range_mi, mpg) to MQTT w/ HA discovery        │
 │      │   display server: renders the dash gauge frame ──── HTTP :8090 ───────┐              │
 │      ▼                                                                       │              │
 │   InfluxDB 2.7 ◄── bigair is the only writer ──► Grafana (dashboards, Geomap)│              │
 └──────────────────────────────────────────────────────────────────────────────┼──────────────┘
                                                                                ▼
                                   Dash display: LilyGo T5 ePaper S3 PRO (eink-screen firmware)
```

\* gpsd and chrony run on the Pi host, not in Docker (Decision #28). The dash
display fetches a pre-rendered frame from bigair over the van Wi-Fi (Decision #29).

Principles:

- **MQTT is the backbone.** Every source publishes to Mosquitto; every
  consumer subscribes. ESPHome telemetry nodes publish to MQTT directly
  (Decision #23). bigair decodes the Victron devices' BLE broadcasts itself
  (Decision #45). The dash display fetches a frame from
  bigair's display server (Decision #29).
- **bigair is the single InfluxDB writer.** HA's InfluxDB integration is not
  used — its entity-shaped schema would fight ours. One writer, one schema.
- **Hardware isolation.** Only edge modules (`mqtt_io.py`, `influx_io.py`,
  `gps_logger.py`'s gpsd client, `google_io.py`, `eia_io.py`, `weather_io.py`, `victron_io.py`,
  `planner_server.py`) touch the network or devices, and only
  `main.py` imports them. Logic modules (`fuel_model.py`, `fill_detect.py`,
  `heater.py`) take and return frozen dataclasses from `signals.py`, so they
  test without hardware.
- **Decode early, store clean.** Raw WiCAN JSON / NMEA / W-Bus → dataclass
  with units converted at the edge; unit is in every field name.
- **Timestamps are UTC** everywhere; convert to local only for display.
- **Derived state survives power loss.** The fuel model's state (gallons
  at last fill, gallons used since) is written to a local state file with an
  atomic write (temp file, `fsync`, rename) at least every 10 s while driving
  and immediately on every fill. That file is what bigair restores from. The
  retained MQTT copy is only for display: Mosquitto saves retained messages to
  disk on an interval, so it can't be the source of truth.
- **No business logic in `main.py`** or in HA YAML templates — HA displays
  and collects input; Python decides.

## Stack & tooling

| Concern | Tool |
|---|---|
| Dependencies | `uv` (`uv add` / `uv sync`; never `uv pip install`) |
| Language | Python 3.12+ (modern syntax) |
| MQTT | Mosquitto; `aiomqtt` client (added in M1) |
| Storage | InfluxDB 2.7, queried with **InfluxQL** via DBRP mapping (no Flux) |
| Dashboards | Grafana, provisioned from `grafana/provisioning/` |
| Home automation | Home Assistant Container, YAML packages in `homeassistant/packages/` |
| Embedded | ESPHome for future sensor nodes (heater, loads); the dash display runs the eink-screen firmware (`~/src/eink-screen`, PlatformIO + epdiy) |
| GPS | `gpsd` on the Pi host (Debian package), reading the Bluetooth GPS via `/dev/rfcomm0` (Decision #28) |
| Lint / format / types | `ruff` (line length 100), `mypy --strict`, `yamllint` |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Logging | `loguru` (never `print()` for operational output) |
| Deploy | Docker Compose on Raspberry Pi OS (`deploy/`) |

## Project structure

```
src/bigair/          # all Python; one small module per concern
  signals.py         #   frozen dataclasses shared by every module
  obd_decode.py      #   ELM327 responses → ObdSample (register signals only)
  wican_elm.py       #   edge: WiCAN ELM327 TCP poller (Decision #33)
  fuel_model.py      #   tank level, gallons used, range, mpg (Critical)
  state_file.py      #   atomic, fsync'd state file (fuel model's durable state)
  fill_detect.py     #   fill-up detection state machine (Critical)
  heater.py          #   Webasto fuel accounting (Critical)
  fuel_plan.py       #   fuel-stop planner: where/how much to fill on a route
  fuel_sources.py    #   route/station/price-trend decoders for the planner
  google_io.py / eia_io.py   # edges: Google Routes + Places, EIA prices (#71)
  route_weather.py   #   weather along the route: sampling, conditions at ETA (#75)
  trip_service.py    #   whole trip plan (fuel + weather) for CLI and planner page
  planner_server.py  #   edge: trip planner page + API, port 8092 (#75)
  weather_io.py      #   edge: Open-Meteo forecasts, NWS alerts
  gps_logger.py      #   gpsd TPV → GpsFix, track-point cadence
  gpsd_io.py         #   edge: gpsd JSON client
  poi.py / poi_near.py  # vault POI notes → Poi; POIs in reach of a planned route (#88)
  travel_rules.py / day_plan.py  # slow mode: rules, day budget, POI score, proposal (#90)
  destinations.py / dest_sources.py  # where next: candidates, ranking; iOverlander + public land (#91)
  chain.py           #   rough chain of legs and stays to a far goal (#92)
  byways.py          #   scenic byway miles on a route; most scenic alternative (#100)
  claude_agent.py    #   Claude tool loop, spend ledger, proposals (#89)
  claude_tools.py / claude_service.py / claude_io.py  # planner tools + state; SDK edge
  telemetry.py / influxql_io.py  # ask the van: read-only InfluxQL checks; briefing schedule (#94)
  victron_ble.py     #   Victron Instant Readout decoder: AES-CTR + bit layouts (#109)
  victron_io.py      #   edge: BLE advertisements via bleak / BlueZ D-Bus (#109)
  electrical.py      #   latest Victron reading per device → MQTT state, Influx points (#109)
  mqtt_io.py / influx_io.py   # edges (M1)
  main.py            #   wiring only
tests/               # pytest, runs anywhere, no hardware
tests/fixtures/      # synthetic data with known-good answers (never real drives)
homeassistant/       # HA config: configuration.yaml + packages/*.yaml
grafana/provisioning/  # datasources/ and dashboards/*.json
deploy/              # docker-compose.yml, bootstrap.sh (Pi setup), host/ (systemd + gpsd config), mosquitto.conf, .env.example
                     #   mac/: planner copy + HA/Grafana relays on a Mac over Tailscale (#105)
docs/                # pid-register.md, carplay.md, hardware notes; docs/archive/ dated
.claude/skills/      # domain skills (see Claude Code specifics)
```

## Common commands

```bash
uv sync                                         # install deps (run first)
uv run pytest                                   # all tests + coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy src/
uv run yamllint homeassistant grafana deploy .github   # config lint
uv run bigair display --port 8090               # dash gauge server (preview: /api/display.png)
uv run bigair fuel-plan trip.json --tank-gal 40 --mpg 15   # fuel stops per reserve depth (docs/fuel-plan.md)
uv run bigair planner --port 8092                # trip planner page: fuel stops + weather on a map
docker compose -f deploy/docker-compose.yml config   # validate the stack
uvx --from shellcheck-py shellcheck -x deploy/*.sh   # lint shell scripts

# On the Pi (docs/hardware/pi-setup.md):
./deploy/bootstrap.sh       # idempotent install, secrets, start + provision the stack
```

All checks (tests, ruff check, ruff format, mypy, yamllint) must pass
before a PR.

## Data model (InfluxDB)

**Buckets.** `van_raw` — native-rate signals, 400-day retention.
`van_history` — infinite retention: 1-minute aggregates (Influx task, M1),
`track`, `fill_up`, `trip` and all event measurements.

**Units.** Fuel, distance and speed in US customary (`_gal`, `_mi`, `_mph`,
`_mpg`) because every human input (pump receipt, odometer) is US. Everything
else SI (`_c`, `_kpa`, `_v`, `_a`, `_w`, `_g_s`, `_ratio` for dimensionless, `_lph` only
where the raw source is litres). **Every field name ends in its unit.**

**Tags are low-cardinality only** (source, method, circuit, device). Never
tag with trip IDs, prices, coordinates or free text.

| Measurement | Bucket | Tags | Key fields |
|---|---|---|---|
| `engine` | raw | `source` (wican) | `rpm`, `coolant_temp_c`, `oil_temp_c`, `intake_air_temp_c`, `maf_g_s`, `faer_ratio`, `engine_load_pct`, `boost_kpa`, `fuel_rate_lph` |
| `vehicle` | raw | `source` | `speed_mph`, `odometer_mi`, `battery_v`, `ambient_temp_c` |
| `driver` | raw | `source` (wican, can) | `throttle_pct`, `cruise_active`, `cruise_set_mph`, `brake_active` |
| `fuel` | raw | `method` (fuel_rate, maf_faer, maf_fixed) | `tank_gal`, `engine_used_gal`, `heater_used_gal`, `range_mi`, `mpg_rolling`, `mpg_instant_30s`, `fuel_rate_gph`, `stale`, `calibrating` |
| `heater` | raw | `method` (wbus_pulses, runtime_datasheet, current) | `running`, `power_level`, `pump_hz`, `fuel_gph`, `used_gal` |
| `electrical` | raw | `device` (bmv, mppt, dcdc) | `voltage_v`, `current_a`, `power_w`, `soc_pct`, `consumed_ah`, `time_to_go_s`, `alarm`, `aux_voltage_v`, `midpoint_voltage_v`, `temperature_c` (bmv); `pv_power_w`, `yield_today_wh`, `load_current_a` (mppt); `input_voltage_v`, `off_reason` (dcdc); `state`, `charger_error` (mppt, dcdc) |
| `load` | raw | `circuit` (fan, webasto, lights, internet, inverter, pi) | `current_a`, `voltage_v`, `power_w` |
| `track` | history | `source` (gps_bt) | `lat`, `lon`, `alt_m`, `speed_mph`, `heading_deg`, `mpg_30s`, `grade_pct`, `hdop`, `trip_id` |
| `fill_up` | history | `source` (live, manual, historical), `fill_type` (full, partial), `status` (confirmed, pending, skipped) | `gallons`, `price_per_gal`, `total_cost`, `odometer_mi`, `lat`, `lon`, `tank_before_gal`, `tank_after_gal`, `mpg_since_full`, `fill_id` |
| `trip` | history | — | `distance_mi`, `fuel_gal`, `mpg`, `duration_s`, `idle_s`, `cruise_share`, `brake_events`, `trip_id` |

Adding a signal: `/influx-schema` (update this table in the same PR).

## Testing

- **TDD for anything with logic:** failing test → implement → green → lint.
  Mandatory and Critical-tier for `fuel_model.py`, `fill_detect.py`,
  `heater.py`, the historical importer and any decoder/scaling function.
- **Tests run without hardware, network or a broker.** Logic modules take
  dataclasses; edges are mocked at the import site.
- **Fixtures are synthetic with known-good answers.**
  `tests/fixtures/drive_synthetic/` is a 1 Hz drive (idle, city, highway, a
  full fill-up, post-fill driving) generated by `generate.py` from a segment
  table; `expected.json` holds hand-derived tank, mpg and range values. A
  test regenerates the CSV and fails if it was hand-edited — change the
  generator, not the CSV.
- **Acceptance tests for unbuilt features are `xfail(strict=True)`** with
  the issue number in `reason`. When the feature lands they XPASS → fail →
  you remove the marker. Never delete one to get green.
- Config is tested too: Grafana JSON schema checks and yamllint run in CI.

## Risk tiers

A PR's tier is the highest tier of any file it touches.

| Tier | Files | Extra requirements |
|---|---|---|
| **Critical** | `fuel_model.py`, `fill_detect.py`, `heater.py`, Influx schema (Data model table, retention tasks) | TDD, fixture-based acceptance test, worked example in the PR body |
| **High** | decoders/scalings, `historical/`, `homeassistant/packages/fuel*.yaml`, `display_render.py` | Tests where logic exists; on-van verification noted before VERIFIED |
| **Standard** | other Python | Tests |
| **Low** | docs, dashboards, CI, skills | Lint |

## Agent workflow

- **One issue → one worktree → one branch → one PR.** Branch and worktree
  name: `<issue>-<slug>` (e.g. `12-fuel-integrator`).
- **Conventional commits**, small and frequent: `type(scope): summary`,
  types `feat fix docs test refactor ci chore deps`; scopes match modules or
  areas (`fuel`, `fill`, `gps`, `ha`, `grafana`, `esphome`, `heater`,
  `electrical`, `skills`). Reference the issue: `feat(fuel): integrate
  fuel rate (#12)`.
- **PRs use the template**; state the risk tier; Critical PRs include a
  worked numeric example.
- **Hardware-gated issues** (anything needing the real van) get the
  `needs-van` label and can't close until the on-van check is recorded.
- **Milestones live in `ROADMAP.md`**; M1+ tasks are GitHub issues labelled
  with their milestone (`M1`, …).

## Claude Code specifics

- **Skills** live in `.claude/skills/<name>/SKILL.md`; the harness lists
  them each session. Domain skills: `/pid-discovery`, `/can-sniffing`,
  `/fuel-model`, `/fill-up-events`, `/heater-accounting`, `/influx-schema`,
  `/grafana-dashboards`, `/home-assistant-config`, `/dash-display`,
  `/electrical`, `/historical-import`. Process skills: `/tdd`,
  `/pr-checklist`. Skills hold tribal knowledge and procedures that the code
  can't show — when code and skill disagree, the code wins and the skill
  gets fixed.
- **Memory is private; the repo is shared.** A gotcha another agent would
  need belongs in a skill or this file, not only in memory.

## Decisions

Dated, newest last. Supersede an entry by adding a new one that names it.

1. **2026-11-16 — CLAUDE.md is canonical; no AGENTS.md.** HelmLog keeps
   AGENTS.md + an `@AGENTS.md` import because it is multi-tool and its two
   files had drifted. BigAir is Claude-Code-only and the brief specifies
   CLAUDE.md; one file can't drift. Revisit if another agent tool is used.
2. **2026-11-16 — Docker Compose on Pi OS, not HA OS** (Dan's choice).
   Custom Python services, Grafana provisioning from git and a reproducible
   stack matter more than the add-on store.
3. **2026-11-16 — InfluxDB 2.7 with InfluxQL.** HelmLog's choice is 2.x; HA
   and Grafana support it. Queries use InfluxQL through a DBRP mapping
   because Flux is in maintenance mode — keeps a later move to InfluxDB 3
   cheap.
4. **2026-11-16 — MQTT backbone, bigair sole Influx writer** (see
   Architecture). HA-only devices reach MQTT via `mqtt_statestream`.
5. **2026-11-16 — Mixed units with mandatory suffixes.** US for
   fuel/distance/speed (human inputs are US), SI otherwise.
6. **2026-11-16 — Two buckets** (`van_raw` 400 d, `van_history` ∞). Raw
   1 Hz is ~0.5 GB/yr — affordable on an SSD, but keeping it bounded
   protects the Pi; aggregates and events are tiny and kept forever. Pi
   should boot from SSD, not SD card.
7. **2026-11-16 — Community W906 work does not transfer.** The WiCAN W906
   fork (wican-fw #947, `mcgyver78/WiCAN_W906`) targets the EU OM651
   (Delphi CRD3) and states OM642 is not covered; its DIDs (e.g. tank level
   `6038` in litres from the stock sender) are leads only. #520's auto-scan
   on a 2006 W906 found mode 01 PIDs incl. MAF (`10`) and FAER (`24`) but not
   fuel level (`2F`) or fuel rate (`5E`). Our van is a US OM642 — scan it.
8. **2026-11-16 — Diesel fuel-rate fallback order.** PID `5E` → Mercedes
   injection-quantity DID × rpm (if found) → MAF × equivalence ratio (FAER)
   → MAF at fixed AFR. Diesels run lean at variable AFR, so plain MAF is a
   last resort; every method has a pump-receipt correction factor
   (`/fuel-model`).
9. **2026-11-16 — M0 builds no feature logic.** Acceptance tests for the
   fuel model and fill detection ship as `xfail(strict=True)` against the
   synthetic fixture; M1 turns them green. Matches "design in M0, build
   in M1+".
10. **2026-11-16 — HelmLog's data-licensing policy and CI check dropped;
    privacy rule added.** Single owner, no co-op sharing — but the repo is
    public, so no personal data in git.
11. **2026-11-16 — No stage/live promotion or RELEASES.md gate.** One
    device, one owner. Deploy flow is an M1 issue.
12. **2026-11-16 — Risk tiers re-mapped** to what can strand the van (fuel
    and fill logic, schema) rather than HelmLog's auth/federation.
13. **2026-11-16 — Skill evals (`evals/cases.yaml`) deferred to M1.** The
    skills are new; evals need real failure cases to be worth writing.
14. **2026-11-16 — mypy strict with zero baseline.** HelmLog carries an
    inherited error count; a new repo starts clean.
15. **2026-11-16 — AGPL-3.0-or-later**, matching HelmLog.
16. **2026-11-16 — CarPlay can't show sensor values.** Companion CarPlay
    lists actions/toggles for supported domains (no `sensor`), plus
    climate/vacuum screens (`docs/carplay.md`). Fuel/range readout in the cab
    comes from the dash display; CarPlay gets actions (log fill-up, heater).
17. **2026-11-16 — Dash display v0: LilyGo T5 e-paper + LoRa board on
    hand** (Dan's choice; a better device later). Battery, ESPHome deep
    sleep, Wi-Fi to HA's native API; LoRa unused. Exact board/panel revision
    UNVERIFIED until read off the board. Sparkline comes from a precomputed
    compact HA sensor so one wake = one small fetch. **Superseded by #29.**
18. **2026-11-16 — Per-circuit current: INA228 + shunts on I²C**
    (Dan's choice). Accurate at fan/LED currents; up to 16 addresses per bus.
19. **2026-11-16 — USB GPS via gpsd** container; gps_logger is a gpsd
    client, not a serial parser.
20. **2026-11-16 — M0 bootstrap as stacked PRs** (foundations → skills →
    scaffold → roadmap), merged in order by Dan. Lesson: squash-merging a
    stack makes each later PR conflict. Avoid stacking, or merge main into
    the next branch and check the tree is unchanged.
21. **2026-11-17 — The Pi is always on, powered from the house battery**
    (Dan's choice). That means fewer hard power cuts than switched power, but
    the Pi becomes a constant load (roughly 4–6 W, so 8–12 Ah/day at 12.8 V;
    measure it). It's metered as circuit `pi`, and it needs a clean shutdown
    before the battery's low-voltage cutoff. Hard cuts still happen (battery
    disconnect, LVD), so durable state is still required (Decision #22).
    Supply, draw and shutdown: issue #24.
22. **2026-11-17 — Fuel state lives in an fsync'd local state file, not
    retained MQTT** (see Architecture). Mosquitto's default retained-message
    save interval is 30 min, so a power cut could silently roll the tank
    back. `autosave_interval` is cut to 60 s, but only for display copies.
23. **2026-11-17 — ESPHome telemetry nodes publish straight to MQTT**
    (amends #4). The W-Bus heater and INA228 load nodes use ESPHome's `mqtt`
    component, so fuel and energy accounting doesn't depend on HA being up.
    `mqtt_statestream` is only for devices HA alone can reach (Victron
    Bluetooth). The dash display keeps the native API, because it consumes HA
    state.
24. **2026-11-17 — System time comes from GPS.** A Pi has no real-time clock
    (a Pi 5 has one but needs a battery fitted) and the van is often offline,
    so after a cold boot every timestamp would be wrong. Run chrony with a
    gpsd refclock; bigair refuses to write to Influx until the clock is synced
    (tracked in issue #22). Implemented 2026-11-19 with
    `deploy/host/chrony-gps.conf` (SHM 0, `offset 0.095` measured for the XGPS160
    over Bluetooth, loose precision so NTP wins online). Offline failover to GPS
    verified. bigair's write guard (`clock.py`, 2026-11-21) reads the kernel's
    sync status via `adjtimex`, which chrony maintains; it works in the container
    without privileges. InfluxDB writes are buffered until it reports synced.
25. **2026-11-19 — Bring-up on an SD card; SSD before the van install**
    (Dan's choice). Amends #6 for the bench phase only. Docker log rotation
    (10 MB × 3) limits SD wear in the meantime. Setup is headless (SSH, plus
    the web UIs) via the idempotent `deploy/bootstrap.sh`, which also does the
    InfluxDB bucket, DBRP and token provisioning from issue #6. The bootstrap
    never touches networking while the van Wi-Fi is undecided.
26. **2026-11-19 — No `mqtt_statestream` until there's an entity to forward.**
    The M0 placeholder had an empty include list, which HA treats as "publish
    every entity" (checked against HA 2026.11 on the Pi). It also failed at
    first boot, because the MQTT integration is added in the UI after HA
    starts. Removed; it returns in M3 with the Victron entity list.
    `tests/test_ha_config.py` blocks an empty include.
27. **2026-11-19 — Explicit HA integrations, no `default_config`** (Dan:
    "delete things while we know we don't need them"). `default_config`
    loaded network discovery, cloud, energy, voice and backup. On the first
    bring-up that auto-added WeMo, Govee, Thread, Met, Sun, Shopping list and a
    dozen ignored discoveries from the home network. `configuration.yaml` now
    lists only frontend, history, logbook, mobile_app and system_health, and
    every addition names the feature that needs it. Bluetooth comes back in M3
    (Victron); leaving it out meanwhile frees the radio for the XGPS160 trial.
    The 20 auto-added config entries were deleted via HA's REST API, leaving
    only `mqtt` and `mobile_app`. Three always return, because HA core loads
    them unconditionally (`bootstrap.DEFAULT_INTEGRATIONS` in 2026.11):
    `analytics` (never opted in, sends nothing), `backup` (no schedule) and
    `marketplace`. They're inert, and removing them would mean patching HA,
    so leave them.
28. **2026-11-19 — gpsd runs on the Pi host, reading Dan's XGPS160 over
    Bluetooth** (Dan's choice; supersedes #19's container, and the brief's
    "USB GPS preferred"). The trial passed: Serial Port profile on channel 1,
    3D fix at 10 Hz, about 2.9 m accuracy, and automatic recovery after a
    receiver power cycle. The RFCOMM binding (`rfcomm-gps.service`) and
    chrony's SHM refclock (#22) both live on the host, so gpsd does too: the
    Debian package with `-b -n`. **`-b` (read-only) is required**, because
    gpsd's default probing makes the RFCOMM link return I/O errors. The MAC
    lives only in `deploy/.env`. gpsd listens on localhost; bigair reaches it
    with host networking (Decision #35).
29. **2026-11-19 — Dash display: eink-screen firmware + server-rendered
    frame** (Dan's choice; supersedes #17). The board is a **LilyGo T5 ePaper
    S3 PRO** (ESP32-S3, 4.7" 960×540 16-grey parallel e-paper, touch, LoRa,
    GPS), not a 2.13" panel, and ESPHome can't drive it. Dan's
    `~/src/eink-screen` firmware (PlatformIO, epdiy V7 driver) already does,
    as a thin client of HelmLog's `/api/display.raw`. BigAir serves the same
    contract (`bigair display`, port 8090: 259,200-byte 4-bit frame, Bearer
    key, refresh headers, `?screen=` ignored), so **one unit works on the boat
    and in the van**: the firmware locks onto whichever server answers on the
    network it joins. Layout lives in `display_render.py`, so restyling never
    needs a reflash. Hard-won board rules (epdiy not LilyGo-EPD47, never
    `Wire.begin()`, always pass `--upload-port`) live in eink-screen's
    `AGENTS.md`. The ESPHome display config is deleted.
30. **2026-11-19 — Tailscale on bigair-ha for remote access** (Dan's request).
    It joins Dan's tailnet, installed by the bootstrap when `TAILSCALE_ENABLE=true`,
    with Tailscale SSH on and **MagicDNS off** (`--accept-dns=false`), so Docker's
    DNS and offline operation never depend on tailscaled. It's a convenience
    only: nothing in the stack depends on it, and the van works fully offline
    without it. The ESP32 devices (WiCAN, LilyGo) can't join a tailnet, so they
    reach the Pi on the van LAN.
31. **2026-11-19 — Van network and the fuel sender** (Dan). A router in the van
    provides Wi-Fi **`bigair`**. The Pi, WiCAN, LilyGo and phone all join it,
    and the Pi keeps its home Wi-Fi profile too. There's **no aftermarket level
    sender: the stock sender was moved to the 47-gal tank**. Its reading, if
    the ECU or cluster exposes it (UNVERIFIED, #4), is used only as a fill
    *delta* detector, through a calibration table learned from fill-ups
    (sender reading vs model gallons). It's never the tank level, because the
    tank's shape differs from the one the sender was made for.
32. **2026-11-20 — Tank and range are shown while `5E` is calibrating** (Dan's
    choice). `5E` responds with plausible values but stays RESPONDS until a
    full-to-full pump comparison over at least 100 mi. Collecting that needs the
    pipeline running, so tank and range are published flagged `calibrating`
    (HA attribute and a dash label) rather than hidden. This is a deliberate,
    bounded exception to "never show an unverified number as fact": the flag
    *is* the disclosure. It clears automatically when `5E` is VERIFIED and the
    calibration factor is applied.
33. **2026-11-20 — bigair polls the WiCAN's ELM327 interface over TCP**
    (amends #4 for OBD; supersedes the AutoPID→MQTT plan in #5). On the van,
    TCP 35000 polling of the engine ECU was proven in the first scan and drive.
    WiCAN AutoPID's MQTT payload format would have been a guess. Decoders are
    tested from the raw bytes captured on the van (`obd_decode.py`), and the
    poller (`wican_elm.py`) is an edge like the gpsd client. bigair still
    publishes everything derived to MQTT, so the backbone is unchanged for every
    consumer. Only one TCP client at a time: stop the drive logger while bigair
    runs.
34. **2026-11-21 — No provisional tank update while a detected fill is
    pending** (supersedes the "provisionally applies the level-estimated
    gallons" line in `/fill-up-events`). The estimate comes from the stock
    sender through an uncalibrated table (Decision #31), so adding it could be
    off by many gallons and would look confident. Until Dan answers, the tank
    stays at its pre-fill value, an underestimate (the safe direction). HA and
    the dash show **FILL PENDING**. When he answers, the fill is applied as of
    the detection time, so nothing is lost. Revisit once the level table is
    learned from real fills.
35. **2026-11-21 — The bigair service uses host networking** (decides #14's open
    question from Decision #28). gpsd listens only on the host's localhost, which
    is the safe default since it's unauthenticated, and Mosquitto and InfluxDB
    already publish their ports on the host. With `network_mode: host`, bigair
    reaches gpsd, MQTT (`localhost:1883`) and InfluxDB (`localhost:8086`) like
    Home Assistant does, so gpsd never listens on the LAN. The display container
    stays on the Compose network; it only reads the shared gauge-feed volume.
36. **2026-11-21 — Touch-selectable dash screens** (Dan's request, #62). The
    eink-screen firmware's touch menu (from HelmLog) now works in the van:
    bigair serves `/api/display/menu` and `menu.raw` and renders by `?screen=`
    (Fuel, Trip, Economy, Van). Unknown ids still get the fuel screen, so a
    HelmLog id carried over from the boat never blanks the gauge (amends #29's
    "`?screen=` ignored"). The Pi-supply debug strip moved off the fuel screen
    to the Van screen, so the fuel screen always shows its sparkline.
37. **2026-11-21 — The dash preview is keyless but tailnet-only** (Dan's
    request, #62). A read-only preview server (PNGs and a screen-flipping page,
    never `.raw` frames) runs beside the device server. Compose publishes it on
    `127.0.0.1:8091` only, and bootstrap runs `tailscale serve --http=8091` to
    expose it at `http://bigair-ha.<tailnet>.ts.net:8091/`. Serve routes by
    hostname, so the bare tailnet IP returns 404. Tailnet membership is the access
    control. The van LAN never sees it, and the device keeps its Bearer key.
    Like Decision #30, nothing depends on it.
38. **2026-11-22 — Grafana allows anonymous Viewer access** (Dan's request, #65).
    The phone opens the `bigair-phone` dashboard in Safari (Add to Home Screen,
    `?kiosk`) with no login, on the van Wi-Fi or over Tailscale (Dan's request).
    Viewer can't edit or add dashboards (an API write returns 403). Grafana is
    reachable only on the van LAN and the tailnet, but anyone on the van Wi-Fi can
    see the dashboards, including the GPS track map. `GRAFANA_ANONYMOUS=false` in
    `deploy/.env` turns it off. The `fuel` measurement gained `stale` and
    `calibrating` booleans so the live tank panel shows "No fresh data" rather than
    an old number.
39. **2026-11-22 — The phone is the dash; HA's "Van" dashboard is provisioned from
    git** (Dan's request, #64). The iPhone took the LilyGo's dash mount. The
    Companion app works offline on the van Wi-Fi and already has the fill-up flow,
    so the phone dash is a YAML-mode HA dashboard (`homeassistant/dashboards/
    van.yaml`, built-in cards only, no HACS) rather than a new app. bigair publishes
    the Trip and Van screen values on a retained `bigair/van/state` topic (no
    position). **HA ignores discovery `object_id`**: entity ids come from the device
    and entity names (`sensor.bigair_van_fuel_tank`), so renaming a discovered
    entity breaks the dashboard; `tests/test_ha_dashboard.py` catches it.
40. **2026-11-23 — The fuel-stop planner takes a trip file and compares reserve
    depths** (Dan's request, #70). P1 reads a JSON trip file (route miles plus
    stations with mile markers and prices) rather than GPX. Projecting stations
    onto a GPX route needs the routing and places APIs, so it moves to #71. Every
    run prints one plan per reserve depth (default 150/100/75/50 mi) next to a
    "fill at 1/4" baseline, so Dan chooses how deep into the reserve to go. While
    calibrating, plans use `mpg × 0.93`. Stale fuel data refuses to plan.
41. **2026-11-23 — Live fuel-plan sources** (#71; Dan approved a billed Google
    key). Google Routes + Places API (New) search-along-route supply the route
    and the posted diesel prices (`fuelOptions`). EIA weekly `EPD2D` supplies the
    trend: `SCA` for California, `R5XCA` for the rest of the West Coast (WA and OR
    have no series), capped at ±2¢/day. Dan is an **Open Roads** member, so
    Love's, TA and Petro are priced at posted −$0.30 by default (the low end of
    the reported 30–50¢). It's a planning estimate, not the pump price. The
    planner is a CLI on the Mac for now; it reads the tank over Tailscale MQTT.
    The van runs it in #72.
42. **2026-11-24 — The trip planner runs on the Pi as one web page, embedded in HA**
    (Dan's request, #75). `bigair planner` (port 8092, host networking) serves the
    page and its API. The Companion app shows the same page in a Van-dashboard
    "Trip" view, so there's one interface rather than an HA-native copy. Weather
    along the route comes from Open-Meteo (free, no key, 16 days hourly, about every
    25 mi) and NWS alerts (generic User-Agent, no contact details). The browser
    re-times the weather for any departure without a new request; classification
    thresholds come from `route_weather.CLASSIFY`. The page has no login because
    it's reachable only on the van LAN and the tailnet. Paid plans are limited to
    one at a time and `PLANNER_DAILY_LIMIT` a day, with the Google spend cap as the
    backstop. Missing weather or alerts only add a warning; a missing route or fuel
    state, or stale fuel data, means no plan. Tests block outbound connections
    (`tests/conftest.py`).
40. **2026-11-25 — Vault POIs reach the Pi by Syncthing, read-only** (Dan's choice,
    #88). The trip planner reads Dan's road-trip POI notes (Obsidian frontmatter:
    `location`, `tags`, plus optional `visit_min`, `visit_what`, `best_months`,
    `destination` and `rating`). Syncthing sends only that folder one way, from the
    Mac (Send Only) into gitignored `data/poi` on the Pi (Receive Only), and the
    planner mounts it read-only. The notes are personal, so they never go in git,
    and tests use synthetic POIs. The reach selector (round-trip detour / each way /
    ahead) is Dan's to set on the page. The detour is twice the one-way drive from
    the nearest route point, which errs long. A missing visit time or failed lookup
    shows "unknown" and is never guessed. **300 mi/day is a ceiling, never a
    target** (Dan): the scenic planner (#90–#92) must never score longer days higher.
41. **2026-11-25 — Claude advises, Python decides** (Dan's interview, #89). bigair calls
    Claude (`claude-opus-5-5`, effort medium, server-side refusal fallback) through
    a hand-written tool loop on the Pi, so its spend cap can stop the loop between
    calls. The tools are local and read-only, apart from `propose_route`, which only
    records a suggestion; Dan accepts or dismisses it on the page. Claude never
    changes a plan or estimates fuel. It may see POI notes, plans, the GPS position,
    the forecast and (C3) telemetry, but never Dan's personal planning notes; the
    travel rules come from bigair's settings. Offline, it shows "offline" and the
    planner works without it. The spend cap is $25/month (`CLAUDE_MONTHLY_CAP_USD`,
    kept durably in `data/bigair/claude-spend.json`), backed by the Console spend
    limit. Showing spend in HA/Grafana is deferred until a bigair service owns it
    (single Influx writer); the planner page shows it for now.
42. **2026-11-25 — A Mac can stand in for the phone's path to the Pi** (Dan's request,
    #105). While the phone is the van router's hotspot uplink, it can't reach the Pi
    over Tailscale (#104). `deploy/mac/setup.sh` runs a second `bigair planner` on a
    Mac, listening on the Mac's tailnet address only and reading the tank from the
    Pi's Mosquitto. It also runs byte relays to the Pi's HA (8123) and Grafana (3000).
    The Pi stays the only writer and the only HA. The Mac copy has no GPS position,
    sends no briefing, and keeps its own plan and Claude spend counters (cap $5). The
    relays are stdlib Python, not socat, because the macOS firewall already allows
    uv's Python (`docs/hardware/mac-copy.md`). Like Decision #30, nothing depends on it.
43. **2026-11-25 — Home Assistant is public through the Mac's Funnel; nothing else
    is** (Dan's request, #105). Someone off the tailnet reaches HA at the Mac's
    `ts.net` URL, with HA's login (per-person non-admin users, 2FA) as the gate and a
    5-attempt IP ban. The planner and Grafana have no login, so they stay
    tailnet-only. HA 2026.11 keeps `http:` settings (trusted proxy, ban) in
    `.storage/http`, which git doesn't hold, so the steps live in
    `docs/hardware/mac-copy.md`. If the Pi exposes HA itself later, HelmLog's
    pattern applies (`docs/https-deployment.md` there): Cloudflare Tunnel with Access
    for anything that has no login.
44. **2026-11-26 — The fuel model measures time on the monotonic clock** (#107). A cold
    boot started the Pi's clock 6.55 days behind (the Pi 5's RTC has no battery, and
    chrony doesn't keep timesyncd's clock file). When chrony stepped it at the GPS fix
    while the van was moving, the gap estimate (speed × wall-clock Δt ÷ mpg) booked
    about 169 gal, and the tank showed −129 gal. Now the edge stamps
    `time.monotonic()` on each `ObdSample` and the integrator uses only that for Δt
    and ordering. No gap over an hour is estimated. The bootstrap installs
    `fake-hwclock`, so a cold boot starts near the last shutdown. An RTC battery
    would close the rest. Amends Decision #24: Influx writes already waited for sync,
    but the fuel model didn't need to wait, it needed a clock that never steps.
45. **2026-11-26 — bigair decodes the Victron devices' BLE broadcasts** (#109;
    supersedes the Victron parts of Decisions #4 and #23 and of M3's plan). The
    battery monitor is a **BMV-712 Smart** with built-in Bluetooth, not the BMV-700 over
    VE.Direct that was assumed. With the SmartSolar MPPT 100/30 and the Orion-Tr Smart
    12/12-30, it sends Victron's encrypted Instant Readout. `victron_io.py` (bleak, the
    host's BlueZ over the read-only `/run/dbus` mount) passes the broadcasts to
    `victron_ble.py`, which decodes them to Victron's published layout. HA's Bluetooth stays
    off, so the XGPS160 keeps the radio's attention and there's no `mqtt_statestream`.
    Readings go to `bigair/electrical/state` for HA, and to `electrical` in Influx every
    10 s per device. A device silent for 2 min reads as unknown. Keys live only in
    `deploy/.env` (`VICTRON_<NAME>_MAC`/`_KEY`).
