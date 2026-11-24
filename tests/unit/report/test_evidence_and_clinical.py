import asyncio
import json

import httpx
import pytest

from adapters.report.clients import EviMedClient
from modules.report.clinical import (
    _evaluation_identity,
    _evaluation_rationale_for_report,
    _source_bound_fallback_evaluations,
    attach_suggested_evaluations,
    build_abnormal_microbes,
    build_source_diseases,
    disease_has_patient_microbiome_signal,
    evaluation_is_public_for_disease,
    is_hospital_common_examination,
    promote_evidence_disease_associations,
    recover_source_bound_diagnostic_interpretations,
)
from modules.report.evidence import (
    EvidenceQuery,
    EvidenceRetrievalResult,
    _abundance_relation_matches_source_direction,
    _canonical_condition_name,
    _claim_names_disease,
    _concept_match_score,
    _contains_disease_outcome_marker,
    _diagnostic_search_hints,
    _disease_record_priority_score,
    _disease_terms,
    _is_generic_microbe_condition,
    _is_public_condition_name,
    _match_related_taxon,
    _public_disease_claim,
    _public_relation_description,
    _screen,
    _source_explicitly_supports_relation,
    attach_evidence_refs,
    citation_numbers_for_semantic_records,
    consolidate_public_records,
    is_preclinical_evidence,
    make_candidate_queries,
    make_contextual_relation_queries,
    make_coverage_queries,
    make_function_barrier_queries,
    make_queries,
    make_relation_fallback_queries,
    merge_retrieval_results,
    number_evidence,
    promote_discovery_conditions_to_bindings,
    recover_exact_relation_interpretations,
    retain_referenced_evidence,
    retrieve_evidence,
    validate_relation_bindings,
)
from modules.report.narrative import interpret_evidence
from modules.report.condition_extraction import source_derived_conditions
from modules.report.pipeline import _bounded_evimed_coverage_queries
from modules.report.pipeline import _eligible_public_records
from modules.report.ports import EvidenceServiceError, EviMedChannelResult
from modules.report.schemas import (
    AbnormalMicrobeItem,
    AbnormalMicrobesSection,
    DiseaseRiskItem,
    DiseaseRisksSection,
    EvidenceRecord,
    EvidenceReference,
    EvidenceRelationBinding,
    HealthManagementSection,
    HealthRecommendation,
    HospitalDataInput,
    ParsedDisease,
    ParsedMicrobiomeReport,
    ParsedTaxon,
    PatientEvidenceContext,
    RiskAssociation,
    SampleInfo,
    SourceMetric,
    SubjectInfo,
    stable_id,
)


def test_source_low_and_zero_model_rows_do_not_consume_public_retrieval_budget():
    low = DiseaseRiskItem(
        diseaseId="disease-low",
        name="低风险项目",
        sourceType="source_report",
        sourceResult="原报告关联模型结果 3.83；低风险",
        modelIndex="3.83",
        relatedMicrobes=["普拉梭菌(缺乏)"],
    )
    medium = DiseaseRiskItem(
        diseaseId="disease-medium",
        name="中风险项目",
        sourceType="source_report",
        sourceResult="原报告关联模型结果 55.05；中风险",
        modelIndex="55.05",
        relatedMicrobes=["普拉梭菌(缺乏)"],
    )

    assert disease_has_patient_microbiome_signal(low) is False
    assert disease_has_patient_microbiome_signal(medium) is True


@pytest.mark.parametrize("label", ["新测试菌(缺乏)", "新测试菌（超标）", "新测试菌", "Novelus exampleus(偏低)"])
def test_source_relation_matches_source_bilingual_name_and_abundance_label(label):
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-novel", name="新测试菌", scientificName="Novelus exampleus",
        category="beneficial_abnormal", sourceFinding={"status": "缺乏", "value": "0.01"},
        conclusion="来源结果",
    )
    microbes = AbnormalMicrobesSection(items=[microbe])
    disease = DiseaseRiskItem(
        diseaseId="disease-novel", name="新来源疾病", sourceType="source_report",
        sourceResult="中风险", relatedMicrobes=[label],
    )
    queries = make_queries(microbes, DiseaseRisksSection(items=[disease]))
    relation = next(q for q in queries if q.intent == "microbe_disease_association")
    assert relation.microbe_id == "taxon-novel"
    assert relation.channels == ("literature",)
    assert relation.relation_source_taxon == label  # original audit value preserved
    assert "Novelus exampleus" in relation.query
    assert "Novelus exampleus" in relation.target
    assert relation.required_concepts[0][0] == "Novelus exampleus"
    assert _match_related_taxon("新测试菌(其他菌属)", microbes) == []
    assert _match_related_taxon("另一种菌(缺乏)", microbes) == []


def test_risk_outcome_marker_uses_word_boundaries():
    assert _contains_disease_outcome_marker("Cardiovascular risk in diabetes")
    assert not _contains_disease_outcome_marker(
        "First Latin American clinical practice guidelines for treatment"
    )


@pytest.mark.parametrize("word,expected", [
    ("pathogenesis", "epidemiologic_association"),
    ("pathogens", "established_pathogen"),
])
def test_condition_seed_does_not_confuse_pathogenesis_with_pathogen(word, expected):
    record = EvidenceRecord(
        evidenceKey="novel", category="literature", source="Journal",
        title="Novelus exampleus in ulcerative colitis",
        summary=f"Novelus exampleus abundance is lower in human stool in ulcerative colitis. This study discusses {word}.",
        targets=["Novelus exampleus / 新测试菌 / 溃疡性结肠炎"],
    )
    conditions, _ = source_derived_conditions(record)
    assert conditions
    assert all(c.relation_type == expected for c in conditions)


@pytest.mark.parametrize("source_direction,model_direction,expected", [
    ("lower levels", "decreased", True),
    ("higher levels", "increased", True),
    ("lower levels", "increased", False),
    ("measured levels", "decreased", False),
])
def test_literal_abundance_comparison_survives_background_treatment_word(
    source_direction, model_direction, expected,
):
    microbe = AbnormalMicrobeItem(
        microbeId="novel", name="新测试菌", scientificName="Novelus exampleus",
        category="other_abnormal", conclusion="来源事实",
        sourceFinding={"status": "偏低" if model_direction == "decreased" else "偏高", "value": "1"},
    )
    microbes = AbnormalMicrobesSection(items=[microbe])
    record = EvidenceRecord(
        evidenceKey="comparison", category="literature", source="Journal",
        title="Novelus exampleus in ulcerative colitis: a systematic review",
        summary=(f"Adult stool samples showed {source_direction} of Novelus exampleus in ulcerative colitis compared with controls. "
                 "Studies differed in previous treatments and baseline characteristics."),
        intents=["microbe_disease_association"], supports=["novel"], microbeIds=["novel"],
        interpretation={
            "claim": "溃疡性结肠炎患者中新测试菌水平存在变化。", "direction": model_direction,
            "targetAlignment": "direct", "taxonAlignment": "species",
            "sampleAlignment": "stool", "patientApplicability": "contextual",
        },
        relationBindings=[{
            "relationId": "r", "microbeId": "novel", "microbeName": "Novelus exampleus",
            "microbeAliases": ["新测试菌"], "diseaseId": "uc", "diseaseName": "溃疡性结肠炎",
            "relationType": "epidemiologic_association",
        }],
    )
    public = _eligible_public_records([record], microbes=microbes, subject=SubjectInfo(age=30))
    assert bool(public) is expected
    attach_evidence_refs(microbes, DiseaseRisksSection(), public, {"comparison": 1})
    assert bool(microbe.disease_and_risks) is expected


def test_evidence_numbers_are_stable_when_equal_rank_records_arrive_reordered():
    records = [
        EvidenceRecord(
            evidenceKey=key,
            category="literature",
            title="Same title",
            source="Same source",
            year=2025,
            relevance=80,
        )
        for key in ("record-b", "record-a")
    ]

    first_refs, first_numbers = number_evidence(records)
    second_refs, second_numbers = number_evidence(list(reversed(records)))

    assert first_numbers == second_numbers == {"record-a": 1, "record-b": 2}
    assert [item.title for item in first_refs] == [item.title for item in second_refs]


def test_hospital_check_admission_excludes_self_tools_and_accepts_clinical_tests():
    assert not is_hospital_common_examination("布里斯托大便性状量表")
    assert not is_hospital_common_examination("罗马IV标准评估")
    assert is_hospital_common_examination("艰难梭菌GDH抗原及毒素检测")
    assert is_hospital_common_examination("肛门直肠测压")
    assert is_hospital_common_examination("粪便病原菌培养")
    assert is_hospital_common_examination("血培养")


def test_diagnostic_retry_terms_match_evimed_guide_indexing():
    assert "ultrasound guideline" in _diagnostic_search_hints("肾结石")
    assert (
        "noncontrast CT acute flank pain"
        in _diagnostic_search_hints("肾结石")
    )
    assert "urolithiasis" in _disease_terms("肾结石")
    assert "kidney stone" in _disease_terms("肾结石")
    assert "calcium oxalate stone" in _disease_terms("肾结石")
    assert (
        "infectious diarrhea stool pathogen testing guideline"
        in _diagnostic_search_hints("食物中毒")
    )
    assert "infectious diarrhea" in _disease_terms("食物中毒")
    assert (
        _diagnostic_search_hints("溃疡性结肠炎")[0]
        == (
            "Role of biomarkers for the management of ulcerative colitis "
            "fecal calprotectin"
        )
    )
    assert (
        _diagnostic_search_hints("甲状腺疾病")[0]
        == "thyroid disease 甲状腺功能异常 TSH FT4 诊断 指南"
    )


def test_thyroid_disease_recovers_source_bound_first_line_function_test():
    disease = DiseaseRiskItem(
        disease_id="disease-thyroid",
        name="甲状腺疾病",
        source_type="source_report",
    )
    record = EvidenceRecord(
        evidence_key="thyroid-function-consensus",
        category="guideline",
        title="体检人群甲状腺功能检测管理专家共识",
        source="专家共识",
        summary=(
            "甲状腺功能异常的评估建议进行促甲状腺激素（TSH）"
            "和游离甲状腺素（FT4）检测。"
        ),
        supports=[disease.disease_id],
        targets=["甲状腺疾病"],
        intents=["diagnostic_guideline"],
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
        subject_age=57,
    )

    assert len(disease.suggested_evaluations) == 1
    assert disease.suggested_evaluations[0].name == "甲状腺功能检查（TSH、FT4）"
    assert disease.suggested_evaluations[0].purpose == "评估"
    assert disease.suggested_evaluations[0].evidence_refs == [1]


def test_promoted_disease_diagnostic_queries_use_ranked_guide_phrases():
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId=f"disease-{name}",
                name=name,
                sourceType="evimed_association",
            )
            for name in ("肾结石", "溃疡性结肠炎", "食物中毒")
        ]
    )

    queries = make_coverage_queries(
        AbnormalMicrobesSection(items=[]),
        diseases,
        [],
        age=57,
    )
    diagnostic_queries = {
        query.query for query in queries if query.intent == "diagnostic_guideline"
    }

    assert "kidney stone ultrasound guideline" in diagnostic_queries
    assert (
        "Role of biomarkers for the management of ulcerative colitis "
        "fecal calprotectin"
    ) in diagnostic_queries
    assert "infectious diarrhea stool pathogen testing guideline" in diagnostic_queries
    assert not any(
        query.startswith("food poisoning infectious diarrhea")
        for query in diagnostic_queries
    )


@pytest.mark.parametrize(
    ("disease_name", "title", "summary", "expected_name"),
    (
        (
            "结直肠癌",
            "Colorectal cancer screening guideline",
            "Colonoscopy is used to screen and diagnose colorectal cancer.",
            "结肠镜检查",
        ),
        (
            "肾结石",
            "Kidney stones diagnostic guideline",
            "Non-contrast CT is used to confirm kidney stones.",
            "非增强螺旋CT检查",
        ),
        (
            "肾结石",
            "Urolithiasis diagnostic guideline",
            "Renal ultrasound is the primary imaging test for urolithiasis.",
            "泌尿系超声检查",
        ),
        (
            "肾结石",
            "Urolithiasis clinical evaluation guideline",
            "Urinalysis is included in the clinical evaluation of urolithiasis.",
            "尿常规检查",
        ),
        (
            "胃肠炎",
            "Infectious gastroenteritis diagnostic guideline",
            "Faecal culture is used to confirm infectious gastroenteritis.",
            "粪便病原菌培养",
        ),
        (
            "食物中毒",
            "Food poisoning diagnostic guideline",
            "Stool culture is used to confirm food poisoning.",
            "粪便病原菌培养",
        ),
        (
            "食物中毒",
            "Guidelines on acute infectious diarrhea in adults",
            (
                "Stool testing is used to detect pathogens in adults with "
                "acute infectious diarrhea."
            ),
            "粪便病原学检查",
        ),
        (
            "菌血症",
            "Bacteremia diagnostic guideline",
            "Blood cultures are used to confirm bacteremia.",
            "血培养",
        ),
    ),
)
def test_source_bound_common_hospital_test_survives_model_omission(
    disease_name: str,
    title: str,
    summary: str,
    expected_name: str,
):
    disease = DiseaseRiskItem(
        diseaseId=f"disease-{disease_name}",
        name=disease_name,
        sourceType="evimed_association",
        relatedMicrobes=["测试菌"],
        associationEvidenceRefs=[1],
        evidenceRefs=[1],
    )
    record = EvidenceRecord(
        evidenceKey=f"guide-{disease_name}",
        category="guideline",
        title=title,
        summary=summary,
        source="EviMed",
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=[disease_name],
    )

    recover_source_bound_diagnostic_interpretations(
        DiseaseRisksSection(items=[disease]),
        [record],
        subject_age=57,
    )
    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 2},
        subject_age=57,
    )

    assert [item.name for item in disease.suggested_evaluations] == [expected_name]
    assert disease.suggested_evaluations[0].evidence_refs == [2]


def test_exact_relation_source_can_supply_common_hospital_confirmation_test():
    disease = DiseaseRiskItem(
        diseaseId="evidence-disease-gastroenteritis",
        name="胃肠炎",
        sourceType="evimed_association",
        relatedMicrobes=["沙门氏菌"],
        associationEvidenceRefs=[7],
        evidenceRefs=[7],
    )
    record = EvidenceRecord(
        evidenceKey="salmonella-gastroenteritis-cohort",
        category="literature",
        title="Non-typhoidal Salmonella gastroenteritis in adults",
        summary=(
            "Faecal culture confirmed non-typhoidal Salmonella "
            "gastroenteritis in the adult cohort."
        ),
        source="PubMed",
        supports=["taxon-salmonella"],
        intents=["microbe_disease_association"],
        targets=["Salmonella", "胃肠炎"],
        interpretation={
            "claim": "沙门氏菌与胃肠炎相关。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "taxonAlignment": "genus",
            "sampleAlignment": "stool",
            "recommendedEvaluations": [],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 7},
        subject_age=57,
    )

    assert [item.name for item in disease.suggested_evaluations] == [
        "粪便病原菌培养"
    ]
    assert disease.suggested_evaluations[0].evidence_refs == [7]


def test_disease_claim_rejects_incidental_target_in_off_target_source():
    record = EvidenceRecord(
        evidenceKey="incidental-coronary-disease",
        category="guideline",
        title="Diabetic myocardial disorder: a clinical consensus statement",
        source="European Journal of Heart Failure",
        summary=(
            "Diabetic myocardial disorder is defined as myocardial dysfunction "
            "in diabetes and often coexists with coronary artery disease."
        ),
        targets=["冠心病"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "冠心病中，糖尿病相关心肌病可表现为心肌功能障碍。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) is None


def test_direct_human_gut_association_outranks_generic_disease_definition():
    definition = EvidenceRecord(
        evidenceKey="crc-definition",
        category="guideline",
        title="Colorectal cancer guideline",
        source="Society",
        summary="Colorectal cancer is a malignant disease.",
        targets=["结直肠癌"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "结直肠癌是一种消化道恶性肿瘤。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    association = EvidenceRecord(
        evidenceKey="crc-gut-association",
        category="literature",
        title="Human stool gut microbiome association with colorectal cancer",
        source="Journal",
        summary=(
            "A human stool cohort reported gut microbiome features associated "
            "with colorectal cancer."
        ),
        targets=["结直肠癌"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "人群研究显示肠道菌群特征与结直肠癌存在关联。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "sampleAlignment": "stool",
            "studyDesign": "队列研究",
        },
    )

    assert _disease_record_priority_score(
        association
    ) > _disease_record_priority_score(definition)


def test_attach_disease_refs_uses_human_gut_association_not_guideline():
    disease = DiseaseRiskItem(
        diseaseId="disease-crc",
        name="结直肠癌",
        sourceType="source_report",
        sourceResult="原报告关联模型结果 72；中风险",
    )
    definition = EvidenceRecord(
        evidenceKey="crc-guide",
        category="guideline",
        title="Colorectal cancer guideline",
        source="Society",
        supports=[disease.disease_id],
        diseaseIds=[disease.disease_id],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌是一种消化道恶性肿瘤。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    association = EvidenceRecord(
        evidenceKey="crc-human-stool",
        category="literature",
        title="Human stool gut microbiome association with colorectal cancer",
        source="Journal",
        summary=(
            "A human stool cohort observed gut microbiome features associated "
            "with colorectal cancer."
        ),
        supports=[disease.disease_id],
        diseaseIds=[disease.disease_id],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
        interpretation={
            "claim": "人体粪便研究观察到肠道菌群特征与结直肠癌相关。",
            "studyDesign": "队列研究",
            "targetAlignment": "direct",
            "sampleAlignment": "stool",
            "patientApplicability": "contextual",
        },
    )

    attach_evidence_refs(
        AbnormalMicrobesSection(),
        DiseaseRisksSection(items=[disease]),
        [definition, association],
        {definition.evidence_key: 1, association.evidence_key: 2},
    )

    assert disease.association_evidence_refs == [2]
    assert "肠道菌群" in disease.medical_interpretation
    assert "是一种" not in disease.medical_interpretation


def test_promote_direct_stool_microbe_relation_reuses_source_disease_row():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-fn",
        name="具核梭杆菌",
        scientificName="Fusobacterium nucleatum",
        category="opportunistic_signal",
        sourceFinding={"status": "丰度高", "value": "1.2", "unit": "%"},
        diseaseAndRisks=[
            RiskAssociation(
                name="结直肠癌",
                description="人体粪便研究观察到具核梭杆菌丰度与结直肠癌相关。",
                evidenceRefs=[3],
                patientApplicability="direct",
                sampleAlignment="stool",
            )
        ],
    )
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="provider-placeholder-crc",
                name="结直肠癌",
                sourceType="source_report",
            )
        ]
    )

    promote_evidence_disease_associations(
        diseases,
        AbnormalMicrobesSection(items=[microbe]),
    )

    assert len(diseases.items) == 1
    promoted = diseases.items[0]
    assert promoted.source_type == "source_report"
    assert promoted.related_microbes == ["具核梭杆菌"]
    assert promoted.association_evidence_refs == [3]
    assert "丰度高" in promoted.source_result
    assert "健康关注线索" in promoted.medical_interpretation


def test_promote_contextual_human_stool_relation_but_not_cross_site_relation():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-contextual",
        name="示例菌",
        scientificName="Example bacterium",
        category="beneficial_abnormal",
        sourceFinding={"status": "丰度低", "value": "0.2", "unit": "%"},
        diseaseAndRisks=[
            RiskAssociation(
                name="肠道健康风险",
                description="人体粪便研究观察到示例菌变化与肠道健康风险相关。",
                evidenceRefs=[4],
                patientApplicability="contextual",
                sampleAlignment="stool",
            ),
            RiskAssociation(
                name="呼吸道感染",
                description="呼吸道样本研究观察到相关性。",
                evidenceRefs=[5],
                patientApplicability="contextual",
                sampleAlignment="respiratory",
            ),
        ],
    )
    diseases = DiseaseRisksSection()

    promote_evidence_disease_associations(
        diseases,
        AbnormalMicrobesSection(items=[microbe]),
    )

    assert [item.name for item in diseases.items] == ["肠道健康风险"]
    assert diseases.items[0].related_microbes == ["示例菌"]
    assert diseases.items[0].association_evidence_refs == [4]


def test_low_pathogen_signal_does_not_promote_established_infection():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-low-pathogen",
        name="示例病原菌",
        scientificName="Example pathogen",
        category="pathogenic_signal",
        sourceFinding={"status": "丰度低", "value": "0.01", "unit": "%"},
        diseaseAndRisks=[
            RiskAssociation(
                name="示例感染",
                description="人体粪便资料说明该菌可引起示例感染。",
                relationType="established_pathogen",
                evidenceRefs=[6],
                patientApplicability="contextual",
                sampleAlignment="stool",
            )
        ],
    )
    diseases = DiseaseRisksSection()

    promote_evidence_disease_associations(
        diseases,
        AbnormalMicrobesSection(items=[microbe]),
    )

    assert diseases.items == []


def test_unresolved_provider_disease_placeholder_does_not_create_queries():
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="provider-placeholder",
                name="结直肠癌",
                sourceType="source_report",
            )
        ]
    )

    queries = make_queries(AbnormalMicrobesSection(), diseases)
    assert not any(query.disease_id for query in queries)


def test_disease_gut_association_profile_rejects_generic_guideline():
    record = EvidenceRecord(
        evidenceKey="crc-generic-guide",
        category="guideline",
        title="Colorectal cancer clinical guideline",
        source="Society",
        summary="This guideline defines colorectal cancer in adults.",
    )
    query = EvidenceQuery(
        query="colorectal cancer human gut microbiome association",
        supports=("disease-crc",),
        required_concepts=(("colorectal cancer",),),
        intent="disease_clinical_significance",
        target="结直肠癌",
        disease_id="disease-crc",
        screen_profile="gut_association",
    )

    assert _screen(record, query=query) == (False, "not_association_study")


@pytest.mark.parametrize(
    ("target", "title", "claim"),
    (
        (
            "溃疡性结肠炎",
            "In-hospital complications in ulcerative colitis flares",
            "溃疡性结肠炎住院患者中，缺乏第3天类固醇应答与并发症风险增加相关。",
        ),
        (
            "冠心病",
            "The epidemic of the 20th century: coronary heart disease",
            "冠心病在20世纪成为美国最常见的死亡原因。",
        ),
        (
            "糖化血红蛋白",
            "Whole-genome associations with hemoglobin A1c",
            "糖化血红蛋白相关遗传变异在不同祖先人群中存在差异。",
        ),
        (
            "结直肠癌",
            "Colorectal cancer review",
            "结直肠癌是西方第三大常见癌症。",
        ),
        (
            "胰岛素抵抗",
            "Insulin resistance consensus",
            "胰岛素抵抗是这些疾病的共同病理生理基础。",
        ),
    ),
)
def test_disease_claim_rejects_narrow_historical_genetic_or_vague_prose(
    target, title, claim
):
    record = EvidenceRecord(
        evidenceKey=f"narrow-{stable_id('claim', target)}",
        category="literature",
        title=title,
        source="Journal",
        summary=claim,
        targets=[target],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) is None


def test_source_bound_evaluation_rejects_research_method_without_clinical_purpose():
    disease = DiseaseRiskItem(
        diseaseId="disease-ir",
        name="胰岛素抵抗",
        sourceType="source_report",
    )
    methods_only = EvidenceRecord(
        evidenceKey="homa-method-only",
        category="literature",
        title="Insulin resistance and metabolic syndrome",
        source="Journal",
        summary=(
            "Insulin resistance and beta cell function were calculated using "
            "homeostasis model assessment (HOMA-IR and HOMA-B)."
        ),
        targets=["胰岛素抵抗"],
        intents=["diagnostic_guideline"],
    )
    clinically_bound = methods_only.model_copy(
        update={
            "evidence_key": "homa-clinical-purpose",
            "summary": "HOMA-IR is used to assess insulin resistance in adults.",
        }
    )

    assert _source_bound_fallback_evaluations(disease, methods_only, subject_age=28) == []
    assert [
        item.name
        for item in _source_bound_fallback_evaluations(
            disease, clinically_bound, subject_age=28
        )
    ] == ["HOMA-IR指数"]


@pytest.mark.parametrize(
    ("title", "summary", "expected"),
    (
        (
            "Fatty liver disease diagnostic criteria",
            (
                "Abdominal ultrasonography was performed to diagnose fatty "
                "liver disease in adults."
            ),
            "肝脏超声检查",
        ),
        (
            "Nonalcoholic fatty liver disease clinical guideline",
            (
                "Vibration-controlled transient elastography is used to assess "
                "liver stiffness in patients with nonalcoholic fatty liver disease."
            ),
            "肝脏瞬时弹性成像",
        ),
    ),
)
def test_source_bound_nafld_followup_recovers_only_explicit_examination(
    title: str,
    summary: str,
    expected: str,
):
    disease = DiseaseRiskItem(
        diseaseId="clinical-nafld",
        name="非酒精性脂肪肝",
        sourceType="clinical_context",
    )
    record = EvidenceRecord(
        evidenceKey=f"nafld-{stable_id('test', expected)}",
        category="guideline",
        title=title,
        source="Clinical guideline",
        summary=summary,
        targets=["非酒精性脂肪肝"],
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
    )

    evaluations = _source_bound_fallback_evaluations(
        disease,
        record,
        subject_age=48,
    )

    assert [item.name for item in evaluations] == [expected]


def test_source_bound_nafld_followup_does_not_infer_test_from_title_alone():
    disease = DiseaseRiskItem(
        diseaseId="clinical-nafld",
        name="非酒精性脂肪肝",
        sourceType="clinical_context",
    )
    record = EvidenceRecord(
        evidenceKey="nafld-generic-guideline",
        category="guideline",
        title="Guidelines for nonalcoholic fatty liver disease",
        source="Clinical guideline",
        summary="The guideline discusses assessment and management in adults.",
        targets=["非酒精性脂肪肝"],
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
    )

    assert (
        _source_bound_fallback_evaluations(disease, record, subject_age=48) == []
    )


def test_adult_diagnostic_screen_keeps_mixed_age_guideline():
    query = EvidenceQuery(
        "sleep disorder actigraphy diagnostic guideline",
        ("disease-sleep",),
        (("睡眠障碍", "sleep disorder", "sleep disorders"),),
        intent="diagnostic_guideline",
        target="睡眠障碍",
        channels=("guide",),
        age_group="adult",
        query_language="english",
        disease_id="disease-sleep",
        screen_profile="diagnostic",
    )
    mixed = EvidenceRecord(
        evidenceKey="mixed-age-actigraphy",
        category="guideline",
        title="Actigraphy for the evaluation of sleep disorders",
        source="Sleep Society",
        summary=(
            "The guideline recommends actigraphy for adult patients with "
            "insomnia and also assesses pediatric patients."
        ),
    )
    pediatric_only = mixed.model_copy(
        update={
            "evidence_key": "pediatric-actigraphy",
            "summary": "The guideline assesses pediatric patients and children.",
        }
    )

    assert _screen(mixed, query=query)[0] is True
    assert _screen(pediatric_only, query=query) == (False, "population_mismatch")


def test_diabetes_diagnostic_screen_rejects_nsclc_prognosis_study():
    query = EvidenceQuery(
        "type 2 diabetes fasting plasma glucose diagnostic guideline",
        ("disease-diabetes",),
        (("II型糖尿病", "type 2 diabetes"),),
        intent="diagnostic_guideline",
        target="II型糖尿病",
        channels=("literature",),
        age_group="adult",
        query_language="english",
        disease_id="disease-diabetes",
        screen_profile="diagnostic",
    )
    record = EvidenceRecord(
        evidenceKey="nsclc-glucose",
        category="literature",
        title="Impact of fasting plasma glucose in advanced NSCLC",
        source="Oncology journal",
        summary=(
            "Fasting plasma glucose was assessed for prognosis in NSCLC; "
            "some participants had type 2 diabetes."
        ),
    )

    assert _screen(record, query=query) == (
        False,
        "restricted_or_comorbid_context",
    )


def test_depression_diagnostic_screen_rejects_cystic_fibrosis_registry():
    query = EvidenceQuery(
        "depression PHQ-9 diagnostic guideline",
        ("disease-depression",),
        (("抑郁症", "depression", "depressive disorder"),),
        intent="diagnostic_guideline",
        target="抑郁症",
        channels=("literature",),
        age_group="adult",
        query_language="english",
        disease_id="disease-depression",
        screen_profile="diagnostic",
    )
    record = EvidenceRecord(
        evidenceKey="cf-phq9",
        category="literature",
        title=(
            "A national survey of depression screening in the "
            "Cystic Fibrosis Foundation Patient Registry"
        ),
        source="Special-population journal",
        summary=(
            "Adults with cystic fibrosis were screened for depression "
            "using the PHQ-9."
        ),
    )

    assert _screen(record, query=query) == (
        False,
        "restricted_or_comorbid_context",
    )


def test_unmeasured_resistance_subtype_is_not_a_public_condition():
    assert not _is_public_condition_name("耐药金黄色葡萄球菌感染")
    assert not _is_public_condition_name("难治性铜绿假单胞菌感染")
    assert not _is_public_condition_name("定植")
    assert not _is_public_condition_name("医院获得性感染")
    assert not _is_public_condition_name("医院感染")
    assert not _is_public_condition_name("金黄色葡萄球菌复合群感染")
    assert not _is_public_condition_name("非结核分枝杆菌感染")
    assert not _is_public_condition_name("仔猪链球菌病")
    assert not _is_public_condition_name("孕产妇感染")
    assert not _is_public_condition_name("医院获得性腹泻")
    assert _is_public_condition_name("金黄色葡萄球菌感染")
    assert _is_public_condition_name("猪链球菌病")


def test_veterinary_journal_source_is_preclinical_evidence():
    veterinary = EvidenceRecord(
        evidence_key="veterinary-piglet",
        category="literature",
        title="猪链球菌病的诊治要点",
        source="畜牧兽医科技信息",
        summary="讨论养殖场猪链球菌病的防治。",
        interpretation={
            "claim": "猪链球菌与猪链球菌病相关。",
            "targetAlignment": "direct",
        },
    )
    human = EvidenceRecord(
        evidence_key="human-suis-meningitis",
        category="literature",
        title="Streptococcus suis Meningitis: A Systematic Review",
        source="Clinical Infectious Diseases",
        summary="Streptococcus suis causes meningitis in humans.",
        interpretation={
            "claim": "Streptococcus suis causes meningitis in humans.",
            "targetAlignment": "direct",
        },
    )

    assert is_preclinical_evidence(veterinary)
    assert not is_preclinical_evidence(human)


def test_generic_microbe_infection_is_only_a_fallback_label():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-staphylococcus-aureus",
        name="金黄色葡萄球菌",
        scientificName="Staphylococcus aureus",
        category="pathogenic_signal",
        sourceFinding={"status": "检出"},
    )
    assert _is_generic_microbe_condition("金黄色葡萄球菌感染", microbe)
    assert _is_generic_microbe_condition("Staphylococcus aureus infection", microbe)
    assert not _is_generic_microbe_condition("特应性皮炎", microbe)


@pytest.mark.parametrize(
    "claim",
    (
        "肠沙门氏菌是沙门氏菌病的病原体。",
        "肠沙门氏菌是肠热病和胃肠炎的病原体。",
        "Salmonella enterica causes salmonellosis.",
    ),
)
def test_salmonellosis_binding_accepts_medically_equivalent_outcome_names(claim):
    assert _claim_names_disease(claim, "沙门菌病")


def parsed_report():
    return ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(name="测试患者", age=28, sex="男"),
        sample=SampleInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-fn",
                category="opportunistic",
                latin_name="Fusobacterium nucleatum",
                value="0.2",
                detection_status="detected",
            )
        ],
    )

def test_function_and_barrier_queries_are_source_bound_and_have_no_disease_ids():
    parsed = parsed_report().model_copy(
        update={
            "metrics": [
                SourceMetric(
                    code="butyrate",
                    name="丁酸合成能力",
                    value="47",
                    status="稍弱",
                ),
                SourceMetric(
                    code="gut_barrier",
                    name="肠道屏障功能",
                    value="50",
                    status="稍弱",
                ),
            ]
        }
    )
    microbes = build_abnormal_microbes(parsed)
    queries = make_function_barrier_queries(parsed, microbes, age=28)

    assert {query.intent for query in queries} == {"gut_function", "gut_barrier"}
    assert all(query.microbe_id == microbes.items[0].microbe_id for query in queries)
    assert all(query.disease_id is None for query in queries)
    assert all(query.relation_id is None for query in queries)
    assert len(queries) == 2


def test_contextual_relation_queries_are_high_value_bounded_and_skip_missing_context():
    base = EvidenceQuery(
        query="Fusobacterium nucleatum colorectal cancer",
        supports=("taxon-fn", "disease-crc"),
        required_concepts=(
            ("Fusobacterium nucleatum",),
            ("colorectal cancer", "结直肠癌"),
        ),
        intent="microbe_disease_association",
        target="Fusobacterium nucleatum / 具核梭杆菌 / 结直肠癌",
        relation_disease_id="disease-crc",
        relation_disease_name="结直肠癌",
        relation_source_taxon="Fusobacterium nucleatum",
        microbe_id="taxon-fn",
        disease_id="disease-crc",
        relation_id="relation-crc",
        relation_type="epidemiologic_association",
    )
    context = PatientEvidenceContext(
        age=28,
        ageBand="adult_18_44",
        bmi=29.0,
        bmiGroup="obesity",
    )
    queries = make_contextual_relation_queries(
        [base, base],
        [],
        context,
        max_queries=1,
    )
    assert len(queries) == 1
    assert queries[0].query.endswith("adult")
    assert queries[0].relation_id == "relation-crc"
    assert (
        make_contextual_relation_queries(
            [base],
            [],
            PatientEvidenceContext(),
        )
        == []
    )


class FailingEvidenceClient:
    async def search(self, query, *, supports):
        raise EvidenceServiceError("unavailable")


class MixedEvidenceClient:
    async def search(self, query, *, supports):
        return [
            EvidenceRecord(
                evidence_key="gorilla",
                category="literature",
                title="Gorilla gut microbiome reservoir",
                source="Journal",
                summary=(
                    "Fusobacterium nucleatum was identified in the gut microbiome "
                    "of Gorilla gorilla gorilla."
                ),
                supports=supports,
            ),
            EvidenceRecord(
                evidence_key="canine",
                category="literature",
                title="Clinical study of acute diarrhea in dogs",
                source="Veterinary Journal",
                summary=(
                    "A canine clinical trial identified Fusobacterium nucleatum "
                    "abundance in the gut microbiome as associated with diarrhea."
                ),
                supports=supports,
            ),
            EvidenceRecord(
                evidence_key="human",
                category="literature",
                title="Human colorectal cancer cohort",
                source="Gut",
                summary=(
                    "A human patient cohort identified Fusobacterium nucleatum "
                    "abundance in the gut microbiome as associated with colorectal cancer."
                ),
                supports=supports,
            ),
        ]


@pytest.mark.asyncio
async def test_evimed_retries_one_transient_http_failure():
    attempts = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        requests.append(request)
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 200,
                "data": {
                    "literature": {
                        "total": 1,
                        "list": [
                            {
                                "id": "lit-1",
                                "title": "Human gut microbiome cohort",
                                "abstract": "Human gut microbiome association study.",
                                "journal": "Gut",
                                "authors": ["A Researcher"],
                                "studyType": ["队列研究"],
                                "impactFactor": 12.5,
                            }
                        ],
                    }
                },
            },
        )

    client = EviMedClient(
        base_url="https://evidence.example/review/api",
        api_key="test-key",
        attempts=2,
        transport=httpx.MockTransport(handler),
    )
    records = await client._search_channel(
        "literature",
        "human gut microbiome",
        supports=["taxon-fn"],
    )
    assert attempts == 2
    assert [record.title for record in records] == ["Human gut microbiome cohort"]
    assert requests[-1].url.path.endswith("/review/api/v2/literature-guide")
    request_body = json.loads(requests[-1].content)
    assert request_body == {
        "query": "human gut microbiome",
        "type": "literature",
        "useLlm": False,
        "count": 15,
        "articleTypes": [
            "系统综述/Meta分析",
            "传统综述",
            "随机对照试验",
            "队列研究",
            "病例对照研究",
            "横断面研究",
            "指南/共识",
            "临床试验",
        ],
    }
    assert records[0].provider == "evimed_v2"
    assert records[0].authors == ["A Researcher"]
    assert records[0].study_types == ["队列研究"]
    assert records[0].impact_factor == 12.5


