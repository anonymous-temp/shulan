"""Settlement and provenance helpers for independently recoverable extraction slices."""

import hashlib
import json
from copy import deepcopy

from .extraction import document_sections
from .extraction import merge_document_results
from .extraction import merge_document_times


def empty_analysis():
    return {
        'reportType': 'unknown',
        'title': None,
        'reportTime': None,
        'aiSummary': None,
        'status': 'pending',
        'observations': [],
        'expansion': None,
    }


def nodes(data):
    yield data
    for section in data.get('sections', []):
        yield from nodes(section)


def usable(data):
    return any(
        node.get('observations')
        or (
            node.get('reportType') == 'imaging'
            and isinstance(node.get('expansion'), dict)
            and any(node['expansion'].get(key) for key in ('findings', 'diagnosis'))
        )
        for node in nodes(data)
    )


def unique_issues(issues):
    found = {}
    for issue in issues:
        found.setdefault(json.dumps(issue, ensure_ascii=False, sort_keys=True), issue)
    return list(found.values())


def recovery_identity(item):
    """A physical source identity, never a name/value similarity match."""
    proof = item.get('evidence') or {}
    locator = proof.get('locator') or {}
    if locator.get('table') is not None and locator.get('valueCell') is not None:
        return ('table', locator['table'], locator['valueCell'], tuple(locator.get('valueSpan') or ()))
    if proof.get('recoveryKey'):
        return ('prose_name', proof['recoveryKey'])
    if proof.get('occurrenceKey'):
        return ('physical_text', proof['occurrenceKey'])
    source = item.get('sourceId') or proof.get('sourceId')
    if source and not proof.get('quote'):
        return ('row', source)
    return None


def combine_verified(previous, current):
    """A retry may add missing facts, but cannot overwrite already accepted facts."""
    if not previous:
        return deepcopy(current)
    if not current:
        return deepcopy(previous)

    def empty(data):
        from .basic_info import has_basic_material

        return not data or not any(
            has_basic_material(node)
            or node.get('observations')
            or node.get('unresolvedItems')
            or node.get('title')
            or node.get('reportTime')
            or (isinstance(node.get('expansion'), dict) and any(node['expansion'].values()))
            or isinstance(node.get('expansion'), list)
            and bool(node['expansion'])
            for node in nodes(data)
        )

    if empty(previous):
        return deepcopy(current)
    if empty(current):
        return deepcopy(previous)
    seen = {}
    parts = []
    confirmed = {
        identity
        for data in (previous, current)
        for node in document_sections(data)
        for item in node.get('observations', [])
        if (identity := recovery_identity(item)) is not None
    }
    pending_seen = set()
    for data in (previous, current):
        for node in document_sections(data):
            accepted = []
            for item in node.get('observations', []):
                source = item.get('sourceId')
                evidence = item.get('evidence') or {}
                locator = evidence.get('locator') or {}
                if locator.get('table') is not None and locator.get('valueCell') is not None:
                    identity = recovery_identity(item)
                elif source and not evidence.get('quote'):
                    identity = ('row', source)
                elif evidence.get('occurrenceKey'):
                    identity = ('physical_text', evidence['occurrenceKey'])
                else:
                    # Repeated name/value pairs in a block need not represent
                    # the same occurrence. Without a physical anchor, only an
                    # exact duplicate can be collapsed; do not borrow metadata.
                    literal = {key: value for key, value in item.items() if key != 'sourceSection'}
                    identity = ('literal', json.dumps(literal, ensure_ascii=False, sort_keys=True))
                previous_items = seen.setdefault(identity, [])
                matches = previous_items
                if len(matches) == 1:
                    old = matches[0]
                    for key in (
                        'referenceRange',
                        'unit',
                        'flag',
                        'rawFlag',
                        'observedAt',
                        'method',
                        'specimen',
                        'kind',
                    ):
                        if old.get(key) is None and item.get(key) is not None:
                            old[key] = deepcopy(item[key])
                            old.setdefault('metadataEvidence', {})[key] = deepcopy(evidence)
                else:
                    previous_items.append(item)
                    accepted.append(item)
            node['observations'] = accepted
            pending = []
            for item in node.get('unresolvedItems', []):
                identity = recovery_identity(item)
                if identity in confirmed:
                    continue
                dedupe = identity or ('literal', json.dumps(item, ensure_ascii=False, sort_keys=True))
                if dedupe not in pending_seen:
                    pending_seen.add(dedupe)
                    pending.append(item)
            node['unresolvedItems'] = pending
            if isinstance(node.get('expansion'), list):
                node['expansion'] = deepcopy(accepted)
            for key, value in empty_analysis().items():
                node.setdefault(key, deepcopy(value))
            has_narrative = isinstance(node.get('expansion'), dict) and any(node['expansion'].values())
            new_metadata = any(
                node.get(key) and not any(part.get(key) == node[key] for part in parts)
                for key in ('title', 'reportTime')
            )
            from .basic_info import has_basic_material

            if not accepted and not pending and not has_narrative and not new_metadata and not has_basic_material(node):
                # Retrying a known physical source does not create another
                # subreport merely because its duplicate items were removed.
                continue
            parts.append(node)
    result = merge_document_results(parts) if parts else empty_analysis()
    containers = [data for data in (previous, current) if data.get('sections') and data.get('metadataEvidence')]
    for key in ('title', 'reportTime'):
        candidates = [data for data in containers if data.get(key) and data['metadataEvidence'].get(key)]
        if not candidates:
            continue
        if key == 'title':
            result[key] = candidates[0][key]
        else:
            result[key] = merge_document_times(
                [*(part.get(key) for part in parts), *(data[key] for data in candidates)]
            )
        if result[key]:
            proof = next((data['metadataEvidence'][key] for data in candidates if data[key] == result[key]), None)
            if proof:
                result.setdefault('metadataEvidence', {})[key] = deepcopy(proof)
    return result


