"""Questionnaire v2 accepts complete legal projections without widening v1."""

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON
from pydantic import ValidationError

from adapters.persistence.questionnaires import MAX_DOCUMENT_BYTES
from adapters.persistence.questionnaires import MongoQuestionnaireStore
from health_profile.models import ProfileIngestionSourceType
from health_profile.record_ingestion import RecordAnswerCandidateExtractor
from health_profile.record_source import RecordProfileSourceRecord
from health_profile.source_extraction import ANSWER_EXTRACTION
from health_profile.source_extraction import ProfileUserSourceResolver
from health_profile.source_publications import publication_id
from modules.health_profile.contracts import ProfileAnswerEvidenceV2
from modules.health_profile.contracts import ProfileQuestionnaireSourceSnapshotV2
from modules.health_profile.contracts import ProfileQuestionnaireSubmissionProofV1
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.health_profile.contracts import parse_profile_source_snapshot
from modules.health_profile.contracts import profile_content_digest
from modules.questionnaire.context import answer_rows
from modules.questionnaire.schemas import normalize_answers
from modules.questionnaire.schemas import stable_hash
from modules.questionnaire.schemas import validate_questionnaire
from modules.questionnaire.service import QuestionnaireService
from modules.questionnaire.submitted_material import SUBMITTED_MATERIAL_SELECTION_V1
from modules.questionnaire.submitted_material import QuestionnaireSubmittedMaterialReader

from .test_multi_source_publications import SELF_TEXT
from .test_multi_source_publications import candidate
from .test_multi_source_publications import event_for
from .test_multi_source_publications import setup_source
from .test_multi_source_publications import source_facts
from .test_record_ingestion import _runtime
from .test_source_admission import SCOPE
from .test_source_admission import admission_setup


def submitted(*, name='health_goals', choice_text=SELF_TEXT, count=1, session_id='session-1'):
    questions = [
        {
            'name': name if count == 1 else f'q{index}',
            'type': 'radiogroup',
            'title': '原问题',
            'choices': [{'value': 'selected', 'text': choice_text}],
        }
        for index in range(count)
    ]
    schema = validate_questionnaire({'title': '原问卷', 'pages': [{'name': 'page', 'elements': questions}]})
    answers = {question['name']: 'selected' for question in questions}
    rows, canonical = normalize_answers(schema, answers)
    snapshot = {
        'userId': 'u',
        'sessionId': session_id,
        'questionnaireId': 'source-1',
        'status': 'ready',
        'questionnaire': schema,
        'reports': [],
        'artifacts': [],
    }
    pending = {
        'acceptedAt': '2026-11-09T00:00:00Z',
        'rawAnswers': answers,
        'rawAnswerHash': stable_hash(answers),
        'answers': rows,
        'canonicalAnswers': canonical,
        'answerHash': stable_hash(canonical),
    }
    service = QuestionnaireService(
        store=MongoQuestionnaireStore(None),
        generator=None,
        lease_manager=None,
        session_repository=None,
        clock=lambda: 1794182400.0,
    )
    service._validate_ready_capacity(snapshot)
    service._validate_submission_capacity(snapshot, pending)
    result = service._submission_document(snapshot, pending)
    result['profileIngestion']['deliveryMode'] = 'profile-source-event.v1'
    assert len(BSON.encode(result)) < MAX_DOCUMENT_BYTES
    return result


def raw_material(source, scope=None):
    proof = {
        'contractVersion': 'questionnaire-submission-proof.v1',
        'status': 'submitted',
        'deliveryMode': 'profile-source-event.v1',
        **{
            key: source[key]
            for key in (
                'userId',
                'questionnaireId',
                'submissionId',
                'sessionId',
                'sourceRevision',
                'submittedAt',
                'answerHash',
                'rawAnswerHash',
            )
        },
        'attachmentInventoryDigest': stable_hash({'reports': source['reports'], 'artifacts': source['artifacts']}),
    }
    rows = [{key: value for key, value in row.items() if key != 'sourceKind'} for row in answer_rows(source)]
    return {
        'contractVersion': 'profile.questionnaire-source.v2',
        'sourceType': 'questionnaire',
        'scope': scope or {'userId': source['userId'], 'subjectId': 'self'},
        'sourceId': source['questionnaireId'],
        'sourceRevision': source['sourceRevision'],
        'sessionId': source['sessionId'],
        'status': 'submitted',
        'submittedAt': source['submittedAt'],
        'answers': rows,
        'submissionProof': proof,
    }


def v1_material(raw):
    result = deepcopy(raw)
    result.pop('submissionProof')
    result['contractVersion'] = 'profile.questionnaire-source.v1'
    return result


