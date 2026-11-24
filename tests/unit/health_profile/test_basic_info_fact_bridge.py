"""Existing Profile source writers must publish canonical demographic effects."""

import asyncio
from datetime import UTC
from datetime import datetime

import pytest

from health_profile import HealthProfileService
from health_profile.basic_info import ProfileBasicInfoService
from health_profile.basic_info_store import BasicInfoConflict
from health_profile.basic_info_store import BasicInfoUnavailable
from health_profile.basic_info_store import MongoBasicInfoStore
from health_profile.fact_writer import HealthProfileFactWriter
from health_profile.ingestion import HealthFactPolicy
from health_profile.models import AssertionLevel
from health_profile.models import FactSource
from health_profile.models import FactStatus
from health_profile.models import HealthProfileJob
from health_profile.models import ProfileIngestionSourceType
from health_profile.models import SubjectKind
from health_profile.models import SubjectStatus
from health_profile.processing import ProfileIngestionProcessingError
from health_profile.record_ingestion import RecordBasicInfoMapper
from health_profile.record_source import RecordProfileSourceRecord
from health_profile.snapshots import ProfileSnapshotReader
from health_profile.source_application import ProfilePublicationWorker
from health_profile.source_application import ProfileSourceApplier
from health_profile.source_contracts import ProfileUserInputFieldV1
from tests.unit.health_profile.test_basic_info import NOW
from tests.unit.health_profile.test_basic_info import BsonCollection
from tests.unit.health_profile.test_basic_info import composition as composition
from tests.unit.health_profile.test_basic_info import report
from tests.unit.health_profile.test_basic_info import values
from tests.unit.health_profile.test_multi_source_publications import event_for
from tests.unit.health_profile.test_multi_source_publications import retract_event
from tests.unit.health_profile.test_record_source_settlement import fail_to_partial
from tests.unit.health_profile.test_record_source_settlement import setup_v2

SOURCE_TIME = datetime(2026, 11, 9, tzinfo=UTC)


def record_candidates(subject_id, *, age=42, gender='female', source_id='record-r1'):
    record = RecordProfileSourceRecord(
        user_id='u',
        session_id=source_id,
        round_number=1,
        source_revision=1,
        ingestion_status='queued',
        ingestion_job_id='record-job',
        session={
            'extractedInfo': {'age': age, 'gender': gender},
            'extractedInfoSources': {'age': 'user_input', 'gender': 'user_input'},
            'qaCompletedAt': SOURCE_TIME.isoformat(),
        },
    )
    return RecordBasicInfoMapper().map(record, subject_id=subject_id)


def record_job(subject_id, *, source_id='record-r1', job_id='job-r1'):
    return HealthProfileJob(
        job_id=job_id,
        idempotency_key=job_id,
        idempotency_fingerprint=job_id,
        source_type=ProfileIngestionSourceType.RECORD,
        source_id=source_id,
        source_revision=1,
        user_id='u',
        subject_id=subject_id,
        created_at=SOURCE_TIME,
        updated_at=SOURCE_TIME,
    )


async def assert_public_values(repo, basic, profile, *, age=42, gender='female'):
    current = await basic.read('u')
    assert current.fields['age'].value == age
    assert current.fields['gender'].value == gender
    assert current.fields['age'].confirmed_at == SOURCE_TIME
    assert current.fields['age'].age_basis == 'confirmed_at_time'
    assert current.fields['age'].source_reliability == 'very_high'
    result = await profile.get_profile('u')
    assert result['overview']['age'] == age and result['overview']['gender'] == gender
    listed = await profile.list_facts('u', limit=100)
    ids = {row['factId'] for row in listed['items']}
    expected = {row.fact_id for row in current.fields.values() if row.fact_id}
    assert expected <= ids
    reader = ProfileSnapshotReader(repo, clock=lambda: NOW)
    assert expected <= {row.fact_id for row in (await reader.read('u')).facts}
    assert expected <= {row.fact_id for row in (await reader.read_v2('u')).facts}
    assert expected <= {row.fact_id for row in (await reader.read_page('u')).facts}


