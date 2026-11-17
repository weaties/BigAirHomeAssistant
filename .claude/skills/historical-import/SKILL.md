---
name: historical-import
description: One-time (re-runnable) import of past fill-ups — CSV exported from Dan's Apple Numbers log into the fill_up measurement with source=historical, matching each fill to an iPhone photo for GPS and time, flagging unmatched or ambiguous photos for review, and re-running idempotently. TRIGGER when working on src/bigair/historical/, its tests or fixtures, or the historical fill-up map, mpg/cost history, or before/after-tank dashboards. DO NOT trigger for live fill detection (use /fill-up-events).
---

# Historical fill-up import

## Inputs

1. **Numbers spreadsheet → CSV.** Columns, date format, units and how
   partial fills are marked are all **unknown**. **Ask Dan for a sample export
   before writing the parser.** Don't guess column names. When the sample
   arrives, build a synthetic look-alike fixture (same headers and quirks,
   fake values) in `tests/fixtures/historical/`. The real file stays in
   `data/`, which is gitignored.
2. **Photos (Apple Photos, iPhone)**, one per fill-up, with GPS and time:
   - Preferred: `osxphotos query --from-date … --to-date … --json` on the Mac.
     It reads date, timezone, latitude and longitude from the library
     without exporting images.
   - Alternative: export unmodified originals ("Export Unmodified Original")
     and read EXIF with `exiftool -json -n -DateTimeOriginal
     -OffsetTimeOriginal -GPSLatitude -GPSLongitude`. Edited exports can
     drop metadata.
   - The metadata JSON goes in `data/`, never committed. Locations are
     personal.

## Matching photos to fills

- Key = the **local calendar date** of the photo (using its own timezone
  offset) vs the spreadsheet date.
- Exactly one photo and one fill on a date → **matched**. The fill gets the
  photo's timestamp (UTC) and lat/lon.
- More than one photo or more than one fill on a date → **ambiguous**.
  Otherwise no photo → **unmatched fill**. A photo with no fill →
  **orphan photo**.
- **Never guess.** Ambiguous and unmatched rows go to
  `data/historical_review.csv` with the candidates listed. Dan resolves them
  there (by filling in a `photo_id` column) and the import is re-run.
- Unmatched fills are still imported, timestamped at local noon of their
  date with no lat/lon and `located=false`.

## Writing

- Measurement `fill_up`, bucket `van_history`, tags `source=historical`,
  `fill_type`, `status=confirmed`. Fields as in `/fill-up-events`, plus
  `located`.
- `fill_id` = a deterministic hash of (date, odometer, gallons), so the same
  row always gets the same id and timestamp. **Re-running overwrites rather
  than duplicating.** Changing the matching rules may move a timestamp, so
  in that case delete `source=historical` points first (a documented command
  in the importer's `--help`), then re-import.
- `mpg_since_full` is computed with the same function the live model uses
  (`/fuel-model`). Don't reimplement it.
- Dry-run by default; `--write` actually writes. It prints a summary:
  rows, matched, ambiguous, unmatched, and orphan photos.

## Outputs

- A Grafana Geomap of every past fill, plus mpg and cost-per-mile history
  (`/grafana-dashboards`).
- A **before/after comparison around the 47-gal tank install.** The install
  date is a config value (ask Dan). Use annotations on the history
  dashboards.
- A **baseline mpg prior** for the live fuel model (`/fuel-model` → mpg
  definitions).
