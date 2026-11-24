"""Project already published seven-chapter prose into legacy follow-up slots.

This adapter neither authors nor approves content. Its input is the assembler's
actual accepted list, after target/fact/original validation and application to
``data``. Original evaluate sections stay intact. Legacy slots receive the same
words and per-publication provenance, never newly inferred causes or actions.
"""
from __future__ import annotations

from .evaluation_schema import clinical_problem_projection_codes
from .schemas import ClinicalProblemAnalysis, ClinicalReportBlock


def _node(data, path):
    for part in path:
        data = data[part] if isinstance(part, int) else getattr(data, part)
    return data


def _problem_owners(data, document):
    """Use the blueprint's canonical problem IDs, not shared-fact/name joins."""
    problems = {problem.problem_id for problem in document.problems}
    owners = {}
    for problem_id, code in clinical_problem_projection_codes(data).items():
        if problem_id in problems:
            owners.setdefault(code, []).append(problem_id)
    return owners


def _combined(code, rows, limits):
    """Combine verbatim accepted paragraphs with citation-specific bindings."""
    text, facts, references = [], [], []
    quotes, claims, reference_facts = {}, {}, {}
    for row in rows:
        if row['text'] not in text:
            text.append(row['text'])
        facts.extend(fid for fid in row['factIds'] if fid not in facts)
        references.extend(number for number in row['evidenceRefs'] if number not in references)
        for sentence in row['sentences']:
            for citation in sentence.get('evidence', []):
                key = str(citation['number'])
                quotes.setdefault(key, [])
                quotes[key].extend(quote for quote in citation['quotes'] if quote not in quotes[key])
                claims.setdefault(key, [])
                if sentence['text'] not in claims[key]:
                    claims[key].append(sentence['text'])
                reference_facts.setdefault(key, [])
                reference_facts[key].extend(fid for fid in sentence['factIds']
                    if fid not in reference_facts[key])
    value = '\n\n'.join(text)
    if not value:
        return None
    if len(value) > 4000:
        # The new sections retain the entire text. Do not clip a condition or
        # silently turn an incomplete old-format excerpt into the full claim.
        limits.append({'code': code, 'reason': 'legacy_block_limit',
            'targetIds': [row['targetId'] for row in rows]})
        return None
    return ClinicalReportBlock(code=code, text=value,
        state='insufficient' if any(row.get('completion') == 'source_only' for row in rows) else 'ready',
        fact_ids=facts, evidence_refs=references,
        evidence_quotes={key: '\n'.join(values) for key, values in quotes.items()},
        evidence_claims={key: '\n'.join(values) for key, values in claims.items()},
        evidence_fact_ids=reference_facts)


def adapt_followup_document(document, data, blueprint, accepted):
    """Populate existing follow-up aliases using actual applied blocks only.

    Mutates the delivery document, not source data or the authoring blueprint.
    Stable bibliography numbers remain untouched for the caller's one remap.
    """
    targets = {target['id']: target for chapter in blueprint.chapters
        for target in chapter['targets']}
    owners = _problem_owners(data, document)
    significance, medical_actions, priority_actions, next_actions = {}, {}, {}, {}
    microbiome, strategy, clinical_followup = [], [], []
    seen, limits = set(), []
    for row in accepted:
        target = targets.get(row.get('targetId'))
        if target is None or row['targetId'] in seen:
            continue
        path = tuple(target['path'])
        try:
            applied = _node(data, path)
        except (AttributeError, IndexError, KeyError, TypeError):
            continue
        # The raw author map is deliberately not accepted here. A rejected,
        # stale or merely drafted paragraph cannot create follow-up content.
        if not isinstance(row.get('text'), str) or applied != row['text']:
            continue
        seen.add(row['targetId'])
        code = target.get('owner', {}).get('code')
        if (len(path) == 4 and path[:2] == ('medical_assessment', 'findings')
                and path[-1] in {'explanation', 'source_advice'}):
            medical = data.medical_assessment.findings[path[2]]
            if code == medical.code:
                for pid in owners.get(code, []):
                    destination = significance if path[-1] == 'explanation' else medical_actions
                    destination.setdefault(pid, []).append(row)
        elif (len(path) == 4 and path[:2] == ('comprehensive_conclusion', 'priority_directions')
                and path[-1] == 'followup_action'):
            if owners.get(code):
                clinical_followup.append(row)
                for pid in owners[code]:
                    priority_actions.setdefault(pid, []).append(row)
        elif (len(path) == 4 and path[:2] == ('comprehensive_conclusion', 'next_steps')
                and path[-1] == 'description'):
            if code == data.comprehensive_conclusion.next_steps[path[2]].code and owners.get(code):
                strategy.append(row)
                for pid in owners[code]:
                    next_actions.setdefault(pid, []).append(row)
        elif path and path[0] == 'microbiome_assessment':
            # These are result/meaning slots, not diet, supplement or therapy
            # slots. Their actual measurement scope stays in the copied prose.
            microbiome.append(row)

    # No old generated analyses acquire authority merely by surviving on the
    # organizer document. Only this run's exact published paragraphs are used.
    document.analyses = []
    for problem in document.problems:
        pid = problem.problem_id
        meaning = _combined(f'problem.{pid}.significance', significance.get(pid, []), limits)
        # Priority follow-up leads; separately reviewed same-owner medical
        # advice and next steps add their exact words. _combined deduplicates
        # identical paragraphs without assigning original-advice authority.
        action = _combined(f'problem.{pid}.action', [*priority_actions.get(pid, []),
            *medical_actions.get(pid, []), *next_actions.get(pid, [])], limits)
        if meaning is not None or action is not None:
            document.analyses.append(ClinicalProblemAnalysis(problem_id=pid,
                significance=meaning, action=action))
    aliases = {'microbiome_analysis': microbiome, 'strategy': strategy,
        'clinical_followup': clinical_followup}
    document.sections = [block for block in document.sections if block.code not in aliases]
    for code, rows in aliases.items():
        block = _combined(code, rows, limits)
        if block is not None:
            document.sections.append(block)
    document.background['sevenChapterFollowupBridge'] = {
        'projectedAliases': [code for code in aliases
            if any(block.code == code for block in document.sections)],
        'projectedProblems': [analysis.problem_id for analysis in document.analyses],
        'compatibilityLimits': limits,
    }
    return document
