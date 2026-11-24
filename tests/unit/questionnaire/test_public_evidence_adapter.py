"""Real public Profile policy remains isolated from Questionnaire and private upgrades."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.questionnaire_evidence import ProfileQuestionnaireEvidencePolicy
from adapters.questionnaire_evidence import create_questionnaire_evidence_policy
from modules.health_profile.evidence_contracts import PROFILE_STATEMENT_EVIDENCE_V1
from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION
from modules.questionnaire.evidence import QuestionnaireEvidenceVersionError
from modules.questionnaire.factors_answers import target_answer_refs


def test_real_fixed_rules_survive_a_private_profile_upgrade(monkeypatch):
    import health_profile.record_observations as private

    monkeypatch.setattr(private, 'question_topic', Mock(side_effect=AssertionError('private policy changed')))
    monkeypatch.setattr(
        private, 'normalize_record_statement', Mock(side_effect=AssertionError('private policy changed'))
    )
    rules = create_questionnaire_evidence_policy(selection=LEGACY_EVIDENCE_RULE_SELECTION)
    target = {'kind': 'disease', 'label': '高血压', 'answerIds': ['a']}
    answer = {
        'id': 'a',
        'questionName': 'medical_history',
        'question': {'title': '曾经确诊哪些疾病？', 'type': 'checkbox'},
        'answer': ['高血压'],
        'answerText': '高血压',
        'answerQuoteUnits': ['高血压'],
    }
    before = deepcopy(answer)
    assert target_answer_refs(target, [answer], evidence_policy=rules) == ['a']
    assert answer == before
    with pytest.raises(QuestionnaireEvidenceVersionError):
        create_questionnaire_evidence_policy(
            selection={**LEGACY_EVIDENCE_RULE_SELECTION, 'implementationRevision': 'v2'}
        )
    assert target_answer_refs(target, [answer], evidence_policy=rules) == ['a']
    private.question_topic.assert_not_called()
    private.normalize_record_statement.assert_not_called()


@pytest.mark.parametrize('kind', ['context', 'statement'])
@pytest.mark.parametrize('invalid', ['different_version', 'invalid_contract'])
def test_adapter_rejects_a_result_from_an_unselected_policy(kind, invalid):
    result = {
        'selection': {**LEGACY_EVIDENCE_RULE_SELECTION, 'implementationRevision': 'future'},
        **(
            {'topic': 'medical_history'}
            if kind == 'context'
            else {
                'statementKind': 'condition',
                'subjectKind': 'self',
                'presence': 'present',
            }
        ),
    }
    if invalid == 'invalid_contract':
        result['selection'] = None
    provider = SimpleNamespace(
        selection=PROFILE_STATEMENT_EVIDENCE_V1,
        classify_context=Mock(return_value=result),
        assess_statement=Mock(return_value=result),
    )
    policy = ProfileQuestionnaireEvidencePolicy(provider=provider, selection=LEGACY_EVIDENCE_RULE_SELECTION)
    with pytest.raises(QuestionnaireEvidenceVersionError):
        if kind == 'context':
            policy.classify_context(question='历史', dimension='medical_history')
        else:
            policy.assess_statement(
                kind='condition', label='高血压', answer=['高血压'], question='历史', dimension='medical_history'
            )


def test_public_request_keeps_original_json_and_text():
    captured = []

    def assess(request):
        captured.append(request.model_dump(mode='json', by_alias=True))
        request.answer.append('provider-local mutation')
        return {
            'selection': LEGACY_EVIDENCE_RULE_SELECTION,
            'statementKind': None,
            'subjectKind': None,
            'presence': None,
        }

    provider = SimpleNamespace(selection=PROFILE_STATEMENT_EVIDENCE_V1, assess_statement=assess)
    policy = ProfileQuestionnaireEvidencePolicy(provider=provider, selection=LEGACY_EVIDENCE_RULE_SELECTION)
    answer = ['  高血压  ', {'original': '保留\n原文'}]
    original = deepcopy(answer)
    result = policy.assess_statement(
        kind='condition', label=' 高血压 ', answer=answer, question=' 原始问题 ', dimension='goal'
    )
    assert answer == original
    assert captured == [
        {
            'kind': 'condition',
            'label': ' 高血压 ',
            'answer': original,
            'question': ' 原始问题 ',
            'dimension': 'goal',
        }
    ]
    assert result.statement_kind is None and result.subject_kind is None and result.presence is None
