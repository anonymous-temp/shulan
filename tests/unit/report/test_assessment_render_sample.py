"""Chart fields (spec 2.7) and the six-chapter sample rendering, on synthetic callbacks only."""

import re
from copy import deepcopy

from modules.report.assessment_callback_view import assessment_view_for_rendering
from modules.report.assessment_callback_view import order_assessment_for_delivery
from modules.report.assessment_chart_fields import parse_grade_bands
from modules.report.assessment_chart_fields import parse_reference
from modules.report.assessment_chart_fields import system_atoms
from modules.report.assessment_chart_fields import system_chart_fields
from modules.report.assessment_microbiome_view import annotate_microbiome_chart_fields
from modules.report.assessment_presentation import _complete_systems
from scripts.render_evaluation_report import render_evaluation_html

_FUNCTION_BANDS = (
    '偏低:(0-25)表示相应的营养物质代谢能力弱;\n正常:(25-75)表示相应的营养物质代谢能力正常;\n'
    '偏高:(75-100)表示相应的营养物质代谢能力强。'
)
_FORBIDDEN = ('您的', '部分报告', '免责', '未形成解读', '暂无可展示的解读')


def _source(code='A', record='r'):
    return [{'sourceCode': code, 'recordId': record, 'observedAt': '2026-05-01'}]


def _row(name, value, unit=None, reference=None, status=None, record=None, **extra):
    return {
        'name': name,
        'value': value,
        'unit': unit,
        'referenceRange': reference,
        'status': status,
        'templateStatus': status,
        'observedAt': '2026-05-01',
        'sources': _source(record=record or name),
        'evidenceRefs': [],
        **extra,
    }


def _microbiome():
    return {
        'sampleContext': {'sampleType': '粪便', 'receivedDate': '2026-06-01'},
        'structureGroups': [
            {
                'code': 'overallBalance',
                'name': '微生态综合评分',
                'rows': [
                    {'code': 'microbiomeHealthScore', **_row('肠道微生态总评分', '58.2')},
                    {'code': 'balanceStatus', **_row('平衡状态', 'II 度失调')},
                ],
            },
            {
                'code': 'diversity',
                'name': '物种多样性',
                'rows': [
                    {
                        'code': 'diversity',
                        **_row(
                            '肠道菌群多样性',
                            '3.1',
                            reference='偏低: 0.5-2.5；较低: 2.5-3.5；正常: 3.5-4',
                            status='较低',
                        ),
                    }
                ],
            },
            {
                'code': 'gmwi2',
                'name': 'GMWI2',
                'rows': [
                    {
                        'code': 'gmwi2',
                        **_row(
                            '健康指数', '-0.4', reference='原报告健康方向: >0；不健康倾向: -1~0', status='不健康倾向'
                        ),
                    }
                ],
            },
            {
                'code': 'gutAge',
                'name': '肠道年龄',
                'actualAge': 40,
                'gutAgeDifference': 5.0,
                'rows': [{'code': 'gutAge', **_row('肠道年龄评估', '45', unit='岁', reference='个人对照年龄 40 岁')}],
            },
        ],
        'organismGroups': [
            {
                'code': 'beneficial',
                'name': '有益菌',
                'rows': [
                    {'code': 'beneficialBacteria', **_row('有益菌', '异常', status='异常')},
                    {'code': 'lac', **_row('嗜酸乳杆菌', '0', unit='%', reference='0.0001~0.0669', status='偏低')},
                    {'code': 'pro', **_row('变形菌门', '2.9', unit='%', reference='0~1.0000', status='偏高')},
                ],
                'normalBaseline': {'count': 1, 'rows': [_row('双歧杆菌属', '3.3', unit='%', reference='0.5~9')]},
            }
        ],
        'functionalGroups': [
            {
                'code': 'scfa',
                'name': '2. 短链脂肪酸合成',
                'rows': [
                    {
                        'code': 'template_function:a',
                        **_row('乙酸', '75', unit='分', reference=_FUNCTION_BANDS, status='↑'),
                    }
                ],
                'normalBaseline': {
                    'count': 1,
                    'rows': [
                        {
                            'code': 'template_function:b',
                            **_row('丁酸', '65', unit='分', reference=_FUNCTION_BANDS, status='正常'),
                        }
                    ],
                },
            }
        ],
        'riskSummary': '低风险：结直肠癌（0.12）。',
    }


