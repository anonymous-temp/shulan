from types import SimpleNamespace

from modules.report.evaluation_disease_catalog import _source_family
from modules.report.evaluation_schema import AssessmentScore
from modules.report.evaluation_schema import MedicalFinding
from modules.report.evaluation_schema import RiskMatrixRow
from modules.report.evaluation_schema import SourceReference
from modules.report.evaluation_template_layout import _attach_leftover_rows
from modules.report.evaluation_template_layout import _computed_context_only
from modules.report.evaluation_template_layout import _group_validation
from modules.report.evaluation_template_layout import _source_named_groups
from modules.report.evaluation_template_scores import _publish_basis
from modules.report.evaluation_template_scores import _review_decision
from modules.report.evaluation_template_scores import score_attention_label

CATEGORIES = {'metabolic': '代谢相关疾病', 'intestinal': '肠道相关疾病'}


def _clinical():
    return {
        'clinicalProblem:waist': MedicalFinding(code='clinicalProblem:waist', name='腰围增大', value='92 cm'),
        'clinicalProblem:fat': MedicalFinding(code='clinicalProblem:fat', name='体脂百分比肥胖', value='29.7%'),
        'clinicalProblem:tg': MedicalFinding(code='clinicalProblem:tg', name='甘油三酯升高', value='3.34 mmol/L'),
    }


def _group(**overrides):
    group = {
        'id': 'G1',
        'name': '肥胖（中心性肥胖）',
        'system': '内分泌科',
        'memberCodes': ['clinicalProblem:waist', 'clinicalProblem:fat'],
        'nativeCodes': [],
        'role': 'health_problem',
        'includeInRiskChapter': True,
        'category': 'metabolic',
        'certainty': 'confirmed',
        'attentionLevel': '中风险',
        'focusLabel': '重点管理',
        'priority': 2,
    }
    group.update(overrides)
    return group


def test_auxiliary_addresses_and_level_spellings_no_longer_void_a_correct_group():
    proposed = [
        _group(
            memberCodes=['clinicalProblem:waist', 'clinicalProblem:fat', 'managementGoal:lose-weight'],
            nativeCodes=['nativeAssociation:unknown'],
            attentionLevel='中',
        ),
        _group(
            id='G2',
            name='高甘油三酯血症',
            memberCodes=['clinicalProblem:tg', 'clinicalProblem:waist'],
            attentionLevel='high',
            priority=3,
        ),
    ]
    groups, errors = _group_validation(proposed, _clinical(), {}, CATEGORIES)
    assert errors == []
    assert groups[0]['memberCodes'] == ['clinicalProblem:waist', 'clinicalProblem:fat']
    assert groups[0]['nativeCodes'] == [] and groups[0]['attentionLevel'] == '中风险'
    # A member already owned by an earlier group is dropped from the later one.
    assert groups[1]['memberCodes'] == ['clinicalProblem:tg'] and groups[1]['attentionLevel'] == '高风险'


def test_a_group_without_any_real_member_is_still_rejected():
    groups, errors = _group_validation(
        [_group(memberCodes=['managementGoal:lose-weight'])], _clinical(), {}, CATEGORIES
    )
    assert groups == [] and errors == [{'id': 'G1', 'reason': 'group_address_or_state_invalid'}]
    groups, errors = _group_validation([_group(attentionLevel='很高')], _clinical(), {}, CATEGORIES)
    assert groups == [] and errors


