"""Record adapters over versioned, pure Profile and Document public reads."""

from __future__ import annotations

import hashlib
import json

from modules.analysis.observation_reader import DocumentObservationPageV1
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.contracts import ProfileFactPageV1
from modules.health_profile.contracts import ProfileSubjectV1
from modules.health_profile.current_age import profile_age_context
from modules.record.profile_context import RecordProfileFactV1
from modules.record.profile_context import RecordProfilePageV1
from modules.record.profile_context import RecordProfileSubjectV1
from modules.record.profile_context import RecordSupplementPageV1
from modules.record.profile_memory import RecordProfileMemory
from modules.record.profile_memory import RecordProfileReadError


def _subject(user_id, value):
    if not isinstance(value, ProfileSubjectV1):
        raise RecordProfileReadError('Profile returned an unsupported subject contract')
    value = ProfileSubjectV1.model_validate(value.model_dump(mode='python'))
    relationship = value.relationship or {}
    return RecordProfileSubjectV1(
        user_id=user_id,
        subject_id=value.subject_id,
        kind=value.kind,
        status=value.status,
        version=value.version,
        display_name=value.display_name,
        relation_type=relationship.get('relation_type'),
        is_blood_relative=relationship.get('is_blood_relative') is True,
    )


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _view_token(page):
    watermarks, seen = [], set()
    for watermark in page.source_watermarks:
        if watermark.scope.user_id != page.scope.user_id or (
            watermark.scope.subject_id != page.scope.subject_id
            and page.scope.subject_id not in watermark.affected_subject_ids
        ):
            raise PermissionError('Profile source watermark is outside the selected subject')
        identity = (watermark.scope.subject_id, watermark.source_type, watermark.source_id)
        if identity in seen:
            raise RecordProfileReadError('Profile returned duplicate source watermarks')
        seen.add(identity)
        value = watermark.model_dump(mode='json')
        value['affected_subject_ids'] = sorted(value['affected_subject_ids'])
        watermarks.append(value)
    payload = {
        'contract': page.contract_version,
        'sort': page.sort_version,
        'scope': page.scope.model_dump(mode='json'),
        'subject': page.subject.model_dump(mode='json'),
        'profileRevision': page.profile_revision,
        'publicationStateAvailable': page.coverage.get('publicationStateAvailable'),
        'watermarks': sorted(watermarks, key=_canonical),
    }
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


class RecordProfilePageAdapter:
    def __init__(self, reader):
        self.reader = reader

    async def list_subjects(self, user_id):
        values = await self.reader.list_subjects(user_id)
        return tuple(_subject(user_id, value) for value in values)

    async def read_page(self, user_id, *, subject_id=None, page_size=300, statuses=(), cursor=None):
        original = await self.reader.read_page(
            user_id, subject_id=subject_id, page_size=page_size, statuses=statuses, cursor=cursor
        )
        if not isinstance(original, ProfileFactPageV1):
            raise RecordProfileReadError('Profile returned an unsupported page contract')
        # Detach mutable Any values before another await. Keep Python values for
        # Record's own legacy-safe renderer instead of pre-serializing them.
        page = ProfileFactPageV1.model_validate(original.model_dump(mode='python'))
        if (
            page.contract_version != 'profile-fact-page.v1'
            or page.sort_version != 'effective-start-desc-fact-id-asc.v1'
        ):
            raise RecordProfileReadError('The fixed Profile page version is unavailable')
        if (
            page.user_id != user_id
            or page.scope is not None
            and (page.scope.user_id != user_id or subject_id is not None and page.scope.subject_id != subject_id)
        ):
            raise PermissionError('Profile page is outside the requested scope')
        if page.status == 'ready':
            if page.scope is None or page.subject is None or page.subject.subject_id != page.scope.subject_id:
                raise PermissionError('Profile page subject differs from its authorized scope')
            if page.profile_revision is None:
                raise RecordProfileReadError('Profile returned no current revision')
            subject = _subject(user_id, page.subject)
            token = _view_token(page)
        else:
            subject, token = None, None
        facts = tuple(
            RecordProfileFactV1.capture(
                user_id=user_id,
                subject_id=page.scope.subject_id if page.scope else '',
                fact_id=value.fact_id,
                fact_type=value.fact_type,
                category=value.category,
                display_name=value.display_name,
                status=value.status,
                assertion_level=value.assertion_level,
                verification_status=value.verification_status,
                effective_at=value.effective_period['start'],
                source_types=tuple(source.source_type for source in value.sources),
                **profile_age_context(page, value),
            )
            for value in page.facts
        )
        return RecordProfilePageV1(
            user_id=page.user_id,
            status=page.status,
            page_id=page.page_id,
            subject=subject,
            facts=facts,
            view_token=token,
            next_cursor=page.next_cursor,
            truncated=page.truncated,
            revision_stable=page.coverage.get('revisionStable') is True,
            stale=page.stale,
            error_code=page.error_code,
        )


class RecordDocumentSupplementAdapter:
    def __init__(self, reader):
        self.reader = reader

    async def read(self, *, user_id, subject_id, limit=100):
        scope = DocumentScope(user_id, subject_id)
        page = await self.reader.read(scope=scope, limit=limit)
        if not isinstance(page, DocumentObservationPageV1) or page.contract_version != 'document.observation-read.v1':
            raise RecordProfileReadError('The fixed Document observation contract is unavailable')
        if page.scope != scope:
            raise PermissionError('Document observations are outside the requested scope')
        items = page.items
        if len(items) > limit or page.truncated and len(items) != limit:
            raise RecordProfileReadError('Document observation page violates the requested bound')
        # The legacy supplementary JSON keeps observation field insertion order.
        return RecordSupplementPageV1.capture(user_id=user_id, subject_id=subject_id, items=items)


def create_record_profile_memory(profile_reader, *, document_observations=None):
    """Compose without I/O; root owns public readers and their resource lifetimes."""
    return RecordProfileMemory(
        RecordProfilePageAdapter(profile_reader),
        supplements=(
            RecordDocumentSupplementAdapter(document_observations) if document_observations is not None else None
        ),
    )
