"""Fixed interview themes, scoped facts, legacy consent, and patient response quality."""

import json
from copy import deepcopy

import pytest

from modules.record.application import fallback
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application import interview
from modules.record.application.output_repair import AnalysisRepair
from modules.record.application.output_repair import PresentationRepair

from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit
from .test_intake_coverage import advance
from .test_intake_coverage import fact
from .test_intake_coverage import session
from .test_output_repair import message
from .test_output_repair import patient


def gap(dimension, *, optional=False, **kwargs):
    return {
        'topic': intake.DIMENSION_LABELS[dimension],
        'dimension': dimension,
        'scope': 'general',
        'informationNeeded': intake.DIMENSION_LABELS[dimension],
        'reason': '补充报告没有记录的日常背景' if optional else '了解用户关注的健康背景信息',
        'optionalBackground': optional,
        **kwargs,
    }


def assessment(*dimensions, facts=(), optional=False, consent='unspecified', sufficient=False):
    return flow.PatientAssessment(
        intent='answer',
        substantive=True,
        intakeSufficient=sufficient,
        summary='用户关注日常健康管理，没有提出具体不适。',
        concerns=[],
        intake={'complaints': [], 'facts': list(facts), 'backgroundConsent': consent},
        gaps=[gap(dimension, optional=optional) for dimension in dimensions],
    )


def lifestyle_session(*dimensions):
    snapshot = session()
    snapshot, _ = advance(snapshot, assessment(*dimensions), '想了解整体健康，没有不适。')
    snapshot, _ = advance(snapshot, assessment(*dimensions), '没有已知疾病。')
    snapshot, decision = advance(snapshot, assessment(*dimensions), '没有已知的血亲家族病史。')
    assert snapshot['qaFlow']['intakeQuestionCount'] == 3
    return snapshot, decision


def legacy_invitation(dimension='diet_structure'):
    """An already cached old invitation; new interview code does not issue it."""
    snapshot = session()
    snapshot['qaFlow']['intake'].update(backgroundConsent='invited', backgroundDimension=dimension)
    snapshot['currentQuestion'] = {
        'questionId': 'legacy-background-invitation',
        'question': '是否愿意补充饮食背景？',
        'questionType': 'radio',
        'options': [intake.BACKGROUND_ACCEPT, intake.BACKGROUND_DECLINE],
        'phase': 'intake',
    }
    return snapshot


@pytest.mark.parametrize('goal', ['控制体重', '改善饮食', '了解整体健康'])
def test_wellness_goals_cover_fixed_history_slots_before_independent_lifestyle_topics(goal):
    snapshot = session()
    snapshot, decision = advance(
        snapshot,
        assessment('diet_structure', 'activity', facts=[fact('goal', goal, scope='general')]),
        goal,
    )
    assert decision['target']['dimension'] == 'medical_history'
    snapshot, decision = advance(snapshot, assessment('diet_structure', 'activity'), '没有已知疾病。')
    assert decision['target']['dimension'] == 'family_history'
    snapshot, decision = advance(snapshot, assessment('diet_structure', 'activity'), '没有已知血亲家族病史。')
    assert decision['target']['dimension'] == 'diet_structure'
    snapshot, decision = advance(
        snapshot,
        assessment('diet_structure', 'activity', facts=[fact('diet_structure', '清淡饮食', scope='general')]),
        '清淡饮食',
    )
    assert decision['target']['dimension'] == 'activity'
    assert snapshot['qaFlow']['intake']['complaints'] == []
    asked = snapshot['qaFlow']['intake']['asked']
    assert asked == ['general:medical_history', 'general:family_history', 'general:diet_structure', 'general:activity']
    snapshot, decision = advance(snapshot, assessment(sufficient=True), '每天走路上下班')
    assert decision['action'] == 'invite_supplement'
    assert snapshot['qaFlow']['intakeQuestionCount'] == 5
    assert snapshot['currentQuestion']['options'] == ['没有了']