@pytest.mark.asyncio
async def test_legacy_record_basics_remain_visible_after_name_edit(composition):
    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await profile.upsert_subject(
        'u',
        'rename-before-record',
        subject_id=subject.subject_id,
        display_name='Synthetic name',
        expected_subject_version=subject.version,
    )
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    policy = HealthFactPolicy()
    job = record_job(subject.subject_id)
    for candidate in record_candidates(subject.subject_id):
        await writer.write(
            job=job,
            subject=subject,
            candidate=candidate,
            decision=policy.evaluate(candidate, subject_kind=SubjectKind.SELF),
        )
    await assert_public_values(repo, basic, profile)


@pytest.mark.asyncio
async def test_formal_record_basics_remain_visible_after_name_edit():
    flow = await setup_v2(outputs=[])
    basic = ProfileBasicInfoService(flow.repository, MongoBasicInfoStore(BsonCollection()), clock=lambda: NOW)
    flow.repository.basic_info = basic
    profile = HealthProfileService(flow.repository, clock=lambda: NOW)
    flow.snapshot = flow.snapshot.model_copy(
        update={
            'user_inputs': (
                ProfileUserInputFieldV1(field='age', value=42, provenance='user_input'),
                ProfileUserInputFieldV1(field='gender', value='female', provenance='user_input'),
            )
        }
    )
    flow.applier = ProfileSourceApplier(flow.inbox, resolvers=flow.applier.resolvers, extractor=flow.applier.extractor)
    flow.worker = ProfilePublicationWorker(flow.inbox, flow.applier)
    subject = await flow.repository.get_subject('u', flow.scope.subject_id)
    await profile.upsert_subject(
        'u',
        'rename-before-formal',
        subject_id=subject.subject_id,
        display_name='Synthetic name',
        expected_subject_version=subject.version,
    )
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    await assert_public_values(flow.repository, basic, profile)


async def write_age(repo, subject, *, age=42, job_id='job-r1', writer=None):
    writer = writer or HealthProfileFactWriter(repo, clock=lambda: NOW)
    candidate = next(
        row for row in record_candidates(subject.subject_id, age=age) if row.fact_type == 'demographic.age'
    )
    job = record_job(subject.subject_id, job_id=job_id)
    decision = HealthFactPolicy().evaluate(candidate, subject_kind=SubjectKind.SELF)
    await writer.write(job=job, subject=subject, candidate=candidate, decision=decision)
    return writer, candidate, job, decision


@pytest.mark.asyncio
async def test_same_grade_legacy_record_preserves_api_current_and_candidate(composition):
    repo, basic, profile, _ = composition
    command = await basic.register('u', 'api-current', values=values(age=45), confirmed_at=NOW)
    await basic.apply('u', command.command_id)
    subject = await repo.get_self_subject('u')
    before = (await basic.read('u')).fields['age'].governance_epoch
    await write_age(repo, subject)
    current = await basic.read('u')
    assert current.fields['age'].value == 45 and current.fields['age'].governance_epoch == before
    candidates = [row for row in current.sources if row.field == 'age' and row.value == 42]
    assert len(candidates) == 1 and candidates[0].active and not candidates[0].current
    assert candidates[0].source_ref['sourceType'] == 'record'
    assert candidates[0].confirmed_at == SOURCE_TIME


@pytest.mark.asyncio
async def test_record_high_replaces_report_low_with_real_provenance(composition):
    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await basic.ingest_report(
        'u', subject.subject_id, source_id='report-low', source_revision=1, fields=[report('age', 40)]
    )
    await write_age(repo, subject)
    current = (await basic.read('u')).fields['age']
    assert current.value == 42 and current.source_reliability == 'very_high'
    fact = await repo.get_fact('u', subject.subject_id, current.fact_id)
    assert fact.sources[0].source_type == 'record'
    assert fact.basic_info_lifecycle_epoch == subject.lifecycle_epoch


