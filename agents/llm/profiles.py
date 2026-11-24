"""集中描述各 AI 能力所使用的模型调用配置。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ..errors import ModelProfileAlreadyRegisteredError
from ..errors import ModelProfileNotFoundError
from .reasoning import DEFAULT_REASONING_EFFORT
from .reasoning import validate_reasoning_effort
from .reasoning import validate_reasoning_kwargs
from .types import ContextPolicy


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """一个命名的、厂商无关的模型调用配置。"""

    name: str
    context_policy: ContextPolicy
    prompt_name: str | None = None
    model: str | None = None
    thinking: bool = False
    prompt_variables: Mapping[str, Any] = field(default_factory=dict)
    model_kwargs: Mapping[str, Any] = field(default_factory=dict)
    web_search: bool = False
    capture_transport_metrics: bool = False
    reasoning_effort: str = DEFAULT_REASONING_EFFORT

    def __post_init__(self) -> None:
        object.__setattr__(self, 'name', _normalize_text('name', self.name))
        if not isinstance(self.context_policy, ContextPolicy):
            raise TypeError('context_policy must be a ContextPolicy')
        if self.prompt_name is not None:
            object.__setattr__(
                self,
                'prompt_name',
                _normalize_text('prompt_name', self.prompt_name),
            )
        if self.model is not None:
            object.__setattr__(
                self,
                'model',
                _normalize_text('model', self.model),
            )
        if not isinstance(self.thinking, bool):
            raise TypeError('thinking must be a boolean')
        validate_reasoning_effort(self.reasoning_effort)
        if not isinstance(self.web_search, bool):
            raise TypeError('web_search must be a boolean')
        if not isinstance(self.capture_transport_metrics, bool):
            raise TypeError('capture_transport_metrics must be a boolean')
        object.__setattr__(
            self,
            'prompt_variables',
            _copy_mapping('prompt_variables', self.prompt_variables),
        )
        object.__setattr__(
            self,
            'model_kwargs',
            _copy_mapping('model_kwargs', self.model_kwargs),
        )
        reserved_kwargs = {
            'content',
            'model',
            'supplemental_context',
            'thinking',
            'usage',
        }.intersection(self.model_kwargs)
        if reserved_kwargs:
            names = ', '.join(sorted(reserved_kwargs))
            raise ValueError(f'model_kwargs contains reserved keys: {names}')
        validate_web_search_kwargs(self.model_kwargs)
        validate_reasoning_kwargs(self.model_kwargs)


def validate_web_search_kwargs(model_kwargs: Mapping[str, Any]) -> None:
    """联网开关只能由显式字段控制，避免透传参数覆盖请求级关闭。"""
    conflicts: set[str] = set()
    for prefix, options in (
        ('', model_kwargs),
        ('extra_body.', model_kwargs.get('extra_body')),
    ):
        if not isinstance(options, Mapping):
            continue
        for key in {'web_search', 'search', 'enable_search', 'search_query'}.intersection(options):
            conflicts.add(f'{prefix}{key}')
        tools = options.get('tools')
        if isinstance(tools, (list, tuple)):
            for tool in tools:
                if not isinstance(tool, Mapping):
                    continue
                tool_type = tool.get('type')
                if isinstance(tool_type, str) and (tool_type == 'web_search' or tool_type.startswith('web_search_')):
                    conflicts.add(f'{prefix}tools')
    if conflicts:
        names = ', '.join(sorted(conflicts))
        raise ValueError(
            f'Configure web_search on ModelProfile or LLMRequest, not in model_kwargs: {names}',
        )


class ModelProfileRegistry:
    """在启动阶段注册 ModelProfile，运行阶段只读查询。"""

    def __init__(self, *, fallback: 'ModelProfileRegistry | None' = None) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        self._fallback = fallback

    def register(self, profile: ModelProfile) -> ModelProfile:
        if not isinstance(profile, ModelProfile):
            raise TypeError('profile must be a ModelProfile')
        if profile.name in self.names():
            raise ModelProfileAlreadyRegisteredError(
                f'Model profile already registered: {profile.name}',
            )
        self._profiles[profile.name] = profile
        return profile

    def get(self, name: str) -> ModelProfile:
        normalized_name = _normalize_text('name', name)
        try:
            return self._profiles[normalized_name]
        except KeyError as error:
            if self._fallback is not None:
                return self._fallback.get(normalized_name)
            raise ModelProfileNotFoundError(
                f'Unknown model profile: {normalized_name}',
            ) from error

    def names(self) -> tuple[str, ...]:
        """按名称返回稳定排序的已注册 Profile。"""
        inherited = () if self._fallback is None else self._fallback.names()
        return tuple(sorted(set(self._profiles) | set(inherited)))

    def merged(self, staged: 'ModelProfileRegistry') -> 'ModelProfileRegistry':
        result = ModelProfileRegistry()
        result._profiles = dict(self._profiles)
        for profile in staged._profiles.values():
            result.register(profile)
        return result

    def publish(self, validated: 'ModelProfileRegistry') -> None:
        self._profiles = dict(validated._profiles)


def _normalize_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized


def _copy_mapping(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f'{name} must be a mapping')
    return dict(value)
