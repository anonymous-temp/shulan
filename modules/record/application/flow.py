"""Deterministic Record flow policy and validated, patient-focused model results.

The business snapshot owns counters. No model can change a counter or silently
drop an accepted concern. Patient analysis receives a projection, never this
entire state. A successful user submission advances the global counter at most
once even when several concerns are answered together.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from pydantic import AliasChoices
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import field_validator
from pydantic import model_validator

from agents.llm.text_quality import check_generated_value
from modules.record.application import intake as intake_policy
from modules.record.application import interview as interview_policy
from modules.record.application.intake import Dimension
from modules.record.application.intake import IntakeEvidence
from modules.record.application.intake import IntakeUnderstanding
from modules.record.application.intake import normalize_dimension
from modules.record.application.presentation import COMPLETION_MESSAGE
from modules.record.application.presentation import NO_MORE_OPTION
from modules.record.application.presentation import acknowledgement_text
from modules.record.application.presentation import choice_context_options
from modules.record.application.presentation import normalize_option
from modules.record.application.presentation import question_task
from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.presentation import validate_answer_entry_instructions
from modules.record.application.presentation import validate_choice_labels
from modules.record.application.presentation import validate_copy_components
from modules.record.application.presentation import validate_response_style
from modules.record.application.presentation import with_answer_hint
from modules.record.application.public_text import strip_internal_notices
from modules.record.application.validation import RecordValidationError

FLOW_VERSION = 2
INTAKE_TARGET = interview_policy.INTAKE_TARGET
MIN_INTAKE_TURNS = interview_policy.MIN_INTAKE_TURNS
MAX_INTAKE_TURNS = interview_policy.MAX_INTAKE_TURNS
MAX_QA_ROUNDS = interview_policy.MAX_QA_ROUNDS
MAX_VAGUE_ANSWERS = interview_policy.MAX_VAGUE_ANSWERS
OPENING_MAX_OPTIONS = interview_policy.OPENING_MAX_OPTIONS
MAX_INTERACTIONS = 25
MAX_CONCERN_HANDLINGS = 2
RESPONSE_STYLE_VERSION = 15
MAX_RESPONSE_CHARS = 400
MAX_TURN_RESPONSE_CHARS = 900
_REFUSAL_SHORTCUTS = (
    '不想回答',
    '不愿回答',
    '不愿意回答',
    '暂不回答',
    '不方便',
    '不便回答',
    '不想补充',
    '不愿补充',
    '不愿意补充',
    '暂不补充',
    '不想说',
    '不愿说',
    '不愿透露',
    '暂时不说',
    '以后再说',
    '跳过',
    '稍后回答',
)
ASPECTS = ('symptoms', 'habits', 'medical_history', 'family_history', 'other_symptoms')
ASPECT_LABELS = {
    'symptoms': '症状表现',
    'habits': '生活习惯',
    'medical_history': '既往病史',
    'family_history': '家族病史',
    'other_symptoms': '其他症状',
}
INTENT_LABELS = {
    'answer': '回答问题',
    'additional': '补充疑问',
    'no_more': '没有更多问题',
    'stop': '结束交流',
    'unclear': '表达不明确',
}


class ModelObject(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class InformationGap(ModelObject):
    topic: str = Field(validation_alias=AliasChoices('主题', 'topic'), min_length=1, max_length=160)
    aspect: Literal['symptoms', 'habits', 'medical_history', 'family_history', 'other_symptoms'] = Field(
        validation_alias=AliasChoices('角度', 'aspect')
    )
    informationNeeded: str = Field(
        validation_alias=AliasChoices('待补充信息', 'informationNeeded'), min_length=1, max_length=1500
    )
    dimension: Dimension | None = Field(default=None, validation_alias=AliasChoices('内容', 'dimension'))
    scope: Literal['complaint', 'primary', 'general'] = Field(
        default='primary', validation_alias=AliasChoices('范围', 'scope')
    )
    complaintIndexes: list[int] = Field(
        default_factory=list, validation_alias=AliasChoices('困扰索引', 'complaintIndexes')
    )
    _complaint_ids: list[str] = PrivateAttr(default_factory=list)
    reason: str = Field(default='', validation_alias=AliasChoices('提问关联', 'reason'), max_length=800)
    relevance: Literal['focused', 'general'] = Field(
        default='general', validation_alias=AliasChoices('相关性', 'relevance')
    )
    essential: bool = Field(default=False, validation_alias=AliasChoices('关键缺口', 'essential'))
    optionalBackground: bool = Field(default=False, validation_alias=AliasChoices('可选背景', 'optionalBackground'))

    _dimension = field_validator('dimension', mode='before')(normalize_dimension)

    @property
    def complaintIds(self) -> list[str]:
        return self._complaint_ids

    @complaintIds.setter
    def complaintIds(self, value: list[str]) -> None:
        self._complaint_ids = list(value)

    @model_validator(mode='before')
    @classmethod
    def derive_aspect(cls, value):
        if not isinstance(value, dict):
            return value
        dimension = normalize_dimension(value.get('内容', value.get('dimension')))
        if not isinstance(dimension, str) or dimension not in intake_policy.DIMENSION_LABELS:
            return value
        # The fine-grained purpose determines the legacy broad classification.
        # Keep both aliases if present so extra='forbid' still rejects ambiguity.
        result = dict(value)
        result['角度' if '角度' in result else 'aspect'] = {
            'habits': 'habits',
            'medical_history': 'medical_history',
            'medication': 'medical_history',
            'family_history': 'family_history',
            'other_symptoms': 'other_symptoms',
        }.get(dimension, 'habits' if dimension in intake_policy.LIFESTYLE_DIMENSIONS else 'symptoms')
        return result

    @field_validator('aspect', mode='before')
    @classmethod
    def normalize_aspect(cls, value):
        return (
            {label: key for key, label in ASPECT_LABELS.items()}.get(value, value) if isinstance(value, str) else value
        )

    @model_validator(mode='after')
    def validate_optional_background(self):
        if self.relevance == 'focused' and not self.reason:
            raise RecordValidationError('focused_gap_reason_required', '关联明确的候选必须说明已有资料或目标依据')
        if self.optionalBackground:
            if self.scope != 'general' or self.dimension not in intake_policy.BACKGROUND_DIMENSIONS:
                raise RecordValidationError(
                    'optional_background_scope', '独立背景只能使用 general 范围的背景分类，不能编造与困扰的关联'
                )
            if self.essential:
                raise RecordValidationError('optional_background_not_essential', '独立背景不能标记为关键缺口')
        return self


class ConcernAssessment(ModelObject):
    id: str | None = Field(default=None, validation_alias=AliasChoices('编号', 'id'))
    text: str = Field(validation_alias=AliasChoices('问题', 'text'), min_length=1, max_length=2000)
    evidence: str = Field(validation_alias=AliasChoices('用户原话', 'evidence'), min_length=1, max_length=4000)
    readyToAnswer: bool = Field(validation_alias=AliasChoices('可直接回应', 'readyToAnswer'))
    informationNeeded: str = Field(validation_alias=AliasChoices('待补充信息', 'informationNeeded'), max_length=1500)


class PatientAssessment(ModelObject):
    # Model-facing labels reduce protocol English in patient analysis; dumps keep
    # the internal vocabulary. Existing field names remain accepted for retries.
    intent: Literal['answer', 'additional', 'no_more', 'stop', 'unclear'] = Field(
        validation_alias=AliasChoices('意图', 'intent')
    )
    substantive: bool = Field(validation_alias=AliasChoices('包含实质信息', 'substantive'))
    intakeSufficient: bool = Field(validation_alias=AliasChoices('建档信息充分', 'intakeSufficient'))
    summary: str = Field(validation_alias=AliasChoices('患者情况理解', 'summary'), min_length=1, max_length=6000)
    concerns: list[ConcernAssessment] = Field(validation_alias=AliasChoices('疑问', 'concerns'))
    gaps: list[InformationGap] = Field(validation_alias=AliasChoices('信息缺口', 'gaps'))
    intake: IntakeUnderstanding = Field(validation_alias=AliasChoices('建档信息', 'intake'))

    @field_validator('intent', mode='before')
    @classmethod
    def normalize_intent(cls, value):
        return (
            {label: key for key, label in INTENT_LABELS.items()}.get(value, value) if isinstance(value, str) else value
        )


class GeneratedQuestion(ModelObject):
    question: str = Field(min_length=1, max_length=2000)
    questionType: Literal['text', 'radio', 'checkbox']
    options: dict[str, str] | list[str]

    @field_validator('options')
    @classmethod
    def normalize_options(cls, value):
        return [normalize_option(item) for item in value] if isinstance(value, list) else value

    @model_validator(mode='after')
    def validate_options(self):
        if self.questionType == 'text':
            if not isinstance(self.options, dict) or not self.options:
                raise RecordValidationError('text_input_required', '文本问题必须提供至少一个输入字段')
        elif not isinstance(self.options, list) or (
            len(self.options) < 2 and not (self.questionType == 'radio' and self.options == [NO_MORE_OPTION])
        ):
            raise RecordValidationError('choice_options_required', '选择题必须提供至少两个选项')
        elif any(not item for item in self.options) or len(set(self.options)) != len(self.options):
            raise RecordValidationError('invalid_choice_options', '选择题选项不能为空或重复')
        return self


class ConcernResponse(ModelObject):
    concernId: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=MAX_RESPONSE_CHARS)


def validate_new_question(question: GeneratedQuestion, *, action: str) -> None:
    """New questions always have shortcuts; old text questions remain readable."""
    if question.questionType not in ('radio', 'checkbox'):
        code = 'opening_choice_required' if action == 'open_intake' else 'question_choices_required'
        raise RecordValidationError(code, '每道新问题都必须使用单选或多选快捷选项，同时支持自定义回答')
    if len(question.options) < 2 and action not in ('invite_supplement', 'clarify_supplement'):
        raise RecordValidationError('ordinary_choice_options_required', '常规选择题必须提供至少两个选项')
    if action == 'open_intake' and len(question.options) > OPENING_MAX_OPTIONS:
        raise RecordValidationError(
            'opening_option_limit', f'首题选项不能超过 {OPENING_MAX_OPTIONS} 个，按相关内容提供，不必凑满'
        )
    if any(marker in option for option in question.options for marker in _REFUSAL_SHORTCUTS):
        raise RecordValidationError(
            'refusal_shortcut_forbidden', '快捷选项不能提供拒绝回答或跳过选项；保留用户自由输入'
        )
    validate_choice_labels(question.options)
    validate_answer_entry_instructions(question.question, options=question.options)


class MessageEvidence(IntakeEvidence):
    # Basic information is valid for acknowledgement, not an intake complaint or report fact.
    source: Literal['本次回答', '历史回答', '本次报告', '长期画像', '附带资料', '基本信息'] = Field(
        validation_alias=AliasChoices('来源', 'source')
    )


class PatientMessage(ModelObject):
    acknowledgement: str = Field(max_length=300)
    # Accepted for old model payloads, but never appended to a new public turn.
    rationale: str = Field(default='', max_length=200)
    evidence: list[MessageEvidence] = Field(max_length=8)
    focusRef: str | None = Field(default=None, min_length=1, max_length=160)


class TurnPresentation(ModelObject):
    assistantMessage: PatientMessage
    responses: list[ConcernResponse]
    question: GeneratedQuestion | None


def parse_model_object(content: str, schema: type[ModelObject], *, source_text: str = '') -> ModelObject:
    value = content.strip()
    if value.startswith('```'):
        lines = value.splitlines()
        if len(lines) < 3 or lines[-1].strip() != '```':
            raise RecordValidationError('unclosed_json_fence', '模型 JSON 围栏未闭合')
        value = '\n'.join(lines[1:-1])
    parsed = json.loads(value)
    check_generated_value(parsed, chinese_context=True, source_text=source_text)
    return schema.model_validate(parsed)


def new_flow(*, legacy: bool = False) -> dict:
    result = {
        'version': FLOW_VERSION,
        'phase': 'intake',
        'interactionCount': 0,
        'intakeCount': 0,
        'intakeQuestionCount': 0,
        'qaRoundCount': 0,
        'qaVagueCount': 0,
        'preQaAnswered': False,
        'supplementClarifications': 0,
        'concerns': [],
        'focus': None,
        'intake': intake_policy.new_intake(),
        'finalMessage': '',
        'finalReasoning': '',
        'endReason': None,
        'lastSubmission': None,
    }
    if not legacy:
        result['interviewPolicy'] = interview_policy.new_policy()
    return result


def answer_text(answer) -> str:
    return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, sort_keys=True)


def _is_no_more_option(answer) -> bool:
    if isinstance(answer, list) and len(answer) == 1:
        answer = answer[0]
    elif isinstance(answer, dict) and set(answer) == {'content'}:
        answer = answer['content']
    return isinstance(answer, str) and answer.strip() == NO_MORE_OPTION


def finish_without_more(session: dict, answer) -> dict | None:
    """The sole supplement shortcut is deterministic; mixed/custom answers aren't."""
    flow = session['qaFlow']
    if flow['phase'] not in ('supplement', 'qa') or not session.get('currentQuestion'):
        return None
    if any(item['status'] == 'pending' for item in flow['concerns']):
        return None
    if not _is_no_more_option(answer):
        return None
    draft = deepcopy(session)
    draft['qaFlow'].update(
        phase='finished', endReason='no_more_questions', finalMessage=COMPLETION_MESSAGE, finalReasoning=''
    )
    draft['currentQuestion'] = None
    return draft


