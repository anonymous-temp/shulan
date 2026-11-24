"""传入 LangGraph 节点的单次 Agent 运行上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """不进入 Graph State 的请求身份和运行标识。"""

    user_id: str
    session_id: str
    request_id: str
    run_id: str
    subject_id: str | None = None
    health_profile_context: str | None = field(default=None, repr=False)
    session_health_context: str | None = field(default=None, repr=False)
    # 报告内容与画像必须分区：不复用 health_profile_context 混入报告。
    report_context: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_identifier('user_id', self.user_id)
        _require_identifier('session_id', self.session_id)
        _require_identifier('request_id', self.request_id)
        _require_identifier('run_id', self.run_id)
        if self.subject_id is not None:
            _require_identifier('subject_id', self.subject_id)
        if self.health_profile_context is not None:
            _require_text('health_profile_context', self.health_profile_context)
        if self.session_health_context is not None:
            _require_text('session_health_context', self.session_health_context)
        if self.report_context is not None:
            _require_text('report_context', self.report_context)

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        session_id: str,
        request_id: str | None = None,
        run_id: str | None = None,
        subject_id: str | None = None,
        health_profile_context: str | None = None,
        session_health_context: str | None = None,
        report_context: str | None = None,
    ) -> 'AgentRunContext':
        """创建运行上下文，并补全内部 request/run 标识。"""
        return cls(
            user_id=user_id,
            session_id=session_id,
            request_id=request_id or str(uuid4()),
            run_id=run_id or str(uuid4()),
            subject_id=subject_id,
            health_profile_context=health_profile_context,
            session_health_context=session_health_context,
            report_context=report_context,
        )


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if not value.strip():
        raise ValueError(f'{name} cannot be empty')


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string or None')
    if not value.strip():
        raise ValueError(f'{name} cannot be empty')
