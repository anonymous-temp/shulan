"""Request-local, independently reviewed disease and domain health scores.

Higher scores indicate better current assessed health. They are neither a
conversion of source model values nor a clinically validated probability.
Only a live receipt bound to the exact current facts and risk snapshot may
authorise publication; serialising the document cannot transfer approval.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re

from .evaluation_catalog import RISK_CATEGORY_DEFINITIONS, _fixed_standard_number, presentation_health_level
from .evaluation_risk_assessment import (
    approved_assessments, editorial_risk_representatives, risk_level,
    risk_priority_key, is_publishable_disease_assessment, disease_assessment_bases,
)
from .evaluation_health_wire import (
    FactIdCodec, author_health_wave, review_health_wave, clarify_health_rejections,
)

_FACTORS = ('severity', 'persistence', 'clinicalBurden', 'personalRelevance')
_RUBRIC = {
    'version': 'seven-domain-attention-v2',
    'scale': '0–100综合关注指数；高分表示更需关注，不是患病概率或健康得分',
    'bands': [{'min': 0, 'max': 24, 'level': '正常'},
              {'min': 25, 'max': 49, 'level': '注意'},
              {'min': 50, 'max': 74, 'level': '中风险'},
              {'min': 75, 'max': 100, 'level': '高风险'}],
    'factors': {
        'severity': '同维当前问题或已审潜在风险的严重性、功能影响与紧迫性；不能把来源信号当确诊',
        'persistence': '真实报告的持续时间、反复或进展；未询问和时间不详不能当阴性或扣分',
        'clinicalBurden': '已存在疾病、客观临床异常及本人症状的整体负担；同病与共用指标只计一次',
        'personalRelevance': '已审同病关系、真实个人诱因、适用限制与来源信号的个人相关性',
    },
    'rules': [
        '按整维的真实健康负担和已审风险综合判断，不复制最高单病分，不按条目数量计分，不平均不同原始量纲。',
        '同维低关注条目不能稀释显著当前问题；同一问题的多个别名、指标或关联信号不能累加抬分。',
        '临床问题为核心；菌群与功能证据辅助个人关联，只有真实菌群信号时仍可评潜在关注程度。',
        '当前问题与潜在风险区分；严重程度、个人相关性和证据可靠程度不同，不把弱证据等同低风险。',
        '原疾病模型数值和等级仅为输入，不乘100、不做100减、不机械照搬；不能借他病模型为本病原指数。',
        '只有同病真实正常检查或明确否认的同一症状可作限制；未确诊、未询问不作阴性。',
        '无有效已审资料的维度不评估；有已审低关注结果的维度可以评估，不能因正常而留空。',
        '四因素归因必须绑定输入riskId及其真实factId/claimId；不得新增病因、症状或关系。',
    ],
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False, default=str).encode()).hexdigest()


def _snapshot(document):
    approved = sorted(approved_assessments(document), key=risk_priority_key)
    representatives = editorial_risk_representatives(document, approved)
    # A symptom/assay and a reviewed disease may share a catalogue identity.
    # They are different scopes; the former cannot occupy the latter's slot.
    selected, seen = [], set()
    for risk in representatives:
        key = (risk.category, risk.disease_id, getattr(risk, 'entity_kind', None))
        if key not in seen:
            selected.append(risk)
            seen.add(key)
    facts = {fact.fact_id: fact for fact in document.facts}
    source = {'facts': [fact.model_dump(mode='json', by_alias=True)
            for fact in sorted(document.facts, key=lambda fact: fact.fact_id)],
        'approved': [risk.model_dump(mode='json', by_alias=True) for risk in approved],
        'representativeIds': [risk.risk_id for risk in selected],
        'subject': getattr(document.risk_registry, 'subject_snapshot', {})}
    return selected, facts, _digest(source)


def _disease_health_targets(document, dimension_risks):
    """Build disease targets independently of observation-first emphasis."""
    bases = disease_assessment_bases(document)
    candidates = {risk.risk_id: risk for risk in [*approved_assessments(document), *dimension_risks]
        if is_publishable_disease_assessment(risk) and risk.risk_id in bases
        and bases[risk.risk_id]['assessment'] is risk
        and bases[risk.risk_id]['basis'] in {'clinical_classification', 'individual_risk'}}
    result, seen = [], set()
    for risk in sorted(candidates.values(), key=lambda row: (
            0 if row.certainty in {'confirmed', 'current_problem'} else 1, risk_priority_key(row))):
        identity = (risk.category, risk.disease_id)
        if identity not in seen:
            result.append(risk)
            seen.add(identity)
    return result


@dataclass(frozen=True)
class _DimensionReceipt:
    scope: str
    decisions: dict
    proof: str


@dataclass(frozen=True)
class _HealthScoreReceipt:
    scope: str
    dimensions: dict
    risks: dict
    rubric: str
    proof: str


def approved_dimension_scores(document):
    """Only the fresh health rubric's independent decisions can be published."""
    receipt = getattr(document, '_dimension_score_receipt', None)
    if not isinstance(receipt, _HealthScoreReceipt):
        return {}
    _, _, scope = _snapshot(document)
    if (receipt.scope != scope or receipt.rubric != _digest(_HEALTH_RUBRIC)
            or receipt.proof != _digest({'dimensions': receipt.dimensions, 'risks': receipt.risks})
            or any(value.get('reviewProtocolRevision') != _HEALTH_REVIEW_PROTOCOL
                for value in [*receipt.dimensions.values(), *receipt.risks.values()])):
        return {}
    return deepcopy(receipt.dimensions)


def approved_health_scores(document):
    """Return risk_id -> health score, without changing any risk approval."""
    receipt = getattr(document, '_dimension_score_receipt', None)
    if not isinstance(receipt, _HealthScoreReceipt):
        return {}
    _, _, scope = _snapshot(document)
    if (receipt.scope != scope or receipt.rubric != _digest(_HEALTH_RUBRIC)
            or receipt.proof != _digest({'dimensions': receipt.dimensions, 'risks': receipt.risks})
            or any(value.get('reviewProtocolRevision') != _HEALTH_REVIEW_PROTOCOL
                for value in [*receipt.dimensions.values(), *receipt.risks.values()])):
        return {}
    return deepcopy(receipt.risks)


def approved_dimension_fact_ids(document, code=None):
    """All actually reviewed attributions, including source-only functions.

    Read this batch once during projection. A risk-only support list cannot
    account for a functional source premise that deliberately has no disease
    owner. Serialized decisions never grant this helper any authority.
    """
    decisions = approved_dimension_scores(document)
    known = {fact.fact_id for fact in document.facts}
    result = {}
    for key, decision in decisions.items():
        selected = []
        for factor in decision.get('factors', []):
            selected.extend(factor.get('factQuotes', {}))
            selected.extend(factor.get('sourceFactIds', []))
            for attribution in factor.get('claimAttributions', []):
                selected.extend(attribution.get('factIds', []))
            for attribution in factor.get('sourceFactAttributions', []):
                selected.extend([attribution.get('factId'), attribution.get('sourceFactId')])
        result[key] = [fid for fid in dict.fromkeys(selected) if isinstance(fid, str) and fid in known]
    return result.get(code, []) if code is not None else result


def mean_dimension_score(rows):
    """Arithmetic mean of exactly the configured supplied domains, never imputation."""
    if rows is None:
        return None
    expected = {code for code, _ in RISK_CATEGORY_DEFINITIONS}
    rows = list(rows)
    if len(rows) != len(expected):
        return None
    values = {}
    for row in rows:
        code = row.get('code') if isinstance(row, dict) else getattr(row, 'code', None)
        score = row.get('score') if isinstance(row, dict) else getattr(row, 'score', None)
        if not isinstance(code, str) or code not in expected or code in values:
            return None
        number = _fixed_standard_number(score)
        if number is None or not 0 <= number <= 100:
            return None
        values[code] = number
    if set(values) != expected:
        return None
    mean = (sum(values.values(), Decimal(0)) / Decimal(len(expected))).quantize(
        Decimal('0.1'), rounding=ROUND_HALF_UP)
    return int(mean) if mean == mean.to_integral_value() else float(mean)


def build_overall_dimension_score(document, chart_rows):
    """Derive a total only from this request's unchanged reviewed domain scores."""
    from .evaluation_schema import AssessmentScore
    decisions = approved_dimension_scores(document)
    result = AssessmentScore(code='overall', name='健康管理评估指数',
        availability='partial' if decisions else 'unavailable')
    expected = {code for code, _ in RISK_CATEGORY_DEFINITIONS}
    rows = list(chart_rows) if chart_rows is not None else []
    mean = mean_dimension_score(rows)
    if (set(decisions) != expected or mean is None
            or any(not hasattr(row, 'score') or not hasattr(row, 'sources')
                or not hasattr(row, 'evidence_refs') for row in rows)):
        return result
    if any(_fixed_standard_number(row.score) != _fixed_standard_number(decisions[row.code]['index'])
            for row in rows):
        return result
    result.score, result.assessment_level = mean, presentation_health_level(mean)
    result.availability = ('available' if all(decisions[code].get('availability') == 'available'
        for code in expected) else 'partial')
    for row in rows:
        for source in row.sources:
            if source not in result.sources:
                result.sources.append(source.model_copy(deep=True))
    result.evidence_refs = sorted({number for row in rows for number in row.evidence_refs})
    return result


