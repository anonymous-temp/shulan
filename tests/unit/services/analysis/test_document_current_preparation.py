"""The current Document boundary consumes only public, fixed Analysis artifacts."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_understanding import DocumentCurrentUnderstandingService
from modules.analysis.preparation import DocumentPreparationBinding
from tests.unit._support.current_document import TEXT
from tests.unit._support.current_document import basic_fields
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_snapshots import SCOPE

SELECTION = OutputSelection('health_report', 'current-contract', 'synthetic-current-renderer')


def preparation():
    document = {
        '_id': 'fixed-document',
        **SCOPE.as_dict(),
        'canonicalReportId': 'fixed-source',
        'contentSha256': 'a' * 64,
        'fileName': 'synthetic.pdf',
        'fileType': 'pdf',
        'status': 'completed',
        'parserVersion': 'synthetic-parser',
        'interpretationVersion': 'synthetic-current-extractor',
        'interpretationValidatedVersion': 'synthetic-current-validator',
        'interpretationResultRound': 7,
        'interpretationRound': 7,
        'payloadRefs': {'original_file': 'original', 'docmind_result': 'parse', 'interpretation': 'interpretation'},
    }
    parsed = {'text': TEXT, 'pages': [{'page': 1, 'text': TEXT}]}
    raw = {
        'reportType': 'laboratory',
        'reportTime': '2026-11-01',
        'observations': [],
        'processing': {'status': 'partial'},
        **basic_fields(),
    }
    binding = DocumentPreparationBinding.capture(document, parsed, raw)
    fixed = SimpleNamespace(
        user_id=SCOPE.user_id,
        subject_id=SCOPE.subject_id,
        document_id=document['_id'],
        preparation_binding=binding,
        parsed_document=parsed,
        interpretation=raw,
        output_selection=SELECTION,
    )
    outcome = SimpleNamespace(
        document_id=document['_id'],
        trace_id='request-trace',
        reused=False,
        preparation_binding=binding,
        output_selection=SELECTION,
    )
    port = SimpleNamespace(
        prepare_document=AsyncMock(return_value=outcome), read_prepared_round=AsyncMock(return_value=fixed)
    )
    store = MongoDocumentSnapshotStore(snapshots=MemoryMongoCollection(), chunks=MemoryMongoCollection())
    return SimpleNamespace(port=port, fixed=fixed, store=store)


@pytest.mark.asyncio
async def test_current_prepare_uses_public_port_and_actual_opaque_revision_only():
    env = preparation()
    service = DocumentCurrentUnderstandingService(snapshots=env.store, preparation=env.port)
    bundle = await service.prepare_bundle(scope=SCOPE, file_id='https://example.test/synthetic.pdf')
    env.port.prepare_document.assert_awaited_once_with(
        user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, file_id='https://example.test/synthetic.pdf'
    )
    env.port.read_prepared_round.assert_awaited_once_with(
        user_id=SCOPE.user_id, subject_id=SCOPE.subject_id, document_id='fixed-document', interpretation_round=7
    )
    assert bundle.facts.payload['extractorRevision'] == 'synthetic-current-extractor'
    assert bundle.facts.payload['validationRevision'] == 'synthetic-current-validator'
    assert bundle.understanding.payload['selection']['contract_version'] == 'current-contract'
    assert bundle.material.payload['parsedDocument'] == env.fixed.parsed_document
    assert bundle.facts.payload['clinicalAccepted'] is False
    env.port.prepare_document.reset_mock()
    env.port.read_prepared_round.reset_mock()
    restored = await service.read_bundle(scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id)
    assert restored.understanding == bundle.understanding
    env.port.prepare_document.assert_not_awaited()
    env.port.read_prepared_round.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['scope', 'binding', 'selection', 'round'])
async def test_current_prepare_rejects_wrong_fixed_identity_without_archiving(mutation):
    env = preparation()
    fixed = deepcopy(env.fixed)
    if mutation == 'scope':
        fixed.subject_id = 'another-subject'
    elif mutation == 'binding':
        fixed.parsed_document['text'] += 'changed'
    elif mutation == 'selection':
        fixed.output_selection = OutputSelection('health_report', 'current-contract', 'different-extractor')
    else:
        from dataclasses import replace

        fixed.preparation_binding = replace(
            fixed.preparation_binding,
            input_snapshot=replace(fixed.preparation_binding.input_snapshot, interpretation_round=8),
        )
    env.port.read_prepared_round.return_value = fixed
    service = DocumentCurrentUnderstandingService(snapshots=env.store, preparation=env.port)
    with pytest.raises((ValueError, RuntimeError)):
        await service.prepare_bundle(scope=SCOPE, file_id='https://example.test/synthetic.pdf')
    assert not env.store.snapshots.documents


@pytest.mark.asyncio
async def test_current_archive_validates_original_parse_digest_on_read():
    from dataclasses import replace

    from modules.analysis.snapshots import canonical_json

    env = preparation()
    service = DocumentCurrentUnderstandingService(snapshots=env.store, preparation=env.port)
    bundle = await service.prepare_bundle(scope=SCOPE, file_id='https://example.test/synthetic.pdf')
    payload = bundle.material.payload
    payload['parsedDocument']['pages'][0]['text'] = 'different fixed parse'
    forged = replace(bundle.material, _payload_json=canonical_json(payload))
    facts_payload = bundle.facts.payload
    facts_payload['materialSnapshotRef'] = forged.snapshot_id
    facts = replace(bundle.facts, _payload_json=canonical_json(facts_payload))
    understanding_payload = bundle.understanding.payload
    understanding_payload.update(materialSnapshotRef=forged.snapshot_id, factsSnapshotRef=facts.snapshot_id)
    understanding = replace(bundle.understanding, _payload_json=canonical_json(understanding_payload))
    with pytest.raises(ValueError, match='actual binding'):
        replace(bundle, material=forged, facts=facts, understanding=understanding)
