"""Versioned lifecycle-aware supplementary reads; v1 keeps its original semantics."""

import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from modules.analysis.application.extraction import validated_document_time
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.observation_reader import DocumentObservationPageV1
from modules.analysis.observation_reader import DocumentObservationReader
from modules.analysis.observation_reader import DocumentObservationReadError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest


class DocumentLifecycleLegacyIngestionGuard:
    """For v1, reject managed sources instead of returning an old 'completed'."""

    def __init__(self, *, lifecycle, ingestion):
        self.lifecycle = lifecycle
        self.ingestion = ingestion

    async def read_status(self, *, scope, source_id, source_revision):
        before = await self.lifecycle.read(scope=scope, source_id=source_id)
        if not before.legacy_observations_visible:
            raise DocumentObservationReadError('managed source requires document.observation-read.v2')
        status = await self.ingestion.read_status(scope=scope, source_id=source_id, source_revision=source_revision)
        if await self.lifecycle.read(scope=scope, source_id=source_id) != before:
            raise DocumentObservationReadError('source lifecycle changed during the legacy observation read')
        return status


@dataclass(frozen=True, slots=True)
class DocumentObservationCursorV2:
    scope: DocumentScope
    phase: str = 'legacy'
    legacy_after_id: str | None = None
    source_after_id: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentObservationPageV2:
    scope: DocumentScope
    read_at: datetime
    _legacy_json: str
    _managed_json: str
    scanned: int
    hidden: int
    scan_budget_exhausted: bool
    continuation: DocumentObservationCursorV2 | None
    contract_version: str = 'document.observation-read.v2'

    @property
    def legacy_items(self):
        return tuple(json.loads(self._legacy_json))

    @property
    def managed_sources(self):
        # These are fixed refs, not newly governed or consumer-rendered facts.
        return tuple(json.loads(self._managed_json))

    @property
    def truncated(self):
        return self.continuation is not None

    @property
    def snapshot_id(self):
        return 'document_observations_v2_' + digest(
            {
                'scope': self.scope.as_dict(),
                'legacy': self.legacy_items,
                'managed': self.managed_sources,
                'continuation': asdict(self.continuation) if self.continuation else None,
            }
        )


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def read_first(self, **kwargs):
        return self.rows


class DocumentLifecycleObservationReader:
    """Two bounded seek scans: legacy rows, then managed source material refs.

    Active event sources are not reconstructed from stale legacy rows. The caller
    can resolve their exact expected lifecycle through read_active_material.
    """

    def __init__(self, *, source, ingestion, lifecycle, clock=None):
        self.source = source
        self.lifecycle = lifecycle
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ingestion = DocumentLifecycleLegacyIngestionGuard(lifecycle=lifecycle, ingestion=ingestion)

    async def read(self, *, scope, limit=100, scan_budget=1000, cursor=None):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit scope is required')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        if type(scan_budget) is not int or not limit <= scan_budget <= 1000:
            raise ValueError('scan budget must be between the result limit and 1000')
        cursor = cursor or DocumentObservationCursorV2(scope)
        if not isinstance(cursor, DocumentObservationCursorV2) or cursor.scope != scope:
            raise PermissionError('observation cursor belongs to a different scope')
        if cursor.phase not in {'legacy', 'sources'}:
            raise ValueError('unsupported observation cursor phase')
        read_at = self.clock()
        if not isinstance(read_at, datetime) or read_at.utcoffset() is None:
            raise ValueError('an aware read time is required')
        phase, legacy_after, source_after = cursor.phase, cursor.legacy_after_id, cursor.source_after_id
        legacy, managed, seen = [], [], {}
        scanned = hidden = 0
        continuation = None
        while scanned < scan_budget and len(legacy) + len(managed) < limit:
            remaining = min(scan_budget - scanned, limit - len(legacy) - len(managed))
            if phase == 'legacy':
                rows = await self.source.read_after(scope=scope, after_id=legacy_after, limit=remaining + 1)
                after = legacy_after
            else:
                rows = await self.lifecycle.read_sources(scope=scope, after_id=source_after, limit=remaining + 1)
                after = source_after
            if not isinstance(rows, list) or len(rows) > remaining + 1:
                raise DocumentObservationReadError('source exceeded the bounded scan')
            for row in rows:
                identity = row.get('_id')
                if any(row.get(key) != value for key, value in scope.as_dict().items()):
                    raise PermissionError('observation scan escaped its scope')
                if not isinstance(identity, str) or not identity or after is not None and identity <= after:
                    raise DocumentObservationReadError('scan order or identity is invalid')
                after = identity
            for row in rows[:remaining]:
                scanned += 1
                source_id = row.get('canonicalReportId') if phase == 'legacy' else row.get('sourceId')
                state = await self.lifecycle.read(scope=scope, source_id=source_id)
                if source_id in seen and seen[source_id] != state:
                    raise DocumentObservationReadError('source changed between observation occurrences')
                seen[source_id] = state
                if phase == 'legacy':
                    legacy_after = row['_id']
                    if state.legacy_observations_visible:
                        reader = DocumentObservationReader(
                            source=_Rows([row]), ingestion=self.ingestion, clock=self.clock
                        )
                        legacy.extend((await reader.read(scope=scope, limit=1)).items)
                    else:
                        hidden += 1
                else:
                    source_after = row['_id']
                    if state.mode == 'events' and state.status == 'active':
                        managed.append(asdict(state))
                    else:
                        hidden += 1
            more = len(rows) > remaining
            if more:
                continuation = DocumentObservationCursorV2(scope, phase, legacy_after, source_after)
                continue
            if phase == 'legacy':
                phase = 'sources'
                continuation = DocumentObservationCursorV2(scope, phase, legacy_after, source_after)
            else:
                continuation = None
                break
        for source_id, selected in seen.items():
            if await self.lifecycle.read(scope=scope, source_id=source_id) != selected:
                raise DocumentObservationReadError('source changed while constructing the observation page')
        return DocumentObservationPageV2(
            scope,
            read_at,
            canonical_json(legacy),
            canonical_json(managed),
            scanned,
            hidden,
            scanned >= scan_budget and continuation is not None,
            continuation,
        )