def _encode_dimension_item(item):
    """Local claim names are unique only inside their owning risk."""
    value = deepcopy(item)
    mapping = []
    for ri, risk in enumerate(value['risks'], 1):
        aliases = {}
        for ci, claim in enumerate(risk['claims'], 1):
            original = claim['claimId']
            qualified = f'R{ri}C{ci}'
            aliases[original] = qualified
            mapping.append({'claimId': qualified, 'riskId': risk['riskId'],
                'sourceClaimId': original, 'factIds': sorted(claim['factQuotes'])})
            claim['claimId'] = qualified
        for field in ('lifestyleActions', 'clinicalActions'):
            for action in risk.get(field, []):
                action['claimIds'] = [aliases.get(cid, cid) for cid in action.get('claimIds', [])]
    value['claimIdMap'] = mapping
    return value


def _normalise_factor_ids(row, key='factors'):
    """Repeated identical IDs are formatting, not additional contribution.

    Ambiguous legacy names are never guessed: they still fail membership in
    the qualified packet and must be authored again and independently reviewed.
    """
    value = deepcopy(row)
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return value
    for factor in value[key]:
        if not isinstance(factor, dict):
            continue
        for field in ('riskIds', 'factIds', 'claimIds'):
            ids = factor.get(field)
            if isinstance(ids, list) and all(isinstance(identifier, str) for identifier in ids):
                factor[field] = list(dict.fromkeys(ids))
    return value


def _receipt_decision(row, item):
    """Retain the independently reviewed wire IDs and their exact owners."""
    value = deepcopy(row)
    mapping = {item['claimId']: item for item in item['claimIdMap']}
    for factor in value['factors']:
        factor['claimAttributions'] = [deepcopy(mapping[cid]) for cid in factor['claimIds']]
        if factor.get('sourceFactIds'):
            source_ids = set(factor['sourceFactIds'])
            factor['sourceFactAttributions'] = [deepcopy(p) for p in item.get('premiseCatalog', [])
                if p.get('sourceFactId') in source_ids]
    return value


def _factor_schema():
    return [{'name': name, 'assessment': 'low|moderate|high|not_assessed',
        'riskIds': ['该因素采用的输入riskId'], 'factIds': ['同risk实际factId'],
        'claimIds': ['只用claimIdMap中R1C1等唯一claimId']} for name in _FACTORS]


def _decision_errors(row, item, facts):
    if not isinstance(row, dict) or set(row) != {'code', 'index', 'level', 'factors'}:
        return ['unexpected_dimension_fields']
    if row['code'] != item['code'] or type(row['index']) is not int or not 0 <= row['index'] <= 100:
        return ['invalid_dimension_index']
    if row['level'] != risk_level(row['index']):
        return ['dimension_level_index_mismatch']
    factors = row['factors']
    if (not isinstance(factors, list) or len(factors) != len(_FACTORS)
            or any(not isinstance(value, dict) or not isinstance(value.get('name'), str) for value in factors)
            or {value['name'] for value in factors} != set(_FACTORS)):
        return ['dimension_factors_missing']
    risks = {risk['riskId']: risk for risk in item['risks']}
    active = False
    for factor in factors:
        if (set(factor) != {'name', 'assessment', 'riskIds', 'factIds', 'claimIds'}
                or not isinstance(factor['assessment'], str)
                or factor['assessment'] not in {'low', 'moderate', 'high', 'not_assessed'}):
            return ['invalid_dimension_factor']
        for key in ('riskIds', 'factIds', 'claimIds'):
            if (not isinstance(factor[key], list) or any(not isinstance(value, str) for value in factor[key])
                    or len(set(factor[key])) != len(factor[key])):
                return ['invalid_dimension_factor_ids']
        if not set(factor['riskIds']) <= risks.keys():
            return ['foreign_dimension_risk']
        own = [risks[rid] for rid in factor['riskIds']]
        allowed_facts = {fid for risk in own for field in ('supportFactIds', 'opposingFactIds',
            'backgroundFactIds', 'sourceModelFactIds') for fid in risk[field]}
        allowed_claims = {claim['claimId'] for risk in own for claim in risk['claims']}
        if not set(factor['factIds']) <= allowed_facts & facts.keys() or not set(factor['claimIds']) <= allowed_claims:
            return ['unbound_dimension_factor']
        if factor['assessment'] != 'not_assessed':
            if not factor['riskIds'] or not factor['factIds']:
                return ['dimension_factor_without_basis']
            active = True
        elif factor['riskIds'] or factor['factIds'] or factor['claimIds']:
            return ['unassessed_dimension_factor_has_basis']
    return [] if active else ['all_dimension_factors_unassessed']


def _unused_review_background_ids(review, decision, item):
    """An extra checked background is not an extra numerical premise."""
    known = {fact['factId'] for fact in item.get('facts', [])}
    background = {fid for risk in item['risks'] for fid in risk['backgroundFactIds']}
    bound = {fid for risk in item['risks'] for claim in risk['claims'] for fid in claim['factQuotes']}
    bound.update(fid for entry in item.get('claimIdMap', []) for fid in entry['factIds'])
    for owner, field in ((decision, 'factors'), (review, 'factorChecks')):
        rows = owner.get(field)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get('factIds'), list):
                    bound.update(fid for fid in row['factIds'] if isinstance(fid, str))
    return (known & background) - bound


def _review_errors(review, decision, item):
    expected = {'code', 'index', 'level', 'valid', 'scoreJustified', 'allFactorsChecked',
        'consideredRiskIds', 'consideredOpposingFactIds', 'checkedFactorNames',
        'factorChecks', 'unsupportedFactorNames', 'missingNotUsedAsNegative', 'duplicateBurdenCounted'}
    if not isinstance(review, dict) or set(review) != expected:
        return ['dimension_review_missing']
    risks = item['risks']
    required_risks = {risk['riskId'] for risk in risks}
    required_opposition = {fid for risk in risks for fid in risk['opposingFactIds']}
    if (review['code'] != decision['code'] or type(review['index']) is not int
            or review['index'] != decision['index'] or review['level'] != decision['level']
            or any(review[key] is not True for key in ('valid', 'scoreJustified', 'allFactorsChecked',
                'missingNotUsedAsNegative')) or review['duplicateBurdenCounted'] is not False
            or review['unsupportedFactorNames'] != []):
        return ['dimension_decision_not_independently_approved']
    for key, expected_ids in (('consideredRiskIds', required_risks),
            ('consideredOpposingFactIds', required_opposition), ('checkedFactorNames', set(_FACTORS))):
        value = review[key]
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            return ['incomplete_dimension_factor_review']
        actual = set(value)
        if key == 'consideredOpposingFactIds':
            # Required opposition is never optional. A reviewer may also
            # acknowledge supplied background that contributes to no factor
            # or claim; that metadata cannot create a new scoring premise.
            if (not expected_ids <= actual or not actual - expected_ids <=
                    _unused_review_background_ids(review, decision, item)):
                return ['incomplete_dimension_factor_review']
        elif actual != expected_ids:
            return ['incomplete_dimension_factor_review']
    checks = review['factorChecks']
    if not isinstance(checks, list) or len(checks) != len(_FACTORS):
        return ['dimension_factor_checks_missing']
    for factor in decision['factors']:
        own = [check for check in checks if isinstance(check, dict) and check.get('name') == factor['name']]
        if (len(own) != 1 or set(own[0]) != {*factor, 'valid', 'supportsAssessment'}
                or own[0]['valid'] is not True or own[0]['supportsAssessment'] is not True
                or any(own[0].get(key) != value for key, value in factor.items())):
            return ['dimension_factor_not_independently_bound']
    return []