def test_maximum_escaped_question_name_and_long_session_keep_original_identity():
    source = submitted(name='~' * 512, session_id='s' * 512)
    raw = raw_material(source)
    assert len(raw['answers'][0]['sourcePointer']) == 1039
    snapshot = parse_profile_source_snapshot(raw)
    assert isinstance(snapshot, ProfileQuestionnaireSourceSnapshotV2)
    assert snapshot.answers[0].question_id == '~' * 512 and snapshot.session_id == 's' * 512
    assert snapshot.answers[0].source_pointer == '/answers/' + '~0' * 512 + '/value'
    with pytest.raises(ValidationError):
        parse_profile_source_snapshot(v1_material(raw))


def test_maximum_accepted_answer_count_and_version_specific_limit():
    raw = raw_material(submitted(count=500))
    snapshot = parse_profile_source_snapshot(raw)
    assert len(snapshot.answers) == 500
    extra = {**raw['answers'][-1], 'answerIndex': 500, 'questionId': 'q500', 'sourcePointer': '/answers/q500/value'}
    raw['answers'].append(extra)
    with pytest.raises(ValidationError):
        parse_profile_source_snapshot(raw)
    assert len(parse_profile_source_snapshot(v1_material(raw)).answers) == 501


def test_real_accepted_control_character_choice_exceeds_v1_but_is_preserved_by_v2():
    source = submitted(choice_text='\x01' * 5_000_000)
    raw = raw_material(source)
    snapshot = parse_profile_source_snapshot(raw)
    canonical = snapshot.model_dump(mode='json', by_alias=True)
    size = len(json.dumps(canonical, ensure_ascii=False, allow_nan=False).encode())
    assert 30_000_000 < size < 32 * 1024 * 1024
    assert snapshot.answers[0].answer == '\x01' * 5_000_000
    assert len(BSON.encode(source)) < 15 * 1024 * 1024
    with pytest.raises(ValidationError, match='public source snapshot exceeds'):
        parse_profile_source_snapshot(v1_material(raw))


def test_v2_byte_budget_is_finite_and_exact_including_envelope():
    raw = parse_profile_source_snapshot(raw_material(submitted(choice_text=''))).model_dump(mode='json', by_alias=True)
    used = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode())
    raw['answers'][0]['answer'] = 'x' * (32 * 1024 * 1024 - used)
    accepted = parse_profile_source_snapshot(raw)
    assert len(accepted.answers[0].answer) == 32 * 1024 * 1024 - used
    raw['answers'][0]['answer'] += 'x'
    with pytest.raises(ValidationError, match='Questionnaire v2 snapshot exceeds'):
        parse_profile_source_snapshot(raw)


@pytest.mark.parametrize(
    'field,value',
    [
        ('sourcePointer', 'x' * 1040),
        ('questionId', 'x' * 513),
        ('question', 'x' * 513),
        ('intakeDimension', 'x' * 513),
        ('answeredAt', 'x' * 129),
        ('answer', {'not': 'mapped text'}),
        ('answer', 1),
        ('answer', b'bytes'),
    ],
)
def test_v2_answer_schema_rejects_non_mapping_shapes_and_out_of_bound_fields(field, value):
    raw = raw_material(submitted())['answers'][0]
    raw[field] = value
    with pytest.raises(ValidationError):
        ProfileAnswerEvidenceV2.model_validate(raw)


@pytest.mark.parametrize(
    'field,value',
    [
        ('userId', 'other'),
        ('questionnaireId', 'other'),
        ('submissionId', 'other'),
        ('sessionId', 'other'),
        ('sourceRevision', 2),
        ('submittedAt', 'other-time'),
        ('deliveryMode', 'legacy'),
        ('answerHash', 'invalid'),
        ('rawAnswerHash', 'invalid'),
        ('attachmentInventoryDigest', 'invalid'),
    ],
)
def test_submission_proof_cannot_drift_from_fixed_source_or_claim_legacy(field, value):
    raw = raw_material(submitted())
    raw['submissionProof'][field] = value
    with pytest.raises(ValidationError):
        parse_profile_source_snapshot(raw)


@pytest.mark.parametrize(
    'field', ['contractVersion', 'status', 'deliveryMode', 'answerHash', 'rawAnswerHash', 'attachmentInventoryDigest']
)
def test_submission_proof_requires_every_fixed_field(field):
    raw = raw_material(submitted())['submissionProof']
    raw.pop(field)
    with pytest.raises(ValidationError):
        ProfileQuestionnaireSubmissionProofV1.model_validate(raw)


@pytest.mark.asyncio
async def test_proof_matches_real_public_questionnaire_submitted_material_reader():
    source = submitted()
    reader = QuestionnaireSubmittedMaterialReader(
        store=SimpleNamespace(get_submitted=AsyncMock(return_value=source)),
        archive=object(),
    )
    public = await reader.read_submission(
        user_id='u',
        questionnaire_id='source-1',
        source_revision=1,
        selection=deepcopy(SUBMITTED_MATERIAL_SELECTION_V1),
    )
    snapshot = parse_profile_source_snapshot(raw_material(source))
    assert snapshot.submission_proof.model_dump(mode='json', by_alias=True) == public['payload']['submissionProof']
    assert 'reports' not in snapshot.model_dump() and 'artifacts' not in snapshot.model_dump()


