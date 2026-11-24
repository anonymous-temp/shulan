"""Offline lifecycle contracts: no models, Redis, Mongo or private environment."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireExpiredError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from modules.questionnaire.schemas import QuestionnaireValidationError
from modules.questionnaire.schemas import normalize_answers
from modules.questionnaire.schemas import validate_questionnaire
from modules.questionnaire.service import QuestionnaireService

SURVEY = {
    'title': 'Health questionnaire',
    'pages': [
        {
            'name': 'main',
            'elements': [
                {'name': 'note', 'type': 'text', 'title': 'Note', 'isRequired': True},
                {
                    'name': 'choice',
                    'type': 'radiogroup',
                    'title': 'Choice',
                    'choices': [{'value': 'yes'}, {'value': 'no'}],
                },
                {
                    'name': 'habits',
                    'type': 'checkbox',
                    'title': 'Habits',
                    'choices': [{'value': 'a'}, {'value': 'b'}],
                    'showNoneItem': True,
                },
            ],
        }
    ],
}


class MemoryStore:
    def __init__(self):
        self.docs = {}
        self.fail_submission_once = False

    async def insert(self, snapshot):
        key = (snapshot['userId'], snapshot['questionnaireId'])
        if key in self.docs:
            return False
        self.docs[key] = deepcopy(snapshot)
        return True

    async def save(self, snapshot, *, expected_revision):
        key = (snapshot['userId'], snapshot['questionnaireId'])
        if self.fail_submission_once and snapshot['status'] == 'submitted':
            self.fail_submission_once = False
            raise RuntimeError('simulated acknowledgement failure before write')
        if self.docs[key]['revision'] != expected_revision:
            return False
        self.docs[key] = deepcopy(snapshot)
        return True

    async def get_by_id(self, user_id, questionnaire_id):
        return deepcopy(self.docs.get((user_id, questionnaire_id)))

    async def get_current(self, user_id, session_id):
        candidates = [doc for doc in self.docs.values() if doc['userId'] == user_id and doc['sessionId'] == session_id]
        return deepcopy(candidates[-1]) if candidates else None

    async def get_submitted(self, user_id, questionnaire_id):
        snapshot = await self.get_by_id(user_id, questionnaire_id)
        return snapshot if snapshot and snapshot['status'] == 'submitted' else None

    async def list_recoverable(self, **kwargs):
        return [deepcopy(doc) for doc in self.docs.values() if doc['status'] != 'submitted']

    async def list_pending_ingestion(self, **kwargs):
        return [
            deepcopy(doc)
            for doc in self.docs.values()
            if doc['status'] == 'submitted' and doc['profileIngestion']['status'] == 'pending'
        ]

    async def mark_ingestion_queued(self, user_id, questionnaire_id, job_id):
        doc = self.docs[(user_id, questionnaire_id)]
        doc['profileIngestion'].update(status='queued', jobId=job_id)
        return True


class Leases:
    def __init__(self):
        self.owned = {}

    async def try_acquire(self, key, owner):
        if key in self.owned:
            return None
        lease = SimpleNamespace(key=key, owner=owner, ttl_seconds=300)
        self.owned[key] = lease
        return lease

    async def renew(self, lease):
        return self.owned.get(lease.key) is lease

    async def release(self, lease):
        if self.owned.get(lease.key) is lease:
            self.owned.pop(lease.key)
            return True
        return False


class Sessions:
    def __init__(self):
        self.docs = {}

    async def get(self, user_id, session_id):
        return deepcopy(self.docs.get((user_id, session_id)))

    async def save(self, user_id, session_id, snapshot):
        self.docs[(user_id, session_id)] = deepcopy(snapshot)
        return True

    async def save_questionnaire_owner(self, user_id, session_id, snapshot, *, expected):
        current = self.docs.get((user_id, session_id))
        if expected is None:
            if current is not None:
                return False
        elif (
            current is None
            or any(current.get(field) != expected.get(field) for field in ('type', 'questionnaireId', 'ownerRevision'))
            or current.get('type') != 'questionnaire'
        ):
            return False
        self.docs[(user_id, session_id)] = deepcopy(snapshot)
        return True


@pytest.fixture
def harness():
    store, sessions, leases = MemoryStore(), Sessions(), Leases()
    generator = SimpleNamespace(
        generate=AsyncMock(
            return_value={
                'questionnaire': SURVEY,
                'reports': [],
                'guidance': {'clientCheckupMessage': 'Source-only guidance'},
                'provenance': {'questionSources': {}},
                'artifacts': [],
                'mode': 'basic',
                'processing': [],
            }
        ),
        pin_artifacts=AsyncMock(),
        expire_artifacts=AsyncMock(),
        ready_artifacts=AsyncMock(),
    )
    now = [100000.0]
    delivery = AsyncMock()
    service = QuestionnaireService(
        store=store,
        generator=generator,
        lease_manager=leases,
        session_repository=sessions,
        profile_delivery=delivery,
        clock=lambda: now[0],
    )
    return SimpleNamespace(
        service=service,
        store=store,
        sessions=sessions,
        leases=leases,
        generator=generator,
        now=now,
        delivery=delivery,
    )


async def ready(h):
    created = await h.service.create('user', 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    snapshot = await h.service.get('user', 'session')
    assert snapshot['status'] == 'ready'
    assert snapshot['questionnaireId'] == created['questionnaireId']
    return snapshot


@pytest.mark.asyncio
async def test_generation_is_durable_but_not_a_completed_session(harness):
    h = harness
    result = await ready(h)
    assert not result['canChat']
    assert result['mode'] == 'basic'
    assert await h.service.hasPendingSession('user', 'session')
    assert not await h.service.hasPendingSession('user', 'other-session')
    assert h.sessions.docs[('user', 'session')]['type'] == 'questionnaire'
    assert h.sessions.docs[('user', 'session')]['status'] == 'active'
    assert 'questionnaire' not in h.sessions.docs[('user', 'session')]
    h.delivery.assert_not_awaited()
    h.generator.pin_artifacts.assert_not_awaited()
    h.generator.ready_artifacts.assert_awaited_once()
    assert h.generator.ready_artifacts.call_args.args[1] == h.now[0] + 7200
    replay = await h.service.create('user', 'session')
    assert replay['questionnaireId'] == result['questionnaireId']
    h.generator.generate.assert_awaited_once()
    with pytest.raises(QuestionnaireConflictError):
        await h.service.create('user', 'session', [{'url': 'https://example.invalid/report.pdf'}])


@pytest.mark.asyncio
async def test_unknown_answers_complete_without_fabricating_facts(harness):
    h = harness
    result = await ready(h)
    submitted = await h.service.submit('user', result['questionnaireId'], {'note': '  ', 'choice': None})
    assert submitted['status'] == 'submitted'
    assert submitted['canChat']
    assert submitted['answers'] == {}
    assert not await h.service.hasPendingSession('user', 'session')
    doc = await h.store.get_submitted('user', result['questionnaireId'])
    assert doc['rawAnswers'] == {'note': '  ', 'choice': None}
    assert doc['answers']['note']['rawValue'] == '  '
    assert doc['answers']['note']['provided']
    assert doc['answers']['choice']['status'] == 'unknown'
    assert not doc['answers']['habits']['provided']
    assert doc['sourceRevision'] == 1
    assert doc['submissionId'] == doc['questionnaireId']
    assert doc['profileIngestion']['status'] == 'pending'
    assert h.sessions.docs[('user', 'session')]['status'] == 'completed'
    h.delivery.assert_awaited_once_with(user_id='user', questionnaire_id=result['questionnaireId'])


@pytest.mark.asyncio
async def test_raw_snapshot_and_receipt_survive_chat_conversion(harness):
    h = harness
    result = await ready(h)
    raw = {'note': '  original note  ', 'habits': ['b', 'a', 'b']}
    receipt = await h.service.submit('user', result['questionnaireId'], raw)
    assert receipt['answers'] == {'note': 'original note', 'habits': ['a', 'b']}
    h.sessions.docs[('user', 'session')] = {'type': 'chat', 'status': 'active', 'messages': ['hello']}
    assert await h.service.submit('user', result['questionnaireId'], raw) == receipt
    assert (await h.service.get('user', 'session'))['answers'] == receipt['answers']
    assert (await h.service.create('user', 'session'))['status'] == 'submitted'
    assert h.sessions.docs[('user', 'session')]['messages'] == ['hello']
    assert (await h.store.get_submitted('user', result['questionnaireId']))['rawAnswers'] == raw
    h.generator.pin_artifacts.assert_awaited_once()
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'note': 'changed'})


@pytest.mark.asyncio
async def test_ready_expiry_does_not_release_owner_or_extend_on_reads(harness):
    h = harness
    result = await ready(h)
    h.now[0] += 7199
    assert (await h.service.get('user', 'session'))['expiresAt'] == result['expiresAt']
    h.now[0] += 1
    expired = await h.service.get('user', 'session')
    assert expired['status'] == 'expired'
    assert 'questionnaire' not in expired
    assert await h.service.hasPendingSession('user', 'session')
    assert await h.service.hasSession('user', 'session')
    with pytest.raises(QuestionnaireExpiredError):
        await h.service.submit('user', result['questionnaireId'], {})
    h.generator.expire_artifacts.assert_awaited_once()
    regenerated = await h.service.create('user', 'session')
    assert regenerated['questionnaireId'] != result['questionnaireId']
    await asyncio.gather(*list(h.service._tasks.values()))
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {})
    assert (await h.store.get_by_id('user', result['questionnaireId']))['status'] == 'expired'


@pytest.mark.asyncio
async def test_wrong_user_is_not_found(harness):
    result = await ready(harness)
    with pytest.raises(QuestionnaireNotFoundError):
        await harness.service.submit('other-user', result['questionnaireId'], {})
    with pytest.raises(QuestionnaireNotFoundError):
        await harness.service.get('other-user', 'session')


@pytest.mark.asyncio
async def test_persisted_receipt_recovers_pin_failure_after_ready_deadline(harness):
    h = harness
    result = await ready(h)
    h.generator.pin_artifacts.side_effect = [RuntimeError('storage unavailable'), None]
    with pytest.raises(RuntimeError):
        await h.service.submit('user', result['questionnaireId'], {'choice': 'yes'})
    assert await h.service.hasPendingSession('user', 'session')
    public = await h.service.get('user', 'session')
    assert public['submissionPending'] and public['retryAfterSeconds'] == 3
    assert not public['canChat']
    assert (await h.store.get_by_id('user', result['questionnaireId']))['pendingSubmission']['rawAnswers']
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'choice': 'no'})
    h.now[0] += 9000
    assert await h.service.recover_pending() == 1
    assert (await h.service.get('user', 'session'))['status'] == 'submitted'
    h.generator.expire_artifacts.assert_not_awaited()
    assert h.generator.pin_artifacts.await_count == 2


@pytest.mark.asyncio
async def test_submission_database_failure_keeps_accepted_raw_answers(harness):
    h = harness
    result = await ready(h)
    h.store.fail_submission_once = True
    with pytest.raises(RuntimeError):
        await h.service.submit('user', result['questionnaireId'], {'habits': ['none']})
    saved = await h.store.get_by_id('user', result['questionnaireId'])
    assert saved['status'] == 'ready'
    assert saved['pendingSubmission']['rawAnswers'] == {'habits': ['none']}
    assert not h.delivery.await_count
    await h.service.recover_pending()
    assert (await h.service.get('user', 'session'))['answers'] == {'habits': ['none']}


@pytest.mark.asyncio
async def test_profile_failure_is_compensated_without_reopening_submission(harness):
    h = harness
    result = await ready(h)
    h.delivery.side_effect = RuntimeError('queue unavailable')
    receipt = await h.service.submit('user', result['questionnaireId'], {})
    assert receipt['status'] == 'submitted'
    assert (await h.store.get_submitted('user', result['questionnaireId']))['profileIngestion']['status'] == 'pending'
    h.delivery.side_effect = None
    await h.service.recover_pending()
    assert h.delivery.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['qa', 'chat'])
async def test_creation_rejects_existing_shared_session(harness, kind):
    h = harness
    h.sessions.docs[('user', 'session')] = {'type': kind, 'status': 'active'}
    with pytest.raises(QuestionnaireConflictError):
        await h.service.create('user', 'session')
    assert not h.store.docs


@pytest.mark.asyncio
async def test_creation_checks_legacy_redis_session_before_claiming(harness):
    h = harness
    h.service.legacy_session_cache = SimpleNamespace(get=AsyncMock(return_value={'type': 'qa'}))
    with pytest.raises(QuestionnaireConflictError):
        await h.service.create('user', 'session')
    h.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_generation_is_closed_and_recoverable(harness):
    h = harness
    h.generator.generate.side_effect = [RuntimeError('provider failed'), h.generator.generate.return_value]
    created = await h.service.create('user', 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    failed = await h.service.get('user', 'session')
    assert failed['status'] == 'failed'
    assert failed['retryable']
    assert failed['retryAfterSeconds'] == 15
    assert await h.service.hasPendingSession('user', 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    h.now[0] += 15
    await h.service.recover_pending()
    await asyncio.gather(*list(h.service._tasks.values()))
    assert (await h.service.get('user', 'session'))['questionnaireId'] == created['questionnaireId']
    assert (await h.service.get('user', 'session'))['status'] == 'ready'


@pytest.mark.parametrize('value', [False, 0, {}, ['wrong-type']])
def test_false_zero_and_wrong_types_are_not_unknown(value):
    with pytest.raises(QuestionnaireValidationError):
        normalize_answers(SURVEY, {'note': value})


@pytest.mark.parametrize(
    'answers',
    [
        {'extra': 'unrecognized'},
        {'choice': 'unknown-value'},
        {'habits': ['none', 'a']},
        {'habits': [False]},
        {'habits': 'a'},
        [],
    ],
)
def test_answer_shape_and_frozen_option_constraints(answers):
    with pytest.raises(QuestionnaireValidationError):
        normalize_answers(SURVEY, answers)


def test_questionnaire_duplicate_names_and_nonstandard_fields_are_rejected():
    duplicate = deepcopy(SURVEY)
    duplicate['pages'][0]['elements'].append(deepcopy(duplicate['pages'][0]['elements'][0]))
    with pytest.raises(QuestionnaireValidationError):
        validate_questionnaire(duplicate)
    extra = deepcopy(SURVEY)
    extra['pages'][0]['elements'][0]['evidenceRefs'] = ['internal']
    with pytest.raises(QuestionnaireValidationError):
        validate_questionnaire(extra)
    assert validate_questionnaire(SURVEY) == SURVEY


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['dev', 'test', 'production'])
async def test_generation_and_submission_ignore_obsolete_environment_switch(harness, monkeypatch, environment):
    monkeypatch.setenv('SHULAN_ENVIRONMENT', environment)
    monkeypatch.setenv('SHULAN_QUESTIONNAIRE_ENABLED', 'false')
    h = harness
    result = await ready(h)
    assert await h.service.hasPendingSession('user', 'session')
    assert (await h.service.get('user', 'session'))['status'] == 'ready'
    assert (await h.service.submit('user', result['questionnaireId'], {}))['canChat']


@pytest.mark.asyncio
async def test_regeneration_repairs_owner_marker_after_partial_creation_failure(harness):
    h = harness
    first = await ready(h)
    h.now[0] += 7200
    await h.service.get('user', 'session')
    original_save = h.sessions.save_questionnaire_owner
    h.sessions.save_questionnaire_owner = AsyncMock(return_value=False)
    with pytest.raises(QuestionnaireConflictError):
        await h.service.create('user', 'session')
    second = await h.store.get_current('user', 'session')
    assert second['questionnaireId'] != first['questionnaireId']
    assert second['status'] == 'queued'
    assert await h.service.hasPendingSession('user', 'session')
    h.sessions.save_questionnaire_owner = original_save
    await h.service.recover_pending()
    await asyncio.gather(*list(h.service._tasks.values()))
    assert (await h.service.get('user', 'session'))['status'] == 'ready'
    assert h.sessions.docs[('user', 'session')]['questionnaireId'] == second['questionnaireId']


@pytest.mark.asyncio
async def test_invalid_submission_shape_is_400_equivalent_even_after_completion(harness):
    h = harness
    result = await ready(h)
    await h.service.submit('user', result['questionnaireId'], {})
    with pytest.raises(QuestionnaireValidationError):
        await h.service.submit('user', result['questionnaireId'], {'note': False})


@pytest.mark.asyncio
async def test_public_guidance_projection_keeps_evidence_internal(harness):
    h = harness
    internal = {
        'reportSummary': 'summary',
        'clientCheckupMessage': 'message',
        'managerInterview': [
            {
                'question': 'question',
                'reason': 'reason',
                'informationFacet': 'facet',
                'condition': 'internal condition',
                'evidenceRefs': ['e1'],
            }
        ],
        'riskAssessment': [{'priority': 'routine', 'assessment': 'assessment', 'evidenceRefs': ['e1']}],
        'suggestedTests': [{'name': 'test', 'reason': 'reason', 'evidenceRefs': ['e1']}],
    }
    h.generator.generate.return_value['guidance'] = internal
    result = await ready(h)
    assert result['guidance']['managerInterview'] == [{'question': 'question', 'reason': 'reason'}]
    assert result['guidance']['riskAssessment'] == [{'priority': 'routine', 'assessment': 'assessment'}]
    assert result['guidance']['suggestedTests'] == [{'name': 'test', 'reason': 'reason'}]
    snapshot = await h.store.get_by_id('user', result['questionnaireId'])
    assert snapshot['guidance'] == internal
    receipt = await h.service.submit('user', result['questionnaireId'], {})
    assert receipt['guidance'] == result['guidance']
    assert (await h.store.get_submitted('user', result['questionnaireId']))['guidance'] == internal


@pytest.mark.asyncio
async def test_equivalent_canonical_answers_replay_original_receipt_without_rewriting_raw(harness):
    h = harness
    result = await ready(h)
    raw = {'note': '  original note  ', 'habits': ['b', 'a', 'b'], 'choice': None}
    first = await h.service.submit('user', result['questionnaireId'], raw)
    before = await h.store.get_submitted('user', result['questionnaireId'])
    equivalent = {'note': 'original note', 'habits': ['a', 'b']}
    replay = await h.service.submit('user', result['questionnaireId'], equivalent)
    after = await h.store.get_submitted('user', result['questionnaireId'])
    assert replay == first
    assert after == before
    assert after['rawAnswers'] == raw
    assert after['answers']['note']['rawValue'] == '  original note  '
    assert after['answers']['choice']['provided']
    assert after['answerHash'] != after['rawAnswerHash']
    h.generator.pin_artifacts.assert_awaited_once()
    h.delivery.assert_awaited_once()
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'note': 'different note', 'habits': ['a', 'b']})
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'note': 'original note', 'habits': ['b']})


@pytest.mark.asyncio
async def test_missing_null_blank_and_empty_array_answers_are_idempotently_equivalent(harness):
    h = harness
    result = await ready(h)
    first_raw = {'note': None, 'habits': [], 'choice': '   '}
    first = await h.service.submit('user', result['questionnaireId'], first_raw)
    for equivalent in ({}, {'note': ''}, {'note': '  ', 'choice': None}, {'habits': []}):
        assert await h.service.submit('user', result['questionnaireId'], equivalent) == first
    saved = await h.store.get_submitted('user', result['questionnaireId'])
    assert saved['rawAnswers'] == first_raw
    assert saved['canonicalAnswers'] == {}
    assert saved['answers']['note']['provided']
    h.generator.pin_artifacts.assert_awaited_once()
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'habits': ['none']})


@pytest.mark.asyncio
async def test_pending_submission_resumes_with_equivalent_canonical_input_and_original_raw(harness):
    h = harness
    result = await ready(h)
    raw = {'note': '  kept first  ', 'habits': ['b', 'a', 'b'], 'choice': None}
    h.generator.pin_artifacts.side_effect = [RuntimeError('temporarily unavailable'), None]
    with pytest.raises(RuntimeError):
        await h.service.submit('user', result['questionnaireId'], raw)
    pending = await h.store.get_by_id('user', result['questionnaireId'])
    first_receipt = deepcopy(pending['pendingSubmission'])
    with pytest.raises(QuestionnaireConflictError):
        await h.service.submit('user', result['questionnaireId'], {'note': 'changed', 'habits': ['a', 'b']})
    resumed = await h.service.submit(
        'user',
        result['questionnaireId'],
        {'note': 'kept first', 'habits': ['a', 'b']},
    )
    saved = await h.store.get_submitted('user', result['questionnaireId'])
    assert resumed['status'] == 'submitted'
    assert resumed['answers'] == {'note': 'kept first', 'habits': ['a', 'b']}
    assert saved['rawAnswers'] == raw
    assert saved['rawAnswerHash'] == first_receipt['rawAnswerHash']
    assert saved['answerHash'] == first_receipt['answerHash']
    assert saved['submittedAt'] == first_receipt['acceptedAt']
    assert saved['answers'] == first_receipt['answers']
    assert h.generator.pin_artifacts.await_count == 2
    h.delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_identical_create_reuses_generating_instance_without_contending_with_worker_lease(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    generated = deepcopy(h.generator.generate.return_value)

    async def slow_generation(**kwargs):
        entered.set()
        await release.wait()
        return generated

    h.generator.generate.side_effect = slow_generation
    first = await h.service.create('user', 'session')
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        assert h.leases.owned
        replay = await h.service.create('user', 'session', [])
        assert replay['questionnaireId'] == first['questionnaireId']
        assert replay['status'] == 'generating'
        assert not replay['canChat']
        with pytest.raises(QuestionnaireConflictError, match='different generation inputs'):
            await h.service.create('user', 'session', [{'url': 'https://example.invalid/different.pdf'}])
        assert h.generator.generate.await_count == 1
    finally:
        release.set()
        await asyncio.gather(*list(h.service._tasks.values()))
    assert (await h.service.get('user', 'session'))['status'] == 'ready'


@pytest.mark.asyncio
async def test_submit_rechecks_lease_after_delayed_owner_read_without_overwriting_chat(harness):
    h = harness
    result = await ready(h)
    entered, release = asyncio.Event(), asyncio.Event()
    original_get = h.sessions.get

    async def delayed_get(user_id, session_id):
        stale = await original_get(user_id, session_id)
        stored = await h.store.get_current(user_id, session_id)
        if stored['status'] == 'submitted' and not entered.is_set():
            entered.set()
            await release.wait()
        return stale

    h.sessions.get = delayed_get
    task = asyncio.create_task(h.service.submit('user', result['questionnaireId'], {}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    h.leases.owned.clear()
    chat = {
        'type': 'chat',
        'status': 'active',
        'messages': [{'role': 'user', 'content': 'Must remain after takeover'}],
    }
    h.sessions.docs[('user', 'session')] = deepcopy(chat)
    release.set()
    with pytest.raises(QuestionnaireConflictError):
        await task
    assert h.sessions.docs[('user', 'session')] == chat
    submitted = await h.store.get_submitted('user', result['questionnaireId'])
    assert submitted['status'] == 'submitted'
    # The user can recover the original durable receipt after losing ownership.
    assert (await h.service.submit('user', result['questionnaireId'], {}))['status'] == 'submitted'
    assert h.sessions.docs[('user', 'session')] == chat


@pytest.mark.asyncio
async def test_owner_cas_survives_lease_loss_during_final_database_write(harness):
    h = harness
    result = await ready(h)
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = h.sessions.save_questionnaire_owner

    async def delayed_save(user_id, session_id, marker, *, expected):
        if marker['status'] == 'completed':
            entered.set()
            await release.wait()
        return await original_save(user_id, session_id, marker, expected=expected)

    h.sessions.save_questionnaire_owner = delayed_save
    task = asyncio.create_task(h.service.submit('user', result['questionnaireId'], {}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    # The final guard already passed. Only a storage-side condition can protect
    # against takeover while the actual write is still awaiting the database.
    h.leases.owned.clear()
    chat = {
        'type': 'chat',
        'status': 'active',
        'messages': [{'role': 'assistant', 'content': 'Persisted by the newer Chat owner'}],
    }
    h.sessions.docs[('user', 'session')] = deepcopy(chat)
    release.set()
    receipt = await task
    assert receipt['status'] == 'submitted'
    assert h.sessions.docs[('user', 'session')] == chat
    assert (await h.store.get_submitted('user', result['questionnaireId']))['status'] == 'submitted'


@pytest.mark.asyncio
async def test_operational_events_contain_counts_and_ids_but_no_health_payloads(harness, monkeypatch):
    from unittest.mock import Mock

    from modules.questionnaire import service as service_module

    h = harness
    marker = 'PRIVATE_HEALTH_TEXT_MUST_NOT_BE_LOGGED'
    info = Mock()
    monkeypatch.setattr(service_module.logger, 'info', info)
    h.generator.generate.return_value.update(
        {
            'reports': [{'status': 'complete', 'interpretation': {'sourceText': marker}}],
            'guidance': {'clientCheckupMessage': marker},
            'processing': {'reason': marker},
            'mode': marker,
        }
    )
    result = await ready(h)
    h.generator.pin_artifacts.side_effect = [RuntimeError('temporary failure'), None]
    with pytest.raises(RuntimeError):
        await h.service.submit('user', result['questionnaireId'], {'note': marker})
    await h.service.recover_pending()
    messages = [call.args[0] % call.args[1:] for call in info.call_args_list]
    assert any(
        'questionnaire_event=ready' in item
        and 'report_count=1' in item
        and 'usable_report_count=1' in item
        and 'generation_elapsed_ms=' in item
        for item in messages
    )
    assert any('questionnaire_event=submitted' in item and 'source_revision=1' in item for item in messages)
    assert any('questionnaire_event=recovery' in item and 'submissions_completed=1' in item for item in messages)
    assert marker not in '\n'.join(messages)
    assert all('https://' not in item for item in messages)
