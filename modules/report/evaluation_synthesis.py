"""Compact source-bound summaries; measured tables and clinical advice stay intact.

This is an editorial projection, not another scoring or medical inference lane.
The optional projection carries the existing private grading provenance.  A
derived grade with unknown provenance is never promoted to a source label.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
import unicodedata

from modules.report.evaluation_catalog import LOW_FUNCTION_LEVELS
from modules.report.evaluation_meanings import _identity
from modules.report.evaluation_distribution import build_source_distribution, render_source_distribution


_FUNCTION_BACKGROUND_THEMES = {
    'P1': r'糖尿病|血糖|葡萄糖|糖化|肥胖|超重|脂肪肝|脂肪性肝|腹胀|腹痛|腹泻|消化|乳糖|主食|全谷|豆类|膳食|饮食',
    'P2': r'肥胖|超重|脂肪肝|脂肪性肝|胆汁|胆石|胆结石|血脂|胆固醇|甘油三酯|饮食|油脂|外卖|夜宵|肉类|坚果',
    'P3': r'肠|粪便|大便|便秘|腹泻|腹痛|血糖|葡萄糖|糖化|血脂|胆固醇|甘油三酯|脂肪肝|肥胖|体重|BMI|蔬菜|水果|全谷|豆类|饮水|膳食|饮食',
    'P4': r'营养不良|低蛋白|白蛋白|总蛋白|肝功能|转氨酶|肾功能|肌酐|尿素|饮食|豆类|肉类|鱼类|蛋类|乳制品',
    'P5': r'营养不良|贫血|血红蛋白|维生素|叶酸|铁蛋白|血清铁|骨质|骨密度|饮食|蔬菜|水果|全谷|豆类|乳制品',
    'P6': r'睡眠|失眠|焦虑|抑郁|情绪|疼痛|便秘|腹泻|排便',
    'P7': r'尿酸|痛风|嘌呤|肾功能|肌酐',
    'P8': r'抗氧化|氧化应激|氧化胁迫|营养不良',
    'P9': r'炎症|肠炎|肠道感染|感染性腹泻',
    'P10': r'职业暴露|环境暴露|有机溶剂|化学物|农药|杀虫剂|中毒|烟草|吸烟',
}


def _function_personal_context(path, raw, known, reported_background):
    """Select same-domain observations for questions, never as a grade input."""
    path = tuple(path)
    codes = []
    if path[:2] == ('functionalPathways', 'pathways'):
        codes = [raw['functionalPathways']['pathways'][path[2]]['code']]
    elif path[:2] in {('nutritionAssessment', 'detailedMetabolism'),
            ('nutritionAssessment', 'vitaminsAndMinerals')}:
        code = raw[path[0]][path[1]][path[2]]['code']
        codes = [{'carbohydrateMetabolism':'P1', 'lipidMetabolism':'P2',
            'nucleotideMetabolism':'P7', 'harmfulMetabolites':'P9',
            'toxicSubstanceDegradation':'P10', 'vitamins':'P5', 'pigments':'P8'}.get(code)]
    elif path[:2] == ('nutritionAssessment', 'neurotransmitterInterpretation'):
        codes = ['P6']
    elif path == ('nutritionAssessment', 'aminoMetaboliteInterpretation', 'text'):
        from modules.report.evaluation_catalog import AMINO_ACID_ALIASES
        amino_names = {_key(name) for names in AMINO_ACID_ALIASES.values() for name in names}
        # The flat array owns only its actual members. Sharing the amino
        # pathway's planning theme does not import the complete lipid panel,
        # another unmeasured pathway, or a biomedical relationship.
        names = {_key(row.get('name')) for row in raw.get('nutritionAssessment', {}).get(
            'aminoAcidsAndMetabolites', []) if isinstance(row, dict)
            and row.get('value') not in (None, '')}
        if names & amino_names:
            codes.append('P4')
        if names & {_key(name) for name in ('胆汁酸', '初级胆汁酸', '次级胆汁酸')}:
            codes.append('P2')
        if names & {_key(name) for name in ('同型半胱氨酸', '脂多糖', '内毒素')}:
            codes.append('P9')
    elif path == ('nutritionAssessment', 'nutritionInterpretation', 'text'):
        codes = ['P1','P2','P3','P4','P5','P6','P7','P8']
    elif path == ('functionalPathways', 'summary', 'text'):
        codes = list(_FUNCTION_BACKGROUND_THEMES)
    patterns = [_FUNCTION_BACKGROUND_THEMES[code] for code in codes
        if code in _FUNCTION_BACKGROUND_THEMES]
    if not patterns:
        return set(), {}
    theme = re.compile('|'.join(patterns), re.I)

    def field(fact, name):
        return fact.get(name) if isinstance(fact, dict) else getattr(fact, name, None)

    clinical_ids = {fid for fid, fact in known.items()
        if field(fact, 'kind') in {'clinical','self_reported'}
        and theme.search(str(field(fact, 'name') or '') + ' ' + str(field(fact, 'value') or ''))}
    background = {fid: row for fid, row in reported_background.items() if fid in known
        and theme.search(str(row.get('sourceDescription') or field(known[fid], 'value') or ''))}
    return clinical_ids, background


def _shared_resistance_scope_pattern(count):
    numeral = str(count)
    if 2 <= count <= 10:
        numeral = '(?:' + numeral + '|' + '二三四五六七八九十'[count - 2] + ')'
    members = r'(?:全部|各类|' + numeral + r'类)?'
    noun = r'(?:肠道)?(?:抗生素)?耐药基因'
    return (r'(?:本组|本表|整组)(?:的)?' + members + noun + r'|'
        r'(?:本次|本报告|本表)(?:已|所)列(?:出)?(?:的)?' + members + noun + r'|'
        r'(?:本次|本报告)(?:已|所)?提供(?:的)?(?:全部|各类|' + numeral + r'类)' + noun + r'|'
        r'(?:本次|本报告)(?:的)?' + numeral + r'类' + noun)


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _at(raw, *path, default=None):
    for name in path:
        if not isinstance(raw, dict):
            return default
        raw = raw.get(name)
    return raw if raw is not None else default


def nutrition_intro_author_fields(assessment_context, facts):
    """Offer a few real representatives; keep the full inventory for review.

    This writer-only frame selects source observations. It supplies neither a
    new nutrient grade nor approval of a medical relationship or paragraph.
    """
    from types import SimpleNamespace
    from modules.report.evaluation_builder import _source_legend_grade

    native = [row for row in assessment_context if row.get('semanticType') == 'nativeNutritionState']
    directed = {fid for group in native for member in group['members']
        if member.get('direction') for fid in member['factIds']}
    down = {fid for group in native for member in group['members']
        if member.get('direction') == 'down' for fid in member['factIds']}
    processes = {fid:member.get('processType') for group in native
        for member in group['members'] for fid in member['factIds']}
    processes.update({fid:row['metabolismType'][:2] for row in assessment_context
        if row.get('semanticType') == 'nativeFunctionType'
        and row.get('metabolismType') in {'合成代谢','分解代谢'} for fid in row['memberFactIds']})
    groups = []
    for group in assessment_context:
        if group.get('semanticType') != 'nutrientGroup':
            continue
        members, seen = [], set()
        for row in group['members']:
            fact = next((facts[fid] for fid in row['factIds'] if fid in facts
                and facts[fid].kind == 'function' and facts[fid].value not in (None, '')), None)
            if fact is None or fact.fact_id in seen:
                continue
            seen.add(fact.fact_id)
            band = row.get('assessmentLevel') or fact.status or _source_legend_grade(SimpleNamespace(
                kind=fact.kind, source_status=fact.status, value=fact.value, unit=fact.unit,
                reference_range=fact.reference_range))
            members.append({'name':row['name'], 'value':fact.value, 'sourceStatus':fact.status,
                'adoptedLevel':band, 'factIds':[fact.fact_id],
                'processType':processes.get(fact.fact_id),
                'originalDirection':'down' if fact.fact_id in down else 'up' if fact.fact_id in directed else None})
        if not members:
            continue
        harmful_generation = bool(re.search(r'有害|脂多糖|内毒素|毒性物质生成', group['name']))
        weak = [] if harmful_generation else [row for row in members
            if row['adoptedLevel'] in LOW_FUNCTION_LEVELS or row['originalDirection'] == 'down']
        groups.append({'name':group['name'], 'members':members, 'weak':weak,
            'extreme':sum(row['adoptedLevel'] == '极低' for row in weak),
            'interpretationObject':'harmfulGeneration' if harmful_generation else 'metabolicFunction'})
    weak_groups = sorted([group for group in groups if group['weak']], key=lambda group:(
        not bool(group['extreme']), -len(group['weak'])/len(group['members']),
        -group['extreme'], -len(group['weak']), group['name']))
    focus, used = [], set()
    for group in weak_groups:
        ids = {fid for member in group['members'] for fid in member['factIds']}
        if ids & used:
            continue
        representative = min(group['weak'], key=lambda row:(row['adoptedLevel'] != '极低',
            row['originalDirection'] != 'down', row['name']))
        focus.append({'name':group['name'], 'role':'weakPoint', 'representative':representative,
            'memberCount':len(group['members']), 'weakCount':len(group['weak'])})
        used.update(ids)
    retained = [group for group in groups if not group['weak'] and all(
        row['adoptedLevel'] in {'正常','强','合成能力强'}
        and not set(row['factIds']) & directed for row in group['members'])]
    if retained:
        group = max(retained, key=lambda row:(len(row['members']), row['name']))
        focus.append({'name':group['name'], 'role':'retainedFunction',
            'scope':'member_only', 'representative':group['members'][0], 'memberCount':len(group['members'])})
    else:
        # A mixed abnormal group may still contain a genuinely normal member.
        # Keep its scope explicit so it cannot become a whole-group advantage.
        candidates = [(group, row) for group in groups for row in group['members']
            if row['adoptedLevel'] == '正常' and not set(row['factIds']) & directed]
        if candidates:
            group, row = max(candidates, key=lambda pair:(len(pair[0]['members']), pair[1]['name']))
            focus.append({'name':group['name'], 'role':'retainedFunction',
                'scope':'member_only', 'representative':row, 'memberCount':len(group['members'])})
    if not focus:
        return {}
    elevated = [group for group in groups if any(row['originalDirection'] == 'up' for row in group['members'])]
    if elevated:
        group = max(elevated, key=lambda row: sum(member['originalDirection'] == 'up' for member in row['members']))
        member = next(row for row in group['members'] if row['originalDirection'] == 'up')
        focus.append({'name': group['name'], 'role': 'directionalChange', 'scope': 'member_only',
            'representative': member, 'memberCount': len(group['members'])})
    selected = {fid for row in focus for fid in row['representative']['factIds']}
    ids = {fid for group in groups for member in group['members'] for fid in member['factIds']}
    for group in native:
        if selected.intersection(fid for member in group['members'] for fid in member['factIds']):
            ids.update(fid for fid in group['factIds'] if fid in facts)
    background = [row for row in assessment_context if row.get('semanticType') in
        {'sourceReportedBackground','functionPersonalContext'}]
    relations = [row for row in assessment_context
        if row.get('semanticType') == 'approvedPersonalRelation'
        and set(row.get('factIds', ())) & ids]
    for row in background:
        ids.update(fid for fid in row.get('factIds', []) if fid in facts)
    for row in relations:
        ids.update(fid for fid in row.get('factIds', []) if fid in facts)
    frame = {'semanticType':'nutritionIntroFrame', 'focus':focus,
        'actualGroups':[{'name':group['name'], 'members':group['members'],
            'interpretationObject':group['interpretationObject']} for group in groups],
        'factIds':sorted(ids), 'completeSourceInventoryRetainedForReview':True}
    instruction = ('本段是第六章整体营养解读，综合nutritionIntroFrame实际功能组及已有个人关系写三至五句。'
        '先判断整体代谢特点，再解释有依据的健康影响及可行的饮食改善方向；不计数正常异常，不依次搬运成员名册。'
        'focus只帮助选题，不限定仅两个弱项；actualGroups提供完整主题，按健康意义选择重点。'
        '升高或降低只在对应过程真实明确且有解释价值时采用，不为概括两种方向而增加“升降并存”。'
        '能力评分与原始方向分开解释；有害物生成低不归为营养短板，强档也不能自动解释为健康优势。'
        '无需逐项罗列正常结果作对比，不使用“已列低项”“代表方向”“保留功能稳定性”等空泛表达。'
        '个人背景仅可忠实采用本target实际同题事实，不补病史或新的病因关系。'
        'processType未明确的项目只称相关代谢能力，不猜合成或分解；原始升降方向与本次分档分别保留。'
        '纯源内判断的evidenceRefs=[]；健康影响、个人关联与改善建议使用已审关系和实际引用，不凭源内状态补写机制。'
        '首次与修稿均直接返回这段完整新稿，所有句重新来源审核；完整真实明细与原分档保留原表。')
    if relations:
        instruction += ('已有approvedPersonalRelation时，准确采用代表性真实营养成员参与的已审个人关系及对应引用，'
            '解释它对具体症状或风险的意义；不以源内状态免引文重建关系，不用疾病队列介绍或无关临床结果替代。')
    personal_focus = [row for row in assessment_context if row.get('semanticType') == 'approvedPersonalFocus']
    if personal_focus:
        ids.update(fid for row in personal_focus for fid in row.get('factIds', []) if fid in facts)
        instruction += ('可以把实际功能弱项与approvedPersonalFocus中已采用的当前问题共同说明关注顺序，'
            '分别绑定功能和本人状态，不写成病因、互证或功能导致该疾病；不重复全局风险名册。')
    return {'purpose':instruction, 'contentResponsibility':instruction,
        'sourceInterpretationResponsibility':instruction, 'sourceAssessmentAllowed':True,
        'allowedFactIds':sorted(ids), 'assessmentContext':[frame,*background,*relations,*personal_focus],
        'targetVocabulary':{'sourceNames':[row['representative']['name'] for row in focus]},
        'nutritionIntroFrame':frame}


def native_function_source_context(raw, facts):
    """Pair native nutrient summaries with their own measured array members.

    A broad group label never changes a member's score/grade. Its literal
    abnormal-list direction is a separate source observation. Document hash,
    logical panel, source file and report date must all agree; matching names
    in another report cannot supply a direction for this measurement.
    """
    from modules.report.evaluation_meanings import _bound_fact
    from modules.report.evaluation_template_input import NUTRITION_PANELS

    originals = _at(raw, 'appendices', 'sourceItems', default=[])
    declared_types = native_function_type_context(raw, facts)
    locator = re.compile(r'^source:([^:]+):(?:json:|page:\d+:template:)'
        r'([A-Za-z][A-Za-z0-9_]*)' r'(_all_result|\[\d+\](?:\.score)?)$')

    def locations(row, aggregate):
        result = set()
        for source in row.get('sources') or []:
            if source.get('sourceCode') != 'C':
                continue
            match = locator.fullmatch(str(source.get('recordId') or ''))
            if match and match[2] in NUTRITION_PANELS \
                    and (match[3] == '_all_result') == aggregate:
                result.add((match[1], match[2]))
        return result

    result = []
    for summary in originals:
        if summary.get('kind') != 'context' or summary.get('category') != 'structuralAssessment':
            continue
        summary_fact = _bound_fact(summary, facts, 'function')
        aggregate_locations = locations(summary, True)
        if summary_fact is None or len(aggregate_locations) != 1 \
                or summary.get('sourceStatus') != summary_fact.status:
            continue
        details = summary.get('description')
        directions = {}
        for clause in re.split(r'[,，、;；\n]+', str(details or '')):
            if match := re.fullmatch(r'\s*(.+?)\s*([↑↓])\s*', clause):
                directions.setdefault(_key(match[1]), set()).add('up' if match[2] == '↑' else 'down')
        members = []
        for member in originals:
            if member.get('kind') not in {'metric', 'function'} or not locations(member, False) & aggregate_locations:
                continue
            fact = _bound_fact(member, facts, 'function')
            if (fact is None or (fact.source, fact.date) != (summary_fact.source, summary_fact.date)
                    or (member.get('sourceName'), member.get('observedAt')) !=
                        (summary.get('sourceName'), summary.get('observedAt'))):
                continue
            direction = directions.get(_key(fact.name), set())
            members.append({'name': fact.name, 'value': fact.value, 'unit': fact.unit,
                'factIds': [fact.fact_id], 'sources': deepcopy(member.get('sources') or []),
                'direction': next(iter(direction)) if len(direction) == 1 else None,
                'directions': sorted(direction)})
        if members:
            # A source heading may explicitly name one measured process.
            # Mixed headings supply no single type, and an explicit item
            # declaration always takes precedence over its parent heading.
            synthesis, decomposition = ('合成' in summary_fact.name, '分解' in summary_fact.name)
            group_process = '合成' if synthesis and not decomposition else '分解' if decomposition and not synthesis else None
            for member in members:
                declared = [item for item in declared_types
                    if set(item['memberFactIds']).intersection(member['factIds'])]
                member['processType'] = (declared[0]['metabolismType'][:2]
                    if len(declared) == 1 and declared[0]['metabolismType'] in {'合成代谢','分解代谢'}
                    else None if declared else group_process)
                member['processOrigin'] = 'item' if declared else 'sourceGroup' if group_process else None
            direction_facts = [fact for fact in facts.values() if fact.kind == 'context'
                and fact.name == summary_fact.name + '原始方向' and fact.value == details
                and (fact.source, fact.date) == (summary_fact.source, summary_fact.date)]
            sources = deepcopy(summary.get('sources') or [])
            for fact in direction_facts:
                source = {'sourceCode': 'C', 'recordId': fact.fact_id, 'observedAt': fact.date}
                if source not in sources:
                    sources.append(source)
            member_ids = {fid for member in members for fid in member['factIds']}
            labels = [summary_fact.name]
            for section in ('detailedMetabolism', 'vitaminsAndMinerals'):
                for row in _at(raw, 'nutritionAssessment', section, default=[]):
                    row_ids = {fact.fact_id for item in row.get('items', [])
                        if (fact := _bound_fact(item, facts, 'function')) is not None}
                    if row_ids and row_ids <= member_ids and row.get('name'):
                        labels.append(row['name'])
            for row in _at(raw, 'functionalPathways', 'pathways', default=[]):
                row_ids = {fact.fact_id for item in row.get('relatedMetrics', [])
                    if (fact := _bound_fact(item, facts, 'function')) is not None}
                if row_ids and row_ids <= member_ids and row.get('name'):
                    labels.append(row['name'])
            result.append({'panel': next(iter(aggregate_locations))[1], 'name': summary_fact.name,
                'aliases': list(dict.fromkeys(labels)),
                'sourceStatus': summary_fact.status, 'details': details,
                'processTypes': [label for label, present in (
                    ('合成', synthesis), ('分解', decomposition)) if present],
                'mixedSourceClass': bool(synthesis and decomposition),
                'processType': group_process if group_process and all(
                    member['processType'] == group_process for member in members) else None,
                'factIds': [summary_fact.fact_id, *(fact.fact_id for fact in direction_facts)],
                'aggregateFactIds': [summary_fact.fact_id],
                'directionFactIds': [fact.fact_id for fact in direction_facts], 'sources': sources,
                'members': members})
    return result


def native_function_type_context(raw, facts):
    """Bind a declared process type to the same source's exact score cell."""
    from modules.report.evaluation_meanings import _bound_fact
    from modules.report.evaluation_template_input import NUTRITION_PANELS

    locator = re.compile(r'^source:([^:]+):(?:json:|page:\d+:template:)'
        r'([A-Za-z][A-Za-z0-9_]*)\[(\d+)\]\.(score|metabolism_type)$')
    originals = _at(raw, 'appendices', 'sourceItems', default=[])

    def positions(row, suffix):
        return {(match[1],match[2],match[3]) for source in row.get('sources') or []
            if source.get('sourceCode') == 'C'
            and (match := locator.fullmatch(str(source.get('recordId') or '')))
            and match[2] in NUTRITION_PANELS and match[4] == suffix}

    result = []
    for declaration in originals:
        if declaration.get('kind') != 'context' or declaration.get('category') != 'structuralAssessment':
            continue
        type_positions = positions(declaration, 'metabolism_type')
        declared = _bound_fact(declaration, facts, 'function')
        if len(type_positions) != 1 or declared is None:
            continue
        for measured in originals:
            if measured.get('kind') not in {'metric','function'} \
                    or not positions(measured, 'score') & type_positions:
                continue
            fact = _bound_fact(measured, facts, 'function')
            if (fact is None or declared.name != fact.name + '代谢类型'
                    or (fact.source,fact.date) != (declared.source,declared.date)
                    or (measured.get('sourceName'),measured.get('observedAt')) !=
                        (declaration.get('sourceName'),declaration.get('observedAt'))):
                continue
            result.append({'name':fact.name,'metabolismType':declared.value,
                'memberFactIds':[fact.fact_id], 'typeFactIds':[declared.fact_id],
                'factIds':[fact.fact_id,declared.fact_id],
                'sources':deepcopy(declaration.get('sources') or [])})
    return result


