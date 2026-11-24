"""健康画像专科重点、循证检索及综合解释的无状态 Graph。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from ...knowledge import KnowledgeGateway
from ...runtime.context import AgentRunContext
from .interpretation import ProfileInterpreter

PROFILE_EVIDENCE_CAPABILITY = 'profile.retrieve_evidence'
PROFILE_EVIDENCE_GRAPH_VERSION = 'profile-evidence-v2'


class ProfileEvidenceState(TypedDict, total=False):
    facts: list[dict[str, Any]]
    clinicalContext: dict[str, Any]
    profileVersion: int
    evidence: dict[str, Any]
    insights: list[dict[str, Any]]
    complete: bool
    status: str
    request_id: str
    run_id: str


def build_profile_evidence_graph(
    *,
    gateway: KnowledgeGateway,
    llm_gateway=None,
    planner_profile=None,
    synthesis_profile=None,
    timeout_seconds=60.0,
    cache=None,
) -> StateGraph:
    if not isinstance(gateway, KnowledgeGateway):
        raise TypeError('gateway must implement KnowledgeGateway')
    interpreter = ProfileInterpreter(
        gateway,
        llm_gateway=llm_gateway,
        planner_profile=planner_profile,
        synthesis_profile=synthesis_profile,
        timeout_seconds=timeout_seconds,
        cache=cache,
    )

    async def interpret(state: ProfileEvidenceState, runtime: Runtime[AgentRunContext]) -> ProfileEvidenceState:
        facts = _normalize_facts(state.get('facts'))
        context = runtime.context
        clinical_context = state.get('clinicalContext', {})
        result = await interpreter.run(
            facts,
            user_id=context.user_id,
            subject_id=context.subject_id,
            profile_version=state.get('profileVersion', 0),
            clinical_context={
                key: clinical_context[key] for key in ('age', 'gender', 'bmi') if key in clinical_context
            },
        )
        return {**result, 'status': 'completed', 'request_id': context.request_id, 'run_id': context.run_id}

    builder = StateGraph(ProfileEvidenceState, context_schema=AgentRunContext)
    builder.add_node('interpret_profile', interpret)
    builder.add_edge(START, 'interpret_profile')
    builder.add_edge('interpret_profile', END)
    return builder


def _normalize_facts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError('profile evidence facts must be a list')
    normalized = []
    seen = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError('profile evidence fact must be a mapping')
        if raw.get('status', 'active') != 'active':
            continue
        fact = {key: _required_text(key, raw.get(key)) for key in ('factId', 'category', 'displayName', 'factType')}
        if fact['factId'] in seen:
            raise ValueError('duplicate profile fact identity')
        seen.add(fact['factId'])
        for key in (
            'interpretation',
            'value',
            'referenceRange',
            'observedAt',
            'assertionLevel',
            'verificationStatus',
            'sourceTypes',
        ):
            if key in raw:
                fact[key] = raw[key]
        normalized.append(fact)
    return normalized


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value.strip()
