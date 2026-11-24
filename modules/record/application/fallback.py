"""Deterministic publication when a Record model result remains unusable.

These helpers only create drafts. The service must durably save a draft before
publishing its acknowledgement, and owns answer receipts/history/idempotency.
No fallback turns raw user text into a clinical fact or a stock concern answer.
"""

from collections.abc import Mapping
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from agents.llm.text_quality import check_generated_value
from modules.record.application import flow as flow_policy
from modules.record.application import intake as intake_policy
from modules.record.application import interview as interview_policy
from modules.record.application import lifestyle as lifestyle_policy
from modules.record.application.dialogue import validate_dialogue_focus
from modules.record.application.presentation import COMPLETION_MESSAGE
from modules.record.application.presentation import acknowledgement_text
from modules.record.application.presentation import choice_context_options
from modules.record.application.presentation import default_acknowledgement
from modules.record.application.presentation import supplement_question
from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.presentation import validate_answer_entry_instructions
from modules.record.application.presentation import validate_copy_components
from modules.record.application.presentation import validate_response_style
from modules.record.application.presentation import with_answer_hint
from modules.record.application.public_text import strip_internal_notices
from modules.record.application.public_text import validate_internal_notice

MAX_CONSECUTIVE_FALLBACKS = 2
FINISH_OPTION = '先结束本次建档'
CONTINUE_OPTION = '继续补充'
OPENING_MESSAGE = default_acknowledgement({'phase': 'intake'})

_GAP_QUESTIONS = {
    'goal': '您这次希望了解或改善哪些具体问题？可多选；“暂无改善需求”“不清楚”请单独选择。',
    'medical_history': '您曾经被医生明确诊断过哪些疾病？可多选；“没有”或“不清楚”请单独选择。',
    'family_history': (
        '父母、兄弟姐妹等血亲中，有哪些您知道的疾病？可多选，也可以补充是哪位血亲；“没有”或“不清楚”请单独选择。'
    ),
    'habits': '关于日常饮食、作息或活动习惯，您还有哪些情况想补充？',
}

_GAP_OPTIONS = {
    'goal': interview_policy.goal_choice_fields()['options'],
    'habits': ['没有需要补充的内容', '目前记不清'],
}
_GAP_QUESTIONS.update(
    {
        dimension: lifestyle_policy.question_fields(dimension)['question']
        for dimension in intake_policy.LIFESTYLE_DIMENSIONS
    }
)
_GAP_OPTIONS.update(
    {
        dimension: lifestyle_policy.question_fields(dimension)['options']
        for dimension in intake_policy.LIFESTYLE_DIMENSIONS
    }
)


def _gap_question(target: dict) -> dict:
    dimension = target['dimension']
    if target.get('supplementation'):
        quotes = []
        for item in target.get('knownFacts', []):
            for evidence in item.get('evidence', []):
                if not isinstance(evidence, Mapping) or not evidence.get('source'):
                    continue
                quote = evidence.get('quote')
                if not isinstance(quote, str) or not quote.strip() or len(quote) > 300:
                    continue
                try:
                    check_generated_value(quote, chinese_context=True)
                    validate_answer_entry_instructions(quote)
                except ValueError:
                    continue
                if quote not in quotes:
                    quotes.append(quote)
        prefix = '资料中已提供“' + '；'.join(quotes[:2]) + '”。' if quotes else ''
        return {
            'question': prefix + f'关于{target["topic"]}，您还有哪些补充或更正？',
            **interview_policy.history_choice_fields(target),
        }
    choices = interview_policy.history_choice_fields(target) or {
        'questionType': 'checkbox'
        if dimension in ('goal', 'diet_structure', 'hydration', 'stress_coping')
        else 'radio',
        'options': list(_GAP_OPTIONS[dimension]),
    }
    if dimension in intake_policy.LIFESTYLE_DIMENSIONS:
        return lifestyle_policy.question_fields(dimension)
    return {
        'question': _GAP_QUESTIONS[dimension],
        **choices,
    }


