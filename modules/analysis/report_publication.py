"""Explicit historical Report publication from fixed Document preparations.

Only this command reaches the legacy sink. Pure projection reads never execute
the extractor, enqueue Profile work, or select a mutable Document index record.
"""

from dataclasses import asdict

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.compatibility import DocumentLegacyPublicationReceipt
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.contracts import DocumentInputSnapshot
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.report_publication_contracts import DocumentReportPublicationReceipt
from modules.analysis.snapshots import LEGACY_REPORT_INPUT_CONTRACT
from modules.analysis.snapshots import LEGACY_REPORT_SOURCE_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparationReceipt


def _base_receipt(value):
    value = dict(value)
    value['preparation_receipt'] = DocumentPreparationReceipt(**value['preparation_receipt'])
    return DocumentLegacyPublicationReceipt(**value)


class DocumentReportPublicationService:
    def __init__(self, *, compatibility, materials, snapshots, commands, coordinator, read_legacy_revision):
        self.compatibility = compatibility
        self.materials = materials
        self.snapshots = snapshots
        self.commands = commands
        self.coordinator = coordinator
        self.read_legacy_revision = read_legacy_revision

    def _profile(self, selection):
        if selection != LEGACY_REPORT_SOURCE_SELECTION or self.coordinator is None:
            raise UnsupportedOutputVersionError('the fixed legacy Report publisher is unavailable')
        profile = self.coordinator.resolve_profile(output_selection=selection)
        if not self.coordinator.profiles.is_legacy(profile.selection):
            raise UnsupportedOutputVersionError('the historical Report alias must retain its exact implementation')

    async def _validate_inputs(self, manifest):
        bundles, seen = [], {}
        for item in manifest.inputs:
            bundle = await self.compatibility.validate_prepared(
                scope=manifest.scope,
                understanding_snapshot_id=item.understanding_snapshot_ref,
                selection=item.selection,
                preparation_receipt=item.preparation_receipt,
            )
            actual = (bundle.material.snapshot_id, bundle.facts.snapshot_id, bundle.understanding.snapshot_id)
            if actual != (item.material_snapshot_ref, item.facts_snapshot_ref, item.understanding_snapshot_ref):
                raise DocumentPreparationConflictError('the fixed Report input references disagree')
            document_id = item.preparation_receipt.document_id
            if document_id in seen and seen[document_id] != actual:
                raise DocumentPreparationConflictError('duplicate document inputs select different frozen content')
            seen[document_id] = actual
            await self.materials.read_original(scope=manifest.scope, material_snapshot_ref=item.material_snapshot_ref)
            bundles.append(bundle)
        return bundles

    async def _outcome(self, manifest, item, bundle, receipt):
        record = await self.read_legacy_revision(
            receipt.canonical_report_id,
            receipt.legacy_source_revision,
            user_id=manifest.scope.user_id,
            subject_id=manifest.scope.subject_id,
        )
        metadata, understanding = bundle.material.payload['documentMetadata'], bundle.understanding.payload
        if record is None:
            raise DocumentNotFoundError('the exact published base revision is unavailable')
        if (
            record.user_id != manifest.scope.user_id
            or record.subject_id != manifest.scope.subject_id
            or record.report_id != receipt.canonical_report_id
            or record.canonical_report_id != bundle.material.source_id
            or record.document_id != item.preparation_receipt.document_id
            or record.source_revision != receipt.legacy_source_revision
            or record.source_fingerprint != metadata['sourceFingerprint']
        ):
            raise DocumentPreparationConflictError('the published base revision differs from the fixed input')
        report = record.public_result()
        report['documentId'] = record.document_id
        return ProcessedHealthReport(
            report=report,
            text=bundle.material.payload['parsedDocument']['text'],
            document_id=record.document_id,
            trace_id=item.preparation_receipt.trace_id,
            ingestion_status=receipt.ingestion_status,
            ingestion_job_id=receipt.ingestion_job_id,
            reused=item.preparation_receipt.reused,
            archive_trace_id=bundle.material.payload['parseArtifactRef'],
            file_name=metadata['fileName'],
            resolved_file_type=metadata['fileType'],
            content_sha256=metadata['contentSha256'],
            canonical_report_id=receipt.canonical_report_id,
            source_revision=int(receipt.legacy_source_revision),
            input_snapshot=DocumentInputSnapshot(
                parse_artifact_ref=bundle.material.payload['parseArtifactRef'],
                interpretation_artifact_ref=understanding['interpretationArtifactRef'],
                interpretation_revision=understanding['interpretationRevision'],
                validation_revision=understanding['validationRevision'],
                interpretation_round=understanding['interpretationRound'],
            ),
        )

    async def _execution(self, manifest):
        saved = await self.commands.get_command(manifest.scope, manifest.command_id)
        if saved is not None and saved['manifest'] != manifest.as_dict():
            raise DocumentPreparationConflictError('the persisted command manifest conflicts')
        execution_ref = saved.get('executionRef') if saved else None
        if execution_ref is None:
            # Validate every input before writing any formal source, including the
            # second occurrence of a repeated attachment and every original byte.
            bundles = await self._validate_inputs(manifest)
            await self.commands.put_command(manifest)
            bases, outcomes, seen = [], [], set()
            for item, bundle in zip(manifest.inputs, bundles):
                receipt = await self.compatibility.publish_prepared(
                    scope=manifest.scope,
                    understanding_snapshot_id=item.understanding_snapshot_ref,
                    selection=item.selection,
                    preparation_receipt=item.preparation_receipt,
                )
                bases.append(receipt)
                if item.preparation_receipt.document_id not in seen:
                    outcomes.append(await self._outcome(manifest, item, bundle, receipt))
                    seen.add(item.preparation_receipt.document_id)
            snapshot = DocumentSnapshot(
                manifest.scope,
                LEGACY_REPORT_INPUT_CONTRACT,
                bases[0].canonical_report_id,
                1,
                canonical_json(
                    {
                        'manifest': manifest.as_dict(),
                        'basePublications': [asdict(item) for item in bases],
                        'outcomes': [asdict(item) for item in outcomes],
                    }
                ),
            )
            await self.snapshots.put(snapshot)
            # First writer fixes the actual legacy input/receipts before the
            # output coordinator can publish a supplemental source.
            execution_ref = await self.commands.bind_execution(manifest, snapshot.snapshot_id)
        snapshot = await self.snapshots.get(manifest.scope, execution_ref)
        if (
            snapshot.contract_version != LEGACY_REPORT_INPUT_CONTRACT
            or snapshot.payload['manifest'] != manifest.as_dict()
        ):
            raise DocumentPreparationConflictError('the fixed execution snapshot conflicts')
        payload = snapshot.payload
        outcomes = []
        for saved in payload['outcomes']:
            value = dict(saved)
            value['input_snapshot'] = DocumentInputSnapshot(**value['input_snapshot'])
            value['documents'] = tuple(value['documents'])
            outcomes.append(ProcessedHealthReport(**value))
        originals, seen = [], set()
        for item in manifest.inputs:
            if item.preparation_receipt.document_id not in seen:
                originals.append(
                    await self.materials.read_original(
                        scope=manifest.scope, material_snapshot_ref=item.material_snapshot_ref
                    )
                )
                seen.add(item.preparation_receipt.document_id)
        return (
            snapshot,
            tuple(outcomes),
            tuple(originals),
            tuple(_base_receipt(item) for item in payload['basePublications']),
        )

    async def publish_report_source(self, *, scope, inputs, selection):
        manifest = DocumentReportPublicationManifest(scope, inputs, selection)
        saved = await self.commands.get_receipt(scope, manifest.command_id)
        if saved is not None:
            return await self.read_publication(scope=scope, receipt_ref=manifest.command_id)
        self._profile(selection)
        execution, outcomes, originals, bases = await self._execution(manifest)
        output = await self.coordinator.process(
            outcomes,
            user_id=scope.user_id,
            subject_id=scope.subject_id,
            output_selection=selection,
            fixed_originals=originals,
        )
        status = (output.structured_data.get('processing') or {}).get('status', 'complete')
        # Cache lookups also create a fresh coordinator audit trace. Concurrent
        # executions of this same immutable command must choose one actual
        # trace before its ID enters the immutable projection content digest.
        output_trace_id = await self.commands.bind_output_trace(
            manifest,
            binding={
                'executionSnapshotRef': execution.snapshot_id,
                'extractionId': output.extraction_id,
                'outputDigest': digest(output.structured_data),
                'legacySourceRevision': output.source_revision,
            },
            trace_id=output.output_trace_id,
        )
        projection = DocumentSnapshot(
            scope,
            LEGACY_REPORT_SOURCE_CONTRACT,
            bases[0].canonical_report_id,
            1,
            canonical_json(
                {
                    'classification': 'derived_legacy_report_source',
                    'selection': asdict(selection),
                    'executionSnapshotRef': execution.snapshot_id,
                    'extractionId': output.extraction_id,
                    'outputTraceId': output_trace_id,
                    'projection': output.structured_data,
                    'processingStatus': status,
                    'legacySourceRevision': output.source_revision,
                }
            ),
        )
        receipt = DocumentReportPublicationReceipt(
            manifest,
            projection.snapshot_id,
            bases,
            output.source_revision,
            output.ingestion_status,
            output.ingestion_job_id,
            status,
        )
        await self.snapshots.put(projection)
        await self.commands.put_receipt(receipt)
        return await self.read_publication(scope=scope, receipt_ref=manifest.command_id)

    async def read_projection(self, *, scope, projection_ref, selection):
        if selection != LEGACY_REPORT_SOURCE_SELECTION:
            raise UnsupportedOutputVersionError('the exact legacy Report projection is unavailable')
        snapshot = await self.snapshots.get(scope, projection_ref)
        if snapshot.contract_version != LEGACY_REPORT_SOURCE_CONTRACT or snapshot.payload.get('selection') != asdict(
            selection
        ):
            raise DocumentPreparationConflictError('the fixed compatibility projection conflicts')
        return snapshot

    async def read_publication(self, *, scope, receipt_ref):
        saved = await self.commands.get_receipt(scope, receipt_ref)
        if saved is None:
            raise DocumentNotFoundError('fixed Report publication acknowledgement not found in scope')
        receipt = DocumentReportPublicationReceipt.from_dict(saved)
        if receipt.manifest.scope != scope or receipt.receipt_ref != receipt_ref:
            raise DocumentPreparationConflictError('the publication acknowledgement conflicts')
        projection = await self.read_projection(
            scope=scope, projection_ref=receipt.projection_ref, selection=receipt.manifest.selection
        )
        execution = await self.snapshots.get(scope, projection.payload['executionSnapshotRef'])
        if (
            execution.contract_version != LEGACY_REPORT_INPUT_CONTRACT
            or execution.payload['manifest'] != receipt.manifest.as_dict()
            or execution.payload['basePublications'] != [asdict(item) for item in receipt.base_publications]
            or projection.source_id != receipt.canonical_report_id
            or projection.payload['legacySourceRevision'] != receipt.legacy_source_revision
            or projection.payload['processingStatus'] != receipt.processing_status
        ):
            raise DocumentPreparationConflictError('the publication acknowledgement and fixed execution disagree')
        return receipt
