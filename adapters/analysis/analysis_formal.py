"""Document-owned Analysis assembly and compatibility aliases through public Profile services."""

from types import SimpleNamespace

from adapters.analysis.formal_source_store import MongoDocumentFormalSourceStore
from adapters.analysis.managed_source import ProfileManagedDocumentArchive
from modules.analysis.analysis_formal import AnalysisFormalProcessingService
from modules.analysis.analysis_formal import AnalysisFormalPublication
from modules.analysis.analysis_formal_delivery import AnalysisFormalDelivery
from modules.analysis.analysis_formal_delivery import AnalysisFormalSteps
from modules.analysis.analysis_formal_readiness import AnalysisFormalReadiness
from modules.analysis.compatibility import _legacy_revision
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.managed_archive import archive_result
from modules.analysis.snapshots import digest
from utils.report_content import report_source_content


class AnalysisFormalAliases:
    def __init__(self, *, compatibility, managed, profile_archive):
        self.compatibility, self.managed = compatibility, managed
        self.source = compatibility.legacy_publication.source
        self.profile_archive = ProfileManagedDocumentArchive(profile_archive)

    async def read_base(self, scope, base):
        if base['kind'] == 'archive':
            saved = await self.managed.archive.read(scope=scope, archive_ref=base['archiveRef'])
            if (saved['receipt']['canonicalReportId'], saved['receipt']['legacySourceRevision']) != (
                base['canonicalReportId'],
                base['legacySourceRevision'],
            ):
                raise DocumentFormalSourceConflict('Analysis base archive acknowledgement differs')
            return self.managed.archive.public_result(saved)
        if base['kind'] != 'legacy':
            raise DocumentFormalSourceConflict('Analysis base has no actual historical receipt')
        record = await self.source.get_owned_revision(
            base['canonicalReportId'], base['legacySourceRevision'], user_id=scope.user_id, subject_id=scope.subject_id
        )
        if record is None:
            raise LookupError('the fixed Analysis base history is unavailable')
        return record.public_result()

    async def publish(self, command, item, base):
        scope = command.scope
        bundle = await self.compatibility.validate_prepared(
            scope=scope,
            understanding_snapshot_id=item.understanding_snapshot_ref,
            selection=item.selection,
            preparation_receipt=item.preparation_receipt,
        )
        if base['canonicalReportId'] != bundle.material.source_id:
            raise DocumentFormalSourceConflict('Analysis base belongs to another source')
        await self.read_base(scope, base)
        alias = command.report_id
        if alias is None or alias == bundle.material.source_id:
            return {'kind': 'canonical', 'reportId': bundle.material.source_id}
        metadata = bundle.material.payload['documentMetadata']
        existing = await self.source.get_owned(alias, scope.user_id, scope.subject_id)
        if existing is not None:
            if (existing.canonical_report_id, existing.document_id, existing.source_fingerprint) != (
                bundle.material.source_id,
                metadata['documentId'],
                metadata['sourceFingerprint'],
            ):
                raise DocumentFormalSourceConflict('the existing Analysis alias belongs to another document')
            # Historical aliases keep their first saved content, including legacy
            # delivery state. Do not migrate, enqueue or overwrite that history.
            return {
                'kind': 'existing_alias',
                'reportId': alias,
                'legacySourceRevision': _legacy_revision(existing.source_revision),
                'contentDigest': digest(existing.result),
            }
        if base['kind'] == 'legacy':
            saved = await self.compatibility.legacy_publication.save(
                report_id=alias,
                user_id=scope.user_id,
                subject_id=scope.subject_id,
                source_fingerprint=metadata['sourceFingerprint'],
                result=report_source_content(bundle.understanding.payload['interpretation']),
                document_id=metadata['documentId'],
                canonical_report_id=bundle.material.source_id,
            )
            record = saved.record
            if (
                record.report_id,
                record.user_id,
                record.subject_id,
                record.canonical_report_id,
                record.document_id,
                record.source_fingerprint,
            ) != (
                alias,
                scope.user_id,
                scope.subject_id,
                bundle.material.source_id,
                metadata['documentId'],
                metadata['sourceFingerprint'],
            ):
                raise DocumentFormalSourceConflict('legacy alias acknowledged a different document')
            return {
                'kind': 'legacy_alias',
                'reportId': alias,
                'legacySourceRevision': _legacy_revision(record.source_revision),
                'contentDigest': digest(record.result),
            }
        request = {
            'scope': scope.as_dict(),
            'canonicalReportId': bundle.material.source_id,
            'reportId': alias,
            'sourceFingerprint': metadata['sourceFingerprint'],
            'documentId': metadata['documentId'],
            'result': archive_result(bundle),
            'sourceKey': None,
        }
        receipt = await self.profile_archive.archive(scope=scope, request=request)
        identity = {
            key: request[key]
            for key in ('scope', 'canonicalReportId', 'reportId', 'sourceFingerprint', 'documentId', 'result')
        }
        identity['legacySourceRevision'] = 1
        if receipt['contentDigest'] != digest(identity) or any(
            receipt[key] != identity[key] for key in identity if key != 'result'
        ):
            raise DocumentFormalSourceConflict('managed alias acknowledged different fixed material')
        return {'kind': 'archive_alias', 'receipt': receipt}


def create_analysis_formal_processing_service(
    *, processing, compatibility, formal, managed, profile_archive, command_records, policy=None, clock=None
):
    """No I/O or worker start; reuse root-owned providers and a dedicated permanent collection."""
    if (
        not processing.supports_document_reuse
        or compatibility.understanding.processing is not processing
        or formal.steps.compatibility is not compatibility
        or managed.publisher.ledger is not compatibility.legacy_publication.ledger
    ):
        raise ValueError('Analysis must share the actual fixed Document providers and gate')
    aliases = AnalysisFormalAliases(compatibility=compatibility, managed=managed, profile_archive=profile_archive)
    options = {key: value for key, value in {'policy': policy, 'clock': clock}.items() if value is not None}
    store = MongoDocumentFormalSourceStore(command_records)
    readiness = AnalysisFormalReadiness(
        store=store,
        compatibility=compatibility,
        index=processing._document_reuse.index,
        responses=processing._document_trace_store,
    )
    delivery = AnalysisFormalDelivery(
        store=store,
        steps=AnalysisFormalSteps(
            compatibility=compatibility,
            formal=formal,
            aliases=aliases,
            index=processing._document_reuse.index,
            readiness=readiness,
        ),
        **options,
    )
    publication = AnalysisFormalPublication(
        compatibility=compatibility, formal=formal, delivery=delivery, aliases=aliases
    )
    return SimpleNamespace(
        service=AnalysisFormalProcessingService(processing=processing, publication=publication), delivery=delivery
    )
