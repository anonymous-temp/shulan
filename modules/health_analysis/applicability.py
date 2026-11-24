"""Evaluate explicit, cited age boundaries against the frozen current patient age."""

import math
import re
from copy import deepcopy
from unicodedata import normalize

from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.factors import medical_citations

_AGE = r'[0-9]{1,3}(?:\.[0-9]+)?'
_YEARS = r'(?:years?(?: old)?|周?岁)'
_LOWER = r'>=|≥|>'
_UPPER = r'<=|≤|<'
_AGE_SCOPE = re.compile(r'^(?:(?:age range|eligibility|年龄范围|适用年龄)\s*:\s*)')
_ENGLISH_SCOPE = r'(?:applies to|applicable to|eligible)\s+(?:(?:patients|people|persons)\s+)?(?:aged?\s+)?'
_PATTERNS = (
    (
        re.compile(rf'({_AGE})\s*({_UPPER})\s*age\s*({_UPPER})\s*({_AGE})\s*{_YEARS}'),
        lambda match: (float(match[1]), float(match[4]), match[2] != '<', match[3] != '<'),
    ),
    (
        re.compile(
            rf'(?:适用于\s*(?:年龄)?\s*|age\s*)?({_LOWER})\s*({_AGE})\s*{_YEARS}\s*(?:and|且)\s*({_UPPER})\s*({_AGE})\s*{_YEARS}'
        ),
        lambda match: (float(match[2]), float(match[4]), match[1] != '>', match[3] != '<'),
    ),
    (
        re.compile(rf'(?:适用于\s*(?:年龄)?\s*|age\s*)?({_LOWER}|{_UPPER})\s*({_AGE})\s*{_YEARS}'),
        lambda match: _one_endpoint(float(match[2]), match[1] in ('>', '>=', '≥'), match[1] not in ('>', '<')),
    ),
    (
        re.compile(rf'适用于\s*(?:年龄)?\s*({_AGE})\s*{_YEARS}\s*(及以上|或以上|及以下|或以下)(?:的?人群)?'),
        lambda match: _one_endpoint(float(match[1]), match[2].endswith('以上'), True),
    ),
    (
        re.compile(rf'适用于\s*(?:年龄)?\s*(年满|未满|超过|低于)\s*({_AGE})\s*{_YEARS}(?:的?人群)?'),
        lambda match: _one_endpoint(float(match[2]), match[1] in ('年满', '超过'), match[1] == '年满'),
    ),
    (
        re.compile(rf'{_ENGLISH_SCOPE}(at least|over|older than|at most|under|younger than)\s+({_AGE})\s*{_YEARS}'),
        lambda match: _one_endpoint(
            float(match[2]), match[1] in ('at least', 'over', 'older than'), match[1] in ('at least', 'at most')
        ),
    ),
    (
        re.compile(rf'{_ENGLISH_SCOPE}(?:ages\s+)?({_AGE})\s*(?:to|[-–])\s*({_AGE})\s*{_YEARS}\s+inclusive'),
        lambda match: (float(match[1]), float(match[2]), True, True),
    ),
)


def _one_endpoint(age, lower, inclusive):
    return (age, None, inclusive, None) if lower else (None, age, None, inclusive)


def _verified_age_endpoints(citation, passage_text):
    # Require a complete independently delimited statement from the actual
    # passage so a quoted substring cannot omit e.g. "not" or "typically".
    def sentences(text):
        text = normalize('NFKC', text).casefold()
        return [
            ' '.join(match[1].split())
            for match in re.finditer(r'(.+?)([。!?]+|(?<![0-9])\.|\.(?![0-9])|$)', text, re.DOTALL)
            if match[1].strip() and '?' not in match[2]
        ]

    quotes = sentences(citation['quote'])
    if len(quotes) != 1 or quotes[0] not in sentences(passage_text):
        return None
    statement = _AGE_SCOPE.sub('', quotes[0], count=1)
    for pattern, endpoints in _PATTERNS:
        match = pattern.fullmatch(statement)
        if match is not None:
            return endpoints(match)
    return None


def age_applicability(rule, demographics, sources):
    """A failed candidate never closes a catalogue target or removes source facts.

    Semantic extraction identifies a range and its scope. The program validates
    its citation, units and boundaries and computes eligibility. Prevalence,
    historical/family scope and sub-year conditions cannot exclude this patient.
    """
    if rule is None:
        return {'status': 'not_restricted', 'eligible': True}
    if not isinstance(rule, dict):
        return {'status': 'invalid_age_rule', 'eligible': False}
    if rule.get('kind') == 'prevalence':
        return {'status': 'prevalence_not_exclusion', 'eligible': True}
    if rule.get('kind') != 'explicit_range':
        return {'status': 'invalid_age_rule', 'eligible': False}
    if rule.get('subject') not in ('self', 'family', 'other') or rule.get('time') not in ('current', 'historical'):
        return {'status': 'invalid_age_rule', 'eligible': False}
    if rule['subject'] != 'self' or rule['time'] != 'current':
        return {'status': 'different_subject_or_time', 'eligible': True}
    if rule.get('unit') != 'years':
        return {'status': 'requires_finer_age', 'eligible': False}
    lower, upper = rule.get('minAge'), rule.get('maxAge')
    if lower is None and upper is None:
        return {'status': 'invalid_age_rule', 'eligible': False}
    if any(
        value is not None and (type(value) not in (int, float) or not 0 <= value <= 150 or not math.isfinite(value))
        for value in (lower, upper)
    ):
        return {'status': 'invalid_age_rule', 'eligible': False}
    if lower is not None and upper is not None and lower > upper:
        return {'status': 'invalid_age_rule', 'eligible': False}
    if any(type(rule.get(key)) is not bool for key in ('minInclusive', 'maxInclusive')):
        return {'status': 'invalid_age_rule', 'eligible': False}
    sources_by_id = {source['id']: source for source in sources}
    try:
        citations = medical_citations([rule.get('evidence')], sources_by_id)
    except HealthAnalysisGenerationError:
        return {'status': 'unverified_age_rule', 'eligible': False}
    citation = citations[0]
    passage = next(p for p in sources_by_id[citation['sourceId']]['passages'] if p['id'] == citation['passageId'])
    expected = (
        lower,
        upper,
        rule['minInclusive'] if lower is not None else None,
        rule['maxInclusive'] if upper is not None else None,
    )
    if _verified_age_endpoints(citation, passage['text']) != expected:
        return {'status': 'unverified_age_rule', 'eligible': False}
    age = demographics.get('ageYears')
    if type(age) is not int or not 0 <= age <= 150:
        return {'status': 'unknown_current_age', 'eligible': False}
    # Compare integer endpoints against the confirmed completed years.
    # A fractional endpoint inside that year requires a finer-grained age.
    if any(endpoint is not None and age < endpoint < age + 1 for endpoint in (lower, upper)):
        return {'status': 'requires_finer_age', 'eligible': False}
    eligible = (lower is None or age > lower or age == lower and rule['minInclusive']) and (
        upper is None or age < upper or age == upper and rule['maxInclusive']
    )
    return {
        'status': 'within_age_range' if eligible else 'outside_age_range',
        'eligible': eligible,
        'ageYears': age,
        'rule': deepcopy(rule),
        'evidence': citations,
    }
