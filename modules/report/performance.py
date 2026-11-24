"""Request-local performance counters for the report workflow.

The metrics are internal diagnostics.  They intentionally never enter the
patient callback contract and are isolated per asyncio context so concurrent
reports cannot mix counters.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class ReportRuntimeMetrics:
    counters: dict[str, int] = field(default_factory=dict)
    elapsed_ms: dict[str, float] = field(default_factory=dict)

    def increment(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(value)

    def add_elapsed(self, name: str, value: float) -> None:
        self.elapsed_ms[name] = round(
            self.elapsed_ms.get(name, 0.0) + max(0.0, float(value)),
            3,
        )


_ACTIVE_REPORT_METRICS: ContextVar[ReportRuntimeMetrics | None] = ContextVar(
    "active_report_runtime_metrics",
    default=None,
)


def start_report_metrics() -> Token[ReportRuntimeMetrics | None]:
    return _ACTIVE_REPORT_METRICS.set(ReportRuntimeMetrics())


def reset_report_metrics(token: Token[ReportRuntimeMetrics | None]) -> None:
    _ACTIVE_REPORT_METRICS.reset(token)


def increment_report_metric(name: str, value: int = 1) -> None:
    metrics = _ACTIVE_REPORT_METRICS.get()
    if metrics is not None:
        metrics.increment(name, value)


def add_report_elapsed_ms(name: str, value: float) -> None:
    metrics = _ACTIVE_REPORT_METRICS.get()
    if metrics is not None:
        metrics.add_elapsed(name, value)


def report_metrics_snapshot() -> tuple[dict[str, int], dict[str, float]]:
    metrics = _ACTIVE_REPORT_METRICS.get()
    if metrics is None:
        return {}, {}
    return dict(metrics.counters), dict(metrics.elapsed_ms)
