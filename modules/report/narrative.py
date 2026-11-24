"""Constrained model-assisted prose generation.

The model receives immutable IDs and may only attach prose to those IDs.
Deterministic fallbacks are limited to source facts and already validated
evidence, so an unavailable model cannot remove structured report content.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from modules.report.clinical import (
    _evaluation_identity,
    disease_has_patient_microbiome_signal,
    is_hospital_common_examination,
)
from modules.report.schemas import (
    AbnormalMicrobesSection,
    DiseaseRisksSection,
    EvidenceCondition,
    EvidenceInterpretation,
    EvidenceRecommendedEvaluation,
    EvidenceRecord,
    HealthAssessment,
    HealthLevel,
    HealthManagementSection,
    HealthRecommendation,
    HealthRecommendedExamination,
    HospitalDataInput,
    ParsedMicrobiomeReport,
    SuggestedEvaluation,
)
from modules.report.performance import increment_report_metric
from modules.report.terminology import (
    patient_facing_name,
    patient_facing_text,
    patient_health_topic_domain,
    patient_health_topic_is_generic,
    patient_topic_key,
    patient_topics_overlap,
)

EXECUTIVE_SUMMARY_MIN = 150
EXECUTIVE_SUMMARY_MAX = 250


class NarrativeError(RuntimeError):
    pass


class NarrativeClient(Protocol):
    async def generate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class _CommonExamination(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    disease_id: str = Field(alias="diseaseId")
    name: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=240)
    purpose: Literal["筛查", "确认", "评估"]
    hospital_common: bool = Field(alias="hospitalCommon", strict=True)
    already_available: bool = Field(alias="alreadyAvailable", strict=True)
    applicable: bool = Field(strict=True)
    category: Literal[
        "laboratory", "ultrasound", "noncontrast_imaging",
        "physiological_test", "clinical_exam",
    ] | None = None


async def supplement_common_examinations(
    client: NarrativeClient,
    *,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    subject: Any,
    session_context: Any = None,
) -> list[str]:
    """Preserve accepted checks; repair only rejected/missing rows once."""
    validation_errors: dict[str, str] = {}
    missing = await _supplement_common_examinations_batch(
        client, diseases=diseases, hospital_data=hospital_data,
        subject=subject, session_context=session_context,
        validation_errors=validation_errors,
    )
    if not missing:
        return []
    retry_diseases = DiseaseRisksSection(items=[
        item for item in diseases.items if item.disease_id in missing
    ])
    try:
        return await _supplement_common_examinations_batch(
            client, diseases=retry_diseases, hospital_data=hospital_data,
            subject=subject, session_context=session_context, retry=True,
            validation_errors=validation_errors,
        )
    except Exception:
        # A failed repair must not discard the first batch's valid results.
        increment_report_metric("common_exam.repair_failed")
        return missing


async def _supplement_common_examinations_batch(
    client: NarrativeClient,
    *,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    subject: Any,
    session_context: Any = None,
    retry: bool = False,
    validation_errors: dict[str, str] | None = None,
) -> list[str]:
    """One batch for empty rows; never create disease facts or citations."""
    targets = {
        item.disease_id: item for item in diseases.items
        if not item.suggested_evaluations and (
            (item.source_type == "clinical_context" and item.existing_results)
            or (disease_has_patient_microbiome_signal(item)
                and item.medical_interpretation and item.association_evidence_refs)
        )
    }
    if not targets:
        return []
    increment_report_metric("common_exam.batch")
    result = await client.generate({
        "task": "supplement_common_examinations",
        "subject": {"age": subject.age, "sex": subject.sex},
        "hospitalData": [item.model_dump(mode="json", by_alias=True) for item in hospital_data],
        "sessionContext": session_context,
        **({"validationErrors": {identifier: (validation_errors or {}).get(identifier, "missing_result")
                                for identifier in targets}} if retry else {}),
        "diseases": [{
            "diseaseId": item.disease_id, "name": item.display_name or item.name,
            "sourceType": item.source_type, "sourceResult": item.source_result,
            "medicalInterpretation": item.medical_interpretation,
            "existingResults": [value.model_dump(mode="json", by_alias=True)
                                for value in item.existing_results],
        } for item in targets.values()],
        "rules": [
            *(["本次仅修复上次缺失或未通过校验的项目；逐个返回输入ID，检查只能是一项，不使用加号或顿号组合。若无适用新增检查返回examination:null。"] if retry else []),
            "只补充输入疾病的检查，不新增疾病或诊断，不推断个人患病概率。",
            "每个diseaseId最多推荐一项医院常见检查，使用简短通用名称，不组合多项检查。",
            "category如实选择laboratory（常规实验室检验）、ultrasound（超声）、noncontrast_imaging（非造影影像）、physiological_test（生理功能检查）、clinical_exam（临床体格检查）；不是这些类型的常见医院项目返回examination:null，不为通过校验虚报分类或改写检查实质。",
            "purpose只能是筛查、确认、评估三个固定值之一；检查用途的完整句子写入reason，不要写入purpose。",
            "这是基于医学知识的检查建议，不是联网搜索，不要求文献，不生成证据编号或声称指南支持。",
            "结合年龄、性别、现有结果和会话事实判断；资料未提供不等于正常或无症状。",
            "排除已经完成的同类检查（包括同义名称及检查套餐已含项目），不要无理由重复筛查。",
            "优先常规检验、超声等低负担项目，不推荐量表、问卷、研究项目、穿刺、活检、内镜（包括喉镜）或造影。",
            "reason只说明该检查评估什么，不把菌群关联当确诊，不写必须检查；hospitalCommon、alreadyAvailable、applicable必须如实判断。",
            "不能给出合理新增检查时，该diseaseId返回examination:null；不能为了填满而编造检查。",
        ],
        "outputSchema": {"items": [{"diseaseId": "原ID", "examination": {
            "name": "检查名称", "purpose": "评估", "reason": "检查用途",
            "hospitalCommon": True, "alreadyAvailable": False, "applicable": True,
            "category": "laboratory",
        }}]},
    })
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise NarrativeError("common examination response must contain items")
    completed: set[str] = set()
    duplicate_ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for row in result["items"]:
        if (not isinstance(row, dict) or not isinstance(row.get("diseaseId"), str)
                or row["diseaseId"] not in targets):
            continue
        identifier = row["diseaseId"]
        if identifier in by_id:
            duplicate_ids.add(identifier)
        by_id[identifier] = row
    for identifier, row in by_id.items():
        def reject(reason: str) -> None:
            increment_report_metric(f"common_exam.{reason}")
            if validation_errors is not None:
                validation_errors[identifier] = reason

        if identifier in duplicate_ids:
            reject("duplicate_id")
            continue
        if "examination" in row and row["examination"] is None:
            increment_report_metric("common_exam.no_additional_test")
            completed.add(identifier)
            continue
        try:
            data = row["examination"]
            if not isinstance(data, dict):
                reject("invalid_shape")
                continue
            # Models sometimes expand a purpose label into a sentence. Keep
            # the declared category, not a new medical assertion or enum.
            data = dict(data)
            purpose = data.get("purpose")
            if isinstance(purpose, str):
                for category in ("筛查", "确认", "评估"):
                    if purpose.strip().startswith(category):
                        data["purpose"] = category
                        break
            exam = _CommonExamination.model_validate({**data, "diseaseId": identifier})
        except (KeyError, ValidationError):
            reject("invalid_shape")
            continue
        disease = targets[identifier]
        existing = {_evaluation_identity(item.item_name) for item in hospital_data}
        existing.update(_evaluation_identity(item.item_name) for item in disease.existing_results)
        if not exam.hospital_common or not exam.applicable:
            reject("not_applicable")
            continue
        if exam.already_available or _evaluation_identity(exam.name) in existing:
            reject("already_available")
            continue
        if not is_hospital_common_examination(
            exam.name, rationale=exam.reason, category_confirmed=exam.category is not None,
        ):
            reject("not_common")
            continue
        if any(term in exam.name.casefold() for term in (
                    "穿刺", "活检", "内镜", "肠镜", "胃镜", "喉镜", "造影", "核素", "pet",
                    "biopsy", "endoscop", "colonoscopy", "laryngoscop", "angiograph", "、", ";", "；", "+",
                )):
            reject("invasive_or_multiple")
            continue
        # Ignore all model-supplied evidence/provenance fields.
        disease.suggested_evaluations = [SuggestedEvaluation(
            name=patient_facing_name(exam.name) or exam.name,
            rationale=patient_facing_text(exam.reason) or exam.reason,
            purpose=exam.purpose, evidence_refs=[], recommendation_source="model",
        )]
        completed.add(identifier)
        increment_report_metric("common_exam.accepted")
    return [identifier for identifier in targets if identifier not in completed]


async def _generate_retry_payloads(
    client: NarrativeClient,
    payloads: list[dict[str, Any]],
    *,
    concurrency: int = 2,
) -> list[Any]:
    """Run item-level prose repairs without overloading one model client.

    A large report can need several independent microbe repairs. Sending all
    of them through the same client at once caused every request to time out
    together, after which the callback hid every otherwise eligible row. A
    small semaphore keeps the calls independent while still allowing useful
    overlap. Exceptions remain per-item results so one failed organism never
    cancels successful siblings.
    """

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(payload: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await client.generate(payload)

    return await asyncio.gather(
        *(run(payload) for payload in payloads),
        return_exceptions=True,
    )


class _NarrativeModel(BaseModel):
    # Model prose is untrusted.  Unknown fields are ignored and only the
    # explicitly declared allowlist is carried forward.
    model_config = ConfigDict(extra="ignore")


class MicrobeNarrative(_NarrativeModel):
    microbe_id: str = Field(alias="microbeId")
    clinical_role: str | None = Field(
        default=None, alias="clinicalRole", max_length=800
    )
    conclusion: str = Field(min_length=1, max_length=1200)
    patient_interpretation: str | None = Field(
        default=None, alias="patientInterpretation", max_length=1200
    )
    clinical_confirmation: str | None = Field(
        default=None, alias="clinicalConfirmation", max_length=800
    )
    generation_source: Literal["model", "fallback"] = Field(
        default="fallback", exclude=True
    )


class DiseaseNarrative(_NarrativeModel):
    disease_id: str = Field(alias="diseaseId")
    related_content: str = Field(alias="relatedContent", min_length=1, max_length=1600)
    patient_suggestion: str | None = Field(
        default=None,
        alias="patientSuggestion",
        max_length=800,
    )


class AbnormalIndicatorSummaryDraft(_NarrativeModel):
    indicator_code: str = Field(alias="indicatorCode")
    clinical_significance: str = Field(
        alias="clinicalSignificance",
        min_length=1,
        max_length=800,
    )


class AssessmentExplanationDraft(_NarrativeModel):
    code: str
    explanation: str = Field(min_length=1, max_length=800)


class HealthLevelDraft(_NarrativeModel):
    level: str
    summary: str = Field(min_length=1, max_length=1200)
    basis: list[str] = Field(default_factory=list)


class RecommendationDraft(_NarrativeModel):
    category: str
    topic: str | None = Field(default=None, max_length=80)
    priority: str | None = Field(default=None, max_length=20)
    content: str = Field(min_length=1, max_length=800)
    evidence_refs: list[int] = Field(default_factory=list, alias="evidenceRefs")


class RecommendedExaminationDraft(_NarrativeModel):
    candidate_id: str = Field(alias="candidateId")
    reason: str = Field(min_length=1, max_length=800)
    # The model must explicitly judge that the evidence-bound candidate is a
    # routinely orderable hospital examination. Missing/false means reject.
    hospital_common: bool = Field(default=False, alias="hospitalCommon")
    # These values are filled from the canonical candidate after validation;
    # model-provided names, purposes and citations are never trusted.
    name: str | None = None
    purpose: Literal["筛查", "确认", "评估"] | None = None
    evidence_refs: list[int] = Field(default_factory=list, alias="evidenceRefs")


class NarrativeDraft(_NarrativeModel):
    executive_summary: str | None = Field(
        default=None, alias="executiveSummary", max_length=2400
    )
    abnormal_microbes_summary: str | None = Field(
        default=None, alias="abnormalMicrobesSummary", max_length=1600
    )
    microbe_narratives: list[MicrobeNarrative] = Field(
        default_factory=list, alias="microbeNarratives"
    )
    disease_narratives: list[DiseaseNarrative] = Field(
        default_factory=list, alias="diseaseNarratives"
    )
    health_level: HealthLevelDraft | None = Field(default=None, alias="healthLevel")
    health_profile: str | None = Field(
        default=None, alias="healthProfile", max_length=1600
    )
    recommendations: list[RecommendationDraft] = Field(default_factory=list)
    recommended_examinations: list[RecommendedExaminationDraft] = Field(
        default_factory=list,
        alias="recommendedExaminations",
    )
    assessment_explanations: list[AssessmentExplanationDraft] = Field(
        default_factory=list,
        alias="assessmentExplanations",
    )
    abnormal_indicator_summaries: list[AbnormalIndicatorSummaryDraft] = Field(
        default_factory=list,
        alias="abnormalIndicatorSummaries",
    )


class EvidenceInterpretationDraft(_NarrativeModel):
    number: int
    claim: str = Field(min_length=1, max_length=1000)
    population: str | None = Field(default=None, max_length=300)
    study_design: str | None = Field(default=None, alias="studyDesign", max_length=200)
    direction: str = "not_stated"
    effect: str | None = Field(default=None, max_length=500)
    applicability: str | None = Field(default=None, max_length=500)
    limitations: str | None = Field(default=None, max_length=500)
    confidence: str = "unclear"
    target_alignment: str = Field(default="unclear", alias="targetAlignment")
    clinical_role: str | None = Field(
        default=None, alias="clinicalRole", max_length=500
    )
    conditions: list[EvidenceConditionDraft] = Field(default_factory=list)
    taxon_alignment: str = Field(default="unclear", alias="taxonAlignment")
    sample_alignment: str = Field(default="unclear", alias="sampleAlignment")
    patient_applicability: str = Field(default="unclear", alias="patientApplicability")
    recommended_evaluations: list[EvidenceEvaluationDraft] = Field(
        default_factory=list, alias="recommendedEvaluations"
    )


class EvidenceEvaluationDraft(_NarrativeModel):
    name: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=500)
    source_phrase: str = Field(alias="sourcePhrase", min_length=1, max_length=300)
    purpose: str


class EvidenceConditionDraft(_NarrativeModel):
    name: str = Field(min_length=1, max_length=200)
    search_term: str = Field(alias="searchTerm", min_length=1, max_length=200)
    source_phrase: str = Field(alias="sourcePhrase", min_length=1, max_length=300)
    relation_type: str = Field(alias="relationType")


class EvidenceInterpretationBatch(_NarrativeModel):
    evidence_interpretations: list[EvidenceInterpretationDraft] = Field(
        default_factory=list, alias="evidenceInterpretations"
    )


async def interpret_evidence(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
    batch_size: int = 24,
    patient_context: dict[str, Any] | None = None,
    _retry_missing: bool = True,
    _retry_enrichment: bool = True,
) -> set[int]:
    """Interpret only EviMed title/summary facts and attach claim-level metadata."""
    by_number = {
        numbers[record.evidence_key]: record
        for record in records
        if record.evidence_key in numbers
    }
    completed: set[int] = set()
    ordered = sorted(by_number.items())
    if len(ordered) > max(1, batch_size):
        semaphore = asyncio.Semaphore(6)
        chunks = [
            ordered[offset : offset + max(1, batch_size)]
            for offset in range(0, len(ordered), max(1, batch_size))
        ]

        async def run_chunk(
            chunk: list[tuple[int, EvidenceRecord]], *, retry_size: int
        ) -> set[int]:
            async with semaphore:
                return await interpret_evidence(
                    client,
                    records=[record for _number, record in chunk],
                    numbers=numbers,
                    batch_size=retry_size,
                    patient_context=patient_context,
                    _retry_missing=False,
                )

        for result in await asyncio.gather(
            *(run_chunk(chunk, retry_size=batch_size) for chunk in chunks)
        ):
            completed.update(result)
        if _retry_missing:
            missing = [item for item in ordered if item[0] not in completed]
            missing_chunks = [
                missing[offset : offset + 8] for offset in range(0, len(missing), 8)
            ]
            for result in await asyncio.gather(
                *(run_chunk(chunk, retry_size=8) for chunk in missing_chunks)
            ):
                completed.update(result)
            if _retry_enrichment:
                await _retry_evidence_enrichment(
                    client,
                    records=records,
                    numbers=numbers,
                    patient_context=patient_context,
                )
        return completed
    for offset in range(0, len(ordered), max(1, batch_size)):
        batch = ordered[offset : offset + max(1, batch_size)]
        payload = {
            "task": "逐条解读EviMed返回的指南或文献标题摘要，建立结论与证据对应关系",
            "requiredOutput": {
                "evidenceInterpretations": [
                    {
                        "number": "输入编号",
                        "claim": "该记录直接支持的结论",
                        "population": "摘要明确说明的人群，否则null",
                        "studyDesign": "摘要明确说明的研究设计，否则null",
                        "direction": "increased/decreased/associated/mixed/not_stated",
                        "effect": "摘要明确提供的效应量或结果，否则null",
                        "applicability": "对当前报告结论的适用范围",
                        "limitations": "仅根据标题摘要能够确定的局限",
                        "confidence": "high/moderate/low/unclear",
                        "targetAlignment": "direct/indirect/not_applicable/unclear",
                        "clinicalRole": "标题摘要直接支持的菌种医学属性，否则null",
                        "conditions": [
                            {
                                "name": "规范中文疾病或临床状态名称",
                                "searchTerm": "原文中的英文或中文疾病检索词",
                                "sourcePhrase": "标题或摘要中逐字出现的短语",
                                "relationType": (
                                    "established_pathogen/opportunistic/"
                                    "epidemiologic_association/colonization/"
                                    "environmental_or_dietary/not_applicable"
                                ),
                            }
                        ],
                        "taxonAlignment": "species/genus/strain/toxin_gene/unclear",
                        "sampleAlignment": "stool/mucosa/blood/respiratory/other/unclear",
                        "patientApplicability": "direct/contextual/not_applicable/unclear",
                        "recommendedEvaluations": [
                            {
                                "name": "检查或评估名称",
                                "rationale": "该指南摘要支持它的原因",
                                "sourcePhrase": "从标题或摘要逐字摘取的短语",
                                "purpose": "confirm_target/screen_target/characterize_target",
                            }
                        ],
                    }
                ]
            },
            "evidence": [
                {
                    "number": number,
                    "category": record.category,
                    "title": record.title,
                    "summary": record.summary,
                    "intents": record.intents,
                    "supports": record.supports,
                    "targets": record.targets,
                }
                for number, record in batch
            ],
            "patientContext": patient_context or {},
            "strictConstraints": [
                "只解释输入的标题和摘要，不补充记忆中的论文内容",
                "claim必须明确写出targets中的目标菌种、疾病或健康问题名称；无法写出则返回not_applicable",
                "不得把相关性写成因果关系",
                "标题摘要没有人群、效应量或局限时对应字段返回null",
                "指南仅说明其覆盖的诊断筛查或管理范围，不推导菌种致病性",
                "recommendedEvaluations只用于diagnostic_guideline的指南或人体诊断研究，以及明确病原菌关系的指南；sourcePhrase必须逐字出现在输入标题或摘要中",
                "对disease_clinical_significance，标题摘要直接解释targets所列疾病本身的定义、临床特征、症状、自然病程、一般风险，或明确报告该疾病与人体肠道菌群整体特征的关联时targetAlignment为direct；对diagnostic_guideline，只有直接讨论targets所列疾病本身的筛查、诊断或评估时才为direct；并发症、合并症、治疗、随访、特定罕见亚型均为indirect",
                "对菌种证据，标题或摘要直接讨论targets中的准确物种或属及其临床属性时，targetAlignment为direct；不要因当前样本是粪便而改为not_applicable",
                "patientApplicability用于区分个体直接性：准确菌种的其他人群或其他样本部位临床证据应为contextual，只有非人研究、目标不匹配或讨论的是其他菌种时才为not_applicable",
                "对disease_clinical_significance和diagnostic_guideline，patientApplicability表示证据人群是否适用，不表示患者是否已确诊：普通成人疾病证据不得因患者尚未确诊而标为not_applicable",
                "透析、移植、妊娠、儿童、特定并发症、罕见亚型或另一疾病人群的证据，如与targets不完全一致，targetAlignment不得为direct且patientApplicability应为not_applicable",
                "对于health_management，健康成人指南直接支持targets所列膳食、运动、睡眠或排便习惯主题时，targetAlignment为direct；若targets明确写明受检者已有异常主题（如血脂异常相关膳食调护），同一主题的成人指南或人体干预研究也可为direct；未在targets中指定的其他疾病、妊娠、儿童或产品研究为indirect或not_applicable",
                "health_management的target如果明确为成人便秘相关排便调护，成人便秘指南中直接的生活方式和排便行为建议可标为direct，但不得改写为患者已确诊便秘",
                "示例：高血压在2型糖尿病人群中的筛查、IBD患者艰难梭菌检测、移植受者抑郁评估、克罗恩病肛周癌筛查均不是targets本身的确认路径，必须标为indirect且不输出recommendedEvaluations",
                "recommendedEvaluations只在targetAlignment为direct时输出，且项目必须直接用于确认或筛查targets，不输出并发症、共病、治疗监测或罕见亚型检查",
                "patientContext为成人时，不得将儿童或婴幼儿筛查项目作为适用建议",
                "conditions只从标题摘要逐字存在的疾病、感染、定植或环境饮食来源概念中提取，sourcePhrase必须逐字存在，最多3项",
                "普通物种证据涉及特定菌株、毒素或毒力基因时taxonAlignment必须为strain或toxin_gene，patientApplicability不得标为direct",
                "组织或黏膜富集研究用于粪便检出时patientApplicability标为contextual，不得推导个体患病风险",
                "每个输入编号最多返回一项且不得新增编号",
                "返回严格JSON，不使用Markdown",
            ],
        }
        try:
            raw = await client.generate(payload)
            parsed = EvidenceInterpretationBatch.model_validate(raw)
        except (NarrativeError, ValidationError, ValueError, TypeError):
            continue
        allowed = {number for number, _record in batch}
        seen: set[int] = set()
        for item in parsed.evidence_interpretations:
            if item.number not in allowed or item.number in seen:
                continue
            record = by_number[item.number]
            interpretation = _validated_evidence_interpretation(item, record)
            if interpretation is None:
                continue
            record.interpretation = interpretation
            completed.add(item.number)
            seen.add(item.number)
    if _retry_missing:
        missing_records = [
            record for number, record in ordered if number not in completed
        ]
        if missing_records:
            completed.update(
                await interpret_evidence(
                    client,
                    records=missing_records,
                    numbers=numbers,
                    batch_size=8,
                    patient_context=patient_context,
                    _retry_missing=False,
                )
            )
        if _retry_enrichment:
            await _retry_evidence_enrichment(
                client,
                records=records,
                numbers=numbers,
                patient_context=patient_context,
            )
    return completed


async def _retry_evidence_enrichment(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
    patient_context: dict[str, Any] | None,
) -> None:
    """Run independent gap extractors concurrently after the main retry.

    Each extractor mutates disjoint interpretation fields (evaluations,
    conditions, disease prose, or exact-relation prose).  Running them in
    parallel avoids four serial model waits on large reports while preserving
    the same validation gates and deterministic fallbacks.
    """

    await asyncio.gather(
        _retry_missing_evaluations(
            client,
            records=records,
            numbers=numbers,
            patient_context=patient_context,
        ),
        _retry_missing_conditions(
            client,
            records=records,
            numbers=numbers,
            patient_context=patient_context,
        ),
        _retry_weak_disease_claims(
            client,
            records=records,
            numbers=numbers,
        ),
        _retry_weak_relation_claims(
            client,
            records=records,
            numbers=numbers,
        ),
    )


async def _retry_missing_conditions(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
    patient_context: dict[str, Any] | None,
) -> None:
    raw_candidates = [
        record
        for record in records
        if record.interpretation is not None
        and set(record.intents)
        & {
            "microbe_discovery",
            "microbe_clinical_significance",
            "microbe_association",
            "microbe_disease_association",
        }
        and record.interpretation.target_alignment == "direct"
        and record.evidence_key in numbers
        and (
            not record.interpretation.conditions
            or any(
                not _claim_names_condition(
                    record.interpretation.claim,
                    condition.name,
                )
                for condition in record.interpretation.conditions
            )
        )
    ]
    candidates: list[EvidenceRecord] = []
    per_microbe: dict[str, int] = {}
    for record in sorted(
        raw_candidates,
        key=lambda item: (item.relevance, item.year or 0),
        reverse=True,
    ):
        microbe_id = next(iter(record.microbe_ids), "")
        if not microbe_id or per_microbe.get(microbe_id, 0) >= 3:
            continue
        per_microbe[microbe_id] = per_microbe.get(microbe_id, 0) + 1
        candidates.append(record)

    semaphore = asyncio.Semaphore(4)

    async def run_batch(batch: list[EvidenceRecord]) -> None:
        async with semaphore:
            batch_numbers = [numbers[record.evidence_key] for record in batch]
            by_number = dict(zip(batch_numbers, batch, strict=True))
            payload = {
                "task": "仅从该EviMed标题摘要中补抽取目标菌种明确关联的疾病、感染或不良结局",
                "requiredOutput": {
                    "evidenceInterpretations": [
                        {
                            "number": "输入编号",
                            "claim": "明确写出目标菌种与所抽取疾病或不良结局关系的中文结论",
                            "conditions": [
                                {
                                    "name": "规范中文疾病或不良结局名称",
                                    "searchTerm": "标题摘要逐字出现的检索词",
                                    "sourcePhrase": "标题摘要逐字出现的短语",
                                    "relationType": (
                                        "established_pathogen/opportunistic/"
                                        "epidemiologic_association/colonization/"
                                        "environmental_or_dietary"
                                    ),
                                }
                            ],
                        }
                    ]
                },
                "evidence": [
                    {
                        "number": numbers[record.evidence_key],
                        "title": record.title,
                        "summary": record.summary,
                        "targets": record.targets,
                        "existingClaim": record.interpretation.claim,
                    }
                    for record in batch
                ],
                "patientContext": patient_context or {},
                "strictConstraints": [
                    "只抽取标题或摘要逐字出现的疾病、感染、定植或不良结局",
                    "name必须使用规范中文且不得猜测缩写",
                    "claim必须写出targets中的菌名，并逐项写出conditions中的中文name",
                    "观察性相关不得改写为因果",
                    "每个编号最多返回3项；没有明确结局时返回空数组",
                    "返回严格JSON，不使用Markdown",
                ],
            }
            try:
                raw = await client.generate(payload)
                parsed = EvidenceInterpretationBatch.model_validate(raw)
            except (NarrativeError, ValidationError, ValueError, TypeError):
                return
            for item in parsed.evidence_interpretations:
                record = by_number.get(item.number)
                if record is not None and record.interpretation is not None:
                    conditions = _validated_evidence_conditions(item, record)
                    claim = " ".join(item.claim.split())
                    if (
                        conditions
                        and _safe_medical_prose(claim)
                        and _interpretation_names_target(claim, record=record)
                        and all(
                            _claim_names_condition(claim, condition.name)
                            for condition in conditions
                        )
                        and _numbers_are_grounded(
                            claim,
                            _collect_grounded_numbers(record.title, record.summary),
                        )
                    ):
                        record.interpretation.claim = claim
                        record.interpretation.conditions = conditions

    for _ in range(1):
        remaining = [
            record
            for record in candidates
            if record.interpretation is not None
            and (
                not record.interpretation.conditions
                or any(
                    not _claim_names_condition(
                        record.interpretation.claim,
                        condition.name,
                    )
                    for condition in record.interpretation.conditions
                )
            )
        ]
        if not remaining:
            break
        await asyncio.gather(
            *(
                run_batch(remaining[offset : offset + 8])
                for offset in range(0, len(remaining), 8)
            )
        )


async def _retry_weak_disease_claims(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
) -> None:
    # Import locally to keep the evidence module independent from the model
    # client at module-import time.  A claim can look substantive to the
    # generic validator yet still be rejected by the final patient-facing
    # disease sentence gate (for example because it mixes a disease
    # definition with global ranking or treatment prose).
    from modules.report.evidence import _public_disease_claim

    candidates = [
        record
        for record in records
        if record.interpretation is not None
        and record.interpretation.target_alignment == "direct"
        and "disease_clinical_significance" in record.intents
        and record.evidence_key in numbers
        and (
            not _disease_claim_is_substantive(
                record.interpretation.claim,
                record=record,
            )
            or _public_disease_claim(record) is None
        )
    ]
    if not candidates:
        return
    one_per_disease: list[EvidenceRecord] = []
    disease_counts: dict[tuple[str, ...], int] = {}
    for record in sorted(
        candidates,
        key=_disease_claim_repair_priority,
        reverse=True,
    ):
        scope = tuple(record.disease_ids) or tuple(record.targets)
        # Keep two independent, high-quality sources per disease. Model output
        # can omit one numbered row or preserve an unusable ranking clause;
        # a second overview/clinical-features source gives the row a bounded
        # recovery path without broadening the medical gate.
        if disease_counts.get(scope, 0) >= 2:
            continue
        disease_counts[scope] = disease_counts.get(scope, 0) + 1
        one_per_disease.append(record)
    candidates = one_per_disease
    semaphore = asyncio.Semaphore(4)

    async def run_batch(batch: list[EvidenceRecord]) -> None:
        async with semaphore:
            by_number = {numbers[record.evidence_key]: record for record in batch}
            payload = {
                "task": "仅从EviMed标题摘要中重写疾病或健康问题的医学含义",
                "requiredOutput": {
                    "evidenceInterpretations": [
                        {
                            "number": "输入编号",
                            "claim": "直接说明target的定义、临床特征、病理生理或已有关联风险",
                        }
                    ]
                },
                "evidence": [
                    {
                        "number": number,
                        "title": record.title,
                        "summary": record.summary,
                        "targets": record.targets,
                    }
                    for number, record in by_number.items()
                ],
                "strictConstraints": [
                    "只使用输入标题和摘要，不补充记忆中的医学知识",
                    "claim必须明确写出targets中的目标名称",
                    "不写指南讨论、文献介绍、该证据涉及等元叙述",
                    "不把分类、分期、预测模型、样本构成或研究过程写成医学含义",
                    "优先提取该目标的定义、典型特征、自然病程或已明确的并发风险",
                    "若来源同时写了全球排名、患病率、发病率、地区或治疗进展，只改写来源明确支持的疾病性质，删除排名、比例、地区和治疗内容",
                    "优先解释targets中疾病或健康问题本身的定义、临床特征和一般风险",
                    "若标题摘要明确报告targets与人体肠道菌群或肠道微生物组整体特征的关联，允许将该人群关联写入claim，但不得写成因果或个人患病概率",
                    "不得新增标题摘要未明确写出的具体菌种、代谢物、机制或疾病",
                    "不得写未涉及、未讨论、未提供、证据不足、个体化风险评估等缺失说明或文献评价",
                    "例如来源明确称目标为common cancer时，可改写为该目标是一种常见的恶性肿瘤，不保留全球排名",
                    "不写检查、诊断、筛查、治疗或管理建议",
                    "标题摘要没有实质医学内容时不输出该编号",
                    "返回严格JSON，不使用Markdown",
                ],
            }
            try:
                raw = await client.generate(payload)
                parsed = EvidenceInterpretationBatch.model_validate(raw)
            except (NarrativeError, ValidationError, ValueError, TypeError):
                return
            for item in parsed.evidence_interpretations:
                record = by_number.get(item.number)
                if record is None or record.interpretation is None:
                    continue
                claim = " ".join(item.claim.split())
                if (
                    _safe_medical_prose(claim)
                    and _interpretation_names_target(claim, record=record)
                    and _disease_claim_is_substantive(claim, record=record)
                    and _numbers_are_grounded(
                        claim,
                        _collect_grounded_numbers(record.title, record.summary),
                    )
                ):
                    record.interpretation.claim = claim

    await asyncio.gather(
        *(
            run_batch(candidates[offset : offset + 8])
            for offset in range(0, len(candidates), 8)
        )
    )


def _disease_claim_repair_priority(record: EvidenceRecord) -> tuple[float, ...]:
    """Prefer a general disease source over a newer action-only source."""

    title = " ".join(record.title.casefold().split()).strip(" .:;，。")
    target_aliases = {
        " ".join(alias.casefold().split()).strip(" .:;，。")
        for target in record.targets
        for component in target.split("/")
        for alias in _disease_target_aliases(component.strip())
        if alias.strip()
    }
    exact_title = title in target_aliases
    overview = any(
        marker in title
        for marker in (
            "overview",
            "clinical features",
            "clinical characteristics",
            "symptoms",
            "disease review",
            "疾病概述",
            "临床表现",
            "临床特征",
        )
    )
    action_only = any(
        marker in title
        for marker in (
            "treatment",
            "therapy",
            "management",
            "surgery",
            "surgical",
            "resection",
            "guideline update",
            "治疗",
            "手术",
            "管理",
            "用药",
        )
    )
    return (
        float(exact_title),
        float(overview),
        float(not action_only),
        record.relevance,
        float(record.year or 0),
    )


def _disease_target_aliases(value: str) -> tuple[str, ...]:
    """Reuse the canonical disease aliases without a module-level cycle."""

    from modules.report.evidence import _disease_terms

    return _disease_terms(value)


async def repair_disease_evidence_claims(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
) -> None:
    """Repair only patient-facing disease prose for a bounded fallback batch."""

    # Large model batches can omit one or more numbered rows. A second round
    # sees only records whose first result still cannot pass the public
    # disease sentence gate; successful claims are excluded by the candidate
    # predicate and are never rewritten again.
    for _ in range(2):
        await _retry_weak_disease_claims(
            client,
            records=records,
            numbers=numbers,
        )


async def _retry_weak_relation_claims(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
) -> None:
    """Retry exact microbe-condition hits whose first claim is only meta prose.

    The retry receives only the EviMed title and summary.  It cannot create a
    new condition or relation; it can only restate an already bound exact
    microbe-condition pair when that relation is explicit in the source.
    """
    candidates = [
        record
        for record in records
        if record.interpretation is not None
        and record.interpretation.target_alignment == "direct"
        and record.relation_bindings
        and record.evidence_key in numbers
        and not any(
            _relation_claim_is_substantive(
                record.interpretation.claim,
                microbe_name=binding.microbe_name,
                microbe_aliases=binding.microbe_aliases,
                disease_name=binding.disease_name,
            )
            for binding in record.relation_bindings
        )
    ]
    if not candidates:
        return
    one_per_relation: list[EvidenceRecord] = []
    seen_relations: set[tuple[str, ...]] = set()
    for record in sorted(
        candidates,
        key=lambda item: (item.relevance, item.year or 0),
        reverse=True,
    ):
        scope = tuple(
            sorted(binding.relation_id for binding in record.relation_bindings)
        )
        if scope in seen_relations:
            continue
        seen_relations.add(scope)
        one_per_relation.append(record)
    candidates = one_per_relation
    semaphore = asyncio.Semaphore(4)

    async def run_batch(batch: list[EvidenceRecord]) -> None:
        async with semaphore:
            by_number = {numbers[record.evidence_key]: record for record in batch}
            payload = {
                "task": "仅从EviMed标题摘要中重写精确菌种与疾病的医学关系",
                "requiredOutput": {
                    "evidenceInterpretations": [
                        {
                            "number": "输入编号",
                            "claim": "直接说明微生物与疾病的关系",
                        }
                    ]
                },
                "evidence": [
                    {
                        "number": number,
                        "title": record.title,
                        "summary": record.summary,
                        "relations": [
                            {
                                "microbeName": binding.microbe_name,
                                "microbeAliases": binding.microbe_aliases,
                                "diseaseName": binding.disease_name,
                                "relationType": binding.relation_type,
                            }
                            for binding in record.relation_bindings
                        ],
                    }
                    for number, record in by_number.items()
                ],
                "strictConstraints": [
                    "只使用输入标题和摘要，不补充记忆中的医学知识",
                    "claim必须同时写出relations中的微生物名称与疾病名称",
                    "只有标题或摘要明确表达该关系时才输出",
                    "相关性不得改写成因果，不得将患者人群中更常见改写成导致该疾病",
                    "来源只同时明确讨论菌种与疾病而没有因果措辞时，只能写与该疾病相关",
                    "不写指南涵盖、证据涉及、文献介绍、检查、治疗或预后等元叙述",
                    "无法从来源确认时不输出该编号",
                    "返回严格JSON，不使用Markdown",
                ],
            }
            try:
                raw = await client.generate(payload)
                parsed = EvidenceInterpretationBatch.model_validate(raw)
            except (NarrativeError, ValidationError, ValueError, TypeError):
                return
            for item in parsed.evidence_interpretations:
                record = by_number.get(item.number)
                if record is None or record.interpretation is None:
                    continue
                claim = " ".join(item.claim.split())
                matching_binding = next(
                    (
                        binding
                        for binding in record.relation_bindings
                        if _relation_claim_is_substantive(
                            claim,
                            microbe_name=binding.microbe_name,
                            microbe_aliases=binding.microbe_aliases,
                            disease_name=binding.disease_name,
                        )
                    ),
                    None,
                )
                if (
                    matching_binding is not None
                    and _safe_medical_prose(claim)
                    and _causal_language_is_source_grounded(
                        claim,
                        f"{record.title} {record.summary or ''}",
                    )
                    and _numbers_are_grounded(
                        claim,
                        _collect_grounded_numbers(record.title, record.summary),
                    )
                ):
                    record.interpretation.claim = claim

    for _ in range(1):
        remaining = [
            record
            for record in candidates
            if record.interpretation is not None
            and not any(
                _relation_claim_is_substantive(
                    record.interpretation.claim,
                    microbe_name=binding.microbe_name,
                    microbe_aliases=binding.microbe_aliases,
                    disease_name=binding.disease_name,
                )
                for binding in record.relation_bindings
            )
        ]
        if not remaining:
            break
        await asyncio.gather(
            *(
                run_batch(remaining[offset : offset + 8])
                for offset in range(0, len(remaining), 8)
            )
        )


def _relation_claim_is_substantive(
    value: str,
    *,
    microbe_name: str,
    microbe_aliases: list[str],
    disease_name: str,
) -> bool:
    if not value or not re.search(r"[\u3400-\u9fff]", value):
        return False
    normalized = _normalize_evidence_text(value)
    names_microbe = any(
        (alias_key := _normalize_evidence_text(alias)) and alias_key in normalized
        for alias in (microbe_name, *microbe_aliases)
    )
    if not names_microbe or not _claim_names_condition(value, disease_name):
        return False
    if any(
        marker in value
        for marker in (
            "指南涵盖",
            "指南讨论",
            "证据涉及",
            "文献介绍",
            "诊断与管理",
            "诊断和管理",
            "诊疗管理",
            "诊疗指南",
            "诊疗建议",
        )
    ):
        return False
    return any(
        marker in value
        for marker in (
            "引起",
            "导致",
            "所致",
            "病原",
            "致病",
            "相关",
            "关联",
            "风险增加",
            "风险升高",
            "富集",
            "增多",
            "减少",
            "检出率",
            "定植",
            "感染",
        )
    )


def _disease_claim_is_substantive(
    value: str,
    *,
    record: EvidenceRecord,
) -> bool:
    if not value or not re.search(r"[\u3400-\u9fff]", value):
        return False
    if _is_treatment_or_management_content(value):
        return False
    if any(
        marker in value
        for marker in (
            "指南讨论",
            "指南介绍",
            "文献讨论",
            "该证据直接讨论",
            "该文献直接讨论",
            "提供了建议",
            "进行了评论",
            "已有明确描述",
            "实用临床定义",
            "对患者的心理",
            "心理需求",
            "内容涉及一般原则",
            "澳大利亚心血管疾病",
            "发病高峰年龄",
            "男女比例",
            "术后并发症",
            "住院时间延长",
            "中国常见的恶性肿瘤",
            "每年约有50%",
            "痰瘀互结证",
            "中医证候",
            "常被低估",
            "心电图异常",
            "患者心理",
            "抑郁、焦虑症状及睡眠问题",
            "无症状平均风险成人",
            "并称“四高”",
            '并称"四高"',
            "中足受累",
            "步行功能残疾",
            "血清a-ccp",
            "rf、crp",
            "水平异常升高",
        )
    ):
        return False
    if not _interpretation_names_target(value, record=record):
        return False
    residual = value
    for raw_target in record.targets:
        for component in raw_target.split("/"):
            residual = residual.replace(component.strip(), "")
    if len(_normalize_evidence_text(residual)) < 6:
        return False
    # Evaluate only the substance left after removing the target name.  The
    # target itself must not make title-like metadata look clinical (for
    # example the character ``癌`` in ``结直肠癌NICE指南概要``).
    return any(
        marker in residual
        for marker in (
            "定义",
            "是一种",
            "为一种",
            "临床表现",
            "症状",
            "病理",
            "病理生理",
            "发病",
            "并发",
            "风险",
            "相关",
            "预测因子",
            "特征",
            "炎症",
            "炎性",
            "退行",
            "障碍",
            "进展",
            "自身免疫",
            "受累",
            "异常",
            "阈值",
            "血糖",
            "胆管",
            "代谢",
            "感染",
            "恶性",
            "癌",
            "肿瘤",
        )
    )


async def _retry_missing_evaluations(
    client: NarrativeClient,
    *,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
    patient_context: dict[str, Any] | None,
) -> None:
    raw_candidates = [
        record
        for record in records
        if record.category in {"guideline", "literature", "clinical_trial"}
        and record.interpretation is not None
        and record.interpretation.target_alignment == "direct"
        and not record.interpretation.recommended_evaluations
        and set(record.intents)
        & {
            "diagnostic_guideline",
            "microbe_clinical_significance",
            "microbe_disease_association",
        }
        and record.evidence_key in numbers
    ]
    candidates: list[EvidenceRecord] = []
    scope_counts: dict[tuple[str, ...], int] = {}
    for record in sorted(
        raw_candidates,
        key=lambda item: (item.relevance, item.year or 0),
        reverse=True,
    ):
        scope = tuple(record.disease_ids or record.microbe_ids) or (
            record.evidence_key,
        )
        # A search normally retains several directly aligned guide records.
        # Retrying only the first one made a missing model extraction look like
        # "no examination evidence", even when the second or third retained
        # source explicitly named a routine hospital test.  Keep the retry
        # bounded, but validate three ranked candidates before the clinical
        # layer selects the single public recommendation.
        if scope_counts.get(scope, 0) >= 3:
            continue
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        candidates.append(record)

    semaphore = asyncio.Semaphore(4)

    async def run_batch(batch: list[EvidenceRecord]) -> None:
        async with semaphore:
            batch_numbers = [numbers[record.evidence_key] for record in batch]
            by_number = dict(zip(batch_numbers, batch, strict=True))
            payload = {
                "task": "仅从该指南标题摘要中补抽取可直接确认或筛查目标的检查项目",
                "requiredOutput": {
                    "evidenceInterpretations": [
                        {
                            "number": "输入编号",
                            "claim": "原解读结论原样返回",
                            "targetAlignment": "direct",
                            "recommendedEvaluations": [
                                {
                                    "name": "规范检查名称",
                                    "rationale": "该检查用于当前目标的原因",
                                    "sourcePhrase": "标题或摘要中逐字出现的短语",
                                    "purpose": (
                                        "confirm_target/screen_target/"
                                        "characterize_target"
                                    ),
                                }
                            ],
                        }
                    ]
                },
                "evidence": [
                    {
                        "number": numbers[record.evidence_key],
                        "category": record.category,
                        "title": record.title,
                        "summary": record.summary,
                        "targets": record.targets,
                        "intents": record.intents,
                        "existingClaim": record.interpretation.claim,
                    }
                    for record in batch
                ],
                "patientContext": patient_context or {},
                "strictConstraints": [
                    "检查必须直接用于targets本身的确认或筛查",
                    "sourcePhrase必须逐字出现在标题或摘要中",
                    "没有明确检查时返回空数组",
                    "不输出治疗、用药、并发症监测或生活方式建议",
                    "返回严格JSON，不使用Markdown",
                ],
            }
            try:
                raw = await client.generate(payload)
                parsed = EvidenceInterpretationBatch.model_validate(raw)
            except (NarrativeError, ValidationError, ValueError, TypeError):
                return
            for item in parsed.evidence_interpretations:
                record = by_number.get(item.number)
                if record is not None and record.interpretation is not None:
                    record.interpretation.recommended_evaluations = (
                        _validated_evidence_evaluations(item, record)
                    )

    for _ in range(1):
        remaining = [
            record
            for record in candidates
            if record.interpretation is not None
            and not record.interpretation.recommended_evaluations
        ]
        if not remaining:
            break
        await asyncio.gather(
            *(
                run_batch(remaining[offset : offset + 8])
                for offset in range(0, len(remaining), 8)
            )
        )


def _recommended_examination_candidates(
    diseases: DiseaseRisksSection,
    *,
    evidence_numbers: set[int],
) -> list[dict[str, Any]]:
    """Build candidates from validated evidence or the dedicated model task."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for disease in diseases.items:
        for evaluation in disease.suggested_evaluations:
            refs = sorted(set(evaluation.evidence_refs) & evidence_numbers)
            if (
                evaluation.already_available
                or (not refs and evaluation.recommendation_source != "model")
                or _obviously_not_hospital_examination(
                    evaluation.name,
                    rationale=evaluation.rationale,
                )
            ):
                continue
            key = (re.sub(r"\s+", "", evaluation.name).casefold(), evaluation.purpose)
            candidate = grouped.setdefault(
                key,
                {
                    "name": evaluation.name,
                    "purpose": evaluation.purpose,
                    "rationales": [],
                    "evidenceRefs": [],
                    "diseaseIds": [],
                    "diseaseNames": [],
                    "existingResults": [],
                    "medicalInterpretations": [],
                    "patientIndication": False,
                    "modelSuggested": False,
                },
            )
            candidate["rationales"].append(evaluation.rationale)
            if evaluation.recommendation_source == "model":
                candidate["modelSuggested"] = True
                candidate["patientIndication"] = True
            candidate["evidenceRefs"].extend(refs)
            candidate["diseaseIds"].append(disease.disease_id)
            candidate["diseaseNames"].append(disease.display_name or disease.name)
            candidate["existingResults"].extend(
                item.model_dump(mode="json", by_alias=True)
                for item in disease.existing_results
            )
            if _evaluation_has_matching_abnormal_result(evaluation, disease):
                candidate["patientIndication"] = True
            if disease.medical_interpretation:
                candidate["medicalInterpretations"].append(
                    disease.medical_interpretation
                )
            if (
                disease_has_patient_microbiome_signal(disease)
                and disease.association_evidence_refs
            ):
                # A source-bound abnormal taxon plus a validated human
                # association is a patient-specific reason to let the model
                # consider a diagnostic candidate.  It is not a diagnosis;
                # the final reason must still describe the examination as a
                # supplemental assessment and pass the hospital-test gate.
                candidate["patientIndication"] = True

    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(grouped.values(), start=1):
        candidate["candidateId"] = f"exam-{index}"
        for field in (
            "rationales",
            "evidenceRefs",
            "diseaseIds",
            "diseaseNames",
            "medicalInterpretations",
        ):
            candidate[field] = list(dict.fromkeys(candidate[field]))
        candidates.append(candidate)
    return candidates


