"""Six-chapter writing duties, 1.3 and chapter 6 projection, and reader wording.

All records are synthetic and unrelated to any real examinee.
"""

from types import SimpleNamespace

import pytest

from modules.report import assessment_presentation as ap
from modules.report.evaluation_presentation import assessment_reader_text
from modules.report.evaluation_presentation import present_assessment_for_reader
from modules.report.evaluation_report_writer import _PLANNED_REVIEW_GUIDE
from modules.report.evaluation_report_writer import _PLANNED_WRITING_GUIDE
from modules.report.evaluation_report_writer import ASSESSMENT_BANNED_TERMS
from modules.report.evaluation_report_writer import REVIEW_GUIDE
from modules.report.evaluation_report_writer import WRITING_GUIDE

# Disease-specific findings; a generic duty or rule must not name them.
_CASE_TERMS = (
    '隐血',
    '甲状腺',
    '胆囊',
    'C肽',
    'OGTT',
    '糖耐量',
    '牙石',
    '痔疮',
    '便秘',
    '孕酮',
    '脂肪肝',
    '父亲',
    '屈光',
    '颈椎',
)
_DUTY_KEYS = (
    'overviewSummary',
    'conclusionSummary',
    'bodySummary',
    'graphSummary',
    'priority.urgent',
    'priority.routine',
    'priority.gaps',
    'symptomSummary',
    'clinicalLead',
    'specialtySummary',
    'trendSummary',
    'baselineSummary',
    'microbiomeLead',
    'sampleSummary',
    'fermentationSummary',
    'structureSummary',
    'riskSummary',
    'digestive.oralGastric',
    'digestive.hepatobiliaryPancreatic',
    'digestive.lowerDigestive',
    'microbiomeRow',
    'structure.overallBalance',
    'structure.diversity',
    'structure.gmwi2',
    'structure.gutAge',
    'structure.colonizationResistance',
    'structure.composition',
    'structure.barrier',
    'organisms.otherOrganisms',
    'organisms.genes',
    'clusterSummary',
    'lifestyleSummary',
    'layer.clinical',
    'layer.core',
    'layer.maintenance',
    'management.probiotic',
    'management.fiber',
    'management.nutrient',
    'management.lifestyle',
    'body.weightWaist',
    'system.cardiometabolic',
    'clinical.cardiometabolic',
    'clinicalRow.x.y',
    'specialtyRow.x',
    'digestive.other',
    'structure.other',
    'organisms.beneficial',
    'function.vitamin',
    'symptom.S1',
    'symptomEvidence.S1.clinical',
    'symptomEvidence.S1.microbiome',
    'synthesis.c1',
    'synthesisAction.c1',
    'lifestyle.diet',
    'recognition.r1',
    'unknown.cell',
)


def _text(value, refs=()):
    return SimpleNamespace(text=value, evidence_refs=list(refs))


def _source(fid):
    return {'sourceCode': 'A', 'recordId': fid, 'observedAt': '2026-01-05'}


def _views():
    clinical = {
        'bodyMetrics': [
            {'name': '体重指数', 'value': '27.0', 'unit': 'kg/m²', 'status': '超重', 'sources': [_source('B1')]},
            {'name': '腰围', 'value': '95', 'unit': 'cm', 'status': '偏高', 'sources': [_source('B2')]},
            {'name': '身高', 'value': '170', 'unit': 'cm', 'status': None, 'sources': [_source('B3')]},
        ],
        'bodyGroups': [],
        'clinicalGroups': [
            {
                'code': 'organMetabolism',
                'name': '肝胆胰消化与肾脏代谢',
                'templateRows': [],
                'rows': [
                    {
                        'name': '筛查甲',
                        'value': '阳性',
                        'unit': None,
                        'referenceRange': '阴性',
                        'status': '阳性',
                        'observedAt': '2026-01-05',
                        'sources': [_source('F1')],
                    },
                    {
                        'name': '指标乙',
                        'value': '3.1',
                        'unit': 'mmol/L',
                        'referenceRange': '<2.0',
                        'status': '偏高',
                        'observedAt': '2026-01-05',
                        'sources': [_source('F2')],
                    },
                    {
                        'name': '指标丙',
                        'value': '1.0',
                        'unit': 'mmol/L',
                        'referenceRange': '0.5-1.5',
                        'status': '正常',
                        'observedAt': '2026-01-05',
                        'sources': [_source('F2')],
                    },
                ],
            }
        ],
        'specialtyFindings': [
            {
                'code': 'imaging',
                'name': '影像丁',
                'value': '结节，分级3类',
                'status': '有异常所见',
                'sources': [_source('F3')],
            },
        ],
        'digestiveContext': [],
        'trends': [],
        'normalBaselines': [
            {
                'code': 'baseline',
                'rows': [
                    {'name': '基线戊', 'value': '1.2', 'status': '正常', 'sources': [_source('F4')]},
                ],
            }
        ],
    }
    microbiome = {'structure': [], 'structureGroups': [], 'organismGroups': [], 'functionalGroups': []}
    facts = {fid: {'kind': 'clinical', 'name': fid, 'value': ''} for fid in ('B1', 'B2', 'B3', 'F1', 'F2', 'F3', 'F4')}
    return clinical, microbiome, facts


