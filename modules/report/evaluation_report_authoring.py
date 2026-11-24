"""Template-cell authorship with independent factual and medical review.

Models use a request-local packet; source ownership and literal citations are
checked after decoding. Each semantic cell is written as one finished clinical
paragraph, rather than appending medical footnotes to a source-result listing.
This module returns prose only, never changes source result rows.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from time import monotonic

from pydantic import BaseModel

from .fallback_policy import EVALUATE_MODEL_CONCURRENCY


_ROLES = {'observation', 'interpretation', 'action'}
_GROUP_TARGETS = 6
_ACTION_KINDS = {'test', 'consultation', 'lifestyle', 'alert', 'avoid'}
_INLINE_NUMERIC_CITATION = re.compile(r'[\[【]\s*\d+(?:\s*[,，;；、\-–—]\s*\d+)*\s*[\]】]')
_SYNTHESIS_CHECKS = ('noInventedFindings', 'sourceNaturePreserved',
    'noUnsupportedDiagnosis', 'noCausalUpgrade', 'clinicallyProportionate')
_ACTION_SCOPES = {'routine_lifestyle', 'clinical_coordination', 'recorded_care',
    'novel_treatment', 'medical_prescription'}
_SAFE_UNCITED_ACTION_SCOPES = {'routine_lifestyle', 'clinical_coordination', 'recorded_care'}
_INFERENCE_CLASSES = {'source_observation', 'clinical_integration', 'biomedical_relation'}


def _compact_cell(target):
    contract = target.get('cellContract') or {}
    return (contract.get('form') in {'label', 'classification'}
        or bool(contract.get('allowedValues')) or target.get('contentDuty') == 'classification')

def _event(audit, event):
    if isinstance(audit, dict):
        audit.setdefault('authoring', []).append(deepcopy(event))
    elif isinstance(audit, list):
        audit.append(deepcopy(event))


def _role_review_diagnostics(response, draft):
    rows = response.get('reviews', []) if isinstance(response, dict) else []
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        checks = row.get('claimChecks', [])
        result.append({'targetId': row.get('targetId'),
            'ownerBindingValid': row.get('ownerBindingValid'),
            'ownerFactIds': deepcopy(draft.get(row.get('targetId'), {}).get('ownerFactIds', [])),
            'ownerBindingMode': draft.get(row.get('targetId'), {}).get('ownerBindingMode'),
            'returnedRoles': deepcopy(row.get('roles')),
            'sentenceCount': len(draft.get(row.get('targetId'), {}).get('sentences', [])),
            'draftSentences': deepcopy(draft.get(row.get('targetId'), {}).get('sentences', [])),
            'claimChecks': deepcopy(checks) if isinstance(checks, list) else [],
            'reviewIssues': deepcopy(row.get('issues')),
            'roleValid': [check.get('roleValid') for check in checks if isinstance(check, dict)]
                if isinstance(checks, list) else []})
    return result


def _block_chars(row):
    return len('\n\n'.join(str(sentence.get('text', ''))
        for sentence in row.get('sentences', []) if isinstance(sentence, dict)))


def _compression_spec(row, target):
    """Give the editor an exact inclusive budget without changing the cap."""
    sentences = row.get('sentences', [])
    lengths = [len(str(sentence.get('text', ''))) for sentence in sentences]
    maximum = target['cellContract']['maxChars']
    available = max(0, maximum - 2 * max(0, len(sentences) - 1))
    denominator = max(1, sum(lengths))
    budgets = [available * length // denominator for length in lengths]
    for index in range(available - sum(budgets)):
        if budgets:
            budgets[index % len(budgets)] += 1
    return {'id': row['targetId'], 'cellContract': deepcopy(target['cellContract']),
        'actualChars': _block_chars(row), 'maxCharsIncludingParagraphSeparators': maximum,
        'sentenceBudgets': [{'sentenceIndex': index, 'actualChars': length, 'maxChars': budget}
            for index, (length, budget) in enumerate(zip(lengths, budgets, strict=True))]}


def _fact_id(fact):
    return fact.get('factId', fact.get('fact_id'))


def _source_ids(target):
    return tuple(target.get('sourceFactIds', ()))


def _writing_focus(target, facts, patient=None):
    """Resolve the cell's actual subject before asking for patient prose."""
    owner = target.get('owner') or {}
    contract = target.get('cellContract') or {}
    primary = [fid for fid in owner.get('primaryFactIds', ()) if fid in facts]
    preview = [{key: deepcopy(facts[fid][key]) for key in (
        'factId', 'kind', 'name', 'value', 'unit', 'status', 'date', 'referenceRange')
        if key in facts[fid]} for fid in primary[:8]]
    maximum = contract.get('maxChars')
    compact = _compact_cell(target)
    management = contract.get('analysisMode') == 'management_direction'
    preferred = None
    if type(maximum) is int:
        preferred = maximum if compact else maximum * 4 // 5
        if not compact and not (owner.get('aggregate') and maximum > 240):
            preferred = min(140, max(1, preferred))
    if isinstance(patient, BaseModel):
        patient = patient.model_dump(mode='json', by_alias=True)
    patient = patient if isinstance(patient, dict) else {}
    identity = {key: deepcopy(patient[key]) for key in ('sex', 'gender', 'age')
        if key in patient and patient[key] is not None}
    interpretation_steps = (
        '先回答本栏的个人健康问题，明确主要发现对本人意味着什么。',
        '有主诉时只用已记录的病程或触发场景。区分支持临床判断的客观发现和辅助性的研究关联。',
        '同一组发现可以支持共同管理。只有每一步都有适用依据时，才写为连续的医学机制。',
        '同向资料不足时保留独立成立的判断与处理目的。内容可长可短，按实际事实深度决定。',
    )
    return {'subject': owner.get('name'), 'sourceNature': owner.get('measurementType'),
        'patientContext': identity,
        'certainty': owner.get('certainty'), 'column': contract.get('label'),
        'question': contract.get('question'), 'primaryFactIds': primary,
        'primaryFactPreview': preview, 'preferredChars': preferred,
        'interpretationSteps': list(interpretation_steps) if not compact and not management else [],
        'sentencePlan': ('只写指定短语或分级' if compact else
            '一至两句直接给具体管理方向；临床归纳句只用action，不先复述结果或宣告本人功能状态' if management else
            '用一个完整短段统领本组问题及改善次序，不重复细表' if owner.get('aggregate') and type(maximum) is int and maximum > 240
            else '一至两句，先给本栏结论，再给最有用的行动'),
        'anchorRequirement': ('用块级ownerFactIds明确本栏的实际检测对象；句级factIds只列真正支持行动的患者事实，不能把检测对象伪装成个人病因。对象绑定由独审另外核验。'
            if management else '至少选一条primaryFactIds中真实支持本段的事实；习惯仅作个人化联系，不能代替本对象。不要为通过地址校验挂无关事实。'),
        'divisionOfWork': '原值、参考范围和检测状态由相邻结果列展示；本栏交付健康意义与行动，不再抄数值清单。类别段只管理本类别实际对象，不借其他类别问题填充。'}


def _analysis_support_issue(target, role, support):
    contract = target.get('cellContract') or {}
    if _compact_cell(target):
        return None
    if contract.get('form') == 'paragraph' and support == 'source':
        return 'paragraph_requires_synthesis_or_cited_meaning'
    if contract.get('analysisMode') != 'management_direction':
        return None
    if support == 'source':
        return 'management_direction_cannot_be_source_restatement'
    if support == 'clinical_synthesis' and role != 'action':
        return 'management_direction_synthesis_requires_action'
    return None


