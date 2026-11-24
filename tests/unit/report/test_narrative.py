import asyncio
import re

import pytest

from modules.report.narrative import supplement_common_examinations



from modules.report.clinical import build_abnormal_microbes, build_source_diseases
from modules.report.condition_extraction import (
    source_derived_conditions as _source_derived_conditions,
)
from modules.report.evidence import _public_disease_claim
from modules.report.narrative import (
    EvidenceInterpretationDraft,
    NarrativeDraft,
    NarrativeError,
    _bounded_display_text,
    _condition_is_generic_or_interventional,
    _executive_hospital_fact,
    _executive_summary_is_complete,
    _executive_summary_matches_completed_sections,
    _extractive_executive_summary,
    _extractive_health_level_summary,
    _extractive_health_profile,
    _extractive_microbe_summary,
    _fallback_attention_directions,
    _generate_retry_payloads,
    _health_profile_is_complete,
    _is_boundary_boilerplate,
    _merge_attention_directions,
    _merge_missing_narrative,
    _microbe_has_public_narrative_evidence,
    _missing_narrative_fields,
    _narrative_retry_batches,
    _recommended_examination_candidates,
    _retry_weak_relation_claims,
    _safe_extractive_executive_summary,
    _sanitize_disease_narrative,
    _sanitize_disease_patient_suggestion,
    _sanitize_microbe_narrative,
    _source_fact_disease_narrative,
    _source_fact_disease_suggestion,
    _source_fact_microbe_conclusion,
    _source_fact_microbe_narrative,
    _source_phrase_supports_evaluation,
    _strip_evidence_title_prefix,
    _study_design,
    _validated_evidence_interpretation,
    apply_draft,
    apply_extractive_health_fallback,
    ensure_source_fact_row_narratives,
    generate_narrative,
    interpret_evidence,
    repair_disease_evidence_claims,
    validate_draft,
)


@pytest.mark.parametrize(
    "override, expected",
    [
        ({}, "direct"),
        ({"title": "Otherbacter example in bowel disease"}, "not_applicable"),
        ({"summary": "Examplebacter novus has lower levels in blood samples of patients."}, "not_applicable"),
        ({"summary": "Examplebacter novus has lower levels in stool samples of mice."}, "not_applicable"),
        ({"summary": "Examplebacter novus was measured in stool samples of patients."}, "not_applicable"),
        ({"summary": "Toxigenic Examplebacter novus has lower levels in stool samples of patients."}, "not_applicable"),
        ({"summary": "Examplebacter novus has higher levels in stool samples of patients."}, "not_applicable"),
    ],
)
def test_exact_discovery_alignment_does_not_require_patient_diagnosis(override, expected):
    record = EvidenceRecord(
        evidenceKey="novel-discovery", category="literature", source="source",
        title=override.get("title", "Examplebacter novus in bowel disease"),
        summary=override.get("summary", "Patients had lower levels of Examplebacter novus in stool and mucosal samples compared with controls."),
        targets=["Examplebacter novus / 示例菌"], intents=["microbe_discovery"],
    )
    interpretation = _validated_evidence_interpretation(
        EvidenceInterpretationDraft(
            number=1, claim="Examplebacter novus在肠病人群中的丰度降低。",
            direction="decreased", taxonAlignment="species", sampleAlignment="stool",
            targetAlignment="not_applicable", patientApplicability="not_applicable",
        ), record,
    )
    assert interpretation is not None
    assert interpretation.target_alignment == expected
    assert interpretation.patient_applicability == (
        "contextual" if expected == "direct" else "not_applicable"
    )


def test_merge_attention_directions_preserves_model_text_and_fills_distinct_topics():
    model = HealthRecommendation(
        category="diet",
        topic="心血管与代谢健康",
        priority="重点关注",
        content="建议关注长期心血管与代谢风险的变化。",
        evidenceRefs=[1],
    )
    merged = _merge_attention_directions(
        [
            model,
            HealthRecommendation(
                category="diet",
                topic="糖脂代谢",
                priority="重点关注",
                content="糖脂代谢整体表现值得关注。",
            ),
            HealthRecommendation(
                category="general",
                topic="肠道微生态",
                priority="建议关注",
                content="肠道菌群结构及相关功能整体表现值得关注。",
            ),
            HealthRecommendation(
                category="general",
                topic="肠道屏障与免疫调节",
                priority="建议关注",
                content="肠道屏障与免疫调节的整体表现值得关注。",
            ),
            HealthRecommendation(
                category="general",
                topic="高脂血症",
                priority="重点关注",
                content="高脂血症可能增加长期心血管风险。",
            ),
            HealthRecommendation(
                category="general",
                topic="多系统慢性疾病风险关注",
                priority="建议关注",
                content="当前资料涉及多个慢性疾病风险方向。",
            ),
        ]
    )

    assert 3 <= len(merged) <= 5
    assert merged[0] == model
    assert sum("\u4ee3\u8c22" in item.topic for item in merged) == 1
    assert all("高脂血症" != item.topic for item in merged)
    assert all("多系统" not in item.topic for item in merged)
from modules.report.schemas import (
    AbnormalMicrobeItem,
    AbnormalMicrobesSection,
    ClinicalResult,
    DiseaseRiskItem,
    DiseaseRisksSection,
    EnterotypeResult,
    EvidenceRecord,
    EvidenceRelationBinding,
    HealthAssessment,
    HealthAssessmentAbnormalIndicator,
    HealthAssessmentDimension,
    HealthManagementSection,
    HealthRecommendation,
    HospitalDataInput,
    ParsedDisease,
    ParsedMicrobiomeReport,
    ParsedTaxon,
    RiskAssociation,
    SubjectInfo,
    SuggestedEvaluation,
)


def _common_check_disease(identifier="new-disease"):
    return DiseaseRiskItem(
        diseaseId=identifier, name="未配置的健康关注项", sourceType="source_report",
        sourceResult="中风险", relatedMicrobes=["来源异常菌"],
        associationEvidenceRefs=[1], medicalInterpretation="存在相应健康关注方向。",
    )


def test_exact_relation_outcome_is_not_rejected_as_a_duplicate_organism():
    from modules.report.narrative import _validated_evidence_conditions
    record = EvidenceRecord(
        evidenceKey="novel", category="literature", source="Journal",
        title="Novelus exampleus abundance in ulcerative colitis",
        summary="Human stool abundance of Novelus exampleus was lower in ulcerative colitis.",
        targets=["Novelus exampleus / 新测试菌 / 溃疡性结肠炎"],
    )
    draft = EvidenceInterpretationDraft(
        number=1, claim="溃疡性结肠炎患者中新测试菌丰度降低。",
        conditions=[
            {"name": "溃疡性结肠炎", "searchTerm": "ulcerative colitis",
             "sourcePhrase": "ulcerative colitis", "relationType": "epidemiologic_association"},
            {"name": "新测试菌", "searchTerm": "Novelus exampleus",
             "sourcePhrase": "Novelus exampleus", "relationType": "epidemiologic_association"},
        ],
    )
    result = _validated_evidence_conditions(draft, record)
    assert [condition.name for condition in result] == ["溃疡性结肠炎"]


def _common_check_row(identifier="new-disease", name="尿常规检查", **values):
    return {"diseaseId": identifier, "examination": {
        "name": name, "reason": "用于评估尿液相关异常。", "purpose": "评估",
        "hospitalCommon": True, "alreadyAvailable": False, "applicable": True,
        **values,
    }}


@pytest.mark.asyncio
async def test_common_checks_repairs_only_rejected_rows_once():
    calls = []
    class Client:
        async def generate(self, payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"items": [
                    _common_check_row("valid"),
                    _common_check_row("repair", name="粪便常规+隐血"),
                    {"diseaseId": "none", "examination": None},
                ]}
            assert [d["diseaseId"] for d in payload["diseases"]] == ["repair"]
            assert payload["validationErrors"] == {"repair": "invasive_or_multiple"}
            return {"items": [_common_check_row("repair", name="粪便常规")]}
    diseases = DiseaseRisksSection(items=[_common_check_disease(i) for i in ("valid", "repair", "none")])
    assert await supplement_common_examinations(
        Client(), diseases=diseases, hospital_data=[], subject=SubjectInfo(),
    ) == []
    assert len(calls) == 2
    assert diseases.items[0].suggested_evaluations[0].name == "尿常规检查"
    assert diseases.items[1].suggested_evaluations[0].name == "粪便常规"
    assert diseases.items[2].suggested_evaluations == []


@pytest.mark.asyncio
async def test_common_checks_failed_repair_keeps_successful_first_batch():
    calls = 0
    class Client:
        async def generate(self, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError()
            return {"items": [_common_check_row("valid")]}
    diseases = DiseaseRisksSection(items=[_common_check_disease(i) for i in ("valid", "missing")])
    assert await supplement_common_examinations(
        Client(), diseases=diseases, hospital_data=[], subject=SubjectInfo(),
    ) == ["missing"]
    assert len(diseases.items[0].suggested_evaluations) == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_common_checks_one_batch_preserves_existing_and_does_not_invent_refs():
    empty = [_common_check_disease(str(index)) for index in range(23)]
    existing = _common_check_disease("existing")
    existing.suggested_evaluations = [SuggestedEvaluation(
        name="既有检查", rationale="已有用途", evidenceRefs=[2],
    )]
    before = existing.model_dump()
    calls = []

    class Client:
        async def generate(self, payload):
            calls.append(payload)
            assert len(payload["diseases"]) == 23
            return {"items": [
                _common_check_row(str(index), evidenceRefs=[999]) for index in range(23)
            ]}

    diseases = DiseaseRisksSection(items=[*empty, existing])
    assert await supplement_common_examinations(
        Client(), diseases=diseases, hospital_data=[], subject=SubjectInfo(age=31, sex="男"),
        session_context="已记录排尿不适",
    ) == []
    assert len(calls) == 1
    assert calls[0]["sessionContext"] == "已记录排尿不适"
    assert existing.model_dump() == before
    for disease in empty:
        exam = disease.suggested_evaluations[0]
        assert exam.evidence_refs == []
        assert exam.recommendation_source == "model"
        assert disease.association_evidence_refs == [1]
    candidates = _recommended_examination_candidates(diseases, evidence_numbers={1, 2})
    assert any(item["modelSuggested"] and item["evidenceRefs"] == [] for item in candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    _common_check_row(name="布里斯托大便性状量表"),
    _common_check_row(name="甲状腺细针穿刺活检"),
    _common_check_row(name="尿常规检查、血常规检查"),
    _common_check_row(hospitalCommon=False),
    _common_check_row(applicable=False),
    _common_check_row(alreadyAvailable=True),
    _common_check_row(hospitalCommon="true"),
    _common_check_row(purpose="治疗"),
    _common_check_row("unknown-id"),
    {"diseaseId": [], "examination": None},
])
async def test_common_checks_reject_invalid_or_unbound_results(row):
    class Client:
        async def generate(self, payload):
            return {"items": [row]}
    disease = _common_check_disease()
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[disease]), hospital_data=[],
        subject=SubjectInfo(),
    ) == [disease.disease_id]
    assert not disease.suggested_evaluations


@pytest.mark.asyncio
async def test_common_checks_exclude_completed_alias_in_all_hospital_data():
    class Client:
        async def generate(self, payload):
            return {"items": [_common_check_row(name="糖化血红蛋白检测")]}
    disease = _common_check_disease()
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[disease]), subject=SubjectInfo(),
        hospital_data=[HospitalDataInput(dataType="INSPECTION", itemName="HbA1c", testResultValue=5.1)],
    ) == [disease.disease_id]
    assert disease.suggested_evaluations == []


@pytest.mark.asyncio
async def test_common_checks_no_target_no_call_and_explicit_null_is_not_failure():
    class Client:
        async def generate(self, payload):
            return {"items": [{"diseaseId": "new-disease", "examination": None}]}
    assert await supplement_common_examinations(
        None, diseases=DiseaseRisksSection(), hospital_data=[], subject=SubjectInfo(),
    ) == []
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[_common_check_disease()]),
        hospital_data=[], subject=SubjectInfo(),
    ) == []


@pytest.mark.asyncio
async def test_common_check_normalizes_model_expanded_purpose_without_new_claim():
    class Client:
        async def generate(self, payload):
            return {"items": [_common_check_row(purpose="评估尿液相关异常")]}
    disease = _common_check_disease()
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[disease]),
        hospital_data=[], subject=SubjectInfo(),
    ) == []
    assert disease.suggested_evaluations[0].purpose == "评估"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["尿沉渣镜检", "尿渗透压", "合成字典外项目"])
async def test_common_check_classification_does_not_require_name_dictionary(name):
    from modules.report.clinical import is_hospital_common_examination

    assert not is_hospital_common_examination(name)
    calls = []
    class Client:
        async def generate(self, payload):
            calls.append(payload)
            return {"items": [_common_check_row(name=name, category="laboratory", evidenceRefs=[999])]}
    disease = _common_check_disease()
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[disease]),
        hospital_data=[], subject=SubjectInfo(),
    ) == []
    assert len(calls) == 1
    exam = disease.suggested_evaluations[0]
    assert exam.name == name and exam.evidence_refs == []
    assert exam.recommendation_source == "model"
    assert "category" not in exam.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("values,reason", [
    ({"category": "endoscopy"}, "invalid_shape"),
    ({"category": True}, "invalid_shape"),
    ({"name": "合成字典外项目", "category": None}, "not_common"),
    ({"name": "症状自评问卷"}, "not_common"),
    ({"name": "合成研究专用分析"}, "not_common"),
    ({"name": "喉镜"}, "invasive_or_multiple"),
    ({"name": "电子喉镜"}, "invasive_or_multiple"),
    ({"name": "flexible laryngoscopy"}, "invasive_or_multiple"),
    ({"name": "甲状腺细针穿刺活检"}, "invasive_or_multiple"),
    ({"name": "尿沉渣镜检+尿常规"}, "invasive_or_multiple"),
    ({"alreadyAvailable": True}, "already_available"),
    ({"hospitalCommon": False}, "not_applicable"),
    ({"applicable": False}, "not_applicable"),
])
async def test_common_check_category_cannot_bypass_exclusions(values, reason):
    calls = []
    class Client:
        async def generate(self, payload):
            calls.append(payload)
            if len(calls) == 2:
                assert payload["validationErrors"] == {"new-disease": reason}
            return {"items": [_common_check_row(**{"category": "laboratory", **values})]}
    disease = _common_check_disease()
    assert await supplement_common_examinations(
        Client(), diseases=DiseaseRisksSection(items=[disease]),
        hospital_data=[], subject=SubjectInfo(),
    ) == [disease.disease_id]
    assert len(calls) == 2
    assert not disease.suggested_evaluations


@pytest.mark.asyncio
async def test_common_check_name_repair_receives_reason_and_preserves_other_results():
    calls = []
    class Client:
        async def generate(self, payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"items": [_common_check_row("valid"),
                    _common_check_row("repair", name="尿沉渣镜检")]}
            assert payload["validationErrors"] == {"repair": "not_common"}
            assert [d["diseaseId"] for d in payload["diseases"]] == ["repair"]
            return {"items": [_common_check_row("repair", name="尿沉渣镜检", category="laboratory")]}
    diseases = DiseaseRisksSection(items=[_common_check_disease(i) for i in ("valid", "repair")])
    assert await supplement_common_examinations(
        Client(), diseases=diseases, hospital_data=[], subject=SubjectInfo(),
    ) == []
    assert len(calls) == 2
    assert [d.suggested_evaluations[0].name for d in diseases.items] == ["尿常规检查", "尿沉渣镜检"]


def test_source_disease_without_human_evidence_hides_provider_model_index():
    disease = DiseaseRiskItem(
        diseaseId="disease-constipation",
        name="便秘",
        sourceType="source_report",
        itemType="disease",
        modelIndex="55.56",
    )

    text = _source_fact_disease_narrative(disease)

    assert text == ""


def test_disease_suggestion_is_concrete_and_has_no_generic_filler():
    disease = DiseaseRiskItem(
        diseaseId="disease-t2dm",
        name="II型糖尿病",
        sourceType="source_report",
        associationEvidenceRefs=[1],
        suggestedEvaluations=[
            SuggestedEvaluation(
                name="糖化血红蛋白测定",
                rationale="用于评估血糖状态。",
                purpose="评估",
                evidenceRefs=[1],
            )
        ],
    )

    assert _sanitize_disease_patient_suggestion(
        (
            "然而，关注II型糖尿病相关的血糖变化，"
            "可通过糖化血红蛋白测定进一步评估。"
        ),
        disease=disease,
        has_session_context=False,
    ) == (
        "关注2型糖尿病相关的血糖变化，"
        "可通过糖化血红蛋白测定进一步评估。"
    )
    assert _sanitize_disease_patient_suggestion(
        (
            "可以优先关注II型糖尿病相关健康表现，"
            "必要时由专业人员进一步评估。"
        ),
        disease=disease,
        has_session_context=False,
    ) is None


def test_disease_suggestion_cannot_expand_symptom_scale_to_disease_screening():
    disease = DiseaseRiskItem(
        diseaseId="disease-parkinson",
        name="帕金森",
        sourceType="source_report",
        associationEvidenceRefs=[1],
        suggestedEvaluations=[
            SuggestedEvaluation(
                name="医院焦虑抑郁量表",
                rationale="HADS用于帕金森病抑郁筛查和严重程度评估。",
                purpose="筛查",
                evidenceRefs=[1],
            )
        ],
    )

    assert _sanitize_disease_patient_suggestion(
        "可通过医院焦虑抑郁量表进一步筛查帕金森病相关情况。",
        disease=disease,
        has_session_context=False,
    ) is None
    # A candidate examination belongs only in the structured examination
    # field after the hospital-common gate; it must not leak into free text.
    assert _source_fact_disease_suggestion(disease) is None


def test_source_fact_disease_suggestion_uses_only_concrete_available_facts():
    disease = DiseaseRiskItem(
        diseaseId="disease-lipid",
        name="高血脂",
        sourceType="source_report",
        existingResults=[
            ClinicalResult(
                itemName="低密度脂蛋白胆固醇",
                result="3.70 mmol/L",
                status="偏高",
            )
        ],
    )
    assert _source_fact_disease_suggestion(disease) == (
        "重点关注高脂血症相关的低密度脂蛋白胆固醇变化。"
    )

    disease.existing_results = []
    disease.suggested_evaluations = []
    assert _source_fact_disease_suggestion(disease) is None


def test_source_disease_description_keeps_one_provenance_labeled_sentence():
    disease = DiseaseRiskItem(
        diseaseId="disease-ibd",
        name="炎症性肠道疾病",
        sourceType="source_report",
        itemType="disease",
        sourceDescription=(
            "炎症性肠道疾病是一类慢性非特异性肠黏膜炎症性疾病。"
            "后续长段落不应全部复制到报告。"
        ),
    )

    assert _source_fact_disease_narrative(disease) == ""


def test_source_description_survives_a_rejected_prior_source_note():
    disease = DiseaseRiskItem(
        diseaseId="disease-diarrhea",
        name="腹泻",
        sourceType="source_report",
        sourceDescription="腹泻通常指排便频次增多且粪便性状变稀。",
        # The first source-fact pass stores this value on the row.  A later
        # validation pass rejects provenance boilerplate as free-form model
        # prose, but must still recover the internal source description.
        medicalInterpretation=(
            "原菌群报告疾病说明："
            "腹泻通常指排便频次增多且粪便性状变稀。"
        ),
    )

    assert _source_fact_disease_narrative(disease) == ""


def test_disease_narrative_replaces_tautology_with_evidence_bound_rationale():
    disease = DiseaseRiskItem(
        diseaseId="disease-constipation",
        name="便秘",
        sourceType="source_report",
        itemType="disease",
        modelIndex="55.56",
        associationEvidenceRefs=[1],
        medicalInterpretation="便秘相关临床特征。",
    )

    text = _source_fact_disease_narrative(disease)

    assert "本次检测呈现的相关菌群特征" in text
    assert "适当关注便秘" in text