def _plan(**extra):
    plan = {
        'status': 'reviewed',
        'clusters': [
            {
                'code': 'c_urgent',
                'name': '某阳性筛查结果的复查与查因',
                'priority': 'P0',
                'factIds': ['F1'],
                'clinicalFactIds': ['F1'],
                'problemCodes': ['p1'],
            },
            {
                'code': 'c_core',
                'name': '代谢指标偏高的协同管理',
                'priority': 'P1',
                'factIds': ['F2'],
                'clinicalFactIds': ['F2'],
                'problemCodes': ['p2'],
            },
        ],
        'symptoms': [],
        'lifestyleFactors': [],
        'recognitions': [],
        'cellScopes': [],
    }
    plan.update(extra)
    return plan


_ITEMS = {
    'priorityItems': [
        {
            'code': 'rf1',
            'ruleId': 'rule.screen',
            'title': '筛查甲阳性',
            'priority': 'P0',
            'timeframe': {'text': '2 周内', 'days': 14},
            'department': '专科甲',
            'checks': ['复查筛查甲', '专科评估'],
            'factIds': ['F1'],
        }
    ],
    'routineFollowups': [
        {
            'code': 'r1',
            'title': '影像丁所见结节',
            'interval': {'text': '每 12 个月', 'days': 365},
            'department': '专科乙',
            'factIds': ['F3'],
        }
    ],
    'testGaps': [
        {
            'code': 'g1',
            'test': '补查项目己',
            'reason': '指标乙偏高，需明确来源',
            'ruleId': 'gap.rule',
            'triggeredBy': ['F2'],
        }
    ],
}


def _jobs(plan):
    clinical, microbiome, facts = _views()
    return {job[1]: job for job in ap._planned_editorial_jobs(clinical, microbiome, facts, plan)}


def _clusters():
    return [
        {
            'id': 'C1',
            'name': '某阳性筛查结果的复查与查因',
            'priority': 'P0',
            '_planCode': 'c_urgent',
            '_codes': ['p1'],
            'action': None,
            'clinicalSignals': [
                {'name': '筛查甲', 'value': '阳性', 'status': '阳性', 'sources': [_source('F1')]},
                {
                    'name': '原报告医嘱 · 筛查甲',
                    'value': '建议复查筛查甲，专科甲就诊。',
                    'status': '原报告医嘱',
                    'sources': [_source('F1')],
                },
                {
                    'name': '原报告医嘱 · 筛查甲',
                    'value': '若复查后仍阳性，请进一步检查原因',
                    'status': '原报告医嘱',
                    'sources': [_source('F1')],
                },
                {'name': '留样时情况', 'value': '是', 'status': '本人自述', 'sources': [_source('S1')]},
            ],
        },
        {
            'id': 'C2',
            'name': '代谢指标偏高的协同管理',
            'priority': 'P1',
            '_planCode': 'c_core',
            '_codes': ['p2'],
            'action': None,
            'clinicalSignals': [
                {
                    'name': '原报告医嘱 · 指标乙',
                    'value': '定期复查指标乙。',
                    'status': '原报告医嘱',
                    'sources': [_source('F2')],
                },
            ],
        },
    ]


# --- writing duties ----------------------------------------------------------


@pytest.mark.parametrize('key', _DUTY_KEYS)
def test_duties_are_generic_and_in_formal_style(key):
    duty = ap.assessment_template_duty(key)
    assert duty
    assert not [term for term in _CASE_TERMS if term in duty]
    assert not [term for term in ('您', '原件', '原报告', '主检', '模板', '本人报告') if term in duty]