def _evaluation_has_matching_abnormal_result(evaluation: Any, disease: Any) -> bool:
    """Whether a suggested examination addresses an abnormal patient fact.

    Broad disease rows can inherit several hospital results. Domain matching
    prevents a normal glucose result plus an unrelated lipid abnormality from
    being used to justify an oral glucose tolerance test.
    """

    if disease.source_type == "clinical_context" and disease.existing_results:
        # These internal rows are created only after the selected assessment
        # condition has been deterministically matched back to a clinical
        # source result. The evidence gate still binds the proposed test to
        # the same condition before it reaches this candidate list.
        return True

    examination = f"{evaluation.name or ''} {evaluation.rationale or ''}".casefold()
    domains = (
        (("葡萄糖", "血糖", "糖化", "ogtt", "hba1c"), ("葡萄糖", "血糖", "糖化", "hba1c", "胰岛素")),
        (("血脂", "胆固醇", "甘油三酯", "ldl", "hdl"), ("血脂", "胆固醇", "甘油三酯", "ldl", "hdl")),
        (("肝", "alt", "ast", "转氨酶"), ("肝", "alt", "ast", "转氨酶")),
        (("肾", "肌酐", "egfr", "尿白蛋白", "尿酸"), ("肾", "肌酐", "egfr", "尿白蛋白", "尿酸")),
        (("结肠镜", "肠镜", "粪便", "钙卫蛋白"), ("腹泻", "便秘", "便血", "粪便", "肠")),
    )
    matched_result_markers: tuple[str, ...] = ()
    for examination_markers, result_markers in domains:
        if any(marker in examination for marker in examination_markers):
            matched_result_markers = result_markers
            break
    for result in disease.existing_results:
        if not (
            _clinical_result_is_abnormal(result)
            or _clinical_result_explicitly_confirms_disease(result, disease.name)
        ):
            continue
        result_text = f"{result.item_name or ''} {result.result or ''}".casefold()
        if matched_result_markers:
            if any(marker in result_text for marker in matched_result_markers):
                return True
            continue
        if any(
            alias.casefold() in result_text
            for alias in _target_aliases(disease.name)
            if alias
        ):
            return True
    return False


def _clinical_result_explicitly_confirms_disease(result: Any, disease_name: str) -> bool:
    """Recognize an explicit clinical diagnosis even without a status flag."""

    text = f"{result.item_name or ''} {result.result or ''}".casefold()
    if any(
        marker in text
        for marker in ("未见", "无明显", "排除", "否认", "阴性", "negative")
    ):
        return False
    return any(
        alias and alias.casefold() in text for alias in _target_aliases(disease_name)
    )


def _obviously_not_hospital_examination(
    name: str,
    *,
    rationale: str = "",
) -> bool:
    """Reject self-observation and research tools before model selection."""

    text = f"{name} {rationale}".casefold()
    excluded = (
        "量表",
        "问卷",
        "日记",
        "自评",
        "自我评估",
        "自行记录",
        "家庭记录",
        "症状记录",
        "排便记录",
        "食物日记",
        "bristol",
        "patient-reported",
        "self-report",
        "research use only",
        "research-only",
        "研究专用",
    )
    return any(marker in text for marker in excluded)


