"""派生循证快照的重启、并发、失效及故障测试；不连接真实 Mongo/Redis。"""

import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest
from bson import BSON

from agents.persistence.locks import AgentLease
from health_profile.evidence_results import MongoEvidenceResultStore
from health_profile.evidence_results import ProfileEvidenceResults
from health_profile.evidence_results import evidence_key
from health_profile.evidence_results import evidence_revision
from health_profile.models import FactStatus
from health_profile.service import HealthProfileService
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioKnowledge
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import ScenarioLLM
from tests.unit.agents.graphs.health_profile.test_profile_interpretation import runtime_hub
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection
from tests.unit.health_profile.test_specialty_projection import setup_case


class BsonCollection(FakeCollection):
    """模拟 Mongo 的真实数组和时间序列化行为，不能只靠 deepcopy 假存储。"""

    async def insert_one(self, document):
        return await super().insert_one(BSON.encode(document).decode())


class SharedLeases:
    def __init__(self):
        self.owners = {}
        self.renewals = 0
        self.lost = False

    async def try_acquire(self, thread_id, owner_id):
        if thread_id in self.owners:
            return None
        self.owners[thread_id] = owner_id
        return AgentLease(key=thread_id, owner_id=owner_id, ttl_seconds=1)

    async def renew(self, lease):
        self.renewals += 1
        return not self.lost and self.owners.get(lease.key) == lease.owner_id

    async def release(self, lease):
        if self.owners.get(lease.key) != lease.owner_id:
            return False
        del self.owners[lease.key]
        return True


def result(status='ready', *, complete=True):
    return {
        'evidence': {
            'status': status,
            'items': [{'evidenceId': 'evidence_example', 'conclusion': '合成解释'}] if status == 'ready' else [],
            'generatedAt': '2026-09-30T00:00:00Z',
            'knowledgeBaseRevision': 'synthetic-v1',
        },
        'insights': [],
        'complete': complete,
    }


def key_args():
    return {
        'user_id': '001',
        'subject_id': 'subject-001',
        'profile_version': 1,
        'facts': [{'factId': 'a', 'value': 3}, {'factId': 'b', 'value': 4}],
        'clinical_context': {'age': 35},
        'revision': 'rules-v1',
    }


def options(**overrides):
    args = key_args()
    args.update(overrides)
    return {
        'key': evidence_key(**args),
        'user_id': args['user_id'],
        'subject_id': args['subject_id'],
        'profile_version': args['profile_version'],
    }


def coordinator(collection, leases, **kwargs):
    return ProfileEvidenceResults(MongoEvidenceResultStore(collection), leases, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,complete', [('ready', True), ('ready', False), ('empty', True)])
async def test_result_survives_restart_and_local_expiration(status, complete):
    collection, leases = FakeCollection(), SharedLeases()
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        return result(status, complete=complete)

    first_instance = coordinator(collection, leases)
    first = await first_instance.get_or_create(**options(), factory=generate)
    first_instance.cache.entries.clear()
    assert await first_instance.get_or_create(**options(), factory=generate) == first
    restarted = coordinator(collection, leases)
    assert await restarted.get_or_create(**options(), factory=generate) == first
    assert calls == 1
    assert len(collection.documents) == 1
    assert not leases.owners
    assert all('expireAfterSeconds' not in config for _, config in collection.indexes)


@pytest.mark.asyncio
async def test_multiple_instances_generate_once_and_defensive_copy():
    collection, leases = FakeCollection(), SharedLeases()
    instances = [coordinator(collection, leases), coordinator(collection, leases)]
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.4)
        return result()

    outputs = await asyncio.gather(
        *[instances[index % 2].get_or_create(**options(), factory=generate) for index in range(6)]
    )
    assert calls == 1
    assert len(collection.documents) == 1
    assert all(output == outputs[0] for output in outputs)
    outputs[0]['evidence']['items'].clear()
    assert (await instances[0].get_or_create(**options(), factory=generate))['evidence']['items']
    assert leases.renewals >= 2  # 生成途中续期及发布前验证所有权。
    assert not leases.owners


