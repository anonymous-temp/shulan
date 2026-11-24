"""画像摄取处理器共享结果与可安全持久化的错误。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ProfileProcessingSummary


@dataclass(frozen=True, slots=True)
class ProfileIngestionProcessingResult:
    graph_version: str
    policy_version: str
    extractor_version: str
    candidate_count: int
    fact_count: int
    replayed_fact_count: int
    rejected_candidate_count: int
    source_item_count: int | None = None
    skipped_item_count: int = 0
    reason_codes: tuple[str, ...] = ()

    def summary(self) -> ProfileProcessingSummary:
        return ProfileProcessingSummary(
            source_item_count=self.source_item_count,
            candidate_count=self.candidate_count,
            fact_count=self.fact_count,
            replayed_fact_count=self.replayed_fact_count,
            rejected_candidate_count=self.rejected_candidate_count,
            skipped_item_count=self.skipped_item_count,
            reason_codes=self.reason_codes,
        )


class ProfileIngestionProcessingError(RuntimeError):
    """只把稳定错误码交给任务队列，不持久化健康数据。"""

    def __init__(self, error_code: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
