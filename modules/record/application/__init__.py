"""Stable lazy public entry for Record application services."""

from importlib import import_module

__all__ = ['QASessionConflictError', 'QASessionManager']


def __getattr__(name: str):
    if name in __all__:
        return getattr(import_module('services.record'), name)
    raise AttributeError(name)
