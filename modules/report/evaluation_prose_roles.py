"""Private prose responsibilities shared by authoring and publication.

These rules describe a schema slot, never a patient, score or medical claim.
Source truth and original-text citation reviews remain separate authorities.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ProseResponsibility:
    role: str
    min_sentences: int = 1
    max_sentences: int = 3
    source_state_can_complete: bool = False
    management_can_complete: bool = False


def prose_responsibility(path) -> ProseResponsibility:
    path = tuple(path)
    if path in {('healthOverview', 'overallSummary', 'text'),
            ('comprehensiveConclusion', 'overallConclusion', 'text')}:
        return ProseResponsibility('integrated_summary', 1, 4, True)
    if path[:2] == ('healthOverview', 'summaries'):
        return ProseResponsibility('domain_summary', 1, 5, True)
    if (path[:2] == ('healthOverview', 'keyFindings') and path[-1:] == ('summary',)
            or path[:2] == ('comprehensiveConclusion', 'priorityDirections') and path[-1:] == ('basis',)):
        return ProseResponsibility('problem_focus', 1, 3, True)
    if path[:2] == ('comprehensiveConclusion', 'nextSteps') and path[-1:] == ('description',):
        return ProseResponsibility('management', 1, 3, False, True)
    if (path == ('medicalAssessment', 'description')
            or path == ('healthOverview', 'overallScore', 'explanation')
            or path[:2] == ('healthOverview', 'assessmentChart') and path[-1:] == ('explanation',)):
        return ProseResponsibility('assessment_caption', 1, 3, True)
    if path[-1:] in {('followupAction',), ('sourceAdvice',)}:
        return ProseResponsibility('management', 1, 3, False, True)
    if path[:2] == ('medicalAssessment', 'findings') and path[-1:] == ('explanation',):
        return ProseResponsibility('clinical_explanation', 1, 5, True)
    if ((path[:1] == ('nutritionAssessment',) and path[-1:] == ('text',))
            or (path[:2] == ('functionalPathways', 'pathways') and path[-1:] == ('healthMeaning',))
            or path == ('functionalPathways', 'summary', 'text')
            or (path[:1] == ('microbiomeAssessment',) and path[-1:] in {('text',), ('explanation',), ('interpretation',)})):
        return ProseResponsibility('measured_result_explanation', 1, 3, True)
    if path[:2] == ('diseaseRiskAssessment', 'categories') and path[-1:] == ('explanation',):
        return ProseResponsibility('disease_explanation', 1, 3)
    return ProseResponsibility('other', 1, 0)


def source_state_can_complete(path) -> bool:
    return prose_responsibility(path).source_state_can_complete


def sentence_budget(path) -> tuple[int, int] | None:
    role = prose_responsibility(path)
    return (role.min_sentences, role.max_sentences) if role.max_sentences else None


def responsibility_context(path) -> dict:
    role = prose_responsibility(path)
    return {'role': role.role, 'maxSentences': role.max_sentences or None,
        'sourceStateCanComplete': role.source_state_can_complete,
        'managementCanComplete': role.management_can_complete}


def author_responsibility(path) -> str:
    role = prose_responsibility(path)
    if not role.max_sentences:
        return ''
    duty = f'本节点最多{role.max_sentences}句；按实际资料决定篇幅，不为凑句数补写。'
    if role.source_state_can_complete:
        duty += ('本节点可用已核实的实际状态、组内分布或当前问题归纳完成，不强制添加科学作用或改善建议。'
            '原值、原级、成员范围和确定程度保持真实；只选解释本主题所需的少量依据。'
            '功能评分或合成估计明确其测量类型，不改成人体抗炎、免疫、解毒能力或营养充足/缺乏。')
    if not role.management_can_complete:
        duty += '纯管理建议不能代替本节点的结果或问题解释；具体行动留在相应建议字段。'
    else:
        duty += ('本节点独立承载同一问题的实际管理行动，不重复相邻解释。原报告建议只从私有原始来源读取；'
            '普通携报告咨询只说明向医生询问当前结果；疾病特异随访检查、新警示症状或策略仍需实际原文或明确原医嘱。'
            '新动作须绑定本问题真实适用前提并独立审查，不能借相邻解释的引用或把生成文字当原报告建议。')
    return duty + ('新增病因、机制、健康影响及干预效益仍须同对象、同方向及适用人群的实际原文，'
        '逐句绑定本节点事实和可用引用并独立审核；已有来源状态不授予医学关系。'
        '必要时简短说明真实测量对象与人体状态的区别，不写内部规则、审核过程或通用缺证声明。')


_PROCESS = re.compile(r'程序|后台|提示词|模板|生成逻辑|写作|编写|审核|独审|双审|已审|'
    r'可采用|准入|白名单|输出|不得|不要|不把|不将|不能把|不能将|'
    r'来源方向|原始方向|显示分级|显示档位|资料审计|证据不足|资料不足|无法判断')
_SCOPE_CUE = re.compile(r'并非|不是|不等(?:同|于)|不能(?:仅|据此|由此)?|不构成|不代表')


def _field(row, name, default=''):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def measurement_scope_text_allowed(text, *, path=(), facts=(), assessment_context=()) -> bool:
    """Recognize a typed measurement limitation, not a blanket disclaimer.

    This only exempts a boundary from the *process prose* classifier. It does
    not validate facts, approve a medical conclusion or waive citation review.
    Every boundary clause must describe the actual supplied measurement kind;
    a valid boundary cannot shelter a neighbouring process instruction.
    """
    if not isinstance(text, str) or not text.strip() or _PROCESS.search(text):
        return False
    rows = list(facts.values() if isinstance(facts, dict) else facts)
    measured = [row for row in rows if _field(row, 'kind') in {'function', 'microbe', 'context'}]
    if not measured:
        return False
    names = ' '.join(str(_field(row, 'name')) + ' ' + str(_field(row, 'value')) for row in measured)
    context = ' '.join(str(row.get('resultKind') or '') for row in assessment_context if isinstance(row, dict))
    functional = any(_field(row, 'kind') == 'function' for row in measured)
    genes = bool(re.search(r'基因|序列|geneMarker|tcd[A-Z]|耐药', names + ' ' + context, re.I))
    microbial = any(_field(row, 'kind') == 'microbe' for row in measured)
    boundaries = [clause.strip() for clause in re.split(r'[。！？;；\n]', text)
        if _SCOPE_CUE.search(clause)]
    if not boundaries:
        return False
    for clause in boundaries:
        # Keep the subject explicit in the same sentence. Do not inherit an
        # arbitrary negation's subject from a neighbouring result paragraph.
        if (functional and re.search(r'种类|物种|丰富度|多样性', names)
                and re.search(r'种类|物种|丰富度|多样性', clause)
                and re.search(r'不等(?:同|于)|不代表|并非|不是', clause)
                and re.search(r'菌群总量|总菌量|绝对丰度|微生物负荷', clause)):
            continue
        if functional and re.search(r'评分|功能|模型|指数|菌群相关|这些|本项|该项', clause) and re.search(
                r'(?:浓度|含量|血液|血清|脑内|人体营养状态|膳食摄入|摄入量|暴露量|体内残留|'
                r'实测|测定|临床诊断|疾病诊断|个人患病概率|特定疾病的诊断)', clause):
            if not re.search(r'排除|没有|未患|无须|无需|不用|不需', clause):
                continue
        if genes and re.search(r'基因|序列|标记|检出|检测', clause) and re.search(
                r'不等(?:同|于)|不代表|不是|并非', clause) and re.search(
                r'产毒|表达|临床感染|菌种感染|药敏|药物(?:有|无)效|个人耐药', clause):
            continue
        if microbial and re.search(r'菌群|丰度|检出|检测', clause) and re.search(
                r'不等(?:同|于)|不代表|不构成', clause) and re.search(r'临床感染|临床诊断|疾病诊断', clause):
            continue
        return False
    return True


def clinical_claim_support_origins(text, *, facts=(), comparison_directions=None,
                                   original_advice=(), source_quotes=None):
    """Attribute every uncited clinical clause to a finite source operation.

    A semantic review still owns truth/applicability. This independent check
    prevents its broad ``source_assessment`` label from granting a new medical
    proposition. Unknown substantive words fail closed; there is no disease
    blacklist and no inference from a benign-looking consultation wrapper.
    The returned records are private provenance, never patient-facing prose.
    """
    import unicodedata
    rows = list(facts.values() if isinstance(facts, dict) else facts)
    def compact(value):
        return re.sub(r'[\s，,。；;：:、（）()“”"\[\]]+', '',
            unicodedata.normalize('NFKC', str(value or ''))).casefold()
    numeric_token = re.compile(r'[-+]?\d+(?:\.\d+)?')
    def literal_in_source(token, source):
        return bool(re.search(r'(?<![\d.])' + re.escape(token) + r'(?![\d.])', source)) \
            if numeric_token.fullmatch(token) else token in source
    directions = comparison_directions
    if directions is None:
        from decimal import Decimal
        directions = {}
        number = r'[-+]?\d+(?:\.\d+)?'
        for row in rows:
            fid = _field(row, 'fact_id') or _field(row, 'factId')
            value = re.fullmatch(number, str(_field(row, 'value')).strip())
            reference = _field(row, 'reference_range') or _field(row, 'referenceRange')
            interval = re.fullmatch(r'\s*(' + number + r')\s*[-~～–—]\s*(' + number + r')\s*', str(reference or ''))
            if value and interval:
                observed, low, high = map(Decimal, (value[0], interval[1], interval[2]))
                directions[fid] = 'low' if observed < low else 'high' if observed > high else 'within'
    records, failures = [], []
    source_parts = []
    names = []
    for row in rows:
        fid = _field(row, 'fact_id') or _field(row, 'factId')
        value = str(_field(row, 'value'))
        if source_quotes is not None:
            selected = source_quotes.get(fid, ())
            selected = [selected] if isinstance(selected, str) else list(selected)
            if any(not isinstance(quote, str) or not quote.strip() or quote not in value for quote in selected):
                failures.append('来源命题作用域不是同一fact的逐字原文，不能授予局部性质。')
                continue
            if not selected:
                continue
            value = '；'.join(selected)
        for part in re.split(r'[。；;\n]', value):
            if part.strip(): source_parts.append((compact(part), fid, part.strip()))
        for name in str(_field(row, 'name')).split(' / '):
            if name.strip(): names.append((compact(name), fid, name))
    # Longer literal fragments take precedence. A single shared digit cannot
    # establish a disease property, qualitative severity, or negative finding.
    source_parts.sort(key=lambda item: len(item[0]), reverse=True)
    names.sort(key=lambda item: len(item[0]), reverse=True)
    scaffolding = re.compile(r'原报告|原记录|本次|本项|这一项|该项|这些|上述|目前|当前|您的|您|患者|本人|存在|出现|自述|本报告|'
        r'记录中|检查中|报告中|体检|检查|检验|检测|试验|结果|记录|报告|显示|提示|发现|所见|参考范围|单位|'
        r'分别|其中|同时|另外|另有|另见|以及|并且|而|但|与|和|及|的|为|是|有|见|'
        r'处于|属于|单项|一项|该组|本组|各项|状态|水平|数值|指标|标为|标记为|值|'
        r'需要|值得|优先|重点|关注|保留|观察|本身|仍|可见|需要关注|可|应|宜|需|可以|作为|已|共同|总体|综合')
    navigation = re.compile(r'建议|可|可以|请|您|携带|带上|带齐|结合|本次|这次|当前|既往|'
        r'相关|完整|原始|结果|资料|记录|报告|问题|疑问|困扰|情况|症状|不适|'
        r'向|到|与|由|让|医生|医师|门诊|就医|就诊|咨询|询问|沟通|讨论|解读|'
        r'评估|判断|了解|是否|需要|进一步|复查|检查|在|时|若|如果|如|或|及|和|的|'
        r'仍|持续|加重|影响|日常|生活|活动|工作|后续|后|处理|安排|考虑')
    clauses = [part.strip() for part in re.split(r'[，,。；;！？!?\n]', str(text or '')) if part.strip()]
    for clause in clauses:
        token = compact(clause)
        if not token: continue
        literal = next(((fid, quote) for value, fid, quote in source_parts
            if len(token) >= 2 and literal_in_source(token, value)), None)
        if literal:
            records.append({'text': clause, 'supportOrigin': 'literal_source',
                'factIds': [literal[0]], 'sourceQuotes': [literal[1]]})
            continue
        remaining, used = token, set()
        for value, fid, quote in source_parts:
            if numeric_token.fullmatch(value):
                if literal_in_source(value, remaining):
                    remaining = re.sub(r'(?<![\d.])' + re.escape(value) + r'(?![\d.])', '', remaining)
                    used.add(fid)
            elif len(value) >= 2 and value in remaining:
                remaining = remaining.replace(value, '')
                used.add(fid)
        for name, fid, _ in names:
            # Compare a source test's label independent of its routine suffix.
            alias = name if name in remaining else re.sub(r'(?:检测|检查|试验|检验|测定|指标)$', '', name)
            if len(alias) >= 2 and alias in remaining:
                remaining = remaining.replace(alias, '')
                used.add(fid)
        comparison_used = set()
        for row in rows:
            fid = _field(row, 'fact_id') or _field(row, 'factId')
            if fid not in used and len(rows) != 1: continue
            direction = directions.get(fid)
            words = (('高于参考范围', '超出参考范围', '高于所列范围', '偏高', '升高')
                if direction == 'high' else ('低于参考范围', '低于所列范围', '偏低', '降低')
                if direction == 'low' else ('在参考范围内', '处于参考范围内', '正常')
                if direction == 'within' else ())
            for word in words:
                if compact(word) in remaining:
                    remaining = remaining.replace(compact(word), '')
                    comparison_used.add(fid)
                    used.add(fid)
            if fid in used or len(rows) == 1:
                for field in ('unit', 'reference_range', 'referenceRange'):
                    literal_field = compact(_field(row, field))
                    if literal_field and literal_field in remaining:
                        remaining = remaining.replace(literal_field, '')
                        used.add(fid)
            status = compact(_field(row, 'status'))
            if status and status in remaining and (fid in used or len(rows) == 1):
                remaining = remaining.replace(status, '')
                used.add(fid)
        neutral = scaffolding.sub('', remaining)
        literal_tail = next(((fid, quote) for value, fid, quote in source_parts
            if len(neutral) >= 2 and literal_in_source(neutral, value)), None)
        if literal_tail:
            used.add(literal_tail[0])
            neutral = ''
        if used and not neutral:
            records.append({'text': clause,
                'supportOrigin': 'source_measurement_comparison' if comparison_used else 'source_state_projection',
                'factIds': sorted(used), 'sourceQuotes': [str(_field(row, 'value'))
                    for row in rows if (_field(row, 'fact_id') or _field(row, 'factId')) in used]})
            continue
        # Only an explicit original advice witness may support an original
        # medical action. A generic examination description is never advice.
        advice = next((str(value) for value in original_advice
            if compact(value) == compact(text) and token in compact(value)), None)
        if advice:
            records.append({'text': clause, 'supportOrigin': 'explicit_original_advice',
                'factIds': sorted(used), 'sourceQuotes': [advice]})
            continue
        navigation_remaining = re.sub(r'(?:向|到|赴)[\u4e00-\u9fff]{1,8}科(?:门诊)?', '', remaining)
        if (re.search(r'就医|就诊|咨询|沟通|询问|医生|医师', clause)
                and not navigation.sub('', navigation_remaining)):
            records.append({'text': clause, 'supportOrigin': 'care_navigation',
                'factIds': sorted(used) or [(_field(row, 'fact_id') or _field(row, 'factId')) for row in rows],
                'sourceQuotes': []})
            continue
        records.append({'text': clause, 'supportOrigin': 'new_clinical_proposition',
            'factIds': sorted(used), 'unsupportedRemainder': neutral})
        failures.append('本句新增命题“' + clause + '”不属于原记录性质、已核实数值/原级比较或普通就医咨询；'
            '疾病性质、排除/预后判断、特定检查随访策略和新警示症状须拆成独立句，'
            '用本句真正适用原文及本人条件双审；不能借source_assessment或就医导航免审。')
    return tuple(records), failures


def source_only_clinical_proposition_errors(text, facts, *, original_advice=(), source_quotes=None):
    """Shared necessary gate for narrative and registry source-only claims."""
    return clinical_claim_support_origins(text, facts=facts, original_advice=original_advice, source_quotes=source_quotes)[1]
