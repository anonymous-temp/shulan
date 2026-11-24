"""Questionnaire generation contracts with deterministic reports and an offline planner."""

from copy import deepcopy

import pytest

from modules.questionnaire.generation import QuestionnaireGenerator
from modules.questionnaire.generation import build_evidence
from modules.questionnaire.generation import build_questionnaire
from modules.questionnaire.generation import reported_gender

REPORT = {
    'subject': {'gender': '男'},
    'reportTime': '2026-10-01',
    'observations': [{'itemName': '空腹血糖', 'resultValue': '6.4', 'unit': 'mmol/L', 'flag': 'high'}],
}


def plan():
    return {
        'risks': [
            {
                'priority': 'routine',
                'assessment': '血糖变化值得跟进，需结合复查和个人情况判断。',
                'evidenceIds': ['r1_1'],
            }
        ],
        'tests': [{'name': '复查空腹血糖', 'reason': '了解本次结果之后的变化', 'evidenceIds': ['r1_1']}],
        'questions': [
            {
                'title': '这次体检以后，您复查过血糖吗？',
                'description': '报告记录空腹血糖6.4 mmol/L。',
                'choices': ['已复查', '尚未复查', '不清楚'],
                'informationFacet': 'followup_status',
                'evidenceIds': ['r1_1'],
            }
        ],
        'interview': [
            {
                'question': '最近一次复查是哪天，结果是多少？',
                'reason': '核对最新结果',
                'informationFacet': 'followup_result',
                'evidenceIds': ['r1_1'],
            }
        ],
    }


def elements(result):
    return [item for page in result['questionnaire']['pages'] for item in page['elements']]


class Documents:
    def __init__(self, reports=None):
        self.reports = (
            reports
            if reports is not None
            else [{'fileIndex': 0, 'status': 'complete', 'interpretation': deepcopy(REPORT)}]
        )

    async def stage(self, **kwargs):
        return {'reports': deepcopy(self.reports), 'artifacts': []}


class Planner:
    def __init__(self, result=None, fails=False):
        self.result, self.fails, self.calls = result or plan(), fails, []

    async def plan(self, **kwargs):
        self.calls.append(kwargs)
        if self.fails:
            raise RuntimeError('offline failure')
        return deepcopy(self.result)


@pytest.mark.asyncio
async def test_complete_questionnaire_contains_report_bridge_and_complementary_guidance():
    planner = Planner()
    generated = await QuestionnaireGenerator(documents=Documents(), planner=planner).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'url': 'https://example.org/report.pdf'}],
    )
    questions = elements(generated)
    assert len(questions) == 11
    assert [item['name'] for item in questions[:3]] == ['health_goals', 'medical_history', 'family_history']
    assert all('required' not in item and 'isRequired' not in item and 'evidenceRefs' not in item for item in questions)
    assert '6.4' in questions[0]['description']
    assert '简要风险分析' in questions[0]['description']
    assert '复查空腹血糖' in generated['guidance']['clientCheckupMessage']
    assert generated['guidance']['managerInterview'][0]['informationFacet'] == 'followup_result'
    assert generated['provenance']['questionSources']['report_follow_up_1']['evidenceRefs'] == ['r1_1']
    assert planner.calls[0]['gender']['value'] == 'male'


@pytest.mark.parametrize('gender', ['male', 'unknown'])
def test_gender_gate_applies_to_questions_risks_tests_and_interview(gender):
    draft = plan()
    draft['questions'][0]['title'] = '您的月经周期规律吗？'
    draft['risks'][0]['assessment'] = '需结合月经周期判断'
    draft['tests'][0]['name'] = '妇科检查'
    draft['interview'][0]['question'] = '月经具体哪一天开始？'
    result = build_questionnaire(build_evidence([REPORT], gender), draft, gender)
    assert len(elements(result)) == 10
    assert result['guidance']['riskAssessment'] == []
    assert result['guidance']['suggestedTests'] == []
    assert result['guidance']['managerInterview'] == []


def test_female_explicit_source_allows_menstrual_question_and_conflicts_stay_unknown():
    female = {**REPORT, 'subject': {'gender': '女'}}
    assert reported_gender([female])['value'] == 'female'
    assert reported_gender([female, REPORT]) == {
        'value': 'unknown',
        'status': 'conflicting',
        'sources': [{'reportIndex': 0, 'pointer': '/subject/gender'}, {'reportIndex': 1, 'pointer': '/subject/gender'}],
    }
    assert reported_gender([{'title': '王女士妇科检查'}])['value'] == 'unknown'
    draft = plan()
    draft['questions'][0]['title'] = '您的月经周期规律吗？'
    assert len(elements(build_questionnaire(build_evidence([female]), draft, 'female'))) == 11


