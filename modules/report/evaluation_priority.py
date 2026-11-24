"""Keep generated report-wide priority statements on the adopted ordering.

The builder owns that ordering. This validator neither ranks conditions nor
changes a diagnosis, source value, grade, score or original qualification.
"""
from __future__ import annotations

import re
import unicodedata


_GLOBAL_PATHS = {
    ('healthOverview', 'overallSummary', 'text'),
    ('comprehensiveConclusion', 'overallConclusion', 'text'),
}
_FIRST = re.compile(r'首先|首要|最优先|第一(?:位|个)?(?:关注|重点|问题)|'
    r'最(?:重要|紧要|关键|紧迫)|先(?:行|予|关注|处理|解决|落实|安排|评估|围绕)|'
    r'最(?:需要|应当|应|需)[^，,。；;\n]{0,12}(?:关注|处理|解决)|优先')
_NEXT = re.compile(r'其次|然后|随后|再(?:关注|处理|评估)|同时(?:将|把|关注)|最后')
_QUALIFICATION = re.compile(r'(?:疑似|可能|待排|性质待定).*$')
_IMAGING = re.compile(r'影像|超声|磁共振|X线|(?:^|[^A-Za-z])(?:CT|MRI)(?:[^A-Za-z]|$)', re.I)
_HARMFUL_GENERATION = re.compile(r'同型半胱氨酸|脂多糖|homocysteine|lipopolysaccharide', re.I)
_DEFICIT_STATE = re.compile(r'^(?:偏低|较低|低|降低|不足|缺乏|很弱|较弱|弱|合成能力弱|'
    r'失衡|异常|不健康倾向|亚健康|(?:[ⅠⅡⅢⅣIViv一二三四1-4]+度)?失调|正常[-－—–]?弱)$')
_SUMMARY_PATHS = {
    ('healthOverview', 'summaries', 'microbiome', 'text'),
    ('healthOverview', 'summaries', 'nutrition', 'text'),
    ('functionalPathways', 'summary', 'text'),
}


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _value(fact, key):
    return fact.get(key) if isinstance(fact, dict) else getattr(fact, key, None)


def _clinical_priority_key(value):
    """Match an actual clinical entity without its procedure wording."""
    return re.sub(r'可见|显示|观察到|发现|存在|试验|检测|测定|检查', '',
        _key(value)).replace('大便', '粪便')


def _clinical_priority_entity_keys(fact, focus_name):
    """Use literal bound A entities, never a guessed specimen or diagnosis.

    The narrative audit already recognizes complete source conclusions. Use
    that same finite entity set here: a main-exam prefix is not part of the
    patient's finding, and explicit 大便/粪便 wording names the same specimen.
    """
    names = {part.strip() for part in str(_value(fact, 'name') or '').split(' / ')
        if len(_key(part)) >= 2}
    if _value(fact, 'kind') == 'self_reported' and '便秘' in str(focus_name or ''):
        from .evaluation_fixed_assessment import _answer, _affirmative, _questionnaire_question
        answer = str(_answer(fact) or '')
        question = _questionnaire_question(fact)
        symptom = r'便秘|硬便|排便(?:费力|困难|不畅)|需要?用力'
        positive = any(not re.search(r'(?:否认|没有|未见|无|不|不用|无需)[^，,。；;]{0,10}$',
                clause[:match.start()])
            for clause in re.split(r'[，,。；;\n]|但是|然而|但', answer)
            for match in re.finditer(symptom, clause))
        if (_affirmative(fact, pattern=symptom) and positive
                and not re.match(r'\s*(?:是否|有无|会不会|请问是否)', answer)
                and not re.search(r'家族|家人|亲属|血亲|父亲|母亲', question + answer)):
            # These name the same adopted self-reported complaint. They do
            # not establish a constipation diagnosis from a question alone.
            names.update({'便秘', '排便困难', '排便费力', '排便不畅'})
    if _value(fact, 'kind') == 'clinical':
        # Lazy import keeps the shared entity audit authoritative without a
        # module-import cycle (the narrative imports this validation module).
        from modules.report.evaluation_narrative import _clinical_literal_entities
        if isinstance(fact, dict):
            from types import SimpleNamespace
            fact = SimpleNamespace(kind='clinical', value=str(_value(fact, 'value') or ''))
        focus = _clinical_priority_key(focus_name)
        names.update(entity for entity in _clinical_literal_entities(fact)
            if focus and (entity_key := _clinical_priority_key(entity))
            and (entity_key in focus or focus in entity_key))
        # A real qualitative result can be separated from its test name by
        # 呈/为 in prose. Only strip explicit result words from this bound
        # entity, not a specimen or a diagnostic qualification.
        names.update(stem for name in tuple(names)
            if (stem := re.sub(r'(?:弱阳性|阳性|阴性|偏高|偏低)$', '', name)) != name
            and len(_key(stem)) >= 2)
    return {_clinical_priority_key(name) for name in names if _clinical_priority_key(name)}


