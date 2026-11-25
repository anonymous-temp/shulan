"""Report glue of the whole-case path: legacy blocks, audit summaries, case inputs and the authoring entry point."""

import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from modules.report import assessment_case_report as glue
from modules.report.assessment_case_authoring import ANALYZE_TASK
from modules.report.assessment_case_authoring import REVIEW_TASK
from modules.report.assessment_case_authoring import WRITE_TASK
from modules.report.assessment_case_authoring import section_jobs
from modules.report.assessment_case_plan import apply_priority_safety_net
from modules.report.assessment_case_plan import normalize_case_analysis
from modules.report.assessment_case_report import CASE_AUTHORING_BUDGET_SECONDS
from modules.report.assessment_case_report import CHAPTER_FALLBACK_MIN_SECONDS
from modules.report.assessment_case_report import FORMER_PATH_REASONS
from modules.report.assessment_case_report import author_report_case
from modules.report.assessment_case_report import build_report_case_presentation
from modules.report.assessment_case_report import case_audit_summary
from modules.report.assessment_case_report import case_incomplete
from modules.report.assessment_case_report import case_inputs
from modules.report.assessment_case_report import legacy_case_blocks
from modules.report.assessment_presentation import AssessmentPresentation
from tests.unit._support.assessment_case import FakeClient
from tests.unit._support.assessment_case import build_sheet
from tests.unit._support.assessment_case import default_clinical
from tests.unit._support.assessment_case import default_facts
from tests.unit._support.assessment_case import default_microbiome
from tests.unit._support.assessment_case import document
from tests.unit._support.assessment_case import parsed
from tests.unit._support.assessment_case import raw_analysis
from tests.unit._support.assessment_case import survey

SUMMARY_PATH = ['health_overview', 'overall_summary', 'text']
STRUCTURE_PATH = ['microbiome_assessment', 'structure_interpretation', 'text']
CONCLUSION_PATH = ['comprehensive_conclusion', 'overall_conclusion', 'text']


def target(identifier, path, *, sources=('f1', 'f2'), primary=('f1',), roles=('interpretation', 'fact')):
    return {
        'id': identifier,
        'path': list(path),
        'allowedRoles': list(roles),
        'sourceFactIds': list(sources),
        'owner': {'primaryFactIds': list(primary)},
    }


def blueprint(*chapters, facts=('f1', 'f2', 'f3')):
    return SimpleNamespace(
        facts=[{'factId': fact_id} for fact_id in facts] + [{'name': 'no id'}],
        chapters=[{'code': code, 'targets': targets} for code, targets in chapters],
    )


CELLS = {'overview.summary': '总览正文', 'micro.structureSummary': '菌群正文', 'conclusion.summary': '结论正文'}


# ---- constants ----------------------------------------------------------------------------------------------------


def test_budget_constants_are_consistent():
    assert glue.CASE_MAINLINE == 'assessment-case-v1'
    assert CHAPTER_FALLBACK_MIN_SECONDS < CASE_AUTHORING_BUDGET_SECONDS
    assert FORMER_PATH_REASONS and all(reason.isupper() for reason in FORMER_PATH_REASONS)
    assert 'REPORT_LAYOUT_INCOMPLETE' in FORMER_PATH_REASONS


# ---- legacy_case_blocks -------------------------------------------------------------------------------------------


def test_one_interpretation_block_per_legacy_path_with_an_existing_cell():
    plan = blueprint(
        ('overview', [target('t-sum', SUMMARY_PATH), target('t-other', ['health_overview', 'other', 'text'])]),
        ('micro', [target('t-micro', STRUCTURE_PATH, sources=('f2', 'f3'), primary=('f3',))]),
        ('conclusion', [target('t-end', CONCLUSION_PATH)]),
    )

    blocks = legacy_case_blocks(plan, {'cells': CELLS})

    assert set(blocks) == {'overview', 'micro', 'conclusion'}
    assert blocks['overview'] == [
        {
            'targetId': 't-sum',
            'completion': 'complete',
            'actionDetails': [],
            'sentences': [
                {
                    'text': '总览正文',
                    'factIds': ['f1'],
                    'role': 'interpretation',
                    'evidence': [],
                    'support': 'clinical_synthesis',
                }
            ],
        }
    ]
    assert blocks['micro'][0]['sentences'][0]['text'] == '菌群正文'
    assert blocks['micro'][0]['sentences'][0]['factIds'] == ['f3']
    assert blocks['conclusion'][0]['targetId'] == 't-end'