@pytest.mark.asyncio
async def test_evimed_v2_guide_blocks_keep_parent_citation_metadata():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 200,
                "data": {
                    "guide": {
                        "total": 1,
                        "list": [
                            {
                                "id": "guide-1",
                                "title": "Clinical guideline",
                                "year": "2025",
                                "publisher": "Medical Society",
                                "summary": "Parent summary",
                                "url": "https://evidence.example/guide-1",
                            }
                        ],
                        "blocks": [
                            {
                                "guideId": "guide-1",
                                "title": "Clinical guideline",
                                "year": "2025",
                                "publisher": "Medical Society",
                                "language": "en",
                                "text": "Use the named examination to confirm the target condition.",
                            }
                        ],
                    }
                },
            },
        )

    client = EviMedClient(
        base_url="https://evidence.example/review/api",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    records = await client._search_channel(
        "guide",
        "target condition diagnosis",
        supports=["disease-target"],
    )

    assert len(records) == 2
    parent, block = records
    assert block.parent_guide_id == "guide-1"
    assert block.source_evidence_key == parent.evidence_key
    assert block.evidence_text == (
        "Use the named examination to confirm the target condition."
    )
    assert block.url == parent.url
    request_body = json.loads(requests[0].content)
    assert request_body["type"] == "guide"
    assert request_body["searchBlock"] is True


@pytest.mark.asyncio
async def test_evimed_cache_collapses_concurrent_identical_queries():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 200,
                "data": {
                    "literature": {
                        "total": 1,
                        "list": [
                            {
                                "id": "shared",
                                "title": "Shared human cohort",
                                "abstract": "A human cohort study.",
                                "journal": "Journal",
                            }
                        ],
                    }
                },
            },
        )

    client = EviMedClient(
        base_url="https://evidence.example/review/api",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    first, second = await asyncio.gather(
        client._search_channel("literature", "shared query", supports=["a"]),
        client._search_channel("literature", " shared  query ", supports=["b"]),
    )

    assert attempts == 1
    assert first[0].supports == ["a"]
    assert second[0].supports == ["b"]


@pytest.mark.asyncio
async def test_evimed_failure_keeps_source_microbe_without_invented_risk():
    parsed = parsed_report()
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    result = await retrieve_evidence(
        FailingEvidenceClient(),
        [
            EvidenceQuery(
                "query",
                ("taxon-fn",),
                (("Fusobacterium nucleatum",),),
            )
        ],
    )
    assert [item.scientific_name for item in microbes.items] == [
        "Fusobacterium nucleatum"
    ]
    assert microbes.items[0].disease_and_risks == []
    assert diseases.items == []
    assert result.failed_supports == {"taxon-fn"}


@pytest.mark.asyncio
async def test_evimed_screening_rejects_nonhuman_microbiome_hit():
    result = await retrieve_evidence(
        MixedEvidenceClient(),
        [
            EvidenceQuery(
                "query",
                ("taxon-fn",),
                (("Fusobacterium nucleatum",),),
            )
        ],
    )
    assert [record.source_evidence_key for record in result.records] == ["human"]


@pytest.mark.asyncio
async def test_diagnostic_guideline_does_not_require_microbiome_word():
    class Client:
        async def search(self, query, *, supports):
            return [
                EvidenceRecord(
                    evidence_key="diagnosis",
                    category="guideline",
                    title="Clinical guideline for diagnosis and assessment of inflammatory bowel disease",
                    source="Society",
                    summary="The guideline covers diagnostic evaluation and colonoscopy.",
                    supports=supports,
                ),
                EvidenceRecord(
                    evidence_key="fmt",
                    category="guideline",
                    title="Fecal microbiota transplantation guideline for inflammatory bowel disease",
                    source="Society",
                    summary="Treatment recommendations for fecal microbiota transplantation.",
                    supports=supports,
                ),
            ]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                "query",
                ("disease-ibd",),
                (("inflammatory bowel disease",),),
                intent="diagnostic_guideline",
                target="炎症性肠病",
            )
        ],
    )
    assert [item.source_evidence_key for item in result.records] == ["diagnosis"]
    assert result.query_audits[0].rejected_reasons == {"non_diagnostic_guideline": 1}


@pytest.mark.asyncio
async def test_same_evimed_source_gets_distinct_keys_for_distinct_report_purposes():
    class Client:
        async def search(self, query, *, supports):
            return [
                EvidenceRecord(
                    evidence_key="constipation-source",
                    category="guideline",
                    title="2022 Seoul Consensus on Clinical Practice Guidelines for Functional Constipation",
                    source="Society",
                    summary=(
                        "Functional constipation is a digestive disease in adults and may "
                        "present with hard stools and straining. "
                        "The guideline recommends regular bowel habits for adults."
                    ),
                    supports=supports,
                )
            ]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                "constipation symptoms",
                ("disease-constipation",),
                (("constipation",),),
                intent="disease_clinical_significance",
                target="便秘",
                disease_id="disease-constipation",
                screen_profile="disease_context",
            ),
            EvidenceQuery(
                "constipation bowel habits",
                ("health-management",),
                (("constipation",),),
                intent="health_management",
                target="成人便秘相关排便调护",
                screen_profile="health",
            ),
        ],
    )
    assert len(result.records) == 2
    assert len({item.evidence_key for item in result.records}) == 2
    assert {item.source_evidence_key for item in result.records} == {
        "constipation-source"
    }
    assert {tuple(item.intents) for item in result.records} == {
        ("disease_clinical_significance",),
        ("health_management",),
    }


@pytest.mark.asyncio
async def test_diagnostic_guideline_for_special_population_is_not_generalized():
    class Client:
        async def search(self, query, *, supports):
            return [
                EvidenceRecord(
                    evidence_key="epilepsy-depression",
                    category="guideline",
                    title=(
                        "Recommendations for screening and diagnosis of depression "
                        "in adults with epilepsy"
                    ),
                    source="Society",
                    summary="The consensus covers depression screening in epilepsy.",
                    supports=supports,
                )
            ]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                "depression diagnosis",
                ("disease-depression",),
                (("depression",),),
                intent="diagnostic_guideline",
                target="抑郁症",
            )
        ],
    )
    assert result.records == []
    assert result.query_audits[0].rejected_reasons == {
        "restricted_or_comorbid_context": 1
    }


def test_all_source_taxon_relations_create_queries_without_exact_match():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease

    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
            related_taxa=["Fusobacterium", "Peptococcaceae", "Bacteroides dorei"],
        )
    ]
    queries = make_queries(
        build_abnormal_microbes(parsed), build_source_diseases(parsed, [])
    )
    relations = [
        item for item in queries if item.intent == "microbe_disease_association"
    ]
    assert list(dict.fromkeys(item.relation_source_taxon for item in relations)) == [
        "Fusobacterium",
        "Peptococcaceae",
        "Bacteroides dorei",
    ]
    assert relations[0].matched_microbe_ids == ("taxon-fn",)
    assert all(
        item.matched_microbe_ids == ()
        for item in relations
        if item.relation_source_taxon != "Fusobacterium"
    )


def test_unclear_disease_label_does_not_create_diagnostic_query():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease

    parsed.disease_results = [
        ParsedDisease(disease_id="disease-asd", name="ASD", source_score="74.17")
    ]
    queries = make_queries(
        build_abnormal_microbes(parsed), build_source_diseases(parsed, [])
    )
    assert not any(
        item.intent == "diagnostic_guideline" and "disease-asd" in item.supports
        for item in queries
    )


@pytest.mark.asyncio
async def test_single_evimed_channel_failure_is_audited_as_incomplete():
    class Client:
        async def search_detailed(self, query, *, supports, channels):
            return [
                EviMedChannelResult("guide", "failed", []),
                EviMedChannelResult(
                    "literature",
                    "success",
                    [
                        EvidenceRecord(
                            evidence_key="human",
                            category="literature",
                            title="Human colorectal cancer cohort",
                            source="Gut",
                            summary=(
                                "A human cohort found Fusobacterium nucleatum "
                                "abundance in the gut microbiome associated with colorectal cancer."
                            ),
                            supports=supports,
                        )
                    ],
                ),
            ]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                "query",
                ("taxon-fn",),
                (("Fusobacterium nucleatum",),),
            )
        ],
    )
    assert [item.source_evidence_key for item in result.records] == ["human"]
    assert result.failed_supports == {"taxon-fn"}
    assert [item.status for item in result.query_audits[0].channels] == [
        "failed",
        "success",
    ]


def test_evidence_is_grouped_numbered_and_guideline_tests_require_explicit_text():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease

    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
            related_taxa=["Fusobacterium nucleatum"],
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    records = [
        EvidenceRecord(
            evidence_key="trial",
            category="clinical_trial",
            title="Fusobacterium nucleatum clinical trial in gut disease",
            source="Journal",
            year=2026,
            relevance=1,
            supports=["taxon-fn"],
        ),
        EvidenceRecord(
            evidence_key="guide",
            category="guideline",
            title="炎症性肠病临床指南",
            source="学会",
            year=2025,
            summary="指南建议粪便钙卫蛋白和结肠镜用于活动度评估。",
            relevance=1,
            supports=["disease-ibd"],
            intents=["diagnostic_guideline"],
            interpretation={
                "claim": "该指南说明炎症性肠病的活动度评估路径。",
                "recommendedEvaluations": [
                    {
                        "name": "结肠镜检查",
                        "rationale": "用于活动度评估。",
                        "sourcePhrase": "结肠镜",
                        "purpose": "confirm_target",
                    },
                    {
                        "name": "粪便钙卫蛋白",
                        "rationale": "用于活动度评估。",
                        "sourcePhrase": "粪便钙卫蛋白",
                        "purpose": "characterize_target",
                    },
                ],
            },
        ),
        EvidenceRecord(
            evidence_key="paper",
            category="literature",
            title="Fusobacterium nucleatum and colorectal cancer microbiome",
            source="Gut",
            year=2024,
            summary="Human gut microbiome association with colorectal cancer.",
            relevance=1,
            supports=["taxon-fn"],
        ),
    ]
    assert diseases.items[0].name == "结直肠癌"
    references, numbers = number_evidence(records)
    attach_evidence_refs(microbes, diseases, records, numbers)
    assert [item.category for item in references] == [
        "guideline",
        "literature",
        "clinical_trial",
    ]
    # The bibliography may contain discovery hits internally, but a taxon row
    # without a displayed disease relation must not cite them publicly.
    assert microbes.items[0].evidence_refs == []

    from modules.report.schemas import DiseaseRiskItem

    diseases.items.append(
        DiseaseRiskItem(
            disease_id="disease-ibd",
            name="炎症性肠病",
            source_type="source_report",
        )
    )
    attach_suggested_evaluations(diseases, records, numbers)
    assert [item.name for item in diseases.items[-1].suggested_evaluations] == [
        "结肠镜检查",
    ]


def test_public_bibliography_keeps_only_used_refs_and_renumbers_all_fields():
    microbes = build_abnormal_microbes(parsed_report())
    microbes.items[0].disease_and_risks = [
        RiskAssociation(
            name="结直肠癌",
            description="人群研究记录相关性。",
            evidenceRefs=[2],
        )
    ]
    microbes.items[0].evidence_refs = [2, 3]
    diseases = build_source_diseases(parsed_report(), [])
    diseases.items.append(
        DiseaseRiskItem(
            disease_id="disease-used",
            name="便秘",
            source_type="source_report",
            associationEvidenceRefs=[1],
            evidenceRefs=[1],
        )
    )
    health = HealthManagementSection(
        recommendations=[
            HealthRecommendation(
                category="diet",
                content="保持食物多样性。",
                evidenceRefs=[4],
            )
        ]
    )
    references = [
        EvidenceReference(
            number=number,
            category="literature",
            title=f"evidence-{number}",
            source="Journal",
        )
        for number in range(1, 6)
    ]
    retained = retain_referenced_evidence(
        references,
        microbes=microbes,
        diseases=diseases,
        health=health,
    )
    assert [item.title for item in retained] == [
        "evidence-1",
        "evidence-2",
        "evidence-3",
        "evidence-4",
    ]
    assert [item.number for item in retained] == [1, 2, 3, 4]
    assert microbes.items[0].disease_and_risks[0].evidence_refs == [2]
    assert microbes.items[0].evidence_refs == [2, 3]
    assert diseases.items[-1].evidence_refs == [1]
    assert health.recommendations[0].evidence_refs == [4]


def test_existing_hospital_result_keeps_its_role_and_other_guideline_checks():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease

    parsed.disease_results = [
        ParsedDisease(disease_id="disease-crc", name="结直肠癌", source_score="80")
    ]
    hospital = [
        HospitalDataInput(
            data_type="EXAM",
            item_name="无痛肠镜检查",
            exam_result="所见结直肠黏膜大致正常\r术后注意事项：持续腹痛及时就医。",
            check_time="2025-12-18 08:16:50",
        )
    ]
    diseases = build_source_diseases(parsed, hospital)
    assert diseases.items[0].existing_results[0].item_name == "无痛肠镜检查"
    assert diseases.items[0].existing_results[0].result == "所见结直肠黏膜大致正常"
    assert not any(
        query.intent == "diagnostic_guideline"
        for query in make_queries(build_abnormal_microbes(parsed), diseases, age=28)
    )

    record = EvidenceRecord(
        evidence_key="crc-guide",
        category="guideline",
        title="结直肠癌筛查指南",
        source="学会",
        summary=(
            "指南建议使用粪便免疫化学试验进行结直肠癌筛查，"
            "并使用结肠镜检查辅助结直肠癌诊断。"
        ),
        supports=["disease-crc"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "该指南说明结直肠癌筛查。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "结肠镜检查",
                    "rationale": "用于辅助结直肠癌诊断。",
                    "sourcePhrase": "结肠镜检查用于辅助结直肠癌诊断",
                    "purpose": "confirm_target",
                },
                {
                    "name": "粪便隐血试验（FIT）",
                    "rationale": "用于结直肠癌筛查。",
                    "sourcePhrase": "粪便隐血试验用于结直肠癌筛查",
                    "purpose": "screen_target",
                },
            ],
        },
    )
    attach_suggested_evaluations(
        diseases,
        [record],
        {"crc-guide": 1},
    )
    evaluations = diseases.items[0].suggested_evaluations
    assert [item.name for item in evaluations] == [
        "粪便隐血试验（FIT）",
    ]
    assert evaluations[0].already_available is False
    assert diseases.items[0].confirmation_evidence_refs == [1]


@pytest.mark.parametrize(
    ("disease_name", "item_name"),
    (
        ("高血脂", "低密度脂蛋白(LDL-c)"),
        ("高尿酸血症", "血尿酸"),
        ("肾脏疾病", "血肌酐"),
        ("心脑血管疾病", "常规心电图([心脏])"),
        ("肥胖症", "体脂率"),
        ("非酒精性脂肪肝", "天门冬氨酸氨基转移酶(AST)"),
    ),
)
def test_source_disease_synonyms_keep_relevant_existing_results(
    disease_name, item_name
):
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id=f"disease-{stable_id('clinical-match', disease_name)}",
            name=disease_name,
        )
    ]
    data_type = "EXAM" if "心电图" in item_name else "INSPECTION"
    hospital = [
        HospitalDataInput(
            dataType=data_type,
            itemName=item_name,
            examResult="窦性心律 正常范围心电图" if data_type == "EXAM" else None,
            testResultValue=None if data_type == "EXAM" else "1.0",
            resultStatusName="正常",
        )
    ]

    diseases = build_source_diseases(parsed, hospital)

    assert [item.item_name for item in diseases.items[0].existing_results] == [
        item_name
    ]


def test_gastric_report_with_attached_colonoscopy_name_is_not_colorectal_confirmation():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(disease_id="disease-crc", name="结直肠癌", source_score="80")
    ]
    hospital = [
        HospitalDataInput(
            data_type="EXAM",
            item_name="无痛胃镜检查(附带肠镜)(胃)",
            exam_result="慢性非萎缩性胃炎",
        ),
        HospitalDataInput(
            data_type="EXAM",
            item_name="(附加)无痛肠镜检查(肠)",
            exam_result="所见结直肠黏膜大致正常",
        ),
    ]

    diseases = build_source_diseases(parsed, hospital)
    assert [item.item_name for item in diseases.items[0].existing_results] == [
        "(附加)无痛肠镜检查(肠)"
    ]


def test_long_query_ids_are_readable_and_collision_safe():
    first = stable_id(
        "query",
        "microbe-disease-association|Fusobacterium nucleatum|colorectal cancer",
    )
    second = stable_id(
        "query",
        "microbe-disease-association|Fusobacterium nucleatum|colorectal adenoma",
    )
    assert first != second
    assert first.rsplit("-", 1)[-1] != second.rsplit("-", 1)[-1]


def test_latin_taxon_requires_whole_word_not_substring_of_candidate():
    text = "consensus statement on candidate biomarkers".casefold()
    normalized = "".join(character for character in text if character.isalnum())
    assert _concept_match_score(text, normalized, "Candida") == 0
    assert (
        _concept_match_score(
            "human stool Candida cohort", "humanstoolcandidacohort", "Candida"
        )
        == 1
    )


def test_required_concept_rejects_explicitly_negated_target():
    english = "human stool study of non-Aspergillus fungi".casefold()
    chinese = "人群粪便研究纳入非曲霉属真菌"
    assert (
        _concept_match_score(
            english, "humanstoolstudyofnonaspergillusfungi", "Aspergillus"
        )
        == 0
    )
    assert _concept_match_score(chinese, "人群粪便研究纳入非曲霉属真菌", "曲霉属") == 0
    assert (
        _concept_match_score(
            "Fusobacterium-nucleatum cohort",
            "fusobacteriumnucleatumcohort",
            "Fusobacterium nucleatum",
        )
        == 1
    )


def test_scientific_abbreviation_is_accepted_only_as_a_whole_taxon_phrase():
    record = EvidenceRecord(
        evidence_key="abbreviated-taxon",
        category="literature",
        title="C. albicans and vulvovaginal candidiasis",
        source="Journal",
        summary="A human clinical cohort studied C. albicans infection.",
    )
    query = EvidenceQuery(
        "Candida albicans vulvovaginal candidiasis",
        ("taxon-candida-albicans",),
        (
            ("Candida albicans", "C. albicans", "C albicans"),
            ("vulvovaginal candidiasis",),
        ),
        intent="microbe_disease_association",
        screen_profile="pathogen_relation",
        target="Candida albicans / 外阴阴道念珠菌病",
    )
    assert _screen(record, query=query)[0] is True
    unrelated = record.model_copy(
        update={
            "title": "Candidacy and vulvovaginal candidiasis",
            "summary": "A human clinical cohort studied infection.",
        }
    )
    assert _screen(unrelated, query=query)[0] is False


@pytest.mark.asyncio
async def test_required_concept_does_not_match_asd_inside_was_detected():
    record = EvidenceRecord(
        evidence_key="uc-not-asd",
        category="literature",
        title="Bacteroides fragilis was detected in ulcerative colitis",
        source="Journal",
        summary=(
            "A human stool gut microbiome cohort reported an association with "
            "ulcerative colitis."
        ),
    )

    class Client:
        async def search_detailed(self, query, *, supports, channels):
            return [EviMedChannelResult("literature", "success", [record])]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                query="Bacteroides fragilis ASD",
                supports=("taxon-bf", "disease-asd"),
                required_concepts=(("Bacteroides fragilis",), ("ASD",)),
                intent="microbe_disease_association",
                target="Bacteroides fragilis / ASD",
                channels=("literature",),
                microbe_id="taxon-bf",
                disease_id="disease-asd",
                relation_id="relation-asd",
                relation_disease_name="ASD",
                screen_profile="gut_association",
            )
        ],
    )
    assert result.records == []
    assert result.query_audits[0].rejected_reasons == {"missing_required_concept": 1}


@pytest.mark.asyncio
async def test_ai_candidate_does_not_extract_asd_from_was_detected():
    microbes = build_abnormal_microbes(parsed_report())
    record = EvidenceRecord(
        evidence_key="candidate-not-asd",
        category="literature",
        title="Fusobacterium nucleatum was detected in ulcerative colitis",
        source="Journal",
        summary="A human stool cohort reported an ulcerative colitis association.",
        microbe_ids=[microbes.items[0].microbe_id],
        supports=[microbes.items[0].microbe_id],
        intents=["microbe_discovery"],
        targets=[microbes.items[0].scientific_name],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": (
                            "Fusobacterium nucleatum was reported in an ulcerative "
                            "colitis cohort."
                        ),
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "sampleAlignment": "stool",
                        "patientApplicability": "direct",
                        "conditions": [
                            {
                                "name": "ASD",
                                "searchTerm": "ASD",
                                "sourcePhrase": "was detected",
                                "relationType": "epidemiologic_association",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={record.evidence_key: 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.conditions == []
    candidates = make_candidate_queries(microbes, [record], age=28)
    assert len(candidates) == 1
    assert candidates[0].relation_disease_name == "溃疡性结肠炎"
    assert "ASD" not in candidates[0].query


@pytest.mark.asyncio
async def test_product_use_is_not_extracted_as_microbe_disease_risk():
    record = EvidenceRecord(
        evidence_key="product-use",
        category="guideline",
        title="活菌制剂在消化系统疾病中的临床应用",
        source="学会",
        summary="该活菌制剂含有Bacillus cereus，用于调节微生态并改善消化系统疾病症状。",
        microbe_ids=["taxon-bacillus-cereus"],
        intents=["microbe_discovery"],
        targets=["Bacillus cereus"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "含有Bacillus cereus的活菌制剂用于改善消化系统疾病症状。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "消化系统疾病",
                                "searchTerm": "消化系统疾病",
                                "sourcePhrase": "消化系统疾病",
                                "relationType": "established_pathogen",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"product-use": 1})
    assert record.interpretation is not None
    assert record.interpretation.conditions == []


@pytest.mark.asyncio
async def test_diagnosis_and_treatment_guideline_keeps_exact_microbe_disease():
    record = EvidenceRecord(
        evidence_key="botulism-guide",
        category="guideline",
        title="肉毒中毒诊断和治疗指南",
        source="CDC",
        summary=("肉毒中毒是由肉毒梭菌产生的神经毒素介导的危及生命的疾病。"),
        microbe_ids=["taxon-botulinum"],
        intents=["microbe_clinical_significance"],
        targets=["Clostridium botulinum / 肉毒梭菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "肉毒梭菌可介导肉毒中毒。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "肉毒中毒",
                                "searchTerm": "肉毒中毒",
                                "sourcePhrase": "肉毒中毒",
                                "relationType": "established_pathogen",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"botulism-guide": 1})
    assert record.interpretation is not None
    assert [item.name for item in record.interpretation.conditions] == ["肉毒中毒"]


@pytest.mark.asyncio
async def test_treatment_failure_population_is_not_extracted_as_patient_disease_risk():
    record = EvidenceRecord(
        evidence_key="fmt-failure",
        category="literature",
        title=(
            "Bacteroides fragilis strain associates with fecal microbial "
            "transplant failure in ulcerative colitis patients"
        ),
        source="Journal",
        summary="The donor-derived strain was associated with FMT failure.",
        microbe_ids=["taxon-bf"],
        intents=["microbe_discovery"],
        targets=["Bacteroides fragilis / 脆弱拟杆菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "脆弱拟杆菌菌株与溃疡性结肠炎患者粪菌移植失败相关。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "strain",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "溃疡性结肠炎",
                                "searchTerm": "ulcerative colitis",
                                "sourcePhrase": "ulcerative colitis",
                                "relationType": "epidemiologic_association",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"fmt-failure": 1})
    assert record.interpretation is not None
    assert record.interpretation.conditions == []


@pytest.mark.asyncio
async def test_coinfection_guideline_does_not_turn_second_pathogen_into_first_pathogen_disease():
    record = EvidenceRecord(
        evidence_key="coinfection",
        category="guideline",
        title="结核分枝杆菌合并乙型肝炎病毒感染诊治专家共识",
        source="学会",
        summary="该共识讨论两种感染合并存在时的诊治。",
        microbe_ids=["taxon-mtb"],
        intents=["microbe_discovery"],
        targets=["Mycobacterium tuberculosis / 结核分枝杆菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "结核分枝杆菌与乙型肝炎合并感染。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "乙型肝炎",
                                "searchTerm": "乙型肝炎",
                                "sourcePhrase": "乙型肝炎病毒感染",
                                "relationType": "established_pathogen",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"coinfection": 1})
    assert record.interpretation is not None
    assert record.interpretation.conditions == []


@pytest.mark.asyncio
async def test_species_result_cannot_inherit_resistant_hypervirulent_subtype_risk():
    record = EvidenceRecord(
        evidence_key="subtype-risk",
        category="literature",
        title=("Hypervirulent carbapenem-resistant Klebsiella pneumoniae infection"),
        source="Journal",
        summary="Adult patients had CRKP infection.",
        microbe_ids=["taxon-kp"],
        intents=["microbe_discovery"],
        targets=["Klebsiella pneumoniae / 肺炎克雷伯菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "高毒力耐碳青霉烯肺炎克雷伯菌与CRKP感染相关。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "direct",
                        "conditions": [
                            {
                                "name": "CRKP感染",
                                "searchTerm": "CRKP infection",
                                "sourcePhrase": "CRKP infection",
                                "relationType": "established_pathogen",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(Client(), records=[record], numbers={"subtype-risk": 1})
    assert record.interpretation is not None
    assert record.interpretation.taxon_alignment == "strain"
    assert record.interpretation.patient_applicability == "contextual"


@pytest.mark.asyncio
async def test_species_result_cannot_inherit_cdtb_carrying_subtype_risk():
    record = EvidenceRecord(
        evidence_key="cdtb-subtype-risk",
        category="literature",
        title="Carriage of CdtB Encoding Salmonella enterica in Gastroenteritis",
        source="Journal",
        summary=(
            "Carriage of CdtB encoding Salmonella enterica was characterized "
            "among patients with gastroenteritis."
        ),
        microbe_ids=["taxon-salmonella"],
        intents=["microbe_disease_association"],
        targets=["Salmonella enterica / 肠沙门氏菌 / 胃肠炎"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "携带CdtB的肠沙门氏菌与胃肠炎相关。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"cdtb-subtype-risk": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.taxon_alignment == "toxin_gene"
    assert record.interpretation.patient_applicability == "contextual"


@pytest.mark.asyncio
async def test_exact_species_disease_source_is_direct_even_when_model_confuses_population_fit():
    record = EvidenceRecord(
        evidence_key="salmonella-general-relation",
        category="literature",
        title="Genetic diversity of Salmonella enterica during acute human infections",
        source="Journal",
        summary=(
            "Salmonella enterica is the causative agent of both enteric fever "
            "and gastroenteritis in humans. Acute salmonellosis was studied "
            "in patients with human infection."
        ),
        microbe_ids=["taxon-salmonella"],
        disease_ids=["disease-salmonellosis"],
        intents=["microbe_disease_association"],
        targets=["Salmonella enterica / 肠沙门氏菌 / 沙门菌病"],
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-salmonellosis",
                microbe_id="taxon-salmonella",
                microbe_name="Salmonella enterica",
                microbe_aliases=["肠沙门氏菌"],
                disease_id="disease-salmonellosis",
                disease_name="沙门菌病",
                relation_type="established_pathogen",
            )
        ],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "肠沙门氏菌是肠热病和胃肠炎的病原体。",
                        "targetAlignment": "indirect",
                        "taxonAlignment": "species",
                        "sampleAlignment": "stool",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"salmonella-general-relation": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    validate_relation_bindings([record])
    assert len(record.relation_bindings) == 1
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门氏菌",
            disease_name="沙门菌病",
            relation_type="established_pathogen",
        )
        == "肠沙门氏菌可引起沙门菌病。"
    )


def test_one_multicondition_pathogen_claim_is_rendered_per_risk():
    record = EvidenceRecord(
        evidenceKey="salmonella-three-conditions",
        category="literature",
        title="Salmonella enterica human infection",
        source="Journal",
        microbeIds=["taxon-salmonella"],
        diseaseIds=["disease-salmonellosis", "disease-enteric-fever", "disease-ge"],
        targets=["Salmonella enterica / 肠沙门氏菌"],
        interpretation={
            "claim": "肠沙门氏菌是肠热病和胃肠炎的病原体。",
            "targetAlignment": "direct",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId=f"relation-{index}",
                microbeId="taxon-salmonella",
                microbeName="Salmonella enterica",
                microbeAliases=["肠沙门氏菌"],
                diseaseId=disease_id,
                diseaseName=disease_name,
                relationType="established_pathogen",
            )
            for index, (disease_id, disease_name) in enumerate(
                (
                    ("disease-salmonellosis", "沙门菌病"),
                    ("disease-enteric-fever", "肠热病"),
                    ("disease-ge", "胃肠炎"),
                )
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门氏菌",
            disease_name="肠热病",
            relation_type="established_pathogen",
        )
        == "肠沙门氏菌是肠热病的病原体。"
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门氏菌",
            disease_name="胃肠炎",
            relation_type="established_pathogen",
        )
        == "肠沙门氏菌是胃肠炎的病原体。"
    )


@pytest.mark.asyncio
async def test_serotype_relation_is_not_promoted_to_species_level():
    record = EvidenceRecord(
        evidence_key="salmonella-serotype",
        category="literature",
        title="Salmonella enterica serotype Enteritidis caused gastroenteritis",
        source="Journal",
        summary="This serotype caused a human gastroenteritis outbreak.",
        microbe_ids=["taxon-salmonella"],
        disease_ids=["disease-gastroenteritis"],
        intents=["microbe_disease_association"],
        targets=["Salmonella enterica / 肠沙门氏菌 / 胃肠炎"],
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-serotype",
                microbe_id="taxon-salmonella",
                microbe_name="Salmonella enterica",
                microbe_aliases=["肠沙门氏菌"],
                disease_id="disease-gastroenteritis",
                disease_name="胃肠炎",
                relation_type="established_pathogen",
            )
        ],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "肠沙门氏菌肠炎血清型引起胃肠炎暴发。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "sampleAlignment": "stool",
                        "patientApplicability": "contextual",
                    }
                ]
            }

    await interpret_evidence(
        Client(), records=[record], numbers={"salmonella-serotype": 1}
    )
    assert record.interpretation is not None
    assert record.interpretation.taxon_alignment == "strain"


@pytest.mark.asyncio
async def test_organism_label_is_not_accepted_as_its_own_disease_condition():
    record = EvidenceRecord(
        evidence_key="organism-not-condition",
        category="literature",
        title="Candida albicans in clinical samples",
        source="Journal",
        summary="Candida albicans was detected in human clinical samples.",
        microbe_ids=["taxon-ca"],
        intents=["microbe_discovery"],
        targets=["Candida albicans / 白念珠菌"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": "白念珠菌可在人体临床样本中检出。",
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "白念珠菌",
                                "searchTerm": "Candida albicans",
                                "sourcePhrase": "Candida albicans",
                                "relationType": "established_pathogen",
                            }
                        ],
                    }
                ]
            }

    await interpret_evidence(
        Client(),
        records=[record],
        numbers={"organism-not-condition": 1},
    )
    assert record.interpretation is not None
    assert record.interpretation.conditions == []


def test_pathogen_queries_start_exact_and_only_broaden_when_empty():
    queries = make_queries(
        build_abnormal_microbes(parsed_report()),
        build_source_diseases(parsed_report(), []),
        age=28,
    )
    discovery = [item for item in queries if item.intent == "microbe_discovery"]
    assert {item.query for item in discovery} == {"Fusobacterium nucleatum"}
    assert not any(item.intent == "microbe_clinical_significance" for item in queries)
    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed_report()),
        build_source_diseases(parsed_report(), []),
        [],
        age=28,
    )
    assert {
        item.query
        for item in coverage
        if item.intent == "microbe_clinical_significance"
    } == {
        "Fusobacterium nucleatum infection disease",
        "Fusobacterium nucleatum clinical infection",
        "Fusobacterium nucleatum pathogen",
        "F. nucleatum infection disease",
        "F nucleatum infection disease",
        "具核梭杆菌 感染 疾病",
    }
    assert not any("human clinical" in item.query for item in coverage)
    assert not any("human gut microbiome" in item.query for item in coverage)
    assert len({item.query_id for item in queries}) == len(queries)
    health_queries = {
        item.query for item in queries if item.intent == "health_management"
    }
    intervention_queries = {
        item.query for item in queries if item.intent == "intervention"
    }
    assert "bowel habits adults guideline" in health_queries
    assert "bowel movement frequency adults" in intervention_queries
    assert not any("gut microbiome" in query for query in health_queries)


def test_primary_queries_round_robin_microbe_disease_and_health_modules():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease, ParsedTaxon

    parsed.taxa.append(
        ParsedTaxon(
            taxonId="taxon-salmonella",
            category="pathogenic",
            latinName="Salmonella enterica",
            value="0.01",
            explicitlyAbnormal=True,
        )
    )
    parsed.disease_results = [
        ParsedDisease(
            diseaseId="disease-crc",
            name="结直肠癌",
            sourceScore="1.2",
        )
    ]

    queries = make_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        age=28,
    )

    assert [item.intent for item in queries[:3]] == [
        "microbe_discovery",
        "disease_clinical_significance",
        "health_management",
    ]
    first_health_index = next(
        index
        for index, item in enumerate(queries)
        if item.intent in {"health_management", "intervention"}
    )
    last_microbe_index = max(
        index
        for index, item in enumerate(queries)
        if item.intent == "microbe_discovery"
    )
    assert first_health_index < last_microbe_index


def test_abnormal_lipids_replace_generic_diet_and_exercise_queries_without_growth():
    parsed = parsed_report()
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    baseline = make_queries(microbes, diseases, age=28)
    queries = make_queries(
        microbes,
        diseases,
        age=28,
        hospital_data=[
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白(LDL-c)",
                resultStatus=1,
                resultStatusName="偏高",
                testResultValue="3.70",
            )
        ],
    )

    baseline_health = [
        item for item in baseline if item.intent in {"health_management", "intervention"}
    ]
    health = [
        item for item in queries if item.intent in {"health_management", "intervention"}
    ]
    assert len(health) == len(baseline_health) == 8
    targets = {item.target for item in health}
    assert "血脂异常相关膳食调护" in targets
    assert "血脂异常相关运动调护" in targets
    assert "健康成人膳食调护" not in targets
    assert "健康成人运动调护" not in targets
    assert any(
        "dyslipidemia" in alias
        for item in health
        if item.target == "血脂异常相关膳食调护"
        for concept in item.required_concepts
        for alias in concept
    )


def test_normal_lipid_result_does_not_create_condition_specific_health_query():
    parsed = parsed_report()
    queries = make_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        age=28,
        hospital_data=[
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="低密度脂蛋白(LDL-c)",
                resultStatus=0,
                resultStatusName="正常",
                testResultValue="2.10",
            )
        ],
    )

    targets = {
        item.target
        for item in queries
        if item.intent in {"health_management", "intervention"}
    }
    assert "健康成人膳食调护" in targets
    assert "健康成人运动调护" in targets
    assert not any(target.startswith("血脂异常相关") for target in targets)


def test_lipid_health_coverage_retries_only_missing_condition_specific_topic():
    parsed = parsed_report()
    diet_guide = EvidenceRecord(
        evidenceKey="lipid-diet-guide",
        category="guideline",
        title="成人血脂异常膳食指南",
        source="Society",
        intents=["health_management"],
        targets=["血脂异常相关膳食调护"],
        interpretation={
            "claim": "指南支持血脂异常的膳食调护。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
        },
    )
    queries = make_coverage_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        [diet_guide],
        age=57,
        hospital_data=[
            HospitalDataInput(
                dataType="INSPECTION",
                itemName="总胆固醇（TC）",
                resultStatus=1,
                resultStatusName="偏高",
            )
        ],
    )
    health_targets = {
        item.target for item in queries if item.intent == "health_management"
    }
    assert "血脂异常相关膳食调护" not in health_targets
    assert "血脂异常相关运动调护" in health_targets


def test_strain_or_toxin_discovery_does_not_create_species_disease_query():
    microbes = build_abnormal_microbes(parsed_report())
    record = EvidenceRecord(
        evidence_key="strain-only",
        category="literature",
        title="Toxigenic Fusobacterium nucleatum disease report",
        source="Journal",
        microbe_ids=["taxon-fn"],
        intents=["microbe_discovery"],
        interpretation={
            "claim": "Fusobacterium nucleatum toxin strain was reported.",
            "targetAlignment": "direct",
            "taxonAlignment": "toxin_gene",
            "conditions": [
                {
                    "name": "毒素相关疾病",
                    "searchTerm": "toxin disease",
                    "sourcePhrase": "toxin disease",
                    "relationType": "established_pathogen",
                }
            ],
        },
    )
    assert make_candidate_queries(microbes, [record], age=28) == []


def test_explicit_source_outcomes_seed_exact_queries_but_not_public_bindings():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-perfringens",
            category="pathogenic",
            latin_name="Clostridium perfringens",
            value="0.2",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    record = EvidenceRecord(
        evidence_key="perfringens-strain-discovery",
        category="literature",
        title="Virulence genes of Clostridium perfringens",
        source="Journal",
        summary=(
            "Clostridium perfringens causes human gas gangrene, food "
            "poisoning and clostridial myonecrosis."
        ),
        microbe_ids=["taxon-perfringens"],
        intents=["microbe_discovery"],
        targets=["Clostridium perfringens / 产气荚膜梭菌"],
        interpretation={
            "claim": "该研究分析产气荚膜梭菌毒力基因。",
            "targetAlignment": "direct",
            "taxonAlignment": "toxin_gene",
            "patientApplicability": "not_applicable",
        },
    )

    queries = make_candidate_queries(microbes, [record], age=28)

    assert {query.relation_disease_name for query in queries} == {
        "食物中毒",
        "气性坏疽",
        "梭菌性肌坏死",
    }
    assert all(query.intent == "microbe_disease_association" for query in queries)
    assert record.relation_bindings == []
    assert "microbe_disease_association" not in record.intents


