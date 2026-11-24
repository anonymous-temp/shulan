"""Multi-source immutable evidence, fixed extraction, and cross-subject recovery."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agents import GraphRegistry
from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import ProfileSourceSnapshotRefV1
from health_profile.contracts import profile_content_digest
from health_profile.ingestion import FactTypeRegistry
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import HealthProfileJobQueue
from health_profile.models import AssertionLevel
from health_profile.models import FactStatus
from health_profile.models import ProfileIngestionSourceType
from health_profile.models import VerificationStatus
from health_profile.source_contracts import ProfileAnswerEvidenceV1
from health_profile.source_contracts import ProfileChatSourceSnapshotV1
from health_profile.source_contracts import ProfileQuestionnaireSourceSnapshotV1
from health_profile.source_contracts import ProfileRecordSourceSnapshotV1
from health_profile.source_contracts import ProfileUserInputFieldV1
from health_profile.source_extraction import ANSWER_EXTRACTION
from health_profile.source_extraction import DurableProfileSourceExtractor
from health_profile.source_extraction import ProfileUserSourceResolver
from health_profile.source_material import retraction_snapshot
from health_profile.source_material import retraction_snapshot_id
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import ProfileSourceInbox
from health_profile.source_publications import publication_id
from health_profile.worker import ProfileIngestionProcessorRouter

from .test_chat_ingestion import _runtime as chat_runtime
from .test_profile_read_and_subjects import FakeCollection
from .test_profile_read_and_subjects import FakeCursor
from .test_profile_read_and_subjects import _matches
from .test_public_contracts import setup_flow
from .test_record_ingestion import _runtime as answer_runtime


class PublicationCollection(FakeCollection):
    """Add Mongo array membership semantics needed by affected-subject reads."""

    def find(self, query):
        if '$or' not in query or not any('affectedSubjectIds' in branch for branch in query['$or']):
            return super().find(query)
        wanted = query['$or'][1]['affectedSubjectIds']['$in']
        base = {key: value for key, value in query.items() if key != '$or'}
        return FakeCursor(
            [
                row
                for row in self.documents
                if _matches(row, base)
                and (
                    _matches(row, query['$or'][0])
                    or any(value in row.get('affectedSubjectIds', []) for value in wanted)
                )
            ]
        )


class PrototypeInbox(ProfileSourceInbox):
    """Offline-only fixture for the unfinished Record capability; never a public switch."""

    @staticmethod
    def _require_cutover_capability(source_type, material_contract_version=None):
        return None


SELF_TEXT = '我最近总是头痛'
FAMILY_TEXT = '父亲患有糖尿病'


def candidate(*, family=False, answer_index=None):
    row = {
        'factType': 'family_history.condition' if family else 'symptom.reported',
        'displayName': '糖尿病家族史' if family else '头痛',
        'valueType': 'text',
        'value': '糖尿病' if family else '反复头痛',
        'subjectKind': 'blood_relative' if family else 'self',
        'subjectHint': '父亲' if family else '本人',
        'evidenceText': FAMILY_TEXT if family else SELF_TEXT,
        'confidence': 0.96,
    }
    if answer_index is not None:
        row['answerIndex'] = answer_index
    return row


def answer(index=0, *, family=False):
    return ProfileAnswerEvidenceV1(
        answer_index=index,
        source_pointer=f'/answers/{index}',
        question_id=f'q{index}',
        question_type='text',
        question='您或家人有什么健康情况',
        answer=FAMILY_TEXT if family else SELF_TEXT,
        answered_at='2026-11-09T00:00:00Z',
    )


async def setup_source(kind, *, family=False, rows=None, outputs=None):
    flow = await setup_flow(authorize=False)
    flow.collections['publications'].__class__ = PublicationCollection
    if kind == 'record':
        flow.inbox.__class__ = PrototypeInbox
    runtime, gateway = (chat_runtime if kind == 'chat' else answer_runtime)(
        {
            'candidates': outputs
            if outputs is not None
            else [candidate(family=family, answer_index=None if kind == 'chat' else 0)],
        }
    )
    source_id = 'message-1' if kind == 'chat' else 'source-1'
    payload = dict(scope=flow.scope, source_id=source_id, source_revision=1, session_id='session-1')
    if kind == 'chat':
        snapshot = ProfileChatSourceSnapshotV1(
            **payload, content=FAMILY_TEXT if family else SELF_TEXT, created_at=flow.now[0]
        )
    elif kind == 'record':
        snapshot = ProfileRecordSourceSnapshotV1(
            **payload,
            answers=tuple(rows or [answer(family=family)]),
            completed_at='2026-11-09T00:00:00Z',
            round_number=1,
        )
    else:
        snapshot = ProfileQuestionnaireSourceSnapshotV1(
            **payload, answers=tuple(rows or [answer(family=family)]), submitted_at='2026-11-09T00:00:00Z'
        )
    materials = {}
    reader = SimpleNamespace(
        read_source_snapshot=AsyncMock(side_effect=lambda ref, scope: deepcopy(materials[ref.snapshot_id]))
    )
    flow.applier.resolvers[(kind, snapshot.contract_version)] = ProfileUserSourceResolver(reader)
    flow.applier.extractor = DurableProfileSourceExtractor(runtime.hub, policy_version=HealthFactPolicy.VERSION)
    authorization = flow.authorization.model_copy(
        update={
            'source_type': kind,
            'source_id': source_id,
            'legacy_source_intents_retired': True,
        }
    )
    await flow.inbox.authorize_cutover(authorization, scope=flow.scope)
    flow.snapshot, flow.materials, flow.runtime, flow.gateway = snapshot, materials, runtime, gateway
    return flow


def event_for(flow, snapshot=None, *, revision=1, kind=None):
    snapshot = snapshot or flow.snapshot
    raw = snapshot.model_dump(mode='json', by_alias=True)
    digest = profile_content_digest(raw)
    ref = ProfileSourceSnapshotRefV1(
        provider=snapshot.source_type, contract_version=snapshot.contract_version, snapshot_id='raw_' + digest
    )
    flow.materials[ref.snapshot_id] = raw
    return ProfileSourceEventV1(
        event_id=f'{snapshot.source_type}-{revision}',
        scope=flow.scope,
        source_type=snapshot.source_type,
        source_id=snapshot.source_id,
        source_revision=snapshot.source_revision,
        publication_revision=revision,
        prior_revision=revision - 1 if revision > 1 else None,
        event_kind=kind or ('publish' if revision == 1 else 'correct'),
        occurred_at=flow.now[0],
        snapshot_ref=ref,
        content_digest=digest,
    )


def retract_event(flow, *, revision=2):
    event = event_for(flow, revision=revision, kind='retract')
    event = event.model_copy(
        update={
            'snapshot_ref': ProfileSourceSnapshotRefV1(
                provider='profile',
                contract_version='profile-source-retraction.v1',
                snapshot_id=retraction_snapshot_id(flow.scope, event.source_type, event.source_id, revision),
            )
        }
    )
    return event.model_copy(update={'content_digest': profile_content_digest(retraction_snapshot(event))})


async def source_facts(flow):
    result = []
    for subject in await flow.repository.list_subjects('u'):
        result.extend(
            await flow.repository.list_facts('u', subject.subject_id, statuses=(FactStatus.ACTIVE, FactStatus.PENDING))
        )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'record', 'questionnaire'])
async def test_public_raw_sources_reuse_existing_extractors_and_only_profile_governs(kind):
    flow = await setup_source(kind)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    facts = await source_facts(flow)
    assert len(facts) == 1 and facts[0].sources[0].source_type == kind
    assert facts[0].assertion_level is AssertionLevel.USER_REPORTED
    assert len(flow.gateway.complete_requests) == 1
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.processing_revision == f'profile-{kind}-source-v1'
    assert flow.scope.subject_id in receipt.profile_revisions


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'record', 'questionnaire'])
async def test_family_source_retraction_covers_saved_relative_targets(kind):
    flow = await setup_source(kind, family=True)
    await flow.inbox.receive(event_for(flow), scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    facts = await source_facts(flow)
    assert len(facts) == 1 and facts[0].subject_id != flow.scope.subject_id
    relative_id = facts[0].subject_id
    assert (
        relative_id in (await flow.inbox.get_watermark(flow.scope, kind, flow.snapshot.source_id)).affected_subject_ids
    )
    event = retract_event(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert await source_facts(flow) == []
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and relative_id in receipt.profile_revisions
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_model_output_survives_interruption_before_candidate_checkpoint(monkeypatch):
    flow = await setup_source('record')
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    save = flow.inbox.save_extracted_candidates
    failures = [True]

    async def fail_once(*args):
        if failures[0]:
            failures[0] = False
            raise RuntimeError('interrupted after model output was persisted')
        return await save(*args)

    monkeypatch.setattr(flow.inbox, 'save_extracted_candidates', fail_once)
    assert await flow.worker.recover_pending() == 0
    assert len(flow.gateway.complete_requests) == 1
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 1
    assert len(flow.gateway.complete_requests) == 1
    assert len(await source_facts(flow)) == 1


@pytest.mark.asyncio
async def test_correction_cannot_override_a_human_confirmed_relative():
    flow = await setup_source('questionnaire', family=True)
    await flow.inbox.receive(event_for(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    fact = (await source_facts(flow))[0]
    await flow.repository.replace_fact(
        fact.model_copy(
            update={
                'assertion_level': AssertionLevel.USER_CONFIRMED,
                'verification_status': VerificationStatus.CONFIRMED,
                'last_request_id': 'human-confirmed',
                'version': fact.version + 1,
            }
        ),
        expected_version=fact.version,
        allowed_statuses=(fact.status,),
    )
    event = event_for(flow, flow.snapshot.model_copy(update={'answers': ()}), revision=2)
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.manual_governance_requires_review'
    assert len(await source_facts(flow)) == 1 and len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_null_subject_legacy_jobs_block_cutover_and_later_router_execution():
    flow = await setup_flow(authorize=False)
    queue = HealthProfileJobQueue(flow.repository)
    queued = await queue.enqueue(
        source_type=ProfileIngestionSourceType.CHAT,
        source_id='source-1',
        source_revision=1,
        user_id='u',
        subject_id=None,
    )
    authorization = flow.authorization.model_copy(
        update={
            'source_type': 'chat',
            'source_id': 'source-1',
            'legacy_source_intents_retired': True,
        }
    )
    with pytest.raises(ProfileSourceConflictError, match='quiesce'):
        await flow.inbox.authorize_cutover(authorization, scope=flow.scope)
    processor = SimpleNamespace(process=AsyncMock(side_effect=AssertionError('old model must not run')))
    router = ProfileIngestionProcessorRouter(
        {ProfileIngestionSourceType.CHAT: processor}, source_guard=flow.repository.is_source_publication_managed
    )
    result = await router.process(queued.job)
    assert result.reason_codes == ('source.managed_by_publication',)
    processor.process.assert_not_awaited()
    assert await queue.is_source_publication_managed('u', None, 'chat', 'source-1')


@pytest.mark.asyncio
async def test_new_source_requires_explicit_retirement_of_legacy_delivery_intents():
    flow = await setup_flow(authorize=False)
    with pytest.raises(ProfileSourceConflictError, match='retire'):
        await flow.inbox.authorize_cutover(
            flow.authorization.model_copy(update={'source_type': 'chat'}), scope=flow.scope
        )


@pytest.mark.asyncio
async def test_material_scope_is_checked_before_model_calls():
    flow = await setup_source('chat')
    event = event_for(flow)
    flow.materials[event.snapshot_ref.snapshot_id]['scope']['userId'] = 'another-user'
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    assert not flow.gateway.complete_requests and not await source_facts(flow)
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'failed'


def test_public_source_contract_rejects_assistant_text_and_authoritative_fact_fields():
    scope = ProfileScope(user_id='u', subject_id='self')
    with pytest.raises(ValidationError):
        ProfileChatSourceSnapshotV1(
            scope=scope,
            source_id='m',
            source_revision=1,
            session_id='s',
            role='assistant',
            content='advice',
            created_at='2026-11-09T00:00:00Z',
        )
    with pytest.raises(ValidationError):
        ProfileAnswerEvidenceV1(
            answer_index=0, source_pointer='/x', question='q', answer='a', factType='condition.diagnosis'
        )
    with pytest.raises(ValidationError):
        ProfileUserInputFieldV1(field='height', value=175, provenance='model_output')


@pytest.mark.asyncio
async def test_record_v1_cutover_remains_hard_blocked_after_v2_support():
    flow = await setup_flow(authorize=False)
    authorization = flow.authorization.model_copy(
        update={
            'source_type': 'record',
            'legacy_source_intents_retired': True,
        }
    )
    with pytest.raises(ProfileSourceConflictError, match='source.record_cutover_not_ready'):
        await flow.inbox.authorize_cutover(authorization, scope=flow.scope)
    assert await flow.inbox.get_cutover(flow.scope, 'record', authorization.source_id) is None
    prototype = await setup_source('record')
    prototype.inbox.__class__ = ProfileSourceInbox
    with pytest.raises(ProfileSourceConflictError, match='source.record_cutover_not_ready'):
        await prototype.inbox.receive(event_for(prototype), scope=prototype.scope)


@pytest.mark.asyncio
async def test_failed_second_batch_reuses_durable_first_batch_on_recovery(monkeypatch):
    flow = await setup_source('questionnaire', rows=[answer(index) for index in range(13)], outputs=[])
    original = flow.gateway.complete
    attempts = []

    async def complete(request):
        attempts.append(request.content)
        if len(attempts) == 2:
            raise RuntimeError('temporary second-batch model failure')
        return await original(request)

    monkeypatch.setattr(flow.gateway, 'complete', complete)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'retry_wait'
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 1
    assert len(attempts) == 3 and attempts[1] == attempts[2] and attempts[0] != attempts[1]
    saved = await flow.inbox.store.get(publication_id(event))
    assert len(saved['extractionCalls']) == 2


@pytest.mark.asyncio
async def test_fixed_extraction_version_survives_changed_capability_alias():
    flow = await setup_source('questionnaire')
    selected = flow.runtime.registry.get(ANSWER_EXTRACTION.capability, selection=ANSWER_EXTRACTION)

    class Unexpected:
        async def ainvoke(self, *args, **kwargs):
            raise AssertionError('latest alias must not run')

    changed = GraphRegistry()
    changed.register(ANSWER_EXTRACTION.capability, Unexpected(), version='future')
    changed.register(ANSWER_EXTRACTION.capability, selected.graph, version=selected.version, legacy=False)
    flow.runtime.registry.publish(changed)
    await flow.inbox.receive(event_for(flow), scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_frozen_processing_revision_change_fails_without_replaying_model(monkeypatch):
    flow = await setup_source('chat')
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    monkeypatch.setattr(flow.inbox, 'save_extracted_candidates', AsyncMock(side_effect=RuntimeError('crash')))
    assert await flow.worker.recover_pending() == 0
    flow.applier.extractor.policy_version = 'unavailable-new-policy'
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.processing_version_unavailable'
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_relative_snapshot_waits_for_correction_then_empty_source_withdraws_old_target():
    flow = await setup_source('questionnaire', family=True)
    await flow.inbox.receive(event_for(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    relative_id = (await source_facts(flow))[0].subject_id
    ready = await flow.reader.read('u', subject_id=relative_id)
    assert ready.status == 'ready' and not ready.stale and len(ready.source_watermarks) == 1
    event = event_for(flow, flow.snapshot.model_copy(update={'answers': ()}), revision=2)
    await flow.inbox.receive(event, scope=flow.scope)
    pending = await flow.reader.read('u', subject_id=relative_id)
    assert pending.stale and pending.coverage['publicationPending']
    assert await flow.worker.recover_pending() == 1
    final = await flow.reader.read(
        'u', subject_id=relative_id, required_publications=(('questionnaire', event.source_id, 2),)
    )
    assert final.status == 'ready' and not final.stale and not final.facts
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_material_revision_cannot_regress_after_correction():
    flow = await setup_source('chat')
    await flow.inbox.receive(event_for(flow, flow.snapshot.model_copy(update={'source_revision': 2})), scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    event = event_for(flow, revision=2)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.error_code == 'source.material_revision_regressed'
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'questionnaire'])
async def test_lost_lease_after_model_return_does_not_escape_recovery_or_commit(monkeypatch, kind):
    flow = await setup_source(kind)
    original = flow.gateway.complete
    first = True

    async def expire_once(request):
        nonlocal first
        result = await original(request)
        if first:
            first = False
            flow.now[0] += timedelta(seconds=121)
        return result

    monkeypatch.setattr(flow.gateway, 'complete', expire_once)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'processing' and receipt.attempts == 1 and not receipt.error_code
    assert not await source_facts(flow)
    assert await flow.worker.recover_pending() == 1
    assert len(flow.gateway.complete_requests) == 2


@pytest.mark.asyncio
async def test_missing_exact_child_version_fails_explicitly_without_using_latest():
    flow = await setup_source('questionnaire')

    class Unselected:
        async def ainvoke(self, *args, **kwargs):
            raise AssertionError('must not fall back to another version')

    future = GraphRegistry()
    future.register(ANSWER_EXTRACTION.capability, Unselected(), version='future')
    flow.runtime.registry.publish(future)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.extraction_version_unavailable'
    assert not flow.gateway.complete_requests


@pytest.mark.asyncio
async def test_model_wait_is_cancelled_when_lease_heartbeat_loses_ownership(monkeypatch):
    flow = await setup_source('chat')
    flow.worker.lease_seconds = 1
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_forever(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(flow.gateway, 'complete', wait_forever)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    recovery = asyncio.create_task(flow.worker.recover_pending())
    await asyncio.wait_for(started.wait(), timeout=3)
    flow.now[0] += timedelta(seconds=2)
    assert await asyncio.wait_for(recovery, timeout=3) == 0
    assert cancelled.is_set() and not await source_facts(flow)
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.attempts == 1 and receipt.status == 'processing'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['provider', 'application'])
async def test_exception_after_lease_expiry_does_not_abort_other_sources(monkeypatch, failure_point):
    flow = await setup_source('chat')
    first = event_for(flow)
    second_snapshot = flow.snapshot.model_copy(update={'source_id': 'message-2'})
    second = event_for(flow, second_snapshot).model_copy(update={'event_id': 'chat-second'})
    await flow.inbox.authorize_cutover(
        flow.authorization.model_copy(
            update={
                'source_type': 'chat',
                'source_id': second.source_id,
                'legacy_source_intents_retired': True,
            }
        ),
        scope=flow.scope,
    )
    await flow.inbox.receive(first, scope=flow.scope)
    await flow.inbox.receive(second, scope=flow.scope)
    target, method = (flow.gateway, 'complete') if failure_point == 'provider' else (flow.applier, 'apply')
    original = getattr(target, method)
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            flow.now[0] += timedelta(seconds=121)
            raise RuntimeError('provider/application failed after ownership expired')
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, method, fail_once)
    assert await flow.worker.recover_pending() == 1
    receipts = [await flow.inbox.get_receipt(event.event_id, scope=flow.scope) for event in (first, second)]
    assert sorted(receipt.status for receipt in receipts) == ['applied', 'processing']
    assert all(receipt.error_code is None for receipt in receipts)
    assert await flow.worker.recover_pending() == 1
    assert len(await source_facts(flow)) == 2


@pytest.mark.asyncio
async def test_fact_registry_version_is_frozen_before_extraction(monkeypatch):
    flow = await setup_source('chat')
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    monkeypatch.setattr(flow.inbox, 'save_extracted_candidates', AsyncMock(side_effect=RuntimeError('crash')))
    assert await flow.worker.recover_pending() == 0
    saved = await flow.inbox.store.get(publication_id(event))
    assert saved['processingSelection']['factRegistryVersion'] == FactTypeRegistry.VERSION
    monkeypatch.setattr(FactTypeRegistry, 'VERSION', 'future-registry')
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.processing_version_unavailable'
    assert len(flow.gateway.complete_requests) == 1