def reviewed_management_owner_binding(block, target, facts):
    """Recheck the reviewed object identity at the publication boundary."""
    owner_ids = block.get('ownerFactIds')
    primary = set((target.get('owner') or {}).get('primaryFactIds') or ())
    sentences = block.get('sentences')
    return bool(block.get('ownerBindingMode') == 'object_identity'
        and block.get('ownerBindingValid') is True
        and (target.get('cellContract') or {}).get('analysisMode') == 'management_direction'
        and isinstance(owner_ids, list) and owner_ids
        and all(isinstance(fid, str) and fid in primary and fid in facts for fid in owner_ids)
        and isinstance(sentences, list) and sentences
        and all(isinstance(sentence, dict) and sentence.get('role') == 'action'
            and sentence.get('support') == 'clinical_synthesis' for sentence in sentences))


def _analysis_repair_direction(target):
    if (target.get('cellContract') or {}).get('analysisMode') == 'management_direction':
        return ('直接写与本对象及真实个人情况相称的管理行动，clinical_synthesis只用action；'
            '结果和状态留在数据列。确需机制解释时单独使用获准原段，不把预测状态当本人的功能或疾病状态。')
    return ('删除结果复述，重新形成与本对象相称的结论或管理行动；事实放在相邻结果列。'
        '临床归纳使用clinical_synthesis，新增医学机制使用实际获准原段；不要只改support标签保留无支持的命题。')


def _repair_claim_diagnostics(response, draft, rejected):
    """Return the reviewer's actual disputed clauses, not a generic retry."""
    diagnostics = {}
    rows = response.get('reviews', ()) if isinstance(response, dict) else ()
    for review in rows if isinstance(rows, list) else ():
        if not isinstance(review, dict) or review.get('targetId') not in rejected:
            continue
        tid = review['targetId']
        sentences = draft.get(tid, {}).get('sentences', ())
        checks = review.get('claimChecks')
        disputed = []
        if isinstance(checks, list):
            for index, (sentence, check) in enumerate(zip(sentences, checks)):
                if not isinstance(check, dict):
                    continue
                if (check.get('uncoveredClaims') or any(check.get(key) is False for key in (
                        'patientConditionsSatisfied', 'noUnsupportedRemainder',
                        'sourceNaturePreserved', 'noInventedFindings',
                        'noUnsupportedDiagnosis', 'noCausalUpgrade', 'clinicallyProportionate'))):
                    disputed.append({'sentenceIndex': index, 'text': sentence.get('text'),
                        'support': sentence.get('support'),
                        'uncoveredClaims': deepcopy(check.get('uncoveredClaims') or []),
                        'reviewReason': check.get('reasoningBasis')})
        diagnostics[tid] = {'error': 'independent_review_rejected',
            'issues': deepcopy(review.get('issues') or rejected[tid]),
            'disputedSentences': disputed,
            'repairDirection': '删除不受支持的分句，按本对象真实情况重写完整短段；已有问题与新建议分别使用适合的support。不要把病因猜测改成确定结论，也不要加一段限制说明来保留错误命题。'}
    return diagnostics


def _records(evidence):
    rows = evidence.get('summaries', []) if isinstance(evidence, dict) else getattr(evidence, 'summaries', [])
    result = {}
    counts = Counter(row.get('number') for row in rows if isinstance(row, dict))
    for row in rows:
        if (isinstance(row, dict) and type(row.get('number')) is int and row['number'] > 0
                and counts[row['number']] == 1 and isinstance(row.get('sourceText'), str)
                and row['sourceText'].strip()):
            result[row['number']] = row
    return result


def _related(record, chapter, target):
    if _direct_target(record, target):
        return True
    if record.get('chapterCodes'):
        return chapter['code'] in record['chapterCodes']
    return bool(set(record.get('sourceFactIds', ())) & set(_source_ids(target)))


def _direct_target(record, target):
    """Retrieval coverage priority, never a medical approval permission."""
    return target['id'] in record.get('targetIds', ())


def _optional_action_details(details, sentences, *, audit=None, target_id=None, roles=None):
    """Keep only literal private action annotations, never suppress prose.

    The complete paragraph still undergoes factual and medical review. These
    optional navigation annotations cannot authorize or change an action.
    """
    accepted = []
    rows = details if isinstance(details, list) else []
    if not isinstance(details, list):
        _event(audit, {'stage': 'optional_action_metadata_discarded',
            'targetId': target_id, 'reason': 'invalid_action_details'})
    for position, detail in enumerate(rows):
        reason = None
        if not isinstance(detail, dict):
            reason = 'invalid_action_detail'
        else:
            kind, name, timeframe, indices = (detail.get(key)
                for key in ('kind', 'name', 'timeframe', 'sentenceIndices'))
            if (not isinstance(kind, str) or kind not in _ACTION_KINDS
                    or not isinstance(name, str) or not name.strip()
                    or not isinstance(indices, list) or not indices
                    or any(type(index) is not int or not 0 <= index < len(sentences) for index in indices)
                    or len(set(indices)) != len(indices)):
                reason = 'invalid_action_detail'
            else:
                texts = [sentences[index].get('text') for index in indices]
                actual_roles = roles if isinstance(roles, list) else [row.get('role') for row in sentences]
                if any(actual_roles[index] != 'action' for index in indices):
                    reason = 'action_detail_requires_reviewed_action_sentence'
                elif (any(not isinstance(text, str) for text in texts)
                        or not any(name in text for text in texts)
                        or (timeframe is not None and (not isinstance(timeframe, str)
                            or not timeframe.strip() or not any(timeframe in text for text in texts)))):
                    reason = 'action_detail_not_literal'
        if reason:
            _event(audit, {'stage': 'optional_action_metadata_discarded',
                'targetId': target_id, 'detailIndex': position, 'reason': reason})
        else:
            accepted.append(deepcopy(detail))
    return accepted


