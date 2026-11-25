"""Whole-case fact sheet: handles, questionnaire flattening, view rows, missing material and number forms."""

from types import SimpleNamespace

from modules.report.assessment_case_sheet import HANDLE
from modules.report.assessment_case_sheet import build_case_sheet
from modules.report.assessment_case_sheet import number_forms
from modules.report.assessment_case_sheet import text_numbers
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_facts
from tests.unit._support.assessment_case import default_microbiome
from tests.unit._support.assessment_case import document
from tests.unit._support.assessment_case import fact
from tests.unit._support.assessment_case import handle_named
from tests.unit._support.assessment_case import handle_of
from tests.unit._support.assessment_case import parsed
from tests.unit._support.assessment_case import request
from tests.unit._support.assessment_case import survey
from tests.unit._support.assessment_case import view_row


def kinds(sheet):
    return {handle: entry['kind'] for handle, entry in sheet.entries.items()}


def test_handles_follow_record_kind():
    sheet = build_sheet()

    assert kinds(sheet) == {
        'L1': 'clinical',
        'L2': 'clinical',
        'L3': 'clinical',
        'L4': 'clinical',
        'L5': 'clinical',
        'D1': 'derived',
        'A1': 'advice',
        'Q1': 'reported',
        'Q2': 'reported',
        'Q3': 'reported',
        'M1': 'microbiome',
        'M2': 'microbiome',
        'M3': 'microbiome',
    }
    assert sheet.entry('L3')['factId'] == 'fact-tg'
    assert sheet.entry('D1')['factId'] == 'derived-fact-tyg'
    assert sheet.entry('M2')['name'] == '阿克曼菌' and sheet.entry('M2')['abnormal'] is True
    assert sheet.entry('M1')['abnormal'] is False
    assert sheet.entry('X9') is None
    assert sheet.handles('microbiome') == ['M1', 'M2', 'M3']
    assert sheet.has_clinical and sheet.has_questionnaire and sheet.has_microbiome
    assert 'L3\t甘油三酯(TG)\t3.34\tmmol/L\t0.00-1.70\t偏高\t血脂7项\t2026-06-30' in sheet.text
    assert all(HANDLE.fullmatch(handle) for handle in sheet.entries)


def test_recorded_conclusion_is_marked_by_section_or_by_name_prefix():
    facts = [
        fact('c-1', '主检结论：血脂偏高', '建议复查', section='主检'),
        fact('c-2', '总检结论：肝脏超声异常', '专科随诊', section='一般检查'),  # prefix only
        fact('c-3', '建议清淡饮食', '少油', section='总结与建议'),  # section only
        fact('c-4', '空腹葡萄糖', '5.1', section='糖代谢'),
    ]
    sheet = build_sheet(facts=facts, clinical={}, microbiome={})

    assert kinds(sheet)['A1'] == kinds(sheet)['A2'] == kinds(sheet)['A3'] == 'advice'
    assert [sheet.entry(handle)['name'] for handle in ('A1', 'A2', 'A3')] == [
        '血脂偏高',
        '肝脏超声异常',
        '建议清淡饮食',
    ]  # the recorded-conclusion prefix is removed
    assert '主检结论' not in sheet.text and '总检结论' not in sheet.text
    assert sheet.entry('L1')['name'] == '空腹葡萄糖'


def test_derived_prefix_wins_over_conclusion_section():
    facts = [fact('derived-fact-x', '主检综合指数', '3', section='主检', source='检查类别：主检；算式 a+b')]

    sheet = build_sheet(facts=facts, clinical={}, microbiome={}, req=request(surveys=[]))

    assert kinds(sheet) == {'D1': 'derived'}
    assert sheet.entry('D1')['formula'].startswith('检查类别')
    assert sheet.has_clinical


def test_non_clinical_facts_never_get_clinical_handles():
    facts = [
        fact('m-1', '某菌', '1', kind='microbe'),
        fact('f-1', '某功能', '2', kind='function'),
        fact('x-1', '背景', '3', kind='context'),
    ]

    sheet = build_sheet(facts=facts, clinical={}, microbiome={}, req=request(surveys=[]))

    assert not sheet.handles()  # microbiome records come from the view, not from raw facts
    assert not sheet.has_clinical


def test_questionnaire_flattening_maps_choices_and_joins_lists():
    sheet = build_sheet(facts=[], clinical={}, microbiome={})

    rows = {entry['name']: entry['value'] for entry in sheet.entries.values()}
    assert rows == {'睡眠情况': '入睡困难', '近期不适': '乏力、腹胀', '家族史': '父亲糖尿病'}  # empty answer skipped
    assert '## 健康问卷' in sheet.text
    assert sheet.has_questionnaire and not sheet.has_clinical


