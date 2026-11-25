"""Whole-case authoring: cell jobs, text cleaning, number checks, section writing, review and the full run."""

import time

import pytest

from modules.report.assessment_case_authoring import ANALYZE_TASK
from modules.report.assessment_case_authoring import CASE_TASKS
from modules.report.assessment_case_authoring import REVIEW_TASK
from modules.report.assessment_case_authoring import WRITE_TASK
from modules.report.assessment_case_authoring import CaseAuthoringError
from modules.report.assessment_case_authoring import analysis_payload
from modules.report.assessment_case_authoring import author_assessment_case
from modules.report.assessment_case_authoring import cell_issues
from modules.report.assessment_case_authoring import clean_cell_text
from modules.report.assessment_case_authoring import review_case
from modules.report.assessment_case_authoring import section_jobs
from modules.report.assessment_case_authoring import section_payload
from modules.report.assessment_case_authoring import unverified_numbers
from modules.report.assessment_case_authoring import write_case_sections
from modules.report.assessment_case_guides import CASE_GUIDE_VERSION
from modules.report.assessment_case_guides import SECTION_GUIDES
from modules.report.assessment_case_plan import apply_priority_safety_net
from modules.report.assessment_case_plan import normalize_case_analysis
from tests.unit._support.assessment_case import FakeClient
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_microbiome
from tests.unit._support.assessment_case import raw_analysis
from tests.unit._support.assessment_case import request


def later(seconds=600.0):
    return time.monotonic() + seconds


@pytest.fixture
def sheet():
    return build_sheet()


@pytest.fixture
def plan(sheet):
    return normalize_case_analysis(raw_analysis(sheet), sheet, clinical=default_clinical())


def jobs_of(plan, sheet, **extra):
    return section_jobs(
        plan,
        sheet,
        clinical=extra.get('clinical', default_clinical()),
        microbiome=extra.get('microbiome', default_microbiome()),
    )


def cell_ids(jobs):
    return [cell['id'] for job in jobs for cell in job['cells']]


def answer_all(payload, text='正文'):
    return {'cells': {cell['id']: f'{text}{n}' for n, cell in enumerate(payload['cells'])}}


def no_review(payload):
    return {'issues': [], 'missedFindings': []}


# ---- clean_cell_text ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('空腹血糖 5.74 mmol/L（L2、L3），偏高。', '空腹血糖 5.74 mmol/L，偏高。'),
        ('甘油三酯偏高(M12, M13)；血糖正常（ L1 ）。', '甘油三酯偏高；血糖正常。'),
        ('血脂偏高 D1 需复查', '血脂偏高 需复查'),
        ('**重点** 您的 `TG` 偏高', '重点 受检者的 TG 偏高'),
        ('# 标题\n正文  内容', '标题\n正文 内容'),
        ('__加粗__ 文字', '加粗 文字'),
        ('见 CAL1、L1a、CA125 与 L12345', '见 CAL1、L1a、CA125 与 L12345'),  # not handles: letters or digits around
        ('段落一\n\n  段落二', '段落一\n段落二'),
    ],
)
def test_clean_cell_text_removes_handles_marks_and_second_person(raw, expected):
    assert clean_cell_text(raw) == expected


@pytest.mark.parametrize('raw', [None, '', '   ', 'null', 'NULL', 'None', '无', '（无）', '（L1）', 5, ['a'], {'a': 1}])
def test_clean_cell_text_returns_none_for_empty_or_non_text(raw):
    assert clean_cell_text(raw) is None


# ---- unverified_numbers -------------------------------------------------------------------------------------------


def test_numbers_on_the_sheet_are_never_reported_in_any_spelling(sheet):
    assert unverified_numbers('甘油三酯 3.34，BMI 29.30，腰围 29.3，肝酶 100.0，血糖 5.740', sheet) == []


def test_decimals_and_long_integers_missing_from_the_sheet_are_reported_once(sheet):
    assert unverified_numbers('TG 7.77 与 7.77，另有 4500 例和 12 项', sheet) == ['7.77', '4500']


def test_short_integers_dates_and_ranges_are_ignored(sheet):
    assert unverified_numbers('共 12 项，2 周内，90 天内；2026-06-30；正常范围 2.5-4', sheet) == []


@pytest.mark.parametrize(
    'text', ['CA19-9 偏高', '维生素B12 偏低', 'HbA1c 正常', 'CA125 与 CA153', 'IL-6 与 TNF-α 升高', '25-羟维生素D']
)
def test_digits_inside_test_names_are_not_numbers(sheet, text):
    assert unverified_numbers(text, sheet) == []


def test_a_number_after_a_test_name_is_still_checked(sheet):
    assert unverified_numbers('CA19-9 37.5 U/mL，HbA1c 6.9%', sheet) == ['37.5', '6.9']


