"""Current Analysis HTTP strategy with real offline Document and Profile providers."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPRequest
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from tornado.web import Application

import adapters.analysis.analysis_current as current_analysis
from adapters.analysis.analysis_current import create_analysis_current_processing_service
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.snapshots import DocumentScope
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import actual_analysis_composition
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import settle
from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import Collection
from web.handlers.analysis import HealthReportHandler


async def composition(monkeypatch, replies):
    flow = await actual_analysis_composition(monkeypatch, replies)
    source = flow.upstream.source
    source.get_owned = AsyncMock(return_value=None)
    source._ensure_same_source = AsyncMock(side_effect=AssertionError('no legacy result is present'))
    flow.commands = Collection()
    flow.commands.with_options = lambda **_options: flow.commands
    flow.consumer = flow.runtime.consumption
    flow.clock = [1000.0]

    async def resolve(scope):
        return await flow.repository.get_subject(scope.user_id, scope.subject_id)

    def recreate():
        return create_analysis_current_processing_service(
            processing=flow.analysis,
            prepared=flow.prepared,
            consumption=flow.consumer,
            resolve_subject=resolve,
            command_records=flow.commands,
            clock=lambda: flow.clock[0],
        )

    flow.recreate = recreate
    flow.current = recreate()
    return flow


async def submit(flow, **overrides):
    body = {'fileId': 'https://example.test/synthetic.pdf', 'userId': flow.scope.user_id, **overrides}
    app = Application(
        [(r'/interpret', HealthReportHandler, {'processing_service_factory': lambda: flow.current.service})]
    )
    sockets = bind_sockets(0, address='127.0.0.1')
    server = HTTPServer(app)
    server.add_sockets(sockets)
    client = AsyncHTTPClient(force_instance=True)
    try:
        response = await client.fetch(
            HTTPRequest(
                f'http://127.0.0.1:{sockets[0].getsockname()[1]}/interpret',
                method='POST',
                body=json.dumps(body),
            ),
            raise_error=False,
        )
        return response.code, json.loads(response.body)
    finally:
        server.stop()
        await server.close_all_connections()
        client.close()


@pytest.mark.asyncio
async def test_http_current_fixed_aliases_and_profile_use_one_file_one_interpretation(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    first, second = await asyncio.gather(submit(flow, reportId='first-alias'), submit(flow, reportId='second-alias'))

    for alias, (status, body) in zip(('first-alias', 'second-alias'), (first, second), strict=True):
        assert status == 200
        assert body['data']['reportId'] == alias
        assert body['data']['basicInfo']['age'] == 42
        assert body['data']['processing']['status'] == 'complete'
        assert await flow.current.service.read_result(scope=flow.scope, report_id=alias) == body['data']
    assert first[1]['data']['documentId'] == second[1]['data']['documentId']
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    assert len(flow.ledger.intents.documents) == 0

    flow.current = flow.recreate()
    assert await flow.current.delivery.recover_pending() == 2
    assert len(flow.ledger.intents.documents) == 1
    await settle(flow)
    current = await info(flow)
    assert current.fields['age'].value == 42
    assert current.fields['age'].source_reliability == 'medium'
    with pytest.raises(DocumentNotFoundError):
        await flow.current.service.read_result(
            scope=DocumentScope('different-user', flow.scope.subject_id), report_id='first-alias'
        )
    status, body = await submit(flow, fileId=None, documentId=first[1]['data']['documentId'], reportId='first-alias')
    assert status == 200 and body['data'] == first[1]['data']
    assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    flow.upstream.source.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_delivery_replays_fixed_preparation_after_busy_without_model_or_download(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    real_consume = flow.consumer.consume_prepared
    consume = AsyncMock(side_effect=DocumentBusyError('synthetic temporary unavailability'))
    monkeypatch.setattr(flow.consumer, 'consume_prepared', consume)
    status, body = await submit(flow, reportId='retry-alias')
    assert status == 200
    assert await flow.current.delivery.recover_pending() == 1
    first_call = consume.await_args
    prepare = AsyncMock(side_effect=AssertionError('recovery cannot start a new interpretation'))
    monkeypatch.setattr(flow.analysis, 'prepare_document', prepare)
    flow.upstream.downloader.download.side_effect = AssertionError('recovery cannot redownload')
    consume.side_effect = real_consume
    flow.clock[0] += 31
    flow.current = flow.recreate()
    assert await flow.current.delivery.recover_pending() == 1
    assert consume.await_args == first_call
    assert await flow.current.service.read_result(scope=flow.scope, report_id='retry-alias') == body['data']
    prepare.assert_not_awaited()
    await settle(flow)
    assert len(flow.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_unusable_current_analysis_keeps_first_display_without_publication(monkeypatch):
    flow = await composition(monkeypatch, [{}, {}])
    status, body = await submit(flow, reportId='empty-alias')
    assert status == 200
    assert body['data']['processing']['status'] == 'unavailable'
    assert await flow.current.service.read_result(scope=flow.scope, report_id='empty-alias') == body['data']
    assert await flow.current.delivery.recover_pending() == 1
    assert not flow.ledger.intents.documents
    assert len(flow.upstream.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_current_alias_collision_uses_existing_http_error_and_scoped_lookup(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    status, body = await submit(flow, reportId='reserved-alias')
    assert status == 200
    flow.upstream.downloader.download.return_value = b'%PDF-1.7\nchanged-synthetic-original'
    status, _ = await submit(flow, reportId='reserved-alias')
    assert status == 409
    status, _ = await submit(flow, subjectId='different-subject')
    assert status == 404
    assert await flow.current.service.read_result(scope=flow.scope, report_id='reserved-alias') == body['data']
    assert len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_interrupted_analysis_response_can_be_archived_from_the_original_round(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    publication = flow.current.service.publication
    monkeypatch.setattr(
        publication, 'finish', AsyncMock(side_effect=ConnectionError('synthetic response interruption'))
    )
    status, _ = await submit(flow, reportId='interrupted')
    assert status == 500
    with pytest.raises(DocumentNotFoundError):
        await flow.current.service.read_result(scope=flow.scope, report_id='interrupted')
    flow.upstream.downloader.download.side_effect = AssertionError('recover from the fixed archived round')
    flow.current = flow.recreate()
    assert await flow.current.delivery.recover_pending() == 1
    assert (await flow.current.service.read_result(scope=flow.scope, report_id='interrupted'))['basicInfo']['age'] == 42
    await settle(flow)
    assert len(flow.upstream.client.submit_calls) == len(flow.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_current_fixed_analysis_result_stays_readable_when_old_lifecycle_cannot_publish(monkeypatch):
    from health_profile.models import SubjectStatus

    flow = await composition(monkeypatch, [model_report()])
    status, parsed = await submit(flow, reportId='old-lifecycle')
    assert status == 200
    for requested in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        await flow.repository.update_subject(
            subject.model_copy(update={'status': requested, 'version': subject.version + 1}),
            expected_version=subject.version,
        )
    flow.current = flow.recreate()
    assert await flow.current.delivery.recover_pending() == 1
    assert not flow.ledger.intents.documents
    assert await flow.current.service.read_result(scope=flow.scope, report_id='old-lifecycle') == parsed['data']
    (command,) = [row for row in flow.commands.documents.values() if row.get('phase')]
    assert command['status'] == 'failed' and command['errorCode'] == 'DocumentFormalSourceConflict'


@pytest.mark.asyncio
async def test_preexisting_analysis_intent_keeps_original_policy_until_new_round(monkeypatch):
    first = model_report()
    corrected = model_report()
    corrected['observations'][0]['resultValue'] = '15'
    flow = await composition(monkeypatch, [first, corrected])

    # Persist the pre-change command shape and restart before delivery.
    with monkeypatch.context() as legacy:
        legacy.setattr(current_analysis, 'ANALYSIS_PUBLIC_CONFIRMATION', None)
        status, body = await submit(flow, reportId='existing-alias')
    assert status == 200
    document_id = body['data']['documentId']
    (legacy_command,) = [row['command'] for row in flow.commands.documents.values() if row.get('phase')]
    assert 'confirmation_basis' not in legacy_command

    flow.current = flow.recreate()
    assert await flow.current.delivery.recover_pending() == 1
    await settle(flow)
    legacy_facts = await clinical_facts(flow)
    assert legacy_facts
    assert all(source.confirmation_basis is None for fact in legacy_facts for source in fact.sources)

    status, _ = await submit(flow, fileId=None, documentId=document_id, reportId='same-round-alias')
    assert status == 200
    assert await flow.current.delivery.recover_pending() == 1
    await settle(flow)
    assert await clinical_facts(flow) == legacy_facts
    assert len(flow.ledger.intents.documents) == 1

    flow.upstream.index_collection.documents[document_id].update(processingStatus='partial', processingRetryable=True)
    status, _ = await submit(flow, fileId=None, documentId=document_id, reportId='corrected-alias')
    assert status == 200
    assert await flow.current.delivery.recover_pending() == 1
    await settle(flow)
    active = [fact for fact in await clinical_facts(flow) if fact.status == 'active']
    (observation,) = active
    assert observation.value['number'] == 15
    assert observation.verification_status == 'confirmed'
    assert observation.sources[0].confirmation_basis == 'analysis_public'
    assert len(flow.ledger.intents.documents) == len(flow.gateway.complete_requests) == 2
    assert len(flow.upstream.client.submit_calls) == 1
