from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.report.factory import load_report_session
from health_profile.ingestion import HealthFactPolicy
from health_profile.ingestion import PolicyDisposition
from health_profile.models import ProfileIngestionSourceType
from health_profile.models import SubjectKind
from health_profile.questionnaire_source import QuestionnaireProfileSourceService
from health_profile.record_ingestion import RecordAnswerCandidateExtractor
from health_profile.record_source import RecordProfileSourceRecord
from modules.questionnaire.context import answer_rows
from modules.questionnaire.context import submitted_context
from modules.questionnaire.schemas import normalize_answers
from tests.unit.health_profile.test_record_ingestion import _runtime


def snapshot():
    questionnaire = {
        'title': '测试',
        'pages': [
            {
                'name': 'p',
                'elements': [
                    {
                        'name': 'medical_history',
                        'type': 'checkbox',
                        'title': '医生确诊过哪些疾病？',
                        'choices': [
                            {'value': 'diabetes', 'text': '糖尿病'},
                            {'value': 'hypertension', 'text': '高血压'},
                        ],
                    },
                    {'name': 'habit', 'type': 'text', 'title': '日常习惯'},
                ],
            }
        ],
    }
    rows, canonical = normalize_answers(questionnaire, {'medical_history': ['diabetes'], 'habit': None})
    return {
        'questionnaireId': 'questionnaire-1',
        'userId': 'user-1',
        'sessionId': 'session-1',
        'status': 'submitted',
        'sourceRevision': 1,
        'submittedAt': 1791000000,
        'answers': rows,
        'canonicalAnswers': canonical,
        'profileIngestion': {'status': 'pending'},
        'guidance': {'risk': 'AI生成的风险不能提取'},
        'reports': [
            {
                'interpretation': {
                    'aiSummary': 'AI推断不进入事实',
                    'observations': [
                        {'itemName': '血糖', 'resultValue': '6.0', 'unit': 'mmol/L', 'observedAt': '2026-11-04'},
                    ],
                }
            }
        ],
    }


def test_submission_projection_keeps_labels_and_default_unknown_separate():
    data = snapshot()
    before = deepcopy(data)
    rows = answer_rows(data)
    assert len(rows) == 1
    assert rows[0]['answer'] == '糖尿病'
    assert rows[0]['sourcePointer'] == '/answers/medical_history/value'
    context = submitted_context(data)
    assert context['unansweredQuestions'] == ['habit']
    assert context['healthData']['hospitalData'][0]['itemName'] == '血糖'
    assert 'AI' not in str(context)
    assert data == before
    data['status'] = 'ready'
    assert answer_rows(data) == []
    assert submitted_context(data) == {}


@pytest.mark.asyncio
async def test_questionnaire_real_extractor_uses_labels_and_new_source_governance():
    data = snapshot()
    runtime, gateway = _runtime(
        {
            'candidates': [
                {
                    'answerIndex': 0,
                    'factType': 'condition.diagnosis',
                    'displayName': '糖尿病',
                    'valueType': 'text',
                    'value': '糖尿病',
                    'subjectKind': 'self',
                    'subjectHint': '本人',
                    'evidenceText': '糖尿病',
                    'confidence': 0.9,
                }
            ]
        }
    )
    source = RecordProfileSourceRecord('user-1', 'questionnaire-1', 1, 1, 'queued', None, {})
    candidates = await RecordAnswerCandidateExtractor(
        runtime.hub, source_type=ProfileIngestionSourceType.QUESTIONNAIRE
    ).extract_answer_rows(
        source,
        answer_rows(data),
        subject_id='self-1',
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source.source_type == 'questionnaire'
    assert candidate.source.source_id == 'questionnaire-1'
    assert candidate.evidence_text == '糖尿病'
    assert (
        HealthFactPolicy().evaluate(candidate, subject_kind=SubjectKind.SELF).disposition is PolicyDisposition.PENDING
    )
    sent = gateway.complete_requests[0].content
    assert 'diabetes' not in sent and 'AI生成' not in sent and 'habit' not in sent


@pytest.mark.asyncio
async def test_empty_submission_never_invokes_extraction_model():
    runtime, gateway = _runtime({'candidates': []})
    source = RecordProfileSourceRecord('user-1', 'q-empty', 1, 1, 'queued', None, {})
    result = await RecordAnswerCandidateExtractor(
        runtime.hub, source_type=ProfileIngestionSourceType.QUESTIONNAIRE
    ).extract_answer_rows(
        source,
        [],
        subject_id='self-1',
    )
    assert result == () and gateway.complete_requests == []


@pytest.mark.asyncio
async def test_profile_delivery_retries_saved_source_after_enqueue_failure():
    data = snapshot()
    store = SimpleNamespace(
        get_submitted=AsyncMock(return_value=data),
        mark_ingestion_queued=AsyncMock(return_value=True),
        list_pending_ingestion=AsyncMock(return_value=[data]),
    )
    queue = SimpleNamespace(
        enqueue=AsyncMock(side_effect=[RuntimeError('offline'), SimpleNamespace(job=SimpleNamespace(job_id='job-1'))])
    )
    service = QuestionnaireProfileSourceService(store, queue)
    assert await service.ensure_queued(user_id='user-1', questionnaire_id='questionnaire-1') is None
    store.mark_ingestion_queued.assert_not_called()
    assert await service.recover_pending() == 1
    assert queue.enqueue.call_args.kwargs['source_type'] is ProfileIngestionSourceType.QUESTIONNAIRE
    store.mark_ingestion_queued.assert_awaited_once_with('user-1', 'questionnaire-1', 'job-1')


@pytest.mark.asyncio
@pytest.mark.parametrize('submitted', [False, True])
async def test_report_adapter_reads_only_submitted_questionnaire(submitted):
    context = submitted_context(snapshot()) if submitted else None
    service = SimpleNamespace(
        get_session_context=AsyncMock(return_value=context), hasPendingSession=AsyncMock(return_value=not submitted)
    )
    redis = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('must not read old cache')))
    mongo = SimpleNamespace(find_one=AsyncMock(side_effect=AssertionError('must not fall back to draft owner')))
    result = await load_report_session(
        'user-1', 'session-1', redis_client=redis, session_collection=mongo, questionnaire_service=service
    )
    assert result == context


