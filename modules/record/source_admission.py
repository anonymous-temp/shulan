"""Record-owned checks for first publication from an immutable new-mode completion."""

import json
from copy import deepcopy

from modules.record.profile_publication import DELIVERY_MODE
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import source_intent_id
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.profile_source import RecordSourceDraftV1
from modules.record.profile_source import content_digest

RECORD_SOURCE_ADMISSION_REVISION = 'record-source-admission.v1'


def fixed_request(*, material, snapshot_ref, completion_id, draft_id):
    value = deepcopy(
        {
            'material': material,
            'snapshotRef': snapshot_ref,
            'completionId': completion_id,
            'draftId': draft_id,
        }
    )
    scope = value['material'].get('scope', {})
    source_id = value['material'].get('sourceId')
    if (
        value['material'].get('contractVersion') != 'profile.record-source.v2'
        or value['material'].get('sourceType') != 'record'
        or value['material'].get('sessionId') != source_id
        or type(value['material'].get('sourceRevision')) is not int
        or value['material']['sourceRevision'] != 1
        or set(scope) != {'userId', 'subjectId'}
        or any(
            not isinstance(x, str) or not x or x.strip() != x
            for x in (
                scope.get('userId'),
                scope.get('subjectId'),
                source_id,
                completion_id,
                draft_id,
            )
        )
        or value['snapshotRef']
        != {
            'provider': 'record',
            'contractVersion': 'profile.record-source.v2',
            'snapshotId': 'record_material_' + content_digest(value['material']),
        }
    ):
        raise RecordSourceConflict('fresh Record admission requires its exact initial material')
    return value


def request_identity(request):
    material = request['material']
    return source_intent_id(material['scope']['userId'], material['sourceId'])


def verify_intent(intent, request):
    if not isinstance(intent, dict):
        raise RecordSourceConflict('the durable Record completion intent is missing')
    material = request['material']
    draft = RecordSourceDraftV1(intent['draftJson'])
    if (
        intent.get('_id') != request_identity(request)
        or intent.get('consumerRevision') != RECORD_PROFILE_SOURCE_REVISION
        or intent.get('userId') != material['scope']['userId']
        or intent.get('sessionId') != material['sourceId']
        or intent.get('draftId') != request['draftId']
        or intent.get('completionId') != request['completionId']
        or draft.draft_id != request['draftId']
        or draft.profile_material(material['scope']) != material
        or json.loads(intent['materialJson']) != material
        or intent.get('scope') != material['scope']
        or intent.get('snapshotRef') != request['snapshotRef']
    ):
        raise RecordSourceConflict('Record source intent differs from its fixed admission material')
    return draft


def verify_completion(session, intent, *, pending_commit=False):
    """Validate retained raw QA evidence even after the supported Chat handoff."""
    if not isinstance(session, dict):
        raise RecordSourceConflict('the completed Record business evidence is missing')
    value = deepcopy(session)
    marker = value.get('qaProfileIngestion') or {}
    binding = marker.get('recordSourceIntent') or {}
    expected = {
        'consumerRevision': RECORD_PROFILE_SOURCE_REVISION,
        'intentId': intent['_id'],
        'draftId': intent['draftId'],
        'rawDigest': RecordSourceDraftV1(intent['draftJson']).digest,
    }
    if not pending_commit:
        expected['completionId'] = intent['completionId']
    if (
        value.get('userId') != intent['userId']
        or value.get('sessionId') != intent['sessionId']
        or marker.get('deliveryMode') != DELIVERY_MODE
        or marker.get('status') != 'source_managed'
        or binding != expected
        or value.get('recordCompletionId', intent['completionId']) != intent['completionId']
    ):
        raise RecordSourceConflict('Record completion does not retain its exact new-mode binding')
    if value.get('type') == 'chat' and not pending_commit:
        value.update(type='qa', status='completed', completedAt=value.get('qaCompletedAt'), round=value.get('qaRound'))
    elif value.get('recordProfileConsumerRevision') != RECORD_PROFILE_SOURCE_REVISION:
        raise RecordSourceConflict('Record completion has no accepted source version')
    if (
        value.get('type') == 'qa'
        and session.get('type') == 'qa'
        and value.get('recordCompletionId') != intent['completionId']
    ):
        raise RecordSourceConflict('Record completion has no fixed commit identity')
    draft = RecordSourceDraftV1.capture(value)
    if draft.draft_id != intent['draftId'] or draft.payload != RecordSourceDraftV1(intent['draftJson']).payload:
        raise RecordSourceConflict('retained Record evidence differs from the completed raw draft')
    return {'completionId': intent['completionId'], 'draftId': draft.draft_id, 'rawDigest': draft.digest}
