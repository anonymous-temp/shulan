"""Report-level repair requests; never medical approval or new treatment."""
from __future__ import annotations

import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)
_REASON = 'EVALUATION_WHOLE_PLAN_INCOMPLETE'
_CATEGORIES = {'main_problem_missing', 'missing_action', 'baseline_mismatch',
    'contradiction', 'duplicate', 'arithmetic', 'measurement_scope', 'generic_only'}
_ACTION_FIELDS = {('medicalAssessment', 'findings', 'sourceAdvice'),
    ('comprehensiveConclusion', 'priorityDirections', 'followupAction'),
    ('comprehensiveConclusion', 'nextSteps', 'description')}


def _code(path):
    return 'eval:' + json.dumps(path, ensure_ascii=False, separators=(',', ':'))


def _locator_scope(row):
    return tuple(sorted((s.source_code or '', s.record_id or '', s.observed_at or '') for s in row.sources))


def _items(data, selected=()):
    result = []
    codes = {entry['code'] for entry in selected}
    owners = {(row.name, _locator_scope(row)): row.code for row in data.medical_assessment.findings
        if row.code in codes}

    def add(path, text, owner, sources=(), owner_code=None):
        if owner_code is None:
            stamp = (owner, tuple(sorted((s.source_code or '', s.record_id or '', s.observed_at or '') for s in sources)))
            owner_code = owners.get(stamp)
        result.append({'fieldId': 'P' + str(len(result) + 1), 'path': list(path),
            'owner': owner, 'text': text or '', 'ownerCode': owner_code,
            'factIds': sorted({s.record_id for s in sources if s.record_id})})

    overview, conclusion = data.health_overview, data.comprehensive_conclusion
    add(('healthOverview', 'overallSummary', 'text'), overview.overall_summary.text,
        '整体健康主线', overview.overall_summary.sources)
    add(('healthOverview', 'summaries', 'medical', 'text'), overview.summaries.medical.text,
        '医学摘要', overview.summaries.medical.sources)
    add(('comprehensiveConclusion', 'overallConclusion', 'text'), conclusion.overall_conclusion.text,
        '整体结论', conclusion.overall_conclusion.sources)
    add(('comprehensiveConclusion', 'favorableFactors', 'text'), conclusion.favorable_factors.text,
        '有利因素', conclusion.favorable_factors.sources)
    for i, row in enumerate(data.medical_assessment.findings):
        add(('medicalAssessment', 'findings', i, 'sourceAdvice'), row.source_advice,
            row.name, row.sources, row.code)
    for i, row in enumerate(conclusion.priority_directions):
        add(('comprehensiveConclusion', 'priorityDirections', i, 'basis'), row.basis, row.name, row.sources)
        add(('comprehensiveConclusion', 'priorityDirections', i, 'followupAction'), row.followup_action,
            row.name, row.sources)
    for i, row in enumerate(conclusion.next_steps):
        add(('comprehensiveConclusion', 'nextSteps', i, 'description'), row.description,
            row.name, row.sources, row.code)
    return result


def _source_context(document, referenced_ids=()):
    referenced_ids = set(referenced_ids)
    facts = {fact.fact_id: fact for fact in document.facts
        if fact.kind in {'clinical', 'self_reported'} or fact.fact_id in referenced_ids}
    problems = []
    for problem in document.problems:
        if problem.certainty not in {'confirmed', 'suspected', 'source_finding', 'self_reported', 'current_problem'}:
            continue
        quotes = [dict(factId=source.fact_id, quote=source.quote) for source in problem.sources
            if source.fact_id in facts and source.quote and source.quote in str(facts[source.fact_id].value)]
        if quotes:
            problems.append({'problemId': problem.problem_id, 'name': problem.title,
                'certainty': problem.certainty, 'sourcePriority': problem.priority, 'factQuotes': quotes})
    return facts, problems


def _is_action_path(path):
    return len(path) == 4 and (path[0], path[1], path[-1]) in _ACTION_FIELDS