class DocumentCurrentObservationReader:
    """Undated observations supplement the Profile facts from the same fixed source."""

    def __init__(self, *, lifecycle, current_source, resolve_lifecycle, clock=None):
        self.lifecycle, self.current_source = lifecycle, current_source
        self.resolve_lifecycle = resolve_lifecycle
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def read(self, *, scope, limit=100):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit DocumentScope is required')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        epoch = await self.resolve_lifecycle(scope)
        if epoch is None:
            return DocumentObservationPageV1(scope, self.clock(), '[]', False)
        items, selected = {}, {}
        after, scanned = None, 0
        more = True
        while scanned < 1000 and len(items) <= limit and more:
            capacity = min(40, 1000 - scanned)
            rows = await self.lifecycle.read_sources(scope=scope, after_id=after, limit=capacity + 1)
            if len(rows) > capacity + 1:
                raise DocumentObservationReadError('source exceeded the bounded scan')
            for row in rows[:capacity]:
                if any(row.get(key) != value for key, value in scope.as_dict().items()):
                    raise PermissionError('observation scan escaped its scope')
                identity = row.get('_id')
                if not isinstance(identity, str) or not identity or after is not None and identity <= after:
                    raise DocumentObservationReadError('source scan order is invalid')
                after = identity
                scanned += 1
                binding = row.get('currentSourceBinding') or {}
                if binding.get('lifecycleEpoch') != epoch:
                    continue
                source_id = row['sourceId']
                state = await self.lifecycle.read(scope=scope, source_id=source_id)
                if state.status != 'active' or state.active_contract_version != COMPLETE_SOURCE_CONTRACT_V2:
                    continue
                self._collect(scope, epoch, state, await self._read_source(scope, state), items)
                selected[source_id] = state
                if len(items) > limit:
                    break
            more = len(rows) > capacity
        for source_id, state in selected.items():
            if await self.lifecycle.read(scope=scope, source_id=source_id) != state:
                raise DocumentObservationReadError('source changed while constructing the observation page')
        if await self.resolve_lifecycle(scope) != epoch:
            raise DocumentObservationReadError('subject lifecycle changed while reading observations')
        truncated = len(items) > limit
        return DocumentObservationPageV1(scope, self.clock(), canonical_json(list(items.values())[:limit]), truncated)

    async def _read_source(self, scope, state):
        try:
            return await self.current_source.read_source_observations(
                scope=scope, snapshot_id=state.active_snapshot_ref
            )
        except (DocumentCompleteSourceUnavailable, LookupError) as error:
            raise DocumentObservationReadError('fixed current source is unavailable') from error

    @staticmethod
    def _collect(scope, epoch, state, source, items):
        payload = source['payload']
        if (
            source['scope'] != scope.as_dict()
            or source['sourceId'] != state.source_id
            or source['sourceRevision'] != state.active_material_revision
            or payload['coverage']['lifecycleEpoch'] != epoch
            or digest(source) != state.active_content_digest
        ):
            raise DocumentObservationReadError('fixed current source differs from its applied publication')
        for contribution in payload['clinicalContributions']:
            namespace = contribution['contributionId'].replace('~', '~0').replace('/', '~1')
            for row in contribution['observations']:
                label, result = row.get('label'), row.get('rawValue')
                if not label or result is None or validated_document_time(row.get('observedAt')):
                    continue
                if not any(key in row for key in ('sourceSection', 'sourceCategory', 'sourceObservationId')) and (
                    validated_document_time(contribution.get('reportTime')) is not None
                ):
                    continue
                pointer = '/contributions/' + namespace + row['observationRef']
                documents = tuple(
                    dict.fromkeys(
                        item['sourceDocumentId']
                        for item in row.get('evidence', ())
                        if isinstance(item, dict) and 'sourceDocumentId' in item
                    )
                )
                if not documents and len(payload['materialSnapshotRefs']) == 1:
                    documents = (payload['materialSnapshotRefs'][0]['documentId'],)
                if not documents:
                    continue
                observation_id = 'observation_current_' + digest([state.source_id, pointer])
                items.setdefault(
                    observation_id,
                    {
                        'observationId': observation_id,
                        'documentId': documents[0] if len(documents) == 1 else None,
                        'documentIds': list(documents),
                        'canonicalReportId': state.source_id,
                        'sourceRevision': source['sourceRevision'],
                        'sourcePointer': pointer,
                        'observation': {
                            'itemName': label,
                            'resultValue': result,
                            **{
                                field: row[field]
                                for field in ('unit', 'referenceRange', 'flag', 'method', 'specimen')
                                if row.get(field) is not None
                            },
                        },
                        'observedAt': None,
                        'dateStatus': 'unknown',
                        'ingestionStatus': 'completed',
                        'factEligibility': 'date_unknown',
                    },
                )