@pytest.mark.asyncio
async def test_legacy_lost_canonical_ack_replays_original_intent_without_new_fence(composition, monkeypatch):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    finish = writer.basic_info_bridge._finish
    failed = False

    async def lose_first_ack(fence, status):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError('synthetic acknowledgement loss')
        return await finish(fence, status)

    monkeypatch.setattr(writer.basic_info_bridge, '_finish', lose_first_ack)
    with pytest.raises(OSError):
        await write_age(repo, subject, writer=writer)
    assert len(collections[1].documents) == 1
    monkeypatch.setattr(writer.basic_info_bridge, '_finish', finish)
    await write_age(repo, subject, writer=writer)
    await write_age(repo, subject, writer=writer)  # Existing event also repairs through the original intent.
    assert len(collections[1].documents) == 1
    current = (await basic.read('u')).fields['age']
    assert current.value == 42 and current.confirmed_at == SOURCE_TIME and current.governance_epoch == 0


@pytest.mark.asyncio
async def test_legacy_inflight_source_cannot_override_new_manual_epoch(composition, monkeypatch):
    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    original_create = repo.create_fact
    prepared, release = asyncio.Event(), asyncio.Event()

    async def pause_record(fact):
        if fact.sources[0].source_type == 'record':
            prepared.set()
            await release.wait()
        return await original_create(fact)

    monkeypatch.setattr(repo, 'create_fact', pause_record)
    running = asyncio.create_task(write_age(repo, subject, writer=writer))
    await prepared.wait()
    command = await basic.register('u', 'new-api', values=values(age=45), confirmed_at=NOW)
    await basic.apply('u', command.command_id)
    release.set()
    await running
    current = await basic.read('u')
    assert current.fields['age'].value == 45
    old = [
        row for row in current.sources if row.field == 'age' and (row.source_ref or {}).get('sourceType') == 'record'
    ]
    assert old and old[0].value is None and not old[0].active and not old[0].current


@pytest.mark.asyncio
async def test_legacy_materialized_fact_never_reimports_after_lifecycle_change(composition, monkeypatch):
    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    original_create = repo.create_fact
    written, release = asyncio.Event(), asyncio.Event()

    async def pause_after_create(fact):
        result = await original_create(fact)
        written.set()
        await release.wait()
        return result

    monkeypatch.setattr(repo, 'create_fact', pause_after_create)
    running = asyncio.create_task(write_age(repo, subject, writer=writer))
    await written.wait()
    archived = await repo.update_subject(
        subject.model_copy(update={'status': SubjectStatus.ARCHIVED, 'updated_at': NOW}),
        expected_version=subject.version,
    )
    await repo.update_subject(
        archived.model_copy(update={'status': SubjectStatus.ACTIVE, 'updated_at': NOW}),
        expected_version=archived.version,
    )
    release.set()
    await running
    assert (await basic.read('u')).fields['age'].value is None
    assert (await profile.get_profile('u'))['overview']['age'] is None
    assert not (await profile.list_facts('u'))['items']


@pytest.mark.asyncio
async def test_unadmitted_old_job_is_not_rebound_to_new_lifecycle(composition):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    archived = await repo.update_subject(
        subject.model_copy(update={'status': SubjectStatus.ARCHIVED, 'updated_at': NOW}),
        expected_version=subject.version,
    )
    active = await repo.update_subject(
        archived.model_copy(update={'status': SubjectStatus.ACTIVE, 'updated_at': NOW}),
        expected_version=archived.version,
    )
    with pytest.raises(ProfileIngestionProcessingError) as raised:
        await write_age(repo, active)
    assert raised.value.error_code == 'basic_info.source_epoch_unproven' and raised.value.retryable is False
    assert not collections[1].documents
    assert (await basic.read('u')).fields['age'].value is None


async def formal_flow():
    flow = await setup_v2(outputs=[])
    basic = ProfileBasicInfoService(flow.repository, MongoBasicInfoStore(BsonCollection()), clock=lambda: NOW)
    flow.repository.basic_info = basic
    profile = HealthProfileService(flow.repository, clock=lambda: NOW)
    flow.applier = ProfileSourceApplier(flow.inbox, resolvers=flow.applier.resolvers, extractor=flow.applier.extractor)
    flow.worker = ProfilePublicationWorker(flow.inbox, flow.applier)
    return flow, basic, profile