def native_function_type_failures(text, assessment_context, facts):
    """A local measured-score label cannot reverse its literal process type.

    The source type limits what that score measured; it does not disprove an
    independently cited study of the same substance's other biology.
    """
    ids = {fact.fact_id for fact in facts}
    normalize = lambda value: re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',value or '')).casefold()
    processes = r'合成|分解|利用|吸收|摄取'
    rating = (r'(?:均|都)?(?:仍然|仍|依然)?(?:被)?'
        r'(?:评价为|评定为|评为|判定为|判为|标记为|标为|处于|表现为?|为|呈|保持|有)?'
        r'(?:较弱|偏弱|偏低|弱|不足|正常|较强|偏强|增强|强|保留|充足|良好)')
    predicate = (r'评分|得分|分值|分档|评级|评估|估计|'
        r'(?:正常|较弱|偏弱|弱|较强|强)(?:的)?(?:项目|条目|项)|'
        r'(?:功能|能力|表现)(?='+rating+r')')
    # A measurement phrase can say “合成相关评分” or “合成方面的分档”.
    # Locate the process root and its rating noun in the same short phrase,
    # without crossing punctuation or a separately named process.
    claims = re.compile(r'('+processes+r')(?:代谢)?'
        r'(?:(?!(?:'+processes+r')|[，,。！？；;\n、]).){0,24}?(?:'+predicate+r')')
    subjects, mixed_groups = [], []
    for context in assessment_context:
        if context.get('semanticType') == 'nativeFunctionType' and context.get('metabolismType') in {'合成代谢','分解代谢'}:
            subjects.append((context['name'],[context['name']],context['metabolismType'][:2],context.get('memberFactIds') or []))
        elif context.get('semanticType') == 'nativeNutritionState':
            for member in context.get('members') or []:
                if member.get('processType') in {'合成','分解'}:
                    subjects.append((member['name'],[member['name']],member['processType'],member['factIds']))
            if context.get('processType') in {'合成','分解'}:
                base = re.split(r'合成|分解',context['name'],maxsplit=1)[0]
                aliases = [part for part in re.split(r'[（）()]',base)if part]
                subjects.append((base,aliases,context['processType'],
                    [fid for member in context['members']for fid in member['factIds']]))
            if (context.get('mixedSourceClass') is True
                    and set(context.get('processTypes') or []) == {'合成','分解'}
                    and any(fact.fact_id in set(context.get('aggregateFactIds') or [])
                        and fact.kind == 'context' and fact.name == context.get('name')
                        and '合成' in fact.name and '分解' in fact.name for fact in facts)):
                base = re.split(r'合成|分解',context['name'],maxsplit=1)[0]
                mixed_groups.append((base,[part for part in re.split(r'[（）()]',base)if part]))
    def check_process(name, actual, stated):
        if stated != actual:
            failures.append(name+'的原始测量类型为'+actual+'代谢，不能将该项结果改称'
                +stated+'能力；混合类型摘要使用代谢评分。')
    def following_list(clause, claim):
        following = clause[claim.end():]
        if (not re.search(processes,following)
                and re.match(r'(?:的)?(?:'+rating
                    +r'(?:档|水平)?(?:的)?(?:项目|条目|项)?(?:的)?)?'
                    r'(?:包括|包含|有|为|是|如下|[:：])',following)):
            return normalize(following)
        return None
    failures = []
    # Keep a study's scope across commas, while a patient-specific rating in
    # the same sentence still has to respect its actual source process.
    clauses = [clause for sentence in re.split(r'[。！？\n]', text or '')
        if not (re.search(r'研究|文献|试验|体外|培养|队列', sentence)
            and not re.search(r'本次|本组|本报告|原报告|来源|检测', sentence))
        for clause in re.split(r'[，,；;]', sentence)]
    for clause in clauses:
        if re.search(r'研究|文献|试验|体外|培养|队列', clause) and not re.search(
                r'本次|本组|本报告|原报告|来源|检测', clause):
            continue
        start = 0
        for claim in claims.finditer(clause):
            subject = clause[start:claim.start()]
            start = claim.end()
            if re.search(r'(?:不是|并非|不属于|不应称为|不能称为|而非)[^，,。；;]{0,4}$',subject):
                continue
            for name,aliases,actual,member_ids in subjects:
                if not ids.intersection(member_ids):
                    continue
                for alias in aliases:
                    for mention in re.finditer(re.escape(normalize(alias)), normalize(subject)):
                        # A coordinated list can share the final predicate,
                        # but an earlier item's own process stays with it:
                        # “A分解能力和B合成能力均较弱” is not A合成能力.
                        local = re.match(r'(?:的|相关)?('+processes+r')',
                            normalize(subject)[mention.end():])
                        check_process(name,actual,local[1] if local else claim[1])
            # The same rating can precede its list: “合成相关的正常项
            # 包括A、B”. An item's explicitly stated own process takes
            # precedence over that shared heading.
            compact = following_list(clause,claim)
            if compact:
                for name,aliases,actual,member_ids in subjects:
                    if not ids.intersection(member_ids):
                        continue
                    for alias in aliases:
                        if normalize(alias) in compact:
                            check_process(name,actual,claim[1])
        # A mixed source group is a different constraint from a single item's
        # type. “脂质、氨基酸等四类合成代谢” silently narrows the first
        # group even though no individual lipid name appears in the sentence.
        if mixed_groups and re.search(
                r'本次|本组|本报告|原报告|来源|检测|评估|估计|评分|短板|弱项|正常|异常|较弱|偏弱|保留',clause):
            group_claims = [claim for claim in claims.finditer(clause)if claim[1]in {'合成','分解'}]
            group_claims.extend(re.finditer(r'(合成|分解)代谢',clause))
            for claim in group_claims:
                if (re.match(r'(?:的)?(?:项目|条目|分项|部分)',clause[claim.end():])
                        or re.match(r'(?:代谢)?(?:的)?(?:项目|条目|分项|部分)',
                            clause[claim.start()+len(claim[1]):])):
                    continue
                subject = normalize(clause[:claim.start()])
                for name,aliases in mixed_groups:
                    for alias in aliases:
                        for mention in re.finditer(re.escape(normalize(alias)),subject):
                            tail = subject[mention.end():]
                            # An independently named neutral group has its
                            # own scope, not a later neighbour's predicate.
                            if re.match(r'(?:的|相关)?(?:(?:与|及)[^、和与及合成分解]{1,8})?代谢',tail):
                                continue
                            local = re.match(r'(?:的|相关)?(合成|分解)(?:代谢)?'
                                r'(?:(?:以及|和|与|及|或)?(合成|分解))?',
                                tail+normalize(claim[0]))
                            if local:
                                stated = {value for value in local.groups() if value}
                            else:
                                shared = re.search(r'(合成|分解)(?:代谢)?(?:以及|和|与|及|或)$',tail)
                                stated = {claim[1], *([shared[1]] if shared else [])}
                            if stated != {'合成','分解'}:
                                failures.append(name+'的原始组别包含合成和分解代谢，不能将整组归为单一'
                                    +claim[1]+'代谢；混合组使用代谢表述或分别保留原过程。')
                compact = following_list(clause,claim)
                if compact:
                    for name,aliases in mixed_groups:
                        if any(normalize(alias)in compact for alias in aliases):
                            failures.append(name+'的原始组别包含合成和分解代谢，不能将整组归为单一'
                                +claim[1]+'代谢；混合组使用代谢表述或分别保留原过程。')
        # Shared predicates such as “氨基酸利用和糖类分解能力仍有保留”
        # still assign each named object its own process, even when only the
        # last object carries “能力”. No general research mechanism is denied.
        if not re.search(r'评分|得分|分值|档|正常|较弱|偏弱|偏低|不足|充足|保留|较强|增强',clause):
            continue
        compact = normalize(clause)
        for name,aliases,actual,member_ids in subjects:
            if not ids.intersection(member_ids):
                continue
            for alias in aliases:
                for match in re.finditer(re.escape(normalize(alias))+r'(?:的|相关)?('+processes+r')',compact):
                    check_process(name,actual,match[1])
    return list(dict.fromkeys(failures))


@dataclass
class _Paragraph:
    sentences: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def add(self, sentence, rows):
        if sentence and rows:
            self.sentences.append(sentence)
            self.rows.extend(rows)
        return self


class _Inventory:
    def __init__(self, raw, facts, projection, summary_results, bound_fact, *, reviewed_blocks=None):
        self.raw, self.facts = raw, facts
        self.projection = projection or {}
        self.summary_results, self.bound_fact = summary_results, bound_fact
        self.native_function_context = native_function_source_context(raw, facts)
        self.reviewed_blocks = reviewed_blocks or {}
        self._owner_paths = {}

        def index(value, path=()):
            if isinstance(value, dict):
                self._owner_paths[id(value)] = path
                for key, child in value.items():
                    index(child, (*path, key))
            elif isinstance(value, list):
                for position, child in enumerate(value):
                    index(child, (*path, position))
        index(raw)

    def reviewed_result(self, owner, text_key='text'):
        """Only exact, live sentence reviews can complete an interpretation.

        A public reference number, a source description or a clinical source
        attached to the paragraph is not evidence of its content role.
        """
        if not isinstance(owner, dict):
            return False
        path = (*self._owner_paths.get(id(owner), ()), text_key)
        proof = self.reviewed_blocks.get(path)
        if not isinstance(proof, tuple) or len(proof) != 2:
            return False
        block, target = proof
        if target.path != path or block.text != owner.get(text_key):
            return False
        from modules.report.evaluation_narrative import reviewed_result_lines
        return bool(reviewed_result_lines(block, target, self.facts))

    def rows(self, *path):
        rows = _at(self.raw, *path, default=[])
        projected = _at(self.projection, *path, default=[])
        return self.bound_rows(rows, projected)

    def bound_rows(self, rows, projected):
        if not isinstance(rows, list):
            return []
        actual = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = deepcopy(row)
            matches = [other for other in projected if isinstance(other, dict)
                and all(other.get(key) == row.get(key) for key in
                    ('code', 'name', 'value', 'unit', 'referenceRange', 'sourceStatus', 'assessmentLevel'))
                and {s.get('recordId') for s in other.get('sources', [])}
                    & {s.get('recordId') for s in row.get('sources', [])}]
            origins = {other.get('_assessmentOrigin') for other in matches}
            if len(origins) == 1:
                item['_assessmentOrigin'] = origins.pop()
            actual.append(item)
        return actual

    def functions(self, rows, *, favorable=False):
        verified = []
        for row in rows:
            if row.get('code') == 'otherNormalClasses':
                # This display aggregate has no single measurement. Restore
                # only its typed, source-bound AMR members for summary use.
                ids = {source.get('recordId') for source in row.get('sources') or []
                    if source.get('sourceCode') in {None, 'C'}}
                for original in _at(self.raw, 'appendices', 'sourceItems', default=[]):
                    if original.get('category') != 'antimicrobialResistance':
                        continue
                    member = {**original, 'sources': [source for source in original.get('sources') or []
                        if source.get('recordId') in ids and source.get('sourceCode') in {None, 'C'}]}
                    fact = self.bound_fact(member, self.facts, 'function')
                    if fact is None or member.get('sourceStatus') != fact.status \
                            or fact.status not in {'正常', '未检出', '阴性'}:
                        continue
                    member.update(assessmentLevel=fact.status, _assessmentOrigin='source')
                    verified.append(member)
                continue
            fact = self.bound_fact(row, self.facts, 'function')
            if fact is None or (row.get('sourceStatus') is not None
                    and row['sourceStatus'] != fact.status):
                continue
            if row.get('_assessmentOrigin') == 'reference' and (
                    not row.get('referenceRange') or
                    _key(row['referenceRange']) != _key(fact.reference_range)):
                continue
            verified.append(row)
        actual = self.summary_results(verified, self.facts, favorable=favorable)
        for row in actual:
            row['_summaryMeasurementFactIds'] = set(row['_summaryFactIds'])
            row['_summaryMeasurementFacts'] = [self.facts[fid] for fid in row['_summaryFactIds']
                if fid in self.facts]
            categories = {original.get('category')
                for original in _at(self.raw, 'appendices', 'sourceItems', default=[])
                if any(source.get('recordId') in row['_summaryMeasurementFactIds']
                    for source in original.get('sources') or [])}
            row['_distributionObjectType'] = ('耐药基因检测' if 'antimicrobialResistance' in categories
                else '原报告基因标记条目' if 'virulence' in categories else '菌群功能测量')
            contexts = []
            for context in self.native_function_context:
                members = [member for member in context['members']
                    if set(member['factIds']) & row['_summaryFactIds']]
                if not members:
                    continue
                contexts.append({**context, 'members': members})
                row['_summaryNativeGroupAbnormal'] = row.get('_summaryNativeGroupAbnormal', False) \
                    or context['sourceStatus'] == '异常'
                row['_summaryNativeDeviation'] = row.get('_summaryNativeDeviation', False) \
                    or bool(context.get('directionFactIds') and any(
                        member.get('directions') or member['direction'] for member in members))
                row['_summaryFactIds'].update(context['factIds'])
                for source in context['sources']:
                    if source not in row['sources']:
                        row['sources'].append(deepcopy(source))
            row['_summaryNativeContexts'] = contexts
        return [row for row in actual if not favorable or not row.get('_summaryNativeDeviation')]

    def medical_rows(self, collection='findings'):
        """Read original adopted facts behind a grouped medical display row.

        Group labels and concatenated values are presentation. Resolve their
        explicit A references to immutable observations before making a compact
        summary; never bind a group title to a different or inferred result.
        """
        result = []
        for row in _at(self.raw, 'medicalAssessment', collection, default=[]):
            if not str(row.get('code') or '').startswith('medicalRisk:'):
                result.append(row)
                continue
            adopted = {}
            for source in row.get('sources') or []:
                if source.get('sourceCode') not in {None, 'A'}:
                    continue
                fact = self.facts.get(source.get('recordId'))
                if fact is None or fact.kind != 'clinical':
                    continue
                if source.get('observedAt') is not None and fact.date is not None \
                        and source['observedAt'] != fact.date:
                    continue
                adopted.setdefault(fact.fact_id, (fact, []))[1].append(source)
            for fact, sources in adopted.values():
                grade = '异常' if fact.abnormal else ('正常' if fact.status == '正常'
                    else row.get('assessmentLevel'))
                result.append({**row, 'code': fact.fact_id, 'name': fact.name,
                    'value': fact.value, 'unit': fact.unit, 'referenceRange': fact.reference_range,
                    'sourceStatus': fact.status, 'assessmentLevel': grade, 'sources': deepcopy(sources)})
        return result

    def sources(self, rows):
        result = []
        for row in rows:
            ids = row.get('_summaryFactIds', row.get('factIds', ()))
            for source in row.get('sources', []):
                fact = self.facts.get(source.get('recordId'))
                if fact is None or ids and fact.fact_id not in ids:
                    continue
                if source.get('observedAt') is not None and fact.date is not None \
                        and source['observedAt'] != fact.date:
                    continue
                if source not in result:
                    result.append(deepcopy(source))
        return result


def _put(owner, paragraph, inventory, *, discard_inapplicable=False):
    """Keep a role-complete reviewed block, or replace text and refs together."""
    if not isinstance(owner, dict) or not paragraph.sentences:
        return
    sources = inventory.sources(paragraph.rows)
    if not sources:
        return
    text = '\n'.join(dict.fromkeys(paragraph.sentences))
    if inventory.reviewed_result(owner) and not discard_inapplicable:
        # Keep the reviewed paragraph and its exact reference/source binding.
        # Management alone never completes a result-interpretation slot.
        return
    owner['evidenceRefs'] = []
    owner.update(text=text, sources=sources,
        availability='partial')


def _normal(row):
    return not row.get('_summaryNativeDeviation') \
        and row.get('_summaryLabel') in {'正常', '强', '合成能力强', '平衡', '健康'}


def _low(row):
    return row.get('_summaryLabel') in LOW_FUNCTION_LEVELS


def _structure_deviation(row):
    """Recognize supplied structural states without recalculating a grade."""
    label = row.get('_summaryLabel')
    return isinstance(label, str) and bool(re.fullmatch(
        r'偏低|较低|低|降低|缺乏|偏高|较高|高|升高|失衡|异常|不健康倾向|亚健康|'
        r'(?:[ⅠⅡⅢⅣIViv一二三四1-4]+度)?失调|很弱|较弱|弱|不足', label.strip()))


def _example(row):
    fact = row['_summaryFact']
    value = fact.value + ((' ' + fact.unit) if fact.unit else '')
    label = row['_summaryLabel']
    origin = row['_summaryOrigin']
    suffix = ('原分级' if origin == 'source' else
        '区间分级' if origin == 'reference' else '评估分级')
    return row['name'] + ' ' + value + (('（' + suffix + '：' + str(label) + '）') if label else '')


def _structure_states(rows):
    """Describe the actual named dimensions with their own source grades."""
    return '；'.join(_example(row) for row in rows)


