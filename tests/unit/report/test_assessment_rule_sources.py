"""Program rule conclusions (R) as a citable source of writing and source review.

All data are synthetic. These tests fix that a rule conclusion is admitted only
inside its own cell scope, that an action phrased from it passes source review,
that changing its time limit or department is rejected deterministically, that
patient findings still need their own records, and that the rule literature
reaches the bibliography with stable numbers.
"""

from __future__ import annotations

from types import SimpleNamespace

from modules.report.assessment_rule_sources import attach_rule_references
from modules.report.assessment_rule_sources import cell_rule_conclusions
from modules.report.assessment_rule_sources import model_rule_conclusions
from modules.report.assessment_rule_sources import rule_action_issue
from modules.report.assessment_rule_sources import sentence_rules
from modules.report.assessment_source_receipts import ACTION_ROLE
from modules.report.assessment_source_receipts import normalize_receipt
from modules.report.assessment_source_receipts import repair_receipt_findings
from modules.report.assessment_source_receipts import repaired_receipt
from modules.report.assessment_source_receipts import retained_paragraphs
from modules.report.assessment_source_review import build_observation_registry
from modules.report.assessment_source_review import build_source_frame
from modules.report.assessment_source_review import validate_source_proofs
from modules.report.evaluation_schema import ReferenceLiterature
from modules.report.evaluation_schema import TextAssessment

GUIDE_A = {
    'key': 'syn_guide_a',
    'title': '合成指南甲：筛查阳性结果的处理',
    'citation': '合成杂志, 2020, 1(1): 1-9',
    'year': 2020,
}
GUIDE_B = {'key': 'syn_guide_b', 'title': 'Synthetic follow-up statement', 'doi': '10.0000/syn.b', 'year': 2021}
GUIDE_C = {'key': 'syn_guide_c', 'title': '合成共识丙', 'citation': '合成学报, 2019, 2(3): 10-20'}
DEFAULT = {'key': 'program_default', 'title': '本规则表的操作时限', 'citation': '程序默认三档'}

PLAN = {
    'priorityItems': [
        {
            'code': 'RF-SYN-SCREEN',
            'ruleId': 'RF-SYN-SCREEN',
            'title': '粪便隐血阳性',
            'priority': 'P0',
            'timeframe': {'text': '1 个月内就诊', 'days': 30, 'basis': 'program_default'},
            'department': '消化内科',
            'checks': ['消化内科就诊', '结肠镜检查（由专科评估）'],
            'factIds': ['f-screen'],
            'sources': [GUIDE_A, DEFAULT],
            'origin': 'red_flag_rule',
        },
        {
            'code': 'RF-ADVICE-URGENT:p9',
            'ruleId': 'RF-ADVICE-URGENT',
            'title': '原建议尽快就诊的发现',
            'priority': 'P0',
            'timeframe': {'text': '尽快就诊（原检查建议）', 'days': 14, 'basis': 'original_advice'},
            'department': '相关专科',
            'checks': ['建议尽快就诊'],
            'factIds': ['f-other'],
            'sources': [{'key': 'original_advice', 'title': '原检查建议', 'quote': '建议尽快就诊'}],
            'origin': 'original_advice',
        },
    ],
    'routineFollowups': [
        {
            'code': 'RT-SYN-NODULE',
            'ruleId': 'RT-SYN-NODULE',
            'title': '器官结节',
            'interval': {'text': '6–12 个月复查超声', 'days': 365, 'basis': 'rule_default'},
            'department': '肝胆外科',
            'factIds': ['f-nodule'],
            'sources': [GUIDE_B],
            'origin': 'routine_rule',
        },
        {
            'code': 'advice:p8',
            'ruleId': 'RT-ORIGINAL-ADVICE',
            'title': '原建议随访的发现',
            'interval': {'text': '按原检查建议', 'days': None, 'basis': 'original_advice_without_interval'},
            'department': '眼科',
            'factIds': ['f-eye'],
            'sources': [{'key': 'original_advice', 'title': '原检查建议', 'quote': '建议眼科随诊'}],
            'origin': 'original_advice',
        },
    ],
    'testGaps': [
        {
            'code': 'gapLab',
            'ruleId': 'TG-SYN-LAB',
            'test': '补查项目丙',
            'reason': '检验乙偏高时需以补查项目丙明确状态。',
            'triggeredBy': ['f-lab'],
            'sources': [GUIDE_C],
        },
        {'code': 'gapAge', 'ruleId': 'TG-SYN-AGE', 'test': '年龄筛查丁', 'reason': '按年龄筛查。', 'triggeredBy': []},
    ],
    'clusters': [
        {'code': 'c-urgent', 'priority': 'P0', 'priorityItemCodes': ['RF-SYN-SCREEN']},
        {'code': 'c-core', 'priority': 'P1', 'priorityItemCodes': []},
    ],
}
TARGET_CLAIM = {
    'recordIds': ['f-lab'],
    'label': '检验乙',
    'authority': 'target',
    'reference': '<2.6 mmol/L',
    'targetId': 'syn-target-1',
    'applicability': {'minAge': 18},
    'sources': [{'title': '合成目标研究', 'year': 2018, 'doi': '10.0000/syn.t', 'journal': '合成期刊'}],
}
HANDLES = {'f-screen': 'F001', 'f-nodule': 'F002', 'f-lab': 'F003', 'f-other': 'F009'}
RECORDS = [
    {'id': 'F001', 'name': '粪便隐血试验', 'value': '弱阳性'},
    {'id': 'F002', 'name': '腹部超声', 'value': '见一枚结节'},
    {'id': 'F003', 'name': '检验乙', 'value': '3.4', 'unit': 'mmol/L', 'referenceRange': '0-3.4'},
    {'id': 'F009', 'name': '另一项检查', 'value': '未见异常'},
]