def _answer_fragments(answer):
    """Read text values from plain or structured user answers."""
    if isinstance(answer, str):
        yield answer
    elif isinstance(answer, dict):
        for value in answer.values():
            yield from _answer_fragments(value)
    elif isinstance(answer, list):
        for value in answer:
            yield from _answer_fragments(value)


def _matching_concern(concern: ConcernAssessment, concerns: list[dict]) -> dict | None:
    """Resolve a missing model ID just as the merge policy does."""
    if concern.id:
        return next((item for item in concerns if item['id'] == concern.id), None)
    return next((item for item in concerns if item.get('text') == concern.text), None)


def validate_concern_source(concern: ConcernAssessment, session: dict, answer) -> None:
    """A previously answered question needs a fresh request, not old evidence."""
    known = _matching_concern(concern, session['qaFlow']['concerns'])
    if known is None or known.get('status', 'pending') == 'pending':
        return
    # Evidence is the user's actual wording. Ignore punctuation/spacing, but do
    # not use historical answers or AI explanations to establish a fresh ask.
    quote = ''.join(character.casefold() for character in concern.evidence if character.isalnum())
    if quote and any(
        quote in ''.join(character.casefold() for character in fragment if character.isalnum())
        for fragment in _answer_fragments(answer)
    ):
        return
    raise RecordValidationError(
        'repeated_concern_source_unavailable',
        '已答疑问只有用户本轮主动重问时才能再次列出；用户原话必须摘自本次回答，不能沿用历史依据',
    )