def test_report_context_fills_typed_hospital_data_without_profile_or_redownload():
    from modules.report.pipeline import merge_request_session_fallbacks
    from modules.report.schemas import ReportRequest

    data = snapshot()
    data['reports'][0]['interpretation']['sections'] = []
    context = submitted_context(data)
    request = ReportRequest(userId='user-1', sessionId='session-1')
    merged = merge_request_session_fallbacks(request, context)
    assert len(merged.hospital_data) == 1
    assert context['answers'][0]['answeredAt'] == '2026-10-03T04:00:00+00:00'
    from modules.report.pipeline import extract_session_context

    assert extract_session_context(context)['answers'][0]['answeredAt'] == '2026-10-03T04:00:00+00:00'
    assert merged.hospital_data[0].data_type == 'INSPECTION'
    assert merged.hospital_data[0].test_result_value == '6.0 mmol/L'
    assert merged.hospital_data[0].report_time == '2026-11-04'
    assert merged.file_list == []
    explicit = request.model_copy(update={'hospital_data': merged.hospital_data})
    assert merge_request_session_fallbacks(explicit, context).hospital_data == explicit.hospital_data


def test_imaging_snapshot_preserves_source_findings_without_ai_summary():
    data = snapshot()
    data['reports'] = [
        {
            'artifactId': 'a-imaging',
            'interpretation': {
                'reportType': 'imaging',
                'reportTime': '2026-11-01',
                'sections': [],
                'observations': [],
                'expansion': {'findings': '胆囊壁毛糙', 'diagnosis': '建议随访'},
                'aiSummary': '不可作为原文',
            },
        }
    ]
    rows = submitted_context(data)['healthData']['hospitalData']
    assert len(rows) == 2
    assert all(row['dataType'] == 'EXAM' and row['reportTime'] == '2026-11-01' for row in rows)
    assert rows[0]['examResult'] == '胆囊壁毛糙'
    assert '不可作为原文' not in str(rows)


@pytest.mark.asyncio
async def test_independent_processor_publishes_reports_once_and_skips_existing_document_references():
    from health_profile.questionnaire_ingestion import QuestionnaireProfileProcessor
    from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
    from tests.unit.health_profile.test_record_ingestion import RecordFixture
    from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
    from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture

    data = snapshot()
    data['answers'] = {}
    data['artifacts'] = [
        {'artifactId': 'url-1', 'contentSha256': 'sha-same', 'kind': 'questionnaire_document'},
        {'artifactId': 'url-2', 'contentSha256': 'sha-same', 'kind': 'questionnaire_document'},
        {'artifactId': 'existing', 'contentSha256': 'sha-old', 'kind': 'document_reference'},
    ]
    data['reports'] = [{'artifactId': item['artifactId'], 'interpretation': {}} for item in data['artifacts']]
    subject = ReportIngestionFixture.subject()
    repository = FakeProfileRepository(subject)
    profile = FakeRecordProfileService(subject, repository=repository)
    source = SimpleNamespace(save=AsyncMock())
    documents = SimpleNamespace(read_artifact=AsyncMock(return_value={'observations': []}))
    extractor = SimpleNamespace(VERSION='test', extract_answer_rows=AsyncMock(return_value=()))
    processor = QuestionnaireProfileProcessor(
        store=SimpleNamespace(get_submitted=AsyncMock(return_value=data)),
        repository=repository,
        profile_service=profile,
        agent_hub=None,
        documents=documents,
        report_source=source,
        answer_extractor=extractor,
    )
    job = RecordFixture.job().model_copy(
        update={'source_type': ProfileIngestionSourceType.QUESTIONNAIRE, 'source_id': 'questionnaire-1'}
    )
    result = await processor.process(job)
    assert result.fact_count == 0
    assert documents.read_artifact.await_count == 1
    assert source.save.await_count == 1
    assert extractor.extract_answer_rows.call_args.args[1] == []
    first_id = source.save.call_args.kwargs['report_id']
    await processor.process(job)
    assert source.save.call_args.kwargs['report_id'] == first_id