def _codes(scope, **extra):
    return [(row['kind'], row['code']) for row in cell_rule_conclusions(scope, PLAN, **extra)]


def test_rules_are_admitted_only_inside_their_cell_scope():
    urgent = ('priority_item', 'RF-SYN-SCREEN')
    routine = ('routine_followup', 'RT-SYN-NODULE')
    gaps = [('test_gap', 'gapLab'), ('test_gap', 'gapAge')]
    # Items that only restate original advice stay with the advice (A).
    assert _codes('priority.urgent') == [urgent]
    assert _codes('priority.routine') == [routine]
    assert _codes('priority.gaps') == gaps
    assert _codes('layer.clinical') == [urgent, routine, *gaps]
    assert _codes('synthesis.c-urgent') == _codes('synthesisAction.c-urgent') == [urgent]
    for scope in ('synthesis.c-core', 'overviewSummary', 'conclusionSummary', 'management.lifestyle', 'layer.core'):
        assert _codes(scope) == [], scope
    assert _codes('clinicalRow.lipid.panel') == []
    assert _codes('clinicalRow.lipid.panel', comparison_claims=[TARGET_CLAIM]) == [('target', 'syn-target-1')]
    # A rule whose trigger records lie outside the cell is not admitted; a
    # screening item without trigger records is.
    assert _codes('layer.clinical', scope_fact_ids={'f-nodule'}) == [routine, ('test_gap', 'gapAge')]
    entry = cell_rule_conclusions('priority.urgent', PLAN)[0]
    assert [source['key'] for source in entry['sources']] == ['syn_guide_a']
    assert entry['basisNote'] and entry['timeframe']['basis'] == 'program_default'
    assert entry['conclusion'] == (
        '粪便隐血阳性：1 个月内就诊；科室：消化内科；需完成：消化内科就诊、结肠镜检查（由专科评估）。'
    )
    model = model_rule_conclusions(cell_rule_conclusions('layer.clinical', PLAN), HANDLES)
    assert [row['id'] for row in model] == ['R01', 'R02', 'R03', 'R04']
    assert model[0]['recordIds'] == ['F001'] and model[3]['recordIds'] == []
    assert model[0]['sources'] == [{'title': GUIDE_A['title'], 'citation': GUIDE_A['citation'], 'year': 2020}]


def _frame(paragraphs, scope='priority.urgent', *, advice=(), requires_action=True):
    entries = cell_rule_conclusions(scope, PLAN, comparison_claims=[TARGET_CLAIM])
    cells = {
        'C01': {
            'recordIds': ['F001', 'F002', 'F003'],
            'contextRecordIds': [],
            'sourceContract': {},
            'passageRoles': {},
            'requiresPersonalAction': requires_action,
            'ruleConclusions': model_rule_conclusions(entries, HANDLES),
            'sourceClinicalActions': list(advice),
        }
    }
    return build_source_frame(
        cells,
        {'C01': paragraphs},
        registry=build_observation_registry([]),
        records=RECORDS,
        evidence=[],
    )


def _receipt(private, use='rule_conclusion', **extra):
    return {
        'id': 'C01',
        'paragraphs': [
            {
                'id': pid,
                'sourceVerdict': 'supported',
                'violations': [],
                'claimDecisions': [
                    {'id': candidate['id'], 'use': use, **extra}
                    for candidate in part['claimCandidates']
                    if candidate['kind'] == 'patient_action'
                ],
            }
            for pid, part in private['cells']['C01']['paragraphs'].items()
        ],
    }


