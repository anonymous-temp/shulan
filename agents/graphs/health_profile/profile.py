"""用户健康画像候选提取使用的模型配置。"""

from __future__ import annotations

from ...llm.profiles import ModelProfile
from ...llm.types import ContextPolicy

PROFILE_RECORD_EXTRACTION_PROFILE = 'profile.extract_record_candidates'
PROFILE_CHAT_EXTRACTION_PROFILE = 'profile.extract_chat_candidates'
PROFILE_FOCUS_PROFILE = 'profile.plan_focus'
PROFILE_SYNTHESIS_PROFILE = 'profile.synthesize_evidence'


def create_profile_focus_profile(*, model: str | None = None) -> ModelProfile:
    return ModelProfile(
        name=PROFILE_FOCUS_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='health_profile/focus_plan.txt',
        model=model,
        thinking=False,
    )


def create_profile_synthesis_profile(*, model: str | None = None) -> ModelProfile:
    return ModelProfile(
        name=PROFILE_SYNTHESIS_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='health_profile/evidence_synthesis.txt',
        model=model,
        thinking=False,
    )


def create_profile_record_extraction_profile(
    *,
    model: str | None = None,
) -> ModelProfile:
    """创建不保存上下文、不输出推理过程的建档候选提取 Profile。"""
    return ModelProfile(
        name=PROFILE_RECORD_EXTRACTION_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='health_profile/record_candidates.txt',
        model=model,
        thinking=False,
    )


def create_profile_chat_extraction_profile(
    *,
    model: str | None = None,
) -> ModelProfile:
    """创建不保存上下文、不输出推理过程的聊天候选提取 Profile。"""
    return ModelProfile(
        name=PROFILE_CHAT_EXTRACTION_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='health_profile/chat_candidates.txt',
        model=model,
        thinking=False,
    )
