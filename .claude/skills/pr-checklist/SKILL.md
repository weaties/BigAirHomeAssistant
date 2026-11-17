---
name: pr-checklist
description: Pre-PR verification for BigAir — resolve the risk tier, run the per-tier checks, the unverified-signal and privacy gates, and the documentation-update map. TRIGGER when implementation is complete and the user is ready to open or push a PR ("create a PR", "ready for review", /pr-checklist). DO NOT trigger mid-implementation or during TDD cycles.
---

# /pr-checklist

## 1. Issue hygiene

```bash
gh issue edit <N> --add-label in-progress
gh issue comment <N> --body "In progress on \`<branch>\`"
```
The PR body carries `Closes #N`.

## 2. Resolve the tier

Run `git diff --name-only main...HEAD`. The highest match wins:

| Tier | Paths |
|---|---|
| Critical | `src/bigair/fuel_model.py`, `fill_detect.py`, `heater.py`; the CLAUDE.md Data model table; Influx tasks/retention |
| High | decoders/scalings, `src/bigair/historical/`, `homeassistant/packages/fuel*.yaml` / `fill_up*.yaml`, `esphome/dash_display*.yaml` |
| Standard | other `src/` Python |
| Low | docs, skills, dashboards, CI |

Report it, e.g. "touches `fill_detect.py` → **Critical**". An unlisted `.py`
file defaults to Standard; flag it so the table can be updated.

## 3. Per-tier checks

| Check | Critical | High | Standard | Low |
|---|---|---|---|---|
| pytest | ✔ | ✔ | ✔ | if code |
| ruff check + format | ✔ | ✔ | ✔ | ✔ |
| mypy | ✔ | ✔ | ✔ | if code |
| yamllint / `esphome config` | if YAML | if YAML | if YAML | if YAML |
| Fixture-based acceptance test | ✔ | — | — | — |
| Worked numeric example in PR body | ✔ | — | — | — |
| On-van verification noted | if signal | if signal | — | — |

## 4. Gates (any tier)

- **Unverified signals:** grep the diff for new PID, DID or CAN ID
  constants. Each needs a `docs/pid-register.md` row. Code may only treat it
  as trusted if the row is VERIFIED with evidence.
- **Privacy (public repo):** no real lat/lon, VIN, receipts, photos,
  `secrets.yaml`, SSIDs or passwords. Check `git diff --stat` for files
  under `data/` or large CSVs.
- **xfail markers:** if the feature is built, its `xfail` markers are gone.

## 5. Documentation map

| Change | Update |
|---|---|
| New measurement/field/tag | CLAUDE.md → Data model, `/influx-schema` |
| New module | CLAUDE.md → Project structure; Risk tiers if it's logic |
| New command | CLAUDE.md → Common commands |
| New decision or convention deviation | CLAUDE.md → Decisions (dated) |
| Signal status change | `docs/pid-register.md` + scan write-up |
| New env var | `deploy/.env.example` |