def validate_assessment(assessment: PatientAssessment, session: dict, answer, *, sources: dict | None = None) -> None:
    """Validate identities and available sources before accepting state changes."""
    known = {item['id']: item for item in session['qaFlow']['concerns']}
    seen = set()
    for concern in assessment.concerns:
        identity = concern.id or (concern.text, concern.evidence)
        if identity in seen:
            raise RecordValidationError('duplicate_concern', '同一疑问不能重复列出')
        seen.add(identity)
        if concern.id:
            if concern.id not in known:
                raise RecordValidationError('invalid_concern_identity', '不能为已有疑问编造 ID')
        elif not any(fragment.strip() for fragment in _answer_fragments(answer)):
            raise RecordValidationError('concern_source_unavailable', '新增疑问必须有本次用户实际回答的文本内容')
        validate_concern_source(concern, session, answer)
    required = {item['id'] for item in known.values() if item['status'] == 'pending'}
    if not required.issubset(seen):
        raise RecordValidationError('pending_concerns_missing', '必须评估所有尚未回应的疑问，不能遗漏')
    if answer is None and assessment.intent != 'answer':
        raise RecordValidationError('invalid_opening_intent', '首次接触不能替用户决定结束')
    intake_policy.validate_understanding(assessment.intake, session, answer, sources or {})
    if session['qaFlow']['phase'] == 'intake':
        if any(gap.dimension is None or not gap.reason for gap in assessment.gaps):
            raise RecordValidationError(
                'gap_purpose_required', '每个信息缺口必须提供固定内容分类和与用户情况的提问关联'
            )
        if any(gap.dimension in intake_policy.DETAIL_DIMENSIONS and gap.scope == 'general' for gap in assessment.gaps):
            raise RecordValidationError(
                'detail_gap_requires_primary_scope', '症状细节必须归属具体困扰，不能改成共同背景绕过已问内容'
            )
    for gap in assessment.gaps:
        intake_policy.validate_targets(assessment.intake, gap)


