from types import SimpleNamespace
from itertools import permutations

import pytest
from pydantic import ValidationError

from modules.report.callback import _fixed_callback_body
from modules.report.clinical import (
    attach_clinical_followup_context,
    disease_has_patient_microbiome_signal,
)
from modules.report.health_assessment.engine import _level, build_health_assessment
from modules.report.health_assessment.extraction import extract_assessment_facts
from modules.report.health_assessment.narrative import (
    apply_abnormal_indicator_summaries,
    apply_assessment_explanations,
)
from modules.report.health_assessment.normalization import numeric_value
from modules.report.parsing import (
    parse_canonical_data,
    parse_extracted_text,
    parse_provider_data,
)
from modules.report.ports import DownloadedTestData
from modules.report.schemas import (
    DiseaseRisksSection,
    FailureCallback,
    HealthAssessment,
    ParsedDisease,
    ParsedMicrobiomeReport,
    ReportRequest,
    SubjectInfo,
)


def test_clinical_assessment_condition_adds_internal_followup_context():
    request = ReportRequest.model_validate(
        {
            "userId": "u-clinical-followup",
            "sessionId": "s-clinical-followup",
            "hospitalData": [
                {
                    "dataType": "INSPECTION",
                    "itemName": "低密度脂蛋白(LDL-C)",
                    "testResultValue": "3.70",
                    "referenceRange": "0.00-3.37",
                    "resultStatusName": "偏高",
                }
            ],
        }
    )
    assessment = SimpleNamespace(
        dimensions=[
            SimpleNamespace(
                related_diseases=[
                    SimpleNamespace(
                        name="高脂血症",
                        risk_level="medium",
                        source="临床资料",
                        primary=True,
                    )
                ]
            )
        ]
    )
    diseases = DiseaseRisksSection()

    attach_clinical_followup_context(
        diseases,
        assessment,
        request.hospital_data,
    )

    assert len(diseases.items) == 1
    assert diseases.items[0].source_type == "clinical_context"
    assert diseases.items[0].existing_results[0].item_name == "低密度脂蛋白(LDL-C)"
    assert not disease_has_patient_microbiome_signal(diseases.items[0])


def test_clinical_followup_context_keeps_explicit_fatty_liver_diagnosis():
    request = ReportRequest.model_validate(
        {
            "userId": "u-clinical-fatty-liver",
            "sessionId": "s-clinical-fatty-liver",
            "hospitalData": [
                {
                    "dataType": "EXAM",
                    "itemName": "脂肪肝",
                    "examResult": "体检结论：脂肪肝",
                }
            ],
        }
    )
    assessment = SimpleNamespace(
        dimensions=[
            SimpleNamespace(
                related_diseases=[
                    SimpleNamespace(
                        name="非酒精性脂肪肝",
                        risk_level="medium",
                        source="临床资料",
                        primary=True,
                    )
                ]
            )
        ]
    )
    diseases = DiseaseRisksSection()

    attach_clinical_followup_context(
        diseases,
        assessment,
        request.hospital_data,
    )

    assert [item.item_name for item in diseases.items[0].existing_results] == [
        "脂肪肝"
    ]


def _request(**changes):
    payload = {
        "userId": "u-score",
        "sessionId": "s-score",
        "name": "受检者",
        "age": 38,
        "sex": "男",
        "microbiomeData": {
            "sample": {"sampleId": "S-1", "reportDate": "2026-08-10"},
            "overallMetrics": [
                {
                    "code": "gmhi",
                    "name": "肠道微生物健康指数",
                    "value": "1.2",
                    "status": "正常",
                },
                {
                    "code": "panel_beneficial",
                    "name": "有益菌总检出丰度",
                    "value": "12",
                    "unit": "%",
                    "status": "轻度缺乏",
                },
                {
                    "code": "gut_barrier",
                    "name": "肠道屏障功能",
                    "value": "正常",
                    "status": "正常",
                },
            ],
            "shortChainFattyAcids": [
                {
                    "code": "butyrate",
                    "name": "丁酸合成能力",
                    "value": "轻度异常",
                    "status": "轻度异常",
                }
            ],
            "taxa": [],
        },
        "hospitalData": [
            _lab("空腹血糖", "5.4", "mmol/L", "3.9-6.1", "2026-08-10"),
            _lab("LDL-C", "3.7", "mmol/L", "0-3.37", "2026-08-10", "偏高"),
            _lab("收缩压", "126", "mmHg", "90-139", "2026-08-10"),
            _lab("舒张压", "82", "mmHg", "60-89", "2026-08-10"),
            _lab("血肌酐", "88", "μmol/L", "57-111", "2026-08-10"),
            _lab("血尿酸", "410", "μmol/L", "208-428", "2026-08-10"),
        ],
    }
    payload.update(changes)
    return ReportRequest.model_validate(payload)