def _next_intake_target(flow: dict, *, candidates=(), selection=None) -> dict | None:
    selection = selection or {}
    count = flow['intakeQuestionCount']
    if count < 3:
        target = interview_policy.fixed_target(flow, count)
    else:
        target = interview_policy.select_lifestyle_target(flow, candidates=candidates, **selection)
    return interview_policy.select_target(flow, target) if target is not None else None


def _limit_reason(flow: dict) -> str | None:
    if interview_policy.is_expanded(flow):
        if flow['qaRoundCount'] >= interview_policy.MAX_QA_ROUNDS:
            return 'qa_round_limit'
        if flow['qaVagueCount'] >= interview_policy.MAX_VAGUE_ANSWERS:
            return 'vague_question_limit'
    if flow['interactionCount'] >= interview_policy.budget(flow)['maxInteractions']:
        return 'interaction_limit'
    if flow['qaRoundCount'] >= interview_policy.MAX_QA_ROUNDS:
        return 'qa_round_limit'
    if flow['qaVagueCount'] >= interview_policy.MAX_VAGUE_ANSWERS:
        return 'vague_question_limit'
    return None


def _continue_question(
    draft: dict, *, intake: bool, qa: bool = False, candidates=(), selection=None
) -> tuple[dict, str | None]:
    flow = draft['qaFlow']
    if intake:
        target = _next_intake_target(flow, candidates=candidates, selection=selection)
        if target is not None:
            flow['phase'] = 'intake'
            return _gap_question(target), None
        flow['preQaAnswered'] = True
    flow.update(phase='qa' if qa else 'supplement', focus=None)
    return _supplement_question(), None


def _quality(draft: dict) -> dict:
    quality = draft['qaFlow'].setdefault('generationQuality', {})
    quality.setdefault('consecutiveFallbacks', 0)
    quality.setdefault('awaitingChoice', False)
    quality.setdefault('unprocessedQuestionIds', [])
    return quality


def _partial_value(partial, *names, default=None):
    for name in names:
        value = partial.get(name) if isinstance(partial, Mapping) else getattr(partial, name, None)
        if value is not None:
            return value
    return default


def _exact_choice(answer) -> str | None:
    if isinstance(answer, list) and len(answer) == 1:
        answer = answer[0]
    elif isinstance(answer, dict) and set(answer) == {'content'}:
        answer = answer['content']
    return answer.strip() if isinstance(answer, str) else None


def _publish_question(draft: dict, question: dict, message: str, reasoning: str = '', *, concern_id=None) -> None:
    validate_internal_notice(message)
    flow_policy.validate_new_question(
        flow_policy.GeneratedQuestion.model_validate(question),
        action='invite_supplement' if question.get('options') == [flow_policy.NO_MORE_OPTION] else 'fallback',
    )
    draft['currentQuestion'] = {
        **question,
        'question': with_answer_hint(question['question']),
        'questionId': f'q_{uuid4().hex}',
        'assistantMessage': message,
        'reasoning': reasoning,
        'phase': draft['qaFlow']['phase'],
        'concernId': concern_id,
        'intakeDimension': (draft['qaFlow'].get('focus') or {}).get('dimension')
        if draft['qaFlow']['phase'] == 'intake'
        else None,
        'intakeQuestionNumber': draft['qaFlow']['intakeQuestionCount'] + 1
        if draft['qaFlow']['phase'] == 'intake'
        else None,
    }


def _supplement_question() -> dict:
    return supplement_question()


def _opening_question() -> dict:
    return {
        'question': _GAP_QUESTIONS['goal'],
        **interview_policy.goal_choice_fields(),
    }