def test_thousands_separators_are_removed_before_checking(sheet):
    assert unverified_numbers('白细胞 12,345 与 1,234.5', sheet) == ['12345', '1234.5']
    assert unverified_numbers('', sheet) == [] and unverified_numbers(None, sheet) == []


def test_allowed_numbers_are_not_reported(sheet):
    assert unverified_numbers('目标 <1.77', sheet, allowed=frozenset({'1.77'})) == []


# ---- cell_issues --------------------------------------------------------------------------------------------------


def test_cell_issues_collect_numbers_backstage_words_and_overlength(plan, sheet):
    cells = {
        'overview.summary': 'BMI 33.3，且问卷显示乏力',
        'overview.bodySummary': '体重指数与理想目标 24.5 相比',  # 24.5 is not in the sheet or plan
        'conclusion.summary': '正常' * 41,
        'micro.lead': '没有问题 3.34',
    }
    limits = {'overview.summary': 150, 'conclusion.summary': 50, 'overview.bodySummary': 260}

    issues = cell_issues(cells, limits, sheet, plan)

    assert issues['overview.summary'] == {'unverifiedNumbers': ['33.3'], 'backstageWords': ['问卷显示']}
    assert issues['overview.bodySummary'] == {'unverifiedNumbers': ['24.5']}
    assert issues['conclusion.summary'] == {'overLength': {'length': 82, 'maxChars': 50}}
    assert 'micro.lead' not in issues


def test_numbers_of_the_plan_count_as_known(sheet, plan):
    plan['bodyRows'][0]['ideal'] = '<23.9'

    assert cell_issues({'overview.bodySummary': '理想目标 23.9'}, {}, sheet, plan) == {}


def test_length_is_only_flagged_beyond_one_and_a_half_times_the_limit(sheet, plan):
    limits = {'a': 100}

    assert cell_issues({'a': '字' * 160}, limits, sheet, plan) == {}
    assert 'overLength' in cell_issues({'a': '字' * 161}, limits, sheet, plan)['a']


# ---- payloads -----------------------------------------------------------------------------------------------------


def test_analysis_payload_carries_the_sheet_and_schema(sheet):
    payload = analysis_payload(sheet)

    assert payload['task'] == ANALYZE_TASK in CASE_TASKS
    assert payload['caseSheet'] == sheet.text
    assert set(payload['outputSchema']) >= {'systems', 'clusters', 'priorityItems', 'symptoms', 'lifestyle'}
    assert payload['method'] and all(isinstance(line, str) for line in payload['method'])


def test_section_payload_has_no_audit_inside_the_analysis(plan, sheet):
    job = jobs_of(plan, sheet)[0]

    payload = section_payload(job, plan, sheet)

    assert payload['task'] == WRITE_TASK and payload['section'] == job['code']
    assert 'audit' not in payload['analysis'] and 'version' not in payload['analysis']
    assert payload['cells'] == job['cells'] and payload['sectionGuide'] == list(job['guide'])
    assert 'audit' in plan  # the plan itself is not modified


# ---- section_jobs -------------------------------------------------------------------------------------------------


def test_full_case_jobs_and_cell_ids(plan, sheet):
    jobs = jobs_of(plan, sheet)

    assert [job['code'] for job in jobs] == [
        'overview',
        'symptoms',
        'clinical.cardiometabolic',
        'clinical.digestiveRenal',
        'clinicalFrame',
        'microbiomeStructure',
        'microbiomeOrganisms',
        'microbiomeFunctions',
        'clusters',
        'lifestyle',
        'conclusion',
    ]
    ids = cell_ids(jobs)
    assert len(ids) == len(set(ids))
    assert set(ids) >= {
        'overview.bodySummary',
        'overview.bodyRow.1',
        'overview.summary',
        'overview.priority.1',
        'overview.followup.1',
        'overview.gap.1',
        'overview.graphSummary',
        'overview.system.cardiometabolic.evidence',
        'overview.system.digestiveRenal.conclusion',
        'symptoms.lead',
        'symptoms.1.history',
        'symptoms.1.clinicalEvidence',
        'symptoms.1.microbiomeEvidence',
        'symptoms.1.title',
        'symptoms.1.explanation',
        'symptoms.summary',
        'clinical.cardiometabolic.row.2',
        'clinical.cardiometabolic.summary',
        'clinicalFrame.lead',
        'clinicalFrame.baseline',
        'micro.lead',
        'micro.sample',
        'micro.fermentation',
        'micro.structure.diversity',
        'micro.structureSummary',
        'micro.organisms.beneficial',
        'micro.function.scfa',
        'micro.risk',
        'clusters.lead',
        'clusters.C1.impact',
        'clusters.C2.action',
        'lifestyle.background',
        'lifestyle.sleep.behavior',
        'lifestyle.sleep.effect',
        'lifestyle.recognition.1',
        'conclusion.layer.clinical',
        'conclusion.probiotic',
        'conclusion.nutrient',
        'conclusion.lifestyle',
        'conclusion.summary',
    }
    assert all(cell['maxChars'] > 0 and cell['duty'] for job in jobs for cell in job['cells'])


