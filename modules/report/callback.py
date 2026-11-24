"""Map internal report facts to the fixed public callback contract."""

from __future__ import annotations

import re
from typing import Any, TypeVar

from modules.report.artifacts import active_final_report_id
from modules.report.clinical import (
    disease_binding_aliases,
    is_hospital_common_examination,
)
from modules.report.schemas import CallbackPayload, FixedReportCallback, ClinicalReportDocument, ClinicalReportBlock, DiseaseRisksSection
from modules.report.clinical_report import callback_sections
from modules.report.terminology import (
    contains_promotional_absolute,
    patient_disease_suggestion,
    patient_facing_name,
    patient_facing_text,
    patient_health_topic_domain,
    patient_health_topic_is_generic,
    patient_topic_key,
    patient_topics_overlap,
)




def _fixed_callback_body(
    payload: CallbackPayload | dict[str, Any],
) -> dict[str, Any]:
    """Map internal report facts to the frontend's fixed terminal contract."""
    raw = (
        payload.model_dump(mode="json", by_alias=True, exclude_none=False)
        if hasattr(payload, "model_dump")
        else dict(payload)
    )
    report = _mapping(raw.get("report"))
    if report.get("clinicalDocument"):
        document = getattr(getattr(payload, 'report', None), 'clinical_document', None)
        return _clinical_callback_body(raw, report, document=document)
    references, reference_numbers, reference_metadata = _fixed_evidence(report)
    health = _mapping(report.get("healthManagement"))
    abnormal = _mapping(report.get("abnormalMicrobes"))
    subject = _mapping(report.get("subject"))
    sample = _mapping(report.get("sample"))
    health_assessment = _patient_health_assessment(
        _mapping(report.get("healthAssessment"))
    )
    findings = _fixed_findings(
        report,
        reference_numbers=reference_numbers,
        reference_categories=reference_metadata,
    )
    public_conclusion = _public_summary_conclusion(report, findings)
    basis_refs = _unique(
        ref
        for finding in findings
        for field in (
            "interpretation_evidence_refs",
            "recommendation_evidence_refs",
            "evidence_refs",
        )
        for ref in _ref_numbers(finding.get(field))
    )
    summary_refs = _public_ref_numbers(
        _summary_ref_numbers(report, summary=public_conclusion or ""),
        reference_numbers,
    )
    health_recommendations = _health_recommendations(
        health,
        reference_numbers,
        report=report,
    )
    recommended_examinations = _health_recommended_examinations(
        health,
        reference_numbers,
        report=report,
        public_findings=findings,
    )
    published_reference_numbers = {
        *basis_refs,
        *summary_refs,
        *(
            number
            for recommendation in health_recommendations
            for number in recommendation.get("evidence_refs", [])
        ),
        *(
            number
            for examination in recommended_examinations
            for number in examination.get("evidence_refs", [])
        ),
    }
    references = [
        reference
        for reference in references
        if reference.get("number") in published_reference_numbers
    ]
    references = _attach_patient_related_facts(
        references,
        findings=findings,
        health_assessment=health_assessment,
        health_recommendations=health_recommendations,
        recommended_examinations=recommended_examinations,
        reference_metadata=reference_metadata,
        disease_source_facts=_source_disease_patient_facts(report),
    )
    status = str(raw.get("status") or "failed")
    fixed = FixedReportCallback.model_validate(
        {
            "userId": str(raw.get("userId") or ""),
            "sessionId": str(raw.get("sessionId") or ""),
            "finalReportId": active_final_report_id(),
            "type": "evaluate",
            "status": status,
            "generated_at": _calendar_date_text(
                raw.get("generatedAt") or raw.get("generated_at")
            ),
            "subject": {
                "name": _text(subject.get("name")),
                "gender": _text(subject.get("sex")),
                "age": _age_text(subject.get("age")),
                "report_date": _date_text(sample.get("reportDate")),
            },
            "sample": {
                "sample_id": _text(sample.get("sampleId")),
                "sample_type": _text(sample.get("sampleType")),
                "test_item": _text(sample.get("testItem")),
                "sample_date": _date_text(sample.get("sampleDate")),
                "report_date": _date_text(sample.get("reportDate")),
                "sequencing_platform": _text(sample.get("sequencingPlatform")),
            },
            "health_assessment": health_assessment,
            "diagnosis_summary": {
                "conclusion": public_conclusion,
                "clinical_context": _clinical_context(
                    abnormal,
                    health,
                    public_findings=findings,
                ),
                # Health-management attention is not a disease probability.
                # Keep the fixed field but do not convert an overall attention
                # label into low/medium/high patient risk.
                "risk": None,
                "evidence_refs": summary_refs,
            },
            "diagnostic_basis": {
                "abnormal_findings": findings,
                "evidence_refs": basis_refs,
            },
            "diagnostic_advice": {
                "report_interpretation": _report_interpretation(abnormal, health),
                "Plan": {
                    "未来1-2周": None,
                    "未来1个月": None,
                    "未来2-3个月": None,
                    "未来6-12个月": None,
                },
                "health_recommendations": health_recommendations,
                "recommended_examinations": recommended_examinations,
                "not_recommended_behaviors": {},
                "medical_alerts": [],
            },
            "evidence": references,
            "partial_reasons": list(raw.get("partialReasons") or []),
            "error": {
                "error_code": _text(raw.get("errorCode")),
                "message": _text(raw.get("message")),
            },
        }
    )
    # ``exclude_none`` must stay false: null-valued keys are part of the
    # frontend contract and are rendered as empty states.
    body = fixed.model_dump(mode="json", by_alias=True, exclude_none=False)
    return _renumber_public_evidence(body)


def _clinical_callback_body(raw: dict[str, Any], report: dict[str, Any], *, document=None) -> dict[str, Any]:
    # Preserve request-private review receipts from a live work product. A
    # dictionary/replayed public DTO cannot manufacture those approvals.
    if not isinstance(document, ClinicalReportDocument):
        document = ClinicalReportDocument.model_validate(report["clinicalDocument"])
    if not any(block.code == 'overview' for block in document.sections):
        # The new assessment has its own chapter codes. Adapt only the
        # established downstream DTO's summary slot; do not regenerate the
        # removed intervention chapters or mutate the evaluation work product.
        overview = next((block for block in document.sections
            if block.code == 'eval:["healthOverview","overallSummary","text"]'), None)
        document.sections.append(overview.model_copy(update={'code': 'overview'}) if overview is not None
            else ClinicalReportBlock(code='overview',
                text=report.get('executiveSummary') or '现有来源记录已保留，综合评估文字尚未完整生成。',
                state='insufficient'))
    references, _, _ = _fixed_evidence(report)
    subject = _mapping(report.get("subject"))
    sample = _mapping(report.get("sample"))
    sections = callback_sections(document, DiseaseRisksSection.model_validate(report.get("diseaseRisks") or {}), references)
    body = FixedReportCallback.model_validate({
        "userId": str(raw.get("userId") or ""), "sessionId": str(raw.get("sessionId") or ""),
        "finalReportId": active_final_report_id(), "type": "evaluate",
        "status": raw.get("status") or "failed",
        "generated_at": _calendar_date_text(raw.get("generatedAt") or raw.get("generated_at")),
        "subject": {"name": _text(subject.get("name")), "gender": _text(subject.get("sex")),
            "age": _age_text(subject.get("age")), "report_date": _date_text(sample.get("reportDate"))},
        "sample": {"sample_id": sample.get("sampleId"), "sample_type": sample.get("sampleType"),
            "test_item": sample.get("testItem"), "sample_date": sample.get("sampleDate"),
            "report_date": sample.get("reportDate"), "sequencing_platform": sample.get("sequencingPlatform")},
        "healthAssessment": report.get("healthAssessment") or {},
        "partial_reasons": raw.get("partialReasons") or [], **sections,
    }).model_dump(mode="json", by_alias=True, exclude_none=False)
    return _renumber_public_evidence(body)


def _renumber_public_evidence(body: dict[str, Any]) -> dict[str, Any]:
    """Renumber the filtered public bibliography and every structured ref."""

    references = [
        reference
        for value in _items(body.get("evidence"))
        if (reference := _mapping(value))
        and isinstance(reference.get("number"), int)
    ]
    number_map = {
        int(reference["number"]): index
        for index, reference in enumerate(references, start=1)
    }
    for reference in references:
        reference["number"] = number_map[int(reference["number"])]
    body["evidence"] = references

    def remap(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key == "evidenceRefs" or key.endswith("evidence_refs")
                ) and isinstance(child, list):
                    value[key] = _unique(
                        number_map[number]
                        for number in _ref_numbers(child)
                        if number in number_map
                    )
                elif key != "evidence":
                    remap(child)
        elif isinstance(value, list):
            for child in value:
                remap(child)

    remap(body)
    return body


def _fixed_findings(
    report: dict[str, Any],
    *,
    reference_numbers: set[int],
    reference_categories: dict[int, Any],
) -> list[dict[str, Any]]:
    abnormal = _mapping(report.get("abnormalMicrobes"))
    diseases = _mapping(report.get("diseaseRisks"))
    microbe_findings: list[tuple[int, dict[str, Any]]] = []
    source_microbe_findings: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    disease_findings: list[tuple[int, dict[str, Any]]] = []
    for index, raw in enumerate(_items(abnormal.get("items"))):
        item = _mapping(raw)
        if not item:
            continue
        finding = _microbe_finding(
            item,
            reference_numbers=reference_numbers,
            reference_categories=reference_categories,
        )
        if finding is None:
            continue
        source_microbe_findings.append((index, finding, item))
        if _items(finding.get("related_diseases_or_adverse_effects")):
            microbe_findings.append((index, finding))
    # Prefer evidence-bound rows, but do not turn a successfully parsed source
    # abnormality list into an entirely empty public module.  This fallback is
    # reached only when no organism has a publishable health relationship.
    if not microbe_findings:
        microbe_findings = []
        for index, finding, item in source_microbe_findings:
            finding = dict(finding)
            finding["biological_mechanism"] = _source_only_microbe_interpretation(
                finding=_mapping(item.get("sourceFinding")),
                category=_text(item.get("category")),
            )
            microbe_findings.append((index, finding))
    for index, raw in enumerate(_items(diseases.get("items"))):
        item = _mapping(raw)
        if not item:
            continue
        # A condition already supported by the supplied clinical examination
        # belongs to the overall health assessment, abnormal indicators and
        # health summary.  Publishing it again as a microbiome-associated
        # disease risk confuses an established management fact with a model
        # association and also makes completed tests look newly recommended.
        if _disease_is_clinical_management_fact(item):
            continue
        finding = _disease_finding(
            item,
            reference_numbers=reference_numbers,
            reference_categories=reference_categories,
        )
        if finding is not None:
            disease_findings.append((index, finding))
    # The public order is part of the JSON contract.  Rank here, after all
    # evidence and patient-result gates have finished, so HTML and every other
    # consumer receive the same deterministic order without presentation-time
    # inference.  Original position is the final stable tie breaker.
    microbe_findings.sort(key=lambda pair: _finding_sort_key(pair[1], pair[0]))
    disease_findings.sort(key=lambda pair: _finding_sort_key(pair[1], pair[0]))
    # Limit only the public disease section, after validation and ranking.
    # Keep all internal facts for scoring and leave microbe coverage unchanged.
    disease_findings = disease_findings[:5]
    _vary_repeated_disease_suggestion_openings(disease_findings)
    return [
        *(finding for _index, finding in microbe_findings),
        *(finding for _index, finding in disease_findings),
    ]


def _vary_repeated_disease_suggestion_openings(
    disease_findings: list[tuple[int, dict[str, Any]]],
) -> None:
    """Avoid a mechanically repeated sentence opening across disease rows.

    Only the introductory wording changes; the disease, action and evidence
    meaning generated for the row remain untouched.
    """

    replacements = {
        "可通过": ("建议通过", "后续可通过"),
        "可关注": ("建议关注", "后续可关注"),
        "可结合": ("建议结合", "后续可结合"),
        "可留意": ("建议留意", "后续可留意"),
    }
    counts: dict[str, int] = {}
    for _index, finding in disease_findings:
        tips = _mapping(finding.get("tips"))
        suggestion = _text(tips.get("special_reminder"))
        if not suggestion:
            continue
        opening = next(
            (prefix for prefix in replacements if suggestion.startswith(prefix)),
            None,
        )
        if opening is None:
            continue
        occurrence = counts.get(opening, 0)
        counts[opening] = occurrence + 1
        if occurrence < 2:
            continue
        replacement = replacements[opening][(occurrence - 2) % 2]
        tips["special_reminder"] = replacement + suggestion[len(opening) :]
        finding["tips"] = tips


def _finding_sort_key(finding: dict[str, Any], source_index: int) -> tuple[Any, ...]:
    priority_rank = {
        "重点关注": 0,
        "建议关注": 1,
        "持续关注": 2,
        "一般了解": 3,
    }.get(_text(finding.get("priority")) or "", 4)
    evidence_rank = {
        "证据较明确": 0,
        "可能相关": 1,
        "中等证据": 1,
        "初步证据": 2,
        "证据不足": 3,
        "当前不适用": 4,
    }.get(_text(finding.get("evidence_level")) or "", 5)
    abnormal_result_rank = 1
    if finding.get("finding_type") == "disease":
        statuses = " ".join(
            _text(item.get("status")) or ""
            for raw in _items(finding.get("indicators"))
            if (item := _mapping(raw))
        )
        if any(marker in statuses for marker in _ABNORMAL_RESULT_MARKERS) and not any(
            marker in statuses for marker in _NON_ABNORMAL_RESULT_MARKERS
        ):
            abnormal_result_rank = 0
    association_count = len(
        _items(finding.get("related_diseases_or_adverse_effects"))
    )
    recommendation_coverage = bool(
        _items(finding.get("recommendation_evidence_refs"))
    )
    return (
        priority_rank,
        abnormal_result_rank,
        evidence_rank,
        -association_count,
        -int(recommendation_coverage),
        -len(_items(finding.get("evidence_refs"))),
        source_index,
    )