def test_leftover_rows_join_only_the_group_that_literally_names_them():
    clinical = {
        **_clinical(),
        'summary:lipid': MedicalFinding(code='summary:lipid', name='主检结论：混合型高脂血症', value='建议心内科就诊'),
        'summary:che': MedicalFinding(code='summary:che', name='主检结论：胆碱酯酶增高 11853 U/L', value='建议复查'),
    }
    accepted = [
        {'id': 'G1', 'name': '混合型高脂血症', 'role': 'health_problem', 'memberCodes': ['clinicalProblem:tg']},
        {
            'id': 'G2',
            'name': '肥胖',
            'role': 'health_problem',
            'memberCodes': ['clinicalProblem:waist', 'clinicalProblem:fat'],
        },
    ]
    attached = _attach_leftover_rows(accepted, clinical)
    assert attached == [{'code': 'summary:lipid', 'groupId': 'G1'}]
    assert accepted[0]['memberCodes'] == ['clinicalProblem:tg', 'summary:lipid']
    assert 'summary:che' not in accepted[0]['memberCodes'] + accepted[1]['memberCodes']


def test_image_cover_reports_are_recognised_from_the_printed_method_and_risk_table():
    diseases = [
        '克罗恩病',
        '溃疡性结肠炎',
        '结直肠癌',
        '肠易激综合征',
        '胃癌',
        '精神分裂症',
        '抑郁症',
        '2型糖尿病',
        '非酒精性脂肪肝',
        '肝硬化',
    ]
    table = '疾病类型\n风险值\n检测结果\n' + ''.join(f'{name}\n0.1\n低风险\n' for name in diseases)
    body = (
        '===== PAGE 1 =====\n报告防伪\n===== PAGE 2 =====\n检测技术：\n高通量宏基因组测序\n'
        '===== PAGE 5 =====\n第一部分\n1.2 肠道年龄评估\n第二部分\n2.5 抗生素耐药基因\n第三部分\n3.21 帕金森综合征\n正文\n'
        '===== PAGE 13 =====\n' + table
    )
    assert _source_family(body) == 'GM'
    assert _source_family(body.replace('高通量宏基因组测序', '16S测序')) is None
    assert _source_family(body.replace('胃癌\n0.1\n低风险\n', '')) is None


def test_score_basis_names_what_lowered_the_score_in_plain_words():
    candidate = {
        'score': 55,
        'coverage': 'partial',
        'factors': {
            'C': {'points': 30, 'reason': '内部理由不发布'},
            'N': {'points': 5},
            'S': {'points': 0},
            'R': {'points': 5},
            'M': {'points': 0},
            'U': {'points': 5},
        },
    }
    chart = AssessmentScore(code='intestinal', name='肠道相关疾病')
    matrix = RiskMatrixRow(code='intestinal', name='肠道相关疾病')
    _publish_basis(chart, candidate)
    _publish_basis(matrix, candidate)
    assert chart.assessment_level == '重点关注'
    # Largest factor first, no deduction arithmetic for the reader.
    assert chart.explanation == '分数主要受已有的主要健康问题、其他相关问题、危险因素影响；部分相关检查缺少。'
    assert '内部理由' not in chart.explanation
    assert matrix.attention_level == '重点关注' and matrix.basis is None
    assert score_attention_label(90, 'limited') == '关注度低（资料有限）'
    assert score_attention_label(72) == '需要关注'
    assert _publish_basis({'code': 'x'}, candidate) is None  # dict rows of legacy callers are left alone


_PASSING = {
    'sameProblem': True,
    'nameSupported': False,
    'subjectAndCertaintyPreserved': False,
    'noNewDiagnosis': True,
    'categorySuitable': True,
    'attentionJustified': True,
    'nativeLinksSameDisease': False,
    'riskChapterRelevanceJustified': True,
    'normalScreeningNotPromoted': True,
    'focusLabelSuitable': True,
}


