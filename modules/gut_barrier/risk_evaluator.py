"""Server-only seam for a reviewed disease risk model; no clinical model ships here.

Registration is a deployment concern, not an input field. The adapter validates
metadata, applicability, input provenance and results; it never guesses a score.
The default factory registers nothing. Test evaluators live only under tests/.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from inspect import iscoroutinefunction
from typing import Any, Protocol

from .risk_model import build_disease_risks


_UNITS = {"D_LACTATE": "mg/L", "DAO": "U/L", "LPS": "U/L", "IBFC": "score"}
_INPUTS = frozenset((*_UNITS, "age", "sex"))


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    # Match the assessment's documented ISO / slash / Chinese calendar forms.
    # A parse failure is *not* proof that the observation was undated.
    match = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|\b|T)", value.strip())
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _date_supplied(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class RiskBand:
    """Reviewed model-owned bounds; the report adapter does not invent them."""

    label: str
    lower: float
    upper: float
    lower_inclusive: bool
    upper_inclusive: bool


@dataclass(frozen=True)
class RiskModelSpecification:
    model_id: str
    version: str
    approval_reference: str
    validation_reference: str
    endpoint: str
    time_horizon: str
    population: str
    # One tuple per covered disease: (stable catalog code, required input keys).
    disease_inputs: tuple[tuple[str, tuple[str, ...]], ...]
    bands: tuple[RiskBand, ...]
    precision: int
    min_age: int
    max_age: int
    allowed_sexes: tuple[str, ...]
    max_observation_age_days: int
    allow_undated_observations: bool = False
    exclude_acute_inflammation: bool = True

    def validate(self) -> None:
        for field in ("model_id", "version", "approval_reference", "validation_reference",
                      "endpoint", "time_horizon", "population"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError(f"risk model requires {field}")
        if type(self.precision) is not int or not 0 <= self.precision <= 6:
            raise ValueError("risk model precision must be 0..6")
        if any(type(value) is not int for value in (self.min_age, self.max_age, self.max_observation_age_days)):
            raise ValueError("risk model age and observation limits must be integers")
        if not 0 <= self.min_age <= self.max_age <= 150 or self.max_observation_age_days < 0:
            raise ValueError("invalid risk model applicability limits")
        if any(type(value) is not bool for value in (self.allow_undated_observations, self.exclude_acute_inflammation)):
            raise ValueError("risk model applicability flags must be boolean")
        if not isinstance(self.allowed_sexes, tuple) or any(not isinstance(s, str) or not s.strip() for s in self.allowed_sexes):
            raise ValueError("invalid risk model sex scope")
        catalog_codes = {row["code"] for row in build_disease_risks()[0]}
        if not isinstance(self.disease_inputs, tuple) or not self.disease_inputs:
            raise ValueError("risk model must declare per-disease inputs")
        codes = []
        for item in self.disease_inputs:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("invalid per-disease declaration")
            code, inputs = item
            if code not in catalog_codes or not isinstance(inputs, tuple) or not inputs or len(inputs) != len(set(inputs)):
                raise ValueError("invalid risk model disease/input declaration")
            if not set(inputs) <= _INPUTS:
                raise ValueError("risk model requires an input not implemented by this adapter")
            codes.append(code)
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate risk model disease code")
        if not isinstance(self.bands, tuple) or not self.bands:
            raise ValueError("risk model must declare reviewed risk bands")
        previous = None
        labels = set()
        for band in self.bands:
            if not isinstance(band, RiskBand) or not isinstance(band.label, str) or not band.label.strip() or band.label in labels:
                raise ValueError("invalid or duplicate risk band label")
            low, high = _number(band.lower), _number(band.upper)
            if low is None or high is None or not 0 <= low < high <= 10:
                raise ValueError("risk bands must be within the relative index 0..10")
            if type(band.lower_inclusive) is not bool or type(band.upper_inclusive) is not bool:
                raise ValueError("risk band endpoints must be explicit")
            if previous is None:
                if low != 0 or not band.lower_inclusive:
                    raise ValueError("risk bands must start at inclusive zero")
            elif low != _number(previous.upper) or band.lower_inclusive == previous.upper_inclusive:
                raise ValueError("risk bands contain a gap or overlapping endpoint")
            previous = band
            labels.add(band.label)
        if _number(previous.upper) != 10 or not previous.upper_inclusive:
            raise ValueError("risk bands must end at inclusive ten")


class DiseaseRiskEvaluator(Protocol):
    async def evaluate(self, inputs: dict, *, disease_codes: tuple[str, ...]) -> dict: ...


class RiskModelRegistry:
    """Explicit server composition only. There is intentionally no default model."""

    def __init__(self):
        self._models: dict[tuple[str, str], tuple[RiskModelSpecification, DiseaseRiskEvaluator]] = {}

    def register(self, specification: RiskModelSpecification, evaluator: DiseaseRiskEvaluator) -> None:
        if not isinstance(specification, RiskModelSpecification):
            raise TypeError("risk model specification must be server-defined")
        specification.validate()
        if not iscoroutinefunction(getattr(evaluator, "evaluate", None)):
            raise TypeError("risk evaluator must implement async evaluate")
        key = (specification.model_id, specification.version)
        if key in self._models:
            raise ValueError("a risk model version cannot be replaced after registration")
        self._models[key] = (specification, evaluator)

    def resolve(self, model_id: str, version: str):
        if not isinstance(model_id, str) or not isinstance(version, str):
            return None
        return self._models.get((model_id, version))


def _origin(source: Any) -> dict:
    if not isinstance(source, dict):
        return {}
    # Do not pass through request extras, extracted instructions or NGS scores.
    return {key: deepcopy(source[key]) for key in ("kind", "path", "index", "page", "documentId", "url")
            if isinstance(source.get(key), (str, int)) and not isinstance(source.get(key), bool)}


def _inputs(assessment: dict, evaluation_date: str, spec: RiskModelSpecification) -> tuple[dict, dict, dict]:
    subject, context = assessment.get("subject", {}), assessment.get("context", {})
    values, provenance, unavailable = {}, {}, {}
    for key in ("age", "sex"):
        value = subject.get(key)
        if value is not None and value != "":
            values[key] = value
            provenance[key] = {"kind": "declared_demographics", "path": key}
        else:
            unavailable[key] = "missing_input"
    target = _date(evaluation_date)
    raw_sample = context.get("sampleDate")
    parsed_sample = _date(raw_sample)
    invalid_sample = _date_supplied(raw_sample) and parsed_sample is None
    sample = parsed_sample or target
    for marker in assessment.get("markers", []):
        key = marker.get("code")
        if key not in _UNITS or key == "IBFC":
            continue
        if target is None or invalid_sample:
            unavailable[key] = "invalid_evaluation_or_sample_date"
            continue
        number = _number(marker.get("value"))
        if marker.get("status") != "evaluated" or marker.get("operator") != "=" or number is None:
            unavailable[key] = "missing_or_unresolved_measurement"
            continue
        if marker.get("unit") != _UNITS[key]:
            unavailable[key] = "unexpected_measurement_unit"
            continue
        records = []
        invalid_source_date = False
        for record in marker.get("source", []):
            if not isinstance(record, dict):
                continue
            observed = _date(record.get("date"))
            if _date_supplied(record.get("date")) and observed is None:
                invalid_source_date = True
                continue
            if observed is not None and observed != sample:
                continue
            if record.get("isCurrent") is True or (observed == sample and record.get("isCurrent") is not False):
                records.append(record)
        if invalid_source_date:
            unavailable[key] = "invalid_observation_date"
            continue
        if not records:
            unavailable[key] = "missing_current_source"
            continue
        if any(not _origin(record.get("source")) for record in records):
            unavailable[key] = "untraceable_measurement_source"
            continue
        dates = [_date(record.get("date")) for record in records]
        if any(day is None for day in dates) and not spec.allow_undated_observations:
            unavailable[key] = "observation_date_required"
            continue
        if target is None or any(day is not None and not 0 <= (target - day).days <= spec.max_observation_age_days for day in dates):
            unavailable[key] = "observation_outside_model_time_window"
            continue
        values[key] = float(number)
        provenance[key] = {"unit": marker["unit"], "observations": [
            {"date": day.isoformat() if day else None, "source": _origin(record.get("source"))}
            for record, day in zip(records, dates)
        ]}
    barrier = assessment.get("barrier", {})
    if barrier.get("status") == "evaluated" and all(key in values for key in ("D_LACTATE", "DAO", "LPS")) and _number(barrier.get("score")) is not None:
        values["IBFC"] = barrier["score"]
        provenance["IBFC"] = {"kind": "derived", "ruleVersion": assessment.get("ruleVersion"),
                              "inputs": ["D_LACTATE", "DAO", "LPS"]}
    else:
        unavailable["IBFC"] = "barrier_score_unresolved"
    return values, provenance, unavailable


def _trace(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Copy a small JSON audit trace; reject objects, non-finite numbers and PII blobs."""
    budget = [300] if budget is None else budget
    budget[0] -= 1
    if depth > 5 or budget[0] < 0:
        raise ValueError("trace too deep or too large")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and len(value) <= 1000:
        return value
    if isinstance(value, (int, float)) and _number(value) is not None:
        return value
    if isinstance(value, list) and len(value) <= 30:
        return [_trace(item, depth + 1, budget) for item in value]
    if isinstance(value, dict) and len(value) <= 30 and all(isinstance(key, str) and len(key) <= 100 for key in value):
        return {key: _trace(item, depth + 1, budget) for key, item in value.items()}
    raise ValueError("invalid model trace")