def _microbe_finding(
    item: dict[str, Any],
    *,
    reference_numbers: set[int],
    reference_categories: dict[int, Any],
) -> dict[str, Any] | None:
    finding = _mapping(item.get("sourceFinding"))
    model_interpretation_generated = (
        "modelInterpretationGenerated" not in item
        or item.get("modelInterpretationGenerated") is True
    )
    name = patient_facing_name(_text(item.get("name")))
    scientific_name = _text(item.get("scientificName"))
    category = _text(item.get("category"))
    problem = name
    if scientific_name and scientific_name.casefold() != (name or "").casefold():
        problem = f"{name or scientific_name} ({scientific_name})"
    all_risks = [
        risk
        for value in _items(item.get("diseaseAndRisks"))
        if (risk := _mapping(value))
    ]
    risks: list[dict[str, Any]] = []
    for source_risk in all_risks:
        risk = dict(source_risk)
        risk["evidenceRefs"] = [
            number
            for number in _public_ref_numbers(
                _ref_numbers(risk.get("evidenceRefs")), reference_numbers
            )
            if not _reference_depends_on_unmeasured_subtype(
                _mapping(reference_categories.get(number))
            )
        ]
        if _text(risk.get("sampleAlignment")) in {None, "", "unclear"}:
            inferred_sample = _risk_sample_alignment_from_evidence(
                risk,
                reference_categories,
            )
            if inferred_sample:
                risk["sampleAlignment"] = inferred_sample
        if _microbe_risk_is_patient_visible(
            finding=finding,
            risk=risk,
            reference_numbers=reference_numbers,
            reference_categories=reference_categories,
            category=category,
        ):
            risks.append(risk)
        if len(risks) >= 3:
            break
    medical_interpretation = _text(item.get("medicalInterpretation"))
    if not model_interpretation_generated:
        # Evidence interpretation is itself model-assisted and independently
        # grounded to the cited human record. When the optional final prose
        # pass misses one row, use only those validated relationship claims
        # instead of hiding the whole organism. The public-scope gate below
        # removes cross-site outcomes and action advice from this column.
        relationship_fallback = " ".join(
            description
            for risk in risks
            if (description := _text(risk.get("description")))
        ) or None
        # A failed optional prose pass must not erase an independently
        # evidence-bound organism interpretation.  Prefer the validated
        # relationship wording when available; otherwise keep the existing
        # interpretation and let the site/applicability and reference gates
        # below decide whether it is publishable.
        if relationship_fallback:
            medical_interpretation = relationship_fallback
    if medical_interpretation and any(
        marker in medical_interpretation
        for marker in ("原报告模型指数", "模型指数", "模型分值")
    ):
        medical_interpretation = None
    if medical_interpretation:
        hidden_risk_names = {
            risk_name
            for risk in all_risks
            if risk not in risks
            if (risk_name := _text(risk.get("name")))
        }
        medical_interpretation = _remove_hidden_microbe_associations(
            medical_interpretation,
            hidden_risk_names=hidden_risk_names,
        )
    if medical_interpretation:
        medical_interpretation = _remove_duplicate_microbe_associations(
            medical_interpretation,
            risks=risks,
        )
    medical_interpretation = _public_microbe_interpretation(
        medical_interpretation,
        clinical_role=_text(item.get("clinicalRole")),
        risks=risks,
        category=category,
        finding=finding,
    )
    # ``medicalInterpretation`` is finalized after the validated relations are
    # attached and already contains the professionally combined role/outcome
    # text.  Appending every risk description again at callback mapping time
    # duplicated the same sentence two or three times in the public JSON.
    # Keep risk descriptions as a fallback only for older/internal payloads
    # that do not yet provide a medical interpretation.
    association_refs = _public_ref_numbers(
        [
            *(
                number
                for risk in risks
                for number in _ref_numbers(risk.get("evidenceRefs"))
            ),
        ],
        reference_numbers,
    )
    fallback_candidate_refs = _public_ref_numbers(
        [
            *association_refs,
            *_ref_numbers(item.get("interpretationEvidenceRefs")),
        ],
        reference_numbers,
    )
    if (
        not model_interpretation_generated
        and medical_interpretation
        and not _microbe_refs_support_model_assisted_fallback(
            fallback_candidate_refs,
            reference_categories,
        )
    ):
        # Do not publish an arbitrary deterministic/source-only sentence. The
        # degraded path is reserved for a directly aligned human relationship
        # claim; specimen applicability is explained in the public wording
        # instead of deleting an otherwise valid organism record.
        medical_interpretation = None
    interpretation_refs = _public_ref_numbers(
        _ref_numbers(item.get("interpretationEvidenceRefs")),
        reference_numbers,
    )
    if not interpretation_refs and medical_interpretation and association_refs:
        # A direct microbe-outcome study may support both the relationship row
        # and its concise medical explanation. Requiring a second, unrelated
        # organism-overview citation caused otherwise complete rows to vanish.
        interpretation_refs = list(association_refs)
    if not interpretation_refs and medical_interpretation and not association_refs:
        interpretation_refs = _public_ref_numbers(
            _ref_numbers(item.get("evidenceRefs")),
            reference_numbers,
        )
    refs = _unique([*interpretation_refs, *association_refs])
    if not medical_interpretation or not refs:
        # A relation may be rejected because its specimen site or detected
        # direction is not applicable to the current stool result even though
        # the retrieved record itself has a citation.  In that case, publishing
        # either an empty cell or the unrelated citation is misleading.  Keep
        # the source abnormality visible with a measured-result-only boundary.
        medical_interpretation = _source_only_microbe_interpretation(
            finding=finding,
            category=category,
        )
        interpretation_refs = []
        refs = list(association_refs)
    public_risks: list[dict[str, Any]] = []
    for risk in risks:
        public_risks.append(
            {
                "name": patient_facing_name(_text(risk.get("name"))),
                # The adjacent medical-interpretation column owns the prose.
                # Keep this compatibility field fixed but empty so the risk
                # column contains names only.
                "description": None,
                "relation_type": (
                    _text(risk.get("relationType"))
                    or "epidemiologic_association"
                ),
                "evidence_refs": _public_ref_numbers(
                    _ref_numbers(risk.get("evidenceRefs")), reference_numbers
                ),
            }
        )
    # The result and evidence columns already publish the measured value and
    # literature sources.  The medical-interpretation column must still
    # explain the health meaning behind every retained relation.  Prefer the
    # relation-scoped model summary when it passes the final whitelist; fall
    # back to a deterministic one-sentence relation summary only when the
    # model text is missing or repeats source/result boilerplate.
    model_public_mechanism = (
        medical_interpretation
        if medical_interpretation
        and _relation_scoped_microbe_interpretation(
            medical_interpretation,
            risks=risks,
        )
        and _microbe_interpretation_has_explanatory_detail(
            medical_interpretation,
            risks=risks,
            microbe_name=name,
            scientific_name=scientific_name,
        )
        else None
    )
    evidence_public_mechanism = _evidence_bound_microbe_interpretation(
        risks,
        microbe_name=name,
        scientific_name=scientific_name,
        reference_metadata=reference_categories,
    )
    public_mechanism = None
    if risks and model_public_mechanism:
        public_mechanism = model_public_mechanism
    elif risks and evidence_public_mechanism:
        public_mechanism = evidence_public_mechanism
        # This fallback is built from the retained relationship descriptions
        # and their bound claims.  Do not leave an unrelated organism-function
        # citation in the public "medical interpretation evidence" field.
        interpretation_refs = list(association_refs)
    elif risks:
        public_mechanism = _concise_microbe_risk_conclusion(
            risks,
            reference_metadata=reference_categories,
        )
        interpretation_refs = list(association_refs)
    refs = _unique([*interpretation_refs, *association_refs])
    # The source abnormality and its measured value are primary report facts.
    # Evidence enrichment may be incomplete or temporarily unavailable, but it
    # must never change the number of source findings returned to the caller.
    # Keep nullable enrichment fields and report the partial state separately.
    return {
        "finding_type": "microbe",
        "item_id": _text(item.get("microbeId")),
        "problem": patient_facing_text(problem),
        "priority": _microbe_attention_priority(
            category=category,
            finding=finding,
            risks=risks,
        ),
        "indicators": [
            {
                # The organism name is already published in ``problem``.
                # Keeping it here made the "current result" column repeat the
                # same name in every row.
                "name": None,
                "value": _finding_value(finding),
                "status": _text(finding.get("status")),
            }
        ],
        "related_diseases_or_adverse_effects": public_risks,
        "biological_mechanism": patient_facing_text(public_mechanism),
        "recommendations": [],
        "tips": {
            "special_reminder": (
                None
                if risks
                else _microbe_relation_availability_note(
                    finding=finding,
                    category=category,
                )
            ),
            "practical_goal": (
                None
                if refs
                else _microbe_evidence_availability_note(
                    finding=finding,
                    category=category,
                )
            ),
        },
        "interpretation_evidence_refs": interpretation_refs,
        "recommendation_evidence_refs": [],
        "evidence_refs": refs,
        # Grade the concrete relationship evidence first. A general organism
        # interpretation must not inflate a disease association.
        "evidence_level": _evidence_level(
            association_refs or refs,
            reference_categories,
        ),
    }


def _microbe_relation_availability_note(
    *,
    finding: dict[str, Any],
    category: str | None,
) -> str:
    """Explain a genuinely empty relation cell without inventing a disease."""

    status = (_text(finding.get("status")) or "").casefold()
    low_result = any(
        marker in status
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "未检出", "阴性")
    )
    if low_result and category in {
        "pathogenic_signal",
        "pathogenic_abnormal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }:
        return "低丰度本身不提示相关感染或不良影响。"
    if category == "beneficial_abnormal" and low_result:
        return "尚无足够人体证据将本次低丰度对应到具体疾病或不良影响。"
    return "当前没有与本次粪便结果直接匹配的疾病或不良影响证据。"




def _microbe_evidence_availability_note(
    *,
    finding: dict[str, Any],
    category: str | None,
) -> str:
    """State what the bounded search did not establish, not "insufficient"."""

    direction = _microbe_finding_direction(finding)
    if direction == "low" and category in {
        "pathogenic_signal",
        "pathogenic_abnormal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }:
        return "本次低丰度不构成感染证据，因此未生成疾病关联结论。"
    if direction == "low":
        return "未检索到与本次低丰度方向直接匹配的人体粪便证据。"
    if direction == "high":
        return "未检索到与本次高丰度方向直接匹配的人体粪便证据。"
    if direction == "detected":
        return "未检索到与本次检出结果直接匹配的人体粪便或临床证据。"
    return "未检索到与本次检测方向直接匹配的人体粪便证据。"


def _microbe_attention_priority(
    *,
    category: str | None,
    finding: dict[str, Any],
    risks: list[dict[str, Any]],
) -> str:
    """Rank the detected direction, never the organism name by itself."""

    status = " ".join(
        filter(
            None,
            (
                _text(finding.get("status")),
                _text(finding.get("direction")),
                _text(finding.get("sourceResult")),
            ),
        )
    ).casefold()
    harmful_signal = category in {
        "pathogenic_signal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }
    adverse_direction = any(
        marker in status
        for marker in (
            "严重异常",
            "显著升高",
            "偏高",
            "升高",
            "超标",
            "阳性",
            "检出",
            "high",
            "positive",
            "detected",
        )
    )
    directly_applicable = any(
        _text(risk.get("patientApplicability")) == "direct"
        and _text(risk.get("sampleAlignment")) in {"stool", "mucosa"}
        for risk in risks
    )
    if harmful_signal and adverse_direction and directly_applicable:
        return "重点关注"
    if adverse_direction or directly_applicable or risks:
        return "建议关注"
    return "持续关注"


def _source_only_microbe_interpretation(
    *,
    finding: dict[str, Any],
    category: str | None,
) -> str:
    status = (_text(finding.get("status")) or "").casefold()
    low_result = any(
        marker in status
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "未检出", "阴性")
    )
    pathogenic = category in {
        "pathogenic_signal",
        "pathogenic_abnormal",
        "fungi_pathogenic_signal",
    }
    opportunistic = category == "opportunistic_signal"
    if opportunistic and low_result:
        return (
            "条件致病菌的低丰度仅反映本次菌群组成，"
            "本身不提示机会性感染或相关疾病风险。"
        )
    if pathogenic and low_result:
        return (
            "病原相关菌的低丰度不构成活动性感染证据，"
            "因此当前不据此形成具体疾病或不良影响结论。"
        )
    if opportunistic:
        return (
            "条件致病菌在粪便中检出可能反映肠道定植；"
            "现有证据尚不足以对应具体疾病，需结合症状和临床检查判断。"
        )
    if pathogenic:
        return (
            "该结果表示检测到病原相关微生物信号；仅凭粪便菌群结果"
            "不能确认活动性感染，需结合症状和临床病原学检查判断。"
        )
    if category == "beneficial_abnormal" and low_result:
        return (
            "该菌属于常见肠道共生菌，但单次低丰度结果尚不足以判断"
            "具体健康影响。"
        )
    return "该菌群变化目前缺少可直接用于个体解释的人体证据，暂不作疾病风险推断。"


def _microbe_risk_is_patient_visible(
    *,
    finding: dict[str, Any],
    risk: dict[str, Any],
    reference_numbers: set[int],
    reference_categories: dict[int, Any],
    category: str | None,
) -> bool:
    if not _text(risk.get("name")):
        return False
    patient_applicability = _text(risk.get("patientApplicability"))
    # Direct and contextual human gut associations are both patient-visible.
    # Contextual evidence describes a population-level health direction rather
    # than an individual disease probability. Cross-site and non-applicable
    # evidence remains auditable but is not published as a stool association.
    sample_alignment = _text(risk.get("sampleAlignment"))
    if (
        patient_applicability not in {"direct", "contextual"}
        or sample_alignment not in {"stool", "mucosa"}
    ):
        return False
    if not _public_ref_numbers(
        _ref_numbers(risk.get("evidenceRefs")), reference_numbers
    ):
        return False
    relation_type = _text(risk.get("relationType")) or "epidemiologic_association"
    if relation_type == "environmental_or_dietary":
        return False
    status = (_text(finding.get("status")) or "").casefold()
    low_result = any(
        marker in status
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "未检出", "阴性")
    )
    if relation_type in {"established_pathogen", "opportunistic"} and low_result:
        return False
    if low_result and category in {
        "pathogenic_signal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }:
        # A paper showing that an organism can cause an infection does not
        # support turning a *low stool abundance* into that infection risk.
        # Keep only a true human abundance study whose interpreted direction
        # is explicitly decreased and matches the source result.
        evidence_refs = _public_ref_numbers(
            _ref_numbers(risk.get("evidenceRefs")), reference_numbers
        )
        return relation_type == "epidemiologic_association" and any(
            _text(_mapping(reference_categories.get(number)).get("direction"))
            == "decreased"
            for number in evidence_refs
        )
    return True


def _risk_sample_alignment_from_evidence(
    risk: dict[str, Any],
    metadata: dict[int, Any],
) -> str | None:
    """Recover only an explicitly named gut specimen from cited source text."""

    source = " ".join(
        part
        for number in _ref_numbers(risk.get("evidenceRefs"))
        for item in [_mapping(metadata.get(number))]
        for part in (
            _text(item.get("title")),
            _text(item.get("summary")),
            _text(item.get("claim")),
        )
        if part
    ).casefold()
    if any(
        marker in source
        for marker in (
            "mucosal biopsy",
            "colonic biopsy",
            "intestinal biopsy",
            "肠黏膜活检",
            "结肠活检",
        )
    ):
        return "mucosa"
    if any(
        marker in source
        for marker in (
            "stool sample",
            "stool specimen",
            "stool culture",
            "stool microbiome",
            "fecal sample",
            "fecal specimen",
            "fecal culture",
            "fecal microbiome",
            "fecal proteolytic",
            "faecal sample",
            "faecal specimen",
            "faecal culture",
            "faecal microbiome",
            "faecal proteolytic",
            "粪便样本",
            "粪便标本",
            "粪便培养",
            "粪便菌群",
            "粪便微生物组",
            "大便样本",
            "大便标本",
        )
    ):
        return "stool"
    return None


def _microbe_refs_support_model_assisted_fallback(
    numbers: list[int],
    metadata: dict[int, Any],
) -> bool:
    """Allow degraded prose only when the cited evidence was fully screened."""

    for number in numbers:
        item = _mapping(metadata.get(number))
        if not item:
            continue
        if _text(item.get("target_alignment")) != "direct":
            continue
        if _text(item.get("patient_applicability")) == "not_applicable":
            continue
        if not _text(item.get("claim")):
            continue
        return True
    return False


def _remove_duplicate_microbe_associations(
    value: str,
    *,
    risks: list[dict[str, Any]],
) -> str | None:
    del risks
    sentences = [
        sentence.strip()
        for sentence in value.replace("；", "。").split("。")
        if sentence.strip()
    ]
    # The former implementation removed every sentence containing a disease
    # name plus "association" wording. That erased the evidence-bearing core
    # of valid AI summaries and could leave only "this organism was found".
    # Deduplicate exact sentences while preserving their medical meaning.
    kept = list(dict.fromkeys(sentences))
    if not kept:
        return None
    return "。".join(kept) + "。"


def _remove_hidden_microbe_associations(
    value: str,
    *,
    hidden_risk_names: set[str],
) -> str | None:
    """Remove relations rejected by the stool/mucosa applicability gate."""

    if not hidden_risk_names:
        return value
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？!?])\s*", value)
        if sentence.strip()
    ]
    kept = [
        sentence
        for sentence in sentences
        if not any(name in sentence for name in hidden_risk_names)
    ]
    return "".join(kept).strip() or None


def _public_microbe_interpretation(
    value: str | None,
    *,
    clinical_role: str | None,
    risks: list[dict[str, Any]],
    category: str | None,
    finding: dict[str, Any],
) -> str | None:
    """Keep a microbe explanation within the columns that remain public.

    The prose model runs before the final relation gate.  It may therefore
    mention an outcome that is later rejected, or repeat the result/research
    provenance already shown in adjacent columns.  Keep a model explanation
    only when it covers every retained outcome and remains within the public
    wording contract.  The caller supplies the deterministic relation fallback.
    """

    del finding
    for candidate in (value, clinical_role):
        cleaned = _strip_microbe_boundary_and_action_sentences(candidate)
        if not cleaned:
            continue
        if _generic_microbe_role_text(cleaned):
            continue
        if not risks and _microbe_text_names_specific_outcome(cleaned):
            continue
        if risks and not _relation_scoped_microbe_interpretation(
            cleaned,
            risks=risks,
        ):
            continue
        return cleaned
    return None if risks else _category_scoped_microbe_role(category)


def _relation_scoped_microbe_interpretation(
    value: str,
    *,
    risks: list[dict[str, Any]],
) -> bool:
    """Accept a concise medical explanation tied to every retained outcome."""

    risk_names = [
        name
        for raw in risks[:3]
        if (name := patient_facing_name(_text(raw.get("name"))))
    ]
    if not risk_names or not all(name in value for name in risk_names):
        return False
    residual = value
    for name in sorted(risk_names, key=len, reverse=True):
        residual = residual.replace(name, "")
    if re.search(
        r"[\u4e00-\u9fff]{1,12}(?:癌|感染|结石|中毒|综合征|"
        r"肠炎|菌血症|败血症|脓毒症|疾病)",
        residual,
    ):
        # The model paragraph still names an outcome that did not survive the
        # final relation gate.  Falling back to the structured risk list is
        # safer than publishing cross-disease prose.
        return False
    # Two concise sentences may be needed now that the separate relationship
    # column is no longer rendered and its useful meaning is carried here.
    if len(value) > 220 or any(
        marker in value
        for marker in (
            "本次",
            "检测",
            "检出",
            "丰度",
            "人体研究",
            "人体粪便研究",
            "临床证据",
            "文献",
            "循证依据",
            "不能用于判断",
            "需由医生确认",
        )
    ):
        return False
    return any(
        marker in value
        for marker in (
            "提示",
            "风险",
            "影响",
            "可能引起",
            "相关",
            "活动度",
            "严重程度",
            "炎症",
            "屏障",
            "代谢",
        )
    )


