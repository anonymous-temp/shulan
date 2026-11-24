"""V2 clinical materialization is fenced by the original lifecycle before visibility."""

import asyncio
from datetime import timedelta

import pytest

from health_profile.basic_info import field_for_fact
from health_profile.contracts import ProfileFactV1
from health_profile.models import FactStatus
from health_profile.service import HealthProfileConflictError
from health_profile.service import HealthProfileService
from health_profile.snapshots import ProfileSnapshotReader
from health_profile.source_application import ProfilePublicationWorker
from health_profile.source_application import ProfileSourceApplier
from health_profile.source_publications import publication_id
from tests.unit.health_profile.test_basic_info import legacy_fact
from tests.unit.health_profile.test_document_basic_info_application import apply
from tests.unit.health_profile.test_document_basic_info_application import cycle
from tests.unit.health_profile.test_document_basic_info_application import facts
from tests.unit.health_profile.test_document_source_v2_resolver import setup_document_current


async def assert_hidden(flow, fact_id):
    service = HealthProfileService(flow.repository, clock=lambda: flow.now[0])
    projected = await service.get_profile(flow.scope.user_id, subject_id=flow.scope.subject_id)
    assert all(item['factId'] != fact_id for group in projected['factGroups'] for item in group['items'])
    listed = await service.list_facts(flow.scope.user_id, subject_id=flow.scope.subject_id, limit=1)
    assert all(row['factId'] != fact_id for row in listed['items'])
    reader = ProfileSnapshotReader(flow.repository, clock=lambda: flow.now[0])
    assert fact_id not in {row.fact_id for row in (await reader.read(flow.scope.user_id)).facts}
    assert fact_id not in {row.fact_id for row in (await reader.read_v2(flow.scope.user_id)).facts}
    assert fact_id not in {row.fact_id for row in (await reader.read_page(flow.scope.user_id)).facts}


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before_insert', 'after_insert', 'crash_after_insert'])
async def test_v2_clinical_epoch_changes_inside_materialization_never_leak(phase, monkeypatch):
    flow = await setup_document_current(with_basic=False)
    create, changed = flow.repository.create_fact, False

    async def race(fact):
        nonlocal changed
        if changed or field_for_fact(fact) is not None:
            return await create(fact)
        changed = True
        if phase == 'before_insert':
            await cycle(flow)
        result = await create(fact)
        if phase != 'before_insert':
            await cycle(flow)
        if phase == 'crash_after_insert':
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(flow.repository, 'create_fact', race)
    if phase == 'crash_after_insert':
        with pytest.raises(asyncio.CancelledError):
            await apply(flow)
    else:
        receipt = await apply(flow)
        assert receipt.status == 'failed' and receipt.error_code == 'source.document_lifecycle_mismatch'
    physical = await facts(flow)
    assert len(physical) == 1 and physical[0].basic_info_lifecycle_epoch == 1
    # Physical evidence remains for governance; visibility never relies on a compensating write.
    assert physical[0].status is FactStatus.ACTIVE
    await assert_hidden(flow, physical[0].fact_id)
    monkeypatch.setattr(flow.repository, 'create_fact', create)
    flow.now[0] += timedelta(minutes=5)
    restarted = ProfilePublicationWorker(
        flow.inbox, ProfileSourceApplier(flow.inbox, resolvers=flow.resolver.registrations())
    )
    assert await restarted.recover_pending() == 0
    await assert_hidden(flow, physical[0].fact_id)
    assert (await flow.repository.basic_info.read(flow.scope.user_id)).fields['name'].value is None


