"""Coalesce adjacent SSE text deltas without changing business generation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from config.env import SSE_TEXT_BATCH_CHARS as TEXT_BATCH_CHARS
from config.env import SSE_TEXT_BATCH_SECONDS as TEXT_BATCH_SECONDS


def _text_key(payload: dict) -> dict | None:
    data = payload.get('data')
    if not isinstance(data, dict) or data.get('type') not in ('content', 'reasoning'):
        return None
    if not isinstance(data.get('content'), str) or not data['content']:
        return None
    # Only text and timing may change within one batch. Unknown business fields
    # are part of the key, so merging cannot silently discard their changes.
    return {
        **{key: value for key, value in payload.items() if key not in ('data', 'timestamp', 'elapsed_ms')},
        'data': {key: value for key, value in data.items() if key != 'content'},
    }


async def send_batched_sse(
    source: AsyncIterable[dict],
    send: Callable[[dict], Awaitable[Any]],
    *,
    disconnected: Callable[[], bool],
    max_chars: int = TEXT_BATCH_CHARS,
    max_delay: float = TEXT_BATCH_SECONDS,
) -> None:
    """Send first text immediately, then batch on size OR a fixed deadline.

    There is one upstream read in flight and one serial writer, never an
    unbounded producer queue. Timeout does not cancel the upstream read.
    The latest input envelope supplies timing for a merged event. A single
    oversized input is forwarded whole (the character limit is a trigger).
    After disconnect, keep consuming via send's existing skipped-event logging
    path so the service can finish persisting the session.
    """
    if max_chars <= 0 or max_delay <= 0:
        raise ValueError('SSE batch thresholds must be positive')
    iterator = aiter(source)
    loop = asyncio.get_running_loop()
    pending = None
    read_requested = asyncio.Event()
    fragments: list[str] = []
    chars = 0
    envelope = None
    batch_key = None
    deadline = None
    seen_types: set[str] = set()

    async def read_source() -> None:
        # Keep the iterator in one task: provider streams may retain context
        # variables / task-scoped resources across yields. Demand-driven reads
        # also avoid producing an unbounded queue while the socket is slow.
        try:
            while True:
                await read_requested.wait()
                read_requested.clear()
                try:
                    payload = await anext(iterator)
                except (Exception, asyncio.CancelledError) as error:
                    pending.set_result((None, error))
                    return
                pending.set_result((payload, None))
        finally:
            close = getattr(iterator, 'aclose', None)
            if close is not None:
                await close()

    reader = asyncio.create_task(read_source())

    async def flush() -> None:
        nonlocal chars, envelope, batch_key, deadline
        if envelope is None:
            return
        payload = {**envelope, 'data': {**envelope['data'], 'content': ''.join(fragments)}}
        fragments.clear()
        chars, envelope, batch_key, deadline = 0, None, None, None
        await send(payload)

    try:
        while True:
            if pending is None:
                pending = loop.create_future()
                read_requested.set()
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await flush()
                    continue
                done, _ = await asyncio.wait((pending,), timeout=remaining)
                if not done:
                    await flush()
                    continue
            else:
                # wait() leaves ownership of cancellation/cleanup to finally.
                await asyncio.wait((pending,))
            payload, error = pending.result()
            pending = None
            if isinstance(error, StopAsyncIteration):
                await flush()
                break
            if error is not None:
                # Handler will emit the error after all valid buffered text.
                await flush()
                raise error

            key = _text_key(payload)
            if disconnected() or key is None:
                await flush()
                await send(payload)
                continue
            event_type = payload['data']['type']
            if event_type not in seen_types:
                await flush()
                seen_types.add(event_type)
                await send(payload)
                continue
            if batch_key is not None and batch_key != key:
                await flush()
            if envelope is None:
                deadline = loop.time() + max_delay
                batch_key = key
            envelope = {**payload, 'data': dict(payload['data'])}
            fragments.append(payload['data']['content'])
            chars += len(payload['data']['content'])
            if chars >= max_chars:
                await flush()
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
