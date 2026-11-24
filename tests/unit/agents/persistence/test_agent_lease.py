"""Redis Agent 会话 lease 的无网络原子所有权测试。"""

from __future__ import annotations

import unittest
from hashlib import sha256

from agents.errors import AgentLeaseStorageError
from agents.persistence import AgentLease
from agents.persistence import RedisAgentLeaseManager


class FakeAsyncRedis:
    """模拟 lease 使用的 SET NX EX 与单键 Lua 操作。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.set_calls: list[dict] = []
        self.eval_calls: list[dict] = []
        self.failure: Exception | None = None

    async def set(self, name, value, *, ex, nx):
        self.set_calls.append({
            'name': name,
            'value': value,
            'ex': ex,
            'nx': nx,
        })
        if self.failure is not None:
            raise self.failure
        if nx and name in self.values:
            return None
        self.values[name] = value
        self.ttls[name] = ex
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        self.eval_calls.append({
            'script': script,
            'numkeys': numkeys,
            'args': keys_and_args,
        })
        if self.failure is not None:
            raise self.failure
        key = keys_and_args[0]
        owner_id = keys_and_args[1]
        if self.values.get(key) != owner_id:
            return 0
        if 'EXPIRE' in script:
            self.ttls[key] = keys_and_args[2]
            return 1
        del self.values[key]
        del self.ttls[key]
        return 1


class RedisAgentLeaseManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.redis = FakeAsyncRedis()
        self.manager = RedisAgentLeaseManager(
            self.redis,
            ttl_seconds=90,
        )

    async def test_acquire_uses_hashed_key_set_nx_and_ttl(self) -> None:
        thread_id = 'chat:user-1:session-1'

        lease = await self.manager.try_acquire(thread_id, 'run-1')

        expected_key = (
            'shulan:agent:lock:'
            f'{sha256(thread_id.encode("utf-8")).hexdigest()}'
        )
        self.assertEqual(
            lease,
            AgentLease(
                key=expected_key,
                owner_id='run-1',
                ttl_seconds=90,
            ),
        )
        self.assertNotIn(thread_id, lease.key)
        self.assertEqual(self.redis.set_calls, [{
            'name': expected_key,
            'value': 'run-1',
            'ex': 90,
            'nx': True,
        }])

    async def test_same_thread_cannot_be_acquired_by_second_owner(self) -> None:
        first = await self.manager.try_acquire('thread-1', 'run-1')
        second = await self.manager.try_acquire('thread-1', 'run-2')

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_different_threads_can_be_acquired_concurrently(self) -> None:
        first = await self.manager.try_acquire('thread-1', 'run-1')
        second = await self.manager.try_acquire('thread-2', 'run-2')

        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(self.redis.values), 2)

    async def test_only_owner_can_renew_lease(self) -> None:
        lease = await self.manager.try_acquire('thread-1', 'run-1')
        foreign = AgentLease(
            key=lease.key,
            owner_id='run-2',
            ttl_seconds=30,
        )
        self.redis.ttls[lease.key] = 1

        self.assertFalse(await self.manager.renew(foreign))
        self.assertEqual(self.redis.ttls[lease.key], 1)
        self.assertTrue(await self.manager.renew(lease))
        self.assertEqual(self.redis.ttls[lease.key], 90)

    async def test_only_owner_can_release_lease(self) -> None:
        lease = await self.manager.try_acquire('thread-1', 'run-1')
        foreign = AgentLease(
            key=lease.key,
            owner_id='run-2',
            ttl_seconds=90,
        )

        self.assertFalse(await self.manager.release(foreign))
        self.assertIn(lease.key, self.redis.values)
        self.assertTrue(await self.manager.release(lease))
        self.assertNotIn(lease.key, self.redis.values)

    async def test_expired_lease_cannot_be_renewed_or_released(self) -> None:
        lease = await self.manager.try_acquire('thread-1', 'run-1')
        del self.redis.values[lease.key]
        del self.redis.ttls[lease.key]

        self.assertFalse(await self.manager.renew(lease))
        self.assertFalse(await self.manager.release(lease))

    async def test_redis_failure_is_wrapped_without_losing_cause(self) -> None:
        self.redis.failure = ConnectionError('redis unavailable')

        with self.assertRaises(AgentLeaseStorageError) as raised:
            await self.manager.try_acquire('thread-1', 'run-1')

        self.assertIsInstance(raised.exception.__cause__, ConnectionError)

    async def test_input_is_validated_before_redis_access(self) -> None:
        with self.assertRaisesRegex(ValueError, 'thread_id cannot be empty'):
            await self.manager.try_acquire(' ', 'run-1')
        with self.assertRaisesRegex(ValueError, 'owner_id cannot be empty'):
            await self.manager.try_acquire('thread-1', ' ')
        with self.assertRaisesRegex(TypeError, 'lease must be an AgentLease'):
            await self.manager.release(None)
        with self.assertRaisesRegex(
                ValueError,
                'lease does not belong to this manager',
        ):
            await self.manager.release(AgentLease(
                key='other:agent:lock:digest',
                owner_id='run-1',
                ttl_seconds=90,
            ))

        self.assertEqual(self.redis.set_calls, [])
        self.assertEqual(self.redis.eval_calls, [])


class RedisAgentLeaseManagerConfigurationTest(
    unittest.IsolatedAsyncioTestCase,
):
    def test_rejects_invalid_ttl_or_client(self) -> None:
        with self.assertRaisesRegex(ValueError, 'greater than zero'):
            RedisAgentLeaseManager(FakeAsyncRedis(), ttl_seconds=0)
        with self.assertRaisesRegex(TypeError, 'must be an integer'):
            RedisAgentLeaseManager(FakeAsyncRedis(), ttl_seconds=True)
        with self.assertRaisesRegex(TypeError, 'async set and eval'):
            RedisAgentLeaseManager(object(), ttl_seconds=30)
        with self.assertRaisesRegex(ValueError, 'key_prefix cannot be empty'):
            RedisAgentLeaseManager(
                FakeAsyncRedis(),
                ttl_seconds=30,
                key_prefix=' : ',
            )

    async def test_normalizes_custom_key_prefix(self) -> None:
        redis = FakeAsyncRedis()
        manager = RedisAgentLeaseManager(
            redis,
            ttl_seconds=30,
            key_prefix=' custom:agent:lock: ',
        )

        lease = await manager.try_acquire('thread-1', 'run-1')
        self.assertTrue(lease.key.startswith('custom:agent:lock:'))


if __name__ == '__main__':
    unittest.main()
