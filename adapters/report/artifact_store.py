"""MongoDB store for final report batches and their five artifacts.

幂等键是文档 ``_id``：``"<finalReportId>:<reportType>"``。首次写入直接
``insert_one``；重复写入由唯一索引拒绝后比对 ``contentHash``，相同即幂等
成功，不同则保留原件并明确报告冲突。

批次文档由持久化受理流程创建；五类产物的正文及单类状态以产物文档为准。
回调投递状态位于独立集合，保存后的入队间隙由批次补偿扫描恢复。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import UTC
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from modules.report.ports import ReportArtifactRecord
from utils.log import getLogger

logger = getLogger(__name__)

SAVED = "saved"
IDEMPOTENT = "idempotent"
CONFLICT = "conflict"
FAILED = "failed"

# 只重试存储，不重跑解析或模型。
SAVE_ATTEMPTS = 3
SAVE_BACKOFF_SECONDS = (0.2, 0.6)

MAX_LIST_LIMIT = 100


class MongoReportArtifactStore:
    """最终报告的持久化实现；连接与生命周期由既有共享客户端管理。"""

    def __init__(self, *, batches: Any, artifacts: Any, workflow: Any = None) -> None:
        self._batches = batches
        self._artifacts = artifacts
        self.workflow = workflow
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self._artifacts.create_index(
                [("userId", 1), ("finalReportId", 1)],
                name="report_artifact_batch",
            )
            await self._artifacts.create_index(
                [("userId", 1), ("savedAt", -1)],
                name="report_artifact_recent",
            )
            await self._batches.create_index(
                [("userId", 1), ("createdAt", -1)],
                name="report_batch_recent",
            )
            await self._batches.create_index(
                [("userId", 1), ("sessionId", 1), ("createdAt", -1)],
                name="report_batch_session",
            )
            self._indexes_ready = True

    async def save_artifact(
        self,
        record: ReportArtifactRecord,
        *,
        subject_id: str | None = None,
    ) -> str:
        document_id = _artifact_id(record.final_report_id, record.report_type)
        last_error: Exception | None = None
        for attempt in range(SAVE_ATTEMPTS):
            try:
                if self.workflow is not None and not await self.workflow.allow_artifact_write(record.final_report_id):
                    return CONFLICT
                await self.ensure_indexes()
                await self._upsert_batch(record, subject_id=subject_id)
                await self._artifacts.insert_one({
                    "_id": document_id,
                    "finalReportId": record.final_report_id,
                    "userId": record.user_id,
                    "sessionId": record.session_id,
                    "reportType": record.report_type,
                    "generationStatus": record.generation_status,
                    "reviewStatus": record.review_status,
                    "contentHash": record.content_hash,
                    "savedAt": datetime.now(UTC),
                    "schemaVersion": record.schema_version,
                    "body": record.body,
                    "documents": list(record.documents),
                })
                await self._enqueue_saved(record)
                return SAVED
            except DuplicateKeyError:
                existing = await self._artifacts.find_one(
                    {"_id": document_id},
                    {"contentHash": 1},
                )
                if existing and existing.get("contentHash") == record.content_hash:
                    await self._enqueue_saved(record)
                    return IDEMPOTENT
                # 同批次同类型已有不同正文：保留原件，不覆盖。
                logger.warning(
                    "report artifact conflict: final_report_id=%s type=%s",
                    record.final_report_id,
                    record.report_type,
                )
                return CONFLICT
            except Exception as error:
                last_error = error
                if attempt + 1 < SAVE_ATTEMPTS:
                    delay = SAVE_BACKOFF_SECONDS[
                        min(attempt, len(SAVE_BACKOFF_SECONDS) - 1)
                    ]
                    await asyncio.sleep(delay)
        logger.warning(
            "report artifact save failed: final_report_id=%s type=%s error_type=%s",
            record.final_report_id,
            record.report_type,
            type(last_error).__name__,
        )
        return FAILED

    async def _enqueue_saved(self, record: ReportArtifactRecord) -> None:
        if self.workflow is not None:
            try:
                await self.workflow.ensure_delivery(record.final_report_id, record.report_type)
            except Exception as error:
                logger.warning('报告回调入队待补偿: final_report_id=%s type=%s error_type=%s',
                               record.final_report_id, record.report_type, type(error).__name__)

    async def get_delivery(self, *, user_id: str, final_report_id: str, report_type: str) -> dict:
        if self.workflow is None:
            return {'status': 'unknown', 'attempts': 0, 'nextRetryAt': None,
                    'deliveredAt': None, 'lastError': None}
        return await self.workflow.get_delivery(user_id=user_id, final_report_id=final_report_id,
                                                report_type=report_type)

    async def get_artifact(
        self,
        *,
        user_id: str,
        final_report_id: str,
        report_type: str,
    ) -> ReportArtifactRecord | None:
        document = await self._artifacts.find_one({
            "_id": _artifact_id(final_report_id, report_type),
            # 归属校验：错误引用不回退到其他用户的报告。
            "userId": user_id,
        })
        if not document:
            return None
        return ReportArtifactRecord(
            final_report_id=document["finalReportId"],
            user_id=document["userId"],
            session_id=document.get("sessionId", ""),
            report_type=document["reportType"],
            generation_status=document.get("generationStatus", ""),
            review_status=document.get("reviewStatus", ""),
            content_hash=document.get("contentHash", ""),
            body=document.get("body") or {},
            schema_version=int(document.get("schemaVersion") or 1),
            documents=tuple(document.get("documents") or ()),
        )

    async def list_batches(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_indexes()
        bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
        query: dict[str, Any] = {"userId": user_id}
        if session_id:
            query["sessionId"] = session_id
        after = _decode_cursor(cursor)
        if after is not None:
            query["createdAt"] = {"$lt": after}
        found = await (
            self._batches.find(query)
            .sort("createdAt", -1)
            .limit(bounded + 1)
            .to_list(length=bounded + 1)
        )
        page = found[:bounded]
        next_cursor = (
            _encode_cursor(page[-1]["createdAt"])
            if len(found) > bounded and page
            else None
        )
        items = []
        for batch in page:
            items.append({
                "finalReportId": batch["finalReportId"],
                "sessionId": batch.get("sessionId"),
                "subjectId": batch.get("subjectId"),
                "createdAt": _isoformat(batch.get("createdAt")),
                "generationStatus": batch.get("generationStatus", "unknown"),
                "documents": batch.get("documents") or [],
                # 五类状态由产物文档派生，避免批次与产物双写不一致。
                "artifacts": await self._artifact_states(
                    user_id,
                    batch["finalReportId"],
                ),
            })
        return {"userId": user_id, "items": items, "nextCursor": next_cursor}

    async def _artifact_states(
        self,
        user_id: str,
        final_report_id: str,
    ) -> list[dict[str, Any]]:
        documents = await (
            self._artifacts.find(
                {"userId": user_id, "finalReportId": final_report_id},
                {"body": 0},
            )
            .to_list(length=len(_REPORT_TYPES))
        )
        by_type = {item["reportType"]: item for item in documents}
        states = []
        for report_type in _REPORT_TYPES:
            document = by_type.get(report_type)
            if document is None:
                states.append({"reportType": report_type, "state": "absent",
                               "delivery": await self.get_delivery(
                                   user_id=user_id, final_report_id=final_report_id, report_type=report_type)})
                continue
            states.append({
                "reportType": report_type,
                "state": "saved",
                "generationStatus": document.get("generationStatus"),
                "reviewStatus": document.get("reviewStatus"),
                "savedAt": _isoformat(document.get("savedAt")),
                "delivery": await self.get_delivery(
                    user_id=user_id, final_report_id=final_report_id, report_type=report_type),
            })
        return states

    async def _upsert_batch(
        self,
        record: ReportArtifactRecord,
        *,
        subject_id: str | None,
    ) -> None:
        now = datetime.now(UTC)
        update: dict[str, Any] = {
            "$setOnInsert": {
                "finalReportId": record.final_report_id,
                "userId": record.user_id,
                "sessionId": record.session_id,
                "createdAt": now,
                "schemaVersion": record.schema_version,
            },
        }
        if record.documents:
            update['$set'] = {'documents': list(record.documents)}
        else:
            update['$setOnInsert']['documents'] = []
        if subject_id:
            update.setdefault('$set', {})['subjectId'] = subject_id
        await self._batches.update_one(
            {"_id": record.final_report_id},
            update,
            upsert=True,
        )


# [已停用] intervene 干预方案报告暂不生成；恢复时改回下面这行
# _REPORT_TYPES = ("evaluate", "intervene", "instruction", "opinion", "explain")
_REPORT_TYPES = ("evaluate", "instruction", "opinion", "explain", "explain_gutBarrier")


def _artifact_id(final_report_id: str, report_type: str) -> str:
    return f"{final_report_id}:{report_type}"


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _encode_cursor(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    payload = json.dumps({"createdAt": value.isoformat()}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return datetime.fromisoformat(payload["createdAt"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None


__all__ = ["MongoReportArtifactStore"]
