"""Actual Profile repository/store/service composition with synthetic Mongo I/O."""

import asyncio
from datetime import UTC
from datetime import datetime

import pytest
from pydantic import ValidationError

from health_profile import AssertionLevel
from health_profile import EffectivePeriod
from health_profile import FactCategory
from health_profile import FactSource
from health_profile import FactStatus
from health_profile import FactValueType
from health_profile import HealthFact
from health_profile import HealthProfileService
from health_profile import MongoHealthProfileRepository
from health_profile import VerificationStatus
from health_profile.basic_info import ProfileBasicInfoService
from health_profile.basic_info_contracts import BasicInfoEvidenceV1
from health_profile.basic_info_contracts import BasicInfoValuesV1
from health_profile.basic_info_store import BasicInfoConflict
from tests.unit._support.profile_mongo import BsonCollection as BsonCollection
from tests.unit._support.profile_mongo import BsonCursor as BsonCursor

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)


@pytest.fixture
def composition():
    collections = [BsonCollection() for _ in range(6)]
    subjects, facts, events, views, jobs, basic = collections
    repo = MongoHealthProfileRepository(
        subjects=subjects,
        facts=facts,
        events=events,
        views=views,
        jobs=jobs,
        basic_info=basic,
    )
    service = repo.basic_info
    service.clock = lambda: NOW
    return repo, service, HealthProfileService(repo, clock=lambda: NOW), collections


def values(**changes):
    return BasicInfoValuesV1(name='测试甲', age=42, gender='男').model_copy(update=changes)


def report(field, value, grade='low', observed_at=None):
    return {
        'field': field,
        'value': value,
        'sourceReliability': grade,
        'observedAt': observed_at,
        'reason': 'synthetic_evidence',
        'evidence': {'quote': f'{field}: {value}', 'start': 0, 'end': len(f'{field}: {value}')},
        'subjectStatus': 'single',
        'contributionId': f'synthetic_{field}_{value}_{grade}',
    }


async def legacy_fact(
    repo, subject_id, *, fact_id='legacy_age', field='age', value=42, confirmed=False, report_source=False, at=NOW
):
    fact = HealthFact(
        fact_id=fact_id,
        user_id='u',
        subject_id=subject_id,
        fact_type={'age': 'demographic.age', 'name': 'demographic.name', 'dob': 'demographic.birth_date'}[field],
        category=FactCategory.DEMOGRAPHIC,
        display_name=field,
        value_type=FactValueType.QUANTITY if field == 'age' else FactValueType.TEXT,
        value={'number': value, 'unit': 'years'} if field == 'age' else value,
        assertion_level=AssertionLevel.USER_CONFIRMED if confirmed else AssertionLevel.OBSERVED,
        verification_status=VerificationStatus.CONFIRMED if confirmed else VerificationStatus.VERIFIED_SOURCE,
        confidence=1,
        effective_period=EffectivePeriod(start=at),
        status=FactStatus.ACTIVE,
        sources=(
            FactSource(
                source_type='health_report' if report_source else 'legacy', source_id=fact_id, source_revision=1
            ),
        ),
        created_at=at,
        updated_at=at,
    )
    await repo.create_fact(fact)
    return fact


@pytest.mark.asyncio
async def test_register_pending_apply_and_public_projection(composition):
    repo, service, profile, collections = composition
    receipt = await service.register('u', 'op', values=values(), confirmed_at=NOW)
    assert receipt.status == 'registered'
    assert await repo.get_self_subject('u') is None
    assert (await service.get('u', 'op')).command_id == receipt.command_id
    assert (await service.list_pending()).items == (receipt,)
    applied = await service.apply('u', receipt.command_id)
    assert applied.status == 'applied'
    assert not (await service.list_pending()).items
    assert (await service.apply('u', receipt.command_id)).as_dict() == applied.as_dict()
    current = await service.read('u')
    assert {key: item.value for key, item in current.fields.items()} == {'name': '测试甲', 'age': 42, 'gender': 'male'}
    assert all(item.source_reliability == 'very_high' for item in current.fields.values())
    result = await profile.get_profile('u')
    assert result['overview']['age'] == 42
    assert result['overview']['name'] == '测试甲'
    assert result['basicInfo']['age']['ageBasis'] == 'confirmed_at_time'
    assert len(collections[1].documents) == 3


