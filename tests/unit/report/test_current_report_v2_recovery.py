"""Document factory recovery while Report v3 is an explicit opt-in."""

from dataclasses import asdict
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.basic_info import create_document_basic_info_prepared_service
from adapters.analysis.basic_info import create_document_basic_info_source_readers
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from modules.report.document_projection import project_current_report_source_with_observation_refs
from tests.unit._support.current_document import TEXT
from tests.unit._support.current_document import bson_collection
from tests.unit._support.current_document import capture
from tests.unit._support.current_document import environment

# Independently generated using the Report production modules from develop f2b0b5c.
V2_PROJECTION_DIGEST = '20f49901345d45c2a14d34fff1c696129d7e8a8712a327d23f797479c8aaacc2'


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpoint', ['command', 'receipt', 'complete'])
async def test_actual_document_factory_keeps_accepted_v2_projection_executable(monkeypatch, checkpoint):
    def clinical(raw):
        raw.update(
            observations=[{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L', 'kind': 'measurement'}],
            reportType='lab',
            reportTime='2026-11-01',
        )

    content = b'synthetic report'
    env = await environment(edit=clinical, text=TEXT + 'ALT 12 U/L\n')
    env.document.update(contentSha256=sha256(content).hexdigest(), byteSize=len(content))
    env.parsed.update(success=True)
    bundle = replace(await capture(env), receipt=env.original.receipt)
    prepared = create_document_basic_info_prepared_service(understanding=env.api, preparation_records=bson_collection())
    fixed = await prepared.remember(bundle)
    records = bson_collection()
    raw_reader = AsyncMock(return_value=content)
    material_reader = SimpleNamespace(read_original=raw_reader)

    def new_reader():
        return create_document_basic_info_source_readers(
            prepared=prepared, ledger=object(), admission_records=records, material_reader=material_reader
        ).report_projections

    args = dict(scope=env.scope, inputs=(fixed,), request_id='test-fixed-v2', lifecycle_epoch=1)
    original = new_reader()
    if checkpoint == 'command':
        original.projector = AsyncMock(side_effect=OSError('interrupted after command'))
        with pytest.raises(OSError, match='after command'):
            await original.prepare(**args)
    elif checkpoint == 'receipt':
        with monkeypatch.context() as fault:
            fault.setattr(env.store, 'put', AsyncMock(side_effect=OSError('interrupted after receipt')))
            with pytest.raises(OSError, match='after receipt'):
                await original.prepare(**args)
    else:
        await original.prepare(**args)
    manifest = await original.records.require('current_report_projection_command', env.scope, args['request_id'])
    receipt = await original.records.get('current_report_projection_receipt', env.scope, args['request_id'])
    assert (receipt is not None) == (checkpoint != 'command')

    if checkpoint == 'complete':
        raw_reader.side_effect = AssertionError('a fixed read must not reopen the original')
    restarted = new_reader()
    final = await restarted.prepare(**args)

    assert manifest == final.payload['manifest']
    assert manifest['interpretationProjectionRevision'] == 'report-understanding-current.v2'
    assert manifest['selection'] == asdict(REPORT_SOURCE_SELECTION)
    assert final.payload['projectionDigest'] == V2_PROJECTION_DIGEST
    final_receipt = await restarted.records.require('current_report_projection_receipt', env.scope, args['request_id'])
    if receipt is not None:
        assert final_receipt == receipt
    assert final_receipt['snapshotRef'] == final.snapshot_id
    assert final_receipt['contentDigest'] == final.content_digest
    assert final_receipt['projectionDigest'] == V2_PROJECTION_DIGEST
    assert await restarted.read(scope=env.scope, snapshot_id=final.snapshot_id) == final
    (_, staged) = await project_current_report_source_with_observation_refs(((bundle, content),))
    (alt,) = staged['supplementaryHospitalData']
    assert alt['analysisObservationRefs'] == ['/observations/0']
    assert raw_reader.await_count == (1 if checkpoint == 'complete' else 2)
