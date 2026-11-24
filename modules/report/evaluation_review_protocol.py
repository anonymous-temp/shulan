"""One bounded repair of malformed review metadata, before the strict gate.

This module never approves a claim or changes a patient's source facts. A model
review marked valid can still have an invalid patient-premise DTO; requesting
the same review again is preferable to manufacturing a source binding locally.
"""

from copy import deepcopy
from collections.abc import Callable


def _repairable_positive_review(row: dict) -> bool:
    if (row.get('valid') is not True or row.get('unsupportedClaims') != []
            or row.get('applicability') not in {'applicable', 'general'}
            or row.get('clinicalRelevance') not in {'direct', 'general'}):
        return False
    scope = row.get('scope')
    return isinstance(scope, dict) and scope.get('missing') == []


def _metadata_errors(row: dict) -> list[str]:
    if not _repairable_positive_review(row):
        return []
    scope = row['scope']
    requirements = scope.get('requirements') if isinstance(scope, dict) else None
    if not isinstance(requirements, list):
        return []
    errors = []
    if row.get('claimRole') in {'source_fact', 'source_assessment'} and not requirements:
        errors.append('scope.requirements: source_fact/source_assessment必须为本句实际使用的患者来源提供factId、逐字factQuote和useAs；'
            'valid=true不能省去来源绑定。只修审核元数据，不改原句或原始结果；无法核实时返回valid=false。')
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        fields = ('factId', 'factQuote', 'useAs', 'purpose') if row.get('claimRole') == 'source_assessment' else ('factId', 'factQuote')
        for field in fields:
            value = requirement.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f'scope.requirements[{index}].{field}: '
                    'valid=true 的患者前提必须逐字绑定真实来源；纯文献结论不属于患者适用前提。')
    return errors


async def repair_invalid_citation_review(client, payload: dict, response: dict, *,
        metadata_validator: Callable[[dict, dict], list[str]] | None = None,
        repair_receipts: list[dict] | None = None) -> dict:
    """Retry malformed positive reviews once; all substantive gates run later."""
    if payload.get('reviewMode') == 'selected_quotes_only':
        return response
    if not isinstance(response, dict) or not isinstance(response.get('items'), list):
        return response
    requested = payload.get('items')
    if not isinstance(requested, list):
        return response
    originals = {}
    for item in requested:
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            originals.setdefault(item['id'], []).append(item)
    reviewed = {}
    for row in response['items']:
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            reviewed.setdefault(row['id'], []).append(row)
    repair_items = []
    for identifier, rows in reviewed.items():
        if len(rows) != 1 or len(originals.get(identifier, [])) != 1:
            continue
        errors = _metadata_errors(rows[0])
        if metadata_validator is not None and _repairable_positive_review(rows[0]):
            # Inspect copies only. The optional validator identifies metadata
            # omissions; it cannot rewrite the author's facts or the review.
            extra = metadata_validator(deepcopy(originals[identifier][0]), deepcopy(rows[0]))
            if not isinstance(extra, list) or any(not isinstance(error, str) for error in extra):
                raise TypeError('metadata_validator must return list[str]')
            errors = list(dict.fromkeys([*errors, *extra]))
        if errors:
            item = deepcopy(originals[identifier][0])
            item['previousReview'] = deepcopy(rows[0])
            item['validationErrors'] = errors
            repair_items.append(item)
    if not repair_items:
        return response
    request = deepcopy(payload)
    request['items'] = repair_items
    request['reviewMetadataRepair'] = True
    request['rules'] = [*request.get('rules', []),
        '仅重新审核这些原句并修复审核元数据，不改写claim、paragraph、患者事实或证据。',
        '本次仅重审previousReview.valid=true且unsupportedClaims=[]的元数据遗漏。validationErrors中的来源绑定缺失不等于原语义审核判为false；必须准确读取previousReview，不得虚称原valid=false。补齐真实绑定后重新给出本句实际审核结论；如重新发现实质无据，仍返回valid=false并说明。程序不会强制通过。',
        '若validationErrors指出具体factId的factQuote非逐字，并提供真正quoteText：重新核对该条真实事实，从原字段或真正quoteText逐字复制引文；不要按近似字词自行纠错，不改原句或来源值。该事实不能支持本句时返回valid=false，不能借其他项目或改写患者前提来通过。',
        '若validationErrors指出supportQuoteIds遗漏句内原子断言，须从本item完整原文的真实quoteOptions重新核对每一断言并选择QID；不得自动补号、借其他论文补句，完整原文仍不支持时返回valid=false。',
        'supportQuoteIds用于文献对原句的支持；requirements只列实际使用的患者描述和研究适用条件。纯文献结论不得填成缺少factId/factQuote的患者条件。',
        'source_fact原句已明确提到的每个来源项目均须绑定各自原factId/factQuote；程序展示分级的归属见原assessmentContext，不能擅自把本报告或原区间判读改成原件印刷标签。纯文献结论使用其真实用途，不能改成source_fact来省略引文。',
        'source_assessment仅适用于sourceAssessmentAllowed=true的源内AI状态/优势短板/关注顺序/线索强弱/可干预性判断，requirements须有实际factId/factQuote/useAs/purpose=description；不要求来源原文含AI评级措辞。新增疾病、机制、疗效、药物用途、人体含量或药敏不能使用此角色免引文。不得把原valid=false或unsupportedClaims非空修成通过。',
        '若condition只是论文的一般结论或诊断原则，而不是必须由患者满足的入组/干预条件：用supportQuoteIds证明该结论，并从requirements移除这个分类错误的非患者条目。不要改成另一个空factId条目，也不要要求患者已经完成论文所解释的检查来证明论文结论。',
        '不得为了通过审核而删除真实适用条件或编造患者事实。实际需要的患者条件未提供时返回valid=false并说明missing；所有保留的患者前提仍须原事实逐字证明。',
        '返回原id的完整审核对象；后续仍执行全部原有严格校验。']
    try:
        repaired = await client.generate(request)
    except Exception:
        return response
    if not isinstance(repaired, dict) or not isinstance(repaired.get('items'), list):
        return response
    replacements = {}
    allowed = {item['id'] for item in repair_items}
    for row in repaired['items']:
        if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] in allowed:
            replacements.setdefault(row['id'], []).append(row)
    if repair_receipts is not None:
        # A returned original response is not evidence that a fresh review
        # happened. Record only actual, uniquely returned repair replies.
        repair_receipts.extend({'item': deepcopy(item), 'review': deepcopy(replacements[item['id']][0])}
            for item in repair_items if len(replacements.get(item['id'], [])) == 1)
    result = deepcopy(response)
    result['items'] = [deepcopy(replacements[row['id']][0])
        if isinstance(row, dict) and isinstance(row.get('id'), str)
        and len(replacements.get(row['id'], [])) == 1 else row
        for row in result['items']]
    return result


