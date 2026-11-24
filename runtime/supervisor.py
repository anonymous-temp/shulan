"""进程内后台任务准入、追踪与排空。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from utils.exceptions import format_exception_summary
from utils.log import getLogger
from utils.log.context import bind_log_context

logger = getLogger(__name__)


class TaskAdmissionClosedError(RuntimeError):
    """应用排空后拒绝登记新的后台任务。"""


@dataclass(frozen=True, slots=True)
class SupervisedTaskSnapshot:
    name: str
    running_seconds: float


class TaskSupervisor:
    """追踪 fire-and-forget 协程，关闭时只等待、不主动取消。"""

    def __init__(self) -> None:
        self._accepting = True
        self._tasks: dict[asyncio.Task[Any], tuple[str, float]] = {}
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_task_count(self) -> int:
        return len(self._tasks)

    def snapshots(self) -> tuple[SupervisedTaskSnapshot, ...]:
        now = time.monotonic()
        return tuple(
            SupervisedTaskSnapshot(
                name=name,
                running_seconds=max(0.0, now - started_at),
            )
            for name, started_at in sorted(
                self._tasks.values(),
                key=lambda item: item[0],
            )
        )

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        normalized_name = name.strip() if isinstance(name, str) else ''
        if not normalized_name:
            coroutine.close()
            raise ValueError('task name must be a non-empty string')
        if not self._accepting:
            coroutine.close()
            raise TaskAdmissionClosedError(
                'application is draining and no longer accepts background tasks',
            )
        with bind_log_context(task_id=normalized_name):
            task = asyncio.create_task(coroutine, name=normalized_name)
            task.add_done_callback(self._task_finished)
        self._tasks[task] = (normalized_name, time.monotonic())
        self._idle.clear()
        return task

    def stop_accepting(self) -> None:
        self._accepting = False

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        name, _ = self._tasks.pop(
            task,
            (task.get_name(), time.monotonic()),
        )
        if not self._tasks:
            self._idle.set()
        if task.cancelled():
            logger.warning('supervised task was cancelled: name=%s', name)
            return
        error = task.exception()
        if error is not None:
            logger.error(
                'supervised task failed: name=%s %s',
                name,
                format_exception_summary(error),
            )


__all__ = (
    'SupervisedTaskSnapshot',
    'TaskAdmissionClosedError',
    'TaskSupervisor',
)
