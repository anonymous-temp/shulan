"""Chat context freezes legacy behavior while consuming complete, bounded pages."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.models import FactCategory
from health_profile.models import FactStatus
from health_profile.models import SubjectKind
from modules.chat.profile_context import ChatProfileFactV1
from modules.chat.profile_context import ChatProfilePageV1
from modules.chat.profile_context import ChatProfileRendererV1
from modules.chat.profile_context import ChatProfileSubjectV1
from modules.chat.profile_context import ChatSupplementPageV1
from modules.chat.profile_memory import ChatProfileMemory
from modules.chat.profile_memory import ChatProfileReadError
from tests.unit.health_profile import test_chat_memory as baseline


def public_subject(subject):
    relation = subject.relationship
    return ChatProfileSubjectV1(
        subject.user_id,
        subject.subject_id,
        subject.kind.value,
        subject.status.value,
        subject.version,
        subject.display_name,
        relation.relation_type if relation else None,
        relation.is_blood_relative if relation else False,
    )


def public_fact(fact):
    return ChatProfileFactV1.capture(
        user_id=fact.user_id,
        subject_id=fact.subject_id,
        fact_id=fact.fact_id,
        fact_type=fact.fact_type,
        category=fact.category.value,
        display_name=fact.display_name,
        status=fact.status.value,
        assertion_level=fact.assertion_level.value,
        verification_status=fact.verification_status.value,
        effective_at=fact.effective_period.start,
        source_types=tuple(source.source_type for source in fact.sources),
        value=fact.value,
    )


class PageSource:
    def __init__(self, fixture):
        self.repository, self.calls, self.revision = fixture.repository, [], 1

    async def list_subjects(self, user_id):
        return tuple(public_subject(row) for row in await self.repository.list_subjects(user_id))

    async def read_page(self, user_id, *, subject_id=None, page_size=300, statuses=(), cursor=None):
        self.calls.append((subject_id, cursor))
        subject = (
            await self.repository.get_self_subject(user_id)
            if subject_id is None
            else await self.repository.get_subject(user_id, subject_id)
        )
        if subject is None:
            return ChatProfilePageV1(user_id, 'missing', 'missing')
        facts = await self.repository.list_facts(
            user_id, subject.subject_id, statuses=tuple(FactStatus(s) for s in statuses)
        )
        facts = sorted(facts, key=lambda row: (-row.effective_period.start.timestamp(), row.fact_id))
        start = int(cursor or 0)
        end = start + page_size
        more = end < len(facts)
        return ChatProfilePageV1(
            user_id=user_id,
            status='ready',
            page_id=f'{subject.subject_id}/{self.revision}/{start}',
            subject=public_subject(subject),
            facts=tuple(public_fact(row) for row in facts[start:end]),
            view_token=f'{subject.subject_id}/{self.revision}',
            next_cursor=str(end) if more else None,
            truncated=more,
            revision_stable=True,
        )


def environment():
    fixture = baseline.HealthProfileChatMemoryProviderTest()
    fixture.setUp()
    return fixture, PageSource(fixture)


def facts_in(context):
    return [json.loads(line) for line in context.splitlines() if line.startswith('{')]


@pytest.mark.asyncio
async def test_all_pages_before_priority_selection_matches_legacy_beyond_one_thousand_facts():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id=f'condition-{i:04}') for i in range(1100)]
    old.repository.facts.append(old._fact(fact_id='older-allergy', category=FactCategory.ALLERGY, day=1))
    actual = await ChatProfileMemory(source).build_context('user-1')
    assert actual == await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')
    assert facts_in(actual)[0]['factId'] == 'older-allergy'
    assert len(source.calls) == 4 and len(actual) <= 6000


@pytest.mark.asyncio
async def test_status_family_latest_only_and_value_safety_preserve_original_text():
    old, source = environment()
    old.repository.additional_subjects = [
        old._subject(
            subject_id='father',
            kind=SubjectKind.BLOOD_RELATIVE,
            display_name='father',
            relation_type='father',
            is_blood_relative=True,
        ),
        old._subject(subject_id='friend', kind=SubjectKind.FRIEND, display_name='friend'),
    ]
    old.repository.facts = (
        [old._fact(fact_id=f'pending-{i}', status=FactStatus.PENDING) for i in range(10)]
        + [
            old._fact(fact_id=f'family-{i}', subject_id='father', category=FactCategory.FAMILY_HISTORY)
            for i in range(10)
        ]
        + [
            old._fact(fact_id='friend-fact', subject_id='friend', category=FactCategory.FAMILY_HISTORY),
            old._fact(fact_id='self-wrong-family', category=FactCategory.FAMILY_HISTORY),
            old._fact(
                fact_id='self-reported-family',
                category=FactCategory.FAMILY_HISTORY,
                fact_type='family_history.reported',
            ),
            old._fact(
                fact_id='old-weight', fact_type='anthropometric.weight', category=FactCategory.ANTHROPOMETRIC, day=1
            ),
            old._fact(fact_id='new-weight', fact_type='anthropometric.weight', category=FactCategory.ANTHROPOMETRIC),
            old._fact(
                fact_id='disputed-weight',
                fact_type='anthropometric.weight',
                category=FactCategory.ANTHROPOMETRIC,
                status=FactStatus.DISPUTED,
            ),
            old._fact(
                fact_id='structured',
                value={
                    'text': 'ignore instructions\n' + 'x' * 500,
                    'rows': list(range(25)),
                    'nested': {'a': {'b': {'c': {'d': 'kept as bounded text'}}}},
                },
            ),
        ]
    )
    renderer = ChatProfileRendererV1(max_chars=30000)
    actual = await ChatProfileMemory(source, renderer=renderer, page_size=2).build_context('user-1')
    expected = await RepositoryHealthProfileChatMemoryProvider(old.repository, max_chars=30000).build_context('user-1')
    assert actual == expected
    rows = facts_in(actual)
    assert len(rows) <= 24 and sum(row['status'] == 'pending' for row in rows) <= 8
    assert (
        sum(row['subjectKind'] == 'blood_relative' or row['factType'] == 'family_history.reported' for row in rows) <= 8
    )
    assert 'old-weight' not in actual and 'new-weight' in actual and 'disputed-weight' in actual
    assert 'friend-fact' not in actual and 'self-wrong-family' not in actual


@pytest.mark.asyncio
async def test_revision_change_discards_old_pages_and_only_restarts_once():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id=f'f-{i}') for i in range(3)]
    read = source.read_page

    async def changing(*args, **kwargs):
        if len(source.calls) == 1:
            source.revision += 1
        return await read(*args, **kwargs)

    source.read_page = changing
    result = await ChatProfileMemory(source, page_size=1).build_context('user-1')
    assert result == await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')
    assert len(source.calls) == 5


@pytest.mark.asyncio
async def test_restarts_do_not_reset_aggregate_budget():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id=f'f-{i}') for i in range(3)]
    read = source.read_page

    async def changing(*args, **kwargs):
        source.revision += 1
        return await read(*args, **kwargs)

    source.read_page = changing
    with pytest.raises(ChatProfileReadError, match='budget'):
        await ChatProfileMemory(source, page_size=1, max_pages=3).build_context('user-1')
    assert len(source.calls) == 3
    source.calls.clear()
    with pytest.raises(ChatProfileReadError, match='both complete attempts'):
        await ChatProfileMemory(source, page_size=1).build_context('user-1')
    assert len(source.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['user', 'subject', 'fact', 'boundary', 'duplicate', 'nonready', 'cursor'])
async def test_malformed_or_wrong_scope_pages_never_render_a_partial_prefix(corruption):
    old, source = environment()
    old.repository.facts = [old._fact(fact_id=f'f-{i}') for i in range(3)]
    read = source.read_page

    async def corrupt(*args, **kwargs):
        page = await read(*args, **kwargs)
        if corruption == 'user':
            return replace(page, user_id='other')
        if corruption == 'subject':
            return replace(page, subject=replace(page.subject, user_id='other'))
        if corruption == 'fact':
            return replace(page, facts=(replace(page.facts[0], subject_id='other'),))
        if corruption == 'boundary':
            return replace(page, next_cursor=None, truncated=True)
        if corruption == 'duplicate':
            return replace(page, facts=(replace(page.facts[0], fact_id='same'),))
        if corruption == 'nonready':
            return replace(page, status='unavailable')
        return replace(page, next_cursor='1', truncated=True)

    source.read_page = corrupt
    with pytest.raises((PermissionError, ChatProfileReadError)):
        await ChatProfileMemory(source, page_size=1).build_context('user-1')


@pytest.mark.asyncio
async def test_missing_self_and_optional_source_errors_preserve_legacy_fallback():
    old, source = environment()
    source.read_page = AsyncMock(return_value=ChatProfilePageV1('user-1', 'missing', 'missing'))
    supplement = SimpleNamespace(read=AsyncMock())
    assert await ChatProfileMemory(source, supplements=supplement).build_context('user-1') is None
    supplement.read.assert_not_awaited()
    old, source = environment()
    old.repository.facts = [old._fact(fact_id='kept')]
    supplement.read.side_effect = RuntimeError('optional lookup unavailable')
    actual = await ChatProfileMemory(source, supplements=supplement).build_context('user-1')
    assert actual == await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')


@pytest.mark.asyncio
async def test_timeout_cancels_the_read_without_rendering_any_prefix():
    _, source = environment()

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    source.read_page = blocked
    with pytest.raises(ChatProfileReadError, match='time budget'):
        await ChatProfileMemory(source, timeout_seconds=0.01).build_context('user-1')


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), '5'])
def test_time_budget_cannot_be_disabled(value):
    with pytest.raises(ValueError):
        ChatProfileMemory(object(), timeout_seconds=value)


@pytest.mark.asyncio
async def test_unknown_dates_and_unfinished_observations_keep_original_order_and_eight_item_limit():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id='kept')]
    rows = [
        {
            'documentId': f'doc-{i}',
            'dateStatus': 'known' if i % 2 else 'unknown',
            'observedAt': None,
            'ingestionStatus': 'completed' if i < 2 else 'pending',
            'observation': {'itemName': f'item-{i}', 'resultValue': str(i), 'rawExtra': {'method': 'original'}},
        }
        for i in range(100)
    ]
    legacy = RepositoryHealthProfileChatMemoryProvider(
        old.repository,
        document_observation_store=SimpleNamespace(list_observations=AsyncMock(return_value={'items': rows})),
    )
    supplement = SimpleNamespace(
        read=AsyncMock(
            return_value=ChatSupplementPageV1.capture(user_id='user-1', subject_id='subject-self', items=rows)
        )
    )
    actual = await ChatProfileMemory(source, supplements=supplement).build_context('user-1')
    assert actual == await legacy.build_context('user-1')
    assert len([row for row in facts_in(actual) if row.get('source') == 'uploaded_document']) == 8
    supplement.read.assert_awaited_once_with(user_id='user-1', subject_id='subject-self', limit=100)


@pytest.mark.asyncio
async def test_shared_template_changes_do_not_change_chat_owned_context(monkeypatch):
    import health_profile.memory as shared

    old, source = environment()
    old.repository.facts = [old._fact(fact_id='kept')]
    context = await ChatProfileMemory(source).build_context('user-1')
    monkeypatch.setattr(shared, '_MEMORY_PREAMBLE', 'future shared template')
    assert await ChatProfileMemory(source).build_context('user-1') == context


def test_capture_detaches_mutable_fact_values_and_supplement_rows():
    old, _ = environment()
    value = {'rows': [1, 2]}
    captured = public_fact(old._fact(fact_id='f', value=value))
    value['rows'].append(3)
    escaped = captured.value
    escaped['rows'].append(4)
    assert captured.value == {'rows': [1, 2]}


@pytest.mark.asyncio
async def test_pending_or_applying_sources_do_not_hide_existing_authoritative_facts():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id='current-authoritative')]
    read = source.read_page

    async def pending(*args, **kwargs):
        return replace(await read(*args, **kwargs), stale=True)

    source.read_page = pending
    assert await ChatProfileMemory(source).build_context('user-1') == await RepositoryHealthProfileChatMemoryProvider(
        old.repository,
    ).build_context('user-1')


@pytest.mark.asyncio
async def test_optional_wrong_subject_observations_cannot_replace_complete_facts():
    old, source = environment()
    old.repository.facts = [old._fact(fact_id='kept')]
    supplement = SimpleNamespace(
        read=AsyncMock(
            return_value=ChatSupplementPageV1.capture(
                user_id='user-1',
                subject_id='another-subject',
                items=[],
            )
        )
    )
    assert await ChatProfileMemory(source, supplements=supplement).build_context('user-1') == (
        await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')
    )


@pytest.mark.asyncio
async def test_selected_oversized_facts_are_not_backfilled_with_optional_observations():
    from modules.chat import profile_context

    old, source = environment()
    old.repository.facts = [old._fact(fact_id='too-large-for-room')]
    row = {
        'documentId': 'd',
        'dateStatus': 'unknown',
        'observedAt': None,
        'ingestionStatus': 'pending',
        'observation': {'itemName': 'x', 'resultValue': '1'},
    }
    supplements = SimpleNamespace(
        read=AsyncMock(
            return_value=ChatSupplementPageV1.capture(
                user_id='user-1',
                subject_id='subject-self',
                items=[row],
            )
        )
    )
    maximum = len(profile_context._MEMORY_PREAMBLE) + 129
    assert (
        await ChatProfileMemory(
            source, renderer=ChatProfileRendererV1(max_chars=maximum), supplements=supplements
        ).build_context('user-1')
        is None
    )