_TRIGGER_MODIFIABILITY_STANDARDS = {
    '高': '本行存在当前真实、可自主改变的不良行为或暴露；不因理论上可改变就判高。',
    '中': '本行有当前真实的改变对象，且本人原文明确存在现实外部安排或医生约束；工作日、资料未知或一般用药常识本身不证明约束。',
    '低': '固定背景、没有明确当前改变对象或改变性资料不足时的默认等级；不表示存在低风险或确认病因。',
    '维持': '本行真实存在可继续维持的保护行为，不把无暴露或未知对象改写成保护行为。',
    '—': '本行未知、没有当前可改对象，或为家族史等固定背景；不制造不良行为或约束来补等级。',
}
_TRIGGER_DOMAIN_INSTRUCTIONS = {
    'exercise': ('分别采用本人的实际运动及久坐，结合本节点已审当前问题或同一行为管理原则解释一至两个最有依据的管理关注方向。'
        '有适用指南时说明活动在该健康问题管理中的意义，不固定围绕便秘，不复制计划动作或暗示已证明病因。'
        '没有适用原文时不造生理作用，只有纯并存仍留空。'),
    'sleep': ('明确给出的实际睡眠时长与仅有就寝起床时点分开；已有明确时长时，优先用本节点适用的成年人睡眠时长指南评价睡眠充足性这一作息关注方向。'
        '不必强接便秘或反酸，不由时点推算时长，不把睡眠不足推成既有失眠诊断。没有适用参考原文不擅自补阈值；'
        '已有真实作息仍可独立评判线索强弱与可干预性，不能因没有便秘关系便一律不返回等级。'),
    'familyHistory': ('源内判断只归纳已提供血亲的真实确诊疾病史与固定关注背景，保留是谁的病史，'
        '不得写成本人疾病或本人遗传易感、遗传机制、新疾病关联。本人血压等检查结果与血亲确诊史不是同一事实。'
        '源内固定背景的关注不要求原报告已写相同AI评语；新增科学关系如确有必要须另句引用真正适用原文，'
        '不能借孕期、父母双方病史或本人未提供的疾病条件外推。'),
    'medications': ('逐条区分当前用药、原报告历史用药与限定时间内的未使用记录；原报告有药名不证明当前仍服用或同时服用。'
        '明确当前用药种类为0时不新增药物调整对象，不以一般医生限制判中；过去六个月未用抗生素不扩成终生未用。'
        '只有当前真实改变对象和本人真实约束才可据此判断；不得从药名猜用途、将记录推成不适病因或新增停换药建议。'),
    'stress': ('采用本人真实压力负担及已有应对方式。有适用关系原文时说明与当前具体问题的联系；'
        '已有散步、阅读或其他有效放松记录时，可源内概括为当前保留的保护性应对方式，不强配便秘或反酸。'
        '关联列直接写已有放松应对的保护性关注判断，不复抄压力分数，也不把分数改成未经定义的轻中重；不写没有明确关联、无法确认因果或未发现联系。'
        '维持是对现有习惯的可干预性评级，不是新开出的生活方式处方；不把未记载的压力病因或精神疾病补入。'),
    'exposures': ('分别采用真实烟酒与环境记录。已明确不吸烟、无已知被动或职业暴露的项目可概括为本次保留的保护背景；'
        '主动饮酒单独列作已知生活习惯，不称饮酒有益，也不由低频推定容量、酒精剂量或无害。'
        '有适用原文时解释与本次具体问题的联系；无关系原文时直接归纳已知保护/可调整背景，'
        '不强配某病，不写无法确认因果等方法尾语，不复抄频次容量，不因仅有饮酒记录便断言不良暴露或判高。两个等级只根据同一行真实背景独立判断，不把无暴露杜撰成不良行为。'),
}
_TRIGGER_STANDARD_RULE = ('可干预性按gradingStandards.modifiability逐项评判：高为当前真实可改的不良行为；'
    '中须当前真实对象及本人明确现实约束；维持为实际保护行为；低用于未知、无当前可改对象或固定背景的默认等级；兼容旧值—。'
    '所有等级仍由本行真实资料判断，不抄校准值、不固定分配强/高，不把关联强弱当可改变性。')


