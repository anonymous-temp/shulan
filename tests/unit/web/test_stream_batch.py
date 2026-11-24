"""SSE batching preserves text, deadlines, ordering and producer ownership."""

import asyncio
from copy import deepcopy
from contextvars import ContextVar

import pytest

from web.stream_batch import send_batched_sse


def event(kind='content', text='', index=0, **fields):
    return {
        'code': 200, 'message': 'success', 'uuid': 'request-1',
        'timestamp': index, 'elapsed_ms': index,
        'data': {'type': kind, 'content': text, 'userId': 'u', 'sessionId': 's', **fields},
    }


async def sequence(events):
    for payload in events:
        yield payload


async def collect(events, **options):
    received = []

    async def send(payload):
        received.append(payload)

    await send_batched_sse(sequence(events), send, disconnected=lambda: False, **options)
    return received


@pytest.mark.asyncio
async def test_burst_preserves_unicode_and_reduces_event_count_with_latest_timing():
    text = '中文😀e\u0301👩\u200d💻\r\n' * 1000
    source = [event(text=char, index=index) for index, char in enumerate(text)]
    before = deepcopy(source)
    result = await collect(source, max_delay=60)
    assert ''.join(item['data']['content'] for item in result) == text
    assert len(result) == 1 + (len(text) - 1 + 511) // 512
    assert result[0] == source[0]
    assert result[-1]['timestamp'] == len(text) - 1
    assert result[-1]['elapsed_ms'] == len(text) - 1
    assert source == before


@pytest.mark.asyncio
async def test_first_text_of_each_type_and_business_boundaries_keep_order():
    source = [
        event('start'), event('reasoning', '首'), event('reasoning', '推'), event('reasoning', '理'),
        event('content', '首'), event('content', '正'), event('content', '文'),
        event('reasoning', '尾'), event('question', questionId='q', options=['是', '否']),
        {'data': {'type': 'finish'}},
    ]
    assert await collect(source) == [
        source[0], source[1], event('reasoning', '推理'), source[4], event('content', '正文'),
        source[7], source[8], source[9],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [
    event('error', error='模型失败'), {'type': 'error', 'error': '裸错误'},
    event(text=''), event(text=None), event('tool', toolId='t'),
])
async def test_non_text_events_flush_previous_text_and_pass_through(boundary):
    source = [event(text='首'), event(text='尾'), boundary]
    assert await collect(source) == source


@pytest.mark.asyncio
async def test_changed_metadata_is_not_discarded_or_merged_across_sessions():
    source = [event(text='首'), event(text='a'), event(text='b', sessionId='other'),
              event(text='c', sessionId='other', extra=True)]
    assert await collect(source) == source


@pytest.mark.asyncio
async def test_size_trigger_and_large_input_do_not_wait_for_time_or_split_text():
    source = [event(text='首'), event(text='ab'), event(text='cd'), event(text='😀' * 1000)]
    assert await collect(source, max_chars=4, max_delay=60) == [
        source[0], event(text='abcd'), source[-1],
    ]


@pytest.mark.asyncio
async def test_deadline_sends_while_upstream_is_paused_without_cancelling_it():
    release = asyncio.Event()
    flushed = asyncio.Event()
    closed = asyncio.Event()
    received = []

    async def source():
        try:
            yield event(text='首')
            yield event(text='已有缓存')
            await release.wait()
            yield event('finish')
        finally:
            closed.set()

    async def send(payload):
        received.append(payload)
        if payload['data']['content'] == '已有缓存':
            flushed.set()

    task = asyncio.create_task(send_batched_sse(source(), send, disconnected=lambda: False))
    try:
        await asyncio.wait_for(flushed.wait(), 1)
        assert not closed.is_set() and not task.done()
        assert [item['data']['content'] for item in received] == ['首', '已有缓存']
    finally:
        release.set()
        await task
    assert closed.is_set() and received[-1]['data']['type'] == 'finish'