async def generate_narrative(
    client: NarrativeClient,
    *,
    parsed: ParsedMicrobiomeReport,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    session_context: dict[str, Any],
    evidence_summaries: list[dict[str, Any]],
    enterotype: str | None = None,
    health_assessment: HealthAssessment | None = None,
) -> NarrativeDraft:
    effective_enterotype = enterotype or parsed.enterotype
    payload = {
        "task": "生成肠道菌群医学辅助解读中的自然语言字段",
        "requiredOutput": {
            "executiveSummary": "结合本病例事实的全文总结",
            "abnormalMicrobesSummary": "异常与检出菌种小结",
            "microbeNarratives": [
                {
                    "microbeId": "仅使用microbeNarrativeEvidence中的菌种ID",
                    "clinicalRole": "根据该菌种对应人体证据总结其医学属性",
                    "conclusion": "用一句话概括该菌种在本报告中的医学关注点",
                    "patientInterpretation": (
                        "用45至180个汉字的1至2句话，承接不再单独展示的"
                        "相关疾病或不良影响：概括所有已绑定影响，"
                        "并优先说明证据已支持的关系方向、可能影响的"
                        "生理环节、临床表现或疾病活动度；不复述检测"
                        "结果、研究来源、诊断边界或行动建议"
                    ),
                    "clinicalConfirmation": None,
                }
            ],
            "diseaseNarratives": [
                {
                    "diseaseId": "仅使用diseaseNarrativeEvidence中的疾病ID",
                    "relatedContent": (
                        "用1至2句说明哪些相关菌群特征使该疾病方向值得关注；"
                        "不复述检测结果、研究来源、诊断边界或其他疾病"
                    ),
                    "patientSuggestion": (
                        "结合该疾病自身证据、已有异常结果和已验证检查，生成一句"
                        "具体可观察或可执行的关注建议；没有具体依据时返回null"
                    ),
                }
            ],
            "healthLevel": {
                "level": "较低关注/中度关注/重点关注",
                "summary": "肠道健康关注程度的个体化理由",
                "basis": [],
            },
            "healthProfile": (
                "120至180个汉字的综合健康画像，只概括整体状态、"
                "最优先的两个健康方向以及后续管理总策略"
            ),
            "recommendations": [
                {
                    "category": "diet/exercise/sleep/bowel_habits/general",
                    "topic": "患者可理解的健康关注方向",
                    "priority": "重点关注/建议关注",
                    "content": (
                        "只概括接下来应从哪些调养、监测、检查或医学管理方向行动，"
                        "不重复前文风险原因"
                    ),
                    "evidenceRefs": ["仅使用该关注方向对应的证据编号"],
                }
            ],
            "recommendedExaminations": [
                {
                    "candidateId": "仅使用recommendedExaminationCandidates中的候选ID",
                    "hospitalCommon": "是否为医院门诊或住院可常规开具的检验、影像、内镜、病理或功能检查",
                    "reason": "结合全文说明为什么建议补充该检查",
                }
            ],
            "assessmentExplanations": [
                {
                    "code": "本次八选六动态维度代码",
                    "explanation": "只润色已确定分数和证据事实的解释",
                }
            ],
            "abnormalIndicatorSummaries": [
                {
                    "indicatorCode": "仅使用healthAssessment.abnormalIndicators中的indicatorCode",
                    "clinicalSignificance": (
                        "结合该异常指标、所属健康维度、本次其他临床资料和已验证的"
                        "疾病证据总结临床意义；说明具体生理影响，并优先指出1至2个"
                        "最相关而非最严重的疾病或健康风险方向"
                    ),
                }
            ],
        },
        # Disease items are supplied only through diseaseWhitelist.  This keeps
        # withheld/unclear source rows out of the model context and prevents a
        # hidden row (for example an incomplete acronym) from leaking into the
        # public summary.
        "sourceFacts": parsed.model_dump(
            mode="json",
            by_alias=True,
            exclude={"panel_counts", "disease_results", "taxa"},
        ),
        "microbeWhitelist": [
            item.model_dump(
                mode="json",
                by_alias=True,
                exclude={
                    "clinical_role",
                    "conclusion",
                    "patient_interpretation",
                    "clinical_confirmation",
                    "medical_interpretation",
                },
            )
            for item in microbes.items
        ],
        "diseaseWhitelist": [
            item.model_dump(
                mode="json",
                by_alias=True,
                exclude={"model_index"},
            )
            for item in diseases.items
        ],
        "hospitalData": [
            item.model_dump(mode="json", by_alias=True) for item in hospital_data
        ],
        "sessionContext": session_context,
        "healthAssessment": (
            health_assessment.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
            if health_assessment is not None
            else None
        ),
        # The item-level medical prose has already been validated and attached
        # to the two table sections.  The global writer receives only the
        # health-management sources it can actually cite, not every retrieved
        # EviMed record again.
        "evidence": [],
        "strictConstraints": [
            "executiveSummary控制在150至250个汉字并保持为一个自然段，综合概括整体健康状态、最重要的关注方向和肠型，不逐项复述指标、菌种、疾病或原始报告内容",
            "原报告模型指数和模型分值不得进入executiveSummary、healthProfile或任何患者可见文本",
            "executiveSummary不得机械罗列菌种总数或各分类数量，应使用代表菌种、证据关系和病例事实形成自然总结",
            "abnormalMicrobesSummary控制在150至400个汉字，只概括代表性模式",
            "异常与检出菌种超过10项时，abnormalMicrobesSummary至少写出3个代表菌种名称",
            "abnormalMicrobesSummary必须分别概括本次实际存在的pathogenic_signal、opportunistic_signal和fungi_pathogenic_signal，不得只写其中一类",
            "abnormalMicrobesSummary不得出现microbeWhitelist之外的菌种",
            "sourceFinding未明确高低或异常时不得推导丰度等级、阈值或临床意义",
            "sourceResult没有检测方等级时不得推导疾病风险等级",
            "异常菌和疾病医学解读不写治疗、用药、手术或管理方案，只解释疾病、不良影响和相关风险",
            "diseaseNarratives必须逐项覆盖diseaseNarrativeEvidence中的疾病，relatedContent只说明该疾病对应的菌群特征、关联机制或风险方向，控制在1至2句",
            "diseaseNarratives不得以某病是一种、某病是一类、某病是常见的或以某特征为特征等疾病百科定义作为医学解释",
            "patientSuggestion必须针对该疾病给出具体观察方向、异常结果变化或已验证检查，不得使用可以优先关注相关健康表现、必要时由专业人员进一步评估等固定兜底话术",
            "patientSuggestion没有具体证据、异常结果或已验证检查可支撑时必须返回null，不得凭空补充症状、检查或行动",
            "diseaseNarratives只能使用各疾病自身的evidence和existingResults，不得跨疾病拼接证据、检查或结论",
            "diseaseNarratives不得出现原报告列出、原报告将、疾病关联项目、健康关注项目、检测方模型等生成流程或来源说明",
            "diseaseNarratives不得把菌群研究关联写成受检者已经患病、已确诊或确定会发病，不得复述原报告模型指数",
            "microbeNarratives必须逐项覆盖microbeNarrativeEvidence中的菌种，不输出其他菌种",
            "每个patientInterpretation必须只使用该菌种自己的sourceFinding、diseaseAndRisks和evidence，不能跨菌种拼接结论",
            "每个patientInterpretation必须包含该菌种所有已展示健康影响的名称，不得新增未绑定疾病或不良影响",
            "patientInterpretation应以旧版完整医学解读的信息量为基线，但将原相关疾病或不良影响列的有效内容合并到本字段；必须用已绑定证据中的关系方向、功能、机制、临床表现或疾病活动度解释关注原因；除明确病原体关系外，不能只把疾病名改写成“与XX相关”、“可能引起XX”或“提示XX相关风险”；如证据未提供可靠机制，则准确概括关联方向和可关注的具体健康影响，不得编造机制",
            "patientInterpretation用45至180个汉字的1至2句话、最多概括3项已绑定影响；不得重复菌名、检测值、丰度、状态、参考范围或完整检测结果",
            "patientInterpretation不得出现本次、检测、检出、丰度、人体研究、临床证据、文献、依据、不能用于判断、需由医生确认等结果来源或诊断边界话术",
            "established_pathogen使用可能引起，epidemiologic_association使用提示相关风险，涉及活动度或严重程度时使用可能与活动度或严重程度相关；观察性证据不得使用导致或确定增加患病概率",
            "不同菌种的patientInterpretation应根据菌种属性、证据关系、样本适用性和影响方向分别总结，不得套用相同固定句式",
            "病原菌或条件致病菌的粪便检出不能直接写成已感染；观察性研究只能表述为相关或关联，不能写成导致",
            "existingResults存在时必须将其实际结果纳入综合叙述，不得只罗列项目名称",
            "existingResults只陈述已有检查事实，不使用排除、不支持诊断、不提示疾病等排他性结论",
            "疾病、症状、生物标志物和表型按itemType表达；生物标志物的模型分值不得写成实际检验值",
            "所有数值必须逐项来自sourceFacts、白名单、hospitalData或evidence，不得估算、改写或新增",
            "除sourceType为pending时必须说明菌群检测结果尚在生成，以及assessmentExplanations必须展示评分缺失项外，其他模块不得展示未提供、资料不足、无法判断、不判断风险等缺失资料或执行约束说明",
            "不得写需结合临床或需结合个体情况等通用边界话术",
            "sessionContext没有症状资料时不得表述为无症状或症状不相符",
            "recommendations表示健康总结中的下一步行动方向，每项必须包含topic、priority、content和evidenceRefs，输出2至3项，最多3项",
            "recommendations的content只说接下来做什么：概括饮食、运动、作息、症状观察、复查或就医方向；不再解释为什么存在该风险，不重复疾病、菌群、异常指标或循证结论",
            "recommendations可以给出与已有健康问题相符的饮食结构、运动、睡眠、作息、排便或症状观察方向，但不得伪造精确剂量、频次或疗效承诺",
            "recommendations如提及具体检查，名称必须来自recommendedExaminationCandidates；治疗只能表达为检查确认后由医生评估是否需要规范处理，不得新增诊断、药名、剂量、手术、益生菌、补充剂、品牌或产品",
            "recommendations不得复述不良影响或前文风险理由，也不得使用必要时由专业人员进一步评估等没有具体方向的统一兜底话术",
            "recommendations必须对应sourceFacts、hospitalData、healthAssessment或已验证菌群/疾病证据中的实际关注点，不得凭空新增症状或疾病",
            "recommendations不得把观察性菌群关联写成个人患病概率或确定因果，证据不足时使用相关、可能影响或值得关注等审慎表达",
            "recommendations中相同或高度重叠的健康影响必须合并，不能按指标逐条生成",
            "recommendedExaminations是健康总结中的全文汇总检查，只能从recommendedExaminationCandidates选择，最多3项",
            "recommendedExaminations每项hospitalCommon必须根据检查名称与用途判断；候选modelSuggested=true表示已单独生成的常见检查建议，可无文献编号，不得伪造证据；其余候选沿用已有循证内容",
            "量表、问卷、日记、自评、家庭记录、症状记录和研究专用工具不是建议补充检查，hospitalCommon必须返回false",
            "recommendedExaminations应结合本次动态选中的六个维度、已有检查、疾病关注项和菌群结果说明补充该检查的优先理由，不得新增检查名称",
            "recommendedExaminations不能选择已有检查，不能把菌群关联写成已确诊疾病；无候选项时返回空数组",
            "肠型只能写肠型I、肠型II或肠型III，不得改写为Bacteroides型等名称",
            "healthLevel只评价本次肠道健康关注程度，不能评价患者整体健康；summary只用一个完整句子概括形成该关注程度的类别级原因，不重复菌种名、疾病名、检查数值或healthProfile细节",
            "healthLevel和healthProfile不使用模型、分值、评分、指数、原报告或生成过程等实现性用语",
            "executiveSummary只写本次结果结论和需要关注的方向；healthProfile以全身健康评估、现有检查和临床背景为主，肠道微生态只作为其中一个方面；两者不得复制同一句子或重复罗列同一批菌种与疾病",
            "healthProfile控制在120至180个汉字，只概括整体健康状态、最优先的两个健康方向与后续管理总策略",
            "healthProfile不重复肠型，不逐项罗列菌种、疾病、指标数值或其他模块明细",
            "healthProfile不得说明为什么生成、出具或需要这份报告，不写本报告用于、本次评估旨在、形成关注顺序、便于理解或阅读等过程性结论",
            "healthProfile不得把粪便微生物检出概括为感染风险；未有临床诊断时应表述为胃肠道症状观察和必要时的病原学确认",
            "所有患者可见文字不得出现AI、EviMed、检索、筛选、规则、模板、JSON、审核、白名单、存储实现、用户上传、PDF、图片、文件名、解析过程或后台处理",
            "assessmentExplanations只能润色healthAssessment已有的健康意义，不得修改或重复分数、等级、权重、趋势修正、封顶结果、缺失项或冲突项；scoreAvailable为true时不得出现本维度、具体分数或等级名称",
            "assessmentExplanations必须结合本维度已有的关联疾病类别或客观参考指标，说明本次为什么需要关注以及应观察的健康方向；不得只写相关风险需要持续管理、需要优先关注或目前相对稳定等空泛结论",
            "assessmentExplanations不得将健康管理评估指数写成疾病概率或诊断结论，不得虚构未提供的检验值",
            "scoreAvailable为false时，assessmentExplanations必须保留暂不评分及每个missingEvidence项",
            "abnormalIndicatorSummaries必须逐项使用healthAssessment.abnormalIndicators中的indicatorCode，不得遗漏已有异常指标",
            "clinicalSignificance只解释该指标异常在本次整体健康评估中的医学关注含义，不重复完整检测值，不新增阈值、疾病诊断、检查建议、治疗方案或用药建议",
            "clinicalSignificance应结合healthAssessment维度、hospitalData、sourceFacts、microbeNarrativeEvidence和diseaseNarrativeEvidence中已有事实形成个体化总结，不得写成通用模板",
            "菌群功能类异常指标不得向受检者解释模型、算法、推断过程、并非直接检测或不等同于什么；直接说明该变化可能影响的生理功能及1至2个最相关而非最严重的疾病或健康风险方向",
            "菌群功能类异常指标只能使用microbeNarrativeEvidence或diseaseNarrativeEvidence已经验证的风险名称；没有直接证据时只说明具体的屏障、炎症、免疫或代谢影响，不得新增疾病",
            "不得使用乙酸合成能力弱提示乙酸合成功能需关注、免疫力弱提示免疫功能需关注等同义反复句式",
        ],
    }
    health_evidence_refs: dict[str, set[int]] = {
        "diet": set(),
        "exercise": set(),
        "sleep": set(),
        "bowel_habits": set(),
        "general": set(),
    }
    allowed_microbe_confirmations: dict[str, set[str]] = {
        item.microbe_id: set() for item in microbes.items
    }
    evidence_by_number = {
        int(item.get("number") or 0): item
        for item in evidence_summaries
        if int(item.get("number") or 0) > 0
    }
    recommended_examination_candidates = _recommended_examination_candidates(
        diseases,
        evidence_numbers=set(evidence_by_number),
    )
    payload["recommendedExaminationCandidates"] = (
        recommended_examination_candidates
    )
    recommended_examination_candidates_by_id = {
        item["candidateId"]: item
        for item in recommended_examination_candidates
    }
    narrative_microbes = [
        item
        for item in microbes.items
        if item.evidence_refs
        and _microbe_has_public_narrative_evidence(
            item,
            evidence_by_number=evidence_by_number,
        )
    ]
    narrative_microbe_ids = {item.microbe_id for item in narrative_microbes}
    for item in microbes.items:
        item.model_interpretation_required = (
            item.microbe_id in narrative_microbe_ids
        )
    payload["microbeNarrativeEvidence"] = [
        {
            "microbeId": item.microbe_id,
            "name": item.name,
            "scientificName": item.scientific_name,
            "category": item.category,
            "sourceFinding": item.source_finding.model_dump(
                mode="json", by_alias=True, exclude_none=False
            ),
            "diseaseAndRisks": [
                risk.model_dump(mode="json", by_alias=True, exclude_none=False)
                for risk in item.disease_and_risks
                if _risk_is_report_context_applicable(risk)
            ],
            "evidence": [
                {
                    "number": number,
                    "title": evidence_by_number[number].get("title"),
                    "summary": evidence_by_number[number].get("summary"),
                    "interpretation": evidence_by_number[number].get(
                        "interpretation"
                    ),
                }
                for number in list(
                    dict.fromkeys(
                        [
                            *item.interpretation_evidence_refs,
                            *(
                                number
                                for risk in item.disease_and_risks
                                if _risk_is_report_context_applicable(risk)
                                for number in risk.evidence_refs
                            ),
                        ]
                    )
                )[:6]
                if number in evidence_by_number
            ],
        }
        for item in narrative_microbes
    ]
    health_evidence_text_by_number: dict[int, str] = {}
    for item in evidence_summaries:
        interpretation = item.get("interpretation") or {}
        evaluation_names = {
            str(evaluation.get("name") or "").strip()
            for evaluation in interpretation.get("recommendedEvaluations") or []
            if str(evaluation.get("name") or "").strip()
        }
        for microbe_id in item.get("microbeIds") or []:
            if microbe_id in allowed_microbe_confirmations:
                allowed_microbe_confirmations[microbe_id].update(evaluation_names)
        if not set(item.get("intents") or []) & {
            "health_management",
            "intervention",
        }:
            continue
        if (item.get("interpretation") or {}).get("targetAlignment") != "direct":
            continue
        number = int(item["number"])
        health_evidence_text_by_number[number] = json.dumps(
            {
                "title": item.get("title"),
                "summary": item.get("summary"),
                "interpretation": item.get("interpretation"),
                "targets": item.get("targets"),
            },
            ensure_ascii=False,
        )
        targets = " ".join(item.get("targets") or [])
        if "膳食" in targets:
            health_evidence_refs["diet"].add(number)
        if "运动" in targets:
            health_evidence_refs["exercise"].add(number)
        if "睡眠" in targets:
            health_evidence_refs["sleep"].add(number)
        if "排便" in targets:
            health_evidence_refs["bowel_habits"].add(number)
        health_evidence_refs["general"].add(number)
    target_categories = _patient_health_target_categories(
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
        health_assessment=health_assessment,
        session_context=session_context,
    )
    for category in ("diet", "exercise", "sleep", "bowel_habits"):
        if category not in target_categories:
            health_evidence_refs[category].clear()
    health_evidence_refs["general"] = set().union(
        *(health_evidence_refs[category] for category in target_categories),
        *(
            set(item.association_evidence_refs)
            for item in diseases.items
            if item.medical_interpretation or item.association_evidence_refs
        ),
        *(
            set(risk.evidence_refs)
            for microbe in microbes.items
            for risk in microbe.disease_and_risks
        ),
    )
    allowed_health_numbers = {
        number
        for numbers_for_category in health_evidence_refs.values()
        for number in numbers_for_category
    }
    payload["evidence"] = [
        item
        for item in evidence_summaries
        if int(item.get("number") or 0) in allowed_health_numbers
    ]
    narrative_diseases = [
        item
        for item in diseases.items
        if _disease_needs_narrative(item) and item.association_evidence_refs
    ]
    disease_narrative_evidence = [
        {
            "diseaseId": item.disease_id,
            "name": item.name,
            "itemType": item.item_type,
            "sourceResult": item.source_result,
            "sourceDescription": item.source_description,
            "relatedMicrobes": item.related_microbes,
            "existingResults": [
                result.model_dump(mode="json", by_alias=True, exclude_none=False)
                for result in item.existing_results
            ],
            "evidence": [
                {
                    "number": number,
                    "title": evidence_by_number[number].get("title"),
                    "summary": evidence_by_number[number].get("summary"),
                    "interpretation": evidence_by_number[number].get(
                        "interpretation"
                    ),
                }
                for number in list(dict.fromkeys(item.association_evidence_refs))[:6]
                if number in evidence_by_number
            ],
        }
        for item in narrative_diseases
    ]
    payload["diseaseNarrativeEvidence"] = disease_narrative_evidence
    grounded_numbers = _collect_grounded_numbers(
        payload["sourceFacts"],
        payload["microbeWhitelist"],
        payload["diseaseWhitelist"],
        payload["hospitalData"],
        payload["healthAssessment"],
        payload["evidence"],
        payload["microbeNarrativeEvidence"],
        payload["diseaseNarrativeEvidence"],
    )
    try:
        raw = await client.generate(payload)
        draft = NarrativeDraft.model_validate(raw)
        validated = validate_draft(
            draft,
            microbes=microbes,
            diseases=diseases,
            health_assessment=health_assessment,
            session_context=session_context,
            allowed_health_evidence_refs=health_evidence_refs,
            allowed_microbe_confirmations=allowed_microbe_confirmations,
            health_evidence_text_by_number=health_evidence_text_by_number,
            grounded_numbers=grounded_numbers,
            recommended_examination_candidates=(
                recommended_examination_candidates_by_id
            ),
        )
    except (NarrativeError, ValidationError, ValueError, TypeError):
        # A malformed or truncated global response must not discard the
        # structured report.  The bounded retry below asks only for short
        # global fields; per-microbe and per-disease prose is reconstructed
        # later from validated source facts and relation bindings.
        validated = NarrativeDraft()
    retry_calls = 0
    # Keep a separate, data-sized repair budget for organism prose.  A fixed
    # cap of 14 was consumed by one global retry plus the first organism pass
    # in reports containing 14 or more displayable taxa, so the intended
    # second model repair round never ran and every rejected row fell back to
    # the same deterministic sentence.
    retry_call_budget = min(30, max(14, len(narrative_microbes) * 2 + 2))
    microbe_retry_rounds = 0
    for _ in range(3):
        missing = _missing_narrative_fields(
            validated,
            microbes=microbes,
            diseases=diseases,
        )
        # A single bounded retry round is allowed for evidence-backed microbe
        # prose.  Earlier code cleared this list unconditionally, which meant
        # the real model was never asked to repair missing item narratives and
        # every row fell back to the same deterministic wording.
        narrative_microbe_ids = {
            item.microbe_id for item in narrative_microbes
        }
        missing["microbeIds"] = [
            microbe_id
            for microbe_id in missing["microbeIds"]
            if microbe_id in narrative_microbe_ids and microbe_retry_rounds < 2
        ]
        # Disease prose is repaired in bounded multi-item batches below. Mixing
        # it into one-organism requests caused the model to complete only the
        # first content type and made the remaining disease rows fall back to
        # workflow wording instead of a medical explanation.
        missing["diseaseIds"] = []
        missing["recommendationCategories"] = []
        if "recommendations" in missing["fields"]:
            # Health attention directions are repaired in their own bounded
            # request below.  Do not mix them into one-organism retries.
            missing["fields"].remove("recommendations")
        if not any(missing.values()) or retry_calls >= retry_call_budget:
            break
        pending_retries: list[tuple[dict[str, Any], dict[str, Any]]] = []
        # Repair global prose separately from one-organism requests. Previously
        # the first organism batch also carried executive-summary/health fields,
        # so the model often completed the global prose and skipped that
        # organism. All requests still run concurrently.
        retry_batches = _narrative_retry_batches(
            missing,
            # Two short organism rows share the first repair request. Any row
            # still omitted is retried individually in the second round.
            batch_size=2 if microbe_retry_rounds == 0 else 1,
        )
        if missing["fields"] and missing["microbeIds"]:
            for batch in retry_batches:
                batch["fields"] = []
                batch["recommendationCategories"] = []
            retry_batches.insert(
                0,
                {
                    "fields": list(missing["fields"]),
                    "microbeIds": [],
                    "diseaseIds": [],
                    "recommendationCategories": list(
                        missing["recommendationCategories"]
                    ),
                },
            )
        # Keep repair batches small. Large multi-organism requests frequently
        # returned only one or two complete rows and forced the remainder back
        # to identical deterministic prose.
        for batch in retry_batches:
            if retry_calls >= retry_call_budget:
                break
            retry_calls += 1
            retry_fields = set(batch["fields"])
            needs_full_context = bool(
                retry_fields
                & {
                    "executiveSummary",
                    "abnormalMicrobesSummary",
                    "healthLevel",
                    "healthProfile",
                }
            )
            microbe_ids = set(batch["microbeIds"])
            disease_ids = set(batch["diseaseIds"])
            scoped_evidence = [
                item
                for item in payload["evidence"]
                if needs_full_context
                or not (microbe_ids or disease_ids)
                or microbe_ids.intersection(item.get("microbeIds") or [])
                or disease_ids.intersection(item.get("diseaseIds") or [])
            ]
            microbe_only = bool(microbe_ids) and not needs_full_context
            retry_constraints = list(payload["strictConstraints"])
            if microbe_only:
                retry_constraints.extend(
                    [
                        "patientInterpretation中出现的具体疾病、症状或不良影响只能来自mustMentionRiskNames，名称必须原样写出",
                        "不得增加mustMentionRiskNames以外的感染类型、症状、并发症、器官损伤或疾病风险",
                        "patientInterpretation用45至180个汉字的1至2句话，承接已隐藏的相关疾病或不良影响内容，应包含证据已支持的关系方向、生理功能、机制、临床表现或疾病活动度，不能只重写疾病名；不重复菌名、检测结果、人体研究或诊断边界；established_pathogen使用可能引起，观察性关联使用提示相关风险",
                    ]
                )
            retry_payload = {
                "task": (
                    "只为指定菌种补写个体化医学解释，不生成其他字段"
                    if microbe_only
                    else "只补齐第一次输出中缺失或未通过事实校验的字段"
                ),
                "retryOnly": batch,
                "requiredOutput": (
                    {
                        "microbeNarratives": payload["requiredOutput"][
                            "microbeNarratives"
                        ]
                    }
                    if microbe_only
                    else payload["requiredOutput"]
                ),
                "sourceFacts": payload["sourceFacts"],
                "microbeWhitelist": [
                    item
                    for item in payload["microbeWhitelist"]
                    if needs_full_context or item["microbeId"] in microbe_ids
                ],
                "diseaseWhitelist": [
                    item
                    for item in payload["diseaseWhitelist"]
                    if needs_full_context or item["diseaseId"] in disease_ids
                ],
                "hospitalData": payload["hospitalData"],
                "sessionContext": session_context,
                "evidence": scoped_evidence,
                "microbeNarrativeEvidence": [
                    item
                    for item in payload["microbeNarrativeEvidence"]
                    if needs_full_context or item["microbeId"] in microbe_ids
                ],
                "mustMentionRiskNames": {
                    item["microbeId"]: [
                        risk["name"] for risk in item["diseaseAndRisks"]
                    ]
                    for item in payload["microbeNarrativeEvidence"]
                    if item["microbeId"] in microbe_ids
                },
                "strictConstraints": retry_constraints,
            }
            pending_retries.append((batch, retry_payload))
        if any(batch["microbeIds"] for batch, _ in pending_retries):
            microbe_retry_rounds += 1
        # Patient-visible organism rows are the only content that has no safe
        # public fallback. Put their small requests ahead of optional global
        # prose repairs so a slow executive-summary request cannot occupy the
        # limited model slots and starve every microbe row.
        pending_retries.sort(
            key=lambda item: 0 if item[0]["microbeIds"] else 1
        )
        retry_results = await _generate_retry_payloads(
            client,
            [item[1] for item in pending_retries],
            concurrency=2,
        )
        for (batch, _retry_payload), retry_raw in zip(
            pending_retries,
            retry_results,
            strict=True,
        ):
            if isinstance(retry_raw, Exception):
                continue
            try:
                retry = NarrativeDraft.model_validate(retry_raw)
                retry = validate_draft(
                    retry,
                    microbes=microbes,
                    diseases=diseases,
                    health_assessment=health_assessment,
                    session_context=session_context,
                    allowed_health_evidence_refs=health_evidence_refs,
                    allowed_microbe_confirmations=allowed_microbe_confirmations,
                    health_evidence_text_by_number=health_evidence_text_by_number,
                    grounded_numbers=grounded_numbers,
                    recommended_examination_candidates=(
                        recommended_examination_candidates_by_id
                    ),
                )
                validated = _merge_missing_narrative(validated, retry, batch)
            except (ValidationError, ValueError, TypeError, KeyError):
                # Structured facts remain available if a bounded model retry
                # still fails; the pipeline then marks the report partial.
                continue

    completed_disease_ids = {
        item.disease_id for item in validated.disease_narratives
    }
    missing_disease_ids = [
        item["diseaseId"]
        for item in disease_narrative_evidence
        if item["diseaseId"] not in completed_disease_ids
    ]
    disease_retry_payloads: list[dict[str, Any]] = []
    # Two rows per request keeps the output small enough for strict per-row
    # JSON and grounding checks.  Five-row batches frequently returned one
    # complete item and pushed every other row into the same extractive prose.
    for offset in range(0, len(missing_disease_ids), 2):
        disease_ids = set(missing_disease_ids[offset : offset + 2])
        disease_retry_payloads.append(
            {
                "task": "只为指定疾病或健康问题补写医学解释与关联风险",
                "requiredOutput": {
                    "diseaseNarratives": payload["requiredOutput"][
                        "diseaseNarratives"
                    ]
                },
                "diseaseNarrativeEvidence": [
                    item
                    for item in disease_narrative_evidence
                    if item["diseaseId"] in disease_ids
                ],
                "diseaseWhitelist": [
                    item
                    for item in payload["diseaseWhitelist"]
                    if item["diseaseId"] in disease_ids
                ],
                "hospitalData": payload["hospitalData"],
                "sessionContext": session_context,
                "strictConstraints": [
                    "逐项输出输入中的每个diseaseId，不得输出其他ID",
                    "每项只能使用该疾病自己的evidence、existingResults、relatedMicrobes和sourceDescription",
                    "relatedContent只说明该疾病对应的菌群特征、关联机制或风险方向，控制在1至2句，不复述检测结果或研究来源",
                    "relatedContent不得使用某病是一种、某病是一类、某病是常见的或以某特征为特征等百科式开头",
                    "patientSuggestion只使用该疾病自己的证据、existingResults和suggestedEvaluations生成一句具体建议；没有具体依据时返回null",
                    "不得使用可以优先关注相关健康表现、必要时由专业人员进一步评估等固定兜底话术",
                    "不得出现原报告列出、原报告将、疾病关联项目、健康关注项目、检测方模型等来源或生成流程话术",
                    "不得写成已经患病、已经确诊或确定会发病，不得引用原报告模型分值",
                    "不写检查建议、治疗、用药、手术或生活方式方案",
                    "返回严格JSON，不使用Markdown",
                ],
            }
        )
    # Disease prose and abnormal-indicator prose depend only on the already
    # validated source/evidence set, not on each other. Start both repairs
    # before awaiting either one, then merge in the historical disease-first
    # order so the public text remains deterministic.
    disease_retry_task = (
        asyncio.create_task(
            _generate_retry_payloads(
                client,
                disease_retry_payloads,
                concurrency=3,
            )
        )
        if disease_retry_payloads
        else None
    )

    abnormal_indicators = (
        health_assessment.abnormal_indicators if health_assessment is not None else []
    )
    completed_indicator_codes = {
        item.indicator_code for item in validated.abnormal_indicator_summaries
    }
    missing_indicator_codes = {
        item.indicator_code
        for item in abnormal_indicators
        if item.indicator_code not in completed_indicator_codes
    }
    indicator_retry_task: asyncio.Task[dict[str, Any]] | None = None
    if missing_indicator_codes:
        indicator_retry_payload = {
            "task": "只为指定异常指标补写面向受检者的临床意义",
            "requiredOutput": {
                "abnormalIndicatorSummaries": payload["requiredOutput"][
                    "abnormalIndicatorSummaries"
                ]
            },
            "healthAssessment": payload["healthAssessment"],
            "requestedIndicatorCodes": sorted(missing_indicator_codes),
            "hospitalData": payload["hospitalData"],
            "sourceFacts": payload["sourceFacts"],
            "strictConstraints": [
                "逐项输出requestedIndicatorCodes中的每个indicatorCode，不得输出其他编码",
                "clinicalSignificance必须写明对应检测项目，并结合所属健康维度和本次已有资料解释医学关注含义",
                "不得重复完整检测结果，不新增输入中没有的数值、阈值或疾病诊断",
                "菌群功能类指标不得解释模型、算法、推断过程、并非直接检测或不等同于什么；应说明具体生理影响，并从已有证据中选择1至2个最相关而非最严重的风险方向",
                "不得使用指标偏弱提示该指标功能需关注等同义反复句式",
                "不写检查、复查、治疗、用药或生活方式建议",
                "不得出现AI、模型、规则、用户上传、PDF、文件名、解析或后台流程",
                "返回严格JSON，不使用Markdown",
            ],
        }
        indicator_retry_task = asyncio.create_task(
            client.generate(indicator_retry_payload)
        )

    if disease_retry_task is not None:
        disease_retry_results = await disease_retry_task
        for retry_raw in disease_retry_results:
            if isinstance(retry_raw, Exception):
                continue
            try:
                retry = validate_draft(
                    NarrativeDraft.model_validate(retry_raw),
                    microbes=microbes,
                    diseases=diseases,
                    health_assessment=health_assessment,
                    session_context=session_context,
                    allowed_health_evidence_refs=health_evidence_refs,
                    allowed_microbe_confirmations=allowed_microbe_confirmations,
                    health_evidence_text_by_number=health_evidence_text_by_number,
                    grounded_numbers=grounded_numbers,
                    recommended_examination_candidates=(
                        recommended_examination_candidates_by_id
                    ),
                )
            except (ValidationError, ValueError, TypeError, KeyError):
                continue
            validated = _merge_missing_narrative(validated, retry)

    if indicator_retry_task is not None:
        try:
            indicator_raw = await indicator_retry_task
            indicator_retry = validate_draft(
                NarrativeDraft.model_validate(indicator_raw),
                microbes=microbes,
                diseases=diseases,
                health_assessment=health_assessment,
                session_context=session_context,
                allowed_health_evidence_refs=health_evidence_refs,
                allowed_microbe_confirmations=allowed_microbe_confirmations,
                health_evidence_text_by_number=health_evidence_text_by_number,
                grounded_numbers=grounded_numbers,
                recommended_examination_candidates=(
                    recommended_examination_candidates_by_id
                ),
            )
            validated = _merge_missing_narrative(validated, indicator_retry)
        except (NarrativeError, ValidationError, ValueError, TypeError, KeyError):
            pass
    if (
        not _display_length_in_range(
            validated.abnormal_microbes_summary,
            minimum=150,
            maximum=400,
        )
        and microbes.items
    ):
        microbe_conclusions = {
            item.microbe_id: item.conclusion for item in validated.microbe_narratives
        }
        validated.abnormal_microbes_summary = _extractive_microbe_summary(
            microbes=microbes,
            conclusions=microbe_conclusions,
            has_session_context=bool(session_context),
            grounded_numbers=grounded_numbers,
        )
    if not _health_profile_is_complete(
        validated.health_profile,
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
    ):
        validated.health_profile = None

    if (
        not validated.health_level
        or not validated.health_profile
        or len(validated.recommendations) < 2
    ) and any(health_evidence_refs.values()):
        health_evidence = [
            item
            for item in evidence_summaries
            if set(item.get("intents") or [])
            & {"health_management", "intervention"}
            and (item.get("interpretation") or {}).get("targetAlignment") == "direct"
        ]
        for _ in range(2):
            health_payload = {
                "task": "只生成简洁健康画像与建议关注方向",
                "requiredOutput": {
                    "healthLevel": {
                        "level": "较低关注/中度关注/重点关注",
                        "summary": "个体化、完整句子",
                        "basis": [],
                    },
                    "healthProfile": (
                        "120至180个汉字的综合健康画像；只概括整体状态、"
                        "最优先的两个健康方向与后续管理总策略"
                    ),
                    "recommendations": [
                        {
                            "category": "diet/exercise/sleep/bowel_habits/general",
                            "topic": "健康关注方向",
                            "priority": "重点关注/建议关注",
                            "content": (
                                "只概括接下来的调养、监测、检查或医学管理方向，"
                                "不重复前文风险原因"
                            ),
                            "evidenceRefs": ["对应关注方向的证据编号"],
                        }
                    ],
                },
                "enterotype": effective_enterotype,
                "microbeSignals": [
                    {
                        "microbeId": item.microbe_id,
                        "name": item.name,
                        "category": item.category,
                        "status": item.source_finding.status,
                        "risks": [
                            risk.name
                            for risk in item.disease_and_risks
                            if _risk_is_report_context_applicable(risk)
                        ],
                    }
                    for item in microbes.items
                ],
                "diseaseItems": [
                    {
                        "name": item.name,
                        "medicalInterpretation": item.medical_interpretation,
                        "existingResults": [
                            result.model_dump(mode="json", by_alias=True)
                            for result in item.existing_results
                        ],
                    }
                    for item in diseases.items
                ],
                "hospitalData": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in hospital_data
                ],
                "allowedEvidenceByCategory": {
                    category: sorted(health_evidence_refs[category])
                    for category in (
                        "diet",
                        "exercise",
                        "sleep",
                        "bowel_habits",
                        "general",
                    )
                    if health_evidence_refs[category]
                },
                "healthEvidence": health_evidence,
                "recommendedExaminationCandidates": (
                    recommended_examination_candidates
                ),
                "strictConstraints": [
                    "肠型只使用输入的肠型I、肠型II或肠型III",
                    "健康画像使用自然、专业且患者能理解的医学表达，不使用模型、分值、评分、指数、规则或系统流程用语",
                    "健康画像不得提及用户上传、PDF、图片、文件名、解析过程或后台处理",
                    "健康画像不复制报告总结或前文内容，只概括整体健康状态、最优先的两个方向和后续管理总策略",
                    "健康画像不展示肠型，不逐项重复指标数值、菌名或疾病列表，不得整段只讲肠道微生态",
                    "健康画像不解释为什么生成或出具报告，不写本报告用于、本次评估旨在、形成关注顺序、便于阅读等过程性结论",
                    "healthProfile只写输入中实际存在的健康评估、临床资料、疾病关联和菌群结果；某类资料不存在时不强行补齐或暴露缺失说明",
                    "原报告的疾病或健康问题只能写为原报告列出的关联项目，不得改写为既往疾病、病史或已确诊疾病",
                    "healthLevel只评价肠道健康关注程度，summary只用一句类别级原因，不重复具体菌种、疾病、检查结果或healthProfile内容；不写患者整体健康状况，不写正常情况下不致病或特定条件下感染等通用边界",
                    "已有检查只陈述事实，不使用排除、不支持、不提示或表明未发现等排他性结论",
                    "recommendations输出2至3项，最多3项，并按重点关注、建议关注排序",
                    "recommendations每项只写下一步调养、监测、检查或医学管理方向，不再说明风险原因，不重复指标、数值、菌种、疾病和循证结论",
                    "可以给出饮食结构、运动、睡眠、作息、排便或症状观察方向，但不得新增精确剂量、频次、药名、手术、补充剂、产品或疗效承诺",
                    "如提及具体检查，只能使用recommendedExaminationCandidates中的名称；治疗只能写为检查确认后由医生评估是否需要规范处理",
                    "不得只复述不良影响，不得使用必要时进一步评估等空泛兜底话术",
                    "不得把菌群观察性关联写成个人患病概率或确定因果",
                    "相同健康影响合并为一个topic，不按单个指标拆分",
                    "返回严格JSON，不使用Markdown",
                ],
            }
            health_payload["microbeWhitelist"] = health_payload["microbeSignals"]
            try:
                health_raw = await client.generate(health_payload)
                health_draft = NarrativeDraft.model_validate(health_raw)
                health_draft = validate_draft(
                    health_draft,
                    microbes=microbes,
                    diseases=diseases,
                    health_assessment=health_assessment,
                    session_context=session_context,
                    allowed_health_evidence_refs=health_evidence_refs,
                    allowed_microbe_confirmations=allowed_microbe_confirmations,
                    health_evidence_text_by_number=health_evidence_text_by_number,
                    grounded_numbers=grounded_numbers,
                    recommended_examination_candidates=(
                        recommended_examination_candidates_by_id
                    ),
                )
                if not _health_profile_is_complete(
                    health_draft.health_profile,
                    enterotype=effective_enterotype,
                    microbes=microbes,
                    diseases=diseases,
                ):
                    health_draft.health_profile = None
            except (
                NarrativeError,
                ValidationError,
                ValueError,
                TypeError,
                KeyError,
            ):
                continue
            validated = _merge_missing_narrative(
                validated,
                health_draft,
                {
                    "fields": [
                        "healthLevel",
                        "healthProfile",
                        "recommendations",
                    ],
                    "microbeIds": [],
                    "diseaseIds": [],
                },
            )
            if (
                validated.health_level
                and validated.health_profile
                and validated.recommendations
            ):
                break
    if (
        recommended_examination_candidates
        and not validated.recommended_examinations
    ):
        candidate_reference_numbers = {
            number
            for candidate in recommended_examination_candidates
            for number in candidate["evidenceRefs"]
        }
        examination_payload = {
            "task": "结合已完成的全文内容汇总建议补充检查",
            "requiredOutput": {
                "recommendedExaminations": [
                    {
                        "candidateId": "候选检查ID",
                        "hospitalCommon": "是否为医院可常规开具的检查",
                        "reason": "结合全文的优先补充理由",
                    }
                ]
            },
            "recommendedExaminationCandidates": (
                recommended_examination_candidates
            ),
            "validatedReportSections": {
                "executiveSummary": validated.executive_summary,
                "healthProfile": validated.health_profile,
                "healthAssessment": payload["healthAssessment"],
                "microbes": payload["microbeWhitelist"],
                "microbeNarratives": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in validated.microbe_narratives
                ],
                "diseases": payload["diseaseWhitelist"],
                "diseaseNarratives": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in validated.disease_narratives
                ],
                "hospitalData": payload["hospitalData"],
                "sessionContext": session_context,
            },
            "evidence": [
                item
                for item in evidence_summaries
                if int(item.get("number") or 0) in candidate_reference_numbers
            ],
            "strictConstraints": [
                "最多返回3项，按本次整体健康资料的临床相关性和优先级排序",
                "只能返回recommendedExaminationCandidates中存在的candidateId，不得新增、改名或拆分检查项目",
                "hospitalCommon必须明确判断：医院实验室检验、影像、内镜、病理或功能检查才可为true；量表、问卷、日记、自评、家庭记录或研究工具必须为false",
                "reason要综合本次动态选中的六个维度、已有检查、疾病关注项和菌群结果，但不把菌群关联写成个人已患疾病",
                "不重复推荐已完成的检查，不新增检查值、阈值、症状、诊断、用药或治疗建议",
                "无适合项目时返回空数组；返回严格JSON，不使用Markdown",
            ],
        }
        try:
            examination_raw = await client.generate(examination_payload)
            examination_draft = validate_draft(
                NarrativeDraft.model_validate(examination_raw),
                microbes=microbes,
                diseases=diseases,
                health_assessment=health_assessment,
                session_context=session_context,
                allowed_health_evidence_refs=health_evidence_refs,
                allowed_microbe_confirmations=allowed_microbe_confirmations,
                health_evidence_text_by_number=health_evidence_text_by_number,
                grounded_numbers=grounded_numbers,
                recommended_examination_candidates=(
                    recommended_examination_candidates_by_id
                ),
            )
            validated.recommended_examinations = (
                examination_draft.recommended_examinations
            )
        except (NarrativeError, ValidationError, ValueError, TypeError, KeyError):
            # The field remains a fixed empty array rather than publishing an
            # invented examination when the bounded model summary fails.
            pass
    source_profile = _extractive_health_profile(
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
        health_assessment=health_assessment,
    )
    source_health_summary = _extractive_health_level_summary(
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
    )
    if source_health_summary:
        validated.health_level = HealthLevelDraft(
            level=_supported_health_attention_level(
                microbes=microbes,
                diseases=diseases,
            ),
            summary=source_health_summary,
            basis=[],
        )
    else:
        validated.health_level = None
    # Keep a validated, source-bounded model portrait when available.  The
    # former unconditional replacement discarded the professional synthesis
    # and exposed the same short extractive template in every report.  The
    # deterministic portrait is now only a no-model/invalid-model fallback.
    if not _health_profile_is_complete(
        validated.health_profile,
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
    ):
        validated.health_profile = source_profile
    # Keep a model-authored item only after `validate_draft` has checked its
    # microbe ID, numbers, bound outcome names, treatment language and
    # patient-level speculation.  Fill only missing/invalid items with the
    # source-fact fallback.  The previous unconditional replacement here
    # discarded every valid model result and made all public rows look like a
    # fixed template even when the model had completed successfully.
    validated_microbes = {
        item.microbe_id: item for item in validated.microbe_narratives
    }
    narrative_microbe_ids = {item.microbe_id for item in narrative_microbes}
    completed_microbe_narratives: list[MicrobeNarrative] = []
    for microbe in microbes.items:
        item = validated_microbes.get(microbe.microbe_id)
        if (
            microbe.microbe_id not in narrative_microbe_ids
            or item is None
            or not item.conclusion
            or not item.patient_interpretation
        ):
            item = _source_fact_microbe_narrative(microbe)
        completed_microbe_narratives.append(item)
    validated.microbe_narratives = completed_microbe_narratives
    completed_diseases = {
        item.disease_id: item for item in validated.disease_narratives
    }
    for disease in diseases.items:
        # Preserve validated model prose. Only use a directly bound existing
        # interpretation when the model did not complete this row; never
        # replace it with source/workflow wording.
        if disease.disease_id in completed_diseases:
            continue
        content = _source_fact_disease_narrative(disease, microbes=microbes)
        if content:
            completed_diseases[disease.disease_id] = DiseaseNarrative(
                diseaseId=disease.disease_id,
                relatedContent=content,
                patientSuggestion=_source_fact_disease_suggestion(disease),
            )
    validated.disease_narratives = list(completed_diseases.values())
    if not _executive_summary_matches_completed_sections(
        validated.executive_summary,
        recommendations=validated.recommendations,
        source_summary=parsed.source_summary,
    ):
        validated.executive_summary = None
    if not _executive_summary_is_complete(
        validated.executive_summary,
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
    ):
        # Generate the summary once more *after* all four report sections have
        # passed their individual validators.  Earlier retries work from the
        # raw candidate draft; using the completed, evidence-bounded sections
        # here avoids the old mechanical padding fallback that repeated table
        # rows merely to reach 300 characters.
        summary_context = {
            "enterotype": effective_enterotype,
            "abnormalMicrobesSummary": validated.abnormal_microbes_summary,
            "microbes": [
                {
                    "name": item.name,
                    "scientificName": item.scientific_name,
                    "category": item.category,
                    "sourceFinding": item.source_finding.model_dump(
                        mode="json", by_alias=True
                    ),
                    "medicalInterpretation": item.medical_interpretation,
                    "diseaseAndRisks": [
                        {
                            "name": risk.name,
                            "description": risk.description,
                        }
                        for risk in item.disease_and_risks
                    ],
                }
                for item in sorted(
                    microbes.items,
                    key=lambda row: (
                        bool(row.disease_and_risks),
                        len(row.disease_and_risks),
                    ),
                    reverse=True,
                )[:8]
            ],
            "diseases": [
                {
                    "name": item.name,
                    "medicalInterpretation": item.medical_interpretation,
                    "existingResults": [
                        result.model_dump(mode="json", by_alias=True)
                        for result in item.existing_results
                    ],
                }
                for item in sorted(
                    diseases.items,
                    key=lambda row: (
                        bool(row.medical_interpretation),
                        bool(row.existing_results),
                        bool(row.evidence_refs),
                    ),
                    reverse=True,
                )[:8]
            ],
            "hospitalFact": _executive_hospital_fact(hospital_data),
            "healthProfile": validated.health_profile,
            "recommendations": [
                item.model_dump(mode="json", by_alias=True)
                for item in validated.recommendations
            ],
        }
        for _ in range(3):
            try:
                summary_raw = await client.generate(
                    {
                        "task": "根据已完成的医学报告各部分撰写全文摘要",
                        "requiredOutput": {
                            "executiveSummary": "150至250个汉字的一段式综合结论"
                        },
                        "validatedReportSections": summary_context,
                        "strictConstraints": [
                            "只使用validatedReportSections中已给出的事实和医学结论",
                            "自然串联整体健康状态、有证据的重点风险、已有临床资料、肠型和建议关注方向",
                            "不补充新的菌种、疾病、数值、检查或建议",
                            "不展示或复述原报告模型指数、模型分值",
                            "不写检索、筛选、绑定、规则、模板、AI、JSON或后台流程",
                            "不机械罗列各分类数量、不重复表格原句、不用固定边界话术凑字数",
                            "字数控制在150至250个汉字并保持为一个自然段；只概括整体状态和最重要的关注方向，不重复指标、数值、菌种或疾病清单",
                            "建议关注方向只概括recommendations已有的健康影响，不新增指标、数值、症状、疾病或行动方案",
                            "不写检出不代表感染、需结合个体情况、需结合临床、后续持续关注、资料不足等通用边界或流程话术",
                            "不得把检出描述为定植，也不得把无证据关系的菌种高低丰度写成疾病风险或健康影响",
                            "语气采用正式医学报告表达，不出现对生成过程的解释",
                            "返回严格JSON，不使用Markdown",
                        ],
                    }
                )
                summary_draft = NarrativeDraft.model_validate(summary_raw)
                summary_draft = validate_draft(
                    summary_draft,
                    microbes=microbes,
                    diseases=diseases,
                    health_assessment=health_assessment,
                    session_context=session_context,
                    allowed_health_evidence_refs=health_evidence_refs,
                    allowed_microbe_confirmations=allowed_microbe_confirmations,
                    health_evidence_text_by_number=health_evidence_text_by_number,
                    grounded_numbers=grounded_numbers,
                    recommended_examination_candidates=(
                        recommended_examination_candidates_by_id
                    ),
                )
            except (
                NarrativeError,
                ValidationError,
                ValueError,
                TypeError,
                KeyError,
            ):
                continue
            if _executive_summary_is_complete(
                summary_draft.executive_summary,
                enterotype=effective_enterotype,
                microbes=microbes,
                diseases=diseases,
                hospital_data=hospital_data,
            ):
                validated.executive_summary = summary_draft.executive_summary
                break
    if not _executive_summary_is_complete(
        validated.executive_summary,
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
    ):
        validated.executive_summary = _extractive_executive_summary(
            health_profile=validated.health_profile,
            recommendations=validated.recommendations,
            enterotype=effective_enterotype,
            health_level=(
                validated.health_level.level
                if validated.health_level is not None
                else None
            ),
            microbes=microbes,
            diseases=diseases,
            hospital_data=hospital_data,
        )
    validated.executive_summary = _bounded_display_text(
        validated.executive_summary,
        minimum=EXECUTIVE_SUMMARY_MIN,
        maximum=EXECUTIVE_SUMMARY_MAX,
    )
    validated.abnormal_microbes_summary = _bounded_display_text(
        validated.abnormal_microbes_summary,
        minimum=150,
        maximum=400,
    )
    if microbes.items and not _display_length_in_range(
        validated.abnormal_microbes_summary,
        minimum=150,
        maximum=400,
    ):
        # A source-derived summary is used only when generated prose is absent
        # or fails validation; a valid model summary remains visible.
        validated.abnormal_microbes_summary = _extractive_microbe_summary(
            microbes=microbes,
            conclusions={
                item.microbe_id: item.conclusion
                for item in validated.microbe_narratives
            },
            has_session_context=bool(session_context),
        )
    return validated


