"""Version-isolated evidence semantics, with legacy behavior as a migration oracle."""

import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest
from pydantic import ValidationError

from health_profile import record_observations as legacy
from modules.health_profile.application import ProfileEvidencePolicyUnsupportedError
from modules.health_profile.application import create_profile_evidence_policy
from modules.health_profile.evidence_contracts import PROFILE_STATEMENT_EVIDENCE_V1
from modules.health_profile.evidence_contracts import ProfileEvidenceContextV1
from modules.health_profile.evidence_contracts import ProfileEvidencePolicySelection
from modules.health_profile.evidence_contracts import ProfileStatementEvidenceV1


@pytest.fixture
def policy():
    return create_profile_evidence_policy(selection=PROFILE_STATEMENT_EVIDENCE_V1)


CONTEXT_CASES = [
    ('家族中是否有高血压病史', None, 'family_history'),
    ('既往病史有哪些', None, 'medical_history'),
    ('本次最希望改善什么健康问题', None, 'goal'),
    ('普通问题', None, None),
    ('家族中是否有高血压病史', 'medical_history', 'medical_history'),
    ('既往病史有哪些', 'other_dimension', None),
    ('既往病史有哪些', '', 'medical_history'),
    ('普通问题', 'family_history', 'family_history'),
]


@pytest.mark.parametrize('question,dimension,expected', CONTEXT_CASES)
def test_context_is_identical_to_legacy_and_retains_explicit_dimension(policy, question, dimension, expected):
    request = ProfileEvidenceContextV1(question=question, dimension=dimension)
    result = policy.classify_context(request)
    assert result.topic == expected == legacy.question_topic(question, dimension)
    assert result.selection == PROFILE_STATEMENT_EVIDENCE_V1


STATEMENT_CASES = [
    ('condition', '高血压', ['高血压'], 'medical_history', ('condition', 'self', 'present')),
    ('condition', '高血压', '高血压', 'goal', ('interest', 'self', 'present')),
    ('condition', '高血压', '我确诊高血压', 'goal', ('condition', 'self', 'present')),
    ('condition', '高血压', '我没有高血压', 'goal', ('history', 'self', 'absent')),
    ('condition', '高血压', '父亲确诊高血压', 'goal', (None, None, None)),
    ('condition', '高血压', '我没有高血压，父亲有高血压', 'goal', ('history', 'self', 'absent')),
    ('condition', '高血压', '我有高血压，但没有接受治疗', 'goal', ('condition', 'self', 'present')),
    ('condition', '高血压', '我以前患有高血压，现在已经痊愈', 'goal', ('history', 'self', 'present')),
    ('symptom', '腹胀', '我以前有腹胀，现在已经好了', 'goal', ('symptom', 'self', 'present')),
    ('condition', '高血压', ['高血压', '没有'], 'medical_history', ('history', 'self', 'conflicting')),
    ('condition', '高血压', '我确诊高血压和糖尿病', 'goal', ('condition', 'self', 'present')),
    ('condition', '糖尿病', '我没有高血压和糖尿病', 'goal', ('history', 'self', 'absent')),
]


def _legacy_result(request):
    fact_type = 'condition.diagnosis' if request.kind == 'condition' else 'symptom.reported'
    result = legacy.normalize_record_statement(
        fact_type=fact_type,
        value=request.label,
        display_name=request.label,
        evidence=request.label,
        answer=request.answer,
        question=request.question,
        dimension=request.dimension,
        subject_kind='self',
        subject_hint='本人',
        stable_relationship=False,
    )
    if result is None:
        return None, None, None
    kinds = {
        'condition.diagnosis': 'condition',
        'symptom.reported': 'symptom',
        'condition.history_status': 'history',
        'family_history.reported': 'family_history',
        'family_history.condition': 'family_history',
        'goal.health': 'interest',
    }
    return (
        kinds[result.fact_type],
        result.subject_kind,
        result.value.get('presence') if isinstance(result.value, dict) else 'present',
    )


def _verdict(result):
    return result.statement_kind, result.subject_kind, result.presence


@pytest.mark.parametrize('kind,label,answer,dimension,expected', STATEMENT_CASES)
def test_frozen_source_assertion_cases_preserve_migration_behavior(policy, kind, label, answer, dimension, expected):
    request = ProfileStatementEvidenceV1(
        kind=kind, label=label, answer=answer, question='健康情况', dimension=dimension
    )
    result = policy.assess_statement(request)
    assert _verdict(result) == expected == _legacy_result(request)
    assert result.selection == PROFILE_STATEMENT_EVIDENCE_V1


@pytest.mark.parametrize('kind', ['condition', 'symptom'])
@pytest.mark.parametrize('dimension', [None, 'medical_history', 'goal', 'family_history'])
@pytest.mark.parametrize(
    'answer',
    [
        '我有高血压',
        '我没有高血压，父亲有高血压',
        '母亲没有高血压，我确诊高血压',
        '朋友有高血压',
        '我可能有高血压',
        '如果我有高血压，想了解怎么办',
        '我想咨询高血压',
        '我不清楚有没有高血压',
        '我有高血压，但记不清确诊日期',
        '我没有接受高血压治疗',
        '我有高血压，但未接受治疗',
        '我没有被医生明确诊断为高血压',
        '我以前有高血压，现在已经恢复',
        '我没有糖尿病和高血压，但父亲有糖尿病',
        ['高血压', '没有'],
        ['高血压', '不清楚'],
        ['高血压', '糖尿病'],
        {'原文': ['我确诊高血压'], '数量': 1, '未知': None},
    ],
)
def test_supported_self_projection_matches_full_legacy_rule_chain(policy, kind, dimension, answer):
    request = ProfileStatementEvidenceV1(
        kind=kind, label='高血压', answer=answer, question='健康情况', dimension=dimension
    )
    assert _verdict(policy.assess_statement(request)) == _legacy_result(request)