def _function_domains(inv):
    """Use the existing main-domain projection, never a new pathway score."""
    from modules.report.evaluation_narrative import _pathway_main_result_groups

    result = []
    paths = inv.rows('functionalPathways', 'pathways')
    for pathway in paths:
        groups = _pathway_main_result_groups(pathway, inv.raw, inv.facts)
        # Copy origins from the corresponding projected child metrics.
        projected_path = next((p for p in _at(inv.projection, 'functionalPathways', 'pathways', default=[])
            if p.get('code') == pathway.get('code')), {})
        candidates = projected_path.get('relatedMetrics', [])
        rows = []
        for group in groups:
            for row in group:
                item = deepcopy(row)
                matches = [other for other in candidates if all(other.get(key) == row.get(key)
                    for key in ('code', 'name', 'value', 'unit', 'referenceRange', 'sourceStatus', 'assessmentLevel'))]
                origins = {other.get('_assessmentOrigin') for other in matches}
                if len(origins) == 1:
                    item['_assessmentOrigin'] = origins.pop()
                rows.append(item)
        actual = inv.functions(rows)
        if actual:
            result.append({'code': pathway['code'], 'name': pathway['name'], 'rows': actual})
    return result


def _risk_observations(inv):
    """Keep actual extra model risks without adding a fixed disease slot."""
    candidates = [row for category in _at(inv.raw, 'diseaseRiskAssessment', 'categories', default=[])
        for row in category.get('items', [])]
    candidates.extend(_at(inv.raw, 'diseaseRiskAssessment', 'riskRanking', default=[]))
    rows, seen = [], set()
    for row in candidates:
        if row.get('value') in (None, ''):
            continue
        actual = deepcopy(row)
        fact = inv.bound_fact(actual, inv.facts, 'function')
        if fact is None:
            original = inv.facts.get(row.get('code'))
            original_name = re.sub(r'[（(]来源疾病模型指数[）)]$', '', original.name).strip() if original else None
            if (original is not None and original.kind == 'function'
                    and original_name == row.get('name') and original.value == str(row.get('value'))
                    and original.unit == row.get('unit') and original.status == row.get('sourceStatus')):
                actual.setdefault('sources', []).append({'sourceCode': 'C',
                    'recordId': original.fact_id, 'observedAt': original.date})
                fact = inv.bound_fact(actual, inv.facts, 'function')
        if fact is None:
            continue
        identity = (fact.fact_id, _key(row.get('name')), str(row.get('value')), row.get('unit'))
        if identity not in seen:
            seen.add(identity)
            rows.append(actual)
    return inv.functions(rows)


def _domain_groups(domains):
    low = [domain for domain in domains if domain['code'] not in {'P9', 'P10'}
        and any(_low(row) for row in domain['rows'])]
    # A proportion of low rows is not a clinical priority or a severity score.
    # Use the same editorial sequence in overview/conclusion fallbacks. Only
    # actually bound low rows admit a domain; missing/normal themes stay out.
    order = {code:index for index,code in enumerate(('P5','P4','P2','P6','P7','P8','P1','P3'))}
    low.sort(key=lambda domain: order.get(domain['code'], len(order)))
    normal = [domain for domain in domains if domain['code'] not in {'P9', 'P10'} and domain['rows']
        and all(_normal(row) and not row.get('_summaryNativeGroupAbnormal') for row in domain['rows'])]
    return low, normal


def _grades(rows):
    return '、'.join(dict.fromkeys(row['_summaryLabel'] for row in rows if row['_summaryLabel']))


def _functional_picture(domains):
    paragraph = _Paragraph()
    low, normal = _domain_groups(domains)
    if low:
        # This counts supplied observations, not a group-level deficiency.
        details = ['%s的%d/%d项' % (domain['name'], sum(_low(row) for row in domain['rows']),
            len(domain['rows'])) for domain in low[:3]]
        paragraph.add('偏低或弱项见于' + '、'.join(details) + '。',
            [row for group in low[:3] for row in group['rows']])
    if normal:
        rows = [row for group in normal[:3] for row in group['rows']]
        paragraph.add('与此同时，' + '、'.join(group['name'] for group in normal[:3])
            + '的已提供项目分级为' + _grades(rows) + '，与弱项较集中的功能域形成差异。', rows)
    return paragraph


def _states(rows):
    states = {}
    for row in rows:
        key = row['_summaryOrigin'], row['_summaryLabel']
        states[key] = states.get(key, 0) + 1
    return '、'.join(str(count) + '项' + (
        '未提供分级' if label is None else
        '分级为' + str(label)) for (origin, label), count in states.items())


def _structure(inv):
    rows = inv.functions(inv.rows('microbiomeAssessment', 'overallStructure'))
    groups = {}
    for row in rows:
        groups.setdefault(row.get('code') or _identity(row.get('name')) or ('literal', _key(row.get('name'))), []).append(row)
    chosen, conflicts = {}, []
    for code, members in groups.items():
        identities = {(r['_summaryFact'].source, r['_summaryFact'].date or r['_summaryFact'].fact_id,
            r['_summaryFact'].value, r['_summaryFact'].unit, r['_summaryLabel']) for r in members}
        if len(identities) == 1:
            chosen[code] = members[0]
        else:
            conflicts.extend(members)
    paragraph = _Paragraph()
    gmwi = chosen.get('gmwi2')
    normal = [chosen[c] for c in ('speciesCount', 'diversity') if c in chosen and _normal(chosen[c])]
    deviations = [row for code, row in chosen.items() if code != 'gmwi2' and _structure_deviation(row)]
    weak_structure = [chosen[c] for c in ('speciesCount', 'diversity')
        if c in chosen and chosen[c].get('_summaryLabel') in
            LOW_FUNCTION_LEVELS | {'较低', '低', '降低', '不足'}]
    unhealthy = gmwi is not None and gmwi['_summaryLabel'] not in {
        None, '正常', '健康', '健康倾向'}
    dimensions = [chosen[c] for c in ('speciesCount', 'diversity') if c in chosen
        and chosen[c].get('_summaryLabel') is not None]
    # This finite fallback describes only the source configuration. An actual
    # health effect belongs to the independently reviewed narrative, never to
    # a locally inferred infection/immune/causal mechanism.
    selected = ([gmwi] if unhealthy else []) + dimensions
    if selected:
        paragraph.add(_structure_states(selected) + '。', selected)
    other_deviations = [row for row in deviations if row not in dimensions]
    if other_deviations:
        paragraph.add(_structure_states(other_deviations[:2]) + '。', other_deviations[:2])
    be = chosen.get('beRatio')
    fb = chosen.get('fbRatio')
    fb_deviation = fb is not None and (bool(re.search(r'偏向|偏高|偏低|异常|失衡',
        str(fb.get('_summaryLabel') or ''))) or _structure_deviation(fb))
    if be is not None and _normal(be) and fb_deviation:
        paragraph.add('定植抗力正常，菌门组成比例存在偏移。', [be, fb])
    elif fb_deviation:
        paragraph.add('菌门组成比例' + str(fb.get('_summaryLabel') or '存在偏移') + '。', [fb])
    elif be is not None and _normal(be) and (weak_structure or unhealthy or deviations):
        paragraph.add('定植抗力正常。', [be])
    if not paragraph.sentences:
        remaining = [row for row in chosen.values() if row['_summaryLabel'] is not None][:3]
        if remaining:
            paragraph.add('结构关注集中在' + '、'.join(r['name'] + '（' + r['_summaryLabel'] + '）' for r in remaining)
                + '所反映的具体维度，后续观察应围绕这些维度的变化。', remaining)
    if conflicts:
        paragraph.add('同类指标的不同记录保留各自的结果与分级，整体判断以能够确认的结构特征为依据。', conflicts)
    return paragraph


def representative_microbial_rows(original_rows, facts, bound_fact):
    """Share a bounded, nonconflicting patient signal set with the author."""
    rows = []
    for row in original_rows:
        fact = bound_fact(row, facts, 'taxa')
        if fact is not None and row.get('sourceStatus') and row.get('sourceStatus') == fact.status:
            rows.append({**row, 'factIds': [fact.fact_id], '_fact': fact})
    if not rows:
        return []
    groups = {}
    for row in rows:
        groups.setdefault(_key(row.get('scientificName') or row.get('name')), []).append(row)
    selected = []
    # Resolve the representative set internally. Ambiguous directions remain
    # intact in the measured rows and consistency notes, not as a headline
    # replacing the patient's actionable micro-ecological picture.
    directions = {'偏高':'high', '超标':'high', '偏低':'low', '缺乏':'low',
        '正常':'normal', '未见异常':'normal', '异常':'abnormal'}
    conflicted = set()
    for identity, members in groups.items():
        by_day = {}
        for row in members:
            date = row['_fact'].date
            day = date[:10] if date and re.match(r'\d{4}-\d{2}-\d{2}', date) else date
            by_day.setdefault(day or row['_fact'].fact_id, []).append(row)
        if any(len({directions[r['sourceStatus']] for r in group
                if r['sourceStatus'] in directions}) > 1 for group in by_day.values()):
            conflicted.add(identity)
    candidates = [row for row in rows if _key(row.get('scientificName') or row['name']) not in conflicted]
    candidates.sort(key=lambda row: {'beneficial': 0, 'pathogenic': 1, 'opportunistic': 2}.get(row.get('category'), 3))
    for status in ('偏低', '缺乏', '偏高', '超标', '异常'):
        row = next((r for r in candidates if r['sourceStatus'] == status
            and not any(_key(r.get('scientificName') or r['name']) ==
                _key(old[0].get('scientificName') or old[0]['name']) for old, _ in selected)), None)
        if row and len(selected) < 3:
            category = {'beneficial': '有益菌群中的', 'pathogenic': '病原相关菌群中的',
                'opportunistic': '条件致病菌群中的'}.get(row.get('category'), '')
            selected.append(([row], category + row['name'] + '（' + status + '）'))
    return [row for group, _ in selected for row in group]


def _microbial_signals(inv):
    from modules.report.evaluation_narrative import _representative_signal_observations, _signal_display_name
    used = _representative_signal_observations(inv.raw, inv.facts)
    if not used:
        return _Paragraph()
    taxa = [row for row in used if not row.get('_signalSemanticType')]
    descriptions = [({'beneficial': '有益菌群中的', 'pathogenic': '病原相关菌群中的',
        'opportunistic': '条件致病菌群中的'}.get(row.get('category'), '')
        + row['name'] + '（' + row['sourceStatus'] + '）') for row in taxa]
    panel = [row for row in used if row.get('_signalSemanticType')]
    text = ('异常菌群信号包括' + '；'.join(descriptions) + '。') if descriptions else ''
    for genus in taxa:
        genus_name = genus.get('scientificName') or ''
        if len(genus_name.split()) != 1 or genus.get('sourceStatus') not in {'偏低', '缺乏'}:
            continue
        species = next((row for row in taxa if (row.get('scientificName') or '').startswith(genus_name + ' ')
            and len((row.get('scientificName') or '').split()) >= 2
            and row.get('sourceStatus') in {'偏高', '超标'}), None)
        if species:
            genus_name = genus['name'] if genus['name'].endswith('属') else genus['name'] + '属'
            species_name = species['name'] if species['name'].endswith('种') else species['name'] + '种'
            text += (genus_name + '与其中的' + species_name + '属于不同分类层级，'
                '该属偏低与该菌种偏高并存，关注重点是属内构成的分布差异。')
    if panel:
        signals = []
        for row in panel:
            name = _signal_display_name(row)
            if row['_signalSemanticType'] == 'healthAssociation':
                name = (re.sub(r'相关菌群(?:评估|信号)?$', '相关菌群信号', name)
                    if re.search(r'相关菌群(?:评估|信号)?$', name) else name + '相关菌群信号')
            else:
                name += '的检出信号'
            signals.append(name)
        text += '另见' + '、'.join(signals) + '。'
    return _Paragraph().add(text, used)


def _clinical_kind(row, fact):
    """A numeric physical measurement is not a laboratory examination."""
    from .evaluation_builder import _is_imaging_examination
    if _is_imaging_examination(fact.name, row.get('system')):
        return 'imaging'
    if re.search(r'身高|体重|腰围|臀围|胸围|体质指数|BMI|血压|脉搏|心率|体温|'
            r'查体|体格|鼻部|鼻腔|鼻黏膜|鼻粘膜|(?:粪便|大便)(?:性状|形态)', fact.name, re.I):
        return 'examination'
    if re.search(r'检验|化验|laboratory', row.get('system') or '', re.I) or re.search(
            r'细胞|血红蛋白|血小板|胆固醇|甘油三酯|脂蛋白|血糖|糖化|肌酐|尿酸|'
            r'转移酶|转氨酶|胆红素|白蛋白|隐血|潜血|尿蛋白|肿瘤标志物', fact.name):
        return 'laboratory'
    return 'examination'


def _clinical_deviation(row, fact):
    if fact.abnormal or row.get('assessmentLevel') == '异常':
        return True
    # A supplied stool-consistency grade is qualitative, not a missing
    # numerical threshold or a new diagnosis inferred from a B complaint.
    return bool(re.fullmatch(r'(?:粪便|大便)(?:性状|形态)', fact.name)
        and row.get('sourceStatus') == fact.status == '偏硬'
        and re.search(r'偏硬|硬块|硬便|干硬', fact.value))


_EXAMINATION_QUALIFICATION = re.compile(
    r'疑似|可能|考虑|倾向|待排|待定|未定|未明确|不能排除|不排除|性质不明|待进一步')


def _examination_label(name, value, status):
    """A compact summary does not turn an uncertain clause into a diagnosis.

    Unqualified source wording is kept byte-for-byte. The full result and
    original advice remain on their source row in either branch.
    """
    value = str(value or '')
    if not _EXAMINATION_QUALIFICATION.search(value):
        return name + ('所见“' + value + '”' if re.search(r'[\u4e00-\u9fff]', value)
            else '（' + status + '）' if status else '检查异常')
    confirmed = [part.strip() for part in re.split(r'[，,。；;\n]', value)
        if len(re.findall(r'[\u4e00-\u9fff]', part.strip())) >= 2
        and not _EXAMINATION_QUALIFICATION.search(part)
        and not re.search(r'尚需|需进一步|未见|未发现|无明显|未提示|否认|未检出|建议|复查|随访', part)]
    if confirmed:
        return name + '所见“' + '、'.join(dict.fromkeys(confirmed)) + '”'
    return name + ('（' + status + '）' if status else '检查异常')


def _clinical(inv, focus):
    """Resolve a comprehensive focus to each adopted clinical observation."""
    rows, labels, seen = [], [], set()
    priorities = [row for row in _at(inv.raw, 'healthOverview', 'keyFindings', default=[])
        if row.get('module') == '医学评估' and row.get('summary')]
    priorities = sorted(priorities, key=lambda row: row.get('rank') or 10**6)
    # A source may contain both an imaging lesion and a stone. Keep both
    # separately scoped judgments before the laboratory findings; routine
    # history does not displace an actual adopted laboratory abnormality.
    findings = inv.medical_rows()
    abnormal_ids = {source.get('recordId') for row in findings
        if (fact := inv.bound_fact(row, inv.facts, 'clinical')) is not None
        and _clinical_deviation(row, fact) for source in row.get('sources', [])}
    selected = [row for row in priorities if any(s.get('recordId') in abnormal_ids
        for s in row.get('sources', []))]
    if selected:
        focus = [{**row, 'factIds': [source.get('recordId') for source in row.get('sources', [])]}
            for row in selected]
    for row in focus:
        linked = [inv.facts[fid] for fid in row.get('factIds', [])
            if fid in inv.facts and inv.facts[fid].kind == 'clinical']
        if not linked:
            continue
        for fact in linked:
            if fact.fact_id in seen:
                continue
            matches = [finding for finding in findings
                if inv.bound_fact(finding, inv.facts, 'clinical') is fact]
            # A focus inventory is a navigation scope, not a second adoption
            # lane for bare facts or an unreviewed previous paragraph.
            if not matches:
                continue
            seen.add(fact.fact_id)
            kind = _clinical_kind(matches[0], fact)
            imaging = kind == 'imaging'
            relevant = fact.fact_id in abnormal_ids or bool(
                imaging and confirmed_imaging_observations(fact.value))
            if not relevant:
                continue
            label = fact.name
            if kind == 'examination' and _EXAMINATION_QUALIFICATION.search(fact.value):
                label = _examination_label(fact.name, fact.value, fact.status)
            elif len(fact.value) <= 160 and re.search(r'[A-Za-z\u4e00-\u9fff]', fact.value):
                label += '所记“' + fact.value + '”'
            labels.append(label)
            rows.append({**matches[0], 'factIds': [fact.fact_id],
                '_clinicalLabel': label, '_clinicalImaging': imaging, '_clinicalKind': kind,
                '_clinicalFact': fact, '_clinicalAbnormal': relevant})
    paragraph = _Paragraph()
    if rows:
        paragraph.add(_clinical_priority(rows) or '既往医学记录中的' + '、'.join(labels)
            + '仍是后续随访的重点，应结合相关症状与后续检查观察其持续性。', rows)
    return paragraph


def _clinical_priority(rows, *, conclusion=False):
    """Different chapter jobs share facts, not a repeated problem catalogue."""
    # Input order already follows the adopted priorities. Imaging is a test
    # modality, not an independent reason to outrank an actual positive test.
    labels = []
    for row in rows:
        if not row.get('_clinicalAbnormal'):
            continue
        label = row.get('_clinicalProblemTitle') or row.get('_clinicalLabel')
        if label and label not in labels:
            labels.append(label)
        if len(labels) == 3:
            break
    if not labels:
        return None
    return ('后续重点关注' if conclusion else '当前医学关注包括') + '、'.join(labels) + '。'


def _clinical_problem_summary(inv):
    """Use internal organizer-verified titles with their original quotations."""
    paragraph = _Paragraph()
    frames = _at(inv.projection, '_clinicalProblemFrames', default=[])
    priorities = _at(inv.raw, 'healthOverview', 'keyFindings', default=[])
    order = {source.get('recordId'): index for index, row in reversed(list(enumerate(priorities)))
        for source in row.get('sources', []) if source.get('recordId')}
    candidates = []
    for frame in frames:
        sources = frame.get('sources') or []
        if not sources or not frame.get('title'):
            continue
        if any((fact := inv.facts.get(source.get('factId'))) is None
                or fact.kind not in {'clinical', 'self_reported'}
                or not source.get('quote') or source['quote'] not in fact.value for source in sources):
            continue
        if re.search(r'检查异常(?:待(?:复核|核实|核对))?$', frame['title']):
            continue
        rank = min((order.get(source['factId'], 10**6) for source in sources), default=10**6)
        candidates.append((rank, frame))
    candidates.sort(key=lambda item: item[0])
    names, rows = [], []
    for _, frame in candidates:
        if frame['title'] in names:
            continue
        names.append(frame['title'])
        ids = [source['factId'] for source in frame['sources']]
        rows.append({'factIds': ids, 'sources': [
            {'sourceCode': 'A' if inv.facts[fid].kind == 'clinical' else 'B',
             'recordId': fid, 'observedAt': inv.facts[fid].date} for fid in ids]})
        if len(names) == 3:
            break
    if names:
        paragraph.add('当前医学关注包括' + '、'.join(names) + '。', rows)
    return paragraph