@pytest.mark.parametrize(
    'field,value',
    [
        ('user_id', '002'),
        ('subject_id', 'other'),
        ('profile_version', 2),
        ('facts', [{'factId': 'a', 'value': 9}]),
        ('clinical_context', {'age': 36}),
        ('revision', 'rules-v2'),
    ],
)
def test_scope_and_generation_conditions_change_key(field, value):
    original = key_args()
    changed = {**original, field: value}
    assert evidence_key(**original) != evidence_key(**changed)


def test_key_ignores_fact_order_and_revision_tracks_prompts_models():
    args = key_args()
    assert evidence_key(**args) == evidence_key(**{**args, 'facts': args['facts'][::-1]})
    original = evidence_revision(configuration={'model': 'one'}, profiles=[{'prompt': 'original'}])
    assert original == evidence_revision(configuration={'model': 'one'}, profiles=[{'prompt': 'original'}])
    assert original != evidence_revision(configuration={'model': 'two'}, profiles=[{'prompt': 'original'}])
    assert original != evidence_revision(configuration={'model': 'one'}, profiles=[{'prompt': 'edited'}])


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['empty', 'unavailable'])
async def test_transient_empty_recovers_and_then_is_fixed(status):
    collection, leases = FakeCollection(), SharedLeases()
    instance = coordinator(collection, leases)
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        return result(status, complete=False) if calls == 1 else result()

    first = await instance.get_or_create(**options(), factory=generate)
    assert first['evidence']['status'] == status
    assert not collection.documents
    ready = await instance.get_or_create(**options(), factory=generate)
    assert ready['evidence']['status'] == 'ready'
    assert await coordinator(collection, leases).get_or_create(**options(), factory=generate) == ready
    assert calls == 2


@pytest.mark.asyncio
async def test_atomic_first_writer_wins_and_scope_is_verified():
    collection = FakeCollection()
    store = MongoEvidenceResultStore(collection)
    first = result()
    second = result('empty')
    scope = options()
    assert await store.save_if_absent(**scope, result=first) == first
    assert await store.save_if_absent(**scope, result=second) == first
    assert len(collection.documents) == 1
    read_scope = {name: scope[name] for name in ('key', 'user_id', 'subject_id')}
    assert await store.read(**{**read_scope, 'user_id': '002'}) is None
    with pytest.raises(RuntimeError, match='another scope'):
        await store.save_if_absent(**{**scope, 'user_id': '002'}, result=second)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['find_one', 'insert_one', 'create_index'])
async def test_storage_outage_never_returns_unsaved_ready_result(method, monkeypatch):
    collection, leases = FakeCollection(), SharedLeases()

    async def fail(*args, **kwargs):
        raise OSError('synthetic storage outage')

    monkeypatch.setattr(collection, method, fail)
    instance = coordinator(collection, leases)

    async def generate():
        return result()

    with pytest.raises(OSError):
        await instance.get_or_create(**options(), factory=generate)
    assert not instance.cache.entries
    assert not collection.documents
    assert not leases.owners


@pytest.mark.asyncio
async def test_lost_lease_cannot_publish_result():
    collection, leases = FakeCollection(), SharedLeases()
    leases.lost = True

    async def generate():
        return result()

    with pytest.raises(RuntimeError, match='lease lost'):
        await coordinator(collection, leases).get_or_create(**options(), factory=generate)
    assert not collection.documents
    assert not leases.owners


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_generation():
    collection, leases = FakeCollection(), SharedLeases()
    leases.lost = True
    canceled = asyncio.Event()

    async def generate():
        try:
            await asyncio.sleep(30)
        finally:
            canceled.set()

    with pytest.raises(RuntimeError, match='lease lost'):
        await coordinator(collection, leases).get_or_create(**options(), factory=generate)
    assert canceled.is_set()
    assert not collection.documents
    assert not leases.owners


@pytest.mark.asyncio
async def test_last_waiter_cancellation_releases_lock_and_cancels_factory():
    collection, leases = FakeCollection(), SharedLeases()
    started, canceled = asyncio.Event(), asyncio.Event()

    async def generate():
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            canceled.set()

    task = asyncio.create_task(coordinator(collection, leases).get_or_create(**options(), factory=generate))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert canceled.is_set()
    assert not leases.owners
    assert not collection.documents


