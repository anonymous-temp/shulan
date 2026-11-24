"""Source-first overview components and explicitly labelled illustrative scores.

Ratio state anchors are presentation rules, not clinical prediction models.
The original measurements and supplier modules are never changed here.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .evaluation_schema import AssessmentScore, SourceReference


COMPONENT_BLOCK = 'eval:assessmentScores:components'
FIBER_CODE = 'component:dietaryFiberNeed'
COMPONENT_CODES = {'component:beScore', 'component:fbScore', FIBER_CODE}
SOURCE_CODES = {
    'beScore': {'beScore', 'beBalanceScore', 'beRatioScore', 'colonizationResistanceScore'},
    'fbScore': {'fbScore', 'fbEnergyMetabolismScore', 'fbRatioScore', 'energyMetabolismScore'},
    'dietaryFiberNeed': {'dietaryFiberNeed', 'dietaryFiberDemand', 'dietaryFiberDemandScore'},
}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _metrics(data):
    return [*data.microbiome_assessment.overall_structure, *data.microbiome_assessment.health_metrics]


def _native_score(data, code):
    return next((row for row in _metrics(data) if row.code in SOURCE_CODES[code]
        and row.sources and (number := _number(row.value)) is not None and 0 <= number <= 100
        and not str(row.explanation or '').startswith(('AI综合', '规则示意'))), None)


def _fact_digest(ids, facts):
    from .evaluation_scores import _source_fact_digest
    return _source_fact_digest(ids, facts)


def _refs(rows):
    result = []
    for row in rows:
        for ref in row.sources:
            if ref not in result:
                result.append(ref.model_copy(deep=True))
    return result


def _be_state(row):
    value = _number(row.value)
    if value is None or value < 0:
        return None
    reference = str(row.reference_range or '')
    status = str(row.source_status or '')
    if not row.sources:
        return None
    declared = ('重度失衡' if '重度' in status else '失衡' if '失衡' in status
        else '正常' if re.search(r'正常|平衡', status) else None)
    normal_cut = re.search(r'(>=|≥|>|＞)\s*(\d+(?:\.\d+)?)', reference)
    if normal_cut and Decimal(normal_cut[2]) != 1:
        return None
    normal = value > 1 or value == 1 and normal_cut and normal_cut[1] in {'>=','≥'}
    severe_rule = bool(re.search(r'(?:<|＜)\s*0[.]01[^；;。]{0,20}(?:重度|严重)|'
        r'(?:重度|严重)[^；;。]{0,20}(?:<|＜)\s*0[.]01', reference))
    numeric = ('正常' if normal else '重度失衡' if severe_rule and value < Decimal('.01')
        else '失衡' if severe_rule or value < 1 else None)
    if normal_cut and declared and ((declared == '正常') != bool(normal)):
        return None
    if severe_rule and declared and declared != numeric:
        return None
    level = declared or (numeric if normal_cut or severe_rule else None)
    return (level, {'正常':90,'失衡':40,'重度失衡':15}[level]) if level else None


def _fb_state(row):
    value = _number(row.value)
    if value is None or value < 0 or not row.sources:
        return None
    reference = str(row.reference_range or '').strip()
    interval = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[~～–—-]\s*(\d+(?:\.\d+)?)\s*', reference)
    status = str(row.source_status or '')
    if interval:
        lower, upper = map(Decimal, interval.groups())
        if lower >= upper:
            return None
        normal = lower <= value <= upper
        if ('正常' in status or '平衡' in status) and not normal:
            return None
        return ('正常', 90) if normal else ('注意', 65)
    # A literal method-specific direction may support a state illustration;
    # it does not supply a universal numeric reference interval.
    if re.search(r'偏高|偏低|异常|失衡', status):
        return '注意', 65
    if re.search(r'正常|平衡', status):
        return '正常', 90
    return None


def _diet_inventory(data, document):
    facts = {fact.fact_id: fact for fact in document.facts}
    diet_rows = [row for row in data.medical_assessment.trigger_analysis if row.code == 'diet']
    own = {source.record_id for row in diet_rows for source in row.sources}
    # General taxon descriptions, absent intake and unrelated water/meal-time
    # facts cannot become a personal fibre measurement.
    result = []
    for fid in sorted(own):
        fact = facts.get(fid)
        if fact is None or fact.kind != 'self_reported':
            continue
        answer = re.split(r'(?:回答|答案)[：:]', fact.value)[-1].strip().rstrip('。')
        if not answer or re.fullmatch(r'不清楚|不知道|未知|不详|未回答|未提供|不确定', answer):
            continue
        if not re.search(r'蔬菜|水果|全谷|杂粮|豆类|膳食纤维|食物纤维', fact.name + ' ' + fact.value):
            continue
        sources = [ref.model_dump(mode='json', by_alias=True) for row in diet_rows
            for ref in row.sources if ref.record_id == fid]
        result.append({'name': fact.name, 'value': fact.value, 'unit': fact.unit,
            'sourceStatus': fact.status, 'assessmentLevel': None,
            'referenceRange': fact.reference_range, 'factIds': [fid], 'sourceRefs': sources,
            'useAs': 'patient_report', 'assessmentOrigin': 'source'})
    return result


def project_component_scores(data, document):
    """Project only derived slots; native component measurements stay primary."""
    data.health_overview.assessment_chart = [row for row in data.health_overview.assessment_chart
        if row.code not in COMPONENT_CODES]
    for code, ratio_code, name, state_fn in (
        ('beScore', 'beRatio', 'B/E 定植抗力评估评分', _be_state),
        ('fbScore', 'fbRatio', 'F/B 组成状态评估评分', _fb_state)):
        if _native_score(data, code) is not None:
            continue
        row = next((item for item in _metrics(data) if item.code == ratio_code), None)
        state = state_fn(row) if row is not None else None
        if state is None:
            continue
        level, score = state
        if code == 'beScore':
            meaning = ('定植抗力相关的菌群组成保持平衡，是当前结构评估中的保留环节。'
                if level == '正常' else '定植抗力相关的菌群组成失衡，是当前需要关注的结构环节。')
        else:
            meaning = ('厚壁菌门与拟杆菌门的组成比值保持正常。' if level == '正常' else
                '厚壁菌门与拟杆菌门的组成比值偏离参考区间，需关注这两个菌门的组成变化。')
        data.health_overview.assessment_chart.append(AssessmentScore(code='component:' + code,
            name=name, score=score, assessmentLevel=level,
            explanation=meaning, sources=_refs([row]), availability='available'))
    diet_inventory = _diet_inventory(data, document)
    if _native_score(data, 'dietaryFiberNeed') is None and diet_inventory:
        sources = []
        for item in diet_inventory:
            for ref in item['sourceRefs']:
                parsed = SourceReference.model_validate(ref)
                if parsed not in sources:
                    sources.append(parsed)
        slot = AssessmentScore(code=FIBER_CODE, name='膳食纤维需求评估评分', sources=sources)
        data.health_overview.assessment_chart.append(slot)
    return data


def build_component_score_targets(data, document):
    """Ask for an intake-based illustration, never infer an assay or grams."""
    project_component_scores(data, document)
    if _native_score(data, 'dietaryFiberNeed') is not None:
        return []
    inventory = _diet_inventory(data, document)
    if not inventory:
        return []
    facts = {fact.fact_id: fact for fact in document.facts}
    ids = sorted({fid for item in inventory for fid in item['factIds']})
    target = {'code': FIBER_CODE, 'kind': 'component', 'name': '膳食纤维需求综合示意评分',
        'scoreRequired': True, 'allowedFactIds': ids, 'requiredFactIds': [],
        'clinicalPriorities': [], 'inventory': inventory, 'allowedLevels': ['充足', '需求增加'],
        'scale': {'minimum': 0, 'maximum': 100, 'direction': 'higher_is_better'},
        'scoringRubric': {'version': 'fiber-intake-illustration-v1', 'scope': 'internal_ai_illustration_only',
            'anchors': [{'assessmentLevel': '充足', 'scoreAnchor': 90, 'illustrativeRange': [90, 90]},
                {'assessmentLevel': '需求增加', 'scoreAnchor': 65, 'illustrativeRange': [65, 65]}]},
        'rules': ['本项为AI摄入状态示意，非原报告检测分；只按实际蔬果、全谷、豆类等摄入记录归纳。',
            '先读完整真实摄入记录，存在明确摄入薄弱线索时选需求增加及65；支持较充分且结构保留时选充足及90。',
            '只有泛化描述、没有实际摄入线索或无法判断时不返回此项；不凭症状或菌丰度单独断定膳食纤维不足。',
            '不计算每日克数、不推算未知份量，不以半乳糖、淀粉蔗糖、抗炎或短链脂肪酸分替代膳食纤维检测分。',
            '无需把未知摄入维度评为正常；表述仅给已有饮食结构的关注方向，不声称已证实营养缺乏或菌群需求量。',
            'assessmentLevel使用本target.allowedLevels，score严格使用对应示意锚点，不计算百分比或摄入达标率。']}
    target['sourceFactDigest'] = _fact_digest(ids, facts)
    target['sourceDigest'] = hashlib.sha256(json.dumps(target, ensure_ascii=False, sort_keys=True,
        allow_nan=False).encode()).hexdigest()
    return [target]


def apply_component_score_rows(data, document, rows=None, *, reason_validator=None):
    """Apply a live independently reviewed result and its exact-source restore."""
    from .evaluation_scores import _load_rows, _store_rows, assessment_metadata_reviewed
    targets = {item['code']: item for item in build_component_score_targets(data, document)}
    restoring = rows is None
    if restoring:
        rows = _load_rows(document, COMPONENT_BLOCK, 'scores')
    rows = rows if isinstance(rows, list) else []
    validated, errors = [], []
    for code, target in targets.items():
        candidates = [row for row in rows if isinstance(row, dict) and row.get('code') == code]
        reason = None
        if len(candidates) != 1:
            reason = 'missing_or_duplicate'
        else:
            row = candidates[0]
            ids = row.get('factIds')
            expected = {'充足': 90, '需求增加': 65}.get(row.get('assessmentLevel'))
            if type(row.get('score')) is not int or expected != row['score']:
                reason = 'invalid_component_anchor'
            elif not isinstance(ids, list) or not ids or not all(isinstance(fid, str) for fid in ids) \
                    or not set(ids) <= set(target['allowedFactIds']):
                reason = 'unbound_fact'
            elif not isinstance(row.get('judgment'), str) or not row['judgment'].strip() \
                    or not isinstance(row.get('explanation'), str) or not row['explanation'].strip():
                reason = 'missing_judgment'
            elif re.search(r'\d\s*(?:克|g/日|g/d)|确诊|营养缺乏|实测.{0,5}分|检测.{0,5}分', row['judgment']):
                reason = 'unsupported_measurement_or_dose'
            elif reason_validator is not None and reason_validator(row, target):
                reason = 'unsupported_explanation'
            elif restoring and row.get('sourceDigest') != target['sourceDigest']:
                reason = 'source_changed'
            elif not assessment_metadata_reviewed(document, row, target):
                reason = 'independent_review_required'
        slot = next(item for item in data.health_overview.assessment_chart if item.code == code)
        if reason:
            slot.score, slot.assessment_level, slot.explanation = None, None, None
            slot.availability = 'unavailable'
            errors.append('AI_SCORE_INVALID:' + code + ':' + reason)
            continue
        saved = deepcopy(row)
        saved['sourceDigest'] = target['sourceDigest']
        validated.append(saved)
        slot.score, slot.assessment_level = row['score'], row['assessmentLevel']
        slot.explanation = 'AI综合示意分：' + row['judgment'].strip()
        slot.sources = [SourceReference.model_validate(ref) for item in target['inventory']
            if set(ids).intersection(item['factIds']) for ref in item['sourceRefs']]
        slot.availability = 'available'
    if not restoring:
        _store_rows(document, COMPONENT_BLOCK, 'scores', validated)
    return errors
