"""Trusted frontend release binding, outside the frozen callback JSON body."""
from __future__ import annotations

import hashlib
import json

from modules.gut_barrier.display_versions import resolve_display_template
from modules.gut_barrier.publication import SUPPORTED_CALLBACK_TYPES


RENDER_HEADERS = {
    "templateVersion": "X-Gut-Barrier-Template-Version",
    "reportId": "X-Gut-Barrier-Report-Id",
    "reportSha256": "X-Gut-Barrier-Report-SHA256",
}


def report_digest(report: dict) -> str:
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_render_context(report: dict, template_version: str) -> dict:
    return {"templateVersion": resolve_display_template({}, template_version),
            "reportId": report.get("meta", {}).get("reportId"),
            "reportSha256": report_digest(report)}


def resolve_render_context(report: dict, context: dict) -> str:
    if (not isinstance(context, dict)
            or set(context) != {"templateVersion", "reportId", "reportSha256"}
            or context.get("reportId") != report.get("meta", {}).get("reportId")
            or context.get("reportSha256") != report_digest(report)):
        raise ValueError("render context does not match the immutable report")
    # This digest prevents accidental mixing, not an authentication signature.
    # Only use context from the same trusted local audit/release process.
    if context.get("templateVersion") is None:
        raise ValueError("render context template version is required")
    return resolve_display_template({}, context["templateVersion"])


def callback_render_context(callback: dict, template_version: str) -> dict:
    """Bind an already frozen successful receipt; never infer from patient data."""
    report = callback.get("report")
    if (callback.get("type") not in SUPPORTED_CALLBACK_TYPES
            or callback.get("status") not in {"success", "partial"}
            or not isinstance(report, dict)
            or not isinstance(callback.get("finalReportId"), str)
            or not callback["finalReportId"]
            or report.get("meta", {}).get("reportId") != callback["finalReportId"]):
        raise ValueError("callback has no matching generated report")
    return make_render_context(report, template_version)


def render_context_headers(callback: dict, context: dict | None) -> dict:
    if context is None:
        return {}  # Old receipts without a trusted binding keep legacy behavior.
    if not isinstance(context, dict):
        raise ValueError("invalid callback render context")
    expected = callback_render_context(callback, context.get("templateVersion"))
    if expected != context:
        raise ValueError("callback render context does not match frozen report")
    # Report IDs normally come from the server UUID. Reject unsafe header values
    # rather than coercing arbitrary imported historical metadata into HTTP.
    if any(not isinstance(value, str) or not value.isascii()
           or any(ord(char) < 33 or ord(char) > 126 for char in value)
           for value in context.values()):
        raise ValueError("invalid callback render header value")
    return {header: context[field] for field, header in RENDER_HEADERS.items()}


def render_context_from_headers(callback: dict, headers) -> dict | None:
    """Receiver helper: validate/persist context received over trusted transport.

    The digest detects accidental mixing, not forgery. Authenticate the callback
    channel before calling this helper. Missing all headers denotes legacy;
    partial/unknown/mismatched headers fail closed, never silently select v1.
    """
    if not hasattr(headers, "items"):
        raise ValueError("callback headers must be a mapping")
    normalized = {str(key).lower(): value for key, value in headers.items()}
    values = {field: normalized.get(header.lower()) for field, header in RENDER_HEADERS.items()}
    if all(value is None for value in values.values()):
        return None
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("incomplete callback render headers")
    render_context_headers(callback, values)
    return values
