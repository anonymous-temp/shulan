"""The Chat and Record contexts consume the same Profile current-age projection."""

import json
from datetime import UTC
from datetime import datetime

import pytest

from adapters.chat.profile_context import create_chat_profile_context
from adapters.record.profile_context import RecordProfilePageAdapter
from health_profile.models import AssertionLevel
from health_profile.models import EffectivePeriod
from health_profile.models import FactCategory
from health_profile.models import FactSource
from health_profile.models import FactStatus
from health_profile.models import FactValueType
from health_profile.models import HealthFact
from health_profile.models import VerificationStatus
from modules.health_profile.application import create_application_profile_public_contracts
from modules.health_profile.contracts import BasicInfoValuesV1
from modules.record.profile_memory import RecordProfileMemory
from tests.unit._support.current_document_profile import profile_scope


@pytest.mark.asyncio
async def test_current_age_is_shared_by_chat_and_record_without_changing_the_fixed_fact(monkeypatch):
    collections, repository, scope = await profile_scope(monkeypatch)
    profiles = create_application_profile_public_contracts(basic_info_collection=collections['basic_info'])
    confirmed_at = datetime(2026, 11, 12, 10, tzinfo=UTC)
    observed = [confirmed_at]
    profiles.basic_info.clock = profiles.reader._clock = lambda: observed[0]
    birth = HealthFact(
        fact_id='trusted-birth',
        user_id=scope.user_id,
        subject_id=scope.subject_id,
        fact_type='demographic.birth_date',
        category=FactCategory.DEMOGRAPHIC,
        display_name='Birth date',
        value_type=FactValueType.TEXT,
        value='1984-11-13',
        assertion_level=AssertionLevel.USER_CONFIRMED,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=1,
        effective_period=EffectivePeriod(start=confirmed_at),
        status=FactStatus.ACTIVE,
        sources=(FactSource(source_type='manual', source_id='manual-birth', source_revision=1),),
        created_at=confirmed_at,
        updated_at=confirmed_at,
    )
    await repository.create_fact(birth)
    command = await profiles.basic_info.register(
        scope.user_id,
        'synthetic-confirm',
        values=BasicInfoValuesV1(name='Synthetic user', age=41, gender='female'),
        confirmed_at=confirmed_at,
    )
    receipt = await profiles.basic_info.apply(scope.user_id, command.command_id)
    assert receipt.status == 'applied'
    fact_id = receipt.fields['age'].fact_id
    original = await repository.get_fact(scope.user_id, scope.subject_id, fact_id)
    assert original.value['number'] == 41

    chat = create_chat_profile_context(profile_reader=profiles.reader, page_size=2)
    record = RecordProfileMemory(RecordProfilePageAdapter(profiles.reader), page_size=2)

    async def age_payload(provider):
        context = await provider.build_context(scope.user_id)
        for line in context.splitlines():
            if line.startswith('{'):
                row = json.loads(line)
                if row['factId'] == fact_id:
                    return row
        pytest.fail('Shared age did not reach the consumer')

    before = [await age_payload(provider) for provider in (chat, record)]
    assert all(row['value'] == 41 and row['ageBasis'] == 'dob_derived' for row in before)
    observed[0] = datetime(2026, 11, 14, 10, tzinfo=UTC)
    after = [await age_payload(provider) for provider in (chat, record)]
    assert all(row['value'] == 42 and row['ageBasis'] == 'dob_derived' for row in after)
    assert all(row['ageAsOf'].startswith('2026-11-14') for row in after)
    assert all(row['ageConfirmedAt'].startswith('2026-11-12') for row in after)
    assert all(row['value'] == 41 for row in before)
    assert (await repository.get_fact(scope.user_id, scope.subject_id, fact_id)) == original

    await repository.replace_fact(
        birth.model_copy(update={'status': FactStatus.RETRACTED, 'version': birth.version + 1}),
        expected_version=birth.version,
        allowed_statuses=(FactStatus.ACTIVE,),
    )
    fallback = [await age_payload(provider) for provider in (chat, record)]
    assert all(row['value'] == 41 and row['ageBasis'] == 'confirmed_at_time' for row in fallback)
    assert all('ageAsOf' not in row and row['ageConfirmedAt'].startswith('2026-11-12') for row in fallback)


@pytest.mark.asyncio
async def test_dob_only_keeps_birth_date_typed_through_consumer_paging_and_snapshots(monkeypatch):
    collections, repository, scope = await profile_scope(monkeypatch)
    profiles = create_application_profile_public_contracts(basic_info_collection=collections['basic_info'])
    observed = [datetime(2026, 11, 12, 10, tzinfo=UTC)]
    profiles.basic_info.clock = profiles.reader._clock = lambda: observed[0]
    birth = HealthFact(
        fact_id='trusted-dob-only',
        user_id=scope.user_id,
        subject_id=scope.subject_id,
        fact_type='demographic.birth_date',
        category=FactCategory.DEMOGRAPHIC,
        display_name='Birth date',
        value_type=FactValueType.DATE,
        value='1984-11-13',
        assertion_level=AssertionLevel.USER_CONFIRMED,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=1,
        effective_period=EffectivePeriod(start=observed[0]),
        status=FactStatus.ACTIVE,
        sources=(FactSource(source_type='manual', source_id='manual-birth', source_revision=1),),
        created_at=observed[0],
        updated_at=observed[0],
    )
    await repository.create_fact(birth)
    await repository.create_fact(
        birth.model_copy(
            update={
                'fact_id': 'unrelated',
                'fact_type': 'demographic.gender',
                'value_type': FactValueType.TEXT,
                'value': 'female',
            }
        )
    )
    frozen = await profiles.reader.read_v2(scope.user_id)
    assert frozen.basic_info.fields['age'].value == 41
    age_page = await profiles.reader.read_page(scope.user_id, page_size=1)
    assert age_page.status == 'ready' and age_page.next_cursor
    providers = (
        create_chat_profile_context(profile_reader=profiles.reader, page_size=1),
        RecordProfileMemory(RecordProfilePageAdapter(profiles.reader), page_size=1),
    )

    async def birth_payload(provider):
        context = await provider.build_context(scope.user_id)
        births = [
            json.loads(line)
            for line in context.splitlines()
            if line.startswith('{') and 'demographic.birth_date' in line
        ]
        (row,) = births
        assert row['value'] == '1984-11-13'
        assert 'ageBasis' not in row and 'ageAsOf' not in row
        return row['currentAge']

    before = [await birth_payload(provider) for provider in providers]
    assert all(age == {'value': 41, 'ageBasis': 'dob_derived', 'asOf': observed[0].isoformat()} for age in before)
    observed[0] = datetime(2026, 11, 14, 10, tzinfo=UTC)
    after = [await birth_payload(provider) for provider in providers]
    assert all(age == {'value': 42, 'ageBasis': 'dob_derived', 'asOf': observed[0].isoformat()} for age in after)
    current = await profiles.reader.read_v2(scope.user_id)
    assert current.basic_info.fields['age'].value == 42
    assert frozen.basic_info.fields['age'].value == 41
    assert next(fact.value for fact in frozen.facts if fact.fact_id == birth.fact_id) == '1984-11-13'
    assert next(fact.value for fact in current.facts if fact.fact_id == birth.fact_id) == '1984-11-13'
