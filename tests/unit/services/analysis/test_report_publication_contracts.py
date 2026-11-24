"""Recovery must preserve the chosen implementation and ordered source inputs."""

from copy import deepcopy
from dataclasses import replace

import pytest
from bson import BSON
from bson.int64 import Int64

from modules.analysis.compatibility import DocumentLegacyPublicationReceipt
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.report_publication_contracts import DocumentReportPublicationReceipt
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentPreparationReceipt


def input_for(document):
    return DocumentPreparedPublicationInput(
        f'material-{document}',
        f'facts-{document}',
        f'understanding-{document}',
        UNDERSTANDING_SELECTION,
        DocumentPreparationReceipt(document, document, False),
    )


def manifest():
    return DocumentReportPublicationManifest(
        DocumentScope('user', 'self'), (input_for('a'), input_for('b')), LEGACY_REPORT_SOURCE_SELECTION
    )


class FakeReportPublicationPort:
    """Contract fake only; the production executor is a separate provider step."""

    def __init__(self):
        self.receipts = {}
        self.executions = []

    async def publish_report_source(self, *, scope, inputs, selection):
        command = DocumentReportPublicationManifest(scope, inputs, selection)
        if command.command_id not in self.receipts:
            self.executions.append(command)
            self.receipts[command.command_id] = DocumentReportPublicationReceipt(
                command,
                'projection-' + command.command_id,
                tuple(
                    DocumentLegacyPublicationReceipt(
                        item.preparation_receipt,
                        'canonical-' + item.preparation_receipt.document_id,
                        1,
                        'queued',
                        'base-job',
                    )
                    for item in inputs
                ),
                100,
                'queued',
                'projection-job',
                'complete',
            )
        return self.receipts[command.command_id]

    async def read_publication(self, *, scope, receipt_ref):
        result = self.receipts[receipt_ref]
        if result.manifest.scope != scope:
            raise PermissionError('publication outside scope')
        return result


def test_persisted_manifest_keeps_exact_versions_refs_and_order_after_bson_round_trip():
    original = manifest()
    persisted = BSON.encode(original.as_dict()).decode()
    restored = DocumentReportPublicationManifest.from_dict(persisted)
    assert restored == original and restored.command_id == original.command_id
    assert replace(restored, inputs=tuple(reversed(restored.inputs))).command_id != original.command_id
    assert replace(restored, scope=DocumentScope('other', 'self')).command_id != original.command_id
    changed = replace(restored.inputs[0], understanding_snapshot_ref='changed')
    assert replace(restored, inputs=(changed, restored.inputs[1])).command_id != original.command_id


@pytest.mark.parametrize('field', ['commandRevision', 'selection', 'inputs', 'scope'])
def test_missing_persisted_manifest_never_maps_to_a_current_default(field):
    value = manifest().as_dict()
    value.pop(field)
    with pytest.raises(ValueError, match='complete persisted'):
        DocumentReportPublicationManifest.from_dict(value)


def test_input_and_output_implementation_changes_are_explicitly_rejected():
    with pytest.raises(UnsupportedOutputVersionError):
        replace(manifest(), selection=OutputSelection('report_source', '1', 'report-source-v17'))
    with pytest.raises(UnsupportedOutputVersionError):
        replace(input_for('a'), selection=OutputSelection('legacy_understanding', '2', 'new'))


@pytest.mark.asyncio
async def test_fake_consumer_replays_the_frozen_command_and_reads_without_another_execution():
    fake = FakeReportPublicationPort()
    command = manifest()
    receipt = await fake.publish_report_source(scope=command.scope, inputs=command.inputs, selection=command.selection)
    frozen = DocumentReportPublicationManifest.from_dict(deepcopy(command.as_dict()))
    assert (
        await fake.publish_report_source(scope=frozen.scope, inputs=frozen.inputs, selection=frozen.selection)
        == receipt
    )
    assert await fake.read_publication(scope=command.scope, receipt_ref=receipt.receipt_ref) == receipt
    assert len(fake.executions) == 1
    with pytest.raises(PermissionError):
        await fake.read_publication(scope=DocumentScope('other', 'self'), receipt_ref=receipt.receipt_ref)


@pytest.mark.asyncio
async def test_receipt_does_not_promote_partial_outputs_or_reorder_base_publications():
    command = manifest()
    fake = FakeReportPublicationPort()
    receipt = await fake.publish_report_source(scope=command.scope, inputs=command.inputs, selection=command.selection)
    with pytest.raises(ValueError, match='incomplete'):
        replace(receipt, processing_status='partial')
    partial = replace(
        receipt,
        processing_status='partial',
        legacy_source_revision=None,
        ingestion_status='not_submitted',
        ingestion_job_id=None,
    )
    assert partial.canonical_report_id == receipt.canonical_report_id
    with pytest.raises(ValueError, match='ordered'):
        replace(receipt, base_publications=tuple(reversed(receipt.base_publications)))
    assert type(replace(receipt, legacy_source_revision=Int64(2**40)).legacy_source_revision) is int
    with pytest.raises(ValueError, match='legacy revision'):
        replace(receipt, legacy_source_revision=True)
