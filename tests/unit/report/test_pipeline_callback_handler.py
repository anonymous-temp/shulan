import asyncio
import json
import time
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import tornado.testing
import tornado.web

from adapters.report.callback import CallbackClient
from adapters.report.clients import SourceClient
from adapters.report.factory import load_report_session
from modules.report.callback import _attach_patient_related_facts
from modules.report.callback import _callback_disease_risk_rationale_is_present
from modules.report.callback import _clinical_context
from modules.report.callback import _disease_finding
from modules.report.callback import _evaluation_text
from modules.report.callback import _evidence_bound_disease_interpretation
from modules.report.callback import _finding_sort_key
from modules.report.callback import _fixed_callback_body
from modules.report.callback import _fixed_findings
from modules.report.callback import _health_recommendations
from modules.report.callback import _health_recommended_examinations
from modules.report.callback import _hospital_common_examination
from modules.report.callback import _microbe_finding
from modules.report.callback import _patient_health_assessment
from modules.report.callback import _public_summary_conclusion
from modules.report.callback import _renumber_public_evidence
from modules.report.callback import _source_disease_patient_facts
from modules.report.callback import _vary_repeated_disease_suggestion_openings
from modules.report.evidence import EvidenceQuery
from modules.report.narrative import NarrativeError
from modules.report.pipeline import _ACTIVE_PROGRESS
from modules.report.pipeline import ReportPipeline
from modules.report.pipeline import _bounded_priority_public_relation_queries
from modules.report.pipeline import _bounded_untried_diagnostic_queries
from modules.report.pipeline import _clear_evidence_bound_content
from modules.report.pipeline import _content_completeness_reasons
from modules.report.pipeline import _GenerationProgress
from modules.report.pipeline import _interpret_evidence_until
from modules.report.pipeline import _microbe_record_is_public
from modules.report.pipeline import _promote_evidence_diseases_for_followup
from modules.report.pipeline import _public_reference_count
from modules.report.pipeline import _record_matches_subject_demographics
from modules.report.pipeline import _recover_public_eligible_interpretations
from modules.report.pipeline import _relation_record_is_public
from modules.report.pipeline import _retrieve_evidence_until
from modules.report.pipeline import extract_session_context
from modules.report.pipeline import merge_request_session_fallbacks
from modules.report.ports import DownloadedFile
from modules.report.ports import EvidenceServiceError
from modules.report.ports import EviMedChannelResult
from modules.report.schemas import AbnormalMicrobeItem
from modules.report.schemas import AbnormalMicrobesSection
from modules.report.schemas import DiseaseRiskItem
from modules.report.schemas import DiseaseRisksSection
from modules.report.schemas import EvidenceChannelAudit
from modules.report.schemas import EvidenceQueryAudit
from modules.report.schemas import EvidenceRecord
from modules.report.schemas import EvidenceRelationBinding
from modules.report.schemas import FailureCallback
from modules.report.schemas import ReportRequest
from modules.report.schemas import SubjectInfo
from modules.report.task_coordinator import ReportTaskCoordinator
from runtime import TaskSupervisor
from web.handlers.report import ReportGenerateHandler


def test_untried_diagnostic_fallback_uses_next_query_for_same_disease():
    first = EvidenceQuery(
        query="kidney stone ultrasound guideline",
        supports=("disease-kidney-stone",),
        required_concepts=(("kidney stone", "肾结石"),),
        intent="diagnostic_guideline",
        target="肾结石",
        disease_id="disease-kidney-stone",
        screen_profile="diagnostic",
    )
    alternate = EvidenceQuery(
        query="kidney stone noncontrast CT acute flank pain diagnostic guideline",
        supports=("disease-kidney-stone",),
        required_concepts=(("kidney stone", "肾结石"),),
        intent="diagnostic_guideline",
        target="肾结石",
        disease_id="disease-kidney-stone",
        screen_profile="diagnostic",
    )
    attempted = EvidenceQueryAudit(
        queryId=first.query_id,
        queryText=first.query,
        intent="diagnostic_guideline",
        target="肾结石",
        diseaseId="disease-kidney-stone",
    )

    selected = _bounded_untried_diagnostic_queries(
        [first, alternate],
        query_audits=[attempted],
    )

    assert selected == [alternate]


def test_general_diagnostic_guide_is_not_rejected_for_pregnancy_variant():
    record = EvidenceRecord(
        evidenceKey="general-urolithiasis-guide",
        category="guideline",
        title=(
            "ACR Appropriateness Criteria acute flank pain - "
            "suspicion of stone disease (urolithiasis)"
        ),
        source="American College of Radiology",
        summary=(
            "The guideline recommends noncontrast CT for the general adult "
            "population and also discusses ultrasound for pregnant patients."
        ),
        interpretation={
            "claim": "该指南给出疑似泌尿系结石的影像学评估路径。",
            "population": "急性腰痛伴疑似结石病的患者",
            "applicability": "适用于成人肾结石影像学评估。",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert _record_matches_subject_demographics(
        record,
        subject=SubjectInfo(age=30, sex="男"),
    )


def test_pregnancy_only_diagnostic_guide_is_rejected_for_male_subject():
    record = EvidenceRecord(
        evidenceKey="pregnancy-only-urolithiasis-guide",
        category="guideline",
        title="Imaging of urolithiasis in pregnant patients",
        source="Society",
        interpretation={
            "claim": "该指南讨论孕妇泌尿系结石影像学检查。",
            "population": "孕妇",
            "targetAlignment": "direct",
            "patientApplicability": "contextual",
        },
    )

    assert not _record_matches_subject_demographics(
        record,
        subject=SubjectInfo(age=30, sex="男"),
    )


def test_clinical_followup_examination_is_summary_only():
    examinations = _health_recommended_examinations(
        {},
        {7},
        report={
            "diseaseRisks": {
                "items": [
                    {
                        "diseaseId": "clinical-fatty-liver",
                        "name": "非酒精性脂肪肝",
                        "sourceType": "clinical_context",
                        "existingResults": [
                            {"itemName": "肝脏超声", "result": "脂肪肝"}
                        ],
                        "suggestedEvaluations": [
                            {
                                "name": "肝脏瞬时弹性成像",
                                "purpose": "评估",
                                "rationale": "用于评估肝脏纤维化风险。",
                                "alreadyAvailable": False,
                                "evidenceRefs": [7],
                            }
                        ],
                    }
                ]
            }
        },
        public_findings=[],
    )

    assert examinations == [
        {
            "name": "肝脏瞬时弹性成像",
            "purpose": "评估",
            "reason": "用于评估肝脏纤维化风险。",
            "evidence_refs": [7],
        }
    ]


def test_empty_diagnostic_suggestion_distinguishes_valid_empty_from_failure():
    diseases = DiseaseRisksSection(
        items=[
            DiseaseRiskItem(
                diseaseId="clinical-condition",
                name="高脂血症",
                sourceType="clinical_context",
                sourceResult="临床资料记录",
            )
        ]
    )
    successful_empty = EvidenceQueryAudit(
        queryId="query-success",
        queryText="hyperlipidemia diagnostic guideline",
        intent="diagnostic_guideline",
        target="高脂血症",
        diseaseId="clinical-condition",
        channels=[
            EvidenceChannelAudit(
                channel="guide",
                status="success",
                provider="evimed_v2",
            )
        ],
    )
    failed = successful_empty.model_copy(
        update={
            "query_id": "query-failed",
            "channels": [
                EvidenceChannelAudit(
                    channel="guide",
                    status="failed",
                    provider="evimed_v2",
                )
            ],
        }
    )

    assert "DIAGNOSTIC_EVIDENCE_INCOMPLETE" not in _content_completeness_reasons(
        microbes=AbnormalMicrobesSection(),
        diseases=diseases,
        query_audits=[successful_empty],
    )
    assert "DIAGNOSTIC_EVIDENCE_INCOMPLETE" in _content_completeness_reasons(
        microbes=AbnormalMicrobesSection(),
        diseases=diseases,
        query_audits=[failed],
    )
    assert "DIAGNOSTIC_EVIDENCE_INCOMPLETE" in _content_completeness_reasons(
        microbes=AbnormalMicrobesSection(),
        diseases=diseases,
        query_audits=[],
    )


def test_publishable_relation_is_promoted_before_diagnostic_followup():
    microbe = AbnormalMicrobeItem(
        microbeId='taxon-st',
        name='嗜热链球菌',
        scientificName='Streptococcus thermophilus',
        category='beneficial_abnormal',
        sourceFinding={'status': '丰度低', 'value': '0.01%'},
    )
    microbes = AbnormalMicrobesSection(items=[microbe])
    disease = DiseaseRiskItem(
        diseaseId='disease-crc',
        name='结直肠癌',
        sourceType='source_report',
    )
    diseases = DiseaseRisksSection(items=[disease])
    record = EvidenceRecord(
        evidenceKey='st-crc-fecal',
        category='literature',
        title='Streptococcus thermophilus is depleted in colorectal cancer',
        source='Journal',
        summary=(
            'A human cohort found Streptococcus thermophilus depleted in '
            'fecal samples from adults with colorectal cancer.'
        ),
        intents=['microbe_disease_association'],
        microbeIds=[microbe.microbe_id],
        diseaseIds=[disease.disease_id],
        interpretation={
            'claim': '嗜热链球菌与结直肠癌相关。',
            'studyDesign': '队列研究',
            'direction': 'decreased',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'sampleAlignment': 'unclear',
            'patientApplicability': 'contextual',
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId='relation-st-crc',
                microbeId=microbe.microbe_id,
                microbeName=microbe.scientific_name,
                microbeAliases=[microbe.name],
                diseaseId=disease.disease_id,
                diseaseName=disease.name,
                relationType='epidemiologic_association',
                claim='嗜热链球菌与结直肠癌相关。',
            )
        ],
    )

    promoted = _promote_evidence_diseases_for_followup(
        microbes=microbes,
        diseases=diseases,
        records=[record],
        subject=SubjectInfo(age=35),
    )

    assert promoted == {disease.disease_id}
    assert disease.related_microbes
    assert disease.association_evidence_refs == [1]
    assert disease.source_result


class FailingEvidenceClient:
    async def search(self, query, *, supports):
        raise EvidenceServiceError('down')


class NullCallback:
    async def send(self, payload):
        return None


class CapturingCallback:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)


def test_microbe_boundary_is_module_level_even_without_disease_relations():
    context = _clinical_context(
        {},
        {},
        public_findings=[
            {
                'finding_type': 'microbe',
                'problem': '示例菌',
                'related_diseases_or_adverse_effects': [],
            }
        ],
    )

    assert '菌群关联判定说明' in context
    assert '不等同于临床感染或疾病诊断' in context['菌群关联判定说明']


def test_summary_drops_content_hidden_by_final_public_evidence_gates():
    report = {
        'executiveSummary': '人体研究提示嗜热链球菌与结直肠癌相关。',
        'abnormalMicrobes': {
            'items': [
                {
                    'name': '嗜热链球菌',
                    'scientificName': 'Streptococcus thermophilus',
                }
            ]
        },
        'diseaseRisks': {'items': [{'name': '结直肠癌'}]},
    }

    conclusion = _public_summary_conclusion(report, findings=[])

    assert conclusion is not None
    assert "阶段性评估" in conclusion
    assert "嗜热链球菌" not in conclusion
    assert "结直肠癌" not in conclusion


def test_summary_keeps_names_that_survive_final_public_evidence_gates():
    report = {
        'executiveSummary': '本次检出肠沙门菌，人体资料支持关注沙门菌病。',
        'abnormalMicrobes': {'items': [{'name': '肠沙门菌'}]},
        'diseaseRisks': {'items': [{'name': '沙门菌病'}]},
    }
    findings = [
        {
            'finding_type': 'microbe',
            'problem': '肠沙门菌 (Salmonella enterica)',
            'related_diseases_or_adverse_effects': [{'name': '沙门菌病'}],
        }
    ]

    assert _public_summary_conclusion(report, findings) == report['executiveSummary']


def test_summary_drops_normalized_disease_name_hidden_by_public_gate():
    report = {
        'executiveSummary': '本次菌群资料提示可以关注2型糖尿病方向。',
        'abnormalMicrobes': {'items': []},
        'diseaseRisks': {'items': [{'name': 'II型糖尿病'}]},
    }

    conclusion = _public_summary_conclusion(report, findings=[])

    assert conclusion is not None
    assert "阶段性评估" in conclusion
    assert "2型糖尿病" not in conclusion


def test_summary_uses_partial_health_assessment_when_model_summary_is_empty():
    report = {
        'executiveSummary': None,
        'healthAssessment': {
            'summary': (
                '基于目前已获得的5个维度信息，'
                '本次阶段性评估显示消化系统是当前优先关注方向。'
            )
        },
    }

    conclusion = _public_summary_conclusion(report, findings=[])

    assert conclusion == report['healthAssessment']['summary']


def test_summary_falls_back_to_final_public_rows_instead_of_becoming_empty():
    report = {
        'executiveSummary': '本次菌群资料还提到后来被隐藏的内部疾病名称。',
        'abnormalMicrobes': {'items': [{'name': '内部菌', 'scientificName': 'Internal bacterium'}]},
        'diseaseRisks': {'items': [{'name': '内部疾病名称'}]},
        'healthManagement': {
            'enterotype': {'name': '肠型I'},
            'recommendations': [
                {'topic': '肠道微生态'},
                {'topic': '糖脂代谢'},
            ],
        },
    }
    findings = [
        {
            'finding_type': 'microbe',
            'problem': '公开菌 (Public bacterium)',
            'related_diseases_or_adverse_effects': [],
        },
        {
            'finding_type': 'disease',
            'problem': '公开健康方向',
            'related_diseases_or_adverse_effects': [],
        },
    ]

    conclusion = _public_summary_conclusion(report, findings)

    assert conclusion is not None
    assert '肠型I' in conclusion
    assert '肠道微生态' in conclusion
    assert '内部菌' not in conclusion
    assert '内部疾病名称' not in conclusion


def test_microbe_callback_does_not_publish_prose_without_public_risk():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-supported-role',
            'name': '示例菌',
            'scientificName': 'Example bacterium',
            'category': 'commensal',
            'sourceFinding': {'value': '0.3%', 'status': '偏低'},
            'medicalInterpretation': '人体肠道研究支持该菌的菌群功能解读。',
            'modelInterpretationGenerated': False,
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'direct',
                'claim': '人体肠道研究支持该菌的菌群功能解读。',
            }
        },
    )

    assert finding is not None
    assert finding['related_diseases_or_adverse_effects'] == []
    assert finding['biological_mechanism'] is None
    assert finding['indicators'] == [{'name': None, 'value': '0.3%', 'status': '偏低'}]


def test_microbe_callback_keeps_source_row_when_evidence_enrichment_is_empty():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-source-only',
            'name': '来源菌',
            'scientificName': 'Source bacterium',
            'category': 'other_abnormal',
            'sourceFinding': {'value': '0.2', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': None,
            'interpretationEvidenceRefs': [],
            'diseaseAndRisks': [],
            'evidenceRefs': [],
        },
        reference_numbers=set(),
        reference_categories={},
    )

    assert finding is not None
    assert finding['problem'] == '来源菌 (Source bacterium)'
    assert finding['indicators'] == [{'name': None, 'value': '0.2%', 'status': '丰度低'}]
    assert finding['related_diseases_or_adverse_effects'] == []
    assert finding['interpretation_evidence_refs'] == []
    assert finding['tips'] == {
        'special_reminder': '当前没有与本次粪便结果直接匹配的疾病或不良影响证据。',
        'practical_goal': '未检索到与本次低丰度方向直接匹配的人体粪便证据。',
    }


def test_microbe_callback_publishes_contextual_human_stool_relation_only():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-contextual',
            'name': '示例菌',
            'scientificName': 'Example bacterium',
            'category': 'beneficial_abnormal',
            'sourceFinding': {'value': '0.2', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': '人体粪便研究观察到示例菌与肠道健康风险相关。',
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '肠道健康风险',
                    'description': '人体粪便研究观察到示例菌与肠道健康风险相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
                {
                    'name': '呼吸道感染',
                    'description': '呼吸道样本研究观察到相关性。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [2],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'respiratory',
                },
            ],
            'evidenceRefs': [1, 2],
        },
        reference_numbers={1, 2},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '人体粪便研究观察到示例菌与肠道健康风险相关。',
            },
            2: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'respiratory',
                'claim': '呼吸道样本研究观察到相关性。',
            },
        },
    )

    assert finding is not None
    assert [item['name'] for item in finding['related_diseases_or_adverse_effects']] == ['肠道健康风险']
    assert finding['related_diseases_or_adverse_effects'][0]['description'] is None
    assert finding['biological_mechanism'] == '提示肠道健康风险。'
    assert '人体粪便研究' not in finding['biological_mechanism']
    assert '本次' not in finding['biological_mechanism']
    assert '个人患病概率' not in finding['biological_mechanism']


def test_microbe_relation_replaces_generic_colonization_role_with_interpretation():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-oxalobacter',
            'name': '草酸杆菌',
            'scientificName': 'Oxalobacter formigenes',
            'category': 'beneficial_abnormal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': '人体研究记录草酸杆菌可在特定人体部位定植。',
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '肾结石',
                    'description': '草酸杆菌丰度降低可能与肾结石风险增加相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                }
            ],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '草酸杆菌丰度降低与肾结石风险增加相关。',
            }
        },
    )

    assert finding is not None
    assert finding['biological_mechanism'] == (
        '该菌在肾结石相关菌群中呈减少方向，提示这种菌群特征可能涉及肾结石的发生或进展。'
    )
    assert '特定人体部位定植' not in finding['biological_mechanism']