def test_disease_narrative_prefers_specific_human_gut_relation_over_stock_phrase():
    disease = DiseaseRiskItem(
        diseaseId="disease-stones",
        name="肾结石",
        sourceType="evimed_association",
        sourceResult="草酸杆菌：丰度低 0.0007%",
        relatedMicrobes=["草酸杆菌"],
        associationEvidenceRefs=[1],
    )
    microbes = AbnormalMicrobesSection(
        items=[
            AbnormalMicrobeItem(
                microbeId="taxon-oxalobacter",
                name="草酸杆菌",
                scientificName="Oxalobacter formigenes",
                category="beneficial_abnormal",
                sourceFinding={"status": "丰度低", "value": "0.0007", "unit": "%"},
                diseaseAndRisks=[
                    RiskAssociation(
                        name="肾结石",
                        description="草酸杆菌丰度降低可能与肾结石风险增加相关。",
                        evidenceRefs=[1],
                        patientApplicability="contextual",
                        sampleAlignment="stool",
                    )
                ],
            )
        ]
    )

    text = _source_fact_disease_narrative(disease, microbes=microbes)

    assert "草酸杆菌丰度降低可能与肾结石风险增加相关" in text
    assert "可以适当关注" not in text


def test_unresolved_source_disease_row_keeps_an_explicit_limitation_note():
    disease = DiseaseRiskItem(
        diseaseId="disease-ms",
        name="多发性硬化",
        sourceType="source_report",
    )

    text = _source_fact_disease_narrative(disease)

    assert text == ""


def test_source_fact_rows_are_not_blank_when_final_narrative_times_out():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-1",
        name="示例菌",
        scientificName="Example bacterium",
        category="other_abnormal",
        sourceFinding={"status": "低于参考范围", "value": "0.2", "unit": "%"},
    )
    disease = DiseaseRiskItem(
        diseaseId="disease-1",
        name="便秘",
        sourceType="source_report",
        modelIndex="55.56",
    )
    microbes = AbnormalMicrobesSection(items=[microbe])
    diseases = DiseaseRisksSection(items=[disease])

    ensure_source_fact_row_narratives(microbes=microbes, diseases=diseases)

    assert microbe.medical_interpretation == (
        "检测结果显示，示例菌为低于参考范围，检测值为0.2%。"
        "目前仅可确认该项检测变化，尚无足够的人体医学依据"
        "解释其与具体疾病或不良影响的关系。"
    )
    assert disease.medical_interpretation is None


def test_apply_draft_replaces_source_bound_disease_interpretation_after_validation():
    microbes, diseases = sections()
    disease = diseases.items[0]
    disease.medical_interpretation = "便秘是以排便困难或排便频率减少为特征的症候。"
    disease.related_content = disease.medical_interpretation
    disease.association_evidence_refs = [3]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": disease.disease_id,
                    "relatedContent": "某不相关研究结果被模型误写为便秘解读。",
                }
            ]
        }
    )

    apply_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        health=HealthManagementSection(),
        hospital_data=[],
    )

    assert disease.medical_interpretation == "某不相关研究结果被模型误写为便秘解读。"
    assert disease.association_evidence_refs == [3]


def test_recommended_examinations_only_use_canonical_evidence_backed_candidates():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendedExaminations": [
                {
                    "candidateId": "exam-1",
                    "hospitalCommon": True,
                    "reason": "结合本次结直肠健康关注项，可优先补充筛查信息。",
                    "name": "模型伪造名称",
                    "purpose": "确认",
                    "evidenceRefs": [999],
                },
                {
                    "candidateId": "unknown",
                    "reason": "建议补充一项不在候选集中的检查。",
                },
            ]
        }
    )

    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        recommended_examination_candidates={
            "exam-1": {
                "name": "粪便免疫化学试验",
                "purpose": "筛查",
                "evidenceRefs": [3],
            }
        },
    )

    assert len(validated.recommended_examinations) == 1
    examination = validated.recommended_examinations[0]
    assert examination.name == "粪便免疫化学试验"
    assert examination.purpose == "筛查"
    assert examination.evidence_refs == [3]

    health = HealthManagementSection()
    apply_draft(
        validated,
        microbes=microbes,
        diseases=diseases,
        health=health,
        hospital_data=[],
    )
    assert health.recommended_examinations[0].name == "粪便免疫化学试验"


def test_recommended_examination_requires_matching_patient_indication_when_known():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendedExaminations": [
                {
                    "candidateId": "exam-glucose",
                    "hospitalCommon": True,
                    "reason": "用于进一步了解血糖调节能力。",
                }
            ]
        }
    )

    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        recommended_examination_candidates={
            "exam-glucose": {
                "name": "75克口服葡萄糖耐量试验",
                "purpose": "确认",
                "evidenceRefs": [3],
                "patientIndication": False,
            }
        },
    )

    assert validated.recommended_examinations == []


def test_evaluation_source_phrase_must_match_the_examination_family():
    assert _source_phrase_supports_evaluation(
        "1-hour post-load plasma glucose",
        name="75克口服葡萄糖耐量试验1小时血浆葡萄糖",
    )
    assert not _source_phrase_supports_evaluation(
        "requires thoughtful evaluation",
        name="75克口服葡萄糖耐量试验",
    )


def test_recommended_examination_candidates_exclude_completed_or_uncited_checks():
    _microbes, diseases = sections()
    diseases.items[0].suggested_evaluations = [
        SuggestedEvaluation(
            name="结肠镜检查",
            purpose="确认",
            rationale="已经完成的检查",
            alreadyAvailable=True,
            evidenceRefs=[1],
        ),
        SuggestedEvaluation(
            name="无证据检查",
            purpose="评估",
            rationale="没有可公开引用的证据",
            evidenceRefs=[],
        ),
        SuggestedEvaluation(
            name="粪便免疫化学试验",
            purpose="筛查",
            rationale="用于补充结直肠筛查信息",
            evidenceRefs=[3, 99],
        ),
    ]

    candidates = _recommended_examination_candidates(
        diseases,
        evidence_numbers={3},
    )

    assert [item["name"] for item in candidates] == ["粪便免疫化学试验"]
    assert candidates[0]["evidenceRefs"] == [3]


def test_clinical_diagnosis_without_status_can_support_followup_candidate():
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="clinical-fatty-liver",
                name="非酒精性脂肪肝",
                sourceType="clinical_context",
                existingResults=[
                    ClinicalResult(
                        itemName="腹部超声",
                        result="脂肪肝",
                    )
                ],
                suggestedEvaluations=[
                    SuggestedEvaluation(
                        name="肝脏瞬时弹性成像",
                        purpose="评估",
                        rationale="用于评估非酒精性脂肪肝的肝纤维化风险。",
                        evidenceRefs=[3],
                    )
                ],
            )
        ]
    )

    candidates = _recommended_examination_candidates(
        diseases,
        evidence_numbers={3},
    )

    assert candidates[0]["patientIndication"] is True


def test_recommended_examination_candidates_exclude_scales_and_self_records():
    _microbes, diseases = sections()
    diseases.items[0].suggested_evaluations = [
        SuggestedEvaluation(
            name="布里斯托大便性状量表",
            purpose="评估",
            rationale="用于自评排便性状。",
            evidenceRefs=[2],
        ),
        SuggestedEvaluation(
            name="粪便钙卫蛋白检测",
            purpose="评估",
            rationale="用于医院实验室评估肠道炎症。",
            evidenceRefs=[3],
        ),
    ]

    candidates = _recommended_examination_candidates(
        diseases,
        evidence_numbers={2, 3},
    )

    assert [item["name"] for item in candidates] == ["粪便钙卫蛋白检测"]


def test_extractive_health_level_summary_does_not_repeat_profile_details():
    microbes = AbnormalMicrobesSection(
        items=[
            AbnormalMicrobeItem(
                microbeId="taxon-salmonella",
                name="肠沙门菌",
                scientificName="Salmonella enterica",
                category="pathogenic_signal",
                sourceFinding={"status": "超标"},
            ),
            AbnormalMicrobeItem(
                microbeId="taxon-baumannii",
                name="鲍曼不动杆菌",
                scientificName="Acinetobacter baumannii",
                category="opportunistic_signal",
                sourceFinding={"status": "超标"},
            ),
        ],
        summary="",
    )
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="disease-constipation",
                name="便秘",
                sourceType="source_report",
            )
        ]
    )

    summary = _extractive_health_level_summary(
        enterotype="肠型I",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白",
                testResultValue="3.70",
                resultStatusName="偏高",
            )
        ],
    )

    assert summary == (
        "本次肠道健康关注程度主要基于病原相关菌异常、"
        "条件致病菌异常、原报告疾病或健康关联项目。"
    )
    assert "肠型I" not in summary
    assert "肠沙门菌" not in summary
    assert "低密度脂蛋白" not in summary
    assert "3.70" not in summary


def test_executive_summary_cannot_contradict_source_or_add_missing_advice():
    assert not _executive_summary_matches_completed_sections(
        "原报告提示ASD为中高风险。",
        recommendations=[],
        source_summary="ASD为中高风险。",
    )
    assert not _executive_summary_matches_completed_sections(
        "本次菌群呈现平衡状态，建议关注膳食结构。",
        recommendations=[],
        source_summary="肠菌健康指数为-0.04，II度失调。",
    )
    assert _executive_summary_matches_completed_sections(
        "本次健康管理结论重点关注肠道微生态影响。",
        recommendations=[],
        source_summary="肠菌健康指数为-0.04，II度失调。",
    )


def test_case_report_is_detected_from_journal_source_name():
    record = EvidenceRecord(
        evidence_key="case-report-source",
        category="literature",
        title="Atraumatic splenic rupture in Legionella pneumonia",
        source="Case reports in infectious diseases",
    )
    assert _study_design(record) == "病例报告"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (
            "Mucosal biopsies from adults were tested for Bacteroides fragilis.",
            "mucosa",
        ),
        (
            "Faecal samples from adults were tested for Salmonella.",
            "stool",
        ),
    ],
)
def test_evidence_interpretation_recovers_explicit_gut_specimen_alignment(
    summary, expected
):
    record = EvidenceRecord(
        evidence_key=f"explicit-{expected}",
        category="literature",
        title="Human microbiome cohort",
        source="Journal",
        summary=summary,
        targets=["Bacteroides fragilis" if expected == "mucosa" else "Salmonella"],
        intents=["microbe_disease_association"],
    )
    target = record.targets[0]
    interpretation = _validated_evidence_interpretation(
        EvidenceInterpretationDraft(
            number=1,
            claim=f"{target}与目标疾病相关。",
            targetAlignment="direct",
            taxonAlignment="species",
            sampleAlignment="unclear",
            patientApplicability="contextual",
        ),
        record,
    )

    assert interpretation is not None
    assert interpretation.sample_alignment == expected


def test_source_derived_condition_requires_explicit_source_relation():
    record = EvidenceRecord(
        evidence_key="botulism",
        category="guideline",
        title="Clinical guideline for botulism",
        source="Society",
        summary=("Botulism is caused by toxin produced by Clostridium botulinum."),
        targets=["Clostridium botulinum"],
    )
    conditions, claim = _source_derived_conditions(record)
    assert [item.name for item in conditions] == ["肉毒中毒"]
    assert claim is not None and "Clostridium botulinum" in claim

    unrelated = record.model_copy(
        update={"summary": "Botulism and Clostridium botulinum were indexed."}
    )
    assert _source_derived_conditions(unrelated) == ([], None)


def test_source_derived_condition_accepts_exact_taxon_infection_consensus():
    record = EvidenceRecord(
        evidence_key="mtb-infection-testing-consensus",
        category="guideline",
        title="结核分枝杆菌感染检测技术应用专家共识",
        source="中国防痨协会",
        summary=("结核分枝杆菌感染检测是诊断潜伏感染和辅助诊断结核病的重要手段。"),
        targets=["Mycobacterium tuberculosis / 结核分枝杆菌"],
    )

    conditions, claim = _source_derived_conditions(record)

    assert [condition.name for condition in conditions] == ["结核病"]
    assert conditions[0].relation_type == "epidemiologic_association"
    assert claim is not None and "Mycobacterium tuberculosis" in claim


def test_source_derived_condition_rejects_unrelated_ntm_serology_source():
    record = EvidenceRecord(
        evidence_key="fortuitum-tuberculosis-serology-source",
        category="literature",
        title=(
            "Comparison of Mycobacterium fortuitum antigen for serology in "
            "tuberculosis patients"
        ),
        source="Journal",
        summary="The antigen was evaluated in tuberculosis patients.",
        targets=["Mycobacterium fortuitum / 偶发分枝杆菌"],
    )

    assert _source_derived_conditions(record) == ([], None)


def test_source_derived_condition_recovers_explicit_ntm_disease_relation():
    record = EvidenceRecord(
        evidence_key="kansasii-ntm-disease",
        category="literature",
        title="Prevalence of Mycobacterium kansasii in clinical isolates",
        source="Journal",
        summary=(
            "Mycobacterium kansasii infection is one of the most common "
            "causes of non-tuberculosis mycobacterial (NTM) disease worldwide."
        ),
        targets=["Mycobacterium kansasii / 堪萨斯分枝杆菌"],
    )

    conditions, claim = _source_derived_conditions(record)

    assert conditions[0].name == "非结核分枝杆菌病"
    assert conditions[0].relation_type == "established_pathogen"
    assert claim is not None and "Mycobacterium kansasii" in claim


@pytest.mark.parametrize(
    ("title", "target", "expected"),
    (
        (
            "Pseudomonas aeruginosa pneumonia",
            "Pseudomonas aeruginosa / 铜绿假单胞菌",
            "肺炎",
        ),
        (
            "Fusobacterium nucleatum in colorectal cancer",
            "Fusobacterium nucleatum / 具核梭杆菌",
            "结直肠癌",
        ),
    ),
)
def test_concise_exact_title_is_only_used_as_relation_search_seed(
    title: str,
    target: str,
    expected: str,
):
    record = EvidenceRecord(
        evidenceKey=f"concise-seed-{expected}",
        category="literature",
        title=title,
        source="EviMed",
        targets=[target],
    )

    conditions, _claim = _source_derived_conditions(record)

    assert [condition.name for condition in conditions] == [expected]
    assert conditions[0].relation_type == "epidemiologic_association"


def test_source_derived_conditions_recover_explicit_human_pathogen_outcomes():
    perfringens = EvidenceRecord(
        evidence_key="perfringens-outcomes",
        category="literature",
        title="Virulence genes of Clostridium perfringens",
        source="Journal",
        summary=(
            "Clostridium perfringens causes human gas gangrene and food "
            "poisoning as well as clostridial myonecrosis."
        ),
        targets=["Clostridium perfringens / 产气荚膜梭菌"],
    )
    conditions, claim = _source_derived_conditions(perfringens)
    assert [item.name for item in conditions] == [
        "食物中毒",
        "气性坏疽",
        "梭菌性肌坏死",
    ]
    assert claim is not None and "Clostridium perfringens" in claim

    flexneri = EvidenceRecord(
        evidence_key="flexneri-outcome",
        category="literature",
        title="Shigella flexneri virulence",
        source="Journal",
        summary="Shigella flexneri is a human pathogen that causes bloody diarrhea.",
        targets=["Shigella flexneri / 福氏志贺菌"],
    )
    conditions, _claim = _source_derived_conditions(flexneri)
    assert [item.name for item in conditions] == ["血性腹泻"]

    chinese_source = EvidenceRecord(
        evidence_key="perfringens-chinese-source",
        category="literature",
        title="一起由产气荚膜梭菌引起的食物中毒",
        source="Journal",
        summary="调查证实该起食物中毒由产气荚膜梭菌引起。",
        targets=["Clostridium perfringens / 产气荚膜梭菌"],
    )
    conditions, claim = _source_derived_conditions(chinese_source)
    assert [item.name for item in conditions] == ["食物中毒"]
    assert claim is not None and "Clostridium perfringens" in claim

    fortuitum = EvidenceRecord(
        evidence_key="fortuitum-endocarditis",
        category="literature",
        title=("Prosthetic valve endocarditis due to Mycobacterium fortuitum"),
        source="Journal",
        targets=["Mycobacterium fortuitum / 偶发分枝杆菌"],
    )
    conditions, claim = _source_derived_conditions(fortuitum)
    assert [item.name for item in conditions] == ["心内膜炎"]
    assert claim is not None and "Mycobacterium fortuitum" in claim

    erysipelothrix = EvidenceRecord(
        evidence_key="erysipelothrix-erysipeloid",
        category="literature",
        title="Pathogenicity of Erysipelothrix rhusiopathiae",
        source="Journal",
        summary=(
            "Erysipelothrix rhusiopathiae is the causative agent of "
            "erysipeloid in humans."
        ),
        targets=["Erysipelothrix rhusiopathiae / 猪红斑丹毒丝菌"],
    )
    conditions, claim = _source_derived_conditions(erysipelothrix)
    assert [item.name for item in conditions] == ["类丹毒"]
    assert claim is not None and "Erysipelothrix rhusiopathiae" in claim

    cryptococcus = EvidenceRecord(
        evidence_key="cryptococcus-outcome",
        category="literature",
        title="Cryptococcus: from environmental saprophyte to global pathogen",
        source="Journal",
        summary=(
            "Cryptococcosis is an invasive fungal infection caused by species "
            "within the genus Cryptococcus."
        ),
        targets=["Cryptococcus / 隐球菌属"],
    )
    conditions, claim = _source_derived_conditions(cryptococcus)
    assert [item.name for item in conditions] == ["隐球菌病"]
    assert claim is not None and "Cryptococcus" in claim


def test_explicit_source_relation_is_only_a_search_seed():
    record = EvidenceRecord(
        evidence_key="botulism-without-model",
        category="guideline",
        title="Clinical guidelines for diagnosis and treatment of botulism",
        source="CDC",
        summary=("Botulism is caused by neurotoxin produced by Clostridium botulinum."),
        targets=["Clostridium botulinum / 肉毒梭菌"],
        microbeIds=["taxon-botulinum"],
        intents=["microbe_discovery"],
    )
    conditions, claim = _source_derived_conditions(record)
    assert [item.name for item in conditions] == ["肉毒中毒"]
    assert claim is not None
    assert record.interpretation is None


def test_evidence_meta_narration_is_hidden_from_public_prose():
    assert _is_boundary_boilerplate("该指南描述了便秘的评估。")
    assert _is_boundary_boilerplate("该文献讨论肠道菌群与疾病的关联。")
    assert _is_boundary_boilerplate("克罗恩病存在早期阶段定义。")


def test_population_resistance_and_foreign_taxon_are_not_conditions():
    base = {
        "source": "Human clinical evidence",
        "relation_type": "epidemiologic_association",
        "targets": ["Bacteroides fragilis / 脆弱拟杆菌"],
    }
    assert _condition_is_generic_or_interventional(
        name="选择性IgA缺乏症",
        search_term="selective IgA deficiency",
        **base,
    )
    assert _condition_is_generic_or_interventional(
        name="抗生素耐药性",
        search_term="antibiotic resistance",
        **base,
    )
    assert _condition_is_generic_or_interventional(
        name="艰难梭菌感染",
        search_term="Clostridium difficile infection",
        **base,
    )
    for generic_outcome in (
        "更差的预后",
        "不良预后",
        "死亡",
        "死亡风险",
        "交叉感染",
        "医院感染",
        "医疗相关感染",
        "人类感染",
        "人类真菌病原体",
        "潜在人类病原体",
        "主要致病菌",
        "环境来源",
        "真菌感染",
        "实体瘤",
        "发病率",
        "死亡率",
        "免疫受损",
        "性传播感染",
        "发病率和死亡率",
        "抗菌素耐药革兰氏阴性菌感染",
        "胃肠道疾病",
        "严重真菌病",
    ):
        assert _condition_is_generic_or_interventional(
            name=generic_outcome,
            search_term=generic_outcome,
            **base,
        )


def test_disease_interpretation_excludes_confirmation_methods_and_foreign_statistics():
    crohn = DiseaseRiskItem(
        disease_id="disease-crohn",
        name="克罗恩病",
        source_type="evimed_association",
        associationEvidenceRefs=[1],
    )
    result = _sanitize_disease_narrative(
        "克罗恩病是一种慢性炎症性肠病，粪钙卫蛋白、内镜和活检可用于评估。",
        disease=crohn,
        has_session_context=False,
    )
    assert result == ""