def _trigger_grading_standards(payload):
    original = payload.get('gradingStandards')
    return {**(deepcopy(original) if isinstance(original, dict) else {}),
        'modifiability': deepcopy(_TRIGGER_MODIFIABILITY_STANDARDS)}


def _trigger_source_association(value, *, allowed_fact_ids=None):
    """Validate a caller-supplied draft literal, never a review approval."""
    if (not isinstance(value, dict) or not isinstance(value.get('text'), str)
            or not value['text'].strip() or not isinstance(value.get('factIds'), list)
            or not value['factIds'] or any(not isinstance(fid, str) or not fid.strip()
                for fid in value['factIds'])):
        return None
    if allowed_fact_ids is not None and (not isinstance(allowed_fact_ids, list)
            or any(not isinstance(fid, str) for fid in allowed_fact_ids)
            or not set(value['factIds']) <= set(allowed_fact_ids)):
        return None
    return {'text': value['text'], 'factIds': list(dict.fromkeys(value['factIds'])),
        'evidenceRefs': []}


def trigger_row_author_request(payload: dict) -> dict:
    """Give one already scoped trigger row typed author columns, not labels."""
    targets = payload.get('targets')
    if not isinstance(targets, list) or not targets or not all(
            isinstance(row, dict) and isinstance(row.get('code'), str)
            and row['code'].startswith('eval:["medicalAssessment","triggerAnalysis",')
            for row in targets):
        return payload
    sentence = {'type': 'object', 'required': ['text', 'factIds', 'evidenceRefs'],
        'properties': {'text': {'type': 'string'}, 'factIds': {'type': 'array', 'items': {'type': 'string'}},
            'evidenceRefs': {'type': 'array', 'items': {'type': 'integer'}}}}
    def grade(values):
        return {'type': 'object', 'required': ['value', 'factIds'], 'properties': {
            'value': {'enum': [*values, None]},
            'factIds': {'type': 'array', 'items': {'type': 'string'}}}}
    result = deepcopy(payload)
    result['triggerRowAuthoring'] = True
    result['gradingStandards'] = _trigger_grading_standards(payload)
    supplied_associations = {}
    for target in result['targets']:
        target.pop('triggerRepair', None)
        domain = next((row.get('code') for row in target.get('assessmentContext', [])
            if isinstance(row, dict) and row.get('semanticType') == 'triggerScope'), None)
        if domain in _TRIGGER_DOMAIN_INSTRUCTIONS:
            target['sourceDomainInstructions'] = _TRIGGER_DOMAIN_INSTRUCTIONS[domain]
        target['purpose'] = ('基于本行已提供的真实行为、健康表现与可用科学原文，共同判断关联内容、个体线索强度及当前可干预性；'
            '关联内容逐句置于association数组，两个等级分别置于evidenceStrength和modifiability对象。')
        target['sourceAssessmentInstructions'] = ('本行源内关注判断依据实际行为与健康表现，不要求原报告印有同样的AI评语。'
            '新医学机制、一般关系或改善效果须在association中独立成科学句并用真实适用原文引用，不能降为源内判断免审。')
        target['clinicalInterpretationInstructions'] = ('比较本行真实行为与已提供当前医学状态、症状及已评风险，选择一至两个真正有依据的方向，不固定配便秘。'
            'approvedManagementContext只提供同一行为/问题及其原文候选，关联句解释管理关注意义，不复制执行动作或反推病因；仍逐句原文及本人适用性独审。'
            '已有放松应对或保护背景可直接作源内保留判断，不需要先强配一个疾病。只用typed三列输出，不拼旧标签或理由段。')
        source_association = _trigger_source_association(target.get('sourceAssociation'),
            allowed_fact_ids=target.get('allowedFactIds', []))
        if source_association is not None:
            supplied_associations[target['code']] = source_association
            target['sourceAssociation'] = {key: deepcopy(source_association[key])
                for key in ('text', 'factIds')}
            if domain == 'familyHistory':
                target['sourceDomainInstructions'] = ('只根据当前真实血亲确诊史及固定背景判断两个等级，'
                    '保留是谁的病史，不新增本人疾病、遗传机制或其它疾病关联；'
                    '不另写关联、科学句或诊断边界附言。')
            target['purpose'] = ('本行sourceAssociation已提供来自当前真实资料的源内状态草稿；'
                '仅基于相同真实资料判断evidenceStrength和modifiability两个等级，'
                '不另写关联内容，不强配与便秘、反酸等其它问题的关系。')
            target['sourceAssessmentInstructions'] = ('sourceAssociation不是已审核结论，将与本次两个AI等级一起接受全新独立三列审核。'
                '只提出两个等级及其实际factIds；不改写源状态、不补科学句或诊断边界旁白，'
                '未知或不适用保留null，不固定分配等级。')
    result['outputSchema'] = {'sections': [{'code': '请求中的原code', 'state': 'ready或insufficient',
        'association': [sentence], 'evidenceStrength': grade(('强', '中', '弱', '不足')),
        'modifiability': grade(('高', '中', '低', '维持', '—'))}]}
    # This is one complete author protocol. Legacy trigger packets require
    # labels/sentences and forbid these typed columns; retaining those rules
    # would make the model choose between contradictory output contracts.
    result['rules'] = [_TRIGGER_STANDARD_RULE,
        '逐target遵守sourceDomainInstructions：家族史只据真实血亲确诊背景作源内关注，不据此推本人遗传机制；用药须区分当前和历史，明确当前0不虚构改变对象或医生约束。',
        '只使用当前target的allowedFactIds与现有facts、assessmentContext和实际evidence；项目、原值、原分级、日期、阴性与身份不改，不推算未给睡眠时长，不把0/false/正常当未检出，不编造检查或疾病。',
        '关联内容一至两句，以已有具体症状、疾病或风险为主语，直接说明与本行实际行为有支持的直接或间接关联；不复抄左列行为频次、时长或份量，不以资料审计、自述可信度或因果免责声明代替评估，也不强配无对应依据的问题。',
        '没有可支持的关联时association=[]且state=insufficient；两个等级仍只在有本人真实行为/状态依据时填写，否则value=null、factIds=[]。空关联不要求补写关系、重复观察数值或方法说明，等级仍逐列独立审核。压力行可概括已经存在的放松应对，不重抄压力分数；睡眠记录本身不证明其与便秘或反酸有关。',
        '每次均返回同一行三列完整新稿，不只补上一轮缺失列；不得把不同轮孤立等级与另一次关联拼成已审完整行。',
        '本次每个section就是一个诱因分析行。按association句数组、evidenceStrength对象、modifiability对象填写，不自行拼“关联分析：/证据强度：/可干预性：”标签；显示标签由程序按列名统一编译。',
        '三列须基于同一行的真实行为与当前健康表现共同判断。证据强度评价本人的这条诱因线索有多明确，不是论文GRADE等级，也不要求原报告印有AI判断的词；不得无条件把自述判弱或把所有同现判强。',
        '关联句逐句填实际factIds与evidenceRefs；后两列仅value合法等级与其真实factIds，不写句号、理由或引用。任一等级没有可采用的本人行为或状态依据时value=null、factIds=[]，不捏造当前改变对象或默认补级。',
        '可干预性针对本行当前实际对象。正常保护行为可维持，明确没有当前改变对象不按理论可改变判高；外部限制或医生约束必须有本人当前真实依据。',
        '新的病因、机制、健康影响或改善效果仍单独成科学句并引用本节点真实适用原文，不能以三列共同判断为由免除原文独审。',
        '科学句仅用availableEvidenceNumbers中的实际原文，保留原主语、研究人群、适用条件、相关方向和强度；不把特殊人群或同现改为本人病因/疗效，不借其他行为、其他问题或另一篇论文补断言。无适用科学原文时可保留有据源内判断并返回insufficient。',
        '只返回outputSchema指定的严格JSON；每个section的code沿用请求，每个事实引用沿用本次允许的原引用。state仅ready或insufficient，不填编造的参考范围、证据或方案。']
    if supplied_associations:
        result['outputSchema'] = {'sections': [dict(code=target['code'], state='ready或insufficient',
            **({} if target['code'] in supplied_associations else {'association': [deepcopy(sentence)]}),
            evidenceStrength=grade(('强', '中', '弱', '不足')),
            modifiability=grade(('高', '中', '低', '维持', '—'))) for target in result['targets']]}
        result['rules'] = [_TRIGGER_STANDARD_RULE,
            '每个target严格遵守自己的purpose、sourceDomainInstructions和outputSchema；源事实、原值、原级、日期、阴性及身份不改，0/false/正常不能代替未检出，不能推算未提供睡眠时长。',
            '有sourceAssociation的target只输出code、state、evidenceStrength和modifiability，不返回association、sentences、sourceAssociation、理由、标签或科学附句；程序将当前可信源状态草稿与两个AI等级共同交给全新独立三列审核，不把草稿视为已批准内容。',
            '有sourceAssociation时，证据强度评价该条真实线索有多明确；固定家族背景或明确当前无用药对象无需强配便秘、反酸等问题，不新增本人疾病、遗传机制、调整对象或无据医生限制。两个等级仍根据实际资料判断，未知/不适用可null，不能抄校准值或固定赋级。',
            '没有sourceAssociation的target仍输出association句数组及两个等级，三列均为本轮完整新稿；关联内容概括本行实际行为与已有健康表现的有据关系，不复抄左列频次、时长、份量，不用资料审计或泛化免责声明代替评估。',
            '没有sourceAssociation的target如需新的病因、机制、健康影响或改善效果，仍须在association中独立成科学句并引用真实适用原文；无适用证据不得补科学机制或疗效。',
            '等级对象仅value合法等级或null和实际factIds，不加句号、理由或引用。只使用当前target的allowedFactIds及当前facts、assessmentContext、calibration和实际evidence；不借其他行或其它人的资料。',
            '保留正常保护行为与真实当前约束的区别；每轮完整返回本target要求的全部列，不从上一轮补单列拼接。',
            '仅返回outputSchema指定严格JSON，沿用每个原code，state仅ready或insufficient；所有输出仍须真实独立审核和原有硬校验。']
    result['rules'].append('纯粹复述行为与症状并存、同现、同时存在或同一背景，没有具体个人关注或保护应对判断，不能填写关联；'
        '此类内容留在observations，association=[]，已有等级分别按本人真实来源独审。医学关系必须有本句适用原文并独审，不造科学联系。')
    result['rules'].append('没有sourceAssociation且无可采用关联时association=[]、state=insufficient，仍完整返回两个等级对象；'
        '等级没有本人真实行为/状态依据时value=null、factIds=[]，不默认补级。'
        '压力判断不复抄压力分数，睡眠判断不推造与其他症状的关系，任何空列都不以方法说明填充。')
    return result


