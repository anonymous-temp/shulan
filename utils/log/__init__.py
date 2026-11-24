"""Compatible logging entry point; standalone helpers do not initialize sinks.

Importing context, files or preview is configuration-free. Accessing the public
logger API loads core once, sharing its logger registry and file writer.
"""

from importlib import import_module

__all__ = [
    'getLogger',
    'global_logger',
    'configure_tornado_logging',
    'shutdown_logging',
    'ContextFilter',
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module('.core', __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