def _validate_blocks(raw, targets, facts, records, chapter, audit=None, diagnostics=None):
    """Check addresses and literal witnesses, not a medical meaning label."""
    rows = raw.get('blocks', []) if isinstance(raw, dict) else []
    rows = rows if isinstance(rows, list) else []
    counts = Counter(row['targetId'] for row in rows if isinstance(row, dict)
        and isinstance(row.get('targetId'), str))
    valid, errors = {}, {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('targetId'), str):
            continue
        tid = row['targetId']
        if tid not in targets:
            continue
        target, failures, parsed = targets[tid], [], []
        if counts[tid] != 1:
            failures.append('duplicate_target')
        sentences = row.get('sentences')
        if not isinstance(sentences, list) or not sentences:
            failures.append('missing_sentences')
            sentences = []
        allowed = set(_source_ids(target)) & facts.keys()
        for sentence_index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                failures.append('invalid_sentence')
                continue
            text, identifiers, role = (sentence.get(key) for key in ('text', 'factIds', 'role'))
            if not isinstance(text, str) or not text.strip() or text != text.strip():
                failures.append('missing_or_untrimmed_text')
            elif _INLINE_NUMERIC_CITATION.search(text):
                failures.append('inline_numeric_citation_use_evidence_field')
            if (not isinstance(identifiers, list) or not identifiers
                    or any(not isinstance(fid, str) for fid in identifiers)
                    or len(set(identifiers)) != len(identifiers)
                    or not set(identifiers) <= allowed):
                failures.append('fact_scope_mismatch')
                identifiers = []
            # A valid author label is transport metadata. The independent
            # content reviewer may correct it into the target's allowed role.
            if not isinstance(role, str) or role not in _ROLES:
                failures.append('invalid_role')
            citations = sentence.get('evidence', [])
            if not isinstance(citations, list):
                failures.append('invalid_evidence')
                citations = []
            for citation in citations:
                number = citation.get('number') if isinstance(citation, dict) else None
                quotes = citation.get('quotes') if isinstance(citation, dict) else None
                record = records.get(number) if type(number) is int else None
                if (record is None or not _related(record, chapter, target)
                        or not isinstance(quotes, list) or not quotes
                        or any(not isinstance(quote, str) or not quote.strip()
                            or quote not in record['sourceText'] for quote in quotes)):
                    failures.append('citation_original_or_target_mismatch')
            support = sentence.get('support') or ('evidence' if citations else 'source')
            direction_issue = _analysis_support_issue(target, role, support)
            if direction_issue:
                failures.append(direction_issue)
                if isinstance(diagnostics, dict):
                    diagnostics.setdefault(tid, []).append({'error': direction_issue,
                        'sentenceIndex': sentence_index, 'text': text,
                        'requestedRole': role, 'support': support,
                        'repairDirection': _analysis_repair_direction(target)})
            parsed.append({'text': text, 'factIds': deepcopy(identifiers),
                'role': role, 'evidence': deepcopy(citations),
                'support': support})
        primary = set(target.get('owner', {}).get('primaryFactIds') or ())
        used = {fid for sentence in parsed for fid in sentence['factIds']}
        owner_ids = row.get('ownerFactIds', [])
        direction_only = ((target.get('cellContract') or {}).get('analysisMode') == 'management_direction'
            and bool(parsed) and all(sentence['role'] == 'action'
                and sentence['support'] == 'clinical_synthesis' for sentence in parsed))
        explicit_owner = (direction_only and isinstance(owner_ids, list) and bool(owner_ids)
            and all(isinstance(fid, str) and fid in facts and fid in primary for fid in owner_ids))
        if primary and not primary.intersection(used):
            if not explicit_owner:
                failures.append('primary_owner_fact_missing')
        contract = target.get('cellContract') or {}
        text = '\n\n'.join(sentence['text'] for sentence in parsed
            if isinstance(sentence.get('text'), str))
        if contract.get('allowedValues') and text not in contract['allowedValues']:
            failures.append('template_cell_value_not_allowed')
        if type(contract.get('maxChars')) is int and len(text) > contract['maxChars']:
            failures.append('template_cell_length_exceeded')
        details = _optional_action_details(row.get('actionDetails', []), parsed,
            audit=audit, target_id=tid)
        if failures:
            errors[tid] = list(dict.fromkeys(failures))
        else:
            valid[tid] = {'targetId': tid, 'sentences': parsed,
                **({'ownerFactIds': deepcopy(owner_ids), 'ownerBindingMode': 'object_identity'}
                    if explicit_owner and not primary.intersection(used) else {}),
                **({'actionDetails': deepcopy(details)} if details else {})}
    for tid, target in targets.items():
        if target.get('required') and tid not in valid and tid not in errors:
            errors[tid] = ['required_target_missing']
    return valid, errors


def _accepted_blocks(response, draft, targets, audit=None):
    """Accept a complete cell only after an independent per-claim review.

    Clinical integration and priority decisions are reviewed as reasoning from
    actual facts, rather than pretending a paper states this patient's order
    of care. New biomedical claims retain their own original-text obligations.
    """
    reviews = response.get('reviews', []) if isinstance(response, dict) else []
    reviews = reviews if isinstance(reviews, list) else []
    grouped = {}
    for review in reviews:
        if isinstance(review, dict) and isinstance(review.get('targetId'), str):
            grouped.setdefault(review['targetId'], []).append(review)
    accepted, errors = {}, {}
    for tid, block in draft.items():
        matches = grouped.get(tid, [])
        if len(matches) != 1:
            errors[tid] = ['independent_review_missing_or_duplicate']
            continue
        review = matches[0]
        if review.get('verdict') != 'pass' or review.get('issues') != []:
            errors[tid] = (review.get('issues') if isinstance(review.get('issues'), list)
                and review['issues'] else ['independent_review_not_passed'])
            continue
        support, completion, roles = (review.get(key) for key in ('support', 'completion', 'roles'))
        template_cell = bool(targets[tid].get('cellContract'))
        failures = []
        if (block.get('ownerBindingMode') == 'object_identity'
                and review.get('ownerBindingValid') is not True):
            failures.append('management_object_binding_not_independently_confirmed')
        if support not in {'source', 'evidence', 'clinical_synthesis', 'applicable_guideline', 'mixed'}:
            failures.append('review_support_missing')
        if completion != 'complete':
            failures.append('review_completion_missing')
        if template_cell:
            # The writer already supplies one validated role per sentence.
            # The reviewer checks its meaning, without echoing a parallel array
            # which can lose positions or confuse support with sentence role.
            roles = [sentence.get('role') for sentence in block['sentences']]
        if (not isinstance(roles, list) or len(roles) != len(block['sentences'])
                or any(not isinstance(role, str) or role not in targets[tid].get('allowedRoles', _ROLES) for role in roles)):
            failures.append('reviewed_roles_invalid')
        cited = any(sentence['evidence'] for sentence in block['sentences'])
        reasoned = any(sentence.get('support') == 'clinical_synthesis'
            for sentence in block['sentences'])
        if (support in ('evidence', 'applicable_guideline') or support == 'mixed' and not reasoned) and not cited:
            failures.append('reviewed_evidence_has_no_literal_citation')
        if support == 'source' and cited:
            failures.append('source_review_cannot_discard_cited_medical_claim')
        checks = review.get('claimChecks')
        if not isinstance(checks, list) or len(checks) != len(block['sentences']):
            failures.append('sentence_support_review_missing')
        else:
            for sentence_index, (sentence, check) in enumerate(zip(block['sentences'], checks, strict=True)):
                direction_issue = _analysis_support_issue(targets[tid], sentence.get('role'), sentence.get('support'))
                if direction_issue:
                    failures.append(direction_issue)
                    continue
                if isinstance(check, dict) and 'sourceFactsDirectlySupportsClaim' in check:
                    canonical, alias = 'sourceFactsDirectlySupportClaim', 'sourceFactsDirectlySupportsClaim'
                    if canonical in check and (type(check[canonical]) is not bool
                            or type(check[alias]) is not bool or check[canonical] != check[alias]):
                        failures.append('review_field_alias_conflict')
                        _event(audit, {'stage': 'review_field_alias_conflict',
                            'targetId': tid, 'sentenceIndex': sentence_index, 'field': canonical})
                        continue
                    if canonical not in check and type(check[alias]) is bool:
                        check = {**check, canonical: check[alias]}
                        _event(audit, {'stage': 'review_field_alias_normalized',
                            'targetId': tid, 'sentenceIndex': sentence_index,
                            'from': alias, 'to': canonical})
                if (not isinstance(check, dict)
                        or check.get('patientConditionsSatisfied') is not True
                        or check.get('noUnsupportedRemainder') is not True):
                    failures.append('patient_or_claim_support_not_confirmed')
                elif isinstance(check.get('uncoveredClaims'), list) and check['uncoveredClaims']:
                    failures.append('uncovered_claims_in_sentence')
                    failures.extend('unsupported_fragment: ' + str(piece)[:160]
                        for piece in check['uncoveredClaims'])
                elif template_cell and check.get('roleValid') is not True:
                    failures.append('sentence_role_not_independently_confirmed')
                elif template_cell and check.get('inferenceClass') not in _INFERENCE_CLASSES:
                    failures.append('sentence_inference_class_not_confirmed')
                elif (check.get('inferenceClass') == 'biomedical_relation'
                        and (sentence.get('support') not in {'evidence', 'applicable_guideline'}
                            or not sentence['evidence']
                            or check.get('originalDirectlySupportsClaim') is not True)):
                    failures.append('biomedical_relation_requires_applicable_original')
                elif sentence.get('support') in {'clinical_synthesis', 'applicable_guideline'}:
                    if any(check.get(key) is not True for key in _SYNTHESIS_CHECKS):
                        failures.append('clinical_synthesis_boundaries_not_confirmed')
                    if (not isinstance(check.get('reasoningBasis'), str)
                            or not check['reasoningBasis'].strip()):
                        failures.append('clinical_reasoning_basis_missing')
                    if sentence.get('support') == 'applicable_guideline':
                        if (not sentence['evidence']
                                or check.get('applicableGuidelineSupportsDirection') is not True
                                or check.get('patientAdaptationSupported') is not True):
                            failures.append('guideline_adaptation_not_supported')
                    elif sentence.get('role') == 'action':
                        accepted_action = (check.get('actionScope') in _SAFE_UNCITED_ACTION_SCOPES
                            and isinstance(check.get('actionScopeReason'), str)
                            and bool(check['actionScopeReason'].strip()))
                        if not accepted_action:
                            failures.append('uncited_action_exceeds_low_risk_clinical_scope')
                elif sentence['evidence']:
                    if check.get('originalDirectlySupportsClaim') is not True:
                        failures.append('original_support_not_confirmed')
                elif (sentence.get('role') == 'action'
                        or check.get('sourceFactsDirectlySupportClaim') is not True
                        or check.get('noNewMedicalProposition') is not True):
                    failures.append('uncited_sentence_is_not_direct_fact_synthesis')
        duty_checks = ('cellContractSatisfied', 'noProcessText')
        if not _compact_cell(targets[tid]):
            duty_checks += ('notRawResultRestatement',)
        if targets[tid].get('cellContract') and any(review.get(key) is not True for key in duty_checks):
            failures.append('template_cell_duty_not_satisfied')
        if failures:
            errors[tid] = failures
            continue
        sentences = deepcopy(block['sentences'])
        for sentence, role in zip(sentences, roles, strict=True):
            sentence['role'] = role
        details = _optional_action_details(block.get('actionDetails', []), sentences,
            audit=audit, target_id=tid, roles=roles)
        accepted[tid] = {'targetId': tid, 'completion': completion, 'sentences': sentences,
            **({'ownerFactIds': deepcopy(block['ownerFactIds']),
                'ownerBindingMode': 'object_identity', 'ownerBindingValid': True}
                if block.get('ownerBindingMode') == 'object_identity' else {}),
            **({'actionDetails': details} if details else {})}
    return accepted, errors


