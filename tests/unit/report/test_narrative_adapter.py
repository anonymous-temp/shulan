"""报告叙事适配器的契约测试。

叙事生成已改走 AgentHub -> report.narrative Graph。超时、重试、围栏清理与
严格 JSON 对象解析留在适配器层，这里用假 Hub 固定这些行为，不连模型。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from adapters.report.agent_narrative import AgentNarrativeClient
from agents.graphs.report import REPORT_NARRATIVE_CAPABILITY
from modules.report.agent import REPORT_NARRATIVE_SELECTION
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.narrative import NarrativeError


class _FakeHub:
    """按顺序回放模型输出，并记录每次调用的能力与请求标识。"""

    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = 0
        self.capabilities = []
        self.request_ids = []
        self.run_ids = []

    async def invoke(self, capability, request, context, *, selection):
        assert selection == REPORT_NARRATIVE_SELECTION
        self.capabilities.append(capability)
        self.request_ids.append(context.request_id)
        self.run_ids.append(context.run_id)
        content = self._contents[self.calls]
        self.calls += 1
        return SimpleNamespace(data={'content': content})


def _client(hub, *, attempts=2):
    return AgentNarrativeClient(
        hub=hub,
        semaphore=asyncio.Semaphore(1),
        policy=ReportFallbackPolicy(
            llm_attempts=attempts,
            retry_backoff_seconds=(),
        ),
        user_id='user-1',
        session_id='session-1',
        execution_id='exec-1',
    )


@pytest.mark.asyncio
async def test_narrative_adapter_retries_invalid_json():
    hub = _FakeHub(['not-json', '{"summary":"ok"}'])

    result = await _client(hub).generate({'task': 'test'})

    assert result == {'summary': 'ok'}
    assert hub.calls == 2
    assert hub.capabilities == [REPORT_NARRATIVE_CAPABILITY] * 2


@pytest.mark.asyncio
async def test_narrative_adapter_strips_code_fence():
    hub = _FakeHub(['```json\n{"summary":"ok"}\n```'])

    result = await _client(hub, attempts=1).generate({'task': 'test'})

    assert result == {'summary': 'ok'}
    assert hub.calls == 1


@pytest.mark.asyncio
async def test_narrative_adapter_rejects_non_object_response():
    # 合法 JSON 但不是对象：耗尽重试后按业务异常抛出，不返回残缺结构。
    hub = _FakeHub(['[1, 2]', '"text"'])

    with pytest.raises(NarrativeError):
        await _client(hub).generate({'task': 'test'})

    assert hub.calls == 2


@pytest.mark.asyncio
async def test_narrative_adapter_keeps_request_id_across_retries():
    # 同一次 generate 的重试共用 request_id，每次尝试有独立 run_id。
    hub = _FakeHub(['not-json', '{"summary":"ok"}'])

    await _client(hub).generate({'task': 'test'})

    assert len(set(hub.request_ids)) == 1
    assert len(set(hub.run_ids)) == 2


@pytest.mark.asyncio
async def test_narrative_adapter_rejects_empty_payload():
    hub = _FakeHub([])

    with pytest.raises(NarrativeError):
        await _client(hub).generate({})

    assert hub.calls == 0
