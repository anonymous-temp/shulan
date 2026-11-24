"""Committed attachment reads preserve legacy identity without generating or publishing."""

import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON
from pymongo import ReadPreference

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from adapters.questionnaire_submitted_material import create_questionnaire_submitted_material_reader
from modules.questionnaire.application import SUBMITTED_MATERIAL_SELECTION_V1
from modules.questionnaire.application import QuestionnaireSubmittedMaterialError
from modules.questionnaire.application import QuestionnaireSubmittedMaterialReader
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection


class PrimaryCollection(FakeCollection):
    def __init__(self, documents=()):
        super().__init__()
        self.documents = deepcopy(list(documents))
        self.preferences = []

    def with_options(self, *, read_preference):
        self.preferences.append(read_preference)
        return self


@pytest.fixture
def source():
    result = {'observations': [{'itemName': ' 血糖 ', 'resultValue': '6.4', 'unit': 'mmol/L'}]}
    artifact = {
        'artifactId': 'a',
        'questionnaireId': 'q',
        'fileIndex': 0,
        'kind': 'questionnaire_document',
        'contentSha256': 'sha',
    }
    return {
        '_id': 'q',
        'userId': 'u',
        'sessionId': 's',
        'questionnaireId': 'q',
        'submissionId': 'q',
        'status': 'submitted',
        'sourceRevision': 1,
        'submittedAt': '2026-01-01T00:00:00+00:00',
        'answerHash': 'answer',
        'rawAnswerHash': 'raw',
        'reports': [{'artifactId': 'a', 'status': 'complete', 'interpretation': result}],
        'artifacts': [artifact],
        'profileIngestion': {'status': 'pending'},
    }


def owned_reader(source):
    source_store = SimpleNamespace(get_submitted=AsyncMock(side_effect=lambda *args: deepcopy(source)))
    result = source['reports'][0]['interpretation']
    archive = SimpleNamespace(
        get=AsyncMock(
            return_value={
                '_id': 'a',
                'userId': 'u',
                'questionnaireId': 'q',
                'retained': True,
                'contentSha256': 'sha',
            }
        ),
        read_payload=AsyncMock(return_value=deepcopy(result)),
    )
    return QuestionnaireSubmittedMaterialReader(store=source_store, archive=archive), archive


async def read(reader, **overrides):
    return await reader.read_submission(
        **{
            'user_id': 'u',
            'questionnaire_id': 'q',
            'source_revision': 1,
            'selection': SUBMITTED_MATERIAL_SELECTION_V1,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_original_attachment_order_identity_and_exact_result_survive_repeated_reads(source):
    reader, archive = owned_reader(source)
    first = await read(reader)
    selected = first['payload']['attachments']
    assert selected[0]['contentKey'] == 'sha'
    assert selected[0]['interpretation'] == source['reports'][0]['interpretation']
    saved = deepcopy(first)
    first['payload']['attachments'][0]['interpretation'].clear()
    source['profileIngestion'] = {'status': 'queued', 'jobId': 'j'}
    source['revision'] = 'new-storage-revision'
    assert await reader.read_snapshot(saved['reference'], user_id='u') == saved
    archive.read_payload.assert_awaited_with('a', 'u', None, 'interpretation')


@pytest.mark.asyncio
async def test_duplicate_contents_and_document_references_preserve_legacy_selection(source):
    reader, archive = owned_reader(source)
    source['artifacts'] += [
        {**source['artifacts'][0], 'artifactId': 'b', 'fileIndex': 1},
        {**source['artifacts'][0], 'artifactId': 'c', 'fileIndex': 2, 'kind': 'document_reference'},
        {**source['artifacts'][0], 'artifactId': 'd', 'fileIndex': 3},
    ]
    source['reports'] += [
        {**source['reports'][0], 'artifactId': 'b'},
        {**source['reports'][0], 'artifactId': 'c'},
        {'artifactId': 'd', 'status': 'unavailable'},
    ]
    result = await read(reader)
    assert [row['artifactId'] for row in result['payload']['attachments']] == ['a']
    assert result['payload']['excluded'] == [
        {'artifactId': 'b', 'reason': 'duplicate_content'},
        {'artifactId': 'c', 'reason': 'document_reference'},
        {'artifactId': 'd', 'reason': 'no_interpretation'},
    ]
    archive.get.assert_awaited_once()
    archive.read_payload.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_legacy_sha_keeps_artifact_identity_even_if_archive_has_sha(source):
    source['artifacts'][0].pop('contentSha256')
    reader, _ = owned_reader(source)
    assert (await read(reader))['payload']['attachments'][0]['contentKey'] == 'a'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('status', 'ready'),
        ('sourceRevision', True),
        ('sourceRevision', 2),
        ('userId', 'foreign'),
        ('questionnaireId', 'other'),
        ('submissionId', 'other'),
    ],
)
async def test_uncommitted_or_wrong_owner_source_is_rejected_before_archive_read(source, field, value):
    source[field] = value
    reader, archive = owned_reader(source)
    with pytest.raises(QuestionnaireSubmittedMaterialError):
        await read(reader)
    archive.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('retained', False), ('userId', 'other'), ('cleanupState', 'deleting')])
