"""Track A review analysis agent."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from core.agent_base import BaseAgent
from core.llm import CANNED_NOTE
from core.models import Review, ReviewInsight, Signal
from core.signals import SignalType


REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string"},
        "summary": {"type": "string"},
        "suggested_action": {"type": "string"},
        "dish_mentions": {"type": "array"},
        "sentiment": {"type": "string"},
    },
    "required": [
        "severity",
        "summary",
        "suggested_action",
        "dish_mentions",
        "sentiment",
    ],
}


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
        after_commit: List[tuple[str, Any]] = []
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
                after_commit.extend(
                    [
                        ("emit", (SignalType.REVIEW_INSIGHT, payload, {"dedup_key": key})),
                        ("log", ("review", parsed["summary"], payload)),
                        (
                            "broadcast",
                            ("review_insight", {"insight": insight_payload, "review": review_payload}),
                        ),
                    ]
                )
            session.commit()
        finally:
            session.close()
        self._run_after_commit(after_commit)
        return rows

    def _analyze(self, review: Dict[str, Any]) -> Dict[str, Any]:
        if self.llm is not None:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Analyze one restaurant review. Return JSON with "
                        "severity low|medium|high, summary, suggested_action, "
                        "dish_mentions, and sentiment positive|neutral|negative."
                    ),
                },
                {"role": "user", "content": str(review)},
            ]
            result = self.llm.complete(
                messages,
                json_schema=REVIEW_SCHEMA,
                max_tokens=400,
                use_site="review",
            )
            if isinstance(result, dict) and result.get("note") != CANNED_NOTE:
                return self._normalise_llm_analysis(result, review)
        return self._deterministic_analysis(review)

    @staticmethod
    def _normalise_llm_analysis(
        result: Dict[str, Any], review: Dict[str, Any]
    ) -> Dict[str, Any]:
        fallback = ReviewAgent._deterministic_analysis(review)
        sentiment = str(result.get("sentiment") or fallback["sentiment"]).lower()
        if sentiment not in {"positive", "neutral", "negative"}:
            sentiment = fallback["sentiment"]
        severity = str(result.get("severity") or fallback["severity"]).lower()
        if severity not in {"low", "medium", "high"}:
            severity = fallback["severity"]
        mentions = result.get("dish_mentions")
        if not isinstance(mentions, list):
            mentions = fallback["dish_mentions"]
        return {
            "sentiment": sentiment,
            "severity": severity,
            "summary": str(result.get("summary") or fallback["summary"]),
            "suggested_action": str(
                result.get("suggested_action") or fallback["suggested_action"]
            ),
            "dish_mentions": [str(m) for m in mentions],
        }

    @staticmethod
    def _deterministic_analysis(review: Dict[str, Any]) -> Dict[str, Any]:
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

    @staticmethod
    def _insight_to_dict(row: ReviewInsight) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}

    @staticmethod
    def _review_to_dict(row: Review) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}