FINDING = {
    'text': '粪便隐血试验弱阳性，属需优先查因的结果。',
    'factIds': ['F001'],
    'cite': [],
    'role': 'interpretation',
}


def test_an_action_phrased_from_its_rule_conclusion_passes_source_review():
    action = {
        'text': '建议1个月内至消化内科就诊，由专科评估结肠镜检查。',
        'factIds': ['F001'],
        'cite': ['R01'],
        'role': 'action',
    }
    frame, private = _frame([FINDING, action])
    cell = frame['cells'][0]
    assert [row['id'] for row in cell['ruleConclusions']] == ['R01']
    assert cell['paragraphs'][1]['ruleIds'] == ['R01'] and cell['paragraphs'][1]['passageIds'] == []
    assert not any(private['cells']['C01']['literalIssues'].values())
    assert private['cells']['C01']['paragraphs']['P02']['claimCandidates']
    checked = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
    assert checked['accepted'] == ['C01'], checked['semanticIssues']
    assert checked['proofs']['C01']['serverSourceBindings']['P02']['ruleIds'] == ['R01']
    # A single cited R is assigned when the reviewer omits ruleIds.
    assert validate_source_proofs(frame, private, {'cells': [_receipt(private)]})['accepted'] == ['C01']


def test_a_rule_and_its_original_advice_can_support_one_sentence():
    advice = {
        'code': 'p1',
        'name': '粪便隐血',
        'recordIds': ['F001'],
        'originalAdvice': ['建议复查大便常规及隐血，消化内科就诊。'],
    }
    action = {
        'text': '1个月内至消化内科复查大便常规及隐血。',
        'factIds': ['F001'],
        'cite': ['R01'],
        'role': 'action',
    }
    frame, private = _frame([FINDING, action], advice=[advice])
    assert not any(private['cells']['C01']['literalIssues'].values())
    alone = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
    assert 'C01' in alone['semanticIssues']  # the procedures come from the advice, not from the rule
    joined = _receipt(private, ruleIds=['R01'], adviceIds=['A01'])
    assert validate_source_proofs(frame, private, {'cells': [joined]})['accepted'] == ['C01']


def test_a_changed_time_limit_or_department_is_rejected_deterministically():
    for text, label in (
        ('建议2周内至消化内科就诊。', '时限「2周内」'),
        ('建议1个月内至肛肠科就诊。', '科室「肛肠科」'),
    ):
        action = {'text': text, 'factIds': ['F001'], 'cite': ['R01'], 'role': 'action'}
        frame, private = _frame([FINDING, action])
        literal = private['cells']['C01']['literalIssues']['P02']
        assert any(issue.startswith(label) for issue in literal), literal
        checked = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
        details = [issue['detail'] for issue in checked['semanticIssues']['C01']]
        assert any(detail.startswith('P02：' + label) for detail in details)
        assert any(detail.startswith('P02/K') and '规则结论R' in detail for detail in details)
        # Sentence retention removes exactly the rejected action.
        kept, _, _ = retained_paragraphs(
            [FINDING, action], {'P01': 'supported', 'P02': 'source_error'}, checked['semanticIssues']['C01']
        )
        assert [part['text'] for part in kept] == [FINDING['text']]
    rule = cell_rule_conclusions('priority.urgent', PLAN)[0]
    candidate = {'term': '按原检查建议1个月内至消化内科就诊', 'sourceAttribution': True}
    assert '不是原医嘱' in rule_action_issue(candidate, [rule])
    assert rule_action_issue({'term': '1个月内至消化内科完成胃镜'}, [rule]).startswith('转述的检查项目')
    assert rule_action_issue({'term': '至消化内科就诊，由专科评估结肠镜'}, [rule]) is None


def test_routine_intervals_accept_the_rule_range_only():
    rule = cell_rule_conclusions('priority.routine', PLAN)[0]
    assert rule_action_issue({'term': '6～12个月复查超声，肝胆外科随诊'}, [rule]) is None
    assert rule_action_issue({'term': '6个月后复查超声'}, [rule]) is None
    assert rule_action_issue({'term': '3个月后复查超声'}, [rule]).startswith('转述的时限')


