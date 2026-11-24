"""Deterministic source-fact selection and clinical-result matching."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from modules.report.schemas import (
    AbnormalMicrobeItem,
    AbnormalMicrobesSection,
    ClinicalResult,
    DiseaseRiskItem,
    DiseaseRisksSection,
    EnterotypeResult,
    EvidenceInterpretation,
    EvidenceRecommendedEvaluation,
    EvidenceRecord,
    HealthAssessment,
    HospitalDataInput,
    MicrobePanelFinding,
    ParsedMicrobiomeReport,
    ParsedTaxon,
    SourceFinding,
    SuggestedEvaluation,
    stable_id,
)


def build_abnormal_microbes(parsed: ParsedMicrobiomeReport) -> AbnormalMicrobesSection:
    items: list[AbnormalMicrobeItem] = []
    for taxon in focus_taxa(parsed.taxa):
        items.append(
            AbnormalMicrobeItem(
                microbe_id=taxon.taxon_id,
                name=(
                    taxon.chinese_name
                    or _TAXON_CHINESE_NAMES.get(taxon.latin_name.casefold())
                    or taxon.latin_name
                ),
                scientific_name=taxon.latin_name,
                category=_public_category(taxon),
                source_finding=SourceFinding(
                    status=_source_status(taxon),
                    value=taxon.value,
                    unit=taxon.unit,
                    reference_range=taxon.reference_range,
                ),
            )
        )
    panel_findings = []
    for metric in parsed.metrics:
        if not metric.code.startswith("panel_"):
            continue
        category = metric.code.removeprefix("panel_")
        if category not in {
            "beneficial",
            "pathogenic",
            "opportunistic",
            "fungi_pathogenic",
            "other",
        }:
            continue
        panel_findings.append(
            MicrobePanelFinding(
                category=category,
                name=metric.name,
                value=metric.value,
                unit=metric.unit,
                status=metric.status,
            )
        )
    return AbnormalMicrobesSection(
        items=items,
        panel_findings=panel_findings,
    )


def build_source_diseases(
    parsed: ParsedMicrobiomeReport,
    hospital_data: list[HospitalDataInput],
) -> DiseaseRisksSection:
    items: list[DiseaseRiskItem] = []
    for disease in parsed.disease_results:
        item_type = _disease_item_type(disease.name)
        source_parts = []
        if disease.source_score:
            source_parts.append(f"原报告关联模型结果 {disease.source_score}")
        if disease.source_level:
            source_parts.append(disease.source_level)
        items.append(
            DiseaseRiskItem(
                disease_id=disease.disease_id,
                name=disease.name,
                display_name=(
                    f"{disease.name}相关模型信号"
                    if item_type in {"biomarker", "phenotype"}
                    else disease.name
                ),
                source_type="source_report",
                item_type=item_type,
                source_result="；".join(source_parts) or None,
                model_index=disease.source_score,
                source_description=disease.source_description,
                related_microbes=disease.related_taxa,
                existing_results=match_existing_results(disease.name, hospital_data),
            )
        )
    return DiseaseRisksSection(items=items)


def attach_clinical_followup_context(
    diseases: DiseaseRisksSection,
    health_assessment: HealthAssessment,
    hospital_data: list[HospitalDataInput],
    *,
    max_items: int = 3,
) -> None:
    """Add bounded internal clinical rows for whole-report follow-up checks.

    The public disease table remains microbiome-only. These rows give the
    ordinary diagnostic evidence path explicit, source-matched clinical
    conditions so the health summary can recommend a common hospital
    follow-up examination when it has not already been completed.
    """

    if max_items <= 0:
        return
    existing_clinical = {
        _normalized_disease_identity(item.name): item
        for item in diseases.items
        if item.source_type == "clinical_context"
    }
    risk_rank = {"high": 0, "medium": 1, "pending": 2, "low": 3}
    candidates = []
    for dimension_index, dimension in enumerate(health_assessment.dimensions):
        for order, related in enumerate(dimension.related_diseases):
            if related.source != "临床资料" or not related.primary:
                continue
            candidates.append(
                (
                    risk_rank.get(related.risk_level, 4),
                    dimension_index,
                    order,
                    related.name,
                )
            )

    added = 0
    seen: set[str] = set()
    for _risk, _dimension, _order, name in sorted(candidates):
        identity = _normalized_disease_identity(name)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        matched_results = match_existing_results(name, hospital_data)
        if not matched_results:
            continue
        existing = existing_clinical.get(identity)
        if existing is not None:
            existing.existing_results = _dedupe(
                [*existing.existing_results, *matched_results],
                key=lambda item: (
                    item.item_name,
                    item.result,
                    item.result_time,
                ),
            )
            continue
        diseases.items.append(
            DiseaseRiskItem(
                disease_id=stable_id("clinical-followup", identity),
                name=name,
                display_name=name,
                source_type="clinical_context",
                source_result="临床资料记录",
                existing_results=matched_results,
            )
        )
        existing_clinical[identity] = diseases.items[-1]
        added += 1
        if added >= max_items:
            break


def disease_has_patient_microbiome_signal(disease: DiseaseRiskItem) -> bool:
    """Return whether a disease row owns a patient-specific microbiome fact.

    Provider PDFs sometimes contain unresolved export placeholders for every
    disease in their catalogue.  The disease name and a general definition are
    useful for source auditing, but they are not a result for this patient.
    Public retrieval and narrative work therefore require at least one real
    source result or a directly bound abnormal taxon.
    """

    if disease.source_type == "clinical_context":
        return False
    if disease.source_type == "source_report":
        source_result = disease.source_result or ""
        source_level = source_result.rsplit("；", 1)[-1].strip()
        if source_level in {"低风险", "中低风险"}:
            return False
        try:
            if disease.model_index and float(disease.model_index) <= 0:
                return False
        except ValueError:
            pass
    return bool(
        disease.source_result
        or disease.model_index
        or disease.related_microbes
    )


def promote_evidence_disease_associations(
    diseases: DiseaseRisksSection,
    microbes: AbnormalMicrobesSection,
) -> None:
    """Promote evidence-bound human gut associations into disease rows.

    A contextual population association is not an individual disease
    probability, but it is still useful as a non-diagnostic health direction
    when the study specimen is stool or intestinal mucosa. Reuse an unresolved
    provider row with the same disease name instead of appending a duplicate.
    Cross-site, preclinical and unsupported relations remain excluded.
    """

    existing_by_name: dict[str, DiseaseRiskItem] = {}
    for disease in diseases.items:
        existing_by_name.setdefault(
            _normalized_disease_identity(disease.name),
            disease,
        )

    for microbe in microbes.items:
        finding = microbe.source_finding
        finding_text = " ".join(
            value
            for value in (
                finding.status,
                finding.value,
                finding.unit,
            )
            if value
        ).strip()
        for risk in microbe.disease_and_risks:
            if (
                not risk.name
                or not risk.evidence_refs
                or risk.patient_applicability not in {"direct", "contextual"}
                or risk.sample_alignment not in {"stool", "mucosa"}
                or not _microbe_relation_direction_is_publishable(
                    category=microbe.category,
                    source_status=finding.status,
                    relation_type=risk.relation_type,
                )
            ):
                continue
            identity = _normalized_disease_identity(risk.name)
            existing = existing_by_name.get(identity)
            if existing is not None:
                if not existing.source_result:
                    existing.source_result = (
                        f"{microbe.name}：{finding_text}"
                        if finding_text
                        else f"{microbe.name}为本次异常菌群关注项"
                    )
                existing.related_microbes = list(
                    dict.fromkeys([*existing.related_microbes, microbe.name])
                )
                existing.association_evidence_refs = sorted(
                    set(existing.association_evidence_refs + risk.evidence_refs)
                )
                existing.evidence_refs = sorted(
                    set(existing.evidence_refs + risk.evidence_refs)
                )
                if not existing.related_content:
                    existing.related_content = risk.description
                if (
                    not existing.medical_interpretation
                    or microbe.name not in existing.medical_interpretation
                ):
                    existing.medical_interpretation = (
                        _promoted_disease_interpretation(
                            microbe_name=microbe.name,
                            disease_name=risk.name,
                            relation_description=risk.description,
                        )
                    )
                continue
            source_result = (
                f"{microbe.name}：{finding_text}"
                if finding_text
                else f"{microbe.name}为本次异常菌群关注项"
            )
            promoted = DiseaseRiskItem(
                disease_id=stable_id("evidence-disease", risk.name),
                name=risk.name,
                display_name=risk.name,
                source_type="evimed_association",
                source_result=source_result,
                related_content=risk.description,
                medical_interpretation=_promoted_disease_interpretation(
                    microbe_name=microbe.name,
                    disease_name=risk.name,
                    relation_description=risk.description,
                ),
                related_microbes=[microbe.name],
                association_evidence_refs=list(risk.evidence_refs),
                evidence_refs=list(risk.evidence_refs),
            )
            diseases.items.append(promoted)
            existing_by_name[identity] = promoted


def _promoted_disease_interpretation(
    *,
    microbe_name: str,
    disease_name: str,
    relation_description: str | None,
) -> str:
    """Build a cautious fallback from a validated human gut relation."""

    relation = (relation_description or "").strip().rstrip("。；; ")
    if relation and disease_name in relation:
        return (
            f"本次检测中的{microbe_name}变化具有人体肠道研究依据："
            f"{relation}。该结果可作为{disease_name}方向的健康关注线索。"
        )
    return (
        f"本次检测中的{microbe_name}变化与人体肠道研究观察到的"
        f"{disease_name}相关菌群特征一致，可作为该方向的健康关注线索。"
    )


def _microbe_relation_direction_is_publishable(
    *,
    category: str | None,
    source_status: str | None,
    relation_type: str | None,
) -> bool:
    """Prevent a low pathogen abundance from becoming an infection risk."""

    status = (source_status or "").casefold()
    low_result = any(
        marker in status
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "未检出", "阴性")
    )
    if not low_result:
        return True
    if category not in {
        "pathogenic_signal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }:
        return True
    return relation_type == "epidemiologic_association"


def _normalized_disease_identity(value: str) -> str:
    normalized = re.sub(r"[\s（）()\-_/]", "", value).casefold()
    replacements = {
        "炎症性肠道疾病": "炎症性肠病",
        "肠易激综合症": "肠易激综合征",
        "ii型糖尿病": "2型糖尿病",
        "ⅱ型糖尿病": "2型糖尿病",
        "帕金森": "帕金森病",
        "高血脂": "高脂血症",
        "高尿酸": "高尿酸血症",
    }
    return replacements.get(normalized, normalized)


def focus_taxa(taxa: Iterable[ParsedTaxon]) -> list[ParsedTaxon]:
    selected: list[ParsedTaxon] = []
    seen: set[str] = set()
    for taxon in taxa:
        include = taxon.explicitly_abnormal
        if (
            taxon.category in {"pathogenic", "opportunistic", "fungi_pathogenic"}
            and taxon.detection_status in {"detected", "trace"}
            # A source panel may list every detected organism while also
            # marking it normal or providing a per-taxon reference interval.
            # Those rows are not abnormalities.  A bare detection remains a
            # focus finding only when the source supplied no normal comparator.
            and not taxon.reference_range
            and not _source_marks_taxon_normal(taxon.source_status)
        ):
            include = True
        if taxon.category == "beneficial" and not taxon.explicitly_abnormal:
            include = False
        if include and taxon.taxon_id not in seen:
            seen.add(taxon.taxon_id)
            selected.append(taxon)
    return selected


def _source_marks_taxon_normal(value: str | None) -> bool:
    normalized = "".join((value or "").casefold().split())
    return any(
        marker in normalized
        for marker in (
            "正常",
            "normal",
            "参考范围内",
            "范围内",
            "未见异常",
            "无异常",
        )
    )


def determine_enterotype(parsed: ParsedMicrobiomeReport) -> EnterotypeResult | None:
    if parsed.enterotype:
        return EnterotypeResult(name=parsed.enterotype, source="source_report")

    abundance: defaultdict[str, float] = defaultdict(float)
    for taxon in parsed.taxa:
        numeric = _number(taxon.value)
        if numeric is None:
            continue
        normalized = taxon.latin_name.casefold()
        for genus in ("bacteroides", "prevotella", "ruminococcus"):
            if normalized == genus or normalized.startswith(f"{genus} "):
                abundance[genus] += numeric
    if not abundance:
        return None
    ordered = sorted(abundance.items(), key=lambda item: item[1], reverse=True)
    winner, value = ordered[0]
    runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
    if value < 1.0 or (runner_up > 0 and value < runner_up * 2):
        return None
    labels = {
        "bacteroides": "肠型I",
        "prevotella": "肠型II",
        "ruminococcus": "肠型III",
    }
    return EnterotypeResult(
        name=labels[winner],
        source="derived",
        basis=f"{winner}相对丰度{value:g}%，至少为次优势属的2倍",
    )


def attach_suggested_evaluations(
    diseases: DiseaseRisksSection,
    evidence: list[EvidenceRecord],
    evidence_numbers: dict[str, int],
    *,
    subject_age: int | None = None,
) -> None:
    by_support: defaultdict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in evidence:
        for support in record.supports:
            by_support[support].append(record)
    for disease in diseases.items:
        existing_identities = {
            _evaluation_identity(result.item_name)
            for result in disease.existing_results
        }
        # Evidence-promoted disease rows are created from a microbe relation,
        # so the supporting record may still be bound to the taxon rather than
        # the newly created disease id.  Reuse records already cited by the
        # promoted association; otherwise their exact diagnostic content can
        # never reach ``suggested_evaluations``.
        associated_numbers = set(disease.association_evidence_refs)
        candidate_records = _dedupe(
            [
                *by_support.get(disease.disease_id, []),
                *(
                    record
                    for record in evidence
                    if evidence_numbers.get(record.evidence_key)
                    in associated_numbers
                ),
                # The dedicated diagnostic query can fail while another
                # retained EviMed record already contains an exact local
                # disease-test-purpose statement. Reuse that source for this
                # disease regardless of which taxon query retrieved it. The
                # strict matcher below still requires all three concepts in
                # the source text, so the catalogue cannot invent a test.
                *(
                    record
                    for record in evidence
                    if record.evidence_key in evidence_numbers
                    and _source_bound_fallback_evaluations(
                        disease,
                        record,
                        subject_age=subject_age,
                    )
                ),
            ],
            key=lambda record: record.evidence_key,
        )
        records = sorted(
            candidate_records,
            key=lambda record: _diagnostic_record_priority(record, disease=disease),
            reverse=True,
        )
        proposed: list[SuggestedEvaluation] = []
        for record in records:
            if (
                record.category not in {"guideline", "literature", "clinical_trial"}
                or "diagnostic_guideline" not in record.intents
                or record.interpretation is None
            ):
                continue
            source_bound_identities = {
                _evaluation_identity(item.name)
                for item in _source_bound_fallback_evaluations(
                    disease,
                    record,
                    subject_age=subject_age,
                )
            }
            for evaluation in record.interpretation.recommended_evaluations:
                evaluation_identity = _evaluation_identity(evaluation.name)
                if evaluation_identity in existing_identities:
                    # A completed examination remains available to the score
                    # and summary, but is never republished as a supplemental
                    # recommendation.
                    continue
                if not _record_binds_evaluation_to_disease(
                    disease,
                    record,
                    evaluation,
                ):
                    continue
                if (
                    disease.name in _SOURCE_BOUND_DIAGNOSTIC_TESTS
                    and _evaluation_identity(evaluation.name)
                    not in source_bound_identities
                ):
                    # A model-proposed test is not enough.  For catalogued
                    # diseases, the same local source passage must explicitly
                    # contain the disease, test and diagnostic/assessment
                    # purpose before the recommendation can be displayed.
                    continue
                if not evaluation_is_public_for_disease(
                    evaluation.name,
                    rationale=evaluation.rationale,
                    disease=disease,
                    subject_age=subject_age,
                ):
                    continue
                rationale = _evaluation_rationale_for_report(
                    disease=disease,
                    evaluation_name=evaluation.name,
                    source_rationale=evaluation.rationale,
                    purpose=evaluation.purpose,
                    source_text=f"{record.title}。{record.summary or ''}",
                )
                if rationale is None:
                    rationale = _verified_evaluation_purpose(
                        disease=disease,
                        evaluation=evaluation,
                        source_text=f"{record.title}。{record.summary or ''}",
                    )
                # A missing source phrase or an unbound check stays in the
                # audit.  The compact fallback above is allowed only after the
                # source phrase, disease-test binding and hospital-examination
                # gates have all passed; it changes wording, not medical scope.
                if rationale is None:
                    continue
                if disease.name == "糖化血红蛋白":
                    rationale = "用于复核糖化血红蛋白（HbA1c）水平。"
                proposed.append(
                    SuggestedEvaluation(
                        name=evaluation.name,
                        rationale=rationale,
                        already_available=False,
                        purpose=(
                            "评估"
                            if disease.name == "糖化血红蛋白"
                            else "筛查"
                            if any(
                                marker in rationale for marker in ("筛查", "风险分层")
                            )
                            else "评估"
                            if disease.item_type == "phenotype"
                            or any(
                                marker in rationale
                                for marker in (
                                    "评估",
                                    "病因",
                                    "分型",
                                    "类型",
                                    "严重程度",
                                    "功能",
                                )
                            )
                            or (
                                disease.name == "硬化性胆管炎"
                                and any(
                                    marker in evaluation.name.casefold()
                                    for marker in ("mrcp", "磁共振胰胆管", "磁共振胆管")
                                )
                            )
                            else "确认"
                            if disease.item_type == "biomarker"
                            or any(
                                marker in rationale
                                for marker in (
                                    f"检测{disease.name}",
                                    f"诊断{disease.name}",
                                    f"辅助{disease.name}诊断",
                                    "提高诊断的确定性",
                                    "用于检测",
                                )
                            )
                            else _public_evaluation_purpose(evaluation.purpose)
                        ),
                        evidence_refs=[evidence_numbers[record.evidence_key]],
                    )
                )
        # The interpretation model can omit ``recommended_evaluations`` even
        # when an exact EviMed V2 guide block explicitly names both the target
        # disease and a concrete diagnostic test. Recover only those
        # source-bound pairs; this is not a static recommendation fallback and
        # it never turns a treatment mention into a public examination row.
        if not proposed:
            for record in records:
                if (
                    record.category not in {"guideline", "literature"}
                    or record.evidence_key not in evidence_numbers
                ):
                    continue
                interpretation = record.interpretation
                if interpretation is not None and (
                    interpretation.target_alignment != "direct"
                    or interpretation.patient_applicability == "not_applicable"
                ):
                    continue
                source_bound = _source_bound_fallback_evaluations(
                    disease,
                    record,
                    subject_age=subject_age,
                )
                # A relationship search can return a diagnostic meta-analysis
                # whose local text explicitly binds the same disease, test and
                # purpose even though its query intent is not labelled
                # ``diagnostic_guideline``.  The strict source-bound matcher is
                # sufficient here; a catalog name alone never creates a row.
                if "diagnostic_guideline" not in record.intents and not source_bound:
                    continue
                for evaluation in source_bound:
                    if _evaluation_identity(evaluation.name) in existing_identities:
                        continue
                    proposed.append(
                        evaluation.model_copy(
                            update={
                                "already_available": False,
                                "evidence_refs": [
                                    evidence_numbers[record.evidence_key]
                                ],
                            }
                        )
                    )
        deduped = _dedupe(
            proposed,
            key=lambda item: _evaluation_identity(item.name),
        )
        ranked = sorted(
            enumerate(deduped),
            key=lambda pair: (
                _suggested_evaluation_priority(pair[1], disease=disease),
                -pair[0],
            ),
            reverse=True,
        )
        disease.suggested_evaluations = (
            [ranked[0][1]] if ranked else [
                item for item in disease.suggested_evaluations
                if item.recommendation_source == "model"
            ][:1]
        )
        if not disease.suggested_evaluations:
            _clear_confirmation_evidence(disease)
            continue
        # Cite only the confirmation sources attached to checks that are
        # actually visible in this row.  Retrieval candidates rejected by the
        # medical matcher remain auditable but must not leak into the public
        # bibliography or inflate the evidence cell.
        disease.confirmation_evidence_refs = sorted(
            {
                number
                for evaluation in disease.suggested_evaluations
                for number in evaluation.evidence_refs
            }
        )
        disease.evidence_refs = sorted(
            set(disease.association_evidence_refs + disease.confirmation_evidence_refs)
        )


def record_has_displayable_evaluation(
    disease: DiseaseRiskItem,
    record: EvidenceRecord,
    *,
    subject_age: int | None = None,
) -> bool:
    """Use the final table rules when deciding whether a retry is needed."""

    if (
        record.category not in {"guideline", "literature", "clinical_trial"}
        or "diagnostic_guideline" not in record.intents
    ):
        return False
    interpretation = record.interpretation
    if interpretation is None:
        return bool(
            _source_bound_fallback_evaluations(
                disease,
                record,
                subject_age=subject_age,
            )
        )
    if (
        interpretation.target_alignment != "direct"
        or interpretation.patient_applicability == "not_applicable"
    ):
        return False
    source_text = " ".join(
        dict.fromkeys(
            part.strip()
            for part in (record.title, record.summary or "", record.evidence_text or "")
            if part and part.strip()
        )
    )
    for evaluation in interpretation.recommended_evaluations:
        if not _record_binds_evaluation_to_disease(disease, record, evaluation):
            continue
        if disease.name in _SOURCE_BOUND_DIAGNOSTIC_TESTS:
            source_bound_identities = {
                _evaluation_identity(item.name)
                for item in _source_bound_fallback_evaluations(
                    disease,
                    record,
                    subject_age=subject_age,
                )
            }
            if _evaluation_identity(evaluation.name) not in source_bound_identities:
                continue
        if not evaluation_is_public_for_disease(
            evaluation.name,
            rationale=evaluation.rationale,
            disease=disease,
            subject_age=subject_age,
        ):
            continue
        rationale = _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name=evaluation.name,
            source_rationale=evaluation.rationale,
            purpose=evaluation.purpose,
            source_text=source_text,
        )
        if rationale is None:
            rationale = _verified_evaluation_purpose(
                disease=disease,
                evaluation=evaluation,
                source_text=source_text,
            )
        if rationale is not None:
            return True
    return bool(
        _source_bound_fallback_evaluations(
            disease,
            record,
            subject_age=subject_age,
        )
    )


def _record_binds_evaluation_to_disease(
    disease: DiseaseRiskItem,
    record: EvidenceRecord,
    evaluation: EvidenceRecommendedEvaluation,
) -> bool:
    """Require one local source unit to bind target, check and purpose.

    A model-level ``targetAlignment=direct`` describes the retrieved block as
    a whole. It cannot make every examination in a long comorbidity guideline
    relevant to the query disease. A disease-naming rationale is sufficient;
    otherwise the title/summary must provide the local binding.
    """

    source_text = " ".join(
        dict.fromkeys(
            part.strip()
            for part in (record.title, record.summary or "", record.evidence_text or "")
            if part and part.strip()
        )
    )
    if not source_text:
        return False
    disease_aliases = _disease_binding_aliases(disease)
    applicability = (
        record.interpretation.applicability
        if record.interpretation is not None
        else None
    ) or ""
    compact_applicability = "".join(applicability.casefold().split())
    compact_disease = "".join(disease.name.casefold().split())
    if (
        compact_disease
        and (
            f"未涉及{compact_disease}" in compact_applicability
            or f"不适用于{compact_disease}" in compact_applicability
        )
    ):
        # The interpretation service can occasionally bind an otherwise valid
        # hospital test from a completely different guide to the query target
        # (for example thyroid ultrasound to diarrhoea) while explicitly
        # acknowledging that the source does not address that target.  Never
        # let the model-authored rationale override that contradiction.
        return False
    if _evaluation_rationale_targets_disease(
        evaluation.rationale,
        disease=disease,
    ):
        return True
    base_name = re.sub(r"[（(][^）)]*[）)]", "", evaluation.name).strip()
    test_aliases = tuple(
        dict.fromkeys(
            value
            for value in (
                evaluation.source_phrase,
                evaluation.name,
                base_name,
                re.sub(r"(?:检查|检测|测定|扫描|评估)$", "", base_name),
            )
            if value
        )
    )
    purpose_markers = (
        "诊断",
        "筛查",
        "评估",
        "分期",
        "监测",
        "diagnos",
        "confirm",
        "screen",
        "evaluat",
        "assess",
        "detect",
        "monitor",
        "staging",
        "首选",
    )
    title_lowered = record.title.casefold()
    title_scopes_disease = any(
        alias.casefold() in title_lowered for alias in disease_aliases if alias
    )
    return any(
        _source_segment_binds_disease_test(
            segment,
            disease_aliases=disease_aliases,
            test_aliases=test_aliases,
            purpose_markers=purpose_markers,
            title_scopes_disease=title_scopes_disease,
        )
        for segment in (
            part.strip(" ，,")
            for part in re.split(
                r"(?<=[.!?。！？；;])\s*|[\r\n]+",
                source_text,
            )
        )
        if segment
    )


def recover_source_bound_diagnostic_interpretations(
    diseases: DiseaseRisksSection,
    evidence: list[EvidenceRecord],
    *,
    subject_age: int | None = None,
) -> None:
    """Recover exact diagnostic guide blocks when model parsing is missing.

    EviMed V2 can return a useful guide text block while the interpretation
    model times out or omits ``recommendedEvaluations``.  Such a block used to
    be removed by the final public gate before the deterministic source matcher
    could inspect it.  Recovery is deliberately narrow: one retained source
    must name the exact report disease, a catalogued concrete test, and a
    diagnostic/assessment verb.  The catalog supplies aliases only and cannot
    create a recommendation in the absence of matching source text.
    """

    diseases_by_id = {item.disease_id: item for item in diseases.items}
    for record in evidence:
        if (
            record.interpretation is not None
            or record.category not in {"guideline", "literature"}
            or "diagnostic_guideline" not in record.intents
        ):
            continue
        for disease_id in record.supports:
            disease = diseases_by_id.get(disease_id)
            if disease is None:
                continue
            evaluations = _source_bound_fallback_evaluations(
                disease,
                record,
                subject_age=subject_age,
            )
            if not evaluations:
                continue
            record.interpretation = EvidenceInterpretation(
                claim=(
                    f"该来源文本明确列出"
                    f"{'、'.join(item.name for item in evaluations)}"
                    f"用于{disease.name}的临床评估。"
                ),
                study_design=(
                    "临床指南/共识" if record.category == "guideline" else "人体文献"
                ),
                applicability="仅用于配套检查的来源绑定，不表示已确诊。",
                confidence="moderate",
                target_alignment="direct",
                patient_applicability="contextual",
                recommended_evaluations=[
                    EvidenceRecommendedEvaluation(
                        name=item.name,
                        rationale=item.rationale,
                        source_phrase=_diagnostic_source_phrase(record),
                        purpose={
                            "筛查": "screen_target",
                            "评估": "characterize_target",
                            "确认": "confirm_target",
                        }[item.purpose],
                    )
                    for item in evaluations
                ],
            )
            break


def _diagnostic_source_phrase(record: EvidenceRecord) -> str:
    source_text = " ".join(
        dict.fromkeys(
            part.strip()
            for part in (record.title, record.summary or "", record.evidence_text or "")
            if part and part.strip()
        )
    )
    return source_text[:1000]


def _source_bound_fallback_evaluations(
    disease: DiseaseRiskItem,
    record: EvidenceRecord,
    *,
    subject_age: int | None,
) -> list[SuggestedEvaluation]:
    """Recover concrete tests explicitly present in one disease source.

    The catalog supplies nomenclature only. A row is emitted only when the
    retrieved title/text contains an alias for the exact disease, an alias for
    the exact test and clinical diagnostic/assessment wording. Therefore the
    catalog cannot create evidence or a recommendation by itself.
    """

    specifications = _SOURCE_BOUND_DIAGNOSTIC_TESTS.get(disease.name, ())
    if not specifications:
        return []
    source_text = " ".join(
        dict.fromkeys(
            part.strip()
            for part in (record.title, record.summary or "", record.evidence_text or "")
            if part and part.strip()
        )
    )
    disease_aliases = _SOURCE_BOUND_DISEASE_ALIASES.get(
        disease.name,
        (disease.name,),
    )
    purpose_markers = (
        "诊断",
        "筛查",
        "评估",
        "分期",
        "监测",
        "检查",
        "检测",
        "测定",
        "测量",
        "分类标准",
        "分类条件",
        "diagnos",
        "confirm",
        "screen",
        "evaluat",
        "assess",
        "detect",
        "monitor",
        "staging",
        "classification criteria",
        "entry criterion",
    )
    title_lowered = record.title.casefold()
    title_scopes_disease = any(
        alias and alias.casefold() in title_lowered for alias in disease_aliases
    )
    source_segments = [
        segment.strip(" ，,")
        for segment in re.split(r"(?<=[.!?。！？；;])\s*|[\r\n]+", source_text)
        if segment.strip(" ，,")
    ]
    proposed: list[SuggestedEvaluation] = []
    for display_name, aliases, default_purpose in specifications:
        # A title that explicitly names the disease, concrete test and that it
        # is a guideline/criterion is itself a local source binding.  Do not,
        # however, let a generic "guideline" word in the title validate a test
        # mentioned much later and for another purpose in a long text block.
        title_binds_pair = bool(
            title_scopes_disease
            and any(alias.casefold() in title_lowered for alias in aliases if alias)
            and any(
                marker in title_lowered
                for marker in ("guideline", "指南", "criteria", "标准")
            )
        )
        matching_segment = record.title if title_binds_pair else next(
            (
                segment
                for segment in source_segments
                if _source_segment_binds_disease_test(
                    segment,
                    disease_aliases=disease_aliases,
                    test_aliases=aliases,
                    purpose_markers=purpose_markers,
                    title_scopes_disease=title_scopes_disease,
                )
            ),
            None,
        )
        if matching_segment is None:
            continue
        # Purpose belongs to the concrete test, not to an unrelated verb
        # elsewhere in a long guide block. For example, a CKD guide can discuss
        # population screening while eGFR in the same block is used for renal
        # function assessment and staging.
        final_purpose = (
            "评估" if disease.name == "糖化血红蛋白" else default_purpose
        )
        rationale = (
            "用于复核糖化血红蛋白（HbA1c）水平。"
            if disease.name == "糖化血红蛋白"
            else f"用于筛查{disease.name}。"
            if final_purpose == "筛查"
            else f"用于评估{disease.name}。"
            if final_purpose == "评估"
            else f"用于辅助{disease.name}诊断。"
        )
        if not evaluation_is_public_for_disease(
            display_name,
            rationale=rationale,
            disease=disease,
            subject_age=subject_age,
        ):
            continue
        proposed.append(
            SuggestedEvaluation(
                name=display_name,
                rationale=rationale,
                purpose=final_purpose,
            )
        )
    return proposed


def _source_segment_binds_disease_test(
    segment: str,
    *,
    disease_aliases: tuple[str, ...],
    test_aliases: tuple[str, ...],
    purpose_markers: tuple[str, ...],
    title_scopes_disease: bool = False,
) -> bool:
    """Require the disease-test-purpose relation in one local source unit."""

    lowered = segment.casefold()
    disease_hits = [
        lowered.find(alias.casefold())
        for alias in disease_aliases
        if alias and alias.casefold() in lowered
    ]
    test_hits = [
        lowered.find(alias.casefold())
        for alias in test_aliases
        if alias and alias.casefold() in lowered
    ]
    if (not disease_hits and not title_scopes_disease) or not test_hits:
        return False
    if disease_hits and min(
        abs(left - right) for left in disease_hits for right in test_hits
    ) > 320:
        return False
    # Do not count a purpose word that is only part of the test's own name.
    # For example, "homeostasis model assessment (HOMA-IR) was calculated"
    # describes a research method; it does not recommend HOMA-IR as a clinical
    # evaluation for the report disease.  A genuine source sentence retains a
    # separate verb after aliases are masked ("HOMA-IR was used to assess...").
    purpose_context = lowered
    for alias in sorted(test_aliases, key=len, reverse=True):
        if alias:
            purpose_context = purpose_context.replace(alias.casefold(), " ")
    return any(marker in purpose_context for marker in purpose_markers)


def _diagnostic_record_priority(
    record: EvidenceRecord,
    *,
    disease: DiseaseRiskItem,
) -> float:
    """Prefer a disease-specific clinical test over a generic workflow item."""

    interpretation = record.interpretation
    if interpretation is None:
        return -1000.0
    score = record.relevance * 10
    score += {
        "direct": 40,
        "contextual": 10,
        "unclear": 0,
        "not_applicable": -100,
    }.get(interpretation.patient_applicability, 0)
    score += {"high": 20, "moderate": 10, "low": 0}.get(
        interpretation.confidence,
        0,
    )
    for evaluation in interpretation.recommended_evaluations:
        text = f"{evaluation.name} {evaluation.rationale}"
        if disease.name in text:
            score += 50
        if any(
            marker in text
            for marker in ("诊断", "确认", f"检测{disease.name}", "敏感性", "特异性")
        ):
            score += 25
        if "尚无金标准" in text:
            score -= 30
    return score


def _suggested_evaluation_priority(
    evaluation: SuggestedEvaluation,
    *,
    disease: DiseaseRiskItem,
) -> float:
    """Choose one clinically closest visible check deterministically.

    Retrieval and validation may keep several internal candidates. Public
    output favours an explicit disease-matched purpose, a core/initial test,
    and then an already supplied result. Ties preserve the higher-quality
    source/model order established before this final comparison.
    """

    text = f"{evaluation.name} {evaluation.rationale}"
    score = {
        "确认": 30.0,
        "筛查": 25.0,
        "评估": 20.0,
    }.get(evaluation.purpose, 0.0)
    if disease.name in text:
        score += 40.0
    if any(marker in text for marker in ("首选", "初始", "核心", "金标准")):
        score += 30.0
    if any(marker in text for marker in ("诊断", "确认", "筛查", "风险分层")):
        score += 15.0
    return score


def _public_evaluation_purpose(value: str) -> str:
    return {
        "screen_target": "筛查",
        "confirm_target": "确认",
        "characterize_target": "评估",
    }.get(value, "确认")


def _evaluation_rationale_for_report(
    *,
    disease: DiseaseRiskItem,
    evaluation_name: str = "",
    source_rationale: str,
    purpose: str,
    source_text: str = "",
) -> str | None:
    """Return only an evidence-authored clinical purpose.

    ``purpose`` and ``evaluation_name`` are structured validators; they are
    deliberately not used to synthesize patient-facing prose.
    """
    del purpose
    if any(
        marker in source_text.casefold()
        for marker in (
            "病变分割",
            "分割算法",
            "自动算法",
            "深度学习",
            "自动诊断",
            "计算机辅助诊断",
            "research algorithm",
        )
    ):
        return None
    source_lower = source_text.casefold()
    if disease.name == "焦虑症" and any(
        marker in source_lower
        for marker in (
            "duchenne muscular dystrophy",
            "杜氏肌营养不良",
            "颞下颌关节紊乱",
            "temporomandibular disorders",
        )
    ):
        return None
    rationale = " ".join(source_rationale.split()).strip()
    if disease.name == "腺瘤":
        # A colorectal-cancer screening guide may mention prior adenoma as a
        # high-risk history and list FIT elsewhere. That does not establish
        # FIT as an adenoma-screening test. Require the exact disease-test-
        # purpose relation to be recoverable from one source sentence.
        return _source_bound_evaluation_purpose(
            source_text,
            disease_name=disease.name,
            evaluation_name=evaluation_name,
        )
    if any(
        marker in rationale.casefold()
        for marker in ("人工智能", "machine learning", "deep learning")
    ):
        # Research-only computational augmentation is not a routine clinical
        # evaluation to present to the report recipient.
        return None
    if rationale.startswith(
        (
            "研究中",
            "指南中",
            "共识中",
            "证据中",
            "摘要中",
            "摘要表明",
            "摘要显示",
            "文献",
        )
    ):
        rationale = ""
    rationale = re.sub(
        r"^(?:该|本)?(?:指南|共识|研究|证据|摘要|综述|文件|立场声明|文章|框架)"
        r"(?:明确指出|明确支持|明确推荐|明确|直接支持|直接推荐|"
        r"通常建议|建议|指出|强调|推荐|发现|显示|支持|提及)?"
        r"[，,：:]?\s*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"^(?:明确|直接|通常)?(?:建议|推荐|支持)\s*",
        "",
        rationale,
    ).strip()
    rationale = rationale.replace("该检查用于", "用于", 1)
    rationale = rationale.replace("本检查用于", "用于", 1)
    rationale = re.sub(r"^推荐(?=\S)", "", rationale).strip()
    rationale = re.sub(r"^(?:支持|推荐)用于", "用于", rationale)
    rationale = re.sub(r"^(?:表明|说明|提到|提及)", "", rationale).strip()
    rationale = re.sub(r"^(?:认为|可认为)[,，、:：]?\s*", "", rationale).strip()
    rationale = re.sub(r"^影像学检查方面[，,]?\s*", "", rationale).strip()
    rationale = re.sub(
        r"[，,](?:但)?尚无金标准诊断试验[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,](?:因为|依据)(?:该|本)?(?:指南|共识|研究|证据|摘要)中[^,，。；;]*",
        "",
        rationale,
    ).strip()
    # Preserve the source-authored test and purpose while removing only its
    # editorial wrapper.  These two forms are grammatical equivalents and do
    # not add a new medical inference.
    rationale = re.sub(
        r"^使用(?P<test>[^,，。；;]{1,60}?)(?:进行|用于)"
        r"(?P<goal>[^,，。；;]{2,100})$",
        r"\g<test>用于\g<goal>",
        rationale.rstrip("。"),
    ).strip()
    rationale = re.sub(
        r"^使用(?P<test>[^,，。；;]{1,60}?)(?P<verb>评估|诊断|筛查|确认|检测)"
        r"(?P<goal>[^,，。；;]{2,100})$",
        r"\g<test>用于\g<verb>\g<goal>",
        rationale.rstrip("。"),
    ).strip()
    rationale = re.sub(
        r"^(?P<test>[^,，。；;]{1,60}?)在"
        r"(?P<goal>检测[^,，。；;]{2,60})方面"
        r"(?P<effect>具有[^,，。；;]{2,80})$",
        r"\g<test>用于\g<goal>，\g<effect>",
        rationale.rstrip("。"),
    ).strip()
    rationale = re.sub(
        r"^.*[，,]支持使用(?P<test>[^,，。；;]{1,60}?)(?:进行|用于)"
        r"(?P<goal>[^,，。；;]{2,100})$",
        r"\g<test>用于\g<goal>",
        rationale.rstrip("。"),
    ).strip()
    rationale = re.sub(
        r"^对(?P<goal>[^,，。；;]{2,100})进行评估(?:，以提高诊断准确性)?$",
        r"用于评估\g<goal>",
        rationale.rstrip("。"),
    ).strip()
    if rationale.startswith(("规范了", "规范", "指南规范")):
        rationale = ""
    rationale = re.sub(
        r"^(?:该|本)?解读(?:明确指出|指出|显示|认为)?[，,：:]?\s*",
        "",
        rationale,
    ).strip()
    if rationale.startswith(("和", "与", "及", "并")) or any(
        marker in rationale
        for marker in (
            "研究中",
            "指南中",
            "共识中",
            "证据中",
            "摘要中",
            "文献",
            "共识",
            "指南",
            "声明",
            "涵盖",
            "支持相关检查",
            "重新评估",
            "标准化了",
            "引入",
            "新技术",
            "评估了",
            "详细介绍",
            "摘要明确",
        )
    ):
        rationale = ""
    if rationale.startswith(("其用于", "其可用于", "其有助于")):
        rationale = ""
    if rationale.startswith(("需进行", "需要进行", "应进行")):
        rationale = ""
    if rationale.startswith("建议"):
        rationale = ""
    if rationale.startswith(("列出", "归纳", "总结")):
        rationale = ""
    if any(
        marker in rationale.casefold()
        for marker in (
            "分割",
            "分割算法",
            "自动算法",
            "深度学习",
            "诊断标志物的潜力",
            "research algorithm",
        )
    ):
        rationale = ""
    if "列为" in rationale and any(
        marker in rationale for marker in ("常用", "推荐", "方法", "检查")
    ):
        rationale = ""
    if rationale.casefold().startswith(("risk", "风险", "以提高", "为提高")):
        rationale = ""
    rationale = re.sub(
        r"[，,](?:并|从而)?[^。；;]{0,40}(?:指导治疗|指导用药|指导管理)[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"(?:和|及)(?:治疗|用药|管理)[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,](?:该|本)?[^。；;]{0,20}直接针对[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,](?:该|本)?(?:证据|研究)?(?:直接)?支持该检查[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,](?:与[^，,。；;]{1,60}相比[^，,。；;]*|"
        r"[^，,。；;]{0,60}预测价值较低[^，,。；;]*)",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,]用于验证[^。；;]{0,50}诊断价值[^。；;]*",
        "",
        rationale,
    ).strip()
    rationale = rationale.replace("，支持对", "，用于")
    rationale = rationale.replace("该检查直接用于", "用于")
    rationale = rationale.replace("该方法是适合", "用于评估")
    rationale = re.sub(
        r"^[^,，。；;]{0,30}提供(?P<target>[^,，。；;]{2,40})"
        r"评估的最低标准[,，]?用于确认(?:当前)?目标疾病$",
        r"用于评估\g<target>",
        rationale.rstrip("。"),
    ).strip()
    rationale = re.sub(
        r"[,，]与(?:当前)?目标[^,，。；;]{1,40}"
        r"(?:的诊断)?直接相关",
        "",
        rationale,
    ).strip()
    rationale = re.sub(
        r"[，,](?:该检查)?直接对应(?:当前)?目标疾病[^。；;]*",
        "",
        rationale,
    ).strip()
    detection_rate_match = re.fullmatch(
        r"(?P<test>[^,，。；;]{1,60}?)(?:有助于|可)(?:提高|提升)"
        r"(?P<target>[^,，。；;]{1,80}?)(?:阳性)?检出率",
        rationale.rstrip("。"),
    )
    if (
        detection_rate_match
        and disease.name in source_text
        and evaluation_name in source_text
        and "诊断" in source_text
    ):
        rationale = (
            f"{detection_rate_match.group('test')}用于辅助{disease.name}诊断，"
            "提高疾病检出率"
        )
    rationale = re.sub(
        r"^使用[^,，。；;]{1,50}?(诊断(?:和|与|及)?监测[^,，。；;]*)$",
        r"用于\1",
        rationale,
    )
    if any(
        marker in rationale
        for marker in (
            "提供规范",
            "支持其作为",
            "比较了",
            "讨论了",
            "研究了",
            "性能",
            "标准化方案",
            "诊断价值最高",
            "对RA诊断价值",
        )
    ):
        rationale = ""
    if (
        rationale.count("(") != rationale.count(")")
        or rationale.count("（") != rationale.count("）")
        or rationale.rstrip("。！？!?").endswith(("在诊断", "用于诊断和", "用于评估和"))
    ):
        rationale = ""
    if rationale.rstrip("。") in {
        "诊断",
        "筛查",
        "评估",
        "确认",
        "辅助诊断",
        "用于诊断",
        "用于筛查",
        "用于评估",
        "用于确认",
    }:
        rationale = ""
    base_evaluation_name = re.sub(r"[（(][^）)]*[）)]", "", evaluation_name).strip()
    if (
        rationale
        and (
            rationale.startswith(("用于", "可用于", "有助于", "有利于", "帮助"))
            or (
                base_evaluation_name
                and rationale.startswith(base_evaluation_name)
                and any(
                    marker in rationale
                    for marker in ("可评估", "可用于", "用于", "有助于")
                )
            )
            or bool(
                re.match(
                    r"^[^,，。；;]{1,40}(?:可用于|用于|有助于|有利于)",
                    rationale,
                )
            )
            or bool(
                re.match(
                    r"^[^,，。；;]{1,40}[,，](?:可)?用于",
                    rationale,
                )
            )
            or bool(re.match(r"^该[^,，。；;]{1,24}是评估", rationale))
        )
        and not any(
            marker in rationale
            for marker in (
                "AI",
                "EviMed",
                "检索",
                "规则",
                "模板",
                "JSON",
                "系统生成",
                "系统状态",
                "系统规则",
            )
        )
    ):
        return rationale.rstrip("。") + "。"
    return _source_bound_evaluation_purpose(
        source_text,
        disease_name=disease.name,
        evaluation_name=evaluation_name,
    )


def _verified_evaluation_purpose(
    *,
    disease: DiseaseRiskItem,
    evaluation: EvidenceRecommendedEvaluation,
    source_text: str,
) -> str | None:
    """Render a compact purpose only for an already source-bound extraction.

    Model-authored rationales can be discarded by the patient-text cleaner
    even though their verbatim ``source_phrase`` and structured purpose remain
    valid.  Requiring the phrase to occur in the retrieved source prevents an
    empty rationale from erasing that otherwise verified hospital check.
    """

    phrase = " ".join(evaluation.source_phrase.split()).strip()
    if not phrase:
        return None
    normalized_source = "".join(
        character for character in source_text.casefold() if character.isalnum()
    )
    normalized_phrase = "".join(
        character for character in phrase.casefold() if character.isalnum()
    )
    if not normalized_phrase or normalized_phrase not in normalized_source:
        return None
    return {
        "screen_target": f"用于筛查{disease.name}。",
        "confirm_target": f"用于辅助{disease.name}诊断。",
        "characterize_target": f"用于评估{disease.name}。",
    }.get(evaluation.purpose)


def _source_bound_evaluation_purpose(
    source_text: str,
    *,
    disease_name: str,
    evaluation_name: str,
) -> str | None:
    """Extract a test purpose from the EviMed title/summary without inference."""

    text = " ".join(source_text.split()).strip()
    lowered_text = text.casefold()
    if (
        disease_name == "癫痫"
        and _evaluation_identity(evaluation_name) == "brain-mri"
        and "epilepsy" in lowered_text
        and "mri of the brain" in lowered_text
        and "diagnos" in lowered_text
    ):
        return "脑部MRI用于辅助癫痫诊断。"
    base_evaluation_name = re.sub(r"[（(][^）)]*[）)]", "", evaluation_name).strip()
    evaluation_terms = tuple(
        dict.fromkeys(
            term
            for term in (
                evaluation_name,
                base_evaluation_name,
                re.sub(r"(?:检查|检测|测定|扫描)$", "", base_evaluation_name),
            )
            if term
        )
    )
    if (
        not text
        or not evaluation_name
        or not any(name in text for name in evaluation_terms)
    ):
        return None
    if any(
        marker in text.casefold()
        for marker in (
            "病变分割",
            "分割算法",
            "自动算法",
            "深度学习",
            "自动诊断",
            "计算机辅助诊断",
            "research algorithm",
        )
    ):
        return None
    for sentence in re.split(r"[。；;]", text):
        sentence = sentence.strip(" ，,")
        if not sentence:
            continue
        if (
            disease_name in text
            and any(name in sentence for name in evaluation_terms)
            and "首选方法" in sentence
        ):
            return sentence.rstrip("。") + "。"
        if disease_name in sentence and any(
            name in sentence for name in evaluation_terms
        ):
            effect_match = re.search(
                rf"在检测{re.escape(disease_name)}方面"
                r"(?P<effect>具有[^，,。；;]{2,80})",
                sentence,
            )
            if effect_match:
                effect = effect_match.group("effect").strip(" ，,")
                return f"{evaluation_name}用于检测{disease_name}，{effect}。"
        if disease_name not in sentence:
            continue
        if any(name in sentence for name in evaluation_terms) and any(
            marker in sentence
            for marker in (
                "诊断中的价值",
                "诊断价值",
                "实验室诊断",
                "用于诊断",
                "用于辅助诊断",
                "筛查和诊断",
                "筛查与诊断",
            )
        ):
            return f"用于辅助{disease_name}诊断。"
        match = re.search(
            r"(?:实验室|相关|这些|上述)?检查"
            r"(?:对于|可用于|用于|有助于)"
            r"(?P<purpose>(?:明确|评估|判断|区分|识别|筛查|确认)[^。；;]{2,100})",
            sentence,
        )
        if not match:
            continue
        purpose_text = match.group("purpose").strip(" ，,")
        purpose_text = re.sub(r"(?:具有)?重要意义$", "", purpose_text).strip(" ，,")
        purpose_text = re.sub(
            r"(?:和|及|并)?(?:指导|评价)?治疗(?:效果)?[^，,。；;]*$",
            "",
            purpose_text,
        ).strip(" ，,")
        if purpose_text and disease_name in purpose_text:
            return f"用于{purpose_text.rstrip('。')}。"
    return None


def _clear_confirmation_evidence(disease: DiseaseRiskItem) -> None:
    disease.confirmation_evidence_refs = []
    disease.evidence_refs = sorted(set(disease.association_evidence_refs))


def _evaluation_matches_disease_item(
    name: str,
    *,
    rationale: str,
    disease: DiseaseRiskItem,
) -> bool:
    if disease.item_type not in {"biomarker", "phenotype"}:
        return True
    normalized = f"{name} {rationale}".casefold()
    aliases = {
        "糖化血红蛋白": (
            "糖化血红蛋白",
            "hba1c",
            "glycated haemoglobin",
            "glycated hemoglobin",
        ),
        "空腹血糖异常": (
            "空腹血糖",
            "空腹血浆葡萄糖",
            "fpg",
            "fasting plasma glucose",
        ),
    }
    accepted = aliases.get(disease.name, (disease.name,))
    return any(alias.casefold() in normalized for alias in accepted)


def evaluation_is_public_for_disease(
    name: str,
    *,
    rationale: str,
    disease: DiseaseRiskItem,
    subject_age: int | None = None,
) -> bool:
    """Require a concrete test whose modality fits the exact report item."""
    if not is_hospital_common_examination(name, rationale=rationale):
        return False
    normalized_name = "".join(name.casefold().split())
    if any(
        marker in normalized_name
        for marker in (
            "量表",
            "问卷",
            "日记",
            "自评",
            "自我评估",
            "家庭记录",
            "症状记录",
            "排便记录",
            "bristol",
            "patient-reported",
            "self-report",
            "researchonly",
            "研究专用",
        )
    ):
        return False
    if not _evaluation_matches_disease_item(
        name,
        rationale=rationale,
        disease=disease,
    ) or not _is_concrete_evaluation(name, disease_name=disease.name):
        return False
    normalized_rationale = "".join(rationale.casefold().split())
    population_text = f"{name} {rationale}".casefold()
    if any(
        marker in population_text
        for marker in (
            "模型验证",
            "模型分组",
            "研究分组",
            "研究设计",
            "用于分组",
            "协助分组",
            "研究终点",
            "参考标准",
            "纳入标准",
            "validation of the model",
            "model validation",
            "reference standard",
            "research endpoint",
            "study design",
        )
    ):
        # A test used as a research reference standard or model-validation
        # label is not automatically the most appropriate clinical next step
        # for an asymptomatic report recipient.
        return False
    if subject_age is not None and (
        (
            subject_age < 65
            and any(
                marker in population_text
                for marker in ("老年", "老年人", "geriatric", "older adult")
            )
        )
        or (
            subject_age >= 18
            and any(
                marker in population_text
                for marker in ("儿童", "小儿", "患儿", "pediatric", "paediatric")
            )
        )
    ):
        return False
    if any(
        marker in normalized_rationale
        for marker in (
            "用于排除",
            "以排除",
            "用来排除",
            "ruleout",
            "toexclude",
        )
    ):
        # A differential-exclusion test may be clinically appropriate, but it
        # does not support or characterize this report's exact disease row.
        # With a maximum of three public rows, reserve the slots for tests that
        # directly screen, confirm, stage, or characterize the target.
        return False
    # The modality itself must match the requested disease.  Rationale text
    # can mention a target while proposing an unrelated test (for example EEG
    # for depression), so it must not be used to make the modality pass.
    normalized = name.casefold().replace(" ", "")
    if disease.name == "便秘" and any(
        marker in normalized for marker in ("结肠镜", "肠镜", "colonoscopy")
    ):
        return False
    if disease.name in {
        "炎症性肠病",
        "溃疡性结肠炎",
        "克罗恩病",
    } and any(
        marker in normalized
        for marker in (
            "食管胃十二指肠镜",
            "胃十二指肠镜",
            "上消化道内镜",
            "胃镜",
        )
    ):
        # These rows require a disease-specific lower-GI or small-bowel
        # evaluation.  ``十二指肠镜`` must not pass merely because its name
        # happens to contain the substring ``肠镜``.
        return False
    if disease.name in {
        "炎症性肠病",
        "溃疡性结肠炎",
        "克罗恩病",
    } and any(
        marker in normalized
        for marker in ("皮肤活检", "皮损活检", "skinbiopsy", "dermat")
    ):
        # Generic ``biopsy`` matching previously allowed dermatology evidence
        # to enter an inflammatory-bowel-disease row.  Only gastrointestinal
        # mucosal/pathology evaluations belong here.
        return False
    if disease.name == "硬化性胆管炎" and any(
        marker in normalized
        for marker in (
            "ercp",
            "内镜逆行胰胆管",
            "内镜逆行性胰胆管",
            "内镜下逆行胰胆管",
            "内镜下逆行性胰胆管",
        )
    ):
        return False
    if disease.name == "硬化性胆管炎" and "胆管癌" in rationale:
        return False
    if disease.name == "高脂血症" and "胰腺炎" in rationale:
        return False
    if disease.name == "冠心病" and any(
        marker in f"{name} {rationale}".casefold()
        for marker in (
            "msimi",
            "mental stress",
            "精神压力",
            "糖尿病患者",
            "diabetic patients",
            "patients with diabetes",
        )
    ):
        # A special-population or mental-stress protocol is not a general
        # diagnostic path for this report's coronary-heart-disease row.
        return False
    if (
        disease.name == "腺瘤"
        and any(marker in rationale for marker in ("结直肠癌", "结肠癌"))
        and "腺瘤" not in rationale
    ):
        # FIT can participate in colorectal-neoplasia screening, but a source
        # purpose that names cancer only must not be relabelled as support for
        # this separate adenoma row.
        return False


    if disease.name == "多发性硬化症" and any(
        marker in rationale.casefold()
        for marker in (
            "nmosd",
            "视神经脊髓炎",
            "neuromyelitis optica",
            "aqp4",
        )
    ):
        return False
    if disease.name == "多发性硬化症" and "7t" in normalized:
        return False
    if (
        disease.name == "精神分裂症"
        and "panss" in normalized
        and any(marker in rationale for marker in ("诊断", "筛查", "确认"))
    ):
        # PANSS quantifies symptom severity; it does not establish the diagnosis.
        return False
    target_markers = {
        "艰难梭菌感染": (
            "gdh抗原",
            "谷氨酸脱氢酶",
            "毒素检测",
            "快速检测试剂盒",
        ),
        "炎症性肠病": (
            "结肠镜",
            "肠镜",
            "消化内镜",
            "钙卫蛋白",
            "病理",
            "活检",
            "colonoscopy",
            "calprotectin",
            "biopsy",
        ),
        "溃疡性结肠炎": (
            "结肠镜",
            "肠镜",
            "钙卫蛋白",
            "病理",
            "活检",
            "colonoscopy",
            "calprotectin",
            "biopsy",
        ),
        "克罗恩病": (
            "cte",
            "mre",
            "磁共振肠道成像",
            "结肠镜",
            "肠镜",
            "钙卫蛋白",
            "病理",
            "活检",
            "calprotectin",
            "biopsy",
        ),
        "非酒精性脂肪肝": (
            "肝脏超声",
            "肝脏弹性",
            "瞬时弹性",
            "肝功能",
            "alt",
            "ast",
            "fibroscan",
            "elastography",
            "fib-4",
            "fib4",
            "肝活检",
            "肝病理",
            "肝脏影像",
            "磁共振弹性",
            "vcte",
            "mre",
        ),
        "肝硬化": (
            "肝脏超声",
            "肝脏弹性",
            "瞬时弹性",
            "肝功能",
            "血小板",
            "fibroscan",
            "elastography",
        ),
        "高尿酸血症": (
            "血尿酸",
            "尿酸测定",
            "serumurate",
            "uricacid",
        ),
        "阿尔兹海默症": (
            "认知量表",
            "神经心理",
            "mmse",
            "moca",
            "脑部mri",
            "头颅mri",
            "脑部磁共振",
            "头颅磁共振",
            "磁共振成像",
        ),
        "阿尔茨海默病": (
            "认知量表",
            "神经心理",
            "mmse",
            "moca",
            "脑部mri",
            "头颅mri",
            "脑部磁共振",
            "头颅磁共振",
            "磁共振成像",
        ),
        "帕金森病": (
            "神经系统检查",
            "updrs",
            "dat",
            "多巴胺转运体",
            "脑部mri",
            "经颅超声",
            "颅脑超声",
            "神经心理",
        ),
        "多发性硬化症": (
            "脑部mri",
            "脊髓mri",
            "mri检查",
            "磁共振",
            "脑脊液",
            "寡克隆带",
        ),
        "类风湿性关节炎": (
            "类风湿因子",
            "抗ccp",
            "血沉",
            "crp",
            "关节超声",
            "关节x线",
        ),
        "强直性脊柱炎": (
            "骶髂关节mri",
            "骶髂关节ct",
            "骶髂关节x线",
            "hla-b27",
            "骶髂关节磁共振",
            "磁共振成像",
        ),
        "结直肠癌": (
            "结肠镜",
            "肠镜",
            "粪便隐血",
            "fit",
            "病理",
            "活检",
            "ct结肠成像",
        ),
        "腺瘤": (
            "结肠镜",
            "肠镜",
            "粪便隐血",
            "fit",
            "病理",
            "活检",
            "ct结肠成像",
        ),
        "甲状腺癌": (
            "甲状腺超声",
            "细针穿刺",
            "fna",
            "细胞学",
            "病理",
        ),
        "甲状腺疾病": (
            "甲状腺功能",
            "促甲状腺激素",
            "tsh",
            "ft4",
            "ft3",
            "游离甲状腺素",
            "thyroid function",
        ),
        "焦虑症": (
            "gad-7",
            "gad7",
            "焦虑量表",
            "结构化访谈",
            "临床访谈",
        ),
        "精神分裂症": (
            "结构化访谈",
            "临床访谈",
            "panss",
            "精神状态检查",
        ),
        "腹泻型肠易激综合征": (
            "rome",
            "罗马iv",
            "布里斯托",
            "症状量表",
            "症状问卷",
            "钙卫蛋白",
            "calprotectin",
            "lactoferrin",
            "乳铁蛋白",
            "贾第虫",
            "giardia",
            "乳糜泻",
            "胆汁酸",
        ),
        "肥胖": ("体质量指数", "bmi", "腰围"),
        "肥胖症": ("体质量指数", "bmi", "腰围", "体脂"),
        "高脂血症": (
            "血脂",
            "总胆固醇",
            "甘油三酯",
            "低密度脂蛋白",
            "ldl",
            "hdl",
        ),
        "高血压": ("诊室血压", "家庭血压", "动态血压", "血压测量", "abpm"),
        "自身免疫性肝炎": (
            "自身抗体",
            "ana",
            "sma",
            "免疫球蛋白igg",
            "肝活检",
            "肝病理",
        ),
        "肾结石": (
            "泌尿系超声",
            "无增强ct",
            "非增强ct",
            "非对比螺旋ct",
            "无增强螺旋ct",
            "非增强螺旋ct",
            "尿液检查",
            "尿常规",
        ),
        "胃肠炎": (
            "粪便培养",
            "粪便细菌培养",
            "粪便病原菌培养",
            "粪便病原体核酸",
            "stoolculture",
            "faecalculture",
            "fecalculture",
            "multiplexpcr",
        ),
        "食物中毒": (
            "粪便病原学",
            "粪便培养",
            "粪便病原菌培养",
            "粪便病原体核酸",
            "stooltest",
            "stooltesting",
            "stoolculture",
            "faecalculture",
            "fecalculture",
            "multiplexpcr",
        ),
        "菌血症": ("血培养", "bloodculture"),
        "胃癌": ("胃镜", "上消化道内镜", "病理", "活检"),
        "肝癌": (
            "肝脏超声",
            "afp",
            "甲胎蛋白",
            "增强ct",
            "增强mri",
            "对比增强",
            "增强磁共振",
            "钆塞酸",
            "造影剂增强超声",
        ),
        "睡眠障碍": (
            "睡眠日记",
            "体动记录",
            "活动记录",
            "睡眠量表",
            "睡眠问卷",
            "多导睡眠",
            "actigraph",
            "sleepdiary",
            "questionnaire",
        ),
        "系统性红斑狼疮": (
            "抗核抗体",
            "抗dsdna",
            "抗sm抗体",
            "补体",
            "尿常规",
            "ana",
            "antibody",
            "complement",
        ),
        "慢性肾病": (
            "egfr",
            "肌酐",
            "尿白蛋白",
            "尿常规",
            "肾功能",
            "albuminuria",
            "creatinine",
        ),
        "II型糖尿病": (
            "空腹血糖",
            "空腹血浆葡萄糖",
            "糖化血红蛋白",
            "hba1c",
            "口服葡萄糖耐量",
            "ogtt",
        ),
        "冠心病": (
            "冠状动脉ct",
            "冠脉ct",
            "冠状动脉造影",
            "心电图",
            "运动负荷",
            "coronaryct",
            "angiograph",
        ),
        "糖化血红蛋白": ("糖化血红蛋白", "hba1c"),
        "腹胀和排气": (
            "罗马",
            "rome",
            "呼气试验",
            "症状量表",
            "症状问卷",
        ),
        "胰岛素抵抗": (
            "homa",
            "tyg",
            "胰岛素抵抗指数",
            "胰岛素夹",
            "高胰岛素",
            "空腹胰岛素",
        ),
        "癫痫": (
            "脑电图",
            "eeg",
            "癫痫核磁",
            "癫痫mri",
            "脑部mri",
            "头颅mri",
            "脑部磁共振",
            "头颅磁共振",
            "磁共振成像",
        ),
        "便秘": (
            "布里斯托",
            "结肠传输",
            "肛门直肠测压",
            "结肠镜",
            "大便性状量表",
            "rome",
        ),
        "抑郁症": (
            "phq",
            "hamd",
            "抑郁量表",
            "抑郁问卷",
            "结构化访谈",
            "临床访谈",
        ),
        "空腹血糖异常": (
            "空腹血糖",
            "空腹血浆葡萄糖",
            "fastingplasmaglucose",
            "fpg",
        ),
        "硬化性胆管炎": (
            "mrcp",
            "mri",
            "磁共振成像",
            "磁共振胆管",
            "磁共振胰胆管",
            "肝活检",
            "胆管造影",
        ),
    }
    allowed = target_markers.get(disease.name)
    return allowed is None or any(marker in normalized for marker in allowed)


def is_hospital_common_examination(
    name: str | None,
    *,
    rationale: str | None = None,
    category_confirmed: bool = False,
) -> bool:
    """Admit hospital-orderable tests, not self-observation instruments."""

    normalized = "".join((name or "").casefold().split())
    context = f"{normalized} {rationale or ''}".casefold()
    if not normalized or any(
        marker in context
        for marker in (
            "量表",
            "问卷",
            "日记",
            "自评",
            "自我评估",
            "家庭记录",
            "症状记录",
            "排便记录",
            "结构化临床访谈",
            "罗马iv标准",
            "罗马ⅳ标准",
            "bristol",
            "patient-reported",
            "self-report",
            "researchonly",
            "研究专用",
        )
    ):
        return False
    modality_markers = (
        "检查",
        "检测",
        "检验",
        "测定",
        "培养",
        "试验",
        "监测",
        "成像",
        "造影",
        "超声",
        "磁共振",
        "x线",
        "内镜",
        "胃镜",
        "肠镜",
        "活检",
        "病理",
        "心电图",
        "脑电图",
        "肺功能",
        "测压",
        "抗原",
        "抗体",
        "血清",
        "血浆",
        "血糖",
        "血脂",
        "血常规",
        "尿常规",
        "粪便",
        "肌酐",
        "尿酸",
        "胆固醇",
        "胰岛素",
        "蛋白",
        "激素",
        "神经心理",
        "bmi",
        "腰围",
        "ct",
        "mri",
        "mre",
        "mrcp",
        "ccta",
        "eeg",
        "ecg",
        "pcr",
        "hba1c",
        "hla",
        "uacr",
        "egfr",
        "ogtt",
        "fit",
        "fnab",
        "psg",
        "dxa",
        "dexa",
        "fib-4",
        "fib4",
        "homa-ir",
        "homair",
    )
    # A constrained model response may supply a routine examination category.
    # The legacy name heuristics remain for callers without that classification;
    # the exclusions above always apply, including to classified candidates.
    return category_confirmed or any(marker in normalized for marker in modality_markers)


def _evaluation_rationale_targets_disease(
    rationale: str,
    *,
    disease: DiseaseRiskItem,
) -> bool:
    normalized = "".join(rationale.casefold().split())
    if not normalized:
        return False
    return any(
        len(alias) >= 2 and alias.casefold().replace(" ", "") in normalized
        for alias in _disease_binding_aliases(disease)
    )


def _disease_binding_aliases(disease: DiseaseRiskItem) -> tuple[str, ...]:
    return disease_binding_aliases(disease.name)


def disease_binding_aliases(name: str) -> tuple[str, ...]:
    """Return normalized aliases for binding evidence to one disease row."""

    disease_name = "".join(name.casefold().split())
    variants = {disease_name}
    for suffix in ("综合征", "疾病", "症", "病"):
        if disease_name.endswith(suffix) and len(disease_name) > len(suffix) + 1:
            variants.add(disease_name[: -len(suffix)])
    variants.update(
        alias.casefold()
        for alias in _SOURCE_BOUND_DISEASE_ALIASES.get(name, ())
        if alias
    )
    if disease_name.startswith("ii型"):
        variants.add("2型" + disease_name.removeprefix("ii型"))
    return tuple(sorted(variants, key=len, reverse=True))


def _is_concrete_evaluation(name: str, *, disease_name: str) -> bool:
    normalized = "".join(name.split())
    if not normalized or normalized in {
        "检查",
        "检测",
        "筛查",
        "评估",
        "诊断",
        "临床评估",
        "综合评估",
        f"{disease_name}检查",
        f"{disease_name}检测",
        f"{disease_name}筛查",
        f"{disease_name}评估",
    }:
        return False
    concrete_markers = (
        "CT",
        "MRI",
        "MRCP",
        "PET",
        "eGFR",
        "HOMA",
        "HbA1c",
        "HLA",
        "FIB-4",
        "OGTT",
        "FIT",
        "PCR",
        "GDH",
        "FNA",
        "核磁",
        "磁共振",
        "超声",
        "经颅超声",
        "弹性成像",
        "造影",
        "内镜",
        "肠镜",
        "胃镜",
        "活检",
        "病理",
        "培养",
        "核酸",
        "毒素检测",
        "抗原检测",
        "抗体",
        "补体",
        "血糖",
        "血压",
        "BMI",
        "体质量指数",
        "腰围",
        "血脂",
        "胆固醇",
        "甘油三酯",
        "脂蛋白",
        "尿酸",
        "甲胎蛋白",
        "寡克隆带",
        "类风湿因子",
        "抗CCP",
        "葡萄糖",
        "糖化血红蛋白",
        "甲状腺功能",
        "促甲状腺激素",
        "TSH",
        "FT4",
        "FT3",
        "胰岛素夹",
        "胰岛素抵抗指数",
        "肌酐",
        "尿白蛋白",
        "尿常规",
        "肝功能",
        "肾功能",
        "心电图",
        "冠脉",
        "运动负荷",
        "脑电图",
        "多导睡眠",
        "睡眠日记",
        "体动记录",
        "活动记录",
        "量表",
        "问卷",
        "评分",
        "神经心理",
        "罗马",
        "Rome",
        "测压",
        "传输试验",
        "钙卫蛋白",
        "乳铁蛋白",
        "贾第虫",
        "乳糜泻",
        "胆汁酸",
        "隐血",
        "培养",
        "病原学",
    )
    return any(
        marker.casefold() in normalized.casefold() for marker in concrete_markers
    )


def _evaluation_identity(name: str) -> str:
    normalized = "".join(name.casefold().split())
    aliases = (
        (
            ("mrcp", "磁共振胆管造影", "磁共振胰胆管成像", "磁共振胆管成像"),
            "mrcp",
        ),
        (("脑mri", "脑部mri", "头颅mri", "mri检查"), "brain-mri"),
        (("细针穿刺", "fnab", "fna"), "fine-needle-aspiration"),
        (("hba1c", "糖化血红蛋白"), "hba1c"),
        (
            (
                "甲状腺功能",
                "促甲状腺激素",
                "thyroidfunction",
                "thyroid-stimulatinghormone",
                "thyroidstimulatinghormone",
                "tsh",
                "ft4",
            ),
            "thyroid-function",
        ),
        (("homa-ir", "homair", "胰岛素抵抗指数"), "homa-ir"),
        (("egfr", "估算肾小球滤过率", "肾小球滤过率"), "egfr"),
        (("钙卫蛋白", "calprotectin"), "fecal-calprotectin"),
        (
            (
                "粪便免疫化学",
                "粪便隐血",
                "fecalimmunochemical",
                "faecalimmunochemical",
                "fit）",
                "fit)",
            ),
            "fecal-immunochemical-test",
        ),
        (("uacr", "尿白蛋白/肌酐", "尿白蛋白肌酐比"), "uacr"),
        (("ogtt", "口服葡萄糖耐量"), "ogtt"),
        (
            ("fastingglucose", "fastingplasmaglucose", "空腹血糖", "空腹血浆葡萄糖"),
            "fpg",
        ),
        (("colonoscopy", "结肠镜", "肠镜"), "colonoscopy"),
        (("electroencephal", "脑电图"), "eeg"),
        (("actigraph", "体动记录"), "actigraphy"),
        (("抗核抗体", "ana检测"), "ana"),
        (("类风湿因子",), "rf"),
        (("抗ccp抗体", "环瓜氨酸肽抗体"), "anti-ccp"),
        (("冠状动脉ct", "冠脉ct", "coronaryct", "ccta"), "coronary-ct"),
        (
            (
                "非增强螺旋ct",
                "非对比螺旋ct",
                "无增强螺旋ct",
                "非增强ct",
                "非对比ct",
            ),
            "noncontrast-renal-ct",
        ),
    )
    for markers, identity in aliases:
        if any(marker in normalized for marker in markers):
            return identity
    return normalized


def match_existing_results(
    disease_name: str,
    hospital_data: list[HospitalDataInput],
) -> list[ClinicalResult]:
    terms = _matching_terms(disease_name)
    results: list[ClinicalResult] = []
    for item in hospital_data:
        haystack = f"{item.item_name} {item.exam_result or ''}".casefold()
        if not any(term.casefold() in haystack for term in terms):
            continue
        if disease_name in {
            "结直肠癌",
            "腺瘤",
            "炎症性肠病",
            "溃疡性结肠炎",
            "克罗恩病",
        }:
            result_text = (item.exam_result or "").casefold()
            if "胃" in item.item_name and not any(
                term in result_text
                for term in ("结直肠", "结肠", "回肠", "肠黏膜", "肠粘膜")
            ):
                continue
        if item.data_type == "EXAM":
            result = next(
                (
                    line.strip()
                    for line in (item.exam_result or "").splitlines()
                    if line.strip()
                ),
                "",
            )
        else:
            value = str(item.test_result_value or "")
            result = (
                f"{value}（参考范围：{item.reference_range}）"
                if item.reference_range
                else value
            )
        results.append(
            ClinicalResult(
                item_name=item.item_name,
                result=result,
                result_time=item.report_time or item.check_time,
                status=item.result_status_name,
            )
        )
    return results


def _public_category(taxon: ParsedTaxon) -> str:
    if taxon.category == "beneficial":
        return "beneficial_abnormal"
    if taxon.category == "pathogenic":
        return "pathogenic_signal"
    if taxon.category == "opportunistic":
        return "opportunistic_signal"
    if taxon.category == "fungi_pathogenic":
        return "fungi_pathogenic_signal"
    return "other_abnormal"


def _source_status(taxon: ParsedTaxon) -> str:
    if taxon.source_status:
        return taxon.source_status
    if taxon.detection_status == "trace":
        return "痕量检出"
    if taxon.detection_status == "detected":
        return "检出信号"
    return "原报告异常"


def _matching_terms(disease_name: str) -> list[str]:
    terms = [disease_name]
    for canonical, aliases in _CLINICAL_MATCH_TERMS.items():
        if canonical in disease_name or disease_name in canonical:
            terms.extend(aliases)
    return list(dict.fromkeys(terms))


def _disease_item_type(name: str) -> str:
    if name in {"腹胀和排气", "腹泻", "便秘"}:
        return "symptom"
    if name in {"糖化血红蛋白", "空腹血糖异常"}:
        return "biomarker"
    if name in {"胰岛素抵抗"}:
        return "phenotype"
    if name in {"ASD", "双向情感障碍"}:
        return "unclear"
    return "disease"


def _number(value: str | None) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else None


def _dedupe(items: Iterable, *, key):
    results = []
    seen = set()
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        results.append(item)
    return results


_CLINICAL_MATCH_TERMS = {
    "结直肠癌": ("肠镜", "结直肠", "病理"),
    "腺瘤": ("肠镜", "结直肠", "病理"),
    "炎症性肠病": ("肠镜", "结肠镜", "粪钙卫蛋白", "CRP"),
    "溃疡性结肠炎": ("肠镜", "结肠镜", "粪钙卫蛋白", "CRP"),
    "克罗恩病": ("肠镜", "结肠镜", "粪钙卫蛋白", "CRP"),
    "便秘": ("便秘", "排便"),
    "胃癌": ("胃镜", "病理"),
    "II型糖尿病": ("血糖", "糖化血红蛋白", "HbA1c"),
    "空腹血糖异常": ("血糖", "糖化血红蛋白", "HbA1c"),
    "高脂血症": ("总胆固醇", "低密度脂蛋白", "甘油三酯", "HDL", "LDL"),
    "高血脂": ("总胆固醇", "低密度脂蛋白", "甘油三酯", "HDL", "LDL"),
    "高尿酸": ("血尿酸", "尿酸"),
    "肾脏疾病": (
        "血肌酐",
        "肌酐",
        "eGFR",
        "肾小球滤过率",
        "尿素氮",
        "尿白蛋白",
        "尿蛋白",
    ),
    "慢性肾病": (
        "血肌酐",
        "肌酐",
        "eGFR",
        "肾小球滤过率",
        "尿素氮",
        "尿白蛋白",
        "尿蛋白",
    ),
    "心脑血管疾病": (
        "收缩压",
        "舒张压",
        "血压",
        "常规心电图",
        "心电图",
    ),
    "高血压": ("收缩压", "舒张压", "血压"),
    "甲状腺疾病": (
        "甲状腺功能",
        "促甲状腺激素",
        "TSH",
        "FT4",
        "FT3",
        "游离甲状腺素",
        "甲状腺超声",
    ),
    "肥胖": ("BMI", "体重指数", "腰围", "体脂率", "体脂"),
    "非酒精性脂肪肝": (
        "非酒精性脂肪肝",
        "非酒精性脂肪性肝病",
        "代谢相关脂肪性肝病",
        "脂肪肝",
        "肝脏超声",
        "肝脏彩超",
        "谷丙转氨酶",
        "丙氨酸氨基转移酶",
        "谷草转氨酶",
        "天门冬氨酸氨基转移酶",
        "AST/ALT",
    ),
    "酒精性脂肪肝": (
        "酒精性脂肪肝",
        "酒精性脂肪性肝病",
        "肝脏超声",
        "肝脏彩超",
        "谷丙转氨酶",
        "丙氨酸氨基转移酶",
        "谷草转氨酶",
        "天门冬氨酸氨基转移酶",
        "AST/ALT",
    ),
    # A normal resting ECG does not establish or exclude coronary artery
    # disease.  Only disease-specific coronary or ischemia findings are
    # attached to this model item.
    "冠心病": ("冠脉", "冠状动脉", "心肌缺血"),
}


# Names and aliases only. These entries are never emitted unless one retained
# EviMed diagnostic source contains the exact disease, test, and a diagnostic
# or assessment marker; see ``_source_bound_fallback_evaluations``.
_SOURCE_BOUND_DISEASE_ALIASES = {
    "艰难梭菌感染": (
        "艰难梭菌感染",
        "clostridioides difficile infection",
        "clostridium difficile infection",
        "c. difficile infection",
        "cdi",
    ),
    "睡眠障碍": ("sleep disorder", "sleep disorders", "insomnia"),
    "系统性红斑狼疮": (
        "系统性红斑狼疮",
        "systemic lupus erythematosus",
        "systemic lupus",
    ),
    "甲状腺疾病": (
        "甲状腺疾病",
        "甲状腺功能异常",
        "甲状腺功能障碍",
        "thyroid disease",
        "thyroid diseases",
        "thyroid disorder",
        "thyroid disorders",
        "thyroid dysfunction",
    ),
    "冠心病": (
        "冠心病",
        "coronary heart disease",
        "coronary artery disease",
    ),
    "慢性肾病": ("慢性肾病", "慢性肾脏病", "chronic kidney disease", "ckd"),
    "克罗恩病": ("克罗恩病", "crohn disease", "crohn's disease"),
    "糖化血红蛋白": ("糖化血红蛋白", "hba1c", "hemoglobin a1c"),
    "腹胀和排气": ("腹胀", "排气", "abdominal bloating", "flatulence"),
    "胰岛素抵抗": ("胰岛素抵抗", "insulin resistance"),
    "癫痫": ("癫痫", "epilepsy"),
    "便秘": ("便秘", "constipation"),
    "抑郁症": ("抑郁症", "抑郁障碍", "depression", "depressive disorder"),
    "空腹血糖异常": (
        "空腹血糖异常",
        "空腹血糖受损",
        "impaired fasting glucose",
        "impaired fasting glycaemia",
    ),
    "非酒精性脂肪肝": (
        "非酒精性脂肪肝",
        "非酒精性脂肪性肝病",
        "代谢相关脂肪性肝病",
        "nonalcoholic fatty liver disease",
        "non-alcoholic fatty liver disease",
        "fatty liver disease",
        "steatotic liver disease",
        "metabolic dysfunction-associated steatotic liver disease",
        "nafld",
        "masld",
    ),
    "硬化性胆管炎": ("硬化性胆管炎", "sclerosing cholangitis"),
    "炎症性肠病": ("炎症性肠病", "inflammatory bowel disease", "ibd"),
    "炎症性肠道疾病": (
        "炎症性肠道疾病",
        "炎症性肠病",
        "inflammatory bowel disease",
        "ibd",
    ),
    "溃疡性结肠炎": ("溃疡性结肠炎", "ulcerative colitis"),
    "结直肠癌": (
        "结直肠癌",
        "结肠癌",
        "直肠癌",
        "colorectal cancer",
        "colon cancer",
        "rectal cancer",
        "crc",
    ),
    "肾结石": (
        "肾结石",
        "泌尿系结石",
        "kidney stone",
        "kidney stones",
        "renal stone",
        "renal stones",
        "nephrolithiasis",
        "urolithiasis",
    ),
    "胃肠炎": (
        "胃肠炎",
        "感染性胃肠炎",
        "gastroenteritis",
        "infectious gastroenteritis",
    ),
    "食物中毒": (
        "食物中毒",
        "感染性腹泻",
        "food poisoning",
        "foodborne illness",
        "infectious diarrhea",
        "infectious diarrhoea",
        "acute gastroenteritis",
        "急性胃肠炎",
        "salmonellosis",
    ),
    "菌血症": (
        "菌血症",
        "血流感染",
        "血液感染",
        "bacteremia",
        "bacteraemia",
        "bloodstream infection",
        "bloodstream infections",
        "blood stream infection",
        "blood stream infections",
    ),
    "高血脂": (
        "高血脂",
        "高脂血症",
        "hyperlipidemia",
        "dyslipidemia",
        "hypercholesterolemia",
    ),
    "心脑血管疾病": (
        "心脑血管疾病",
        "心血管疾病",
        "cardiovascular disease",
        "cerebrovascular disease",
        "cvd",
    ),
}


_SOURCE_BOUND_DIAGNOSTIC_TESTS = {
    "艰难梭菌感染": (
        (
            "艰难梭菌GDH抗原及毒素检测",
            (
                "gdh抗原",
                "谷氨酸脱氢酶",
                "毒素检测",
                "毒素检测试剂盒",
                "cd快速检测试剂盒",
            ),
            "筛查",
        ),
    ),
    "睡眠障碍": (
        ("睡眠日记", ("睡眠日记", "sleep diary"), "评估"),
        (
            "体动记录（Actigraphy）",
            ("体动记录", "actigraphy", "actigraphic"),
            "评估",
        ),
        (
            "多导睡眠监测（PSG）",
            ("多导睡眠", "polysomnography", "polysomnogram"),
            "确认",
        ),
    ),
    "系统性红斑狼疮": (
        (
            "抗核抗体（ANA）检测",
            ("抗核抗体", "antinuclear antibody", "ana testing"),
            "筛查",
        ),
        (
            "抗双链DNA（anti-dsDNA）抗体检测",
            ("抗dsdna", "anti-dsdna", "double-stranded dna antibody"),
            "确认",
        ),
        (
            "补体C3/C4检测",
            ("补体c3", "补体c4", "complement c3", "complement c4"),
            "评估",
        ),
    ),
    "甲状腺疾病": (
        (
            "甲状腺功能检查（TSH、FT4）",
            (
                "甲状腺功能检查",
                "甲状腺功能检测",
                "甲状腺功能测定",
                "促甲状腺激素",
                "thyroid-stimulating hormone",
                "thyroid stimulating hormone",
                "tsh",
                "ft4",
                "free thyroxine",
            ),
            "评估",
        ),
    ),
    "冠心病": (
        (
            "冠状动脉CT血管成像（CCTA）",
            (
                "冠状动脉ct",
                "冠脉ct",
                "coronary computed tomography angiography",
                "coronary ct angiography",
                "ccta",
            ),
            "评估",
        ),
        (
            "运动负荷心电图",
            ("运动负荷心电图", "exercise electrocardiogram", "exercise ecg"),
            "评估",
        ),
    ),
    "慢性肾病": (
        (
            "尿白蛋白/肌酐比值（UACR）",
            ("uacr", "尿白蛋白/肌酐", "尿白蛋白肌酐比", "白蛋白尿"),
            "评估",
        ),
        (
            "估算肾小球滤过率（eGFR）",
            ("egfr", "估算肾小球滤过率", "肾小球滤过率"),
            "评估",
        ),
        ("血清肌酐检测", ("血清肌酐", "serum creatinine"), "评估"),
    ),
    "克罗恩病": (
        (
            "磁共振肠道成像（MRE）",
            ("mre", "磁共振肠道成像", "magnetic resonance enterography"),
            "评估",
        ),
        (
            "CT肠道成像（CTE）",
            ("cte", "ct肠道成像", "computed tomography enterography"),
            "评估",
        ),
        ("结肠镜检查", ("结肠镜", "colonoscopy"), "确认"),
    ),
    "糖化血红蛋白": (
        (
            "糖化血红蛋白（HbA1c）检测",
            ("hba1c", "糖化血红蛋白", "hemoglobin a1c"),
            "确认",
        ),
    ),
    "腹胀和排气": (
        ("罗马IV标准评估", ("罗马iv", "rome iv", "romeiv"), "评估"),
        (
            "氢-甲烷呼气试验",
            ("氢呼气", "甲烷呼气", "hydrogen breath", "methane breath"),
            "评估",
        ),
        ("腹胀症状量表", ("腹胀量表", "症状量表", "symptom scale"), "评估"),
    ),
    "胰岛素抵抗": (
        (
            "HOMA-IR指数",
            (
                "homeostasis model assessment",
                "homa-ir",
                "homa ir",
                "homair",
                "胰岛素抵抗指数",
            ),
            "评估",
        ),
        (
            "高胰岛素-正常血糖钳夹试验",
            (
                "高胰岛素正常血糖钳夹",
                "hyperinsulinemic-euglycemic clamp",
                "euglycemic clamp",
            ),
            "评估",
        ),
        ("空腹胰岛素检测", ("空腹胰岛素", "fasting insulin"), "评估"),
    ),
    "癫痫": (
        ("脑电图（EEG）检查", ("脑电图", "electroencephalography", "eeg"), "确认"),
        (
            "头颅MRI检查",
            ("头颅mri", "脑部mri", "brain mri", "mri of the brain"),
            "评估",
        ),
    ),
    "便秘": (
        ("布里斯托大便性状量表", ("布里斯托", "bristol stool"), "评估"),
        ("结肠传输试验", ("结肠传输", "colonic transit"), "评估"),
        ("肛门直肠测压", ("肛门直肠测压", "anorectal manometry"), "评估"),
    ),
    "抑郁症": (
        ("PHQ-9量表", ("phq-9", "phq9"), "筛查"),
        ("汉密尔顿抑郁量表（HAMD）", ("hamd", "汉密尔顿抑郁"), "评估"),
        ("结构化临床访谈", ("结构化临床访谈", "structured clinical interview"), "确认"),
    ),
    "空腹血糖异常": (
        (
            "空腹血糖（FPG）检测",
            ("空腹血糖", "空腹血浆葡萄糖", "fasting plasma glucose", "fpg"),
            "确认",
        ),
    ),
    "非酒精性脂肪肝": (
        (
            "肝脏超声检查",
            (
                "肝脏超声",
                "肝脏彩超",
                "腹部超声",
                "liver ultrasound",
                "hepatic ultrasound",
                "abdominal ultrasound",
                "abdominal ultrasonography",
            ),
            "评估",
        ),
        (
            "肝脏瞬时弹性成像",
            (
                "肝脏瞬时弹性",
                "肝脏弹性成像",
                "fibroscan",
                "transient elastography",
                "vibration-controlled transient elastography",
                "vcte",
                "liver stiffness",
            ),
            "评估",
        ),
    ),
    "硬化性胆管炎": (
        ("磁共振胰胆管成像（MRCP）", ("mrcp", "磁共振胰胆管", "磁共振胆管"), "确认"),
    ),
    "炎症性肠病": (
        ("结肠镜检查", ("结肠镜", "colonoscopy"), "确认"),
        (
            "粪便钙卫蛋白检测",
            ("粪钙卫蛋白", "钙卫蛋白", "fecal calprotectin", "faecal calprotectin"),
            "评估",
        ),
    ),
    "炎症性肠道疾病": (
        ("结肠镜检查", ("结肠镜", "colonoscopy"), "确认"),
        (
            "粪便钙卫蛋白检测",
            ("粪钙卫蛋白", "钙卫蛋白", "fecal calprotectin", "faecal calprotectin"),
            "评估",
        ),
    ),
    "溃疡性结肠炎": (
        ("结肠镜检查", ("结肠镜", "colonoscopy"), "确认"),
        (
            "粪便钙卫蛋白检测",
            ("粪钙卫蛋白", "钙卫蛋白", "fecal calprotectin", "faecal calprotectin"),
            "评估",
        ),
    ),
    "结直肠癌": (
        ("结肠镜检查", ("结肠镜", "colonoscopy"), "确认"),
        (
            "粪便免疫化学检测（FIT）",
            (
                "粪便免疫化学检测",
                "粪便免疫化学试验",
                "粪便隐血",
                "fecal immunochemical test",
                "faecal immunochemical test",
                "fit testing",
            ),
            "筛查",
        ),
    ),
    "肾结石": (
        (
            "泌尿系超声检查",
            (
                "泌尿系超声",
                "肾脏超声",
                "renal ultrasound",
                "ultrasound",
                "ultrasonography",
            ),
            "评估",
        ),
        (
            "非增强螺旋CT检查",
            (
                "非增强螺旋ct",
                "无增强螺旋ct",
                "非对比螺旋ct",
                "noncontrast ct",
                "non-contrast ct",
                "unenhanced ct",
            ),
            "确认",
        ),
        ("尿常规检查", ("尿常规", "urinalysis"), "评估"),
    ),
    "胃肠炎": (
        (
            "粪便病原菌培养",
            (
                "粪便培养",
                "粪便细菌培养",
                "粪便病原菌培养",
                "stool culture",
                "faecal culture",
                "fecal culture",
            ),
            "确认",
        ),
        (
            "粪便病原体核酸检测",
            (
                "粪便病原体核酸",
                "胃肠道病原体核酸",
                "gastrointestinal pathogen pcr",
                "multiplex pcr",
            ),
            "确认",
        ),
    ),
    "食物中毒": (
        (
            "粪便病原学检查",
            (
                "粪便病原学检查",
                "粪便病原学检测",
                "stool test",
                "stool testing",
            ),
            "确认",
        ),
        (
            "粪便病原菌培养",
            (
                "粪便培养",
                "stool culture",
                "faecal culture",
                "fecal culture",
                "bacterial and viral culture",
                "selectively cultured",
            ),
            "确认",
        ),
        (
            "粪便病原体核酸检测",
            (
                "粪便病原体核酸",
                "胃肠道病原体核酸",
                "gastrointestinal pathogen pcr",
                "multiplex pcr",
            ),
            "确认",
        ),
    ),
    "菌血症": (
        (
            "血培养",
            ("血培养", "blood culture", "blood cultures"),
            "确认",
        ),
    ),
    "高血脂": (
        (
            "血脂检测",
            (
                "血脂检测",
                "血脂谱",
                "lipid profile",
                "ldl-c",
                "low-density lipoprotein cholesterol",
            ),
            "评估",
        ),
    ),
    "心脑血管疾病": (
        (
            "血压测量",
            ("血压测量", "blood pressure measurement", "blood pressure"),
            "评估",
        ),
        (
            "血脂检测",
            ("血脂检测", "血脂谱", "lipid profile", "ldl-c"),
            "评估",
        ),
    ),
}


_TAXON_CHINESE_NAMES = {
    "salmonella enterica": "肠沙门菌",
    "treponema vincentii": "文森密螺旋体",
    "clostridium botulinum": "肉毒梭菌",
    "campylobacter coli": "结肠弯曲菌",
    "legionella pneumophila": "嗜肺军团菌",
    "clostridium perfringens": "产气荚膜梭菌",
    "plesiomonas shigelloides": "类志贺邻单胞菌",
    "streptococcus suis": "猪链球菌",
    "cronobacter sakazakii": "阪崎克罗诺杆菌",
    "treponema denticola": "齿垢密螺旋体",
    "aeromonas hydrophila": "嗜水气单胞菌",
    "shigella flexneri": "福氏志贺菌",
    "shigella dysenteriae": "痢疾志贺菌",
    "mycobacterium tuberculosis": "结核分枝杆菌",
    "burkholderia gladioli": "唐菖蒲伯克霍尔德菌",
    "aeromonas caviae": "豚鼠气单胞菌",
    "enterobacter cloacae": "阴沟肠杆菌",
    "pseudomonas fulva": "黄褐假单胞菌",
    "bacteroides fragilis": "脆弱拟杆菌",
    "escherichia coli": "大肠埃希菌",
    "pseudomonas fluorescens": "荧光假单胞菌",
    "veillonella parvula": "小韦荣球菌",
    "pseudomonas stutzeri": "施氏假单胞菌",
    "streptococcus pneumoniae": "肺炎链球菌",
    "bacillus cereus": "蜡样芽孢杆菌",
    "fusobacterium nucleatum": "具核梭杆菌",
    "bacteroides vulgatus": "普通拟杆菌",
    "pseudomonas aeruginosa": "铜绿假单胞菌",
    "clostridioides difficile": "艰难梭菌",
    "klebsiella pneumoniae": "肺炎克雷伯菌",
    "thermothelomyces": "嗜热毁丝霉属",
    "debaryomyces": "德巴利酵母属",
    "fusarium": "镰刀菌属",
    "cryptococcus": "隐球菌属",
    "colletotrichum higginsianum": "希金斯炭疽菌",
    "pyricularia pennisetigena": "稻瘟菌属相关种",
    "puccinia striiformis": "条形柄锈菌",
    "candida albicans": "白念珠菌",
    "aspergillus": "曲霉属",
    "malassezia": "马拉色菌属",
    "drechmeria": "德雷克霉属",
    "drechmeria coniospora": "产孢德雷克霉",
    "colletotrichum": "炭疽菌属",
    "ogataea": "小形酵母属",
    "debaryomyces hansenii": "汉逊德巴利酵母",
    "pyricularia": "稻瘟菌属",
    "candida": "念珠菌属",
}
