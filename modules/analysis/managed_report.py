"""Fixed v16 preparation has no formal sink; archive is a separate command."""

import asyncio
import inspect
from dataclasses import asdict

from modules.analysis.application.documents import DocumentProcessingTrace
from modules.analysis.application.output_profiles import DocumentSnapshot as ExtractorInput
from modules.analysis.application.output_profiles import input_processing
from modules.analysis.application.output_profiles import normalize_report_source
from modules.analysis.complete_source import CAPTURE_REVISION
from modules.analysis.complete_source import _material_ref
from modules.analysis.complete_source import _selection
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.contracts import DocumentInputSnapshot
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.managed_archive import archive_result
from modules.analysis.managed_content import managed_public_content_v1
from modules.analysis.managed_source_contracts import MANAGED_REPORT_REVISION
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import MANAGED_REPORT_CONTRACT
from modules.analysis.snapshots import SOURCE_CONTRIBUTION_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from utils.report_source_observations import report_source_items


class DocumentManagedReportPreparation:
    def __init__(self, *, compatibility, materials, snapshots, records, profiles, trace_store, archive):
        self.compatibility, self.materials, self.snapshots = compatibility, materials, snapshots
        self.records, self.profiles, self.trace_store, self.archive = records, profiles, trace_store, archive

    async def inputs(self, *, scope, inputs):
        bundles, unique = [], {}
        for item in inputs:
            bundle = await self.compatibility.validate_prepared(
                scope=scope,
                understanding_snapshot_id=item.understanding_snapshot_ref,
                selection=item.selection,
                preparation_receipt=item.preparation_receipt,
            )
            actual = (bundle.material.snapshot_id, bundle.facts.snapshot_id, bundle.understanding.snapshot_id)
            if actual != (item.material_snapshot_ref, item.facts_snapshot_ref, item.understanding_snapshot_ref):
                raise DocumentCompleteSourceUnavailable('the managed input manifest differs from its preparation')
            previous = unique.setdefault(item.preparation_receipt.document_id, actual)
            if previous != actual:
                raise DocumentCompleteSourceUnavailable('repeated documents select conflicting fixed inputs')
            bundles.append(bundle)
        return bundles

    @staticmethod
    def manifest(scope, inputs, revision):
        if revision != MANAGED_REPORT_REVISION:
            raise UnsupportedOutputVersionError('the exact managed Report preparation is unavailable')
        if not isinstance(inputs, tuple) or not 1 <= len(inputs) <= 100:
            raise ValueError('bounded ordered fixed Report inputs are required')
        if any(not isinstance(item, DocumentPreparedPublicationInput) for item in inputs):
            raise TypeError('fixed preparation inputs are required')
        return {
            'scope': scope.as_dict(),
            'inputs': [item.as_dict() for item in inputs],
            'revision': revision,
            'interpretationProjectionRevision': 'document-understanding-public.v1',
        }

    async def prepare(self, *, scope, inputs, revision):
        manifest = self.manifest(scope, inputs, revision)
        key = 'document_managed_report_' + digest(manifest)
        saved = await self.records.get('managed_report_preparation', scope, key)
        if saved is not None:
            return await self.read(scope=scope, snapshot_id=saved['snapshotRef'])
        bundles = await self.inputs(scope=scope, inputs=inputs)
        profile = self.profiles.get(LEGACY_REPORT_SOURCE_SELECTION)
        if not self.profiles.is_legacy(profile.selection):
            raise UnsupportedOutputVersionError('the registered exact v16 implementation is required')
        snapshots, seen = [], set()
        for bundle in bundles:
            content = await self.materials.read_original(scope=scope, material_snapshot_ref=bundle.material.snapshot_id)
            metadata, understanding = bundle.material.payload['documentMetadata'], bundle.understanding.payload
            if metadata['documentId'] in seen:
                continue
            seen.add(metadata['documentId'])
            snapshots.append(
                ExtractorInput(
                    metadata['documentId'],
                    managed_public_content_v1(archive_result(bundle)),
                    bundle.material.payload['parsedDocument']['text'],
                    metadata['fileName'],
                    metadata['fileType'],
                    metadata['contentSha256'],
                    content,
                    DocumentInputSnapshot(
                        parse_artifact_ref=bundle.material.payload['parseArtifactRef'],
                        interpretation_artifact_ref=understanding['interpretationArtifactRef'],
                        interpretation_revision=understanding['interpretationRevision'],
                        validation_revision=understanding['validationRevision'],
                        interpretation_round=understanding['interpretationRound'],
                    ),
                )
            )
        trace = DocumentProcessingTrace(self.trace_store, user_id=scope.user_id)
        await trace.start(subjectId=scope.subject_id, commandRevision=revision, managedPreparationId=key)
        try:
            if inspect.iscoroutinefunction(profile.extractor):
                output = await profile.extractor(tuple(snapshots))
            else:
                output = await asyncio.to_thread(profile.extractor, tuple(snapshots))
                if inspect.isawaitable(output):
                    output = await output
            output = normalize_report_source(output, snapshots)
            quality = input_processing([item.report for item in snapshots])
            if quality is not None:
                output['processing'] = quality
            content = {
                'manifest': manifest,
                'selection': asdict(LEGACY_REPORT_SOURCE_SELECTION),
                'projection': output,
                'processingStatus': (quality or {}).get('status', 'complete'),
                'materialSnapshotRefs': [_material_ref(bundle) for bundle in bundles],
            }
            trace_id = await self.records.bind_trace(scope, key, digest(content), trace.trace_id)
            projection = DocumentSnapshot(
                scope,
                MANAGED_REPORT_CONTRACT,
                bundles[0].material.source_id,
                bundles[0].material.source_revision,
                canonical_json({**content, 'outputTraceId': trace_id}),
            )
            await self.snapshots.put(projection)
            await self.records.put('managed_report_preparation', scope, key, {'snapshotRef': projection.snapshot_id})
            await trace.update(
                status='completed',
                stage='output_ready',
                extractionId=key,
                sourceRevision=None,
                profileIngestion={'status': 'not_submitted', 'jobId': None},
            )
            return projection
        except (Exception, asyncio.CancelledError) as error:
            await trace.update(status='failed', errorType=type(error).__name__)
            raise

    async def read(self, *, scope, snapshot_id):
        snapshot = await self.snapshots.get(scope, snapshot_id)
        if snapshot.contract_version != MANAGED_REPORT_CONTRACT:
            raise DocumentCompleteSourceUnavailable('the fixed managed Report contract is required')
        payload = snapshot.payload
        raw = payload['manifest']
        inputs = tuple(DocumentPreparedPublicationInput.from_dict(item) for item in raw['inputs'])
        manifest = self.manifest(scope, inputs, raw['revision'])
        if raw != manifest or payload['selection'] != asdict(LEGACY_REPORT_SOURCE_SELECTION):
            raise DocumentCompleteSourceUnavailable('the managed Report execution selection differs')
        bundles = await self.inputs(scope=scope, inputs=inputs)
        if (
            snapshot.source_id != bundles[0].material.source_id
            or payload['materialSnapshotRefs'] != [_material_ref(bundle) for bundle in bundles]
            or any(bundle.material.source_revision != snapshot.source_revision for bundle in bundles)
        ):
            raise DocumentCompleteSourceUnavailable('the managed Report material binding differs')
        key = 'document_managed_report_' + digest(manifest)
        binding = await self.records.require('managed_report_preparation', scope, key)
        content = {k: v for k, v in payload.items() if k != 'outputTraceId'}
        trace = await self.records.require('managed_report_trace', scope, key)
        if binding != {'snapshotRef': snapshot_id} or trace != {
            'contentDigest': digest(content),
            'traceId': payload['outputTraceId'],
        }:
            raise DocumentCompleteSourceUnavailable('the fixed managed execution acknowledgement differs')
        return snapshot

    async def _complete_inputs(self, scope, projection):
        inputs = projection.payload['manifest']['inputs']
        bundles = await self.inputs(
            scope=scope, inputs=tuple(DocumentPreparedPublicationInput.from_dict(x) for x in inputs)
        )
        if any(
            b.understanding.payload.get('inputVersionStatus') != 'known'
            or b.understanding.payload.get('quality', {}).get('status') != 'complete'
            or b.understanding.payload.get('interpretationRevision') != 'analysis-health-report-v12'
            for b in bundles
        ):
            raise DocumentCompleteSourceUnavailable('complete fixed v12 inputs are required for neutral publication')
        return bundles

    async def archive_prepared(self, *, scope, projection_ref, primary_bundle, transition_ref):
        projection = await self.read(scope=scope, snapshot_id=projection_ref)
        if projection.payload['processingStatus'] != 'complete':
            raise DocumentCompleteSourceUnavailable('partial or unavailable Report output cannot replace a source')
        if projection.source_id != primary_bundle.material.source_id:
            raise DocumentCompleteSourceUnavailable('the managed Report canonical owner differs')
        bundles = await self._complete_inputs(scope, projection)
        unique = {bundle.material.payload['documentId']: bundle for bundle in bundles}
        first = bundles[0].understanding.payload['interpretation']
        result = {
            'reportType': first.get('reportType', 'unknown'),
            'reportTime': first.get('reportTime') if len(unique) == 1 else None,
            'reportSource': projection.payload['projection'],
            'sourceDocumentIds': list(unique),
        }
        key = 'managed-report-v16:' + digest(
            [projection.payload['selection'], projection.payload['materialSnapshotRefs'], result]
        )
        archive_ref, saved = await self.archive.save(scope=scope, bundle=primary_bundle, result=result, source_key=key)
        binding_ref = 'document_managed_report_archive_' + digest([projection_ref, archive_ref, transition_ref])
        await self.records.put(
            'managed_report_archive',
            scope,
            binding_ref,
            {
                'projectionRef': projection_ref,
                'archiveRef': archive_ref,
                'sourceResult': result,
                'transitionRef': transition_ref,
            },
        )
        return binding_ref, saved

    async def build_contribution(self, *, scope, archive_ref):
        binding = await self.records.require('managed_report_archive', scope, archive_ref)
        if archive_ref != 'document_managed_report_archive_' + digest(
            [binding['projectionRef'], binding['archiveRef'], binding['transitionRef']]
        ):
            raise DocumentCompleteSourceUnavailable('the managed Report archive reference changed')
        projection = await self.read(scope=scope, snapshot_id=binding['projectionRef'])
        await self._complete_inputs(scope, projection)
        archived = await self.archive.read(scope=scope, archive_ref=binding['archiveRef'])
        result = archived['request']['result']
        if (
            projection.payload['processingStatus'] != 'complete'
            or result != binding['sourceResult']
            or result['reportSource'] != projection.payload['projection']
            or archived['receipt']['canonicalReportId'] != projection.source_id
        ):
            raise DocumentCompleteSourceUnavailable('the managed Report archive does not bind the actual output')
        observations = []
        for pointer, item in report_source_items(projection.payload['projection']):
            observations.append(
                {
                    'observationRef': '/reportSource' + pointer,
                    'identityStatus': 'identity_unresolved',
                    'stableObservationId': None,
                    'label': item.get('itemName'),
                    'rawValue': item.get('resultValue'),
                    'unit': item.get('unit'),
                    'referenceRange': item.get('referenceRange'),
                    'flag': item.get('flag'),
                    'observedAt': item.get('observedAt'),
                    'kind': item.get('kind'),
                    'method': item.get('method'),
                    'specimen': item.get('specimen'),
                    'narrativeType': item.get('narrativeType'),
                    'sourceId': None,
                    'sourceCategory': item.get('sourceCategory'),
                    'evidence': [{'artifactRef': projection.snapshot_id, 'pointer': '/payload/projection' + pointer}],
                }
            )
        snapshot = DocumentSnapshot(
            scope,
            SOURCE_CONTRIBUTION_CONTRACT,
            projection.source_id,
            projection.source_revision,
            canonical_json(
                {
                    'contributionId': 'managed_report_'
                    + digest(
                        [projection.source_id, archived['receipt']['legacySourceRevision'], binding['transitionRef']]
                    ),
                    'kind': 'legacy_report_source',
                    'legacySourceRevision': archived['receipt']['legacySourceRevision'],
                    'implementationSelection': _selection(LEGACY_REPORT_SOURCE_SELECTION),
                    'reportType': result['reportType'],
                    'reportTime': result['reportTime'],
                    'observations': observations,
                }
            ),
        )
        if (
            len(observations) > 1000
            or len({row['observationRef'] for row in observations}) != len(observations)
            or len(canonical_json(snapshot.payload).encode()) > 4_000_000
        ):
            raise DocumentCompleteSourceUnavailable('managed neutral contribution exceeds its bounded contract')
        return snapshot, {
            'captureRevision': CAPTURE_REVISION,
            'kind': 'managed_report_source',
            'archiveRef': archive_ref,
            'contributionSnapshotRef': snapshot.snapshot_id,
            'materialSnapshotRefs': list(
                {x['documentId']: x for x in projection.payload['materialSnapshotRefs']}.values()
            ),
            'understandingSnapshotRefs': [
                {'documentId': x['preparationReceipt']['document_id'], 'snapshotRef': x['understandingSnapshotRef']}
                for x in projection.payload['manifest']['inputs']
            ],
            'originalQuality': {'processingStatus': 'complete', 'projectionRef': projection.snapshot_id},
        }