def test_source_outcome_without_explicit_requested_taxon_is_not_a_query_seed():
    microbes = build_abnormal_microbes(parsed_report())
    record = EvidenceRecord(
        evidence_key="foreign-taxon-outcome",
        category="literature",
        title="Clostridium perfringens and food poisoning",
        source="Journal",
        summary="Clostridium perfringens causes food poisoning.",
        microbe_ids=["taxon-fn"],
        intents=["microbe_discovery"],
        targets=["Fusobacterium nucleatum / 具核梭杆菌"],
    )
    assert make_candidate_queries(microbes, [record], age=28) == []


def test_chinese_animal_evidence_is_preclinical():
    record = EvidenceRecord(
        evidence_key="animal-model",
        category="literature",
        title="脆弱拟杆菌与骨代谢研究",
        source="Journal",
        summary="该结论来自高脂饮食大鼠模型。",
        interpretation={
            "claim": "脆弱拟杆菌在大鼠模型中与骨代谢变化相关。",
            "clinicalRole": "动物实验中的菌群关联。",
        },
    )
    assert is_preclinical_evidence(record) is True


@pytest.mark.parametrize(
    "summary",
    [
        "Pseudomonas fluorescens induced defensin secretion in intestinal epithelial cells.",
        "Drechmeria coniospora is an endoparasite of a nematode host.",
        "该研究为肠上皮细胞实验。",
        "该菌在线虫宿主中形成感染。",
    ],
)
def test_cell_and_nematode_records_are_preclinical(summary):
    record = EvidenceRecord(
        evidence_key=summary,
        category="literature",
        title="Mechanistic study",
        source="Journal",
        summary=summary,
    )
    assert is_preclinical_evidence(record) is True


def test_pathogen_review_is_not_rejected_for_a_later_cell_mechanism():
    query = EvidenceQuery(
        query="Campylobacter coli diarrhoeal disease",
        supports=("taxon-campy",),
        required_concepts=(
            ("Campylobacter coli", "C. coli"),
            ("diarrheal disease", "diarrhoeal disease"),
        ),
        intent="microbe_disease_association",
        target="Campylobacter coli / 感染性腹泻",
        relation_disease_id="disease-infectious-diarrhea",
        relation_disease_name="感染性腹泻",
        relation_source_taxon="Campylobacter coli",
        matched_microbe_ids=("taxon-campy",),
        microbe_id="taxon-campy",
        disease_id="disease-infectious-diarrhea",
        relation_id="relation-campy-diarrhea",
        relation_type="established_pathogen",
        screen_profile="human_relation",
    )
    record = EvidenceRecord(
        evidence_key="campylobacter-review",
        category="literature",
        title="Campylobacter flagella: not just for motility",
        source="Journal",
        summary=(
            "Campylobacter jejuni and Campylobacter coli are among the major "
            "causes of diarrheal disease worldwide. Later sections describe "
            "interactions with intestinal epithelial cells."
        ),
        study_types=["Journal Article", "Review"],
    )

    assert _screen(record, query=query)[0] is True
    record.relation_bindings = [
        EvidenceRelationBinding(
            relation_id="relation-campy-diarrhea",
            microbe_id="taxon-campy",
            microbe_name="Campylobacter coli",
            disease_id="disease-infectious-diarrhea",
            disease_name="感染性腹泻",
            relation_type="established_pathogen",
        )
    ]
    assert is_preclinical_evidence(record) is False


def test_in_silico_or_in_vitro_microbe_result_is_not_clinical_evidence():
    for key, title in (
        (
            "in-silico",
            "In-silico drug target identification for Shigella dysenteriae",
        ),
        (
            "in-vitro",
            "In vitro cytotoxic diterpenoids from Colletotrichum higginsianum",
        ),
    ):
        record = EvidenceRecord(
            evidence_key=key,
            category="literature",
            title=title,
            source="Journal",
            interpretation={
                "claim": "该研究报告了非人体临床实验结果。",
                "targetAlignment": "direct",
                "taxonAlignment": "species",
                "patientApplicability": "contextual",
            },
        )
        assert is_preclinical_evidence(record) is True


def test_depression_rejects_eeg_even_when_rationale_mentions_depression():
    disease = DiseaseRiskItem(
        disease_id="disease-depression",
        name="抑郁症",
        source_type="source_report",
    )
    assert not evaluation_is_public_for_disease(
        "脑电图检查",
        rationale="用于评估抑郁症患者的脑电活动。",
        disease=disease,
    )
    assert not evaluation_is_public_for_disease(
        "PHQ-9抑郁量表",
        rationale="用于评估抑郁症状严重程度。",
        disease=disease,
    )


def test_constipation_colonoscopy_and_psc_ercp_are_not_routine_screening_paths():
    constipation = DiseaseRiskItem(
        disease_id="disease-constipation",
        name="便秘",
        source_type="source_report",
    )
    assert not evaluation_is_public_for_disease(
        "结肠镜检查",
        rationale="用于筛查便秘。",
        disease=constipation,
    )
    psc = DiseaseRiskItem(
        disease_id="disease-psc",
        name="硬化性胆管炎",
        source_type="source_report",
    )
    assert not evaluation_is_public_for_disease(
        "内镜逆行胰胆管造影（ERCP）",
        rationale="用于筛查硬化性胆管炎。",
        disease=psc,
    )
    assert not evaluation_is_public_for_disease(
        "内镜下逆行胰胆管造影",
        rationale="用于评估硬化性胆管炎。",
        disease=psc,
    )
    assert evaluation_is_public_for_disease(
        "磁共振胰胆管成像（MRCP）",
        rationale="用于评估胆管形态。",
        disease=psc,
    )


@pytest.mark.parametrize(
    ("disease_name", "evaluation_name", "rationale"),
    [
        ("克罗恩病", "腹部CT", "用于评估腹部脂肪定量。"),
        ("肝硬化", "内镜", "用于评估食管胃底静脉曲张。"),
        ("高尿酸血症", "彩色多普勒超声", "用于评估关节和颈动脉病变。"),
        ("精神分裂症", "实时荧光定量PCR", "用于验证circRNAs差异表达。"),
        ("高血压", "超声心动图", "用于评估心脏靶器官损害。"),
    ],
)
def test_complication_or_research_evaluation_is_not_target_confirmation(
    disease_name: str,
    evaluation_name: str,
    rationale: str,
):
    disease = DiseaseRiskItem(
        disease_id=f"disease-{disease_name}",
        name=disease_name,
        source_type="source_report",
    )
    assert not evaluation_is_public_for_disease(
        evaluation_name,
        rationale=rationale,
        disease=disease,
    )


def test_alzheimer_research_reference_standard_is_not_patient_check_advice():
    disease = DiseaseRiskItem(
        disease_id="disease-alzheimer",
        name="阿尔茨海默病",
        source_type="source_report",
    )
    assert not evaluation_is_public_for_disease(
        "11C-匹兹堡化合物B正电子发射断层CT显像",
        rationale="用于阿尔茨海默病模型验证的参考标准。",
        disease=disease,
    )
    assert not evaluation_is_public_for_disease(
        "脑部MRI检查",
        rationale="仅作为研究分组和模型验证的参考标准。",
        disease=disease,
    )
    assert not evaluation_is_public_for_disease(
        "认知量表评分",
        rationale="用于协助分组，是研究设计中确认阿尔茨海默病的工具。",
        disease=disease,
    )
    assert not evaluation_is_public_for_disease(
        "MoCA认知量表",
        rationale="用于评估阿尔茨海默病相关的认知功能。",
        disease=disease,
    )


@pytest.mark.parametrize(
    ("disease_name", "evaluation_name"),
    [
        ("非酒精性脂肪肝", "肝脏瞬时弹性成像"),
        ("高尿酸血症", "血尿酸测定"),
        ("类风湿性关节炎", "抗CCP抗体检测"),
        ("结直肠癌", "结肠镜检查"),
        ("高血压", "24小时动态血压监测"),
    ],
)
def test_target_specific_standard_evaluation_candidate_can_pass(
    disease_name: str,
    evaluation_name: str,
):
    disease = DiseaseRiskItem(
        disease_id=f"disease-{disease_name}",
        name=disease_name,
        source_type="source_report",
    )
    assert evaluation_is_public_for_disease(
        evaluation_name,
        rationale=f"用于评估{disease_name}。",
        disease=disease,
    )


def test_pig_serum_evidence_is_preclinical_but_human_zoonosis_guideline_is_not():
    pig_record = EvidenceRecord(
        evidence_key="pig-serum",
        category="literature",
        title="Streptococcus suis immunoassay",
        source="Journal",
        summary="The assay was validated using pig serum samples.",
        interpretation={
            "claim": "The assay detects Streptococcus suis antibodies in pig serum.",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )
    human_record = EvidenceRecord(
        evidence_key="human-zoonosis",
        category="guideline",
        title="人感染猪链球菌病诊疗方案",
        source="学会",
        summary="人感染猪链球菌病可经生猪相关职业暴露传播。",
        interpretation={
            "claim": "Streptococcus suis是人畜共患病病原体。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "direct",
        },
    )
    assert is_preclinical_evidence(pig_record) is True
    assert is_preclinical_evidence(human_record) is False

    baboon_record = EvidenceRecord(
        evidence_key="baboon-gut",
        category="literature",
        title="Gut mycobiome of wild baboons",
        source="Journal",
        summary="Fecal samples from wild baboons contained Ogataea.",
        interpretation={
            "claim": "Ogataea was detected in wild baboon feces.",
            "targetAlignment": "direct",
            "taxonAlignment": "genus",
            "patientApplicability": "contextual",
        },
    )
    assert is_preclinical_evidence(baboon_record) is True

    for title in (
        "Treponema species from necrotic skin lesions of pigs",
        "Human colorectal adenoma organoid polysaccharide effect",
    ):
        assert is_preclinical_evidence(
            EvidenceRecord(
                evidence_key=title,
                category="literature",
                title=title,
                source="Journal",
            )
        )


def test_tcm_intervention_guideline_is_not_general_health_evidence():
    record = EvidenceRecord(
        evidence_key="tcm-constipation",
        category="guideline",
        title="功能性便秘中医治未病干预指南",
        source="Society",
        summary="指南讨论排便习惯和中医干预。",
    )
    query = EvidenceQuery(
        "健康成人排便习惯调护",
        ("health-bowel",),
        (("排便",),),
        intent="health_management",
        target="健康成人排便习惯调护",
        screen_profile="health",
    )
    assert _screen(record, query=query) == (False, "product_specific_intervention")


def test_laboratory_method_guideline_is_not_microbe_medical_discovery():
    record = EvidenceRecord(
        evidence_key="ast-method",
        category="guideline",
        title="Guideline for antimicrobial susceptibility testing of Campylobacter coli",
        source="CLSI",
        summary="The document provides standard laboratory testing methods.",
    )
    query = EvidenceQuery(
        "Campylobacter coli",
        ("taxon-campylobacter-coli",),
        (("Campylobacter coli",),),
        intent="microbe_discovery",
        screen_profile="discovery",
        target="Campylobacter coli",
    )
    assert _screen(record, query=query) == (False, "laboratory_method_only")


def test_laboratory_identification_paper_is_not_microbe_medical_discovery():
    record = EvidenceRecord(
        evidence_key="candida-identification",
        category="literature",
        title="Differentiation of Candida albicans by FT-IR and chemometrics",
        source="Journal",
        summary="Clinical isolates were identified by a laboratory method.",
    )
    query = EvidenceQuery(
        "Candida albicans",
        ("taxon-ca",),
        (("Candida albicans",),),
        intent="microbe_discovery",
        screen_profile="discovery",
        target="Candida albicans",
    )
    assert _screen(record, query=query) == (False, "laboratory_method_only")


def test_treatment_only_paper_is_not_disease_explanation_evidence():
    record = EvidenceRecord(
        evidence_key="ckd-treatment",
        category="guideline",
        title="Treatment and management of chronic kidney disease",
        source="Society",
        summary="Clinical guidance for chronic kidney disease treatment.",
    )
    query = EvidenceQuery(
        "chronic kidney disease",
        ("disease-ckd",),
        (("chronic kidney disease",),),
        intent="disease_clinical_significance",
        screen_profile="disease_context",
        target="慢性肾病",
    )
    assert _screen(record, query=query) == (
        False,
        "treatment_or_management_only",
    )


@pytest.mark.parametrize(
    ("title", "expected_reason"),
    [
        ("II型糖尿病患者的家庭护理指导", "treatment_or_management_only"),
        ("抑郁及抑郁症的哲学解读", "non_clinical_disease_context"),
    ],
)
def test_non_explanatory_disease_articles_are_not_public_evidence(
    title, expected_reason
):
    record = EvidenceRecord(
        evidence_key="non-explanatory",
        category="literature",
        title=title,
        source="Journal",
        summary="临床人群研究讨论该疾病。",
    )
    query = EvidenceQuery(
        "II型糖尿病",
        ("disease-t2d",),
        (("II型糖尿病", "抑郁症"),),
        intent="disease_clinical_significance",
        screen_profile="disease_context",
        target="疾病",
    )
    assert _screen(record, query=query) == (False, expected_reason)


def test_treatment_guideline_is_not_used_as_diagnostic_guidance():
    record = EvidenceRecord(
        evidence_key="uc-treatment",
        category="guideline",
        title="ECCO guidelines on therapeutics in ulcerative colitis: medical treatment",
        source="ECCO",
        summary="This guideline provides recommendations for medical treatment.",
    )
    query = EvidenceQuery(
        "ulcerative colitis diagnosis screening guideline",
        ("disease-uc",),
        (("ulcerative colitis",),),
        intent="diagnostic_guideline",
        screen_profile="diagnostic",
        target="溃疡性结肠炎",
    )
    assert _screen(record, query=query) == (False, "non_diagnostic_guideline")


def test_biomarker_item_rejects_evaluation_for_a_different_glucose_target():
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-hba1c",
            name="糖化血红蛋白",
            source_type="source_report",
            item_type="biomarker",
        )
    ]
    record = EvidenceRecord(
        evidence_key="ogtt-guide",
        category="guideline",
        title="Guideline for oral glucose tolerance testing",
        source="Society",
        supports=["disease-hba1c"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "该指南讨论口服葡萄糖耐量试验。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "75克口服葡萄糖耐量试验",
                    "rationale": "用于筛查2型糖尿病。",
                    "sourcePhrase": "oral glucose tolerance test",
                    "purpose": "screen_target",
                }
            ],
        },
    )
    attach_suggested_evaluations(diseases, [record], {"ogtt-guide": 1})
    assert diseases.items[0].suggested_evaluations == []
    assert diseases.items[0].confirmation_evidence_refs == []


def test_hba1c_biomarker_evaluation_uses_level_review_wording():
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-hba1c",
            name="糖化血红蛋白",
            source_type="source_report",
            item_type="biomarker",
        )
    ]
    record = EvidenceRecord(
        evidence_key="hba1c-guide",
        category="guideline",
        title="HbA1c diagnostic guideline",
        source="Society",
        supports=["disease-hba1c"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "糖化血红蛋白可用于血糖评估。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "糖化血红蛋白",
                    "rationale": "用于糖化血红蛋白检测",
                    "sourcePhrase": "HbA1c",
                    "purpose": "screen_target",
                }
            ],
        },
    )
    attach_suggested_evaluations(diseases, [record], {"hba1c-guide": 1})
    evaluation = diseases.items[0].suggested_evaluations[0]
    assert evaluation.purpose == "评估"
    assert evaluation.rationale == "用于复核糖化血红蛋白（HbA1c）水平。"


def test_verified_source_phrase_survives_editorial_rationale_cleanup():
    disease = DiseaseRiskItem(
        diseaseId="disease-t2d",
        name="II型糖尿病",
        sourceType="source_report",
    )
    record = EvidenceRecord(
        evidenceKey="t2d-hba1c-guide",
        category="guideline",
        title="HbA1c for early detection of type 2 diabetes mellitus",
        source="Society",
        summary=(
            "HbA1c demonstrated high diagnostic accuracy for detecting "
            "type 2 diabetes mellitus."
        ),
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "该指南讨论2型糖尿病的早期检测。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "糖化血红蛋白",
                    "rationale": "研究显示HbA1c对2型糖尿病早期检测具有较高诊断准确性。",
                    "sourcePhrase": "HbA1c demonstrated high diagnostic accuracy",
                    "purpose": "screen_target",
                }
            ],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
        subject_age=31,
    )

    assert len(disease.suggested_evaluations) == 1
    assert disease.suggested_evaluations[0].name == "糖化血红蛋白"
    assert disease.suggested_evaluations[0].rationale == "用于筛查II型糖尿病。"


def test_hla_test_is_a_concrete_hospital_examination():
    disease = DiseaseRiskItem(
        diseaseId="disease-as",
        name="强直性脊柱炎",
        sourceType="source_report",
    )
    record = EvidenceRecord(
        evidenceKey="as-hla-guide",
        category="guideline",
        title="HLA-B27 testing in ankylosing spondylitis diagnosis",
        source="Society",
        summary="HLA-B27 testing is a diagnostic aid in ankylosing spondylitis.",
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "HLA-B27检测可用于强直性脊柱炎诊断评估。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "HLA-B27检测",
                    "rationale": "该检查用于辅助强直性脊柱炎诊断。",
                    "sourcePhrase": "HLA-B27 testing",
                    "purpose": "confirm_target",
                }
            ],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
        subject_age=31,
    )

    assert [item.name for item in disease.suggested_evaluations] == ["HLA-B27检测"]
    assert disease.suggested_evaluations[0].evidence_refs == [1]


def test_source_backed_specific_evaluation_is_not_lost_to_a_fixed_name_list():
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-ckd",
            name="慢性肾病",
            source_type="source_report",
        )
    ]
    record = EvidenceRecord(
        evidence_key="ckd-guide",
        category="guideline",
        title="Chronic kidney disease diagnostic guideline",
        source="Society",
        summary="eGFR is used to evaluate chronic kidney disease.",
        supports=["disease-ckd"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "慢性肾病指南讨论肾小球滤过率评估。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "eGFR评估",
                    "rationale": "用于评估肾功能",
                    "sourcePhrase": "eGFR",
                    "purpose": "characterize_target",
                }
            ],
        },
    )
    attach_suggested_evaluations(diseases, [record], {"ckd-guide": 1})
    assert [item.name for item in diseases.items[0].suggested_evaluations] == [
        "eGFR评估"
    ]


def test_explicit_guide_block_recovers_tests_when_model_omits_evaluations():
    disease = DiseaseRiskItem(
        disease_id="disease-ckd",
        name="慢性肾病",
        source_type="source_report",
    )
    record = EvidenceRecord(
        evidence_key="ckd-guide-block",
        category="guideline",
        title="2024 KDIGO慢性肾脏病评估指南",
        source="EviMed V2",
        summary=(
            "慢性肾脏病的诊断、分期和风险评估依据估算肾小球滤过率"
            "（eGFR）和尿白蛋白/肌酐比值（UACR）。"
        ),
        supports=[disease.disease_id],
        disease_ids=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=[disease.name],
        interpretation={
            "claim": "该指南说明慢性肾病的评估框架。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
            "recommendedEvaluations": [],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 7},
        subject_age=28,
    )

    assert [item.name for item in disease.suggested_evaluations] == [
        "尿白蛋白/肌酐比值（UACR）",
    ]
    assert all(item.purpose == "评估" for item in disease.suggested_evaluations)
    assert all(item.evidence_refs == [7] for item in disease.suggested_evaluations)


def test_source_bound_test_catalog_cannot_create_a_treatment_row():
    disease = DiseaseRiskItem(
        disease_id="disease-ir",
        name="胰岛素抵抗",
        source_type="source_report",
    )
    record = EvidenceRecord(
        evidence_key="ir-treatment-only",
        category="guideline",
        title="胰岛素抵抗治疗共识",
        source="EviMed V2",
        summary="共识讨论HOMA-IR相关人群的药物治疗。",
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "共识讨论治疗。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
            "recommendedEvaluations": [],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
    )

    assert disease.suggested_evaluations == []


def test_evaluation_rationale_is_clinical_and_not_research_meta_prose():
    coronary = DiseaseRiskItem(
        disease_id="disease-chd",
        name="冠心病",
        source_type="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=coronary,
            source_rationale="支持用于冠心病筛查",
            purpose="screen_target",
        )
        == "用于冠心病筛查。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=coronary,
            source_rationale="规范了冠心病CT检查的适应证和诊断",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=coronary,
            source_rationale="表明冠状动脉CT可用于冠心病诊断",
            purpose="confirm_target",
        )
        == "冠状动脉CT可用于冠心病诊断。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=coronary,
            source_rationale=(
                "CT是无创影像学检查，用于冠心病的诊断，"
                "直接支持该检查作为筛查或确认冠心病的工具。"
            ),
            purpose="screen_target",
        )
        == "CT是无创影像学检查，用于冠心病的诊断。"
    )

    lupus = DiseaseRiskItem(
        disease_id="disease-sle",
        name="系统性红斑狼疮",
        source_type="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=lupus,
            evaluation_name="抗核抗体谱检测",
            source_rationale="抗核抗体谱检测有助于提高系统性红斑狼疮的检出率。",
            purpose="screen_target",
            source_text=(
                "系统性红斑狼疮患者抗核抗体谱检测分析。"
                "采用抗核抗体谱检测进行诊断能够提高疾病检出率。"
            ),
        )
        == "抗核抗体谱检测用于辅助系统性红斑狼疮诊断，提高疾病检出率。"
    )

    insulin = DiseaseRiskItem(
        disease_id="disease-ir",
        name="胰岛素抵抗",
        source_type="source_report",
        item_type="phenotype",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=insulin,
            source_rationale="研究中用于验证胰岛素抵抗",
            purpose="characterize_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=insulin,
            source_rationale=(
                "该指数是评估胰岛素抵抗的参考指标，用于验证临床和生化指标的诊断价值。"
            ),
            purpose="characterize_target",
        )
        == "该指数是评估胰岛素抵抗的参考指标。"
    )
    constipation = DiseaseRiskItem(
        disease_id="disease-constipation",
        name="便秘",
        source_type="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            source_rationale="和继发性便秘的鉴别诊断",
            purpose="confirm_target",
        )
        is None
    )

    epilepsy = DiseaseRiskItem(
        disease_id="disease-epilepsy",
        name="癫痫",
        source_type="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=epilepsy,
            source_rationale="用于癫痫的诊断性评估，并指导治疗",
            purpose="confirm_target",
        )
        == "用于癫痫的诊断性评估。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=epilepsy,
            source_rationale="脑电图是核心检查，指南明确其不可取代的作用",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=insulin,
            source_rationale="文献直接提及该方法用于评估胰岛素抵抗",
            purpose="characterize_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=insulin,
            source_rationale="风险，≥209 mg/dL诊断",
            purpose="screen_target",
        )
        is None
    )
    psc = DiseaseRiskItem(
        disease_id="disease-psc",
        name="硬化性胆管炎",
        source_type="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=psc,
            evaluation_name="磁共振胰胆管造影（MRCP）",
            source_rationale="用于筛查原发性硬化性胆管炎的胆管改变",
            purpose="screen_target",
        )
        == "用于筛查原发性硬化性胆管炎的胆管改变。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            source_rationale="诊断",
            purpose="screen_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验",
            source_rationale="共识明确指出此方法用于慢性便秘的诊断评估",
            purpose="characterize_target",
        )
        == "此方法用于慢性便秘的诊断评估。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验",
            source_rationale="共识将结肠传输试验列为慢性便秘的常用评估方法。",
            purpose="characterize_target",
            source_text=(
                "慢性便秘检查与评估中国专家共识。"
                "实验室检查对于明确慢性便秘的病因、类型、严重程度和治疗效果具有重要意义。"
                "常用方法包括结肠传输试验和肛门直肠压力测定。"
            ),
        )
        == "用于明确慢性便秘的病因、类型、严重程度。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验（不透X线标志物法）",
            source_rationale="共识明确将结肠传输试验作为慢性便秘的检查方法之一。",
            purpose="confirm_target",
            source_text=(
                "慢性便秘检查与评估中国专家共识。"
                "实验室检查对于明确慢性便秘的病因、类型、严重程度和治疗效果具有重要意义。"
                "常用方法包括结肠传输试验。"
            ),
        )
        == "用于明确慢性便秘的病因、类型、严重程度。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验",
            source_rationale="列出该检查方法用于慢性便秘评估。",
            purpose="characterize_target",
            source_text=(
                "慢性便秘检查与评估中国专家共识。"
                "实验室检查对于明确慢性便秘的病因、类型、严重程度和治疗效果具有重要意义。"
                "常用方法包括结肠传输试验和肛门直肠压力测定。"
            ),
        )
        == "用于明确慢性便秘的病因、类型、严重程度。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验",
            source_rationale="共识将结肠传输试验列为慢性便秘的常用评估方法。",
            purpose="characterize_target",
            source_text="慢性便秘诊疗共识仅提到相关检查。",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="结肠传输试验",
            source_rationale="指南指出全面检查有利于明确便秘病因和分型，以指导治疗。",
            purpose="characterize_target",
        )
        == "全面检查有利于明确便秘病因和分型。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="相关临床检查",
            source_rationale="用于便秘的诊断、分型和治疗。",
            purpose="characterize_target",
        )
        == "用于便秘的诊断、分型。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="诊断性检查",
            source_rationale="涵盖便秘的诊断方法，支持相关检查。",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="MRI",
            source_rationale="重新评估了MRI的诊断和监测价值，并标准化了MRI方案。",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="CTE",
            source_rationale="引入CTE作为克罗恩病诊断新技术。",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="抗CCP抗体检测",
            source_rationale="评估了抗CCP抗体对RA的诊断价值。",
            purpose="confirm_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="动态血压监测",
            source_rationale="详细介绍了动态血压监测的应用。",
            purpose="characterize_target",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="冠状动脉CT血管成像",
            source_rationale="解读明确指出CCTA用于冠心病诊断。",
            purpose="confirm_target",
        )
        == "CCTA用于冠心病诊断。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="冠状动脉CT检查",
            source_rationale="用于筛查或确认冠心病，直接针对冠心病目标。",
            purpose="confirm_target",
        )
        == "用于筛查或确认冠心病。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="肛门直肠测压",
            source_rationale=(
                "结肠传输和肛门直肠测压可定义病理生理亚型，支持对慢性便秘的评估。"
            ),
            purpose="characterize_target",
        )
        == "结肠传输和肛门直肠测压可定义病理生理亚型，用于慢性便秘的评估。"
    )

    assert _evaluation_identity("抗核抗体（ANA）检测") == _evaluation_identity(
        "抗核抗体检测"
    )
    assert _evaluation_identity("冠状动脉CT成像") == _evaluation_identity(
        "冠状动脉CT血管成像"
    )


def test_confirmation_refs_include_only_displayed_evaluations():
    disease = DiseaseRiskItem(
        disease_id="disease-epilepsy",
        name="癫痫",
        source_type="source_report",
        associationEvidenceRefs=[9],
        confirmationEvidenceRefs=[1, 2],
        evidenceRefs=[1, 2, 9],
    )
    diseases = type("Section", (), {"items": [disease]})()
    good = EvidenceRecord(
        evidence_key="adult-eeg",
        category="guideline",
        title="Adult epilepsy EEG guideline",
        source="Society",
        supports=["disease-epilepsy"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "成人癫痫评估包括脑电图。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "脑电图检查",
                    "rationale": "用于记录癫痫样放电。",
                    "sourcePhrase": "EEG",
                    "purpose": "confirm_target",
                }
            ],
        },
    )
    rejected = EvidenceRecord(
        evidence_key="depression-eeg",
        category="literature",
        title="EEG for depression",
        source="Journal",
        supports=["disease-epilepsy"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "抑郁症研究记录了脑电图。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "PHQ-9量表",
                    "rationale": "用于抑郁症筛查。",
                    "sourcePhrase": "PHQ-9",
                    "purpose": "screen_target",
                }
            ],
        },
    )
    attach_suggested_evaluations(
        diseases,
        [good, rejected],
        {"adult-eeg": 1, "depression-eeg": 2},
    )
    assert disease.confirmation_evidence_refs == [1]
    assert disease.evidence_refs == [1, 9]


def test_diagnostic_guideline_does_not_fill_microbiome_risk_explanation():
    disease = DiseaseRiskItem(
        diseaseId="disease-constipation",
        name="便秘",
        sourceType="source_report",
    )
    diseases = type("Section", (), {"items": [disease]})()
    record = EvidenceRecord(
        evidenceKey="constipation-diagnostic-with-definition",
        category="guideline",
        title="便秘外科诊治指南",
        source="Society",
        supports=["disease-constipation"],
        diseaseIds=["disease-constipation"],
        targets=["便秘"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "便秘是一种常见的功能性肠病，严重影响生活质量。",
            "targetAlignment": "direct",
        },
    )
    attach_evidence_refs(
        type("Microbes", (), {"items": []})(),
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert disease.medical_interpretation is None
    assert disease.association_evidence_refs == []
    assert disease.confirmation_evidence_refs == [1]


def test_direct_human_diagnostic_study_can_support_a_concrete_evaluation():
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-epilepsy",
            name="癫痫",
            source_type="source_report",
        )
    ]
    record = EvidenceRecord(
        evidence_key="adult-eeg-study",
        category="literature",
        title="脑电图检查在成人癫痫诊断中的意义",
        source="Journal",
        supports=["disease-epilepsy"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "成人癫痫诊断研究评价了脑电图检查。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "recommendedEvaluations": [
                {
                    "name": "脑电图检查",
                    "rationale": "用于记录癫痫样放电并辅助确认癫痫。",
                    "sourcePhrase": "脑电图检查",
                    "purpose": "confirm_target",
                }
            ],
        },
    )
    attach_suggested_evaluations(
        diseases,
        [record],
        {"adult-eeg-study": 1},
    )
    assert [item.name for item in diseases.items[0].suggested_evaluations] == [
        "脑电图检查"
    ]


def test_comorbidity_mention_cannot_bind_an_unrelated_guide_test():
    disease = DiseaseRiskItem(
        disease_id="disease-obesity",
        name="肥胖症",
        source_type="source_report",
    )
    record = EvidenceRecord(
        evidence_key="endometrial-cancer-guide",
        category="guideline",
        title="子宫内膜癌诊疗指南",
        source="肿瘤学会",
        summary=(
            "肥胖症可作为相关合并症记录。"
            "增强盆腔MRI用于评估子宫肌层浸润程度。"
        ),
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "该指南讨论子宫内膜癌评估。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
            "recommendedEvaluations": [
                {
                    "name": "增强盆腔MRI",
                    "rationale": "用于评估子宫肌层浸润程度。",
                    "sourcePhrase": "增强盆腔MRI",
                    "purpose": "characterize_target",
                }
            ],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
        subject_age=57,
    )

    assert disease.suggested_evaluations == []
    assert disease.confirmation_evidence_refs == []


def test_model_rationale_cannot_bind_thyroid_ultrasound_to_diarrhoea():
    disease = DiseaseRiskItem(
        disease_id="disease-diarrhoea",
        name="腹泻",
        source_type="source_report",
    )
    record = EvidenceRecord(
        evidence_key="thyroid-cancer-guide",
        category="guideline",
        title="2025 中国肿瘤整合诊治指南—甲状腺癌",
        source="肿瘤学会",
        summary="颈部高分辨率超声用于评估甲状腺结节。",
        supports=[disease.disease_id],
        targets=["腹泻"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "该指南讨论甲状腺癌影像学评估。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "applicability": "当前报告未涉及腹泻诊断，该证据用于背景参考。",
            "recommendedEvaluations": [
                {
                    "name": "颈部高分辨率超声检查",
                    "rationale": (
                        "用于评估甲状腺结节，因此可直接用于"
                        "腹泻相关靶点的排除或确认。"
                    ),
                    "sourcePhrase": "颈部高分辨率超声检查",
                    "purpose": "characterize_target",
                }
            ],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
    )

    assert disease.suggested_evaluations == []
    assert disease.confirmation_evidence_refs == []


def test_obesity_rejects_unrelated_cancer_preparation_check():
    disease = DiseaseRiskItem(
        disease_id="disease-obesity",
        name="肥胖症",
        source_type="source_report",
    )

    assert not evaluation_is_public_for_disease(
        "心电图",
        rationale=(
            "用于评估全身状况，"
            "是子宫内膜癌保留生育功能治疗前评估的检查项目。"
        ),
        disease=disease,
    )
    assert evaluation_is_public_for_disease(
        "体质量指数（BMI）与腰围测量",
        rationale="用于评估肥胖症相关的体重与脂肪分布。",
        disease=disease,
    )


def test_exact_v2_guide_block_recovers_checks_when_model_interpretation_is_missing():
    disease = DiseaseRiskItem(
        disease_id="disease-depression",
        name="抑郁症",
        source_type="source_report",
    )
    diseases = DiseaseRisksSection(items=[disease])
    record = EvidenceRecord(
        evidence_key="depression-guide-block",
        source_evidence_key="depression-guide",
        parent_guide_id="guide-1",
        category="guideline",
        title="Clinical practice guideline for depression screening",
        source="Medical Society",
        summary=(
            "Adults should be screened for depression using the PHQ-9 "
            "patient health questionnaire."
        ),
        evidence_text=(
            "Adults should be screened for depression using the PHQ-9 "
            "patient health questionnaire."
        ),
        supports=["disease-depression"],
        intents=["diagnostic_guideline"],
    )

    recover_source_bound_diagnostic_interpretations(diseases, [record])
    assert record.interpretation is None
    attach_suggested_evaluations(
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert disease.suggested_evaluations == []
    assert disease.confirmation_evidence_refs == []


def test_promoted_cdiff_risk_recovers_explicit_diagnostic_test_from_relation_record():
    disease = DiseaseRiskItem(
        diseaseId="disease-cdiff",
        name="艰难梭菌感染",
        sourceType="evimed_association",
        sourceResult="艰难梭菌：检出 0.0711%",
        relatedMicrobes=["艰难梭菌"],
        associationEvidenceRefs=[1],
        evidenceRefs=[1],
    )
    record = EvidenceRecord(
        evidenceKey="cdiff-diagnostic-meta",
        category="literature",
        title="艰难梭菌谷氨酸脱氢酶抗原及毒素检测试剂盒的诊断价值",
        source="医学期刊",
        summary=(
            "艰难梭菌GDH抗原及毒素检测可用于艰难梭菌感染诊断，"
            "可作为医院初筛检测。"
        ),
        supports=["taxon-cdiff"],
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "艰难梭菌GDH抗原及毒素检测可用于艰难梭菌感染诊断。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "sampleAlignment": "stool",
            "recommendedEvaluations": [],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [record],
        {record.evidence_key: 1},
    )

    assert [item.name for item in disease.suggested_evaluations] == [
        "艰难梭菌GDH抗原及毒素检测"
    ]
    assert disease.suggested_evaluations[0].purpose == "筛查"
    assert disease.confirmation_evidence_refs == [1]


def test_disease_reuses_exact_hospital_test_from_another_retained_taxon_record():
    disease = DiseaseRiskItem(
        diseaseId="disease-bacteremia",
        name="菌血症",
        sourceType="evimed_association",
        sourceResult="沙门氏菌：检出",
        relatedMicrobes=["沙门氏菌"],
        associationEvidenceRefs=[1],
        evidenceRefs=[1],
    )
    association = EvidenceRecord(
        evidenceKey="salmonella-bacteremia",
        category="literature",
        title="Salmonella bacteremia in adults",
        source="Clinical journal",
        supports=["taxon-salmonella"],
        interpretation={
            "claim": "沙门氏菌可引起菌血症。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "sampleAlignment": "stool",
        },
    )
    diagnostic = EvidenceRecord(
        evidenceKey="bloodstream-infection-blood-culture",
        category="literature",
        title="血流感染的实验室诊断价值研究",
        source="Clinical laboratory journal",
        summary=(
            "成人血流感染者建议采用血培养，"
            "血培养可用于临床诊断与病原确认。"
        ),
        supports=["taxon-other"],
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "血培养可用于血流感染诊断。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
            "recommendedEvaluations": [],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [association, diagnostic],
        {association.evidence_key: 1, diagnostic.evidence_key: 2},
    )

    assert [item.name for item in disease.suggested_evaluations] == ["血培养"]
    assert disease.confirmation_evidence_refs == [2]


def test_source_bound_diagnostic_recovery_rejects_test_without_exact_disease():
    disease = DiseaseRiskItem(
        disease_id="disease-depression",
        name="抑郁症",
        source_type="source_report",
    )
    diseases = DiseaseRisksSection(items=[disease])
    record = EvidenceRecord(
        evidence_key="unrelated-phq-guide",
        category="guideline",
        title="General questionnaire implementation guide",
        source="Medical Society",
        evidence_text="PHQ-9 was listed as one of many research questionnaires.",
        supports=["disease-depression"],
        intents=["diagnostic_guideline"],
    )

    recover_source_bound_diagnostic_interpretations(diseases, [record])
    assert record.interpretation is None


def test_suggested_evaluations_remove_generic_names_and_merge_synonyms():
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-psc",
            name="硬化性胆管炎",
            source_type="source_report",
        )
    ]
    record = EvidenceRecord(
        evidence_key="psc-guide",
        category="guideline",
        title="Primary sclerosing cholangitis diagnostic guideline",
        source="Society",
        summary="MRCP is used in the diagnostic evaluation of primary sclerosing cholangitis.",
        supports=["disease-psc"],
        intents=["diagnostic_guideline"],
        interpretation={
            "claim": "硬化性胆管炎指南讨论MRCP。",
            "targetAlignment": "direct",
            "recommendedEvaluations": [
                {
                    "name": "磁共振胆管造影",
                    "rationale": "用于胆管成像",
                    "sourcePhrase": "MRCP",
                    "purpose": "confirm_target",
                },
                {
                    "name": "MRI/MRCP",
                    "rationale": "用于胆管成像",
                    "sourcePhrase": "MRI/MRCP",
                    "purpose": "confirm_target",
                },
                {
                    "name": "硬化性胆管炎诊断评估",
                    "rationale": "用于诊断评估",
                    "sourcePhrase": "diagnostic evaluation",
                    "purpose": "confirm_target",
                },
            ],
        },
    )
    attach_suggested_evaluations(diseases, [record], {"psc-guide": 1})
    assert [item.name for item in diseases.items[0].suggested_evaluations] == [
        "磁共振胆管造影"
    ]


