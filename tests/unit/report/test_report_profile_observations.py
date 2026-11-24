"""Report keeps uncertain Profile observations out of typed and confirmed inputs."""

from dataclasses import replace

from modules.report.evaluation_request_input import prepare_evaluation_request
from modules.report.ports import ReportProfileFact
from modules.report.ports import ReportProfileSnapshot
from modules.report.profile_snapshot import profile_context_partitions
from modules.report.profile_snapshot import profile_indicator_facts
from modules.report.schemas import ReportRequest


def test_profile_lab_results_are_history_and_pending_report_dates_stay_unknown():
    checked = ReportProfileFact(
        fact_id='one',
        subject_id='self',
        subject_kind='self',
        fact_type='laboratory.alt',
        display_name='ALT',
        value={'number': 4, 'unit': 'U/L', 'raw': '<4', 'comparator': '<'},
        value_type='quantity',
        assertion_level='observed',
        verification_status='verified_source',
        status='active',
        effective_start='2026-11-01',
        sources=('health_report',),
    )
    pending = replace(
        checked,
        fact_id='two',
        fact_type='anthropometric.height',
        display_name='height',
        value={'number': 150, 'unit': 'cm'},
        verification_status='unconfirmed',
        status='pending',
        effective_start=None,
        updated_at='2026-11-15',
    )
    other = replace(checked, fact_id='three', subject_id='other', subject_kind='other', display_name='foreign')
    snapshot = ReportProfileSnapshot(
        user_id='synthetic',
        self_subject_id='self',
        read_at='2026-11-15',
        profile_version=1,
        facts=(checked, pending, other),
    )
    assert not profile_indicator_facts(snapshot)
    partitions = profile_context_partitions(snapshot)
    assert len(partitions.background) == 1 and 'ALT <4 U/L' in partitions.background[0]
    assert len(partitions.unconfirmed) == 1 and 'height' in partitions.unconfirmed[0]
    assert '2026-11-15' not in partitions.unconfirmed[0]


def test_pending_report_diagnosis_cannot_become_a_questionnaire_answer():
    pending = ReportProfileFact(
        fact_id='unverified-report',
        subject_id='self',
        subject_kind='self',
        fact_type='condition.diagnosis',
        display_name='Imaging diagnosis',
        value='Needs confirmation',
        value_type='text',
        assertion_level='user_reported',
        verification_status='unconfirmed',
        status='pending',
        sources=('health_report',),
    )
    questionnaire = replace(pending, fact_id='reported-background', sources=('questionnaire',))
    snapshot = ReportProfileSnapshot(
        user_id='synthetic',
        self_subject_id='self',
        read_at='2026-11-15',
        profile_version=1,
        facts=(pending, questionnaire),
    )
    request = ReportRequest(user_id='synthetic', session_id='session')
    result = prepare_evaluation_request(request, snapshot)
    assert [row['questionId'] for row in result.model_extra['healthSurvey']['answers']] == ['reported-background']
    assert any('Imaging diagnosis' in item for item in profile_context_partitions(snapshot).unconfirmed)
