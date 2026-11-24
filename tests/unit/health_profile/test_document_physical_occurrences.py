"""Exercise verified physical occurrences through the public Profile publication path."""

from asyncio import Event
from asyncio import gather
from asyncio import wait_for
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pymongo import ReturnDocument

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import profile_content_digest
from health_profile.models import FactStatus
from health_profile.models import SubjectStatus
from health_profile.service import HealthProfileService
from health_profile.source_material import ProfileSourceApplicationError
from modules.health_profile import ProfileCurrentDocumentSourceResolver
from modules.health_profile import create_application_profile_public_contracts
from tests.unit._support.current_document_profile import profile_scope

OBSERVATIONS = 'document.source-observations.v2'
CONTRIBUTION = 'document.source-contribution.v2'
INVENTORY = 'document.source-inventory.v2'
RETRACTION = 'document.retraction.v1'
CHECKSUM = 'a' * 64
DOC_ID = 'same-original'


def observation(source, *, locator='table:0:row:2', pointer='/observations/0', proof=True, **changes):
    row = {
        'observationRef': pointer,
        'identityStatus': 'identity_unresolved',
        'label': 'ALT',
        'rawValue': '12',
        'unit': 'U/L',
        'observedAt': '2026-11-01T00:00:00+08:00',
    }
    if proof:
        row['physicalOccurrence'] = {
            'documentId': DOC_ID,
            'sourceChecksum': CHECKSUM,
            'locatorRevision': 'report-understanding-current.v3:' + '1' * 64,
            'locator': locator,
            'proofSnapshotRef': 'document_snapshot_' + ('b' if source == 'analysis' else 'c') * 64,
        }
    return {**row, **changes}


class FixedReader:
    def __init__(self):
        self.snapshots = {}
        self.fixed_refs = {}

    async def read_fixed_observation_refs(self, *, scope, snapshot_id):
        assert scope.model_dump(by_alias=True) == self.snapshots[snapshot_id]['scope']
        return deepcopy(self.fixed_refs.get(snapshot_id, {}))

    def save(self, raw):
        digest = profile_content_digest(raw)
        prefix = 'document_source_snapshot_' if raw['contractVersion'] == OBSERVATIONS else 'document_snapshot_'
        ref = {
            'provider': 'document',
            'contractVersion': raw['contractVersion'],
            'snapshotId': prefix + digest,
        }
        self.snapshots[ref['snapshotId']] = deepcopy(raw)
        return ref, digest

    async def read_source_observations(self, *, scope, snapshot_id):
        data = self.snapshots[snapshot_id]
        assert scope.model_dump(by_alias=True) == data['scope']
        return deepcopy(data)

    read_source_inventory = read_source_observations
    read_retraction = read_source_observations


