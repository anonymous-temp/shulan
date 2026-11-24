"""按画像与解释条件持久化首份合格循证结果，使用分布式 lease 合并生成。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from agents.graphs.health_profile.focus import POLICY_VERSION
from agents.graphs.health_profile.interpretation import InterpretationCache
from agents.persistence.locks import AgentLeaseManager
from utils.log import getLogger

logger = getLogger(__name__)


def evidence_result_reusable(result):
    """部分通过校验的 ready 可固定；失败造成的 empty 不固定。"""
    evidence = result.get('evidence', {})
    if not isinstance(evidence, dict) or not isinstance(evidence.get('items'), list):
        return False
    return bool(
        (evidence.get('status') == 'ready' and evidence['items'])
        or (evidence.get('status') == 'empty' and not evidence['items'] and result.get('complete') is True)
    )


def evidence_revision(*, configuration, profiles):
    """仅保存配置与实际提示词的摘要，不包含凭据或进程对象标识。"""
    return _digest(['profile-evidence-snapshot-v1', POLICY_VERSION, configuration, profiles])


def evidence_key(*, user_id, subject_id, profile_version, facts, clinical_context, revision):
    return _digest(
        [
            'profile-evidence-snapshot-v1',
            revision,
            user_id,
            subject_id,
            profile_version,
            sorted(facts, key=lambda fact: fact['factId']),
            clinical_context,
        ]
    )


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MongoEvidenceResultStore:
    """独立派生集合；_id 唯一约束保证首次写入胜出，无自动过期。"""

    def __init__(self, collection):
        self.collection = collection
        self._indexed = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self):
        async with self._index_lock:
            if not self._indexed:
                await self.collection.create_index(
                    [('userId', 1), ('subjectId', 1), ('profileVersion', 1)],
                    name='profile_evidence_scope_version',
                )
                self._indexed = True

    async def read(self, *, key, user_id, subject_id):
        document = await self.collection.find_one({'_id': key, 'userId': user_id, 'subjectId': subject_id})
        if document is None:
            return None
        result = document.get('result')
        if (
            document.get('schemaVersion') != 1
            or not isinstance(result, dict)
            or not isinstance(result.get('insights'), list)
            or not isinstance(result.get('complete'), bool)
            or not evidence_result_reusable(result)
        ):
            raise ValueError('invalid stored profile evidence result')
        restored = deepcopy(result)
        for insight in restored['insights']:
            # BSON 将 tuple 存成 array；恢复内部排序类型，避免与当前事实的 tuple 比较失败。
            if isinstance(insight.get('rank'), list):
                insight['rank'] = tuple(insight['rank'])
        return restored

    async def save_if_absent(self, *, key, user_id, subject_id, profile_version, result):
        if not evidence_result_reusable(result):
            raise ValueError('profile evidence result is not reusable')
        saved = deepcopy({name: result[name] for name in ('evidence', 'insights', 'complete')})
        document = {
            '_id': key,
            'userId': user_id,
            'subjectId': subject_id,
            'profileVersion': profile_version,
            'schemaVersion': 1,
            'createdAt': datetime.now(timezone.utc),
            'result': saved,
        }
        try:
            inserted = await self.collection.insert_one(document)
            if not inserted.acknowledged:
                raise RuntimeError('profile evidence write not acknowledged')
        except DuplicateKeyError:
            winner = await self.read(key=key, user_id=user_id, subject_id=subject_id)
            if winner is None:
                raise RuntimeError('profile evidence key belongs to another scope') from None
            return winner
        return deepcopy(saved)


class ProfileEvidenceResults:
    """进程内合并 + Mongo 固定结果 + Redis 生成 lease；存储故障不返回未保存结果。"""

    def __init__(self, store, lease_manager, *, timeout_seconds=60.0):
        if not isinstance(lease_manager, AgentLeaseManager):
            raise TypeError('lease_manager must implement AgentLeaseManager')
        self.store = store
        self.leases = lease_manager
        self.timeout = timeout_seconds
        self.cache = InterpretationCache()

    async def get_or_create(self, *, key, user_id, subject_id, profile_version, factory):
        return await self.cache.get_or_create(
            key,
            lambda: self._durable_result(
                key=key,
                user_id=user_id,
                subject_id=subject_id,
                profile_version=profile_version,
                factory=factory,
            ),
        )

    async def _durable_result(self, *, key, user_id, subject_id, profile_version, factory):
        scope = {'key': key, 'user_id': user_id, 'subject_id': subject_id}
        async with asyncio.timeout(self.timeout):
            await self.store.ensure_indexes()
            owner = uuid4().hex
            while True:
                saved = await self.store.read(**scope)
                if saved is not None:
                    logger.info('Profile evidence snapshot hit key=%s status=%s', key[:12], saved['evidence']['status'])
                    return saved
                lease = await self.leases.try_acquire('profile-evidence:' + key, owner)
                if lease is not None:
                    break
                await asyncio.sleep(0.1)
            generation = None
            heartbeat = None
            try:
                # 在取得 lease 后重读，关闭读取与获取 lease 之间的竞态。
                saved = await self.store.read(**scope)
                if saved is not None:
                    return saved
                generation = asyncio.create_task(factory())
                heartbeat = asyncio.create_task(self._renew(lease))
                done, _ = await asyncio.wait((generation, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    await heartbeat
                    raise RuntimeError('profile evidence lease heartbeat stopped')
                result = await generation
                if not await self.leases.renew(lease):
                    raise RuntimeError('profile evidence generation lease lost')
                if evidence_result_reusable(result):
                    result = await self.store.save_if_absent(
                        **scope,
                        profile_version=profile_version,
                        result=result,
                    )
                    disposition = 'stored'
                else:
                    disposition = 'retryable'
                logger.info(
                    'Profile evidence snapshot %s key=%s status=%s',
                    disposition,
                    key[:12],
                    result['evidence']['status'],
                )
                return result
            finally:
                tasks = [task for task in (generation, heartbeat) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await self.leases.release(lease)
                except Exception as error:
                    logger.warning('Profile evidence lease release failed category=%s', type(error).__name__)

    async def _renew(self, lease):
        while True:
            await asyncio.sleep(min(10.0, lease.ttl_seconds / 3))
            if not await self.leases.renew(lease):
                raise RuntimeError('profile evidence generation lease lost')
