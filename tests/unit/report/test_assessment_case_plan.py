"""Whole-case plan: handle validation, program rows, grade corrections, ordering, limits and the safety net."""

from copy import deepcopy

import pytest

from modules.report.assessment_case_guides import SYSTEMS
from modules.report.assessment_case_plan import CASE_PLAN_VERSION
from modules.report.assessment_case_plan import OTHER_SYSTEM_NAME
from modules.report.assessment_case_plan import apply_priority_safety_net
from modules.report.assessment_case_plan import clinical_fact_groups
from modules.report.assessment_case_plan import normalize_case_analysis
from modules.report.assessment_case_plan import plan_statistics
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_facts
from tests.unit._support.assessment_case import fact
from tests.unit._support.assessment_case import handle_named
from tests.unit._support.assessment_case import handle_of
from tests.unit._support.assessment_case import raw_analysis

CODES = [code for code, _ in SYSTEMS]


@pytest.fixture
def sheet():
    return build_sheet()


def normalize(raw, sheet, clinical=True):
    return normalize_case_analysis(raw, sheet, clinical=default_clinical() if clinical is True else clinical)


def system_of(plan, code):
    return next(system for system in plan['systems'] if system['code'] == code)


def row(title, handles, status='亚临床边界', **extra):
    return {'title': title, 'handles': handles, 'status': status, **extra}


# ---- references and structure -------------------------------------------------------------------------------------