def test_microbe_relation_recovers_oxalate_mechanism_from_bound_evidence():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-oxalobacter-evidence-mechanism',
            'name': '草酸杆菌',
            'scientificName': 'Oxalobacter formigenes',
            'category': 'beneficial_abnormal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': '提示肾结石风险可能增加。',
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '肾结石',
                    'description': '草酸杆菌丰度降低可能与肾结石风险增加相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                }
            ],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '草酸杆菌定植与复发性草酸钙肾结石风险降低相关。',
                'summary': (
                    'Oxalobacter formigenes metabolizes oxalate in the intestinal '
                    'tract; absence may increase colonic oxalate absorption and '
                    'urinary oxalate.'
                ),
            }
        },
    )

    assert finding is not None
    interpretation = finding['biological_mechanism']
    assert '肠道草酸代谢' in interpretation
    assert '草酸吸收' in interpretation
    assert '尿草酸负荷' in interpretation
    assert '肾结石' in interpretation
    assert '本次' not in interpretation
    assert '人体研究' not in interpretation


def test_microbe_relation_recovers_pathogen_effect_from_bound_evidence():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-cdiff-evidence-mechanism',
            'name': '艰难梭菌',
            'scientificName': 'Clostridioides difficile',
            'category': 'pathogenic_signal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '检出'},
            'medicalInterpretation': '相关证据主要来自粪便样本。',
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '艰难梭菌感染',
                    'description': '艰难梭菌可能引起艰难梭菌感染。',
                    'relationType': 'established_pathogen',
                    'evidenceRefs': [1],
                    'patientApplicability': 'direct',
                    'sampleAlignment': 'stool',
                }
            ],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'direct',
                'sample_alignment': 'stool',
                'claim': '艰难梭菌感染的管理指南。',
                'summary': (
                    'Clostridioides difficile produces toxin that causes diarrhea and inflammation of the colon.'
                ),
            }
        },
    )

    assert finding is not None
    assert finding['biological_mechanism'] == ('该菌可产生毒素并引起腹泻及结肠炎，可能导致艰难梭菌感染。')
    assert '相关证据' not in finding['biological_mechanism']


def test_microbe_relation_keeps_relation_scoped_medical_explanation():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-oxalobacter-mechanism',
            'name': '草酸杆菌',
            'scientificName': 'Oxalobacter formigenes',
            'category': 'beneficial_abnormal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': ('可能影响肠道草酸代谢，提示肾结石风险增加。'),
            'modelInterpretationGenerated': True,
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '肾结石',
                    'description': '草酸杆菌丰度降低可能与肾结石风险增加相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                }
            ],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '草酸杆菌丰度降低与肾结石风险增加相关。',
            }
        },
    )

    assert finding is not None
    assert finding['biological_mechanism'] == ('可能影响肠道草酸代谢，提示肾结石风险增加。')
    assert '本次' not in finding['biological_mechanism']
    assert '人体研究' not in finding['biological_mechanism']


def test_microbe_relation_recovers_detailed_evidence_bound_interpretation():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-veillonella-detailed-fallback',
            'name': '小韦荣球菌',
            'scientificName': 'Veillonella parvula',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.1%', 'status': '检出'},
            'medicalInterpretation': '小韦荣球菌与炎症性肠病和结直肠癌相关。',
            'modelInterpretationGenerated': True,
            'interpretationEvidenceRefs': [1, 2],
            'diseaseAndRisks': [
                {
                    'name': '炎症性肠病',
                    'description': ('人体粪便研究观察到小韦荣球菌在炎症性肠病中富集，可能影响肠黏膜炎症活动。'),
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
                {
                    'name': '结直肠癌',
                    'description': ('小韦荣球菌相关代谢信号可能影响结直肠癌发生过程。'),
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [2],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
            ],
            'evidenceRefs': [1, 2],
        },
        reference_numbers={1, 2},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌与炎症性肠病相关。',
            },
            2: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌与结直肠癌相关。',
            },
        },
    )

    assert finding is not None
    interpretation = finding['biological_mechanism']
    assert '炎症性肠病' in interpretation
    assert '结直肠癌' in interpretation
    assert '肠黏膜炎症活动' in interpretation
    assert '发生过程' in interpretation
    assert '人体研究' not in interpretation


def test_microbe_relation_drops_short_sentence_contained_by_detailed_claim():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-veillonella-contained-claim',
            'name': '小韦荣球菌',
            'scientificName': 'Veillonella parvula',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.1%', 'status': '检出'},
            'medicalInterpretation': ('小韦荣球菌是一种机会致病菌，与结直肠癌和炎症性肠病相关。'),
            'modelInterpretationGenerated': True,
            'interpretationEvidenceRefs': [3],
            'diseaseAndRisks': [
                {
                    'name': '结直肠癌',
                    'description': '小韦荣球菌与右侧结直肠癌的粪便微生物组改变相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
                {
                    'name': '炎症性肠病',
                    'description': '小韦荣球菌等菌种在炎症性肠病中富集。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [2],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
            ],
            'evidenceRefs': [1, 2, 3],
        },
        reference_numbers={1, 2, 3},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': ('小韦荣球菌与右侧结直肠癌的粪便微生物组改变相关，可能作为肿瘤位置特异性诊断生物标志物。'),
            },
            2: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': (
                    '小韦荣球菌等菌种在炎症性肠病中富集，且这些菌种与'
                    '结直肠癌相关，提示相关菌群可能通过抑制维生素E信号'
                    '通路增加结直肠癌风险。'
                ),
            },
            3: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌与肝硬化肌少症患者的肠道菌群改变相关。',
            },
        },
    )

    assert finding is not None
    interpretation = finding['biological_mechanism']
    assert interpretation.count('粪便微生物组改变相关') == 1
    assert interpretation.count('在炎症性肠病中富集') == 1
    assert '肿瘤位置特异性诊断生物标志物' in interpretation
    assert '维生素E信号通路' in interpretation
    assert finding['interpretation_evidence_refs'] == [1, 2]
    assert 3 not in finding['interpretation_evidence_refs']
    assert '人体粪便研究' not in interpretation
    assert '本次' not in interpretation
    assert '检测' not in interpretation


def test_microbe_relation_combines_distinct_risk_types_without_repeated_prompt():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-veillonella-mixed',
            'name': '小韦荣球菌',
            'scientificName': 'Veillonella parvula',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.1%', 'status': '检出'},
            'medicalInterpretation': '已根据证据形成解读。',
            'interpretationEvidenceRefs': [1, 2],
            'diseaseAndRisks': [
                {
                    'name': '炎症性肠病',
                    'description': '相关变化提示炎症性肠病风险增加。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
                {
                    'name': '结直肠癌',
                    'description': '相关变化与结直肠癌相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [2],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                },
            ],
            'evidenceRefs': [1, 2],
        },
        reference_numbers={1, 2},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌相关变化提示炎症性肠病风险增加。',
            },
            2: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌相关变化与结直肠癌相关。',
            },
        },
    )

    assert finding is not None
    assert finding['biological_mechanism'] == ('提示炎症性肠病风险可能增加，并需关注结直肠癌相关风险。')
    assert finding['biological_mechanism'].count('提示') == 1


def test_microbe_relation_uses_only_structured_visible_outcome():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-veillonella',
            'name': '小韦荣球菌',
            'scientificName': 'Veillonella parvula',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '检出'},
            'medicalInterpretation': (
                '炎症性肠病相关菌群可能影响结直肠癌风险，其中小韦荣球菌在炎症性肠病人群粪便中富集。'
            ),
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [
                {
                    'name': '炎症性肠病',
                    'description': ('炎症性肠病相关菌群可能影响结直肠癌风险。小韦荣球菌在炎症性肠病人群粪便中富集。'),
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [1],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'stool',
                }
            ],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '小韦荣球菌在炎症性肠病人群粪便中富集。',
            }
        },
    )

    association = finding['related_diseases_or_adverse_effects'][0]
    assert association['description'] is None
    interpretation = finding['biological_mechanism']
    assert interpretation == '提示炎症性肠病相关风险。'
    assert '结直肠癌' not in interpretation


def test_microbe_callback_drops_hidden_outcome_and_action_from_explanation():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-hidden-outcome',
            'name': '示例菌',
            'scientificName': 'Example bacterium',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.3%', 'status': '偏低'},
            'medicalInterpretation': (
                'IBD患者粪便菌群中富集的该菌与结直肠癌相关。'
                '当前无需采取特殊措施，维持良好个人卫生即可。'
                '建议关注相关表现，并留意后续复查。'
            ),
            'clinicalRole': '该菌是肺部感染的病原体。',
            'modelInterpretationGenerated': True,
            'interpretationEvidenceRefs': [1],
            'diseaseAndRisks': [],
            'evidenceRefs': [1],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'target_alignment': 'direct',
                'patient_applicability': 'direct',
                'claim': '人体资料支持该菌具有机会致病性。',
            }
        },
    )

    assert finding is not None
    assert finding['related_diseases_or_adverse_effects'] == []
    assert finding['biological_mechanism'] is None


def test_disease_callback_keeps_evidence_bound_hospital_test_for_microbiome_risk():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-neurologic',
            'name': '神经系统健康关注项',
            'sourceType': 'source_report',
            'sourceResult': '原报告关联模型结果 68；中风险',
            'relatedMicrobes': ['示例菌'],
            'medicalInterpretation': (
                '本次检测中示例菌的变化与人体研究观察到的神经系统健康方向相关，因此值得关注相关功能表现和长期健康风险。'
            ),
            'associationEvidenceRefs': [1],
            'patientSuggestion': '如需进一步评估，可考虑头颅磁共振成像。',
            'suggestedEvaluations': [
                {
                    'name': '头颅磁共振成像',
                    'purpose': '筛查',
                    'rationale': '用于评估。',
                    'evidenceRefs': [1],
                }
            ],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={1: 'literature'},
    )

    assert finding is not None
    assert finding['recommendations'] == ['头颅磁共振成像：用于评估。']
    assert finding['recommendation_evidence_refs'] == [1]
    assert finding['tips']['special_reminder'] == (
        '如出现认知、情绪或神经系统功能变化，建议到神经内科就诊，并由医生判断是否需要头颅磁共振成像。'
    )


def test_evaluation_text_uses_common_ogtt_name_and_concise_purpose():
    assert (
        _evaluation_text(
            {
                'name': '75克口服葡萄糖耐量试验1小时血浆葡萄糖',
                'purpose': '确认',
                'rationale': '用于检测1小时血浆葡萄糖，以诊断中间高血糖和2型糖尿病。',
            }
        )
        == '口服葡萄糖耐量试验：用于评估血糖代谢情况。'
    )


def test_model_examination_links_disease_summary_without_citation():
    import copy
    item = {
        "diseaseId": "new-disease", "name": "神经系统健康关注项",
        "sourceType": "source_report", "sourceResult": "中风险",
        "relatedMicrobes": ["示例菌"],
        "medicalInterpretation": "本次检测中示例菌的变化与人体研究观察到的神经系统健康方向相关，因此值得关注相关功能表现和长期健康风险。",
        "associationEvidenceRefs": [1], "existingResults": [],
        "suggestedEvaluations": [
            {"name": "布里斯托大便性状量表", "rationale": "用于记录", "evidenceRefs": [1]},
            {"name": "血常规检查", "purpose": "评估", "rationale": "用于评估血细胞相关异常。",
             "recommendationSource": "model", "evidenceRefs": []},
        ],
    }
    before = copy.deepcopy(item)
    finding = _disease_finding(item, reference_numbers={1}, reference_categories={1: "literature"})
    assert finding is not None
    assert finding["recommendations"] == ["血常规检查：用于评估血细胞相关异常。"]
    assert finding["recommendation_evidence_refs"] == []
    assert finding["interpretation_evidence_refs"] == [1]
    assert "recommendationSource" not in finding
    exams = _health_recommended_examinations(
        {}, {1}, report={"diseaseRisks": {"items": [item]}}, public_findings=[finding],
    )
    assert exams[0]["name"] == "血常规检查"
    assert exams[0]["evidence_refs"] == []
    assert item == before


def test_model_check_survives_evidence_checkpoint_and_is_not_missing_evidence():
    from modules.report.clinical import attach_suggested_evaluations
    from modules.report.pipeline import _content_completeness_reasons
    from modules.report.schemas import SuggestedEvaluation
    disease = DiseaseRiskItem(
        diseaseId="clinical", name="临床异常", sourceType="clinical_context",
        suggestedEvaluations=[SuggestedEvaluation(
            name="尿常规检查", rationale="用于评估。", recommendationSource="model",
        )],
    )
    diseases = DiseaseRisksSection(items=[disease])
    _clear_evidence_bound_content(AbnormalMicrobesSection(), diseases)
    attach_suggested_evaluations(diseases, [], {})
    assert len(disease.suggested_evaluations) == 1
    assert disease.confirmation_evidence_refs == []
    assert "DIAGNOSTIC_EVIDENCE_INCOMPLETE" not in _content_completeness_reasons(
        microbes=AbnormalMicrobesSection(), diseases=diseases, query_audits=[],
    )


def test_disease_callback_hides_source_row_when_patient_evidence_is_empty():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-model-only',
            'name': '腺瘤',
            'sourceType': 'source_report',
            'modelIndex': '82',
            'medicalInterpretation': '原报告模型指数为82，不等同于临床诊断。',
            'associationEvidenceRefs': [],
            'confirmationEvidenceRefs': [],
            'suggestedEvaluations': [],
            'existingResults': [],
            'evidenceRefs': [],
        },
        reference_numbers={1},
        reference_categories={1: 'literature'},
    )

    assert finding is None


def test_source_low_risk_catalogue_row_stays_out_of_public_callback():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-low',
            'name': '低风险来源项目',
            'sourceType': 'source_report',
            'sourceResult': '原报告关联模型结果 3.83；低风险',
            'modelIndex': '3.83',
            'relatedMicrobes': ['普拉梭菌(缺乏)'],
            'medicalInterpretation': '人体粪便研究观察到相关菌群变化。',
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['disease_clinical_significance'],
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '人体粪便研究观察到相关菌群变化。',
            }
        },
    )

    assert finding is None


def test_evidence_checkpoint_preserves_source_report_microbe_links():
    source = DiseaseRiskItem(
        diseaseId='disease-source',
        name='炎症性肠病',
        sourceType='source_report',
        sourceResult='原报告关联模型结果 84.78；高风险',
        relatedMicrobes=['变异梭杆菌(超标)', '普拉梭菌(缺乏)'],
        associationEvidenceRefs=[1],
        medicalInterpretation='已有解释',
    )
    promoted = DiseaseRiskItem(
        diseaseId='disease-promoted',
        name='胃肠炎',
        sourceType='evimed_association',
        relatedMicrobes=['类志贺邻单胞菌'],
        associationEvidenceRefs=[2],
        medicalInterpretation='已有解释',
    )

    _clear_evidence_bound_content(
        AbnormalMicrobesSection(),
        DiseaseRisksSection(items=[source, promoted]),
    )

    assert source.related_microbes == ['变异梭杆菌(超标)', '普拉梭菌(缺乏)']
    assert promoted.related_microbes == []


def test_disease_callback_hides_generic_disease_evidence_without_bound_microbe():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-generic-thyroid',
            'name': '甲状腺癌',
            'sourceType': 'source_report',
            'sourceResult': '原报告关联模型结果 47.99；中风险',
            'modelIndex': '47.99',
            'relatedMicrobes': [],
            'medicalInterpretation': '肠道菌群特征与甲状腺癌相关。',
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['disease_clinical_significance'],
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '甲状腺癌患者的肠道菌群组成与对照存在差异。',
            }
        },
    )

    assert finding is None


def test_disease_callback_accepts_microbe_discovery_relation_bound_to_patient():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-uc',
            'name': '溃疡性结肠炎',
            'sourceType': 'source_report',
            'sourceResult': '普拉梭菌：缺乏 0.0296%',
            'modelIndex': '88.78',
            'relatedMicrobes': ['普拉梭菌'],
            'medicalInterpretation': '普拉梭菌与溃疡性结肠炎相关。',
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'title': 'Faecalibacterium prausnitzii in ulcerative colitis',
                'summary': 'Fecal abundance was decreased in adults with UC.',
                'intents': ['microbe_discovery'],
                'target_alignment': 'direct',
                'taxon_alignment': 'species',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '溃疡性结肠炎患者粪便中普拉梭菌丰度降低。',
            }
        },
    )

    assert finding is not None
    assert finding['problem'] == '溃疡性结肠炎'
    assert finding['interpretation_evidence_refs'] == [1]
    assert '普拉梭菌' in finding['biological_mechanism']


def test_disease_callback_accepts_verified_pathogen_causal_wording():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-gastroenteritis',
            'name': '胃肠炎',
            'sourceType': 'evimed_association',
            'sourceResult': '类志贺邻单胞菌：超标 0.0002%',
            'relatedMicrobes': ['类志贺邻单胞菌'],
            'medicalInterpretation': '类志贺邻单胞菌可能引起胃肠炎。',
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'title': 'Plesiomonas shigelloides gastroenteritis',
                'summary': 'Stool isolates were associated with gastroenteritis.',
                'intents': ['microbe_disease_association'],
                'target_alignment': 'direct',
                'taxon_alignment': 'species',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '类志贺邻单胞菌可能引起胃肠炎。',
            }
        },
    )

    assert finding is not None
    assert finding['biological_mechanism'] == '类志贺邻单胞菌可能引起胃肠炎。'
    assert finding['interpretation_evidence_refs'] == [1]