@pytest.mark.parametrize('dimension', sorted(intake.LIFESTYLE_DIMENSIONS))
@pytest.mark.parametrize('status', ['known', 'unknown', 'declined'])
def test_specific_background_covers_only_its_own_purpose(dimension, status):
    parsed = assessment(
        *sorted(intake.LIFESTYLE_DIMENSIONS),
        facts=[fact(dimension, '用户已说明该项情况', scope='general', status=status)],
    )
    state = flow.new_flow()
    intake.merge_understanding(state, parsed.intake)
    candidates = intake.candidates(state, intake.resolve_gaps(state, parsed.intake, parsed.gaps))
    assert {item.dimension for item in candidates} == intake.LIFESTYLE_DIMENSIONS - {dimension}


@pytest.mark.parametrize('dimension', sorted(intake.LIFESTYLE_DIMENSIONS))
def test_background_cannot_be_reasked_by_changing_to_general_scope(dimension):
    state = flow.new_flow()
    parsed = assessment(dimension)
    parsed.intake = intake.IntakeUnderstanding(
        complaints=[{'text': '用户的原有关注', 'evidence': [{'source': '本次回答', 'quote': '用户的原有关注'}]}],
        facts=[{**fact(dimension, '已了解的信息', scope='complaint'), 'complaintIndexes': [0]}],
    )
    intake.merge_understanding(state, parsed.intake)
    state['intake']['focusIds'] = [state['intake']['complaints'][0]['id']]
    assert intake.candidates(state, intake.resolve_gaps(state, parsed.intake, parsed.gaps)) == []


def test_non_digestive_report_keeps_known_history_slot_as_supplementation():
    snapshot = session()
    snapshot['currentQuestion'] = None
    report = '眼科检查：近视加深，建议配镜。'
    parsed = assessment(facts=[fact('medical_history', report, scope='general', source='本次报告')])
    flow.validate_assessment(parsed, snapshot, None, sources={'本次报告': report})
    draft, decision = flow.advance_flow(snapshot, parsed)
    assert decision['action'] == 'open_intake'
    assert decision['target']['dimension'] == 'goal'
    assert draft['qaFlow']['intake']['complaints'] == []
    question = fallback.build_fallback(snapshot, validated_draft=draft, decision=decision)['currentQuestion']
    assert '排便' not in question['question'] + ''.join(question['options'])
    assert {'便秘', '入睡困难', '减轻体重'} <= set(question['options'])
    draft['currentQuestion'] = question
    draft, decision = advance(draft, assessment('sleep'), '希望了解如何安排日常作息')
    target = decision['target']
    assert target['dimension'] == 'medical_history'
    assert target['supplementation'] is True
    assert target['knownFacts'][0]['evidence'] == [{'source': '本次报告', 'quote': report}]
    assert draft['qaFlow']['intake']['complaints'] == []


@pytest.mark.parametrize('legacy_scope', ['general', 'complaint'])
def test_legacy_habits_remain_unsplit_and_receive_only_the_required_supplement_slot(legacy_scope):
    snapshot = session()
    state = snapshot['qaFlow']['intake']
    state['schemaVersion'] = 2
    if legacy_scope == 'complaint':
        state['complaints'] = [{'id': 'old', 'text': '原有关注点', 'evidence': []}]
        state['focusIds'] = ['old']
    owner = 'general' if legacy_scope == 'general' else 'old'
    state['facts'][f'{owner}:habits'] = {
        'dimension': 'habits',
        'scope': legacy_scope,
        'status': 'known',
        'summary': '旧版已收集生活习惯，无法可靠拆分',
        'evidence': [],
    }
    snapshot['qaFlow'].update(intakeQuestionCount=2, intakeCount=2, interactionCount=2)
    original = deepcopy(snapshot)
    updated, decision = advance(snapshot, assessment('diet_structure', 'activity'), '没有家族史补充')
    assert snapshot == original
    assert decision['target']['dimension'] == 'habits'
    assert decision['target']['supplementation'] is True
    assert list(updated['qaFlow']['intake']['facts']) == [f'{owner}:habits']
    assert updated['qaFlow']['intake']['schemaVersion'] == 3
    updated, decision = advance(updated, assessment(sufficient=True), '没有其他生活习惯补充')
    assert decision['action'] == 'invite_supplement'
    assert updated['qaFlow']['intakeQuestionCount'] == 4


