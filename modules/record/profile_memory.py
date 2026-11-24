"""Bounded full-page Profile reads with Record-owned selection and rendering."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Protocol

from modules.record.profile_context import RecordProfileFactV1
from modules.record.profile_context import RecordProfilePageV1
from modules.record.profile_context import RecordProfileRendererV1
from modules.record.profile_context import RecordProfileSubjectV1
from modules.record.profile_context import RecordSupplementPageV1
from modules.record.profile_context import _positive_int
from modules.record.profile_context import _required_text


class RecordProfilePagePort(Protocol):
    async def list_subjects(self, user_id: str) -> tuple[RecordProfileSubjectV1, ...]: ...
    async def read_page(
        self,
        user_id: str,
        *,
        subject_id: str | None = None,
        page_size: int = 300,
        statuses: tuple[str, ...] = ('active', 'pending', 'disputed'),
        cursor: str | None = None,
    ) -> RecordProfilePageV1: ...


class RecordSupplementPort(Protocol):
    async def read(self, *, user_id: str, subject_id: str, limit: int = 100) -> RecordSupplementPageV1: ...


class RecordProfileReadError(RuntimeError):
    """The whole Profile input is unavailable; never render a partial page prefix."""

    code = 'record.profile_read_unavailable'


class _RestartRequired(Exception):
    pass


class RecordProfileMemory:
    """Read-only context provider. Construction never opens resources or reads data."""

    def __init__(
        self,
        reader: RecordProfilePagePort,
        *,
        supplements: RecordSupplementPort | None = None,
        renderer: RecordProfileRendererV1 | None = None,
        page_size=300,
        max_pages=100,
        timeout_seconds=5.0,
        clock=time.monotonic,
    ):
        self.reader, self.supplements = reader, supplements
        self.renderer = renderer or RecordProfileRendererV1()
        self.page_size = _positive_int('page_size', page_size)
        if self.page_size > 1000:
            raise ValueError('page_size must not exceed the public page contract')
        self.max_pages = _positive_int('max_pages', max_pages)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError('timeout_seconds must be positive')
        self.timeout_seconds, self.clock = timeout_seconds, clock

    async def build_context(self, user_id):
        user_id = _required_text('user_id', user_id)
        budget = {'pages': 0, 'deadline': self.clock() + self.timeout_seconds}
        try:
            async with asyncio.timeout(self.timeout_seconds):
                subject, candidates = await self._complete_input(user_id, budget)
        except TimeoutError as error:
            raise RecordProfileReadError('Profile read exceeded its time budget') from error
        if subject is None:
            return None
        context = self.renderer.render(candidates)
        if self.supplements is not None and (remaining := budget['deadline'] - self.clock()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    page = await self.supplements.read(user_id=user_id, subject_id=subject.subject_id, limit=100)
                if not isinstance(page, RecordSupplementPageV1) or (page.user_id, page.subject_id) != (
                    user_id,
                    subject.subject_id,
                ):
                    raise PermissionError('supplementary observations are outside the selected self scope')
                context = self.renderer.supplement(context, page)
            except Exception:
                # Optional source evidence never discards a complete fact context.
                pass
        return context or None

    async def _complete_input(self, user_id, budget):
        for attempt in range(2):
            try:
                subject, facts = await self._subject_facts(user_id, None, budget)
                if subject is None:
                    return None, ()
                candidates = [
                    (subject, fact)
                    for fact in facts
                    if fact.category != 'family_history' or fact.fact_type == 'family_history.reported'
                ]
                subjects = tuple(await self.reader.list_subjects(user_id))
                if any(not isinstance(item, RecordProfileSubjectV1) or item.user_id != user_id for item in subjects):
                    raise PermissionError('Profile subjects are outside the requested user scope')
                if len({item.subject_id for item in subjects}) != len(subjects):
                    raise RecordProfileReadError('Profile subject identities are not unique')
                relatives = sorted(
                    (item for item in subjects if item.is_family),
                    key=lambda item: (item.relation_type or '', item.display_name or '', item.subject_id),
                )
                for relative in relatives:
                    fixed, facts = await self._subject_facts(user_id, relative.subject_id, budget, expected=relative)
                    candidates.extend((fixed, fact) for fact in facts if fact.category == 'family_history')
                return subject, tuple(candidates)
            except _RestartRequired:
                if attempt:
                    raise RecordProfileReadError('Profile changed during both complete read attempts') from None
        raise AssertionError('unreachable')

    async def _subject_facts(self, user_id, subject_id, budget, *, expected=None):
        cursor = None
        subject, view_token = None, None
        facts, seen_facts, seen_cursors, seen_pages = [], set(), set(), set()
        while True:
            if budget['pages'] >= self.max_pages or self.clock() >= budget['deadline']:
                raise RecordProfileReadError('Profile read exceeded its aggregate page/time budget')
            budget['pages'] += 1
            page = await self.reader.read_page(
                user_id,
                subject_id=subject_id,
                page_size=self.page_size,
                statuses=('active', 'pending', 'disputed'),
                cursor=cursor,
            )
            if not isinstance(page, RecordProfilePageV1) or page.user_id != user_id:
                raise PermissionError('Profile page is outside the requested user scope')
            if page.status != 'ready':
                if page.facts or page.next_cursor:
                    raise RecordProfileReadError('A nonready Profile page contains facts or continuation')
                if page.status == 'restart_required':
                    raise _RestartRequired
                if page.status == 'missing':
                    if cursor is None and subject_id is None:
                        return None, ()
                    raise _RestartRequired
                raise RecordProfileReadError('Profile page is unavailable')
            if not isinstance(page.subject, RecordProfileSubjectV1) or (
                page.subject.user_id != user_id
                or page.subject.status != 'active'
                or (subject_id is not None and page.subject.subject_id != subject_id)
                or (subject_id is None and page.subject.kind != 'self')
            ):
                raise PermissionError('Profile page subject does not match the selected scope')
            if expected is not None and page.subject != expected:
                raise _RestartRequired
            if not page.revision_stable or not page.view_token:
                raise _RestartRequired
            if subject is None:
                subject, view_token = page.subject, page.view_token
            elif (page.subject, page.view_token) != (subject, view_token):
                raise _RestartRequired
            if page.truncated != bool(page.next_cursor) or len(page.facts) > self.page_size:
                raise RecordProfileReadError('Profile page has an invalid completeness boundary')
            if not page.page_id or page.page_id in seen_pages or (page.next_cursor and not page.facts):
                raise RecordProfileReadError('Profile pagination made no progress')
            seen_pages.add(page.page_id)
            for fact in page.facts:
                if not isinstance(fact, RecordProfileFactV1) or (fact.user_id, fact.subject_id) != (
                    user_id,
                    subject.subject_id,
                ):
                    raise PermissionError('Profile fact is outside the selected subject')
                if fact.fact_id in seen_facts or fact.status not in ('active', 'pending', 'disputed'):
                    raise RecordProfileReadError('Profile page contains duplicate or excluded facts')
                seen_facts.add(fact.fact_id)
                facts.append(fact)
            if page.next_cursor is None:
                return subject, tuple(facts)
            if page.next_cursor in seen_cursors:
                raise RecordProfileReadError('Profile continuation cursor repeated')
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