def settle(normalized, *, issues=(), evidence=(), version=None):
    """Keep clinical status distinct from extraction quality; never infer global normality."""
    result = deepcopy(normalized)
    issues = unique_issues(issues)
    has_results = usable(result)
    unresolved = False
    for node in nodes(result):
        if node.get('unresolvedItems'):
            unresolved = True
            if node.get('status') != 'abnormal':
                node['status'] = 'pending'
    quality = 'partial' if issues or unresolved else 'complete'
    if not has_results:
        from .basic_info import has_basic_material

        quality = 'partial' if any(has_basic_material(node) for node in nodes(result)) else 'unavailable'
    if quality != 'complete' and result.get('status') != 'abnormal':
        result['status'] = 'pending'
    result['processing'] = {
        'status': quality,
        'retryable': quality != 'complete' and (not issues or any(issue.get('recoverable', True) for issue in issues)),
        'issueCount': len(issues),
        'issues': issues,
    }
    if version:
        result['processing']['version'] = version
    result['analysisEvidence'] = list(evidence)
    return result


def evidence_from(data):
    evidence = []
    for node in document_sections(data):
        for item in node.get('observations', []):
            evidence.append(deepcopy(item))
        expansion = node.get('expansion')
        proofs = node.get('expansionEvidence') or {}
        if node.get('reportType') == 'imaging' and isinstance(expansion, dict):
            for key, name in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                if expansion.get(key) and isinstance(proofs.get(key), dict):
                    evidence.append(
                        {
                            'itemName': name,
                            'resultValue': expansion[key],
                            'kind': 'narrative',
                            'narrativeType': key,
                            'evidence': deepcopy(proofs[key]),
                        }
                    )
    return evidence


def source_bindings(result, evidence):
    """Bind only unambiguous physical occurrences; never guess between identical dated rows."""

    def binding(item):
        proof = item.get('evidence') or {}
        locator = proof.get('locator') or {}
        if locator.get('table') is not None and locator.get('valueCell') is not None:
            physical = (
                ('table-span', locator['table'], locator['valueCell'], *locator['valueSpan'])
                if locator.get('valueSpan')
                else ('table', locator['table'], locator['valueCell'])
            )
        elif proof.get('occurrenceKey'):
            physical = ('prose', proof['occurrenceKey'])
        else:
            return None
        return 'v6:' + hashlib.sha256(json.dumps(physical, ensure_ascii=False).encode()).hexdigest()

    def matching(item):
        if item.get('evidence') and (key := binding(item)):
            return key
        candidates = set()
        for original in evidence:
            if original.get('itemName') != item.get('itemName') or str(original.get('resultValue')) != str(
                item.get('resultValue')
            ):
                continue
            if any(
                original.get(key) is not None and item.get(key) is not None and original[key] != item[key]
                for key in ('observedAt', 'method', 'specimen', 'unit', 'kind')
            ):
                continue
            key = binding(original)
            if key:
                candidates.add(key)
        return next(iter(candidates)) if len(candidates) == 1 else None

    bindings = {}

    def visit(node, path):
        for index, item in enumerate(node.get('observations', [])):
            if key := matching(item):
                bindings[f'{path}/observations/{index}'] = key
        expansion = node.get('expansion')
        if node.get('reportType') == 'imaging' and isinstance(expansion, dict):
            for field, name in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                if expansion.get(field) and (key := matching({'itemName': name, 'resultValue': expansion[field]})):
                    bindings[f'{path}/expansion/{field}'] = key
        for index, section in enumerate(node.get('sections', [])):
            visit(section, f'{path}/sections/{index}')

    visit(result, '')
    return bindings


def trusted_summary(result):
    """Describe only the final retained results and original report narratives."""
    observations = result.get('observations', [])
    if observations:
        abnormal = [item['itemName'] for item in observations if item.get('flag') in ('偏高', '偏低')]
        summary = f'已提取 {len(observations)} 项报告结果。'
        if abnormal:
            summary += '其中' + '、'.join(dict.fromkeys(abnormal))[:140] + '有异常标记。'
        return summary
    findings = []
    for node in result.get('sections') or [result]:
        expansion = node.get('expansion')
        if node.get('reportType') == 'imaging' and isinstance(expansion, dict):
            findings.extend(expansion[key] for key in ('findings', 'diagnosis') if expansion.get(key))
    return '；'.join(dict.fromkeys(findings))[:200] or None


def settle_summaries(result, *, rebuild=False):
    """Finalize optional summaries after corrections, merging and source checks."""
    partial = result.get('processing', {}).get('status') != 'complete'
    incomplete = partial or any(node.get('unresolvedItems') for node in nodes(result))
    rebuild = rebuild or incomplete or bool(result.get('sections'))
    limitation = '仅依据已确认内容，不能代表整份报告。' if incomplete else ''
    for node in nodes(result):
        if not usable(node):
            node['aiSummary'] = None
            continue
        summary = node.get('aiSummary')
        summary = summary.strip() if isinstance(summary, str) else None
        if rebuild or summary and len(summary) > 200:
            summary = trusted_summary(node)
        node['aiSummary'] = summary[: 200 - len(limitation)].rstrip() + limitation if summary else None