def test_writer_and_reviewer_rules_carry_no_case_facts_and_ban_back_office_words():
    rules = [*WRITING_GUIDE, *REVIEW_GUIDE, *_PLANNED_WRITING_GUIDE, *_PLANNED_REVIEW_GUIDE]
    assert not [
        term
        for term in ('甲状腺', '胆囊', 'C肽', 'OGTT', '糖耐量', '牙石', '痔疮', '父亲')
        if any(term in rule for rule in rules)
    ]
    review = ''.join(_PLANNED_REVIEW_GUIDE)
    writing = ''.join(_PLANNED_WRITING_GUIDE)
    for term in ('您', '原件', '原报告', '主检', '模板', '本人报告', '未形成解读', '部分报告'):
        assert term in ASSESSMENT_BANNED_TERMS
        assert f'“{term}”' in review and f'“{term}”' in writing
    assert '必改' in review
    for guard in ('肿瘤标志物', '结节', '血红蛋白', '妊娠'):
        assert guard in writing and guard in ''.join(WRITING_GUIDE)


def test_preface_and_reading_guide_are_formal_and_cover_the_three_parts():
    shown = ap.ASSESSMENT_PREFACE + ''.join(name + text for name, text in ap.ASSESSMENT_READING_GUIDE)
    assert [name for name, _ in ap.ASSESSMENT_READING_GUIDE] == ['报告定位', '双轨评价', '结构导览']
    assert not [term for term in ASSESSMENT_BANNED_TERMS if term in shown]
    assert not [term for term in ('免责', '仅供参考', '由医生结合', '不能替代') if term in shown]
    assert '临床参考区间' in shown and '目标区间' in shown
    # The fixed texts are already in reader wording; the pass leaves them identical.
    assert assessment_reader_text(ap.ASSESSMENT_PREFACE, prose=True) == ap.ASSESSMENT_PREFACE


# --- cell creation -------------------------------------------------------------


def test_priority_cells_follow_the_program_items():
    jobs = _jobs(_plan(**_ITEMS))
    for key in (
        'priority.urgent',
        'priority.routine',
        'priority.gaps',
        'layer.clinical',
        'layer.core',
        'bodySummary',
        'baselineSummary',
    ):
        assert key in jobs, key
    assert 'priority.planned' not in jobs
    assert set(jobs['priority.urgent'][4]) >= {'F1'}
    assert set(jobs['priority.routine'][4]) >= {'F3'}
    assert set(jobs['priority.gaps'][4]) >= {'F2'}
    assert jobs['layer.clinical'][4] and 'F2' in jobs['layer.core'][4]
    assert jobs['priority.urgent'][0] == 'healthOverview'
    assert jobs['layer.core'][2] == '第二层级：核心代谢与肠道微生态干预（90 天）'


def test_missing_items_create_no_cells_and_the_projection_states_the_gap():
    plan = _plan(clusters=[], routineFollowups=[], testGaps=[])
    jobs = _jobs(plan)
    for key in (
        'priority.urgent',
        'priority.routine',
        'priority.gaps',
        'priority.planned',
        'layer.clinical',
        'layer.core',
    ):
        assert key not in jobs, key
    clinical, _, _ = _views()
    findings = ap.planned_priority_findings(plan, [], {}, clinical)
    assert [row['code'] for row in findings] == ['urgent', 'routine']
    assert findings[0]['text'] == findings[0]['action'] == ap._NO_URGENT and findings[0]['items'] == []
    assert findings[0]['priority'] is None
    assert findings[1]['text'] == ap._NO_ROUTINE
    layers = ap.planned_conclusion_layers({}, [], findings)
    assert [row['name'] for row in layers] == [name for _, name in ap._CONCLUSION_LAYERS]
    assert [row['text'] for row in layers] == [ap._LAYER_EMPTY[code] for code, _ in ap._CONCLUSION_LAYERS]


