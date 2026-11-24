"""Read-only long-term profile adapter for the report module.

适配画像模块的公共读取能力，产出 Report 自有的 ``ReportProfileSnapshot``。
不读取前端投影全文，也不解析 Chat 的提示词字符串；Chat 的
``build_context`` 是对话提示，不是报告数据集。

读取失败或超预算时记录脱敏 warning 并返回 ``None``，报告按无画像继续，
不会因为画像不可用而失败，也不等待画像提取 LLM 完成。
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from modules.report.ports import ReportProfileFact
from modules.report.ports import ReportProfileSnapshot
from utils.log import getLogger

logger = getLogger(__name__)

# 单页与总量预算。超出即截断并如实标注，不声称快照包含完整病史。
PAGE_LIMIT = 100
MAX_FACTS = 300

# 只读取与报告相关的三种状态；删除墓碑本身就不会返回。
READ_STATUSES = ("active", "pending", "disputed")

SELF_SUBJECT_KIND = "self"
BLOOD_RELATIVE_KIND = "blood_relative"
# 朋友、非血亲家属的健康事实与本次报告无关，直接不读。
READ_SUBJECT_KINDS = (SELF_SUBJECT_KIND, BLOOD_RELATIVE_KIND)


class HealthProfileSnapshotGateway:
    """通过 HealthProfileService 读取一次固定的画像切片。"""

    def __init__(self, *, profile_service: Any) -> None:
        self._service = profile_service

    async def read(self, user_id: str) -> ReportProfileSnapshot | None:
        try:
            snapshot = await self._read_once(user_id)
            if snapshot is None:
                return None
            # 读取期间画像可能被并发修改：检测到版本变化就整体重取一次，
            # 仍不一致则明确标记 stale，不假装拿到了事务快照。
            confirm = await self._profile_version(user_id)
            if confirm == snapshot.profile_version:
                return snapshot
            retried = await self._read_once(user_id)
            if retried is None:
                return None
            if await self._profile_version(user_id) == retried.profile_version:
                return retried
            logger.warning(
                "report profile snapshot is unstable: user_id=%s",
                user_id,
            )
            return _mark_stale(retried)
        except Exception as error:
            # 不记录事实正文，只留可定位的 user_id。
            logger.warning(
                "report profile snapshot unavailable: user_id=%s error_type=%s",
                user_id,
                type(error).__name__,
            )
            return None

    async def _read_once(self, user_id: str) -> ReportProfileSnapshot | None:
        listing = await self._service.list_subjects(user_id)
        subjects = [
            item
            for item in listing.get("subjects", [])
            if item.get("kind") in READ_SUBJECT_KINDS
            and item.get("status") == "active"
        ]
        self_subject = next(
            (item for item in subjects if item.get("kind") == SELF_SUBJECT_KIND),
            None,
        )
        if self_subject is None:
            return None

        facts: list[ReportProfileFact] = []
        truncated = False
        profile_version: int | None = None

        for subject in subjects:
            subject_id = subject.get("subjectId")
            kind = subject.get("kind")
            if not isinstance(subject_id, str) or not subject_id:
                continue
            cursor: str | None = None
            while True:
                page = await self._service.list_facts(
                    user_id,
                    subject_id=subject_id,
                    statuses=READ_STATUSES,
                    cursor=cursor,
                    limit=PAGE_LIMIT,
                )
                if kind == SELF_SUBJECT_KIND and profile_version is None:
                    version = page.get("profileVersion")
                    profile_version = version if isinstance(version, int) else None
                for item in page.get("items", []):
                    if len(facts) >= MAX_FACTS:
                        truncated = True
                        break
                    fact = _to_fact(item, subject_id=subject_id, subject_kind=kind)
                    if fact is not None:
                        facts.append(fact)
                cursor = page.get("nextCursor")
                if truncated or not cursor:
                    if cursor:
                        truncated = True
                    break
            if truncated:
                break

        return ReportProfileSnapshot(
            user_id=user_id,
            self_subject_id=str(self_subject.get("subjectId")),
            read_at=datetime.now(UTC).isoformat(),
            profile_version=profile_version,
            facts=tuple(facts),
            truncated=truncated,
            truncation_reason=(
                f"facts exceeded {MAX_FACTS}" if truncated else None
            ),
        )

    async def _profile_version(self, user_id: str) -> int | None:
        page = await self._service.list_facts(
            user_id,
            statuses=READ_STATUSES,
            limit=1,
        )
        version = page.get("profileVersion")
        return version if isinstance(version, int) else None


def _to_fact(
    item: dict[str, Any],
    *,
    subject_id: str,
    subject_kind: str,
) -> ReportProfileFact | None:
    fact_id = item.get("factId")
    fact_type = item.get("factType")
    if not isinstance(fact_id, str) or not isinstance(fact_type, str):
        return None
    period = item.get("effectivePeriod")
    period = period if isinstance(period, dict) else {}
    return ReportProfileFact(
        fact_id=fact_id,
        subject_id=subject_id,
        subject_kind=subject_kind,
        fact_type=fact_type,
        display_name=str(item.get("displayName") or fact_type),
        value=item.get("value"),
        value_type=str(item.get("valueType") or ""),
        assertion_level=str(item.get("assertionLevel") or ""),
        verification_status=str(item.get("verificationStatus") or ""),
        status=str(item.get("status") or ""),
        effective_start=_text(period.get("start")),
        effective_end=_text(period.get("end")),
        updated_at=_text(item.get("updatedAt")),
        sources=tuple(
            str(source.get("sourceType") or source.get("type"))
            for source in item.get("sources", [])
            if isinstance(source, dict) and (source.get("sourceType") or source.get("type"))
        ),
    )


def _mark_stale(snapshot: ReportProfileSnapshot) -> ReportProfileSnapshot:
    return ReportProfileSnapshot(
        user_id=snapshot.user_id,
        self_subject_id=snapshot.self_subject_id,
        read_at=snapshot.read_at,
        profile_version=snapshot.profile_version,
        facts=snapshot.facts,
        truncated=snapshot.truncated,
        truncation_reason=snapshot.truncation_reason,
        stale=True,
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


__all__ = ["HealthProfileSnapshotGateway"]