def _partial_finish(
    draft: dict,
    *,
    reason: str,
    message: str = '',
    reasoning: str = '',
    response_chars: int = 0,
) -> dict:
    flow = draft['qaFlow']
    validate_internal_notice(message)
    parts = [message] if message else []
    for concern in flow['concerns']:
        if concern['status'] != 'pending':
            continue
        # Closing a session does not resolve an unanswered question. Preserve
        # its status and original stored response; only project usable old copy.
        previous = strip_internal_notices(concern.get('response', ''))
        try:
            check_generated_value(previous, chinese_context=True)
            validate_answer_entry_instructions(previous, options=(draft.get('currentQuestion') or {}).get('options'))
            validate_response_style(previous)
            if concern.get('responseStyleVersion') != flow_policy.RESPONSE_STYLE_VERSION:
                previous = ''
        except ValueError:
            previous = ''
        if not previous or previous in parts:
            continue
        # Keep complete old partial responses, never cut a medical sentence.
        if response_chars + len(previous) <= flow_policy.MAX_TURN_RESPONSE_CHARS:
            parts.append(previous)
            response_chars += len(previous)
    parts.append(COMPLETION_MESSAGE)
    flow.update(
        phase='finished',
        endReason=reason,
        focus=None,
        finalMessage='\n\n'.join(parts),
        finalReasoning=reasoning,
    )
    _quality(draft)['awaitingChoice'] = False
    draft['currentQuestion'] = None
    return draft


def handle_control_answer(session: dict, answer, *, answer_already_recorded: bool = False) -> dict | None:
    """Only a displayed fallback choice and an exact user selection skip AI."""
    quality = session['qaFlow'].get('generationQuality') or {}
    current = session.get('currentQuestion') or {}
    choice = _exact_choice(answer)
    if (
        session['qaFlow']['phase'] in ('supplement', 'qa')
        and choice == flow_policy.NO_MORE_OPTION
        and quality.get('unprocessedQuestionIds')
    ):
        return _partial_finish(deepcopy(session), reason='no_more_questions')
    if not quality.get('awaitingChoice') or current.get('options') != [FINISH_OPTION, CONTINUE_OPTION]:
        return None
    if choice not in (FINISH_OPTION, CONTINUE_OPTION):
        return None
    draft = deepcopy(session)
    interview_policy.ensure_counters(draft)
    resume_phase = quality.get('resumePhase', session['qaFlow']['phase'])
    _quality(draft)['awaitingChoice'] = False
    reason = 'user_stop' if choice == FINISH_OPTION else _limit_reason(draft['qaFlow'])
    if reason:
        return _partial_finish(draft, reason=reason)
    # This is a control selection, so it never consumes an information turn.
    question, concern_id = _continue_question(
        draft,
        intake=resume_phase == 'intake',
        qa=resume_phase == 'qa',
        selection=interview_policy.selection_context(session),
    )
    _quality(draft).pop('resumePhase', None)
    _publish_question(
        draft,
        question,
        default_acknowledgement(draft['qaFlow']),
        concern_id=concern_id,
    )
    return draft


def mark_generation_success(draft: dict, *, repaired: bool = False) -> None:
    """A complete usable turn resets the streak without erasing earlier gaps."""
    quality = _quality(draft)
    quality.update(consecutiveFallbacks=0, awaitingChoice=False, mode='repaired' if repaired else 'normal')
    quality.pop('failureKind', None)


