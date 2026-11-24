"""Independent source review for priority, disease and fibre assessments."""
from __future__ import annotations

from copy import deepcopy

ASSESSMENT_SOURCE_ROLES = {'clinical':'clinical_record','self_reported':'patient_report',
    'function':'source_model_result','microbe':'microbiome_result','context':'source_context'}
NUMERIC_OVERVIEW_CODES = frozenset({'medical', 'nutrition', 'overall'})
NUMERIC_OVERVIEW_RULES = (
    '本目标只返回code、score、assessmentLevel及1至3个basis.factId；judgment/explanation为空，不返回basis.text。',
    '依据本target完整真实inventory与全部facts，按同一scoringRubric独立评值；代表basis不替代完整资料。',
    'sourceStatus、已采用显示分级与原始升降各自保真：异常升高不能因强档变成健康优势，原始下降不因某正常档被忽略。',
    '来源模型或关联筛查不是本人确诊或症状，0与false保留原义，不等于未检出。不得增造个人疾病或评分公式。',
    '仅用allowedFactIds，满足requiredFactIds及overall的minimumBasisDomains/requiredCoverage；分值0至100整数且越高状态越好。',
)


def metadata_fact_view(value):
    """Keep an adopted self-report's role without promoting a parser notice.

    Only the extractor's exact generated status is shortened in this writer
    view. The immutable source, value, certainty and fingerprint stay intact.
    """
    result=deepcopy(value)
    if (result.get('kind')=='self_reported' and result.get('status')=='用户自述，未核验'
            and str(result.get('factId') or '').startswith('self-reported-fact-')
            and str(result.get('source') or '').startswith('用户自述：')):
        result['status']='用户自述'
    result['expectedUseAs']=ASSESSMENT_SOURCE_ROLES.get(result.get('kind'))
    return result

def requires_independent_assessment_review(code):
    return isinstance(code, str) and (code.startswith(('priority:', 'disease:'))
        or code == 'component:dietaryFiberNeed' or code in NUMERIC_OVERVIEW_CODES)


def metadata_decision_errors(row, target):
    """Do not spend a review call on a structurally incomplete decision."""
    if not requires_independent_assessment_review(target.get('code')):
        return []
    allowed=target.get('allowedLevels') or []
    if target.get('code') in NUMERIC_OVERVIEW_CODES:
        if type(row.get('score')) is not int or not 0 <= row['score'] <= 100:
            return ['numeric_overview_requires_integer_score：综合分必须为0至100整数。']
        if any(row.get(key) not in (None, '') for key in ('judgment', 'explanation')) or any(
                isinstance(item, dict) and item.get('text') not in (None, '')
                for item in (row['basis'] if isinstance(row.get('basis'), list) else [])):
            return ['numeric_overview_has_unrequested_prose：只返回分数、等级及1至3个basis.factId；'
                'judgment/explanation为空，不以本轮任何未审医学理由授权分数。']
    if row.get('assessmentLevel') is None:
        return ['missing_assessmentLevel：每项必须返回assessmentLevel字段，逐字选择'
            + '、'.join(allowed) + '之一；不能用judgment或basis代替等级，也不能遗漏/null。']
    if row['assessmentLevel'] not in allowed:
        return ['invalid_assessmentLevel：assessmentLevel须逐字选择' + '、'.join(allowed) + '之一。']
    if str(target['code']).startswith(('priority:', 'disease:')) and any(key in row for key in ('score','value')):
        return ['综合关注等级目标不返回score/value。']
    if target['code']=='component:dietaryFiberNeed' and type(row.get('score')) is not int:
        return ['missing_component_score：必须返回本target锚点对应的整数score。']
    return []


def metadata_review_items(rows, targets, facts):
    items = []
    for row in rows:
        target = targets.get(row.get('code'))
        if not target or not requires_independent_assessment_review(row.get('code')):
            continue
        numeric = row['code'] in NUMERIC_OVERVIEW_CODES
        claim = (target['name'] + '综合评分为' + str(row['score']) + '，等级为' + row['assessmentLevel'] + '。'
            if numeric else row['judgment'])
        items.append({'id': row['code'], 'claim': claim, 'paragraph': claim,
            'sourceAssessmentAllowed': True, 'evidenceRole': 'individual_synthesis',
            'sourceAssessmentInstructions': ('只独立核对数值和等级：阅读本目标全部真实facts、原始升降与scoringRubric，'
                'basisFactIds只是代表，不替代完整输入。不要求自由医学说明；作者无判断文案不代表缺少评值依据。'
                '原始异常升高不能因模板强档变健康优势；没有足够依据支持该数值或等级时valid=false。'
                if numeric else '这是本次综合状态判断，不是要求原报告已有同样评语。'
                '逐项核对真实前提及评分规则，来源模型和菌群信号不能成为本人确诊。'),
            'assessment': {**{key: deepcopy(row.get(key)) for key in ('score','assessmentLevel','judgment')},
                **({'inventory': deepcopy(target.get('inventory', []))} if numeric else {})},
            'allowedLevels': deepcopy(target.get('allowedLevels', [])),
            'scoringRubric': deepcopy(target.get('scoringRubric')),
            'assessmentRules': deepcopy(target.get('rules', [])),
            'factIds': list(target['allowedFactIds']), 'basisFactIds': list(row['factIds']),
            'facts': [metadata_fact_view(facts[fid].model_dump(mode='json', by_alias=True))
                for fid in target['allowedFactIds'] if fid in facts],
            'evidence': None})
    return items


def metadata_review_errors(row, target, review, facts, *, original_ids=None):
    """A valid flag alone cannot supply the selected factual prerequisites."""
    if (not isinstance(review, dict) or review.get('id') != row.get('code')
            or review.get('valid') is not True):
        return ['独立审核未通过']
    if review.get('claimRole') not in {'source_assessment','source_fact'}:
        return ['综合等级审核须为源内状态判断']
    if review.get('unsupportedClaims') != []:
        return ['独立审核存在未支持断言或未逐项完成']
    scope = review.get('scope')
    if not isinstance(scope, dict) or scope.get('missing') != [] or not isinstance(scope.get('requirements'), list):
        return ['缺少独立审核的完整来源前提']
    known = original_ids or {}
    supported = set()
    allowed = set(target['allowedFactIds'])
    for requirement in scope['requirements']:
        if not isinstance(requirement, dict):
            return ['独立审核来源前提格式不正确']
        fid = requirement.get('factId')
        if original_ids is not None and fid not in original_ids:
            return ['独立审核使用了本请求之外的事实引用']
        fid = known.get(fid, fid)
        fact = facts.get(fid)
        quote = requirement.get('factQuote')
        if (fid not in allowed or fact is None or not isinstance(quote, str) or not quote.strip()
                or quote not in fact.value
                or requirement.get('purpose') != 'description'):
            return ['独立审核引用的来源值、角色或用途不匹配']
        if requirement.get('useAs') != ASSESSMENT_SOURCE_ROLES.get(fact.kind):
            return [f'事实{fact.name}的来源角色须为{ASSESSMENT_SOURCE_ROLES.get(fact.kind)}，'
                '按本次expectedUseAs核对，不能仅按名称猜来源类型']
        supported.add(fid)
    if not set(row['factIds']) <= supported:
        return ['独立审核未覆盖所选真实代表依据']
    return []