def test_existing_clinical_result_hides_redundant_confirmation_path():
    disease = DiseaseRiskItem(
        disease_id="disease-crc",
        name="结直肠癌",
        source_type="source_report",
        existing_results=[
            {
                "itemName": "结肠镜检查",
                "result": "所见结直肠黏膜大致正常",
            }
        ],
        confirmation_evidence_refs=[1],
        evidence_refs=[1],
    )
    diseases = type("Section", (), {"items": [disease]})()
    attach_suggested_evaluations(diseases, [], {})
    assert disease.suggested_evaluations == []
    assert disease.confirmation_evidence_refs == []
    assert disease.evidence_refs == []


def test_nonclinical_enzyme_production_is_not_microbe_medical_discovery():
    record = EvidenceRecord(
        evidence_key="enzyme-production",
        category="literature",
        title="Asparaginase production by Escherichia coli",
        source="Biotechnology journal",
        summary="Industrial fermentation optimized enzyme production in bacterial culture.",
    )
    query = EvidenceQuery(
        "Escherichia coli",
        ("taxon-escherichia-coli",),
        (("Escherichia coli",),),
        intent="microbe_discovery",
        screen_profile="discovery",
        target="Escherichia coli",
    )
    assert _screen(record, query=query) == (False, "non_clinical_target_use")


def test_validated_condition_assigns_evidence_backed_microbe_role():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-ec",
                category="opportunistic",
                latin_name="Escherichia coli",
                detection_status="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="human-role",
        category="guideline",
        title="Clinical guidance for Escherichia coli infection",
        source="Society",
        microbe_ids=["taxon-ec"],
        interpretation={
            "claim": "The guidance describes Escherichia coli as an opportunistic pathogen.",
            "targetAlignment": "direct",
            "conditions": [
                {
                    "name": "机会性感染",
                    "searchTerm": "opportunistic infection",
                    "sourcePhrase": "opportunistic pathogen",
                    "relationType": "opportunistic",
                }
            ],
        },
    )
    attach_evidence_refs(microbes, diseases, [record], {"human-role": 1})
    assert microbes.items[0].clinical_role == (
        "人体资料支持大肠埃希菌在特定宿主或部位中可表现机会致病性。"
    )


def test_direct_scoped_specific_microbe_role_is_kept_instead_of_canned_text():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-pa-role",
                category="opportunistic",
                latin_name="Pseudomonas aeruginosa",
                detection_status="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    record = EvidenceRecord(
        evidenceKey="pseudomonas-specific-role",
        category="guideline",
        title="Clinical guidance for Pseudomonas aeruginosa infection",
        source="EviMed",
        microbeIds=["taxon-pa-role"],
        targets=["Pseudomonas aeruginosa / 铜绿假单胞菌"],
        interpretation={
            "claim": "Pseudomonas aeruginosa is an opportunistic human pathogen.",
            "clinicalRole": (
                "该菌是人体机会致病菌，临床意义受感染部位与宿主易感因素影响。"
            ),
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "conditions": [
                {
                    "name": "机会性感染",
                    "searchTerm": "opportunistic infection",
                    "sourcePhrase": "opportunistic human pathogen",
                    "relationType": "opportunistic",
                }
            ],
        },
    )

    attach_evidence_refs(
        microbes,
        type("Section", (), {"items": []})(),
        [record],
        {record.evidence_key: 1},
    )

    assert microbes.items[0].clinical_role == (
        "该菌是人体机会致病菌，临床意义受感染部位与宿主易感因素影响。"
    )

    record.interpretation.clinical_role = "该起事件为该菌污染食品引起的单次暴发。"
    attach_evidence_refs(
        microbes,
        type("Section", (), {"items": []})(),
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].clinical_role == (
        "人体资料支持铜绿假单胞菌在特定宿主或部位中可表现机会致病性。"
    )


def test_general_microbe_role_keeps_its_own_reference_without_disease_row():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-colonizer",
                category="opportunistic",
                latin_name="Veillonella parvula",
                detection_status="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="colonization-role",
        category="literature",
        title="Human colonisation by Veillonella parvula",
        source="Journal",
        microbe_ids=["taxon-colonizer"],
        interpretation={
            "claim": "Veillonella parvula was detected as a human coloniser.",
            "targetAlignment": "direct",
            "conditions": [
                {
                    "name": "人体定植",
                    "searchTerm": "human colonisation",
                    "sourcePhrase": "human coloniser",
                    "relationType": "colonization",
                }
            ],
        },
    )

    attach_evidence_refs(microbes, diseases, [record], {"colonization-role": 7})

    assert microbes.items[0].disease_and_risks == []
    assert microbes.items[0].clinical_role == (
        "人体研究记录小韦荣球菌可在特定人体部位定植。"
    )
    assert microbes.items[0].evidence_refs == [7]


def test_relation_evidence_is_bound_to_exact_microbe_and_disease_ids():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(name="测试患者", age=28, sex="男"),
        sample=SampleInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-bf",
                category="opportunistic",
                latin_name="Bacteroides fragilis",
                value="9.7",
                detection_status="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {})()
    diseases.items = [
        DiseaseRiskItem(
            disease_id="disease-uc",
            name="溃疡性结肠炎",
            source_type="source_report",
            related_microbes=["Bacteroides fragilis"],
        ),
        DiseaseRiskItem(
            disease_id="disease-asd",
            name="ASD",
            source_type="source_report",
            item_type="unclear",
            related_microbes=["Bacteroides fragilis"],
        ),
    ]
    record = EvidenceRecord(
        evidence_key="uc-only",
        category="literature",
        title="Bacteroides fragilis and ulcerative colitis",
        source="Journal",
        microbe_ids=["taxon-bf"],
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-uc",
                microbe_id="taxon-bf",
                microbe_name="Bacteroides fragilis",
                disease_id="disease-uc",
                disease_name="溃疡性结肠炎",
            )
        ],
        interpretation={
            "claim": "人群研究中脆弱拟杆菌与溃疡性结肠炎的发生存在相关性。",
            "targetAlignment": "direct",
        },
    )
    attach_evidence_refs(microbes, diseases, [record], {"uc-only": 1})
    assert [item.name for item in microbes.items[0].disease_and_risks] == [
        "溃疡性结肠炎"
    ]
    assert microbes.items[0].disease_and_risks[0].description == (
        "人群研究中脆弱拟杆菌与溃疡性结肠炎的发生存在相关性。"
    )
    # A microbe-disease relation supports the microbe row only; it must not
    # appear as an orphan citation for a missing disease medical explanation.
    assert diseases.items[0].association_evidence_refs == []
    assert diseases.items[1].association_evidence_refs == []


def test_relation_label_naming_another_report_taxon_is_not_attached():
    parsed = ParsedMicrobiomeReport(
        source_type="microbiome_data",
        source_name="source.json",
        subject=SubjectInfo(name="测试患者", age=63, sex="男"),
        sample=SampleInfo(),
        taxa=[
            ParsedTaxon(
                taxon_id="taxon-ab",
                category="opportunistic",
                latin_name="Acinetobacter baumannii",
                chinese_name="鲍曼不动杆菌",
                value="0.1",
                detection_status="detected",
            ),
            ParsedTaxon(
                taxon_id="taxon-pa",
                category="opportunistic",
                latin_name="Pseudomonas aeruginosa",
                chinese_name="铜绿假单胞菌",
                value="0.1",
                detection_status="detected",
            ),
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="multi-organism",
        category="literature",
        title="Clinical infections involving A. baumannii and P. aeruginosa",
        source="Journal",
        microbe_ids=["taxon-ab"],
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="wrong-cross-taxon",
                microbe_id="taxon-ab",
                microbe_name="Acinetobacter baumannii",
                disease_id="pseudomonas-infection",
                disease_name="难治性铜绿假单胞菌感染",
                claim="鲍曼不动杆菌和铜绿假单胞菌可分别引起临床感染。",
                target_alignment="direct",
                taxon_alignment="species",
                patient_applicability="contextual",
            )
        ],
        interpretation={
            "claim": "鲍曼不动杆菌和铜绿假单胞菌可分别引起临床感染。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )
    attach_evidence_refs(microbes, diseases, [record], {"multi-organism": 1})
    assert microbes.items[0].disease_and_risks == []


def test_normal_resting_ecg_is_not_used_as_coronary_disease_confirmation():
    parsed = parsed_report()
    from modules.report.schemas import ParsedDisease

    parsed.disease_results = [
        ParsedDisease(disease_id="disease-chd", name="冠心病", source_score="13.61")
    ]
    diseases = build_source_diseases(
        parsed,
        [
            HospitalDataInput(
                data_type="EXAM",
                item_name="常规心电图",
                exam_result="窦性心律，正常范围心电图",
            )
        ],
    )
    assert diseases.items[0].existing_results == []


@pytest.mark.asyncio
async def test_discovery_condition_is_interpreted_then_requeried_as_exact_pair():
    microbes = build_abnormal_microbes(parsed_report())
    record = EvidenceRecord(
        evidence_key="fn-discovery",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer",
        source="Gut",
        summary=(
            "A human stool cohort found Fusobacterium nucleatum associated "
            "with colorectal cancer in the gut microbiome."
        ),
        query_ids=["query-discovery"],
        microbe_ids=["taxon-fn"],
        supports=["taxon-fn"],
        intents=["microbe_discovery"],
        targets=["Fusobacterium nucleatum"],
    )

    class Client:
        async def generate(self, payload):
            return {
                "evidenceInterpretations": [
                    {
                        "number": 1,
                        "claim": (
                            "Fusobacterium nucleatum was associated with "
                            "colorectal cancer in a human stool cohort."
                        ),
                        "targetAlignment": "direct",
                        "taxonAlignment": "species",
                        "sampleAlignment": "stool",
                        "patientApplicability": "contextual",
                        "conditions": [
                            {
                                "name": "结直肠癌",
                                "searchTerm": "colorectal cancer",
                                "sourcePhrase": "colorectal cancer",
                                "relationType": "epidemiologic_association",
                            }
                        ],
                    }
                ]
            }

    completed = await interpret_evidence(
        Client(), records=[record], numbers={"fn-discovery": 1}
    )
    assert completed == {1}
    queries = make_candidate_queries(microbes, [record], age=28)
    assert len(queries) == 1
    assert {item.screen_profile for item in queries} == {"human_relation"}
    assert all(item.relation_disease_name == "结直肠癌" for item in queries)
    assert all(item.microbe_id == "taxon-fn" for item in queries)
    assert all(item.relation_id for item in queries)
    assert all(
        item.query == "Fusobacterium nucleatum colorectal cancer 结直肠癌"
        for item in queries
    )
    fallback = make_relation_fallback_queries(queries, [])
    assert len(fallback) == 1
    assert fallback[0].query == "具核梭杆菌 结直肠癌"
    assert "stool" not in fallback[0].query
    assert fallback[0].screen_profile == "human_relation"


def test_single_case_relation_does_not_suppress_chinese_exact_fallback():
    query = EvidenceQuery(
        query="Clostridium perfringens gas gangrene 气性坏疽",
        supports=("taxon-cp",),
        required_concepts=(
            ("Clostridium perfringens", "C. perfringens"),
            ("gas gangrene", "气性坏疽"),
        ),
        intent="microbe_disease_association",
        target="Clostridium perfringens / 产气荚膜梭菌 / 气性坏疽",
        relation_disease_id="disease-gas-gangrene",
        relation_disease_name="气性坏疽",
        relation_source_taxon="Clostridium perfringens",
        matched_microbe_ids=("taxon-cp",),
        microbe_id="taxon-cp",
        disease_id="disease-gas-gangrene",
        relation_id="relation-cp-gas-gangrene",
        relation_type="established_pathogen",
        screen_profile="human_relation",
    )
    case_record = EvidenceRecord(
        evidence_key="case-record",
        category="literature",
        title="A case of gas gangrene caused by Clostridium perfringens",
        source="Case Reports",
        summary="We present one patient with gas gangrene.",
        interpretation={
            "claim": "产气荚膜梭菌可导致气性坏疽。",
            "studyDesign": "病例报告",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-cp-gas-gangrene",
                microbe_id="taxon-cp",
                microbe_name="Clostridium perfringens",
                disease_id="disease-gas-gangrene",
                disease_name="气性坏疽",
                relation_type="established_pathogen",
            )
        ],
    )
    fallback = make_relation_fallback_queries([query], [case_record])
    assert [item.query for item in fallback] == ["产气荚膜梭菌 气性坏疽"]


def test_chinese_taxon_aliases_seed_and_pass_exact_relation_screening():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-cp",
            category="pathogenic",
            latin_name="Clostridium perfringens",
            value="0.1",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    discovery = EvidenceRecord(
        evidence_key="cp-chinese-discovery",
        category="literature",
        title="一起由产气荚膜梭菌引起的食物中毒",
        source="Journal",
        summary="该起食物中毒由产气荚膜梭菌引起。",
        microbeIds=["taxon-cp"],
        intents=["microbe_clinical_significance"],
        targets=["Clostridium perfringens / 产气荚膜梭菌"],
    )
    queries = make_candidate_queries(microbes, [discovery], age=28)
    assert len(queries) == 1
    assert queries[0].relation_disease_name == "食物中毒"

    old_spelling = EvidenceRecord(
        evidence_key="cp-chinese-old-spelling",
        category="literature",
        title="产气荚膜梭状芽胞杆菌引起的食物中毒暴发",
        source="Journal",
        summary="人群调查证实该菌导致食物中毒。",
    )
    assert _screen(old_spelling, query=queries[0])[0] is True


def test_disease_queries_keep_exact_term_and_add_bounded_gut_association():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
        )
    ]
    queries = make_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        age=28,
    )
    clinical = [
        query for query in queries if query.intent == "disease_clinical_significance"
    ]
    assert {query.query for query in clinical} == {
        "colorectal cancer human gut microbiome association",
    }
    exact = [query for query in clinical if query.screen_profile == "disease_context"]
    association = [
        query for query in clinical if query.screen_profile == "gut_association"
    ]
    assert exact == []
    assert [query.channels for query in association] == [("literature",)]
    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        [],
        age=28,
    )
    disease_fallback = {
        query.query
        for query in coverage
        if query.intent == "disease_clinical_significance"
    }
    assert disease_fallback == {
        (
            '"colorectal cancer"[Title/Abstract] AND '
            '("gut microbiome"[Title/Abstract] OR '
            '"gut microbiota"[Title/Abstract] OR '
            '"intestinal microbiome"[Title/Abstract] OR '
            '"intestinal microbiota"[Title/Abstract])'
        ),
    }


def test_clinical_followup_context_only_generates_diagnostic_coverage():
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="clinical-hyperlipidemia",
                name="高脂血症",
                sourceType="clinical_context",
                sourceResult="临床资料记录",
                existingResults=[
                    {
                        "itemName": "低密度脂蛋白(LDL-C)",
                        "result": "3.70（参考范围：0.00-3.37）",
                        "status": "偏高",
                    }
                ],
            )
        ]
    )
    primary = make_queries(AbnormalMicrobesSection(), diseases, age=28)
    coverage = make_coverage_queries(
        AbnormalMicrobesSection(),
        diseases,
        [],
        age=28,
    )

    assert not any(query.disease_id == "clinical-hyperlipidemia" for query in primary)
    clinical_coverage = [
        query
        for query in coverage
        if query.disease_id == "clinical-hyperlipidemia"
    ]
    assert clinical_coverage
    assert {query.intent for query in clinical_coverage} == {"diagnostic_guideline"}


def test_clinical_followup_prefers_uncompleted_diagnostic_hint():
    disease = DiseaseRiskItem(
        diseaseId="clinical-fatty-liver",
        name="非酒精性脂肪肝",
        sourceType="clinical_context",
        sourceResult="临床资料记录",
        existingResults=[
            {
                "itemName": "腹部超声",
                "result": "脂肪肝",
            },
            {
                "itemName": "丙氨酸氨基转移酶(ALT)",
                "result": "118 U/L",
                "status": "偏高",
            },
        ],
    )

    coverage = make_coverage_queries(
        AbnormalMicrobesSection(),
        DiseaseRisksSection(items=[disease]),
        [],
        age=48,
    )
    selected = _bounded_evimed_coverage_queries(coverage)
    clinical = [
        query for query in selected if query.disease_id == disease.disease_id
    ]

    assert len(clinical) == 1
    assert "FibroScan" in clinical[0].query


def test_primary_disease_queries_do_not_eagerly_expand_overviews_or_examinations():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id=f"disease-{index}",
            name=name,
            source_score=str(index),
        )
        for index, name in enumerate(
            ("结直肠癌", "肾结石", "炎症性肠病", "2型糖尿病"),
            start=1,
        )
    ]

    queries = make_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        age=57,
    )
    disease_queries = [query for query in queries if query.disease_id]

    assert len(disease_queries) == len(parsed.disease_results)
    assert all(query.screen_profile == "gut_association" for query in disease_queries)
    assert not any(query.intent == "diagnostic_guideline" for query in queries)


def test_general_disease_explanation_does_not_suppress_gut_association_retry():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
        )
    ]
    record = EvidenceRecord(
        evidence_key="crc-overview",
        category="literature",
        title="Colorectal cancer.",
        source="Journal",
        summary="Colorectal cancer is a common malignant disease in adults.",
        supports=["disease-crc"],
        disease_ids=["disease-crc"],
        intents=["disease_clinical_significance"],
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌是一种常见的恶性肿瘤。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        [record],
        age=28,
    )

    disease_queries = [
        query.query
        for query in coverage
        if query.intent == "disease_clinical_significance"
    ]
    assert disease_queries == [
        (
            '"colorectal cancer"[Title/Abstract] AND '
            '("gut microbiome"[Title/Abstract] OR '
            '"gut microbiota"[Title/Abstract] OR '
            '"intestinal microbiome"[Title/Abstract] OR '
            '"intestinal microbiota"[Title/Abstract])'
        )
    ]


def test_incidental_gut_mention_does_not_suppress_exact_disease_gut_retry():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-constipation",
            name="便秘",
            source_score="1.2",
        )
    ]
    off_target = EvidenceRecord(
        evidence_key="ibs-fodmap",
        category="literature",
        title="The Role of the FODMAP Diet in IBS",
        source="Journal",
        summary=(
            "Human patients with irritable bowel syndrome may have altered gut "
            "microbiota and can report constipation among several symptoms."
        ),
        supports=["disease-constipation"],
        disease_ids=["disease-constipation"],
        intents=["disease_clinical_significance"],
        targets=["便秘"],
        interpretation={
            "claim": "便秘可能作为肠易激综合征的症状出现。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed),
        build_source_diseases(parsed, []),
        [off_target],
        age=28,
    )

    assert any(
        query.screen_profile == "gut_association"
        and query.disease_id == "disease-constipation"
        for query in coverage
    )


def test_overview_title_separator_is_not_treated_as_disease_subtype():
    record = EvidenceRecord(
        evidence_key="schizophrenia-overview",
        category="literature",
        title="Schizophrenia-An Overview.",
        source="Journal",
        summary="Schizophrenia is a severe mental disease in adults.",
        supports=["disease-schizophrenia"],
        disease_ids=["disease-schizophrenia"],
        intents=["disease_clinical_significance"],
        targets=["精神分裂症"],
        interpretation={
            "claim": "精神分裂症是一种严重的精神疾病。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == "精神分裂症是一种严重的精神疾病。"


def test_disease_display_name_accepts_canonical_alias_as_claim_subject():
    record = EvidenceRecord(
        evidence_key="hyperlipidemia-overview",
        category="literature",
        title="Epidemiology and management of hyperlipidemia.",
        source="Journal",
        summary=(
            "Adults with hyperlipidemia have roughly twice the risk of "
            "cardiovascular disease as adults with normal cholesterol."
        ),
        supports=["disease-lipid"],
        disease_ids=["disease-lipid"],
        intents=["disease_clinical_significance"],
        targets=["高血脂"],
        interpretation={
            "claim": "高脂血症患者心血管疾病风险约为正常人的两倍。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == (
        "高脂血症患者心血管疾病风险约为正常人的两倍。"
    )


def test_ascii_diagnostic_hint_uses_english_disease_name_in_evimed_query():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-depression",
            name="抑郁症",
            source_score="43.86",
        )
    ]

    diseases = build_source_diseases(parsed, [])
    diseases.items[0].source_type = "evimed_association"
    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed),
        diseases,
        [],
        age=28,
    )

    diagnostic_queries = {
        query.query for query in coverage if query.intent == "diagnostic_guideline"
    }
    assert (
        "depression PHQ-9 diagnostic guideline"
        in diagnostic_queries
    )
    assert (
        "depression patient health questionnaire diagnostic guideline"
        in diagnostic_queries
    )
    assert "抑郁症 PHQ-9" not in diagnostic_queries


def test_coverage_retry_only_targets_rows_without_retained_evidence():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    queries = make_coverage_queries(microbes, diseases, [], age=28)
    assert any(
        query.intent == "microbe_clinical_significance"
        and query.query == "Fusobacterium nucleatum infection disease"
        for query in queries
    )
    assert any(
        query.intent == "disease_clinical_significance"
        and query.screen_profile == "gut_association"
        for query in queries
    )
    diagnostic_queries = [
        query for query in queries if query.intent == "diagnostic_guideline"
    ]
    assert diagnostic_queries == []

    covered = [
        EvidenceRecord(
            evidence_key="microbe-covered",
            category="literature",
            title="Fusobacterium nucleatum clinical cohort",
            source="Journal",
            microbe_ids=["taxon-fn"],
            intents=["microbe_clinical_significance"],
            interpretation={
                "claim": "Fusobacterium nucleatum is clinically relevant.",
                "targetAlignment": "direct",
                "taxonAlignment": "species",
                "clinicalRole": "Fusobacterium nucleatum is clinically relevant.",
                "conditions": [
                    {
                        "name": "结直肠癌",
                        "searchTerm": "colorectal cancer",
                        "sourcePhrase": "colorectal cancer",
                        "relationType": "epidemiologic_association",
                    }
                ],
            },
            relationBindings=[
                {
                    "relationId": "relation-fn-crc",
                    "microbeId": "taxon-fn",
                    "microbeName": "Fusobacterium nucleatum",
                    "diseaseId": "disease-crc",
                    "diseaseName": "结直肠癌",
                    "relationType": "epidemiologic_association",
                    "claim": "具核梭杆菌与结直肠癌相关。",
                    "targetAlignment": "direct",
                }
            ],
        ),
        EvidenceRecord(
            evidence_key="disease-covered",
            category="literature",
            title="Colorectal cancer outcomes",
            source="Journal",
            disease_ids=["disease-crc"],
            intents=["disease_clinical_significance"],
            interpretation={
                "claim": "结直肠癌的临床表现可包括便血和排便习惯改变。",
                "targetAlignment": "direct",
            },
        ),
            EvidenceRecord(
                evidence_key="diagnosis-covered",
                category="guideline",
                title="Colorectal cancer diagnosis guideline",
                source="Society",
                summary="Colonoscopy is used to confirm colorectal cancer.",
                disease_ids=["disease-crc"],
            intents=["diagnostic_guideline"],
            interpretation={
                "claim": "结直肠癌指南讨论诊断。",
                "targetAlignment": "direct",
                "recommendedEvaluations": [
                    {
                        "name": "结肠镜检查",
                        "rationale": "用于确认结直肠癌",
                        "sourcePhrase": "diagnosis",
                        "purpose": "confirm_target",
                    }
                ],
            },
        ),
    ]
    remaining = make_coverage_queries(microbes, diseases, covered, age=28)
    assert [query.query for query in remaining] == [
        (
            '"colorectal cancer"[Title/Abstract] AND '
            '("gut microbiome"[Title/Abstract] OR '
            '"gut microbiota"[Title/Abstract] OR '
            '"intestinal microbiome"[Title/Abstract] OR '
            '"intestinal microbiota"[Title/Abstract])'
        )
    ]


def test_microbe_coverage_queries_send_nomenclature_synonym_to_evimed():
    parsed = parsed_report()
    parsed.taxa[0].taxon_id = "taxon-bv"
    parsed.taxa[0].latin_name = "Bacteroides vulgatus"
    parsed.taxa[0].chinese_name = "普通拟杆菌"
    microbes = build_abnormal_microbes(parsed)

    queries = make_coverage_queries(microbes, build_source_diseases(parsed, []), [])

    assert "Phocaeicola vulgatus infection disease" in {
        query.query
        for query in queries
        if query.intent == "microbe_clinical_significance"
    }


def test_low_beneficial_microbe_uses_abundance_association_not_pathogen_query():
    parsed = parsed_report()
    parsed.taxa[0].category = "beneficial"
    parsed.taxa[0].latin_name = "Bifidobacterium longum"
    parsed.taxa[0].chinese_name = "长双歧杆菌"
    parsed.taxa[0].source_status = "偏低"
    parsed.taxa[0].explicitly_abnormal = True
    microbes = build_abnormal_microbes(parsed)

    queries = make_coverage_queries(microbes, build_source_diseases(parsed, []), [])

    microbe_queries = [query for query in queries if query.microbe_id]
    assert {query.query for query in microbe_queries} == {
        "Bifidobacterium longum low abundance",
        "Bifidobacterium longum depleted",
        "B. longum low abundance",
        "B. longum depleted",
        "B longum low abundance",
        "B longum depleted",
        "长双歧杆菌 低丰度",
        "长双歧杆菌 缺乏",
        "Bifidobacterium longum human health",
    }
    assert {query.intent for query in microbe_queries} == {"microbe_association"}
    assert {query.channels for query in microbe_queries} == {("literature",)}
    assert not any("pathogen" in query.query for query in microbe_queries)


def test_high_other_microbe_uses_abundance_association_not_pathogen_query():
    parsed = parsed_report()
    parsed.taxa[0].category = "other"
    parsed.taxa[0].latin_name = "Dorea formicigenerans"
    parsed.taxa[0].chinese_name = "蚁酸生成多雷氏菌"
    parsed.taxa[0].source_status = "超标"
    parsed.taxa[0].explicitly_abnormal = True
    microbes = build_abnormal_microbes(parsed)

    queries = make_coverage_queries(microbes, build_source_diseases(parsed, []), [])

    microbe_queries = [query for query in queries if query.microbe_id]
    assert {query.query for query in microbe_queries} == {
        "Dorea formicigenerans high abundance",
        "Dorea formicigenerans increased",
        "D. formicigenerans high abundance",
        "D. formicigenerans increased",
        "D formicigenerans high abundance",
        "D formicigenerans increased",
        "蚁酸生成多雷氏菌 高丰度",
        "蚁酸生成多雷氏菌 丰度升高",
        "Dorea formicigenerans human health",
    }
    assert {query.intent for query in microbe_queries} == {"microbe_association"}
    assert not any("pathogen" in query.query for query in microbe_queries)


def test_source_diseases_use_english_evimed_primary_terms_when_known():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-nafld",
            name="非酒精性脂肪肝",
            source_score="24.76",
        ),
        ParsedDisease(
            disease_id="disease-parkinson",
            name="帕金森病",
            source_score="35.56",
        ),
    ]
    diseases = build_source_diseases(parsed, [])

    queries = make_queries(build_abnormal_microbes(parsed), diseases, age=63)
    disease_queries = {
        query.disease_id: query.query
        for query in queries
        if query.intent == "disease_clinical_significance"
        and query.screen_profile == "gut_association"
    }
    assert disease_queries == {
        "disease-nafld": (
            "nonalcoholic fatty liver disease human gut microbiome association"
        ),
        "disease-parkinson": "Parkinson disease human gut microbiome association",
    }


def test_source_report_disease_spelling_variants_use_canonical_search_terms():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-ibd-variant",
            name="炎症性肠道疾病",
            source_score="40",
        ),
        ParsedDisease(
            disease_id="disease-lipid-variant",
            name="高血脂",
            source_score="30",
        ),
        ParsedDisease(
            disease_id="disease-cvd-variant",
            name="心脑血管疾病",
            source_score="20",
        ),
    ]
    diseases = build_source_diseases(parsed, [])

    primary = {
        query.disease_id: query.query
        for query in make_queries(build_abnormal_microbes(parsed), diseases, age=57)
        if query.intent == "disease_clinical_significance"
        and query.screen_profile == "gut_association"
    }
    assert primary == {
        "disease-ibd-variant": (
            "inflammatory bowel disease human gut microbiome association"
        ),
        "disease-lipid-variant": "hyperlipidemia human gut microbiome association",
        "disease-cvd-variant": (
            "cardiovascular disease human gut microbiome association"
        ),
    }

    for disease in diseases.items:
        disease.source_type = "evimed_association"
    coverage = make_coverage_queries(
        build_abnormal_microbes(parsed),
        diseases,
        [],
        age=57,
    )
    diagnostic_queries = {
        query.disease_id: [] for query in coverage if query.disease_id
    }
    for query in coverage:
        if query.intent == "diagnostic_guideline" and query.disease_id:
            diagnostic_queries.setdefault(query.disease_id, []).append(query.query)
    assert any(
        "inflammatory bowel disease" in query
        for query in diagnostic_queries["disease-ibd-variant"]
    )
    assert any(
        "hyperlipidemia" in query
        for query in diagnostic_queries["disease-lipid-variant"]
    )
    assert any(
        "cardiovascular disease" in query
        for query in diagnostic_queries["disease-cvd-variant"]
    )


def test_abnormal_beneficial_taxon_uses_top_source_disease_as_exact_candidate():
    parsed = parsed_report()
    parsed.taxa[0].category = "beneficial"
    parsed.taxa[0].latin_name = "Bifidobacterium longum"
    parsed.taxa[0].chinese_name = "长双歧杆菌"
    parsed.taxa[0].source_status = "偏低"
    parsed.taxa[0].explicitly_abnormal = True
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-constipation",
            name="便秘",
            source_score="55.56",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])

    queries = make_candidate_queries(
        microbes,
        [],
        diseases=diseases,
        age=42,
    )

    assert [(query.query, query.screen_profile) for query in queries] == [
        ("Bifidobacterium longum constipation abundance adult", "gut_association")
    ]
    assert queries[0].relation_disease_name == "便秘"


def test_probiotic_trial_is_not_used_as_low_abundance_disease_association():
    record = EvidenceRecord(
        evidence_key="b-longum-rct",
        category="literature",
        title=(
            "Usefulness of Bifidobacterium longum BB536 in adults with "
            "chronic constipation: a randomized controlled trial"
        ),
        source="Journal",
        summary=(
            "Adults with constipation received probiotic Bifidobacterium "
            "longum and bowel outcomes were assessed."
        ),
    )
    query = EvidenceQuery(
        query="Bifidobacterium longum constipation abundance adult",
        supports=("taxon-b-longum", "disease-constipation"),
        required_concepts=(("Bifidobacterium longum",), ("constipation",)),
        intent="microbe_disease_association",
        target="Bifidobacterium longum / 长双歧杆菌 / 便秘",
        relation_disease_id="disease-constipation",
        relation_disease_name="便秘",
        relation_source_taxon="Bifidobacterium longum",
        matched_microbe_ids=("taxon-b-longum",),
        microbe_id="taxon-b-longum",
        disease_id="disease-constipation",
        relation_id="relation-b-longum-constipation",
        screen_profile="gut_association",
    )
    assert _screen(record, query=query) == (
        False,
        "intervention_not_abundance_association",
    )
    fallback = make_relation_fallback_queries([query], [])
    assert len(fallback) == 1
    assert fallback[0].screen_profile == "gut_association"


def test_beneficial_intervention_condition_is_not_promoted_to_abundance_risk():
    parsed = parsed_report()
    parsed.taxa[0].category = "beneficial"
    parsed.taxa[0].latin_name = "Bifidobacterium longum"
    parsed.taxa[0].chinese_name = "长双歧杆菌"
    parsed.taxa[0].source_status = "偏低"
    parsed.taxa[0].explicitly_abnormal = True
    microbes = build_abnormal_microbes(parsed)
    record = EvidenceRecord(
        evidence_key="b-longum-synbiotic",
        category="clinical_trial",
        title="Bifidobacterium longum W11 in adults with constipation",
        source="Journal",
        summary="Adults with constipation received Bifidobacterium longum.",
        intents=["microbe_discovery"],
        microbe_ids=[microbes.items[0].microbe_id],
        interpretation={
            "claim": "长双歧杆菌补充干预与便秘改善相关。",
            "studyDesign": "随机对照试验",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "stool",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "便秘",
                    "searchTerm": "constipation",
                    "sourcePhrase": "constipation",
                    "relationType": "epidemiologic_association",
                }
            ],
        },
    )
    promote_discovery_conditions_to_bindings([record], microbes)
    assert record.relation_bindings == []
    record.relation_bindings = [
        EvidenceRelationBinding(
            relationId="relation-intervention",
            microbeId=microbes.items[0].microbe_id,
            microbeName="Bifidobacterium longum",
            microbeAliases=["长双歧杆菌"],
            diseaseId="disease-constipation",
            diseaseName="便秘",
            claim="长双歧杆菌补充干预与便秘改善相关。",
            targetAlignment="direct",
            taxonAlignment="species",
            sampleAlignment="stool",
            patientApplicability="contextual",
        )
    ]
    attach_evidence_refs(
        microbes,
        type("DiseaseSection", (), {"items": []})(),
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].disease_and_risks == []


@pytest.mark.asyncio
async def test_relation_retrieval_deduplicates_before_limit_and_prefers_adult_source():
    class Client:
        async def search(self, query, *, supports):
            common = {
                "category": "literature",
                "source": "Journal",
                "summary": (
                    "Campylobacter coli is associated with gastroenteritis "
                    "in a human cohort."
                ),
            }
            return [
                EvidenceRecord(
                    evidence_key="pediatric-a",
                    title="Campylobacter coli gastroenteritis in children",
                    **common,
                ),
                EvidenceRecord(
                    evidence_key="pediatric-b",
                    title="CAMPYLOBACTER COLI GASTROENTERITIS IN CHILDREN",
                    **common,
                ),
                EvidenceRecord(
                    evidence_key="adult-cohort",
                    title="Campylobacter coli gastroenteritis in adult patients",
                    **common,
                ),
                EvidenceRecord(
                    evidence_key="adult-outbreak",
                    title="Campylobacter coli gastroenteritis outbreak in patients",
                    **common,
                ),
            ]

    result = await retrieve_evidence(
        Client(),
        [
            EvidenceQuery(
                query="Campylobacter coli gastroenteritis",
                supports=("taxon-cc",),
                required_concepts=(
                    ("Campylobacter coli",),
                    ("gastroenteritis",),
                ),
                intent="microbe_disease_association",
                target="Campylobacter coli / 胃肠炎",
                relation_disease_id="disease-ge",
                relation_disease_name="胃肠炎",
                relation_source_taxon="Campylobacter coli",
                microbe_id="taxon-cc",
                disease_id="disease-ge",
                relation_id="relation-cc-ge",
                relation_type="established_pathogen",
                age_group="adult",
                screen_profile="human_relation",
            )
        ],
    )

    titles = [record.title for record in result.records]
    assert "Campylobacter coli gastroenteritis in adult patients" in titles
    assert "Campylobacter coli gastroenteritis outbreak in patients" in titles
    assert sum("children" in title.casefold() for title in titles) == 1