def confirmed_imaging_observations(value):
    """Complete literal observations usable in the objective overview scope."""
    return [clause.strip() for clause in re.split(r'[，,。；;\r\n]', value)
        if len(re.findall(r'[\u4e00-\u9fff]', clause.strip())) >= 4 and not re.search(
            r'疑似|可能|考虑|倾向|待排|不能排除|未见|未发现|否认|不支持|无明显|对比|前片|建议|复查|随访', clause.strip())
        and re.search(r'结节|结石|囊肿|息肉|占位|积液|增厚|肿大|狭窄|密度|钙化|影|改变', clause.strip())]


def _reported_symptom_rows(inv):
    """Adopted positive B symptoms stay self-reported, never diagnosed by C."""
    result = []
    for row in inv.medical_rows():
        if (not str(row.get('code') or '').startswith('reportedSymptom:')
                or row.get('certainty') != 'self_reported' or row.get('value') in (None, '')):
            continue
        value = str(row['value']).strip()
        linked = [inv.facts[source['recordId']] for source in row.get('sources') or []
            if source.get('sourceCode') == 'B' and source.get('recordId') in inv.facts
            and inv.facts[source['recordId']].kind == 'self_reported'
            and all(part.strip() in inv.facts[source['recordId']].value
                for part in re.split(r'[；;]', value) if part.strip())
            and (not source.get('observedAt') or not inv.facts[source['recordId']].date
                or source['observedAt'] == inv.facts[source['recordId']].date)]
        if not linked:
            # A merged symptom row can span different actual answers. Check
            # every fragment against this row's bound A/B records; never use
            # an unrelated global source or call an A-only finding a B answer.
            candidates = []
            for source in row.get('sources') or []:
                fact = inv.facts.get(source.get('recordId'))
                code = source.get('sourceCode')
                if (fact is None or code not in {'A','B'}
                        or fact.kind != ('clinical' if code == 'A' else 'self_reported')
                        or source.get('observedAt') and fact.date and source['observedAt'] != fact.date):
                    continue
                candidates.append((source, fact))
            def supports(part, fact):
                supplied = fact.value.split('；回答：', 1)[-1]
                return any(not re.search(r'否认|没有|未出现|不伴|(?:^|[：:])无$',
                    re.split(r'[，,；;。\n]', supplied[:match.start()])[-1])
                    for match in re.finditer(re.escape(part), supplied))
            fragments = [(part.strip(), [(source, fact) for source, fact in candidates
                if supports(part.strip(), fact)])
                for part in re.split(r'[；;]', value) if part.strip()]
            if not fragments or any(not matches for _, matches in fragments):
                continue
            parts, linked, selected_sources = [], [], []
            for part, matches in fragments:
                own = next(((source, fact) for source, fact in matches
                    if source['sourceCode'] == 'B'), None)
                if own is None:
                    continue
                source, fact = own
                parts.append(part)
                if fact.fact_id not in {item.fact_id for item in linked}:
                    linked.append(fact); selected_sources.append(source)
            if not parts:
                continue
            value = '；'.join(parts)
            row = {**row, 'value': value, 'sources': selected_sources}
        phrase = re.sub(r'^(?:最近|近期|目前|现在|当前|还|另外|并且|而且)+', '', value).rstrip('。；;')
        if (not linked or phrase.casefold() in {'0','false','no','无','否','没有','不'}
                or re.search(r'^(?:否认|没有|无|未见|不伴)', phrase)):
            continue
        result.append({**row, 'factIds': [fact.fact_id for fact in linked], '_symptomPhrase': phrase})
    return result

def _reviewed_overview_focus(inv, owner, reviewed_overview):
    """Keep one complete B concern sentence only with live independent proof."""
    from modules.report.evaluation_narrative import _ReviewedEvaluationBlock, _Target, _reviewed_source_state
    from modules.report.evaluation_content import content_rule_failures
    path = ('healthOverview', 'overallSummary', 'text')
    if not isinstance(reviewed_overview, tuple) or len(reviewed_overview) != 2:
        return _Paragraph()
    block, target = reviewed_overview
    if (not isinstance(block, _ReviewedEvaluationBlock) or not isinstance(target, _Target)
            or target.path != path or not isinstance(owner, dict) or block.text != owner.get('text')
            or not _reviewed_source_state(block, target, inv.facts)
            or content_rule_failures(block.text, path=path, facts=inv.facts,
                assessment_context=target.assessment_context)):
        return _Paragraph()
    reported = _reported_symptom_rows(inv)
    for line, ids, cited, _ in block._sentence_sources:
        if (cited or len(re.findall(r'[。！？]', line)) > 1
                or not re.search(r'自述|问卷|主诉', line)
                or not re.search(r'关注|围绕|首要|优先|重点', line)):
            continue
        matching = [row for row in reported if set(ids) & set(row['factIds'])
            and row['_symptomPhrase'] in line]
        if matching:
            return _Paragraph().add(line, matching)
    return _Paragraph()


def _objective_overview(inv, *, reviewed_focus=None):
    """Project source anchors without assigning one source a fixed priority."""
    paragraph = _Paragraph()
    clinical, observations = [], []
    for row in inv.medical_rows():
        fact = inv.bound_fact(row, inv.facts, 'clinical')
        if fact is None or row.get('assessmentLevel') != '异常':
            continue
        clinical.append({**row, 'factIds': [fact.fact_id]})
        if fact.imaging:
            # Only a complete, independently factual source clause is used.
            # Never turn "疑似X" into "X" by removing its qualifier.
            observations.extend(confirmed_imaging_observations(fact.value))
        else:
            observations.append(fact.name + (fact.status if fact.status in {'偏高', '偏低', '阳性', '异常'} else '异常'))
    reported = _reported_symptom_rows(inv)
    symptom_names = list(dict.fromkeys(row['_symptomPhrase'] for row in reported))[:3]
    clinical_picture = _clinical_problem_summary(inv)
    if reviewed_focus and reviewed_focus.sentences:
        clinical_picture = reviewed_focus
    elif not clinical_picture.sentences and observations:
        clinical_picture.add('当前医学关注包括' + '、'.join(dict.fromkeys(observations[:3]))
            + ('，同时关注问卷自述的' + '、'.join(symptom_names) if symptom_names else '') + '。',
            [*clinical, *reported])
    elif not clinical_picture.sentences and reported:
        clinical_picture.add('当前自述不适为' + '、'.join(symptom_names) + '。', reported)
    if reviewed_focus and reviewed_focus.sentences:
        paragraph.sentences.extend(reviewed_focus.sentences)
        paragraph.rows.extend(reviewed_focus.rows)
    structure = inv.functions(inv.rows('microbiomeAssessment', 'overallStructure'))
    domains = _function_domains(inv)
    low, normal = _domain_groups(domains)
    if not paragraph.sentences:
        deviations = [row for row in structure if _structure_deviation(row)]
        native_deviations = [row for domain in domains for row in domain['rows']
            if row.get('_summaryNativeGroupAbnormal') or row.get('_summaryNativeDeviation')]
        if deviations or low or native_deviations:
            focus = ('菌群结构偏离' if deviations else '')
            focus += ('及相关功能弱项' if deviations else '具体功能弱项') if low else ''
            if not focus:
                focus = '检测已列的功能异常方向'
            paragraph.add('菌群与功能方面存在' + focus + '。',
                [*deviations, *native_deviations, *(row for group in low for row in group['rows'])])
    statements, used = [], []
    for row in inv.rows('microbiomeAssessment', 'overallStructure'):
        if row.get('code') != 'balanceStatus' or row.get('value') is None:
            continue
        linked = [inv.facts[s['recordId']] for s in row.get('sources', [])
            if s.get('recordId') in inv.facts and inv.facts[s['recordId']].value == str(row['value'])]
        if linked:
            statements.append('菌群平衡状态为' + re.sub(r'\s+', '', str(row['value'])))
            used.append({**row, 'factIds': [f.fact_id for f in linked]})
    for code in ('gmwi2', 'speciesCount', 'diversity'):
        matching = [row for row in structure if row.get('code') == code and row['_summaryLabel']]
        if matching:
            labels = list(dict.fromkeys(row['_summaryLabel'] for row in matching))
            statements.append(matching[0]['name'] + (
                '为' if len(matching) == 1 else '的已提供记录分级为' if len(labels) == 1 else
                '的已提供记录分级分别为') + '、'.join(labels))
            used.extend(matching)
    if statements:
        gmwi = [row for row in structure if row.get('code') == 'gmwi2']
        diversity = [row for row in structure if row.get('code') in {'speciesCount', 'diversity'}]
        if (gmwi and all(row['_summaryLabel'] in {'不健康倾向', '亚健康', '失衡', '异常'} for row in gmwi)
                and {row.get('code') for row in diversity if _normal(row)} == {'speciesCount', 'diversity'}
                and all(_normal(row) for row in diversity)):
            statements.append('整体呈现组成偏移与丰富度、多样性保留并存的结构特点')
        # The source labels remain exact, but the overview keeps only a few
        # anchors instead of copying the complete structure table.
        paragraph.add('菌群方面，' + '，'.join(statements[:2]) + '。', used)
    def retained_states(groups):
        return '，'.join(group['name'] + '的已提供项目保留' + _grades(group['rows']) + '分级'
            for group in groups)
    strong_abnormal = [domain for domain in domains if any(row.get('_summaryNativeDeviation')
        and row.get('_summaryLabel') in {'强','合成能力强'}
        and any(member['direction'] == 'up' for context in row.get('_summaryNativeContexts') or []
            for member in context['members']) for row in domain['rows'])]
    focus, focus_rows = [], []
    if low:
        focus.append('功能关注先放在' + '、'.join(group['name'] for group in low[:2]) + '等已列弱项')
        focus_rows.extend(row for group in low[:2] for row in group['rows'])
    elif normal:
        focus.append(retained_states(normal[:2]))
        focus_rows.extend(row for group in normal[:2] for row in group['rows'])
    if strong_abnormal:
        group = strong_abnormal[0]
        summary = _native_function_picture([group])
        focus.append(''.join(summary.sentences).rstrip('。') + '，需一并关注')
        focus_rows.extend(group['rows'])
    if not focus:
        abnormal = [domain for domain in domains if any(row.get('_summaryNativeGroupAbnormal')
            or row.get('_summaryNativeDeviation') for row in domain['rows'])]
        if abnormal:
            focus.append('功能关注以' + '、'.join(group['name'] for group in abnormal[:2]) + '的检测异常方向为主')
            focus_rows.extend(row for group in abnormal[:2] for row in group['rows'])
    elevated = [row for row in _risk_observations(inv) if row.get('sourceStatus') in {'中风险', '高风险'}]
    if elevated:
        focus.insert(0, '疾病风险需关注' + '、'.join(row['name'] for row in elevated[:2]))
        focus_rows.extend(elevated[:2])
    if focus:
        paragraph.add('；'.join(focus) + '。', focus_rows)
    if not reviewed_focus or not reviewed_focus.sentences:
        paragraph.sentences.extend(clinical_picture.sentences)
        paragraph.rows.extend(clinical_picture.rows)
    if paragraph.sentences:
        paragraph.sentences = [''.join(paragraph.sentences)]
    return paragraph


def enforce_objective_overview(raw, facts, *, projection=None, reviewed_overview=None, reviewed_blocks=None):
    """Preserve a live reviewed integrated judgment, or project source anchors."""
    from modules.report.evaluation_narrative import (
        _bound_fact, _summary_results, _ReviewedEvaluationBlock, _Target,
        _reviewed_source_state, _global_priority_missing,
    )
    from modules.report.evaluation_prose_roles import sentence_budget
    if not isinstance(raw.get('healthOverview'), dict):
        return
    proofs = dict(reviewed_blocks or {})
    if isinstance(reviewed_overview, tuple) and len(reviewed_overview) == 2:
        proofs[('healthOverview', 'overallSummary', 'text')] = reviewed_overview
    inv = _Inventory(raw, facts, projection, _summary_results, _bound_fact, reviewed_blocks=proofs)
    owner = raw['healthOverview'].get('overallSummary')
    if reviewed_overview is None:
        reviewed_overview = proofs.get(('healthOverview', 'overallSummary', 'text'))
    reviewed_focus = _reviewed_overview_focus(inv, owner, reviewed_overview)
    complete_review = False
    if isinstance(reviewed_overview, tuple) and len(reviewed_overview) == 2:
        block, target = reviewed_overview
        if isinstance(block, _ReviewedEvaluationBlock) and isinstance(target, _Target):
            kinds = {facts[fid].kind for _, ids, _, _ in block._sentence_sources for fid in ids if fid in facts}
            complete_review = bool(target.path == ('healthOverview', 'overallSummary', 'text')
                and block.text == owner.get('text')
                and kinds & {'clinical', 'self_reported', 'microbe', 'function', 'context'}
                and len(re.findall(r'[。！？]', block.text)) <= sentence_budget(target.path)[1]
                and _reviewed_source_state(block, target, facts)
                and not _global_priority_missing(target, block, facts))
    paragraph = _Paragraph() if complete_review else _objective_overview(inv, reviewed_focus=reviewed_focus)
    if paragraph.sentences:
        _put(owner, paragraph, inv, discard_inapplicable=True)
    # A concise medical summary can keep its live, role-complete review.
    # Without that proof, derive it from the same adopted observations as its
    # table rather than retaining an unverified diagnostic/management block.
    medical = _medical_module(inv)
    if medical.sentences:
        _put(raw['healthOverview'].get('summaries', {}).get('medical'), medical, inv)


def _medical_module(inv):
    """Describe the clinical pattern without copying the priority table."""
    adopted = _clinical_problem_summary(inv)
    if adopted.sentences:
        return adopted
    normal, abnormal, imaging, laboratory, examinations = [], [], [], [], []
    for collection in ('findings', 'normalFindings'):
        for row in inv.medical_rows(collection):
            fact = inv.bound_fact(row, inv.facts, 'clinical')
            if fact is None:
                continue
            kind = _clinical_kind(row, fact)
            is_imaging = kind == 'imaging'
            uncertain_imaging = is_imaging and bool(re.search(r'疑似|可能|待排|性质待定', fact.value))
            deviated = _clinical_deviation(row, fact)
            if row.get('assessmentLevel') != '正常' and not deviated and not uncertain_imaging:
                continue
            actual = {**row, 'factIds': [fact.fact_id]}
            if row.get('assessmentLevel') == '正常' and not fact.abnormal:
                normal.append(actual)
                continue
            if not deviated and not uncertain_imaging:
                continue
            abnormal.append(actual)
            if is_imaging:
                imaging.append(actual)
            elif kind == 'laboratory':
                laboratory.append(actual)
            else:
                examinations.append(actual)
    paragraph = _Paragraph()
    if laboratory:
        selected = sorted(laboratory, key=lambda row: (
            not bool(re.search(r'阳性|\d\s*\+', str(row.get('value') or '')))))[:2]
        paragraph.add('检验需关注' + '、'.join(row['name'] + ' ' + str(row.get('value'))
            + (row.get('unit') or '') for row in selected) + '。', selected)
    if imaging:
        confirmed = []
        for row in imaging:
            for clause in re.split(r'[，,。；;\r\n]', str(row.get('value') or '')):
                if (re.search(r'结节|结石|囊肿|息肉|占位|积液|增厚|肿大|密度|钙化|影|改变', clause)
                        and not re.search(r'疑似|可能|考虑|倾向|待排|不能排除|未见|未发现|无明显|建议|复查|随访', clause)):
                    confirmed.append(clause.strip())
        if confirmed:
            paragraph.add('影像记录有' + '、'.join(dict.fromkeys(confirmed[:2])) + '。', imaging)
    if examinations:
        labels = []
        for row in examinations:
            labels.append(_examination_label(row['name'], row.get('value'), row.get('sourceStatus')))
        paragraph.add('其他检查需关注' + '、'.join(dict.fromkeys(labels[:2])) + '。', examinations[:2])
    if abnormal and not imaging and not laboratory and not examinations:
        paragraph.add('已有医学异常发现仍需结合后续症状与检查结果持续关注。', abnormal)
    # One adopted symptom row may contain fragments from several B answers.
    # Reuse the per-fragment source matcher used by the overview; requiring
    # every fragment in one fact discarded the real merged bowel complaint.
    from modules.report.evaluation_narrative import _medical_reported_focus_rows
    reported = _medical_reported_focus_rows(inv.raw, inv.facts)
    symptom_names = list(dict.fromkeys(row['_symptomPhrase'] for row in reported))
    if reported:
        paragraph.add('本次问卷自述的主要不适为' + '、'.join(symptom_names)
            + ('，按各自症状主题分别关注。' if len(reported) > 1 else '。'), reported)
    if paragraph.sentences:
        paragraph.sentences = [''.join(paragraph.sentences)]
    return paragraph