def test_mutating_all_current_legacy_rules_cannot_change_fixed_v1(policy, monkeypatch):
    request = ProfileStatementEvidenceV1(
        kind='condition', label='高血压', answer='我确诊高血压', question='本次健康问题', dimension='goal'
    )
    before = policy.assess_statement(request)

    def changed(*args, **kwargs):
        raise RuntimeError('current legacy revision has different rules')

    for name, value in tuple(vars(legacy).items()):
        if getattr(value, '__module__', None) == legacy.__name__ and callable(value):
            monkeypatch.setattr(legacy, name, changed)
        elif name.isupper() or name.startswith('_') and name[1:].isupper():
            monkeypatch.setattr(legacy, name, None)
    with pytest.raises(RuntimeError, match='different rules'):
        legacy.normalize_record_statement()
    assert policy.assess_statement(request) == before
    assert create_profile_evidence_policy(selection=PROFILE_STATEMENT_EVIDENCE_V1).assess_statement(request) == before
    assert policy.classify_context(ProfileEvidenceContextV1(question='既往病史')).topic == 'medical_history'


@pytest.mark.parametrize(
    'contract,revision',
    [
        ('profile.statement-evidence.v2', 'profile-statement-evidence-v1'),
        ('profile.statement-evidence.v1', 'profile-statement-evidence-v2'),
        ('profile.statement-evidence.v1', 'latest'),
    ],
)
def test_unimplemented_exact_selection_is_rejected_without_fallback(contract, revision):
    selection = ProfileEvidencePolicySelection(contract_version=contract, implementation_revision=revision)
    with pytest.raises(ProfileEvidencePolicyUnsupportedError, match='Unsupported Profile evidence policy'):
        create_profile_evidence_policy(selection=selection)


def test_factory_has_no_implicit_default_and_selection_and_results_are_frozen(policy):
    with pytest.raises(TypeError):
        create_profile_evidence_policy()
    with pytest.raises(ValidationError):
        policy.selection.implementation_revision = 'latest'
    result = policy.classify_context(ProfileEvidenceContextV1(question='既往病史'))
    with pytest.raises(ValidationError):
        result.topic = 'goal'
    assert result.model_dump(mode='json', by_alias=True) == {
        'selection': {
            'contractVersion': 'profile.statement-evidence.v1',
            'implementationRevision': 'profile-statement-evidence-v1',
        },
        'topic': 'medical_history',
    }


def test_original_json_is_copied_and_never_rewritten_by_evaluation(policy):
    answer = {'原文': ['  我确诊高血压  '], '数值': 1, '布尔': False, '空': None}
    original = deepcopy(answer)
    request = ProfileStatementEvidenceV1(
        kind='condition', label='高血压', answer=answer, question='健康情况', dimension='goal'
    )
    answer['原文'].append('后来我没有高血压')
    assert request.answer == original
    assert list(request.answer) == list(original)
    policy.assess_statement(request)
    assert request.answer == original
    assert type(request.answer['数值']) is int and type(request.answer['布尔']) is bool


@pytest.mark.parametrize('updates', [{'kind': 'diagnosis'}, {'label': 3}, {'question': False}, {'dimension': 1}])
def test_contract_does_not_coerce_candidate_text_or_unknown_kinds(updates):
    raw = {'kind': 'condition', 'label': '高血压', 'answer': ['高血压'], 'question': '病史', **updates}
    with pytest.raises(ValidationError):
        ProfileStatementEvidenceV1.model_validate(raw)


def test_pure_public_factory_import_and_execution_never_load_legacy_io_modules():
    script = r"""
import importlib.abc
import sys
class Forbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'health_profile', 'agents', 'db', 'config', 'pymongo', 'redis'}:
            raise AssertionError('unexpected I/O dependency: ' + fullname)
sys.meta_path.insert(0, Forbidden())
from modules.health_profile.application import create_profile_evidence_policy
from modules.health_profile.evidence_contracts import PROFILE_STATEMENT_EVIDENCE_V1
from modules.health_profile.evidence_contracts import ProfileEvidenceContextV1
from modules.health_profile.evidence_contracts import ProfileStatementEvidenceV1
policy = create_profile_evidence_policy(selection=PROFILE_STATEMENT_EVIDENCE_V1)
context = ProfileEvidenceContextV1(question='history', dimension='medical_history')
assert policy.classify_context(context).topic == 'medical_history'
request = ProfileStatementEvidenceV1(
    kind='symptom', label='pain', answer='pain', question='symptom', dimension='medical_history'
)
result = policy.assess_statement(request)
assert result.statement_kind == 'symptom'
assert 'health_profile.record_observations' not in sys.modules
print('pure evidence policy ok')
"""
    result = subprocess.run(
        [sys.executable, '-B', '-c', script],
        check=False,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=20,
        env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'pure evidence policy ok'


def test_public_facade_exports_share_the_exact_dto():
    from modules.health_profile import contracts

    assert contracts.ProfileStatementEvidenceV1 is ProfileStatementEvidenceV1
    assert json.loads(PROFILE_STATEMENT_EVIDENCE_V1.model_dump_json(by_alias=True))['implementationRevision'] == (
        'profile-statement-evidence-v1'
    )
