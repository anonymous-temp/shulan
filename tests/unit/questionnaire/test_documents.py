"""Prove staging stays outside shared publication and retains exact source artifacts."""

import hashlib
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.questionnaire_documents import MongoOwnedQuestionnaireDocumentReader
from adapters.questionnaire_documents import MongoQuestionnaireArchive
from adapters.questionnaire_documents import QuestionnaireDocumentStager
from tests.unit._support.current_document import actual_analysis_preparation
from tests.unit._support.current_document import bson_collection
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document import no_value
from tests.unit._support.current_document import report
from tests.unit.services.analysis.fakes import InMemoryDocumentTraceStore

CONTENT = b'%PDF-1.7 synthetic archived source'


class Archive(InMemoryDocumentTraceStore):
    async def get(self, *, user_id, artifact_id):
        result = self.documents.get(artifact_id)
        return deepcopy(result) if result and result['userId'] == user_id else None

    async def write_payload(self, trace_id, user_id, name, payload):
        await super().write_payload(trace_id, user_id, name, payload)
        self.documents[trace_id].setdefault('payloads', {})[name] = {'stored': True}
        self.documents[trace_id].setdefault('writeCounts', {}).setdefault(name, 0)
        self.documents[trace_id]['writeCounts'][name] += 1


class Interpreter:
    def __init__(self, *, fail_analyze=False, ambiguous=False):
        self.calls = []
        self.fail_analyze = fail_analyze
        self.ambiguous = ambiguous

    async def extract_document(self, **kwargs):
        self.calls.append(('parse', kwargs['content']))
        trace = kwargs['trace']
        await trace.write_payload('original_file', {'content': kwargs['content']})
        await trace.update(stage='submitting')
        if self.ambiguous:
            raise RuntimeError('upstream acceptance unknown')
        await trace.update(stage='polling', taskId='task1')
        trace.parsed_document = {'success': True, 'text': 'verified source'}
        await trace.write_payload('docmind_result', trace.parsed_document)
        return 'verified source'

    async def analyze(self, text, **kwargs):
        self.calls.append(('analyze', text))
        assert kwargs['subject_id'] is None
        if self.fail_analyze:
            raise RuntimeError('model temporarily unavailable')
        return {'observations': [{'itemName': '血糖', 'resultValue': '6.4', 'unit': 'mmol/L'}]}


@pytest.mark.asyncio
async def test_url_staging_archives_bytes_and_parse_without_any_profile_dependency():
    archive, interpreter = Archive(), Interpreter()
    downloader = SimpleNamespace(download=AsyncMock(return_value=CONTENT))
    stage = QuestionnaireDocumentStager(interpreter=interpreter, archive=archive, downloader=downloader)
    args = dict(user_id='u', questionnaire_id='q', file_list=[{'url': 'https://example.org/report.pdf'}])
    result = await stage.stage(**args)
    artifact = result['artifacts'][0]
    assert artifact['contentSha256'] == hashlib.sha256(CONTENT).hexdigest()
    assert archive.payloads[(artifact['artifactId'], 'original_file')]['content'] == CONTENT
    assert archive.payloads[(artifact['artifactId'], 'docmind_result')]['text'] == 'verified source'
    assert result['reports'][0]['status'] == 'complete'
    again = await stage.stage(**args)
    assert again == result
    assert downloader.download.await_count == 1
    assert len(interpreter.calls) == 2
    assert archive.documents[artifact['artifactId']]['subjectId'] is None
    assert archive.documents[artifact['artifactId']]['writeCounts']['original_file'] == 1


@pytest.mark.asyncio
async def test_retry_uses_frozen_bytes_and_parser_result_not_changed_url():
    archive, interpreter = Archive(), Interpreter(fail_analyze=True)
    downloader = SimpleNamespace(download=AsyncMock(return_value=CONTENT))
    stage = QuestionnaireDocumentStager(interpreter=interpreter, archive=archive, downloader=downloader)
    args = dict(user_id='u', questionnaire_id='q', file_list=[{'url': 'https://example.org/r.pdf'}])
    assert (await stage.stage(**args))['reports'][0]['status'] == 'unavailable'
    downloader.download.return_value = b'%PDF different body'
    interpreter.fail_analyze = False
    result = await stage.stage(**args)
    assert result['reports'][0]['status'] == 'complete'
    assert downloader.download.await_count == 1
    assert [call for call in interpreter.calls if call[0] == 'parse'] == [('parse', CONTENT)]