_HEALTH_WEIGHTS = {'currentBurden': 40, 'functionalImpact': 25, 'persistence': 15, 'personalRelevance': 20}
_HEALTH_ANCHORS = (0, 25, 50, 75, 90, 100)
_HEALTH_AUTHOR_PROTOCOL = 'health-premise-author-v1'
_HEALTH_RUBRIC = {
    'version': 'integrated-health-v5',
    'authorProtocolRevision': _HEALTH_AUTHOR_PROTOCOL,
    'reviewProtocolRevision': 'health-premise-atomic-review-v5',
    'scale': '0–100综合健康评分，高分表示当前综合健康状态较好、风险关注较低；项目产品量表，不是患病概率或已验证医学量表',
    'weights': _HEALTH_WEIGHTS,
    'bands': [{'min': 80, 'max': 100, 'level': '低风险'},
        {'min': 60, 'maxExclusive': 80, 'level': '中风险'},
        {'min': 0, 'maxExclusive': 60, 'level': '高风险'}],
    'anchors': {
        '100': '对本因素有明确同病正常结果、明确同症状否认或无影响的真实记录，不以缺失、未确诊或未问代替',
        '90': '确有适用的轻微或非特异线索，但尚未显示明显同病影响；不能凭无临床资料判90',
        '75': '有来源支持的轻度异常、轻度症状或已审相关风险，健康影响较轻',
        '50': '已审依据显示明确中等健康负担，或实际持续症状/功能影响达到中等程度；多项轻度偏离不能只因数量多评为50',
        '25': '有明显/重度负担、持续功能影响或高度个人相关的已审风险，需要重点改善',
        '0': '有真实危急状态、严重器官功能影响或紧迫高危事实；不能仅据菌群高风险模型给0',
    },
    'factors': {
        'currentBurden': '本病或整维当前客观异常和症状的程度；异常的程度需要原参考范围、实际结论或已审适用关系支持',
        'functionalImpact': '实际不适及对生活、器官功能、活动能力的影响；仅有指标异常不能编造生活受限',
        'persistence': '本人实际持续、反复、进展或恢复情况；只有一次检测或时间不详为unknown，不是健康100或轻度90',
        'personalRelevance': '已审同病信号、本人诱因、家族背景与适用科学关系所支持的个人健康影响；来源风险仅一个输入',
    },
    'rules': [
        '本次重新评估各因素健康锚点，不从旧risk.index做100减，不沿用旧维度分，不为图形分散拉分。',
        '每个因素只选0/25/50/75/90/100或null；具体程度由真实factQuotes和已审claim支持，不插入新医学断言。',
        '有资料但不能评定该因素时applicability=unknown，score=null；确实不适用为not_applicable/null；都不算0或100。',
        '当前负担、生活影响、持续性只能用真实医学检查/本人报告；菌群信号不能冒充已发生症状或已确诊疾病。',
        '仅有同病来源风险信号时仍可评个人相关性，但不能把原模型直接换成健康分；其他未知因素null，整体partial。',
        '同病直接正常结果或明确同症状否认可作限制，未提及、未确诊、缺少家族史或未查不是阴性。',
        '同病反证、实际重要异常和全部同病已审资料都须考虑；不漏严重当前问题，不挑选健康项稀释真实异常。',
        '单病保持原临床身份和certainty；整维综合本维全部已审问题，不平均病种、不按数量计分，不复制最高单病。',
        '同一事实/别名/重复来源只算同一依据；相同因素锚点允许同分，只有实际不同程度才异分。',
        '多个轻度异常不能按数量改称中等负担；分档须依据实际程度、持续/功能影响或已审适用分层，不硬编码个体检验值。',
        'currentBurden的50/25/0必须有本人来源明确的中度/重度/危急程度依据；异常计数、症状出现、频次和持续时间本身不能代替该程度。程度未分的真实当前问题不因此判正常，也不能凭常见轻症名给重档。',
        '代码按已评因素实际权重归一计算，保留一位小数；未评因素仍为null并标partial，真正没有有效依据不输出分数。',
        '这次评分不修改原报告原数值、原等级，不修改已有风险临床判断和其独立审批。',
    ],
}


def health_score_from_factors(factors):
    """Deterministic product rubric; unknown factors never become healthy."""
    if (not isinstance(factors, list) or len(factors) != len(_HEALTH_WEIGHTS)
            or any(not isinstance(factor, dict) or not isinstance(factor.get('name'), str) for factor in factors)
            or {factor.get('name') for factor in factors} != _HEALTH_WEIGHTS.keys()):
        return None
    numerator, denominator = Decimal(0), Decimal(0)
    for factor in factors:
        state, score = factor.get('applicability'), factor.get('score')
        if state in {'unknown', 'not_applicable'} and score is None:
            continue
        if state != 'assessed' or type(score) is not int or score not in _HEALTH_ANCHORS:
            return None
        weight = Decimal(_HEALTH_WEIGHTS[factor['name']])
        numerator += weight * Decimal(score)
        denominator += weight
    if not denominator:
        return None
    score = (numerator / denominator).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    return int(score) if score == score.to_integral_value() else float(score)


def _health_factor_schema():
    return [{'name': name, 'score': '0|25|50|75|90|100|null',
        'applicability': 'assessed|unknown|not_applicable',
        'riskIds': ['所用输入riskId'], 'factQuotes': {'真实factId': '该fact.value逐字片段'},
        'claimIds': ['本包claimIdMap中的唯一claimId；unknown时riskIds/claimIds=[]、factQuotes={}']}
        for name in _HEALTH_WEIGHTS]


def _health_author_schema(item):
    return {'protocolRevision': _HEALTH_AUTHOR_PROTOCOL, 'code': item['code'],
        'factors': [{'name': name, 'score': '0|25|50|75|90|100|null',
            'applicability': 'assessed|unknown|not_applicable',
            'premiseIds': ['只选allowedPremiseIdsByFactor本因素允许的premiseId；未知时[]']}
            for name in _HEALTH_WEIGHTS]}


def _clinical_observation(fact):
    return (fact.kind == 'clinical' and not fact.fact_id.startswith('auxiliary-fact-')
        and not any(word in str(fact.source or '').lower() for word in ('microbiome', '菌群', '宏基因')))


def _health_premise_role(risk, claim, fact, quote, *, opposing=False):
    """Reuse reviewed observation scopes; never infer symptoms from behavior."""
    if _clinical_observation(fact):
        return 'observed_clinical_state', ['currentBurden', 'personalRelevance']
    if fact.kind != 'self_reported':
        return 'source_signal', ['personalRelevance']
    observed = (not opposing and (
        risk.get('entityKind') == 'symptom' and risk.get('certainty') in {'self_reported', 'current_problem', 'confirmed'}
        or claim.get('role') == 'diagnostic_principle'
        and risk.get('entityKind') == 'disease' and risk.get('certainty') in {'current_problem', 'confirmed'}))
    if observed:
        eligible = ['currentBurden', 'personalRelevance']
        # A reviewed symptom's explicit temporal excerpt is different from
        # the duration of an exposure or a bare specimen/report timestamp.
        if isinstance(fact.date, str) and fact.date.strip() and fact.date in quote:
            eligible.append('persistence')
        return 'observed_symptom', eligible
    # This includes short sleep duration, diet, activity, family/diagnosis
    # history, and unresolved self-report prose. None proves current illness.
    # An opposing history such as "no known diagnosis" remains mandatory
    # review context, but is not a numerical premise for symptom absence or
    # a healthier score. Clinical same-disease counter-observations above
    # retain their actual eligibility.
    return ('opposing_history', []) if opposing else ('reported_context', ['personalRelevance'])


def _documented_current_degree(fact):
    """Source severity, never a new disease-risk or numeric health score."""
    levels = {
        '轻度': 'mild', '轻微': 'mild', '轻度异常': 'mild', '轻度升高': 'mild', '边缘升高': 'mild',
        '中度': 'moderate', '中度异常': 'moderate', '中度升高': 'moderate',
        '重度': 'severe', '重度异常': 'severe', '重度升高': 'severe',
        '危急': 'critical', '危急值': 'critical',
    }
    if fact.kind not in {'clinical', 'self_reported'} or not _clinical_observation(fact) and fact.kind != 'self_reported':
        return None
    status = str(fact.status or '').strip()
    if status in levels:
        return {'degree': levels[status], 'basis': 'explicit_source_status', 'sourceStatus': status}
    # Concentration reference categories, not ASCVD risk or treatment targets.
    # These primary-care ranges are never extrapolated to other units/items.
    name = re.sub(r'[\s()（）_/-]+', '', fact.name).lower()
    aliases = {
        'tg': ('triglycerides', Decimal('1.7'), Decimal('2.3')),
        '甘油三酯': ('triglycerides', Decimal('1.7'), Decimal('2.3')),
        '甘油三酯tg': ('triglycerides', Decimal('1.7'), Decimal('2.3')),
        'triglycerides': ('triglycerides', Decimal('1.7'), Decimal('2.3')),
        'ldlc': ('ldl_cholesterol', Decimal('3.4'), Decimal('4.1')),
        'ldlcholesterol': ('ldl_cholesterol', Decimal('3.4'), Decimal('4.1')),
        '低密度脂蛋白胆固醇': ('ldl_cholesterol', Decimal('3.4'), Decimal('4.1')),
        '低密度脂蛋白胆固醇ldlc': ('ldl_cholesterol', Decimal('3.4'), Decimal('4.1')),
    }
    rule = aliases.get(name)
    number = _fixed_standard_number(fact.value)
    if (not _clinical_observation(fact) or rule is None or number is None
            or str(fact.unit or '').replace(' ', '').lower() != 'mmol/l'
            or not rule[1] <= number < rule[2]):
        return None
    return {'degree': 'mild', 'basis': 'concentration_reference_category', 'sourceStatus': status,
        'referenceCategory': '边缘升高', 'indicator': rule[0],
        'reference': '中国血脂管理指南（基层版2024年）表1',
        'url': 'https://www.chinagp.net/EN/10.12114/j.issn.1007-9572.2024.0005',
        'applicability': '仅该血脂浓度参考分层；不是个人ASCVD分层、疾病严重度或治疗目标'}


def _bounded_health_risk_view(value):
    value = deepcopy(value)
    for field in ('clinicalActions', 'lifestyleActions', 'scoringRationale'):
        value.pop(field, None)
    value['claims'] = [claim for claim in value.get('claims', []) if claim.get('role') != 'management']
    # Retrieval/retired management keys have no original in this numeric
    # packet and cannot masquerade as acknowledged reviewed evidence.
    value['evidenceKeys'] = sorted(_health_evidence_keys([value]))
    return value


