"""Regressions of the whole-case path found by the first unit-test pass and persona runs."""

import asyncio
import time
from types import SimpleNamespace

from modules.report.assessment_case_authoring import clean_cell_text
from modules.report.assessment_case_authoring import review_case
from modules.report.assessment_case_authoring import unverified_numbers
from modules.report.assessment_case_plan import apply_priority_safety_net
from modules.report.assessment_case_plan import normalize_case_analysis
from modules.report.assessment_case_projection import build_case_presentation
from modules.report.assessment_case_report import FORMER_PATH_REASONS
from modules.report.assessment_case_report import case_audit_summary
from modules.report.assessment_case_sheet import CaseSheet
from modules.report.assessment_case_sheet import text_numbers
from tests.unit._support.assessment_case import FakeClient
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_facts
from tests.unit._support.assessment_case import default_microbiome
from tests.unit._support.assessment_case import document
from tests.unit._support.assessment_case import handle_named
from tests.unit._support.assessment_case import handle_of
from tests.unit._support.assessment_case import parsed
from tests.unit._support.assessment_case import raw_analysis
from tests.unit._support.assessment_case import request


def _project(raw, cells=None, flags=None):
    clinical, microbiome = default_clinical(), default_microbiome()
    sheet = build_sheet(clinical=clinical, microbiome=microbiome)
    plan = normalize_case_analysis(raw(sheet) if callable(raw) else raw, sheet, clinical=clinical)
    apply_priority_safety_net(plan, sheet, flags)
    case = {'plan': plan, 'cells': cells or {}}
    presentation = build_case_presentation(
        request=request(surveys=None),
        parsed=parsed(),
        document=document(*default_facts()),
        data=SimpleNamespace(appendices=SimpleNamespace(reference_literature=[])),
        clinical=clinical,
        microbiome=microbiome,
        sheet=sheet,
        case=case,
        timing={'clinicalDate': '2026-06-30'},
    )
    return presentation, plan, sheet


def test_priority_item_with_only_a_title_falls_back_to_the_title_alone():
    def raw(sheet):
        analysis = raw_analysis(sheet)
        analysis['priorityItems'] = [{'title': '粪便隐血查因', 'handles': [handle_of(sheet, 'fact-fobt')]}]
        return analysis

    presentation, _, _ = _project(raw)
    urgent = presentation.overview['priorityFindings'][0]
    assert urgent['items'][0]['text'] == '粪便隐血查因。'


def test_table_row_chips_are_limited_to_the_displayed_members():
    def raw(sheet):
        analysis = raw_analysis(sheet)
        us = handle_of(sheet, 'fact-us')
        analysis['systems'][1]['rows'] = [{'title': '胆囊息肉', 'handles': [us], 'status': '亚临床边界'}]
        return analysis

    presentation, _, _ = _project(raw)
    group = next(item for item in presentation.clinical['clinicalGroups'] if item['code'] == 'organMetabolism')
    row = next(item for item in group['templateRows'] if item['name'] == '胆囊息肉')
    assert [member['value'] for member in row['rows']] == ['胆囊息肉 5mm']
    assert len(row['anchors']) == 1 and row['moreCount'] == 0


def test_gap_naming_a_recorded_test_without_its_abbreviation_is_dropped():
    sheet = build_sheet()
    raw = raw_analysis(sheet)
    raw['testGaps'] = [
        {'test': '空腹葡萄糖', 'because': '已做', 'handles': []},
        {'test': '甘油三酯（TG）', 'because': '已做', 'handles': []},
        {'test': '糖耐量试验', 'because': '空腹血糖接近上限', 'handles': []},
    ]
    plan = normalize_case_analysis(raw, sheet, clinical=default_clinical())
    assert [gap['test'] for gap in plan['testGaps']] == ['糖耐量试验']
    assert {row['title'] for row in plan['audit']['dropped'] if row['reason'] == 'test_already_recorded'} == {
        '空腹葡萄糖',
        '甘油三酯（TG）',
    }


