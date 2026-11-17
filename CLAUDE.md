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
 OBD-II port                 USB on Pi              Bluetooth / VE.Direct      ESP32 nodes (ESPHome)
 ┌──────────┐             ┌──────────┐            ┌──────────────────┐      ┌───────────────────────┐
 │ WiCAN    │             │ USB GPS  │            │ Victron BMV-700, │      │ W-Bus heater (Webasto)│
 │ (OBD/PRO)│             │ → gpsd   │            │ MPPT, DC-DC,     │      │ INA228 load sensors   │
 └────┬─────┘             └────┬─────┘            │ shore charger    │      │ e-paper dash display ◄┼──┐
      │ MQTT                   │ TCP 2947         └───┬──────────────┘      └──────────┬────────────┘  │
      ▼                        ▼                      │ Venus OS MQTT / HA BLE         │ native API     │
 ┌────────────────────────────────────────────────────┴────────────────────────────────┴───────────┐   │
 │ Raspberry Pi (Pi OS, Docker Compose, in-van Wi-Fi hotspot, no internet required)                │   │
 │                                                                                                  │   │
 │   Mosquitto ◄──── all signals flow through MQTT ────► Home Assistant ── Companion app / CarPlay │   │
 │      ▲  │                                              (display, notifications, user input,    │   │
 │      │  ▼                                               mqtt_statestream for HA-only devices) ─┼───┘
 │   bigair services (Python): ingest → decode → fuel model, fill detection, GPS track, heater    │
 │      │   publishes derived sensors (tank_gal, range_mi, mpg) back to MQTT w/ HA discovery     │
 │      ▼                                                                                           │
 │   InfluxDB 2.7  ◄── bigair is the only writer ──►  Grafana (provisioned dashboards, Geomap)     │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Principles:

- **MQTT is the backbone.** Every source publishes to Mosquitto; every
  consumer subscribes. Devices only HA can talk to (Victron BLE) are pushed
  to MQTT with `mqtt_statestream`.
- **bigair is the single InfluxDB writer.** HA's InfluxDB integration is not
  used — its entity-shaped schema would fight ours. One writer, one schema.
- **Hardware isolation.** Only edge modules (`mqtt_io.py`, `influx_io.py`,
  `gps_logger.py`'s gpsd client) touch the network or devices, and only
  `main.py` imports them. Logic modules (`fuel_model.py`, `fill_detect.py`,
  `heater.py`) take and return frozen dataclasses from `signals.py`, so they
  test without hardware.
- **Decode early, store clean.** Raw WiCAN JSON / NMEA / W-Bus → dataclass
  with units converted at the edge; unit is in every field name.
- **Timestamps are UTC** everywhere; convert to local only for display.
- **Derived state survives restarts.** The fuel model's state (gallons at
  last fill, gallons used since) is persisted (retained MQTT + Influx) so a
  reboot never resets the tank.
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
| Embedded | ESPHome (`esphome/`) |
| GPS | `gpsd` container on the USB GPS |
| Lint / format / types | `ruff` (line length 100), `mypy --strict`, `yamllint` |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Logging | `loguru` (never `print()` for operational output) |
| Deploy | Docker Compose on Raspberry Pi OS (`deploy/`) |

## Project structure

```
src/bigair/          # all Python; one small module per concern
  signals.py         #   frozen dataclasses shared by every module
  fuel_model.py      #   tank level, gallons used, range, mpg (Critical)
  fill_detect.py     #   fill-up detection state machine (Critical)
  heater.py          #   Webasto fuel accounting (Critical)
  gps_logger.py      #   gpsd → track points
  mqtt_io.py / influx_io.py   # edges (M1)
  main.py            #   wiring only
tests/               # pytest, runs anywhere, no hardware
tests/fixtures/      # synthetic data with known-good answers (never real drives)
homeassistant/       # HA config: configuration.yaml + packages/*.yaml
esphome/             # one YAML per ESP32 node + common/ fragments
grafana/provisioning/  # datasources/ and dashboards/*.json
deploy/              # docker-compose.yml, mosquitto.conf, .env.example
docs/                # pid-register.md, carplay.md, hardware notes; docs/archive/ dated
.claude/skills/      # domain skills (see Claude Code specifics)
```