def build_health_premise_catalog(item, facts, *, target_number=1):
    """Bind each selectable tuple before authors choose any numerical value."""
    value = deepcopy(item)
    value['risks'] = [_bounded_health_risk_view(risk) for risk in value['risks']]
    source_ids = []
    if value.get('kind') == 'dimension' and value.get('targetId') == 'autoimmune':
        source_ids = sorted(fid for fid, fact in facts.items() if fact.kind == 'function'
            and fid.startswith('function-fact-gut-immunity-') and fact.name == '肠道免疫力'
            and _fixed_standard_number(fact.value) is not None)
        present = {fact['factId'] for fact in value['facts']}
        value['facts'].extend(facts[fid].model_dump(mode='json', by_alias=True)
            for fid in source_ids if fid not in present)
    value['sourceFunctionalFactIds'] = source_ids
    catalog, seen = [], set()
    for risk in value['risks']:
        for claim in risk['claims']:
            for fid, quote in claim['factQuotes'].items():
                fact = facts.get(fid)
                if (fact is None or not isinstance(quote, str) or not quote.strip()
                        or quote not in fact.value):
                    continue
                identity = (risk['riskId'], claim['claimId'], fid, quote, 'support')
                if identity in seen:
                    continue
                seen.add(identity)
                role, eligible = _health_premise_role(risk, claim, fact, quote)
                catalog.append({'premiseId': f'T{target_number}P{len(catalog) + 1}',
                    'targetCode': value['code'], 'side': 'source_signal' if role == 'source_signal' else 'support',
                    'ownerRiskId': risk['riskId'], 'claimId': claim['claimId'],
                    'factId': fid, 'adoptedQuote': quote, 'premiseRole': role,
                    'eligibleFactors': eligible,
                    'reviewedEvidenceKeys': sorted({anchor['evidenceKey'] for anchor in claim.get('evidence', [])}),
                    'currentDegree': _documented_current_degree(fact) if role in
                        {'observed_clinical_state', 'observed_symptom'} else None,
                    # "NonSource" means more than a provider's unexamined
                    # source_fact signal, not a requirement for A/B data:
                    # an actual C finding plus an applicable approved medical
                    # relationship is personal evidence too.
                    'hasReviewedNonSourceRelation': claim.get('role') in
                        {'outcome', 'etiology', 'mechanism', 'diagnostic_principle'} and bool(claim.get('evidence'))})
        for fid in risk['opposingFactIds']:
            fact = facts.get(fid)
            if fact is None or not isinstance(fact.value, str) or not fact.value.strip():
                continue
            role, eligible = _health_premise_role(risk, {}, fact, fact.value, opposing=True)
            catalog.append({'premiseId': f'T{target_number}P{len(catalog) + 1}',
                'targetCode': value['code'], 'side': 'opposing', 'ownerRiskId': risk['riskId'],
                'claimId': None, 'factId': fid, 'adoptedQuote': fact.value,
                'premiseRole': role, 'eligibleFactors': eligible, 'reviewedEvidenceKeys': [],
                'currentDegree': None,
                'hasReviewedNonSourceRelation': _clinical_observation(fact)})
    for fid in source_ids:
        catalog.append({'premiseId': f'T{target_number}P{len(catalog) + 1}',
            'targetCode': value['code'], 'side': 'source_signal', 'ownerRiskId': None,
            'claimId': None, 'sourceFactId': fid, 'factId': fid, 'adoptedQuote': facts[fid].value,
            'premiseRole': 'source_function', 'eligibleFactors': ['personalRelevance'],
            'currentDegree': None, 'reviewedEvidenceKeys': [], 'hasReviewedNonSourceRelation': False,
            'scopeMeaning': '肠道免疫相关菌群功能的真实源指标；仅管理关注线索，不是人体免疫细胞、感染或系统免疫病的临床测量'})
    value['premiseCatalog'] = catalog
    value['allowedPremiseIdsByFactor'] = {name: [entry['premiseId'] for entry in catalog
        if name in entry['eligibleFactors']] for name in _HEALTH_WEIGHTS}
    value['premiseCatalogFingerprint'] = _digest(catalog)
    value['currentBurdenDegreeWitnesses'] = [{'premiseId': p['premiseId'], 'factId': p['factId'],
        'adoptedQuote': p['adoptedQuote'], **p['currentDegree']}
        for p in catalog if p.get('currentDegree') is not None]
    native = []
    for risk in value['risks']:
        own = [entry for entry in catalog if entry['ownerRiskId'] == risk['riskId']]
        if any(entry['premiseRole'] in {'observed_clinical_state', 'observed_symptom'}
                or entry['hasReviewedNonSourceRelation'] for entry in own):
            continue
        for fid in risk['sourceModelFactIds']:
            fact = facts.get(fid)
            if fact is not None and fact.status in {'低风险', '中风险', '高风险'}:
                native.append({'riskId': risk['riskId'], 'factId': fid, 'value': fact.value,
                    'originalLevel': fact.status,
                    'reason': '没有本病实际相关临床支持/反证；未确诊和未询问不是正常反证'})
    value['sourceOnlyNativeGradeReferences'] = native
    return value


def expand_health_premise_decision(row, item):
    """Expand only selected verified tuples; never repair an old decision."""
    if (not isinstance(row, dict) or set(row) != {'protocolRevision', 'code', 'factors'}
            or row.get('protocolRevision') != _HEALTH_AUTHOR_PROTOCOL or row.get('code') != item['code']):
        return None, ['health_author_protocol_mismatch']
    rows = row['factors']
    if (not isinstance(rows, list) or len(rows) != len(_HEALTH_WEIGHTS)
            or any(not isinstance(factor, dict) or not isinstance(factor.get('name'), str) for factor in rows)
            or {factor['name'] for factor in rows} != set(_HEALTH_WEIGHTS)):
        return None, ['health_factors_missing']
    catalog = {entry['premiseId']: entry for entry in item['premiseCatalog']}
    expanded, errors = [], []
    for factor in rows:
        name = factor['name']
        if set(factor) != {'name', 'score', 'applicability', 'premiseIds'}:
            errors.append('unexpected_health_premise_fields:' + name)
            continue
        ids = factor['premiseIds']
        if not isinstance(ids, list) or any(not isinstance(pid, str) for pid in ids):
            errors.append('invalid_health_premise_ids:' + name)
            continue
        ids = list(dict.fromkeys(ids))
        if factor['applicability'] in {'unknown', 'not_applicable'}:
            if factor['score'] is not None or ids:
                errors.append('unassessed_health_factor_has_score_or_basis:' + name)
            expanded.append({'name': name, 'score': None, 'applicability': factor['applicability'],
                'riskIds': [], 'factQuotes': {}, 'claimIds': []})
            continue
        allowed = set(item['allowedPremiseIdsByFactor'][name])
        if not ids or any(pid not in catalog or catalog[pid]['targetCode'] != item['code'] for pid in ids):
            errors.append('unknown_or_foreign_health_premise:' + name)
            continue
        if not set(ids) <= allowed:
            errors.append('health_premise_factor_not_eligible:' + name)
            continue
        selected = [catalog[pid] for pid in ids]
        quotes = {}
        for entry in selected:
            fid, quote = entry['factId'], entry['adoptedQuote']
            if fid in quotes and quotes[fid] != quote:
                errors.append('conflicting_health_premise_quotes:' + name)
            quotes[fid] = quote
        expanded.append({'name': name, 'score': factor['score'], 'applicability': factor['applicability'],
            'riskIds': list(dict.fromkeys(entry['ownerRiskId'] for entry in selected if entry['ownerRiskId'] is not None)),
            'factQuotes': quotes,
            'claimIds': list(dict.fromkeys(entry['claimId'] for entry in selected if entry['claimId'] is not None))})
        source_facts = list(dict.fromkeys(entry['sourceFactId'] for entry in selected if entry.get('sourceFactId')))
        if source_facts:
            expanded[-1]['sourceFactIds'] = source_facts
    return ({'code': item['code'], 'factors': expanded} if not errors else None), errors


def _source_only_native_grade_errors(decision, item):
    # This exception applies only when no applicable personal relation or
    # observed clinical state exists. It is a reference-band constraint, not
    # a conversion of native indices or a general aggregation by maximum.
    catalog = item.get('premiseCatalog', [])
    if any(p['premiseRole'] in {'observed_clinical_state', 'observed_symptom'}
            or p.get('hasReviewedNonSourceRelation') for p in catalog):
        return []
    order = {'低风险': 0, '中风险': 1, '高风险': 2}
    level = presentation_health_level(health_score_from_factors(decision['factors']))
    references = item.get('sourceOnlyNativeGradeReferences', [])
    if level not in order:
        return []
    personal = next(f for f in decision['factors'] if f['name'] == 'personalRelevance')
    if personal['score'] in {0, 100}:
        return ['source_only_signal_is_not_critical_or_confirmed_normal']
    if not references:
        return []
    original = {entry['originalLevel'] for entry in references}
    if item.get('kind') == 'risk' and len(original) != 1:
        return ['conflicting_source_only_native_reference_levels']
    reference = max(original, key=order.get)
    if level != reference:
        return ['source_only_reference_band_changed_without_related_evidence']
    return []


def _current_burden_degree_errors(decision, item):
    factor = next((f for f in decision['factors'] if f['name'] == 'currentBurden'), None)
    if factor is None or factor['applicability'] != 'assessed':
        return []
    selected = [p for p in item.get('premiseCatalog', [])
        if p['claimId'] in factor['claimIds'] and p['ownerRiskId'] in factor['riskIds']
        and factor['factQuotes'].get(p['factId']) == p['adoptedQuote'] and p.get('currentDegree')]
    needed = {50: {'moderate', 'severe', 'critical'}, 25: {'severe', 'critical'}, 0: {'critical'}}
    if factor['score'] <= 50 and not any(
            p['currentDegree']['degree'] in needed.get(factor['score'], set()) for p in selected):
        return ['current_burden_degree_not_documented']
    clinical_anchors = {'moderate': 50, 'severe': 25, 'critical': 0}
    if any(p['currentDegree'].get('basis') == 'explicit_source_status'
            and p['currentDegree']['degree'] in clinical_anchors
            and factor['score'] > clinical_anchors[p['currentDegree']['degree']] for p in selected):
        return ['current_burden_understates_documented_degree']
    return []


