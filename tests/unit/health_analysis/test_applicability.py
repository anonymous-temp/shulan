"""Explicit synthetic age limits test arithmetic and provenance, not medical policy."""

from copy import deepcopy

import pytest

from modules.health_analysis.applicability import age_applicability

SOURCES = [
    {
        'id': 's',
        'version': 'synthetic-v1',
        'passages': [{'id': 'p', 'text': 'Eligibility: 18 <= age < 65 years.', 'locator': {'kind': 'test'}}],
    }
]
RULE = {
    'kind': 'explicit_range',
    'subject': 'self',
    'time': 'current',
    'unit': 'years',
    'minAge': 18,
    'maxAge': 65,
    'minInclusive': True,
    'maxInclusive': False,
    'evidence': {'sourceId': 's', 'passageId': 'p', 'quote': SOURCES[0]['passages'][0]['text']},
}


@pytest.mark.parametrize('age,eligible', [(17, False), (18, True), (64, True), (65, False), (66, False)])
def test_explicit_age_range_boundaries(age, eligible):
    result = age_applicability(RULE, {'ageYears': age}, SOURCES)
    assert result['eligible'] is eligible and result['evidence']


@pytest.mark.parametrize(
    'change', [{'subject': 'family'}, {'subject': 'other'}, {'time': 'historical'}, {'kind': 'prevalence'}]
)
def test_current_patient_age_cannot_reject_historical_family_or_prevalence(change):
    assert age_applicability({**RULE, **change}, {'ageYears': 0}, SOURCES)['eligible']


@pytest.mark.parametrize(
    'change',
    [
        {'evidence': {}},
        {'unit': 'months'},
        {'minAge': True},
        {'minAge': 10**400},
        {'subject': ['self']},
        {'time': {'when': 'current'}},
        {'maxAge': float('nan')},
        {'minAge': 70},
        {'minInclusive': 1},
        {'subject': 'unknown'},
    ],
)
def test_unverified_or_unexpressible_rule_does_not_publish_candidate(change):
    assert not age_applicability({**RULE, **change}, {'ageYears': 42}, SOURCES)['eligible']


def test_age_decision_does_not_mutate_source_or_frozen_rule():
    sources, rule = deepcopy(SOURCES), deepcopy(RULE)
    result = age_applicability(rule, {'ageYears': 42}, sources)
    result['rule']['minAge'] = 0
    assert sources == SOURCES and rule == RULE


@pytest.mark.asyncio
async def test_publication_filters_age_candidate_without_closing_its_target():
    from modules.health_analysis.deduplication import review_candidates

    async def call(stage, body):
        return {
            'questions': [
                {
                    'candidateId': 'q',
                    'verdict': 'approved',
                    'reason': 'synthetic',
                    'targetAligned': True,
                    'lifestyleOnly': True,
                    'novel': True,
                    'suitable': True,
                    'ageRule': RULE,
                }
            ]
        }

    candidate = {
        'candidateId': 'q',
        'targetId': 'exercise_frequency',
        'question': {'title': 'Synthetic current exercise'},
    }
    result = await review_candidates(
        call,
        questions=[candidate],
        interviews=[],
        tests=[],
        snapshot={'effective_demographics': {'ageYears': 65}},
        sources=SOURCES,
    )
    assert not result['questions']
    assert result['filters'][0]['ageApplicability']['status'] == 'outside_age_range'
    assert 'coverage' not in result


@pytest.mark.parametrize(
    'passage,quote,change',
    [
        ('适用于65岁及以上。', '适用于65岁及以上。', {'maxAge': None}),
        ('适用于18岁及以上。', '适用于18岁及以上。', {}),
        ('适用于18岁及以上。', '适用于18岁及以上。', {'maxAge': None, 'minInclusive': False}),
        ('本病好发于18至65岁。', '本病好发于18至65岁。', {}),
        ('适用于18月龄及以上。', '适用于18月龄及以上。', {'maxAge': None}),
        ('适用于18岁及以上？', '适用于18岁及以上？', {'maxAge': None}),
        ('普通人群可参考该材料。', '普通人群可参考该材料。', {}),
        ('不适用于18岁及以上。', '适用于18岁及以上。', {'maxAge': None}),
        ('Applicable to people aged 18 to 65 years.', 'Applicable to people aged 18 to 65 years.', {}),
        ('Eligibility: 18 <= age <= 65 years.', 'Eligibility: 18 <= age <= 65 years.', {}),
        ('Eligibility: 18 <= age < 66 years.', 'Eligibility: 18 <= age < 66 years.', {}),
    ],
)
def test_age_rule_requires_the_actual_cited_boundaries_and_endpoint_semantics(passage, quote, change):
    rule = {**RULE, **change, 'evidence': {**RULE['evidence'], 'quote': quote}}
    sources = deepcopy(SOURCES)
    sources[0]['passages'][0]['text'] = passage

    assert age_applicability(rule, {'ageYears': 42}, sources) == {
        'status': 'unverified_age_rule',
        'eligible': False,
    }


@pytest.mark.parametrize(
    'quote,change,age,status',
    [
        ('适用于65岁及以上。', {'minAge': 65, 'maxAge': None}, 65, 'within_age_range'),
        ('适用于未满65周岁。', {'minAge': None}, 65, 'outside_age_range'),
        ('适用于年龄≥18岁且<65岁。', {}, 65, 'outside_age_range'),
        ('Applies to patients aged over 18 years.', {'maxAge': None, 'minInclusive': False}, 18, 'outside_age_range'),
        ('Applicable to ages 18 to 65 years inclusive.', {'maxInclusive': True}, 65, 'within_age_range'),
        ('Eligibility: age >= 18.5 years.', {'minAge': 18.5, 'maxAge': None}, 19, 'within_age_range'),
    ],
)
def test_age_rule_applies_only_verified_units_and_open_or_closed_endpoints(quote, change, age, status):
    rule = {**RULE, **change, 'evidence': {**RULE['evidence'], 'quote': quote}}
    sources = deepcopy(SOURCES)
    sources[0]['passages'][0]['text'] = f'Hypothetical scenario. {quote} Method details follow.'

    result = age_applicability(rule, {'ageYears': age}, sources)

    assert result['status'] == status
    assert result['evidence'][0]['sourceVersion'] == 'synthetic-v1'


def test_fractional_bound_in_current_year_requires_finer_age():
    quote = 'Eligibility: age >= 18.5 years.'
    sources = deepcopy(SOURCES)
    sources[0]['passages'][0]['text'] = quote
    rule = {**RULE, 'minAge': 18.5, 'maxAge': None, 'evidence': {**RULE['evidence'], 'quote': quote}}

    assert age_applicability(rule, {'ageYears': 18}, sources) == {'status': 'requires_finer_age', 'eligible': False}
