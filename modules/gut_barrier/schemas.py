"""Public request models for the independent gut barrier API."""

from __future__ import annotations

import math
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .input_limits import normalize_structured_source


_MISSING_TEXT = {"", "null", "none", "n/a", "na", "--", "-", "—", "未知", "未填写", "未提供", "未检测", "未测"}


def _missing_placeholder(value: Any) -> bool:
    return value is None or isinstance(value, str) and value.strip().casefold() in _MISSING_TEXT


def _empty_source(value: Any) -> Any:
    return None if _missing_placeholder(value) or isinstance(value, list) and not value else value


def _survey_identity_conflicts(current: dict, previous: dict) -> bool:
    # Do not join clinical answers from explicitly different questionnaire subjects.
    for fields in (("name",), ("age",), ("gender", "sex"), ("idCard",)):
        identities = []
        for survey in (current, previous):
            identity = next((survey[key] for key in fields
                             if key in survey and not _missing_placeholder(survey[key])), None)
            if identity is not None:
                identity = str(identity).strip()
                if fields == ("age",):
                    match = re.fullmatch(r"(\d+)(?:\.0+)?\s*岁?", identity)
                    identity = str(int(match.group(1))) if match else identity
            identities.append(identity)
        if all(item is not None for item in identities) and identities[0] != identities[1]:
            return True
    return False


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


def normalize_url(value: str) -> str:
    """Normalize a complete Markdown link, not arbitrary text or HTML."""
    value = value.strip()
    if len(value) > 4096:
        raise ValueError("URL exceeds 4096 characters")
    link = re.fullmatch(r"\[[^\]\r\n]*\]\((https?://[^\s]+)\)", value)
    if link:
        value = link.group(1)
    if re.search(r"&(?:#\d+|#x[\da-fA-F]+|[A-Za-z]+);", value):
        raise ValueError("URL must not contain HTML entities")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("URL must not contain credentials or fragments")
    if any(ord(char) < 33 for char in value) or any(char in value for char in '<>"'):
        raise ValueError("invalid URL characters")
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=False,
                              validate_by_name=False, extra="forbid")


class RemoteFileInput(InputModel):
    url: str = Field(min_length=1, max_length=4096)
    file_type: Literal["pdf", "image"]

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented_document_reference(cls, value: Any) -> Any:
        if isinstance(value, dict) and ("documentId" in value or "document_id" in value):
            raise ValueError("documentId is not supported; supply url and fileType")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_url(value)


class IndicatorValue(InputModel):
    value: str | int | float | None = None
    unit: str | None = Field(default=None, max_length=64)

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: Any) -> Any:
        if _missing_placeholder(value):
            return None
        if isinstance(value, bool):
            raise ValueError("indicator value cannot be boolean")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("indicator value must be finite")
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 256:
                raise ValueError("indicator value exceeds 256 characters")
            if not value:
                return None
            if value.casefold() in {"nan", "inf", "+inf", "-inf", "infinity"}:
                raise ValueError("indicator value must be finite")
        return value

    @field_validator("unit", mode="before")
    @classmethod
    def validate_unit(cls, value: Any) -> Any:
        # Preserve an incomplete pair for assessment; never infer a missing unit.
        return None if _missing_placeholder(value) else value.strip() if isinstance(value, str) else value


class GutHealthIndicator(InputModel):
    """Three assessed markers; accept and discard the retired hsCRP input."""

    bacterial_endotoxin: IndicatorValue | None = None
    diamine_oxidase: IndicatorValue | None = None
    d_lactic_acid: IndicatorValue | None = None
    hs_crp: IndicatorValue | None = Field(default=None, alias="hsCRP", exclude=True)
    complete: bool | None = Field(default=None, strict=True)

    @field_validator("bacterial_endotoxin", "diamine_oxidase", "d_lactic_acid", mode="before")
    @classmethod
    def empty_marker(cls, value: Any) -> Any:
        return _empty_source(value)

    @field_validator("hs_crp", mode="before")
    @classmethod
    def discard_retired_marker(cls, value: Any) -> None:
        return None


class GutBarrierRequest(InputModel):
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=128)
    age: int | None = Field(default=None, ge=0, le=150)
    sex: str | None = Field(default=None, max_length=32)
    file_list: list[RemoteFileInput] = Field(default_factory=list, max_length=10)
    gut_health_indicator: GutHealthIndicator | None = None
    test_data: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    hospital_data: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    # Accept the revised questionnaire spelling; keep persisted requests stable.
    health_survey: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("healthSurvey", "healthSurvery"))
    microbiome_data: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_questionnaire_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "healthSurvery" not in value:
            return value
        copied = dict(value)
        current = _empty_source(copied.pop("healthSurvery"))
        previous = _empty_source(copied.get("healthSurvey"))
        if isinstance(current, dict) and isinstance(previous, dict):
            _validate_free_structure(current)
            _validate_free_structure(previous)
            if not _survey_identity_conflicts(current, previous):
                merged = dict(previous)
                for key, item in current.items():
                    if key not in merged or not _missing_placeholder(item):
                        merged[key] = item
                current = merged
        elif current is None:
            current = previous
        elif previous is not None and not isinstance(previous, dict):
            raise ValueError("healthSurvey must be an object or absent")
        copied["healthSurvey"] = current
        return copied

    @field_validator("health_survey", "microbiome_data", "gut_health_indicator", mode="before")
    @classmethod
    def empty_optional_source(cls, value: Any) -> Any:
        return _empty_source(value)

    @field_validator("health_survey")
    @classmethod
    def bounded_optional_source(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _validate_free_structure(value)
        return value

    @field_validator("microbiome_data")
    @classmethod
    def bounded_microbiome_document(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return normalize_structured_source(value) if value is not None else None

    @field_validator("user_id", "session_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("identifier must be nonempty and contain no control characters")
        return value

    @field_validator("age", mode="before")
    @classmethod
    def validate_age(cls, value: Any) -> Any:
        if _missing_placeholder(value):
            return None
        if isinstance(value, bool):
            raise ValueError("age must be an integer")
        if isinstance(value, str):
            value = value.strip()
            match = re.fullmatch(r"(\d+)(?:\.0+)?\s*岁?", value)
            if match:
                return int(match.group(1))
        return value

    @field_validator("file_list", "test_data", "hospital_data", mode="before")
    @classmethod
    def empty_list(cls, value: Any) -> Any:
        return [] if _missing_placeholder(value) else value

    @field_validator("test_data", "hospital_data")
    @classmethod
    def normalize_known_urls(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Keep free-form source structures intact; only known URL fields change.
        result = []
        for row in rows:
            _validate_free_structure(row)
            copied = dict(row)
            for key in ("resultDetailUrl", "resultSummaryUrl", "url"):
                if copied.get(key) is not None:
                    if not isinstance(copied[key], str):
                        raise ValueError(f"{key} must be a URL string")
                    copied[key] = None if _missing_placeholder(copied[key]) else normalize_url(copied[key])
            result.append(copied)
        return result

def _validate_free_structure(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    """Bound free-form source rows without dictating their clinical fields."""
    budget = [2000] if budget is None else budget
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        raise ValueError("source row is too deeply nested or too large")
    if isinstance(value, str) and len(value) > 20000:
        raise ValueError("source string exceeds 20000 characters")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise ValueError("invalid source field name")
            _validate_free_structure(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _validate_free_structure(item, depth + 1, budget)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("source numbers must be finite")
