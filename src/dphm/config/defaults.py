"""Default audit-column patterns, tolerances, and scales (05 §2, 05 §1 `defaults`).

The audit-column list is the single most load-bearing default in the product. Risk R1:
with `_LOADED_AT` left in the compared set, every row looks changed, SCD check 6 fires on
every row of every run, and the first run tells us nothing.
"""

from __future__ import annotations

from typing import Final

# Matched case-insensitively against the NORMALIZED UPPER-CASE column name (05 §2).
DEFAULT_AUDIT_PATTERNS: Final[tuple[str, ...]] = (
    "_LOADED_AT",
    "_UPDATED_AT",
    "ETL_*",
    "DW_*",
    "*_BATCH_ID",
    "INSERTED_AT",
    "SOURCE_FILE*",
    "_FIVETRAN_*",
    "_AIRBYTE_*",
)

DEFAULT_KEY_PATTERNS: Final[tuple[str, ...]] = ("*_ID", "*_KEY", "*_CODE")
DEFAULT_DERIVED_PATTERNS: Final[tuple[str, ...]] = ("*_FLAG_DERIVED", "*_SCORE")

# 05 §1 `defaults`. Decided once, in config, not per check (02 §4).
DEFAULT_NUMERIC_SCALE: Final[int] = 6
DEFAULT_FLOAT_ABS_TOLERANCE: Final[float] = 1e-9
DEFAULT_FLOAT_REL_TOLERANCE: Final[float] = 1e-9
DEFAULT_TIMESTAMP_GRANULARITY: Final[str] = "second"

# 06 §6. `min_rows_to_evaluate` exists because a check over an empty table trivially
# passes — the "check that can never fail" gate 3 exists to catch.
DEFAULT_MIN_ROWS_TO_EVALUATE: Final[int] = 1
DEFAULT_SAMPLE_LIMIT: Final[int] = 20

# 07 §3. Changing bucket_count invalidates cached fingerprints, so it is recorded per run.
DEFAULT_BUCKET_COUNT: Final[int] = 1024
DEFAULT_TIER2_MAX_KEYS: Final[int] = 50_000

# 13 §4 budgets.
DEFAULT_LANE_A_WAREHOUSE_SECONDS: Final[int] = 900
DEFAULT_LANE_B_USD: Final[float] = 5.00
DEFAULT_LANE_B_RDS_SECONDS: Final[int] = 600
DEFAULT_LLM_USD: Final[float] = 2.00

# 10 gate 1.
DEFAULT_CHECK_BUDGET_MS: Final[int] = 60_000
DEFAULT_CHECK_BUDGET_USD: Final[float] = 0.05

# The two schemas DPHM_WRITER owns, and nothing else (02 §1, 13 §1).
STATE_SCHEMA: Final[str] = "DPHM_STATE"
SCRATCH_SCHEMA: Final[str] = "DPHM_SCRATCH"
