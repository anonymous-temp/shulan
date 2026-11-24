"""Compact exception diagnostics without Python traceback output."""

from __future__ import annotations

from types import TracebackType

from pydantic import ValidationError


def _message(error: BaseException) -> str:
    if isinstance(error, ValidationError):
        # str(ValidationError) includes rejected input, including whole patient
        # objects for missing fields. Domain logs can provide safe diagnostics.
        return f'{error.error_count()} validation error(s); input details omitted'
    return ' '.join(str(error).splitlines())[:1000]


def _location(tb: TracebackType | None) -> str:
    if tb is None:
        return '-'
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f'{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}'


def format_exception_summary(error: BaseException, tb: TracebackType | None = None) -> str:
    """Retain the outer failure and root cause, without source text or frame locals."""
    root = error
    chain = []
    seen = set()
    while id(root) not in seen:
        seen.add(id(root))
        chain.append(type(root).__name__)
        cause = root.__cause__
        if cause is None and not root.__suppress_context__:
            cause = root.__context__
        if cause is None or id(cause) in seen:
            break
        root = cause
    summary = (
        f'error_type={type(error).__name__}, message={_message(error)}, '
        f'error_at={_location(tb if tb is not None else error.__traceback__)}'
    )
    if root is not error:
        summary += (
            f', error_chain={" -> ".join(chain)}, root_message={_message(root)}, '
            f'root_at={_location(root.__traceback__)}'
        )
    return summary
