"""Chat task replay, saved-message admission, independent business response and fair recovery."""

import asyncio
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import create_autospec

import pytest
from bson import BSON
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.command_cursor import AsyncCommandCursor

from adapters.chat.profile_source import ChatProfileSourceRouter
from adapters.chat.profile_source import create_chat_source_resources
from adapters.chat.source_admission import ChatMaterialPreparer
from adapters.chat.source_store import MongoChatSavedSourceReader
from modules.chat.profile_publication import ChatSourcePublicationService
from modules.chat.profile_publication import source_task_id
from modules.chat.profile_source import ChatProfileDeliveryPolicy
from modules.chat.profile_source import prepare_user_message_source
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceReceiptV1
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.fakes import MemoryMongoCursor
from tests.unit.services.chat.test_profile_source_draft import source


class Cursor(MemoryMongoCursor):
    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def max_time_ms(self, value):
        assert value == 5000
        return self


class Collection(MemoryMongoCollection):
    def __init__(self):
        super().__init__()
        self.options = []
        self.pipelines = []

    def with_options(self, **options):
        self.options.append(options)
        return self

    def matches(self, item, filters):
        remaining = {}
        for key, expected in filters.items():
            if isinstance(expected, dict):
                actual = self.value(item, key)
                if '$elemMatch' in expected:
                    if not any(self.matches(value, expected['$elemMatch']) for value in actual or []):
                        return False
                    continue
                if '$nin' in expected and actual in expected['$nin']:
                    return False
                if '$ne' in expected and actual == expected['$ne']:
                    return False
                if '$exists' in expected and self.has_field(item, key) != expected['$exists']:
                    return False
            remaining[key] = expected
        return super().matches(item, remaining)

    async def insert_one(self, value):
        value = deepcopy(value)
        value.setdefault('_id', str(len(self.documents)))
        return await super().insert_one(BSON.encode(value).decode())

    async def replace_one(self, query, value):
        for key, old in self.documents.items():
            if self.matches(old, query):
                self.documents[key] = BSON.encode(value).decode()
                return SimpleNamespace(acknowledged=True, modified_count=1)
        return SimpleNamespace(acknowledged=True, modified_count=0)

    def find(self, query, projection=None):
        return Cursor([row for row in self.documents.values() if self.matches(row, query)])

    async def aggregate(self, pipeline, **kwargs):
        assert kwargs['maxTimeMS'] == 5000 and kwargs['collation'].document == {'locale': 'simple'}
        self.pipelines.append(deepcopy(pipeline))
        rows = deepcopy(list(self.documents.values()))
        for stage in pipeline:
            if '$match' in stage:
                rows = [row for row in rows if self.matches(row, stage['$match'])]
            elif '$unwind' in stage:
                field = stage['$unwind'][1:]
                rows = [{**deepcopy(row), field: deepcopy(item)} for row in rows for item in row.get(field, [])]
            elif '$project' in stage:
                rows = [
                    {
                        key: deepcopy(self.value(row, value[1:]) if isinstance(value, str) else row.get(key))
                        for key, value in stage['$project'].items()
                        if value
                    }
                    for row in rows
                ]
            elif '$sort' in stage:
                rows = Cursor(rows).sort(list(stage['$sort'].items())).rows
            elif '$limit' in stage:
                rows = rows[: stage['$limit']]
            else:
                raise AssertionError(stage)
        return Cursor(rows)


class Admission:
    def __init__(self):
        self.calls = []
        self.fail = None

    async def prepare_self_scope(self, user_id):
        self.calls.append('scope')
        if self.fail == 'scope':
            raise RuntimeError('offline Profile unavailable')
        return {'userId': user_id, 'subjectId': 'self'}

    async def reserve(self, material):
        self.calls.append('reserve')
        event = material['event']
        return {
            'status': 'blocked',
            'authorization': {
                'admissionKind': 'fresh',
                'scope': event['scope'],
                'sourceType': 'chat',
                'sourceId': event['sourceId'],
                'authorizationId': 'auth',
                'ownerFenceRef': {'provider': 'chat', 'snapshotId': 'proof'},
                'materialContractVersion': 'profile.chat-source.v2',
                'materialSnapshotRef': event['snapshotRef'],
                'materialContentDigest': event['contentDigest'],
            },
        }

    async def authorize(self, reservation):
        self.calls.append('authorize')
        return {**deepcopy(reservation), 'status': 'ready'}


