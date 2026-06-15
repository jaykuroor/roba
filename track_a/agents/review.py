"""Track A review analysis agent."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from core.agent_base import BaseAgent
from core.models import Review, ReviewInsight, Signal
from core.signals import SignalType


class ReviewAgent(BaseAgent):
    """Turns review rows into Track A insights and REVIEW_INSIGHT signals."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        llm: Optional[Any] = None,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        super().__init__(bus, db_session_factory, "track_a.review")
        self.llm = llm
        self.ws_broadcast = ws_broadcast
        self.subscribe(["sensing"])

    def register(self, orchestrator: Any) -> None:
        orchestrator.register(
            "interval",
            self.process_unprocessed,
            interval_sim_s=900.0,
            name="track_a_review_scan",
        )

    def on_signal(self, signal: Signal) -> None:
        if signal.type == SignalType.USER_FACT.value and (signal.payload or {}).get("intent") == "add_review":
            self.process_unprocessed()

    def process_unprocessed(self) -> List[ReviewInsight]:
        rows: List[ReviewInsight] = []
        now = float(self.bus.sim_time)
        session = self.db_session_factory()
        try:
            reviews = (
                session.query(
                    Review.id,
                    Review.source,
                    Review.rating,
                    Review.text,
                    Review.dish_mentions,
                    Review.sentiment,
                    Review.sim_time,
                )
                .filter(Review.processed == 0)
                .order_by(Review.sim_time.asc(), Review.id.asc())
                .all()
            )
            for review in reviews:
                review_id, source, rating, text, dish_mentions, _sentiment, sim_time = review
                review_data = {
                    "id": review_id,
                    "source": source,
                    "rating": rating,
                    "text": text,
                    "dish_mentions": dish_mentions or [],
                    "sim_time": sim_time,
                }
                parsed = self._analyze(review_data)
                severity = self._trend_severity(session, parsed["dish_mentions"], parsed["severity"])
                insight = ReviewInsight(
                    review_id=review_id,
                    insight_type="sentiment",
                    summary=parsed["summary"],
                    suggested_action=parsed["suggested_action"],
                    severity=severity,
                    sim_time=now,
                )
                session.add(insight)
                review_obj = session.get(Review, review_id)
                if review_obj is not None:
                    review_obj.processed = 1
                    review_obj.sentiment = parsed["sentiment"]
                    review_obj.dish_mentions = parsed["dish_mentions"]
                session.flush()
                session.refresh(insight)
                insight_payload = {
                    "id": insight.id,
                    "review_id": review_id,
                    "insight_type": "sentiment",
                    "summary": parsed["summary"],
                    "suggested_action": parsed["suggested_action"],
                    "severity": severity,
                    "sim_time": now,
                }
                review_payload = {
                    "id": review_id,
                    "source": source,
                    "rating": rating,
                    "text": text,
                    "dish_mentions": parsed["dish_mentions"],
                    "sentiment": parsed["sentiment"],
                    "sim_time": sim_time,
                    "processed": 1,
                }
                rows.append(insight)

                payload = {
                    "review_id": review_id,
                    "severity": severity,
                    "summary": parsed["summary"],
                    "suggested_action": parsed["suggested_action"],
                    "dish_mentions": parsed["dish_mentions"],
                }
                key = "review:" + (parsed["dish_mentions"][0] if parsed["dish_mentions"] else "general")
                self.emit(SignalType.REVIEW_INSIGHT, payload, dedup_key=key)
                self.log_event("review", parsed["summary"], payload)
                self._broadcast(
                    "review_insight",
                    {"insight": insight_payload, "review": review_payload},
                )
            session.commit()
        finally:
            session.close()
        return rows

    @staticmethod
    def _analyze(review: Dict[str, Any]) -> Dict[str, Any]:
        text = str(review.get("text") or "").lower()
        mentions = list(review.get("dish_mentions") or [])
        rating = float(review.get("rating") or 0.0)
        negative_words = {"cold", "soggy", "slow", "bland", "small", "bad", "waited", "awful"}
        positive_words = {"best", "great", "lovely", "fresh", "tasty", "return", "authentic", "addictive"}
        if rating <= 2 or any(word in text for word in negative_words):
            sentiment = "negative"
            severity = "high" if rating <= 2 else "medium"
            summary = f"Negative review: {review.get('text')}"
            action = "Inspect recipe, prep timing, and station execution for mentioned dishes."
        elif rating >= 4 or any(word in text for word in positive_words):
            sentiment = "positive"
            severity = "low"
            summary = f"Positive review: {review.get('text')}"
            action = "Consider featuring mentioned dishes in demand planning."
        else:
            sentiment = "neutral"
            severity = "low"
            summary = f"Neutral review: {review.get('text')}"
            action = "Monitor for repeated theme before changing demand assumptions."
        return {
            "sentiment": sentiment,
            "severity": severity,
            "summary": summary,
            "suggested_action": action,
            "dish_mentions": mentions,
        }

    @staticmethod
    def _trend_severity(session: Any, mentions: List[str], default: str) -> str:
        if not mentions:
            return default
        counts: Counter[str] = Counter()
        reviews = session.query(Review.dish_mentions).filter(Review.sentiment == "negative").all()
        for (dish_mentions,) in reviews:
            for mention in dish_mentions or []:
                counts[str(mention).lower()] += 1
        if any(counts[m.lower()] >= 3 for m in mentions):
            return "high"
        return default

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast(event, payload)

    @staticmethod
    def _insight_to_dict(row: ReviewInsight) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _review_to_dict(row: Review) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}