def source_clinical_correlations(raw, facts, *, projection=None):
    """Compare A/B observations with C results; C never corroborates itself."""
    from modules.report.evaluation_narrative import _bound_fact, _summary_results, _relevant_clinical_facts
    from modules.report.evaluation_result_rules import has_risk_index
    from modules.report.evaluation_disease_context import category_associations
    from modules.report.evaluation_native_modules import native_association_result, native_association_positive
    from modules.report.evaluation_fixed_assessment import _goal_only_question
    inv = _Inventory(raw, facts, projection, _summary_results, _bound_fact)
    abnormal_ids = {fact.fact_id for row in inv.medical_rows()
        if row.get('assessmentLevel') == '异常' and (fact := _bound_fact(row, facts, 'clinical')) is not None}
    result = {}
    category_names = {
        'intestinal': r'肠|胃|排便|便秘|腹胀|腹泻|腹痛',
        'metabolic': r'代谢|血糖|葡萄糖|糖化|血脂|血压|糖尿病|脂肪肝|冠心病|心血管|体重|BMI|腰围|甘油三酯|胆固醇|脂蛋白|肝|胆|胰|甲状腺|营养|贫血',
        'mental': r'情绪|心理|焦虑|抑郁|睡眠|入睡|失眠|孤独症|自闭症|社交沟通|刻板行为',
        'neurological': r'帕金森|阿尔|阿兹|认知|记忆|震颤|癫痫|脑|神经',
        'tumor': r'肿瘤|结节|间质瘤|占位|肿块|癌|隐血试验|粪便隐血',
        'autoimmune': r'免疫|类风湿|红斑狼疮|强直性|多发性硬化|1型糖尿病|Ⅰ型糖尿病|I型糖尿病|过敏|湿疹|鼻炎|荨麻疹|哮喘|感染|嗜酸性粒细胞',
        'aging': r'骨质疏松|骨密度|衰老|衰弱|肌少|跌倒|握力|动脉粥样硬化|认知下降|frailty|sarcopenia',
        'allergy': r'鼻塞|喷嚏|流涕|鼻炎|鼻黏膜|嗜酸性粒细胞|过敏|湿疹|荨麻疹|紫癜',
        'respiratory': r'哮喘|喘息|喘鸣|反复呼吸道感染|反复感冒|反复肺炎|睡眠呼吸暂停|打鼾|憋醒',
        'nutrition': r'营养不良|贫血|血红蛋白|血清铁|铁蛋白|转铁蛋白|体重不增|生长迟缓',
    }
    for index, category in enumerate(_at(raw, 'diseaseRiskAssessment', 'categories', default=[])):
        c_rows = []
        for row in category.get('items', []):
            actual_association = (str(row.get('code') or '').startswith(('association:', 'nativeAssociation:'))
                and row.get('sourceStatus') in {'检出', '阳性', '异常'}
                and any(source.get('sourceCode') == 'C' and (fact := facts.get(source.get('recordId')))
                    and fact.kind == 'context' and fact.status == row.get('sourceStatus')
                    for source in row.get('sources', [])))
            numerical_model = has_risk_index(row) and not str(row.get('code') or '').startswith(('association:', 'nativeAssociation:'))
            if not numerical_model and not actual_association:
                continue  # A/B attention is not an original C model result.
            ids = [source['recordId'] for source in row.get('sources', [])
                if source.get('sourceCode') == 'C' and source.get('recordId') in facts
                and facts[source['recordId']].kind in {'function', 'context', 'microbe'}]
            sources = deepcopy(row.get('sources', []))
            original = facts.get(row.get('code'))
            if (not ids and original is not None and original.kind == 'function'
                    and row.get('name') and row['name'] in original.name
                    and str(row.get('value')) == original.value
                    and row.get('sourceStatus') == original.status):
                ids = [original.fact_id]
                sources.append({'sourceCode': 'C', 'recordId': original.fact_id, 'observedAt': original.date})
            if ids:
                c_rows.append({**row, 'sources': sources, 'factIds': ids,
                    **({'_nativeAssociation': True} if actual_association else {})})
        for row in category_associations(raw, category['code']):
            if not native_association_positive(row):
                continue
            ids = [source['recordId'] for source in row.get('sources', [])
                if source.get('sourceCode') == 'C' and source.get('recordId') in facts
                and facts[source['recordId']].kind == 'context']
            if ids:
                c_rows.append({**row, 'factIds': ids, '_nativeAssociation': True})
        if not c_rows:
            continue
        owner = {'text': None, 'sources': [], 'evidenceRefs': [], 'availability': 'unavailable'}
        result[index] = owner
        pattern = category_names.get(category['code'])
        def relevant_normal(fact):
            # An adopted normal result is useful cross-source information,
            # not evidence that a source model's disease has been excluded.
            return fact.kind == 'clinical' and (
                category['code'] == 'metabolic' and re.search(r'血糖|葡萄糖|糖化血红蛋白', fact.name)
                or category['code'] in {'intestinal', 'tumor'}
                and any(re.search(r'结直肠癌|大肠癌|胃癌', row.get('name') or '')
                    for row in category.get('items', []))
                and re.search(r'粪便隐血|大便隐血|隐血试验', fact.name)
                or category['code'] == 'nutrition' and re.search(r'血红蛋白|血清铁|铁蛋白|转铁蛋白', fact.name))
        pool = ([fact for fact in facts.values() if fact.kind in {'clinical', 'self_reported'}]
            if category['code'] in {'allergy', 'respiratory', 'nutrition'} else
            _relevant_clinical_facts(category['code'], list(facts.values())))
        if category['code'] in {'autoimmune', 'mental', 'neurological', 'tumor'}:
            from modules.report.evaluation_fixed_assessment import _clinical_pattern
            from modules.report.evaluation_catalog import fixed_disease_code
            disease_patterns = [expression for row in category.get('items', [])
                if (expression := (_clinical_pattern(row.get('code'))
                    or _clinical_pattern(fixed_disease_code(row.get('name') or ''))))]
            pool = [fact for fact in pool if any(re.search(expression,
                fact.name + ' ' + fact.value, re.I) for expression in disease_patterns)]
        clinical = [fact for fact in pool
            if pattern and re.search(pattern, fact.name + ' ' +
                re.sub(r'^问题：.*?；回答：', '', fact.value, flags=re.S), re.I)
            and not (fact.kind == 'self_reported' and re.search(
                r'家族|父亲|母亲|血亲|亲属', fact.name + ' ' + fact.value))
            and not (fact.kind == 'self_reported' and fact.value.startswith('问题：')
                and fact.name in re.sub(r'^问题：.*?；回答：', '', fact.value, flags=re.S)
                and not re.search(pattern, fact.name, re.I))
            and not (fact.kind == 'self_reported' and _goal_only_question(fact))
            and (fact.kind == 'self_reported' or fact.abnormal or fact.fact_id in abnormal_ids
                or relevant_normal(fact)
                or any(row['name'] in fact.name + ' ' + fact.value for row in c_rows if len(row['name']) >= 3))
            and not (fact.kind == 'clinical' and not fact.abnormal and not relevant_normal(fact)
                and (fact.status in {'正常', '阴性'} or re.search(r'未见.*异常|无明显异常', fact.value)))]
        # Prefer direct complaint/condition records to incidental words inside
        # one long answer. A family history remains attributed to the relative.
        mirrors = [re.fullmatch(r'问题：(.*?)；回答：(.*)', fact.value, re.S)
            for fact in clinical if fact.kind == 'self_reported']
        clinical = [fact for fact in clinical if not any(match and fact.name == match[1]
            and fact.value == match[2] for match in mirrors)]
        clinical.sort(key=lambda fact: (not bool(fact.kind == 'clinical' and (
            fact.abnormal or fact.fact_id in abnormal_ids)),
            not relevant_normal(fact), bool(re.search(r'家族|父亲|母亲|血亲|亲属', fact.name)),
            not bool(fact.kind == 'self_reported' and re.search(
            r'主诉|入睡困难|排便费力|便秘|腹胀|鼻塞|喷嚏|流涕|喘息|喘鸣', fact.name)), not bool(re.search(pattern, fact.name, re.I)),
            fact.value.startswith('问题：'), not fact.abnormal))
        grouped = {}
        for fact in clinical:
            value = re.sub(r'^问题：.*?；回答：', '', fact.value, flags=re.S)
            identity = ((fact.source, fact.date, fact.value, fact.unit, fact.status)
                if fact.kind == 'self_reported' and fact.value.startswith('问题：') else (fact.fact_id,))
            group = grouped.setdefault(identity, {'lead': fact, 'value': value, 'members': []})
            group['members'].append(fact)
        groups = list(grouped.values())[:(5 if category['code'] == 'metabolic' else
            3 if category['code'] in {'neurological', 'tumor', 'allergy', 'respiratory', 'nutrition'} else 2)]
        selected = [(group['lead'], group['value']) for group in groups]
        if not selected:
            continue
        themes = [(r'睡眠|失眠', r'睡眠|入睡|失眠', '睡眠'),
            (r'便秘', r'排便|便秘', '排便'),
            (r'肠易激', r'肠易激|(?<![A-Za-z])IBS(?![A-Za-z])', '肠易激'),
            (r'腹胀|排气', r'腹胀|排气', '腹胀排气'),
            (r'心血管|冠心', r'心血管|冠心', '心血管背景'),
            (r'过敏性鼻炎', r'鼻塞|喷嚏|流涕|鼻黏膜|嗜酸性粒细胞', '鼻部'),
            (r'缺铁性贫血', r'贫血|血红蛋白|血清铁|铁蛋白|转铁蛋白', '贫血')]
        shared = lambda row: [label for c_pattern, ab_pattern, label in themes
            if re.search(c_pattern, row['name']) and any(
                re.search(ab_pattern, fact.name + ' ' + value) for fact, value in selected)]
        relevant_c = sorted(c_rows, key=lambda row: (not bool(shared(row)),
            {'高风险': 0, '中风险': 1, '低风险': 2}.get(row.get('sourceStatus'), 3)))[:2]
        family_only = all(fact.kind == 'self_reported' and re.search(
            r'家族|父亲|母亲|血亲|亲属', fact.name + ' ' + value) for fact, value in selected)
        if family_only:
            relevant_c = [row for row in c_rows if shared(row)][:2]
            if not relevant_c:
                continue
        ab_rows = []
        for group in groups:
            ab_rows.append({'factIds': [member.fact_id for member in group['members']], 'sources': [
                {'sourceCode': 'A' if member.kind == 'clinical' else 'B',
                    'recordId': member.fact_id, 'observedAt': member.date} for member in group['members']]})
        # This is a source-state projection, not an approved AI paragraph or
        # a new medical mechanism. Keep the clinical state and the original
        # model level distinct without rebuilding a dated numeric roster.
        abnormal = [fact for fact, _ in selected if fact.kind == 'clinical'
            and (fact.abnormal or fact.fact_id in abnormal_ids)]
        normals = [fact for fact, _ in selected if relevant_normal(fact)
            and not fact.abnormal and fact.fact_id not in abnormal_ids
            and fact.status in {'正常', '阴性'}]
        statements = []
        if abnormal:
            names = list(dict.fromkeys(fact.name for fact in abnormal))
            statements.append('本类已记录的检查异常主要涉及' + '、'.join(names[:3]) + '。')
        if normals:
            statements.append('、'.join(dict.fromkeys(fact.name for fact in normals))
                + '保留原正常结果。')
        selected_ids = {member.fact_id for group in groups for member in group['members']}
        symptoms = [row for row in _reported_symptom_rows(inv)
            if category['code'] == 'intestinal' and any(source.get('recordId') in selected_ids
                for source in row.get('sources', []))]
        if symptoms:
            statements.append('本次自述不适包括' + '、'.join(dict.fromkeys(
                str(row['name']).replace('（自述）', '') for row in symptoms)) + '。')
            ab_rows.extend(symptoms)
        else:
            descriptions = []
            for fact, value in selected:
                if fact.kind != 'self_reported':
                    continue
                if family_only:
                    descriptions.append('亲属病史背景为' + value.rstrip('。'))
                elif value.strip().casefold() in {'false', '否', '无'}:
                    if category['code'] == 'neurological' and re.search(
                            r'医生.*诊断.*(?:慢性疾病|发育障碍)', fact.name):
                        descriptions.append('问卷未报告已确诊的慢性疾病或发育障碍')
                    elif category['code'] == 'neurological' and re.search(
                            r'(?:语言|交流|互动|发育).*专业人员.*异常', fact.name):
                        descriptions.append('问卷未报告专业人员指出的语言交流、互动或发育异常')
                    else:
                        # Keep an explicit negative without guessing the
                        # inverse of an unfamiliar or compound question.
                        descriptions.append('问卷记录' + fact.name.rstrip('？?') + '：否')
                else:
                    display = fact.name if fact.value.startswith('问题：') and fact.name in value else value
                    if category['code'] in {'allergy', 'respiratory', 'nutrition'} or fact.value.startswith('问题：'):
                        display = value
                        # One answer may contain unrelated symptom
                        # clauses. Present only literal clauses of this theme;
                        # the full source fact and locator remain unchanged.
                        parts = re.split(r'[，,、；;。\r\n]', display)
                        display = '、'.join(part.strip() for part in parts
                            if part.strip() and re.search(pattern, part, re.I))
                        if not display:
                            continue
                    if category['code'] == 'mental' and (match := re.search(
                            r'(?:无|未有|没有)已知精神疾病诊断', display)):
                        display = match[0]
                    elif category['code'] == 'mental' and re.search(r'睡眠|午睡|作息', fact.name) \
                            and (match := re.search(r'(?:通常|一般)?无夜醒', display)):
                        display = match[0]
                    elif category['code'] in {'mental', 'neurological'}:
                        parts = re.split(r'[；;。]', display)
                        if len(parts) > 1 and not any(re.match(r'\s*(?:但|不过|然而|同时)', part)
                                for part in parts[1:] if part.strip()):
                            display = parts[0]
                    descriptions.append('问卷记录' + display.rstrip('。'))
            if category['code'] == 'mental' and any('无已知精神疾病诊断' in text for text in descriptions):
                descriptions = [text for text in descriptions if '无已知精神疾病诊断' in text]
            if descriptions:
                statements.append('；'.join(dict.fromkeys(descriptions)) + '。')
        if not statements:
            continue  # A C-only sentence is not a clinical comparison.
        models = [row for row in (relevant_c if family_only else c_rows) if has_risk_index(row) and not row.get('_nativeAssociation')]
        model_levels = {row.get('sourceStatus') for row in models}
        if len(models) > 1 and len(model_levels) == 1 and next(iter(model_levels)) in {'低风险', '中风险', '高风险'}:
            model_sources = models
            statements.append('本类已给出的菌群疾病模型均为' + models[0]['sourceStatus'] + '。')
        else:
            model_sources = sorted(models, key=lambda row:
                {'高风险': 0, '中风险': 1, '低风险': 2}.get(row.get('sourceStatus'), 3))[:1]
            if model_sources:
                row = model_sources[0]
                statements.append('原菌群模型中的' + row['name'] + '（'
                    + str(row.get('sourceStatus') or row.get('value')) + '）是本类保留的模型信号。')
        association_candidates = [row for row in (relevant_c if family_only else c_rows)
            if row.get('_nativeAssociation') and row.get('sourceStatus') in {'检出', '阳性', '异常'}]
        associations = sorted(association_candidates,
            key=lambda row: (not any(model['name'] == row['name'] for model in model_sources)
                if model_sources and category['code'] != 'intestinal' else not bool(shared(row)),
                not bool(shared(row))))[:1]
        if associations:
            row = associations[0]
            label = native_association_result(row)
            statements.append(row['name'] + '：' + label + '。' if label else
                row['name'] + ('相关菌群筛查' if '相关菌群' not in row['name'] else '')
                + '（' + row['sourceStatus'] + '）。')
        if abnormal and category['code'] == 'metabolic':
            statements.append('现有代谢检查异常、已测糖代谢状态与菌群模型分级按各自维度关注。'
                if normals else '已有代谢检查异常仍是本类状态评估的关注内容。')
        elif symptoms and models:
            statements.append('本类评估保留实际不适的关注，也保留原菌群模型的独立分级。')
        if not statements or not (model_sources or associations):
            continue
        owner.update(text=''.join(statements),
            sources=inv.sources([*ab_rows, *model_sources, *associations]), availability='partial')
    return result


def _reported_background(inv):
    """Adopt attributed history for priorities, never the medical-finding lane."""
    history = []
    for row in _at(inv.raw, 'appendices', 'sourceItems', default=[]):
        if row.get('kind') != 'context' or row.get('category') != 'sourceReportedBackground' \
                or row.get('sourceType') != 'microbiome' or not re.search(r'病史|疾病史', row.get('name') or ''):
            continue
        fact = inv.bound_fact(row, inv.facts, 'function')
        if fact is not None and fact.kind == 'context' and not re.fullmatch(
                r'无|否认|不详|未提供|无(?:既往|特殊)?病史', fact.value.strip()):
            history.append({**row, 'factIds': [fact.fact_id], '_historyValue': fact.value})
    if not history:
        return _Paragraph()
    chosen = history[:1]
    return _Paragraph().add('健康管理应优先围绕原报告记载的' + chosen[0]['_historyValue'].rstrip('。')
        + '持续随访，并结合既有诊疗安排跟踪相关状态的变化。', chosen)


def _behavior_dimensions(inv, *, actionable_only=False):
    """Summarise adjustable dimensions actually described by B answers.

    These are behavioural relationships, not clinical causes or prescriptions.
    Bed/wake times remain source observations; no sleep duration is derived.
    """
    dimensions = {
        'diet': ('饮食', [(r'蔬菜|蔬果|食物|外卖', '食物与蔬菜安排'),
            (r'进餐|晚餐|夜宵|早餐', '进餐节律'), (r'饮水|喝水', '日常饮水'),
            (r'咖啡', '咖啡饮用时间')]),
        'exercise': ('活动', [(r'步行|骑车|运动|锻炼', '已有活动的规律性'),
            (r'久坐|连续坐|起身', '连续久坐与起身安排')]),
        'sleep': ('睡眠', [(r'上床|起床|睡起|作息|入睡', '上床起床与入睡安排'),
            (r'手机|屏幕', '睡前屏幕使用')]),
        'stress': ('压力应对', [(r'音乐|散步|放松|减压', '已有放松方式的规律性')]),
    }
    parts, selected = [], []
    for row in _at(inv.raw, 'medicalAssessment', 'triggerAnalysis', default=[]):
        if row.get('code') not in dimensions:
            continue
        if actionable_only and (not row.get('association')
                or row.get('modifiability') not in {'高','中'}
                or row.get('evidenceStrength') not in {'强','中'}):
            continue
        own = [inv.facts[source['recordId']] for source in row.get('sources', [])
            if source.get('sourceCode') == 'B' and source.get('recordId') in inv.facts
            and inv.facts[source['recordId']].kind == 'self_reported'
            and not re.fullmatch(r'(?:不清楚|不知道|不详|未知|未回答|未提供|无法回答)[。\s]*',
                inv.facts[source['recordId']].value)]
        if not own:
            continue
        label, patterns = dimensions[row['code']]
        features = [name for pattern, name in patterns
            if any(re.search(pattern, fact.name + ' ' + fact.value) for fact in own)]
        if not features:
            continue
        parts.append(label + '方面的' + '、'.join(features))
        selected.append({**row, 'factIds': [fact.fact_id for fact in own]})
    return parts, selected


def _behavior_picture(inv, *, concise=False, actionable_only=False):
    parts, selected = _behavior_dimensions(inv, actionable_only=actionable_only)
    if concise and parts:
        preferred = {'diet': '进餐节律', 'exercise': '连续久坐与起身安排', 'sleep': '睡前屏幕使用'}
        directions = []
        for part, row in zip(parts, selected):
            wanted = preferred.get(row['code'])
            directions.append(wanted if wanted and wanted in part else
                part.split('方面的', 1)[-1].split('、', 1)[0])
        return _Paragraph().add('日常调整可先围绕' + '、'.join(directions)
            + '开展，并观察实际主诉的变化。', selected)
    return _Paragraph().add('问卷显示，本次可调整的日常环节集中在' + '；'.join(parts)
        + '。这些环节可与实际主诉的变化一并持续观察。', selected) if parts else _Paragraph()