def test_disease_callback_accepts_evidence_bound_human_gut_rationale():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-gut-risk',
            'name': '肠道健康风险',
            'sourceType': 'evimed_association',
            'sourceResult': '示例菌：丰度低 0.2%',
            'relatedMicrobes': ['示例菌'],
            'medicalInterpretation': (
                '本次检测中的示例菌变化具有人体肠道研究依据：'
                '人体粪便研究观察到示例菌变化与肠道健康风险相关。'
                '该结果可作为肠道健康风险方向的健康关注线索。'
            ),
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['microbe_disease_association'],
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '人体粪便研究观察到示例菌变化与肠道健康风险相关。',
            }
        },
    )

    assert finding is not None
    assert finding['problem'] == '肠道健康风险'
    assert finding['priority'] == '建议关注'
    assert finding['interpretation_evidence_refs'] == [1]
    assert finding['biological_mechanism'] == ('示例菌相关变化提示肠道健康风险。')
    assert '本次检测' not in finding['biological_mechanism']
    assert '人体研究' not in finding['biological_mechanism']


def test_disease_callback_keeps_microbe_bound_source_row_without_model_index():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-source-constipation',
            'name': '便秘',
            'sourceType': 'source_report',
            'sourceResult': '示例菌：丰度异常',
            'modelIndex': None,
            'relatedMicrobes': ['示例菌'],
            'medicalInterpretation': ('人体粪便研究观察到示例菌变化与便秘相关，因此可关注排便频率和排便困难等变化。'),
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['disease_clinical_significance'],
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '人体粪便研究观察到示例菌变化与便秘相关。',
            }
        },
    )

    assert finding is not None
    assert finding['problem'] == '便秘'
    assert finding['priority'] == '建议关注'
    assert finding['interpretation_evidence_refs'] == [1]
    assert finding['biological_mechanism'] == ('示例菌相关变化提示需关注便秘相关风险。')
    assert '相关菌群相关变化' not in finding['biological_mechanism']
    assert finding['tips']['special_reminder'] == ('如持续出现胃肠道症状与排便变化，建议到消化内科进一步评估。')


@pytest.mark.parametrize(
    'value',
    [
        (
            '本次检测中小韦荣球菌等相关菌的变化是本报告关注'
            '炎症性肠病方向的依据。人体肠道研究具体提示：'
            '小韦荣球菌与炎症性肠病相关。'
        ),
        ('本次菌群检测检出普通拟杆菌。人体研究提示普通拟杆菌与溃疡性结肠炎严重程度相关，可关注结直肠健康。'),
    ],
)
def test_disease_rationale_accepts_specific_evidence_bound_wording(value):
    assert _callback_disease_risk_rationale_is_present(value)


def test_old_disease_fallback_is_rewritten_as_result_led_patient_wording():
    value = _evidence_bound_disease_interpretation(
        item={
            'name': '肾结石',
            'displayName': '肾结石',
            'sourceResult': '草酸杆菌：丰度低 0.0007 %',
            'relatedMicrobes': ['草酸杆菌'],
        },
        medical_interpretation=(
            '本次检测中草酸杆菌等相关菌的变化是本报告关注'
            '肾结石方向的依据。人体肠道研究具体提示：'
            '草酸杆菌丰度降低可能与肾结石风险增加相关。'
        ),
        evidence_refs=[1],
        excluded_evidence_refs=[],
        metadata={1: {'claim': '草酸杆菌丰度降低可能与肾结石风险增加相关。'}},
    )

    assert value is not None
    assert value == '草酸杆菌相关变化提示需关注肾结石风险。'
    assert '本次检测' not in value
    assert '人体粪便研究' not in value
    assert '不能用于判断' not in value
    assert '是本报告关注' not in value
    assert '人体肠道研究具体提示' not in value


def test_result_led_disease_wording_keeps_only_current_disease_clause():
    value = _evidence_bound_disease_interpretation(
        item={
            'name': '菌血症',
            'displayName': '菌血症',
            'sourceResult': '沙门氏菌：检出 9.7%',
            'relatedMicrobes': ['沙门氏菌'],
        },
        medical_interpretation=('本次检测中沙门氏菌的变化是本报告关注菌血症方向的依据。'),
        evidence_refs=[1],
        excluded_evidence_refs=[],
        metadata={1: {'claim': ('沙门氏菌与胃肠炎相关。沙门氏菌与食物中毒相关。沙门氏菌与菌血症相关。')}},
    )

    assert value is not None
    assert value == '沙门氏菌相关变化提示需关注菌血症相关风险。'
    assert '胃肠炎' not in value
    assert '食物中毒' not in value


def test_disease_callback_keeps_verified_human_relation_without_literal_human_word():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-gastroenteritis',
            'name': '胃肠炎',
            'sourceType': 'evimed_association',
            'sourceResult': '沙门氏菌：检出 9.7%',
            'relatedMicrobes': ['沙门氏菌'],
            'medicalInterpretation': (
                '本次菌群检测检出沙门氏菌。一项研究显示该菌与胃肠炎相关，建议关注腹泻、呕吐和发热等急性胃肠道表现。'
            ),
            'associationEvidenceRefs': [71],
            'suggestedEvaluations': [
                {
                    'name': '粪便病原菌培养',
                    'purpose': '确认',
                    'rationale': '用于辅助胃肠炎诊断。',
                    'alreadyAvailable': False,
                    'evidenceRefs': [71],
                }
            ],
            'existingResults': [],
        },
        reference_numbers={71},
        reference_categories={
            71: {
                'category': 'literature',
                'title': 'Salmonella gastroenteritis cohort',
                'summary': 'Faecal culture confirmed gastroenteritis.',
                'claim': 'Salmonella is associated with gastroenteritis.',
                'intents': ['microbe_disease_association'],
                'target_alignment': 'direct',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
            }
        },
    )

    assert finding is not None
    assert finding['problem'] == '胃肠炎'
    assert finding['biological_mechanism'] == ('沙门氏菌相关变化提示需关注胃肠炎相关风险。')
    assert '本次菌群检测' not in finding['biological_mechanism']
    assert '一项研究' not in finding['biological_mechanism']
    assert '建议关注' not in finding['biological_mechanism']
    assert finding['recommendations'] == ['便检：用于辅助胃肠炎诊断。']
    assert finding['tips']['special_reminder'] == (
        '如出现胃肠道症状与排便变化，建议到消化内科就诊，并由医生判断是否需要便检。'
    )


def test_unverified_disease_rationale_still_requires_explicit_human_relation():
    assert not _callback_disease_risk_rationale_is_present(
        '本次菌群检测检出沙门氏菌。一项研究显示该菌与胃肠炎相关，建议关注急性胃肠道表现。'
    )


def test_disease_callback_rejects_generic_suggestion_and_normalizes_terms():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-t2dm',
            'name': 'II型糖尿病',
            'sourceType': 'source_report',
            'sourceResult': '原报告关联模型结果 68；中风险',
            'relatedMicrobes': ['示例菌'],
            'medicalInterpretation': (
                '本次检测中示例菌的变化与人体研究观察到的II型糖尿病相关方向一致，因此值得关注糖代谢表现和长期健康风险。'
            ),
            'patientSuggestion': ('可以优先关注II型糖尿病相关健康表现，必要时由专业人员进一步评估。'),
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={1: 'literature'},
    )

    assert finding is not None
    assert finding['problem'] == '2型糖尿病'
    assert finding['biological_mechanism'] == ('本次检测中示例菌的变化与人体研究观察到的2型糖尿病相关方向一致。')
    assert finding['tips']['special_reminder'] == ('如持续出现血糖及相关代谢变化，建议到内分泌科进一步评估。')


def test_uncited_microbe_does_not_publish_unsupported_health_claim():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-source-only',
            'name': '示例菌',
            'scientificName': 'Example bacterium',
            'category': 'beneficial_abnormal',
            'sourceFinding': {'value': '0.2', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': '人体研究支持该菌参与肠道生态维持。',
            'diseaseAndRisks': [],
            'interpretationEvidenceRefs': [],
            'associationEvidenceRefs': [],
            'evidenceRefs': [],
        },
        reference_numbers=set(),
        reference_categories={},
    )

    assert finding is not None
    assert finding['interpretation_evidence_refs'] == []
    assert finding['biological_mechanism'] is None


def test_inapplicable_cited_microbe_does_not_publish_unrelated_citation():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-low-pathogen',
            'name': '示例致病菌',
            'scientificName': 'Example pathogen',
            'category': 'pathogenic_abnormal',
            'sourceFinding': {'value': '0.1', 'unit': '%', 'status': '丰度低'},
            'medicalInterpretation': None,
            'diseaseAndRisks': [],
            'interpretationEvidenceRefs': [7],
            'associationEvidenceRefs': [],
            'evidenceRefs': [7],
        },
        reference_numbers={7},
        reference_categories={7: {'category': 'literature'}},
    )

    assert finding is not None
    assert finding['biological_mechanism'] is None
    assert finding['interpretation_evidence_refs'] == []
    assert finding['evidence_refs'] == []


def test_subtype_only_reference_is_not_exposed_for_generic_species_result():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-b-fragilis',
            'name': '脆弱拟杆菌',
            'scientificName': 'Bacteroides fragilis',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.074', 'unit': '%', 'status': '检出'},
            'medicalInterpretation': ('脆弱拟杆菌是肠道常见菌，但产肠毒素株与结直肠癌相关。'),
            'diseaseAndRisks': [
                {
                    'name': '结直肠癌',
                    'description': '脆弱拟杆菌与结直肠癌相关。',
                    'relationType': 'epidemiologic_association',
                    'evidenceRefs': [51],
                    'patientApplicability': 'contextual',
                    'sampleAlignment': 'mucosa',
                }
            ],
            'interpretationEvidenceRefs': [51],
            'evidenceRefs': [51],
        },
        reference_numbers={51},
        reference_categories={
            51: {
                'category': 'literature',
                'title': 'Enterotoxigenic Bacteroides fragilis and colorectal cancer',
                'summary': 'ETBF produces BFT encoded by the bft gene.',
                'claim': '脆弱拟杆菌与结直肠癌相关。',
                'targets': ['Bacteroides fragilis / 脆弱拟杆菌 / 结直肠癌'],
                'target_alignment': 'direct',
                'taxon_alignment': 'species',
                'sample_alignment': 'mucosa',
                'patient_applicability': 'contextual',
            }
        },
    )

    assert finding is not None
    assert finding['related_diseases_or_adverse_effects'] == []
    assert finding['biological_mechanism'] is None
    assert finding['evidence_refs'] == []


def test_microbe_relation_recovers_explicit_stool_sample_from_evidence_text():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-salmonella',
            'name': '沙门氏菌',
            'scientificName': 'Salmonella',
            'category': 'pathogenic_signal',
            'sourceFinding': {'value': '9.7', 'unit': '%', 'status': '检出'},
            'medicalInterpretation': '沙门氏菌与胃肠炎相关。',
            'diseaseAndRisks': [
                {
                    'name': '胃肠炎',
                    'description': '沙门氏菌与胃肠炎相关。',
                    'relationType': 'established_pathogen',
                    'sampleAlignment': 'unclear',
                    'patientApplicability': 'contextual',
                    'evidenceRefs': [66],
                }
            ],
            'interpretationEvidenceRefs': [66],
            'evidenceRefs': [66],
        },
        reference_numbers={66},
        reference_categories={
            66: {
                'category': 'literature',
                'title': 'Salmonella gastroenteritis cohort',
                'summary': 'Adults had faecal culture confirmed gastroenteritis.',
                'claim': 'Salmonella is associated with gastroenteritis.',
                'target_alignment': 'direct',
                'sample_alignment': 'unclear',
                'patient_applicability': 'contextual',
            }
        },
    )

    assert finding is not None
    assert [item['name'] for item in finding['related_diseases_or_adverse_effects']] == ['胃肠炎']
    association = finding['related_diseases_or_adverse_effects'][0]
    assert association['evidence_refs'] == [66]
    assert association['description'] is None
    assert finding['biological_mechanism'] == '可能引起胃肠炎。'
    assert association['description'] is None
    assert '胃肠炎' in finding['biological_mechanism']


def test_microbe_relation_recovers_fecal_function_wording_as_stool_context():
    finding = _microbe_finding(
        {
            'microbeId': 'taxon-p-vulgatus',
            'name': '普通拟杆菌',
            'scientificName': 'Phocaeicola vulgatus',
            'category': 'opportunistic_signal',
            'sourceFinding': {'value': '0.3', 'unit': '%', 'status': '检出'},
            'medicalInterpretation': '人体粪便研究观察到该菌与溃疡性结肠炎严重程度相关。',
            'diseaseAndRisks': [
                {
                    'name': '溃疡性结肠炎',
                    'description': '普通拟杆菌与溃疡性结肠炎相关。',
                    'relationType': 'epidemiologic_association',
                    'sampleAlignment': 'unclear',
                    'patientApplicability': 'contextual',
                    'evidenceRefs': [28],
                }
            ],
            'interpretationEvidenceRefs': [28],
            'evidenceRefs': [28],
        },
        reference_numbers={28},
        reference_categories={
            28: {
                'category': 'literature',
                'title': 'Pathobionts in inflammatory bowel disease',
                'summary': (
                    'Increased fecal proteolytic activity linked to '
                    'Phocaeicola vulgatus was a biomarker for UC severity.'
                ),
                'claim': '普通拟杆菌与溃疡性结肠炎相关。',
                'target_alignment': 'direct',
                'sample_alignment': 'unclear',
                'patient_applicability': 'contextual',
            }
        },
    )

    assert finding is not None
    assert [item['name'] for item in finding['related_diseases_or_adverse_effects']] == ['溃疡性结肠炎']
    assert finding['related_diseases_or_adverse_effects'][0]['evidence_refs'] == [28]


def test_health_summary_falls_back_to_published_disease_hospital_examination():
    fixed = _fixed_callback_body(
        {
            'userId': 'u',
            'sessionId': 's',
            'status': 'success',
            'report': {
                'subject': {},
                'sample': {},
                'abnormalMicrobes': {'items': []},
                'diseaseRisks': {
                    'items': [
                        {
                            'diseaseId': 'disease-ibd',
                            'name': '炎症性肠病',
                            'displayName': '炎症性肠病',
                            'sourceType': 'source_report',
                            'sourceResult': '示例菌：检出 0.1%',
                            'relatedMicrobes': ['示例菌'],
                            'medicalInterpretation': (
                                '本次检测中示例菌的变化与人体粪便研究观察到的炎症性肠病方向相关，建议关注肠道炎症风险。'
                            ),
                            'associationEvidenceRefs': [1],
                            'suggestedEvaluations': [
                                {
                                    'name': '粪便钙卫蛋白检测',
                                    'purpose': '评估',
                                    'rationale': '用于评估炎症性肠病。',
                                    'alreadyAvailable': False,
                                    'evidenceRefs': [2],
                                }
                            ],
                            'existingResults': [],
                        }
                    ]
                },
                'healthManagement': {
                    'recommendations': [],
                    'recommendedExaminations': [
                        {
                            'name': '粪便钙卫蛋白检测',
                            'purpose': '评估',
                            'reason': '模型汇总理由。',
                            # The model ref is intentionally wrong. The final
                            # callback must use the disease candidate's exact
                            # diagnostic-guideline ref instead.
                            'evidenceRefs': [1],
                        }
                    ],
                },
                'evidenceReferences': [
                    {
                        'number': 1,
                        'provider': 'evimed_v2',
                        'category': 'literature',
                        'title': 'Human stool microbiome and IBD',
                        'source': 'Journal',
                        'intents': ['microbe_disease_association'],
                        'interpretation': {
                            'claim': '人体粪便研究观察到示例菌变化与炎症性肠病相关。',
                            'targetAlignment': 'direct',
                            'sampleAlignment': 'stool',
                            'patientApplicability': 'contextual',
                        },
                    },
                    {
                        'number': 2,
                        'provider': 'evimed_v2',
                        'category': 'guideline',
                        'title': 'IBD fecal calprotectin guideline',
                        'source': 'Society',
                        'intents': ['diagnostic_guideline'],
                    },
                ],
            },
        }
    )

    disease = next(item for item in fixed['diagnostic_basis']['abnormal_findings'] if item['finding_type'] == 'disease')
    assert disease['recommendations'] == ['便检：用于评估炎症性肠病。']
    assert disease['recommendation_evidence_refs'] == [2]
    assert fixed['diagnostic_advice']['recommended_examinations'] == [
        {
            'name': '便检',
            'purpose': '评估',
            'reason': '用于评估炎症性肠病。',
            'evidence_refs': [2],
        }
    ]


def test_health_summary_fills_three_distinct_attention_directions():
    recommendations = _health_recommendations(
        {
            'recommendations': [
                {
                    'category': 'exercise',
                    'topic': '规律运动',
                    'priority': '重点关注',
                    'content': '将有氧运动与抗阻训练结合。',
                },
                {
                    'category': 'general',
                    'topic': '肠道微生态',
                    'priority': '重点关注',
                    'content': '保持饮食多样和作息规律，观察排便变化。',
                },
            ]
        },
        set(),
        report={
            'healthAssessment': {
                'dimensions': [
                    {'code': 'digestive_system', 'level': 'priority_attention'},
                    {'code': 'immune_system', 'level': 'priority_attention'},
                    {'code': 'metabolic_system', 'level': 'attention'},
                ]
            }
        },
    )

    assert len(recommendations) == 3
    assert {item['topic'] for item in recommendations} == {
        '规律运动',
        '肠道微生态',
        '免疫系统健康',
    }