def test_deduplicates_facets_across_customer_and_manager_without_generic_checklist():
    draft = plan()
    draft['questions'].append({**draft['questions'][0], 'title': '体检后是否又测过血糖？'})
    draft['questions'].append({**draft['questions'][0], 'informationFacet': 'medical_history'})
    draft['interview'].append({**draft['interview'][0], 'informationFacet': 'followup_status'})
    draft['interview'].append({**draft['interview'][0], 'informationFacet': 'family_history'})
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    assert len(elements(result)) == 11
    assert len(result['guidance']['managerInterview']) == 1


@pytest.mark.asyncio
async def test_all_reports_failed_is_ten_question_base_and_never_calls_planner():
    planner = Planner()
    result = await QuestionnaireGenerator(
        documents=Documents([{'fileIndex': 0, 'status': 'unavailable'}]),
        planner=planner,
    ).generate(user_id='u', questionnaire_id='q', file_list=[{'documentId': 'd'}])
    assert result['mode'] == 'basic'
    assert result['processing']['reason'] == 'reports_unavailable'
    assert len(elements(result)) == 10
    assert result['guidance']['riskAssessment'] == []
    assert result['guidance']['suggestedTests'] == []
    assert result['guidance']['managerInterview'] == []
    assert planner.calls == []


@pytest.mark.asyncio
async def test_planner_failure_preserves_report_but_does_not_invent_risk_or_followups():
    result = await QuestionnaireGenerator(documents=Documents(), planner=Planner(fails=True)).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'url': 'https://example.org/r.pdf'}],
    )
    assert result['processing']['reason'] == 'planner_unavailable'
    assert len(elements(result)) == 10
    assert result['reports'][0]['interpretation'] == REPORT
    assert result['guidance']['riskAssessment'] == []


def test_invalid_evidence_is_rejected_and_original_file_index_is_preserved():
    report = {**REPORT, '_questionnaireReportIndex': 2}
    evidence = build_evidence([report])
    assert evidence[0]['id'] == 'r3_1'
    assert evidence[0]['reportIndex'] == 2
    result = build_questionnaire(evidence, plan(), 'male')
    assert len(elements(result)) == 10
    assert result['guidance']['riskAssessment'] == []


def test_report_questions_are_capped_and_do_not_repeat_lifestyle_facets():
    draft = plan()
    facets = [
        'symptom_presence',
        'symptom_timing',
        'symptom_severity',
        'medication_use',
        'exposure',
        'test_context',
        'sleep_duration',
    ]
    draft['questions'] = [
        {**draft['questions'][0], 'title': f'具体问题{index}', 'informationFacet': facet}
        for index, facet in enumerate(facets)
    ]
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    assert len(elements(result)) == 15
    assert len(result['questionSources']) == 15


def test_interview_relabels_duplicate_existence_question_and_conditions_real_details():
    draft = plan()
    draft['interview'] = [
        {
            'question': '您是否复查过血糖？',
            'reason': '了解复查情况',
            'informationFacet': 'followup_result',
            'evidenceIds': ['r1_1'],
        },
        {
            'question': '哪天做的，结果是多少？',
            'reason': '了解复查结果',
            'informationFacet': 'followup_result',
            'evidenceIds': ['r1_1'],
        },
        {
            'question': '具体药品叫什么，何时开始用的？',
            'reason': '核对用药时间',
            'informationFacet': 'medication_detail',
            'evidenceIds': ['r1_1'],
        },
    ]
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    rows = result['guidance']['managerInterview']
    assert len(rows) == 2
    assert rows[0]['question'].startswith('如果您已经做过相关复查，')
    assert rows[1]['question'].startswith('如果您使用过相关药物，')


def test_generated_base_and_report_forms_pass_production_schema():
    from modules.questionnaire.schemas import validate_questionnaire

    for draft in ({}, plan()):
        result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
        assert validate_questionnaire(result['questionnaire']) == result['questionnaire']


@pytest.mark.asyncio
async def test_raw_gender_evidence_survives_normalized_analysis_projection():
    reports = [
        {
            'fileIndex': 1,
            'status': 'complete',
            'interpretation': {'observations': REPORT['observations']},
            'genderEvidence': [
                {'value': 'male', 'pointer': '/docmind_result/text', 'start': 0, 'end': 4, 'quote': '性别：男'}
            ],
        }
    ]
    planner = Planner()
    result = await QuestionnaireGenerator(documents=Documents(reports), planner=planner).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'url': 'https://example.org/r.pdf'}],
    )
    assert result['provenance']['gender']['value'] == 'male'
    assert result['provenance']['gender']['sources'][0]['reportIndex'] == 1