def test_a_group_rejected_only_for_its_wording_keeps_its_members_under_the_source_name():
    clinical = {
        'clinicalProblem:cp': MedicalFinding(
            code='clinicalProblem:cp', name='空腹C肽(C-P)检查异常', certainty='source_finding'
        ),
        'ezscan': MedicalFinding(code='ezscan', name='糖尿病早期风险筛查(EZSCAN)'),
        'summary:dm': MedicalFinding(code='summary:dm', name='主检结论：糖尿病筛查'),
    }
    group = _group(
        id='G5',
        name='糖代谢异常待评估（空腹胰岛素及C肽升高、HOMA-IR升高）',
        memberCodes=list(clinical),
        nativeCodes=['nativeAssociation:jkj_T2D'],
        certainty='potential_risk',
    )
    rejected = [{'id': 'G5', 'reason': 'independent_layout_review_not_passed', 'issues': [], 'checks': _PASSING}]
    kept = _source_named_groups([group], rejected, [], clinical)
    assert len(kept) == 1
    adopted = kept[0]
    # The proposed wording is replaced by the leading source row; the reviewed
    # routing survives, the vendor link that the reviewer refused does not.
    assert adopted['name'] == '空腹C肽(C-P)检查异常' and adopted['certainty'] == 'source_finding'
    assert adopted['memberCodes'] == list(clinical)
    assert adopted['includeInRiskChapter'] is True and adopted['category'] == 'metabolic'
    assert adopted['nativeCodes'] == [] and adopted['sourceNamed'] is True


def test_a_group_that_is_not_one_problem_or_adds_a_diagnosis_stays_scattered():
    clinical = _clinical()
    group = _group(memberCodes=['clinicalProblem:waist', 'clinicalProblem:tg'])
    for check in ('sameProblem', 'noNewDiagnosis'):
        rejected = [{'id': 'G1', 'issues': [], 'checks': {**_PASSING, check: False}}]
        assert _source_named_groups([group], rejected, [], clinical) == []
    # Members already adopted elsewhere are never taken twice.
    rejected = [{'id': 'G1', 'issues': [], 'checks': _PASSING}]
    owner = {'id': 'G0', 'memberCodes': ['clinicalProblem:tg']}
    assert _source_named_groups([group], rejected, [owner], clinical) == []


def test_routing_the_reviewer_refused_is_dropped_from_the_source_named_group():
    clinical = _clinical()
    group = _group(memberCodes=['clinicalProblem:waist', 'clinicalProblem:fat'])
    rejected = [{'id': 'G1', 'issues': [], 'checks': {**_PASSING, 'categorySuitable': False}}]
    adopted = _source_named_groups([group], rejected, [], clinical)[0]
    assert adopted['includeInRiskChapter'] is False
    assert adopted['category'] is None and adopted['attentionLevel'] is None


def test_an_unflagged_calculated_index_is_context_and_not_a_findings_row():
    facts = {
        'derived-mets': SimpleNamespace(source='根据体检记录计算：5项中具备≥3项', abnormal=False),
        'derived-bmi': SimpleNamespace(source='根据体检记录计算：BMI', abnormal=True),
        'lab-cp': SimpleNamespace(source='体检报告', abnormal=True),
    }

    def row(*ids):
        return MedicalFinding(name='x', sources=[SourceReference(source_code='A', record_id=fid) for fid in ids])

    assert _computed_context_only(row('derived-mets'), facts) is True
    assert _computed_context_only(row('derived-bmi'), facts) is False
    assert _computed_context_only(row('derived-mets', 'lab-cp'), facts) is False
    assert _computed_context_only(row(), facts) is False


def test_a_reviewer_correction_given_as_bare_factors_is_read_with_the_proposal_state():
    factors = {key: {'points': 0, 'factIds': [], 'groupCodes': [], 'reason': 'x'} for key in 'CNSRMU'}
    proposal = {'code': 'mental', 'coverage': 'partial', 'impact': 'none', 'impactFactIds': [], 'factors': {}}
    decision = _review_decision(factors, 'mental', proposal)
    assert decision == {
        'coverage': 'partial',
        'impact': 'none',
        'impactFactIds': [],
        'factors': factors,
        'code': 'mental',
    }
    full = {'code': 'mental', 'coverage': 'limited', 'factors': factors}
    assert _review_decision(full, 'mental', proposal) == full
    assert _review_decision(None, 'mental', proposal) is None


