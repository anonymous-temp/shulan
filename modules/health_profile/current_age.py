"""Project the shared Profile age into a transient consumer context."""

from datetime import date
from datetime import datetime

from health_profile.basic_info import BIRTH_TYPES
from health_profile.basic_info import field_for_fact


def _time_text(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError('Profile current age has an invalid observation time')


def profile_age_context(page, fact):
    """Use the current age only for the matching fact in this fixed Profile page."""
    current = page.coverage.get('currentAge')
    if (
        page.subject is None
        or page.subject.kind != 'self'
        or fact.category != 'demographic'
        or not isinstance(current, dict)
        or current.get('factId') != fact.fact_id
        or current.get('ageBasis') not in {'reported_at_report', 'confirmed_at_time', 'dob_derived'}
        or type(current.get('value')) is not int
    ):
        return {'value': fact.value}
    if fact.fact_type in BIRTH_TYPES and current['ageBasis'] == 'dob_derived':
        return {
            'value': fact.value,
            'current_age': (current['value'], current['ageBasis'], _time_text(current.get('asOf'))),
        }
    if field_for_fact(fact) != 'age':
        return {'value': fact.value}
    return {
        'value': current['value'],
        'age_basis': current['ageBasis'],
        'age_as_of': _time_text(current.get('asOf')) if current['ageBasis'] == 'dob_derived' else None,
        'age_observed_at': _time_text(current.get('observedAt')),
        'age_confirmed_at': _time_text(current.get('confirmedAt')),
    }
