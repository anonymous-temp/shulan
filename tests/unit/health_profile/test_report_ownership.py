"""Report ownership through durable Profile state and the public source worker."""

import asyncio
from dataclasses import replace

import pytest

from health_profile.basic_info_contracts import BasicInfoValuesV1
from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.models import SubjectKind
from health_profile.report_ownership import ReportOwnership
from health_profile.service import HealthProfileService
from health_profile.source_material import ProfileSourceApplicationError
from health_profile.source_material import ProfileSourceMaterial
from health_profile.source_publications import publication_id
from tests.unit._support.current_document_profile import composition
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle


def report(flow, document, name=None, gender=None, *, observations=(), slot=None):
    scope = ProfileScope(user_id=flow.scope.user_id, subject_id=flow.scope.subject_id)
    return ProfileSourceMaterial(
        scope,
        'health_report',
        document,
        1,
        {},
        observations=tuple(observations) or ({'observationRef': '/observations/0', 'patientSlot': slot},),
        basic_info=tuple(
            {'field': field, 'value': value, 'patientSlot': slot}
            for field, value in (('name', name), ('gender', gender))
            if value is not None
        ),
        routing_documents=(document,),
        lifecycle_epoch=flow.epoch,
    )


async def prefill(flow, *, gender):
    operation = await flow.profile.basic_info.register(
        flow.scope.user_id,
        'authoritative-api-values',
        values=BasicInfoValuesV1(name='confirmed-client-name', gender=gender, age=50),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await flow.profile.basic_info.apply(flow.scope.user_id, operation.command_id)


async def clinical_at(flow, subject_id):
    result = await flow.profile.reader.read(flow.scope.user_id, subject_id=subject_id)
    return tuple(fact for fact in result.facts if fact.category != 'demographic')


@pytest.mark.asyncio
@pytest.mark.parametrize(('gender', 'expected_other'), [('female', False), ('male', True)])
async def test_upload_ownership_compares_gender_canonically_and_withdraws_its_own_facts(
    monkeypatch, gender, expected_other
):
    flow = await composition(monkeypatch)
    await prefill(flow, gender=gender)
    await publish(flow)
    subjects = await flow.repository.list_subjects(flow.scope.user_id)
    others = [subject for subject in subjects if subject.kind is SubjectKind.OTHER]
    assert bool(others) is expected_other
    target = others[0].subject_id if others else flow.scope.subject_id
    assert await clinical_at(flow, target)
    assert bool(await clinical_at(flow, flow.scope.subject_id)) is not expected_other
    assert (await info(flow)).fields['name'].value == 'confirmed-client-name'
    if expected_other:
        assert others[0].relationship.relation_type == 'pending'
        other = await flow.profile.basic_info.read(flow.scope.user_id, subject_id=target)
        assert other.fields['gender'].value == 'female'
        assert other.fields['name'].value != 'confirmed-client-name'
    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=flow.env.bundle.material.source_id,
        source_revision=1,
        request_id='withdraw-classified-source',
        expected_prior_revision=1,
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    assert not await clinical_at(flow, target)
    assert (await info(flow)).fields['name'].value == 'confirmed-client-name'


@pytest.mark.asyncio
async def test_two_conflicting_first_reports_use_the_same_identity_cas(monkeypatch):
    flow = await composition(monkeypatch)
    repository = flow.repository
    subject = await repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    ownership = ReportOwnership(repository)
    first = report(flow, 'one', 'Synthetic A', 'male')
    second = report(flow, 'two', 'Synthetic B', 'female')
    results = await asyncio.gather(
        ownership.reserve(subject, first, 'first'), ownership.reserve(subject, second, 'second')
    )
    assert sum(next(iter(result.values()))['subjectId'] == subject.subject_id for result in results) == 1
    for result, material, operation in zip(results, (first, second), ('first', 'second'), strict=True):
        await ownership.materialize(subject, result, flow.profile.inbox.now)
        await ownership.finish(material.scope, flow.epoch, operation)
    install_profile(flow)
    restarted = ReportOwnership(flow.repository)
    assert await restarted.lookup(first.scope, flow.epoch, 'one') == results[0]['one', '']
    assert await restarted.lookup(first.scope, flow.epoch, 'two') == results[1]['two', '']
    repeated = await restarted.reserve(subject, replace(first, basic_info=()), 'report-alias')
    assert repeated == results[0]
    state = await restarted.basic._scope(subject.user_id, subject.subject_id, flow.epoch)
    assert not state['reportRoutes'] and not state['reportRoutingPending']


@pytest.mark.asyncio
async def test_explicit_multi_parses_distinct_slots_and_never_uses_unknown_rows_for_self(monkeypatch):
    flow = await composition(monkeypatch)
    await prefill(flow, gender='female')
    repository = flow.repository
    ownership = ReportOwnership(repository)
    subject = await repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    a = report(flow, 'multi', 'confirmed-client-name', 'female', slot='patient-a')
    b = report(flow, 'multi', 'someone else', 'male', slot='patient-b')
    material = replace(
        a,
        basic_info=(*a.basic_info, *b.basic_info),
        observations=(
            a.observations[0],
            {'observationRef': '/observations/1', 'patientSlot': 'patient-b'},
            {'observationRef': '/observations/2'},
        ),
    )
    targets = await ownership.reserve(subject, material, 'multi')
    assert targets['multi', 'patient-a']['subjectId'] == subject.subject_id
    assert len({row['subjectId'] for row in targets.values()}) == 3
    assert ('multi', 'unassigned') in targets
    await ownership.materialize(subject, targets, flow.profile.inbox.now)
    await ownership.finish(material.scope, flow.epoch, 'multi')
    with pytest.raises(ProfileSourceApplicationError, match='source.document_patient_ownership_conflict'):
        await ownership.reserve(subject, report(flow, 'multi'), 'different-layout')


@pytest.mark.asyncio
async def test_missing_identity_defaults_to_the_explicit_non_self_subject(monkeypatch):
    flow = await composition(monkeypatch)
    service = HealthProfileService(flow.repository)
    target = (
        await service.upsert_subject(
            flow.scope.user_id,
            'known-relative',
            kind='blood_relative',
            relationship={'relationType': 'parent', 'isBloodRelative': True},
        )
    ).subject
    ownership = ReportOwnership(flow.repository)
    material = report(flow, 'no-identity')
    material = replace(material, scope=ProfileScope(user_id=target.user_id, subject_id=target.subject_id))
    result = await ownership.reserve(target, material, 'explicit-target-upload')
    assert result['no-identity', '']['subjectId'] == target.subject_id


@pytest.mark.asyncio
async def test_terminal_unapplied_first_identity_does_not_block_a_new_report(monkeypatch):
    flow = await composition(monkeypatch)
    ownership = ReportOwnership(flow.repository)
    subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    abandoned = report(flow, 'failed', 'Synthetic A', 'male')
    assert (await ownership.reserve(subject, abandoned, 'failed'))['failed', '']['subjectId'] == subject.subject_id
    await ownership.finish(abandoned.scope, flow.epoch, 'failed', failed=True)
    assert await ownership.lookup(abandoned.scope, flow.epoch, 'failed') is None
    fresh = report(flow, 'fresh', 'Synthetic B', 'female')
    assert (await ownership.reserve(subject, fresh, 'fresh'))['fresh', '']['subjectId'] == subject.subject_id
    with pytest.raises(ProfileSourceApplicationError, match='source.document_patient_ownership_conflict'):
        await ownership.reserve(subject, abandoned, 'failed')


@pytest.mark.asyncio
async def test_grouped_report_uses_each_originals_identity(monkeypatch):
    flow = await composition(monkeypatch)
    subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    ownership = ReportOwnership(flow.repository)
    first = report(flow, 'first', 'Synthetic A', 'male')
    second = report(flow, 'second', 'Synthetic B', 'female')

    def bind(material, field):
        document = material.routing_documents[0]
        return tuple({**row, 'sourceDocumentId': document} for row in getattr(material, field))

    joined = replace(
        first,
        basic_info=bind(first, 'basic_info') + bind(second, 'basic_info'),
        observations=bind(first, 'observations') + bind(second, 'observations'),
        routing_documents=('first', 'second'),
    )
    targets = await ownership.reserve(subject, joined, 'two-originals')
    assert targets['first', '']['subjectId'] == subject.subject_id
    assert targets['second', '']['subjectId'] != subject.subject_id
    await ownership.materialize(subject, targets, flow.profile.inbox.now)
    await ownership.finish(joined.scope, flow.epoch, 'two-originals')
    assert await ownership.lookup(joined.scope, flow.epoch, 'first') == targets['first', '']
    assert await ownership.lookup(joined.scope, flow.epoch, 'second') == targets['second', '']


@pytest.mark.asyncio
async def test_grouping_one_single_and_one_multiple_patient_original_keeps_single_route(monkeypatch):
    flow = await composition(monkeypatch)
    subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    ownership = ReportOwnership(flow.repository)
    first = report(flow, 'first')
    original = await ownership.reserve(subject, first, 'first-only')
    await ownership.finish(first.scope, flow.epoch, 'first-only')
    grouped = replace(
        first,
        observations=(
            {'observationRef': '/observations/0', 'sourceDocumentId': 'first'},
            {'observationRef': '/observations/1', 'sourceDocumentId': 'second', 'patientSlot': 'patient-a'},
            {'observationRef': '/observations/2', 'sourceDocumentId': 'second'},
        ),
        routing_documents=('first', 'second'),
    )
    targets = await ownership.reserve(subject, grouped, 'both-originals')
    assert targets['first', ''] == original['first', '']
    assert {document for document, _ in targets} == {'first', 'second'}
    assert ('second', 'unassigned') in targets and ('first', 'unassigned') not in targets
    assert len({value['subjectId'] for value in targets.values()}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('before_plan', 'cleanup_lost', 'dual_conflict'),
    [(False, False, False), (True, True, False), (False, True, True)],
)
async def test_first_report_redrive_recovers_only_its_compatible_fixed_target(
    monkeypatch, before_plan, cleanup_lost, dual_conflict
):
    flow = await composition(monkeypatch)
    worker = flow.profile.worker

    async def interrupted(*_args, **_kwargs):
        raise ProfileSourceApplicationError('source.synthetic_terminal_failure')

    async def cleanup_interrupted(*_args):
        pass  # Failure state persisted immediately before the cleanup call was lost.

    method = '_plan_material' if before_plan else '_reserve_physical_facts'
    monkeypatch.setattr(worker.applier, method, interrupted)
    if cleanup_lost:
        monkeypatch.setattr(worker, '_finish_failed_ownership', cleanup_interrupted)
    await flow.runtime.publisher.publish(
        scope=flow.scope,
        snapshot_id=flow.capture.source_snapshot_ref,
        request_id='first-source-publication',
        expected_prior_revision=None,
    )
    assert await worker.recover_pending() == 0
    event = ProfileSourceEventV1.model_validate(next(iter(flow.ledger.intents.documents.values()))['event'])
    identity = publication_id(event)
    failed = await flow.profile.inbox.store.get(identity)
    assert failed['status'] == 'failed'
    assert (failed['plan'] is None) == before_plan
    assert not await clinical_at(flow, flow.scope.subject_id)

    if dual_conflict:
        await prefill(flow, gender='male')
    install_profile(flow)
    await flow.profile.inbox.redrive(event.event_id, scope=event.scope)
    applied = await flow.profile.worker.recover_pending()
    receipt = await flow.profile.inbox.get_receipt(event.event_id, scope=event.scope)
    assert applied == (0 if dual_conflict else 1)
    assert receipt.status == ('failed' if dual_conflict else 'applied')
    assert bool(await clinical_at(flow, flow.scope.subject_id)) is not dual_conflict
    subjects = await flow.repository.list_subjects(flow.scope.user_id)
    assert [subject.subject_id for subject in subjects] == [flow.scope.subject_id]
    ownership = ReportOwnership(flow.repository)
    lookup = await ownership.lookup(event.scope, flow.epoch, flow.env.document['_id'])
    if dual_conflict:
        assert receipt.error_code == 'source.document_patient_ownership_conflict'
        assert lookup is None
    else:
        assert lookup == {'subjectId': flow.scope.subject_id, 'lifecycleEpoch': flow.epoch}
        assert (await info(flow)).fields['name'].value