def test_binding_uses_existing_primary_facts_first_up_to_six():
    sources = [f'f{n}' for n in range(1, 10)]
    plan = blueprint(
        (
            'overview',
            [
                target(
                    't', SUMMARY_PATH, sources=sources, primary=['f9', 'f2', 'f7', 'f4', 'f5', 'f6', 'f8', 'f1', 'zz']
                )
            ],
        ),
        facts=sources,
    )

    block = legacy_case_blocks(plan, {'cells': CELLS})['overview'][0]

    assert block['sentences'][0]['factIds'] == ['f9', 'f2', 'f7', 'f4', 'f5', 'f6']


def test_binding_falls_back_to_the_first_allowed_fact_when_no_primary_fact_is_usable():
    plan = blueprint(('overview', [target('t', SUMMARY_PATH, sources=('f3', 'f2', 'gone'), primary=('gone',))]))

    block = legacy_case_blocks(plan, {'cells': CELLS})['overview'][0]

    assert block['sentences'][0]['factIds'] == ['f2']  # sorted, and only facts that exist in the blueprint


def test_targets_without_a_cell_or_a_usable_fact_or_the_interpretation_role_are_skipped():
    plan = blueprint(
        ('micro', [target('no-cell', STRUCTURE_PATH)]),
        (
            'overview',
            [
                target('no-fact', SUMMARY_PATH, sources=('gone',), primary=('gone',)),
                target('no-source', SUMMARY_PATH, sources=(), primary=()),
                target('wrong-role', SUMMARY_PATH, roles=('fact',)),
                target('no-roles', SUMMARY_PATH, roles=()),
            ],
        ),
        ('conclusion', [target('empty-cell', CONCLUSION_PATH)]),
    )

    blocks = legacy_case_blocks(plan, {'cells': {'overview.summary': '总览正文', 'conclusion.summary': ''}})

    assert blocks == {}


def test_missing_cells_key_and_empty_case_give_no_blocks():
    plan = blueprint(('overview', [target('t', SUMMARY_PATH)]))

    assert legacy_case_blocks(plan, {}) == {} and legacy_case_blocks(plan, {'cells': None}) == {}


def test_a_chapter_can_hold_several_blocks_and_targets_without_roles_key_are_skipped():
    bare = {'id': 'bare', 'path': SUMMARY_PATH, 'sourceFactIds': ['f1']}
    plan = blueprint(('overview', [target('a', SUMMARY_PATH), target('b', SUMMARY_PATH, primary=('f2',)), bare]))

    blocks = legacy_case_blocks(plan, {'cells': CELLS})

    assert [block['targetId'] for block in blocks['overview']] == ['a', 'b']


# ---- case_incomplete ----------------------------------------------------------------------------------------------


def test_incomplete_lists_failed_sections_then_missing_cells():
    case = {
        'audit': {'sections': {'failed': ['lifestyle', 'clinical.cardiometabolic']}},
        'projection': {'missingCells': ['clusters.C1.impact', 'symptoms.1.explanation']},
    }

    assert case_incomplete(case) == [
        'section:lifestyle',
        'section:clinical.cardiometabolic',
        'clusters.C1.impact',
        'symptoms.1.explanation',
    ]


@pytest.mark.parametrize(
    'case',
    [
        {},
        {'audit': None, 'projection': None},
        {'audit': {}, 'projection': {}},
        {'audit': {'sections': {'failed': None}}, 'projection': {'missingCells': None}},
    ],
)
def test_incomplete_is_empty_for_a_complete_or_unprojected_case(case):
    assert case_incomplete(case) == []