def test_every_job_uses_a_known_guide(plan, sheet):
    known = list(SECTION_GUIDES.values())

    assert all(job['guide'] in known for job in jobs_of(plan, sheet))


def test_cell_context_carries_the_records_the_writer_must_use(plan, sheet):
    cells = {cell['id']: cell for job in jobs_of(plan, sheet) for cell in job['cells']}

    assert cells['overview.priority.1']['records'] == ['L4'] and cells['overview.priority.1']['checks'] == ['结肠镜']
    assert cells['overview.bodyRow.1'] == {
        'id': 'overview.bodyRow.1',
        'duty': '生理与代谢意义',
        'maxChars': 110,
        'title': '体重指数',
        'records': ['L1'],
        'ideal': '<24',
        'status': '高于理想值',
    }
    assert 'dataNotes' in cells['clinical.cardiometabolic.calibration']  # the calibration note of the plan
    assert 'records' not in cells['symptoms.1.history']
    assert cells['symptoms.1.history']['reported'] == ['Q2']


def test_no_symptom_job_without_symptoms(plan, sheet):
    plan['symptoms'] = []

    jobs = jobs_of(plan, sheet)

    assert 'symptoms' not in [job['code'] for job in jobs]
    assert not [identifier for identifier in cell_ids(jobs) if identifier.startswith('symptoms.')]


def test_no_microbiome_jobs_when_the_sheet_has_no_microbiome_data():
    sheet = build_sheet(microbiome={})
    plan = normalize_case_analysis(raw_analysis(build_sheet()), sheet, clinical=default_clinical())

    jobs = section_jobs(plan, sheet, clinical=default_clinical(), microbiome={})

    assert not [job['code'] for job in jobs if job['code'].startswith('microbiome')]
    ids = cell_ids(jobs)
    assert not [identifier for identifier in ids if identifier.startswith('micro.')]
    assert not {'conclusion.probiotic', 'conclusion.fiber', 'conclusion.nutrient'} & set(ids)
    assert {'conclusion.layer.core', 'conclusion.lifestyle', 'conclusion.summary'} <= set(ids)


def test_no_clinical_jobs_without_clinical_rows(sheet):
    empty = normalize_case_analysis({}, build_sheet(facts=[], clinical={}, microbiome={}), clinical={})
    bare = build_sheet(facts=[], clinical={}, req=request(surveys=[]))

    jobs = section_jobs(empty, bare, clinical={}, microbiome=default_microbiome())

    codes = [job['code'] for job in jobs]
    assert not [code for code in codes if code.startswith('clinical')]
    assert 'overview' in codes and 'conclusion' in codes
    assert 'overview.bodySummary' not in cell_ids(jobs)  # nothing to summarise without body rows
    assert 'clusters' not in codes and 'lifestyle' not in codes


def test_untested_systems_get_no_overview_cells(plan, sheet):
    ids = cell_ids(jobs_of(plan, sheet))

    for code in ('endocrineMusculoskeletal', 'hematologyImmune'):
        assert plan['systems'][[system['code'] for system in plan['systems']].index(code)]['status'] == '未检测'
        assert not [identifier for identifier in ids if f'overview.system.{code}.' in identifier]
    assert 'overview.system.microbiome.evidence' in ids  # the microbiome system has data


def test_microbiome_system_gets_no_chapter_three_job(plan, sheet):
    assert not [job['code'] for job in jobs_of(plan, sheet) if job['code'] == 'clinical.microbiome']


def test_digestive_context_and_group_members_become_cells(plan, sheet):
    clinical = default_clinical()
    clinical['digestiveContext'] = [
        {'code': 'oral', 'name': '口腔', 'rows': [{'name': '牙石', 'value': '1度', 'unit': None}] * 14},
        {'name': '无编码', 'rows': []},
    ]

    jobs = section_jobs(plan, sheet, clinical=clinical, microbiome=default_microbiome())

    cells = {cell['id']: cell for job in jobs for cell in job['cells']}
    assert 'micro.digestive.oral' in cells and not [key for key in cells if 'None' in key]
    assert (
        len(cells['micro.digestive.oral']['members']) == 12
        and cells['micro.digestive.oral']['members'][0] == '牙石 1度'
    )