def _is_bare_microbe_risk_restatement(value: str, *, microbe: Any) -> bool:
    """Detect prose that merely wraps hidden outcome names in a template.

    The previous exact regular expression missed common model variants such as
    ``organism X is associated with disease Y``.  Those short sentences then
    replaced the richer, evidence-bound interpretation even though they added
    nothing beyond the now-hidden relationship column.  An established
    pathogen statement (``may cause ...``) is itself a clinically meaningful
    relation; observational rows additionally need a supported direction,
    function, mechanism, manifestation, activity or severity detail.
    """

    compact = re.sub(r"[\s。，,;；：:]", "", value)
    risk_names = [
        re.sub(r"\s+", "", risk.name)
        for risk in microbe.disease_and_risks[:3]
        if risk.name
    ]
    if not compact or not risk_names:
        return False
    established = all(
        risk.relation_type in {"established_pathogen", "opportunistic"}
        for risk in microbe.disease_and_risks[:3]
    )
    if established and "可能引起" in compact:
        return False

    residual = compact
    for name in (
        microbe.name,
        getattr(microbe, "scientific_name", None),
        *risk_names,
    ):
        if name:
            residual = residual.replace(re.sub(r"\s+", "", name), "")
    explanatory_markers = (
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
    if any(marker in residual for marker in explanatory_markers):
        return False
    residual = re.sub(
        r"(?:这种菌群变化|相关变化|菌群特征|该菌|此菌|"
        r"在特定情况下|可能|与|和|及|或|并|提示|需关注|"
        r"相关风险|风险|相关|增加|升高|引起)",
        "",
        residual,
    )
    return len(re.findall(r"[\u3400-\u9fffA-Za-z]", residual)) < 8


def validate_draft(
    draft: NarrativeDraft,
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    health_assessment: HealthAssessment | None = None,
    session_context: dict[str, Any] | None = None,
    allowed_health_evidence_refs: dict[str, set[int]] | None = None,
    allowed_microbe_confirmations: dict[str, set[str]] | None = None,
    health_evidence_text_by_number: dict[int, str] | None = None,
    grounded_numbers: set[float] | None = None,
    recommended_examination_candidates: dict[str, dict[str, Any]] | None = None,
) -> NarrativeDraft:
    microbe_by_id = {item.microbe_id: item for item in microbes.items}
    disease_by_id = {item.disease_id: item for item in diseases.items}
    has_session_context = bool(session_context)
    microbe_narratives: list[MicrobeNarrative] = []
    for item in draft.microbe_narratives:
        microbe = microbe_by_id.get(item.microbe_id)
        if microbe is None:
            continue
        item.conclusion = _sanitize_microbe_narrative(
            item.conclusion,
            microbe=microbe,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
            field="conclusion",
        )
        item.clinical_role = (
            _sanitize_microbe_narrative(
                item.clinical_role or "",
                microbe=microbe,
                has_session_context=has_session_context,
                grounded_numbers=grounded_numbers,
                field="clinical_role",
            )
            or None
        )
        item.patient_interpretation = (
            _sanitize_microbe_narrative(
                item.patient_interpretation or "",
                microbe=microbe,
                has_session_context=has_session_context,
                grounded_numbers=grounded_numbers,
                field="patient_interpretation",
            )
            or None
        )
        if (
            item.patient_interpretation
            and microbe.disease_and_risks
            and _is_bare_microbe_risk_restatement(
                item.patient_interpretation,
                microbe=microbe,
            )
        ):
            # A bare restatement such as "may cause X" does not explain the
            # medical meaning requested by the public column. Mark it missing
            # so the bounded one-organism repair pass can ask for an
            # evidence-backed physiological or clinical impact.
            item.patient_interpretation = None
        if item.patient_interpretation and microbe.disease_and_risks:
            normalized_interpretation = _normalize_evidence_text(
                item.patient_interpretation
            )
            if any(
                _normalize_evidence_text(risk.name) not in normalized_interpretation
                for risk in microbe.disease_and_risks
            ) or _interpretation_adds_unbound_outcome(
                item.patient_interpretation,
                microbe=microbe,
            ):
                item.patient_interpretation = None
        if item.patient_interpretation and any(
            marker in item.patient_interpretation
            for marker in (
                "您的粪便样本中检出了该菌",
                "本次结果为检出",
                "本次粪便结果为",
                "是其已展示的健康影响",
            )
        ):
            item.patient_interpretation = None
        item.clinical_confirmation = _validated_microbe_confirmation(
            item.clinical_confirmation,
            allowed=(allowed_microbe_confirmations or {}).get(
                microbe.microbe_id, set()
            ),
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        )
        if item.conclusion:
            # A model-produced conclusion alone is not the requested medical
            # interpretation.  Mark the row as model-authored only after the
            # patient-facing interpretation survives all grounding and style
            # checks; otherwise the safe fallback will fill it later.
            if item.patient_interpretation:
                item.generation_source = "model"
            microbe_narratives.append(item)
    draft.microbe_narratives = microbe_narratives

    disease_narratives: list[DiseaseNarrative] = []
    for item in draft.disease_narratives:
        disease = disease_by_id.get(item.disease_id)
        if (
            disease is None
            or not _disease_needs_narrative(disease)
            or not disease.association_evidence_refs
        ):
            continue
        item.related_content = _sanitize_disease_narrative(
            item.related_content,
            disease=disease,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        )
        item.patient_suggestion = _sanitize_disease_patient_suggestion(
            item.patient_suggestion,
            disease=disease,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        )
        if item.related_content:
            disease_narratives.append(item)
    draft.disease_narratives = disease_narratives
    indicator_by_code = {
        item.indicator_code: item
        for item in (
            health_assessment.abnormal_indicators
            if health_assessment is not None
            else []
        )
    }
    abnormal_indicator_summaries: list[AbnormalIndicatorSummaryDraft] = []
    seen_indicator_codes: set[str] = set()
    for item in draft.abnormal_indicator_summaries:
        indicator = indicator_by_code.get(item.indicator_code)
        if indicator is None or item.indicator_code in seen_indicator_codes:
            continue
        text = _sanitize_abnormal_indicator_summary(
            item.clinical_significance,
            indicator=indicator,
        )
        if not text:
            continue
        item.clinical_significance = text
        abnormal_indicator_summaries.append(item)
        seen_indicator_codes.add(item.indicator_code)
    draft.abnormal_indicator_summaries = abnormal_indicator_summaries
    draft.executive_summary = _validated_text(
        draft.executive_summary,
        has_session_context=has_session_context,
        grounded_numbers=grounded_numbers,
    )
    draft.abnormal_microbes_summary = _validated_microbe_summary(
        draft.abnormal_microbes_summary,
        microbes=microbes,
        has_session_context=has_session_context,
        grounded_numbers=grounded_numbers,
    )
    health_profile = _remove_model_health_claims(draft.health_profile or "")
    health_profile = re.sub(
        r"既往(?:疾病列表|疾病|病史)中?(?:包括|有)",
        "原报告列出的疾病关联项目包括",
        health_profile,
    )
    health_profile = re.sub(
        r"(?:同时[，,])?您还存在([^。]{1,120}?)(?:等)?(?:消化系统相关)?健康问题",
        r"原报告列出的疾病或健康关联项目包括\1",
        health_profile,
    )
    health_profile = re.sub(
        r"原报告(?:同时)?(?:提及|列出)([^。]{1,160}?)(?:等)?健康问题",
        r"原报告列出的疾病或健康关联项目包括\1",
        health_profile,
    )
    health_profile = re.sub(
        r"原报告列出的疾病或健康关联项目包括(?:的)?(?:包括)+",
        "原报告列出的疾病或健康关联项目包括",
        health_profile,
    )
    health_profile = re.sub(
        r"(?:您还(?:有|存在))?原报告列出的疾病或健康关联项目包括的?",
        "原报告列出的疾病或健康关联项目包括",
        health_profile,
    )
    health_profile = _sanitize_health_profile(health_profile)
    draft.health_profile = _validated_text(
        health_profile,
        has_session_context=has_session_context,
        grounded_numbers=grounded_numbers,
    )
    if draft.health_profile:
        draft.health_profile = re.sub(
            r"[，,](?:该|此)肠型[^。；;]{0,80}(?:相关|有关)",
            "",
            draft.health_profile,
        )
        draft.health_profile = re.sub(
            r"[，,]这些情况与[^。；;]{0,120}(?:共同)?提示[^。；;]*",
            "",
            draft.health_profile,
        )
    if draft.health_level:
        draft.health_level.level = _normalize_health_level(draft.health_level.level)
        health_summary = _remove_model_health_claims(draft.health_level.summary)
        health_summary = re.sub(
            r"原报告关联([^。]{1,120}?)等多个健康问题",
            r"原报告列出的疾病或健康关联项目包括\1等",
            health_summary,
        )
        if draft.health_level.level not in {
            "较低关注",
            "中度关注",
            "重点关注",
        } or not (
            summary := _validated_text(
                health_summary,
                has_session_context=has_session_context,
                grounded_numbers=grounded_numbers,
            )
        ):
            draft.health_level = None
        else:
            normalized_summary = summary.rstrip("；;")
            draft.health_level.summary = normalized_summary + (
                "。" if normalized_summary[-1:] not in "。！？!?" else ""
            )
            model_basis_allowed = any(
                any(
                    level in (disease.source_result or "")
                    for level in ("高风险", "中风险", "低风险", "偏高", "偏低")
                )
                for disease in diseases.items
            )
            draft.health_level.basis = [
                text
                for item in draft.health_level.basis
                if (
                    text := _validated_text(
                        item,
                        has_session_context=has_session_context,
                        grounded_numbers=grounded_numbers,
                    )
                )
                is not None
                and _basis_is_patient_facing(text)
                and (model_basis_allowed or "模型" not in text)
            ][:8]
    allowed_categories = {"diet", "exercise", "sleep", "bowel_habits", "general"}
    recommendations = []
    seen_attention_topics: set[str] = set()
    for item in draft.recommendations:
        item.category = item.category if item.category in allowed_categories else "general"
        item.topic = _validated_attention_topic(
            item.topic,
            category=item.category,
            health_assessment=health_assessment,
        )
        item.priority = _validated_attention_priority(
            item.priority,
            topic=item.topic,
            category=item.category,
            health_assessment=health_assessment,
        )
        item.content = _validated_text(
            item.content,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        ) or ""
        item.content = patient_facing_text(
            _strip_evidence_title_prefix(item.content).strip()
        ) or ""
        if item.content and item.content[-1] not in "。！？!?":
            item.content += "。"
        if (
            not item.topic
            or item.priority not in {"重点关注", "建议关注"}
            or _contains_unsupported_absolute_claim(item.content)
            or not _attention_direction_is_valid(item.content)
            or not _attention_examinations_are_allowed(
                item.content,
                candidates=recommended_examination_candidates,
            )
        ):
            continue
        if allowed_health_evidence_refs is not None:
            category_refs = set(
                allowed_health_evidence_refs.get(item.category, set())
            )
            item.evidence_refs = sorted(
                set(item.evidence_refs) & category_refs
            )
            if (
                health_evidence_text_by_number is None
                and item.category == "general"
                and len(_attention_action_domains(item.content) - {"clinical"}) > 1
            ):
                # Multiple actions cannot be bound to individual references
                # when the source text is unavailable for verification.
                continue
            if health_evidence_text_by_number is not None and item.evidence_refs:
                item.evidence_refs = [
                    number
                    for number in item.evidence_refs
                    if _attention_evidence_supports_action(
                        content=item.content,
                        evidence_text=health_evidence_text_by_number.get(number, ""),
                        category=item.category,
                    )
                ]
                required_domains = _attention_action_domains(item.content) - {
                    "clinical"
                }
                if required_domains and not required_domains.issubset(
                    set().union(
                        *(
                            _attention_action_domains(
                                health_evidence_text_by_number.get(number, "")
                            )
                            for number in item.evidence_refs
                        )
                    )
                ):
                    continue
            if not item.evidence_refs and not _attention_has_assessment_support(
                item.topic,
                category=item.category,
                health_assessment=health_assessment,
            ):
                continue
        topic_key = _normalize_evidence_text(item.topic)
        if topic_key in seen_attention_topics:
            continue
        seen_attention_topics.add(topic_key)
        recommendations.append(item)
    recommendations.sort(
        key=lambda item: (
            0 if item.priority == "重点关注" else 1,
            item.topic or "",
        )
    )
    draft.recommendations = recommendations[:3]
    recommended_examinations: list[RecommendedExaminationDraft] = []
    seen_examination_keys: set[tuple[str, str]] = set()
    candidate_map = recommended_examination_candidates or {}
    for item in draft.recommended_examinations:
        candidate = candidate_map.get(item.candidate_id)
        reason = _validated_text(
            item.reason,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        )
        if (
            candidate is None
            or not reason
            or item.hospital_common is not True
            or candidate.get("patientIndication") is False
        ):
            continue
        name = str(candidate.get("name") or "").strip()
        purpose = str(candidate.get("purpose") or "").strip()
        refs = sorted(
            {
                int(number)
                for number in candidate.get("evidenceRefs") or []
                if str(number).isdigit() and int(number) > 0
            }
        )
        key = (re.sub(r"\s+", "", name).casefold(), purpose)
        if (
            not name
            or purpose not in {"筛查", "确认", "评估"}
            or (not refs and candidate.get("modelSuggested") is not True)
            or _obviously_not_hospital_examination(
                name,
                rationale=" ".join(
                    str(value) for value in candidate.get("rationales") or []
                ),
            )
        ):
            continue
        if key in seen_examination_keys:
            continue
        item.name = name
        item.purpose = purpose
        item.reason = reason
        item.evidence_refs = refs
        recommended_examinations.append(item)
        seen_examination_keys.add(key)
    draft.recommended_examinations = recommended_examinations[:3]
    return draft


def ensure_source_fact_row_narratives(
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
) -> None:
    """Populate every source row before the optional final narrative pass.

    Evidence retrieval and the final prose request share the same report
    deadline.  Previously, a timeout during the prose request returned a
    progress snapshot in which otherwise valid source rows had blank medical
    interpretation cells.  These deterministic fallbacks contain only the
    measured source fact and already validated evidence relations.  They may
    later be replaced by ``apply_draft`` but never require model completion.
    """

    for microbe in microbes.items:
        narrative = _source_fact_microbe_narrative(microbe)
        microbe.conclusion = microbe.conclusion or narrative.conclusion
        microbe.patient_interpretation = (
            microbe.patient_interpretation or narrative.patient_interpretation
        )
        microbe.medical_interpretation = (
            microbe.medical_interpretation or narrative.patient_interpretation
        )
    for disease in diseases.items:
        if not disease.medical_interpretation:
            content = _source_fact_disease_narrative(disease, microbes=microbes)
            if content:
                disease.related_content = content
                disease.medical_interpretation = content
        if not disease.patient_suggestion:
            disease.patient_suggestion = _source_fact_disease_suggestion(disease)


def apply_draft(
    draft: NarrativeDraft,
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    health: HealthManagementSection,
    hospital_data: list[HospitalDataInput] | None = None,
) -> str | None:
    microbe_by_id = {item.microbe_id: item for item in microbes.items}
    for item in draft.microbe_narratives:
        target = microbe_by_id[item.microbe_id]
        target.clinical_role = item.clinical_role
        target.conclusion = item.conclusion
        target.patient_interpretation = item.patient_interpretation
        target.medical_interpretation = item.patient_interpretation
        target.clinical_confirmation = item.clinical_confirmation
        target.model_interpretation_generated = item.generation_source == "model"
    disease_by_id = {item.disease_id: item for item in diseases.items}
    for item in draft.disease_narratives:
        target = disease_by_id[item.disease_id]
        # `validate_draft` only retains prose generated from this disease's own
        # bound evidence and existing results. It can therefore replace the
        # earlier extractive placeholder without changing citation ownership.
        target.related_content = item.related_content
        target.medical_interpretation = item.related_content
        if item.patient_suggestion:
            target.patient_suggestion = item.patient_suggestion
    microbes.summary = draft.abnormal_microbes_summary
    if draft.health_level:
        level = _supported_health_attention_level(
            microbes=microbes,
            diseases=diseases,
        )
        summary = draft.health_level.summary
        if level != "重点关注":
            summary = (
                summary.replace("需重点关注", "值得关注")
                .replace("需要重点关注", "值得关注")
                .replace("需要重点管理", "值得关注")
                .replace("需重点留意", "值得关注")
            )
        health.health_level = HealthLevel(
            level=level,
            summary=summary,
            basis=draft.health_level.basis,
        )
    health.health_profile = draft.health_profile
    if draft.recommendations:
        model_recommendations = [
            HealthRecommendation(
                category=item.category,
                topic=item.topic,
                priority=item.priority,
                content=item.content,
                evidence_refs=item.evidence_refs,
            )
            for item in draft.recommendations
        ]
        # Keep the evidence-bounded checkpoint created before the model call.
        # A valid model response can contain only one direction; merging fills
        # the public 3-5 direction summary without another model request or
        # any newly invented health fact.
        health.recommendations = _merge_attention_directions(
            [*model_recommendations, *health.recommendations]
        )
    health.recommended_examinations = [
        HealthRecommendedExamination(
            name=item.name,
            purpose=item.purpose,
            reason=item.reason,
            evidence_refs=item.evidence_refs,
        )
        for item in draft.recommended_examinations
        if item.name and item.purpose
    ]
    executive_summary = draft.executive_summary
    effective_hospital_data = hospital_data or []
    effective_enterotype = (
        health.enterotype.name if health.enterotype is not None else None
    )
    # Narrative validation runs before the final health fallbacks are applied.
    # If the model summary is missing at that point, build it once more from
    # the completed public sections so a valid evidence-backed report does not
    # lose its summary merely because one model field failed validation.
    if not _executive_summary_is_complete(
        executive_summary,
        enterotype=effective_enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=effective_hospital_data,
    ):
        executive_summary = _extractive_executive_summary(
            health_profile=health.health_profile,
            recommendations=health.recommendations,
            enterotype=effective_enterotype,
            health_level=(
                health.health_level.level if health.health_level is not None else None
            ),
            microbes=microbes,
            diseases=diseases,
            hospital_data=effective_hospital_data,
        )
    return executive_summary


def _merge_attention_directions(
    recommendations: list[HealthRecommendation],
) -> list[HealthRecommendation]:
    ranked = sorted(
        enumerate(recommendations),
        key=lambda pair: (
            0 if pair[1].priority == "重点关注" else 1,
            pair[0],
        ),
    )
    output: list[HealthRecommendation] = []
    for _index, item in ranked:
        if patient_health_topic_is_generic(item.topic):
            continue
        overlapping_index = next(
            (
                index
                for index, existing in enumerate(output)
                if (
                    patient_topics_overlap(existing.topic, item.topic)
                    or patient_health_topic_domain(existing.topic)
                    == patient_health_topic_domain(item.topic)
                )
            ),
            None,
        )
        if overlapping_index is not None:
            existing = output[overlapping_index]
            if (
                existing.priority == item.priority
                and patient_topics_overlap(existing.topic, item.topic)
                and len(patient_topic_key(item.topic))
                > len(patient_topic_key(existing.topic))
            ):
                output[overlapping_index] = item
            continue
        output.append(item)
        if len(output) == 3:
            break
    return output


def _patient_health_target_categories(
    *,
    parsed: ParsedMicrobiomeReport | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    health_assessment: HealthAssessment | None,
    session_context: dict[str, Any] | None,
) -> set[str]:
    """Return lifestyle topics supported by an actual patient-side target.

    Evidence availability alone must not create a sleep, exercise or bowel
    recommendation. A topic is eligible only when this request contains a
    corresponding assessment concern, clinical fact, symptom or evidence-bound
    source-report disease item.
    """

    categories: set[str] = set()
    attention_codes = {
        item.code
        for item in (health_assessment.dimensions if health_assessment else [])
        if item.score_available and item.level in {"attention", "priority_attention"}
    }
    if attention_codes & {
        "metabolic_system",
        "digestive_system",
    }:
        categories.add("diet")
    if attention_codes & {
        "metabolic_system",
        "cardiovascular_system",
    }:
        categories.add("exercise")

    values: list[Any] = [
        [item.model_dump(mode="json", by_alias=True) for item in hospital_data],
        session_context or {},
    ]
    if parsed is not None:
        values.append(parsed.model_dump(mode="json", by_alias=True, exclude_none=True))
    patient_text = json.dumps(values, ensure_ascii=False).casefold()
    disease_text = " ".join(
        item.name
        for item in diseases.items
        if item.medical_interpretation and item.association_evidence_refs
    ).casefold()
    if any(
        marker in f"{patient_text} {disease_text}"
        for marker in ("便秘", "腹泻", "排便", "大便", "腹胀", "肠易激")
    ):
        categories.add("bowel_habits")
    if any(
        marker in patient_text
        for marker in (
            "睡眠不足",
            "失眠",
            "睡眠质量差",
            "入睡困难",
            "作息不规律",
            "sleep deprivation",
            "insomnia",
        )
    ):
        categories.add("sleep")
    abnormal_hospital_text = " ".join(
        f"{item.item_name} {item.result_status_name or ''} {item.exam_result or ''}".casefold()
        for item in hospital_data
        if _hospital_row_is_explicitly_abnormal(item)
    )
    if any(
        marker in f"{abnormal_hospital_text} {disease_text}"
        for marker in (
            "血脂",
            "胆固醇",
            "甘油三酯",
            "ldl",
            "血糖",
            "肥胖",
            "体重",
            "bmi",
            "腰围",
        )
    ):
        categories.update({"diet", "exercise"})
    if microbes.items:
        categories.add("diet")
    return categories


def _hospital_row_is_explicitly_abnormal(item: HospitalDataInput) -> bool:
    status_name = (item.result_status_name or "").strip().casefold()
    if any(
        marker in status_name
        for marker in ("正常", "未见异常", "阴性", "normal", "negative")
    ):
        return False
    if any(
        marker in status_name
        for marker in (
            "偏高",
            "偏低",
            "升高",
            "降低",
            "超标",
            "异常",
            "阳性",
            "high",
            "low",
            "abnormal",
            "positive",
        )
    ):
        return True
    return str(item.result_status).strip().casefold() in {
        "1",
        "2",
        "high",
        "low",
        "abnormal",
        "positive",
    }


def _fallback_attention_directions(
    *,
    existing: list[HealthRecommendation],
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    health_assessment: HealthAssessment | None,
    available_numbers: set[int],
) -> list[HealthRecommendation]:
    candidates: list[tuple[int, int, HealthRecommendation]] = []
    source_index = 0

    for item in existing:
        topic = _validated_attention_topic(
            item.topic,
            category=item.category,
            health_assessment=health_assessment,
        )
        priority = _validated_attention_priority(
            item.priority,
            topic=topic,
            category=item.category,
            health_assessment=health_assessment,
        )
        if topic and _attention_direction_is_valid(item.content):
            candidates.append(
                (
                    0 if priority == "重点关注" else 1,
                    source_index,
                    item.model_copy(
                        update={
                            "topic": topic,
                            "priority": priority,
                            "evidence_refs": sorted(
                                set(item.evidence_refs) & available_numbers
                            ),
                        }
                    ),
                )
            )
            source_index += 1

    for dimension in health_assessment.dimensions if health_assessment else []:
        if not dimension.score_available or dimension.level not in {
            "attention",
            "priority_attention",
        }:
            continue
        content = _dimension_attention_direction(
            dimension.code,
            priority=dimension.level == "priority_attention",
        )
        if not content:
            continue
        priority = (
            "重点关注"
            if dimension.level == "priority_attention"
            else "建议关注"
        )
        candidates.append(
            (
                0 if priority == "重点关注" else 1,
                source_index,
                HealthRecommendation(
                    category="general",
                    topic=dimension.name,
                    priority=priority,
                    content=content,
                    evidence_refs=[],
                ),
            )
        )
        source_index += 1

    # A dimension can remain unscored (for example because one required item
    # conflicts) or still land in the broad ``good`` band while containing a
    # source-confirmed abnormal indicator.  Dropping those indicators here made
    # otherwise complete clinical reports return no health directions at all.
    # Use the fixed dimension action text, never the raw model prose, and let
    # the normal topic/domain deduplication below collapse overlaps.
    dimension_names = {
        dimension.code: dimension.name
        for dimension in health_assessment.dimensions
    } if health_assessment else {}
    for indicator in health_assessment.abnormal_indicators if health_assessment else []:
        content = _dimension_attention_direction(
            indicator.dimension_code,
            priority=indicator.severity == "priority_attention",
        )
        if not content:
            continue
        priority = (
            "重点关注"
            if indicator.severity == "priority_attention"
            else "建议关注"
        )
        candidates.append(
            (
                0 if priority == "重点关注" else 1,
                source_index,
                HealthRecommendation(
                    category="general",
                    topic=(
                        dimension_names.get(indicator.dimension_code)
                        or indicator.dimension_name
                    ),
                    priority=priority,
                    content=content,
                    evidence_refs=[],
                ),
            )
        )
        source_index += 1

    # Disease and microbe rows already explain their individual risks.  The
    # health summary intentionally does not turn them into another list of the
    # same items; fallback actions come only from cross-module dimensions.
    del microbes, diseases

    candidates.sort(key=lambda item: (item[0], item[1]))
    output: list[HealthRecommendation] = []
    seen_topics: set[str] = set()
    seen_domains: set[str] = set()
    for _priority, _index, item in candidates:
        topic_key = _normalize_evidence_text(item.topic or "")
        domain = patient_health_topic_domain(item.topic)
        if (
            not topic_key
            or topic_key in seen_topics
            or (domain and domain in seen_domains)
        ):
            continue
        output.append(item)
        seen_topics.add(topic_key)
        if domain:
            seen_domains.add(domain)
        if len(output) >= 3:
            break
    return output


def _dimension_attention_direction(code: str, *, priority: bool) -> str | None:
    descriptions = {
        "digestive_system": (
            "保持饮食结构多样和作息规律，持续观察腹痛、腹胀"
            "及排便变化；相关不适持续或加重时前往消化专科评估。"
        ),
        "metabolic_system": (
            "优先调整饮食结构，将有氧运动与抗阻训练结合，并管理"
            "体重变化；按医生建议复查糖脂代谢相关项目，根据趋势调整方案。"
        ),
        "cardiovascular_system": (
            "持续管理饮食、体重和运动习惯，并按医生建议复查心血管"
            "相关指标；出现胸痛、胸闷或活动耐量明显下降时及时就医。"
        ),
        "urinary_system": (
            "优先管理饮食结构、体重和水分摄入，按医生建议复查肾功能"
            "及尿酸相关项目，根据变化趋势调整后续管理。"
        ),
        "immune_system": (
            "保持规律作息并关注持续炎症或免疫相关不适；已有明确症状"
            "或检查异常时，由相应专科结合临床资料进一步评估。"
        ),
        "nervous_system": (
            "关注记忆、运动协调及感觉变化；相关表现持续或加重时，"
            "前往神经内科评估。"
        ),
        "mental_system": (
            "持续关注情绪、睡眠和日常功能变化；明显影响生活或持续"
            "加重时，及时寻求精神心理专业评估。"
        ),
        "tumor_risk": (
            "结合年龄、症状和既往检查落实相应筛查；出现持续异常表现"
            "时，由医生判断是否需要进一步检查。"
        ),
    }
    del priority
    return descriptions.get(code)


def _clinical_result_is_abnormal(result: Any) -> bool:
    status = str(getattr(result, "status", None) or "")
    if any(marker in status for marker in ("正常", "阴性", "未见异常", "无明显异常")):
        return False
    return any(
        marker in status
        for marker in ("严重", "显著", "偏高", "偏低", "升高", "降低", "异常", "阳性")
    )


def apply_extractive_health_fallback(
    *,
    health: HealthManagementSection,
    evidence_summaries: list[dict[str, Any]],
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    health_assessment: HealthAssessment | None = None,
) -> str | None:
    """Checkpoint a concise conclusion and source-bounded attention directions."""

    available_numbers = {
        int(item.get("number") or 0)
        for item in evidence_summaries
        if str(item.get("number") or "").isdigit()
        and int(item.get("number") or 0) > 0
    }
    health.recommendations = _fallback_attention_directions(
        existing=health.recommendations,
        microbes=microbes,
        diseases=diseases,
        health_assessment=health_assessment,
        available_numbers=available_numbers,
    )

    enterotype = health.enterotype.name if health.enterotype is not None else None
    if not health.health_profile:
        health.health_profile = _extractive_health_profile(
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
            hospital_data=hospital_data,
            health_assessment=health_assessment,
        )
    if health.health_level is None:
        level_summary = _extractive_health_level_summary(
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
            hospital_data=hospital_data,
        )
        if level_summary:
            health.health_level = HealthLevel(
                level=_supported_health_attention_level(
                    microbes=microbes,
                    diseases=diseases,
                ),
                summary=level_summary,
                basis=[],
            )
    return _extractive_executive_summary(
        health_profile=health.health_profile,
        recommendations=health.recommendations,
        enterotype=enterotype,
        health_level=(
            health.health_level.level if health.health_level is not None else None
        ),
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
    )


def _supported_health_attention_level(
    *, microbes: AbnormalMicrobesSection, diseases: DiseaseRisksSection
) -> str:
    explicit_high = any(
        any(
            marker in (item.source_finding.status or "")
            for marker in ("严重异常", "显著升高", "高风险", "强阳性")
        )
        for item in microbes.items
    ) or any(
        any(
            marker in (item.source_result or "")
            for marker in ("严重异常", "显著升高", "高风险", "强阳性")
        )
        for item in diseases.items
    )
    if explicit_high:
        return "重点关注"
    if any(item.disease_and_risks for item in microbes.items) or diseases.items:
        return "中度关注"
    return "较低关注"


def _missing_narrative_fields(
    draft: NarrativeDraft,
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
) -> dict[str, Any]:
    completed_microbes = {
        item.microbe_id
        for item in draft.microbe_narratives
        # The public column is the patient interpretation. A clinical-role
        # fragment by itself must not suppress the bounded repair request;
        # otherwise rows such as "this is an opportunistic organism" are
        # mistaken for a completed individualized medical explanation.
        if item.conclusion and item.patient_interpretation
    }
    completed_diseases = {item.disease_id for item in draft.disease_narratives}
    fields = []
    if not _display_length_in_range(
        draft.executive_summary,
        minimum=EXECUTIVE_SUMMARY_MIN,
        maximum=EXECUTIVE_SUMMARY_MAX,
    ):
        fields.append("executiveSummary")
    if not _display_length_in_range(
        draft.abnormal_microbes_summary,
        minimum=150,
        maximum=400,
    ):
        fields.append("abnormalMicrobesSummary")
    if not draft.health_level:
        fields.append("healthLevel")
    if not draft.health_profile:
        fields.append("healthProfile")
    # One accepted action direction is not a complete patient summary. Keep
    # repairing until the intended minimum of two distinct directions has
    # survived the evidence and wording validators.
    if len(draft.recommendations) < 2:
        fields.append("recommendations")
    return {
        "fields": fields,
        "microbeIds": [
            item.microbe_id
            for item in microbes.items
            if item.microbe_id not in completed_microbes
            and (
                bool(item.disease_and_risks)
                or bool(getattr(item, "model_interpretation_required", False))
            )
        ],
        "diseaseIds": [
            item.disease_id
            for item in diseases.items
            if item.disease_id not in completed_diseases
            and _disease_needs_narrative(item)
        ],
    }


def _merge_missing_narrative(
    original: NarrativeDraft,
    retry: NarrativeDraft,
    missing: dict[str, Any] | None = None,
) -> NarrativeDraft:
    fields = set((missing or {}).get("fields") or [])
    if (
        not original.executive_summary or "executiveSummary" in fields
    ) and retry.executive_summary:
        original.executive_summary = retry.executive_summary
    if (
        not original.abnormal_microbes_summary or "abnormalMicrobesSummary" in fields
    ) and retry.abnormal_microbes_summary:
        original.abnormal_microbes_summary = retry.abnormal_microbes_summary
    if (not original.health_level or "healthLevel" in fields) and retry.health_level:
        original.health_level = retry.health_level
    if (
        not original.health_profile or "healthProfile" in fields
    ) and retry.health_profile:
        original.health_profile = retry.health_profile
    if (
        not original.recommendations or "recommendations" in fields
    ) and retry.recommendations:
        by_topic = {
            _normalize_evidence_text(item.topic or item.category): item
            for item in original.recommendations
        }
        for item in retry.recommendations:
            by_topic[_normalize_evidence_text(item.topic or item.category)] = item
        original.recommendations = list(by_topic.values())
    if retry.assessment_explanations:
        by_code = {item.code: item for item in original.assessment_explanations}
        for item in retry.assessment_explanations:
            by_code[item.code] = item
        original.assessment_explanations = list(by_code.values())
    if retry.abnormal_indicator_summaries:
        by_indicator = {
            item.indicator_code: item
            for item in original.abnormal_indicator_summaries
        }
        for item in retry.abnormal_indicator_summaries:
            by_indicator[item.indicator_code] = item
        original.abnormal_indicator_summaries = list(by_indicator.values())

    microbes_by_id = {item.microbe_id: item for item in original.microbe_narratives}
    for item in retry.microbe_narratives:
        existing = microbes_by_id.get(item.microbe_id)
        if existing is None:
            original.microbe_narratives.append(item)
            microbes_by_id[item.microbe_id] = item
            continue
        if not existing.clinical_role and item.clinical_role:
            existing.clinical_role = item.clinical_role
        if not existing.patient_interpretation and item.patient_interpretation:
            existing.patient_interpretation = item.patient_interpretation
            if item.generation_source == "model":
                existing.generation_source = "model"
        if not existing.clinical_confirmation and item.clinical_confirmation:
            existing.clinical_confirmation = item.clinical_confirmation
        if not existing.conclusion and item.conclusion:
            existing.conclusion = item.conclusion
    disease_ids = {item.disease_id for item in original.disease_narratives}
    original.disease_narratives.extend(
        item for item in retry.disease_narratives if item.disease_id not in disease_ids
    )
    return original


def _narrative_retry_batches(
    missing: dict[str, Any], *, batch_size: int = 10
) -> list[dict[str, Any]]:
    tagged_ids = [("microbeIds", item) for item in missing.get("microbeIds") or []] + [
        ("diseaseIds", item) for item in missing.get("diseaseIds") or []
    ]
    if not tagged_ids:
        return [
            {
                "fields": list(missing.get("fields") or []),
                "microbeIds": [],
                "diseaseIds": [],
                "recommendationCategories": list(
                    missing.get("recommendationCategories") or []
                ),
            }
        ]
    batches = []
    for offset in range(0, len(tagged_ids), max(1, batch_size)):
        batch = tagged_ids[offset : offset + max(1, batch_size)]
        batches.append(
            {
                "fields": list(missing.get("fields") or []) if offset == 0 else [],
                "microbeIds": [item for kind, item in batch if kind == "microbeIds"],
                "diseaseIds": [item for kind, item in batch if kind == "diseaseIds"],
                "recommendationCategories": (
                    list(missing.get("recommendationCategories") or [])
                    if offset == 0
                    else []
                ),
            }
        )
    return batches


def _validated_text(
    value: str | None,
    *,
    has_session_context: bool = False,
    grounded_numbers: set[float] | None = None,
) -> str | None:
    if value is None:
        return None
    value = _normalize_enterotype_prose(value)
    return (
        _sanitize_sentences(
            value,
            predicate=lambda sentence: (
                _safe_medical_prose(sentence)
                and _safe_symptom_claim(sentence, has_context=has_session_context)
                and not _contains_unsupported_absolute_claim(sentence)
                and not _contains_unsupported_general_level(sentence)
                and not _is_missing_data_filler(sentence)
                and not _is_boundary_boilerplate(sentence)
                and not _is_overlong_enumeration(sentence)
                and _numbers_are_grounded(sentence, grounded_numbers)
            ),
        )
        or None
    )


def _display_length_in_range(
    value: str | None,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    if not value:
        return False
    length = len("".join(value.split()))
    return minimum <= length <= maximum


_HEALTH_PROFILE_META_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:本|这|此)(?:次|份)?报告(?:的)?(?:用于|旨在|目的|作用|生成|出具)"),
    re.compile(r"(?:生成|出具|需要)(?:这|此|本)(?:份|次)?报告"),
    re.compile(r"形成[^。；;]{0,24}(?:关注|管理)顺序"),
    re.compile(r"便于(?:患者|用户|读者)?(?:理解|阅读|查看)"),
)