@pytest.mark.asyncio
async def test_same_operation_conflict_and_foreign_scope(composition):
    _, service, _, _ = composition
    first = await service.register('u', 'op', values=values(), confirmed_at=NOW)
    assert (await service.register('u', 'op', values=values(), confirmed_at=NOW)) == first
    with pytest.raises(BasicInfoConflict):
        await service.register('u', 'op', values=values(age=43), confirmed_at=NOW)
    assert await service.get('other', 'op') is None
    with pytest.raises(BasicInfoConflict):
        await service.apply('other', first.command_id)


@pytest.mark.asyncio
async def test_newer_no_change_supersedes_old_pending_and_no_duplicate_facts(composition):
    _, service, _, collections = composition
    first = await service.register('u', 'first', values=values(), confirmed_at=NOW)
    await service.apply('u', first.command_id)
    old = await service.register('u', 'old', values=values(age=43), confirmed_at=NOW)
    new = await service.register('u', 'new', values=values(), confirmed_at=NOW)
    assert (await service.apply('u', new.command_id)).status == 'no_change'
    assert (await service.apply('u', old.command_id)).status == 'superseded'
    assert (await service.read('u')).fields['age'].value == 42
    assert len(collections[1].documents) == 3


@pytest.mark.asyncio
async def test_ai_tiers_independent_fields_same_grade_and_withdrawal(composition):
    _, service, profile, _ = composition
    subject = (await profile.ensure_self_profile('u')).subject
    await service.ingest_report('u', subject.subject_id, source_id='a', source_revision=1, fields=[report('age', 40)])
    await service.ingest_report('u', subject.subject_id, source_id='b', source_revision=1, fields=[report('age', 41)])
    assert (await service.read('u')).fields['age'].value == 40
    await service.ingest_report(
        'u', subject.subject_id, source_id='c', source_revision=1, fields=[report('age', 42, 'medium')]
    )
    assert (await service.read('u')).fields['age'].value == 42
    await service.withdraw_report('u', subject.subject_id, source_id='c')
    assert (await service.read('u')).fields['age'].value == 40
    command = await service.register('u', 'api', values=values(age=39), confirmed_at=NOW)
    await service.apply('u', command.command_id)
    await service.ingest_report(
        'u', subject.subject_id, source_id='d', source_revision=1, fields=[report('age', 45, 'medium')]
    )
    assert (await service.read('u')).fields['age'].value == 39


@pytest.mark.asyncio
async def test_cancel_is_durable_and_restart_reuses_store(composition):
    from health_profile.basic_info import ProfileBasicInfoService

    repo, service, _, _ = composition
    command = await service.register('u', 'op', values=values(), confirmed_at=NOW)
    await service.cancel('u', command.command_id)
    restored = ProfileBasicInfoService(repo, service.store, clock=lambda: NOW)
    assert (await restored.apply('u', command.command_id)).status == 'cancelled'
    assert not (await restored.list_pending()).items


@pytest.mark.asyncio
async def test_concurrent_first_registration_binds_one_subject(composition):
    repo, service, _, _ = composition
    commands = await asyncio.gather(
        *(service.register('u', f'op{i}', values=values(age=40 + i), confirmed_at=NOW) for i in range(4))
    )
    assert len({item.subject_id for item in commands}) == 1
    await asyncio.gather(*(service.apply('u', item.command_id) for item in commands))
    assert (await service.read('u')).fields['age'].value == 43
    assert (await repo.get_self_subject('u')).subject_id == commands[-1].subject_id


@pytest.mark.parametrize('changes', [{'age': True}, {'age': 151}, {'age': '42'}, {'name': ' '}, {'gender': 'unknown'}])
def test_strict_api_values(changes):
    with pytest.raises(ValidationError):
        BasicInfoValuesV1.model_validate({'name': '测试甲', 'age': 42, 'gender': '男', **changes})