def _clinical():
    lipids = [
        _row('甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高'),
        _row('总胆固醇(TC)', '5.95', 'mmol/L', '3.14-5.86', '偏高'),
        _row('高密度脂蛋白胆固醇', '1.30', 'mmol/L', '>1.04', None),
    ]
    glucose = [
        _row(
            '空腹葡萄糖(Glu)',
            '7.2',
            'mmol/L',
            None,
            None,
            valueNumber=7.2,
            referenceLow=3.9,
            referenceHigh=6.1,
            referenceKind='range',
            level='focus',
            direction='up',
            target={'low': 4.4, 'high': 6.1, 'text': '4.4–6.1 mmol/L', 'evidenceRefs': [1]},
        ),
    ]
    return {
        'bodyGroups': [
            {
                'code': 'weightWaist',
                'name': '身高、体重与围度',
                'summary': '体重指数属超重。',
                'rows': [_row('体重指数(BMI)', '26.1', 'kg/m2', '18.5-23.9', '超重'), _row('身高', '170.0', 'cm')],
            }
        ],
        'clinicalGroups': [
            {
                'code': 'cardiometabolic',
                'name': '糖脂代谢',
                'templateRows': [
                    {
                        'code': 'lipids',
                        'name': '血脂',
                        'rows': lipids,
                        'summary': '甘油三酯升高。',
                        'evidenceRefs': [1],
                    },
                    {'code': 'glucose', 'name': '血糖', 'rows': glucose, 'summary': None},
                ],
            },
            {
                'code': 'hematologyImmune',
                'name': '血液',
                'templateRows': [
                    {'code': 'blood', 'name': '血常规', 'rows': [_row('血红蛋白', '150', 'g/L', '130-175')]},
                ],
            },
        ],
        'symptoms': [
            {
                'code': 'S1',
                'name': '容易疲劳',
                'value': '容易疲劳',
                'history': '您最近通常每天睡多长时间？：6至7小时；您平时一周大约有几天安排运动？：没有运动',
                'interpretation': '疲劳与睡眠时长并列记录。',
                'clinicalSignals': [_row('甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高')],
                'microbiomeSignals': [_row('嗜酸乳杆菌', '0', '%', '0.0001~0.0669', '偏低')],
                'evidenceRefs': [],
            }
        ],
        'specialtyFindings': [],
        'trends': [
            {
                'name': '甘油三酯',
                'direction': '上升',
                'points': [{'date': '2025-05-01', 'value': '2.1'}, {'date': '2026-05-01', 'value': '3.34'}],
            }
        ],
        'normalBaselines': [],
        'digestiveContext': [],
    }


def _view(**overrides):
    view = {
        'version': 'assessment.v1',
        'title': '示例评估报告',
        'overview': {
            'dataTiming': {'clinicalDate': '2026-05-01'},
            'preface': '固定前言。',
            'readingGuide': [{'name': '报告定位', 'text': '说明需要关注的发现。'}],
            'summary': '总体以血脂异常为主。',
            'evidenceRefs': [],
            'systems': [],
            'priorityFindings': [],
            'graphHub': '核心健康主题',
            'graphClusterIds': ['C1', 'C2'],
        },
        'clinical': _clinical(),
        'microbiome': _microbiome(),
        'clusters': [
            {
                'id': 'C1',
                'name': '粪便隐血阳性的复查',
                'priority': 'P0',
                'interpretation': None,
                'action': None,
                'evidenceRefs': [],
                'clinicalSignals': [_row('粪便隐血试验', '阳性', reference='阴性', status='阳性')],
                'microbiomeSignals': [],
            },
            {
                'id': 'C2',
                'name': '血脂异常与体重管理',
                'priority': 'P1',
                'interpretation': '甘油三酯与体重指数同时偏高。',
                'action': '三个月后复查血脂。',
                'evidenceRefs': [1],
                'clinicalSignals': [
                    _row('甘油三酯(TG)', '3.34', 'mmol/L', '0.00-1.70', '偏高'),
                    {**_row('您平时一周大约有几天安排运动？', '没有运动', status='本人自述'), 'sources': _source('B')},
                ],
                'microbiomeSignals': [_row('变形菌门', '2.9', '%', '0~1.0000', '偏高')],
            },
        ],
        'clusterSummary': None,
        'clusterSummaryEvidenceRefs': [],
        'lifestyleSummary': None,
        'lifestyleSummaryEvidenceRefs': [],
        'lifestyleFactors': [
            {
                'code': 'lifestyle.exercise',
                'name': '体力活动不足与久坐',
                'kind': 'habit',
                'value': '您平时一周大约有几天安排运动？：没有运动',
                'interpretation': '建议逐步增加活动。',
                'clusterIds': ['C2'],
                'evidenceRefs': [],
            }
        ],
        'lifestyleRecognition': [],
        'conclusion': {
            'summary': '先完成粪便隐血复查。',
            'evidenceRefs': [],
            'priorities': [
                {'id': 'C2', 'name': '血脂异常与体重管理', 'priority': 'P1', 'action': '三个月后复查血脂。'}
            ],
            'layers': [],
            'managementDirections': [
                {'code': 'lifestyle', 'name': '个体化生活方式管理', 'text': '每周运动 150 分钟。'}
            ],
        },
    }
    for key, value in overrides.items():
        view[key] = value
    return view