def test_public_reference_count_excludes_evimed_records():
    references = [
        SimpleNamespace(provider='evimed_v2'),
        SimpleNamespace(provider='pubmed'),
        SimpleNamespace(provider='pmc'),
        SimpleNamespace(provider='official_guideline'),
        SimpleNamespace(provider=None),
    ]
    assert _public_reference_count(references) == 3


def test_public_callback_evidence_is_contiguous_and_all_refs_are_remapped():
    body = {
        'diagnosis_summary': {'evidence_refs': [4, 11]},
        'diagnostic_basis': {
            'abnormal_findings': [
                {
                    'interpretation_evidence_refs': [11],
                    'recommendation_evidence_refs': [4],
                    'evidence_refs': [4, 11],
                }
            ]
        },
        'evidence': [
            {'number': 4, 'title': 'first'},
            {'number': 11, 'title': 'second'},
        ],
    }

    remapped = _renumber_public_evidence(body)

    assert [item['number'] for item in remapped['evidence']] == [1, 2]
    assert remapped['diagnosis_summary']['evidence_refs'] == [1, 2]
    finding = remapped['diagnostic_basis']['abnormal_findings'][0]
    assert finding['interpretation_evidence_refs'] == [2]
    assert finding['recommendation_evidence_refs'] == [1]
    assert finding['evidence_refs'] == [1, 2]


def test_patient_callback_excludes_specialist_functional_tests():
    assert not _hospital_common_examination('肛门直肠测压')
    assert not _hospital_common_examination('HOMA-IR指数')
    assert _hospital_common_examination('粪便病原学检查')
    assert _hospital_common_examination('血培养')
    assert _hospital_common_examination('非增强螺旋CT检查')


def test_priority_public_relations_keep_one_pathogen_relation_per_taxon():
    queries = [
        EvidenceQuery(
            query=f'{taxon} {condition}',
            supports=(microbe_id,),
            required_concepts=((taxon,), (condition,)),
            intent='microbe_disease_association',
            target=f'{taxon} / {condition}',
            microbe_id=microbe_id,
            relation_id=f'relation-{microbe_id}-{condition}',
            relation_type=relation_type,
        )
        for microbe_id, taxon, condition, relation_type in (
            ('campy', 'Campylobacter coli', 'diarrhea', 'established_pathogen'),
            ('campy', 'Campylobacter coli', 'gastroenteritis', 'established_pathogen'),
            ('fn', 'Fusobacterium nucleatum', 'cancer', 'epidemiologic_association'),
            ('shigella', 'Shigella dysenteriae', 'shigellosis', 'established_pathogen'),
            ('aeromonas', 'Aeromonas hydrophila', 'gastroenteritis', 'opportunistic'),
        )
    ]

    bounded = _bounded_priority_public_relation_queries(queries, max_queries=2)

    assert [query.microbe_id for query in bounded] == ['campy', 'shigella']


@pytest.mark.asyncio
async def test_retrieval_queue_starts_next_query_before_slowest_worker_finishes():
    class TimedEvidenceClient:
        def __init__(self):
            self.started = {}
            self.finished = {}

        async def search_detailed(self, query, *, supports, channels):
            loop = asyncio.get_running_loop()
            self.started[query] = loop.time()
            await asyncio.sleep({'slow': 0.08, 'fast': 0.01, 'next': 0.01}[query])
            self.finished[query] = loop.time()
            return [
                EviMedChannelResult(
                    channel,
                    'success' if channel in channels else 'not_requested',
                    [],
                    provider='test',
                )
                for channel in ('guide', 'literature')
            ]

    queries = [
        EvidenceQuery(
            query=query,
            supports=(query,),
            required_concepts=((query,),),
            intent='disease_clinical_significance',
            target=query,
        )
        for query in ('slow', 'fast', 'next')
    ]
    client = TimedEvidenceClient()

    result = await _retrieve_evidence_until(
        client,
        queries,
        deadline=asyncio.get_running_loop().time() + 1.0,
        provider='test',
        concurrency=2,
    )

    assert client.started['next'] < client.finished['slow']
    assert [audit.query_text for audit in result.query_audits] == [
        'slow',
        'fast',
        'next',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['public_web', 'public_literature'])
async def test_public_semantic_queries_use_two_outer_workers(provider):
    class Client:
        def __init__(self):
            self.active = 0
            self.maximum_active = 0

        async def search_detailed(self, query, *, supports, channels):
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return [
                EviMedChannelResult(
                    channel,
                    'success' if channel in channels else 'not_requested',
                    [],
                    provider='public_web',
                )
                for channel in ('guide', 'literature')
            ]

    queries = [
        EvidenceQuery(
            query=f'public query {index}',
            supports=(f'target-{index}',),
            required_concepts=((f'target-{index}',),),
            intent='disease_clinical_significance',
            target=f'target-{index}',
        )
        for index in range(4)
    ]
    client = Client()

    await _retrieve_evidence_until(
        client,
        queries,
        deadline=time.monotonic() + 1.0,
        provider=provider,
        concurrency=8,
    )

    assert client.maximum_active == 2


@pytest.mark.asyncio
async def test_report_lifetime_retrieval_cache_reuses_exact_semantic_query():
    class Client:
        def __init__(self):
            self.calls = 0

        async def search_detailed(self, query, *, supports, channels):
            self.calls += 1
            return [
                EviMedChannelResult(
                    channel,
                    'success' if channel in channels else 'not_requested',
                    [],
                    provider='test',
                )
                for channel in ('guide', 'literature')
            ]

    query = EvidenceQuery(
        query='same semantic query',
        supports=('target',),
        required_concepts=(('target',),),
        intent='disease_clinical_significance',
        target='target',
    )
    client = Client()
    token = _ACTIVE_PROGRESS.set(_GenerationProgress())
    try:
        first = await _retrieve_evidence_until(
            client,
            [query],
            deadline=time.monotonic() + 1.0,
            provider='test',
        )
        second = await _retrieve_evidence_until(
            client,
            [query],
            deadline=time.monotonic() + 1.0,
            provider='test',
        )
    finally:
        _ACTIVE_PROGRESS.reset(token)

    assert client.calls == 1
    assert first.query_audits[0].query_id == second.query_audits[0].query_id


@pytest.mark.asyncio
async def test_report_lifetime_interpretation_cache_reuses_exact_semantic_record():
    class UnexpectedNarrativeClient:
        async def generate(self, payload):
            raise AssertionError('cached interpretation should avoid a model call')

    first = EvidenceRecord(
        evidenceKey='semantic-record',
        category='literature',
        title='Human cohort',
        source='Journal',
        interpretation={
            'claim': '人体队列研究观察到目标菌与目标疾病相关。',
            'studyDesign': '队列研究',
            'direction': 'associated',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'sampleAlignment': 'stool',
            'patientApplicability': 'contextual',
        },
    )
    second = first.model_copy(deep=True)
    second.interpretation = None
    progress_token = _ACTIVE_PROGRESS.set(_GenerationProgress())
    try:
        await _interpret_evidence_until(
            UnexpectedNarrativeClient(),
            records=[first],
            numbers={first.evidence_key: 1},
            patient_context={'subject': {'age': 57}},
            deadline=asyncio.get_running_loop().time() + 1.0,
            _retry_enrichment=False,
        )
        completed = await _interpret_evidence_until(
            UnexpectedNarrativeClient(),
            records=[second],
            numbers={second.evidence_key: 1},
            patient_context={'subject': {'age': 57}},
            deadline=asyncio.get_running_loop().time() + 1.0,
            _retry_enrichment=False,
        )
    finally:
        _ACTIVE_PROGRESS.reset(progress_token)

    assert completed == {1}
    assert second.interpretation is not None
    assert second.interpretation.claim == first.interpretation.claim


@pytest.mark.asyncio
async def test_process_interpretation_cache_reuses_same_evidence_and_context():
    class UnexpectedNarrativeClient:
        async def generate(self, payload):
            raise AssertionError('process cache should avoid a model call')

    first = EvidenceRecord(
        evidenceKey='process-semantic-record-unique',
        category='literature',
        title='Unique human cohort for process cache',
        source='Journal',
        interpretation={
            'claim': '人体队列研究观察到目标菌与目标疾病相关。',
            'studyDesign': '队列研究',
            'direction': 'associated',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'sampleAlignment': 'stool',
            'patientApplicability': 'contextual',
        },
    )
    context = {'subject': {'age': 57, 'sex': '男'}}
    first_token = _ACTIVE_PROGRESS.set(_GenerationProgress())
    try:
        await _interpret_evidence_until(
            UnexpectedNarrativeClient(),
            records=[first],
            numbers={first.evidence_key: 1},
            patient_context=context,
            deadline=asyncio.get_running_loop().time() + 1.0,
            _retry_enrichment=False,
        )
    finally:
        _ACTIVE_PROGRESS.reset(first_token)

    second = first.model_copy(deep=True)
    second.interpretation = None
    second_token = _ACTIVE_PROGRESS.set(_GenerationProgress())
    try:
        completed = await _interpret_evidence_until(
            UnexpectedNarrativeClient(),
            records=[second],
            numbers={second.evidence_key: 1},
            patient_context=context,
            deadline=asyncio.get_running_loop().time() + 1.0,
            _retry_enrichment=False,
        )
    finally:
        _ACTIVE_PROGRESS.reset(second_token)

    assert completed == {1}
    assert second.interpretation is not None
    assert second.interpretation.claim == first.interpretation.claim


def test_source_bound_diagnostic_recovery_runs_before_public_coverage():
    disease = DiseaseRiskItem(
        diseaseId='disease-thyroid',
        name='甲状腺疾病',
        sourceType='source_report',
    )
    record = EvidenceRecord(
        evidenceKey='thyroid-consensus',
        category='guideline',
        title='体检人群甲状腺功能检测管理专家共识',
        source='专家共识',
        summary=('甲状腺功能异常的评估建议进行促甲状腺激素（TSH）和游离甲状腺素（FT4）检测。'),
        supports=[disease.disease_id],
        targets=[disease.name],
        intents=['diagnostic_guideline'],
    )

    _recover_public_eligible_interpretations(
        diseases=DiseaseRisksSection(items=[disease]),
        records=[record],
        subject=SubjectInfo(age=57),
    )

    assert record.interpretation is not None
    assert record.interpretation.target_alignment == 'direct'
    assert [item.name for item in record.interpretation.recommended_evaluations] == ['甲状腺功能检查（TSH、FT4）']


def test_source_disease_rows_stay_out_of_public_findings_without_evidence():
    report = {
        'abnormalMicrobes': {'items': []},
        'diseaseRisks': {
            'items': [
                {
                    'diseaseId': 'disease-first',
                    'name': '来源疾病一',
                    'sourceType': 'source_report',
                    'modelIndex': '12.3',
                },
                {
                    'diseaseId': 'disease-second',
                    'name': '来源疾病二',
                    'sourceType': 'source_report',
                    'sourceResult': '来源报告列出的关注项目',
                },
            ]
        },
    }

    repeated = [
        _fixed_findings(
            report,
            reference_numbers=set(),
            reference_categories={},
        )
        for _ in range(5)
    ]

    assert repeated == [repeated[0]] * 5
    assert repeated[0] == []


@pytest.mark.parametrize('count', [0, 3, 5, 8])
def test_public_diseases_are_capped_after_ranking_without_mutating_source(monkeypatch, count):
    from copy import deepcopy

    # Put the strongest patient match last, so truncation before sorting fails.
    diseases = [
        {
            'item_id': f'disease-{i}',
            'finding_type': 'disease',
            'priority': '建议关注' if i < 2 else '重点关注',
            'evidence_level': '可能相关',
            'indicators': [{'status': '偏高'}] if i == count - 1 else [],
        }
        for i in range(count)
    ]
    microbes = [
        {
            'item_id': f'microbe-{i}',
            'finding_type': 'microbe',
            'related_diseases_or_adverse_effects': [{'name': '关联方向'}],
        }
        for i in range(7)
    ]
    report = {'diseaseRisks': {'items': diseases}, 'abnormalMicrobes': {'items': microbes}}
    original = deepcopy(report)
    monkeypatch.setattr('modules.report.callback._disease_finding', lambda item, **kwargs: deepcopy(item))
    monkeypatch.setattr('modules.report.callback._microbe_finding', lambda item, **kwargs: deepcopy(item))
    findings = _fixed_findings(report, reference_numbers=set(), reference_categories={})
    selected = [item['item_id'] for item in findings if item['finding_type'] == 'disease']
    expected = [item['item_id'] for _, item in sorted(
        enumerate(diseases), key=lambda pair: _finding_sort_key(pair[1], pair[0]),
    )[:5]]
    assert selected == expected
    assert len(selected) == min(count, 5)
    assert [item for item in findings if item['finding_type'] == 'microbe'] == microbes
    assert report == original


def test_callback_sections_use_stable_attention_priority_order():
    findings = [
        {'priority': '建议关注', 'finding_type': 'disease'},
        {'priority': '重点关注', 'finding_type': 'disease'},
    ]
    ordered_findings = sorted(
        enumerate(findings),
        key=lambda pair: _finding_sort_key(pair[1], pair[0]),
    )
    assert [item[1]['priority'] for item in ordered_findings] == [
        '重点关注',
        '建议关注',
    ]

    assessment = _patient_health_assessment(
        {
            'dimensions': [
                {
                    'code': 'cardiovascular_risk',
                    'score': 45,
                    'scoreAvailable': True,
                    'evidenceUsed': [
                        {
                            'indicatorCode': 'ldl',
                            'item': '低密度脂蛋白胆固醇',
                            'value': '3.70',
                        },
                        {
                            'indicatorCode': 'smoking',
                            'item': '吸烟状态',
                            'value': '不吸烟',
                        },
                    ],
                    'ruleBasis': [
                        {
                            'ruleId': 'same-rule',
                            'indicator': '低密度脂蛋白胆固醇',
                            'source': '同一标准',
                            'version': '1',
                            'boundary': '相同判定边界',
                        },
                        {
                            'ruleId': 'same-rule',
                            'indicator': '总胆固醇',
                            'source': '同一标准',
                            'version': '1',
                            'boundary': '相同判定边界',
                        },
                    ],
                },
                {
                    'code': 'gut_barrier',
                    'score': 70,
                    'scoreAvailable': True,
                },
            ],
            'abnormalIndicators': [
                {
                    'indicatorCode': 'gut',
                    'dimensionCode': 'gut_barrier',
                    'severity': 'attention',
                    'status': '偏低',
                    'date': '2026-08-01',
                },
                {
                    'indicatorCode': 'ldl',
                    'dimensionCode': 'cardiovascular_risk',
                    'severity': 'priority_attention',
                    'status': '显著升高',
                    'date': '2026-07-01',
                },
            ],
        }
    )
    assert [item['indicatorCode'] for item in assessment['abnormalIndicators']] == ['ldl', 'gut']
    cardiovascular = assessment['dimensions'][0]
    assert [item['indicatorCode'] for item in cardiovascular['evidenceUsed']] == ['smoking']
    assert cardiovascular['ruleBasis'] == [
        {
            'ruleId': 'same-rule',
            'indicator': '低密度脂蛋白胆固醇、总胆固醇',
            'source': '同一标准',
            'version': '1',
            'boundary': '相同判定边界',
        }
    ]
    recommendations = _health_recommendations(
        {
            'recommendations': [
                {
                    'category': 'general',
                    'topic': '胃肠道与排便表现',
                    'priority': '建议关注',
                    'content': '相关变化可能影响胃肠道舒适度。',
                },
                {
                    'category': 'general',
                    'topic': '心血管与代谢健康',
                    'priority': '重点关注',
                    'content': '持续异常可能增加长期心血管风险。',
                },
                {
                    'category': 'diet',
                    'topic': '饮食结构',
                    'priority': '重点关注',
                    'content': '地中海饮食被证实能预防心血管疾病。',
                },
                {
                    'category': 'exercise',
                    'topic': '运动与体能',
                    'priority': '建议关注',
                    'content': ('增加有氧运动与抗阻训练相结合的运动方式可能改善心血管风险。'),
                },
                {
                    'category': 'general',
                    'topic': '心血管与代谢健康异常管理',
                    'priority': '重点关注',
                    'content': '同一健康影响不应因标题措辞不同而重复展示。',
                },
                {
                    'category': 'general',
                    'topic': '胃肠道与排便表现管理',
                    'priority': '建议关注',
                    'content': '包含关系的健康方向也不应重复展示。',
                },
            ]
        },
        set(),
        report={},
    )
    assert [item['priority'] for item in recommendations] == [
        '重点关注',
        '建议关注',
        '建议关注',
    ]
    assert all('被证实能预防' not in item['content'] for item in recommendations)
    assert recommendations[-1]['content'] == ('将有氧运动与抗阻训练结合可能改善心血管风险。')


def test_callback_hides_model_language_and_explains_concrete_health_risks():
    assessment = _patient_health_assessment(
        {
            'dimensions': [],
            'abnormalIndicators': [
                {
                    'indicatorCode': 'barrier_aux:acetate',
                    'item': '乙酸合成能力',
                    'status': '稍弱',
                    'clinicalSignificance': (
                        '乙酸合成能力稍弱，可能影响肠道短链脂肪酸生成，进而减弱抗炎和屏障维持作用。'
                    ),
                },
                {
                    'indicatorCode': 'barrier_aux:butyrate',
                    'item': '丁酸合成能力',
                    'status': '稍弱',
                    'clinicalSignificance': ('该项为基于菌群数据推断的代谢能力模型值，不等同于实际测得的丁酸浓度。'),
                },
                {
                    'indicatorCode': 'barrier_core:gut_immunity',
                    'item': '肠道免疫力',
                    'status': '稍弱',
                    'clinicalSignificance': ('该项为基于菌群数据推断的功能模型值，不等同于直接测得的人体免疫能力。'),
                },
            ],
        }
    )

    by_code = {item['indicatorCode']: item['clinicalSignificance'] for item in assessment['abnormalIndicators']}
    assert '炎症性肠病' in by_code['barrier_aux:butyrate']
    assert '结直肠健康风险' in by_code['barrier_aux:butyrate']
    assert '感染性腹泻' in by_code['barrier_core:gut_immunity']
    assert '胃肠道炎症' in by_code['barrier_aux:acetate']
    assert '糖脂代谢' in by_code['barrier_aux:acetate']
    assert all(marker not in text for text in by_code.values() for marker in ('模型', '推断', '不等同于', '直接测得'))