def _action_groups(data, document, items, selected):
    """One selected problem needs one current independently reviewed action."""
    from . import evaluation_narrative as narrative
    eligible = [block for block in document.sections
        if isinstance(block, narrative._ReviewedEvaluationBlock) and block._sentence_sources]
    targets = {t.path: t for t in narrative._targets(data, document)} if eligible else {}
    facts = {fact.fact_id: fact for fact in document.facts}
    live = set()
    finite_core = narrative._selected_source_focus_fallbacks(data, document)
    for item in items:
        path = tuple(item['path'])
        if path in finite_core and item['text'] == finite_core[path]['text']:
            item['coveredProblemIds'] = sorted({pid for row in selected for pid in row['problemIds']})
        elif path in finite_core and (target := targets.get(path)) is not None:
            block = next((block for block in eligible if block.code == target.code and block.text == item['text']
                and narrative._reviewed_source_state(block, target, facts)), None)
            if block is not None:
                missing_codes = {row.get('code') for row in narrative._global_priority_missing(target, block, facts)}
                item['coveredProblemIds'] = sorted({pid for row in selected if row['code'] not in missing_codes
                    for pid in row['problemIds']})
    for item in items:
        path = tuple(item['path'])
        if not _is_action_path(path) or not item['text']:
            continue
        target = targets.get(path)
        if target is None:
            continue
        if any(block.code == target.code and block.text == item['text']
                and narrative._reviewed_source_state(block, target, facts)
                and narrative._finding_executable_action(block.text) for block in eligible):
            live.add(item['fieldId'])
    groups = []
    for entry in selected:
        owned = [item for item in items if item.get('ownerCode') == entry['code']
            and _is_action_path(item['path'])]
        groups.append({**entry, 'actionFieldIds': [item['fieldId'] for item in owned],
            'reviewedExecutableActionFieldIds': [item['fieldId'] for item in owned if item['fieldId'] in live]})
    return groups


def _measurement_scope_witness(quote, item, facts):
    """Demand a real subject/predicate crossing, not a reviewer's paraphrase."""
    from .evaluation_content import measurement_object_semantics_failures
    from .evaluation_prose_roles import measurement_scope_text_allowed
    linked = [facts[fid] for fid in item['factIds'] if fid in facts]
    if measurement_scope_text_allowed(quote, path=item['path'], facts=linked):
        return False
    if measurement_object_semantics_failures(quote, linked):
        return True
    functional = any(f.kind == 'function' for f in linked)
    genes = any(re.search(r'基因|序列|gene', f.name, re.I) for f in linked)
    for clause in re.split(r'[。；;\n]', quote):
        if re.search(r'并非|不是|不代表|不等于|不等同|不能据此', clause):
            continue
        if functional and re.search(r'人体|体内|血液|血清|脑内|患者|您的', clause) and re.search(
                r'(?:浓度|含量|营养|免疫|抗炎|解毒|合成能力).{0,12}(?:充足|正常|不足|缺乏|低下|受损|升高|降低)', clause):
            return True
        if genes and re.search(r'(?:本人|患者|您|实际|已)[^。；;]{0,12}(?:感染|表达|产毒|分泌毒素|耐药|药敏)', clause):
            return True
    return False


def _reason_introduces_medical_action(reason, texts):
    if not re.search(r'建议|应当|应到|需要|缺少|未有|未见', reason):
        return False
    actual = '\n'.join(texts)
    methods = re.findall(r'(?<![A-Za-z])(?:OGTT|CT|MRI)(?![A-Za-z])|内镜|胃镜|肠镜|超声|活检|病理|组织学|免疫组化|'
        r'(?:到|转诊|咨询|就诊)[\u4e00-\u9fff]{1,10}科', reason, re.I)
    return any(method not in actual for method in methods)