def test_lifestyle_cluster_given_as_c_number_means_the_model_order():
    sheet = build_sheet()
    raw = raw_analysis(sheet)
    # The model's second cluster carries the priority item, so it becomes C1 after ordering.
    raw['lifestyle'] = [
        {'dimension': 'sleep', 'handles': [handle_named(sheet, '睡眠情况', 'reported')], 'clusters': ['C2']}
    ]
    plan = normalize_case_analysis(raw, sheet, clinical=default_clinical())
    promoted = next(cluster for cluster in plan['clusters'] if cluster['name'] == '隐血—消化道')
    assert promoted['id'] == 'C1'
    assert plan['lifestyle'][0]['clusterIds'] == ['C1']


def test_removed_handles_leave_no_space_before_punctuation():
    assert clean_cell_text('血脂偏高 D1。') == '血脂偏高。'
    assert clean_cell_text('偏高(M12, M13)；血糖正常。') == '偏高；血糖正常。'


def test_recorded_value_written_with_fewer_decimals_is_verified():
    sheet = CaseSheet(text='', numbers=frozenset(text_numbers('拟杆菌属 18.1036 % 扭链瘤胃球菌 4.7804 %')))
    assert unverified_numbers('拟杆菌属18.10%、扭链瘤胃球菌4.78%', sheet) == []
    assert unverified_numbers('扭链瘤胃球菌4.79%', sheet) == ['4.79']


def test_review_finding_with_checks_given_as_one_string_keeps_it_whole():
    sheet = build_sheet()
    plan = normalize_case_analysis(raw_analysis(sheet), sheet, clinical=default_clinical())
    plan['priorityItems'] = []
    us = handle_of(sheet, 'fact-us')
    answer = {
        'issues': [],
        'missedFindings': [
            {'title': '胆囊息肉评估', 'handles': [us], 'text': '需由肝胆外科评估。', 'checks': '腹部超声'},
        ],
    }
    client = FakeClient(review_assessment_case=lambda payload: answer if 'overview' in payload['reviewScope'] else {})
    cells = {'overview.summary': '概述。'}
    asyncio.run(
        review_case(client, cells, {'overview.summary': 150}, plan, sheet, deadline=time.monotonic() + 30, audit={})
    )
    item = plan['priorityItems'][0]
    assert item['origin'] == 'review' and item['checks'] == ['腹部超声']
    assert cells['overview.priority.1'] == '需由肝胆外科评估。'


def test_audit_summary_keeps_no_review_text():
    case = {
        'audit': {
            'review': {
                'programFindings': {'a': {}},
                'remainingFindings': {},
                'missedFindings': [{'title': '发现', 'text': '受检者的文字', 'handles': ['L1']}],
            }
        },
        'plan': {'audit': {}},
    }
    summary = case_audit_summary(case)
    assert summary['review']['missedFindings'] == [{'handles': ['L1']}]
    assert summary['review']['programFindings'] == {'cells': 1}


def test_former_path_reasons_do_not_include_source_failures():
    assert 'REPORTED_FACT_SOURCE_MISMATCH' in FORMER_PATH_REASONS
    assert not FORMER_PATH_REASONS & {
        'DOCUMENT_SOURCE_INCOMPLETE',
        'REPORT_CONTENT_INCOMPLETE',
        'ASSESSMENT_DATA_UNAVAILABLE',
    }


def test_calibration_cell_is_requested_only_for_the_system_that_cites_the_reconciled_record():
    from modules.report.assessment_case_authoring import section_jobs

    clinical, microbiome = default_clinical(), default_microbiome()
    sheet = build_sheet(clinical=clinical, microbiome=microbiome)
    plan = normalize_case_analysis(raw_analysis(sheet), sheet, clinical=clinical)
    cells = {
        cell['id']
        for job in section_jobs(plan, sheet, clinical=clinical, microbiome=microbiome)
        for cell in job['cells']
    }
    # The only calibration note of the fixture reconciles the fasting glucose reading.
    assert 'clinical.cardiometabolic.calibration' in cells
    assert 'clinical.digestiveRenal.calibration' not in cells