def is_harmful_generation_metric(row):
    """Identify a functional-row direction without assigning a health grade.

    Callers supply adopted functional metrics, not hospital analytes. Unknown
    scores remain ungraded; this helper supplies no concentration threshold.
    """
    if _value(row, 'kind') == 'clinical':
        return False
    names = ' '.join(str(_value(row, key) or '') for key in ('name', 'code'))
    if _HARMFUL_GENERATION.search(names):
        return True
    section = _value(row, 'sourceSection') or _value(row, 'source_section')
    return isinstance(section, str) and '有害代谢物' in section


def functional_direction_failures(text, *, path=(), facts=(), assessment_context=()):
    """Harmful generation is not a beneficial capacity to replenish.

    This validates the direction of prose only. It neither reverses a source
    grade nor interprets a score as a human metabolite concentration.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    known = list(facts.values()) if isinstance(facts, dict) else list(facts)
    function_names = {_key(_value(fact, 'name')) for fact in known
        if _value(fact, 'kind') == 'function' and _value(fact, 'name')}
    harmful_names = {name for name in function_names if _HARMFUL_GENERATION.search(name)}
    aggregate_name = lambda name: bool(re.fullmatch(
        r'有害代谢物(?:合成|生成)能力(?:汇总)?', str(name or '')))
    normal_aggregate = any(_value(fact, 'kind') == 'context'
        and aggregate_name(_value(fact, 'name'))
        and _value(fact, 'value') == _value(fact, 'status') == '正常' for fact in known)
    normal_aggregate_context = any(isinstance(row, dict)
        and aggregate_name(row.get('name')) and row.get('value') == row.get('sourceStatus') == '正常'
        and row.get('assessmentOrigin') == 'source' for row in assessment_context)
    normal_aggregate = normal_aggregate or normal_aggregate_context
    local_function_names = {_key(row.get('name')) for row in assessment_context if isinstance(row, dict)
        and row.get('factIds') and _key(row.get('name')) in function_names}
    p9_context = normal_aggregate_context and local_function_names <= harmful_names
    pathway_meaning = bool(tuple(path[:2]) == ('functionalPathways', 'pathways')
        and path[-1:] == ('healthMeaning',))
    harmful_pathway = (tuple(path[:3]) == ('functionalPathways', 'pathways', 8)
        or normal_aggregate_context and bool(local_function_names) and local_function_names <= harmful_names)
    if normal_aggregate:
        for sentence in re.split(r'[。；;！？\n]', text):
            if not (pathway_meaning or p9_context or re.search(r'有害(?:代谢物|负荷)|同型半胱氨酸|脂多糖', sentence)):
                continue
            # Actual clinical advice and a specific unresolved specimen keep
            # their own scope even when supplied beside this functional row.
            if any(_value(fact, 'kind') == 'clinical' and _key(sentence)
                    and _key(sentence) in _key(_value(fact, 'value')) for fact in known):
                continue
            if re.search(r'(?:标本|样本)[^，,。；;]{0,12}(?:待核实|待明确|不明|未明|未注明)', sentence):
                continue
            if harmful_pathway and re.search(
                    r'(?:整体|总体|本通路|本功能域)[^。；;]{0,18}(?:偏低|偏弱|较弱|不足)|'
                    r'(?:模型|合成|生成)(?:评分|分值|估计|能力)?[^。；;]{0,10}(?:整体|总体)(?:偏低|偏弱|较弱)', sentence):
                return ['有害代谢物功能域已有同源整体正常结论，不能把未分级子项分值或其他菌群信号改写成整体偏低、偏弱或不足；'
                    '原值及原分级不变，应分别解释已确认的整体正常与实际菌群信号。']
            if re.search(r'未形成可比较的?(?:强弱|高低|风险)(?:判断|判定|结论)|'
                    r'(?:关注重点|重[点心]|应|需)[^。；;]{0,45}'
                    r'(?:原始报告|原报告|来源)[^。；;]{0,35}(?:复核|核对)|'
                    r'核对(?:原始|报告|数据)?来源', sentence):
                return ['本功能域已有同源有害代谢物合成/生成能力的真实组级正常结论；'
                    '不能用未形成可比较结论、核对来源或泛化复核话术替代该已采用状态。'
                    '须完整重写本句并保留组级正常，单项数值与未分级状态不变。']
    if not harmful_names:
        return []
    local_names = {_key(row.get('name')) for row in assessment_context if isinstance(row, dict)
        and row.get('factIds') and _key(row.get('name')) in function_names
        and row.get('semanticType') not in {'sourceReportedBackground', 'editorialFocus'}}
    implicit_scope = bool(tuple(path[:2]) == ('functionalPathways', 'pathways')
        and path[-1:] == ('healthMeaning',) and local_names and local_names <= harmful_names)
    adopted_names = {_key(row.get('name')) for row in assessment_context if isinstance(row, dict)
        and row.get('assessmentOrigin') in {'source', 'reference', 'report'}
        and row.get('assessmentLevel') is not None}
    ungraded_names = {_key(_value(fact, 'name')) for fact in known
        if _value(fact, 'kind') == 'function' and _key(_value(fact, 'name')) in harmful_names
        and _value(fact, 'status') in (None, '')
        and not (_value(fact,'reference_range') or _value(fact,'referenceRange'))
        and _key(_value(fact, 'name')) not in adopted_names}
    # A native aggregate's normal label never grades its children. Validate
    # positive and negative direction statements against each named original
    # observation before the generic negation/aggregate shortcuts below.
    individual = [fact for fact in known if _value(fact,'kind') == 'function'
        and _HARMFUL_GENERATION.search(str(_value(fact,'name') or ''))]
    direction = re.compile(r'升高|上升|增高|降低|下降|增加|减少|偏高|偏低|较高|较低|'
        r'高于|低于|升降|趋势|波动|平稳|稳定')
    for sentence in re.split(r'[。；;！？\n]', text):
        if (re.search(r'研究|文献|队列|体外|培养',sentence)
                and not re.search(r'本次|本组|本通路|来源|原报告|该次检测',sentence)):
            continue
        for clause in re.split(r'[，,]',sentence):
            if not direction.search(clause):
                continue
            mentioned = [fact for fact in individual if _key(_value(fact,'name')) in _key(clause)]
            if not mentioned and (harmful_pathway or implicit_scope) and re.search(
                    r'两项|两种|二者|两者|各项|子项|分项|(?:2|两)个|两条|(?:评分|分值)(?:均|都)',clause):
                mentioned = individual
            # “两项分值” may follow a comma after their explicit names.
            if not mentioned and (harmful_pathway or implicit_scope) and re.search(r'两项|二者|两者',sentence):
                mentioned = [fact for fact in individual if _key(_value(fact,'name')) in _key(sentence)]
            unknown = [fact for fact in mentioned if _value(fact,'status') in (None,'')
                and not (_value(fact,'reference_range') or _value(fact,'referenceRange'))]
            if unknown:
                return ['有害代谢物子项未给自身原始分级或参考范围，不能对其分值声称升降或趋势，'
                    '包括未见同向升高等否定式；组级正常或另一子项的原级不能下放。保留实际组状态及原始数值。']
    native_aggregate = any(_value(fact, 'kind') == 'context' and re.fullmatch(
        r'有害代谢物(?:合成|生成)能力(?:汇总)?', str(_value(fact, 'name') or ''))
        and _value(fact, 'status') not in (None, '') for fact in known)
    for sentence in re.split(r'[。；;！？\n]', text):
        named = any(name in _key(sentence) for name in harmful_names) or bool(
            re.search(r'有害(?:代谢物|负荷)|内毒素(?:生成|合成)', sentence))
        if not named and not implicit_scope:
            continue
        if re.search(r'不应|不能|不可|并非|不是|无需|不需', sentence):
            continue
        ungraded_scope = any(name in _key(sentence) for name in ungraded_names) or bool(
            implicit_scope and ungraded_names and not native_aggregate)
        if ungraded_scope and re.search(
                r'(?:评分|分值|估计|负荷|生成|合成|能力|相关|水平)[^。；;]{0,18}'
                r'(?:高于|低于|较高|较低|偏高|偏低|强弱|更强|更弱|偏离|温和|平稳)|'
                r'(?:负荷|浓度|含量)[^。；;]{0,8}(?:高|低|升高|降低|异常)', sentence):
            return ['未提供自身分级或适用参考范围的有害代谢物分值不能推定高低、强弱或人体负荷；'
                '不同项目的数字大小不产生风险排序。已有整体结论只归于原始聚合项目，不下放为子项分级。']
        deficit = re.search(r'短板|弱项|能力不足|缺乏|能力(?:更弱|较弱|偏弱|弱)|'
            r'(?:提高|提升|增强|补足|补充|恢复)[^。；;]{0,12}(?:合成|生成|能力)', sentence)
        reversed_advantage = re.search(r'(?:合成|生成|能力)[^。；;]{0,12}(?:强|升高|增强|提高)'
            r'[^。；;]{0,20}(?:优势|良好|更好)', sentence)
        if deficit or reversed_advantage:
            return ['有害代谢物生成方向不能套用有益营养合成的弱项/补足或高分优势逻辑；'
                '保留该项目已有数值与原分级，按有害生成的实际方向作源内判断，不新增人体浓度或阈值。']
    return []


def summary_state_coverage_failures(text, *, path=(), assessment_context=()):
    """A chapter summary must represent supplied deficits as well as strengths."""
    if tuple(path) not in _SUMMARY_PATHS or not isinstance(text, str) or not text.strip():
        return []
    if tuple(path) == ('healthOverview', 'summaries', 'nutrition', 'text') \
            and re.match(r'^\s*其中', text):
        return ['营养评估摘要须形成可独立阅读的整体判断；不能把原段落依赖前文的其中子句'
            '单独恢复为整段摘要。应根据本段已采用的功能状态重写完整判断，'
            '保留原项目与原分级，不补人体营养缺乏诊断。']
    rows = []
    for row in assessment_context:
        if not isinstance(row, dict):
            continue
        if row.get('semanticType') == 'summaryFrame':
            rows.extend(row.get('items') or [])
        elif row.get('semanticType') == 'nutrientGroup':
            rows.extend(row.get('members') or [])
        elif row.get('factIds'):
            rows.append(row)
    deficits = []
    for row in rows:
        if _HARMFUL_GENERATION.search(str(row.get('name') or '')):
            continue
        label = row.get('adoptedLevel')
        if label is None and row.get('assessmentOrigin') in {'source', 'reference', 'report'}:
            label = row.get('assessmentLevel')
        if label is None:
            label = row.get('sourceStatus')
        if label is not None and _DEFICIT_STATE.fullmatch(str(label).strip()):
            deficits.append(row)
    if deficits and not re.search(r'低|弱|缺乏|不足|失调|失衡|亚健康|异常|偏移|短板', text):
        names = '、'.join(dict.fromkeys(str(row.get('name') or '') for row in deficits[:3]))
        return ['章节小结遗漏已采用的偏离或弱项（' + names + '）；'
            '须同时概括实际短板与保留功能，不能仅凭正常/强的代表项目写成整体维持优势。']
    if deficits and re.search(r'(?:整体|本次|本组|各项|全部|所有)[^。；;！？\n]{0,12}'
            r'(?:未见|没有|无|不存在)[^。；;！？\n]{0,8}(?:短板|弱项|偏弱|不足)', text):
        return ['已采用结果存在偏离或弱项，不能概括为整体未见短板；'
            '局部正常/强的功能保留其自身范围，并同时说明实际弱项所在。']
    return []




def trigger_modifiability_constraint_failures(level, *, scope, facts):
    """A middle action grade needs a real current implementation constraint.

    This necessary source check neither chooses a replacement grade nor
    supplies a relationship. C history, missing quantities, ordinary clock
    times and the theoretical ability to change a habit are not constraints.
    """
    if level != '中':
        return []
    from .evaluation_fixed_assessment import _answer, _affirmative, _questionnaire_question
    error = ['可干预性中须有本人当前原文明确的现实安排、医师限制或实际活动受限；'
        '本行未找到该约束，工作日、可改变对象或频次/时长/量缺失不能代替。'
        '请按同一分级标准独立重评，不自动改为高或补造限制。']
    if not isinstance(scope, dict):
        return error
    known = facts if isinstance(facts, dict) else {
        fid: fact for fact in (facts if isinstance(facts, (list, tuple)) else ())
        if isinstance(fid := (_value(fact, 'fact_id') or _value(fact, 'factId')), str) and fid}
    own = {fid for fid in (scope.get('factIds') or []) if isinstance(fid, str)} \
        if isinstance(scope.get('factIds'), (list, tuple)) else set()
    patients = {fid for fid in (scope.get('patientStateFactIds') or []) if isinstance(fid, str)} \
        if isinstance(scope.get('patientStateFactIds'), (list, tuple)) else set()
    constraint = (
        r'轮班|倒班|夜班|三班倒|固定(?:排班|班次|晚班)|'
        r'(?:工作安排|工作制度|工作场所|职业环境|同住|居住)[^，,。；;]{0,25}'
        r'(?:限制|受限|无法|不能|需要协调|需协调|必须|要求)|'
        r'(?:医生|医师|医嘱)[^，,。；;]{0,25}(?:限制|禁止|暂停|不得|不能|不宜|避免|不可自行)|'
        r'(?:需|须|必须)[^，,。；;]{0,6}(?:医生|医师)[^，,。；;]{0,12}(?:批准|评估|同意)|'
        r'(?:运动|活动|行动|行走|步行|负重)[^，,。；;]{0,4}(?:受限|困难)|'
        r'(?:不能|无法)(?:独立)?(?:行走|步行|活动|运动|负重)')
    constraint_token = r'轮班|倒班|夜班|三班倒|受限|限制|禁止|暂停|不得|不能|不宜|避免|不可自行|批准|评估|同意|困难|无法'
    relevant = {
        'diet': r'饮食|进食|饮水|吞咽|食物|进餐',
        'exercise': r'运动|活动|步行|行走|负重|锻炼',
        'sleep': r'睡眠|入睡|起床|作息|轮班|倒班|夜班|睡觉',
        'stress': r'压力|放松|情绪|心理|排班|轮班|工作安排',
        'medications': r'药物|用药|处方|停药|换药|剂量',
        'exposures': r'暴露|吸烟|抽烟|饮酒|工作场所|职业|同住|居住|家庭',
    }.get(scope.get('code') if isinstance(scope.get('code'), str) else None)
    for fid in own | patients:
        fact = known.get(fid)
        if _value(fact, 'kind') not in {'clinical', 'self_reported'}:
            continue
        value = _value(fact, 'value')
        if not isinstance(value, str):
            continue
        question = _questionnaire_question(fact)
        if re.search(r'家族|血亲|亲属病史|父母病史', question) or scope.get('code') == 'familyHistory':
            continue
        text = _answer(fact).strip()
        if question and text.startswith(question + '：'):
            text = text[len(question) + 1:]
        text = re.split(r'[；;]日期[:：]', text, maxsplit=1)[0].strip()
        if (re.search(r'既往|过去|以前|曾经|历史', question)
                and not re.search(r'目前|当前|现在|至今|仍', question + ' ' + text)):
            continue
        if re.fullmatch(r'不清楚.*|不知道.*|未知.*|不详.*|未评估.*|未提供.*|否|无|没有|false|0', text, re.I):
            continue
        answered_yes = bool(re.fullmatch(r'是|有|存在|true', text, re.I))
        if answered_yes:
            text = question  # A literal yes answers this same source question.
        if fid not in own and (relevant and not re.search(relevant, text)
                or not _affirmative(fact, pattern=constraint_token)):
            continue
        for clause in re.split(r'[，,。；;\n]|但是|然而|但', text):
            if re.search(r'以前|以往|曾经|既往|过去|去年|前年|曾|计划|准备|将来', clause):
                current = list(re.finditer(r'目前|现在|当前|现已|至今', clause))
                if not current:
                    continue
                clause = clause[current[-1].end():]
            if (re.search(r'不清楚|不知道|未知|不详|疑似|可能|如果|假如', clause)
                    or not answered_yes and re.search(r'是否|会不会|[？?]', clause)
                    or re.search(r'未限制|未受限|未禁止|没有限制|没有约束|不限制|不受(?:任何)?限制|'
                        r'(?:未|没有|不)(?:实行|采取|需要|安排)?(?:轮班|倒班|上夜班)|不上夜班|'
                        r'不再(?:轮班|倒班|上夜班|受限|限制)|无需协调|不需协调|'
                        r'可(?:以)?(?:自由|自主|自行)调整', clause)):
                continue
            # Reuse the existing clause-level denial parser. Household/work
            # constraints in an own behaviour answer may mention relatives;
            # patient-state family records have already been excluded above.
            sample = {'value': clause}
            if (_affirmative(sample, pattern=constraint_token)
                    or fid in own and _affirmative(sample, family=True, pattern=constraint_token)) \
                    and re.search(constraint, clause):
                return []
    return error


def trigger_source_association(scope, facts):
    """Return a narrow source frame, never a grade or an approval receipt.

    Only explicit B family diagnoses and a typed current zero-medication
    count qualify. The caller must obtain a new complete-row review before
    publishing this frame with any model-authored grades.
    """
    if not isinstance(scope, dict):
        return None
    from .evaluation_fixed_assessment import _answer, _affirmative, _questionnaire_question
    known = facts if isinstance(facts, dict) else {
        fid: fact for fact in (facts if isinstance(facts, (list, tuple)) else ())
        if isinstance(fid := (_value(fact, 'fact_id') or _value(fact, 'factId')), str) and fid}
    own = [fid for fid in scope.get('factIds', []) if isinstance(fid, str)] \
        if isinstance(scope.get('factIds'), (list, tuple)) else []
    kin = r'父亲|母亲|父母|兄弟|姐妹|兄长|姐姐|弟弟|妹妹|祖父|祖母|外公|外婆|儿子|女儿|血亲'
    if scope.get('code') == 'familyHistory':
        statements, ids = [], []
        for fid in own:
            fact = known.get(fid)
            if _value(fact, 'kind') != 'self_reported' or not isinstance(_value(fact, 'value'), str):
                continue
            for part in re.split(r'[，,。；;\n]|但是|然而|但', _answer(fact)):
                part = part.strip()
                if (not part or part not in _value(fact, 'value') or not re.search(kin, part)
                        or not re.search(r'诊断|确诊', part)
                        or re.search(r'未知|不详|不清楚|不确定|听说|可能|疑似|是否|[？?]|'
                            r'(?:未|无|没有|否认)[^，,。；;]{0,8}(?:诊断|确诊)|'
                            r'(?:父亲|母亲|兄弟|姐妹)的(?:朋友|同事|妻子|丈夫)', part)
                        or not _affirmative({'value': part}, family=True, pattern=r'诊断|确诊')):
                    continue
                if part not in statements:
                    statements.append(part)
                if fid not in ids:
                    ids.append(fid)
        return {'text': '；'.join(statements) + '，作为本次固定家族背景关注。', 'factIds': ids} if statements else None
    if scope.get('code') != 'medications':
        return None
    marked = scope.get('currentMedicationZeroFactIds')
    if not isinstance(marked, (list, tuple)):
        return None
    zeros = []
    for fid in marked:
        fact = known.get(fid) if isinstance(fid, str) and fid in own else None
        if _value(fact, 'kind') != 'self_reported' or not isinstance(_value(fact, 'value'), str):
            continue
        question = _questionnaire_question(fact)
        if (not re.search(r'目前|当前|现在|每日|每天', question)
                or not re.search(r'用药|药物|服药', question)
                or not re.search(r'种类数|种数|数量', question)
                or re.search(r'父亲|母亲|父母|兄弟|姐妹|家族|血亲', question)
                or re.search(r'既往|过去|以前|曾经|历史|去年', question)
                and not re.search(r'目前|当前|现在', question)
                or not re.fullmatch(r'\s*0(?:\.0+)?\s*(?:种)?\s*', _answer(fact), re.I)):
            continue
        if fid not in zeros:
            zeros.append(fid)
    if not zeros:
        return None
    patients = scope.get('patientStateFactIds') if isinstance(scope.get('patientStateFactIds'), (list, tuple)) else []
    for fid in [*own, *patients]:
        fact = known.get(fid) if isinstance(fid, str) else None
        if fid in zeros or _value(fact, 'kind') not in {'clinical', 'self_reported'} \
                or not isinstance(_value(fact, 'value'), str):
            continue
        question, text = _questionnaire_question(fact), _answer(fact)
        if (not re.search(r'目前|当前|现在|正在|每日|每天', question + ' ' + text)
                or not re.search(r'用药|药物|服药|处方|补充剂|抗生素', question + ' ' + text)
                or re.search(r'既往|过去|以前|曾经|历史', question)
                and not re.search(r'目前|当前|现在|正在', text)):
            continue
        for part in re.split(r'[，,。；;\n]|但是|然而|但', text):
            part = part.strip()
            if (not part or re.fullmatch(r'0(?:\.0+)?\s*(?:种)?|false|无|否', part, re.I)
                    or re.search(r'不清楚|不知道|未知|不详|未提供|曾|以前|过去|既往|用过|已停|停用|停药|已结束', part)
                    or re.search(r'(?:未|不|没有|无|否认)(?:自行|正在|目前|当前|长期|任何|明确|报告|在){0,4}'
                        r'(?:服用|口服|用药|服药|使用|用)', part)):
                continue
            if (_affirmative({'value': part}, pattern=r'服用|口服|用药|药物|处方|药|补充剂|抗生素')
                    or re.search(r'本人|我|自己', part)
                    and _affirmative({'value': part}, family=True, pattern=r'服用|口服|用药|药物|处方|药|补充剂|抗生素')):
                return None
    return {'text': '当前无用药调整对象。', 'factIds': zeros}


def _local_function_priority_failures(text, path, assessment_context):
    """A pathway may order its own findings, not the report's other chapters."""
    if not (len(path) == 4 and tuple(path[:2]) == ('functionalPathways', 'pathways')
            and path[-1] == 'healthMeaning' and isinstance(text, str)):
        return []
    names = set()
    for row in assessment_context:
        if row.get('semanticType') in {'sourceReportedBackground', 'editorialFocus'}:
            continue
        if row.get('factIds') or row.get('semanticType') == 'nutrientGroup':
            names.update(_key(name) for name in [row.get('name'), *(row.get('aliases') or [])]
                if isinstance(name, str) and len(_key(name)) >= 2)
        if row.get('semanticType') == 'antimicrobialResistance':
            names.add('耐药基因')
    comparison = re.compile(r'(?:排在|排到|置于)(?P<object>[^，,。；;！？\n]{1,70}?)(?:之前|之后|前面|后面)|'
        r'(?:优先于|先于|次于|后于)(?P<direct>[^，,。；;！？\n]{1,70})')
    for sentence in re.split(r'[。；;！？\n]', text):
        if not re.search(r'关注|管理|优先级|全局', sentence):
            continue
        for match in comparison.finditer(sentence):
            scope = _key(match.group('object') or match.group('direct'))
            if re.fullmatch(r'(?:本组|本通路|本功能域)(?:其他|其余|正常|较弱|弱|强)*'
                    r'(?:项目|子项|功能|方向|部分)', scope):
                continue
            remainder = scope
            for name in sorted(names, key=len, reverse=True):
                remainder = remainder.replace(name, '')
            remainder = re.sub(r'以及|相关|对应|项目|子项|方向|部分|指标|功能|[和与及等的]', '', remainder)
            if not names or remainder:
                return ['单条功能通路只解释自身结果与组内关注顺序，不能将本通路排在其他章节的问题之前或之后；'
                    '全局优先级由健康总览与综合结论统一呈现。请完整重写本句的自身保留优势或局部短板，'
                    '不通过补入其他章节事实重排全局，也不删去后半句后保留未经审核的前半句。']
    return []