def _valid_issue(issue, items, problems, facts, *, selected=(), action_groups=()):
    if not isinstance(issue, dict) or not isinstance(issue.get('category'), str) or issue['category'] not in _CATEGORIES:
        return None
    if not isinstance(issue.get('fieldId'), str):
        return None
    item = items.get(issue.get('fieldId'))
    if item is None or not isinstance(issue.get('reason'), str) or not issue['reason'].strip():
        return None
    quote = issue.get('quote', '')
    if not isinstance(quote, str) or quote and quote not in item['text']:
        return None
    category = issue['category']; missing = category in {'main_problem_missing', 'missing_action'}
    if not missing and (not quote or not item['text']):
        return None
    ids = issue.get('problemIds', [])
    if not isinstance(ids, list) or any(not isinstance(problem_id, str) or problem_id not in problems for problem_id in ids):
        return None
    selected_ids = {pid for row in selected for pid in row['problemIds']}
    if missing and (not ids or not set(ids) <= selected_ids):
        return None
    witnesses = issue.get('factQuotes', {})
    if not isinstance(witnesses, dict) or any(fid not in facts or not isinstance(value, str)
            or not value or value not in str(facts[fid].value) for fid, value in witnesses.items()):
        return None
    if missing and not witnesses:
        return None
    if missing and any(not set(witnesses).intersection({fid for row in selected
            if problem_id in row['problemIds'] for fid in row['factIds']}) for problem_id in ids):
        return None
    if category == 'main_problem_missing' and set(ids).intersection(item.get('coveredProblemIds', ())):
        return None
    related = issue.get('relatedFieldId'); related_quote = issue.get('relatedQuote', '')
    if related is not None and not isinstance(related, str):
        return None
    if related is not None and (related not in items or not isinstance(related_quote, str)
            or not related_quote or related_quote not in items[related]['text']):
        return None
    if category in {'contradiction', 'duplicate'} and related is None:
        return None
    if category == 'duplicate' and (item['path'][:2] != ['comprehensiveConclusion', 'nextSteps']
            or items[related]['path'][:2] != ['comprehensiveConclusion', 'nextSteps']
            or item['path'] == items[related]['path']):
        return None
    if category == 'measurement_scope' and not _measurement_scope_witness(quote, item, facts):
        return None
    group = None
    if category == 'missing_action':
        group = next((row for row in action_groups if set(ids) <= set(row['problemIds'])), None)
        if (group is None or not _is_action_path(item['path'])
                or item['fieldId'] not in group['actionFieldIds']
                or group['reviewedExecutableActionFieldIds']):
            return None
    if category == 'main_problem_missing' and tuple(item['path']) not in {
            ('healthOverview', 'overallSummary', 'text'), ('healthOverview', 'summaries', 'medical', 'text'),
            ('comprehensiveConclusion', 'overallConclusion', 'text')}:
        return None
    relevant = [item['text']] + ([items[related]['text']] if related is not None else [])
    if group:
        relevant.extend(items[fid]['text'] for fid in group['actionFieldIds'])
    if _reason_introduces_medical_action(issue['reason'], relevant):
        return None
    return {**issue, 'path': item['path'], 'repairCode': _code(item['path']),
        **({'actionGroupCode': group['code']} if group else {})}