def test_rules_outside_the_cell_scope_cannot_be_cited():
    action = {'text': '建议1个月内至消化内科就诊。', 'factIds': ['F001'], 'cite': ['R01'], 'role': 'action'}
    frame, private = _frame([FINDING, action], scope='overviewSummary')
    cell = frame['cells'][0]
    assert cell['ruleConclusions'] == [] and cell['paragraphs'][1]['ruleIds'] == []
    assert private['cells']['C01']['literalIssues']['P02'] == [
        '引用的规则编号「R01」不属于本格范围内的规则结论，不能引用。'
    ]
    checked = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
    details = [issue['detail'] for issue in checked['semanticIssues']['C01']]
    assert any('所用规则结论未在本段cite中引用' in detail for detail in details)
    # A rule admitted to the cell but not cited by the paragraph is not usable either.
    uncited = {**action, 'cite': []}
    frame, private = _frame([FINDING, uncited])
    assert frame['cells'][0]['ruleConclusions'] == []
    checked = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
    assert 'C01' in checked['semanticIssues']


def test_patient_findings_still_need_their_own_records():
    unbound = {'text': '建议1个月内至消化内科就诊。', 'factIds': ['F002'], 'cite': ['R01'], 'role': 'action'}
    frame, private = _frame([FINDING, unbound])
    assert any('触发记录未在本段绑定' in issue for issue in private['cells']['C01']['literalIssues']['P02'])
    # A rule citation does not cover a misstated patient value.
    stated = {
        'text': '粪便隐血试验强阳性，建议1个月内至消化内科就诊。',
        'factIds': ['F001'],
        'cite': ['R01'],
        'role': 'action',
    }
    frame, private = _frame([stated])
    receipt = _receipt(private, ruleIds=['R01'])
    receipt['paragraphs'][0].update(
        sourceVerdict='unsupported',
        violations=[
            {
                'kind': 'source_fact',
                'quote': '粪便隐血试验强阳性',
                'recordIds': ['F001'],
                'passageIds': [],
                'ruleIds': ['R01', 'R07'],
                'reason': '记录为弱阳性。',
            }
        ],
    )
    normalized, _ = normalize_receipt(receipt, private['cells']['C01'])
    assert normalized['paragraphs'][0]['violations'][0]['ruleIds'] == ['R01']
    checked = validate_source_proofs(frame, private, {'cells': [receipt]})
    assert any(issue['detail'].startswith('粪便隐血试验强阳性：') for issue in checked['semanticIssues']['C01'])


def test_target_values_follow_the_cited_target():
    supported = {
        'text': '检验乙3.4 mmol/L在检验单参考0-3.4内，但高于目标<2.6 mmol/L。',
        'factIds': ['F003'],
        'cite': ['R01'],
        'role': 'interpretation',
    }
    frame, private = _frame([supported], scope='clinicalRow.lipid.panel')
    assert frame['cells'][0]['ruleConclusions'][0]['target'] == '<2.6 mmol/L'
    assert not private['cells']['C01']['literalIssues']['P01']
    changed = {**supported, 'text': '检验乙3.4 mmol/L高于目标<1.8 mmol/L。'}
    frame, private = _frame([changed], scope='clinicalRow.lipid.panel')
    assert private['cells']['C01']['literalIssues']['P01'] == [
        '目标数值「1.8」与所引目标区间及本段记录不一致，目标值只照用所引R。'
    ]


def test_rule_ids_are_receipt_bookkeeping():
    action = {'text': '建议1个月内至消化内科就诊。', 'factIds': ['F001'], 'cite': ['R01'], 'role': 'action'}
    frame, private = _frame([FINDING, action])
    receipt = _receipt(private, ruleIds=['R01', 'R05'])
    receipt['paragraphs'][0]['claimDecisions'] = [
        {'id': candidate['id'], 'use': 'not_action', 'ruleIds': ['R01']}
        for candidate in private['cells']['C01']['paragraphs']['P01']['claimCandidates']
    ]
    normalized, corrections = normalize_receipt(receipt, private['cells']['C01'])
    kinds = {row['type'] for row in corrections}
    assert 'claim_rules_limited' in kinds
    assert all(decision['ruleIds'] == ['R01'] for decision in normalized['paragraphs'][1]['claimDecisions'])
    assert validate_source_proofs(frame, private, {'cells': [receipt]})['accepted'] == ['C01']


