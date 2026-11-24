"""Pure reads of the single authoritative Profile; consumers own rendering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime

from .contracts import ProfileFactPageV1
from .contracts import ProfileFactV1
from .contracts import ProfileScope
from .contracts import ProfileSnapshotV1
from .contracts import ProfileSubjectV1
from .contracts import profile_content_digest
from .models import FactStatus
from .models import SubjectKind
from .models import SubjectStatus
from .repository import HealthProfileRepository


class ProfileSnapshotReader:
    def __init__(self, repository: HealthProfileRepository, *, publications=None, clock: Callable | None = None):
        self._repository = repository
        self._publications = publications
        self._clock = clock or (lambda: datetime.now(UTC))

    async def resolve_scope(self, user_id: str, *, subject_id: str | None = None) -> ProfileScope | None:
        """Resolve an existing active subject. No bootstrap, index creation or model calls."""
        user_id = _identifier(user_id)
        subject = (
            await self._repository.get_self_subject(user_id)
            if subject_id is None
            else await self._repository.get_subject(user_id, _identifier(subject_id))
        )
        if subject is None or subject.status is not SubjectStatus.ACTIVE:
            return None
        if subject.user_id != user_id:
            raise PermissionError('profile subject is outside the requested scope')
        return ProfileScope(user_id=user_id, subject_id=subject.subject_id)

    async def resolve_lifecycle(self, scope: ProfileScope) -> int | None:
        """Resolve the existing active subject without consulting publication state."""
        subject = await self._repository.get_subject(scope.user_id, scope.subject_id)
        if (
            subject is None
            or subject.user_id != scope.user_id
            or subject.subject_id != scope.subject_id
            or subject.status is not SubjectStatus.ACTIVE
        ):
            return None
        return subject.lifecycle_epoch

    async def resolve_existing_self_lifecycle(self, scope: ProfileScope) -> tuple[str, int, SubjectStatus] | None:
        """Check the selected self or discover a replacement without creating either."""
        subject = await self._repository.get_subject(scope.user_id, scope.subject_id)
        if subject is None:
            subject = await self._repository.get_self_subject(scope.user_id)
        if subject is None:
            return None
        if subject.user_id != scope.user_id or subject.kind != SubjectKind.SELF:
            raise PermissionError('self subject is outside the requested scope')
        return subject.subject_id, subject.lifecycle_epoch, subject.status

    async def resolve_self_lifecycle(self, scope: ProfileScope) -> int | None:
        """Resolve only the fixed active self subject; never create it."""
        subject = await self._repository.get_self_subject(scope.user_id)
        if (
            subject is None
            or subject.user_id != scope.user_id
            or subject.subject_id != scope.subject_id
            or subject.status is not SubjectStatus.ACTIVE
        ):
            return None
        return subject.lifecycle_epoch

    async def list_subjects(self, user_id: str) -> tuple[ProfileSubjectV1, ...]:
        user_id = _identifier(user_id)
        subjects = await self._repository.list_subjects(user_id)
        return tuple(_subject(item) for item in subjects if item.status is SubjectStatus.ACTIVE)

    async def read_page(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        page_size: int = 300,
        statuses: tuple[str, ...] = ('active', 'pending', 'disputed'),
        categories: tuple[str, ...] = (),
        fact_types: tuple[str, ...] = (),
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
        cursor: str | None = None,
    ) -> ProfileFactPageV1:
        """Pure structured paging; consumers own bounded restarts and rendering."""
        from .fact_pages import read_fact_page

        return await read_fact_page(
            self._repository,
            publications=self._publications,
            clock=self._clock,
            user_id=user_id,
            subject_id=subject_id,
            page_size=page_size,
            statuses=statuses,
            categories=categories,
            fact_types=fact_types,
            effective_from=effective_from,
            effective_to=effective_to,
            cursor=cursor,
        )

    async def read_v2(self, user_id, *, subject_id=None, max_facts=300, statuses=('active', 'pending', 'disputed')):
        """Explicit v2; v1 schemas and already captured v1 bytes remain unchanged."""
        from .basic_info_contracts import ProfileFactV2
        from .basic_info_contracts import ProfileSnapshotV2

        basic = getattr(self._repository, 'basic_info', None)
        if basic is None:
            raise RuntimeError('basic_info.provider_unavailable')
        for _ in range(2):
            original = await self.read(user_id, subject_id=subject_id, max_facts=max_facts, statuses=statuses)
            info = await basic.read(user_id, subject_id=subject_id, as_of=original.read_at)
            if info.revision == original.coverage.get('basicInfoRevision', 0):
                break
        else:
            original = original.model_copy(update={'stale': True})
        grades = await basic.fact_reliabilities(user_id, info.subject_id) if info.subject_id else {}
        selected = {item.fact_id for item in info.fields.values() if item.fact_id}
        payload = original.model_dump(exclude={'snapshot_id', 'contract_version', 'facts'})
        payload.update(
            basic_info=info,
            facts=tuple(
                ProfileFactV2(
                    **fact.model_dump(),
                    source_reliability=grades.get(fact.fact_id),
                    current_basic_info=(fact.fact_id in selected) if fact.fact_id in grades else None,
                )
                for fact in original.facts
            ),
        )
        from pydantic import TypeAdapter

        canonical = TypeAdapter(dict).dump_python(payload, mode='json')
        return ProfileSnapshotV2(snapshot_id='profile_snapshot_v2_' + profile_content_digest(canonical), **payload)

    async def read(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        max_facts: int = 300,
        statuses: tuple[str, ...] = ('active', 'pending', 'disputed'),
        required_publications: tuple[tuple[str, str, int], ...] = (),
    ) -> ProfileSnapshotV1:
        user_id = _identifier(user_id)
        subject_id = None if subject_id is None else _identifier(subject_id)
        if isinstance(max_facts, bool) or not isinstance(max_facts, int) or not 1 <= max_facts <= 1000:
            raise ValueError('max_facts must be an integer between 1 and 1000')
        fact_statuses = tuple(FactStatus(value) for value in statuses)
        if not fact_statuses or FactStatus.DELETED in fact_statuses:
            raise ValueError('statuses must select public, non-deleted facts')
        for source_type, source_id, revision in required_publications:
            _identifier(source_type)
            _identifier(source_id)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError('required publication revisions must be positive integers')
        read_at = self._clock()
        if read_at.tzinfo is None or read_at.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        base = {'user_id': user_id, 'read_at': read_at}
        try:
            scope = await self.resolve_scope(user_id, subject_id=subject_id)
            if scope is None:
                return _snapshot(**base, status='missing', coverage={'reason': 'subject_missing'})
            base['scope'] = scope
            watermarks = ()
            waiting = False
            for attempt in range(2):
                subject = await self._repository.get_subject(scope.user_id, scope.subject_id)
                if subject is None or subject.status is not SubjectStatus.ACTIVE:
                    return _snapshot(**base, status='missing', coverage={'reason': 'subject_missing'})
                before = await self._repository.get_view(scope.user_id, scope.subject_id)
                basic = getattr(self._repository, 'basic_info', None)
                info = await basic.read(user_id, subject_id=scope.subject_id, as_of=read_at) if basic else None
                query = dict(
                    categories=(),
                    statuses=fact_statuses,
                    fact_types=(),
                    effective_from=None,
                    effective_to=None,
                    after=None,
                    limit=max_facts + 1,
                )
                facts = (
                    await basic.list_current_fact_page(user_id, scope.subject_id, snapshot=info, **query)
                    if basic
                    else await self._repository.list_fact_page(user_id, scope.subject_id, **query)
                )
                if self._publications is not None:
                    watermarks = await self._publications.list_watermarks(scope)
                elif required_publications:
                    return _snapshot(**base, status='unavailable', error_code='publication_store_unavailable')
                by_source = {(row.source_type, row.source_id): row for row in watermarks}
                required_states = []
                for source_type, source_id, revision in required_publications:
                    watermark = by_source.get((source_type, source_id))
                    state = (
                        await self._publications.get_publication_state(
                            watermark.scope, source_type, source_id, revision
                        )
                        if watermark
                        else {'status': 'missing', 'applicationState': 'not_applied'}
                    )
                    required_states.append(
                        {'sourceType': source_type, 'sourceId': source_id, 'publicationRevision': revision, **state}
                    )
                waiting = any(row['applicationState'] != 'applied' for row in required_states)
                after = await self._repository.get_view(scope.user_id, scope.subject_id)
                stable = before is not None and after is not None and before.profile_version == after.profile_version
                if basic:
                    latest = await basic.read(user_id, subject_id=scope.subject_id, as_of=read_at)
                    stable = (
                        stable and info.revision == latest.revision and info.lifecycle_epoch == latest.lifecycle_epoch
                    )
                    stable = stable and (
                        not info.revision
                        or (
                            after.basic_info_lifecycle_epoch == info.lifecycle_epoch
                            and after.basic_info_revision >= info.revision
                        )
                    )
                if stable or attempt == 1:
                    break
            applying = any(row.applying_publication_revision is not None for row in watermarks)
            pending = any(row.accepted_publication_revision > row.settled_publication_revision for row in watermarks)
            return _snapshot(
                **base,
                status='ready',
                profile_revision=after.profile_version if after else None,
                subject=_subject(subject),
                facts=tuple(_fact(item) for item in facts[:max_facts]),
                truncated=len(facts) > max_facts,
                stale=not stable or applying or pending or waiting,
                source_watermarks=watermarks,
                coverage={
                    'statuses': [item.value for item in fact_statuses],
                    'returnedFacts': min(len(facts), max_facts),
                    'consistency': 'optimistic_revision_check',
                    'revisionStable': stable,
                    'publicationApplying': applying,
                    'publicationPending': pending,
                    'requiredPublicationsApplied': not waiting,
                    'requiredPublicationStates': required_states,
                    **({'basicInfoRevision': info.revision} if basic else {}),
                },
            )
        except Exception:
            # No exception text or health values escape the public error boundary.
            return _snapshot(**base, status='unavailable', error_code='profile_read_unavailable')


def _snapshot(**kwargs):
    payload = {key: value for key, value in kwargs.items() if key != 'read_at'}
    from pydantic import TypeAdapter

    canonical = TypeAdapter(dict).dump_python(payload, mode='json')
    return ProfileSnapshotV1(snapshot_id='profile_snapshot_' + profile_content_digest(canonical), **kwargs)


def _fact(value):
    fields = ProfileFactV1.model_fields
    return ProfileFactV1(**{key: item for key, item in value.model_dump().items() if key in fields})


def _subject(value):
    return ProfileSubjectV1(
        **{key: item for key, item in value.model_dump().items() if key in ProfileSubjectV1.model_fields}
    )


def _identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('identifier must be nonblank')
    return value.strip()
