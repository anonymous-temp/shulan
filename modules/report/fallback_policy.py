"""Fixed retry and degradation rules for the report workflow.

These values are business rules rather than deployment configuration.  They
remain code-local and are injectable in tests; no public environment variable
is required or consulted here.
"""

from __future__ import annotations

from dataclasses import dataclass


# Report-only admission limit. Do not change the shared policy default below:
# other report types reuse it and retain their existing two-task limit.
REPORT_GENERATION_CONCURRENCY = 20

# Evaluate model work is bounded both within one report and across the
# application. Admission of 20 reports must not create 20 independent pools.
EVALUATE_MODEL_CONCURRENCY = 6
EVALUATE_SHARED_MODEL_CONCURRENCY = 12


@dataclass(frozen=True)
class ReportFallbackPolicy:
    source_fetch_attempts: int = 3
    evidence_attempts: int = 3
    llm_attempts: int = 2
    product_catalog_attempts: int = 3
    callback_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0)
    completed_result_ttl_seconds: float = 900.0
    completed_result_max_entries: int = 256
    # Business-safe in-process resource ceilings.  They are deliberately
    # code-local: deployments do not need another public environment switch,
    # while tests can still inject smaller values deterministically.
    max_concurrent_evaluate_reports: int = 2
    source_parse_concurrency: int = 4

    def attempts_for(self, value: int) -> int:
        return max(1, int(value))

    def backoff_after(self, failed_attempt_index: int) -> float:
        """Return delay after a zero-based failed attempt."""

        if not self.retry_backoff_seconds:
            return 0.0
        index = min(max(0, failed_attempt_index), len(self.retry_backoff_seconds) - 1)
        return max(0.0, float(self.retry_backoff_seconds[index]))


DEFAULT_REPORT_FALLBACK_POLICY = ReportFallbackPolicy()
