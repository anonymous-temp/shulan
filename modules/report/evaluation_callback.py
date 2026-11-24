"""Project the new evaluate contract without invoking legacy public mapping."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from datetime import datetime
from typing import Any

from modules.report.artifacts import active_final_report_id
from modules.report.assessment_callback_view import ORDERED_ASSESSMENT_VERSION
from modules.report.assessment_callback_view import assessment_view_for_rendering
from modules.report.assessment_callback_view import order_assessment_for_delivery
from modules.report.evaluation_schema import EvaluationCallback
from modules.report.evaluation_schema import EvaluationData
from modules.report.evaluation_schema import EvaluationError
from modules.report.evaluation_schema import EvaluationSample
from modules.report.evaluation_schema import EvaluationSubject


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    return dict(value) if isinstance(value, Mapping) else {}


def _get(value: dict[str, Any], camel: str, snake: str) -> Any:
    return value[camel] if camel in value else value.get(snake)


def _compact_fact_locations(data: dict[str, Any], clinical_document: Any) -> None:
    """Drop a private array position only beside its identical stable fact reference."""
    facts = _mapping(clinical_document).get('facts') or []
    fact_ids = [_get(_mapping(fact), 'factId', 'fact_id') for fact in facts]
    counts = Counter(fact_id for fact_id in fact_ids if isinstance(fact_id, str))
    aliases = {
        f'clinicalDocument.facts[{index}]': fact_id
        for index, fact_id in enumerate(fact_ids)
        if isinstance(fact_id, str)
        and fact_id
        and counts[fact_id] == 1
        and not fact_id.startswith('clinicalDocument.facts[')
    }
    if not aliases:
        return

    def source_key(ref):
        if (
            isinstance(ref, dict)
            and ref.keys() == {'sourceCode', 'recordId', 'observedAt'}
            and isinstance(ref['recordId'], str)
            and ref['sourceCode'] in ('A', 'B', 'C')
            and (ref['observedAt'] is None or isinstance(ref['observedAt'], str))
        ):
            return ref['sourceCode'], ref['observedAt'], ref['recordId']
        return None

    def visit(node):
        if isinstance(node, dict):
            refs = node.get('sources')
            if isinstance(refs, list) and len(refs) > 1:
                keys = [source_key(ref) for ref in refs]
                present = set(keys)
                node['sources'] = [
                    ref
                    for ref, key in zip(refs, keys, strict=True)
                    if key is None or (key[0], key[1], aliases.get(key[2])) not in present
                ]
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(data)


def build_evaluation_callback(payload: Any) -> dict[str, Any]:
    """Return validated evaluate fields with the approved raw-detail compaction.

    ``report.evaluationData`` is the sole public-content input. Historical
    narrative fields, audit records and internal prompts are never copied.
    Final serialization removes the explicitly retired raw/statistical arrays
    and duplicate health-topic rows, hides source notices and suppresses empty
    overall/health rows. Other confirmed names and empty questionnaire
    structures remain. Generated cell prose uses a single paragraph.
    """
    if not isinstance(payload, Mapping) and not hasattr(payload, "model_dump"):
        raise TypeError("evaluate callback payload must be a mapping or model")
    raw = _mapping(payload)
    if "schemaVersion" in raw or "schema_version" in raw:
        public = EvaluationCallback.model_validate(deepcopy(raw))
        if public.status == "failed":
            public.data = EvaluationData()
        return public.model_dump(mode="json", by_alias=True, exclude_none=False)

    report = _mapping(raw.get("report"))
    subject = _mapping(report.get("subject"))
    sample = _mapping(report.get("sample"))
    status = raw.get("status") or "failed"
    supplied_data = _get(report, "evaluationData", "evaluation_data")
    data = (
        EvaluationData()
        if status == "failed" or supplied_data is None
        else EvaluationData.model_validate(supplied_data)
    )
    # evaluationData is the reviewed final projection. Transport must not
    # re-group the clinical findings from the pre-authoring document.
    final_report_id = _get(raw, "finalReportId", "final_report_id") or active_final_report_id()
    generated_at = _get(raw, "generatedAt", "generated_at")
    if isinstance(generated_at, (date, datetime)):
        generated_at = generated_at.isoformat()
    error = _mapping(raw.get("error"))
    public = EvaluationCallback(
        user_id=_get(raw, "userId", "user_id"),
        session_id=_get(raw, "sessionId", "session_id"),
        group_id=_get(raw, "groupId", "group_id"),
        fmt_typing_report_url=_get(raw, "fmtTypingReportUrl", "fmt_typing_report_url"),
        package=deepcopy(raw.get("package") if raw.get("package") is not None else []),
        final_report_id=final_report_id,
        type="evaluate",
        status=status,
        generated_at=generated_at,
        report_date=_get(raw, "reportDate", "report_date"),
        partial_reasons=_get(raw, "partialReasons", "partial_reasons") or [],
        subject=EvaluationSubject(
            name=subject.get("name"),
            gender=subject.get("sex") if subject.get("sex") is not None else subject.get("gender"),
            age=subject.get("age"),
        ),
        sample=EvaluationSample(**{
            camel: _get(sample, camel, snake)
            for camel, snake in (
                ("sampleId", "sample_id"), ("sampleType", "sample_type"),
                ("testItem", "test_item"), ("sampleDate", "sample_date"),
                ("reportDate", "report_date"), ("sequencingPlatform", "sequencing_platform"),
                ("method", "method"), ("effectiveSequences", "effective_sequences"),
                ("qualityControl", "quality_control"),
            )
        }),
        data=data,
        error=EvaluationError(
            error_code=_get(raw, "errorCode", "error_code") or _get(error, "errorCode", "error_code"),
            message=raw.get("message") if raw.get("message") is not None else error.get("message"),
        ),
    )
    document = public.model_dump(mode="json", by_alias=True, exclude_none=False)
    _compact_fact_locations(document['data'], _get(report, 'clinicalDocument', 'clinical_document'))
    return document


def _referenced_numbers(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list) and (key == 'evidenceRefs' or key.endswith('EvidenceRefs')):
                yield from (number for number in value if type(number) is int)
            else:
                yield from _referenced_numbers(value)
    elif isinstance(node, list):
        for row in node:
            yield from _referenced_numbers(row)


def build_frontend_evaluation_callback(payload: Any) -> dict[str, Any]:
    """Publish the current display body once and retain the callback envelope."""
    if isinstance(payload, Mapping):
        data = payload.get('data')
        if (
            payload.get('schemaVersion') == 'evaluate.v2.0'
            and payload.get('type') == 'evaluate'
            and isinstance(data, Mapping)
            and data.get('version') in ('assessment.v1', ORDERED_ASSESSMENT_VERSION)
        ):
            payload = {
                **payload,
                'data': {
                    'assessmentPresentation': assessment_view_for_rendering(data),
                    'appendices': data.get('appendices') or {},
                },
            }

    document = build_evaluation_callback(payload)
    data = document['data']
    view = data.get('assessmentPresentation')
    if document['status'] == 'failed':
        document['data'] = {}
    elif isinstance(view, dict) and view.get('version') == 'assessment.v1':
        # Delivery preserves the complete confirmed fields and source arrays.
        # Choosing a concise table for PDF is a renderer responsibility; it
        # must not delete a reviewed conclusion or truncate the callback data.
        used = set(_referenced_numbers(view))
        references = data['appendices']['referenceLiterature']
        appendices = {'referenceLiterature': [
            {key: row[key] for key in ('number', 'title', 'year', 'doi', 'url')}
            for row in references if row['number'] in used
        ]}
        document['data'] = order_assessment_for_delivery(view, appendices)
    else:
        return document  # older renderable reports and partial fallback results

    sample = document['sample']
    document['sample'] = {key: sample[key] for key in ('sampleId', 'sampleDate', 'reportDate', 'method')}
    return document
