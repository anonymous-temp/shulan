"""从已完成建档回答中提取候选健康事实的无状态 LangGraph。"""

from __future__ import annotations

from typing import Any
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from ...errors import AgentConfigurationError
from ...llm.profiles import ModelProfile
from ...llm.protocol import LLMGateway
from ...llm.types import ContextPolicy
from ...llm.types import LLMRequest
from ...runtime.context import AgentRunContext
from .profile import PROFILE_RECORD_EXTRACTION_PROFILE

PROFILE_RECORD_EXTRACTION_CAPABILITY = PROFILE_RECORD_EXTRACTION_PROFILE
PROFILE_RECORD_EXTRACTION_GRAPH_VERSION = 'profile-record-extraction-v4'


class ProfileRecordExtractionState(TypedDict, total=False):
    """来源正文只存在于单次无状态调用，不写入 checkpoint。"""

    source_content: str
    request_id: str
    run_id: str
    content: str
    usage: dict[str, Any]
    provider: str | None
    model: str | None
    status: str


def build_profile_record_extraction_graph(
        *,
        gateway: LLMGateway,
        profile: ModelProfile,
) -> StateGraph:
    if not isinstance(gateway, LLMGateway):
        raise TypeError('gateway must implement LLMGateway')
    if not isinstance(profile, ModelProfile):
        raise TypeError('profile must be a ModelProfile')
    if profile.name != PROFILE_RECORD_EXTRACTION_PROFILE:
        raise AgentConfigurationError(
            'record candidate graph requires profile '
            f'{PROFILE_RECORD_EXTRACTION_PROFILE!r}',
        )
    if profile.context_policy is not ContextPolicy.STATELESS:
        raise AgentConfigurationError(
            'record candidate graph requires STATELESS context policy',
        )

    async def call_model(
            state: ProfileRecordExtractionState,
            runtime: Runtime[AgentRunContext],
    ) -> ProfileRecordExtractionState:
        source_content = state.get('source_content')
        if not isinstance(source_content, str) or not source_content.strip():
            raise ValueError(
                'record candidate graph requires non-empty source_content',
            )
        result = await gateway.complete(LLMRequest(
            profile=profile,
            content=source_content,
        ))
        context = runtime.context
        return {
            'request_id': context.request_id,
            'run_id': context.run_id,
            'content': result.content,
            # reasoning_content 不进入 Graph 输出或任何画像来源数据。
            'usage': {} if result.usage is None else result.usage.to_dict(),
            'provider': result.provider,
            'model': result.model,
            'status': 'completed',
        }

    builder = StateGraph(
        ProfileRecordExtractionState,
        context_schema=AgentRunContext,
    )
    builder.add_node('extract_candidates', call_model)
    builder.add_edge(START, 'extract_candidates')
    builder.add_edge('extract_candidates', END)
    return builder
