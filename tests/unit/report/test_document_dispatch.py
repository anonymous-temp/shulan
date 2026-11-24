"""Old admitted manifests keep their real factory under concurrent new tasks."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.report.agent import ReportAgent
from modules.report.agent import ReportExecutionVersionError
from modules.report.document_dispatch import ReportDocumentFactoryDispatcher
from tests.unit.report.test_durable_reports import REQUEST


class Factory:
    def __init__(self, revision):
        self.manifest = {'consumerRevision': revision, 'selection': {'version': 'fixed'}}
        self.calls = []

    def document_input_manifest(self):
        return deepcopy(self.manifest)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.manifest['consumerRevision']


def setup():
    old, new = Factory('report-fixed-documents.v1'), Factory('report-formal-documents.v1')
    dispatcher = ReportDocumentFactoryDispatcher(default=new, supported=(old,))
    pipeline = SimpleNamespace(document_source_factory=dispatcher, generation_timeout_seconds=1800.0)
    return SimpleNamespace(old=old, new=new, dispatcher=dispatcher, pipeline=pipeline, agent=ReportAgent(pipeline))


def job(env, identity, *, old=False):
    manifest = env.agent.execution_manifest()
    if old:
        manifest['documentConsumer'] = env.old.document_input_manifest()
    return {
        '_id': identity,
        'userId': REQUEST.user_id,
        'sessionId': REQUEST.session_id,
        'request': REQUEST.model_dump(by_alias=True),
        'executionManifest': manifest,
    }


@pytest.mark.asyncio
async def test_concurrent_old_new_whole_manifests_choose_exact_factory_and_reset_afterwards():
    env = setup()
    both = asyncio.Event()
    entered, seen = [], {}

    async def run(body, *, final_report_id, defer_callbacks):
        entered.append(final_report_id)
        if len(entered) == 2:
            both.set()
        await both.wait()
        seen[final_report_id] = env.dispatcher.create(user_id=body.user_id)
        payload = {'userId': body.user_id, 'sessionId': body.session_id, 'finalReportId': final_report_id}
        return SimpleNamespace(evaluate_payload=payload, followup_payloads=[])

    env.pipeline.execute_and_callback = run
    await asyncio.gather(env.agent.execute(job(env, 'old', old=True)), env.agent.execute(job(env, 'new')))
    assert seen == {'old': 'report-fixed-documents.v1', 'new': 'report-formal-documents.v1'}
    assert len(env.old.calls) == len(env.new.calls) == 1
    with pytest.raises(ValueError, match='bind'):
        env.dispatcher.create()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['dependency', 'consumer', 'factory'])
async def test_wrong_full_manifest_or_drift_fails_before_any_business_call(change):
    env = setup()
    accepted = job(env, 'old', old=True)
    if change == 'dependency':
        accepted['executionManifest']['dependencies']['changed'] = True
    elif change == 'consumer':
        accepted['executionManifest']['documentConsumer']['selection']['version'] = 'other'
    else:
        env.old.manifest['selection']['version'] = 'other'
    with pytest.raises(ReportExecutionVersionError):
        await env.agent.execute(accepted)
    assert not env.old.calls and not env.new.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
async def test_manifest_binding_resets_on_business_failure_or_cancellation(error):
    env = setup()

    async def run(*args, **kwargs):
        assert env.dispatcher.create() == 'report-fixed-documents.v1'
        raise error()

    env.pipeline.execute_and_callback = run
    with pytest.raises(error):
        await env.agent.execute(job(env, 'old', old=True))
    with pytest.raises(ValueError, match='bind'):
        env.dispatcher.create()