def test_clinical_role_without_condition_does_not_suppress_microbe_retry():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = build_source_diseases(parsed_report(), [])
    role_only = EvidenceRecord(
        evidence_key="microbe-role-only",
        category="literature",
        title="Fusobacterium nucleatum clinical overview",
        source="Journal",
        microbeIds=["taxon-fn"],
        intents=["microbe_clinical_significance"],
        interpretation={
            "claim": "Fusobacterium nucleatum is clinically relevant.",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "clinicalRole": "Fusobacterium nucleatum is clinically relevant.",
        },
    )
    retry = make_coverage_queries(microbes, diseases, [role_only], age=28)
    assert {
        item.query for item in retry if item.intent == "microbe_clinical_significance"
    } == {
        "Fusobacterium nucleatum infection disease",
        "Fusobacterium nucleatum clinical infection",
        "Fusobacterium nucleatum pathogen",
        "F. nucleatum infection disease",
        "F nucleatum infection disease",
        "具核梭杆菌 感染 疾病",
    }


def test_cross_site_relation_does_not_suppress_stool_relation_retry():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-aeromonas",
        name="豚鼠气单胞菌",
        scientificName="Aeromonas caviae",
        category="pathogenic_signal",
        sourceFinding={"status": "超标", "value": "0.0003", "unit": "%"},
    )
    record = EvidenceRecord(
        evidenceKey="aeromonas-blood",
        category="literature",
        title="Aeromonas caviae bacteremia in adults",
        source="Journal",
        microbeIds=[microbe.microbe_id],
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "Aeromonas caviae was associated with bacteremia.",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            # Simulate a model omission: the literal source still says
            # bacteremia, so coverage must not be treated as a stool relation.
            "sampleAlignment": "unclear",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-aeromonas-bacteremia",
                microbeId=microbe.microbe_id,
                microbeName=microbe.scientific_name,
                diseaseId="disease-bacteremia",
                diseaseName="菌血症",
                relationType="opportunistic",
                claim="Aeromonas caviae was associated with bacteremia.",
                targetAlignment="direct",
                taxonAlignment="species",
                sampleAlignment="unclear",
                patientApplicability="contextual",
            )
        ],
    )

    retry = make_coverage_queries(
        AbnormalMicrobesSection(items=[microbe]),
        DiseaseRisksSection(),
        [record],
        age=28,
    )

    assert any(
        query.microbe_id == microbe.microbe_id
        and query.intent == "microbe_clinical_significance"
        for query in retry
    )


def test_unbound_candidate_condition_does_not_suppress_name_based_retry():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = build_source_diseases(parsed_report(), [])
    candidate_only = EvidenceRecord(
        evidence_key="microbe-condition-only",
        category="guideline",
        title="Fusobacterium nucleatum clinical overview",
        source="Journal",
        microbeIds=["taxon-fn"],
        intents=["microbe_discovery"],
        interpretation={
            "claim": "Fusobacterium nucleatum is discussed with colorectal cancer.",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "conditions": [
                {
                    "name": "结直肠癌",
                    "searchTerm": "colorectal cancer",
                    "sourcePhrase": "colorectal cancer",
                    "relationType": "epidemiologic_association",
                }
            ],
        },
    )

    retry = make_coverage_queries(microbes, diseases, [candidate_only], age=28)

    assert any(
        query.intent == "microbe_clinical_significance"
        and query.query == "Fusobacterium nucleatum infection disease"
        for query in retry
    )


def test_raw_uninterpreted_hit_does_not_suppress_coverage_retry():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    raw_hit = EvidenceRecord(
        evidence_key="raw-uninterpreted",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer",
        source="Journal",
        microbe_ids=["taxon-fn"],
        disease_ids=["disease-crc"],
        intents=[
            "microbe_clinical_significance",
            "disease_clinical_significance",
            "diagnostic_guideline",
        ],
    )
    queries = make_coverage_queries(microbes, diseases, [raw_hit], age=28)
    assert {query.intent for query in queries} >= {
        "microbe_clinical_significance",
        "disease_clinical_significance",
    }
    assert not any(query.intent == "diagnostic_guideline" for query in queries)


def test_tautological_disease_claim_does_not_suppress_coverage_retry():
    parsed = parsed_report()
    parsed.disease_results = [
        ParsedDisease(
            disease_id="disease-crc",
            name="结直肠癌",
            source_score="1.2",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    diseases = build_source_diseases(parsed, [])
    records = [
        EvidenceRecord(
            evidence_key="disease-overview",
            category="literature",
            title="Colorectal cancer clinical features",
            source="Journal",
            disease_ids=["disease-crc"],
            intents=["disease_clinical_significance"],
            interpretation={
                "claim": "结直肠癌是该文献直接讨论的疾病。",
                "targetAlignment": "direct",
                "patientApplicability": "not_applicable",
            },
        ),
        EvidenceRecord(
            evidence_key="diagnostic-guide",
            category="guideline",
            title="Colorectal cancer diagnostic guideline",
            source="Society",
            disease_ids=["disease-crc"],
            intents=["diagnostic_guideline"],
            interpretation={
                "claim": "结直肠癌指南讨论诊断。",
                "targetAlignment": "direct",
                "patientApplicability": "not_applicable",
                "recommendedEvaluations": [
                    {
                        "name": "结肠镜检查",
                        "rationale": "用于确认结直肠癌",
                        "sourcePhrase": "diagnostic",
                        "purpose": "confirm_target",
                    }
                ],
            },
        ),
    ]
    remaining = make_coverage_queries(microbes, diseases, records, age=28)
    assert any(
        query.disease_id == "disease-crc"
        and query.intent == "disease_clinical_significance"
        for query in remaining
    )


def test_special_population_disease_hit_is_not_public_or_coverage():
    record = EvidenceRecord(
        evidence_key="dialysis-sleep",
        category="literature",
        title="睡眠障碍与非睡眠障碍维持性血液透析患者肠道菌群分析",
        source="Journal",
        summary="临床研究比较维持性血液透析患者。",
        targets=["睡眠障碍"],
        interpretation={
            "claim": "维持性血液透析患者的睡眠障碍与肠道菌群相关。",
            "targetAlignment": "direct",
        },
    )
    query = EvidenceQuery(
        "sleep disorders",
        ("disease-sleep",),
        (("sleep disorders", "睡眠障碍"),),
        intent="disease_clinical_significance",
        target="睡眠障碍",
        screen_profile="disease_context",
    )
    assert _screen(record, query=query) == (
        False,
        "restricted_or_comorbid_context",
    )


def test_sleep_disease_hit_in_diabetes_bone_context_is_rejected():
    record = EvidenceRecord(
        evidence_key="diabetes-bone-sleep",
        category="literature",
        title="睡眠障碍与2型糖尿病患者骨密度降低和骨折风险",
        source="Journal",
        summary="研究评估糖尿病特殊人群的骨折风险。",
        targets=["睡眠障碍"],
    )
    query = EvidenceQuery(
        "sleep disorders",
        ("disease-sleep",),
        (("sleep disorders", "睡眠障碍"),),
        intent="disease_clinical_significance",
        target="睡眠障碍",
        screen_profile="disease_context",
    )
    assert _screen(record, query=query) == (
        False,
        "restricted_or_comorbid_context",
    )


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("癫痫", "癫痫人群丙戊酸暴露与癌症发生风险"),
        ("冠心病", "2007 SIGN指南：冠心病的心律失常"),
        ("腺瘤", "Statin use and colorectal adenoma risk in a prevention trial"),
        (
            "溃疡性结肠炎",
            "Donor-derived strain associated with fecal microbial transplant failure in ulcerative colitis",
        ),
        (
            "溃疡性结肠炎",
            "Oral-gut axis mechanisms and therapeutic implications in ulcerative colitis",
        ),
        ("睡眠障碍", "睡眠障碍及感觉过敏与带状疱疹后神经痛风险"),
        ("抑郁症", "伴精神病性症状的抑郁症临床表现"),
        (
            "II型糖尿病",
            "Pharmacological management of type 2 diabetes mellitus and CKD",
        ),
        (
            "结直肠癌",
            "NCCN hereditary and familial high risk assessment for colorectal cancer",
        ),
        ("结直肠癌", "CT and MRI annotation consensus for colorectal cancer"),
        (
            "克罗恩病",
            "Development of an early Crohn disease definition for disease-modification trials",
        ),
        ("慢性肾病", "地中海贫血患者发生慢性肾病的风险评估"),
    ],
)
def test_comorbid_or_intervention_disease_context_is_rejected(target, title):
    record = EvidenceRecord(
        evidence_key=title,
        category="literature",
        title=title,
        source="Journal",
        summary=f"人群研究记录{target}相关风险。",
        targets=[target],
    )
    query = EvidenceQuery(
        target,
        ("disease-target",),
        ((target,),),
        intent="disease_clinical_significance",
        target=target,
        screen_profile="disease_context",
    )
    assert _screen(record, query=query) == (
        False,
        "restricted_or_comorbid_context",
    )


def test_public_disease_claim_rejects_tautology_but_keeps_clinical_meaning():
    record = EvidenceRecord(
        evidence_key="crc",
        category="literature",
        title="Colorectal cancer clinical features",
        source="Journal",
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌是该证据直接讨论的疾病。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) is None
    record.interpretation.claim = "结直肠癌的临床表现可包括便血和排便习惯改变。"
    assert _public_disease_claim(record) == record.interpretation.claim


def test_public_disease_claim_uses_exact_source_sentence_when_model_is_tautological():
    record = EvidenceRecord(
        evidence_key="constipation-source-fallback",
        category="guideline",
        title="便秘外科诊治指南（2025版）",
        source="Society",
        summary=(
            "慢性便秘是一种常见的功能性肠病，"
            "严重影响患者的生活质量与心理健康。"
            "新版指南更新了诊疗策略。"
        ),
        targets=["便秘"],
        interpretation={
            "claim": "该指南系统更新了便秘的诊断和治疗策略。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == (
        "慢性便秘是一种常见的功能性肠病，严重影响患者的生活质量与心理健康。"
    )


def test_public_disease_claim_does_not_turn_source_management_into_meaning():
    record = EvidenceRecord(
        evidence_key="constipation-management-source",
        category="guideline",
        title="便秘管理指南",
        source="Society",
        summary="指南建议便秘患者增加纤维摄入并使用渗透性泻药。",
        targets=["便秘"],
        interpretation={
            "claim": "该指南提供了便秘管理建议。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(record) is None


@pytest.mark.parametrize(
    "summary",
    [
        "建议包括区分功能性和继发性便秘。",
        "强调便秘不仅是独立症状，也可能是多种疾病的体现。",
        "以期切实解决功能性便秘的实际问题。",
        "指出其在慢性功能性便秘中的角色尚未完全明确。",
    ],
)
def test_public_disease_claim_rejects_source_editorial_fragments(summary):
    record = EvidenceRecord(
        evidence_key=summary,
        category="guideline",
        title="成人便秘指南",
        source="Society",
        summary=summary,
        targets=["便秘"],
        interpretation={
            "claim": "该指南为便秘管理提供建议。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(record) is None


def test_public_disease_claim_removes_population_and_special_cohort_clauses():
    record = EvidenceRecord(
        evidence_key="crohn-context",
        category="literature",
        title="Crohn disease clinical review",
        source="Journal",
        targets=["克罗恩病"],
        interpretation={
            "claim": (
                "克罗恩病是慢性炎症性肠病，静脉导管相关血流感染与导管留置时间相关。"
            ),
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) == "克罗恩病是慢性炎症性肠病。"

    record.targets = ["甲状腺癌"]
    record.interpretation.claim = "甲状腺癌全球发病率上升，已成为十大恶性肿瘤之一。"
    assert _public_disease_claim(record) is None


def test_public_disease_claim_rejects_monitoring_and_tcm_outcome_sources():
    monitoring = EvidenceRecord(
        evidence_key="cgm",
        category="guideline",
        title="Continuous glucose monitoring consensus",
        source="Society",
        targets=["糖化血红蛋白"],
        interpretation={
            "claim": "持续葡萄糖监测可改善糖化血红蛋白水平。",
            "targetAlignment": "direct",
        },
    )
    tcm = EvidenceRecord(
        evidence_key="tcm-adenoma",
        category="literature",
        title="结直肠腺瘤中医疗效评价",
        source="Journal",
        targets=["腺瘤"],
        interpretation={
            "claim": "腺瘤相关结局包括中医证候积分和疗效。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(monitoring) is None
    assert _public_disease_claim(tcm) is None


@pytest.mark.parametrize(
    "claim",
    [
        "该文献摘要支持空腹血糖异常的临床意义。",
        "支持克罗恩病的管理。",
        "敌意与冠心病相关，社会支持与冠心病相关。",
        "不典型临床表现的癫痫可出现腹痛。",
        "推荐慢性肾病患者限制蛋白质摄入。",
        "慢性工作压力和轮班工作与冠心病风险相关。",
        "该证据总结为炎症性肠病提供营养支持。",
    ],
)
def test_public_disease_claim_rejects_process_or_incidental_context(claim):
    record = EvidenceRecord(
        evidence_key=claim,
        category="literature",
        title="Disease context",
        source="Journal",
        targets=["冠心病"],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )
    assert _public_disease_claim(record) is None


@pytest.mark.parametrize(
    "claim",
    [
        "该研究分析了抑郁症患者自杀风险的影响因素。",
        "便秘是一种临床问题，以指导临床实践，但该摘要未提供便秘的具体定义或病理生理细节。",
        "炎症性肠病需要营养支持。",
        "炎症性肠病患者需要营养支持。",
    ],
)
def test_public_disease_claim_rejects_process_and_management_for_exact_target(claim):
    target = "抑郁症" if "抑郁症" in claim else "炎症性肠病"
    record = EvidenceRecord(
        evidence_key=claim,
        category="literature",
        title=f"{target} clinical context",
        source="Journal",
        targets=[target],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )
    assert _public_disease_claim(record) is None


def test_public_relation_description_rejects_vague_involvement_phrase():
    record = EvidenceRecord(
        evidence_key="vague-difficile",
        category="guideline",
        title="Clostridioides difficile infection guideline",
        source="Society",
        interpretation={
            "claim": "涉及艰难梭菌感染。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "direct",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-difficile",
                microbe_id="taxon-difficile",
                microbe_name="艰难梭菌",
                disease_id="disease-difficile",
                disease_name="艰难梭菌感染",
                claim="涉及艰难梭菌感染。",
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="艰难梭菌",
            disease_name="艰难梭菌感染",
            relation_type="established_pathogen",
        )
        == ""
    )


def test_public_relation_description_is_specific_to_each_disease_and_drops_study_detail():
    claim = (
        "肠沙门氏菌是肠热病和胃肠炎的病原体，"
        "与急性沙门菌病相关，"
        "肠道感染表现出比血液感染更大的遗传变异。"
    )
    record = EvidenceRecord(
        evidence_key="salmonella-multiple-outcomes",
        category="literature",
        title="Genetic diversity of Salmonella enterica during acute human infections",
        source="Journal",
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="salmonella-acute",
                microbe_id="taxon-salmonella",
                microbe_name="Salmonella enterica",
                microbe_aliases=["肠沙门氏菌"],
                disease_id="disease-acute-salmonellosis",
                disease_name="急性沙门菌病",
                claim=claim,
            ),
            EvidenceRelationBinding(
                relation_id="salmonella-enteric-fever",
                microbe_id="taxon-salmonella",
                microbe_name="Salmonella enterica",
                microbe_aliases=["肠沙门氏菌"],
                disease_id="disease-enteric-fever",
                disease_name="肠热病",
                claim=claim,
            ),
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门氏菌",
            disease_name="急性沙门菌病",
            relation_type="established_pathogen",
        )
        == "肠沙门氏菌与急性沙门菌病相关。"
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门氏菌",
            disease_name="肠热病",
            relation_type="established_pathogen",
        )
        == "肠沙门氏菌是肠热病的病原体。"
    )


def test_species_relation_description_rejects_unmeasured_virulence_gene_claim():
    record = EvidenceRecord(
        evidence_key="salmonella-cdtb",
        category="literature",
        title="Salmonella enterica CdtB in gastrointestinal disease",
        source="Journal",
        interpretation={
            "claim": (
                "携带CdtB编码基因的肠沙门菌在胃肠炎患者中检出，提示其与胃肠炎可能相关。"
            ),
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-salmonella-gastroenteritis",
                microbe_id="taxon-salmonella",
                microbe_name="Salmonella enterica",
                microbe_aliases=["肠沙门菌"],
                disease_id="disease-gastroenteritis",
                disease_name="胃肠炎",
                relation_type="established_pathogen",
                claim=(
                    "携带CdtB编码基因的肠沙门菌在胃肠炎患者中检出，"
                    "提示其与胃肠炎可能相关。"
                ),
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门菌",
            disease_name="胃肠炎",
            relation_type="established_pathogen",
        )
        == ""
    )

    record.interpretation.claim = "携带CdtB的肠沙门菌与胃肠炎相关。"
    record.relation_bindings[0].claim = "携带CdtB的肠沙门菌与胃肠炎相关。"
    assert (
        _public_relation_description(
            record,
            microbe_name="肠沙门菌",
            disease_name="胃肠炎",
            relation_type="established_pathogen",
        )
        == ""
    )


def test_non_tuberculous_mycobacterium_is_not_reported_as_tuberculosis():
    record = EvidenceRecord(
        evidence_key="kansasii-tuberculosis",
        category="literature",
        title="Mycobacterium kansasii and tuberculosis differential diagnosis",
        source="Journal",
        interpretation={
            "claim": "堪萨斯分枝杆菌与结核病相关，属于非结核分枝杆菌。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-kansasii-tb",
                microbe_id="taxon-kansasii",
                microbe_name="Mycobacterium kansasii",
                microbe_aliases=["堪萨斯分枝杆菌"],
                disease_id="disease-tuberculosis",
                disease_name="结核病",
                relation_type="epidemiologic_association",
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="堪萨斯分枝杆菌",
            disease_name="结核病",
            relation_type="epidemiologic_association",
        )
        == ""
    )


def test_species_relation_drops_unmeasured_subtype_clause_but_keeps_species_claim():
    record = EvidenceRecord(
        evidence_key="staph-mrsa",
        category="guideline",
        title="Staphylococcus aureus surgical site infection guideline",
        source="Society",
        interpretation={
            "claim": ("金黄色葡萄球菌是手术部位感染的重要病原体，包括耐甲氧西林菌株。"),
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="relation-staph-ssi",
                microbe_id="taxon-staph",
                microbe_name="Staphylococcus aureus",
                microbe_aliases=["金黄色葡萄球菌"],
                disease_id="disease-ssi",
                disease_name="手术部位感染",
                relation_type="established_pathogen",
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="金黄色葡萄球菌",
            disease_name="手术部位感染",
            relation_type="established_pathogen",
        )
        == "金黄色葡萄球菌是手术部位感染的重要病原体。"
    )


def test_public_disease_claim_keeps_definition_and_removes_management_tail():
    record = EvidenceRecord(
        evidence_key="crohn-definition",
        category="guideline",
        title="Crohn disease guideline",
        source="Society",
        targets=["克罗恩病"],
        interpretation={
            "claim": "克罗恩病是慢性炎症性肠病，支持克罗恩病的管理。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) == "克罗恩病是慢性炎症性肠病。"


def test_public_disease_claim_removes_editorial_progress_clauses():
    record = EvidenceRecord(
        evidence_key="constipation-editorial-progress",
        category="guideline",
        title="便秘外科诊治指南",
        source="Society",
        targets=["便秘"],
        interpretation={
            "claim": (
                "便秘是一种常见的功能性肠病，严重影响生活质量与心理健康，"
                "其病理机制认识不断深入，微创外科技术亦在进步。"
            ),
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) == (
        "便秘是一种常见的功能性肠病，严重影响生活质量与心理健康。"
    )
    record.interpretation.claim = (
        "便秘是一种常见的功能性肠病，可严重影响生活质量与心理健康，"
        "其病理机制的认识不断深入。"
    )
    assert _public_disease_claim(record) == (
        "便秘是一种常见的功能性肠病，可严重影响生活质量与心理健康。"
    )


def test_public_disease_claim_requires_the_target_condition_in_the_claim():
    record = EvidenceRecord(
        evidence_key="wrong-cardiovascular-target",
        category="literature",
        title="Cardiovascular risk factors",
        source="Journal",
        targets=["冠心病"],
        interpretation={
            "claim": "2型糖尿病与心血管疾病风险因素相关。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) is None


def test_public_disease_claim_rejects_an_unrelated_patient_cohort():
    record = EvidenceRecord(
        evidence_key="tinnitus-sleep",
        category="literature",
        title="Sleep problems in tinnitus patients",
        source="Journal",
        targets=["睡眠障碍"],
        interpretation={
            "claim": "睡眠障碍在耳鸣患者中较常见，二者相互影响。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) is None


def test_public_relation_description_removes_source_process_wording():
    record = EvidenceRecord(
        evidence_key="botulism-review",
        category="literature",
        title="Clostridium botulinum and botulism",
        source="Journal",
        interpretation={
            "claim": (
                "该综述描述了肉毒梭菌的致病性和毒力，"
                "强调其神经毒素是导致肉毒中毒的主要因素，"
                "并总结了孢子形成在传播中的作用。"
            ),
            "targetAlignment": "direct",
            "taxonAlignment": "species",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="botulism",
                microbe_id="taxon-botulinum",
                microbe_name="Clostridium botulinum",
                disease_id="disease-botulism",
                disease_name="肉毒中毒",
            )
        ],
    )
    assert (
        _public_relation_description(
            record,
            microbe_name="肉毒梭菌",
            disease_name="肉毒中毒",
            relation_type="established_pathogen",
        )
        == "肉毒梭菌的致病性和毒力，肉毒梭菌神经毒素是导致肉毒中毒的主要因素。"
    )

    record.interpretation.claim = (
        "临床诊疗指南——传染病学分册》包括猪链球菌病，表明猪链球菌与猪链球菌病存在关联。"
    )
    record.relation_bindings[0].microbe_name = "Streptococcus suis"
    record.relation_bindings[0].disease_name = "猪链球菌病"
    assert (
        _public_relation_description(
            record,
            microbe_name="猪链球菌",
            disease_name="猪链球菌病",
            relation_type="established_pathogen",
        )
        == "猪链球菌与猪链球菌病存在关联。"
    )


def test_patient_facing_condition_aliases_are_merged():
    assert _canonical_condition_name("志贺菌病") == "细菌性痢疾"
    assert _canonical_condition_name("肺炎链球菌感染") == "肺炎球菌病"
    assert _canonical_condition_name("肺炎链球菌性肺炎") == "肺炎"
    assert _canonical_condition_name("细菌性脑膜炎") == "脑膜炎"
    assert _canonical_condition_name("伪膜性肠炎") == "假膜性肠炎"
    assert _canonical_condition_name("大肠癌") == "结直肠癌"


def test_direct_clinical_condition_requires_exact_relation_search():
    record = EvidenceRecord(
        evidence_key="campy-enteritis",
        category="literature",
        title="Campylobacter coli causes food-borne enteritis",
        source="Journal",
        microbe_ids=["taxon-campy"],
        intents=["microbe_clinical_significance"],
        targets=["Campylobacter coli / 结肠弯曲菌"],
        interpretation={
            "claim": "结肠弯曲菌可引起食源性肠炎。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "食源性肠炎",
                    "searchTerm": "food-borne enteritis",
                    "sourcePhrase": "food-borne enteritis",
                    "relationType": "established_pathogen",
                }
            ],
        },
    )
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-campy",
            category="pathogenic",
            latin_name="Campylobacter coli",
            value="0.1",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    queries = make_candidate_queries(microbes, [record], age=28)
    assert len(queries) == 3
    assert {item.relation_disease_name for item in queries} == {
        "食源性肠炎",
        "感染性腹泻",
        "胃肠炎",
    }
    assert {item.query for item in queries} == {
        "Campylobacter coli food-borne enteritis 食源性肠炎",
        "Campylobacter coli diarrhoeal disease",
        "Campylobacter coli gastroenteritis",
    }
    assert record.relation_bindings == []
    assert "microbe_disease_association" not in record.intents


def test_exact_relation_queries_screen_with_disease_spelling_aliases():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-campy",
            category="pathogenic",
            latin_name="Campylobacter coli",
            value="0.1",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)

    queries = make_candidate_queries(microbes, [], age=28)

    diarrhoea_query = next(
        item for item in queries if item.relation_disease_name == "感染性腹泻"
    )
    assert diarrhoea_query.query == "Campylobacter coli diarrhoeal disease"
    assert "diarrhoeal disease" in diarrhoea_query.required_concepts[1]
    assert "diarrheal disease" in diarrhoea_query.required_concepts[1]


def test_exact_pathogen_queries_are_bounded_to_human_disease_context():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-shigella-dysenteriae",
            category="pathogenic",
            latin_name="Shigella dysenteriae",
            value="0.1",
            detection_status="detected",
        ),
        ParsedTaxon(
            taxon_id="taxon-aeromonas-hydrophila",
            category="opportunistic",
            latin_name="Aeromonas hydrophila",
            value="0.1",
            detection_status="detected",
        ),
    ]
    microbes = build_abnormal_microbes(parsed)

    queries = make_candidate_queries(microbes, [], age=28)

    query_texts = {item.query for item in queries}
    assert "Shigella dysenteriae causes shigellosis humans" in query_texts
    assert "Aeromonas hydrophila causes human gastroenteritis adults" in query_texts


def test_taxon_relation_hints_only_seed_exact_evimed_queries():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-flexneri",
            category="pathogenic",
            latin_name="Shigella flexneri",
            value="0.1",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)

    queries = make_candidate_queries(microbes, [], age=28)

    assert len(queries) == 1
    assert queries[0].query == "Shigella flexneri bacillary dysentery"
    assert queries[0].relation_disease_name == "细菌性痢疾"


def test_exact_source_relation_recovers_when_ai_interpretation_is_missing():
    binding = EvidenceRelationBinding(
        relationId="relation-plesiomonas-gastroenteritis",
        microbeId="taxon-plesiomonas",
        microbeName="Plesiomonas shigelloides",
        microbeAliases=["类志贺邻单胞菌"],
        diseaseId="disease-gastroenteritis",
        diseaseName="胃肠炎",
        relationType="established_pathogen",
    )
    record = EvidenceRecord(
        evidenceKey="plesiomonas-review",
        category="literature",
        title="Plesiomonas shigelloides associated with human gastroenteritis",
        source="Clinical Microbiology Reviews",
        studyTypes=["Review"],
        relationBindings=[binding],
    )

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.taxon_alignment == "species"
    assert record.interpretation.patient_applicability == "contextual"
    assert record.interpretation.claim == "类志贺邻单胞菌与胃肠炎相关。"
    assert record.relation_bindings[0].claim == "类志贺邻单胞菌与胃肠炎相关。"


def test_exact_source_relation_recovers_incomplete_ai_alignment():
    record = EvidenceRecord(
        evidenceKey="aeromonas-review",
        category="literature",
        title="Aeromonas caviae associated with human gastroenteritis",
        source="Clinical Review",
        studyTypes=["Review"],
        interpretation={
            "claim": "The source was not interpreted.",
            "targetAlignment": "not_applicable",
            "taxonAlignment": "unclear",
            "patientApplicability": "unclear",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-aeromonas-gastroenteritis",
                microbeId="taxon-aeromonas",
                microbeName="Aeromonas caviae",
                microbeAliases=["豚鼠气单胞菌"],
                diseaseId="disease-gastroenteritis",
                diseaseName="胃肠炎",
                relationType="opportunistic",
            )
        ],
    )

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation.target_alignment == "direct"
    assert record.interpretation.taxon_alignment == "species"
    assert record.relation_bindings[0].claim == "豚鼠气单胞菌与胃肠炎相关。"


def test_exact_human_stool_relation_recovers_singular_kidney_stone_term():
    record = EvidenceRecord(
        evidenceKey="human-stool-stone-relation",
        category="literature",
        title=(
            "Testibacter intestinalis may reduce the risk of calcium oxalate "
            "kidney stone formation"
        ),
        source="Clinical cohort journal",
        summary=(
            "A case-control study of 200 adults measured Testibacter "
            "intestinalis colonization by stool culture. The organism was "
            "less prevalent in recurrent calcium oxalate kidney stone "
            "patients, and absence of colonization was associated with "
            "higher risk."
        ),
        studyTypes=["Case-Control Study"],
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-testibacter-stone",
                microbeId="taxon-testibacter",
                microbeName="Testibacter intestinalis",
                microbeAliases=["测试肠菌"],
                diseaseId="disease-stone",
                diseaseName="肾结石",
                relationType="epidemiologic_association",
            )
        ],
    )

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation is not None
    assert record.interpretation.direction == "decreased"
    assert record.interpretation.sample_alignment == "stool"
    assert record.relation_bindings[0].claim == "测试肠菌与肾结石相关。"


def test_relation_description_rejects_incomplete_locative_fragment():
    record = EvidenceRecord(
        evidenceKey="pseudomonas-bsi-fragment",
        category="literature",
        title="Pseudomonas aeruginosa bloodstream infection",
        source="Journal",
        summary="Pseudomonas aeruginosa was associated with bloodstream infection.",
        interpretation={
            "claim": "铜绿假单胞菌血流感染中。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-pa-bsi",
                microbeId="taxon-pa",
                microbeName="Pseudomonas aeruginosa",
                microbeAliases=["铜绿假单胞菌"],
                diseaseId="disease-bsi",
                diseaseName="血流感染",
                relationType="established_pathogen",
                claim="铜绿假单胞菌血流感染中。",
            )
        ],
    )

    assert _public_relation_description(
        record,
        microbe_name="铜绿假单胞菌",
        disease_name="血流感染",
        relation_type="established_pathogen",
    ) == "铜绿假单胞菌与血流感染相关。"


def test_binding_validation_recovers_direct_but_disease_omitting_ai_claim():
    record = EvidenceRecord(
        evidenceKey="plesiomonas-generic-role",
        category="literature",
        title="Plesiomonas shigelloides associated with human gastroenteritis",
        source="Clinical Microbiology Reviews",
        studyTypes=["Review"],
        interpretation={
            "claim": "类志贺邻单胞菌具有病原学意义。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-plesiomonas-gastroenteritis",
                microbeId="taxon-plesiomonas",
                microbeName="Plesiomonas shigelloides",
                microbeAliases=["类志贺邻单胞菌"],
                diseaseId="disease-gastroenteritis",
                diseaseName="胃肠炎",
                relationType="established_pathogen",
            )
        ],
    )

    # The normal pipeline calls validation immediately after model parsing.
    # Source-bounded recovery must therefore happen before validation removes
    # an otherwise valid exact-query binding.
    validate_relation_bindings([record])

    assert record.interpretation.claim == "类志贺邻单胞菌与胃肠炎相关。"
    assert len(record.relation_bindings) == 1
    assert record.relation_bindings[0].claim == "类志贺邻单胞菌与胃肠炎相关。"


def test_exact_relation_recovery_keeps_abbreviated_latin_species_sentence():
    record = EvidenceRecord(
        evidenceKey="campylobacter-abbreviated",
        category="literature",
        title="The physiology of Campylobacter species",
        source="Clinical review",
        summary=(
            "Campylobacter jejuni and C. coli are recognised as leading causes "
            "of bacterial foodborne diarrhoeal disease worldwide."
        ),
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-campylobacter-diarrhea",
                microbeId="taxon-campylobacter",
                microbeName="Campylobacter coli",
                microbeAliases=["结肠弯曲菌"],
                diseaseId="disease-infectious-diarrhea",
                diseaseName="感染性腹泻",
                relationType="established_pathogen",
            )
        ],
    )

    validate_relation_bindings([record])

    assert record.interpretation is not None
    assert record.interpretation.claim == "结肠弯曲菌与感染性腹泻相关。"
    assert len(record.relation_bindings) == 1


def test_high_risk_hematology_relation_is_not_generalized_to_stool_report():
    record = EvidenceRecord(
        evidenceKey="fusarium-hematology",
        category="literature",
        title="Fusarium skin lesions in high-risk hematology patients",
        source="Journal",
        summary=(
            "Fusarium skin lesions were associated with invasive fusariosis "
            "in high-risk hematology patients."
        ),
        interpretation={
            "claim": "高危血液病患者的皮肤病灶与侵袭性镰刀菌病相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "genus",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-fusarium",
                microbeId="taxon-fusarium",
                microbeName="Fusarium",
                microbeAliases=["镰刀菌属"],
                diseaseId="disease-fusariosis",
                diseaseName="镰刀菌病",
                relationType="opportunistic",
                claim="Fusarium was associated with invasive fusariosis.",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="镰刀菌属",
            disease_name="镰刀菌病",
            relation_type="opportunistic",
        )
        == ""
    )


def test_loose_source_comention_cannot_recover_missing_ai_interpretation():
    record = EvidenceRecord(
        evidenceKey="loose-comention",
        category="literature",
        title="Microbiome profiling in gastroenteritis patients",
        source="Journal",
        summary="Plesiomonas shigelloides was one of many detected organisms.",
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-loose",
                microbeId="taxon-plesiomonas",
                microbeName="Plesiomonas shigelloides",
                diseaseId="disease-gastroenteritis",
                diseaseName="胃肠炎",
                relationType="established_pathogen",
            )
        ],
    )

    recover_exact_relation_interpretations([record])

    assert record.interpretation is None


@pytest.mark.parametrize(
    ("scientific_name", "expected_conditions"),
    (
        ("Pseudomonas aeruginosa", {"肺炎", "血流感染"}),
        ("Candida albicans", {"念珠菌病"}),
        ("Aspergillus", {"曲霉病"}),
    ),
)
def test_common_pathogen_hints_only_create_exact_evidence_queries(
    scientific_name: str,
    expected_conditions: set[str],
):
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id=f"taxon-{stable_id('hint', scientific_name)}",
            category=(
                "fungi_pathogenic"
                if scientific_name != "Pseudomonas aeruginosa"
                else "opportunistic"
            ),
            latin_name=scientific_name,
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)

    queries = make_candidate_queries(microbes, [], age=28)

    assert {query.relation_disease_name for query in queries} == expected_conditions
    assert all(query.intent == "microbe_disease_association" for query in queries)
    assert all(query.relation_id for query in queries)
    assert queries[0].screen_profile == "human_relation"
    # A nomenclature hint is a search seed, not public evidence or a relation.
    assert microbes.items[0].disease_and_risks == []


def test_descriptive_metagenome_cooccurrence_is_not_causal_relation_evidence():
    query = EvidenceQuery(
        query="Veillonella parvula osteomyelitis 骨髓炎",
        supports=("taxon-vp",),
        required_concepts=(("Veillonella parvula",), ("osteomyelitis", "骨髓炎")),
        intent="microbe_disease_association",
        target="Veillonella parvula / 小韦荣球菌 / 骨髓炎",
        microbe_id="taxon-vp",
        disease_id="disease-osteomyelitis",
        relation_id="relation-vp-osteomyelitis",
        relation_disease_name="骨髓炎",
        relation_source_taxon="Veillonella parvula",
        relation_type="established_pathogen",
        screen_profile="human_relation",
    )
    descriptive = EvidenceRecord(
        evidence_key="foot-microbiome",
        category="literature",
        title=(
            "Microbiome analysis of diabetic foot osteomyelitis by "
            "metagenome sequencing technology"
        ),
        source="Journal",
        summary=(
            "Veillonella parvula was among the organisms detected in patient samples."
        ),
    )
    associated = descriptive.model_copy(
        update={
            "evidence_key": "foot-microbiome-associated",
            "summary": (
                "Veillonella parvula abundance was associated with "
                "osteomyelitis in patient samples."
            ),
        }
    )

    assert _screen(descriptive, query=query) == (
        False,
        "descriptive_microbiome_only",
    )
    assert _screen(associated, query=query)[0] is True


def test_exact_taxon_hint_has_priority_over_looser_discovery_conditions():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-flexneri",
            category="pathogenic",
            latin_name="Shigella flexneri",
            value="0.1",
            detection_status="detected",
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    discovery = EvidenceRecord(
        evidence_key="flexneri-discovery",
        category="literature",
        title="Clinical outcomes of Shigella flexneri",
        source="Journal",
        microbeIds=["taxon-flexneri"],
        intents=["microbe_discovery"],
        interpretation={
            "claim": "Human outcomes were reported for Shigella flexneri.",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "志贺菌病",
                    "searchTerm": "shigellosis",
                    "sourcePhrase": "shigellosis",
                    "relationType": "established_pathogen",
                },
                {
                    "name": "水样腹泻",
                    "searchTerm": "watery diarrhea",
                    "sourcePhrase": "watery diarrhea",
                    "relationType": "established_pathogen",
                },
                {
                    "name": "痢疾",
                    "searchTerm": "dysentery",
                    "sourcePhrase": "dysentery",
                    "relationType": "established_pathogen",
                },
            ],
        },
    )

    queries = make_candidate_queries(microbes, [discovery], age=28)

    # Exact verification follows the same three-candidate cap as the public
    # row; a fourth loose candidate must not consume report time.
    assert len(queries) == 3
    assert queries[0].query == "Shigella flexneri bacillary dysentery"
    assert "细菌性痢疾" in {item.relation_disease_name for item in queries}
    assert microbes.items[0].disease_and_risks == []