def normalize_trigger_row_author_response(response: dict, *, source_associations=None) -> dict:
    """Compile drafts, using source literals only from the trusted caller map.

    A supplied literal has no approval metadata. The complete new triad still
    needs the unchanged independent source review and all substantive gates.
    """
    if not isinstance(response, dict) or not isinstance(response.get('sections'), list):
        return response
    result = deepcopy(response)
    for row in result['sections']:
        if not isinstance(row, dict):
            continue
        source_association = (_trigger_source_association(source_associations.get(row.get('code')))
            if isinstance(source_associations, dict) and isinstance(row.get('code'), str) else None)
        if source_association is None and 'association' not in row:
            continue  # Previously supported sentence-shaped responses stay compatible.
        associations = [source_association] if source_association is not None else row.get('association')
        if (not isinstance(associations, list) or any(
                not isinstance(part, dict) or not isinstance(part.get('text'), str) or not part['text'].strip()
                or not isinstance(part.get('factIds'), list) or any(not isinstance(fid, str) for fid in part['factIds'])
                or not isinstance(part.get('evidenceRefs'), list) or any(type(number) is not int for number in part['evidenceRefs'])
                for part in associations)):
            continue
        grades = []
        for field, label, allowed in (('evidenceStrength', '证据强度', {'强', '中', '弱', '不足'}),
                ('modifiability', '可干预性', {'高', '中', '低', '维持', '—'})):
            raw = row.get(field)
            if (not isinstance(raw, dict) or 'value' not in raw or raw.get('value') is not None and not isinstance(raw.get('value'), str)
                    or raw.get('value') not in {*allowed, None}
                    or not isinstance(raw.get('factIds'), list)
                    or any(not isinstance(fid, str) for fid in raw['factIds'])):
                break
            if raw['value'] is not None:
                grades.append({'text': label + '：' + raw['value'],
                    'factIds': list(raw['factIds']), 'evidenceRefs': []})
        else:
            sentences = []
            for index, part in enumerate(associations):
                text = part['text'].strip()
                # An author may redundantly supply the column label. This is
                # draft formatting, before any independent approval occurs.
                for label in ('关联分析：', '关联分析:'):
                    if text.startswith(label):
                        text = text[len(label):].lstrip()
                        break
                sentences.append({**deepcopy(part), 'text': ('关联分析：' if index == 0 else '') + text})
            sentences.extend(grades)
            compiled = {key: value for key, value in row.items()
                if key not in {'association', 'evidenceStrength', 'modifiability', 'sourceAssociation'}}
            compiled['sentences'] = sentences
            compiled['factIds'] = list(dict.fromkeys(fid for part in sentences for fid in part['factIds']))
            if not associations:
                compiled['state'] = 'insufficient'
            row.clear()
            row.update(compiled)
    return result