def test_an_action_split_out_of_an_interpretation_keeps_its_rule_citation():
    paragraphs = [
        {
            'text': '粪便隐血试验弱阳性，属需优先查因的结果。建议1个月内至消化内科就诊。',
            'factIds': ['F001'],
            'cite': ['R01'],
            'role': 'interpretation',
        }
    ]
    frame, private = _frame(paragraphs, requires_action=False)
    checked = validate_source_proofs(frame, private, {'cells': [_receipt(private, ruleIds=['R01'])]})
    issues = checked['semanticIssues']['C01']
    assert {issue.get('code') for issue in issues} == {ACTION_ROLE}
    repaired, notes, origins = repair_receipt_findings(paragraphs, issues, private['cells']['C01'])
    assert [(part['role'], part['cite']) for part in repaired] == [('interpretation', ['R01']), ('action', ['R01'])]
    new_frame, new_private = _frame(repaired, requires_action=False)
    carried = repaired_receipt(
        checked['receipts']['C01'], private['cells']['C01'], new_private['cells']['C01'], origins
    )
    assert validate_source_proofs(new_frame, new_private, {'cells': [carried]})['accepted'] == ['C01']


def test_retention_drops_unsupported_background_passages_but_keeps_rule_citations():
    paragraphs = [
        {'text': '建议1个月内至消化内科就诊。', 'factIds': ['F001'], 'cite': ['E2.1', 'R01'], 'role': 'action'}
    ]
    issues = [{'detail': '该行动段有E未能支持其中任何实际新增行动。原医嘱仅依本项A/F，解释和背景引文应另放。'}]
    kept, removed, _ = retained_paragraphs(paragraphs, {'P01': 'supported'}, issues)
    assert kept[0]['cite'] == ['R01'] and removed == [{'paragraph': 'P01', 'removedPassages': ['E2.1']}]


def test_published_sentences_carry_rule_literature_into_the_bibliography():
    owner = {'ruleConclusions': cell_rule_conclusions('layer.clinical', PLAN)}
    assert sentence_rules(owner, ['E1.1', 'R02', 'R09']) == [
        {'ruleId': 'RT-SYN-NODULE', 'kind': 'routine_followup', 'sources': [GUIDE_B]}
    ]
    library = [
        ReferenceLiterature(number=1, title='合成指南甲：筛查阳性结果的处理'),
        ReferenceLiterature(number=2, title='Another publication', doi='10.0000/other'),
    ]
    editorial = {'layer.clinical': TextAssessment(text='首月安排。', evidence_refs=[2])}
    data = SimpleNamespace(appendices=SimpleNamespace(reference_literature=library), _assessment_editorial=editorial)
    blueprint = SimpleNamespace(
        chapters=[{'targets': [{'id': 't1', 'path': ['_assessment_editorial', 'layer.clinical', 'text']}]}]
    )
    rules = [*sentence_rules(owner, ['R01']), *sentence_rules(owner, ['R02'])]
    accepted = [{'targetId': 't1', 'sentences': [{'text': '首月安排。', 'rules': rules}]}]
    assert attach_rule_references(data, blueprint, accepted) == [3]
    assert editorial['layer.clinical'].evidence_refs == [2, 1, 3]
    assert library[-1].doi == '10.0000/syn.b' and library[-1].supporting_sections == [
        'assessmentPresentation.rule.routine'
    ]
    # A second projection of the same data adds nothing.
    assert attach_rule_references(data, blueprint, accepted) == []
    assert editorial['layer.clinical'].evidence_refs == [2, 1, 3] and len(library) == 3


def test_writer_keeps_rule_citations_and_strips_leaked_rule_handles():
    from modules.report.evaluation_report_writer import _block
    from modules.report.evaluation_report_writer import _paragraphs

    rows = _paragraphs(
        {'paragraphs': [{'text': '建议1个月内至消化内科就诊（R01）。', 'factIds': ['F001'], 'cite': ['R01']}]},
        clean_prose=False,
    )
    assert rows[0]['text'] == '建议1个月内至消化内科就诊。' and rows[0]['cite'] == ['R01']
    target = {
        'id': 't1',
        'path': ['_assessment_editorial', 'priority.urgent', 'text'],
        'owner': {'contentPlanVersion': 'v', 'ruleConclusions': cell_rule_conclusions('priority.urgent', PLAN)},
        'sourceFactIds': ['f-screen'],
        'allowedRoles': ['action'],
    }
    dossier = SimpleNamespace(planned=True, reverse={'F001': 'f-screen'}, facts={'f-screen': {}})
    library = SimpleNamespace(allowed_targets={}, citation=lambda pids: [])
    block = _block(target, [{**rows[0], 'role': 'action'}], dossier, library, {'form': 'paragraph'})
    sentence = block['sentences'][0]
    assert sentence['support'] == 'clinical_synthesis' and sentence['evidence'] == []
    assert sentence['rules'] == [{'ruleId': 'RF-SYN-SCREEN', 'kind': 'priority_item', 'sources': [GUIDE_A]}]
