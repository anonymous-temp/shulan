"""Public selection and provenance contracts for shared document outputs."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutputSelection:
    profile_name: str
    contract_version: str
    implementation_revision: str

    def __post_init__(self):
        for value in (self.profile_name, self.contract_version, self.implementation_revision):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError('Output selection requires non-empty, exact version identifiers')


class UnsupportedOutputVersionError(ValueError):
    """The requested implementation is unavailable; never substitute another revision."""

    code = 'document.output_version_unsupported'
    retryable = False


class OutputRegistrationConflictError(ValueError):
    """A full version key or fixed legacy alias was registered twice."""

    code = 'document.output_registration_conflict'


class OutputCacheConflictError(ValueError):
    """Several legacy results claim the same input; explicit reconciliation is required."""

    code = 'document.output_cache_conflict'
    retryable = False


@dataclass(frozen=True, slots=True)
class DocumentInputSnapshot:
    """Versions read from the persisted input, not from the current deployment."""

    parse_artifact_ref: str | None
    interpretation_artifact_ref: str | None
    interpretation_revision: str | None
    validation_revision: str | None
    interpretation_round: int
