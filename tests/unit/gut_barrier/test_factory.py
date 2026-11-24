from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest


@pytest.fixture
def factory_setup(monkeypatch):
    import sys

    # Initialize shared adapters before replacing deployment settings with a narrow stub.
    import config

    collection = SimpleNamespace(with_options=Mock())
    collection.with_options.return_value = collection

    class Database:
        def __getitem__(self, name):
            assert name == 'gut_barrier_report_jobs'
            return collection

    monkeypatch.setitem(sys.modules, 'db.mongodb', SimpleNamespace(database=Database()))
    settings = SimpleNamespace(
        GUT_BARRIER_PRODUCT_URL='https://products.invalid/catalog',
        REPORT_CALLBACK_URL='https://receiver.invalid/callback',
        REPORT_PRODUCT_URL='https://products.invalid/catalog',
        REPORT_GENERATION_TIMEOUT_SECONDS=1200,
        REPORT_CALLBACK_CONCURRENCY=3,
        REPORT_CALLBACK_TIMEOUT_SECONDS=7,
        REPORT_CALLBACK_BATCH_SIZE=11,
        REPORT_CALLBACK_RETRY_DELAYS_SECONDS=(7, 19, 83),
        REPORT_CALLBACK_RETRY_JITTER_RATIO=0.2,
        REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS=99,
        REPORT_CALLBACK_POLL_INTERVAL_SECONDS=6,
    )
    monkeypatch.setitem(sys.modules, 'config.env', settings)
    monkeypatch.setattr(config, 'env', settings, raising=False)
    resources = SimpleNamespace(register_async=Mock())
    return SimpleNamespace(collection=collection, settings=settings, resources=resources)


def test_new_service_uses_own_collection_and_shared_durable_callback_settings(factory_setup):
    from adapters.gut_barrier.factory import build_gut_barrier_service
    from adapters.report.delivery import ReportCallbackTransport
    from modules.report.durable import ReportCallbackWorker

    setup = factory_setup
    service = build_gut_barrier_service(parsing_client=None, resources=setup.resources)
    assert service.callback_url == setup.settings.REPORT_CALLBACK_URL
    assert service.pipeline.products.url == setup.settings.REPORT_PRODUCT_URL
    assert service.store.collection is setup.collection and service.store.lease_seconds == 120
    assert service.store.reuse_seconds == 900
    assert service.timeout_seconds == 1200 and service.concurrency == 20
    assert service.pipeline.narrative is None
    worker = service.callback_worker
    assert isinstance(worker, ReportCallbackWorker) and isinstance(worker.transport, ReportCallbackTransport)
    assert worker.concurrency == 3 and worker.timeout_seconds == 7 and worker.batch_size == 11
    assert worker.retry_delays_seconds == (7, 19, 83) and worker.jitter_ratio == 0.2
    assert worker.backlog_log_interval_seconds == 99
    assert worker.callback_scope == 'gut_barrier'
    assert worker.transport.timeout_seconds == 7 and worker.transport.concurrency == 3
    assert setup.resources.register_async.call_count == 3
    assert service.pipeline.evidence is not None


def test_questionnaire_processing_does_not_require_model_configuration(factory_setup):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    with patch('aion.create_chat_client') as create_client:
        service = build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources)
    create_client.assert_not_called()
    assert not hasattr(service.pipeline, 'survey_classifier')
    assert factory_setup.resources.register_async.call_count == 3


def test_production_uses_public_report_port_without_running_it_during_composition(factory_setup):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    peer = SimpleNamespace(submit=AsyncMock(), get_receipt=AsyncMock())
    service = build_gut_barrier_service(
        parsing_client=None, resources=factory_setup.resources, report_followup_service=peer
    )
    assert service.followup_handler is None
    assert service.delegation_worker.port.service is peer
    assert service.delegation_policy.version == 'gut-barrier.report-delegation.v1'
    peer.submit.assert_not_awaited()
    assert service.pipeline.narrative is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['success', 'partial', 'failed'])
async def test_public_adapter_forwards_frozen_envelope_for_every_status(factory_setup, status):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    peer = SimpleNamespace(submit=AsyncMock(return_value=SimpleNamespace(task_id='independent-task')))
    service = build_gut_barrier_service(
        parsing_client=None, resources=factory_setup.resources, report_followup_service=peer
    )
    body = {
        'type': 'gutBarrier',
        'finalReportId': 'gbr-current',
        'userId': 'u',
        'sessionId': 's',
        'status': status,
        'report': {'sections': []},
    }
    assert await service.delegation_worker.port.submit(body) == 'independent-task'
    submitted = peer.submit.await_args.args[0]
    body['report']['sections'].append('later')
    assert submitted.body['report']['sections'] == []
    assert submitted.source_final_report_id == 'gbr-current'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'type': 'evaluate'}, {'type': None}, {'finalReportId': None}])