# ---- case_audit_summary -------------------------------------------------------------------------------------------


def test_audit_summary_replaces_per_cell_findings_by_counts_and_keeps_the_rest():
    audit = {
        'guideVersion': 'g1',
        'plan': {'clusters': []},
        'sections': {'failed': []},
        'review': {
            'programFindings': {'overview.summary': {'unverifiedNumbers': ['33.3']}, 'micro.lead': {}},
            'remainingFindings': {'overview.summary': {'unverifiedNumbers': ['33.3']}},
            'revised': [{'id': 'overview.summary', 'problem': 'p'}],
            'failedGroups': [['micro']],
        },
    }
    case = {'audit': audit, 'plan': {'audit': {'dropped': [{'scope': 'x'}]}}, 'projection': {'missingCells': ['a']}}
    before = deepcopy(case)

    summary = case_audit_summary(case)

    assert summary['review']['programFindings'] == {'cells': 2} and summary['review']['remainingFindings'] == {
        'cells': 1
    }
    assert summary['review']['revised'] == [{'id': 'overview.summary', 'problem': 'p'}]
    assert summary['review']['failedGroups'] == [['micro']]
    assert summary['planAudit'] == {'dropped': [{'scope': 'x'}]} and summary['projection'] == {'missingCells': ['a']}
    assert summary['guideVersion'] == 'g1' and summary['plan'] == {'clusters': []}
    assert case == before  # the stored audit keeps its details


def test_audit_summary_of_a_skipped_review_and_of_an_empty_case():
    skipped = case_audit_summary({'audit': {'review': {'skipped': 'no_time', 'programFindings': {'a': {}}}}})

    assert skipped['review'] == {'skipped': 'no_time', 'programFindings': {'cells': 1}}
    assert case_audit_summary({}) == {'review': {}, 'planAudit': None, 'projection': None}


# ---- case_inputs / author_report_case / build_report_case_presentation --------------------------------------------


class Env:
    """The program views behind ``case_inputs`` replaced by the synthetic ones of the fixtures."""

    def __init__(self, monkeypatch, *, facts=None, clinical=None, microbiome=None, flags=None, flags_error=None):
        self.facts = default_facts() if facts is None else facts
        self.clinical_view = default_clinical() if clinical is None else clinical
        self.clinical_view['_readerOmittedCells'] = ['dropped']
        self.microbiome_view = default_microbiome() if microbiome is None else microbiome
        self.flag_calls = []
        self.flags = flags if flags is not None else {'priorityItems': [], 'routineFollowups': []}
        self.view_calls = []

        def clinical_view(req, parsed_value, doc, data, *, reader_plan=False):
            self.view_calls.append(('clinical', reader_plan))
            return self.clinical_view

        def microbiome_view(parsed_value, data, *, source_facts=()):
            self.view_calls.append(('microbiome', len(source_facts)))
            return self.microbiome_view

        def red_flags(facts, subject=None, problems=()):
            self.flag_calls.append((list(facts), subject, problems))
            if flags_error:
                raise flags_error
            return self.flags

        monkeypatch.setattr('modules.report.assessment_clinical_view.build_clinical_view', clinical_view)
        monkeypatch.setattr('modules.report.assessment_microbiome_view.build_microbiome_view', microbiome_view)
        monkeypatch.setattr(
            'modules.report.assessment_microbiome_view.annotate_microbiome_chart_fields', lambda view: view
        )
        monkeypatch.setattr('modules.report.assessment_red_flags.evaluate_red_flags', red_flags)
        self.req = SimpleNamespace(
            survey_list=[survey()], hospital_data=[SimpleNamespace(check_time='2026-06-30 09:00', model_extra={})]
        )
        self.document = document(*self.facts)
        self.blueprint = SimpleNamespace(
            facts=[{'factId': 'fact-tg'}], data=SimpleNamespace(appendices=SimpleNamespace(reference_literature=[]))
        )
        self.state = SimpleNamespace(
            request=self.req,
            parsed=parsed(),
            document=self.document,
            blueprint=self.blueprint,
            audit={},
            stage_timings={},
        )

    def inputs(self):
        return case_inputs(
            request=self.req,
            parsed=self.state.parsed,
            document=self.document,
            data=self.blueprint.data,
            blueprint=self.blueprint,
        )


