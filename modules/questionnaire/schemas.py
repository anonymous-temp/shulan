"""Strict public survey schema and lossless answer normalization.

The product deliberately accepts unanswered required questions as unknown. The
display-only isRequired flag does not manufacture a user's health statement.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from urllib.parse import urlsplit


class QuestionnaireValidationError(ValueError):
    pass


class QuestionnaireConflictError(ValueError):
    pass


class QuestionnaireNotFoundError(LookupError):
    pass


class QuestionnaireExpiredError(QuestionnaireConflictError):
    pass


def required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionnaireValidationError(f'{field} must be a non-empty string')
    if len(value) > 512:
        raise QuestionnaireValidationError(f'{field} is too long')
    return value


def _keys(value, allowed, required, field):
    if not isinstance(value, dict):
        raise QuestionnaireValidationError(f'{field} must be an object')
    if set(value) - set(allowed) or set(required) - set(value):
        raise QuestionnaireValidationError(f'{field} has unsupported or missing fields')


def validate_questionnaire(value: dict) -> dict:
    _keys(value, {'title', 'description', 'pages'}, {'title', 'pages'}, 'questionnaire')
    required_text(value['title'], 'questionnaire.title')
    if 'description' in value and not isinstance(value['description'], str):
        raise QuestionnaireValidationError('questionnaire.description must be a string')
    pages = value['pages']
    if not isinstance(pages, list) or not pages or len(pages) > 100:
        raise QuestionnaireValidationError('questionnaire.pages must be a non-empty array')
    page_names, question_names = set(), set()
    for page in pages:
        _keys(page, {'name', 'elements'}, {'name', 'elements'}, 'page')
        name = required_text(page['name'], 'page.name')
        if name in page_names:
            raise QuestionnaireValidationError('duplicate page name')
        page_names.add(name)
        if not isinstance(page['elements'], list) or not page['elements']:
            raise QuestionnaireValidationError('page.elements must be a non-empty array')
        for question in page['elements']:
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
                'question',
            )
            name = required_text(question['name'], 'question.name')
            if name in question_names:
                raise QuestionnaireValidationError('duplicate question name')
            question_names.add(name)
            if len(question_names) > 500:
                raise QuestionnaireValidationError('too many questions')
            required_text(question['title'], 'question.title')
            kind = question['type']
            if kind not in ('text', 'radiogroup', 'checkbox'):
                raise QuestionnaireValidationError('unsupported question type')
            for field in ('description', 'noneText'):
                if field in question and not isinstance(question[field], str):
                    raise QuestionnaireValidationError(f'{field} must be a string')
            for field in ('isRequired', 'showNoneItem'):
                if field in question and not isinstance(question[field], bool):
                    raise QuestionnaireValidationError(f'{field} must be a boolean')
            if kind != 'checkbox' and any(k in question for k in ('showNoneItem', 'noneText', 'noneValue')):
                raise QuestionnaireValidationError('none-item fields require checkbox')
            if 'noneValue' in question and question['noneValue'] != 'none':
                raise QuestionnaireValidationError('noneValue must be none')
            if kind == 'text':
                if 'choices' in question:
                    raise QuestionnaireValidationError('text questions cannot contain choices')
                continue
            choices = question.get('choices')
            if not isinstance(choices, list) or not choices or len(choices) > 200:
                raise QuestionnaireValidationError('choices must be a non-empty array')
            values = set()
            for choice in choices:
                _keys(choice, {'value', 'text'}, {'value'}, 'choice')
                option = required_text(choice['value'], 'choice.value')
                if option in values:
                    raise QuestionnaireValidationError('duplicate choice value')
                if option == 'none' and question.get('showNoneItem'):
                    raise QuestionnaireValidationError('none cannot also be an ordinary choice')
                values.add(option)
                if 'text' in choice and not isinstance(choice['text'], str):
                    raise QuestionnaireValidationError('choice.text must be a string')
    # Validate standard JSON, without coercing or silently rewriting model output.
    stable_hash(value)
    return deepcopy(value)


def normalize_answers(questionnaire: dict, answers: dict) -> tuple[dict, dict]:
    """Return source rows and the schema-compatible canonical answer map."""
    if not isinstance(answers, dict):
        raise QuestionnaireValidationError('answers must be an object')
    stable_hash(answers)
    questions = {q['name']: q for page in questionnaire['pages'] for q in page['elements']}
    if set(answers) - set(questions):
        raise QuestionnaireValidationError('answers contains an unknown question')
    rows, canonical = {}, {}
    for name, question in questions.items():
        provided = name in answers
        raw = deepcopy(answers.get(name))
        empty = raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []
        # bool/number are never treated as empty; the declared question type
        # below rejects them, including false and zero.
        if empty:
            rows[name] = {
                'provided': provided,
                'rawValue': raw,
                'status': 'unknown',
                'value': None,
                'question': deepcopy(question),
            }
            continue
        kind = question['type']
        if kind == 'text':
            if not isinstance(raw, str):
                raise QuestionnaireValidationError(f'{name} must be a string')
            if len(raw) > 20000:
                raise QuestionnaireValidationError(f'{name} is too long')
            value = raw.strip()
        elif kind == 'radiogroup':
            allowed = {choice['value'] for choice in question['choices']}
            if not isinstance(raw, str) or raw not in allowed:
                raise QuestionnaireValidationError(f'{name} has an invalid choice')
            value = raw
        else:
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise QuestionnaireValidationError(f'{name} must be an array of strings')
            allowed = [choice['value'] for choice in question['choices']]
            selected = set(raw)
            if 'none' in selected and question.get('showNoneItem'):
                if selected != {'none'}:
                    raise QuestionnaireValidationError(f'{name}: none is mutually exclusive')
                value = ['none']
            else:
                if selected - set(allowed):
                    raise QuestionnaireValidationError(f'{name} has an invalid choice')
                value = [option for option in allowed if option in selected]
        rows[name] = {
            'provided': provided,
            'rawValue': raw,
            'status': 'answered',
            'value': value,
            'question': deepcopy(question),
        }
        canonical[name] = deepcopy(value)
    return rows, canonical


def stable_hash(value) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise QuestionnaireValidationError('value must be standard JSON') from error
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def normalize_file_list(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 5:
        raise QuestionnaireValidationError('fileList must be an array with at most five files')
    result = []
    for item in value:
        _keys(item, {'documentId', 'url', 'fileType'}, set(), 'file')
        if bool(item.get('documentId')) == bool(item.get('url')):
            raise QuestionnaireValidationError('each file requires exactly one documentId or url')
        if 'documentId' in item:
            required_text(item['documentId'], 'documentId')
            if 'fileType' in item:
                raise QuestionnaireValidationError('documentId cannot be combined with fileType')
        if 'url' in item:
            url = item['url']
            if not isinstance(url, str) or len(url) > 8192:
                raise QuestionnaireValidationError('url must be a string')
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise QuestionnaireValidationError('url must be HTTP or HTTPS')
        if 'fileType' in item and item['fileType'] not in ('pdf', 'image'):
            raise QuestionnaireValidationError('fileType must be pdf or image')
        result.append(deepcopy(item))
    return result
