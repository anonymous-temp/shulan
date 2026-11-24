"""应用生命周期状态与入站请求排空协调。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from itertools import count


class LifecycleState(str, Enum):
    """应用对流量和关闭流程可见的生命周期状态。"""

    STARTING = 'starting'
    READY = 'ready'
    DRAINING = 'draining'
    STOPPED = 'stopped'


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    state: LifecycleState
    active_requests: int

    @property
    def ready(self) -> bool:
        return self.state is LifecycleState.READY


class RequestLease:
    """一个已进入 Handler 的请求租约；释放操作幂等。"""

    __slots__ = ('_lifecycle', '_token')

    def __init__(self, lifecycle: ApplicationLifecycle, token: int) -> None:
        self._lifecycle = lifecycle
        self._token: int | None = token

    def release(self) -> None:
        token = self._token
        if token is None:
            return
        self._token = None
        self._lifecycle._release_request(token)


class ApplicationLifecycle:
    """在 IOLoop 单线程内原子管理 readiness 与活跃请求。"""

    def __init__(self) -> None:
        self._state = LifecycleState.STARTING
        self._request_tokens: set[int] = set()
        self._request_token_sequence = count(1)
        self._requests_idle = asyncio.Event()
        self._requests_idle.set()

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state is LifecycleState.READY

    @property
    def active_request_count(self) -> int:
        return len(self._request_tokens)

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            state=self._state,
            active_requests=self.active_request_count,
        )

    def mark_ready(self) -> None:
        if self._state is LifecycleState.READY:
            return
        if self._state is not LifecycleState.STARTING:
            raise RuntimeError(
                f'cannot mark lifecycle ready from {self._state.value}',
            )
        self._state = LifecycleState.READY

    def try_begin_request(self) -> RequestLease | None:
        """登记新业务请求；非 READY 状态拒绝登记。"""
        if self._state is not LifecycleState.READY:
            return None
        token = next(self._request_token_sequence)
        self._request_tokens.add(token)
        self._requests_idle.clear()
        return RequestLease(self, token)

    def begin_draining(self) -> bool:
        """进入排空状态；只有第一次调用返回 ``True``。"""
        if self._state in (LifecycleState.DRAINING, LifecycleState.STOPPED):
            return False
        self._state = LifecycleState.DRAINING
        return True

    async def wait_for_requests(self) -> None:
        await self._requests_idle.wait()

    def mark_stopped(self) -> None:
        if self._state is LifecycleState.STOPPED:
            return
        if self._state is not LifecycleState.DRAINING:
            raise RuntimeError(
                f'cannot mark lifecycle stopped from {self._state.value}',
            )
        if self._request_tokens:
            raise RuntimeError('cannot stop while requests are active')
        self._state = LifecycleState.STOPPED

    def _release_request(self, token: int) -> None:
        self._request_tokens.discard(token)
        if not self._request_tokens:
            self._requests_idle.set()


__all__ = (
    'ApplicationLifecycle',
    'LifecycleSnapshot',
    'LifecycleState',
    'RequestLease',
)