def _provided_original_evidence_keys(evidence):
    return {row['evidenceKey'] for row in evidence if isinstance(row, dict)
        and isinstance(row.get('evidenceKey'), str) and not row.get('opposingOriginalUnavailable')
        and (isinstance(row.get('sourceText'), str) and row['sourceText'].strip()
            or any(isinstance(anchor.get('quote'), str) and anchor['quote'].strip()
                for anchor in row.get('reviewedAnchors', []) if isinstance(anchor, dict)))}


def _health_risk_view(risk):
    """Score approved present-state relations, not prescriptions or drafts."""
    return _bounded_health_risk_view(risk.model_dump(mode='json', by_alias=True))


def _health_evidence_keys(risks):
    return {key for risk in risks for key in [*risk.get('opposingEvidenceKeys', []),
        *(anchor.get('evidenceKey') for claim in risk.get('claims', [])
            for anchor in claim.get('evidence', []))] if isinstance(key, str) and key}


def _health_evidence_context(risks, summaries):
    """Only used reviewed quotations and actual unquoted opposition travel.

    The health scorer cannot author new medical relations. Approved anchors
    already live inside each risk claim; unrelated papers and their repeated
    abstracts/full-text blocks cannot improve this numerical review.
    """
    available = {row.get('evidenceKey'): row for row in summaries or [] if isinstance(row, dict)}
    anchors, opposing = {}, set()
    for risk in risks:
        opposing.update(risk.get('opposingEvidenceKeys', []))
        for claim in risk.get('claims', []):
            for anchor in claim.get('evidence', []):
                key = anchor.get('evidenceKey')
                if not isinstance(key, str) or not key:
                    continue
                entry = {'riskId': risk['riskId'], 'claimId': claim['claimId'],
                    'quote': anchor.get('quote'), 'direction': anchor.get('direction')}
                if entry not in anchors.setdefault(key, []):
                    anchors[key].append(entry)
    result = []
    for key in sorted(_health_evidence_keys(risks)):
        source = available.get(key, {})
        entry = {'evidenceKey': key, **{field: source.get(field)
            for field in ('title', 'year', 'doi', 'url')}, 'reviewedAnchors': anchors.get(key, [])}
        # A reviewer must still see real scientific opposition that has no
        # selected opposing quotation; a title alone cannot substitute for it.
        if key in opposing and not any(anchor.get('direction') in {'oppose', 'mixed'}
                and isinstance(anchor.get('quote'), str) and anchor['quote'].strip()
                for anchor in entry['reviewedAnchors']):
            text = source.get('sourceText')
            entry['sourceText'] = text if isinstance(text, str) and text.strip() else None
            entry['opposingOriginalUnavailable'] = entry['sourceText'] is None
        result.append(entry)
    return result


def _health_decision_errors(row, item, facts):
    if not isinstance(row, dict) or set(row) != {'code', 'factors'} or row['code'] != item['code']:
        return ['unexpected_health_score_fields']
    factors = row['factors']
    if (not isinstance(factors, list) or len(factors) != len(_HEALTH_WEIGHTS)
            or any(not isinstance(factor, dict) or not isinstance(factor.get('name'), str) for factor in factors)
            or {factor.get('name') for factor in factors} != _HEALTH_WEIGHTS.keys()):
        return ['health_factors_missing']
    risks = {risk['riskId']: risk for risk in item['risks']}
    mapping = {entry['claimId']: entry for entry in item['claimIdMap']}
    for factor in factors:
        fields = {'name', 'score', 'applicability', 'riskIds', 'factQuotes', 'claimIds'}
        if set(factor) not in (fields, fields | {'sourceFactIds'}):
            return ['unexpected_health_factor_fields']
        for key in ('riskIds', 'claimIds'):
            if (not isinstance(factor[key], list) or any(not isinstance(value, str) for value in factor[key])
                    or len(factor[key]) != len(set(factor[key]))):
                return ['invalid_health_factor_ids']
        quotes = factor['factQuotes']
        if not isinstance(quotes, dict):
            return ['invalid_health_factor_quotes']
        source_ids = factor.get('sourceFactIds', [])
        if (not isinstance(source_ids, list) or any(not isinstance(fid, str) for fid in source_ids)
                or len(source_ids) != len(set(source_ids))):
            return ['invalid_health_source_fact_ids']
        if source_ids and (factor['name'] != 'personalRelevance' or item.get('kind') != 'dimension'
                or item.get('targetId') != 'autoimmune' or not set(source_ids) <= set(item.get('sourceFunctionalFactIds', []))
                or not set(source_ids) <= set(quotes)
                or any(fid not in facts or facts[fid].kind != 'function'
                    or not fid.startswith('function-fact-gut-immunity-') or facts[fid].name != '肠道免疫力'
                    or quotes[fid] != facts[fid].value for fid in source_ids)):
            return ['unbound_or_ineligible_health_source_function']
        if not isinstance(factor['applicability'], str):
            return ['invalid_health_factor_applicability']
        if factor['applicability'] in {'unknown', 'not_applicable'}:
            if factor['score'] is not None or factor['riskIds'] or quotes or factor['claimIds'] or source_ids:
                return ['unassessed_health_factor_has_score_or_basis']
            continue
        if (factor['applicability'] != 'assessed' or type(factor['score']) is not int
                or factor['score'] not in _HEALTH_ANCHORS or not (factor['riskIds'] or source_ids) or not quotes
                ):
            return ['health_factor_without_assessed_basis']
        if not set(factor['riskIds']) <= risks.keys():
            return ['foreign_health_factor_risk']
        own = [risks[rid] for rid in factor['riskIds']]
        allowed = {fid for risk in own for field in ('supportFactIds', 'opposingFactIds',
            'backgroundFactIds', 'sourceModelFactIds') for fid in risk[field]}
        allowed.update(source_ids)
        if (not set(quotes) <= allowed & facts.keys()
                or any(not isinstance(quote, str) or not quote.strip() or quote not in facts[fid].value
                    for fid, quote in quotes.items())):
            return ['unbound_health_factor_quotes']
        if any(cid not in mapping or mapping[cid]['riskId'] not in factor['riskIds']
                for cid in factor['claimIds']):
            return ['unbound_health_factor_claim']
        adopted = {fid for cid in factor['claimIds'] for fid in mapping[cid]['factIds']}
        opposing = {fid for risk in own for fid in risk['opposingFactIds']}
        if not factor['claimIds'] and not set(quotes) <= opposing | set(source_ids):
            return ['health_factor_without_reviewed_claim_or_opposition']
        if not set(quotes) <= adopted | opposing | set(source_ids):
            return ['unreviewed_health_factor_premise']
        selected_claims = [claim for risk in own for claim in risk['claims']
            if claim['claimId'] in factor['claimIds']]
        if any(fid not in opposing and fid not in source_ids and not any(fid in claim['factQuotes']
                and quote in claim['factQuotes'][fid] for claim in selected_claims)
                for fid, quote in quotes.items()):
            return ['unreviewed_health_factor_quote']
        # A source-only association is not an observation of current burden,
        # daily function or persistence, including when its original grade is low.
        if factor['name'] != 'personalRelevance' and not any(
                facts[fid].kind == 'self_reported' or facts[fid].kind == 'clinical'
                and not facts[fid].fact_id.startswith('auxiliary-fact-')
                and not any(token in str(facts[fid].source or '').lower()
                    for token in ('microbiome', '菌群', '宏基因')) for fid in quotes):
            return ['source_signal_is_not_observed_health_burden']
    return []


def _health_factor_feedback(row, item, facts):
    """Explain all rejected factors without repairing their IDs or scores."""
    if not isinstance(row, dict) or not isinstance(row.get('factors'), list):
        return []
    mapping = {entry['claimId']: entry for entry in item['claimIdMap']}
    risks = {risk['riskId']: risk for risk in item['risks']}
    claims = {claim['claimId']: claim for risk in item['risks'] for claim in risk['claims']}
    result = []
    for factor in row['factors']:
        if not isinstance(factor, dict):
            result.append({'factor': None, 'errors': ['health_factor_object_required']})
            continue
        name = factor.get('name')
        if not isinstance(name, str) or name not in _HEALTH_WEIGHTS:
            result.append({'factor': name, 'errors': ['unknown_health_factor']})
            continue
        isolated = [{'name': code, 'score': None, 'applicability': 'unknown',
            'riskIds': [], 'factQuotes': {}, 'claimIds': []} for code in _HEALTH_WEIGHTS]
        isolated = [deepcopy(factor) if value['name'] == name else value for value in isolated]
        errors = _health_decision_errors({'code': item['code'], 'factors': isolated}, item, facts)
        ids = factor.get('riskIds') if isinstance(factor.get('riskIds'), list) else []
        ids = [rid for rid in ids if isinstance(rid, str)]
        selected = factor.get('claimIds') if isinstance(factor.get('claimIds'), list) else []
        selected = [cid for cid in selected if isinstance(cid, str)]
        quotes = factor.get('factQuotes') if isinstance(factor.get('factQuotes'), dict) else {}
        unknown = [cid for cid in selected if cid not in mapping]
        wrong_owners = [{'claimId': cid, 'actualOwnerRiskId': mapping[cid]['riskId'],
            'declaredRiskIds': ids} for cid in selected if cid in mapping and mapping[cid]['riskId'] not in ids]
        adopted = {fid for cid in selected if cid in mapping for fid in mapping[cid]['factIds']}
        opposing = {fid for rid in ids if rid in risks for fid in risks[rid]['opposingFactIds']}
        unadopted = sorted(set(quotes) - adopted - opposing - set(factor.get('sourceFactIds', [])))
        bad_quotes = [fid for fid, quote in quotes.items() if fid not in facts
            or not isinstance(quote, str) or not quote.strip() or quote not in facts[fid].value]
        if errors or unknown or wrong_owners or unadopted or bad_quotes:
            owners = set(ids) | {mapping[cid]['riskId'] for cid in selected if cid in mapping}
            result.append({'factor': name, 'errors': errors, 'unknownClaimIds': unknown,
                'wrongOwnerClaims': wrong_owners, 'unadoptedQuotedFactIds': unadopted,
                'literalQuoteMismatchFactIds': bad_quotes,
                'allowedClaimBindings': [{'claimId': cid, 'ownerRiskId': entry['riskId'],
                    'factQuotes': deepcopy(claims[cid]['factQuotes'])} for cid, entry in mapping.items()
                    if entry['riskId'] in owners and cid in claims],
                'permission': '仅定位错误；不得替换ID或删除坏引文后沿用旧分，必须重新判断因素并完整独审。'})
    return result