class Receiver:
    def __init__(self):
        self.calls, self.lookups = [], []
        self.fail_ack = False
        self.status = 'received'

    def receipt(self, event):
        status = self.status
        return ProfileSourceReceiptV1(
            event_id=event['eventId'],
            publication_id='pub',
            scope=event['scope'],
            source_type='chat',
            source_id=event['sourceId'],
            publication_revision=1,
            status=status,
            received_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            application_state='applied' if status == 'applied' else 'partial' if status == 'partial' else 'not_applied',
            watermark={
                **{key: event[key] for key in ('scope', 'sourceType', 'sourceId')},
                'acceptedPublicationRevision': 1,
                'appliedPublicationRevision': int(status == 'applied'),
            },
        ).model_dump(mode='json', by_alias=True)

    async def receive(self, event):
        ProfileSourceEventV1.model_validate(event)
        self.calls.append(deepcopy(event))
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError('lost acceptance ACK')
        return self.receipt(event)

    async def get_receipt(self, event):
        self.lookups.append(deepcopy(event))
        return self.receipt(event)


def environment(policy=ChatProfileDeliveryPolicy()):
    owned, sessions = Collection(), Collection()
    resources = create_chat_source_resources(collection=owned, sessions=sessions)
    now = [datetime(2026, 11, 10, tzinfo=UTC).timestamp()]
    admission, receiver = Admission(), Receiver()
    service = ChatSourcePublicationService(
        store=resources.store,
        owner=resources.owner,
        materials=ChatMaterialPreparer(owner=resources.owner, materials=resources.materials),
        admission=admission,
        receiver=receiver,
        clock=lambda: now[0],
    )
    raw, message = source()
    raw['type'] = 'chat'
    prepare_user_message_source(raw, message, policy=policy, now=datetime.fromtimestamp(now[0], UTC))
    return SimpleNamespace(
        owned=owned,
        sessions=sessions,
        resources=resources,
        service=service,
        raw=raw,
        now=now,
        admission=admission,
        receiver=receiver,
        identity=source_task_id('u', 'm'),
    )


async def save(env):
    await env.sessions.insert_one(env.raw)
    await env.service.ensure_queued(user_id='u', message_id='m')


async def task(env):
    return await env.resources.store.get(env.identity)


@pytest.mark.asyncio
async def test_register_is_business_only_and_missing_save_cannot_dispatch():
    env = environment()
    assert await env.service.ensure_queued(user_id='u', message_id='m') is None
    assert not env.owned.documents and not env.admission.calls
    await save(env)
    assert (await task(env))['status'] == 'pending' and not env.admission.calls
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'waiting_receipt'
    assert env.admission.calls == ['scope', 'reserve', 'authorize']
    assert env.receiver.calls[0]['sourceRevision'] == 1
    assert not env.owned.indexes and not env.sessions.indexes


@pytest.mark.asyncio
async def test_ack_loss_replays_exact_event_not_unverified_lookup_and_session_update_is_irrelevant():
    env = environment()
    await save(env)
    env.receiver.fail_ack = True
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'retry_wait'
    env.sessions.documents['0']['updatedAt'] = 'later'
    env.sessions.documents['0']['messages'].append({'messageId': 'other', 'role': 'assistant', 'content': 'unrelated'})
    env.now[0] += 31
    await env.service.recover_one(env.identity)
    assert env.receiver.calls[0] == env.receiver.calls[1] and not env.receiver.lookups
    assert env.admission.calls == ['scope', 'reserve', 'authorize']
    env.receiver.status = 'applied'
    env.now[0] += 31
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'applied' and len(env.receiver.lookups) == 1
    assert env.sessions.documents['0']['chatProfileIngestion'][0]['status'] == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['content', 'timestamp', 'role', 'mode', 'consumer', 'digest', 'job'])
async def test_saved_source_change_fails_before_profile_dispatch(field):
    env = environment()
    await save(env)
    row = env.sessions.documents['0']
    if field in {'content', 'timestamp', 'role'}:
        row['messages'][0][field] = 'changed'
    else:
        key = {'mode': 'deliveryMode', 'consumer': 'consumerRevision', 'digest': 'draftDigest', 'job': 'jobId'}[field]
        row['chatProfileIngestion'][0][key] = 'changed'
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'failed' and not env.admission.calls