def _sanitize_health_profile(value: str | None) -> str:
    """Remove display-only repetition and report-generation explanations."""

    text = patient_facing_text(value) or ""
    if not text:
        return ""
    text = (
        text.replace(
            "肠道感染相关风险",
            "胃肠道症状变化及必要时的病原学确认",
        )
        .replace("肠道感染风险", "胃肠道症状变化及必要时的病原学确认")
        .replace("感染相关风险", "相关症状变化及必要时的临床确认")
    )
    # Enterotype remains available in the fixed JSON field, but the health
    # portrait must stand on its own and must not make it reappear in HTML.
    text = re.sub(
        r"(?:本次|当前)?(?:为)?肠型(?:为|：|:)?(?:肠型)?"
        r"(?:I{1,3}|Ⅰ|Ⅱ|Ⅲ|一|二|三)(?:型)?"
        r"(?:[，,；;。]\s*(?:并|同时)?\s*)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    retained = [
        sentence
        for sentence in re.split(r"(?<=[。！？!?])\s*", text)
        if sentence.strip()
        and not any(pattern.search(sentence) for pattern in _HEALTH_PROFILE_META_PATTERNS)
    ]
    return patient_facing_text("".join(retained)) or ""


def _health_profile_is_complete(
    value: str | None,
    *,
    enterotype: str | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
) -> bool:
    if not value:
        return False
    if _sanitize_health_profile(value) != patient_facing_text(value):
        return False
    # Keep the portrait readable and concise. Detailed findings belong to the
    # disease, microbe and indicator sections instead of this summary.
    length = len("".join(value.split()))
    if length < 100 or length > 180:
        return False
    del enterotype
    contextual_only_risks = _contextual_only_risk_names(microbes)
    risk_text = value
    for microbe in microbes.items:
        risk_text = risk_text.replace(microbe.name, "").replace(
            microbe.scientific_name, ""
        )
    # A name can legitimately appear because the source report itself contains
    # that disease row, even when a separate taxon paper about the same disease
    # is contextual only.  Do not mistake the source-report fact for use of the
    # contextual taxon relation.
    for disease in diseases.items:
        risk_text = risk_text.replace(disease.name, "")
    risk_text = _remove_direct_risk_names(risk_text, microbes)
    if any(name in risk_text for name in contextual_only_risks):
        return False
    # The portrait represents overall health. It is valid when it does not
    # introduce a context-only disease claim; it no longer has to repeat an
    # enterotype, a microbe name and a provider disease item just to pass a
    # completeness gate.
    return True


def _risk_is_report_context_applicable(risk: Any) -> bool:
    return risk.patient_applicability in {
        "direct",
        "contextual",
    } and risk.sample_alignment in {"stool", "mucosa"}


def _microbe_has_public_narrative_evidence(
    microbe: Any,
    *,
    evidence_by_number: dict[int, dict[str, Any]],
) -> bool:
    """Match the model workload to rows that the callback can publish."""

    status = (microbe.source_finding.status or "").casefold()
    low_result = any(
        marker in status
        for marker in ("丰度低", "偏低", "降低", "减少", "缺乏", "未检出", "阴性")
    )
    for risk in microbe.disease_and_risks:
        if not risk.name or not risk.evidence_refs:
            continue
        if not _risk_is_report_context_applicable(risk):
            continue
        if risk.relation_type == "environmental_or_dietary":
            continue
        if (
            risk.relation_type in {"established_pathogen", "opportunistic"}
            and low_result
        ):
            continue
        if low_result and microbe.category in {
            "pathogenic_signal",
            "opportunistic_signal",
            "fungi_pathogenic_signal",
        }:
            if risk.relation_type != "epidemiologic_association":
                continue
            if not any(
                (
                    evidence_by_number.get(number, {}).get("interpretation") or {}
                ).get("direction")
                == "decreased"
                for number in risk.evidence_refs
            ):
                continue
        return True
    # A directly aligned human organism-role record can support a professional
    # interpretation without creating a disease relationship.  Such rows still
    # need the model prose pass; otherwise every no-risk organism receives the
    # same extractive pathogen/colonisation template.
    for number in microbe.interpretation_evidence_refs:
        evidence = evidence_by_number.get(number) or {}
        interpretation = evidence.get("interpretation") or {}
        if microbe.microbe_id not in (evidence.get("microbeIds") or []):
            continue
        if interpretation.get("targetAlignment") != "direct":
            continue
        if interpretation.get("patientApplicability") == "not_applicable":
            continue
        if not interpretation.get("claim"):
            continue
        if set(evidence.get("intents") or []) & {
            "microbe_discovery",
            "microbe_clinical_significance",
            "microbe_association",
        }:
            return True
    return False


def _contextual_only_risk_names(
    microbes: AbnormalMicrobesSection,
) -> set[str]:
    direct = {
        risk.name
        for item in microbes.items
        for risk in item.disease_and_risks
        if _risk_is_report_context_applicable(risk)
    }
    contextual = {
        risk.name
        for item in microbes.items
        for risk in item.disease_and_risks
        if not _risk_is_report_context_applicable(risk)
    }
    return contextual - direct


def _remove_direct_risk_names(
    value: str,
    microbes: AbnormalMicrobesSection,
) -> str:
    direct = {
        risk.name
        for item in microbes.items
        for risk in item.disease_and_risks
        if _risk_is_report_context_applicable(risk)
    }
    for name in sorted(direct, key=len, reverse=True):
        value = value.replace(name, "")
    return value


def _extractive_health_profile(
    *,
    enterotype: str | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
    health_assessment: HealthAssessment | None = None,
) -> str | None:
    """Build one concise overall portrait without repeating report rows."""

    dimension_focuses: list[str] = []
    if health_assessment is not None:
        ordered = sorted(
            enumerate(health_assessment.dimensions),
            key=lambda pair: (
                {
                    "priority_attention": 0,
                    "attention": 1,
                }.get(pair[1].level, 2),
                pair[1].score if pair[1].score_available else 101,
                pair[0],
            ),
        )
        dimension_focuses = [
            item.name
            for _index, item in ordered
            if item.score_available
            and item.level in {"priority_attention", "attention"}
        ]

    clinical_focuses: list[str] = []
    for item in hospital_data:
        text = f"{item.item_name or ''}{item.exam_result or ''}"
        status = item.result_status_name or ""
        abnormal_status = bool(status) and not any(
            marker in status for marker in ("正常", "阴性", "未见异常")
        )
        abnormal = abnormal_status or (
            bool(item.exam_result)
            and not any(
                marker in (item.exam_result or "")
                for marker in ("未见明显异常", "未见异常", "正常范围", "大致正常")
            )
        )
        if not abnormal:
            continue
        if any(marker in text for marker in ("血脂", "胆固醇", "低密度", "甘油三酯", "血糖")):
            clinical_focuses.append("代谢与心血管健康")
        elif any(marker in text for marker in ("胃镜", "肠镜", "胃炎", "消化", "胃肠")):
            clinical_focuses.append("胃肠道健康")
        elif any(marker in text for marker in ("肺结节", "肺部", "肺间质", "胸部CT", "呼吸")):
            clinical_focuses.append("呼吸系统健康")
        elif any(marker in text for marker in ("脂肪肝", "肝功能", "肝脏", "胆囊", "胆石", "胆结石")):
            clinical_focuses.append("肝胆与代谢健康")
        elif any(marker in text for marker in ("骨密度", "骨量", "颈椎", "腰椎", "关节")):
            clinical_focuses.append("骨骼与运动系统健康")
        elif any(marker in text for marker in ("前列腺", "尿常规", "尿白细胞", "尿路")):
            clinical_focuses.append("泌尿与前列腺健康")
        elif any(marker in text for marker in ("尿酸", "肾功能", "肌酐", "eGFR")):
            clinical_focuses.append("肾功能与尿酸代谢")
        elif any(marker in text for marker in ("血压", "心电图", "心脏")):
            clinical_focuses.append("心血管健康")
        elif any(marker in text for marker in ("视力", "眼底", "屈光")):
            clinical_focuses.append("眼部健康")

    gut_markers = ("肠", "胃", "微生态", "屏障")
    systemic_candidates = [
        focus
        for focus in [*clinical_focuses, *dimension_focuses]
        if not any(marker in focus for marker in gut_markers)
    ]
    gut_candidates = [
        focus
        for focus in [*dimension_focuses, *clinical_focuses]
        if any(marker in focus for marker in gut_markers)
    ]
    focuses = list(
        dict.fromkeys(
            [
                *(systemic_candidates[:1]),
                *(gut_candidates[:1]),
                *systemic_candidates[1:],
                *gut_candidates[1:],
            ]
        )
    )[:3]
    has_gut_context = bool(microbes.items or diseases.items) or any(
        marker in focus for focus in focuses for marker in ("肠", "胃", "微生态", "屏障")
    )
    if not focuses and not has_gut_context:
        del enterotype
        return None

    focus_text = "、".join(focuses) if focuses else "胃肠道与肠道微生态健康"
    parts = [f"综合现有健康资料，当前建议优先围绕{focus_text}开展健康管理。"]
    if has_gut_context and clinical_focuses:
        parts.append(
            "肠道微生态、胃肠功能与已有临床检查结果可共同用于安排健康管理重点。"
        )
    elif has_gut_context:
        parts.append(
            "肠道微生态结果可用于安排胃肠功能观察和生活方式管理重点。"
        )
    parts.append(
        "后续以饮食结构、规律活动和作息管理为基础，按医生建议完成必要复查，"
        "并持续观察相关症状及健康趋势。"
    )
    del enterotype
    profile = _sanitize_health_profile("".join(parts))
    return profile or None


def _extractive_health_level_summary(
    *,
    enterotype: str | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
) -> str | None:
    """Explain the attention label with report facts, never inferred disease risk."""
    signals: list[str] = []
    del enterotype
    category_names = {item.category for item in microbes.items}
    if category_names & {"pathogenic_signal", "fungi_pathogenic_signal"}:
        signals.append("病原相关菌异常")
    if "opportunistic_signal" in category_names:
        signals.append("条件致病菌异常")
    if category_names & {"beneficial_abnormal", "other_abnormal"}:
        signals.append("其他菌群组成变化")
    if diseases.items:
        signals.append("原报告疾病或健康关联项目")
    # Detailed examination values belong to the single health portrait.  The
    # attention summary deliberately remains category-level so the same case
    # facts are not repeated in three consecutive cards.
    del hospital_data
    if not signals:
        return None
    return "本次肠道健康关注程度主要基于" + "、".join(signals) + "。"


def _executive_summary_is_complete(
    value: str | None,
    *,
    enterotype: str | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
) -> bool:
    if not _display_length_in_range(
        value,
        minimum=EXECUTIVE_SUMMARY_MIN,
        maximum=EXECUTIVE_SUMMARY_MAX,
    ):
        return False
    assert value is not None
    if any(
        marker in value
        for marker in (
            "原报告",
            "用户上传",
            "PDF",
            "图片",
            "文件名",
            "解析过程",
            "后台处理",
            "未标注参考范围",
            "模型指数",
            "模型分值",
            "评分过程",
            "检出不代表感染",
            "需结合个体情况",
            "需要结合个体情况",
            "需结合临床",
            "需要结合临床",
            "后续持续关注",
            "定植可能",
            "虽无异常",
            "潜在耐药风险",
            "上述结果基于",
            "本摘要",
            "概括性解读",
            "评估情况",
            "反映了当前肠道菌群及健康相关状态",
            "以上为本报告核心内容",
            "供医学参考",
            "本次报告仅描述",
            "所有数值和项目均来自",
            "以上结果基于",
            "可作为了解当前",
            "肠道菌群构成受多种因素影响",
            "单一时间点",
            "基本的维护方向",
            "本次评估中的相对位置",
            "整体而言",
            "基础数据",
            "共记录",
            "正向意义",
            "未涉及其他未列出",
            "以上为本次检测结果的综合解读",
            "以上调护建议",
        )
    ):
        return False
    if re.search(
        r"病理机制(?:的)?认识(?:仍)?(?:在)?(?:不断|持续|日益)?深入",
        value,
    ):
        return False
    if _is_missing_data_filler(value):
        return False
    if re.search(
        r"(?:模型|指数|分值)[^。；;]{0,28}"
        r"(?:患病风险|风险等级|风险值得关注|提示[^。；;]{0,8}风险)",
        value,
    ):
        return False
    if re.search(
        r"(?:模型|指数|分值)[^。；;]{0,36}"
        r"(?:提示需|提示需要|提示应当|提示应该|提示应)",
        value,
    ):
        return False
    if any(
        marker in value
        for marker in (
            "对当前肠道状态的改善",
            "改善当前肠道状态",
            "改善具有积极意义",
        )
    ):
        return False
    if re.search(
        r"(?:模型|指数|分值)[^。；;]{0,36}仅代表[^。；;]{0,20}(?:数值|记录)",
        value,
    ):
        return False
    if enterotype and enterotype not in value:
        return False
    mentioned_microbes = sum(
        1
        for item in microbes.items
        if item.name in value or item.scientific_name in value
    )
    mentioned_diseases = sum(1 for item in diseases.items if item.name in value)
    if mentioned_microbes > 3 or mentioned_diseases > 3:
        return False
    return any(
        marker in value
        for marker in (
            "关注",
            "风险",
            "健康",
            "影响",
            "表现",
            "功能",
            "代谢",
        )
    )


def _executive_summary_matches_completed_sections(
    value: str | None,
    *,
    recommendations: list[RecommendationDraft],
    source_summary: str | None,
) -> bool:
    """Reject a global draft that contradicts or outruns validated sections."""
    if not value:
        return False
    if re.search(r"(?<![A-Za-z])ASD(?![A-Za-z])", value, flags=re.IGNORECASE):
        return False
    source = " ".join((source_summary or "").split())
    if "失调" in source and re.search(
        r"菌群(?:处于|呈现|为|提示)?平衡(?:状态)?|提示菌群平衡",
        value,
    ):
        return False
    return True


def _extractive_executive_summary(
    *,
    health_profile: str | None,
    recommendations: list[Any],
    enterotype: str | None,
    health_level: str | None = None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    hospital_data: list[HospitalDataInput],
) -> str | None:
    """Compose a complete summary from already validated, case-specific text.

    It adds no medical conclusion: names, results and actions all come from the
    source report, hospital input or already validated evidence-backed prose.
    """

    def sentences(value: str | None) -> list[str]:
        if not value:
            return []
        return [
            segment.strip().rstrip("；;")
            for segment in re.split(r"(?<=[。！？!?])\s*|[；;]", value)
            if segment.strip()
        ]

    # healthProfile is retained internally for compatibility, but the public
    # report has one summary only.  Never seed executiveSummary by copying a
    # second portrait with the same facts.
    del health_profile, health_level
    parts: list[str] = []
    category_topics = {
        "diet": "营养与代谢健康",
        "exercise": "心血管与体能健康",
        "sleep": "睡眠与恢复",
        "bowel_habits": "胃肠道与排便表现",
        "general": "综合健康影响",
    }
    attention_topics: list[str] = []
    for item in recommendations:
        topic = patient_facing_text(getattr(item, "topic", None)) or category_topics.get(
            getattr(item, "category", "")
        )
        if not topic:
            continue
        overlapping_index = next(
            (
                index
                for index, existing in enumerate(attention_topics)
                if (
                    patient_topics_overlap(existing, topic)
                    or patient_health_topic_domain(existing)
                    == patient_health_topic_domain(topic)
                )
            ),
            None,
        )
        if overlapping_index is not None:
            if len(patient_topic_key(topic)) > len(
                patient_topic_key(attention_topics[overlapping_index])
            ):
                attention_topics[overlapping_index] = topic
            continue
        attention_topics.append(topic)
        if len(attention_topics) == 3:
            break
    if not attention_topics:
        if microbes.items:
            attention_topics.append("肠道微生态")
        if diseases.items:
            attention_topics.append("相关健康影响")
    if attention_topics:
        parts.append(
            "当前健康管理优先关注"
            + "、".join(attention_topics)
            + "，这些方向与本次已有健康资料反映的变化一致。"
        )
    if enterotype:
        parts.append(f"本次肠型为{enterotype}。")
    if microbes.items:
        parts.append("肠道微生态的结构和相关功能存在值得关注的变化。")

    displayed_relations = []
    for item in microbes.items:
        eligible_risks = [
            risk
            for risk in item.disease_and_risks
            if (
                risk.patient_applicability == "direct"
                and risk.sample_alignment in {"stool", "unclear"}
            )
            or not any(
                marker in risk.description
                for marker in (
                    "口腔样本",
                    "口腔菌群",
                    "牙周",
                    "呼吸道样本",
                    "血液样本",
                    "组织样本",
                    "黏膜样本",
                )
            )
        ]
        if eligible_risks:
            summary_item = item.model_copy(update={"disease_and_risks": eligible_risks})
            displayed_relations.extend(
                description + "。"
                for description in _compact_microbe_risk_descriptions(summary_item)
            )
    displayed_relations = list(dict.fromkeys(displayed_relations))[:1]
    if displayed_relations:
        parts.append(
            "人体研究支持的主要健康影响方向为："
            + "".join(
                description
                if description.endswith(("。", "！", "？", "!", "?"))
                else description + "。"
                for description in displayed_relations
            )
        )

    if diseases.items:
        ordered_diseases = sorted(
            diseases.items,
            key=lambda item: (
                bool(item.medical_interpretation),
                bool(item.evidence_refs),
                bool(item.existing_results),
            ),
            reverse=True,
        )
        supported_names = [
            patient_facing_name(item.display_name or item.name)
            for item in ordered_diseases
            if item.medical_interpretation and item.association_evidence_refs
        ][:2]
        supported_names = [name for name in supported_names if name]
        if supported_names:
            parts.append(
                "疾病关联部分主要涉及"
                + "、".join(supported_names)
                + "等健康方向；这些项目来自菌群检测关联或人群研究，"
                + "不代表个人诊断。"
            )

    if len("".join(parts)) < EXECUTIVE_SUMMARY_MIN:
        parts.append(
            "这些变化可能共同影响胃肠道功能、代谢状态和长期健康风险，"
            "后续关注重点应放在具体身体表现及已有异常的变化趋势上。"
        )
    if len("".join(parts)) < EXECUTIVE_SUMMARY_MIN:
        parts.append(
            "日常可重点观察胃肠道舒适度、排便表现和相关身体功能的变化，"
            "对于持续存在或逐渐加重的异常应优先处理。"
        )
    if len("".join(parts)) < EXECUTIVE_SUMMARY_MIN:
        for disease in diseases.items:
            public_disease_text = _sanitize_disease_narrative(
                disease.medical_interpretation or "",
                disease=disease,
                has_session_context=bool(hospital_data),
            )
            disease_sentences = sentences(public_disease_text)
            if disease_sentences:
                parts.append(disease_sentences[0])
            if disease_sentences:
                break
    if len("".join(parts)) < EXECUTIVE_SUMMARY_MIN:
        for disease in diseases.items:
            public_disease_text = _sanitize_disease_narrative(
                disease.medical_interpretation or "",
                disease=disease,
                has_session_context=bool(hospital_data),
            )
            for sentence in sentences(public_disease_text):
                normalized = sentence
                if normalized[-1:] not in "。！？!?":
                    normalized += "。"
                parts.append(normalized)
                if len("".join(parts)) >= EXECUTIVE_SUMMARY_MIN:
                    break
            if len("".join(parts)) >= EXECUTIVE_SUMMARY_MIN:
                break
    unique_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = " ".join(part.split()).strip()
        if not normalized:
            continue
        if normalized[-1:] not in "。！？!?":
            normalized += "。"
        marker = _normalize_evidence_text(normalized)
        if marker in seen:
            continue
        seen.add(marker)
        unique_parts.append(normalized)

    # Every component above is either an immutable source fact or prose that
    # has already passed its dedicated validator. Re-running the general prose
    # sanitizer here would incorrectly discard an exact hospital statement
    # such as an endoscopy result containing the word "正常".
    candidate = "".join(unique_parts)
    candidate = _bounded_display_text(
        candidate,
        minimum=EXECUTIVE_SUMMARY_MIN,
        maximum=EXECUTIVE_SUMMARY_MAX,
    )
    if not candidate:
        candidate = _safe_extractive_executive_summary(
            attention_topics=attention_topics,
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
        )
        if not candidate:
            return None
    # Each component was already validated independently.  The general model
    # prose validator can reject a source disease name that happens to match a
    # contextual literature outcome, so the extractive fallback only enforces
    # the required report domains and length here.
    if enterotype and enterotype not in candidate:
        candidate = _safe_extractive_executive_summary(
            attention_topics=attention_topics,
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
        )
        if not candidate:
            return None
    if not _executive_summary_is_complete(
        candidate,
        enterotype=enterotype,
        microbes=microbes,
        diseases=diseases,
        hospital_data=hospital_data,
    ):
        candidate = _safe_extractive_executive_summary(
            attention_topics=attention_topics,
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
        )
        if not candidate or not _executive_summary_is_complete(
            candidate,
            enterotype=enterotype,
            microbes=microbes,
            diseases=diseases,
            hospital_data=hospital_data,
        ):
            return None
    return candidate


def _safe_extractive_executive_summary(
    *,
    attention_topics: list[str],
    enterotype: str | None,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
) -> str | None:
    """Build a compact summary when one detailed sentence fails prose gates.

    Detailed disease and organism explanations remain in their own modules.
    This fallback therefore uses only already-published headings and the fixed
    management workflow, avoiding the all-or-nothing failure where one rejected
    evidence sentence erased the entire health summary.
    """

    topics = [topic for topic in attention_topics if topic][:3]
    if not topics:
        if microbes.items:
            topics.append("肠道微生态")
        if diseases.items:
            topics.append("相关健康影响")
    if not topics:
        return None

    parts = [
        "本次健康资料提示，当前管理重点为"
        + "、".join(topics)
        + "。"
    ]
    if enterotype:
        parts.append(f"本次肠型为{enterotype}。")
    if microbes.items:
        parts.append(
            "肠道菌群结构及相关功能存在需要持续管理的变化，"
            "可结合胃肠道舒适度和排便表现观察其后续趋势。"
        )
    supported_diseases = [
        patient_facing_name(item.display_name or item.name)
        for item in diseases.items
        if item.medical_interpretation and item.association_evidence_refs
    ][:2]
    supported_diseases = [name for name in supported_diseases if name]
    if supported_diseases:
        parts.append(
            "循证信息支持重点留意"
            + "、".join(supported_diseases)
            + "等健康方向，并关注相关身体表现的变化。"
        )
    parts.append(
        "后续应优先处理已经明确的异常，围绕饮食结构、规律活动和作息"
        "开展持续管理，按医生建议完成必要复查，并根据症状及指标趋势调整方案。"
    )
    parts.append(
        "如出现持续或逐渐加重的不适，应及时到相应专科进一步评估。"
    )
    return _bounded_display_text(
        "".join(parts),
        minimum=EXECUTIVE_SUMMARY_MIN,
        maximum=EXECUTIVE_SUMMARY_MAX,
    )


def _executive_hospital_fact(
    hospital_data: list[HospitalDataInput],
) -> str | None:
    if not hospital_data:
        return None
    relevance_markers = (
        "肠镜",
        "胃镜",
        "消化",
        "血脂",
        "胆固醇",
        "低密度脂蛋白",
        "血糖",
        "肺结节",
        "肺间质",
        "脂肪肝",
        "胆囊",
        "胆结石",
        "骨密度",
        "颈椎",
        "前列腺",
        "尿常规",
    )
    ordered = sorted(
        hospital_data,
        key=lambda item: (
            any(marker in (item.item_name or "") for marker in relevance_markers),
            bool(item.result_status_name),
            bool(item.exam_result or item.test_result_value),
            item.report_time or item.check_time or "",
        ),
        reverse=True,
    )
    facts: list[str] = []
    seen_names: set[str] = set()
    for item in ordered:
        name = " ".join((item.item_name or "").split())
        normalized_name = _normalize_evidence_text(name)
        if not name or normalized_name in seen_names:
            continue
        is_relevant = any(marker in name for marker in relevance_markers)
        is_abnormal = bool(item.result_status_name)
        if not is_relevant and not is_abnormal:
            continue
        if item.data_type == "EXAM" and item.exam_result:
            result = " ".join(item.exam_result.splitlines()[0].split())
            if 0 < len(result) <= 90:
                facts.append(f"{name}：{result.rstrip('。')}")
                seen_names.add(normalized_name)
        if item.data_type == "INSPECTION" and item.test_result_value:
            status = item.result_status_name or ""
            facts.append(f"{name}{item.test_result_value}{status}")
            seen_names.add(normalized_name)
        if len(facts) >= 4:
            break
    return "现有检查及检验资料显示" + "；".join(facts) + "。" if facts else None


def _bounded_display_text(
    value: str | None,
    *,
    minimum: int,
    maximum: int,
) -> str | None:
    """Keep model-authored complete clauses inside a public display bound."""
    if not value:
        return None
    normalized = " ".join(value.split()).strip()
    if _display_length_in_range(normalized, minimum=minimum, maximum=maximum):
        finished = normalized
        if finished[-1:] not in "。！？!?":
            finished = finished.rstrip("，；,; ") + "。"
        if _display_length_in_range(finished, minimum=minimum, maximum=maximum):
            return finished
    if len("".join(normalized.split())) < minimum:
        return None
    segments = [
        segment.strip()
        for segment in re.split(r"(?<=[。！？!?])\s*", normalized)
        if segment.strip()
    ]
    retained: list[str] = []
    for segment in segments:
        candidate = "".join([*retained, segment])
        if len("".join(candidate.split())) <= maximum:
            retained.append(segment)
            continue
        clauses = [
            clause.strip()
            for clause in re.split(r"(?<=[；;])\s*|(?<=，)\s*", segment)
            if clause.strip()
        ]
        for clause in clauses:
            candidate = "".join([*retained, clause])
            if len("".join(candidate.split())) > maximum:
                break
            retained.append(clause)
        # A long evidence sentence must not discard later short, case-specific
        # sentences such as the enterotype or the action summary.  Continue
        # scanning while there is room; only stop once the public upper bound is
        # reached.
        if len("".join("".join(retained).split())) >= maximum:
            break
    result = "".join(retained).strip()
    if result and result[-1] not in "。！？!?":
        result = result.rstrip("，；,; ") + "。"
    return (
        result
        if _display_length_in_range(result, minimum=minimum, maximum=maximum)
        else None
    )