def source_event(flow, source, *, rows=None, groups=(), revision=1, retract=False, retained_contribution_id=None):
    identity = {
        'scope': flow.scope.model_dump(by_alias=True),
        'sourceType': 'health_report',
        'sourceId': source,
        'sourceRevision': revision,
    }
    kind = 'retract' if retract else 'publish' if revision == 1 else 'correct'
    if retract:
        raw = {
            **identity,
            'contractVersion': RETRACTION,
            'publicationRevision': revision,
            'priorRevision': revision - 1,
            'eventKind': kind,
            'lifecycleEpoch': flow.epoch,
        }
    else:
        refs, contributions = [], []
        for index, group in enumerate(([observation(source)] if rows is None else rows, *groups)):
            contribution_id = f'{source}-clinical-{revision}'
            material = {
                'contributionId': (retained_contribution_id or contribution_id)
                if index == 0
                else f'{contribution_id}-{index}',
                'kind': 'clinical',
                'implementationSelection': {
                    'profileName': 'health_report',
                    'contractVersion': 'current',
                    'implementationRevision': 'fixed-synthetic-producer',
                },
                'reportType': 'lab',
                'reportTime': '2026-11-01',
                'observations': group,
                'basicInfo': [],
                'clinicalContentContract': 'document.clinical-content.v1',
                'clinicalContentDigest': '3' * 64,
            }
            contribution_ref, contribution_digest = flow.fixed.save(
                {**identity, 'contractVersion': CONTRIBUTION, 'payload': material}
            )
            clinical = {
                **material,
                'contractVersion': CONTRIBUTION,
                'sourceRevision': revision,
                'snapshotId': contribution_ref['snapshotId'],
                'contentDigest': contribution_digest,
            }
            contributions.append(clinical)
            refs.append(
                {
                    key: value
                    for key, value in clinical.items()
                    if key not in {'reportType', 'reportTime', 'observations'}
                }
            )
        inventory_ref, inventory_digest = flow.fixed.save(
            {
                **identity,
                'contractVersion': INVENTORY,
                'payload': {
                    'fence': {'admissionKind': 'current', 'evidenceRef': 'fixed-synthetic-plan'},
                    'lifecycleEpoch': flow.epoch,
                    'activeSetEvidenceRef': 'fixed-synthetic-plan',
                    'retiredContributions': [],
                    'contributions': refs,
                },
            }
        )
        raw = {
            **identity,
            'contractVersion': OBSERVATIONS,
            'payload': {
                'quality': {'status': 'accepted'},
                'materialSnapshotRefs': [
                    {
                        'snapshotId': 'document_snapshot_' + '4' * 64,
                        'contractVersion': 'document.material.v1',
                        'contentDigest': '4' * 64,
                        'documentId': DOC_ID,
                        'sourceRevision': revision,
                    }
                ],
                'basicInfo': [],
                'clinicalAvailable': True,
                'contributions': refs,
                'clinicalContributions': contributions,
                'coverage': {
                    **identity,
                    'contractVersion': 'document.source-coverage.v2',
                    'lifecycleEpoch': flow.epoch,
                    'inventorySnapshotRef': inventory_ref,
                    'inventoryDigest': inventory_digest,
                    'orderedContributionIds': [ref['contributionId'] for ref in refs],
                    'complete': True,
                },
            },
        }
    ref, digest = flow.fixed.save(raw)
    return ProfileSourceEventV1(
        scope=flow.scope,
        event_id=f'{source}-{kind}-{revision}',
        source_type='health_report',
        source_id=source,
        source_revision=revision,
        publication_revision=revision,
        prior_revision=revision - 1 if revision > 1 else None,
        event_kind=kind,
        content_digest=digest,
        occurred_at=flow.now[0],
        snapshot_ref=ref,
    )


@pytest_asyncio.fixture
async def flow(monkeypatch):
    cols, _, scope = await profile_scope(monkeypatch)
    fixed = FixedReader()
    now = [datetime(2026, 11, 13, 12, tzinfo=UTC)]

    def start():
        resolver = ProfileCurrentDocumentSourceResolver(fixed)
        profile = create_application_profile_public_contracts(
            resolvers=resolver.registrations(), basic_info_collection=cols['basic_info']
        )
        profile.inbox.clock = lambda: now[0]
        profile.basic_info.clock = lambda: now[0]
        return profile

    profile = start()
    scope = ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id)
    epoch = (await profile.inbox.repository.get_subject(scope.user_id, scope.subject_id)).lifecycle_epoch
    return SimpleNamespace(scope=scope, epoch=epoch, fixed=fixed, profile=profile, now=now, restart=start)


async def run(flow, event, *, status='applied'):
    await flow.profile.inbox.receive(event, scope=flow.scope)
    await flow.profile.worker.recover_pending()
    receipt = await flow.profile.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == status, receipt
    return receipt


async def profile_facts(flow):
    snapshot = await flow.profile.reader.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    return tuple(fact for fact in snapshot.facts if fact.category != 'demographic')


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['analysis', 'report'])
async def test_same_original_keeps_one_fact_and_independent_retractable_sources(flow, first):
    second = 'report' if first == 'analysis' else 'analysis'
    for source in (first, second):
        rows = [observation(source, rawValue='12.0' if source == 'report' else '12')]
        groups = ()
        if source == 'report':
            groups = ([observation(source, method='enzymatic')],)
            rows[0]['observedAt'] = '2026-10-31T16:00:00Z'
        event = source_event(flow, source, rows=rows, groups=groups)
        await run(flow, event)
        (fact,) = await profile_facts(flow)
        if source == first:
            first_id = fact.fact_id
        else:
            assert fact.fact_id == first_id
    assert fact.value['method'] == 'enzymatic'
    assert [(item.source_id, item.pointer) for item in fact.sources] == [
        ('analysis', '/contributions/analysis-clinical-1/observations/0'),
        ('report', '/contributions/report-clinical-1-1/observations/0'),
        ('report', '/contributions/report-clinical-1/observations/0'),
    ]
    assert 'physicalOrigins' not in fact.model_dump(by_alias=True)

    await run(flow, source_event(flow, first, revision=2, retract=True))
    (remaining,) = await profile_facts(flow)
    assert remaining.fact_id == first_id
    assert {item.source_id for item in remaining.sources} == {second}
    assert remaining.value.get('method') == ('enzymatic' if second == 'report' else None)
    await run(flow, source_event(flow, second, revision=2, retract=True))
    assert not await profile_facts(flow)
    stored = await flow.profile.inbox.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, first_id)
    assert stored.status is FactStatus.RETRACTED and all(origin.reading is None for origin in stored.physical_origins)


