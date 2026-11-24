"""Pure, bounded original observations; Profile owns their ingestion status."""

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Protocol

from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest

OBSERVATION_READ_CONTRACT = 'document.observation-read.v1'


class DocumentObservationReadError(RuntimeError):
    code = 'document.observation_read_unavailable'


class DocumentObservationSource(Protocol):
    async def read_first(self, *, scope: DocumentScope, limit: int) -> list[dict]: ...


class DocumentObservationIngestionReader(Protocol):
    async def read_status(self, *, scope: DocumentScope, source_id: str, source_revision: int) -> str: ...


@dataclass(frozen=True, slots=True)
class DocumentObservationPageV1:
    scope: DocumentScope
    read_at: datetime
    _items_json: str
    truncated: bool
    contract_version: str = OBSERVATION_READ_CONTRACT

    @property
    def items(self):
        return tuple(json.loads(self._items_json))

    @property
    def snapshot_id(self):
        return 'document_observations_' + digest(
            {
                'scope': self.scope.as_dict(),
                'contract': self.contract_version,
                'itemsJson': self._items_json,
                'truncated': self.truncated,
            }
        )


class DocumentObservationReader:
    def __init__(self, *, source: DocumentObservationSource, ingestion: DocumentObservationIngestionReader, clock=None):
        self.source = source
        self.ingestion = ingestion
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def read(self, *, scope: DocumentScope, limit: int = 100) -> DocumentObservationPageV1:
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit DocumentScope is required')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        read_at = self.clock()
        if not isinstance(read_at, datetime) or read_at.utcoffset() is None:
            raise ValueError('a timezone-aware read time is required')
        rows = await self.source.read_first(scope=scope, limit=limit + 1)
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise DocumentObservationReadError('observation source violated its bounded read contract')
        rows = deepcopy(rows)
        items, statuses = [], {}
        previous_id = None
        for row in rows:
            if not isinstance(row, dict) or any(row.get(key) != value for key, value in scope.as_dict().items()):
                raise PermissionError('observation source returned an item outside the requested scope')
            identity = row.get('_id')
            if not isinstance(identity, str) or not identity or previous_id is not None and identity <= previous_id:
                raise DocumentObservationReadError('observation source order or identity is invalid')
            previous_id = identity
        for row in rows[:limit]:
            source_id, revision = row.get('canonicalReportId'), row.get('ownerRevision')
            if (
                not isinstance(source_id, str)
                or not source_id
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
            ):
                raise DocumentObservationReadError('observation source revision is unavailable')
            key = (source_id, int(revision))
            if key not in statuses:
                status = await self.ingestion.read_status(
                    scope=scope, source_id=source_id, source_revision=int(revision)
                )
                if not isinstance(status, str) or not status.strip():
                    raise DocumentObservationReadError('ingestion status is unavailable')
                statuses[key] = status
            if not isinstance(row.get('observation'), dict) or row.get('dateStatus') not in {'known', 'unknown'}:
                raise DocumentObservationReadError('observation content is malformed')
            items.append(
                {
                    'observationId': row['_id'],
                    'documentId': row['documentId'],
                    'documentIds': row['documentIds'],
                    'canonicalReportId': source_id,
                    'sourceRevision': int(revision),
                    'sourcePointer': row['sourcePointer'],
                    'observation': row['observation'],
                    'observedAt': row['observedAt'],
                    'dateStatus': row['dateStatus'],
                    'ingestionStatus': statuses[key],
                    'factEligibility': 'date_unknown' if row['dateStatus'] == 'unknown' else 'subject_to_fact_policy',
                }
            )
        # Copy all values into a bounded immutable read result. This is a read
        # projection, not a transaction snapshot or a newly governed health fact.
        return DocumentObservationPageV1(
            scope, read_at, json.dumps(items, ensure_ascii=False, allow_nan=False), len(rows) > limit
        )
