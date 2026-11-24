"""Integrated Report and Record producers preserve one Profile fact per original position."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from adapters.record.current_documents import create_current_record_document_port
from adapters.report.current_document_checkpoints import MongoReportDocumentCheckpoints
from adapters.report.current_documents import CurrentReportDocumentFactory
from adapters.report.current_documents import ReportDocumentSubject
from health_profile.service import HealthProfileService
from modules.report.durable_ports import ACTIVE_GENERATION
from modules.report.schemas import RemoteFileInput
from modules.report.schemas import SubjectInfo
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import cycle
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_occurrence import page
from tests.unit._support.current_occurrence import readers_for
from tests.unit._support.current_report_projection import composition
from tests.unit.report.durable_fakes import Clock
from tests.unit.report.durable_fakes import Collection


@pytest.mark.asyncio
@pytest.mark.parametrize('order', ['record', 'report', 'parallel'])
@pytest.mark.parametrize('first_to_withdraw', ['record', 'report'])
async def test_actual_consumers_same_original(monkeypatch, order, first_to_withdraw):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    flow.readers = readers_for(flow)
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )
    a, b = await page(flow, 'A'), await page(flow, 'B')
    record_port = create_current_record_document_port(
        prepared=flow.prepared,
        documents=flow.runtime.consumption,
        ensure_self_profile=HealthProfileService(flow.repository).ensure_self_profile,
    )
    clock, batches = Clock(), Collection()
    batches.documents['same-original'] = {
        '_id': 'same-original',
        'userId': flow.scope.user_id,
        'sessionId': 'report-session',
        'durableVersion': 1,
        'generationState': 'running',
        'leaseToken': 'generation-a',
        'leaseExpiresAt': clock.now + timedelta(seconds=120),
    }
    report_factory = CurrentReportDocumentFactory(
        prepared=flow.prepared,
        report_projections=flow.readers.report_projections,
        consumer=flow.runtime.consumption,
        select_scope=AsyncMock(return_value=ReportDocumentSubject(flow.scope, flow.epoch, SubjectInfo())),
        material_reader=flow.readers.report_projections.materials,
        checkpoints=MongoReportDocumentCheckpoints(batches, clock=clock),
    )

    async def record():
        checkpoint = AsyncMock()
        result = await record_port.process_attachment(
            user_id=flow.scope.user_id,
            session_id='record-session',
            file_index=0,
            attachment={'documentId': b.receipt.document_id},
            settled_document_ids=set(),
            on_prepared=checkpoint,
        )
        checkpoint.assert_awaited_once()
        assert result.publication.publication_revision == 1

    async def report():
        token = ACTIVE_GENERATION.set(('same-original', 'generation-a'))
        try:
            report_port = report_factory.create(
                user_id=flow.scope.user_id, session_id='report-session', final_report_id='same-original'
            )
            for index, bundle in enumerate((a, b)):
                await report_port.read(RemoteFileInput(document_id=bundle.receipt.document_id), index=index)
            group = await report_port.combine([0, 1])
            refs = await report_port.settle_attachments(group.references)
            assert len(refs) == 2
            assert {ref['ingestionStatus'] for ref in refs} == {'not_submitted'}
            assert {ref['groupIngestionStatus'] for ref in refs} == {'queued'}
            assert all(ref['groupJobId'] and ref['groupSourceRevision'] == 1 for ref in refs)
            assert {ref['groupExtractionId'] for ref in refs} == {group.references[0]['groupExtractionId']}
        finally:
            ACTIVE_GENERATION.reset(token)

    if order == 'parallel':
        await asyncio.gather(record(), report())
    elif order == 'record':
        await record()
        await settle(flow)
        await report()
    else:
        await report()
        await settle(flow)
        await record()
    await settle(flow)
    sources = {'record': b.material.source_id, 'report': a.material.source_id}
    assert sources['record'] != sources['report']
    for source_id in sources.values():
        state = await flow.ledger.read_source(flow.scope, source_id)
        assert state['appliedRevision'] == state['publicationRevision'] == state['settledRevision'] == 1
    facts = await clinical_facts(flow)
    alt = [row for row in facts if row.display_name == 'ALT']
    assert len(alt) == 2
    shared = next(row for row in alt if len({source.source_id for source in row.sources}) == 2)
    stable_id = shared.fact_id
    assert len([row for row in facts if row.display_name == 'pH']) == 4
    assert {source.source_id for source in shared.sources} == set(sources.values())
    stored = await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, stable_id)
    assert {origin.source_id for origin in stored.physical_origins} == set(sources.values())
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 2

    async def withdraw(source_id):
        await flow.runtime.publisher.retract(
            scope=flow.scope,
            source_id=source_id,
            source_revision=1,
            request_id='withdraw-' + source_id,
            expected_prior_revision=1,
            lifecycle_epoch=flow.epoch,
        )
        await settle(flow)
        state = await flow.ledger.read_source(flow.scope, source_id)
        assert state['appliedRevision'] == state['publicationRevision'] == state['settledRevision'] == 2

    first, second = (
        sources[name] for name in (first_to_withdraw, 'report' if first_to_withdraw == 'record' else 'record')
    )
    await withdraw(first)
    shared = next(row for row in await clinical_facts(flow) if row.fact_id == stable_id)
    assert {source.source_id for source in shared.sources} == {second}
    await withdraw(second)
    assert not await clinical_facts(flow)
    await cycle(flow)
    assert not await clinical_facts(flow)
