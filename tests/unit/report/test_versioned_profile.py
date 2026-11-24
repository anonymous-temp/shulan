"""Report renders public Profile data without shared prompt strings or side effects."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.report.versioned_profile import VersionedReportProfileGateway
from health_profile.contracts import ProfileFactSourceV1
from health_profile.contracts import ProfileFactV1
from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSnapshotV1
from health_profile.contracts import ProfileSubjectV1

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def subject(identity='self', kind='self'):
    return ProfileSubjectV1(subject_id=identity, kind=kind, status='active', version=1)


def snapshot(who=None, *, value=170, revision=3, status='ready', stale=False):
    who = who or subject()
    fact = ProfileFactV1(
        fact_id='fact-' + who.subject_id,
        fact_type='height',
        category='basic',
        display_name='height',
        value_type='number',
        value=value,
        assertion_level='user_reported',
        verification_status='unverified',
        confidence=1,
        effective_period={'start': NOW, 'end': None},
        status='active',
        version=1,
        created_at=NOW,
        updated_at=NOW,
        sources=(ProfileFactSourceV1(source_type='record', source_id='s', source_revision=1),),
    )
    return ProfileSnapshotV1(
        snapshot_id='snap-' + who.subject_id,
        user_id='u',
        read_at=NOW,
        scope=ProfileScope(user_id='u', subject_id=who.subject_id),
        status=status,
        profile_revision=revision,
        subject=who,
        facts=(fact,) if status == 'ready' else (),
        stale=stale,
    )


@pytest.mark.asyncio
async def test_public_snapshot_maps_self_and_relatives_preserving_values_and_source_types():
    subjects = (subject(), subject('mother', 'blood_relative'), subject('friend', 'other'))
    rows = {who.subject_id: snapshot(who) for who in subjects}
    reader = SimpleNamespace(
        list_subjects=AsyncMock(return_value=subjects),
        read=AsyncMock(side_effect=lambda user_id, **kw: rows[kw['subject_id']]),
    )
    result = await VersionedReportProfileGateway(reader).read('u')
    assert result.self_subject_id == 'self' and result.profile_version == 3 and not result.stale
    assert len(result.facts) == 2
    assert result.facts[0].value == 170 and result.facts[0].updated_at == NOW.isoformat().replace('+00:00', 'Z')
    assert result.facts[1].subject_kind == 'blood_relative' and result.facts[1].sources == ('record',)
    assert all(call.kwargs['subject_id'] != 'friend' for call in reader.read.call_args_list)


@pytest.mark.asyncio
async def test_read_failure_or_missing_profile_uses_existing_no_profile_fallback():
    reader = SimpleNamespace(list_subjects=AsyncMock(return_value=()), read=AsyncMock())
    assert await VersionedReportProfileGateway(reader).read('u') is None
    reader.read.assert_not_awaited()
    reader.list_subjects.return_value = (subject(),)
    reader.read.return_value = snapshot(status='unavailable')
    assert await VersionedReportProfileGateway(reader).read('u') is None


@pytest.mark.asyncio
async def test_cross_user_snapshot_is_rejected_and_never_rendered():
    wrong = snapshot().model_copy(update={'scope': ProfileScope(user_id='other', subject_id='self')})
    reader = SimpleNamespace(list_subjects=AsyncMock(return_value=(subject(),)), read=AsyncMock(return_value=wrong))
    assert await VersionedReportProfileGateway(reader).read('u') is None


@pytest.mark.asyncio
async def test_concurrent_changes_retry_once_then_mark_stale():
    reader = SimpleNamespace(
        list_subjects=AsyncMock(return_value=(subject(),)),
        read=AsyncMock(side_effect=[snapshot(revision=n) for n in (1, 2, 3, 4)]),
    )
    result = await VersionedReportProfileGateway(reader).read('u')
    assert result.profile_version == 3 and result.stale and reader.read.await_count == 4


@pytest.mark.asyncio
async def test_pending_publication_staleness_survives_stable_revision_check():
    row = snapshot(stale=True)
    reader = SimpleNamespace(list_subjects=AsyncMock(return_value=(subject(),)), read=AsyncMock(return_value=row))
    assert (await VersionedReportProfileGateway(reader).read('u')).stale


@pytest.mark.asyncio
async def test_global_fact_allowance_and_subject_order_are_preserved():
    first, second = subject('mother', 'blood_relative'), subject()
    full = snapshot(first).model_copy(update={'facts': tuple(snapshot(first).facts[0] for _ in range(300))})
    reader = SimpleNamespace(
        list_subjects=AsyncMock(return_value=(first, second)),
        read=AsyncMock(side_effect=lambda user_id, **kw: full if kw['subject_id'] == 'mother' else snapshot()),
    )
    result = await VersionedReportProfileGateway(reader).read('u')
    assert len(result.facts) == 300 and result.truncated
    assert all(item.subject_kind == 'blood_relative' for item in result.facts)
    assert result.self_subject_id == 'self'
