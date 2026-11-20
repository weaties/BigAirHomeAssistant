"""Fill-up prompt state machine (#11) — Critical tier.

A detected fill becomes a pending prompt on the phone (via HA). Re-prompt
rules from /fill-up-events: when a prompt has gone unanswered for 10 min, or
Dan tapped "Later", prompt again at the next park or ignition on, up to 3
prompts in all. After that the fill is skipped and HA leaves a persistent
notification. Pure: samples and decisions in, MQTT messages out.

Pending prompts live in memory: after a restart, a reply to an old prompt is
logged as a manual fill at the time of the reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from bigair.signals import FillCandidate, ObdSample

CANDIDATE_TOPIC = "bigair/fill_up/candidate"  # HA turns each message into a phone prompt
EXPIRED_TOPIC = "bigair/fill_up/expired"  # HA leaves a persistent notification
PARK_AFTER = timedelta(seconds=60)
IGNITION_GAP = timedelta(seconds=60)  # no samples this long, then samples = engine on again


@dataclass
class _Pending:
    candidate: FillCandidate
    prompts: int = 0
    last_prompt: datetime | None = None
    later: bool = False


@dataclass
class _Motion:
    last: ObdSample | None = None
    stopped_since: datetime | None = None
    moved_since_park: bool = False
    parked: bool = False


def candidate_message(c: FillCandidate, attempt: int) -> tuple[str, str]:
    return (
        CANDIDATE_TOPIC,
        json.dumps(
            {
                "fill_id": c.fill_id,
                "ts": c.ts.isoformat(),
                "est_gallons": round(c.est_gallons, 1),
                "attempt": attempt,
                "odometer_mi": c.odometer_mi,
                "lat": c.lat,
                "lon": c.lon,
            }
        ),
    )


@dataclass
class FillPrompter:
    max_prompts: int = 3
    answer_within: timedelta = timedelta(minutes=10)
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _motion: _Motion = field(default_factory=_Motion)
    _skipped: list[FillCandidate] = field(default_factory=list)

    @property
    def pending_ids(self) -> set[str]:
        return set(self._pending)

    def _prompt(self, p: _Pending, now: datetime) -> tuple[str, str]:
        p.prompts += 1
        p.last_prompt = now
        p.later = False
        return candidate_message(p.candidate, p.prompts)

    def add(self, candidate: FillCandidate, now: datetime) -> list[tuple[str, str]]:
        p = _Pending(candidate)
        self._pending[candidate.fill_id] = p
        return [self._prompt(p, now)]

    def resolve(self, fill_id: str) -> FillCandidate | None:
        """Dan answered Full/Partial: stop prompting, hand back the candidate."""
        p = self._pending.pop(fill_id, None)
        return None if p is None else p.candidate

    def skip(self, fill_id: str) -> FillCandidate | None:
        """Dan said "Not a fill"."""
        return self.resolve(fill_id)

    def later(self, fill_id: str, now: datetime) -> bool:
        p = self._pending.get(fill_id)
        if p is None:
            return False
        p.later = True
        return True

    def drain_skipped(self) -> list[FillCandidate]:
        out, self._skipped = self._skipped, []
        return out

    def _event(self, now: datetime) -> list[tuple[str, str]]:
        """A park or ignition on: re-prompt or expire whatever is due."""
        out: list[tuple[str, str]] = []
        for fill_id, p in list(self._pending.items()):
            due = p.later or p.last_prompt is None or now - p.last_prompt >= self.answer_within
            if not due:
                continue
            if p.prompts < self.max_prompts:
                out.append(self._prompt(p, now))
            else:
                del self._pending[fill_id]
                self._skipped.append(p.candidate)
                out.append(
                    (
                        EXPIRED_TOPIC,
                        json.dumps(
                            {
                                "fill_id": fill_id,
                                "ts": p.candidate.ts.isoformat(),
                                "est_gallons": round(p.candidate.est_gallons, 1),
                            }
                        ),
                    )
                )
        return out

    def on_sample(self, sample: ObdSample) -> list[tuple[str, str]]:
        m = self._motion
        last, m.last = m.last, sample
        event = False
        if sample.ignition and (
            last is None or not last.ignition or sample.ts - last.ts > IGNITION_GAP
        ):
            event = last is not None  # engine on again (not the very first sample)
            m.parked = False
            m.stopped_since = None
        elif not sample.ignition:
            if last is not None and last.ignition and not m.parked:
                event = True  # engine off = parked
            m.parked = True
            m.stopped_since = None
        else:
            speed = sample.speed_mph
            if speed is not None and speed > 1.0:
                m.moved_since_park = True
                m.stopped_since = None
                m.parked = False
            elif speed is not None:
                if m.stopped_since is None:
                    m.stopped_since = sample.ts
                if (
                    not m.parked
                    and m.moved_since_park
                    and sample.ts - m.stopped_since >= PARK_AFTER
                ):
                    event = True
                    m.parked = True
                    m.moved_since_park = False
        return self._event(sample.ts) if event and self._pending else []
