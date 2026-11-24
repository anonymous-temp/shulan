"""厂商无关的 LLM 请求、结果和上下文策略类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any

from .reasoning import validate_reasoning_effort

if TYPE_CHECKING:
    from .profiles import ModelProfile


class ContextPolicy(StrEnum):
    """当前已经确认并需要实现的模型上下文策略。"""

    STATELESS = 'stateless'
    AION_FULL = 'aion_full'


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Graph 向 LLMGateway 发起的一次请求。"""

    profile: 'ModelProfile'
    content: str
    supplemental_context: str | None = None
    conversation_key: str | None = None
    thinking: bool | None = None
    web_search: bool | None = None
    reasoning_effort: str | None = None
    search_query: str | None = None

    def __post_init__(self) -> None:
        from .profiles import ModelProfile

        if not isinstance(self.profile, ModelProfile):
            raise TypeError('profile must be a ModelProfile')
        if not isinstance(self.content, str):
            raise TypeError('content must be a string')
        if not self.content.strip():
            raise ValueError('content cannot be empty')
        if self.supplemental_context is not None:
            if not isinstance(self.supplemental_context, str):
                raise TypeError('supplemental_context must be a string or None')
            if not self.supplemental_context.strip():
                raise ValueError('supplemental_context cannot be empty')
        if self.thinking is not None and not isinstance(self.thinking, bool):
            raise TypeError('thinking must be a boolean or None')
        if self.reasoning_effort is not None:
            validate_reasoning_effort(self.reasoning_effort)
        if self.web_search is not None and not isinstance(self.web_search, bool):
            raise TypeError('web_search must be a boolean or None')
        if self.search_query is not None:
            _require_text('search_query', self.search_query)
            if not self.effective_web_search:
                raise ValueError('search_query requires web_search=True')

        if self.profile.context_policy is ContextPolicy.STATELESS:
            if self.conversation_key is not None:
                raise ValueError(
                    'Stateless model requests cannot set conversation_key',
                )
        elif self.profile.context_policy is ContextPolicy.AION_FULL:
            _require_optional_key(self.conversation_key)

    @property
    def effective_thinking(self) -> bool:
        """请求级设置优先，否则使用 Profile 默认值。"""
        if self.thinking is not None:
            return self.thinking
        return self.profile.thinking

    @property
    def effective_reasoning_effort(self) -> str:
        """Request overrides legacy model_kwargs, then the profile default."""
        if self.reasoning_effort is not None:
            return self.reasoning_effort
        return validate_reasoning_effort(
            self.profile.model_kwargs.get('reasoning_effort', self.profile.reasoning_effort)
        )

    @property
    def effective_web_search(self) -> bool:
        """请求级联网设置优先，否则使用默认关闭的 Profile 设置。"""
        if self.web_search is not None:
            return self.web_search
        return self.profile.web_search

    @property
    def effective_content(self) -> str:
        """返回发给模型的本轮内容。

        ``supplemental_context`` 只影响本次模型请求；有状态
        Gateway 持久化历史时仍应保存原始 ``content``。
        """
        if self.supplemental_context is None:
            return self.content
        return (
            f'{self.content}\n\n'
            '--- 本轮补充参考资料开始 ---\n'
            f'{self.supplemental_context.strip()}\n'
            '--- 本轮补充参考资料结束 ---'
        )


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """跨厂商规范化的 Token 使用量和原始明细。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonnegative_int('prompt_tokens', self.prompt_tokens)
        _require_nonnegative_int('completion_tokens', self.completion_tokens)
        _require_nonnegative_int('total_tokens', self.total_tokens)
        if not isinstance(self.raw, Mapping):
            raise TypeError('raw must be a mapping')

    def to_dict(self) -> dict[str, Any]:
        return {
            'promptTokens': self.prompt_tokens,
            'completionTokens': self.completion_tokens,
            'totalTokens': self.total_tokens,
            'raw': dict(self.raw),
        }


@dataclass(frozen=True, slots=True)
class LLMResult:
    """一次完整模型调用的规范化结果。"""

    content: str
    reasoning_content: str | None = None
    usage: LLMUsage | None = None
    provider: str | None = None
    model: str | None = None
    transport_retries: int | None = None
    # Provider-returned retrieval materials, never extracted from model prose.
    # Empty on older AION versions and when the provider exposes no sources.
    search_sources: tuple[Mapping[str, Any], ...] = ()
    search_trace: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError('content must be a string')
        if self.reasoning_content is not None:
            if not isinstance(self.reasoning_content, str):
                raise TypeError('reasoning_content must be a string or None')
        if self.usage is not None and not isinstance(self.usage, LLMUsage):
            raise TypeError('usage must be LLMUsage or None')
        if self.provider is not None:
            _require_text('provider', self.provider)
        if self.model is not None:
            _require_text('model', self.model)
        _require_nonnegative_int('transport_retries', self.transport_retries)
        _require_search_metadata(self.search_sources, self.search_trace)


@dataclass(frozen=True, slots=True)
class LLMChunk:
    """一次流式模型调用中的规范化增量。"""

    content: str | None = None
    reasoning_content: str | None = None
    usage: LLMUsage | None = None
    search_sources: tuple[Mapping[str, Any], ...] = ()
    search_trace: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError('content must be a string or None')
        if self.reasoning_content is not None:
            if not isinstance(self.reasoning_content, str):
                raise TypeError('reasoning_content must be a string or None')
        if self.usage is not None and not isinstance(self.usage, LLMUsage):
            raise TypeError('usage must be LLMUsage or None')

        _require_search_metadata(self.search_sources, self.search_trace)


def _require_search_metadata(sources: tuple[Mapping[str, Any], ...], trace: Mapping[str, Any] | None) -> None:
    if not isinstance(sources, tuple) or any(not isinstance(source, Mapping) for source in sources):
        raise TypeError('search_sources must be a tuple of mappings')
    if trace is not None and not isinstance(trace, Mapping):
        raise TypeError('search_trace must be a mapping or None')


def _require_optional_key(value: str | None) -> None:
    if value is None:
        raise ValueError('AION_FULL model requests require conversation_key')
    _require_text('conversation_key', value)


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if not value.strip():
        raise ValueError(f'{name} cannot be empty')


def _require_nonnegative_int(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{name} must be an integer or None')
    if value < 0:
        raise ValueError(f'{name} cannot be negative')
