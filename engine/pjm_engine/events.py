"""Event types and the heapq-based scheduler.

Events fire in (timestamp, priority, sequence_id) order. Lower priority numbers
fire first at the same timestamp — info events get higher numbers than gate
closures so a gate's data view reflects everything published up to that gate.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator

# Priority lanes at the same timestamp.
PRIO_GATE_CLOSING = 10
PRIO_AWARD_PUBLISHED = 20
PRIO_PRICE_PUBLISHED = 30
PRIO_SETTLEMENT = 40


@dataclass(frozen=True)
class Event:
    timestamp: datetime  # tz-aware UTC
    asset_id: str
    priority: int = PRIO_GATE_CLOSING


@dataclass(frozen=True)
class DAGateClosing(Event):
    """DA gate closes 11:00 EPT D-1; this event fires at 11:00 EPT for operating day D."""

    operating_date: date | None = None  # the day whose 24 hours are being bid
    priority: int = PRIO_GATE_CLOSING


@dataclass(frozen=True)
class RegDailyOfferGate(Event):
    """Daily Regulation offer locks at D-1 14:15 EPT.

    The post-Oct-2025 redesign clears Reg in half-hour assignment blocks, but
    the *offer* is daily (per pjm-data.md §2.1: daily Reg offer by D-1 14:15,
    hourly MW updates up to T-35 min). The strategy submits Reg
    SelfSchedules for all 48 half-hour blocks of D at this gate; the engine
    settles each block per-MTU as RT gates fire.
    """

    operating_date: date | None = None
    priority: int = PRIO_GATE_CLOSING


@dataclass(frozen=True)
class SREventCalled(Event):
    """PJM called a Synchronized Reserve event (pjm-data.md §2.2, §7.2).

    Fires at `event_start` (= the historical event_start_utc from the SR
    events feed). The runner doesn't ask the strategy for a decision —
    SR commitments were locked at prior gates; here we check delivery and
    emit shortfall + clawback rows if the asset can't deliver in full.

    Priority is `PRIO_SETTLEMENT` so any RT-gate-induced commitments at the
    same timestamp finish first; the event then sees the up-to-date
    commitment ledger.
    """

    event_end: datetime | None = None  # tz-aware UTC, exclusive
    sub_zones: str = ""  # "" = RTO-only; csv of MAD etc otherwise
    priority: int = 40  # PRIO_SETTLEMENT — fires after gates


@dataclass(frozen=True)
class MonthlyCapacityAccrual(Event):
    """RPM (capacity) accrual for one calendar month.

    Fires at month-1 00:00 UTC for each month in the backtest window. The
    runner looks up the clearing price for (Delivery Year, asset.LDA),
    computes `ucap_mw × $/MW-day × days_in_month`, and emits one
    `Capacity_RPM` revenue row covering the whole month. Strategies don't
    decide anything here — capacity was committed in a BRA 3 years prior;
    the engine assumes the asset cleared at the observed price.

    Priority `PRIO_SETTLEMENT` so any same-timestamp gates fire first; the
    capacity row lands as a flat baseline alongside whatever else accrues.
    """

    year: int = 0
    month: int = 0
    days_in_month: int = 0
    priority: int = 40  # PRIO_SETTLEMENT


@dataclass(frozen=True)
class RTGateClosing(Event):
    """RT dispatch gate for ONE 5-min MTU; fires at mtu_start − RT_GATE_LEAD.

    The strategy decides physical dispatch for a single MTU at a time. A
    non-response (Acknowledgment) defaults to honoring the DA position for the
    parent hour; an RT bid overrides DA for this MTU.

    This is the per-MTU model that matches PJM's actual RT SCED cadence
    (every 5 min).
    """

    mtu_start: datetime | None = None  # tz-aware UTC, the target MTU's start
    priority: int = PRIO_GATE_CLOSING

    @property
    def mtu_end(self) -> datetime:
        """Exclusive end of the target MTU (5 min after start)."""
        return self.mtu_start + timedelta(minutes=5)

    @property
    def parent_hour_start(self) -> datetime:
        """Top of the operating hour this MTU belongs to (used for DA award lookup)."""
        return self.mtu_start.replace(minute=0, second=0, microsecond=0)


class EventScheduler:
    """Min-heap on (timestamp, priority, sequence_id, event)."""

    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, int, Event]] = []
        self._seq = itertools.count()

    def push(self, event: Event) -> None:
        heapq.heappush(
            self._heap,
            (event.timestamp, event.priority, next(self._seq), event),
        )

    def pop(self) -> Event:
        return heapq.heappop(self._heap)[3]

    def peek_timestamp(self) -> datetime:
        """Timestamp of the next event without popping it."""
        return self._heap[0][0]

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def drain(self) -> Iterator[Event]:
        while self._heap:
            yield self.pop()
