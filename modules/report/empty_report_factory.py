"""One typed fallback schema for model failures and durable recovery."""
from __future__ import annotations

from typing import Any

from modules.report.followup_defaults import default_followup_report


def empty_followup_report(
    report_type: str,
    *,
    evaluate: dict[str, Any],
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    # 失败、依赖失败和重启恢复使用同一份中立字段定义，避免嵌套对象变成 {}。
    # 有真实来源的手册内容仍保留，状态由调用方明确标记为 failed。
    return default_followup_report(
        report_type, evaluate=evaluate, user_id=user_id, session_id=session_id,
    )
