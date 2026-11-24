"""Agent Core 的领域异常。"""

from collections.abc import Mapping
from typing import Any


class AgentError(Exception):
    """所有 Agent Core 异常的基类。"""


class AgentConfigurationError(AgentError):
    """Agent 组件配置无效。"""


class AgentCapabilityNotFoundError(AgentConfigurationError):
    """请求了尚未注册的 Agent 能力。"""


class AgentCapabilityAlreadyRegisteredError(AgentConfigurationError):
    """重复注册了同名 Agent 能力。"""


class AgentProtocolError(AgentError):
    """Graph 输入或输出不符合 AgentHub 协议。"""


class ModelProfileNotFoundError(AgentConfigurationError):
    """请求了尚未注册的模型配置。"""


class ModelProfileAlreadyRegisteredError(AgentConfigurationError):
    """重复注册了同名模型配置。"""


class AionGatewayError(AgentError):
    """AION Client 创建、调用或 Context 转换失败。"""

    def __init__(
        self,
        message: str,
        *,
        search_trace: Mapping[str, Any] | None = None,
        search_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.search_trace = search_trace
        self.search_reason = search_reason


class LLMCapabilityNotSupportedError(AgentConfigurationError):
    """当前模型接入层未提供请求显式开启的能力。"""


class AionResponseError(AionGatewayError):
    """AION 返回结果不符合 LLMGateway 契约。"""


class LLMContextConflictError(AgentError):
    """保存模型上下文时发现 revision 已被其他运行更新。"""


class LLMContextStorageError(AgentError):
    """模型上下文无法从持久化存储读取或写入。"""


class AgentLeaseStorageError(AgentError):
    """Agent 会话 lease 无法从分布式存储获取或变更。"""


class AgentRuntimeClosedError(AgentError):
    """尝试使用已经关闭的 Agent 生产运行时。"""