@pytest.mark.asyncio
async def test_lock_wait_is_bounded():
    collection, leases = FakeCollection(), SharedLeases()
    held = await leases.try_acquire('profile-evidence:' + options()['key'], 'another-process')

    async def generate():
        raise AssertionError('waiting request must not generate')

    with pytest.raises(TimeoutError):
        await coordinator(collection, leases, timeout_seconds=0.15).get_or_create(**options(), factory=generate)
    assert leases.owners[held.key] == held.owner_id
    assert not collection.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'no_relevant'])
async def test_service_restores_same_evidence_and_summary_after_restart(mode):
    case, knowledge, llm, add = await setup_case(mode=mode)
    add('glucose', '血糖', 'laboratory.glucose')
    add('pending', '血钾', 'laboratory.potassium', status=FactStatus.PENDING)
    collection, leases = FakeCollection(), SharedLeases()
    case.service._evidence_results = coordinator(collection, leases)
    case.service._evidence_revision = 'synthetic-rules-v1'
    facts_before = deepcopy(case.facts.documents)
    views_before = deepcopy(case.views.documents)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    counts = len(llm.calls), len(knowledge.queries)
    restarted_llm, restarted_knowledge = ScenarioLLM(), ScenarioKnowledge()
    restarted = HealthProfileService(
        case.service._repository,
        agent_hub=runtime_hub(restarted_knowledge, restarted_llm),
        evidence_results=coordinator(collection, leases),
        evidence_revision='synthetic-rules-v1',
        clock=lambda: case.now + timedelta(minutes=30),
    )
    second = await restarted.get_profile(
        'synthetic-user',
        include_evidence=True,
        fact_statuses=['pending'],
        indicator_scope='abnormal',
    )
    assert [item['factId'] for group in second['factGroups'] for item in group['items']] == ['pending']
    assert second['evidence'] == first['evidence']
    assert second['summary'] == first['summary']
    assert second['alerts'] == first['alerts']
    assert second['overview'] == first['overview']
    assert second['profileVersion'] == first['profileVersion']
    assert counts == (len(llm.calls), len(knowledge.queries))
    assert not restarted_llm.calls
    assert not restarted_knowledge.queries
    assert case.facts.documents == facts_before
    assert case.views.documents == views_before

    restarted._evidence_revision = 'synthetic-rules-v2'
    third = await restarted.get_profile('synthetic-user', include_evidence=True)
    assert third['evidence']['status'] == 'ready'
    assert len(restarted_llm.calls) == 2
    assert len(collection.documents) == 2


@pytest.mark.asyncio
async def test_profile_version_update_generates_new_snapshot():
    case, _, llm, add = await setup_case()
    add('glucose', '血糖', 'laboratory.glucose')
    collection, leases = FakeCollection(), SharedLeases()
    case.service._evidence_results = coordinator(collection, leases)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    for view in case.views.documents:
        view['profileVersion'] += 1
    second = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert second['profileVersion'] == first['profileVersion'] + 1
    assert len(collection.documents) == 2
    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_service_empty_profile_persists_time_and_not_requested_skips_storage():
    case, _, llm, _ = await setup_case()
    collection, leases = FakeCollection(), SharedLeases()
    case.service._evidence_results = coordinator(collection, leases)
    skipped = await case.service.get_profile('synthetic-user', include_evidence=False)
    assert skipped['evidence']['status'] == 'not_requested'
    assert not collection.indexes
    assert not collection.documents
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    case.now += timedelta(hours=1)
    case.service._evidence_results = coordinator(collection, leases)
    second = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert first['evidence'] == second['evidence']
    assert first['evidence']['status'] == 'empty'
    assert not llm.calls


@pytest.mark.asyncio
async def test_service_storage_failure_is_unavailable_not_variable_evidence(monkeypatch):
    case, _, llm, add = await setup_case()
    add('glucose', '血糖', 'laboratory.glucose')
    collection, leases = FakeCollection(), SharedLeases()

    async def fail(document):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(collection, 'insert_one', fail)
    case.service._evidence_results = coordinator(collection, leases)
    output = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert output['evidence']['status'] == 'unavailable'
    assert output['evidence']['items'] == []
    assert len(llm.calls) == 2
    assert not collection.documents
    assert not leases.owners