def test_disease_interpretation_translates_uncontrolled_epilepsy_modifier():
    epilepsy = DiseaseRiskItem(
        disease_id="disease-epilepsy",
        name="癫痫",
        source_type="evimed_association",
        associationEvidenceRefs=[1],
    )

    assert _sanitize_disease_narrative(
        "癫痫是一种以反复发作为特征的慢性脑部疾病，uncontrolled癫痫患者中更值得关注。",
        disease=epilepsy,
        has_session_context=False,
    ) == ""


def test_disease_interpretation_excludes_management_in_place_of_explanation():
    constipation = DiseaseRiskItem(
        disease_id="disease-constipation",
        name="便秘",
        source_type="evimed_association",
        associationEvidenceRefs=[1],
    )
    assert (
        _sanitize_disease_narrative(
            "包括饮食、行为和生活方式调整，支持便秘相关排便调护。",
            disease=constipation,
            has_session_context=False,
        )
        == ""
    )


def test_public_medical_interpretation_removes_treatment_and_epidemiology_meta():
    microbes, _diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体临床证据将肠沙门菌列为沙门菌病相关病原体。",
            evidenceRefs=[1],
        )
    ]
    assert (
        _sanitize_microbe_narrative(
            "肠沙门菌与沙门菌病相关，是沙门菌病的病原体，抗生素治疗可缩短病程。",
            microbe=microbe,
            has_session_context=False,
            field="patient_interpretation",
        )
        == "肠沙门菌与沙门菌病相关。"
    )


def test_public_microbe_interpretation_keeps_medical_fact_but_removes_boundaries():
    microbes, _diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="菌血症",
            description="人体证据记录该关联。",
            evidenceRefs=[1],
        )
    ]
    value = "肠沙门菌是菌血症的相关病原体；在粪便样本中仅作为研究背景。"
    assert (
        _sanitize_microbe_narrative(
            value,
            microbe=microbe,
            has_session_context=False,
            field="patient_interpretation",
        )
        == "肠沙门菌是菌血症的相关病原体。"
    )

    for value in (
        "在本次肠道样本中检出该菌。肠沙门菌是菌血症的相关病原体。",
        "本次在肠道样本中检出。肠沙门菌是菌血症的相关病原体。",
        "在肠道样本中检出该菌。肠沙门菌是菌血症的相关病原体。",
        "肠沙门菌是菌血症的相关病原体。本次检出为低丰度信号，不一定代表感染。",
        "肠沙门菌是菌血症的相关病原体。本次肠道检出为低丰度信号，需关注全身情况。",
    ):
        assert (
            _sanitize_microbe_narrative(
                value,
                microbe=microbe,
                has_session_context=False,
                field="patient_interpretation",
            )
            == "肠沙门菌是菌血症的相关病原体。"
        )

    assert (
        _sanitize_microbe_narrative(
            "肠沙门菌与菌血症相关，但主要经呼吸道感染，与肠道检出关联性有限。",
            microbe=microbe,
            has_session_context=False,
            field="patient_interpretation",
        )
        == "肠沙门菌与菌血症相关。"
    )

    value = (
        "肠沙门菌是菌血症的相关病原体，但在健康人其他部位也常见，仅凭检出不能确定致病。"
    )
    assert (
        _sanitize_microbe_narrative(
            value,
            microbe=microbe,
            has_session_context=False,
            field="patient_interpretation",
        )
        == "肠沙门菌是菌血症的相关病原体。"
    )

    disease = DiseaseRiskItem(
        disease_id="disease-sleep",
        name="睡眠障碍",
        source_type="source_report",
        associationEvidenceRefs=[2],
    )
    assert (
        _sanitize_disease_narrative(
            "睡眠障碍会影响睡眠质量，世界卫生组织调查显示27%的人有睡眠问题。",
            disease=disease,
            has_session_context=False,
            grounded_numbers={27.0},
        )
        == ""
    )

    diabetes = DiseaseRiskItem(
        disease_id="disease-diabetes",
        name="II型糖尿病",
        source_type="source_report",
        associationEvidenceRefs=[3],
    )
    assert (
        _sanitize_disease_narrative(
            "II型糖尿病患病率预计因人口老龄化而上升，"
            "社会经济差异和体重污名会影响健康结局。",
            disease=diabetes,
            has_session_context=False,
        )
        == ""
    )

    crc = DiseaseRiskItem(
        disease_id="disease-crc",
        name="结直肠癌",
        source_type="evimed_association",
        associationEvidenceRefs=[2],
    )
    assert not _sanitize_disease_narrative(
        "结直肠癌在英国是第4常见癌症，5年生存率为60%。",
        disease=crc,
        has_session_context=False,
        grounded_numbers={4.0, 5.0, 60.0},
    )
    crc_result = _sanitize_disease_narrative(
        "结直肠癌是常见恶性肿瘤，早筛、早诊、早治是降低死亡率的关键措施。",
        disease=crc,
        has_session_context=False,
    )
    assert not crc_result

    constipation = DiseaseRiskItem(
        disease_id="disease-constipation",
        name="便秘",
        source_type="evimed_association",
        associationEvidenceRefs=[3],
    )
    constipation_result = _sanitize_disease_narrative(
        "便秘表现为排便次数减少和排便费力，可用于功能性便秘的临床识别。",
        disease=constipation,
        has_session_context=False,
    )
    assert constipation_result == ""
    assert not _sanitize_disease_narrative(
        "便秘的评估强调区分功能性便秘与继发性便秘。",
        disease=constipation,
        has_session_context=False,
    )
    assert (
        _sanitize_disease_narrative(
            (
                "便秘是一种常见的消化系统疾病，"
                "强调区分原发性和继发性病因，并且便秘对生活质量有显著影响。"
            ),
            disease=constipation,
            has_session_context=False,
        )
        == ""
    )
    assert (
        _sanitize_disease_narrative(
            "便秘是一种常见的功能性肠病，可严重影响生活质量与心理健康，"
            "其病理机制认识在持续深入。",
            disease=constipation,
            has_session_context=False,
        )
        == ""
    )

    crc_relation = _sanitize_disease_narrative(
        "具核梭杆菌与结直肠癌存在群体层面的相关性，但个体风险受多种因素影响。",
        disease=crc,
        has_session_context=False,
    )
    assert crc_relation == ""

    diabetes = DiseaseRiskItem(
        disease_id="disease-t2dm",
        name="II型糖尿病",
        source_type="evimed_association",
        associationEvidenceRefs=[4],
    )
    diabetes_result = _sanitize_disease_narrative(
        "II型糖尿病的基本病理生理特征是胰岛β细胞功能障碍，保护胰岛β细胞功能有助于延缓2型糖尿病的进展。",
        disease=diabetes,
        has_session_context=False,
    )
    assert diabetes_result == ""

    hba1c = DiseaseRiskItem(
        disease_id="disease-hba1c",
        name="糖化血红蛋白",
        source_type="evimed_association",
        item_type="biomarker",
        associationEvidenceRefs=[5],
    )
    assert not _sanitize_disease_narrative(
        "空腹血糖异常是糖尿病前期的一种表现。",
        disease=hba1c,
        has_session_context=False,
    )
    assert not _sanitize_disease_narrative(
        "糖化血红蛋白反映一段时间内的平均血糖水平。",
        disease=hba1c,
        has_session_context=False,
    )


def test_recommendation_drops_generic_activity_even_when_source_repeats_it():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "exercise",
                    "content": "建议保持规律身体活动。运动还能改善整体健康，增强免疫功能。",
                    "evidenceRefs": [2],
                }
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": set(),
            "exercise": {2},
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={2: "成人身体活动指南建议保持规律身体活动。"},
    )
    assert validated.recommendations == []


def test_extractive_health_profile_uses_only_source_and_report_applicable_risks():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体粪便研究记录该关联。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        ),
        RiskAssociation(
            name="牙髓感染",
            description="口腔样本研究记录该关联。",
            evidenceRefs=[2],
            patientApplicability="contextual",
            sampleAlignment="other",
        ),
    ]
    profile = _extractive_health_profile(
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )
    assert profile is not None
    assert "肠道微生态结果可用于安排胃肠功能观察" in profile
    assert "沙门菌病（肠沙门菌检测变化）" not in profile
    assert "牙髓感染" not in profile
    assert "便秘" not in profile
    assert "多个健康问题" not in profile


def test_extractive_health_profile_leads_with_overall_health_before_gut_content():
    microbes, diseases = sections()
    dimensions = [
        HealthAssessmentDimension(
            code=code,
            name=name,
            weight=weight,
            score=score,
            score_available=True,
            display_score=str(score),
            level=level,
            level_label=label,
            confidence="moderate",
            explanation=f"{name}评估说明。",
        )
        for code, name, weight, score, level, label in (
            ("digestive_system", "消化系统", 1 / 6, 50, "priority_attention", "重点关注"),
            ("metabolic_system", "代谢系统", 1 / 6, 50, "priority_attention", "重点关注"),
            ("cardiovascular_system", "心脑血管系统", 1 / 6, 70, "attention", "需要关注"),
            ("immune_system", "免疫系统", 1 / 6, 70, "attention", "需要关注"),
            ("urinary_system", "泌尿系统", 1 / 6, 85, "good", "良好"),
            ("nervous_system", "神经系统", 1 / 6, 85, "good", "良好"),
        )
    ]
    assessment = HealthAssessment(
        overall_score=68,
        overall_score_available=True,
        display_overall_score="68",
        level="attention",
        level_label="需要关注",
        summary="本次综合评估需要关注。",
        dimensions=dimensions,
    )

    profile = _extractive_health_profile(
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        health_assessment=assessment,
    )

    assert profile is not None
    assert profile.startswith("综合现有健康资料，当前建议优先围绕消化系统、代谢系统")
    assert "饮食结构、规律活动和作息管理" in profile
    assert profile.index("综合现有健康资料") < profile.index("肠道微生态")
    assert "68分" not in profile
    assert "肠型II" not in profile


def _valid_executive_summary(*, disease: str = "") -> str:
    topic = f"{disease}相关的健康影响作为本次关注方向之一。" if disease else ""
    return topic + (
        "本次健康管理评估将肠道微生态、全身代谢与现有临床资料纳入同一背景理解。"
        "肠型及达到人体证据要求的菌群关系用于说明可能受影响的健康方向，"
        "疾病关联内容仅保留具有可用医学解释的部分。"
        "已有检查用于补充当前健康背景，应优先关注资料中较明确的胃肠道功能、"
        "代谢及心血管影响，并观察这些方向的变化。"
        "这些结论用于形成当前健康管理关注顺序。"
    )


def _valid_microbe_summary() -> str:
    return (
        "本次病原相关菌检出肠沙门菌，小结根据原报告分类、本次样本结果与已绑定人体证据归纳。"
        "表格逐项保留检测方给出的菌种名称和状态，并将能够与该菌精确匹配的疾病或不良影响按临床重要性整理。"
        "医学解读仅汇总已经通过菌种层级和对象对齐的证据内容。"
        "读者可以在同一行对照菌种、检测结果、相关风险、医学解读与证据编号，便于阅读。"
    )


def test_bounded_display_text_keeps_complete_sentences_within_public_limit():
    sentence = "本次结果记录了检测方提供的菌种与疾病关联信息，并按证据逐项归纳。"
    value = sentence * 20
    bounded = _bounded_display_text(value, minimum=300, maximum=500)
    assert bounded is not None
    assert 300 <= len("".join(bounded.split())) <= 500
    assert bounded.endswith("。")
    assert bounded in value


def test_bounded_display_text_replaces_non_terminal_semicolon():
    value = "本次结果记录了检测方提供的菌种与疾病关联信息，并按证据逐项归纳；" * 12
    bounded = _bounded_display_text(value, minimum=300, maximum=500)
    assert bounded is not None
    assert bounded.endswith("。")
    assert not bounded.endswith("；")


def test_health_profile_does_not_require_repeating_every_gut_detail():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病的临床关联。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    assert _health_profile_is_complete(
        "综合现有健康资料，当前需优先关注糖脂代谢与长期心血管健康，"
        "同时留意胃肠道舒适度和排便表现的变化。"
        "这些方向反映的是当前健康管理重点，不等同于个人疾病诊断。"
        "日常可持续观察相关身体表现和变化趋势，"
        "同时以饮食、活动和作息管理为基础推进后续健康管理。",
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
    )


def test_health_profile_rejects_other_body_site_contextual_risk():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="牙髓感染",
            description="口腔样本人体资料记录牙髓感染。",
            evidenceRefs=[1],
            patientApplicability="contextual",
            sampleAlignment="other",
        )
    ]
    assert not _health_profile_is_complete(
        "本次为肠型II，检出肠沙门菌，与牙髓感染相关。原报告列出便秘。",
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
    )


def test_health_profile_may_focus_on_overall_health_without_repeating_microbe():
    microbes, diseases = sections()
    assert _health_profile_is_complete(
        "综合现有健康资料，当前需优先关注血脂与长期心血管健康，"
        "同时留意胃肠道舒适度和排便表现。"
        "日常应持续观察相关身体表现和变化趋势，"
        "对持续或逐渐加重的异常优先进行健康管理，"
        "并以饮食结构、规律活动和作息调整作为长期管理基础。",
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
    )


def test_source_disease_name_does_not_make_health_profile_contextual():
    microbes, diseases = sections()
    diseases.items[0].name = "牙髓感染"
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="牙髓感染",
            description="口腔样本研究记录该关联。",
            evidenceRefs=[1],
            patientApplicability="contextual",
            sampleAlignment="other",
        )
    ]
    assert _health_profile_is_complete(
        "综合现有健康资料，肠道微生态和胃肠道功能需要持续关注。"
        "现有资料包含肠沙门菌以及牙髓感染这一健康关联项目，"
        "这类关联主要用于理解可能的健康影响，不等同于个人疾病诊断。"
        "后续以饮食、活动和作息管理为基础，持续观察胃肠道功能变化。",
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
    )


def test_patient_interpretation_must_cover_each_displayed_risk():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病", description="可引起急性胃肠道症状。", evidenceRefs=[1]
        ),
        RiskAssociation(
            name="菌血症", description="易感人群可出现侵袭性感染。", evidenceRefs=[1]
        ),
    ]
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次样本检出肠沙门菌。",
                    "patientInterpretation": "人体证据记录肠沙门菌与沙门菌病的关联。",
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.microbe_narratives[0].patient_interpretation is None


def test_patient_interpretation_rejects_mechanical_detection_template():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
        )
    ]
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次样本检出肠沙门菌。",
                    "patientInterpretation": (
                        "您的粪便样本中检出了该菌。"
                        "沙门菌病是其已展示的健康影响。"
                    ),
                }
            ]
        }
    )

    validated = validate_draft(draft, microbes=microbes, diseases=diseases)

    assert validated.microbe_narratives[0].patient_interpretation is None
    assert validated.microbe_narratives[0].generation_source == "fallback"


def test_clinical_role_without_patient_interpretation_still_requests_repair():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
        )
    ]
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次检出肠沙门菌。",
                    "clinicalRole": "肠沙门菌具有病原学意义。",
                }
            ]
        }
    )

    missing = _missing_narrative_fields(
        draft,
        microbes=microbes,
        diseases=diseases,
    )

    assert missing["microbeIds"] == ["allowed-taxon"]


def test_evidence_only_microbe_still_requests_model_interpretation():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = []
    microbe.model_interpretation_required = True

    missing = _missing_narrative_fields(
        NarrativeDraft(),
        microbes=microbes,
        diseases=diseases,
    )

    assert missing["microbeIds"] == ["allowed-taxon"]


def test_evidence_only_microbe_keeps_model_authored_medical_role():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = []
    microbe.model_interpretation_required = True
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "肠沙门菌具有病原学意义。",
                    "patientInterpretation": (
                        "人体资料支持肠沙门菌具有病原学意义，"
                        "也可作为肠道定植菌被观察。"
                    ),
                }
            ]
        }
    )

    validated = validate_draft(draft, microbes=microbes, diseases=diseases)

    assert validated.microbe_narratives[0].patient_interpretation
    assert validated.microbe_narratives[0].generation_source == "model"


def test_single_health_direction_remains_in_retry_fields():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "general",
                    "topic": "肠道微生态",
                    "priority": "重点关注",
                    "content": "保持饮食结构多样并观察胃肠道功能变化。",
                }
            ]
        }
    )

    missing = _missing_narrative_fields(
        draft,
        microbes=microbes,
        diseases=diseases,
    )

    assert "recommendations" in missing["fields"]


def test_disease_narrative_keeps_scoped_followup_attention_sentence():
    microbes, diseases = sections()
    disease = diseases.items[0]
    disease.association_evidence_refs = [1]
    disease.related_microbes = ["Salmonella enterica"]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": (
                        "本次检测中Salmonella enterica的相关变化与"
                        "人体研究中观察到的便秘方向一致。"
                        "因此建议关注排便困难等症状和胃肠功能变化。"
                    ),
                }
            ]
        }
    )

    validated = validate_draft(draft, microbes=microbes, diseases=diseases)

    text = validated.disease_narratives[0].related_content
    assert "Salmonella enterica" in text
    assert "排便困难等症状" in text


def test_disease_narrative_accepts_natural_model_attention_wording():
    microbes, diseases = sections()
    disease = diseases.items[0]
    disease.association_evidence_refs = [1]
    disease.related_microbes = ["Dorea", "Turicibacter", "Rothia"]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": (
                        "人体研究观察到，在便秘型肠易激综合征伴有腹胀的人群中，"
                        "肠道菌群中的Dorea、Turicibacter和Rothia存在组间差异。"
                        "本次检测中这些菌群的状态可能与排便规律性或腹部不适等感受"
                        "相关，值得关注。"
                    ),
                }
            ]
        }
    )

    validated = validate_draft(draft, microbes=microbes, diseases=diseases)

    assert validated.disease_narratives
    assert "值得关注" in validated.disease_narratives[0].related_content


def test_retry_merge_preserves_model_authorship_marker():
    original = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次检出肠沙门菌。",
                    "clinicalRole": "肠沙门菌具有病原学意义。",
                }
            ]
        }
    )
    retry = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次检出肠沙门菌。",
                    "patientInterpretation": (
                        "肠沙门菌具有病原学意义，"
                        "人体证据记录其与沙门菌病相关。"
                    ),
                    "generation_source": "model",
                }
            ]
        }
    )

    merged = _merge_missing_narrative(
        original,
        retry,
        {"fields": [], "microbeIds": ["allowed-taxon"], "diseaseIds": []},
    )

    assert merged.microbe_narratives[0].generation_source == "model"


def test_execution_boundary_removal_does_not_publish_bare_study_restatement():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次样本检出肠沙门菌。",
                    "patientInterpretation": (
                        "人体证据记录肠沙门菌与沙门菌病相关。"
                        "本报告为粪便检出，不直接等同于感染。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    # Removing the boundary leaves only research provenance plus an association
    # name.  The dedicated retry should replace it with a real medical
    # interpretation instead of publishing that old adjacent-column content.
    assert validated.microbe_narratives[0].patient_interpretation is None


def test_microbe_interpretation_retries_bare_disease_name_restatement():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门菌可引起沙门菌病。",
            relationType="established_pathogen",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    terse = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "肠沙门菌具有病原学意义。",
                    "patientInterpretation": "可能引起沙门菌病。",
                }
            ]
        }
    )
    richer = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "肠沙门菌具有病原学意义。",
                    "patientInterpretation": (
                        "可能影响肠道黏膜炎症及全身感染过程，"
                        "并引起沙门菌病。"
                    ),
                }
            ]
        }
    )

    terse_validated = validate_draft(terse, microbes=microbes, diseases=diseases)
    richer_validated = validate_draft(richer, microbes=microbes, diseases=diseases)

    # For a validated established pathogen relationship, "may cause X" is the
    # clinically meaningful relation itself and does not need a fabricated
    # mechanism merely to make the sentence longer.
    assert terse_validated.microbe_narratives[0].patient_interpretation == (
        "可能引起沙门菌病。"
    )
    assert richer_validated.microbe_narratives[0].patient_interpretation == (
        "可能影响肠道黏膜炎症及全身感染过程，并引起沙门菌病。"
    )