def test_evidence_budget_prioritizes_abnormal_rows_even_when_they_occur_late():
    report = {
        'observations': [{'itemName': f'普通项目{index}', 'resultValue': '1', 'flag': 'normal'} for index in range(20)]
        + [{'itemName': '后部异常项目', 'resultValue': '7', 'flag': 'high'}]
    }
    evidence = build_evidence([report])
    assert len(evidence) == 16
    assert evidence[0]['name'] == '后部异常项目'
    assert evidence[0]['pointer'] == '/observations/20'


@pytest.mark.parametrize(
    'bad_label',
    [
        '某种疾病',
        '其他疾病',
        '心血管病（高血压/冠心病）',
        '类别（甲/乙）',
        '高血压/糖尿病',
        '不愿回答',
        '拒绝回答',
        '跳过',
        '补充说明',
        '点击填写',
        '自行输入',
        '其他请填写',
        '已检查（请补充）',
        '这是一个超过二十四个字符的冗长选项标签应该直接被判无效不能截断发布',
    ],
)
def test_invalid_followup_choice_removes_only_that_question(bad_label):
    draft = plan()
    good = deepcopy(draft['questions'][0])
    bad = {
        **good,
        'title': '最近是否使用相关药物？',
        'informationFacet': 'medication_use',
        'choices': [bad_label, '没有', '不清楚'],
    }
    draft['questions'] = [bad, good]
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    followups = [item for item in elements(result) if item['name'].startswith('report_follow_up_')]
    assert len(followups) == 1
    assert followups[0]['title'] == good['title']
    assert result['guidance']['riskAssessment']
    assert result['guidance']['managerInterview']


@pytest.mark.parametrize(
    'choices',
    [
        ['每周1～2次', '每周3～4次', '没有', '不清楚'],
        ['5～7 mmol/L', '8～10 mmol/L', '没有', '不清楚'],
        ['每天1/2杯', '每天1杯', '没有', '不清楚'],
    ],
)
def test_valid_quantity_ranges_units_and_negative_unknown_labels_are_kept(choices):
    draft = plan()
    draft['questions'][0]['choices'] = choices
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    followup = next(item for item in elements(result) if item['name'] == 'report_follow_up_1')
    assert [item['value'] for item in followup['choices']] == choices


def test_microbiome_parent_and_mirrored_mixed_sections_cannot_supply_planning_evidence():
    microbial = {'itemName': '双歧杆菌', 'resultValue': '0.2', 'unit': '%', 'flag': 'low'}
    clinical = REPORT['observations'][0]
    microbiome = {'reportType': 'microbiome', 'observations': [microbial]}
    assert build_evidence([microbiome]) == []
    mixed = {
        'reportType': 'checkup',
        'observations': [{**microbial, 'sourceSection': 0}, {**clinical, 'sourceSection': 1}],
        'sections': [microbiome, {'reportType': 'lab', 'observations': [clinical]}],
        'expansion': {'findings': '不能使用混合来源根摘要中的菌群信号'},
    }
    original = deepcopy(mixed)
    evidence = build_evidence([mixed])
    assert [item['name'] for item in evidence] == ['空腹血糖']
    assert mixed == original
    del mixed['observations'][0]['sourceSection']
    assert [item['name'] for item in build_evidence([mixed])] == ['空腹血糖']


@pytest.mark.asyncio
async def test_microbiome_only_upload_falls_back_without_planner_or_risk():
    microbiome = {
        'reportType': 'microbiome',
        'observations': [
            {'itemName': '菌群多样性', 'resultValue': '1.2', 'flag': 'low'},
        ],
    }
    planner = Planner()
    documents = Documents([{'fileIndex': 0, 'status': 'complete', 'interpretation': microbiome}])
    result = await QuestionnaireGenerator(documents=documents, planner=planner).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'documentId': 'microbiome'}],
    )
    assert result['mode'] == 'basic'
    assert len(elements(result)) == 10
    assert result['guidance']['riskAssessment'] == []
    assert result['guidance']['suggestedTests'] == []
    assert planner.calls == []
    assert result['reports'][0]['interpretation'] == microbiome


