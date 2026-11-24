"""Record source composition uses real public providers and offline BSON stores."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo import ReadPreference

from adapters.record.completion import MongoRecordCompletionStore
from bootstrap.record_sources import build_record_source_resources
from bootstrap.record_sources import build_record_source_runtime
from bootstrap.record_sources import create_application_record_source_resources
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import HealthProfileJobQueue
from health_profile.record_source import MongoRecordProfileStore
from health_profile.record_source import RecordProfileSourceService
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import source_intent_id
from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from tests.unit.agents.graphs.record.test_profile_publication import Collection as BsonCollection
from tests.unit.agents.graphs.record.test_profile_source_draft import session
from tests.unit.agents.graphs.record.test_source_admission import Leases
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection


class Collection(BsonCollection):
    def __init__(self, database, name):
        super().__init__()
        self.database, self.name = database, name
        self.options, self.reads = [], []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def find(self, query, projection=None):
        self.reads.append(deepcopy(query))
        return super().find(query, projection)

    async def find_one(self, query):
        self.reads.append(deepcopy(query))
        return await super().find_one(query)


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection(self, name)
        return self.collections[name]


async def environment():
    database = Database()
    sessions = database['configured_sessions']
    cache, leases = SimpleNamespace(get=AsyncMock(return_value=None)), Leases()
    now = [datetime(2026, 11, 10, tzinfo=UTC)]
    # Readers and ownership evidence exist before the Profile dependency graph.
    resources = build_record_source_resources(
        completion_collection=sessions, session_cache=cache, lease_manager=leases, clock=lambda: now[0]
    )
    profile = await setup_flow(authorize=False)
    now = profile.now
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=presence, owner_fence_readers={'record': resources.owner}
    )
    profile.applier.resolvers[('record', 'profile.record-source.v2')] = ProfileUserSourceResolver(
        resources.material_reader
    )
    agent_runtime, gateway = _runtime(
        {
            'candidates': [
                {
                    'answerIndex': 0,
                    'factType': 'symptom.reported',
                    'displayName': '头痛',
                    'valueType': 'text',
                    'value': '反复头痛',
                    'subjectKind': 'self',
                    'subjectHint': '本人',
                    'evidenceText': '我最近总是头痛',
                    'confidence': 0.96,
                }
            ]
        }
    )
    profile.applier.extractor = DurableProfileSourceExtractor(
        agent_runtime.hub, policy_version=HealthFactPolicy.VERSION
    )
    legacy = RecordProfileSourceService(
        MongoRecordProfileStore(sessions), HealthProfileJobQueue(profile.repository), clock=lambda: now[0]
    )
    completions = MongoRecordCompletionStore(sessions)
    runtime = build_record_source_runtime(
        resources=resources,
        legacy=legacy,
        completion_store=completions,
        inbox=profile.inbox,
        presence=presence,
        fresh_admission=fresh,
        clock=lambda: now[0].timestamp(),
    )
    return SimpleNamespace(
        database=database,
        sessions=sessions,
        cache=cache,
        leases=leases,
        resources=resources,
        profile=profile,
        presence=presence,
        fresh=fresh,
        legacy=legacy,
        completions=completions,
        runtime=runtime,
        now=now,
        gateway=gateway,
    )


async def complete(env, *, revision=RECORD_PROFILE_SOURCE_REVISION):
    raw = session()
    raw.update(
        userId='u',
        recordProfileConsumerRevision=revision,
        recordCompletionId='commit-1',
        qaHistory=[{'question': {'questionId': 'q0', 'question': '近况'}, 'answer': '我最近总是头痛'}],
        extractedInfo={'age': 42},
        extractedInfoSources={'age': 'user_input'},
        healthData={'hospitalData': [], 'weRunData': [], 'smartDeviceData': {}},
    )
    env.runtime.router.prepare_completed_snapshot(raw)
    assert await env.runtime.completion_store.publish_once('u', 'session', raw)
    return raw


async def row(env):
    return await env.resources.store.get(source_intent_id('u', 'session'))


def restart(env):
    resources = build_record_source_resources(
        completion_collection=env.sessions, session_cache=env.cache, lease_manager=env.leases, clock=lambda: env.now[0]
    )
    fresh = ProfileFreshSourceAdmission(
        inbox=env.profile.inbox, presence_reader=env.presence, owner_fence_readers={'record': resources.owner}
    )
    env.resources, env.fresh = resources, fresh
    env.profile.applier.resolvers[('record', 'profile.record-source.v2')] = ProfileUserSourceResolver(
        resources.material_reader
    )
    env.runtime = build_record_source_runtime(
        resources=resources,
        legacy=env.legacy,
        completion_store=env.completions,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=fresh,
        clock=lambda: env.now[0].timestamp(),
    )


def test_construction_is_pure_and_reuses_exact_resources_and_completion_evidence():
    database = Database()
    sessions, cache, leases = database['tenant_sessions'], object(), object()
    resources = build_record_source_resources(completion_collection=sessions, session_cache=cache, lease_manager=leases)
    completions = MongoRecordCompletionStore(sessions)
    runtime = build_record_source_runtime(
        resources=resources,
        legacy=object(),
        completion_store=completions,
        inbox=object(),
        presence=object(),
        fresh_admission=object(),
    )
    sources = database['tenant_sessions_record_profile_sources']
    admissions = database['tenant_sessions_record_source_admissions']
    assert set(database.collections) == {sessions.name, sources.name, admissions.name}
    assert resources.material_reader.publications.store is resources.store is runtime.worker.store
    assert resources.owner.evidence.sources is resources.store
    assert resources.owner.evidence.collection is sessions
    assert resources.owner.evidence.cache is cache and resources.owner.evidence.leases is leases
    assert runtime.worker.completion_evidence is resources.owner.evidence
    assert runtime.router.publications is runtime.completion_store.publications is runtime.worker
    assert runtime.completion_store.completions is completions
    assert runtime.router.consumer_revision == RECORD_PROFILE_SOURCE_REVISION
    assert sessions.options == [{'read_preference': ReadPreference.PRIMARY}]
    for collection in (sources, admissions):
        assert collection.options[0]['read_preference'] == ReadPreference.PRIMARY
        assert collection.options[0]['write_concern'].document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
    assert all(not item.documents and not item.reads and not item.indexes for item in database.collections.values())


def test_application_resources_only_use_configured_sessions_and_require_real_owner_dependencies(monkeypatch):
    import db.mongodb as mongodb

    database = Database()
    monkeypatch.setattr(mongodb, 'collection_sessions', database['configured'])
    resources = create_application_record_source_resources(session_cache=object(), lease_manager=object())
    assert resources.store.collection is database['configured_record_profile_sources']
    assert resources.owner.evidence.collection is database['configured']
    with pytest.raises(ValueError, match='actual session cache'):
        create_application_record_source_resources(session_cache=None, lease_manager=object())
    assert all(not item.reads and not item.documents and not item.indexes for item in database.collections.values())


@pytest.mark.asyncio
async def test_completion_is_local_then_actual_profile_fresh_receive_and_apply_run_in_background(monkeypatch):
    env = await environment()
    scope = AsyncMock(wraps=env.profile.inbox.prepare_self_scope)
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', scope)
    raw = await complete(env)
    frozen = deepcopy(env.sessions.documents)
    await env.runtime.router.ensure_queued(user_id='u', session_id='session')
    assert scope.await_count == 0 and not env.profile.collections['publications'].documents
    assert not env.gateway.complete_requests
    assert await env.runtime.worker.recover_pending() == 1
    current = await row(env)
    assert current['status'] == 'waiting_receipt' and current['authorization']['status'] == 'ready'
    material = await env.resources.material_reader.read_source_snapshot(
        ProfileSourceSnapshotRefV1.model_validate(current['snapshotRef']), scope=env.profile.scope
    )
    assert material['qaHistory'] == raw['qaHistory']
    assert await env.profile.worker.recover_pending() == 1
    env.now[0] += timedelta(seconds=31)
    await env.runtime.worker.recover_pending()
    assert (await row(env))['status'] == 'applied'
    assert len((await env.profile.reader.read('u')).facts) == 2 and len(env.gateway.complete_requests) == 1
    assert env.sessions.documents == frozen and not env.profile.collections['jobs'].documents


@pytest.mark.asyncio
async def test_profile_outage_keeps_completion_and_restart_recovers_without_get_or_business_replay(monkeypatch):
    env = await environment()
    prepare_scope = env.profile.inbox.prepare_self_scope
    monkeypatch.setattr(
        env.profile.inbox, 'prepare_self_scope', AsyncMock(side_effect=ConnectionError('Profile offline'))
    )
    await complete(env)
    frozen = deepcopy(env.sessions.documents)
    await env.runtime.worker.recover_pending()
    assert (await row(env))['status'] == 'retry_wait'
    assert next(iter(env.sessions.documents.values()))['status'] == 'completed'
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', prepare_scope)
    restart(env)
    env.now[0] += timedelta(seconds=31)
    await env.runtime.worker.recover_pending()
    assert (await row(env))['status'] == 'waiting_receipt'
    assert await env.profile.worker.recover_pending() == 1
    env.now[0] += timedelta(seconds=31)
    await env.runtime.worker.recover_pending()
    assert (await row(env))['status'] == 'applied' and env.sessions.documents == frozen


@pytest.mark.asyncio
async def test_lost_receive_ack_reuses_fixed_material_and_authorization_after_full_helper_restart(monkeypatch):
    env = await environment()
    await complete(env)
    receive, requests = env.profile.inbox.receive, []

    async def lost(event, **kwargs):
        requests.append(event.model_dump(mode='json', by_alias=True))
        result = await receive(event, **kwargs)
        if len(requests) == 1:
            raise ConnectionError('received ACK lost')
        return result

    monkeypatch.setattr(env.profile.inbox, 'receive', lost)
    await env.runtime.worker.recover_pending()
    before = await row(env)
    assert before['status'] == 'retry_wait'
    restart(env)
    env.now[0] += timedelta(seconds=31)
    await env.runtime.worker.recover_pending()
    after = await row(env)
    assert after['status'] == 'waiting_receipt'
    assert before['materialJson'] == after['materialJson'] and before['authorization'] == after['authorization']
    assert requests == [requests[0], requests[0]]
    assert len([r for r in env.profile.collections['publications'].documents if r['recordKind'] == 'publication']) == 1
    assert len(env.database['configured_sessions_record_source_admissions'].documents) == 1


@pytest.mark.asyncio
async def test_full_completion_evidence_is_used_before_any_profile_command(monkeypatch):
    env = await environment()
    await complete(env)
    next(iter(env.sessions.documents.values()))['qaHistory'][0]['answer'] = 'uncommitted mutation'
    scope = AsyncMock(wraps=env.profile.inbox.prepare_self_scope)
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', scope)
    await env.runtime.worker.recover_pending()
    assert (await row(env))['status'] == 'failed'
    scope.assert_not_awaited()
    assert not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_real_legacy_path_remains_separate_and_new_worker_never_runs_old_scan(monkeypatch):
    env = await environment()
    legacy_scan = AsyncMock(side_effect=AssertionError('legacy recovery has its own owner'))
    monkeypatch.setattr(env.legacy, 'recover_pending', legacy_scan)
    await complete(env, revision=LEGACY_RECORD_PROFILE_REVISION)
    result = await env.runtime.router.ensure_queued(user_id='u', session_id='session')
    assert result.ingestion_queued and len(env.profile.collections['jobs'].documents) == 1
    assert await row(env) is None and await env.runtime.worker.recover_pending() == 0
    legacy_scan.assert_not_awaited()
    with pytest.raises(RecordSourceConflict, match='unavailable'):
        await complete(env, revision='unknown.v9')
    assert not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_missing_fresh_capability_does_not_prevent_business_completion():
    env = await environment()
    env.runtime = build_record_source_runtime(
        resources=env.resources,
        legacy=env.legacy,
        completion_store=env.completions,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=None,
        clock=lambda: env.now[0].timestamp(),
    )
    await complete(env)
    await env.runtime.worker.recover_pending()
    assert next(iter(env.sessions.documents.values()))['status'] == 'completed'
    assert (await row(env))['status'] == 'retry_wait'
    assert not env.profile.collections['publications'].documents