def trigger_row_review_request(payload: dict, items: list[dict], *, allowed_fact_ids,
        allow_empty_association=False, separate_scientific_association=False) -> dict | None:
    """Review three AI columns together with the same current source scope.

    This requests real independent decisions; it adds no approved source ID.
    A grade's newly proposed binding still requires the usual metadata review.
    """
    fields = {'association', 'evidenceStrength', 'modifiability'}
    if (not items or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or not isinstance(item.get('claim'), str) or item.get('triggerColumn') not in fields
            or item.get('evidence') is not None or item.get('sourceAssessmentAllowed') is not True
            for item in items)):
        return None
    grouped = {field: [row for row in items if row['triggerColumn'] == field] for field in fields}
    grade_only = bool(allow_empty_association and not grouped['association'])
    if (len({item['id'] for item in items}) != len(items)
            or not grouped['association'] and not grade_only
            or any(len(grouped[field]) > 1 or not grade_only and len(grouped[field]) != 1
                for field in fields - {'association'})):
        return None
    scopes = [context for row in items for context in row.get('assessmentContext', [])
        if isinstance(context, dict) and context.get('semanticType') == 'triggerScope']
    if not scopes or len({scope.get('code') for scope in scopes}) != 1:
        return None
    allowed = set(allowed_fact_ids)
    own = {fid for scope in scopes for fid in scope.get('factIds', [])} & allowed
    state = {fid for scope in scopes for fid in scope.get('patientStateFactIds', [])} & allowed
    available = {fact['factId']: fact for fact in payload.get('sourceInventory', [])
        if isinstance(fact, dict) and fact.get('factId') in allowed}
    for row in items:
        available.update({fact['factId']: fact for fact in row.get('facts', [])
            if isinstance(fact, dict) and fact.get('factId') in allowed})
    association_ids = {fid for row in grouped['association']
        for key in ('declaredFactIds', 'sourceBindingCandidateFactIds') for fid in row.get(key, [])} & allowed
    shared_ids = (own | state | association_ids) & set(available)
    from .evaluation_priority import trigger_modifiability_constraint_failures
    modifier = next(iter(grouped['modifiability']), {})
    mid = modifier.get('claim', '').replace(' ', '') in {'可干预性：中', '可干预性:中'}
    constraint_ids = {fid for fid in state & set(available) if mid
        and not trigger_modifiability_constraint_failures('中', scope={
            'code': scopes[0].get('code'), 'factIds': [], 'patientStateFactIds': [fid]}, facts=available)}
    if not own or not shared_ids:
        return None
    result = deepcopy(payload)
    result['items'] = deepcopy(items)
    result['sourceStateOnly'] = True
    result['gradingStandards'] = _trigger_grading_standards(payload)
    domain_rule = _TRIGGER_DOMAIN_INSTRUCTIONS.get(scopes[0].get('code'))
    if domain_rule:
        result['sourceDomainInstructions'] = domain_rule
    result['atomicTriggerRow'] = {'code': scopes[0].get('code'),
        'association': [{'id': row['id'], 'text': row['claim']} for row in grouped['association']],
        'evidenceStrength': ({'id': grouped['evidenceStrength'][0]['id'], 'text': grouped['evidenceStrength'][0]['claim']}
            if grouped['evidenceStrength'] else None),
        'modifiability': ({'id': grouped['modifiability'][0]['id'], 'text': grouped['modifiability'][0]['claim']}
            if grouped['modifiability'] else None),
        'associationUnavailable': grade_only and not separate_scientific_association,
        'scientificAssociationReviewPending': bool(separate_scientific_association),
        'behaviorFactIds': sorted(own), 'currentStateFactIds': sorted(state),
        'facts': [deepcopy(available[fid]) for fid in sorted(shared_ids)]}
    for item in result['items']:
        ids = (own | constraint_ids) & set(available) if item['triggerColumn'] == 'modifiability' else shared_ids
        item['facts'] = [deepcopy(available[fid]) for fid in sorted(ids & set(available))]
        item['sourceBindingCandidateFactIds'] = sorted(set(item.get('sourceBindingCandidateFactIds', [])) | ids)
        item['paragraph'] = '\n'.join(row['claim'] for row in items)
        item['purpose'] = '共同核实本行关联判断、个体线索强度与当前行为可干预性；不把强度误作论文证据等级。'
        if item['triggerColumn'] == 'modifiability':
            item['assessmentContext'] = [{**deepcopy(row), 'factIds': sorted(own),
                'patientStateFactIds': sorted(constraint_ids)} for row in item.get('assessmentContext', [])
                if isinstance(row, dict) and row.get('semanticType') == 'triggerScope']
    result['rules'] = [*result.get('rules', []), _TRIGGER_STANDARD_RULE,
        'sourceDomainInstructions如存在，逐句核实该领域来源职责。血亲确诊史只支持该血亲历史及固定关注，不能按源内判断批准本人遗传机制；历史药名不证明当前用药，当前0不支持药物调整对象或凭常识判医生约束中。',
        'atomicTriggerRow是同一行完整原稿，须同时阅读三列与同一scope的原始行为、当前不适和检查记录，再对每个原id分别给出真实审核回执；不改写原句，不补固定等级。',
        'associationUnavailable=true表示本行尚无可采用关联，不生成、不证明、不补齐关联；只对实际提交等级及其本人来源分别审核。等级没有真实依据仍valid=false，不能因为关联空而默认补等级，也不能把缺少医学关联当作已记录行为不可评级。',
        'scientificAssociationReviewPending=true表示关联科学句正在另行接受原文双审，本组只审核实际提交的源内判断和等级；不因本组没有科学句而拒绝已有等级，也不替科学句批准关系或借等级证明科学关系。最终只有另行科学双审通过的句子才能发布。',
        '关联列若只有行为与不适同现/并存或观察清单，应valid=false；source_fact真实不等于source_assessment有用。只有具体个人关注、保护应对或明确源状态判断才可按相应职责审核；医学关联仍需适用原文，不用强度等级反过来证明关联。',
        '具体个人管理关注与医学作用分开：根据真实饮食结构或身体活动及当前真实血脂、腰围等状态，判断该行为主题是本人的可调整管理关注环节，可按source_assessment独审，不要求另有论文证明这一关注选择。它必须给出具体管理意义，不能只有同属、同时存在或观察清单；不能声称该行为造成异常、影响指标、改善结局或适合某项具体治疗。后者仍须适用原文双审，普通关注判断不能代替科学句通过。',
        'evidenceStrength是本人的这条诱因线索强弱，不是GRADE或论文证据等级。已采用本人行为、症状与检查的源内综合判断可为source_assessment，不能仅因缺少论文学术分级就拒绝；也不能将同现自动当因果或强证据。',
        '强度与关联列共同按本行全部真实前提评判，必要时逐一绑定行为及当前健康表现；可干预性只绑定本行真实行为及assessmentContext.patientStateFactIds中已明确的本人当前实施约束，纯症状、历史或其他维度不能当约束。结合整行判断是否有当前改变对象、可维持的保护行为或真实限制。',
        '三列须彼此一致。正常保护行为不为凑诱因判高，无真实当前药物对象不借一般医生限制判中；不把未知或否认暴露写成已存在的不良行为。',
        '证据强度与可干预性中的强/中/弱/不足、高/中/低/维持/—只是独立评级；尤其可干预性：维持没有开出行动方案。按source_assessment及adviceKind=explanation核对等级与事实，不因维持一词误判为general_care或management。关联列若实际提出执行动作仍按真实职责拒绝，不能借评级规则免审。',
        '原机制、病因、人体功能或疗效必须引用实际适用科学原文，不能因为处于诱因行就使用source_assessment免审。只核本行来源/判断，科学句仍另走原文双审。',
        '每个源内判断的scope.requirements须逐字绑定实际factId/factQuote/useAs/purpose=description；一个字段的valid不能替另一个字段通过。返回全部原id；任一列无据或无法核实时明确valid=false，不凭上一轮或作者自称强/高通过。']
    return result