def full_client(**overrides):
    def analyze(payload):
        return raw_analysis(build_sheet())

    def write(payload):
        return {'cells': {cell['id']: f'文本[{cell["id"]}]' for cell in payload['cells']}}

    handlers = {ANALYZE_TASK: analyze, WRITE_TASK: write, REVIEW_TASK: lambda payload: {}}
    handlers.update(overrides)
    return FakeClient(**handlers)


def test_case_inputs_build_views_sheet_flags_and_timing(monkeypatch):
    env = Env(monkeypatch, flags={'priorityItems': [{'ruleId': 'R'}], 'routineFollowups': []})

    inputs = env.inputs()

    assert set(inputs) == {'clinical', 'microbiome', 'sheet', 'flags', 'timing'}
    assert '_readerOmittedCells' not in inputs['clinical']
    assert env.view_calls == [('clinical', True), ('microbiome', len(env.facts))]
    assert inputs['sheet'].has_clinical and inputs['sheet'].has_microbiome
    assert inputs['sheet'].entry('L3')['name'] == '甘油三酯(TG)'
    assert inputs['timing']['clinicalDate'] == '2026-06-30' and inputs['timing']['microbiomeSampleDate'] == '2026-11-02'
    assert '临床体检日期 2026-06-30' in inputs['sheet'].text
    assert inputs['flags'] == {'priorityItems': [{'ruleId': 'R'}], 'routineFollowups': []}
    facts, subject, problems = env.flag_calls[0]
    assert facts == [{'factId': 'fact-tg'}] and subject is env.state.parsed.subject and problems == ()


def test_case_inputs_survive_a_failing_safety_net(monkeypatch):
    env = Env(monkeypatch, flags_error=RuntimeError('rules broken'))

    inputs = env.inputs()

    assert inputs['flags'] == {'priorityItems': [], 'routineFollowups': [], 'error': True}
    assert inputs['sheet'].entries  # the sheet and the views are unaffected


@pytest.mark.asyncio
async def test_author_report_case_returns_the_case_with_inputs_and_audit(monkeypatch):
    env = Env(monkeypatch)
    client = full_client()

    case = await author_report_case(env.state, client, deadline=time.monotonic() + 600)

    assert set(case) == {'plan', 'cells', 'limits', 'raw', 'inputs', 'audit'}
    assert case['audit'] is env.state.audit['caseAuthoring']
    assert case['audit']['sheet']['hasClinical'] and case['audit']['sheet']['records'] == len(
        case['inputs']['sheet'].entries
    )
    assert case['audit']['sheet']['characters'] == len(case['inputs']['sheet'].text)
    assert 'overview.summary' in case['cells'] and case['audit']['guideVersion']
    assert env.state.stage_timings['report_case_authoring'] >= 0
    assert [payload['task'] for payload in client.calls].count(ANALYZE_TASK) == 1


@pytest.mark.asyncio
async def test_author_report_case_applies_the_safety_net_flags(monkeypatch):
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
        ],
        'routineFollowups': [],
    }
    env = Env(monkeypatch, flags=flags)

    case = await author_report_case(env.state, full_client(), deadline=time.monotonic() + 600)

    assert [(item['title'], item['origin']) for item in case['plan']['priorityItems']] == [
        ('粪便隐血查因', 'model'),
        ('超声规则', 'safety_net'),
    ]


@pytest.mark.asyncio
async def test_a_case_without_any_record_is_not_authored(monkeypatch):
    env = Env(monkeypatch, facts=[], clinical={}, microbiome={})
    env.req.survey_list = []
    client = full_client()

    case = await author_report_case(env.state, client, deadline=time.monotonic() + 600)

    assert case is None and client.calls == []
    assert env.state.audit['caseAuthoring']['error'] == 'no_records'
    assert env.state.audit['caseAuthoring']['sheet']['records'] == 0