def _merge_concerns(flow: dict, assessment: PatientAssessment, question_id: str | None, *, answer=None) -> list[str]:
    touched = []
    for observation in assessment.concerns:
        validate_concern_source(observation, {'qaFlow': flow}, answer)
        item = _matching_concern(observation, flow['concerns'])
        if item is None:
            item = {
                'id': f'concern_{uuid4().hex}',
                'text': observation.text,
                'evidence': observation.evidence,
                'sourceQuestionId': question_id,
                'originPhase': flow['phase'],
                'handledCount': 0,
                'status': 'pending',
                'response': '',
            }
            flow['concerns'].append(item)
        item['readyToAnswer'] = observation.readyToAnswer
        item['informationNeeded'] = observation.informationNeeded
        touched.append(item['id'])
    return touched


def advance_flow(
    session: dict,
    assessment: PatientAssessment,
    *,
    answer=None,
    answer_already_recorded=False,
) -> tuple[dict, dict]:
    """Apply bounded interview/QA rules to an isolated draft; perform no I/O."""
    draft = deepcopy(session)
    flow = draft['qaFlow']
    interview_policy.ensure_counters(draft)
    limits = interview_policy.budget(flow)
    intake_policy.merge_understanding(flow, assessment.intake)
    question = draft.get('currentQuestion') or {}
    incoming_phase = flow['phase']
    no_more_option = incoming_phase in ('supplement', 'qa') and _is_no_more_option(answer)
    previous_ids = {item['id'] for item in flow['concerns']}
    touched = _merge_concerns(flow, assessment, question.get('questionId'), answer=answer)
    if answer is not None:
        # Echoing pending concerns for their final response does not turn the
        # supplement's explicit completion option into a substantive answer.
        control_only = no_more_option or (
            assessment.intent in ('stop', 'no_more', 'unclear') and not assessment.substantive and not touched
        )
        substantive = not control_only and (incoming_phase != 'supplement' or assessment.substantive or bool(touched))
        if substantive and not answer_already_recorded:
            flow['interactionCount'] += 1
            if incoming_phase == 'intake':
                flow['intakeCount'] += 1
        if incoming_phase == 'intake' and not answer_already_recorded:
            flow['intakeQuestionCount'] += 1
        if not answer_already_recorded and flow['interactionCount'] > limits['maxInteractions']:
            raise RecordValidationError('interaction_limit_exceeded', '会话交互次数已达到上限')

    pending = [item for item in flow['concerns'] if item['status'] == 'pending']
    repeated = [item for item in flow['concerns'] if item['id'] in touched and item['status'] != 'pending']
    decision = {
        'action': '',
        'summary': assessment.summary,
        'target': None,
        'responses': [],
        'endReason': None,
    }

    def choose(action, target=None):
        decision.update(action=action, target=target, **question_task(action, target))
        if action in ('invite_supplement', 'clarify_supplement', 'finish'):
            flow['focus'] = None

    def finish(reason):
        flow['phase'] = 'finished'
        flow['endReason'] = reason
        choose('finish')
        decision['endReason'] = reason
        # Every accepted concern receives a response, including unresolved ones.
        decision['responses'] = [_response_instruction(item, final=True) for item in pending + repeated]
        return draft, decision

    if assessment.intent == 'stop':
        return finish('user_stop')
    if flow['interactionCount'] >= limits['maxInteractions'] and (
        incoming_phase == 'intake' or not interview_policy.is_expanded(flow)
    ):
        return finish('interaction_limit')
    if no_more_option:
        return finish('no_more_questions')

    if incoming_phase == 'intake':
        selection = interview_policy.selection_context(session)
        count = flow['intakeQuestionCount']
        if count < 3:
            target = interview_policy.fixed_target(flow, count)
        else:
            eligible = interview_policy.lifestyle_targets(flow, assessment.intake, assessment.gaps, **selection)
            target = interview_policy.select_lifestyle_target(
                flow, candidates=eligible, sufficient=assessment.intakeSufficient, **selection
            )
        if target is not None:
            choose('open_intake' if answer is None else 'ask_intake', interview_policy.select_target(flow, target))
            return draft, decision
        # The pre-QA response addresses earlier requests once, before the user is
        # invited to ask more. It never consumes the five subsequent QA replies.
        flow['phase'] = 'supplement'
        flow['preQaAnswered'] = True
        flow['qaVagueCount'] = 0
        flow['supplementClarifications'] = 0
        choose('invite_supplement')
        decision['preQaResponse'] = True
        decision['responses'] = [_response_instruction(item, final=True) for item in pending + repeated]
        return draft, decision

    if flow['qaRoundCount'] >= MAX_QA_ROUNDS:
        return finish('qa_round_limit')
    has_actual_question = (
        assessment.substantive
        and bool(touched)
        and (
            assessment.intent == 'additional'
            or bool(repeated)
            or any(identity not in previous_ids for identity in touched)
        )
    )
    pending_target = next((item for item in pending if item['id'] == question.get('concernId')), None)
    answering_clarification = pending_target is not None and assessment.substantive and assessment.intent == 'answer'
    if assessment.intent == 'no_more' and not assessment.substantive and not has_actual_question:
        return finish('no_more_questions')
    if not has_actual_question and not answering_clarification:
        if answer is not None and not answer_already_recorded:
            flow['qaVagueCount'] += 1
        flow['supplementClarifications'] = flow['qaVagueCount']
        if flow['qaVagueCount'] >= MAX_VAGUE_ANSWERS:
            return finish('vague_question_limit')
        if flow['interactionCount'] >= limits['maxInteractions']:
            return finish('interaction_limit')
        choose('clarify_supplement')
        return draft, decision

    flow['qaVagueCount'] = 0
    flow['supplementClarifications'] = 0
    flow['phase'] = 'qa'
    if not answer_already_recorded:
        flow['qaRoundCount'] += 1
    if flow['qaRoundCount'] >= MAX_QA_ROUNDS:
        return finish('qa_round_limit')

    if flow['interactionCount'] >= limits['maxInteractions']:
        return finish('interaction_limit')
    decision['responses'] = [_response_instruction(item, final=True) for item in pending + repeated]

    choose('invite_supplement')
    return draft, decision


