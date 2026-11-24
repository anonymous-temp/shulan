"""Report contribution restoration through actual Profile providers and synthetic Mongo I/O."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from health_profile import HealthProfileService
from health_profile import MongoHealthProfileRepository
from health_profile.basic_info_contracts import BasicInfoValuesV1
from health_profile.basic_info_store import BasicInfoConflict
from health_profile.models import EffectivePeriod
from health_profile.models import FactSource
from health_profile.models import FactStatus
from health_profile.models import HealthFact
from health_profile.snapshots import ProfileSnapshotReader
from tests.unit.health_profile.test_basic_info import BsonCollection

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
USER = 'test-basic-info-report-restore'


@pytest_asyncio.fixture
async def provider():
    subjects, facts, events, views, jobs, basic = (BsonCollection() for _ in range(6))
    repository = MongoHealthProfileRepository(
        subjects=subjects, facts=facts, events=events, views=views, jobs=jobs, basic_info=basic
    )
    service = repository.basic_info
    service.clock = lambda: NOW
    profile = HealthProfileService(repository, clock=lambda: NOW)
    subject = (await profile.ensure_self_profile(USER)).subject
    return SimpleNamespace(
        repository=repository,
        service=service,
        profile=profile,
        subject_id=subject.subject_id,
        basic_collection=basic,
    )


def contribution(identity, value, grade='medium'):
    quote = f'Synthetic patient age: {value}'
    return {
        'field': 'age',
        'value': value,
        'sourceReliability': grade,
        'observedAt': '2026-11-01',
        'reason': 'Synthetic report evidence',
        'evidence': {'quote': quote, 'start': 0, 'end': len(quote)},
        'subjectStatus': 'single',
        'contributionId': identity,
    }


async def publish(env, source, revision, fields):
    return await env.service.ingest_report(
        USER,
        env.subject_id,
        source_id=source,
        source_revision=1,
        publication_revision=revision,
        fields=fields,
    )


async def withdraw(env, source, revision):
    return await env.service.withdraw_report(USER, env.subject_id, source_id=source, publication_revision=revision)


def candidate(snapshot, identity):
    return next(row for row in snapshot.sources if (row.source_ref or {}).get('contributionId') == identity)


@pytest.mark.asyncio
async def test_restored_higher_grade_is_current_and_preserves_fact_identity_and_fixed_snapshots(provider):
    low, medium = contribution('a-low', 40, 'low'), contribution('b-medium', 42)
    await publish(provider, 'a', 1, [low])
    initial = await publish(provider, 'b', 1, [medium])
    saved = candidate(initial, 'b-medium')
    reader = ProfileSnapshotReader(provider.repository, clock=lambda: NOW)
    old_v1, old_v2 = await reader.read(USER), await reader.read_v2(USER)
    old_bytes = old_v1.model_dump_json(), old_v2.model_dump_json()
    fallback = await withdraw(provider, 'b', 2)
    assert fallback.fields['age'].value == 40 and fallback.fields['age'].source_reliability == 'low'
    assert not candidate(fallback, 'b-medium').active
    restored = await publish(provider, 'b', 3, [medium])
    assert restored.fields['age'].value == 42
    assert restored.fields['age'].source_reliability == 'medium'
    current = candidate(restored, 'b-medium')
    assert current.active and current.current
    assert (current.source_key, current.fact_id) == (saved.source_key, saved.fact_id)
    assert [row.source_key for row in restored.sources] == [row.source_key for row in initial.sources]
    assert (await provider.repository.get_fact(USER, provider.subject_id, saved.fact_id)).status is FactStatus.ACTIVE
    replay = await publish(provider, 'b', 3, [medium])
    assert replay.model_dump_json() == restored.model_dump_json()
    assert (old_v1.model_dump_json(), old_v2.model_dump_json()) == old_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('restored_value', 'restored_grade', 'current_value', 'current_grade', 'expected_source'),
    [
        (40, 'medium', 42, 'medium', 'current'),
        (40, 'low', 42, 'medium', 'current'),
        (40, 'medium', 40, 'low', 'restored'),
    ],
)
async def test_restoration_obeys_current_grade_and_same_value_upgrade(
    provider, restored_value, restored_grade, current_value, current_grade, expected_source
):
    restored = contribution('restored', restored_value, restored_grade)
    current = contribution('current', current_value, current_grade)
    await publish(provider, 'restored', 1, [restored])
    await withdraw(provider, 'restored', 2)
    await publish(provider, 'current', 1, [current])
    result = await publish(provider, 'restored', 3, [restored])
    assert candidate(result, expected_source).current
    assert candidate(result, 'restored').active
    expected = current if expected_source == 'current' else restored
    assert (result.fields['age'].value, result.fields['age'].source_reliability) == (
        expected['value'],
        expected['sourceReliability'],
    )


@pytest.mark.asyncio
async def test_restored_report_cannot_override_api_confirmation(provider):
    medium = contribution('report-age', 42)
    await publish(provider, 'report', 1, [medium])
    await withdraw(provider, 'report', 2)
    confirmation = await provider.service.register(
        USER,
        'synthetic-confirm',
        confirmed_at=NOW,
        values=BasicInfoValuesV1(name='Synthetic patient', age=50, gender='male'),
    )
    await provider.service.apply(USER, confirmation.command_id)
    before = (await provider.service.read(USER)).fields['age']
    after = await publish(provider, 'report', 3, [medium])
    assert after.fields['age'] == before
    assert after.fields['age'].source_reliability == 'very_high'
    assert candidate(after, 'report-age').active and not candidate(after, 'report-age').current


@pytest.mark.asyncio
async def test_restored_report_cannot_override_protected_unknown_history(provider):
    await provider.repository.create_fact(
        HealthFact(
            fact_id='synthetic-unknown-age',
            user_id=USER,
            subject_id=provider.subject_id,
            fact_type='demographic.age',
            category='demographic',
            display_name='Synthetic legacy age',
            value_type='quantity',
            value={'number': 39, 'unit': 'years'},
            assertion_level='observed',
            verification_status='verified_source',
            confidence=0.8,
            effective_period=EffectivePeriod(start=NOW),
            status='active',
            sources=(FactSource(source_type='legacy', source_id='synthetic-legacy', source_revision=1),),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    medium = contribution('report-age', 42)
    await publish(provider, 'report', 1, [medium])
    await withdraw(provider, 'report', 2)
    before = (await provider.service.read(USER)).fields['age']
    result = await publish(provider, 'report', 3, [medium])
    assert result.fields['age'] == before
    assert result.fields['age'].value == 39 and result.fields['age'].protected
    assert candidate(result, 'report-age').active and not candidate(result, 'report-age').current


@pytest.mark.asyncio
async def test_higher_publication_cannot_restore_deleted_contribution_or_erased_evidence(provider):
    medium = contribution('deleted-report-age', 42)
    initial = await publish(provider, 'report', 1, [medium])
    original = candidate(initial, 'deleted-report-age')
    fact = await provider.repository.get_fact(USER, provider.subject_id, original.fact_id)
    view = await provider.repository.get_view(USER, provider.subject_id)
    await provider.profile.delete_fact(
        USER,
        provider.subject_id,
        original.fact_id,
        delete_scope='fact',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id='synthetic-delete',
    )
    after_delete = await provider.service.read(USER)
    result = await publish(provider, 'report', 3, [medium])
    deleted = next(row for row in result.sources if row.source_key == original.source_key)
    assert result.fields['age'] == after_delete.fields['age']
    assert result.fields['age'].value is None
    assert deleted.deleted and not deleted.active and not deleted.current and deleted.value is None
    assert 'evidence' not in (deleted.source_ref or {})
    assert await provider.repository.get_fact(USER, provider.subject_id, original.fact_id) is None
    assert await provider.repository.get_deleted_fact_tombstone(USER, provider.subject_id, original.fact_id)


async def reject_candidate(provider, fact_id, *, request='synthetic-reject'):
    fact = await provider.repository.get_fact(USER, provider.subject_id, fact_id)
    view = await provider.repository.get_view(USER, provider.subject_id)
    return await provider.profile.resolve_fact(
        USER,
        provider.subject_id,
        fact_id,
        'reject',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        request_id=request,
    )


@pytest.mark.asyncio
async def test_legacy_manual_rejection_is_not_restored_after_its_inventory_entry_was_removed(provider):
    low, medium = contribution('rejected-low', 40, 'low'), contribution('selected-medium', 42)
    await publish(provider, 'report', 1, [low])
    before = await publish(provider, 'report', 2, [low, medium])
    rejected_fact_id = candidate(before, 'rejected-low').fact_id
    await reject_candidate(provider, rejected_fact_id)

    # Model a pre-fix persisted rejection with its real scope/index and fact.
    key = provider.service._scope_key(USER, provider.subject_id, before.lifecycle_epoch)
    next(row for row in provider.basic_collection.documents if row['_id'] == key).pop('rejectedFactIds')
    withdrawn = await withdraw(provider, 'report', 3)
    assert withdrawn.fields['age'].value is None
    restored = await publish(provider, 'report', 4, [low])
    assert restored.fields['age'].value is None
    rejected = candidate(restored, 'rejected-low')
    assert not rejected.active and not rejected.current and not rejected.deleted
    assert rejected.value == 40 and rejected.source_ref['evidence'] == low['evidence']
    assert (
        await provider.repository.get_fact(USER, provider.subject_id, rejected_fact_id)
    ).status is FactStatus.RETRACTED
    assert candidate(await publish(provider, 'report', 4, [low]), 'rejected-low') == rejected


@pytest.mark.asyncio
async def test_report_application_loses_race_to_fenced_manual_rejection(provider, monkeypatch):
    low, medium = contribution('racing-low', 40, 'low'), contribution('selected-medium', 42)
    await publish(provider, 'report', 1, [low])
    before = await publish(provider, 'report', 2, [low, medium])
    rejected_id = candidate(before, 'racing-low').fact_id
    original = provider.service._materialize
    observed = []

    async def interleave(user_id, subject, source, operation_id):
        if source['factId'] == rejected_id and not observed:
            observed.append(await reject_candidate(provider, rejected_id))
        return await original(user_id, subject, source, operation_id)

    monkeypatch.setattr(provider.service, '_materialize', interleave)
    result = await publish(provider, 'report', 3, [low, medium])
    assert len(observed) == 1
    assert (result.fields['age'].value, result.fields['age'].fact_id) == (42, before.fields['age'].fact_id)
    assert not candidate(result, 'racing-low').active
    assert (await provider.repository.get_fact(USER, provider.subject_id, rejected_id)).status is FactStatus.RETRACTED
    assert candidate(await publish(provider, 'report', 3, [low, medium]), 'racing-low').active is False


@pytest.mark.asyncio
async def test_lower_or_equal_publication_cannot_restore_withdrawn_contribution(provider):
    low, medium = contribution('low', 40, 'low'), contribution('medium', 42)
    await publish(provider, 'low', 1, [low])
    await publish(provider, 'medium', 1, [medium])
    retired = await withdraw(provider, 'medium', 2)
    old = await publish(provider, 'medium', 1, [medium])
    assert old.model_dump_json() == retired.model_dump_json()
    assert not candidate(old, 'medium').active and old.fields['age'].value == 40
    with pytest.raises(BasicInfoConflict, match='publication_content_conflict'):
        await publish(provider, 'medium', 2, [medium])
    assert (await provider.service.read(USER)).model_dump_json() == retired.model_dump_json()
    assert (await withdraw(provider, 'medium', 2)).model_dump_json() == retired.model_dump_json()
    changed = deepcopy(medium)
    changed['value'] = 43
    with pytest.raises(BasicInfoConflict, match='contribution_content_conflict'):
        await publish(provider, 'medium', 3, [changed])
    assert (await provider.service.read(USER)).model_dump_json() == retired.model_dump_json()


@pytest.mark.asyncio
async def test_batch_restoration_without_current_uses_original_order(provider):
    first, second = contribution('first', 40), contribution('second', 42)
    initial = await publish(provider, 'report', 1, [first, second])
    assert initial.fields['age'].value == 40
    await withdraw(provider, 'report', 2)
    restored = await publish(provider, 'report', 3, [second, first])
    assert restored.fields['age'].value == 40
    assert candidate(restored, 'first').fact_id == candidate(initial, 'first').fact_id
    assert [row.source_key for row in restored.sources] == [row.source_key for row in initial.sources]


@pytest.mark.asyncio
async def test_batch_new_and_restored_same_grade_uses_original_admission_order(provider):
    first, new = contribution('first', 40), contribution('new', 42)
    await publish(provider, 'report', 1, [first])
    await withdraw(provider, 'report', 2)
    restored = await publish(provider, 'report', 3, [new, first])
    assert restored.fields['age'].value == 40
    assert candidate(restored, 'first').current and not candidate(restored, 'new').current


@pytest.mark.asyncio
async def test_batch_restored_higher_grade_compares_in_original_order(provider):
    low, first, second = contribution('low', 38, 'low'), contribution('first', 40), contribution('second', 42)
    await publish(provider, 'fallback', 1, [low])
    await publish(provider, 'report', 1, [first, second])
    await withdraw(provider, 'report', 2)
    restored = await publish(provider, 'report', 3, [second, first])
    assert restored.fields['age'].value == 40
    assert candidate(restored, 'first').current and not candidate(restored, 'second').current


@pytest.mark.asyncio
async def test_restored_activity_selection_and_publication_watermark_commit_together(provider, monkeypatch):
    low, medium = contribution('low', 40, 'low'), contribution('medium', 42)
    await publish(provider, 'fallback', 1, [low])
    await publish(provider, 'report', 1, [medium])
    await withdraw(provider, 'report', 2)
    original_cas = provider.service.store.cas
    applied = []

    async def observe_commit(before, after):
        matching = [row for row in after.get('reportPublications', {}).values() if row['revision'] == 3]
        if matching:
            prior_read = await provider.service.read(USER)
            assert prior_read.fields['age'].value == 40
            key = next(
                key for key, row in after['sources'].items() if row['sourceRef'].get('contributionId') == 'medium'
            )
            assert after['sources'][key]['active']
            assert after['selected']['age'] == key
            applied.append(key)
        return await original_cas(before, after)

    monkeypatch.setattr(provider.service.store, 'cas', observe_commit)
    restored = await publish(provider, 'report', 3, [medium])
    assert len(applied) == 1 and restored.fields['age'].value == 42


@pytest.mark.asyncio
async def test_qualified_replacement_uses_original_order_for_restored_same_grade_sources(provider):
    first = contribution('first-medium', 40)
    second = contribution('second-medium', 42)
    low = contribution('low-current', 38, 'low')
    initial = await publish(provider, 'same-owner', 1, [first, second, low])
    original = candidate(initial, 'first-medium')
    assert initial.fields['age'].value == 40
    await withdraw(provider, 'same-owner', 2)
    fallback = await publish(provider, 'same-owner', 3, [low])
    assert fallback.fields['age'].value == 38
    # Removing the current low source enters the qualified replacements path;
    # reversing the restored array must not change the old admission order.
    restored = await publish(provider, 'same-owner', 4, [second, first])
    assert restored.fields['age'].value == 40
    assert candidate(restored, 'first-medium').current
    assert candidate(restored, 'first-medium').fact_id == original.fact_id
    assert not candidate(restored, 'low-current').active
    assert (await publish(provider, 'same-owner', 4, [second, first])).model_dump_json() == restored.model_dump_json()


@pytest.mark.asyncio
async def test_deleted_higher_grade_cannot_qualify_a_conflicting_lower_grade_replacement(provider):
    first = contribution('first-medium', 40)
    deleted = contribution('deleted-medium', 42)
    low = contribution('low-current', 38, 'low')
    initial = await publish(provider, 'same-owner', 1, [first, deleted, low])
    removed = candidate(initial, 'deleted-medium')
    fact = await provider.repository.get_fact(USER, provider.subject_id, removed.fact_id)
    view = await provider.repository.get_view(USER, provider.subject_id)
    await provider.profile.delete_fact(
        USER,
        provider.subject_id,
        fact.fact_id,
        delete_scope='fact',
        request_id='delete-noncurrent',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
    )
    assert (await provider.service.read(USER)).fields['age'].value == 40
    await withdraw(provider, 'same-owner', 2)
    fallback = await publish(provider, 'same-owner', 3, [low])
    assert fallback.fields['age'].value == 38
    with pytest.raises(BasicInfoConflict, match='unqualified_source_replacement'):
        await publish(provider, 'same-owner', 4, [deleted, contribution('new-low', 39, 'low')])
    current = await provider.service.read(USER)
    assert current.fields['age'].value == 38
    tombstone = next(row for row in current.sources if row.fact_id == removed.fact_id)
    assert tombstone.deleted and tombstone.value is None
    assert not any(row.source_ref.get('contributionId') == 'new-low' for row in current.sources)