def _body(view, status='partial_success'):
    appendices = {'referenceLiterature': [{'number': 1, 'title': '示例指南', 'year': 2024, 'doi': None, 'url': None}]}
    return {
        'schemaVersion': 'evaluate.v2.0',
        'type': 'evaluate',
        'status': status,
        'partialReasons': ['REPORT_CONTENT_INCOMPLETE', 'REPORT_LAYOUT_INCOMPLETE'],
        'subject': {'name': '合成受检者', 'gender': '男', 'age': 40},
        'sample': {'sampleId': 'demo-1', 'sampleDate': None, 'reportDate': '2026-06-01', 'method': '宏基因组测序'},
        'data': order_assessment_for_delivery(deepcopy(view), appendices),
    }


def _page(view, **kwargs):
    return render_evaluation_html(_body(view, **kwargs)).split('</style>', 1)[1]


def test_references_and_grade_bands_parse_from_source_strings():
    assert parse_reference('3.89-6.11') == {'low': 3.89, 'high': 6.11, 'kind': 'range'}
    assert parse_reference('男性<90') == {'low': None, 'high': 90.0, 'kind': 'upper'}
    assert parse_reference('>1') == {'low': 1.0, 'high': None, 'kind': 'lower'}
    assert parse_reference('-4.0-3.9') == {'low': -4.0, 'high': 3.9, 'kind': 'range'}
    assert parse_reference('阴性') == {'low': None, 'high': None, 'kind': 'qualitative'}
    assert parse_reference('个人对照年龄 48 岁') is None
    assert parse_grade_bands(_FUNCTION_BANDS) == [
        {'label': '偏低', 'low': 0.0, 'high': 25.0},
        {'label': '正常', 'low': 25.0, 'high': 75.0},
        {'label': '偏高', 'low': 75.0, 'high': 100.0},
    ]
    # Band labels lose back-office prefixes; an open end stays open.
    assert parse_grade_bands('原报告健康方向: >0；不健康倾向: -1~0') == [
        {'label': '健康方向', 'low': 0.0, 'high': None},
        {'label': '不健康倾向', 'low': -1.0, 'high': 0.0},
    ]


def test_microbiome_rows_gain_chart_fields_without_changing_source_fields():
    view = _microbiome()
    original = deepcopy(view)
    annotate_microbiome_chart_fields(view)
    structure = {row['code']: row for group in view['structureGroups'] for row in group['rows']}
    organisms = {row['code']: row for row in view['organismGroups'][0]['rows']}
    functions = view['functionalGroups'][0]
    for group, before in zip(view['structureGroups'], original['structureGroups'], strict=True):
        for row, old in zip(group['rows'], before['rows'], strict=True):
            assert {key: row[key] for key in old} == old  # append only
    assert structure['balanceStatus']['level'] == 'focus'
    assert structure['microbiomeHealthScore']['level'] == 'focus'  # inherits its dysbiosis grade
    assert structure['microbiomeHealthScore']['valueNumber'] == 58.2
    assert structure['diversity']['bands'][1] == {'label': '较低', 'low': 2.5, 'high': 3.5}
    assert structure['diversity']['level'] == 'borderline'
    assert structure['gmwi2']['bands'][0]['label'] == '健康方向'
    assert (structure['gutAge']['ageDifference'], structure['gutAge']['level']) == (5.0, 'borderline')
    assert organisms['lac']['abundance'] == 0.0
    assert (organisms['lac']['referenceLow'], organisms['lac']['referenceHigh']) == (0.0001, 0.0669)
    assert (organisms['lac']['direction'], organisms['lac']['level']) == ('down', 'borderline')
    assert organisms['beneficialBacteria']['level'] == 'borderline'
    assert view['organismGroups'][0]['normalBaseline']['rows'][0]['level'] == 'normal'
    acetate = functions['rows'][0]
    assert (acetate['score'], acetate['direction'], acetate['level']) == (75.0, 'up', 'borderline')
    assert [band['label'] for band in acetate['bands']] == ['偏低', '正常', '偏高']
    assert functions['normalBaseline']['rows'][0]['level'] == 'normal'