def test_evidence_card_binds_explicit_patient_indicator_and_microbe_facts():
    references = [
        {'number': 1, 'patient_related_facts': []},
        {'number': 2, 'patient_related_facts': []},
    ]
    output = _attach_patient_related_facts(
        references,
        findings=[
            {
                'finding_type': 'microbe',
                'problem': '艰难梭菌',
                'indicators': [{'name': None, 'value': '0.0711%', 'status': '检出'}],
                'related_diseases_or_adverse_effects': [],
                'interpretation_evidence_refs': [1],
                'recommendation_evidence_refs': [],
                'evidence_refs': [1],
            }
        ],
        health_assessment={
            'abnormalIndicators': [
                {
                    'indicatorCode': 'platelet_count',
                    'item': '血小板计数',
                    'value': '420',
                    'unit': '10^9/L',
                    'status': '偏高',
                }
            ]
        },
        health_recommendations=[],
        recommended_examinations=[],
        reference_metadata={
            1: {'title': 'Clostridioides difficile clinical evidence'},
            2: {'claim': '血小板计数偏高与相关风险的人体研究。'},
        },
    )

    assert output[0]['patient_related_facts'] == ['艰难梭菌：检出，0.0711%']
    assert output[1]['patient_related_facts'] == ['血小板计数：偏高，420 10^9/L']


def test_evidence_card_uses_original_source_fact_for_disease_and_examination():
    references = [{'number': number, 'patient_related_facts': []} for number in (1, 2, 3)]
    output = _attach_patient_related_facts(
        references,
        findings=[
            {
                'finding_type': 'microbe',
                'problem': '嗜热链球菌',
                'indicators': [{'name': None, 'value': '0.0151%', 'status': '丰度低'}],
                'related_diseases_or_adverse_effects': [{'name': '结直肠癌', 'evidence_refs': [1]}],
                'interpretation_evidence_refs': [1],
                'recommendation_evidence_refs': [],
                'evidence_refs': [1],
            },
            {
                'finding_type': 'disease',
                'problem': '结直肠癌',
                'interpretation_evidence_refs': [2],
                'recommendation_evidence_refs': [3],
                'evidence_refs': [2, 3],
            },
        ],
        health_assessment={'abnormalIndicators': []},
        health_recommendations=[],
        recommended_examinations=[
            {
                'name': '便潜血检查',
                'reason': '用于筛查结直肠癌。',
                'evidence_refs': [3],
            }
        ],
        reference_metadata={},
    )

    assert all(item['patient_related_facts'] == ['嗜热链球菌：丰度低，0.0151%'] for item in output)
    assert output[2]['conclusion'] == (
        '嗜热链球菌：丰度低，0.0151%提示需关注结直肠癌；'
        '建议进行便潜血检查，用于筛查结直肠癌。'
    )
    assert all(
        '菌群关联风险' not in fact and '建议补充' not in fact
        for item in output
        for fact in item['patient_related_facts']
    )


def test_evidence_card_binds_assessment_fact_named_by_disease_explanation():
    references = [
        {'number': 1, 'patient_related_facts': []},
        {'number': 6, 'patient_related_facts': []},
    ]
    output = _attach_patient_related_facts(
        references,
        findings=[
            {
                'finding_type': 'disease',
                'problem': '2型糖尿病',
                'biological_mechanism': (
                    'Clostridium bolteae（超标）、粪杆菌属（缺乏）或'
                    '普拉梭菌（缺乏）相关变化提示需关注2型糖尿病相关风险。'
                ),
                'interpretation_evidence_refs': [6],
                'recommendation_evidence_refs': [1],
                'evidence_refs': [6, 1],
            }
        ],
        health_assessment={
            'dimensions': [
                {
                    'evidenceUsed': [
                        {
                            'indicatorCode': 'microbe_composition:faecalibacterium',
                            'item': '普拉梭菌',
                            'value': '0.0296',
                            'unit': '%',
                            'status': '缺乏',
                        },
                        {
                            'indicatorCode': 'microbe_composition:unrelated',
                            'item': '无关菌种',
                            'value': '1.0',
                            'unit': '%',
                            'status': '超标',
                        },
                    ]
                }
            ],
            'abnormalIndicators': [],
        },
        health_recommendations=[],
        recommended_examinations=[
            {
                'name': '口服葡萄糖耐量试验',
                'reason': '用于评估血糖代谢情况。',
                'evidence_refs': [1],
            }
        ],
        reference_metadata={},
    )

    assert output[0]['patient_related_facts'] == ['普拉梭菌：缺乏，0.0296%']
    assert output[1]['patient_related_facts'] == ['普拉梭菌：缺乏，0.0296%']


def test_evidence_card_binds_original_source_related_microbes():
    source_facts = _source_disease_patient_facts(
        {
            'diseaseRisks': {
                'items': [
                    {
                        'name': '甲状腺癌',
                        'displayName': '甲状腺癌',
                        'relatedMicrobes': ['粪杆菌属(缺乏)', '拟杆菌属（缺乏）'],
                    }
                ]
            }
        }
    )
    output = _attach_patient_related_facts(
        [{'number': 2, 'patient_related_facts': []}],
        findings=[
            {
                'finding_type': 'disease',
                'problem': '甲状腺癌',
                'biological_mechanism': ('粪杆菌属（缺乏）和拟杆菌属（缺乏）相关变化提示需关注甲状腺癌相关风险。'),
                'interpretation_evidence_refs': [2],
                'recommendation_evidence_refs': [],
                'evidence_refs': [2],
            }
        ],
        health_assessment={'dimensions': [], 'abnormalIndicators': []},
        health_recommendations=[],
        recommended_examinations=[],
        reference_metadata={},
        disease_source_facts=source_facts,
    )

    assert output[0]['patient_related_facts'] == [
        '粪杆菌属：缺乏',
        '拟杆菌属：缺乏',
    ]


def test_fixed_findings_keeps_source_microbe_when_module_would_be_empty():
    findings = _fixed_findings(
        {
            'abnormalMicrobes': {
                'items': [
                    {
                        'microbeId': 'taxon-source-only',
                        'name': '青春双歧杆菌',
                        'scientificName': 'Bifidobacterium adolescentis',
                        'category': 'beneficial_abnormal',
                        'sourceFinding': {'status': '丰度低', 'value': '0.0062%'},
                        'diseaseAndRisks': [],
                        'medicalInterpretation': '本次检测显示该菌丰度较低。',
                        'evidenceRefs': [],
                    }
                ]
            },
            'diseaseRisks': {'items': []},
        },
        reference_numbers=set(),
        reference_categories={},
    )

    assert len(findings) == 1
    assert findings[0]['problem'] == '青春双歧杆菌 (Bifidobacterium adolescentis)'
    assert findings[0]['related_diseases_or_adverse_effects'] == []
    assert findings[0]['biological_mechanism'] == (
        '该菌属于常见肠道共生菌，但单次低丰度结果尚不足以判断具体健康影响。'
    )


def test_fixed_findings_still_prefers_evidence_bound_microbes():
    findings = _fixed_findings(
        {
            'abnormalMicrobes': {
                'items': [
                    {
                        'microbeId': 'taxon-source-only',
                        'name': '青春双歧杆菌',
                        'category': 'beneficial_abnormal',
                        'sourceFinding': {'status': '丰度低', 'value': '0.0062%'},
                        'diseaseAndRisks': [],
                    },
                    {
                        'microbeId': 'taxon-supported',
                        'name': '示例菌',
                        'category': 'other_abnormal',
                        'sourceFinding': {'status': '超标', 'value': '1%'},
                        'diseaseAndRisks': [
                            {
                                'name': '肠道健康风险',
                                'description': '示例菌与肠道健康风险相关。',
                                'evidenceRefs': [1],
                                'patientApplicability': 'direct',
                                'sampleAlignment': 'stool',
                            }
                        ],
                        'medicalInterpretation': '示例菌与肠道健康风险相关。',
                        'evidenceRefs': [1],
                    },
                ]
            },
            'diseaseRisks': {'items': []},
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['microbe_disease_association'],
                'target_alignment': 'direct',
                'taxon_alignment': 'species',
                'patient_applicability': 'direct',
                'sample_alignment': 'stool',
                'claim': '示例菌与肠道健康风险相关。',
            }
        },
    )

    assert [finding['problem'] for finding in findings] == ['示例菌']


def test_disease_callback_accepts_disease_level_gut_evidence_without_taxon_claim():
    finding = _disease_finding(
        {
            'diseaseId': 'disease-as',
            'name': '强直性脊柱炎',
            'sourceType': 'source_report',
            'sourceResult': '原报告关联模型结果 40；中风险',
            'modelIndex': '40',
            'relatedMicrobes': ['粪拟杆菌(超标)'],
            'medicalInterpretation': '强直性脊柱炎患者的肠道菌群组成存在差异。',
            'associationEvidenceRefs': [1],
            'suggestedEvaluations': [],
            'existingResults': [],
        },
        reference_numbers={1},
        reference_categories={
            1: {
                'category': 'literature',
                'intents': ['disease_clinical_significance'],
                'target_alignment': 'direct',
                'taxon_alignment': 'unclear',
                'patient_applicability': 'contextual',
                'sample_alignment': 'stool',
                'claim': '强直性脊柱炎患者的肠道菌群组成与健康人存在显著差异。',
            }
        },
    )

    assert finding is not None
    assert finding['problem'] == '强直性脊柱炎'
    assert finding['biological_mechanism'] == (
        '相关菌群特征提示需关注强直性脊柱炎相关风险。'
    )


def test_session_fallbacks_fill_missing_request_without_overwriting_explicit_data():
    request = ReportRequest.model_validate(
        {
            'userId': 'u-session',
            'sessionId': 's-session',
            'name': '接口姓名',
        }
    )
    merged = merge_request_session_fallbacks(
        request,
        {
            'extractedInfo': {
                'name': '会话姓名',
                'gender': '女',
                'age': '57',
            },
            'healthData': {
                'fileList': [{'url': 'https://example.com/report.pdf', 'fileType': 'pdf'}],
                'hospitalData': [
                    {
                        'dataType': 'INSPECTION',
                        'itemName': '总胆固醇',
                        'testResultValue': '6.48',
                    }
                ],
            },
        },
    )

    assert merged.name == '接口姓名'
    assert merged.sex == '女'
    assert merged.age == 57
    assert len(merged.file_list) == 1
    assert merged.hospital_data[0].item_name == '总胆固醇'


@pytest.mark.parametrize('file_type', ['image', 'jpg', 'jpeg', 'png', 'bmp', 'gif', None])
def test_session_image_types_are_normalized_without_accepting_legacy_public_inputs(file_type):
    item = {'url': 'https://example.com/report.jpg'}
    if file_type is not None:
        item['fileType'] = file_type
    session = {'healthData': {'fileList': [item]}}
    request = ReportRequest.model_validate({'userId': 'u-image', 'sessionId': 's-image'})
    merged = merge_request_session_fallbacks(request, session)
    assert merged.file_list[0].file_type == 'image'
    assert item.get('fileType') == file_type
    assert request.file_list == []
    explicit = ReportRequest.model_validate(
        {
            'userId': 'u-image',
            'sessionId': 's-image',
            'fileList': [{'url': 'https://example.com/new.pdf', 'fileType': 'pdf'}],
        }
    )
    assert merge_request_session_fallbacks(explicit, session).file_list == explicit.file_list
    if file_type != 'image':
        with pytest.raises(ValueError):
            ReportRequest.model_validate({'userId': 'u-image', 'sessionId': 's-image', 'fileList': [item]})


def test_malformed_legacy_file_url_does_not_abort_other_session_fallbacks():
    request = ReportRequest.model_validate({'userId': 'u-image', 'sessionId': 's-image'})
    merged = merge_request_session_fallbacks(
        request,
        {
            'extractedInfo': {'age': '57'},
            'healthData': {'fileList': [{'url': 'https://[invalid'}]},
        },
    )
    assert merged.file_list == []
    assert merged.age == 57


def test_session_context_keeps_user_text_but_removes_sources_and_assistant_text():
    context = extract_session_context(
        {
            'healthData': {
                'name': '患者',
                'fileList': [{'url': 'https://example.com/report.pdf', 'fileType': 'pdf'}],
                'hospitalData': [{'itemName': '总胆固醇'}],
            },
            'messages': [
                {'role': 'user', 'content': '近期睡眠约6小时'},
                {'role': 'assistant', 'content': '建议诊断为某疾病'},
            ],
        }
    )

    assert context['healthData'] == {'name': '患者'}
    assert context['messages'] == [{'role': 'user', 'content': '近期睡眠约6小时'}]


@pytest.mark.asyncio
async def test_report_session_loader_reads_active_redis_session_before_mongodb():
    class Redis:
        async def get(self, key):
            assert key == 'shulan:session:u1:s1'
            return json.dumps({'status': 'active', 'answers': {'sleep': '6小时'}})

    class Mongo:
        async def find_one(self, *_args, **_kwargs):
            raise AssertionError('MongoDB should not be queried after a Redis hit')

    result = await load_report_session(
        'u1',
        's1',
        redis_client=Redis(),
        session_collection=Mongo(),
    )

    assert result == {'status': 'active', 'answers': {'sleep': '6小时'}}


@pytest.mark.asyncio
async def test_report_session_loader_falls_back_to_mongodb_after_invalid_cache():
    class Redis:
        async def get(self, _key):
            return 'not-json'

    class Mongo:
        async def find_one(self, query, projection):
            assert query == {'userId': 'u1', 'sessionId': 's1'}
            assert projection == {'_id': 0}
            return {'status': 'completed', 'qaHistory': []}

    result = await load_report_session(
        'u1',
        's1',
        redis_client=Redis(),
        session_collection=Mongo(),
    )

    assert result == {'status': 'completed', 'qaHistory': []}


def test_callback_fills_empty_measured_indicator_clinical_significance():
    assessment = _patient_health_assessment(
        {
            'dimensions': [],
            'abnormalIndicators': [
                {
                    'indicatorCode': 'total_cholesterol',
                    'item': '总胆固醇',
                    'status': '偏高',
                    'clinicalSignificance': None,
                },
                {
                    'indicatorCode': 'unmapped-indicator',
                    'item': None,
                    'status': None,
                    'dimensionName': '心血管健康',
                    'clinicalSignificance': None,
                },
            ],
        }
    )

    significance = [item['clinicalSignificance'] for item in assessment['abnormalIndicators']]
    assert all(significance)
    assert '动脉粥样硬化性心血管风险' in significance[0]
    assert significance[1].startswith('该指标异常')


def test_health_profile_does_not_turn_stool_signal_into_infection_risk():
    context = _clinical_context(
        {},
        {'healthProfile': ('当前优先关注血脂调控和肠道感染相关风险，并持续开展健康管理。')},
    )

    profile = context['综合健康画像']
    assert '肠道感染相关风险' not in profile
    assert '胃肠道症状变化及必要时的病原学确认' in profile


def test_repeated_disease_suggestion_openings_are_varied_after_two_rows():
    disease_findings = [
        (
            index,
            {
                'tips': {
                    'special_reminder': f'可通过检查{index}进一步评估相关情况。',
                    'practical_goal': None,
                }
            },
        )
        for index in range(5)
    ]

    _vary_repeated_disease_suggestion_openings(disease_findings)

    suggestions = [finding['tips']['special_reminder'] for _index, finding in disease_findings]
    assert suggestions[:2] == [
        '可通过检查0进一步评估相关情况。',
        '可通过检查1进一步评估相关情况。',
    ]
    assert suggestions[2].startswith('建议通过')
    assert suggestions[3].startswith('后续可通过')
    assert suggestions[4].startswith('建议通过')
    assert sum(item.startswith('可通过') for item in suggestions) == 2


def test_health_summary_fills_two_distinct_action_directions_from_assessment():
    recommendations = _health_recommendations(
        {
            'recommendations': [
                {
                    'category': 'general',
                    'topic': '肠道微生态',
                    'priority': '重点关注',
                    'content': '保持饮食结构多样和作息规律，观察胃肠道功能变化。',
                }
            ]
        },
        set(),
        report={
            'healthAssessment': {
                'dimensions': [
                    {
                        'code': 'digestive_system',
                        'name': '消化系统',
                        'level': 'priority_attention',
                    },
                    {
                        'code': 'metabolic_system',
                        'name': '代谢系统',
                        'level': 'attention',
                    },
                ]
            }
        },
    )

    assert len(recommendations) == 2
    assert [item['topic'] for item in recommendations] == [
        '肠道微生态',
        '心血管与代谢管理',
    ]


def test_species_relation_rejects_genus_level_evidence():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-bf'
    microbe.scientific_name = 'Bacteroides fragilis'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='genus-only',
        category='literature',
        title='Bacteroides and colitis',
        source='Journal',
        interpretation={
            'claim': 'Bacteroides was associated with colitis.',
            'targetAlignment': 'direct',
            'taxonAlignment': 'genus',
            'patientApplicability': 'direct',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-bf-colitis',
                microbe_id='taxon-bf',
                microbe_name='Bacteroides fragilis',
                disease_id='disease-colitis',
                disease_name='结肠炎',
            )
        ],
    )
    assert _relation_record_is_public(record, microbes) is False
    record.interpretation.taxon_alignment = 'species'
    assert _relation_record_is_public(record, microbes) is True
    record.interpretation.patient_applicability = 'contextual'
    assert _relation_record_is_public(record, microbes) is True