_HEALTH_REJECTION_KINDS = {'factor_anchor', 'factor_applicability', 'premise_binding',
    'omitted_problem', 'duplicate_burden', 'formula', 'rubric_mismatch'}
_HEALTH_REJECTION_RULES = {'anchors', 'factor-applicability', 'scope-ownership',
    'adopted-premises-only', 'observed-burden-only', 'same-disease-counterevidence',
    'unknown-is-not-healthy', 'no-duplicate-burden', 'weighted-mean', 'health-bands'}


def _health_rejection_schema(item):
    return {'code': item['code'], 'reasons': [{'kind': '|'.join(sorted(_HEALTH_REJECTION_KINDS)),
        'check': '具体失败的factorChecks.<因素>[.<flag>]或globalChecks.<项>或覆盖/重算字段，不写泛化拒绝',
        'factor': 'currentBurden|functionalImpact|persistence|personalRelevance|null',
        'riskIds': ['真实输入riskId'], 'factQuotes': {'真实factId': '逐字事实片段'},
        'claimIds': ['本包真实claimId'], 'evidenceQuotes': {'本项已审evidenceKey': '已审原文逐字片段'},
        'rubricRule': '|'.join(sorted(_HEALTH_REJECTION_RULES)),
        'detail': '仅内部：明确为什么该因素/总体判定不成立；通过则reasons=[]，不能只说不合理'}]}


def _health_rejection_errors(entry, item, facts, evidence_context=(), *, review=None):
    if (not isinstance(entry, dict) or set(entry) != {'code', 'reasons'}
            or entry['code'] != item['code'] or not isinstance(entry['reasons'], list) or not entry['reasons']):
        return ['health_rejection_reason_missing']
    risks = {risk['riskId']: risk for risk in item['risks']}
    mapping = {value['claimId']: value for value in item['claimIdMap']}
    allowed_facts = {fact['factId'] for fact in item['facts']}
    anchors = {}
    for risk in item['risks']:
        for claim in risk['claims']:
            for anchor in claim.get('evidence', []):
                anchors.setdefault(anchor['evidenceKey'], []).append(anchor['quote'])
    own_evidence = _health_evidence_keys(item['risks'])
    for source in evidence_context:
        text = source.get('sourceText')
        if source.get('evidenceKey') in own_evidence and isinstance(text, str) and text.strip():
            anchors.setdefault(source['evidenceKey'], []).append(text)
    for reason in entry['reasons']:
        if (not isinstance(reason, dict) or set(reason) != {'kind', 'check', 'factor', 'riskIds',
                'factQuotes', 'claimIds', 'evidenceQuotes', 'rubricRule', 'detail'}
                or not isinstance(reason['kind'], str) or reason['kind'] not in _HEALTH_REJECTION_KINDS
                or reason['factor'] is not None and (not isinstance(reason['factor'], str)
                    or reason['factor'] not in _HEALTH_WEIGHTS)
                or not isinstance(reason['check'], str) or reason['check'] not in _health_reason_check_names()
                or not isinstance(reason['rubricRule'], str) or reason['rubricRule'] not in _HEALTH_REJECTION_RULES
                or not isinstance(reason['detail'], str) or not 1 <= len(reason['detail'].strip()) <= 600):
            return ['invalid_health_rejection_reason']
        for key, allowed in (('riskIds', risks.keys()), ('claimIds', mapping.keys())):
            values = reason[key]
            if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                    or not set(values) <= allowed):
                return ['unbound_health_rejection_reason']
        if any(mapping[cid]['riskId'] not in reason['riskIds'] for cid in reason['claimIds']):
            return ['unbound_health_rejection_reason']
        quotes, evidence = reason['factQuotes'], reason['evidenceQuotes']
        if (not isinstance(quotes, dict) or not set(quotes) <= allowed_facts
                or any(fid not in facts or not isinstance(quote, str) or not quote.strip()
                    or quote not in facts[fid].value for fid, quote in quotes.items())
                or not isinstance(evidence, dict) or any(key not in anchors or not isinstance(quote, str)
                    or not quote.strip() or not any(quote in original for original in anchors[key])
                    for key, quote in evidence.items())):
            return ['unbound_health_rejection_quote']
        if reason['kind'] not in {'formula', 'rubric_mismatch'} and not (quotes or evidence or reason['claimIds']):
            return ['health_rejection_reason_without_basis']
    if review is not None:
        failed = _health_failed_review_checks(review, item['decision'], item)
        explained = {reason['check'] for reason in entry['reasons']}
        if any(not any(check == reason or check.startswith(reason + '.') for reason in explained) for check in failed):
            return ['health_rejection_reason_missing_check']
        if any(not any(check == reason or check.startswith(reason + '.') for check in failed) for reason in explained):
            return ['health_rejection_reason_targets_passing_check']
    return []


_HEALTH_REVIEW_PROTOCOL = _HEALTH_RUBRIC['reviewProtocolRevision']
_HEALTH_GLOBAL_TRUE = ('allRelevantPatientStatesChecked', 'sameDiseaseApplicabilityChecked',
    'wholeTargetBurdenRepresented', 'missingNotUsedAsHealthy', 'sourceSignalNotPromotedToDiagnosis',
    'sourceValuesAndGradesPreserved', 'factorsMutuallyConsistent',
    'behaviorNotUsedAsObservedBurden', 'sourceOnlyNativeGradesNotLoweredByMissingData',
    'sourceOnlyReferenceBandNotRaisedWithoutRelatedEvidence',
    'currentBurdenDegreeSupportedByPatientSource', 'sourceFunctionNotPromotedToSystemicDisease')
_HEALTH_GLOBAL_FALSE = ('severeProblemsOmitted', 'duplicateBurdenCounted')


def _health_reason_check_names():
    return {'protocolRevision', 'recomputedScore', 'recomputedLevel', 'formula',
        'consideredRiskIds', 'consideredFactIds', 'consideredOpposingFactIds',
        'consideredEvidenceKeys', 'consideredOpposingEvidenceKeys',
        *(f'globalChecks.{key}' for key in (*_HEALTH_GLOBAL_TRUE, *_HEALTH_GLOBAL_FALSE, 'unsupportedFactorNames')),
        *(f'factorChecks.{name}' for name in _HEALTH_WEIGHTS),
        *(f'factorChecks.{name}.{flag}' for name in _HEALTH_WEIGHTS
            for flag in ('valid', 'anchorJustified', 'applicabilityJustified'))}


def _health_failed_review_checks(review, decision, item):
    result = set()
    for error in _health_review_errors(review, decision, item):
        if error.startswith('health_factor_check_failed:'):
            _, name, flag = error.split(':')
            result.add('factorChecks.' + name + '.' + flag)
        elif error.startswith('health_factor_review_binding_failed:'):
            result.add('factorChecks.' + error.split(':', 1)[1])
        elif error.startswith('health_global_check_failed:'):
            result.add('globalChecks.' + error.split(':', 1)[1])
        elif error.startswith('health_review_coverage_missing:'):
            result.add(error.split(':', 1)[1])
        elif error == 'health_review_recomputed_result_mismatch':
            if review.get('recomputedScore') != decision['index']:
                result.add('recomputedScore')
            if review.get('recomputedLevel') != decision['level']:
                result.add('recomputedLevel')
        elif error == 'health_score_formula_mismatch':
            result.add('formula')
        elif error == 'health_review_protocol_mismatch':
            result.add('protocolRevision')
        elif error == 'health_global_checks_missing':
            result.update('globalChecks.' + key for key in (*_HEALTH_GLOBAL_TRUE, *_HEALTH_GLOBAL_FALSE, 'unsupportedFactorNames'))
        elif error == 'health_factor_review_missing':
            result.update('factorChecks.' + name for name in _HEALTH_WEIGHTS)
    return result


