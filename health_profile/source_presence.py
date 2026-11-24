"""Bounded PRIMARY reads of Profile-owned source history; never initialize storage."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC
from datetime import datetime

from pymongo import ReadPreference

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .source_admission_contracts import ProfilePresenceSectionV1
from .source_admission_contracts import ProfileSourcePresenceV1

SOURCE_TYPES = frozenset({'health_report', 'record', 'questionnaire', 'chat'})


def _source_identity(scope, source_type, source_id):
    if not isinstance(scope, ProfileScope):
        raise TypeError('scope must be a ProfileScope')
    if source_type not in SOURCE_TYPES:
        raise ValueError('unsupported source type')
    if (
        not isinstance(source_id, str)
        or not source_id.strip()
        or source_id != source_id.strip()
        or len(source_id) > 256
    ):
        raise ValueError('source_id must be a normalized nonempty identifier')
    return source_id


class MongoProfileSourcePresenceReader:
    """No index, subject, view, job or publication writes, including construction.

    Every section is individually bounded; there is no cross-collection snapshot.
    A fresh command must hold the owner's admission fence, persist the Profile
    fence, and recheck absence. General reads do not grant admission.
    """

    def __init__(self, *, subjects, reports, revisions, jobs, facts, publications, clock=None, max_time_ms=2000):
        if type(max_time_ms) is not int or not 1 <= max_time_ms <= 10000:
            raise ValueError('max_time_ms must be between 1 and 10000')
        self._collections = {
            name: collection.with_options(read_preference=ReadPreference.PRIMARY)
            for name, collection in {
                'subjects': subjects,
                'reports': reports,
                'report_revisions': revisions,
                'jobs': jobs,
                'fact_contributions': facts,
                'publications': publications,
            }.items()
        }
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_time_ms = max_time_ms

    async def _scan(self, kind, query, projection, limit):
        cursor = self._collections[kind].find(query, projection).sort([('_id', 1)])
        rows = await cursor.max_time_ms(self._max_time_ms).limit(limit + 1).to_list(length=limit + 1)
        return rows[:limit], len(rows) > limit

    async def read_legacy_presence(self, scope, *, source_type, source_id, limit=1000, _ignore_marker=None):
        _source_identity(scope, source_type, source_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return an aware datetime')
        base = dict(scope=scope, source_type=source_type, source_id=source_id, read_at=now)
        try:
            subject = await self._collections['subjects'].find_one(
                {'userId': scope.user_id, 'subjectId': scope.subject_id, 'status': 'active'},
                {'_id': 0, 'userId': 1, 'subjectId': 1, 'kind': 1},
                max_time_ms=self._max_time_ms,
            )
            if subject is None:
                return ProfileSourcePresenceV1(**base, status='missing', error_code='source.subject_missing')
            if source_type != 'health_report' and subject.get('kind') != 'self':
                return ProfileSourcePresenceV1(**base, status='missing', error_code='source.self_anchor_required')
            sections = []
            source_ids = [source_id]
            if source_type == 'health_report':
                reports, truncated = await self._scan(
                    'reports',
                    {'$or': [{'reportId': source_id}, {'canonicalReportId': source_id}]},
                    {
                        '_id': 1,
                        'reportId': 1,
                        'userId': 1,
                        'subjectId': 1,
                        'sourceRevision': 1,
                        'canonicalReportId': 1,
                        'sourceFingerprint': 1,
                        'profileIngestion.status': 1,
                    },
                    limit,
                )
                if any(
                    row.get('userId') != scope.user_id or row.get('subjectId') != scope.subject_id for row in reports
                ):
                    return ProfileSourcePresenceV1(
                        **base, status='unavailable', error_code='source.history_scope_ambiguous'
                    )
                if any(not isinstance(row.get('reportId'), str) for row in reports):
                    raise ValueError('malformed report identity')
                source_ids = sorted(set(source_ids + [row['reportId'] for row in reports]))
                sections.append(_section('reports', reports, truncated))
                revisions, truncated = await self._scan(
                    'report_revisions',
                    {'$or': [{'reportId': {'$in': source_ids}}, {'canonicalReportId': source_id}]},
                    {
                        '_id': 1,
                        'reportId': 1,
                        'userId': 1,
                        'subjectId': 1,
                        'sourceRevision': 1,
                        'sourceKey': 1,
                        'profileIngestion.status': 1,
                    },
                    limit,
                )
                if any(
                    row.get('userId') != scope.user_id or row.get('subjectId') != scope.subject_id for row in revisions
                ):
                    return ProfileSourcePresenceV1(
                        **base, status='unavailable', error_code='source.history_scope_ambiguous'
                    )
                sections.append(_section('report_revisions', revisions, truncated))
            jobs, truncated = await self._scan(
                'jobs',
                {'userId': scope.user_id, 'sourceType': source_type, 'sourceId': {'$in': source_ids}},
                {
                    '_id': 1,
                    'jobId': 1,
                    'subjectId': 1,
                    'sourceId': 1,
                    'sourceRevision': 1,
                    'idempotencyKey': 1,
                    'status': 1,
                },
                limit,
            )
            # No status/subject filter: null-anchor jobs and completed/failed jobs
            # are real history. Relative-target facts share the owner's source.
            sections.append(_section('jobs', jobs, truncated))
            facts, truncated = await self._scan(
                'fact_contributions',
                {
                    'userId': scope.user_id,
                    'sources': {
                        '$elemMatch': {
                            'sourceType': source_type,
                            'sourceId': {'$in': source_ids},
                        }
                    },
                },
                {'_id': 1, 'factId': 1, 'subjectId': 1, 'status': 1, 'version': 1},
                limit,
            )
            sections.append(_section('fact_contributions', facts, truncated))
            identities = [
                {'sourceType': source_type, 'sourceId': {'$in': source_ids}},
                {'event.sourceType': source_type, 'event.sourceId': {'$in': source_ids}},
                {'authorization.sourceType': source_type, 'authorization.sourceId': {'$in': source_ids}},
            ]
            query = {'scope.userId': scope.user_id, '$or': identities}
            if _ignore_marker is not None:
                query['_id'] = {'$ne': _ignore_marker}
            publications, truncated = await self._scan(
                'publications',
                query,
                {'_id': 1, 'recordKind': 1, 'status': 1, 'revision': 1},
                limit,
            )
            sections.append(_section('publications', publications, truncated))
            status = 'present' if any(section.observed_count for section in sections) else 'absent'
            if any(section.truncated for section in sections):
                status = 'unavailable'
            proof = {
                'contractVersion': 'profile-source-presence.v1',
                'scope': scope.model_dump(by_alias=True),
                'sourceType': source_type,
                'sourceId': source_id,
                'status': status,
                'sections': [section.model_dump(by_alias=True) for section in sections],
            }
            digest = profile_content_digest(proof)
            return ProfileSourcePresenceV1(
                **base,
                status=status,
                sections=tuple(sections),
                content_digest=digest,
                proof_ref='profile_presence_' + digest,
                error_code='source.presence_limit_exceeded' if status == 'unavailable' else None,
            )
        except Exception:
            return ProfileSourcePresenceV1(**base, status='unavailable', error_code='source.presence_read_unavailable')


def _section(kind, rows, truncated):
    # Only projected identity/status metadata is hashed; medical values and job
    # payloads are neither selected nor returned. BSON Int64 is a JSON integer.
    values = deepcopy(rows)
    for row in values:
        if '_id' in row:
            row['_id'] = str(row['_id'])
    values.sort(key=lambda row: profile_content_digest(row))
    return ProfilePresenceSectionV1(
        kind=kind,
        observed_count=len(rows),
        truncated=truncated,
        evidence_digest=profile_content_digest(values),
    )
