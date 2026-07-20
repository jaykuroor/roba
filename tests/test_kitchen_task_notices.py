"""Kitchen-task notice tiering + approval kinds.

The tier walkthrough (grace → tier1 create → tier2 update → done-late flip →
not_done once) lives in core/kitchen_tasks._demo; run it against a throwaway
DB so CI exercises the full flow.
"""
import os
import subprocess
import sys
from pathlib import Path

from core.approvals import kind_for


def test_kind_for_notice_vs_decision():
    assert kind_for("kitchen_task") == "notice"
    assert kind_for("staff_shift") == "notice"
    for decision_type in (
        "purchase_order", "promo", "batch", "outbound_call", "forecast_override_proposal",
    ):
        assert kind_for(decision_type) == "decision", decision_type


def test_kitchen_task_notice_tier_walkthrough(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "core.kitchen_tasks"],
        env={**os.environ, "DB_PATH": str(tmp_path / "kt.db")},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "kitchen_tasks demo OK" in result.stdout