## Common commands

```bash
uv sync                                         # install deps (run first)
uv run pytest                                   # all tests + coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy src/
uv run yamllint homeassistant esphome deploy    # config lint
uvx esphome config esphome/<node>.yaml          # validate one ESPHome node
docker compose -f deploy/docker-compose.yml config   # validate the stack
```

All checks (tests, ruff check, ruff format, mypy, yamllint) must pass
before a PR.

## Data model (InfluxDB)

**Buckets.** `van_raw` — native-rate signals, 400-day retention.
`van_history` — infinite retention: 1-minute aggregates (Influx task, M1),
`track`, `fill_up`, `trip` and all event measurements.

**Units.** Fuel, distance and speed in US customary (`_gal`, `_mi`, `_mph`,
`_mpg`) because every human input (pump receipt, odometer) is US. Everything
else SI (`_c`, `_kpa`, `_v`, `_a`, `_w`, `_g_s`, `_lph` only where the raw
source is litres). **Every field name ends in its unit.**

**Tags are low-cardinality only** (source, method, circuit, device). Never
tag with trip IDs, prices, coordinates or free text.

| Measurement | Bucket | Tags | Key fields |
|---|---|---|---|
| `engine` | raw | `source` (wican) | `rpm`, `coolant_temp_c`, `intake_air_temp_c`, `maf_g_s`, `engine_load_pct`, `boost_kpa`, `fuel_rate_lph` |
| `vehicle` | raw | `source` | `speed_mph`, `odometer_mi`, `battery_v` |
| `driver` | raw | `source` (wican, can) | `throttle_pct`, `cruise_active`, `cruise_set_mph`, `brake_active` |
| `fuel` | raw | `method` (fuel_rate, maf_faer, maf_fixed) | `tank_gal`, `engine_used_gal`, `heater_used_gal`, `range_mi`, `mpg_rolling`, `mpg_instant_30s`, `fuel_rate_gph` |
| `heater` | raw | `method` (wbus_pulses, runtime_datasheet, current) | `running`, `power_level`, `pump_hz`, `fuel_gph`, `used_gal` |
| `electrical` | raw | `device` (bmv700, mppt, dcdc, shore) | `voltage_v`, `current_a`, `power_w`, `soc_pct` |
| `load` | raw | `circuit` (fan, webasto, lights, internet, inverter) | `current_a`, `voltage_v`, `power_w` |
| `track` | history | `source` (gps_usb) | `lat`, `lon`, `alt_m`, `speed_mph`, `heading_deg`, `mpg_30s`, `hdop`, `trip_id` |
| `fill_up` | history | `source` (live, manual, historical), `fill_type` (full, partial), `status` (confirmed, pending, skipped) | `gallons`, `price_per_gal`, `total_cost`, `odometer_mi`, `lat`, `lon`, `tank_before_gal`, `tank_after_gal`, `mpg_since_full` |
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
| **High** | decoders/scalings, `historical/`, `homeassistant/packages/fuel*.yaml`, ESPHome display | Tests where logic exists; on-van verification noted before VERIFIED |
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
    compact HA sensor so one wake = one small fetch.
18. **2026-11-16 — Per-circuit current: INA228 + shunts on I²C**
    (Dan's choice). Accurate at fan/LED currents; up to 16 addresses per bus.
19. **2026-11-16 — USB GPS via gpsd** container; gps_logger is a gpsd
    client, not a serial parser.
20. **2026-11-16 — M0 bootstrap as stacked PRs** (foundations → skills →
    scaffold → roadmap), merged in order by Dan.