def test_new_lifestyle_question_is_direct_and_does_not_offer_refusal_choices():
    snapshot, decision = lifestyle_session('diet_structure')
    assert decision['action'] == 'ask_intake'
    assert decision['target']['dimension'] == 'diet_structure'
    assert decision['target']['optionalBackground'] is False
    assert snapshot['qaFlow']['intake']['backgroundConsent'] == 'unasked'
    assert 'general:diet_structure' in snapshot['qaFlow']['intake']['asked']
    assert snapshot['qaFlow']['intake']['facts'] == {}
    assert intake.BACKGROUND_DECLINE not in snapshot['currentQuestion']['options']


@pytest.mark.parametrize(
    'answer', [intake.BACKGROUND_DECLINE, [intake.BACKGROUND_DECLINE], {'content': intake.BACKGROUND_DECLINE}]
)
def test_cached_legacy_refusal_overrides_model_consent_without_creating_health_facts(answer):
    snapshot = legacy_invitation()
    old_goal = assessment(facts=[fact('goal', '了解整体健康', scope='general')])
    intake.merge_understanding(snapshot['qaFlow'], old_goal.intake)
    intake.update_background_consent(snapshot['qaFlow'], answer, 'accepted')
    state = snapshot['qaFlow']['intake']
    parsed = assessment('diet_structure', 'activity', optional=True)
    intake.merge_understanding(snapshot['qaFlow'], parsed.intake)
    candidates = intake.candidates(
        snapshot['qaFlow'], intake.resolve_gaps(snapshot['qaFlow'], parsed.intake, parsed.gaps)
    )
    assert candidates == []
    assert state['backgroundConsent'] == 'declined'
    assert state['acceptedBackgroundDimensions'] == []
    assert list(state['facts']) == ['general:goal']
    assert state['facts']['general:goal']['summary'] == '了解整体健康'


@pytest.mark.parametrize(
    ('answer', 'consent', 'expected'), [('先不聊这些', 'declined', 'declined'), ('随便吧', 'unspecified', 'deferred')]
)
def test_cached_legacy_custom_consent_never_becomes_normal_health(answer, consent, expected):
    snapshot = legacy_invitation()
    intake.update_background_consent(snapshot['qaFlow'], answer, consent)
    assert snapshot['qaFlow']['intake']['backgroundConsent'] == expected
    assert snapshot['qaFlow']['intake']['facts'] == {}
    assert snapshot['qaFlow']['intake']['asked'] == []


def test_cached_legacy_consent_is_scoped_to_the_accepted_purpose():
    snapshot = legacy_invitation()
    intake.update_background_consent(snapshot['qaFlow'], intake.BACKGROUND_ACCEPT)
    state = snapshot['qaFlow']
    assert state['intake']['acceptedBackgroundDimensions'] == ['diet_structure']
    parsed = assessment('diet_structure', optional=True)
    intake.merge_understanding(state, parsed.intake)
    target = intake.select_gap(state, intake.candidates(state, intake.resolve_gaps(state, parsed.intake, parsed.gaps)))
    assert not target.get('backgroundInvitation')
    assert state['intake']['asked'] == ['general:diet_structure']
    parsed = assessment('activity', optional=True)
    intake.merge_understanding(state, parsed.intake)
    target = intake.select_gap(state, intake.candidates(state, intake.resolve_gaps(state, parsed.intake, parsed.gaps)))
    assert target['backgroundInvitation'] is True
    assert state['intake']['backgroundDimension'] == 'activity'
    assert state['intake']['acceptedBackgroundDimensions'] == ['diet_structure']


