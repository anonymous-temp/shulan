"""Lazy public composition helpers, without initializing unrelated application dependencies."""

from importlib import import_module

__all__ = [
    'AppContainer',
    'build_application_bundle',
    'build_container',
    'create_application_health_report_interpreter',
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = 'bootstrap.container' if name == 'AppContainer' else 'bootstrap.factory'
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
