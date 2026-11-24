"""Offline exact public-shape tests; no Document implementation dependency."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import profile_content_digest
from health_profile.document_source_contracts_v2 import DOCUMENT_PROCESSING_REVISION
from health_profile.document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT
from health_profile.document_source_current import ProfileCurrentDocumentSourceResolver
from health_profile.source_material import ProfileSourceApplicationError

SCOPE = ProfileScope(user_id='test-user', subject_id='test-self')
IDENTITY = {
    'scope': SCOPE.model_dump(by_alias=True),
    'sourceType': 'health_report',
    'sourceId': 'test-report',
    'sourceRevision': 1,
}


def basic(field='name', value='测试受检者', *, contribution_id='basic-name'):
    quote = str(value)
    return {
        'field': field,
        'value': value,
        'sourceReliability': 'medium',
        'observedAt': None,
        'reason': '原文中的单一受检者信息',
        'evidence': {'quote': quote, 'start': 0, 'end': len(quote)},
        'subjectStatus': 'single',
        'subjectRef': None,
        'subjectEvidence': None,
        'contributionId': contribution_id,
        'understandingSnapshotRef': 'document_snapshot_' + '1' * 64,
        'factsSnapshotRef': 'document_snapshot_' + '2' * 64,
        'interpretationRound': 2,
        'interpretationRevision': 'test-extractor-revision',
    }


def contribution(kind, *, field=None, scope=SCOPE, contribution_id=None):
    identity = contribution_id or ('basic-' + field['field'] if field is not None else kind)
    payload = {
        'contributionId': identity,
        'kind': kind,
        'implementationSelection': {
            'profileName': 'health_report',
            'contractVersion': 'current',
            'implementationRevision': 'test-document-renderer',
        },
        'reportType': 'lab',
        'reportTime': '2026-11-01',
        'observations': []
        if field
        else [
            {
                'observationRef': '/observations/0',
                'identityStatus': 'identity_unresolved',
                'label': 'ALT',
                'rawValue': '12',
                'unit': 'U/L',
                'observedAt': None,
            }
        ],
        'basicInfo': [{**field, 'contributionId': identity}] if field else [],
        'clinicalContentContract': None if field else 'document.clinical-content.v1',
        'clinicalContentDigest': None if field else '3' * 64,
    }
    contract = 'document.source-contribution.v2'
    digest = profile_content_digest(
        {
            **IDENTITY,
            'scope': scope.model_dump(by_alias=True),
            'contractVersion': contract,
            'payload': payload,
        }
    )
    full = {
        **payload,
        'contractVersion': contract,
        'sourceRevision': 1,
        'snapshotId': 'document_snapshot_' + digest,
        'contentDigest': digest,
    }
    ref = {key: value for key, value in full.items() if key not in {'reportType', 'reportTime', 'observations'}}
    return ref, None if field else full


def fixture(*, with_basic=True, with_clinical=True, retired=(), scope=SCOPE, lifecycle_epoch=3):
    identity = {**IDENTITY, 'scope': scope.model_dump(by_alias=True)}
    rows = []
    if with_clinical:
        rows.append(contribution('clinical', scope=scope))
    if with_basic:
        rows.append(contribution('basic_info', field=basic(), scope=scope))
    refs = [row for row, _ in rows]
    inventory = {
        **identity,
        'contractVersion': 'document.source-inventory.v2',
        'payload': {
            'fence': {
                'admissionKind': 'current',
                'evidenceRef': 'test-persistent-plan',
            },
            'lifecycleEpoch': lifecycle_epoch,
            'activeSetEvidenceRef': 'test-persistent-plan',
            'retiredContributions': list(deepcopy(retired)),
            'contributions': deepcopy(refs),
        },
    }
    raw = {
        **identity,
        'contractVersion': DOCUMENT_SOURCE_CONTRACT,
        'payload': {
            'quality': {'status': 'accepted'},
            'materialSnapshotRefs': [
                {
                    'snapshotId': 'document_snapshot_' + '4' * 64,
                    'contractVersion': 'document.material.v1',
                    'contentDigest': '4' * 64,
                    'documentId': 'test-document',
                    'sourceRevision': 1,
                }
            ],
            'basicInfo': [field for row in refs for field in row.get('basicInfo', [])],
            'clinicalAvailable': bool(with_clinical or any(row['kind'] != 'basic_info' for row in retired)),
            'contributions': refs,
            'clinicalContributions': [full for _, full in rows if full is not None],
            'coverage': {
                **identity,
                'contractVersion': 'document.source-coverage.v2',
                'lifecycleEpoch': lifecycle_epoch,
                'inventorySnapshotRef': {},
                'inventoryDigest': '',
                'orderedContributionIds': [row['contributionId'] for row in refs],
                'complete': True,
            },
        },
    }
    flow = SimpleNamespace(materials={}, inventories={}, raw=raw, inventory=inventory, scope=scope)
    flow.reader = SimpleNamespace(
        read_source_observations=AsyncMock(
            side_effect=lambda scope, snapshot_id: deepcopy(flow.materials[snapshot_id])
        ),
        read_source_inventory=AsyncMock(side_effect=lambda scope, snapshot_id: deepcopy(flow.inventories[snapshot_id])),
        read_retraction=AsyncMock(side_effect=lambda scope, snapshot_id: deepcopy(flow.materials[snapshot_id])),
    )
    flow.resolver = ProfileCurrentDocumentSourceResolver(flow.reader)
    rebuild(flow)
    return flow


def rebuild(flow, *, revision=1):
    digest = profile_content_digest(flow.inventory)
    ref = {
        'provider': 'document',
        'contractVersion': 'document.source-inventory.v2',
        'snapshotId': 'document_snapshot_' + digest,
    }
    flow.raw['payload']['coverage'].update(inventorySnapshotRef=ref, inventoryDigest=digest)
    flow.inventories[ref['snapshotId']] = deepcopy(flow.inventory)
    flow.event = event_for(flow, flow.raw, revision=revision)
    return flow.event


def event_for(flow, raw, *, retraction=False, revision=1):
    if retraction and revision == 1:
        revision = 2
    digest = profile_content_digest(raw)
    snapshot_id = ('document_snapshot_' if retraction else 'document_source_snapshot_') + digest
    flow.materials[snapshot_id] = deepcopy(raw)
    return ProfileSourceEventV1(
        scope=flow.scope,
        event_id=('test-retract-' if retraction else 'test-publish-') + str(revision),
        source_type='health_report',
        source_id='test-report',
        source_revision=1,
        publication_revision=revision,
        prior_revision=revision - 1 if revision > 1 else None,
        event_kind='retract' if retraction else 'publish' if revision == 1 else 'correct',
        content_digest=digest,
        occurred_at=datetime(2026, 11, 12, tzinfo=UTC),
        snapshot_ref={'provider': 'document', 'contractVersion': raw['contractVersion'], 'snapshotId': snapshot_id},
    )


def retirement(row):
    return {
        **row,
        'retirementKind': 'retracted',
        'retirementEvidenceRef': 'test-persistent-retirement',
        'supersededByContributionId': None,
    }


async def resolve(flow, **kwargs):
    return await flow.resolver.resolve_fixed(
        flow.event, flow.resolver.processing_manifest(flow.event.snapshot_ref.contract_version), **kwargs
    )


@pytest.mark.asyncio
async def test_v2_keeps_fixed_basic_fields_and_complete_clinical_input_independent():
    flow = fixture()
    material = await resolve(flow, lifecycle_epoch=3)
    assert material.basic_info == tuple(flow.raw['payload']['basicInfo'])
    assert material.basic_info_available and material.clinical_available
    assert material.lifecycle_epoch == 3
    assert material.observations[0]['observedAt'] is None
    assert material.observations[0]['observationRef'] == '/contributions/clinical/observations/0'
    ready = await flow.resolver.inspect(
        flow.event.snapshot_ref, scope=SCOPE, source_id='test-report', content_digest=flow.event.content_digest
    )
    assert ready.contract_version == 'profile-document-source-readiness.v2'
    assert ready.processing_revision == DOCUMENT_PROCESSING_REVISION and ready.lifecycle_epoch == 3
    assert flow.resolver.VERSION == DOCUMENT_PROCESSING_REVISION
    assert set(flow.resolver.registrations()) == {
        ('document', DOCUMENT_SOURCE_CONTRACT),
        ('document', 'document.retraction.v1'),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('with_basic,with_clinical', [(True, False), (False, True)])
async def test_missing_domain_does_not_withdraw_other_domain(with_basic, with_clinical):
    flow = fixture(with_basic=with_basic, with_clinical=with_clinical)
    material = await resolve(flow)
    assert material.basic_info_available is with_basic
    assert material.clinical_available is with_clinical
    assert bool(material.observations) is with_clinical


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['clinical', 'basic_info'])
async def test_retirement_only_requires_persistent_domain_specific_proof(kind):
    ref, _ = contribution(kind, field=basic() if kind == 'basic_info' else None)
    flow = fixture(with_basic=False, with_clinical=False, retired=(retirement(ref),))
    material = await resolve(flow)
    assert not material.observations and not material.basic_info
    assert material.basic_info_available is (kind == 'basic_info')
    assert material.clinical_available is (kind == 'clinical')
    del flow.inventory['payload']['retiredContributions'][0]['retirementEvidenceRef']
    rebuild(flow)
    with pytest.raises(ProfileSourceApplicationError, match='contract_invalid'):
        await resolve(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value,observed_at',
    [
        ('age', 0.5, '2026-11-01'),
        ('gender', '女', '2026-11-01T12:34:56'),
        ('name', '测' * 600, '2026-11-01T12:34:56+08:00'),
    ],
)
async def test_report_values_preserve_fractional_age_original_dates_and_unbounded_name(field, value, observed_at):
    flow = fixture(with_basic=False, with_clinical=False)
    item = basic(field, value)
    item['observedAt'] = observed_at
    ref, _ = contribution('basic_info', field=item)
    flow.raw['payload']['basicInfo'] = ref['basicInfo']
    flow.raw['payload']['contributions'] = flow.inventory['payload']['contributions'] = [ref]
    flow.raw['payload']['coverage']['orderedContributionIds'] = [ref['contributionId']]
    rebuild(flow)
    material = await resolve(flow)
    assert material.basic_info[0]['value'] == value and material.basic_info[0]['observedAt'] == observed_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change',
    [
        'epoch_missing',
        'epoch_zero',
        'epoch_bool',
        'epoch_mismatch',
        'order',
        'inventory_refs',
        'clinical_flag',
        'clinical_missing',
        'basic_missing',
        'retired_active',
        'retired_target',
        'material_ref',
        'contribution_ref',
        'clinical_digest',
        'source_scope',
        'source_hash',
        'inventory_hash',
        'empty',
    ],
)
async def test_v2_rejects_unproven_complete_inventory_and_fixed_bindings(change):
    flow = fixture()
    payload, inventory = flow.raw['payload'], flow.inventory['payload']
    if change == 'epoch_missing':
        del payload['coverage']['lifecycleEpoch']
    elif change == 'epoch_zero':
        payload['coverage']['lifecycleEpoch'] = 0
    elif change == 'epoch_bool':
        payload['coverage']['lifecycleEpoch'] = True
    elif change == 'epoch_mismatch':
        inventory['lifecycleEpoch'] = 4
    elif change == 'order':
        payload['coverage']['orderedContributionIds'].reverse()
    elif change == 'inventory_refs':
        inventory['contributions'] = inventory['contributions'][:1]
    elif change == 'clinical_flag':
        payload['clinicalAvailable'] = False
    elif change == 'clinical_missing':
        payload['clinicalContributions'] = []
    elif change == 'basic_missing':
        payload['basicInfo'] = []
    elif change == 'retired_active':
        inventory['retiredContributions'] = [retirement(inventory['contributions'][0])]
    elif change == 'retired_target':
        row, _ = contribution('basic_info', field=basic('age', 4))
        row = retirement(row)
        row['supersededByContributionId'] = 'nonexistent'
        inventory['retiredContributions'] = [row]
    elif change == 'material_ref':
        payload['materialSnapshotRefs'][0]['snapshotId'] = 'invented-material'
    elif change == 'contribution_ref':
        payload['contributions'][0]['snapshotId'] = 'invented-contribution'
        inventory['contributions'] = deepcopy(payload['contributions'])
    elif change == 'clinical_digest':
        payload['clinicalContributions'][0]['observations'][0]['rawValue'] = '999'
    elif change == 'source_scope':
        flow.raw['scope']['subjectId'] = 'test-other'
    elif change == 'empty':
        payload.update(contributions=[], clinicalContributions=[], basicInfo=[], clinicalAvailable=False)
        inventory['contributions'] = []
        payload['coverage']['orderedContributionIds'] = []
    rebuild(flow)
    if change == 'source_hash':
        flow.materials[flow.event.snapshot_ref.snapshot_id]['payload']['quality']['details'] = {'changed': True}
    elif change == 'inventory_hash':
        flow.inventories[payload['coverage']['inventorySnapshotRef']['snapshotId']]['payload'][
            'activeSetEvidenceRef'
        ] = 'changed'
    with pytest.raises(ProfileSourceApplicationError):
        await resolve(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'key,value',
    [
        ('sourceReliability', 'very_high'),
        ('subjectStatus', 'ambiguous'),
        ('value', ''),
        ('value', None),
        ('reason', ' '),
        ('reason', 'x' * 2001),
        ('evidence', {'quote': '值', 'start': 2, 'end': 2}),
        ('observedAt', 'yesterday'),
        ('factsSnapshotRef', 'latest'),
        ('interpretationRound', True),
    ],
)
async def test_v2_rejects_nonadmissible_basic_field_evidence(key, value):
    flow = fixture(with_clinical=False)
    flow.raw['payload']['basicInfo'][0][key] = value
    flow.raw['payload']['contributions'][0]['basicInfo'] = deepcopy(flow.raw['payload']['basicInfo'])
    flow.inventory['payload']['contributions'] = deepcopy(flow.raw['payload']['contributions'])
    rebuild(flow)
    with pytest.raises(ProfileSourceApplicationError):
        await resolve(flow)


@pytest.mark.asyncio
async def test_fixed_processing_and_lifecycle_must_match_without_selecting_another_implementation():
    flow = fixture()
    fixed = flow.resolver.processing_manifest(DOCUMENT_SOURCE_CONTRACT)
    with pytest.raises(ProfileSourceApplicationError, match='processing_version_unavailable'):
        await flow.resolver.resolve_fixed(flow.event, {**fixed, 'mapperVersion': 'missing-version'})
    with pytest.raises(ProfileSourceApplicationError, match='lifecycle_epoch_mismatch'):
        await resolve(flow, lifecycle_epoch=4)
    assert not hasattr(flow.resolver, 'for_processing')


@pytest.mark.asyncio
async def test_current_retraction_reads_its_own_original_lifecycle_before_any_source_binding():
    flow = fixture()
    raw = {
        **IDENTITY,
        'contractVersion': 'document.retraction.v1',
        'publicationRevision': 2,
        'priorRevision': 1,
        'eventKind': 'retract',
        'lifecycleEpoch': 3,
    }
    event = event_for(flow, raw, retraction=True)
    manifest = flow.resolver.processing_manifest('document.retraction.v1')
    material = await flow.resolver.resolve_fixed(event, manifest)
    assert material.lifecycle_epoch == 3 and material.basic_info_available
    assert material.processing_selection == manifest
    assert flow.reader.read_source_observations.await_count == 0
    with pytest.raises(ProfileSourceApplicationError, match='lifecycle_epoch_mismatch'):
        await flow.resolver.resolve_fixed(event, manifest, lifecycle_epoch=5)
    del raw['lifecycleEpoch']
    event = event_for(flow, raw, retraction=True)
    with pytest.raises(ProfileSourceApplicationError, match='lifecycle_epoch_required'):
        await flow.resolver.resolve_fixed(event, manifest)


async def setup_document_current(**fixture_options):
    """Real receiver/application composition over synthetic Mongo I/O, without bypasses."""
    from health_profile.repository import MongoHealthProfileRepository
    from health_profile.service import HealthProfileService
    from health_profile.snapshots import ProfileSnapshotReader
    from health_profile.source_application import ProfilePublicationWorker
    from health_profile.source_application import ProfileSourceApplier
    from health_profile.source_publications import MongoProfilePublicationStore
    from health_profile.source_publications import ProfileSourceInbox

    from .test_basic_info import BsonCollection

    now = [datetime(2026, 11, 12, tzinfo=UTC)]
    collections = {
        name: BsonCollection()
        for name in ('subjects', 'facts', 'events', 'views', 'jobs', 'basic_info', 'publications')
    }
    repository = MongoHealthProfileRepository(**collections)
    repository.basic_info.clock = lambda: now[0]
    service = HealthProfileService(repository, clock=lambda: now[0])
    bootstrap = await service.ensure_self_profile('test-user')
    scope = ProfileScope(user_id='test-user', subject_id=bootstrap.subject.subject_id)
    flow = fixture(scope=scope, lifecycle_epoch=bootstrap.subject.lifecycle_epoch, **fixture_options)
    flow.now, flow.collections, flow.repository, flow.service = now, collections, repository, service
    flow.public_reader = flow.reader
    flow.inbox = ProfileSourceInbox(
        MongoProfilePublicationStore(collections['publications']),
        repository=repository,
        clock=lambda: now[0],
    )
    flow.inbox.current_document_source = flow.resolver
    flow.applier = ProfileSourceApplier(flow.inbox, resolvers=flow.resolver.registrations())
    flow.worker = ProfilePublicationWorker(flow.inbox, flow.applier)
    flow.profile_reader = ProfileSnapshotReader(repository, publications=flow.inbox, clock=lambda: now[0])
    return flow


@pytest.mark.asyncio
async def test_real_profile_fixture_has_explicit_basic_store_and_pure_unadmitted_materials():
    flow = await setup_document_current()
    assert flow.repository.basic_info is not None
    assert flow.raw['scope'] == flow.scope.model_dump(by_alias=True)
    subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
    assert flow.raw['payload']['coverage']['lifecycleEpoch'] == subject.lifecycle_epoch
    assert not flow.collections['publications'].documents
    material = await resolve(flow, lifecycle_epoch=subject.lifecycle_epoch)
    assert material.basic_info_available and material.clinical_available
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_current_clinical_only_input_can_omit_basic_information():
    flow = fixture(with_basic=False)
    del flow.raw['payload']['basicInfo']
    rebuild(flow)
    material = await resolve(flow)
    assert not material.basic_info_available and material.basic_info == ()
    assert material.clinical_available and material.clinical_inventory_digest


@pytest.mark.asyncio
async def test_producer_revisions_are_opaque_and_do_not_certify_analysis_or_renderer_versions():
    flow = fixture(with_clinical=False)
    field = flow.raw['payload']['contributions'][0]['basicInfo'][0]
    field['interpretationRevision'] = 'test-extractor-independent-from-renderer'
    flow.raw['payload']['basicInfo'] = [deepcopy(field)]
    flow.inventory['payload']['contributions'] = deepcopy(flow.raw['payload']['contributions'])
    rebuild(flow)
    material = await resolve(flow)
    assert material.basic_info[0]['interpretationRevision'] == 'test-extractor-independent-from-renderer'
    assert (
        flow.raw['payload']['contributions'][0]['implementationSelection']['implementationRevision']
        == 'test-document-renderer'
    )


@pytest.mark.asyncio
async def test_same_understanding_reference_cannot_claim_different_extractor_binding():
    flow = fixture(with_clinical=False)
    field = basic('age', 42)
    field['interpretationRevision'] = 'test-other-extractor'
    ref, _ = contribution('basic_info', field=field)
    flow.raw['payload']['contributions'].append(ref)
    flow.raw['payload']['basicInfo'].extend(deepcopy(ref['basicInfo']))
    flow.inventory['payload']['contributions'] = deepcopy(flow.raw['payload']['contributions'])
    flow.raw['payload']['coverage']['orderedContributionIds'].append(ref['contributionId'])
    rebuild(flow)
    with pytest.raises(ProfileSourceApplicationError, match='basic_info_binding_mismatch'):
        await resolve(flow)


@pytest.mark.asyncio
async def test_public_current_port_and_factory_use_one_resolver_for_intake_and_recovery(monkeypatch):
    from health_profile import application
    from modules.health_profile import ProfileCurrentDocumentSourceResolver as PublicResolver
    from modules.health_profile.contracts import DOCUMENT_SOURCE_CONTRACT as public_contract
    from modules.health_profile.contracts import DocumentCompleteSource

    flow = await setup_document_current()
    assert PublicResolver is type(flow.resolver)
    assert DocumentCompleteSource.model_validate(flow.raw).contract_version == public_contract
    monkeypatch.setattr(application, 'collection_health_profile_source_publications', flow.collections['publications'])
    store = application.MongoHealthReportStore(type(flow.collections['facts'])(), type(flow.collections['facts'])())
    public = application._build_profile_public_contracts(
        flow.repository, store, resolvers=flow.resolver.registrations()
    )
    assert public.inbox.current_document_source is flow.resolver
    assert public.inbox.document_source is None
    await public.inbox.receive(flow.event, scope=flow.scope)
    assert await public.worker.recover_pending() == 1
    assert (await public.inbox.get_receipt(flow.event.event_id, scope=flow.scope)).status == 'applied'