@pytest.mark.asyncio
async def test_v2_plan_fixes_clinical_epoch_before_first_physical_write(monkeypatch):
    flow = await setup_document_current(with_basic=False)

    async def stop_before_write(fact):
        raise OSError('synthetic interruption before materialization')

    monkeypatch.setattr(flow.repository, 'create_fact', stop_before_write)
    await apply(flow)
    stored = await flow.inbox.store.get(publication_id(flow.event))
    assert stored['plan']['lifecycleEpoch'] == 1
    assert stored['plan']['creates'][0]['basic_info_lifecycle_epoch'] == 1
    assert not await facts(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize('tag', [None, 99])
async def test_existing_fixed_v2_plan_adopts_only_its_original_epoch(tag, monkeypatch):
    flow = await setup_document_current(with_basic=False)
    create = flow.repository.create_fact

    async def stop_before_write(fact):
        raise OSError('synthetic interruption before materialization')

    monkeypatch.setattr(flow.repository, 'create_fact', stop_before_write)
    await apply(flow)

    def historical_plan(row):
        row['plan']['creates'][0]['basic_info_lifecycle_epoch'] = tag

    await flow.inbox._update(publication_id(flow.event), historical_plan)
    monkeypatch.setattr(flow.repository, 'create_fact', create)
    flow.now[0] += timedelta(minutes=5)
    restarted = ProfilePublicationWorker(
        flow.inbox, ProfileSourceApplier(flow.inbox, resolvers=flow.resolver.registrations())
    )
    applied = await restarted.recover_pending()
    if tag is None:
        assert applied == 1
        assert (await facts(flow))[0].basic_info_lifecycle_epoch == 1
        # Do not rewrite the fixed plan or acquire the then-current subject epoch.
        stored = await flow.inbox.store.get(publication_id(flow.event))
        assert stored['plan']['creates'][0]['basic_info_lifecycle_epoch'] is None
    else:
        assert applied == 0 and not await facts(flow)
        receipt = await flow.inbox.get_receipt(flow.event.event_id, scope=flow.scope)
        assert receipt.status == 'failed' and receipt.error_code == 'source.document_fact_lifecycle_mismatch'


@pytest.mark.asyncio
async def test_zero_basic_revision_still_filters_old_clinical_and_birth_rows():
    flow = await setup_document_current(with_basic=False)
    await apply(flow)
    old = (await facts(flow))[0]
    await cycle(flow)
    legacy = old.model_copy(
        update={
            'fact_id': 'test-v1-clinical',
            'creation_request_id': 'test-v1-clinical',
            'basic_info_lifecycle_epoch': None,
        }
    )
    current = old.model_copy(
        update={
            'fact_id': 'test-current-clinical',
            'creation_request_id': 'test-current-clinical',
            'basic_info_lifecycle_epoch': 3,
        }
    )
    await flow.repository.create_fact(legacy)
    await flow.repository.create_fact(current)
    birth = await legacy_fact(
        flow.repository,
        flow.scope.subject_id,
        fact_id='test-old-birth',
        field='dob',
        value='1980-01-01',
        confirmed=True,
        at=flow.now[0],
    )
    await flow.repository.replace_fact(
        birth.model_copy(update={'basic_info_lifecycle_epoch': 1}),
        expected_version=birth.version,
        allowed_statuses=(birth.status,),
    )
    basic = flow.repository.basic_info
    snapshot = await basic.read(flow.scope.user_id)
    assert snapshot.fields['age'].value is None
    zero = snapshot.model_copy(update={'revision': 0})
    selected = await basic.select_facts(flow.scope.user_id, flow.scope.subject_id, await facts(flow), snapshot=zero)
    assert {row.fact_id for row in selected} == {legacy.fact_id, current.fact_id}


@pytest.mark.asyncio
async def test_current_clinical_pages_refill_legacy_v1_survives_and_fixed_snapshots_do_not_change():
    from health_profile.memory import RepositoryHealthProfileChatMemoryProvider

    flow = await setup_document_current(with_basic=False)
    await apply(flow)
    old = (await facts(flow))[0]
    frozen = await flow.profile_reader.read(flow.scope.user_id)
    frozen_json = frozen.model_dump_json()
    assert old.fact_id in {row.fact_id for row in frozen.facts}
    await cycle(flow)
    current = old.model_copy(
        update={
            'fact_id': 'test-current-clinical',
            'creation_request_id': 'test-current-clinical',
            'basic_info_lifecycle_epoch': 3,
            'display_name': 'test-current-label',
        }
    )
    legacy = old.model_copy(
        update={
            'fact_id': 'test-v1-clinical',
            'creation_request_id': 'test-v1-clinical',
            'basic_info_lifecycle_epoch': None,
            'display_name': 'test-legacy-label',
        }
    )
    await flow.repository.create_fact(current)
    await flow.repository.create_fact(legacy)
    await flow.repository.basic_info.publish_governance(flow.scope.user_id, flow.scope.subject_id)
    reader = ProfileSnapshotReader(flow.repository, clock=lambda: flow.now[0])
    page = await reader.read_page(flow.scope.user_id, page_size=1)
    assert page.status == 'ready' and len(page.facts) == 1 and page.next_cursor
    second = await reader.read_page(flow.scope.user_id, page_size=1, cursor=page.next_cursor)
    assert second.status == 'ready' and len(second.facts) == 1 and not second.next_cursor
    assert {page.facts[0].fact_id, second.facts[0].fact_id} == {current.fact_id, legacy.fact_id}
    assert frozen.model_dump_json() == frozen_json
    assert 'basic_info_lifecycle_epoch' not in ProfileFactV1.model_fields
    memory = RepositoryHealthProfileChatMemoryProvider(flow.repository)
    rendered = await memory.build_context(flow.scope.user_id)
    assert rendered is not None and old.display_name not in rendered
    assert 'test-current-label' in rendered or 'test-legacy-label' in rendered
    await assert_hidden(flow, old.fact_id)


@pytest.mark.asyncio
async def test_zero_revision_service_fact_cursor_cannot_continue_in_another_lifecycle():
    flow = await setup_document_current(with_basic=False)
    await apply(flow)
    old = (await facts(flow))[0]
    await flow.repository.create_fact(
        old.model_copy(
            update={
                'fact_id': 'test-extra-clinical',
                'creation_request_id': 'test-extra-clinical',
            }
        )
    )
    service = HealthProfileService(flow.repository, clock=lambda: flow.now[0])
    assert (await flow.repository.basic_info.read(flow.scope.user_id)).revision == 0
    first = await service.list_facts(flow.scope.user_id, subject_id=flow.scope.subject_id, limit=1)
    assert first['nextCursor']
    await cycle(flow)
    assert (await flow.repository.basic_info.read(flow.scope.user_id)).revision == 0
    with pytest.raises(HealthProfileConflictError):
        await service.list_facts(
            flow.scope.user_id, subject_id=flow.scope.subject_id, limit=1, cursor=first['nextCursor']
        )