def test_program_items_reach_the_writer_as_context_purpose():
    clinical, microbiome, facts = _views()
    bindings = ap._planned_source_bindings(clinical, microbiome, facts, _plan(**_ITEMS))
    urgent = bindings['priority.urgent']['contextPurpose']
    assert '筛查甲阳性' in urgent and '2 周内' in urgent and '专科甲' in urgent and '复查筛查甲' in urgent
    assert '每 12 个月' in bindings['priority.routine']['contextPurpose']
    assert '补查项目己' in bindings['priority.gaps']['contextPurpose']
    assert '筛查甲阳性' in bindings['layer.clinical']['contextPurpose']
    body = bindings['bodySummary']['contextPurpose']
    assert '体重指数 27.0 kg/m²（超重）' in body and '腰围 95 cm（偏高）' in body and '身高' not in body


# --- 1.3 and chapter 6 projection ---------------------------------------------------


def test_a_withheld_urgent_cell_never_removes_the_p0_cluster_or_the_layers():
    """Regression: a P0 cluster without an accepted priority/layer cell used to leave 1.3 and 6.1 empty."""
    clinical, _, _ = _views()
    plan = _plan()  # an older plan: no priorityItems / routineFollowups / testGaps
    findings = ap.planned_priority_findings(plan, _clusters(), {}, clinical)
    urgent = findings[0]
    assert urgent['code'] == 'urgent' and urgent['priority'] == 'P0'
    card = urgent['items'][0]
    assert card['name'] == '某阳性筛查结果的复查与查因' and card['priority'] == 'P0' and card['clusterId'] == 'C1'
    assert '建议复查筛查甲，专科甲就诊。' in card['text'] and '若复查后仍阳性，请进一步检查原因。' in card['text']
    assert [row['name'] for row in card['basisRows']] == ['筛查甲']
    assert urgent['text'] == card['text']
    layers = ap.planned_conclusion_layers({}, _clusters(), findings)
    assert len(layers) == 3
    assert card['text'] in layers[0]['text'] and layers[0]['clusterIds'] == ['C1']
    assert layers[1]['text'] == '代谢指标偏高的协同管理（C2）：定期复查指标乙。' and layers[1]['clusterIds'] == ['C2']
    assert layers[2]['text'] == ap._LAYER_EMPTY['maintenance']


def test_structured_cards_keep_fields_and_authored_sentences():
    clinical, _, _ = _views()
    editorial = {
        'priority.urgent': _text('筛查甲阳性，2 周内至专科甲完成复查与评估。', [3]),
        'layer.core': _text('第二层级成句。', [5]),
    }
    findings = ap.planned_priority_findings(_plan(**_ITEMS), _clusters(), editorial, clinical)
    urgent, routine, gaps = findings
    assert urgent['text'] == urgent['action'] == '筛查甲阳性，2 周内至专科甲完成复查与评估。'
    assert urgent['evidenceRefs'] == [3] and urgent['timeframe'] == {'text': '2 周内', 'days': 14}
    card = urgent['items'][0]
    assert len(urgent['items']) == 1  # the P0 cluster is owned by the red-flag item
    assert card['clusterId'] == 'C1' and card['ruleId'] == 'rule.screen'
    assert card['department'] == '专科甲' and card['checks'] == ['复查筛查甲', '专科评估']
    assert card['text'] == '筛查甲阳性：建议2 周内至专科甲就诊，完成复查筛查甲、专科评估。'
    assert card['basisRows'][0]['name'] == '筛查甲' and card['basisRows'][0]['sources'][0]['recordId'] == 'F1'
    assert routine['items'][0]['timeframe'] == {'text': '每 12 个月', 'days': 365}
    assert routine['items'][0]['text'] == '影像丁所见结节：专科乙随访，每 12 个月复查。'
    assert routine['text'] == routine['items'][0]['text']
    assert gaps['items'][0]['text'] == '补查项目己：指标乙偏高，需明确来源。'
    assert [row['name'] for row in gaps['items'][0]['basisRows']] == ['指标乙', '指标丙']
    layers = ap.planned_conclusion_layers(editorial, _clusters(), findings)
    assert layers[1]['text'] == '第二层级成句。' and layers[1]['evidenceRefs'] == [5]
    assert all(card['text'] in layers[0]['text'] for card in (*urgent['items'], *routine['items'], *gaps['items']))