def test_salmonella_exact_search_prioritizes_common_human_diseases():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-salmonella",
            category="pathogenic",
            latin_name="Salmonella enterica",
            value="0.0001",
            detection_status="trace",
        )
    ]
    microbes = build_abnormal_microbes(parsed)

    queries = make_candidate_queries(microbes, [], age=42)

    assert [item.query for item in queries] == [
        "Salmonella enterica infection",
        "Salmonella enterica gastroenteritis",
        "Salmonella enterica enteric fever",
    ]
    assert all(item.screen_profile == "human_relation" for item in queries)
    assert queries[0].required_concepts[1] == (
        "沙门菌病",
        "沙门氏菌病",
        "salmonellosis",
        "肠热病",
        "肠热症",
        "enteric fever",
        "胃肠炎",
        "gastroenteritis",
        "infection",
    )


def test_current_report_taxa_use_exact_source_verification_queries():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-st",
            category="beneficial",
            latin_name="Streptococcus thermophilus",
            chinese_name="嗜热链球菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-cb",
            category="beneficial",
            latin_name="Clostridium butyricum",
            chinese_name="丁酸梭菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-of",
            category="beneficial",
            latin_name="Oxalobacter formigenes",
            chinese_name="草酸杆菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-ba",
            category="beneficial",
            latin_name="Bifidobacterium adolescentis",
            chinese_name="青春双歧杆菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-pv",
            category="other",
            latin_name="Phocaeicola vulgatus",
            chinese_name="普通拟杆菌",
            source_status="检出",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-vp",
            category="other",
            latin_name="Veillonella parvula",
            chinese_name="小韦荣球菌",
            source_status="检出",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-pp",
            category="opportunistic",
            latin_name="Pseudomonas putida",
            chinese_name="恶臭假单胞菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-salmonella",
            category="pathogenic",
            latin_name="Salmonella",
            chinese_name="沙门氏菌",
            source_status="检出",
            explicitly_abnormal=True,
        ),
        ParsedTaxon(
            taxon_id="taxon-mk",
            category="pathogenic",
            latin_name="Mycobacterium kansasii",
            chinese_name="堪萨斯分枝杆菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        ),
    ]
    microbes = build_abnormal_microbes(parsed)

    primary_discovery = {
        query.microbe_id
        for query in make_queries(microbes, DiseaseRisksSection(), age=28)
        if query.intent == "microbe_discovery"
    }
    assert "taxon-pp" not in primary_discovery
    assert "taxon-mk" not in primary_discovery
    assert "taxon-salmonella" in primary_discovery

    queries = make_candidate_queries(microbes, [], age=28)
    by_taxon = {
        item.scientific_name: [
            query for query in queries if query.microbe_id == item.microbe_id
        ]
        for item in microbes.items
    }

    assert [query.query for query in by_taxon["Streptococcus thermophilus"]] == [
        "Streptococcus thermophilus depleted colorectal cancer"
    ]
    assert [query.query for query in by_taxon["Clostridium butyricum"]] == [
        "Clostridium butyricum depleted inflammatory bowel disease"
    ]
    assert [query.query for query in by_taxon["Oxalobacter formigenes"]] == [
        "Oxalobacter formigenes colonization risk calcium oxalate stones adults"
    ]
    assert [query.query for query in by_taxon["Bifidobacterium adolescentis"]] == [
        "Bifidobacterium adolescentis depleted inflammatory bowel disease"
    ]
    assert [query.query for query in by_taxon["Phocaeicola vulgatus"]] == [
        (
            "Multi-omics analyses ulcerative colitis Bacteroides vulgatus "
            "proteases disease severity"
        )
    ]
    assert [query.query for query in by_taxon["Veillonella parvula"]] == [
        "Veillonella parvula colorectal cancer microbiome"
    ]
    assert by_taxon["Pseudomonas putida"] == []
    assert [query.query for query in by_taxon["Salmonella"]] == [
        "Salmonella infection",
        "Salmonella gastroenteritis",
        "Salmonella bacteremia",
    ]
    assert by_taxon["Mycobacterium kansasii"] == []
    assert all(
        query.screen_profile == "gut_association"
        for scientific_name in (
            "Streptococcus thermophilus",
            "Clostridium butyricum",
            "Oxalobacter formigenes",
            "Bifidobacterium adolescentis",
        )
        for query in by_taxon[scientific_name]
    )
    assert all(
        query.screen_profile == "human_relation"
        for scientific_name in (
            "Salmonella",
            "Phocaeicola vulgatus",
            "Veillonella parvula",
        )
        for query in by_taxon[scientific_name]
    )


def test_non_supportive_raw_binding_does_not_suppress_exact_fallback_query():
    parsed = parsed_report()
    parsed.taxa = [
        ParsedTaxon(
            taxon_id="taxon-of",
            category="beneficial",
            latin_name="Oxalobacter formigenes",
            chinese_name="草酸杆菌",
            source_status="丰度低",
            explicitly_abnormal=True,
        )
    ]
    microbes = build_abnormal_microbes(parsed)
    non_supportive = EvidenceRecord(
        evidenceKey="oxalobacter-no-significant-association",
        category="literature",
        title="Oxalobacter formigenes and kidney stone formation",
        source="Journal",
        summary=(
            "Oxalobacter formigenes colonization was not significantly "
            "associated with nephrolithiasis."
        ),
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "草酸杆菌定植与肾结石无显著关联。",
            "direction": "not_stated",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-of-stone",
                microbeId="taxon-of",
                microbeName="Oxalobacter formigenes",
                microbeAliases=["草酸杆菌"],
                diseaseId="disease-stone",
                diseaseName="肾结石",
                relationType="epidemiologic_association",
                targetAlignment="direct",
                taxonAlignment="species",
                patientApplicability="contextual",
            )
        ],
    )

    queries = make_candidate_queries(microbes, [non_supportive], age=57)

    assert [query.query for query in queries] == [
        "Oxalobacter formigenes colonization risk calcium oxalate stones adults"
    ]


def test_low_abundance_direction_can_come_from_retained_source_text():
    parsed = parsed_report()
    parsed.taxa[0].category = "beneficial"
    parsed.taxa[0].latin_name = "Streptococcus thermophilus"
    parsed.taxa[0].chinese_name = "嗜热链球菌"
    parsed.taxa[0].source_status = "丰度低"
    parsed.taxa[0].explicitly_abnormal = True
    item = build_abnormal_microbes(parsed).items[0]
    record = EvidenceRecord(
        evidenceKey="thermophilus-crc",
        category="literature",
        title="Streptococcus thermophilus is depleted in colorectal cancer",
        source="Journal",
        summary=(
            "Streptococcus thermophilus was depleted in patients with "
            "colorectal cancer in multicohort fecal metagenomic data."
        ),
        interpretation={
            "claim": "嗜热链球菌与结直肠癌相关。",
            "direction": "decreased",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )

    assert _abundance_relation_matches_source_direction(
        item=item,
        record=record,
        description="嗜热链球菌与结直肠癌相关。",
    )
    item.source_finding.status = "丰度高"
    assert not _abundance_relation_matches_source_direction(
        item=item,
        record=record,
        description="嗜热链球菌与结直肠癌相关。",
    )


@pytest.mark.parametrize(
    ("query", "record"),
    (
        (
            EvidenceQuery(
                query="Streptococcus thermophilus depleted colorectal cancer",
                supports=("taxon-st",),
                required_concepts=(
                    ("Streptococcus thermophilus", "嗜热链球菌"),
                    ("结直肠癌", "colorectal cancer"),
                ),
                intent="microbe_disease_association",
                target="Streptococcus thermophilus / 嗜热链球菌 / 结直肠癌",
                relation_disease_id="disease-crc",
                relation_disease_name="结直肠癌",
                relation_source_taxon="Streptococcus thermophilus",
                microbe_id="taxon-st",
                disease_id="disease-crc",
                relation_id="relation-st-crc",
                screen_profile="gut_association",
            ),
            EvidenceRecord(
                evidenceKey="mixed-human-mouse-st",
                category="literature",
                title=(
                    "Streptococcus thermophilus Inhibits Colorectal "
                    "Tumorigenesis Through Secreting beta-Galactosidase"
                ),
                source="Journal",
                summary=(
                    "Streptococcus thermophilus was identified to be depleted "
                    "in patients with colorectal cancer by shotgun metagenomic "
                    "sequencing of 526 multicohort fecal samples. Its mechanism "
                    "was then assessed in cultured epithelial cells and mice."
                ),
            ),
        ),
        (
            EvidenceQuery(
                query="Clostridium butyricum depleted inflammatory bowel disease",
                supports=("taxon-cb",),
                required_concepts=(
                    ("Clostridium butyricum", "丁酸梭菌"),
                    ("炎症性肠病", "inflammatory bowel disease", "IBD"),
                ),
                intent="microbe_disease_association",
                target="Clostridium butyricum / 丁酸梭菌 / 炎症性肠病",
                relation_disease_id="disease-ibd",
                relation_disease_name="炎症性肠病",
                relation_source_taxon="Clostridium butyricum",
                microbe_id="taxon-cb",
                disease_id="disease-ibd",
                relation_id="relation-cb-ibd",
                screen_profile="gut_association",
            ),
            EvidenceRecord(
                evidenceKey="review-cb-ibd",
                category="literature",
                title="Positive role of Clostridium in autoimmune diseases",
                source="Journal",
                studyTypes=["Review"],
                summary=(
                    "Studies show that Clostridium butyricum is almost depleted "
                    "in multiple autoimmune diseases. This depletion is associated "
                    "with impaired intestinal barrier homeostasis. In diseases "
                    "such as inflammatory bowel disease, restoration of gut "
                    "Clostridium species is under study."
                ),
            ),
        ),
    ),
)
def test_source_bound_human_abundance_survives_mixed_or_review_context(
    query,
    record,
):
    assert _screen(record, query=query) == (True, "retained")


def test_exact_human_depletion_is_recovered_from_mixed_mechanism_source():
    record = EvidenceRecord(
        evidenceKey="mixed-human-mouse-st-recovery",
        category="literature",
        title=(
            "Streptococcus thermophilus Inhibits Colorectal "
            "Tumorigenesis Through Secreting beta-Galactosidase"
        ),
        source="Journal",
        summary=(
            "Streptococcus thermophilus was identified to be depleted "
            "in patients with colorectal cancer by shotgun metagenomic "
            "sequencing of 526 multicohort fecal samples. Oral gavage "
            "increased its gut abundance in mice and inhibited tumor growth."
        ),
        intents=["microbe_disease_association"],
        studyTypes=["Human cohort", "Animal experiment"],
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-st-crc",
                microbeId="taxon-st",
                microbeName="Streptococcus thermophilus",
                microbeAliases=["嗜热链球菌"],
                diseaseId="disease-crc",
                diseaseName="结直肠癌",
                relationType="epidemiologic_association",
            )
        ],
    )

    recover_exact_relation_interpretations([record])

    assert record.interpretation is not None
    assert record.interpretation.direction == "decreased"
    assert record.relation_bindings[0].claim == "嗜热链球菌与结直肠癌相关。"
    assert not is_preclinical_evidence(record)


def test_recent_unindexed_human_depletion_survives_mixed_animal_mechanism():
    record = EvidenceRecord(
        evidenceKey="mixed-human-mouse-b-adolescentis-recovery",
        category="literature",
        title=(
            "Genome analysis of Bifidobacterium adolescentis and "
            "investigation of its effects on inflammation and intestinal "
            "barrier function"
        ),
        source="Journal",
        summary=(
            "We designed cohort studies and animal experiments. The "
            "depletion of Bifidobacterium adolescentis in individuals with "
            "inflammatory bowel disease suggested its significance for "
            "intestinal health. The strain was then studied in a mouse "
            "colitis model."
        ),
        intents=["microbe_disease_association"],
        studyTypes=["Journal Article"],
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-ba-ibd",
                microbeId="taxon-ba",
                microbeName="Bifidobacterium adolescentis",
                microbeAliases=["青春双歧杆菌"],
                diseaseId="disease-ibd",
                diseaseName="炎症性肠病",
                relationType="epidemiologic_association",
            )
        ],
    )
    query = EvidenceQuery(
        "Bifidobacterium adolescentis depleted inflammatory bowel disease",
        ("taxon-ba",),
        (
            ("Bifidobacterium adolescentis",),
            ("炎症性肠病", "inflammatory bowel disease"),
        ),
        intent="microbe_disease_association",
        target="Bifidobacterium adolescentis / 青春双歧杆菌",
        screen_profile="gut_association",
        relation_type="epidemiologic_association",
    )

    assert _screen(record, query=query) == (True, "retained")

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation is not None
    assert record.interpretation.direction == "decreased"
    assert record.relation_bindings[0].claim == "青春双歧杆菌与炎症性肠病相关。"
    assert not is_preclinical_evidence(record)


def test_human_cohort_link_and_overabundance_recovers_vulgatus_relation():
    record = EvidenceRecord(
        evidenceKey="human-vulgatus-uc-recovery",
        category="literature",
        title=(
            "Multi-omics analyses of the ulcerative colitis gut microbiome "
            "link Bacteroides vulgatus proteases with disease severity"
        ),
        source="Journal",
        summary=(
            "We analysed faecal samples from 40 ulcerative colitis patients "
            "and an independent validation cohort. Active patients had an "
            "overabundance of proteases from Bacteroides vulgatus. Mouse "
            "experiments subsequently tested the mechanism."
        ),
        intents=["microbe_disease_association"],
        studyTypes=["Journal Article"],
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-pv-uc",
                microbeId="taxon-pv",
                microbeName="Phocaeicola vulgatus",
                microbeAliases=["普通拟杆菌"],
                diseaseId="disease-uc",
                diseaseName="溃疡性结肠炎",
                relationType="epidemiologic_association",
            )
        ],
    )

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation is not None
    assert record.interpretation.direction == "increased"
    assert record.relation_bindings[0].claim == "普通拟杆菌与溃疡性结肠炎相关。"
    assert not is_preclinical_evidence(record)


def test_short_disease_acronym_requires_word_boundaries():
    assert _claim_names_disease("The UC cohort included adult patients.", "溃疡性结肠炎")
    assert not _claim_names_disease("Mucus barrier function was measured.", "溃疡性结肠炎")


def test_mycobacterium_kansasii_pulmonary_disease_english_alias_is_bound():
    assert _claim_names_disease(
        "Mycobacterium kansasii is a cause of NTM pulmonary disease.",
        "非结核分枝杆菌肺病",
    )


def test_mycobacterium_kansasii_disease_entity_recovers_exact_relation():
    record = EvidenceRecord(
        evidenceKey="kansasii-pulmonary-disease",
        category="literature",
        title="Mycobacterium kansasii",
        source="Journal",
        summary=(
            "The majority of patients with Mycobacterium kansasii pulmonary "
            "disease have underlying pulmonary comorbidities."
        ),
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "该文献介绍堪萨斯分枝杆菌的临床特征。",
            "targetAlignment": "indirect",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="kansasii-pulmonary-binding",
                microbeId="taxon-mk",
                microbeName="Mycobacterium kansasii",
                microbeAliases=["堪萨斯分枝杆菌"],
                diseaseId="disease-ntm-pd",
                diseaseName="非结核分枝杆菌肺病",
                relationType="established_pathogen",
            )
        ],
    )

    assert _source_explicitly_supports_relation(
        record,
        record.relation_bindings[0],
    )
    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])
    assert record.relation_bindings
    assert record.interpretation.target_alignment == "direct"


def test_recovered_exact_relation_preserves_explicit_faecal_sample_site():
    record = EvidenceRecord(
        evidenceKey="salmonella-faecal-cohort",
        category="literature",
        title="Nontyphoidal Salmonella gastroenteritis in adults",
        source="Journal",
        summary=(
            "Adults with faecal culture confirmed Salmonella gastroenteritis "
            "were included in this cohort."
        ),
        intents=["microbe_disease_association"],
        interpretation={
            "claim": "该研究讨论成人胃肠炎。",
            "targetAlignment": "indirect",
            "taxonAlignment": "genus",
            "sampleAlignment": "unclear",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="salmonella-gastroenteritis",
                microbeId="taxon-salmonella",
                microbeName="Salmonella",
                microbeAliases=["沙门氏菌"],
                diseaseId="disease-gastroenteritis",
                diseaseName="胃肠炎",
                relationType="established_pathogen",
            )
        ],
    )

    recover_exact_relation_interpretations([record])
    validate_relation_bindings([record])

    assert record.interpretation.sample_alignment == "stool"
    assert record.relation_bindings[0].sample_alignment == "stool"


def test_relation_binding_is_removed_when_claim_names_another_disease():
    record = EvidenceRecord(
        evidence_key="mismatched-condition",
        category="literature",
        title="Fusobacterium nucleatum and multiple sclerosis",
        source="Journal",
        interpretation={
            "claim": "具核梭杆菌与多发性硬化严重程度相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="wrong-periodontal-binding",
                microbe_id="taxon-fn",
                microbe_name="Fusobacterium nucleatum",
                disease_id="disease-periodontal",
                disease_name="牙周病",
            )
        ],
    )
    validate_relation_bindings([record])
    assert record.relation_bindings == []


def test_human_relation_does_not_require_gut_phrase_and_gut_profile_accepts_stool():
    human_record = EvidenceRecord(
        evidence_key="human-relation",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer in patients",
        source="Journal",
        summary="A human cohort found an association with colorectal cancer.",
    )
    human_query = EvidenceQuery(
        query="Fusobacterium nucleatum colorectal cancer",
        supports=("taxon-fn",),
        required_concepts=(
            ("Fusobacterium nucleatum",),
            ("colorectal cancer",),
        ),
        intent="microbe_disease_association",
        screen_profile="human_relation",
    )
    assert _screen(human_record, query=human_query)[0] is True

    stool_record = human_record.model_copy(
        update={
            "evidence_key": "stool-relation",
            "summary": (
                "A human stool cohort found Fusobacterium nucleatum "
                "associated with colorectal cancer."
            ),
        }
    )
    stool_query = EvidenceQuery(
        query="Fusobacterium nucleatum colorectal cancer stool",
        supports=("taxon-fn",),
        required_concepts=(
            ("Fusobacterium nucleatum",),
            ("colorectal cancer",),
        ),
        intent="microbe_disease_association",
        screen_profile="gut_association",
    )
    assert _screen(stool_record, query=stool_query)[0] is True


def test_microbe_risks_are_ranked_and_limited_to_three():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    records = []
    for index, (name, relation_type, category) in enumerate(
        (
            ("结直肠癌", "epidemiologic_association", "literature"),
            ("菌血症", "established_pathogen", "guideline"),
            ("肠道定植", "colonization", "literature"),
            ("炎症性肠病", "epidemiologic_association", "literature"),
        ),
        start=1,
    ):
        records.append(
            EvidenceRecord(
                evidence_key=f"relation-{index}",
                category=category,
                title=f"Fusobacterium nucleatum and {name}",
                source="Journal",
                relevance=1,
                relation_bindings=[
                    EvidenceRelationBinding(
                        relation_id=f"binding-{index}",
                        microbe_id="taxon-fn",
                        microbe_name="Fusobacterium nucleatum",
                        disease_id=f"disease-{index}",
                        disease_name=name,
                        relation_type=relation_type,
                    )
                ],
                interpretation={
                    "claim": f"Fusobacterium nucleatum was associated with {name}.",
                    "targetAlignment": "direct",
                    "taxonAlignment": "species",
                    "sampleAlignment": "stool",
                    "patientApplicability": "direct",
                    "confidence": "moderate",
                },
            )
        )
    numbers = {
        record.evidence_key: index for index, record in enumerate(records, start=1)
    }
    attach_evidence_refs(microbes, diseases, records, numbers)
    risks = microbes.items[0].disease_and_risks
    assert len(risks) == 3
    assert risks[0].name == "菌血症"
    assert all(risk.evidence_refs for risk in risks)


def test_direct_discovery_condition_is_not_lost_when_second_search_is_empty():
    microbes = build_abnormal_microbes(parsed_report())
    record = EvidenceRecord(
        evidence_key="direct-discovery",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer",
        source="Journal",
        summary=(
            "A human cohort found Fusobacterium nucleatum associated with "
            "colorectal cancer."
        ),
        intents=["microbe_discovery"],
        microbe_ids=["taxon-fn"],
        interpretation={
            "claim": "人体队列记录具核梭杆菌与结直肠癌相关。",
            "studyDesign": "队列研究",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "stool",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "结直肠癌",
                    "searchTerm": "colorectal cancer",
                    "sourcePhrase": "colorectal cancer",
                    "relationType": "epidemiologic_association",
                }
            ],
        },
    )
    promote_discovery_conditions_to_bindings([record], microbes)
    validate_relation_bindings([record])
    assert len(record.relation_bindings) == 1
    assert record.relation_bindings[0].disease_name == "结直肠癌"


def test_direct_taxon_search_promotes_source_named_disease_despite_other_sample_site():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-pseudomonas",
        name="铜绿假单胞菌",
        scientificName="Pseudomonas aeruginosa",
        category="opportunistic_signal",
        sourceFinding={"status": "检出"},
    )
    microbes = AbnormalMicrobesSection(items=[microbe], summary="")
    record = EvidenceRecord(
        evidenceKey="pseudomonas-lrti-consensus",
        category="guideline",
        title="中国铜绿假单胞菌下呼吸道感染诊治专家共识",
        source="中华医学会",
        summary="铜绿假单胞菌是下呼吸道感染常见致病菌。",
        intents=["microbe_discovery"],
        targets=["Pseudomonas aeruginosa / 铜绿假单胞菌"],
        microbeIds=[microbe.microbe_id],
        interpretation={
            "claim": "铜绿假单胞菌是下呼吸道感染常见致病菌。",
            "targetAlignment": "indirect",
            "taxonAlignment": "species",
            "sampleAlignment": "respiratory",
            "patientApplicability": "contextual",
            "confidence": "high",
        },
    )

    promote_discovery_conditions_to_bindings([record], microbes)
    validate_relation_bindings([record])

    assert [binding.disease_name for binding in record.relation_bindings] == [
        "下呼吸道感染"
    ]


def test_same_citation_keeps_intent_specific_records_for_binding():
    clinical = EvidenceRecord(
        evidence_key="clinical-copy",
        category="guideline",
        title="Clinical practice guideline for chronic kidney disease",
        source="Society",
        supports=["disease-ckd"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "慢性肾病可出现肾小球滤过率异常。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
        },
    )
    diagnostic = clinical.model_copy(
        deep=True,
        update={
            "evidence_key": "diagnostic-copy",
            "intents": ["diagnostic_guideline"],
            "interpretation": {
                "claim": "慢性肾病的诊断评估包括肾小球滤过率。",
                "targetAlignment": "direct",
                "patientApplicability": "direct",
            },
        },
    )
    semantic = [clinical, diagnostic]
    citations = consolidate_public_records(
        [record.model_copy(deep=True) for record in semantic]
    )
    references, citation_numbers = number_evidence(citations)
    semantic_numbers = citation_numbers_for_semantic_records(
        semantic,
        citations,
        citation_numbers,
    )
    assert len(references) == 1
    assert semantic_numbers == {"clinical-copy": 1, "diagnostic-copy": 1}


def test_retrieval_merge_does_not_collapse_different_semantic_intents():
    clinical = EvidenceRecord(
        evidence_key="same-source-clinical",
        category="guideline",
        title="2022 Seoul Consensus on Functional Constipation",
        source="Society",
        supports=["disease-constipation"],
        intents=["disease_clinical_significance"],
        targets=["便秘"],
    )
    health = clinical.model_copy(
        deep=True,
        update={
            "evidence_key": "same-source-health",
            "supports": ["health-management"],
            "intents": ["health_management"],
            "targets": ["成人便秘相关排便调护"],
        },
    )
    merged = merge_retrieval_results(
        EvidenceRetrievalResult([clinical], set(), [], []),
        EvidenceRetrievalResult([health], set(), [], []),
    )
    assert len(merged.records) == 2
    assert {tuple(item.intents) for item in merged.records} == {
        ("disease_clinical_significance",),
        ("health_management",),
    }


def test_generic_disease_claims_do_not_become_microbiome_association_refs():
    microbes = type("Section", (), {"items": []})()
    disease = DiseaseRiskItem(
        disease_id="disease-constipation",
        name="便秘",
        source_type="source_report",
    )
    diseases = type("Section", (), {"items": [disease]})()
    records = []
    for number in range(1, 4):
        records.append(
            EvidenceRecord(
                evidence_key=f"constipation-{number}",
                category="guideline" if number < 3 else "literature",
                title=f"Constipation evidence {number}",
                source="Society",
                relevance=1 - number / 100,
                supports=["disease-constipation"],
                intents=["disease_clinical_significance"],
                interpretation={
                    "claim": f"便秘的临床特征证据结论{number}。",
                    "targetAlignment": "direct",
                    "patientApplicability": "direct",
                    "confidence": "moderate",
                },
            )
        )
    numbers = {record.evidence_key: index for index, record in enumerate(records, 1)}
    attach_evidence_refs(microbes, diseases, records, numbers)
    assert disease.association_evidence_refs == []
    assert disease.medical_interpretation is None