def _generic_microbe_role_text(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if any(
        marker in compact
        for marker in (
            "可在特定人体部位定植",
            "在特定人体部位定植",
            "在特定宿主或部位中可表现机会致病性",
        )
    ):
        return True
    return bool(
        re.search(
            r"人体(?:资料|研究)支持.+(?:相应临床情境中具有病原学意义|"
            r"特定宿主或部位中可表现机会致病性|属于常见肠道共生菌|"
            r"可在特定人体部位定植)",
            compact,
        )
    )


def _strip_microbe_boundary_and_action_sentences(value: str | None) -> str | None:
    if not value:
        return None
    excluded = (
        "不等同",
        "不等于",
        "不代表",
        "不能确认",
        "不能据此",
        "不能单独",
        "无需采取",
        "不需采取",
        "维持良好",
        "建议保持",
        "建议调整",
        "建议关注",
        "建议复查",
        "留意后续",
        "后续复查",
        "前往就医",
        "前往专科",
        "就医评估",
        "饮食调整",
        "运动管理",
        "个人卫生",
        "食品安全习惯",
        "定期进行",
        "保持健康生活方式",
        "保持健康的生活方式",
    )
    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。；;!?！？]+", value)
        if sentence.strip()
    ]
    kept = [
        sentence
        for sentence in sentences
        if not any(marker in sentence for marker in excluded)
    ]
    return "。".join(dict.fromkeys(kept)) + "。" if kept else None


def _microbe_text_names_specific_outcome(value: str) -> bool:
    return any(
        marker in value
        for marker in (
            "癌",
            "肿瘤",
            "综合征",
            "腹泻",
            "结肠炎",
            "肠炎",
            "肺部感染",
            "菌血症",
            "败血症",
            "脓毒症",
            "脑膜炎",
            "心内膜炎",
            "气性坏疽",
            "结石",
            "肥胖",
            "糖尿病",
            "肾脏疾病",
            "患病概率",
            "疾病风险",
            "相关风险",
            "风险中起作用",
        )
    )


def _category_scoped_microbe_role(category: str | None) -> str | None:
    del category
    # A category label alone is not evidence for an individualized medical
    # explanation.  The measured-result fallback below explains the boundary
    # without manufacturing a generic organism role or repeating one template.
    return None


_SUBTYPE_ONLY_REFERENCE_MARKERS = (
    "enterotoxigenic",
    "toxigenic strain",
    "toxigenic strains",
    "toxin-producing",
    "toxin producing",
    "bft gene",
    "bft1",
    "bft2",
    "bacteroides fragilis toxin",
    "产肠毒素",
    "产毒菌株",
    "产毒株",
    "产毒型",
    "毒素基因",
)


def _reference_depends_on_unmeasured_subtype(item: dict[str, Any]) -> bool:
    """Return true when a generic target cites only a strain/toxin relation."""

    if not item:
        return False
    source = " ".join(
        value.casefold()
        for value in (
            _text(item.get("title")),
            _text(item.get("summary")),
            _text(item.get("claim")),
        )
        if value
    )
    if not any(marker in source for marker in _SUBTYPE_ONLY_REFERENCE_MARKERS):
        return False
    requested = " ".join(
        value.casefold()
        for raw_target in _items(item.get("targets"))
        if (value := _text(raw_target))
    )
    return not any(marker in requested for marker in _SUBTYPE_ONLY_REFERENCE_MARKERS)




def _concise_microbe_risk_conclusion(
    risks: list[dict[str, Any]],
    *,
    reference_metadata: dict[int, Any],
) -> str | None:
    """Summarize at most three validated outcomes in one patient sentence."""

    established: list[str] = []
    severity_related: list[str] = []
    increased_risk: list[str] = []
    associated: list[str] = []
    opportunistic: list[str] = []
    for risk in risks[:3]:
        disease = patient_facing_name(_text(risk.get("name")))
        if not disease:
            continue
        relation_type = (
            _text(risk.get("relationType"))
            or _text(risk.get("relation_type"))
            or "epidemiologic_association"
        )
        evidence_text = " ".join(
            value.casefold()
            for value in (
                _text(risk.get("description")),
                *(
                    text
                    for number in _ref_numbers(risk.get("evidenceRefs"))
                    for item in [_mapping(reference_metadata.get(number))]
                    for text in (
                        _text(item.get("title")),
                        _text(item.get("summary")),
                        _text(item.get("claim")),
                    )
                ),
            )
            if value
        )
        if relation_type == "established_pathogen":
            established.append(disease)
        elif relation_type == "opportunistic":
            opportunistic.append(disease)
        elif any(
            marker in evidence_text
            for marker in ("严重程度", "活动度", "severity", "disease activity")
        ):
            severity_related.append(disease)
        elif any(
            marker in evidence_text
            for marker in (
                "风险增加",
                "风险升高",
                "increased risk",
                "higher risk",
                "risk increase",
            )
        ):
            increased_risk.append(disease)
        else:
            associated.append(disease)

    clauses: list[str] = []
    if established:
        clauses.append(f"可能引起{_patient_list(established)}")
    if opportunistic:
        clauses.append(f"在特定情况下可能引起{_patient_list(opportunistic)}")
    if severity_related:
        clauses.append(
            f"可能与{_patient_list(severity_related)}活动度或严重程度相关"
        )
    if increased_risk and associated:
        clauses.append(
            f"提示{_risk_phrase(increased_risk)}可能增加，"
            f"并需关注{_risk_phrase(associated, related=True)}"
        )
    elif increased_risk:
        clauses.append(f"提示{_risk_phrase(increased_risk)}可能增加")
    elif associated:
        clauses.append(f"提示{_risk_phrase(associated, related=True)}")
    return "；".join(clauses).rstrip("。；; ") + "。" if clauses else None


def _microbe_interpretation_has_explanatory_detail(
    value: str,
    *,
    risks: list[dict[str, Any]],
    microbe_name: str | None = None,
    scientific_name: str | None = None,
) -> bool:
    """Require more than the content of the hidden relationship-name column."""

    # Result facts and source provenance have dedicated adjacent columns.
    # Reject the whole candidate instead of deleting half a sentence and
    # publishing an ungrammatical remainder.
    if any(
        marker in value
        for marker in (
            "本次",
            "检测",
            "检出",
            "丰度",
            "人体研究",
            "人体粪便研究",
            "临床证据",
            "相关证据",
            "证据主要来自",
            "文献",
            "循证依据",
        )
    ):
        return False

    compact = re.sub(r"[\s。，,;；：:]", "", value)
    risk_names = [
        name
        for raw in risks[:3]
        if (name := patient_facing_name(_text(raw.get("name"))))
    ]
    if not risk_names or not all(name in compact for name in risk_names):
        return False
    relation_types = {
        _text(raw.get("relationType"))
        or _text(raw.get("relation_type"))
        or "epidemiologic_association"
        for raw in risks[:3]
    }
    if relation_types.issubset({"established_pathogen", "opportunistic"}) and (
        "可能引起" in compact or "病原体" in compact
    ):
        return True

    residual = compact
    for name in (microbe_name, scientific_name, *risk_names):
        if name:
            residual = residual.replace(re.sub(r"\s+", "", name), "")
    if any(
        marker in residual
        for marker in (
            "活动度",
            "严重程度",
            "生理",
            "功能",
            "代谢",
            "黏膜",
            "屏障",
            "免疫",
            "炎症",
            "定植",
            "毒素",
            "信号",
            "通路",
            "标志物",
            "草酸",
            "能量",
            "通透性",
            "发生过程",
            "临床表现",
            "症状",
        )
    ):
        return True
    residual = re.sub(
        r"(?:这种菌群变化|相关变化|菌群特征|该菌|此菌|"
        r"是一种机会致病菌|机会致病菌|病原菌|"
        r"在特定情况下|可能|与|和|及|或|并|提示|需关注|"
        r"相关风险|风险|相关|增加|升高|引起)",
        "",
        residual,
    )
    return len(re.findall(r"[\u3400-\u9fffA-Za-z]", residual)) >= 8


def _evidence_bound_microbe_interpretation(
    risks: list[dict[str, Any]],
    *,
    microbe_name: str | None,
    scientific_name: str | None,
    reference_metadata: dict[int, Any],
) -> str | None:
    """Recover a detailed interpretation from validated relation prose.

    This is used only when the optional final prose is absent or collapses to
    the hidden disease-name column.  It never invents a mechanism: every kept
    sentence comes from a retained, evidence-bound relationship description.
    Result repetition, study-provenance wording and diagnosis/action boundary
    text are removed before publication.
    """

    sentences: list[str] = []
    for risk in risks[:3]:
        values = [_text(risk.get("description"))]
        values.extend(
            _text(_mapping(reference_metadata.get(number)).get("claim"))
            for number in _ref_numbers(risk.get("evidenceRefs"))
        )
        for raw_value in values:
            value = _strip_microbe_boundary_and_action_sentences(raw_value)
            if not value:
                continue
            for sentence in re.split(r"(?<=[。！？!?])\s*", value):
                sentence = _clean_evidence_bound_microbe_sentence(
                    sentence,
                    microbe_name=microbe_name,
                    scientific_name=scientific_name,
                )
                if sentence and sentence not in sentences:
                    sentences.append(sentence)
    sentences = _prefer_more_informative_microbe_sentences(sentences)
    mechanism_candidate = _microbe_interpretation_from_evidence_features(
        risks,
        reference_metadata=reference_metadata,
    )
    if (
        mechanism_candidate
        and any(
            marker in mechanism_candidate
            for marker in ("草酸吸收", "屏障完整性", "产生毒素")
        )
        and _microbe_interpretation_has_explanatory_detail(
            mechanism_candidate,
            risks=risks,
            microbe_name=microbe_name,
            scientific_name=scientific_name,
        )
    ):
        return mechanism_candidate

    candidate = "".join(sentences)
    if len(candidate) > 220:
        candidate = "".join(sentences[:2])[:219].rstrip("，,；; ") + "。"
    if (
        candidate
        and _relation_scoped_microbe_interpretation(candidate, risks=risks)
        and _microbe_interpretation_has_explanatory_detail(
            candidate,
            risks=risks,
            microbe_name=microbe_name,
            scientific_name=scientific_name,
        )
    ):
        return candidate

    if (
        mechanism_candidate
        and _microbe_interpretation_has_explanatory_detail(
            mechanism_candidate,
            risks=risks,
            microbe_name=microbe_name,
            scientific_name=scientific_name,
        )
    ):
        return mechanism_candidate
    return None


def _prefer_more_informative_microbe_sentences(
    sentences: list[str],
) -> list[str]:
    """Drop a short evidence sentence when a longer one fully contains it.

    Evidence descriptions and their bound reference claims often express the
    same conclusion twice, with the latter adding a mechanism or clinical
    meaning.  Exact-string de-duplication is insufficient in that case and can
    publish paragraphs such as ``A相关。A相关，可能作为...。``.  Prefer the
    longer statement while retaining genuinely different outcome sentences.
    """

    normalized = [
        re.sub(r"[\s，,。；;：:!?！？]", "", sentence)
        for sentence in sentences
    ]
    kept: list[str] = []
    for index, sentence in enumerate(sentences):
        compact = normalized[index]
        if any(
            compact
            and compact != other
            and compact in other
            for other_index, other in enumerate(normalized)
            if other_index != index
        ):
            continue
        kept.append(sentence)
    return kept


def _clean_evidence_bound_microbe_sentence(
    sentence: str,
    *,
    microbe_name: str | None,
    scientific_name: str | None,
) -> str | None:
    sentence = sentence.strip()
    if not sentence:
        return None
    sentence = re.sub(
        r"^(?:本次(?:粪便|肠道|菌群)?(?:样本)?(?:检测)?"
        r"[^，,。]{0,70})[，,]",
        "",
        sentence,
    )
    sentence = re.sub(
        r"^(?:人体(?:粪便|临床|肠道)?研究(?:结果)?(?:中)?|"
        r"人体临床证据)(?:观察到|显示|提示|发现|记录)?[：:，,]?",
        "",
        sentence,
    )
    for name in (scientific_name, microbe_name):
        if name:
            sentence = sentence.replace(name, "该菌")
    sentence = re.sub(
        r"该菌(?:的)?(?:本次)?(?:检出信号|呈检出|"
        r"丰度(?:较)?(?:低|高|降低|升高)|"
        r"检出|未检出)[，,]?",
        "这种菌群变化",
        sentence,
    )
    # Effect sizes belong to the evidence card; keeping them here can be
    # mistaken for this person's disease probability.
    sentence = re.sub(r"(?:约|大约)?\d+(?:\.\d+)?%", "", sentence)
    sentence = re.sub(r"\s+", "", sentence).strip(" ，,；;")
    if not sentence:
        return None
    chinese_count = len(re.findall(r"[\u3400-\u9fff]", sentence))
    latin_count = len(re.findall(r"[A-Za-z]", sentence))
    if chinese_count < 4 or latin_count > chinese_count:
        return None
    if sentence[-1] not in "。！？!?":
        sentence += "。"
    return sentence


def _microbe_interpretation_from_evidence_features(
    risks: list[dict[str, Any]],
    *,
    reference_metadata: dict[int, Any],
) -> str | None:
    """Recover broad medical meaning explicitly present in bound evidence.

    These rules describe physiological domains rather than named organisms or
    fixed diseases. Outcome names still come only from retained relations.
    """

    risk_names = [
        name
        for raw in risks[:3]
        if (name := patient_facing_name(_text(raw.get("name"))))
    ]
    if not risk_names:
        return None
    evidence_text = " ".join(
        value.casefold()
        for risk in risks[:3]
        for number in _ref_numbers(risk.get("evidenceRefs"))
        for item in [_mapping(reference_metadata.get(number))]
        for value in (
            _text(item.get("title")),
            _text(item.get("summary")),
            _text(item.get("claim")),
        )
        if value
    )
    outcomes = _patient_list(risk_names)
    relation_types = {
        _text(raw.get("relationType"))
        or _text(raw.get("relation_type"))
        or "epidemiologic_association"
        for raw in risks[:3]
    }
    if any(
        marker in evidence_text
        for marker in (
            "metabolizes oxalate",
            "oxalate metabolism",
            "oxalate absorption",
            "urinary oxalate",
            "草酸代谢",
            "草酸吸收",
            "尿草酸",
        )
    ):
        return (
            "该菌参与肠道草酸代谢，其定植变化可能影响草酸吸收及尿草酸负荷，"
            f"提示需关注{outcomes}相关风险。"
        )
    if all(
        any(marker in evidence_text for marker in markers)
        for markers in (
            ("intestinal barrier", "barrier integrity", "肠道屏障", "屏障完整性"),
            ("immune homeostasis", "免疫稳态", "immune"),
            ("inflammation", "炎症"),
        )
    ):
        return (
            "该菌变化可能影响肠道屏障完整性、免疫稳态和炎症状态，"
            f"并与{outcomes}的活动度或严重程度相关。"
        )
    if (
        relation_types.issubset({"established_pathogen", "opportunistic"})
        and any(marker in evidence_text for marker in ("toxin", "毒素"))
        and any(marker in evidence_text for marker in ("diarrhea", "腹泻"))
    ):
        return (
            "该菌可产生毒素并引起腹泻及结肠炎，"
            f"可能导致{outcomes}。"
        )
    if any(
        marker in evidence_text
        for marker in (
            "tumor location",
            "tumour location",
            "microbial biomarker",
            "oncogene",
            "tumorigenesis",
            "tumourigenesis",
            "肿瘤部位",
            "微生物标志物",
            "肿瘤发生",
        )
    ):
        return (
            f"相关菌群特征可能涉及{outcomes}的发生过程、肿瘤部位特征或"
            "微生物标志物变化，提示需关注相应肠道健康风险。"
        )
    direction = next(
        (
            direction
            for risk in risks[:3]
            if (
                direction := _microbe_relation_evidence_direction(
                    risk,
                    reference_metadata,
                )
            )
        ),
        None,
    )
    if direction in {"low", "high"}:
        direction_text = "减少" if direction == "low" else "增多"
        return (
            f"该菌在{outcomes}相关菌群中呈{direction_text}方向，"
            f"提示这种菌群特征可能涉及{outcomes}的发生或进展。"
        )
    return None


def _risk_phrase(values: list[str], *, related: bool = False) -> str:
    unique = _unique(value for value in values if value)
    if len(unique) == 1 and unique[0].endswith(
        ("风险", "不良影响", "不良反应")
    ):
        return unique[0]
    suffix = "相关风险" if related else "风险"
    return f"{_patient_list(unique)}{suffix}"


def _patient_list(values: list[str]) -> str:
    unique = _unique(value for value in values if value)
    if len(unique) <= 1:
        return "".join(unique)
    if len(unique) == 2:
        return "和".join(unique)
    return "、".join(unique[:-1]) + "或" + unique[-1]


