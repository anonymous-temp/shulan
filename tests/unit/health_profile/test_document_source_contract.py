"""Complete public Document materials, exact readiness, and existing Profile governance."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import ProfileSourceSnapshotRefV1
from health_profile.contracts import profile_content_digest
from health_profile.document_source import ProfileDocumentSourceResolver
from health_profile.document_source_contracts import DOCUMENT_SOURCE_CONTRACT
from health_profile.document_source_contracts import DocumentContributionV1
from health_profile.source_material import ProfileSourceApplicationError
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import publication_id

from .test_public_contracts import setup_flow
from .test_public_contracts import visible


def complete_source(flow, *, value='12', legacy_value='9', revision=1):
    identity = {
        'scope': flow.scope.model_dump(by_alias=True),
        'sourceType': 'health_report',
        'sourceId': 'report-1',
        'sourceRevision': 1,
    }
    contributions = []
    for kind, label, val in [('base', 'ALT', value), ('legacy_report_source', 'AST', legacy_value)]:
        row = DocumentContributionV1(
            contribution_id=kind,
            kind=kind,
            snapshot_id='part-' + kind + str(revision),
            content_digest='0' * 64,
            source_revision=1,
            legacy_source_revision=9173322013456,
            implementation_selection={
                'profileName': 'health_report' if kind == 'base' else 'report_source',
                'contractVersion': '1',
                'implementationRevision': 'analysis-health-report-v12' if kind == 'base' else 'report-source-v16',
            },
            report_type='lab',
            report_time='2026-11-01',
            observations=(
                {
                    'observationRef': '/observations/0',
                    'identityStatus': 'identity_unresolved',
                    'label': label,
                    'rawValue': val,
                    'unit': 'U/L',
                    'evidence': [{'quote': val}],
                },
            ),
        )
        row = row.model_copy(
            update={'content_digest': profile_content_digest(row.canonical(scope=flow.scope, source_id='report-1'))}
        )
        contributions.append(row)
    inventory = {
        **identity,
        'contractVersion': 'document.source-inventory.v1',
        'payload': {
            'fence': {
                'evidenceRef': 'real-owner-fence-1',
                'publishersQuiesced': True,
                'workersQuiesced': True,
                'legacyIntentsRetired': True,
            },
            'activeSetEvidenceRef': 'active-set-evidence-1',
            'retiredContributions': [],
            'legacyState': 'captured',
            'legacySourceRevision': 9173322013456,
            'contributions': [row.reference().model_dump(mode='json', by_alias=True) for row in contributions],
        },
    }
    inventory_ref = ProfileSourceSnapshotRefV1(
        provider='document', contract_version='document.source-inventory.v1', snapshot_id='inventory-' + str(revision)
    )
    raw = {
        **identity,
        'contractVersion': DOCUMENT_SOURCE_CONTRACT,
        'payload': {
            'quality': {'status': 'accepted'},
            'materialSnapshotRefs': [
                {
                    'snapshotId': 'original-1',
                    'contractVersion': 'document.material.v1',
                    'contentDigest': 'a' * 64,
                    'documentId': 'document-1',
                    'sourceRevision': 1,
                }
            ],
            'contributions': [row.model_dump(mode='json', by_alias=True) for row in contributions],
            'coverage': {
                **identity,
                'contractVersion': 'document.source-coverage.v1',
                'legacySourceRevision': 9173322013456,
                'inventorySnapshotRef': inventory_ref.model_dump(by_alias=True),
                'inventoryDigest': profile_content_digest(inventory),
                'orderedContributionIds': [row.contribution_id for row in contributions],
                'complete': True,
            },
        },
    }
    flow.inventories[inventory_ref.snapshot_id] = inventory
    return event_for(flow, raw, revision=revision)


def event_for(flow, raw, *, revision=1, kind=None):
    digest = profile_content_digest(raw)
    snapshot_id = ('document_snapshot_' if kind == 'retract' else 'document_source_snapshot_') + digest
    flow.materials[snapshot_id] = deepcopy(raw)
    return ProfileSourceEventV1(
        scope=flow.scope,
        event_id='document-' + str(revision),
        source_type='health_report',
        source_id='report-1',
        source_revision=1,
        publication_revision=revision,
        prior_revision=revision - 1 if revision > 1 else None,
        event_kind=kind or ('publish' if revision == 1 else 'correct'),
        content_digest=digest,
        occurred_at=flow.now[0],
        snapshot_ref=ProfileSourceSnapshotRefV1(
            provider='document', contract_version=raw['contractVersion'], snapshot_id=snapshot_id
        ),
    )


async def setup_document(*, authorize=True):
    flow = await setup_flow(authorize=False)
    flow.materials, flow.inventories = {}, {}
    flow.public_reader = SimpleNamespace(
        read_source_observations=AsyncMock(
            side_effect=lambda scope, snapshot_id: deepcopy(flow.materials[snapshot_id])
        ),
        read_source_inventory=AsyncMock(side_effect=lambda scope, snapshot_id: deepcopy(flow.inventories[snapshot_id])),
        read_retraction=AsyncMock(side_effect=lambda scope, snapshot_id: deepcopy(flow.materials[snapshot_id])),
    )
    flow.resolver = ProfileDocumentSourceResolver(flow.public_reader)
    flow.inbox.document_source = flow.resolver
    flow.applier.resolvers[('document', DOCUMENT_SOURCE_CONTRACT)] = flow.resolver
    flow.applier.resolvers[('document', 'document.retraction.v1')] = flow.resolver
    flow.event = complete_source(flow)
    flow.authorization = flow.authorization.model_copy(
        update={
            'material_contract_version': DOCUMENT_SOURCE_CONTRACT,
            'material_snapshot_ref': flow.event.snapshot_ref,
            'material_content_digest': flow.event.content_digest,
            'processing_revision': flow.resolver.VERSION,
            'legacy_source_intents_retired': True,
            'legacy_quiescence_evidence_ref': 'real-owner-fence-1',
        }
    )
    if authorize:
        await flow.inbox.authorize_cutover(flow.authorization, scope=flow.scope)
    return flow


def retract(flow, revision=2):
    raw = {
        'scope': flow.scope.model_dump(by_alias=True),
        'contractVersion': 'document.retraction.v1',
        'sourceType': 'health_report',
        'sourceId': 'report-1',
        'sourceRevision': 1,
        'publicationRevision': revision,
        'priorRevision': revision - 1,
        'eventKind': 'retract',
    }
    return event_for(flow, raw, revision=revision, kind='retract')


@pytest.mark.asyncio
async def test_complete_source_maps_base_and_legacy_v16_then_retracts_both():
    flow = await setup_document()
    ready = await flow.resolver.inspect(
        flow.event.snapshot_ref, scope=flow.scope, source_id='report-1', content_digest=flow.event.content_digest
    )
    assert ready.contribution_ids == ('base', 'legacy_report_source')
    await flow.inbox.receive(flow.event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    facts = await visible(flow)
    assert len(facts) == 2
    assert {fact.sources[0].pointer for fact in facts} == {
        '/contributions/base/observations/0',
        '/contributions/legacy_report_source/observations/0',
    }
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.processing_revision == 'profile-document-source-v1'
    event = retract(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert not await visible(flow)
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_incomplete_source_never_authorizes_or_removes_existing_contributions():
    flow = await setup_document(authorize=False)
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    raw['payload']['contributions'].pop()
    raw['payload']['coverage']['orderedContributionIds'].pop()
    event = event_for(flow, raw)
    auth = flow.authorization.model_copy(
        update={'material_snapshot_ref': event.snapshot_ref, 'material_content_digest': event.content_digest}
    )
    with pytest.raises(ProfileSourceApplicationError, match='inventory_contributions_mismatch'):
        await flow.inbox.authorize_cutover(auth, scope=flow.scope)
    assert await flow.inbox.get_cutover(flow.scope, 'health_report', 'report-1') is None
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['scope', 'digest', 'order', 'extra', 'selection', 'revision', 'fence', 'legacy'])
async def test_exact_public_readiness_rejects_unverifiable_material(change):
    flow = await setup_document(authorize=False)
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    inv = flow.inventories['inventory-1']
    if change == 'scope':
        raw['scope']['userId'] = 'other'
    if change == 'digest':
        raw['payload']['contributions'][1]['observations'][0]['rawValue'] = '999'
    if change == 'order':
        raw['payload']['coverage']['orderedContributionIds'].reverse()
    if change == 'extra':
        raw['payload']['contributions'][1]['observations'][0]['factType'] = 'condition.diagnosis'
    if change == 'selection':
        raw['payload']['contributions'][1]['implementationSelection']['implementationRevision'] = (
            'new-private-projection'
        )
    if change == 'revision':
        raw['payload']['materialSnapshotRefs'][0]['sourceRevision'] = 2
    if change == 'fence':
        inv['payload']['fence']['workersQuiesced'] = False
    if change == 'legacy':
        inv['payload']['legacyState'] = 'absent'
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inv)
    event = event_for(flow, raw)
    with pytest.raises(ProfileSourceApplicationError):
        await flow.resolver.inspect(
            event.snapshot_ref, scope=flow.scope, source_id='report-1', content_digest=event.content_digest
        )
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_missing_receiver_and_missing_fence_are_hard_cutover_failures():
    flow = await setup_document(authorize=False)
    flow.inbox.document_source = None
    with pytest.raises(ProfileSourceConflictError, match='receiver_not_ready'):
        await flow.inbox.authorize_cutover(flow.authorization, scope=flow.scope)
    flow.inbox.document_source = flow.resolver
    with pytest.raises(ProfileSourceApplicationError, match='quiescence_evidence_required'):
        await flow.inbox.authorize_cutover(
            flow.authorization.model_copy(update={'legacy_quiescence_evidence_ref': None}), scope=flow.scope
        )
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_fixed_material_ack_replay_does_not_require_reader_but_new_revision_does():
    flow = await setup_document()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    calls = flow.public_reader.read_source_observations.call_count
    flow.inbox.document_source = None
    receipt = await flow.inbox.receive(flow.event, scope=flow.scope)
    assert receipt.status == 'received'
    assert flow.public_reader.read_source_observations.call_count == calls
    with pytest.raises(ProfileSourceConflictError, match='receiver_not_ready'):
        await flow.inbox.receive(complete_source(flow, revision=2), scope=flow.scope)


@pytest.mark.asyncio
async def test_correct_out_of_order_preserves_both_contributions_and_replay_versions():
    flow = await setup_document()
    second = complete_source(flow, value='18', legacy_value='11', revision=2)
    await flow.inbox.receive(second, scope=flow.scope)
    await flow.worker.recover_pending()
    assert not await visible(flow)
    await flow.inbox.receive(flow.event, scope=flow.scope)
    await flow.worker.recover_pending()
    await flow.worker.recover_pending()
    assert len(await visible(flow)) == 2
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).status == 'applied'
    assert (await flow.inbox.store.get(publication_id(second)))['processingSelection'][
        'mapperVersion'
    ] == 'health-report-candidate-mapper-v2'


@pytest.mark.asyncio
async def test_version_drift_after_freeze_does_not_apply_latest(monkeypatch):
    flow = await setup_document()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    original = flow.applier.mapper.map

    def interrupted(*args):
        raise RuntimeError('offline interruption')

    monkeypatch.setattr(flow.applier.mapper, 'map', interrupted)
    await flow.worker.recover_pending()
    monkeypatch.setattr(flow.applier.mapper, 'map', original)
    monkeypatch.setattr(flow.resolver, 'VERSION', 'unavailable-new-version')
    from datetime import timedelta

    flow.now[0] += timedelta(hours=1)
    await flow.worker.recover_pending()
    assert not await visible(flow)
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.processing_version_unavailable'


@pytest.mark.asyncio
async def test_retired_history_is_audit_only_and_cannot_reenter_active_set():
    flow = await setup_document(authorize=False)
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    inventory = flow.inventories['inventory-1']
    retired = deepcopy(inventory['payload']['contributions'][1])
    retired.update(
        contributionId='old-supplement',
        snapshotId='old-snapshot',
        retirementKind='corrected',
        retirementEvidenceRef='retired-proof-1',
        supersededByContributionId='legacy_report_source',
    )
    inventory['payload']['retiredContributions'] = [retired]
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inventory)
    event = event_for(flow, raw)
    material = await flow.resolver.resolve(event)
    assert len(material.observations) == 2
    assert not any('old-supplement' in item['observationRef'] for item in material.observations)
    inventory['payload']['retiredContributions'][0]['snapshotId'] = raw['payload']['contributions'][1]['snapshotId']
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inventory)
    event = event_for(flow, raw)
    with pytest.raises(ProfileSourceApplicationError, match='retired_contribution_active'):
        await flow.resolver.resolve(event)


@pytest.mark.asyncio
async def test_no_active_set_proof_cannot_claim_complete():
    flow = await setup_document(authorize=False)
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    inventory = flow.inventories['inventory-1']
    del inventory['payload']['activeSetEvidenceRef']
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inventory)
    event = event_for(flow, raw)
    with pytest.raises(ProfileSourceApplicationError, match='contract_invalid'):
        await flow.resolver.resolve(event)


@pytest.mark.asyncio
async def test_alias_interruption_recovers_frozen_processing_without_live_reader(monkeypatch):
    flow = await setup_document()
    insert = flow.inbox.store.insert

    async def interrupt(document):
        if document['recordKind'] == 'publication':
            raise RuntimeError('offline crash after alias')
        return await insert(document)

    monkeypatch.setattr(flow.inbox.store, 'insert', interrupt)
    with pytest.raises(RuntimeError, match='offline crash'):
        await flow.inbox.receive(flow.event, scope=flow.scope)
    alias = next(row for row in flow.collections['publications'].documents if row['recordKind'] == 'alias')
    selected = deepcopy(alias['processingSelection'])
    assert selected['implementationRevision'] == 'profile-document-source-v1'
    flow.inbox.document_source = None
    monkeypatch.setattr(flow.inbox.store, 'insert', insert)
    await flow.inbox.receive(flow.event, scope=flow.scope)
    saved = await flow.inbox.store.get(publication_id(flow.event))
    assert saved['processingSelection'] == selected


@pytest.mark.asyncio
async def test_incomplete_governance_is_settled_by_retraction_without_late_revival(monkeypatch):
    flow = await setup_document()
    flow.worker.max_attempts = 1
    await flow.inbox.receive(flow.event, scope=flow.scope)
    original = flow.applier._create
    calls = []

    async def interrupt(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError('offline crash mid-plan')
        return await original(*args, **kwargs)

    monkeypatch.setattr(flow.applier, '_create', interrupt)
    await flow.worker.recover_pending()
    assert len(await visible(flow)) == 1
    assert (await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)).status != 'applied'
    event = retract(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    monkeypatch.setattr(flow.applier, '_create', original)
    await flow.worker.recover_pending()
    await flow.worker.recover_pending()
    assert not await visible(flow)
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.application_state == 'superseded_by_retraction'
    assert (await flow.inbox.get_receipt(event.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_public_fact_set_and_legacy_bridge_cannot_bypass_document_cutover():
    flow = await setup_document()
    event = flow.event.model_copy(
        update={'snapshot_ref': flow.event.snapshot_ref.model_copy(update={'contract_version': 'document.facts.v1'})}
    )
    with pytest.raises(ProfileSourceConflictError, match='cutover_material_mismatch'):
        await flow.inbox.receive(event, scope=flow.scope)
    event = flow.event.model_copy(
        update={
            'snapshot_ref': flow.event.snapshot_ref.model_copy(
                update={'provider': 'health_report', 'contract_version': 'health-report-observations.v1'}
            )
        }
    )
    with pytest.raises(ProfileSourceConflictError, match='complete_source_required'):
        await flow.inbox.receive(event, scope=flow.scope)


@pytest.mark.asyncio
async def test_human_confirmation_still_blocks_source_correction():
    from health_profile.models import AssertionLevel
    from health_profile.models import VerificationStatus

    flow = await setup_document()
    await flow.inbox.receive(flow.event, scope=flow.scope)
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
    event = complete_source(flow, value='90', legacy_value='80', revision=2)
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.manual_governance_requires_review'
    assert (await flow.repository.get_fact('u', fact.subject_id, fact.fact_id)).value == fact.value


@pytest.mark.asyncio
async def test_real_legacy_v16_neutral_observations_preserve_mapping_and_dates():
    from health_profile.report_ingestion import HealthReportCandidateMapper
    from health_profile.report_source import HealthReportSourceRecord
    from utils.report_source_observations import report_source_items

    flow = await setup_document(authorize=False)
    legacy = {
        'sample': {'reportDate': '2026-09-01'},
        'taxa': [{'name': 'Bifidobacterium', 'value': '3.1', 'unit': '%'}],
        'supplementaryHospitalData': [
            {'dataType': 'INSPECTION', 'itemName': 'AST', 'testResultValue': '33 U/L', 'reportTime': '2026-10-01'}
        ],
    }
    observations = []
    for pointer, item in report_source_items(legacy):
        observations.append(
            {
                'observationRef': '/reportSource' + pointer,
                'identityStatus': 'identity_unresolved',
                'label': item['itemName'],
                'rawValue': item['resultValue'],
                'unit': item.get('unit'),
                'observedAt': item.get('observedAt'),
                'referenceRange': item.get('referenceRange'),
                'flag': item.get('flag'),
                'kind': item.get('kind'),
                'sourceCategory': item['sourceCategory'],
            }
        )
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    supplement = DocumentContributionV1.model_validate(
        {**raw['payload']['contributions'][1], 'observations': observations}
    )
    supplement = supplement.model_copy(
        update={'content_digest': profile_content_digest(supplement.canonical(scope=flow.scope, source_id='report-1'))}
    )
    raw['payload']['contributions'][1] = supplement.model_dump(mode='json', by_alias=True)
    inventory = flow.inventories['inventory-1']
    inventory['payload']['contributions'][1] = supplement.reference().model_dump(mode='json', by_alias=True)
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inventory)
    event = event_for(flow, raw)
    material = await flow.resolver.resolve(event)
    mapped, _ = flow.applier.mapper.map(event, material)
    old = HealthReportSourceRecord(
        report_id='report-1',
        source_revision=9173322013456,
        user_id='u',
        subject_id=flow.scope.subject_id,
        source_fingerprint='old',
        result={
            'reportType': 'lab',
            'reportTime': '2026-11-01',
            'observations': [{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L'}],
            'reportSource': legacy,
        },
        created_at=flow.now[0],
        updated_at=flow.now[0],
        ingestion_status='queued',
        ingestion_job_id='old-job',
    )
    expected = HealthReportCandidateMapper().map(old)
    assert len(mapped) == len(expected.candidates) >= 2
    assert [(x.fact_type, x.value, x.effective_period) for x in mapped] == [
        (x.fact_type, x.value, x.effective_period) for x in expected.candidates
    ]


@pytest.mark.asyncio
async def test_unstarted_event_already_freezes_processing_version(monkeypatch):
    flow = await setup_document()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    saved = await flow.inbox.store.get(publication_id(flow.event))
    assert saved['processingSelection']['implementationRevision'] == 'profile-document-source-v1'
    monkeypatch.setattr(flow.resolver, 'VERSION', 'incompatible-new-version')
    await flow.worker.recover_pending()
    assert not await visible(flow)
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.processing_version_unavailable'


@pytest.mark.asyncio
async def test_public_composition_requires_both_exact_resolvers(monkeypatch):
    from health_profile import application

    flow = await setup_document(authorize=False)
    monkeypatch.setattr(application, 'collection_health_profile_source_publications', flow.collections['publications'])
    half = {('document', DOCUMENT_SOURCE_CONTRACT): flow.resolver}
    collection_type = type(flow.collections['facts'])
    store = application.MongoHealthReportStore(collection_type(), collection_type())
    public = application._build_profile_public_contracts(flow.repository, store, resolvers=half)
    assert public.inbox.document_source is None
    public = application._build_profile_public_contracts(
        flow.repository, store, resolvers=flow.resolver.registrations()
    )
    assert public.inbox.document_source is flow.resolver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'quality', [{}, {'status': 'unavailable'}, {'status': 'partial'}, {'status': 'completed'}, {'status': 'unknown'}]
)
async def test_unaccepted_source_quality_cannot_inspect_cutover_or_replace_facts(quality):
    flow = await setup_document(authorize=False)
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    raw['payload']['quality'] = quality
    invalid = event_for(flow, raw)
    with pytest.raises(ProfileSourceApplicationError, match='contract_invalid'):
        await flow.resolver.inspect(
            invalid.snapshot_ref, scope=flow.scope, source_id='report-1', content_digest=invalid.content_digest
        )
    auth = flow.authorization.model_copy(
        update={'material_snapshot_ref': invalid.snapshot_ref, 'material_content_digest': invalid.content_digest}
    )
    with pytest.raises(ProfileSourceApplicationError, match='contract_invalid'):
        await flow.inbox.authorize_cutover(auth, scope=flow.scope)
    assert not flow.collections['publications'].documents
    await flow.inbox.authorize_cutover(flow.authorization, scope=flow.scope)
    await flow.inbox.receive(flow.event, scope=flow.scope)
    await flow.worker.recover_pending()
    before = deepcopy(flow.collections['facts'].documents)
    replacement = event_for(flow, raw, revision=2)
    with pytest.raises(ProfileSourceApplicationError, match='contract_invalid'):
        await flow.inbox.receive(replacement, scope=flow.scope)
    assert flow.collections['facts'].documents == before
    assert await flow.inbox.get_receipt(replacement.event_id, scope=flow.scope) is None
