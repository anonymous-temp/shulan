"""Agent 会话的 Redis 分布式 lease。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from ..errors import AgentLeaseStorageError

_DEFAULT_KEY_PREFIX = 'shulan:agent:lock'

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""".strip()

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
""".strip()


class AsyncRedisLeaseClient(Protocol):
    """RedisAgentLeaseManager 使用的最小异步 Redis 接口。"""

    async def set(
            self,
            name: str,
            value: str,
            *,
            ex: int,
            nx: bool,
    ) -> Any:
        ...

    async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_args: str | int,
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class AgentLease:
    """一次已获得的会话执行权，只能由相同 owner 续期或释放。"""

    key: str
    owner_id: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        _require_text('key', self.key)
        _require_text('owner_id', self.owner_id)
        _require_ttl(self.ttl_seconds)


@runtime_checkable
class AgentLeaseManager(Protocol):
    """AgentHub 后续依赖的分布式会话 lease 边界。"""

    async def try_acquire(
            self,
            thread_id: str,
            owner_id: str,
    ) -> AgentLease | None:
        ...

    async def renew(self, lease: AgentLease) -> bool:
        ...

    async def release(self, lease: AgentLease) -> bool:
        ...


class RedisAgentLeaseManager:
    """通过 Redis 原子命令串行化同一个 Agent thread 的运行。"""

    def __init__(
            self,
            client: AsyncRedisLeaseClient,
            *,
            ttl_seconds: int,
            key_prefix: str = _DEFAULT_KEY_PREFIX,
    ) -> None:
        for method_name in ('set', 'eval'):
            if not callable(getattr(client, method_name, None)):
                raise TypeError(
                    'client must provide async set and eval methods',
                )
        _require_ttl(ttl_seconds)
        _require_text('key_prefix', key_prefix)
        normalized_prefix = key_prefix.strip().rstrip(':')
        _require_text('key_prefix', normalized_prefix)
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._key_prefix = normalized_prefix

    async def try_acquire(
            self,
            thread_id: str,
            owner_id: str,
    ) -> AgentLease | None:
        """尝试获得 thread；已被其他运行持有时返回 None。"""
        _require_text('thread_id', thread_id)
        _require_text('owner_id', owner_id)
        key = self._key(thread_id)
        try:
            result = await self._client.set(
                key,
                owner_id,
                ex=self._ttl_seconds,
                nx=True,
            )
        except Exception as error:
            raise AgentLeaseStorageError(
                f'Cannot acquire Agent lease {key!r}',
            ) from error

        if result is None or result is False:
            return None
        if result is not True:
            raise AgentLeaseStorageError(
                'Redis SET NX returned an unsupported result',
            )
        return AgentLease(
            key=key,
            owner_id=owner_id,
            ttl_seconds=self._ttl_seconds,
        )

    async def renew(self, lease: AgentLease) -> bool:
        """仅当 lease 仍由相同 owner 持有时刷新 TTL。"""
        self._require_managed_lease(lease)
        try:
            result = await self._client.eval(
                _RENEW_SCRIPT,
                1,
                lease.key,
                lease.owner_id,
                self._ttl_seconds,
            )
        except Exception as error:
            raise AgentLeaseStorageError(
                f'Cannot renew Agent lease {lease.key!r}',
            ) from error
        return _redis_boolean(result, operation='renew')

    async def release(self, lease: AgentLease) -> bool:
        """仅当 lease 仍由相同 owner 持有时删除 Redis 键。"""
        self._require_managed_lease(lease)
        try:
            result = await self._client.eval(
                _RELEASE_SCRIPT,
                1,
                lease.key,
                lease.owner_id,
            )
        except Exception as error:
            raise AgentLeaseStorageError(
                f'Cannot release Agent lease {lease.key!r}',
            ) from error
        return _redis_boolean(result, operation='release')

    def _key(self, thread_id: str) -> str:
        digest = sha256(thread_id.encode('utf-8')).hexdigest()
        return f'{self._key_prefix}:{digest}'

    def _require_managed_lease(self, lease: AgentLease) -> None:
        _require_lease(lease)
        if not lease.key.startswith(f'{self._key_prefix}:'):
            raise ValueError('lease does not belong to this manager')


def _redis_boolean(value: Any, *, operation: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise AgentLeaseStorageError(
        f'Redis lease {operation} returned an unsupported result',
    )


def _require_lease(value: AgentLease) -> None:
    if not isinstance(value, AgentLease):
        raise TypeError('lease must be an AgentLease')


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if not value.strip():
        raise ValueError(f'{name} cannot be empty')


def _require_ttl(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('ttl_seconds must be an integer')
    if value <= 0:
        raise ValueError('ttl_seconds must be greater than zero')
