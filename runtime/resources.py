"""类型化进程资源登记、逆序关闭与失败聚合。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeVar, runtime_checkable


@runtime_checkable
class SupportsClose(Protocol):
    """提供同步关闭方法的资源。"""

    def close(self) -> None:
        ...


@runtime_checkable
class SupportsAsyncClose(Protocol):
    """提供异步关闭方法的资源。"""

    def aclose(self) -> Awaitable[None]:
        ...


SyncResourceT = TypeVar('SyncResourceT', bound=SupportsClose)
AsyncResourceT = TypeVar('AsyncResourceT', bound=SupportsAsyncClose)


class ResourceRegistryClosedError(RuntimeError):
    """资源登记表已进入或完成关闭，不能再登记资源。"""


class AsyncCloseRequiredError(RuntimeError):
    """同步关闭不能安全处理异步资源。"""

    def __init__(self, resource_names: tuple[str, ...]) -> None:
        self.resource_names = resource_names
        super().__init__(
            'async resources require awaiting ResourceRegistry.aclose(): '
            + ', '.join(resource_names),
        )


@dataclass(frozen=True, slots=True)
class ResourceCloseFailure:
    """单项资源关闭失败，保留资源名和原始异常。"""

    resource_name: str
    error: Exception


class ResourceCloseError(RuntimeError):
    """一轮资源关闭中收集到的全部失败。"""

    def __init__(self, failures: tuple[ResourceCloseFailure, ...]) -> None:
        self.failures = failures
        names = ', '.join(failure.resource_name for failure in failures)
        super().__init__(
            f'failed to close {len(failures)} resource(s): {names}',
        )


class _CloseKind(str, Enum):
    SYNC = 'sync'
    ASYNC = 'async'


class _RegistryState(str, Enum):
    OPEN = 'open'
    CLOSING = 'closing'
    CLOSED = 'closed'


@dataclass(frozen=True, slots=True)
class _ResourceEntry:
    resource: object
    name: str
    closer: Callable[[], Any]
    kind: _CloseKind


class ResourceRegistry:
    """拥有进程资源，并按照登记顺序的逆序至多关闭一次。

    同步 ``close`` 在存在异步资源时不会关闭任何资源，而是要求调用方
    改用 ``aclose``。异步 ``aclose`` 可以同时处理同步和异步资源。
    每轮关闭会尝试全部资源，最后以 ``ResourceCloseError`` 聚合失败。
    """

    def __init__(self) -> None:
        self._entries: list[_ResourceEntry] = []
        self._state = _RegistryState.OPEN

    @property
    def closed(self) -> bool:
        return self._state is _RegistryState.CLOSED

    @property
    def resource_count(self) -> int:
        return len(self._entries)

    def __contains__(self, resource: object) -> bool:
        return any(entry.resource is resource for entry in self._entries)

    def register_sync(
            self,
            resource: SyncResourceT,
            *,
            name: str | None = None,
    ) -> SyncResourceT:
        """登记具有 ``close`` 方法的同步资源并原样返回。"""
        closer = getattr(resource, 'close', None)
        if not callable(closer):
            raise TypeError('sync resource must provide a callable close method')
        self._register(
            resource,
            closer=closer,
            kind=_CloseKind.SYNC,
            name=name,
        )
        return resource

    def register_async(
            self,
            resource: AsyncResourceT,
            *,
            name: str | None = None,
    ) -> AsyncResourceT:
        """登记具有 ``aclose`` 方法的异步资源并原样返回。"""
        closer = getattr(resource, 'aclose', None)
        if not callable(closer):
            raise TypeError(
                'async resource must provide a callable aclose method',
            )
        self._register(
            resource,
            closer=closer,
            kind=_CloseKind.ASYNC,
            name=name,
        )
        return resource

    def close(self) -> None:
        """逆序关闭全部同步资源；重复调用为空操作。"""
        if self._state is _RegistryState.CLOSED:
            return
        self._require_not_closing()
        async_names = tuple(
            entry.name
            for entry in reversed(self._entries)
            if entry.kind is _CloseKind.ASYNC
        )
        if async_names:
            raise AsyncCloseRequiredError(async_names)
        failures = self._begin_close()
        for entry in reversed(self._entries):
            try:
                entry.closer()
            except Exception as error:
                failures.append(ResourceCloseFailure(entry.name, error))
        self._finish_close(failures)

    async def aclose(self) -> None:
        """逆序关闭同步及异步资源；重复调用为空操作。"""
        if self._state is _RegistryState.CLOSED:
            return
        self._require_not_closing()
        failures = self._begin_close()
        for entry in reversed(self._entries):
            try:
                result = entry.closer()
                if entry.kind is _CloseKind.ASYNC:
                    if not inspect.isawaitable(result):
                        raise TypeError(
                            f'async resource {entry.name!r} returned '
                            'a non-awaitable from aclose',
                        )
                    await result
            except Exception as error:
                failures.append(ResourceCloseFailure(entry.name, error))
        self._finish_close(failures)

    def _register(
            self,
            resource: object,
            *,
            closer: Callable[[], Any],
            kind: _CloseKind,
            name: str | None,
    ) -> None:
        if self._state is not _RegistryState.OPEN:
            raise ResourceRegistryClosedError(
                'cannot register resources after closing has started',
            )
        normalized_name = name.strip() if isinstance(name, str) else ''
        if name is not None and not normalized_name:
            raise ValueError('resource name must be a non-empty string')
        self._entries.append(_ResourceEntry(
            resource=resource,
            name=normalized_name or type(resource).__qualname__,
            closer=closer,
            kind=kind,
        ))

    def _require_not_closing(self) -> None:
        if self._state is _RegistryState.CLOSING:
            raise ResourceRegistryClosedError(
                'resource closing is already in progress',
            )

    def _begin_close(self) -> list[ResourceCloseFailure]:
        self._state = _RegistryState.CLOSING
        return []

    def _finish_close(
            self,
            failures: list[ResourceCloseFailure],
    ) -> None:
        self._entries.clear()
        self._state = _RegistryState.CLOSED
        if failures:
            raise ResourceCloseError(tuple(failures))


__all__ = (
    'AsyncCloseRequiredError',
    'ResourceCloseError',
    'ResourceCloseFailure',
    'ResourceRegistry',
    'ResourceRegistryClosedError',
    'SupportsAsyncClose',
    'SupportsClose',
)
