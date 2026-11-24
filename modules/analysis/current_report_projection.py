"""Durable current Report source execution, independent of publication."""

import asyncio
from dataclasses import asdict
from weakref import WeakValueDictionary

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_understanding import DocumentCurrentBundle
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.snapshots import CURRENT_REPORT_PROJECTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

COMMAND_REVISION = 'document.current-report-projection-command.v1'
_COMMAND = 'current_report_projection_command'
_RECEIPT = 'current_report_projection_receipt'


def _require(condition, reason):
    if not condition:
        raise DocumentCompleteSourceUnavailable(reason)


class DocumentCurrentReportProjection:
    """Execute a scoped command and retain its first immutable Report output."""

    def __init__(
        self, *, prepared, materials, snapshots, records, projector, selection, interpretation_projection_revision
    ):
        if not isinstance(selection, OutputSelection) or not callable(projector):
            raise TypeError('an exact Report projector and output selection are required')
        exact_text(interpretation_projection_revision, 'Report interpretation projection revision')
        self.prepared, self.materials, self.snapshots, self.records = prepared, materials, snapshots, records
        self.projector = projector
        self.selection = selection
        self.interpretation_projection_revision = interpretation_projection_revision
        self._locks = WeakValueDictionary()

    @staticmethod
    def _manifest(scope, inputs, request_id, lifecycle_epoch):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        exact_text(request_id, 'Report projection request')
        if type(lifecycle_epoch) is not int or lifecycle_epoch < 1:
            raise ValueError('a fixed positive lifecycle is required')
        if not isinstance(inputs, tuple) or not 1 <= len(inputs) <= 100:
            raise ValueError('a bounded ordered tuple of fixed Report inputs is required')
        if any(not isinstance(item, DocumentPreparedInput) for item in inputs):
            raise TypeError('the original durable preparation inputs are required')
        return {
            'commandRevision': COMMAND_REVISION,
            'scope': scope.as_dict(),
            'requestId': request_id,
            'lifecycleEpoch': lifecycle_epoch,
            'inputs': [item.as_dict() for item in inputs],
        }

    async def _bundles(self, scope, inputs):
        unique = {}
        for item in inputs:
            bundle = await self.prepared.validate_prepared(scope=scope, prepared=item)
            _require(isinstance(bundle, DocumentCurrentBundle), 'the exact current Report input bundle is required')
            material = bundle.material
            _require(material.scope == scope, 'the fixed Report input scope differs')
            doc_id = material.payload['documentId']
            same = unique.setdefault(doc_id, bundle)
            _require(same == bundle, 'one document has conflicting fixed Report inputs')
        return tuple(unique.values())

    @staticmethod
    def _dependencies(bundles):
        dependencies = []
        for bundle in bundles:
            material, facts, understanding = bundle.material, bundle.facts.payload, bundle.understanding.payload
            dependencies.append(
                {
                    'documentId': material.payload['documentId'],
                    'materialSnapshotRef': material.snapshot_id,
                    'factsSnapshotRef': bundle.facts.snapshot_id,
                    'understandingSnapshotRef': bundle.understanding.snapshot_id,
                    'contentSha256': material.payload['contentSha256'],
                    'clinicalContentContract': facts['clinicalContentContract'],
                    'clinicalContentDigest': facts['clinicalContentDigest'],
                    'interpretationRound': facts['interpretationRound'],
                    'sourceId': material.source_id,
                    'sourceRevision': material.source_revision,
                    'inputSelection': understanding['selection'],
                    'extractorRevision': facts['extractorRevision'],
                    'validationRevision': facts['validationRevision'],
                    'clinicalAccepted': facts['clinicalAccepted'],
                }
            )
        return dependencies

    @staticmethod
    def _processing_status(projection, bundles):
        _require(isinstance(projection, dict), 'the actual Report output must be an object')
        documents = [
            {
                'documentId': item.material.payload['documentId'],
                'sourceName': item.material.payload['fileName'],
                'sourceChecksum': item.material.payload['contentSha256'],
            }
            for item in bundles
        ]
        _require(projection.get('sourceDocuments') == documents, 'the actual Report output selects different inputs')
        quality = {item.facts.payload['quality']['status'] for item in bundles}
        expected = 'unavailable' if 'unavailable' in quality else 'partial' if 'partial' in quality else 'complete'
        processing = projection.get('processing')
        if expected == 'complete':
            valid = 'processing' not in projection
        else:
            valid = (
                isinstance(processing, dict)
                and set(processing) == {'status', 'retryable', 'issueCount'}
                and processing['status'] == expected
                and type(processing['retryable']) is bool
                and type(processing['issueCount']) is int
                and processing['issueCount'] >= 0
            )
        _require(valid, 'the actual Report quality differs from its fixed inputs')
        return expected

    @staticmethod
    def _trace_id(manifest):
        return 'document_current_report_trace_' + digest(manifest)

    @staticmethod
    def _receipt(snapshot, payload):
        return {
            'snapshotRef': snapshot.snapshot_id,
            'contentDigest': snapshot.content_digest,
            'manifestDigest': digest(payload['manifest']),
            'projectionDigest': payload['projectionDigest'],
            'traceId': payload['traceId'],
        }

    async def prepare(self, *, scope, inputs, request_id, lifecycle_epoch):
        command = self._manifest(scope, inputs, request_id, lifecycle_epoch)
        manifest = {
            **command,
            'selection': asdict(self.selection),
            'interpretationProjectionRevision': self.interpretation_projection_revision,
        }
        lock = self._locks.setdefault((scope, request_id), asyncio.Lock())
        async with lock:
            saved = await self.records.get(_COMMAND, scope, request_id)
            if saved is not None:
                _require(
                    all(saved.get(key) == value for key, value in command.items()),
                    'the fixed Report command inputs or lifecycle differ',
                )
                receipt = await self.records.get(_RECEIPT, scope, request_id)
                if receipt is not None:
                    try:
                        existing = await self.snapshots.get(scope, receipt['snapshotRef'])
                    except DocumentNotFoundError:
                        # The immutable receipt was bound before the final snapshot put.
                        # Retry materialization and never accept different output.
                        pass
                    else:
                        return await self._validate_read(scope, existing, receipt['snapshotRef'])
                _require(saved == manifest, 'the original Report execution revisions are unavailable for retry')
            bundles = await self._bundles(scope, inputs)
            arguments = []
            for bundle in bundles:
                original = await self.materials.read_original(
                    scope=scope, material_snapshot_ref=bundle.material.snapshot_id
                )
                arguments.append((bundle, original))
            await self.records.put(_COMMAND, scope, request_id, manifest)
            selection, projection = await self.projector(tuple(arguments))
            _require(selection == self.selection, 'the actual Report execution selection differs')
            payload = {
                'manifest': manifest,
                'selection': asdict(selection),
                'projection': projection,
                'projectionDigest': digest(projection),
                'processingStatus': self._processing_status(projection, bundles),
                'traceId': self._trace_id(manifest),
                'dependencies': self._dependencies(bundles),
            }
            primary = bundles[0].material
            snapshot = DocumentSnapshot(
                scope,
                CURRENT_REPORT_PROJECTION_CONTRACT,
                primary.source_id,
                primary.source_revision,
                canonical_json(payload),
            )
            _require(
                len(canonical_json(snapshot.canonical).encode('utf-8')) <= 4_000_000,
                'Report projection exceeds its byte bound',
            )
            await self.records.put(_RECEIPT, scope, request_id, self._receipt(snapshot, snapshot.payload))
            await self.snapshots.put(snapshot)
            return await self.read(scope=scope, snapshot_id=snapshot.snapshot_id)

    async def _validate_read(self, scope, snapshot, snapshot_id):
        _require(
            snapshot.scope == scope
            and snapshot.contract_version == CURRENT_REPORT_PROJECTION_CONTRACT
            and snapshot.snapshot_id == snapshot_id,
            'the fixed current Report projection identity differs',
        )
        payload = snapshot.payload
        _require(
            set(payload)
            == {
                'manifest',
                'selection',
                'projection',
                'projectionDigest',
                'processingStatus',
                'traceId',
                'dependencies',
            },
            'the fixed current Report projection contract differs',
        )
        supplied = payload['manifest']
        inputs = tuple(DocumentPreparedInput.from_dict(item) for item in supplied['inputs'])
        request_id = supplied['requestId']
        manifest = {
            **self._manifest(scope, inputs, request_id, supplied['lifecycleEpoch']),
            'selection': asdict(OutputSelection(**supplied['selection'])),
            'interpretationProjectionRevision': supplied['interpretationProjectionRevision'],
        }
        exact_text(manifest['interpretationProjectionRevision'], 'Report interpretation projection revision')
        _require(
            supplied == manifest
            and payload['selection'] == manifest['selection']
            and payload['traceId'] == self._trace_id(manifest)
            and payload['projectionDigest'] == digest(payload['projection']),
            'the fixed Report manifest, revision or output digest differs',
        )
        _require(
            await self.records.require(_COMMAND, scope, request_id) == manifest
            and await self.records.require(_RECEIPT, scope, request_id) == self._receipt(snapshot, payload),
            'the durable Report execution binding differs',
        )
        bundles = await self._bundles(scope, inputs)
        primary = bundles[0].material
        _require(
            snapshot.source_id == primary.source_id
            and snapshot.source_revision == primary.source_revision
            and payload['dependencies'] == self._dependencies(bundles)
            and payload['processingStatus'] == self._processing_status(payload['projection'], bundles),
            'the fixed Report dependencies or quality differ',
        )
        return snapshot

    async def read(self, *, scope, snapshot_id):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        exact_text(snapshot_id, 'Report projection snapshot')
        snapshot = await self.snapshots.get(scope, snapshot_id)
        return await self._validate_read(scope, snapshot, snapshot_id)
