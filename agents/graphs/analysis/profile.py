"""体检报告解读使用的模型配置。"""

from __future__ import annotations

from ...llm.profiles import ModelProfile
from ...llm.types import ContextPolicy

ANALYSIS_HEALTH_REPORT_PROFILE = 'analysis.health_report'
ANALYSIS_MAX_OUTPUT_TOKENS = 1024 * 100


def create_analysis_health_report_profile(
    *,
    model: str | None = None,
) -> ModelProfile:
    """创建一次性体检报告结构化解读的无状态 Profile。"""
    return ModelProfile(
        name=ANALYSIS_HEALTH_REPORT_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='analysis/health_report.txt',
        model=model,
        thinking=False,
        capture_transport_metrics=True,
        model_kwargs={
            'max_tokens': ANALYSIS_MAX_OUTPUT_TOKENS,
            'response_format': {'type': 'json_object'},
            'temperature': 0,
        },
    )