def test_a_repair_may_extend_an_accepted_group_only_through_review():
    from modules.report.evaluation_template_layout import _accepted_extensions
    from modules.report.evaluation_template_layout import _apply_extensions

    accepted = [
        _group(id='G1', memberCodes=['clinicalProblem:waist', 'clinicalProblem:fat']),
        _group(id='G9', role='normal_screening', includeInRiskChapter=False, memberCodes=['ecg']),
    ]
    remaining = {'summary:waist': object(), 'advice:inbody': object(), 'ecg-note': object()}
    proposed = [
        # The model re-proposes the locked group with the rows it forgot.
        _group(id='G1', memberCodes=['clinicalProblem:waist', 'summary:waist', 'advice:inbody']),
        _group(id='G9', memberCodes=['ecg-note']),
        _group(id='G30', name='其他', memberCodes=['ecg-note']),
    ]
    extensions, others = _accepted_extensions(proposed, accepted, remaining)
    assert [group['id'] for group in others] == ['G30']
    assert len(extensions) == 1 and extensions[0]['extends'] == 'G1'
    assert extensions[0]['memberCodes'] == [
        'clinicalProblem:waist',
        'clinicalProblem:fat',
        'summary:waist',
        'advice:inbody',
    ]
    # Nothing changes until the extension passes independent review.
    assert accepted[0]['memberCodes'] == ['clinicalProblem:waist', 'clinicalProblem:fat']
    applied = _apply_extensions(accepted, extensions)
    assert applied == [{'id': 'G1', 'addedMemberCodes': ['summary:waist', 'advice:inbody']}]
    assert accepted[0]['memberCodes'][-2:] == ['summary:waist', 'advice:inbody']


def test_grouped_results_are_listed_once_without_advice_or_capture_markers():
    from modules.report.evaluation_template_layout import _member_advice
    from modules.report.evaluation_template_layout import _reader_value

    members = [
        MedicalFinding(
            name='甘油三酯(TG)检查异常', value='甘油三酯(TG)：3.34 mmol/L（参考0.00-1.70；偏高）', source_status='偏高'
        ),
        MedicalFinding(name='总胆固醇', value='5.88', unit='mmol/L', reference_range='3.14-5.86', source_status='偏高'),
        MedicalFinding(name='主检结论：混合型高脂血症', value='建议心内科咨询就诊。必要时在医生指导下使用调脂药物。'),
        MedicalFinding(name='颈动脉斑块', value='建议心内科随诊，控制血压、血糖、血脂。'),
        MedicalFinding(
            name='痔疮发作', value='是，有痔疮发作', source_status='用户自述，未核验', certainty='self_reported'
        ),
        MedicalFinding(name='脂肪肝', value='腹部彩超：超声提示：脂肪肝。'),
        MedicalFinding(name='脂肪肝'),
    ]
    value = _reader_value(members)
    assert value == (
        '甘油三酯(TG)：3.34 mmol/L（参考0.00-1.70；偏高）；总胆固醇：5.88 mmol/L（参考3.14-5.86；偏高）；'
        '腹部彩超：超声提示：脂肪肝。；问卷：痔疮发作'
    )
    for hidden in ('主检', '建议', '未核验', '检查异常'):
        assert hidden not in value
    advice = _member_advice(members)
    # A conclusion line is split into its advice sentences; each is kept verbatim.
    assert '建议心内科咨询就诊。' in advice and '必要时在医生指导下使用调脂药物。' in advice
    assert '建议心内科随诊，控制血压、血糖、血脂。' in advice
    # An advice sentence without 建议 is still advice, never a listed result.
    renamed = MedicalFinding(name='血脂', value='必要时在医生指导下使用调脂药物。')
    assert _reader_value([*members, renamed]) == value