def test_planner_fields_are_read_tolerantly():
    clinical, _, _ = _views()
    plan = _plan(
        priorityItems=[
            {'code': 'a', 'title': '事项一', 'timeframe': '首月', 'department': None, 'checks': '复查'},
            {'code': 'b', 'title': '事项二', 'timeframe': 7, 'checks': [{'name': '检查甲'}, 3, '']},
            {'code': 'c', 'title': '', 'timeframe': {'days': 'x'}},
            'not-a-row',
            {'code': 'd', 'title': '非P0', 'priority': 'P1'},
        ],
        routineFollowups='bad',
        testGaps=None,
    )
    findings = ap.planned_priority_findings(plan, [], {}, clinical)
    cards = findings[0]['items']
    assert [card['name'] for card in cards] == ['事项一', '事项二']
    assert cards[0]['timeframe'] == {'text': '首月', 'days': None} and cards[0]['department'] is None
    assert cards[0]['checks'] == ['复查']
    assert cards[1]['timeframe'] == {'text': '7 天内', 'days': 7} and cards[1]['checks'] == ['检查甲']
    assert findings[1]['text'] == ap._NO_ROUTINE and len(findings) == 2


def test_routine_follow_up_falls_back_to_recorded_advice_outside_clusters(monkeypatch):
    clinical, _, facts = _views()
    data = SimpleNamespace(
        _template_layout={
            'groups': [
                {'code': 'p1', 'focusLabel': '优先查因'},
                {'code': 'p3', 'focusLabel': '结合症状随访'},
                {'code': 'p4', 'focusLabel': '持续管理'},
            ]
        }
    )
    monkeypatch.setattr(
        ap,
        '_source_clinical_actions',
        lambda data, facts: [
            {'code': 'p1', 'name': '筛查甲', 'originalAdvice': ['尽快复查。'], 'sourceFactIds': ['F1']},
            {'code': 'p3', 'name': '影像丁所见结节', 'originalAdvice': ['定期复查影像。'], 'sourceFactIds': ['F3']},
            {'code': 'p4', 'name': '无建议的发现', 'originalAdvice': [], 'sourceFactIds': []},
        ],
    )
    findings = ap.planned_priority_findings(_plan(), _clusters(), {}, clinical, data=data, facts=facts)
    routine = findings[1]
    assert [card['name'] for card in routine['items']] == ['影像丁所见结节']
    assert routine['items'][0]['text'] == '影像丁所见结节：定期复查影像。'


def test_management_directions_follow_section_6_2():
    editorial = {
        'management.probiotic': _text('功效菌方向。'),
        'management.fiber': _text('纤维方向。'),
        'management.lifestyle': _text('去诱因要点。'),
    }
    directions = ap.planned_management_directions(editorial, has_microbiome=True, has_urgent=True, has_lifestyle=True)
    mtpn, lsmi = directions
    assert mtpn['name'] == ap._MTPN_NAME and mtpn['text'] == ap._MTPN_AFTER_P0
    assert [row['name'] for row in mtpn['items']] == ['功效菌选择依据', '益生元与膳食纤维']
    assert lsmi['code'] == 'lifestyle' and lsmi['name'] == ap._LSMI_NAME and lsmi['text'] == '去诱因要点。'
    empty = ap.planned_management_directions({}, has_microbiome=False, has_urgent=False, has_lifestyle=False)
    assert empty == [
        {'name': ap._MTPN_NAME, 'items': [], 'text': ap._MTPN_NO_MICROBIOME},
        {'code': 'lifestyle', 'name': ap._LSMI_NAME, 'text': ap._LSMI_NO_FACTORS},
    ]
    # A planned lifestyle cell that did not pass review stays visibly missing.
    assert ap.planned_management_directions({}, has_microbiome=True, has_urgent=False, has_lifestyle=True) == []


# --- reader wording -------------------------------------------------------------------


