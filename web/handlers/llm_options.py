"""Validate public LLM options before starting an HTTP or SSE response."""

import tornado.web

from agents.llm.reasoning import DEFAULT_REASONING_EFFORT
from agents.llm.reasoning import validate_reasoning_effort


def reasoning_effort_from_body(body: dict) -> str:
    try:
        return validate_reasoning_effort(body.get('reasoningEffort', DEFAULT_REASONING_EFFORT))
    except ValueError as error:
        raise tornado.web.HTTPError(400, log_message='reasoningEffort 必须为 low、high 或 max') from error
