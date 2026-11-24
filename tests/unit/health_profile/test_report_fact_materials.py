"""Canonical Profile report facts only, including visible pending evidence."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.contracts import ProfileFactSourceV1
from health_profile.contracts import ProfileScope
from health_profile.document_archive_contracts import ProfileReportLifecycleConflict
from health_profile.report_fact_materials import ProfileReportFactsReader
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import personal_citations
from modules.health_analysis.factors import personal_quote_units


def fact(name, *, status='active', verification='verified_source', source_type='health_report'):
    return SimpleNamespace(
        fact_id=name,
        fact_type='laboratory.synthetic',
        value_type='quantity',
        value={'number': 4, 'raw': '<4', 'unit': 'U/L', 'comparator': '<'},
        display_name=name,
        interpretation=None,
        reference_range=None,
        status=status,
        verification_status=verification,
        version=1,
        effective_period={'start': datetime(2026, 11, 1, tzinfo=UTC) if status == 'active' else None},
        sources=(
            ProfileFactSourceV1(source_type=source_type, source_id='report-1', source_revision=1, pointer='/row'),
        ),
    )


def profiles(facts):
    scope = ProfileScope(user_id='u', subject_id='self')
    page = SimpleNamespace(
        status='ready',
        scope=scope,
        subject=SimpleNamespace(kind='self'),
        next_cursor=None,
        profile_revision=2,
        coverage={'revisionStable': True},
        stale=False,
        facts=facts,
    )
    return SimpleNamespace(
        resolve_existing_self_lifecycle=AsyncMock(return_value=('self', 1, 'active')),
        read_page=AsyncMock(return_value=page),
    )


@pytest.mark.asyncio
async def test_report_family_history_keeps_blood_relative_scope_and_report_provenance():
    history = fact('report-history')
    history.fact_type = 'family_history.reported'
    history.value_type = 'object'
    history.value = {'presence': 'present', 'description': 'Synthetic family history statement'}
    questionnaire = fact('questionnaire-history', source_type='questionnaire')
    questionnaire.fact_type = history.fact_type
    provider = profiles((history, questionnaire))

    result = await ProfileReportFactsReader(provider).read(user_id='u', subject_id='self', expected_lifecycle_epoch=1)

    assert 'family_history' in provider.read_page.call_args.kwargs['categories']
    (material,) = result['materials']
    assert material['id'] == 'report:report-history'
    assert material['backgroundScope'] == 'family_history'
    assert material['content']['observation'] == history.value
    assert material['metadata']['verificationStatus'] == 'verified_source'


@pytest.mark.asyncio
async def test_report_facts_keep_pending_visible_without_proving_personal_conditions():
    provider = profiles(
        (
            fact('checked'),
            fact('pending', status='pending', verification='unconfirmed'),
            fact('chat-source', source_type='chat'),
        )
    )
    result = await ProfileReportFactsReader(provider).read(user_id='u', subject_id='self', expected_lifecycle_epoch=1)
    checked, pending = result['materials']
    assert checked['content']['resultValue'] == '<4' and '4' not in personal_quote_units(checked)
    assert checked['metadata']['sources'][0]['pointer'] == '/row'
    assert pending['status'] == 'unknown' and pending['content']['measuredAt'] is None
    with pytest.raises(HealthAnalysisGenerationError):
        personal_citations([{'evidenceId': pending['id'], 'quote': '<4'}], {pending['id']: pending})
    provider.resolve_existing_self_lifecycle.return_value = ('self', 2, 'active')
    with pytest.raises(ProfileReportLifecycleConflict):
        await ProfileReportFactsReader(provider).read(user_id='u', subject_id='self', expected_lifecycle_epoch=1)


@pytest.mark.asyncio
async def test_report_facts_expose_recording_time_separately_and_remain_usable():
    report = fact('undated', verification='confirmed')
    at = datetime(2026, 11, 12, tzinfo=UTC)
    report.effective_period['start'] = None
    report.sources = (
        ProfileFactSourceV1(
            source_type='health_report',
            source_id='report-1',
            source_revision=1,
            pointer='/row',
            confirmation_basis='analysis_public',
            recorded_at=at,
        ),
    )
    provider = profiles((report, fact('dated')))
    result = await ProfileReportFactsReader(provider).read(user_id='u', subject_id='self', expected_lifecycle_epoch=1)
    undated, dated = result['materials']
    assert undated['content']['measuredAt'] is None
    assert undated['content']['recordedAt'] == at.isoformat()
    assert 'status' not in undated
    assert undated['metadata']['sources'][0]['confirmationBasis'] == 'analysis_public'
    assert dated['content']['measuredAt']
    assert 'recordedAt' not in dated['content']
    assert 'recordedAt' not in dated['metadata']['sources'][0]
