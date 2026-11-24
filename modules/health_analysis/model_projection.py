"""Name-free analysis view, retaining an internal reversible trace of projection."""

from copy import deepcopy
from unicodedata import combining
from unicodedata import normalize

from modules.health_analysis.contracts import AnalysisGenerationError


def _comparison_text(value):
    """Return compatibility-folded text and its original character offsets."""
    if value.isascii():
        return value.lower(), range(len(value))
    characters = []
    marks = []
    for index, original in enumerate(value):
        for char in normalize('NFKD', normalize('NFKD', original).casefold()):
            if combining(char):
                marks.append((char, index))
            else:
                characters.extend(sorted(marks, key=lambda mark: combining(mark[0])))
                marks.clear()
                characters.append((char, index))
    characters.extend(sorted(marks, key=lambda mark: combining(mark[0])))
    return ''.join(char for char, _ in characters), [index for _, index in characters]


def _redact_text(item, names, marker):
    if not names:
        return item
    folded, offsets = _comparison_text(item)
    intervals = []
    for name in names:
        start = folded.find(name)
        while start != -1:
            matched = offsets[start : start + len(name)]
            intervals.append((min(matched), max(matched) + 1))
            start = folded.find(name, start + 1)
    if not intervals:
        return item

    chunks = []
    intervals.sort()
    start, end = intervals[0]
    previous_end = 0
    for next_start, next_end in intervals[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        chunks.extend((item[previous_end:start], marker))
        previous_end, start, end = end, next_start, next_end
    chunks.extend((item[previous_end:start], marker, item[end:]))
    return ''.join(chunks)


_GENDERS = ('male', 'female')
_MATERIAL_STATUSES = ('answered', 'denied', 'unknown')
_QUESTION_TYPES = ('text', 'radiogroup', 'checkbox')
_MATERIALS = ('materials', 'personal_materials')
_QUESTION_ROWS = (
    'askedQuestions',
    'asked_questions',
    'questions',
    'retainedQuestions',
    'catalogue',
    'targets',
    'backgroundTargets',
)
_CONDITION_ROWS = (
    ('candidate', 'conditions', None),
    ('factors', 'candidates', None, 'conditions', None),
    ('factors', 'candidates', None, 'assessment', 'conditions', None),
)
_CANDIDATE_ROWS = ('feedback', 'questions', 'retainedQuestions', 'interviews', 'tests')


def _controlled_text(path, value):
    # Only protect canonical protocol locations, never an identically named
    # field in arbitrary report content or source metadata.
    if path and path[0] == 'snapshot':
        path = path[1:]
    parent, key = path[:-1], path[-1]
    if parent in (('effective_demographics',), ('effectiveDemographics',)):
        return key == 'gender' and value in _GENDERS or key == 'sourceReliability' and value == 'very_high'
    if parent in (('effective_demographics', 'fieldPointers'), ('effectiveDemographics', 'fieldPointers')):
        return (key, value) in (('ageYears', '/age'), ('gender', '/gender'))
    if not parent:
        return key in ('profile_gender', 'profileGender') and value in _GENDERS

    if parent in (('survey', 'requiredBackgroundTargets'), ('survey', 'questionIds')):
        return key is None
    if parent == ('survey',) and key in ('policyVersion', 'protocolFeedback'):
        return True
    if parent in (('survey', 'rules', None), ('survey', 'previousFindings', None)):
        if key in ('ruleId', 'scope', 'status'):
            return True
    if parent in (
        ('survey', 'previousFindings', None, 'questionIds'),
        ('survey', 'rules', None, 'targetIds'),
        ('surveyRules', None, 'targetIds'),
    ):
        return key is None
    if parent == ('survey', 'previousQuestionFindings', None):
        return key == 'candidateId'
    if parent in (('survey', 'previousQuestionFindings', None, 'issues', None), ('feedback', None, 'issues', None)):
        return key in ('ruleId', 'field')
    if len(parent) == 2 and parent[1] is None:
        if parent[0] in ('surveyRules', 'surveyFindings') and key in ('ruleId', 'scope', 'status'):
            return True
        group = parent[0]
        if group in _MATERIALS:
            return (
                key == 'id'
                or key == 'kind'
                and value in ('report', 'chat', 'answer')
                or key == 'status'
                and value in _MATERIAL_STATUSES
                or key == 'backgroundScope'
                and value == 'family_history'
            )
        if group in _CANDIDATE_ROWS and key in ('candidateId', 'targetId'):
            return True
        if group in ('askedQuestions', 'asked_questions') and key == 'pointer':
            return True
        if group in ('sources', 'catalogue', 'targets', 'backgroundTargets') and key == 'id':
            return True

    if len(parent) == 3 and parent[1:] == (None, 'question') and parent[0] in _QUESTION_ROWS:
        return key == 'type' and value in _QUESTION_TYPES
    if len(parent) == 3 and parent[0] in _MATERIALS and parent[1:] == (None, 'content'):
        return key == 'status' and value in _MATERIAL_STATUSES
    if len(parent) == 4 and parent[0] in _MATERIALS and parent[1:] == (None, 'content', 'question'):
        return key == 'type' and value in _QUESTION_TYPES
    if parent in _CONDITION_ROWS:
        return (
            key == 'id'
            or key == 'kind'
            and value in ('presence', 'temporality', 'applicability')
            or key == 'status'
            and value in ('met', 'not_met', 'unknown', 'conflicting')
        )
    if parent in (('target',), ('factors', 'targets', None)):
        return key == 'id' or key == 'kind' and value in ('indicator', 'symptom', 'disease', 'health_goal')
    if parent in (('candidate',), ('factors', 'candidates', None)):
        return key in ('id', 'targetId')
    if parent in (('sources', None, 'passages', None), ('factors', 'sources', None, 'passages', None)):
        return key == 'id' or key == 'kind' and value in ('abstract', 'fullText', 'guideBlock')
    if parent in (('candidate', 'knowledgeEvidence', None), ('factors', 'candidates', None, 'knowledgeEvidence', None)):
        return key in ('sourceId', 'passageId')
    if parent == ('factors', 'sources', None):
        return key == 'id'
    return False


def project_names(value, names):
    names = {_comparison_text(name)[0] for name in names if isinstance(name, str) and name}
    names.discard('')
    marker = next(
        (value for value in ('[姓名已隐去]', '[REDACTED]', '□') if all(name not in value.casefold() for name in names)),
        '',
    )
    changes = []

    def project(item, pointer, path, report_text=False):
        if isinstance(item, str):
            if not report_text and path and _controlled_text(path, item):
                return item
            result = _redact_text(item, names, marker)
            if result != item:
                changes.append({'pointer': pointer, 'originalValue': item, 'projectedValue': result})
            return result
        if isinstance(item, list):
            return [
                project(child, pointer + '/' + str(index), (*path, None), report_text)
                for index, child in enumerate(item)
            ]
        if isinstance(item, dict):
            result = {}
            material_path = path[1:] if path and path[0] == 'snapshot' else path
            report_row = (
                len(material_path) == 2
                and material_path[0] in _MATERIALS
                and material_path[1] is None
                and item.get('kind') == 'report'
            )
            for key, child in item.items():
                if not isinstance(key, str):
                    raise AnalysisGenerationError('Model projection requires JSON keys')
                escaped = key.replace('~', '~0').replace('/', '~1')
                result[key] = project(
                    child,
                    pointer + '/' + escaped,
                    (*path, key),
                    report_text or report_row and key in ('content', 'metadata'),
                )
            return result
        return deepcopy(item)

    return project(value, '', ()), {'version': 'confirmed-name-projection-v2', 'changes': changes}
