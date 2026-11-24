"""Real V2 inbox-to-profile application, using synthetic immutable Document reads."""

from copy import deepcopy
from datetime import timedelta

import pytest

from health_profile.basic_info_contracts import BasicInfoValuesV1
from health_profile.contracts import ProfileScope
from health_profile.contracts import profile_content_digest
from health_profile.models import FactStatus
from health_profile.models import SubjectStatus
from health_profile.source_publications import publication_id
from health_profile.source_publications import source_key
from tests.unit.health_profile.test_document_source_v2_resolver import basic
from tests.unit.health_profile.test_document_source_v2_resolver import contribution
from tests.unit.health_profile.test_document_source_v2_resolver import event_for
from tests.unit.health_profile.test_document_source_v2_resolver import fixture
from tests.unit.health_profile.test_document_source_v2_resolver import rebuild
from tests.unit.health_profile.test_document_source_v2_resolver import retirement
from tests.unit.health_profile.test_document_source_v2_resolver import setup_document_current


async def apply(flow, event=None):
    event = event or flow.event
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    return await flow.inbox.get_receipt(event.event_id, scope=flow.scope)


async def current(flow):
    return await flow.repository.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id)


async def facts(flow, *, statuses=(FactStatus.ACTIVE,)):
    return await flow.repository.list_facts(flow.scope.user_id, flow.scope.subject_id, statuses=statuses)


async def cycle(flow):
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        await flow.repository.update_subject(
            subject.model_copy(
                update={
                    'status': status,
                    'version': subject.version + 1,
                    'updated_at': flow.now[0],
                }
            ),
            expected_version=subject.version,
        )


@pytest.mark.asyncio
async def test_v2_basic_and_clinical_apply_through_the_same_durable_publication():
    flow = await setup_document_current()
    receipt = await apply(flow)
    assert receipt.status == 'applied'
    info = await current(flow)
    assert info.fields['name'].value == '测试受检者'
    assert info.fields['name'].source_reliability == 'medium'
    rows = await facts(flow)
    assert any(row.fact_type == 'demographic.name' for row in rows)
    assert any(row.fact_type != 'demographic.name' for row in rows)
    stored = await flow.inbox.store.get(publication_id(flow.event))
    assert stored['plan']['documentBasicInfo']['publicationRevision'] == 1
    assert stored['plan']['lifecycleEpoch'] == info.lifecycle_epoch
    assert stored['basicInfoApplication']['effect'] == 'upsert'
    assert stored['basicInfoApplication']['lifecycleEpoch'] == info.lifecycle_epoch
    assert not stored['extractionCalls']


@pytest.mark.asyncio
async def test_v2_basic_only_partial_material_does_not_create_clinical_facts():
    flow = await setup_document_current(with_clinical=False)
    assert (await apply(flow)).status == 'applied'
    assert (await current(flow)).fields['name'].value == '测试受检者'
    assert all(row.fact_type == 'demographic.name' for row in await facts(flow))
    stored = await flow.inbox.store.get(publication_id(flow.event))
    assert not stored['plan']['creates'] and not stored['plan']['withdraws']