def _matrix_behavior_summary(inv):
    """Roll up adopted AI row judgements without judging the habits again."""
    selected, priority, secondary, favorable = [], [], [], []
    for row in _at(inv.raw, 'medicalAssessment', 'triggerAnalysis', default=[]):
        if not row.get('association') or not row.get('sources'):
            continue
        strength = re.match(r'强|中|弱|不足', str(row.get('evidenceStrength') or ''))
        action = re.match(r'高|中|维持|—', str(row.get('modifiability') or ''))
        if strength is None or action is None:
            continue
        own = [inv.facts[source['recordId']] for source in row['sources']
            if source.get('recordId') in inv.facts and source.get('sourceCode') == 'B'
            and inv.facts[source['recordId']].kind == 'self_reported']
        if not own:
            continue
        association = str(row['association'])
        non_trigger = bool(re.search(r'不列.{0,10}(?:重点|诱因)|不(?:作为|列为|列入|纳入).{0,10}(?:重点|诱因)|'
            r'(?:未|没有|未能)识别.{0,12}(?:不良|诱因)|未发现.{0,12}(?:不良|诱因)', association))
        if action[0] in {'高','中'}:
            if non_trigger:
                continue
            if strength[0] in {'强','中'}:
                priority.append(row['name']); selected.append(row)
            elif strength[0] == '弱':
                secondary.append(row['name']); selected.append(row)
        if action[0] == '维持':
            for fact in own:
                if re.search(r'不清楚|不知道|未知|不详', fact.value):
                    continue
                if re.search(r'从不吸烟|从不抽烟|不吸烟|不抽烟', fact.value):
                    favorable.append('不吸烟'); selected.append(row)
                if row.get('code') == 'stress' and re.search(r'阅读|读书|音乐|散步|放松', fact.value):
                    favorable.append('已有的放松习惯'); selected.append(row)
    paragraph = _Paragraph()
    if priority:
        paragraph.add('结合本章诱因分析，本次优先关注' + '、'.join(dict.fromkeys(priority))
            + '中的可调整环节。', selected)
    if secondary:
        paragraph.add('、'.join(dict.fromkeys(secondary)) + '作为次要观察方向，与实际主诉变化一并观察。', selected)
    if favorable:
        paragraph.add('可继续保持' + '与'.join(dict.fromkeys(favorable)) + '。', selected)
    return paragraph


def project_behavioral_content(raw, facts, *, projection=None):
    """Keep B's actual behavioural dimensions in the final editorial slots."""
    from modules.report.evaluation_narrative import _bound_fact, _summary_results
    inv = _Inventory(raw, facts, projection, _summary_results, _bound_fact)
    parts, rows = _behavior_dimensions(inv, actionable_only=True)
    # Per-row applicability/strength are the reviewed individual judgments.
    # Do not overwrite them with a fixed ‘可调整的重点’ source roster.
    behavior = _behavior_picture(inv, actionable_only=True)
    if not behavior.sentences:
        summary = _matrix_behavior_summary(inv)
        if summary.sentences:
            _put(raw['medicalAssessment']['triggerConclusion'], summary, inv, discard_inapplicable=True)
        return
    ids = {fid for row in behavior.rows for fid in row.get('factIds', [])}
    for owner, paragraph in ((raw['medicalAssessment']['triggerConclusion'], behavior),
            (raw['comprehensiveConclusion']['overallConclusion'], _behavior_picture(inv, concise=True, actionable_only=True))):
        if not ids.intersection(source.get('recordId') for source in owner.get('sources', [])):
            # Add a complete source-bound paragraph; do not edit a reviewed
            # scientific claim or borrow its citations for these B facts.
            existing = owner.get('text')
            sources = deepcopy(owner.get('sources', []))
            owner['text'] = '\n'.join(part for part in (existing, *paragraph.sentences) if part)
            for source in inv.sources(paragraph.rows):
                if source not in sources:
                    sources.append(source)
            owner.update(sources=sources, availability='partial')
    for direction in raw['comprehensiveConclusion'].get('priorityDirections', []):
        action = direction.get('followupAction') or ''
        if re.fullmatch(r'持续关注.+来源模型风险提示及其后续变化。', action):
            direction['followupAction'] = '后续就诊时结合该方向实际主诉的变化及已有临床资料，评估上述菌群来源模型提示。'
    summary = _matrix_behavior_summary(inv)
    if summary.sentences:
        # Row strengths/actions have already passed individual review. A
        # second free-text assessment must not reverse their direction.
        _put(raw['medicalAssessment']['triggerConclusion'], summary, inv, discard_inapplicable=True)


def _trigger_background(inv):
    """Describe adopted symptom/history context without inventing a cause."""
    groups = {'symptoms': [], 'history': [], 'medications': []}
    for row in _at(inv.raw, 'appendices', 'sourceItems', default=[]):
        if row.get('kind') != 'context' or row.get('category') != 'sourceReportedBackground':
            continue
        fact = inv.bound_fact(row, inv.facts, 'function')
        if fact is None or fact.kind != 'context' or re.fullmatch(
                r'无|否认|不详|未提供|无(?:既往|特殊)?病史|无(?:症状|主诉|用药)', fact.value.strip()):
            continue
        key = next((key for key, pattern in (('symptoms', r'症状|主诉'),
            ('history', r'病史|疾病史'), ('medications', r'用药|药物'))
            if re.search(pattern, row.get('name') or '')), None)
        if key:
            groups[key].append({**row, 'factIds': [fact.fact_id], '_value': fact.value})
    paragraph = _behavior_picture(inv)
    symptoms, history, medications = (groups[key] for key in ('symptoms', 'history', 'medications'))
    symptom_text = '；'.join(row['_value'] for row in symptoms)
    history_text = '；'.join(row['_value'] for row in history)
    if symptoms and history:
        bowel = next((phrase for phrase in ('排便困难或排便不尽感', '排便困难', '排便不尽感')
            if phrase in symptom_text), None)
        if bowel and '便秘' in history_text:
            constipation = '慢性便秘' if '慢性便秘' in history_text else '便秘'
            paragraph.add('原报告记载的' + bowel + '与' + constipation
                + '病史构成一致的排便关注方向，随访应以排便变化和既有病史管理为主线。', [*symptoms, *history])
        else:
            paragraph.add('原报告所列主诉与病史应并行随访，关注症状是否持续或反复，并结合既有诊疗安排观察其变化。',
                [*symptoms, *history])
    elif symptoms:
        paragraph.add('已记载的主诉是本节持续观察的重点，后续围绕症状是否持续、反复及其实际变化开展随访。', symptoms)
    elif history:
        paragraph.add('已记载病史应纳入持续管理，并结合既有诊疗安排观察相关状态的变化。', history)
    if medications:
        paragraph.add('原报告所列用药应纳入同一随访背景，关注用药期间症状和实际表现的变化。', medications)
    return paragraph


def _important_normal_records(inv):
    """Select explicit examination conclusions, not arbitrary zero-value rows."""
    result = []
    conflict_ids = {source.get('recordId') for row in _at(inv.raw, 'medicalAssessment', 'consistencyNotes', default=[])
        for source in row.get('sources', [])}
    for row in _at(inv.raw, 'medicalAssessment', 'normalFindings', default=[]):
        fact = inv.bound_fact(row, inv.facts, 'clinical')
        if fact is None or fact.abnormal or fact.fact_id in conflict_ids \
                or row.get('assessmentLevel') != '正常' or row.get('sourceStatus') != fact.status:
            continue
        if not re.search(r'影像|超声|CT|MRI|磁共振|核磁|X线|胸片|内镜|心电图',
                (row.get('system') or '') + ' ' + fact.name, re.I):
            continue
        literal_normal = bool(re.search(r'未见(?:明显)?异常|无明显异常|(?:检查|心电图|结果)正常', fact.value))
        if fact.status in {'正常', '阴性', '未见异常'} or fact.status is None and literal_normal:
            result.append({**row, 'factIds': [fact.fact_id]})
    return result[:2]


def _function_distribution(rows):
    """Keep literal observations separate from their contextual support."""
    facts = {row['_summaryFact'].fact_id: row['_summaryFact'] for row in rows
        if row.get('_summaryFact') is not None}
    facts.update({fact.fact_id: fact for row in rows
        for fact in row.get('_summaryMeasurementFacts') or ()})
    return build_source_distribution(rows, facts)


def _function_difference(rows):
    """Describe every supplied member, including normal and ungraded values."""
    return render_source_distribution(_function_distribution(rows))


def _native_function_comparison(rows):
    native = [row for row in rows if row.get('_summaryNativeContexts')]
    return render_source_distribution(_function_distribution(native)), {id(row) for row in native}


def _native_function_picture(domains):
    statements, rows = [], []
    for domain in domains:
        if domain['code'] in {'P9', 'P10'}:
            continue
        actual = [row for row in domain['rows']
            if row.get('_summaryLabel') in {'正常', '强', '合成能力强'}
                and (row.get('_summaryNativeGroupAbnormal') or row.get('_summaryNativeDeviation'))]
        directions = {member['direction'] for row in actual
            for group in row.get('_summaryNativeContexts') or [] for member in group['members']
            if member['direction']}
        if not actual or not directions:
            continue
        direction = '降低' if directions == {'down'} else '升高' if directions == {'up'} else '升高及降低'
        strong = [row for row in actual if row['_summaryLabel'] in {'强','合成能力强'}]
        states = list(dict.fromkeys(group['sourceStatus'] for row in actual
            for group in row.get('_summaryNativeContexts') or [] if group['sourceStatus']))
        state = ('整体为' + states[0] if len(states) == 1 else '整体记录分别为' + '、'.join(states))
        if strong:
            statements.append(domain['name'] + '评分为' + _grades(strong) + '，' + state + '（' + direction + '方向）')
        else:
            # A group's abnormal label does not change a normal child's band.
            # Preserve each supplied arrow as that member's own observation.
            for row in actual:
                own_directions = {member['direction'] for group in row.get('_summaryNativeContexts') or []
                    for member in group['members'] if member.get('direction')}
                own_direction = ('降低' if own_directions == {'down'} else '升高'
                    if own_directions == {'up'} else '升高及降低')
                statements.append(row['name'] + '分级为' + row['_summaryLabel']
                    + '，另有' + own_direction + '方向标记')
        rows.extend(actual)
    return _Paragraph().add('；'.join(statements) + '。' if statements else None, rows)


def _native_pathway_summary(domains):
    """Compare a few source-confirmed shortfalls and retained domains."""
    entries = {}
    for domain in domains:
        for row in domain['rows']:
            for context in row.get('_summaryNativeContexts') or []:
                identity = context['panel'], tuple(context['aggregateFactIds'])
                entry = entries.setdefault(identity, {'context': context, 'domain': domain, 'rows': [], 'members': {}})
                entry['rows'].append(row)
                for member in context['members']:
                    entry['members'][tuple(member['factIds'])] = member
    if not entries:
        return _Paragraph()
    abnormal = [entry for entry in entries.values() if entry['context']['sourceStatus'] == '异常']
    normal = [entry for entry in entries.values() if entry['context']['sourceStatus'] == '正常']
    rows = []
    statements = []
    if abnormal:
        def distribution(entry):
            # Read the current adopted band; a missing snapshot provenance may
            # use only that exact observation's complete printed legend. This
            # selection does not recalculate or publish any patient's grade.
            from types import SimpleNamespace
            from modules.report.evaluation_builder import _source_legend_grade
            low, extreme = set(), set()
            for row in entry['rows']:
                fact = row.get('_summaryFact')
                if fact is None:
                    continue
                band = row.get('_summaryLabel') or _source_legend_grade(SimpleNamespace(
                    kind=fact.kind, source_status=fact.status, unit=fact.unit,
                    value=fact.value, reference_range=fact.reference_range))
                member = next((member for ids, member in entry['members'].items()
                    if fact.fact_id in ids), None)
                if band in LOW_FUNCTION_LEVELS:
                    low.add(fact.fact_id)
                if band == '极低':
                    extreme.add(fact.fact_id)
            return low, extreme
        for entry in abnormal:
            entry['weakIds'], entry['extremeIds'] = distribution(entry)
        weak = sorted([entry for entry in abnormal if entry['weakIds']], key=lambda entry: (
            not bool(entry['extremeIds']),
            -len(entry['weakIds']) / max(1, len(entry['members'])),
            -len(entry['extremeIds']), -len(entry['weakIds']), entry['domain']['name']))[:3]
        if weak:
            statements.append('较集中的功能弱项见于' + '、'.join(dict.fromkeys(
                entry['domain']['name'] for entry in weak)) + '，重点在各组实际下降的具体环节。')
            rows.extend(row for entry in weak for row in entry['rows'])
        # The original arrow owns direction; a display band such as
        # "正常-强" cannot hide an actual increase from the source picture.
        elevated = sorted([entry for entry in abnormal if entry['context'].get('directionFactIds')
            and any(member['direction'] == 'up' for member in entry['members'].values())], key=lambda entry: (
                -sum(member['direction'] == 'up' for member in entry['members'].values())
                    / max(1, len(entry['members'])), entry['domain']['name']))[:1]
        if elevated:
            statements.append('、'.join(dict.fromkeys(entry['domain']['name'] for entry in elevated))
                + '另有原报告升高方向标记。')
            rows.extend(row for entry in elevated for row in entry['rows'])
        if not weak and not elevated:
            selected = abnormal[:2]
            statements.append('功能异常涉及' + '、'.join(dict.fromkeys(
                entry['domain']['name'] for entry in selected)) + '。')
            rows.extend(row for entry in selected for row in entry['rows'])
    if normal:
        selected = normal[:2]
        statements.append('与此同时，' + '、'.join(dict.fromkeys(entry['context']['name'] for entry in selected))
            + '整体正常。')
        rows.extend(row for entry in selected for row in entry['rows'])
    return _Paragraph().add(''.join(statements), rows)


def source_nutrition_focus(raw, facts, *, projection=None):
    """A bound state summary remains possible without a medical mechanism.

    It identifies concentration and direction of actual functional findings;
    it does not infer a disease, deficiency, dietary dose or treatment effect.
    """
    from .evaluation_narrative import _summary_results, _bound_fact
    inv = _Inventory(raw, facts, projection or raw, _summary_results, _bound_fact)
    domains = [domain for domain in _function_domains(inv) if domain['code'] in
        {'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P8'}]
    paragraph = _native_pathway_summary(domains)
    if not paragraph.sentences:
        weak, normal = _domain_groups(domains)
        selected = weak[:2]
        if selected:
            paragraph.add('营养相关菌群功能的主要弱项集中于' + '、'.join(row['name'] for row in selected)
                + '，这些环节是本章优先关注的重点。', [row for domain in selected for row in domain['rows']])
        retained = normal[:1]
        if retained:
            paragraph.add('、'.join(row['name'] for row in retained) + '的已测功能保持正常。',
                [row for domain in retained for row in domain['rows']])
    sources = inv.sources(paragraph.rows)
    if not paragraph.sentences or not sources:
        return None
    return {'text': ''.join(dict.fromkeys(paragraph.sentences)), 'sources': sources,
        'evidenceRefs': [], 'availability': 'partial'}


def nutrition_focus_coverage_failures(text, assessment_context):
    """An intro must not turn one normal member into the complete picture.

    This checks distinct supplied direction families, not a quota of names,
    science sentences or diseases. The full measurements stay in their rows.
    """
    from .evaluation_template_input import NUTRITION_PANELS
    groups = [row for row in assessment_context if row.get('semanticType') == 'nativeNutritionState'
        and row.get('panel') in NUTRITION_PANELS and NUTRITION_PANELS[row['panel']][1]
            in {'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P8'}]
    errors = []
    for direction, pattern in (('down', r'下降|降低|减弱|弱项|偏弱|较弱|极低|短板'),
            ('up', r'升高|增加|增强|偏高|升降并存')):
        changed = [group for group in groups if any(member.get('direction') == direction
            for member in group.get('members', []))]
        if not changed:
            continue
        def addressed(group):
            names = [re.sub(r'合成|分解|代谢|能力|和|与|（.*?）', '', group['name']),
                *(member['name'] for member in group.get('members', []) if member.get('direction') == direction)]
            return any(re.search(pattern, clause) and any(name and name in clause for name in names)
                for clause in re.split(r'[。；;！？\n]', text or ''))
        if not any(addressed(group) for group in changed):
            errors.append('整体营养解读须回应实际' + ('下降' if direction == 'down' else '升高')
                + '方向，不能只以一个正常成员或建议方向替代已测变化；概括代表功能即可，不复抄全表。')
    return errors


def source_function_focus(raw, facts, *, projection=None):
    """Use actual functional differences when no scientific summary passed."""
    from .evaluation_narrative import _summary_results, _bound_fact
    inv = _Inventory(raw, facts, projection or raw, _summary_results, _bound_fact)
    paragraph = _native_pathway_summary(_function_domains(inv))
    if not paragraph.sentences or not paragraph.rows:
        return None
    return {'text': ''.join(dict.fromkeys(paragraph.sentences)), 'sources': inv.sources(paragraph.rows),
        'evidenceRefs': [], 'availability': 'partial'}


def _native_group_aliases(group):
    """Use the actual panel/member grouping's labels, including parentheses."""
    labels = []
    for name in (group.get('name'), *group.get('aliases', ())):
        if not isinstance(name, str) or not name.strip():
            continue
        labels.extend([name, re.sub(r'[（(][^）)]*[）)]', '', name),
            *re.findall(r'[（(]([^）)]+)[）)]', name)])
    labels.extend(re.sub(r'合成|分解|代谢|能力|功能|和|与', '', name) for name in list(labels))
    return tuple(dict.fromkeys(name.strip() for name in labels if len(name.strip()) >= 2))