@pytest.mark.asyncio
async def test_same_values_at_distinct_positions_and_unproven_rows_stay_separate(flow):
    await run(flow, source_event(flow, 'analysis'))
    rows = [
        observation('report', locator='table:0:row:3'),
        observation('report', pointer='/observations/1', proof=False),
    ]
    await run(flow, source_event(flow, 'report', rows=rows))
    facts = await profile_facts(flow)
    assert len(facts) == 3 and len({part.fact_id for part in facts}) == 3


@pytest.mark.asyncio
async def test_contradictory_readings_keep_existing_fact_and_block_application(flow):
    await run(flow, source_event(flow, 'analysis'))
    (before,) = await profile_facts(flow)
    altered = observation('report', rawValue='99')
    receipt = await run(flow, source_event(flow, 'report', rows=[altered]), status='failed')
    assert receipt.error_code == 'source.physical_occurrence_conflict'
    assert (await profile_facts(flow)) == (before,)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['document', 'checksum', 'snapshot'])
async def test_invalid_original_proof_is_rejected_before_intake(flow, change):
    row = observation('analysis')
    if change == 'document':
        row['physicalOccurrence']['documentId'] = 'different-document'
    elif change == 'checksum':
        row['physicalOccurrence']['sourceChecksum'] = 'opaque-id'
    else:
        row['physicalOccurrence']['proofSnapshotRef'] = 'invented'
    event = source_event(flow, 'analysis', rows=[row])
    with pytest.raises(ProfileSourceApplicationError, match='source.document_(physical_occurrence|contract)'):
        await flow.profile.inbox.receive(event, scope=flow.scope)
    assert await flow.profile.inbox.get_receipt(event.event_id, scope=flow.scope) is None


@pytest.mark.asyncio
async def test_correction_replaces_own_proof_and_recovers_lost_receipt_without_duplicate_fact(flow, monkeypatch):
    await run(flow, source_event(flow, 'analysis'))
    await run(flow, source_event(flow, 'report'))
    (before,) = await profile_facts(flow)
    correction = source_event(flow, 'analysis', revision=2)
    await flow.profile.inbox.receive(correction, scope=flow.scope)

    async def lose_receipt(*args, **kwargs):
        raise RuntimeError('synthetic ACK loss')

    with monkeypatch.context() as patch:
        patch.setattr(flow.profile.worker.applier, '_record_effect', lose_receipt)
        await flow.profile.worker.recover_pending()
    flow.profile = flow.restart()
    flow.now[0] += timedelta(minutes=1)
    await run(flow, correction)
    (after,) = await profile_facts(flow)
    assert after.fact_id == before.fact_id
    assert [(s.source_id, s.source_revision) for s in after.sources] == [('analysis', 2), ('report', 1)]
    stored = await flow.profile.inbox.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, after.fact_id)
    assert [(o.source_id, o.publication_revision) for o in stored.physical_origins] == [('analysis', 2), ('report', 1)]
    assert after.version == before.version + 1


