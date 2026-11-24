"""Diagnostic identity isolated across asyncio tasks and copied thread contexts."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import replace
from functools import wraps
from inspect import signature


@dataclass(frozen=True)
class LogContext:
    user_id: str | None = None
    session_id: str | None = None
    request_uuid: str | None = None
    task_id: str | None = None


_current = ContextVar('log_context', default=LogContext())


def log_context():
    return _current.get()


def update_log_context(**fields):
    context = replace(log_context(), **fields)
    _current.set(context)
    return context


def request_log_context(function):
    """Restore a handler's snapshot for callbacks outside the request task."""
    @wraps(function)
    def wrapped(handler, *args, **kwargs):
        with bind_log_context(getattr(handler, '_log_context', None) or LogContext()):
            return function(handler, *args, **kwargs)
    return wrapped


@contextmanager
def bind_log_context(context=None, **fields):
    token = _current.set(replace(context or log_context(), **fields))
    try:
        yield log_context()
    finally:
        _current.reset(token)


def task_log_context(*, user='user_id', session='session_id', job=None, task=None, clear=False):
    """Bind async work boundaries, including their exception handlers."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            arguments = parameters.bind(*args, **kwargs)
            arguments.apply_defaults()
            values = arguments.arguments
            current = log_context()
            if clear:
                context = LogContext()
            else:
                item = values.get(job) if job else None

                def field(python_name, json_name):
                    if isinstance(item, dict):
                        return item.get(json_name) or item.get(python_name)
                    return getattr(item, python_name, None)

                user_id = field('user_id', 'userId') if job else values.get(user)
                session_id = field('session_id', 'sessionId') if job else values.get(session)
                task_id = (field('job_id', '_id') if job else None) or task
                same_user = bool(user_id) and current.user_id == user_id
                context = LogContext(
                    user_id=user_id,
                    session_id=session_id or (current.session_id if same_user else None),
                    request_uuid=current.request_uuid if same_user else None,
                    task_id=str(task_id) if task_id is not None else current.task_id,
                )
            with bind_log_context(context):
                return await function(*args, **kwargs)

        return wrapped
    return decorate
