"""Bulk callback migration uses persisted results and never invokes a real service."""

from copy import deepcopy
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from modules.report.schemas import ReportRequest
from scripts import report_callbacks
from tests.unit.report.test_durable_reports import complete
from tests.unit.report.test_durable_reports import setup

TARGET = 'http://new-receiver.test:9080/callback'


async def create_batch(env, user_id, *, generate=True):
    batch = await env.service.submit(ReportRequest(user_id=user_id, session_id='session'))
    if generate:
        await env.service.drain_generation()
    return batch


@pytest.mark.asyncio
async def test_migration_pages_all_users_preserves_confirmed_results_and_updates_future_types():
    env = setup()
    batch = await create_batch(env, 'one')
    confirmed = await env.workflow.claim_delivery('confirmed')
    await env.workflow.acknowledge_delivery(confirmed)
    in_flight = await env.workflow.claim_delivery('old-target')
    done = await create_batch(env, 'done')
    await env.deliveries.update_many({'finalReportId': done['_id']}, {
        '$set': {'status': 'delivered', 'deliveredAt': env.clock()}, '$unset': {'nextRetryAt': ''},
    })
    env.service.callback_url = TARGET
    current = await create_batch(env, 'current')
    env.service.callback_url = 'http://old-receiver.test/callback'
    queued = await create_batch(env, 'queued', generate=False)
    for report_id in ('historical', 'opted-in-history'):
        await env.batches.insert_one({'_id': report_id, 'userId': 'legacy'})
        await env.artifacts.insert_one({'_id': f'{report_id}:evaluate', 'finalReportId': report_id,
                                        'userId': 'legacy', 'reportType': 'evaluate',
                                        'contentHash': 'legacy-hash', 'body': {'legacy': True}})
    await env.workflow.redrive(user_id='legacy', final_report_id='opted-in-history',
                               target_url='http://old-receiver.test/callback', operator='dev', reason='explicit opt-in')
    before_batches = deepcopy(env.batches.documents)
    before_deliveries = deepcopy(env.deliveries.documents)
    before_artifacts = deepcopy(env.artifacts.documents)
    generated = env.pipeline.calls

    result = await env.workflow.migrate_pending(
        target_url=TARGET, operator='dev', reason='deployment move', page_size=1,
    )

    assert result['status'] == 'scheduled'
    assert result['scannedBatches'] == 5
    assert result['matchedBatches'] == result['migratedBatches'] == 3
    assert result['pendingDeliveries'] == 4  # Three remaining results and one explicitly opted-in historical result.
    assert result['failedBatches'] == 0
    assert env.pipeline.calls == generated
    assert env.artifacts.documents == before_artifacts
    assert env.deliveries.documents[confirmed['_id']] == before_deliveries[confirmed['_id']]
    assert not await env.workflow.acknowledge_delivery(in_flight)
    for report_id in (done['_id'], current['_id'], 'historical'):
        assert env.batches.documents[report_id] == before_batches[report_id]
        for key, row in before_deliveries.items():
            if row['finalReportId'] == report_id:
                assert env.deliveries.documents[key] == row
    for report_id in (batch['_id'], queued['_id'], 'opted-in-history'):
        moved = env.batches.documents[report_id]
        assert moved['callbackUrl'] == TARGET
        audit = moved['deliveryOperations'][-1]
        assert audit['operationId'] == result['operationId']
        assert audit['operator'] == 'dev' and audit['reason'] == 'deployment move'
    for row in env.deliveries.documents.values():
        if row['status'] != 'delivered' and row['finalReportId'] != current['_id']:
            assert row['targetUrl'] == TARGET and row['nextRetryAt'] == env.clock()
    stable = deepcopy((env.batches.documents, env.deliveries.documents))
    again = await env.workflow.migrate_pending(
        target_url=TARGET, operator='dev', reason='repeated command', page_size=2,
    )
    assert again['matchedBatches'] == again['migratedBatches'] == 0
    assert (env.batches.documents, env.deliveries.documents) == stable
    await env.service.drain_generation()
    future = [row for row in env.deliveries.documents.values() if row['finalReportId'] == queued['_id']]
    assert len(future) == 4 and all(row['targetUrl'] == TARGET for row in future)


@pytest.mark.asyncio
async def test_preview_is_read_only_and_missing_outbox_records_are_recovered_on_migration():
    env = setup()
    await complete(env)
    env.deliveries.documents.clear()  # Crash after saving artifacts, before creating callback tasks.
    before = deepcopy((env.batches.documents, env.artifacts.documents, env.deliveries.documents))
    preview = await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='move', dry_run=True)
    assert preview['status'] == 'preview' and preview['matchedBatches'] == 1
    assert preview['migratedBatches'] == preview['pendingDeliveries'] == 0
    assert (env.batches.documents, env.artifacts.documents, env.deliveries.documents) == before
    result = await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='move')
    assert result['migratedBatches'] == 1
    assert len(env.deliveries.documents) == 4
    assert all(row['targetUrl'] == TARGET for row in env.deliveries.documents.values())
    assert env.pipeline.calls == 1


