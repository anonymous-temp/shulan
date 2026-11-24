"""Frozen survey protocol for Health Analysis, independent of legacy sessions.

The v1 shape and per-survey limits come from Questionnaire's existing public
protocol. Every historical question remains asked even when it has no answer.
Only actual, nonempty answers become personal material; an explicit unknown is
still an actual answer. This decoder does not infer diagnoses or reconcile
contradictory statements across surveys.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass

SURVEY_PROTOCOL_VERSION = 'health-analysis-survey-protocol-v1'
_UNKNOWN_ANSWERS = frozenset({'不清楚', '不知道', '不确定', '记不清', 'unknown', 'unsure'})


class InvalidSurveyError(ValueError):
    """The external survey violates the frozen protocol, without exposing values."""


@dataclass(frozen=True, slots=True)
class DecodedSurveys:
    asked_questions: list[dict]
    personal_answers: list[dict]
    has_historical_schemas: bool
    has_personal_data: bool


def _standard_json(value, active: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidSurveyError('surveyList must contain only standard JSON values')
        return
    if not isinstance(value, (dict, list)):
        raise InvalidSurveyError('surveyList must contain only standard JSON values')
    if id(value) in active:
        raise InvalidSurveyError('surveyList must contain only standard JSON values')
    active.add(id(value))
    try:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise InvalidSurveyError('surveyList object keys must be strings')
            children = value.values()
        else:
            children = value
        for child in children:
            _standard_json(child, active)
    finally:
        active.remove(id(value))


def _keys(value, allowed: set[str], required: set[str], field: str) -> None:
    if not isinstance(value, dict):
        raise InvalidSurveyError(f'{field} must be an object')
    if set(value) - allowed or required - set(value):
        raise InvalidSurveyError(f'{field} has unsupported or missing fields')


def _required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSurveyError(f'{field} must be a non-empty string')
    if len(value) > 512:
        raise InvalidSurveyError(f'{field} is too long')
    return value


def _validate_schema(schema, field: str) -> None:
    _keys(schema, {'title', 'description', 'pages'}, {'title', 'pages'}, field)
    _required_text(schema['title'], f'{field}.title')
    if 'description' in schema and not isinstance(schema['description'], str):
        raise InvalidSurveyError(f'{field}.description must be a string')
    pages = schema['pages']
    if not isinstance(pages, list) or not pages or len(pages) > 100:
        raise InvalidSurveyError(f'{field}.pages must contain 1 to 100 pages')
    page_names, question_names = set(), set()
    for page_index, page in enumerate(pages):
        page_field = f'{field}.pages[{page_index}]'
        _keys(page, {'name', 'elements'}, {'name', 'elements'}, page_field)
        name = _required_text(page['name'], f'{page_field}.name')
        if name in page_names:
            raise InvalidSurveyError(f'{field} contains duplicate page names')
        page_names.add(name)
        elements = page['elements']
        if not isinstance(elements, list) or not elements:
            raise InvalidSurveyError(f'{page_field}.elements must be a non-empty array')
        for question_index, question in enumerate(elements):
            question_field = f'{page_field}.elements[{question_index}]'
            _keys(
                question,
                {
                    'name',
                    'type',
                    'title',
                    'description',
                    'isRequired',
                    'choices',
                    'showNoneItem',
                    'noneText',
                    'noneValue',
                },
                {'name', 'type', 'title'},
                question_field,
            )
            name = _required_text(question['name'], f'{question_field}.name')
            if name in question_names:
                raise InvalidSurveyError(f'{field} contains duplicate question names')
            question_names.add(name)
            if len(question_names) > 500:
                raise InvalidSurveyError(f'{field} contains more than 500 questions')
            _required_text(question['title'], f'{question_field}.title')
            kind = question['type']
            if kind not in ('text', 'radiogroup', 'checkbox'):
                raise InvalidSurveyError(f'{question_field}.type is unsupported')
            for key in ('description', 'noneText'):
                if key in question and not isinstance(question[key], str):
                    raise InvalidSurveyError(f'{question_field}.{key} must be a string')
            for key in ('isRequired', 'showNoneItem'):
                if key in question and not isinstance(question[key], bool):
                    raise InvalidSurveyError(f'{question_field}.{key} must be a boolean')
            if kind != 'checkbox' and any(key in question for key in ('showNoneItem', 'noneText', 'noneValue')):
                raise InvalidSurveyError(f'{question_field} none-item fields require checkbox')
            if 'noneValue' in question and question['noneValue'] != 'none':
                raise InvalidSurveyError(f'{question_field}.noneValue is unsupported')
            if kind == 'text':
                if 'choices' in question:
                    raise InvalidSurveyError(f'{question_field} text questions cannot contain choices')
                continue
            choices = question.get('choices')
            if not isinstance(choices, list) or not choices or len(choices) > 200:
                raise InvalidSurveyError(f'{question_field}.choices must contain 1 to 200 choices')
            values = set()
            for choice_index, choice in enumerate(choices):
                choice_field = f'{question_field}.choices[{choice_index}]'
                _keys(choice, {'value', 'text'}, {'value'}, choice_field)
                option = _required_text(choice['value'], f'{choice_field}.value')
                if option in values:
                    raise InvalidSurveyError(f'{question_field} contains duplicate choice values')
                if option == 'none' and question.get('showNoneItem'):
                    raise InvalidSurveyError(f'{question_field} none cannot also be an ordinary choice')
                values.add(option)
                if 'text' in choice and not isinstance(choice['text'], str):
                    raise InvalidSurveyError(f'{choice_field}.text must be a string')


def _answer(question: dict, answer: dict, *, field: str) -> dict:
    name = question['name']
    provided = name in answer
    raw = answer.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
        return {
            'provided': provided,
            'rawValue': [] if isinstance(raw, list) else raw,
            'value': None,
            'readableAnswer': None,
            'status': 'unknown',
        }
    kind = question['type']
    labels = {choice['value']: choice.get('text', choice['value']) for choice in question.get('choices', [])}
    if kind == 'text':
        if not isinstance(raw, str):
            raise InvalidSurveyError(f'{field} must be a string')
        if len(raw) > 20000:
            raise InvalidSurveyError(f'{field} is too long')
        value = raw.strip()
        units = [value]
    elif kind == 'radiogroup':
        if not isinstance(raw, str) or raw not in labels:
            raise InvalidSurveyError(f'{field} has an invalid choice')
        value = raw
        units = [labels[value]]
    else:
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise InvalidSurveyError(f'{field} must be an array of strings')
        # Copy only after validating the declared answer shape. Arbitrarily
        # nested JSON is not a legal answer and must not reach deepcopy.
        raw = list(raw)
        selected = set(raw)
        if 'none' in selected and question.get('showNoneItem'):
            if selected != {'none'}:
                raise InvalidSurveyError(f'{field} none is mutually exclusive')
            value = ['none']
            units = [question.get('noneText', '以上均无')]
        else:
            if selected - set(labels):
                raise InvalidSurveyError(f'{field} has an invalid choice')
            value = [option for option in labels if option in selected]
            units = [labels[option] for option in value]
    status = 'answered'
    if kind == 'checkbox' and value == ['none'] and question.get('showNoneItem'):
        status = 'denied'
    elif all(unit.strip().lower() in _UNKNOWN_ANSWERS for unit in units):
        status = 'unknown'
    return {
        'provided': provided,
        'rawValue': raw,
        'value': value,
        'readableAnswer': '\n'.join(units),
        'status': status,
    }


def decode_surveys(survey_list) -> DecodedSurveys:
    """Decode flat answer maps, retaining historical questions and source scope.

    Missing/null/blank/empty answers stay in ``asked_questions`` with no decoded
    value. They never enter ``personal_answers``. Explicit unknown answers do.
    The ``denied`` status only denotes the protocol's scoped none-item; this
    function does not infer denial or conflict from arbitrary medical wording.
    """
    try:
        _standard_json(survey_list, set())
    except RecursionError:
        raise InvalidSurveyError('surveyList must contain only standard JSON values') from None
    if not isinstance(survey_list, list):
        raise InvalidSurveyError('surveyList must be an array')
    asked, personal = [], []
    for survey_index, survey in enumerate(survey_list):
        field = f'surveyList[{survey_index}]'
        _keys(survey, {'schema', 'answer'}, {'schema', 'answer'}, field)
        schema, answer = survey['schema'], survey['answer']
        _validate_schema(schema, f'{field}.schema')
        if not isinstance(answer, dict):
            raise InvalidSurveyError(f'{field}.answer must be an object')
        names = {question['name'] for page in schema['pages'] for question in page['elements']}
        if set(answer) - names:
            raise InvalidSurveyError(f'{field}.answer contains an unknown question')
        for page_index, page in enumerate(schema['pages']):
            for question_index, question in enumerate(page['elements']):
                decoded = _answer(question, answer, field=f'{field}.answer at question {question_index}')
                row = {
                    'surveyIndex': survey_index,
                    'surveyTitle': schema['title'],
                    'surveyDescription': schema.get('description'),
                    'pageIndex': page_index,
                    'pageName': page['name'],
                    'questionIndex': question_index,
                    'name': question['name'],
                    'pointer': f'/surveyList/{survey_index}/schema/pages/{page_index}/elements/{question_index}',
                    'question': deepcopy(question),
                    **decoded,
                }
                asked.append(row)
                if decoded['value'] is not None:
                    personal.append(deepcopy(row))
    return DecodedSurveys(asked, personal, bool(survey_list), bool(personal))
