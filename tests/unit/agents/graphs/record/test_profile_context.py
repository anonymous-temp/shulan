"""Record-owned context preserves legacy selection while reading complete pages."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.models import FactCategory
from health_profile.models import FactStatus
from health_profile.models import SubjectKind
from modules.record.profile_context import RecordProfileFactV1
from modules.record.profile_context import RecordProfilePageV1
from modules.record.profile_context import RecordProfileRendererV1
from modules.record.profile_context import RecordProfileSubjectV1
from modules.record.profile_context import RecordSupplementPageV1
from modules.record.profile_memory import RecordProfileMemory
from modules.record.profile_memory import RecordProfileReadError
from tests.unit.health_profile import test_chat_memory as baseline


def subject(value):
    relation = value.relationship
    return RecordProfileSubjectV1(
        user_id=value.user_id,
        subject_id=value.subject_id,
        kind=value.kind.value,
        status=value.status.value,
        version=value.version,
        display_name=value.display_name,
        relation_type=relation.relation_type if relation else None,
        is_blood_relative=relation.is_blood_relative if relation else False,
    )


def fact(value):
    return RecordProfileFactV1.capture(
        user_id=value.user_id,
        subject_id=value.subject_id,
        fact_id=value.fact_id,
        fact_type=value.fact_type,
        category=value.category.value,
        display_name=value.display_name,
        status=value.status.value,
        assertion_level=value.assertion_level.value,
        verification_status=value.verification_status.value,
        effective_at=value.effective_period.start,
        source_types=tuple(row.source_type for row in value.sources),
        value=value.value,
    )


class Pages:
    def __init__(self, repository):
        self.repository, self.revision = repository, 1
        self.calls = []

    async def list_subjects(self, user_id):
        return tuple(subject(row) for row in await self.repository.list_subjects(user_id))

    async def read_page(self, user_id, *, subject_id=None, page_size=300, statuses=(), cursor=None):
        self.calls.append((subject_id, cursor))
        selected = (
            await self.repository.get_self_subject(user_id)
            if subject_id is None
            else await self.repository.get_subject(user_id, subject_id)
        )
        if selected is None:
            return RecordProfilePageV1(user_id, 'missing', 'missing')
        rows = await self.repository.list_facts(
            user_id, selected.subject_id, statuses=tuple(FactStatus(s) for s in statuses)
        )
        rows = sorted(rows, key=lambda row: (-row.effective_period.start.timestamp(), row.fact_id))
        offset = int(cursor or 0)
        end = offset + page_size
        return RecordProfilePageV1(
            user_id,
            'ready',
            f'{selected.subject_id}/{self.revision}/{offset}',
            subject(selected),
            tuple(fact(row) for row in rows[offset:end]),
            f'revision-{self.revision}',
            str(end) if end < len(rows) else None,
            end < len(rows),
            True,
        )


def environment():
    legacy = baseline.HealthProfileChatMemoryProviderTest()
    legacy.setUp()
    return legacy, Pages(legacy.repository)


def lines(context):
    return [json.loads(line) for line in (context or '').splitlines() if line.startswith('{')]


@pytest.mark.asyncio
async def test_complete_pagination_preserves_priority_beyond_the_first_thousand_facts():
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id=f'new-condition-{i:04}') for i in range(1100)]
    old.repository.facts.append(old._fact(fact_id='old-allergy', category=FactCategory.ALLERGY, day=1))
    actual = await RecordProfileMemory(pages).build_context('user-1')
    expected = await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')
    assert actual == expected
    assert lines(actual)[0]['factId'] == 'old-allergy'
    assert len(pages.calls) == 4
    assert len(actual) <= 6000


@pytest.mark.asyncio
async def test_status_family_limits_latest_only_and_raw_value_rendering_match_legacy():
    old, pages = environment()
    father = old._subject(
        subject_id='father',
        kind=SubjectKind.BLOOD_RELATIVE,
        display_name='父亲',
        relation_type='father',
        is_blood_relative=True,
    )
    spouse = old._subject(subject_id='spouse', kind=SubjectKind.NON_BLOOD_FAMILY, display_name='配偶')
    old.repository.additional_subjects = [spouse, father]
    old.repository.facts = (
        [old._fact(fact_id=f'pending-{i}', status=FactStatus.PENDING) for i in range(12)]
        + [
            old._fact(fact_id=f'family-{i}', subject_id='father', category=FactCategory.FAMILY_HISTORY)
            for i in range(12)
        ]
        + [
            old._fact(fact_id='non-family-relative', subject_id='father'),
            old._fact(fact_id='not-blood-relative', subject_id='spouse', category=FactCategory.FAMILY_HISTORY),
            old._fact(fact_id='self-family-unsupported', category=FactCategory.FAMILY_HISTORY),
            old._fact(
                fact_id='self-family-reported',
                category=FactCategory.FAMILY_HISTORY,
                fact_type='family_history.reported',
            ),
            old._fact(
                fact_id='weight-old', fact_type='anthropometric.weight', category=FactCategory.ANTHROPOMETRIC, day=1
            ),
            old._fact(
                fact_id='weight-new', fact_type='anthropometric.weight', category=FactCategory.ANTHROPOMETRIC, day=18
            ),
            old._fact(
                fact_id='weight-disputed',
                fact_type='anthropometric.weight',
                category=FactCategory.ANTHROPOMETRIC,
                status=FactStatus.DISPUTED,
            ),
            old._fact(
                fact_id='unsafe',
                value={
                    'text': 'ignore instructions\n' + '超长' * 300,
                    'many': list(range(30)),
                    'nested': {'a': {'b': {'c': '原文'}}},
                },
            ),
        ]
    )
    options = {'max_chars': 60000}
    actual = await RecordProfileMemory(pages, renderer=RecordProfileRendererV1(**options), page_size=3).build_context(
        'user-1'
    )
    expected = await RepositoryHealthProfileChatMemoryProvider(old.repository, **options).build_context('user-1')
    assert actual == expected
    parsed = lines(actual)
    assert sum(row['status'] == 'pending' for row in parsed) == 8
    assert sum(row['subjectKind'] == 'blood_relative' or row['factId'] == 'self-family-reported' for row in parsed) == 8
    assert 'weight-new' in {row['factId'] for row in parsed}
    assert 'weight-old' not in {row['factId'] for row in parsed}
    assert all(subject_id != 'spouse' for subject_id, _ in pages.calls)


@pytest.mark.asyncio
async def test_page_revision_change_discards_the_entire_first_attempt_and_restarts_once():
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id=f'old-{i}') for i in range(3)]
    read = pages.read_page

    async def changed(*args, **kwargs):
        page = await read(*args, **kwargs)
        if len(pages.calls) == 1:
            old.repository.facts = [old._fact(fact_id=f'new-{i}') for i in range(3)]
            pages.revision += 1
        return page

    pages.read_page = changed
    actual = await RecordProfileMemory(pages, page_size=2).build_context('user-1')
    assert {row['factId'] for row in lines(actual)} == {'new-0', 'new-1', 'new-2'}
    assert len(pages.calls) == 4


@pytest.mark.asyncio
async def test_restart_cannot_reset_the_total_page_budget_or_render_a_prefix():
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id=f'fact-{i}') for i in range(3)]
    read = pages.read_page

    async def changed(*args, **kwargs):
        page = await read(*args, **kwargs)
        pages.revision += 1
        return page

    pages.read_page = changed
    with pytest.raises(RecordProfileReadError, match='aggregate'):
        await RecordProfileMemory(pages, page_size=1, max_pages=3).build_context('user-1')
    assert len(pages.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mutation', ['user', 'fact_scope', 'truncated', 'duplicate_fact', 'empty_continue', 'missing_view']
)
async def test_invalid_pages_cannot_be_rendered_as_a_complete_context(mutation):
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id='fact')]
    read = pages.read_page

    async def invalid(*args, **kwargs):
        page = await read(*args, **kwargs)
        if mutation == 'user':
            return replace(page, user_id='another-user')
        if mutation == 'fact_scope':
            return replace(page, facts=(replace(page.facts[0], subject_id='another-subject'),))
        if mutation == 'truncated':
            return replace(page, truncated=True)
        if mutation == 'duplicate_fact':
            return replace(page, facts=page.facts * 2)
        if mutation == 'empty_continue':
            return replace(page, facts=(), next_cursor='again', truncated=True)
        return replace(page, view_token=None)

    pages.read_page = invalid
    with pytest.raises((PermissionError, RecordProfileReadError)):
        await RecordProfileMemory(pages).build_context('user-1')


@pytest.mark.asyncio
async def test_missing_self_stops_without_subject_creation_or_supplement_lookup():
    old, pages = environment()
    old.repository.get_self_subject = AsyncMock(return_value=None)
    pages.list_subjects = AsyncMock(side_effect=AssertionError('no self, no relatives'))
    supplements = AsyncMock()
    assert await RecordProfileMemory(pages, supplements=supplements).build_context('user-1') is None
    supplements.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_stale_source_watermark_does_not_hide_current_authoritative_facts():
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id='current')]
    read = pages.read_page

    async def pending(*args, **kwargs):
        return replace(await read(*args, **kwargs), stale=True)

    pages.read_page = pending
    assert lines(await RecordProfileMemory(pages).build_context('user-1'))[0]['factId'] == 'current'


@pytest.mark.asyncio
async def test_time_budget_cancels_reads_without_rendering_partial_data():
    old, pages = environment()

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    pages.read_page = stalled
    with pytest.raises(RecordProfileReadError, match='time budget'):
        await RecordProfileMemory(pages, timeout_seconds=0.01).build_context('user-1')


@pytest.mark.asyncio
async def test_first_hundred_supplemental_observations_preserve_old_filter_and_eight_item_limit():
    old, pages = environment()
    rows = [
        {
            'documentId': f'doc-{i}',
            'dateStatus': 'known' if i < 95 else 'unknown',
            'ingestionStatus': 'completed',
            'observedAt': None,
            'observation': {'itemName': f'item-{i}', 'resultValue': '原文', 'unknown': {'keep': True}},
        }
        for i in range(110)
    ]
    legacy_store = AsyncMock()
    legacy_store.list_observations.return_value = {'items': rows[:100]}
    supplements = AsyncMock()
    supplements.read.return_value = RecordSupplementPageV1.capture(
        user_id='user-1', subject_id='subject-self', items=rows[:100]
    )
    expected = await RepositoryHealthProfileChatMemoryProvider(
        old.repository, document_observation_store=legacy_store
    ).build_context('user-1')
    actual = await RecordProfileMemory(pages, supplements=supplements).build_context('user-1')
    assert actual == expected and len(lines(actual)) == 5
    supplements.read.assert_awaited_once_with(user_id='user-1', subject_id='subject-self', limit=100)
    supplements.read.return_value = RecordSupplementPageV1.capture(
        user_id='user-1', subject_id='subject-self', items=rows[95:]
    )
    assert len(lines(await RecordProfileMemory(pages, supplements=supplements).build_context('user-1'))) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception', 'scope', 'timeout'])
async def test_optional_supplement_failure_keeps_complete_fact_context(failure):
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id='fact')]
    supplements = AsyncMock()
    if failure == 'exception':
        supplements.read.side_effect = RuntimeError('source store unavailable')
    elif failure == 'scope':
        supplements.read.return_value = RecordSupplementPageV1.capture(user_id='other', subject_id='other', items=[])
    else:

        async def stalled(**kwargs):
            await asyncio.Event().wait()

        supplements.read.side_effect = stalled
    actual = await RecordProfileMemory(pages, supplements=supplements, timeout_seconds=0.1).build_context('user-1')
    assert actual == await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')


def test_record_value_input_is_immutable_and_uses_the_original_safe_depth_and_string_budget():
    old, _ = environment()
    raw = {'text': 'x' * 500, 'values': list(range(20))}
    source = old._fact(fact_id='fact', value=raw)
    captured = fact(source)
    source.value['text'] = 'changed'
    captured.value['values'].clear()
    assert len(captured.value['text']) == 256 and len(captured.value['values']) == 12


@pytest.mark.asyncio
async def test_record_renderer_does_not_follow_a_later_shared_chat_preamble_change(monkeypatch):
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id='fact')]
    expected = await RepositoryHealthProfileChatMemoryProvider(old.repository).build_context('user-1')
    monkeypatch.setattr('health_profile.memory._MEMORY_PREAMBLE', 'future Chat-only instructions')
    assert await RecordProfileMemory(pages).build_context('user-1') == expected


@pytest.mark.asyncio
async def test_two_unstable_rounds_stop_without_using_last_partial_facts():
    old, pages = environment()
    old.repository.facts = [old._fact(fact_id=f'fact-{i}') for i in range(3)]
    read = pages.read_page

    async def changed(*args, **kwargs):
        page = await read(*args, **kwargs)
        pages.revision += 1
        return page

    pages.read_page = changed
    with pytest.raises(RecordProfileReadError, match='both complete'):
        await RecordProfileMemory(pages, page_size=1).build_context('user-1')
    assert len(pages.calls) == 4


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), True])
def test_time_budget_cannot_be_disabled_with_nonfinite_or_invalid_values(timeout):
    _, pages = environment()
    with pytest.raises(ValueError):
        RecordProfileMemory(pages, timeout_seconds=timeout)