@pytest.mark.asyncio
async def test_duplicate_message_across_sessions_is_not_claimed():
    env = environment()
    await save(env)
    other = deepcopy(env.raw)
    other['sessionId'] = 'other'
    await env.sessions.insert_one(other)
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'failed' and not env.admission.calls


@pytest.mark.asyncio
async def test_budget_survives_crashes_and_expired_lease_then_requires_explicit_redrive():
    env = environment(ChatProfileDeliveryPolicy(max_attempts=2, lease_seconds=2))
    await save(env)
    for _ in range(2):
        assert await env.resources.store.claim(env.identity, now=env.now[0], owner='dead')
        env.now[0] += 3
    await env.service.recover_one(env.identity)
    row = await task(env)
    assert row['status'] == 'failed' and row['errorCode'] == 'chat_source_attempts_exhausted'
    assert not env.admission.calls
    await env.service.redrive_transport(env.identity, reason='operator retry', policy=ChatProfileDeliveryPolicy())
    await env.service.recover_one(env.identity)
    assert (await task(env))['attempts'] == 4 and (await task(env))['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_provider_failure_and_expiry_cannot_start_next_call():
    env = environment(ChatProfileDeliveryPolicy(max_age_seconds=10))
    await save(env)

    async def slow_scope(user_id):
        env.admission.calls.append('scope')
        env.now[0] += 11
        return {'userId': user_id, 'subjectId': 'self'}

    env.admission.prepare_self_scope = slow_scope
    await env.service.recover_one(env.identity)
    assert (await task(env))['errorCode'] == 'chat_source_deadline_exceeded'
    assert env.admission.calls == ['scope'] and not env.receiver.calls


@pytest.mark.asyncio
async def test_stale_owner_cannot_save_failure_or_release_new_owner():
    env = environment(ChatProfileDeliveryPolicy(lease_seconds=2))
    await save(env)

    async def replaced_owner(user_id):
        env.now[0] += 3
        await env.resources.store.claim(env.identity, now=env.now[0], owner='new-worker')
        raise RuntimeError('late remote failure')

    env.admission.prepare_self_scope = replaced_owner
    await env.service.recover_one(env.identity)
    row = await task(env)
    assert row['leaseOwner'] == 'new-worker' and row['status'] == 'pending' and row['attempts'] == 2


@pytest.mark.asyncio
async def test_cancelled_external_work_consumes_attempt_and_releases_only_its_lease():
    env = environment()
    await save(env)
    env.admission.prepare_self_scope = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await env.service.recover_one(env.identity)
    row = await task(env)
    assert row['attempts'] == 1 and row['leaseOwner'] is None and not env.receiver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['failed', 'partial'])
async def test_profile_terminal_receipt_cannot_be_transport_redriven(status):
    env = environment()
    await save(env)
    env.receiver.status = status
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == status
    with pytest.raises(ValueError):
        await env.service.redrive_transport(env.identity, reason='wrong governance', policy=ChatProfileDeliveryPolicy())


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [[], [{'userId': 'u', 'sessionId': 's', 'messageId': 'm'}]])
async def test_discovery_uses_pymongo_async_cursor_contract(rows):
    collection = create_autospec(AsyncCollection, instance=True, spec_set=True)
    cursor = create_autospec(AsyncCommandCursor, instance=True, spec_set=True)
    collection.aggregate.return_value = cursor
    cursor.to_list.return_value = rows

    result = await MongoChatSavedSourceReader(collection).discover(after=None, limit=2)

    assert result == rows
    collection.aggregate.assert_awaited_once()
    assert collection.aggregate.await_args.kwargs['maxTimeMS'] == 5000
    assert collection.aggregate.await_args.kwargs['collation'].document == {'locale': 'simple'}
    cursor.to_list.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_discovery_advances_past_invalid_and_existing_pending_markers_and_wraps():
    env = environment()
    for index in range(5):
        raw, message = source()
        raw.update(type='chat', sessionId=f's{index}')
        message['messageId'] = f'm{index}'
        prepare_user_message_source(raw, message, now=datetime.fromtimestamp(env.now[0], UTC))
        if index == 0:
            raw['chatProfileIngestion'][0]['consumerRevision'] = 'future'
        await env.sessions.insert_one(raw)
    env.admission.fail = 'scope'
    for _ in range(3):
        await env.service.recover_pending(limit=2)
    tasks = [row for row in env.owned.documents.values() if row.get('recordKind') == 'task']
    assert {row['messageId'] for row in tasks} == {'m1', 'm2', 'm3', 'm4'}
    assert len([row for row in env.owned.documents.values() if row.get('recordKind') == 'discovery_error']) == 1
    # New message sorts before the last observed session, and is found next cycle.
    row = env.sessions.documents['0']
    new = {**deepcopy(row['messages'][0]), 'messageId': 'new'}
    row['messages'].append(new)
    prepare_user_message_source(row, new, now=datetime.fromtimestamp(env.now[0], UTC))
    await env.service.recover_pending(limit=2)
    assert await env.resources.store.get(source_task_id('u', 'new')) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [None, 'legacy', 'future', 'profile-source-event.v1'])