async def review_evaluation_plan(client, data, document, *, subject=None):
    """One review plus at most one review-format repair; never rewrite prose."""
    if document is None:
        return {'status': 'not_applicable', 'issues': [], 'repairCodes': []}
    from .evaluation_clinical_focus import selected_source_clinical_focus
    selected = selected_source_clinical_focus(data, document)
    items = _items(data, selected)
    facts, problems = _source_context(document, {fid for item in items for fid in item['factIds']})
    groups = _action_groups(data, document, items, selected)
    fingerprint = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if client is None:
        result = {'status': 'unavailable', 'issues': [], 'repairCodes': [], 'fingerprint': fingerprint}
    else:
        aliases = {'F' + str(i + 1): fact for i, fact in enumerate(facts.values())}
        fact_alias = {fact.fact_id: alias for alias, fact in aliases.items()}
        payload_items = [{**item, 'factIds': [fact_alias[fid] for fid in item['factIds'] if fid in fact_alias]} for item in items]
        payload_problems = [{**problem, 'factQuotes': [{'factId': fact_alias[s['factId']], 'quote': s['quote']}
            for s in problem['factQuotes']]} for problem in problems]
        payload = {'task': 'review_complete_evaluation_plan', 'subject': subject or {}, 'fields': payload_items,
            'expectedFieldIds': [item['fieldId'] for item in items], 'currentSourceProblems': payload_problems,
            'selectedClinicalFocus': [{**row, 'factIds': [fact_alias[fid] for fid in row['factIds'] if fid in fact_alias]} for row in selected],
            'actionCoverageGroups': [{**row, 'factIds': [fact_alias[fid] for fid in row['factIds'] if fid in fact_alias]} for row in groups],
            'patientFacts': [{'factId': alias, 'name': f.name, 'value': f.value, 'kind': f.kind,
                'unit': f.unit, 'date': f.date, 'status': f.status, 'referenceRange': f.reference_range,
                'source': f.source} for alias, f in aliases.items()],
            'rules': [
                '读完所有fields，checkedFieldIds完整且各一次。本审核只请求修复，不批准医学断言、不新增诊断或治疗。',
                '完整currentSourceProblems和patientFacts只作真值语境；main_problem_missing和missing_action仅可针对selectedClinicalFocus.problemIds，不为轻微review级化验或正常记录新增主线和行动配额。目标不是疾病，自述不升级确诊。',
                '整体总览、医学摘要、结论须保真传达selectedClinicalFocus主题，可合并同主题或使用准确概括，不机械要求标题词出现；不要求全部观测进一段。',
                '同一selected问题的医学sourceAdvice、priority followupAction与nextSteps为actionCoverageGroups：跨这些字段至少一个真实可执行且独立审核的动作即可，reviewedExecutableActionFieldIds非空不得报同问题missing_action；不强迫每栏重复动作。无任何动作时只报一项同owner缺口，不逐字段重复。',
                '保留来源状态及必要测量边界；measurement_scope必须quote中有实际跨越对象的主体和谓词。明确菌群丁酸合成或合成评分不能被理由改写成人体浓度；理由本身不是越界witness。',
                '菌群评分不等于人体能力、代谢物浓度、营养缺乏、基因表达、感染或药敏；物种丰富度不等绝对菌量。明确测量对象的有限源状态合法，不因缺科学引用报错。',
                '维持/增减符合真实基线；钟点时差正确，在床不等实际睡眠。baseline_mismatch须实际factQuotes；不能从否定史或未知推其他否定。',
                'duplicate只指同一nextSteps列表中重复执行相同行动，必须同时逐字给两行quote；跨章导航/医学说明与下一步复用不是duplicate。',
                '所有非遗漏错误须quote逐字存在本field；contradiction/duplicate同时给真实relatedFieldId/relatedQuote。遗漏只列selected真实problemIds与逐字来源witness。',
                'reason只解释实际差距，禁止建议新增检查、科室、药物、阈值或策略；不能以格式审核给医疗行动授权。不要按篇幅/引用数/用词偏好报错。'],
            'outputSchema': {'checkedFieldIds': [item['fieldId'] for item in items], 'issues': [{
                'category': '|'.join(sorted(_CATEGORIES)), 'fieldId': 'P1', 'quote': '',
                'relatedFieldId': None, 'relatedQuote': '', 'problemIds': [],
                'factQuotes': {'F1': '逐字值片段'}, 'reason': '具体差距，无替代医疗建议'}]}}
        try:
            item_map = {item['fieldId']: item for item in items}
            raw = {}; attempts = 0
            for attempt in range(2):
                attempts += 1
                raw = await client.generate(payload)
                complete = (isinstance(raw, dict) and isinstance(raw.get('checkedFieldIds'), list)
                    and all(isinstance(field_id, str) for field_id in raw['checkedFieldIds'])
                    and len(raw['checkedFieldIds']) == len(item_map) and set(raw['checkedFieldIds']) == set(item_map)
                    and isinstance(raw.get('issues'), list))
                issues, rejected, seen_groups = [], 0, set()
                for issue in raw.get('issues', []) if isinstance(raw, dict) else []:
                    if isinstance(issue, dict) and isinstance(issue.get('factQuotes'), dict):
                        issue = {**issue, 'factQuotes': {aliases[k].fact_id if k in aliases else k: v
                            for k, v in issue['factQuotes'].items()}}
                    valid = _valid_issue(issue, item_map, {p['problemId'] for p in problems}, facts,
                        selected=selected, action_groups=groups)
                    if valid and valid.get('actionGroupCode') in seen_groups:
                        continue
                    if valid:
                        issues.append(valid)
                        if valid.get('actionGroupCode'): seen_groups.add(valid['actionGroupCode'])
                    else:
                        rejected += 1
                if complete and not rejected:
                    break
                payload = {**payload, 'mode': 'repair_review_format', 'previousReview': raw,
                    'reviewFormatFeedback': {'allFieldIdsCheckedOnce': complete, 'invalidIssueCount': rejected},
                    'rules': [*payload['rules'], '这是至多一次的审核格式/职责校正，只修正审核JSON或撤回无实际witness/不属selected/重复配额的问题，不改报告、不改正确文本、不新增医学建议。']}
            # A reviewer cannot declare an empty cross-field action group
            # complete. This creates only a missing-duty repair request, never
            # a medical suggestion or an approval for any candidate action.
            for group in groups:
                if group['reviewedExecutableActionFieldIds'] or not group['actionFieldIds'] or group['code'] in seen_groups:
                    continue
                candidates = [item_map[fid] for fid in group['actionFieldIds']]
                owner = next((item for item in candidates if item['path'][-1] == 'sourceAdvice'), candidates[0])
                issues.append({'category': 'missing_action', 'fieldId': owner['fieldId'], 'quote': '',
                    'relatedFieldId': None, 'relatedQuote': '', 'problemIds': group['problemIds'],
                    'factQuotes': {fid: facts[fid].value for fid in group['factIds'] if fid in facts},
                    'reason': '该选定问题在现有建议字段中尚无独立审核通过的可执行行动。',
                    'path': owner['path'], 'repairCode': _code(owner['path']), 'actionGroupCode': group['code'],
                    'reviewOrigin': 'current_private_action_duty'})
                seen_groups.add(group['code'])
            result = {'status': 'pass' if complete and not issues and not rejected else 'incomplete',
                'checkedFieldCount': len(raw.get('checkedFieldIds', [])) if isinstance(raw, dict) else 0,
                'fieldCount': len(items), 'issues': issues, 'rejectedIssueCount': rejected,
                'reviewAttempts': attempts, 'selectedFocusCount': len(selected), 'actionGroupCount': len(groups),
                'repairCodes': list(dict.fromkeys(issue['repairCode'] for issue in issues)), 'fingerprint': fingerprint}
        except Exception as exc:
            logger.warning('report whole-plan review unavailable: %s', type(exc).__name__)
            result = {'status': 'unavailable', 'issues': [], 'repairCodes': [], 'fingerprint': fingerprint}
    feedback = {}
    for issue in result['issues']: feedback.setdefault(issue['repairCode'], []).append(issue)
    document._evaluation_plan_review_feedback = feedback
    document._evaluation_plan_review = result
    document._evaluation_plan_review_history = [*getattr(document, '_evaluation_plan_review_history', ()), result]
    document.incomplete = [reason for reason in document.incomplete if reason != _REASON]
    if result['status'] not in {'pass', 'not_applicable'}: document.incomplete.append(_REASON)
    logger.info('report whole-plan review: %s', json.dumps(result, ensure_ascii=False))
    return result
