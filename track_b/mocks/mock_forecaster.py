"""MockForecaster — Track B's standalone demand placeholder (02 §B5).

Active only when ``DEMO_MODE=track_b``. It is the **single most important
placeholder** in the track: every later milestone (depletion → reorder → toggle
→ expiry → promo) is validated against the demand it emits, so the numbers are
derived from real seed history rather than hand-waved.

Behaviour (§B5, numbers per §18.1):
  - Every ``FORECAST_INTERVAL_SIM_S``, for each **active** menu item, emit a
    ``DEMAND_FORECAST`` for the current daypart with
    ``qty = baseline(item, daypart, dow)`` (multipliers empty, ``confidence
    0.8``). ``baseline`` is the mean total quantity that item sold in that
    ``(daypart, day-of-week)`` across the seeded ``order_lines`` history, with
    the §18.1 fallbacks: ``(item, daypart)`` mean → item mean → a small default.
  - At each ``batch_definition``'s ``decide_by`` (per applicable daypart, once
    per sim-day), emit ``BATCH_DECISION(cook, qty=forecast)`` sized into the
    batch's ``[min, max]`` step grid.

It is removed automatically in ``combined`` (real Track A signals arrive) — the
registration in ``track_b/agents/__init__.py`` only constructs it for
``DEMO_MODE=track_b``, so no code changes are needed to merge.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from core import config
from core.clock import SECONDS_PER_DAY
from core.models import BatchDefinition, MenuItem, OrderLine

logger = logging.getLogger(__name__)

# Final fallback when there is no seeded history at all for an item — keeps
# demand signals flowing so the rest of the track still gets exercised.
DEFAULT_FORECAST_QTY = 5.0
CONFIDENCE = 0.8


def _hhmm_to_secs(hhmm: str) -> int:
    """``"08:00"`` → seconds-into-day (28800)."""
    hh, mm = hhmm.split(":")
    return int(hh) * 3600 + int(mm) * 60


# Daypart windows as (name, start_secs, end_secs), ordered by start (§22).
_DAYPARTS: List[Tuple[str, int, int]] = [
    (name, _hhmm_to_secs(start), _hhmm_to_secs(end))
    for name, (start, end, _w) in config.DAYPARTS.items()
]


def _seconds_into_day(sim_time: float) -> int:
    """Seconds-into-day for ``sim_time`` (handles negative history times)."""
    return int(sim_time % SECONDS_PER_DAY)


def _daypart_at(sim_time: float) -> Optional[Tuple[str, int, int]]:
    """The ``(name, start, end)`` daypart covering ``sim_time``; ``None`` if it
    falls in closed hours / a gap."""
    secs = _seconds_into_day(sim_time)
    for name, start, end in _DAYPARTS:
        if start <= secs < end:
            return (name, start, end)
    return None


def _day_index(sim_time: float) -> int:
    """Sim-day index (negative for the seeded pre-history)."""
    return math.floor(sim_time / SECONDS_PER_DAY)


def _round_to_step(qty: float, minimum: float, step: float, maximum: float) -> float:
    """Clamp ``qty`` into ``[minimum, maximum]`` snapped to the step grid (§18.3)."""
    q = max(minimum, min(qty, maximum))
    if step and step > 0:
        steps = round((q - minimum) / step)
        q = minimum + steps * step
        q = max(minimum, min(q, maximum))
    return q


class MockForecaster:
    """Emits ``DEMAND_FORECAST`` per item + ``BATCH_DECISION`` per batch (§B5)."""

    def __init__(self, bus: Any, db_session_factory: Any, name: str = "forecaster"):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.name = name
        # Lazily-built baseline caches keyed at three §18.1 granularities.
        self._by_item_dp_dow: Dict[Tuple[int, str, int], float] = {}
        self._by_item_dp: Dict[Tuple[int, str], float] = {}
        self._by_item: Dict[int, float] = {}
        self._baselines_ready = False
        # Guard so each batch decision fires once per (batch, daypart, sim-day).
        self._fired_batches: set[Tuple[int, str, int]] = set()

    # -- registration -------------------------------------------------------

    def register(self, orchestrator: Any) -> None:
        """Register the single forecast/batch interval trigger (§17).

        ``due_at`` is set to the current sim-time so the first qualifying tick
        after ``play`` fires immediately — demand shows up in the signal log
        right away rather than one interval later."""
        orchestrator.register(
            "interval",
            self.tick,
            interval_sim_s=config.FORECAST_INTERVAL_SIM_S,
            due_at=self.bus.sim_time,
            name="mock_forecaster",
        )

    # -- per-interval tick --------------------------------------------------

    def tick(self) -> None:
        """Emit demand for every active item, then any due batch decisions."""
        self._emit_demand_forecasts()
        self._emit_due_batch_decisions()

    # -- demand -------------------------------------------------------------

    def _emit_demand_forecasts(self) -> None:
        now = self.bus.sim_time
        daypart = _daypart_at(now)
        if daypart is None:
            # Closed hours — the sim normally won't tick here; nothing to do.
            return
        dp_name, dp_start, dp_end = daypart
        day_start = _day_index(now) * SECONDS_PER_DAY
        window = {"start": float(day_start + dp_start), "end": float(day_start + dp_end)}
        dow = _day_index(now) % 7
        ttl = max(window["end"] - now, 1.0)  # "until window end" (§15)

        session = self.db_session_factory()
        try:
            self._ensure_baselines(session)
            items = (
                session.query(MenuItem)
                .filter(MenuItem.active == 1)
                .order_by(MenuItem.id.asc())
                .all()
            )
            item_ids = [mi.id for mi in items]
        finally:
            session.close()

        for item_id in item_ids:
            baseline = self._baseline(item_id, dp_name, dow)
            qty = round(baseline)  # multipliers empty in the mock
            self.bus.emit(
                "DEMAND_FORECAST",
                {
                    "menu_item_id": item_id,
                    "window": window,
                    "daypart": dp_name,
                    "qty": float(qty),
                    "baseline": float(baseline),
                    "multipliers": {},
                    "confidence": CONFIDENCE,
                },
                source=self.name,
                ttl=ttl,
                dedup_key=f"demand:{item_id}",
            )

    # -- batch decisions ----------------------------------------------------

    def _emit_due_batch_decisions(self) -> None:
        now = self.bus.sim_time
        day_idx = _day_index(now)
        day_start = day_idx * SECONDS_PER_DAY
        dow = day_idx % 7

        session = self.db_session_factory()
        try:
            self._ensure_baselines(session)
            bdefs = session.query(BatchDefinition).order_by(BatchDefinition.id.asc()).all()
            # Detach the fields we need so we can close the session first.
            specs = [
                {
                    "id": b.id,
                    "menu_item_id": b.menu_item_id,
                    "dayparts": list(b.dayparts or []),
                    "decide_by_offset_min": b.decide_by_offset_min,
                    "prep_lead_time_min": b.prep_lead_time_min,
                    "batch_size_min": b.batch_size_min or 0.0,
                    "batch_size_step": b.batch_size_step or 0.0,
                    "batch_size_max": b.batch_size_max,
                }
                for b in bdefs
            ]
        finally:
            session.close()

        for spec in specs:
            for dp_name in spec["dayparts"]:
                dp = next((d for d in _DAYPARTS if d[0] == dp_name), None)
                if dp is None:
                    continue
                serve_start = day_start + dp[1]
                # decide_by = serve_start − decide_by_offset (explicit field),
                # falling back to the §17 prep_lead + buffer form.
                if spec["decide_by_offset_min"] is not None:
                    decide_by = serve_start - spec["decide_by_offset_min"] * 60.0
                else:
                    decide_by = (
                        serve_start
                        - (spec["prep_lead_time_min"] or 0.0) * 60.0
                        - config.BATCH_BUFFER_SIM_S
                    )

                key = (spec["id"], dp_name, day_idx)
                if key in self._fired_batches or now < decide_by:
                    continue

                baseline = self._baseline(spec["menu_item_id"], dp_name, dow)
                qty = _round_to_step(
                    round(baseline),
                    spec["batch_size_min"],
                    spec["batch_size_step"],
                    spec["batch_size_max"] if spec["batch_size_max"] is not None else baseline,
                )
                serve_window = {"start": float(serve_start), "end": float(day_start + dp[2])}
                self.bus.emit(
                    "BATCH_DECISION",
                    {
                        "batch_definition_id": spec["id"],
                        "menu_item_id": spec["menu_item_id"],
                        "serve_window": serve_window,
                        "decision": "cook",
                        "qty": float(qty),
                        "by": "agent",
                    },
                    source=self.name,
                    dedup_key=f"batch:{spec['id']}:{day_idx}:{dp_name}",
                )
                self._fired_batches.add(key)

    # -- baselines (§18.1) --------------------------------------------------

    def _ensure_baselines(self, session: Any) -> None:
        """Build the baseline caches once from the seeded ``order_lines``
        history (rows at negative sim-time, i.e. the pre-day-0 history)."""
        if self._baselines_ready:
            return

        rows = (
            session.query(OrderLine.menu_item_id, OrderLine.qty, OrderLine.sim_time)
            .filter(OrderLine.status == "sold", OrderLine.sim_time < 0)
            .all()
        )

        # Sum qty per (item, daypart, dow, day_index); then average the per-day
        # totals so the baseline is "expected demand for this daypart-occurrence".
        per_day: Dict[Tuple[int, str, int, int], float] = defaultdict(float)
        for item_id, qty, sim_time in rows:
            dp = _daypart_at(sim_time)
            if dp is None:
                continue
            di = _day_index(sim_time)
            per_day[(item_id, dp[0], di % 7, di)] += float(qty or 0.0)

        agg_dow: Dict[Tuple[int, str, int], List[float]] = defaultdict(list)
        agg_dp: Dict[Tuple[int, str], List[float]] = defaultdict(list)
        agg_item: Dict[int, List[float]] = defaultdict(list)
        for (item_id, dp_name, dow, _di), total in per_day.items():
            agg_dow[(item_id, dp_name, dow)].append(total)
            agg_dp[(item_id, dp_name)].append(total)
            agg_item[item_id].append(total)

        self._by_item_dp_dow = {k: sum(v) / len(v) for k, v in agg_dow.items()}
        self._by_item_dp = {k: sum(v) / len(v) for k, v in agg_dp.items()}
        self._by_item = {k: sum(v) / len(v) for k, v in agg_item.items()}
        self._baselines_ready = True

    def _baseline(self, item_id: int, daypart: str, dow: int) -> float:
        """Baseline demand with the §18.1 fallback chain."""
        val = self._by_item_dp_dow.get((item_id, daypart, dow))
        if val is None:
            val = self._by_item_dp.get((item_id, daypart))
        if val is None:
            val = self._by_item.get(item_id)
        if val is None:
            return DEFAULT_FORECAST_QTY
        return val
