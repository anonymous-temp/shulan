"""Public Profile reads, durable source intake and source-wide application, offline."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import ProfileSourceSnapshotRefV1
from health_profile.contracts import profile_content_digest
from health_profile.models import AssertionLevel
from health_profile.models import FactSource
from health_profile.models import FactStatus
from health_profile.models import VerificationStatus
from health_profile.report_source import HealthReportSourceRecord
from health_profile.repository import MongoHealthProfileRepository
from health_profile.snapshots import ProfileSnapshotReader
from health_profile.source_material import legacy_report_material
from health_profile.source_material import legacy_report_snapshot_id
from health_profile.source_material import retraction_snapshot
from health_profile.source_material import retraction_snapshot_id
from health_profile.source_publications import MongoProfilePublicationStore
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import ProfileSourceLeaseError
from tests.unit._support.document_profile_composition.profile.public_contracts import setup_flow as setup_flow

from .test_profile_read_and_subjects import FakeCollection


def publication(flow, *, revision=1, legacy_revision=1, value='12', kind=None, event_id=None):
    kind = kind or ('publish' if revision == 1 else 'correct')
    raw = {
        'reportType': 'lab',
        'reportTime': '2026-11-01',
        'observations': [{'itemName': 'ALT', 'resultValue': value, 'unit': 'U/L', 'flag': 'high'}],
    }
    record = HealthReportSourceRecord(
        report_id='report-1',
        user_id=flow.scope.user_id,
        subject_id=flow.scope.subject_id,
        source_revision=legacy_revision,
        source_fingerprint='source-fingerprint',
        result=raw,
        ingestion_status='queued',
        ingestion_job_id='legacy-job',
        created_at=flow.now[0],
        updated_at=flow.now[0],
    )
    flow.source_records[('report-1', legacy_revision)] = record
    material = legacy_report_material(record, source_revision=1)
    return ProfileSourceEventV1(
        event_id=event_id or f'event-{revision}',
        scope=flow.scope,
        source_type='health_report',
        source_id='report-1',
        source_revision=1,
        legacy_source_revision=legacy_revision,
        publication_revision=revision,
        prior_revision=revision - 1 if revision > 1 else None,
        event_kind=kind,
        occurred_at=flow.now[0],
        content_digest=profile_content_digest(material.snapshot),
        snapshot_ref=ProfileSourceSnapshotRefV1(
            provider='health_report',
            contract_version='health-report-observations.v1',
            snapshot_id=legacy_report_snapshot_id(flow.scope, 'report-1', legacy_revision),
        ),
    )


def retract(flow, *, revision=2):
    event = publication(flow, revision=revision, legacy_revision=99, kind='retract')
    event = event.model_copy(
        update={
            'snapshot_ref': ProfileSourceSnapshotRefV1(
                provider='profile',
                contract_version='profile-source-retraction.v1',
                snapshot_id=retraction_snapshot_id(flow.scope, 'health_report', 'report-1', revision),
            )
        }
    )
    return event.model_copy(update={'content_digest': profile_content_digest(retraction_snapshot(event))})


async def visible(flow):
    return await flow.repository.list_facts(
        'u', flow.scope.subject_id, statuses=(FactStatus.ACTIVE, FactStatus.PENDING)
    )


@pytest.mark.asyncio
async def test_snapshot_and_scope_resolution_never_bootstrap_or_enqueue():
    collections = {name: FakeCollection() for name in ('subjects', 'facts', 'events', 'views', 'jobs')}
    reader = ProfileSnapshotReader(MongoHealthProfileRepository(**collections))
    assert await reader.resolve_scope('u') is None
    assert (await reader.read('u')).status == 'missing'
    assert await reader.list_subjects('u') == ()
    assert all(not value.documents and not value.indexes for value in collections.values())


@pytest.mark.asyncio
async def test_source_wide_correction_waits_for_prior_and_ignores_hashed_legacy_revision_order():
    flow = await setup_flow()
    second = publication(flow, revision=2, legacy_revision=2, value='23')
    first = publication(flow, legacy_revision=9999999, value='12')
    waiting = await flow.inbox.receive(second, scope=flow.scope)
    assert waiting.status == 'waiting' and waiting.watermark.applied_publication_revision == 0
    assert await flow.worker.recover_pending() == 0
    await flow.inbox.receive(first, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert await flow.worker.recover_pending() == 1
    facts = await visible(flow)
    assert len(facts) == 1 and facts[0].value['number'] == 23
    assert len(flow.collections['facts'].documents) == 2
    receipt = await flow.inbox.get_receipt(second.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.watermark.applied_publication_revision == 2
    snapshot = await flow.reader.read('u', required_publications=(('health_report', 'report-1', 2),))
    assert snapshot.status == 'ready' and snapshot.coverage['requiredPublicationsApplied'] is True
    assert snapshot.facts[0].sources[0].source_revision == 1
    snapshot.facts[0].value['number'] = 100
    assert (await visible(flow))[0].value['number'] == 23


@pytest.mark.asyncio
async def test_duplicate_event_and_publication_identities_are_checked_independently():
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    duplicate = event.model_copy(update={'event_id': 'second-transport-id'})
    await flow.inbox.receive(duplicate, scope=flow.scope)
    await flow.worker.recover_pending()
    assert len(await visible(flow)) == 1
    assert (await flow.inbox.get_receipt(duplicate.event_id, scope=flow.scope)).status == 'applied'
    with pytest.raises(ProfileSourceConflictError):
        await flow.inbox.receive(event.model_copy(update={'content_digest': 'a' * 64}), scope=flow.scope)
    with pytest.raises(ProfileSourceConflictError):
        await flow.inbox.receive(publication(flow, revision=2, event_id=event.event_id), scope=flow.scope)
    assert len([row for row in flow.collections['publications'].documents if row['recordKind'] == 'publication']) == 1


@pytest.mark.asyncio
async def test_retraction_barrier_is_received_before_applied_and_old_publication_cannot_revive():
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    withdrawn = retract(flow)
    receipt = await flow.inbox.receive(withdrawn, scope=flow.scope)
    assert receipt.watermark.accepted_retraction_revision == 2
    assert receipt.watermark.applied_retraction_revision == 0
    await flow.worker.recover_pending()
    assert await visible(flow) == ()
    await flow.worker.recover_pending()
    assert await visible(flow) == ()
    assert (
        await flow.inbox.get_receipt(withdrawn.event_id, scope=flow.scope)
    ).watermark.applied_retraction_revision == 2
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    assert await visible(flow) == ()


@pytest.mark.asyncio
async def test_interrupted_fact_event_write_replays_plan_without_duplicate_facts(monkeypatch):
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    append = flow.repository.append_event
    failures = [True]

    async def fail_once(change):
        if failures[0]:
            failures[0] = False
            raise RuntimeError('simulated failure after fact write')
        return await append(change)

    monkeypatch.setattr(flow.repository, 'append_event', fail_once)
    assert await flow.worker.recover_pending() == 0
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'retry_wait'
    assert len(await visible(flow)) == 1
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 1
    assert len(await visible(flow)) == 1
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_retraction_removes_only_its_source_support():
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    fact = (await visible(flow))[0]
    other = FactSource(source_type='record', source_id='record-2', source_revision=1)
    await flow.repository.replace_fact(
        fact.model_copy(update={'sources': (*fact.sources, other), 'version': fact.version + 1}),
        expected_version=fact.version,
        allowed_statuses=(fact.status,),
    )
    await flow.inbox.receive(retract(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    remaining = await visible(flow)
    assert len(remaining) == 1 and remaining[0].sources == (other,)


@pytest.mark.asyncio
async def test_source_correction_cannot_overwrite_human_confirmation():
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    fact = (await visible(flow))[0]
    confirmed = fact.model_copy(
        update={
            'assertion_level': AssertionLevel.USER_CONFIRMED,
            'verification_status': VerificationStatus.CONFIRMED,
            'last_request_id': 'human-confirmation',
            'version': fact.version + 1,
        }
    )
    await flow.repository.replace_fact(confirmed, expected_version=fact.version, allowed_statuses=(fact.status,))
    corrected = publication(flow, revision=2, legacy_revision=777, value='99')
    await flow.inbox.receive(corrected, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(corrected.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.manual_governance_requires_review'
    assert (await visible(flow))[0].value == fact.value


@pytest.mark.asyncio
async def test_event_scope_and_legacy_pending_jobs_block_cutover():
    flow = await setup_flow()
    event = publication(flow)
    with pytest.raises(PermissionError):
        await flow.inbox.receive(event, scope=ProfileScope(user_id='other', subject_id=flow.scope.subject_id))
    flow.collections['jobs'].documents.append(
        {
            '_id': 'legacy',
            'userId': 'u',
            'subjectId': flow.scope.subject_id,
            'sourceType': 'health_report',
            'sourceId': 'report-1',
            'status': 'running',
        }
    )
    with pytest.raises(ProfileSourceConflictError, match='quiesce'):
        await flow.inbox.receive(event, scope=flow.scope)
    assert not any(row['recordKind'] == 'publication' for row in flow.collections['publications'].documents)


@pytest.mark.asyncio
async def test_expired_worker_cannot_commit_applied_watermark():
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    cursor = next(row for row in flow.collections['publications'].documents if row['recordKind'] == 'cursor')
    lease = await flow.inbox.claim(deepcopy(cursor), lease_seconds=1)
    flow.now[0] += timedelta(seconds=2)
    replacement = await flow.inbox.claim(deepcopy(cursor), lease_seconds=120)
    assert replacement['leaseToken'] != lease['leaseToken']
    with pytest.raises(ProfileSourceLeaseError):
        await flow.inbox.check_lease(lease)
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).watermark.applied_publication_revision == 0


@pytest.mark.asyncio
async def test_intake_recovers_after_alias_persisted_but_publication_insert_failed(monkeypatch):
    flow = await setup_flow()
    event = publication(flow)
    original = flow.inbox.store.insert
    fail = [True]

    async def interrupted(document):
        if document['recordKind'] == 'publication' and fail[0]:
            fail[0] = False
            raise RuntimeError('simulated process death')
        return await original(document)

    monkeypatch.setattr(flow.inbox.store, 'insert', interrupted)
    with pytest.raises(RuntimeError):
        await flow.inbox.receive(event, scope=flow.scope)
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'received' and receipt.watermark.applied_publication_revision == 0
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'
    assert len(await visible(flow)) == 1


@pytest.mark.asyncio
async def test_applied_cursor_repairs_receipt_after_final_row_update_failure(monkeypatch):
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    original = flow.inbox.store.replace
    fail = [True]

    async def interrupted(document, revision):
        if document.get('recordKind') == 'publication' and document.get('status') == 'applied' and fail[0]:
            fail[0] = False
            raise RuntimeError('simulated receipt update failure')
        return await original(document, revision)

    monkeypatch.setattr(flow.inbox.store, 'replace', interrupted)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.error_code is None
    assert receipt.watermark.applied_publication_revision == 1
    assert len(await visible(flow)) == 1


@pytest.mark.asyncio
async def test_deleted_source_fact_cannot_reappear_under_a_new_publication():
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    fact = (await visible(flow))[0]
    await flow.repository.mark_fact_deleted(
        user_id='u',
        subject_id=flow.scope.subject_id,
        fact_id=fact.fact_id,
        expected_version=fact.version,
        request_id='human-delete',
        request_fingerprint='delete',
        deleted_at=flow.now[0],
    )
    event = publication(flow, revision=2, legacy_revision=300, value='15')
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.manual_governance_requires_review'
    assert await visible(flow) == ()


@pytest.mark.asyncio
async def test_missing_digest_and_unknown_resolver_never_advance_applied_watermark():
    flow = await setup_flow()
    event = publication(flow).model_copy(update={'content_digest': '0' * 64})
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.content_digest_mismatch'
    assert receipt.watermark.applied_publication_revision == 0
    assert await visible(flow) == ()


@pytest.mark.asyncio
async def test_snapshot_distinguishes_truncation_pending_watermark_and_read_failure(monkeypatch):
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    waiting = await flow.reader.read('u', required_publications=(('health_report', 'report-1', 2),))
    assert waiting.stale and waiting.coverage['requiredPublicationsApplied'] is False
    await flow.inbox.receive(retract(flow), scope=flow.scope)
    retracting = await flow.reader.read('u')
    assert retracting.stale and retracting.coverage['publicationPending'] is True

    async def failed(*args, **kwargs):
        raise RuntimeError('private details must not escape')

    monkeypatch.setattr(flow.repository, 'list_fact_page', failed)
    snapshot = await flow.reader.read('u')
    assert snapshot.status == 'unavailable' and snapshot.error_code == 'profile_read_unavailable'
    assert 'private details' not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_exact_owned_legacy_revision_read_has_no_ingestion_side_effects():
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.report_source import HealthReportSourceConflictError
    from health_profile.report_source import HealthReportSourceService
    from health_profile.report_source import MongoHealthReportStore

    from .test_health_report_source import InMemoryJobRepository

    flow = await setup_flow()
    publication(flow, legacy_revision=991)
    record = flow.source_records[('report-1', 991)]
    reports, revisions = FakeCollection(), FakeCollection()
    store = MongoHealthReportStore(reports, revisions)
    await store.create_revision(record, 'legacy-source-key')
    queue_repository = InMemoryJobRepository()
    service = HealthReportSourceService(store, HealthProfileJobQueue(queue_repository))
    before = deepcopy((reports.documents, revisions.documents, reports.indexes, revisions.indexes))
    read = await service.get_owned_revision('report-1', 991, user_id='u', subject_id=flow.scope.subject_id)
    assert read == record
    assert before == (reports.documents, revisions.documents, reports.indexes, revisions.indexes)
    assert queue_repository.jobs == {}
    with pytest.raises(HealthReportSourceConflictError):
        await service.get_owned_revision('report-1', 991, user_id='other', subject_id=flow.scope.subject_id)
    assert await service.get_owned_revision('missing', 991, user_id='u', subject_id=flow.scope.subject_id) is None


@pytest.mark.asyncio
async def test_public_factory_uses_dedicated_collection_and_existing_governance(monkeypatch):
    from health_profile import application

    flow = await setup_flow()
    mapping = {
        'collection_health_subjects': 'subjects',
        'collection_health_profile_facts': 'facts',
        'collection_health_profile_events': 'events',
        'collection_health_profile_views': 'views',
        'collection_health_profile_jobs': 'jobs',
        'collection_health_profile_source_publications': 'publications',
    }
    for name, collection in mapping.items():
        monkeypatch.setattr(application, name, flow.collections[collection])
    monkeypatch.setattr(application, 'collection_health_reports', FakeCollection())
    monkeypatch.setattr(application, 'collection_health_report_revisions', FakeCollection())
    services = application.create_application_profile_public_contracts()
    assert await services.reader.resolve_scope('u') == flow.scope
    assert services.worker.applier.repository is services.inbox.repository
    assert services.inbox.store.collection is flow.collections['publications']
    assert await services.worker.recover_pending() == 0


@pytest.mark.asyncio
async def test_first_receive_requires_explicit_source_owner_cutover_authorization():
    flow = await setup_flow(authorize=False)
    with pytest.raises(ProfileSourceConflictError, match='explicit quiesced'):
        await flow.inbox.receive(publication(flow), scope=flow.scope)
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_cutover_marker_fences_legacy_admission_even_when_pending_check_fails():
    from health_profile.ingestion import HealthProfileJobConflictError
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.models import ProfileIngestionSourceType

    flow = await setup_flow(authorize=False)
    queue = HealthProfileJobQueue(flow.repository, clock=lambda: flow.now[0])
    await queue.enqueue(
        source_type=ProfileIngestionSourceType.HEALTH_REPORT,
        source_id='report-1',
        source_revision=1,
        user_id='u',
        subject_id=flow.scope.subject_id,
    )
    with pytest.raises(ProfileSourceConflictError, match='quiesce'):
        await flow.inbox.authorize_cutover(flow.authorization, scope=flow.scope)
    assert await flow.repository.is_source_publication_managed(
        'u',
        flow.scope.subject_id,
        'health_report',
        'report-1',
    )
    with pytest.raises(HealthProfileJobConflictError, match='fenced'):
        await queue.enqueue(
            source_type=ProfileIngestionSourceType.HEALTH_REPORT,
            source_id='report-1',
            source_revision=2,
            user_id='u',
            subject_id=flow.scope.subject_id,
        )
    with pytest.raises(ProfileSourceConflictError, match='explicit quiesced'):
        await flow.inbox.receive(publication(flow), scope=flow.scope)
    assert len(flow.collections['jobs'].documents) == 1


@pytest.mark.asyncio
async def test_missing_prior_revision_has_bounded_wait_and_explicit_redrive():
    flow = await setup_flow()
    second = publication(flow, revision=2, legacy_revision=222)
    await flow.inbox.receive(second, scope=flow.scope)
    flow.now[0] += timedelta(days=2)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(second.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.prior_revision_timeout'
    assert receipt.watermark.applied_publication_revision == 0
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    assert await flow.worker.recover_pending() == 0
    await flow.inbox.redrive(second.event_id, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_cutover_authorization_can_be_queried_without_writing_state():
    flow = await setup_flow()
    before = deepcopy(flow.collections['publications'].documents)
    state = await flow.inbox.get_cutover(flow.scope, 'health_report', 'report-1')
    assert state.status == 'ready' and state.authorization == flow.authorization
    assert before == flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_human_change_after_persisted_plan_keeps_priority_and_prevents_new_source_values(monkeypatch):
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    old = (await visible(flow))[0]
    event = publication(flow, revision=2, legacy_revision=888, value='44')
    await flow.inbox.receive(event, scope=flow.scope)
    original = flow.inbox.save_plan

    async def confirm_after_plan(identity, plan):
        saved = await original(identity, plan)
        await flow.repository.replace_fact(
            old.model_copy(
                update={
                    'assertion_level': AssertionLevel.USER_CONFIRMED,
                    'verification_status': VerificationStatus.CONFIRMED,
                    'last_request_id': 'human-after-plan',
                    'version': old.version + 1,
                }
            ),
            expected_version=old.version,
            allowed_statuses=(old.status,),
        )
        return saved

    monkeypatch.setattr(flow.inbox, 'save_plan', confirm_after_plan)
    await flow.worker.recover_pending()
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'failed'
    remaining = await visible(flow)
    assert len(remaining) == 1 and remaining[0].value == old.value


@pytest.mark.asyncio
async def test_expired_worker_late_create_cannot_revive_an_applied_retraction(monkeypatch):
    flow = await setup_flow()
    first = publication(flow)
    await flow.inbox.receive(first, scope=flow.scope)
    create_fact = flow.repository.create_fact
    paused = asyncio.Event()
    resume = asyncio.Event()
    intercepted = False

    async def delay_first_create(fact):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            paused.set()
            await resume.wait()
        return await create_fact(fact)

    monkeypatch.setattr(flow.repository, 'create_fact', delay_first_create)
    old_worker = asyncio.create_task(flow.worker.recover_pending())
    try:
        await asyncio.wait_for(paused.wait(), timeout=2)
        flow.now[0] += timedelta(seconds=121)
        withdrawn = retract(flow)
        await flow.inbox.receive(withdrawn, scope=flow.scope)
        assert await flow.worker.recover_pending() == 1
        assert await flow.worker.recover_pending() == 1
        assert await visible(flow) == ()
        resume.set()
        assert await asyncio.wait_for(old_worker, timeout=2) == 0
        assert await visible(flow) == ()
        watermark = await flow.inbox.get_watermark(flow.scope, 'health_report', 'report-1')
        assert watermark.applied_publication_revision == watermark.applied_retraction_revision == 2
        assert (await flow.inbox.get_receipt(first.event_id, scope=flow.scope)).status == 'applied'
    finally:
        resume.set()
        if not old_worker.done():
            old_worker.cancel()
            await asyncio.gather(old_worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupted_processing_consumes_the_persisted_attempt_budget(monkeypatch):
    flow = await setup_flow()
    event = publication(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    apply = flow.applier.apply
    dispatches = 0

    async def lose_lease(cursor, publication):
        nonlocal dispatches
        dispatches += 1
        flow.now[0] += timedelta(seconds=121)
        raise ProfileSourceLeaseError('simulated process loss after dispatch')

    monkeypatch.setattr(flow.applier, 'apply', lose_lease)
    for _ in range(7):
        await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert dispatches == receipt.attempts == 5
    assert receipt.status == 'failed' and receipt.error_code == 'source.attempts_exhausted'
    assert receipt.watermark.applied_publication_revision == 0
    monkeypatch.setattr(flow.applier, 'apply', apply)
    await flow.inbox.redrive(event.event_id, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1


@pytest.mark.asyncio
async def test_only_a_live_owner_renews_the_lease_as_application_makes_progress():
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    cursor = next(row for row in flow.collections['publications'].documents if row['recordKind'] == 'cursor')
    lease = await flow.inbox.claim(cursor)
    flow.now[0] += timedelta(seconds=61)
    renewed = await flow.inbox.check_lease(lease)
    assert renewed['leaseExpiresAt'] == flow.now[0] + timedelta(seconds=120)
    flow.now[0] += timedelta(seconds=121)
    replacement = await flow.inbox.claim(renewed)
    with pytest.raises(ProfileSourceLeaseError):
        await flow.inbox.check_lease(lease)
    assert (await flow.inbox.check_lease(replacement))['leaseToken'] == replacement['leaseToken']


@pytest.mark.asyncio
async def test_managed_pending_reports_retire_so_later_legacy_reports_can_recover():
    from pymongo import ReturnDocument

    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.report_source import HealthReportSourceService
    from health_profile.report_source import MongoHealthReportStore
    from health_profile.report_source import _record_to_document

    class Reports(FakeCollection):
        def find(self, filters, projection=None):
            return super().find(filters)

        async def update_one(self, filters, update):
            row = await self.find_one_and_update(filters, update, return_document=ReturnDocument.AFTER)
            return SimpleNamespace(acknowledged=True, matched_count=int(row is not None))

    flow = await setup_flow(authorize=False)
    reports, revisions = Reports(), Reports()
    store = MongoHealthReportStore(reports, revisions)
    for index in range(3):
        report_id = f'report-{index}'
        record = HealthReportSourceRecord(
            report_id=report_id,
            user_id=flow.scope.user_id,
            subject_id=flow.scope.subject_id,
            source_revision=1,
            source_fingerprint=report_id,
            result={},
            ingestion_status='pending',
            ingestion_job_id=None,
            created_at=flow.now[0],
            updated_at=flow.now[0],
        )
        await store.create_report(record)
        if index < 2:
            await flow.inbox.authorize_cutover(
                flow.authorization.model_copy(update={'source_id': report_id}),
                scope=flow.scope,
            )
    historical = record.__class__(
        report_id='report-0',
        user_id=flow.scope.user_id,
        subject_id=flow.scope.subject_id,
        source_revision=2,
        source_fingerprint='history',
        result={},
        ingestion_status='pending',
        ingestion_job_id=None,
        created_at=flow.now[0],
        updated_at=flow.now[0],
    )
    await revisions.insert_one(_record_to_document(historical))
    service = HealthReportSourceService(store, HealthProfileJobQueue(flow.repository), clock=lambda: flow.now[0])
    assert await service.recover_pending(limit=2) == 0
    for report_id in ('report-0', 'report-1'):
        managed = await store.get_report(report_id)
        assert managed.ingestion_status == 'source_managed' and managed.ingestion_job_id is None
    assert (await store.get_revision('report-0', 2)).ingestion_status == 'source_managed'
    assert await service.recover_pending(limit=2) == 1
    assert (await store.get_report('report-2')).ingestion_status == 'queued'
    assert len(flow.collections['jobs'].documents) == 1


@pytest.mark.asyncio
async def test_public_fact_serialization_preserves_creation_and_update_times():
    flow = await setup_flow()
    await flow.inbox.receive(publication(flow), scope=flow.scope)
    await flow.worker.recover_pending()
    fact = (await visible(flow))[0]
    updated_at = fact.updated_at + timedelta(hours=2)
    await flow.repository.replace_fact(
        fact.model_copy(update={'updated_at': updated_at, 'version': fact.version + 1}),
        expected_version=fact.version,
        allowed_statuses=(fact.status,),
    )
    snapshot = await flow.reader.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    public = snapshot.facts[0]
    assert public.created_at == fact.created_at and public.updated_at == updated_at
    payload = public.model_dump(mode='json', by_alias=True)
    assert payload['createdAt'] == fact.created_at.isoformat().replace('+00:00', 'Z')
    assert payload['updatedAt'] == updated_at.isoformat().replace('+00:00', 'Z')
    assert 'creationRequestId' not in payload and 'lastRequestId' not in payload


@pytest.mark.asyncio
async def test_bson_roundtrip_preserves_event_identity_and_normalizes_ledger_utc(monkeypatch):
    from bson import BSON

    class BsonCollection(FakeCollection):
        async def insert_one(self, document):
            return await super().insert_one(BSON.encode(document).decode())

        async def find_one_and_replace(self, filters, replacement, *, return_document):
            return await super().find_one_and_replace(
                filters,
                BSON.encode(replacement).decode(),
                return_document=return_document,
            )

    flow = await setup_flow(authorize=False)
    collection = BsonCollection()
    flow.inbox.store = MongoProfilePublicationStore(collection)
    monkeypatch.setattr(flow.repository, '_publications', collection)
    flow.now[0] = flow.now[0].replace(microsecond=123456)
    authorization = flow.authorization.model_copy(update={'authorized_at': flow.now[0]})
    await flow.inbox.authorize_cutover(authorization, scope=flow.scope)
    assert (await flow.inbox.authorize_cutover(authorization, scope=flow.scope)).authorization == authorization
    event = publication(flow)
    original = flow.inbox.store.insert
    failures = [True]

    async def interrupt_intake(document):
        if document['recordKind'] == 'publication' and failures[0]:
            failures[0] = False
            raise RuntimeError('stop after immutable event persisted')
        return await original(document)

    monkeypatch.setattr(flow.inbox.store, 'insert', interrupt_intake)
    with pytest.raises(RuntimeError):
        await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    receipt = await flow.inbox.receive(event, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.received_at.tzinfo is not None
    alias = await flow.inbox.store.get(flow.inbox.alias_id(flow.scope, event.event_id))
    assert ProfileSourceEventV1.model_validate(alias['event']) == event
    assert alias['event']['occurredAt'].endswith('.123456Z')
    assert (await flow.reader.read('u')).status == 'ready'