def _continuation_question(
    draft: dict,
    session: dict,
    *,
    decision: dict | None,
    incoming_phase: str,
    generated_question,
    partial_analysis,
    partial_understanding,
) -> tuple[dict, str | None]:
    flow = draft['qaFlow']
    if (
        (decision or {}).get('action') in ('open_intake', 'ask_intake')
        and ((decision or {}).get('target') or {}).get('dimension') in _GAP_QUESTIONS
        and flow['intakeQuestionCount'] < interview_policy.budget(flow)['maxIntake']
    ):
        target = interview_policy.select_target(flow, decision['target'])
        flow['phase'] = 'intake'
        question = generated_question.model_dump() if generated_question is not None else _gap_question(target)
        question.update(interview_policy.history_choice_fields(target))
        return question, None
    selection = interview_policy.selection_context(session)
    candidates = []
    gaps = _partial_value(partial_analysis, 'gaps', default=[]) if partial_analysis is not None else []
    if decision is None and gaps:
        candidate_flow = flow
        if partial_understanding is None:
            # Bind general gaps without treating absent observations as updates.
            gaps = [gap for gap in gaps if gap.scope == 'general' and not gap.complaintIndexes]
            candidate_flow = deepcopy(flow)
            partial_understanding = intake_policy.IntakeUnderstanding(complaints=[], facts=[])
            intake_policy.merge_understanding(candidate_flow, partial_understanding)
        candidates = interview_policy.lifestyle_targets(candidate_flow, partial_understanding, gaps, **selection)
    return _continue_question(
        draft,
        intake=(
            incoming_phase == 'intake'
            and ((decision or {}).get('action') not in ('invite_supplement', 'clarify_supplement'))
        ),
        qa=incoming_phase == 'qa',
        candidates=candidates,
        selection=selection,
    )


