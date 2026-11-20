"""bigair service core (#53): samples → fuel model + fill detector → outputs.

No network here: main.py wires the WiCAN poller, MQTT and InfluxDB around it.
Owns the durable state file (Decision #22): saved at most every 10 s while
samples arrive and immediately on every fill.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from loguru import logger

from bigair.fill_detect import FillDetector, FillDetectorConfig
from bigair.fuel_model import FuelModel, FuelModelConfig
from bigair.ha_discovery import MANUAL_FILL_TOPIC, fuel_state_payload
from bigair.influx_io import fuel_point, vehicle_point
from bigair.signals import FillSource, FillStatus, FillType, FillUp, FuelState
from bigair.state_file import StateFileError, StateSaver, read_state

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from bigair.influx_io import Point
    from bigair.signals import FillCandidate, ObdSample

SILENT_WHILE_MOVING = timedelta(seconds=60)  # no samples this long after moving → stale


@dataclass(frozen=True)
class ServiceConfig:
    state_path: Path
    capacity_gal: float = 47.0
    prior_mpg: float = 15.0
    save_interval_s: float = 10.0
    fuel_rate_verified: bool = False  # flip when 5E is VERIFIED (Decision #32)
    fuel_method: str = "fuel_rate"


class FuelService:
    def __init__(self, config: ServiceConfig, model: FuelModel | None) -> None:
        self.config = config
        self.model = model
        self.detector = FillDetector(FillDetectorConfig(capacity_gal=config.capacity_gal))
        self.saver = StateSaver(config.state_path, config.save_interval_s)
        self.last_sample: ObdSample | None = None

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
        candidate = self.detector.update(sample)
        if self.model is not None:
            self.model.update(sample)
            self.saver.save(self.model.snapshot(), sample.ts)
        return candidate

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

    def handle_command(self, topic: str, payload: bytes, now: datetime) -> bool:
        """MQTT commands from HA. True if acted on."""
        if topic == MANUAL_FILL_TOPIC and payload.strip() == b"full":
            self.handle_manual_full(now)
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
        )

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
        if s is not None and s.ignition:
            points.append(vehicle_point(s))
        return points