@pytest.mark.asyncio
async def test_unknown_upstream_submission_is_not_reissued_on_retry():
    interpreter = Interpreter(ambiguous=True)
    stage = QuestionnaireDocumentStager(
        interpreter=interpreter, archive=Archive(), downloader=SimpleNamespace(download=AsyncMock(return_value=CONTENT))
    )
    args = dict(user_id='u', questionnaire_id='q', file_list=[{'url': 'https://example.org/r.pdf'}])
    await stage.stage(**args)
    result = await stage.stage(**args)
    assert result['reports'][0]['errorCode'] == 'submission_unknown'
    assert len(interpreter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'file_list',
    [
        [{'content': 'base64', 'fileName': 'x.pdf'}],
        [{'url': 'https://example.org/r.pdf', 'documentId': 'd'}],
        [{'documentId': 'd', 'fileType': 'pdf'}],
    ],
)
async def test_rejects_local_or_ambiguous_inputs(file_list):
    stage = QuestionnaireDocumentStager(interpreter=Interpreter(), archive=Archive())
    with pytest.raises(ValueError):
        await stage.stage(user_id='u', questionnaire_id='q', file_list=file_list)


@pytest.mark.asyncio
async def test_document_reference_reads_only_owner_scoped_archive_and_checks_subject():
    index = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                '_id': 'd',
                'userId': 'u',
                'subjectId': 's',
                'contentSha256': hashlib.sha256(CONTENT).hexdigest(),
                'byteSize': len(CONTENT),
                'fileName': 'r.pdf',
                'fileType': 'pdf',
                'payloadRefs': {'original_file': 'trace'},
            }
        )
    )
    subjects = SimpleNamespace(find_one=AsyncMock(return_value={'subjectId': 's'}))
    traces = SimpleNamespace(read_payload=AsyncMock(return_value={'content': CONTENT}))
    reader = MongoOwnedQuestionnaireDocumentReader(document_index=index, document_traces=traces, subjects=subjects)
    result = await reader.read(user_id='u', document_id='d')
    assert result['content'] == CONTENT
    index.find_one.assert_awaited_once_with({'_id': 'd', 'userId': 'u'})
    traces.read_payload.assert_awaited_once_with('trace', 'u', 's', 'original_file')
    subjects.find_one.return_value = None
    with pytest.raises(LookupError):
        await reader.read(user_id='u', document_id='d')
    assert traces.read_payload.await_count == 1


class Collection:
    def __init__(self, documents=()):
        self.documents = {item['_id']: deepcopy(item) for item in documents}

    @staticmethod
    def match(row, query):
        for key, wanted in query.items():
            value = row.get(key)
            if isinstance(wanted, dict):
                for op, target in wanted.items():
                    if op == '$exists' and (key in row) != target:
                        return False
                    if op == '$ne' and value == target:
                        return False
                    if op == '$lte' and (value is None or value > target):
                        return False
            elif value != wanted:
                return False
        return True

    async def find_one(self, query):
        return next((deepcopy(item) for item in self.documents.values() if self.match(item, query)), None)

    def find(self, query, projection=None):
        async def to_list(length):
            return [deepcopy(item) for item in self.documents.values() if self.match(item, query)][:length]

        return SimpleNamespace(to_list=to_list)

    async def update_one(self, query, update):
        for item in self.documents.values():
            if self.match(item, query):
                item.update(deepcopy(update.get('$set', {})))
                for key in update.get('$unset', {}):
                    item.pop(key, None)
                return SimpleNamespace(acknowledged=True, matched_count=1)
        return SimpleNamespace(acknowledged=True, matched_count=0)

    async def delete_one(self, query):
        for key, item in list(self.documents.items()):
            if self.match(item, query):
                del self.documents[key]

    async def delete_many(self, query):
        for key, item in list(self.documents.items()):
            if self.match(item, query):
                del self.documents[key]