@pytest.mark.asyncio
async def test_v2_report_keeps_higher_api_confirmation_and_records_report_candidate():
    flow = await setup_document_current()
    basic_service = flow.repository.basic_info
    command = await basic_service.register(
        flow.scope.user_id,
        'api-confirmation',
        values=BasicInfoValuesV1(name='接口确认姓名', age=42, gender='男'),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await basic_service.apply(flow.scope.user_id, command.command_id)
    assert (await apply(flow)).status == 'applied'
    info = await current(flow)
    assert info.fields['name'].value == '接口确认姓名'
    assert info.fields['name'].source_reliability == 'very_high'
    report = [row for row in info.sources if row.value == '测试受检者']
    assert len(report) == 1 and report[0].active and not report[0].current


@pytest.mark.asyncio
async def test_v2_basic_change_preserves_unchanged_clinical_fact_identity_and_status():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    clinical = next(row for row in await facts(flow) if row.fact_type != 'demographic.name')
    original = clinical.model_dump_json()
    age_ref, _ = contribution('basic_info', field=basic('age', 42), scope=flow.scope)
    flow.raw['payload']['contributions'].append(age_ref)
    flow.inventory['payload']['contributions'].append(deepcopy(age_ref))
    flow.raw['payload']['basicInfo'].extend(age_ref['basicInfo'])
    flow.raw['payload']['coverage']['orderedContributionIds'].append(age_ref['contributionId'])
    event = rebuild(flow, revision=2)
    assert (await apply(flow, event)).status == 'applied'
    preserved = await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, clinical.fact_id)
    assert preserved.model_dump_json() == original
    assert (await current(flow)).fields['age'].value == 42
    stored = await flow.inbox.store.get(publication_id(event))
    assert not stored['plan']['creates'] and not stored['plan']['withdraws']
    assert stored['plan']['activeFactIds'] == [clinical.fact_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_retirement', [False, True])
async def test_v2_empty_basic_fields_require_retirement_to_clear_current(explicit_retirement):
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    clinical = next(row for row in await facts(flow) if row.fact_type != 'demographic.name')
    old_basic = next(row for row in flow.raw['payload']['contributions'] if row['kind'] == 'basic_info')
    flow.raw['payload']['basicInfo'] = []
    flow.raw['payload']['contributions'] = [
        row for row in flow.raw['payload']['contributions'] if row['kind'] != 'basic_info'
    ]
    flow.inventory['payload']['contributions'] = deepcopy(flow.raw['payload']['contributions'])
    flow.raw['payload']['coverage']['orderedContributionIds'] = [
        row['contributionId'] for row in flow.raw['payload']['contributions']
    ]
    if explicit_retirement:
        flow.inventory['payload']['retiredContributions'] = [retirement(old_basic)]
    event = rebuild(flow, revision=2)
    assert (await apply(flow, event)).status == 'applied'
    assert (await current(flow)).fields['name'].value == (None if explicit_retirement else '测试受检者')
    preserved = await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, clinical.fact_id)
    assert preserved.status is FactStatus.ACTIVE and preserved.version == clinical.version


def basic_age_contribution(flow, contribution_id, grade, *, value=42):
    field = {**basic('age', value), 'sourceReliability': grade}
    return contribution('basic_info', field=field, scope=flow.scope, contribution_id=contribution_id)[0]


def basic_inventory(flow, *, active, retired=(), revision):
    payload = flow.raw['payload']
    refs = [row for row in payload['contributions'] if row['kind'] == 'clinical'] + list(active)
    payload['contributions'] = deepcopy(refs)
    payload['basicInfo'] = [field for row in refs for field in row['basicInfo']]
    payload['coverage']['orderedContributionIds'] = [row['contributionId'] for row in refs]
    flow.inventory['payload']['contributions'] = deepcopy(refs)
    flow.inventory['payload']['retiredContributions'] = deepcopy(list(retired))
    return rebuild(flow, revision=revision)


async def age_inventory():
    flow = await setup_document_current()
    medium = basic_age_contribution(flow, 'age-medium', 'medium')
    low = basic_age_contribution(flow, 'age-low', 'low')
    name = next(row for row in flow.raw['payload']['contributions'] if row['kind'] == 'basic_info')
    first = basic_inventory(flow, active=[name, medium], revision=1)
    assert (await apply(flow, first)).status == 'applied'
    second = basic_inventory(flow, active=[name, medium, low], revision=2)
    assert (await apply(flow, second)).status == 'applied'
    return flow, name, medium, low


