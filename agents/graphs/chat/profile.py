"""普通聊天使用的模型配置。"""

from __future__ import annotations

from ...llm.profiles import ModelProfile
from ...llm.types import ContextPolicy

CHAT_REPLY_PROFILE = 'chat.reply'
CHAT_SUGGESTIONS_PROFILE = 'chat.suggestions'


def create_chat_reply_profile(*, model: str | None = None) -> ModelProfile:
    """创建普通聊天的默认 ModelProfile。

    普通聊天启用 AION_FULL，使同一个内部会话标识下的模型历史由
    AION Context Repository 持久化，而不是写入 LangGraph State。
    """
    return ModelProfile(
        name=CHAT_REPLY_PROFILE,
        context_policy=ContextPolicy.AION_FULL,
        prompt_name='chat/chat_system.txt',
        model=model,
        thinking=True,
    )


def create_chat_suggestions_profile(
    *,
    model: str | None = None,
) -> ModelProfile:
    """创建个性化推荐问题的无状态 Profile。"""
    return ModelProfile(
        name=CHAT_SUGGESTIONS_PROFILE,
        context_policy=ContextPolicy.STATELESS,
        prompt_name='chat/suggestions.txt',
        model=model,
        thinking=False,
    )