def _microbe_finding_direction(finding: dict[str, Any]) -> str | None:
    source = " ".join(
        value.casefold()
        for value in (
            _text(finding.get("status")),
            _text(finding.get("direction")),
            _text(finding.get("sourceResult")),
        )
        if value
    )
    if any(
        marker in source
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "depleted", "lower", "reduced")
    ):
        return "low"
    if any(
        marker in source
        for marker in ("丰度高", "偏高", "升高", "增加", "超标", "enriched", "higher", "increased")
    ):
        return "high"
    if any(marker in source for marker in ("检出", "阳性", "detected", "positive")):
        return "detected"
    return None


def _microbe_relation_evidence_direction(
    risk: dict[str, Any],
    metadata: dict[int, Any],
) -> str | None:
    source = " ".join(
        value.casefold()
        for number in _ref_numbers(risk.get("evidenceRefs"))
        for item in [_mapping(metadata.get(number))]
        for value in (
            _text(item.get("title")),
            _text(item.get("summary")),
            _text(item.get("claim")),
        )
        if value
    )
    if any(
        marker in source
        for marker in (
            "depleted",
            "lower abundance",
            "low abundance",
            "reduced abundance",
            "decreased abundance",
            "丰度降低",
            "丰度减少",
            "丰度较低",
        )
    ):
        return "low"
    if any(
        marker in source
        for marker in (
            "enriched",
            "higher abundance",
            "high abundance",
            "increased abundance",
            "elevated abundance",
            "丰度升高",
            "丰度增加",
            "丰度较高",
        )
    ):
        return "high"
    return None






def _disease_finding(
    item: dict[str, Any],
    *,
    reference_numbers: set[int],
    reference_categories: dict[int, Any],
) -> dict[str, Any] | None:
    if not _callback_disease_has_patient_microbiome_signal(item):
        return None
    evaluations = [
        _mapping(value) for value in _items(item.get("suggestedEvaluations"))
    ]
    existing_evaluation_ids = {
        _callback_evaluation_identity(_text(result.get("itemName")) or "")
        for raw in _items(item.get("existingResults"))
        if (result := _mapping(raw))
        and _text(result.get("itemName"))
    }
    # Hospital results are already preserved above as source indicators.  Do
    # not duplicate them in the recommendation column: that would turn one
    # source fact into two frontend items and could make the documented
    # maximum of one suggested evaluation appear to be violated.
    visible_evaluations = [
        (evaluation, text)
        for evaluation in evaluations
        if evaluation.get("alreadyAvailable") is not True
        if _callback_evaluation_identity(_text(evaluation.get("name")) or "")
        not in existing_evaluation_ids
        if _hospital_common_examination(
            _text(evaluation.get("name")),
            rationale=_text(evaluation.get("rationale")),
        )
        if (text := _evaluation_text(evaluation))
        and (evaluation.get("recommendationSource") == "model" or _public_ref_numbers(
            _ref_numbers(evaluation.get("evidenceRefs")), reference_numbers
        ))
    ][:1]
    recommendations = _unique(
        patient_facing_text(text) for _, text in visible_evaluations
    )
    source_association_refs = _public_ref_numbers(
        _ref_numbers(item.get("associationEvidenceRefs")),
        reference_numbers,
    )
    association_refs = list(source_association_refs)
    medical_interpretation = _text(item.get("medicalInterpretation"))
    has_abnormal_patient_result = _disease_has_supporting_abnormal_result(item)
    # Keep a source-report disease row when its explanation is backed by a
    # directly aligned human disease record. Requiring every record to also
    # contain literal gut-microbiome wording deleted most of the source list,
    # even though the source row itself was not invented by this API. A
    # diagnostic-only record still cannot masquerade as disease explanation.
    association_refs = [
        number
        for number in association_refs
        if _disease_ref_supports_explanation(number, reference_categories)
    ]
    medical_interpretation = _evidence_bound_disease_interpretation(
        item=item,
        medical_interpretation=medical_interpretation,
        evidence_refs=association_refs,
        excluded_evidence_refs=[
            number for number in source_association_refs if number not in association_refs
        ],
        metadata=reference_categories,
    )
    # An independent medical interpretation and its references remain an
    # atomic pair.  Deterministic source-provenance notes are different:
    # they explicitly tell the reader that the text came from the uploaded
    # provider report, or that the provider row has insufficient readable
    # detail.  Those notes are allowed without a literature number so the
    # fixed disease row does not become an unexplained blank cell.
    interpretation_refs = association_refs if medical_interpretation else []
    if not interpretation_refs:
        medical_interpretation = None
    if has_abnormal_patient_result and not medical_interpretation:
        medical_interpretation = _disease_patient_result_interpretation(item)
    recommendation_refs = _public_ref_numbers(
        [
            number
            for evaluation, _ in visible_evaluations
            for number in _ref_numbers(evaluation.get("evidenceRefs"))
        ],
        reference_numbers,
    )
    refs = _unique([*interpretation_refs, *recommendation_refs])
    priority = _disease_attention_priority(
        item=item,
        interpretation_refs=interpretation_refs,
        recommendation_refs=recommendation_refs,
    )
    public_interpretation = patient_facing_text(_disease_public_interpretation(
        medical_interpretation,
        disease_name=_text(item.get("name")),
        human_relation_verified=bool(interpretation_refs),
    ))
    if not public_interpretation or not interpretation_refs:
        # Source rows remain available in the internal source/audit model, but
        # an unsupported name-only row is not a patient-facing medical risk.
        # Publishing it would create an empty table row that looks like an
        # individual disease conclusion.  Only evidence-bound rows enter the
        # callback JSON.
        return None
    suggestion = patient_disease_suggestion(_text(item.get("patientSuggestion")))
    evaluation_ids = {
        _callback_evaluation_identity(_text(evaluation.get("name")) or "")
        for evaluation in evaluations
        if _text(evaluation.get("name"))
    }
    if suggestion and any(
        identity
        and any(
            marker in suggestion.casefold()
            for marker in _callback_evaluation_identity_markers(identity)
        )
        for identity in {*existing_evaluation_ids, *evaluation_ids}
    ):
        suggestion = _remove_evaluation_clause_from_suggestion(
            suggestion,
            identities={*existing_evaluation_ids, *evaluation_ids},
        )
    if not suggestion:
        suggestion = _disease_attention_suggestion(
            disease_name=(
                patient_facing_name(
                    _text(item.get("displayName")) or _text(item.get("name"))
                )
                or "该风险方向"
            ),
            medical_interpretation=public_interpretation,
            evaluations=[evaluation for evaluation, _ in visible_evaluations],
        )
    return {
        "finding_type": "disease",
        "item_id": _text(item.get("diseaseId")),
        "problem": patient_facing_name(
            _text(item.get("displayName")) or _text(item.get("name"))
        ),
        "priority": priority,
        # Existing clinical results are consumed by the score, abnormal-
        # indicator and summary modules. They are intentionally absent from
        # the microbiome disease-risk row so they cannot be mistaken for a
        # newly suggested examination.
        "indicators": [],
        "related_diseases_or_adverse_effects": [],
        "biological_mechanism": public_interpretation,
        "recommendations": recommendations,
        "tips": {
            "special_reminder": suggestion,
            # An empty recommendation list is a valid result when no new,
            # evidence-supported hospital examination is warranted.  Do not
            # turn that absence into a patient-facing failure placeholder.
            "practical_goal": None,
        },
        "interpretation_evidence_refs": interpretation_refs,
        "recommendation_evidence_refs": recommendation_refs,
        "evidence_refs": refs,
        "evidence_level": _evidence_level(refs, reference_categories),
    }


def _callback_evaluation_identity(value: str) -> str:
    normalized = re.sub(r"[\s（）()\-_/]", "", value).casefold()
    aliases = (
        (("结肠镜", "肠镜"), "colonoscopy"),
        (("胃镜", "上消化道内镜"), "gastroscopy"),
        (("糖化血红蛋白", "hba1c"), "hba1c"),
        (
            (
                "甲状腺功能",
                "促甲状腺激素",
                "thyroidfunction",
                "tsh",
                "ft4",
            ),
            "thyroid-function",
        ),
        (("血脂", "胆固醇", "低密度", "甘油三酯"), "lipid-panel"),
        (("粪便隐血", "fit"), "fecal-occult-blood"),
    )
    for names, identity in aliases:
        if any(name in normalized for name in names):
            return identity
    return normalized


def _remove_evaluation_clause_from_suggestion(
    suggestion: str,
    *,
    identities: set[str],
) -> str | None:
    markers = {
        marker.casefold()
        for identity in identities
        for marker in _callback_evaluation_identity_markers(identity)
        if marker
    }
    clauses = [
        clause.strip()
        for clause in re.split(r"[，,；;]", suggestion)
        if clause.strip()
    ]
    kept = [
        clause.rstrip("。！？!?")
        for clause in clauses
        if not (
            any(marker in clause.casefold() for marker in markers)
            and any(
                action in clause
                for action in (
                    "检查",
                    "检验",
                    "筛查",
                    "复查",
                    "需要",
                    "判断",
                    "考虑",
                )
            )
        )
    ]
    value = "，".join(kept).strip()
    if not value or value in {"如需进一步评估", "必要时进一步评估"}:
        return None
    return value + "。"


def _hospital_common_examination(
    name: str | None,
    *,
    rationale: str | None = None,
) -> bool:
    return bool(
        name
        and _specific_evaluation_name(name)
        and is_hospital_common_examination(name, rationale=rationale)
        and _patient_facing_common_examination(name)
    )


def _patient_facing_common_examination(name: str) -> bool:
    """Keep specialist/research procedures out of a general health summary."""

    normalized = "".join(name.casefold().split())
    return not any(
        marker in normalized
        for marker in (
            "肛门直肠测压",
            "结肠传输",
            "胃肠传输",
            "胰岛素钳夹",
            "正电子发射",
            "单光子发射",
            "多导睡眠",
            "神经心理",
            "结构化访谈",
            "腰椎穿刺",
            "脑脊液寡克隆带",
            "homa-ir",
            "homair",
            "fib-4",
            "fib4",
            "researchonly",
        )
    )


def _disease_attention_suggestion(
    *,
    disease_name: str,
    medical_interpretation: str | None,
    evaluations: list[dict[str, Any]],
) -> str:
    """Return a concrete, non-diagnostic action when the model omits advice."""

    del medical_interpretation
    domain, specialty = _disease_attention_domain(disease_name)
    if evaluations:
        evaluation_name = _patient_examination_name(
            _text(evaluations[0].get("name"))
        )
        if evaluation_name:
            return (
                f"如出现{domain}，建议到{specialty}就诊，"
                f"并由医生判断是否需要{evaluation_name}。"
            )
    return f"如持续出现{domain}，建议到{specialty}进一步评估。"


def _disease_attention_domain(disease_name: str) -> tuple[str, str]:
    if any(marker in disease_name for marker in ("菌血症", "败血", "脓毒")):
        return "发热、寒战及其他全身感染表现", "感染科或急诊科"
    if any(
        marker in disease_name
        for marker in (
            "肠",
            "胃",
            "食物中毒",
            "便秘",
            "腹泻",
            "艰难梭菌",
            "克罗恩",
        )
    ):
        return "胃肠道症状与排便变化", "消化内科"
    # Match diabetes before the broad urinary marker below; otherwise the
    # character "尿" in "糖尿病" is misrouted to nephrology/urology.
    if any(marker in disease_name for marker in ("糖尿病", "血糖", "糖代谢")):
        return "血糖及相关代谢变化", "内分泌科"
    if any(marker in disease_name for marker in ("甲状腺", "甲亢", "甲减")):
        return "心率、体重、怕冷或怕热等变化", "内分泌科"
    if any(marker in disease_name for marker in ("肾", "尿", "结石", "痛风")):
        return "泌尿系统不适与肾功能变化", "泌尿外科或肾内科"
    if any(marker in disease_name for marker in ("心", "血管", "高血压")):
        return "心血管相关症状与功能变化", "心血管内科"
    if any(marker in disease_name for marker in ("神经", "认知", "阿尔茨海默", "帕金森")):
        return "认知、情绪或神经系统功能变化", "神经内科"
    return f"{disease_name}相关的健康变化", "相应专科"


def _callback_evaluation_identity_markers(identity: str) -> tuple[str, ...]:
    return {
        "colonoscopy": ("结肠镜", "肠镜"),
        "gastroscopy": ("胃镜", "上消化道内镜"),
        "hba1c": ("糖化血红蛋白", "hba1c"),
        "lipid-panel": ("血脂", "胆固醇", "低密度", "甘油三酯"),
        "fecal-occult-blood": ("粪便隐血", "fit"),
    }.get(identity, (identity,))


def _disease_ref_supports_explanation(
    number: int,
    metadata: dict[int, Any],
) -> bool:
    """Keep only direct human gut evidence for a microbiome-risk row."""

    raw = metadata.get(number)
    item = _mapping(raw)
    # Older callers supplied only the public evidence category. The reference
    # number is still valid, so preserve the former behavior for that shape.
    if not item:
        return raw is not None
    if _text(item.get("category")) == "guideline":
        return False
    target_alignment = _text(item.get("target_alignment"))
    if target_alignment and target_alignment != "direct":
        return False
    if _text(item.get("patient_applicability")) == "not_applicable":
        return False
    intents = set(_items(item.get("intents")))
    if intents and not intents.intersection(
        {
            "disease_clinical_significance",
            "microbe_disease_association",
            "microbe_association",
            # Discovery records may contain the strongest exact taxon-outcome
            # source found for this patient.  They are admitted only after the
            # same direct-target, stool/mucosa, taxon and association checks
            # below, and `_evidence_bound_disease_interpretation` still has to
            # bind the claim to this disease and this report microbe.
            "microbe_discovery",
        }
    ):
        return False
    taxon_scoped = bool(
        intents.intersection(
            {"microbe_disease_association", "microbe_association", "microbe_discovery"}
        )
    )
    if taxon_scoped and _text(item.get("taxon_alignment")) in {
        "strain",
        "toxin_gene",
        "unclear",
    }:
        return False
    if taxon_scoped and _reference_depends_on_unmeasured_subtype(item):
        return False
    sample_alignment = _text(item.get("sample_alignment"))
    if sample_alignment in {"blood", "respiratory", "other"}:
        return False
    evidence_text = " ".join(
        value.casefold()
        for value in (
            _text(item.get("title")),
            _text(item.get("summary")),
            _text(item.get("claim")),
        )
        if value
    )
    has_gut_context = any(
        marker in evidence_text
        for marker in (
            "gut microbi",
            "intestinal microbi",
            "fecal microbi",
            "faecal microbi",
            "stool microbi",
            "肠道菌群",
            "肠道微生物",
            "粪便菌群",
            "粪便微生物",
        )
    )
    has_association = any(
        marker in evidence_text
        for marker in (
            "associated",
            "association",
            "correlated",
            "difference",
            "differences",
            "enriched",
            "decreased",
            "increased",
            "相关",
            "关联",
            "差异",
            "富集",
            "升高",
            "降低",
            "感染",
            "病原",
        )
    )
    return has_association and (
        has_gut_context or sample_alignment in {"stool", "mucosa"}
    )


def _evidence_bound_disease_interpretation(
    *,
    item: dict[str, Any],
    medical_interpretation: str | None,
    evidence_refs: list[int],
    excluded_evidence_refs: list[int],
    metadata: dict[int, Any],
) -> str | None:
    """Remove organism claims whose supporting reference failed final gates."""

    if not medical_interpretation or not evidence_refs:
        return medical_interpretation
    claims = _unique(
        patient_facing_text(_text(_mapping(metadata.get(number)).get("claim")))
        for number in evidence_refs
    )
    claims = [claim for claim in claims if claim]
    if not claims:
        # Compatibility callers may provide only the public evidence category.
        # Without private claim metadata there is nothing to reconcile, so keep
        # the already validated prose rather than manufacturing an empty claim.
        return medical_interpretation
    related_microbes = _unique(
        patient_facing_name(_text(value)) for value in _items(item.get("relatedMicrobes"))
    )
    del excluded_evidence_refs
    disease = patient_facing_name(
        _text(item.get("displayName")) or _text(item.get("name"))
    ) or "该健康方向"
    disease_claims = [
        claim for claim in claims if _claim_mentions_disease(claim, disease)
    ]
    if not disease_claims:
        return None
    supported_microbes = [
        name
        for name in related_microbes
        if name and any(name in claim for claim in disease_claims)
    ]
    taxon_scoped = any(
        set(_items(_mapping(metadata.get(number)).get("intents"))).intersection(
            {"microbe_disease_association", "microbe_association", "microbe_discovery"}
        )
        for number in evidence_refs
    )
    # Rebuild from disease-scoped claims even when the model paragraph looks
    # fluent. A single source record can contain several disease relations;
    # retaining the original paragraph allowed a colorectal-cancer clause to
    # appear in the inflammatory-bowel-disease row.
    return _result_led_disease_interpretation(
        item=item,
        disease=disease,
        microbes=(
            supported_microbes or related_microbes
            if taxon_scoped
            else supported_microbes
        ),
        claims=disease_claims,
    )