def _lab(name, value, unit, reference, when, status=None):
    return {
        "dataType": "INSPECTION",
        "itemName": f"{name}({unit})",
        "testResultValue": value,
        "referenceRange": reference,
        "resultStatusName": status,
        "reportTime": when,
    }


def test_fixed_callback_always_has_six_ordered_dimensions_on_failure():
    body = _fixed_callback_body(
        FailureCallback(
            user_id="u",
            session_id="s",
            error_code="SOURCE_PARSE_FAILED",
            message="failed",
        )
    )

    assessment = body["healthAssessment"]
    assert assessment["overallScore"] == 0
    assert assessment["overallScoreAvailable"] is False
    assert [item["code"] for item in assessment["dimensions"]] == [
        "cardiovascular_system",
        "metabolic_system",
        "digestive_system",
        "immune_system",
        "urinary_system",
        "nervous_system",
    ]
    assert all(item["missingEvidence"] for item in assessment["dimensions"])
    assert assessment["abnormalIndicators"] == []


def test_patient_copy_changes_preserve_fixed_callback_field_names():
    body = _fixed_callback_body(
        FailureCallback(
            user_id="u",
            session_id="s",
            error_code="SOURCE_PARSE_FAILED",
            message="failed",
        )
    )

    assert list(body) == [
        "userId",
        "sessionId",
        "finalReportId",
        "type",
        "status",
        "generated_at",
        "subject",
        "sample",
        "healthAssessment",
        "diagnosis_summary",
        "diagnostic_basis",
        "diagnostic_advice",
        "evidence",
        "partial_reasons",
        "error",
    ]
    assert list(body["healthAssessment"]) == [
        "name",
        "ruleVersion",
        "overallScore",
        "overallScoreAvailable",
        "displayOverallScore",
        "level",
        "levelLabel",
        "summary",
        "dimensions",
        "abnormalIndicators",
    ]
    assert list(body["healthAssessment"]["dimensions"][0]) == [
        "code",
        "name",
        "weight",
        "score",
        "scoreAvailable",
        "displayScore",
        "level",
        "levelLabel",
        "confidence",
        "evidenceUsed",
        "ruleBasis",
        "missingEvidence",
        "conflicts",
        "trendAdjustment",
        "scoreCap",
        "relatedDiseases",
        "explanation",
    ]


def test_health_assessment_allows_dynamic_order_but_rejects_duplicate_dimensions():
    payload = HealthAssessment().model_dump(mode="json")
    payload["dimensions"][0], payload["dimensions"][1] = (
        payload["dimensions"][1],
        payload["dimensions"][0],
    )
    reordered = HealthAssessment.model_validate(payload)
    assert reordered.dimensions[0].code == "metabolic_system"

    payload["dimensions"][1] = payload["dimensions"][0]
    with pytest.raises(ValidationError, match="unique"):
        HealthAssessment.model_validate(payload)


def test_health_assessment_rejects_zero_as_a_real_available_score():
    payload = HealthAssessment().model_dump(mode="json")
    payload["dimensions"][0]["score_available"] = True
    payload["dimensions"][0]["display_score"] = "0"
    payload["dimensions"][0]["level"] = "priority_attention"
    payload["dimensions"][0]["missing_evidence"] = []

    with pytest.raises(
        ValidationError, match="available dimension score must be positive"
    ):
        HealthAssessment.model_validate(payload)


def test_partial_data_uses_zero_only_as_render_value_and_hides_overall_score():
    request = _request(hospitalData=[])
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})

    assert assessment.overall_score == 0
    assert assessment.overall_score_available is False
    assert assessment.display_overall_score == "--"
    assert "阶段性评估" in assessment.summary
    assert "已具备评分条件的维度" in assessment.summary
    gut = assessment.dimensions[0]
    assert gut.score_available is True
    metabolic = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )
    assert metabolic.score == 0
    assert metabolic.score_available is False
    assert metabolic.display_score == "--"
    assert metabolic.missing_evidence