def test_microbe_interpretation_retries_organism_prefixed_bare_association():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="炎症性肠病",
            description="相关变化与炎症性肠病活动度相关。",
            evidenceRefs=[1],
            patientApplicability="contextual",
            sampleAlignment="stool",
        ),
        RiskAssociation(
            name="结直肠癌",
            description="相关代谢信号可能影响结直肠癌风险。",
            evidenceRefs=[2],
            patientApplicability="contextual",
            sampleAlignment="stool",
        ),
    ]
    terse = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "已形成医学解读。",
                    "patientInterpretation": (
                        "肠沙门菌与炎症性肠病和结直肠癌相关。"
                    ),
                }
            ]
        }
    )
    richer = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "已形成医学解读。",
                    "patientInterpretation": (
                        "可能影响肠黏膜炎症活动，与炎症性肠病活动度相关；"
                        "相关代谢信号也可能影响结直肠癌风险。"
                    ),
                }
            ]
        }
    )

    terse_validated = validate_draft(terse, microbes=microbes, diseases=diseases)
    richer_validated = validate_draft(richer, microbes=microbes, diseases=diseases)

    assert terse_validated.microbe_narratives[0].patient_interpretation is None
    assert richer_validated.microbe_narratives[0].patient_interpretation == (
        "可能影响肠黏膜炎症活动，与炎症性肠病活动度相关；"
        "相关代谢信号也可能影响结直肠癌风险。"
    )


def test_microbe_interpretation_removes_context_strain_and_population_asides():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
        )
    ]
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "本次样本检出肠沙门菌。",
                    "patientInterpretation": (
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "但该证据来自血液样本，对于粪便样本仅提供背景参考。"
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "部分菌株携带毒素基因。"
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "在慢性肝病患者中可引起侵袭性感染。"
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "但本次检测仅反映粪便菌群构成，不直接提示个体患病风险。"
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "需分子测序进一步确认。"
                        "该菌是机会致病菌，人体证据记录肠沙门菌与沙门菌病相关。"
                        "人体证据记录肠沙门菌与沙门菌病相关，"
                        "但该证据来自非肠道样本，与粪便检出的关联有限。"
                        "肠沙门菌在人体临床证据中被列为沙门菌病相关病原体。"
                        "肠沙门菌是机会致病菌，人体临床资料记录其与沙门菌病相关。"
                        "肠沙门菌与沙门菌病相关，也可作为定植菌检出。"
                        "肠沙门菌是肠道常见菌群，但部分亚型与沙门菌病的群体性关联已有文献报告，提示其可能参与疾病过程。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    text = validated.microbe_narratives[0].patient_interpretation or ""
    assert "沙门菌病" in text
    assert "背景参考" not in text
    assert "毒素基因" not in text
    assert "慢性肝病" not in text
    assert "仅反映" not in text
    assert "测序" not in text
    assert "机会致病菌" not in text
    assert "证据来自" not in text
    assert "在人体临床证据中被列为" not in text
    assert "肠沙门菌是沙门菌病相关病原体" in text
    assert "定植菌" not in text
    assert "记录其" not in text
    assert "肠道常见菌群" not in text
    assert "文献报告" not in text


def test_executive_hospital_fact_keeps_relevant_exam_and_latest_abnormal_labs():
    facts = _executive_hospital_fact(
        [
            HospitalDataInput(
                dataType="EXAM",
                itemName="胃镜检查",
                examResult="慢性非萎缩性胃炎",
                reportTime="2025-12-18 08:15:39",
            ),
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白",
                testResultValue="3.70",
                resultStatusName="偏高",
                reportTime="2025-12-15 09:31:08",
            ),
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白",
                testResultValue="3.44",
                resultStatusName="偏高",
                reportTime="2025-10-29 12:44:25",
            ),
        ]
    )
    assert facts is not None
    assert "慢性非萎缩性胃炎" in facts
    assert "3.70偏高" in facts
    assert "3.44" not in facts


def test_health_profile_does_not_turn_source_association_items_into_history():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {"healthProfile": ("本次为肠型II，并检出肠沙门菌。既往疾病列表中包括便秘。")}
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert "既往疾病" not in (validated.health_profile or "")
    assert "肠型II" not in (validated.health_profile or "")
    assert "原报告列出的疾病关联项目包括便秘" in (validated.health_profile or "")


def test_health_profile_does_not_turn_source_association_items_into_patient_problems():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "healthProfile": (
                "本次为肠型II，并检出肠沙门菌。您还存在便秘等消化系统相关健康问题。"
            )
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert "您还存在" not in (validated.health_profile or "")
    assert "肠型II" not in (validated.health_profile or "")
    assert "原报告列出的疾病或健康关联项目包括便秘" in (validated.health_profile or "")


def test_executive_summary_requires_all_report_domains_without_microbe_listing():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    summary = _valid_executive_summary(disease="便秘").replace(
        "本次健康管理评估",
        "本次肠型为肠型II。本次健康管理评估",
        1,
    )
    assert _executive_summary_is_complete(
        summary,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    assert not _executive_summary_is_complete(
        summary.replace("本次肠型为肠型II。", ""),
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    mechanical = summary.replace(
        "本次健康管理评估",
        "本次共记录46项菌种结果，其中病原相关菌15项。本次粪便菌群检测中，",
        1,
    )
    assert not _executive_summary_is_complete(
        mechanical,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    fixed_boundary = summary.replace(
        "这些结论用于形成当前健康管理关注顺序。",
        "已有临床资料显示，这些模型指数仅代表报告记录的数值。",
    )
    assert not _executive_summary_is_complete(
        fixed_boundary,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )
    model_action = summary.replace(
        "这些结论用于形成当前健康管理关注顺序。",
        "模型指数为55.56，提示需依据临床标准进一步区分",
    )
    assert not _executive_summary_is_complete(
        model_action,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    generic_improvement = summary.replace(
        "这些结论用于形成当前健康管理关注顺序。",
        "维持规律生活方式对当前肠道状态的改善具有积极意义。",
    )
    assert not _executive_summary_is_complete(
        generic_improvement,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    process_tail = summary.replace(
        "这些结论用于形成当前健康管理关注顺序。",
        "上述结果基于本次样本检测，本摘要提供整体状况的概括性解读。",
    )
    assert not _executive_summary_is_complete(
        process_tail,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
    )
    for tail in (
        "以上调护建议旨在支持整体肠道功能维护。",
        "本次报告仅描述报告中的记录，所有数值和项目均来自原报告记录。",
    ):
        assert not _executive_summary_is_complete(
            summary.replace(
                "这些结论用于形成当前健康管理关注顺序。",
                tail,
            ),
            enterotype="肠型II",
            microbes=microbes,
            diseases=diseases,
            hospital_data=[HospitalDataInput(dataType="EXAM", itemName="肠镜检查")],
        )


def test_executive_summary_must_name_an_evidence_bound_microbe_risk():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    summary = _valid_executive_summary(disease="便秘").replace(
        "沙门菌病", "相关感染", 1
    )
    assert not _executive_summary_is_complete(
        summary,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )


def test_executive_summary_rejects_content_shorter_than_formal_report_contract():
    microbes, diseases = sections()
    short = (
        "本次为肠型II，原报告记录便秘关联项目。"
        "本次检出肠沙门菌，并将有证据的医学关联与日常饮食调护一并归纳。"
    )
    assert len(short) < 150
    assert not _executive_summary_is_complete(
        short,
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )


def test_executive_summary_rejects_missing_data_process_text():
    microbes, diseases = sections()
    summary = _valid_executive_summary(disease="便秘") + "本次检测未标注参考范围。"
    assert not _executive_summary_is_complete(
        summary,
        enterotype=None,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )


@pytest.mark.parametrize(
    "editorial",
    [
        "其病理机制认识在不断深入。",
        "其病理机制认识在持续深入。",
        "其病理机制的认识日益深入。",
    ],
)
def test_executive_summary_rejects_editorial_progress_text(editorial):
    microbes, diseases = sections()
    summary = _valid_executive_summary(disease="便秘") + editorial
    assert 150 <= len(summary) <= 250
    assert not _executive_summary_is_complete(
        summary,
        enterotype=None,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )


@pytest.mark.parametrize(
    "padding",
    (
        "以上结果基于本次样本检测。",
        "本次为单一时间点的样本分析。",
        "整体而言，本次检测提供了基础数据。",
        "以上为本次检测结果的综合解读。",
    ),
)
def test_executive_summary_rejects_generic_length_padding(padding):
    microbes, diseases = sections()
    summary = _valid_executive_summary(disease="便秘") + padding
    assert 150 <= len(summary) <= 250
    assert not _executive_summary_is_complete(
        summary,
        enterotype=None,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )


def test_extractive_executive_summary_uses_only_validated_case_specific_fields():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体证据记录肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        ),
        RiskAssociation(
            name="便秘",
            description="口腔样本研究记录肠沙门菌与便秘相关。",
            evidenceRefs=[9],
            patientApplicability="contextual",
            sampleAlignment="other",
        ),
    ]
    disease = diseases.items[0]
    disease.medical_interpretation = (
        "便秘的临床特征可包括排便次数减少、排便困难或粪便干硬，"
        "相关人体证据提示其可能影响日常生活质量。"
    )
    disease.evidence_refs = [2]
    disease.association_evidence_refs = [2]
    result = _extractive_executive_summary(
        health_profile="本次为肠型II，并检出肠沙门菌，同时需关注便秘相关健康表现。",
        recommendations=[
            HealthRecommendation(
                category="diet",
                content="日常饮食保持多样化食物选择，形成稳定、均衡的饮食结构。",
                evidenceRefs=[3],
            ),
            HealthRecommendation(
                category="exercise",
                content="保持规律身体活动，将体育活动作为日常健康安排。",
                evidenceRefs=[4],
            ),
            HealthRecommendation(
                category="sleep",
                content="保持规律作息，并重视稳定的就寝和起床时间。",
                evidenceRefs=[5],
            ),
            HealthRecommendation(
                category="bowel_habits",
                content="关注排便规律，有便意时及时如厕。",
                evidenceRefs=[6],
            ),
        ],
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[
            HospitalDataInput(
                dataType="EXAM",
                itemName="无痛肠镜检查",
                examResult="所见结直肠粘膜大致正常。",
            )
        ],
    )
    assert result is not None
    assert 150 <= len("".join(result.split())) <= 250
    assert "肠型II" in result
    assert "肠沙门菌" in result
    assert "便秘" in result
    assert "原报告" not in result
    assert "所见结直肠粘膜大致正常" not in result
    assert "模型指数" not in result
    assert "日常饮食保持多样化" not in result
    assert "保持规律身体活动" not in result
    assert "口腔样本" not in result
    assert "共记录" not in result
    assert "病原相关菌1项" not in result


def test_extractive_executive_summary_does_not_repeat_sections_to_pad_length():
    microbes, diseases = sections()
    result = _extractive_executive_summary(
        health_profile=("本次肠型为肠型II，并记录肠沙门菌和便秘。" * 20),
        recommendations=[],
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )
    assert result is not None
    assert result.count("本次肠型为肠型II") == 1


def test_extractive_executive_summary_merges_equivalent_attention_topics():
    microbes, diseases = sections()
    result = _extractive_executive_summary(
        health_profile=None,
        recommendations=[
            HealthRecommendation(
                category="general",
                topic="血脂管理",
                content="关注血脂相关健康影响。",
            ),
            HealthRecommendation(
                category="general",
                topic="血脂异常管理",
                content="同一影响的另一种标题写法。",
            ),
            HealthRecommendation(
                category="bowel_habits",
                topic="排便功能",
                content="关注胃肠道表现。",
            ),
            HealthRecommendation(
                category="bowel_habits",
                topic="肠道菌群平衡与排便功能",
                content="包含排便功能的更完整健康方向。",
            ),
        ],
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )

    assert result is not None
    assert "血脂管理、血脂异常管理" not in result
    assert result.count("血脂") == 1
    assert "排便功能、肠道菌群平衡与排便功能" not in result
    assert "肠道菌群平衡与排便功能" in result


def test_fallback_attention_directions_use_confirmed_abnormal_indicators_even_when_dimension_is_unscored():
    microbes, diseases = sections()
    assessment = HealthAssessment()
    assessment.abnormal_indicators = [
        HealthAssessmentAbnormalIndicator(
            dimensionCode="metabolic_system",
            dimensionName="代谢系统",
            indicatorCode="triglyceride",
            item="甘油三酯",
            value="3.34",
            unit="mmol/L",
            status="偏高",
            severity="priority_attention",
        )
    ]

    health = HealthManagementSection()
    summary = apply_extractive_health_fallback(
        health=health,
        evidence_summaries=[],
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        health_assessment=assessment,
    )

    assert summary is not None
    assert [item.topic for item in health.recommendations] == ["代谢系统"]
    assert health.recommendations[0].priority == "重点关注"


def test_safe_extractive_summary_survives_rejected_detailed_relation_sentence():
    microbes, diseases = sections()
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="该方向需结合临床检查进一步判断。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    diseases.items[0].medical_interpretation = "相关菌群变化提示便秘健康方向。"
    diseases.items[0].association_evidence_refs = [2]

    summary = _safe_extractive_executive_summary(
        attention_topics=["肠道微生态", "糖脂代谢"],
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
    )

    assert summary is not None
    assert 150 <= len(summary) <= 250
    assert "肠型II" in summary
    assert "需结合临床" not in summary


def test_extractive_executive_summary_can_use_validated_evaluation_purpose():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.name = "肠沙门氏菌"
    microbe.source_finding.status = "痕量检出"
    microbe.source_finding.value = "<0.0001"
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门氏菌可引起沙门菌病。",
            evidenceRefs=[1],
        ),
        RiskAssociation(
            name="肠热病",
            description="肠沙门氏菌与肠热病相关。",
            evidenceRefs=[2],
        ),
    ]
    beneficial = microbe.model_copy(deep=True)
    beneficial.microbe_id = "taxon-b-longum"
    beneficial.name = "长双歧杆菌"
    beneficial.scientific_name = "Bifidobacterium longum"
    beneficial.category = "beneficial_abnormal"
    beneficial.source_finding.status = "偏低"
    beneficial.source_finding.value = "0.01"
    beneficial.disease_and_risks = []
    beneficial.evidence_refs = []
    microbes.items.append(beneficial)
    disease = diseases.items[0]
    disease.medical_interpretation = (
        "慢性便秘是一种常见的功能性肠病，可严重影响生活质量与心理健康。"
    )
    disease.evidence_refs = [3, 4]
    disease.suggested_evaluations = [
        SuggestedEvaluation(
            name="结肠传输试验",
            rationale="用于明确慢性便秘的病因、类型、严重程度。",
            purpose="评估",
            evidenceRefs=[4],
        )
    ]
    result = _extractive_executive_summary(
        health_profile=None,
        recommendations=[
            HealthRecommendation(
                category="diet",
                content="日常饮食可保持食物多样性，并合理搭配各类食物。",
                evidenceRefs=[5],
            ),
            HealthRecommendation(
                category="exercise",
                content="可在日常生活中安排身体活动。",
                evidenceRefs=[6],
            ),
            HealthRecommendation(
                category="sleep",
                content="保持规律的睡眠作息，固定就寝和起床时间，避免睡眠不足。",
                evidenceRefs=[7],
            ),
            HealthRecommendation(
                category="bowel_habits",
                content="可关注并保持规律的排便习惯。",
                evidenceRefs=[8],
            ),
        ],
        enterotype="肠型II",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )

    assert result is not None
    assert 150 <= len(result) <= 250
    assert "疾病关联风险中存在" not in result
    assert "结肠传输试验" not in result


def test_extractive_summary_keeps_detected_pathogen_without_inventing_its_risk():
    microbes, diseases = sections()
    salmonella = microbes.items[0]
    salmonella.name = "肠沙门菌"
    salmonella.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门菌可引起沙门菌病。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    mtb = salmonella.model_copy(deep=True)
    mtb.microbe_id = "taxon-mtb"
    mtb.name = "结核分枝杆菌"
    mtb.scientific_name = "Mycobacterium tuberculosis"
    mtb.source_finding.value = "0.001"
    mtb.disease_and_risks = []
    mtb.evidence_refs = []
    microbes.items.append(mtb)
    diseases.items[
        0
    ].medical_interpretation = "便秘是以排便次数减少、排便困难或粪便干硬为特征的症候。"
    diseases.items[0].evidence_refs = [2]
    recommendations = [
        HealthRecommendation(
            category="diet",
            content=(
                "日常饮食保持食物多样性，合理搭配谷物、蔬菜、水果和蛋白质食物，"
                "并根据日常进食情况逐步形成稳定、均衡且可持续的饮食结构。"
            ),
            evidenceRefs=[3],
        ),
        HealthRecommendation(
            category="exercise",
            content="根据个人情况安排规律身体活动，循序渐进并减少久坐时间。",
            evidenceRefs=[4],
        ),
        HealthRecommendation(
            category="sleep",
            content="保持规律作息，固定就寝和起床时间，避免持续睡眠不足。",
            evidenceRefs=[5],
        ),
        HealthRecommendation(
            category="bowel_habits",
            content="关注排便规律与粪便性状，有便意时及时如厕并避免长时间忍便。",
            evidenceRefs=[6],
        ),
    ]

    result = _extractive_executive_summary(
        health_profile=None,
        recommendations=recommendations,
        enterotype="肠型I",
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
    )

    assert result is not None
    assert 150 <= len(result) <= 250
    assert "沙门菌病" in result
    assert "结核分枝杆菌（Mycobacterium tuberculosis" not in result
    assert "结核分枝杆菌与" not in result


def test_apply_draft_rebuilds_summary_after_final_health_sections_are_available():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.name = "肠沙门菌"
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门菌是沙门菌病的病原体。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    microbe.medical_interpretation = "肠沙门菌是沙门菌病的病原体。"
    diseases.items[
        0
    ].medical_interpretation = "便秘的病理生理涉及结肠运动异常和感觉功能障碍。"
    diseases.items[0].evidence_refs = [2]
    diseases.items[0].suggested_evaluations = [
        SuggestedEvaluation(
            name="肛门直肠测压",
            rationale="用于评估排便障碍。",
            purpose="评估",
            evidenceRefs=[3],
        )
    ]
    health = HealthManagementSection(
        enterotype=EnterotypeResult(name="肠型I", source="source_report")
    )
    draft = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": "本次检出肠沙门菌，并有人体证据支持沙门菌病关联。",
            "healthLevel": {
                "level": "中度关注",
                "summary": "本次菌种结果及原报告便秘关联项目构成当前健康画像。",
            },
            "healthProfile": "本次为肠型I，并检出肠沙门菌。原报告另列出便秘关联项目。",
            "recommendations": [
                {
                    "category": "diet",
                    "content": "日常饮食可保持食物多样性，并合理搭配各类食物。",
                    "evidenceRefs": [4],
                },
                {
                    "category": "exercise",
                    "content": (
                        "进行身体活动时注意心跳、呼吸加快等反应，"
                        "选择适合自己的活动方式，并保持规律。"
                    ),
                    "evidenceRefs": [5],
                },
                {
                    "category": "sleep",
                    "content": (
                        "保持规律的就寝和起床时间，尽早入睡，避免睡眠时间不规律。"
                    ),
                    "evidenceRefs": [6],
                },
                {
                    "category": "bowel_habits",
                    "content": "关注排便规律与粪便性状，有便意时及时如厕并避免长时间忍便。",
                    "evidenceRefs": [7],
                },
            ],
        }
    )

    summary = apply_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        health=health,
        hospital_data=[],
    )

    assert summary is not None
    assert 150 <= len(summary) <= 250
    assert "肠型I" in summary
    assert "疾病关联风险中存在" not in summary


def test_health_profile_normalizes_awkward_source_disease_transition():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "healthProfile": (
                "本次为肠型II，并检出肠沙门菌。"
                "您还有原报告列出的疾病或健康关联项目包括的便秘。"
            )
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.health_profile == (
        "检出肠沙门菌。原报告列出的疾病或健康关联项目包括便秘。"
    )