def _response_instruction(concern: dict, *, final: bool) -> dict:
    resolution = ('answered' if concern.get('readyToAnswer') else 'limited') if final else 'pending'
    instruction = {
        'concernId': concern['id'],
        'text': concern['text'],
        'informationNeeded': concern.get('informationNeeded', ''),
        'resolution': resolution,
    }
    previous = list(concern.get('responseHistory', []))
    if concern.get('status', 'pending') != 'pending' and concern.get('response'):
        previous.append(concern['response'])
    previous = [cleaned for text in previous if (cleaned := strip_internal_notices(text))]
    if previous:
        # Even a third request gets a fresh, brief explanation within the same
        # expression call. The previous wording is a reference, never a fact or
        # a ready-made response to publish again.
        instruction['previousResponse'] = previous[-1]
        instruction['previousResponses'] = list(dict.fromkeys(previous))
    return instruction


def remember_response(concern: dict, text: str) -> None:
    """Keep a bounded expression reference, never a patient-fact source."""
    previous = concern.get('response')
    if previous and previous != text and concern.get('status', 'pending') != 'pending':
        history = list(dict.fromkeys([*concern.get('responseHistory', []), previous]))
        concern['responseHistory'] = history[-MAX_QA_ROUNDS:]
    concern['response'] = text


