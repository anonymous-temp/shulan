"""Read the fixed recording date of an Analysis report observation."""

from datetime import datetime
from datetime import timezone


def observation_timestamp(value: datetime | None) -> float:
    if value is None:
        return float('-inf')
    if value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def observation_recency(fact) -> tuple[float, float, float]:
    observed = fact.effective_period.start
    recorded = recorded_report_source(fact.sources) if observed is None else None
    return (
        observation_timestamp(observed),
        observation_timestamp(recorded.recorded_at if recorded else None),
        observation_timestamp(fact.updated_at),
    )


def recorded_report_source(sources):
    return min(
        (
            source
            for source in sources
            if source.source_type == 'health_report'
            and source.confirmation_basis == 'analysis_public'
            and source.recorded_at is not None
        ),
        key=lambda source: (observation_timestamp(source.recorded_at), source.source_id),
        default=None,
    )