def test_plant_disease_relation_is_never_public_for_a_patient_report():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-puccinia'
    microbe.scientific_name = 'Puccinia striiformis'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='wheat-yellow-rust',
        category='literature',
        title='Puccinia striiformis causes wheat yellow rust',
        source='Plant Pathology',
        summary='A plant pathogen responsible for wheat rust disease.',
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='puccinia-wheat-rust',
                microbe_id='taxon-puccinia',
                microbe_name='Puccinia striiformis',
                disease_id='disease-wheat-rust',
                disease_name='小麦黄锈病',
            )
        ],
        interpretation={
            'claim': 'Puccinia striiformis可导致小麦黄锈病。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
    )
    assert (
        _relation_record_is_public(
            record,
            microbes,
            subject=SubjectInfo(age=28, sex='男'),
        )
        is False
    )


def test_adult_male_report_rejects_age_sex_and_treatment_specific_relations():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-ca'
    microbe.scientific_name = 'Candida albicans'
    microbes.items = [microbe]

    def relation_record(title):
        return EvidenceRecord(
            evidence_key=title,
            category='literature',
            title=title,
            source='Journal',
            interpretation={
                'claim': title,
                'targetAlignment': 'direct',
                'taxonAlignment': 'species',
                'patientApplicability': 'contextual',
            },
            relation_bindings=[
                EvidenceRelationBinding(
                    relation_id='relation-ca',
                    microbe_id='taxon-ca',
                    microbe_name='Candida albicans',
                    disease_id='disease-candidiasis',
                    disease_name='念珠菌病',
                )
            ],
        )

    subject = SubjectInfo(age=28, sex='男')
    assert not _relation_record_is_public(
        relation_record('Candida albicans in pregnant women'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('复发性念珠菌性阴道炎患者的阴道白念珠菌'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans and preterm birth'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans infection in children'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('婴儿人群中的 Candida albicans 念珠菌病'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans during immune checkpoint therapy'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans detected in a root canal infection'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans infection in transplant patients'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans pathogen in selective IgA deficiency'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans infection in immunocompromised patients'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans cluster among injection drug users'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans association in Vietnamese patients'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('免疫受损患者中的 Candida albicans 感染'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans sepsis in a thalassemia patient'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('免疫功能低下患者中的 Candida albicans 感染'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans epidemiology in Western Africa'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans immune reconstitution syndrome'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans in 日本旅行者'),
        microbes,
        subject=subject,
    )
    assert not _relation_record_is_public(
        relation_record('Candida albicans infection in cystic fibrosis'),
        microbes,
        subject=subject,
    )


def test_laboratory_transformation_paper_is_not_public_relation_evidence():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-fn'
    microbe.scientific_name = 'Fusobacterium nucleatum'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='transformation-method',
        category='literature',
        title='Genetic Transformation of Fusobacterium nucleatum',
        source='Methods in Molecular Biology',
        summary='An experimental protocol for genetic transformation.',
        interpretation={
            'claim': 'Fusobacterium nucleatum was studied.',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-fn',
                microbe_id='taxon-fn',
                microbe_name='Fusobacterium nucleatum',
                disease_id='disease-crc',
                disease_name='结直肠癌',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


def test_exact_pathogen_review_survives_repeated_public_gates():
    microbe = SimpleNamespace(
        microbe_id='taxon-campy',
        scientific_name='Campylobacter coli',
    )
    microbes = SimpleNamespace(items=[microbe])
    record = EvidenceRecord(
        evidence_key='campylobacter-review',
        category='literature',
        title='Campylobacter flagella: not just for motility',
        source='Clinical Microbiology Reviews',
        summary=(
            'Campylobacter jejuni and Campylobacter coli are among the major '
            'causes of diarrheal disease worldwide. Later sections describe '
            'interactions with epithelial cells.'
        ),
        study_types=['Journal Article', 'Review'],
        microbe_ids=['taxon-campy'],
        intents=['microbe_disease_association'],
        interpretation={
            'claim': 'Campylobacter coli与感染性腹泻相关。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-campy-diarrhea',
                microbe_id='taxon-campy',
                microbe_name='Campylobacter coli',
                disease_id='disease-infectious-diarrhea',
                disease_name='感染性腹泻',
                relation_type='established_pathogen',
                claim='Campylobacter coli与感染性腹泻相关。',
                target_alignment='direct',
                taxon_alignment='species',
                patient_applicability='contextual',
            )
        ],
    )

    assert _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(name='患者', age=28, sex='男'),
    )
    assert _microbe_record_is_public(record, microbes)


@pytest.mark.parametrize(
    'title',
    [
        'Virulence in an experimental model of poultry necrotic enteritis',
        'Puccinia striiformis and wheat rust plant disease',
        'Predictive models of biofilm formation on food industry surfaces',
        'Functional insights into a modular LPMO protein',
        'Donor-derived strain associated with fecal microbial transplant failure',
        'Economic analysis of empiric versus diagnostic-driven strategies',
        'Oral-gut axis mechanisms and therapeutic implications',
        '2024 guideline for antibiotic-resistant gram-negative infection treatment',
        '2024抗菌素耐药革兰阴性菌感染治疗指南',
        'Growth inhibitory activity of plant extract in prevention and treatment of infection',
        '鼠李糖乳杆菌抑制阪崎克罗诺杆菌致脑膜炎的体内外实验',
        'Live attenuated Example species mutants as vaccine candidates',
        'Example species and retention of medical devices',
        'Immune response to Example species in infected patients',
        'Example species disease of the sinus and orbit',
        'Latent tuberculosis among healthcare workers',
        'Aeromonas infection in hemodialysis patients',
        'Mycobacterium fortuitum prosthetic valve endocarditis',
    ],
)
def test_nonhuman_or_nonclinical_relation_sources_are_not_public(title):
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-test'
    microbe.scientific_name = 'Example species'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key=title,
        category='literature',
        title=title,
        source='Journal',
        interpretation={
            'claim': 'Example species was associated with a disease.',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-test',
                microbe_id='taxon-test',
                microbe_name='Example species',
                disease_id='disease-test',
                disease_name='示例疾病',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


@pytest.mark.parametrize(
    'site_specific_outcome',
    [
        '社区获得性肺炎',
        '小脑功能障碍',
        '无创伤性脾破裂',
        '复杂性皮肤和软组织感染',
        '皮肤和软组织感染',
        '头皮屑',
        '椎间盘炎',
        '口腔念珠菌病',
        '口腔白斑',
        '糖尿病足感染',
        '创伤伤口感染',
        '创面感染',
        '甲真菌病',
        '侵袭性肺曲霉病',
        '支气管扩张',
        '真菌致敏',
        '角膜炎',
        '食管念珠菌病',
        '牙周炎',
        '性传播感染',
        '头屑',
        '念珠菌性食管炎',
        '胆道感染',
        '抗菌素耐药革兰氏阴性菌感染',
        '胃肠道疾病',
        '机会性病原体',
        '肺炎',
    ],
)
def test_stool_report_does_not_publish_unrelated_body_site_outcomes(
    site_specific_outcome,
):
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-ca'
    microbe.scientific_name = 'Candida albicans'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key=f'site-{site_specific_outcome}',
        category='literature',
        title='Human clinical association study',
        source='Journal',
        interpretation={
            'claim': f'人体研究报告与{site_specific_outcome}相关。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id=f'relation-{site_specific_outcome}',
                microbe_id='taxon-ca',
                microbe_name='Candida albicans',
                disease_id=f'disease-{site_specific_outcome}',
                disease_name=site_specific_outcome,
                relation_type='established_pathogen',
            )
        ],
    )
    assert not _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(age=28, sex='男'),
    )


def test_exact_pathogen_lower_respiratory_relation_is_public_evidence():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-pseudomonas'
    microbe.scientific_name = 'Pseudomonas aeruginosa'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='pseudomonas-lrti-consensus',
        category='guideline',
        title='中国铜绿假单胞菌下呼吸道感染诊治专家共识',
        source='中华医学会',
        summary='铜绿假单胞菌是下呼吸道感染常见致病菌。',
        interpretation={
            'claim': '铜绿假单胞菌是下呼吸道感染常见致病菌。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'sampleAlignment': 'respiratory',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-pseudomonas-lrti',
                microbe_id='taxon-pseudomonas',
                microbe_name='Pseudomonas aeruginosa',
                microbe_aliases=['铜绿假单胞菌'],
                disease_id='disease-lrti',
                disease_name='下呼吸道感染',
                relation_type='established_pathogen',
            )
        ],
    )

    assert _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(age=57, sex='女'),
    )


def test_direct_pathogen_relation_is_not_lost_when_source_also_mentions_resistance():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-salmonella'
    microbe.scientific_name = 'Salmonella enterica'
    microbes.items = [microbe]
    claim = '肠沙门氏菌是肠热病和胃肠炎的病原体，部分分离株存在抗生素耐药基因差异。'
    record = EvidenceRecord(
        evidence_key='salmonella-disease-and-resistance',
        category='literature',
        title='Genetic diversity of Salmonella enterica during acute infections',
        source='Journal',
        summary=(
            'Salmonella enterica is the causative agent of enteric fever and '
            'gastroenteritis. Some isolates differed in antibiotic resistance genes.'
        ),
        interpretation={
            'claim': claim,
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'sampleAlignment': 'stool',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-salmonellosis',
                microbe_id='taxon-salmonella',
                microbe_name='Salmonella enterica',
                microbe_aliases=['肠沙门氏菌'],
                disease_id='disease-salmonellosis',
                disease_name='沙门菌病',
                relation_type='established_pathogen',
                claim=claim,
            )
        ],
    )
    assert _relation_record_is_public(record, microbes)


def test_species_finding_rejects_strain_level_discovery_evidence():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-bf'
    microbe.scientific_name = 'Bacteroides fragilis'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='etbf-only',
        category='literature',
        title='Enterotoxigenic Bacteroides fragilis in Crohn disease tissue',
        source='Journal',
        microbe_ids=['taxon-bf'],
        intents=['microbe_discovery'],
        interpretation={
            'claim': 'Enterotoxigenic Bacteroides fragilis was detected in tissue.',
            'targetAlignment': 'direct',
            'taxonAlignment': 'strain',
            'sampleAlignment': 'mucosa',
            'patientApplicability': 'contextual',
        },
    )
    assert _microbe_record_is_public(record, microbes) is False
    record.interpretation.taxon_alignment = 'species'
    assert _microbe_record_is_public(record, microbes) is True


@pytest.mark.parametrize(
    ('title', 'source', 'summary', 'study_design'),
    [
        (
            'Atraumatic splenic rupture in Legionella pneumonia',
            'Case reports in infectious diseases',
            'Legionella pneumophila was identified in the patient.',
            '病例报告',
        ),
        (
            'Pseudomonas fluorescens host response',
            'Journal',
            'Pseudomonas fluorescens induced defensin in epithelial cells.',
            None,
        ),
        (
            'Drechmeria coniospora infection model',
            'Journal',
            'Drechmeria coniospora is a nematode endoparasite.',
            None,
        ),
    ],
)
def test_case_and_preclinical_relations_are_not_public(title, source, summary, study_design):
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-target'
    microbe.scientific_name = title.split()[0] + ' ' + title.split()[1]
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key=title,
        category='literature',
        title=title,
        source=source,
        summary=summary,
        interpretation={
            'claim': title,
            'studyDesign': study_design,
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-target',
                microbe_id='taxon-target',
                microbe_name=microbe.scientific_name,
                disease_id='disease-target',
                disease_name='感染性疾病',
                relation_type='established_pathogen',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


def test_stool_report_rejects_oral_leukoplakia_source_with_generic_outcome():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-candida-albicans'
    microbe.scientific_name = 'Candida albicans'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='candida-leukoplakia',
        category='literature',
        title='Candida albicans in oral leukoplakia and malignant transformation',
        source='Journal',
        summary='Candida albicans was studied in oral leukoplakia.',
        interpretation={
            'claim': '白念珠菌与恶性转化相关。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-candida-malignant',
                microbe_id='taxon-candida-albicans',
                microbe_name='Candida albicans',
                disease_id='disease-malignant',
                disease_name='恶性转化',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


def test_stool_report_rejects_esbl_surgical_site_oncology_relation():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-ecoli'
    microbe.scientific_name = 'Escherichia coli'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidenceKey='ecoli-esbl-surgery',
        category='literature',
        title='ESBL-producing Escherichia coli in oncology surgical site infection',
        source='Journal',
        summary=('ESBL-producing Escherichia coli was studied in surgical site infections among oncology patients.'),
        interpretation={
            'claim': ('肿瘤科患者手术部位感染产ESBLs的大肠埃希氏菌与侵入性操作及使用抗菌药物有关。'),
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relationBindings=[
            EvidenceRelationBinding(
                relationId='relation-ecoli-surgery',
                microbeId='taxon-ecoli',
                microbeName='Escherichia coli',
                diseaseId='disease-surgical-site-infection',
                diseaseName='手术部位感染',
                relationType='opportunistic',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


def test_action_only_and_tcm_relation_sources_are_not_public():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-cd'
    microbe.scientific_name = 'Clostridioides difficile'
    microbes.items = [microbe]

    def record(title, summary):
        return EvidenceRecord(
            evidence_key=title,
            category='guideline',
            title=title,
            source='Society',
            summary=summary,
            interpretation={
                'claim': summary,
                'targetAlignment': 'direct',
                'taxonAlignment': 'species',
                'patientApplicability': 'contextual',
            },
            relation_bindings=[
                EvidenceRelationBinding(
                    relation_id='relation-cd',
                    microbe_id='taxon-cd',
                    microbe_name='Clostridioides difficile',
                    disease_id='disease-cdi',
                    disease_name='艰难梭菌感染',
                    relation_type='established_pathogen',
                )
            ],
        )

    assert not _relation_record_is_public(
        record(
            'Guideline on fecal microbiota transplantation treatment',
            'Management recommendations for Clostridioides difficile infection.',
        ),
        microbes,
    )
    assert not _relation_record_is_public(
        record(
            '结核病中医辨证与治法方药共识',
            '本共识介绍治疗方法。',
        ),
        microbes,
    )


def test_general_adult_guideline_is_not_rejected_for_mentioning_special_groups():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-botulinum'
    microbe.scientific_name = 'Clostridium botulinum'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='general-botulism-guide',
        category='guideline',
        title='Clinical guidelines for diagnosis and treatment of botulism',
        source='CDC',
        summary=(
            'Botulinum neurotoxin produced by Clostridium botulinum causes '
            'botulism. The general guideline also discusses children and pregnant women.'
        ),
        interpretation={
            'claim': '肉毒梭菌产生的毒素可导致肉毒中毒。',
            'studyDesign': '临床指南',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-botulism',
                microbe_id='taxon-botulinum',
                microbe_name='Clostridium botulinum',
                disease_id='disease-botulism',
                disease_name='肉毒中毒',
                relation_type='established_pathogen',
            )
        ],
    )
    assert _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(age=28, sex='男'),
    )


def test_positive_relation_is_not_rejected_by_negative_control_wording():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-botulinum'
    microbe.scientific_name = 'Clostridium botulinum'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='botulism-negative-controls',
        category='literature',
        title='Stool examination for Clostridium botulinum in botulism',
        source='Journal',
        summary=(
            'Clostridium botulinum was identified in stools from patients with '
            'botulism, but not in controls not associated with confirmed botulism.'
        ),
        interpretation={
            'claim': '肉毒梭菌与肉毒中毒相关。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='botulism-relation',
                microbe_id='taxon-botulinum',
                microbe_name='Clostridium botulinum',
                disease_id='disease-botulism',
                disease_name='肉毒中毒',
                relation_type='established_pathogen',
            )
        ],
    )
    assert _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(age=28, sex='男'),
    )


def test_exact_microbe_diagnostic_guideline_can_support_relation():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-shigella-dysenteriae'
    microbe.scientific_name = 'Shigella dysenteriae'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='who-shigellosis',
        category='guideline',
        title='WHO guide for bacillary dysentery',
        source='WHO',
        summary=(
            'Guide for the diagnosis and management of Shigella dysenteriae, '
            'including an algorithm for management of shigellosis.'
        ),
        interpretation={
            'claim': 'Shigella dysenteriae与细菌性痢疾相关。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-shigellosis',
                microbe_id=microbe.microbe_id,
                microbe_name=microbe.scientific_name,
                disease_id='disease-shigellosis',
                disease_name='细菌性痢疾',
                relation_type='established_pathogen',
            )
        ],
    )
    assert _relation_record_is_public(
        record,
        microbes,
        subject=SubjectInfo(age=28, sex='男'),
    )


def test_model_identified_rare_case_relation_is_not_public():
    microbes = type('Section', (), {})()
    microbe = type('Microbe', (), {})()
    microbe.microbe_id = 'taxon-bc'
    microbe.scientific_name = 'Bacillus cereus'
    microbes.items = [microbe]
    record = EvidenceRecord(
        evidence_key='rare-subdural-case',
        category='literature',
        title='Uncommon pathogen causing subdural empyema',
        source='Journal',
        interpretation={
            'claim': '文献报道蜡样芽孢杆菌导致硬膜下脓肿的罕见病例。',
            'targetAlignment': 'direct',
            'taxonAlignment': 'species',
            'patientApplicability': 'contextual',
        },
        relation_bindings=[
            EvidenceRelationBinding(
                relation_id='relation-bc',
                microbe_id='taxon-bc',
                microbe_name='Bacillus cereus',
                disease_id='disease-empyema',
                disease_name='硬膜下脓肿',
                relation_type='opportunistic',
            )
        ],
    )
    assert not _relation_record_is_public(record, microbes)