def build_fallback(
    session: dict,
    *,
    answer=None,
    answer_already_recorded: bool = False,
    validated_draft: dict | None = None,
    decision: dict | None = None,
    partial_analysis=None,
    analysis_partial: bool = False,
    partial_presentation=None,
    presentation_patient: dict | None = None,
    reasoning: str = '',
    failure_kind: str = 'validation',
) -> dict:
    """Keep validated facts/responses and publish a conservative continuation.

    A supplied draft with a decision has already advanced deterministic counters.
    A draft without a decision may contain individually validated intake facts,
    but its raw answer is still retained as not fully analysed. Presentation
    components must be individually validated by the output-repair collector.
    """
    draft = deepcopy(validated_draft if validated_draft is not None else session)
    interview_policy.ensure_counters(draft)
    flow = draft['qaFlow']
    quality = _quality(draft)
    previous_flow = session['qaFlow']
    previous_quality = previous_flow.get('generationQuality') or {}
    incoming_phase = (
        previous_quality.get('resumePhase', previous_flow['phase'])
        if previous_quality.get('awaitingChoice')
        else previous_flow['phase']
    )
    if decision is None:
        flow['phase'] = incoming_phase
    partial_understanding = None
    if decision is None and partial_analysis is not None:
        partial_intake = _partial_value(partial_analysis, 'partial_intake')
        if partial_intake is not None:
            intake_policy.merge_understanding(flow, partial_intake)
            partial_understanding = partial_intake
        partial_concerns = _partial_value(partial_analysis, 'concerns', default=[])
        valid_concerns = []
        for concern in partial_concerns:
            try:
                flow_policy.validate_concern_source(concern, session, answer)
                valid_concerns.append(concern)
            except ValueError:
                continue
        partial_concerns = valid_concerns
        if partial_concerns:
            flow_policy._merge_concerns(
                flow,
                SimpleNamespace(concerns=partial_concerns),
                (session.get('currentQuestion') or {}).get('questionId'),
                answer=answer,
            )
    if decision is None:
        partial_intake = _partial_value(partial_analysis, 'partial_intake') if partial_analysis is not None else None
        intake_policy.update_background_consent(
            flow, answer, getattr(partial_intake, 'backgroundConsent', 'unspecified')
        )
    analysis_usable = (
        decision is not None
        or flow['concerns'] != previous_flow['concerns']
        or any(
            (flow.get('intake') or {}).get(key) != (previous_flow.get('intake') or {}).get(key)
            for key in ('complaints', 'facts')
        )
    )
    if decision is None and answer is not None and not answer_already_recorded:
        flow['interactionCount'] = min(interview_policy.budget(flow)['maxInteractions'], flow['interactionCount'] + 1)
        if incoming_phase == 'intake':
            flow['intakeCount'] += 1
            flow['intakeQuestionCount'] += 1
        elif _exact_choice(answer) != flow_policy.NO_MORE_OPTION:
            # A failed analysis cannot classify a question as vague. Its attempt
            # still consumes one QA reply, without fabricating clinical intent.
            flow['qaRoundCount'] += 1
    if (decision is None or analysis_partial) and answer is not None:
        question_id = (session.get('currentQuestion') or {}).get('questionId')
        if question_id and question_id not in quality['unprocessedQuestionIds']:
            quality['unprocessedQuestionIds'].append(question_id)

    parts = []
    patient_message = _partial_value(partial_presentation, 'assistantMessage', 'assistant_message')
    generated_question = _partial_value(partial_presentation, 'question')
    proposed_options = getattr(generated_question, 'options', None)
    choices = (
        interview_policy.history_choice_fields(decision.get('target'))
        if (decision or {}).get('action') in ('open_intake', 'ask_intake')
        else {}
    )
    if generated_question is not None:
        try:
            flow_policy.validate_new_question(generated_question, action=(decision or {}).get('action', 'fallback'))
            validate_answer_entry_instructions(generated_question.question, options=choices.get('options'))
        except ValueError:
            generated_question = None
    reason = _limit_reason(flow)
    end_reason = (decision or {}).get('endReason')
    explicit_no_more = _exact_choice(answer) == flow_policy.NO_MORE_OPTION and incoming_phase in ('supplement', 'qa')
    trusted_finish = (
        (decision or {}).get('action') == 'finish'
        and bool(end_reason)
        and (
            end_reason in ('qa_round_limit', 'vague_question_limit', 'interaction_limit')
            or (end_reason == 'user_stop' and not analysis_partial)
            or explicit_no_more
        )
    )
    continuation = {
        'decision': decision,
        'incoming_phase': incoming_phase,
        'generated_question': generated_question,
        'partial_analysis': partial_analysis,
        'partial_understanding': partial_understanding,
    }
    if reason or trusted_finish or explicit_no_more:
        options = (session.get('currentQuestion') or {}).get('options')
    else:
        # Check retained components against the actual fallback choices, even
        # when no model question survived. Preview on a copy so selection does
        # not count as a published topic or change fallback control behavior.
        preview_draft = deepcopy(draft)
        preview, _ = _continuation_question(preview_draft, session, **continuation)
        options = preview['options']
    options = choice_context_options(options, proposed_options)
    if patient_message is not None:
        try:
            actual_dimension = (
                (preview_draft['qaFlow'].get('focus') or {}).get('dimension')
                if not (reason or trusted_finish or explicit_no_more)
                else None
            )
            expected_dimension = ((decision or {}).get('target') or {}).get('dimension')
            if expected_dimension is not None and actual_dimension != expected_dimension:
                raise ValueError('fallback_target_changed')
            validate_dialogue_focus(patient_message, presentation_patient or {}, action=(decision or {}).get('action'))
            validate_acknowledgement_style(
                patient_message,
                action=(decision or {}).get('action', 'fallback'),
                has_responses=bool((decision or {}).get('responses')),
            )
            validate_answer_entry_instructions(patient_message.acknowledgement, options=options)
            validate_answer_entry_instructions(patient_message.rationale, options=options)
        except ValueError:
            patient_message = None
    retained_responses = _partial_value(partial_presentation, 'responses', default=[])
    if isinstance(retained_responses, Mapping):
        retained_responses = list(retained_responses.values())
    instructions = {item['concernId']: item for item in (decision or {}).get('responses', [])}
    known = {item['id']: item for item in flow['concerns']}
    invalid_reuse = set()
    for identity, instruction in instructions.items():
        if 'reuseResponse' not in instruction or identity not in known:
            continue
        # A fresh answer can refer to the earlier reply, never publish it as a
        # replacement for this turn. Already published history is untouched.
        flow_policy.remember_response(known[identity], '')
        known[identity]['status'] = 'pending'
        invalid_reuse.add(identity)
    response_count = 0
    response_chars = 0
    seen = set()
    for response in retained_responses:
        identity = response.concernId
        instruction = instructions.get(identity)
        if identity in seen or identity in invalid_reuse or identity not in known or instruction is None:
            continue
        seen.add(identity)
        concern = known[identity]
        response_text = response.text
        try:
            validate_answer_entry_instructions(response_text, options=options)
            validate_response_style(response_text, previous_response=instruction.get('previousResponse', ''))
            for previous in instruction.get('previousResponses', []):
                validate_response_style(response_text, previous_response=previous)
            validate_copy_components(
                SimpleNamespace(acknowledgement='', rationale=''),
                [*parts, response_text],
                action=(decision or {}).get('action', 'invite_supplement'),
            )
        except ValueError:
            flow_policy.remember_response(concern, '')
            concern['status'] = 'pending'
            continue
        if response_chars + len(response_text) > flow_policy.MAX_TURN_RESPONSE_CHARS:
            if 'reuseResponse' not in instruction:
                flow_policy.remember_response(concern, '')
                concern['status'] = 'pending'
            continue
        if 'reuseResponse' not in instruction:
            concern['handledCount'] += 1
            flow_policy.remember_response(concern, response.text)
            concern['status'] = instruction['resolution']
            concern['responseStyleVersion'] = flow_policy.RESPONSE_STYLE_VERSION
        parts.append(concern['response'])
        response_chars += len(response_text)
        response_count += 1

    acknowledgement = ''
    focus_ref = None
    if response_count or reason or trusted_finish or explicit_no_more:
        # The first substantive explanation bridges naturally into QA. Do not
        # prepend a separate recap; a closing-only turn uses the fixed ending.
        pass
    elif (
        patient_message is not None
        and patient_message.acknowledgement.strip()
        and (decision or {}).get('action') != 'finish'
    ):
        acknowledgement = acknowledgement_text(
            patient_message, action=(decision or {}).get('action', 'invite_supplement')
        )
        focus_ref = getattr(patient_message, 'focusRef', None)
    elif answer is not None:
        acknowledgement = default_acknowledgement(preview_draft['qaFlow'])
    else:
        acknowledgement = OPENING_MESSAGE
    if acknowledgement:
        parts.insert(0, acknowledgement)
    # Analysis gaps and unanswered concerns remain in internal state. They do
    # not add processing notices or synthetic replies to the conversation.

    whole_fallback = not (
        analysis_usable or patient_message is not None or response_count or generated_question is not None
    )
    if whole_fallback and answer is not None:
        quality['consecutiveFallbacks'] += 1
    elif not whole_fallback:
        quality['consecutiveFallbacks'] = 0
    quality.update(mode='fallback' if whole_fallback else 'partial', failureKind=failure_kind, awaitingChoice=False)

    if reason or trusted_finish or explicit_no_more:
        return _partial_finish(
            draft,
            reason=reason or end_reason or 'no_more_questions',
            message='\n\n'.join(parts),
            reasoning=reasoning,
            response_chars=response_chars,
        )

    if whole_fallback and answer is not None and quality['consecutiveFallbacks'] >= MAX_CONSECUTIVE_FALLBACKS:
        quality.update(awaitingChoice=True, resumePhase=incoming_phase)
        flow.update(phase='supplement', focus=None)
        question = {
            'question': '您希望先结束本次建档，还是继续补充？',
            'questionType': 'radio',
            'options': [FINISH_OPTION, CONTINUE_OPTION],
        }
        concern_id = None
    else:
        question, concern_id = _continuation_question(draft, session, **continuation)
    # A failed presentation may have provisionally selected finish; continuation
    # must never retain terminal metadata from that uncommitted draft.
    flow.update(finalMessage='', finalReasoning='', endReason=None)
    _publish_question(draft, question, '\n\n'.join(parts), reasoning, concern_id=concern_id)
    draft['currentQuestion']['acknowledgementText'] = acknowledgement
    draft['currentQuestion']['dialogueFocusRef'] = focus_ref
    return draft
