"""Concrete outbound adapters for the Record module."""

from adapters.record.prompts import AionPromptRenderer
from adapters.record.prompts import create_application_record_prompt_renderer

__all__ = [
    'AionPromptRenderer',
    'create_application_record_prompt_renderer',
]