@pytest.mark.asyncio
async def test_ready_ttl_pin_and_expiry_keep_submitted_originals_and_remove_unsubmitted_chunks():
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    rows = [
        {'_id': key, 'userId': 'u', 'questionnaireId': key, 'expiresAt': now, 'retained': False}
        for key in ('submitted', 'abandoned')
    ]
    rows[1]['expiresAt'] = now + timedelta(hours=1)
    metadata = Collection(rows)
    chunks = Collection([{'_id': key, 'traceId': key, 'userId': 'u'} for key in ('submitted', 'abandoned')])
    archive = MongoQuestionnaireArchive(metadata, chunks)
    ready_expiry = now + timedelta(hours=2)
    await archive.ready(user_id='u', questionnaire_id='submitted', artifact_ids=['submitted'], expires_at=ready_expiry)
    assert metadata.documents['submitted']['expiresAt'] == ready_expiry
    await archive.pin(user_id='u', questionnaire_id='submitted', artifact_ids=['submitted'])
    await archive.expire(user_id='u', questionnaire_id='abandoned', now=now)
    await archive.expire(user_id='u', questionnaire_id='submitted', now=now + timedelta(days=1))
    assert list(metadata.documents) == ['submitted']
    assert list(chunks.documents) == ['submitted']
    with pytest.raises(LookupError):
        await archive.pin(user_id='other', questionnaire_id='submitted', artifact_ids=['submitted'])


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('性别：男 年龄：45', ['male']),
        ('Gender: Female\nAge: 42', ['female']),
        ('姓名：王女士 妇科检查', []),
        ('性别：男\n性别：女', ['male', 'female']),
    ],
)
def test_gender_evidence_requires_literal_report_label(text, expected):
    from adapters.questionnaire_documents import _gender_evidence

    found = _gender_evidence(text)
    assert [item['value'] for item in found] == expected
    for item in found:
        assert text[item['start'] : item['end']] == item['quote']


@pytest.mark.asyncio
async def test_deleted_or_wrong_owner_artifact_cannot_be_read():
    stage = QuestionnaireDocumentStager(
        interpreter=Interpreter(),
        archive=Archive(),
        downloader=SimpleNamespace(download=AsyncMock(return_value=CONTENT)),
    )
    result = await stage.stage(user_id='u', questionnaire_id='q', file_list=[{'url': 'https://example.org/r.pdf'}])
    artifact = result['artifacts'][0]
    with pytest.raises(LookupError):
        await stage.read_artifact(user_id='other', artifact=artifact)
    with pytest.raises(LookupError):
        await stage.read_artifact(user_id='u', artifact={**artifact, 'questionnaireId': 'other'})


@pytest.mark.asyncio
async def test_existing_document_reference_has_explicit_nonpublication_kind():
    source_reader = SimpleNamespace(
        read=AsyncMock(
            return_value={
                'content': CONTENT,
                'fileName': 'archived.pdf',
                'parsed': {'success': True, 'text': 'verified source'},
            }
        )
    )
    downloader = SimpleNamespace(download=AsyncMock(side_effect=AssertionError('must not download a documentId')))
    stage = QuestionnaireDocumentStager(
        interpreter=Interpreter(), archive=Archive(), source_reader=source_reader, downloader=downloader
    )
    result = await stage.stage(user_id='u', questionnaire_id='q', file_list=[{'documentId': 'existing'}])
    artifact = result['artifacts'][0]
    assert artifact['kind'] == 'document_reference'
    assert artifact['sourceDocumentId'] == 'existing'
    assert result['reports'][0]['status'] == 'complete'
    downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_interpreter_archives_basic_check_and_reuses_questionnaire_artifact():
    first_response = model_report()
    no_value(first_response, 'age')
    flow = await actual_analysis_preparation([first_response, report(name=None, gender=None)])
    archive = MongoQuestionnaireArchive(bson_collection(), bson_collection())
    stage = QuestionnaireDocumentStager(
        interpreter=flow.analysis._current_interpreter,
        archive=archive,
        downloader=flow.upstream.downloader,
    )
    args = {
        'user_id': 'synthetic-user',
        'questionnaire_id': 'synthetic-questionnaire',
        'file_list': [{'url': 'https://example.test/source.pdf', 'fileType': 'pdf'}],
    }

    result = await stage.stage(**args)

    row = result['reports'][0]
    assert row['status'] == 'complete'
    assert row['interpretation']['basicInfo']['age'] == 42
    document = await archive.get(user_id=args['user_id'], artifact_id=row['artifactId'])
    assert {
        'analysis_part_00000',
        'analysis_input_00000_0',
        'analysis_output_00000_0',
        'analysis_part_00000_0',
    } <= document['payloads'].keys()
    assert await stage.stage(**args) == result
    assert len(flow.gateway.complete_requests) == 2