def test_source_gut_total_score_resolves_microecology_and_lists_abnormal_inputs():
    request = ReportRequest.model_validate(
        {
            "userId": "u-source-score",
            "sessionId": "s-source-score",
            "age": 57,
            "sex": "女",
        }
    )
    parsed = parse_extracted_text(
        request,
        filename="microbiome.pdf",
        source_type="pdf",
        text=(
            "您的肠道总评分为：52\n中度失衡\n"
            "|肠道微生物健康指数|-0.02|亚健康|\n"
            "|肠道菌群多样性评估|7.36|\u6b63常|3-10|\n"
            "|肠道菌群种类评估|1372|\u6b63常|1200-1500|\n"
            "|B/E比值|0.0786|\u504f低|>1|\n"
            "|肠道菌群致病潜力评估|1.8901|\u5931衡|2.5-11.662|\n"
            "|肠道膳食纤维需求|44|\u9700求高|>80|"
        ),
    )

    assessment = build_health_assessment(request, parsed, {})
    gut = assessment.dimensions[0]

    assert gut.score_available is True
    assert gut.score == 52
    assert gut.level == "priority_attention"
    assert gut.missing_evidence == []
    assert "消化道、肝胆及肠道微生态相关健康风险" in gut.explanation
    assert "52分" not in gut.explanation
    assert "本维度" not in gut.explanation
    assert gut.level_label not in gut.explanation
    assert "B/E比值" not in gut.explanation
    abnormal = {item.indicator_code: item for item in assessment.abnormal_indicators}
    assert set(abnormal) >= {
        "microbiome:gmhi",
        "microbiome:be_ratio",
        "microbiome:pathogenic_potential",
        "microbiome:dietary_fiber_need",
    }
    assert "microbiome:diversity" not in abnormal
    assert "microbiome:species_count" not in abnormal
    assert abnormal["microbiome:be_ratio"].reference_range == ">1"
    assert abnormal["microbiome:gmhi"].reference_range == (
        "原报告未提供数值参考范围"
    )


def test_digestive_reference_indicators_hide_zero_percent_microbiome_values():
    baseline_request = _request()
    baseline = build_health_assessment(
        baseline_request,
        parse_canonical_data(baseline_request, baseline_request.microbiome_data),
        {},
    )
    baseline_digestive = next(
        item for item in baseline.dimensions if item.code == "digestive_system"
    )
    assert any(
        evidence.indicator_code == "microbe_composition:panel_beneficial"
        for evidence in baseline_digestive.evidence_used
    )

    payload = baseline_request.model_dump(mode="json", by_alias=True)
    panel = next(
        item
        for item in payload["microbiomeData"]["overallMetrics"]
        if item["code"] == "panel_beneficial"
    )
    panel["value"] = "0"
    panel["unit"] = "%"
    request = ReportRequest.model_validate(payload)
    parsed = parse_canonical_data(request, request.microbiome_data)

    assessment = build_health_assessment(request, parsed, {})
    digestive = next(
        item for item in assessment.dimensions if item.code == "digestive_system"
    )

    assert digestive.score_available is True
    assert digestive.score == baseline_digestive.score
    assert not any(
        evidence.indicator_code == "microbe_composition:panel_beneficial"
        for evidence in digestive.evidence_used
    )
    assert not any(
        numeric_value(evidence.value) == 0
        and "%" in f"{evidence.value or ''}{evidence.unit or ''}"
        for evidence in digestive.evidence_used
    )


