"""One current source flow rejects drift before creating any application effects."""

from copy import deepcopy

import pytest

from health_profile.contracts import ProfileSourceCutoverAuthorizationV1
from health_profile.contracts import profile_content_digest
from health_profile.source_admission_contracts import ProfileFreshSourceAuthorizationV1
from health_profile.source_material import ProfileSourceApplicationError
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import publication_id
from tests.unit.health_profile.test_document_basic_info_application import apply
from tests.unit.health_profile.test_document_basic_info_application import current
from tests.unit.health_profile.test_document_basic_info_application import facts
from tests.unit.health_profile.test_document_source_contract import setup_document
from tests.unit.health_profile.test_document_source_v2_resolver import fixture
from tests.unit.health_profile.test_document_source_v2_resolver import rebuild
from tests.unit.health_profile.test_document_source_v2_resolver import setup_document_current
from tests.unit.health_profile.test_source_admission import fresh_document_setup


def install(flow, revision, *, publication=1, value='当前报告姓名', grade='low', clinical=False):
    incoming = fixture(scope=flow.scope, lifecycle_epoch=1, with_clinical=clinical)
    raw, inventory = incoming.raw, incoming.inventory
    for container in (raw, inventory, raw['payload']['coverage']):
        container['sourceRevision'] = revision
    clinical_rows = {row['contributionId']: row for row in raw['payload']['clinicalContributions']}
    for ref in raw['payload']['contributions']:
        row = clinical_rows.get(ref['contributionId'])
        if row is None:
            field = ref['basicInfo'][0]
            field.update(value=value, sourceReliability=grade)
            field['evidence'] = {'quote': value, 'start': 0, 'end': len(value)}
            identity = f'basic-name-{revision}-{grade}'
            ref['contributionId'] = field['contributionId'] = identity
            payload = {
                key: val
                for key, val in ref.items()
                if key not in {'contractVersion', 'sourceRevision', 'snapshotId', 'contentDigest'}
            }
            payload.update(reportType='lab', reportTime='2026-11-01', observations=[])
        else:
            payload = {
                key: val
                for key, val in row.items()
                if key not in {'contractVersion', 'sourceRevision', 'snapshotId', 'contentDigest'}
            }
        digest = profile_content_digest(
            {
                'scope': raw['scope'],
                'sourceType': 'health_report',
                'sourceId': raw['sourceId'],
                'sourceRevision': revision,
                'contractVersion': ref['contractVersion'],
                'payload': payload,
            }
        )
        binding = {'sourceRevision': revision, 'snapshotId': 'document_snapshot_' + digest, 'contentDigest': digest}
        ref.update(binding)
        if row is not None:
            row.update(binding)
    raw['payload']['basicInfo'] = [
        field for row in raw['payload']['contributions'] for field in row.get('basicInfo', [])
    ]
    raw['payload']['coverage']['orderedContributionIds'] = [
        row['contributionId'] for row in raw['payload']['contributions']
    ]
    inventory['payload']['contributions'] = deepcopy(raw['payload']['contributions'])
    raw['payload']['materialSnapshotRefs'][0]['sourceRevision'] = revision
    rebuild(incoming, revision=publication)
    event = incoming.event.model_copy(update={'source_revision': revision})
    flow.raw, flow.inventory = raw, inventory
    flow.materials.update(incoming.materials)
    flow.inventories.update(incoming.inventories)
    flow.event = event
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize('clinical,saved_plan', [(False, False), (False, True), (True, True)])
async def test_regressed_material_never_executes_basic_action_even_with_a_fixed_plan(clinical, saved_plan):
    flow = await setup_document_current(with_clinical=clinical)
    install(flow, 2, clinical=clinical)
    assert (await apply(flow)).status == 'applied'
    before = (await current(flow)).model_dump_json()
    physical = [row.model_dump_json() for row in await facts(flow)]
    view = await flow.repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    second = install(flow, 1, publication=2, value='旧报告姓名', grade='medium', clinical=clinical)
    await flow.inbox.receive(second, scope=flow.scope)
    if saved_plan:
        # A plan persisted by the previously bypassing path must not evade the
        # current apply guard; restoring it must not first touch basic fields.
        plan = {
            'creates': [],
            'withdraws': [],
            'activeFactIds': [],
            'reasonCodes': [],
            'subjectIds': [flow.scope.subject_id],
            'lifecycleEpoch': 1,
            'clinicalInventoryDigest': 'unchanged-clinical',
            'documentBasicInfo': {
                'sourceId': second.source_id,
                'sourceRevision': 1,
                'publicationRevision': 2,
                'lifecycleEpoch': 1,
                'fields': flow.raw['payload']['basicInfo'],
                'retract': False,
            },
        }
        await flow.inbox.save_plan(publication_id(second), plan)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(second.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.material_revision_regressed'
    assert (await current(flow)).model_dump_json() == before
    assert [row.model_dump_json() for row in await facts(flow)] == physical
    assert (
        await flow.repository.get_view(flow.scope.user_id, flow.scope.subject_id)
    ).profile_version == view.profile_version
    assert receipt.watermark.accepted_publication_revision == 2
    assert receipt.watermark.applied_publication_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [2, 3])