async def test_public_adapter_rejects_non_gut_envelopes(factory_setup, change):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    peer = SimpleNamespace(submit=AsyncMock())
    service = build_gut_barrier_service(
        parsing_client=None, resources=factory_setup.resources, report_followup_service=peer
    )
    with pytest.raises(ValueError, match='identity'):
        await service.delegation_worker.port.submit(
            {'type': 'gutBarrier', 'finalReportId': 'gbr-id', 'userId': 'u', 'sessionId': 's', **change}
        )
    peer.submit.assert_not_awaited()


@pytest.mark.parametrize('prefix', ['', 'SHULAN_'])
@pytest.mark.parametrize('override', ['', 'https://legacy.invalid/callback', 'invalid-url'])
def test_removed_settings_cannot_disable_service_or_override_shared_callback(
    monkeypatch, factory_setup, prefix, override
):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    monkeypatch.setenv(f'{prefix}GUT_BARRIER_ENABLED', 'false')
    monkeypatch.setenv(f'{prefix}GUT_BARRIER_CALLBACK_URL', override)
    service = build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources)
    assert service is not None
    assert service.callback_url == factory_setup.settings.REPORT_CALLBACK_URL
    assert factory_setup.resources.register_async.call_count == 3


def test_shared_callback_url_comes_from_centralized_configuration(monkeypatch, factory_setup):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    monkeypatch.setattr(factory_setup.settings, 'REPORT_CALLBACK_URL', ' https://shared.invalid/callback ')
    service = build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources)
    assert service.callback_url == 'https://shared.invalid/callback'


@pytest.mark.parametrize('url', ['', 'https://gut-barrier.invalid/catalog'])
def test_product_url_comes_from_centralized_configuration(monkeypatch, factory_setup, url):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    monkeypatch.setattr(factory_setup.settings, 'GUT_BARRIER_PRODUCT_URL', url)
    service = build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources)
    assert service.pipeline.products.url == url


@pytest.mark.parametrize(
    'url',
    [
        '',
        '   ',
        'javascript:alert(1)',
        'http:///callback',
        'http://username:password@receiver.invalid/callback',
        'https://receiver.invalid/callback#fragment',
        'http://receiver.invalid:invalid/callback',
        'http://receiver.invalid:65536/callback',
        'http://[invalid/callback',
    ],
)
def test_invalid_callback_configuration_is_rejected(monkeypatch, factory_setup, url):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    monkeypatch.setattr(factory_setup.settings, 'REPORT_CALLBACK_URL', url)
    with pytest.raises(ValueError, match='REPORT_CALLBACK_URL'):
        build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources)
    factory_setup.collection.with_options.assert_not_called()
    factory_setup.resources.register_async.assert_not_called()


def test_reviewed_risk_model_is_explicitly_injected_and_versioned(factory_setup):
    from adapters.gut_barrier.factory import build_gut_barrier_service
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.risk_evaluator import RiskModelRegistry
    from tests.unit.gut_barrier.test_risk_model_adapter import MODEL_ID
    from tests.unit.gut_barrier.test_risk_model_adapter import MODEL_VERSION
    from tests.unit.gut_barrier.test_risk_model_adapter import evaluator
    from tests.unit.gut_barrier.test_risk_model_adapter import specification

    registry = RiskModelRegistry()
    registry.register(specification(), evaluator())
    service = build_gut_barrier_service(
        parsing_client=None,
        resources=factory_setup.resources,
        risk_models=registry,
        risk_model_id=MODEL_ID,
        risk_model_version=MODEL_VERSION,
    )
    assert service.pipeline.risk_models is registry
    assert service.pipeline.risk_model_id == MODEL_ID
    assert service.pipeline.version != GutBarrierPipeline.version
    assert (
        service.pipeline.version == GutBarrierPipeline(risk_model_id=MODEL_ID, risk_model_version=MODEL_VERSION).version
    )
    assert service.pipeline.version != GutBarrierPipeline(risk_model_id=MODEL_ID, risk_model_version='other').version


@pytest.mark.parametrize(
    'config',
    [{'risk_model_id': 'missing'}, {'risk_models': object()}, {'risk_model_id': '', 'risk_model_version': '1'}],
)
def test_incomplete_risk_configuration_fails_before_creating_resources(factory_setup, config):
    from adapters.gut_barrier.factory import build_gut_barrier_service

    with pytest.raises(ValueError, match='registered, reviewed'):
        build_gut_barrier_service(parsing_client=None, resources=factory_setup.resources, **config)
    factory_setup.resources.register_async.assert_not_called()
