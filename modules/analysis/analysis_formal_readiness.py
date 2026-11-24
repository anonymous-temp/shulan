"""Persist the fixed Analysis response before any independently runnable delivery."""

from copy import deepcopy

from modules.analysis.analysis_formal_preparation import _verify
from modules.analysis.analysis_formal_preparation import capture_analysis_preparation
from modules.analysis.application.analysis_display import display_result
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparationReceipt
from utils.report_content import report_source_content


class AnalysisFormalReadiness:
    prepared_type = DocumentPreparedPublicationInput

    def __init__(self, *, store, compatibility, index, responses):
        self.store, self.compatibility, self.index = store, compatibility, index
        self.responses = responses

    async def prepare(self, command, *, prepared=None):
        identity = command.command_id + ':ready'
        saved = await self.store.get(identity)
        if saved is not None:
            validated, bundle = await self._validate(command, saved)
            if prepared is not None and prepared.as_dict() != validated['prepared']:
                raise DocumentFormalSourceConflict('Analysis delivery preparation differs from its readiness')
            return validated, bundle
        if prepared is None:
            prepared, bundle = await self._capture(command)
        else:
            bundle = await self._bundle(command, prepared)
        result = bundle.understanding.payload['interpretation']
        public = display_result(report_source_content(deepcopy(result)))
        public.pop('sourceRevision', None)
        if isinstance(public.get('processing'), dict):
            public['processing'].pop('sourceRevision', None)
        public.update(
            reportId=command.report_id or command.canonical_report_id,
            documentId=command.document_id,
            **command.scope.as_dict(),
        )
        await self._save_response(command, public)
        content = {
            'commandDigest': digest(command.as_dict()),
            'prepared': prepared.as_dict(),
            'responseRef': command.trace_id,
            'responseDigest': digest(public),
        }
        row = {'_id': identity, 'status': 'completed', 'content': content, 'contentDigest': digest(content)}
        try:
            actual = await self.store.insert(row)
        except Exception:
            # An acknowledged write may lose its transport response. Only the
            # same immutable content can turn that outcome into success.
            actual = await self.store.get(identity)
            if actual != row:
                raise
        if actual != row:
            raise DocumentFormalSourceConflict('Analysis readiness already binds another fixed result')
        persisted = await self.store.get(identity)
        if persisted != row:
            raise RuntimeError('Analysis readiness was not durably readable')
        return await self._validate(command, persisted)

    async def _capture(self, command):
        return await capture_analysis_preparation(compatibility=self.compatibility, index=self.index, command=command)

    async def _response(self, command):
        return await self.responses.read_payload(
            command.trace_id, command.scope.user_id, command.scope.subject_id, 'analysis_response'
        )

    async def _save_response(self, command, response):
        try:
            existing = await self._response(command)
        except LookupError:
            existing = None
        if existing is not None:
            if existing != response:
                raise DocumentFormalSourceConflict('Analysis response archive already binds another result')
            return
        try:
            await self.responses.write_payload(command.trace_id, command.scope.user_id, 'analysis_response', response)
        except Exception as error:
            try:
                persisted = await self._response(command)
            except LookupError:
                raise error
            if persisted != response:
                raise error
        if await self._response(command) != response:
            raise RuntimeError('Analysis response archive was not durably readable')

    async def _bundle(self, command, prepared):
        if prepared.preparation_receipt != DocumentPreparationReceipt(
            command.document_id, command.trace_id, command.document_id != command.trace_id
        ):
            raise DocumentFormalSourceConflict('Analysis readiness preparation belongs to another request')
        bundle = await self.compatibility.validate_prepared(
            scope=command.scope,
            understanding_snapshot_id=prepared.understanding_snapshot_ref,
            selection=prepared.selection,
            preparation_receipt=prepared.preparation_receipt,
        )
        _verify(command, bundle)
        if (bundle.material.snapshot_id, bundle.facts.snapshot_id) != (
            prepared.material_snapshot_ref,
            prepared.facts_snapshot_ref,
        ):
            raise DocumentFormalSourceConflict('Analysis readiness has inconsistent fixed references')
        return bundle

    async def _validate(self, command, row):
        content = row.get('content')
        if (
            row.get('_id') != command.command_id + ':ready'
            or row.get('status') != 'completed'
            or not isinstance(content, dict)
            or row.get('contentDigest') != digest(content)
            or content.get('commandDigest') != digest(command.as_dict())
        ):
            raise DocumentFormalSourceConflict('Analysis readiness identity or content differs')
        prepared = self.prepared_type.from_dict(content['prepared'])
        bundle = await self._bundle(command, prepared)
        response = await self._response(command)
        if (
            content.get('responseRef') != command.trace_id
            or not isinstance(response, dict)
            or digest(response) != content.get('responseDigest')
        ):
            raise DocumentFormalSourceConflict('Analysis fixed response archive is unavailable or differs')
        return {**deepcopy(content), 'response': deepcopy(response)}, bundle
