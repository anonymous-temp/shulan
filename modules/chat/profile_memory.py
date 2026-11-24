"""Chat's bounded, complete Profile read orchestration; no provider internals."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Protocol

from .profile_context import ChatProfileFactV1
from .profile_context import ChatProfilePageV1
from .profile_context import ChatProfileRendererV1
from .profile_context import ChatProfileSubjectV1
from .profile_context import ChatSupplementPageV1
from .profile_context import _positive_int
from .profile_context import _required_text


class ChatProfilePagePort(Protocol):
    async def list_subjects(self, user_id: str) -> tuple[ChatProfileSubjectV1, ...]: ...

    async def read_page(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        page_size: int = 300,
        statuses: tuple[str, ...] = ('active', 'pending', 'disputed'),
        cursor: str | None = None,
    ) -> ChatProfilePageV1: ...


class ChatSupplementPort(Protocol):
    async def read(self, *, user_id: str, subject_id: str, limit: int = 100) -> ChatSupplementPageV1: ...


class ChatProfileReadError(RuntimeError):
    code = 'chat.profile_read_unavailable'


class _Changed(Exception):
    pass


@dataclass
class _Budget:
    deadline: float
    pages: int = 0


class ChatProfileMemory:
    """Build one transient request context without creating or changing Profile."""

    def __init__(
        self,
        reader: ChatProfilePagePort,
        *,
        supplements: ChatSupplementPort | None = None,
        renderer: ChatProfileRendererV1 | None = None,
        page_size=300,
        max_pages=100,
        timeout_seconds=5.0,
        clock=time.monotonic,
    ):
        self.reader, self.supplements = reader, supplements
        self.renderer = renderer or ChatProfileRendererV1()
        self.page_size = _positive_int('page_size', page_size)
        self.max_pages = _positive_int('max_pages', max_pages)
        if self.page_size > 1000:
            raise ValueError('page_size must not exceed 1000')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError('timeout_seconds must be finite and positive')
        self.timeout_seconds, self.clock = timeout_seconds, clock

    async def build_context(self, user_id: str) -> str | None:
        user_id = _required_text('user_id', user_id)
        budget = _Budget(self.clock() + self.timeout_seconds)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                subject, facts = await self._complete(user_id, budget)
        except TimeoutError as error:
            raise ChatProfileReadError('Profile read exceeded its time budget') from error
        if subject is None:
            return None
        context = self.renderer.render(facts)
        remaining = budget.deadline - self.clock()
        if self.supplements is not None and remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    rows = await self.supplements.read(user_id=user_id, subject_id=subject.subject_id, limit=100)
                if not isinstance(rows, ChatSupplementPageV1) or (rows.user_id, rows.subject_id) != (
                    user_id,
                    subject.subject_id,
                ):
                    raise PermissionError('supplement scope does not match the selected self')
                context = self.renderer.supplement(context, rows)
            except Exception:
                # Optional original observations cannot remove a completed fact context.
                pass
        return context or None

    async def _complete(self, user_id, budget):
        for attempt in range(2):
            try:
                subject, facts = await self._all_pages(user_id, None, budget)
                if subject is None:
                    return None, ()
                candidates = [
                    (subject, fact)
                    for fact in facts
                    if fact.category != 'family_history' or fact.fact_type == 'family_history.reported'
                ]
                subjects = tuple(await self.reader.list_subjects(user_id))
                if any(not isinstance(row, ChatProfileSubjectV1) or row.user_id != user_id for row in subjects):
                    raise PermissionError('subject listing belongs to another user')
                if len({row.subject_id for row in subjects}) != len(subjects):
                    raise ChatProfileReadError('subject listing contains duplicate identities')
                relatives = sorted(
                    (row for row in subjects if row.is_family),
                    key=lambda row: (row.relation_type or '', row.display_name or '', row.subject_id),
                )
                for relative in relatives:
                    selected, family_facts = await self._all_pages(
                        user_id, relative.subject_id, budget, expected=relative
                    )
                    candidates.extend((selected, fact) for fact in family_facts if fact.category == 'family_history')
                return subject, tuple(candidates)
            except _Changed:
                if attempt == 1:
                    raise ChatProfileReadError('Profile changed during both complete attempts') from None
        raise AssertionError('unreachable')

    async def _all_pages(self, user_id, subject_id, budget, *, expected=None):
        cursor, fixed_subject, token = None, None, None
        result, fact_ids, page_ids, cursors = [], set(), set(), set()
        while True:
            if budget.pages >= self.max_pages or self.clock() >= budget.deadline:
                raise ChatProfileReadError('aggregate Profile read budget exhausted')
            budget.pages += 1
            page = await self.reader.read_page(
                user_id,
                subject_id=subject_id,
                page_size=self.page_size,
                statuses=('active', 'pending', 'disputed'),
                cursor=cursor,
            )
            if not isinstance(page, ChatProfilePageV1) or page.user_id != user_id:
                raise PermissionError('Profile page belongs to another user')
            if page.status != 'ready':
                if page.facts or page.next_cursor:
                    raise ChatProfileReadError('nonready Profile page contains facts or a cursor')
                if page.status == 'missing' and cursor is None and subject_id is None:
                    return None, ()
                if page.status in ('restart_required', 'missing'):
                    raise _Changed
                raise ChatProfileReadError('Profile page unavailable')
            subject = page.subject
            if (
                not isinstance(subject, ChatProfileSubjectV1)
                or subject.user_id != user_id
                or subject.status != 'active'
                or (subject_id is None and subject.kind != 'self')
                or (subject_id is not None and subject.subject_id != subject_id)
            ):
                raise PermissionError('Profile page subject does not match the selected scope')
            if expected is not None and subject != expected:
                raise _Changed
            if not page.revision_stable or not page.view_token:
                raise _Changed
            if fixed_subject is None:
                fixed_subject, token = subject, page.view_token
            elif fixed_subject != subject or token != page.view_token:
                raise _Changed
            if page.truncated != bool(page.next_cursor) or len(page.facts) > self.page_size:
                raise ChatProfileReadError('incomplete Profile page boundary')
            if not page.page_id or page.page_id in page_ids or (page.next_cursor and not page.facts):
                raise ChatProfileReadError('Profile pagination did not advance')
            page_ids.add(page.page_id)
            for fact in page.facts:
                if not isinstance(fact, ChatProfileFactV1) or (fact.user_id, fact.subject_id) != (
                    user_id,
                    subject.subject_id,
                ):
                    raise PermissionError('Profile fact does not match the selected scope')
                if fact.fact_id in fact_ids or fact.status not in ('active', 'pending', 'disputed'):
                    raise ChatProfileReadError('duplicate or excluded Profile fact')
                fact_ids.add(fact.fact_id)
                result.append(fact)
            if page.next_cursor is None:
                return fixed_subject, tuple(result)
            if page.next_cursor in cursors:
                raise ChatProfileReadError('Profile continuation cursor repeated')
            cursors.add(page.next_cursor)
            cursor = page.next_cursor