@pytest.mark.asyncio
async def test_two_sources_can_begin_materializing_the_same_fact_concurrently(flow, monkeypatch):
    peer = flow.restart()
    inbox = flow.profile.inbox
    events = [source_event(flow, source) for source in ('analysis', 'report')]
    for event in events:
        await inbox.receive(event, scope=flow.scope)
    arrived = Event()
    counter = 0

    def install(repository):
        create_fact = repository.create_fact

        async def hold_both(fact):
            nonlocal counter
            counter += 1
            if counter == 2:
                arrived.set()
            await arrived.wait()
            return await create_fact(fact)

        monkeypatch.setattr(repository, 'create_fact', hold_both)

    install(inbox.repository)
    install(peer.inbox.repository)
    await gather(flow.profile.worker.recover_pending(), peer.worker.recover_pending())
    flow.now[0] += timedelta(minutes=1)
    await flow.profile.worker.recover_pending()
    assert counter == 2
    for event in events:
        receipt = await inbox.get_receipt(event.event_id, scope=flow.scope)
        assert receipt.status == 'applied', receipt
    (fact,) = await profile_facts(flow)
    assert {item.source_id for item in fact.sources} == {'analysis', 'report'}


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['reject', 'replace', 'delete'])
async def test_manual_reject_or_delete_blocks_the_same_original_from_another_source(flow, decision):
    def pending(source):
        return observation(source, kind='narrative', narrativeType='finding', label='Finding', rawValue='Example.')

    analysis = source_event(flow, 'analysis', rows=[pending('analysis')])
    await run(flow, analysis)
    (fact,) = await profile_facts(flow)
    repository = flow.profile.inbox.repository
    service = HealthProfileService(repository, clock=lambda: flow.now[0])
    view = await repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    keys = {
        'expected_fact_version': fact.version,
        'expected_profile_version': view.profile_version,
        'request_id': 'physical-occurrence-user-action',
    }
    if decision == 'delete':
        await service.delete_fact(flow.scope.user_id, flow.scope.subject_id, fact.fact_id, delete_scope='fact', **keys)
    else:
        replacement = (
            {
                'valueType': 'text',
                'value': 'Manual correction.',
                'effectivePeriod': {'start': '2026-11-01T00:00:00+08:00'},
            }
            if decision == 'replace'
            else None
        )
        await service.resolve_fact(
            flow.scope.user_id, flow.scope.subject_id, fact.fact_id, decision, replacement=replacement, **keys
        )
    await run(flow, analysis)
    receipt = await run(flow, source_event(flow, 'report', rows=[pending('report')]), status='failed')
    assert receipt.error_code == 'source.manual_governance_requires_review'
    await run(flow, source_event(flow, 'analysis', revision=2, retract=True))
    remaining = await profile_facts(flow)
    if decision == 'replace':
        (correction,) = remaining
        assert correction.value == 'Manual correction.'
        assert correction.fact_id != fact.fact_id
        stored = await repository.get_fact(flow.scope.user_id, flow.scope.subject_id, correction.fact_id)
        assert stored.basic_info_lifecycle_epoch == flow.epoch
        await rotate_lifecycle(flow)
        assert not await profile_facts(flow)
    else:
        assert not remaining


async def rotate_lifecycle(flow):
    repository = flow.profile.inbox.repository
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        current = await repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        await repository.update_subject(
            current.model_copy(update={'status': status, 'version': current.version + 1, 'updated_at': flow.now[0]}),
            expected_version=current.version,
        )
    flow.epoch = (await repository.get_subject(flow.scope.user_id, flow.scope.subject_id)).lifecycle_epoch


@pytest.mark.asyncio
async def test_same_original_in_new_lifecycle_gets_a_new_identity(flow):
    await run(flow, source_event(flow, 'analysis'))
    (before,) = await profile_facts(flow)
    await rotate_lifecycle(flow)
    await run(flow, source_event(flow, 'report'))
    (after,) = await profile_facts(flow)
    assert after.fact_id != before.fact_id
    assert {item.source_id for item in after.sources} == {'report'}


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['analysis', 'report'])
async def test_user_confirmation_remains_after_both_sources_are_withdrawn(flow, first):
    events = {
        source: source_event(
            flow,
            source,
            rows=[observation(source, kind='narrative', narrativeType='finding', label='Finding', rawValue='Example.')],
        )
        for source in ('analysis', 'report')
    }
    for event in events.values():
        await run(flow, event)
    (fact,) = await profile_facts(flow)
    repository = flow.profile.inbox.repository
    service = HealthProfileService(repository, clock=lambda: flow.now[0])
    view = await repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    await service.resolve_fact(
        flow.scope.user_id,
        flow.scope.subject_id,
        fact.fact_id,
        'confirm',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='user-confirm-same-original',
    )
    for event in events.values():
        await run(flow, event)
    (confirmed,) = await profile_facts(flow)
    assert confirmed.fact_id == fact.fact_id and confirmed.version == fact.version + 1
    assert confirmed.status == 'active'
    assert {item.source_id for item in confirmed.sources} == {'analysis', 'report'}
    second = 'report' if first == 'analysis' else 'analysis'
    await run(flow, source_event(flow, first, revision=2, retract=True))
    (remaining,) = await profile_facts(flow)
    assert {item.source_type for item in remaining.sources} == {'manual', 'health_report'}
    assert {item.source_id for item in remaining.sources if item.source_type == 'health_report'} == {second}
    await run(flow, source_event(flow, second, revision=2, retract=True))
    (manual,) = await profile_facts(flow)
    assert manual.fact_id == fact.fact_id and manual.value == fact.value
    assert manual.status == 'active' and manual.verification_status == 'confirmed'
    assert [item.source_type for item in manual.sources] == ['manual']


