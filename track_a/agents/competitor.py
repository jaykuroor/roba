"""Track A competitor intelligence agent."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core import config
from core.agent_base import BaseAgent
from core.models import Call, Competitor, CompetitorIntel, CompetitorOffer, MenuItem, Signal
from core.signals import SignalType


class CompetitorAgent(BaseAgent):
    """Passive competitor sensing and approval-gated research calls."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        calls: Optional[Any] = None,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        super().__init__(bus, db_session_factory, "track_a.competitor")
        self.calls = calls
        self.ws_broadcast = ws_broadcast
        self.subscribe(["sensing", "forecasting"])

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            self.passive_monitor,
            interval_sim_s=10800.0,
            name="track_a_competitor_monitor",
        )

    def on_signal(self, signal: Signal) -> None:
        if signal.type == SignalType.CALL_OUTCOME.value:
            self.handle_call_outcome(signal)

    def passive_monitor(self) -> List[Dict[str, Any]]:
        updates: List[Dict[str, Any]] = []
        after_commit: List[tuple[str, Any]] = []
        session = self.db_session_factory()
        try:
            competitors = session.query(Competitor).order_by(Competitor.id.asc()).all()
            for competitor in competitors:
                offers = (
                    session.query(CompetitorOffer)
                    .filter(CompetitorOffer.competitor_id == competitor.id)
                    .all()
                )
                summary = ", ".join(o.dish_or_combo for o in offers[:3]) or "No tracked offers"
                payload = {
                    "competitor_id": competitor.id,
                    "is_open": bool(competitor.is_open),
                    "offers_changed": False,
                    "summary": summary,
                }
                after_commit.append(
                    (
                        "emit",
                        (
                            SignalType.COMPETITOR_UPDATE,
                            payload,
                            {"dedup_key": f"competitor:{competitor.id}"},
                        ),
                    )
                )
                updates.append(payload)
        finally:
            session.close()
        self._run_after_commit(after_commit)
        self._broadcast("competitor_update", {"updates": updates})
        return updates

    def discover_targets(self) -> List[Dict[str, Any]]:
        session = self.db_session_factory()
        try:
            candidates = []
            for competitor in session.query(Competitor).all():
                cuisines = [str(c).lower() for c in (competitor.cuisine or [])]
                if competitor.distance_km is None or float(competitor.distance_km) > config.COMPETITOR_RADIUS_KM:
                    continue
                if "italian" not in cuisines and cuisines:
                    continue
                proximity = 1.0 / max(float(competitor.distance_km or 0.1), 0.1)
                candidates.append((float(competitor.rating or 0.0) * proximity, competitor))
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            return [self._competitor_to_dict(c) for _score, c in candidates[: config.COMPETITOR_CALL_TARGETS]]
        finally:
            session.close()

    def request_research(self, competitor_id: int) -> Dict[str, Any]:
        if self.calls is None:
            raise RuntimeError("Call subsystem is not wired")
        call = self.calls.request(
            agent="competitor_intel",
            counterparty_type="competitor",
            counterparty_id=competitor_id,
            purpose="ask favourite dish",
        )
        self.log_event(
            "competitor",
            f"Requested undercover customer call for competitor #{competitor_id}",
            {"call_id": call.id, "competitor_id": competitor_id},
        )
        return {"call_id": call.id, "status": call.status, "approval_id": call.approval_id}

    def handle_call_outcome(self, signal: Signal) -> Optional[CompetitorIntel]:
        payload = signal.payload or {}
        if payload.get("counterparty_type") != "competitor":
            return None
        call_id = payload.get("call_id")
        outcome = payload.get("outcome") or {}

        session = self.db_session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None or call.agent != "competitor_intel":
                return None
            popular = list(outcome.get("popular_dishes") or [])
            price_points = dict(outcome.get("price_points") or {})
            if not popular:
                fallback_offer = (
                    session.query(CompetitorOffer)
                    .filter(CompetitorOffer.competitor_id == call.counterparty_id)
                    .order_by(CompetitorOffer.id.asc())
                    .first()
                )
                if fallback_offer is not None:
                    popular = [fallback_offer.dish_or_combo]
                    price_points = {fallback_offer.dish_or_combo: fallback_offer.price}
            if not popular:
                return None
            row = CompetitorIntel(
                competitor_id=call.counterparty_id,
                method="call",
                popular_dishes=popular,
                price_points=price_points,
                notes="approval-gated customer-style research call",
                call_id=call.id,
                sim_time=float(self.bus.sim_time),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
        finally:
            session.close()

        self.emit(
            SignalType.COMPETITOR_INTEL,
            {
                "competitor_id": row.competitor_id,
                "popular_dishes": row.popular_dishes or [],
                "price_points": row.price_points or {},
                "method": "call",
                "call_id": row.call_id,
            },
            dedup_key=f"competitor-intel:{row.competitor_id}:{row.call_id}",
        )
        self.log_event(
            "competitor",
            f"Competitor #{row.competitor_id} favourite dishes: {', '.join(row.popular_dishes or [])}",
            self._intel_to_dict(row),
        )
        self._broadcast("competitor_intel", {"intel": self._intel_to_dict(row)})
        return row

    def map_popular_to_menu_item(self, popular_dish: str) -> Optional[int]:
        needle = popular_dish.lower()
        session = self.db_session_factory()
        try:
            for item in session.query(MenuItem).all():
                name = (item.name or "").lower()
                if needle in name or name in needle:
                    return item.id
        finally:
            session.close()
        return None

    @staticmethod
    def _competitor_to_dict(row: Competitor) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _intel_to_dict(row: CompetitorIntel) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}