def test_microbiome_without_organism_groups_has_no_organism_job(plan, sheet):
    microbiome = default_microbiome()
    microbiome['organismGroups'] = []

    codes = [job['code'] for job in jobs_of(plan, sheet, microbiome=microbiome)]

    assert 'microbiomeOrganisms' not in codes and 'microbiomeFunctions' in codes


# ---- write_case_sections ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_cells_are_accepted_cleaned_and_unknown_ids_ignored(plan, sheet):
    def write(payload):
        cells = {cell['id']: f'正文 {cell["id"]}（L3）您好' for cell in payload['cells']}
        cells['bogus.cell'] = '不该出现'
        return {'cells': cells}

    audit = {}
    client = FakeClient(**{WRITE_TASK: write})

    cells, limits = await write_case_sections(
        client, plan, sheet, clinical=default_clinical(), microbiome=default_microbiome(), deadline=later(), audit=audit
    )

    assert 'bogus.cell' not in cells
    assert cells['overview.summary'] == '正文 overview.summary受检者好'
    assert set(cells) == set(limits) and limits['overview.summary'] == 150
    assert audit['sections']['failed'] == [] and audit['sections']['incomplete'] == {}
    assert audit['sections']['cellCount'] == audit['sections']['requestedCells'] == len(cells)
    assert audit['sections']['jobs'] == [job['code'] for job in jobs_of(plan, sheet)]
    assert {call['status'] for call in audit['calls']} == {'ok'} and len(audit['calls']) == len(client.calls)


@pytest.mark.asyncio
async def test_response_without_cells_wrapper_is_read_as_the_cell_map(plan, sheet):
    client = FakeClient(**{WRITE_TASK: lambda payload: {cell['id']: '直接写入' for cell in payload['cells']}})

    cells, _ = await write_case_sections(client, plan, sheet, deadline=later(), audit={})

    assert cells['overview.summary'] == '直接写入'


@pytest.mark.asyncio
async def test_a_failed_call_is_retried_once(plan, sheet):
    attempts = {}

    def write(payload):
        section = payload['section']
        attempts[section] = attempts.get(section, 0) + 1
        if section == 'conclusion' and attempts[section] == 1:
            return RuntimeError('boom')
        return answer_all(payload)

    audit = {}
    client = FakeClient(**{WRITE_TASK: write})

    cells, _ = await write_case_sections(
        client, plan, sheet, clinical=default_clinical(), microbiome=default_microbiome(), deadline=later(), audit=audit
    )

    assert attempts['conclusion'] == 2 and 'conclusion.summary' in cells
    assert audit['sections']['failed'] == []
    labels = [call['label'] for call in audit['calls']]
    assert 'write:conclusion' in labels and 'write:conclusion:retry2' in labels
    assert next(call for call in audit['calls'] if call['label'] == 'write:conclusion')['status'] == 'failed'


@pytest.mark.asyncio
async def test_a_section_that_fails_twice_is_listed_as_failed(plan, sheet):
    def write(payload):
        return RuntimeError('boom') if payload['section'] == 'lifestyle' else answer_all(payload)

    audit = {}
    client = FakeClient(**{WRITE_TASK: write})

    cells, limits = await write_case_sections(client, plan, sheet, deadline=later(), audit=audit)

    assert audit['sections']['failed'] == ['lifestyle']
    assert len([payload for payload in client.calls if payload['section'] == 'lifestyle']) == 2
    assert not [key for key in cells if key.startswith('lifestyle.')]
    assert any(key.startswith('lifestyle.') for key in limits)  # still requested, so it can be reported missing


@pytest.mark.asyncio
async def test_no_retry_when_too_little_time_is_left(plan, sheet):
    client = FakeClient(**{WRITE_TASK: lambda payload: RuntimeError('boom')})
    audit = {}

    cells, _ = await write_case_sections(client, plan, sheet, deadline=later(10.0), audit=audit)

    assert cells == {} and len(audit['sections']['failed']) == len(audit['sections']['jobs'])
    assert len(client.calls) == len(audit['sections']['jobs'])


@pytest.mark.asyncio
async def test_a_passed_deadline_skips_every_call(plan, sheet):
    client = FakeClient(**{WRITE_TASK: answer_all})
    audit = {}

    cells, _ = await write_case_sections(client, plan, sheet, deadline=later(-5.0), audit=audit)

    assert cells == {} and client.calls == []
    assert {call['status'] for call in audit['calls']} == {'skipped_no_time'}
    assert audit['sections']['failed'] == audit['sections']['jobs']