def test_system_fields_keep_six_anchors_and_count_the_rest():
    rows = [_row(f'指标{index}', str(10 + index), 'U/L', '0-5', '偏高', record=f'r{index}') for index in range(8)]
    rows.append(_row('正常项', '3', 'U/L', '0-5', None))
    clinical = {'clinicalGroups': [{'code': 'organMetabolism', 'templateRows': [{'code': 'x', 'rows': rows}]}]}
    atoms = system_atoms('digestiveRenal', ('organMetabolism',), clinical, {})
    used = [{'sources': _source(record='r7')}]
    fields = system_chart_fields(atoms, used)
    assert fields['level'] == 'focus'
    assert len(fields['anchors']) == 6 and fields['moreCount'] == 2
    assert fields['anchors'][0]['name'] == '指标7'  # an anchor of the reviewed cell comes first
    assert fields['anchors'][0] == {'name': '指标7', 'value': '17', 'unit': 'U/L', 'direction': 'up', 'level': 'focus'}
    assert system_chart_fields([], ()) == {'level': 'notTested', 'anchors': [], 'moreCount': 0}


def test_every_fixed_system_is_listed_with_its_level():
    reviewed = {
        'code': 'cardiometabolic',
        'name': '糖脂代谢与心脑血管系统',
        'status': '分项异常',
        'summary': '血脂偏高。',
        'evidenceRefs': [],
        'rows': [],
    }
    systems = _complete_systems([dict(reviewed)], _clinical(), annotate_microbiome_chart_fields(_microbiome()))
    assert [row['code'] for row in systems] == [
        'cardiometabolic',
        'digestiveRenal',
        'microbiome',
        'endocrineMusculoskeletal',
        'hematologyImmune',
    ]
    by_code = {row['code']: row for row in systems}
    assert {key: by_code['cardiometabolic'][key] for key in reviewed} == reviewed
    assert by_code['cardiometabolic']['level'] == 'focus'
    assert by_code['digestiveRenal']['level'] == 'notTested' and by_code['digestiveRenal']['summary'] is None
    assert by_code['hematologyImmune']['level'] == 'normal'
    assert by_code['microbiome']['anchors'][0]['name'] == '肠道微生态总评分'


def _priority_findings():
    return [
        {
            'code': 'urgent',
            'name': '优先排查',
            'priority': 'P0',
            'text': '粪便隐血阳性需优先复查。',
            'evidenceRefs': [],
            'items': [
                {
                    'code': 'fobt',
                    'name': '粪便隐血阳性复查',
                    'priority': 'P0',
                    'timeframe': {'text': '2 周内', 'days': 14},
                    'department': '消化内科',
                    'checks': ['粪便隐血复查', '肠镜评估'],
                    'basisRows': [_row('粪便隐血试验', '阳性', reference='阴性', status='阳性')],
                    'text': '粪便隐血阳性复查：建议2 周内至消化内科就诊，完成粪便隐血复查、肠镜评估。',
                    'clusterId': 'C1',
                    'ruleId': 'fobt-positive',
                }
            ],
        },
        {
            'code': 'routine',
            'name': '常规专科随访',
            'priority': None,
            'text': '胆囊息肉按间隔复查。',
            'evidenceRefs': [],
            'items': [
                {
                    'code': 'polyp',
                    'name': '胆囊息肉',
                    'priority': None,
                    'timeframe': {'text': '每 6 个月', 'days': 182},
                    'department': '肝胆外科',
                    'checks': ['胆囊B超'],
                    'basisRows': [],
                    'text': '胆囊息肉：肝胆外科随访，复查胆囊B超，每 6 个月复查。',
                },
                {
                    'code': 'eye',
                    'name': '屈光不正',
                    'priority': None,
                    'timeframe': None,
                    'department': None,
                    'checks': [],
                    'basisRows': [],
                    'text': '屈光不正：建议眼科门诊验光。',
                },
            ],
        },
        {
            'code': 'gaps',
            'name': '检测盲区',
            'priority': None,
            'text': None,
            'evidenceRefs': [],
            'items': [
                {
                    'code': 'hcy',
                    'name': '同型半胱氨酸',
                    'reason': '评估一碳代谢',
                    'text': '同型半胱氨酸：评估一碳代谢。',
                }
            ],
        },
    ]