def test_microbe_risk_without_source_bounded_explanation_is_not_displayed():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="relation-without-public-explanation",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer",
        source="Journal",
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="binding-without-public-explanation",
                microbe_id="taxon-fn",
                microbe_name="Fusobacterium nucleatum",
                disease_id="disease-crc",
                disease_name="结直肠癌",
                relation_type="epidemiologic_association",
            )
        ],
        interpretation={
            # The claim does not name the organism, so it cannot explain the
            # organism-disease relationship shown in this row.
            "claim": "人体队列研究显示结直肠癌与肠道微生态变化相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )
    attach_evidence_refs(
        microbes,
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].disease_and_risks == []
    assert microbes.items[0].evidence_refs == []


def test_enteric_fever_synonyms_are_combined_into_one_public_risk():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    records = []
    for number, disease_name in enumerate(("肠热症", "伤寒副伤寒"), 1):
        records.append(
            EvidenceRecord(
                evidence_key=f"enteric-fever-{number}",
                category="guideline",
                title=f"Fusobacterium nucleatum and {disease_name}",
                source="Society",
                relation_bindings=[
                    EvidenceRelationBinding(
                        relation_id=f"enteric-binding-{number}",
                        microbe_id="taxon-fn",
                        microbe_name="Fusobacterium nucleatum",
                        disease_id=f"enteric-disease-{number}",
                        disease_name=disease_name,
                        relation_type="established_pathogen",
                    )
                ],
                interpretation={
                    "claim": (f"Fusobacterium nucleatum人体感染可出现{disease_name}。"),
                    "targetAlignment": "direct",
                    "taxonAlignment": "species",
                    "patientApplicability": "direct",
                },
            )
        )
    attach_evidence_refs(
        microbes,
        diseases,
        records,
        {record.evidence_key: number for number, record in enumerate(records, 1)},
    )
    assert [risk.name for risk in microbes.items[0].disease_and_risks] == ["肠热病"]
    assert microbes.items[0].disease_and_risks[0].evidence_refs == [1, 2]


def test_disease_claim_from_a_different_patient_population_is_not_displayed():
    record = EvidenceRecord(
        evidence_key="sleep-schizophrenia-population",
        category="literature",
        title="Sleep disorder in patients with schizophrenia and insomnia",
        source="Journal",
        targets=["睡眠障碍"],
        interpretation={
            "claim": ("伴有失眠的精神分裂症患者的睡眠障碍与血清维生素D水平降低相关。"),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) is None

    record.interpretation.claim = "睡眠障碍可影响日间功能和生活质量。"
    assert _public_disease_claim(record) == record.interpretation.claim


def test_actigraphy_synonyms_share_one_evaluation_identity():
    assert _evaluation_identity("腕式体动记录") == "actigraphy"
    assert _evaluation_identity("7-day wrist actigraph monitoring") == "actigraphy"


def test_relation_description_keeps_subject_across_comma_and_tries_next_source():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()

    def relation_record(key, category, claim):
        return EvidenceRecord(
            evidence_key=key,
            category=category,
            title=f"Fusobacterium nucleatum colorectal cancer {key}",
            source="Journal",
            relation_bindings=[
                EvidenceRelationBinding(
                    relation_id=f"binding-{key}",
                    microbe_id="taxon-fn",
                    microbe_name="Fusobacterium nucleatum",
                    disease_id="disease-crc",
                    disease_name="结直肠癌",
                    relation_type="epidemiologic_association",
                )
            ],
            interpretation={
                "claim": claim,
                "targetAlignment": "direct",
                "taxonAlignment": "species",
                "patientApplicability": "contextual",
            },
        )

    records = [
        relation_record(
            "method-first",
            "guideline",
            "具核梭杆菌抗体水平可用于结直肠癌诊断。",
        ),
        relation_record(
            "medical-second",
            "literature",
            "具核梭杆菌是人体肠道相关菌，与结直肠癌风险增加相关。",
        ),
    ]
    attach_evidence_refs(
        microbes,
        diseases,
        records,
        {record.evidence_key: number for number, record in enumerate(records, 1)},
    )
    risks = microbes.items[0].disease_and_risks
    assert len(risks) == 1
    assert risks[0].name == "结直肠癌"
    assert "风险增加相关" in risks[0].description
    assert "抗体水平" not in risks[0].description


def test_generic_pathogen_label_is_not_a_public_disease_or_adverse_outcome():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="generic-pathogen-label",
        category="literature",
        title="Fusobacterium nucleatum human pathogen",
        source="Journal",
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id="generic-pathogen-binding",
                microbe_id="taxon-fn",
                microbe_name="Fusobacterium nucleatum",
                disease_id="generic-pathogen-condition",
                disease_name="人类病原真菌",
                relation_type="opportunistic",
            )
        ],
        interpretation={
            "claim": "具核梭杆菌是人类病原真菌。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )
    attach_evidence_refs(
        microbes,
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].disease_and_risks == []


def test_disease_claim_removes_special_region_and_prediction_tool_prose():
    diabetes = EvidenceRecord(
        evidence_key="diabetes-mixed",
        category="literature",
        title="Type 2 diabetes clinical evidence",
        source="Journal",
        targets=["II型糖尿病"],
        interpretation={
            "claim": (
                "II型糖尿病常伴长期无症状高血糖，增加慢性并发症风险；"
                "新疆地区2型糖尿病与西北燥证相关。"
            ),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(diabetes) == (
        "II型糖尿病常伴长期无症状高血糖，增加慢性并发症风险。"
    )

    adenoma = EvidenceRecord(
        evidence_key="adenoma-mixed",
        category="literature",
        title="Colorectal adenoma risk evidence",
        source="Journal",
        targets=["腺瘤"],
        interpretation={
            "claim": (
                "男性性别是结直肠腺瘤的预测因素，优势比高于粪便潜血试验；"
                "幽门螺杆菌感染与结直肠腺瘤风险相关。"
            ),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(adenoma) == ("幽门螺杆菌感染与结直肠腺瘤风险相关。")


@pytest.mark.parametrize(
    "claim",
    [
        "结直肠癌患者中具核梭杆菌的感染率为32%。",
        "具核梭杆菌与结直肠癌患者的降钙素原水平相关。",
        "医疗器械留置期间具核梭杆菌可见于结直肠癌患者。",
        "具核梭杆菌对抗真菌药物敏感，并与结直肠癌相关。",
    ],
)
def test_process_or_population_statistics_do_not_become_microbe_risks(claim):
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    record = EvidenceRecord(
        evidence_key="non-medical-relation-description",
        category="literature",
        title="Fusobacterium nucleatum in colorectal cancer",
        source="Journal",
        relationBindings=[
            EvidenceRelationBinding(
                relationId="non-medical-binding",
                microbeId="taxon-fn",
                microbeName="Fusobacterium nucleatum",
                microbeAliases=["具核梭杆菌"],
                diseaseId="disease-crc",
                diseaseName="结直肠癌",
                relationType="epidemiologic_association",
            )
        ],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )
    attach_evidence_refs(
        microbes,
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].disease_and_risks == []


def test_meta_relation_and_generic_symptom_rows_are_not_public_risks():
    microbes = build_abnormal_microbes(parsed_report())
    diseases = type("Section", (), {"items": []})()
    records = []
    for index, (condition, claim) in enumerate(
        (
            ("机会性感染", "包括具核梭杆菌机会性感染。"),
            ("发热", "具核梭杆菌感染可出现发热。"),
            ("头痛", "具核梭杆菌感染可出现头痛。"),
        ),
        1,
    ):
        records.append(
            EvidenceRecord(
                evidence_key=f"generic-condition-{index}",
                category="guideline",
                title=f"Fusobacterium nucleatum {condition}",
                source="Society",
                relationBindings=[
                    EvidenceRelationBinding(
                        relationId=f"generic-condition-binding-{index}",
                        microbeId="taxon-fn",
                        microbeName="Fusobacterium nucleatum",
                        microbeAliases=["具核梭杆菌"],
                        diseaseId=f"generic-condition-disease-{index}",
                        diseaseName=condition,
                        relationType="opportunistic",
                    )
                ],
                interpretation={
                    "claim": claim,
                    "targetAlignment": "direct",
                    "taxonAlignment": "species",
                    "patientApplicability": "contextual",
                },
            )
        )
    attach_evidence_refs(
        microbes,
        diseases,
        records,
        {record.evidence_key: index for index, record in enumerate(records, 1)},
    )
    assert microbes.items[0].disease_and_risks == []


def test_disease_treatment_only_claim_is_not_medical_interpretation():
    record = EvidenceRecord(
        evidence_key="crohn-maintenance",
        category="guideline",
        title="Crohn disease maintenance guideline",
        source="Society",
        targets=["克罗恩病"],
        interpretation={
            "claim": "硫嘌呴用于低风险克罗恩病患者维持。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) is None

    record.targets = ["糖化血红蛋白"]
    record.interpretation.claim = "与糖化血红蛋白的检测质量相关。"
    assert _public_disease_claim(record) is None


def test_high_beneficial_microbe_does_not_use_low_abundance_or_intervention_claims():
    parsed = ParsedMicrobiomeReport(
        sourceType="microbiome_data",
        sourceName="source.json",
        subject=SubjectInfo(name="测试患者"),
        taxa=[
            ParsedTaxon(
                taxonId="taxon-akk",
                category="beneficial",
                chineseName="嗜黏蛋白-艾克曼菌",
                latinName="Akkermansia muciniphila",
                value="4.565",
                referenceRange="0-2.4003",
                sourceStatus="超标",
                explicitlyAbnormal=True,
                detectionStatus="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {"items": []})()
    records = []
    for key, direction, claim in (
        (
            "opposite-direction",
            "decreased",
            "嗜黏蛋白-艾克曼菌丰度降低与胰岛素抵抗风险增加相关。",
        ),
        (
            "intervention-benefit",
            "associated",
            "嗜黏蛋白-艾克曼菌对II型糖尿病具有改善代谢、减轻炎症的作用。",
        ),
        (
            "generic-benefit",
            "associated",
            "嗜黏蛋白-艾克曼菌对II型糖尿病及相关疾病具有改善作用。",
        ),
        (
            "barrier-benefit",
            "associated",
            "嗜黏蛋白-艾克曼菌通过调节肠道屏障、降低炎症和改善胰岛素敏感性，与胰岛素抵抗相关。",
        ),
        (
            "direction-not-stated",
            "associated",
            "嗜黏蛋白-艾克曼菌与胰岛素抵抗相关。",
        ),
    ):
        records.append(
            EvidenceRecord(
                evidenceKey=key,
                category="literature",
                title="Akkermansia muciniphila and type 2 diabetes",
                source="Journal",
                relationBindings=[
                    EvidenceRelationBinding(
                        relationId=f"binding-{key}",
                        microbeId="taxon-akk",
                        microbeName="Akkermansia muciniphila",
                        microbeAliases=["嗜黏蛋白-艾克曼菌"],
                        diseaseId="disease-t2d",
                        diseaseName="II型糖尿病",
                        relationType="epidemiologic_association",
                        claim=claim,
                    )
                ],
                interpretation={
                    "claim": claim,
                    "direction": direction,
                    "targetAlignment": "direct",
                    "taxonAlignment": "species",
                    "sampleAlignment": "stool",
                    "patientApplicability": "contextual",
                },
            )
        )
    attach_evidence_refs(
        microbes,
        diseases,
        records,
        {record.evidence_key: index for index, record in enumerate(records, 1)},
    )
    assert microbes.items[0].disease_and_risks == []


def test_detected_pathogen_does_not_use_an_unreported_high_level_claim():
    parsed = ParsedMicrobiomeReport(
        sourceType="microbiome_data",
        sourceName="source.json",
        subject=SubjectInfo(name="测试患者"),
        taxa=[
            ParsedTaxon(
                taxonId="taxon-pneumococcus",
                category="pathogenic",
                chineseName="肺炎链球菌",
                latinName="Streptococcus pneumoniae",
                value="0.0005",
                detectionStatus="detected",
            )
        ],
    )
    microbes = build_abnormal_microbes(parsed)
    diseases = type("Section", (), {"items": []})()
    claim = "肺炎链球菌与肺炎球菌病相关，高水平提示疾病更严重。"
    record = EvidenceRecord(
        evidenceKey="pneumococcus-high-level",
        category="literature",
        title="Streptococcus pneumoniae and pneumococcal disease",
        source="Journal",
        relationBindings=[
            EvidenceRelationBinding(
                relationId="binding-pneumococcus",
                microbeId="taxon-pneumococcus",
                microbeName="Streptococcus pneumoniae",
                microbeAliases=["肺炎链球菌"],
                diseaseId="disease-pneumococcus",
                diseaseName="肺炎球菌病",
                relationType="established_pathogen",
                claim=claim,
            )
        ],
        interpretation={
            "claim": claim,
            "direction": "increased",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "other",
            "patientApplicability": "contextual",
        },
    )
    attach_evidence_refs(
        microbes,
        diseases,
        [record],
        {record.evidence_key: 1},
    )
    assert microbes.items[0].disease_and_risks == []


def test_normal_or_reference_bounded_detected_pathogens_are_not_abnormal_rows():
    parsed = ParsedMicrobiomeReport(
        sourceType="microbiome_data",
        sourceName="source.json",
        subject=SubjectInfo(name="测试患者"),
        taxa=[
            ParsedTaxon(
                taxonId="taxon-normal",
                category="pathogenic",
                chineseName="肺炎链球菌",
                latinName="Streptococcus pneumoniae",
                value="0.0005",
                sourceStatus="正常",
                detectionStatus="detected",
            ),
            ParsedTaxon(
                taxonId="taxon-in-range",
                category="opportunistic",
                chineseName="具核梭杆菌",
                latinName="Fusobacterium nucleatum",
                value="0.004",
                referenceRange="0-0.01",
                detectionStatus="detected",
            ),
            ParsedTaxon(
                taxonId="taxon-abnormal",
                category="pathogenic",
                chineseName="肠沙门菌",
                latinName="Salmonella enterica",
                value="0.02",
                referenceRange="0-0.001",
                sourceStatus="超标",
                explicitlyAbnormal=True,
                detectionStatus="detected",
            ),
        ],
    )

    microbes = build_abnormal_microbes(parsed)

    assert [item.scientific_name for item in microbes.items] == ["Salmonella enterica"]


def test_relation_binding_requires_tight_source_relation_not_global_cooccurrence():
    record = EvidenceRecord(
        evidenceKey="generic-handbook",
        category="guideline",
        title="传染病临床手册",
        source="Society",
        summary=(
            "本书收录人感染猪链球菌病。各章节还包括病毒性疾病、寄生虫病及细菌性痢疾。"
        ),
        relationBindings=[
            EvidenceRelationBinding(
                relationId="wrong-binding",
                microbeId="taxon-suis",
                microbeName="Streptococcus suis",
                microbeAliases=["猪链球菌"],
                diseaseId="disease-dysentery",
                diseaseName="细菌性痢疾",
            )
        ],
        interpretation={
            "claim": "猪链球菌与细菌性痢疾相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )

    validate_relation_bindings([record])

    assert record.relation_bindings == []


def _species_overreach_description(claim):
    record = EvidenceRecord(
        evidenceKey="species-overreach",
        category="literature",
        title="Fusobacterium nucleatum and colorectal cancer",
        source="Journal",
        relationBindings=[
            EvidenceRelationBinding(
                relationId="species-overreach-binding",
                microbeId="taxon-fn",
                microbeName="Fusobacterium nucleatum",
                microbeAliases=["具核梭杆菌"],
                diseaseId="disease-crc",
                diseaseName="结直肠癌",
                relationType="epidemiologic_association",
                claim=claim,
            )
        ],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "stool",
            "patientApplicability": "contextual",
        },
    )
    return _public_relation_description(
        record,
        microbe_name="具核梭杆菌",
        disease_name="结直肠癌",
        relation_type="epidemiologic_association",
    )


def test_gene_clause_is_removed_from_otherwise_valid_species_relation():
    description = _species_overreach_description(
        "具核梭杆菌与结直肠癌发生进展相关，fadA基因水平在腺癌中高于腺瘤。"
    )
    assert "结直肠癌发生进展相关" in description
    assert "基因" not in description


def test_subspecies_toxin_claim_is_not_a_species_level_risk():
    description = _species_overreach_description(
        "具核梭杆菌某亚种产生米酵菌酸，与结直肠癌相关。"
    )
    assert description == ""


@pytest.mark.parametrize(
    ("target", "claim"),
    [
        ("炎症性肠病", "炎症性肠病不是COVID-19的风险因素。"),
        ("冠心病", "亚临床甲状腺功能异常与冠心病风险增加相关。"),
        ("多发性硬化症", "多发性硬化症的阴燃相关恶化。"),
        ("肥胖", "该科学声明阐明了肥胖的发病机制。"),
        ("高血压", "高血压中的发作性高血压可导致血流动力学改变。"),
        ("肾结石", "肾结石的检出与影像学方法相关。"),
        ("胃癌", "日本胃癌分类第三版。"),
        ("肝癌", "螺旋CTAP可显示肝癌门脉受侵。"),
        ("甲状腺癌", "甲状腺癌近年呈高发趋势，死亡和复发风险是临床关注点。"),
        ("胃癌", "该规约提供了胃癌分期和处理的标准系统。"),
        ("甲状腺癌", "甲状腺癌血清标志物的临床应用。"),
        ("结直肠癌", "结直肠癌是英国第4位最常见恶性肿瘤，5年生存率约60%。"),
        ("癫痫", "癫痫患者可出现不典型的临床表现。"),
        (
            "便秘",
            "便秘分度标准量化后有助于临床决策，并强调了便秘与精神心理障碍的相关性。",
        ),
        (
            "II型糖尿病",
            "II型糖尿病与人口老龄化和社会经济差异相关，体重管理和生活方式改变对预防和缓解疾病很重要。",
        ),
        ("冠心病", "血清ESM-1和Myo水平与冠心病不稳定型心绞痛患者MACE风险相关。"),
        ("类风湿性关节炎", "2010 ACR类风湿性关节炎分类标准聚焦早期特征。"),
        ("甲状腺癌", "为晚期甲状腺癌提供基于分子标志物的检测算法。"),
        ("腹泻型肠易激综合征", "抗肠神经元抗体与腹泻型肠易激综合征相关。"),
        ("肾结石", "维生素D与肾结石的关系尚无定论。"),
        ("肝硬化", "2023国外专家意见：对肝硬化患者侵入性操作进行出血风险分级。"),
        ("高尿酸血症", "女性＞357 μmol/L，并区分无症状高尿酸血症。"),
        ("类风湿性关节炎", "类风湿性关节炎基于滑膜炎、血清学异常和症状持续时间。"),
        ("焦虑症", "焦虑症与心理因素相关，严重时可引发抑郁症。"),
        ("肥胖", "该科学声明阐述了肥胖的发病机制。"),
        ("高血压", "运动试验中高血压反应是运动员未来发生高血压的风险因素。"),
        ("肾结石", "血浆致动脉硬化指数（AIP）升高与肾结石患病风险增加相关。"),
        ("胃癌", "ACR适当性标准为胃癌分期和随访提供影像学推荐。"),
        ("胃癌", "pT1b期早期胃癌的淋巴结转移率与脉管癌栓和肿瘤出芽相关。"),
        ("肝硬化", "肝硬化需要规范管理并发症。"),
        ("类风湿性关节炎", "类风湿性关节炎患者的中医体质分布与炎症反应相关。"),
        ("高脂血症", "高脂血症可通过食养方式辅助预防和控制。"),
        ("胃癌", "胃癌的日本分类系统提供了标准化的分期方法。"),
    ],
)
def test_off_target_or_process_disease_claim_is_not_displayed(target, claim):
    record = EvidenceRecord(
        evidenceKey=f"bad-{stable_id('claim', target)}",
        category="literature",
        title=f"{target} clinical evidence",
        source="Journal",
        targets=[target],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) is None


def test_disease_claim_removes_social_context_tail_without_dropping_definition():
    record = EvidenceRecord(
        evidenceKey="diabetes-social-context",
        category="literature",
        title="Type 2 diabetes clinical evidence",
        source="Journal",
        targets=["II型糖尿病"],
        interpretation={
            "claim": (
                "II型糖尿病是一种以血糖升高为特征的慢性代谢性疾病，"
                "其发生发展与体重、生活方式及社会环境因素密切相关。"
            ),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) == (
        "II型糖尿病是一种以血糖升高为特征的慢性代谢性疾病。"
    )


def test_disease_claim_keeps_general_meaning_and_removes_special_subtype_tail():
    record = EvidenceRecord(
        evidenceKey="crohn-general-and-severe",
        category="literature",
        title="Crohn disease clinical evidence",
        source="Journal",
        targets=["克罗恩病"],
        interpretation={
            "claim": ("克罗恩病是一种慢性炎症性肠病，重症克罗恩病患者预后不佳。"),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) == "克罗恩病是一种慢性炎症性肠病。"


@pytest.mark.parametrize(
    ("target", "claim", "expected"),
    [
        (
            "炎症性肠病",
            "炎症性肠病包括溃疡性结肠炎和克罗恩病，欧洲和美国各有约100万患者。",
            "炎症性肠病包括溃疡性结肠炎和克罗恩病。",
        ),
        (
            "焦虑症",
            "焦虑症患者的睡眠状况与焦虑症状显著相关；两者互为因果。",
            None,
        ),
        (
            "高血压",
            "夜间高血压与心血管风险相关；γ-谷氨酰转移酶与非杓型高血压相关。",
            "夜间高血压与心血管风险相关。",
        ),
    ],
)
def test_disease_claim_keeps_medical_meaning_without_statistics_or_biomarkers(
    target, claim, expected
):
    record = EvidenceRecord(
        evidenceKey=f"mixed-{stable_id('claim', target)}",
        category="literature",
        title=f"{target} clinical evidence",
        source="Journal",
        targets=[target],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) == expected


def test_disease_claim_does_not_leave_a_conjunction_fragment():
    record = EvidenceRecord(
        evidenceKey="ibsd-antibody-fragment",
        category="literature",
        title="腹泻型肠易激综合征 clinical evidence",
        source="Journal",
        targets=["腹泻型肠易激综合征"],
        interpretation={
            "claim": (
                "抗肠神经元抗体与腹泻型肠易激综合征相关，"
                "且抗体强阳性与更重的肠道症状相关。"
            ),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )
    assert _public_disease_claim(record) is None


def test_species_level_relation_removes_serotype_and_enterotoxin_clauses():
    assert _species_overreach_description("具核梭杆菌5b型与结直肠癌相关。") == ""
    assert (
        _species_overreach_description(
            "具核梭杆菌与结直肠癌相关，患者粪便中检出肠毒素。"
        )
        == "具核梭杆菌与结直肠癌相关。"
    )


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        ("II型糖尿病", "2型糖尿病高血糖管理需考虑心血管和肾脏获益。"),
        ("冠心病", "抑郁症与冠心病相关，该文献为ESC工作组立场文件的勘误。"),
        ("多发性硬化症", "血清Irisin和Nesfatin-1水平下降与多发性硬化症进展相关。"),
        ("癫痫", "视觉诱发的癫痫发作可通过避免强光闪烁来降低。"),
        ("肥胖", "2017 TES科学声明：肥胖的发病机制描述了肥胖的发病机制。"),
        ("高血压", "文章主要包括高血压的分类。"),
        ("胃癌", "胃癌分期处理规约。"),
        ("甲状腺癌", "甲状腺癌(differentiated thyroid carcinoma。"),
    ),
)
def test_public_disease_claim_rejects_editorial_research_or_broken_prose(target, claim):
    record = EvidenceRecord(
        evidenceKey=f"unsafe-{stable_id('claim', target)}",
        category="literature",
        title=f"{target} clinical evidence",
        source="Journal",
        targets=[target],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) is None


def test_disease_claim_keeps_medical_fact_before_editorial_harm_clause():
    record = EvidenceRecord(
        evidenceKey="liver-cancer-medical-clause",
        category="guideline",
        title="肝癌指南比较",
        source="Journal",
        targets=["肝癌"],
        summary="肝癌是一种常见恶性肿瘤，严重危害人民健康。",
        interpretation={
            "claim": "该文比较了不同肝癌指南。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == "肝癌是一种常见恶性肿瘤。"


def test_kidney_stone_source_keeps_asymptomatic_medical_fact():
    record = EvidenceRecord(
        evidenceKey="kidney-stone-clinical-guide",
        category="guideline",
        title="肾结石的临床诊断指南",
        source="CARI",
        targets=["肾结石"],
        summary="肾结石多数无症状，血尿不足以确诊或排除输尿管结石。",
        interpretation={
            "claim": "该指南讨论肾结石诊断。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
        },
    )

    assert _public_disease_claim(record) == "肾结石多数无症状。"


def test_upper_endoscopy_does_not_match_ibd_by_duodenum_substring():
    disease = DiseaseRiskItem(
        diseaseId="disease-ibd",
        name="炎症性肠病",
        sourceType="source_report",
    )

    assert not evaluation_is_public_for_disease(
        "食管胃十二指肠镜",
        rationale="用于炎症性肠病的明确诊断。",
        disease=disease,
    )


def test_epilepsy_brain_mri_keeps_source_bound_diagnostic_purpose():
    disease = DiseaseRiskItem(
        diseaseId="disease-epilepsy",
        name="癫痫",
        sourceType="source_report",
    )
    source_text = (
        "Seizures and epilepsy can be challenging to diagnose. "
        "MRI of the brain is usually appropriate for the clinical scenarios."
    )

    assert evaluation_is_public_for_disease(
        "脑部MRI",
        rationale="用于辅助癫痫诊断。",
        disease=disease,
    )
    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="脑部MRI",
            source_rationale="指南指出在癫痫临床场景中通常适用。",
            purpose="confirm_target",
            source_text=source_text,
        )
        == "脑部MRI用于辅助癫痫诊断。"
    )


def test_evaluation_rationale_removes_internal_target_alignment_phrase():
    disease = DiseaseRiskItem(
        diseaseId="disease-thyroid-cancer",
        name="甲状腺癌",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="甲状腺超声检查",
            source_rationale=("用于甲状腺结节的良恶性筛查，直接对应目标疾病的筛查。"),
            purpose="screen_target",
        )
        == "用于甲状腺结节的良恶性筛查。"
    )


def test_evaluation_rationale_removes_target_and_minimum_standard_meta_prose():
    epilepsy = DiseaseRiskItem(
        diseaseId="disease-epilepsy",
        name="癫痫",
        sourceType="source_report",
    )
    liver_cancer = DiseaseRiskItem(
        diseaseId="disease-liver-cancer",
        name="肝癌",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=epilepsy,
            evaluation_name="长期视频脑电图监测",
            source_rationale="提供癫痫评估的最低标准，用于确认目标疾病。",
            purpose="characterize_target",
        )
        == "用于评估癫痫。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=liver_cancer,
            evaluation_name="钆塞酸二钠增强磁共振成像",
            source_rationale="用于检测肝细胞癌，与目标肝癌的诊断直接相关。",
            purpose="confirm_target",
        )
        == "用于检测肝细胞癌。"
    )


def test_evaluation_rationale_rejects_ai_augmented_research_method():
    disease = DiseaseRiskItem(
        diseaseId="disease-liver-cancer",
        name="肝癌",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="动态增强MRI",
            source_rationale=(
                "动态增强MRI结合人工智能可提升小肝癌检出率，用于肝癌早期筛查。"
            ),
            purpose="screen_target",
        )
        is None
    )


def test_public_disease_claim_cleans_markdown_grading_and_inline_citations():
    inflammatory = EvidenceRecord(
        evidenceKey="ibd-markdown",
        category="guideline",
        title="Inflammatory bowel disease",
        source="Society",
        targets=["炎症性肠病"],
        interpretation={
            "claim": "*炎症性肠病主要包括溃疡性结肠炎和克罗恩病。",
            "targetAlignment": "direct",
        },
    )
    schizophrenia = EvidenceRecord(
        evidenceKey="schizophrenia-citation",
        category="guideline",
        title="Schizophrenia definition",
        source="Society",
        targets=["精神分裂症"],
        interpretation={
            "claim": "精神分裂症是一组可出现感知、思维和行为异常的精神疾病[1]。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(inflammatory) == (
        "炎症性肠病主要包括溃疡性结肠炎和克罗恩病。"
    )
    assert _public_disease_claim(schizophrenia) == (
        "精神分裂症是一组可出现感知、思维和行为异常的精神疾病。"
    )


def test_evaluation_purpose_rejects_research_segmentation_but_keeps_source_diagnosis():
    ms = DiseaseRiskItem(
        diseaseId="disease-ms",
        name="多发性硬化症",
        sourceType="source_report",
    )
    ra = DiseaseRiskItem(
        diseaseId="disease-ra",
        name="类风湿性关节炎",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=ms,
            evaluation_name="脑部MRI",
            source_rationale="用于多发性硬化症病变分割",
            purpose="characterize_target",
            source_text="多发性硬化症脑部MRI研究中的病变分割算法。",
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=ra,
            evaluation_name="抗CCP抗体和抗MCV抗体检测",
            source_rationale="摘要表明这些抗体对RA诊断具有高价值",
            purpose="screen_target",
            source_text=("抗CCP抗体和抗MCV抗体对类风湿性关节炎诊断中的价值。"),
        )
        == "用于辅助类风湿性关节炎诊断。"
    )


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        ("炎症性肠病", "覆盖炎症性肠病的临床管理范围。"),
        ("肝硬化", "肝硬化是一种需要管理的疾病。"),
        ("冠心病", "提示糖尿病与冠心病风险相关。"),
        ("阿尔兹海默症", "脑灌注MRI能反映阿尔兹海默症患者的血流动力学改变。"),
        ("癫痫", "病变相关性癫痫是癫痫外科的手术适应证之一。"),
        ("抑郁症", "忧郁快感缺失型抑郁症是抑郁症的一种类型。"),
        ("肾结石", "经常使用鱼油与新发肾结石风险降低相关。"),
        ("胃癌", "涉及遗传性弥漫型胃癌及家族性胃癌。"),
        ("肝癌", "方法:43例肝癌患者均经置管行S-CTAP。"),
    ),
)
def test_disease_claim_rejects_off_target_subtype_method_or_management(target, claim):
    record = EvidenceRecord(
        evidenceKey=f"row-quality-{stable_id('claim', target + claim)}",
        category="literature",
        title=f"{target} evidence",
        source="Journal",
        targets=[target],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )

    assert _public_disease_claim(record) is None


def test_disease_definition_and_narrow_non_gut_hit_are_not_risk_explanations():
    disease = DiseaseRiskItem(
        diseaseId="disease-obesity",
        name="肥胖",
        sourceType="source_report",
    )
    narrow = EvidenceRecord(
        evidenceKey="obesity-primary-narrow",
        category="literature",
        title="Metabolic flexibility in obesity",
        source="Journal",
        supports=[disease.disease_id],
        intents=["disease_clinical_significance"],
        targets=["肥胖"],
        interpretation={
            "claim": "肥胖与代谢灵活性下降相关。",
            "targetAlignment": "direct",
        },
    )
    definition = EvidenceRecord(
        evidenceKey="obesity-diagnostic-definition",
        category="guideline",
        title="Clinical obesity definition",
        source="Society",
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=["肥胖"],
        interpretation={
            "claim": "肥胖是一种以脂肪过多为特征的慢性疾病。",
            "targetAlignment": "direct",
        },
    )

    attach_evidence_refs(
        type("Section", (), {"items": []})(),
        type("Section", (), {"items": [disease]})(),
        [narrow, definition],
        {narrow.evidence_key: 1, definition.evidence_key: 2},
    )

    assert disease.medical_interpretation is None
    assert disease.association_evidence_refs == []
    assert disease.confirmation_evidence_refs == [2]


def test_english_bacteremia_title_can_support_exact_species_relation():
    record = EvidenceRecord(
        evidenceKey="baumannii-bacteremia",
        category="literature",
        title="Bacteremia caused by Acinetobacter baumannii.",
        source="Journal",
        summary="Acinetobacter baumannii bacteremia was observed in adults.",
        relationBindings=[
            EvidenceRelationBinding(
                relationId="baumannii-bacteremia-binding",
                microbeId="taxon-ab",
                microbeName="Acinetobacter baumannii",
                microbeAliases=["鲍曼不动杆菌"],
                diseaseId="disease-bacteremia",
                diseaseName="菌血症",
                relationType="established_pathogen",
            )
        ],
        interpretation={
            "claim": "该研究比较了鲍曼不动杆菌菌血症的死亡率。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
    )

    validate_relation_bindings([record])

    assert len(record.relation_bindings) == 1
    assert (
        _public_relation_description(
            record,
            microbe_name="鲍曼不动杆菌",
            disease_name="菌血症",
            relation_type="established_pathogen",
        )
        == "鲍曼不动杆菌可引起菌血症。"
    )
    assert (
        _species_overreach_description(
            "具核梭杆菌与结直肠癌相关，志贺氏菌属分为4群，本摘要报告两批病例。"
        )
        == "具核梭杆菌与结直肠癌相关。"
    )


def test_exact_relation_query_can_promote_a_different_source_derived_condition():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-baumannii",
        name="鲍曼不动杆菌",
        scientificName="Acinetobacter baumannii",
        category="pathogenic_signal",
        sourceFinding={"status": "超标", "value": "0.001", "unit": "%"},
    )
    microbes = AbnormalMicrobesSection(items=[microbe], summary="")
    record = EvidenceRecord(
        evidenceKey="baumannii-cap",
        category="literature",
        title="Community-acquired pneumonia caused by Acinetobacter baumannii.",
        source="Journal",
        summary="Acinetobacter baumannii causes community-acquired pneumonia in adults.",
        intents=["microbe_disease_association"],
        microbeIds=[microbe.microbe_id],
        interpretation={
            "claim": "鲍曼不动杆菌可引起社区获得性肺炎。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "respiratory",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "社区获得性肺炎",
                    "searchTerm": "community-acquired pneumonia",
                    "sourcePhrase": "community-acquired pneumonia",
                    "relationType": "established_pathogen",
                }
            ],
        },
    )

    promote_discovery_conditions_to_bindings([record], microbes)
    validate_relation_bindings([record])

    assert [item.disease_name for item in record.relation_bindings] == [
        "社区获得性肺炎"
    ]


def test_treatment_title_can_keep_a_separate_source_bounded_disease_description():
    record = EvidenceRecord(
        evidenceKey="ibsd-treatment-review",
        category="literature",
        title="Targeted therapies for diarrhea-predominant irritable bowel syndrome.",
        source="Journal",
        targets=["腹泻型肠易激综合征"],
        interpretation={
            "claim": "腹泻型肠易激综合征可出现腹痛、腹胀和排便习惯改变。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == (
        "腹泻型肠易激综合征可出现腹痛、腹胀和排便习惯改变。"
    )


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        (
            "肝硬化",
            (
                "肝硬化腹水是一种高发且易复发的临床病症，"
                "强调了中西医结合的优势，突出临床实用导向。"
            ),
        ),
        ("精神分裂症", "精神分裂症的多基因风险评分与认知能力相关。"),
        ("高脂血症", "高脂血症在亚洲人群中的风险可在较低BMI时增高。"),
    ),
)
def test_public_disease_claim_rejects_subtype_promotion_or_research_metric(
    target, claim
):
    record = EvidenceRecord(
        evidenceKey=f"formal-row-{stable_id('claim', target)}",
        category="literature",
        title=f"{target} evidence",
        source="Journal",
        targets=[target],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )

    assert _public_disease_claim(record) is None


def test_ibsd_and_kidney_stone_accept_source_supported_clinical_tests():
    ibsd = DiseaseRiskItem(
        diseaseId="disease-ibsd",
        name="腹泻型肠易激综合征",
        sourceType="source_report",
    )
    stone = DiseaseRiskItem(
        diseaseId="disease-stone",
        name="肾结石",
        sourceType="source_report",
    )

    assert evaluation_is_public_for_disease(
        "粪便钙卫蛋白",
        rationale="用于与炎症性肠病进行鉴别。",
        disease=ibsd,
    )
    assert evaluation_is_public_for_disease(
        "非对比螺旋CT",
        rationale="用于辅助确认肾结石。",
        disease=stone,
    )


def test_validated_exact_relation_overrides_model_alignment_fluctuation():
    record = EvidenceRecord(
        evidenceKey="legionella-pneumonia-exact",
        category="literature",
        title="Pneumonia caused by Legionella dumoffii.",
        source="Journal",
        summary="Legionella dumoffii causes pneumonia in humans.",
        interpretation={
            "claim": "杜莫夫军团菌可引起肺炎。",
            "targetAlignment": "indirect",
            "taxonAlignment": "species",
            "sampleAlignment": "respiratory",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="legionella-pneumonia-binding",
                microbeId="taxon-legionella-dumoffii",
                microbeName="Legionella dumoffii",
                microbeAliases=["杜莫夫军团菌"],
                diseaseId="disease-pneumonia",
                diseaseName="肺炎",
                relationType="established_pathogen",
                targetAlignment="indirect",
            )
        ],
    )

    validate_relation_bindings([record])

    assert len(record.relation_bindings) == 1
    assert record.interpretation is not None
    assert record.interpretation.target_alignment == "direct"
    assert record.relation_bindings[0].target_alignment == "direct"


def test_single_abundance_association_names_the_measured_direction():
    record = EvidenceRecord(
        evidenceKey="rectale-psoriasis",
        category="literature",
        title="Roseburia rectale abundance in psoriasis",
        source="Journal",
        summary="Roseburia rectale abundance was decreased in human psoriasis.",
        interpretation={
            "claim": "直肠罗斯拜瑞氏菌在银屑病中显著减少。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "direction": "decreased",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="rectale-psoriasis-binding",
                microbeId="taxon-rectale",
                microbeName="Roseburia rectale",
                microbeAliases=["直肠罗斯拜瑞氏菌"],
                diseaseId="disease-psoriasis",
                diseaseName="银屑病",
                relationType="epidemiologic_association",
                claim="直肠罗斯拜瑞氏菌在银屑病中显著减少。",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="直肠罗斯拜瑞氏菌",
            disease_name="银屑病",
            relation_type="epidemiologic_association",
        )
        == "直肠罗斯拜瑞氏菌在银屑病中丰度显著减少。"
    )


def test_exact_taxon_condition_noun_phrase_title_is_relation_evidence():
    record = EvidenceRecord(
        evidenceKey="staph-pneumonia-title",
        category="literature",
        title="Bacteremic Staphylococcus aureus pneumonia.",
        source="Journal",
        interpretation={
            "claim": "金黄色葡萄球菌肺炎可伴菌血症。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "blood",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="staph-pneumonia-binding",
                microbeId="taxon-staph",
                microbeName="Staphylococcus aureus",
                microbeAliases=["金黄色葡萄球菌"],
                diseaseId="disease-pneumonia",
                diseaseName="肺炎",
                relationType="established_pathogen",
            )
        ],
    )

    validate_relation_bindings([record])

    assert len(record.relation_bindings) == 1
    assert (
        _public_relation_description(
            record,
            microbe_name="金黄色葡萄球菌",
            disease_name="肺炎",
            relation_type="established_pathogen",
        )
        == "金黄色葡萄球菌与肺炎相关。"
    )


def test_relation_summary_does_not_upgrade_association_to_causation():
    record = EvidenceRecord(
        evidenceKey="staph-impetigo-consensus",
        category="guideline",
        title="金黄色葡萄球菌与特应性皮炎加重共识",
        source="专家组",
        summary=(
            "金黄色葡萄球菌负担增加与特应性皮炎严重程度相关，"
            "并可见脓疱病等并发症；部分毒力因子可引起炎症反应。"
        ),
        interpretation={
            "claim": "金黄色葡萄球菌与脓疱病等并发症相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "other",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="staph-impetigo-binding",
                microbeId="taxon-staph",
                microbeName="Staphylococcus aureus",
                microbeAliases=["金黄色葡萄球菌"],
                diseaseId="disease-impetigo",
                diseaseName="脓疱病",
                relationType="established_pathogen",
                claim="金黄色葡萄球菌与脓疱病等并发症相关。",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="金黄色葡萄球菌",
            disease_name="脓疱病",
            relation_type="established_pathogen",
        )
        == "金黄色葡萄球菌与脓疱病相关。"
    )


def test_epidemiologic_binding_never_publishes_model_pathogen_wording():
    record = EvidenceRecord(
        evidenceKey="vulgatus-uc-association",
        category="literature",
        title=(
            "Multi-omics analyses of the ulcerative colitis gut microbiome "
            "link Bacteroides vulgatus with disease severity"
        ),
        source="Journal",
        summary=(
            "A human cohort found an overabundance of Bacteroides vulgatus "
            "features in patients with ulcerative colitis."
        ),
        interpretation={
            "claim": "普通拟杆菌是溃疡性结肠炎的病原体。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="vulgatus-uc-binding",
                microbeId="taxon-vulgatus",
                microbeName="Phocaeicola vulgatus",
                microbeAliases=["Bacteroides vulgatus", "普通拟杆菌"],
                diseaseId="disease-uc",
                diseaseName="溃疡性结肠炎",
                relationType="epidemiologic_association",
                claim="普通拟杆菌是溃疡性结肠炎的病原体。",
            )
        ],
    )

    assert _public_relation_description(
        record,
        microbe_name="普通拟杆菌",
        disease_name="溃疡性结肠炎",
        relation_type="epidemiologic_association",
    ) == "普通拟杆菌与溃疡性结肠炎相关。"


def test_low_beneficial_result_keeps_source_bounded_protective_risk_relation():
    parsed = parsed_report()
    parsed.taxa[0].category = "beneficial"
    parsed.taxa[0].latin_name = "Oxalobacter formigenes"
    parsed.taxa[0].chinese_name = "草酸杆菌"
    parsed.taxa[0].source_status = "丰度低"
    parsed.taxa[0].explicitly_abnormal = True
    item = build_abnormal_microbes(parsed).items[0]
    record = EvidenceRecord(
        evidenceKey="oxalobacter-stone",
        category="literature",
        title="Oxalobacter formigenes may reduce the risk of calcium oxalate kidney stones.",
        source="Journal",
        summary=(
            "The absence of Oxalobacter formigenes could favor kidney stone "
            "development. Colonization was associated with a 70% reduction "
            "in the risk of calcium oxalate kidney stones in adults."
        ),
        interpretation={
            "claim": "草酸杆菌定植与钙草酸肾结石风险降低70%相关。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "stool",
            "patientApplicability": "contextual",
            "direction": "decreased",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="oxalobacter-stone-binding",
                microbeId=item.microbe_id,
                microbeName="Oxalobacter formigenes",
                microbeAliases=["草酸杆菌"],
                diseaseId="disease-kidney-stone",
                diseaseName="肾结石",
                relationType="epidemiologic_association",
                claim="草酸杆菌定植与钙草酸肾结石风险降低70%相关。",
            )
        ],
    )
    description = _public_relation_description(
        record,
        microbe_name="草酸杆菌",
        disease_name="肾结石",
        relation_type="epidemiologic_association",
    )

    assert description == "草酸杆菌丰度降低可能与肾结石风险增加相关。"
    assert _abundance_relation_matches_source_direction(
        item=item,
        record=record,
        description=description,
    )


def test_causal_source_fallback_survives_resistance_or_treatment_title():
    record = EvidenceRecord(
        evidenceKey="mtb-treatment-causal-source",
        category="guideline",
        title="Treatment of isoniazid-resistant tuberculosis caused by Mycobacterium tuberculosis",
        source="Society",
        summary="Mycobacterium tuberculosis causes tuberculosis in humans.",
        interpretation={
            "claim": "该指南讨论耐药结核病的治疗。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="mtb-tuberculosis-binding",
                microbeId="taxon-mtb",
                microbeName="Mycobacterium tuberculosis",
                microbeAliases=["结核分枝杆菌"],
                diseaseId="disease-tuberculosis",
                diseaseName="结核病",
                relationType="established_pathogen",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="结核分枝杆菌",
            disease_name="结核病",
            relation_type="established_pathogen",
        )
        == "结核分枝杆菌可引起结核病。"
    )


def test_exact_taxon_infection_diagnostic_consensus_supports_cautious_relation():
    record = EvidenceRecord(
        evidenceKey="mtb-infection-diagnostic-consensus",
        category="guideline",
        title="结核分枝杆菌感染检测技术应用专家共识",
        source="中国防痨协会",
        summary=(
            "结核分枝杆菌感染检测是诊断结核分枝杆菌潜伏感染和辅助诊断结核病的重要手段。"
        ),
        interpretation={
            "claim": "结核分枝杆菌感染检测技术是辅助诊断结核病的重要手段。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="mtb-tuberculosis-diagnostic-binding",
                microbeId="taxon-mtb",
                microbeName="Mycobacterium tuberculosis",
                microbeAliases=["结核分枝杆菌"],
                diseaseId="disease-tuberculosis",
                diseaseName="结核病",
                relationType="established_pathogen",
                claim="结核分枝杆菌感染检测技术是辅助诊断结核病的重要手段。",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="结核分枝杆菌",
            disease_name="结核病",
            relation_type="established_pathogen",
        )
        == "结核分枝杆菌与结核病相关。"
    )


def test_source_relation_survives_when_model_omits_condition_from_claim():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-mtb",
        name="结核分枝杆菌",
        scientificName="Mycobacterium tuberculosis",
        category="pathogenic_signal",
        sourceFinding={"status": "检出"},
    )
    microbes = AbnormalMicrobesSection(items=[microbe], summary="")
    record = EvidenceRecord(
        evidenceKey="mtb-source-over-model",
        category="guideline",
        title="结核分枝杆菌感染检测技术应用专家共识",
        source="中国防痨协会",
        summary=("结核分枝杆菌感染检测是诊断潜伏感染和辅助诊断结核病的重要手段。"),
        intents=["microbe_discovery"],
        targets=["Mycobacterium tuberculosis / 结核分枝杆菌"],
        microbeIds=[microbe.microbe_id],
        interpretation={
            "claim": "该共识介绍感染检测技术，但未归纳具体疾病关系。",
            "targetAlignment": "indirect",
            "taxonAlignment": "species",
            "sampleAlignment": "other",
            "patientApplicability": "contextual",
        },
    )

    promote_discovery_conditions_to_bindings([record], microbes)
    validate_relation_bindings([record])

    assert [binding.disease_name for binding in record.relation_bindings] == ["结核病"]
    assert (
        _public_relation_description(
            record,
            microbe_name=microbe.name,
            disease_name="结核病",
            relation_type="epidemiologic_association",
        )
        == "结核分枝杆菌与结核病相关。"
    )


def test_english_erysipeloid_source_validates_chinese_relation_binding():
    microbe = AbnormalMicrobeItem(
        microbeId="taxon-erysipelothrix",
        name="猪红斑丹毒丝菌",
        scientificName="Erysipelothrix rhusiopathiae",
        category="pathogenic_signal",
        sourceFinding={"status": "超标"},
    )
    microbes = AbnormalMicrobesSection(items=[microbe], summary="")
    record = EvidenceRecord(
        evidenceKey="erysipelothrix-human-erysipeloid",
        category="literature",
        title="Pathogenicity of Erysipelothrix rhusiopathiae",
        source="Journal",
        summary=(
            "Erysipelothrix rhusiopathiae is the causative agent of "
            "erysipeloid in humans."
        ),
        intents=["microbe_clinical_significance"],
        targets=["Erysipelothrix rhusiopathiae / 猪红斑丹毒丝菌"],
        microbeIds=[microbe.microbe_id],
        interpretation={
            "claim": "猪红斑丹毒丝菌是人类类丹毒的病原体。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "sampleAlignment": "other",
            "patientApplicability": "contextual",
            "conditions": [
                {
                    "name": "类丹毒",
                    "searchTerm": "erysipeloid",
                    "sourcePhrase": "erysipeloid",
                    "relationType": "established_pathogen",
                }
            ],
        },
    )

    promote_discovery_conditions_to_bindings([record], microbes)
    validate_relation_bindings([record])

    assert [binding.disease_name for binding in record.relation_bindings] == ["类丹毒"]
    assert (
        _public_relation_description(
            record,
            microbe_name=microbe.name,
            disease_name="类丹毒",
            relation_type="established_pathogen",
        )
        == "猪红斑丹毒丝菌是类丹毒的病原体。"
    )