def test_record_without_a_display_row_is_shown_as_recorded():
    def raw(sheet):
        analysis = raw_analysis(sheet)
        adv_free = handle_of(sheet, 'fact-glu')
        analysis['systems'][0]['rows'] = [{'title': '血糖', 'handles': [adv_free], 'status': '稳健达标'}]
        return analysis

    clinical = default_clinical()
    clinical['clinicalGroups'][0]['rows'] = [
        row for row in clinical['clinicalGroups'][0]['rows'] if row['code'] != 'fact-glu'
    ]
    microbiome = default_microbiome()
    sheet = build_sheet(clinical=clinical, microbiome=microbiome)
    plan = normalize_case_analysis(raw(sheet), sheet, clinical=clinical)
    case = {'plan': plan, 'cells': {}}
    presentation = build_case_presentation(
        request=request(surveys=None),
        parsed=parsed(),
        document=document(*default_facts()),
        data=SimpleNamespace(appendices=SimpleNamespace(reference_literature=[])),
        clinical=clinical,
        microbiome=microbiome,
        sheet=sheet,
        case=case,
        timing={'clinicalDate': '2026-06-30'},
    )
    group = next(item for item in presentation.clinical['clinicalGroups'] if item['code'] == 'cardiometabolic')
    row = next(item for item in group['templateRows'] if item['name'] == '血糖')
    assert [(member['name'], member['value'], member['unit']) for member in row['rows']] == [
        ('空腹葡萄糖(Glu)', '5.74', 'mmol/L')
    ]


def test_chapter_three_puts_haematology_before_endocrine():
    def raw(sheet):
        analysis = raw_analysis(sheet)
        glu, tg = handle_of(sheet, 'fact-glu'), handle_of(sheet, 'fact-tg')
        analysis['systems'] += [
            {
                'code': 'endocrineMusculoskeletal',
                'status': '稳健达标',
                'rows': [{'title': '内分泌', 'handles': [glu], 'status': '稳健达标'}],
            },
            {
                'code': 'hematologyImmune',
                'status': '稳健达标',
                'rows': [{'title': '血液', 'handles': [tg], 'status': '重点关注'}],
            },
        ]
        return analysis

    presentation, _, _ = _project(raw)
    codes = [group['code'] for group in presentation.clinical['clinicalGroups']]
    assert codes.index('hematologyImmune') < codes.index('endocrineMusculoskeletal')
    # Chapter one keeps the fixed system order of the contract.
    systems = [system['code'] for system in presentation.overview['systems']]
    assert systems.index('endocrineMusculoskeletal') < systems.index('hematologyImmune')


# ---- requests without a microbiome report, and the rule-only fallback ------------------------------------------


def _cell_ids(raw, sheet, clinical, microbiome):
    from modules.report.assessment_case_authoring import section_jobs

    plan = normalize_case_analysis(raw, sheet, clinical=clinical)
    jobs = section_jobs(plan, sheet, clinical=clinical, microbiome=microbiome)
    return {cell['id'] for job in jobs for cell in job['cells']}


def test_microbiome_evidence_cells_are_only_requested_with_a_microbiome_report():
    clinical = default_clinical()
    full = build_sheet(clinical=clinical, microbiome=default_microbiome())
    raw = raw_analysis(full)
    with_report = _cell_ids(raw, full, clinical, default_microbiome())
    # The same analysis against a sheet without microbiome records: its M handles are dropped.
    without = _cell_ids(raw, build_sheet(clinical=clinical, microbiome={}), clinical, {})
    assert any(cell.endswith('.microbiomeEvidence') for cell in with_report)
    assert not any(cell.endswith('.microbiomeEvidence') or cell.startswith('micro.') for cell in without)
    assert any(cell.endswith('.clinicalEvidence') for cell in without)


def test_microbiome_report_without_digestive_examinations_says_so_in_chapter_four():
    presentation, _, _ = _project(raw_analysis)
    context = presentation.clinical['digestiveContext']
    assert [group['code'] for group in context] == ['digestiveNotRecorded'] and context[0]['summary']