def _layers():
    return [
        {
            'code': 'clinical',
            'name': '第一层级：临床专科排查与随访（首月）',
            'text': '先完成隐血复查。',
            'evidenceRefs': [],
            'clusterIds': ['C1'],
        },
        {
            'code': 'core',
            'name': '第二层级：核心代谢与肠道微生态干预（90 天）',
            'text': '控制体重与血脂。',
            'evidenceRefs': [],
            'clusterIds': ['C2'],
        },
        {
            'code': 'maintenance',
            'name': '第三层级：营养储备与长期健康维护（持续）',
            'text': '本次未形成需协同调理的失衡集群。',
            'evidenceRefs': [],
            'clusterIds': [],
        },
    ]


def test_ordered_callback_passes_new_fields_through():
    view = _view()
    view['overview']['systems'] = [
        {
            'code': 'cardiometabolic',
            'name': '糖脂',
            'level': 'focus',
            'anchors': [{'name': 'TG', 'value': '3.34', 'unit': 'mmol/L', 'direction': 'up'}],
            'moreCount': 1,
            'rows': [],
        }
    ]
    view['overview']['priorityFindings'] = _priority_findings()
    view['conclusion']['layers'] = _layers()
    view['clinical']['baselineSummary'] = '肝肾功能与血常规在参考区间内。'
    data = _body(view)['data']
    chapters = {chapter['code']: chapter['content'] for chapter in data['chapters']}
    overview = chapters['overview']
    assert overview['systems'][0]['anchors'][0]['value'] == '3.34' and overview['systems'][0]['moreCount'] == 1
    assert overview['priorityFindings'][0]['items'][0]['timeframe'] == {'text': '2 周内', 'days': 14}
    assert chapters['conclusion']['layers'][1]['clusterIds'] == ['C2']
    assert chapters['clinical']['baselineSummary'] == '肝肾功能与血常规在参考区间内。'
    assert assessment_view_for_rendering(data)['overview']['priorityFindings'] == view['overview']['priorityFindings']


def test_sample_draws_the_spec_components():
    page = _page(_view())
    for marker in (
        'class="as-cover"',
        'class="as-legend"',
        'class="as-mrow"',
        'class="band"',
        'class="as-sys lv-',
        'class="as-act p0"',
        'figure class="as-graph"',
        'class="as-matrix"',
        'class="as-srow"',
        'class="as-cluster"',
        'as-lsm',
        'class="as-roadmap"',
        'class="as-trend"',
        'class="as-bandkey"',
    ):
        assert marker in page, marker
    # Five fixed system cards, a system without data reads as not tested.
    assert page.count('<article class="as-sys') == 5 and '本次资料未包含该系统的检查项目。' in page
    # A cluster card has the three evidence columns and the reviewed reading.
    assert all(title in page for title in ('临床与影像锚点', '微生态与代谢锚点', '相关生活因素', '健康影响'))
    assert '先完成第一层级的专科排查' in page  # a P0 item precedes formula advice
    for word in _FORBIDDEN:
        assert word not in page, word
    assert '您' not in page  # questionnaire prompts are shown as impersonal labels
    assert '最近每日睡眠时长：6至7小时' in page


def test_structured_clinical_fields_win_and_strings_are_the_fallback():
    page = _page(_view())
    glucose = re.search(r'<div class="as-mrow"><div class="nm">空腹葡萄糖\(Glu\).*?</div></div>', page).group(0)
    assert 'class="dot lv-focus"' in glucose and 'class="tgt"' in glucose
    assert '>6.1<' in glucose and '目标 4.4–6.1 mmol/L' in glucose and 'href="#ref-1"' in glucose
    lipid = re.search(r'<div class="as-mrow"><div class="nm">甘油三酯\(TG\).*?</div></div>', page).group(0)
    assert 'class="dot lv-focus"' in lipid and '参考 0.00-1.70' in lipid and '>1.7<' in lipid
    # Normal members are compressed into one line instead of a drawn row.
    assert '高密度脂蛋白胆固醇 <span class="num">1.30 mmol/L</span>' in page


