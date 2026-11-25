"""Whole-case projection: the callback fields filled from the plan, the written cells and the program's views."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.report import assessment_presentation as legacy
from modules.report.assessment_case_authoring import section_jobs
from modules.report.assessment_case_plan import STATUS_LEVELS
from modules.report.assessment_case_plan import apply_priority_safety_net
from modules.report.assessment_case_plan import normalize_case_analysis
from modules.report.assessment_case_projection import CASE_PRESENTATION_VERSION
from modules.report.assessment_case_projection import NO_MICROBIOME_LEAD
from modules.report.assessment_case_projection import NO_QUESTIONNAIRE_LIFESTYLE
from modules.report.assessment_case_projection import build_case_presentation
from modules.report.assessment_case_projection import worst_level
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_facts
from tests.unit._support.assessment_case import default_microbiome
from tests.unit._support.assessment_case import document
from tests.unit._support.assessment_case import fact
from tests.unit._support.assessment_case import parsed
from tests.unit._support.assessment_case import raw_analysis
from tests.unit._support.assessment_case import request
from tests.unit._support.assessment_case import view_row

TIMING = {'clinicalDate': '2026-06-30', 'microbiomeSampleDate': '2026-11-02', 'daysBetween': 125}
FLAGS = {
    'priorityItems': [
        {
            'ruleId': 'RF-FOBT',
            'title': '隐血规则',
            'factIds': ['fact-fobt'],
            'timeframe': {'text': '2 周内', 'days': 14},
            'department': '消化内科',
            'checks': ['结肠镜'],
            'sources': [
                {'key': 'crc', 'title': '结直肠癌筛查指南', 'doi': '10.1/crc'},
                {'key': 'program_default', 'title': '本规则表的操作时限'},
                {'key': 'x', 'title': ''},
            ],
        }
    ]
}


def make_data(library=None):
    return SimpleNamespace(appendices=SimpleNamespace(reference_literature=[] if library is None else library))


def written(plan, sheet, clinical, microbiome):
    jobs = section_jobs(plan, sheet, clinical=clinical, microbiome=microbiome)
    return {cell['id']: f'文本[{cell["id"]}]' for job in jobs for cell in job['cells']}


class Case:
    """One projected case: the presentation plus everything it was built from."""

    def __init__(
        self,
        *,
        raw=None,
        facts=None,
        clinical=None,
        microbiome=None,
        req=None,
        flags=FLAGS,
        drop=(),
        cells=None,
        data=None,
        parsed_value=None,
        timing=TIMING,
    ):
        self.clinical = default_clinical() if clinical is None else clinical
        self.microbiome = default_microbiome() if microbiome is None else microbiome
        self.facts = default_facts() if facts is None else facts
        self.req = request(surveys=None) if req is None else req
        base = build_sheet(facts=default_facts(), clinical=default_clinical(), microbiome=default_microbiome())
        self.sheet = build_sheet(facts=self.facts, clinical=self.clinical, microbiome=self.microbiome, req=req)
        self.raw = raw if raw is not None else raw_analysis(base)
        self.plan = normalize_case_analysis(self.raw, self.sheet, clinical=self.clinical)
        apply_priority_safety_net(self.plan, self.sheet, flags)
        self.cells = written(self.plan, self.sheet, self.clinical, self.microbiome) if cells is None else cells
        for identifier in drop:
            self.cells.pop(identifier, None)
        self.data = data if data is not None else make_data()
        self.case = {'plan': self.plan, 'cells': self.cells}
        self.input_clinical = deepcopy(self.clinical)
        self.presentation = build_case_presentation(
            request=self.req,
            parsed=parsed_value or parsed(),
            document=document(*self.facts),
            data=self.data,
            clinical=self.clinical,
            microbiome=self.microbiome,
            sheet=self.sheet,
            case=self.case,
            timing=timing,
        )

    @property
    def overview(self):
        return self.presentation.overview

    @property
    def clinical_view(self):
        return self.presentation.clinical

    @property
    def micro(self):
        return self.presentation.microbiome

    @property
    def missing(self):
        return self.case['projection']['missingCells']

    def template_rows(self, code):
        group = next(item for item in self.clinical_view['clinicalGroups'] if item['code'] == code)
        return group['templateRows']


@pytest.fixture(scope='module')
def full():
    return Case()


NO_SURVEY = request(surveys=[])


# ---- general shape ------------------------------------------------------------------------------------------------


def test_presentation_is_the_existing_model_with_a_projection_record(full):
    assert isinstance(full.presentation, legacy.AssessmentPresentation)
    dumped = full.presentation.model_dump(by_alias=True)
    assert dumped['version'] == 'assessment.v1' and 'clusterSummary' in dumped and 'lifestyleFactors' in dumped
    assert full.case['projection'] == {'version': CASE_PRESENTATION_VERSION, 'missingCells': []}


def test_inputs_are_not_mutated_by_the_projection(full):
    assert 'bodyGroups' not in full.clinical and 'symptoms' not in full.clinical
    assert [group['code'] for group in full.clinical['clinicalGroups']] == ['cardiometabolic', 'organMetabolism']
    assert 'summary' not in full.microbiome['structureGroups'][0]


def test_worst_level_ranks_known_levels_only():
    assert worst_level(['normal', 'focus', 'borderline', 'bogus']) == 'focus'
    assert worst_level(['bogus', None]) is None and worst_level([]) is None


# ---- chapter one and three tables ---------------------------------------------------------------------------------


def test_body_groups_carry_name_status_level_reference_summary_and_display_rows(full):
    groups = full.clinical_view['bodyGroups']

    assert len(groups) == 1
    group = groups[0]
    assert (group['code'], group['name'], group['status'], group['level']) == (
        'body1',
        '体重指数',
        '高于理想值',
        'borderline',
    )
    assert group['comparisonReference'] == '<24' and group['summary'] == '文本[overview.bodyRow.1]'
    assert group['completion'] == 'complete' and 'recordedConclusions' not in group
    assert [(row['code'], row['name'], row['value'], row['unit']) for row in group['rows']] == [
        ('fact-bmi', '体重指数(BMI)', '29.3', 'kg/m2')
    ]
    assert full.overview['bodySummary'] == '文本[overview.bodySummary]'


def test_chapter_three_groups_have_template_rows_and_flat_program_rows(full):
    groups = {group['code']: group for group in full.clinical_view['clinicalGroups']}

    assert list(groups) == ['cardiometabolic', 'organMetabolism']
    cardio = groups['cardiometabolic']
    assert cardio['name'] == '糖脂代谢、胰岛素敏感性与心血管系统评估'
    assert (cardio['status'], cardio['level'], cardio['summary']) == (
        '重点关注',
        'focus',
        '文本[clinical.cardiometabolic.summary]',
    )
    assert cardio['calibrationNote'] == '文本[clinical.cardiometabolic.calibration]'
    assert [row['code'] for row in cardio['rows']] == ['fact-glu', 'fact-tg', 'derived-fact-tyg']  # the program view
    first = cardio['templateRows'][0]
    assert (first['code'], first['name'], first['status'], first['level']) == (
        'row1',
        '血脂与代谢',
        '重点关注',
        'focus',
    )
    assert first['comparisonReference'] == '<1.7' and first['summary'] == '文本[clinical.cardiometabolic.row.1]'
    assert [row['code'] for row in first['rows']] == ['fact-tg', 'derived-fact-tyg']
    assert first['anchors'] and first['anchors'][0]['name'] == '甘油三酯(TG)' and first['moreCount'] == 0
    assert first['recordedConclusions'] == [] and first['evidenceRefs'] == []
    assert [row['name'] for row in cardio['templateRows']][:2] == ['血脂与代谢', '血糖']


def test_group_without_program_rows_lists_the_members_of_its_template_rows():
    clinical = default_clinical()
    clinical['clinicalGroups'][0]['code'] = 'unknownGroup'  # no program group matches the system any more

    case = Case(clinical=clinical)

    group = next(item for item in case.clinical_view['clinicalGroups'] if item['code'] == 'cardiometabolic')
    assert group['rows'] and {row['code'] for row in group['rows']} == {
        row['code'] for template in group['templateRows'] for row in template['rows']
    }
    assert len({row['code'] for row in group['rows']}) == len(group['rows'])


def test_status_labels_map_to_levels_as_in_the_contract():
    assert STATUS_LEVELS == {
        '重点关注': 'focus',
        '亚临床边界': 'borderline',
        '高于理想值': 'borderline',
        '低于理想值': 'borderline',
        '稳健达标': 'normal',
        '未检测': 'notTested',
    }
    labels = ['重点关注', '亚临床边界', '高于理想值', '低于理想值', '稳健达标']
    raw = raw_analysis(build_sheet())
    raw['systems'] = [
        {
            'code': 'endocrineMusculoskeletal',
            'status': '亚临床边界',
            'rows': [{'title': f'行{n}', 'handles': ['L2'], 'status': label} for n, label in enumerate(labels)],
        }
    ]

    case = Case(raw=raw)

    rows = case.template_rows('endocrineMusculoskeletal')
    assert [(row['status'], row['level']) for row in rows[:5]] == [(label, STATUS_LEVELS[label]) for label in labels]


def test_member_rows_are_never_shown_more_severe_than_their_table_row():
    raw = raw_analysis(build_sheet())
    raw['systems'] = [
        {
            'code': 'cardiometabolic',
            'status': '高于理想值',
            'rows': [
                {'title': '血脂', 'handles': ['L3', 'D1'], 'status': '高于理想值'},  # L3 is a focus row in the view
                {'title': '影像', 'handles': ['L5'], 'status': '稳健达标'},
                {'title': '隐血', 'handles': ['L4'], 'status': '重点关注'},
            ],
        }
    ]
    case = Case(raw=raw)

    rows = {row['name']: row for row in case.template_rows('cardiometabolic')}

    assert [member['level'] for member in rows['血脂']['rows']] == ['borderline', 'borderline']
    assert {member['level'] for member in rows['影像']['rows']} == {'normal'}
    assert [member['level'] for member in rows['隐血']['rows']] == ['focus']
    assert case.sheet.entry('L3')['rows'][0]['level'] == 'focus'  # the sheet's own rows are untouched
    assert case.clinical['clinicalGroups'][0]['rows'][1]['level'] == 'focus'


def test_missing_interpretation_falls_back_to_a_listing_of_recorded_values_and_is_reported():
    case = Case(drop=['clinical.cardiometabolic.row.1', 'overview.bodyRow.1'])

    first = case.template_rows('cardiometabolic')[0]
    assert first['completion'] == 'source_only'
    assert first['summary'] == '甘油三酯(TG) 3.34 mmol/L（参考 0.00-1.70）；TyG 指数 8.9（参考 <8.5）。'
    assert case.template_rows('cardiometabolic')[1]['completion'] == 'complete'
    body = case.clinical_view['bodyGroups'][0]
    assert body['completion'] == 'source_only' and body['summary'] == '体重指数(BMI) 29.3 kg/m2。'
    assert case.missing == ['clinical.cardiometabolic.row.1', 'overview.bodyRow.1']


def test_a_row_without_any_recorded_value_has_no_fallback_text():
    clinical = default_clinical()
    clinical['clinicalGroups'][0]['rows'][1]['value'] = ''
    case = Case(clinical=clinical, drop=['clinical.cardiometabolic.row.1'])

    row = case.template_rows('cardiometabolic')[0]

    assert row['summary'] == 'TyG 指数 8.9（参考 <8.5）。'  # only rows with a value are listed


def test_recorded_conclusions_of_cited_advice_are_listed_on_the_row():
    raw = raw_analysis(build_sheet())
    raw['systems'][0]['rows'][0]['handles'] = ['L3', 'A1']

    case = Case(raw=raw)

    assert case.template_rows('cardiometabolic')[0]['recordedConclusions'] == ['建议复查血脂']


def test_multi_span_record_shows_only_the_spans_named_by_the_row_title():
    raw = raw_analysis(build_sheet())
    raw['systems'][1]['rows'] = [
        {'title': '胆囊息肉', 'handles': ['L5'], 'status': '亚临床边界'},
        {'title': '肝脏与肾脏', 'handles': ['L5'], 'status': '亚临床边界'},
        {'title': '影像所见', 'handles': ['L5'], 'status': '亚临床边界'},
    ]
    case = Case(raw=raw)

    rows = {row['name']: [member['code'] for member in row['rows']] for row in case.template_rows('organMetabolism')}

    assert rows['胆囊息肉'] == ['fact-us:span:c:d']
    assert rows['肝脏与肾脏'] == ['fact-us:span:a:b', 'fact-us:span:e:f']
    assert rows['影像所见'] == ['fact-us:span:a:b', 'fact-us:span:c:d', 'fact-us:span:e:f']  # the abnormal ones


def test_multi_span_record_with_normal_spans_collapses_to_one_row():
    clinical = default_clinical()
    for row in clinical['clinicalGroups'][1]['rows']:
        if ':span:' in row['code']:
            row.update(level='normal', direction=None)
    raw = raw_analysis(build_sheet())
    raw['systems'][1]['rows'] = [{'title': '影像所见', 'handles': ['L5'], 'status': '稳健达标'}]
    case = Case(raw=raw, clinical=clinical)

    row = next(item for item in case.template_rows('organMetabolism') if item['name'] == '影像所见')

    assert len(row['rows']) == 1
    collapsed = row['rows'][0]
    assert (collapsed['code'], collapsed['name'], collapsed['level']) == ('fact-us', '腹部超声', 'normal')
    assert collapsed['value'] == '脂肪肝；胆囊息肉 5mm；肾囊肿'


def test_a_record_with_more_than_four_named_spans_is_limited():
    clinical = default_clinical()
    clinical['clinicalGroups'][1]['rows'] += [
        view_row(f'fact-us:span:n{n}:x', f'腹部超声 · 结节{n}', f'结节{n}', level='borderline') for n in range(6)
    ]
    raw = raw_analysis(build_sheet())
    raw['systems'][1]['rows'] = [{'title': '结节', 'handles': ['L5'], 'status': '亚临床边界'}]

    case = Case(raw=raw, clinical=clinical)

    row = next(item for item in case.template_rows('organMetabolism') if item['name'] == '结节')
    assert len(row['rows']) == 4


def test_unflagged_program_rows_do_not_repeat_cited_specialty_findings():
    clinical = default_clinical()
    clinical['specialtyFindings'] = [
        view_row('fact-us:span:z:z', '腹部超声 · 其他', '已被引用', level='normal'),
        view_row('spec-1', '主检结论：肺结节', '建议随访', level='borderline'),
    ]

    case = Case(clinical=clinical)

    assert [row['code'] for row in case.clinical_view['specialtyFindings']] == ['spec-1']
    assert case.clinical_view['specialtyFindings'][0]['name'] == '肺结节'  # reader wording of program rows


def test_program_row_wording_is_cleaned_but_written_prose_is_left_as_reviewed():
    cells = None
    clinical = default_clinical()
    clinical['clinicalGroups'][0]['rows'][1]['name'] = '主检结论：甘油三酯(TG)'

    case = Case(clinical=clinical, cells=cells)
    case.cells['clinical.cardiometabolic.summary'] = '主检结论：由写作者给出，不改写'
    rebuilt = build_case_presentation(
        request=case.req,
        parsed=parsed(),
        document=document(*case.facts),
        data=make_data(),
        clinical=clinical,
        microbiome=case.microbiome,
        sheet=case.sheet,
        case=case.case,
        timing=TIMING,
    )

    flat = rebuilt.clinical['clinicalGroups'][0]
    assert flat['rows'][1]['name'] == '甘油三酯(TG)'
    assert flat['summary'] == '主检结论：由写作者给出，不改写'


# ---- chapter one overview -----------------------------------------------------------------------------------------


def test_systems_of_the_overview_follow_the_plan(full):
    systems = {system['code']: system for system in full.overview['systems']}

    assert (
        list(systems)
        == [
            'cardiometabolic',
            'digestiveRenal',
            'microbiome',
            'endocrineMusculoskeletal',
            'hematologyImmune',
            'otherClinical',
        ][:5]
    )
    cardio = systems['cardiometabolic']
    assert (cardio['status'], cardio['level']) == ('重点关注', 'focus')
    assert cardio['keyFindings'] == '文本[overview.system.cardiometabolic.evidence]'
    assert cardio['summary'] == '文本[overview.system.cardiometabolic.conclusion]'
    assert [row['name'] for row in cardio['rows']] == ['甘油三酯(TG)']
    assert cardio['anchors'][0]['name'] == '甘油三酯(TG)'
    untested = systems['hematologyImmune']
    assert (untested['status'], untested['level']) == ('未检测', 'notTested')
    assert untested['keyFindings'] is None and untested['summary'] is None and untested['rows'] == []
    assert untested['anchors'] == [] and untested['moreCount'] == 0


def test_other_system_is_published_under_the_legacy_code():
    raw = raw_analysis(build_sheet())
    raw['systems'].append(
        {
            'code': 'lung',
            'name': '呼吸系统',
            'status': '亚临床边界',
            'rows': [{'title': '肺结节', 'handles': ['L5'], 'status': '亚临床边界'}],
        }
    )

    case = Case(raw=raw)

    assert case.overview['systems'][-1]['code'] == 'otherClinical'
    assert case.overview['systems'][-1]['name'] == '呼吸系统'
    assert case.clinical_view['clinicalGroups'][-1]['code'] == 'otherClinical'


def test_phenotype_data_notes_and_timing(full):
    assert full.overview['phenotype'] == '代谢性脂肪肝倾向'
    assert full.overview['dataNotes'] == [{'kind': 'calibration', 'name': '读数校准', 'text': '以空腹读数为准'}]
    assert full.overview['dataTiming'] == TIMING
    assert full.overview['preface'] == legacy.ASSESSMENT_PREFACE
    assert [item['name'] for item in full.overview['readingGuide']] == [
        name for name, _ in legacy.ASSESSMENT_READING_GUIDE
    ]


def test_timing_is_read_from_the_request_when_not_given():
    req = SimpleNamespace(
        survey_list=[], hospital_data=[SimpleNamespace(check_time='2026-06-30 08:00', model_extra={})]
    )

    case = Case(req=req, timing=None)

    assert case.overview['dataTiming']['clinicalDate'] == '2026-06-30'
    assert case.overview['dataTiming']['microbiomeSampleDate'] == '2026-11-02'
    assert case.overview['dataTiming']['daysBetween'] == 125


def test_priority_findings_have_urgent_routine_and_gaps_with_item_fields(full):
    urgent, routine, gaps = full.overview['priorityFindings']

    assert [section['code'] for section in (urgent, routine, gaps)] == ['urgent', 'routine', 'gaps']
    assert (urgent['name'], urgent['priority'], urgent['text']) == ('优先排查', 'P0', '文本[overview.priority.1]')
    item = urgent['items'][0]
    assert (item['code'], item['name'], item['priority'], item['department'], item['checks']) == (
        'RF-FOBT',
        '粪便隐血查因',
        'P0',
        '消化内科',
        ['结肠镜'],
    )
    assert item['timeframe'] == {'text': '2 周内', 'days': 14}  # tightened by the rule
    assert (item['clusterId'], item['origin'], item['ruleId']) == ('C1', 'model', 'RF-FOBT')
    assert item['text'] == '文本[overview.priority.1]' and item['basisRows'][0]['name'] == '粪便隐血'
    assert item['evidenceRefs'] == [2] == urgent['evidenceRefs']  # number 1 is the checked target of chapter three
    assert (urgent['timeframe'], urgent['department'], urgent['action']) == (
        item['timeframe'],
        '消化内科',
        urgent['text'],
    )
    follow = routine['items'][0]
    assert (follow['code'], follow['name'], follow['priority'], follow['timeframe']) == (
        'followup-1',
        '胆囊息肉',
        None,
        {'text': '每 6 个月', 'days': 180},
    )
    assert routine['priority'] is None and follow['text'] == '文本[overview.followup.1]'
    gap = gaps['items'][0]
    assert (gap['code'], gap['name'], gap['reason'], gap['text']) == (
        'gap-1',
        '糖耐量试验',
        '空腹血糖接近上限',
        '文本[overview.gap.1]',
    )
    assert gap['timeframe'] is None and gap['checks'] == []


def test_gaps_section_exists_only_when_there_are_gaps():
    raw = raw_analysis(build_sheet())
    raw['testGaps'] = []

    case = Case(raw=raw)

    assert [section['code'] for section in case.overview['priorityFindings']] == ['urgent', 'routine']


def test_no_priority_items_and_no_followups_use_the_fixed_statements():
    raw = raw_analysis(build_sheet())
    raw.update(priorityItems=[], followups=[])

    case = Case(raw=raw, flags=None)

    urgent, routine = case.overview['priorityFindings'][:2]
    assert urgent['text'] == legacy._NO_URGENT and urgent['items'] == [] and urgent['priority'] is None
    assert routine['text'] == legacy._NO_ROUTINE and routine['items'] == []
    assert (urgent['timeframe'], urgent['department'], urgent['evidenceRefs']) == (None, None, [])
    assert case.presentation.conclusion['managementDirections'][0]['text'] is None  # no "finish the checks first"


def test_missing_item_texts_are_composed_from_the_plan_and_reported():
    case = Case(drop=['overview.priority.1', 'overview.followup.1', 'overview.gap.1'])

    urgent, routine, gaps = case.overview['priorityFindings']

    assert urgent['items'][0]['text'] == '粪便隐血查因：2 周内就诊，消化内科，完成结肠镜。'
    assert routine['items'][0]['text'] == '胆囊息肉，肝胆外科随访，每 6 个月。'
    assert gaps['items'][0]['text'] == '因空腹血糖接近上限，建议补查糖耐量试验。'
    assert case.missing == ['overview.priority.1']  # only the priority text is reported as missing


def test_safety_net_item_projects_with_its_rule_id_and_without_model_cluster():
    flags = {
        'priorityItems': [
            {
                'ruleId': 'RF-US',
                'title': '超声规则',
                'factIds': ['fact-us'],
                'timeframe': {'text': '1 个月内', 'days': 30},
                'department': '肝胆外科',
                'checks': [],
                'sources': [],
            }
        ]
    }

    case = Case(flags=flags)

    items = case.overview['priorityFindings'][0]['items']
    assert [(item['code'], item['origin']) for item in items] == [('priority-1', 'model'), ('RF-US', 'safety_net')]
    assert 'ruleId' not in items[0] and items[1]['ruleId'] == 'RF-US'


def test_rule_sources_are_numbered_in_the_bibliography_once():
    library = []
    data = make_data(library)

    first = Case(data=data)
    second = Case(data=data)  # the projection can be rebuilt (checkpoint recovery) without growing the bibliography

    assert [(entry.number, entry.title) for entry in library] == [(1, 'ADA 糖尿病诊疗标准'), (2, '结直肠癌筛查指南')]
    assert (
        first.overview['priorityFindings'][0]['evidenceRefs']
        == [2]
        == second.overview['priorityFindings'][0]['evidenceRefs']
    )
    assert library[1].study_type == '指南' and library[1].supporting_sections == [
        'assessmentPresentation.rule.priority'
    ]


def test_cluster_evidence_refs_come_from_the_priority_items_of_the_cluster(full):
    by_id = {cluster['id']: cluster for cluster in full.presentation.clusters}

    assert by_id['C1']['evidenceRefs'] == [2]  # the P0 cluster that carries the rule hit
    assert by_id['C2']['evidenceRefs'] == []


def test_checked_target_literature_is_cited_on_the_display_rows(full):
    group = full.clinical_view['clinicalGroups'][0]
    glu = next(row for row in group['rows'] if row['code'] == 'fact-glu')

    assert glu['target']['evidenceRefs'] == [1]
    template = next(row for row in group['templateRows'] if row['name'] == '血糖')
    assert template['rows'][0]['target']['evidenceRefs'] == [1]  # the sheet row copy carries the number too


def test_missing_bibliography_is_tolerated():
    case = Case(data=SimpleNamespace())

    assert case.overview['priorityFindings'][0]['items'][0]['evidenceRefs'] == []


# ---- chapter two --------------------------------------------------------------------------------------------------


def test_symptoms_are_filled_from_the_plan_and_cells(full):
    symptom = full.clinical_view['symptoms'][0]

    assert (symptom['code'], symptom['name'], symptom['value'], symptom['status']) == (
        'S1',
        '乏力腹胀',
        '乏力腹胀',
        '本人自述',
    )
    assert symptom['history'] == '文本[symptoms.1.history]'
    assert symptom['interpretationTitle'] == '文本[symptoms.1.title]'
    assert symptom['interpretation'] == '文本[symptoms.1.explanation]'
    assert symptom['clinicalEvidence'] == '文本[symptoms.1.clinicalEvidence]'
    assert symptom['microbiomeEvidence'] == '文本[symptoms.1.microbiomeEvidence]'
    assert symptom['question'] == '近期不适' and symptom['sourceKind'] == 'selfReported'
    assert [signal['name'] for signal in symptom['clinicalSignals']] == ['甘油三酯(TG)']
    assert [signal['name'] for signal in symptom['microbiomeSignals']] == ['阿克曼菌']
    assert full.clinical_view['symptomsLead'] == '文本[symptoms.lead]'
    assert full.clinical_view['symptomSummary'] == '文本[symptoms.summary]'


def test_without_symptoms_the_fixed_statements_are_used():
    raw = raw_analysis(build_sheet())
    raw['symptoms'] = []

    case = Case(raw=raw)

    assert case.clinical_view['symptoms'] == []
    assert case.clinical_view['symptomsLead'] == legacy.NO_SYMPTOM_LEAD
    assert case.clinical_view['symptomSummary'] == legacy.NO_SYMPTOM_SUMMARY


def test_missing_symptom_explanation_is_reported():
    case = Case(drop=['symptoms.1.explanation'])

    assert case.clinical_view['symptoms'][0]['interpretation'] is None
    assert case.missing == ['symptoms.1.explanation']


def test_symptom_question_label_loses_the_second_person():
    survey_schema = {
        'title': '问卷',
        'pages': [{'elements': [{'name': 'q', 'title': '您近期有哪些不适？', 'choices': ['乏力']}]}],
    }
    from tests.unit._support.assessment_case import survey

    req = request(surveys=[survey(answer={'q': '乏力'}, schema=survey_schema)])
    base = build_sheet(req=req)
    raw = {
        'symptoms': [{'name': '乏力', 'handles': ['Q1']}],
        'systems': [
            {'code': 'cardiometabolic', 'status': '亚临床边界', 'rows': [{'title': '血糖', 'handles': ['L2']}]}
        ],
    }

    case = Case(raw=raw, req=req)

    assert base.entry('Q1')['name'] == '您近期有哪些不适？'
    assert '您' not in case.clinical_view['symptoms'][0]['question']


# ---- chapters one to four without material ------------------------------------------------------------------------


def test_without_microbiome_chapter_four_and_the_formula_direction_say_so():
    case = Case(microbiome={})

    assert case.micro['overviewSummary'] == NO_MICROBIOME_LEAD
    assert case.micro['sampleSummary'] is None and case.micro['fermentationSummary'] is None
    assert case.micro['structureSummary'] is None and case.micro['riskSummary'] is None
    direction = case.presentation.conclusion['managementDirections'][0]
    assert direction == {'name': legacy._MTPN_NAME, 'items': [], 'text': legacy._MTPN_NO_MICROBIOME}
    assert not [identifier for identifier in case.missing if identifier.startswith('micro.')]
    # Chapter four then only carries its statement: the digestive examinations stay in chapter three.
    assert case.clinical_view['digestiveContext'] == []


def test_with_microbiome_chapter_four_and_the_formula_direction_are_filled(full):
    assert full.micro['overviewSummary'] == '文本[micro.lead]'
    assert full.micro['sampleSummary'] == '文本[micro.sample]'
    assert full.micro['fermentationSummary'] == '文本[micro.fermentation]'
    assert full.micro['structureSummary'] == '文本[micro.structureSummary]'
    assert full.micro['riskSummary'] == '文本[micro.risk]'
    assert full.micro['structureGroups'][0]['summary'] == '文本[micro.structure.diversity]'
    assert full.micro['organismGroups'][0]['summary'] == '文本[micro.organisms.beneficial]'
    assert full.micro['functionalGroups'][0]['summary'] == '文本[micro.function.scfa]'
    direction = full.presentation.conclusion['managementDirections'][0]
    assert direction['name'] == legacy._MTPN_NAME
    assert [(item['code'], item['text']) for item in direction['items']] == [
        ('probiotic', '文本[conclusion.probiotic]'),
        ('fiber', '文本[conclusion.fiber]'),
        ('nutrient', '文本[conclusion.nutrient]'),
    ]
    assert direction['text'] == legacy._MTPN_AFTER_P0  # a priority item is open


def test_disease_model_summary_is_appended_to_the_risk_text():
    result = SimpleNamespace(name='肠癌模型', source_score='0.3', source_level='低风险')

    case = Case(parsed_value=SimpleNamespace(subject=parsed().subject, disease_results=[result]))

    assert case.micro['riskSummary'].startswith(
        '文本[micro.risk]\n\n菌群疾病关联模型的分档及指数：低风险：肠癌模型（0.3）'
    )


def test_digestive_context_groups_get_their_summaries():
    clinical = default_clinical()
    clinical['digestiveContext'] = [{'code': 'oral', 'name': '口腔', 'rows': [view_row('f-oral', '牙石', '1度')]}]

    case = Case(clinical=clinical)

    assert case.clinical_view['digestiveContext'][0]['summary'] == '文本[micro.digestive.oral]'


def test_missing_group_summaries_of_chapter_four_are_reported():
    case = Case(drop=['micro.structure.diversity', 'micro.function.scfa', 'overview.system.cardiometabolic.conclusion'])

    assert case.micro['structureGroups'][0]['summary'] is None
    assert case.missing == [
        'micro.function.scfa',
        'micro.structure.diversity',
        'overview.system.cardiometabolic.conclusion',
    ]


def test_without_clinical_data_chapters_one_and_three_state_it():
    case = Case(facts=[], clinical={}, req=NO_SURVEY, raw={})

    assert case.clinical_view['summary'] == legacy.NO_CLINICAL_SUMMARY
    assert case.clinical_view['baselineSummary'] == legacy.NO_BASELINE_SUMMARY
    assert case.clinical_view['trendSummary'] == legacy.NO_TREND_STATEMENT
    assert case.clinical_view['specialtySummary'] is None and case.clinical_view['clinicalGroups'] == []
    assert case.overview['bodySummary'] == legacy.NO_BODY_SUMMARY


def test_with_clinical_data_chapter_three_frame_cells_are_used(full):
    assert full.clinical_view['summary'] == '文本[clinicalFrame.lead]'
    assert full.clinical_view['specialtySummary'] == '文本[clinicalFrame.notIncluded]'
    assert full.clinical_view['trendSummary'] == '文本[clinicalFrame.trend]'
    assert full.clinical_view['baselineSummary'] == '文本[clinicalFrame.baseline]'


def test_trend_statement_is_omitted_when_the_view_has_trends():
    clinical = default_clinical()
    clinical['trends'] = [{'code': 't1'}]

    case = Case(clinical=clinical, drop=['clinicalFrame.trend'])

    assert case.clinical_view['trendSummary'] is None


# ---- chapters five and six ----------------------------------------------------------------------------------------


def test_clusters_are_filled_with_signals_and_the_graph_fields(full):
    clusters = full.presentation.clusters

    assert [(cluster['id'], cluster['name'], cluster['priority']) for cluster in clusters] == [
        ('C1', '隐血—消化道', 'P0'),
        ('C2', '糖脂代谢—脂肪肝', 'P1'),
    ]
    second = clusters[1]
    assert second['interpretation'] == '文本[clusters.C2.impact]' and second['action'] == '文本[clusters.C2.action]'
    assert second['clinicalEvidence'] == '文本[clusters.C2.clinicalEvidence]'
    assert second['microbiomeEvidence'] == '文本[clusters.C2.microbiomeEvidence]'
    assert [signal['name'] for signal in second['clinicalSignals']][:2] == ['甘油三酯(TG)', '肝脏']
    assert [signal['name'] for signal in second['microbiomeSignals']] == ['阿克曼菌']
    assert second['reportedSignals'] == [
        {
            'name': '睡眠情况',
            'value': '入睡困难',
            'unit': None,
            'referenceRange': None,
            'status': '本人自述',
            'observedAt': None,
            'level': None,
            'direction': None,
            'evidenceRefs': [],
            'sourceKind': 'selfReported',
        }
    ]
    assert full.presentation.cluster_summary == '文本[clusters.lead]'
    assert full.overview['graphHub'] == '核心健康主题' and full.overview['graphClusterIds'] == ['C1', 'C2']
    assert full.overview['graphSummary'] == '文本[overview.graphSummary]'


def test_without_clusters_the_fixed_statements_are_used():
    case = Case(raw={'systems': []}, flags=None)

    assert case.presentation.clusters == []
    assert case.presentation.cluster_summary == legacy._NO_CLUSTERS
    assert case.overview['graphSummary'] == legacy._NO_GRAPH and case.overview['graphHub'] is None
    assert case.overview['graphClusterIds'] == []
    assert [layer['clusterIds'] for layer in case.presentation.conclusion['layers']] == [[], [], []]


def test_missing_cluster_interpretation_is_reported():
    case = Case(drop=['clusters.C1.impact'])

    assert case.presentation.clusters[0]['interpretation'] is None
    assert case.missing == ['clusters.C1.impact']


def test_lifestyle_factors_background_and_recognitions(full):
    assert full.presentation.lifestyle_factors == [
        {
            'code': 'sleep',
            'name': '睡眠节律与夜间恢复',
            'kind': 'habit',
            'value': '文本[lifestyle.sleep.behavior]',
            'interpretation': '文本[lifestyle.sleep.effect]',
            'clusterIds': ['C2'],
            'evidenceRefs': [],
        }
    ]
    assert full.presentation.lifestyle_summary == '文本[lifestyle.background]'
    assert full.presentation.lifestyle_recognition == [
        {'code': 'recognition1', 'name': '关于血糖的校准', 'text': '文本[lifestyle.recognition.1]', 'evidenceRefs': []}
    ]


def test_recognition_without_text_is_left_out():
    case = Case(drop=['lifestyle.recognition.1'])

    assert case.presentation.lifestyle_recognition == []


def test_lifestyle_statements_depend_on_whether_a_questionnaire_exists():
    raw = {'systems': []}

    without = Case(raw=raw, req=NO_SURVEY, flags=None)
    with_questionnaire = Case(raw=raw, flags=None)

    assert without.presentation.lifestyle_summary == NO_QUESTIONNAIRE_LIFESTYLE
    assert with_questionnaire.presentation.lifestyle_summary == legacy.NO_LIFESTYLE_SUMMARY
    assert without.presentation.lifestyle_factors == []


def test_three_conclusion_layers_list_their_clusters_by_priority(full):
    conclusion = full.presentation.conclusion

    assert [(layer['code'], layer['name'], layer['text'], layer['clusterIds']) for layer in conclusion['layers']] == [
        ('clinical', legacy._CONCLUSION_LAYERS[0][1], '文本[conclusion.layer.clinical]', ['C1']),
        ('core', legacy._CONCLUSION_LAYERS[1][1], '文本[conclusion.layer.core]', ['C2']),
        ('maintenance', legacy._CONCLUSION_LAYERS[2][1], '文本[conclusion.layer.maintenance]', []),
    ]
    assert conclusion['summary'] == '文本[conclusion.summary]'
    assert [(item['id'], item['priority'], item['action']) for item in conclusion['priorities']] == [
        ('C1', 'P0', '文本[clusters.C1.action]'),
        ('C2', 'P1', '文本[clusters.C2.action]'),
    ]
    lifestyle = conclusion['managementDirections'][1]
    assert (lifestyle['code'], lifestyle['name'], lifestyle['text']) == (
        'lifestyle',
        legacy._LSMI_NAME,
        '文本[conclusion.lifestyle]',
    )


def test_missing_layer_and_lifestyle_texts_use_the_fixed_statements():
    case = Case(
        drop=[
            'conclusion.layer.clinical',
            'conclusion.layer.maintenance',
            'conclusion.lifestyle',
            'conclusion.probiotic',
        ]
    )

    conclusion = case.presentation.conclusion
    assert conclusion['layers'][0]['text'] == legacy._LAYER_EMPTY['clinical']
    assert conclusion['layers'][2]['text'] == legacy._LAYER_EMPTY['maintenance']
    assert conclusion['managementDirections'][1]['text'] == legacy._LSMI_NO_FACTORS
    assert [item['code'] for item in conclusion['managementDirections'][0]['items']] == ['fiber', 'nutrient']


# ---- signals ------------------------------------------------------------------------------------------------------


def test_signals_of_each_record_kind():
    raw = raw_analysis(build_sheet())
    raw['clusters'][0]['clinicalHandles'] = ['L1', 'A1', 'L99']
    raw['clusters'][0]['reportedHandles'] = ['Q2']

    case = Case(raw=raw)

    cluster = next(item for item in case.presentation.clusters if item['name'] == '糖脂代谢—脂肪肝')
    by_name = {signal['name']: signal for signal in cluster['clinicalSignals']}
    assert by_name['体重指数(BMI)']['level'] == 'borderline' and by_name['体重指数(BMI)']['value'] == '29.3'
    assert by_name['建议复查血脂'] == {
        'name': '建议复查血脂',
        'value': None,
        'unit': None,
        'referenceRange': None,
        'status': '体检结论',
        'observedAt': '2026-06-30',
        'level': None,
        'direction': None,
        'evidenceRefs': [],
        'kind': 'advice',
    }
    assert cluster['reportedSignals'][0]['value'] == '乏力、腹胀'


def test_long_values_are_not_shown_as_signal_values():
    facts = [*default_facts(), fact('fact-long', '长文本项目', '很' * 40, section='内科')]
    clinical = default_clinical()
    raw = raw_analysis(build_sheet())
    raw['clusters'][0]['clinicalHandles'] = ['L6']

    case = Case(raw=raw, facts=facts, clinical=clinical)

    cluster = next(item for item in case.presentation.clusters if item['name'] == '糖脂代谢—脂肪肝')
    signal = next(item for item in cluster['clinicalSignals'] if item['name'] == '长文本项目')
    assert signal['value'] is None  # too long for a chip; the listing carries the full text elsewhere
