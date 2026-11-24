"""Decode native trigger questionnaires and retain legacy profile fallback."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping

from modules.report.profile_snapshot import BACKGROUND_ALLOW_LIST
from modules.report.profile_snapshot import FAMILY_HISTORY_ALLOW_LIST
from modules.report.profile_snapshot import profile_fact_text

_TRIGGER_SURVEYS = (
    ('initial', 'initSurveySchema', 'init_survey_schema', 'initSurveyAnswer', 'init_survey_answer'),
    ('advanced', 'advancedSurveySchema', 'advanced_survey_schema', 'advancedSurveyAnswer', 'advanced_survey_answer'),
)


def _input_value(value, camel, snake):
    if isinstance(value, Mapping):
        return value[camel] if camel in value else value.get(snake)
    if hasattr(value, snake):
        return getattr(value, snake)
    return (getattr(value, 'model_extra', None) or {}).get(camel)


def _trigger_surveys(request):
    """The declared list replaces stage inputs without flattening its order."""
    surveys = _input_value(request, 'surveyList', 'survey_list')
    if surveys is not None:
        if not isinstance(surveys, list):
            _survey_error('surveyList', 'must be an array of questionnaires')
        for index, survey in enumerate(surveys):
            path = f'surveyList[{index}]'
            if not isinstance(survey, Mapping) and not hasattr(survey, 'survey_schema'):
                _survey_error(path, 'must be a questionnaire entry')
            schema = _input_value(survey, 'schema', 'survey_schema')
            if schema is None:
                _survey_error(path + '.schema', 'must be a questionnaire object')
            reported_at = next((
                _input_value(survey, camel, snake)
                for camel, snake in (
                    ('submittedAt', 'submitted_at'), ('submitTime', 'submit_time'),
                    ('submissionTime', 'submission_time'),
                )
                if isinstance(_input_value(survey, camel, snake), str)
                and _input_value(survey, camel, snake).strip()
            ), None)
            yield (path, path + '.schema', schema, path + '.answer',
                _input_value(survey, 'answer', 'answer'), reported_at)
        return
    for stage, schema_alias, schema_key, answer_alias, answer_key in _TRIGGER_SURVEYS:
        yield (stage, schema_alias, _input_value(request, schema_alias, schema_key),
            answer_alias, _input_value(request, answer_alias, answer_key), None)


def fmt_typing_report_source(request):
    """Resolve the top-level form first while retaining the legacy source."""
    url = _input_value(request, 'fmtTypingReportUrl', 'fmt_typing_report_url')
    if isinstance(url, str) and url.strip():
        return url, 'fmtTypingReportUrl'
    spec = _input_value(request, 'microbiomeReportData', 'microbiome_report_data')
    url = _input_value(spec, 'fmtTypingReportUrl', 'fmt_typing_report_url') if spec is not None else None
    return (url, 'microbiomeReportData.fmtTypingReportUrl')


def followup_request_context(request):
    """Opaque routing/source inputs for the post-evaluate generation method."""
    from copy import deepcopy

    url, _ = fmt_typing_report_source(request)
    context = {'groupId': request.group_id, 'fmtTypingReportUrl': url,
        'package': deepcopy(request.package)}
    if request.microbiome_report_data is not None:
        context['microbiomeReportData'] = request.microbiome_report_data.model_dump(
            mode='json', by_alias=True, exclude_none=False)
    return context


def evaluation_callback_request_context(request):
    """Keep the confirmed downstream routing fields on the public envelope.

    Source questionnaires and profile snapshots belong to the existing
    generation DTO, not to a duplicated public report-data inventory.
    """
    context = followup_request_context(request)
    return {key: context[key] for key in ('groupId', 'fmtTypingReportUrl', 'package')}


def _survey_error(path, detail):
    raise ValueError(f'{path}: {detail}')


def _nonblank_string(value, path):
    if not isinstance(value, str) or not value.strip():
        _survey_error(path, 'must be a nonempty string')
    return value


def _optional_string(value, path):
    if value is not None and not isinstance(value, str):
        _survey_error(path, 'must be a string')


def _survey_questions(schema, path):
    if not isinstance(schema, dict):
        _survey_error(path, 'must be a questionnaire object')
    _nonblank_string(schema.get('title'), path + '.title')
    _optional_string(schema.get('description'), path + '.description')
    pages = schema.get('pages')
    if not isinstance(pages, list) or not pages:
        _survey_error(path + '.pages', 'must contain at least one page')
    questions, page_names = {}, set()
    for page_index, page in enumerate(pages):
        page_path = f'{path}.pages[{page_index}]'
        if not isinstance(page, dict):
            _survey_error(page_path, 'must be a page object')
        page_name = _nonblank_string(page.get('name'), page_path + '.name')
        if page_name in page_names:
            _survey_error(page_path + '.name', 'duplicate page name')
        page_names.add(page_name)
        elements = page.get('elements')
        if not isinstance(elements, list) or not elements:
            _survey_error(page_path + '.elements', 'must contain at least one question')
        for index, question in enumerate(elements):
            question_path = f'{page_path}.elements[{index}]'
            if not isinstance(question, dict):
                _survey_error(question_path, 'must be a question object')
            name = _nonblank_string(question.get('name'), question_path + '.name')
            if name in questions:
                _survey_error(question_path + '.name', 'question name must be unique across the questionnaire')
            _nonblank_string(question.get('title'), question_path + '.title')
            _optional_string(question.get('description'), question_path + '.description')
            if question.get('type') not in {'text', 'radiogroup', 'checkbox'}:
                _survey_error(question_path + '.type', 'only text, radiogroup and checkbox are supported')
            if 'isRequired' in question and type(question['isRequired']) is not bool:
                _survey_error(question_path + '.isRequired', 'must be a boolean')
            choices = {}
            if question['type'] in {'radiogroup', 'checkbox'}:
                declared = question.get('choices')
                if not isinstance(declared, list) or not declared:
                    _survey_error(question_path + '.choices', 'must contain at least one option object')
                for choice_index, choice in enumerate(declared):
                    choice_path = f'{question_path}.choices[{choice_index}]'
                    if not isinstance(choice, dict):
                        _survey_error(choice_path, 'must be an option object')
                    value = _nonblank_string(choice.get('value'), choice_path + '.value')
                    if value in choices:
                        _survey_error(choice_path + '.value', 'duplicate option value')
                    _optional_string(choice.get('text'), choice_path + '.text')
                    label = choice.get('text')
                    choices[value] = label if isinstance(label, str) and label.strip() else value
            if question['type'] == 'checkbox':
                if 'showNoneItem' in question and type(question['showNoneItem']) is not bool:
                    _survey_error(question_path + '.showNoneItem', 'must be a boolean')
                _optional_string(question.get('noneText'), question_path + '.noneText')
                if 'noneValue' in question and question['noneValue'] != 'none':
                    _survey_error(question_path + '.noneValue', 'must be the fixed value "none"')
                if question.get('showNoneItem', False) and 'none' in choices:
                    _survey_error(question_path + '.choices', 'the none option is reserved when showNoneItem is enabled')
            questions[name] = (question, choices)
    return questions


def _survey_answer_object(value, questions, path):
    if value is None or value == '':
        return {}
    if not isinstance(value, dict):
        _survey_error(path, 'must be an answer object')
    # All three supported answer types are scalar strings or arrays. An
    # object under the sole "answers" key is therefore an envelope even
    # when the questionnaire also has a question literally named answers.
    if set(value) == {'answers'} and isinstance(value['answers'], dict):
        value = value['answers']
    unknown = [name for name in value if name not in questions]
    if unknown:
        _survey_error(path, 'unknown question name: ' + str(unknown[0]))
    return value


def _survey_answer(question, choices, value, path):
    kind = question['type']
    missing = value is None or isinstance(value, str) and not value.strip()
    if kind == 'checkbox' and isinstance(value, list) and not value:
        missing = True
    if missing:
        if question.get('isRequired', False):
            _survey_error(path, 'required question has no answer')
        return None
    if kind == 'text':
        if not isinstance(value, str):
            _survey_error(path, 'text answers must be strings')
        return value.strip()
    if kind == 'radiogroup':
        if not isinstance(value, str) or value not in choices:
            _survey_error(path, 'single-choice answer must be a declared option value')
        return choices[value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _survey_error(path, 'multiple-choice answers must be an array of strings')
    selected = set(value)
    if 'none' in selected and selected != {'none'}:
        _survey_error(path, 'none is mutually exclusive with ordinary choices')
    if question.get('showNoneItem', False) and 'none' in selected:
        return question.get('noneText') or '以上均无'
    unknown = selected.difference(choices)
    if unknown:
        _survey_error(path, 'multiple-choice answer contains an undeclared option value')
    return '、'.join(text for value, text in choices.items() if value in selected)


def trigger_survey_answers(request):
    """Validate native questionnaires and project only answered, labelled QAs.

    Raw schemas/answers remain in the request. The projection carries source
    identity independently per stage; it never includes question descriptions,
    unanswered questions, option dictionaries or guessed clinical meanings.
    """
    output = []
    for stage, schema_alias, schema, answer_alias, answers, reported_at in _trigger_surveys(request):
        if schema is None or schema == '':
            if answers is not None and answers != '' and answers != {}:
                _survey_error(schema_alias, 'schema is required when answers are provided')
            continue
        questions = _survey_questions(schema, schema_alias)
        wrapped = (isinstance(answers, dict) and set(answers) == {'answers'}
            and isinstance(answers['answers'], dict))
        supplied = _survey_answer_object(answers, questions, answer_alias)
        for name, (question, choices) in questions.items():
            origin = f'request.{answer_alias}' + ('.answers' if wrapped else '')
            path = f'{origin}[{json.dumps(name, ensure_ascii=False)}]'
            answer = _survey_answer(question, choices, supplied.get(name), path)
            if answer is None:
                continue
            row = dict(questionId=stage + ':' + name, question=question['title'],
                answer=answer, stage=stage, sourcePath=path, sourceId=path)
            # Preserve collection metadata through the native-to-source
            # adapter. It dates the answer, not the retrospective event in
            # that answer; no parsing, timezone inference or age filtering.
            if reported_at is not None:
                row['submittedAt'] = reported_at
            output.append(row)
    return output


def validate_trigger_surveys(payload):
    """Admission-time validation for a raw alias/snake-case request mapping."""
    trigger_survey_answers(payload)


def _identity_key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value if value is not None else ''))).casefold()


def _identity_question(name, question, *, family_context=False):
    """Only explicit patient identity labels, never nickname/history guesses."""
    title = _identity_key(question['title']).rstrip(':?')
    name_key = _identity_key(name)
    qualifier = _identity_key(question.get('description'))
    if (question['type'] == 'checkbox' or family_context
            or re.search(r'父|母|家族|家属|血亲|亲属|监护|医生|昵称|网名|账号|账户|联系人|'
                r'father|mother|family|relative|guardian|parent|doctor|physician|nick[_-]?name|account',
                name_key + ' ' + title + ' ' + qualifier, re.I)):
        return None
    exact = {
        '姓名': 'name', '受检者姓名': 'name',
        '年龄': 'age', '性别': 'sex',
    }
    if title in exact:
        return exact[title]
    canonical = {'name': 'name', 'age': 'age', 'gender': 'sex', 'sex': 'sex'}
    field = canonical.get(name_key)
    patterns = {
        'name': r'(?:(?:请填写|请输入|请提供)?(?:您的|本人|受检者)?)姓名|(?:patient)?name',
        'age': r'(?:(?:请填写|请输入|请提供)?(?:您的|本人|受检者)?)年龄(?:\((?:周)?岁\))?|age(?:\(years?\))?',
        'sex': r'(?:(?:请选择|请填写|请提供)?(?:您的|本人|受检者)?)(?:生理)?性别|gender|sex',
    }
    return field if field and re.fullmatch(patterns[field], title, re.I) else None


def _identity_value(field, answer):
    if field == 'age':
        match = re.fullmatch(r'(\d{1,3})(?:\.0+)?\s*(?:周岁|岁|years?|yrs?)?',
            unicodedata.normalize('NFKC', answer).strip(), re.I)
        if match is not None and 0 <= int(match[1]) <= 150:
            return int(match[1])
        return None
    if field == 'sex':
        return {'男': '男', '男性': '男', '男生': '男', 'male': '男', 'm': '男',
            '女': '女', '女性': '女', '女生': '女', 'female': '女', 'f': '女'}.get(_identity_key(answer))
    name = ' '.join(answer.split())
    if _identity_key(name) in {'', '未知', '不详', '无', '不便透露', '不清楚', '未提供',
            'unknown', 'undisclosed', 'anonymous', 'none', 'n/a'}:
        return None
    return name


def trigger_survey_subject(payload):
    """Recover explicit questionnaire identity without mixing known people.

    A missing/unparseable age stays unknown; the full answer still remains in
    the questionnaire. Independent stage/top-level conflicts fail admission
    rather than arbitrarily choosing one person's name, sex or age.
    """
    result, origins = {}, {}
    for _, schema_alias, schema, answer_alias, answers, _ in _trigger_surveys(payload):
        if schema is None or schema == '':
            continue
        questions = _survey_questions(schema, schema_alias)
        supplied = _survey_answer_object(answers, questions, answer_alias)
        family_questions = set()
        family_schema = bool(re.fullmatch(
            r'(?:家族史|家属|血亲|亲属|familyhistory|relative)(?:问卷|调查|调查表|信息|信息表|survey|questionnaire)?',
            _identity_key(schema['title'])))
        for page in schema['pages']:
            scope = ' '.join(str(page.get(key) or '') for key in ('name', 'title', 'description'))
            if family_schema or re.search(r'家族|家属|血亲|亲属|监护|family|relative|guardian', scope, re.I):
                family_questions.update(question['name'] for question in page['elements'])
        for name, (question, choices) in questions.items():
            field = _identity_question(name, question, family_context=name in family_questions)
            if field is None:
                continue
            path = f'{answer_alias}[{json.dumps(name, ensure_ascii=False)}]'
            answer = _survey_answer(question, choices, supplied.get(name), path)
            if answer is None:
                continue
            value = _identity_value(field, answer)
            if value is None:
                continue
            if field in result and _identity_key(result[field]) != _identity_key(value):
                _survey_error(path, f'explicit {field} conflicts with {origins[field]}')
            result.setdefault(field, value)
            origins.setdefault(field, path)
    for field, value in result.items():
        aliases = (('sex', 'sex'), ('gender', 'gender')) if field == 'sex' else ((field, field),)
        for camel, snake in aliases:
            supplied = _input_value(payload, camel, snake)
            if supplied is None or isinstance(supplied, str) and not supplied.strip():
                continue
            known = _identity_value(field, str(supplied))
            if known is not None and _identity_key(known) != _identity_key(value):
                _survey_error(origins[field], f'explicit {field} conflicts with top-level {camel}')
    return result


def _has_usable_answers(value):
    """An empty answer envelope is absent; a literal zero/false is supplied."""
    if value is None or value == '' or value == [] or value == {}:
        return False
    if isinstance(value, list):
        return any(_has_usable_answers(item) for item in value)
    if isinstance(value, dict):
        for key in ('answer', 'answerValue', 'value', 'result', 'resultValue', 'score'):
            if key in value:
                return _has_usable_answers(value[key])
        containers = ('answers', 'answer', 'qaHistory', 'questions', 'items')
        if any(key in value for key in containers):
            return any(_has_usable_answers(value[key]) for key in containers if key in value)
        metadata = {
            'userId',
            'sessionId',
            'name',
            'age',
            'sex',
            'gender',
            'submittedAt',
            'submitTime',
            'submissionTime',
            'version',
            'questionCount',
            'answeredCount',
            'questionId',
            'question',
            'questionText',
            'title',
            'sourcePath',
            'isSimulated',
        }
        return any(_has_usable_answers(item) for key, item in value.items() if key not in metadata)
    return True


def _snapshot_answers(snapshot, user_id):
    """Reuse the per-run service snapshot as self-report, never as exam results."""
    if snapshot is None or snapshot.user_id != user_id or snapshot.stale:
        return []
    answers = []
    for fact in snapshot.facts:
        if (
            fact.subject_kind not in {'self', 'blood_relative'}
            or fact.status not in {'active', 'pending'}
            or fact.assertion_level not in {'observed', 'clinician_recorded', 'user_confirmed', 'user_reported'}
            or 'health_report' in fact.sources
            or fact.fact_type not in BACKGROUND_ALLOW_LIST | FAMILY_HISTORY_ALLOW_LIST
            or fact.value in (None, '', [], {})
        ):
            continue
        family = fact.subject_kind == 'blood_relative' or fact.fact_type in FAMILY_HISTORY_ALLOW_LIST
        label = ('家族史：' if family else '') + (fact.display_name or fact.fact_type)
        category = (
            'familyHistory'
            if family
            else {
                'condition.diagnosis': 'medicalHistory',
                'condition.history_status': 'medicalHistory',
                'symptom.reported': 'chiefComplaint',
                'goal.health': 'healthGoals',
                'allergy.substance': 'allergies',
                'medication.current': 'medications',
            }.get(fact.fact_type)
        )
        item = dict(
            questionId=fact.fact_id,
            question=label,
            answer=profile_fact_text(fact.fact_type, label, fact.value),
            submittedAt=fact.effective_start or fact.updated_at,
            profileStatus=fact.status,
            assertionLevel=fact.assertion_level,
            sourcePath=f'healthProfile.facts[{fact.fact_id}]',
        )
        if category:
            item['category'] = category
        answers.append(item)
    return answers


def prepare_evaluation_request(request, profile_snapshot=None):
    """Prefer native trigger stages, otherwise keep existing answer fallback.

    An explicitly declared native questionnaire does not revive superseded
    legacy answers when unanswered. Without native stages, explicit legacy
    questionnaires retain priority over the per-run HealthProfileService
    snapshot. Former simulated userProfile extras are not a profile source.
    Profile statements remain reported background, never hospital findings.
    """
    native_answers = trigger_survey_answers(request)
    native_declared = (_input_value(request, 'surveyList', 'survey_list') is not None
        or any(_input_value(request, alias, key) not in (None, '')
            for _, alias, key, _, _ in _TRIGGER_SURVEYS))
    if native_declared:
        copied = request.model_copy(deep=True)
        copied.__pydantic_extra__['healthSurvey'] = dict(answers=native_answers)
        # These alternative legacy channels must not reintroduce superseded
        # answers. Only the working projection changes; raw admission input
        # and the native schemas/answers stay intact.
        for key in ('questionnaire', 'survey'):
            if key in copied.__pydantic_extra__:
                copied.__pydantic_extra__[key] = None
        return copied
    extra = request.model_extra or {}
    if any(_has_usable_answers(extra.get(key)) for key in ('healthSurvey', 'questionnaire', 'survey')):
        return request
    answers = _snapshot_answers(profile_snapshot, request.user_id)
    if not answers:
        return request
    copied = request.model_copy(deep=True)
    # Metadata stays on the original profile and each answer. Putting scalar
    # metadata next to answers would turn it into a spurious questionnaire row.
    copied.__pydantic_extra__['healthSurvey'] = dict(answers=answers)
    return copied


def inline_microbiome_data(request):
    spec = request.microbiome_report_data
    return (spec.model_extra or {}).get('dataInfo') if spec is not None else None