@pytest.mark.asyncio
async def test_weak_exact_relation_claim_is_retried_from_evimed_title_and_summary():
    record = EvidenceRecord(
        evidence_key="shigella-relation",
        category="guideline",
        title="Guidelines for shigellosis caused by Shigella dysenteriae",
        source="Society",
        summary=(
            "Shigella dysenteriae causes shigellosis and may present with bloody "
            "diarrhea."
        ),
        targets=["Shigella dysenteriae / 痢疾志贺菌", "细菌性痢疾"],
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-shigella",
                microbeId="taxon-shigella",
                microbeName="Shigella dysenteriae",
                microbeAliases=["痢疾志贺菌"],
                diseaseId="disease-shigellosis",
                diseaseName="细菌性痢疾",
                relationType="established_pathogen",
            )
        ],
        interpretation={
            "claim": "该指南提供了细菌性痢疾的诊断与管理建议。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )

    class Client:
        async def generate(self, payload):
            assert payload["task"] == "仅从EviMed标题摘要中重写精确菌种与疾病的医学关系"
            assert set(payload["evidence"][0]) == {
                "number",
                "title",
                "summary",
                "relations",
            }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "痢疾志贺菌可引起细菌性痢疾，并可出现血性腹泻。",
                    }
                ]
            }

    await _retry_weak_relation_claims(
        Client(),
        records=[record],
        numbers={record.evidence_key: 1},
    )
    assert record.interpretation is not None
    assert record.interpretation.claim == (
        "痢疾志贺菌可引起细菌性痢疾，并可出现血性腹泻。"
    )


@pytest.mark.asyncio
async def test_genus_mycotoxin_source_is_not_treated_as_genus_level_disease_evidence():
    record = EvidenceRecord(
        evidence_key="fusarium-mycotoxin",
        category="literature",
        title="Fusarium mycotoxin and health effects",
        source="Journal",
        summary="Fusarium mycotoxins were associated with adverse health effects.",
        microbeIds=["taxon-fusarium"],
        intents=["microbe_disease_association"],
        targets=["Fusarium / 镰刀菌属 / 癌症"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "镰刀菌属霉菌毒素与癌症相关。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "genus",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={record.evidence_key: 1},
        _retry_missing=False,
    )
    assert record.interpretation is not None
    assert record.interpretation.taxon_alignment == "toxin_gene"


@pytest.mark.asyncio
async def test_exact_species_in_source_and_claim_corrects_model_genus_alignment():
    record = EvidenceRecord(
        evidence_key="campylobacter-overview",
        category="literature",
        title="弯曲菌及弯曲菌病的流行现状",
        source="Journal",
        summary="Campylobacter coli是弯曲菌感染最常见的菌种之一。",
        microbeIds=["taxon-campylobacter-coli"],
        intents=["microbe_disease_association"],
        targets=["Campylobacter coli / 结肠弯曲菌 / 弯曲菌病"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "Campylobacter coli与弯曲菌病相关。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "genus",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={record.evidence_key: 1},
        _retry_missing=False,
    )
    assert record.interpretation is not None
    assert record.interpretation.taxon_alignment == "species"


@pytest.mark.asyncio
async def test_evidence_interpretation_is_number_bound_and_rejects_invented_effect():
    record = EvidenceRecord(
        evidence_key="paper",
        category="literature",
        title="Human cohort of Fusobacterium nucleatum and colorectal cancer",
        source="Gut",
        summary=(
            "A human cohort reported Fusobacterium nucleatum abundance associated "
            "with colorectal cancer in the gut microbiome."
        ),
        supports=["taxon-fn", "disease-crc"],
        intents=["microbe_disease_association"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "人群队列结果显示具核梭杆菌丰度与结直肠癌存在关联。",
                        "population": "人群队列",
                        "studyDesign": "队列研究",
                        "direction": "associated",
                        "effect": "风险增加80%",
                        "applicability": "支持群体层面的关联判断。",
                        "limitations": "摘要未提供个体诊断效能。",
                        "confidence": "moderate",
                    },
                    {"number": 99, "claim": "不允许的新编号"},
                ]
            }

    completed = await interpret_evidence(
        Client(), records=[record], numbers={"paper": 1}
    )
    assert completed == {1}
    assert record.interpretation.study_design == "队列研究"
    assert record.interpretation.effect is None


@pytest.mark.asyncio
async def test_chinese_case_report_design_is_detected_from_title():
    record = EvidenceRecord(
        evidence_key="case-report",
        category="literature",
        title="假单胞菌指部感染一例",
        source="Journal",
        summary="报道一名患者的局部伤口感染。",
        supports=["taxon-pf"],
        intents=["microbe_clinical_significance"],
        microbeIds=["taxon-pf"],
        targets=["Pseudomonas fluorescens"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该个案报道了假单胞菌的局部伤口感染。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"case-report": 1})
    assert record.interpretation is not None
    assert record.interpretation.study_design == "病例报告"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "claim"),
    [
        ("结直肠癌", "Colorectal cancer is the disease discussed in this guideline."),
        ("II型糖尿病", "Type 2 diabetes is directly addressed by this guideline."),
        ("睡眠障碍", "Sleep disorders are directly addressed by this consensus."),
        ("健康成人运动调护", "Physical activity is recommended for healthy adults."),
    ],
)
async def test_english_evimed_claim_matches_chinese_public_target(target, claim):
    record = EvidenceRecord(
        evidence_key="english-target",
        category="guideline",
        title=claim,
        source="Society",
        summary=claim,
        supports=["target-id"],
        intents=[
            "health_management" if "调护" in target else "disease_clinical_significance"
        ],
        targets=[target],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": claim,
                        "targetAlignment": "direct",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    assert await interpret_evidence(
        Client(), records=[record], numbers={"english-target": 1}
    ) == {1}
    assert record.interpretation.target_alignment == "direct"


@pytest.mark.asyncio
async def test_target_disease_name_is_not_mistaken_for_restricted_comorbidity():
    record = EvidenceRecord(
        evidence_key="epilepsy-target",
        category="guideline",
        title="Clinical guideline for diagnosis of epilepsy in adults",
        source="Society",
        summary="This guideline directly addresses diagnosis of epilepsy in adults.",
        supports=["disease-epilepsy"],
        intents=["diagnostic_guideline"],
        targets=["癫痫"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "The guideline directly addresses diagnosis of epilepsy.",
                        "targetAlignment": "direct",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"epilepsy-target": 1})
    assert record.interpretation.target_alignment == "direct"


@pytest.mark.asyncio
async def test_explicit_causal_wording_from_evimed_is_not_rejected():
    record = EvidenceRecord(
        evidence_key="explicit-cause",
        category="literature",
        title="Campylobacter coli causes enteritis in humans",
        source="Journal",
        summary="Campylobacter coli causes food-borne enteritis in humans.",
        supports=["taxon-campy"],
        intents=["microbe_clinical_significance"],
        targets=["Campylobacter coli / 结肠弯曲菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "结肠弯曲菌可引起食源性肠炎。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    assert await interpret_evidence(
        Client(), records=[record], numbers={"explicit-cause": 1}
    ) == {1}


@pytest.mark.asyncio
async def test_evidence_interpretation_batches_run_with_bounded_concurrency():
    records = [
        EvidenceRecord(
            evidence_key=f"paper-{number}",
            category="literature",
            title=f"Human stool cohort of Salmonella enterica {number}",
            source="Journal",
            summary="A human stool cohort studied Salmonella enterica infection.",
            supports=["taxon-salmonella"],
            intents=["microbe_clinical_significance"],
            targets=["Salmonella enterica"],
        )
        for number in range(9)
    ]
    numbers = {record.evidence_key: index for index, record in enumerate(records, 1)}

    class Client:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def generate(self, payload):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {
                "evidenceInterpretations": [
                    {
                        "number": item["number"],
                        "claim": "Salmonella enterica人体粪便队列记录感染相关结果。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "sampleAlignment": "stool",
                        "patientApplicability": "contextual",
                    }
                    for item in payload["evidence"]
                ]
            }

    client = Client()
    completed = await interpret_evidence(
        client,
        records=records,
        numbers=numbers,
        batch_size=4,
    )
    assert completed == set(range(1, 10))
    assert 2 <= client.max_active <= 3


@pytest.mark.asyncio
async def test_missing_evidence_interpretations_retry_in_batches_not_individually():
    records = [
        EvidenceRecord(
            evidence_key=f"partial-{number}",
            category="literature",
            title=f"Salmonella enterica infection {number}",
            source="Journal",
            summary="Salmonella enterica is associated with infection.",
            supports=["taxon-salmonella"],
            intents=["microbe_clinical_significance"],
            targets=["Salmonella enterica"],
        )
        for number in range(16)
    ]
    numbers = {record.evidence_key: index for index, record in enumerate(records, 1)}

    class Client:
        def __init__(self):
            self.main_batch_sizes = []

        async def generate(self, payload):
            if payload["task"].startswith("逐条解读EviMed"):
                self.main_batch_sizes.append(len(payload["evidence"]))
                item = payload["evidence"][0]
                return {
                    "evidenceInterpretations": [
                        {
                            "number": item["number"],
                            "claim": "Salmonella enterica与感染相关。",
                            "targetAlignment": "direct",
                            "taxonAlignment": "species",
                            "patientApplicability": "contextual",
                        }
                    ]
                }
            return {"evidenceInterpretations": []}

    client = Client()
    await interpret_evidence(
        client,
        records=records,
        numbers=numbers,
        batch_size=8,
    )
    assert len(client.main_batch_sizes) == 4
    assert sorted(client.main_batch_sizes) == [6, 8, 8, 8]


@pytest.mark.asyncio
async def test_management_phrase_cannot_be_rewritten_as_diagnostic_test():
    record = EvidenceRecord(
        evidence_key="management-guide",
        category="guideline",
        title="Guideline for the management of chronic coronary disease",
        source="Society",
        summary="The guideline supports management of chronic coronary disease.",
        supports=["disease-coronary"],
        intents=["diagnostic_guideline"],
        targets=["冠心病"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南讨论慢性冠心病管理。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [
                            {
                                "name": "慢性冠心病评估",
                                "rationale": "用于确认冠心病。",
                                "sourcePhrase": "management of chronic coronary disease",
                                "purpose": "screen_target",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"management-guide": 1}
    )
    assert record.interpretation.recommended_evaluations == []


@pytest.mark.asyncio
async def test_guideline_recommending_against_screening_cannot_create_check():
    record = EvidenceRecord(
        evidence_key="against-screening",
        category="guideline",
        title="Recommendation on screening adults for depression",
        source="Task force",
        summary="The task force recommends against screening using a questionnaire.",
        supports=["disease-depression"],
        intents=["diagnostic_guideline"],
        targets=["抑郁症"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "工作组不建议常规问卷筛查。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [
                            {
                                "name": "抑郁问卷筛查",
                                "rationale": "用于筛查。",
                                "sourcePhrase": "screening using a questionnaire",
                                "purpose": "screen_target",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"against-screening": 1}
    )
    assert record.interpretation.recommended_evaluations == []


@pytest.mark.asyncio
async def test_evaluation_with_self_negating_rationale_is_removed():
    record = EvidenceRecord(
        evidence_key="indirect-hba1c",
        category="guideline",
        title="Position statement on glucose testing and HbA1c",
        source="Society",
        summary="The statement mentions glycated haemoglobin (HbA1c).",
        supports=["disease-hba1c"],
        intents=["diagnostic_guideline"],
        targets=["糖化血红蛋白"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该声明提及糖化血红蛋白。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [
                            {
                                "name": "糖化血红蛋白（HbA1c）",
                                "rationale": "摘要提及该指标，但未作为目标直接筛查依据。",
                                "sourcePhrase": "glycated haemoglobin (HbA1c)",
                                "purpose": "characterize_target",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"indirect-hba1c": 1})
    assert record.interpretation.recommended_evaluations == []


@pytest.mark.asyncio
async def test_post_load_glucose_name_is_medically_normalized():
    record = EvidenceRecord(
        evidence_key="post-load",
        category="guideline",
        title="Position statement on 1-hour post-load plasma glucose",
        source="IDF",
        summary="Use 1-hour post-load plasma glucose during a 75-g OGTT.",
        supports=["disease-diabetes"],
        intents=["diagnostic_guideline"],
        targets=["II型糖尿病"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该声明讨论2型糖尿病诊断。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [
                            {
                                "name": "1小时餐后血糖",
                                "rationale": "用于诊断。",
                                "sourcePhrase": "1-hour post-load plasma glucose",
                                "purpose": "confirm_target",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"post-load": 1})
    assert record.interpretation.recommended_evaluations[0].name == (
        "75克口服葡萄糖耐量试验1小时血浆葡萄糖"
    )


@pytest.mark.asyncio
async def test_direct_guideline_gets_bounded_retry_for_missing_evaluation():
    record = EvidenceRecord(
        evidence_key="ibd-colonoscopy",
        category="guideline",
        title="Guideline for diagnosis of inflammatory bowel disease",
        source="Society",
        summary="Diagnostic evaluation includes colonoscopy.",
        supports=["disease-ibd"],
        intents=["diagnostic_guideline"],
        targets=["炎症性肠病"],
    )

    class Client:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if payload["task"].startswith("仅从该指南"):
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": "该指南讨论炎症性肠病诊断。",
                            "targetAlignment": "direct",
                            "recommendedEvaluations": [
                                {
                                    "name": "结肠镜检查",
                                    "rationale": "用于炎症性肠病诊断评估。",
                                    "sourcePhrase": "colonoscopy",
                                    "purpose": "confirm_target",
                                }
                            ],
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南讨论炎症性肠病诊断。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [],
                    }
                ]
            }

    client = Client()
    await interpret_evidence(client, records=[record], numbers={"ibd-colonoscopy": 1})
    assert client.calls == 2
    assert [item.name for item in record.interpretation.recommended_evaluations] == [
        "结肠镜检查"
    ]


@pytest.mark.asyncio
async def test_missing_evaluation_retry_checks_multiple_ranked_sources_per_disease():
    records = [
        EvidenceRecord(
            evidence_key=f"diabetes-guide-{index}",
            category="guideline",
            title=f"Type 2 diabetes diagnostic guideline {index}",
            source="Society",
            year=2026 - index,
            summary=(
                "Diagnosis of type 2 diabetes includes fasting plasma glucose."
                if index == 2
                else "This guideline discusses diagnosis of type 2 diabetes."
            ),
            supports=["disease-diabetes"],
            diseaseIds=["disease-diabetes"],
            intents=["diagnostic_guideline"],
            targets=["2型糖尿病"],
        )
        for index in range(1, 4)
    ]

    class Client:
        def __init__(self):
            self.retry_evidence_count = 0

        async def generate(self, payload):
            if payload["task"].startswith("仅从该指南"):
                self.retry_evidence_count = len(payload["evidence"])
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 2,
                            "claim": "该指南讨论2型糖尿病诊断。",
                            "targetAlignment": "direct",
                            "recommendedEvaluations": [
                                {
                                    "name": "空腹血糖",
                                    "rationale": "用于2型糖尿病诊断评估。",
                                    "sourcePhrase": "fasting plasma glucose",
                                    "purpose": "confirm_target",
                                }
                            ],
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": index,
                        "claim": "该指南讨论2型糖尿病诊断。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [],
                    }
                    for index in range(1, 4)
                ]
            }

    client = Client()
    await interpret_evidence(
        client,
        records=records,
        numbers={record.evidence_key: index for index, record in enumerate(records, 1)},
    )

    assert client.retry_evidence_count == 3
    assert [
        item.name for item in records[1].interpretation.recommended_evaluations
    ] == ["空腹血糖"]


@pytest.mark.asyncio
async def test_microbe_condition_retry_rewrites_claim_before_relation_binding():
    record = EvidenceRecord(
        evidence_key="suis-disease",
        category="guideline",
        title="人感染猪链球菌病诊疗方案",
        source="Authority",
        summary="人感染猪链球菌病是一种由猪链球菌引起的人畜共患病。",
        supports=["taxon-suis"],
        microbeIds=["taxon-suis"],
        intents=["microbe_clinical_significance"],
        targets=["Streptococcus suis / 猪链球菌"],
    )

    class Client:
        async def generate(self, payload):
            if payload["task"].startswith("仅从该EviMed标题摘要中补抽取"):
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": "猪链球菌可导致人感染猪链球菌病。",
                            "conditions": [
                                {
                                    "name": "人感染猪链球菌病",
                                    "searchTerm": "人感染猪链球菌病",
                                    "sourcePhrase": "人感染猪链球菌病",
                                    "relationType": "established_pathogen",
                                }
                            ],
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "猪链球菌是人畜共患病病原体。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"suis-disease": 1})
    assert record.interpretation.claim == "猪链球菌可导致人感染猪链球菌病。"
    assert [item.name for item in record.interpretation.conditions] == [
        "人感染猪链球菌病"
    ]


@pytest.mark.asyncio
async def test_subtype_test_cannot_be_generalized_to_broad_sleep_target():
    record = EvidenceRecord(
        evidence_key="sleep-breathing",
        category="guideline",
        title="Practice parameters for polysomnography",
        source="Society",
        summary=(
            "Polysomnography is routinely indicated for the diagnosis of "
            "sleep-related breathing disorders."
        ),
        supports=["disease-sleep"],
        intents=["diagnostic_guideline"],
        targets=["睡眠障碍"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "多导睡眠图用于睡眠相关呼吸障碍诊断。",
                        "targetAlignment": "direct",
                        "recommendedEvaluations": [
                            {
                                "name": "多导睡眠图",
                                "rationale": "用于诊断睡眠相关呼吸障碍。",
                                "sourcePhrase": (
                                    "Polysomnography is routinely indicated for the "
                                    "diagnosis of sleep-related breathing disorders"
                                ),
                                "purpose": "screen_target",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"sleep-breathing": 1})
    assert record.interpretation.recommended_evaluations == []
    assert record.interpretation.target_alignment == "indirect"


@pytest.mark.asyncio
async def test_association_claim_must_name_the_target_taxon():
    record = EvidenceRecord(
        evidence_key="wrong-taxon",
        category="literature",
        title="Candidate probiotic study mentioning Candida in supplementary results",
        source="Journal",
        summary="The main finding concerns Lactiplantibacillus plantarum.",
        supports=["taxon-candida"],
        intents=["microbe_association"],
        targets=["Candida"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "植物乳杆菌可影响宿主肠道菌群。",
                        "targetAlignment": "indirect",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"wrong-taxon": 1})
    assert record.interpretation.target_alignment == "not_applicable"


@pytest.mark.asyncio
async def test_discovery_interpretation_cannot_swap_batch_targets():
    record = EvidenceRecord(
        evidence_key="staph-guide",
        category="guideline",
        title="Guideline on Staphylococcus aureus infection",
        source="Society",
        summary="The guideline discusses Staphylococcus aureus infection.",
        supports=["taxon-staph"],
        intents=["microbe_discovery"],
        targets=["Staphylococcus aureus"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "Streptococcus suis is a zoonotic pathogen.",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"staph-guide": 1})
    assert record.interpretation.target_alignment == "not_applicable"


@pytest.mark.asyncio
async def test_direct_adult_disease_evidence_is_contextual_when_model_marks_patient_not_applicable():
    record = EvidenceRecord(
        evidence_key="crc-clinical",
        category="literature",
        title="Clinical features of colorectal cancer in adults",
        source="Journal",
        summary="Colorectal cancer may present with rectal bleeding and changed bowel habits.",
        supports=["disease-crc"],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "结直肠癌的临床表现可包括便血和排便习惯改变。",
                        "targetAlignment": "direct",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"crc-clinical": 1})
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"


@pytest.mark.asyncio
async def test_direct_adult_health_guideline_is_contextual_when_model_marks_not_applicable():
    record = EvidenceRecord(
        evidence_key="adult-activity-guide",
        category="guideline",
        title="中国成人身体活动指南",
        source="学会",
        summary="指南提供成人身体活动的健康建议。",
        supports=["health-exercise"],
        intents=["health_management"],
        targets=["健康成人运动调护"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南提供成人身体活动建议。",
                        "targetAlignment": "not_applicable",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={"adult-activity-guide": 1},
    )
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"


@pytest.mark.asyncio
async def test_exact_lipid_guideline_is_direct_when_model_marks_it_indirect():
    record = EvidenceRecord(
        evidenceKey="lipid-lifestyle-guide",
        category="guideline",
        title="ESC/EAS成人血脂异常管理指南",
        source="Society",
        summary=(
            "指南强调血脂异常的生活方式干预包括饮食，"
            "饱和脂肪酸是影响LDL-C水平的重要饮食因素。"
        ),
        supports=["health-management"],
        intents=["health_management"],
        targets=["血脂异常相关膳食调护"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南说明血脂异常相关膳食调护。",
                        "targetAlignment": "indirect",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={"lipid-lifestyle-guide": 1},
    )
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"


@pytest.mark.asyncio
async def test_unrelated_diabetes_lifestyle_guide_does_not_cover_lipid_target():
    record = EvidenceRecord(
        evidenceKey="diabetes-lifestyle-guide",
        category="guideline",
        title="Adult diabetes lifestyle guideline",
        source="Society",
        summary="The guideline recommends diet and physical activity for diabetes.",
        supports=["health-management"],
        intents=["health_management"],
        targets=["血脂异常相关膳食调护"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南讨论糖尿病饮食管理。",
                        "targetAlignment": "indirect",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={"diabetes-lifestyle-guide": 1},
    )
    assert record.interpretation.target_alignment == "indirect"
    assert record.interpretation.patient_applicability == "not_applicable"


@pytest.mark.asyncio
async def test_weak_disease_claim_is_rewritten_from_same_evimed_summary():
    record = EvidenceRecord(
        evidence_key="crc-clinical-retry",
        category="literature",
        title="Clinical features of colorectal cancer in adults",
        source="Journal",
        summary="Colorectal cancer may present with rectal bleeding and changed bowel habits.",
        supports=["disease-crc"],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
    )

    class Client:
        def __init__(self):
            self.tasks = []

        async def generate(self, payload):
            self.tasks.append(payload["task"])
            if payload["task"] == "仅从EviMed标题摘要中重写疾病或健康问题的医学含义":
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": "结直肠癌的临床表现可包括便血和排便习惯改变。",
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "结直肠癌是该文献直接讨论的疾病。",
                        "targetAlignment": "direct",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    client = Client()
    await interpret_evidence(
        client,
        records=[record],
        numbers={"crc-clinical-retry": 1},
    )
    assert "仅从EviMed标题摘要中重写疾病或健康问题的医学含义" in client.tasks
    assert record.interpretation.claim == "结直肠癌的临床表现可包括便血和排便习惯改变。"


@pytest.mark.asyncio
async def test_public_disease_repair_removes_ranking_that_final_gate_rejects():
    record = EvidenceRecord(
        evidence_key="crc-overview-ranking",
        category="literature",
        title="Colorectal cancer.",
        source="Journal",
        summary=(
            "Colorectal cancer is the third most common cancer worldwide. "
            "The review also discusses treatment advances."
        ),
        supports=["disease-crc"],
        disease_ids=["disease-crc"],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌是全球第三大常见癌症，治疗方式正在进展。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    class Client:
        async def generate(self, payload):
            assert payload["task"] == "仅从EviMed标题摘要中重写疾病或健康问题的医学含义"
            assert any("删除排名" in rule for rule in payload["strictConstraints"])
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "结直肠癌是一种常见的恶性肿瘤。",
                    }
                ]
            }

    await repair_disease_evidence_claims(
        Client(),
        records=[record],
        numbers={"crc-overview-ranking": 1},
    )

    assert record.interpretation.claim == "结直肠癌是一种常见的恶性肿瘤。"


@pytest.mark.asyncio
async def test_public_disease_repair_keeps_source_bounded_gut_association():
    record = EvidenceRecord(
        evidence_key="depression-gut-association",
        category="literature",
        title="The gut microbiota in anxiety and depression - A systematic review",
        source="Journal",
        summary=(
            "Depression was associated with differences in gut microbiota "
            "composition in human studies."
        ),
        supports=["disease-depression"],
        disease_ids=["disease-depression"],
        intents=["disease_clinical_significance"],
        targets=["抑郁症"],
        interpretation={
            "claim": "该文献讨论抑郁症与肠道菌群。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    class Client:
        async def generate(self, payload):
            assert any(
                "允许将该人群关联写入claim" in rule
                for rule in payload["strictConstraints"]
            )
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "抑郁症在人群研究中与肠道菌群组成差异相关。",
                    }
                ]
            }

    await repair_disease_evidence_claims(
        Client(),
        records=[record],
        numbers={"depression-gut-association": 1},
    )

    assert record.interpretation.claim == "抑郁症在人群研究中与肠道菌群组成差异相关。"
    assert _public_disease_claim(record) == "抑郁症在人群研究中与肠道菌群组成差异相关。"


@pytest.mark.asyncio
async def test_exact_human_gut_disease_source_recovers_indirect_model_alignment():
    record = EvidenceRecord(
        evidence_key="parkinson-gut-review",
        category="literature",
        title="Gut microbiota and Parkinson disease: a systematic review",
        source="Journal",
        summary=(
            "Human studies reported gut microbiota differences associated with "
            "Parkinson disease."
        ),
        supports=["disease-parkinson"],
        disease_ids=["disease-parkinson"],
        intents=["disease_clinical_significance"],
        targets=["帕金森"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "帕金森病在人群研究中与肠道菌群组成差异相关。",
                        "targetAlignment": "indirect",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={"parkinson-gut-review": 1},
    )

    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"


@pytest.mark.asyncio
async def test_exact_disease_source_recovers_from_model_not_applicable_alignment():
    record = EvidenceRecord(
        evidence_key="constipation-clinical-retry",
        category="guideline",
        title="2022 Seoul Consensus on Clinical Practice Guidelines for Functional Constipation",
        source="Society",
        summary=(
            "Constipation manifests as infrequent bowel movements, hard stools, "
            "incomplete evacuation, and straining at defecation."
        ),
        supports=["disease-constipation"],
        diseaseIds=["disease-constipation"],
        intents=["disease_clinical_significance"],
        targets=["便秘"],
    )

    class Client:
        async def generate(self, payload):
            if payload["task"] == "仅从EviMed标题摘要中重写疾病或健康问题的医学含义":
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": "便秘的临床表现可包括排便次数减少、粪便干硬和排便费力。",
                        }
                    ]
                }
            if (
                payload["task"]
                == "仅从该指南标题摘要中补抽取可直接确认或筛查目标的检查项目"
            ):
                return {"evidenceInterpretations": []}
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南介绍便秘。",
                        "targetAlignment": "not_applicable",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"constipation-clinical-retry": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"
    assert record.interpretation.claim == (
        "便秘的临床表现可包括排便次数减少、粪便干硬和排便费力。"
    )


@pytest.mark.asyncio
async def test_disease_meaning_does_not_require_a_microbiome_relation():
    record = EvidenceRecord(
        evidence_key="nafld-clinical-retry",
        category="guideline",
        title="Non-alcoholic fatty liver disease in adults 2021",
        source="Society",
        summary=(
            "Nonalcoholic fatty liver disease is a common progressive liver disease "
            "that can lead to cirrhosis, hepatocellular carcinoma, and end-stage liver disease."
        ),
        supports=["disease-nafld"],
        diseaseIds=["disease-nafld"],
        intents=["disease_clinical_significance"],
        targets=["非酒精性脂肪肝"],
    )

    class Client:
        async def generate(self, payload):
            if payload["task"] == "仅从EviMed标题摘要中重写疾病或健康问题的医学含义":
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": (
                                "非酒精性脂肪肝是进行性肝病，"
                                "可进展为肝硬化、肝细胞癌或终末期肝病。"
                            ),
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南未讨论菌群。",
                        "targetAlignment": "indirect",
                        "patientApplicability": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"nafld-clinical-retry": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"
    assert record.interpretation.claim.startswith("非酒精性脂肪肝是进行性肝病")


@pytest.mark.asyncio
async def test_alzheimer_chinese_spelling_variant_is_exact_target_evidence():
    record = EvidenceRecord(
        evidence_key="alzheimer-spelling-variant",
        category="guideline",
        title="阿尔茨海默病居家筛查专家共识",
        source="Society",
        summary=(
            "阿尔茨海默病是常见的神经退行性疾病，可出现进行性记忆和认知功能障碍。"
        ),
        supports=["disease-ad"],
        diseaseIds=["disease-ad"],
        intents=["disease_clinical_significance"],
        targets=["阿尔兹海默症"],
    )

    class Client:
        async def generate(self, payload):
            if payload["task"] == "仅从EviMed标题摘要中重写疾病或健康问题的医学含义":
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": (
                                "阿尔兹海默症是神经退行性疾病，"
                                "可出现进行性记忆和认知功能障碍。"
                            ),
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "来源使用不同中文译名。",
                        "targetAlignment": "not_applicable",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"alzheimer-spelling-variant": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.claim.startswith("阿尔兹海默症是神经退行性疾病")


@pytest.mark.asyncio
async def test_exact_diagnostic_source_recovers_evaluation_from_model_not_applicable_alignment():
    record = EvidenceRecord(
        evidence_key="constipation-diagnostic-retry",
        category="guideline",
        title="2022 Seoul Consensus on Clinical Practice Guidelines for Functional Constipation",
        source="Society",
        summary=(
            "During the diagnosis of chronic constipation, colonoscopy and "
            "anorectal manometry are useful for objective assessment."
        ),
        supports=["disease-constipation"],
        diseaseIds=["disease-constipation"],
        intents=["diagnostic_guideline"],
        targets=["便秘"],
    )

    class Client:
        async def generate(self, payload):
            if (
                payload["task"]
                == "仅从该指南标题摘要中补抽取可直接确认或筛查目标的检查项目"
            ):
                return {
                    "evidenceInterpretations": [
                        {
                            "number": 1,
                            "claim": "便秘的诊断可进行客观评估。",
                            "targetAlignment": "direct",
                            "recommendedEvaluations": [
                                {
                                    "name": "肛门直肠测压",
                                    "rationale": "用于客观评估便秘相关排便功能。",
                                    "sourcePhrase": "anorectal manometry",
                                    "purpose": "characterize_target",
                                }
                            ],
                        }
                    ]
                }
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "该指南讨论便秘的诊断。",
                        "targetAlignment": "not_applicable",
                        "patientApplicability": "not_applicable",
                        "recommendedEvaluations": [],
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"constipation-diagnostic-retry": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.patient_applicability == "contextual"
    assert [item.name for item in record.interpretation.recommended_evaluations] == [
        "肛门直肠测压"
    ]


def sections():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                value="0.1",
                detection_status="detected",
            )
        ],
        disease_results=[
            ParsedDisease(
                disease_id="allowed-disease",
                name="便秘",
                source_score="55",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    microbes.items[0].evidence_refs = [1]
    return microbes, build_source_diseases(parsed, [])


@pytest.mark.asyncio
async def test_real_narrative_prompt_requests_evidence_scoped_microbe_summaries():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.interpretation_evidence_refs = [1]
    microbe.association_evidence_refs = [1]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体研究显示肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        )
    ]
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                chinese_name="肠沙门菌",
                value="0.1",
                detection_status="detected",
            )
        ],
        enterotype="肠型II",
    )

    class Client:
        def __init__(self):
            self.payloads = []

        async def generate(self, payload):
            self.payloads.append(payload)
            return {
                "executiveSummary": _valid_executive_summary(),
                "abnormalMicrobesSummary": _valid_microbe_summary(),
                "microbeNarratives": [
                    {
                        "microbeId": "allowed-taxon",
                        "clinicalRole": "肠沙门菌具有明确的人体病原学意义。",
                        "conclusion": "肠沙门菌是本次需要关注的病原相关菌种。",
                        "patientInterpretation": (
                            "人体研究显示肠沙门菌与沙门菌病相关。"
                            "其医学意义需要按照病原学属性和本次肠道样本背景综合理解。"
                        ),
                    }
                ],
                "healthLevel": {
                    "level": "中度关注",
                    "summary": "本次肠道健康关注程度主要来自病原相关菌信号。",
                    "basis": [],
                },
                "healthProfile": "本次全身健康概况需要结合现有检查与肠道微生态共同理解。",
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        session_context={},
        evidence_summaries=[
            {
                "number": 1,
                "title": "Human salmonellosis study",
                "summary": "Human evidence links Salmonella enterica to salmonellosis.",
                "intents": ["microbe_disease_association"],
                "interpretation": {
                    "claim": "人体研究显示肠沙门菌与沙门菌病相关。",
                    "targetAlignment": "direct",
                },
            }
        ],
        enterotype="肠型II",
    )

    first = client.payloads[0]
    assert "microbeNarratives" in first["requiredOutput"]
    assert first["microbeNarrativeEvidence"][0]["microbeId"] == "allowed-taxon"
    assert "medicalInterpretation" not in first["microbeWhitelist"][0]
    assert draft.microbe_narratives[0].patient_interpretation.startswith(
        "人体研究显示肠沙门菌与沙门菌病相关"
    )
    assert draft.microbe_narratives[0].generation_source == "model"
    assert microbe.model_interpretation_required is True