def _presentation_dict():
    return {
        'overview': {
            'priorityFindings': [
                {
                    'code': 'routine',
                    'name': '常规专科随访',
                    'text': '按原建议随访；主检结论建议复查。',
                    'action': '您需按原医嘱复查。',
                    'items': [],
                }
            ],
            'bodySummary': '原件未作分级；按原件在正常范围。',
        },
        'clinical': {
            'clinicalGroups': [
                {
                    'code': 'g',
                    'name': '组',
                    'summary': '本人报告的既往史与原报告分档一致。',
                    'templateRows': [
                        {
                            'code': 'r',
                            'name': '指标',
                            'status': '按原件记录，未作统一分级；指标高于模板目标',
                            'comparisonReference': '模板比较目标（非诊断界值）：指标 <1.5',
                            'summary': '指标 6.12（计算值）高于模板比较目标。',
                            'rows': [
                                {
                                    'name': '指标',
                                    'value': '6.12',
                                    'unit': 'mIU/L',
                                    'referenceRange': '原报告健康方向: >0',
                                    'templateStatus': '原文正常所见',
                                    'observedAt': '2026-01-05',
                                    'sources': [_source('F9')],
                                }
                            ],
                        }
                    ],
                }
            ],
            'symptoms': [
                {
                    'code': 'S1',
                    'name': '容易疲劳',
                    'question': '您最近有哪些具体不适？',
                    'history': '您最近通常每天睡多长时间？：6至7小时；您平时一周大约有几天安排运动？：没有运动',
                    'value': '容易疲劳',
                }
            ],
        },
        'clusters': [
            {
                'id': 'C1',
                'name': '主题',
                'clinicalSignals': [
                    {
                        'name': '原报告医嘱 · 主检结论：某发现',
                        'value': '建议复查。',
                        'status': '原报告医嘱',
                        'sources': [_source('F1')],
                    },
                    {
                        'name': '您平时的正餐通常吃到哪些食物？可多选。',
                        'value': '米饭、蔬菜',
                        'status': '本人自述',
                        'sources': [_source('S2')],
                    },
                ],
            }
        ],
        'conclusion': {'layers': [{'code': 'core', 'name': '第二层级', 'text': '建议您每周记录体重。'}]},
    }


def test_reader_wording_reaches_every_six_chapter_text_but_not_values():
    view = _presentation_dict()
    changed = present_assessment_for_reader(view)
    assert changed >= 10
    shown = []

    def collect(node, key=None):
        if isinstance(node, dict):
            for name, item in node.items():
                collect(item, name)
        elif isinstance(node, list):
            for item in node:
                collect(item, key)
        elif isinstance(node, str) and not (key == 'status' and node == '原报告医嘱'):
            shown.append(node)

    collect(view)
    assert not [
        text for text in shown for term in ('您', '原件', '原报告', '主检', '模板', '本人报告', '原文') if term in text
    ]
    row = view['clinical']['clinicalGroups'][0]['templateRows'][0]
    assert row['status'] == '未作统一分级；指标高于目标区间'
    assert row['comparisonReference'] == '目标区间（非诊断界值）：指标 <1.5'
    assert row['summary'] == '指标6.12（计算值）高于目标区间。'
    measured = row['rows'][0]
    assert (measured['value'], measured['unit'], measured['observedAt']) == ('6.12', 'mIU/L', '2026-01-05')
    assert measured['referenceRange'] == '报告健康方向: >0' and measured['templateStatus'] == '正常所见'
    assert measured['sources'] == [_source('F9')]
    assert view['overview']['bodySummary'] == '报告未作分级；在正常范围。'
    finding = view['overview']['priorityFindings'][0]
    assert finding['text'] == '按检查建议随访；体检结论建议复查。' and finding['action'] == '需按检查建议复查。'
    symptom = view['clinical']['symptoms'][0]
    assert symptom['history'] == '最近通常每日睡眠时长：6至7小时；平时每周安排运动天数：没有运动'
    assert symptom['question'] == '最近有哪些具体不适？' and symptom['value'] == '容易疲劳'
    advice, diet = view['clusters'][0]['clinicalSignals']
    assert advice['status'] == '原报告医嘱' and advice['name'] == '检查建议 · 体检结论：某发现'
    assert diet['name'] == '平时的正餐常吃食物' and diet['value'] == '米饭、蔬菜'
    assert view['conclusion']['layers'][0]['text'] == '建议每周记录体重。'


def test_reader_wording_applies_to_the_presentation_model():
    presentation = ap.AssessmentPresentation(
        overview={'preface': ap.ASSESSMENT_PREFACE, 'summary': '受检者主检结论为某发现。'},
        clinical={},
        microbiome={'riskSummary': '原报告分档为低。'},
        clusters=[],
        lifestyleFactors=[],
        clusterSummary='您的主题见下文。',
        conclusion={'layers': []},
    )
    assert present_assessment_for_reader(presentation) == 3
    assert presentation.overview['summary'] == '受检者体检结论为某发现。'
    assert presentation.overview['preface'] == ap.ASSESSMENT_PREFACE
    assert presentation.microbiome['riskSummary'] == '报告分档为低。'
    assert presentation.cluster_summary == '主题见下文。'
