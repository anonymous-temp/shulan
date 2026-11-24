"""Business rules for persisting the five final report artifacts.

保存边界（文档 §8.2）：

    单类报告生成、校验并固定对外结构
      -> 幂等保存产物
      -> 成为可查询、可供 Chat 引用的报告
      -> 发送该产物的业务回调

规则：

* 只保存**最终对外结构**；模型 reasoning、内部提示词、密钥和内部审计
  都不进产物文档。
* 幂等键是 ``(finalReportId, reportType)``：相同内容重复提交幂等成功，
  不同内容重放产生明确冲突并保留原件。
* **生成结果与回调交付状态分开**：产物文档不记录投递状态，回调成功与否
  不决定报告是否可查询。
* 保存失败时不发送对应的成功回调，也不宣称归档完成。
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from contextvars import Token
from typing import Any

from modules.report.ports import ReportArtifactRecord
from modules.report.ports import ReportArtifactStore

EVALUATE_REPORT_TYPES: tuple[str, ...] = (
    "evaluate",
    "instruction",
    "opinion",
    "explain",
)

# Storage accepts every active report kind, while an evaluate batch generates
# only EVALUATE_REPORT_TYPES. Other sources must not become recovery placeholders.
REPORT_TYPES: tuple[str, ...] = (
    *EVALUATE_REPORT_TYPES,
    # 肠道与炎症报告解读：入参 type=gutBarrier，产物类型为 explain_gutBarrier。
    # 不加进来的话 save_report_artifact 会直接 SKIPPED，产物既不落库也不回调。
    "explain_gutBarrier",
)

# 所有产物固定标注为 AI 生成、未经医生核验，不因类型名含「诊断意见」
# 或「临床医嘱」而冒充医生签发内容。
REVIEW_STATUS_AI_UNVERIFIED = "ai_unverified"

ARTIFACT_SCHEMA_VERSION = 1

# 保存结果。只有前三种允许继续发送该产物的成功回调。
SAVED = "saved"
IDEMPOTENT = "idempotent"
SKIPPED = "skipped"          # 未配置存储：保持迁移前行为
CONFLICT = "conflict"        # 同批次同类型已存在不同正文，保留原件
FAILED = "failed"            # 存储不可用，重试后仍失败

DELIVERABLE_RESULTS = frozenset({SAVED, IDEMPOTENT, SKIPPED})


class _ActiveBatch:
    """一次报告执行内不变的批次身份。"""

    __slots__ = (
        'store', 'final_report_id', 'user_id', 'session_id', 'subject_id', 'documents', 'defer_callbacks',
    )

    def __init__(
        self,
        *,
        store: ReportArtifactStore | None,
        final_report_id: str,
        user_id: str,
        session_id: str,
        defer_callbacks: bool = False,
    ) -> None:
        self.store = store
        self.final_report_id = final_report_id
        self.user_id = user_id
        self.session_id = session_id
        self.defer_callbacks = defer_callbacks
        self.subject_id: str | None = None
        self.documents: tuple[dict[str, Any], ...] = ()


_ACTIVE_BATCH: ContextVar[_ActiveBatch | None] = ContextVar(
    "active_report_batch",
    default=None,
)


def bind_report_batch(
    *,
    store: ReportArtifactStore | None,
    final_report_id: str | None,
    user_id: str,
    session_id: str,
    defer_callbacks: bool = False,
) -> Token[_ActiveBatch | None] | None:
    """绑定本次执行的批次；缺少存储或批次号时不绑定，行为与迁移前一致。"""
    if not final_report_id:
        return None
    return _ACTIVE_BATCH.set(_ActiveBatch(
        store=store,
        final_report_id=final_report_id,
        user_id=user_id,
        session_id=session_id,
        defer_callbacks=defer_callbacks,
    ))


def reset_report_batch(token: Token[_ActiveBatch | None] | None) -> None:
    if token is not None:
        _ACTIVE_BATCH.reset(token)


def bind_artifact_namespace(namespace: str) -> Token[_ActiveBatch | None] | None:
    """在当前批次下临时切到一个子命名空间保存产物，用完调用 reset_report_batch 复位。

    用途：同一个 finalReportId 下会出现两份同类型产物时（如评估报告与肠道与炎症报告
    各自生成的 opinion / instruction），后保存的一份会因幂等键 ``(finalReportId, reportType)``
    相同而被判为冲突、不发回调。把其中一方的存储键改成 ``<finalReportId>:<namespace>``
    即可互不覆盖。只影响存储键，不改报文里的 finalReportId；未绑定批次时什么都不做。
    """
    batch = _ACTIVE_BATCH.get()
    if batch is None or not namespace:
        return None
    derived = _ActiveBatch(
        store=batch.store,
        final_report_id=f"{batch.final_report_id}:{namespace}",
        user_id=batch.user_id,
        session_id=batch.session_id,
        defer_callbacks=batch.defer_callbacks,
    )
    derived.subject_id = batch.subject_id
    derived.documents = batch.documents
    return _ACTIVE_BATCH.set(derived)


def active_final_report_id() -> str | None:
    batch = _ACTIVE_BATCH.get()
    return None if batch is None else batch.final_report_id


def callbacks_are_deferred() -> bool:
    batch = _ACTIVE_BATCH.get()
    return batch is not None and batch.defer_callbacks


def note_report_subject(subject_id: str | None) -> None:
    """记录本人主体，供批次文档使用；报告执行内只写一次。"""
    batch = _ACTIVE_BATCH.get()
    if batch is not None and subject_id and batch.subject_id is None:
        batch.subject_id = subject_id


def content_hash(body: Any) -> str:
    """对外正文的规范化摘要，用于幂等与冲突判定。"""
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def note_report_documents(documents: list[dict[str, Any]]) -> None:
    batch = _ACTIVE_BATCH.get()
    if batch is not None:
        indexed = {item["fileIndex"]: dict(item) for item in batch.documents}
        for item in documents:
            previous = indexed.get(item["fileIndex"], {})
            updates = {
                key: value
                for key, value in item.items()
                if key not in previous or (
                    value is not None
                    and not (key == "ingestionStatus" and value == "unknown")
                )
            }
            indexed[item["fileIndex"]] = {**previous, **updates}
        batch.documents = tuple(indexed[i] for i in sorted(indexed))


def build_artifact_record(
    batch: _ActiveBatch,
    *,
    report_type: str,
    body: dict[str, Any],
    generation_status: str,
) -> ReportArtifactRecord:
    return ReportArtifactRecord(
        final_report_id=batch.final_report_id,
        user_id=batch.user_id,
        session_id=batch.session_id,
        report_type=report_type,
        generation_status=generation_status,
        review_status=REVIEW_STATUS_AI_UNVERIFIED,
        content_hash=content_hash(body),
        body=body,
        schema_version=ARTIFACT_SCHEMA_VERSION,
        documents=batch.documents,
    )


async def save_report_artifact(
    report_type: str,
    body: dict[str, Any],
    *,
    generation_status: str,
) -> str:
    """幂等保存一类产物，返回保存结果。

    永不抛出：存储不可用只影响该产物是否发送成功回调，不影响其他产物，
    也不重跑解析或模型。
    """
    if report_type not in REPORT_TYPES:
        return SKIPPED
    batch = _ACTIVE_BATCH.get()
    if batch is None or batch.store is None:
        return SKIPPED
    if not isinstance(body, dict) or not body:
        return SKIPPED
    record = build_artifact_record(
        batch,
        report_type=report_type,
        body=body,
        generation_status=generation_status,
    )
    return await batch.store.save_artifact(record, subject_id=batch.subject_id)


def is_deliverable(result: str) -> bool:
    """保存失败或冲突时不得发送该产物的成功回调。"""
    return result in DELIVERABLE_RESULTS


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "CONFLICT",
    "EVALUATE_REPORT_TYPES",
    "FAILED",
    "IDEMPOTENT",
    "REPORT_TYPES",
    "REVIEW_STATUS_AI_UNVERIFIED",
    "SAVED",
    "SKIPPED",
    "active_final_report_id",
    "bind_artifact_namespace",
    "bind_report_batch",
    "content_hash",
    "is_deliverable",
    "note_report_subject",
    "reset_report_batch",
    "save_report_artifact",
]
