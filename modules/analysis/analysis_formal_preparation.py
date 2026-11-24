"""Read the accepted document round without starting parsing, a model or recovery."""

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.contracts import OutputSelection
from modules.analysis.formal_source import DocumentFormalSourceWait
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentPreparationReceipt


def _verify(command, bundle):
    material, understanding = bundle.material, bundle.understanding.payload
    if (
        material.source_id != command.canonical_report_id
        or material.payload['documentId'] != command.document_id
        or material.payload['contentSha256'] != command.content_sha256
        or understanding['interpretationRevision'] != command.interpretation_revision
        or understanding['interpretationRound'] != command.interpretation_round
    ):
        raise DocumentFormalSourceConflict('Analysis prepared material differs from its accepted execution')


def _prepared(binding):
    return DocumentPreparedPublicationInput(
        binding['materialSnapshotRef'],
        binding['factsSnapshotRef'],
        binding['understandingSnapshotRef'],
        OutputSelection(**binding['selection']),
        DocumentPreparationReceipt(**binding['preparationReceipt']),
    )


async def capture_analysis_preparation(*, compatibility, index, command):
    document = await index.get(command.document_id, command.scope.user_id, command.scope.subject_id)
    if document is None or (document.get('canonicalReportId'), document.get('contentSha256')) != (
        command.canonical_report_id,
        command.content_sha256,
    ):
        raise DocumentFormalSourceConflict('Analysis document owner or immutable identity changed')
    if command.report_id and command.report_id not in document.get('reportAliases', []):
        raise DocumentFormalSourceConflict('Analysis alias was not durably reserved for this document')
    try:
        binding = await compatibility.publications.get_preparation(command.scope, command.trace_id)
    except DocumentNotFoundError:
        binding = None
    if binding is None:
        bundle = None
        capture_round = getattr(compatibility.understanding, 'capture_round_bundle', None)
        if capture_round is not None:
            try:
                bundle = await capture_round(
                    scope=command.scope,
                    document_id=command.document_id,
                    interpretation_round=command.interpretation_round,
                    selection=UNDERSTANDING_SELECTION,
                )
            except DocumentNotFoundError as error:
                if type(error) is not DocumentNotFoundError:
                    raise
        if bundle is None:
            result_round = document.get('interpretationResultRound', document.get('interpretationRound', 0))
            if result_round > command.interpretation_round:
                raise DocumentFormalSourceConflict('a later interpretation cannot replace the accepted Analysis round')
            if document['status'] == 'failed':
                if document.get('retryable') is False:
                    raise DocumentFormalSourceConflict('the accepted Analysis preparation cannot recover')
                raise DocumentFormalSourceWait()
            if document['status'] != 'completed' or result_round < command.interpretation_round:
                raise DocumentFormalSourceWait()
            if document.get('interpretationVersion') != command.interpretation_revision:
                raise DocumentFormalSourceConflict('Analysis interpretation implementation changed')
            bundle = await compatibility.understanding.capture_bundle(
                scope=command.scope,
                document_id=command.document_id,
                selection=UNDERSTANDING_SELECTION,
            )
        _verify(command, bundle)
        receipt = DocumentPreparationReceipt(
            command.document_id, command.trace_id, command.document_id != command.trace_id
        )
        binding = compatibility._binding(bundle, UNDERSTANDING_SELECTION, receipt)
        # Write-once binding makes acknowledgement loss replay the same snapshot,
        # even after a later explicit request changes the mutable document index.
        try:
            await compatibility.publications.put_preparation(command.scope, command.trace_id, binding)
        except Exception:
            persisted = await compatibility.publications.get_preparation(command.scope, command.trace_id)
            if persisted != binding:
                raise
    item = _prepared(binding)
    if item.preparation_receipt != DocumentPreparationReceipt(
        command.document_id, command.trace_id, command.document_id != command.trace_id
    ):
        raise DocumentFormalSourceConflict('Analysis preparation receipt belongs to another request')
    bundle = await compatibility.validate_prepared(
        scope=command.scope,
        understanding_snapshot_id=item.understanding_snapshot_ref,
        selection=item.selection,
        preparation_receipt=item.preparation_receipt,
    )
    if (bundle.material.snapshot_id, bundle.facts.snapshot_id) != (item.material_snapshot_ref, item.facts_snapshot_ref):
        raise DocumentFormalSourceConflict('Analysis material and facts references differ')
    _verify(command, bundle)
    return item, bundle
