"""Explicit historical Record policy snapshots used by offline fixtures."""


def short_interview_policy() -> dict:
    """The reviewed 4–6 policy is explicit, never inferred from missing state."""
    return {'version': 1, 'minIntake': 4, 'targetIntake': 5, 'maxIntake': 6, 'maxInteractions': 20}