def test_unrelated_ntm_diagnostic_antigen_does_not_inherit_tuberculosis_relation():
    record = EvidenceRecord(
        evidenceKey="fortuitum-tuberculosis-serology",
        category="literature",
        title=(
            "Comparison of antigens from Mycobacterium tuberculosis and "
            "Mycobacterium fortuitum for serology in tuberculosis patients"
        ),
        source="Journal",
        summary=(
            "An antigen from Mycobacterium fortuitum was evaluated for "
            "serology in tuberculosis patients."
        ),
        interpretation={
            "claim": "偶发分枝杆菌抗原用于结核病血清学检测。",
            "targetAlignment": "direct",
            "taxonAlignment": "species",
            "patientApplicability": "contextual",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId="fortuitum-tuberculosis-diagnostic-binding",
                microbeId="taxon-fortuitum",
                microbeName="Mycobacterium fortuitum",
                microbeAliases=["偶发分枝杆菌"],
                diseaseId="disease-tuberculosis",
                diseaseName="结核病",
                relationType="established_pathogen",
                claim="偶发分枝杆菌抗原用于结核病血清学检测。",
            )
        ],
    )

    assert (
        _public_relation_description(
            record,
            microbe_name="偶发分枝杆菌",
            disease_name="结核病",
            relation_type="established_pathogen",
        )
        == ""
    )


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        ("阿尔兹海默症", "miR-19b-3p通过PTEN mRNA水平参与阿尔兹海默症。"),
        ("类风湿性关节炎", "类风湿性关节炎患者常见足部问题。"),
        ("癫痫", "癫痫需结合相关资料，因此，以指导临床。"),
    ),
)
def test_disease_claim_rejects_research_metric_narrow_problem_and_broken_tail(
    target, claim
):
    record = EvidenceRecord(
        evidenceKey=f"manual-audit-{stable_id('claim', target + claim)}",
        category="literature",
        title=f"{target} clinical evidence",
        source="Journal",
        targets=[target],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )

    assert _public_disease_claim(record) is None


def test_constipation_definition_drops_unrelated_colorectal_cancer_clause():
    record = EvidenceRecord(
        evidenceKey="constipation-definition-clean",
        category="guideline",
        title="慢性便秘临床指南",
        source="Society",
        targets=["便秘"],
        interpretation={
            "claim": (
                "便秘是以排便次数减少、排便困难或粪便干硬为特征的症候，"
                "与结直肠恶性肿瘤可能存在关联。"
            ),
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(record) == (
        "便秘是以排便次数减少、排便困难或粪便干硬为特征的症候。"
    )


def test_evaluation_output_removes_comparison_tail_and_rejects_off_target_pancreatitis():
    diabetes = DiseaseRiskItem(
        diseaseId="disease-diabetes",
        name="II型糖尿病",
        sourceType="source_report",
    )
    lipids = DiseaseRiskItem(
        diseaseId="disease-lipids",
        name="高脂血症",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=diabetes,
            evaluation_name="糖化血红蛋白（HbA1c）",
            source_rationale=(
                "用于筛查II型糖尿病及中间高血糖，与空腹血糖相比预测价值较低。"
            ),
            purpose="screen_target",
        )
        == "用于筛查II型糖尿病及中间高血糖。"
    )
    assert not evaluation_is_public_for_disease(
        "血脂检测",
        rationale="用于评估高脂血症性急性胰腺炎。",
        disease=lipids,
    )


def test_disease_evaluation_rejects_special_population_and_wrong_outcome():
    coronary = DiseaseRiskItem(
        diseaseId="disease-chd",
        name="冠心病",
        sourceType="source_report",
    )
    adenoma = DiseaseRiskItem(
        diseaseId="disease-adenoma",
        name="腺瘤",
        sourceType="source_report",
    )

    assert not evaluation_is_public_for_disease(
        "心电图",
        rationale="用于精神压力试验前后对比ST段改变，以诊断MSIMI。",
        disease=coronary,
    )
    assert not evaluation_is_public_for_disease(
        "动态心电图检查",
        rationale="用于筛查糖尿病患者的冠心病。",
        disease=coronary,
    )
    assert not evaluation_is_public_for_disease(
        "免疫法粪便隐血试验",
        rationale="用于筛查结直肠癌。",
        disease=adenoma,
    )
    assert evaluation_is_public_for_disease(
        "结肠镜检查",
        rationale="用于识别和确认结直肠腺瘤。",
        disease=adenoma,
    )
    assert (
        _evaluation_rationale_for_report(
            disease=adenoma,
            evaluation_name="免疫法粪便隐血试验",
            source_rationale="用于腺瘤的筛查。",
            purpose="screen_target",
            source_text=(
                "结直肠腺瘤病史属于结肠癌高危因素。"
                "免疫法粪便隐血试验用于结肠癌筛查。"
            ),
        )
        is None
    )
    assert (
        _evaluation_rationale_for_report(
            disease=adenoma,
            evaluation_name="结肠镜检查",
            source_rationale="用于腺瘤的筛查。",
            purpose="screen_target",
            source_text="结肠镜检查用于结直肠腺瘤筛查和诊断。",
        )
        == "用于辅助腺瘤诊断。"
    )


def test_rheumatoid_factor_names_share_one_evaluation_identity():
    assert _evaluation_identity("类风湿因子") == "rf"
    assert _evaluation_identity("类风湿因子检测") == "rf"


def test_relation_output_rejects_single_case_and_procedure_specific_sources():
    for title, summary in (
        (
            "Mycobacterium fortuitum prosthetic valve endocarditis",
            "We describe a patient with prosthetic valve endocarditis.",
        ),
        (
            "Post cardiac surgery Mycobacterium fortuitum infections",
            "An outbreak involved sternal wound infection after cardiac surgery.",
        ),
        (
            "Latent tuberculosis during immunosuppressive therapy",
            "免疫抑制剂使用期间可发生潜伏感染再激活。",
        ),
    ):
        record = EvidenceRecord(
            evidenceKey=stable_id("evidence", title),
            category="literature",
            title=title,
            source="Journal",
            summary=summary,
            interpretation={
                "claim": "偶发分枝杆菌可引起心内膜炎。",
                "targetAlignment": "direct",
            },
            relationBindings=[
                EvidenceRelationBinding(
                    relationId=stable_id("relation", title),
                    microbeId="taxon-mf",
                    microbeName="Mycobacterium fortuitum",
                    diseaseId="disease-endocarditis",
                    diseaseName="心内膜炎",
                    relationType="established_pathogen",
                    claim="偶发分枝杆菌可引起心内膜炎。",
                )
            ],
        )
        assert not _public_relation_description(
            record,
            microbe_name="偶发分枝杆菌",
            disease_name="心内膜炎",
            relation_type="established_pathogen",
        )


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        ("甲状腺癌", "甲状腺癌手术切除标本在病理科标本中占比较大。"),
        ("焦虑症", "焦虑症与生活节奏紧张相关，并可发展为严重抑郁症。"),
        ("胃癌", "胃癌的分类使用T、N、M编码及淋巴结分站。"),
        ("肝癌", "肝癌m6A RNA甲基化调控因子风险预测模型与预后相关。"),
        ("腹泻型肠易激综合征", "腹泻型肠易激综合征粪便钙卫蛋白与健康对照无显著差异。"),
        ("胃癌", "胃癌常转移到腹腔。"),
        ("肝癌", "肝癌是我国第5位常见肿瘤及第2位肿瘤致死病因。"),
    ),
)
def test_manual_audit_rejects_non_explanatory_disease_prose(target, claim):
    record = EvidenceRecord(
        evidenceKey=stable_id("audit", target + claim),
        category="literature",
        title=f"{target} evidence",
        source="Journal",
        targets=[target],
        interpretation={"claim": claim, "targetAlignment": "direct"},
    )
    assert _public_disease_claim(record) is None


def test_evaluation_rationale_removes_model_process_tail_and_panss_diagnosis():
    thyroid = DiseaseRiskItem(
        diseaseId="disease-thyroid",
        name="甲状腺癌",
        sourceType="source_report",
    )
    colorectal = DiseaseRiskItem(
        diseaseId="disease-crc",
        name="结直肠癌",
        sourceType="source_report",
    )
    schizophrenia = DiseaseRiskItem(
        diseaseId="disease-schizophrenia",
        name="精神分裂症",
        sourceType="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=thyroid,
            evaluation_name="超声引导下细针穿刺活检",
            source_rationale=(
                "用于甲状腺结节性质的判断，直接针对甲状腺癌的确认或筛查。"
            ),
            purpose="confirm_target",
        )
        == "用于甲状腺结节性质的判断。"
    )
    constipation = DiseaseRiskItem(
        diseaseId="disease-constipation-purpose",
        name="便秘",
        sourceType="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=constipation,
            evaluation_name="肛门直肠测压",
            source_rationale="用于定义便秘的病理生理亚型，直接针对便秘的诊断。",
            purpose="characterize_target",
        )
        == "用于定义便秘的病理生理亚型。"
    )
    assert (
        _evaluation_rationale_for_report(
            disease=colorectal,
            evaluation_name="CT结肠成像",
            source_rationale="认为无对比CT结肠成像通常适用于结直肠癌筛查。",
            purpose="screen_target",
        )
        == "无对比CT结肠成像通常适用于结直肠癌筛查。"
    )
    assert not evaluation_is_public_for_disease(
        "PANSS量表评估",
        rationale="PANSS条目可用于识别症状亚型并支持诊断。",
        disease=schizophrenia,
    )


@pytest.mark.parametrize(
    ("title", "summary"),
    (
        ("异烟肼耐药结核病治疗指南", "用于异烟肼耐药结核病患者。"),
        (
            "665株鲍曼复合醋酸钙不动杆菌临床分布及耐药分析",
            "研究醋酸钙不动杆菌复合体的临床分布。",
        ),
        (
            "Catheter infection due to A. johnsonii",
            "A. johnsonii, formerly Acinetobacter calcoaceticus var. lwoffii, was isolated.",
        ),
    ),
)
def test_relation_output_rejects_resistant_subtype_complex_and_old_taxonomy(
    title: str,
    summary: str,
):
    record = EvidenceRecord(
        evidenceKey=stable_id("narrow-relation", title),
        category="guideline",
        title=title,
        source="EviMed",
        summary=summary,
        interpretation={
            "claim": "醋酸钙不动杆菌可引起感染。",
            "targetAlignment": "direct",
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId=stable_id("relation", title),
                microbeId="taxon-target",
                microbeName="Acinetobacter calcoaceticus",
                diseaseId="disease-infection",
                diseaseName="感染",
                relationType="established_pathogen",
                claim="醋酸钙不动杆菌可引起感染。",
            )
        ],
    )
    assert not _public_relation_description(
        record,
        microbe_name="醋酸钙不动杆菌",
        disease_name="感染",
        relation_type="established_pathogen",
    )


def test_baumannii_relation_rejects_mdr_icu_source_but_keeps_community_pneumonia():
    def record(title: str, summary: str, key: str) -> EvidenceRecord:
        return EvidenceRecord(
            evidenceKey=key,
            category="literature",
            title=title,
            source="EviMed",
            summary=summary,
            interpretation={
                "claim": "鲍曼不动杆菌可引起肺炎。",
                "targetAlignment": "direct",
            },
            relationBindings=[
                EvidenceRelationBinding(
                    relationId=f"relation-{key}",
                    microbeId="taxon-baumannii",
                    microbeName="Acinetobacter baumannii",
                    diseaseId="disease-pneumonia",
                    diseaseName="肺炎",
                    relationType="established_pathogen",
                    claim="鲍曼不动杆菌可引起肺炎。",
                )
            ],
        )

    mdr_icu = record(
        "Risk Factors and Drug Resistance of the MDR Acinetobacter Baumannii in Pneumonia Patients in ICU",
        "MDR Acinetobacter baumannii was studied in pneumonia patients in ICU.",
        "baumannii-mdr-icu",
    )
    community = record(
        "Severe community-acquired pneumonia due to Acinetobacter baumannii",
        "Acinetobacter baumannii caused community-acquired pneumonia; some patients later required ICU care.",
        "baumannii-community-pneumonia",
    )

    assert not _public_relation_description(
        mdr_icu,
        microbe_name="鲍曼不动杆菌",
        disease_name="肺炎",
        relation_type="established_pathogen",
    )
    assert _public_relation_description(
        community,
        microbe_name="鲍曼不动杆菌",
        disease_name="肺炎",
        relation_type="established_pathogen",
    )


def test_disease_definition_is_not_published_as_microbiome_risk_rationale():
    disease = DiseaseRiskItem(
        diseaseId="disease-constipation-definition",
        name="便秘",
        sourceType="source_report",
    )
    diseases = DiseaseRisksSection(items=[disease])
    microbes = AbnormalMicrobesSection(items=[])
    definition = EvidenceRecord(
        evidenceKey="constipation-definition-source",
        category="guideline",
        title="慢性便秘共识",
        source="EviMed",
        supports=[disease.disease_id],
        diseaseIds=[disease.disease_id],
        intents=["disease_clinical_significance"],
        targets=["便秘"],
        interpretation={
            "claim": "慢性便秘是一种常见的消化系统疾病，可表现为排便次数减少、粪便干硬或排便困难。",
            "targetAlignment": "direct",
        },
    )
    diagnostic = EvidenceRecord(
        evidenceKey="constipation-diagnostic-process",
        category="guideline",
        title="便秘诊断评估",
        source="EviMed",
        supports=[disease.disease_id],
        diseaseIds=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=["便秘"],
        interpretation={
            "claim": "便秘严重程度量表等工具可用，结肠传输时间和肛门直肠测压可定义病理生理亚型。",
            "targetAlignment": "direct",
        },
    )

    attach_evidence_refs(
        microbes,
        diseases,
        [definition, diagnostic],
        {definition.evidence_key: 1, diagnostic.evidence_key: 2},
    )

    assert disease.medical_interpretation is None
    assert disease.association_evidence_refs == []
    assert disease.confirmation_evidence_refs == [2]


def test_title_like_disease_claim_is_not_public_medical_meaning():
    record = EvidenceRecord(
        evidenceKey="crc-title-only",
        category="guideline",
        title="NICE colorectal cancer guidance",
        source="EviMed",
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌NICE指南概要。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) is None


@pytest.mark.parametrize(
    ("target", "claim"),
    (
        (
            "便秘",
            "便秘是一种慢性疾病，影响生活质量，其定义、分类、流行病学及病理生理已有明确描述。",
        ),
        ("癫痫", "癫痫的实用临床定义。"),
        (
            "类风湿性关节炎",
            "类风湿性关节炎对患者的心理有巨大的影响，内容涉及一般原则和照护过程。",
        ),
        ("高血压", "高血压是澳大利亚心血管疾病及全因死亡的首要风险因素。"),
        ("肾结石", "肾结石发病高峰年龄为20至40岁，男女比例为3:2。"),
        ("胃癌", "胃癌术后并发症与Bormann III/IV型肿瘤和住院时间延长相关。"),
        ("肝癌", "肝癌是中国常见的恶性肿瘤，每年约有50%的新发病例。"),
        ("高血压", "高血压病痰瘀互结证的临床特征包括头重如裹、胸闷和舌苔腻。"),
        (
            "肝硬化",
            "肝硬化患者中，肝脏硬度和肝功能指标与心电图异常的存在相关，血小板计数也有差异。",
        ),
        (
            "类风湿性关节炎",
            "类风湿性关节炎对患者心理有巨大影响，患者可能伴随抑郁、焦虑症状及睡眠问题。",
        ),
        ("类风湿性关节炎", "类风湿性关节炎：中足受累是影响步行功能残疾的风险因素。"),
        ("类风湿性关节炎", "类风湿性关节炎患者血清A-CCP、RF、CRP水平异常升高。"),
        (
            "腹泻型肠易激综合征",
            "腹泻型肠易激综合征以慢性水样腹泻为特征，病程至少持续四周。",
        ),
    ),
)
def test_public_disease_claim_rejects_metadata_population_or_narrow_cohort_text(
    target: str,
    claim: str,
):
    record = EvidenceRecord(
        evidenceKey=f"unsafe-human-audit-{stable_id('disease', target)}",
        category="literature",
        title=f"{target} evidence",
        source="EviMed",
        targets=[target],
        interpretation={
            "claim": claim,
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) is None


def test_public_disease_claim_removes_fourth_high_slogan_but_keeps_medical_risk():
    record = EvidenceRecord(
        evidenceKey="hyperuricemia-with-slogan",
        category="guideline",
        title="高尿酸血症医学证据",
        source="EviMed",
        targets=["高尿酸血症"],
        interpretation={
            "claim": (
                "高尿酸血症是一种慢性代谢异常，"
                "并已被列为继高血压、高血脂、高血糖之后的第四高，"
                "可增加痛风风险。"
            ),
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _public_disease_claim(record) == (
        "高尿酸血症是一种慢性代谢异常，可增加痛风风险。"
    )


def test_public_disease_claim_removes_four_high_enumeration_and_screening_population():
    hyperuricemia = EvidenceRecord(
        evidenceKey="hyperuricemia-four-high-enumeration",
        category="literature",
        title="高尿酸血症医学证据",
        source="EviMed",
        targets=["高尿酸血症"],
        interpretation={
            "claim": (
                "高尿酸血症是一种系统性代谢疾病，是痛风的主要危险因素，"
                "并与高血压、高血脂、高血糖并称“四高”。"
            ),
            "targetAlignment": "direct",
        },
    )
    colorectal = EvidenceRecord(
        evidenceKey="crc-screening-population-tail",
        category="guideline",
        title="结直肠癌筛查指南",
        source="EviMed",
        targets=["结直肠癌"],
        interpretation={
            "claim": "结直肠癌是发生于结直肠部位的恶性肿瘤，在无症状平均风险成人中。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(hyperuricemia) == (
        "高尿酸血症是一种系统性代谢疾病，是痛风的主要危险因素。"
    )
    assert _public_disease_claim(colorectal) == (
        "结直肠癌是发生于结直肠部位的恶性肿瘤。"
    )


def test_multiple_sclerosis_rejects_nmosd_specific_evaluation():
    disease = DiseaseRiskItem(
        diseaseId="disease-ms",
        name="多发性硬化症",
        sourceType="source_report",
    )

    assert not evaluation_is_public_for_disease(
        "脑部MRI检查",
        rationale="用于筛查NMOSD的特征性MRI表现。",
        disease=disease,
    )
    assert evaluation_is_public_for_disease(
        "脑部MRI检查",
        rationale="用于识别多发性硬化症的脑部脱髓鞘病灶。",
        disease=disease,
    )
    assert not evaluation_is_public_for_disease(
        "7T MRI检查",
        rationale="用于多发性硬化症的诊断和监测。",
        disease=disease,
    )


def test_target_evaluation_rejects_exclusion_and_wrong_biopsy_site():
    epilepsy = DiseaseRiskItem(
        diseaseId="disease-epilepsy",
        name="癫痫",
        sourceType="source_report",
    )
    ibd = DiseaseRiskItem(
        diseaseId="disease-ibd",
        name="炎症性肠病",
        sourceType="source_report",
    )

    assert not evaluation_is_public_for_disease(
        "脑电图",
        rationale="用于排除癫痫。",
        disease=epilepsy,
    )
    assert evaluation_is_public_for_disease(
        "脑电图",
        rationale="用于记录癫痫样放电并辅助癫痫分型。",
        disease=epilepsy,
    )
    assert not evaluation_is_public_for_disease(
        "皮肤活检",
        rationale="用于评估炎症性肠病相关皮损。",
        disease=ibd,
    )
    assert evaluation_is_public_for_disease(
        "结肠镜下黏膜活检",
        rationale="用于评估肠黏膜炎症及病理特征。",
        disease=ibd,
    )
    depression = DiseaseRiskItem(
        diseaseId="disease-depression-age",
        name="抑郁症",
        sourceType="source_report",
    )
    assert not evaluation_is_public_for_disease(
        "老年抑郁量表",
        rationale="用于筛查老年人抑郁症状。",
        disease=depression,
        subject_age=28,
    )
    assert not evaluation_is_public_for_disease(
        "老年抑郁量表",
        rationale="用于筛查老年人抑郁症状。",
        disease=depression,
        subject_age=70,
    )


def test_epidemiologic_relation_requires_more_than_title_co_mention():
    binding = EvidenceRelationBinding(
        relationId="relation-co-mention",
        microbeId="taxon-pyricularia",
        microbeName="Pyricularia pennisetigena",
        diseaseId="disease-tb",
        diseaseName="结核病",
        relationType="epidemiologic_association",
        claim="Pyricularia pennisetigena与结核病相关。",
    )
    co_mention = EvidenceRecord(
        evidenceKey="pyricularia-tb-co-mention",
        category="literature",
        title="Pyricularia pennisetigena and tuberculosis patients",
        source="EviMed",
        summary="The organism was included in a sequencing panel.",
    )
    associated = EvidenceRecord(
        evidenceKey="pyricularia-tb-associated",
        category="literature",
        title=("Pyricularia pennisetigena abundance associated with tuberculosis"),
        source="EviMed",
    )
    related = EvidenceRecord(
        evidenceKey="pyricularia-tb-related",
        category="literature",
        title="Pyricularia pennisetigena is related to tuberculosis",
        source="EviMed",
        summary="A human clinical cohort reported this relationship.",
    )

    assert not _source_explicitly_supports_relation(co_mention, binding)
    assert _source_explicitly_supports_relation(associated, binding)
    assert _source_explicitly_supports_relation(related, binding)

    related_query = EvidenceQuery(
        "Pyricularia pennisetigena tuberculosis",
        ("taxon-pyricularia",),
        (("Pyricularia pennisetigena",), ("tuberculosis",)),
        intent="microbe_disease_association",
        screen_profile="pathogen_relation",
        target="Pyricularia pennisetigena / 结核病",
    )
    assert _screen(related, query=related_query)[0] is True

    subtype_only = EvidenceRecord(
        evidenceKey="etbf-crc",
        category="literature",
        title="Enterotoxigenic Bacteroides fragilis and colorectal cancer",
        source="EviMed",
        summary=(
            "Enterotoxigenic Bacteroides fragilis produces BFT encoded by the "
            "bft gene and is associated with colorectal cancer."
        ),
        targets=["Bacteroides fragilis / 脆弱拟杆菌 / 结直肠癌"],
    )
    generic_species_binding = EvidenceRelationBinding(
        relationId="relation-bf-crc",
        microbeId="taxon-bf",
        microbeName="Bacteroides fragilis",
        microbeAliases=["脆弱拟杆菌"],
        diseaseId="disease-crc",
        diseaseName="结直肠癌",
        relationType="epidemiologic_association",
    )
    assert not _source_explicitly_supports_relation(
        subtype_only,
        generic_species_binding,
    )

    comparator_only = EvidenceRecord(
        evidenceKey="pneumococcus-comparator-only",
        category="guideline",
        title="Adult Mycoplasma pneumoniae pneumonia consensus",
        source="EviMed",
        summary=(
            "Mycoplasma pneumoniae causes community-acquired pneumonia and is "
            "more prevalent than Streptococcus pneumoniae."
        ),
    )
    pneumococcus_binding = EvidenceRelationBinding(
        relationId="relation-pneumococcus-pneumonia",
        microbeId="taxon-pneumococcus",
        microbeName="Streptococcus pneumoniae",
        microbeAliases=["肺炎链球菌"],
        diseaseId="disease-pneumonia",
        diseaseName="肺炎",
        relationType="established_pathogen",
        claim="肺炎链球菌可引起肺炎。",
    )
    assert not _source_explicitly_supports_relation(
        comparator_only,
        pneumococcus_binding,
    )

    pulmonary_microbiome = EvidenceRecord(
        evidenceKey="pyricularia-tb-lung-microbiome",
        category="literature",
        title=(
            "Pathogen Metagenomics Reveals Distinct Lung Microbiota Signatures "
            "Between Bacteriologically Confirmed and Negative Tuberculosis Patients"
        ),
        source="EviMed",
        summary=(
            "In addition to Mycobacterium tuberculosis, Pyricularia "
            "pennisetigena was enriched in the bacteriological confirmation group."
        ),
    )
    pathogen_binding = binding.model_copy(
        update={"relation_type": "established_pathogen"}
    )
    assert not _source_explicitly_supports_relation(
        pulmonary_microbiome,
        pathogen_binding,
    )

    ntm_definition = EvidenceRecord(
        evidenceKey="ntm-definition-not-mtb-relation",
        category="guideline",
        title="中国皮肤非结核分枝杆菌病诊治专家共识",
        source="EviMed",
        summary=(
            "非结核分枝杆菌系指除结核分枝杆菌复合群以外的分枝杆菌，"
            "可引起皮肤及软组织感染。"
        ),
    )
    mtb_skin_binding = EvidenceRelationBinding(
        relationId="relation-mtb-skin",
        microbeId="taxon-mtb",
        microbeName="Mycobacterium tuberculosis",
        diseaseId="disease-skin-infection",
        diseaseName="皮肤及软组织感染",
        relationType="established_pathogen",
        claim="结核分枝杆菌可引起皮肤及软组织感染。",
    )
    assert not _source_explicitly_supports_relation(
        ntm_definition,
        mtb_skin_binding,
    )


def test_deep_learning_automatic_diagnosis_is_not_a_clinical_evaluation_purpose():
    disease = DiseaseRiskItem(
        diseaseId="disease-alzheimer",
        name="阿尔兹海默症",
        sourceType="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="脑部MRI检查",
            source_rationale="该检查用于通过海马体区域识别阿尔兹海默症。",
            purpose="confirm_target",
            source_text="基于深度学习的阿尔兹海默症自动诊断方法研究，使用脑部MRI检查。",
        )
        is None
    )


def test_diagnostic_false_negative_source_is_not_a_disease_relation():
    claim = "非烟曲霉曲霉属感染与慢性肺曲霉病患者曲霉沉淀素试验阴性相关。"
    record = EvidenceRecord(
        evidenceKey="aspergillus-negative-test",
        category="literature",
        title=(
            "Non-fumigatus Aspergillus Infection Associated with a Negative "
            "Aspergillus Precipitin Test in Chronic Pulmonary Aspergillosis"
        ),
        source="EviMed",
        summary=(
            "Non-fumigatus Aspergillus species were associated with false-negative "
            "precipitin test results in chronic pulmonary aspergillosis."
        ),
        interpretation={"claim": claim, "targetAlignment": "direct"},
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-aspergillus-test",
                microbeId="taxon-aspergillus",
                microbeName="Aspergillus",
                diseaseId="disease-aspergillosis",
                diseaseName="曲霉病",
                relationType="opportunistic",
                claim=claim,
            )
        ],
    )

    assert not _public_relation_description(
        record,
        microbe_name="曲霉属",
        disease_name="曲霉病",
        relation_type="opportunistic",
    )


def test_pathogen_ranking_is_reduced_to_a_stable_relation_statement():
    claim = (
        "肺炎链球菌是成人社区获得性肺炎的首要致病原，"
        "但肺炎支原体已超过肺炎链球菌成为成人CAP的首要致病原。"
    )
    record = EvidenceRecord(
        evidenceKey="pneumococcus-ranking",
        category="literature",
        title="Streptococcus pneumoniae pneumonia in adults",
        source="EviMed",
        summary=claim,
        interpretation={"claim": claim, "targetAlignment": "direct"},
        relationBindings=[
            EvidenceRelationBinding(
                relationId="relation-pneumococcus-pneumonia",
                microbeId="taxon-pneumococcus",
                microbeName="Streptococcus pneumoniae",
                microbeAliases=["肺炎链球菌"],
                diseaseId="disease-pneumonia",
                diseaseName="肺炎",
                relationType="established_pathogen",
                claim=claim,
            )
        ],
    )

    description = _public_relation_description(
        record,
        microbe_name="肺炎链球菌",
        disease_name="肺炎",
        relation_type="established_pathogen",
    )

    assert description == "肺炎链球菌与肺炎相关。"
    assert "首要" not in description
    assert "超过" not in description


def test_special_population_anxiety_scale_is_not_a_general_evaluation():
    disease = DiseaseRiskItem(
        diseaseId="disease-anxiety",
        name="焦虑症",
        sourceType="source_report",
    )

    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="GAD7量表",
            source_rationale="该量表用于焦虑症状的筛查。",
            purpose="screen_target",
            source_text="Screening in adults with Duchenne muscular dystrophy using GAD7.",
        )
        is None
    )


def test_diagnostic_evaluation_prefers_disease_specific_ct_purpose():
    disease = DiseaseRiskItem(
        diseaseId="disease-kidney-stone",
        name="肾结石",
        sourceType="source_report",
    )
    generic = EvidenceRecord(
        evidenceKey="kidney-stone-generic-ct",
        category="guideline",
        title="肾结石临床诊断指南",
        source="EviMed",
        relevance=1.0,
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=[disease.name],
        interpretation={
            "claim": "指南讨论肾结石诊断。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
            "confidence": "low",
            "recommendedEvaluations": [
                {
                    "name": "非增强螺旋CT",
                    "rationale": "影像学检查方面，非增强螺旋CT是急性腰部疼痛的首选方法，但尚无金标准诊断试验。",
                    "sourcePhrase": "非增强螺旋CT",
                    "purpose": "screen_target",
                }
            ],
        },
    )
    specific = EvidenceRecord(
        evidenceKey="kidney-stone-specific-ct",
        category="guideline",
        title="肾结石放射诊断指南",
        source="EviMed",
        summary=("指南指出，非对比螺旋CT在检测肾结石方面具有最高的敏感性和特异性。"),
        relevance=1.0,
        supports=[disease.disease_id],
        intents=["diagnostic_guideline"],
        targets=[disease.name],
        interpretation={
            "claim": "指南讨论肾结石诊断。",
            "targetAlignment": "direct",
            "patientApplicability": "direct",
            "confidence": "low",
            "recommendedEvaluations": [
                {
                    "name": "非对比螺旋CT扫描",
                    "rationale": "指南指出其具有最高敏感性和特异性。",
                    "sourcePhrase": "非对比螺旋CT扫描",
                    "purpose": "screen_target",
                }
            ],
        },
    )

    attach_suggested_evaluations(
        DiseaseRisksSection(items=[disease]),
        [generic, specific],
        {generic.evidence_key: 1, specific.evidence_key: 2},
    )

    assert len(disease.suggested_evaluations) == 1
    assert disease.suggested_evaluations[0].name == "非对比螺旋CT扫描"
    assert disease.suggested_evaluations[0].purpose == "确认"
    assert disease.suggested_evaluations[0].evidence_refs == [2]
    assert disease.suggested_evaluations[0].rationale == (
        "非对比螺旋CT扫描用于检测肾结石，具有最高的敏感性和特异性。"
    )


def test_kidney_stone_guide_generic_ultrasound_term_recovers_common_exam():
    disease = DiseaseRiskItem(
        diseaseId="disease-kidney-stone",
        name="肾结石",
        sourceType="source_report",
    )
    record = EvidenceRecord(
        evidenceKey="kidney-stone-ultrasound-guide",
        category="guideline",
        title=(
            "ACR Appropriateness Criteria acute onset flank pain - "
            "suspicion of stone disease (urolithiasis)"
        ),
        source="EviMed",
        summary=(
            "The guideline outlines diagnostic imaging procedures such as "
            "ultrasound for clinical evaluation."
        ),
    )

    evaluations = _source_bound_fallback_evaluations(
        disease,
        record,
        subject_age=30,
    )

    assert [item.name for item in evaluations] == ["泌尿系超声检查"]
    assert evaluations[0].rationale == "用于评估肾结石。"


def test_thyroid_fna_names_share_one_evaluation_identity():
    assert _evaluation_identity("超声引导下甲状腺结节细针穿刺活检") == (
        _evaluation_identity("甲状腺结节FNAB")
    )


def test_homa_ir_names_share_one_evaluation_identity():
    assert _evaluation_identity("HOMA-IR指数") == _evaluation_identity("HOMA-IR")


@pytest.mark.parametrize(
    ("disease_name", "evaluation_name", "source_rationale", "expected"),
    (
        (
            "II型糖尿病",
            "空腹血糖",
            "指南推荐使用空腹血糖进行糖尿病筛查。",
            "空腹血糖用于糖尿病筛查。",
        ),
        (
            "非酒精性脂肪肝",
            "FIB-4评分",
            "指南建议使用FIB-4评分评估肝纤维化。",
            "FIB-4评分用于评估肝纤维化。",
        ),
        (
            "甲状腺癌",
            "FNAB",
            "该共识直接推荐FNAB用于甲状腺结节性质判断。",
            "FNAB用于甲状腺结节性质判断。",
        ),
    ),
)
def test_guideline_wrapper_is_removed_without_dropping_test_purpose(
    disease_name: str,
    evaluation_name: str,
    source_rationale: str,
    expected: str,
):
    disease = DiseaseRiskItem(
        diseaseId=f"disease-{disease_name}",
        name=disease_name,
        sourceType="source_report",
    )
    assert evaluation_is_public_for_disease(
        evaluation_name,
        rationale=source_rationale,
        disease=disease,
    )
    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name=evaluation_name,
            source_rationale=source_rationale,
            purpose="confirm_target",
        )
        == expected
    )


def test_segmentation_algorithm_is_not_a_clinical_confirmation_path():
    disease = DiseaseRiskItem(
        diseaseId="disease-ms",
        name="多发性硬化症",
        sourceType="source_report",
    )
    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name="脑部MRI",
            source_rationale=(
                "该检查用于多发性硬化症脑部病变的识别与分割，是诊断和监测的影像学基础。"
            ),
            purpose="confirm_target",
            source_text="多发性硬化症脑部MRI研究中病变分割算法。",
        )
        is None
    )


@pytest.mark.parametrize(
    ("disease_name", "evaluation_name", "source_rationale", "source_text", "expected"),
    (
        (
            "系统性红斑狼疮",
            "抗核抗体（ANA）检测",
            "该检查用于确认系统性红斑狼疮的诊断，因为外周血抗核抗体阳性是其典型特征。",
            "",
            "用于确认系统性红斑狼疮的诊断，因为外周血抗核抗体阳性是其典型特征。",
        ),
        (
            "肥胖",
            "体重指数（BMI）测量",
            "该检查用于筛查肥胖，因为指南中提及体重指数作为诊断标准的一部分。",
            "",
            "用于筛查肥胖。",
        ),
        (
            "癫痫",
            "脑电图检查",
            "指南指出脑电图对癫痫的诊断和定位具有重要作用，支持使用脑电图进行癫痫诊断。",
            "",
            "脑电图用于癫痫诊断。",
        ),
        (
            "阿尔兹海默症",
            "神经心理学评估",
            "框架建议对记忆、语言、视空间、执行、行为和功能进行评估，以提高诊断准确性。",
            "",
            "用于评估记忆、语言、视空间、执行、行为和功能。",
        ),
        (
            "肾结石",
            "非增强螺旋CT",
            "指南推荐在急性腰部疼痛情况下使用非增强螺旋CT。",
            "2007CARI肾结石的临床诊断指南。影像学检查方面，非增强螺旋CT是急性腰部疼痛的首选方法。",
            "影像学检查方面，非增强螺旋CT是急性腰部疼痛的首选方法。",
        ),
    ),
)
def test_source_authored_clinical_purpose_survives_editorial_wrapper(
    disease_name: str,
    evaluation_name: str,
    source_rationale: str,
    source_text: str,
    expected: str,
):
    disease = DiseaseRiskItem(
        diseaseId=f"disease-{disease_name}",
        name=disease_name,
        sourceType="source_report",
    )
    assert evaluation_is_public_for_disease(
        evaluation_name,
        rationale=source_rationale,
        disease=disease,
    )
    assert (
        _evaluation_rationale_for_report(
            disease=disease,
            evaluation_name=evaluation_name,
            source_rationale=source_rationale,
            purpose="confirm_target",
            source_text=source_text,
        )
        == expected
    )


def test_visual_induced_epilepsy_subtype_is_not_general_epilepsy_meaning():
    record = EvidenceRecord(
        evidenceKey="visual-epilepsy-subtype",
        category="guideline",
        title="视觉诱发的癫痫发作",
        source="EviMed",
        targets=["癫痫"],
        interpretation={
            "claim": "癫痫患者可发生视觉诱发性发作，相关风险涉及闪光刺激和致痫条纹。",
            "targetAlignment": "direct",
        },
    )
    assert _public_disease_claim(record) is None


def test_hyphenated_special_syndrome_is_not_general_disease_meaning():
    record = EvidenceRecord(
        evidenceKey="parkinson-special-syndrome",
        category="literature",
        title="帕金森-高热综合征的临床特征",
        source="Journal",
        summary="帕金森-高热综合征是停用多巴胺能药物后出现的罕见并发症。",
        targets=["帕金森"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "帕金森-高热综合征是停用多巴胺能药物后出现的罕见并发症。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(record) is None


def test_special_syndrome_source_can_keep_separate_general_disease_sentence():
    record = EvidenceRecord(
        evidenceKey="parkinson-general-and-special",
        category="literature",
        title="帕金森-高热综合征的临床特征",
        source="Journal",
        summary=(
            "帕金森病是一种慢性神经系统疾病，可表现为运动迟缓和震颤。"
            "帕金森-高热综合征是停用多巴胺能药物后出现的罕见并发症。"
        ),
        targets=["帕金森"],
        intents=["disease_clinical_significance"],
        interpretation={
            "claim": "帕金森病是一种慢性神经系统疾病，可表现为运动迟缓和震颤。",
            "targetAlignment": "direct",
        },
    )

    assert _public_disease_claim(record) == (
        "帕金森病是一种慢性神经系统疾病，可表现为运动迟缓和震颤。"
    )
