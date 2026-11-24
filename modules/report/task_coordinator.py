"""Process-local report task coalescing and bounded completed-result reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.schemas import ReportRequest

REPORT_CONTENT_REVISION = "evaluate-v2.0-content-20261124-source-led-review-v53"

@dataclass(frozen=True)
class ReportExecutionResult:
    evaluate_payload: dict[str, Any]
    followup_payloads: tuple[dict[str, Any], ...] = ()
    module_statuses: dict[str, dict[str, str]] | None = None


@dataclass
class _TaskEntry:
    task: asyncio.Task[ReportExecutionResult]
    completed_at: float | None = None
    result: ReportExecutionResult | None = None


def report_request_fingerprint(request: ReportRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json", by_alias=True, exclude_none=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = (
        f"report-api-v1|evaluate.v2.0|{REPORT_CONTENT_REVISION}|{EXTRACTOR_VERSION}|fallback-v1|{canonical}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


class ReportTaskCoordinator:
    """Coalesce identical tasks without changing the public API contract.

    Completed results are retained for a short fixed TTL and replayed through
    the ordinary delivery path.  This is intentionally dependency-free and
    remains process-local; it does not use Redis or another shared cache.
    """

    def __init__(
        self,
        *,
        policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
    ) -> None:
        self.policy = policy
        self._entries: dict[str, _TaskEntry] = {}
        self._batches: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def acquire_batch(self, fingerprint: str) -> str:
        """在受理响应之前确定批次身份。

        运行中的相同规范化请求与短期完成结果重投复用原 finalReportId；
        条目过期后真正重新生成，分配新批次并保留旧报告。因此批次号不能
        由 fingerprint 直接推导，只能按当前存活的条目查表。
        """
        self._remove_expired_locked()
        existing = self._batches.get(fingerprint)
        if existing is not None:
            return existing
        final_report_id = f"rpt_{uuid4().hex}"
        self._batches[fingerprint] = final_report_id
        return final_report_id

    async def execute(
        self,
        fingerprint: str,
        *,
        runner: Callable[[], Awaitable[ReportExecutionResult]],
        replay: Callable[[ReportExecutionResult], Awaitable[None]],
    ) -> ReportExecutionResult:
        owner = False
        cached: ReportExecutionResult | None = None
        async with self._lock:
            self._remove_expired_locked()
            entry = self._entries.get(fingerprint)
            if entry is None:
                task = asyncio.create_task(
                    runner(),
                    name=f"report:{fingerprint[:12]}",
                )
                entry = _TaskEntry(task=task)
                self._entries[fingerprint] = entry
                owner = True
            elif entry.result is not None:
                cached = entry.result
            task = entry.task

        if cached is not None:
            await replay(cached)
            return cached

        result = await asyncio.shield(task)
        if owner:
            async with self._lock:
                current = self._entries.get(fingerprint)
                if current is entry:
                    current.result = result
                    current.completed_at = time.monotonic()
                    self._remove_expired_locked()
        return result

    def _remove_expired_locked(self) -> None:
        now = time.monotonic()
        ttl = max(0.0, self.policy.completed_result_ttl_seconds)
        for key, entry in list(self._entries.items()):
            if entry.task.cancelled() or (
                entry.task.done() and entry.task.exception() is not None
            ) or (
                entry.completed_at is not None
                and now - entry.completed_at > ttl
            ):
                self._entries.pop(key, None)
                self._batches.pop(key, None)
        maximum = max(0, int(self.policy.completed_result_max_entries))
        completed = sorted(
            (
                (entry.completed_at, key)
                for key, entry in self._entries.items()
                if entry.completed_at is not None
            ),
            key=lambda item: item[0],
        )
        for _completed_at, key in completed[: max(0, len(completed) - maximum)]:
            self._entries.pop(key, None)
            self._batches.pop(key, None)