def _medical_roles(target, chapter):
    path = target.get('path', ())
    if not path or target.get('contentDuty') == 'source_state':
        return set()
    return set(target.get('allowedRoles', ('interpretation',))) & {'interpretation', 'action'}


def _medical_role(target, chapter):
    """Compatibility helper; chapter number never grants authorship."""
    roles = _medical_roles(target, chapter)
    return 'interpretation' if 'interpretation' in roles else 'action' if 'action' in roles else None


def _passage_library(records, chapter, targets):
    """Only explicitly selected same-owner source passages reach authors.

    The source selector grants candidate eligibility, never clinical approval.
    The writer cannot supply its own shortened quotes or fabricate an evidence
    number: citations are materialized from these exact server-held passages.
    """
    from .evaluation_report_evidence import report_source_packet
    def roles_for(link, target):
        owner = target.get('owner', {})
        eligible = _medical_roles(target, chapter).intersection(link.get('eligibleRoles', ()))
        primary = set(link.get('sourceFactIds', ())) & set(owner.get('primaryFactIds', ()))
        if (not owner.get('aggregate') and owner.get('kind') in {
                'clinical', 'clinical_observation', 'focus', 'goal', 'clinical_decision'}):
            codes = {value for value in [owner.get('code'), *(owner.get('sourceOwnerCodes') or ())]
                if isinstance(value, str) and value}
            owned = link.get('ownerCode') in codes and bool(primary)
        else:
            owned = bool(primary or (not owner.get('primaryFactIds') and
                set(link.get('sourceFactIds', ())).intersection(target.get('sourceFactIds', ()))))
        if owned:
            return eligible
        # A clinical guideline can support an action for this actual patient,
        # without becoming proof of a separate microbial/function mechanism.
        context = set(owner.get('managementContextFactIds') or ())
        if 'action' in eligible and context.intersection(link.get('sourceFactIds', ())):
            return {'action'}
        return set()
    choices = []
    for record in records.values():
        links = [link for link in record.get('topicLinks', ())
            if any(tid in link.get('targetIds', ())
                and roles_for(link, target)
                for tid, target in targets.items())]
        if links:
            choices.append((record, links))
    # Each owner gets a turn before extra publications consume the budget.
    ordered, seen = [], set()
    for tid in targets:
        for record, links in choices:
            if record['number'] not in seen and any(tid in link.get('targetIds', ()) for link in links):
                ordered.append((record, links))
                seen.add(record['number'])
                break
    ordered.extend((record, links) for record, links in choices if record['number'] not in seen)
    library, used, budget = {}, set(), 24000
    for record, links in ordered[:8]:
        quotes = list(dict.fromkeys(quote for link in links for quote in link.get('selectedQuotes', ())))
        # New selector links carry their actual admitted literal objects. A
        # 2600-char selector paragraph must not be reconstructed at 12000 chars
        # and acquire a different offset/ID before authors can cite it.
        from .evaluation_report_evidence import _complete_passages
        from .evaluation_source_scope import source_scope_options
        full = record['sourceText']
        digest = hashlib.sha256(full.encode()).hexdigest()
        passages, related_by_id = {}, {}
        for link in links:
            if 'selectedOriginalPassages' not in link:
                # Only historical links use the old reconstruction path.
                view = report_source_packet(record, chapter_code=chapter['code'],
                    quotes=link.get('selectedQuotes', ()), max_chars=12000)
                candidates = view.get('passages', ())
            else:
                candidates = []
                stored = link['selectedOriginalPassages']
                for original in stored if isinstance(stored, list) else ():
                    if not isinstance(original, dict):
                        continue
                    identity = original.get('id', original.get('passageId'))
                    text, start, end = (original.get(key) for key in ('text', 'start', 'end'))
                    if (identity not in link.get('selectedPassageIds', ())
                            or text not in link.get('selectedQuotes', ())
                            or original.get('completeSentences') is not True
                            or original.get('fullSourceSha256') != digest
                            or type(start) is not int or type(end) is not int
                            or not 0 <= start < end <= len(full) or full[start:end] != text):
                        continue
                    complete = _complete_passages(full, [{'start': start, 'end': end}])
                    if (len(complete) != 1 or complete[0]['passageId'] != identity
                            or complete[0]['text'] != text):
                        continue
                    candidates.append({'id': identity, **complete[0]})
            for passage in candidates:
                identity = passage.get('id', passage.get('passageId'))
                text = passage.get('text')
                if (not isinstance(identity, str) or not isinstance(text, str) or not text.strip()
                        or passage.get('completeSentences') is not True or text not in full
                        or identity not in link.get('selectedPassageIds', ())):
                    continue
                passages[identity] = passage
                related_by_id.setdefault(identity, []).append(link)
        for identity, passage in passages.items():
            text = passage['text']
            if (record['number'], identity) in used:
                continue
            related = related_by_id[identity]
            allowed = {tid: sorted({role for link in related
                if tid in link.get('targetIds', ())
                for role in roles_for(link, target)})
                for tid, target in targets.items()}
            allowed = {tid: roles for tid, roles in allowed.items() if roles}
            if not allowed:
                continue
            # Derive contextual scope again from the unchanged server original,
            # never grant permission from a model-supplied/stale scope field.
            scope = source_scope_options(full, record.get('title') or '', selected_quotes=(text,))
            item = {'id': f'P{len(library) + 1:03d}', 'publicationNumber': record['number'],
                'title': record.get('title'), 'year': record.get('year'), 'doi': record.get('doi'),
                'text': text, 'allowedTargets': allowed, 'sourceScopeOptions': scope,
                'sourcePassageId': identity, 'governingContextStatus': 'requires_independent_review',
                'candidateUses': list(dict.fromkeys(link.get('candidateUse') for link in related))}
            size = len(json.dumps(item, ensure_ascii=False))
            if size > budget:
                continue
            library[item['id']] = item
            budget -= size
            used.add((record['number'], identity))

    return library