@pytest.mark.asyncio
async def test_explicit_basic_retirement_reselects_carried_low_and_allows_next_correction(monkeypatch):
    flow, name, medium, low = await age_inventory()
    before = await current(flow)
    assert before.fields['age'].source_ref['contributionId'] == 'age-medium'
    clinical = next(row for row in await facts(flow) if not row.fact_type.startswith('demographic.'))
    retired = retirement(medium)
    event = basic_inventory(flow, active=[name, low], retired=[retired], revision=3)
    service = flow.repository.basic_info
    ingest = service.ingest_report

    async def lose_ack(*args, **kwargs):
        await ingest(*args, **kwargs)
        raise OSError('synthetic explicit retirement acknowledgement loss')

    with monkeypatch.context() as lost:
        lost.setattr(service, 'ingest_report', lose_ack)
        assert (await apply(flow, event)).status == 'retry_wait'
    interim = await current(flow)
    assert interim.fields['age'].source_ref['contributionId'] == 'age-low'
    assert interim.fields['age'].source_reliability == 'low'
    assert interim.fields['age'].value == 42
    assert interim.fields['name'] == before.fields['name']
    assert interim.fields['gender'] == before.fields['gender']
    low_before = next(row for row in before.sources if row.source_ref.get('contributionId') == 'age-low')
    assert interim.fields['age'].fact_id == low_before.fact_id

    flow.public_reader.read_source_observations.side_effect = AssertionError('must reuse the saved retirement plan')
    flow.now[0] += timedelta(minutes=5)
    assert await flow.worker.recover_pending() == 1
    receipt = await flow.inbox.receive(event, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.publication_revision == 3
    stored = await flow.inbox.store.get(publication_id(event))
    (proof,) = stored['plan']['documentBasicInfo']['retiredBasicContributions']
    assert proof['field'] == 'age' and proof['contributionId'] == 'age-medium'
    assert proof['retirementEvidenceRef'] == retired['retirementEvidenceRef']
    cursor_id = 'cursor_' + source_key(flow.scope, event.source_type, event.source_id)
    cursor = await flow.inbox.store.get(cursor_id)
    assert cursor['appliedPublicationRevision'] == cursor['settledPublicationRevision'] == 3
    assert (await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, clinical.fact_id)) == clinical

    flow.public_reader.read_source_observations.side_effect = lambda scope, snapshot_id: deepcopy(
        flow.materials[snapshot_id]
    )
    higher = basic_age_contribution(flow, 'age-new', 'medium', value=43)
    fourth = basic_inventory(flow, active=[name, low, higher], retired=[retired], revision=4)
    assert (await apply(flow, fourth)).status == 'applied'
    after = await current(flow)
    assert after.fields['age'].source_ref['contributionId'] == 'age-new'
    assert after.fields['age'].value == 43
    assert not next(row for row in after.sources if row.source_ref.get('contributionId') == 'age-medium').active
    assert (await flow.inbox.receive(event, scope=flow.scope)).status == 'applied'
    assert (await current(flow)).fields['age'] == after.fields['age']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_proof', 'mixed_change', 'mismatched_raw_identity'])
async def test_basic_retirement_does_not_bypass_inventory_or_identity_checks(invalid):
    flow, name, medium, low = await age_inventory()
    active = [name, low]
    proof = retirement(medium)
    if invalid == 'mixed_change':
        active.append(basic_age_contribution(flow, 'age-weak-new', 'low', value=41))
    elif invalid == 'mismatched_raw_identity':
        proof['basicInfo'] = [{**proof['basicInfo'][0], 'value': 42.0}]
    event = basic_inventory(flow, active=active, retired=[] if invalid == 'missing_proof' else [proof], revision=3)
    before = (await current(flow)).fields['age']
    receipt = await apply(flow, event)
    assert receipt.status == 'failed'
    assert (await current(flow)).fields['age'] == before
    cursor = await flow.inbox.store.get('cursor_' + source_key(flow.scope, event.source_type, event.source_id))
    assert cursor['appliedPublicationRevision'] == cursor['settledPublicationRevision'] == 2


@pytest.mark.asyncio
async def test_v2_duplicate_ack_replays_without_a_live_document_reader():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    frozen = (await current(flow)).model_dump_json()
    flow.inbox.current_document_source = None
    flow.public_reader.read_source_observations.side_effect = AssertionError('duplicate must use its frozen receipt')
    receipt = await flow.inbox.receive(flow.event, scope=flow.scope)
    assert receipt.status == 'applied'
    assert await flow.worker.recover_pending() == 0
    assert (await current(flow)).model_dump_json() == frozen


