"""Version identity and independent limits for gut-barrier report generation.

The application composes one service instance per process. This is an
in-process generation limit, not a deployment-wide or callback-delivery limit.
Keep it separate from shared report defaults, which other report types reuse.
"""

# Version preflight reads this without initializing logging or configuration.
SERVICE_VERSION = "gut-barrier-api-v2.20260924-final-input"
GUT_BARRIER_GENERATION_CONCURRENCY = 20
# Retired durable job identity, retained only to exclude historical children.
# Never rename stored jobs or rewrite their frozen bodies during an upgrade.
EXPLANATION_TYPE = "gutBarrierExplain"


def is_explanation_callback(body):
    """Identify retired independent explain bodies so they cannot be sent."""
    if not isinstance(body, dict):
        return False
    return body.get("type") == EXPLANATION_TYPE or (
        body.get("type") == "gutBarrier"
        and "schemaVersion" not in body and "report" not in body
        and any(key in body for key in ("diagnostic_summary", "diagnostic_basis", "diagnostic_advice"))
    )
