"""Questionnaire-owned fixed submission evidence and independent delivery intent."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime

CONSUMER_REVISION = 'questionnaire-profile-source.v1'
DELIVERY_MODE = 'profile-source-event.v1'
MATERIAL_CONTRACT = 'profile.questionnaire-source.v2'
LEGACY_SELECTION = {'consumerRevision': 'questionnaire-profile-legacy.v1', 'deliveryMode': 'legacy'}
DRAFT_CONTRACT = 'questionnaire.profile-source-draft.v1'
ATTACHMENT_SELECTION = 'document-questionnaire-attachment-publication-v1'


class QuestionnaireSourceConflict(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def source_digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class QuestionnaireSourcePolicy:
    max_attempts: int = 24
    retry_seconds: int = 30
    max_age_seconds: int = 86400
    lease_seconds: int = 120

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError('Questionnaire source policy requires positive integer bounds')


def execution_selection(policy=QuestionnaireSourcePolicy()):
    return {
        'consumerRevision': CONSUMER_REVISION,
        'deliveryMode': DELIVERY_MODE,
        'materialContractVersion': MATERIAL_CONTRACT,
        'attachmentSelection': ATTACHMENT_SELECTION,
        'policy': asdict(policy),
    }


def acceptance_selection(user_id, selection):
    selection = validate_selection(selection)
    # Scope V1 cannot represent these existing HTTP identities. Freeze legacy at
    # first acceptance; never fall back after accepting an event-mode source.
    if not isinstance(user_id, str) or len(user_id) > 256 or user_id != user_id.strip():
        return deepcopy(LEGACY_SELECTION)
    return selection


def validate_selection(value):
    if value == LEGACY_SELECTION:
        return deepcopy(LEGACY_SELECTION)
    if not isinstance(value, dict):
        raise QuestionnaireSourceConflict('Questionnaire source selection is unavailable')
    policy = QuestionnaireSourcePolicy(**value.get('policy', {}))
    if value != execution_selection(policy):
        raise QuestionnaireSourceConflict('The exact Questionnaire source consumer is unavailable')
    return deepcopy(value)


def submission_proof(snapshot):
    if snapshot.get('status') != 'submitted' or snapshot.get('submissionId') != snapshot.get('questionnaireId'):
        raise QuestionnaireSourceConflict('Questionnaire source is not a committed submission')
    if type(snapshot.get('sourceRevision')) is not int or snapshot['sourceRevision'] != 1:
        raise QuestionnaireSourceConflict('Unsupported Questionnaire source revision')
    for key in ('userId', 'sessionId', 'questionnaireId', 'submittedAt'):
        if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
            raise QuestionnaireSourceConflict('Questionnaire source identity is incomplete')
    instant = datetime.fromisoformat(snapshot['submittedAt'])
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise QuestionnaireSourceConflict('Questionnaire submission time must retain its timezone')
    return {
        'contractVersion': 'questionnaire-submission-proof.v1',
        'status': 'submitted',
        'deliveryMode': DELIVERY_MODE,
        'userId': snapshot['userId'],
        'questionnaireId': snapshot['questionnaireId'],
        'submissionId': snapshot['submissionId'],
        'sessionId': snapshot['sessionId'],
        'sourceRevision': 1,
        'submittedAt': snapshot['submittedAt'],
        'answerHash': snapshot['answerHash'],
        'rawAnswerHash': snapshot['rawAnswerHash'],
        'attachmentInventoryDigest': source_digest(
            {'reports': snapshot.get('reports', []), 'artifacts': snapshot.get('artifacts', [])}
        ),
    }


def raw_submission(snapshot):
    """The saved survey and every original answer are retained, without copying report bodies."""
    proof = submission_proof(snapshot)
    raw = {key: deepcopy(snapshot[key]) for key in ('questionnaire', 'rawAnswers', 'answers', 'canonicalAnswers')}
    if (
        source_digest(raw['rawAnswers']) != proof['rawAnswerHash']
        or source_digest(raw['canonicalAnswers']) != proof['answerHash']
    ):
        raise QuestionnaireSourceConflict('Questionnaire answer evidence no longer matches its acceptance')
    return {'contractVersion': DRAFT_CONTRACT, 'submissionProof': proof, **raw}


def prepare_submission_source(snapshot):
    """Add only compact intent metadata to the same business write as the original answers."""
    selection = snapshot.get('profileSourceSelection')
    if selection is None or selection == LEGACY_SELECTION:
        return None  # An already accepted legacy questionnaire never opts in during submit.
    selection = validate_selection(selection)
    raw = raw_submission(snapshot)
    digest = source_digest(raw)
    identity = source_digest([snapshot['userId'], snapshot['questionnaireId'], digest])
    state = {
        'status': 'pending',
        'jobId': None,
        'deliveryMode': DELIVERY_MODE,
        'consumerRevision': CONSUMER_REVISION,
        'sourceId': snapshot['questionnaireId'],
        'sourceRevision': 1,
        'publicationRevision': 1,
        'draftId': 'questionnaire_raw_' + digest,
        'draftDigest': digest,
        'intentId': 'questionnaire_intent_' + identity,
        'attachmentIntent': {
            'requestId': 'questionnaire_attachments_' + identity,
            'selection': ATTACHMENT_SELECTION,
            'inventoryDigest': raw['submissionProof']['attachmentInventoryDigest'],
        },
        'selection': selection,
        'createdAt': snapshot['submittedAt'],
    }
    state['manifestDigest'] = source_digest(state)
    snapshot['profileIngestion'] = deepcopy(state)
    return state


def saved_binding(snapshot):
    """Verify a persisted business document; unrelated factor/task revisions are not identity."""
    if not isinstance(snapshot, dict):
        return None
    state = snapshot.get('profileIngestion', {})
    if not isinstance(state, dict):
        raise QuestionnaireSourceConflict('Invalid Questionnaire delivery metadata')
    if state.get('deliveryMode') in (None, 'legacy'):
        if snapshot.get('profileSourceSelection') not in (None, LEGACY_SELECTION):
            raise QuestionnaireSourceConflict('New Questionnaire acceptance lost its delivery mode')
        return None
    candidate = deepcopy(snapshot)
    expected = prepare_submission_source(candidate)
    if expected is None or state != expected:
        raise QuestionnaireSourceConflict('The committed Questionnaire source or intent changed')
    return deepcopy(expected)


def answer_evidence_v1(raw):
    """Frozen Questionnaire mapping from the accepted schema; it never declares facts.

    Text uses the already accepted canonical value. Choice labels, none labels,
    order, original JSON pointers and submitted time match the legacy source.
    """
    rows = []
    names = [question['name'] for page in raw['questionnaire']['pages'] for question in page['elements']]
    for name in names:
        item = raw['answers'][name]
        if not isinstance(item, dict) or item.get('status') != 'answered' or not item.get('provided'):
            continue
        question, value = item['question'], item['value']
        if question['type'] == 'text':
            text = value
        else:
            choices = {choice['value']: choice.get('text', choice['value']) for choice in question['choices']}
            if question.get('showNoneItem'):
                choices['none'] = question.get('noneText', '没有')
            text = '、'.join(choices[token] for token in (value if isinstance(value, list) else [value]))
        rows.append(
            {
                'answerIndex': len(rows),
                'sourcePointer': '/answers/' + name.replace('~', '~0').replace('/', '~1') + '/value',
                'questionId': name,
                'questionType': question['type'],
                'intakeDimension': 'goal' if name == 'health_goals' else name,
                'question': question['title'],
                'answer': text,
                'answeredAt': raw['submissionProof']['submittedAt'],
                'provided': True,
            }
        )
    return rows
