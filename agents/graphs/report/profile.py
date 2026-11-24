"""最终报告主叙事使用的模型配置。"""

from __future__ import annotations

from ...llm.profiles import ModelProfile
from ...llm.types import ContextPolicy

REPORT_NARRATIVE_PROFILE = 'report.narrative'

# Tasks that plan source relationships or write/review whole chapters reason
# before answering. The many
# small structural tasks (extraction, grouping, scoring attribution, query
# planning) stay non-thinking: they are bounded JSON transformations and the
# extra latency would consume the report's time budget.
REPORT_THINKING_TASKS = frozenset({
    'write_report_chapter', 'review_report_chapter',
    'plan_source_bound_assessment_content', 'review_source_bound_assessment_content',
})


def create_report_narrative_profile(
        *,
        model: str | None = None,
        prompt_name: str = 'report/narrative.txt',
) -> ModelProfile:
    """创建最终报告主叙事的无状态 Profile。

    ``temperature=0`` 是既有业务行为：同一份来源和证据不应随机增减
    已经通过校验的公开行。报告不复用模型多轮历史，因此使用 STATELESS，
    并由注册函数关闭 checkpoint；这不妨碍业务层把已校验的画像快照映射
    进 payload。
    """
    return ModelProfile(
        name=REPORT_NARRATIVE_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name=prompt_name,
        model=model,
        thinking=False,
        model_kwargs={'temperature': 0},
    )


REPORT_FOLLOWUP_PROFILE = 'report.followup'


def create_report_followup_profile(
        *,
        model: str | None = None,
) -> ModelProfile:
    """创建后续四类报告内部模型任务的无状态 Profile。

    固定 Prompt 与主叙事完全隔离：主叙事禁止推荐产品，本 Prompt 是产品与
    干预导向的，两者不能合并。既有调用没有设置 temperature，这里同样不设，
    保持模型默认采样参数不变。
    """
    return ModelProfile(
        name=REPORT_FOLLOWUP_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='report/followups/system.txt',
        model=model,
        thinking=False,
    )


REPORT_RETRIEVE_CONTEXT_PROFILE = 'report.retrieve_context'


def create_report_retrieve_context_profile(
        *,
        model: str | None = None,
) -> ModelProfile:
    """创建多份已授权报告候选的无状态选择 Profile。"""
    return ModelProfile(
        name=REPORT_RETRIEVE_CONTEXT_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='report/retrieve_context.txt',
        model=model,
        thinking=False,
        model_kwargs={'temperature': 0},
    )