def test_application_factory_wires_durable_store_and_restart_stable_revision(monkeypatch):
    from aion.prompts import prompt_manager

    from config import env
    from health_profile import application
    from tests.unit.agents.graphs.health_profile.test_profile_evidence_graph import _build_runtime

    collection = FakeCollection()
    leases = SharedLeases()
    runtime, _ = _build_runtime(ScenarioKnowledge())
    runtime.lease_manager = leases
    monkeypatch.setattr(application, 'collection_health_profile_evidence_results', collection)
    monkeypatch.setattr(application, '_document_observations', lambda: None)
    monkeypatch.setattr(env, 'LLM_PROVIDER', 'synthetic-provider')
    monkeypatch.setattr(env, 'LLM_DEFAULT_MODEL', 'synthetic-model')
    monkeypatch.setattr(env, 'KNOWLEDGE_API_BASE_URL', 'https://example.test/knowledge')
    monkeypatch.setattr(prompt_manager, 'render', lambda name, **kwargs: 'synthetic prompt for ' + name)

    service = application.create_application_health_profile_service(runtime)
    assert service._evidence_results.store.collection is collection
    assert service._evidence_results.leases is leases

    restarted_runtime, _ = _build_runtime(ScenarioKnowledge())
    restarted_runtime.lease_manager = SharedLeases()
    restarted = application.create_application_health_profile_service(restarted_runtime)
    assert service._evidence_revision == restarted._evidence_revision
    monkeypatch.setattr(prompt_manager, 'render', lambda name, **kwargs: 'updated prompt for ' + name)
    assert application._application_evidence_revision(runtime) != service._evidence_revision
    monkeypatch.setattr(prompt_manager, 'render', lambda name, **kwargs: 'synthetic prompt for ' + name)
    monkeypatch.setattr(env, 'LLM_DEFAULT_MODEL', 'another-model')
    assert application._application_evidence_revision(runtime) != service._evidence_revision


@pytest.mark.asyncio
async def test_service_partial_ready_is_fixed_across_restart_without_new_alerts():
    from tests.unit.agents.graphs.health_profile.test_profile_evidence_stability import AlternatingLLM

    case, _, _, add = await setup_case()
    add('glucose', '血糖', 'laboratory.glucose')
    add('lipids', '甘油三酯', 'laboratory.triglyceride')
    collection, leases = BsonCollection(), SharedLeases()
    alternating = AlternatingLLM()
    case.service._agent_hub = runtime_hub(ScenarioKnowledge(), alternating)
    case.service._evidence_results = coordinator(collection, leases)
    first = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert first['evidence']['status'] == 'ready'
    assert collection.documents[0]['result']['complete'] is False
    case.service._evidence_results = coordinator(collection, leases)
    case.service._agent_hub = runtime_hub(ScenarioKnowledge(), ScenarioLLM(mode='no_relevant'))
    restarted = await case.service.get_profile('synthetic-user', include_evidence=True)
    assert restarted['evidence'] == first['evidence']
    assert restarted['alerts'] == first['alerts']
    assert restarted['summary'] == first['summary']


@pytest.mark.asyncio
async def test_persisted_result_can_be_read_without_generation_lease():
    collection, leases = FakeCollection(), SharedLeases()

    async def generate():
        return result()

    first = await coordinator(collection, leases).get_or_create(**options(), factory=generate)

    class UnavailableLeases(SharedLeases):
        async def try_acquire(self, thread_id, owner_id):
            raise OSError('synthetic Redis outage')

    assert await coordinator(collection, UnavailableLeases()).get_or_create(**options(), factory=generate) == first


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_not_regenerated_or_overwritten():
    collection, leases = FakeCollection(), SharedLeases()
    scope = options()
    collection.documents.append(
        {
            '_id': scope['key'],
            'userId': scope['user_id'],
            'subjectId': scope['subject_id'],
            'schemaVersion': 1,
            'result': result('empty', complete=False),
        }
    )

    async def generate():
        raise AssertionError('corrupt snapshots must fail closed')

    with pytest.raises(ValueError, match='invalid stored'):
        await coordinator(collection, leases).get_or_create(**scope, factory=generate)
    assert len(collection.documents) == 1
