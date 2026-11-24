"""AION prompt-manager adapter for the Record prompt port."""

from __future__ import annotations

from aion.prompts import prompt_manager

from modules.record.ports import PromptRenderer


class AionPromptRenderer:
    def render(self, name: str, **variables) -> str:
        return prompt_manager.render(name, **variables)


def create_application_record_prompt_renderer() -> PromptRenderer:
    return AionPromptRenderer()


__all__ = [
    'AionPromptRenderer',
    'create_application_record_prompt_renderer',
]
