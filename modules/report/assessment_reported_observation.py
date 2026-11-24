"""Model views of admitted questionnaire assertions, not whole mixed answers."""

from __future__ import annotations

import re
from copy import deepcopy

from .clinical_source_projection import reported_temporal_context


def model_reported_observation(fact, *, reported_role=None, temporal_context=None):
    """Keep one selected assertion separate from its original question/answer.

    Extracted facets are only selected when they occur in the actual answer.
    Without such a literal selection the complete answer remains available;
    a model-generated facet can never replace or invent the source response.
    """
    if fact.get('kind') != 'self_reported':
        return None
    role = reported_role or fact.get('_reportedSourceRole') or {}
    original = fact.get('value')
    wrapped = re.match(r'^\s*问题[：:](.*?)；\s*回答[：:](.*)$', str(original or ''), re.S)
    question = role.get('question') or (wrapped[1] if wrapped else fact.get('name'))
    answer = role['answer'] if 'answer' in role else wrapped[2] if wrapped else original
    facet = role.get('extractedFacet')
    selected = isinstance(facet, str) and bool(facet.strip()) and isinstance(answer, str) and facet in answer
    temporal = deepcopy(temporal_context or role.get('temporalContext') or reported_temporal_context(
        fact, reported_role=role,
    ))
    result = {
        'label': fact.get('name'),
        'value': facet if selected else deepcopy(answer),
        'measurementType': 'reported_assertion' if role else 'source_reported_answer',
        'assertionRole': role.get('role') or 'source_reported_answer',
        'certainty': role.get('certainty'),
        'selectionBasis': 'literal_answer_facet' if selected else 'complete_source_answer',
        'questionContext': question,
        'temporalContext': temporal,
        'literalSource': {
            **{key: deepcopy(fact[key]) for key in ('name', 'value', 'status', 'date', 'source') if key in fact},
            'question': question,
            'answer': deepcopy(answer),
        },
    }
    if temporal.get('reportedAt') is not None:
        result['reportedAt'] = deepcopy(temporal['reportedAt'])
    if temporal.get('kind') == 'retrospective_event':
        result['eventIdentity'] = {
            'scope': 'reported_recalled_event_only',
            'eventDate': temporal.get('eventDate'),
            'eventId': temporal.get('eventId'),
            'clinicalLinkStatus': temporal.get('eventLinkStatus'),
            'mayQualifyDatedClinicalResult': temporal.get('mayQualifyDatedClinicalResult'),
        }
    return result


def model_reported_role(role):
    """Expose the assertion's role without reintroducing the whole answer."""
    return {key: deepcopy(value) for key, value in role.items() if key not in {'question', 'answer'}}