@pytest.mark.asyncio
@pytest.mark.parametrize('correcting', [False, True])
async def test_competing_reading_fails_before_creating_or_withdrawing_other_facts(flow, monkeypatch, correcting):
    from health_profile import source_application

    peer = flow.restart()
    locators = sorted(
        ('table:0:row:2', 'table:0:row:3'),
        key=lambda locator: profile_content_digest(
            [
                flow.scope.user_id,
                flow.scope.subject_id,
                flow.epoch,
                DOC_ID,
                CHECKSUM,
                'report-understanding-current.v3:' + '1' * 64,
                locator,
            ]
        ),
    )
    earlier, contested = locators
    analysis = source_event(flow, 'analysis', rows=[observation('analysis', locator=contested)])
    if correcting:
        await run(
            flow,
            source_event(
                flow,
                'report',
                rows=[
                    observation('report', locator=earlier, rawValue='22'),
                    observation('report', locator='table:0:row:5', rawValue='33', pointer='/observations/1'),
                ],
            ),
        )
    before = await profile_facts(flow)
    report = source_event(
        flow,
        'report',
        revision=2 if correcting else 1,
        rows=[
            observation('report', locator=earlier, rawValue='23'),
            observation('report', locator=contested, rawValue='99', pointer='/observations/1'),
            observation('report', proof=False, rawValue='44', pointer='/observations/2'),
        ],
    )
    reserve = source_application.reserve_physical_fact
    interleaved = False
    attempts = []

    async def reserve_after_winner(applier, cursor, event, fact):
        nonlocal interleaved
        if event.source_id == 'report':
            attempts.append(fact.value['number'])
        if event.source_id == 'report' and fact.value['number'] == 99 and not interleaved:
            interleaved = True
            await peer.inbox.receive(analysis, scope=flow.scope)
            assert await peer.worker.recover_pending() == 1
            winner = source_event(flow, 'analysis', revision=2, rows=[observation('analysis', locator=contested)])
            await peer.inbox.receive(winner, scope=flow.scope)
            assert await peer.worker.recover_pending() == 1
        await reserve(applier, cursor, event, fact)

    monkeypatch.setattr(source_application, 'reserve_physical_fact', reserve_after_winner)
    receipt = await run(flow, report, status='failed')
    assert receipt.error_code == 'source.physical_occurrence_conflict' and interleaved
    assert attempts == [23, 99]
    facts = await profile_facts(flow)
    (winner,) = [fact for fact in facts if {source.source_id for source in fact.sources} == {'analysis'}]
    assert winner.value['number'] == 12 and winner.sources[0].source_revision == 2
    assert {fact.fact_id: fact for fact in facts if fact.fact_id != winner.fact_id} == {
        fact.fact_id: fact for fact in before
    }
    page = await flow.profile.inbox.repository.list_facts(
        flow.scope.user_id,
        flow.scope.subject_id,
        statuses=tuple(status for status in FactStatus if status != 'deleted'),
    )
    assert [fact for fact in page if fact.value['number'] in {23, 44, 99}] == []
    assert not any(fact.physical_reservations for fact in page)


@pytest.mark.asyncio
async def test_reservations_resume_after_lease_expiry_without_early_visibility(flow, monkeypatch):
    from health_profile import source_application

    events = source_event(
        flow,
        'report',
        rows=[
            observation('report', locator='table:0:row:3', rawValue='23'),
            observation('report', locator='table:0:row:5', rawValue='33', pointer='/observations/1'),
        ],
    )
    await flow.profile.inbox.receive(events, scope=flow.scope)
    reserve = source_application.reserve_physical_fact
    held = []

    async def expire_once(applier, cursor, event, fact):
        await reserve(applier, cursor, event, fact)
        held.append(fact)
        if len(held) == 1:
            assert not await profile_facts(flow)
            repository = flow.profile.inbox.repository
            assert await repository.get_fact(flow.scope.user_id, flow.scope.subject_id, fact.fact_id) is None
            flow.now[0] += timedelta(minutes=3)

    with monkeypatch.context() as patch:
        patch.setattr(source_application, 'reserve_physical_fact', expire_once)
        assert await flow.profile.worker.recover_pending() == 0
    assert len(held) == 1
    assert not await profile_facts(flow)
    flow.profile = flow.restart()
    await run(flow, events)
    assert sorted(fact.value['number'] for fact in await profile_facts(flow)) == [23, 33]


