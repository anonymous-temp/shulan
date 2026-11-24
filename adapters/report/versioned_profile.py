"""Report-owned rendering input from Profile's public structured snapshots."""

from __future__ import annotations

from dataclasses import replace

from adapters.report.profile_snapshot import MAX_FACTS
from adapters.report.profile_snapshot import READ_STATUSES
from adapters.report.profile_snapshot import READ_SUBJECT_KINDS
from adapters.report.profile_snapshot import SELF_SUBJECT_KIND
from adapters.report.profile_snapshot import _to_fact
from modules.report.ports import ReportProfileSnapshot
from utils.log import getLogger

logger = getLogger(__name__)


class VersionedReportProfileGateway:
    def __init__(self, reader):
        self.reader = reader

    async def read(self, user_id):
        try:
            for attempt in range(2):
                subjects = tuple(
                    item
                    for item in await self.reader.list_subjects(user_id)
                    if item.kind in READ_SUBJECT_KINDS and item.status == 'active'
                )
                self_subject = next((item for item in subjects if item.kind == SELF_SUBJECT_KIND), None)
                if self_subject is None:
                    return None
                # Keep Profile's public ordering; the previous domain adapter
                # used the same subject order and global 300-fact allowance.
                facts, snapshots = [], []
                truncated = False
                for subject in subjects:
                    remaining = MAX_FACTS - len(facts)
                    if remaining == 0:
                        truncated = True
                        break
                    snapshot = await self.reader.read(
                        user_id, subject_id=subject.subject_id, max_facts=remaining, statuses=READ_STATUSES
                    )
                    if snapshot.contract_version != 'profile-snapshot.v1' or snapshot.status != 'ready':
                        return None
                    if (
                        snapshot.user_id != user_id
                        or snapshot.scope is None
                        or snapshot.scope.user_id != user_id
                        or snapshot.scope.subject_id != subject.subject_id
                        or snapshot.subject is None
                        or snapshot.subject.kind != subject.kind
                    ):
                        raise PermissionError('Profile snapshot identity differs from requested scope')
                    snapshots.append(snapshot)
                    for value in snapshot.facts:
                        fact = _to_fact(
                            value.model_dump(mode='json', by_alias=True),
                            subject_id=subject.subject_id,
                            subject_kind=subject.kind,
                        )
                        if fact is not None:
                            facts.append(fact)
                    truncated = truncated or snapshot.truncated
                    if truncated:
                        break
                self_snapshot = next(
                    (item for item in snapshots if item.scope.subject_id == self_subject.subject_id), None
                )
                if self_snapshot is None:
                    # Preserve the legacy subject ordering and bounded facts.
                    # Read only self metadata if relatives used the allowance.
                    self_snapshot = await self.reader.read(
                        user_id, subject_id=self_subject.subject_id, max_facts=1, statuses=READ_STATUSES
                    )
                    if (
                        self_snapshot.contract_version != 'profile-snapshot.v1'
                        or self_snapshot.status != 'ready'
                        or self_snapshot.scope is None
                        or self_snapshot.scope.user_id != user_id
                        or self_snapshot.scope.subject_id != self_subject.subject_id
                    ):
                        return None
                    snapshots.append(self_snapshot)
                result = ReportProfileSnapshot(
                    user_id=user_id,
                    self_subject_id=self_subject.subject_id,
                    read_at=self_snapshot.read_at.isoformat(),
                    profile_version=self_snapshot.profile_revision,
                    facts=tuple(facts),
                    truncated=truncated,
                    truncation_reason=f'facts exceeded {MAX_FACTS}' if truncated else None,
                    stale=any(item.stale for item in snapshots),
                )
                stable = await self._stable(user_id, subjects, snapshots)
                if stable:
                    return result
                if attempt == 1:
                    return replace(result, stale=True)
            return None
        except Exception as error:
            logger.warning(
                'report public profile snapshot unavailable: user_id=%s error_type=%s', user_id, type(error).__name__
            )
            return None

    async def _stable(self, user_id, subjects, snapshots):
        current = tuple(
            item
            for item in await self.reader.list_subjects(user_id)
            if item.kind in READ_SUBJECT_KINDS and item.status == 'active'
        )
        if current != subjects:
            return False
        for original in snapshots:
            check = await self.reader.read(
                user_id, subject_id=original.scope.subject_id, max_facts=1, statuses=READ_STATUSES
            )
            if (
                check.status != 'ready'
                or check.scope != original.scope
                or check.subject != original.subject
                or check.profile_revision is None
                or check.profile_revision != original.profile_revision
                or check.source_watermarks != original.source_watermarks
            ):
                return False
        return True
