"""Source-preserving clinical grouping for the agreed report table layout.

Grouping is authored and independently checked once. It changes display rows,
not measurements or patient identities. Original rows are kept in the private
source inventory, so a grouped problem never erases its actual witnesses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections import Counter
from copy import deepcopy
from time import monotonic

from .evaluation_derived_indices import is_derived_index
from .evaluation_disease_identity import same_source_disease
from .evaluation_schema import AntimicrobialResistance
from .evaluation_schema import DiseaseRiskItem
from .evaluation_schema import MedicalFinding
from .evaluation_schema import MetricAssessment
from .evaluation_schema import NextStep
from .evaluation_schema import OverviewFinding
from .evaluation_schema import PriorityDirection
from .evaluation_schema import QuestionnaireSummaryItem
from .evaluation_schema import SourceObservation
from .evaluation_schema import SourceReference

_LEVELS = {'高风险', '中风险', '低风险'}
_LEVEL_ORDER = {'高风险': 0, '中风险': 1, '低风险': 2}
_NEGATIVE = {'未检出', '阴性', '未发现', '未见异常', '未见明显异常', '未检出相关菌群异常'}
_NORMAL = {'正常', '低风险', '未检出', '阴性', '未见异常', '未见明显异常'}
_GROUP_ROLES = {'health_problem', 'secondary_finding', 'normal_screening', 'management_goal'}
_CANONICAL_DIRECTIONS = {'↑': '升高', '↓': '降低', '增高': '升高', '下降': '降低'}
_FOCUS_LABELS = {'优先查因', '重点管理', '持续管理', '近期复评', '按症状管理', '结合症状随访'}
_EXPLICIT_SOURCE_STATES = _NORMAL | _NEGATIVE | _LEVELS | {
    '偏高', '偏低', '升高', '降低', '增高', '下降', '异常', '超标', '检出',
    '阳性', '弱阳性', '强阳性', '↑', '↓', '▲', '▼', '较弱', '很弱', '较强', '丰富'}
_SOFTWARE_SOURCE_STATES = {'用户自述，未核验', '用户自述,未核验', '用户自述',
    '问卷自述', '本人自述', '自述', '未核验', 'self_reported'}


def _plain(value):
    return value.model_dump(mode='json', by_alias=False) if hasattr(value, 'model_dump') else deepcopy(value)


def _stable(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        default=str).encode()).hexdigest()[:18]


def _sources(rows):
    result, seen = [], set()
    for row in rows:
        for source in getattr(row, 'sources', ()):
            key = json.dumps(_plain(source), sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                result.append(deepcopy(source))
    return result


def _status(row):
    return row.source_status or row.assessment_level


def _not_detected(row):
    return any(isinstance(value, str) and value.strip() in _NEGATIVE
        for value in (getattr(row, 'source_status', None), getattr(row, 'detection_status', None),
            getattr(row, 'value', None)))


def _archive(data, rows, kind):
    existing = {row.observation_id for row in data.appendices.source_items}
    for row in rows:
        raw = _plain(row)
        identity = 'template-original:' + kind + ':' + _stable(raw)
        if identity in existing:
            continue
        data.appendices.source_items.append(SourceObservation(
            observation_id=identity, code=row.code, name=row.name,
            scientific_name=getattr(row, 'scientific_name', None),
            value=deepcopy(row.value), unit=row.unit, reference_range=row.reference_range,
            source_status=deepcopy(row.source_status), kind=kind,
            category=getattr(row, 'system', None) or getattr(row, 'category', None),
            source_type='template_layout_original',
            description=json.dumps(raw, ensure_ascii=False, sort_keys=True),
            sources=deepcopy(row.sources), evidence_refs=deepcopy(row.evidence_refs),
            availability=row.availability))
        existing.add(identity)


def _detail(row):
    pieces = [str(row.name or row.code or '')]
    if row.value is not None:
        pieces.append(str(row.value) + (str(row.unit) if row.unit else ''))
    status = _status(row)
    if status and str(status) not in pieces:
        pieces.append(str(status))
    return '：'.join(piece for piece in pieces if piece)


_SUMMARY_NAME = re.compile(
    r'^\s*(?:主检(?:结论|建议|小结)|总检(?:结论|建议)|原报告建议)[：:]|(?:原报告|原始报告)[)）]?\s*$'
)
_ADVICE_NAME = re.compile(r'^\s*(?:主检建议|总检建议|原报告(?:复查|随访)?建议|医嘱)(?:\s*$|\s*[：:])')
# 必要时/医生指导 open an advice sentence of their own ("必要时在医生指导下使用…");
# without them such a sentence would be read as a result.
_ADVICE_SENTENCE = re.compile(r'^\s*(?:建议|请|宜|应|需|可)|建议|就诊|随诊|复查|咨询|门诊|结合临床|必要时|医生指导')


def _reader_name(row):
    name = re.sub(r'^\s*(?:主检(?:结论|建议|小结)|总检(?:结论|建议))[：:]\s*', '', str(row.name or row.code or ''))
    name = re.sub(r'[（(]\s*(?:原报告|原始报告|计算值)\s*[)）]', '', name)
    return re.sub(r'检查异常(?:待复核)?$', '', name).strip()


def _self_reported(row):
    state = str(_status(row) or '')
    return row.certainty == 'self_reported' or any(token in state for token in ('自述', '未核验'))


def _summary_row(row):
    """A physician-summary or advice line (its advice is kept separately).

    Recognised by its name, or by content: a non-measurement row whose text is
    made only of advice sentences (a summary line renamed upstream).
    """
    if _SUMMARY_NAME.search(str(row.name or '')) or str(row.name or '').endswith('建议'):
        return True
    text = str(row.value or '').strip()
    measured = row.unit or row.reference_range or re.search(r'\d', text)
    return bool(text) and not measured and not _summary_findings(row)


def _summary_findings(row):
    """The finding part of a summary line, without its advice sentences."""
    sentences = re.findall(r'[^。；;]+[。；;]?', str(row.value or ''))
    return ''.join(part for part in sentences if not _ADVICE_SENTENCE.search(part)).strip('；; ')


def _reader_value(members):
    """The results of a grouped problem as the reader sees them.

    Measurements and imaging findings keep their recorded wording; summary
    and advice lines are left to the management cell (their advice is kept
    in the layout metadata); questionnaire answers are listed once by name.
    """
    pieces, reported = [], []
    for row in members:
        if _self_reported(row):
            reported.append(_reader_name(row))
            continue
        if _summary_row(row):
            continue
        name, value = _reader_name(row), '' if row.value is None else str(row.value).strip()
        if not row.unit and not row.reference_range and _summary_findings(row):
            value = _summary_findings(row)  # free text: results only, advice goes to the advice list
        value = re.sub(r'^\s*' + re.escape(name) + r'\s*[：:]\s*', '', value) if name else value
        # A value that already names its own items ("总胆固醇：5.88…") needs no prefix.
        labelled = bool(re.match(r'^[^：:；;]{1,40}[：:]', value))
        piece = value if labelled else f'{name}：{value}' if name and value else name or value
        if row.unit and row.unit not in piece:
            piece += ' ' + str(row.unit)
        state = _status(row)
        state = None if not state or str(state) in _SOFTWARE_SOURCE_STATES or str(state) in piece else str(state)
        if row.reference_range and '参考' not in piece:
            piece += '（参考' + str(row.reference_range) + (f'；{state}' if state else '') + '）'
        elif state:
            piece += f'（{state}）'
        pieces.append(piece.rstrip('；; '))
    if not pieces:
        # Only summary lines: keep their findings (never their advice).
        pieces = [f'{_reader_name(row)}：{_summary_findings(row)}'.rstrip('：')
            for row in members if _summary_row(row) and not _self_reported(row)]
    if reported:
        pieces.append('问卷：' + '、'.join(dict.fromkeys(name for name in reported if name)))
    # Flatten to single results, drop exact repeats and bare names already
    # shown inside another result.
    items = []
    for piece in pieces:
        for part in _top_level_parts(piece):
            if part and part not in items:
                items.append(part)
    items = [item for item in items if '：' in item or not any(item != other and item in other for other in items)]
    return '；'.join(items)


def _top_level_parts(text):
    parts, depth, current = [], 0, []
    for char in str(text or ''):
        depth += char in '（('
        depth -= char in '）)' and depth > 0
        if char in '；;' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append(''.join(current).strip())
    return [part.strip('；; ') for part in parts if part.strip('；; ')]


def _member_advice(members):
    advice = []
    for row in members:
        if row.value and _ADVICE_NAME.match(str(row.name or '')):
            advice.append(str(row.value).strip())
        elif row.value and not row.unit and not row.reference_range:
            sentences = re.findall(r'[^。；;]+[。；;]?', str(row.value))
            advice.extend(part.strip() for part in sentences if _ADVICE_SENTENCE.search(part))
        if getattr(row, 'source_advice', None):
            advice.append(str(row.source_advice).strip())
    return list(dict.fromkeys(item for item in advice if item))


def _indicator(row):
    # Imaging conclusions and reported symptoms often have no separate
    # high/low flag. Their actual recorded result still belongs in the evidence
    # cell; a label alone must not replace or erase that result.
    state = _status(row)
    if isinstance(state, str) and state.strip() in _SOFTWARE_SOURCE_STATES:
        state = None
    if not state and row.value is not None:
        state = str(row.value)
        if row.unit:
            state += str(row.unit)
    if _self_reported(row):
        state = '问卷填写'
    return MetricAssessment(code=row.code, name=_reader_name(row),
        source_status=state, sources=deepcopy(row.sources),
        availability=row.availability)


def _indicator_members(members):
    """Prefer actual measurements over a repeated main-exam summary."""
    def summary(row):
        return isinstance(row.value, str) and row.value.lstrip().startswith(
            ('主检结论', '主检提示', '总检结论'))

    direct = any(not summary(row) and row.certainty != 'self_reported'
        and row.value not in (None, '')
        and (row.unit or row.reference_range or any(source.source_code == 'A' for source in row.sources))
        for row in members)
    if not direct:
        return members
    return [row for row in members if not summary(row)
        or isinstance(_status(row), str) and _status(row).strip() in _EXPLICIT_SOURCE_STATES]


def _explicit_normal_screening(row):
    """Recognize an actual normal conclusion, not one reassuring word in a report."""
    import re
    state = str(_status(row) or '').strip()
    value = str(row.value or '').strip()
    abnormal_text = value.replace('未见明显异常', '').replace('未见异常', '').replace('无异常', '')
    contradicted = re.search(r'异常|偏高|偏低|增高|降低|减退|骨量减少|疏松', abnormal_text)
    if state in {'正常', '未见异常', '未见明显异常'} and not contradicted:
        return True
    if value in {'正常', '未见异常', '未见明显异常'}:
        return True
    # A complete source conclusion can follow numerical screening results.
    # Contradictory/abnormal conclusions are never made normal by this rule.
    return bool(re.search(r'(?:诊断提示|检查结论|检查结果|结论|诊断)\s*[：:]\s*正常[。；;\s]*$', value)
        and not contradicted)


def _management_goal_only(row, facts):
    """A stated improvement goal is context, not another disease or symptom.

    The decision needs the actual question/answer context and no independent
    clinical witness. A change such as unexplained or non-intentional weight
    loss remains an actual reported symptom, regardless of the questionnaire.
    """
    import re
    witnesses = [facts[ref.record_id] for ref in row.sources if ref.record_id in facts]
    if not witnesses or any(fact.kind != 'self_reported' for fact in witnesses):
        return False
    contexts = [str(fact.name or '') + ' ' + str(fact.value or '') for fact in witnesses]
    if any(re.search(r'不明原因|非意愿|非刻意|无意(?:中)?|未主动|并非主动', context) for context in contexts):
        return False
    goal_context = r'希望.{0,12}改善|想.{0,12}改善|健康目标|改善目标|希望.{0,8}解决'
    if not all(re.search(goal_context, context) for context in contexts):
        return False
    imperative = r'^(?:(?:希望|想(?:要)?|计划|目标|改善|优化|减轻|降低|控制|增强|提高|维持|保持)\s*)+'
    name = str(row.name or '').strip()
    subject = re.sub(imperative, '', name).strip()
    # "Improve constipation" may contain a real reported symptom. A
    # normalized symptom witness must not be lost with its goal wording.
    if len(subject) >= 2 and any(subject in str(fact.name or '')
            and not re.search(goal_context, str(fact.name or ''))
            and not re.match(imperative, str(fact.name or '')) for fact in witnesses):
        return False
    return bool(re.match(imperative, name))


def _group_validation(groups, clinical, native, categories):
    """Validate addresses and partition only; medical approval is independent."""
    result, errors, used = [], [], set()
    ids = Counter(row.get('id') for row in groups if isinstance(row, dict))
    for group in groups:
        if not isinstance(group, dict):
            errors.append({'id': None, 'reason': 'invalid_group'})
            continue
        # Management goals are merged into a real problem by instruction, and
        # models echo their "managementGoal:" address among memberCodes. Such
        # auxiliary or already-used addresses are dropped rather than voiding
        # the whole clinically correct group (which scattered its members into
        # separate rows). A group left without any real member is still invalid.
        group = dict(group)
        if isinstance(group.get('memberCodes'), list):
            group['memberCodes'] = [code for code in dict.fromkeys(group['memberCodes'])
                if isinstance(code, str) and code in clinical and code not in used]
        if isinstance(group.get('nativeCodes'), list):
            group['nativeCodes'] = [code for code in dict.fromkeys(group['nativeCodes'])
                if isinstance(code, str) and code in native]
        # The three-level vocabulary is fixed; "高"/"中"/"低" or English words
        # are the same answer in another spelling, not an invalid group.
        level = group.get('attentionLevel')
        if isinstance(level, str):
            group['attentionLevel'] = _LEVEL_SPELLINGS.get(level.strip().casefold(), level.strip())
            if group['attentionLevel'] in {'null', 'none', ''}:
                group['attentionLevel'] = None
        if group.get('includeInRiskChapter') is False and group.get('category') in {'', 'null', 'none'}:
            group['category'] = None
        identity, codes = group.get('id'), group.get('memberCodes')
        include = group.get('includeInRiskChapter')
        role = group.get('role')
        category_valid = (group.get('category') in categories if include is True
            else group.get('category') is None or group.get('category') in categories)
        if (not isinstance(identity, str) or not identity or ids[identity] != 1
                or not isinstance(codes, list) or not codes or len(set(codes)) != len(codes)
                or any(code not in clinical for code in codes) or used.intersection(codes)
                or not isinstance(group.get('name'), str) or not group['name'].strip()
                or type(include) is not bool or role not in _GROUP_ROLES
                or not category_valid
                or group.get('attentionLevel') not in (_LEVELS if include else _LEVELS | {None})
                or include and role in {'secondary_finding', 'normal_screening', 'management_goal'}
                or type(group.get('priority')) is not int or group['priority'] < 1
                or group.get('certainty') not in {
                    'confirmed', 'current_problem', 'self_reported', 'potential_risk', 'suspected'}
                or not isinstance(group.get('nativeCodes', []), list)
                or any(code not in native for code in group.get('nativeCodes', []))):
            errors.append({'id': identity, 'reason': 'group_address_or_state_invalid'})
            continue
        if include and all(_explicit_normal_screening(clinical[code]) for code in codes):
            errors.append({'id': identity, 'reason': 'normal_screening_cannot_be_a_disease_risk'})
            continue
        used.update(codes)
        adopted = deepcopy(group)
        # A management label is not a disease probability or a source grade.
        # Unknown labels are not published; the reviewed group can use a
        # non-specific management label without inventing a cause or diagnosis.
        if adopted.get('focusLabel') not in _FOCUS_LABELS:
            adopted['focusLabel'] = None
        result.append(adopted)
    return result, errors


_REPAIR_ROUNDS = 2
_LEVEL_SPELLINGS = {'高': '高风险', '中': '中风险', '低': '低风险', 'high': '高风险', 'medium': '中风险',
    'moderate': '中风险', 'low': '低风险', '高关注': '高风险', '中关注': '中风险', '低关注': '低风险'}


def _numeric_model_value(value):
    return bool(re.fullmatch(r'\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*', str(value if value is not None else '')))


def _problem_key(value):
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = re.sub(r'^主检结论[:：]', '', text)
    text = re.sub(r'[（(][^）)]*[）)]', '', text)
    text = re.sub(r'[\d.]+\s*(?:mmol/L|U/L|ng/m?[lL]|cm|kg|%)?', '', text)
    return re.sub(r'[\s:：,，;；、/]+', '', text)


def _attach_leftover_rows(accepted, clinical):
    """Attach an ungrouped row to the accepted group that literally names it.

    Only an exact name containment is used (for example a physician-summary
    line repeating an accepted diagnosis). Nothing is renamed and no new
    diagnosis is created; rows without a literal match stay as their own rows.
    """
    adopted = {code for group in accepted for code in group['memberCodes']}
    attached = []
    for code, row in clinical.items():
        if code in adopted:
            continue
        key = _problem_key(getattr(row, 'name', None))
        if len(key) < 2:
            continue
        matches = [group for group in accepted if group.get('role') == 'health_problem'
            and len(_problem_key(group.get('name'))) >= 2
            and (_problem_key(group['name']) in key or key in _problem_key(group['name']))]
        if len(matches) == 1:
            matches[0]['memberCodes'].append(code)
            adopted.add(code)
            attached.append({'code': code, 'groupId': matches[0].get('id')})
    return attached


def _approved_groups(groups, response):
    reviews = response.get('reviews', []) if isinstance(response, dict) else []
    by_id = {}
    for row in reviews if isinstance(reviews, list) else []:
        if isinstance(row, dict):
            by_id.setdefault(row.get('id'), []).append(row)
    accepted, rejected = [], []
    checks = ('sameProblem', 'nameSupported', 'subjectAndCertaintyPreserved',
        'noNewDiagnosis', 'categorySuitable', 'attentionJustified', 'nativeLinksSameDisease',
        'riskChapterRelevanceJustified', 'normalScreeningNotPromoted', 'focusLabelSuitable')
    for group in groups:
        rows = by_id.get(group['id'], [])
        review = rows[0] if len(rows) == 1 else {}
        if (review.get('verdict') == 'pass' and review.get('issues') == []
                and all(review.get(key) is True for key in checks)):
            accepted.append(group)
        else:
            rejected.append({'id': group['id'], 'reason': 'independent_layout_review_not_passed',
                'issues': deepcopy(review.get('issues', [])),
                'checks': {key: review.get(key) is True for key in checks}})
    return accepted, rejected


def _computed_context_only(row, facts):
    """A calculated index that crosses no threshold is context, not a finding.

    Calculated values are added so that every chapter cites one number. One
    that is flagged (for example an obesity grade) stays with its problem; an
    unflagged count or ratio standing alone must not become a row of the
    findings table. Its fact stays available to every writing task.
    """
    witnesses = [facts[ref.record_id] for ref in row.sources if ref.record_id in facts]
    return bool(witnesses) and all(is_derived_index(getattr(fact, 'source', None))
        and not getattr(fact, 'abnormal', False) for fact in witnesses)


def _accepted_extensions(proposed, accepted, remaining):
    """Split repair proposals that reuse an accepted group's id.

    Accepted groups are locked, but a repair round may assign a still
    unadopted row to one of them (a physician-summary line or the original
    report's advice for the same problem). Such a proposal becomes a candidate
    extension: the accepted group plus only the new rows, reviewed like any
    other group. Returns ``(extensions, other_proposals)``.
    """
    by_id = {group['id']: group for group in accepted}
    extensions, others, claimed = [], [], set()
    for group in proposed if isinstance(proposed, list) else ():
        target = by_id.get(group.get('id')) if isinstance(group, dict) else None
        if target is None:
            others.append(group)
            continue
        if target.get('role') in {'management_goal', 'normal_screening'}:
            continue
        extra = [code for code in dict.fromkeys(group.get('memberCodes') or ())
            if isinstance(code, str) and code in remaining and code not in claimed]
        if extra:
            claimed.update(extra)
            extensions.append({**deepcopy(target), 'memberCodes': [*target['memberCodes'], *extra],
                'extends': target['id'], 'addedMemberCodes': extra})
    return extensions, others


def _apply_extensions(accepted, approved):
    by_id = {group['id']: group for group in accepted}
    applied = []
    for extension in approved:
        target = by_id.get(extension['extends'])
        if target is None:
            continue
        added = [code for code in extension['addedMemberCodes'] if code not in target['memberCodes']]
        target['memberCodes'].extend(added)
        applied.append({'id': target['id'], 'addedMemberCodes': added})
    return applied


def _source_named_groups(groups, rejected, accepted, clinical):
    """Keep the members of a still-rejected group together under a source name.

    When the independent review confirms that the members are one problem and
    no diagnosis is added, but keeps objecting to the wording of the proposed
    name or certainty, scattering the members into separate rows is the worst
    outcome for the reader. The group is adopted with the literal name and
    certainty of its leading source row; routing, attention, vendor links and
    the action label are kept only where the same review approved them.
    """
    adopted = {code for group in accepted for code in group['memberCodes']}
    verdicts = {row['id']: row.get('checks') or {} for row in rejected}
    kept = []
    for group in groups:
        checks = verdicts.get(group['id'])
        codes = group['memberCodes']
        if (checks is None or adopted.intersection(codes)
                or group.get('role') in {'management_goal', 'normal_screening'}
                or not all(checks.get(key) for key in ('sameProblem', 'noNewDiagnosis', 'normalScreeningNotPromoted'))):
            continue
        lead = clinical[next((code for code in codes if code.startswith('clinicalProblem:')), codes[0])]
        name = re.sub(r'^主检结论[:：]\s*', '', str(lead.name or '')).strip()
        if not name:
            continue
        routed = bool(group.get('includeInRiskChapter')) and all(checks.get(key) for key in (
            'categorySuitable', 'riskChapterRelevanceJustified', 'attentionJustified'))
        fallback = {**deepcopy(group), 'name': name, 'certainty': lead.certainty,
            'includeInRiskChapter': routed, 'category': group.get('category') if routed else None,
            'attentionLevel': group.get('attentionLevel') if routed else None,
            'nativeCodes': list(group.get('nativeCodes', ())) if checks.get('nativeLinksSameDisease') else [],
            'focusLabel': group.get('focusLabel') if checks.get('focusLabelSuitable') else None,
            'sourceNamed': True}
        adopted.update(codes)
        kept.append(fallback)
    return kept


def _native_relevant(row):
    if _not_detected(row):
        return False
    state = str(_status(row) or '')
    return state in {'中风险', '中高风险', '高风险'} or any(
        isinstance(value, str) and value.strip() in {'检出', '阳性', '检出相关菌群异常'}
        for value in (row.value, row.source_status))


def _literal_gene_symbols(description, label):
    """Read a machine-preserved gene column, never the panel's name."""
    if not isinstance(description, str):
        return []
    matches = re.findall(r'(?:^|[；;\n])\s*' + re.escape(label)
        + r'\s*[:：]\s*([^；;\n]*)', description)
    return list(dict.fromkeys(symbol for clause in matches
        for symbol in re.split(r'[,，、\s]+', clause.strip())
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', symbol)))


def _source_gene_detections(data):
    result = []
    for row in data.appendices.source_items:
        if row.category != 'virulence' or row.source_type == 'template_layout_original':
            continue
        detected = _literal_gene_symbols(row.description, '检出基因')
        result.append({'reportedLabel': row.name, 'annotationVerified': False,
            'detectedGenes': detected,
            'candidateGenes': _literal_gene_symbols(row.description, '检测面板候选基因'),
            'claimObjects': [] if _not_detected(row) else detected,
            'sourceStatus': _status(row) or row.value,
            'sourceFactIds': list(dict.fromkeys(source.record_id for source in row.sources
                if source.record_id)), 'sources': [_plain(source) for source in row.sources]})
    return result


def _project_detected_gene_views(data, gene_records):
    """Use explicit same-row detected symbols in repeated display views."""
    def row_refs(row):
        return {(source.source_code, source.record_id, source.observed_at)
            for source in row.sources if source.record_id
            and not re.search(r':page:\d+$', source.record_id)}

    collections = [pathway.related_metrics for pathway in data.functional_pathways.pathways]
    collections.extend(row.related_indicators
        for row in data.disease_risk_assessment.risk_ranking)
    collections.extend(row.related_indicators
        for category in data.disease_risk_assessment.categories for row in category.items)
    for rows in collections:
        for row in rows:
            sources = row_refs(row)
            matches = [record for record in gene_records
                if row.name == record['reportedLabel'] and record['claimObjects']
                and sources.intersection((source.get('source_code'), source.get('record_id'),
                    source.get('observed_at')) for source in record['sources'])]
            if len(matches) != 1:
                continue
            _archive(data, [row], 'template_original_gene_display')
            row.name = '、'.join(matches[0]['claimObjects'])


def _source_health_layout(data):
    """Keep measured modules, fold panel children and normal AMR categories."""
    section = data.microbiome_assessment
    originals = list(section.health_metrics)
    _archive(data, originals, 'template_original_health_metric')
    panel = next((row for row in originals if row.code == 'virulenceGenes'), None)
    gene_records = _source_gene_detections(data)
    layout = getattr(data, '_template_layout', {})
    layout['geneDetections'] = gene_records
    object.__setattr__(data, '_template_layout', layout)
    _project_detected_gene_views(data, gene_records)
    gene_source_ids = {source_id for record in gene_records for source_id in record['sourceFactIds']}
    children = [row for row in originals if row.code != 'virulenceGenes'
        and (any(source.record_id in gene_source_ids for source in row.sources)
            or any(term in str(row.name or '') for term in ('毒素基因', '肠毒素', 'EPEC', 'STEC')))]
    if panel is not None:
        positive = [row for row in children if not _not_detected(row)]
        panel.sources = _sources([panel, *positive])
        # A reported toxin/pathogen panel label is not a verified functional
        # annotation. The display detail owns only explicit detected symbols
        # and literal status; candidates and source labels stay in the archive.
        details = list(dict.fromkeys(symbol + '：' + str(record['sourceStatus'])
            for record in gene_records for symbol in record['claimObjects']))
        if hasattr(panel, 'details'):
            panel.details = '；'.join(details) or None
    section.health_metrics = [row for row in originals
        if not _not_detected(row) and row not in children]
    for row in section.health_metrics:
        if hasattr(row, 'details') and not row.details:
            matching = [taxon for taxon in section.taxa if taxon.category == row.code
                and not _not_detected(taxon)]
            if matching:
                row.details = '；'.join((taxon.name or '') + '：' + str(_status(taxon) or '')
                    for taxon in matching)
    section.taxa = [row for row in section.taxa if not _not_detected(row)]
    amr = list(section.antimicrobial_resistance)
    _archive(data, amr, 'template_original_antimicrobial_resistance')
    normal = [row for row in amr if _status(row) in _NORMAL or _not_detected(row)]
    abnormal = [row for row in amr if row not in normal]
    if normal:
        abnormal.append(AntimicrobialResistance(code='otherResistanceCategories',
            name='其余抗生素类别', antibiotic_class='其余类别',
            value='；'.join(_detail(row) for row in normal), source_status='正常',
            assessment_level='正常', sources=_sources(normal), availability='available'))
    section.antimicrobial_resistance = abnormal


def normalize_template_source_states(data):
    """Label explicit source directions and actual child states, never clinical risk."""
    changes, seen = [], set()

    def assign(row, state, reason):
        _archive(data, [row], 'template_original_source_state')
        row.assessment_level = state
        changes.append({'code': row.code, 'name': row.name, 'state': state, 'reason': reason})

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif hasattr(type(value), 'model_fields'):
            if isinstance(value, MetricAssessment):
                current = str(value.assessment_level or '').strip()
                source = str(value.source_status or '').strip()
                if current in _CANONICAL_DIRECTIONS:
                    assign(value, _CANONICAL_DIRECTIONS[current], 'explicit_state_synonym')
                elif not current and source in _CANONICAL_DIRECTIONS:
                    assign(value, _CANONICAL_DIRECTIONS[source], 'explicit_source_direction')
                elif not current and source in {'正常', '异常', '偏低', '极低', '偏高', '较弱', '很弱', '较强', '升高', '降低', '丰富', '不足'}:
                    assign(value, source, 'explicit_source_state')
            for key in type(value).model_fields:
                if key not in {'sources', 'appendices'}:
                    visit(getattr(value, key))

    for section in (data.microbiome_assessment, data.nutrition_assessment,
            data.functional_pathways, data.disease_risk_assessment):
        visit(section)
    normal = {'正常', '未见异常', '未见明显异常'}
    weak = {'偏低', '极低', '降低', '较弱', '很弱', '弱', '减弱', '低下'}
    shifted = {'偏高', '升高', '较高', '过高', '增强'}
    for row in data.functional_pathways.pathways:
        if row.assessment_level or row.source_status:
            continue
        members = list(row.related_metrics)
        states = [str(member.assessment_level or member.source_status or '').strip()
            for member in members]
        known = [state for state in states if state]
        if not known:
            continue
        low, high = any(state in weak for state in known), any(state in shifted for state in known)
        if low and high:
            state = '组合变化'
        elif low:
            state = '存在弱项'
        elif high or any(state not in normal for state in known):
            state = '存在偏移'
        elif len(known) == len(members):
            state = '正常'
        else:
            state = '部分项目正常'
        assign(row, state, 'explicit_related_metric_states')
    # Only a report's already recorded enterotype permits the fixed label.
    # Dominant taxa, relative abundance and missing values never infer a type.
    enterotypes = {
        '拟杆菌型': 'I型（拟杆菌型）', '拟杆菌属型': 'I型（拟杆菌型）',
        '普雷沃氏菌型': 'II型（普雷沃氏菌型）', '普氏菌型': 'II型（普氏菌型）',
        '瘤胃球菌型': 'III型（瘤胃球菌型）', '瘤胃球菌属型': 'III型（瘤胃球菌型）',
    }
    for row in data.microbiome_assessment.overall_structure:
        if row.code == 'enterotype' and not row.assessment_level:
            explicit = str(row.value or '').strip()
            if explicit in enterotypes:
                assign(row, enterotypes[explicit], 'explicit_report_enterotype')
    return changes


def _clinical_priority_key(group):
    # Cause-finding is an actual clinical task, not a probability of a named
    # disease. Chronic burden must not move it down the report solely because
    # the burden received a higher internal attention level.
    return (0 if group.get('focusLabel') == '优先查因' else 1,
        group['priority'], _LEVEL_ORDER.get(group.get('attentionLevel'), 3),
        group['name'])


def _install_layout(data, accepted, clinical, native, category_names, *, known_fact_ids=None):
    ordered = sorted(accepted, key=_clinical_priority_key)
    known_fact_ids = set(known_fact_ids or ())
    used, grouped, risks, meta, linked_native, normals = set(), [], [], [], set(), []
    for group in ordered:
        members = [clinical[code] for code in group['memberCodes']]
        native_members = [native[code][1] for code in group.get('nativeCodes', [])]
        used.update(group['memberCodes'])
        if group.get('role') in {'management_goal', 'normal_screening'}:
            private_role = group['role']
            prefix = 'templateGoal:' if private_role == 'management_goal' else 'templateNormal:'
            meta.append({**deepcopy(group), 'code': prefix + _stable(group['memberCodes']),
                'riskCode': None, 'origin': private_role,
                'sourceFactIds': list(dict.fromkeys(ref.record_id for row in members
                    for ref in row.sources if ref.record_id in known_fact_ids)),
                'sources': [_plain(ref) for ref in _sources(members)]})
            if private_role == 'normal_screening':
                # Normal results are information too (and reassurance): they are
                # shown as the examination's normal findings, never as problems.
                normals.append(MedicalFinding(code=prefix + _stable(group['memberCodes']), name=group['name'],
                    system=group.get('system'), value=_reader_value(members) or None,
                    certainty='confirmed', sources=_sources(members), availability='available'))
            continue
        if group['includeInRiskChapter']:
            linked_native.update(group.get('nativeCodes', []))
        code = 'templateProblem:' + _stable(sorted(group['memberCodes']))
        row = MedicalFinding(code=code, name=group['name'], system=group.get('system'),
            value=_reader_value(members) or '；'.join(_detail(member) for member in members),
            assessment_level=None, certainty=group['certainty'],
            sources=_sources(members), availability='available')
        grouped.append(row)
        risk_code = None
        if group['includeInRiskChapter']:
            risk_code = 'templateRisk:' + group['category'] + ':' + _stable(sorted(group['memberCodes']))
            risk = DiseaseRiskItem(code=risk_code, name=group['name'], value=None,
                assessment_level=None, related_indicators=[_indicator(member)
                    for member in _indicator_members(members) if not _summary_row(member)],
                sources=_sources([*members, *native_members]),
                availability='partial')
            for source in native_members:
                for taxon in source.related_taxa:
                    risk.related_indicators.append(MetricAssessment(name=taxon.name or taxon.scientific_name,
                        source_status=taxon.source_status, sources=deepcopy(taxon.sources),
                        availability='available'))
            risks.append(risk)
        meta.append({**deepcopy(group), 'code': code, 'riskCode': risk_code,
            'origin': 'clinical_group', 'originalAdvice': _member_advice(members),
            'sourceFactIds': list(dict.fromkeys(
                ref.record_id for ref in row.sources if ref.record_id in known_fact_ids)),
            'sources': [_plain(ref) for ref in row.sources]})
    # Rejected/omitted rows remain visible in their original clinical form.
    grouped.extend(deepcopy(row) for code, row in clinical.items() if code not in used)
    for code, (category, original) in native.items():
        if code in linked_native or not _native_relevant(original):
            continue
        if any(group['includeInRiskChapter'] and same_source_disease(original.name, group['name'])
                for group in ordered):
            continue
        risk_code = 'templateRisk:' + category + ':' + _stable(code)
        indicators = [deepcopy(row) for row in original.related_indicators]
        indicators.extend(MetricAssessment(name=taxon.name or taxon.scientific_name,
            value=taxon.value, unit=taxon.unit,
            source_status=taxon.source_status, sources=deepcopy(taxon.sources),
            availability='available') for taxon in original.related_taxa)
        model_value = original.value if _numeric_model_value(original.value) else None
        model_level = (original.source_status or original.assessment_level) if model_value is not None else None
        # The vendor prints a model index and an association panel for some
        # diseases. They are one topic for the reader: merge them into the row
        # already created instead of listing the disease twice.
        twin = next((row for row in risks if row.code and row.code.startswith('templateRisk:' + category + ':')
            and row.source_status and row.source_status.startswith('菌群')
            and same_source_disease(row.name, original.name)), None)
        if twin is not None:
            known = {(item.name, item.source_status) for item in twin.related_indicators}
            twin.related_indicators.extend(item for item in indicators
                if (item.name, item.source_status) not in known)
            for source in original.sources:
                if source not in twin.sources:
                    twin.sources.append(deepcopy(source))
            if model_value is not None and twin.value is None:
                twin.value = model_value
                twin.source_status = '菌群评估：' + str(model_level or '已评估')
            for group in meta:
                if group.get('riskCode') == twin.code:
                    group['nativeCodes'] = list(dict.fromkeys([*group.get('nativeCodes', ()), code]))
                    group['sourceFactIds'] = list(dict.fromkeys([*group.get('sourceFactIds', ()),
                        *(ref.record_id for ref in original.sources if ref.record_id in known_fact_ids)]))
            continue
        # A positive source model itself is an actual observation, even when
        # it supplies no specific organisms. Its source nature stays explicit.
        if not indicators:
            indicators = [MetricAssessment(code=original.code,
                name=(original.name or '') + '检测关联结果',
                source_status=original.source_status or original.assessment_level
                    or (str(original.value) if original.value is not None else None),
                sources=deepcopy(original.sources), availability=original.availability)]
        # The printed model index and grade are shown with their source named;
        # assessment_level stays empty because it is not this report's grading.
        risks.append(DiseaseRiskItem(code=risk_code, name=original.name, value=model_value,
            source_status=('菌群评估：' + str(model_level or '已评估')) if model_value is not None else '菌群关联',
            assessment_level=None,
            related_indicators=indicators, sources=deepcopy(original.sources), availability='partial'))
        meta.append({'code': risk_code, 'riskCode': risk_code, 'name': original.name,
            'category': category, 'origin': 'source_association', 'memberCodes': [],
            'role': 'source_association', 'includeInRiskChapter': True,
            'nativeCodes': [code], 'certainty': 'source_association',
            'attentionLevel': None, 'sourceFactIds': list(dict.fromkeys(
                ref.record_id for ref in original.sources if ref.record_id in known_fact_ids)),
            'sources': [_plain(ref) for ref in original.sources]})
    data.medical_assessment.findings = grouped
    data.medical_assessment.normal_findings = normals
    data.disease_risk_assessment.risk_ranking = risks
    priorities = [group for group in meta if group['origin'] == 'clinical_group'
        and group.get('includeInRiskChapter')
        and group.get('attentionLevel') in {'高风险', '中风险'}]
    clinical_lookup = {row.code: row for row in grouped}
    data.health_overview.key_findings = [OverviewFinding(rank=index,
        name=group['name'], module=category_names[group['category']],
        assessment_level=group.get('focusLabel') or (
            '重点管理' if group.get('attentionLevel') in {'高风险', '中风险'} else '持续管理'),
        sources=deepcopy(clinical_lookup[group['code']].sources),
        availability='partial') for index, group in enumerate(priorities, 1)]
    data.comprehensive_conclusion.priority_directions = [PriorityDirection(rank=index,
        name=group['name'], sources=deepcopy(clinical_lookup[group['code']].sources),
        availability='partial') for index, group in enumerate(priorities, 1)]
    data.comprehensive_conclusion.next_steps = [NextStep(code=group['code'],
        name=group['name'], sources=deepcopy(clinical_lookup[group['code']].sources),
        availability='partial') for group in priorities]
    return meta


async def arrange_template_rows(client, data, document, subject, deadline=None, audit=None):
    """Group source findings, independently review routing, and repair once.

    Medical findings retain all observations. A separately reviewed boolean
    decides which actual problems belong in the seven risk directions; the
    navigation category itself never declares a diagnosis. All original model
    proposals/reviews remain private audit material, including rejected names.
    """
    from .clinical_report import reported_fact_kind

    journal = audit if isinstance(audit, dict) else {}
    findings = list(data.medical_assessment.findings)
    _archive(data, findings, 'template_original_clinical')
    source_facts = {fact.fact_id: fact for fact in getattr(document, 'facts', ())}
    management_goals = [row for row in findings if _management_goal_only(row, source_facts)]
    findings = [row for row in findings if row not in management_goals]
    _archive(data, management_goals, 'template_original_management_goal')
    journal['templateManagementGoals'] = [_plain(row) for row in management_goals]
    computed_context = [row for row in findings if _computed_context_only(row, source_facts)]
    findings = [row for row in findings if row not in computed_context]
    _archive(data, computed_context, 'template_original_computed_context')
    journal['templateComputedContext'] = [row.name for row in computed_context]
    _archive(data, data.medical_assessment.normal_findings, 'template_original_normal')
    data.medical_assessment.normal_findings = []
    data.medical_assessment.questionnaire_summary = []
    for fact in source_facts.values():
        if reported_fact_kind(fact) != 'symptom':
            continue
        _, separator, response = fact.value.partition('；回答：')
        value = (response if separator else fact.value).strip()
        if value:
            data.medical_assessment.questionnaire_summary.append(QuestionnaireSummaryItem(
                code=fact.fact_id,
                name=fact.name,
                value=value,
                sources=[SourceReference(source_code='B', record_id=fact.fact_id)],
                availability='available',
            ))
    data.medical_assessment.consistency_notes = []
    data.introduction.description = ''
    native = {}
    category_names = {row.code: row.name for row in data.disease_risk_assessment.categories}
    for category in data.disease_risk_assessment.categories:
        for index, row in enumerate(category.items):
            key = str(row.code or 'native:' + category.code + ':' + str(index))
            if key in native:
                key += ':source-row:' + str(index)
            native[key] = (category.code, row)
    clinical = {}
    for index, row in enumerate(findings):
        key = str(row.code or 'clinical:' + str(index))
        if key in clinical:
            key += ':source-row:' + str(index)
        clinical[key] = row
    known_fact_ids = {fact.fact_id for fact in getattr(document, 'facts', ())}
    payload = {'task': 'arrange_report_template_rows', 'subject': _plain(subject),
        'categories': [{'code': code, 'name': name} for code, name in category_names.items()],
        'managementGoals': [_plain(row) for row in management_goals],
        'clinicalRows': [{'code': code, 'name': row.name, 'value': row.value,
            'unit': row.unit, 'status': _status(row), 'system': row.system,
            'certainty': row.certainty, 'explicitNormalScreening': _explicit_normal_screening(row),
            'sources': [_plain(ref) for ref in row.sources]} for code, row in clinical.items()],
        'nativeDiseaseRows': [{'code': code, 'category': category, 'name': row.name,
            'value': row.value, 'status': _status(row), 'sourceOnly': True}
            for code, (category, row) in native.items()],
        'personalContextFacts': [{key: raw[key] for key in (
            'fact_id', 'kind', 'name', 'value', 'unit', 'status', 'date') if key in raw}
            for fact in getattr(document, 'facts', ())
            if getattr(fact, 'kind', None) in {'clinical', 'self_reported', 'context'}
            for raw in [_plain(fact)]],
        'instructions': [
            '只组织真实临床资料，不生成正文或新诊断。同病指标归入同一实际疾病/健康问题，不把TC/TG/LDL或腰围分别命名疾病。多个独立问题不能只因同属系统而合成一病。',
            '已有肥胖或中心性肥胖时，腰围、体重指数和体成分作为同一问题的具体依据合并，不另造腰围增大疾病行。正常骨密度等筛查结果用normal_screening，只作为其他问题的内部判断背景，不命名为当前疾病或异常。',
            '同一次、同一功能主题检查的多个相关信号可聚合为一项源观察，例如脑血管功能检查内的流速、弹性、调节功能改变；名称保持检测观察性质，完整保留成员结果及原总体结论，不改名卒中或脑血管病，也不在多行重复互抄不同观察对象。不同器官的独立影像病变不能仅因同次检查而合并。',
            'memberCodes覆盖全部clinicalRows且每条仅一次，包括次要发现；实际健康问题及次要发现保留医学评估表，正常筛查和纯管理目标保内部来源，不列当前问题。风险章另作相关性选择，不是每组都进六类。',
            'role用health_problem、secondary_finding、normal_screening或management_goal。includeInRiskChapter为真实布尔：仅与六类疾病/健康风险有实质管理关联的问题为true；轻微非特异化验、次要局部影像、正常筛查和个人管理目标不强塞疾病风险方向。',
            '需要实际随访管理的结节、息肉及肿瘤等健康问题，按病变所在器官归入给定六类：胃肠归intestinal，肝胆胰和甲状腺归metabolic，脑和神经系统归neurological；其他按实际器官或临床系统选择相关领域，不单列tumor。保留准确病变名称、影像性质与随访任务，不能将结节或息肉改为癌症。没有实际管理意义的偶然发现仍可只保医学表。',
            'explicitNormalScreening=true的正常检查不能生成疾病风险；role=normal_screening或secondary_finding必须includeInRiskChapter=false。没有合适六类导航时category=null，保留医学表且不增分区。',
            'includeInRiskChapter=true时category选给定六类；这是导航，不是病因归属或确诊。疲劳若有实际健康意义可在精神与睡眠方向导航，但不能因此改名抑郁症、睡眠障碍或其他精神疾病。',
            '名称和确定性必须由原记录支持。隐血不能命名癌症，孤立C肽升高不能命名胰岛素抵抗；可用准确的源健康问题名保留该项，不要为迎合疾病目录强造诊断。本人/家属、否定/自述/疑似/确诊不能互换。',
            'nativeDiseaseRows仅来源模型/关联，不能据此新增临床组。nativeCodes只连接确属同病的来源行，不拿另一疾病或菌群信号给临床命名背书。',
            'attentionLevel仅高中低三档管理关注；非风险章的次要/正常组可null。system用真实临床系统或专科；priority从1开始，表示本人实际临床处理先后，不是原模型风险概率或严重度的机械排序。优先查因及紧迫处理任务先于稳定慢性管理，其后综合病情、实际影响及处置时机排序；不要复制示例患者或发明测值。'],
        'outputSchema': {'groups': [{'id': 'G1', 'name': '真实疾病或健康问题',
            'system': '系统专科', 'memberCodes': ['given code'], 'nativeCodes': [],
            'role': 'health_problem|secondary_finding|normal_screening|management_goal',
            'includeInRiskChapter': True, 'category': 'given category or null',
            'certainty': 'confirmed|current_problem|self_reported|potential_risk|suspected',
            'attentionLevel': '高风险|中风险|低风险|null',
            'focusLabel': '优先查因|重点管理|持续管理|近期复评|按症状管理|结合症状随访', 'priority': 1}]}}
    payload['instructions'].append(
        'attentionLevel只作内部管理排序，不能写成个人疾病风险等级。对本人问题给focusLabel行动短标签，按真实处理任务选优先查因、重点管理、持续管理、近期复评、按症状管理或结合症状随访；不把标签写成确诊、患病概率或未经验证的高风险。')
    payload['instructions'].append(
        'name是简短的问题名（疾病名，或“检查项目＋所见”），一般不超过16个字。名称只概括本组memberCodes里的记录：personalContextFacts中其他项目和计算值不写进名称，也不在名称里罗列数值、留样条件、病史或解释；成员既有当前检验所见又有筛查提示时，名称两者都要覆盖。')
    payload['instructions'].append(
        '问诊与查体的背景记录（既往史、用药史、家族史、生活方式、未见异常的体格检查）不是一项健康问题：role用normal_screening、includeInRiskChapter=false、category=null，只作内部背景，不在医学评估表单列。')
    payload['instructions'].append(
        'managementGoals是希望改善的管理目标，合并进相应真实问题的管理情景，不单列为疾病；其他clinicalRows若也只有目标语境可标management_goal且includeInRiskChapter=false。真正出现的不明原因或非意愿症状不可因其属于改善诉求而删除。')
    accepted = []
    try:
        async def generate(body):
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('template_layout_deadline')
            async with asyncio.timeout(remaining):
                return await client.generate(body)

        async def review(groups, context, *, repair=False):
            if not groups:
                return [], []
            body = deepcopy(context)
            body.update(task='review_report_template_rows', groups=groups,
                repairRound=repair, reviewInstructions=[
                    '逐组独立检查同一真实问题、姓名/确定性及成员值保持，没有新诊断；不为了填目录认可错误名字。',
                    '复核肥胖及腰围/体成分依据是否合理聚合；同一次同主题功能检查可归一项源观察并保留总体结论，不能让相邻行的建议对象互相替代。正常筛查不当当前异常或疾病。',
                    'includeInRiskChapter单独判断：次要发现保医学表即可，不必硬塞六类。正常筛查不能变疾病风险；分类只导航、不等于诊断。category=null且不入风险章是合法的。',
                    '结节、息肉及肿瘤按所在器官归入给定六类，不单列tumor；胃肠归intestinal，肝胆胰和甲状腺归metabolic，脑和神经系统归neurological。良性或小病变不自动排除；保准确病变名，不以癌症替换，不以来源模型癌症面板代替真实管理对象。',
                    '单C肽不能直接命名胰岛素抵抗；疲劳不能直接命名精神疾病。若名称不成立，请issues说明应回到哪个原始健康问题，而不是要求继续解释错误病名。',
                    '确认nativeCodes确实同病，不把来源模型或另一疾病当临床确诊依据。attention须合实际管理轻重，非风险组null合法。'],
                outputSchema={'reviews': [{'id': 'given group id', 'verdict': 'pass|fail',
                    'issues': [], 'sameProblem': True, 'nameSupported': True,
                    'subjectAndCertaintyPreserved': True, 'noNewDiagnosis': True,
                    'categorySuitable': True, 'attentionJustified': True,
                    'nativeLinksSameDisease': True, 'riskChapterRelevanceJustified': True,
                    'normalScreeningNotPromoted': True, 'focusLabelSuitable': True}]})
            body['reviewInstructions'].append(
                '核focusLabel是否适合本人实际问题及处理任务；不能把内部attentionLevel或原菌群分档当个人疾病风险。缺失标签可用通用重点管理/持续管理，不凭空增加查因病名。')
            body['reviewInstructions'].append(
                '核priority是否按本人实际临床处理先后排列：优先查因及紧迫任务不应被稳定慢性管理冲淡；同类任务综合病情、功能影响和处置时机。priority不是原模型风险概率，也不要求高attentionLevel一律在前。')
            body['reviewInstructions'].append(
                '名称简短即可，不要求在名称里写出数值、条件或全部成员；名称没有超出成员记录、没有新增诊断就算nameSupported。问诊与查体的背景记录归normal_screening作内部背景是合法的。')
            body['reviewInstructions'].append(
                '希望改善的目标不能独立冒充疾病或症状，已有真实病症应保留且目标仅作管理背景；实际非意愿或不明原因变化不是纯目标。')
            response = await generate(body)
            journal.setdefault('templateLayoutReviewResponses', []).append(deepcopy(response))
            return _approved_groups(groups, response)

        if clinical:
            authored = await generate(payload)
            proposed = authored.get('groups', []) if isinstance(authored, dict) else []
            journal['templateLayoutProposedGroups'] = deepcopy(proposed)
            groups, errors = _group_validation(proposed, clinical, native, category_names)
            journal['templateLayoutAddressErrors'] = errors
            accepted, rejected = await review(groups, payload)
            journal['templateLayoutRejections'] = rejected
            # Two repair rounds: the first usually fixes routing, the second the
            # wording the reviewer asked for. Each round sees only the rows that
            # are still unadopted, with the latest objections.
            history = [(groups, rejected)]
            last_rejected, last_proposed, last_errors = rejected, proposed, errors
            for round_index in range(_REPAIR_ROUNDS):
                adopted = {code for group in accepted for code in group['memberCodes']}
                remaining_clinical = {code: row for code, row in clinical.items() if code not in adopted}
                if not remaining_clinical:
                    break
                repair_body = deepcopy(payload)
                repair_body.update(task='repair_report_template_rows',
                    clinicalRows=[row for row in payload['clinicalRows'] if row['code'] in remaining_clinical],
                    previousProposedGroups=deepcopy(last_proposed), addressIssues=deepcopy(last_errors),
                    rejectedGroups=deepcopy(last_rejected), acceptedGroups=deepcopy(accepted),
                    repairInstructions=[
                        '仅修复当前未采纳clinicalRows；已采纳组不可改、成员不可重复。只允许给出这些真实成员的新分组。',
                        '错误病名可退回准确源健康问题名，不要维护未成立诊断；必要时改role、includeInRiskChapter=false、category=null，医学表仍保留。',
                        '如分组没有足够依据，使用原源问题名称和原确定性单独保留；不要删除其源记录，也不强行命名新病。',
                        'rejectedGroups的issues是审核人对上一稿的具体意见，逐条照改；审核人认可的部分（同一问题、导航、关注等级）保持不变。',
                        '未采纳行若只是某个已采纳组同一问题的补充依据（例如主检结论、原报告建议），返回该已采纳组的原id，memberCodes只写这些未采纳行，审核通过后并入该组。'])
                repaired = await generate(repair_body)
                repair_proposed = repaired.get('groups', []) if isinstance(repaired, dict) else []
                extensions, repair_proposed = _accepted_extensions(repair_proposed, accepted, remaining_clinical)
                repair_groups, repair_errors = _group_validation(repair_proposed,
                    remaining_clinical, native, category_names)
                repaired_accepted, repaired_rejected = await review(
                    [*repair_groups, *extensions], repair_body, repair=True)
                extended = _apply_extensions(accepted, [group for group in repaired_accepted if group.get('extends')])
                repaired_accepted = [group for group in repaired_accepted if not group.get('extends')]
                if extended:
                    journal.setdefault('templateLayoutExtendedGroups', []).extend(extended)
                accepted.extend(repaired_accepted)
                suffix = '' if round_index == 0 else str(round_index + 1)
                journal['templateLayoutRepairProposedGroups' + suffix] = deepcopy(repair_proposed)
                journal['templateLayoutRepairAddressErrors' + suffix] = repair_errors
                journal['templateLayoutRepairRejections' + suffix] = repaired_rejected
                history.append((repair_groups, repaired_rejected))
                last_rejected, last_proposed, last_errors = repaired_rejected, repair_proposed, repair_errors
            source_named = []
            for round_groups, round_rejected in reversed(history):  # the latest wording first
                source_named.extend(_source_named_groups(
                    round_groups, round_rejected, [*accepted, *source_named], clinical))
            accepted.extend(source_named)
            journal['templateLayoutSourceNamedGroups'] = [
                {'id': group['id'], 'name': group['name'], 'memberCodes': list(group['memberCodes'])}
                for group in source_named]
            journal['templateLayoutAttachedLeftovers'] = _attach_leftover_rows(accepted, clinical)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        journal['templateLayoutError'] = type(error).__name__
    meta = _install_layout(data, accepted, clinical, native, category_names,
        known_fact_ids=known_fact_ids)
    object.__setattr__(data, '_template_layout', {'groups': meta,
        'managementGoals': [_plain(row) for row in management_goals]})
    _source_health_layout(data)
    journal['templateSourceStateNormalizations'] = normalize_template_source_states(data)
    adopted = {code for group in accepted for code in group['memberCodes']}
    journal['templateLayout'] = {'acceptedGroups': len(accepted),
        'unresolvedMemberCodes': [code for code in clinical if code not in adopted],
        'sourceClinicalRowCount': len(clinical), 'displayClinicalRowCount': len(data.medical_assessment.findings),
        'diseaseRowCount': len(data.disease_risk_assessment.risk_ranking),
        'riskRelevantClinicalGroups': sum(bool(group.get('includeInRiskChapter')) for group in accepted),
        'secondaryOrNormalGroups': sum(not group.get('includeInRiskChapter') for group in accepted),
        'singleDiseaseScoresCreated': False, 'groups': deepcopy(meta)}
    return data


async def link_template_risk_context(client, data, document, subject, deadline=None, audit=None):
    """Review bounded personal context using short IDs; retain original facts."""
    journal = audit if isinstance(audit, dict) else {}
    rows = {row.code: row for row in data.disease_risk_assessment.risk_ranking if row.code}
    all_facts = {fact.fact_id: fact for fact in getattr(document, 'facts', ())}
    context_ids = {fid for fid, fact in all_facts.items()
        if fact.kind in {'clinical', 'self_reported'}}
    indicators = {code: {'I' + str(index + 1): item
        for index, item in enumerate(row.related_indicators)} for code, row in rows.items()}
    indicator_fact_ids = {code: {iid: {ref.record_id for ref in item.sources
        if ref.record_id in all_facts} for iid, item in items.items()}
        for code, items in indicators.items()}
    display_ids = {fid for items in indicator_fact_ids.values()
        for ids in items.values() for fid in ids}
    facts = {fid: fact for fid, fact in all_facts.items() if fid in context_ids | display_ids}
    if not rows or not facts:
        journal['templateRiskContext'] = {'acceptedLinks': [], 'addedIndicators': 0}
        return data
    risk_ids = {'R' + str(i + 1): code for i, code in enumerate(rows)}
    fact_ids = {'F' + str(i + 1): fid for i, fid in enumerate(facts)}
    risk_wire, fact_wire = {v:k for k,v in risk_ids.items()}, {v:k for k,v in fact_ids.items()}
    journal['templateRiskContextIdMap'] = {'riskIds': risk_ids, 'factIds': fact_ids}
    group_metadata = {group.get('riskCode'): group for group in getattr(data, '_template_layout', {}).get('groups', ())}
    payload = {'task': 'link_report_risk_context', 'subject': _plain(subject),
        'riskRows': [{'riskCode': risk_wire[code], 'name': row.name, 'status': _status(row),
            'origin': group_metadata.get(code, {}).get('origin'),
            'existingIndicators': [{**_plain(item), 'indicatorId': iid,
                'factIds': [fact_wire[fid] for fid in sorted(indicator_fact_ids[code][iid])]}
                for iid, item in indicators[code].items()],
            'existingSources': [_plain(ref) for ref in row.sources]} for code, row in rows.items()],
        'sourceFacts': [{**{key: raw[key] for key in ('kind', 'name', 'value', 'unit',
            'status', 'date', 'source') if key in raw}, 'fact_id': fact_wire[fid]}
            for fid, fact in facts.items() for raw in [_plain(fact)]],
        'instructions': [
            '仅为给定已有问题选择个人临床情景，不建病名、不诊断、不评分、不推新阈值或因果。返回给定R/F短ID，不抄长出处。',
            '不限于同病确诊：真实体格、实验室、症状、饮食活动及本人家族背景均可相关；不能因未确诊同病就遗漏。',
            '完整阅读全部事实。每risk按本病相关性优先次序选至多5条关键临床/自述supportFactIds、至多2条关键contraryFactIds；必须保留真正关键证据，不能任意挑前几项或遗漏有意义的糖化、家族史等。既有C指标另行保留，不占新增临床支持名额。',
            '反证只供内部，不改正常为异常；本人/亲属、否定/阳性、自述/确诊不可互换；提问不等于阳性。',
            '非特异疲劳、一般睡眠时长、减重诉求不机械作为神经退行性疾病或重性精神疾病的阳性支持；没有具体疾病诊断、相应功能表现或公认的本病风险联系时，只可放managementContextFactIds作管理背景，不公开当疾病证据。',
            '同轮生成evidenceDisplay：既有指标引用indicatorId及其真实factId，新增临床支持用indicatorId=null并引用supportFactIds。displayName是简短准确项目名，recordedPresentation必须是该fact原值或原状态的精确连续片段，保主体、数值、单位、方向；不生成解释、诊断、阈值。',
            '显示名及表现不带问卷题号、完整题干、多选分隔符┋、主检长段或未核验等后台标签。可将真实问题简称为睡眠时长、糖尿病家族史等，表现只摘真实答案。不能为了简短改数值、把正常当异常、把亲属病史当本人疾病。',
            '尽量为既有全部有用指标提供简洁显示；原有C菌群/基因等证据不能因新增临床资料而丢失。来源基因栏目标签不是已核实功能身份，已给实际基因符号的指标名应保实际符号。',
            'rationale只在私有审核保存。最终科学解释仍须正文与文献审核。'],
        'outputSchema': {'links': [{'riskCode': 'R1', 'supportFactIds': ['F1'],
            'contraryFactIds': [], 'managementContextFactIds': [],
            'evidenceDisplay': [{'indicatorId': 'I1 or null', 'factId': 'F1',
                'displayName': '简洁项目名', 'recordedPresentation': '原值或状态的连续片段'}],
            'rationale': '事实关联说明'}]}}
    try:
        async def generate(body):
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('risk_context_deadline')
            async with asyncio.timeout(remaining):
                return await client.generate(body)
        def restore(link):
            if not isinstance(link, dict):
                return link
            result = deepcopy(link)
            result['riskCode'] = risk_ids.get(result.get('riskCode'), result.get('riskCode'))
            for field in ('supportFactIds', 'contraryFactIds', 'managementContextFactIds'):
                if isinstance(result.get(field), list):
                    result[field] = [fact_ids.get(fid, fid) if isinstance(fid, str) else fid for fid in result[field]]
            if isinstance(result.get('evidenceDisplay'), list):
                for item in result['evidenceDisplay']:
                    if isinstance(item, dict) and isinstance(item.get('factId'), str):
                        item['factId'] = fact_ids.get(item.get('factId'), item.get('factId'))
            return result
        def literal_display(item, code, support):
            if (not isinstance(item, dict) or not isinstance(item.get('factId'), str)
                    or item['factId'] not in facts):
                return False
            iid, fid = item.get('indicatorId'), item['factId']
            if iid is not None and not isinstance(iid, str):
                return False
            if (iid is None and fid not in support or iid is not None
                    and (iid not in indicators[code] or fid not in indicator_fact_ids[code][iid])):
                return False
            name, presentation = item.get('displayName'), item.get('recordedPresentation')
            if (not isinstance(name, str) or not name or name != name.strip() or len(name) > 80
                    or not isinstance(presentation, str) or not presentation or len(presentation) > 120
                    or presentation != presentation.strip()
                    or any(token in name + presentation for token in ('┋', '未核验', '主检结论', '\n'))
                    or any(token in name for token in ('?', '？'))):
                return False
            fact = facts[fid]
            originals = [str(value) for value in (fact.value, fact.status) if value not in (None, '')]
            if fact.value not in (None, '') and fact.unit:
                originals.append(str(fact.value) + str(fact.unit))
            return any(presentation in value for value in originals)
        authored = await generate(payload)
        wire_proposed = authored.get('links', []) if isinstance(authored, dict) else []
        journal['templateRiskContextWireProposed'] = deepcopy(wire_proposed)
        proposed = [restore(link) for link in wire_proposed] if isinstance(wire_proposed, list) else []
        journal['templateRiskContextProposed'] = deepcopy(proposed)
        counts = Counter(link.get('riskCode') for link in proposed if isinstance(link, dict))
        valid, errors = [], []
        for link in proposed:
            if not isinstance(link, dict) or link.get('riskCode') not in rows or counts[link['riskCode']] != 1:
                errors.append({'reason': 'unknown_or_duplicate_risk_context'})
                continue
            support, contrary = link.get('supportFactIds'), link.get('contraryFactIds')
            management = link.get('managementContextFactIds', [])
            if (not isinstance(support, list) or not isinstance(contrary, list)
                    or not isinstance(management, list)
                    or any(not isinstance(fid, str) or fid not in context_ids for fid in support + contrary + management)
                    or set(support).intersection(contrary)
                    or len(set(support)) > 5 or len(set(contrary)) > 2):
                errors.append({'riskCode': link['riskCode'], 'reason': 'invalid_context_fact_addresses'})
                continue
            displays = link.get('evidenceDisplay', [])
            if not isinstance(displays, list):
                displays = []
            valid_displays = [{**item, 'displayId': 'D' + str(index + 1)}
                for index, item in enumerate(displays) if literal_display(item, link['riskCode'], support)]
            if len(valid_displays) != len(displays):
                errors.append({'riskCode': link['riskCode'], 'reason': 'nonliteral_or_foreign_evidence_display'})
            valid.append({**link, 'supportFactIds': list(dict.fromkeys(support)),
                'contraryFactIds': list(dict.fromkeys(contrary)),
                'managementContextFactIds': list(dict.fromkeys(management)),
                'evidenceDisplay': valid_displays})
        journal['templateRiskContextAddressErrors'] = errors
        if not valid:
            journal['templateRiskContext'] = {'acceptedLinks': [], 'addedIndicators': 0}
            return data
        async def review_batch(batch):
            body = deepcopy(payload)
            wanted = {risk_wire[link['riskCode']] for link in batch}
            body.update(task='review_report_risk_context',
                riskRows=[row for row in payload['riskRows'] if row['riskCode'] in wanted],
                links=[{**link, 'riskCode': risk_wire[link['riskCode']],
                    'supportFactIds': [fact_wire[fid] for fid in link['supportFactIds']],
                    'contraryFactIds': [fact_wire[fid] for fid in link['contraryFactIds']],
                    'managementContextFactIds': [fact_wire[fid] for fid in link['managementContextFactIds']],
                    'evidenceDisplay': [{**item, 'factId': fact_wire[item['factId']]}
                        for item in link['evidenceDisplay']]} for link in batch],
                reviewInstructions=[
                    '逐事实独审个人相关性、真实原文和本人/家属范围；不是要求已确诊同病，也不批准新医学关系。全部临床与相反事实供核查，不要只挑支持。',
                    '只返回R/F短ID、保留原对应支持/反证列表的已通过子集。剔除提问当阳性、否定当病史、无关或原状态被改写；四布尔均真才pass。',
                    '独审每条evidenceDisplay的本人/亲属范围、项目简称忠实性、原文连续片段、数值单位及方向、本病实际相关性；只返回对应D短ID和独立布尔结果，不重抄或改写作者短句。正常反证只保内部，不靠通用疲劳、睡眠或减重目标给特定疾病凑证据。',
                    '保留本病真正关键的糖化/家族史等支持；管理背景可放managementContextFactIds，仅内部保存。既有C证据不能被临床补充全盘替换。'],
                outputSchema={'reviews': [{'riskCode': 'R1', 'verdict': 'pass|fail',
                    'supportFactIds': ['F1'], 'contraryFactIds': [], 'personalRelevance': True,
                    'subjectScopePreserved': True, 'literalResultPreserved': True, 'noNewDiagnosis': True,
                    'managementContextFactIds': [], 'evidenceDisplay': [{'displayId': 'D1',
                        'verdict': 'pass|fail', 'displayNameFaithful': True, 'literalSpanPreserved': True,
                        'subjectScopePreserved': True, 'valueUnitDirectionPreserved': True,
                        'supportsThisProblem': True, 'issues': []}], 'issues': []}]})
            return await generate(body)
        responses = await asyncio.gather(*(review_batch(valid[i:i+6]) for i in range(0, len(valid), 6)), return_exceptions=True)
        journal['templateRiskContextWireReviews'] = [deepcopy(r) for r in responses if not isinstance(r, BaseException)]
        journal['templateRiskContextReviewErrors'] = [type(r).__name__ for r in responses if isinstance(r, BaseException)]
        reviews = [restore(item) for response in responses if isinstance(response, dict)
            for item in response.get('reviews', []) if isinstance(item, dict)]
        journal['templateRiskContextReviews'] = deepcopy(reviews)
        by_code, accepted, added, projected = {}, [], 0, 0
        for review in reviews:
            by_code.setdefault(review.get('riskCode'), []).append(review)
        for link in valid:
            answers = by_code.get(link['riskCode'], [])
            answer = answers[0] if len(answers) == 1 else {}
            support, contrary = answer.get('supportFactIds'), answer.get('contraryFactIds')
            if (answer.get('verdict') != 'pass' or answer.get('issues') != []
                    or not all(answer.get(key) is True for key in ('personalRelevance', 'subjectScopePreserved', 'literalResultPreserved', 'noNewDiagnosis'))
                    or not isinstance(support, list) or not isinstance(contrary, list)
                    or any(fid not in link['supportFactIds'] for fid in support)
                    or any(fid not in link['contraryFactIds'] for fid in contrary)):
                continue
            row = rows[link['riskCode']]
            support, contrary = list(dict.fromkeys(support)), list(dict.fromkeys(contrary))
            display_answers = {}
            for item in answer.get('evidenceDisplay', []) if isinstance(answer.get('evidenceDisplay'), list) else []:
                if isinstance(item, dict) and isinstance(item.get('displayId'), str):
                    display_answers.setdefault(item.get('displayId'), []).append(item)
            displays = []
            for item in link['evidenceDisplay']:
                choices = display_answers.get(item['displayId'], [])
                reviewed = choices[0] if len(choices) == 1 else {}
                if (reviewed.get('verdict') == 'pass' and reviewed.get('issues') == []
                        and all(reviewed.get(key) is True for key in ('displayNameFaithful',
                            'literalSpanPreserved', 'subjectScopePreserved',
                            'valueUnitDirectionPreserved', 'supportsThisProblem'))
                        and literal_display(item, link['riskCode'], support)):
                    displays.append(item)
            def fact_sources(fid):
                fact = facts[fid]
                code = fact.source[:1] if fact.source[:2] in {'A:', 'B:', 'C:'} else (
                    'A' if fact.kind == 'clinical' else 'B' if fact.kind == 'self_reported' else 'C')
                refs = [SourceReference(source_code=code, record_id=fid, observed_at=fact.date)]
                if fact.source:
                    refs.append(SourceReference(source_code=code, record_id=fact.source, observed_at=fact.date))
                return refs
            replacements = {}
            for item in displays:
                if item.get('indicatorId') is not None:
                    replacements.setdefault(item['indicatorId'], []).append(item)
            visible, shown = [], set()
            for iid, original in indicators[link['riskCode']].items():
                selected = replacements.get(iid, [])
                if not selected:
                    visible.append(original)
                    continue
                _archive(data, [original], 'template_original_evidence_display')
                for item in selected:
                    identity = (item['factId'], item['displayName'], item['recordedPresentation'])
                    if identity in shown:
                        continue
                    shown.add(identity)
                    display = deepcopy(original)
                    display.name = item['displayName']
                    original_state = original.source_status
                    display.source_status = original_state if isinstance(original_state, str) \
                        and original_state.strip() in _EXPLICIT_SOURCE_STATES else item['recordedPresentation']
                    display.sources.extend(ref for ref in fact_sources(item['factId']) if ref not in display.sources)
                    visible.append(display)
                    projected += 1
            for item in displays:
                fid = item['factId']
                if item.get('indicatorId') is not None or fid not in support:
                    continue
                identity = (fid, item['displayName'], item['recordedPresentation'])
                if identity in shown or any(any(ref.record_id == fid for ref in old.sources) for old in visible):
                    continue
                shown.add(identity)
                visible.append(MetricAssessment(code='riskContext:' + fid, name=item['displayName'],
                    source_status=item['recordedPresentation'], sources=fact_sources(fid), availability='available'))
                added += 1
            row.related_indicators = visible
            for fid in support:
                refs = fact_sources(fid)
                row.sources.extend(ref for ref in refs if ref not in row.sources)
            management = answer.get('managementContextFactIds', [])
            management = [fid for fid in management if fid in link['managementContextFactIds']
                and fid not in support and fid not in contrary] if isinstance(management, list) else []
            accepted.append({'riskCode': link['riskCode'], 'supportFactIds': support,
                'contraryFactIds': contrary, 'managementContextFactIds': management,
                'evidenceDisplay': deepcopy(displays), 'rationale': link.get('rationale')})
            for group in getattr(data, '_template_layout', {}).get('groups', ()):
                if group.get('riskCode') == link['riskCode']:
                    group['riskContextFactIds'], group['contraryContextFactIds'] = support, contrary
                    group['managementContextFactIds'] = management
                    group['sourceFactIds'] = list(dict.fromkeys([*group.get('sourceFactIds', []), *support]))
        journal['templateRiskContext'] = {'acceptedLinks': accepted, 'addedIndicators': added,
            'projectedIndicators': projected, 'reviewBatches': len(responses),
            'displaySupportLimit': 5, 'internalContraryLimit': 2}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        journal['templateRiskContextError'] = type(error).__name__
    return data