def native_paragraph_favourable_failures(text, assessment_context, *, patient_state=False,
                                        subject_fact_ids=()):
    """A patient's source state remains the subject across commas/sentences.

    Plain biology is not a claim that this sample is favourable. This only
    checks patient-state judgements and resolves their preceding native group.
    """
    groups = [row for row in assessment_context if row.get('semanticType') == 'nativeNutritionState']
    scope = set(subject_fact_ids)
    scoped = [group for group in groups if scope.intersection(
        fid for member in group.get('members', []) for fid in member.get('factIds', ()))]
    active = [(scoped[0], [], True)] if len(scoped) == 1 else []
    mentioned = set()
    retained_ids = {fid for row in assessment_context if row.get('semanticType') == 'nutrientGroup'
        for member in row.get('members', []) if member.get('assessmentLevel') == '正常'
        and member.get('nativeDirection') not in {'up', 'down'}
        for fid in member.get('factIds', [])}
    def retained_subset(contexts):
        # A retained example is a bounded display-state assertion, not a
        # healthy whole-panel verdict. Resolve it from this actual source
        # inventory; never assume that an unlisted low item is normal.
        for group, selected, _ in contexts:
            if group.get('sourceStatus') == '正常' and not any(
                    member.get('direction') for member in group.get('members', [])):
                return True
            for member in selected or group.get('members', []):
                if member.get('direction'):
                    continue
                if (member.get('assessmentLevel') == '正常' or member.get('sourceStatus') == '正常'
                        or retained_ids.intersection(member.get('factIds', []))):
                    return True
        return False

    for clause in re.split(r'[，,。！？\n；;]', text or ''):
        matched = []
        for group in groups:
            named = any(alias in clause for alias in _native_group_aliases(group))
            members = [member for member in group.get('members', []) if member['name'] in clause]
            if named or members:
                matched.append((group, members, named))
        if matched:
            active = matched
            mentioned.update(group.get('panel') or group['name'] for group, _, _ in matched)
        anaphora = re.search(r'(?:本|该|此|这一)(?:[\u4e00-\u9fff]{0,6})'
            r'(?:组|项|通路|功能域|结果|功能)|这些结果|上述结果|组内', clause)
        ellipsis = re.match(r'\s*(?:属于|表现为|构成|是|可作为|保留|整体|总体)', clause)
        personal = patient_state or anaphora or re.search(r'本次|评分|已测', clause)
        favourable = re.search(r'保留|有利功能|有利因素|健康优势', clause)
        if not personal or not favourable or not (matched or anaphora or ellipsis):
            continue
        if re.search(r'不(?:是|属于|作为|视为)[^，,。；;]{0,12}$', clause[:favourable.start()]):
            continue
        if re.match(r'原(?:始)?(?:值|级|分级|标签|结果|方向)|检测值|记录', clause[favourable.end():]):
            continue
        broad = not matched and bool(re.search(r'整体|总体|格局', clause))
        if broad:
            # A report-wide closing clause has the scope of the described
            # inventory, not automatically its last comma's SCFA subject.
            covered = [group for group in groups if (group.get('panel') or group['name']) in mentioned]
            if not covered:
                covered = [group for group in groups if any(alias in (text or '')
                    for alias in _native_group_aliases(group))]
            if covered:
                active = [(group, [], True) for group in covered]
        subset = bool(re.search(r'(?:弱项|偏弱项|短板|下降项)[^。；;]{0,16}'
            r'(?:与|和|及)[^。；;]{0,6}(?:保留项|正常项)[^。；;]{0,8}并存|'
            r'(?:部分|一些|个别|少量)[^。；;]{0,8}(?:保留|正常)|'
            r'保留[^。；;]{0,8}(?:部分|一些|个别|少量)', clause))
        if subset and not re.search(r'整体(?:均|全部)?(?:为|属于|是)?[^。；;]{0,8}'
                r'(?:健康优势|有利功能|保留优势)', clause) and retained_subset(active):
            continue
        for group, members, whole in active:
            affected = [member for member in members if member.get('direction')]
            untouched = [member for member in members if not member.get('direction')]
            if affected or whole and group.get('sourceStatus') == '异常' and not untouched:
                return ['同源异常方向跨句仍属于同一功能结果；本次/这一结果的健康优势判断不能只依据强档，'
                    '不能在后句丢掉原始升降方向或把异常增强改成有利功能。']
    return []


def native_state_focus(groups):
    """Replace a rejected paragraph with complete verified source states."""
    if len(groups) != 1: return None
    group = groups[0]
    directions = {member.get('direction') for member in group.get('members', []) if member.get('direction')}
    if not directions or not group.get('sources'): return None
    direction = '增强' if directions == {'up'} else '减弱' if directions == {'down'} else '升高与降低并存'
    text = group['name'] + '呈' + direction + '方向，是本次需要关注的功能变化。'
    return {'text': text, 'sources': deepcopy(group['sources']), 'evidenceRefs': [], 'availability': 'partial'}


def native_distribution_focus_failures(text, assessment_context, *, subject_fact_ids=()):
    """A retained example does not describe the extent of a whole source panel."""
    own = set(subject_fact_ids)
    groups = [group for group in assessment_context if group.get('semanticType') == 'nativeNutritionState']
    scoped = [group for group in groups if own.intersection(
        fid for member in group.get('members', ()) for fid in member.get('factIds', ()))]
    for group in groups:
        members = group.get('members') or []
        directed = [member for member in members if member.get('direction') in {'up', 'down'}]
        if not members or len(directed) * 2 <= len(members):
            continue
        aliases = _native_group_aliases(group)
        local = [clause for clause in re.split(r'[。！？\n；;]', text or '')
            if any(alias in clause for alias in aliases) or len(scoped) == 1 and scoped[0] is group
            and re.search(r'本组|该组|本通路|本功能域|组内', clause)]
        if not local:
            continue
        scoped_text = '。'.join(local)
        limited = re.search(r'(?:短板|异常|下降|减弱|偏低)[^。；;]{0,18}'
            r'(?:仅|只是|局部|部分|少数)|(?:仅|只是|局部|少数)[^。；;]{0,18}'
            r'(?:短板|异常|下降|减弱|偏低)', scoped_text)
        if limited and not re.search(r'分档|档位|评分|分级', limited[0]):
            # The original direction and the display band remain different
            # observations. Explicitly naming a graded subset is legitimate;
            # minimising the whole native pattern from one example is not.
            if not re.search(r'(?:多项|多种|大多|多数|广泛|大部分|\d{2,}项)'
                    r'[^。；;]{0,20}(?:下降|降低|减弱|异常|升高|增强)', scoped_text):
                return ['本组原生异常方向涉及过半已测项目，不能用一个保留项把整组变化概括为局部少数短板；'
                    '用一句话如实概括本功能域的整体变化，再选少量实际弱项说明健康意义；'
                    '不要求列出全部成员、逐项方向或正常异常数量。能力评分的局部强弱与原始升降分别保持真实，'
                    '不把正常评分写成整组没有变化，也不因原始下降把所有评分重判为弱。']
    return []


def nutrition_judgment_options(raw, facts, allowed_fact_ids):
    """Literal source-state choices for a last wording repair, never scores."""
    from .evaluation_template_input import NUTRITION_PANELS
    allowed = set(allowed_fact_ids)
    observations = []
    for group in native_function_source_context(raw, facts):
        if NUTRITION_PANELS[group['panel']][1] not in {'P1', 'P2', 'P3', 'P4', 'P5', 'P6'}:
            continue
        member_ids = {fid for member in group['members'] for fid in member['factIds']}
        if not member_ids or not member_ids <= allowed:
            continue
        up = sum(member.get('direction') == 'up' for member in group['members'])
        down = sum(member.get('direction') == 'down' for member in group['members'])
        total = len(group['members'])
        if up and down:
            state = '升降并存'
        elif up:
            state = '升高' if up == total else '多项升高' if up * 2 > total else '部分项目升高'
        elif down:
            state = '下降' if down == total else '多项下降' if down * 2 > total else '部分项目下降'
        elif group.get('sourceStatus') == '正常':
            state = '正常'
        else:
            continue
        observations.append((group.get('sourceStatus') == '正常', group['name'] + state))
    # Keep each source statement complete. Length limits select paragraphs,
    # rather than slicing away a subject, a qualifier or an abnormal direction.
    clauses = []
    for _, clause in sorted(observations, key=lambda item: item[0]):
        candidate = '；'.join([*clauses, clause]) + '。'
        if len(candidate) <= 120:
            clauses.append(clause)
    return ['；'.join(clauses) + '。'] if clauses else []


def _harmful_function_difference(rows):
    """A harmful-generation score is a state, never a replenishment goal."""
    return _function_difference(rows)


def _is_source_detection(fact):
    return bool(fact and _key(fact.status) in {'检出', '阳性', 'detected', 'positive'}
        and not re.match(r'未检出|未能检出|阴性|not\s+detected|negative', fact.value, re.I))


def pathway_detection_coverage_failures(path, text, observations, facts):
    """Require native virulence signals before an accepted state replaces them.

    Only the explicit source semantic group supplies required facts. A fungal,
    viral, clinical or other detected observation does not become a virulence
    signal merely because it shares the same result word.
    """
    path = tuple(path)
    if path[:2] != ('functionalPathways', 'pathways') or path[-1:] != ('healthMeaning',):
        return []
    required = {}
    for row in observations:
        if row.get('semanticType') != 'virulence':
            continue
        for fact_id in row.get('factIds', []):
            fact = facts.get(fact_id)
            if (fact and fact.kind in {'context', 'function'} and _key(fact.name) == _key(row.get('name'))
                    and _is_source_detection(fact)):
                required[fact_id] = fact
    statements = []
    for sentence in re.split(r'[。！？\n]', text or ''):
        if (re.search(r'研究|文献|体外|培养|队列', sentence)
                and not re.search(r'本次|本组|原报告|来源(?:记录|标记)|已有', sentence)):
            continue
        # A source sentence may contrast detected groups with explicitly
        # negative groups. A latter negative clause does not negate the
        # separately stated positive subjects in the preceding clause.
        for line in re.split(r'[，,；;]', sentence):
            if (not re.search(r'检出|阳性|\bdetected\b|\bpositive\b', line, re.I)
                or re.search(r'未检出|未能检出|没有检出|阴性|not\s+detected|negative', line, re.I)
                or re.search(r'研究|文献|体外|培养|队列', line)
                    and not re.search(r'本次|本组|原报告|来源(?:记录|标记)|已有', line)):
                continue
            statements.append(_key(line))
    return ['主要毒力检出信号未在来源状态中保留：' + fact.name for fact in required.values()
        if not any(_key(fact.name) in line for line in statements)]


def _virulence_rows(inv, pathway, *, detected_only=True):
    """Use exact source-item category and observation binding, never a name guess."""
    originals = [row for row in _at(inv.raw, 'appendices', 'sourceItems', default=[])
        if row.get('category') == 'virulence']
    result = []
    for row in pathway.get('relatedMetrics', []):
        fact = inv.bound_fact(row, inv.facts, 'function')
        if fact is None or detected_only and not _is_source_detection(fact):
            continue
        if not any(all(original.get(key) == row.get(key) for key in
                ('name', 'value', 'unit', 'sourceStatus'))
                and (bound := inv.bound_fact(original, inv.facts, 'function')) is not None
                and bound.fact_id == fact.fact_id for original in originals):
            continue
        result.append({**row, '_summaryFactIds': {fact.fact_id}, '_detectionLabel': fact.status})
    return result


def _virulence_signal_text(rows):
    if not rows:
        return None
    groups = {}
    for row in rows:
        groups.setdefault(row.get('_detectionLabel', row.get('sourceStatus')), []).append(row['name'])
    return ''.join('原报告基因标记条目中，' + '、'.join('“' + name + '”' for name in dict.fromkeys(names))
        + '标记为' + label + '。'
        for label, names in groups.items() if label)


def _virulence_function_comparison(rows):
    return _function_difference(rows)


def _set_function_text(owner, text, rows, inv):
    """Explain actual results; source advice cannot stand in for interpretation."""
    if not isinstance(owner, dict):
        return
    if inv.reviewed_result(owner):
        return  # The accepted whole paragraph is the single publication unit.
    owner.update(text=text or None, evidenceRefs=[],
        sources=inv.sources(rows) if text else [], availability='partial' if text else 'unavailable')



def _native_direction_covered(text, context, member):
    """Recognize an actual source direction, never a relative score ranking."""
    from modules.report.evaluation_meanings import _ALIASES

    direction = member.get('direction')
    if not direction:
        return True
    pattern = r'升高|上升|偏高|向上|↑' if direction == 'up' else r'降低|下降|偏低|向下|↓'
    normalize = lambda value: re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value or '')).casefold()
    aliases = [member['name'], *_ALIASES.get(_identity(member['name']), ())]
    mentions = lambda clause, names: any(normalize(name) in normalize(clause) for name in names)

    def affirmative(clause):
        return bool(re.search(pattern, re.sub(r'(?:未(?:见|发现|列为|列入)?|没有|无|不)(?:明显)?'
            r'(?:升高|上升|偏高|向上|↑|降低|下降|偏低|向下|↓)', '', clause)))

    for sentence in re.split(r'[。！？\n]', text or ''):
        if (re.search(r'研究|文献|试验|体外|动物|培养|队列', sentence)
                and not re.search(r'本次|本组|本通路|该组|整组|检测|原报告|来源|原清单', sentence)):
            continue
        clauses = re.split(r'[,，;；]', sentence)
        for index, clause in enumerate(clauses):
            if not mentions(clause, aliases):
                continue
            if affirmative(clause):
                return True
            # A sole named object's following pronoun clause retains the same
            # subject. A different named measurement cannot supply its state.
            following = clauses[index + 1] if index + 1 < len(clauses) else ''
            if (re.match(r'\s*(?:仍|且|但|同时|并)', following) and affirmative(following)
                    and not any(other['name'] in following for other in context['members']
                        if other['name'] != member['name'])):
                return True
        if not affirmative(sentence):
            continue
        matching = [item for item in context['members'] if item.get('direction') == direction]
        others = [item for item in context['members'] if not item.get('direction')]
        uniform = len({item['direction'] for item in context['members'] if item.get('direction')}) == 1
        if not uniform and any(re.search(r'(?:异常清单|异常方向|异常项(?:目)?)[^。]{0,16}'
                r'(?:包含|包括|兼有|涉及|呈现|存在|有)', clause)
                and not re.search(r'不(?:含|包括|包含|涉及|存在|呈现)|未(?:见|含|包括|包含)|没有', clause)
                and re.search(r'升高|上升|偏高|向上|↑', clause)
                and re.search(r'降低|下降|偏低|向下|↓', clause) for clause in clauses):
            return True
        # These collective forms refer to the declared native abnormal list,
        # or to all actual members when every member has the same direction.
        if uniform and any(affirmative(clause) and re.search(
                r'(?:异常(?:项目?|清单|方向)|方向清单)[^。]{0,24}(?:均|全部|一致)', clause)
                for clause in clauses):
            return True
        if uniform and len(matching) == len(context['members']) and any(affirmative(clause) and re.search(
                r'(?:本组|该组|三项|3项|各单项|各项|全部|三者|三种)[^。]{0,24}(?:均|一致)', clause)
                for clause in clauses):
            return True
        # A majority statement can cover a panel's abnormal list only when
        # its true unlisted members are explicitly identified as exceptions.
        if (uniform and len(matching) > len(context['members']) / 2 and others
                and re.search(r'方向清单|原清单|来源|原报告', sentence)
                and re.search(r'多数|大多数', sentence)
                and all(mentions(sentence, [item['name']]) and re.search(
                    re.escape(item['name']) + r'[^，,。；;]{0,14}未(?:列入|列为|见)', sentence)
                    for item in others)):
            return True
    return False


def _native_status_covered(text, context):
    status = context.get('sourceStatus')
    if not status:
        return True
    for sentence in re.split(r'[。！？\n]', text or ''):
        if re.search(r'(?:未见|没有|无)(?:明显)?' + re.escape(status), sentence):
            continue
        if re.search(r'(?:整体|整组|本组|该组|来源|原报告|检测|栏目)[^。]{0,30}'
                + re.escape(status), sentence):
            return True
    return False


def reconcile_native_function_states(raw, facts, *, projection=None):
    """Restore missing literal native states after all trusted prose restores.

    This final local reconciliation appends at most one finite source sentence
    per native panel. It preserves reviewed medical prose and its citations;
    contradictory judgments must still be rejected by the narrative guard.
    No measurement, grade, A/B source row or overview paragraph is changed.
    """
    from modules.report.evaluation_narrative import _bound_fact, _summary_results

    if not isinstance(raw, dict) or not facts:
        return raw
    inv = _Inventory(raw, facts, projection, _summary_results, _bound_fact)

    def reconcile(owner, key, rows):
        if not isinstance(owner, dict):
            return
        actual = inv.functions(rows)
        ids = {fid for row in actual for fid in row['_summaryFactIds']}
        for original in inv.native_function_context:
            members = [member for member in original['members'] if ids.intersection(member['factIds'])]
            if not members:
                continue
            context = {**original, 'members': members}
            existing = owner.get(key) or ''
            missing = [member for member in members
                if member.get('direction') and not _native_direction_covered(existing, context, member)]
            parts = []
            if not _native_status_covered(existing, context):
                parts.append(context['name'] + '整体为' + str(context['sourceStatus']))
            if missing:
                directions = {member['direction'] for member in missing}
                if len(missing) <= 3:
                    parts.append('检测异常方向为' + '、'.join(member['name'] + (
                        '↑' if member['direction'] == 'up' else '↓') for member in missing))
                elif len({member['direction'] for member in members if member.get('direction')}) > 1:
                    # A previously stated up item does not turn the remaining
                    # down items into a uniformly down whole native panel.
                    parts.append('异常项目包含升高及降低方向')
                elif len(directions) == 1:
                    parts.append('异常项目均为' + (
                        '升高（↑）' if directions == {'up'} else '降低（↓）') + '方向')
                else:
                    parts.append('异常项目包含升高与降低方向')
            if not existing.strip() and context['panel'] == 'yingyang2_ajs2':
                weak = [row for row in actual if row.get('_summaryLabel') == '弱'
                    and any(set(member['factIds']) & row['_summaryFactIds'] for member in members)]
                if weak:
                    names = list(dict.fromkeys(row['name'] for row in weak))
                    parts.append('本域可优先关注' + '、'.join(names[:2])
                        + ('等' if len(names) > 2 else '') + '评分为弱的项目')
            if not parts:
                continue
            sentence = '；'.join(parts) + '。'
            owner[key] = ' '.join(part for part in (existing.strip(), sentence) if part)
            for source in inv.sources(actual):
                if source not in owner.setdefault('sources', []):
                    owner['sources'].append(source)
            owner['availability'] = 'available' if owner.get('availability') == 'available' else 'partial'

    micro = raw.get('microbiomeAssessment') or {}
    reconcile(micro.get('scfaInterpretation'), 'text', inv.rows('microbiomeAssessment', 'shortChainFattyAcids'))
    nutrition = raw.get('nutritionAssessment') or {}
    reconcile(nutrition.get('neurotransmitterInterpretation'), 'text', inv.rows('nutritionAssessment', 'neurotransmitters'))
    for collection in ('detailedMetabolism', 'vitaminsAndMinerals'):
        projected = _at(inv.projection, 'nutritionAssessment', collection, default=[])
        for group in nutrition.get(collection) or []:
            candidates = [item for item in projected if item.get('code') == group.get('code')]
            rows = inv.bound_rows(group.get('items') or [], candidates[0].get('items') or [] if len(candidates) == 1 else [])
            reconcile(group.get('explanation'), 'text', rows)
    domains = {domain['code']:domain for domain in _function_domains(inv)}
    for pathway in (raw.get('functionalPathways') or {}).get('pathways') or []:
        if domain := domains.get(pathway.get('code')):
            reconcile(pathway, 'healthMeaning', domain['rows'])
    return raw


def _refine_function_groups(inv):
    nutrition = inv.raw['nutritionAssessment']
    for collection in ('detailedMetabolism', 'vitaminsAndMinerals'):
        projected = _at(inv.projection, 'nutritionAssessment', collection, default=[])
        for group in nutrition.get(collection, []):
            matches = [candidate for candidate in projected if candidate.get('code') == group.get('code')
                and candidate.get('name') == group.get('name')]
            candidates = matches[0].get('items', []) if len(matches) == 1 else []
            rows = inv.functions(inv.bound_rows(group.get('items', []), candidates))
            _set_function_text(group.get('explanation'), _function_difference(rows), rows, inv)


