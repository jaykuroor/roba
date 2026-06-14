"""All constants & thresholds for the demo (00_ARCHITECTURE.md §22).

No magic numbers are left to implementers — every framework-level value lives
here, with the exact name and value from the spec. The sim/pos/weather values
are the binding defaults; the UI exposes a subset of them as adjustable.
"""

import os

# clock
OPERATING_WINDOW = ("08:00", "23:00")      # 54000 sim-s
REAL_MINUTES_PER_DAY_1X = 15               # default
TICK_REAL_MS = 250
SPEEDS = [0.25, 0.5, 1, 2, 4, 8]
SKIP_CLOSED_HOURS = True
CALL_MODE = "freeze"                        # or "slow" (0.1x)

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
STAFF_CAP_FACTOR = 0.5
VELOCITY_CLAMP = (0.6, 1.6)
SUGGESTION_INTERVAL_SIM_S = 54000          # ~1 sim-day

# batches
BATCH_BUFFER_SIM_S = 900

# inventory
SAFETY_DAYS = 0.5
PAR_DAYS = 3
EXPIRY_SCAN_SIM_S = 3600
EXPIRY_WINDOW_SIM_S = 172800               # 2 sim-days
PROMO_DISCOUNT_PCT = 20
APPROVAL_PO_THRESHOLD = 200                # currency units; above -> needs approval

# signals
SIGNAL_COOLDOWN_SIM_S = 1800
MAX_CASCADE_DEPTH = 5

# competitors / calls
COMPETITOR_RADIUS_KM = 3
COMPETITOR_CALL_TARGETS = 2

# llm
LLM_FALLBACK = ["gemini", "groq", "openrouter", "canned"]
LLM_RETRIES = 3
LLM_BACKOFF_BASE_S = 1.5
LLM_INTER_CALL_SLEEP_S = 2

# weather
WEATHER_FETCH_SIM_S = 10800                # every 3 sim-h

# database
DB_PATH = os.getenv("DB_PATH", "demo.db")