def _result_led_disease_interpretation(
    *,
    item: dict[str, Any],
    disease: str,
    microbes: list[str],
    claims: list[str],
) -> str:
    """Explain only why the specified disease direction deserves attention."""

    del item
    visible_microbes = _unique(name for name in microbes if name)[:3]
    subject = _patient_list(visible_microbes)
    change_signal = f"{subject}相关变化" if subject else "相关菌群特征"
    focused_claims = [
        focused
        for claim in claims
        if (focused := _patient_disease_evidence_observation(
            claim,
            disease=disease,
            microbes=visible_microbes,
        ))
    ]
    evidence_text = " ".join(focused_claims).casefold()
    if any(
        marker in evidence_text
        for marker in (
            "可引起",
            "可能引起",
            "导致",
            "病原体",
            "致病菌",
            "causes",
            "pathogen",
        )
    ):
        return f"{subject or '相关菌群'}可能引起{disease}。"
    if any(
        marker in evidence_text
        for marker in ("严重程度", "活动度", "severity", "disease activity")
    ):
        return f"{change_signal}可能与{disease}活动度或严重程度相关。"
    if any(
        marker in evidence_text
        for marker in (
            "风险增加",
            "风险升高",
            "increased risk",
            "higher risk",
            "risk increase",
        )
    ):
        suffix = disease if disease.endswith("风险") else f"{disease}风险"
        return f"{change_signal}提示需关注{suffix}。"
    if disease.endswith("风险"):
        return f"{change_signal}提示{disease}。"
    return f"{change_signal}提示需关注{disease}相关风险。"


def _patient_disease_evidence_observation(
    claim: str,
    *,
    disease: str,
    microbes: list[str],
) -> str | None:
    public_claim = patient_facing_text(claim)
    if not public_claim:
        return None
    text = public_claim.strip().rstrip("。；; ")
    if not text or not _claim_mentions_disease(text, disease):
        return None
    # A single evidence claim may list several disease relations. Keep only
    # the target-disease segment and, when present, its immediately following
    # activity/severity qualifier. This prevents another outcome from leaking
    # into the current row through a long compound sentence.
    relevant_clauses = [
        clause.strip()
        for clause in re.split(r"[。；;\n]+", text)
        if _claim_mentions_disease(clause, disease)
    ]
    focused: list[str] = []
    for clause in relevant_clauses:
        segments = [part.strip() for part in re.split(r"[，,]", clause) if part.strip()]
        for index, segment in enumerate(segments):
            if not _claim_mentions_disease(segment, disease):
                continue
            selected = [segment]
            if index + 1 < len(segments) and any(
                marker in segments[index + 1]
                for marker in ("严重程度", "活动度", "severity", "disease activity")
            ):
                selected.append(segments[index + 1])
            focused.append("，".join(selected))
    value = "；".join(_unique(focused)).strip()
    if not value:
        return None
    for prefix in (
        "人体粪便研究观察到",
        "人体肠道研究观察到",
        "人体临床证据显示",
        "人体研究结果提示",
        "人体研究提示",
        "人体研究显示",
        "研究观察到",
    ):
        value = value.removeprefix(prefix).strip()
    return value or None


