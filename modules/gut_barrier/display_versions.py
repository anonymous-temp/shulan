"""Internal rendering release IDs, intentionally absent from the wire schema.

A bare historical public.v2 receipt always uses v1. Consumers must explicitly
bind a release (or use the persisted audit) to opt into a newer frontend asset.
"""

from dataclasses import dataclass, replace
from types import MappingProxyType


LEGACY_DISPLAY_TEMPLATE = "gut-barrier-static.v1"
PRE_XU_DISPLAY_TEMPLATE = "gut-barrier-static.v2"
XU_DISPLAY_TEMPLATE = "gut-barrier-static.v3"
REFINED_XU_DISPLAY_TEMPLATE = "gut-barrier-static.v4"
CLEAN_REVIEW_DISPLAY_TEMPLATE = "gut-barrier-static.v5"
SOURCE_STAGE_DISPLAY_TEMPLATE = "gut-barrier-static.v6"
WORD_REVIEW_DISPLAY_TEMPLATE = "gut-barrier-static.v7"
CANONICAL_DISPLAY_TEMPLATE = "gut-barrier-static.v8"
PRODUCT_FEEDBACK_DISPLAY_TEMPLATE = "gut-barrier-static.v9"
ORDERED_REFERENCE_DISPLAY_TEMPLATE = "gut-barrier-static.v10"
TEMPLATE_RISK_DISPLAY_TEMPLATE = "gut-barrier-static.v11"
PINNED_REFERENCE_DISPLAY_TEMPLATE = "gut-barrier-static.v12"
CLOSED_BOUNDARY_DISPLAY_TEMPLATE = "gut-barrier-static.v13"
BARRIER_ONLY_DISPLAY_TEMPLATE = "gut-barrier-static.v14"
LATEST_DISPLAY_TEMPLATE = BARRIER_ONLY_DISPLAY_TEMPLATE


@dataclass(frozen=True)
class DisplayCapabilities:
    compact_layout: bool = False
    xu_review: bool = False
    primary_reference_number: int = 5
    refined_fixed_layout: bool = False
    clean_review_layout: bool = False
    source_stage_layout: bool = False
    word_review_layout: bool = False
    canonical_dynamic_layout: bool = False
    product_feedback_layout: bool = False
    ordered_reference_layout: bool = False
    template_risk_layout: bool = False
    pinned_reference_layout: bool = False
    closed_boundary_layout: bool = False
    barrier_only_layout: bool = False


DISPLAY_CAPABILITIES = MappingProxyType({
    LEGACY_DISPLAY_TEMPLATE: DisplayCapabilities(),
    PRE_XU_DISPLAY_TEMPLATE: DisplayCapabilities(compact_layout=True, primary_reference_number=4),
    XU_DISPLAY_TEMPLATE: DisplayCapabilities(compact_layout=True, xu_review=True, primary_reference_number=4),
    REFINED_XU_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4, refined_fixed_layout=True),
    CLEAN_REVIEW_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True),
    SOURCE_STAGE_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True),
    WORD_REVIEW_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True),
    CANONICAL_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True),
    PRODUCT_FEEDBACK_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True, product_feedback_layout=True),
    ORDERED_REFERENCE_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True, product_feedback_layout=True,
        ordered_reference_layout=True),
    TEMPLATE_RISK_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True, product_feedback_layout=True,
        ordered_reference_layout=True, template_risk_layout=True),
    PINNED_REFERENCE_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True, product_feedback_layout=True,
        ordered_reference_layout=True, template_risk_layout=True, pinned_reference_layout=True),
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE: DisplayCapabilities(
        compact_layout=True, xu_review=True, primary_reference_number=4,
        refined_fixed_layout=True, clean_review_layout=True, source_stage_layout=True,
        word_review_layout=True, canonical_dynamic_layout=True, product_feedback_layout=True,
        ordered_reference_layout=True, template_risk_layout=True, pinned_reference_layout=True,
        closed_boundary_layout=True),
})
DISPLAY_CAPABILITIES = MappingProxyType({
    **DISPLAY_CAPABILITIES,
    BARRIER_ONLY_DISPLAY_TEMPLATE: replace(DISPLAY_CAPABILITIES[CLOSED_BOUNDARY_DISPLAY_TEMPLATE],
                                           barrier_only_layout=True),
})
SUPPORTED_DISPLAY_TEMPLATES = frozenset(DISPLAY_CAPABILITIES)


def display_capabilities(version: str) -> DisplayCapabilities:
    """Release capabilities do not change when the latest-release pointer moves."""
    return DISPLAY_CAPABILITIES[resolve_display_template({}, version)]


def resolve_display_template(report: dict, explicit: str | None = None) -> str:
    version = explicit
    # Never interpret arbitrary extra fields on a received public callback as
    # trusted internal release metadata. Public versions have no such contract.
    if version is None and not str(report.get("schemaVersion", "")).startswith("gut-barrier-public."):
        meta = report.get("meta")
        if isinstance(meta, dict):
            version = meta.get("displayTemplateVersion")
    if version is None:
        return LEGACY_DISPLAY_TEMPLATE
    if not isinstance(version, str) or version not in SUPPORTED_DISPLAY_TEMPLATES:
        raise ValueError("unsupported gut barrier display template version")
    return version