@pytest.mark.asyncio
async def test_planner_receives_only_checkup_evidence_with_original_report_indices():
    micro = {
        'reportType': 'microbiome',
        'observations': [
            {'itemName': '菌群功能', 'resultValue': '偏低', 'flag': 'low'},
        ],
    }
    planner = Planner()
    documents = Documents(
        [
            {'fileIndex': 0, 'status': 'complete', 'interpretation': micro},
            {'fileIndex': 1, 'status': 'complete', 'interpretation': {**REPORT, 'reportType': 'lab'}},
        ]
    )
    await QuestionnaireGenerator(documents=documents, planner=planner).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'documentId': 'microbiome'}, {'documentId': 'checkup'}],
    )
    assert [item['name'] for item in planner.calls[0]['evidence']] == ['空腹血糖']
    assert planner.calls[0]['evidence'][0]['id'] == 'r2_1'


def test_evidence_linked_bridges_personalize_selected_topics_without_changing_fixed_scope():
    draft = plan()
    draft['bridges'] = [
        {
            'name': 'medical_history',
            'description': '报告记录了血糖结果，仍需结合完整的个人疾病史理解。',
            'evidenceIds': ['r1_1'],
        },
        {'name': 'diet_structure', 'description': '结合报告中的血糖记录，了解日常食物搭配。', 'evidenceIds': ['r1_1']},
        {'name': 'family_history', 'description': '不应把家族史缩为血糖问题。', 'evidenceIds': ['r1_1']},
        {'name': 'health_goals', 'description': '不应覆盖整体总结。', 'evidenceIds': ['r1_1']},
        {'name': 'water_intake', 'description': '不应加入未选中的生活题。', 'evidenceIds': ['r1_1']},
    ]
    baseline = build_questionnaire(build_evidence([REPORT]), plan(), 'male')
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    before = {item['name']: item for item in elements(baseline)}
    questions = {item['name']: item for item in elements(result)}
    assert questions['medical_history']['description'] == draft['bridges'][0]['description']
    assert questions['diet_structure']['description'] == draft['bridges'][1]['description']
    assert questions['medical_history']['title'] == before['medical_history']['title']
    assert questions['family_history'] == before['family_history']
    assert questions['health_goals'] == before['health_goals']
    assert 'water_intake' not in questions
    assert result['questionSources']['medical_history']['evidenceRefs'] == ['r1_1']
    assert result['questionSources']['diet_structure']['origin'] == 'report_bridge'


@pytest.mark.parametrize(
    'bridge',
    [
        {'name': 'medical_history', 'description': '缺少来源的承接', 'evidenceIds': ['missing']},
        {'name': 'diet_structure', 'description': '结合月经周期了解饮食', 'evidenceIds': ['r1_1']},
        {'name': 'diet_structure', 'description': '可以点击选项后输入具体内容', 'evidenceIds': ['r1_1']},
    ],
)
def test_invalid_bridge_does_not_replace_safe_template_or_remove_question(bridge):
    draft = plan()
    draft['bridges'] = [bridge]
    before = build_questionnaire(build_evidence([REPORT]), plan(), 'male')
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    assert result['questionnaire'] == before['questionnaire']


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['no_reports', 'reports_unavailable', 'planner_unavailable'])
async def test_fallback_customer_copy_invites_answers_without_exposing_processing_status(scenario):
    reports = (
        []
        if scenario == 'no_reports'
        else ([{'fileIndex': 0, 'status': 'unavailable'}] if scenario == 'reports_unavailable' else None)
    )
    result = await QuestionnaireGenerator(
        documents=Documents(reports),
        planner=Planner(fails=scenario == 'planner_unavailable'),
    ).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[] if scenario == 'no_reports' else [{'url': 'https://example.org/r.pdf'}],
    )
    guidance = result['guidance']
    opening = elements(result)[0]['description']
    assert result['mode'] == 'basic' and result['processing']['reason'] == scenario
    assert guidance['riskNote'] == '' and guidance['riskAssessment'] == []
    assert '简要风险分析' not in opening
    assert '填写' in guidance['clientCheckupMessage']
    customer_copy = opening + guidance['reportSummary'] + guidance['clientCheckupMessage']
    assert all(word not in customer_copy for word in ('未获得', '资料不足', '解析失败', '生成报告补充题', '降级'))
    if scenario == 'planner_unavailable':
        assert '空腹血糖 6.4 mmol/L' in opening
        assert '仍需结合检查时间和个人情况理解' in opening
    else:
        assert '基础健康问卷' in opening and '检查记录' not in opening


def test_report_summary_without_risks_keeps_source_context_and_omits_risk_section():
    draft = plan()
    draft['risks'] = []
    result = build_questionnaire(build_evidence([REPORT]), draft, 'male')
    opening = elements(result)[0]['description']
    assert '空腹血糖 6.4 mmol/L' in opening
    assert '仍需结合检查时间和个人情况理解' in opening
    assert '简要风险分析' not in opening
    assert result['guidance']['riskNote'] == ''
    assert result['guidance']['suggestedTests']
