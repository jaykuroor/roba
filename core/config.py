"""All constants & thresholds for the demo (00_ARCHITECTURE.md §22).

No magic numbers are left to implementers — every framework-level value lives
here, with the exact name and value from the spec. The sim/pos/weather values
are the binding defaults; the UI exposes a subset of them as adjustable.
"""

import os

from dotenv import dotenv_values

for _key, _value in dotenv_values(".env").items():
    if _key in {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_MODEL",
        "GEMINI_LIVE_MODEL",
        "GEMINI_LIVE_CALL_MODEL",
        "LLM_FORECAST_AUTO_MODE",
    } and _key not in os.environ and _value:
        os.environ[_key] = _value

# clock
OPERATING_WINDOW = ("08:00", "23:00")      # 54000 sim-s
REAL_MINUTES_PER_DAY_1X = 15               # default
TICK_REAL_MS = 250
SPEEDS = [0.25, 0.5, 1, 2, 4, 8]
SKIP_CLOSED_HOURS = True
CALL_MODE = "realtime"                      # or "freeze" | "slow" (0.1x)

# dayparts (start, end, weight) — weights sum ~1.0
DAYPARTS = {
    "breakfast": ("08:00", "11:00", 0.18),
    "lunch": ("11:00", "15:00", 0.34),
    "afternoon": ("15:00", "17:00", 0.10),
    "dinner": ("17:00", "22:00", 0.33),
    "late": ("22:00", "23:00", 0.05),
}

# pos
BASE_ORDERS_PER_DAY = 300
LINES_PER_ORDER = {1: .5, 2: .3, 3: .2}
CHANNEL_MIX = {"dine_in": .70, "delivery": .20, "takeout": .10}
CANCEL_RATE = 0.03
VELOCITY_WINDOW_SIM_S = 1800
VELOCITY_ANOMALY_PCT = 0.30

# forecasting
FORECAST_INTERVAL_SIM_S = 1800
HISTORY_DAYS = 30
EVENT_MULT = 1.35
EVENT_ATTENDANCE_REFERENCE = 1000.0
EVENT_ATTENDANCE_MAX_MULT = 2.0
EVENT_STACK_MAX_MULT = 2.5
STAFF_CAP_FACTOR = 0.5
VELOCITY_CLAMP = (0.6, 1.6)
SUGGESTION_INTERVAL_SIM_S = 54000          # ~1 sim-day
LLM_MULTIPLIER_CLAMP = (0.0, 2.0)
LLM_EXTREME_MULTIPLIERS = (0.0, 2.0)
LLM_FORECAST_AUTO_MODE = os.getenv("LLM_FORECAST_AUTO_MODE", "1").lower() in {
    "1", "true", "yes", "on"
}
COLD_TEMP_C = 12.0
HOT_TEMP_C = 30.0

# batches
BATCH_BUFFER_SIM_S = 900

# inventory
SAFETY_DAYS = 0.5
PAR_DAYS = 3
EXPIRY_SCAN_SIM_S = 3600
EXPIRY_WINDOW_SIM_S = 172800               # 2 sim-days
PROMO_DISCOUNT_PCT = 20
APPROVAL_PO_THRESHOLD = 500                # currency units; above -> needs approval