@pytest.mark.asyncio
async def test_terminal_reservation_cleanup_recovers_after_ack_loss(flow, monkeypatch):
    from health_profile import source_application

    earlier, conflict = sorted(
        ('table:0:row:2', 'table:0:row:3'),
        key=lambda locator: profile_content_digest(
            [
                flow.scope.user_id,
                flow.scope.subject_id,
                flow.epoch,
                DOC_ID,
                CHECKSUM,
                'report-understanding-current.v3:' + '1' * 64,
                locator,
            ]
        ),
    )
    await run(flow, source_event(flow, 'analysis', rows=[observation('analysis', locator=conflict)]))
    report = source_event(
        flow,
        'report',
        rows=[
            observation('report', locator=earlier, rawValue='23'),
            observation('report', locator=conflict, pointer='/observations/1', rawValue='99'),
        ],
    )
    await flow.profile.inbox.receive(report, scope=flow.scope)
    release = source_application.release_physical_fact
    interrupted = []

    async def lose_cleanup_ack(applier, cursor, event, fact):
        await release(applier, cursor, event, fact)
        interrupted.append(fact.fact_id)
        raise RuntimeError('synthetic cleanup acknowledgement loss')

    with monkeypatch.context() as patch:
        patch.setattr(source_application, 'release_physical_fact', lose_cleanup_ack)
        assert await flow.profile.worker.recover_pending() == 0
    assert len(interrupted) == 1
    flow.now[0] += timedelta(minutes=1)
    flow.profile = flow.restart()
    receipt = await run(flow, report, status='failed')
    assert receipt.error_code == 'source.physical_occurrence_conflict'
    await run(
        flow, source_event(flow, 'independent', rows=[observation('independent', locator=earlier, rawValue='24')])
    )
    assert sorted(fact.value['number'] for fact in await profile_facts(flow)) == [12, 24]


def fixed_analysis_event(
    flow, source, *, round_number=1, dated=False, revision=1, retained_contribution_id=None, **changes
):
    trusted = source == 'analysis'
    row = observation(
        source,
        observedAt='2026-11-01T00:00:00+08:00' if dated else None,
        sourceDocumentId=DOC_ID,
        verificationStatus=changes.pop('verificationStatus', 'confirmed' if trusted else 'verified_source'),
        proof=not trusted,
        **changes,
    )
    if trusted:
        row['confirmationBasis'] = 'analysis_public'
    pointer = row['observationRef']
    event = source_event(flow, source, rows=[row], revision=revision, retained_contribution_id=retained_contribution_id)
    flow.fixed.fixed_refs[event.snapshot_ref.snapshot_id] = {
        (retained_contribution_id or f'{source}-clinical-{revision}'): {
            pointer: [{'documentId': DOC_ID, 'interpretationRound': round_number, 'observationRef': pointer}]
        }
    }
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['analysis', 'report'])
async def test_trusted_analysis_merges_only_the_exact_verified_fixed_observation(flow, first):
    events = {
        'analysis': fixed_analysis_event(flow, 'analysis'),
        'report': fixed_analysis_event(flow, 'report', dated=True),
    }
    second = 'report' if first == 'analysis' else 'analysis'
    await run(flow, events[first])
    (before,) = await profile_facts(flow)
    analysis_recorded_at = flow.now[0] if first == 'analysis' else flow.now[0] + timedelta(minutes=1)
    flow.now[0] += timedelta(minutes=1)
    await run(flow, events[second])
    (merged,) = await profile_facts(flow)
    assert merged.fact_id == before.fact_id
    assert merged.status == 'active'
    assert merged.verification_status == 'confirmed'
    assert {source.source_id for source in merged.sources} == set(events)
    by_source = {source.source_id: source for source in merged.sources}
    assert by_source['analysis'].confirmation_basis == 'analysis_public'
    assert by_source['analysis'].recorded_at == analysis_recorded_at
    assert by_source['report'].confirmation_basis is None
    assert by_source['report'].recorded_at is None

    different_row = observation('distinct-report', locator='table:0:row:3', pointer='/observations/1')
    await run(flow, source_event(flow, 'distinct-report', rows=[different_row]))
    assert len(await profile_facts(flow)) == 2
    await run(flow, source_event(flow, 'analysis', revision=2, retract=True))
    remaining = next(row for row in await profile_facts(flow) if row.fact_id == merged.fact_id)
    assert remaining.status == 'active'
    assert remaining.verification_status == 'verified_source'
    assert [source.source_id for source in remaining.sources] == ['report']


