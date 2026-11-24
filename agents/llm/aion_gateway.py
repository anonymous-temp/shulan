"""使用 AION 实现 Shulan LLMGateway 协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import is_dataclass
from dataclasses import replace
from inspect import Parameter
from inspect import signature
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

import aion
import httpx
from aion.prompts import prompt_manager
from openai import AsyncOpenAI

from ..errors import AgentConfigurationError
from ..errors import AionGatewayError
from ..errors import AionResponseError
from ..errors import LLMCapabilityNotSupportedError
from ..persistence.llm_context import LLMContextRepository
from .profiles import validate_web_search_kwargs
from .reasoning import validate_reasoning_kwargs
from .types import ContextPolicy
from .types import LLMChunk
from .types import LLMRequest
from .types import LLMResult
from .types import LLMUsage

if TYPE_CHECKING:
    from .profiles import ModelProfile


class AionChatClient(Protocol):
    """AionLLMGateway 依赖的最小 AION Client 协议。"""

    provider: str | None
    model: str

    async def complete_async(self, content: str, **kwargs: Any) -> Any: ...

    def stream_async(self, content: str, **kwargs: Any) -> AsyncIterator[Any]: ...

    def import_context(self, data: Mapping[str, Any]) -> None: ...

    def export_context(self) -> dict[str, Any]: ...


AionClientFactory = Callable[..., AionChatClient]
PromptRenderer = Callable[..., str]
_UNRENDERED = object()


@dataclass(slots=True)
class _ClientSession:
    client: AionChatClient
    context_key: str | None = None
    context_revision: int = 0
    baseline_context: dict[str, Any] | None = None


@dataclass
class _TransportCounter:
    """Count model SDK attempts, excluding the separate retrieval requests."""

    http_client: httpx.AsyncClient
    requests: int = 0

    @property
    def retries(self):
        return max(0, self.requests - 1)

    async def on_request(self, request):
        if request.method == 'POST' and request.url.path.rstrip('/').endswith(
            ('/chat/completions', '/responses', '/messages')
        ):
            self.requests += 1

    @classmethod
    def attach(cls, client):
        sdk = getattr(client, 'async_client', None)
        # This optional observer is guarded against AION/SDK changes. Never report
        # an invented zero for clients whose transport is not observable.
        http_client = getattr(sdk, '_client', None)
        if not isinstance(sdk, AsyncOpenAI) or not isinstance(http_client, httpx.AsyncClient):
            return None
        observer = cls(http_client)
        http_client.event_hooks['request'].append(observer.on_request)
        return observer

    def detach(self):
        self.http_client.event_hooks['request'].remove(self.on_request)


class AionLLMGateway:
    """把 Shulan 的模型协议适配到统一 AION Chat Client。"""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        default_model: str | None = None,
        context_repository: LLMContextRepository | None = None,
        client_factory: AionClientFactory | None = None,
        prompt_renderer: PromptRenderer | None = None,
    ) -> None:
        self._provider = _normalize_required_text('provider', provider)
        self._api_key = _normalize_required_text('api_key', api_key)
        self._default_model = _normalize_optional_text(
            'default_model',
            default_model,
        )
        if context_repository is not None:
            if not isinstance(context_repository, LLMContextRepository):
                raise TypeError(
                    'context_repository must implement LLMContextRepository',
                )
        self._context_repository = context_repository
        self._client_factory = client_factory or aion.create_chat_client
        self._prompt_renderer = prompt_renderer or prompt_manager.render

    async def complete(self, request: LLMRequest) -> LLMResult:
        """通过 AION 执行完整模型调用。"""
        _require_request(request)
        session = await self._create_session(request)
        search_kwargs = _web_search_kwargs(
            session.client.complete_async,
            enabled=request.effective_web_search,
            search_query=request.search_query,
        )
        model_kwargs = _reasoning_kwargs(session.client.complete_async, request)
        transport = _TransportCounter.attach(session.client) if request.profile.capture_transport_metrics else None
        try:
            result = await session.client.complete_async(
                request.effective_content,
                thinking=request.effective_thinking,
                **model_kwargs,
                **search_kwargs,
            )
        except Exception as error:
            wrapped = _gateway_call_error('AION complete_async failed', error)
            wrapped.transport_retries = None if transport is None else transport.retries
            raise wrapped from error
        finally:
            if transport is not None:
                transport.detach()

        normalized = self._normalize_result(session.client, result)
        if transport is not None:
            normalized = replace(normalized, transport_retries=transport.retries)
        await self._persist_context_if_changed(
            session,
            persisted_user_content=(request.content if request.supplemental_context is not None else None),
        )
        return normalized

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        """通过 AION 执行流式模型调用，完整消费后才保存 Context。"""
        _require_request(request)
        session = await self._create_session(request)
        search_kwargs = _web_search_kwargs(
            session.client.stream_async,
            enabled=request.effective_web_search,
            search_query=request.search_query,
        )
        model_kwargs = _reasoning_kwargs(session.client.stream_async, request)
        try:
            async for chunk in session.client.stream_async(
                request.effective_content,
                thinking=request.effective_thinking,
                usage=True,
                **model_kwargs,
                **search_kwargs,
            ):
                yield self._normalize_chunk(chunk)
        except Exception as error:
            raise _gateway_call_error('AION stream_async failed', error) from error

        await self._persist_context_if_changed(
            session,
            persisted_user_content=(request.content if request.supplemental_context is not None else None),
        )

    def request_model(self, profile: 'ModelProfile') -> tuple[str, str | None]:
        return self._provider, profile.model or self._default_model

    @staticmethod
    def _stream_tools(request, session, *, tools, options, trace):
        """Private transport for the guarded tool adapter; SDK entry stays centralized."""
        return session.client.stream_async(
            request.effective_content,
            thinking=request.effective_thinking,
            usage=True,
            tool_functions=tools,
            tool_options=options,
            tool_trace=trace,
            **_reasoning_kwargs(session.client.stream_async, request),
        )

    async def _create_session(self, request: LLMRequest, *, rendered_prompt=_UNRENDERED) -> _ClientSession:
        context_enabled = request.profile.context_policy is ContextPolicy.AION_FULL
        if context_enabled and self._context_repository is None:
            raise AgentConfigurationError(
                'AION_FULL requires an LLMContextRepository',
            )

        provider, model = self.request_model(request.profile)
        system_content = self.render_prompt(request.profile) if rendered_prompt is _UNRENDERED else rendered_prompt
        client_kwargs: dict[str, Any] = {
            'api_key': self._api_key,
            'context': context_enabled,
            'system_content': system_content,
        }
        if model is not None:
            client_kwargs['model'] = model

        try:
            client = self._client_factory(provider, **client_kwargs)
        except Exception as error:
            raise AionGatewayError('Cannot create AION Chat Client') from error

        session = _ClientSession(client=client)
        if not context_enabled:
            return session

        context_key = self._context_key(request)
        repository = self._context_repository
        assert repository is not None
        snapshot = await repository.load(context_key)
        if snapshot is not None:
            try:
                # AION import_context 会替换整个 Context（包括 system）。
                # 部署中的 Prompt 是角色规则的权威来源，历史只恢复对话，
                # 避免老会话继续沿用旧角色；没有配置 Prompt 的能力保持原样。
                payload = deepcopy(dict(snapshot.payload))
                if system_content is not None:
                    payload['system'] = {'role': 'system', 'content': system_content}
                client.import_context(payload)
            except Exception as error:
                raise AionGatewayError('Cannot import AION Context') from error
            session.context_revision = snapshot.revision

        try:
            baseline = client.export_context()
        except Exception as error:
            raise AionGatewayError('Cannot export initial AION Context') from error
        session.context_key = context_key
        session.baseline_context = baseline
        return session

    def render_prompt(self, profile: 'ModelProfile') -> str | None:
        if profile.prompt_name is None:
            return None
        try:
            content = self._prompt_renderer(
                profile.prompt_name,
                **dict(profile.prompt_variables),
            )
        except Exception as error:
            raise AionGatewayError(
                f'Cannot render model prompt: {profile.prompt_name}',
            ) from error
        if not isinstance(content, str):
            raise AionResponseError('Prompt renderer must return a string')
        return content

    @staticmethod
    def _context_key(request: LLMRequest) -> str:
        assert request.conversation_key is not None
        return f'{request.profile.name}:{request.conversation_key}'

    async def _persist_context_if_changed(
        self,
        session: _ClientSession,
        *,
        persisted_user_content: str | None = None,
    ) -> None:
        if session.context_key is None:
            return
        try:
            current_context = session.client.export_context()
        except Exception as error:
            raise AionGatewayError('Cannot export completed AION Context') from error
        if current_context == session.baseline_context:
            return
        if persisted_user_content is not None:
            current_context = _replace_latest_context_user_content(
                current_context,
                persisted_user_content,
            )

        repository = self._context_repository
        assert repository is not None
        await repository.save(
            session.context_key,
            current_context,
            expected_revision=session.context_revision,
        )

    def _normalize_result(
        self,
        client: AionChatClient,
        result: Any,
    ) -> LLMResult:
        content = getattr(result, 'content', None)
        if not isinstance(content, str):
            raise AionResponseError(
                'AION complete result must contain string content',
            )
        reasoning = getattr(result, 'reasoning_content', None)
        if reasoning is not None and not isinstance(reasoning, str):
            raise AionResponseError(
                'AION reasoning_content must be a string or None',
            )
        sources, trace = _normalize_search_metadata(result)
        return LLMResult(
            content=content,
            reasoning_content=reasoning,
            usage=_normalize_usage(getattr(result, 'usage', None)),
            provider=_client_optional_text(client, 'provider') or self._provider,
            model=_client_optional_text(client, 'model'),
            search_sources=sources,
            search_trace=trace,
        )

    @staticmethod
    def _normalize_chunk(chunk: Any) -> LLMChunk:
        content = getattr(chunk, 'content', None)
        reasoning = getattr(chunk, 'reasoning_content', None)
        if content is not None and not isinstance(content, str):
            raise AionResponseError(
                'AION stream content must be a string or None',
            )
        if reasoning is not None and not isinstance(reasoning, str):
            raise AionResponseError(
                'AION stream reasoning_content must be a string or None',
            )
        sources, trace = _normalize_search_metadata(chunk)
        return LLMChunk(
            content=content,
            reasoning_content=reasoning,
            usage=_normalize_usage(getattr(chunk, 'usage', None)),
            search_sources=sources,
            search_trace=trace,
        )


def _normalize_search_metadata(result: Any) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any] | None]:
    """Keep retrieval evidence separate from generated content in either response mode."""
    trace = _normalize_search_trace(getattr(result, 'search_trace', None))
    if trace is None:
        return _normalize_search_sources(getattr(result, 'search_sources', None)), None
    sources = tuple(
        {
            'id': str(source['source_id']),
            'title': source['title'],
            'url': source['url'],
            'text': source['excerpt'],
            'excerpt_origin': source['excerpt_origin'],
            'locator': {'source_id': source['source_id'], 'excerpt_origin': source['excerpt_origin']},
        }
        for source in trace['sources']
        if isinstance(source['excerpt'], str) and source['excerpt'].strip()
    )
    return sources, trace


def _search_record(value: Any, names: tuple[str, ...], *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {name: value.get(name) for name in names}
    if is_dataclass(value) and not isinstance(value, type):
        return {name: getattr(value, name, None) for name in names}
    raise AionResponseError(f'AION {label} must be a mapping or dataclass')


def _normalize_search_trace(value: Any) -> dict[str, Any] | None:
    """Copy only the public trace fields; never retain raw SDK requests or responses."""
    if value is None:
        return None
    trace = _search_record(value, ('query', 'sources', 'operations'), label='search_trace')
    if not isinstance(trace['query'], str):
        raise AionResponseError('AION search_trace.query must be a string')
    if not isinstance(trace['sources'], (list, tuple)) or not isinstance(trace['operations'], (list, tuple)):
        raise AionResponseError('AION search_trace sources and operations must be sequences')
    sources = []
    for value in trace['sources']:
        source = _search_record(
            value, ('source_id', 'title', 'url', 'excerpt', 'excerpt_origin'), label='search source'
        )
        source_id = source['source_id']
        if isinstance(source_id, bool) or not isinstance(source_id, (str, int)) or not str(source_id).strip():
            raise AionResponseError('AION search source requires a string or integer source_id')
        if not isinstance(source['title'], str) or not isinstance(source['url'], str):
            raise AionResponseError('AION search source title and url must be strings')
        if source['excerpt'] is not None and not isinstance(source['excerpt'], str):
            raise AionResponseError('AION search source excerpt must be a string or None')
        if source['excerpt_origin'] not in ('search', 'fetch', None):
            raise AionResponseError('AION search source has an invalid excerpt_origin')
        sources.append(source)
    operations = []
    for value in trace['operations']:
        operation = _search_record(
            value, ('kind', 'target', 'started_at', 'status', 'status_code', 'error'), label='search operation'
        )
        if operation['kind'] not in ('search', 'fetch') or operation['status'] not in ('success', 'empty', 'failed'):
            raise AionResponseError('AION search operation has an invalid kind or status')
        if not isinstance(operation['target'], str) or not isinstance(operation['started_at'], str):
            raise AionResponseError('AION search operation target and started_at must be strings')
        code = operation['status_code']
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise AionResponseError('AION search operation status_code must be an integer or None')
        error = operation['error']
        # AION records exception class names, not exception messages or response bodies.
        operation['error'] = error if isinstance(error, str) and error.isidentifier() else None
        operations.append(operation)
    return {'query': trace['query'], 'sources': sources, 'operations': operations}


def _gateway_call_error(message: str, error: Exception) -> AionGatewayError:
    try:
        trace = _normalize_search_trace(getattr(error, 'search_trace', None))
    except AionResponseError:
        # Malformed diagnostics must not replace the original provider failure.
        trace = None
    reason = getattr(error, 'reason', None)
    if reason not in ('request_failed', 'no_results', 'no_readable_evidence'):
        reason = None
    return AionGatewayError(message, search_trace=trace, search_reason=reason)


def _normalize_search_sources(sources: Any) -> tuple[Mapping[str, Any], ...]:
    """Preserve legacy provider material when no structured search_trace is returned.

    AION 0.6.0 DeepSeek traces take precedence in _normalize_search_metadata.
    Missing legacy material stays empty; generated citations are never evidence.
    """
    if sources is None:
        return ()
    if not isinstance(sources, (list, tuple)) or any(not isinstance(source, Mapping) for source in sources):
        raise AionResponseError('AION search_sources must be a sequence of mappings')
    return tuple(deepcopy(dict(source)) for source in sources)


def _normalize_usage(usage: Any) -> LLMUsage | None:
    if usage is None:
        return None
    if not isinstance(usage, Mapping):
        raise AionResponseError('AION usage must be a mapping or None')

    def token_count(name: str) -> int | None:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    return LLMUsage(
        prompt_tokens=token_count('prompt_tokens'),
        completion_tokens=token_count('completion_tokens'),
        total_tokens=token_count('total_tokens'),
        raw=dict(usage),
    )


def _client_optional_text(client: AionChatClient, name: str) -> str | None:
    return _normalize_optional_text(name, getattr(client, name, None))


def _replace_latest_context_user_content(
    payload: Mapping[str, Any],
    content: str,
) -> dict[str, Any]:
    """以原始用户内容替换 Context 中本轮的临时增强文本。"""
    normalized = deepcopy(dict(payload))
    turns = normalized.get('turns')
    if not isinstance(turns, list) or not turns:
        raise AionResponseError(
            'AION Context did not archive the completed supplemental turn',
        )
    latest = turns[-1]
    if not isinstance(latest, dict):
        raise AionResponseError('AION Context latest turn must be a mapping')
    user = latest.get('user')
    if not isinstance(user, dict) or user.get('role') != 'user':
        raise AionResponseError(
            'AION Context latest turn must contain a user message',
        )
    user['content'] = content
    return normalized


def _require_request(request: LLMRequest) -> None:
    if not isinstance(request, LLMRequest):
        raise TypeError('request must be an LLMRequest')
    # Profile 复制了映射，但映射本身仍可变；调用时重新校验，保证关闭不能被绕过。
    validate_web_search_kwargs(request.profile.model_kwargs)
    validate_reasoning_kwargs(request.profile.model_kwargs)


def _reasoning_kwargs(method: Callable[..., Any], request: LLMRequest) -> dict[str, Any]:
    """Only send effort for thinking calls with an explicit AION parameter."""
    options = dict(request.profile.model_kwargs)
    options.pop('reasoning_effort', None)
    if not request.effective_thinking:
        return options
    try:
        parameter = signature(method).parameters.get('reasoning_effort')
    except (TypeError, ValueError):
        parameter = None
    if parameter is None or parameter.kind not in (
        Parameter.POSITIONAL_OR_KEYWORD,
        Parameter.KEYWORD_ONLY,
    ):
        if (
            request.reasoning_effort is None
            and request.profile.reasoning_effort == 'low'
            and 'reasoning_effort' not in request.profile.model_kwargs
        ):
            # Other providers keep their existing thinking behavior unless the
            # caller explicitly requests effort control.
            return options
        raise LLMCapabilityNotSupportedError(
            'reasoning_effort requires an AION client with an explicit reasoning_effort parameter '
            'on the requested complete_async/stream_async method'
        )
    options['reasoning_effort'] = request.effective_reasoning_effort
    return options


def _web_search_kwargs(method: Callable[..., Any], *, enabled: bool, search_query: str | None = None) -> dict[str, Any]:
    """仅使用 AION 已声明的 search 参数，不把 **kwargs 当作能力支持。"""
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    parameter = parameters.get('search')
    if parameter is not None and parameter.kind in (
        Parameter.POSITIONAL_OR_KEYWORD,
        Parameter.KEYWORD_ONLY,
    ):
        options: dict[str, Any] = {'search': enabled}
        if search_query is not None:
            if not enabled:
                raise ValueError('search_query requires web_search=True')
            query_parameter = parameters.get('search_query')
            if query_parameter is None or query_parameter.kind not in (
                Parameter.POSITIONAL_OR_KEYWORD,
                Parameter.KEYWORD_ONLY,
            ):
                raise LLMCapabilityNotSupportedError(
                    'search_query requires an AION client with an explicit search_query parameter '
                    'on the requested complete_async/stream_async method'
                )
            options['search_query'] = search_query
        return options
    if enabled:
        raise LLMCapabilityNotSupportedError(
            'web_search=True requires an AION client with an explicit search parameter '
            'on the requested complete_async/stream_async method; '
            'upgrade AION with search support for the configured provider',
        )
    # 旧 Client 不识别 search；默认关闭时保持原调用参数不变。
    return {}


def _normalize_required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized


def _normalize_optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_required_text(name, value)