def _collect_grounded_numbers(*values: Any) -> set[float]:
    serialized = json.dumps(values, ensure_ascii=False, default=str)
    return {
        float(match)
        for match in re.findall(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?", serialized)
    }


def _numbers_are_grounded(
    value: str,
    grounded_numbers: set[float] | None,
) -> bool:
    if grounded_numbers is None:
        return True
    observed = [
        float(match) for match in re.findall(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?", value)
    ]
    for number in observed:
        if not any(
            abs(number - source) <= max(0.005, abs(source) * 0.005)
            for source in grounded_numbers
        ):
            return False
    return True


def _basis_is_patient_facing(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]|\d", value))


def _normalize_enterotype_prose(value: str) -> str:
    replacements = {
        "Bacteroides型": "肠型I",
        "拟杆菌型": "肠型I",
        "Prevotella型": "肠型II",
        "普雷沃氏菌型": "肠型II",
        "Ruminococcus型": "肠型III",
        "瘤胃球菌型": "肠型III",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def _causal_language_is_source_grounded(claim: str, source: str) -> bool:
    if not any(
        term in claim for term in ("导致", "引起", "造成", "证实致病", "病因", "病原体")
    ):
        return True
    lowered = source.casefold()
    return any(
        term in lowered
        for term in (
            "causal",
            "causality",
            "randomized",
            "randomised",
            " causes ",
            " cause ",
            " caused ",
            " due to ",
            "causative agent",
            "etiologic agent",
            "导致",
            "引起",
            "所致",
            "病因",
            "病原体",
        )
    )


def _validated_evidence_interpretation(
    item: EvidenceInterpretationDraft,
    record: EvidenceRecord,
) -> EvidenceInterpretation | None:
    claim = " ".join(item.claim.split())
    if not claim or not _safe_medical_prose(claim):
        return None
    if not _causal_language_is_source_grounded(
        claim,
        f"{record.title} {record.summary or ''}",
    ):
        return None
    source = f"{record.title} {record.summary or ''}".casefold()
    direction = (
        item.direction
        if item.direction
        in {
            "increased",
            "decreased",
            "associated",
            "mixed",
            "not_stated",
        }
        else "not_stated"
    )
    confidence = (
        item.confidence
        if item.confidence
        in {
            "high",
            "moderate",
            "low",
            "unclear",
        }
        else "unclear"
    )
    target_alignment = (
        item.target_alignment
        if item.target_alignment
        in {
            "direct",
            "indirect",
            "not_applicable",
            "unclear",
        }
        else "unclear"
    )
    if target_alignment == "direct" and _restricted_target_context(record, item=item):
        target_alignment = "indirect"
    if set(record.intents) & {
        "microbe_discovery",
        "microbe_clinical_significance",
        "microbe_association",
        "microbe_disease_association",
        "gut_function",
        "gut_barrier",
    } and not _interpretation_names_target(item.claim, record=record):
        target_alignment = "not_applicable"
    if (
        "diagnostic_guideline" in record.intents
        and target_alignment == "direct"
        and not _interpretation_names_target(item.claim, record=record)
    ):
        target_alignment = "not_applicable"
    if (
        "disease_clinical_significance" in record.intents
        and target_alignment == "direct"
        and not _interpretation_names_target(item.claim, record=record)
    ):
        target_alignment = "not_applicable"
    taxon_alignment = (
        item.taxon_alignment
        if item.taxon_alignment
        in {"species", "genus", "strain", "toxin_gene", "unclear"}
        else "unclear"
    )
    if set(record.intents) & {
        "microbe_discovery",
        "microbe_clinical_significance",
        "microbe_association",
        "microbe_disease_association",
        "gut_function",
        "gut_barrier",
    }:
        exact_species = {
            match.casefold()
            for target in record.targets
            for match in re.findall(
                r"\b[A-Z][a-z]+\s+[a-z][a-z-]+\b",
                target,
            )
        }
        if (
            taxon_alignment in {"genus", "unclear"}
            and any(name in source for name in exact_species)
            and any(name in claim.casefold() for name in exact_species)
        ):
            taxon_alignment = "species"
        if any(
            marker in source
            for marker in (
                "toxin gene",
                "toxigenic",
                "toxin-producing",
                "virulence gene",
                "aero gene",
                "cdta",
                "cdtb",
                "cdtc",
                "mycotoxin",
                "毒素基因",
                "毒力基因",
                "产毒",
                "霉菌毒素",
            )
        ):
            taxon_alignment = "toxin_gene"
        elif any(
            marker in source
            for marker in (
                "carbapenem-resistant",
                "carbapenem resistant",
                "hypervirulent",
                "crkp",
                "serotype",
                "serovar",
                "subspecies",
                "subsp.",
                "耐碳青霉烯",
                "高毒力",
                "血清型",
                "亚种",
            )
        ):
            taxon_alignment = "strain"
        if (
            taxon_alignment not in {"strain", "toxin_gene"}
            and (
                _source_explicitly_supports_requested_relation(record)
                or (
                    "microbe_discovery" in record.intents
                    and direction in {"increased", "decreased"}
                    and item.sample_alignment in {"stool", "mucosa"}
                    and any(
                        name in record.title.casefold() and name in claim.casefold()
                        for name in exact_species
                    )
                    and re.search(r"\b(?:patients|participants|subjects)\b", source)
                    and re.search(
                        r"\b(?:stool|faecal|fecal|mucosal)\b[^.;]{0,60}\b(?:samples|specimens|biopsies)\b",
                        source,
                    )
                    and any(
                        marker in source
                        for marker in (
                            ("lower levels", "lower abundance", "decreased abundance", "decreased in")
                            if direction == "decreased"
                            else ("higher levels", "higher abundance", "increased abundance", "increased in")
                        )
                    )
                )
            )
            and not _restricted_target_context(record, item=item)
        ):
            # targetAlignment describes whether the source addresses the
            # requested organism-condition pair. Population/sample fit is
            # carried separately by patientApplicability/sampleAlignment.
            # Discovery has no relation binding yet. An exact species title
            # with literal human gut abundance data still addresses that
            # organism, even when the patient lacks the studied diagnosis.
            # This does not bypass downstream relation/population validation.
            target_alignment = "direct"
    sample_alignment = (
        item.sample_alignment
        if item.sample_alignment
        in {"stool", "mucosa", "blood", "respiratory", "other", "unclear"}
        else "unclear"
    )
    if sample_alignment == "unclear":
        # The model occasionally leaves this field unset even when the EviMed
        # title/abstract explicitly names the specimen. Recover only literal,
        # high-precision gut specimen phrases; this improves recall without
        # reclassifying blood, respiratory or unspecified clinical evidence as
        # applicable to a stool microbiome result.
        if any(
            marker in source
            for marker in (
                "mucosal biopsy",
                "mucosal biopsies",
                "colonic biopsy",
                "colonic biopsies",
                "intestinal biopsy",
                "intestinal biopsies",
                "luminal washing",
                "luminal washings",
                "肠黏膜活检",
                "肠黏膜标本",
                "结肠活检",
                "黏膜活检",
            )
        ):
            sample_alignment = "mucosa"
        elif any(
            marker in source
            for marker in (
                "stool sample",
                "stool samples",
                "stool specimen",
                "stool specimens",
                "stool culture",
                "fecal sample",
                "fecal samples",
                "faecal sample",
                "faecal samples",
                "fecal specimen",
                "faecal specimen",
                "fecal culture",
                "faecal culture",
                "粪便样本",
                "粪便标本",
                "粪便培养",
                "粪便灌洗液",
                "大便样本",
                "大便标本",
            )
        ):
            sample_alignment = "stool"
    if (
        target_alignment != "direct"
        and record.category == "guideline"
        and set(record.intents) & {"health_management", "intervention"}
        and _health_guideline_matches_target(record)
        and not _restricted_target_context(record, item=item)
    ):
        # The record has already passed the exact health-topic and restricted-
        # population screen. Do not let model variability reject a healthy-
        # adult guideline whose title/summary directly names the requested
        # lifestyle topic.
        target_alignment = "direct"
    if (
        target_alignment != "direct"
        and _source_explicitly_addresses_requested_disease(record)
        and not _restricted_target_context(record, item=item)
    ):
        # Alignment is a property of the EviMed title/summary, not of whether
        # this patient already has the disease.  When the source itself names
        # the exact target and discusses its clinical meaning or diagnosis,
        # preserve it for the claim/evaluation retry instead of allowing a
        # variable model label to cut off the rest of the pipeline.
        target_alignment = "direct"
    patient_applicability = (
        item.patient_applicability
        if item.patient_applicability
        in {"direct", "contextual", "not_applicable", "unclear"}
        else "unclear"
    )
    if (
        taxon_alignment in {"strain", "toxin_gene"}
        and patient_applicability == "direct"
    ):
        patient_applicability = "contextual"
    if (
        sample_alignment in {"mucosa", "blood", "respiratory"}
        and patient_applicability == "direct"
    ):
        patient_applicability = "contextual"
    if (
        patient_applicability == "not_applicable"
        and target_alignment == "direct"
        and set(record.intents)
        & {
            "microbe_discovery",
            "microbe_clinical_significance",
            "microbe_association",
            "microbe_disease_association",
            "disease_clinical_significance",
            "diagnostic_guideline",
            "health_management",
            "gut_function",
            "gut_barrier",
            "intervention",
        }
    ):
        # For an exact, directly aligned human target, absence of a patient
        # diagnosis changes the claim from individualized to contextual; it
        # does not make the evidence irrelevant. Restricted populations and
        # comorbid targets have already been downgraded above.
        patient_applicability = "contextual"
    clinical_role = " ".join(item.clinical_role.split()) if item.clinical_role else None
    if clinical_role and (
        not _safe_medical_prose(clinical_role)
        or not _interpretation_names_target(clinical_role, record=record)
    ):
        clinical_role = None
    detected_design = _study_design(record)
    effect = " ".join(item.effect.split()) if item.effect else None
    if (
        effect
        and not any(
            token in source for token in re.findall(r"\d+(?:\.\d+)?", effect)
        )
        and re.search(r"\d", effect)
    ):
        # Do not allow the model to invent an effect size absent from EviMed's
        # title/summary.  Qualitative effects may remain.
        effect = None
    return EvidenceInterpretation(
        claim=claim,
        population=_validated_evidence_population(item.population),
        study_design=detected_design,
        direction=direction,
        effect=effect,
        applicability=(
            " ".join(item.applicability.split()) if item.applicability else None
        ),
        limitations=" ".join(item.limitations.split()) if item.limitations else None,
        confidence=confidence,
        target_alignment=target_alignment,
        recommended_evaluations=(
            _validated_evidence_evaluations(item, record)
            if target_alignment == "direct"
            else []
        ),
        clinical_role=clinical_role,
        conditions=_validated_evidence_conditions(item, record),
        taxon_alignment=taxon_alignment,
        sample_alignment=sample_alignment,
        patient_applicability=patient_applicability,
    )


def _health_guideline_matches_target(record: EvidenceRecord) -> bool:
    target = " ".join(record.targets).casefold()
    source = f"{record.title} {record.summary or ''}".casefold()
    topics = (
        (("膳食", "饮食", "营养"), ("膳食", "饮食", "营养", "diet", "nutrition")),
        (
            ("运动", "身体活动"),
            ("运动", "身体活动", "体力活动", "physical activity", "exercise"),
        ),
        (("睡眠",), ("睡眠", "sleep")),
        (("排便",), ("排便", "大便", "bowel habit", "bowel movement")),
    )
    topic_matches = any(
        any(marker in target for marker in target_markers)
        and any(marker in source for marker in source_markers)
        for target_markers, source_markers in topics
    )
    if not topic_matches:
        return False
    condition_scopes = (
        (
            ("血脂异常",),
            (
                "血脂异常",
                "高胆固醇",
                "低密度脂蛋白",
                "dyslipidemia",
                "hypercholesterolemia",
                "cholesterol",
                "ldl-c",
            ),
        ),
        (
            ("血糖异常",),
            ("血糖异常", "高血糖", "hyperglycemia", "prediabetes"),
        ),
        (
            ("血压异常",),
            ("血压异常", "高血压", "hypertension", "blood pressure"),
        ),
        (
            ("体重与体成分异常",),
            ("超重", "肥胖", "overweight", "obesity", "body mass index"),
        ),
        (
            ("便秘相关",),
            ("便秘", "constipation"),
        ),
    )
    return all(
        not any(marker in target for marker in target_markers)
        or any(marker in source for marker in source_markers)
        for target_markers, source_markers in condition_scopes
    )


def _source_explicitly_addresses_requested_disease(record: EvidenceRecord) -> bool:
    intents = set(record.intents)
    relevant_intents = intents & {
        "disease_clinical_significance",
        "diagnostic_guideline",
    }
    if not relevant_intents:
        return False
    source = f"{record.title} {record.summary or ''}".casefold()
    normalized_source = _normalize_evidence_text(source)
    names_target = any(
        (alias_key := _normalize_evidence_text(alias))
        and alias_key in normalized_source
        for raw_target in record.targets
        for raw_component in raw_target.split("/")
        for alias in _target_aliases(raw_component)
    )
    if not names_target:
        return False
    clinical_markers = (
        "clinical feature",
        "symptom",
        "manifest",
        "definition",
        "pathophysiolog",
        "presents with",
        "quality of life",
        "characterized by",
        "natural history",
        "complication",
        "risk factor",
        "progressive",
        "fibrosis",
        "cirrhosis",
        "asymptomatic",
        "neurodegenerative",
        "metabolic disease",
        "inflammatory disease",
        "临床表现",
        "症状",
        "定义",
        "病理生理",
        "生活质量",
        "排便困难",
        "排便次数",
        "为特征",
        "以……为特征",
        "自然病程",
        "并发症",
        "危险因素",
        "进行性",
        "无症状",
        "神经退行",
        "纤维化",
        "肝硬化",
        "代谢性疾病",
        "炎症性疾病",
    )
    diagnostic_markers = (
        "diagnos",
        "screen",
        "test",
        "assessment",
        "evaluation",
        "colonoscopy",
        "manometry",
        "stool form scale",
        "诊断",
        "筛查",
        "检查",
        "检测",
        "评估",
        "测压",
        "内镜",
        "量表",
    )
    human_gut_association = (
        any(
            marker in source
            for marker in (
                "human",
                "patient",
                "participant",
                "cohort",
                "case-control",
                "cross-sectional",
                "systematic review",
                "meta-analysis",
                "患者",
                "人群",
                "队列",
                "系统综述",
                "荟萃分析",
            )
        )
        and any(
            marker in source
            for marker in (
                "gut microbiome",
                "gut microbiota",
                "intestinal microbiome",
                "intestinal microbiota",
                "肠道菌群",
                "肠道微生物组",
                "肠道微生态",
            )
        )
        and any(
            marker in source
            for marker in (
                "associated",
                "association",
                "correlated",
                "dysbiosis",
                "difference",
                "altered",
                "change",
                "相关",
                "关联",
                "失调",
                "差异",
                "改变",
            )
        )
    )
    return (
        "disease_clinical_significance" in relevant_intents
        and (
            any(marker in source for marker in clinical_markers)
            or human_gut_association
        )
    ) or (
        "diagnostic_guideline" in relevant_intents
        and any(marker in source for marker in diagnostic_markers)
    )


def _validated_evidence_conditions(
    item: EvidenceInterpretationDraft,
    record: EvidenceRecord,
) -> list[EvidenceCondition]:
    source = _normalize_evidence_text(f"{record.title} {record.summary or ''}")
    results: list[EvidenceCondition] = []
    seen: set[str] = set()
    allowed_types = {
        "established_pathogen",
        "opportunistic",
        "epidemiologic_association",
        "colonization",
        "environmental_or_dietary",
        "not_applicable",
    }
    for candidate in item.conditions:
        name = " ".join(candidate.name.split())
        search_term = " ".join(candidate.search_term.split())
        source_phrase = " ".join(candidate.source_phrase.split())
        normalized_phrase = _normalize_evidence_text(source_phrase)
        marker = _normalize_evidence_text(name)
        if (
            not name
            or not search_term
            or not source_phrase
            or not normalized_phrase
            or normalized_phrase not in source
            or candidate.relation_type not in allowed_types
            or not re.search(r"[\u3400-\u9fff]", name)
            or _condition_is_generic_or_interventional(
                name=name,
                search_term=search_term,
                source=f"{record.title} {record.summary or ''}",
                relation_type=candidate.relation_type,
                targets=record.targets,
            )
            or marker in seen
            or not _safe_medical_prose(name)
        ):
            continue
        seen.add(marker)
        results.append(
            EvidenceCondition(
                name=name,
                search_term=search_term,
                source_phrase=source_phrase,
                relation_type=candidate.relation_type,
            )
        )
    return results[:3]


def _condition_is_generic_or_interventional(
    *,
    name: str,
    search_term: str,
    source: str,
    relation_type: str,
    targets: list[str],
) -> bool:
    normalized_name = _normalize_evidence_text(name)
    normalized_term = _normalize_evidence_text(search_term)
    if normalized_name in {
        "感染",
        "感染风险",
        "交叉感染风险",
        "感染性疾病",
        "持续性感染",
        "厌氧菌感染",
        "疾病",
        "临床情况",
        "严重临床情况",
        "病原体",
        "主要致病菌",
        "致病性",
        "临床致病性",
        "人类病原体",
        "潜在人类病原体",
        "新兴人类病原体",
        "携带者",
        "水源性病原体",
        "免疫功能低下",
        "免疫抑制",
        "易感人群",
        "免疫功能低下人群",
        "免疫抑制人群",
        "选择性iga缺乏",
        "选择性iga缺乏症",
        "iga缺乏",
        "免疫缺陷",
        "免疫缺陷人群",
        "抗生素耐药性",
        "抗生素耐药",
        "抗生素耐药病原体",
        "耐药",
        "cre感染",
        "抗菌药物耐药性",
        "多重耐药性",
        "更差的预后",
        "不良预后",
        "死亡",
        "死亡风险",
        "交叉感染",
        "医院感染",
        "医疗相关感染",
        "环境来源",
        "真菌感染",
        "人类感染",
        "急性感染",
        "感染暴发",
        "多系统感染",
        "鲍曼不动杆菌感染",
        "人类真菌病原体",
        "真菌病原体",
        "实体瘤",
        "发病率",
        "死亡率",
        "长期死亡率",
        "发病率和死亡率",
        "发病率与死亡率",
        "抗菌素耐药革兰氏阴性菌感染",
        "抗菌药物耐药革兰阴性菌感染",
        "胃肠道疾病",
        "严重真菌病",
        "罕见侵袭性酵母菌感染",
        "免疫受损",
        "性传播感染",
        "性传播疾病",
        "immunocompromised",
        "immunosuppression",
        "selectiveigadeficiency",
        "immunodeficiency",
        "susceptiblepopulation",
    }:
        return True
    if normalized_name.endswith(("致病性", "发病率", "死亡率")):
        return True
    target_components = {
        _normalize_evidence_text(component)
        for target in targets
        # Microbe targets are scientific name / display name / outcome.
        # The outcome is exactly what a relation query asks us to extract;
        # only the organism identities themselves are not disease outcomes.
        for component in target.split("/")[:2]
        if component.strip()
    }
    if normalized_name in target_components or normalized_term in target_components:
        return True
    # A condition phrase naming another organism is not an outcome of the
    # requested taxon.  This blocks co-mention papers such as a title listing
    # C. difficile and B. fragilis from becoming a causal cross-link.
    foreign_taxa = {
        _normalize_evidence_text(match)
        for match in re.findall(
            r"(?<![A-Za-z])[A-Z][a-z]{2,}\s+[a-z][a-z-]{2,}(?![A-Za-z])",
            f"{name} {search_term}",
        )
    }
    if foreign_taxa and not foreign_taxa <= target_components:
        return True
    source_lowered = source.casefold()
    if any(
        marker in source_lowered
        for marker in (
            "probiotic supplementation",
            "probiotic treatment",
            "treatment failure",
            "therapy failure",
            "transplant failure",
            "fecal microbial transplant failure",
            "faecal microbial transplant failure",
            "co-infection",
            "coinfection",
            "supplementation with",
            "probiotic product",
            "作为益生菌",
            "益生菌补充",
            "活菌制剂",
            "活菌片",
            "移植失败",
            "合并感染",
            "合并",
            "用于调节",
            "用于改善",
        )
    ):
        return True
    # A guideline often mentions diagnosis or treatment while still defining
    # an exact disease. Reject an interventional candidate, not every disease
    # candidate from such a source.
    lowered = f"{name} {search_term}".casefold()
    return any(
        marker in lowered
        for marker in (
            "probiotic supplementation",
            "probiotic treatment",
            "treatment failure",
            "therapy failure",
            "transplant failure",
            "fecal microbial transplant failure",
            "faecal microbial transplant failure",
            "treatment of",
            "therapy for",
            "management of",
            "co-infection",
            "coinfection",
            "administered",
            "supplementation with",
            "probiotic product",
            "therapeutic use",
            "作为益生菌",
            "益生菌补充",
            "补充治疗",
            "治疗失败",
            "移植失败",
            "治疗",
            "诊治",
            "管理",
            "合并",
            "灌胃",
            "活菌片",
            "活菌制剂",
            "组成成分",
            "用于调节",
            "用于改善",
        )
    )


def _restricted_target_context(
    record: EvidenceRecord,
    *,
    item: EvidenceInterpretationDraft,
) -> bool:
    """Deterministic backstop for model-overgeneralized applicability."""
    title = " ".join(
        part
        for part in (
            record.title,
            item.claim,
            item.population,
            item.applicability,
        )
        if part
    ).casefold()
    normalized_targets = {
        _normalize_evidence_text(alias)
        for raw_target in record.targets
        for raw_component in raw_target.split("/")
        for alias in _target_aliases(raw_component)
    }
    if set(record.intents) & {
        "diagnostic_guideline",
        "disease_clinical_significance",
    } and any(
        marker in title
        and not any(
            (marker_key := _normalize_evidence_text(marker))
            and (marker_key in target_key or target_key in marker_key)
            for target_key in normalized_targets
        )
        for marker in (
            "epilepsy",
            "癫痫",
            "transplant",
            "移植",
            "clinical trials",
            "clinical trial",
            "临床试验",
            "perianal cancer",
            "肛周癌",
            "acute coronary syndrome",
            "急性冠脉综合征",
            "menopausal",
            "postmenopausal",
            "menopause",
            "绝经",
            "cancer patient",
            "cancer patients",
            "oncology patient",
            "癌症患者",
            "肿瘤患者",
            "stricture",
            "stenosis",
            "狭窄",
            "older persons",
            "older adults",
            "elderly",
            "老年",
            "stroke-related",
            "post-stroke",
            "stroke patients",
            "卒中相关",
            "卒中后",
            "stroke",
            "脑卒中",
            "polycystic ovary",
            "polycystic ovarian",
            "多囊卵巢",
            "sleep-related breathing",
            "sleep disordered breathing",
            "sleep apnea",
            "睡眠呼吸",
            "呼吸障碍",
            "parkinson",
            "帕金森",
            "hemodialysis",
            "haemodialysis",
            "dialysis",
            "血液透析",
            "透析患者",
            "hepatitis c",
            "hcv infection",
            "丙型肝炎",
            "丙肝",
            "non-alcoholic fatty liver",
            "nonalcoholic fatty liver",
            "fatty liver disease",
            "hepatic steatosis",
            "mri-pdff",
            "nafld",
            "masld",
            "脂肪肝",
            "肝脂肪",
            "eyelid myoclonia",
            "眼睑肌阵挛",
            "juvenile myoclonic",
            "青少年肌阵挛",
        )
    ):
        return True
    return "health_management" in record.intents and any(
        marker in title
        for marker in (
            "obesity",
            "obese",
            "肥胖",
            "pregnan",
            "妊娠",
            "pediatric",
            "children",
            "儿童",
            "covid",
            "coronavirus",
            "新冠",
            "冠状病毒",
            "疫情期间",
        )
    )


def _interpretation_names_target(
    claim: str,
    *,
    record: EvidenceRecord,
) -> bool:
    if not record.targets:
        return True
    normalized_claim = _normalize_evidence_text(claim)
    for raw_target in record.targets:
        for raw_component in raw_target.split("/"):
            accepted = _target_aliases(raw_component)
            if any(
                _normalize_evidence_text(alias) in normalized_claim
                for alias in accepted
                if alias
            ):
                return True
    return False


def _source_explicitly_supports_requested_relation(record: EvidenceRecord) -> bool:
    if not record.relation_bindings:
        return False
    source = f"{record.title} {record.summary or ''}".casefold()
    if not any(
        marker in source
        for marker in (
            "causative agent",
            "etiologic agent",
            "etiological agent",
            "caused by",
            "causes ",
            "pathogen",
            "associated with",
            "association with",
            "infection",
            "病原体",
            "致病菌",
            "引起",
            "导致",
            "相关",
            "关联",
            "感染",
        )
    ):
        return False
    normalized_source = _normalize_evidence_text(source)
    for binding in record.relation_bindings:
        taxon_aliases = (
            binding.microbe_name,
            *binding.microbe_aliases,
        )
        disease_aliases = _target_aliases(binding.disease_name)
        if any(
            (alias_key := _normalize_evidence_text(alias))
            and alias_key in normalized_source
            for alias in taxon_aliases
        ) and any(
            (alias_key := _normalize_evidence_text(alias))
            and alias_key in normalized_source
            for alias in disease_aliases
        ):
            return True
    return False


def _target_aliases(value: str) -> tuple[str, ...]:
    target = value.strip().casefold()
    # Source reports often use display variants such as ``帕金森``、
    # ``肠易激综合症`` or umbrella labels such as ``肾脏疾病``.  Reuse the
    # canonical retrieval aliases here so alignment validation does not call
    # an exact English title off-target merely because this smaller narrative
    # alias table lacks the display spelling.
    from modules.report.evidence import _disease_terms

    return tuple(
        dict.fromkeys(
            (
                target,
                *_TARGET_ALIASES.get(target, ()),
                *(alias.casefold() for alias in _disease_terms(value)),
            )
        )
    )


def _validated_evidence_population(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    if any(
        marker in normalized
        for marker in ("未明确", "未说明", "通常", "推测", "可能为", "一般认为")
    ):
        return None
    return normalized


def _validated_evidence_evaluations(
    item: EvidenceInterpretationDraft,
    record: EvidenceRecord,
) -> list[EvidenceRecommendedEvaluation]:
    intents = set(record.intents)
    diagnostic_source = "diagnostic_guideline" in intents and record.category in {
        "guideline",
        "literature",
        "clinical_trial",
    }
    microbe_guideline = record.category == "guideline" and bool(
        intents
        & {
            "microbe_clinical_significance",
            "microbe_disease_association",
        }
    )
    if not (diagnostic_source or microbe_guideline):
        return []
    plain_source = f"{record.title} {record.summary or ''}".casefold()
    if any(
        marker in plain_source
        for marker in (
            "recommend against",
            "recommends against",
            "not recommend",
            "do not recommend",
            "should not be screened",
            "不建议",
            "不推荐",
            "反对筛查",
        )
    ):
        return []
    source = _normalize_evidence_text(f"{record.title} {record.summary or ''}")
    results = []
    seen = set()
    for evaluation in item.recommended_evaluations:
        name = " ".join(evaluation.name.split())
        phrase = " ".join(evaluation.source_phrase.split())
        rationale = " ".join(evaluation.rationale.split())
        if any(
            marker in rationale.casefold()
            for marker in (
                "未作为目标直接",
                "未作为直接",
                "不作为",
                "并非直接",
                "not a direct",
                "not directly",
            )
        ):
            continue
        if not phrase or _normalize_evidence_text(phrase) not in source:
            continue
        if _restricted_evaluation_context(phrase, record=record):
            continue
        if not _source_phrase_supports_evaluation(phrase, name=name):
            continue
        if (
            "post-load plasma glucose" in phrase.casefold()
            or "1-hour plasma glucose" in phrase.casefold()
        ):
            name = "75克口服葡萄糖耐量试验1小时血浆葡萄糖"
        if evaluation.purpose not in {
            "confirm_target",
            "screen_target",
            "characterize_target",
        }:
            continue
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        results.append(
            EvidenceRecommendedEvaluation(
                name=name,
                rationale=rationale,
                source_phrase=phrase,
                purpose=evaluation.purpose,
            )
        )
    return results[:8]


def _source_phrase_supports_evaluation(value: str, *, name: str) -> bool:
    normalized = value.casefold()
    normalized_phrase = _normalize_evidence_text(value)
    normalized_name = _normalize_evidence_text(name)
    if normalized_name and (
        normalized_name in normalized_phrase or normalized_phrase in normalized_name
    ):
        return True
    # Generic words such as "test" or "evaluation" cannot support an
    # arbitrary model-invented examination. Cross-language names are accepted
    # only within a known clinical examination family.
    families = (
        (("ogtt", "口服葡萄糖耐量", "糖耐量", "餐后血糖"), ("ogtt", "oral glucose tolerance", "post-load plasma glucose", "75-g")),
        (("空腹血糖", "fpg"), ("fasting plasma glucose", "fasting blood glucose", "fpg")),
        (("hba1c", "糖化血红蛋白"), ("hba1c", "glycated haemoglobin", "glycated hemoglobin")),
        (("结肠镜", "肠镜"), ("colonoscopy", "colonic endoscopy")),
        (("胃镜", "上消化道内镜"), ("gastroscopy", "upper gastrointestinal endoscopy")),
        (("磁共振", "mri", "mrcp"), ("magnetic resonance", "mri", "mrcp")),
        (("ct", "计算机断层"), ("computed tomography", "ct")),
        (("活检", "病理"), ("biopsy", "histopathology", "pathology")),
        (("测压",), ("manometry",)),
        (("脑电图", "eeg"), ("electroencephal", "eeg")),
        (("钙卫蛋白",), ("calprotectin",)),
        (("粪便隐血", "fit"), ("fecal immunochemical", "faecal immunochemical", "occult blood", "fit")),
        (("肝硬度", "瞬时弹性"), ("liver stiffness", "transient elastography")),
    )
    return any(
        any(marker in normalized_name for marker in name_markers)
        and any(marker in normalized for marker in phrase_markers)
        for name_markers, phrase_markers in families
    )


def _claim_names_condition(value: str, condition_name: str) -> bool:
    normalized = _normalize_evidence_text(value)
    return any(
        (marker := _normalize_evidence_text(alias)) and marker in normalized
        for alias in _target_aliases(condition_name)
    )


def _restricted_evaluation_context(value: str, *, record: EvidenceRecord) -> bool:
    normalized = value.casefold()
    target = " ".join(record.targets).casefold()
    restricted = (
        ("sleep-related breathing", ("sleep-related breathing", "睡眠呼吸")),
        ("sleep disordered breathing", ("sleep disordered breathing", "睡眠呼吸")),
        ("obstructive sleep apnea", ("sleep apnea", "睡眠呼吸暂停")),
        ("睡眠相关呼吸", ("睡眠呼吸",)),
        (
            "mri-pdff",
            (
                "non-alcoholic fatty liver",
                "nonalcoholic fatty liver",
                "nafld",
                "masld",
                "脂肪肝",
            ),
        ),
        ("肝脂肪含量", ("脂肪肝", "nafld", "masld")),
    )
    return any(
        marker in normalized and not any(alias in target for alias in allowed_targets)
        for marker, allowed_targets in restricted
    )


def _normalize_evidence_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _study_design(record: EvidenceRecord) -> str | None:
    if record.category == "guideline":
        return "临床指南"
    text = f"{record.title} {record.summary or ''} {record.source}".casefold()
    for markers, label in (
        (("meta-analysis", "meta analysis"), "Meta分析"),
        (("systematic review",), "系统综述"),
        (("randomized", "randomised", "randomized controlled"), "随机对照试验"),
        (("prospective cohort",), "前瞻性队列研究"),
        (("cohort",), "队列研究"),
        (("case-control", "case control"), "病例对照研究"),
        (("cross-sectional", "cross sectional"), "横断面研究"),
        (("clinical trial",), "临床试验"),
        (
            (
                "case report",
                "case reports",
                "case-report",
                "病例报告",
                "一例",
                "1例",
                "个案",
            ),
            "病例报告",
        ),
    ):
        if any(marker in text for marker in markers):
            return label
    return None


def _safe_medical_prose(value: str) -> bool:
    normalized = value.casefold()
    return not any(term.casefold() in normalized for term in _INTERNAL_TERMS)


_ATTENTION_CATEGORY_DIMENSIONS = {
    "diet": {
        "metabolic_system",
        "digestive_system",
    },
    "exercise": {
        "metabolic_system",
        "cardiovascular_system",
    },
    "sleep": {"mental_system", "nervous_system"},
    "bowel_habits": {"digestive_system"},
    "general": set(),
}


def _validated_attention_topic(
    value: str | None,
    *,
    category: str,
    health_assessment: HealthAssessment | None,
) -> str | None:
    topic = " ".join((value or "").split()).strip("：:，,。 ")
    forbidden = (
        "原报告",
        "用户上传",
        "PDF",
        "图片",
        "模型",
        "指标值",
        "评分过程",
    )
    if topic and 2 <= len(topic) <= 40 and not any(
        marker in topic for marker in forbidden
    ):
        return topic
    dimensions = [
        item
        for item in (health_assessment.dimensions if health_assessment else [])
        if item.score_available and item.level in {"attention", "priority_attention"}
    ]
    allowed_codes = _ATTENTION_CATEGORY_DIMENSIONS.get(category, set())
    matched = [item.name for item in dimensions if item.code in allowed_codes]
    if not matched and category == "general":
        matched = [item.name for item in dimensions]
    if matched:
        return "与".join(matched[:2]) + "健康"
    return {
        "diet": "营养与代谢健康",
        "exercise": "心血管与体能健康",
        "sleep": "睡眠与恢复",
        "bowel_habits": "胃肠道与排便表现",
        "general": "综合健康影响",
    }.get(category)


def _validated_attention_priority(
    value: str | None,
    *,
    topic: str | None,
    category: str,
    health_assessment: HealthAssessment | None,
) -> str:
    normalized = "".join((value or "").split())
    if normalized in {"重点关注", "建议关注"}:
        return normalized
    topic_text = topic or ""
    allowed_codes = _ATTENTION_CATEGORY_DIMENSIONS.get(category, set())
    for item in health_assessment.dimensions if health_assessment else []:
        if not item.score_available or item.level != "priority_attention":
            continue
        if item.code in allowed_codes or item.name in topic_text or category == "general":
            return "重点关注"
    return "建议关注"


def _attention_has_assessment_support(
    topic: str,
    *,
    category: str,
    health_assessment: HealthAssessment | None,
) -> bool:
    if health_assessment is None:
        return False
    allowed_codes = _ATTENTION_CATEGORY_DIMENSIONS.get(category, set())
    return any(
        item.score_available
        and item.level in {"attention", "priority_attention"}
        and (
            item.code in allowed_codes
            or item.name in topic
            or category == "general"
        )
        for item in health_assessment.dimensions
    )


def _attention_direction_is_valid(value: str) -> bool:
    if not value or len(value) > 300:
        return False
    forbidden = (
        "原报告",
        "用户上传",
        "PDF",
        "图片",
        "文件名",
        "模型",
        "评分过程",
        "建议用药",
        "自行用药",
        "自行停药",
        "药物方案",
        "用药方案",
        "手术方案",
        "疗效承诺",
        "益生菌",
        "益生元",
        "补充剂",
        "品牌",
        "产品",
    )
    if any(marker in value for marker in forbidden):
        return False
    if any(
        marker in value
        for marker in (
            "以支持整体健康",
            "有助于整体健康",
            "促进身体活动和健康",
            "保证合理营养",
            "有助于维持健康",
            "支持整体健康",
        )
    ):
        return False
    if re.match(r"^\s*(?:的(?:建议|指导原则)|建议)\s*[，,]", value):
        return False
    # The health summary is the action layer. Outcome claims belong in the
    # preceding risk/indicator sections and often become unsupported when a
    # model joins an action source with a separate outcome source.
    if re.search(
        r"(?:有助于|能够|从而|以便|用于)?"
        r"(?:改善|增强|降低|减少|预防)"
        r"[^。；;]{0,36}(?:健康|风险|功能|水平|状态|免疫|胆固醇|血脂|血糖|血压)?",
        value,
    ):
        return False
    if re.search(r"对[^。；;]{0,30}管理具有[^。；;]{0,20}作用", value):
        return False
    if re.search(
        r"\d+(?:\.\d+)?\s*(?:mmol/L|mg/dL|μmol/L|kg/m²|%|次|分钟|小时)",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    if "主要风险：" in value or re.search(
        r"(?:可能|可|易)[^。；;]{0,30}(?:增加|升高|导致)"
        r"[^。；;]{0,24}(?:疾病|健康)?风险",
        value,
    ):
        return False
    has_direction = any(
        marker in value
        for marker in (
            "调养方向",
            "管理方向",
            "检查方向",
            "医学处理方向",
            "饮食结构",
            "体重管理",
            "活动习惯",
            "运动",
            "有氧",
            "抗阻",
            "睡眠",
            "作息",
            "排便记录",
            "症状记录",
            "记录排便",
            "补充检查",
            "复查",
            "医学评估",
            "临床评估",
            "规范治疗",
            "进一步检查或治疗",
        )
    )
    return has_direction


def _attention_action_domains(value: str) -> set[str]:
    normalized = value.casefold()
    markers = {
        "diet": ("饮食", "膳食", "蔬菜", "食物", "diet", "nutrition"),
        "exercise": ("运动", "活动", "有氧", "抗阻", "exercise", "activity"),
        "sleep": ("睡眠", "作息", "就寝", "起床", "sleep", "bedtime", "wake-up"),
        "bowel_habits": ("排便", "腹痛", "腹胀", "便秘", "腹泻", "bowel", "stool", "constipation"),
        "clinical": ("复查", "就医", "专科", "检查", "评估", "follow-up", "screen", "test"),
    }
    return {
        domain
        for domain, values in markers.items()
        if any(marker in normalized for marker in values)
    }


def _attention_evidence_supports_action(
    *,
    content: str,
    evidence_text: str,
    category: str,
) -> bool:
    if not evidence_text:
        return False
    content_domains = _attention_action_domains(content)
    evidence_domains = _attention_action_domains(evidence_text)
    category_domain = category if category in {
        "diet",
        "exercise",
        "sleep",
        "bowel_habits",
    } else None
    if category_domain and category_domain not in evidence_domains:
        return False
    if not any(
        marker in evidence_text.casefold()
        for marker in (
            "建议",
            "推荐",
            "保持",
            "结合",
            "进行",
            "规律",
            "recommend",
            "guideline",
            "supports",
        )
    ):
        return False
    if not content_domains or not content_domains.issubset(evidence_domains):
        return False
    content_details = _attention_action_details(content)
    evidence_details = _attention_action_details(evidence_text)
    return not content_details or content_details.issubset(evidence_details)


def _attention_action_details(value: str) -> set[str]:
    """Return specific patient actions that one source must support together."""

    normalized = value.casefold()
    marker_groups = {
        "diet_variety": ("食物多样", "饮食多样", "diverse food", "food variety"),
        "diet_balance": ("合理搭配", "均衡饮食", "平衡膳食", "balanced diet"),
        "exercise_aerobic": ("有氧", "aerobic"),
        "exercise_resistance": ("抗阻", "力量训练", "力量练习", "resistance", "strength"),
        "exercise_gradual": ("逐步增加活动量", "循序渐进", "gradually increase"),
        "exercise_regular": ("规律身体活动", "规律运动", "regular physical activity"),
        "sleep_bedtime": ("就寝时间", "固定就寝", "bedtime", "bedtimes"),
        "sleep_wake": ("起床时间", "固定起床", "wake-up", "wake up"),
        "sleep_device": ("电子设备", "手机", "internet use before bed", "screen before bed"),
        "bowel_timed": ("固定时间排便", "regular toileting time", "scheduled toileting"),
        "bowel_urge": ("有便意时及时如厕", "respond to the urge"),
        "bowel_no_holding": ("避免憋便", "避免忍便", "do not delay defecation"),
    }
    return {
        name
        for name, markers in marker_groups.items()
        if any(marker in normalized for marker in markers)
    }


def _attention_examinations_are_allowed(
    value: str,
    *,
    candidates: dict[str, dict[str, Any]] | None,
) -> bool:
    """Reject invented concrete tests while allowing general care directions."""

    if _obviously_not_hospital_examination(value):
        return False

    names = {
        str(item.get("name") or "").strip()
        for item in (candidates or {}).values()
        if str(item.get("name") or "").strip()
    }
    explicit_test = any(
        marker in value
        for marker in (
            "测定",
            "试验",
            "量表",
            "问卷",
            "内镜",
            "肠镜",
            "胃镜",
            "超声",
            "心电图",
            "磁共振",
            "MRI",
            "CT检查",
        )
    ) or bool(
        re.search(
            r"(?:血糖|血脂|肝功能|肾功能|尿酸|甲状腺|粪便|尿液|"
            r"影像)[^。；;]{0,12}(?:检查|检测|复查)",
            value,
        )
    )
    if not explicit_test:
        return True
    return any(name in value for name in names)


def _strip_evidence_title_prefix(value: str) -> str:
    value = re.sub(
        r"^(?:根据|依据)(?:《[^》]{1,100}》(?:和|、|及)?)+[，,:：]?\s*",
        "",
        value,
    )
    value = re.sub(
        r"^(?:参照|依据|根据)[^，。]{1,100}?(?:指南|共识)[，,]\s*",
        "",
        value,
    )
    return value.strip()


def _remove_model_health_claims(value: str) -> str:
    """Disease-model scores describe associations, not overall health level."""
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return ""
    model_markers = ("模型", "分值", "评分", "模型指数")
    if not any(marker in normalized for marker in model_markers):
        return normalized
    output: list[str] = []
    for sentence in re.findall(r"[^。！？!?]+[。！？!?]?", normalized):
        sentence = sentence.strip()
        if not sentence:
            continue
        if not any(marker in sentence for marker in model_markers):
            output.append(sentence)
            continue
        if "的但" in sentence:
            continue
        retained: list[str] = []
        for raw_clause in re.split(r"[，,；;。]+", sentence):
            clause = raw_clause.strip()
            if "但" in clause and any(
                marker in clause.split("但", 1)[0]
                for marker in ("尚可", "良好", "正常", "一般")
            ):
                clause = clause.split("但", 1)[1].strip()
            clause = re.sub(r"^(?:且|同时|并且|此外|其中)", "", clause).strip()
            if (
                not clause
                or any(marker in clause for marker in model_markers)
                or "整体健康状态" in clause
                or _contains_unsupported_general_level(clause)
                or clause.endswith(("多个", "项目中", "以及", "、"))
                or clause
                in {
                    "需综合评估",
                    "建议综合评估",
                    "提示需要关注",
                    "提示需关注",
                    "需要关注",
                    "需关注",
                }
            ):
                continue
            retained.append(clause)
        if retained:
            output.append("；".join(retained) + "。")
    return "".join(output).strip()


def _sanitize_microbe_narrative(
    value: str,
    *,
    microbe: Any,
    has_session_context: bool,
    grounded_numbers: set[float] | None = None,
    field: str = "conclusion",
) -> str:
    if field == "patient_interpretation":
        # Preserve the evidence-backed medical statement while removing
        # patient-page boundary clauses and evidence-process narration. These
        # restrictions remain enforced by the structured evidence fields.
        value = re.sub(
            r"[；;][^。！？!?]{0,160}(?:粪便样本|仅凭检出|"
            r"不能确定致病|不能证明|不意味着|"
            r"需在特定情况)[^。！？!?]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:但)?在健康人[^。！？!?]{0,160}"
            r"(?:仅凭检出|不能确定|不意味着)[^。！？!?]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,]病例报告[^，,。；;]*?(?=[，,。；;]|$)",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:属于)?(?:临床)?指南明确的病原体关联",
            "",
            value,
        )
        value = re.sub(
            r"(?:^|(?<=[。！？!?]))(?:在)?(?:本次)?(?:在)?"
            r"肠道样本[^。！？!?]{0,160}[。！？!?]?",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:但)?[^。；;]{0,100}"
            r"与肠道检出关联性有限",
            "",
            value,
        )
        value = re.sub(
            r"(?:^|(?<=[。！？!?]))检出(?:该菌|此菌|这一菌种)?"
            r"[^。！？!?]{0,120}(?:潜在风险|不意味着|并不意味着)"
            r"[^。！？!?]*[。！？!?]?",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:且|并|同时|也)?[^。；;]{0,80}"
            r"(?:抗生素|抗菌药|抗凝药|药物|用药|治疗)[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:但|且|并|同时)?(?:也)?(?:可能)?(?:是|属于)?"
            r"[^。；;]{0,30}(?:口腔)?共生菌[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            rf"(?:^|(?<=[。！？!?])){re.escape(microbe.name)}是肠道常见菌群[，,](?:但)?部分亚型",
            f"{microbe.name}部分亚型",
            value,
        )
        value = value.replace("的群体性关联已有文献报告", "存在群体层面的相关性")
        value = re.sub(
            r"([\u3400-\u9fffA-Za-z .-]+?)在人体临床证据中被列为",
            r"\1是",
            value,
        )
        value = re.sub(
            r"人体临床证据将([\u3400-\u9fffA-Za-z .-]+?)列为",
            r"\1是",
            value,
        )
        value = re.sub(
            r"[，,](?:流行病学|人体|临床)?研究支持[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"(?:^|(?<=[。！？!?]))(?:该菌|此菌|其)(?:是|属于)?(?:一种)?"
            r"(?:机会性?致病菌|条件致病菌|共生菌|"
            r"可作为定植菌检出)[，,]?",
            "",
            value,
        )
        value = re.sub(
            rf"(?:^|(?<=[。！？!?])){re.escape(microbe.name)}"
            r"(?:是|属于)(?:一种)?(?:机会性?致病菌|条件致病菌)[，,]?",
            "",
            value,
        )
        value = value.replace(
            "人体临床资料记录其",
            microbe.name,
        ).replace("人体研究记录其", microbe.name)
        value = value.replace("确定性机会性病原体", "机会性病原体")
        value = value.replace("确定性病原体", "病原体")
        value = re.sub(
            r"[，,]?但该证据来自[^。；;]*?(?:仅提供背景参考|仅作背景参考)",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:部分)?菌株[^。；;]*(?:毒素|毒力|基因)[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"(?:部分|某些)菌株[^。；;]*"
            r"(?:免疫调节|特性|毒素|毒力|基因)[^。；;]*[；;]?",
            "",
            value,
        )
        value = re.sub(
            r"[，,]在[^。；;]{1,80}?患者中可引起[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:该|此)?(?:感染|疾病)?[^，。；;]{0,60}"
            r"(?:治疗困难|需重点预防|需预防)[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:但|而|且)?(?:本次|本次检测|本报告)"
            r"[^。；;]{0,100}(?:不直接|仅反映|仅提示)[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:但)?(?:该|此)?证据来自[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:提示|表明)该菌[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,]提示其可能参与[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:该菌与[^。；;]{0,40})?的?关联已有证据支持[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"^(?:该菌是)?[^。；;]{0,40}调查中需要关注的病原体[，,]?",
            "该菌",
            value,
        )
        value = re.sub(
            r"[，,](?:但|且|并|同时)?[^。；;]{0,80}"
            r"(?:鉴定|测序|检测|检查|确认)[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:也)?可作为定植菌检出[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:是)?(?:医疗|医院)相关感染[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:尤其)?(?:多见于|常见于|主要见于|主要影响|"
            r"在)[^，。；;]{0,50}?(?:患者|宿主)(?:中)?[^。；;]*",
            "",
            value,
        )
        value = re.sub(
            r"[，,](?:且|并|同时)?[^，。；;]{0,40}(?:多重)?耐药[^。；;]*",
            "",
            value,
        )
    source_text = " ".join(
        part
        for part in (
            microbe.source_finding.status,
            microbe.source_finding.reference_range,
        )
        if part
    )
    source_has_level = any(term in source_text for term in _SOURCE_LEVEL_TERMS)
    sanitized = _sanitize_sentences(
        value,
        predicate=lambda sentence: (
            _safe_medical_prose(sentence)
            and _safe_symptom_claim(sentence, has_context=has_session_context)
            and not _is_boundary_boilerplate(sentence)
            and not _contains_unsupported_absolute_claim(sentence)
            and not _is_missing_data_filler(sentence)
            and not (
                field == "patient_interpretation" and _is_boundary_boilerplate(sentence)
            )
            and not (
                field == "patient_interpretation"
                and _is_treatment_or_management_content(sentence)
            )
            and not _is_overlong_enumeration(sentence)
            and _numbers_are_grounded(sentence, grounded_numbers)
            and (
                source_has_level
                or not any(term in sentence for term in _INFERRED_LEVEL_TERMS)
            )
            and not (
                field
                in {
                    "conclusion",
                    "clinical_role",
                    "patient_interpretation",
                }
                and not microbe.disease_and_risks
                and _contains_unbound_outcome_claim(sentence)
            )
            and not (
                field == "patient_interpretation"
                and _contains_patient_level_speculation(
                    sentence, has_session_context=has_session_context
                )
            )
        ),
    )
    sanitized = _sanitize_sentences(
        sanitized,
        predicate=lambda sentence: (
            not any(
                term in sentence
                for term in (
                    "不作为诊断",
                    "不能作为诊断",
                    "不代表诊断",
                    "不反映患病概率",
                    "不代表患病概率",
                    "不构成临床诊断",
                )
            )
        ),
    )
    sanitized = sanitized.strip(" ；;")
    if sanitized and sanitized[-1] not in "。！？!?":
        sanitized += "。"
    if microbe.evidence_refs:
        return sanitized
    return _sanitize_sentences(
        sanitized,
        predicate=lambda sentence: (
            not any(term in sentence for term in _UNSUPPORTED_UNCITED_TERMS)
            or (
                any(origin in sentence for origin in ("原报告", "检测方"))
                and not any(term in sentence for term in ("疾病", "风险", "感染"))
            )
        ),
    )


def _contains_unbound_outcome_claim(value: str) -> bool:
    """Reject an uncited outcome while retaining an evidence-backed role.

    A directly aligned human record can support descriptions such as
    ``opportunistic pathogen``, ``commensal`` or ``colonisation`` even when it
    does not establish a stool-specific disease relation. Treating those
    organism roles as disease claims discarded model-authored prose and
    forced the public report back to one repeated pathogen template. Concrete
    infections, diseases, symptoms and causal relations remain disallowed
    until a structured risk relation binds them.
    """

    return bool(re.search(r"与[^。；]{1,40}(?:相关|关联)", value)) or any(
        term in value
        for term in (
            "感染",
            "疾病",
            "风险",
            "胃肠炎",
            "腹泻",
            "便秘",
            "痢疾",
            "心内膜炎",
            "骨髓炎",
            "肺病",
            "皮炎",
            "骨质疏松",
            "糖尿病",
            "癌",
            "综合征",
            "导致",
            "中毒",
            "炎症反应",
        )
    )


def _contains_patient_level_speculation(
    value: str, *, has_session_context: bool
) -> bool:
    if any(term in value for term in ("污染", "偶然携带", "暴露所致")):
        return True
    return bool(
        not has_session_context
        and re.search(
        r"(?:未|无|没有)[^。；]{0,20}(?:症状|表现|感染证据)", value
        )
    )


def _interpretation_adds_unbound_outcome(value: str, *, microbe: Any) -> bool:
    allowed = " ".join(
        part
        for risk in microbe.disease_and_risks
        for part in (risk.name, risk.description)
    )
    markers = (
        "硬膜外脓肿",
        "脊柱炎",
        "脊椎炎",
        "皮肤感染",
        "皮肤伤口",
        "下呼吸道感染",
        "肺炎",
        "根管感染",
        "根尖周疾病",
        "牙周炎",
        "角膜炎",
        "脂溢性皮炎",
        "帕金森病",
        "骨炎",
        "脑膜炎",
        "菌血症",
        "败血症",
        "心包炎",
        "食源性肠炎",
        "胃肠炎",
        "腹泻",
        "结直肠癌",
        "溃疡性结肠炎",
        "炎症性肠病",
        "腺瘤",
        "肉毒中毒",
        "早产",
        "艰难梭菌感染",
        "念珠菌病",
        "真菌病",
        "中枢神经系统疾病",
        "尿路感染",
        "伤口感染",
    )
    return any(marker in value and marker not in allowed for marker in markers)


def _validated_microbe_confirmation(
    value: str | None,
    *,
    allowed: set[str],
    has_session_context: bool,
    grounded_numbers: set[float] | None,
) -> str | None:
    if not value or not allowed:
        return None
    normalized_value = _normalize_evidence_text(value)
    if not any(
        _normalize_evidence_text(name) in normalized_value for name in allowed if name
    ):
        return None
    return _validated_text(
        value,
        has_session_context=has_session_context,
        grounded_numbers=grounded_numbers,
    )


def _sanitize_disease_patient_suggestion(
    value: str | None,
    *,
    disease: Any,
    has_session_context: bool,
    grounded_numbers: set[float] | None = None,
) -> str | None:
    """Keep one concrete, disease-bound patient suggestion.

    The model may summarize only this row's evidence, existing results and
    validated evaluations.  Generic professional-evaluation filler is less
    useful than an empty field and is deliberately rejected.
    """

    if not value:
        return None
    normalized = _validated_text(
        value,
        has_session_context=has_session_context,
        grounded_numbers=grounded_numbers,
    )
    normalized = patient_facing_text(normalized)
    if not normalized:
        return None
    if any(
        marker in normalized
        for marker in (
            "可以优先关注",
            "相关健康表现",
            "必要时由专业人员进一步评估",
            "需结合临床",
            "需要结合临床",
            "建议就医",
            "建议用药",
            "治疗方案",
            "用药方案",
            "益生菌",
            "补充剂",
        )
    ) or _contains_unsupported_absolute_claim(normalized):
        return None

    allowed_evaluations = {
        item.name for item in disease.suggested_evaluations if item.name
    }
    allowed_existing = {
        item.item_name for item in disease.existing_results if item.item_name
    }
    if any(
        marker in normalized
        for marker in ("检查", "检测", "测定", "量表", "问卷", "内镜", "复查")
    ) and not any(
        name in normalized for name in {*allowed_evaluations, *allowed_existing}
    ):
        return None
    display_name = patient_facing_name(disease.display_name or disease.name) or ""
    for evaluation in disease.suggested_evaluations:
        if not evaluation.name or evaluation.name not in normalized:
            continue
        rationale = patient_facing_text(evaluation.rationale) or ""
        # A symptom/complication instrument must not be rewritten as a direct
        # screen or confirmation test for the parent disease.  For example,
        # HADS may assess depression in Parkinson disease, but does not screen
        # for Parkinson disease itself.
        if display_name and any(
            marker in normalized
            for marker in (
                f"筛查{display_name}",
                f"确认{display_name}",
                f"评估{display_name}相关情况",
            )
        ) and not any(
            marker in rationale
            for marker in (
                f"{display_name}筛查",
                f"筛查{display_name}",
                f"{display_name}诊断",
                f"诊断{display_name}",
                f"{display_name}确认",
                f"确认{display_name}",
                f"评估{display_name}",
            )
        ):
            return None
    if not _disease_sentence_has_patient_fact(normalized, disease=disease):
        return None

    sentences = [
        sentence
        for sentence in re.split(r"(?<=[。！？!?])\s*", normalized)
        if sentence
    ][:2]
    result = patient_facing_text("".join(sentences))
    if not result:
        return None
    return result if result[-1] in "。！？!?" else result + "。"


def _sanitize_disease_narrative(
    value: str,
    *,
    disease: Any,
    has_session_context: bool,
    grounded_numbers: set[float] | None = None,
) -> str:
    if not _disease_needs_narrative(disease):
        return ""
    if any(
        marker in value
        for marker in (
            "原菌群报告",
            "原报告列出",
            "原报告将",
            "疾病关联项目",
            "健康关注项目",
            "检测方模型",
        )
    ):
        return ""
    # EviMed summaries can occasionally retain this single common English
    # modifier inside otherwise Chinese prose. Translate the exact phrase so
    # the report stays readable without introducing a new medical claim.
    value = re.sub(
        r"\buncontrolled\s*(?=癫痫)",
        "控制不佳的",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^[*#\s]+", "", value)
    value = re.sub(
        r"^[（(][^()（）]{1,24}[）)]\s*(?:声明|statement)\s*\d*[：:]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\[(?:\d+(?:[-,–]\d+)*)\]", "", value)
    # Remove an editorial instruction embedded in an otherwise useful disease
    # explanation without discarding the surrounding EviMed-authored facts.
    # A sentence made only of this instruction is still rejected below.
    value = re.sub(
        r"[，,](?:强调|需注意|需要注意)区分[^，,。；;]*",
        "",
        value,
    )
    value = re.sub(r"[，,](?:并且|且|同时)[，,]?", "，", value)
    value = re.sub(
        r"[，,](?:其)?病理机制(?:的)?认识"
        r"(?:仍)?(?:在)?(?:持续|不断|日益)?深入[^ 。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"(?:^|(?<=[；;。]))[^。；;]{0,100}?的流行病学[；;]?",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:世界卫生组织|WHO|全球)[^。！？!?]{0,120}"
        r"(?:调查|发病率|患病率|死亡率)[^。！？!?]*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"[，,](?:运动可|运动能|控制目标应|目标应|早期筛查|建议进行|建议接受)[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:可能)?影响[^。！？!?]*(?:治疗|应答)[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:需|需要|可|可通过|通常需)[^。！？!?]{0,100}"
        r"(?:检查|诊断|评估)[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:不同医疗机构|不同医院|不同医师)[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:可用于|用于)[^。！？!?]{0,80}(?:临床)?识别[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:但)?个体风险[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:但|不过)?(?:本次)?(?:检测|菌群结果)"
        r"[^。！？!?]{0,80}(?:未直接提示|不能单独判断|不代表)"
        r"[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,][^。！？!?]{0,80}(?:有助于|可帮助)"
        r"(?:延缓|改善|降低|预防)[^。！？!?]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,][^。！？!?]{0,80}保护[^。！？!?]{0,50}(?:至关重要|十分重要)[^。！？!?]*",
        "",
        value,
    )
    # Confirmation methods belong in the dedicated evaluation column, not in
    # the disease interpretation itself.
    value = re.sub(
        r"[，,][^。！？!?]{0,100}?(?:粪钙卫蛋白|内镜|活检|脑电图|"
        r"影像学|问卷|量表|检查|检测|测定)[^。！？!?]*",
        "",
        value,
    )
    # Foreign rankings and population statistics are not an individual
    # interpretation for this report.
    value = _sanitize_sentences(
        value,
        predicate=lambda sentence: (
            not bool(
                re.search(
                    r"(?:英国|美国|日本|欧洲|United Kingdom|\bUK\b)"
                    r"[^。！？!?]{0,80}(?:第\s*\d|常见|生存率|发病率|患病率|死亡率)",
                    sentence,
                    flags=re.IGNORECASE,
                )
            )
            and not any(
                marker in sentence
                for marker in (
                    "患病率预计",
                    "人口老龄化",
                    "社会经济差异",
                    "社会决定因素",
                    "弱势社区",
                    "弱势人群",
                    "体重污名",
                    "经济负担",
                    "疾病负担",
                )
            )
        ),
    )
    source_text = disease.source_result or ""
    source_has_level = any(term in source_text for term in _SOURCE_LEVEL_TERMS)
    sanitized = _sanitize_sentences(
        value,
        predicate=lambda sentence: (
            _safe_medical_prose(sentence)
            and _safe_symptom_claim(sentence, has_context=has_session_context)
            and not _contains_unsupported_absolute_claim(sentence)
            and not _is_missing_data_filler(sentence)
            and not _is_boundary_boilerplate(sentence)
            and not _is_treatment_or_management_content(sentence)
            and _disease_narrative_has_public_clinical_meaning(
                sentence,
                disease_name=disease.name,
            )
            and not bool(
                re.search(
                    r"^(?:全球|世界卫生组织|WHO)[^。！？!?]{0,120}"
                    r"(?:调查|发病率|患病率|死亡率)",
                    sentence,
                    flags=re.IGNORECASE,
                )
            )
            and not any(
                evaluation.name in sentence
                for evaluation in disease.suggested_evaluations
            )
            and _numbers_are_grounded(sentence, grounded_numbers)
            and not any(term in sentence for term in ("模型", "分值", "指数"))
            and (
                source_has_level
                or not any(term in sentence for term in _DISEASE_LEVEL_TERMS)
            )
            and ("正常范围" not in sentence or bool(disease.existing_results))
            and not (disease.source_type == "evimed_association" and "模型" in sentence)
        ),
    )
    sanitized = _sanitize_sentences(
        sanitized,
        predicate=lambda sentence: (
            not any(
                term in sentence
                for term in (
                    "不作为诊断",
                    "不能作为诊断",
                    "不代表诊断",
                    "不反映患病概率",
                    "不代表患病概率",
                    "不构成临床诊断",
                    "不代表临床诊断",
                    "仅作为模型提示",
                    "可能与菌群构成有关",
                    "仅提示心理状态",
                    "强调区分",
                    "病理机制认识不断深入",
                    "微创外科技术",
                )
            )
        ),
    )
    sanitized = _sanitize_sentences(
        sanitized,
        predicate=lambda sentence: _disease_narrative_sentence_is_bound(
            sentence,
            disease=disease,
        )
        and not _is_disease_definition_sentence(
            sentence,
            disease_name=disease.name,
        ),
    )
    has_association_evidence = bool(disease.association_evidence_refs) or (
        disease.source_type == "evimed_association" and bool(disease.evidence_refs)
    )
    if has_association_evidence:
        result = sanitized
    else:
        result = _sanitize_sentences(
            sanitized,
            predicate=lambda sentence: (
                not any(term in sentence for term in _UNSUPPORTED_UNCITED_TERMS)
                or (
                    any(
                        origin in sentence
                        for origin in ("原报告", "检测方", "模型分值", "模型结果")
                    )
                    and not any(
                        term in sentence
                        for term in ("导致", "风险升高", "风险增高", "感染")
                    )
                )
            ),
        )
    result = result.rstrip("；; ")
    if not _disease_narrative_explains_risk(result, disease_name=disease.name):
        return ""
    return f"{result}。" if result and result[-1] not in "。！？!?" else result


def _sanitize_abnormal_indicator_summary(value: str, *, indicator: Any) -> str:
    """Keep a clinical-meaning summary bound to one deterministic indicator."""

    normalized = " ".join(value.split()).strip()
    if not normalized or len(normalized) > 800:
        return ""
    if any(
        marker in normalized
        for marker in (
            "AI",
            "JSON",
            "模型",
            "规则",
            "用户上传",
            "PDF",
            "图片",
            "文件名",
            "解析过程",
            "后台处理",
            "原报告列出",
            "原报告将",
            "已确诊",
            "诊断为",
            "已经患有",
            "建议检查",
            "建议复查",
            "建议治疗",
            "建议用药",
            "治疗方案",
            "用药方案",
        )
    ):
        return ""
    if _normalize_evidence_text(indicator.item) not in _normalize_evidence_text(
        normalized
    ):
        return ""
    allowed_numbers = {
        float(number)
        for number in re.findall(
            r"(?<![A-Za-z])\d+(?:\.\d+)?",
            " ".join(
                str(item)
                for item in (indicator.value, indicator.reference_range)
                if item not in (None, "")
            ),
        )
    }
    if not _numbers_are_grounded(normalized, allowed_numbers):
        return ""
    result = _sanitize_sentences(
        normalized,
        predicate=lambda sentence: (
            _safe_medical_prose(sentence)
            and not _is_treatment_or_management_content(sentence)
            and not _is_missing_data_filler(sentence)
        ),
    ).rstrip("；; ")
    return f"{result}。" if result and result[-1] not in "。！？!?" else result


def _disease_narrative_has_public_clinical_meaning(
    value: str, *, disease_name: str
) -> bool:
    """Keep medical meaning; hide source/editorial and research workflow prose."""

    if value.count("(") != value.count(")") or value.count("（") != value.count("）"):
        return False
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "erratum",
            "勘误",
            "摘要表明",
            "摘要显示",
            "医务人员",
            "认识不足",
            "忽视或漏诊",
            "忽视",
            "漏诊",
            "专家经过多次讨论",
            "根据各自的经验",
            "科学声明",
            "文章主要包括",
            "本文主要包括",
            "分期处理规约",
            "生存质量有所改善",
            "手术风险",
            "严重危害人民健康",
            "威胁人民健康",
            "病变分割",
            "分割算法",
            "自动算法",
            "预测价值不如",
            "诊断价值",
            "有作为诊断标志物的潜力",
            "血清irisin",
            "nesfatin-1",
            "ykl-40",
            "th22",
            "il-22",
            "ncf1c",
            "视觉诱发的癫痫",
            "视觉诱发性",
            "光敏性癫痫",
            "闪光刺激",
            "致痫条纹",
            "免疫性癫痫相关概念",
            "已有明确描述",
            "实用临床定义",
            "对患者的心理",
            "心理需求",
            "内容涉及一般原则",
            "澳大利亚心血管疾病",
            "发病高峰年龄",
            "男女比例",
            "术后并发症",
            "bormann iii",
            "bormann iv",
            "住院时间延长",
            "中国常见的恶性肿瘤",
            "每年约有50%",
            "痰瘀互结证",
            "中医证候",
            "常被低估",
            "心电图异常",
            "患者心理",
            "抑郁、焦虑症状及睡眠问题",
            "无症状平均风险成人",
            "并称“四高”",
            '并称"四高"',
            "中足受累",
            "步行功能残疾",
            "血清a-ccp",
            "rf、crp",
            "水平异常升高",
        )
    ):
        return False
    subject_match = re.match(
        r"^([^,，。；;]{2,32}?(?:症|病|癌|炎|异常))与",
        value,
    )
    if subject_match and disease_name not in subject_match.group(1):
        return False
    normalized_core = re.sub(r"[\s，,。；;:：（）()]", "", value)
    normalized_name = re.sub(r"[\s，,。；;:：（）()]", "", disease_name)
    if normalized_core in {
        f"{normalized_name}相关临床特征",
        f"{normalized_name}临床特征",
        f"{normalized_name}相关临床表现",
        f"{normalized_name}临床表现",
        f"{normalized_name}相关信息",
    }:
        return False
    if (
        disease_name == "腹泻型肠易激综合征"
        and "慢性水样腹泻" in value
        and "腹痛" not in value
    ):
        return False
    core = value.strip().rstrip("。！？!?")
    return not re.fullmatch(
        r"[^,，。；;]{2,40}(?:发病机制|病理机制)(?:十分)?复杂",
        core,
    )


def _is_disease_definition_sentence(value: str, *, disease_name: str) -> bool:
    normalized = re.sub(
        r"^(?:本次检测显示|本次检测提示)[，,:：]?\s*",
        "",
        value.strip(),
    )
    name = re.escape(disease_name)
    return bool(
        re.search(
            rf"^(?:{name}|该疾病|该健康问题)"
            r"(?:是一种|是一类|是常见的|是一项|以[^,，。；;]{2,80}为特征)",
            normalized,
        )
    )


def _disease_narrative_explains_risk(
    value: str,
    *,
    disease_name: str,
) -> bool:
    """Require a report-specific rationale and an attention direction."""

    if not value or disease_name not in value:
        return False
    has_report_basis = any(
        marker in value
        for marker in (
            "本次检测",
            "本次菌群",
            "菌群特征",
            "菌群变化",
            "相关菌",
        )
    )
    has_evidence_relation = any(
        marker in value
        for marker in (
            "人体研究",
            "人群研究",
            "临床研究",
            "人体证据",
            "研究观察",
            "存在关联",
            "相关",
        )
    )
    has_attention = any(
        marker in value
        for marker in (
            "适当关注",
            "建议关注",
            "可关注",
            "值得关注",
            "需要关注",
            "留意",
            "观察",
        )
    )
    has_risk_object = any(
        marker in value
        for marker in (
            "风险",
            "症状",
            "功能",
            "不适",
            "表现",
            "排便",
            "舒适度",
            "健康影响",
            "不良影响",
        )
    )
    return has_report_basis and has_evidence_relation and has_attention and has_risk_object


def _disease_narrative_sentence_is_bound(value: str, *, disease: Any) -> bool:
    """Allow a scoped follow-up sentence without weakening fact ownership.

    The first sentence normally names the retained disease and related
    microbes; the second says what symptoms, function or long-term risk to
    watch. Requiring the disease name in every sentence deleted that second
    sentence and then rejected the whole paragraph as incomplete.
    """

    if _disease_sentence_has_patient_fact(value, disease=disease):
        return True
    if not any(
        marker in value
        for marker in (
            "因此",
            "由此",
            "本次检测",
            "这类关联",
            "该关联",
            "该方向",
            "相关风险",
        )
    ):
        return False
    if not any(marker in value for marker in ("关注", "留意", "观察")):
        return False
    if not any(
        marker in value
        for marker in (
            "风险",
            "症状",
            "功能",
            "不适",
            "表现",
            "排便",
            "舒适度",
            "健康影响",
            "不良影响",
        )
    ):
        return False
    # A continuation may mention symptoms, but a named disease target must be
    # the retained row's disease rather than a model-added diagnosis.
    attention_target = re.search(
        r"(?:关注|留意|观察)([^\u3002；]{1,40}?)(?:病|癌|炎|综合征)",
        value,
    )
    return not attention_target or disease.name in attention_target.group(0)


def _disease_needs_narrative(disease: Any) -> bool:
    return bool(
        disease_has_patient_microbiome_signal(disease)
        and disease.association_evidence_refs
    )


def _disease_sentence_has_patient_fact(value: str, *, disease: Any) -> bool:
    """Keep prose only when it names a retained relation, result, or action."""
    normalized = _normalize_evidence_text(value)
    if disease.item_type in {"biomarker", "phenotype"}:
        return any(
            _normalize_evidence_text(alias) in normalized
            for alias in _target_aliases(disease.name)
            if alias
        )
    candidates = [
        disease.name,
        *_target_aliases(disease.name),
        *disease.related_microbes,
        *(item.item_name for item in disease.existing_results),
        *(item.result for item in disease.existing_results),
        *(item.name for item in disease.suggested_evaluations),
    ]
    allowed_microbes = {
        _normalize_evidence_text(name) for name in disease.related_microbes
    }
    mentioned_microbes = {
        _normalize_evidence_text(name)
        for name in re.findall(
            r"(?<![A-Za-z])[A-Z][a-z]{2,}(?:\s+[a-z][a-z0-9.-]{2,})(?![A-Za-z])",
            value,
        )
    }
    if mentioned_microbes - allowed_microbes:
        return False
    return any(
        (candidate_text := _normalize_evidence_text(str(candidate)))
        and candidate_text in normalized
        for candidate in candidates
    )


def _validated_microbe_summary(
    value: str | None,
    *,
    microbes: AbnormalMicrobesSection,
    has_session_context: bool,
    grounded_numbers: set[float] | None = None,
) -> str | None:
    if value is None:
        return None
    has_non_abnormal = any(
        item.source_finding.status in {"检出信号", "痕量检出", "检出"}
        for item in microbes.items
    )
    sanitized = _sanitize_sentences(
        value,
        predicate=lambda sentence: (
            _safe_medical_prose(sentence)
            and _safe_symptom_claim(sentence, has_context=has_session_context)
            and not _contains_unsupported_absolute_claim(sentence)
            and not _contains_unsupported_general_level(sentence)
            and not _is_missing_data_filler(sentence)
            and not _is_overlong_enumeration(sentence)
            and not re.search(
                r"(?:共|记录|纳入|检出)\s*\d+\s*项|"
                r"(?:丰度低|偏低|检出|痕量检出)\s*\d+\s*项",
                sentence,
            )
            and _numbers_are_grounded(sentence, grounded_numbers)
            and not (
                has_non_abnormal
                and "均" in sentence
                and "异常" in sentence
                and "标记" in sentence
            )
            and not (
                "所有菌种" in sentence
                and any(marker in sentence for marker in ("检出信号", "痕量检出"))
            )
            and "未标示异常" not in sentence
        ),
    )
    if not sanitized:
        return None
    if _contains_unknown_scientific_name(sanitized, microbes=microbes):
        return None
    category_requirements = {
        "pathogenic_signal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
    }
    present_categories = {item.category for item in microbes.items}
    required_categories = present_categories & category_requirements
    if len(required_categories) > 1:
        for category in required_categories:
            if not any(
                item.category == category
                and (item.name in sanitized or item.scientific_name in sanitized)
                for item in microbes.items
            ):
                return None
    if len(microbes.items) > 10:
        if len(sanitized) < 60:
            return None
        mentioned = sum(
            item.name in sanitized or item.scientific_name in sanitized
            for item in microbes.items
        )
        if mentioned < 3 or mentioned > 8:
            return None
    return sanitized


def _extractive_microbe_summary(
    *,
    microbes: AbnormalMicrobesSection,
    conclusions: dict[str, str | None],
    has_session_context: bool,
    grounded_numbers: set[float] | None = None,
) -> str | None:
    """Summarize already-validated model prose without fixed medical claims."""
    labels = {
        "pathogenic_signal": "病原相关菌",
        "opportunistic_signal": "条件致病菌",
        "fungi_pathogenic_signal": "潜在致病真菌",
        "beneficial_abnormal": "有益菌异常",
        "other_abnormal": "其他异常菌",
    }
    if len(microbes.items) <= 10:
        concise_pieces: list[str] = []
        for category, label in labels.items():
            candidates = [item for item in microbes.items if item.category == category]
            if not candidates:
                continue
            representative = max(
                candidates,
                key=lambda item: (
                    bool(item.disease_and_risks),
                    len(item.disease_and_risks),
                    _numeric_source_value(item.source_finding.value),
                ),
            )
            concise_pieces.append(
                f"{label}：{(conclusions.get(representative.microbe_id) or _source_fact_microbe_conclusion(representative)).strip()}"
            )
        if not concise_pieces:
            return None
        return _validated_microbe_summary(
            "；".join(concise_pieces),
            microbes=microbes,
            has_session_context=has_session_context,
            grounded_numbers=grounded_numbers,
        )

    pieces: list[str] = []
    for category, label in labels.items():
        candidates = [item for item in microbes.items if item.category == category]
        if not candidates:
            continue
        representatives = sorted(
            candidates,
            key=lambda item: (
                bool(item.disease_and_risks),
                len(item.disease_and_risks),
                _numeric_source_value(item.source_finding.value),
            ),
            reverse=True,
        )[:2]
        details = []
        for representative in representatives:
            finding = representative.source_finding
            result = finding.status or "检出"
            if finding.value is not None:
                result += f"{finding.value}{finding.unit or ''}"
            risk_names = "、".join(
                risk.name for risk in representative.disease_and_risks
            )
            detail = f"{representative.name}（{result}）"
            if risk_names:
                detail += f"，人体证据涉及{risk_names}"
            details.append(detail)
        pieces.append(f"{label}以{'；'.join(details)}为代表")
    if not pieces:
        return None
    risk_names = list(
        dict.fromkeys(
            risk.name for item in microbes.items for risk in item.disease_and_risks
        )
    )[:8]
    if risk_names:
        pieces.append(
            f"目前有直接人体资料支持的主要医学关联涉及{'、'.join(risk_names)}"
        )
    candidate = "；".join(pieces) + "。"
    validated = _validated_microbe_summary(
        candidate,
        microbes=microbes,
        has_session_context=has_session_context,
        # All numbers in this branch are copied from source findings or
        # calculated from the parsed source rows, not model-authored prose.
        grounded_numbers=None,
    )
    return _bounded_display_text(validated, minimum=150, maximum=400)


def _numeric_source_value(value: str | None) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else float("-inf")


def _source_fact_microbe_conclusion(microbe: Any) -> str:
    status = microbe.source_finding.status or "检出"
    sentence = f"检测结果显示，{microbe.name}为{status}"
    if microbe.source_finding.value is not None:
        sentence += (
            f"，检测值为{microbe.source_finding.value}"
            f"{microbe.source_finding.unit or ''}"
        )
    if microbe.source_finding.reference_range:
        sentence += (
            f"，参考范围为{microbe.source_finding.reference_range}"
            f"{microbe.source_finding.unit or ''}"
        )
    return sentence + "。"


def _source_fact_microbe_narrative(microbe: Any) -> MicrobeNarrative:
    descriptions = _compact_microbe_risk_descriptions(microbe)
    relation_interpretation = "。".join(descriptions) or None
    if relation_interpretation:
        relation_interpretation += "。"
        relation_interpretation = (
            _sanitize_sentences(
                relation_interpretation,
                # Risk descriptions have already passed the claim-level relation
                # filter.  Reuse them verbatim so a visible table risk cannot lose
                # its paired medical interpretation in a second, broader filter.
                predicate=lambda sentence: not _is_boundary_boilerplate(sentence),
            )
            or None
        )
    interpretation_parts: list[str] = []
    if microbe.clinical_role:
        interpretation_parts.append(microbe.clinical_role.rstrip("。"))
    if relation_interpretation:
        relation_text = relation_interpretation.rstrip("。")
        role_already_names_outcomes = bool(
            interpretation_parts
            and microbe.disease_and_risks
            and all(
                risk.name
                and _normalize_evidence_text(risk.name)
                in _normalize_evidence_text(" ".join(interpretation_parts))
                for risk in microbe.disease_and_risks[:3]
            )
        )
        if not role_already_names_outcomes and not any(
            _normalize_evidence_text(relation_text) in _normalize_evidence_text(part)
            for part in interpretation_parts
        ):
            interpretation_parts.append(relation_text)
    interpretation = "。".join(dict.fromkeys(interpretation_parts))
    if interpretation:
        interpretation += "。"
    else:
        # A missing disease relation must not turn the whole row into an empty
        # shell.  When no medical source survives, report only the measured
        # source fact and do not invent a mechanism or patient-level risk.
        interpretation = (
            _source_fact_microbe_conclusion(microbe)
            + "目前仅可确认该项检测变化，尚无足够的人体医学依据"
            "解释其与具体疾病或不良影响的关系。"
        )
    return MicrobeNarrative(
        microbeId=microbe.microbe_id,
        clinicalRole=microbe.clinical_role,
        conclusion=_source_fact_microbe_conclusion(microbe),
        patientInterpretation=interpretation,
    )


def _compact_microbe_risk_descriptions(microbe: Any) -> list[str]:
    """Combine repeated predicates while preserving the validated risk names."""
    causal: list[str] = []
    pathogen: list[str] = []
    associated: list[str] = []
    other: list[str] = []
    escaped_name = re.escape(microbe.name)
    for risk in microbe.disease_and_risks[:3]:
        description = (risk.description or "").strip().rstrip("。")
        if not description:
            continue
        if re.fullmatch(
            rf"{escaped_name}(?:（[^）]+）)?可引起{re.escape(risk.name)}",
            description,
        ):
            causal.append(risk.name)
            continue
        if re.fullmatch(
            rf"{escaped_name}(?:（[^）]+）)?是.+的病原体",
            description,
        ):
            pathogen.append(risk.name)
            continue
        if re.fullmatch(
            rf"{escaped_name}(?:（[^）]+）)?与.+(?:相关|关联)",
            description,
        ):
            associated.append(risk.name)
            continue
        other.append(description)

    compact: list[str] = []
    predicates: list[str] = []
    if causal:
        predicates.append(f"可引起{'、'.join(dict.fromkeys(causal))}")
    if pathogen:
        prefix = "也是" if predicates else "是"
        predicates.append(f"{prefix}{'、'.join(dict.fromkeys(pathogen))}的病原体")
    if associated:
        prefix = "并与" if predicates else "与"
        predicates.append(f"{prefix}{'、'.join(dict.fromkeys(associated))}相关")
    if predicates:
        compact.append(f"{microbe.name}{'，'.join(predicates)}")
    compact.extend(dict.fromkeys(other))
    return compact


def _source_fact_disease_narrative(
    disease: Any,
    *,
    microbes: AbnormalMicrobesSection | None = None,
) -> str:
    parts = []
    if disease.medical_interpretation:
        parts.append(disease.medical_interpretation)
    disease_aliases = {
        _normalize_evidence_text(alias)
        for alias in (disease.name, disease.display_name, *_target_aliases(disease.name))
        if alias
    }
    relation_details = [
        detail
        for item in (microbes.items if microbes else [])
        for risk in item.disease_and_risks
        if _normalize_evidence_text(risk.name) in disease_aliases
        if risk.description
        and _risk_is_report_context_applicable(risk)
        if (
            detail := re.split(r"(?<=[。！？!?])\s*", risk.description.strip())[0]
            .strip()
            .rstrip("。；; ")
        )
    ]
    parts.extend(relation_details)
    sanitized = _sanitize_disease_narrative(
        "".join(dict.fromkeys(part for part in parts if part)),
        disease=disease,
        has_session_context=False,
    )
    if sanitized:
        return sanitized
    if disease.association_evidence_refs:
        display_name = patient_facing_text(disease.display_name or disease.name)
        if not display_name:
            return ""
        related = [
            patient_facing_text(name)
            for name in disease.related_microbes
            if patient_facing_text(name)
        ][:2]
        basis = (
            f"本次检测中{'、'.join(related)}等相关菌的变化"
            if related
            else "本次检测呈现的相关菌群特征"
        )
        evidence_details = list(dict.fromkeys(relation_details))[:2]
        if evidence_details:
            return (
                f"{basis}是本报告关注{display_name}方向的依据。"
                f"人体肠道研究具体提示：{'；'.join(evidence_details)}。"
            )
        return (
            f"{basis}与人体研究中观察到的{display_name}相关方向一致，"
            f"因此可以适当关注{display_name}及其相关健康风险。"
        )
    return ""


def _source_fact_disease_suggestion(disease: Any) -> str | None:
    """Build a concrete fallback without inventing symptoms or actions."""

    display_name = patient_facing_text(disease.display_name or disease.name)
    if not display_name:
        return None
    abnormal_items = list(
        dict.fromkeys(
            result.item_name
            for result in disease.existing_results
            if result.item_name and _clinical_result_is_abnormal(result)
        )
    )
    if abnormal_items:
        suggestion = (
            f"重点关注{display_name}相关的"
            f"{'、'.join(abnormal_items[:2])}变化。"
        )
        return patient_facing_text(suggestion)
    # Examination candidates have their own structured field and evidence
    # references. Copying one into the free-text advice column caused scales
    # and other candidates to appear as recommendations even after the
    # hospital-examination gate had correctly rejected them.
    return None


def _contains_unknown_scientific_name(
    value: str, *, microbes: AbnormalMicrobesSection
) -> bool:
    allowed = {item.scientific_name.casefold() for item in microbes.items}
    allowed_genera = {name.split()[0] for name in allowed if name}
    mentions = re.findall(
        r"(?<![A-Za-z])[A-Z][a-z]{2,}(?:\s+[a-z][a-z0-9.-]{2,})?(?![A-Za-z])",
        value,
    )
    for mention in mentions:
        normalized = mention.casefold()
        if " " in normalized:
            if normalized not in allowed:
                return True
        elif normalized not in allowed_genera:
            return True
    return False


def _safe_symptom_claim(value: str, *, has_context: bool) -> bool:
    if has_context:
        return True
    normalized = value.casefold()
    return not any(
        term.casefold() in normalized for term in _NEGATIVE_SYMPTOM_TERMS
    ) and not bool(re.search(r"(?:未|无|没有)[^。；]{0,20}(?:症状|表现)", value))


def _contains_unsupported_absolute_claim(value: str) -> bool:
    normalized = value.casefold()
    return any(term.casefold() in normalized for term in _UNSUPPORTED_ABSOLUTE_TERMS)


def _contains_unsupported_general_level(value: str) -> bool:
    return any(pattern.search(value) for pattern in _UNSUPPORTED_GENERAL_LEVEL_PATTERNS)


def _is_missing_data_filler(value: str) -> bool:
    return any(term in value for term in _MISSING_DATA_FILLER_TERMS) or bool(
        re.search(r"(?:无|未见|没有)[^。；]{0,24}(?:证据|记录|资料|病史)", value)
    )


def _is_boundary_boilerplate(value: str) -> bool:
    """Hide execution-like disclaimers and unsupported exclusion language."""
    if any(
        marker in value
        for marker in (
            "不等同于",
            "不等于",
            "不代表",
            "不能作为",
            "不能用于",
            "不能据此",
            "仅作为背景",
            "作为背景",
            "仅作为参考",
            "仅表示存在",
            "只表示存在",
            "仅报告检出",
            "只报告检出",
            "不具有直接相关性",
            "不直接等同",
            "不直接指示",
            "不直接推断",
            "不直接提示",
            "仅反映",
            "仅作为检出信号",
            "本报告为粪便检出",
            "本次为粪便检出",
            "本样本为粪便",
            "可能为环境来源",
            "需结合",
            "需要结合",
            "未提示耐药性",
            "意义尚不明确",
            "意义尚不清楚",
            "在正常情况下可能不引起疾病",
            "在特定条件下可能引发感染",
            "您的健康状况需要重点关注",
            "整体健康状况需要重点关注",
            "排除了",
            "排除相关",
            "该指南描述",
            "指南描述",
            "指南介绍",
            "该文献讨论",
            "文献讨论",
            "文献介绍",
            "本次检出仅提示菌种存在",
            "本次在肠道菌群中检出，仅提示菌种存在",
            "本次在肠道菌群中检出，仅记录",
            "本次检出仅记录",
            "具体作用尚需更多研究",
            "存在早期阶段定义",
        )
    ):
        return True
    return bool(
        re.search(r"(?:不支持|不提示)[^。；]{0,30}(?:诊断|病变|感染)", value)
        or re.search(r"提示存在[^。；]{0,16}(?:核酸|信号)", value)
        or re.search(
            r"本次[^。；]{0,24}(?:检出|记录)[^。；]{0,16}(?:仅提示|仅记录)",
            value,
        )
        or re.search(
            r"本次[^。；]{0,24}检出[^。；]{0,40}"
            r"(?:低丰度|不一定|通常为|需警惕|需关注)",
            value,
        )
        or "与肠道检出关联性有限" in value
        or re.search(r"(?:无|未提供)[^。；]{0,20}(?:免疫学|检查|指标|资料)", value)
        or "尚需更多研究" in value
        or "表明目前未发现" in value
    )


def _is_treatment_or_management_content(value: str) -> bool:
    return any(
        marker in value
        for marker in (
            "治疗方案",
            "治疗",
            "诊断和治疗",
            "诊断与治疗",
            "诊断及治疗",
            "临床诊断",
            "诊断",
            "治疗建议",
            "治疗方法",
            "用药",
            "药物",
            "抗凝药",
            "抗生素",
            "达比加群",
            "阿哌沙班",
            "利伐沙班",
            "依度沙班",
            "服药",
            "使用抗毒素",
            "及时使用",
            "外科手术",
            "手术治疗",
            "化疗",
            "泻药",
            "渗透性泻药",
            "一线治疗",
            "二线治疗",
            "给予治疗",
            "补充治疗",
            "中医药",
            "他汀",
            "塞来昔布",
            "早筛",
            "早诊",
            "早治",
            "治疗可能",
            "治疗困难",
            "诊治",
            "管理策略",
            "营养管理",
            "营养支持",
            "管理的重点",
            "生活方式调整",
            "生活方式干预",
            "饮食调整",
            "行为调整",
            "排便调护",
            "健康调护",
            "减重",
            "血糖控制",
            "进行保护",
            "预防性干预",
            "需重点预防",
            "需预防",
            "建议进行",
            "建议接受",
            "建议采用",
            "建议咨询",
            "运动可改善",
            "运动可降低",
            "控制目标应",
            "控制目标",
            "合理目标",
            "管理提供建议",
            "提供管理建议",
            "该指南",
            "本指南",
            "指南替代",
            "指南推荐",
            "用于诊断",
            "用于排除",
            "诊断或排除",
            "药敏试验",
            "用于调节",
            "用于改善",
            "制剂",
            "组成成分",
        )
    )


def _is_overlong_enumeration(value: str) -> bool:
    separators = value.count("、") + value.count("，") + value.count("；")
    return value.count("、") > 8 or (len(value) > 450 and separators > 10)


def _sanitize_sentences(value: str, *, predicate) -> str:
    normalized = " ".join(value.split()).strip()
    normalized = _remove_non_display_clauses(normalized)
    if not normalized:
        return ""
    sentences = re.split(r"(?<=[。！？!?；;])\s*", normalized)
    retained = [
        sentence
        for sentence in sentences
        if sentence and not _is_incomplete_sentence(sentence) and predicate(sentence)
    ]
    return patient_facing_text("".join(retained).strip()) or ""


def _is_incomplete_sentence(value: str) -> bool:
    core = value.strip().rstrip("，,。；;!?！？").strip()
    if not core:
        return True
    dangling_connectives = (
        "但",
        "且",
        "同时",
        "此外",
        "因此",
        "同样",
        "以及",
        "其中",
        "尤其",
        "例如",
        "如",
        "多种",
        "多项",
    )
    if core.endswith(dangling_connectives):
        return True
    if "模型分值中" in core and not re.search(r"\d", core):
        return True
    return bool(
        re.search(r"(?:且|并且|同时)多(?:种|项)(?:结果|信号)?结合", core)
        or re.search(r"^多(?:种|项).{0,12}需结合临床", core)
        or re.search(r"(?:但|且|同时)(?:均|也|同样)?[，,](?:但|且|同时)", core)
        or re.search(r"等(?:但|且|同时|结合)", core)
        or re.search(r"(?:但|且|同时)(?:均|也|同样|多个|多项)$", core)
        or re.search(r"(?:但|且|同时)(?:均|也|同样)[，,]", core)
        or re.search(r"的(?:但|且|同时)", core)
        or re.search(r"(?:且|同时)?(?:多个|多项)(?:包括|如)", core)
    )


def _remove_non_display_clauses(value: str) -> str:
    normalized = value
    patterns = (
        r"[，,]?仅作为检出信号",
        r"(?:虽然)?(?:肠道)?菌群多样性尚可[，,]?但",
        r"(?:且|同时)?(?:多项)?(?:疾病)?(?:风险)?(?:模型)?分值较高(?:（[^）]*）)?[，,]?",
        r"[，,]?但?本次检测未提供参考范围，无法判断其丰度是否异常",
        r"[，,]?但?均未提供参考范围",
        r"[，,]?但?均无参考范围(?:，仅作检出描述)?",
        r"[，,]?但?未提供参考范围",
        r"[，,]?但?检测方未标(?:记|示)(?:为)?异常",
        r"[，,]?但?(?:该菌在)?(?:此次|本次)检测中未标(?:注|记)(?:为)?异常",
        r"[，,]?但?未标(?:注|记)(?:为)?异常",
        r"[，,]?但?未见异常标记(?:，仅提示检出)?",
        r"[，,]?但?均未见异常标记",
        r"[，,]?但?均无异常标记",
        r"[，,]?(?:但)?(?:均)?无(?:检测方)?异常标记",
        r"[，,]?其余菌种均未标记异常",
        r"[，,]?(?:(?:但)?(?:均)?|但本报告)未提供等级(?:判定)?(?:[，,]不判断风险)?",
        r"[，,]?(?:但)?(?:均)?无(?:检测方)?(?:风险)?等级(?:判定)?",
        r"[，,]?(?:但)?(?:均)?未提供风险等级(?:判定)?",
        r"[，,]?(?:但)?(?:均)?未提供异常(?:标记)?或高低(?:判断|判定)",
        r"[，,]?(?:但)?未具体说明(?:菌种|细菌|真菌)",
        r"[，,]?未显示异常(?:标记)?",
        r"[，,]?(?:因此)?不判断(?:个人)?风险(?:等级)?",
        r"[，,]?未提供分值或等级(?:判定)?",
        r"[，,]?但本次检测中未提供该疾病的风险分值或等级",
        r"[，,]?且具体关联尚不明确",
        r"[，,]?(?:仍|均)?需结合临床(?:综合)?(?:评估|关注)?",
        r"[，,]?(?:所有|全部)?检出(?:结果|信号)?(?:均)?需结合个体情况",
        r"[，,]?但未检测到疾病风险等级",
        r"[，,]?但本次检测仅显示[^。；;]*?，未(?:直接)?提示[^。；;]*?风险",
        r"[，,]?但本病例未进行相关评估",
        r"[，,]?未提供[^。；;，,]*?相关临床表现",
        r"[，,]?可能与菌群构成有关",
        r"[，,]?(?:因此|故)该分值[^。；;]*",
        r"[，,]?该分值不(?:构成|代表)[^。；;]*",
        r"[，,]?该分值仅[^。；;]*",
        r"[，,]?均仅表示模型分值",
    )
    normalized = normalized.replace(
        "原报告疾病模型包括",
        "原报告列出的疾病关联项目包括",
    )
    for pattern in patterns:
        normalized = re.sub(pattern, "", normalized)
    return normalized.replace("白名单内", "")


def _normalize_health_level(value: str) -> str:
    normalized = "".join(value.split())
    aliases = {
        "健康": "较低关注",
        "正常": "较低关注",
        "良好": "较低关注",
        "中等": "中度关注",
        "普通": "中度关注",
        "一般": "中度关注",
        "关注": "重点关注",
        "需要关注": "重点关注",
        "需关注": "重点关注",
        "需改善": "重点关注",
    }
    return aliases.get(normalized, normalized)


def _strip_code_fence(value: str) -> str:
    normalized = value.strip()
    normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*```$", "", normalized)
    return normalized.strip()


_TARGET_ALIASES = {
    "aspergillus": ("曲霉",),
    "candida": ("念珠菌",),
    "fusobacterium nucleatum": ("具核梭杆菌",),
    "bacteroides vulgatus": ("普通拟杆菌",),
    "bacteroides fragilis": ("脆弱拟杆菌",),
    "睡眠障碍": ("sleep disorder", "sleep disorders"),
    "结直肠癌": ("colorectal cancer", "colon cancer", "rectal cancer"),
    "溃疡性结肠炎": ("ulcerative colitis",),
    "硬化性胆管炎": ("sclerosing cholangitis",),
    "系统性红斑狼疮": ("systemic lupus erythematosus", "sle"),
    "慢性肾病": ("chronic kidney disease", "ckd"),
    "ii型糖尿病": ("2型糖尿病", "type 2 diabetes", "t2d", "t2dm"),
    "克罗恩病": ("crohn disease", "crohn's disease"),
    "冠心病": ("coronary heart disease", "coronary artery disease"),
    "糖化血红蛋白": ("hemoglobin a1c", "hba1c", "glycated hemoglobin"),
    "腹胀和排气": ("abdominal bloating", "bloating", "flatulence"),
    "胰岛素抵抗": ("insulin resistance",),
    "非酒精性脂肪肝": (
        "nonalcoholic fatty liver disease",
        "non-alcoholic fatty liver disease",
        "fatty liver disease",
        "nafld",
        "metabolic dysfunction-associated steatotic liver disease",
        "masld",
        "代谢相关脂肪性肝病",
    ),
    "肝硬化": ("liver cirrhosis", "cirrhosis"),
    "高尿酸血症": ("hyperuricemia", "hyperuricaemia"),
    "阿尔兹海默症": (
        "阿尔茨海默病",
        "阿尔茨海默症",
        "alzheimer disease",
        "alzheimer's disease",
    ),
    "帕金森病": ("parkinson disease", "parkinson's disease"),
    "癫痫": ("epilepsy",),
    "多发性硬化症": ("multiple sclerosis",),
    "类风湿性关节炎": ("rheumatoid arthritis",),
    "强直性脊柱炎": ("ankylosing spondylitis",),
    "甲状腺癌": ("thyroid cancer", "thyroid carcinoma"),
    "焦虑症": ("anxiety disorder", "anxiety"),
    "精神分裂症": ("schizophrenia",),
    "腹泻型肠易激综合征": (
        "diarrhea-predominant irritable bowel syndrome",
        "diarrhoea-predominant irritable bowel syndrome",
        "ibs-d",
    ),
    "高脂血症": ("hyperlipidemia", "hyperlipidaemia", "dyslipidemia"),
    "高血压": ("hypertension", "high blood pressure"),
    "自身免疫性肝炎": ("autoimmune hepatitis",),
    "肾结石": ("kidney stone", "kidney stones", "nephrolithiasis"),
    "胃癌": ("gastric cancer", "stomach cancer"),
    "肝癌": ("liver cancer", "hepatocellular carcinoma", "hcc"),
    "肥胖": ("obesity",),
    "抑郁症": ("depression", "depressive disorder"),
    "炎症性肠病": ("inflammatory bowel disease", "ibd"),
    "便秘": ("constipation",),
    "沙门菌病": (
        "沙门氏菌病",
        "salmonellosis",
        "salmonella infection",
        "肠热病",
        "肠热症",
        "enteric fever",
        "胃肠炎",
        "gastroenteritis",
    ),
    "沙门氏菌病": ("沙门菌病", "salmonellosis", "salmonella infection"),
    "肠热病": ("肠热症", "enteric fever", "typhoid fever"),
    "肠热症": ("肠热病", "enteric fever", "typhoid fever"),
    "胃肠炎": ("gastroenteritis",),
    "空腹血糖异常": ("impaired fasting glucose",),
    "腺瘤": ("colorectal adenoma", "intestinal adenoma", "adenoma"),
    "健康成人膳食调护": ("healthy adult diet", "healthy diet", "nutrition"),
    "健康成人运动调护": ("physical activity", "exercise", "healthy lifestyle"),
    "健康成人睡眠调护": ("healthy sleep", "sleep health", "sleep"),
    "健康成人排便习惯调护": ("bowel habits", "bowel habit", "defecation"),
    "血脂异常相关膳食调护": (
        "dyslipidemia diet",
        "dyslipidemia dietary lifestyle",
        "lipid lowering diet",
        "dietary management",
    ),
    "血脂异常相关运动调护": (
        "dyslipidemia physical activity",
        "dyslipidemia exercise",
        "physical activity intervention",
    ),
    "血糖异常相关膳食调护": (
        "hyperglycemia diet",
        "prediabetes diet",
        "dietary lifestyle intervention",
    ),
    "血糖异常相关运动调护": (
        "hyperglycemia physical activity",
        "prediabetes exercise",
        "physical activity intervention",
    ),
    "血压异常相关膳食调护": (
        "elevated blood pressure diet",
        "hypertension diet",
        "dietary lifestyle intervention",
    ),
    "血压异常相关运动调护": (
        "elevated blood pressure physical activity",
        "hypertension exercise",
        "physical activity intervention",
    ),
    "体重与体成分异常相关膳食调护": (
        "overweight diet",
        "obesity diet",
        "dietary lifestyle intervention",
    ),
    "体重与体成分异常相关运动调护": (
        "overweight physical activity",
        "obesity exercise",
        "physical activity intervention",
    ),
}


_INTERNAL_TERMS = (
    "AI生成",
    "AI归纳",
    "大模型",
    "固定规则",
    "系统规则",
    "系统执行",
    "模板生成",
    "后台流程",
    "JSON存储",
    "P0",
    "P1",
    "P2",
    "P3",
    "panel_",
    "disease-",
    "taxon-",
    "evidence-",
    "potential致病",
    "检出不等于感染",
    "EviMed",
    "白名单",
    "证据绑定",
    "已绑定",
    "表格逐项",
    "对应行",
    "证据编号",
    "gmwi",
    "gmhi",
    "diversity",
    "species_count",
    "fb_ratio",
    "be_ratio",
    "pathogenic_potential",
    "sourceSummary",
    "source-report",
    "status:",
    "用户上传",
    "上传文件",
    "上传的PDF",
    "PDF文件",
    "图片文件",
    "文件名",
    "解析结果",
    "解析过程",
    "模型生成",
    "菌群多样性",
    "肠道菌群多样性",
    "肠道微生物健康指数",
    "肠道代谢能力",
    "肠道合成能力",
)

_PROHIBITED_RECOMMENDATIONS = (
    "益生菌",
    "益生元",
    "保健品",
    "补充剂",
    "胶囊",
    "片剂",
    "处方药",
    "用药",
    "服药",
    "剂量",
    "疗程",
    "菌群移植",
    "FMT",
    "检查",
    "检测",
    "量表",
    "自我评估",
    "评估排便",
    "监测",
    "复查",
    "筛查",
    "就诊",
    "就医",
    "咨询医生",
    "毒素滞留",
    "毒素在肠道内滞留",
    "感染风险",
)

_SOURCE_LEVEL_TERMS = (
    "升高",
    "增高",
    "偏高",
    "降低",
    "偏低",
    "减少",
    "异常",
    "超出",
    "低于",
    "高于",
    "风险",
    "阳性",
    "阴性",
)

_INFERRED_LEVEL_TERMS = (
    "升高",
    "增高",
    "偏高",
    "显著高",
    "降低",
    "偏低",
    "显著低",
    "正常",
    "阈值",
    "超标",
    "不足",
    "过多",
    "丰度低",
    "丰度较低",
    "丰度高",
    "丰度较高",
    "丰度极低",
    "优势菌",
    "优势菌种",
    "优势菌群",
    "主要优势菌",
)

_DISEASE_LEVEL_TERMS = (
    "高风险",
    "低风险",
    "风险升高",
    "风险增高",
    "风险降低",
    "偏高",
    "偏低",
)

_NEGATIVE_SYMPTOM_TERMS = (
    "无相关症状",
    "无明显症状",
    "没有症状",
    "未出现症状",
    "未见症状",
    "症状不相符",
    "缺乏相符症状",
    "不存在相符症状",
)

_UNSUPPORTED_ABSOLUTE_TERMS = (
    "被证实能预防",
    "已证实能预防",
    "可以预防",
    "能够预防",
    "一定能够",
    "确保改善",
    "保证改善",
    "远低于致病阈值",
    "低于致病阈值",
    "高于致病阈值",
    "无临床意义",
    "没有临床意义",
    "检测噪音",
    "检测误差",
    "不提示活动性感染",
    "排除活动性感染",
    "不存在活动性感染",
    "不支持感染",
    "未发生感染",
    "无需关注",
    "未见明显异常",
    "未发现异常",
    "未提示异常",
    "未显示异常",
    "无异常提示",
    "未明确异常",
    "未明确为感染",
    "未明确感染",
    "低丰度检出",
    "整体菌群结构与功能状态尚可",
    "微弱信号",
    "强信号",
    "感染风险",
    "不提示疾病",
    "实验室污染",
    "不作为诊断",
    "不能作为诊断",
    "不代表诊断",
    "不构成临床诊断",
    "不代表临床诊断",
)

_UNSUPPORTED_GENERAL_LEVEL_PATTERNS = (
    re.compile(r"(?:优势菌|优势菌种|优势菌群|主要优势)"),
    re.compile(r"(?:以|由)[^。；]{1,100}(?:为主|占主导)"),
    re.compile(
        r"(?:疾病)?模型[^。；]{0,80}(?:提示|显示)[^。；]{0,60}(?:相关信号|模型信号)(?:较高|偏高|升高)"
    ),
    re.compile(
        r"(?=.*(?:疾病)?模型)(?=.*(?:提示|表明).{0,40}风险)(?=.*(?:较高|偏高|升高|增高))"
    ),
    re.compile(r"(?=.*(?:疾病)?模型)(?=.*(?:提示|表明).{0,30}(?:需要关注|需关注))"),
    re.compile(
        r"(?:模型|分值|评分)[^，,。；;]{0,12}(?:较高|偏高|偏低|高风险|低风险|风险升高|风险增高)"
    ),
    re.compile(r"(?:模型|分值|评分)[^，,。；;]{0,4}(?:为)?高(?:水平)?"),
    re.compile(
        r"(?:较高|偏高|高风险|风险升高|风险增高)[^，,。；;]{0,80}(?:模型|分值|评分)"
    ),
    re.compile(r"(?:疾病)?模型[^，,。；;]{0,80}(?:提示|表明)[^，,。；;]{0,30}风险"),
    re.compile(
        r"(?:菌群|丰度|指数|比值)[^，,。；;]{0,16}(?:偏高|偏低|升高|降低|高于|低于正常)"
    ),
    re.compile(r"(?:菌种|菌属|菌群|丰度|占比|比例)[^，,。；;]{0,24}(?:偏|较)(?:高|低)"),
    re.compile(r"(?:菌种|菌群|信号)[^，,。；;]{0,24}(?:低水平|高水平)"),
    re.compile(r"(?:菌种|菌群|信号|检出)[^，,。；;]{0,24}(?:低丰度|高丰度)"),
    re.compile(r"(?:多为|均为)[^，,。；;]{0,12}(?:低丰度|高丰度)"),
    re.compile(r"多(?:种|项)[^，,。；;]{0,12}(?:低丰度|高丰度)"),
    re.compile(r"(?:少量|少数)[^，,。；;]{0,12}(?:致病菌|菌种|菌群|信号)"),
    re.compile(r"(?:多样性|指数|比值).{0,80}(?:尚可|良好|一般|正常|异常)"),
    re.compile(r"(?:疾病)?模型.{0,120}(?:需要关注|需关注)"),
    re.compile(r"(?:多样性|指数|比值)[^，,。；;]{0,16}(?:接近零|接近0)"),
)

_MISSING_DATA_FILLER_TERMS = (
    "无相关临床资料",
    "无相关临床检验结果",
    "无相关实验室检查结果",
    "无临床诊断信息",
    "缺少临床资料",
    "缺乏",
    "未提供临床资料",
    "未提供风险等级",
    "未提供异常",
    "未提供各菌种",
    "以上均为检出状态描述",
    "资料不足",
    "信息不足",
    "无法判断",
    "不能判断",
    "未提供",
    "未标注参考范围",
    "目前无",
    "缺少",
    "无进一步证据",
    "无临床关联证据",
    "意义有限",
    "具体意义有限",
    "无免疫抑制",
    "无相关病史记录",
    "未达异常水平",
    "未直接涉及致病性",
    "无已知人体致病性",
    "无异常判定",
    "无异常标注",
    "无参考范围",
    "意义不明",
)

_UNSUPPORTED_UNCITED_TERMS = (
    "文献",
    "研究",
    "指南",
    "相关",
    "导致",
    "风险",
    "感染",
    "疾病",
    "临床意义",
    "病原体",
    "共生",
    "定植",
    "环境",
    "食物",
    "饮食来源",
    "口腔",
    "皮肤",
    "常见菌",
)
