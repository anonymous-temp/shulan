"""Receipt bookkeeping, server repairs and sentence retention around source review.

All data are synthetic. These tests fix that receipt-only defects are corrected
deterministically, that content findings still go back to the author, and that
an unresolved duty keeps only reviewed sentences or a source-only listing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modules.report.assessment_measurement_claims import _INDEPENDENT_EVENT
from modules.report.assessment_source_receipts import ACTION_ROLE
from modules.report.assessment_source_receipts import RECALL_TIMING_NOTE
from modules.report.assessment_source_receipts import add_recall_timing_note
from modules.report.assessment_source_receipts import normalize_receipt
from modules.report.assessment_source_receipts import renumber_cell_claims
from modules.report.assessment_source_receipts import repair_receipt_findings
from modules.report.assessment_source_receipts import repaired_receipt
from modules.report.assessment_source_receipts import retained_paragraphs
from modules.report.assessment_source_receipts import source_only_text
from modules.report.assessment_source_receipts import trim_rejected_clauses
from modules.report.assessment_source_review import build_observation_registry
from modules.report.assessment_source_review import build_source_frame
from modules.report.assessment_source_review import validate_source_proofs


def _private_cell(paragraphs):
    return {
        'versionHash': 'v',
        'paragraphs': paragraphs,
        'groups': {},
        'observations': {},
        'records': {},
        'sourceBoundaries': {},
        'advice': {},
        'evidence': {},
        'passageRoles': {},
        'literalIssues': {},
        'requiresPersonalAction': False,
    }


def _paragraph(pid, text, kids, *, role='interpretation', passages=()):
    return {
        'id': pid,
        'title': None,
        'text': text,
        'recordIds': ['F001'],
        'passageIds': list(passages),
        'role': role,
        'claimCandidates': [{'id': kid, 'kind': 'patient_action', 'term': text[:6], 'start': 0} for kid in kids],
    }


def test_claims_are_numbered_once_per_cell():
    parts = [{'claimCandidates': [{'id': 'K01'}, {'id': 'K02'}]}, {'claimCandidates': [{'id': 'K01'}]}]
    renumber_cell_claims(parts)
    assert [c['id'] for part in parts for c in part['claimCandidates']] == ['K01', 'K02', 'K03']


def test_misplaced_and_duplicated_decisions_are_receipt_bookkeeping_not_a_review_round():
    private = {
        'cells': {
            'C01': _private_cell(
                {
                    'P01': _paragraph('P01', '血糖结果在参考范围内。', ['K01']),
                    'P02': _paragraph('P02', '血脂结果偏高需要关注。', ['K02']),
                }
            )
        }
    }
    response = {
        'cells': [
            {
                'id': 'C01',
                'paragraphs': [
                    {
                        'id': 'P01',
                        'sourceVerdict': 'supported',
                        'violations': [],
                        'claimDecisions': [
                            {'id': 'K01', 'use': 'not_action', 'passageIds': ['E9.9']},
                            {'id': 'K02', 'use': 'not_action'},
                            {'id': 'K01', 'use': 'not_action'},
                        ],
                    },
                    {'id': 'P2', 'sourceVerdict': 'supported', 'violations': [], 'claimDecisions': []},
                ],
            }
        ]
    }
    checked = validate_source_proofs({}, private, response)
    assert checked['accepted'] == ['C01']
    kinds = {row['type'] for row in checked['receiptCorrections']['C01']}
    assert {'claim_moved', 'duplicate_claim_dropped', 'claim_passages_dropped', 'paragraph_id_normalized'} <= kinds


def test_violation_sources_are_limited_and_contradictory_verdict_follows_the_error():
    cell = _private_cell({'P01': _paragraph('P01', '诊室血压96/61 mmHg。', [])})
    row = {
        'id': 'C01',
        'paragraphs': [
            {
                'id': 'P01',
                'sourceVerdict': 'supported',
                'violations': [
                    {
                        'kind': 'source_fact',
                        'quote': '诊室血压 96/61 mmHg',
                        'recordIds': ['F001', 'F999'],
                        'passageIds': ['E1.1'],
                        'reason': '未记录测量场所。',
                    }
                ],
                'claimDecisions': [],
            }
        ],
    }
    normalized, _ = normalize_receipt(row, cell)
    part = normalized['paragraphs'][0]
    assert part['sourceVerdict'] == 'unsupported'
    assert part['violations'][0]['quote'] == '诊室血压96/61 mmHg'
    assert part['violations'][0]['recordIds'] == ['F001']
    assert part['violations'][0]['passageIds'] == []
    checked = validate_source_proofs({}, {'cells': {'C01': cell}}, {'cells': [row]})
    # The reported error itself is never dropped: it still goes to the author.
    assert 'C01' in checked['semanticIssues'] and not checked['formatIssues']


def _frame(paragraphs):
    cells = {
        'C01': {
            'recordIds': ['F001'],
            'contextRecordIds': [],
            'sourceContract': {},
            'passageRoles': {'E1.1': ['interpretation']},
        }
    }
    return build_source_frame(
        cells,
        {'C01': paragraphs},
        registry=build_observation_registry([]),
        records=[{'id': 'F001', 'name': '空腹血糖', 'value': '5.1', 'unit': 'mmol/L'}],
        evidence=[{'title': '合成指南', 'passages': [{'pid': 'E1.1', 'text': '血糖正常者按常规随访。'}]}],
    )


def test_action_inside_interpretation_is_split_out_without_background_citation_and_revalidated():
    paragraphs = [
        {
            'text': '血糖结果在参考范围内。建议半年后复查血糖。',
            'factIds': ['F001'],
            'cite': ['E1.1'],
            'role': 'interpretation',
        }
    ]
    frame, private = _frame(paragraphs)
    receipt = {
        'id': 'C01',
        'paragraphs': [
            {
                'id': 'P01',
                'sourceVerdict': 'supported',
                'violations': [],
                'claimDecisions': [
                    {'id': candidate['id'], 'use': 'patient_management'}
                    for candidate in private['cells']['C01']['paragraphs']['P01']['claimCandidates']
                ],
            }
        ],
    }
    checked = validate_source_proofs(frame, private, {'cells': [receipt]})
    issues = checked['semanticIssues']['C01']
    assert [issue.get('code') for issue in issues] == [ACTION_ROLE]

    repaired, notes, origins = repair_receipt_findings(paragraphs, issues, private['cells']['C01'])
    assert [(part['role'], part['text'], part['cite']) for part in repaired] == [
        ('interpretation', '血糖结果在参考范围内。', ['E1.1']),
        ('action', '建议半年后复查血糖。', []),
    ]
    assert origins == [0, 0] and notes[0]['mode'] == 'split'
    new_frame, new_private = _frame(repaired)
    carried = repaired_receipt(
        checked['receipts']['C01'], private['cells']['C01'], new_private['cells']['C01'], origins
    )
    assert validate_source_proofs(new_frame, new_private, {'cells': [carried]})['accepted'] == ['C01']


def test_content_findings_are_not_repaired_by_the_server():
    issues = [{'detail': '诊室血压：未记录测量场所。', 'paragraph': 'P01'}]
    cell = _private_cell({'P01': _paragraph('P01', '诊室血压96/61 mmHg。', [])})
    assert repair_receipt_findings([{'text': '诊室血压96/61 mmHg。', 'role': 'observation'}], issues, cell) is None


def test_recall_timing_note_states_the_unconfirmed_relation():
    assert _INDEPENDENT_EVENT.search(RECALL_TIMING_NOTE)
    updated, changed = add_recall_timing_note([{'text': '自述留取标本时便秘；'}], ['P01'])
    assert changed == ['P01']
    assert updated[0]['text'] == '自述留取标本时便秘。' + RECALL_TIMING_NOTE
    again, changed = add_recall_timing_note(updated, ['P01'])
    assert again == updated and changed == []


def test_retention_removes_only_rejected_clauses_of_reviewed_paragraphs():
    paragraphs = [
        {
            'text': '胆碱酯酶11853 U/L，高于参考上限，属轻度升高。',
            'factIds': ['F001'],
            'cite': [],
            'role': 'interpretation',
        },
        {
            'text': '控制饮食、增加运动；半年后复查OGTT；心内科咨询。',
            'factIds': ['F001'],
            'cite': ['E2.5'],
            'role': 'action',
        },
        {'text': '未审核的段落。', 'factIds': ['F001'], 'cite': [], 'role': 'interpretation'},
    ]
    statuses = {'P01': 'supported', 'P02': 'source_error', 'P03': 'format_error'}
    issues = [
        {
            'type': 'unsupported_claim',
            'detail': 'P02/K05（半年后复查OGTT）：转述的具体检查项目并非所选A给该处理对象的项目。',
        },
        {
            'type': 'unsupported_claim',
            'detail': '该行动段有E未能支持其中任何实际新增行动。原医嘱仅依本项A/F，解释和背景引文应另放。',
        },
    ]
    kept, removed, origins = retained_paragraphs(paragraphs, statuses, issues)
    assert [part['text'] for part in kept] == [paragraphs[0]['text'], '控制饮食、增加运动；心内科咨询。']
    assert kept[1]['cite'] == [] and origins == [0, 1]
    assert {row['paragraph'] for row in removed} == {'P02', 'P03'}


def test_retention_refuses_findings_it_cannot_place():
    paragraphs = [{'text': '多样性4.26，属丰富。', 'factIds': ['F001'], 'cite': [], 'role': 'interpretation'}]
    issues = [{'type': 'missing_interpretation', 'detail': '未完成本格职责。'}]
    assert retained_paragraphs(paragraphs, {'P01': 'source_error'}, issues) == ([], [], [])
    assert trim_rejected_clauses('甲。乙。', [None]) is None
    assert trim_rejected_clauses('甲为1。乙为2；丙为3。', ['乙为2']) == '甲为1。丙为3。'


def test_source_only_text_copies_values_references_and_states():
    facts = [
        {'name': '腰围', 'value': '92', 'unit': 'cm', 'referenceRange': '男性<90', 'status': '偏高'},
        {'name': '空腹血糖', 'value': '5.74', 'unit': 'mmol/L', 'referenceRange': '3.9-6.1', 'status': '正常'},
        {'name': '无值项', 'value': ''},
    ]
    assert source_only_text(facts) == '腰围92 cm（参考男性<90，偏高）；空腹血糖5.74 mmol/L（参考3.9-6.1，正常）。'


def _target(target_id, **owner):
    return {
        'id': target_id,
        'path': ['_assessment_editorial', target_id, 'text'],
        'owner': {'primaryFactIds': ['f3', 'f1', 'f2'], **owner},
        'sourceFactIds': ['f1', 'f2', 'f3'],
        'allowedRoles': ['observation', 'interpretation'],
        'cellContract': {},
        'required': True,
    }


def test_unfinished_duties_keep_retained_sentences_or_a_source_only_listing():
    from modules.report.evaluation_report import rescue_unfinished_public_targets
    from modules.report.evaluation_report import source_only_fallback_block

    facts = {
        'f1': {'factId': 'f1', 'name': '甘油三酯', 'value': '3.34', 'unit': 'mmol/L', 'status': '偏高'},
        'f2': {'factId': 'f2', 'name': '总胆固醇', 'value': '4.1', 'unit': 'mmol/L', 'status': '正常'},
        'f3': {'factId': 'f3', 'kind': 'self_reported', 'name': '还有哪些想改善的健康问题', 'value': '血糖控制'},
    }
    targets = {
        't1': _target('t1'),
        't2': _target('t2'),
        't3': _target('t3', requiresPersonalAction=True),
    }
    blueprint = SimpleNamespace(
        facts=list(facts.values()),
        chapters=[{'code': 'medicalAssessment', 'targets': list(targets.values())}],
    )
    state = SimpleNamespace(blueprint=blueprint, audit={})
    retained_block = {'targetId': 't1', 'completion': 'complete', 'sentences': [{'text': '保留句。'}]}
    published = []

    def checkpoint(code, blocks, missing):
        published.extend((code, block) for block in blocks)

    unresolved = {('medicalAssessment', 't1'): {}, ('medicalAssessment', 't2'): {}}
    salvage = {'t1': ('medicalAssessment', {'block': retained_block, 'removed': [{'paragraph': 'P02'}]})}
    rescue_unfinished_public_targets(
        state, targets, {'t1', 't2', 't3'}, {'t1', 't2', 't3'}, unresolved, salvage, set(), checkpoint
    )
    by_target = {block['targetId']: block for _, block in published}
    assert by_target['t1'] == retained_block
    assert by_target['t2']['completion'] == 'source_only'
    assert by_target['t2']['sentences'][0]['text'] == '甘油三酯3.34 mmol/L（偏高）；总胆固醇4.1 mmol/L（正常）。'
    assert 't3' not in by_target  # an action duty is never filled with a value list
    assert state.audit['sentenceRetention'][0]['targetId'] == 't1'
    assert source_only_fallback_block({**targets['t2'], 'sourceFallback': {'targetId': 't2'}}, facts) is None


def test_follow_up_consumers_and_plan_degradation():
    from modules.report.evaluation_report import content_plan_degraded_sections
    from modules.report.evaluation_report import followup_consumes_path

    assert followup_consumes_path(['medical_assessment', 'findings', 0, 'explanation'])
    assert followup_consumes_path(['comprehensive_conclusion', 'priority_directions', 1, 'followup_action'])
    assert not followup_consumes_path(['nutrition_assessment', 'nutrition_interpretation'])
    assert not followup_consumes_path(['functional_pathways', 'summary'])
    plan = {
        'status': 'reviewed',
        'completeness': 'degraded',
        'sectionStatus': {'clusters': {'state': 'reviewed'}, 'symptoms': {'state': 'fallback'}},
    }
    assert content_plan_degraded_sections(plan) == ['symptoms']
    assert content_plan_degraded_sections({'status': 'reviewed', 'completeness': 'complete'}) == []
    assert content_plan_degraded_sections({'completeness': 'degraded'}) == ['unspecified']


def test_one_report_runs_twelve_model_calls_at_once_within_a_wider_shared_pool():
    from adapters.report.agent_narrative import AgentNarrativeClient
    from modules.report.fallback_policy import EVALUATE_MODEL_CONCURRENCY
    from modules.report.fallback_policy import EVALUATE_SHARED_MODEL_CONCURRENCY
    from modules.report.fallback_policy import ReportFallbackPolicy

    assert EVALUATE_MODEL_CONCURRENCY == 12
    assert EVALUATE_SHARED_MODEL_CONCURRENCY >= 2 * EVALUATE_MODEL_CONCURRENCY

    class Hub:
        def __init__(self):
            self.active = self.peak = 0
            self.release = asyncio.Event()

        async def invoke(self, capability, request, context, *, selection):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await self.release.wait()
            self.active -= 1
            return SimpleNamespace(data={'content': '{}'})

    async def scenario():
        hub = Hub()
        client = AgentNarrativeClient(
            hub=hub,
            semaphore=asyncio.Semaphore(EVALUATE_SHARED_MODEL_CONCURRENCY),
            policy=ReportFallbackPolicy(llm_attempts=1, retry_backoff_seconds=()),
            user_id='u',
            session_id='s',
            execution_id='e',
        )
        calls = [asyncio.create_task(client.generate({'task': 'write_report_chapter'})) for _ in range(20)]
        for _ in range(50):
            await asyncio.sleep(0)
        peak = hub.peak
        hub.release.set()
        await asyncio.gather(*calls)
        return peak

    assert asyncio.run(scenario()) == EVALUATE_MODEL_CONCURRENCY


@pytest.mark.parametrize('text', ['运动方式宜按个人意愿和能力确定（E2.5）。', '见【E12.3】。'])
def test_leaked_passage_handles_are_removed_from_paragraphs(text):
    from modules.report.evaluation_report_writer import _paragraphs

    assert 'E' not in _paragraphs({'paragraphs': [{'text': text}]}, clean_prose=False)[0]['text']


_STOOL = {
    'sourceQuote': '建议复查大便常规+"OB"，消化内科就诊；若复查后仍阳性，请进一步检查原因。',
    'sourceAdviceScopes': [],
}
_STOOL_CONDITION = {'sourceQuote': '若复查后仍阳性，请进一步检查原因。', 'sourceAdviceScopes': []}


@pytest.mark.parametrize(
    ('term', 'advice', 'accepted'),
    [
        ('复查大便常规及隐血，并至消化内科就诊', [_STOOL], True),
        ('按体检结论复查大便常规+OB并消化内科就诊，仍阳性进一步查因', [_STOOL, _STOOL_CONDITION], True),
        ('复查大便常规并至内分泌科就诊', [_STOOL], False),
        ('复查大便常规，仍阳性再至消化内科就诊', [_STOOL], False),
    ],
)
def test_a_retelling_may_join_sibling_actions_of_the_selected_order_but_not_change_them(term, advice, accepted):
    from modules.report.assessment_measurement_claims import advice_authority_issue

    issue = advice_authority_issue({'term': term, 'statement': term}, advice)
    assert (issue is None) is accepted, issue


def test_source_only_text_separates_alphanumeric_names_and_never_stacks_punctuation():
    facts = [
        {'name': '糖链抗原CA125', 'value': '6.3', 'unit': 'U/mL', 'referenceRange': '<35.0'},
        {'name': '糖链抗原CA19-9', 'value': '6.6', 'unit': 'U/mL'},
        {'name': '宫颈细胞学', 'value': '未见肿瘤细胞。'},
    ]
    assert (
        source_only_text(facts)
        == '糖链抗原CA125 6.3 U/mL（参考<35.0）；糖链抗原CA19-9 6.6 U/mL；宫颈细胞学未见肿瘤细胞。'
    )


def test_a_condition_left_without_its_removed_action_is_removed_too():
    text = '按检查建议复查大便常规及隐血，并至消化内科就诊；复查后仍为阳性时进一步检查原因。体重需复评。'
    assert trim_rejected_clauses(text, ['并至消化内科就诊']) == '体重需复评。'


def test_unresolved_detail_cells_are_repaired_while_the_first_pass_is_still_running(monkeypatch):
    import dataclasses
    import time

    from modules.report import evaluation_report
    from modules.report import evaluation_report_writer

    def target(identifier, code):
        return {
            'id': identifier,
            'path': ['_assessment_editorial', identifier, 'text'],
            'owner': {'contentPlanVersion': 1, 'contentScopeCode': code, 'primaryFactIds': []},
            'sourceFactIds': [],
            'allowedRoles': ['interpretation'],
            'required': True,
            'publicationScope': 'assessment',
        }

    @dataclasses.dataclass
    class Blueprint:
        chapters: list
        data: object
        facts: list = dataclasses.field(default_factory=list)

    chapter = {
        'code': 'medicalAssessment',
        'number': 3,
        'title': '三',
        'targets': [
            target('d1', 'clinicalRow.lipid.ldl'),
            target('d2', 'clinicalRow.lipid.tg'),
            target('s1', 'clinicalLead'),
        ],
    }
    blueprint = Blueprint(chapters=[chapter], data=SimpleNamespace(_assessment_content_plan={'status': 'reviewed'}))
    state = SimpleNamespace(
        blueprint=blueprint, audit={}, approved_blocks={}, evidence=None, stage_timings={}, started=time.monotonic()
    )
    events = []

    def block(identifier):
        return {'targetId': identifier, 'completion': 'complete', 'sentences': [{'text': identifier}]}

    async def fake_author(client, *, blueprint, authoring_scope, on_checkpoint, **_):
        phase = authoring_scope['phase']
        ids = [t['id'] for c in blueprint.chapters for t in c['targets']]
        events.append((phase, 'start', tuple(ids)))
        if phase == 'public_first_pass':
            on_checkpoint(
                'medicalAssessment',
                [block('d2')],
                [{'chapterCode': 'medicalAssessment', 'targetId': 'd1', 'issues': []}],
            )
            await asyncio.sleep(0.05)  # the summary batch is still running
            events.append((phase, 'end', tuple(ids)))
            return {'medicalAssessment': [block('d2'), block('s1')]}
        events.append((phase, 'end', tuple(ids)))
        return {'medicalAssessment': [block(i) for i in ids]}

    monkeypatch.setattr(evaluation_report_writer, 'author_report_chapters', fake_author)
    asyncio.run(evaluation_report._author_chapters(state, None, None, time.monotonic() + 60))
    repair_start = events.index(('public_repair', 'start', ('d1',)))
    first_end = next(i for i, row in enumerate(events) if row[:2] == ('public_first_pass', 'end'))
    assert repair_start < first_end
    assert {b['targetId'] for b in state.approved_blocks['medicalAssessment']} == {'d1', 'd2', 's1'}
    assert sum(1 for row in events if row[0] == 'public_repair' and row[1] == 'start') == 1


def test_the_coordinated_sweep_does_not_wait_for_a_slow_early_repair(monkeypatch):
    import dataclasses
    import time

    from modules.report import evaluation_report
    from modules.report import evaluation_report_writer

    def target(identifier, code):
        return {
            'id': identifier,
            'path': ['_assessment_editorial', identifier, 'text'],
            'owner': {'contentPlanVersion': 1, 'contentScopeCode': code, 'primaryFactIds': []},
            'sourceFactIds': [],
            'allowedRoles': ['interpretation'],
            'required': True,
            'publicationScope': 'assessment',
        }

    @dataclasses.dataclass
    class Blueprint:
        chapters: list
        data: object
        facts: list = dataclasses.field(default_factory=list)

    chapter = {
        'code': 'medicalAssessment',
        'number': 3,
        'title': '三',
        'targets': [target('d1', 'clinicalRow.lipid.ldl'), target('s1', 'clinicalLead')],
    }
    blueprint = Blueprint(chapters=[chapter], data=SimpleNamespace(_assessment_content_plan={'status': 'reviewed'}))
    state = SimpleNamespace(
        blueprint=blueprint, audit={}, approved_blocks={}, evidence=None, stage_timings={}, started=time.monotonic()
    )
    events = []

    def block(identifier):
        return {'targetId': identifier, 'completion': 'complete', 'sentences': [{'text': identifier}]}

    async def fake_author(client, *, blueprint, authoring_scope, on_checkpoint, **_):
        phase = authoring_scope['phase']
        ids = tuple(t['id'] for c in blueprint.chapters for t in c['targets'])
        events.append((phase, 'start', ids))
        if phase == 'public_first_pass':
            on_checkpoint(
                'medicalAssessment',
                [],
                [
                    {'chapterCode': 'medicalAssessment', 'targetId': 'd1', 'issues': []},
                    {'chapterCode': 'medicalAssessment', 'targetId': 's1', 'issues': []},
                ],
            )
            await asyncio.sleep(0.01)
            events.append((phase, 'end', ids))
            return {}
        if ids == ('d1',):
            await asyncio.sleep(0.2)  # a slow early repair
        events.append((phase, 'end', ids))
        return {'medicalAssessment': [block(i) for i in ids]}

    monkeypatch.setattr(evaluation_report_writer, 'author_report_chapters', fake_author)
    asyncio.run(evaluation_report._author_chapters(state, None, None, time.monotonic() + 60))
    assert events.index(('public_repair', 'start', ('s1',))) < events.index(('public_repair', 'end', ('d1',)))
    assert {b['targetId'] for b in state.approved_blocks['medicalAssessment']} == {'d1', 's1'}


def test_past_the_delivery_target_open_duties_keep_their_retained_sentences(monkeypatch):
    import dataclasses
    import time

    from modules.report import evaluation_report
    from modules.report import evaluation_report_writer

    @dataclasses.dataclass
    class Blueprint:
        chapters: list
        data: object
        facts: list = dataclasses.field(default_factory=list)

    detail = {
        'id': 'd1',
        'path': ['_assessment_editorial', 'd1', 'text'],
        'owner': {'contentPlanVersion': 1, 'contentScopeCode': 'clinicalRow.lipid.ldl', 'primaryFactIds': []},
        'sourceFactIds': [],
        'allowedRoles': ['interpretation'],
        'required': True,
        'publicationScope': 'assessment',
    }
    chapter = {'code': 'medicalAssessment', 'number': 3, 'title': '三', 'targets': [detail]}
    blueprint = Blueprint(chapters=[chapter], data=SimpleNamespace(_assessment_content_plan={'status': 'reviewed'}))
    # Generation started long ago: the delivery target has already passed.
    state = SimpleNamespace(
        blueprint=blueprint,
        audit={},
        approved_blocks={},
        evidence=None,
        stage_timings={},
        started=time.monotonic() - 5000,
    )
    retained = {
        'targetId': 'd1',
        'completion': 'complete',
        'sentences': [{'text': '保留句。', 'role': 'interpretation'}],
    }
    phases = []

    async def fake_author(client, *, blueprint, authoring_scope, on_checkpoint, **_):
        phases.append(authoring_scope['phase'])
        on_checkpoint(
            'medicalAssessment',
            [],
            [
                {
                    'chapterCode': 'medicalAssessment',
                    'targetId': 'd1',
                    'issues': [],
                    'salvage': {'block': retained, 'removed': [], 'phase': authoring_scope['phase']},
                }
            ],
        )
        return {}

    monkeypatch.setattr(evaluation_report_writer, 'author_report_chapters', fake_author)
    asyncio.run(evaluation_report._author_chapters(state, None, None, time.monotonic() + 600))
    assert phases == ['public_first_pass']
    assert state.approved_blocks['medicalAssessment'] == [retained]
    assert state.audit['sentenceRetention'][0]['targetId'] == 'd1'
