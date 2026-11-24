"""Bounded, pure Document archive reads; consumers own presentation and fact policy."""

import base64
import json
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace

from modules.analysis.preparation import payload_digest
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from modules.analysis.understanding import UNDERSTANDING_SELECTION


class DocumentArchiveReadError(RuntimeError):
    """An owned archive cannot currently be read, distinct from an empty scope."""


class DocumentArchiveChangedError(DocumentArchiveReadError):
    """The fixed selection or source lifecycle changed; restart the query."""


@dataclass(frozen=True, slots=True)
class ArchivedExtractionDescriptor:
    extraction_id: str | None
    document_id: str
    canonical_report_id: str
    document_ids: tuple[str, ...]
    title: str | None
    report_time: str | None
    interpretation_round: int | None
    material_ref: str | None
    result_ref: str | None
    processing: dict
    source_status: str
    publication_revision: int | None
    source_revision: int | None
    lifecycle_token: str
    archive_status: str
    reason: str | None = None
    result_digest: str | None = None
    lifecycle_epoch: int | None = None
    current_source: bool = False
    archive_lifecycle_epoch: int | None = None
    archive_managed_bindings: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ArchivedExtraction:
    descriptor: ArchivedExtractionDescriptor
    result: dict | None
    source_observations: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchivedExtractionPage:
    entries: tuple[ArchivedExtractionDescriptor, ...]
    next_after_id: str | None