@pytest.mark.asyncio
async def test_model_disease_narrative_is_requested_and_preserved():
    microbes, diseases = sections()
    disease = diseases.items[0]
    disease.association_evidence_refs = [1]
    disease.evidence_refs = [1]
    parsed = ParsedMicrobiomeReport(
        sourceType="microbiome_data",
        sourceName="source.json",
        subject=SubjectInfo(),
        diseaseResults=[
            ParsedDisease(
                diseaseId=disease.disease_id,
                name=disease.name,
                sourceScore="55",
            )
        ],
    )

    class Client:
        def __init__(self):
            self.payloads = []

        async def generate(self, payload):
            self.payloads.append(payload)
            return {
                "executiveSummary": _valid_executive_summary(disease="便秘"),
                "abnormalMicrobesSummary": _valid_microbe_summary(),
                "diseaseNarratives": [
                    {
                        "diseaseId": disease.disease_id,
                        "relatedContent": (
                            "本次检测呈现与便秘相关的菌群变化。"
                            "人体研究观察到便秘与肠道菌群组成变化存在关联，"
                            "因此可以适当关注排便困难等相关症状和胃肠功能变化。"
                        ),
                    }
                ],
                "healthLevel": {
                    "level": "中度关注",
                    "summary": "本次肠道健康关注程度主要来自已有关联信息。",
                    "basis": [],
                },
                "healthProfile": "本次资料包含肠道微生态和便秘相关健康信息。",
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        session_context={},
        evidence_summaries=[
            {
                "number": 1,
                "title": "Human gut microbiome and constipation",
                "summary": "Human studies report gut microbiome differences in constipation.",
                "intents": ["disease_clinical_significance"],
                "interpretation": {
                    "claim": "人体研究提示便秘与肠道菌群组成变化存在关联。",
                    "targetAlignment": "direct",
                },
            }
        ],
    )

    assert client.payloads[0]["diseaseNarrativeEvidence"][0]["diseaseId"] == (
        disease.disease_id
    )
    assert draft.disease_narratives[0].related_content.startswith(
        "本次检测呈现与便秘相关的菌群变化"
    )
    assert "原报告列出" not in draft.disease_narratives[0].related_content


@pytest.mark.asyncio
async def test_contextual_stool_relation_retry_is_single_item_and_excludes_global_fields():
    microbes, diseases = sections()
    microbe = microbes.items[0]
    microbe.interpretation_evidence_refs = [1]
    microbe.evidence_refs = [1]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="人体研究显示肠沙门菌与沙门菌病相关。",
            evidenceRefs=[1],
            patientApplicability="contextual",
            sampleAlignment="stool",
        )
    ]
    parsed = ParsedMicrobiomeReport(
        sourceType="microbiome_data",
        sourceName="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxonId="allowed-taxon",
                category="pathogenic",
                latinName="Salmonella enterica",
                chineseName="肠沙门菌",
                value="0.1",
                detectionStatus="detected",
            )
        ],
        enterotype="肠型II",
    )

    class Client:
        def __init__(self):
            self.payloads = []
            self.microbe_retry_count = 0

        async def generate(self, payload):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return {
                    "executiveSummary": _valid_executive_summary(),
                    "abnormalMicrobesSummary": _valid_microbe_summary(),
                    "microbeNarratives": [
                        {
                            "microbeId": "allowed-taxon",
                            "clinicalRole": "肠沙门菌具有病原学意义。",
                            "conclusion": "肠沙门菌是本次需要关注的菌种。",
                        }
                    ],
                    "healthLevel": {
                        "level": "中度关注",
                        "summary": "本次肠道健康关注程度来自病原相关菌信号。",
                        "basis": [],
                    },
                    "healthProfile": "现有健康资料提示需关注肠道微生态状态。",
                }
            if payload["task"].startswith("根据已完成的医学报告"):
                return {"executiveSummary": _valid_executive_summary()}
            if payload["task"].startswith("只生成简洁健康画像与建议关注方向"):
                return {
                    "healthProfile": "现有健康资料提示需关注肠道微生态状态。",
                    "recommendations": [],
                }
            assert payload["task"].startswith("只为指定菌种")
            assert set(payload["requiredOutput"]) == {"microbeNarratives"}
            assert payload["retryOnly"]["microbeIds"] == ["allowed-taxon"]
            assert payload["mustMentionRiskNames"] == {
                "allowed-taxon": ["沙门菌病"]
            }
            self.microbe_retry_count += 1
            if self.microbe_retry_count == 1:
                return {
                    "microbeNarratives": [
                        {
                            "microbeId": "allowed-taxon",
                            "clinicalRole": "肠沙门菌具有病原学意义。",
                            "conclusion": "肠沙门菌是本次需要关注的菌种。",
                            "patientInterpretation": (
                                "您的粪便样本中检出了该菌。"
                                "沙门菌病是其已展示的健康影响。"
                            ),
                        }
                    ]
                }
            return {
                "microbeNarratives": [
                    {
                        "microbeId": "allowed-taxon",
                        "clinicalRole": "肠沙门菌具有病原学意义。",
                        "conclusion": "肠沙门菌是本次需要关注的菌种。",
                        "patientInterpretation": (
                            "肠沙门菌具有病原学意义，"
                            "人体研究显示其与沙门菌病相关。"
                        ),
                    }
                ]
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        session_context={},
        evidence_summaries=[
            {
                "number": 1,
                "title": "Human salmonellosis study",
                "summary": "Human evidence links Salmonella to salmonellosis.",
                "intents": ["microbe_disease_association"],
                "interpretation": {
                    "claim": "人体研究显示肠沙门菌与沙门菌病相关。",
                    "targetAlignment": "direct",
                },
            }
        ],
        enterotype="肠型II",
    )

    microbe_retry_payloads = [
        payload
        for payload in client.payloads
        if payload["task"].startswith("只为指定菌种")
    ]
    assert len(microbe_retry_payloads) == 2
    assert draft.microbe_narratives[0].generation_source == "model"


@pytest.mark.asyncio
async def test_narrative_item_retries_are_limited_and_fail_independently():
    class Client:
        def __init__(self):
            self.active = 0
            self.maximum_active = 0

        async def generate(self, payload):
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.01)
                if payload["id"] == 2:
                    raise NarrativeError("one row failed")
                return {"id": payload["id"]}
            finally:
                self.active -= 1

    client = Client()
    results = await _generate_retry_payloads(
        client,
        [{"id": index} for index in range(5)],
        concurrency=2,
    )

    assert client.maximum_active == 2
    assert [result["id"] for result in results if isinstance(result, dict)] == [
        0,
        1,
        3,
        4,
    ]
    assert isinstance(results[2], NarrativeError)


def test_low_abundance_pathogen_is_not_sent_for_generic_pathogen_narrative():
    microbe = AbnormalMicrobeItem(
        microbeId="low-pathogen",
        name="金黄色葡萄球菌",
        scientificName="Staphylococcus aureus",
        category="pathogenic_signal",
        sourceFinding={"status": "丰度低"},
        diseaseAndRisks=[
            RiskAssociation(
                name="菌血症",
                description="人体病原学资料记录该菌可引起菌血症。",
                relationType="established_pathogen",
                evidenceRefs=[1],
            )
        ],
        evidenceRefs=[1],
        interpretationEvidenceRefs=[1],
    )

    assert not _microbe_has_public_narrative_evidence(
        microbe,
        evidence_by_number={
            1: {"interpretation": {"direction": "increased"}}
        },
    )


