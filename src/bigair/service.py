"""bigair service core (#53): samples → fuel model + fill detector → outputs.

No network here: main.py wires the WiCAN poller, MQTT and InfluxDB around it.
Owns the durable state file (Decision #22): saved at most every 10 s while
samples arrive and immediately on every fill.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from loguru import logger

from bigair.fill_detect import FillDetector, FillDetectorConfig
from bigair.fill_input import FillDecision, FillInput, parse_response
from bigair.fill_prompt import FillPrompter
from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.gps_logger import TrackRecorder
from bigair.ha_discovery import (
    FILL_RESPONSE_TOPIC,
    FILL_RESULT_TOPIC,
    MANUAL_FILL_TOPIC,
    fuel_state_payload,
)
from bigair.influx_io import fill_up_point, fuel_point, track_point, vehicle_point
from bigair.signals import FillSource, FillStatus, FillType, FillUp, FuelState
from bigair.state_file import StateFileError, StateSaver, read_state
from bigair.trip import TripMpg

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from bigair.influx_io import Point
    from bigair.signals import FillCandidate, GpsFix, ObdSample

SILENT_WHILE_MOVING = timedelta(seconds=60)  # no samples this long after moving → stale
DUPLICATE_WINDOW = timedelta(minutes=10)  # same fill numbers again this soon = double tap
VOLTAGE_MAX_AGE = timedelta(seconds=30)  # older van voltage isn't shown (WiCAN asleep)


@dataclass(frozen=True)
class ServiceConfig:
    state_path: Path
    capacity_gal: float = 47.0
    prior_mpg: float = 15.0
    save_interval_s: float = 10.0
    fuel_rate_verified: bool = False  # flip when 5E is VERIFIED (Decision #32)
    fuel_method: str = "fuel_rate"


def _skipped_fill(c: FillCandidate) -> FillUp:
    return FillUp(
        fill_id=c.fill_id,
        ts=c.ts,
        source=FillSource.LIVE,
        fill_type=FillType.FULL,  # unknown; the tag needs a value, status says it was skipped
        status=FillStatus.SKIPPED,
        gallons=None,
        odometer_mi=c.odometer_mi,
        lat=c.lat,
        lon=c.lon,
    )


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class FuelService:
    def __init__(self, config: ServiceConfig, model: FuelModel | None) -> None:
        self.config = config
        self.model = model
        self.detector = FillDetector(FillDetectorConfig(capacity_gal=config.capacity_gal))
        self.saver = StateSaver(config.state_path, config.save_interval_s)
        self.last_sample: ObdSample | None = None
        self.trip = TripMpg()
        self._outbox: deque[tuple[str, str]] = deque()  # (topic, payload) to publish
        self._points: deque[Point] = deque()  # event points for Influx (fill-ups)
        self._last_input: tuple[FillInput, datetime] | None = None
        self.prompter = FillPrompter()
        self.track = TrackRecorder()
        self.last_fix: GpsFix | None = None

    @property
    def model_config(self) -> FuelModelConfig:
        return FuelModelConfig(
            capacity_gal=self.config.capacity_gal, prior_mpg=self.config.prior_mpg
        )

    @classmethod
    def start(cls, config: ServiceConfig) -> FuelService:
        """Restore from the state file; no file means the tank is unknown until a fill."""
        model = None
        try:
            data = read_state(config.state_path)
        except StateFileError as e:
            logger.error("fuel state unreadable, tank unknown until the next full fill: {}", e)
            data = None
        svc = cls(config, None)
        if data is not None:
            model = FuelModel.from_snapshot(svc.model_config, data)
            logger.info("fuel state restored: {:.2f} gal", model.tank_gal)
        else:
            logger.warning("no fuel state yet: press 'Filled to full' to start the tank")
        svc.model = model
        return svc

    def handle_sample(self, sample: ObdSample) -> FillCandidate | None:
        self.last_sample = sample
        self.trip.update(sample)
        candidate = self.detector.update(sample, self.last_fix)
        if self.model is not None:
            self.model.update(sample)
            self.saver.save(self.model.snapshot(), sample.ts)
        # Prompting (#11). The tank is NOT changed while a fill is pending (Decision #34).
        if candidate is not None:
            self._outbox.extend(self.prompter.add(candidate, sample.ts))
        self._outbox.extend(self.prompter.on_sample(sample))
        for skipped in self.prompter.drain_skipped():
            self._points.append(fill_up_point(_skipped_fill(skipped), None))
        return candidate

    def handle_fix(self, fix: GpsFix) -> Point | None:
        """Every GPS fix; returns a track point when one is due (#14)."""
        self.last_fix = fix
        if not self.track.keep(fix):
            return None
        mpg = self.model.mpg_instant_30s if self.model is not None else None
        return track_point(fix, mpg_30s=mpg, trip_id=self.trip.trip_id)

    def handle_manual_full(self, now: datetime) -> None:
        """'Filled to full' from HA: seed the tank on a fresh install, else a full fill."""
        if self.model is None:
            state = FuelState(
                ts=now,
                gal_after_last_fill=self.config.capacity_gal,
                engine_used_gal=0.0,
                heater_used_gal=0.0,
                last_fill_type=FillType.FULL,
            )
            self.model = FuelModel(self.model_config, state)
        else:
            self.model.apply_fill(
                FillUp(
                    fill_id=str(uuid.uuid4()),
                    ts=now,
                    source=FillSource.MANUAL,
                    fill_type=FillType.FULL,
                    status=FillStatus.CONFIRMED,
                    gallons=None,
                )
            )
        logger.info("filled to full: tank {:.1f} gal", self.model.tank_gal)
        self.saver.save(self.model.snapshot(), now, force=True)

    def drain_outbox(self) -> list[tuple[str, str]]:
        out = list(self._outbox)
        self._outbox.clear()
        return out

    def drain_points(self) -> list[Point]:
        out = list(self._points)
        self._points.clear()
        return out

    def _reply(self, ok: bool, message: str, fill_id: str | None = None) -> None:
        logger.info("fill-up {}: {}", "ok" if ok else "rejected", message)
        self._outbox.append(
            (FILL_RESULT_TOPIC, json.dumps({"ok": ok, "message": message, "fill_id": fill_id}))
        )

    def handle_fill_response(self, payload: bytes, now: datetime) -> None:
        """Logged fill-up from HA (form or notification reply), #12."""
        try:
            parsed = parse_response(payload.decode(errors="replace"))
        except ValueError as e:
            self._reply(False, f"Not logged: {e}")
            return
        if isinstance(parsed, FillDecision):
            self._handle_decision(parsed, now)
            return
        inp = parsed
        candidate = self.prompter.resolve(inp.fill_id)
        if self.model is not None and inp.fill_id == self.model.state.last_fill_id:
            self._reply(False, "Already logged", inp.fill_id)
            return
        last = self._last_input
        if (
            last is not None
            and now - last[1] < DUPLICATE_WINDOW
            and (last[0].kind, last[0].gallons, last[0].price_per_gal)
            == (inp.kind, inp.gallons, inp.price_per_gal)
        ):
            self._reply(False, "Ignored duplicate of the fill just logged", inp.fill_id)
            return
        if self.model is None:
            if inp.kind is not FillType.FULL:
                self._reply(False, "Not logged: start the tank with a full fill first")
                return
            # Fresh install: a full fill starts the tank. No interval to measure yet.
            self.handle_manual_full(now)
            self._last_input = (inp, now)
            self._reply(True, f"Tank started full ({inp.gallons:.1f} gal pumped)", inp.fill_id)
            return
        fill = FillUp(
            fill_id=inp.fill_id,
            # A reply to a detection prompt is applied as of the detection.
            ts=candidate.ts if candidate else now,
            source=FillSource.LIVE if candidate else FillSource.MANUAL,
            fill_type=inp.kind,
            status=FillStatus.CONFIRMED,
            gallons=inp.gallons,
            price_per_gal=inp.price_per_gal,
            odometer_mi=candidate.odometer_mi if candidate else None,
            lat=candidate.lat if candidate else None,
            lon=candidate.lon if candidate else None,
        )
        self.model.apply_fill(fill)
        result = self.model.last_fill
        self.saver.save(self.model.snapshot(), now, force=True)
        self._last_input = (inp, now)
        if result is None:
            self._reply(False, "Not logged: the fuel model refused the fill", inp.fill_id)
            return
        self._points.append(fill_up_point(fill, result))
        price = f" @ ${inp.price_per_gal:.2f}" if inp.price_per_gal else ""
        mpg = (
            f" · {result.mpg_since_full:.1f} mpg since last full"
            if result.mpg_since_full is not None
            else ""
        )
        self._reply(
            True,
            f"Logged {inp.kind.value} {inp.gallons:.1f} gal{price} · tank "
            f"{result.tank_before_gal:.1f} → {result.tank_after_gal:.1f}{mpg}",
            inp.fill_id,
        )

    def _handle_decision(self, d: FillDecision, now: datetime) -> None:
        if d.action == "later":
            if self.prompter.later(d.fill_id, now):
                self._reply(True, "OK, I'll ask again at the next stop", d.fill_id)
            else:
                self._reply(False, "That fill-up prompt is no longer open", d.fill_id)
            return
        candidate = self.prompter.skip(d.fill_id)
        if candidate is None:
            self._reply(False, "That fill-up prompt is no longer open", d.fill_id)
            return
        self._points.append(fill_up_point(_skipped_fill(candidate), None))
        self._reply(True, "Marked as not a fill", d.fill_id)

    def handle_command(self, topic: str, payload: bytes, now: datetime) -> bool:
        """MQTT commands from HA. True if acted on."""
        if topic == MANUAL_FILL_TOPIC and payload.strip() == b"full":
            self.handle_manual_full(now)
            return True
        if topic == FILL_RESPONSE_TOPIC:
            self.handle_fill_response(payload, now)
            return True
        logger.warning("ignoring command {} {!r}", topic, payload[:40])
        return False

    def flush(self, now: datetime) -> None:
        """Force a state save (shutdown)."""
        if self.model is not None:
            self.saver.save(self.model.snapshot(), now, force=True)

    def is_stale(self, now: datetime) -> bool:
        if self.model is not None and self.model.state.stale:
            return True
        s = self.last_sample
        moving = s is not None and s.ignition and (s.speed_mph or 0.0) > 1.0
        return moving and s is not None and now - s.ts > SILENT_WHILE_MOVING

    def state_payload(self, now: datetime) -> str:
        m = self.model
        return fuel_state_payload(
            m.state.ts if m else now,
            m.tank_gal if m else None,
            m.range_mi if m else None,
            m.mpg_rolling if m else None,
            m.mpg_instant_30s if m else None,
            m.mpg_since_full if m else None,
            self.is_stale(now),
            calibrating=not self.config.fuel_rate_verified,
            fill_pending=bool(self.prompter.pending_ids),
        )

    def gauge_feed(self, now: datetime) -> dict[str, object]:
        """What the dash display needs (#17). `ts` is when the service wrote it, so an
        old feed means the service is down; parked with the engine off stays live."""
        m = self.model
        return {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tank_gal": None if m is None else round(m.tank_gal, 2),
            "range_mi": None if m is None else round(m.range_mi, 1),
            "capacity_gal": self.config.capacity_gal,
            "stale": self.is_stale(now),
            "calibrating": not self.config.fuel_rate_verified,
            "mpg_spark": [round(v, 2) for v in self.trip.buckets],
            "van_battery_v": self._fresh_battery_v(now),
            # Rolling mpg only once measured: below 50 mi it's the prior, not data.
            "mpg_rolling": (
                round(m.mpg_rolling, 2) if m is not None and m.mpg_rolling_measured else None
            ),
            "mpg_instant_30s": _r(m.mpg_instant_30s if m else None),
            "mpg_since_full": _r(m.mpg_since_full if m else None),
            "trip_mpg": _r(self.trip.trip_mpg),
            "fill_pending": bool(self.prompter.pending_ids),
            # Trip and Van dash screens (#62); the renderer judges freshness from the times.
            "trip_mi": round(self.trip.trip_miles, 2),
            "trip_gal": round(self.trip.trip_gal, 3),
            "trip_start": _iso(self.trip.trip_start),
            "obd_ts": _iso(s.ts) if (s := self.last_sample) else None,
            "ignition": s.ignition if s else None,
            "gps_ts": _iso(f.ts) if (f := self.last_fix) else None,
            "gps_hdop": f.hdop if f else None,
        }

    def _fresh_battery_v(self, now: datetime) -> float | None:
        s = self.last_sample
        if s is None or s.battery_v is None or now - s.ts > VOLTAGE_MAX_AGE:
            return None
        return s.battery_v

    def influx_points(self, now: datetime) -> list[Point]:
        points: list[Point] = []
        s = self.last_sample
        m = self.model
        if m is not None:
            points.append(
                fuel_point(
                    now,
                    self.config.fuel_method,
                    m.tank_gal,
                    m.state.engine_used_gal,
                    m.state.heater_used_gal,
                    m.range_mi,
                    m.mpg_rolling,
                    m.mpg_instant_30s,
                    s.fuel_rate_gph if s else None,
                )
            )
        if s is not None and (s.ignition or s.battery_v is not None):
            points.append(vehicle_point(s))
        return points