def test_abnormal_module_hides_management_only_deviation_when_source_marks_normal():
    request = _request(
        sex="女",
        hospitalData=[
            _lab("空腹血糖", "5.4", "mmol/L", "3.9-6.1", "2026-08-10"),
            _lab("LDL-C", "3.2", "mmol/L", "0-3.37", "2026-08-10", "正常"),
            _lab("HDL-C", "1.18", "mmol/L", "1.04-1.55", "2026-08-10", "正常"),
            _lab("血肌酐", "88", "μmol/L", "57-111", "2026-08-10", "正常"),
            _lab("血尿酸", "410", "μmol/L", "208-428", "2026-08-10", "正常"),
        ],
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    abnormal = {item.indicator_code: item for item in assessment.abnormal_indicators}

    assert "hdl_c" not in abnormal
    assert "uric_acid" not in abnormal
    assert all(
        item.status != "未达到健康管理评估标准"
        for item in assessment.abnormal_indicators
    )
    metabolism = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )
    kidney = next(
        item for item in assessment.dimensions if item.code == "urinary_system"
    )
    assert "HDL-C1.18mmol/L" not in metabolism.explanation
    assert "血尿酸410μmol/L" not in kidney.explanation
    assert any(item.item == "HDL-C" for item in metabolism.evidence_used)
    assert any(item.item == "血尿酸" for item in kidney.evidence_used)


def test_fully_missing_source_keeps_all_six_dimensions_unscored():
    request = ReportRequest.model_validate(
        {"userId": "u-empty", "sessionId": "s-empty", "age": 38, "sex": "男"}
    )
    parsed = ParsedMicrobiomeReport(
        source_type="pending",
        source_name="菌群检测结果尚在生成",
        subject=SubjectInfo(age=38, sex="男"),
    )

    assessment = build_health_assessment(request, parsed, {})

    assert assessment.overall_score_available is False
    assert assessment.display_overall_score == "--"
    assert len(assessment.dimensions) == 6
    assert all(item.score == 0 for item in assessment.dimensions)
    assert all(item.display_score == "--" for item in assessment.dimensions)
    assert all(item.missing_evidence for item in assessment.dimensions)
    assert "阶段性评估" in assessment.summary
    assert "尚不足以判断六个系统的整体健康水平" in assessment.summary


def test_complete_component_data_keeps_unmatched_systems_pending():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    session = {
        "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
        "answers": ["不吸烟"],
    }
    assessment = build_health_assessment(request, parsed, session)

    assert len(assessment.dimensions) == 6
    assert any(item.score_available for item in assessment.dimensions)
    assert any(not item.score_available for item in assessment.dimensions)
    assert assessment.overall_score_available is False
    assert assessment.display_overall_score == "--"
    # 六个维度各占 1/6，二进制浮点求和不精确等于 1.0，按容差比较。
    assert abs(sum(item.weight for item in assessment.dimensions) - 1.0) < 1e-9
    assert all(item.rule_basis for item in assessment.dimensions if item.score_available)


def test_same_day_same_priority_conflict_prevents_indicator_use():
    request = _request(
        hospitalData=[
            _lab("空腹血糖", "5.4", "mmol/L", "3.9-6.1", "2026-08-10"),
            _lab("空腹血糖", "7.2", "mmol/L", "3.9-6.1", "2026-08-10"),
            _lab("LDL-C", "3.7", "mmol/L", "0-3.37", "2026-08-10"),
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    metabolic = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )

    assert metabolic.score_available is False
    assert metabolic.conflicts
    assert any("空腹血糖" in value for value in metabolic.conflicts)


def test_same_value_with_richer_status_is_not_treated_as_conflict():
    request = _request(
        hospitalData=[
            _lab("空腹血糖", "5.4", "mmol/L", "3.9-6.1", "2026-08-10"),
            _lab("LDL-C", "3.40", "mmol/L", "0-3.37", "2026-08-10", "偏高"),
            _lab("LDL-C", "3.40", "mmol/L", "0-3.37", "2026-08-10"),
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    metabolic = next(
        item for item in assessment.dimensions if item.code == "metabolic_system"
    )

    assert not any("ldl_c" in value for value in metabolic.conflicts)
    ldl = next(
        item for item in metabolic.evidence_used if item.indicator_code == "ldl_c"
    )
    assert ldl.status == "偏高"


def test_disease_only_scored_system_exposes_reference_indicator_and_specific_copy():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-parkinson",
            name="帕金森病",
            source_level="中风险",
        ),
    ]

    assessment = build_health_assessment(request, parsed, {})
    mental = next(
        item for item in assessment.dimensions if item.code == "nervous_system"
    )

    assert mental.score_available is True
    assert any(
        item.indicator_code == "disease_risk_summary:nervous_system"
        and item.value == "中风险1项"
        for item in mental.evidence_used
    )
    assert "帕金森病为中风险关注项" in mental.explanation


def test_latest_abnormal_history_applies_bounded_trend_adjustment():
    request = _request(
        hospitalData=[
            _lab("空腹血糖", "5.5", "mmol/L", "3.9-6.1", "2025-08-10"),
            _lab("空腹血糖", "6.5", "mmol/L", "3.9-6.1", "2026-08-10", "偏高"),
            _lab("LDL-C", "3.8", "mmol/L", "0-3.37", "2025-08-10", "偏高"),
            _lab("LDL-C", "4.2", "mmol/L", "0-3.37", "2026-08-10", "偏高"),
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    metabolic = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )

    assert metabolic.trend_adjustment == -10


def test_confirmed_poorly_controlled_disease_caps_score_at_54():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {"answers": ["既往史：已确诊糖尿病，目前控制不佳"]},
    )
    metabolic = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )

    assert metabolic.score_cap.applied is True
    assert metabolic.score_cap.maximum == 54
    assert metabolic.score <= 54


