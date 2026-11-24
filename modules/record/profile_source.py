"""Record-owned immutable raw evidence and explicit publication selections.

Profile owns extraction/governance; this module only freezes accepted business
inputs. Constructing a draft never ensures a subject, calls a model or publishes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass

LEGACY_RECORD_PROFILE_REVISION = 'legacy-record-profile.v1'
RECORD_PROFILE_SOURCE_REVISION = 'record-profile-source.v1'
RECORD_SOURCE_DRAFT_CONTRACT = 'record.source-draft.v1'
RECORD_PROFILE_MATERIAL_CONTRACT = 'profile.record-source.v2'
_USER_INPUT_FIELDS = ('name', 'gender', 'age', 'height', 'weight')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _identifier(value, name):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f'{name} must be a normalized nonempty string')
    return value


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'{name} must be a positive exact integer')
    return value


@dataclass(frozen=True, slots=True)
class RecordSourceDraftV1:
    """Scope-free frozen business evidence, before Profile's explicit self command."""

    _payload_json: str

    def __post_init__(self):
        value = json.loads(self._payload_json)
        if not isinstance(value, dict) or value.get('contractVersion') != RECORD_SOURCE_DRAFT_CONTRACT:
            raise ValueError('unsupported Record source draft contract')
        _identifier(value.get('userId'), 'userId')
        _identifier(value.get('sessionId'), 'sessionId')
        if value.get('sourceId') != value['sessionId']:
            raise ValueError('Record source identity must retain its session ID')
        _positive(value.get('sourceRevision'), 'sourceRevision')
        _positive(value.get('roundNumber'), 'roundNumber')
        if not isinstance(value.get('completedAt'), str) or not value['completedAt']:
            raise ValueError('the original completedAt text is required')
        for key in ('qaHistory', 'userInputs', 'hospitalData', 'weRunData'):
            if not isinstance(value.get(key), list):
                raise TypeError(f'{key} must preserve its raw array')
        if not isinstance(value.get('smartDeviceData'), dict):
            raise TypeError('smartDeviceData must preserve its raw object')
        selected = set()
        for row in value['userInputs']:
            if (
                not isinstance(row, dict)
                or row.get('field') not in _USER_INPUT_FIELDS
                or row.get('provenance') != 'user_input'
                or row['field'] in selected
                or 'value' not in row
            ):
                raise ValueError('only unique explicit user-input fields are allowed')
            selected.add(row['field'])
        # Fail on nonfinite or unsupported data; never stringify or truncate it.
        canonical_json(value)

    @classmethod
    def capture(cls, session):
        if not isinstance(session, dict) or session.get('type') != 'qa' or session.get('status') != 'completed':
            raise ValueError('a completed QA business snapshot is required')
        health = session.get('healthData', {})
        info = session.get('extractedInfo', {})
        provenance = session.get('extractedInfoSources', {})
        if not all(isinstance(value, dict) for value in (health, info, provenance)):
            raise TypeError('Record health data and explicit field provenance must be objects')
        payload = {
            'contractVersion': RECORD_SOURCE_DRAFT_CONTRACT,
            'userId': session.get('userId'),
            'sessionId': session.get('sessionId'),
            'sourceId': session.get('sessionId'),
            'sourceRevision': session.get('qaSourceRevision', 1),
            'roundNumber': session.get('round', 1),
            'completedAt': session.get('completedAt'),
            'qaHistory': deepcopy(session.get('qaHistory', [])),
            'userInputs': [
                {'field': field, 'value': deepcopy(info[field]), 'provenance': 'user_input'}
                for field in _USER_INPUT_FIELDS
                if provenance.get(field) == 'user_input' and info.get(field) is not None and info.get(field) != ''
            ],
            'hospitalData': deepcopy(health.get('hospitalData', [])),
            'weRunData': deepcopy(health.get('weRunData', [])),
            'smartDeviceData': deepcopy(health.get('smartDeviceData', {})),
        }
        return cls(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False))

    @property
    def payload(self):
        return json.loads(self._payload_json)

    @property
    def digest(self):
        return content_digest(self.payload)

    @property
    def draft_id(self):
        return 'record_source_draft_' + self.digest

    @property
    def user_id(self):
        return self.payload['userId']

    @property
    def source_id(self):
        return self.payload['sourceId']

    def profile_material(self, scope):
        """Render the v2 JSON envelope after an authorized, persisted scope choice."""
        if not isinstance(scope, dict) or scope.get('userId') != self.user_id:
            raise PermissionError('Profile scope belongs to a different source owner')
        _identifier(scope.get('subjectId'), 'subjectId')
        value = self.payload
        value.pop('userId')
        value['contractVersion'] = RECORD_PROFILE_MATERIAL_CONTRACT
        value['sourceType'] = 'record'
        value['scope'] = {'userId': scope['userId'], 'subjectId': scope['subjectId']}
        return value


@dataclass(frozen=True, slots=True)
class RecordPublicationPolicy:
    max_dispatches: int = 5
    max_transport_failures: int = 5
    deadline_seconds: int = 86400
    lease_seconds: int = 60
    io_timeout_seconds: int = 20
    poll_seconds: int = 30

    def __post_init__(self):
        for name, value in asdict(self).items():
            _positive(value, name)
        if 2 * self.io_timeout_seconds >= self.lease_seconds:
            raise ValueError('bounded receiver calls must fit inside the source lease')
