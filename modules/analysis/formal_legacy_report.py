"""Versioned legacy-first Report supplement using already verified base references.

The old Report command remains unchanged. A managed secondary input is read from
its actual archive; it is never sent back through a disabled legacy base writer.
"""

from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass

from modules.analysis.formal_source_contracts import FORMAL_REPORT_REUSE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.managed_archive import archive_result
from modules.analysis.managed_content import managed_public_content_v1
from modules.analysis.report_publication import _base_receipt
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.snapshots import FORMAL_LEGACY_REPORT_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest


@dataclass(frozen=True, slots=True)
class _ArchivedBase:
    canonical_report_id: str
    legacy_source_revision: int
    ingestion_status: str
    ingestion_job_id: str | None


class DocumentFormalLegacyReport:
    def __init__(self, *, publications, managed):
        self.publications, self.managed = publications, managed
        self.records = managed.commands.records

    async def publish(self, *, command, bases):
        return await self._publish(command=command, bases=bases)

    async def publish_prepared(self, *, command, bases, projection_ref):
        if command.command_revision != FORMAL_REPORT_REUSE_REVISION:
            raise DocumentFormalSourceConflict('prepared publication requires the exact formal v2 command')
        projection = await self.managed.reports.read(scope=command.scope, snapshot_id=projection_ref)
        if (
            projection.payload['manifest']['inputs'] != [item.as_dict() for item in command.inputs]
            or projection.payload['processingStatus'] != 'complete'
        ):
            raise DocumentFormalSourceConflict('the prepared output differs from the formal Report command')
        return await self._publish(command=command, bases=bases, projection=projection)

    @staticmethod
    def _same_prepared_report(outcome, bundle):
        actual = deepcopy(outcome.report)
        # These fields are added by the legacy archive, not read by the exact
        # registered v16 clinical/structured extraction. The cache still receives
        # the complete original outcome, including its actual opaque revision.
        for key in ('reportId', 'userId', 'subjectId', 'documentId', 'sourceRevision'):
            actual.pop(key, None)
        expected = managed_public_content_v1(archive_result(bundle))
        for key in ('reportId', 'userId', 'subjectId', 'documentId', 'sourceRevision'):
            expected.pop(key, None)
        if actual != expected:
            raise DocumentFormalSourceConflict('the legacy base clinical content differs from prepared Report input')

    async def _publish(self, *, command, bases, projection=None):
        binding = {'command': command.as_dict(), 'bases': list(bases), 'revision': 'formal-fixed-bases-v1'}
        if projection is not None:
            binding.update(revision='formal-fixed-bases-v2', preparedProjectionRef=projection.snapshot_id)
        key = command.command_id + ':legacy-report'
        stored = await self.records.get('formal_legacy_report_result', command.scope, key)
        if stored is not None:
            if stored['binding'] != binding:
                raise DocumentFormalSourceConflict('legacy Report supplement changed its fixed bases')
            return stored['receipt']
        self.publications._profile(LEGACY_REPORT_SOURCE_SELECTION)
        await self.records.put('formal_legacy_report_request', command.scope, key, binding)
        by_source = {item['sourceId']: item for item in bases}
        if len(by_source) != len(bases) or not bases or bases[0]['kind'] != 'legacy':
            raise DocumentFormalSourceConflict(
                'legacy supplement requires unique fixed bases and a legacy first source'
            )
        outcomes, originals, seen = [], [], set()
        for item in command.inputs:
            bundle = await self.publications.compatibility.validate_prepared(
                scope=command.scope,
                understanding_snapshot_id=item.understanding_snapshot_ref,
                selection=item.selection,
                preparation_receipt=item.preparation_receipt,
            )
            if (bundle.material.snapshot_id, bundle.facts.snapshot_id) != (
                item.material_snapshot_ref,
                item.facts_snapshot_ref,
            ):
                raise DocumentFormalSourceConflict('legacy supplement fixed references changed')
            if item.preparation_receipt.document_id in seen:
                continue
            source = by_source.get(bundle.material.source_id)
            if source is None:
                raise DocumentFormalSourceConflict('legacy supplement has no verified base reference')
            if source['kind'] == 'legacy':
                actual = await self.publications.compatibility.publish_prepared(
                    scope=command.scope,
                    understanding_snapshot_id=item.understanding_snapshot_ref,
                    selection=item.selection,
                    preparation_receipt=item.preparation_receipt,
                )
                if asdict(actual) != source['receipt']:
                    raise DocumentFormalSourceConflict('the original legacy base acknowledgement changed')
                resolved = _base_receipt(source['receipt'])
            elif source['kind'] == 'archive':
                saved = await self.managed.archive.read(scope=command.scope, archive_ref=source['archiveRef'])
                request, receipt = saved['request'], saved['receipt']
                if (
                    request['canonicalReportId'] != bundle.material.source_id
                    or request['documentId'] != item.preparation_receipt.document_id
                    or request['sourceFingerprint'] != bundle.material.payload['documentMetadata']['sourceFingerprint']
                ):
                    raise DocumentFormalSourceConflict('managed archive belongs to a different fixed base')
                if source.get('managedCommandId'):
                    actual = await self.managed.commands.read_receipt(
                        scope=command.scope, command_id=source['managedCommandId']
                    )
                    if actual['baseArchiveRef'] != source['archiveRef']:
                        raise DocumentFormalSourceConflict('managed command does not bind this archive')
                else:
                    # Only the original fresh base can have no later managed command.
                    source_material = await self.managed.source_reader.read_source_observations(
                        scope=command.scope, snapshot_id=source['sourceSnapshotRef']
                    )
                    if source_material['sourceId'] != bundle.material.source_id or request['sourceKey'] is not None:
                        raise DocumentFormalSourceConflict('fresh archive does not identify the first fixed source')
                record = await self.publications.read_legacy_revision(
                    receipt['canonicalReportId'],
                    receipt['legacySourceRevision'],
                    user_id=command.scope.user_id,
                    subject_id=command.scope.subject_id,
                )
                if record is None:
                    raise LookupError('the actual managed compatibility archive is unavailable')
                resolved = _ArchivedBase(
                    receipt['canonicalReportId'],
                    receipt['legacySourceRevision'],
                    record.ingestion_status,
                    record.ingestion_job_id,
                )
            else:
                raise DocumentFormalSourceConflict('an unaccepted base cannot feed a formal legacy supplement')
            if item.preparation_receipt.document_id not in seen:
                # This Document-owned helper reads and validates the exact stored
                # revision. _ArchivedBase describes an archive, not a legacy-write receipt.
                outcome = await self.publications._outcome(command, item, bundle, resolved)
                if projection is not None:
                    self._same_prepared_report(outcome, bundle)
                outcomes.append(outcome)
                originals.append(
                    await self.publications.materials.read_original(
                        scope=command.scope, material_snapshot_ref=item.material_snapshot_ref
                    )
                )
                seen.add(item.preparation_receipt.document_id)
        options = {
            'user_id': command.scope.user_id,
            'subject_id': command.scope.subject_id,
            'output_selection': LEGACY_REPORT_SOURCE_SELECTION,
            'fixed_originals': tuple(originals),
        }
        if projection is None:
            output = await self.publications.coordinator.process(tuple(outcomes), **options)
        else:
            output = await self.publications.coordinator.publish_prepared_report_v1(
                tuple(outcomes),
                prepared_projection=projection.payload['projection'],
                preparation_ref=projection.snapshot_id,
                preparation_trace_id=projection.payload['outputTraceId'],
                **options,
            )
        quality = output.structured_data.get('processing', {}).get('status', 'complete')
        content = {
            'binding': binding,
            'projection': output.structured_data,
            'extractionId': output.extraction_id,
            'legacySourceRevision': output.source_revision,
            'processingStatus': quality,
        }
        trace = await self.records.bind_trace(command.scope, key, digest(content), output.output_trace_id)
        snapshot = DocumentSnapshot(
            command.scope,
            FORMAL_LEGACY_REPORT_CONTRACT,
            bases[0]['sourceId'],
            1,
            canonical_json({**content, 'outputTraceId': trace}),
        )
        await self.publications.snapshots.put(snapshot)
        receipt = {
            'kind': 'legacy_report',
            'projectionRef': snapshot.snapshot_id,
            'sourceId': bases[0]['sourceId'],
            'legacySourceRevision': output.source_revision,
            'ingestionStatus': output.ingestion_status,
            'jobId': output.ingestion_job_id,
            'processingStatus': quality,
            'extractionId': output.extraction_id,
            'outputTraceId': trace,
        }
        await self.records.put(
            'formal_legacy_report_result', command.scope, key, {'binding': binding, 'receipt': receipt}
        )
        return receipt
