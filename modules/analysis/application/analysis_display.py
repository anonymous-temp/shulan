"""Project validated report JSON for the existing public API."""

import json
from copy import deepcopy

from utils.report_content import report_public_content

from .analysis_outcomes import nodes
from .analysis_outcomes import recovery_identity
from .analysis_outcomes import usable


def has_displayable(data):
    from .basic_info import has_basic_material

    if any(has_basic_material(node) for node in nodes(data)):
        return True
    if usable(data):
        return True
    return any(
        node.get('title')
        or node.get('reportTime')
        or any(
            item.get('itemName') or item.get('resultValue') is not None
            for item in node.get('unresolvedItems', [])
            if isinstance(item, dict)
        )
        for node in nodes(data)
    )


def display_result(data):
    """Project an independent API outcome, without weakening the shared clinical gate."""
    result = deepcopy(data)
    result.setdefault('observations', [])
    processing = result.setdefault('processing', {})
    has_results = usable(result)
    if has_results:
        processing.setdefault('status', 'complete')
    displayable = has_displayable(result)
    unresolved, seen = [], set()
    for index, node in enumerate(nodes(result)):
        for original in node.get('unresolvedItems', []):
            item = deepcopy(original)
            identity = recovery_identity(item) or (
                'literal',
                json.dumps({key: value for key, value in item.items() if key != 'sourceSection'}, sort_keys=True),
            )
            if identity in seen:
                continue
            seen.add(identity)
            if node is not result:
                item.setdefault('sourceSection', node.get('sectionIndex', index - 1))
            unresolved.append(item)
    result['unresolvedItems'] = unresolved
    if not has_results:
        processing['status'] = 'partial' if displayable else 'unavailable'
        result['status'] = 'pending'
        result['aiSummary'] = None
    processing.setdefault('retryable', processing.get('status') != 'complete')
    processing.setdefault('issueCount', 0)
    public = report_public_content(result)
    public.setdefault('unresolvedItems', [])
    return public
