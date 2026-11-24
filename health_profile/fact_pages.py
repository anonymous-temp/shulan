"""Pure Profile pagination; never bootstrap subjects or repair materialized views."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from pydantic import TypeAdapter

from .contracts import ProfileFactPageV1
from .contracts import ProfileScope
from .contracts import profile_content_digest
from .models import FactCategory
from .models import FactStatus
from .models import SubjectStatus
from .pagination import decode_cursor
from .pagination import encode_cursor
from .snapshots import _fact
from .snapshots import _identifier
from .snapshots import _subject

_CONTRACT = 'profile-fact-page.v1'
_SORT = 'effective-start-desc-fact-id-asc.v1'
_CURSOR_KIND = 'profile-public-fact-page'


async def read_fact_page(
    repository,
    *,
    publications,
    clock,
    user_id,
    subject_id,
    page_size,
    statuses,
    categories,
    fact_types,
    effective_from,
    effective_to,
    cursor,
) -> ProfileFactPageV1:
    # Validate request/cursor before any I/O. Cursors bind a query, not permission;
    # all repository access still uses the authenticated caller's user/scope.
    user_id = _identifier(user_id)
    subject_id = None if subject_id is None else _identifier(subject_id)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
        raise ValueError('page_size must be an integer between 1 and 1000')
    statuses = tuple(FactStatus(value) for value in _values(statuses))
    categories = tuple(FactCategory(value) for value in _values(categories))
    fact_types = _values(fact_types)
    if not statuses or FactStatus.DELETED in statuses:
        raise ValueError('statuses must select public, non-deleted facts')
    effective_from = _input_time(effective_from)
    effective_to = _input_time(effective_to)
    if effective_from is not None and effective_to is not None and effective_from > effective_to:
        raise ValueError('effective_from cannot follow effective_to')
    filters = {
        'statuses': [item.value for item in statuses],
        'categories': [item.value for item in categories],
        'factTypes': list(fact_types),
        'effectiveFrom': effective_from.isoformat() if effective_from else None,
        'effectiveTo': effective_to.isoformat() if effective_to else None,
    }
    request_key = profile_content_digest(
        {
            'userId': user_id,
            'requestedSubjectId': subject_id,
            'pageSize': page_size,
            'contractVersion': _CONTRACT,
            'sortVersion': _SORT,
            **filters,
        }
    )
    saved = _decode(cursor, request_key) if cursor is not None else None
    read_at = _input_time(clock())
    if read_at is None:
        raise ValueError('clock must return a timezone-aware datetime')
    base = {'user_id': user_id, 'read_at': read_at}
    try:
        subject = (
            await repository.get_self_subject(user_id)
            if subject_id is None
            else await repository.get_subject(user_id, subject_id)
        )
        if subject is None or subject.status is not SubjectStatus.ACTIVE:
            return _page(**base, status='missing', coverage={'reason': 'subject_missing'})
        if subject.user_id != user_id or (subject_id is not None and subject.subject_id != subject_id):
            raise PermissionError('profile subject is outside the requested scope')
        scope = ProfileScope(user_id=user_id, subject_id=subject.subject_id)
        base['scope'] = scope
        before = await _state(repository, publications, scope, subject, as_of=read_at)
        if before is None:
            return _page(**base, status='unavailable', error_code='profile_revision_unavailable')
        state_key, revision, watermarks, info = before
        if saved is not None and (saved['scope'] != scope.model_dump(by_alias=True) or saved['stateKey'] != state_key):
            return _restart(base)
        query = dict(
            categories=categories,
            statuses=statuses,
            fact_types=fact_types,
            effective_from=effective_from,
            effective_to=effective_to,
            after=saved['after'] if saved else None,
            limit=page_size + 1,
        )
        basic = getattr(repository, 'basic_info', None)
        facts = (
            await basic.list_current_fact_page(user_id, scope.subject_id, snapshot=info, **query)
            if basic
            else await repository.list_fact_page(user_id, scope.subject_id, **query)
        )
        if any(item.user_id != user_id or item.subject_id != scope.subject_id for item in facts):
            raise PermissionError('profile fact is outside the requested scope')
        after_subject = await repository.get_subject(user_id, scope.subject_id)
        if after_subject is None or after_subject.status is not SubjectStatus.ACTIVE:
            return _restart(base)
        after = await _state(repository, publications, scope, after_subject, as_of=read_at)
        if after is None or after[0] != state_key:
            return _restart(base)
        items = facts[:page_size]
        more = len(facts) > page_size
        next_cursor = None
        if more:
            last = items[-1]
            next_cursor = encode_cursor(
                _CURSOR_KIND,
                {
                    'requestKey': request_key,
                    'stateKey': state_key,
                    'scope': scope.model_dump(by_alias=True),
                    'contractVersion': _CONTRACT,
                    'sortVersion': _SORT,
                    'start': _stored_time(last.effective_period.start).isoformat()
                    if last.effective_period.start
                    else None,
                    'factId': last.fact_id,
                },
            )
        applying = any(item.applying_publication_revision is not None for item in watermarks)
        pending = any(item.accepted_publication_revision > item.settled_publication_revision for item in watermarks)
        age = _current_age(info)
        return _page(
            **base,
            status='ready',
            profile_revision=revision,
            subject=_subject(subject),
            facts=tuple(_public_fact(item) for item in items),
            next_cursor=next_cursor,
            truncated=more,
            stale=applying or pending,
            source_watermarks=watermarks,
            coverage={
                **filters,
                'pageSize': page_size,
                'returnedFacts': len(items),
                'consistency': 'optimistic_revision_check',
                'revisionStable': True,
                'publicationApplying': applying,
                'publicationPending': pending,
                'publicationStateAvailable': publications is not None,
                **({'basicInfoRevision': info.revision} if info else {}),
                **({'currentAge': age} if age and any(item.fact_id == age['factId'] for item in items) else {}),
            },
        )
    except Exception:
        # Do not expose exception text, internal collection names or health values.
        return _page(**base, status='unavailable', error_code='profile_read_unavailable')


async def _state(repository, publications, scope, subject, *, as_of=None):
    if subject.user_id != scope.user_id or subject.subject_id != scope.subject_id:
        raise PermissionError('subject changed ownership')
    view = await repository.get_view(scope.user_id, scope.subject_id)
    if view is None:
        return None
    if view.user_id != scope.user_id or view.subject_id != scope.subject_id:
        raise PermissionError('profile view is outside the requested scope')
    basic = getattr(repository, 'basic_info', None)
    info = await basic.read(scope.user_id, subject_id=scope.subject_id, as_of=as_of) if basic else None
    if (
        info is not None
        and info.revision
        and (view.basic_info_lifecycle_epoch != info.lifecycle_epoch or view.basic_info_revision < info.revision)
    ):
        return None
    watermarks = tuple(await publications.list_watermarks(scope)) if publications is not None else ()
    if any(
        item.scope.user_id != scope.user_id
        or (item.scope.subject_id != scope.subject_id and scope.subject_id not in item.affected_subject_ids)
        for item in watermarks
    ):
        raise PermissionError('publication watermark is outside the requested scope')
    # Ordering is not part of publication identity; the provider may return rows
    # in different database order while nothing has changed.
    watermarks = tuple(
        sorted(
            watermarks, key=lambda item: (item.scope.user_id, item.scope.subject_id, item.source_type, item.source_id)
        )
    )
    age = _current_age(info)
    key = profile_content_digest(
        {
            'profileRevision': view.profile_version,
            'subject': _subject(subject).model_dump(mode='json', by_alias=True),
            'publicationStateAvailable': publications is not None,
            'sourceWatermarks': [item.model_dump(mode='json', by_alias=True) for item in watermarks],
            **({'basicInfoRevision': info.revision, 'lifecycleEpoch': info.lifecycle_epoch} if info else {}),
            **({'currentAge': {key: value for key, value in age.items() if key != 'asOf'}} if age else {}),
        }
    )
    return key, view.profile_version, watermarks, info


def _current_age(info):
    if info is None:
        return None
    age = info.fields.get('age')
    if age is None or age.fact_id is None or type(age.value) is not int:
        return None
    return {
        'factId': age.fact_id,
        'value': age.value,
        'ageBasis': age.age_basis,
        'asOf': (age.as_of or info.as_of).isoformat(),
        'observedAt': age.observed_at.isoformat() if isinstance(age.observed_at, datetime) else age.observed_at,
        'confirmedAt': age.confirmed_at.isoformat() if age.confirmed_at is not None else None,
    }


def _decode(cursor, request_key):
    if not isinstance(cursor, str) or len(cursor) > 16384:
        raise ValueError('cursor must be a bounded string')
    saved = decode_cursor(_CURSOR_KIND, cursor)
    if (
        type(saved.get('v')) is not int
        or saved.get('requestKey') != request_key
        or saved.get('contractVersion') != _CONTRACT
        or saved.get('sortVersion') != _SORT
    ):
        raise ValueError('cursor does not match the requested page contract or query')
    try:
        scope = ProfileScope.model_validate(saved['scope'])
        state_key = saved['stateKey']
        if (
            not isinstance(state_key, str)
            or len(state_key) != 64
            or any(c not in '0123456789abcdef' for c in state_key)
        ):
            raise ValueError('invalid state key')
        start = _input_time(datetime.fromisoformat(saved['start'])) if saved['start'] is not None else None
        fact_id = _identifier(saved['factId'])
        if len(fact_id) > 512:
            raise ValueError('fact ID is too long')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('cursor payload is invalid') from error
    return {'scope': scope.model_dump(by_alias=True), 'stateKey': state_key, 'after': (start, fact_id)}


def _values(values):
    if not isinstance(values, (tuple, list)) or len(values) > 100:
        raise ValueError('filters must be lists of at most 100 values')
    normalized = tuple(sorted({_identifier(value) for value in values}))
    if any(len(value) > 256 for value in normalized):
        raise ValueError('filter values cannot exceed 256 characters')
    return normalized


def _input_time(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('times must be timezone-aware datetimes')
    return value.astimezone(UTC)


def _stored_time(value):
    # PyMongo's default BSON decoding returns naive UTC, not local wall time.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _public_fact(value):
    public = _fact(value)
    return public.model_copy(
        update={
            'effective_period': {
                key: _stored_time(item) if item else None for key, item in public.effective_period.items()
            },
            'created_at': _stored_time(public.created_at),
            'updated_at': _stored_time(public.updated_at),
            'valid_until': _stored_time(public.valid_until) if public.valid_until else None,
            'sources': tuple(
                item.model_copy(update={'observed_at': _stored_time(item.observed_at) if item.observed_at else None})
                for item in public.sources
            ),
        }
    )


def _restart(base):
    return _page(**base, status='restart_required', stale=True, error_code='profile_page_revision_changed')


def _page(**kwargs):
    payload = TypeAdapter(dict).dump_python(
        {key: value for key, value in kwargs.items() if key != 'read_at'}, mode='json'
    )
    return ProfileFactPageV1(page_id='profile_page_' + profile_content_digest(payload), **kwargs)
