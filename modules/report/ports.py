"""Business-facing ports and transport-neutral report boundary types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from modules.report.schemas import (
    CallbackPayload,
    EvidenceRecord,
    HospitalDataInput,
    RemoteFileInput,
    ParsedMicrobiomeReport,
    TestDataInput,
)


class ExternalSourceError(RuntimeError):
    """A caller-provided report source could not be acquired or decoded."""


class ExternalSourceSecurityError(ExternalSourceError):
    """A caller-provided URL violates source-network safety constraints."""


class RetryableExternalSourceError(ExternalSourceError):
    """A transient source error may be retried by the adapter."""


class EvidenceServiceError(RuntimeError):
    """An evidence provider could not serve a valid response."""


@dataclass(frozen=True)
class ReportArtifactRecord:
    """一类最终报告产物的持久化记录。

    ``body`` 是最终对外结构；模型 reasoning、内部提示词、密钥和内部审计
    都不在其中。刻意不含回调交付状态：生成结果与投递状态分开，回调成功
    与否不决定报告是否可查询。
    """

    final_report_id: str
    user_id: str
    session_id: str
    report_type: str
    generation_status: str
    review_status: str
    content_hash: str
    body: dict[str, Any]
    schema_version: int = 1
    documents: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ReportArtifactSummary:
    """查询用的产物摘要，不含正文。"""

    final_report_id: str
    report_type: str
    generation_status: str
    review_status: str
    saved_at: str


class ReportArtifactStore(Protocol):
    """Report 自有的最终报告读写端口。

    Chat 与 Agent 节点必须经由本端口访问，不得直接操作集合。
    """

    async def save_artifact(
        self,
        record: ReportArtifactRecord,
        *,
        subject_id: str | None = None,
    ) -> str: ...

    async def get_artifact(
        self,
        *,
        user_id: str,
        final_report_id: str,
        report_type: str,
    ) -> ReportArtifactRecord | None: ...

    async def list_batches(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReportContextSelection:
    """Agent 从已授权目录中选出的报告引用。"""

    final_report_id: str
    report_type: str


class ReportContextSelector(Protocol):
    """多份候选时的受控报告选择端口。"""

    async def select(
        self,
        *,
        user_id: str,
        session_id: str,
        message: str,
        candidates: list[dict[str, Any]],
    ) -> ReportContextSelection | None: ...


class ProfileSnapshotError(RuntimeError):
    """长期画像快照无法读取；报告按无画像降级继续。"""


@dataclass(frozen=True)
class ReportProfileFact:
    """快照中的单条只读健康事实。

    字段取自画像公共投影，不含租户标识和内部幂等元数据。
    """

    fact_id: str
    subject_id: str
    subject_kind: str
    fact_type: str
    display_name: str
    value: Any
    value_type: str
    assertion_level: str
    verification_status: str
    status: str
    effective_start: str | None = None
    effective_end: str | None = None
    updated_at: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportProfileSnapshot:
    """一次报告执行内固定不变的长期画像切片。

    ``truncated`` 必须如实反映预算截断：一个有预算的快照不能被当作
    完整病史。``stale`` 表示读取期间画像版本发生变化且重取后仍不一致。
    """

    user_id: str
    self_subject_id: str
    read_at: str
    profile_version: int | None
    facts: tuple[ReportProfileFact, ...] = ()
    truncated: bool = False
    truncation_reason: str | None = None
    stale: bool = False


class ProfileSnapshotGateway(Protocol):
    """Report 自有的只读长期画像端口。

    实现方负责适配画像模块的公共读取能力；报告业务不直接依赖
    health_profile 的领域模型，也不解析 Chat 的提示词字符串。
    """

    async def read(self, user_id: str) -> ReportProfileSnapshot | None: ...


class DocumentServiceError(RuntimeError):
    """The document extraction service could not return usable text."""


class FollowupGatewayError(RuntimeError):
    """A follow-up report provider failed with an explicit retry decision."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class EviMedChannelResult:
    channel: str
    status: str
    records: list[EvidenceRecord]
    provider: str | None = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class DownloadedFile:
    filename: str
    content_type: str
    content: bytes
    declared_type: str


@dataclass(frozen=True)
class DownloadedTestData:
    detail: dict[str, Any]
    detail_checksum: str
    summary: str | None = None


class SourceGateway(Protocol):
    async def fetch_file(self, source: RemoteFileInput) -> DownloadedFile: ...

    async def fetch_test_data(self, source: TestDataInput) -> DownloadedTestData: ...

    async def fetch_evaluation_json(self, url: str) -> DownloadedTestData: ...


class DocumentTextGateway(Protocol):
    async def extract_text(
        self,
        source: RemoteFileInput,
        *,
        filename: str,
        content: bytes | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class ReportDocumentSource:
    parsed: ParsedMicrobiomeReport
    text: str
    filename: str
    file_type: str
    references: tuple[dict[str, Any], ...] = ()
    # Ownership-checked original clinical cells, consumed only by evaluate.
    # They are not an accepted shared interpretation or a public API field.
    evaluation_clinical_rows: tuple[HospitalDataInput, ...] = ()
    # Verified native originals are transport-local and never serialized.
    # Evaluate uses them for the same recovery as originalReportUrl inputs.
    evaluation_original_documents: tuple[Any, ...] = ()


class ReportDocumentGateway(Protocol):
    """Request-bound shared-file processing; implementations own no model clients."""

    async def read(self, source: RemoteFileInput, *, index: int) -> ReportDocumentSource: ...

    async def combine(self, indexes: list[int]) -> ReportDocumentSource: ...


class EvidenceGateway(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def search(
        self,
        query: str,
        *,
        supports: list[str],
    ) -> list[EvidenceRecord]: ...

    async def search_detailed(
        self,
        query: str,
        *,
        supports: list[str],
        channels: tuple[str, ...] = ("guide", "literature"),
    ) -> list[EviMedChannelResult]: ...


class ReportCallbackGateway(Protocol):
    def build_body(
        self,
        payload: CallbackPayload | dict[str, Any],
    ) -> dict[str, Any]: ...

    async def send(
        self,
        payload: CallbackPayload | dict[str, Any],
    ) -> dict[str, Any]: ...

    async def deliver(self, body: dict[str, Any]) -> None: ...


class FollowupModelGateway(Protocol):
    """后续报告的受控模型任务端口。

    task_type 由服务端允许列表校验；调用方不能传系统 Prompt、模型名或工具。
    """

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str: ...


class FollowupReportGateway(Protocol):
    """后续报告的非模型出站 I/O：产品目录与 HTTP 回调。"""

    async def fetch_product_catalog(
        self,
        *,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]: ...

    async def deliver_json(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 300.0,
    ) -> int: ...