@pytest.mark.asyncio
async def test_formal_partial_source_publishes_confirmed_basic_without_full_success(monkeypatch):
    flow, basic, profile = await formal_flow()
    event = event_for(flow)
    await fail_to_partial(flow, event, monkeypatch)
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'partial'
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == SOURCE_TIME
    assert (await profile.get_profile('u'))['overview']['age'] == 42


@pytest.mark.asyncio
async def test_formal_withdraw_preserves_independent_confirmation():
    flow, basic, profile = await formal_flow()
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    command = await basic.register('u', 'same-value-confirmation', values=values(age=42), confirmed_at=NOW)
    await basic.apply('u', command.command_id)
    retract = retract_event(flow, revision=2)
    await flow.inbox.receive(retract, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == NOW
    assert (await profile.get_profile('u'))['overview']['age'] == 42


@pytest.mark.asyncio
async def test_formal_existing_event_recovers_same_intent_after_ack_loss(monkeypatch):
    flow, basic, _ = await formal_flow()
    append = flow.repository.append_event
    lost = False

    async def lose_source_event_ack(event):
        nonlocal lost
        result = await append(event)
        if event.event_type == 'source_publication_applied' and not lost:
            lost = True
            raise OSError('synthetic event acknowledgement loss')
        return result

    monkeypatch.setattr(flow.repository, 'append_event', lose_source_event_ack)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    from datetime import timedelta

    flow.now[0] += timedelta(minutes=6)
    assert await flow.worker.recover_pending() == 1
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == SOURCE_TIME and age.governance_epoch == 0


@pytest.mark.asyncio
async def test_historical_event_without_intent_never_creates_fresh_confirmation(composition):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await profile.upsert_subject(
        'u',
        'name-before-historical',
        subject_id=subject.subject_id,
        display_name='Synthetic',
        expected_subject_version=subject.version,
    )
    repo.basic_info = None
    old_writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    _, candidate, job, decision = await write_age(repo, subject, writer=old_writer)
    repo.basic_info = basic
    before = len(collections[-1].documents)
    repaired = HealthProfileFactWriter(repo, clock=lambda: NOW)
    assert not await repaired.write(job=job, subject=subject, candidate=candidate, decision=decision)
    assert len(collections[-1].documents) == before
    assert (await basic.read('u')).fields['age'].value is None


@pytest.mark.asyncio
async def test_provably_new_admission_can_publish_after_lifecycle_restart(composition):
    repo, basic, profile, _ = composition
    from datetime import timedelta

    subject = (await profile.ensure_self_profile('u')).subject
    archived = await repo.update_subject(
        subject.model_copy(update={'status': SubjectStatus.ARCHIVED, 'updated_at': NOW}),
        expected_version=subject.version,
    )
    active = await repo.update_subject(
        archived.model_copy(update={'status': SubjectStatus.ACTIVE, 'updated_at': NOW}),
        expected_version=archived.version,
    )
    admitted = NOW + timedelta(minutes=1)
    basic.clock = lambda: admitted
    candidate = next(row for row in record_candidates(active.subject_id) if row.fact_type == 'demographic.age')
    candidate = candidate.model_copy(
        update={
            'effective_period': candidate.effective_period.model_copy(update={'start': admitted}),
            'source': candidate.source.model_copy(update={'observed_at': admitted}),
        }
    )
    job = record_job(active.subject_id).model_copy(update={'created_at': admitted, 'updated_at': admitted})
    writer = HealthProfileFactWriter(repo, clock=lambda: admitted)
    await writer.write(
        job=job,
        subject=active,
        candidate=candidate,
        decision=HealthFactPolicy().evaluate(candidate, subject_kind=SubjectKind.SELF),
    )
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == admitted
    assert (
        await repo.get_fact('u', active.subject_id, age.fact_id)
    ).basic_info_lifecycle_epoch == active.lifecycle_epoch


@pytest.mark.asyncio
async def test_formal_retraction_keeps_existing_confirmed_record_semantics():
    flow, basic, _ = await formal_flow()
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    await flow.inbox.receive(retract_event(flow), scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == SOURCE_TIME


@pytest.mark.asyncio
async def test_bound_demographic_metadata_jobs_reuse_actual_physical_fact(composition):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    candidate = next(row for row in record_candidates(subject.subject_id) if row.fact_type == 'demographic.age')
    candidate = candidate.model_copy(
        update={
            'assertion_level': AssertionLevel.OBSERVED,
            'source': FactSource(
                source_type='health_report',
                source_id='physical-report',
                source_revision=1,
                pointer='/sourceObservations/v6_synthetic/age',
                observed_at=SOURCE_TIME,
            ),
        }
    )
    policy = HealthFactPolicy()
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    first_job = record_job(subject.subject_id).model_copy(
        update={'source_type': ProfileIngestionSourceType.HEALTH_REPORT, 'source_id': 'physical-report'}
    )
    await writer.write(
        job=first_job,
        subject=subject,
        candidate=candidate,
        decision=policy.evaluate(candidate, subject_kind=SubjectKind.SELF),
    )
    first = (await basic.read('u')).fields['age']
    enriched = candidate.model_copy(
        update={'interpretation': 'normal', 'source': candidate.source.model_copy(update={'source_revision': 2})}
    )
    second_job = first_job.model_copy(update={'job_id': 'metadata-job-2', 'source_revision': 2})
    await writer.write(
        job=second_job,
        subject=subject,
        candidate=enriched,
        decision=policy.evaluate(enriched, subject_kind=SubjectKind.SELF),
    )
    current = (await basic.read('u')).fields['age']
    assert len(collections[1].documents) == 1 and current.fact_id == first.fact_id and current.value == 42
    assert not current.protected
    assert {row.source_revision for row in (await repo.get_fact('u', subject.subject_id, first.fact_id)).sources} == {
        1,
        2,
    }


@pytest.mark.asyncio
async def test_repair_with_original_epoch_tag_uses_metadata_cas_without_version_change(composition):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await write_age(repo, subject)
    effect = next(row for row in collections[-1].documents if row.get('recordKind') == 'basic_info_fact_effect')
    before = await repo.get_fact('u', subject.subject_id, effect['factId'])
    # Emulate a historical completed effect whose immutable plan retained its original epoch.
    collections[-1].documents.remove(effect)
    for document in collections[1].documents:
        if document['factId'] == before.fact_id:
            document.pop('basicInfoLifecycleEpoch', None)
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)
    stored = await repo.get_fact('u', subject.subject_id, before.fact_id)
    await writer.basic_info_bridge.repair(
        stored,
        subject,
        effect['operationId'],
        proof_epoch=subject.lifecycle_epoch,
        source_type='record',
        source_id='record-r1',
    )
    after = await repo.get_fact('u', subject.subject_id, before.fact_id)
    assert after.version == before.version and after.basic_info_lifecycle_epoch == subject.lifecycle_epoch
    assert after.last_request_id == before.last_request_id


@pytest.mark.asyncio
async def test_withdraw_keeps_other_valid_source_for_shared_physical_fact(composition):
    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer, _, _, _ = await write_age(repo, subject)
    current = (await basic.read('u')).fields['age']
    fact = await repo.get_fact('u', subject.subject_id, current.fact_id)
    remaining = FactSource(source_type='chat', source_id='other-owner', source_revision=1, observed_at=SOURCE_TIME)
    shared = fact.model_copy(update={'sources': (*fact.sources, remaining), 'version': fact.version + 1})
    shared = await repo.replace_fact(shared, expected_version=fact.version, allowed_statuses=(fact.status,))
    fence = await writer.basic_info_bridge.prepare(
        shared,
        subject,
        'withdraw-one-owner',
        admitted_at=NOW,
        effect='withdraw',
        source_type='record',
        source_id='record-r1',
        publication_revision=2,
    )
    retained = shared.model_copy(update={'sources': (remaining,), 'version': shared.version + 1})
    retained = await repo.replace_fact(retained, expected_version=shared.version, allowed_statuses=(shared.status,))
    await writer.basic_info_bridge.publish(fence, retained)
    await basic.publish_governance('u', subject.subject_id)
    snapshot = await basic.read('u')
    assert snapshot.fields['age'].value == 42
    assert any(row.active and (row.source_ref or {}).get('sourceId') == 'other-owner' for row in snapshot.sources)
    assert not any(row.active and (row.source_ref or {}).get('sourceId') == 'record-r1' for row in snapshot.sources)


@pytest.mark.asyncio
async def test_old_partial_plan_without_tag_survives_full_redrive(monkeypatch):
    flow, basic, profile = await formal_flow()
    subject = await flow.repository.get_subject('u', flow.scope.subject_id)
    await profile.upsert_subject(
        'u',
        'name-before-old-partial',
        subject_id=subject.subject_id,
        display_name='Synthetic',
        expected_subject_version=subject.version,
    )
    flow.repository.basic_info = None
    flow.applier = ProfileSourceApplier(flow.inbox, resolvers=flow.applier.resolvers, extractor=flow.applier.extractor)
    flow.worker = ProfilePublicationWorker(flow.inbox, flow.applier)
    event = event_for(flow)
    original = await fail_to_partial(flow, event, monkeypatch)
    facts = await flow.repository.list_facts('u', subject.subject_id, statuses=(FactStatus.ACTIVE,))
    old = next(row for row in facts if row.fact_type == 'demographic.age')
    assert old.basic_info_lifecycle_epoch is None
    flow.repository.basic_info = basic
    flow.applier = ProfileSourceApplier(flow.inbox, resolvers=flow.applier.resolvers, extractor=flow.applier.extractor)
    flow.worker = ProfilePublicationWorker(flow.inbox, flow.applier)
    monkeypatch.setattr(flow.gateway, 'complete', original)
    await flow.inbox.redrive(event.event_id, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'
    repaired = await flow.repository.get_fact('u', subject.subject_id, old.fact_id)
    assert repaired.version == old.version and repaired.basic_info_lifecycle_epoch == subject.lifecycle_epoch
    assert (await basic.read('u')).fields['age'].value == 42


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', [False, True])
async def test_formal_bridge_errors_keep_domain_retry_policy(monkeypatch, unavailable):
    flow, _, _ = await formal_flow()

    async def fail_bridge(*args, **kwargs):
        if unavailable:
            raise BasicInfoUnavailable('basic_info.concurrent_source_effect')
        raise BasicInfoConflict('basic_info.source_effect_conflict')

    monkeypatch.setattr(flow.applier.basic_info_bridge, 'prepare', fail_bridge)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == ('retry_wait' if unavailable else 'failed')
    assert receipt.error_code == (
        'basic_info.concurrent_source_effect' if unavailable else 'basic_info.source_effect_conflict'
    )
    assert receipt.partial_application is None


@pytest.mark.asyncio
@pytest.mark.parametrize('retryable', [None, True, False])
async def test_legacy_worker_only_terminally_fails_explicit_nonretryable(composition, retryable):
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.models import ProfileJobStatus
    from health_profile.worker import HealthProfileIngestionWorker

    repo, _, _, collections = composition
    queue = HealthProfileJobQueue(repo, clock=lambda: NOW)

    class Processor:
        async def process(self, job):
            raise ProfileIngestionProcessingError('basic_info.source_effect_conflict', 'synthetic', retryable=retryable)

    enqueued = await queue.enqueue(
        source_type=ProfileIngestionSourceType.RECORD, source_id='worker-source', user_id='u', source_revision=1
    )
    from datetime import timedelta

    claimed = enqueued.job.model_copy(
        update={
            'status': ProfileJobStatus.RUNNING,
            'attempts': 1,
            'lease_owner': 'synthetic-worker',
            'lease_token': 'fixed-token',
            'lease_expires_at': NOW + timedelta(minutes=5),
        }
    )
    collections[4].documents[0] = claimed.to_document(document_id=collections[4].documents[0]['_id'])
    worker = HealthProfileIngestionWorker(queue, Processor(), worker_id='synthetic-worker')
    await worker._process_claimed(claimed)
    document = collections[4].documents[0]
    assert document['status'] == (
        ProfileJobStatus.DEAD_LETTER.value if retryable is False else ProfileJobStatus.RETRY_WAIT.value
    )


@pytest.mark.asyncio
async def test_old_physical_fact_without_event_or_intent_reuses_actual_id(composition, monkeypatch):
    repo, basic, profile, collections = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await profile.upsert_subject(
        'u',
        'name-before-old-ack',
        subject_id=subject.subject_id,
        display_name='Synthetic',
        expected_subject_version=subject.version,
    )
    repo.basic_info = None
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW, id_factory=lambda _: 'old-physical-id')
    append = repo.append_event

    async def fail_before_event(event):
        raise OSError('synthetic crash before event')

    monkeypatch.setattr(repo, 'append_event', fail_before_event)
    with pytest.raises(OSError):
        await write_age(repo, subject, writer=writer)
    repo.basic_info = basic
    monkeypatch.setattr(repo, 'append_event', append)
    current_writer = HealthProfileFactWriter(repo, clock=lambda: NOW, id_factory=lambda _: 'different-random-id')
    await write_age(repo, subject, writer=current_writer)
    age = (await basic.read('u')).fields['age']
    assert len([row for row in collections[1].documents if row['factType'] == 'demographic.age']) == 1
    assert age.value == 42 and age.fact_id == 'old-physical-id' and age.confirmed_at == SOURCE_TIME
    assert not any(row.deleted for row in (await basic.read('u')).sources if row.field == 'age')


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', [False, True])
async def test_legacy_writer_maps_bridge_error_to_processing_domain(composition, monkeypatch, unavailable):
    repo, _, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer = HealthProfileFactWriter(repo, clock=lambda: NOW)

    async def fail_bridge(*args, **kwargs):
        if unavailable:
            raise BasicInfoUnavailable('basic_info.concurrent_source_effect')
        raise BasicInfoConflict('basic_info.source_effect_conflict')

    monkeypatch.setattr(writer.basic_info_bridge, 'prepare', fail_bridge)
    with pytest.raises(ProfileIngestionProcessingError) as raised:
        await write_age(repo, subject, writer=writer)
    assert raised.value.retryable is unavailable
    assert raised.value.error_code == (
        'basic_info.concurrent_source_effect' if unavailable else 'basic_info.source_effect_conflict'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('microsecond', [0, 123456])
async def test_source_effect_fingerprint_survives_bson_naive_utc_roundtrip(composition, microsecond):
    from bson import BSON

    from health_profile.models import HealthFact

    repo, basic, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    writer, _, _, _ = await write_age(repo, subject)
    fact = await repo.get_fact('u', subject.subject_id, (await basic.read('u')).fields['age'].fact_id)
    fact = fact.model_copy(
        update={
            'effective_period': fact.effective_period.model_copy(
                update={
                    'start': fact.effective_period.start.replace(microsecond=microsecond),
                }
            ),
            'sources': tuple(
                source.model_copy(
                    update={
                        'observed_at': source.observed_at.replace(microsecond=microsecond),
                    }
                )
                for source in fact.sources
            ),
        }
    )
    first = await writer.basic_info_bridge.prepare(fact, subject, 'bson-roundtrip', admitted_at=NOW)
    decoded = HealthFact.model_validate(BSON.encode(fact.model_dump(by_alias=True)).decode())
    assert decoded.effective_period.start.tzinfo is None
    repeated = await writer.basic_info_bridge.prepare(decoded, subject, 'bson-roundtrip', admitted_at=NOW)
    assert repeated['_id'] == first['_id'] and repeated['payloadHash'] == first['payloadHash']
