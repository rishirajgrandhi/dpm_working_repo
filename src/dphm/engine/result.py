"""The CheckResult model (06 §2, 08).

Four states, and the distinction matters: PASS, FAIL, INCONCLUSIVE (we tried and could
not tell) and UNVALIDATED (we have no way to check this yet). Coverage treats the last
two as NOT covered. "We couldn't tell" and "everything's fine" never share a status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["PASS", "FAIL", "INCONCLUSIVE", "UNVALIDATED"]


@dataclass
class CheckResult:
    result_id: str
    run_id: str
    check_id: str
    status: Status
    failing_row_count: int = 0
    evaluated_row_count: int | None = None
    failing_fingerprint: str | None = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    # REQUIRED. If the engine cannot say what it compared, the result is an error, not a
    # pass (risk R1, 08 §2).
    columns_compared: list[str] = field(default_factory=list)
    columns_excluded: list[str] = field(default_factory=list)
    exclusion_reasons: dict[str, str] = field(default_factory=dict)
    scope_predicate: str = ""
    sql_sha256: str = ""
    query_id: str | None = None
    duration_ms: int = 0
    estimated_usd: float | None = None
    suppressed_by: str | None = None
    error_message: str | None = None
    violation_codes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status == "PASS" and not self.columns_compared:
            raise ValueError(
                f"check {self.check_id} reported PASS without recording which columns it "
                "compared. A run that cannot say what it compared is an error, not a pass "
                "(risk R1)."
            )

    @property
    def is_failure(self) -> bool:
        return self.status == "FAIL"

    @property
    def counts_as_covered(self) -> bool:
        return self.status in {"PASS", "FAIL"}