def test_questionnaire_accepts_answers_wrapper_dict_surveys_and_submitted_time():
    wrapped = survey(answer={'answers': {'q_sleep': 'a'}}, submitted='2026-07-01')
    as_dict = {
        'schema': {'title': '二号问卷', 'pages': [{'elements': [{'name': 'k', 'title': '吸烟'}]}]},
        'answer': {'k': ['从不']},
        'submittedAt': '2026-07-02',
    }

    sheet = build_sheet(facts=[], clinical={}, microbiome={}, req=request(surveys=[wrapped, as_dict]))

    assert [(e['section'], e['name'], e['value']) for e in sheet.entries.values()] == [
        ('健康问卷（提交时间 2026-07-01）', '睡眠情况', '睡眠充足'),
        ('二号问卷（提交时间 2026-07-02）', '吸烟', '从不'),
    ]
    assert '## 二号问卷（提交时间 2026-07-02）' in sheet.text


def test_questionnaire_without_survey_list_reads_init_survey_attributes():
    req = SimpleNamespace(init_survey_schema=survey().schema_, init_survey_answer=survey().answer)

    sheet = build_sheet(facts=[], clinical={}, microbiome={}, req=req)

    assert sorted(entry['name'] for entry in sheet.entries.values()) == ['家族史', '睡眠情况', '近期不适']


def test_empty_survey_list_does_not_fall_back_to_init_attributes():
    req = SimpleNamespace(survey_list=[], init_survey_schema=survey().schema_, init_survey_answer=survey().answer)

    sheet = build_sheet(facts=[], clinical={}, microbiome={}, req=req)

    assert not sheet.has_questionnaire
    assert '本次没有提供问卷或本人自述。' in sheet.text


def test_without_a_survey_self_reported_facts_are_used():
    facts = [
        fact('r-1', '近期不适', '乏力', kind='self_reported'),
        fact('r-2', '既往史', '高血压', kind='self_reported', reported_assertion_kind='history'),
    ]

    sheet = build_sheet(facts=facts, clinical={}, microbiome={}, req=request(no_survey_attrs=True))

    assert [(e['name'], e['value'], e['factId']) for e in sheet.entries.values()] == [('近期不适', '乏力', 'r-1')]
    assert sheet.has_questionnaire


def test_survey_answers_reuse_the_fact_id_of_an_identical_self_reported_fact():
    facts = [fact('r-1', '睡眠情况', '入睡困难', kind='self_reported')]

    sheet = build_sheet(facts=facts, clinical={}, microbiome={})

    assert sheet.entry('Q1')['factId'] == 'r-1'
    assert sheet.entry('Q2')['factId'] is None


def test_missing_material_is_stated_and_flagged():
    sheet = build_sheet(facts=[], clinical={}, microbiome={}, req=request(surveys=[]))

    assert not (sheet.has_clinical or sheet.has_questionnaire or sheet.has_microbiome)
    assert sheet.entries == {}
    assert '本次没有提供体检或检验资料。' in sheet.text
    assert '本次没有提供问卷或本人自述。' in sheet.text
    assert '本次没有提供肠道菌群检测结果。' in sheet.text


def test_microbiome_missing_only_states_the_microbiome():
    sheet = build_sheet(microbiome={})

    assert sheet.has_clinical and sheet.has_questionnaire and not sheet.has_microbiome
    assert '本次没有提供肠道菌群检测结果。' in sheet.text
    assert '本次没有提供体检或检验资料。' not in sheet.text


def test_conclusions_alone_do_not_count_as_clinical_data():
    facts = [fact('c-1', '主检结论：正常', '无', section='主检')]

    sheet = build_sheet(facts=facts, clinical={}, microbiome={})

    assert not sheet.has_clinical
    assert 'A1' in sheet.entries
    assert '本次没有提供体检或检验资料。' not in sheet.text  # the report has a conclusion section already


def test_view_rows_attach_to_their_fact_including_span_rows():
    sheet = build_sheet()

    assert [row['code'] for row in sheet.entry(handle_of(sheet, 'fact-us'))['rows']] == [
        'fact-us:span:a:b',
        'fact-us:span:c:d',
        'fact-us:span:e:f',
    ]
    assert [row['code'] for row in sheet.entry('L3')['rows']] == ['fact-tg']
    assert sheet.entry('L1')['rows'][0]['name'] == '体重指数(BMI)'  # body metrics are attached too
    assert sheet.entry('A1')['rows'] == []


def test_same_row_object_in_two_views_is_attached_once():
    clinical = default_clinical()
    clinical['bodyGroups'] = [{'rows': [clinical['bodyMetrics'][0]]}]

    sheet = build_sheet(clinical=clinical)

    assert len(sheet.entry('L1')['rows']) == 1


def test_checked_target_is_listed_in_its_own_section():
    sheet = build_sheet()

    assert sheet.entry('L2')['target'] == '<5.6 mmol/L'
    section = sheet.text.split('# 已核对出处的理想目标（有这些项目时优先采用）\n', 1)[1].split('\n\n', 1)[0]
    assert section == 'L2\t空腹葡萄糖(Glu)\t<5.6 mmol/L'
    assert sheet.entry('L3')['target'] == ''