@pytest.mark.asyncio
async def test_undated_automatic_confirmation_recovers_fixed_time_and_allows_correction(flow, monkeypatch):
    first = fixed_analysis_event(flow, 'analysis')
    await flow.profile.inbox.receive(first, scope=flow.scope)

    async def lose_receipt(*args, **kwargs):
        raise RuntimeError('synthetic receipt loss')

    original_time = flow.now[0]
    with monkeypatch.context() as patch:
        patch.setattr(flow.profile.worker.applier, '_record_effect', lose_receipt)
        await flow.profile.worker.recover_pending()
    flow.profile = flow.restart()
    flow.now[0] += timedelta(minutes=1)
    await run(flow, first)
    (before,) = await profile_facts(flow)
    assert before.effective_period['start'] is None
    assert before.sources[0].observed_at is None
    assert before.sources[0].recorded_at == original_time
    assert before.verification_status == 'confirmed'
    assert before.assertion_level == 'observed'

    newer = fixed_analysis_event(flow, 'analysis', round_number=2, revision=2)
    await run(flow, newer)
    (after,) = await profile_facts(flow)
    assert after.fact_id != before.fact_id
    assert after.sources[0].recorded_at == flow.now[0]
    assert after.verification_status == 'confirmed'
    await run(flow, source_event(flow, 'analysis', revision=3, retract=True))
    assert not await profile_facts(flow)


@pytest.mark.asyncio
async def test_fixed_report_reference_rejects_unverified_alias(flow):
    event = fixed_analysis_event(flow, 'report')
    aliases = flow.fixed.fixed_refs[event.snapshot_ref.snapshot_id]
    aliases['report-clinical-1']['/observations/0'][0]['documentId'] = 'unrelated-original'
    with pytest.raises(ProfileSourceApplicationError, match='source.document_fixed_observation_ref_mismatch'):
        await flow.profile.inbox.receive(event, scope=flow.scope)
    assert await flow.profile.inbox.get_receipt(event.event_id, scope=flow.scope) is None


@pytest.mark.asyncio
async def test_republished_retained_observation_does_not_get_a_new_recording_time(flow):
    first = fixed_analysis_event(flow, 'analysis')
    await run(flow, first)
    (before,) = await profile_facts(flow)
    flow.now[0] += timedelta(minutes=1)
    correction = fixed_analysis_event(flow, 'analysis', revision=2, retained_contribution_id='analysis-clinical-1')
    await run(flow, correction)
    (after,) = await profile_facts(flow)
    assert after.fact_id == before.fact_id
    assert after.sources[0].source_revision == 2
    assert after.sources[0].recorded_at == before.sources[0].recorded_at


@pytest.mark.asyncio
async def test_existing_native_observation_identity_stays_fixed_as_other_sources_join(flow):
    await run(flow, source_event(flow, 'record'))
    (native,) = await profile_facts(flow)
    await run(flow, fixed_analysis_event(flow, 'report', dated=True))
    await run(flow, fixed_analysis_event(flow, 'analysis'))
    (merged,) = await profile_facts(flow)
    assert merged.fact_id == native.fact_id
    assert merged.verification_status == 'confirmed'
    assert {source.source_id for source in merged.sources} == {'analysis', 'record', 'report'}


@pytest.mark.asyncio
async def test_concurrent_first_plans_with_one_fixed_alias_have_one_fact_id(flow, monkeypatch):
    peer = flow.restart()
    events = [fixed_analysis_event(flow, 'analysis'), fixed_analysis_event(flow, 'report', dated=True)]
    for event in events:
        await flow.profile.inbox.receive(event, scope=flow.scope)

    plans = []
    both_planned = Event()

    def install(applier):
        plan = applier._plan

        async def synchronize(*args, **kwargs):
            fixed = await plan(*args, **kwargs)
            plans.append(fixed['activeFactIds'])
            if len(plans) == 2:
                both_planned.set()
            await both_planned.wait()
            return fixed

        monkeypatch.setattr(applier, '_plan', synchronize)

    install(flow.profile.worker.applier)
    install(peer.worker.applier)
    await wait_for(gather(flow.profile.worker.recover_pending(), peer.worker.recover_pending()), timeout=5)
    assert len(plans) == 2 and plans[0] == plans[1]
    flow.now[0] += timedelta(minutes=1)
    await flow.profile.worker.recover_pending()
    for event in events:
        receipt = await flow.profile.inbox.get_receipt(event.event_id, scope=flow.scope)
        assert receipt.status == 'applied'
    (fact,) = await profile_facts(flow)
    assert fact.fact_id in plans[0]
    assert {source.source_id for source in fact.sources} == {'analysis', 'report'}