def test_score_format_slips_with_one_rubric_reading_do_not_void_a_dimension():
    from modules.report.evaluation_template_scores import _decision

    context = {'code': 'aging', 'groups': [], 'facts': {'fact-bmd': {'kind': 'clinical'}}}
    factors = {key: {'points': 0, 'factIds': [], 'groupCodes': [], 'reason': '无'} for key in 'CNSRMU'}
    factors['C'] = {'points': 0, 'factIds': ['fact-bmd'], 'groupCodes': [], 'reason': '骨密度正常，不计分'}
    factors['U'] = {'points': 5, 'factIds': [], 'groupCodes': [], 'reason': '资料有限'}
    decision = _decision({'code': 'aging', 'coverage': 'limited', 'factors': factors}, context)
    # The zero factor claims nothing; U follows the stated coverage (limited = 10).
    assert decision['factors']['C']['factIds'] == [] and decision['factors']['U']['points'] == 10
    assert decision['score'] == 90


def test_one_failed_scoring_call_does_not_lose_every_dimension():
    import asyncio

    from modules.report.evaluation_schema import EvaluationData
    from modules.report.evaluation_template_scores import score_template_dimensions

    data = EvaluationData()
    data.health_overview.assessment_chart = [AssessmentScore(code='aging', name='衰老相关疾病')]
    data.disease_risk_assessment.overview_matrix = [RiskMatrixRow(code='aging', name='衰老相关疾病')]
    document = SimpleNamespace(facts=[{'factId': 'fact-bmd', 'kind': 'clinical', 'name': '骨密度', 'value': '正常'}])
    factors = {key: {'points': 0, 'factIds': [], 'groupCodes': [], 'reason': '无'} for key in 'CNSRMU'}
    factors['U'] = {'points': 10, 'factIds': [], 'groupCodes': [], 'reason': '缺少针对性检查'}
    dimension = {'code': 'aging', 'coverage': 'limited', 'impact': 'none', 'impactFactIds': [], 'factors': factors}
    checks = dict.fromkeys(
        (
            'factBindingsValid',
            'independentProblemsDeduplicated',
            'noRepeatedFactorFacts',
            'severityAndFunctionSupported',
            'uncertaintyKeptSeparate',
            'sourceSignalsNotDiagnoses',
            'noDiseaseRiskInvented',
            'rubricApplied',
        ),
        True,
    )

    class Model:
        calls = 0

        async def generate(self, payload):
            Model.calls += 1
            if Model.calls == 2:  # the first independent review fails outright
                raise RuntimeError('model call failed')
            if payload['task'].startswith('review'):
                return {'reviews': [{'code': 'aging', 'verdict': 'pass', 'issues': [], **checks}]}
            return {'dimensions': [dimension]}

    audit = {}
    asyncio.run(score_template_dimensions(Model(), data, document, {'age': 48}, audit=audit))
    assert data.health_overview.assessment_chart[0].score == 90


def test_risk_background_bookkeeping_is_read_by_the_rubric():
    from modules.report.evaluation_template_scores import _decision

    facts = {fid: {'kind': 'self_reported'} for fid in ('f-father', 'f-mother', 'f-salt', 'f-bp')}
    context = {'code': 'mental', 'groups': [], 'facts': facts}
    factors = {key: {'points': 0, 'factIds': [], 'groupCodes': [], 'reason': '无'} for key in 'CNSRMU'}
    factors['R'] = {
        'points': 5,  # claimed 5, but three kinds are listed
        'factIds': ['f-father', 'f-salt'],
        'groupCodes': [],
        'reason': '家族、行为与临床背景',
        'backgrounds': [
            {'kind': 'family', 'factIds': ['f-father', 'f-mother']},
            {'kind': 'family', 'factIds': ['f-mother']},  # repeated kind
            {'kind': 'behavior', 'factIds': ['f-salt', 'f-father']},  # fact already used
            {'kind': 'clinical', 'factIds': ['f-bp']},
        ],
    }
    decision = _decision({'code': 'mental', 'coverage': 'adequate', 'factors': factors}, context)
    r = decision['factors']['R']
    assert r['points'] == 15 and sorted(r['factIds']) == ['f-bp', 'f-father', 'f-mother', 'f-salt']
    assert decision['score'] == 85