def test_no_target_section_when_no_row_has_a_target():
    clinical = default_clinical()
    clinical['clinicalGroups'][0]['rows'][0].pop('target')

    assert '已核对出处的理想目标' not in build_sheet(clinical=clinical).text


def test_subject_and_timing_header():
    timing = {'clinicalDate': '2026-06-30', 'microbiomeReceivedDate': '2026-11-02', 'daysBetween': 125}

    sheet = build_sheet(timing=timing)

    assert sheet.subject == {'sex': '男', 'age': '48'}
    assert '性别：男\t年龄：48' in sheet.text
    assert '资料日期：临床体检日期 2026-06-30；菌群收样日期 2026-11-02（采样日期未注明）；两者相隔 125 天' in sheet.text


def test_subject_falls_back_to_request_and_marks_unknown():
    sheet = build_case_sheet(
        request=request(surveys=[], sex='女', age=39),
        parsed=SimpleNamespace(subject=None),
        document=document(),
        clinical={},
        microbiome={},
    )
    unknown = build_case_sheet(
        request=request(surveys=[]),
        parsed=SimpleNamespace(subject=None),
        document=document(),
        clinical={},
        microbiome={},
    )

    assert sheet.subject == {'sex': '女', 'age': '39'}
    assert unknown.subject == {'sex': None, 'age': None}
    assert '性别：未记录\t年龄：未记录' in unknown.text


def test_microbiome_block_header_notes_and_normal_members():
    sheet = build_sheet(disease_models='肠癌模型：相似度 0.3')

    assert '# 肠道菌群宏基因组检测（样本 粪便；采样日期 2026-11-02）' in sheet.text
    assert '参考范围内（共 1 项）：双歧杆菌 2.0%' in sheet.text
    assert '## 菌群疾病关联模型（相似度指数，不是患病概率）\n肠癌模型：相似度 0.3' in sheet.text
    assert not any(entry['name'] == '双歧杆菌' for entry in sheet.entries.values())  # normal members have no handle


def test_microbiome_rows_repeated_in_two_groups_get_one_handle():
    microbiome = default_microbiome()
    microbiome['structure'] = [view_row('m-shannon', '香农多样性', '3.2')]
    microbiome['structureGroups'][0]['rows'].append(view_row('dup', '香农多样性', '3.2'))

    sheet = build_sheet(microbiome=microbiome)

    assert [entry['name'] for entry in sheet.entries.values() if entry['kind'] == 'microbiome'].count('香农多样性') == 1


def test_microbiome_status_mark_falls_back_to_direction_arrows():
    microbiome = default_microbiome()
    microbiome['functionalGroups'][0]['rows'] = [
        view_row('m-1', '通路甲', '10', direction='down'),
        view_row('m-2', '通路乙', '90', direction='up'),
        view_row('m-3', '参考', 'x' * 5, reference='r' * 120),
    ]

    sheet = build_sheet(microbiome=microbiome)

    assert sheet.entry(handle_named(sheet, '通路甲'))['status'] == '↓'
    assert sheet.entry(handle_named(sheet, '通路乙'))['status'] == '↑'
    assert sheet.entry(handle_named(sheet, '参考'))['reference'] == 'r' * 90 + '…'


def test_valid_keeps_order_drops_unknown_and_duplicate_handles():
    sheet = build_sheet()

    assert sheet.valid(['L3', 'X1', 'L1', 'L3', ' D1 ', None, 'M2']) == ['L3', 'L1', 'D1', 'M2']
    assert sheet.valid(None) == []
    assert sheet.valid(['L3', 'D1', 'A1', 'Q1', 'M1'], ('clinical', 'derived')) == ['L3', 'D1']
    assert sheet.valid(['Q1', 'L1'], ('reported',)) == ['Q1']
    assert sheet.valid(['L1', 'L1', 'L1']) == ['L1']


def test_sheet_numbers_cover_equivalent_spellings():
    sheet = build_sheet()

    for spelling in ('24.00', '24.0', '24'):
        assert spelling in text_numbers('胰岛素 24.00')
    assert {'3.34', '5.74', '0', '1.7', '29.3', '8.9', '48'} <= sheet.numbers
    assert '1.70' in sheet.numbers and '1.7' in sheet.numbers  # printed range 0.00-1.70
    assert '0.00' in sheet.numbers and '0.0' in sheet.numbers
    assert '5' in sheet.numbers and '5.0' in sheet.numbers  # integers also carry a ".0" spelling (5mm)


def test_number_forms():
    assert number_forms('24.00') == {'24.00', '24', '24.0'}
    assert number_forms('7') == {'7', '7.0'}
    assert number_forms('0.50') == {'0.50', '0.5'}
    assert text_numbers(None) == set()


def test_default_facts_fixture_has_the_expected_shape():
    assert [item.fact_id for item in default_facts()][:2] == ['fact-bmi', 'fact-glu']
    assert parsed().subject.sex == '男'