@pytest.mark.asyncio
async def test_mostly_missing_answer_is_retried_and_partial_answers_are_reported(plan, sheet):
    seen = {}

    def write(payload):
        section = payload['section']
        seen[section] = seen.get(section, 0) + 1
        if section == 'overview':
            first = payload['cells'][0]['id']
            return {'cells': {first: '只有一格'}}  # always incomplete, even on the retry
        if section == 'conclusion':
            if seen[section] == 1:
                return {'cells': {}}  # nothing usable: retried
            return answer_all(payload)
        cells = answer_all(payload)['cells']
        cells[payload['cells'][-1]['id']] = None  # an explicit null is not a missing cell
        return {'cells': cells}

    audit = {}
    client = FakeClient(**{WRITE_TASK: write})

    cells, _ = await write_case_sections(
        client, plan, sheet, clinical=default_clinical(), microbiome=default_microbiome(), deadline=later(), audit=audit
    )

    assert seen['overview'] == 2 and seen['conclusion'] == 2 and seen['clusters'] == 1
    incomplete = audit['sections']['incomplete']
    assert set(incomplete) == {'overview'} and 'overview.summary' in incomplete['overview']
    assert 'clusters.C2.action' not in cells and 'conclusion.summary' in cells  # the explicit null


@pytest.mark.asyncio
async def test_empty_text_counts_as_missing(plan, sheet):
    def write(payload):
        cells = answer_all(payload)['cells']
        cells[payload['cells'][0]['id']] = '（L1）'
        return {'cells': cells}

    audit = {}
    cells, _ = await write_case_sections(FakeClient(**{WRITE_TASK: write}), plan, sheet, deadline=later(), audit=audit)

    assert 'overview.bodySummary' not in cells
    assert 'overview.bodySummary' in audit['sections']['incomplete']['overview']


# ---- review_case --------------------------------------------------------------------------------------------------


def review_cells():
    cells = {
        'overview.summary': '原文总览',
        'overview.bodySummary': '原文体征',
        'clinical.cardiometabolic.summary': '原文小结',
        'micro.lead': '原文导语',
        'conclusion.summary': '原文结论',
    }
    return cells, {key: 60 for key in cells}


def reviewer(responses):
    """Answer by the first chapter prefix of ``responses`` that the review scope contains (any grouping works)."""

    def handle(payload):
        for prefix, response in responses.items():
            if prefix in payload['reviewScope']:
                return response() if callable(response) else response
        return {}

    return handle


@pytest.mark.asyncio
async def test_revised_text_replaces_the_draft(plan, sheet):
    cells, limits = review_cells()
    client = FakeClient(
        **{
            REVIEW_TASK: reviewer(
                {
                    'overview': {
                        'issues': [{'id': 'overview.summary', 'problem': '数值不符', 'revised': '改后总览（L3）'}]
                    },
                }
            )
        }
    )
    audit = {}

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit=audit)

    assert cells['overview.summary'] == '改后总览'
    assert result['revised'] == [{'id': 'overview.summary', 'problem': '数值不符'}]
    assert cells['overview.bodySummary'] == '原文体征'
    assert audit['review'] is result and result['failedGroups'] == [] and result['rejected'] == []


@pytest.mark.asyncio
async def test_abbreviated_id_is_accepted_only_when_it_names_exactly_one_cell(plan, sheet):
    cells, limits = review_cells()
    client = FakeClient(
        **{
            REVIEW_TASK: reviewer(
                {
                    'overview': {
                        'issues': [
                            {'id': 'bodySummary', 'revised': '短写改正'},  # unique suffix: accepted
                            {
                                'id': 'summary',
                                'revised': '含糊',
                            },  # overview / clinical / conclusion all end with .summary
                            {'id': 'overview.nothing', 'revised': '不存在'},
                            {'id': '', 'revised': 'x'},
                            {'id': 5, 'revised': 'x'},
                            'not a dict',
                            {'id': 'overview.summary', 'revised': '（L1）'},  # nothing left after cleaning
                        ]
                    },
                }
            )
        }
    )

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert cells['overview.bodySummary'] == '短写改正'
    assert cells['overview.summary'] == '原文总览' and cells['conclusion.summary'] == '原文结论'
    assert [item['id'] for item in result['revised']] == ['overview.bodySummary']
    assert len(result['rejected']) == 5  # the non-dict entry is skipped silently
    assert {item['reason'] for item in result['rejected']} == {'unknown_cell_or_empty_revision'}


@pytest.mark.asyncio
async def test_overlong_revision_is_rejected(plan, sheet):
    cells, limits = review_cells()
    client = FakeClient(
        **{
            REVIEW_TASK: reviewer(
                {
                    'overview': {
                        'issues': [
                            {'id': 'overview.summary', 'revised': '长' * 97},
                            {'id': 'overview.bodySummary', 'revised': '长' * 96},
                        ]
                    },
                }
            )
        }
    )

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert result['rejected'] == [{'id': 'overview.summary', 'reason': 'revision_too_long'}]  # limit 60 * 1.6 = 96
    assert cells['overview.summary'] == '原文总览' and cells['overview.bodySummary'] == '长' * 96