@pytest.mark.asyncio
async def test_session_lookup_failure_does_not_block_structured_report():
    async def missing_session(user_id, session_id):
        raise TimeoutError('mongo timeout')

    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=FailingEvidenceClient(),
        callback_client=NullCallback(),
        session_loader=missing_session,
        narrative_client=None,
    )
    request = ReportRequest.model_validate(
        {
            'userId': 'u1',
            'sessionId': 's1',
            'name': '患者',
            'microbiomeData': {
                'overallMetrics': [{'code': 'gmwi2', 'name': '健康指数', 'value': -0.2}],
                'taxa': [
                    {
                        'category': 'pathogenic',
                        'latinName': 'Salmonella enterica',
                        'value': 0.01,
                    }
                ],
            },
        }
    )
    result = await pipeline.generate(request)
    assert result.status == 'partial_success'
    assert result.report.abnormal_microbes.items[0].scientific_name == 'Salmonella enterica'
    assert 'MODEL_NARRATIVE_UNAVAILABLE' in result.partial_reasons
    assert 'EVIDENCE_RETRIEVAL_INCOMPLETE' in result.partial_reasons
    assert 'MICROBE_EVIDENCE_INCOMPLETE' in result.partial_reasons


@pytest.mark.asyncio
async def test_scan_only_clinical_pdf_keeps_health_report_nonempty(monkeypatch):
    class ScanSourceClient:
        async def fetch_file(self, source):
            return DownloadedFile(
                'unknown-hospital.pdf',
                'application/pdf',
                b'scan-only-pdf',
                'pdf',
            )

    class DocumentClient:
        async def extract_text(self, source, *, filename, content=None):
            assert content == b'scan-only-pdf'
            return (
                '姓名：刘杰\n性别：男 年龄：49岁\n'
                '报告日期：2026-08-03\n'
                '血压(收缩压)：129 mmHg\n血压(舒张压)：76 mmHg\n'
                '| 检验项目 | 结果 | 单位 | 参考范围 | 状态 |\n'
                '| --- | --- | --- | --- | --- |\n'
                '| 低密度脂蛋白胆固醇 | 4.21 | mmol/L | 0.00-3.37 | 偏高 |\n'
                '| 首次出现的实验室项目Q | 9.8 | U/L | 1.0-5.0 | 偏高 |\n'
                '体检结论\n'
                '1. 左侧甲状腺结节 TI-RADS 3类。\n'
            )

    async def no_session(_user_id, _session_id):
        return {}

    pipeline = ReportPipeline(
        source_client=ScanSourceClient(),
        document_client=DocumentClient(),
        evidence_client=FailingEvidenceClient(),
        callback_client=NullCallback(),
        session_loader=no_session,
        narrative_client=None,
    )
    request = ReportRequest.model_validate(
        {
            'userId': 'clinical-only-user',
            'sessionId': 'clinical-only-session',
            'fileList': [
                {
                    'url': 'https://example.com/unknown-hospital.pdf',
                    'fileType': 'pdf',
                }
            ],
        }
    )

    result = await pipeline.generate(request)

    assert result.status == 'partial_success'
    assert result.report.abnormal_microbes.items == []
    assert result.report.disease_risks.items == []
    assert result.report.subject == SubjectInfo(name='刘杰', age=49, sex='男')
    cardiovascular = next(
        item for item in result.report.health_assessment.dimensions if item.code == 'cardiovascular_system'
    )
    assert '年龄' not in cardiovascular.missing_evidence
    assert '性别' not in cardiovascular.missing_evidence
    assert any(item.item == 'LDL-C' for item in result.report.health_assessment.abnormal_indicators)
    assert result.report.health_assessment.dimensions
    assert result.report.executive_summary
    assert '肠道菌群检测结果尚在生成中' not in result.report.executive_summary


@pytest.mark.asyncio
async def test_empty_evimed_coverage_triggers_public_evidence_fallback():
    class EmptyEviMed:
        async def search_detailed(self, query, *, supports, channels):
            return [
                EviMedChannelResult(
                    channel,
                    'success' if channel in channels else 'not_requested',
                    [],
                    provider='evimed_v2',
                )
                for channel in ('guide', 'literature')
            ]

    class CountingPublicEvidence:
        def __init__(self):
            self.queries = []

        async def search_detailed(self, query, *, supports, channels):
            self.queries.append((query, supports, channels))
            return [
                EviMedChannelResult(
                    channel,
                    'success' if channel in channels else 'not_requested',
                    [],
                    provider='public_web',
                )
                for channel in ('guide', 'literature')
            ]

    class UnavailableNarrative:
        async def generate(self, payload):
            raise NarrativeError('unavailable')

    async def no_session(user_id, session_id):
        return {}

    public = CountingPublicEvidence()
    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=EmptyEviMed(),
        public_evidence_client=public,
        callback_client=NullCallback(),
        session_loader=no_session,
        narrative_client=UnavailableNarrative(),
    )
    result = await pipeline.generate(
        ReportRequest.model_validate(
            {
                'userId': 'u-public-fallback',
                'sessionId': 's-public-fallback',
                'name': '患者',
                'age': 28,
                'microbiomeData': {
                    'taxa': [
                        {
                            'category': 'pathogenic',
                            'latinName': 'Salmonella enterica',
                            'value': 0.01,
                        }
                    ]
                },
            }
        )
    )

    assert public.queries
    query_texts = [query for query, _supports, _channels in public.queries]
    assert query_texts.index('Salmonella enterica infection') < query_texts.index(
        'Salmonella enterica infection disease'
    )
    assert result.report.abnormal_microbes.items[0].scientific_name == ('Salmonella enterica')
    assert result.status == 'partial_success'


@pytest.mark.asyncio
async def test_session_lookup_is_time_bounded():
    async def hanging_session(user_id, session_id):
        await asyncio.sleep(1)
        return {'symptoms': ['不应到达']}

    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=FailingEvidenceClient(),
        callback_client=NullCallback(),
        session_loader=hanging_session,
        narrative_client=None,
        session_timeout_seconds=0.1,
    )
    request = ReportRequest.model_validate(
        {
            'userId': 'u-timeout',
            'sessionId': 's-timeout',
            'name': '患者',
            'microbiomeData': {
                'taxa': [
                    {
                        'category': 'pathogenic',
                        'latinName': 'Salmonella enterica',
                        'value': 0.01,
                    }
                ]
            },
        }
    )
    started = asyncio.get_running_loop().time()
    result = await pipeline.generate(request)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.5
    assert result.report.abnormal_microbes.items


@pytest.mark.asyncio
async def test_generation_timeout_before_parsing_sends_fixed_partial_callback():
    callback = CapturingCallback()

    async def no_session(user_id, session_id):
        return {}

    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=FailingEvidenceClient(),
        callback_client=callback,
        session_loader=no_session,
        narrative_client=None,
        generation_timeout_seconds=0.01,
    )

    async def hanging_generate(request):
        await asyncio.sleep(1)

    pipeline.generate = hanging_generate
    request = ReportRequest.model_validate(
        {
            'userId': 'u-generation-timeout',
            'sessionId': 's-generation-timeout',
            'name': '患者',
            'microbiomeData': {'taxa': []},
        }
    )
    await pipeline.execute_and_callback(request)
    assert len(callback.payloads) == 1
    payload = callback.payloads[0]
    assert payload.status == 'partial_success'
    assert payload.partial_reasons == ['REPORT_GENERATION_TIMEOUT']
    assert payload.report.abnormal_microbes.items == []
    assert payload.report.disease_risks.items == []


@pytest.mark.asyncio
async def test_generation_deadline_returns_partial_after_source_was_parsed():
    callback = CapturingCallback()

    class HangingEvidenceClient:
        async def search_detailed(self, query, *, supports, channels):
            await asyncio.sleep(1)
            return []

    async def no_session(user_id, session_id):
        return {}

    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=HangingEvidenceClient(),
        callback_client=callback,
        session_loader=no_session,
        narrative_client=None,
        generation_timeout_seconds=0.05,
    )
    request = ReportRequest.model_validate(
        {
            'userId': 'u-deadline-partial',
            'sessionId': 's-deadline-partial',
            'name': '患者',
            'microbiomeData': {
                'taxa': [
                    {
                        'category': 'pathogenic',
                        'latinName': 'Salmonella enterica',
                        'value': 0.01,
                    }
                ]
            },
        }
    )

    await pipeline.execute_and_callback(request)

    assert len(callback.payloads) == 1
    payload = callback.payloads[0]
    assert payload.status == 'partial_success'
    assert 'GENERATION_DEADLINE_REACHED' in payload.partial_reasons
    assert payload.report.abnormal_microbes.items[0].scientific_name == ('Salmonella enterica')
    assert payload.report.evidence_references == []


@pytest.mark.asyncio
async def test_pipeline_confirms_discovered_microbe_disease_with_exact_query():
    class EvidenceClient:
        async def search_detailed(self, query, *, supports, channels):
            guide = EviMedChannelResult('guide', 'not_requested', [])
            literature = EviMedChannelResult('literature', 'not_requested', [])
            if 'guide' in channels:
                guide = EviMedChannelResult('guide', 'success', [])
            if 'literature' in channels:
                records = []
                if query == 'Salmonella enterica':
                    records = [
                        EvidenceRecord(
                            evidence_key='salmonella-shared-paper',
                            category='literature',
                            title='Salmonella enterica infection and salmonellosis',
                            source='Journal',
                            summary=(
                                'Human patients with Salmonella enterica infection '
                                'may present with salmonellosis and diarrhea.'
                            ),
                            supports=supports,
                        )
                    ]
                elif 'salmonellosis' in query or '沙门菌病' in query:
                    records = [
                        EvidenceRecord(
                            evidence_key='salmonella-shared-paper',
                            category='literature',
                            title='Salmonella enterica infection and salmonellosis',
                            source='Journal',
                            summary=(
                                'A human clinical cohort confirmed the association '
                                'between Salmonella enterica infection and salmonellosis.'
                            ),
                            supports=supports,
                        )
                    ]
                literature = EviMedChannelResult('literature', 'success', records)
            return [guide, literature]

    class NarrativeClient:
        async def generate(self, payload):
            if payload['task'].startswith('逐条解读'):
                output = []
                for evidence in payload['evidence']:
                    if 'microbe_disease_association' not in evidence['intents']:
                        output.append(
                            {
                                'number': evidence['number'],
                                'claim': ('人体临床资料记录肠沙门菌感染与沙门菌病相关。'),
                                'targetAlignment': 'direct',
                                'taxonAlignment': 'species',
                                'sampleAlignment': 'stool',
                                'patientApplicability': 'contextual',
                                'clinicalRole': ('Salmonella enterica is described as a human infectious pathogen.'),
                                'conditions': [
                                    {
                                        'name': '沙门菌病',
                                        'searchTerm': 'salmonellosis',
                                        'sourcePhrase': 'salmonellosis',
                                        'relationType': 'established_pathogen',
                                    }
                                ],
                            }
                        )
                    else:
                        output.append(
                            {
                                'number': evidence['number'],
                                'claim': ('人体临床队列记录肠沙门菌感染与沙门菌病相关。'),
                                'targetAlignment': 'direct',
                                'taxonAlignment': 'species',
                                'sampleAlignment': 'stool',
                                'patientApplicability': 'direct',
                            }
                        )
                return {'evidenceInterpretations': output}
            if 'evidence' in payload and 'microbeWhitelist' not in payload:
                return {'evidenceInterpretations': []}
            if payload['task'] == 'supplement_common_examinations':
                return {'items': [
                    {'diseaseId': item['diseaseId'], 'examination': None}
                    for item in payload['diseases']
                ]}
            return {
                'executiveSummary': '本次粪便样本检出肠沙门菌，文献记录其与沙门菌病的临床关联。',
                'abnormalMicrobesSummary': '病原相关菌：本次检出肠沙门菌。',
                'microbeNarratives': [
                    {
                        'microbeId': payload['microbeWhitelist'][0]['microbeId'],
                        'clinicalRole': '文献将肠沙门菌描述为人类感染性病原菌。',
                        'conclusion': '本次粪便样本检出肠沙门菌相关序列。',
                        'patientInterpretation': '本次检出可作为沙门菌病的病原背景信息。',
                    }
                ],
                'healthLevel': {
                    'level': '一般',
                    'summary': '本次报告记录一项病原相关菌信号。',
                    'basis': ['肠沙门菌检出'],
                },
                'healthProfile': '本次以肠沙门菌检出信号为主要关注点。',
            }

    async def no_session(user_id, session_id):
        return {}

    pipeline = ReportPipeline(
        source_client=SourceClient(),
        evidence_client=EvidenceClient(),
        callback_client=NullCallback(),
        session_loader=no_session,
        narrative_client=NarrativeClient(),
    )
    result = await pipeline.generate(
        ReportRequest.model_validate(
            {
                'userId': 'u2',
                'sessionId': 's2',
                'name': '患者',
                'age': 28,
                'microbiomeData': {
                    'taxa': [
                        {
                            'category': 'pathogenic',
                            'latinName': 'Salmonella enterica',
                            'value': 0.01,
                        }
                    ]
                },
            }
        )
    )
    microbe = result.report.abnormal_microbes.items[0]
    assert [item.name for item in microbe.disease_and_risks] == ['沙门菌病']
    assert microbe.disease_and_risks[0].relation_type == 'established_pathogen'
    assert microbe.disease_and_risks[0].evidence_refs
    relation_audits = [
        audit for audit in result.audit.evidence_queries if audit.intent == 'microbe_disease_association'
    ]
    assert any(audit.relation_id for audit in relation_audits)
    assert any('沙门菌病' in audit.query_text for audit in relation_audits)
    assert any(audit.public_count > 0 for audit in relation_audits)


@pytest.mark.asyncio
async def test_failure_callback_uses_evaluate_v2_contract():
    captured = {}

    async def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    client = CallbackClient(
        url='https://callback.example/report',
        transport=httpx.MockTransport(handler),
    )
    payload = FailureCallback(
        user_id='u',
        session_id='s',
        error_code='SOURCE_PARSE_FAILED',
        message='未能解析有效的菌群检测数据',
    )
    await client.send(payload)
    assert set(captured) == {
        'schemaVersion', 'userId', 'sessionId', 'finalReportId', 'type', 'status',
        'generatedAt', 'reportDate', 'partialReasons', 'subject', 'sample', 'data', 'error',
        'groupId', 'fmtTypingReportUrl', 'package',
    }
    assert captured['schemaVersion'] == 'evaluate.v2.0'
    assert captured['userId'] == 'u'
    assert captured['sessionId'] == 's'
    assert captured['type'] == 'evaluate'
    assert captured['status'] == 'failed'
    assert captured['generatedAt'] is None
    assert date.fromisoformat(captured['reportDate']).isoformat() == captured['reportDate']
    assert captured['partialReasons'] == []
    assert captured['subject'] == {'name': None, 'gender': None, 'age': None}
    assert captured['sample']['sampleId'] is None
    assert captured['data'] == {}
    assert captured['error'] == {
        'errorCode': 'SOURCE_PARSE_FAILED',
        'message': payload.message,
    }


@pytest.mark.asyncio
async def test_success_callback_delivers_evaluate_v2_content():
    captured = {}

    async def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    client = CallbackClient(
        url='https://callback.example/report',
        transport=httpx.MockTransport(handler),
    )
    await client.send({
        'userId': 'u',
        'sessionId': 's',
        'status': 'success',
        'generatedAt': '2026-08-10T12:00:00Z',
        'report': {
            'subject': {'name': '患者', 'age': 28, 'sex': '男'},
            'sample': {'sampleId': '3718407'},
            'executiveSummary': 'legacy summary',
            'evaluationData': {
                'medicalAssessment': {
                    'findings': [{'name': '血糖', 'value': '5.1', 'unit': 'mmol/L', 'availability': 'available'}],
                },
            },
        },
        'audit': {'evidenceQueries': [{'queryText': 'internal audit'}]},
    })
    assert captured['schemaVersion'] == 'evaluate.v2.0'
    assert captured['type'] == 'evaluate'
    assert captured['status'] == 'success'
    assert captured['generatedAt'] == '2026-08-10T12:00:00Z'
    assert captured['reportDate'] == '2026-08-10'
    assert captured['subject'] == {'name': '患者', 'gender': '男', 'age': 28}
    assert captured['sample']['sampleId'] == '3718407'
    finding, = captured['data']['medicalAssessment']['findings']
    assert (finding['name'], finding['value'], finding['unit']) == ('血糖', '5.1', 'mmol/L')
    assert captured['error'] == {'errorCode': None, 'message': None}
    assert not {'report', 'audit', 'diagnosis_summary', 'diagnostic_basis'} & captured.keys()


