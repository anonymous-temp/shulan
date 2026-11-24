"""Select complete, source-bound predecessor paragraphs for a new assessment cell's review."""

from __future__ import annotations

_SECTION_INPUTS = {
    'overviewSummary': ('system.', 'body.', 'synthesis.', 'priority.urgent'),
    'bodySummary': ('body.',),
    'graphSummary': ('synthesis.', 'system.'),
    'symptomSummary': ('symptom.', 'symptomEvidence.'),
    'clinicalLead': ('clinical.', 'clinicalRow.', 'specialtyRow.'),
    'specialtySummary': ('specialtyRow.',),
    'microbiomeLead': ('digestive.', 'structure.', 'organisms.'),
    'structureSummary': ('structure.',),
    'riskSummary': ('clinicalRow.', 'digestive.', 'specialtyRow.', 'symptom.'),
    'clusterSummary': ('synthesis.', 'synthesisAction.'),
    'conclusionSummary': ('layer.', 'synthesis.', 'synthesisAction.', 'system.'),
}


def reviewed_source_prefixes(target):
    owner = target.get('owner') or {}
    if not owner.get('contentPlanVersion'):
        return ()
    key = owner.get('contentScopeCode') or ''
    stem, _, code = key.partition('.')
    if stem == 'clinical':
        return ('clinicalRow.' + code + '.',)
    if stem == 'symptom':
        return ('symptomEvidence.' + code + '.',)
    if stem == 'synthesisAction':
        return ('synthesis.' + code, 'clinicalRow.', 'specialtyRow.', 'lifestyle.')
    if stem == 'system' and code == 'microbiome':
        return ('structure.', 'organisms.', 'function.')
    if stem == 'system':
        return ('clinical.', 'clinicalRow.', 'body.', 'specialtyRow.')
    if stem in {'synthesis', 'recognition'}:
        return ('clinicalRow.', 'specialtyRow.', 'body.', 'symptom.', 'organisms.', 'function.')
    if stem == 'management':
        return {
            'lifestyle': ('lifestyle.', 'recognition.', 'synthesisAction.'),
            'fiber': ('lifestyle.', 'digestive.', 'function.', 'symptom.'),
            'nutrient': ('function.', 'lifestyle.', 'clinicalRow.'),
            'probiotic': ('organisms.', 'symptom.', 'digestive.'),
        }.get(code, ())
    if stem in {'layer', 'priority'}:
        return ('synthesisAction.', 'synthesis.', 'clinicalRow.', 'specialtyRow.', 'lifestyle.')
    if stem == 'lifestyle':
        return ('body.', 'clinicalRow.', 'specialtyRow.', 'symptom.')
    return _SECTION_INPUTS.get(key, ())


def reviewed_cell_sources(target, previous, blueprint_targets, handles, *, visible_ids=None, max_chars=1300):
    """Return candidate readings, never a completed target or a substitute for its evidence review.

    A whole paragraph and every one of its factual witnesses must be admissible
    to the receiving cell. One shared record cannot transfer a paragraph that
    also describes other organs, measurements, people or dates.
    """
    prefixes = reviewed_source_prefixes(target)
    if not prefixes or max_chars < 1:
        return []
    owner = target['owner']
    allowed = set(target.get('sourceFactIds', ())).intersection(handles)
    if visible_ids is not None:
        allowed.intersection_update(visible_ids)
    primary = set(owner.get('primaryFactIds', ()))
    action = owner.get('requiredRole') == 'action'
    choices = []
    for blocks in (previous or {}).values():
        for block in blocks:
            source_id = block.get('targetId')
            source = blueprint_targets.get(source_id)
            if not source or source_id == target['id'] or block.get('completion') != 'complete':
                continue
            source_owner = source.get('owner') or {}
            key = source_owner.get('contentScopeCode') or ''
            order = next((i for i, prefix in enumerate(prefixes)
                          if key == prefix or prefix.endswith('.') and key.startswith(prefix)), None)
            if order is None:
                continue
            for paragraph in block.get('sentences', ()):
                text = (paragraph.get('text') or '').strip()
                ids = list(dict.fromkeys(paragraph.get('factIds', ())))
                if not ids or not set(ids).issubset(allowed) or not text or len(text) > max_chars:
                    continue
                role = paragraph.get('role')
                if role not in {'interpretation', 'observation', 'action'}:
                    continue
                rank = (not block.get('analysisGenerated', True), not primary.intersection(ids),
                        role != ('action' if action else 'interpretation'), order)
                choices.append((rank, {
                    'reviewedTarget': source_id, 'sourceItem': source_owner.get('name'), 'sourceScope': key,
                    'completion': block['completion'], 'analysisGenerated': block.get('analysisGenerated', True),
                    'text': text, 'recordIds': [handles[fid] for fid in ids], 'role': role,
                    'support': paragraph.get('support'),
                    'originalEvidence': [
                        {'number': citation['number'], 'quotes': list(citation.get('quotes') or ())}
                        for citation in paragraph.get('evidence') or () if citation.get('number') is not None
                    ],
                }))
    selected, used_sources, used_texts = [], set(), set()
    chars = 0
    for _, choice in sorted(choices, key=lambda pair: pair[0]):
        source_id, text = choice['reviewedTarget'], choice['text']
        if source_id in used_sources or text in used_texts or chars + len(text) > max_chars:
            continue
        selected.append(choice)
        used_sources.add(source_id)
        used_texts.add(text)
        chars += len(text)
        if len(selected) == 5:
            break
    return selected
