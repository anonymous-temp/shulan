"""不会访问网络的确定性 LLMGateway 测试替身。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Sequence

from agents.llm.types import LLMChunk
from agents.llm.types import LLMRequest
from agents.llm.types import LLMResult


class FakeLLMGateway:
    """记录请求并返回调用方预置的完整或流式结果。"""

    def __init__(
            self,
            *,
            complete_result: LLMResult | None = None,
            complete_results: Sequence[LLMResult] = (),
            stream_chunks: Sequence[LLMChunk] = (),
    ) -> None:
        self.complete_result = complete_result or LLMResult(content='fake reply')
        self.complete_results = list(complete_results)
        self.stream_chunks = tuple(stream_chunks)
        self.complete_requests: list[LLMRequest] = []
        self.stream_requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        if not isinstance(request, LLMRequest):
            raise TypeError('request must be an LLMRequest')
        self.complete_requests.append(request)
        if self.complete_results:
            return self.complete_results.pop(0)
        return self.complete_result

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        if not isinstance(request, LLMRequest):
            raise TypeError('request must be an LLMRequest')
        self.stream_requests.append(request)
        for chunk in self.stream_chunks:
            yield chunk