def test_report_cannot_claim_api_grade():
    with pytest.raises(ValidationError):
        BasicInfoEvidenceV1.model_validate(report('age', 42, 'very_high'))


@pytest.mark.asyncio
async def test_complete_inventory_carry_upgrade_retire_and_watermarks(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    low, medium = report('age', 40), report('age', 40, 'medium', '2026-11-01')
    await service.ingest_report('u', sid, source_id='a', source_revision=2, publication_revision=1, fields=[low])
    await service.ingest_report(
        'u', sid, source_id='b', source_revision=1, publication_revision=1, fields=[report('age', 41)]
    )
    await service.ingest_report(
        'u', sid, source_id='a', source_revision=2, publication_revision=2, fields=[low, medium]
    )
    current = await service.read('u')
    assert current.fields['age'].value == 40
    assert current.fields['age'].source_reliability == 'medium'
    assert current.fields['age'].observed_at == '2026-11-01'
    raw = await repo.get_fact('u', sid, current.fields['age'].fact_id)
    assert raw.sources[0].source_id == 'a' and raw.sources[0].source_revision == 2
    await service.withdraw_report(
        'u', sid, source_id='a', contribution_ids=[medium['contributionId']], publication_revision=3
    )
    assert (await service.read('u')).fields['age'].value == 40
    assert (await repo.get_fact('u', sid, raw.fact_id)).status is FactStatus.RETRACTED
    await service.withdraw_report('u', sid, source_id='a', publication_revision=4)
    assert (await service.read('u')).fields['age'].value == 41
    await service.ingest_report(
        'u', sid, source_id='a', source_revision=2, publication_revision=2, fields=[low, medium]
    )
    assert (await service.read('u')).fields['age'].value == 41
    with pytest.raises(BasicInfoConflict, match='publication_content_conflict'):
        await service.ingest_report('u', sid, source_id='a', source_revision=2, publication_revision=4, fields=[low])


@pytest.mark.asyncio
async def test_same_grade_cannot_replace_by_retiring_original_contribution(composition):
    _, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=[report('age', 40)])
    with pytest.raises(BasicInfoConflict, match='unqualified_source_replacement'):
        await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=[report('age', 41)])
    assert (await service.read('u')).fields['age'].value == 40


@pytest.mark.asyncio
async def test_same_value_confirmation_survives_report_withdrawal(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=[report('age', 42)])
    original = (await service.read('u')).fields['age'].fact_id
    command = await service.register('u', 'confirm', values=values(), confirmed_at=NOW)
    receipt = await service.apply('u', command.command_id)
    assert receipt.fields['age'].status == 'no_change'
    await service.withdraw_report('u', sid, source_id='a')
    assert (await service.read('u')).fields['age'].fact_id == original
    fact = await repo.get_fact('u', sid, original)
    assert fact.status is FactStatus.ACTIVE
    assert any(source.source_type == 'manual' for source in fact.sources)
    assert fact.verification_status is VerificationStatus.CONFIRMED


@pytest.mark.asyncio
async def test_partial_report_materialization_is_never_current_and_replay_recovers(composition, monkeypatch):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    create = repo.create_fact
    count = 0

    async def fail_second(fact):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('synthetic storage failure')
        return await create(fact)

    monkeypatch.setattr(repo, 'create_fact', fail_second)
    fields = [report('name', 'Synthetic'), report('age', 42)]
    with pytest.raises(OSError):
        await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=fields)
    assert (await service.read('u')).revision == 1
    assert (await profile.get_profile('u'))['overview']['name'] is None
    assert not (await profile.list_facts('u'))['items']
    from health_profile.snapshots import ProfileSnapshotReader

    assert not (await ProfileSnapshotReader(repo, clock=lambda: NOW).read('u')).facts
    monkeypatch.setattr(repo, 'create_fact', create)
    await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=fields)
    assert (await service.read('u')).fields['name'].value == 'Synthetic'