@pytest.mark.asyncio
async def test_v2_lost_basic_commit_ack_recovers_original_plan_without_overriding_later_api(monkeypatch):
    flow = await setup_document_current()
    basic_service = flow.repository.basic_info
    ingest = basic_service.ingest_report
    calls = 0

    async def lose_ack(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await ingest(*args, **kwargs)
        if calls == 1:
            raise OSError('synthetic basic commit acknowledgement loss')
        return result

    monkeypatch.setattr(basic_service, 'ingest_report', lose_ack)
    assert (await apply(flow)).status == 'retry_wait'
    command = await basic_service.register(
        flow.scope.user_id,
        'later-api',
        values=BasicInfoValuesV1(name='后来确认', age=42, gender='男'),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await basic_service.apply(flow.scope.user_id, command.command_id)
    flow.now[0] += timedelta(minutes=5)
    assert await flow.worker.recover_pending() == 1
    assert (await current(flow)).fields['name'].value == '后来确认'
    assert (
        len(
            [
                row
                for row in await facts(flow)
                if row.fact_type not in {'demographic.name', 'demographic.age', 'demographic.sex'}
            ]
        )
        == 1
    )
    stored = await flow.inbox.store.get(publication_id(flow.event))
    assert stored['plan']['lifecycleEpoch'] == 1 and stored['basicInfoApplication']['effect'] == 'upsert'
    assert calls == 2


@pytest.mark.asyncio
async def test_v2_old_accepted_publication_cannot_bind_reactivated_subject():
    flow = await setup_document_current()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    await cycle(flow)
    assert await flow.worker.recover_pending() == 0
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.document_lifecycle_mismatch'
    assert not await facts(flow)
    assert (await current(flow)).fields['name'].value is None


@pytest.mark.asyncio
@pytest.mark.parametrize('with_basic', [False, True])
async def test_v2_epoch_change_between_independent_stages_stops_clinical_writes(monkeypatch, with_basic):
    flow = await setup_document_current(with_basic=with_basic)
    original = flow.applier._apply_document_basic_info

    async def change_epoch(*args):
        result = await original(*args)
        await cycle(flow)
        return result

    monkeypatch.setattr(flow.applier, '_apply_document_basic_info', change_epoch)
    receipt = await apply(flow)
    assert receipt.status == 'failed' and receipt.error_code == 'source.document_lifecycle_mismatch'
    assert not any(row.fact_type != 'demographic.name' for row in await facts(flow))
    assert (await current(flow)).fields['name'].value is None


@pytest.mark.asyncio
async def test_v2_retraction_removes_report_basic_and_clinical_with_original_epoch():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    body = {
        'scope': flow.scope.model_dump(by_alias=True),
        'contractVersion': 'document.retraction.v1',
        'sourceType': 'health_report',
        'sourceId': flow.event.source_id,
        'sourceRevision': 1,
        'publicationRevision': 2,
        'priorRevision': 1,
        'eventKind': 'retract',
        'lifecycleEpoch': 1,
    }
    event = event_for(flow, body, revision=2, retraction=True)
    assert (await apply(flow, event)).status == 'applied'
    assert not await facts(flow)
    assert (await current(flow)).fields['name'].value is None
    stored = await flow.inbox.store.get(publication_id(event))
    assert stored['basicInfoApplication']['effect'] == 'withdraw'
    assert stored['lifecycleEpoch'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted_kind', ['publication', 'cursor'])
async def test_v2_staged_old_epoch_becomes_terminal_and_does_not_starve_valid_intake(monkeypatch, interrupted_kind):
    flow = await setup_document_current()
    insert = flow.inbox.store.insert

    async def interrupt(document):
        if document['recordKind'] == interrupted_kind:
            raise OSError('synthetic interruption during intake')
        return await insert(document)

    monkeypatch.setattr(flow.inbox.store, 'insert', interrupt)
    with pytest.raises(OSError):
        await flow.inbox.receive(flow.event, scope=flow.scope)
    monkeypatch.setattr(flow.inbox.store, 'insert', insert)
    old_alias_id = flow.inbox.alias_id(flow.scope, flow.event.event_id)
    original = await flow.inbox.store.get(old_alias_id)
    await cycle(flow)
    flow.now[0] += timedelta(seconds=1)
    other = (await flow.service.ensure_self_profile('test-user-2')).subject
    scope = ProfileScope(user_id=other.user_id, subject_id=other.subject_id)
    pending = fixture(scope=scope, lifecycle_epoch=other.lifecycle_epoch)
    flow.materials.update(pending.materials)
    flow.inventories.update(pending.inventories)
    monkeypatch.setattr(flow.inbox.store, 'insert', interrupt)
    with pytest.raises(OSError):
        await flow.inbox.receive(pending.event, scope=scope)
    monkeypatch.setattr(flow.inbox.store, 'insert', insert)
    assert await flow.inbox.repair_staged(limit=1) == 1
    saved = await flow.inbox.store.get(old_alias_id)
    assert saved['status'] == 'failed'
    assert saved['lifecycleEpoch'] == original['lifecycleEpoch'] == 1
    assert saved['processingSelection'] == original['processingSelection']
    assert saved['fingerprint'] == original['fingerprint']
    receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.document_lifecycle_mismatch'
    assert await flow.inbox.repair_staged(limit=1) == 1
    good = await flow.inbox.store.get(publication_id(pending.event))
    assert good is not None and good['lifecycleEpoch'] == other.lifecycle_epoch
    assert not await flow.inbox.store.scan({'recordKind': 'alias', 'status': 'staged'}, 10)
    assert not flow.collections['facts'].documents
    assert await flow.inbox.repair_staged(limit=1) == 0


@pytest.mark.asyncio
async def test_current_basic_only_material_preserves_the_previous_complete_clinical_inventory():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    clinical = next(row for row in await facts(flow) if row.fact_type != 'demographic.name')
    cursor = await flow.inbox.store.get('cursor_' + source_key(flow.scope, 'health_report', flow.event.source_id))
    digest = cursor['clinicalInventoryDigest']
    previous = fixture(scope=flow.scope, lifecycle_epoch=1, with_clinical=False)
    flow.materials.update(previous.materials)
    flow.inventories.update(previous.inventories)
    event = event_for(flow, previous.raw, revision=2)
    assert (await apply(flow, event)).status == 'applied'
    preserved = await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, clinical.fact_id)
    assert preserved.status is FactStatus.ACTIVE and preserved.version == clinical.version
    cursor = await flow.inbox.store.get('cursor_' + source_key(flow.scope, 'health_report', flow.event.source_id))
    assert cursor['clinicalInventoryDigest'] == digest


@pytest.mark.asyncio
async def test_current_explicit_clinical_retirement_clears_only_its_clinical_inventory():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    before = (await current(flow)).fields['name'].value
    clinical = next(row for row in flow.raw['payload']['contributions'] if row['kind'] == 'clinical')
    next_flow = fixture(scope=flow.scope, lifecycle_epoch=1, with_clinical=False, retired=(retirement(clinical),))
    flow.materials.update(next_flow.materials)
    flow.inventories.update(next_flow.inventories)
    event = event_for(flow, next_flow.raw, revision=2)
    assert (await apply(flow, event)).status == 'applied'
    assert (await current(flow)).fields['name'].value == before
    assert all(row.fact_type == 'demographic.name' for row in await facts(flow))


@pytest.mark.asyncio
async def test_fixed_clinical_content_digest_change_is_not_suppressed_when_observations_are_equal():
    flow = await setup_document_current()
    assert (await apply(flow)).status == 'applied'
    clinical = next(row for row in await facts(flow) if row.fact_type != 'demographic.name')
    row = flow.raw['payload']['clinicalContributions'][0]
    row['clinicalContentDigest'] = '9' * 64
    payload = {
        key: value
        for key, value in row.items()
        if key not in {'contractVersion', 'sourceRevision', 'snapshotId', 'contentDigest'}
    }
    digest = profile_content_digest(
        {
            'scope': flow.raw['scope'],
            'sourceType': 'health_report',
            'sourceId': flow.event.source_id,
            'sourceRevision': 1,
            'contractVersion': row['contractVersion'],
            'payload': payload,
        }
    )
    row.update(contentDigest=digest, snapshotId='document_snapshot_' + digest)
    flow.raw['payload']['contributions'][0] = {
        key: value for key, value in row.items() if key not in {'reportType', 'reportTime', 'observations'}
    }
    flow.inventory['payload']['contributions'] = deepcopy(flow.raw['payload']['contributions'])
    event = rebuild(flow, revision=2)
    assert (await apply(flow, event)).status == 'applied'
    old = await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, clinical.fact_id)
    assert old.status is FactStatus.RETRACTED
    assert any(item.fact_type != 'demographic.name' and item.fact_id != clinical.fact_id for item in await facts(flow))