def test_realistic_answer_is_normalised_and_every_reference_exists(sheet):
    plan = normalize(raw_analysis(sheet), sheet)

    assert plan['version'] == CASE_PLAN_VERSION
    assert plan['phenotype'] == {'label': '代谢性脂肪肝倾向', 'handles': ['L3', 'L5']}
    assert [system['code'] for system in plan['systems']] == CODES
    assert plan['bodyRows'][0]['index'] == 1 and plan['bodyRows'][0]['origin'] == 'model'
    cardio = system_of(plan, 'cardiometabolic')
    assert [(item['index'], item['title']) for item in cardio['rows']] == [(1, '血脂与代谢'), (2, '血糖')]
    assert plan['symptoms'][0]['code'] == 'S1' and plan['symptoms'][0]['handles'] == ['Q2']
    assert plan['audit']['dropped'] == [] and plan['audit']['programRows'] == []

    def handles(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith('andles') or key == 'handles':
                    yield from value
                else:
                    yield from handles(value)
        elif isinstance(node, list):
            for value in node:
                yield from handles(value)

    assert set(handles({key: value for key, value in plan.items() if key != 'audit'})) <= set(sheet.entries)


def test_garbage_input_gives_an_empty_but_complete_plan(sheet):
    for raw in (None, [], 'text', {'systems': 'x', 'clusters': 3, 'bodyRows': {'a': 1}}):
        plan = normalize(raw, sheet)
        assert [system['code'] for system in plan['systems']] == CODES
        assert plan['clusters'] == [] and plan['priorityItems'] == [] and plan['symptoms'] == []
        assert plan['phenotype'] == {'label': None, 'handles': []}


def test_unknown_handles_are_dropped_and_a_row_without_a_record_is_recorded(sheet):
    raw = {
        'systems': [
            {
                'code': 'cardiometabolic',
                'status': '重点关注',
                'rows': [
                    row('有效', ['L3', 'L99', 'Q1']),  # Q handles are not measured records
                    row('全无效', ['L98', 'M1']),
                    row('无编号', []),
                    {'handles': ['L3']},  # no title
                ],
            }
        ]
    }

    plan = normalize(raw, sheet)

    rows = system_of(plan, 'cardiometabolic')['rows']
    assert [(item['title'], item['handles']) for item in rows[:1]] == [('有效', ['L3'])]
    dropped = plan['audit']['dropped']
    assert {'scope': 'cardiometabolic', 'title': '全无效', 'reason': 'no_valid_record'} in dropped
    assert {'scope': 'cardiometabolic', 'title': '无编号', 'reason': 'no_valid_record'} in dropped
    assert {'scope': 'cardiometabolic', 'title': '', 'reason': 'no_valid_record'} in dropped  # no title


def test_record_may_be_cited_by_several_rows(sheet):
    raw = {
        'systems': [
            {
                'code': 'digestiveRenal',
                'status': '亚临床边界',
                'rows': [
                    row('脂肪肝', ['L5']),
                    row('胆囊息肉', ['L5']),
                ],
            }
        ]
    }

    rows = system_of(normalize(raw, sheet), 'digestiveRenal')['rows']

    assert [item['handles'] for item in rows[:2]] == [['L5'], ['L5']]


def test_cluster_without_a_valid_record_is_dropped_and_recorded(sheet):
    raw = {
        'clusters': [
            {
                'name': '悬空集群',
                'priority': 'P1',
                'clinicalHandles': ['L99'],
                'microbiomeHandles': ['L3'],
                'reportedHandles': ['M1'],
            },  # handles of the wrong kind do not count
            {'name': '有效集群', 'priority': 'P1', 'clinicalHandles': ['L3', 'M1']},
            {'name': '', 'clinicalHandles': ['L3']},
        ]
    }

    plan = normalize(raw, sheet)

    assert [cluster['name'] for cluster in plan['clusters']] == ['有效集群']
    assert plan['clusters'][0]['clinicalHandles'] == ['L3']  # M1 is not a clinical handle
    assert {'scope': 'clusters', 'title': '悬空集群', 'reason': 'no_valid_record'} in plan['audit']['dropped']
    assert {'scope': 'clusters', 'title': '', 'reason': 'no_valid_record'} in plan['audit']['dropped']


def test_priority_item_without_a_valid_record_is_dropped(sheet):
    raw = {
        'priorityItems': [
            {'title': '无依据', 'handles': ['L77'], 'days': 7},
            {'title': '有依据', 'handles': ['L4'], 'days': 'abc', 'checks': ['a', '', 'b']},
        ]
    }

    plan = normalize(raw, sheet)

    assert [(item['title'], item['days'], item['checks']) for item in plan['priorityItems']] == [
        ('有依据', None, ['a', 'b'])
    ]
    assert {'scope': 'priorityItems', 'title': '无依据', 'reason': 'no_valid_record'} in plan['audit']['dropped']


# ---- systems ------------------------------------------------------------------------------------------------------


def test_all_five_systems_are_present_in_fixed_order_and_empty_ones_are_not_tested(sheet):
    raw = {
        'systems': [
            {'code': 'hematologyImmune', 'status': '重点关注', 'rows': [row('血象', ['L3'])]},
            {'code': 'cardiometabolic', 'status': '重点关注', 'rows': [row('血脂', ['L3'])]},
        ]
    }

    plan = normalize(raw, sheet, clinical=None)

    assert [system['code'] for system in plan['systems']][:5] == CODES
    assert [system['name'] for system in plan['systems']][:5] == [name for _, name in SYSTEMS]
    assert system_of(plan, 'digestiveRenal')['status'] == '未检测'
    assert system_of(plan, 'cardiometabolic')['rows'][0]['index'] == 1


def test_system_named_like_a_known_system_without_code_is_matched_by_name(sheet):
    raw = {'systems': [{'code': 'foo', 'name': '糖脂代谢', 'status': '高于理想值', 'rows': [row('血脂', ['L3'])]}]}

    plan = normalize(raw, sheet)

    assert system_of(plan, 'cardiometabolic')['rows'][0]['title'] == '血脂'
    assert [system['code'] for system in plan['systems']] == CODES


def test_unknown_extra_system_becomes_other(sheet):
    raw = {'systems': [{'code': 'lung', 'name': '呼吸系统', 'status': '亚临床边界', 'rows': [row('肺结节', ['L5'])]}]}

    plan = normalize(raw, sheet, clinical=None)

    other = plan['systems'][-1]
    assert other['code'] == 'other' and other['name'] == '呼吸系统'
    assert [system['code'] for system in plan['systems'][:5]] == CODES
    assert other['rows'][0]['handles'] == ['L5']


def test_extra_system_without_rows_or_key_records_is_not_listed(sheet):
    raw = {'systems': [{'code': 'lung', 'name': '呼吸系统', 'status': '亚临床边界', 'rows': [row('无效', ['L88'])]}]}

    plan = normalize(raw, sheet)

    assert [system['code'] for system in plan['systems']] == CODES


def test_several_extra_systems_are_merged_into_one_with_the_worst_status(sheet):
    raw = {
        'systems': [
            {
                'code': 'lung',
                'name': '肺',
                'status': '亚临床边界',
                'keyHandles': ['L4'],
                'rows': [row('肺结节', ['L4'])],
            },
            {'code': 'other2', 'name': '乳腺', 'status': '重点关注', 'rows': [row('乳腺结节', ['L5'])]},
            {'code': 'zzz', 'name': '妇科', 'status': '未检测', 'rows': [row('宫颈', ['L2'])]},
        ]
    }

    plan = normalize(raw, sheet)

    others = [system for system in plan['systems'] if system['code'] == 'other']
    assert len(others) == 1
    assert others[0]['name'] == OTHER_SYSTEM_NAME and others[0]['status'] == '重点关注'
    assert [item['title'] for item in others[0]['rows']] == ['肺结节', '乳腺结节', '宫颈']
    assert [item['index'] for item in others[0]['rows']] == [1, 2, 3]


def test_microbiome_system_never_keeps_rows(sheet):
    raw = {
        'systems': [
            {
                'code': 'microbiome',
                'status': '亚临床边界',
                'keyHandles': ['M2', 'L3'],
                'rows': [row('菌群', ['L3']), row('无效', ['L99'])],
            }
        ]
    }

    plan = normalize(raw, sheet)

    micro = system_of(plan, 'microbiome')
    assert micro['rows'] == [] and micro['status'] == '亚临床边界' and micro['keyHandles'] == ['M2', 'L3']
    assert not any(item['scope'] == 'microbiome' for item in plan['audit']['dropped'])


def test_microbiome_system_marked_not_tested_is_corrected_when_the_sheet_has_microbiome(sheet):
    plan = normalize({'systems': [{'code': 'microbiome', 'status': '未检测'}]}, sheet)

    assert system_of(plan, 'microbiome')['status'] == '亚临床边界'
    assert {'scope': 'microbiome', 'reason': 'system_with_records_marked_not_tested'} in plan['audit']['corrections']
    bare = build_sheet(microbiome={})
    assert (
        system_of(normalize({'systems': [{'code': 'microbiome', 'status': '未检测'}]}, bare), 'microbiome')['status']
        == '未检测'
    )


def test_repeated_system_only_adds_rows_on_new_records(sheet):
    raw = {
        'systems': [
            {'code': 'cardiometabolic', 'status': '重点关注', 'rows': [row('血脂', ['L3'])]},
            {'code': 'cardiometabolic', 'status': '高于理想值', 'rows': [row('重复', ['L3']), row('血糖', ['L2'])]},
        ]
    }

    rows = system_of(normalize(raw, sheet), 'cardiometabolic')['rows']

    assert [item['title'] for item in rows if item['origin'] == 'model'] == ['血脂', '血糖']


def test_system_with_rows_but_marked_not_tested_takes_its_worst_row_grade(sheet):
    raw = {
        'systems': [
            {
                'code': 'cardiometabolic',
                'status': '未检测',
                'rows': [row('血糖', ['L2'], '稳健达标'), row('血脂', ['L3'], '高于理想值')],
            }
        ]
    }

    plan = normalize(raw, sheet)

    assert system_of(plan, 'cardiometabolic')['status'] == '亚临床边界'  # worst of its rows, program row of D1 included
    assert {'scope': 'cardiometabolic', 'reason': 'system_with_records_marked_not_tested'} in plan['audit'][
        'corrections'
    ]


def test_system_without_any_usable_row_is_reset_to_not_tested(sheet):
    raw = {'systems': [{'code': 'hematologyImmune', 'status': '重点关注', 'rows': [row('无效', ['L99'])]}]}

    plan = normalize(raw, sheet)

    assert system_of(plan, 'hematologyImmune')['status'] == '未检测'
    assert system_of(plan, 'hematologyImmune')['keyHandles'] == []


def test_missing_key_handles_are_taken_from_the_most_severe_rows_up_to_eight(sheet):
    raw = {
        'systems': [
            {
                'code': 'cardiometabolic',
                'status': '重点关注',
                'rows': [row('血糖', ['L2'], '稳健达标'), row('血脂', ['L3', 'D1'], '重点关注')],
            }
        ]
    }

    system = system_of(normalize(raw, sheet), 'cardiometabolic')

    assert system['keyHandles'][:3] == ['L3', 'D1', 'L2']
    many = {'systems': [{'code': 'cardiometabolic', 'status': '重点关注', 'keyHandles': list(sheet.entries)}]}
    assert len(system_of(normalize(many, sheet), 'cardiometabolic')['keyHandles']) == 8


def test_unreadable_grade_defaults_to_borderline_for_rows_and_not_tested_for_systems(sheet):
    raw = {
        'systems': [
            {'code': 'cardiometabolic', 'status': '不明', 'keyHandles': ['L3'], 'rows': [row('血脂', ['L3'], '很严重')]}
        ]
    }

    system = system_of(normalize(raw, sheet), 'cardiometabolic')

    assert system['rows'][0]['status'] == '亚临床边界'
    assert system['status'] == '亚临床边界'  # not tested is corrected because the system has records


def test_system_row_limit_is_twelve_and_body_row_limit_is_six(sheet):
    raw = {
        'bodyRows': [row(f'体格{n}', ['L1']) for n in range(9)],
        'systems': [
            {'code': 'cardiometabolic', 'status': '亚临床边界', 'rows': [row(f'行{n}', ['L2']) for n in range(20)]}
        ],
    }

    plan = normalize(raw, sheet)

    assert [item['origin'] for item in plan['bodyRows']].count('model') == 6
    assert len([item for item in system_of(plan, 'cardiometabolic')['rows'] if item['origin'] == 'model']) == 12


# ---- program rows -------------------------------------------------------------------------------------------------


def test_unmentioned_abnormal_records_get_program_rows_in_their_system(sheet):
    raw = {
        'systems': [{'code': 'cardiometabolic', 'status': '高于理想值', 'rows': [row('血糖', ['L2'], '高于理想值')]}]
    }

    plan = normalize(raw, sheet)

    cardio = system_of(plan, 'cardiometabolic')['rows']
    digestive = system_of(plan, 'digestiveRenal')['rows']
    assert [(item['title'], item['handles'], item['status'], item['origin']) for item in cardio] == [
        ('血糖', ['L2'], '高于理想值', 'model'),
        ('甘油三酯(TG)', ['L3'], '重点关注', 'program'),  # the view shows it as focus
        ('TyG 指数', ['D1'], '亚临床边界', 'program'),
    ]
    assert [(item['title'], item['status']) for item in digestive] == [
        ('粪便隐血', '重点关注'),
        ('腹部超声', '亚临床边界'),
    ]
    assert [item['index'] for item in digestive] == [1, 2]
    assert system_of(plan, 'digestiveRenal')['status'] == '重点关注'  # was not tested, now carries its worst row
    audit = plan['audit']['programRows']
    assert [(item['handle'], item['scope'], item['reason']) for item in audit] == [
        ('L3', 'cardiometabolic', 'abnormal_record_unmentioned'),
        ('L4', 'digestiveRenal', 'abnormal_record_unmentioned'),
        ('L5', 'digestiveRenal', 'abnormal_record_unmentioned'),
        ('D1', 'cardiometabolic', 'abnormal_record_unmentioned'),
    ]


def test_program_row_carries_the_checked_target_as_ideal():
    facts = [fact('fact-glu', '空腹葡萄糖(Glu)', '7.2', reference_range='3.89-6.11', abnormal=True)]
    sheet = build_sheet(facts=facts, microbiome={})

    plan = normalize({}, sheet)

    program = system_of(plan, 'cardiometabolic')['rows'][0]
    assert program['ideal'] == '<5.6 mmol/L' and program['origin'] == 'program'


def test_abnormal_body_record_goes_to_the_body_rows():
    facts = default_facts()
    facts[0] = fact('fact-bmi', '体重指数(BMI)', '29.3', unit='kg/m2', abnormal=True)
    sheet = build_sheet(facts=facts)

    plan = normalize({}, sheet)

    assert [(item['title'], item['origin'], item['status']) for item in plan['bodyRows']] == [
        ('体重指数(BMI)', 'program', '亚临床边界')
    ]
    assert plan['audit']['programRows'][0]['scope'] == 'body'


def test_record_without_a_known_group_goes_to_the_other_system(sheet):
    plan = normalize({}, sheet, clinical=None)

    other = plan['systems'][-1]
    assert other['code'] == 'other' and other['name'] == OTHER_SYSTEM_NAME
    assert [item['handles'] for item in other['rows']] == [['L3'], ['L4'], ['L5'], ['D1']]
    assert other['status'] == '重点关注'
    assert [system['status'] for system in plan['systems'][:5]] == [
        '未检测',
        '未检测',
        '亚临床边界',
        '未检测',
        '未检测',
    ]  # only the microbiome system has records (chapter four)


def test_record_mentioned_only_in_a_data_note_counts_as_covered(sheet):
    raw = {'dataNotes': [{'kind': 'missing', 'note': '该项已核对', 'handles': ['L3', 'L99']}]}

    plan = normalize(raw, sheet)

    assert 'L3' not in [item['handle'] for item in plan['audit']['programRows']]
    assert plan['dataNotes'][0]['handles'] == ['L3']


def test_a_focus_view_row_needs_a_row_even_when_the_fact_is_not_flagged_abnormal():
    facts = [fact('fact-glu', '空腹葡萄糖(Glu)', '5.74')]
    clinical = default_clinical()
    clinical['clinicalGroups'][0]['rows'][0]['level'] = 'focus'
    sheet = build_sheet(facts=facts, clinical=clinical, microbiome={})

    plan = normalize({}, sheet, clinical=clinical)

    assert system_of(plan, 'cardiometabolic')['rows'][0]['status'] == '重点关注'


def test_normal_records_and_advice_get_no_program_rows():
    facts = [fact('f-1', '空腹葡萄糖', '5.1'), fact('c-1', '主检结论：异常', '复查', section='主检', abnormal=True)]
    sheet = build_sheet(facts=facts, clinical={}, microbiome={})

    assert normalize({}, sheet, clinical=None)['audit']['programRows'] == []


def test_clinical_fact_groups_reads_span_codes_and_body_metrics():
    groups, body = clinical_fact_groups(default_clinical())

    assert groups['fact-us'] == 'organMetabolism' and groups['fact-glu'] == 'cardiometabolic'
    assert body == {'fact-bmi'}
    assert clinical_fact_groups(None) == ({}, set())


# ---- grade corrections --------------------------------------------------------------------------------------------


def test_robust_row_citing_a_focus_record_is_corrected_and_recorded(sheet):
    raw = {
        'systems': [
            {
                'code': 'cardiometabolic',
                'status': '重点关注',
                'rows': [row('血脂', ['L2', 'L3'], '稳健达标'), row('血糖', ['L2'], '稳健达标')],
            }
        ]
    }

    plan = normalize(raw, sheet)

    rows = system_of(plan, 'cardiometabolic')['rows']
    assert [item['status'] for item in rows[:2]] == ['亚临床边界', '稳健达标']
    assert {
        'scope': 'cardiometabolic',
        'title': '血脂',
        'handle': 'L3',
        'reason': 'record_outside_reference_shown_as_normal',
    } in plan['audit']['corrections']


def test_robust_body_row_is_corrected_too_and_other_grades_are_untouched(sheet):
    clinical = default_clinical()
    clinical['bodyMetrics'][0]['level'] = 'focus'
    raw = {'bodyRows': [row('体重', ['L1'], '稳健达标'), row('体重2', ['L1'], '高于理想值')]}

    plan = normalize(raw, sheet, clinical=clinical)
    plan2 = normalize_case_analysis(raw, build_sheet(clinical=clinical), clinical=clinical)

    assert [item['status'] for item in plan['bodyRows']] == ['稳健达标', '高于理想值']  # sheet rows say borderline
    assert [item['status'] for item in plan2['bodyRows']] == ['亚临床边界', '高于理想值']
    assert [item['scope'] for item in plan2['audit']['corrections'] if item.get('handle')] == ['body']


def test_advice_records_do_not_trigger_the_grade_correction(sheet):
    raw = {'systems': [{'code': 'cardiometabolic', 'status': '稳健达标', 'rows': [row('结论', ['A1'], '稳健达标')]}]}

    plan = normalize(raw, sheet)

    assert system_of(plan, 'cardiometabolic')['rows'][0]['status'] == '稳健达标'
    assert not any(
        item['reason'] == 'record_outside_reference_shown_as_normal' for item in plan['audit']['corrections']
    )


# ---- clusters, priority items, lifestyle --------------------------------------------------------------------------


def clusters_raw():
    return [
        {'name': '乙', 'priority': 'P2', 'clinicalHandles': ['L2']},
        {'name': '甲', 'priority': 'P1', 'clinicalHandles': ['L3']},
        {'name': '丙', 'priority': 'p0 级', 'clinicalHandles': ['L4']},
        {'name': '丁', 'priority': 'oops', 'reportedHandles': ['Q1']},
        {'name': '戊', 'priority': 'P1', 'microbiomeHandles': ['M2']},
    ]


def test_clusters_are_ordered_by_priority_stably_and_renumbered(sheet):
    plan = normalize({'clusters': clusters_raw()}, sheet)

    assert [(c['id'], c['index'], c['name'], c['priority']) for c in plan['clusters']] == [
        ('C1', 1, '丙', 'P0'),
        ('C2', 2, '甲', 'P1'),
        ('C3', 3, '戊', 'P1'),
        ('C4', 4, '乙', 'P2'),
        ('C5', 5, '丁', 'P2'),
    ]
    assert all('source' not in cluster for cluster in plan['clusters'])


def test_cluster_limit_is_six(sheet):
    raw = {'clusters': [{'name': f'集群{n}', 'priority': 'P2', 'clinicalHandles': ['L3']} for n in range(9)]}

    assert len(normalize(raw, sheet)['clusters']) == 6


def test_priority_item_promotes_its_cluster_and_links_follow_the_renumbering(sheet):
    raw = {
        'clusters': [
            {'name': '乙', 'priority': 'P1', 'clinicalHandles': ['L3']},
            {'name': '甲', 'priority': 'P2', 'clinicalHandles': ['L4']},
        ],
        'priorityItems': [{'title': '隐血', 'handles': ['L4'], 'cluster': '2'}],
        'lifestyle': [{'dimension': 'sleep', 'handles': ['Q1'], 'clusters': [1, 2, 'x', 7, 2]}],
    }

    plan = normalize(raw, sheet)

    assert [(c['id'], c['name'], c['priority']) for c in plan['clusters']] == [('C1', '甲', 'P0'), ('C2', '乙', 'P1')]
    assert plan['priorityItems'][0]['clusterId'] == 'C1'
    assert plan['lifestyle'][0]['clusterIds'] == ['C2', 'C1']
    assert {'scope': 'clusters', 'title': '甲', 'reason': 'cluster_of_priority_item_promoted'} in plan['audit'][
        'corrections'
    ]


def test_priority_item_without_a_cluster_number_finds_the_cluster_by_shared_record(sheet):
    raw = {
        'clusters': [
            {'name': '乙', 'priority': 'P2', 'clinicalHandles': ['L3']},
            {'name': '甲', 'priority': 'P2', 'clinicalHandles': ['L4', 'L5']},
        ],
        'priorityItems': [
            {'title': '甲事项', 'handles': ['Q1', 'L5']},
            {'title': '无集群', 'handles': ['L1'], 'cluster': 9},
        ],
    }

    plan = normalize(raw, sheet)

    assert [(item['title'], item['clusterId']) for item in plan['priorityItems']] == [
        ('甲事项', 'C1'),
        ('无集群', None),
    ]
    assert plan['clusters'][0]['name'] == '甲' and plan['clusters'][0]['priority'] == 'P0'


def test_priority_items_are_limited_and_indexed(sheet):
    raw = {'priorityItems': [{'title': f'事项{n}', 'handles': ['L4']} for n in range(8)]}

    items = normalize(raw, sheet)['priorityItems']

    assert [item['index'] for item in items] == [1, 2, 3, 4, 5]


def test_lifestyle_rows_are_merged_per_dimension_in_canonical_order(sheet):
    raw = {
        'clusters': [{'name': '甲', 'priority': 'P1', 'clinicalHandles': ['L3']}],
        'lifestyle': [
            {'dimension': 'exercise', 'handles': ['Q2'], 'clusters': [1]},
            {'dimension': 'diet', 'handles': ['Q1', 'L3'], 'clusters': []},
            {'dimension': 'exercise', 'handles': ['Q2', 'Q3'], 'clusters': ['1', 1]},
            {'dimension': 'sleep', 'handles': ['M1']},  # microbiome is no lifestyle evidence
            {'dimension': 'bogus', 'handles': ['Q1']},
        ],
    }

    lifestyle = normalize(raw, sheet)['lifestyle']

    assert [(row['dimension'], row['handles'], row['clusterIds'], row['index']) for row in lifestyle] == [
        ('diet', ['Q1', 'L3'], [], 1),
        ('exercise', ['Q2', 'Q3'], ['C1'], 2),
    ]
    assert lifestyle[0]['name'] == '膳食结构与餐桌模式'


def test_symptoms_need_a_questionnaire_handle(sheet):
    raw = {
        'symptoms': [
            {
                'name': '乏力',
                'handles': ['Q2', 'L3'],
                'clinicalHandles': ['L3', 'M1', 'Q1'],
                'microbiomeHandles': ['M2', 'L1'],
            },
            {'name': '由检查反推', 'handles': ['L3']},
            {'name': '', 'handles': ['Q1']},
        ]
    }

    plan = normalize(raw, sheet)

    assert plan['symptoms'] == [
        {
            'index': 1,
            'code': 'S1',
            'name': '乏力',
            'handles': ['Q2'],
            'clinicalHandles': ['L3'],
            'microbiomeHandles': ['M2'],
        }
    ]
    assert plan['audit']['dropped'] == [
        {'scope': 'symptoms', 'title': '由检查反推', 'reason': 'not_reported_by_subject'}
    ]


def test_symptom_limit_is_five(sheet):
    raw = {'symptoms': [{'name': f'不适{n}', 'handles': ['Q1']} for n in range(8)]}

    assert [item['code'] for item in normalize(raw, sheet)['symptoms']] == ['S1', 'S2', 'S3', 'S4', 'S5']


def test_test_gap_naming_a_recorded_test_is_dropped(sheet):
    raw = {
        'testGaps': [
            {'test': '空腹葡萄糖(Glu)', 'because': '重复', 'handles': ['L2']},
            {'test': '空腹 葡萄糖（glu）', 'because': '写法不同'},
            {'test': '糖耐量试验', 'because': '空腹血糖接近上限', 'handles': ['L2', 'L99']},
            {'test': '', 'because': '无名'},
        ]
    }

    plan = normalize(raw, sheet)

    assert plan['testGaps'] == [{'index': 1, 'test': '糖耐量试验', 'because': '空腹血糖接近上限', 'handles': ['L2']}]
    reasons = [item['reason'] for item in plan['audit']['dropped'] if item['scope'] == 'testGaps']
    assert reasons == ['test_already_recorded', 'test_already_recorded']


def test_list_limits_hold(sheet):
    raw = {
        'followups': [{'title': f'随访{n}', 'handles': ['L3'], 'checks': list('abcdef')} for n in range(9)],
        'testGaps': [{'test': f'检查{n}'} for n in range(9)],
        'recognitions': [{'title': f'校准{n}'} for n in range(7)],
        'dataNotes': [{'note': f'说明{n}', 'kind': 'weird'} for n in range(9)],
    }

    plan = normalize(raw, sheet)

    assert len(plan['followups']) == 6 and len(plan['followups'][0]['checks']) == 4
    assert [item['index'] for item in plan['followups']] == [1, 2, 3, 4, 5, 6]
    assert len(plan['testGaps']) == 6 and len(plan['recognitions']) == 4 and len(plan['dataNotes']) == 6
    assert {note['kind'] for note in plan['dataNotes']} == {'calibration'}


def test_followup_without_a_record_is_dropped(sheet):
    raw = {
        'followups': [{'title': '无依据', 'handles': ['L99']}, {'title': '有依据', 'handles': ['L5'], 'days': 99999}]
    }

    followups = normalize(raw, sheet)['followups']

    assert [(item['title'], item['days']) for item in followups] == [('有依据', None)]


def test_plan_statistics_summarises_the_plan(sheet):
    stats = plan_statistics(normalize(raw_analysis(sheet), sheet))

    assert stats['systems'][0] == ('cardiometabolic', '重点关注', 2 + 0)
    assert [item[0] for item in stats['systems']] == CODES
    assert stats['bodyRows'] == 1 and stats['symptoms'] == 1 and stats['followups'] == 1 and stats['testGaps'] == 1
    assert stats['clusters'] == [('C1', 'P0'), ('C2', 'P1')]
    assert stats['priorityItems'] == [('粪便隐血查因', 'model')]
    assert stats['lifestyle'] == ['sleep'] and stats['recognitions'] == 1
    assert (stats['dropped'], stats['corrections'], stats['programRows']) == (0, 1, 0)


# ---- safety net ---------------------------------------------------------------------------------------------------


def hit(rule_id, fact_ids, *, days=None, text=None, title='规则命中', department='消化内科', sources=None):
    return {
        'ruleId': rule_id,
        'title': title,
        'factIds': fact_ids,
        'timeframe': {'text': text, 'days': days},
        'department': department,
        'checks': ['结肠镜'],
        'sources': sources or [{'key': 'k', 'title': '指南'}],
    }


def planned(sheet, **extra):
    raw = {
        'clusters': [
            {'name': '糖脂', 'priority': 'P1', 'clinicalHandles': ['L3']},
            {'name': '消化道', 'priority': 'P2', 'clinicalHandles': ['L4']},
        ],
        'priorityItems': [{'title': '血脂', 'handles': ['L3'], 'days': 90, 'timeframe': '3 个月内', 'cluster': 1}],
        'lifestyle': [{'dimension': 'diet', 'handles': ['Q1'], 'clusters': [1, 2]}],
        **extra,
    }
    return normalize(raw, sheet)


def test_rule_hit_without_a_shared_record_is_appended_and_promotes_its_cluster(sheet):
    plan = planned(sheet)
    assert [(c['id'], c['name'], c['priority']) for c in plan['clusters']] == [
        ('C1', '糖脂', 'P0'),
        ('C2', '消化道', 'P2'),
    ]

    audit = apply_priority_safety_net(
        plan, sheet, {'priorityItems': [hit('RF-FOBT', ['fact-fobt'], days=14, text='2 周内')]}
    )

    added = plan['priorityItems'][1]
    assert added['origin'] == 'safety_net' and added['index'] == 2 and added['handles'] == ['L4']
    assert added['ruleIds'] == ['RF-FOBT'] and added['days'] == 14 and added['timeframe'] == '2 周内'
    assert added['sources'] == [{'key': 'k', 'title': '指南'}]
    assert [(c['id'], c['priority']) for c in plan['clusters']] == [('C1', 'P0'), ('C2', 'P0')]
    assert added['clusterId'] == 'C2'
    assert audit == [{'action': 'priority_item_added', 'title': '规则命中', 'ruleId': 'RF-FOBT', 'handles': ['L4']}]
    assert plan['audit']['safetyNet'] == audit


def test_promotion_renumbers_clusters_items_and_lifestyle_links(sheet):
    raw = {
        'clusters': [
            {'name': '甲', 'priority': 'P1', 'clinicalHandles': ['L3']},
            {'name': '乙', 'priority': 'P2', 'clinicalHandles': ['L4']},
            {'name': '丙', 'priority': 'P2', 'clinicalHandles': ['L5']},
        ],
        'priorityItems': [{'title': '甲事项', 'handles': ['L3'], 'cluster': 1}],
        'lifestyle': [{'dimension': 'diet', 'handles': ['Q1'], 'clusters': [1, 2, 3]}],
    }
    plan = normalize(raw, sheet)
    assert [c['name'] for c in plan['clusters']] == ['甲', '乙', '丙']

    apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-US', ['fact-us'], days=30)]})

    assert [(c['id'], c['name'], c['priority'], c['index']) for c in plan['clusters']] == [
        ('C1', '甲', 'P0', 1),
        ('C2', '丙', 'P0', 2),
        ('C3', '乙', 'P2', 3),
    ]
    assert [(item['title'], item['clusterId']) for item in plan['priorityItems']] == [
        ('甲事项', 'C1'),
        ('规则命中', 'C2'),
    ]
    assert plan['lifestyle'][0]['clusterIds'] == ['C1', 'C2', 'C3']


def test_hit_without_any_cluster_is_added_unlinked(sheet):
    plan = normalize({}, sheet)

    apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-FOBT', ['fact-fobt'], days=7)]})

    assert plan['priorityItems'][0]['clusterId'] is None and plan['clusters'] == []


def test_hit_overlapping_a_model_item_adds_rule_ids_and_tightens_the_time_limit(sheet):
    plan = planned(sheet)

    audit = apply_priority_safety_net(
        plan, sheet, {'priorityItems': [hit('RF-LIPID', ['fact-tg'], days=30, text='1 个月内', sources=[{'key': 'a'}])]}
    )

    item = plan['priorityItems'][0]
    assert len(plan['priorityItems']) == 1 and item['origin'] == 'model'
    assert (item['days'], item['timeframe'], item['ruleIds'], item['sources']) == (
        30,
        '1 个月内',
        ['RF-LIPID'],
        [{'key': 'a'}],
    )
    assert audit == [{'action': 'timeframe_tightened', 'title': '血脂', 'ruleId': 'RF-LIPID', 'from': 90, 'to': 30}]


def test_hit_never_loosens_the_time_limit_but_still_adds_its_sources(sheet):
    plan = planned(sheet)

    audit = apply_priority_safety_net(
        plan,
        sheet,
        {
            'priorityItems': [
                hit('RF-A', ['fact-tg'], days=180, text='半年内'),
                hit('RF-B', ['fact-tg'], days=90, text='3 个月内'),
            ]
        },
    )

    item = plan['priorityItems'][0]
    assert (item['days'], item['timeframe']) == (90, '3 个月内')
    assert item['ruleIds'] == ['RF-A', 'RF-B'] and len(item['sources']) == 2
    assert audit == []


def test_hit_gives_a_model_item_without_days_the_rule_limit(sheet):
    raw = {'priorityItems': [{'title': '隐血', 'handles': ['L4']}]}
    plan = normalize(raw, sheet)

    apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-1', ['fact-fobt'], days=14, text='2 周内')]})

    assert (plan['priorityItems'][0]['days'], plan['priorityItems'][0]['timeframe']) == (14, '2 周内')


def test_hit_without_numeric_days_keeps_the_model_limit(sheet):
    plan = planned(sheet)

    apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-1', ['fact-tg'], days=None, text='尽快')]})

    assert (plan['priorityItems'][0]['days'], plan['priorityItems'][0]['timeframe']) == (90, '3 个月内')


def test_hits_whose_facts_are_not_on_the_sheet_are_ignored(sheet):
    plan = planned(sheet)
    before = deepcopy(plan['priorityItems'])

    audit = apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-X', ['nope', 'fact-missing'], days=1)]})

    assert audit == [] and plan['priorityItems'] == before


def test_hit_on_a_microbiome_fact_uses_the_microbiome_handle(sheet):
    plan = normalize({}, sheet)

    apply_priority_safety_net(plan, sheet, {'priorityItems': [hit('RF-M', ['m-akk'], days=30)]})

    assert plan['priorityItems'][0]['handles'] == ['M2']


def test_missing_flags_are_accepted(sheet):
    for flags in (None, {}, {'priorityItems': None}):
        plan = planned(sheet)
        assert apply_priority_safety_net(plan, sheet, flags) == []
        assert plan['audit']['safetyNet'] == []


def test_routine_followup_hits_add_sources_to_a_matching_followup_only(sheet):
    raw = {'followups': [{'title': '胆囊息肉', 'handles': ['L5']}, {'title': '血脂', 'handles': ['L3']}]}
    plan = normalize(raw, sheet)

    apply_priority_safety_net(
        plan,
        sheet,
        {
            'routineFollowups': [
                hit('RF-GB', ['fact-us'], sources=[{'key': 'gb'}]),
                hit('RF-NONE', ['fact-fobt']),
                hit('RF-EMPTY', []),
            ]
        },
    )

    assert plan['followups'][0]['ruleIds'] == ['RF-GB'] and plan['followups'][0]['sources'] == [{'key': 'gb'}]
    assert 'ruleIds' not in plan['followups'][1]
    assert len(plan['followups']) == 2  # a routine hit never adds a follow-up by itself


def test_safety_net_does_not_share_source_lists_with_the_flags(sheet):
    plan = planned(sheet)
    flags = {'priorityItems': [hit('RF-LIPID', ['fact-tg'], days=30, sources=[{'key': 'a'}])]}

    apply_priority_safety_net(plan, sheet, flags)
    plan['priorityItems'][0]['sources'][0]['key'] = 'changed'

    assert flags['priorityItems'][0]['sources'][0]['key'] == 'a'


def test_helper_lookups_are_consistent_with_the_fixture(sheet):
    assert handle_of(sheet, 'fact-fobt') == 'L4' and handle_named(sheet, '睡眠情况') == 'Q1'