def _refine_pathway_meanings(inv, domains, *, resistance=()):
    """Preserve reviewed interpretations or project verified finite differences."""
    by_code = {domain['code']: domain for domain in domains}
    for pathway in inv.raw['functionalPathways'].get('pathways', []):
        domain = by_code.get(pathway.get('code'))
        signals = _virulence_rows(inv, pathway, detected_only=False) if pathway.get('code') == 'P9' else []
        signal_text = _virulence_signal_text(signals)
        if inv.reviewed_result(pathway, 'healthMeaning'):
            context = [{'semanticType': 'virulence', 'name': row['name'],
                'factIds': list(row['_summaryFactIds'])} for row in signals]
            if pathway_detection_coverage_failures(('functionalPathways', 'pathways', 'healthMeaning'),
                    pathway['healthMeaning'], context, inv.facts):
                # Preserve the reviewed paragraph whole. This separate literal
                # source sentence gains no literature approval by proximity.
                pathway['healthMeaning'] = signal_text + '\n' + pathway['healthMeaning']
                for source in inv.sources(signals):
                    if source not in pathway['sources']:
                        pathway['sources'].append(source)
            continue
        if not domain:
            pathway.update(healthMeaning=None, evidenceRefs=[])
            continue
        rows = domain['rows']
        resistance_rows = []
        if pathway.get('code') == 'P10' and resistance:
            resistance_ids = {fid for row in resistance for fid in row['_summaryFactIds']}
            resistance_rows = [row for row in rows if row['_summaryFactIds'] & resistance_ids]
            rows = [row for row in rows if not row['_summaryFactIds'] & resistance_ids]
        text = (_harmful_function_difference(rows) if pathway.get('code') == 'P9'
            else _function_difference(rows))
        if signals:
            virulence_ids = {fact_id for row in _virulence_rows(inv, pathway, detected_only=False)
                for fact_id in row['_summaryFactIds']}
            functional = [row for row in rows if not row['_summaryFactIds'] & virulence_ids]
            comparison = _virulence_function_comparison(functional)
            text = signal_text + (comparison or '')
        if resistance_rows:
            text = '耐药基因：' + _states(resistance_rows) + '。' + (
                '其他功能预测：' + text if text else '')
        pathway['healthMeaning'] = text or None
        pathway['evidenceRefs'] = []
        if any(row.get('_summaryNativeContexts') for row in rows):
            for source in inv.sources(rows):
                if source not in pathway.setdefault('sources', []):
                    pathway['sources'].append(source)


def refine_evaluation_summaries(raw, facts, *, projection=None, reviewed_signals=False, reviewed_blocks=None):
    """Mutate and return only supported text summaries; leave all values intact."""
    from modules.report.evaluation_narrative import (
        _bound_fact, _editorial_focus, _summary_results, _nutrition_group_context,
        _nutrient_group_statement_failures,
    )
    from modules.report.evaluation_content import content_rule_failures

    if not isinstance(raw, dict) or not facts:
        return raw
    inv = _Inventory(raw, facts, projection, _summary_results, _bound_fact, reviewed_blocks=reviewed_blocks)
    # Minimal/empty projections do not acquire prose simply because facts exist.
    if not all(isinstance(raw.get(key), dict) for key in
            ('healthOverview', 'microbiomeAssessment', 'nutritionAssessment',
             'functionalPathways', 'medicalAssessment', 'comprehensiveConclusion')):
        return raw
    focus = _editorial_focus(raw, facts) if raw['healthOverview'].get('keyFindings') else []
    clinical = _clinical(inv, focus)
    reported = _reported_background(inv)
    trigger = raw['medicalAssessment'].get('triggerConclusion')
    if isinstance(trigger, dict):
        background = (_behavior_picture(inv) if trigger.get('text') and trigger.get('evidenceRefs')
            else _trigger_background(inv))
        if background.sentences:
            _put(trigger, background, inv,
                discard_inapplicable=not bool(trigger.get('text') and trigger.get('evidenceRefs')))
        else:
            trigger.update(text=None, sources=[], evidenceRefs=[], availability='unavailable')
    structure = _structure(inv)
    domains = _function_domains(inv)
    amr = inv.functions(inv.rows('microbiomeAssessment', 'antimicrobialResistance'))
    _refine_function_groups(inv)
    _refine_pathway_meanings(inv, domains, resistance=amr)
    low_domains, normal_domains = _domain_groups(domains)
    picture = _functional_picture(domains)
    native_picture = _native_function_picture(domains)
    overview = raw['healthOverview']
    broad = _Paragraph()
    unhealthy_structure = [row for row in structure.rows if row.get('code') == 'gmwi2'
        and row.get('_summaryLabel') not in {None, '正常', '健康', '健康倾向'}]
    preserved_structure = [row for row in structure.rows if row.get('code') in {'speciesCount', 'diversity'}
        and row.get('_summaryLabel') is not None]
    clinical_priority = _clinical_priority(clinical.rows)
    if unhealthy_structure:
        rows = [*unhealthy_structure, *preserved_structure]
        broad.add('微生态方面，' + _structure_states(rows) + '。', rows)
    elif structure.sentences:
        broad.add('菌群结构的已测指标分级为' + _grades(structure.rows) + '，具体偏离与保留优势应分别关注。', structure.rows)
    if low_domains:
        names = '、'.join(group['name'] for group in low_domains[:3])
        normal_names = '、'.join(group['name'] for group in normal_domains[:2])
        broad.add('功能差异集中于' + names + '中的弱项'
            + ('，而' + normal_names + '仍有保留，呈现局部功能差异而非各功能一致减弱。' if normal_names else '，应优先关注这些具体弱项。'), picture.rows)
    elif picture.sentences:
        broad.add('已测菌群合成与代谢功能评分保留正常或强分级。', picture.rows)
    broad.sentences.extend(native_picture.sentences)
    broad.rows.extend(native_picture.rows)
    if clinical_priority:
        broad.add(clinical_priority, clinical.rows)
    has_overall_order = False  # Finite source facts cannot author a global ranking.
    if reported.sentences:
        # Existing clinical findings and adopted source history coexist. A
        # clinical input must not silently remove the latter from synthesis.
        if not clinical.rows:
            broad.sentences = [*reported.sentences, *broad.sentences]
        elif not has_overall_order:
            broad.sentences.append('原报告记载的病史也作为持续随访背景。')
        broad.rows.extend(reported.rows)
    if len(broad.sentences) > 2:
        broad.sentences = [broad.sentences[0], ''.join(broad.sentences[1:])]
    _put(overview.get('overallSummary'), broad, inv)
    summaries = overview.get('summaries', {})
    medical_owner = summaries.get('medical')
    _put(medical_owner, _medical_module(inv), inv, discard_inapplicable=bool(
        content_rule_failures((medical_owner or {}).get('text'),
            path=('healthOverview', 'summaries', 'medical', 'text'), facts=facts)))
    brief_structure = _Paragraph()
    if structure.rows:
        deviations = [row for row in structure.rows if _structure_deviation(row)]
        preserved = [row for row in structure.rows if row.get('code') in {'speciesCount','diversity'}
            and row.get('_summaryLabel') is not None]
        if unhealthy_structure and preserved:
            selected = [*unhealthy_structure[:1], *preserved]
            brief_structure.add(_structure_states(selected) + '。', selected)
        elif deviations:
            brief_structure.add('菌群结构的关注集中在' + '、'.join(row['name'] + '（' + row['_summaryLabel'] + '）'
                for row in deviations[:3]) + '，应优先观察这些实际偏离的结构维度。', deviations[:3])
        else:
            brief_structure.add('菌群结构的已测维度呈现' + _grades(structure.rows)
                + '的表现，后续关注以实际偏离的结构维度为重点。', structure.rows)
    health_deviations = [row for row in inv.functions(inv.rows('microbiomeAssessment', 'healthMetrics'))
        if row.get('code') in {'gutImmunity', 'antiInflammatoryCapacity', 'intestinalBarrier'}
        and (_low(row) or _structure_deviation(row))]
    if health_deviations:
        brief_structure.add('肠道功能还需关注' + '、'.join(row['name'] + '（' + row['_summaryLabel'] + '）'
            for row in health_deviations[:2]) + '，与结构偏离一并作为后续关注方向。', health_deviations[:2])
    micro_owner = summaries.get('microbiome')
    _put(micro_owner, brief_structure, inv, discard_inapplicable=bool(content_rule_failures(
        (micro_owner or {}).get('text'), path=('healthOverview','summaries','microbiome','text'), facts=facts)))
    brief_nutrition = _Paragraph()
    if low_domains:
        low_rows = [r for g in low_domains[:3] for r in g['rows'] if _low(r)]
        brief_nutrition.add('营养与代谢功能的主要短板集中在' + '、'.join(g['name'] for g in low_domains[:3])
            + '中已列的低项，后续关注应围绕这些具体合成与代谢环节。', low_rows)
    if normal_domains:
        normal_rows = [r for g in normal_domains[:3] for r in g['rows']]
        brief_nutrition.add('同时，' + '、'.join(g['name'] for g in normal_domains[:3])
            + '的已提供项目保留' + _grades(normal_rows) + '分级，宜同时观察低项变化与保留功能的稳定性。', normal_rows)
    brief_nutrition.sentences.extend(native_picture.sentences)
    brief_nutrition.rows.extend(native_picture.rows)
    nutrition_owner = summaries.get('nutrition')
    _put(nutrition_owner, brief_nutrition, inv, discard_inapplicable=bool(content_rule_failures(
        (nutrition_owner or {}).get('text'), path=('healthOverview','summaries','nutrition','text'), facts=facts)))
    micro = raw['microbiomeAssessment']
    _put(micro.get('structureInterpretation'), structure, inv)
    scfa = inv.functions(inv.rows('microbiomeAssessment', 'shortChainFattyAcids'))
    _set_function_text(micro.get('scfaInterpretation'), _function_difference(scfa), scfa, inv)
    signals = _microbial_signals(inv)
    owner = micro.get('keyMicrobialSignals', {})
    # This slot explains the measured signal inventory. Literature belongs in
    # its reviewed clinical/functional slot, not a repeated research catalogue.
    # With no bound representatives _put leaves the original block unchanged.
    _put(owner, signals, inv, discard_inapplicable=not reviewed_signals)
    if picture.sentences:
        nutrition = _Paragraph()
        representatives = [next(row for row in domain['rows'] if _low(row)) for domain in low_domains[:3]]
        if representatives:
            nutrition.add('营养相关功能减弱主要涉及' + '、'.join(domain['name'] for domain in low_domains[:3])
                + '。', [row for domain in low_domains[:3] for row in domain['rows'] if _low(row)])
        elif normal_domains:
            nutrition.add('已测菌群营养合成与代谢功能评分保留正常或较强分级。',
                [row for domain in normal_domains for row in domain['rows']])
        nutrition.sentences.extend(native_picture.sentences)
        nutrition.rows.extend(native_picture.rows)
        _put(raw['nutritionAssessment'].get('nutritionInterpretation'), nutrition, inv)
    neuro = next((g for g in domains if g['code'] == 'P6'), None)
    neuro_rows = neuro['rows'] if neuro else []
    _set_function_text(raw['nutritionAssessment'].get('neurotransmitterInterpretation'),
        _function_difference(neuro_rows), neuro_rows, inv)
    amino_rows = inv.functions(inv.rows('nutritionAssessment', 'aminoAcidsAndMetabolites'))
    _set_function_text(raw['nutritionAssessment'].get('aminoMetaboliteInterpretation'),
        _function_difference(amino_rows), amino_rows, inv)
    native_pathway_summary = _native_pathway_summary(domains)
    pathway_summary = native_pathway_summary if native_pathway_summary.sentences else _Paragraph()
    # A chapter summary compares a few actual domains. Full item inventories
    # remain in their tables, including when a model-authored roster is rejected.
    retained_domains = normal_domains[:2]
    retained_rows = [row for domain in retained_domains for row in domain['rows']]
    if not native_pathway_summary.sentences and low_domains and normal_domains:
        pathway_summary.add('功能通路差异主要表现为' + '、'.join(g['name'] for g in low_domains[:3])
            + '中弱项较集中，而' + '、'.join(g['name'] for g in retained_domains)
            + '的已提供项目保留' + _grades(retained_rows)
            + '分级；后续关注以弱项集中的功能环节为重点，同时观察保留功能的稳定性。', picture.rows)
    elif not native_pathway_summary.sentences and low_domains:
        pathway_summary.add('功能关注点主要位于' + '、'.join(g['name'] for g in low_domains[:3])
            + '中已提供的低项。', picture.rows)
    elif not native_pathway_summary.sentences and normal_domains:
        pathway_summary.add('已提供项目中，' + '、'.join(g['name'] for g in retained_domains)
            + '保留' + _grades(retained_rows) + '分级，可作为后续观察功能稳定性的保留优势。', picture.rows)
    p10 = next((g for g in domains if g['code'] == 'P10'), None)
    if p10 and amr:
        amr_ids = {fid for row in amr for fid in row['_summaryFactIds']}
        other = [row for row in p10['rows'] if not row['_summaryFactIds'] & amr_ids]
        if native_pathway_summary.sentences and other:
            pathway_summary.add('毒性物质分解与耐药类别评估属于不同维度，各自按本节已测状态理解。', [*amr, *other])
        elif native_pathway_summary.sentences:
            pass  # The native chapter summary is not a second AMR inventory.
        elif other:
            pathway_summary.add('防御与耐受通路需分开理解：耐药条目为' + _states(amr)
                + '；其他功能预测为' + _states(other) + '。', [*amr, *other])
        else:
            pathway_summary.add('防御与耐受通路中，耐药条目为' + _states(amr) + '。', amr)
    # Earlier aggregation can contain an accepted model's unsupported source
    # label even after the per-pathway slot has been cleared. Apply the same
    # admission rule to the final summary, including the empty-result case.
    summary_rows = pathway_summary.rows or [row for domain in domains for row in domain['rows']]
    if native_pathway_summary.sentences:
        _put(raw['functionalPathways'].get('summary'), pathway_summary, inv)
    else:
        _set_function_text(raw['functionalPathways'].get('summary'),
            '\n'.join(dict.fromkeys(pathway_summary.sentences)) or None, summary_rows, inv)
    conclusion = _Paragraph()
    if reported.sentences:
        history = '、'.join(dict.fromkeys(row['_historyValue'].rstrip('。') for row in reported.rows))
        conclusion.add('原报告记载的' + history + '也应纳入持续随访，并结合既有诊疗安排观察相关状态。', reported.rows)
    if unhealthy_structure:
        weak_health = [row for row in inv.functions(inv.rows('microbiomeAssessment', 'healthMetrics'))
            if row.get('code') in {'gutImmunity', 'antiInflammatoryCapacity', 'intestinalBarrier'} and _low(row)]
        conclusion.add('菌群整体组成呈' + _grades(unhealthy_structure)
            + ('，同时' + '、'.join(row['name'] + '为' + row['_summaryLabel'] for row in weak_health[:2])
                if weak_health else '')
            + ('；营养与代谢功能应优先关注' + '、'.join(g['name'] for g in low_domains[:3])
                + '中已列的弱项。' if low_domains else '，组成状态是后续微生态关注重点。'),
            [*structure.rows, *weak_health, *picture.rows])
    if clinical.rows:
        clinical_action = _clinical_priority(clinical.rows, conclusion=True)
        conclusion.sentences.extend([clinical_action] if clinical_action else clinical.sentences)
        conclusion.rows.extend(clinical.rows)
    symptoms = _reported_symptom_rows(inv)
    if symptoms:
        phrase = '当前还应关注问卷自述的' + '、'.join(dict.fromkeys(
            row['_symptomPhrase'] for row in symptoms)) + '，将实际不适的变化作为后续观察重点。'
        conclusion.sentences.insert(1 if clinical.rows else 0, phrase)
        conclusion.rows.extend(symptoms)
    high = [row for row in _risk_observations(inv) if row['_summaryLabel'] == '高风险']
    if high:
        conclusion.add('疾病风险需重点关注' + '、'.join(row['name'] for row in high[:3]) + '。', high[:3])
    if picture.rows and not (unhealthy_structure and low_domains):
        conclusion.add('功能关注集中于' + '、'.join(g['name'] for g in low_domains[:3])
            + '的具体低项。' if low_domains else '已提供的功能项目保留正常或强分级。', picture.rows)
    conclusion.sentences.extend(native_picture.sentences)
    conclusion.rows.extend(native_picture.rows)
    conclusion_owner = raw['comprehensiveConclusion'].get('overallConclusion')
    behavior = _behavior_picture(inv, concise=True, actionable_only=True)
    conclusion.sentences.extend(behavior.sentences)
    conclusion.rows.extend(behavior.rows)
    _put(conclusion_owner, conclusion, inv)
    favorable = _Paragraph()
    for domain in domains:
        if domain['code'] not in {'P3', 'P4'}:
            continue
        rows = inv.functions(domain['rows'], favorable=True)
        if rows:
            names = '、'.join(row['name'] for row in rows[:3])
            favorable.add(domain['name'] + '相关菌群评分中，' + names
                + ('等' if len(rows) > 3 else '') + f'{len(rows)}项保留' + _grades(rows) + '分级。', rows)
    health = inv.functions(inv.rows('microbiomeAssessment', 'healthMetrics'), favorable=True)
    if health:
        scoped_names = {'antiInflammatoryCapacity': '菌群抗炎相关评分', 'gutImmunity': '菌群免疫相关评分'}
        favorable.add('、'.join(scoped_names.get(row.get('code'), '原报告“' + row['name'] + '”指标')
            for row in health[:2]) + '保留' + _grades(health[:2]) + '分级。', health[:2])
    clinical_normal = _important_normal_records(inv)
    if clinical_normal:
        favorable.add('既往' + '、'.join(row['name'] for row in clinical_normal)
            + '检查保留原有正常结论，可作为对应检查时点的对照信息。', clinical_normal)
    favorable_owner = raw['comprehensiveConclusion'].get('favorableFactors')
    favorable_text = (favorable_owner or {}).get('text')
    invalid_favorable = content_rule_failures(favorable_text,
        path=('comprehensiveConclusion', 'favorableFactors', 'text'), facts=facts)
    if favorable_text:
        invalid_favorable.extend(_nutrient_group_statement_failures(favorable_text,
            list(facts.values()), tuple(_nutrition_group_context(raw, facts))))
        favorable_source_ids = {source.get('recordId') for source in favorable_owner.get('sources') or []}
        contradicted = [row for domain in domains for row in domain['rows']
            if row.get('_summaryNativeDeviation') and favorable_source_ids & row['_summaryFactIds']
            and any(name in favorable_text for name in (row['name'], *(
                context['name'] for context in row.get('_summaryNativeContexts') or [])))]
        if contradicted:
            invalid_favorable.append('来源明确偏离项不能作为有利因素')
    if invalid_favorable and not favorable.sentences and isinstance(favorable_owner, dict):
        favorable_owner.update(text=None, sources=[], evidenceRefs=[], availability='unavailable')
    _put(favorable_owner, favorable, inv, discard_inapplicable=bool(invalid_favorable))
    return raw