async def setup_v2():
    flow = await setup_source('questionnaire')
    source = submitted()
    raw = raw_material(source, flow.scope.model_dump(mode='json', by_alias=True))
    flow.snapshot = parse_profile_source_snapshot(raw)
    reader = SimpleNamespace(
        read_source_snapshot=AsyncMock(side_effect=lambda ref, scope: deepcopy(flow.materials[ref.snapshot_id]))
    )
    flow.applier.resolvers[('questionnaire', flow.snapshot.contract_version)] = ProfileUserSourceResolver(reader)
    return flow, source


@pytest.mark.asyncio
async def test_v2_reuses_legacy_questionnaire_extractor_and_fixes_processing_revision_without_extra_calls():
    flow, source = await setup_v2()
    runtime, gateway = _runtime({'candidates': [candidate(answer_index=0)]})
    record = RecordProfileSourceRecord('u', source['questionnaireId'], 1, 1, 'queued', None, {})
    await RecordAnswerCandidateExtractor(
        runtime.hub, source_type=ProfileIngestionSourceType.QUESTIONNAIRE
    ).extract_answer_rows(
        record,
        answer_rows(source),
        subject_id=flow.scope.subject_id,
    )
    event = event_for(flow)
    before = deepcopy(flow.materials[event.snapshot_ref.snapshot_id])
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.processing_revision == 'profile-questionnaire-source-v2'
    assert flow.gateway.complete_requests[0].content == gateway.complete_requests[0].content
    assert flow.materials[event.snapshot_ref.snapshot_id] == before
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    assert len(flow.gateway.complete_requests) == 1 and len(await source_facts(flow)) == 1
    saved = await flow.inbox.store.get(publication_id(event))
    assert saved['processingSelection']['materialContract'] == 'profile.questionnaire-source.v2'
    assert saved['processingSelection']['childSelection'] == asdict(ANSWER_EXTRACTION)


@pytest.mark.asyncio
async def test_interruption_reuses_frozen_v2_model_output(monkeypatch):
    flow, _ = await setup_v2()
    event = event_for(flow)
    save = flow.inbox.save_extracted_candidates
    fail = [True]

    async def interrupted(*args):
        if fail[0]:
            fail[0] = False
            raise RuntimeError('saved model output but lost candidate ACK')
        return await save(*args)

    monkeypatch.setattr(flow.inbox, 'save_extracted_candidates', interrupted)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    assert len(flow.gateway.complete_requests) == 1
    flow.now[0] += timedelta(seconds=3)
    assert await flow.worker.recover_pending() == 1
    assert len(flow.gateway.complete_requests) == 1
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.processing_revision == 'profile-questionnaire-source-v2' and receipt.status == 'applied'


@pytest.mark.asyncio
async def test_fresh_v2_reservation_ready_and_exact_material_receive_use_actual_public_admission():
    flow = await admission_setup('questionnaire')
    source = submitted()
    source['questionnaireId'] = source['submissionId'] = 'source'
    snapshot = parse_profile_source_snapshot(raw_material(source, SCOPE.model_dump(mode='json', by_alias=True)))
    raw = snapshot.model_dump(mode='json', by_alias=True)
    ref = ProfileSourceSnapshotRefV1(
        provider='questionnaire', contract_version=snapshot.contract_version, snapshot_id='fixed-v2'
    )
    auth = flow.auth.model_copy(
        update={
            'material_contract_version': snapshot.contract_version,
            'material_snapshot_ref': ref,
            'material_content_digest': profile_content_digest(raw),
        }
    )
    assert (await flow.admission.reserve_fresh_source(auth, scope=SCOPE)).status == 'blocked'
    assert (await flow.admission.authorize_fresh_source(auth, scope=SCOPE)).status == 'ready'
    event = ProfileSourceEventV1(
        event_id='q-v2-event',
        scope=SCOPE,
        source_type='questionnaire',
        source_id='source',
        source_revision=1,
        publication_revision=1,
        event_kind='publish',
        occurred_at=auth.authorized_at,
        content_digest=profile_content_digest(raw),
        snapshot_ref=ref,
    )
    receipt = await flow.inbox.receive(event, scope=SCOPE)
    assert receipt.status == 'received'
    assert (await flow.admission.reserve_fresh_source(auth, scope=SCOPE)).status == 'ready'


def test_global_scope_limit_is_unchanged_and_long_user_requires_pinned_legacy_selection():
    raw = raw_material(submitted())
    raw['scope']['userId'] = raw['submissionProof']['userId'] = 'u' * 257
    with pytest.raises(ValidationError):
        parse_profile_source_snapshot(raw)
    with pytest.raises(ValidationError):
        ProfileScope(user_id=' padded ', subject_id='self')
