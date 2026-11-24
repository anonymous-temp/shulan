"""Health Analysis's synchronous request, budget and delivery contracts."""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class InvalidAnalysisInputError(ValueError):
    pass


class AnalysisGenerationStage(StrEnum):
    MATERIALS = 'materials'
    TARGETS = 'targets'
    RESEARCH = 'research'
    MATCH = 'match'
    RISK_ANALYSIS = 'risk_analysis'
    PLAN = 'plan'
    COVERAGE = 'coverage'
    REVIEW = 'review'
    SURVEY_REWRITE = 'survey_rewrite'
    RESULT = 'result'


class AnalysisGenerationFailure(StrEnum):
    UNCLASSIFIED = 'unclassified'
    MODEL_UNAVAILABLE = 'model_unavailable'
    MODEL_TIMEOUT = 'model_timeout'
    INVALID_MODEL_RESPONSE = 'invalid_model_response'
    INVALID_STAGE_OUTPUT = 'invalid_stage_output'
    REVIEW_VERSION_MISMATCH = 'review_version_mismatch'
    REVIEW_QUESTIONS_MISSING = 'review_questions_missing'
    REVIEW_VERDICTS_INVALID = 'review_verdicts_invalid'
    REVIEW_ISSUES_INVALID = 'review_issues_invalid'
    REVIEW_RULES_MISSING = 'review_rules_missing'
    REVIEW_RULES_INVALID = 'review_rules_invalid'
    REVIEW_RECOMMENDATIONS_INVALID = 'review_recommendations_invalid'
    SURVEY_REVIEW_EXHAUSTED = 'survey_review_exhausted'
    SURVEY_REWRITE_INVALID = 'survey_rewrite_invalid'
    SURVEY_COVERAGE_INCOMPLETE = 'survey_coverage_incomplete'


class AnalysisGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: AnalysisGenerationStage = AnalysisGenerationStage.RESULT,
        reason: AnalysisGenerationFailure = AnalysisGenerationFailure.UNCLASSIFIED,
    ):
        super().__init__(message)
        self.stage = stage
        self.reason = reason


@contextmanager
def analysis_generation_stage(stage: AnalysisGenerationStage):
    """Label expected generation failures without overriding an inner stage."""
    try:
        yield
    except AnalysisGenerationError as error:
        if error.stage is AnalysisGenerationStage.RESULT:
            error.stage = stage
        if error.reason is AnalysisGenerationFailure.UNCLASSIFIED:
            error.reason = AnalysisGenerationFailure.INVALID_STAGE_OUTPUT
        raise


class AnalysisTimeoutError(TimeoutError):
    pass


class AnalysisCapacityError(RuntimeError):
    pass


class AnalysisBusyError(RuntimeError):
    pass


class TraceDeliveryError(RuntimeError):
    pass


class KnowledgeUnavailableError(RuntimeError):
    pass


class SourceUnavailableError(RuntimeError):
    """Expected source dependency failure while reading a fixed user/group, without body text."""

    def __init__(self, category: str = 'unavailable'):
        self.category = category
        super().__init__('Source dependency unavailable')


@dataclass(frozen=True)
class AnalysisLimits:
    """Development guardrails, pending measurement with actual dependencies.

    These bounds make offline behavior explicit; they do not promise a live
    service's latency or capacity. Every wait consumes total_timeout_seconds.
    """

    total_timeout_seconds: float = 120
    source_timeout_seconds: float = 10
    trace_timeout_seconds: float = 5
    max_input_bytes: int = 2_000_000
    max_surveys: int = 20
    max_questions: int = 1000
    max_source_bytes: int = 2_000_000
    max_trace_bytes: int = 8_000_000
    max_active_requests: int = 4
    max_waiting_requests: int = 16
    trace_attempts: int = 2

    def __post_init__(self):
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive')
            if name != 'total_timeout_seconds' and name.endswith('_seconds'):
                if value > self.total_timeout_seconds:
                    raise ValueError(f'{name} exceeds total execution timeout')
            if not name.endswith('_seconds') and not isinstance(value, int):
                raise ValueError(f'{name} must be an integer')


@dataclass(frozen=True)
class TraceReceipt:
    run_id: str
    content_hash: str
    durable: bool


class RunTraceWriter(Protocol):
    async def write(self, record: dict) -> TraceReceipt:
        """Confirm identical content persisted for runId, reject content conflicts."""
        ...


@dataclass(frozen=True)
class AnalysisSourceScope:
    """Lookup parameters and explicit sender mapping, without an authorization provider."""

    user_id: str
    group_id: str
    subject_id: str | None = None
    patient_sender_ids: tuple[str, ...] = ()
    lifecycle_epoch: int | None = None


class PatientSenderReader(Protocol):
    """Data-attribution mapping only; no access authorization or relationship proof."""

    async def read(self, *, user_id: str, group_id: str) -> tuple[str, ...]: ...
