"""健康建档使用的模型配置。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...llm.profiles import ModelProfile
from ...llm.types import ContextPolicy

RECORD_PLAN_PROFILE = 'record.plan'
RECORD_QUESTION_PROFILE = 'record.question'


def build_record_question_prompt_variables(
    question_topics: Mapping[str, Any],
) -> dict[str, str]:
    """把问题主题配置转换为 record/question.txt 所需变量。"""
    if not isinstance(question_topics, Mapping):
        raise TypeError('question_topics must be a mapping')
    topics_text = ''.join(
        f'- {topic["category"]}: {topic["description"]}\n' for topic in question_topics.get('topics', [])
    )
    rules_text = '\n'.join(f'{index + 1}. {rule}' for index, rule in enumerate(question_topics.get('rules', [])))
    return {
        'topics_text': topics_text,
        'rules_text': rules_text,
    }


def create_record_plan_profile(*, model: str | None = None) -> ModelProfile:
    """创建理解患者情况与信息缺口的无状态 Profile，保留原能力名。"""
    return ModelProfile(
        name=RECORD_PLAN_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='record/plan.txt',
        model=model,
        thinking=False,
    )


def create_record_question_profile(
    *,
    prompt_variables: Mapping[str, Any],
    model: str | None = None,
) -> ModelProfile:
    """创建按应用层裁定生成回应与问题的无状态 Profile。"""
    return ModelProfile(
        name=RECORD_QUESTION_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='record/question.txt',
        prompt_variables=prompt_variables,
        model=model,
        thinking=False,
        model_kwargs={'response_format': {'type': 'json_object'}},
    )