class DocumentArchivedExtractionReader:
    def __init__(self, *, index, traces, understanding, lifecycle, managed, current_source=None):
        self.index, self.traces, self.understanding = index, traces, understanding
        self.lifecycle, self.managed = lifecycle, managed
        self.current_source = current_source

    async def read_source_lifecycle(self, *, scope, canonical_report_id):
        return await self.lifecycle.read(scope=scope, source_id=canonical_report_id)

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')

    @staticmethod
    def _identity(scope, document_id, binding):
        value = [scope.as_dict(), document_id, binding]
        return base64.urlsafe_b64encode(canonical_json(value).encode()).decode().rstrip('=')

    async def list_archived(self, *, scope, document_id=None, after_id=None, limit=20):
        self._scope(scope)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('archive limit must be between 1 and 100')
        for value in (document_id, after_id):
            if value is not None and (not isinstance(value, str) or not value or value.strip() != value):
                raise ValueError('archive identities must be nonblank strings')
        if document_id is not None:
            return await self._list_filtered(scope, document_id, after_id, limit)
        rows = await self.index.read_archived_documents(
            scope=scope, document_id=document_id, after_id=after_id, limit=limit + 1
        )
        entries = []
        for row in rows[:limit]:
            entries.append((await self._read(scope, row)).descriptor)
        return ArchivedExtractionPage(tuple(entries), rows[limit - 1]['_id'] if len(rows) > limit else None)

    @staticmethod
    def _filtered_cursor(scope, document_id, after_source_id):
        content = canonical_json([scope.as_dict(), document_id, after_source_id]).encode()
        return 'related_' + base64.urlsafe_b64encode(content).decode().rstrip('=')

    async def _list_filtered(self, scope, document_id, after_id, limit):
        entries, after_source = [], None
        if after_id is not None:
            try:
                if not after_id.startswith('related_'):
                    raise ValueError('unknown filtered cursor')
                encoded = after_id[len('related_') :]
                owner, selected_document, after_source = json.loads(
                    base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                )
                if owner != scope.as_dict() or selected_document != document_id:
                    raise ValueError('archive cursor belongs to a different filter')
                if after_source is not None and (not isinstance(after_source, str) or not after_source):
                    raise ValueError('invalid related source position')
            except (ValueError, TypeError, UnicodeError) as error:
                raise ValueError('invalid filtered archive cursor') from error
        owned = await self.index.get(document_id, scope.user_id, scope.subject_id)
        if after_id is None and owned is not None:
            entries.append((await self._read(scope, owned)).descriptor)
            if len(entries) == limit:
                return ArchivedExtractionPage(tuple(entries), self._filtered_cursor(scope, document_id, None))
        budget = limit - len(entries)
        if self.current_source is not None and self.managed is None:
            return await self._list_current_related(scope, document_id, after_source, entries, budget, owned)
        sources = (
            await self.managed.commands.records.read_document_sources(
                scope=scope,
                document_id=document_id,
                after_source_id=after_source,
                limit=budget + 1,
            )
            if self.managed is not None
            else []
        )
        for source_id in sources[:budget]:
            after_source = source_id
            if owned is not None and source_id == owned['canonicalReportId']:
                continue
            related = await self._related(scope, source_id, document_id)
            if related is not None:
                entries.append(related.descriptor)
        continuation = self._filtered_cursor(scope, document_id, after_source) if len(sources) > budget else None
        return ArchivedExtractionPage(tuple(entries), continuation)

    async def _list_current_related(self, scope, document_id, after, entries, budget, owned):
        scan_limit = 100
        rows = await self.lifecycle.read_sources(scope=scope, after_id=after, limit=scan_limit + 1)
        for index, row in enumerate(rows[:scan_limit]):
            key = row.get('_id')
            if row.get('sourceId') is None or not isinstance(key, str) or (after is not None and key <= after):
                raise DocumentArchiveReadError('related source scan returned an invalid identity')
            if any(row.get(field) != value for field, value in scope.as_dict().items()):
                raise PermissionError('related source scan escaped its scope')
            after = key
            if owned is not None and row['sourceId'] == owned['canonicalReportId']:
                continue
            related = await self._current_related(scope, row, document_id)
            if related is not None:
                entries.append(related.descriptor)
                budget -= 1
                if budget == 0:
                    more = index < len(rows) - 1
                    break
        else:
            more = len(rows) > scan_limit
        continuation = self._filtered_cursor(scope, document_id, after) if more else None
        return ArchivedExtractionPage(tuple(entries), continuation)

    async def _current_related(self, scope, row, document_id):
        if row is None or row.get('currentSourceBinding') is None or not row.get('publicationRevision'):
            return None
        source_id = row['sourceId']
        before = await self.read_source_lifecycle(scope=scope, canonical_report_id=source_id)
        if before.status not in {'active', 'pending'}:
            return None
        try:
            bundle, observations, documents, _ = await self._active_current(scope, before, document_id, row)
        except LookupError as error:
            raise DocumentArchiveChangedError('the related current capture is no longer available') from error
        observations = tuple(
            {**item, 'sourceStatus': before.status} for item in observations if item['documentId'] == document_id
        )
        after = await self.read_source_lifecycle(scope=scope, canonical_report_id=source_id)
        if after != before:
            raise DocumentArchiveChangedError('the related current source changed during reading')
        if bundle is None or not observations:
            return None
        result = bundle.understanding.payload['interpretation']
        epoch = row['currentSourceBinding']['lifecycleEpoch']
        descriptor = ArchivedExtractionDescriptor(
            self._identity(
                scope,
                document_id,
                {
                    'sourceId': source_id,
                    'currentRelated': True,
                    'understandingRef': bundle.understanding.snapshot_id,
                    'publicationRevision': before.publication_revision,
                },
            ),
            document_id,
            source_id,
            documents,
            result.get('title'),
            result.get('reportTime'),
            bundle.understanding.payload['interpretationRound'],
            bundle.material.snapshot_id,
            bundle.understanding.snapshot_id,
            deepcopy(result.get('processing') or {}),
            before.status,
            before.publication_revision,
            before.active_material_revision or row['currentSourceBinding']['sourceRevision'],
            digest(asdict(before)),
            'ready',
            result_digest=payload_digest(result),
            lifecycle_epoch=epoch,
            current_source=True,
        )
        return ArchivedExtraction(descriptor, None, observations)

    async def _related(self, scope, source_id, document_id):
        state = await self.read_source_lifecycle(scope=scope, canonical_report_id=source_id)
        if state.mode != 'events' or not state.publication_revision or state.status in {'withdrawn', 'retracting'}:
            return None
        # The manifest is only a discovery hint. Current material is authoritative.
        source = await self._source_snapshot(scope, state)
        refs = source.payload['materialSnapshotRefs']
        if not any(ref['documentId'] == document_id for ref in refs):
            return None
        inventory = await self.managed.source_reader.snapshots.get(
            scope, source.payload['coverage']['inventorySnapshotRef']['snapshotId']
        )
        plan = await self.managed.commands.read_verified_plan(
            scope=scope, plan_ref=inventory.payload['activeSetEvidenceRef']
        )
        primary_id = None
        for handle in plan.active:
            contribution, proof = await self.managed.source_reader._handle(scope, handle)
            if contribution.payload['kind'] == 'base':
                primary_id = proof.payload['materialSnapshotRefs'][0]['documentId']
        primary = await self.index.get(primary_id, scope.user_id, scope.subject_id) if primary_id else None
        if primary is None:
            raise DocumentArchiveReadError('the related source primary document is unavailable')
        value = await self._read(scope, primary, include_unpublished=False)
        observations = tuple(row for row in value.source_observations if document_id in row.get('documentIds', ()))
        if not observations:
            return None
        return replace(value, result=None, source_observations=observations)

    async def read_fixed(self, *, scope, extraction_id, expected_lifecycle_token=None, document_id=None):
        self._scope(scope)
        try:
            value = json.loads(base64.urlsafe_b64decode(extraction_id + '=' * (-len(extraction_id) % 4)))
            owner, selected_document_id, binding = value
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError('invalid fixed extraction identity') from error
        if owner != scope.as_dict():
            raise PermissionError('archive selection belongs to a different scope')
        if isinstance(binding, dict) and binding.get('currentRelated') is True:
            source_id = binding.get('sourceId')
            if not isinstance(source_id, str) or not source_id:
                raise ValueError('invalid related extraction identity')
            row = await self.lifecycle.ledger.read_source(scope, source_id)
            result = await self._current_related(scope, row, selected_document_id)
            if (
                result is None
                or result.descriptor.extraction_id != extraction_id
                or (document_id is not None and selected_document_id != document_id)
                or (
                    expected_lifecycle_token is not None
                    and result.descriptor.lifecycle_token != expected_lifecycle_token
                )
            ):
                raise DocumentArchiveChangedError('the fixed related archive or source lifecycle changed')
            return result
        row = await self.index.get(selected_document_id, scope.user_id, scope.subject_id)
        if row is None:
            raise DocumentArchiveChangedError('the owned archive selection no longer exists')
        if document_id is not None and document_id != selected_document_id:
            result = await self._related(scope, row['canonicalReportId'], document_id)
            if result is None:
                raise DocumentArchiveChangedError('the related contribution is no longer effective')
        else:
            result = await self._read(scope, row)
        if result.descriptor.extraction_id != extraction_id or (
            expected_lifecycle_token is not None and result.descriptor.lifecycle_token != expected_lifecycle_token
        ):
            raise DocumentArchiveChangedError('the fixed archive or source lifecycle changed')
        return result

    async def _source_snapshot(self, scope, state):
        if state.status == 'active':
            source = await self.lifecycle.read_active_material(scope=scope, source_id=state.source_id, expected=state)
        else:
            # A persisted candidate is readable before Profile applies it. Select
            # that exact candidate, never the preceding (possibly retired) base.
            row = await self.lifecycle.ledger.read_source(scope, state.source_id)
            event = (row.get('currentIntent') or {}).get('event') or {}
            if event.get('eventKind') == 'retract' or not event.get('snapshotRef'):
                raise DocumentArchiveReadError('the pending source has no fixed readable candidate')
            source = await self.managed.source_reader.snapshots.get(scope, event['snapshotRef']['snapshotId'])
            if source.source_id != state.source_id or source.content_digest != event['contentDigest']:
                raise DocumentArchiveReadError('the pending source candidate binding differs')
        return source

    async def _active_current(self, scope, state, document_id, row):
        binding = row.get('currentSourceBinding') or {}
        if (
            binding.get('scope') != scope.as_dict()
            or binding.get('sourceId') != state.source_id
            or type(binding.get('lifecycleEpoch')) is not int
            or binding['lifecycleEpoch'] < 1
        ):
            raise DocumentArchiveReadError('the fixed current source binding differs')
        intent = row.get('currentIntent') or {}
        event = intent.get('event') or {}
        if event.get('publicationRevision') != state.publication_revision:
            raise DocumentArchiveChangedError('the fixed current archive publication changed')
        withdrawing = state.status in {'withdrawn', 'retracting'}
        if withdrawing:
            if event.get('eventKind') != 'retract' or event.get('priorRevision') != state.publication_revision - 1:
                raise DocumentArchiveChangedError('the fixed withdrawal predecessor changed')
            predecessor = await self.lifecycle.ledger.read_previous_publication(
                scope, state.source_id, event['priorRevision'], event_id=intent.get('withdrawalPredecessorId')
            )
            prior = (predecessor or {}).get('event') or {}
            if (
                prior.get('publicationRevision') != event['priorRevision']
                or prior.get('sourceRevision') != event.get('sourceRevision')
                or prior.get('eventId') != (predecessor or {}).get('_id')
            ):
                raise DocumentArchiveReadError('the fixed withdrawal predecessor is unavailable')
            event = prior
        ref = event.get('snapshotRef') or {}
        if (
            self.current_source is None
            or ref.get('contractVersion') != COMPLETE_SOURCE_CONTRACT_V2
            or event.get('eventKind') not in {'publish', 'correct'}
            or event.get('scope') != scope.as_dict()
            or event.get('sourceId') != state.source_id
        ):
            raise DocumentArchiveReadError('the fixed current archive is unavailable')
        source, bundle, observations, managed_bindings = await self.current_source.read_archive_material(
            scope=scope, snapshot_id=ref['snapshotId'], document_id=document_id
        )
        if (
            source['scope'] != scope.as_dict()
            or source['sourceId'] != state.source_id
            or source['sourceRevision'] != event['sourceRevision']
            or source['payload']['coverage']['lifecycleEpoch'] != binding['lifecycleEpoch']
            or digest(source) != event['contentDigest']
            or (state.status == 'active' and ref['snapshotId'] != state.active_snapshot_ref)
        ):
            raise DocumentArchiveReadError('the fixed current archive material differs')
        observations = (
            ()
            if withdrawing
            else tuple({**item, 'publicationRevision': state.publication_revision} for item in observations)
        )
        return (
            None if withdrawing else bundle,
            observations,
            tuple(dict.fromkeys(ref['documentId'] for ref in source['payload']['materialSnapshotRefs'])),
            managed_bindings,
        )

    async def _active(self, scope, state, document_id):
        source = await self._source_snapshot(scope, state)
        inventory = await self.managed.source_reader.snapshots.get(
            scope, source.payload['coverage']['inventorySnapshotRef']['snapshotId']
        )
        plan = await self.managed.commands.read_verified_plan(
            scope=scope, plan_ref=inventory.payload['activeSetEvidenceRef']
        )
        reference, contribution_documents, contribution_proofs = None, {}, {}
        for handle in plan.active:
            contribution, proof = await self.managed.source_reader._handle(scope, handle)
            contribution_documents[contribution.payload['contributionId']] = [
                ref['documentId'] for ref in proof.payload['materialSnapshotRefs']
            ]
            contribution_proofs[contribution.payload['contributionId']] = {
                'contributionSnapshotRef': contribution.snapshot_id,
                'contributionContentDigest': contribution.content_digest,
                'captureProofRef': proof.snapshot_id,
                'materialSnapshotRefs': deepcopy(proof.payload['materialSnapshotRefs']),
                'sourceSnapshotRef': source.snapshot_id,
                'sourceContentDigest': source.content_digest,
            }
            if contribution.payload['kind'] == 'base':
                if any(ref['documentId'] == document_id for ref in proof.payload['materialSnapshotRefs']):
                    reference = proof.payload['understandingSnapshotRef']
        if reference is None:
            raise DocumentArchiveReadError('the effective source has no fixed interpretation for this document')
        bundle = await self.understanding.read_bundle(
            scope=scope, understanding_snapshot_id=reference, selection=UNDERSTANDING_SELECTION
        )
        observations = []
        for contribution in source.payload['contributions']:
            namespace = contribution['contributionId'].replace('~', '~0').replace('/', '~1')
            document_ids = contribution_documents[contribution['contributionId']]
            for item in contribution['observations']:
                observations.append(
                    {
                        **deepcopy(item),
                        **contribution_proofs[contribution['contributionId']],
                        'sourcePointer': '/contributions/' + namespace + item['observationRef'],
                        'contributionId': contribution['contributionId'],
                        'contributionKind': contribution['kind'],
                        'sourceRevision': (
                            contribution['legacySourceRevision']
                            if contribution['legacySourceRevision'] is not None
                            else contribution['sourceRevision']
                        ),
                        'publicationRevision': state.publication_revision,
                        'materialSourceRevision': source.source_revision,
                        'documentId': document_ids[0] if len(document_ids) == 1 else None,
                        'documentIds': document_ids,
                    }
                )
        return bundle, tuple(observations), tuple(ref['documentId'] for ref in source.payload['materialSnapshotRefs'])

    async def _stored(self, scope, document):
        rounds = document.get('archivedRounds', {})
        fixed = deepcopy(rounds[max(rounds, key=int)]) if rounds else deepcopy(document)
        fixed.pop('archivedRounds', None)
        refs = fixed.get('payloadRefs', {})
        if not refs.get('interpretation') or not refs.get('docmind_result'):
            return fixed, None
        parsed = await self.traces.read_payload(
            refs['docmind_result'], scope.user_id, scope.subject_id, 'docmind_result'
        )
        result = await self.traces.read_payload(
            refs['interpretation'], scope.user_id, scope.subject_id, 'interpretation'
        )
        if not isinstance(parsed, dict) or not isinstance(result, dict):
            raise DocumentArchiveReadError('archive payload shape is invalid')
        if rounds and (
            payload_digest(parsed) != fixed['parseDigest'] or payload_digest(result) != fixed['interpretationDigest']
        ):
            raise DocumentArchiveReadError('fixed archive content digest differs')
        return fixed, result

    async def _read(self, scope, document, *, include_unpublished=True):
        if any(document.get(key) != value for key, value in scope.as_dict().items()):
            raise PermissionError('document archive escaped its scope')
        source_id, document_id = document['canonicalReportId'], document['_id']
        before = await self.read_source_lifecycle(scope=scope, canonical_report_id=source_id)
        token = digest(asdict(before))
        status, archive_status, reason = 'pending', 'ready', None
        result, observations, fixed = None, (), {}
        document_ids = (document_id,)
        material_ref = result_ref = None
        revision = document.get('sourceRevision')
        publication_revision = before.publication_revision or None
        binding = self._manifest_binding(document)
        round_number = binding[0] if binding is not None else None
        if binding is not None:
            material_ref, result_ref = binding[1:3]
        current_binding, current, archive_epoch = None, None, None
        managed_bindings = ()
        if self.current_source is not None and before.mode == 'events' and before.publication_revision:
            current = await self.lifecycle.ledger.read_source(scope, source_id)
            current_binding = (current or {}).get('currentSourceBinding')
        try:
            if before.status in {'withdrawn', 'retracting'}:
                status, archive_status = 'withdrawn', 'withdrawn'
                if current_binding is not None:
                    _, _, document_ids, managed_bindings = await self._active_current(
                        scope, before, document_id, current
                    )
                if binding is None:
                    archive_status, reason = 'unavailable', 'fixed_archive_missing'
            elif before.mode == 'events' and before.publication_revision:
                if current_binding is not None:
                    bundle, observations, document_ids, managed_bindings = await self._active_current(
                        scope, before, document_id, current
                    )
                elif self.managed is not None:
                    bundle, observations, document_ids = await self._active(scope, before, document_id)
                else:
                    raise DocumentArchiveReadError('the publication does not have a current fixed archive')
                if bundle is None:
                    raise DocumentArchiveReadError('the active inventory has no interpretation for this document')
                fixed = bundle.material.payload['documentMetadata']
                result = bundle.understanding.payload['interpretation']
                stored = (document.get('archivedRounds') or {}).get(str(bundle.facts.payload['interpretationRound']))
                archive_epoch = self._archive_epoch(stored or {})
                if archive_epoch is None and current_binding is not None:
                    archive_epoch = current_binding['lifecycleEpoch']
                material_ref, result_ref = bundle.material.snapshot_id, bundle.understanding.snapshot_id
                revision = bundle.facts.payload.get('legacySourceRevision') or bundle.material.source_revision
                round_number = bundle.understanding.payload['interpretationRound']
                status = 'active' if before.status == 'active' else 'pending'
                binding = [
                    round_number,
                    bundle.material.payload['parseArtifactRef'],
                    bundle.understanding.payload['interpretationArtifactRef'],
                    payload_digest(result),
                ]
                active_extraction_id = self._identity(scope, document_id, binding)
                observations = tuple(
                    {
                        **row,
                        'extractionId': active_extraction_id,
                        'sourceStatus': 'active' if before.status == 'active' else 'pending',
                    }
                    for row in observations
                )
                rounds = document.get('archivedRounds') or {}
                if include_unpublished and rounds and int(max(rounds, key=int)) > round_number:
                    # A newly archived execution is visible before its delivery
                    # worker publishes. Effective older observations stay separate.
                    binding = self._manifest_binding(document)
                    round_number = int(max(rounds, key=int))
                    material_ref, result_ref = binding[1:3] if binding is not None else (None, None)
                    revision, publication_revision, status = None, None, 'pending'
                    try:
                        stored, result = await self._stored(scope, document)
                        archive_epoch = self._archive_epoch(stored) if binding is not None else None
                        if result is None or binding is None:
                            raise DocumentArchiveReadError('the pending fixed archive is unavailable')
                    except (LookupError, RuntimeError, ValueError, KeyError) as error:
                        if isinstance(error, DocumentArchiveChangedError):
                            raise
                        # Keep proven active observations for standard reads while
                        # the pending archive keeps its own identity and failure.
                        result = None
                        status, archive_status, reason = 'unavailable', 'unavailable', 'archive_unavailable'
            else:
                fixed, result = await self._stored(scope, document)
                archive_epoch = self._archive_epoch(fixed) if binding is not None else None
                refs = fixed.get('payloadRefs', {})
                material_ref, result_ref = refs.get('docmind_result'), refs.get('interpretation')
                round_number = fixed.get('interpretationResultRound', fixed.get('interpretationRound'))
                revision = fixed.get('sourceRevision')
                if result is not None and material_ref and result_ref:
                    binding = [round_number, material_ref, result_ref, payload_digest(result)]
                if result is None:
                    waiting = document.get('status') in {'pending', 'running', 'submission_unknown'}
                    archive_status, reason = (
                        ('pending', 'processing_pending') if waiting else ('unavailable', 'archive_missing')
                    )
                elif (result.get('processing') or {}).get('status') != 'complete':
                    status = 'not_published'
        except (LookupError, RuntimeError, ValueError, KeyError) as error:
            if isinstance(error, DocumentArchiveChangedError):
                raise
            result, observations = None, ()
            status, archive_status, reason = 'unavailable', 'unavailable', 'archive_unavailable'
        if await self.read_source_lifecycle(scope=scope, canonical_report_id=source_id) != before:
            raise DocumentArchiveChangedError('source lifecycle changed during archive reading')
        # Pin pending/unpublished selection as well as the formal lifecycle.
        current = await self.index.get(document_id, scope.user_id, scope.subject_id)
        if current is None or self._fixed_key(current) != self._fixed_key(document):
            raise DocumentArchiveChangedError('archive selection changed during reading')
        visible = result or {}
        if binding is None:
            archive_status = 'unavailable'
            reason = reason or 'fixed_archive_missing'
        descriptor = ArchivedExtractionDescriptor(
            self._identity(scope, document_id, binding) if binding is not None else None,
            document_id,
            source_id,
            document_ids,
            visible.get('title'),
            visible.get('reportTime'),
            round_number,
            material_ref,
            result_ref,
            deepcopy(visible.get('processing') or {}),
            status,
            publication_revision,
            revision,
            token,
            archive_status,
            reason,
            payload_digest(result) if result is not None else None,
            current_binding.get('lifecycleEpoch') if current_binding else archive_epoch,
            current_binding is not None or archive_epoch is not None,
            archive_epoch,
            managed_bindings,
        )
        return ArchivedExtraction(descriptor, deepcopy(result), observations)

    @staticmethod
    def _archive_epoch(archive):
        epoch = archive.get('subjectLifecycleEpoch')
        return epoch if type(epoch) is int and epoch > 0 else None

    @staticmethod
    def _manifest_binding(document):
        rounds = document.get('archivedRounds') or {}
        if not rounds:
            return None
        fixed = rounds[max(rounds, key=int)]
        refs = fixed.get('payloadRefs') or {}
        content_digest = fixed.get('interpretationDigest')
        number = fixed.get('interpretationResultRound')
        if (
            type(number) is not int
            or number < 0
            or not refs.get('docmind_result')
            or not refs.get('interpretation')
            or not isinstance(content_digest, str)
            or len(content_digest) != 64
        ):
            return None
        return [number, refs['docmind_result'], refs['interpretation'], content_digest]

    @staticmethod
    def _fixed_key(document):
        rounds = document.get('archivedRounds') or {}
        if rounds:
            return rounds[max(rounds, key=int)]
        return {key: document.get(key) for key in ('payloadRefs', 'interpretationRound', 'interpretationResultRound')}