def test_empty_sections_show_their_missing_statements():
    view = _view(
        clinical={
            'bodyGroups': [],
            'clinicalGroups': [],
            'symptoms': [],
            'specialtyFindings': [],
            'trends': [],
            'normalBaselines': [],
            'digestiveContext': [],
        },
        microbiome={},
        clusters=[],
        lifestyleFactors=[],
        conclusion={},
    )
    view['overview']['graphClusterIds'] = []
    page = _page(view)
    for sentence in (
        '本次资料未包含体格测量。',
        '本次未发现需优先就医排查的结果。',
        '本次各系统未形成需集中干预的失衡方向。',
        '本次未记录主诉症状。',
        '本次资料未包含可比较的历次结果。',
        '本次资料未包含肠道菌群检测。',
        '未形成需集中干预的失衡集群。',
        '本次未发现需在首月内完成的专科排查或随访事项。',
        '本次资料不足以形成配方与生活方式干预方向。',
    ):
        assert sentence in page, sentence
    assert page.count('本次资料未包含该系统的检查项目。') == 5
    assert '2.2 主要异常与日常感受的关联' in page
    assert page.count('<span class="as-muted">未记录</span>') == 5  # five lifestyle dimensions
    for word in _FORBIDDEN:
        assert word not in page, word


def test_priority_items_become_action_cards_schedule_and_roadmap():
    view = _view()
    view['overview']['priorityFindings'] = _priority_findings()
    view['conclusion']['layers'] = _layers()
    view['clinical']['baselineSummary'] = '肝肾功能与血常规在参考区间内。'
    page = _page(view)
    card = re.search(r'<article class="as-act p0">.*?</article>', page).group(0)
    assert all(part in card for part in ('粪便隐血阳性复查', '2 周内', '消化内科', '粪便隐血复查；肠镜评估'))
    assert 'href="#as-cluster-C1"' in card and '>阳性<' in card
    assert '建议2 周内至消化内科就诊' not in card  # the composed line repeats the drawn fields
    schedule = re.search(r'<table class="sched">.*?</table>', page).group(0)
    assert '胆囊息肉' in schedule and schedule.count('class="mk"') == 1 and schedule.count('mk re') == 1
    assert '屈光不正' not in schedule and '建议眼科门诊验光。' in page  # no interval: listed, not scheduled
    assert '同型半胱氨酸' in page and '评估一碳代谢' in page
    stages = dict(re.findall(r'<section class="as-stage (s\d)">(.*?)</section>', page))
    assert '粪便隐血阳性复查' in stages['s1'] and '胆囊息肉' in stages['s1'] and '补查 同型半胱氨酸' in stages['s1']
    assert 'C1 粪便隐血阳性的复查' not in stages['s1']  # the P0 cluster is already its action card
    assert 'C2 血脂异常与体重管理' in stages['s2']
    assert '本次未形成需协同调理的失衡集群。' in stages['s3']
    assert '肝肾功能与血常规在参考区间内。' in page


def test_recorded_advice_is_read_from_its_kind_field():
    view = _view()
    advice = {**_row('检查建议 · 体检结论：粪便隐血', '建议复查大便隐血。'), 'kind': 'advice', 'status': None}
    view['clusters'][0]['clinicalSignals'].append(advice)
    page = _page(view)
    card = re.search(r'<article class="as-cluster" id="as-cluster-C1">.*?</article>', page).group(0)
    assert '<h5>检查建议</h5><p>建议复查大便隐血。</p>' in card
    assert '建议复查大便隐血' not in card.split('<h5>检查建议</h5>')[0]  # not drawn as a clinical anchor


def test_relabelled_questionnaire_answers_are_split_into_lines():
    view = _view()
    view['clinical']['symptoms'][0]['history'] = '最近通常每日睡眠时长：6至7小时；平时每周安排运动天数：没有运动'
    page = _page(view)
    assert (
        '<span class="qa">最近通常每日睡眠时长：6至7小时</span><span class="qa">平时每周安排运动天数：没有运动</span>'
        in page
    )