def test_cached_legacy_acceptance_with_actual_information_does_not_reask_it():
    snapshot = legacy_invitation()
    answer = '可以，我平时经常外食'
    parsed = assessment(
        'diet_structure',
        optional=True,
        consent='accepted',
        facts=[fact('diet_structure', '平时经常外食', scope='general')],
    )
    flow.validate_assessment(parsed, snapshot, answer)
    intake.merge_understanding(snapshot['qaFlow'], parsed.intake)
    intake.update_background_consent(snapshot['qaFlow'], answer, parsed.intake.backgroundConsent)
    assert (
        intake.candidates(snapshot['qaFlow'], intake.resolve_gaps(snapshot['qaFlow'], parsed.intake, parsed.gaps)) == []
    )
    assert snapshot['qaFlow']['intake']['backgroundConsent'] == 'accepted'
    assert snapshot['qaFlow']['intake']['asked'] == []


def test_consent_cannot_be_invented_before_an_invitation():
    with pytest.raises(ValueError, match='仅根据用户'):
        flow.validate_assessment(assessment(consent='accepted'), session(), '我想了解体重管理')


@pytest.mark.parametrize('invalid', [{'scope': 'primary'}, {'dimension': 'onset'}, {'essential': True}])
def test_invalid_independent_background_is_rejected_and_repair_keeps_valid_gaps(invalid):
    raw = assessment('diet_structure', optional=True).model_dump()
    raw['gaps'].insert(0, {**gap('activity', optional=True), **invalid})
    collector = AnalysisRepair(session(), '了解整体健康')
    parsed = collector.ingest(json.dumps(raw, ensure_ascii=False))
    assert parsed is not None
    assert [item.dimension for item in parsed.gaps] == ['diet_structure']
    assert collector.diagnostics['invalidCounts']['gaps'] == 1


def test_partial_analysis_retains_cached_legacy_refusal_during_repair():
    collector = AnalysisRepair(legacy_invitation(), '暂时不想聊这些')
    raw = assessment(consent='declined').model_dump()
    raw['intent'] = 'invalid'
    assert collector.ingest(json.dumps(raw, ensure_ascii=False)) is None
    assert collector.partial_intake.backgroundConsent == 'declined'
    parsed = collector.ingest(json.dumps({'intent': 'answer'}))
    assert parsed.intake.backgroundConsent == 'declined'


@pytest.mark.parametrize('status', ['unknown', 'declined'])
def test_free_text_unavailable_lifestyle_keeps_evidence_and_moves_to_another_dimension(status):
    snapshot, _ = lifestyle_session('diet_structure', 'activity')
    answer = '不清楚饮食情况' if status == 'unknown' else '暂时不想说饮食情况'
    parsed = assessment(
        'diet_structure', 'activity', facts=[fact('diet_structure', answer, scope='general', status=status)]
    )
    snapshot, decision = advance(snapshot, parsed, answer)
    saved = snapshot['qaFlow']['intake']['facts']['general:diet_structure']
    assert saved['status'] == status
    assert saved['summary'] == answer
    assert saved['evidence'] == [{'source': '本次回答', 'quote': answer}]
    assert decision['target']['dimension'] == 'activity'
    assert snapshot['qaFlow']['intake']['backgroundConsent'] == 'unasked'


@pytest.mark.parametrize('answer', ['暂时不想说饮食情况', '我不方便说饮食情况'])
def test_analysis_failure_does_not_lose_original_answer_or_claim_healthy_background(answer):
    snapshot, _ = lifestyle_session('diet_structure', 'activity')
    draft = fallback.build_fallback(snapshot, answer=answer)
    assert 'general:diet_structure' not in draft['qaFlow']['intake']['facts']
    assert snapshot['currentQuestion']['questionId'] in draft['qaFlow']['generationQuality']['unprocessedQuestionIds']
    assert draft['qaFlow']['intake']['backgroundConsent'] == 'unasked'
    assert draft['currentQuestion']['intakeDimension'] != 'diet_structure'
    assert '尚未完成可靠分析' not in draft['currentQuestion']['assistantMessage']
    assert '正常' not in draft['currentQuestion']['assistantMessage']