# procurement — forecast-driven par / reorder sizing
# REORDER_INTERVAL_DAYS: expected days between reorder checks (how long between sweeps)
REORDER_INTERVAL_DAYS = float(os.getenv("REORDER_INTERVAL_DAYS", "1.0"))
# SAFETY_FRACTION: safety_stock = SAFETY_FRACTION * robust_daily_usage * lead_days
SAFETY_FRACTION = float(os.getenv("SAFETY_FRACTION", "0.25"))
# DYNAMIC_PAR_INTERVAL_SIM_S: how often to recompute par/reorder_point from the horizon
DYNAMIC_PAR_INTERVAL_SIM_S = float(
    os.getenv("DYNAMIC_PAR_INTERVAL_SIM_S", str(SUGGESTION_INTERVAL_SIM_S * 7))
)  # ~1 sim-week
# HORIZON_EMIT_INTERVAL_SIM_S: how often to emit the rolling 7-day horizon signal
HORIZON_EMIT_INTERVAL_SIM_S = float(
    os.getenv("HORIZON_EMIT_INTERVAL_SIM_S", str(54000.0))  # ~1 sim-day
)
INVENTORY_SHORTAGE_SIGNALS_ENABLED = os.getenv(
    "INVENTORY_SHORTAGE_SIGNALS_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}

# signals
SIGNAL_COOLDOWN_SIM_S = 1800
MAX_CASCADE_DEPTH = 5

# competitors / calls
COMPETITOR_RADIUS_KM = 3
COMPETITOR_CALL_TARGETS = 2
COMPETITOR_AGGREGATOR_POLL_SIM_S = 1800
COMPETITOR_MENU_REFRESH_SIM_S = 7200

# sourcing optimizer (call overhaul)
# SOURCING_RUN_INTERVAL_SIM_S: how often to re-run the MILP/greedy sourcing solve.
SOURCING_RUN_INTERVAL_SIM_S = float(os.getenv("SOURCING_RUN_INTERVAL_SIM_S", str(54000.0 * 3)))  # ~3 sim-days
# SOURCING_SWITCHING_COST: default per-ingredient switching cost (currency). Overridable via AppSettings.
SOURCING_SWITCHING_COST = float(os.getenv("SOURCING_SWITCHING_COST", "5.0"))
# SOURCING_HORIZON_DAYS: default planning horizon. Overridable via AppSettings.
SOURCING_HORIZON_DAYS = float(os.getenv("SOURCING_HORIZON_DAYS", "7.0"))
# NEGOTIATION_COOLDOWN_SIM_S: minimum sim-time between negotiation calls for the same supplier+ingredient.
NEGOTIATION_COOLDOWN_SIM_S = float(os.getenv("NEGOTIATION_COOLDOWN_SIM_S", str(54000.0 * 7)))  # ~1 sim-week
# NEGOTIATION_MIN_SAVINGS_PCT: minimum % price improvement required before requesting a call.
NEGOTIATION_MIN_SAVINGS_PCT = float(os.getenv("NEGOTIATION_MIN_SAVINGS_PCT", "8.0"))
# AUTO_APPLY_SUPPLIER_CHANGES: runtime default (overridable via AppSettings DB row).
AUTO_APPLY_SUPPLIER_CHANGES = os.getenv("AUTO_APPLY_SUPPLIER_CHANGES", "0").lower() in {
    "1", "true", "yes", "on"
}

# llm — Vertex AI only; Groq/OpenRouter removed
# NOTE: gemini-3.1-flash-lite returns 404 (not a real / accessible Vertex model),
# which silently drops every completion to the canned no-op (call extraction,
# forecasting, cook reports all degrade invisibly). gemini-2.5-flash-lite is the
# lite model actually available to this project/region.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
# Supplier-call outcome extraction drives real ordering, so it uses a stronger
# reasoning model than the default (parses compound offers reliably). Runs once
# per call, post-hangup, so latency/cost are not a concern.
GEMINI_EXTRACTION_MODEL = os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-2.5-pro")
LLM_FALLBACK = ["gemini", "canned"]
# Deadline for the LLM-authority forecast pass. gemini-2.5-pro on the large
# structured forecast prompt regularly needs 30-60s; on timeout the
# deterministic forecast publishes immediately (wait bounded by this value).
LLM_AUTHORITY_TIMEOUT_S = float(os.getenv("LLM_AUTHORITY_TIMEOUT_S", "75"))
LLM_RETRIES = 3
LLM_BACKOFF_BASE_S = 1.5
LLM_INTER_CALL_SLEEP_S = 2
VOICE_EMIT_LEGACY_USER_FACT = os.getenv(
    "VOICE_EMIT_LEGACY_USER_FACT", "true"
).strip().lower() not in {"0", "false", "no", "off"}

# Voice planner (Stream B)
VOICE_DEFAULT_MODE = os.getenv("VOICE_DEFAULT_MODE", "confirm")  # "confirm" | "auto"

# Kitchen task overdue-notice escalation tiers, in sim-minutes past due.
# A pending task raises/updates ONE manager notice as it crosses each tier
# (5 min → first notice, 10/15 min → escalations). Urgency climbs
# normal → high → critical per tier; temp/safety tasks start one tier hotter.
TASK_OVERDUE_NOTICE_TIERS_MIN = [
    int(x) for x in os.getenv("TASK_OVERDUE_NOTICE_TIERS_MIN", "5,10,15").split(",")
]

# Kitchen batch lifecycle (Stream B3)
BATCH_APPROVAL_GATED = os.getenv("BATCH_APPROVAL_GATED", "0").lower() in {
    "1", "true", "yes", "on"
}

# Inventory optimizer LLM (Stream E)
OPTIMIZER_LLM_AUTO_MODE = os.getenv("OPTIMIZER_LLM_AUTO_MODE", "0").lower() in {
    "1", "true", "yes", "on"
}
PROMO_SLOW_MOVER_PCT = int(os.getenv("PROMO_SLOW_MOVER_PCT", "15"))

# Procurement reliability premium (two-pass MILP)
# RELIABILITY_CASH_TOLERANCE: maximum fraction of the cash-optimal plan cost that may
# be spent to buy down one-day-delay exposure.  0.01 = 1%.  Set to 0 to force pure
# cash-optimal ordering (reliability has no influence on supplier selection or timing).
# Procurement production-start hour: deliveries arriving at or before this hour
# (24-h clock) are available for the same day's production.  Deliveries after
# this hour only serve the *next* day.  Default 8.0 = 08:00 (matches the
# historical DAY_OPEN_OFFSET assumption; no behavioral change unless a supplier's
# delivery_hour is set above this threshold).
PRODUCTION_START_HOUR = float(os.getenv("PRODUCTION_START_HOUR", "8.0"))
# Grace window (hours) past PRODUCTION_START_HOUR during which an arrival still
# serves the SAME day, and past an order-by cutoff during which the order can
# still make its delivery slot.  The morning forecast+plan runs minutes after
# day-open (LLM realtime holds advance the sim clock), so without slack every
# supplier's first feasible delivery slips a full day at 08:0x — all order
# dates land tomorrow (zero POs today), rows are born at_risk, and near-term
# demand shows phantom "uncoverable" gaps.  Sales run all day, so an arrival
# shortly after open genuinely serves that day.
PROCUREMENT_SERVICE_GRACE_H = float(os.getenv("PROCUREMENT_SERVICE_GRACE_H", "2.0"))

RELIABILITY_CASH_TOLERANCE = float(os.getenv("RELIABILITY_CASH_TOLERANCE", "0.01"))
# RELIABILITY_STRESS_ENABLED: when True, run the two-pass lexicographic solve (pass 1
# = cash-optimal, pass 2 = minimize one-day-delay exposure within the cash cap).
# Set to "0" / "false" to fall back to single-pass cash-optimal (same as tolerance=0).
RELIABILITY_STRESS_ENABLED = os.getenv("RELIABILITY_STRESS_ENABLED", "1").lower() in {
    "1", "true", "yes", "on"
}
# RELIABILITY_ROBUST_HARD_DELAY: when True, run a pass-3 solve that hard-constrains
# demand coverage even if qualifying (unreliable) suppliers' deliveries slip one day.
# Default OFF — the banner reports delay exposure honestly regardless of this flag.
# Enabling raises cost and may force earlier or more-reliable-supplier orders.
# Falls back gracefully to pass-2 plan if the delay scenario is infeasible.
RELIABILITY_ROBUST_HARD_DELAY = os.getenv("RELIABILITY_ROBUST_HARD_DELAY", "0").lower() in {
    "1", "true", "yes", "on"
}
# RELIABILITY_ROBUST_MIN_RELIABILITY: reliability_score threshold below which a
# supplier qualifies for the delay scenario in robust mode (default 0.95).
RELIABILITY_ROBUST_MIN_RELIABILITY = float(
    os.getenv("RELIABILITY_ROBUST_MIN_RELIABILITY", "0.95")
)

# PROCUREMENT_JITTER_FRACTION: fraction of total horizon demand used as the
# anti-jitter threshold for top-up orders.  When a re-plan would create a top-up
# (existing pipeline already covers part of demand), the unrounded shortfall must
# exceed (JITTER_FRACTION × total_demand) before the top-up is persisted.
# 0.0 (default) disables the filter — every solver-determined top-up is kept.
# Suggested range: 0.05–0.15 for moderate jitter suppression.
PROCUREMENT_JITTER_FRACTION = float(os.getenv("PROCUREMENT_JITTER_FRACTION", "0.0"))

# COVERAGE_EPSILON: tolerance for the "coverage_ok" aggregate shortfall gate.
# Replaces the legacy flat <= 1.0 check (which masked real sub-gram shortages).
# Default 0.01 base units (10 mg / 10 µL) is safely above float-arithmetic noise
# (~1e-8) but below any operationally meaningful shortage (e.g. 1g flour).
COVERAGE_EPSILON = float(os.getenv("COVERAGE_EPSILON", "0.01"))

# Vertex AI Live API (Stream B5)
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-live-2.5-flash-native-audio")
GEMINI_LIVE_CALL_MODEL = os.getenv("GEMINI_LIVE_CALL_MODEL", "gemini-live-2.5-flash-native-audio")

# Availability OOS mode: "threshold" (disabled when on_hand ≤ safety_stock/reorder_point)
#                        "zero"      (disabled only when on_hand ≤ 0)
AVAILABILITY_OOS_MODE = "threshold"

# Vertex AI auth / project
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "roba.json")

# weather
WEATHER_FETCH_SIM_S = 10800                # every 3 sim-h

# weather → channel-mix shift (§18.5; applied by the POS simulator). Per
# condition, a multiplier on each channel's sampling weight; channels absent
# from a condition default to 1.0. (The item-level hot/cold factors in §18.5
# are the Forecaster's job, not the POS — only the channel shift lives here.)
WEATHER_CHANNEL_SHIFT = {
    "rain":  {"dine_in": 0.85, "delivery": 1.20},
    "storm": {"dine_in": 0.85, "delivery": 1.20},
    "snow":  {"dine_in": 0.60, "delivery": 1.10},
}

# database
DB_PATH = os.getenv("DB_PATH", "demo.db")
# Reasoning model (used by core/reasoner.py for complex trade-off decisions)
GEMINI_REASONER_MODEL: str = os.getenv("GEMINI_REASONER_MODEL", "gemini-2.5-pro")

