"""Persist Analysis base/alias intent before HTTP completion; background reads never regenerate."""

from copy import deepcopy

from modules.analysis.analysis_formal_contracts import AnalysisFormalCommand
from modules.analysis.formal_source import DocumentFormalSourceService
from modules.analysis.formal_source import DocumentFormalSourceWait
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.formal_source_contracts import DocumentFormalSourcePolicy
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import digest


class AnalysisFormalDelivery(DocumentFormalSourceService):
    command_type = AnalysisFormalCommand

    async def submit(self, command):
        return await self._submit_command(command, self.command_type)

    def _command(self, row):
        if row is None:
            raise LookupError('the accepted Analysis delivery is unavailable')
        command = self.command_type.from_dict(row['command'])
        if command.command_id != row['_id'] or digest(command.as_dict()) != row['commandDigest']:
            raise DocumentFormalSourceConflict('the accepted Analysis delivery binding changed')
        DocumentFormalSourcePolicy(**row['policy'])
        return command

    @staticmethod
    def _receipt(row):
        return deepcopy(
            {
                'commandId': row['_id'],
                'commandRevision': row['command']['command_revision'],
                'status': row['status'],
                'phase': row['phase'],
                'progress': row['progress'],
                'errorCode': row.get('errorCode'),
            }
        )


class AnalysisFormalSteps:
    def __init__(self, *, compatibility, formal, aliases, index, readiness):
        self.compatibility, self.formal, self.aliases, self.index = compatibility, formal, aliases, index
        self.readiness = readiness

    async def step(self, command, row):
        phase, progress = row['phase'], deepcopy(row['progress'])
        if phase == 'prepare':
            ready, bundle = await self.readiness.prepare(command)
            if bundle.understanding.payload['quality'].get('status') == 'unavailable':
                return {'status': 'not_published'}
            return {'phase': 'submit_base', 'progress': {'prepared': ready['prepared']}}
        item = DocumentPreparedPublicationInput.from_dict(progress['prepared'])
        # A separately runnable Document worker must never see the base command
        # until its fixed response and all preparation references are durable.
        await self.readiness.prepare(command, prepared=item)
        if phase == 'submit_base':
            await self.formal.submit(command.base_command(item))
            return {'phase': 'wait_base'}
        if phase == 'wait_base':
            receipt = await self.formal.read_receipt(
                scope=command.scope, command_id=command.base_command(item).command_id
            )
            bases = receipt['result']['basePublications']
            if receipt['status'] in {'failed', 'not_published'}:
                return {'status': receipt['status'], 'progress': {**progress, 'baseReceipt': receipt}}
            if not bases:
                raise DocumentFormalSourceWait()
            if len(bases) != 1 or bases[0]['kind'] not in {'legacy', 'archive'}:
                raise DocumentFormalSourceConflict('Analysis requires one actual base acknowledgement')
            return {'phase': 'alias', 'progress': {**progress, 'base': bases[0]}}
        if phase == 'alias':
            progress['alias'] = await self.aliases.publish(command, item, progress['base'])
            return {'phase': 'finish', 'progress': progress}
        if phase == 'finish':
            return {'status': 'completed'}
        raise DocumentFormalSourceConflict('unknown Analysis delivery phase')