@pytest.mark.asyncio
async def test_frontend_callback_sends_assessment_in_pdf_order_and_preserves_visible_report():
    from scripts.render_evaluation_report import render_evaluation_html

    view = {
        'version': 'assessment.v1',
        'title': '示例报告',
        'overview': {
            'dataTiming': {'clinicalDate': '2026-11-01'},
            'preface': '固定前言',
            'readingGuide': [{'name': '阅读指引', 'text': '请按章节阅读。'}],
            'summary': '示例总览',
            'evidenceRefs': [7],
        },
        'clinical': {
            'bodyGroups': [{'name': '体格', 'rows': [{'name': '示例指标', 'value': 0}]}],
            'symptoms': [{'name': '排便不适'}],
            'clinicalGroups': [],
            'digestiveContext': [{'name': '消化情况', 'rows': []}],
        },
        'microbiome': {},
        'clusters': [],
        'lifestyleFactors': [],
        'conclusion': {},
    }
    payload = {
        'schemaVersion': 'evaluate.v2.0', 'type': 'evaluate', 'status': 'partial_success',
        'userId': 'u', 'sessionId': 's', 'finalReportId': 'batch',
        'generatedAt': '2026-11-22T17:00:00Z', 'reportDate': '2026-11-23',
        'partialReasons': ['REPORT_LAYOUT_INCOMPLETE'],
        'subject': {'name': '示例用户', 'age': 42, 'gender': '女'},
        'sample': {'sampleId': 'demo', 'testItem': '报告详情'},
        'fmtTypingReportUrl': 'https://example.org/demo.pdf', 'package': ['PFMT'],
        'data': {
            'assessmentPresentation': view,
            'microbiomeAssessment': {'taxa': [{'name': '旧清单'}]},
            'appendices': {'referenceLiterature': [
                {'number': 7, 'title': '示例文献', 'authors': ['示例作者'], 'url': 'https://example.org/a'},
                {'number': 8, 'title': '旧清单引文'},
            ]},
        },
    }
    display = {}

    async def handler(request: httpx.Request):
        display.update(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    client = CallbackClient(url='https://callback.example/report', transport=httpx.MockTransport(handler))
    await client.send(payload)
    data = display['data']
    assert list(data) == ['version', 'introduction', 'chapters', 'appendices']
    assert data['version'] == 'assessment.v2'
    assert data['introduction'] == {
        'title': view['title'],
        'prefaceTitle': '前言与阅读指引',
        **{key: view['overview'][key] for key in ('dataTiming', 'preface', 'readingGuide')},
    }
    chapters = data['chapters']
    assert [(chapter['number'], chapter['code']) for chapter in chapters] == list(enumerate(
        ('overview', 'symptoms', 'clinical', 'microbiome', 'clusters', 'conclusion'), 1,
    ))
    assert list(chapters[0]['content']) == ['bodyGroups', 'summary', 'evidenceRefs']
    assert chapters[0]['content']['bodyGroups'] == view['clinical']['bodyGroups']
    assert chapters[1]['content']['symptoms'] == view['clinical']['symptoms']
    assert chapters[2]['content'] == {'clinicalGroups': []}
    assert chapters[3]['content']['digestiveContext'] == view['clinical']['digestiveContext']
    assert data['appendices']['referenceLiterature'] == [
        {'number': 7, 'title': '示例文献', 'year': None, 'doi': None, 'url': 'https://example.org/a'},
    ]
    assert display['subject'] == payload['subject']
    for field in ('type', 'status', 'finalReportId', 'generatedAt', 'reportDate', 'fmtTypingReportUrl', 'package'):
        assert display[field] == payload[field]
    assert display.keys() == {
        'schemaVersion', 'type', 'userId', 'sessionId', 'groupId', 'finalReportId',
        'fmtTypingReportUrl', 'package', 'status', 'generatedAt', 'reportDate',
        'partialReasons', 'subject', 'sample', 'error', 'data',
    }
    assert display['sample'] == {'sampleId': 'demo', 'sampleDate': None, 'reportDate': None, 'method': None}
    assert client.build_body(display) == display
    assert render_evaluation_html(display) == render_evaluation_html(payload)


def test_success_callback_retains_fixed_context_for_followup_generation():
    client = CallbackClient(url='https://callback.example/report')
    captured = client.build_followup_body(
        {
            'userId': 'u',
            'sessionId': 's',
            'status': 'success',
            'generatedAt': '2026-08-10T12:00:00Z',
            'partialReasons': [],
            'report': {
                'subject': {'name': '患者', 'age': 28, 'sex': '男'},
                'sample': {
                    'sampleId': '3718407',
                    'sampleType': '粪便',
                    'testItem': '肠道菌群检测',
                    'sampleDate': '2026_04_09',
                    'reportDate': '2026_04_10',
                    'sequencingPlatform': 'NovaSeq',
                },
                'executiveSummary': '本次检出肠沙门菌，并关注结直肠癌。',
                'abnormalMicrobes': {
                    'panelFindings': [{'name': 'internal'}],
                    'summary': '异常菌群概况。',
                    'items': [
                        {
                            'microbeId': 'taxon-1',
                            'name': '肠沙门菌',
                            'scientificName': 'Salmonella enterica',
                            'category': 'pathogenic_signal',
                            'sourceFinding': {
                                'status': '超标',
                                'value': '0.01',
                                'unit': '%',
                            },
                            'medicalInterpretation': '已校验的医学解读。',
                            'diseaseAndRisks': [
                                {
                                    'name': '沙门菌病',
                                    'description': '与沙门菌病相关。',
                                    'evidenceRefs': [1],
                                    'patientApplicability': 'direct',
                                    'sampleAlignment': 'stool',
                                }
                            ],
                            'interpretationEvidenceRefs': [1],
                            'associationEvidenceRefs': [1],
                            'evidenceRefs': [1],
                            'clinicalRole': 'internal',
                            'conclusion': 'internal',
                            'patientInterpretation': 'internal',
                            'clinicalConfirmation': 'internal',
                        }
                    ],
                },
                'diseaseRisks': {
                    'items': [
                        {
                            'diseaseId': 'disease-1',
                            'name': '结直肠癌',
                            'modelIndex': '1.2',
                            'medicalInterpretation': (
                                '本次检测中具核梭杆菌的变化与人体粪便研究观察到的'
                                '结直肠癌相关方向一致，因此值得关注排便表现和长期健康风险。'
                            ),
                            'patientSuggestion': (
                                '然而，关注结直肠癌可能涉及的排便习惯变化，可通过结肠镜检查进一步确认相关情况。'
                            ),
                            'sourceType': 'source_report',
                            'itemType': 'disease',
                            'sourceResult': 'internal',
                            'relatedMicrobes': ['具核梭杆菌'],
                            'associationEvidenceRefs': [1],
                            'confirmationEvidenceRefs': [2],
                            'evidenceRefs': [1],
                            'existingResults': [
                                {
                                    'itemName': '无痛肠镜检查',
                                    'result': '结直肠黏膜大致正常',
                                }
                            ],
                            'suggestedEvaluations': [
                                {
                                    'name': '结肠镜检查',
                                    'purpose': '确认',
                                    'rationale': '用于观察结直肠黏膜。',
                                    'evidenceRefs': [1],
                                }
                            ],
                        }
                    ]
                },
                'healthManagement': {
                    'enterotype': {
                        'name': '肠型I',
                        'source': 'derived',
                        'basis': 'internal',
                    },
                    'healthLevel': {
                        'level': '中度关注',
                        'summary': '个体化概括。',
                        'basis': ['internal'],
                    },
                    'healthProfile': '综合健康画像。',
                    'recommendations': [
                        {
                            'category': 'general',
                            'topic': '心血管与代谢健康',
                            'priority': '重点关注',
                            'content': '优先调整饮食结构，并保持规律运动。',
                            'evidenceRefs': [1],
                        }
                    ],
                },
                'evidenceReferences': [
                    {
                        'number': 1,
                        'provider': 'evimed_v2',
                        'category': 'literature',
                        'title': 'Title',
                        'source': 'Journal',
                        'year': 2025,
                        'summary': 'internal',
                        'url': None,
                        'doi': None,
                        'authors': ['A Researcher'],
                        'studyTypes': ['队列研究'],
                        'intents': ['microbe_disease_association'],
                        'interpretation': {
                            'claim': ('人体粪便研究观察到肠道菌群特征与结直肠癌相关。'),
                            'studyDesign': '队列研究',
                            'targetAlignment': 'direct',
                            'sampleAlignment': 'stool',
                            'patientApplicability': 'contextual',
                        },
                        'targets': ['internal'],
                    }
                ],
            },
            'audit': {'evidenceQueries': [{'queryText': 'internal audit'}]},
        }
    )
    assert 'report' not in captured
    assert 'audit' not in captured
    assert captured['type'] == 'evaluate'
    assert captured['status'] == 'success'
    assert captured['generated_at'] == '2026-08-10'
    assert captured['subject'] == {
        'name': '患者',
        'gender': '男',
        'age': '28岁',
        'report_date': '2026-04-10',
    }
    assert captured['sample'] == {
        'sample_id': '3718407',
        'sample_type': '粪便',
        'test_item': '肠道菌群检测',
        'sample_date': '2026-04-09',
        'report_date': '2026-04-10',
        'sequencing_platform': 'NovaSeq',
    }
    assert captured['diagnosis_summary']['conclusion'] == ('本次检出肠沙门菌，并关注结直肠癌。')
    # 健康管理关注等级不等同于疾病发生概率，不做风险等级推断。
    assert captured['diagnosis_summary']['risk'] is None
    assert captured['diagnosis_summary']['clinical_context']['肠型'] == '肠型I'
    assert captured['diagnosis_summary']['clinical_context']['综合健康画像'] == ('综合健康画像。')
    assert '肠沙门菌' in captured['diagnosis_summary']['clinical_context']['异常菌群概况']
    assert captured['diagnosis_summary']['clinical_context']['菌群关联判定说明'] == (
        '菌群医学解读用于说明本次粪便样本中的健康关注线索。'
        '检出或丰度变化不等同于临床感染或疾病诊断；'
        '是否存在相应问题，需由医生结合症状、体征和针对性检查判断。'
    )
    findings = captured['diagnostic_basis']['abnormal_findings']
    assert [item['finding_type'] for item in findings] == ['microbe', 'disease']
    assert findings[0]['problem'] == '肠沙门菌 (Salmonella enterica)'
    assert findings[0]['indicators'] == [{'name': None, 'value': '0.01%', 'status': '超标'}]
    association = findings[0]['related_diseases_or_adverse_effects'][0]
    assert association['name'] == '沙门菌病'
    assert association['relation_type'] == 'epidemiologic_association'
    assert association['evidence_refs'] == [1]
    assert association['description'] is None
    assert '沙门菌病' in findings[0]['biological_mechanism']
    assert '不能单独用于判断' not in findings[0]['biological_mechanism']
    assert findings[0]['interpretation_evidence_refs'] == [1]
    assert findings[0]['recommendation_evidence_refs'] == []
    assert findings[1]['related_diseases_or_adverse_effects'] == []
    assert findings[0]['tips'] == {
        'special_reminder': None,
        'practical_goal': None,
    }
    assert findings[1]['indicators'] == []
    assert findings[1]['recommendations'] == []
    assert findings[1]['tips']['special_reminder'] == ('关注结直肠癌可能涉及的排便习惯变化。')
    assert findings[1]['interpretation_evidence_refs'] == [1]
    assert findings[1]['recommendation_evidence_refs'] == []
    recommendation = captured['diagnostic_advice']['health_recommendations'][0]
    assert recommendation['topic'] == '心血管与代谢健康'
    assert recommendation['priority'] == '重点关注'
    assert recommendation['content'] == '优先调整饮食结构，并保持规律运动。'
    assert len(captured['evidence']) == 1
    evidence = captured['evidence'][0]
    assert set(evidence) == {
        'number',
        'evidence_type',
        'topic',
        'title',
        'source',
        'publication_date',
        'url',
        'evidence_level',
        'provider',
        'summary',
        'conclusion',
        'key_evidence',
        'patient_related_facts',
        'evidence_grade',
        'study_type',
        'authors',
    }
    assert evidence['number'] == 1
    assert evidence['url'] is None
    assert evidence['provider'] == 'evimed_v2'
    assert evidence['authors'] == ['A Researcher']
    assert evidence['study_type'] == '队列研究'
    assert evidence['conclusion'] == (
        '肠沙门菌 (Salmonella enterica)：超标，0.01%'
        '提示需关注沙门菌病、结直肠癌；'
        '建议关注结直肠癌可能涉及的排便习惯变化。'
    )
    assert '人体粪便研究观察到' not in evidence['conclusion']
    assert evidence['key_evidence'] == '研究类型：队列研究'
    assert any('肠沙门菌' in fact for fact in evidence['patient_related_facts'])
    assert evidence['evidence_grade'] == '弱'
    assert findings[0]['evidence_refs'] == [1]
    assert captured['diagnosis_summary']['evidence_refs'] == [1]
    assert captured['error'] == {'error_code': None, 'message': None}


class _FakePipeline:
    def __init__(self):
        self.requests = []

    async def execute_and_callback(self, request, *, final_report_id=None):
        self.requests.append(request)


class _TaskManager:
    def __init__(self):
        self.tasks = []

    def create_task(self, coroutine, *, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task


class _RegistrationTrackingTaskManager(_TaskManager):
    def __init__(self, response_finished):
        super().__init__()
        self.response_finished = response_finished
        self.registered_before_response_finished = None

    def create_task(self, coroutine, *, name):
        self.registered_before_response_finished = not self.response_finished.is_set()
        return super().create_task(coroutine, name=name)


class TestReportHandler(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.pipeline = _FakePipeline()
        self.task_manager = _TaskManager()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/report',
                    ReportGenerateHandler,
                    {
                        'pipeline': self.pipeline,
                        'task_supervisor': self.task_manager,
                        'task_coordinator': ReportTaskCoordinator(),
                    },
                )
            ],
        )

    @tornado.testing.gen_test
    async def test_immediate_processing_response_and_full_payload(self):
        response = await self.http_client.fetch(
            self.get_url('/api/v1/report'),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'u1',
                    'sessionId': 's1',
                    'name': '患者',
                    'fileList': [],
                    'testData': [],
                    'hospitalData': [],
                }
            ),
        )
        body = json.loads(response.body)
        data = dict(body['data'])
        # 批次号每次生成都不同，只固定其形状与前缀。
        final_report_id = data.pop('finalReportId')
        assert isinstance(final_report_id, str)
        assert final_report_id.startswith('rpt_')
        assert data == {
            'userId': 'u1',
            'sessionId': 's1',
            'status': 'processing',
        }
        await asyncio.gather(*self.task_manager.tasks)
        assert self.pipeline.requests[0].name == '患者'

    @tornado.testing.gen_test
    async def test_accepts_image_file_type(self):
        response = await self.http_client.fetch(
            self.get_url("/api/v1/report"),
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "userId": "u-image",
                    "sessionId": "s-image",
                    "fileList": [
                        {
                            "url": "https://example.com/report-page.jpg",
                            "fileType": "image",
                        }
                    ],
                }
            ),
        )

        assert response.code == 200
        assert json.loads(response.body)["data"]["status"] == "processing"
        await asyncio.gather(*self.task_manager.tasks)
        assert self.pipeline.requests[0].file_list[0].file_type == "image"

    @tornado.testing.gen_test
    async def test_v2_routes_are_not_registered(self):
        response = await self.http_client.fetch(
            self.get_url('/api/v2/reports'),
            raise_error=False,
        )
        assert response.code == 404


class TestReportHandlerDraining(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.pipeline = _FakePipeline()
        self.task_supervisor = TaskSupervisor()
        self.task_supervisor.stop_accepting()
        return tornado.web.Application(
            [
                (
                    r'/api/v1/report',
                    ReportGenerateHandler,
                    {
                        'pipeline': self.pipeline,
                        'task_supervisor': self.task_supervisor,
                        'task_coordinator': ReportTaskCoordinator(),
                    },
                )
            ],
        )

    @tornado.testing.gen_test
    async def test_rejects_before_acknowledging_unregistered_task(self):
        response = await self.http_client.fetch(
            self.get_url('/api/v1/report'),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'u-draining',
                    'sessionId': 's-draining',
                    'name': '患者',
                    'fileList': [],
                    'testData': [],
                    'hospitalData': [],
                }
            ),
            raise_error=False,
        )

        assert response.code == 503
        assert self.task_supervisor.active_task_count == 0
        assert self.pipeline.requests == []


class _BlockingStartPipeline:
    def __init__(self, response_finished):
        self.response_finished = response_finished
        self.started_after_response = None

    async def execute_and_callback(self, request, *, final_report_id=None):
        self.started_after_response = self.response_finished.is_set()


class _FinishTrackingReportHandler(ReportGenerateHandler):
    async def finish(self, chunk=None):
        result = super().finish(chunk)
        if result is not None:
            await result
        self.application.settings['response_finished'].set()


class TestReportHandlerImmediateFlush(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.response_finished = asyncio.Event()
        self.task_manager = _RegistrationTrackingTaskManager(
            self.response_finished,
        )
        self.pipeline = _BlockingStartPipeline(self.response_finished)
        return tornado.web.Application(
            [
                (
                    r'/api/v1/report',
                    _FinishTrackingReportHandler,
                    {
                        'pipeline': self.pipeline,
                        'task_supervisor': self.task_manager,
                        'task_coordinator': ReportTaskCoordinator(),
                    },
                )
            ],
            response_finished=self.response_finished,
        )

    @tornado.testing.gen_test
    async def test_background_work_starts_after_processing_response_finishes(self):
        response = await self.http_client.fetch(
            self.get_url('/api/v1/report'),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps(
                {
                    'userId': 'u-immediate',
                    'sessionId': 's-immediate',
                    'name': '患者',
                    'fileList': [],
                    'testData': [],
                    'hospitalData': [],
                }
            ),
        )
        assert response.code == 200
        assert json.loads(response.body)['data']['status'] == 'processing'
        await asyncio.gather(*self.task_manager.tasks)
        assert self.task_manager.registered_before_response_finished is True
        assert self.pipeline.started_after_response is True
