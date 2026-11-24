"""Stable lazy public entry for Chat application services."""

from importlib import import_module

__all__ = [
    'CHAT_MAIN_SELECTION',
    'ChatAgent',
    'ChatCustomMessageConflictError',
    'ChatCustomMessageValidationError',
    'ChatSessionManager',
    'ChatSessionNotFoundError',
    'chat_agent_manifest',
    'install_chat_agent',
]


def __getattr__(name: str):
    if name in {'CHAT_MAIN_SELECTION', 'ChatAgent', 'chat_agent_manifest', 'install_chat_agent'}:
        return getattr(import_module('modules.chat.agent'), name)
    if name in __all__:
        return getattr(import_module('modules.chat.application.service'), name)
    raise AttributeError(name)
