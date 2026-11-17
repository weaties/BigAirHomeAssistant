---
name: tdd
description: BigAir-specific test patterns — the synthetic drive fixture, xfail(strict) acceptance tests for unbuilt features, dataclass-in/dataclass-out logic tests, mocking MQTT/Influx/gpsd edges, and decoder tests from raw bytes. The red-green-refactor cycle itself is mandated in CLAUDE.md. TRIGGER when writing or modifying Python under src/bigair/ or tests/. DO NOT trigger for docs, YAML-only config, dashboards or skill edits.
---

# TDD — BigAir patterns

CLAUDE.md mandates failing test → implement → green → lint. This skill
covers the project-specific patterns.

## Logic tests: dataclasses in, dataclasses out

Logic modules (`fuel_model`, `fill_detect`, `heater`) never see MQTT or
Influx. Build `signals.py` dataclasses directly:

```python
from bigair.signals import ObdSample

def test_idle_burns_fuel_without_distance() -> None:
    samples = [ObdSample(ts=t0 + timedelta(seconds=i), speed_mph=0.0, fuel_rate_gph=0.6, ...)
               for i in range(3601)]
    ...
```

## The synthetic drive fixture

- `tests/fixtures/drive_synthetic/` has `drive.csv` (1 Hz),
  `expected.json` (hand-derived answers) and `generate.py` (a segment table
  → CSV).
- Load it with the `drive` fixture from `tests/conftest.py`. Never parse the
  CSV ad hoc in a test.
- **Change the generator, not the CSV.** `test_fixture_is_generated` fails if
  the CSV differs from a fresh generation.
- Expected values are derived by hand from the segment table. Never paste
  output from the code under test into `expected.json`; that's
  test-after.

## Acceptance tests for unbuilt features

```python
@pytest.mark.xfail(strict=True, reason="M1 #<issue>: fuel integrator not built yet")
def test_tank_after_drive_matches_fixture(drive: Drive) -> None: ...
```

`xfail_strict = true` is set in pyproject, so when the feature lands the
test XPASSes and fails. Removing the marker is part of the feature PR.

## Edges

- MQTT (`aiomqtt`) and Influx clients: patch at the import site in the edge
  module (`bigair.mqtt_io.aiomqtt.Client`). Logic tests don't need this.
- Influx serialisation: assert the exact line-protocol string, including
  the float formatting.
- Decoders: the test input is the **raw bytes from a scan write-up**
  (`docs/scans/…`), and the expected value is the reference reading. If
  there's no scan, there's no decoder (`/pid-discovery`).

## Don't rationalize

| Excuse | Rebuttal |
|---|---|
| "It's just a unit conversion." | Gallons vs litres is precisely the bug that strands a van. Test it. |
| "I'll update expected.json to match the output." | Then the test proves nothing. Re-derive by hand, or find the bug. |
| "The xfail is in the way, I'll delete it." | It's the acceptance criterion. Make it pass, then remove the marker. |
| "mypy is noisy here." | The baseline is zero. Any error is yours. |
