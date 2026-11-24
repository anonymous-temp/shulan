"""Real Chat public composition with offline BSON and public Profile admission/application."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo import ReadPreference

from bootstrap.chat_sources import build_chat_source_resources
from bootstrap.chat_sources import build_chat_source_runtime
from bootstrap.chat_sources import create_application_chat_source_resources
from health_profile.chat_source import ChatProfileSourceService
from health_profile.chat_source import MongoChatProfileStore
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import HealthProfileJobQueue
from modules.chat.profile_publication import source_task_id
from modules.chat.profile_source import CHAT_PROFILE_DELIVERY_MODE
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfilePublicationWorker
from modules.health_profile.application import ProfileSourceApplier
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from tests.unit.health_profile.test_chat_ingestion import _runtime
from tests.unit.health_profile.test_multi_source_publications import SELF_TEXT
from tests.unit.health_profile.test_multi_source_publications import candidate
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection
from tests.unit.services.chat.test_profile_source_draft import source
from tests.unit.services.chat.test_profile_source_publication import Collection as BsonCollection


class Collection(BsonCollection):
    def __init__(self, database, name):
        super().__init__()
        self.database, self.name = database, name
        self.reads = []

    def find(self, query, projection=None):
        self.reads.append(deepcopy(query))
        return super().find(query, projection)

    async def find_one(self, query):
        self.reads.append(deepcopy(query))
        return await super().find_one(query)

    async def update_one(self, query, update):
        # Mongo's positional array update for the unchanged legacy sender.
        if any('.$.' in key for key in update.get('$set', {})):
            row = await self.find_one(query)
            if row is not None:
                for key, value in update['$set'].items():
                    field, member = key.split('.$.')
                    selected = query[field]['$elemMatch']
                    for item in self.documents[row['_id']][field]:
                        if self.matches(item, selected):
                            item[member] = deepcopy(value)
                            break
            return SimpleNamespace(acknowledged=True, matched_count=int(row is not None))
        return await super().update_one(query, update)


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
    # Immutable reader/fence resources exist before the Profile services.
    resources = build_chat_source_resources(sessions=sessions)
    profile = await setup_flow(authorize=False)
    now = profile.now
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=presence, owner_fence_readers={'chat': resources.fence_reader}
    )
    agent_runtime, gateway = _runtime({'candidates': [candidate()]})
    applier = ProfileSourceApplier(
        profile.inbox,
        resolvers={('chat', 'profile.chat-source.v2'): ProfileUserSourceResolver(resources.snapshot_reader)},
        extractor=DurableProfileSourceExtractor(agent_runtime.hub, policy_version=HealthFactPolicy.VERSION),
    )
    legacy = ChatProfileSourceService(
        MongoChatProfileStore(sessions), HealthProfileJobQueue(profile.repository), clock=lambda: now[0]
    )
    runtime = build_chat_source_runtime(
        resources=resources,
        inbox=profile.inbox,
        presence=presence,
        fresh_admission=fresh,
        legacy=legacy,
        clock=lambda: now[0].timestamp(),
    )
    return SimpleNamespace(
        database=database,
        sessions=sessions,
        resources=resources,
        profile=profile,
        presence=presence,
        fresh=fresh,
        legacy=legacy,
        runtime=runtime,
        now=now,
        gateway=gateway,
        profile_worker=ProfilePublicationWorker(profile.inbox, applier),
    )


async def save(env, *, changes=None):
    raw, message = source()
    raw['type'] = 'chat'
    for target, key, value in changes or ():
        (raw if target == 'session' else message)[key] = value
    env.runtime.router.prepare_user_message(raw, message)
    await env.sessions.insert_one(raw)
    return raw, message


async def task(env, user='u', message='m'):
    return await env.resources.store.get(source_task_id(user, message))


def test_resources_and_runtime_construction_are_pure_and_keep_exact_storage_identity():
    database = Database()
    sessions = database['tenant_sessions']
    resources = build_chat_source_resources(sessions=sessions)
    runtime = build_chat_source_runtime(
        resources=resources, inbox=object(), presence=object(), fresh_admission=object(), legacy=object()
    )
    owned = database['tenant_sessions_chat_profile_sources']
    assert set(database.collections) == {'tenant_sessions', 'tenant_sessions_chat_profile_sources'}
    assert resources.store.collection is resources.materials.collection is owned
    assert resources.owner.collection is sessions
    assert resources.fence_reader.store is resources.store
    assert resources.fence_reader.owner is resources.owner
    assert runtime.router.owner is resources.owner and runtime.worker.owner is resources.owner
    assert runtime.worker is runtime.router.publication
    assert runtime.worker.store is resources.store
    assert sessions.options == [{'read_preference': ReadPreference.PRIMARY}]
    assert owned.options[0]['read_preference'] == ReadPreference.PRIMARY
    assert owned.options[0]['write_concern'].document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
    assert all(not item.documents and not item.indexes and not item.reads for item in database.collections.values())


def test_application_resources_use_only_the_configured_session_namespace(monkeypatch):
    import db.mongodb as mongodb

    database = Database()
    monkeypatch.setattr(mongodb, 'collection_sessions', database['configured'])
    resources = create_application_chat_source_resources()
    assert resources.owner.collection is database['configured']
    assert resources.store.collection is database['configured_chat_profile_sources']
    assert all(not item.reads and not item.documents and not item.indexes for item in database.collections.values())


@pytest.mark.asyncio
async def test_saved_raw_intent_is_local_then_actual_profile_is_applied_by_background_only():
    env = await environment()
    raw, message = await save(env, changes=[('message', 'content', '  ' + SELF_TEXT + '  ')])
    frozen = deepcopy(env.sessions.documents)
    await env.runtime.router.ensure_queued(user_id='u', message_id='m')
    assert (await task(env))['status'] == 'pending'
    assert not env.profile.collections['publications'].documents and not env.gateway.complete_requests
    assert await env.runtime.worker.recover_pending() == 1
    row = await task(env)
    assert row['authorization']['status'] == 'ready' and row['status'] == 'waiting_receipt'
    assert row['material']['snapshot']['content'] == message['content']
    assert row['material']['snapshot']['timestamp'] == message['timestamp']
    ref = ProfileSourceSnapshotRefV1.model_validate(row['material']['event']['snapshotRef'])
    material = await env.resources.snapshot_reader.read_source_snapshot(ref, scope=env.profile.scope)
    assert material['content'] == message['content']
    assert await env.profile_worker.recover_pending() == 1
    env.now[0] += timedelta(seconds=31)
    await env.runtime.worker.recover_pending()
    assert (await task(env))['status'] == 'applied'
    assert (await env.profile.reader.read('u')).facts and len(env.gateway.complete_requests) == 1
    assert env.sessions.documents == frozen and not env.profile.collections['jobs'].documents
    assert raw['chatProfileIngestion'][0]['deliveryMode'] == CHAT_PROFILE_DELIVERY_MODE


@pytest.mark.asyncio
async def test_profile_outage_and_local_registration_loss_recover_without_business_or_query_replay(monkeypatch):
    env = await environment()
    await save(env)
    original = env.resources.store.insert
    monkeypatch.setattr(env.resources.store, 'insert', AsyncMock(side_effect=RuntimeError('local unavailable')))
    assert await env.runtime.router.ensure_queued(user_id='u', message_id='m') is None
    assert env.sessions.documents and not env.profile.collections['publications'].documents
    monkeypatch.setattr(env.resources.store, 'insert', original)
    prepare_scope = env.profile.inbox.prepare_self_scope
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', AsyncMock(side_effect=RuntimeError('Profile offline')))
    await env.runtime.worker.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    monkeypatch.setattr(env.profile.inbox, 'prepare_self_scope', prepare_scope)
    resources = build_chat_source_resources(sessions=env.sessions)
    restarted = build_chat_source_runtime(
        resources=resources,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
        legacy=env.legacy,
        clock=lambda: env.now[0].timestamp(),
    )
    env.now[0] += timedelta(seconds=31)
    await restarted.worker.recover_pending()
    assert (await task(env))['status'] == 'waiting_receipt'
    assert await env.profile_worker.recover_pending() == 1
    env.now[0] += timedelta(seconds=31)
    await restarted.worker.recover_pending()
    assert (await task(env))['status'] == 'applied'
    assert len(env.sessions.documents) == 1 and len(env.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_returned_worker_never_runs_legacy_scan_and_explicit_legacy_input_keeps_original_queue(monkeypatch):
    env = await environment()
    legacy_scan = AsyncMock(side_effect=AssertionError('the existing Profile worker owns this scan'))
    monkeypatch.setattr(env.legacy, 'recover_pending', legacy_scan)
    await save(env, changes=[('session', 'userId', 'u' * 257)])
    await env.runtime.router.ensure_queued(user_id='u' * 257, message_id='m')
    assert len(env.profile.collections['jobs'].documents) == 1
    saved = next(iter(env.sessions.documents.values()))
    assert saved['chatProfileIngestion'][0]['deliveryMode'] == 'legacy'
    assert saved['userId'] == 'u' * 257
    assert await env.runtime.worker.recover_pending() == 0
    legacy_scan.assert_not_awaited()
    assert await task(env, 'u' * 257) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [CHAT_PROFILE_DELIVERY_MODE, 'unknown'])
async def test_new_and_unknown_modes_cannot_enter_legacy_even_before_a_profile_marker(mode):
    env = await environment()
    await save(env)
    if mode == 'unknown':
        env.sessions.documents['0']['chatProfileIngestion'][0]['deliveryMode'] = mode
    assert not env.profile.collections['publications'].documents
    legacy_result = await env.legacy.ensure_queued(user_id='u', message_id='m')
    assert legacy_result is not None and not legacy_result.ingestion_queued
    assert not env.profile.collections['jobs'].documents
    if mode == 'unknown':
        assert await env.runtime.router.ensure_queued(user_id='u', message_id='m') is None
        await env.runtime.worker.recover_pending()
        assert await task(env) is None
        assert not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_lost_receive_ack_restarts_with_same_fixed_material_and_authorization(monkeypatch):
    env = await environment()
    await save(env)
    receive = env.profile.inbox.receive
    requests = []

    async def unknown(event, **kwargs):
        requests.append(event.model_dump(mode='json', by_alias=True))
        result = await receive(event, **kwargs)
        if len(requests) == 1:
            raise ConnectionError('acknowledgement lost after durable reception')
        return result

    monkeypatch.setattr(env.profile.inbox, 'receive', unknown)
    await env.runtime.worker.recover_pending()
    before = await task(env)
    assert before['status'] == 'retry_wait'
    resources = build_chat_source_resources(sessions=env.sessions)
    restarted = build_chat_source_runtime(
        resources=resources,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
        legacy=env.legacy,
        clock=lambda: env.now[0].timestamp(),
    )
    env.now[0] += timedelta(seconds=31)
    await restarted.worker.recover_pending()
    after = await task(env)
    assert after['status'] == 'waiting_receipt'
    assert requests == [requests[0], requests[0]]
    assert after['authorization'] == before['authorization'] and after['material'] == before['material']
    assert len([r for r in env.profile.collections['publications'].documents if r['recordKind'] == 'publication']) == 1
    assert len([r for r in resources.store.collection.documents.values() if r.get('recordKind') == 'admission']) == 1
    assert env.sessions.documents['0']['messages'][0]['content'] == '  original\ntext  '