def _health_review_schema(item, decision):
    return {'protocolRevision': _HEALTH_REVIEW_PROTOCOL, 'code': item['code'],
        'recomputedScore': '按实际可评因素权重重算，须与decision.index一致',
        'recomputedLevel': '按80/60健康分界重判，须与decision.level一致',
        'consideredRiskIds': ['全部输入riskId'], 'consideredFactIds': ['全部输入factId'],
        'consideredOpposingFactIds': ['全部输入opposingFactIds'],
        'consideredEvidenceKeys': ['必须包含本item全部已审anchor与反证evidenceKey；可额外确认本包providedOriginalEvidenceKeys中已给真实原文，额外确认不赋予新评分依据'],
        'consideredOpposingEvidenceKeys': ['本item全部opposingEvidenceKeys'],
        'factorChecks': [{**factor, 'valid': True, 'anchorJustified': True,
            'applicabilityJustified': True} for factor in decision['factors']],
        'globalChecks': {**{name: True for name in _HEALTH_GLOBAL_TRUE},
            **{name: False for name in _HEALTH_GLOBAL_FALSE}, 'unsupportedFactorNames': []}}


def _health_review_errors(review, decision, item):
    """Fresh atomic/global verdicts have one derived authorization result.

    Legacy aggregate valid/scoreJustified responses are deliberately rejected;
    their false verdicts can never be reinterpreted as current approvals.
    """
    expected = {'protocolRevision', 'code', 'recomputedScore', 'recomputedLevel',
        'consideredRiskIds', 'consideredFactIds', 'consideredOpposingFactIds',
        'consideredEvidenceKeys', 'consideredOpposingEvidenceKeys', 'factorChecks', 'globalChecks'}
    if (not isinstance(review, dict) or set(review) != expected
            or review.get('protocolRevision') != _HEALTH_REVIEW_PROTOCOL):
        return ['health_review_protocol_mismatch']
    errors = []
    if (review['code'] != decision['code'] or type(review['recomputedScore']) not in {int, float}
            or review['recomputedScore'] != decision['index'] or review['recomputedLevel'] != decision['level']):
        errors.append('health_review_recomputed_result_mismatch')
    risks = item['risks']
    for key, expected_ids in (('consideredRiskIds', {risk['riskId'] for risk in risks}),
            ('consideredFactIds', {fact['factId'] for fact in item['facts']}),
            ('consideredOpposingFactIds', {fid for risk in risks for fid in risk['opposingFactIds']}),
            ('consideredEvidenceKeys', _health_evidence_keys(risks)),
            ('consideredOpposingEvidenceKeys', {key for risk in risks for key in risk.get('opposingEvidenceKeys', [])})):
        value = review[key]
        valid = isinstance(value, list) and all(isinstance(identifier, str) for identifier in value)
        if valid and key == 'consideredEvidenceKeys':
            actual = set(value)
            provided = set(item.get('providedOriginalEvidenceKeys', expected_ids))
            # Additional acknowledged reading is metadata only. The factor
            # premises remain restricted to this target's approved claims.
            used = {anchor['evidenceKey'] for risk in risks for claim in risk['claims']
                if any(claim['claimId'] in factor['claimIds'] for factor in decision['factors'])
                for anchor in claim.get('evidence', [])}
            valid = expected_ids <= actual <= provided and not (actual - expected_ids) & used
        elif valid:
            valid = set(value) == expected_ids
        if not valid:
            errors.append('health_review_coverage_missing:' + key)
    global_checks = review['globalChecks']
    if (not isinstance(global_checks, dict) or set(global_checks) != {
            *_HEALTH_GLOBAL_TRUE, *_HEALTH_GLOBAL_FALSE, 'unsupportedFactorNames'}):
        errors.append('health_global_checks_missing')
    else:
        errors.extend('health_global_check_failed:' + name for name in _HEALTH_GLOBAL_TRUE
            if global_checks[name] is not True)
        errors.extend('health_global_check_failed:' + name for name in _HEALTH_GLOBAL_FALSE
            if global_checks[name] is not False)
        if global_checks['unsupportedFactorNames'] != []:
            errors.append('health_global_check_failed:unsupportedFactorNames')
    checks = review['factorChecks']
    if not isinstance(checks, list) or len(checks) != len(_HEALTH_WEIGHTS):
        errors.append('health_factor_review_missing')
    else:
        for factor in decision['factors']:
            matches = [check for check in checks if isinstance(check, dict) and check.get('name') == factor['name']]
            if (len(matches) != 1 or set(matches[0]) != {*factor, 'valid', 'anchorJustified', 'applicabilityJustified'}
                    or any(matches[0].get(key) != value for key, value in factor.items())):
                errors.append('health_factor_review_binding_failed:' + factor['name'])
                continue
            errors.extend('health_factor_check_failed:' + factor['name'] + ':' + field
                for field in ('valid', 'anchorJustified', 'applicabilityJustified') if matches[0][field] is not True)
    if health_score_from_factors(decision['factors']) != decision['index']:
        errors.append('health_score_formula_mismatch')
    return errors