@pytest.mark.asyncio
async def test_new_fragments_do_not_reset_deadline():
    flushed = asyncio.Event()
    received = []
    produced = 0

    async def source():
        nonlocal produced
        yield event(text='首')
        for _ in range(100):
            produced += 1
            yield event(text='x')
            await asyncio.sleep(0.01)

    async def send(payload):
        received.append(payload)
        if payload['data']['content'].startswith('x'):
            flushed.set()

    task = asyncio.create_task(send_batched_sse(source(), send, disconnected=lambda: False))
    try:
        await asyncio.wait_for(flushed.wait(), 0.5)
        assert produced < 100
    finally:
        await task
    assert ''.join(item['data']['content'] for item in received) == '首' + 'x' * 100


@pytest.mark.asyncio
async def test_generator_failure_flushes_valid_tail_before_propagating():
    received = []

    async def source():
        yield event(text='首')
        yield event(text='尾')
        raise RuntimeError('上游失败')

    async def send(payload):
        received.append(payload)

    with pytest.raises(RuntimeError, match='上游失败'):
        await send_batched_sse(source(), send, disconnected=lambda: False)
    assert [item['data']['content'] for item in received] == ['首', '尾']


@pytest.mark.asyncio
async def test_disconnect_keeps_consuming_for_persistence_without_batching():
    disconnected = False
    persisted = False
    received = []

    async def source():
        nonlocal disconnected, persisted
        yield event(text='首')
        yield event(text='尾')
        disconnected = True
        yield event(text='断开后1')
        yield event(text='断开后2')
        persisted = True

    async def send(payload):
        received.append(payload)

    await send_batched_sse(source(), send, disconnected=lambda: disconnected)
    assert persisted
    assert [item['data']['content'] for item in received] == ['首', '尾', '断开后1', '断开后2']


@pytest.mark.asyncio
async def test_cancellation_closes_in_flight_read_and_has_no_late_sends():
    reading = asyncio.Event()
    closed = asyncio.Event()
    received = []

    async def source():
        try:
            yield event(text='首')
            yield event(text='尾')
            reading.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def send(payload):
        received.append(payload)

    task = asyncio.create_task(send_batched_sse(source(), send, disconnected=lambda: False, max_delay=60))
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert received == [event(text='首')]


@pytest.mark.asyncio
async def test_slow_writer_has_no_unbounded_read_ahead_or_concurrent_sends():
    release = asyncio.Event()
    writing = asyncio.Event()
    produced = 0
    active = 0
    received = []

    async def source():
        nonlocal produced
        for _ in range(100):
            produced += 1
            yield event(text='x')

    async def send(payload):
        nonlocal active
        active += 1
        assert active == 1
        if not received:
            writing.set()
            await release.wait()
        received.append(payload)
        active -= 1

    task = asyncio.create_task(send_batched_sse(source(), send, disconnected=lambda: False))
    await writing.wait()
    try:
        await asyncio.sleep(0)
        assert produced == 1
    finally:
        release.set()
        await task
    assert ''.join(item['data']['content'] for item in received) == 'x' * 100


@pytest.mark.asyncio
async def test_upstream_context_and_task_are_stable_across_yields_and_cleanup():
    context = ContextVar('stream_test_context', default='outer')
    closed = False

    async def source():
        nonlocal closed
        owner = asyncio.current_task()
        token = context.set('provider')
        try:
            for char in '原始内容':
                assert context.get() == 'provider'
                assert asyncio.current_task() is owner
                yield event(text=char)
        finally:
            context.reset(token)
            closed = True

    result = []

    async def send(payload):
        assert context.get() == 'outer'
        result.append(payload)

    await send_batched_sse(source(), send, disconnected=lambda: False)
    assert closed
    assert ''.join(item['data']['content'] for item in result) == '原始内容'


@pytest.mark.asyncio
async def test_upstream_cancellation_propagates_without_hanging():
    async def source():
        yield event(text='首')
        raise asyncio.CancelledError()

    async def send(payload):
        pass

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(send_batched_sse(source(), send, disconnected=lambda: False), 1)