def test_rule_only_case_lists_recorded_values_and_the_rule_items():
    from modules.report.assessment_case_projection import DEGRADED_STATEMENT
    from modules.report.assessment_case_report import rule_only_case

    clinical, microbiome = default_clinical(), default_microbiome()
    sheet = build_sheet(clinical=clinical, microbiome=microbiome)
    flags = {
        'priorityItems': [
            {
                'ruleId': 'RF-FOBT',
                'title': '粪便隐血阳性查因',
                'factIds': ['fact-fobt'],
                'timeframe': {'text': '2 周内', 'days': 14},
                'department': '消化内科',
                'checks': ['结肠镜'],
                'sources': [],
            }
        ]
    }
    inputs = {'sheet': sheet, 'clinical': clinical, 'microbiome': microbiome, 'flags': flags, 'timing': None}
    case = rule_only_case(inputs)
    assert case['degraded'] and case['cells'] == {} and case['inputs'] is inputs
    assert [item['origin'] for item in case['plan']['priorityItems']] == ['safety_net']

    presentation = build_case_presentation(
        request=request(surveys=None),
        parsed=parsed(),
        document=document(*default_facts()),
        data=SimpleNamespace(appendices=SimpleNamespace(reference_literature=[])),
        clinical=clinical,
        microbiome=microbiome,
        sheet=sheet,
        case=case,
        timing={'clinicalDate': '2026-06-30'},
    )
    urgent = presentation.overview['priorityFindings'][0]['items']
    assert len(urgent) == 1 and '消化内科' in urgent[0]['text'] and '结肠镜' in urgent[0]['text']
    assert presentation.overview['graphSummary'] == DEGRADED_STATEMENT
    # Every abnormal record is still shown with its recorded value, without model text.
    rows = [row for group in presentation.clinical['clinicalGroups'] for row in group['templateRows']]
    assert {member['name'] for row in rows for member in row['rows']} >= {'甘油三酯(TG)', 'TyG 指数', '粪便隐血'}
    assert {row['completion'] for row in rows} == {'source_only'}


def test_gap_to_a_quoted_bound_or_target_is_a_verified_number():
    sheet = build_sheet()
    glucose = sheet.entry(handle_of(sheet, 'fact-glu'))
    assert glucose['value'] == '5.74' and '6.11' in glucose['reference']
    # 6.11 - 5.74: the gap between the quoted value and the bound of its own record.
    assert unverified_numbers('空腹血糖5.74 mmol/L，距参考上限尚有0.37 mmol/L。', sheet) == []
    # 5.74 - 5.6: both numbers stand in the text.
    assert unverified_numbers('空腹血糖5.74 mmol/L，高于理想值5.6 mmol/L共0.14 mmol/L。', sheet) == []
    # A planned target counts as a bound.
    assert unverified_numbers('空腹血糖5.74 mmol/L，高于理想值0.14 mmol/L。', sheet, frozenset({'5.6'})) == []
    # A gap that no pair of quoted numbers gives stays unverified.
    assert unverified_numbers('空腹血糖5.74 mmol/L，距参考上限尚有0.47 mmol/L。', sheet) == ['0.47']
    # The bound of a record that the text does not quote is not used.
    assert unverified_numbers('甘油三酯偏高，距参考上限尚有0.37 mmol/L。', sheet) == ['0.37']


def test_cluster_without_microbiome_records_gets_no_microbiome_evidence_cell():
    clinical, microbiome = default_clinical(), default_microbiome()
    sheet = build_sheet(clinical=clinical, microbiome=microbiome)
    raw = raw_analysis(sheet)
    for cluster in raw['clusters']:
        cluster['microbiomeHandles'] = []
    cells = _cell_ids(raw, sheet, clinical, microbiome)
    assert any(cell.startswith('clusters.') and cell.endswith('.clinicalEvidence') for cell in cells)
    assert not any(cell.startswith('clusters.') and cell.endswith('.microbiomeEvidence') for cell in cells)
    assert any(cell.startswith('micro.') for cell in cells)  # chapter four itself is still written


