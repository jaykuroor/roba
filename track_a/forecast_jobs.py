"""Background forecast job runner for Track A.

The runner keeps the simulation clock out of forecast sequencing:
deterministic forecasts are coalesced and short, while LLM finalizer work runs
outside the tick loop and turns durable changes into approval requests.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from core import db
from core.models import ForecastJob, Signal
from core.signals import SignalType
from track_a.agents.forecaster import current_window, forecast_window_matches

logger = logging.getLogger(__name__)

DETERMINISTIC_FORECAST = "deterministic_forecast"
LLM_AUTHORITY_FORECAST = "llm_authority_forecast"
LLM_FINALIZER = "llm_finalizer"
ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"succeeded", "failed", "superseded", "stale"}


class ForecastJobRunner:
    """In-process queue for deterministic and LLM forecast jobs."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        forecaster: Any,
        approvals: Any,
        ws_broadcast: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> None:
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.forecaster = forecaster
        self.approvals = approvals
        self.ws_broadcast = ws_broadcast
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._recover_interrupted_jobs()
        self._thread = threading.Thread(
            target=self._worker,
            name="forecast-job-runner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def set_ws_broadcast(self, fn: Callable[[str, Dict[str, Any]], Any]) -> None:
        self.ws_broadcast = fn

    # -- enqueue --------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        trigger_reason: str = "manual",
        requested_by: str = "system",
    ) -> Dict[str, Any]:
        if kind not in {DETERMINISTIC_FORECAST, LLM_AUTHORITY_FORECAST, LLM_FINALIZER}:
            raise ValueError(f"Unknown forecast job kind {kind!r}")

        now = float(self.bus.sim_time)
        daypart, window = current_window(now)
        with db.DB_LOCK:
            session = self.db_session_factory()
            try:
                existing = self._active_job_for_window(session, kind, daypart, window)
                if existing is not None:
                    data = self.to_dict(existing)
                    session.expunge(existing)
                    return data

                row = ForecastJob(
                    job_id=str(uuid.uuid4()),
                    kind=kind,
                    status="queued",
                    sim_time=now,
                    daypart=daypart,
                    window=dict(window),
                    requested_by=requested_by,
                    trigger_reason=trigger_reason,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    error=None,
                    result={},
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                data = self.to_dict(row)
                session.expunge(row)
            finally:
                session.close()

        self._queue.put(data["job_id"])
        self._broadcast(data)
        return data

    def on_approval_resolved(self, signal: Signal) -> None:
        payload = dict(signal.payload or {})
        if payload.get("type") != "forecast_override_proposal":
            return
        proposal = dict(payload.get("payload") or {})
        if payload.get("decision") != "approved":
            return

        with db.DB_LOCK:
            self.forecaster.persist_approved_llm_override(proposal)
        self.enqueue(
            DETERMINISTIC_FORECAST,
            trigger_reason="approval:forecast_override_proposal",
            requested_by="approval",
        )

    # -- worker ---------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                continue
            try:
                self._run_job(job_id)
            except Exception:  # noqa: BLE001 - keep the worker alive.
                logger.exception("Forecast job %s failed outside status handling", job_id)
                self._finish(job_id, "failed", error="Unhandled forecast job error")

    def _run_job(self, job_id: str) -> None:
        job = self._start(job_id)
        if job is None:
            return
        if not self._window_is_current(job["window"]):
            self._finish(job_id, "stale", result={"reason": "window_changed_before_start"})
            return

        # While a forecast/agent job runs, the orchestrator syncs the sim
        # clock to realtime (1 sim-min = 1 real-min).
        hold = f"forecast:{job_id}"
        labels = {
            DETERMINISTIC_FORECAST: "Demand forecast",
            LLM_AUTHORITY_FORECAST: "Demand forecast (LLM)",
            LLM_FINALIZER: "Forecast review (LLM)",
        }
        self.bus.hold_realtime(hold, labels.get(job["kind"], "Forecast job"))
        try:
            if job["kind"] == DETERMINISTIC_FORECAST:
                self._run_deterministic(job)
            elif job["kind"] == LLM_AUTHORITY_FORECAST:
                self._run_llm_authority(job)
            elif job["kind"] == LLM_FINALIZER:
                self._run_llm_finalizer(job)
        finally:
            self.bus.release_realtime(hold)

    def _run_deterministic(self, job: Dict[str, Any]) -> None:
        try:
            # Do not hold DB_LOCK while forecasting. run_forecast may invoke the
            # LLM when material context is live, and the simulation tick loop
            # needs the same lock to keep the clock moving.
            rows = self.forecaster.run_forecast(job.get("trigger_reason") or "job")
            self._finish(
                job["job_id"],
                "succeeded",
                result={"created": len(rows), "optimized": False},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Deterministic forecast job failed")
            self._finish(job["job_id"], "failed", error=str(exc))

    def _run_llm_authority(self, job: Dict[str, Any]) -> None:
        try:
            # LLM-authoritative runs are intentionally outside the global
            # coordinator lock. Job status transitions stay locked via
            # _start/_finish, but slow model calls must not pause the sim clock.
            rows = self.forecaster.run_forecast(
                job.get("trigger_reason") or "job",
                optimize=True,
                optimize_batches=False,
            )
            self._finish(
                job["job_id"],
                "succeeded",
                result={"created": len(rows), "optimized": True, "authority": "llm"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM authority forecast job failed")
            self._finish(job["job_id"], "failed", error=str(exc))

    def _run_llm_finalizer(self, job: Dict[str, Any]) -> None:
        try:
            result = self.forecaster.propose_llm_forecast_overrides(job["job_id"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM finalizer job failed")
            self._finish(job["job_id"], "failed", error=str(exc))
            return

        if not self._window_is_current(job["window"]):
            self._finish(
                job["job_id"],
                "stale",
                result={**result, "reason": "window_changed_after_llm"},
            )
            return

        approval_ids: List[int] = []
        with db.DB_LOCK:
            for proposal in result.get("proposals") or []:
                row = self.approvals.create(
                    type="forecast_override_proposal",
                    title=f"Apply forecast override for {proposal.get('item_name')}",
                    summary=str(proposal.get("reason") or "LLM proposed a forecast override."),
                    payload=proposal,
                    urgency="normal",
                )
                approval_ids.append(int(row.id))

        final_result = {
            **result,
            "approval_ids": approval_ids,
            "needs_approval": bool(approval_ids),
        }
        self._finish(job["job_id"], "succeeded", result=final_result)

    # -- persistence ----------------------------------------------------

    def _start(self, job_id: str) -> Optional[Dict[str, Any]]:
        with db.DB_LOCK:
            session = self.db_session_factory()
            try:
                row = self._job_by_id(session, job_id)
                if row is None or row.status not in ACTIVE_STATUSES:
                    return None
                row.status = "running"
                row.started_at = float(self.bus.sim_time)
                session.commit()
                session.refresh(row)
                data = self.to_dict(row)
                session.expunge(row)
            finally:
                session.close()
        self._broadcast(data)
        return data

    def _finish(
        self,
        job_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal forecast job status {status!r}")
        with db.DB_LOCK:
            session = self.db_session_factory()
            try:
                row = self._job_by_id(session, job_id)
                if row is None:
                    return None
                row.status = status
                row.finished_at = float(self.bus.sim_time)
                row.error = error
                row.result = result or {}
                session.commit()
                session.refresh(row)
                data = self.to_dict(row)
                session.expunge(row)
            finally:
                session.close()
        self._broadcast(data)
        return data

    def _recover_interrupted_jobs(self) -> None:
        with db.DB_LOCK:
            session = self.db_session_factory()
            try:
                rows = (
                    session.query(ForecastJob)
                    .filter(ForecastJob.status.in_(tuple(ACTIVE_STATUSES)))
                    .all()
                )
                for row in rows:
                    row.status = "failed"
                    row.finished_at = float(self.bus.sim_time)
                    row.error = "Forecast job runner restarted before completion."
                session.commit()
            finally:
                session.close()

    @staticmethod
    def _job_by_id(session: Any, job_id: str) -> Optional[ForecastJob]:
        return (
            session.query(ForecastJob)
            .filter(ForecastJob.job_id == job_id)
            .one_or_none()
        )

    @staticmethod
    def _active_job_for_window(
        session: Any,
        kind: str,
        daypart: str,
        window: Dict[str, float],
    ) -> Optional[ForecastJob]:
        rows = (
            session.query(ForecastJob)
            .filter(
                ForecastJob.kind == kind,
                ForecastJob.status.in_(tuple(ACTIVE_STATUSES)),
                ForecastJob.daypart == daypart,
            )
            .order_by(ForecastJob.created_at.desc(), ForecastJob.id.desc())
            .all()
        )
        for row in rows:
            if forecast_window_matches(row.window or {}, window):
                return row
        return None

    def _window_is_current(self, window: Dict[str, Any]) -> bool:
        now = float(self.bus.sim_time)
        try:
            return float(window["start"]) <= now < float(window["end"])
        except (KeyError, TypeError, ValueError):
            return False

    def _broadcast(self, job: Dict[str, Any]) -> None:
        if self.ws_broadcast is not None:
            self.ws_broadcast("forecast_job_updated", {"job": job})

    @staticmethod
    def to_dict(row: ForecastJob) -> Dict[str, Any]:
        return {col.key: getattr(row, col.key) for col in row.__table__.columns}