def _claim_mentions_disease(value: str, disease: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(
        alias and re.sub(r"\s+", "", alias).casefold() in normalized
        for alias in disease_binding_aliases(disease)
    )


def _callback_disease_has_patient_microbiome_signal(item: dict[str, Any]) -> bool:
    """Reject provider placeholders without deleting supported source rows.

    Some provider disease tables contain a named source row and a retrievable
    human-gut explanation but do not expose their proprietary model score or
    the contributing organism in machine-readable fields.  Treating the score
    as mandatory deleted those evidence-backed rows at callback time.
    """

    source_result = _text(item.get("sourceResult")) or ""
    model_index = _text(item.get("modelIndex")) or ""
    related_microbes = [
        value for raw in _items(item.get("relatedMicrobes"))
        if (value := _text(raw))
    ]
    placeholder = f"{source_result} {model_index}".casefold()
    if re.search(r"<[^>]+>|_png\b|结果暂不可用|结果未解析", placeholder):
        return False
    if _text(item.get("sourceType")) == "source_report":
        # Provider panels can include a full catalogue of low/zero model
        # entries. They remain available in the internal source audit, but the
        # patient-facing risk table should contain only the source's high or
        # medium attention rows that also acquire usable evidence below.
        source_level = source_result.rsplit("；", 1)[-1].strip()
        if source_level in {"低风险", "中低风险"}:
            return False
        try:
            if model_index and float(model_index) <= 0:
                return False
        except ValueError:
            pass
    association_refs = _ref_numbers(item.get("associationEvidenceRefs"))
    # A provider model score or a general disease paper is not a patient-side
    # disease signal.  The public disease-risk table requires an exact link to
    # at least one abnormal organism from this report.  Source rows without
    # that link remain intact in the internal/audit model.
    return bool(
        related_microbes
        and association_refs
        and (source_result or model_index or _text(item.get("medicalInterpretation")))
    )


_ABNORMAL_RESULT_MARKERS = (
    "严重异常",
    "强阳性",
    "显著升高",
    "显著降低",
    "偏高",
    "偏低",
    "超标",
    "升高",
    "降低",
    "异常",
    "阳性",
)
_NON_ABNORMAL_RESULT_MARKERS = (
    "未见异常",
    "无明显异常",
    "未见明显异常",
    "正常",
    "阴性",
)


def _disease_attention_priority(
    *,
    item: dict[str, Any],
    interpretation_refs: list[int],
    recommendation_refs: list[int],
) -> str:
    """Rank health-management attention without inferring disease probability.

    The provider model index is deliberately excluded.  It has no shared
    clinical threshold in this API and therefore cannot safely be converted
    into a high/medium/low disease-risk label.
    """
    if _text(item.get("sourceType")) not in {
        "source_report",
        "evimed_association",
    }:
        return "一般了解"
    if not interpretation_refs:
        return "一般了解"
    if _disease_has_supporting_abnormal_result(item):
        return "重点关注"
    # A valid source-report row must not disappear merely because one bounded
    # retrieval attempt returned no usable evidence. Missing support remains
    # visible as a limitation and is recorded by the internal coverage audit;
    # it never creates an unsupported interpretation or examination.
    return "建议关注"


def _disease_has_supporting_abnormal_result(item: dict[str, Any]) -> bool:
    """Return whether a current clinical result directly supports attention.

    `match_existing_results` intentionally has high recall.  This final gate
    checks direction as well, so a low AST value cannot be used as evidence of
    fatty liver and a normal blood pressure cannot promote hypertension.
    """

    disease = (_text(item.get("name")) or "").casefold()
    for raw in _items(item.get("existingResults")):
        result = _mapping(raw)
        status = _text(result.get("status")) or ""
        if not status or any(
            marker in status for marker in _NON_ABNORMAL_RESULT_MARKERS
        ):
            continue
        name = (_text(result.get("itemName")) or "").casefold()
        value = (_text(result.get("result")) or "").casefold()
        if _abnormal_result_supports_disease(
            disease=disease,
            item_name=name,
            result=value,
            status=status,
        ):
            return True
    return False


def _disease_is_clinical_management_fact(item: dict[str, Any]) -> bool:
    """Separate patient-side clinical facts from microbiome risk rows.

    A directionally matching abnormal result (for example elevated LDL-C for
    dyslipidaemia) is managed through the dynamic system assessment and
    health summary.  An explicit diagnostic statement in a supplied clinical
    result is handled the same way. Normal screening results never trigger
    this classification and therefore do not silently remove a microbiome
    association.
    """

    if _disease_has_supporting_abnormal_result(item):
        return True
    disease = patient_facing_name(_text(item.get("name"))) or ""
    normalized_disease = re.sub(r"[\s（）()\-]", "", disease).casefold()
    if not normalized_disease:
        return False
    aliases = {
        normalized_disease,
        normalized_disease.replace("症", ""),
        normalized_disease.replace("性", ""),
    }
    for raw in _items(item.get("existingResults")):
        result = _mapping(raw)
        value = _text(result.get("result")) or ""
        status = _text(result.get("status")) or ""
        normalized = re.sub(r"[\s（）()\-]", "", value).casefold()
        if not normalized or any(
            marker in f"{status} {value}"
            for marker in _NON_ABNORMAL_RESULT_MARKERS
        ):
            continue
        if any(alias and alias in normalized for alias in aliases):
            return True
    return False


def _abnormal_result_supports_disease(
    *,
    disease: str,
    item_name: str,
    result: str,
    status: str,
) -> bool:
    high = any(marker in status for marker in ("偏高", "升高", "超标", "阳性"))
    low = any(marker in status for marker in ("偏低", "降低"))
    text = f"{item_name} {result}"
    directional_groups = (
        (
            ("高血脂", "高脂血症", "血脂异常"),
            ("总胆固醇", "低密度脂蛋白", "甘油三酯", "ldl", "tc", "tg"),
            ("高密度脂蛋白", "hdl"),
        ),
        (
            ("糖尿病", "血糖异常"),
            ("血糖", "糖化血红蛋白", "hba1c"),
            (),
        ),
        (("高血压",), ("收缩压", "舒张压", "血压"), ()),
        (("高尿酸", "痛风"), ("血尿酸", "尿酸"), ()),
        (("肥胖",), ("bmi", "体重指数", "腰围", "体脂"), ()),
    )
    for diseases, high_markers, low_markers in directional_groups:
        if not any(marker in disease for marker in diseases):
            continue
        return (high and any(marker in text for marker in high_markers)) or (
            low and any(marker in text for marker in low_markers)
        )
    if any(marker in disease for marker in ("脂肪肝", "肝脏疾病")):
        if any(marker in text for marker in ("超声", "彩超", "ct", "磁共振")):
            return any(marker in result for marker in ("脂肪肝", "脂肪沉积"))
        return high and any(
            marker in text
            for marker in ("转氨酶", "谷丙", "谷草", "alt", "ast")
        )
    if any(marker in disease for marker in ("肾脏疾病", "慢性肾病")):
        return (
            high
            and any(marker in text for marker in ("肌酐", "尿素氮", "尿蛋白"))
        ) or (
            low and any(marker in text for marker in ("egfr", "肾小球滤过率"))
        )
    if "甲状腺" in disease:
        return any(
            marker in text
            for marker in ("tsh", "ft3", "ft4", "甲状腺", "促甲状腺")
        )
    return False


def _disease_patient_result_interpretation(item: dict[str, Any]) -> str:
    disease = _text(item.get("name")) or "该健康问题"
    findings: list[str] = []
    for raw in _items(item.get("existingResults")):
        result = _mapping(raw)
        status = _text(result.get("status")) or ""
        name = _text(result.get("itemName")) or ""
        value = _text(result.get("result")) or ""
        if not _abnormal_result_supports_disease(
            disease=disease.casefold(),
            item_name=name.casefold(),
            result=value.casefold(),
            status=status,
        ):
            continue
        detail = f"{name}{value}" if value else name
        if status and status not in detail:
            detail += f"（{status}）"
        findings.append(detail)
    shown = "；".join(_unique(findings)[:3])
    if shown:
        return (
            f"现有检查资料显示{shown}，提示当前与{disease}相关的健康指标"
            "需要重点关注，并结合后续变化进行专业评估。"
        )
    return f"现有检查资料提示与{disease}相关的健康指标需要重点关注。"


def _disease_public_interpretation(
    medical_interpretation: str | None,
    *,
    disease_name: str | None = None,
    human_relation_verified: bool = False,
) -> str | None:
    if not medical_interpretation:
        return None
    value = _strip_attention_prefix(medical_interpretation)
    forbidden = (
        "原菌群报告",
        "原报告列出",
        "原报告将",
        "疾病关联项目",
        "健康关注项目",
        "检测方模型",
    )
    # Drop only the workflow/provenance sentence.  The previous all-or-nothing
    # check erased an otherwise valid model medical explanation whenever one
    # trailing sentence mentioned the source report.
    sentences = []
    for raw_sentence in re.split(r"(?<=[。！？!?])\s*", value):
        sentence = raw_sentence.strip()
        if not sentence or any(marker in sentence for marker in forbidden):
            continue
        if _callback_disease_definition_sentence(
            sentence,
            disease_name=disease_name,
        ):
            continue
        sentence = _strip_trailing_disease_action_clause(sentence)
        if sentence:
            sentences.append(sentence)
    cleaned = "".join(sentences).strip()
    if cleaned and _callback_disease_risk_rationale_is_present(
        cleaned,
        human_relation_verified=human_relation_verified,
    ):
        return cleaned
    return None


def _strip_trailing_disease_action_clause(value: str) -> str:
    """Keep the risk rationale here and leave actions to the advice column."""

    cleaned = re.sub(
        r"[，, ；;](?:因此)?(?:建议|可|可以|需|值得)"
        r"(?:优先|重点|适当)?(?:关注|观察|就医|评估).*$",
        "",
        value,
    ).strip()
    if re.match(
        r"^(?:因此)?(?:建议|可|可以|需|值得)"
        r"(?:优先|重点|适当)?(?:关注|观察|就医|评估)",
        cleaned,
    ):
        return ""
    if cleaned and cleaned[-1] not in "。！？!?":
        cleaned += "。"
    return cleaned


def _callback_disease_definition_sentence(
    value: str,
    *,
    disease_name: str | None,
) -> bool:
    disease = re.escape(disease_name or "")
    subject = rf"(?:{disease}|该疾病|该健康问题)" if disease else r"(?:该疾病|该健康问题)"
    return bool(
        re.search(
            subject
            + r"(?:是一种|是一类|是常见的|以[^,，。；;]{2,80}为特征)",
            value.strip(),
        )
    )


def _callback_disease_risk_rationale_is_present(
    value: str,
    *,
    human_relation_verified: bool = False,
) -> bool:
    patient_signal = (
        any(marker in value for marker in ("本次检测", "本次菌群检测"))
        and any(
            marker in value
            for marker in ("菌", "检出", "丰度", "变化", "组成", "特征")
        )
    ) or any(
        marker in value
        for marker in ("菌群特征", "菌群变化", "菌相关变化", "相关菌")
    )
    human_relation = (
        any(marker in value for marker in ("人体", "人群"))
        and any(marker in value for marker in ("研究", "证据"))
        and any(marker in value for marker in ("相关", "关联", "风险"))
    )
    health_meaning = any(
        marker in value
        for marker in ("关注", "依据", "线索", "提示", "风险", "观察")
    )
    if human_relation_verified and "菌" in value and any(
        marker in value
        for marker in ("可能引起", "可引起", "可能导致", "相关变化")
    ):
        # Result-led pathogen wording is intentionally concise, for example
        # “类志贺邻单胞菌可能引起胃肠炎”。  The cited human stool relation
        # already supplies the evidence boundary; requiring another stock
        # “本次检测提示风险” clause incorrectly erased the whole disease row.
        patient_signal = True
        health_meaning = True
    if human_relation_verified and any(
        marker in value for marker in ("相关", "关联")
    ):
        health_meaning = True
    # The callback has already screened every interpretation reference for a
    # direct human gut/stool relationship.  Do not delete an otherwise
    # patient-specific explanation merely because the prose model used
    # "一项研究" instead of the literal words "人体研究".  Callers without
    # that verified evidence still need the stricter lexical check.
    return (
        patient_signal
        and health_meaning
        and (human_relation or human_relation_verified)
    )


def _strip_attention_prefix(value: str) -> str:
    for prefix in (
        "健康管理关注程度：重点关注。",
        "健康管理关注程度：建议关注。",
        "健康管理关注程度：一般了解。",
    ):
        if value.startswith(prefix):
            return value.removeprefix(prefix).strip() or value
    return value


def _fixed_evidence(
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[int], dict[int, dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    reference_numbers: set[int] = set()
    reference_metadata: dict[int, dict[str, Any]] = {}
    for raw in _items(report.get("evidenceReferences")):
        item = _mapping(raw)
        number = _number(item.get("number"))
        if number is None:
            continue
        category = _text(item.get("category"))
        reference_numbers.add(number)
        interpretation = _mapping(item.get("interpretation"))
        reference_metadata[number] = {
            "category": category,
            "title": _text(item.get("title")),
            "summary": _text(item.get("summary")),
            "source": _text(item.get("source")),
            "url": _text(item.get("url")),
            "claim": _text(interpretation.get("claim")),
            "intents": [
                intent
                for raw_intent in _items(item.get("intents"))
                if (intent := _text(raw_intent))
            ],
            "study_design": _text(interpretation.get("studyDesign"))
            or _join_text(_items(item.get("studyTypes"))),
            "direction": _text(interpretation.get("direction")),
            "confidence": _text(interpretation.get("confidence")),
            "target_alignment": _text(interpretation.get("targetAlignment")),
            "taxon_alignment": _text(interpretation.get("taxonAlignment")),
            "sample_alignment": _text(interpretation.get("sampleAlignment")),
            "patient_applicability": _text(
                interpretation.get("patientApplicability")
            ),
            "targets": [
                target
                for raw_target in _items(item.get("targets"))
                if (target := _text(raw_target))
            ],
        }
        evidence.append(
            {
                "number": number,
                "evidence_type": category,
                "topic": _evidence_topic(item.get("intents")),
                "title": _text(item.get("title")),
                "source": _text(item.get("source")),
                "publication_date": _text(item.get("year")),
                "url": _text(item.get("url")),
                "evidence_level": _category_level(category),
                "provider": _text(item.get("provider")),
                "summary": _text(item.get("summary")),
                # Built after patient facts, risks and actions are bound. The
                # public conclusion answers why this citation matters to this
                # person; the paper claim remains internal metadata.
                "conclusion": None,
                "key_evidence": _concise_evidence_basis(
                    interpretation,
                    fallback_study_type=_join_text(_items(item.get("studyTypes"))),
                ),
                "patient_related_facts": [],
                "evidence_grade": _public_evidence_grade(
                    category=category,
                    confidence=_text(interpretation.get("confidence")),
                ),
                "study_type": (
                    _text(interpretation.get("studyDesign"))
                    or _join_text(_items(item.get("studyTypes")))
                ),
                "authors": [
                    author
                    for value in _items(item.get("authors"))
                    if (author := _text(value))
                ],
            }
        )
    return evidence, reference_numbers, reference_metadata


def _attach_patient_related_facts(
    references: list[dict[str, Any]],
    *,
    findings: list[dict[str, Any]],
    health_assessment: dict[str, Any],
    health_recommendations: list[dict[str, Any]],
    recommended_examinations: list[dict[str, Any]],
    reference_metadata: dict[int, dict[str, Any]],
    disease_source_facts: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Bind each public citation to concise facts from this report.

    Literature metadata explains what a source says; this field explains why
    the citation appears for this person.  Only facts already present in the
    fixed callback are used, so the binding cannot invent a value or finding.
    """

    facts_by_number: dict[int, list[str]] = {}
    targets_by_number: dict[int, list[str]] = {}
    actions_by_number: dict[int, list[str]] = {}
    source_facts_by_risk: dict[str, list[str]] = {}

    def add(numbers: Any, value: str | None) -> None:
        if not value:
            return
        for number in _ref_numbers(numbers):
            facts_by_number.setdefault(number, []).append(value)

    def add_target(numbers: Any, value: str | None) -> None:
        if not value:
            return
        for number in _ref_numbers(numbers):
            targets_by_number.setdefault(number, []).append(value)

    def add_action(numbers: Any, value: str | None) -> None:
        action = _evidence_action_text(value)
        if not action:
            return
        for number in _ref_numbers(numbers):
            actions_by_number.setdefault(number, []).append(action)

    # Build relationship-to-source-fact bindings first.  Disease rows and
    # examination guidelines may cite different references from the organism
    # study, but the patient fact that made the topic relevant is still the
    # original measured organism result—not the derived label "菌群关联风险"
    # and not the action "建议补充检查".
    for finding in findings:
        if _text(finding.get("finding_type")) != "microbe":
            continue
        all_refs = _unique(
            [
                *_ref_numbers(finding.get("interpretation_evidence_refs")),
                *_ref_numbers(finding.get("recommendation_evidence_refs")),
                *_ref_numbers(finding.get("evidence_refs")),
            ]
        )
        base_fact = _microbe_patient_fact(finding)
        add(all_refs, base_fact)
        for raw_risk in _items(
            finding.get("related_diseases_or_adverse_effects")
        ):
            risk = _mapping(raw_risk)
            risk_name = _text(risk.get("name"))
            if base_fact and risk_name:
                source_facts_by_risk.setdefault(risk_name, []).append(base_fact)
            add(risk.get("evidence_refs"), base_fact)
            add_target(risk.get("evidence_refs"), risk_name)

    for finding in findings:
        if _text(finding.get("finding_type")) != "disease":
            continue
        problem = _text(finding.get("problem"))
        if not problem:
            continue
        all_refs = _unique(
            [
                *_ref_numbers(finding.get("interpretation_evidence_refs")),
                *_ref_numbers(finding.get("recommendation_evidence_refs")),
                *_ref_numbers(finding.get("evidence_refs")),
            ]
        )
        source_facts = list(source_facts_by_risk.get(problem, []))
        source_facts.extend((disease_source_facts or {}).get(problem, []))
        source_facts.extend(
            _assessment_facts_named_in_context(
                health_assessment,
                context=" ".join(
                    value
                    for value in (
                        _text(finding.get("biological_mechanism")),
                        _text(_mapping(finding.get("tips")).get("special_reminder")),
                    )
                    if value
                ),
            )
        )
        for fact in _unique(source_facts):
            add(all_refs, fact)
        add_target(all_refs, problem)
        reminder = _text(_mapping(finding.get("tips")).get("special_reminder"))
        add_action(all_refs, reminder)
        for recommendation in _items(finding.get("recommendations")):
            add_action(finding.get("recommendation_evidence_refs"), _text(recommendation))

    abnormal_indicators = [
        item
        for raw in _items(health_assessment.get("abnormalIndicators"))
        if (item := _mapping(raw))
    ]
    # Some clinical references are attached directly to a recommendation or
    # examination instead of an abnormal-indicator row.  If the citation
    # itself explicitly names a measured item, bind that exact patient fact as
    # well.  This makes cards say, for example, which platelet or LDL-C result
    # is relevant without asking the prose model to invent a connection.
    for number, metadata in reference_metadata.items():
        for target in _items(metadata.get("targets")):
            add_target([number], patient_facing_name(_text(target)))
        evidence_context = " ".join(
            value
            for value in (
                _text(metadata.get("title")),
                _text(metadata.get("summary")),
                _text(metadata.get("claim")),
                _join_text(_items(metadata.get("targets"))),
            )
            if value
        )
        for indicator in abnormal_indicators:
            if not _evidence_context_names_indicator(
                evidence_context,
                indicator=indicator,
            ):
                continue
            add([number], _abnormal_indicator_patient_fact(indicator))
    for recommendation in health_recommendations:
        topic = _text(recommendation.get("topic"))
        add_target(recommendation.get("evidence_refs"), topic)
        add_action(recommendation.get("evidence_refs"), _text(recommendation.get("content")))
        matched = [
            fact
            for indicator in abnormal_indicators
            if topic
            and patient_topics_overlap(
                topic,
                " ".join(
                    value
                    for value in (
                        _text(indicator.get("dimensionName")),
                        _text(indicator.get("item")),
                    )
                    if value
                ),
            )
            if (fact := _abnormal_indicator_patient_fact(indicator))
        ]
        for fact in matched[:3]:
            add(recommendation.get("evidence_refs"), fact)

    for examination in recommended_examinations:
        examination_name = _text(examination.get("name"))
        reason = _text(examination.get("reason")) or ""
        matched_risks = [
            risk_name
            for risk_name in source_facts_by_risk
            if risk_name in reason
        ]
        for risk_name in matched_risks:
            add_target(examination.get("evidence_refs"), risk_name)
            for fact in source_facts_by_risk.get(risk_name, []):
                add(examination.get("evidence_refs"), fact)
        if examination_name:
            purpose = reason.rstrip("。")
            add_action(
                examination.get("evidence_refs"),
                f"建议进行{examination_name}"
                + (f"，{purpose}" if purpose else ""),
            )

    output: list[dict[str, Any]] = []
    for reference in references:
        item = dict(reference)
        number = _number(item.get("number"))
        item["patient_related_facts"] = _unique(
            facts_by_number.get(number or -1, [])
        )[:3]
        item["conclusion"] = _patient_evidence_conclusion(
            facts=item["patient_related_facts"],
            targets=_unique(targets_by_number.get(number or -1, []))[:2],
            actions=_unique(actions_by_number.get(number or -1, []))[:1],
        )
        output.append(item)
    return output


def _evidence_action_text(value: str | None) -> str | None:
    text = patient_facing_text(_text(value))
    if not text:
        return None
    return text.rstrip("。；; ") + "。"


def _patient_evidence_conclusion(
    *,
    facts: list[str],
    targets: list[str],
    actions: list[str],
) -> str | None:
    """Explain one citation as patient fact -> concern -> next action."""

    if not facts:
        return None
    fact_text = "、".join(facts[:2])
    if targets:
        concern = f"{fact_text}提示需关注{'、'.join(targets[:2])}"
    else:
        concern = f"{fact_text}是本次需要关注的指标"
    action = actions[0] if actions else "建议关注相关症状或状态变化，如有不适前往相应专科评估。"
    if not action.startswith(("建议", "如", "若", "持续", "优先", "定期")):
        action = f"建议{action}"
    return patient_facing_text(f"{concern}；{action}")


def _source_disease_patient_facts(
    report: dict[str, Any],
) -> dict[str, list[str]]:
    """Keep the provider's original organism signals for evidence binding.

    The public disease row deliberately hides provider model internals and the
    separate organism list.  Evidence cards still need the original patient
    fact that caused the citation to appear.  This mapper consumes only the
    source report's ``relatedMicrobes`` values and never derives a disease
    probability or invents a measurement.
    """

    diseases = _mapping(report.get("diseaseRisks"))
    output: dict[str, list[str]] = {}
    for raw in _items(diseases.get("items")):
        item = _mapping(raw)
        disease = patient_facing_name(
            _text(item.get("displayName")) or _text(item.get("name"))
        )
        if not disease:
            continue
        facts = [
            fact
            for raw_microbe in _items(item.get("relatedMicrobes"))
            if (fact := _source_related_microbe_fact(_text(raw_microbe)))
        ]
        if facts:
            output[disease] = _unique(facts)[:3]
    return output


def _source_related_microbe_fact(value: str | None) -> str | None:
    text = _text(value)
    if not text:
        return None
    match = re.fullmatch(r"\s*(.+?)\s*[（(]\s*([^（）()]+)\s*[）)]\s*", text)
    if match:
        name, result = (part.strip() for part in match.groups())
        return f"{name}：{result}" if name and result else None
    if "：" in text or ":" in text:
        return text
    return None


def _evidence_context_names_indicator(
    context: str,
    *,
    indicator: dict[str, Any],
) -> bool:
    """Require an explicit indicator name or a narrow, known synonym."""

    name = _text(indicator.get("item"))
    code = (_text(indicator.get("indicatorCode")) or "").casefold()
    if name and name.casefold() in context.casefold():
        return True
    aliases = {
        "platelet_count": ("血小板计数", "血小板"),
        "total_cholesterol": ("总胆固醇",),
        "ldl_c": ("ldl-c", "ldl c", "低密度脂蛋白胆固醇"),
        "hdl_c": ("hdl-c", "hdl c", "高密度脂蛋白胆固醇"),
        "triglyceride": ("甘油三酯",),
        "uric_acid": ("尿酸",),
    }
    compact_context = context.casefold()
    return any(alias.casefold() in compact_context for alias in aliases.get(code, ()))


def _microbe_patient_fact(finding: dict[str, Any]) -> str | None:
    problem = _text(finding.get("problem"))
    indicator = next(
        (
            item
            for raw in _items(finding.get("indicators"))
            if (item := _mapping(raw))
        ),
        {},
    )
    result = "，".join(
        value
        for value in (
            _text(indicator.get("status")),
            _text(indicator.get("value")),
        )
        if value
    )
    if problem and result:
        return f"{problem}：{result}"
    return problem


def _abnormal_indicator_patient_fact(item: dict[str, Any]) -> str | None:
    name = _text(item.get("item"))
    value = _text(item.get("value"))
    unit = _text(item.get("unit"))
    status = _text(item.get("status"))
    measured = value
    if value and unit and unit.casefold() not in value.casefold():
        measured = f"{value}{unit}" if unit == "%" else f"{value} {unit}"
    result = "，".join(part for part in (status, measured) if part)
    if name and result:
        return f"{name}：{result}"
    return name


def _assessment_facts_named_in_context(
    health_assessment: dict[str, Any],
    *,
    context: str,
) -> list[str]:
    """Return measured assessment facts explicitly named by published prose.

    Disease interpretation and its examination guideline can use different
    citations while sharing the same patient-side trigger.  Only indicators
    already present in the fixed assessment are eligible, and the indicator
    name must occur verbatim in the published disease explanation.  This keeps
    the evidence card tied to an original result without exposing a derived
    label such as ``菌群关联风险：2型糖尿病``.
    """

    normalized_context = re.sub(r"\s+", "", context).casefold()
    if not normalized_context:
        return []
    candidates = [
        item
        for raw_dimension in _items(health_assessment.get("dimensions"))
        if (dimension := _mapping(raw_dimension))
        for raw_item in _items(dimension.get("evidenceUsed"))
        if (item := _mapping(raw_item))
    ]
    candidates.extend(
        item
        for raw_item in _items(health_assessment.get("abnormalIndicators"))
        if (item := _mapping(raw_item))
    )
    facts: list[str] = []
    for item in candidates:
        name = _text(item.get("item"))
        if not name or re.sub(r"\s+", "", name).casefold() not in normalized_context:
            continue
        if fact := _abnormal_indicator_patient_fact(item):
            facts.append(fact)
    return _unique(facts)[:3]


def _concise_evidence_basis(
    interpretation: dict[str, Any],
    *,
    fallback_study_type: str | None = None,
) -> str | None:
    """Expose only already-structured study facts, never the full abstract."""

    parts: list[str] = []
    values = (
        ("研究人群", _text(interpretation.get("population"))),
        (
            "研究类型",
            _text(interpretation.get("studyDesign")) or fallback_study_type,
        ),
        ("主要结果", _text(interpretation.get("effect"))),
    )
    for label, raw_value in values:
        if value := patient_facing_text(raw_value):
            parts.append(f"{label}：{value.rstrip('。；; ')}")
    return "；".join(parts) or None


def _public_evidence_grade(
    *,
    category: str | None,
    confidence: str | None,
) -> str | None:
    """Map the validated interpretation confidence to a concise display grade."""

    grade = {
        "high": "强",
        "moderate": "中",
        "low": "弱",
    }.get((confidence or "").casefold())
    if grade:
        return grade
    return {
        "guideline": "中",
        "drug_label": "中",
        "clinical_trial": "弱",
        "literature": "弱",
    }.get(category or "")


def _patient_health_assessment(value: dict[str, Any]) -> dict[str, Any]:
    """Remove internal scoring fallback language from the public assessment."""

    def sanitize(raw: Any) -> Any:
        if isinstance(raw, dict):
            output = {key: sanitize(item) for key, item in raw.items()}
            if _text(raw.get("sourceType")) == "pdf_or_image":
                # Patient pages should explain the clinical source, not expose
                # storage object names or parsing-channel implementation.
                output["source"] = "菌群检测报告"
                output["sourceType"] = "microbiome_data"
            return output
        if isinstance(raw, list):
            return [sanitize(item) for item in raw]
        if isinstance(raw, str):
            value = raw.replace(
                "（来源报告：正常；健康管理评估：未达标）",
                "（来源报告：正常）",
            )
            value = value.replace("（健康管理评估：未达标）", "")
            value = value.replace("（未达到健康管理评估标准）", "").replace(
                "未达到健康管理评估标准", "需要关注"
            )
            return patient_facing_text(value) or ""
        return raw

    output = sanitize(value)
    dimension_scores = {
        str(item.get("code") or ""): int(item.get("score") or 100)
        for raw in _items(value.get("dimensions"))
        if (item := _mapping(raw)) and item.get("scoreAvailable") is True
    }
    abnormal_indicators = [
        (index, sanitize(item))
        for index, item in enumerate(value.get("abnormalIndicators", []))
        if _mapping(item).get("status") != "未达到健康管理评估标准"
    ]
    abnormal_indicators.sort(
        key=lambda pair: _abnormal_indicator_sort_key(
            pair[1],
            source_index=pair[0],
            dimension_scores=dimension_scores,
        )
    )
    output["abnormalIndicators"] = [item for _index, item in abnormal_indicators]
    for indicator in output["abnormalIndicators"]:
        significance = (
            _public_indicator_clinical_significance(
                _text(indicator.get("clinicalSignificance")),
                indicator=indicator,
            )
            or _model_metric_clinical_significance(indicator)
            or _clinical_indicator_significance_fallback(indicator)
        )
        if significance:
            indicator["clinicalSignificance"] = significance
    abnormal_codes = {
        str(item.get("indicatorCode") or "")
        for item in output["abnormalIndicators"]
        if item.get("indicatorCode")
    }
    for raw_dimension in _items(output.get("dimensions")):
        dimension = _mapping(raw_dimension)
        if not dimension:
            continue
        dimension["ruleBasis"] = _deduplicated_public_rule_basis(
            _items(dimension.get("ruleBasis"))
        )
        evidence_used: list[dict[str, Any]] = []
        seen_evidence: set[tuple[Any, ...]] = set()
        for raw_evidence in _items(dimension.get("evidenceUsed")):
            evidence = _mapping(raw_evidence)
            if not evidence or str(evidence.get("indicatorCode") or "") in abnormal_codes:
                continue
            key = (
                evidence.get("indicatorCode"),
                evidence.get("value"),
                evidence.get("unit"),
                evidence.get("date"),
            )
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            evidence_used.append(evidence)
        dimension["evidenceUsed"] = evidence_used
    return output


def _model_metric_clinical_significance(item: dict[str, Any]) -> str | None:
    """Provide a useful fallback when model-authored clinical prose is absent."""

    code = str(item.get("indicatorCode") or "")
    status = _text(item.get("status")) or "当前结果"
    name = _text(item.get("item")) or "该指标"
    if code.startswith("barrier_aux:"):
        if "acetate" in code:
            return (
                f"{name}{status}可能影响肠道酸性环境和黏膜屏障稳态，"
                "建议关注胃肠道炎症及糖脂代谢相关风险。"
            )
        if "propionate" in code:
            return (
                f"{name}{status}可能影响肠道能量利用和糖脂代谢调节，"
                "建议关注血糖、血脂异常相关风险。"
            )
        if "butyrate" in code:
            return (
                f"{name}{status}可能影响结肠上皮能量供给和黏膜屏障稳态，"
                "建议关注炎症性肠病及结直肠健康风险。"
            )
        return f"{name}{status}可能影响肠黏膜屏障和代谢稳态，建议关注胃肠道炎症相关风险。"
    if code.startswith("barrier_core:"):
        if "anti_inflammatory" in code:
            return (
                f"{name}{status}可能影响肠黏膜炎症调节，"
                "建议关注炎症性肠病及反复腹泻等胃肠道炎症风险。"
            )
        return (
            f"{name}{status}可能影响肠道对病原微生物的防御和黏膜免疫稳态，"
            "建议关注感染性腹泻及胃肠道感染风险。"
        )
    if code.startswith("microbiome:"):
        return (
            f"{name}{status}可能增加条件致病菌扩增和肠道生态失衡的机会，"
            "建议关注感染性腹泻及胃肠道炎症风险。"
        )
    return None


def _public_indicator_clinical_significance(
    value: str | None,
    *,
    indicator: dict[str, Any],
) -> str | None:
    """Keep only patient-facing, clinically useful indicator explanations."""

    if not value:
        return None
    forbidden = (
        "模型",
        "推断",
        "基于菌群数据",
        "不等同于",
        "直接测得",
        "算法",
    )
    if any(marker in value for marker in forbidden):
        return None
    if re.search(r"提示(?:与)?[^，。；]{0,30}(?:功能|能力)(?:表现)?(?:需要|需)关注", value):
        return None
    code = str(indicator.get("indicatorCode") or "")
    if code.startswith(("barrier_aux:", "barrier_core:", "microbiome:")) and "风险" not in value:
        return None
    return patient_facing_text(value)


def _clinical_indicator_significance_fallback(item: dict[str, Any]) -> str | None:
    """Keep a measured abnormal indicator clinically readable after LLM omission.

    The model-authored summary remains the first choice.  This fallback uses
    only the already validated item name, status and assessment dimension; it
    never adds a diagnosis, threshold, examination or treatment suggestion.
    """

    name = _text(item.get("item")) or "该指标"
    status = _text(item.get("status")) or "异常"
    dimension = _text(item.get("dimensionName"))
    code = str(item.get("indicatorCode") or "")
    if code == "total_cholesterol":
        return (
            f"{name}{status}提示血脂负担需要关注；若持续异常，"
            "可能增加动脉粥样硬化性心血管风险，"
            "应结合其他血脂项目及历史变化综合评估。"
        )
    if code == "ldl_c":
        return (
            f"{name}{status}提示动脉粥样硬化性脂蛋白负担需要关注，"
            "是心血管风险管理的重要因素，"
            "应结合整体血脂结果及其他危险因素综合理解。"
        )
    domain = dimension or "相关健康"
    return (
        f"{name}{status}提示{domain}方面需要关注；"
        "应结合同维度其他指标及历史变化综合理解。"
    )


def _deduplicated_public_rule_basis(
    values: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    indicators: dict[tuple[str, str, str, str], list[str]] = {}
    for raw in values:
        item = _mapping(raw)
        if not item:
            continue
        key = (
            str(item.get("ruleId") or ""),
            str(item.get("source") or ""),
            str(item.get("version") or ""),
            str(item.get("boundary") or ""),
        )
        groups.setdefault(key, dict(item))
        indicator = _text(item.get("indicator"))
        if indicator:
            indicators.setdefault(key, []).append(indicator)
    output: list[dict[str, Any]] = []
    for key, item in groups.items():
        names = _unique(indicators.get(key, []))
        if names:
            item["indicator"] = (
                "、".join(names[:3])
                + (f"等{len(names)}项指标" if len(names) > 3 else "")
            )
        output.append(item)
    return output


def _abnormal_indicator_sort_key(
    item: dict[str, Any],
    *,
    source_index: int,
    dimension_scores: dict[str, int],
) -> tuple[int, int, int, int, int]:
    severity_rank = {
        "priority_attention": 0,
        "attention": 1,
    }.get(str(item.get("severity") or ""), 2)
    status = _text(item.get("status")) or ""
    status_rank = (
        0
        if any(marker in status for marker in ("严重", "显著", "重度", "强阳性"))
        else 1
        if any(marker in status for marker in _ABNORMAL_RESULT_MARKERS)
        else 2
    )
    dimension_score = dimension_scores.get(str(item.get("dimensionCode") or ""), 100)
    date_digits = re.sub(r"\D", "", _text(item.get("date")) or "")[:8]
    date_rank = -int(date_digits) if date_digits.isdigit() else 0
    return severity_rank, status_rank, dimension_score, date_rank, source_index


def _clinical_context(
    abnormal: dict[str, Any],
    health: dict[str, Any],
    *,
    public_findings: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    context: dict[str, str] = {}
    enterotype = _mapping(health.get("enterotype"))
    if public_findings is not None:
        microbe_summary = _public_microbe_context_summary(public_findings)
        pending_summary = _text(abnormal.get("summary"))
        if not microbe_summary and pending_summary and "尚在生成" in pending_summary:
            microbe_summary = pending_summary
    else:
        microbe_summary = _text(abnormal.get("summary"))
    candidates = (
        ("异常菌群概况", microbe_summary),
        ("肠型", enterotype.get("name")),
        ("综合健康画像", _public_health_profile(_text(health.get("healthProfile")))),
    )
    for key, value in candidates:
        if text := patient_facing_text(_text(value)):
            context[key] = text
    if public_findings is not None and any(
        finding.get("finding_type") == "microbe" for finding in public_findings
    ):
        context["菌群关联判定说明"] = (
            "菌群医学解读用于说明本次粪便样本中的健康关注线索。"
            "检出或丰度变化不等同于临床感染或疾病诊断；"
            "是否存在相应问题，需由医生结合症状、体征和针对性检查判断。"
        )
    return context


def _public_health_profile(value: str | None) -> str | None:
    text = patient_facing_text(value)
    if not text:
        return None
    return (
        text.replace(
            "肠道感染相关风险",
            "胃肠道症状变化及必要时的病原学确认",
        )
        .replace("肠道感染风险", "胃肠道症状变化及必要时的病原学确认")
        .replace("感染相关风险", "相关症状变化及必要时的临床确认")
    )


def _public_microbe_context_summary(
    findings: list[dict[str, Any]],
) -> str | None:
    microbes = [
        finding
        for finding in findings
        if finding.get("finding_type") == "microbe"
    ]
    if not microbes:
        return None
    names = _unique(
        _text(finding.get("problem")) for finding in microbes
    )[:3]
    outcomes = _unique(
        _text(risk.get("name"))
        for finding in microbes
        for risk in _items(finding.get("related_diseases_or_adverse_effects"))
    )[:6]
    summary = f"本次达到人体证据和医学解读要求的菌群关注项包括{'、'.join(names)}"
    if outcomes:
        summary += f"；相关疾病或不良影响包括{'、'.join(outcomes)}"
    return summary + "。"


def _public_summary_conclusion(
    report: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str | None:
    """Publish the model summary only when it matches the final public rows.

    Evidence and patient-applicability gates run after the prose model.  A
    summary created before those gates must not keep an organism or disease
    that the final callback correctly removed.  When the model summary is
    absent or unusable, publish a stage summary from the remaining public
    facts instead of leaving the comprehensive conclusion empty.
    """

    summary = patient_facing_text(_text(report.get("executiveSummary")))
    if not summary:
        return _public_fallback_conclusion(report, findings)
    public_text = " ".join(
        filter(
            None,
            (
                _text(finding.get("problem"))
                for finding in findings
            ),
        )
    )
    public_text += " " + " ".join(
        filter(
            None,
            (
                _text(risk.get("name"))
                for finding in findings
                for raw in _items(
                    finding.get("related_diseases_or_adverse_effects")
                )
                if (risk := _mapping(raw))
            ),
        )
    )
    public_disease_names = {
        name
        for finding in findings
        if finding.get("finding_type") == "disease"
        if (name := _text(finding.get("problem")))
    }
    public_microbe_text = " ".join(
        filter(
            None,
            (
                _text(finding.get("problem"))
                for finding in findings
                if finding.get("finding_type") == "microbe"
            ),
        )
    )

    abnormal = _mapping(report.get("abnormalMicrobes"))
    for raw in _items(abnormal.get("items")):
        item = _mapping(raw)
        for name in (_text(item.get("name")), _text(item.get("scientificName"))):
            if name and name in summary and name not in public_microbe_text:
                return _public_fallback_conclusion(report, findings)

    diseases = _mapping(report.get("diseaseRisks"))
    for raw in _items(diseases.get("items")):
        item = _mapping(raw)
        name = patient_facing_name(_text(item.get("name")))
        if name and name in summary and name not in public_text:
            return _public_fallback_conclusion(report, findings)

    if not public_disease_names and any(
        marker in summary
        for marker in ("疾病关联部分", "疾病关联风险", "患病概率")
    ):
        return _public_fallback_conclusion(report, findings)
    return summary


def _public_fallback_conclusion(
    report: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str | None:
    """Summarize only rows that survived the final public evidence gates.

    The prose model runs before final citation/applicability filtering.  A
    single later-hidden name must not erase the entire conclusion; it is safer
    and more useful to replace that paragraph with a compact summary whose
    inputs are the final findings and already-validated management topics.
    """

    health = _mapping(report.get("healthManagement"))
    topics = _unique(
        patient_facing_text(_text(_mapping(raw).get("topic")))
        for raw in _items(health.get("recommendations"))
    )[:3]
    has_microbes = any(
        finding.get("finding_type") == "microbe" for finding in findings
    )
    has_diseases = any(
        finding.get("finding_type") == "disease" for finding in findings
    )
    if not topics:
        if has_microbes:
            topics.append("肠道微生态")
        if has_diseases:
            topics.append("相关健康影响")
    if not topics:
        assessment_summary = patient_facing_text(
            _text(_mapping(report.get("healthAssessment")).get("summary"))
        )
        if assessment_summary:
            return assessment_summary
        return (
            "本次阶段性评估显示，当前资料尚不足以形成完整的六维综合评分；"
            "本结论仅反映目前已获得的信息，"
            "待相关检测或体检资料补充后再更新综合评价。"
        )

    parts = [
        "本次健康资料提示，当前管理重点为"
        + "、".join(topics)
        + "。"
    ]
    enterotype = _mapping(health.get("enterotype"))
    enterotype_name = _text(enterotype.get("name"))
    if enterotype_name:
        parts.append(f"本次肠型为{enterotype_name}。")
    if has_microbes:
        parts.append(
            "肠道菌群结构及相关功能存在需要持续管理的变化，"
            "可结合胃肠道舒适度和排便表现观察后续趋势。"
        )
    if has_diseases:
        parts.append(
            "循证支持的健康风险方向可用于安排症状观察和就医重点，"
            "并应结合已有检查结果理解其实际意义。"
        )
    parts.append(
        "后续应优先处理已经明确的异常，围绕饮食结构、规律活动和作息"
        "开展持续管理，按医生建议完成必要复查，并根据症状及指标趋势调整方案。"
    )
    parts.append(
        "如出现持续或逐渐加重的不适，应及时到相应专科进一步评估。"
    )
    return "".join(parts)


def _report_interpretation(
    abnormal: dict[str, Any], health: dict[str, Any]
) -> list[str]:
    level = _mapping(health.get("healthLevel"))
    # The comprehensive portrait is already published once under
    # diagnosis_summary.clinical_context. Do not repeat it verbatim in the
    # health-management section; this field is only an incomplete-report
    # fallback when no portrait exists.
    if _text(health.get("healthProfile")):
        return []
    for raw in (level.get("summary"), abnormal.get("summary")):
        if value := _text(raw):
            return [value]
    return []


def _health_recommendations(
    health: dict[str, Any],
    reference_numbers: set[int],
    *,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    targets = _health_recommendation_targets(report)
    recommendations: list[tuple[int, dict[str, Any]]] = []
    for index, raw in enumerate(_items(health.get("recommendations"))):
        item = _mapping(raw)
        category = _text(item.get("category")) or "general"
        content = _health_action_content(_text(item.get("content")))
        refs = _public_ref_numbers(
            _ref_numbers(item.get("evidenceRefs")),
            reference_numbers,
        )
        topic = patient_facing_text(
            _text(item.get("topic")) or targets.get(category)
        )
        priority = _text(item.get("priority")) or _health_attention_priority(
            report,
            category=category,
            topic=topic,
        )
        if (
            not content
            or contains_promotional_absolute(content)
            or "主要风险：" in content
            or re.search(
                r"(?:可能|可|易)[^。；;]{0,30}(?:增加|升高|导致)"
                r"[^。；;]{0,24}(?:疾病|健康)?风险",
                content,
            )
            or not topic
            or patient_health_topic_is_generic(topic)
            or priority not in {"重点关注", "建议关注"}
        ):
            continue
        recommendations.append(
            (
                index,
                {
                    "category": category,
                    "topic": topic,
                    "priority": priority,
                    "content": content,
                    "evidence_refs": refs,
                },
            )
        )
    recommendations.sort(
        key=lambda pair: (
            0 if pair[1]["priority"] == "重点关注" else 1,
            pair[0],
        )
    )
    deduplicated: list[dict[str, Any]] = []
    for _index, item in recommendations:
        overlapping_index = next(
            (
                index
                for index, existing in enumerate(deduplicated)
                if (
                    patient_topics_overlap(existing.get("topic"), item.get("topic"))
                    or _health_summary_topic_domain(existing.get("topic"))
                    == _health_summary_topic_domain(item.get("topic"))
                )
            ),
            None,
        )
        if overlapping_index is not None:
            existing = deduplicated[overlapping_index]
            if (
                existing.get("priority") == item.get("priority")
                and len(patient_topic_key(item.get("topic")))
                > len(patient_topic_key(existing.get("topic")))
            ):
                deduplicated[overlapping_index] = item
            continue
        deduplicated.append(item)
        if len(deduplicated) == 3:
            break
    if len(deduplicated) < 3:
        for item in _assessment_action_recommendations(report):
            if any(
                patient_topics_overlap(existing.get("topic"), item.get("topic"))
                or _health_summary_topic_domain(existing.get("topic"))
                == _health_summary_topic_domain(item.get("topic"))
                for existing in deduplicated
            ):
                continue
            deduplicated.append(item)
            if len(deduplicated) == 3:
                break
    return deduplicated


def _health_summary_topic_domain(value: str | None) -> str:
    topic = value or ""
    if any(marker in topic for marker in ("肠道", "胃肠", "消化", "排便")):
        return "gut"
    if any(
        marker in topic
        for marker in ("心血管", "代谢", "血脂", "血糖", "体重")
    ):
        return "cardiometabolic"
    if any(marker in topic for marker in ("肾脏", "尿酸", "泌尿")):
        return "kidney"
    if any(marker in topic for marker in ("运动", "活动", "有氧", "抗阻")):
        return "exercise"
    if any(marker in topic for marker in ("饮食", "营养", "膳食")):
        return "diet"
    if any(marker in topic for marker in ("睡眠", "作息")):
        return "sleep"
    return patient_health_topic_domain(topic) or patient_topic_key(topic)


def _assessment_action_recommendations(
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build action-only summary fallbacks from scored patient dimensions."""

    templates = {
        "digestive_system": (
            "general",
            "消化系统健康",
            (
                "保持饮食结构多样和作息规律，持续观察腹痛、腹胀及排便变化；"
                "相关不适持续或加重时前往消化专科评估。"
            ),
        ),
        "metabolic_system": (
            "general",
            "心血管与代谢管理",
            (
                "优先调整饮食结构，将有氧运动与抗阻训练结合，并管理体重变化；"
                "按医生建议复查血脂相关项目，根据变化趋势调整管理方案。"
            ),
        ),
        "cardiovascular_system": (
            "general",
            "心血管健康",
            (
                "持续管理饮食、体重和运动习惯，并按医生建议复查相关项目；"
                "出现胸痛、胸闷或活动耐量明显下降时及时就医。"
            ),
        ),
        "urinary_system": (
            "general",
            "肾脏与尿酸管理",
            (
                "保持均衡饮食和合理饮水，并将体重纳入持续管理；按医生建议"
                "复查肾功能及尿酸相关项目，根据变化趋势调整方案。"
            ),
        ),
        "immune_system": (
            "general",
            "免疫系统健康",
            (
                "保持规律作息并关注持续炎症或免疫相关不适；已有明确症状或"
                "检查异常时，由相应专科结合临床资料进一步评估。"
            ),
        ),
        "nervous_system": (
            "general",
            "神经系统健康",
            (
                "关注记忆、运动协调及感觉变化；相关表现持续或加重时前往"
                "神经内科评估。"
            ),
        ),
        "mental_system": (
            "general",
            "精神心理健康",
            (
                "持续关注情绪、睡眠和日常功能变化；明显影响生活或持续加重"
                "时，及时寻求精神心理专业评估。"
            ),
        ),
        "tumor_risk": (
            "general",
            "肿瘤风险管理",
            (
                "结合年龄、症状和既往检查落实相应筛查；出现持续异常表现时，"
                "由医生判断是否需要进一步检查。"
            ),
        ),
    }
    output: list[dict[str, Any]] = []
    assessment = _mapping(report.get("healthAssessment"))
    for raw in _items(assessment.get("dimensions")):
        dimension = _mapping(raw)
        level = _text(dimension.get("level"))
        template = templates.get(_text(dimension.get("code")) or "")
        if level not in {"priority_attention", "attention"} or template is None:
            continue
        category, topic, content = template
        output.append(
            {
                "category": category,
                "topic": topic,
                "priority": (
                    "重点关注" if level == "priority_attention" else "建议关注"
                ),
                "content": content,
                "evidence_refs": [],
            }
        )
    return output


def _health_action_content(value: str | None) -> str | None:
    """Keep the action, not a repeated benefit/risk explanation."""

    text = patient_facing_text(value)
    if not text:
        return None
    if re.match(r"^\s*(?:的(?:建议|指导原则)|建议)\s*[，,]", text):
        return None
    clauses = [part.strip() for part in re.split(r"[，,。]", text) if part.strip()]
    if len(clauses) > 1 and any(
        marker in clauses[-1]
        for marker in (
            "有助于",
            "具有基础性作用",
            "改善",
            "增强",
            "降低",
            "减少",
            "预防",
        )
    ):
        text = "，".join(clauses[:-1]).rstrip("。；; ") + "。"
    if re.search(
        r"(?:有助于|能够|从而|以便|对[^\u3002；;]{0,24}具有)"
        r"[^\u3002；;]{0,36}(?:作用|改善|增强|降低|减少|预防)",
        text,
    ):
        return None
    return text


def _health_attention_priority(
    report: dict[str, Any],
    *,
    category: str,
    topic: str | None,
) -> str:
    assessment = _mapping(report.get("healthAssessment"))
    topic_text = topic or ""
    category_codes = {
        "diet": {
            "metabolic_system",
            "digestive_system",
        },
        "exercise": {
            "metabolic_system",
            "cardiovascular_system",
        },
        "bowel_habits": {"digestive_system"},
        "sleep": {"mental_system", "nervous_system"},
        "general": set(),
    }
    for raw in _items(assessment.get("dimensions")):
        item = _mapping(raw)
        if not item or item.get("level") != "priority_attention":
            continue
        if (
            str(item.get("code") or "") in category_codes.get(category, set())
            or (_text(item.get("name")) or "") in topic_text
            or category == "general"
        ):
            return "重点关注"
    return "建议关注"


def _health_recommended_examinations(
    health: dict[str, Any],
    reference_numbers: set[int],
    *,
    report: dict[str, Any],
    public_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Disease-row suggestions have already been bound to a specific evidence
    # source and checked against completed examinations.  Use them as the only
    # source of truth.  The model's health-summary list may rank a matching
    # candidate, but its free-form evidence numbers must never replace the
    # exact candidate refs.
    public_disease_ids = {
        _text(finding.get("item_id"))
        for finding in public_findings
        if finding.get("finding_type") == "disease"
        and _text(finding.get("item_id"))
    }
    candidates: list[dict[str, Any]] = []
    diseases = _mapping(report.get("diseaseRisks"))
    for raw in _items(diseases.get("items")):
        disease = _mapping(raw)
        if (
            _text(disease.get("diseaseId")) not in public_disease_ids
            and _text(disease.get("sourceType")) != "clinical_context"
        ):
            continue
        existing_ids = {
            _callback_evaluation_identity(_text(result.get("itemName")) or "")
            for value in _items(disease.get("existingResults"))
            if (result := _mapping(value)) and _text(result.get("itemName"))
        }
        for value in _items(disease.get("suggestedEvaluations")):
            evaluation = _mapping(value)
            source_name = _text(evaluation.get("name"))
            name = _patient_examination_name(source_name)
            purpose = _text(evaluation.get("purpose"))
            reason = patient_facing_text(_text(evaluation.get("rationale")))
            refs = _public_ref_numbers(
                _ref_numbers(evaluation.get("evidenceRefs")), reference_numbers
            )
            if (
                evaluation.get("alreadyAvailable") is True
                or not source_name
                or not name
                or purpose not in {"筛查", "确认", "评估"}
                or not reason
                or (not refs and evaluation.get("recommendationSource") != "model")
                or _callback_evaluation_identity(source_name) in existing_ids
                or not _hospital_common_examination(source_name, rationale=reason)
            ):
                continue
            candidates.append(
                {
                    "order": len(candidates),
                    "identity": _callback_evaluation_identity(source_name),
                    "name": name,
                    "purpose": purpose,
                    "reason": reason,
                    "evidence_refs": refs,
                }
            )
            break

    preferred = [
        (
            _callback_evaluation_identity(_text(item.get("name")) or ""),
            _text(item.get("purpose")),
        )
        for raw in _items(health.get("recommendedExaminations"))
        if (item := _mapping(raw))
    ]
    preferred_rank = {key: index for index, key in enumerate(preferred)}
    candidates.sort(
        key=lambda item: (
            preferred_rank.get((item["identity"], item["purpose"]), len(preferred)),
            item["order"],
        )
    )
    examinations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate["name"].casefold()
        if key in seen:
            continue
        examinations.append(
            {
                "name": candidate["name"],
                "purpose": candidate["purpose"],
                "reason": candidate["reason"],
                "evidence_refs": candidate["evidence_refs"],
            }
        )
        seen.add(key)
        if len(examinations) == 3:
            break
    return examinations


def _health_recommendation_targets(report: dict[str, Any]) -> dict[str, str]:
    assessment = _mapping(report.get("healthAssessment"))
    dimensions = [
        item
        for raw in _items(assessment.get("dimensions"))
        if (item := _mapping(raw))
        and item.get("level") in {"attention", "priority_attention"}
    ]
    by_code = {
        str(item.get("code") or ""): _text(item.get("name"))
        for item in dimensions
    }

    def joined(codes: tuple[str, ...]) -> str | None:
        values = _unique(by_code[code] for code in codes if by_code.get(code))
        return "、".join(values[:2]) or None

    targets: dict[str, str] = {}
    if target := joined(
        (
            "metabolic_system",
            "digestive_system",
        )
    ):
        targets["diet"] = target
    abnormal = _mapping(report.get("abnormalMicrobes"))
    if _items(abnormal.get("items")) and "diet" not in targets:
        targets["diet"] = "肠道微生态"
    if target := joined(
        (
            "metabolic_system",
            "cardiovascular_system",
        )
    ):
        targets["exercise"] = target

    diseases = _mapping(report.get("diseaseRisks"))
    disease_text = " ".join(
        filter(
            None,
            (
                _text(item.get("name"))
                for raw in _items(diseases.get("items"))
                if (item := _mapping(raw))
                and _ref_numbers(item.get("associationEvidenceRefs"))
            ),
        )
    )
    if any(marker in disease_text for marker in ("便秘", "腹泻", "排便", "肠易激")):
        targets["bowel_habits"] = "排便和胃肠道健康"

    patient_text = " ".join(
        filter(
            None,
            (
                _text(report.get("executiveSummary")),
                _text(health_profile)
                if (health_profile := _mapping(report.get("healthManagement")).get("healthProfile"))
                else None,
            ),
        )
    )
    if any(
        marker in patient_text
        for marker in ("睡眠不足", "失眠", "睡眠质量", "入睡困难", "作息不规律")
    ):
        targets["sleep"] = "睡眠和作息"
    if dimensions:
        targets["general"] = "、".join(
            _unique(
                name
                for item in dimensions
                if (name := _text(item.get("name")))
            )[:2]
        ) or "综合健康影响"
    return targets


def _age_text(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text if text.endswith("岁") else f"{text}岁"


def _date_text(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("_", "-").replace("/", "-")
    return normalized.replace("年", "-").replace("月", "-").removesuffix("日")


def _calendar_date_text(value: Any) -> str | None:
    """Serialize a generated timestamp as the patient-facing calendar date."""

    text = _text(value)
    if not text:
        return None
    match = re.search(
        r"(?P<year>\d{4})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})",
        text,
    )
    if not match:
        return None
    return (
        f"{int(match.group('year')):04d}-"
        f"{int(match.group('month')):02d}-"
        f"{int(match.group('day')):02d}"
    )


_PATIENT_EXAMINATION_NAMES = {
    "泌尿系超声检查": "泌尿系超声",
    "非增强螺旋ct检查": "泌尿系CT",
    "尿常规检查": "尿常规",
    "粪便病原菌培养": "便检",
    "粪便病原学检查": "便检",
    "粪便病原体核酸检测": "便检",
    "粪便钙卫蛋白检测": "便检",
    "粪便免疫化学检测（fit）": "便潜血检查",
    "艰难梭菌gdh抗原及毒素检测": "便检",
    "甲状腺功能检查（tsh、ft4）": "甲状腺功能检查",
}


def _patient_examination_name(value: str | None) -> str | None:
    """Use familiar hospital names without weakening the evidence binding."""

    name = _text(value)
    if not name:
        return None
    key = re.sub(r"\s+", "", name).casefold()
    if "口服葡萄糖耐量" in key:
        return "口服葡萄糖耐量试验"
    return _PATIENT_EXAMINATION_NAMES.get(key, name)


def _finding_value(finding: dict[str, Any]) -> str | None:
    value = _text(finding.get("value"))
    unit = _text(finding.get("unit"))
    if not value:
        return None
    return f"{value}{unit}" if unit and not value.endswith(unit) else value


def _evaluation_text(value: dict[str, Any]) -> str | None:
    source_name = _text(value.get("name"))
    name = _patient_examination_name(source_name)
    rationale = _text(value.get("rationale"))
    purpose = _text(value.get("purpose"))
    if (
        not name
        or not rationale
        or not purpose
        or not source_name
        or not _specific_evaluation_name(source_name)
    ):
        return None
    # ``rationale`` already states the examination purpose in patient
    # language. Repeating the internal purpose enum in parentheses produced
    # awkward strings such as ``便检（评估）：用于评估……``.
    prefix = name
    if value.get("alreadyAvailable") is True:
        prefix = f"已有检查·{prefix}"
    concise_rationale = re.split(r"[。；;\n]", rationale, maxsplit=1)[0].strip()
    if name == "口服葡萄糖耐量试验":
        concise_rationale = "用于评估血糖代谢情况"
    elif name == "甲状腺功能检查":
        concise_rationale = "用于初步评估甲状腺功能异常"
    if concise_rationale and concise_rationale[-1] not in "。！？!?":
        concise_rationale += "。"
    return "：".join(part for part in (prefix, concise_rationale) if part)


def _specific_evaluation_name(value: str) -> bool:
    normalized = " ".join(value.split()).strip("：:，,。 ")
    if len(normalized) < 2:
        return False
    generic = (
        "进一步检查",
        "相关检查",
        "完善检查",
        "临床检查",
        "专科评估",
        "进一步评估",
        "医学评估",
    )
    return not any(normalized == marker for marker in generic)


def _evidence_topic(value: Any) -> str | None:
    labels = {
        "microbe_discovery": "异常菌群医学关联",
        "microbe_clinical_significance": "异常菌群医学关联",
        "microbe_association": "异常菌群医学关联",
        "microbe_disease_association": "菌种与疾病关联",
        "disease_clinical_significance": "疾病医学解释",
        "diagnostic_guideline": "配套临床检查",
        "health_management": "健康总结",
        "intervention": "人体干预与生活方式",
        "gut_function": "肠道功能关联",
        "gut_barrier": "肠道屏障关联",
    }
    topics = _unique(label for raw in _items(value) if (label := labels.get(str(raw))))
    return "、".join(topics) or None


def _category_level(category: str | None) -> str | None:
    return {
        "guideline": "guideline_or_consensus",
        "literature": "peer_reviewed_literature",
        "clinical_trial": "clinical_trial",
        "drug_label": "drug_label",
    }.get(category or "")


def _evidence_level(
    numbers: list[int],
    metadata: dict[int, Any],
) -> str | None:
    """Aggregate the plan's four evidence grades deterministically.

    A legacy ``number -> category`` map is accepted for internal compatibility.
    Missing optional population/design metadata lowers the grade; it does not
    erase an otherwise exact and eligible record.
    """

    entries: list[dict[str, Any]] = []
    for number in dict.fromkeys(numbers):
        raw = metadata.get(number)
        if isinstance(raw, str):
            entries.append({"category": raw})
        elif isinstance(raw, dict):
            entries.append(raw)
    if not entries:
        return None

    applicable = [
        item
        for item in entries
        if item.get("patient_applicability") != "not_applicable"
    ]
    if not applicable:
        return "当前不适用"

    def normalized(value: Any) -> str:
        return str(value or "").strip().casefold()

    def is_ineligible(item: dict[str, Any]) -> bool:
        design = normalized(item.get("study_design"))
        return any(
            marker in design
            for marker in (
                "animal",
                "in vitro",
                "cell",
                "动物",
                "细胞",
                "病例报告",
                "case report",
            )
        )

    eligible = [item for item in applicable if not is_ineligible(item)]
    if not eligible:
        return "证据不足"

    def is_high_quality_review(item: dict[str, Any]) -> bool:
        design = normalized(item.get("study_design"))
        return any(
            marker in design
            for marker in (
                "systematic review",
                "meta-analysis",
                "meta analysis",
                "系统综述",
                "meta分析",
            )
        ) and item.get("patient_applicability") in {
            None,
            "",
            "direct",
            "contextual",
        }

    if any(is_high_quality_review(item) for item in eligible):
        return "证据较明确"

    if any(
        item.get("category") == "guideline"
        and item.get("patient_applicability")
        in {None, "", "direct", "contextual"}
        for item in eligible
    ):
        return "证据较明确"

    strong_records = [
        item
        for item in eligible
        if item.get("patient_applicability")
        in {None, "", "direct", "contextual"}
        and item.get("taxon_alignment") not in {"strain", "toxin_gene"}
        and item.get("sample_alignment")
        not in {"blood", "respiratory", "other"}
    ]
    fingerprints = {
        "|".join(
            normalized(item.get(key))
            for key in ("title", "source", "url")
        )
        for item in strong_records
    }
    directions = {
        normalized(item.get("direction"))
        for item in strong_records
        if normalized(item.get("direction"))
        not in {"", "not_stated", "mixed"}
    }
    if len(fingerprints) >= 2 and len(directions) == 1:
        return "证据较明确"

    return "可能相关"


def _summary_ref_numbers(
    report: dict[str, Any],
    *,
    summary: str | None = None,
) -> list[int]:
    """Bind summary refs only to source rows explicitly named in the summary."""
    public_summary = summary if summary is not None else _text(
        report.get("executiveSummary")
    )
    if not public_summary:
        return []
    numbers: list[int] = []
    abnormal = _mapping(report.get("abnormalMicrobes"))
    for raw in _items(abnormal.get("items")):
        item = _mapping(raw)
        names = (_text(item.get("name")), _text(item.get("scientificName")))
        if any(name and name in public_summary for name in names):
            numbers.extend(_ref_numbers(item.get("evidenceRefs")))
    diseases = _mapping(report.get("diseaseRisks"))
    for raw in _items(diseases.get("items")):
        item = _mapping(raw)
        name = _text(item.get("name"))
        if name and name in public_summary:
            numbers.extend(_ref_numbers(item.get("evidenceRefs")))
    health = _mapping(report.get("healthManagement"))
    for raw in _items(health.get("recommendations")):
        item = _mapping(raw)
        content = _text(item.get("content"))
        if content and content in public_summary:
            numbers.extend(_ref_numbers(item.get("evidenceRefs")))
    return list(dict.fromkeys(numbers))


def _public_ref_numbers(values: list[int], reference_numbers: set[int]) -> list[int]:
    return _unique(value for value in values if value in reference_numbers)


def _ref_numbers(value: Any) -> list[int]:
    return [number for raw in _items(value) if (number := _number(raw)) is not None]


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _join_text(values: list[str | None]) -> str | None:
    return " ".join(_unique(value for value in values if value)) or None


_T = TypeVar("_T")


def _unique(values: Any) -> list[_T]:
    return list(dict.fromkeys(values))
