"""Stable lazy public entry for Analysis application services."""

from importlib import import_module

__all__ = [
    'HealthReportInterpreter',
    'HealthReportProcessingService',
    'HealthReportUnsupportedFormatError',
    'HealthReportUpstreamError',
    'ProcessedHealthReport',
    'PreparedDocumentRound',
]


def __getattr__(name: str):
    if name in __all__:
        return getattr(import_module('services.analysis'), name)
    raise AttributeError(name)