def test_confirmed_well_controlled_disease_caps_score_at_74():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {"answers": ["既往史：已确诊糖尿病，目前控制良好"]},
    )
    metabolic = next(
        item
        for item in assessment.dimensions
        if item.code == "metabolic_system"
    )

    assert metabolic.score_cap.applied is True
    assert metabolic.score_cap.maximum == 74
    assert metabolic.score <= 74


def test_attention_level_boundaries_are_exact():
    assert _level(54) == ("priority_attention", "重点关注")
    assert _level(55) == ("attention", "需要关注")
    assert _level(74) == ("attention", "需要关注")
    assert _level(75) == ("good", "状态良好")


def test_combined_blood_pressure_value_is_split_into_two_scoreable_facts():
    request = _request(
        hospitalData=[
            {
                "dataType": "INSPECTION",
                "itemName": "血压",
                "testResultValue": "126/82 mmHg",
                "reportTime": "2026-08-10",
            }
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    facts, _diagnoses = extract_assessment_facts(request, parsed, {})

    by_code = {item.code: item for item in facts}
    assert by_code["systolic_bp"].numeric_value == 126
    assert by_code["diastolic_bp"].numeric_value == 82


def test_hs_crp_name_is_not_misclassified_as_serum_creatinine():
    request = _request(
        hospitalData=[
            {
                "dataType": "INSPECTION",
                "itemName": "超敏C反应蛋白(hs-CRP)",
                "testResultValue": "2.40 mg/L",
                "referenceRange": "0-3.0",
                "reportTime": "2026-08-10",
            },
            {
                "dataType": "INSPECTION",
                "itemName": "肌酐(Cr)",
                "testResultValue": "54 μmol/L",
                "referenceRange": "57-97",
                "reportTime": "2026-08-10",
            },
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    facts, _diagnoses = extract_assessment_facts(request, parsed, {})

    creatinine = [item for item in facts if item.code == "creatinine"]
    assert len(creatinine) == 1
    assert creatinine[0].source == "肌酐(Cr)"


def test_model_explanation_cannot_drop_missing_items_or_change_score():
    assessment = HealthAssessment()
    before = assessment.model_dump()
    code = assessment.dimensions[0].code
    apply_assessment_explanations(
        assessment,
        [{"code": code, "explanation": "这个维度很好。"}],
    )

    assert assessment.model_dump() == before


def test_model_explanation_cannot_change_available_score_or_level():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {
            "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
            "answers": ["不吸烟"],
        },
    )
    dimension = assessment.dimensions[0]
    before = dimension.explanation

    apply_assessment_explanations(
        assessment,
        [
            {
                "code": dimension.code,
                "explanation": "本维度为99分，当前为“状态良好”。",
            }
        ],
    )

    assert dimension.explanation == before


def test_model_explanation_cannot_expose_upload_or_file_process_terms():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {
            "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
            "answers": ["不吸烟"],
        },
    )
    dimension = assessment.dimensions[0]
    before = dimension.explanation

    apply_assessment_explanations(
        assessment,
        [
            {
                "code": dimension.code,
                "explanation": (
                    f"根据用户上传的PDF文件，本维度为{dimension.score}分，"
                    f"当前为“{dimension.level_label}”。"
                ),
            }
        ],
    )

    assert dimension.explanation == before


def test_model_explanation_may_only_polish_dimension_health_meaning():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {
            "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
            "answers": ["不吸烟"],
        },
    )
    dimension = assessment.dimensions[0]
    grounded = "肠道菌群结构和代谢功能总体较稳定。"

    apply_assessment_explanations(
        assessment,
        [{"code": dimension.code, "explanation": grounded}],
    )

    assert dimension.explanation == grounded


def test_model_summary_adds_clinical_significance_without_changing_indicator():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    indicator = next(
        item for item in assessment.abnormal_indicators if item.indicator_code == "ldl_c"
    )
    before = indicator.model_dump()
    summary = (
        "LDL-C偏高反映低密度脂蛋白胆固醇负荷增加，"
        "是本次糖脂代谢和心血管健康评估中需要关注的异常指标。"
    )

    apply_abnormal_indicator_summaries(
        assessment,
        [{"indicatorCode": "ldl_c", "clinicalSignificance": summary}],
    )

    assert indicator.clinical_significance == summary
    assert {
        key: value
        for key, value in indicator.model_dump().items()
        if key != "clinical_significance"
    } == {key: value for key, value in before.items() if key != "clinical_significance"}


def test_model_summary_rejects_diagnosis_and_ungrounded_numbers():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    indicator = next(
        item for item in assessment.abnormal_indicators if item.indicator_code == "ldl_c"
    )

    apply_abnormal_indicator_summaries(
        assessment,
        [
            {
                "indicatorCode": "ldl_c",
                "clinicalSignificance": "LDL-C为9.9，说明已确诊心血管疾病。",
            }
        ],
    )

    assert indicator.clinical_significance is None


def test_common_si_unit_is_inferred_only_from_compatible_reference_range():
    request = _request(
        hospitalData=[
            {
                "dataType": "INSPECTION",
                "itemName": "低密度脂蛋白(LDL-c)",
                "testResultValue": "3.70",
                "referenceRange": "0.00-3.37",
                "reportTime": "2026-08-10",
            },
            {
                "dataType": "INSPECTION",
                "itemName": "血肌酐",
                "testResultValue": "88",
                "referenceRange": "57-111",
                "reportTime": "2026-08-10",
            },
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    facts, _diagnoses = extract_assessment_facts(request, parsed, {})
    by_code = {item.code: item for item in facts}

    assert by_code["ldl_c"].unit == "mmol/L"
    assert by_code["creatinine"].unit == "μmol/L"


def test_uric_acid_mg_dl_is_converted_before_using_si_threshold():
    request = _request(
        hospitalData=[
            _lab("血尿酸", "8.0", "mg/dL", "3.5-7.2", "2026-08-10"),
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    facts, _diagnoses = extract_assessment_facts(request, parsed, {})
    uric = next(item for item in facts if item.code == "uric_acid")

    assert uric.unit == "μmol/L"
    assert uric.numeric_value == pytest.approx(475.84)


def test_creatinine_without_safe_unit_cannot_be_used_to_derive_egfr():
    request = _request(
        hospitalData=[
            {
                "dataType": "INSPECTION",
                "itemName": "血肌酐",
                "testResultValue": "1.0",
                "reportTime": "2026-08-10",
            },
            _lab("血尿酸", "410", "μmol/L", "208-428", "2026-08-10"),
        ]
    )
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(request, parsed, {})
    kidney = next(
        item for item in assessment.dimensions if item.code == "urinary_system"
    )

    assert kidney.score_available is False
    assert any("明确单位" in item for item in kidney.missing_evidence)


def test_height_without_safe_unit_cannot_generate_an_invalid_bmi():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {"healthData": {"height": "1.75", "weight": "75", "waist": "86"}},
    )
    body = next(
        item for item in assessment.dimensions if item.code == "metabolic_system"
    )

    assert body.score_available is True
    assert not any(item.indicator_code == "bmi" for item in body.evidence_used)


def test_explicit_pound_weight_is_normalized_before_bmi_calculation():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {
            "healthData": {
                "height": "175 cm",
                "weight": "165 lb",
                "waist": "86 cm",
            }
        },
    )
    body = next(
        item for item in assessment.dimensions if item.code == "metabolic_system"
    )

    assert body.score_available is True
    bmi = next(item for item in body.evidence_used if item.indicator_code == "bmi")
    assert float(bmi.value) == pytest.approx(24.4, abs=0.1)


@pytest.mark.parametrize("name", ["体重指数", "体质量指数", "身体质量指数", "BMI", "Body Mass Index"])
def test_body_mass_index_is_not_a_weight_or_a_weight_conflict(name):
    request = _request(hospitalData=[
        _lab("体重", "70", "kg", "", "2026-08-10"),
        _lab(name, "20.56", "kg/(m*m)", "", "2026-08-10"),
        _lab("身高", "184.5", "cm", "", "2026-08-10"),
    ])
    parsed = parse_canonical_data(request, request.microbiome_data)
    facts, _ = extract_assessment_facts(request, parsed, {})
    assert [f.numeric_value for f in facts if f.code == "weight"] == [70]
    assessment = build_health_assessment(request, parsed, {})
    assert not any("weight存在" in conflict for d in assessment.dimensions for conflict in d.conflicts)


def test_minor_population_does_not_use_adult_body_composition_rule():
    request = _request(age=16)
    parsed = parse_canonical_data(request, request.microbiome_data)
    assessment = build_health_assessment(
        request,
        parsed,
        {"healthData": {"height": "170 cm", "weight": "60 kg", "waist": "72 cm"}},
    )
    body = next(
        item for item in assessment.dimensions if item.code == "metabolic_system"
    )

    assert body.score_available is True
    assert not any(item.indicator_code == "bmi" for item in body.evidence_used)


def test_scoring_has_no_network_dependency(monkeypatch):
    import socket

    def fail_network(*_args, **_kwargs):
        raise AssertionError("health assessment scoring attempted a network call")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)

    assessment = build_health_assessment(
        request,
        parsed,
        {
            "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
            "answers": ["不吸烟"],
        },
    )

    assert assessment.dimensions


def test_fixed_six_systems_exclude_mental_and_tumour_even_when_high_priority():
    request = ReportRequest.model_validate(
        {"userId": "u-eight", "sessionId": "s-eight", "age": 45, "sex": "女"}
    )
    parsed = ParsedMicrobiomeReport.model_validate(
        {
            "sourceType": "pdf",
            "sourceName": "eight-system.pdf",
            "subject": {"age": 45, "sex": "女"},
            "diseaseResults": [
                {"diseaseId": "d1", "name": "焦虑症", "sourceLevel": "高风险"},
                {"diseaseId": "d2", "name": "系统性红斑狼疮", "sourceLevel": "低风险"},
                {"diseaseId": "d3", "name": "结直肠癌", "sourceLevel": "中风险"},
                {"diseaseId": "d4", "name": "肾结石", "sourceLevel": "中风险"},
                {"diseaseId": "d5", "name": "帕金森病", "sourceLevel": "中风险"},
                {"diseaseId": "d6", "name": "高血压", "sourceLevel": "中风险"},
                {"diseaseId": "d7", "name": "炎症性肠病", "sourceLevel": "高风险"},
                {"diseaseId": "d8", "name": "肥胖", "sourceLevel": "中风险"},
            ],
        }
    )

    assessment = build_health_assessment(request, parsed, {})

    assert [item['code'] for item in assessment.model_dump(by_alias=True)['dimensions']] == [
        "cardiovascular_system",
        "metabolic_system", "digestive_system", "immune_system", "urinary_system", "nervous_system",
    ]
    digestive = next(item for item in assessment.dimensions if item.code == "digestive_system")
    assert digestive.score == 45
    assert digestive.related_diseases[0].risk_label == "高风险"
    assert all(item.weight == pytest.approx(1 / 6) for item in assessment.dimensions)


def test_fixed_six_output_order_is_stable_for_all_internal_orders():
    assessment = HealthAssessment()
    expected = assessment.model_dump(by_alias=True)
    for ordered in permutations(assessment.dimensions):
        shuffled = assessment.model_copy(update={"dimensions": list(ordered)})
        before = [id(item) for item in shuffled.dimensions]
        assert shuffled.model_dump(by_alias=True) == expected
        assert [id(item) for item in shuffled.dimensions] == before


@pytest.mark.parametrize("name", ["焦虑症", "肾癌", "胃癌"])
def test_removed_primary_systems_do_not_become_other_scored_diseases(name):
    request = ReportRequest(user_id="u", session_id="s")
    parsed = ParsedMicrobiomeReport(source_type="pdf", source_name="x", subject=SubjectInfo(),
                                   disease_results=[ParsedDisease(disease_id="d", name=name, source_level="高风险")])
    assessment = build_health_assessment(request, parsed, {})
    assert not any(item.score_available for item in assessment.dimensions)
    assert parsed.disease_results[0].name == name


def test_source_model_number_does_not_create_an_unaudited_risk_grade():
    request = ReportRequest.model_validate(
        {"userId": "u-pending", "sessionId": "s-pending"}
    )
    parsed = ParsedMicrobiomeReport.model_validate(
        {
            "sourceType": "pdf",
            "sourceName": "pending.pdf",
            "subject": {},
            "diseaseResults": [
                {
                    "diseaseId": "gastric-cancer",
                    "name": "胃癌",
                    "sourceScore": "99",
                }
            ],
        }
    )

    assessment = build_health_assessment(request, parsed, {})
    assert all(not item.score_available for item in assessment.dimensions)
    assert not any(item.code == "tumor_risk" for item in assessment.dimensions)
    assert parsed.disease_results[0].source_score == "99"


def test_secondary_disease_system_is_explanatory_and_not_double_selected():
    request = ReportRequest.model_validate(
        {"userId": "u-secondary", "sessionId": "s-secondary"}
    )
    parsed = ParsedMicrobiomeReport.model_validate(
        {
            "sourceType": "pdf",
            "sourceName": "secondary.pdf",
            "subject": {},
            "diseaseResults": [
                {"diseaseId": "d1", "name": "焦虑症", "sourceLevel": "高风险"},
                {"diseaseId": "d2", "name": "系统性红斑狼疮", "sourceLevel": "高风险"},
                {"diseaseId": "d3", "name": "结直肠癌", "sourceLevel": "中风险"},
                {"diseaseId": "d4", "name": "肾结石", "sourceLevel": "中风险"},
                {"diseaseId": "d5", "name": "帕金森病", "sourceLevel": "中风险"},
                {"diseaseId": "d6", "name": "高血压", "sourceLevel": "中风险"},
            ],
        }
    )

    assessment = build_health_assessment(request, parsed, {})
    codes = [item.code for item in assessment.dimensions]

    assert "tumor_risk" not in codes
    assert "mental_system" not in codes
    digestive = next(item for item in assessment.dimensions if item.code == "digestive_system")
    assert not digestive.score_available  # secondary association is not reclassified to primary
    assert any(item.name == "结直肠癌" and not item.primary for item in digestive.related_diseases)


def test_provider_function_fields_become_barrier_metrics_not_diseases():
    request = _request(microbiomeData=None)
    downloaded = DownloadedTestData(
        detail={
            "microbeData": {
                "comprehensive": [
                    {"latinName": "GMWI2", "value": "1.0"},
                    {"latinName": "Diversity_Index", "value": "7.0"},
                ],
                "Probiotics": [],
                "Pathogenic": [],
                "Conditional": [],
                "Fungi_Pathgen": [],
            },
            "diseaseRisk": {
                "肠道抗炎能力": "较弱",
                "丁酸合成能力": "轻度异常",
                "高脂血症": "1.2",
            },
        },
        detail_checksum="checksum",
    )
    parsed = parse_provider_data(request, downloaded)

    metrics = {item.code: item for item in parsed.metrics}
    assert metrics["anti_inflammatory"].status == "较弱"
    assert metrics["butyrate"].status == "轻度异常"
    assert [item.name for item in parsed.disease_results] == ["高脂血症"]


def test_scoring_is_deterministic_and_does_not_call_external_clients():
    request = _request()
    parsed = parse_canonical_data(request, request.microbiome_data)
    session = {
        "healthData": {"height": "175 cm", "weight": "75 kg", "waist": "86 cm"},
        "answers": ["不吸烟"],
    }

    first = build_health_assessment(request, parsed, session)
    second = build_health_assessment(request, parsed, session)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_existing_request_shape_is_unchanged_and_no_health_data_field_is_required():
    request = _request()
    assert "health_data" not in request.__class__.model_fields
    facts, _diagnoses = extract_assessment_facts(
        request,
        parse_canonical_data(request, request.microbiome_data),
        {},
    )
    assert facts