@pytest.mark.parametrize(
    'dimension', ['goal', 'medical_history', 'family_history', *sorted(intake.LIFESTYLE_DIMENSIONS)]
)
def test_every_legal_targeted_fallback_has_usable_shortcuts_and_free_input(dimension):
    before = session()
    before['qaFlow']['interviewPolicy'] = interview.new_policy()
    count = 0 if dimension in ('goal', 'medical_history') else 1 if dimension == 'family_history' else 2
    before['qaFlow'].update(intakeQuestionCount=count, intakeCount=count, interactionCount=count)
    answer = None if dimension == 'goal' else '用户的实际回答'
    draft, decision = flow.advance_flow(before, assessment(dimension), answer=answer)
    assert decision['target']['dimension'] == dimension
    draft = fallback.build_fallback(before, answer=answer, validated_draft=draft, decision=decision)
    question = draft['currentQuestion']
    assert question['questionType'] in ('radio', 'checkbox')
    assert len(question['options']) >= 2
    assert '文字或语音' in question['question']
    assert question['intakeDimension'] == dimension
    assert intake.BACKGROUND_DECLINE not in question['options']


@pytest.mark.parametrize('action', ['open_intake', 'ask_intake', 'clarify_concern'])
def test_repair_rejects_new_text_questions_but_preserves_valid_message(action):
    collector = PresentationRepair({'action': action, 'responses': []}, patient())
    raw = {
        'assistantMessage': message(),
        'responses': [],
        'question': {'question': '请自由描述。', 'questionType': 'text', 'options': {'content': ''}},
    }
    assert collector.ingest(json.dumps(raw, ensure_ascii=False)) is None
    assert collector.partial_presentation['assistantMessage'] is not None
    assert collector.partial_presentation['question'] is None
    raw['question'] = {'question': '更接近哪种情况？', 'questionType': 'radio', 'options': ['偶尔出现', '经常出现']}
    assert collector.ingest(json.dumps(raw, ensure_ascii=False)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('degraded', [False, True])
async def test_free_text_refusal_survives_service_save_restore_and_retry(streaming, degraded):
    manager, module, gateway, _ = setup_manager()
    original = gateway.content
    next_analysis = assessment().model_dump()

    def content(request):
        if request.profile.name == 'record.plan':
            gateway.requests.append(request)
            return json.dumps(next_analysis, ensure_ascii=False)
        return original(request)

    gateway.content = content
    current = await manager.createSession('u', 's')
    next_analysis = assessment('diet_structure', 'activity').model_dump()
    current = await submit(manager, current, '没有不适，想了解整体健康', streaming=streaming)
    assert cached(module)['currentQuestion']['intakeDimension'] == 'medical_history'
    current = await submit(manager, current, '没有已知疾病', streaming=streaming)
    assert cached(module)['currentQuestion']['intakeDimension'] == 'family_history'
    current = await submit(manager, current, '没有已知血亲家族病史', streaming=streaming)
    assert cached(module)['currentQuestion']['intakeDimension'] == 'diet_structure'
    refused_question = current
    answer = '暂时不想说饮食情况'
    next_analysis = (
        {}
        if degraded
        else assessment(
            'diet_structure', 'activity', facts=[fact('diet_structure', answer, scope='general', status='declined')]
        ).model_dump()
    )
    current = await submit(manager, refused_question, answer, streaming=streaming)
    saved = cached(module)
    state = saved['qaFlow']['intake']
    assert saved['qaHistory'][-1]['answer'] == answer
    assert state['backgroundConsent'] == 'unasked' and state['complaints'] == []
    assert saved['currentQuestion']['intakeDimension'] == 'activity'
    assert (current['intakeQuestionCount'], current['qaRoundCount']) == (4, 0)
    if degraded:
        assert 'general:diet_structure' not in state['facts']
        assert refused_question['questionId'] in saved['qaFlow']['generationQuality']['unprocessedQuestionIds']
        assert '正常' not in current['assistantMessage']
    else:
        assert state['facts']['general:diet_structure']['status'] == 'declined'
        assert state['facts']['general:diet_structure']['evidence'] == [{'source': '本次回答', 'quote': answer}]
    assert (await manager.getSession('u', 's')) == {key: value for key, value in current.items() if key != 'type'}
    before = deepcopy(cached(module))
    calls = len(gateway.requests)
    assert await submit(manager, refused_question, answer, streaming=streaming) == current
    assert cached(module) == before
    assert len(gateway.requests) == calls