def _materialize_medical_draft(raw, targets, library, diagnostics=None):
    """Bind cited whole passages; directly recorded facts remain separate."""
    result, errors = [], {}
    rows = raw.get('blocks', ()) if isinstance(raw, dict) else ()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get('targetId') not in targets:
            continue
        tid, sentences, issues = row['targetId'], [], []
        for sentence_index, sentence in enumerate(row.get('sentences', ())):
            if not isinstance(sentence, dict):
                issues.append('invalid_medical_sentence')
                continue
            identifiers = sentence.get('passageIds', [])
            role = sentence.get('role')
            support = sentence.get('support') or ('evidence' if identifiers else 'source')
            if (not isinstance(identifiers, list)
                    or any(not isinstance(pid, str) or pid not in library for pid in identifiers)
                    or len(set(identifiers)) != len(identifiers)
                    or role not in _ROLES
                    or support not in {'source', 'evidence', 'clinical_synthesis', 'applicable_guideline'}):
                issues.append('invalid_sentence_provenance')
                continue
            if support in {'evidence', 'applicable_guideline'} and (not identifiers or role == 'observation'):
                issues.append('medical_claim_requires_given_passages')
                continue
            if support == 'source' and (identifiers or role == 'action'):
                issues.append('uncited_source_cannot_grant_new_action')
                continue
            if support == 'clinical_synthesis' and identifiers:
                issues.append('cited_synthesis_requires_explicit_evidence_or_guideline_support')
                continue
            direction_issue = _analysis_support_issue(targets[tid], role, support)
            if direction_issue:
                issues.append(direction_issue)
                if isinstance(diagnostics, dict):
                    diagnostics.setdefault(tid, []).append({'error': direction_issue,
                        'sentenceIndex': sentence_index, 'text': sentence.get('text'),
                        'requestedRole': role, 'support': support,
                        'repairDirection': _analysis_repair_direction(targets[tid])})
                continue
            references = {}
            for pid in identifiers:
                passage = library[pid]
                if role not in passage['allowedTargets'].get(tid, ()):
                    issues.append('passage_role_or_owner_mismatch')
                    if isinstance(diagnostics, dict):
                        diagnostics.setdefault(tid, []).append({
                            'error': 'passage_role_or_owner_mismatch',
                            'sentenceIndex': sentence_index, 'text': sentence.get('text'),
                            'requestedRole': role, 'support': support, 'passageId': pid,
                            'allowedRolesForThisTarget': deepcopy(passage['allowedTargets'].get(tid, [])),
                            'allowedPassageIdsByRole': {candidate_role: [candidate_id
                                for candidate_id, candidate in library.items()
                                if candidate_role in candidate['allowedTargets'].get(tid, ())]
                                for candidate_role in ('interpretation', 'action')},
                            'repairDirection': '保留原文授权角色；拆成有引文的解释和无引文的常规协调时，后者须独立符合clinical_synthesis的临床边界。不得改原段权限或把新医学命题改名绕过证据。'})
                    continue
                references.setdefault(passage['publicationNumber'], []).append(passage['text'])
            sentences.append({'text': sentence.get('text'), 'factIds': deepcopy(sentence.get('factIds')),
                'role': role, 'support': support,
                'evidence': [{'number': number, 'quotes': list(dict.fromkeys(quotes))}
                    for number, quotes in references.items()]})
        if issues:
            errors[tid] = list(dict.fromkeys(issues))
        else:
            result.append({'targetId': tid, 'sentences': sentences,
                'ownerFactIds': deepcopy(row.get('ownerFactIds', [])),
                'actionDetails': deepcopy(row.get('actionDetails', []))})
    return {'blocks': result}, errors


def _merge_medical_addition(source, medical):
    """Compatibility name: the reviewed cell replaces source prose in full.

    Source truth is retained in immutable result columns and canonical facts;
    retaining a result-list paragraph in a meaning cell is not source fidelity.
    """
    return {**deepcopy(medical), 'medicalGenerated': True, 'analysisGenerated': True}


def _reviewed_context(previous, targets):
    """Supply compact accepted paragraphs, never unreviewed source filler."""
    current = set(targets)
    result = {}
    for code, blocks in previous.items():
        selected = []
        for block in blocks:
            if (block.get('targetId') in current or not (
                    block.get('analysisGenerated') or block.get('medicalGenerated'))):
                continue
            selected.append({'targetId': block['targetId'], 'sentences': [
                {'text': sentence['text'], 'factIds': deepcopy(sentence['factIds']),
                 'role': sentence['role'], 'evidenceNumbers': [citation['number']
                    for citation in sentence.get('evidence', ())]}
                for sentence in block.get('sentences', ())]})
        if selected:
            result[code] = selected
    return result