async def test_router_never_sends_unknown_or_new_mode_to_legacy(mode):
    env = environment()
    raw = deepcopy(env.raw)
    raw['chatProfileIngestion'][0]['deliveryMode'] = mode
    await env.sessions.insert_one(raw)
    legacy = SimpleNamespace(ensure_queued=AsyncMock(), prepare_user_message=lambda *args: None)
    router = ChatProfileSourceRouter(publication=env.service, owner=env.resources.owner, legacy=legacy)
    await router.ensure_queued(user_id='u', message_id='m')
    assert legacy.ensure_queued.await_count == int(mode in (None, 'legacy'))
    assert not env.admission.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_actual_chat_business_save_succeeds_without_any_profile_call(stream):
    from tests.unit.services.chat.test_chat_service_agent import ChatSessionManagerAgentTest
    from tests.unit.services.chat.test_chat_service_agent import FakeSessionCollection

    env = environment()
    sessions = FakeSessionCollection()
    owner = SimpleNamespace(read_session=AsyncMock(side_effect=lambda **kwargs: deepcopy(sessions.document)))

    async def read_source(**kwargs):
        from adapters.chat.source_store import saved_binding

        return saved_binding(await owner.read_session(**kwargs), kwargs['message_id'])

    owner.read_source = read_source
    env.service.owner = owner
    env.admission.prepare_self_scope = AsyncMock(side_effect=AssertionError('Profile must remain background'))
    router = ChatProfileSourceRouter(
        publication=env.service, owner=owner, legacy=SimpleNamespace(ensure_queued=AsyncMock())
    )
    manager, *_ = ChatSessionManagerAgentTest()._manager(sessions, chat_profile_source=router)
    if stream:
        events = [event async for event in manager.sendMessageStream('user-1', 'session-1', 'raw message')]
        assert events[-1]['data']['type'] == 'finish'
    else:
        await manager.sendMessage('user-1', 'session-1', 'raw message')
    message = sessions.document['messages'][0]
    assert sessions.document['chatProfileIngestion'][0]['deliveryMode'] == 'profile-source-event.v1'
    assert await env.resources.store.get(source_task_id('user-1', message['messageId']))
    env.admission.prepare_self_scope.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_registration_outage_degrades_then_scanner_repairs_saved_source(monkeypatch):
    env = environment()
    await env.sessions.insert_one(env.raw)
    original = env.resources.store.insert
    env.resources.store.insert = AsyncMock(side_effect=RuntimeError('local task storage temporarily unavailable'))
    assert await env.service.ensure_queued(user_id='u', message_id='m') is None
    assert not env.admission.calls and not env.owned.documents
    env.resources.store.insert = original
    assert await env.service.recover_pending() == 1
    assert (await task(env))['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_terminal_budget_is_not_extended_by_restarting_with_new_defaults():
    env = environment(ChatProfileDeliveryPolicy(max_attempts=2))
    await save(env)
    env.admission.fail = 'scope'
    await env.service.recover_one(env.identity)
    env.now[0] += 31
    await env.service.recover_one(env.identity)
    assert (await task(env))['status'] == 'failed' and (await task(env))['attempts'] == 2
    assert await env.service.recover_one(env.identity) is None
    assert len(env.admission.calls) == 2


@pytest.mark.asyncio
async def test_duplicate_discovery_cas_cannot_regress_cursor():
    env = environment()
    first = await env.resources.store.scan_state()
    page = {'userId': 'u', 'sessionId': 's', 'messageId': 'm'}
    assert await env.resources.store.advance_scan(first, page)
    assert not await env.resources.store.advance_scan(first, None)
    assert (await env.resources.store.scan_state())['after'] == page
