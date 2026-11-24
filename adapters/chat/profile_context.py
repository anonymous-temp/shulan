"""Map fixed public Profile/Document reads into Chat-owned context inputs."""

from modules.analysis.observation_reader import DocumentObservationPageV1
from modules.analysis.snapshots import DocumentScope
from modules.chat.profile_context import ChatProfileFactV1
from modules.chat.profile_context import ChatProfilePageV1
from modules.chat.profile_context import ChatProfileSubjectV1
from modules.chat.profile_context import ChatSupplementPageV1
from modules.chat.profile_memory import ChatProfileMemory
from modules.chat.profile_memory import ChatProfileReadError
from modules.health_profile.contracts import ProfileFactPageV1
from modules.health_profile.contracts import ProfileSubjectV1
from modules.health_profile.contracts import profile_content_digest
from modules.health_profile.current_age import profile_age_context


class PublicChatProfilePages:
    def __init__(self, reader):
        self.reader = reader

    async def list_subjects(self, user_id):
        return tuple(_subject(user_id, item) for item in await self.reader.list_subjects(user_id))

    async def read_page(
        self, user_id, *, subject_id=None, page_size=300, statuses=('active', 'pending', 'disputed'), cursor=None
    ):
        page = await self.reader.read_page(
            user_id, subject_id=subject_id, page_size=page_size, statuses=statuses, cursor=cursor
        )
        if (
            not isinstance(page, ProfileFactPageV1)
            or page.contract_version != 'profile-fact-page.v1'
            or page.sort_version != 'effective-start-desc-fact-id-asc.v1'
        ):
            raise ChatProfileReadError('the fixed Profile page contract is unavailable')
        if (
            page.user_id != user_id
            or page.scope is not None
            and (page.scope.user_id != user_id or subject_id is not None and page.scope.subject_id != subject_id)
        ):
            raise PermissionError('Profile page belongs to another scope')
        if page.status != 'ready':
            if page.facts or page.next_cursor:
                raise ChatProfileReadError('a nonready public page contains facts or continuation')
            return ChatProfilePageV1(user_id, page.status, page.page_id, stale=page.stale, error_code=page.error_code)
        if (
            page.scope is None
            or page.subject is None
            or page.scope.subject_id != page.subject.subject_id
            or type(page.profile_revision) is not int
            or page.profile_revision < 1
        ):
            raise ChatProfileReadError('public Profile page has no complete identity or revision')
        subject = _subject(user_id, page.subject)
        watermarks = sorted(
            page.source_watermarks,
            key=lambda row: (row.scope.user_id, row.scope.subject_id, row.source_type, row.source_id),
        )
        if any(
            row.scope.user_id != user_id
            or (row.scope.subject_id != subject.subject_id and subject.subject_id not in row.affected_subject_ids)
            for row in watermarks
        ):
            raise PermissionError('Profile source watermark belongs to another scope')
        token = profile_content_digest(
            {
                'contract': page.contract_version,
                'sort': page.sort_version,
                'scope': page.scope.model_dump(mode='json', by_alias=True),
                'subject': page.subject.model_dump(mode='json', by_alias=True),
                'profileRevision': page.profile_revision,
                'publicationStateAvailable': page.coverage.get('publicationStateAvailable'),
                'sourceWatermarks': [row.model_dump(mode='json', by_alias=True) for row in watermarks],
            }
        )
        facts = tuple(
            ChatProfileFactV1.capture(
                user_id=user_id,
                subject_id=subject.subject_id,
                fact_id=row.fact_id,
                fact_type=row.fact_type,
                category=row.category,
                display_name=row.display_name,
                status=row.status,
                assertion_level=row.assertion_level,
                verification_status=row.verification_status,
                effective_at=row.effective_period['start'],
                source_types=tuple(source.source_type for source in row.sources),
                **profile_age_context(page, row),
            )
            for row in page.facts
        )
        return ChatProfilePageV1(
            user_id,
            'ready',
            page.page_id,
            subject,
            facts,
            token,
            page.next_cursor,
            page.truncated,
            page.coverage.get('revisionStable') is True,
            page.stale,
            page.error_code,
        )


class PublicChatSupplements:
    def __init__(self, reader):
        self.reader = reader

    async def read(self, *, user_id, subject_id, limit=100):
        scope = DocumentScope(user_id, subject_id)
        page = await self.reader.read(scope=scope, limit=limit)
        if not isinstance(page, DocumentObservationPageV1) or page.contract_version != 'document.observation-read.v1':
            raise ChatProfileReadError('the fixed Document observation contract is unavailable')
        if page.scope != scope:
            raise PermissionError('Document observations belong to another scope')
        items = page.items
        if len(items) > limit or page.truncated and len(items) != limit:
            raise ChatProfileReadError('Document observations exceeded the requested first page')
        return ChatSupplementPageV1.capture(user_id=user_id, subject_id=subject_id, items=items)


def create_chat_profile_context(*, profile_reader, observation_reader=None, **budgets):
    """Pure composition; application bootstrap owns the public provider resources."""
    return ChatProfileMemory(
        PublicChatProfilePages(profile_reader),
        supplements=PublicChatSupplements(observation_reader) if observation_reader is not None else None,
        **budgets,
    )


def _subject(user_id, value):
    if not isinstance(value, ProfileSubjectV1):
        raise ChatProfileReadError('the public subject contract is unavailable')
    relation = value.relationship
    if relation is not None and (
        not isinstance(relation.get('relation_type'), str) or type(relation.get('is_blood_relative')) is not bool
    ):
        raise ChatProfileReadError('the public relationship contract is unavailable')
    return ChatProfileSubjectV1(
        user_id,
        value.subject_id,
        value.kind,
        value.status,
        value.version,
        value.display_name,
        relation['relation_type'] if relation else None,
        relation['is_blood_relative'] if relation else False,
    )