def test_low_abundance_pathogen_can_use_matching_decreased_human_association():
    microbe = AbnormalMicrobeItem(
        microbeId="low-associated-taxon",
        name="示例菌",
        scientificName="Example species",
        category="pathogenic_signal",
        sourceFinding={"status": "丰度低"},
        diseaseAndRisks=[
            RiskAssociation(
                name="示例疾病",
                description="人群研究显示该菌减少与示例疾病相关。",
                relationType="epidemiologic_association",
                evidenceRefs=[1],
                patientApplicability="direct",
                sampleAlignment="stool",
            )
        ],
        evidenceRefs=[1],
        interpretationEvidenceRefs=[1],
    )

    assert _microbe_has_public_narrative_evidence(
        microbe,
        evidence_by_number={
            1: {"interpretation": {"direction": "decreased"}}
        },
    )


def test_contextual_other_site_evidence_is_not_ai_narrative_target():
    microbe = AbnormalMicrobeItem(
        microbeId="contextual-taxon",
        name="示例菌",
        scientificName="Example species",
        category="beneficial_abnormal",
        sourceFinding={"status": "丰度低"},
        diseaseAndRisks=[
            RiskAssociation(
                name="示例疾病",
                description="人体其他部位研究记录该菌。",
                relationType="epidemiologic_association",
                evidenceRefs=[1],
                patientApplicability="contextual",
                sampleAlignment="other",
            )
        ],
        evidenceRefs=[1],
        interpretationEvidenceRefs=[1],
    )

    assert not _microbe_has_public_narrative_evidence(
        microbe,
        evidence_by_number={1: {"interpretation": {"direction": "decreased"}}},
    )


def test_direct_human_organism_role_is_ai_narrative_target_without_disease():
    microbe = AbnormalMicrobeItem(
        microbeId="direct-role-taxon",
        name="示例菌",
        scientificName="Example species",
        category="other_abnormal",
        sourceFinding={"status": "偏低"},
        diseaseAndRisks=[],
        evidenceRefs=[1],
        interpretationEvidenceRefs=[1],
    )

    assert _microbe_has_public_narrative_evidence(
        microbe,
        evidence_by_number={
            1: {
                "microbeIds": ["direct-role-taxon"],
                "intents": ["microbe_clinical_significance"],
                "interpretation": {
                    "claim": "人体肠道资料支持该菌的菌群功能解读。",
                    "targetAlignment": "direct",
                    "patientApplicability": "direct",
                },
            }
        },
    )


def test_extractive_summary_uses_validated_item_conclusion():
    microbes, _diseases = sections()
    summary = _extractive_microbe_summary(
        microbes=microbes,
        conclusions={"allowed-taxon": "原报告检出肠沙门菌信号，数值为0.1%。"},
        has_session_context=False,
    )
    assert summary == "病原相关菌：原报告检出肠沙门菌信号，数值为0.1%。"


def test_large_extractive_summary_does_not_depend_on_model_item_conclusions():
    microbes, _diseases = sections()
    template = microbes.items[0]
    microbes.items = [
        template.model_copy(
            update={
                "microbe_id": f"taxon-{index}",
                "name": f"菌种{index}",
                "scientific_name": f"Species {index}",
                "category": (
                    "pathogenic_signal"
                    if index < 4
                    else "opportunistic_signal"
                    if index < 8
                    else "fungi_pathogenic_signal"
                ),
            }
        )
        for index in range(12)
    ]
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="感染性腹泻",
            description="人体证据记录该菌与感染性腹泻相关。",
            evidenceRefs=[1],
            patientApplicability="direct",
            sampleAlignment="stool",
        ),
        RiskAssociation(
            name="腹泻",
            description="其他样本研究记录该菌与腹泻相关。",
            evidenceRefs=[2],
            patientApplicability="contextual",
            sampleAlignment="other",
        ),
    ]
    summary = _extractive_microbe_summary(
        microbes=microbes,
        conclusions={},
        has_session_context=False,
    )
    assert summary is not None
    assert 150 <= len("".join(summary.split())) <= 400
    assert "感染性腹泻" in summary
    assert all(
        label in summary for label in ("病原相关菌", "条件致病菌", "潜在致病真菌")
    )
    assert not re.search(r"共\d+项|记录\d+项|检出\d+项", summary)


@pytest.mark.parametrize("outcome", ["致病性", "临床致病性", "长期死亡率"])
def test_generic_microbe_outcome_is_not_published_as_disease(outcome):
    assert _condition_is_generic_or_interventional(
        name=outcome,
        search_term=outcome,
        source=f"Yersinia enterocolitica {outcome}",
        relation_type="epidemiologic_association",
        targets=["Yersinia enterocolitica / 小肠结肠炎耶尔森氏菌"],
    )


def test_source_fact_microbe_fallback_adds_no_medical_inference():
    microbes, _diseases = sections()
    assert _source_fact_microbe_conclusion(microbes.items[0]) == (
        "检测结果显示，肠沙门菌为检出信号，检测值为0.1%。"
    )


def test_source_fact_microbe_fallback_includes_available_reference_range():
    microbes, _diseases = sections()
    microbe = microbes.items[0]
    microbe.source_finding.reference_range = "0.2-1.5"
    assert _source_fact_microbe_conclusion(microbe) == (
        "检测结果显示，肠沙门菌为检出信号，检测值为0.1%，参考范围为0.2-1.5%。"
    )


def test_source_fact_microbe_narrative_compacts_repeated_pathogen_predicates():
    microbes, _diseases = sections()
    microbe = microbes.items[0]
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门菌可引起沙门菌病。",
            evidenceRefs=[1],
        ),
        RiskAssociation(
            name="肠热病",
            description="肠沙门菌是肠热病的病原体。",
            evidenceRefs=[1],
        ),
        RiskAssociation(
            name="胃肠炎",
            description="肠沙门菌是胃肠炎的病原体。",
            evidenceRefs=[1],
        ),
    ]

    narrative = _source_fact_microbe_narrative(microbe)
    assert narrative.patient_interpretation == (
        "肠沙门菌可引起沙门菌病，也是肠热病、胃肠炎的病原体。"
    )


def test_source_fact_microbe_narrative_combines_specific_role_and_outcome():
    microbes, _diseases = sections()
    microbe = microbes.items[0]
    microbe.clinical_role = (
        "人体临床资料将该菌归为肠道病原相关菌，其临床意义取决于菌株和宿主背景。"
    )
    microbe.disease_and_risks = [
        RiskAssociation(
            name="沙门菌病",
            description="肠沙门菌可引起沙门菌病。",
            evidenceRefs=[1],
        )
    ]

    narrative = _source_fact_microbe_narrative(microbe)

    assert narrative.patient_interpretation == (
        "人体临床资料将该菌归为肠道病原相关菌，"
        "其临床意义取决于菌株和宿主背景。"
        "肠沙门菌可引起沙门菌病。"
    )


def test_source_fact_microbe_narrative_never_leaves_interpretation_empty():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-bifidobacterium",
        name="青春双歧杆菌",
        scientificName="Bifidobacterium adolescentis",
        category="beneficial_abnormal",
        sourceFinding={"status": "丰度低", "value": "0.0062", "unit": "%"},
    )

    narrative = _source_fact_microbe_narrative(microbe)

    assert narrative.patient_interpretation == (
        "检测结果显示，青春双歧杆菌为丰度低，检测值为0.0062%。"
        "目前仅可确认该项检测变化，尚无足够的人体医学依据"
        "解释其与具体疾病或不良影响的关系。"
    )


def test_narrative_cannot_add_ids_or_product_recommendations():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "本次检出沙门氏菌信号，需结合症状理解。",
            "microbeNarratives": [
                {"microbeId": "allowed-taxon", "conclusion": "原报告检出该菌DNA信号。"},
                {"microbeId": "invented", "conclusion": "虚构内容"},
            ],
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": "该项来自检测方模型。",
                }
            ],
            "healthLevel": {
                "level": "需关注",
                "summary": "存在多个需关注结果",
                "basis": [],
            },
            "recommendations": [
                {"category": "diet", "content": "增加蔬菜和全谷物的多样性。"},
                {"category": "general", "content": "每日服用益生菌胶囊。"},
            ],
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert [item.microbe_id for item in validated.microbe_narratives] == [
        "allowed-taxon"
    ]
    assert validated.recommendations == []


def test_recommendation_removes_source_titles_and_rejects_unsupported_details():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "diet",
                    "content": "建议控制脂肪摄入并增加膳食纤维。",
                    "evidenceRefs": [1],
                },
                {
                    "category": "exercise",
                    "content": "根据《中国健康生活方式指南》，建议保持规律身体活动。",
                    "evidenceRefs": [2],
                },
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {1},
            "exercise": {2},
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={
            1: "指南建议采用整体健康饮食模式。",
            2: "指南建议保持规律身体活动。",
        },
    )
    assert validated.recommendations == []


def test_recommendation_keeps_only_individually_supporting_evidence_refs():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "exercise",
                    "topic": "心血管与代谢健康",
                    "priority": "重点关注",
                    "content": "将有氧运动与抗阻训练结合。",
                    "evidenceRefs": [1, 2],
                }
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": set(),
            "exercise": {1, 2},
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={
            1: '{"summary":"人体证据提示体力活动不足可能增加长期心血管与代谢健康风险。"}',
            2: '{"summary":"指南建议将有氧运动与抗阻训练结合。"}',
        },
    )
    assert len(validated.recommendations) == 1
    assert validated.recommendations[0].evidence_refs == [2]


def test_health_attention_keeps_action_only_and_rejects_repeated_risk():
    microbes, diseases = sections()
    risk_only = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "general",
                    "topic": "心血管与代谢健康",
                    "priority": "重点关注",
                    "content": "持续异常可能增加长期心血管风险。",
                }
            ]
        }
    )
    assert validate_draft(
        risk_only,
        microbes=microbes,
        diseases=diseases,
    ).recommendations == []

    action_only = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "general",
                    "topic": "消化系统健康检查与随访",
                    "priority": "建议关注",
                    "content": (
                        "建议保持规律作息和均衡饮食，若出现腹痛、腹泻"
                        "或排便习惯改变等症状时，可考虑进一步检查。"
                    ),
                }
            ]
        }
    )
    action_validated = validate_draft(
        action_only,
        microbes=microbes,
        diseases=diseases,
    )
    assert len(action_validated.recommendations) == 1

    risk_and_direction = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "general",
                    "topic": "心血管与代谢健康",
                    "priority": "重点关注",
                    "content": (
                        "主要风险：持续异常可能增加长期心血管风险。"
                        "调养方向：优先管理饮食结构、体重和活动习惯。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(
        risk_and_direction,
        microbes=microbes,
        diseases=diseases,
    )
    assert validated.recommendations == []


def test_health_attention_cannot_invent_a_concrete_examination():
    microbes, diseases = sections()
    raw = {
        "recommendations": [
            {
                "category": "bowel_habits",
                "topic": "胃肠道与排便表现",
                "priority": "建议关注",
                "content": (
                    "主要风险：排便变化可能影响胃肠道舒适度。"
                    "检查方向：可优先补充布里斯托大便性状量表进行评估。"
                ),
            }
        ]
    }
    assert validate_draft(
        NarrativeDraft.model_validate(raw),
        microbes=microbes,
        diseases=diseases,
    ).recommendations == []

    validated = validate_draft(
        NarrativeDraft.model_validate(raw),
        microbes=microbes,
        diseases=diseases,
        recommended_examination_candidates={
            "exam-1": {
                "name": "布里斯托大便性状量表",
                "purpose": "评估",
                "evidenceRefs": [1],
            }
        },
    )
    assert validated.recommendations == []


def test_attention_fallback_does_not_repeat_disease_rows_or_scales():
    disease = DiseaseRiskItem(
        diseaseId="disease-constipation",
        name="便秘",
        sourceType="source_report",
        medicalInterpretation="便秘可能影响排便舒适度和日常生活。",
        associationEvidenceRefs=[1],
        suggestedEvaluations=[
            SuggestedEvaluation(
                name="布里斯托大便性状量表",
                purpose="评估",
                rationale="用于评估排便性状。",
                evidenceRefs=[1],
            )
        ],
    )
    result = _fallback_attention_directions(
        existing=[],
        microbes=AbnormalMicrobesSection(),
        diseases=DiseaseRisksSection(items=[disease]),
        health_assessment=None,
        available_numbers={1},
    )

    assert result == []


def test_recommendation_rejects_cross_source_composite_claim():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "exercise",
                    "content": "有氧联合抗阻运动有助于改善低密度脂蛋白胆固醇水平。",
                    "evidenceRefs": [1, 2],
                }
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": set(),
            "exercise": {1, 2},
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={
            1: '{"summary":"指南建议有氧联合抗阻运动。"}',
            2: '{"summary":"血脂指南记录低密度脂蛋白胆固醇管理目标。"}',
        },
    )
    assert validated.recommendations == []


def test_recommendations_do_not_combine_unsupported_action_details():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "exercise",
                    "content": "选择有氧运动并逐步增加活动量。",
                    "evidenceRefs": [1],
                },
                {
                    "category": "sleep",
                    "content": "固定就寝和起床时间，避免睡前使用电子设备。",
                    "evidenceRefs": [2, 3],
                },
                {
                    "category": "bowel_habits",
                    "content": "每天固定时间排便，避免憋便。",
                    "evidenceRefs": [4],
                },
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": set(),
            "exercise": {1},
            "sleep": {2, 3},
            "bowel_habits": {4},
            "general": set(),
        },
        health_evidence_text_by_number={
            1: '{"title":"身体活动指南","summary":"建议采用有氧运动。"}',
            2: '{"summary":"Available evidence supports consistent bedtimes and wake-up times."}',
            3: '{"summary":"Internet use before bed was associated with sleep difficulties."}',
            4: '{"title":"便秘指南","summary":"建议建立排便习惯。"}',
        },
    )
    assert validated.recommendations == []


def test_recommendation_removes_malformed_suggestion_lead_in():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "diet",
                    "content": "建议，保持食物多样，合理搭配。",
                    "evidenceRefs": [1],
                }
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {1},
            "exercise": set(),
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={
            1: "中国居民膳食指南建议食物要多样、合理搭配。"
        },
    )
    assert validated.recommendations == []


def test_recommendation_removes_generic_benefit_and_management_reference_tail():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "sleep",
                    "content": (
                        "保持规律的睡眠时间，尽量固定就寝和起床时间，"
                        "以支持整体健康和肠道功能。"
                    ),
                    "evidenceRefs": [1],
                },
                {
                    "category": "bowel_habits",
                    "content": (
                        "关注排便规律性，如出现便秘症状，"
                        "可参考慢性便秘管理原则进行生活方式调整。"
                    ),
                    "evidenceRefs": [2],
                },
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": set(),
            "exercise": set(),
            "sleep": {1},
            "bowel_habits": {2},
        },
        health_evidence_text_by_number={
            1: '{"summary":"Available evidence supports consistent bedtimes and wake-up times."}',
            2: '{"summary":"Guideline recommends regular bowel habits."}',
        },
    )
    assert validated.recommendations == []

    generic_effects = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "diet",
                    "content": "保持食物多样并合理搭配，保证合理营养。",
                    "evidenceRefs": [3],
                },
                {
                    "category": "sleep",
                    "content": "保持规律作息，有助于整体健康。",
                    "evidenceRefs": [4],
                },
                {
                    "category": "exercise",
                    "content": "定期进行有氧和力量练习，有助于促进身体活动和健康。",
                    "evidenceRefs": [5],
                },
                {
                    "category": "sleep",
                    "content": "保持规律作息，有助于维持健康。",
                    "evidenceRefs": [6],
                },
            ]
        }
    )
    validated_effects = validate_draft(
        generic_effects,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {3},
            "exercise": {5},
            "sleep": {4, 6},
            "bowel_habits": set(),
        },
        health_evidence_text_by_number={
            3: '{"summary":"指南建议保持食物多样并合理搭配。"}',
            4: '{"summary":"指南建议保持规律作息。"}',
            5: '{"summary":"指南建议定期进行有氧和力量练习。"}',
            6: '{"summary":"指南建议保持规律作息。"}',
        },
    )
    assert validated_effects.recommendations == []


def test_recommendations_reject_general_malformed_and_cross_topic_bowel_content():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "general",
                    "content": "保持均衡饮食和规律运动。",
                    "evidenceRefs": [1, 2],
                },
                {
                    "category": "exercise",
                    "content": "的建议，建议保持规律运动。",
                    "evidenceRefs": [2],
                },
                {
                    "category": "diet",
                    "content": "的指导原则，建议保持食物多样化。",
                    "evidenceRefs": [1],
                },
                {
                    "category": "bowel_habits",
                    "content": "保持规律排便可改善胆固醇和心血管状态。",
                    "evidenceRefs": [3],
                },
                {
                    "category": "bowel_habits",
                    "content": "有便意时及时如厕，保持规律排便习惯，如有不适可参考相关指南进行管理。",
                    "evidenceRefs": [3],
                },
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {1},
            "exercise": {2},
            "sleep": set(),
            "bowel_habits": {3},
            "general": {1, 2, 3},
        },
    )
    assert validated.recommendations == []


def test_recommendation_removes_unbracketed_guideline_lead_in():
    assert (
        _strip_evidence_title_prefix("参照中国居民膳食指南，保持平衡膳食结构。")
        == "保持平衡膳食结构。"
    )

    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "diet",
                    "content": "参照《中国居民膳食指南》的建议，保持食物多样化。",
                    "evidenceRefs": [1],
                }
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {1},
            "exercise": set(),
            "sleep": set(),
            "bowel_habits": set(),
            "general": set(),
        },
        health_evidence_text_by_number={1: "指南建议保持食物多样化。"},
    )
    assert validated.recommendations == []


def test_internal_execution_language_is_removed():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "结论由固定规则生成，AI只进行文字归纳。",
            "healthProfile": "系统执行后判断为一般。",
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary is None
    assert validated.health_profile is None


def test_health_profile_removes_enterotype_and_report_purpose_language():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "healthProfile": (
                "本次肠型为肠型II。"
                "本报告用于形成当前健康管理关注顺序。"
                "现有检查提示糖脂代谢和心血管健康需要关注。"
            )
        }
    )

    validated = validate_draft(draft, microbes=microbes, diseases=diseases)

    assert validated.health_profile == (
        "现有检查提示糖脂代谢和心血管健康需要关注。"
    )


def test_model_extras_and_long_basis_do_not_discard_whole_draft():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "unexpectedField": "ignored",
            "healthLevel": {
                "level": "一般",
                "summary": "结合本次结果，整体水平为一般。",
                "basis": [f"依据{i}" for i in range(10)],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.health_level is not None
    assert len(validated.health_level.basis) == 8


def test_unsupported_microbe_and_disease_inferences_are_removed():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "该菌显著偏高，但远低于致病阈值，无临床意义。",
                }
            ],
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": "该分值提示便秘风险显著偏高。",
                }
            ],
            "recommendations": [
                {"category": "general", "content": "定期监测血脂并复查肠镜。"},
                {"category": "bowel_habits", "content": "规律排便可减少毒素滞留。"},
            ],
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.microbe_narratives == []
    assert validated.disease_narratives == []
    assert validated.recommendations == []


def test_unbound_disease_and_patient_speculation_are_removed_from_microbe_row():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "clinicalRole": "该菌可导致胃肠炎和感染。",
                    "conclusion": (
                        "本次样本检出肠沙门菌相关序列。该结果提示胃肠炎风险。"
                    ),
                    "patientInterpretation": (
                        "当前未发现感染证据，可能来自污染或偶然携带。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    item = validated.microbe_narratives[0]
    assert item.conclusion == "本次样本检出肠沙门菌相关序列。"
    assert item.clinical_role is None
    assert item.patient_interpretation is None


def test_summary_cannot_turn_detected_signals_into_abnormal_marks():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": (
                "多种菌均为检出信号及异常标记。"
                "Bacteroides fragilis占比较高，但检测方未标记为异常。"
            ),
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": "检测方模型结果55，未提供等级判定。",
                }
            ],
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.abnormal_microbes_summary is None
    assert validated.disease_narratives == []