@pytest.mark.asyncio
async def test_an_unavailable_analysis_returns_none_and_names_the_error(monkeypatch):
    env = Env(monkeypatch)
    client = full_client(**{ANALYZE_TASK: RuntimeError('down')})

    case = await author_report_case(env.state, client, deadline=time.monotonic() + 600)

    assert case is None
    assert env.state.audit['caseAuthoring']['error'] == 'CaseAuthoringError'
    assert 'report_case_authoring' not in env.state.stage_timings


@pytest.mark.asyncio
async def test_a_failing_input_step_returns_none_and_names_the_error(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.setattr(
        'modules.report.assessment_clinical_view.build_clinical_view',
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyError('x')),
    )

    case = await author_report_case(env.state, full_client(), deadline=time.monotonic() + 600)

    assert case is None and env.state.audit['caseAuthoring']['error'] == 'KeyError'


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(monkeypatch):
    env = Env(monkeypatch)
    client = full_client(**{ANALYZE_TASK: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await author_report_case(env.state, client, deadline=time.monotonic() + 600)


@pytest.mark.asyncio
async def test_the_authoring_budget_is_capped_but_an_earlier_deadline_wins(monkeypatch):
    env = Env(monkeypatch)
    seen = []

    async def fake_author(client, sheet, *, clinical, microbiome, flags, deadline, audit):
        seen.append(deadline)
        return {'plan': {}, 'cells': {}, 'limits': {}, 'raw': {}}

    monkeypatch.setattr('modules.report.assessment_case_authoring.author_assessment_case', fake_author)
    now = time.monotonic()

    await author_report_case(env.state, object(), deadline=now + 5000)
    await author_report_case(env.state, object(), deadline=now + 100)

    assert now + CASE_AUTHORING_BUDGET_SECONDS <= seen[0] <= now + CASE_AUTHORING_BUDGET_SECONDS + 5
    assert seen[1] == now + 100


def projection_case(monkeypatch):
    env = Env(monkeypatch)
    inputs = env.inputs()
    sheet = inputs['sheet']
    plan = normalize_case_analysis(raw_analysis(sheet), sheet, clinical=inputs['clinical'])
    apply_priority_safety_net(plan, sheet, inputs['flags'])
    jobs = section_jobs(plan, sheet, clinical=inputs['clinical'], microbiome=inputs['microbiome'])
    cells = {cell['id']: f'文本[{cell["id"]}]' for job in jobs for cell in job['cells']}
    return env, inputs, {'plan': plan, 'cells': cells}


def test_projection_entry_uses_the_stored_inputs(monkeypatch):
    env, inputs, case = projection_case(monkeypatch)
    case['inputs'] = inputs
    env.view_calls.clear()

    presentation = build_report_case_presentation(
        request=env.req,
        parsed=env.state.parsed,
        document=env.document,
        data=env.blueprint.data,
        blueprint=env.blueprint,
        accepted=None,
        case=case,
    )

    assert isinstance(presentation, AssessmentPresentation)
    assert env.view_calls == []  # nothing is rebuilt
    assert presentation.overview['dataTiming'] == inputs['timing']
    assert presentation.overview['summary'] == '文本[overview.summary]'
    assert case['projection']['missingCells'] == []


def test_projection_entry_rebuilds_the_inputs_after_a_checkpoint_restore(monkeypatch):
    env, _, case = projection_case(monkeypatch)
    env.view_calls.clear()

    presentation = build_report_case_presentation(
        request=env.req,
        parsed=env.state.parsed,
        document=env.document,
        data=env.blueprint.data,
        blueprint=env.blueprint,
        accepted=None,
        case=case,
    )

    assert env.view_calls == [('clinical', True), ('microbiome', len(env.facts))]
    assert set(case['inputs']) == {'clinical', 'microbiome', 'sheet', 'flags', 'timing'}
    assert presentation.clinical['symptoms'][0]['interpretation'] == '文本[symptoms.1.explanation]'
    assert case_incomplete(case) == []