async def author_report_sections(client, *, blueprint, evidence, subject, deadline, audit=None,
                                 previous_reviewed=None):
    """Write and review complete template cells against their own duties.

    All chapters use the same semantic-cell contract. Direct fact synthesis and
    external medical propositions have distinct per-sentence proof obligations.
    Failed cells stay unresolved instead of publishing result-list filler.
    """
    from .evaluation_report_packet import build_chapter_packet

    facts = {_fact_id(f): f for f in blueprint.facts if isinstance(f, dict) and _fact_id(f)}
    records = _records(evidence)
    result = deepcopy(previous_reviewed or {})
    journal = audit if isinstance(audit, dict) else {}
    selected_ids = {target['id'] for chapter in blueprint.chapters for target in chapter['targets']
        if _medical_role(target, chapter)}
    journal.setdefault('medicalUnresolved', [])
    journal['medicalUnresolved'] = [row for row in journal['medicalUnresolved']
        if row.get('targetId') not in selected_ids]
    gate = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    buckets = {code: {block['targetId']: block for block in blocks} for code, blocks in result.items()}
    for chapter in blueprint.chapters:
        bucket = buckets.setdefault(chapter['code'], {})
        for target in chapter['targets']:
            existing = bucket.get(target['id'], {})
            if _medical_role(target, chapter) and not (
                    existing.get('analysisGenerated') or existing.get('medicalGenerated')):
                bucket.pop(target['id'], None)
        result[chapter['code']] = list(bucket.values())
    journal['reviewedBlocks'] = deepcopy(result)
    prior_context = _reviewed_context(result, selected_ids)

    async def generate(packet, base, additions):
        payload = deepcopy(base) | packet.encode(additions)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        async with gate:
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('report_template_authoring_deadline')
            async with asyncio.timeout(remaining):
                response = await client.generate(payload)
        if not isinstance(response, dict):
            raise ValueError('template_response_must_be_object')
        rows = response.get('blocks', [])
        review_rows = response.get('reviews', [])
        _event(audit, {'chapterCode': base.get('chapter', {}).get('code'),
            'stage': 'model_response_addressing', 'task': payload.get('task'),
            'requestedTargetIds': [target.get('id') for target in payload.get('targets', ())
                if isinstance(target, dict)],
            'blocksCount': len(rows) if isinstance(rows, list) else None,
            'responseTargetIds': [row.get('targetId') for row in rows if isinstance(row, dict)]
                if isinstance(rows, list) else [],
            'reviewTargetIds': [row.get('targetId') for row in review_rows if isinstance(row, dict)]
                if isinstance(review_rows, list) else []})
        return packet.decode(response), len(encoded)

    async def group_pass(chapter, group, index):
        targets = {target['id']: target for target in group}
        library = _passage_library(records, chapter, targets)
        accepted, failures = {}, {}
        previous_drafts = {}
        repair_diagnostics = {}
        for attempt in range(2):
            active = {tid: target for tid, target in targets.items() if tid not in accepted}
            if not active:
                break
            packet = build_chapter_packet(blueprint, chapter, list(active.values()), subject, prior_context)
            base = deepcopy(packet.payload)
            patient_context = {}
            for value in (subject, base.get('subject'), base.get('patientContext')):
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode='json', by_alias=True)
                if isinstance(value, dict):
                    patient_context.update({key: deepcopy(value[key]) for key in ('sex', 'gender', 'age')
                        if key in value and value[key] is not None})
            base['sourceMeasurementsAreReadOnly'] = True
            visible = [{**passage, 'allowedTargets': {tid: roles for tid, roles
                in passage['allowedTargets'].items() if tid in active}}
                for passage in library.values() if any(tid in active for tid in passage['allowedTargets'])]
            base['originalPassages'] = packet.encode(visible)
            for model_target in base['targets']:
                tid = packet.decode(model_target['id'])
                model_target['writingFocus'] = packet.encode(_writing_focus(active[tid], facts, patient_context))
                model_target['allowedPassageIdsByRole'] = {role: [pid for pid, passage in library.items()
                    if role in passage['allowedTargets'].get(tid, ())]
                    for role in ('interpretation', 'action')}
            base['rules'] = [
                '按每个target.cellContract完成整个单元格或总结段落，不能在程序复述段后追加一句。表中原值/状态保持只读，意义栏只交付临床归纳、影响与改善重点。',
                '普通表格只写1至2句短段；结构是语义目标，可一句整合，不是必须拆成五句。根据question选择最有用的结论、联系和做法，不逐项复述指标。',
                'cellContract.form为paragraph的分析单元格不使用support=source独立复述结果；原始事实在相邻结果列。本段用clinical_synthesis交付相称结论/行动，确需新增医学机制才用实际获准原段。分类、短标签按各自职责处理。',
                '先按writingFocus确认本栏主体、问题性质、主事实和职责；通常每段至少用一条真正支持本段的主事实。management_direction全为clinical_synthesis/action时，可用块级ownerFactIds绑定实际检测对象，句级factIds只取真实支持行动的资料，不把检测对象强塞为个人病因。矩阵写本类主要事项与安排，具体频次和数值留给明细及行动表。',
                'writingFocus.patientContext的性别和年龄适用于本target每一句话；不把其他性别或年龄人群的生理阶段、复查时机和病史套用给本人，未提供的生理阶段与病史不补造。',
                'cellContract.analysisMode为management_direction时，直接给与本对象及本人情况相称的管理方向；clinical_synthesis只写role=action，source结果复述不充当分析。确需机制解释时，单独使用获准原段的evidence句。不要从功能预测推出本人的炎症、免疫、衰老或营养状态，也不要把这种推断放进行动的前提。',
                '轻微孤立偏离及局部维护项目可用一句相称行动完成，不为凑结构扩出病因猜测、疾病风险或三段排除说明；已有口腔清洁等维护问题就直接给对应维护方向，不需要硬造新的疾病风险。',
                '普通数据列承载数值，分析列说明健康意义；不以“发现A，所以A异常，建议关注A”循环，不以几个正常几个异常充当综合评价，不生造无证据风险等级。',
                '全文用第二人称或客观书面语；先说结论，后说与本人的联系和可以采取的改善方向。行内关注本对象，章内总结整组影响，避免和相邻列/段重复。',
                '每个target必须完整回答question，不因没有paper省略整组单元格；不需要新机制时用clinical_synthesis归纳真实问题、同病整合、相互联系和处理先后顺序，不能只把原值重抄。',
                '每句标support：source=原记录的直接概括；clinical_synthesis=根据真实事实/已审内容进行适度临床推理；evidence=具体新增医学命题；applicable_guideline=把所给通用推荐按本人实际情况落实。',
                'clinical_synthesis可解释真实问题与个人习惯的联系、归纳管理重点和安排优先级，并给相称的低风险日常调整和就诊协调，不要求论文逐字写出哪个人应先处理哪一项；不得据此创造确诊、升级研究相关为因果、把菌群预测当人体缺乏、开药补剂处方或承诺疗效。',
                'evidence/applicable_guideline必须有给定passageIds，保真实对象/人群/条件/方向；适用指南支持行动方向，患者事实支持具体落实和排序，不机械要求原文逐字重复个人安排。clinical_synthesis/source的passageIds返回空数组。',
                '引用只能选本target.allowedPassageIdsByRole中与本句role一致的P编号，不能借同组另一target的引文。没有适用原文时可作有据临床归纳，不把新机制或病因藏进clinical_synthesis。',
                '同一段可包含有据interpretation和action，按target.allowedRoles及原段allowedTargets执行；改善方向可以放在本栏，具体行动表负责次数、时长或可核目标，不四处重复方案。',
                '实际本人、家族、目标、自述、否定、疑似各守原性质；单纯菌群关联不能变成已确诊疾病。序列/功能预测不写成感染、人体缺乏、递质实测或个人病因。',
                'cellContract.form为classification/label时只输出所需汉字或短语，不套段落结构；证据强度按真实关联依据质量评定，可干预性按真实习惯的可调整程度评定，属于栏目判断，不需要原文逐字引用该汉字。',
                '不得生成品牌、产品选择、药物或补剂处方。确无支持的新机制不编造，仍对真实健康问题给有据的临床归纳与相称安排，不用资料缺失或后台规则填空。',
                '不要把F编号、passageId、证据审查、缺项流程、来源规则或“不能用于诊断”等后台说明写入成稿。',
                '成稿直接告诉您当前应重视什么、如何做、希望改善什么。用准确的问题名称、主语和性质保持医学边界，不在各行重复“不能代表、仅是模型、尚未明确、没有记录”等空结论。证据不支持的疾病或病因就不写，不能删去限定后把研究关联升级为确诊。',
            ]
            try:
                raw, chars = await generate(packet, base, {
                    'task': 'author_report_template_cells' if attempt == 0 else 'repair_report_template_cells',
                    'repairFeedback': {tid: values for tid, values in failures.items() if tid in active},
                    'repairDiagnostics': {tid: details for tid, details in repair_diagnostics.items() if tid in active},
                    'previousDrafts': [deepcopy(row) for tid, row in previous_drafts.items() if tid in active],
                    'repairMeasurements': {tid: {'actualChars': sum(len(str(sentence.get('text', '')))
                            for sentence in row.get('sentences', []) if isinstance(sentence, dict))
                            + max(0, len(row.get('sentences', []))-1)*2,
                        'maxChars': active[tid].get('cellContract', {}).get('maxChars'),
                        'sentenceIndicesAreZeroBased': True}
                        for tid, row in previous_drafts.items() if tid in active},
                    'alreadyApprovedTargetIds': list(accepted),
                    'writingBudget': '以每target.writingFocus.preferredChars为首稿篇幅，maxChars为硬上限；矩阵通常60至80字，普通单元格1至2句，全章摘要可用一个完整短段。修复时阅读previousDrafts、repairDiagnostics及actualChars/maxChars，删重复数值、泛化规则和已在别栏展示的方案，不删断医学命题。actionDetails为可选私有导航信息，可以返回空数组；若填写，索引从0开始且只指action句，name/timeframe逐字摘自该句。',
                    'outputSchema': {'blocks': [{'targetId': 'given id', 'ownerFactIds': ['本target的primaryFactIds'], 'sentences': [{
                        'text': '按本单元格职责完成的临床成稿', 'factIds': ['F001'],
                        'role': 'observation|interpretation|action',
                        'support': 'source|clinical_synthesis|evidence|applicable_guideline',
                        'passageIds': []}], 'actionDetails': [{
                        'kind': 'test|consultation|lifestyle|alert|avoid',
                        'name': '动作原文子串', 'timeframe': None, 'sentenceIndices': [0]}]}]}})
                previous_drafts = {row['targetId']: deepcopy(row) for row in raw.get('blocks', [])
                    if isinstance(row, dict) and row.get('targetId') in active}
                oversized = [row for tid, row in previous_drafts.items()
                    if type(active[tid].get('cellContract', {}).get('maxChars')) is int
                    and len('\n\n'.join(str(sentence.get('text', '')) for sentence
                        in row.get('sentences', []) if isinstance(sentence, dict)))
                    > active[tid]['cellContract']['maxChars']]
                if oversized:
                    compact_targets = [_compression_spec(row, active[row['targetId']]) for row in oversized]
                    compact_base = {'chapter': {'code': chapter['code']},
                        'targets': packet.encode(compact_targets),
                        'rules': ['只压缩给定原稿，不增加事实、结论、条件、因果或新建议，不改变主体和确定性。',
                            '保留原句数量及顺序，每句只改text；每句可以删除重复分句、原值清单和后台规则，保留本栏必要结论、真实主体、医学适用条件与相称行动，不截断医学命题。逐句遵守sentenceBudgets，总长度含段落分隔符必须不超过maxCharsIncludingParagraphSeparators。',
                            'actionDetails只是可选私有导航信息，不为保留其文字而扩写正文；原句的医学适用条件、确定性和建议安全边界必须保留，不添加新文献。']}
                    try:
                        edited, editor_chars = await generate(packet, compact_base, {
                            'task': 'compress_report_template_cells', 'blocks': oversized,
                            'outputSchema': {'blocks': [{'targetId': 'given id',
                                'sentences': [{'text': '对应原句的完整压缩句'}]}]}})
                        rewritten = {row.get('targetId'): row for row in edited.get('blocks', [])
                            if isinstance(row, dict)}
                        applied, compression_checks = [], []
                        for row in oversized:
                            replacement = rewritten.get(row['targetId'], {}).get('sentences')
                            if (not isinstance(replacement, list) or len(replacement) != len(row.get('sentences', []))
                                    or any(not isinstance(sentence, dict) or not isinstance(sentence.get('text'), str)
                                        or not sentence['text'].strip() for sentence in replacement)):
                                compression_checks.append({'targetId': row['targetId'],
                                    'reason': 'editor_sentence_shape_mismatch'})
                                continue
                            maximum = active[row['targetId']]['cellContract']['maxChars']
                            candidate_chars = _block_chars({'sentences': replacement})
                            compression_checks.append({'targetId': row['targetId'],
                                'actualChars': candidate_chars, 'maxChars': maximum,
                                'reason': 'within_budget' if candidate_chars <= maximum else 'editor_still_over_budget'})
                            if candidate_chars <= maximum:
                                for original, changed in zip(row['sentences'], replacement, strict=True):
                                    original['text'] = changed['text']
                                applied.append(row['targetId'])
                        raw['blocks'] = [previous_drafts.get(row.get('targetId'), row)
                            if isinstance(row, dict) else row for row in raw.get('blocks', [])]
                        _event(audit, {'chapterCode': chapter['code'], 'group': index,
                            'stage': 'bounded_text_compression', 'targetIds': applied,
                            'attempt': attempt, 'checks': compression_checks,
                            'modelInputChars': editor_chars})
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        _event(audit, {'chapterCode': chapter['code'], 'group': index,
                            'stage': 'bounded_text_compression_unavailable', 'errorType': type(error).__name__})
                repair_diagnostics = {}
                bound, errors = _materialize_medical_draft(raw, active, library, repair_diagnostics)
                draft, local = _validate_blocks(bound, active, facts, records, chapter,
                    audit=audit, diagnostics=repair_diagnostics)
                for tid, values in local.items():
                    specific = errors.get(tid, [])
                    errors[tid] = list(dict.fromkeys([*specific, *[value for value in values
                        if value != 'required_target_missing' or not specific]]))
                    if 'primary_owner_fact_missing' in values:
                        repair_diagnostics.setdefault(tid, []).append({
                            'error': 'primary_owner_fact_missing',
                            'writingFocus': _writing_focus(active[tid], facts, patient_context),
                            'usedFactIds': list(dict.fromkeys(fid
                                for sentence in previous_drafts.get(tid, {}).get('sentences', ())
                                if isinstance(sentence, dict)
                                for fid in (sentence.get('factIds') or ()) if isinstance(fid, str))),
                            'repairDirection': '重新围绕本对象作结论，并选真正支持该结论的主事实；行为事实只补充个人联系。不要只在现稿挂一个无关主事实编号。'})
                    if 'template_cell_length_exceeded' in values:
                        repair_diagnostics.setdefault(tid, []).append({
                            'error': 'template_cell_length_exceeded',
                            'actualChars': _block_chars(previous_drafts.get(tid, {})),
                            'maxChars': active[tid].get('cellContract', {}).get('maxChars'),
                            'preferredChars': _writing_focus(active[tid], facts, patient_context)['preferredChars'],
                            'repairDirection': '重组为本栏负责的结论与行动，原值、范围、重复限制说明和其他栏的完整方案不再写入；不提高上限、不截断安全条件。'})
                for tid in active.keys() - draft.keys() - errors.keys():
                    errors[tid] = ['required_template_cell_missing']
                _event(audit, {'chapterCode': chapter['code'], 'group': index,
                    'stage': 'template_author' if attempt == 0 else 'template_repair',
                    'modelInputChars': chars, 'draftTargets': list(draft),
                    'errors': errors, 'passageCount': len(library),
                    'repairDiagnostics': deepcopy(repair_diagnostics),
                    'targetSpecifications': [{key: deepcopy(target.get(key)) for key in (
                        'id', 'path', 'cellContract', 'owner')} for target in active.values()],
                    'rawDraftBlocks': deepcopy(raw.get('blocks', []))})
                if draft:
                    review_request = {
                        'task': 'review_report_template_cells', 'blocks': list(draft.values()),
                        'reviewRules': [
                            '按form独立检查cellContract：paragraph才检查段落结构；classification/label只检查指定字值或短语的合理性，不要求补三步骤、改善方案或论文逐字引用该汉字。所有形式都回答question且无后台思考/来源规则。',
                            'paragraph分析单元格不能由support=source的结果复述充当成稿；结论及相称行动应为clinical_synthesis，新的机制命题须获准原段。不能把正文只改标签后放行，仍逐命题核事实、临床相称性与原文支持。',
                            'management_direction栏只接受相称管理行动的clinical_synthesis/action，不能让source复述或无引文interpretation冒充分析；evidence机制句仍逐命题核适用原段。行动前提同样不能把功能预测升级为本人炎症、免疫、衰老或营养状态。',
                            'ownerBindingMode=object_identity时，逐项核ownerFactIds确属该target真实检测对象，且行动与该对象的管理任务相称，ownerBindingValid才为true。块级对象绑定不证明疾病因果，句级患者前提和临床安全仍须全部通过；不得要求把无因果依据的检测分数硬挂为行动的患者前提。',
                            '仅有疾病关联信号而无针对性临床症状时，简短给出对应症状识别与就诊触发即可；不要强求把肥胖、血脂或疲劳写成精神/神经疾病诱因，也不要求重复本人未患病的排除说明。',
                            '逐target核writingFocus.patientContext的真实性别年龄与全部建议适用性；其他性别人群的生理阶段、复查安排不得套用。轻微或局部维护项目允许简短行动，不要求补一段未经支持的风险或正常/排除清单。',
                            '医学边界在内部核验：正文用准确主体、问题性质和适当行动表达结论，不要求靠反复出现“不能代表、仅为模型、尚未明确”等泛化限制句才判安全；删去这些空句也绝不能升级个人诊断或因果。以现有noProcessText和notRawResultRestatement检查这一职责。',
                            '先独立定inferenceClass：clinical_integration仅整合同病管理、事实与安排；biomedical_relation包括疾病病因、发生机制、跨病关系和器官病变排除，必须有适用原文。不能把脂肪肝转氨酶正常写成炎症不明显，不能凭肥胖/血脂给结节或息肉归共同诱因，也不能把菌种文献的homeostasis扩写成个人病因。',
                            '逐句核患者前提及全部命题。evidence需完整原段直接支持；source仅忠实概括。clinical_synthesis不得发明发现/确诊、改来源性质、升级相关为因果或把功能预测当人体缺乏；处方、疗效承诺和超出合理临床归纳的新策略不能放行。',
                            '菌群原始评估可如实记录，但不能直接变成人体免疫/营养/神经状态；仅隐血阳性不判结直肠癌高风险，单研究关联不推个人确诊。',
                            '对clinical_synthesis行动明确给actionScope和理由：routine_lifestyle/clinical_coordination/recorded_care可在相称及耐受条件下接受；novel_treatment/medical_prescription必须有适用指南原文。不要沿用默认false的冗余开关，也不要把温和步行、日常饮食目标自动当药物处方。',
                            '每句claimChecks.roleValid独立核作者的role与实际句义：observation记事实、interpretation作解释、action提行动；clinical_synthesis等是support而非role。新模板不另回roles平行数组，也不能用缺少echo拒绝已核句。未知/伪装role仍须fail。',
                            '只审核完整正文的临床安全、事实与命题支持；actionDetails为可选私有导航元数据，已由程序另行过滤，不以导航缺失否定合格正文。每target一次review，每句一次claimChecks；正文任一前提或命题不成立就fail。',
                            '对带引句逐个独立命题填coveredClinicalClaims/patientAdaptations/uncoveredClaims；通用减重指南可适配本人的日常频次，不能同时替隐血查因或睡眠监测背书。未覆盖片段逐字写入uncoveredClaims，让作者拆句并匹配各自support，不能整句借一篇文献通过。',
                            '每种support按对应义务审核：source用sourceFactsDirectlySupportClaim/noNewMedicalProposition；evidence用originalDirectlySupportsClaim；clinical_synthesis与applicable_guideline均用五项边界检查和reasoningBasis，后者再核适用指南方向与个体化落实。',
                        ],
                        'outputSchema': {'reviews': [{'targetId': 'given id', 'verdict': 'pass|fail',
                            'issues': [], 'support': 'source|clinical_synthesis|evidence|applicable_guideline|mixed', 'completion': 'complete',
                            'cellContractSatisfied': True, 'notRawResultRestatement': True,
                            'noProcessText': True, 'ownerBindingValid': True, 'claimChecks': [{
                                'roleValid': True,
                                'coveredClinicalClaims': [], 'patientAdaptations': [], 'uncoveredClaims': [],
                                'inferenceClass': 'source_observation|clinical_integration|biomedical_relation',
                                'patientConditionsSatisfied': True, 'originalDirectlySupportsClaim': False,
                                'sourceFactsDirectlySupportClaim': False, 'noNewMedicalProposition': False,
                                'noInventedFindings': True, 'sourceNaturePreserved': True,
                                'noUnsupportedDiagnosis': True, 'noCausalUpgrade': True,
                                'clinicallyProportionate': True, 'reasoningBasis': '本句的实际事实与临床归纳逻辑',
                                'applicableGuidelineSupportsDirection': False,
                                'patientAdaptationSupported': False,
                                'actionScope': 'routine_lifestyle|clinical_coordination|recorded_care|novel_treatment|medical_prescription',
                                'actionScopeReason': '行动性质、个体适用和耐受的核验理由；非行动句为空',
                                'noUnsupportedRemainder': True}]}]}
                    }
                    if any(not target.get('cellContract') for target in active.values()):
                        review_request['outputSchema']['reviews'][0]['roles'] = ['observation|interpretation|action']
                    reviewed, review_chars = await generate(packet, base, review_request)
                    _event(audit, {'chapterCode': chapter['code'], 'group': index,
                        'stage': 'review_role_diagnostics', 'targets': _role_review_diagnostics(reviewed, draft)})
                    passed, rejected = _accepted_blocks(reviewed, draft, active, audit=audit)
                    for tid, detail in _repair_claim_diagnostics(reviewed, draft, rejected).items():
                        repair_diagnostics.setdefault(tid, []).append(detail)
                    errors.update(rejected)
                    accepted.update(passed)
                    for tid, medical in passed.items():
                        buckets.setdefault(chapter['code'], {})[tid] = _merge_medical_addition(None, medical)
                    result[chapter['code']] = list(buckets[chapter['code']].values())
                    journal['reviewedBlocks'] = deepcopy(result)
                    _event(audit, {'chapterCode': chapter['code'], 'group': index,
                        'stage': 'template_independent_review', 'attempt': attempt,
                        'acceptedTargets': list(passed), 'errors': errors, 'modelInputChars': review_chars})
                failures = {tid: values for tid, values in errors.items() if tid not in accepted}
                if not failures:
                    break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = {tid: [type(error).__name__] for tid in active}
                _event(audit, {'chapterCode': chapter['code'], 'stage': 'template_incomplete',
                    'errorType': type(error).__name__, 'targetIds': list(failures)})
                if isinstance(error, TimeoutError):
                    break
        journal['medicalUnresolved'].extend({'chapterCode': chapter['code'], 'targetId': tid,
            'errors': values} for tid, values in failures.items())

    work = []
    for chapter in blueprint.chapters:
        targets = [target for target in chapter['targets'] if _medical_role(target, chapter)
            and not buckets.get(chapter['code'], {}).get(target['id'], {}).get('analysisGenerated')]
        for index, start in enumerate(range(0, len(targets), _GROUP_TARGETS), 1):
            work.append(group_pass(chapter, targets[start:start + _GROUP_TARGETS], index))
    if work:
        await asyncio.gather(*work)
    return result