@pytest.mark.asyncio
async def test_revision_may_be_as_long_as_one_and_a_third_of_a_draft_that_was_already_long(plan, sheet):
    cells = {'overview.summary': '长' * 200}

    result = await review_case(
        FakeClient(
            **{REVIEW_TASK: reviewer({'overview': {'issues': [{'id': 'overview.summary', 'revised': '改' * 260}]}})}
        ),
        cells,
        {'overview.summary': 60},
        plan,
        sheet,
        deadline=later(),
        audit={},
    )

    assert result['revised'] and cells['overview.summary'] == '改' * 260  # 200 * 1.3


@pytest.mark.asyncio
async def test_missed_finding_with_valid_handles_becomes_a_priority_item_with_its_text_cell(plan, sheet):
    cells, limits = review_cells()
    finding = {
        'title': '甲状腺结节分级偏高',
        'handles': ['L5', 'L99'],
        'timeframe': '2 周内',
        'days': '14',
        'department': '甲状腺外科',
        'checks': ['细针穿刺', ''],
        'text': '超声提示结节，建议（L5）尽快穿刺。',
    }
    client = FakeClient(**{REVIEW_TASK: reviewer({'conclusion': {'missedFindings': [finding]}})})

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert len(plan['priorityItems']) == 2 and plan['priorityItems'][1] == {
        'index': 2,
        'title': '甲状腺结节分级偏高',
        'timeframe': '2 周内',
        'days': 14,
        'department': '甲状腺外科',
        'checks': ['细针穿刺'],
        'handles': ['L5'],
        'clusterId': None,
        'origin': 'review',
    }
    assert cells['overview.priority.2'] == '超声提示结节，建议尽快穿刺。'
    assert result['missedFindings'] == [finding]


@pytest.mark.asyncio
async def test_missed_finding_is_ignored_when_already_covered_or_unusable(plan, sheet):
    cells, limits = review_cells()
    findings = [
        {'title': '已覆盖', 'handles': ['L4'], 'text': '与现有事项同一依据'},  # L4 is in the model's priority item
        {'title': '无依据', 'handles': ['L99'], 'text': '没有有效编号'},
        {'title': '无正文', 'handles': ['L5'], 'text': '（L5）'},
        {'title': '', 'handles': ['L5'], 'text': '没有标题'},
        {'title': '坏天数', 'handles': ['L2'], 'days': -3, 'text': '时限无效', 'checks': '复查'},
    ]
    client = FakeClient(**{REVIEW_TASK: reviewer({'micro': {'missedFindings': findings + ['x', 3]}})})

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert [item['title'] for item in plan['priorityItems']] == ['粪便隐血查因', '坏天数']
    assert plan['priorityItems'][1]['days'] is None and plan['priorityItems'][1]['timeframe'] is None
    assert 'overview.priority.3' not in cells
    assert len(result['missedFindings']) == 4  # the one without valid handles is not even listed


@pytest.mark.asyncio
async def test_the_same_finding_from_two_reviewers_adds_one_priority_item(plan, sheet):
    cells, limits = review_cells()
    cells['symptoms.lead'] = '原文主诉'
    finding = {'title': '重复发现', 'handles': ['L5'], 'text': '请尽快就诊'}
    client = FakeClient(
        **{REVIEW_TASK: reviewer({'overview': {'missedFindings': [finding]}, 'micro': {'missedFindings': [finding]}})}
    )

    await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert [item['title'] for item in plan['priorityItems']].count('重复发现') == 1


@pytest.mark.asyncio
async def test_a_failing_review_group_is_recorded_and_does_not_raise(plan, sheet):
    cells, limits = review_cells()
    client = FakeClient(
        **{
            REVIEW_TASK: reviewer(
                {
                    'micro': lambda: RuntimeError('boom'),
                    'clinical': {'issues': [{'id': 'clinical.cardiometabolic.summary', 'revised': '改后小结'}]},
                }
            )
        }
    )
    audit = {}

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit=audit)

    assert len(result['failedGroups']) == 1 and 'micro' in result['failedGroups'][0]
    assert cells['clinical.cardiometabolic.summary'] == '改后小结'
    assert [call['status'] for call in audit['calls'] if 'micro' in call['label']] == ['failed']


@pytest.mark.asyncio
async def test_non_dict_review_answers_are_tolerated(plan, sheet):
    cells, limits = review_cells()
    client = FakeClient(**{REVIEW_TASK: lambda payload: ['not', 'a', 'dict']})

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    assert result['revised'] == [] and result['failedGroups'] == []


