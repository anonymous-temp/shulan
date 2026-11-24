"""LangGraph 节点依赖的最小 LLM Gateway 协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from typing import runtime_checkable

from .types import LLMChunk
from .types import LLMRequest
from .types import LLMResult


@runtime_checkable
class LLMGateway(Protocol):
    """统一完整和流式模型调用，不暴露具体厂商 SDK。"""

    async def complete(self, request: LLMRequest) -> LLMResult:
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        ...