async def assess_dimension_scores(client, document, evidence_summaries=None, *, subject=None):
    """One fresh author/reviewer batch covers six domains and their diseases."""
    object.__setattr__(document, '_dimension_score_receipt', None)
    risks, facts, scope = _snapshot(document)
    if not risks:
        return {}
    groups = [(f'dimension:{code}', 'dimension', code, name, [risk for risk in risks if risk.category == code])
        for code, name in RISK_CATEGORY_DEFINITIONS]
    disease_targets = _disease_health_targets(document, risks)
    groups.extend((f'risk:{risk.risk_id}', 'risk', risk.risk_id, risk.name, [risk]) for risk in disease_targets)
    views = {risk.risk_id: _health_risk_view(risk) for risk in [*risks, *disease_targets]}
    evidence = _health_evidence_context(list(views.values()), evidence_summaries)
    unavailable_opposition = {row['evidenceKey'] for row in evidence if row.get('opposingOriginalUnavailable')}
    items = []
    for code, kind, target, name, own in groups:
        if not own:
            continue
        ids = {fid for risk in own for field in ('support_fact_ids', 'opposing_fact_ids',
            'background_fact_ids', 'source_model_fact_ids') for fid in getattr(risk, field)}
        item = _encode_dimension_item({'code': code, 'kind': kind, 'targetId': target, 'name': name,
            'risks': [views[risk.risk_id] for risk in own],
            'facts': [facts[fid].model_dump(mode='json', by_alias=True) for fid in sorted(ids) if fid in facts]})
        if kind == 'dimension':
            identities = {}
            for risk in own:
                identities.setdefault(risk.disease_id, []).append(risk)
            item['burdenGroups'] = [{'diseaseId': disease_id,
                'riskIds': [risk.risk_id for risk in members], 'countAsOneBurden': True}
                for disease_id, members in identities.items() if len(members) > 1]
        items.append(build_health_premise_catalog(item, facts, target_number=len(items) + 1))
    blocked = [item for item in items if any(set(risk.get('opposingEvidenceKeys', [])) & unavailable_opposition
        for risk in item['risks'])]
    items = [item for item in items if item not in blocked]
    for item in blocked:
        marker = 'HEALTH_SCORE_OPPOSING_ORIGINAL_UNAVAILABLE:' + item['code']
        if marker not in document.incomplete:
            document.incomplete.append(marker)
    if not items:
        return {}
    provided_originals = sorted(_provided_original_evidence_keys(evidence))
    for item in items:
        item['providedOriginalEvidenceKeys'] = provided_originals
    codec = FactIdCodec.from_items(items)
    base = {'subject': subject or {}, 'rubric': _HEALTH_RUBRIC,
        'evidenceSummaries': evidence, 'rules': [*_HEALTH_RUBRIC['rules'],
            '旧risk.index/level只保留原独审临床关注记录，不能转成100-index或冒充新健康分。',
            'claimId已按每个包整包唯一编码，风险ID不变；typed factId/factIds及factQuotes键使用本包F短码，返回也只用这些短码，原始数值和引文绝不能改写。',
            '作者只返回health-premise-author-v1的四因素score/applicability/premiseIds，不自行填写riskIds/claimIds/factQuotes；premiseCatalog已由服务端逐字绑定真实owner、claim、fact及采用片段，选中后确定展开，不能重写引文或跨target借P。',
            '每因素仅选择allowedPremiseIdsByFactor中的P；没有可用P则unknown/null/[]。行为暴露（含单纯睡眠时长）、家族/诊断史和纯来源菌群信号只支持personalRelevance；未确诊不是无症状，不能给临床负担90/100。',
            'currentBurden须真实临床状态或实际症状，functionalImpact须实际功能影响，persistence须实际状态持续，不能拿饮食运动睡眠习惯冒充当前疾病负担或病程。',
            'currentBurdenDegreeWitnesses是本人来源可核的程度依据；50/25/0分别需要已记录中度/重度/危急证据。多个轻项、仅出现症状或持续三个月不证明当前中度；不替作者改分，无依据须重评或unknown，不当正常。',
            'sourceFunctionalFactIds仅收本维规范化原功能指标：例如肠道免疫力0/很弱是真实源功能管理线索，只可personalRelevance；不能当人体免疫细胞测量、感染或系统免疫病。展开的sourceFactIds由服务端绑定，不由作者填写。',
            'sourceOnlyNativeGradeReferences保留健康管理关注的原级参考，不是个人患病概率。完全没有实际相关个人关系/临床状态时，不得无依据升级或降级；只有来源时也不能称危急0或健康100。只有纯源信号的整维不能被其他低级或未知稀释其明确中/高参考档；这是窄参考档约束，不设具体数。存在真实相关个人资料时，仍综合判定，不取最高单病或均值，不乘100、不100减。',
            'burdenGroups中同一疾病identity的症状/指标/疾病scope是同一负担的不同信息；取真实事实共同判断，不能重复加分或把症状升级为疾病。',
            '文献仅携实际已审逐字anchor及元数据；没有反向anchor的实际反证保留原文。只核现有关系的因素适用，不新增关系或按标题推断医学意义。',
            '只输出四因素锚点和精确归因；代码算总分，不创作新的患者状态、病因或正文。']}
    decisions, pending, feedback, clarification_used = {}, items, {}, False
    for attempt in range(2):
        payload = {**base, 'task': 'assess_integrated_health_scores', 'items': pending,
            'outputSchema': {'decisions': [_health_author_schema(item) for item in pending]}}
        if feedback:
            payload['previousRejectedDecisions'] = feedback
            payload['rules'] = [*base['rules'], '这是唯一一次修正；仅按真实输入修复原因素和独审错误，不沿用被拒评分。']
        try:
            rows, wire_errors = await author_health_wave(client, payload, codec)
        except Exception:
            break
        authored, feedback = {}, {}
        for item in pending:
            matches = [row for row in rows if isinstance(row, dict) and row.get('code') == item['code']]
            selected = matches[0] if len(matches) == 1 else None
            row, premise_errors = expand_health_premise_decision(selected, item)
            errors = wire_errors.get(item['code'], []) + premise_errors
            if row is not None:
                errors += _health_decision_errors(row, item, facts)
                if not errors:
                    errors += _current_burden_degree_errors(row, item)
                    errors += _source_only_native_grade_errors(row, item)
            score = health_score_from_factors(row['factors']) if not errors else None
            if not errors and score is None:
                errors = ['health_score_has_no_assessed_factors']
            if errors:
                feedback[item['code']] = {'errors': errors,
                    'factorErrors': _health_factor_feedback(row, item, facts),
                    'allowedPremiseIdsByFactor': item['allowedPremiseIdsByFactor'],
                    'currentBurdenDegreeWitnesses': item['currentBurdenDegreeWitnesses'],
                    'sourceOnlyNativeGradeReferences': item['sourceOnlyNativeGradeReferences'],
                    'rejectedPremiseSelection': selected}
            else:
                item['selectedPremiseIdsByFactor'] = {factor['name']: list(dict.fromkeys(factor['premiseIds']))
                    for factor in selected['factors']}
                authored[item['code']] = {**row, 'index': score, 'level': presentation_health_level(score)}
        if authored:
            review_items = [{**item, 'decision': authored[item['code']]} for item in pending if item['code'] in authored]
            try:
                review_payload = {**base, 'task': 'review_integrated_health_scores',
                    'items': review_items, 'rules': [*base['rules'],
                        '独立逐因素核锚点的临床含义、实际适用性和引文；不能只复制作者factor就同意。',
                        '这是新的health-premise-atomic-review-v5协议；作者的premiseIds已由服务端确定展开成decision，各factor精确绑定真实采用的owner/claim/quote或明确sourceFactIds。独审须看完整展开及全部相反资料，不返回总体valid/scoreJustified，旧协议不得混用。',
                        '遍历本项全部风险、本人事实、相反事实及相反文献。任何重要遗漏、同病适用不成立、虚构阴性、源信号升级诊断、因素矛盾或重复负担，都必须在相应globalChecks显式否定。',
                        '按实际可评因素权重独立重算recomputedScore/Level；不更改作者值，算式或分界不一致就指出对应字段，不能凭偏好另一个数否定正确公式。',
                        '任何factor/global失败check必须在rejectionReasons指向具体check并给实际事实/已审claim或量表规则；无失败check时reasons=[]，不要写肯定解释冒充拒绝原因。',
                        'consideredEvidenceKeys须包含本item全部已审anchor/反证key；可以确认阅读本包providedOriginalEvidenceKeys中其他真实已给原文，但额外确认不是本factor新增依据。漏required或包外key仍拒绝。',
                        '50锚要求已有中等程度负担或实际中等持续/功能影响；多个轻微偏离不能按数量算中等。不知道某因素时核其实为unknown，不当健康值。'],
                    'rejectionReasonFormat': _health_rejection_schema(review_items[0])['reasons'][0],
                    'outputSchema': {'reviews': [_health_review_schema(item, authored[item['code']]) for item in review_items],
                        'rejectionReasons': [{'code': item['code'], 'reasons': []} for item in review_items]}}
                reviews, wire_review_errors, rejection_reasons = await review_health_wave(
                    client, review_payload, codec, include_reasons=True)
            except Exception:
                reviews, wire_review_errors, rejection_reasons = [], {}, {}
            unclear_rejections = []
            for item in review_items:
                row = authored[item['code']]
                matches = [review for review in reviews if isinstance(review, dict) and review.get('code') == item['code']]
                review = _normalise_factor_ids(matches[0], 'factorChecks') if len(matches) == 1 else None
                errors = (wire_review_errors.get(item['code'], []) + (_health_review_errors(review, row, item)
                    if review is not None else ['health_score_review_missing']))
                sidecar = rejection_reasons.get(item['code'])
                if not errors and isinstance(sidecar, dict) and sidecar.get('reasons'):
                    errors = ['health_review_positive_with_rejection_reason']
                if errors:
                    feedback[item['code']] = {'decision': row, 'review': matches[:1], 'errors': errors}
                    reason_errors = _health_rejection_errors(sidecar, item, facts, evidence, review=review)
                    if not reason_errors:
                        feedback[item['code']]['rejectionReasons'] = sidecar['reasons']
                    else:
                        feedback[item['code']]['rejectionReasonErrors'] = reason_errors
                        if (review is not None and review.get('protocolRevision') == _HEALTH_REVIEW_PROTOCOL
                                and _health_failed_review_checks(review, row, item)):
                            unclear_rejections.append({**item, 'rejectedReview': deepcopy(review)})
                else:
                    value = _receipt_decision(row, item)
                    value['reviewProtocolRevision'] = _HEALTH_REVIEW_PROTOCOL
                    value['authorProtocolRevision'] = _HEALTH_AUTHOR_PROTOCOL
                    value['premiseCatalogFingerprint'] = item['premiseCatalogFingerprint']
                    value['selectedPremiseIdsByFactor'] = deepcopy(item['selectedPremiseIdsByFactor'])
                    value['evidencePackageFingerprint'] = _digest(evidence)
                    value['acknowledgedAdditionalEvidenceKeys'] = sorted(set(review['consideredEvidenceKeys'])
                        - _health_evidence_keys(item['risks']))
                    value['availability'] = ('available' if all(factor['applicability'] == 'assessed'
                        for factor in value['factors']) else 'partial')
                    value['assessedWeight'] = sum(_HEALTH_WEIGHTS[factor['name']]
                        for factor in value['factors'] if factor['applicability'] == 'assessed')
                    decisions[item['code']] = value
            if unclear_rejections and not clarification_used:
                clarification_used = True
                clarification_payload = {**base, 'task': 'clarify_health_score_rejections',
                    'items': unclear_rejections, 'rules': [*base['rules'],
                        '仅补充刚才失败check的明确理由，不修改recomputedScore/Level、任何factor/global否定或因素分数，不返回新的reviews或临床结论。',
                        '逐code解释哪个因素、事实归因或量表规则导致拒绝；必须引用实际输入。无法给依据时保留拒绝，不改为通过。'],
                    'outputSchema': {'rejectionReasons': [_health_rejection_schema(item) for item in unclear_rejections]}}
                try:
                    clarified, clarification_errors = await clarify_health_rejections(
                        client, clarification_payload, codec)
                except Exception:
                    clarified, clarification_errors = {}, {}
                for item in unclear_rejections:
                    entry = clarified.get(item['code'])
                    errors = clarification_errors.get(item['code'], []) + _health_rejection_errors(
                        entry, item, facts, evidence, review=item['rejectedReview'])
                    if not errors:
                        feedback[item['code']]['rejectionReasons'] = entry['reasons']
                        feedback[item['code']]['reasonClarification'] = 'rejection_only_original_verdict_unchanged'
                        feedback[item['code']].pop('rejectionReasonErrors', None)
                    else:
                        feedback[item['code']]['rejectionReasonErrors'] = errors
        pending = [item for item in pending if item['code'] not in decisions]
        if not pending:
            break
    if _snapshot(document)[2] != scope:
        return {}
    dimensions = {item['targetId']: decisions[item['code']] for item in items
        if item['kind'] == 'dimension' and item['code'] in decisions}
    risk_scores = {item['targetId']: decisions[item['code']] for item in items
        if item['kind'] == 'risk' and item['code'] in decisions}
    if decisions:
        dimensions, risk_scores = deepcopy(dimensions), deepcopy(risk_scores)
        object.__setattr__(document, '_dimension_score_receipt', _HealthScoreReceipt(scope,
            dimensions, risk_scores, _digest(_HEALTH_RUBRIC), _digest({'dimensions': dimensions, 'risks': risk_scores})))
    for item in pending:
        marker = 'HEALTH_SCORE_ASSESSMENT_INCOMPLETE:' + item['code']
        if marker not in document.incomplete:
            document.incomplete.append(marker)
    return deepcopy(dimensions)