@pytest.mark.asyncio
async def test_each_group_only_sees_its_own_cells_and_the_program_findings_of_them(plan, sheet):
    cells, limits = review_cells()
    cells['overview.summary'] = '出现了 33.3 这个数'
    client = FakeClient(**{REVIEW_TASK: no_review})

    result = await review_case(client, cells, limits, plan, sheet, deadline=later(), audit={})

    reviewed = []
    for payload in client.calls:
        scope = set(payload['reviewScope'])
        ids = [cell['id'] for cell in payload['cells']]
        assert ids and all(identifier.split('.', 1)[0] in scope for identifier in ids)
        assert set(payload['programFindings']) <= set(ids)
        reviewed += ids
    assert sorted(reviewed) == sorted(cells)  # every cell is reviewed exactly once
    micro = next(payload for payload in client.calls if 'micro' in payload['reviewScope'])
    assert [cell['id'] for cell in micro['cells']] == ['micro.lead'] and micro['programFindings'] == {}
    overview = next(payload for payload in client.calls if 'overview' in payload['reviewScope'])
    assert set(overview['programFindings']) == {'overview.summary'}
    assert result['programFindings']['overview.summary'] == {'unverifiedNumbers': ['33.3']}
    assert result['remainingFindings'] == result['programFindings']  # nothing was revised


@pytest.mark.asyncio
async def test_a_group_without_cells_is_not_reviewed(plan, sheet):
    cells = {'overview.summary': '原文'}
    client = FakeClient(**{REVIEW_TASK: no_review})

    await review_case(client, cells, {}, plan, sheet, deadline=later(), audit={})

    assert len(client.calls) == 1 and 'overview' in client.calls[0]['reviewScope']


@pytest.mark.asyncio
async def test_remaining_findings_are_recomputed_after_revision(plan, sheet):
    cells = {'overview.summary': '出现了 33.3 这个数'}
    client = FakeClient(
        **{REVIEW_TASK: reviewer({'overview': {'issues': [{'id': 'overview.summary', 'revised': '数字已删除'}]}})}
    )

    result = await review_case(client, cells, {'overview.summary': 150}, plan, sheet, deadline=later(), audit={})

    assert result['programFindings'] and result['remainingFindings'] == {}


# ---- author_assessment_case ---------------------------------------------------------------------------------------


def full_client(**overrides):
    handlers = {
        ANALYZE_TASK: lambda payload: raw_analysis(build_sheet()),
        WRITE_TASK: answer_all,
        REVIEW_TASK: no_review,
    }
    handlers.update(overrides)
    return FakeClient(**handlers)


@pytest.mark.asyncio
async def test_full_happy_path(sheet):
    client = full_client()
    audit = {}
    flags = {
        'priorityItems': [
            {
                'ruleId': 'RF-US',
                'title': '腹部超声异常',
                'factIds': ['fact-us'],
                'timeframe': {'text': '1 个月内', 'days': 30},
                'department': '肝胆外科',
                'checks': ['增强 CT'],
                'sources': [],
            }
        ]
    }

    case = await author_assessment_case(
        client,
        sheet,
        clinical=default_clinical(),
        microbiome=default_microbiome(),
        flags=flags,
        deadline=later(),
        audit=audit,
    )

    assert set(case) == {'plan', 'cells', 'limits', 'raw'} and case['raw'] == raw_analysis(sheet)
    plan = case['plan']
    assert [(item['title'], item['origin']) for item in plan['priorityItems']] == [
        ('粪便隐血查因', 'model'),
        ('腹部超声异常', 'safety_net'),
    ]
    assert 'overview.priority.2' in case['cells']  # the writer got a cell for the safety-net item
    assert set(case['cells']) == set(case['limits'])
    assert [payload['task'] for payload in client.calls].count(ANALYZE_TASK) == 1
    assert audit['guideVersion'] == CASE_GUIDE_VERSION
    assert audit['plan']['priorityItems'] == [('粪便隐血查因', 'model'), ('腹部超声异常', 'safety_net')]
    assert audit['sections']['failed'] == [] and audit['review']['failedGroups'] == []
    assert {'analysisSeconds', 'writeSeconds', 'reviewSeconds', 'totalSeconds'} <= set(audit)
    assert audit['calls'][0] == {
        'label': 'analysis',
        'task': ANALYZE_TASK,
        'status': 'ok',
        'seconds': audit['calls'][0]['seconds'],
    }
    assert plan['audit']['safetyNet'][0]['action'] == 'priority_item_added'


