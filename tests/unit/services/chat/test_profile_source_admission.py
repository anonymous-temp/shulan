"""Real public Profile fresh admission and application with Chat's actual owner evidence."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.chat.profile_source import create_chat_profile_source
from health_profile.ingestion import HealthFactPolicy
from health_profile.models import FactStatus
from modules.chat.profile_source import prepare_user_message_source
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from tests.unit.health_profile.test_chat_ingestion import _runtime
from tests.unit.health_profile.test_multi_source_publications import candidate
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection
from tests.unit.services.chat.test_profile_source_publication import environment
from tests.unit.services.chat.test_profile_source_publication import save
from tests.unit.services.chat.test_profile_source_publication import task


async def composition():
    env = environment()
    profile = await setup_flow(authorize=False)
    env.now[0] = profile.now[0].timestamp()
    env.raw['chatProfileIngestion'] = []
    env.raw['messages'][0]['content'] = '  我最近总是头痛  '
    prepare_user_message_source(env.raw, env.raw['messages'][0], now=profile.now[0])
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: profile.now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=presence, owner_fence_readers={'chat': env.resources.fence_reader}
    )
    env.router = create_chat_profile_source(
        resources=env.resources,
        inbox=profile.inbox,
        presence=presence,
        fresh_admission=fresh,
        legacy=AsyncMock(),
        clock=lambda: env.now[0],
    )
    env.service = env.router.publication
    profile.applier.resolvers[('chat', 'profile.chat-source.v2')] = ProfileUserSourceResolver(
        env.resources.snapshot_reader
    )
    runtime, gateway = _runtime({'candidates': [candidate()]})
    profile.applier.extractor = DurableProfileSourceExtractor(runtime.hub, policy_version=HealthFactPolicy.VERSION)
    env.profile, env.gateway, env.presence, env.fresh, env.presence_collections = (
        profile,
        gateway,
        presence,
        fresh,
        collections,
    )
    return env


async def retry(env):
    env.now[0] += 31
    env.profile.now[0] += timedelta(seconds=31)
    await env.service.recover_pending()
    return await task(env)


@pytest.mark.asyncio
async def test_actual_scope_fresh_admission_resolver_and_apply_are_background_and_replay_safe():
    env = await composition()
    await save(env)
    assert not env.profile.collections['publications'].documents
    assert await env.service.recover_pending() == 1
    current = await task(env)
    assert current['status'] == 'waiting_receipt' and current['authorization']['status'] == 'ready'
    assert not env.profile.collections['jobs'].documents
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    facts = await env.profile.repository.list_facts(
        'u', env.profile.scope.subject_id, statuses=(FactStatus.ACTIVE, FactStatus.PENDING)
    )
    assert len(facts) == 1 and len(env.gateway.complete_requests) == 1
    assert current['material']['snapshot']['content'] == '  我最近总是头痛  '
    assert len([row for row in env.owned.documents.values() if row.get('recordKind') == 'admission']) == 1
    assert env.owned.indexes == []


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['reserve_fresh_source', 'authorize_fresh_source', 'receive', 'owner_insert'])
async def test_actual_ack_loss_reuses_auth_and_event_with_reconstructed_adapters(phase, monkeypatch):
    env = await composition()
    await save(env)
    target = env.profile.inbox if phase == 'receive' else env.fresh
    name = phase
    if phase == 'owner_insert':
        target, name = env.owned, 'insert_one'
    original = getattr(target, name)
    first = [True]

    async def lost(*args, **kwargs):
        result = await original(*args, **kwargs)
        eligible = phase != 'owner_insert' or args[0].get('recordKind') == 'admission'
        if first[0] and eligible:
            first[0] = False
            raise RuntimeError('offline ACK lost')
        return result

    monkeypatch.setattr(target, name, lost)
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    auth = deepcopy(
        next(row for row in env.owned.documents.values() if row.get('recordKind') == 'admission')['authorization']
    )
    env.router = create_chat_profile_source(
        resources=env.resources,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
        legacy=AsyncMock(),
        clock=lambda: env.now[0],
    )
    env.service = env.router.publication
    assert (await retry(env))['status'] == 'waiting_receipt'
    assert (await task(env))['authorization']['authorization'] == auth
    assert (
        len([row for row in env.profile.collections['publications'].documents if row['recordKind'] == 'publication'])
        == 1
    )
    assert len([row for row in env.owned.documents.values() if row.get('recordKind') == 'admission']) == 1


@pytest.mark.asyncio
async def test_ready_replay_never_recomputes_absence_and_keeps_real_ready_reservation(monkeypatch):
    env = await composition()
    await save(env)
    await env.service.recover_pending()
    row = env.owned.documents[env.identity]
    for key in ('reservation', 'authorization', 'receipt'):
        row.pop(key)
    env.presence.read_legacy_presence = AsyncMock(side_effect=AssertionError('must not inspect current absence'))
    assert (await retry(env))['reservation']['status'] == 'ready'
    env.presence.read_legacy_presence.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'processing', 'completed', 'failed', 'dead_letter'])
async def test_any_old_job_including_null_subject_rejects_fresh(status):
    env = await composition()
    await save(env)
    env.profile.collections['jobs'].documents.append(
        {'_id': 'old', 'userId': 'u', 'subjectId': None, 'sourceType': 'chat', 'sourceId': 'm', 'status': status}
    )
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed'
    assert not env.profile.collections['publications'].documents
    assert not any(row.get('recordKind') == 'admission' for row in env.owned.documents.values())
    assert env.sessions.documents['0']['messages'][0]['content'] == '  我最近总是头痛  '


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['relative', 'overflow', 'read_failure', 'after_read_race'])
async def test_full_absence_rejects_unsupported_history_and_rechecks_races(kind):
    env = await composition()
    await save(env)
    if kind == 'relative':
        env.profile.collections['facts'].documents.append(
            {
                '_id': 'family',
                'userId': 'u',
                'subjectId': 'father',
                'status': 'deleted',
                'sources': [{'sourceType': 'chat', 'sourceId': 'm'}],
            }
        )
    elif kind == 'overflow':
        env.profile.collections['jobs'].documents.extend(
            {'_id': str(i), 'userId': 'u', 'sourceType': 'chat', 'sourceId': 'm', 'status': 'completed'}
            for i in range(1001)
        )
    elif kind == 'read_failure':
        env.presence_collections['jobs'].fail = True
    else:

        def race():
            env.presence_collections['jobs'].after_read = None
            env.profile.collections['jobs'].documents.append(
                {'_id': 'race', 'userId': 'u', 'sourceType': 'chat', 'sourceId': 'm', 'status': 'processing'}
            )

        env.presence_collections['jobs'].after_read = race
    await env.service.recover_pending()
    assert not env.profile.collections['publications'].documents
    assert (await task(env))['status'] in {'retry_wait', 'failed'}
    if kind == 'read_failure':
        assert (await task(env))['status'] == 'retry_wait'


@pytest.mark.asyncio
async def test_fixed_fence_read_does_not_read_session_but_held_check_detects_changed_message():
    env = await composition()
    await save(env)
    await env.service.recover_pending()
    auth = (await task(env))['authorization']['authorization']
    ref = ProfileSourceSnapshotRefV1.model_validate(auth['ownerFenceRef'])
    proof = await env.resources.fence_reader.read_admission_fence(scope=env.profile.scope, snapshot_ref=ref)
    env.sessions.documents['0']['updatedAt'] = datetime.now(UTC)
    env.sessions.documents['0']['messages'].append({'role': 'assistant', 'messageId': 'other'})
    assert await env.resources.fence_reader.is_admission_fence_held(
        scope=env.profile.scope, snapshot_ref=ref, content_digest=auth['ownerFenceDigest']
    )
    with pytest.raises(ValueError):
        await env.resources.fence_reader.read_admission_fence(
            scope=ProfileScope(user_id='other', subject_id='other'), snapshot_ref=ref
        )
    env.sessions.documents['0']['messages'][0]['content'] = 'changed'
    assert await env.resources.fence_reader.read_admission_fence(scope=env.profile.scope, snapshot_ref=ref) == proof
    with pytest.raises(ValueError):
        await env.resources.fence_reader.is_admission_fence_held(
            scope=env.profile.scope, snapshot_ref=ref, content_digest=auth['ownerFenceDigest']
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['reservation', 'authorization', 'receipt', 'dispatchAttempts'])
async def test_local_formal_history_without_owner_auth_cannot_be_claimed_fresh(field):
    env = await composition()
    await save(env)
    env.owned.documents[env.identity][field] = 1 if field == 'dispatchAttempts' else {'unknown': 'previous'}
    await env.service.recover_pending()
    assert not env.profile.collections['publications'].documents
    assert not any(row.get('recordKind') == 'admission' for row in env.owned.documents.values())
    assert (await task(env))['status'] == 'failed'


@pytest.mark.asyncio
async def test_concurrent_initial_admission_freezes_one_authorization_and_rejects_mutation():
    import asyncio

    env = await composition()
    await save(env)
    original = env.fresh.reserve_fresh_source
    env.fresh.reserve_fresh_source = AsyncMock(side_effect=RuntimeError('pause after durable auth'))
    await env.service.recover_one(env.identity)
    material = deepcopy((await task(env))['material'])
    env.fresh.reserve_fresh_source = original
    states = await asyncio.gather(env.service.admission.reserve(material), env.service.admission.reserve(material))
    assert states[0]['authorization'] == states[1]['authorization']
    stored = next(row for row in env.owned.documents.values() if row.get('recordKind') == 'admission')
    stored['authorization']['authorizationId'] = 'changed'
    with pytest.raises(ValueError, match='record changed'):
        await env.service.admission.reserve(material)


@pytest.mark.asyncio
async def test_actual_legacy_sender_never_admits_new_mode_even_before_marker_exists():
    from health_profile.chat_source import ChatProfileSourceService
    from health_profile.chat_source import MongoChatProfileStore
    from health_profile.ingestion import HealthProfileJobQueue
    from tests.unit.health_profile.test_chat_profile_source import FakeSessionCollection
    from tests.unit.health_profile.test_chat_profile_source import InMemoryJobRepository

    env = await composition()
    await save(env)
    jobs = InMemoryJobRepository()
    legacy = ChatProfileSourceService(
        MongoChatProfileStore(FakeSessionCollection(env.raw)), HealthProfileJobQueue(jobs)
    )
    result = await legacy.ensure_queued(user_id='u', message_id='m')
    assert not result.ingestion_queued and not jobs.jobs
    assert not env.profile.collections['publications'].documents
    await env.service.recover_pending()
    result = await legacy.ensure_queued(user_id='u', message_id='m')
    assert not result.ingestion_queued and not jobs.jobs