def test_missing_data_and_invented_model_level_do_not_reach_report():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": "检测方模型结果55，未提供等级，不判断风险。",
                }
            ],
            "healthProfile": "存在较高的便秘55模型分值，但未提供等级。",
            "healthLevel": {
                "level": "需关注",
                "summary": "疾病模型分值高，需综合评估。",
                "basis": ["疾病模型分值高"],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.disease_narratives == []
    assert validated.health_profile is None
    assert validated.health_level is None


def test_clause_removal_cannot_leave_dangling_sentence_fragments():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": (
                "潜在致病真菌被检测方标记超标。条件致病菌亦被检出，但同样。"
            ),
            "healthLevel": {
                "level": "需关注",
                "summary": "潜在致病真菌超标，且多种结合血脂异常，提示需关注。",
                "basis": [],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.abnormal_microbes_summary == "潜在致病真菌被检测方标记超标。"
    assert validated.health_level is None


def test_generic_clinical_boundaries_and_unmarked_levels_are_hidden():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": (
                "多种潜在致病菌均为低丰度检出，未提示异常。"
                "便秘模型结果为55，但无检测方等级判定。"
            ),
            "healthProfile": ("疾病模型提示部分代谢相关风险，需结合临床综合评估。"),
            "healthLevel": {
                "level": "需关注",
                "summary": "多种需结合临床关注。",
                "basis": [],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "便秘模型结果为55。"
    assert validated.health_profile is None
    assert validated.health_level is None


def test_grade_clause_removal_preserves_grammar_and_rejects_new_euphemisms():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": (
                "便秘模型结果为55，但均无检测方等级判定，"
                "且血脂偏高。"
                "多种潜在致病菌被检出，但均未明确为感染。"
            ),
            "abnormalMicrobesSummary": ("真菌指标超标，但具体菌种未明确异常。"),
            "healthProfile": "菌群多样性3.48，菌群多样性一般。",
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "便秘模型结果为55，且血脂偏高。"
    assert validated.abnormal_microbes_summary is None
    assert validated.health_profile is None


def test_missing_narratives_are_split_into_bounded_retry_batches():
    batches = _narrative_retry_batches(
        {
            "fields": ["healthProfile"],
            "microbeIds": [f"microbe-{index}" for index in range(12)],
            "diseaseIds": [f"disease-{index}" for index in range(9)],
        },
        batch_size=10,
    )
    assert [len(item["microbeIds"]) + len(item["diseaseIds"]) for item in batches] == [
        10,
        10,
        1,
    ]
    assert batches[0]["fields"] == ["healthProfile"]
    assert batches[1]["fields"] == []


def test_unpunctuated_transition_and_inferred_abundance_summary_are_rejected():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "便秘、肠炎等但结合肠镜结果大致正常。",
            "abnormalMicrobesSummary": "检出多种真菌，但多为痕量或低丰度。",
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary is None
    assert validated.abnormal_microbes_summary is None


def test_generic_clinical_phrase_is_removed_and_plural_abundance_is_rejected():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "便秘模型分值55，均需结合临床。",
            "abnormalMicrobesSummary": "检出多种低丰度病原菌。",
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "便秘模型分值55。"
    assert validated.abnormal_microbes_summary is None


def test_large_microbe_section_requires_representative_named_summary():
    microbes, diseases = sections()
    template = microbes.items[0]
    microbes.items = [
        template.model_copy(
            update={
                "microbe_id": f"taxon-{index}",
                "name": f"菌种{index}",
                "scientific_name": f"Species {index}",
            }
        )
        for index in range(12)
    ]
    too_short = NarrativeDraft.model_validate(
        {"abnormalMicrobesSummary": "本次潜在致病真菌指标被检测方标记超标。"}
    )
    assert (
        validate_draft(
            too_short, microbes=microbes, diseases=diseases
        ).abnormal_microbes_summary
        is None
    )
    detailed = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": (
                "本次检出的代表菌种包括菌种0、菌种1和菌种2，"
                "同时包含条件致病菌与潜在致病真菌信号。"
                "其中真菌组分指标由检测方标记为超标，"
                "其余菌种按原报告的检出或痕量状态呈现。"
            )
        }
    )
    validated = validate_draft(detailed, microbes=microbes, diseases=diseases)
    assert validated.abnormal_microbes_summary is not None


def test_missing_grade_variants_and_internal_ids_are_removed():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": (
                "便秘模型分值55，未提供风险等级。沙门氏菌被检出，但无异常标记。"
            ),
            "healthProfile": "沙门氏菌被检出，但未提供异常或高低判断。",
            "healthLevel": {
                "level": "一般",
                "summary": "本次包含可用的个体事实。",
                "basis": ["disease-internal: 55", "血脂偏高"],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "便秘模型分值55。沙门氏菌被检出。"
    assert validated.health_profile == "沙门氏菌被检出。"
    assert validated.health_level is not None
    assert validated.health_level.basis == ["血脂偏高"]


def test_microbe_summary_cannot_name_taxon_outside_section_whitelist():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": (
                "本次表格中检出Salmonella enterica信号，"
                "同时又提到未纳入该表的Faecalibacterium prausnitzii。"
            )
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.abnormal_microbes_summary is None


def test_dangling_connectors_missing_taxon_note_and_inferred_quantity_are_removed():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "检出多种条件致病菌，但均。",
            "healthProfile": (
                "真菌指标被标记超标，但未具体说明菌种。"
                "疾病模型提示便秘的但肠镜大致正常。"
            ),
            "healthLevel": {
                "level": "需关注",
                "summary": "真菌超标，且多个包括便秘和肠炎。",
                "basis": [],
            },
            "abnormalMicrobesSummary": "本次还检出少量致病菌信号。",
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary is None
    assert validated.health_profile == "真菌指标被标记超标。"
    assert validated.health_level is None
    assert validated.abnormal_microbes_summary is None


def test_comma_after_dangling_uniform_claim_is_removed():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": (
                "本次检出沙门氏菌等病原相关菌，但均，仅提示检出。便秘模型分值为55。"
            )
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "便秘模型分值为55。"


def test_ungrounded_number_is_removed_from_public_prose():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {"executiveSummary": ("菌群多样性指数为3.69。便秘模型分值为55。")}
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        grounded_numbers={3.4818, 55.0},
    )
    assert validated.executive_summary == "便秘模型分值为55。"


def test_quantified_or_cross_topic_health_advice_requires_matching_evidence():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "recommendations": [
                {
                    "category": "exercise",
                    "content": "每周运动150分钟。",
                    "evidenceRefs": [2],
                },
                {
                    "category": "general",
                    "content": "保持均衡饮食、规律运动和充足睡眠。",
                    "evidenceRefs": [1, 2],
                },
                {
                    "category": "diet",
                    "content": "增加食物种类，保持均衡膳食。",
                    "evidenceRefs": [1],
                },
                {
                    "category": "diet",
                    "content": "增加蔬菜摄入以维持肠道菌群稳定。",
                    "evidenceRefs": [1],
                },
            ]
        }
    )
    validated = validate_draft(
        draft,
        microbes=microbes,
        diseases=diseases,
        allowed_health_evidence_refs={
            "diet": {1},
            "exercise": {2},
            "sleep": set(),
            "bowel_habits": set(),
            "general": {1, 2},
        },
        health_evidence_text_by_number={
            1: "指南建议保持均衡饮食。",
            2: "指南建议进行规律运动。",
        },
    )
    assert validated.recommendations == []


def test_microbe_summary_drops_missing_data_explanation_but_keeps_findings():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "abnormalMicrobesSummary": (
                "本次病原相关菌检出沙门氏菌。"
                "检测方未提供各菌种异常判定，以上均为检出状态描述。"
            )
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.abnormal_microbes_summary == "本次病原相关菌检出沙门氏菌。"


def test_no_abnormality_euphemism_and_generic_individual_boundary_are_hidden():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "沙门氏菌被检出，未显示异常。",
            "abnormalMicrobesSummary": ("沙门氏菌被检出。所有检出均需结合个体情况。"),
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary == "沙门氏菌被检出。"
    assert validated.abnormal_microbes_summary == "沙门氏菌被检出。"


def test_cross_clause_model_risk_inference_ratio_level_and_infection_advice_are_rejected():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": (
                "疾病模型结果包括便秘55、肠炎46，均提示相关疾病风险可能较高。"
            ),
            "healthProfile": "条件致病菌中拟杆菌属比例较高。",
            "healthLevel": {
                "level": "需关注",
                "summary": "疾病模型评分包括便秘55，提示需要关注。",
                "basis": ["potential致病真菌超标", "血脂偏高"],
            },
            "recommendations": [
                {
                    "category": "general",
                    "content": "注意饮食卫生，减少潜在致病菌感染风险。",
                }
            ],
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.executive_summary is None
    assert validated.health_profile is None
    assert validated.health_level is None
    assert validated.recommendations == []


def test_mixed_health_sentence_keeps_supported_clauses():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "healthLevel": {
                "level": "需关注",
                "summary": (
                    "虽然肠道菌群多样性尚可，但潜在致病真菌被标记超标，"
                    "且多项疾病风险分值较高（便秘55），同时存在血脂偏高，"
                    "整体健康状态需关注。"
                ),
                "basis": [],
            }
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.health_level is not None
    assert "尚可" not in validated.health_level.summary
    assert "分值较高" not in validated.health_level.summary
    assert "潜在致病真菌被标记超标" in validated.health_level.summary


def test_missing_session_context_cannot_be_described_as_no_symptoms():
    microbes, diseases = sections()
    draft = NarrativeDraft.model_validate(
        {
            "executiveSummary": "目前无明显症状，因此不支持感染。",
            "healthProfile": "未见症状，整体状态良好。",
        }
    )
    validated = validate_draft(
        draft, microbes=microbes, diseases=diseases, session_context={}
    )
    assert validated.executive_summary is None
    assert validated.health_profile is None


def test_microbe_without_evidence_cannot_receive_model_conclusion():
    microbes, diseases = sections()
    microbes.items[0].evidence_refs = []
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {"microbeId": "allowed-taxon", "conclusion": "文献提示该菌与肠炎有关。"}
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.microbe_narratives == []


def test_source_only_microbe_narrative_and_health_level_alias_are_kept():
    microbes, diseases = sections()
    microbes.items[0].evidence_refs = []
    draft = NarrativeDraft.model_validate(
        {
            "microbeNarratives": [
                {
                    "microbeId": "allowed-taxon",
                    "conclusion": "原报告检出Salmonella enterica，比例为0.1%。",
                }
            ],
            "healthLevel": {
                "level": "需要关注",
                "summary": "检测方明确标记了一项需关注结果。",
                "basis": [
                    "panel_fungi_pathogenic: 超标",
                    "gmwi2",
                    "gmhi: -0.02",
                    "潜在致病真菌超标",
                ],
            },
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.microbe_narratives[0].conclusion.startswith("原报告检出")
    assert validated.health_level.level == "重点关注"
    assert validated.health_level.basis == ["潜在致病真菌超标"]


def test_evidence_disease_removes_negative_risk_boilerplate():
    microbes, diseases = sections()
    diseases.items[0].source_type = "evimed_association"
    diseases.items[0].source_result = None
    diseases.items[0].evidence_refs = [1]
    diseases.items[0].association_evidence_refs = [1]
    diseases.items[0].related_microbes = ["Salmonella enterica"]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": (
                        "本次检测呈现与便秘相关的菌群变化，"
                        "人体研究观察到Salmonella enterica与便秘存在关联，"
                        "因此可适当关注排便症状和胃肠功能，"
                        "但本次检测未直接提示便秘风险。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert "本次检测呈现与便秘相关的菌群变化" in (
        validated.disease_narratives[0].related_content
    )
    assert "未直接提示" not in validated.disease_narratives[0].related_content


def test_disease_narrative_keeps_real_result_and_drops_model_boilerplate():
    microbes, diseases = sections()
    diseases.items[0].existing_results = [
        ClinicalResult(
            itemName="（附加）无痛肠镜检查(肠)",
            result="所见结直肠黏膜大致正常",
        )
    ]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": (
                        "原报告关联模型结果55，为便秘相关模型信号。"
                        "已有的（附加）无痛肠镜检查(肠)显示结直肠黏膜大致正常，"
                        "因此该分值不构成临床诊断，可能与菌群构成有关。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    # 正常肠镜结果保留在 existingResults 供用户对照，不得在
    # 疾病关联解读中改写成排除性结论。
    assert validated.disease_narratives == []


def test_disease_narrative_rejects_unbound_extra_microbe_names():
    microbes, diseases = sections()
    diseases.items[0].source_type = "evimed_association"
    diseases.items[0].association_evidence_refs = [1]
    diseases.items[0].related_microbes = ["Fusobacterium nucleatum"]
    draft = NarrativeDraft.model_validate(
        {
            "diseaseNarratives": [
                {
                    "diseaseId": "allowed-disease",
                    "relatedContent": (
                        "相关菌种包括Bacteroides fragilis和Fusobacterium nucleatum。"
                    ),
                }
            ]
        }
    )
    validated = validate_draft(draft, microbes=microbes, diseases=diseases)
    assert validated.disease_narratives == []


@pytest.mark.asyncio
async def test_no_citation_forces_microbe_narrative_back_to_source_facts():
    microbes, _diseases = sections()
    microbes.items[0].evidence_refs = []
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                value="0.1",
                detection_status="detected",
            )
        ],
    )

    class Client:
        async def generate(self, payload):
            return {
                "executiveSummary": "本次样本记录一项菌种检出信号。",
                "abnormalMicrobesSummary": "本次检出Salmonella enterica。",
                "microbeNarratives": [
                    {
                        "microbeId": "allowed-taxon",
                        "clinicalRole": "该菌是常见肠道共生菌。",
                        "conclusion": "本次检出值为0.1。",
                        "patientInterpretation": "可能来自食物或环境。",
                    }
                ],
                "healthLevel": {
                    "level": "一般",
                    "summary": "本次报告记录一项菌种检出结果。",
                    "basis": [],
                },
                "healthProfile": "本次报告记录一项菌种检出结果。",
            }

    draft = await generate_narrative(
        Client(),
        parsed=parsed,
        microbes=microbes,
        diseases=build_source_diseases(parsed, []),
        hospital_data=[],
        session_context={},
        evidence_summaries=[],
    )
    item = draft.microbe_narratives[0]
    assert item.clinical_role is None
    assert item.conclusion == "检测结果显示，肠沙门菌为检出信号，检测值为0.1%。"
    assert item.patient_interpretation == (
        item.conclusion + "目前仅可确认该项检测变化，尚无足够的人体医学依据"
        "解释其与具体疾病或不良影响的关系。"
    )


@pytest.mark.asyncio
async def test_final_narrative_hides_withheld_disease_and_rebuilds_disease_context():
    microbes, diseases = sections()
    diseases.items[0].related_microbes = ["Salmonella enterica"]
    diseases.items[0].existing_results = [
        ClinicalResult(itemName="无痛肠镜检查(肠)", result="结直肠黏膜大致正常")
    ]
    diseases.items[0].suggested_evaluations = [
        SuggestedEvaluation(
            name="便秘症状评估",
            rationale="指南直接建议",
            evidenceRefs=[3],
        )
    ]
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                chinese_name="肠沙门菌",
                value="0.1",
                detection_status="detected",
            )
        ],
        disease_results=[
            ParsedDisease(disease_id="allowed-disease", name="便秘", source_score="55"),
            ParsedDisease(disease_id="withheld-asd", name="ASD", source_score="12"),
        ],
        enterotype="肠型I",
    )

    class Client:
        def __init__(self):
            self.payloads = []

        async def generate(self, payload):
            self.payloads.append(payload)
            return {
                "executiveSummary": "ASD分值不构成临床诊断。",
                "abnormalMicrobesSummary": "本次病原相关菌检出肠沙门菌。",
                "microbeNarratives": [
                    {
                        "microbeId": "allowed-taxon",
                        "clinicalRole": "检测方病原相关菌面板检出信号。",
                        "conclusion": "本次样本检出肠沙门菌。",
                        "patientInterpretation": "肠沙门菌在本次粪便样本中检出。",
                    }
                ],
                "diseaseNarratives": [
                    {
                        "diseaseId": "allowed-disease",
                        "relatedContent": "正常肠镜已经排除便秘相关疾病。",
                    }
                ],
                "healthLevel": {
                    "level": "一般",
                    "summary": "肠型I及本次检出结果构成当前健康画像。",
                    "basis": [],
                },
                "healthProfile": "肠型I，主要优势菌为肠沙门菌。",
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白(LDL-c)",
                testResultValue="3.70",
                resultStatusName="偏高",
            )
        ],
        session_context={},
        evidence_summaries=[],
    )
    assert "diseaseResults" not in client.payloads[0]["sourceFacts"]
    # The invalid ASD draft is discarded; the bounded extractive fallback uses
    # only validated report sections and must not repeat raw indicator values.
    assert draft.executive_summary is not None
    assert "ASD" not in draft.executive_summary
    assert "3.70" not in draft.executive_summary
    assert "便秘" not in (draft.health_profile or "")
    assert "低密度脂蛋白" not in (draft.health_profile or "")
    assert "代谢与心血管健康" in (draft.health_profile or "")
    assert "优势菌" not in (draft.health_profile or "")
    assert draft.disease_narratives == []
    assert draft.health_level.level == "中度关注"


@pytest.mark.asyncio
async def test_truncated_initial_model_response_recovers_with_bounded_global_retry():
    microbes, _ = sections()
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                chinese_name="肠沙门菌",
                detection_status="detected",
            )
        ],
    )
    diseases = build_source_diseases(parsed, [])

    class Client:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise NarrativeError("truncated JSON")
            return {
                "executiveSummary": _valid_executive_summary(),
                "abnormalMicrobesSummary": _valid_microbe_summary(),
                "healthLevel": {
                    "level": "一般",
                    "summary": "本次样本记录肠沙门菌检出。",
                    "basis": [],
                },
                "healthProfile": "本次样本记录肠沙门菌检出。",
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        session_context={},
        evidence_summaries=[],
    )
    # Three bounded global retries recover from the malformed first response;
    # an extractive source-fact fallback then completes the section without a
    # second serial model loop.
    assert client.calls == 4
    assert draft.executive_summary == _valid_executive_summary()
    assert draft.microbe_narratives[0].conclusion


@pytest.mark.asyncio
async def test_missing_narrative_fields_are_retried_without_fixed_fallback():
    microbes, diseases = sections()
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="allowed-taxon",
                category="pathogenic",
                latin_name="Salmonella enterica",
                value="0.1",
                detection_status="detected",
            )
        ],
        disease_results=[
            ParsedDisease(
                disease_id="allowed-disease",
                name="便秘",
                source_score="55",
            )
        ],
    )

    class Client:
        def __init__(self):
            self.calls = 0
            self.payloads = []

        async def generate(self, payload):
            self.calls += 1
            self.payloads.append(payload)
            if self.calls == 1:
                return {
                    "executiveSummary": _valid_executive_summary(disease="便秘"),
                    "abnormalMicrobesSummary": _valid_microbe_summary(),
                    "microbeNarratives": [
                        {
                            "microbeId": "allowed-taxon",
                            "clinicalRole": "检测方病原相关菌面板检出信号。",
                            "conclusion": "原报告检出Salmonella enterica。",
                            "patientInterpretation": "本次粪便样本记录了该菌相关序列。",
                        }
                    ],
                    "healthProfile": "本次报告包含一项菌种检出结果。",
                    "recommendations": [
                        {"category": "diet", "content": "保持饮食多样化。"}
                    ],
                }
            return {
                "executiveSummary": _valid_executive_summary(disease="便秘"),
                "diseaseNarratives": [
                    {
                        "diseaseId": "allowed-disease",
                        "relatedContent": "检测方模型分值为55。",
                    }
                ],
                "healthLevel": {
                    "level": "一般",
                    "summary": "结合本次来源结果，整体健康水平为一般。",
                    "basis": [],
                },
            }

    client = Client()
    draft = await generate_narrative(
        client,
        parsed=parsed,
        microbes=microbes,
        diseases=diseases,
        hospital_data=[],
        session_context={},
        evidence_summaries=[],
    )
    # The redesigned pipeline rejects implementation-oriented helper text and
    # bounds the model work to one initial call plus three focused retries. It
    # still never inserts an unsupported medical conclusion.
    assert client.calls == 4
    assert client.payloads[1]["microbeWhitelist"][0]["microbeId"] == "allowed-taxon"
    assert client.payloads[1]["diseaseWhitelist"][0]["diseaseId"] == "allowed-disease"
    assert draft.health_level.level == "中度关注"
    assert draft.disease_narratives == []