@pytest.mark.asyncio
async def test_review_corrections_and_missed_findings_reach_the_result(sheet):
    def review(payload):
        if 'overview' in payload['reviewScope']:
            return {
                'issues': [{'id': 'overview.summary', 'revised': '复核后的总览'}],
                'missedFindings': [{'title': '新发现', 'handles': ['L1'], 'text': '尽快排查'}],
            }
        return {}

    case = await author_assessment_case(
        full_client(**{REVIEW_TASK: review}),
        sheet,
        clinical=default_clinical(),
        microbiome=default_microbiome(),
        deadline=later(),
        audit={},
    )

    assert case['cells']['overview.summary'] == '复核后的总览'
    assert case['plan']['priorityItems'][-1]['origin'] == 'review'
    assert case['cells'][f'overview.priority.{len(case["plan"]["priorityItems"])}'] == '尽快排查'


@pytest.mark.asyncio
async def test_flags_none_and_no_flags_are_accepted(sheet):
    case = await author_assessment_case(
        full_client(),
        sheet,
        clinical=default_clinical(),
        microbiome=default_microbiome(),
        flags=None,
        deadline=later(),
        audit={},
    )

    assert case['plan']['audit']['safetyNet'] == []


@pytest.mark.asyncio
async def test_analysis_that_keeps_failing_raises(sheet):
    client = full_client(**{ANALYZE_TASK: RuntimeError('model down')})
    audit = {}

    with pytest.raises(CaseAuthoringError):
        await author_assessment_case(client, sheet, deadline=later(), audit=audit)

    assert [payload['task'] for payload in client.calls] == [ANALYZE_TASK, ANALYZE_TASK]
    assert audit['analysisError'] == 'RuntimeError'
    assert [call['label'] for call in audit['calls']] == ['analysis', 'analysis:retry']


@pytest.mark.asyncio
async def test_analysis_recovers_on_the_retry(sheet):
    answers = iter([RuntimeError('once'), raw_analysis(sheet)])

    case = await author_assessment_case(
        full_client(**{ANALYZE_TASK: lambda payload: next(answers)}),
        sheet,
        clinical=default_clinical(),
        microbiome=default_microbiome(),
        deadline=later(),
        audit={},
    )

    assert case['plan']['systems'][0]['rows']


@pytest.mark.asyncio
async def test_empty_analysis_of_a_case_with_clinical_data_counts_as_failed(sheet):
    client = full_client(**{ANALYZE_TASK: {'phenotype': {'label': 'x'}}})

    with pytest.raises(CaseAuthoringError):
        await author_assessment_case(client, sheet, deadline=later(), audit={})

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_empty_analysis_is_fine_for_a_case_without_clinical_data():
    sheet = build_sheet(facts=[], clinical={}, microbiome={})
    client = full_client(**{ANALYZE_TASK: {'phenotype': {'label': '暂无'}}})

    case = await author_assessment_case(client, sheet, deadline=later(), audit={})

    assert case['plan']['clusters'] == [] and [payload['task'] for payload in client.calls].count(ANALYZE_TASK) == 1
    assert 'conclusion.summary' in case['cells']


@pytest.mark.asyncio
async def test_non_dict_analysis_is_rejected(sheet):
    client = full_client(**{ANALYZE_TASK: ['not', 'a', 'dict']})

    with pytest.raises(CaseAuthoringError):
        await author_assessment_case(client, sheet, deadline=later(), audit={})


@pytest.mark.asyncio
async def test_a_deadline_that_has_already_passed_raises_without_calling_the_model(sheet):
    client = full_client()
    audit = {}

    with pytest.raises(CaseAuthoringError):
        await author_assessment_case(client, sheet, deadline=later(-1.0), audit=audit)

    assert client.calls == [] and audit['analysisError'] == 'TimeoutError'
    assert audit['calls'][0]['status'] == 'skipped_no_time'


@pytest.mark.asyncio
async def test_review_is_skipped_when_less_than_the_minimum_step_is_left(sheet):
    client = full_client()
    audit = {}

    case = await author_assessment_case(
        client, sheet, clinical=default_clinical(), microbiome=default_microbiome(), deadline=later(15.0), audit=audit
    )

    assert not client.tasks(REVIEW_TASK) and case['cells']
    assert audit['review']['skipped'] == 'no_time' and 'programFindings' in audit['review']


@pytest.mark.asyncio
async def test_writing_failures_do_not_stop_the_run(sheet):
    client = full_client(**{WRITE_TASK: lambda payload: RuntimeError('boom')})
    audit = {}

    case = await author_assessment_case(
        client, sheet, clinical=default_clinical(), microbiome=default_microbiome(), deadline=later(), audit=audit
    )

    assert case['cells'] == {} and audit['sections']['failed']
    assert case['plan']['clusters']


def test_flags_apply_priority_safety_net_is_the_plan_function():
    # the authoring module must use the plan module's net, not a copy
    from modules.report import assessment_case_authoring

    assert assessment_case_authoring.apply_priority_safety_net is apply_priority_safety_net