async def test_current_source_keeps_same_or_newer_material_revision_usable(revision):
    flow = await setup_document_current(with_clinical=False)
    install(flow, 2)
    assert (await apply(flow)).status == 'applied'
    install(flow, revision, publication=2, value='较可信当前姓名', grade='medium')
    assert (await apply(flow)).status == 'applied'
    assert (await current(flow)).fields['name'].value == '较可信当前姓名'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cutover', 'fresh'])
@pytest.mark.parametrize('declared,material', [(1, 2), (2, 1)])
async def test_declaration_mismatch_never_reserves_the_source_and_correct_retry_succeeds(kind, declared, material):
    if kind == 'cutover':
        flow = await setup_document(authorize=False)
        good, dto = flow.authorization, ProfileSourceCutoverAuthorizationV1

        async def authorize(value):
            return await flow.inbox.authorize_cutover(value, scope=flow.scope)
    else:
        flow = await fresh_document_setup()
        good, dto = flow.fresh_auth, ProfileFreshSourceAuthorizationV1

        async def authorize(value):
            return await flow.admission.authorize_fresh_source(value, scope=flow.scope)

    raw = good.model_dump(mode='json', by_alias=True)
    raw.update(
        materialContractVersion=f'document.source-observations.v{declared}',
        processingRevision=f'profile-document-source-v{declared}',
    )
    raw['materialSnapshotRef']['contractVersion'] = f'document.source-observations.v{material}'
    bad = dto.model_validate(raw)
    with pytest.raises((ProfileSourceConflictError, ProfileSourceApplicationError)):
        await authorize(bad)
    assert await flow.inbox.get_cutover(flow.scope, 'health_report', flow.event.source_id) is None
    assert not any(
        row['recordKind'] in {'alias', 'publication', 'cursor'} for row in flow.collections['publications'].documents
    )
    assert (await authorize(good)).status == 'ready'


@pytest.mark.asyncio
async def test_current_receiver_has_one_processing_algorithm_and_one_planner():
    flow = await setup_document_current()
    assert not hasattr(flow.resolver, 'for_processing')
    assert not hasattr(flow.applier, '_plan_document_v2')


@pytest.mark.asyncio
async def test_missing_required_predecessor_is_retryable_before_a_fixed_plan_or_any_materialization():
    flow = await setup_document_current(with_clinical=False)
    first = install(flow, 1)
    assert (await apply(flow)).status == 'applied'
    before = (await current(flow)).model_dump_json()
    second = install(flow, 2, publication=2, value='test-new-value', grade='medium')
    await flow.inbox.receive(second, scope=flow.scope)
    documents = flow.collections['publications'].documents
    documents[:] = [row for row in documents if row['_id'] != publication_id(first)]
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(second.event_id, scope=flow.scope)
    assert receipt.status == 'retry_wait' and receipt.error_code == 'source.snapshot_missing'
    saved = await flow.inbox.store.get(publication_id(second))
    assert saved['plan'] is None and saved.get('basicInfoApplication') is None
    assert (await current(flow)).model_dump_json() == before