@pytest.mark.asyncio
async def test_deleting_native_fact_preserves_fixed_alias_barrier(flow):
    await run(flow, source_event(flow, 'record'))
    await run(flow, fixed_analysis_event(flow, 'report', dated=True))
    (fact,) = await profile_facts(flow)
    repository = flow.profile.inbox.repository
    service = HealthProfileService(repository, clock=lambda: flow.now[0])
    view = await repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    await service.delete_fact(
        flow.scope.user_id,
        flow.scope.subject_id,
        fact.fact_id,
        delete_scope='fact',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='fixed-occurrence-manual-deletion',
    )
    receipt = await run(flow, fixed_analysis_event(flow, 'analysis'), status='failed')
    assert receipt.error_code == 'source.manual_governance_requires_review'
    assert not await profile_facts(flow)


@pytest.mark.asyncio
async def test_legacy_deleted_native_fact_blocks_report_from_adopting_new_alias(flow):
    await run(flow, source_event(flow, 'record'))
    (fact,) = await profile_facts(flow)
    repository = flow.profile.inbox.repository
    service = HealthProfileService(repository, clock=lambda: flow.now[0])
    view = await repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    await service.delete_fact(
        flow.scope.user_id,
        flow.scope.subject_id,
        fact.fact_id,
        delete_scope='fact',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='legacy-occurrence-manual-deletion',
    )
    deleted = await repository.get_deleted_fact_tombstone(flow.scope.user_id, flow.scope.subject_id, fact.fact_id)
    # Old deletions retained the fact ID and erased the explicit occurrence key.
    key = deleted.pop('physicalOccurrenceKey')
    assert fact.fact_id == 'fact_physical_' + key
    await repository._facts.find_one_and_replace(
        {'userId': flow.scope.user_id, 'subjectId': flow.scope.subject_id, 'factId': fact.fact_id},
        deleted,
        return_document=ReturnDocument.AFTER,
    )

    report = fixed_analysis_event(flow, 'report', dated=True)
    receipt = await run(flow, report, status='failed')
    assert receipt.error_code == 'source.manual_governance_requires_review'
    assert not await profile_facts(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['analysis', 'report'])
async def test_proven_bridge_keeps_original_unit_when_verifier_adds_optional_qualifiers(flow, first):
    analysis = fixed_analysis_event(
        flow,
        'analysis',
        label='Synthetic assay',
        unit='10\u2079/L',
        specimen='Serum',
    )
    report = fixed_analysis_event(
        flow,
        'report',
        dated=True,
        label='Synthetic assay',
        unit='10^9/l',
        specimen='\uff33erum',
        method='Synthetic method',
    )
    order = (analysis, report) if first == 'analysis' else (report, analysis)
    await run(flow, order[0])
    (initial,) = await profile_facts(flow)
    await run(flow, order[1])
    (merged,) = await profile_facts(flow)
    assert merged.fact_id == initial.fact_id
    assert merged.fact_type == initial.fact_type
    assert merged.value['unit'] == '10\u2079/L'
    assert merged.value['specimen'] == 'Serum'
    assert merged.value['method'] == 'Synthetic method'
    assert merged.verification_status == 'confirmed'


@pytest.mark.asyncio
async def test_proven_undated_report_observation_merges_without_confirming_report_only_policy(flow):
    report = fixed_analysis_event(flow, 'report', verificationStatus='unconfirmed')
    await run(flow, report)
    (pending,) = await profile_facts(flow)
    assert pending.status == 'pending'

    await run(flow, fixed_analysis_event(flow, 'analysis'))
    (active,) = await profile_facts(flow)
    assert active.fact_id == pending.fact_id
    assert active.status == 'active'
    assert active.verification_status == 'confirmed'
    assert active.effective_period['start'] is None

    await run(flow, source_event(flow, 'analysis', revision=2, retract=True))
    (restored,) = await profile_facts(flow)
    assert restored.fact_id == pending.fact_id
    assert restored.status == 'pending'
    assert restored.verification_status == 'unconfirmed'
    assert restored.sources[0].recorded_at is None
