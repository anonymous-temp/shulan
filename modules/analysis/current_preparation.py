"""Durable current preparation receipts, independent of publication and Profile truth."""

from dataclasses import asdict

from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.current_understanding import DocumentPreparedInput


class DocumentPreparedService:
    def __init__(self, *, understanding, records):
        self.understanding, self.records = understanding, records

    @staticmethod
    def input(bundle):
        from modules.analysis.contracts import OutputSelection

        return DocumentPreparedInput(
            bundle.material.snapshot_id,
            bundle.facts.snapshot_id,
            bundle.understanding.snapshot_id,
            OutputSelection(**bundle.understanding.payload['selection']),
            bundle.receipt,
        )

    async def remember(self, bundle):
        prepared = self.input(bundle)
        await self.records.put_preparation(bundle.material.scope, bundle.receipt.trace_id, prepared.as_dict())
        return prepared

    async def prepare_bundle(self, *, scope, **kwargs):
        bundle = await self.understanding.prepare_bundle(scope=scope, **kwargs)
        await self.remember(bundle)
        return bundle

    async def validate_prepared(self, *, scope, prepared):
        bundle = await self.understanding.read_bundle(
            scope=scope, understanding_snapshot_id=prepared.understanding_snapshot_ref
        )
        return await self._validate_prepared_bundle(scope, prepared, bundle)

    async def _validate_prepared_bundle(self, scope, prepared, bundle):
        actual = DocumentPreparedInput(
            bundle.material.snapshot_id,
            bundle.facts.snapshot_id,
            bundle.understanding.snapshot_id,
            prepared.selection,
            prepared.preparation_receipt,
        )
        if (
            actual.as_dict() != prepared.as_dict()
            or asdict(prepared.selection) != bundle.understanding.payload['selection']
            or prepared.preparation_receipt.document_id != bundle.material.payload['documentId']
            or await self.records.get_preparation(scope, prepared.preparation_receipt.trace_id) != prepared.as_dict()
        ):
            raise DocumentPreparationConflictError('the original durable preparation binding differs')
        return bundle