def test_fact_sheet_states_how_the_interval_between_the_two_materials_is_read():
    near = build_sheet(timing={'clinicalDate': '2026-06-30', 'microbiomeSampleDate': '2026-11-02', 'daysBetween': 125})
    assert '两者相隔 125 天（不超过 6 个月，属同一健康周期，两类资料可互相印证）' in near.text
    far = build_sheet(timing={'clinicalDate': '2025-06-30', 'microbiomeReceivedDate': '2026-11-02'})
    assert '体检日期与菌群收样日期相隔 490 天（超过 6 个月，结论以较新的资料为准）' in far.text
    assert '相隔' not in build_sheet(timing={'clinicalDate': '2026-06-30'}).text


def test_second_record_of_the_same_reading_is_shown_once():
    from modules.report.assessment_case_projection import _repeats

    shown = [{'name': '体重指数(BMI)', 'value': '29.3', 'unit': 'kg/m2'}, {'name': '腰围', 'value': '92', 'unit': 'cm'}]
    assert _repeats({'name': '体重指数 BMI', 'value': '29.3', 'unit': 'kg/m2'}, shown)
    assert _repeats({'name': '腰围及中心型肥胖判定', 'value': '92', 'unit': 'cm'}, shown)
    # Another value, another unit, another item or a qualitative result is kept.
    assert not _repeats({'name': '体重指数 BMI', 'value': '29.4', 'unit': 'kg/m2'}, shown)
    assert not _repeats({'name': '腰围', 'value': '92', 'unit': 'mm'}, shown)
    assert not _repeats({'name': '臀围', 'value': '92', 'unit': 'cm'}, shown)
    assert not _repeats(
        {'name': '尿隐血', 'value': '阴性', 'unit': None}, [{'name': '隐血', 'value': '阴性', 'unit': None}]
    )


def test_second_record_of_a_cited_reading_gets_no_program_row():
    from tests.unit._support.assessment_case import fact
    from tests.unit._support.assessment_case import view_row

    def program_rows(value):
        facts, clinical = default_facts(), default_clinical()
        # The device printout carries a triglyceride reading next to the laboratory sheet.
        facts.append(
            fact(
                'fact-tg-device',
                '甘油三酯 TG',
                value,
                unit='mmol/L',
                reference_range='0.00-1.70',
                status='偏高',
                abnormal=True,
            )
        )
        clinical['clinicalGroups'][0]['rows'].append(
            view_row('fact-tg-device', '甘油三酯 TG', value, unit='mmol/L', reference='0.00-1.70', level='focus')
        )
        sheet = build_sheet(facts=facts, clinical=clinical)
        plan = normalize_case_analysis(raw_analysis(sheet), sheet, clinical=clinical)
        return handle_of(sheet, 'fact-tg-device') in {row['handle'] for row in plan['audit']['programRows']}

    assert not program_rows('3.34')  # the same reading as the cited laboratory record
    assert program_rows('3.52')  # another reading is still given its own row


def test_count_units_copied_from_the_sheet_are_written_readably():
    from modules.report.assessment_case_sheet import readable_units

    assert clean_cell_text('白细胞6.8×10^9/L、血小板231 10E9/L，红细胞4.21×10E12/L。') == (
        '白细胞6.8×10⁹/L、血小板231×10⁹/L，红细胞4.21×10¹²/L。'
    )
    # Already readable text, other powers and plain numbers are left alone.
    assert (
        readable_units('白细胞6.8×10⁹/L，评分10分，10/20，1.5×10^3 copies')
        == '白细胞6.8×10⁹/L，评分10分，10/20，1.5×10^3 copies'
    )
    sheet = CaseSheet(text='', numbers=frozenset(text_numbers('白细胞计数 6.8 10^9/L')))
    assert unverified_numbers(readable_units('白细胞6.8×10^9/L'), sheet) == []