@pytest.mark.asyncio
async def test_partial_failure_preserves_intent_continues_other_batches_and_can_be_repeated():
    env = setup()
    await create_batch(env, 'one')
    await create_batch(env, 'two')
    apply = env.workflow.apply_redrive
    interrupted = []

    async def fail_once(batch):
        if not interrupted:
            interrupted.append(batch['_id'])
            raise ConnectionError('private database connection details')
        await apply(batch)

    with patch.object(env.workflow, 'apply_redrive', side_effect=fail_once):
        result = await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='move', page_size=1)
    assert result['status'] == 'partial_failure'
    assert result['failedBatches'] == result['migratedBatches'] == 1
    assert result['errors'] == [{'finalReportId': interrupted[0], 'errorType': 'ConnectionError'}]
    assert 'redrivePending' in env.batches.documents[interrupted[0]]
    resumed = await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='resume')
    assert resumed['status'] == 'scheduled' and resumed['migratedBatches'] == 1
    assert all('redrivePending' not in row for row in env.batches.documents.values())
    assert all(row['targetUrl'] == TARGET for row in env.deliveries.documents.values())
    assert env.pipeline.calls == 2


@pytest.mark.asyncio
async def test_confirmation_that_wins_during_migration_is_never_reopened():
    env = setup()
    batch = await complete(env)
    redrive = env.workflow.redrive

    async def confirmed_before_update(**kwargs):
        await env.deliveries.update_many({'finalReportId': batch['_id']}, {'$set': {'status': 'delivered'}})
        return await redrive(**kwargs)

    with patch.object(env.workflow, 'redrive', side_effect=confirmed_before_update):
        await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='move')
    assert all(row['status'] == 'delivered' and row['targetUrl'] != TARGET
               for row in env.deliveries.documents.values())


@pytest.mark.asyncio
async def test_migration_excludes_new_admissions_after_its_start_time():
    env = setup()
    for report_id in ('rpt_10', 'rpt_20'):
        await env.batches.insert_one({'_id': report_id, 'userId': 'u', 'durableVersion': 1,
                                      'deliveryEnabled': True, 'createdAt': env.clock(),
                                      'callbackUrl': 'http://old.test/callback'})
    redrive = env.workflow.redrive

    async def admit_while_migrating(**kwargs):
        if kwargs['final_report_id'] == 'rpt_10':
            env.clock.advance(1)
            await env.batches.insert_one({'_id': 'rpt_15', 'userId': 'late', 'durableVersion': 1,
                                          'deliveryEnabled': True, 'createdAt': env.clock(),
                                          'callbackUrl': 'http://old.test/callback'})
        return await redrive(**kwargs)

    with patch.object(env.workflow, 'redrive', side_effect=admit_while_migrating):
        result = await env.workflow.migrate_pending(target_url=TARGET, operator='dev', reason='move', page_size=1)
    assert result['scannedBatches'] == result['migratedBatches'] == 2
    assert env.batches.documents['rpt_15']['callbackUrl'] == 'http://old.test/callback'


def test_migration_command_requires_no_identifiers_or_target_url():
    args = report_callbacks.parse_args(['migrate-pending'])
    assert args.user_id is args.final_report_id is args.target_url is None
    assert args.reason == report_callbacks.DEFAULT_MIGRATION_REASON
    assert not args.dry_run
    assert report_callbacks.parse_args(['migrate-pending', '--dry-run']).dry_run
    legacy = report_callbacks.parse_args(['retry', '--user-id', 'u', '--final-report-id', 'rpt', '--reason', 'retry'])
    assert legacy.action == 'retry'


@pytest.mark.parametrize('args', [
    ['migrate-pending', '--user-id', 'u'], ['migrate-pending', '--final-report-id', 'rpt'],
    ['migrate-pending', '--user-id', ''],
    ['migrate-pending', '--target-url', TARGET], ['status'], ['retry'],
    ['retry', '--user-id', 'u', '--final-report-id', 'rpt'],
    ['status', '--user-id', 'u', '--final-report-id', 'rpt', '--dry-run'],
])
def test_migration_does_not_silently_ignore_filters_or_relax_single_batch_requirements(args):
    with pytest.raises(SystemExit) as error:
        report_callbacks.parse_args(args)
    assert error.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('dry_run', [False, True])
async def test_command_reads_current_config_and_closes_client_without_sending_callbacks(dry_run, capsys):
    env = setup()
    await complete(env)
    database = ModuleType('db.mongodb')
    database.client = SimpleNamespace(close=AsyncMock())
    args = report_callbacks.parse_args(['migrate-pending', *(['--dry-run'] if dry_run else [])])
    with (
        patch.dict('sys.modules', {'db.mongodb': database}),
        patch('adapters.report.factory.create_application_report_artifact_store', return_value=env.archive),
        patch('config.env.REPORT_CALLBACK_URL', TARGET + '?token=private'),
    ):
        result = await report_callbacks.run(args)
    database.client.close.assert_awaited_once()
    assert result['status'] == ('preview' if dry_run else 'scheduled')
    assert result['matchedBatches'] == 1
    assert result['targetOrigin'] == 'http://new-receiver.test:9080'
    assert 'private' not in capsys.readouterr().out
    assert env.pipeline.calls == 1
    if not dry_run:
        assert all(row['targetUrl'] == TARGET + '?token=private' for row in env.deliveries.documents.values())


@pytest.mark.asyncio
async def test_command_returns_failure_exit_status_for_incomplete_migration():
    database = ModuleType('db.mongodb')
    database.client = SimpleNamespace(close=AsyncMock())
    workflow = SimpleNamespace(
        migrate_pending=AsyncMock(return_value={'status': 'partial_failure', 'failedBatches': 1}),
    )
    with (
        patch.dict('sys.modules', {'db.mongodb': database}),
        patch('adapters.report.factory.create_application_report_artifact_store',
              return_value=SimpleNamespace(workflow=workflow)),
        patch('config.env.REPORT_CALLBACK_URL', TARGET),
        pytest.raises(SystemExit) as error,
    ):
        await report_callbacks.run(report_callbacks.parse_args(['migrate-pending']))
    assert error.value.code == 1
    database.client.close.assert_awaited_once()
