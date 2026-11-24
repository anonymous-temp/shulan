import asyncio

import pytest

from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import log_context
from utils.log.context import task_log_context


@pytest.mark.asyncio
async def test_concurrent_requests_child_tasks_threads_and_restoration():
    async def request(user):
        with bind_log_context(LogContext(user_id=user, request_uuid=f'request-{user}')):
            await asyncio.sleep(0)
            async def child():
                await asyncio.sleep(0)
                return log_context()
            child_context, thread_context = await asyncio.gather(
                asyncio.create_task(child()), asyncio.to_thread(log_context),
            )
            assert child_context == thread_context == log_context()
            return child_context.user_id
    assert await asyncio.gather(request('a'), request('b')) == ['a', 'b']
    assert log_context() == LogContext()


@pytest.mark.asyncio
async def test_scanner_clears_triggering_request_and_binds_each_job():
    seen = []
    @task_log_context(job='job')
    async def process(job):
        await asyncio.sleep(0)
        seen.append(log_context())
        if job['userId'] == 'b':
            raise ValueError('job failure')
    @task_log_context(clear=True)
    async def scan():
        assert log_context() == LogContext()
        await asyncio.gather(
            process({'userId': 'a', 'sessionId': 'sa', '_id': 'j-a'}),
            process({'userId': 'b', 'sessionId': 'sb', '_id': 'j-b'}),
            return_exceptions=True,
        )
        assert log_context() == LogContext()
    original = LogContext(user_id='trigger', request_uuid='request-trigger')
    with bind_log_context(original):
        await scan()
        assert log_context() == original
    assert {item.user_id for item in seen} == {'a', 'b'}
    assert all(item.request_uuid is None for item in seen)
    assert {item.task_id for item in seen} == {'j-a', 'j-b'}


@pytest.mark.asyncio
async def test_same_user_child_without_session_argument_keeps_request_identity():
    @task_log_context(task='delivery')
    async def deliver(user_id):
        return log_context()
    with bind_log_context(user_id='u', session_id='s', request_uuid='request'):
        assert await deliver('u') == LogContext(
            user_id='u', session_id='s', request_uuid='request', task_id='delivery',
        )
        assert await deliver('other') == LogContext(user_id='other', task_id='delivery')
