"""Raw Record evidence stays immutable and does not acquire provider semantics."""

from copy import deepcopy

import pytest

from modules.health_profile.contracts import ProfileRecordSourceSnapshotV2
from modules.record.profile_source import RecordPublicationPolicy
from modules.record.profile_source import RecordSourceDraftV1


def session():
    return {
        'userId': 'user',
        'sessionId': 'session',
        'type': 'qa',
        'status': 'completed',
        'round': 1,
        'completedAt': '2026-11-10T12:01:02',
        'qaHistory': [
            {
                'question': {'text': '原始问题'},
                'answer': {'values': ['  原文  ', 'second']},
                'answeredAt': 'original-time',
            }
        ],
        'extractedInfo': {'name': '原始姓名', 'age': 37, 'height': 0, 'weight': 60},
        'extractedInfoSources': {
            'name': 'user_input',
            'age': 'health_report',
            'height': 'user_input',
            'weight': 'model',
        },
        'healthData': {
            'hospitalData': [{'z': 'keep', 'nested': [2, 1]}, 'unfiltered raw row'],
            'weRunData': [{'date': 'original-date', 'steps': 99}],
            'smartDeviceData': {'nested': {'raw': [1, 2]}},
            'doctorProductPackages': [{'not': 'profile evidence'}],
        },
        'parsedHealthReports': [{'report': 'already has an independent publisher'}],
        'healthProfileContext': 'existing Profile context is not new evidence',
    }


def test_raw_draft_is_detached_and_exact_public_v2_payload_keeps_original_arrays():
    raw = session()
    original = deepcopy(raw)
    draft = RecordSourceDraftV1.capture(raw)
    raw['qaHistory'][0]['answer']['values'].clear()
    draft.payload['qaHistory'].clear()
    material = draft.profile_material({'userId': 'user', 'subjectId': 'self'})
    public = ProfileRecordSourceSnapshotV2.model_validate(material).model_dump(mode='json', by_alias=True)
    assert public == material
    assert material['qaHistory'] == original['qaHistory']
    assert material['hospitalData'] == original['healthData']['hospitalData']
    assert material['weRunData'] == original['healthData']['weRunData']
    assert material['smartDeviceData'] == original['healthData']['smartDeviceData']
    assert material['userInputs'] == [
        {'field': 'name', 'value': '原始姓名', 'provenance': 'user_input'},
        {'field': 'height', 'value': 0, 'provenance': 'user_input'},
    ]
    assert not {'parsedHealthReports', 'healthProfileContext', 'doctorProductPackages'} & material.keys()
    assert RecordSourceDraftV1.capture(original).draft_id == draft.draft_id


def test_scope_and_original_material_identity_cannot_be_reassigned():
    draft = RecordSourceDraftV1.capture(session())
    with pytest.raises(PermissionError):
        draft.profile_material({'userId': 'other', 'subjectId': 'self'})
    changed = session()
    changed['qaHistory'].reverse()
    changed['qaHistory'][0]['answer']['values'].reverse()
    assert RecordSourceDraftV1.capture(changed).draft_id != draft.draft_id


@pytest.mark.parametrize(
    'field,value',
    [('status', 'active'), ('type', 'chat'), ('round', True), ('qaSourceRevision', 0), ('completedAt', None)],
)
def test_invalid_completion_cannot_create_a_source_draft(field, value):
    raw = session()
    raw[field] = value
    with pytest.raises((ValueError, TypeError)):
        RecordSourceDraftV1.capture(raw)


@pytest.mark.parametrize('field,value', [('qaHistory', {}), ('healthData', ['not-object'])])
def test_invalid_original_shapes_are_not_coerced_or_omitted(field, value):
    raw = session()
    raw[field] = value
    with pytest.raises((ValueError, TypeError)):
        RecordSourceDraftV1.capture(raw)


def test_nonfinite_source_and_invalid_recovery_policy_fail_explicitly():
    raw = session()
    raw['healthData']['hospitalData'] = [{'value': float('nan')}]
    with pytest.raises(ValueError):
        RecordSourceDraftV1.capture(raw)
    with pytest.raises(ValueError):
        RecordPublicationPolicy(max_dispatches=True)
    with pytest.raises(ValueError):
        RecordPublicationPolicy(io_timeout_seconds=31)


def test_capture_preserves_raw_object_insertion_order():
    source = session()
    source['healthData']['hospitalData'] = [{'z': 1, 'a': 2}]
    draft = RecordSourceDraftV1.capture(source)
    assert list(draft.payload['hospitalData'][0]) == ['z', 'a']


@pytest.mark.parametrize('field', ['healthData', 'extractedInfo', 'extractedInfoSources'])
def test_invalid_falsey_objects_are_not_silently_discarded(field):
    source = session()
    source[field] = []
    with pytest.raises(TypeError):
        RecordSourceDraftV1.capture(source)