@pytest.mark.asyncio
async def test_registration_ack_loss_is_gettable_and_pending_pagination_recovers_all_users(composition, monkeypatch):
    repo, service, _, _ = composition
    cas = service.store.cas
    lost = False

    async def lose_ack(before, after):
        nonlocal lost
        result = await cas(before, after)
        if result and not lost and after.get('commands'):
            lost = True
            raise OSError('synthetic lost acknowledgement')
        return result

    monkeypatch.setattr(service.store, 'cas', lose_ack)
    with pytest.raises(OSError):
        await service.register('u', 'first', values=values(), confirmed_at=NOW)
    monkeypatch.setattr(service.store, 'cas', cas)
    recovered = await service.get('u', 'first')
    assert recovered.status == 'registered'
    for user, operation in [('u', 'second'), ('v', 'third'), ('w', 'fourth')]:
        await service.register(user, operation, values=values(), confirmed_at=NOW)
    restored = ProfileBasicInfoService(repo, service.store, clock=lambda: NOW)
    page, seen = await restored.list_pending(limit=1), []
    while True:
        seen.extend(item.operation_id for item in page.items)
        if page.next_cursor is None:
            break
        page = await restored.list_pending(limit=1, cursor=page.next_cursor)
    assert sorted(seen) == ['first', 'fourth', 'second', 'third']
    await restored.apply('u', recovered.command_id)
    assert (await restored.get('u', 'first')).status == 'superseded'


