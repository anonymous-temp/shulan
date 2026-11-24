"""Stable profile application errors, independent of storage and services."""


class HealthProfileValidationError(ValueError):
    """画像请求字段或领域组合不合法。"""


class HealthProfileNotFoundError(LookupError):
    """用户可访问范围内不存在指定画像资源。"""


class HealthProfileConflictError(RuntimeError):
    """幂等键重用、资源状态或乐观并发版本发生冲突。"""
