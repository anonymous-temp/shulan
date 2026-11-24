"""Record v2 raw inputs, partial governance and explicit retraction settlement."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from agents import GraphRegistry
from health_profile.models import AssertionLevel
from health_profile.models import FactStatus
from health_profile.record_ingestion import _sanitized_answers
from health_profile.source_contracts import ProfileRecordSourceSnapshotV2
from health_profile.source_contracts import ProfileUserInputFieldV1
from health_profile.source_extraction import ProfileUserSourceResolver
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import ProfileSourceInbox
from health_profile.source_publications import publication_id

from .test_multi_source_publications import SELF_TEXT
from .test_multi_source_publications import event_for
from .test_multi_source_publications import retract_event
from .test_multi_source_publications import setup_source
from .test_multi_source_publications import source_facts


async def setup_v2(*, basic=True, outputs=None):
    flow = await setup_source('record', outputs=outputs)
    flow.snapshot = ProfileRecordSourceSnapshotV2(
        scope=flow.scope,
        source_id=flow.snapshot.source_id,
        source_revision=1,
        session_id=flow.snapshot.session_id,
        completed_at='2026-11-09T00:00:00Z',
        round_number=1,
        qa_history=({'question': {'questionId': 'q0', 'question': 'question'}, 'answer': SELF_TEXT},),
        user_inputs=(ProfileUserInputFieldV1(field='age', value=42, provenance='user_input'),) if basic else (),
    )
    previous = flow.applier.resolvers[('record', 'profile.record-source.v1')]
    flow.applier.resolvers[('record', flow.snapshot.contract_version)] = ProfileUserSourceResolver(previous.reader)
    return flow


async def fail_to_partial(flow, event, monkeypatch):
    original = flow.gateway.complete
    monkeypatch.setattr(flow.gateway, 'complete', AsyncMock(side_effect=RuntimeError('offline model failure')))
    await flow.inbox.receive(event, scope=flow.scope)
    for _ in range(flow.worker.max_attempts):
        await flow.worker.recover_pending()
        flow.now[0] += timedelta(minutes=6)
    return original


@pytest.mark.asyncio
async def test_v2_keeps_raw_input_order_and_reuses_legacy_source_sanitization():
    flow = await setup_v2(outputs=[])
    history = [None, *deepcopy(flow.snapshot.qa_history)]
    health = {
        'hospitalData': [{'z': 2, 'a': 1, 'observedAt': '2026-10-01'}],
        'weRunData': [{'steps': 300}],
        'smartDeviceData': {'z/~': {'v': 3}, 'a': [1, 2]},
    }
    flow.snapshot = flow.snapshot.model_copy(
        update={
            'qa_history': tuple(history),
            'hospital_data': tuple(health['hospitalData']),
            'we_run_data': tuple(health['weRunData']),
            'smart_device_data': health['smartDeviceData'],
        }
    )
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    content = json.loads(flow.gateway.complete_requests[0].content)
    assert content['answers'] == _sanitized_answers({'qaHistory': history, 'healthData': health})
    row = await flow.inbox.store.get(publication_id(event))
    assert row['basicCandidatesCheckpoint'] and row['extractedCandidates']
    assert row['processingSelection']['implementationRevision'] == 'profile-record-source-v2'


@pytest.mark.asyncio
async def test_terminal_model_failure_applies_only_explicit_basics_without_full_watermark(monkeypatch):
    flow = await setup_v2()
    event = event_for(flow)
    await fail_to_partial(flow, event, monkeypatch)
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == receipt.application_state == 'partial'
    assert receipt.partial_application['status'] == 'applied'
    assert receipt.watermark.applied_publication_revision == receipt.watermark.settled_publication_revision == 0
    facts = await source_facts(flow)
    assert len(facts) == 1 and facts[0].assertion_level is AssertionLevel.USER_CONFIRMED
    read = await flow.reader.read('u', required_publications=(('record', flow.snapshot.source_id, 1),))
    assert not read.coverage['requiredPublicationsApplied']
    assert read.coverage['requiredPublicationStates'][0]['applicationState'] == 'partial'
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    assert len(await source_facts(flow)) == 1


@pytest.mark.asyncio
async def test_partial_correction_keeps_previous_nonbasic_contributions_and_blocks_next_correct(monkeypatch):
    flow = await setup_v2(basic=False)
    first = event_for(flow)
    await flow.inbox.receive(first, scope=flow.scope)
    await flow.worker.recover_pending()
    original = (await source_facts(flow))[0]
    flow.snapshot = flow.snapshot.model_copy(
        update={'user_inputs': (ProfileUserInputFieldV1(field='age', value=42, provenance='user_input'),)}
    )
    second = event_for(flow, revision=2)
    await fail_to_partial(flow, second, monkeypatch)
    facts = await source_facts(flow)
    assert original.fact_id in {fact.fact_id for fact in facts} and len(facts) == 2
    third = event_for(flow, revision=3)
    await flow.inbox.receive(third, scope=flow.scope)
    await flow.worker.recover_pending()
    assert (await flow.inbox.get_receipt(third.event_id, scope=flow.scope)).status == 'waiting'
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).watermark.applied_publication_revision == 1


@pytest.mark.asyncio
async def test_full_redrive_reuses_partial_fact_instead_of_withdrawing_it(monkeypatch):
    flow = await setup_v2()
    event = event_for(flow)
    original = await fail_to_partial(flow, event, monkeypatch)
    basic = (await source_facts(flow))[0]
    monkeypatch.setattr(flow.gateway, 'complete', original)
    await flow.inbox.redrive(event.event_id, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.application_state == receipt.status == 'applied'
    facts = await source_facts(flow)
    assert len(facts) == 2 and basic.fact_id in {fact.fact_id for fact in facts}
    assert (await flow.repository.get_fact('u', basic.subject_id, basic.fact_id)).status is not FactStatus.RETRACTED
    saved = await flow.inbox.store.get(publication_id(event))
    assert basic.fact_id not in {item['fact_id'] for item in saved['plan']['withdraws']}


@pytest.mark.asyncio
async def test_retraction_settles_partial_and_waiting_predecessors_without_faking_applied(monkeypatch):
    flow = await setup_v2()
    first = event_for(flow)
    await fail_to_partial(flow, first, monkeypatch)
    second = event_for(flow, revision=2)
    third = retract_event(flow, revision=3)
    await flow.inbox.receive(third, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0  # revision two is absent
    await flow.inbox.receive(second, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    for event in (first, second):
        receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
        assert receipt.status == 'superseded' and receipt.application_state == 'superseded_by_retraction'
        assert receipt.superseded_by_publication_revision == 3
        with pytest.raises(ProfileSourceConflictError):
            await flow.inbox.redrive(event.event_id, scope=flow.scope)
    receipt = await flow.inbox.get_receipt(third.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.watermark.settled_publication_revision == 3
    read = await flow.reader.read('u', required_publications=(('record', flow.snapshot.source_id, 1),))
    assert not read.coverage['requiredPublicationsApplied']
    assert read.coverage['requiredPublicationStates'][0]['applicationState'] == 'superseded_by_retraction'
    # USER_CONFIRMED keeps the established human-governance semantics after source withdrawal.
    facts = await source_facts(flow)
    assert len(facts) == 1 and facts[0].sources[0].source_type == 'manual'
    await flow.inbox.receive(first, scope=flow.scope)
    await flow.worker.recover_pending()
    assert (await flow.inbox.get_receipt(first.event_id, scope=flow.scope)).status == 'superseded'


@pytest.mark.asyncio
async def test_only_explicit_v2_authorization_opens_the_real_record_inbox():
    flow = await setup_v2()
    flow.inbox.__class__ = ProfileSourceInbox
    flow.snapshot = flow.snapshot.model_copy(update={'source_id': 'production-v2'})
    authorization = flow.authorization.model_copy(
        update={
            'source_type': 'record',
            'source_id': flow.snapshot.source_id,
            'legacy_source_intents_retired': True,
            'material_contract_version': 'profile.record-source.v2',
        }
    )
    await flow.inbox.authorize_cutover(authorization, scope=flow.scope)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_partial_write_retry_never_repeats_model_or_discards_saved_basics(monkeypatch):
    flow = await setup_v2()
    flow.worker.max_attempts = 2
    create = flow.repository.create_fact
    calls = []

    async def transient(fact):
        calls.append(fact.fact_id)
        if len(calls) == 1:
            raise RuntimeError('offline fact-store interruption')
        return await create(fact)

    monkeypatch.setattr(flow.repository, 'create_fact', transient)
    event = event_for(flow)
    await fail_to_partial(flow, event, monkeypatch)
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'retry_wait'
    model_calls = flow.gateway.complete.await_count
    await flow.worker.recover_pending()
    assert flow.gateway.complete.await_count == model_calls == 2
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.application_state == 'partial' and receipt.partial_application['attempts'] == 2
    assert len(await source_facts(flow)) == 1 and calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', [False, True])
async def test_terminal_retraction_tombstones_all_interrupted_plan_creates_before_late_writer(monkeypatch, partial):
    flow = await setup_v2(basic=partial)
    flow.worker.max_attempts = 1
    if partial:
        monkeypatch.setattr(flow.gateway, 'complete', AsyncMock(side_effect=RuntimeError('offline failure')))
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    create = flow.repository.create_fact
    entered, resume = asyncio.Event(), asyncio.Event()
    intercepted = False

    async def paused_create(fact):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            entered.set()
            await resume.wait()
        return await create(fact)

    monkeypatch.setattr(flow.repository, 'create_fact', paused_create)
    old = asyncio.create_task(flow.worker.recover_pending())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        flow.now[0] += timedelta(seconds=121)
        retraction = retract_event(flow)
        await flow.inbox.receive(retraction, scope=flow.scope)
        await flow.worker.recover_pending()  # exhaust the interrupted full/partial attempt
        assert await flow.worker.recover_pending() == 1
        assert not await source_facts(flow)
        resume.set()
        assert await asyncio.wait_for(old, 3) == 0
        assert not await source_facts(flow)
        assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'superseded'
    finally:
        resume.set()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_retraction_cursor_crash_keeps_truthful_receipts_and_replays_saved_withdrawals(monkeypatch):
    flow = await setup_v2()
    first = event_for(flow)
    await fail_to_partial(flow, first, monkeypatch)
    second = retract_event(flow)
    await flow.inbox.receive(second, scope=flow.scope)
    replace = flow.inbox.store.replace
    interrupted = False

    async def crash_advance(document, revision):
        nonlocal interrupted
        if document['recordKind'] == 'cursor' and document.get('settledPublicationRevision') == 2 and not interrupted:
            interrupted = True
            raise RuntimeError('offline interruption before cursor commit')
        return await replace(document, revision)

    monkeypatch.setattr(flow.inbox.store, 'replace', crash_advance)
    assert await flow.worker.recover_pending() == 0
    assert (await flow.inbox.get_receipt(first.event_id, scope=flow.scope)).application_state == 'partial'
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).application_state == 'incomplete'
    flow.now[0] += timedelta(minutes=6)
    assert await flow.worker.recover_pending() == 1
    assert (
        await flow.inbox.get_receipt(first.event_id, scope=flow.scope)
    ).application_state == 'superseded_by_retraction'
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).application_state == 'applied'


@pytest.mark.asyncio
async def test_manual_edit_after_partial_prevents_full_redrive_overwrite(monkeypatch):
    flow = await setup_v2()
    event = event_for(flow)
    original = await fail_to_partial(flow, event, monkeypatch)
    fact = (await source_facts(flow))[0]
    changed = fact.model_copy(
        update={'value': {'value': 43, 'unit': 'years'}, 'version': fact.version + 1, 'last_request_id': 'human-edit'}
    )
    await flow.repository.replace_fact(
        changed, expected_version=fact.version, allowed_statuses=(FactStatus.ACTIVE, FactStatus.PENDING)
    )
    monkeypatch.setattr(flow.gateway, 'complete', original)
    await flow.inbox.redrive(event.event_id, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.manual_governance_requires_review'
    assert (await flow.repository.get_fact('u', fact.subject_id, fact.fact_id)).value == changed.value


@pytest.mark.asyncio
async def test_scope_bootstrap_accepts_explicit_name_only_for_new_subject():
    flow = await setup_v2()
    scope = await flow.inbox.prepare_self_scope('new-user', display_name='Explicit name')
    assert (await flow.repository.get_subject('new-user', scope.subject_id)).display_name == 'Explicit name'
    await flow.inbox.prepare_self_scope('new-user', display_name='Later source name')
    assert (await flow.repository.get_subject('new-user', scope.subject_id)).display_name == 'Explicit name'


@pytest.mark.asyncio
async def test_partial_plan_missing_after_restart_requires_frozen_processing_version(monkeypatch):
    flow = await setup_v2()
    flow.worker.max_attempts = 1
    begin = flow.inbox.begin_partial

    async def crash_after_intent(*args):
        await begin(*args)
        raise RuntimeError('offline crash after partial intent')

    monkeypatch.setattr(flow.inbox, 'begin_partial', crash_after_intent)
    monkeypatch.setattr(flow.gateway, 'complete', AsyncMock(side_effect=RuntimeError('model failure')))
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    flow.applier.extractor.policy_version = 'unavailable-next-policy'
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.processing_version_unavailable'
    assert not await source_facts(flow)


@pytest.mark.asyncio
async def test_partial_side_effects_before_failure_are_reported_incomplete(monkeypatch):
    flow = await setup_v2()
    flow.worker.max_attempts = 1
    flow.snapshot = flow.snapshot.model_copy(
        update={
            'user_inputs': (
                *flow.snapshot.user_inputs,
                ProfileUserInputFieldV1(field='height', value=175, provenance='user_input'),
            )
        }
    )
    create = flow.repository.create_fact
    calls = 0

    async def second_write_fails(fact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('offline second basic write failure')
        return await create(fact)

    monkeypatch.setattr(flow.repository, 'create_fact', second_write_fails)
    event = event_for(flow)
    await fail_to_partial(flow, event, monkeypatch)
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.application_state == 'incomplete'
    assert receipt.partial_application['status'] == 'failed' and len(await source_facts(flow)) == 1
    retraction = retract_event(flow)
    await flow.inbox.receive(retraction, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'superseded'


@pytest.mark.asyncio
async def test_old_authorization_without_new_optional_contract_field_remains_replayable():
    from .test_public_contracts import setup_flow

    flow = await setup_flow()
    for row in flow.collections['publications'].documents:
        if row['recordKind'] == 'cutover':
            row['authorization'].pop('materialContractVersion', None)
    await flow.inbox.authorize_cutover(flow.authorization, scope=flow.scope)
    assert (await flow.inbox.get_cutover(flow.scope, 'health_report', 'report-1')).status == 'ready'


@pytest.mark.asyncio
async def test_missing_exact_child_allows_frozen_basic_partial_without_latest_fallback():
    flow = await setup_v2()
    flow.runtime.registry.publish(GraphRegistry())
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == receipt.application_state == 'partial'
    assert receipt.error_code == 'source.extraction_version_unavailable'
    assert len(await source_facts(flow)) == 1 and not flow.gateway.complete_requests
    row = await flow.inbox.store.get(publication_id(event))
    assert row['basicCandidatesCheckpoint'] and row['extractedCandidates'] is None


@pytest.mark.asyncio
async def test_admission_reserves_one_retraction_slot_and_rejects_excess_before_writes():
    flow = await setup_v2()
    within = event_for(flow, revision=1000)
    await flow.inbox.receive(within, scope=flow.scope)
    before = deepcopy(flow.collections['publications'].documents)
    excess = event_for(flow, revision=1001)
    with pytest.raises(ProfileSourceConflictError, match='unsettled_revision_limit_exceeded'):
        await flow.inbox.receive(excess, scope=flow.scope)
    assert flow.collections['publications'].documents == before
    retraction = retract_event(flow, revision=1001)
    receipt = await flow.inbox.receive(retraction, scope=flow.scope)
    assert receipt.status == 'waiting'
    too_far = retract_event(flow, revision=1002)
    with pytest.raises(ProfileSourceConflictError, match='unsettled_revision_limit_exceeded'):
        await flow.inbox.receive(too_far, scope=flow.scope)


@pytest.mark.asyncio
async def test_historical_over_budget_chain_fails_visibly_and_keeps_duplicate_retry(monkeypatch):
    flow = await setup_v2()
    first = event_for(flow)
    await fail_to_partial(flow, first, monkeypatch)
    for revision in (2, 3):
        await flow.inbox.receive(event_for(flow, revision=revision), scope=flow.scope)
    fourth = retract_event(flow, revision=4)
    await flow.inbox.receive(fourth, scope=flow.scope)
    # Simulate a previously accepted chain exceeding a newly explicit budget.
    monkeypatch.setattr('health_profile.source_publications.MAX_UNSETTLED_SOURCE_REVISIONS', 2)
    monkeypatch.setattr('health_profile.source_application.MAX_UNSETTLED_SOURCE_REVISIONS', 2)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(fourth.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.settlement_revision_limit_exceeded'
    assert receipt.watermark.settled_publication_revision == 0
    assert (await flow.inbox.receive(fourth, scope=flow.scope)).status == 'failed'
    get = flow.inbox.store.get
    calls = []

    async def observe(key):
        calls.append(key)
        return await get(key)

    monkeypatch.setattr(flow.inbox.store, 'get', observe)
    await flow.worker.recover_pending()
    assert publication_id(event_for(flow, revision=2)) not in calls