@pytest.mark.asyncio
async def test_partial_application_restart_repairs_fixed_command(composition, monkeypatch):
    repo, service, _, collections = composition
    command = await service.register('u', 'op', values=values(), confirmed_at=NOW)
    create = repo.create_fact
    calls = 0

    async def fail_second(fact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic failure')
        return await create(fact)

    monkeypatch.setattr(repo, 'create_fact', fail_second)
    with pytest.raises(OSError):
        await service.apply('u', command.command_id)
    partial = await service.get('u', 'op')
    assert partial.fields['name'].status == 'applied' and partial.fields['gender'].status == 'pending'
    assert (await service.list_pending()).items
    monkeypatch.setattr(repo, 'create_fact', create)
    restored = ProfileBasicInfoService(repo, service.store, clock=lambda: NOW)
    assert (await restored.apply('u', command.command_id)).status == 'applied'
    assert len(collections[1].documents) == 3


@pytest.mark.asyncio
async def test_legacy_unknown_and_high_protection_and_label_is_not_name(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    await legacy_fact(repo, sid, fact_id='high', value=45, confirmed=True, at=datetime(2025, 1, 1, tzinfo=UTC))
    await legacy_fact(repo, sid, fact_id='low', value=40, report_source=True)
    await legacy_fact(repo, sid, field='name', fact_id='unknown_name', value='Legacy Real Name')
    await service.ingest_report(
        'u',
        sid,
        source_id='a',
        source_revision=1,
        fields=[report('age', 41, 'medium'), report('name', 'New Report Name', 'medium')],
    )
    current = await service.read('u')
    assert current.fields['age'].value == 45
    assert current.fields['name'].value == 'Legacy Real Name' and current.fields['name'].protected
    assert current.fields['name'].source_reliability == 'unknown'
    assert (await repo.get_self_subject('u')).display_name != 'New Report Name'
    assert current.sources[0].age_basis is None  # Historical confirmation time is unknown.


@pytest.mark.asyncio
@pytest.mark.parametrize('with_report', [False, True])
async def test_trusted_dob_survives_report_age_or_name_only_governance(composition, with_report):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    birth = await legacy_fact(repo, sid, field='dob', value='1980-11-13', confirmed=True)
    if with_report:
        await service.ingest_report('u', sid, source_id='a', source_revision=1, fields=[report('age', 30)])
    else:
        await service.guard_mutation('u', sid, 'name', 'name_change')
        await service.accept_manual_name('u', sid, 'New Name', 'name_change')
        await service.publish_governance('u', sid)
    current = await service.read('u')
    assert current.fields['age'].value == 45 and current.fields['age'].age_basis == 'dob_derived'
    assert current.fields['age'].source_ref['birthFactId'] == birth.fact_id
    assert (await profile.get_profile('u'))['overview']['age'] == 45


@pytest.mark.asyncio
async def test_delayed_confirmation_matches_dob_at_confirmation_and_withdrawal_falls_back(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    birth = await legacy_fact(repo, sid, field='dob', value='1984-11-13', confirmed=True)
    first = await service.register('u', 'same_before_birthday', values=values(age=41), confirmed_at=NOW)
    service.clock = lambda: datetime(2026, 11, 14, tzinfo=UTC)
    await service.apply('u', first.command_id)
    second = await service.register('u', 'same_again', values=values(age=41), confirmed_at=NOW)
    assert (await service.apply('u', second.command_id)).status == 'no_change'
    info = await service.read('u')
    assert info.fields['age'].value == 42 and info.fields['age'].age_basis == 'dob_derived'
    assert (await service.read('u', as_of=NOW)).fields['age'].value == 41
    await repo.replace_fact(
        birth.model_copy(update={'status': FactStatus.RETRACTED, 'version': birth.version + 1}),
        expected_version=birth.version,
        allowed_statuses=(FactStatus.ACTIVE,),
    )
    fallback = (await service.read('u')).fields['age']
    assert fallback.value == 41 and fallback.age_basis == 'confirmed_at_time' and fallback.confirmed_at == NOW


@pytest.mark.asyncio
async def test_cross_entry_name_edit_and_fact_delete_fence_old_confirmation(composition):
    repo, service, profile, _ = composition
    initial = await service.register('u', 'initial', values=values(), confirmed_at=NOW)
    await service.apply('u', initial.command_id)
    old = await service.register('u', 'old', values=values(name='Old Pending'), confirmed_at=NOW)
    subject = await repo.get_self_subject('u')
    await profile.upsert_subject(
        'u',
        request_id='manual_name',
        subject_id=subject.subject_id,
        display_name='Manual New',
        expected_subject_version=subject.version,
    )
    assert (await service.apply('u', old.command_id)).fields['name'].status == 'superseded'
    assert (await service.read('u')).fields['name'].value == 'Manual New'
    assert (await repo.list_subjects('u'))[0].display_name == 'Manual New'
    pending = await service.register('u', 'before_delete', values=values(age=50), confirmed_at=NOW)
    age = (await service.read('u')).fields['age']
    fact = await repo.get_fact('u', subject.subject_id, age.fact_id)
    view = await repo.get_view('u', subject.subject_id)
    await profile.delete_fact(
        'u',
        subject.subject_id,
        fact.fact_id,
        delete_scope='fact',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='delete_age',
    )
    assert (await service.apply('u', pending.command_id)).fields['age'].status == 'superseded'
    current = await service.read('u')
    assert current.fields['age'].value is None
    assert all(row.value is None for row in current.sources if row.fact_id == fact.fact_id)
    assert (await repo.get_view('u', subject.subject_id)).basic_info_revision == current.revision


@pytest.mark.asyncio
async def test_archived_subject_is_not_rebound_or_recreated(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    command = await service.register('u', 'old', values=values(), confirmed_at=NOW)
    subject = await repo.get_subject('u', sid)
    from health_profile.models import SubjectStatus

    await repo.update_subject(
        subject.model_copy(update={'status': SubjectStatus.ARCHIVED}), expected_version=subject.version
    )
    assert (await service.apply('u', command.command_id)).status == 'blocked'
    assert (await service.get('u', 'old')).subject_id == sid
    with pytest.raises(BasicInfoConflict):
        await service.register('u', 'new', values=values(), confirmed_at=NOW)


@pytest.mark.asyncio
async def test_v1_frozen_bytes_v2_candidates_and_unknown_dates_paging(composition):
    repo, service, profile, _ = composition
    from health_profile.snapshots import ProfileSnapshotReader

    reader = ProfileSnapshotReader(repo, clock=lambda: NOW)
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    before = await reader.read('u')
    frozen = before.model_dump_json(by_alias=True)
    await service.ingest_report(
        'u', sid, source_id='a', source_revision=1, fields=[report('age', 40), report('name', 'Report Name')]
    )
    await service.ingest_report('u', sid, source_id='b', source_revision=1, fields=[report('age', 41, 'medium')])
    assert before.model_dump_json(by_alias=True) == frozen
    v2 = await reader.read_v2('u')
    assert v2.contract_version == 'profile-snapshot.v2'
    assert len(v2.basic_info.sources) == 3
    assert {fact.fact_id for fact in v2.facts} == {
        item.fact_id for item in v2.basic_info.fields.values() if item.fact_id
    }
    page = await reader.read_page('u', page_size=1)
    facts = list(page.facts)
    while page.next_cursor:
        page = await reader.read_page('u', page_size=1, cursor=page.next_cursor)
        assert page.status == 'ready' and page.facts
        facts.extend(page.facts)
    assert len(facts) == 2 and all(fact.effective_period['start'] is None for fact in facts)
    api_page = await profile.list_facts('u', limit=1)
    saved_cursor = api_page['nextCursor']
    api_facts = list(api_page['items'])
    while api_page['nextCursor']:
        api_page = await profile.list_facts('u', limit=1, cursor=api_page['nextCursor'])
        assert api_page['items']
        api_facts.extend(api_page['items'])
    assert len(api_facts) == 2
    await service.ingest_report(
        'u', sid, source_id='c', source_revision=1, fields=[report('name', 'Upgraded Name', 'medium')]
    )
    from health_profile import HealthProfileConflictError

    with pytest.raises(HealthProfileConflictError):
        await profile.list_facts('u', limit=1, cursor=saved_cursor)


@pytest.mark.asyncio
async def test_delayed_old_apply_cannot_rewrite_new_manual_name(composition, monkeypatch):
    repo, service, profile, _ = composition
    initial = await service.register('u', 'initial', values=values(), confirmed_at=NOW)
    await service.apply('u', initial.command_id)
    old = await service.register('u', 'delayed', values=values(name='Earlier Confirmation'), confirmed_at=NOW)
    paused, release = asyncio.Event(), asyncio.Event()

    async def delayed_sync(*args):
        paused.set()
        await release.wait()

    monkeypatch.setattr(service, '_sync_display', delayed_sync)
    running = asyncio.create_task(service.apply('u', old.command_id))
    await paused.wait()
    subject = await repo.get_self_subject('u')
    await profile.upsert_subject(
        'u',
        request_id='later_name',
        subject_id=subject.subject_id,
        display_name='Later Manual Name',
        expected_subject_version=subject.version,
    )
    release.set()
    await running
    assert (await service.read('u')).fields['name'].value == 'Later Manual Name'
    assert (await repo.get_self_subject('u')).display_name == 'Later Manual Name'
    assert (await repo.list_subjects('u'))[0].display_name == 'Later Manual Name'


@pytest.mark.asyncio
async def test_conflicting_dob_is_retained_but_explicitly_not_used(composition):
    repo, service, profile, _ = composition
    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    birth = await legacy_fact(repo, sid, field='dob', value='1980-01-01', confirmed=True)
    command = await service.register('u', 'confirm', values=values(age=42), confirmed_at=NOW)
    await service.apply('u', command.command_id)
    age = (await service.read('u')).fields['age']
    assert age.value == 42 and age.age_basis == 'confirmed_at_time'
    assert age.source_ref['birthDateDecision'] == 'conflict'
    assert age.source_ref['birthDateComparedAt'] == NOW
    assert (await repo.get_fact('u', sid, birth.fact_id)).status is FactStatus.ACTIVE


@pytest.mark.asyncio
async def test_active_demographic_resolution_uses_shared_fence_and_no_observation_date(composition):
    repo, service, profile, _ = composition
    initial = await service.register('u', 'initial', values=values(), confirmed_at=NOW)
    await service.apply('u', initial.command_id)
    pending = await service.register('u', 'before_replace', values=values(age=50), confirmed_at=NOW)
    current = await service.read('u')
    fact = await repo.get_fact('u', current.subject_id, current.fields['age'].fact_id)
    view = await repo.get_view('u', current.subject_id)
    await profile.resolve_fact(
        'u',
        current.subject_id,
        fact.fact_id,
        'replace',
        replacement={
            'valueType': 'quantity',
            'value': {'number': 44, 'unit': 'years'},
            'effectivePeriod': {'start': None},
        },
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='manual_replace',
    )
    assert (await service.apply('u', pending.command_id)).fields['age'].status == 'superseded'
    age = (await service.read('u')).fields['age']
    assert age.value == 44 and age.source_reliability == 'very_high' and age.observed_at is None


@pytest.mark.asyncio
async def test_lifecycle_change_during_materialization_never_publishes_or_reimports_old_epoch(composition, monkeypatch):
    repo, service, profile, _ = composition
    from health_profile.models import SubjectStatus
    from health_profile.snapshots import ProfileSnapshotReader

    sid = (await profile.ensure_self_profile('u')).subject.subject_id
    command = await service.register('u', 'before_archive', values=values(name='Stale Name'), confirmed_at=NOW)
    materialize = service._materialize
    paused, release = asyncio.Event(), asyncio.Event()

    async def pause_after_write(*args):
        result = await materialize(*args)
        paused.set()
        await release.wait()
        return result

    monkeypatch.setattr(service, '_materialize', pause_after_write)
    applying = asyncio.create_task(service.apply('u', command.command_id))
    await paused.wait()
    original = await repo.get_subject('u', sid)
    archived = await repo.update_subject(
        original.model_copy(update={'status': SubjectStatus.ARCHIVED}), expected_version=original.version
    )
    await repo.update_subject(
        archived.model_copy(update={'status': SubjectStatus.ACTIVE}), expected_version=archived.version
    )
    release.set()
    assert (await applying).status == 'blocked'
    current = await service.read('u')
    assert current.lifecycle_epoch == 3 and current.revision > 0
    assert all(item.value is None for item in current.fields.values())
    assert (await profile.get_profile('u'))['overview']['name'] is None
    assert not (await profile.list_facts('u'))['items']
    reader = ProfileSnapshotReader(repo, clock=lambda: NOW)
    assert not (await reader.read('u')).facts
    assert not (await reader.read_v2('u')).facts
    assert not (await reader.read_page('u')).facts
    assert (await service.get('u', 'before_archive')).lifecycle_epoch == 1


def test_empty_legacy_sources_do_not_imply_report_low():
    from health_profile.basic_info import _reliability

    fact = HealthFact.model_construct(
        assertion_level=AssertionLevel.OBSERVED, verification_status=VerificationStatus.UNCONFIRMED, sources=()
    )
    assert _reliability(fact) == 'unknown'


@pytest.mark.asyncio
async def test_public_factory_exposes_shared_provider_without_construction_io(composition, monkeypatch):
    _, _, _, collections = composition
    from health_profile import application
    from modules.health_profile import ProfileBasicInfoService as PublicService
    from modules.health_profile import create_application_profile_public_contracts
    from modules.health_profile.contracts import BasicInfoSourceV2

    for attribute, collection in zip(
        (
            'collection_health_subjects',
            'collection_health_profile_facts',
            'collection_health_profile_events',
            'collection_health_profile_views',
            'collection_health_profile_jobs',
        ),
        collections[:5],
        strict=True,
    ):
        monkeypatch.setattr(application, attribute, collection)
    monkeypatch.setattr(application, 'collection_health_profile_source_publications', BsonCollection())
    monkeypatch.setattr(application, 'collection_health_reports', BsonCollection())
    monkeypatch.setattr(application, 'collection_health_report_revisions', BsonCollection())
    public = create_application_profile_public_contracts(basic_info_collection=collections[-1])
    assert isinstance(public.basic_info, PublicService)
    assert public.basic_info.repository is public.inbox.repository
    assert public.basic_info.store.collection is collections[-1]
    assert all(not item.documents and not item.indexes for item in collections)
    command = await public.basic_info.register('u', 'public', values=values(), confirmed_at=NOW)
    await public.basic_info.apply('u', command.command_id)
    snapshot = await public.reader.read_v2('u')
    assert snapshot.basic_info.fields['name'].value == values().name
    assert isinstance(snapshot.basic_info.sources[0], BasicInfoSourceV2)


@pytest.mark.asyncio
async def test_pending_operation_prefix_advances_past_nonmatching_scopes_and_binds_the_cursor(composition):
    _, service, _, _ = composition
    await service.register('scoped-a', 'other:one', values=values(), confirmed_at=NOW)
    await service.register('scoped-b', 'other:two', values=values(), confirmed_at=NOW)
    await service.register('scoped-c', 'other:three', values=values(), confirmed_at=NOW)
    rows = await service.store.scan(limit=10)
    user = rows[-1]['userId']
    await service.register(user, 'health-analysis:target', values=values(), confirmed_at=NOW)

    first = await service.list_pending(limit=1, operation_prefix='health-analysis:')
    assert not first.items and first.next_cursor
    with pytest.raises(ValueError, match='invalid_cursor'):
        await service.list_pending(limit=1, operation_prefix='other:', cursor=first.next_cursor)
    ready = await service.list_pending(limit=1, operation_prefix='health-analysis:', cursor=first.next_cursor)
    assert [item.operation_id for item in ready.items] == ['health-analysis:target']
    assert ready.next_cursor is None


@pytest.mark.asyncio
async def test_current_age_page_preserves_original_fact_and_restarts_across_birthday(composition):
    repo, service, profile, _ = composition
    from health_profile.snapshots import ProfileSnapshotReader

    subject = (await profile.ensure_self_profile('u')).subject
    await legacy_fact(repo, subject.subject_id, field='dob', value='1984-11-13', confirmed=True)
    command = await service.register('u', 'birth-cursor', values=values(age=41), confirmed_at=NOW)
    await service.apply('u', command.command_id)
    now = [NOW]
    reader = ProfileSnapshotReader(repo, clock=lambda: now[0])
    fixed_page = await reader.read_page('u')
    original_age = next(fact for fact in fixed_page.facts if fact.fact_type == 'demographic.age')
    assert fixed_page.coverage['currentAge']['value'] == 41
    assert original_age.value['number'] == 41
    first = await reader.read_page('u', page_size=1)
    assert first.next_cursor is not None
    now[0] = datetime(2026, 11, 14, tzinfo=UTC)
    assert (await reader.read_page('u', page_size=1, cursor=first.next_cursor)).status == 'restart_required'
    current = await reader.read_page('u')
    projected = current.coverage['currentAge']
    assert (projected['factId'], projected['value'], projected['ageBasis']) == (original_age.fact_id, 42, 'dob_derived')
    assert current.coverage['currentAge']['asOf'] == now[0].isoformat()
    assert next(fact for fact in current.facts if fact.fact_id == original_age.fact_id) == original_age
    assert fixed_page.coverage['currentAge']['value'] == 41


@pytest.mark.asyncio
async def test_real_basic_info_store_uses_majority_write_and_receipt_read_without_connecting():
    from pymongo import AsyncMongoClient
    from pymongo import ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern

    from health_profile.basic_info_store import MongoBasicInfoStore

    client = AsyncMongoClient(connect=False)
    try:
        inherited = client.synthetic.get_collection(
            'basic_info',
            read_preference=ReadPreference.SECONDARY,
            read_concern=ReadConcern('local'),
            write_concern=WriteConcern(w=1),
        )
        store = MongoBasicInfoStore(inherited)
        assert store.collection.full_name == inherited.full_name
        assert store.collection.read_preference == ReadPreference.PRIMARY
        assert store.collection.read_concern.level == 'majority'
        assert store.collection.write_concern.document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
        assert inherited.write_concern.document == {'w': 1}
        fake = BsonCollection()
        assert MongoBasicInfoStore(fake).collection is fake
    finally:
        await client.close()