def _validate_result(item: dict, spec: RiskModelSpecification) -> tuple[float | None, str | None, str | None]:
    if item.get("probability") is not None:
        return None, None, "probability_not_supported_by_relative_index_model"
    number = _number(item.get("riskIndex"))
    if number is None or not 0 <= number <= 10:
        return None, None, "invalid_risk_index"
    if number != number.quantize(Decimal(1).scaleb(-spec.precision)):
        return None, None, "risk_index_precision_mismatch"
    for band in spec.bands:
        lower, upper = _number(band.lower), _number(band.upper)
        if (number > lower or number == lower and band.lower_inclusive) and (number < upper or number == upper and band.upper_inclusive):
            if item.get("riskLevel") != band.label:
                return None, None, "risk_level_mismatch"
            return float(number), band.label, None
    return None, None, "unclassified_risk_index"


async def evaluate_registered_risks(assessment: dict, *, registry: RiskModelRegistry | None,
                                    model_id: str | None, version: str | None,
                                    evaluation_date: str) -> tuple[list[dict], dict]:
    """Evaluate eligible rows only and fail closed independently per disease.

    No request object is accepted. Model identity/approval cannot come from JSON.
    Returned metadata is internal audit data, not an extension of the public API.
    """
    rows, audit = build_disease_risks()
    if registry is None and model_id is None and version is None:
        return rows, audit
    resolved = registry.resolve(model_id, version) if isinstance(registry, RiskModelRegistry) else None
    if resolved is None:
        audit.update(status="not_configured", modelId=model_id, modelVersion=version,
                     reasons=["server_model_version_not_registered"])
        return rows, audit
    spec, evaluator = resolved
    audit.update(modelId=spec.model_id, modelVersion=spec.version,
                 scale={"kind": "relative_index", "min": 0, "max": 10, "precision": spec.precision,
                        "bands": [asdict(band) for band in spec.bands]},
                 metadata={"approvalReference": spec.approval_reference, "validationReference": spec.validation_reference,
                           "endpoint": spec.endpoint, "timeHorizon": spec.time_horizon, "population": spec.population},
                 evaluationDate=evaluation_date, inputSources={}, inputValues={}, items=[], reasons=[])
    values, sources, unavailable = _inputs(assessment, evaluation_date, spec)
    audit["inputSources"], audit["inputValues"] = deepcopy(sources), deepcopy(values)
    scope_reason = None
    age = values.get("age")
    if _date(evaluation_date) is None:
        scope_reason = "invalid_evaluation_date"
    elif age is None:
        scope_reason = "age_required_for_model_applicability"
    elif type(age) is not int or not spec.min_age <= age <= spec.max_age:
        scope_reason = "age_outside_model_population"
    elif spec.allowed_sexes and values.get("sex") not in spec.allowed_sexes:
        scope_reason = "sex_outside_model_population"
    elif spec.exclude_acute_inflammation and any(assessment.get("context", {}).get(key) is True for key in ("acuteInfection", "acuteInflammation")):
        scope_reason = "acute_inflammatory_context_excluded"
    required = dict(spec.disease_inputs)
    eligible = []
    item_audits = {}
    for row in rows:
        code = row["code"]
        if code not in required:
            status, reasons = "not_supported", ["disease_not_covered_by_model"]
        elif scope_reason:
            status = "missing_inputs" if age is None else "not_applicable"
            reasons = [scope_reason]
        else:
            reasons = [f"{key}:{unavailable.get(key, 'missing_input')}" for key in required[code] if key not in values]
            status = "missing_inputs" if reasons else "pending"
        row.update(status=status, reasons=reasons)
        item_audits[code] = {"code": code, "status": status, "reasons": list(reasons),
                             "requiredInputs": list(required.get(code, ())), "trace": None}
        if status == "pending":
            eligible.append(code)
    failure = None
    response = None
    if eligible:
        try:
            # Bound only this independent adapter; existing API scheduling and
            # generation/callback timeouts are not changed.
            async with asyncio.timeout(15):
                response = await evaluator.evaluate({"evaluationDate": evaluation_date, "values": deepcopy(values),
                                                     "sources": deepcopy(sources)}, disease_codes=tuple(eligible))
        except Exception as error:
            failure = f"evaluator_error:{type(error).__name__}"
        if failure is None and (not isinstance(response, dict) or response.get("modelId") != spec.model_id or response.get("modelVersion") != spec.version):
            failure = "model_response_version_mismatch"
        if failure is None and not isinstance(response.get("results"), list):
            failure = "invalid_model_result_collection"
        if failure is None and len(response["results"]) > 2 * len(rows):
            failure = "oversized_model_result_collection"
    results = response.get("results", []) if isinstance(response, dict) and failure is None else []
    indexed: dict[str, list] = {}
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and isinstance(item.get("code"), str) and item["code"] in eligible:
                indexed.setdefault(item["code"], []).append(item)
            else:
                audit["reasons"].append("unexpected_model_result_item")
    for row in rows:
        code = row["code"]
        if code not in eligible:
            continue
        candidates = indexed.get(code, [])
        reason = failure or ("model_result_missing" if not candidates else "duplicate_model_result" if len(candidates) > 1 else None)
        value, level, trace = None, None, None
        if reason is None:
            item = candidates[0]
            if item.get("status", "evaluated") != "evaluated":
                reason = "model_item_not_evaluated"
            else:
                value, level, reason = _validate_result(item, spec)
                if reason is None:
                    try:
                        trace = _trace(item.get("trace"))
                    except ValueError:
                        reason = "invalid_model_trace"
        if reason:
            row.update(status="failed", reasons=[reason])
        else:
            row.update(riskIndex=value, riskLevel=level, status="evaluated", reasons=[])
        item_audits[code].update(status=row["status"], reasons=list(row["reasons"]), trace=trace)
    counts = Counter(row["status"] for row in rows)
    if counts["evaluated"] == len(rows) and not audit["reasons"]:
        status = "evaluated"
    elif counts["evaluated"]:
        status = "partial"
    elif counts["failed"]:
        status = "failed"
    elif counts["not_applicable"]:
        status = "not_applicable"
    else:
        status = "missing_inputs"
    audit.update(status=status, items=list(item_audits.values()), counts=dict(counts),
                 reasons=sorted(set(audit["reasons"] + [reason for row in rows for reason in row["reasons"]])))
    return rows, audit