async def test_unretained_or_inaccessible_archive_is_not_a_source_proof(source, field, value):
    reader, archive = owned_reader(source)
    archive.get.return_value[field] = value
    with pytest.raises(LookupError):
        await read(reader)
    archive.read_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_reference_rejects_later_source_or_interpretation_changes(source):
    reader, archive = owned_reader(source)
    result = await read(reader)
    archive.read_payload.return_value = {'changed': 'not committed'}
    with pytest.raises(QuestionnaireSubmittedMaterialError, match='interpretation'):
        await reader.read_snapshot(result['reference'], user_id='u')
    source['reports'][0]['interpretation'] = {'changed': 'not committed'}
    with pytest.raises(QuestionnaireSubmittedMaterialError, match='no longer matches'):
        await reader.read_snapshot(result['reference'], user_id='u')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'selection', [None, {}, {**SUBMITTED_MATERIAL_SELECTION_V1, 'implementationRevision': 'future'}]
)
async def test_unsupported_version_never_reads_or_regenerates_material(source, selection):
    reader, archive = owned_reader(source)
    with pytest.raises(QuestionnaireSubmittedMaterialError):
        await read(reader, selection=selection)
    reader.store.get_submitted.assert_not_awaited()
    archive.read_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_mongo_adapters_read_primary_and_verified_bson_without_indexes_or_writes(source):
    payload = BSON.encode({'payload': source['reports'][0]['interpretation']})
    documents = PrimaryCollection(
        [
            {
                '_id': 'a',
                'userId': 'u',
                'subjectId': None,
                'questionnaireId': 'q',
                'retained': True,
                'contentSha256': 'sha',
                'payloads': {
                    'interpretation': {
                        'payloadId': 'p',
                        'chunks': 1,
                        'bytes': len(payload),
                        'sha256': hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
        ]
    )
    chunks = PrimaryCollection([{'_id': 'a:p:0', 'userId': 'u', 'data': payload}])
    questionnaires = PrimaryCollection([source])
    originals = [deepcopy(row.documents) for row in (questionnaires, documents, chunks)]
    reader = create_questionnaire_submitted_material_reader(
        questionnaires=questionnaires,
        documents=documents,
        chunks=chunks,
    )
    assert all(row.preferences == [ReadPreference.PRIMARY] for row in (questionnaires, documents, chunks))
    result = await read(reader)
    assert result['payload']['attachments'][0]['interpretation'] == source['reports'][0]['interpretation']
    assert [row.documents for row in (questionnaires, documents, chunks)] == originals
    assert all(row.indexes == [] for row in (questionnaires, documents, chunks))
    chunks.documents[0]['data'] = b'corrupted'
    with pytest.raises(RuntimeError):
        await reader.read_snapshot(result['reference'], user_id='u')


@pytest.mark.asyncio
async def test_legacy_pending_scan_does_not_starve_behind_new_or_unknown_modes(source):
    collection = PrimaryCollection()
    for number in range(120):
        collection.documents.append(
            {
                **deepcopy(source),
                '_id': f'new-{number}',
                'questionnaireId': f'new-{number}',
                'profileIngestion': {
                    'status': 'pending',
                    'deliveryMode': 'profile-source-event.v1' if number % 2 else 'future',
                },
            }
        )
    collection.documents.append({**deepcopy(source), '_id': 'legacy', 'questionnaireId': 'legacy'})
    result = await MongoQuestionnaireStore(collection).list_pending_ingestion(limit=1)
    assert [row['questionnaireId'] for row in result] == ['legacy']


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing_artifact', 'duplicate_artifact', 'unknown_mode'])
async def test_incomplete_inventory_or_unknown_source_mode_is_not_silently_published(source, corruption):
    reader, archive = owned_reader(source)
    if corruption == 'missing_artifact':
        source['artifacts'] = []
    elif corruption == 'duplicate_artifact':
        source['artifacts'].append(deepcopy(source['artifacts'][0]))
    else:
        source['profileIngestion']['deliveryMode'] = 'future'
    with pytest.raises(QuestionnaireSubmittedMaterialError):
        await read(reader)
    archive.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('quality', ['complete', 'partial'])
async def test_archived_processing_quality_is_preserved_without_reinterpretation(source, quality):
    source['reports'][0]['status'] = quality
    source['reports'][0]['interpretation']['processing'] = {'status': quality, 'issues': ['原始质量说明']}
    reader, archive = owned_reader(source)
    result = await read(reader)
    assert result['payload']['attachments'][0]['interpretation']['processing'] == {
        'status': quality,
        'issues': ['原始质量说明'],
    }
    archive.read_payload.assert_awaited_once()
