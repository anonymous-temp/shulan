"""Record's fixed creation consumes actual Formal, archive, Profile and immutable providers."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.record.document_compatibility import create_prepared_record_document_port
from adapters.record.formal_documents import create_formal_record_document_port
from health_profile.service import HealthProfileService
from modules.record.documents import RECORD_FORMAL_DOCUMENT_CHECKPOINT
from tests.unit.health_profile.test_public_contracts import visible
from tests.unit.services.analysis.test_document_formal_source import build
from tests.unit.services.analysis.test_document_formal_source import flow as document_flow

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_prepared_document_creation import restart


async def environment():
    current = await document_flow()
    profile_service = HealthProfileService(current.profile.repository, clock=lambda: current.profile.now[0])

    async def ensure_self(user_id):
        return (await profile_service.ensure_self_profile(user_id)).subject.subject_id

    current.env.api.ensure_self = ensure_self
    previous = create_prepared_record_document_port(current.env.api)
    adapter = create_formal_record_document_port(current.env.api, current.formal, previous_port=previous)
    record = creation_manager(record_document_port=adapter)
    record.manager._loadHealthProfileContext = AsyncMock(return_value=None)
    current.record, current.adapter = record, adapter
    return current


async def create(current, *, count=1):
    return await current.record.manager.createSession(
        current.scope.user_id,
        'record-formal',
        file_list=(
            [{'url': 'https://example.test/fresh.pdf'}]
            if count == 1
            else [{'documentId': current.bundle.receipt.document_id} for _ in range(count)]
        ),
    )


async def saved(current):
    return await current.record.manager._session_cache.get(current.scope.user_id, 'record-formal')


async def resume(current):
    current.profile.now[0] += timedelta(seconds=31)
    restart(current.record)
    await current.record.manager.recoverPendingCreations()
    await drain(current.record)


@pytest.mark.asyncio
async def test_real_archive_receipt_allows_first_question_without_profile_application():
    current = await environment()
    result = await create(current)
    assert result['status'] == 'question'
    record = await saved(current)
    projection = record['recordDocumentProjections'][0]
    assert projection['prepared']['contractVersion'] == RECORD_FORMAL_DOCUMENT_CHECKPOINT
    assert projection['publication']['legacySourceRevision'] == 1
    assert projection['publication']['ingestionStatus'] == 'source_managed'
    assert projection['publication']['ingestionJobId'] is None
    assert record['documents'][0]['reused'] is True
    assert current.env.flow.collection.documents
    assert not await visible(current.profile)
    assert not current.env.flow.jobs.jobs
    assert len(current.env.flow.graph.calls) == len(current.env.flow.client.submit_calls) == 1
    assert await current.profile.worker.recover_pending() == 1
    assert await visible(current.profile)


@pytest.mark.asyncio
async def test_real_ack_loss_then_restarted_formal_service_never_reprepares_or_republishes():
    current = await environment()
    execute = current.formal.execute
    commands = []

    async def lost(command):
        commands.append(command.as_dict())
        await execute(command)
        raise ConnectionError('formal receipt ACK lost')

    current.formal.execute = lost
    assert (await create(current))['status'] == 'failed'
    before = await saved(current)
    checkpoint = deepcopy(before['qaCreation']['checkpoint']['preparedDocuments']['0'])
    archives = deepcopy(current.env.flow.collection.documents)
    intents = deepcopy(current.env.ledger.intents.documents)
    current.formal = build(current)
    current.adapter.publisher.formal = current.formal
    current.env.api.prepare_bundle = AsyncMock(side_effect=AssertionError('recovery cannot prepare latest'))
    current.env.api.ensure_self_scope = AsyncMock(side_effect=AssertionError('recovery cannot ensure subject'))
    current.env.flow.index.get = AsyncMock(side_effect=AssertionError('recovery cannot read mutable index'))
    await resume(current)
    record = await saved(current)
    assert record['status'] == 'active'
    assert record['recordDocumentProjections'][0]['prepared'] == checkpoint
    assert current.env.flow.collection.documents == archives
    assert current.env.ledger.intents.documents == intents
    row = await current.formal.store.get(checkpoint['formalCommand']['commandId'])
    assert row['command'] == commands[0]
    current.env.api.prepare_bundle.assert_not_awaited()
    assert len(current.env.flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_real_duplicate_attachments_wait_for_same_source_and_keep_order_and_actual_receipts():
    current = await environment()
    reserve = current.fresh.reserve_fresh_source
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=RuntimeError('Profile unavailable'))
    assert (await create(current, count=2))['status'] == 'processing'
    before = await saved(current)
    assert before['qaCreation']['attempts'] == 0
    frozen = deepcopy(before['qaCreation']['checkpoint']['preparedDocuments'])
    assert set(frozen) == {'0', '1'}
    assert frozen['0']['preparationReceipt']['traceId'] != frozen['1']['preparationReceipt']['traceId']
    assert not current.record.gateway.requests
    current.fresh.reserve_fresh_source = reserve
    current.env.api.prepare_bundle = AsyncMock(side_effect=AssertionError('fixed checkpoints already exist'))
    await resume(current)
    # The second command needs the first managed source to become active. The
    # completed first attachment remains frozen while the other source waits.
    waiting = await saved(current)
    assert waiting['status'] == 'creating'
    assert waiting['qaCreation']['attempts'] == 0
    assert len(waiting['qaCreation']['checkpoint']['files']) == 1
    assert await current.profile.worker.recover_pending() == 1
    current.profile.now[0] += timedelta(minutes=1)
    await current.recovery.recover_pending()
    await resume(current)
    record = await saved(current)
    assert record['status'] == 'active'
    assert [value['fileIndex'] for value in record['documents']] == [0, 1]
    assert len(record['parsedHealthReports']) == 1
    assert [x['prepared'] for x in record['recordDocumentProjections']] == [frozen['0'], frozen['1']]
    assert all(x['publication']['legacySourceRevision'] > 0 for x in record['recordDocumentProjections'])
    assert len(current.env.flow.graph.calls) == len(current.env.flow.client.submit_calls) == 1
    assert not current.env.flow.jobs.jobs