def trigger_row_optional_source_review_ids(request: dict, response: dict) -> set[str]:
    """Recognise only independently classified extra non-source sentences.

    No verdict is approved here. The first source association and the two
    judgement columns remain mandatory even if the reviewer calls them
    science; unknown, missing and duplicate replies always fail closed.
    """
    if not isinstance(request.get('atomicTriggerRow'), dict):
        return set()
    items = request.get('items')
    replies = response.get('items') if isinstance(response, dict) else None
    if (not isinstance(items, list) or not isinstance(replies, list)
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in items)
            or len({row['id'] for row in items}) != len(items)):
        return set()
    first = next((row['id'] for row in items if row.get('triggerColumn') == 'association'), None)
    by_id = {}
    for row in replies:
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            by_id.setdefault(row['id'], []).append(row)
    known_non_source = {'uncertainty', 'background', 'mechanism', 'management', 'population_association'}
    return {item['id'] for item in items if item.get('triggerColumn') == 'association'
        and item['id'] != first and item.get('evidence') is None
        and len(by_id.get(item['id'], [])) == 1
        and by_id[item['id']][0].get('claimRole') in known_non_source}


def medical_navigation_review_request(payload: dict, items: list[dict]) -> dict | None:
    """Review ordinary consultation as consultation, never as a new test.

    This selects a duty, not a verdict. Every literal premise, specialty and
    action still receives a new independent response and existing validation.
    """
    if not items or not all(item.get('evidence') is None and item.get('managementInstructions')
            and any(row.get('semanticType') == 'medicalFindingScope'
                for row in item.get('assessmentContext', ())) for item in items):
        return None
    result = deepcopy(payload)
    result['ordinaryMedicalNavigationReview'] = True
    result['rules'] = [
        '逐item独立核对本人真实来源、科室、评估对象和目的。只审核原claim，不重写、不补症状或固定通过。返回所有原id及完整原outputSchema。',
        '本组是普通专业导航，不是用文献证明新疾病或检查适应证。围绕真实当前异常/持续主诉给常规咨询评估，或在常规就诊时讨论已有潜在信号的个人意义，可无引文；不能仅因quoteOptions为空、未确诊或仅有C信号就否定一切咨询导航。',
        '如/若今后出现困扰时再向相应专科咨询，是条件式未来安排，不断言症状目前存在；不要把条件句转为必须已存在的患者入组前提。仍须核对科室及表达是否适当，不借条件式编写诊断、危险程度或治疗。',
        '判断是否需要进一步评估/检查是就诊时由临床判断的目的，不等于已经开出某检查。具体命名新检验、影像、内镜、活检、药物、补剂、紧急就医等级或治疗获益仍须本项实际支持；本组无原文，超出普通咨询的这类内容必须拒绝，不能凭信号给疾病专属筛查适应证。',
        '检测结果/菌群信号不能升级为本人已患病；新增医学因果、作用机制、病理解释或把腰围等观察另写成未经证实诊断必须拒绝。常规咨询目的不是新的治疗效益断言。',
        '每项实际使用的已存在观察须逐字factId/factQuote/useAs/purpose=description，既有阴性或未记录不等于排除。条件式未来症状不伪填成已存在事实，也不借旁人的情况或不相关项目作本人前提。',
        '可通过的纯咨询行动用claimRole=management、scope.adviceKind=referral_or_test、supportQuoteIds=[]；scope.requirements绑定真正支持导航的已有观察/信号，不能创造原文或患者事实。所有valid、applicability、clinicalRelevance和unsupportedClaims均按实际内容判断，含无据新医学断言则valid=false并指出具体原因。',
    ]
    return result