def priority_boundary_failures(text, *, path=(), facts=(), assessment_context=()):
    """Reject a contradictory first-priority paragraph as a complete block.

    An adopted rank-one finding is authoritative for both global
    summaries. Mere mention later in the paragraph does not establish that
    it was actually selected first. Other chapters and unranked inputs keep
    their original responsibilities.
    """
    scoped_summary = (len(path) == 4 and tuple(path[:2]) == ('healthOverview', 'keyFindings')
        or tuple(path) == ('healthOverview', 'summaries', 'nutrition', 'text'))
    if scoped_summary and isinstance(text, str) and re.search(
            r'(?:营养功能|营养代谢|全报告|整体报告|本次评估)(?:中|的|范围内)?[^。；;]{0,18}'
            r'(?:最需要优先|最值得优先|首要|最高优先)|优先级更高', text):
        return ['分项核心提示只说明自身主题的真实短板，不另定全报告或全营养最高优先级；'
            '总体顺序使用本报告已采用的重点关注方向，完整重写本段，不剪掉比较词后保留旧判断。']
    local = _local_function_priority_failures(text, path, assessment_context)
    if local:
        return local
    clinical_summary = tuple(path) == ('healthOverview', 'summaries', 'medical', 'text')
    clinical_finding = len(path) == 4 and tuple(path[:2]) == ('healthOverview', 'keyFindings')
    if (tuple(path) not in _GLOBAL_PATHS and not clinical_summary and not clinical_finding
            or not isinstance(text, str)):
        return []
    known = facts if isinstance(facts, dict) else {
        _value(fact, 'fact_id') or _value(fact, 'factId'): fact for fact in facts}
    focuses = [row for row in assessment_context if isinstance(row, dict)
        and row.get('semanticType') == 'editorialFocus'
        and isinstance(row.get('rank'), (int, float)) and not isinstance(row['rank'], bool)
        and any(fid in known and _value(known[fid], 'kind') in (
            {'clinical', 'self_reported'} if clinical_summary else
            {'clinical', 'self_reported', 'microbe', 'function', 'context'})
            for fid in row.get('factIds', []))]
    if not focuses:
        return []
    first_rank = min(row['rank'] for row in focuses)
    leads = [row for row in focuses if row['rank'] == first_rank]
    aliases = set()
    for row in leads:
        names = [row.get('name', '')]
        names.extend(_value(known[fid], 'name') or '' for fid in row.get('factIds', []) if fid in known)
        for fid in row.get('factIds', []):
            if fid in known:
                aliases.update(_clinical_priority_entity_keys(known[fid], row.get('name')))
        for name in names:
            aliases.update(part for fragment in re.split(r'[，,；;、\n]', name)
                for candidate in (fragment, _QUALIFICATION.sub('', fragment))
                if len(part := _key(candidate)) >= 2)
            if _IMAGING.search(name):
                aliases.update({'影像异常', '影像问题', '影像发现', '影像记录', '影像中的'})
    aliases = {_clinical_priority_key(alias) for alias in aliases}
    for sentence in re.split(r'[。；;！？\n]', text):
        cue = _FIRST.search(sentence)
        if cue is None:
            continue
        before, after = sentence[:cue.start()], sentence[cue.end():]
        after = _NEXT.split(after, maxsplit=1)[0]
        selected = any(alias in _clinical_priority_key(after) for alias in aliases)
        # A subject can precede a generic action: "the CT finding ... should
        # first receive specialist assessment". A different named problem
        # following the cue cannot borrow an earlier mention of that finding.
        generic_action = re.match(r'\s*(?:由|按|落实|安排|进行|接受|予以|予)[^。；;\n]{0,30}'
            r'(?:专科|随诊|评估|检查|处理)', after)
        subject_priority = re.match(r'\s*(?:于|问题|重点|方向|任务)', after)
        subject_assessment = re.fullmatch(r'\s*(?:关注(?:的)?(?:临床|医学)?(?:异常|问题|重点|事项)|'
            r'明确(?:其|该项|本项)?(?:出血)?原因(?:的)?(?:独立)?(?:临床|医学)?(?:异常|问题)?)\s*', after)
        if selected or (generic_action or subject_priority or subject_assessment) and any(
                alias in _clinical_priority_key(before) for alias in aliases):
            return []
        return ['全局综合段落的首个优先判断与已采用editorialFocus首位关注问题不一致；'
            '须整段重写并保持第二章与第八章同一关注顺序，不能仅在后文补提该问题']
    return []