def apply_presentation(draft: dict, decision: dict, presentation: TurnPresentation, reasoning: str) -> None:
    """Validate coverage and question shape before committing a generated turn."""
    required = {item['concernId']: item for item in decision['responses']}
    actual = [item.concernId for item in presentation.responses]
    if len(actual) != len(set(actual)) or set(actual) != set(required):
        raise RecordValidationError('response_coverage_mismatch', '答复必须逐项覆盖指定疑问，不能遗漏或额外编造疑问')
    if sum(len(item.text) for item in presentation.responses) > MAX_TURN_RESPONSE_CHARS:
        raise RecordValidationError('response_too_long', '本轮答疑总计不超过900字，请逐项简短回应并保留必要限制')
    closing = decision['action'] == 'finish'
    fixed_question = decision['action'] in ('invite_supplement', 'clarify_supplement', 'invite_background')
    if fixed_question:
        presentation.question = GeneratedQuestion(
            **(
                decision.get('fixedQuestion')
                or question_task(decision['action'], decision.get('target'))['fixedQuestion']
            )
        )
    if closing != (presentation.question is None):
        raise RecordValidationError('question_action_mismatch', '结束回复不得提问，继续问答必须提供一个待答问题')
    proposed_options = presentation.question.options if presentation.question is not None else None
    if not closing:
        validate_new_question(presentation.question, action=decision['action'])
        if decision['action'] in ('open_intake', 'ask_intake') and (
            choices := interview_policy.history_choice_fields(decision.get('target'))
        ):
            presentation.question = presentation.question.model_copy(update=choices)
            validate_new_question(presentation.question, action=decision['action'])

    options = choice_context_options(
        proposed_options, presentation.question.options if presentation.question is not None else None
    )
    validate_acknowledgement_style(
        presentation.assistantMessage, action=decision['action'], has_responses=bool(required)
    )
    validate_answer_entry_instructions(presentation.assistantMessage.acknowledgement, options=options)
    validate_answer_entry_instructions(presentation.assistantMessage.rationale, options=options)
    for response in presentation.responses:
        instruction = required[response.concernId]
        validate_response_style(response.text, previous_response=instruction.get('previousResponse', ''))
        for previous in instruction.get('previousResponses', []):
            validate_response_style(response.text, previous_response=previous)
        validate_answer_entry_instructions(response.text, options=options)
        if instruction.get('reuseResponse'):
            raise RecordValidationError(
                'qa_response_reuse_forbidden', '本轮答复需要简短补充或换一种解释，不能复用旧答原文'
            )
    validate_copy_components(presentation.assistantMessage, presentation.responses, action=decision['action'])

    flow = draft['qaFlow']
    acknowledgement = '' if required else acknowledgement_text(presentation.assistantMessage, action=decision['action'])
    parts = [acknowledgement] if acknowledgement and not closing else []
    known = {item['id']: item for item in flow['concerns']}
    for response in presentation.responses:
        item = known[response.concernId]
        instruction = required[item['id']]
        item['handledCount'] += 1
        remember_response(item, response.text)
        item['status'] = instruction['resolution']
        item['responseStyleVersion'] = RESPONSE_STYLE_VERSION
        parts.append(item['response'])
    message = '\n\n'.join(parts)
    if closing:
        # Final explanations follow the individual answers rather than announcing
        # completion before the user's concerns have been addressed.
        ending = (
            '如果后续有具体问题，可以继续向我咨询。本次建档先到这里。'
            if decision.get('endReason') == 'vague_question_limit'
            else COMPLETION_MESSAGE
        )
        flow['finalMessage'] = '\n\n'.join(parts + [ending])
        flow['finalReasoning'] = reasoning
        draft['currentQuestion'] = None
    else:
        draft['currentQuestion'] = {
            **presentation.question.model_dump(),
            'question': with_answer_hint(presentation.question.question),
            'questionId': f'q_{uuid4().hex}',
            'assistantMessage': message,
            'acknowledgementText': acknowledgement,
            'dialogueFocusRef': presentation.assistantMessage.focusRef if acknowledgement else None,
            'reasoning': reasoning,
            'phase': flow['phase'],
            'concernId': (decision.get('target') or {}).get('concernId'),
            'intakeDimension': (decision.get('target') or {}).get('dimension') if flow['phase'] == 'intake' else None,
            'intakeQuestionNumber': flow.get('intakeQuestionCount', 0) + 1 if flow['phase'] == 'intake' else None,
        }