def enforce_trigger_row_review(request: dict, response: dict) -> dict:
    """No partially approved source row can masquerade as a complete AI row."""
    if not isinstance(request.get('atomicTriggerRow'), dict):
        return response
    wanted = request.get('items', [])
    rows = response.get('items') if isinstance(response, dict) else None
    by_id = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            by_id.setdefault(row['id'], []).append(row)
    optional = trigger_row_optional_source_review_ids(request, response)
    failures = []
    for item in wanted:
        if item['id'] in optional:
            continue
        matched = by_id.get(item['id'], [])
        if len(matched) != 1:
            failures.append('本行字段独立回执缺失或重复：' + item['id'])
            continue
        row = matched[0]
        if (not _repairable_positive_review(row) or _metadata_errors(row)
                or not isinstance((row.get('scope') or {}).get('requirements'), list)
                or not row['scope']['requirements']):
            failures.append('本行字段尚未完整通过来源/判断核实：' + item['id'])
        if item.get('triggerColumn') in {'evidenceStrength', 'modifiability'} and row.get('claimRole') != 'source_assessment':
            failures.append('本行等级必须按源内判断真实审核：' + item['id'])
        if item.get('triggerColumn') == 'association' and row.get('claimRole') not in {'source_fact', 'source_assessment'}:
            failures.append('本行必需源判断未获明确来源角色审批：' + item['id'])
    if not failures:
        return response
    result = deepcopy(response) if isinstance(response, dict) else {}
    rejected = []
    for item in wanted:
        matches = by_id.get(item['id'], [])
        row = deepcopy(matches[0]) if len(matches) == 1 else {'id': item['id']}
        if item['id'] in optional:
            rejected.append(row)  # Keep the real verdict; never turn false into true.
            continue
        prior = row.get('unsupportedClaims')
        row.update(valid=False, unsupportedClaims=list(dict.fromkeys([
            *(prior if isinstance(prior, list) and all(isinstance(text, str) for text in prior) else []),
            *failures])), reason='本行三列尚未共同通过独立核实；不提交孤立等级或不完整关联。')
        rejected.append(row)
    result['items'] = rejected
    return result
