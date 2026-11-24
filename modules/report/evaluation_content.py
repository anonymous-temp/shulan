"""Internal content responsibilities for the user-facing assessment report.

Roles route existing facts and original literature to the right explanation.
They do not add source observations or approve a medical statement.
"""
from __future__ import annotations

from copy import deepcopy
import re

from modules.report.evaluation_scores import is_assessment_method_clause
from modules.report.evaluation_prose_roles import author_responsibility, measurement_scope_text_allowed


PATIENT_LANGUAGE_RESPONSIBILITY = (
    '面向用户直接、自然地表达，统一称“您”或省略主语，不称“本人”，不切换第一人称。'
    '每段先说明本主题实际状态或问题；风险、症状、诱因与改善只在本节点有真实适用依据时采用，不强求每段齐备。'
    '指标与原等级只作判断依据，不以“发现A、所以A异常、建议关注A”代替分析；'
    '不使用“已列低项”“代表方向”“同类评估”“保留记录”“来源列示”“原标计数”“明确非零”等程序或编写过程措辞。'
    '同一段落与表格的解释不换词重复；新增一句应提供新的健康意义或行动信息。'
    '诱因只采用实际提供的行为与病史；未证明的因果关系按相关因素表达，不替用户补充饮食属性或症状。'
)


HEALTH_MEANING_RESPONSIBILITY = (
    '本栏的明细已在details及原结果列完整保留，解释不再列一遍菌名、基因名、数值、原级或参考范围。'
    '先用少量实际锚点解释本组状态或真实分布；有适用原文时另句说明同对象生理作用或健康影响。'
    '由独立语义审核区分事实有效与解释完成：真实原状态、成员名单、参考背景或检测对象定义可按真实观察保留，'
    '源内分布标observation_only，只完成真实结果解释，不冒充人体健康影响；定义或测量边界不能单独完成本栏。'
    '没有同对象适用科学原文时，可用已核实的状态或分布完成本栏，不补造人体机制，也不因缺引文删除真实状态。'
    '基因项目直接称本项基因标记；必要测量边界须与实际对象相符，不以否定句代替结果解释。'
    '源内观察与有引文科学句分别审核；新增科学作用仍须独立通过原文及意义职责双审，彼此不借证明。'
    '科学作用须直接回应本项功能或真实成员；整个菌群共同的保护、维生素或纤维代谢作用，不能代替特定类别或菌种的作用。'
)


AMR_MEANING_RESPONSIBILITY = (
    '整组解释覆盖私有panel范围内的全部真实类别；单类结果不得代表整组。数值0及正常标签保持测量和原级含义，'
    '不将0或正常改成未检出、阴性或无携带，不按不同类别量程比较数值高低。'
    '源内状态判断须绑定实际各类来源；一般肠道耐药基因库的科学意义可以独立引用适用原文，'
    '不要求先证明本人感染或耐药，也不要求文献证明本次原标签。'
    '基因丰度不是本人或病原体药敏；个人感染、耐药、基因转移、用药选择或医学用途须有该句实际适用原文与本人前提。'
    '本栏审核有用的状态判断及同对象科学意义，不采用数字/区间复述、逐项诊断边界声明或无原文的用药建议。'
)


def content_role(path):
    path = tuple(path)
    if ((path[:2] == ('medicalAssessment', 'findings') and path[-1:] == ('sourceAdvice',))
            or (path[:2] == ('comprehensiveConclusion', 'priorityDirections') and path[-1:] == ('followupAction',))
            or (path[:2] == ('comprehensiveConclusion', 'nextSteps') and path[-1:] == ('description',))):
        return 'management'
    if path[:2] == ('medicalAssessment', 'triggerAnalysis') and path[-1:] == ('association',):
        # This is a patient-level cause-candidate assessment. Literature
        # supports its medical premises, not the patient's observed behaviour.
        return 'individual_synthesis'
    if path[:2] == ('diseaseRiskAssessment', 'categories') and 'items' in path:
        return 'disease_meaning'
    if path[:2] in {('microbiomeAssessment', 'healthMetrics'),
            ('microbiomeAssessment', 'antimicrobialResistance')}:
        return 'general_function'
    if path[:2] == ('medicalAssessment', 'findings'):
        return 'clinical_principle'
    if path[:2] == ('microbiomeAssessment', 'keyMicrobialSignals'):
        return 'population_association'
    if (path[:1] == ('nutritionAssessment',) or path[:2] == ('functionalPathways', 'pathways')
            or path[:2] in {('microbiomeAssessment', 'structureInterpretation'),
                ('microbiomeAssessment', 'scfaInterpretation')}):
        return 'general_function'
    return 'individual_synthesis'


def synthesis_path(path):
    path = tuple(path)
    return (path[:1] in {('healthOverview',), ('comprehensiveConclusion',)}
        or path == ('functionalPathways', 'summary', 'text')
        or path == ('medicalAssessment', 'triggerConclusion', 'text')
        or (path[:2] == ('diseaseRiskAssessment', 'categories') and path[-2:] == ('clinicalCorrelation', 'text')))


def normal_resistance_group_context(path, context):
    """A private complete normal inventory owns a deliberately shared slot."""
    if tuple(path) != ('microbiomeAssessment', 'antimicrobialResistance', 0, 'interpretation'):
        return False
    group = next((row for row in context if row.get('semanticType') == 'antimicrobialResistancePanel'), None)
    rows = [row for row in context if row.get('semanticType') == 'antimicrobialResistance']
    return bool(group and group.get('meaningScope') == 'whole_supplied_normal_panel'
        and group.get('storageRow') == 0 and type(group.get('memberCount')) is int
        and group['memberCount'] == len(rows) >= 2
        and all(row.get('value') not in (None, '') and row.get('sourceStatus') == '正常'
            and row.get('assessmentLevel') in (None, '正常') and row.get('factIds') for row in rows)
        and len({row.get('name') for row in rows}) == len(rows)
        and set(group.get('factIds') or []) == {fid for row in rows for fid in row['factIds']})


def role_purpose(purpose, path, assessment_context=(), *, author=False):
    if author and (tuple(path[:2]) in {('microbiomeAssessment', 'healthMetrics'),
            ('microbiomeAssessment', 'antimicrobialResistance')}
            or tuple(path) == ('functionalPathways', 'pathways', 2, 'healthMeaning')):
        # Author the short positive task. Review metadata instructions and
        # the full independent source/scope duties belong to the reviewer.
        return purpose + PATIENT_LANGUAGE_RESPONSIBILITY + author_responsibility(path)
    instruction = (SOURCE_COMPARISON_PURPOSE if source_comparison_path(path)
        else ROLE_PURPOSES.get(content_role(path), ''))
    if single_observation_path(path) and not normal_resistance_group_context(path, assessment_context):
        instruction = ('' if tuple(path[:2]) == ('microbiomeAssessment','antimicrobialResistance')
            else instruction) + SINGLE_OBSERVATION_PURPOSE
    if tuple(path[:2]) == ('microbiomeAssessment', 'healthMetrics'):
        instruction += HEALTH_MEANING_RESPONSIBILITY
    if tuple(path[:2]) == ('microbiomeAssessment', 'antimicrobialResistance'):
        instruction += AMR_MEANING_RESPONSIBILITY
    if tuple(path) in {('healthOverview','overallSummary','text'),
            ('comprehensiveConclusion','overallConclusion','text')}:
        instruction += ('综合医学检查、症状、病史和实际行为形成整体健康画像，菌群与功能结果辅助解释个人相关性。'
            '采用editorialFocus已核验的顺序，概括主要问题、共同诱因和改善顺序；不把检查类型本身当成紧急程度。'
            '不逐项复述数值，不在首尾重复同一关注清单，不把自述内容原样重复给客户；完整依据保留在分项。')
    result = purpose if instruction in purpose else purpose + instruction
    return result + (PATIENT_LANGUAGE_RESPONSIBILITY if author else '') + author_responsibility(path)


def health_amr_author_path(path):
    return tuple(path[:2]) in {('microbiomeAssessment', 'healthMetrics'),
        ('microbiomeAssessment', 'antimicrobialResistance')}


def amr_interpretation_path(path):
    return (len(path) == 4 and tuple(path[:2]) ==
        ('microbiomeAssessment', 'antimicrobialResistance') and path[-1] == 'interpretation')


def independent_function_author_path(path):
    """Separate a function's literature meaning from its existing results.

    This routes author packets only. It neither declares a measurement present
    nor changes the scope or acceptance of any previously reviewed sentence.
    """
    path = tuple(path)
    return ((path[:2] == ('functionalPathways', 'pathways')
            and path[-1:] == ('healthMeaning',))
        or path == ('microbiomeAssessment', 'scfaInterpretation', 'text')
        or (path[:1] == ('nutritionAssessment',) and path[-1:] == ('text',)
            and content_role(path) == 'general_function'))


def text_batches(targets, *, scoped=False, evidence_by_target=None):
    """Keep broad synthesis inventories out of individual evidence questions."""
    if not scoped:
        protocols = {}
        for target in targets:
            protocol = (health_amr_author_path(target.path), amr_interpretation_path(target.path),
                independent_function_author_path(target.path),
                target.path == ('healthOverview', 'summaries', 'medical', 'text'))
            protocols.setdefault(protocol, []).append(target)
        return [group[start:start + 5] for group in protocols.values() for start in range(0, len(group), 5)]
    groups = {}
    batches = []
    for target in targets:
        role = content_role(target.path)
        if role in {'individual_synthesis', 'population_association'}:
            batches.append([target])
        else:
            # A shared evidence array previously let the author borrow an
            # original that belonged only to the neighbouring function node.
            scope = tuple(sorted((record.get('number'), record.get('sourceText') or '')
                for record in (evidence_by_target or {}).get(target.code, [])))
            groups.setdefault((role, health_amr_author_path(target.path), amr_interpretation_path(target.path),
                independent_function_author_path(target.path),
                scope if role == 'general_function' else None), []).append(target)
    for group in groups.values():
        batches.extend(group[start:start + 3] for start in range(0, len(group), 3))
    return batches


def topic_records(records, path, fact_ids):
    """Select exact declared consumers, keeping the original source unchanged."""
    selected = []
    for record in records:
        topics = [topic for topic in record.get('evaluationTopics', [])
            if list(path) in topic.get('consumerPaths', [])
            and set(topic.get('factIds', [])).intersection(fact_ids)]
        if topics:
            selected.append({**record, 'evaluationTopics': deepcopy(topics)})
    return selected


def annotate_topics(records, bindings):
    return [{**record, 'evaluationTopics': [deepcopy(bindings[support])
        for support in record.get('supports', []) if support in bindings]}
        for record in records]


def general_domain_sentence(target, record, review, line):
    """A scientific explanation may have no personal eligibility conditions.

    This marks scope only after ordinary source and sentence review. It never
    approves the claim, creates patient observations or validates a score.
    """
    role = content_role(target.path)
    summary = tuple(target.path) in {
            ('healthOverview', 'summaries', 'microbiome', 'text'),
            ('healthOverview', 'summaries', 'nutrition', 'text')}
    fact_ids = set(target.fact_ids)
    if summary:
        # The abstract may independently explain its actual C objects. The
        # same topic's explicit consumer/fact binding below is still required;
        # this does not grant a medical abstract general-function authority.
        role = 'general_function'
        measured = {fid for context in target.assessment_context
            for row in [context, *context.get('members', []), *context.get('items', [])]
            if row.get('name') and row.get('value') not in (None, '')
            for fid in row.get('factIds', [])}
        fact_ids.intersection_update(measured)
    scope = review.get('scope') or {}
    if (role not in {'general_function', 'disease_meaning'}
            or review.get('applicability') != 'general'
            or scope.get('requirements') != []
            or scope.get('adviceKind') != 'explanation'
            or re.search(r'本(?:次|例|人|项检测)|受检者|该患者|你的|您的|当前(?:结果|检测)|'
                r'(?:检测|检出|测得|评分|分值)(?:结果|显示|提示|为)|按本报告', line)):
        return False
    return any(role in topic.get('allowedRoles', [])
        and list(target.path) in topic.get('consumerPaths', [])
        and fact_ids.intersection(topic.get('factIds', []))
        for topic in record.get('evaluationTopics', []))


def unbound_function_cohort_result(path, line, review):
    """A disease cohort's shift is not this measured function's meaning."""
    correlation = tuple(path[:2]) == ('diseaseRiskAssessment','categories') and tuple(path[-2:]) == ('clinicalCorrelation','text')
    if content_role(path) != 'general_function' and not correlation:
        return False
    explicit_cohort = re.search(r'百岁|超百岁|极高龄|病例|患者|感染者|患儿|治疗者|(?:疾病|患病|临床)队列|'
        r'已诊断[^，,。；;]{0,14}人群|(?:接受|经过|经)[^，,。；;]{0,24}治疗', line)
    cohort_change = re.search(r'重排|富集|显著|增加|减少|升高|降低|高于|低于|偏低|改善|恢复|缓解|完成率|组间|差异', line)
    requirements = (review.get('scope') or {}).get('requirements') or []
    return bool(explicit_cohort and cohort_change and not any(
        isinstance(row, dict) and row.get('purpose') == 'eligibility' for row in requirements))


def _host_only_xenobiotic_source(path, source):
    """Host receptor regulation is not a measured microbial degradation duty."""
    if tuple(path) != ('functionalPathways', 'pathways', 9, 'healthMeaning'):
        return False
    if not re.search(r'nuclear receptors?|\bPXR\b|\bCAR\b|pregnane X|constitutive androstane|'
            r'宿主[^。]{0,15}(?:受体|解毒)|核受体', source, re.I):
        return False
    # A mixed review remains eligible when it actually describes a microbial
    # catabolic process. The ordinary original-text reviews still decide which
    # particular sentence and object that evidence can support.
    return not re.search(r'(?:\b(?:bacteri\w*|microbi\w*)|细菌|菌群)'
        r'[^.!?。\n]{0,55}(?:\b(?:degrad\w*|cataboli\w*|biotransform\w*)|降解|分解)', source, re.I)


def _same_gene_bacterial_process(path, source, facts, line=''):
    """An exact source gene and organism may have a basic biochemical role.

    Such work need not say "gut". It supplies no host or patient outcome and
    still goes through the ordinary original-text and selected-quote reviews.
    """
    if tuple(path[:2]) != ('microbiomeAssessment', 'healthMetrics') or facts is None:
        return False
    if line and re.search(r'本次|本人|本例|受检者|患者|人体|临床|症状|感染|腹泻|损伤|炎症|致病性|产毒能力', line):
        return False
    if not re.search(r'encod\w*|enzym\w*|cataly\w*|glycosyl\w*|glucosyl\w*|translocat\w*|efflux|transport\w*|pump|编码|催化|糖基|酶|外排|转运|泵', source, re.I):
        return False
    from modules.report.evaluation_evidence_plan import _gene_panel_search_taxa
    values = facts.values() if isinstance(facts, dict) else facts
    for fact in values:
        if fact.kind not in {'context', 'function'} or '检出基因' not in fact.value:
            continue
        taxa = _gene_panel_search_taxa(fact.name)
        if not taxa or not any(re.search(r'\b' + re.escape(taxon) + r'\b', source, re.I) for taxon in taxa):
            continue
        detected = re.findall(r'检出基因[：:]\s*([^；;\n]+)', fact.value)
        genes = [gene for text in detected for gene in re.split(r'[,，、\s]+', text)
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', gene)]
        if any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(gene) + r'(?![A-Za-z0-9_])', source, re.I)
                and (not line or re.search(r'(?<![A-Za-z0-9_])' + re.escape(gene) + r'(?![A-Za-z0-9_])', line, re.I))
                for gene in genes):
            return True
    return False


def _lipid_pathway_process(text):
    """A lipid process is more specific than a dietary or metabolic outcome.

    SCFA production belongs to its own pathway. Mentioning its downstream
    glucose/lipid benefits does not explain the measured lipid/bile domain.
    This necessary domain check does not approve the claim or its citation.
    """
    text = re.sub(r'短链脂肪酸|\bSCFAs?\b|short.chain fatty acids?', '', text or '', flags=re.I)
    domain = (r'脂质|脂类|脂肪|胆汁酸|胆固醇|类固醇|甘油|花生四烯酸|亚油酸|磷脂|鞘脂|'
        r'\b(?:lipids?|fats?|fatty acids?|bile acids?|steroids?|cholesterol|triglycerides?|phospholipids?)\b')
    process = (r'合成|转化|去结合|水解|降解|分解|'
        r'\b(?:biosynth\w*|synthes\w*|degrad\w*|breakdown|deconjug\w*|hydrolys\w*|biotransform\w*|transform\w*)\b')
    for clause in re.split(r'[，,。；;！？\n]', text):
        if re.search('(?:' + domain + r').{0,36}(?:' + process + ')|(?:' + process
                + r').{0,36}(?:' + domain + ')', clause, re.I):
            return True
        if re.search(r'(?:胆汁酸|胆固醇|类固醇|bile acids?|cholesterol|steroids?)\s*(?:的)?\s*(?:代谢|metabolism)'
                r'|\bmetaboli[sz]\w*\s+(?:lipids?|fats?|fatty acids?|bile acids?|cholesterol)\b', clause, re.I):
            return True
    return False


def _neurotransmitter_pathway_process(text):
    """Require a transmitter's process or role, not a Trp/immune topic match.

    This is a necessary object check; the ordinary citation review still owns
    the exact claim, experimental scope, direction and strength. A metabolite
    merely listed inside a parenthetical inventory supplies no such process.
    """
    text = re.sub(r'\([^)]*[,、][^)]*\)|（[^）]*[,、][^）]*）', '', text or '')
    transmitter = (r'多巴胺|血清素|5[-‐‑–\s]?羟色胺|[γΓ]-?氨基丁酸|伽马氨基丁酸|神经递质|'
        r'\b(?:dopamine|serotonin|5[-‐‑–\s]?HT|GABA|gamma[-\s]aminobutyric acid|'
        r'5[-\s]hydroxytryptamine|neurotransmitters?)\b')
    production = (r'合成(?!能力|评分|评级|通路|状态|方向|[：:])|生成|产生|转化|脱羧|分解|降解|'
        r'\b(?:synthesi[sz]\w*|biosynth\w*|produc\w*|convert\w*|conversion|transform\w*|'
        r'decarboxyl\w*|degrad\w*)\b')
    role = (r'(?:是|为|作为)?(?:可|能够|可以|主要|也|还|则)*(?:参与|调节|调控|传递|结合|作用于)|'
        r'(?:(?:can|may|also|directly)\s+)*'
        r'(?:regulat\w*|modulat\w*|transmit\w*|signal\w*|bind\w*|acts?\s+(?:on|in)|'
        r'(?:is|are)\s+(?:vital|crucial|essential|important|critical|necessary)\s+(?:for|to|in)|'
        r'(?:is\s+)?involved\s+in|participat\w*\s+in)\b')
    function = (r'神经|信号|受体|肠道运动|结肠运动|肠蠕动|分泌|免疫|兴奋|抑制|情绪|饥饿|食欲|睡眠|疼痛|痛觉|'
        r'\b(?:neurotransmit\w*|neural|neuronal|signals?|signall?ing|receptors?|motility|'
        r'secretion|secretory|immune|inhibitory|excitatory|emotion(?:al)?|hunger|appetite|sleep|pain)\b')
    for clause in re.split(r'[。；;！？!?\n]|\.(?:\s|$)', text):
        if re.search('(?:' + production + r').{0,55}(?:' + transmitter + ')', clause, re.I):
            return True
        if re.search('(?:' + transmitter + r').{0,16}(?:' + production + r').{0,20}'
                r'(?:由|通过|依赖|需要|底物|催化|酶|from|by|via|requires?)', clause, re.I):
            return True
        if re.search('(?:' + transmitter + r').{0,12}(?:由|通过|from|via).{0,45}(?:'
                + production + ')', clause, re.I):
            return True
        if re.search(r'(?:调节|调控|影响|参与|modulat\w*|regulat\w*|affect\w*)\s*.{0,20}(?:'
                + transmitter + r').{0,8}(?:合成|生成|代谢|synthesis|production|metabolism)', clause, re.I):
            return True
        for item in re.finditer(transmitter, clause, re.I):
            # Bind the action to this transmitter's predicate/relative clause.
            # A long real function list need not fit a character window, but
            # a later sentence or another subject cannot supply this action.
            tail = clause[item.end():].strip()
            tail = re.sub(r'^,?\s*(?:which|that)\s+', '', tail, flags=re.I)
            tail = re.split(r'\b(?:whereas|while|but)\b|然而|但是|但|而', tail,
                maxsplit=1, flags=re.I)[0]
            tail = re.sub(r'^(?:是|为)(?:一种|一类)?(?:抑制性|兴奋性)?神经递质\s*[，,]\s*', '', tail)
            if re.match(r'^(?:对|对于|在)', tail):
                importance = re.search(r'至关重要|至为重要|重要作用|关键作用|必不可少|不可或缺', tail)
                if importance and re.search(function, tail[:importance.start()], re.I):
                    return True
            tail = re.sub(r'^(?:is|are)\s+(?:(?:a|an|the)\s+)?'
                r'(?:(?:inhibitory|excitatory)\s+)?neurotransmitters?\s+'
                r'(?:(?:which|that)\s+)?', '', tail, flags=re.I)
            action = re.match(role, tail, re.I)
            if action:
                own_function = re.split(r'[,，]|\band\b', tail[action.end():],
                    maxsplit=1, flags=re.I)[0]
                if re.search(function, own_function, re.I):
                    return True
    return False


def _sugar_object_process(text, objects):
    """Require a process of a measured sugar in this sentence/context."""
    process = (r'分解|水解|利用|转化|发酵|消化|代谢(?!能力|评分|状态)|'
        r'\b(?:degrad\w*|hydrolys\w*|utili[sz]\w*|convert\w*|ferment\w*|digest\w*|'
        r'metaboli[sz](?:e|es|ed|ing)|metabolism)\b')
    for clause in re.split(r'[，,。；;！？!?\n]|\.(?:\s|$)', text or ''):
        if any(re.search('(?:' + obj + r').{0,36}(?:' + process + ')|(?:'
                + process + r').{0,36}(?:' + obj + ')', clause, re.I) for obj in objects):
            return True
    return False


def measured_sugar_study_scope_failures(source, *, title=''):
    """Keep an animal-feeding experiment out of personal sugar-role duties.

    This is a necessary scope check for evaluate's measured sugar candidates,
    not a ban on animal basic science or a claim that an unlabelled paper is
    human. Mixed human/animal work still needs the ordinary sentence reviews.
    """
    source = source or ''
    animal = re.search(r'\b(?:rats?|mice|mouse|murine|steers?|heifers?|Holstein|'
        r'rumen|ruminal|ruminants?|piglets?|weaned pigs?|broilers?|chicks?|poultry)\b|'
        r'大鼠|小鼠|肉牛|奶牛|犊牛|瘤胃|反刍动物|仔猪|雏鸡|肉鸡',title+'\n'+source,re.I)
    feeding = re.search(r'\b(?:fed|feeding|feeding trial|dietary treatments?|'
        r'experimental diets?|feed.to.gain|growth performance)\b|饲喂|饲料|生长性能',source,re.I)
    human_title = re.search(r'\bhumans?\b|人体|人类',title,re.I)
    human_cohort = re.search(r'\b(?:recruited|enrolled|randomi[sz]ed|allocated)\b'
        r'[^.!?\n]{0,65}\b(?:human subjects|participants|volunteers|patients)\b|'
        r'\b(?:human subjects|participants|volunteers|patients)\b[^.!?\n]{0,65}'
        r'\b(?:recruited|enrolled|randomi[sz]ed|allocated|received)\b',source,re.I)
    if animal and feeding and not human_title and not human_cohort:
        return ['本篇为动物饲喂实验，不能承担本次个人糖类代谢通路的健康意义或人体疾病支持；'
            '应选有真实人体肠道过程的原文，不以省略动物范围或补限制声明替代适用性。']
    return []


def function_research_use_failures(path, source, line='', *, facts=None, subject_age=None):
    """Check original study population/purpose before any paraphrased use.

    A topic binding or a general-review marker cannot make a mismatched study
    this person's association, or make cellular definitions a gut-health meaning.
    Source facts, exposure thresholds and source results are never manufactured.
    """
    source = source or ''
    if tuple(path) == ('functionalPathways', 'pathways', 8, 'healthMeaning') and line:
        # A familiar bacterial location cannot be added to a different
        # original's LPS/HCY effect. The selected-quote review calls this same
        # gate with its own excerpts, never another record's description.
        objects = (r'脂多糖|\b(?:lipopolysaccharides?|LPS)\b',
            r'同型半胱氨酸|\b(?:homocysteine|HCY)\b')
        locations = ((r'细胞壁|\bcell[\s-]*walls?\b', '细胞壁'),
            (r'外膜|\bouter[\s-]*membranes?\b', '外膜'),
            (r'胞内|细胞内|\bintracellular\b', '胞内'),
            (r'胞外|细胞外|\bextracellular\b', '胞外'))
        scopes = re.split(r'[。；;！？!?\n]|\.(?=\s+[A-Z]|$)|,\s+(?:whereas|while|but|and)\b|然而|但是|，而', source)
        for obj in objects:
            if not re.search(obj, line, re.I):
                continue
            for location, name in locations:
                if (re.search(location, line, re.I) and not any(re.search(obj, scope, re.I)
                        and re.search(location, scope, re.I) for scope in scopes)):
                    return ['本句新增的' + name + '定位须由自己所引原句明确支持同一对象；'
                        '常识、同篇缩略词的其他蛋白或另一条记录不提供该定位。须忠实重写整句，不删定位词救旧句。']
        nf = r'NF[\s-]?(?:κ|kappa|k)[\s-]?B'
        for clause in re.split(r'[，,。；;！？\n]', line):
            molecule = re.search(nf, clause, re.I)
            if molecule is None:
                continue
            before, after = clause[:molecule.start()], clause[molecule.end():]
            produced = re.search(r'产生|生成|合成', before) or (
                re.search(r'产生|生成|合成', after)
                and not re.search(r'激活|信号|活性|响应|磷酸化', after.split('产生', 1)[0]))
            if produced and not re.search(
                    r'(?:synthesis|expression|production)(?:\s+levels?)?\s+(?:of\s+)?' + nf + '|'
                    + nf + r'\s+(?:protein\s+)?(?:synthesis|expression|production)|'
                    + nf + r'[^，,。；;\n]{0,8}(?:蛋白)?(?:合成|表达|生成|产生)|'
                    r'(?:合成|表达|生成|产生)[^，,。；;\n]{0,8}' + nf, source, re.I):
                return ['NFκB信号诱导或激活不能改写为该蛋白产生；并列的NFκB与IL-8 production原句不单独证明NFκB生成。'
                    '只有自己所选原句明确其合成、表达或产生才采用该断言；请忠实新写整句，不程序替换旧谓语。']
    # The study's actual population and descriptive purpose travel with the
    # original, so moving it to another node or rewording its result cannot help.
    if (re.search(r'centenarian|百岁|超百岁', source, re.I)
            and re.search(r'metagenom|microbiom|cohort|subjects?|samples?|宏基因组|菌群|样本|队列', source, re.I)):
        return ['极高龄特定队列的描述性菌群研究未建立与本例的适用关系，不用于本例的诱因、功能或综合解释']
    if facts is not None:
        actual = [fact for fact in facts if getattr(fact, 'kind', None) in {'clinical','self_reported'}
            or getattr(fact, 'kind', None) == 'context' and re.search(r'症状|病史', fact.name)]
        def affirmative(pattern):
            return any(re.search(pattern, clause, re.I) and not re.search(r'否认|没有|无|从不|未见|不清楚|未知', clause)
                for fact in actual for clause in re.split(r'[，,。；;\n]', fact.name+' '+fact.value))
        quality = re.search(r'restful(?:ness)?|daytime\s+sleepiness|sleep\s+quality', source, re.I)
        duration = re.search(r'sleep\s*[≥>]\s*(\d+(?:\.\d+)?)\s*hours?', source, re.I)
        if quality and re.search(r'diarrhea', source, re.I) or duration and re.search(r'constipation', source, re.I):
            quality_match = bool(quality and affirmative(r'腹泻|diarrhea') and affirmative(
                r'(?:白天|日间)[^，,。；;]{0,12}(?:困倦|嗜睡|未休息|不解乏)|PSQI|restful'))
            long_sleep = bool(duration and affirmative(r'便秘|constipation') and any(
                float(match[1]) >= float(duration[1]) and not re.search(r'没有|否认|不足|不到|少于|不超过|[<≤]', clause[:match.start(1)])
                for fact in actual for clause in re.split(r'[，,。；;\n]', fact.value)
                for match in re.finditer(r'(?<!入)睡(?:眠|觉)?[^\d，,。；;]{0,6}(\d+(?:\.\d+)?)\s*(?:小时|hours?)', clause, re.I)))
            if not quality_match and not long_sleep:
                return ['原研究定义的睡眠暴露与对应排便结局没有同时匹配本人实际资料；一般研究范围说明不能替代本例关联']
    gut = re.search(r'\bgut\b|intestin|\bbowel\b|\benteric\b|\bcolon(?:ic)?\b|colitis|diarrh|肠道|肠炎|肠上皮|腹泻', source, re.I)
    nonclinical_purpose = re.search(r'industrial applications?|industrialists|commercialization|'
        r'bioremediation|environmental monitoring|yeast\s+Saccharomyces|'
        r'bacterial pathogenesis|cell functions?[^.]{0,80}DNA replication|工业应用|环境修复', source, re.I)
    if nonclinical_purpose and not gut:
        return ['完整原文的研究目的为工业、环境或基础细胞机制，未建立本例肠道健康适用性；不跨节点复用为个体解释']
    if content_role(path) != 'general_function':
        return []
    if re.search(r'代谢产物[^。；;]{0,35}免疫耐受', line):
        same_actor = any(re.search(r'metabolites?|代谢产物', sentence, re.I)
            and re.search(r'immune\s+tolerance|免疫耐受', sentence, re.I)
            for sentence in re.split(r'[。！？\n]|\.(?:\s+|$)', source))
        if not same_actor:
            return ['原文未在同一研究陈述中将免疫耐受作用归于代谢产物；不能把色氨酸本身的作用转给其代谢产物。']
    # A source saying that SCFAs are bacterial metabolites does not, by
    # itself, supply the additional substrate and fermentation process. Keep
    # the source usable for its narrower statement or choose an original that
    # actually states the process; a familiar mechanism is not a quotation.
    if (re.search(r'短链脂肪酸|\bSCFAs?\b|short.chain fatty acids?', line, re.I)
            and re.search(r'发酵|\bferment\w*', line, re.I)
            and re.search(r'膳食纤维|(?:dietary\s+)?fib(?:er|re)', line, re.I)
            and not (re.search(r'发酵|\bferment\w*', source, re.I)
                and re.search(r'膳食纤维|(?:dietary\s+)?fib(?:er|re)', source, re.I))):
        return ['该原文没有同时陈述膳食纤维底物与发酵过程，不能补入常识扩写；改用真实陈述该过程的原文，或仅写本篇明确支持的菌群代谢物及食物影响。']
    if _host_only_xenobiotic_source(path, source):
        return ['宿主核受体调控不能替代被测菌群的化学物降解功能；应采用实际解释微生物降解或生物转化的原文']
    sugar_path = (tuple(path) == ('functionalPathways', 'pathways', 0, 'healthMeaning')
        or tuple(path[:2]) == ('nutritionAssessment', 'detailedMetabolism')
        and tuple(path[-2:]) == ('explanation', 'text') and 'items' not in path)
    if tuple(path)==('functionalPathways','pathways',0,'healthMeaning'):
        title = source.split('\n', 1)[0] if '\n' in source else ''
        failures = measured_sugar_study_scope_failures(source, title=title)
        if failures:
            return failures
    if sugar_path and line and facts is not None:
        values = facts.values() if isinstance(facts, dict) else facts
        names = [fact.name for fact in values if getattr(fact, 'kind', None) == 'function'
            and getattr(fact, 'value', '') not in (None, '')
            and not re.search(r'纤维需求|fiber need|fibre need', fact.name, re.I)]
        sugar_objects = (r'半乳糖|\bgalactose\b', r'淀粉|蔗糖|\bstarch\b|\bsucrose\b')
        # A real fibre-degradation assay remains a distinct eligible object.
        # Taxon names or a fibre-demand rating cannot manufacture that assay.
        if names and all(any(re.search(obj, name, re.I) for obj in sugar_objects) for name in names):
            title = source.split('\n', 1)[0] if '\n' in source else ''
            failures = measured_sugar_study_scope_failures(source,title=title)
            if failures:
                return failures
            actual = [obj for obj in sugar_objects if any(re.search(obj, name, re.I) for name in names)]
            # The same original may use its immediately defined abbreviation
            # or a shared parallel-subject predicate. Reuse that bounded
            # literal-role check, without expanding any selected quotation.
            from .evaluation_reference_plan import measured_function_sentence_role
            from .clinical_report import _review_quote_options
            aliases=tuple(alias for name in names for alias in (
                ('galactose',) if re.search(sugar_objects[0],name,re.I) else ('starch','sucrose')))
            original_process=any(measured_function_sentence_role('P1',quote,aliases,source=source)
                for quote in _review_quote_options(source).values())
            literal_line_process=bool(line in source and measured_function_sentence_role(
                'P1',line,aliases,source=source))
            if not (_sugar_object_process(line, actual) or literal_line_process) or not (
                    _sugar_object_process(source, actual) or original_process):
                return ['糖类代谢科学句须说明本组实际半乳糖、淀粉或蔗糖的利用、分解等过程；'
                    '仅说明膳食纤维发酵产生短链脂肪酸及其作用，或只列出糖类名称，不能替代这些实际测项的意义']
    if (tuple(path[:2]) == ('microbiomeAssessment', 'healthMetrics')
            and tuple(path[-1:]) == ('explanation',) and line):
        supplementation = re.search(r'补充|服用|给予|摄入|supplement\w*|administ\w*|intake', line, re.I)
        clinical_benefit = re.search(r'预防|减轻|缓解|治疗|prevent\w*|alleviat\w*|reliev\w*|treat\w*', line, re.I)
        illness = re.search(r'疾病|炎症|感染|便秘|腹泻|肠炎|肿瘤|癌|'
            r'diseases?|inflammation|infection|constipation|diarrh\w*|colitis|cancer', line, re.I)
        if supplementation and clinical_benefit and illness:
            return ['补充或给药策略及其可能预防、减轻疾病的临床获益不属于评估单项的基础意义；'
                '保留同文独立支持的基础生理或代谢机制，不以研究设想或可能语气替代本栏职责']
        strain = re.search(r'\bstrains?\b|菌株|(?:\b[A-Z]\.\s*[a-z]+|\b[A-Z][a-z]+\s+[a-z]+)'
            r'(?:\s+subsp\.\s+[a-z]+)?\s+[A-Z]{1,6}[- ]?\d{2,}', source)
        intervention = re.search(r'probiotic|supplement\w*|administ\w*|intervention|treatment|'
            r'益生菌|补充|服用|干预|给药', source, re.I)
        clinical = re.search(r'clinical|trial|patients?|participants?|functional constipation|'
            r'临床|受试者|患者|便秘人群', source, re.I)
        outcome = re.search(r'便秘|腹泻|排便|(?:肠道|胃肠)(?:传输|转运)时间|症状|'
            r'constipation|diarrh\w*|bowel movements?|stool frequency|intestinal transit|symptoms?', line, re.I)
        benefit = re.search(r'改善|缓解|减轻|缩短|减少|降低|增加|提高|'
            r'improv\w*|reliev\w*|reduc\w*|shorten\w*|increas\w*', line, re.I)
        if strain and intervention and clinical and outcome and benefit:
            return ['特定菌株补充干预的临床疗效不能替代所测一般物种或菌群的基础意义；'
                '另选原文明示的基础生理或代谢过程，不因同文含临床试验而禁用其独立支持的基础研究句']
    if (tuple(path[:2]) == ('functionalPathways', 'pathways') and len(path) == 4
            and path[2] == 1 and line and not _lipid_pathway_process(line)):
        return ['脂质与胆汁酸通路的科学句须解释实际脂类或胆汁酸合成、转化等过程及相应意义；'
            '仅提食物脂肪、短链脂肪酸产生或糖脂代谢健康结局不能代替本通路解释']
    if tuple(path) in {('functionalPathways', 'pathways', 5, 'healthMeaning'),
            ('nutritionAssessment', 'neurotransmitterInterpretation', 'text')} and line:
        if not _neurotransmitter_pathway_process(line):
            return ['神经递质合成通路的科学句须实际说明神经递质的生成、转化或作用；'
                '仅说明菌群、色氨酸代谢与免疫的关系，或附加递质名称及评分，不能替代本通路的对象职责']
        if not _neurotransmitter_pathway_process(source):
            return ['神经递质合成通路的原文须实际陈述神经递质的生成、转化或作用；'
                '仅列举血清素等代谢物或说明色氨酸与免疫的关系，不能支持本句新增的递质过程']
    if (re.search(r'\b(?:broilers?|chicks?|poultry|piglets?|weaned pigs?|ruminants?)\b|肉鸡|雏鸡|仔猪|家禽',source,re.I)
            and re.search(r'\b(?:supplement\w*|dietary treatment|growth performance|feed.to.gain|feeding trial)\b|饲料|饲喂|生长性能',source,re.I)):
        return ['动物饲喂或补充干预的酶活、代谢或生长结果不能充当用户肠道功能的健康意义；注明动物实验或添加不能外推声明也不改变该用途不匹配']
    if type(subject_age) in (int,float) and subject_age>=18 and re.search(r'(?:review aims|this review|focus(?:es|ing)? on)[^.!?\n]{0,160}'
            r'(?:preterm|premature infant)|早产儿[^。\n]{0,40}(?:综述|屏障发育|坏死性小肠结肠炎)',source,re.I):
        return ['该原文以早产儿发育、屏障病理及预防治疗为专项问题，未建立本次用户功能解释的适用性；通用背景须另取对应人体功能原文，不以专项应用结果或免责声明填充']
    if (type(subject_age) in (int, float) and subject_age >= 18
            and re.search(r'\binfants?\b|neonat\w*|early life|fetal|placent\w*|婴儿|新生儿|母胎|胎儿|胎盘', source, re.I)
            and re.search(r'(?:肠道|菌群|宿主|免疫)[^，,。；;\n]{0,20}(?:成熟|发育)|胎盘|胎儿|母胎|'
                r'gut maturation|(?:microbiota|intestinal|host|immune)[^.!?\n]{0,30}(?:maturation|development)', line, re.I)):
        return ['婴儿或母胎阶段依赖的宿主成熟/发育结局不适用于成年人的本栏健康意义；保留研究限定也不能替代成年功能用途。'
            '可另选原文独立支持的基础菌群代谢或基因作用，不要求基本培养过程与本人同龄。']
    if not gut and not _same_gene_bacterial_process(path, source, facts, line):
        return ['原文研究目的未涉及肠道功能或宿主肠道健康；工业、环境、模式生物或纯细胞定义不填充本例功能意义']
    return []



def detection_absence_failures(text, *, value=None, source_status=None):
    """A row's negative detection claim needs that same row's source label.

    A displayed quantitative zero or a normal reference grade does not state
    an assay's absence/detection-limit semantics. A panel summary cannot lend
    its negative meaning to an individual measured row.
    """
    if not isinstance(text, str):
        return []
    absence = r'未检出|未检测到|没有检测到|阴性|未查见'
    zero = (type(value) in (int, float) and value == 0 or isinstance(value, str)
        and re.fullmatch(r'[+-]?0+(?:\.0+)?', value.strip()) is not None)
    if zero:
        # A numeric zero does not establish the assay's detection limit.
        # Preserve ordinary "未见超出/升高" normal-background comparisons.
        signals = [clause for clause in re.split(r'[，,。；;！？\n]', text)
            if re.search(r'(?:未见|未呈现|没有)[^，,。；;！？\n]{0,18}(?:携带|丰度|检出|检测)信号', clause)
            and not re.search(r'超出|高于|升高|偏高|增多|异常|不能|不代表|不等于|是否', clause)]
        if signals and not re.search(absence, ' '.join(str(part) for part in (value, source_status) if part is not None)):
            return ['真实零值不等于未检出或无可测携带/丰度信号；保留0及原正常标记，不补检测限结论。']
    if not re.search(absence, text):
        return []
    # A concrete uncertainty/negative-inference sentence is not an assertion
    # that this specimen tested negative.
    clauses = [clause for clause in re.split(r'[。；;！？\n]', text)
        if re.search(absence, clause) and not re.search(
            r'不能|不可|无法|尚不能|不代表|不等于|不意味着|是否|不确定', clause)]
    if not clauses:
        return []
    original = ' '.join(str(part) for part in (value, source_status) if part is not None)
    if not re.search(r'未检出|未检测到|没有检测到|未查见|阴性', original):
        return ['逐行未检出/阴性结论须由同一原行明确阴性原值或原标签支持；数值0、正常和总类结论均不能替代']
    return []


def _health_association_patient_state_failures(text, facts, assessment_context):
    """A positive microbial panel is not an observed patient condition.

    Use the existing typed panel identities and exact source vocabulary. A
    real A/B observation or C-reported history must support this same theme;
    unrelated clinical facts, family history and research cannot supply it.
    """
    from .evaluation_catalog import fixed_disease_code, FIXED_DISEASE_ALIASES
    from .evaluation_template_input import HEALTH_ASSOCIATION_PANELS
    from .evaluation_fixed_assessment import _answer, _goal_only_question, _goal_clause

    values = list(facts.values() if isinstance(facts, dict) else facts)
    associations = [row for row in assessment_context
        if row.get('semanticType') == 'healthAssociation'
        or row.get('resultKind') == 'sourceHealthAssociation']
    association_ids = {fid for row in associations for fid in row.get('factIds', [])}
    names = {row['name'] for row in associations if isinstance(row.get('name'), str) and row['name'].strip()}
    # Final finite projections can lack a target object, but retain the
    # original context fact and its explicit detection/related-microbe words.
    for fact in values:
        if getattr(fact, 'kind', None) != 'context':
            continue
        value, name = str(fact.value or ''), str(fact.name or '')
        if (re.match(r'^\s*(?:未?检出|未?检测到)(?:[；;，,：:\s]|$)', value)
                and (name in HEALTH_ASSOCIATION_PANELS.values() or fixed_disease_code(name)
                    or re.search(r'相关菌|关联菌', value))):
            names.add(name)
            association_ids.add(fact.fact_id)
    if not names:
        return []
    histories = {fid for row in assessment_context
        if row.get('semanticType') == 'sourceReportedBackground' for fid in row.get('factIds', [])}
    clinical = [fact for fact in values if getattr(fact, 'fact_id', None) not in association_ids
        and (getattr(fact, 'kind', None) in {'clinical', 'self_reported'}
            or getattr(fact, 'kind', None) == 'context' and (fact.fact_id in histories
                or re.search(r'(?:原报告|来源报告).*(?:症状|主诉|病史|疾病史|疾病情况)', fact.name)))]
    def aliases(name):
        code = fixed_disease_code(name)
        terms = (name, *FIXED_DISEASE_ALIASES.get(code, ()))
        # Ordinary patient wording can name a recorded complaint as a
        # "problem" rather than repeat a panel's "disorder" heading. These
        # are claim-detection aliases, never a new diagnosis mapping.
        variants = [term[:-2] + suffix for term in terms if term.endswith('障碍')
            for suffix in ('问题', '困扰')]
        variants.extend(re.sub(r'^(?:[ⅠⅡⅢⅣIV]+|[1-4一二三四])\s*型\s*', '', term)
            for term in terms if re.match(r'^(?:[ⅠⅡⅢⅣIV]+|[1-4一二三四])\s*型', term))
        return tuple(dict.fromkeys((*terms, *variants)))
    def supported(terms):
        # The existing symptom-outcome vocabulary can support an observed
        # sleep complaint, without treating sleep apnoea as the same disease.
        if '睡眠障碍' in terms:
            from .evaluation_evidence_plan import _TRIGGER_OUTCOMES
            terms = (*terms, *(alias for _, title, names in _TRIGGER_OUTCOMES
                if title == 'sleep onset difficulty' for alias in names))
        pattern = '|'.join(re.escape(term) for term in terms)
        for fact in clinical:
            if (re.search(r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹', fact.name)
                    or fact.kind == 'self_reported' and _goal_only_question(fact)):
                continue
            value = _answer(fact)
            if not value or re.fullmatch(
                    r'\s*(?:0(?:\.0+)?|正常|充足|良好|无异常|没有困扰)\s*[。.]?', value):
                continue
            for clause in re.split(r'[，,。；;！？\n]', value):
                if re.search(r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹', clause) or _goal_clause(clause):
                    continue
                for match in re.finditer(pattern, clause, re.I):
                    prefix, suffix = clause[:match.start()], clause[match.end():]
                    prefix = re.split(r'但是|但|然而|而|同时|并且|且|伴', prefix)[-1]
                    if (re.search(r'否认|未见|没有|未患|未确诊|未诊断|排除|不考虑|无|'
                            r'建议|留意|关注|观察|监测|预防|疑似|考虑|可能|是否|'
                            r'如果|假如|如出现|若出现|如有|若有', prefix[-12:])
                            or re.match(r'\s*(?:相关)?(?:风险|易感|可能|疑似|待排|是否)', suffix)
                            or re.match(r'\s*[：:]?\s*(?:无|否|正常|阴性|未检出)(?:$|[。；;])', suffix)):
                        continue
                    return True
            # An affirmative response to this actual question/name is usable;
            # the question's keyword alone with a negative/other answer is not.
            if re.search(pattern, fact.name, re.I) and re.fullmatch(r'是|有|存在|已确诊|确诊|阳性', value):
                return True
            if (re.search(pattern, fact.name, re.I) and not re.search(r'风险|评分|量表|筛查|概率', fact.name)
                    and re.search(r'[1-9]\d*(?:\.\d+)?\s*次', value)
                    and not re.search(r'否认|没有|未见|无|未知|不清楚', value)):
                return True
        return False
    for name in sorted(names, key=len, reverse=True):
        terms = aliases(name)
        pattern = '|'.join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
        clauses = [(clause, sentence) for sentence in re.split(r'[。；;！？\n]', text)
            for clause in re.split(r'[，,]', sentence)]
        for clause, sentence in clauses:
            for match in re.finditer(pattern, clause, re.I):
                before, after = clause[:match.start()], clause[match.end():]
                population = r'研究|文献|队列|患者中|患者组|人群中|患者(?:常|通常|可能)|临床上|一般人群'
                person = r'本人|本例|本次(?!研究|实验)|本项(?!研究|实验)|受检者|您的|该用户|该患者'
                if (re.search(population, sentence) and not re.search(person, sentence)
                        or re.search(population, clause) and not re.search(person, clause)):
                    continue
                if (re.search(r'(?:如|若|假如)(?:有|出现|发生|存在)?|是否|可能|潜在|'
                        r'不代表|不等于|不能|未记录|否认|未患|未确诊|未诊断|未见|没有|不存在|未出现|未发生|'
                        r'无(?:明显|持续|相关)?$', before[-18:])
                        or re.match(r'\s*(?:的)?(?:相关|关联)(?:的)?(?:菌群|菌|微生物|信号)|'
                            r'\s*(?:的)?(?:菌群|微生物|(?:较高|较低|高|中|低|一定)?风险|易感)|'
                            r'\s*(?:存在|具有|面临).{0,8}(?:风险|易感)', after)
                        or re.search(r'相关|关联', before[-4:]) and re.match(r'\s*(?:菌群|菌|信号)', after)):
                    continue
                if re.search(r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹', before) and not re.search(
                        r'本人|用户|受检者|该患者', before):
                    continue
                claim = (re.search(r'(?:已检出|检出|患有|已患|确诊|诊断为|罹患|主诉|自述|'
                            r'记录了|记载了|记录有|记载有|记录为|记载为|已记录|已记载|'
                            r'已有|既有|原有|已存在|实际|当前|目前|现有|存在|出现|发生)(?:的)?\s*$', before)
                    or re.search(r'(?:当前|现有|实际|已记录|本人).{0,12}(?:问题|症状|疾病|病史).{0,4}[为是：:]?\s*$', before)
                    or re.match(r'\s*(?:已|已经)?(?:检出|检测到|存在|出现|发生|确诊|患有|伴随)', after)
                    or re.match(r'\s*(?:表现|症状|问题|情况|困扰).{0,35}(?:存在|出现|发生|并存|已记录|已记载)', after)
                    or re.match(r'\s*是.{0,22}(?:实际|现有|当前|已有|已记录|已记载).{0,12}'
                        r'(?:问题|症状|表现|困扰|情况|病史)', after)
                    or re.match(r'\s*是.{0,22}已存在', after)
                    or re.match(r'\s*(?:与|和).{0,40}(?:同时存在|并存)', after))
                if claim and not supported(terms):
                    return ['“' + name + '”仅有健康相关菌群检出，不能写成本人已发生的症状、实际问题或疾病；'
                        '该主题须有真实医学/自述或sourceReportedBackground病史支持，其他主题的临床记录不能代替。']
    return []


def reported_symptom_attribution_failures(text, facts=(), assessment_context=()):
    """A C-report symptom attribution needs its recorded symptom source.

    A B answer may support the same health theme, and a C association panel
    may be positive, while neither changes what C actually recorded as a
    symptom. Check the explicit attribution before independent review/prose
    restoration, without changing any original words or source relations.
    """
    if not isinstance(text, str):
        return []
    failures = _health_association_patient_state_failures(text, facts, assessment_context)
    recorded = [row.get('sourceDescription') or row.get('value')
        for row in assessment_context
        if row.get('semanticType') == 'sourceReportedBackground'
        and re.search(r'症状|主诉', row.get('name') or '')]
    values = facts.values() if isinstance(facts, dict) else facts
    recorded.extend(fact.value for fact in values
        if getattr(fact, 'kind', None) == 'context'
        and re.search(r'(?:原报告|来源报告).*(?:症状|主诉)', fact.name))
    recorded = [value for value in recorded if isinstance(value, str) and value.strip()]
    if not recorded:
        return failures
    lead = r'(?:原报告|来源报告)(?:中)?(?:记载|记录|提及|列出|所列)?(?:的)?'
    lists = []
    for attribution in re.finditer(lead, text):
        following = re.split(r'(?:问卷|用户|本人)(?:自述|自报|填写|报告)',
            text[attribution.end():], maxsplit=1)[0]
        pattern = (r'^(?:临床)?(?:症状|主诉)(?:包括|为|有|[：:])(?P<items>[^。；;，,\n]{1,100})'
            if re.match(r'^(?:临床)?(?:症状|主诉)', following) else
            r'^(?![^。；;，,\n]{0,100}(?:病史|疾病史|用药|问卷))'
            r'(?P<items>[^。；;，,\n]{1,100}?)(?:等)?(?:症状|主诉)')
        match = re.match(pattern, following)
        if match is not None:
            lists.append(match['items'])
    key = lambda value: re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]', '', value)
    originals = [key(value) for value in recorded]
    for names in lists:
        for fragment in re.split(r'[、，,]|(?:以及|及|和|与|或)', names):
            fragment = re.sub(r'^(?:上述|所列|已有|原有|相关)', '', fragment.strip())
            if fragment and not any(key(fragment) in original for original in originals):
                return [*failures, '原报告症状归属仅由其实际症状记录支持；“' + fragment
                    + '”不在该症状记录中，问卷自述和健康关联检出须各按本身来源表述']
    return failures

def source_comparison_path(path):
    return tuple(path[:2]) == ('diseaseRiskAssessment','categories') and tuple(path[-2:]) == ('clinicalCorrelation','text')


def integrated_risk_grade_failures(text, risks):
    """Check authored integrated grades, without touching original labels."""
    from .evaluation_catalog import presentation_health_level
    errors = []
    for risk in risks:
        name, score = risk.get('name'), risk.get('index')
        if not isinstance(name, str):
            continue
        expected = presentation_health_level(score) if type(score) in {int, float} else None
        base = re.sub(r'(?:相关|发生|易感)?风险$', '', name)
        for clause in re.split(r'[，,。；;！？\n]', text or ''):
            if not any(label and label in clause for label in {name, base}):
                continue
            match = re.search(r'(?:评为|分级(?:为)?|属于|风险(?:水平|等级)?(?:为|是|：|:)?|'
                r'关注(?:程度|等级)(?:为|是|：|:)?|为)\s*'
                r'(低风险|中风险|高风险|正常|注意|低关注|中关注|高关注)(?=$|[的档级，,。；;！？\s])', clause)
            if match:
                claimed = {'低关注': '低风险', '中关注': '中风险', '高关注': '高风险'}.get(match[1], match[1])
                if expected is None:
                    errors.append('本病尚无通过独立审核的公开健康评分，不得用内部旧风险指数或原报告等级补写综合等级。')
                elif claimed != expected:
                    errors.append('综合疾病关注采用本次独立健康分对应的低/中/高风险，高分低风险；'
                        '不得输出旧四档或另改本病公开等级，原报告原级仍留原结果字段。')
    return list(dict.fromkeys(errors))


def medical_summary_comparison_failures(text, facts=(), assessment_context=()):
    """Compare recorded symptom themes, not two entire source inventories.

    An independently recorded sleep complaint does not become corroborated
    merely because the same B answer also contains a shared bowel complaint.
    The original B/C words and their separate source identities stay intact.
    """
    if not isinstance(text, str):
        return []
    themes = {
        '消化道': r'排便|便秘|腹泻|腹胀|腹痛|排气|大便|口腔异味|口臭|反酸|恶心|呕吐',
        '睡眠': r'入睡|睡眠|失眠|早醒|夜醒|嗜睡|睡不',
        '情绪': r'焦虑|抑郁|情绪|紧张',
        '心血管': r'胸痛|心悸|心慌|胸闷',
        '神经': r'头痛|头晕|震颤|麻木|记忆',
    }
    kinds = lambda value: {name for name, pattern in themes.items() if re.search(pattern, value)}
    recorded = [row.get('sourceDescription') or row.get('value') or '' for row in assessment_context
        if row.get('semanticType') == 'sourceReportedBackground'
        and re.search(r'症状|主诉', row.get('name') or '')]
    values = list(facts.values()) if isinstance(facts, dict) else list(facts)
    recorded.extend(fact.value for fact in values if getattr(fact, 'kind', None) == 'context'
        and re.search(r'(?:原报告|来源报告).*(?:症状|主诉)', fact.name))
    c_themes = kinds(' '.join(value for value in recorded if isinstance(value, str)))
    if not recorded:
        return []
    b_lead = r'(?:问卷|用户|本人)(?:自述|自报|填写|报告)(?:的)?'
    c_lead = r'(?:原报告|来源报告)(?:中)?(?:记载|记录|提及|列出|所列)?(?:的)?'
    agreement = r'方向一致|方向相符|相互印证|相互吻合|相一致|相符|相吻合|共同构成'
    for sentence in re.split(r'[。；;！？\n]', text):
        if not re.search(agreement, sentence):
            continue
        b, c = re.search(b_lead, sentence), re.search(c_lead, sentence)
        if b is None or c is None:
            continue
        end = re.search(agreement, sentence).start()
        first, second = sorted((b, c), key=lambda match: match.start())
        spans = {('B' if first is b else 'C'): sentence[first.end():second.start()],
            ('B' if second is b else 'C'): sentence[second.end():end]}
        stated_b, stated_c = kinds(spans['B']), kinds(spans['C'])
        if stated_b != stated_c or not stated_c <= c_themes:
            return ['医学摘要只能按双方实际共同的症状主题比较；独立问卷困扰须分开说明，不能将含不同主题的整份清单概括为一致或共同印证']
    return []


def favorable_role_failures(text, facts=(), assessment_context=()):
    """A retained advantage is neither a disease roster nor a care plan."""
    if not isinstance(text, str):
        return []
    failures = []
    if text.strip():
        # A true source observation still has to fulfil this slot's role.
        # A paragraph led by an abnormal group does not become an advantage
        # merely because a later subordinate clause mentions one high score.
        # Conversely, a real retained result may keep its necessary weak-item
        # qualification; do not reject the whole paragraph for one weak word.
        positive = re.compile(
            r'(?<!不)(?<!非)正常|阴性|未检出|未(?:见|列|列出|列为|列入|发现)[^，,。；;！？\n]{0,8}异常|'
            r'(?<!不)(?:在|处于|位于)参考范围(?:内)?|'
            r'(?:仍|尚)(?:然)?(?:有|具备|保有)[^，,。；;！？\n]{0,12}(?:能力|功能)|'
            r'保留[^，,。；;！？\n]{0,12}(?:正常|良好|较好|优势)|'
            r'(?:能力|功能)[^，,。；;！？\n]{0,8}(?:保留|维持|保持)|'
            r'保留(?:了|着|部分|一定)?[^，,。；;！？\n]{0,8}(?:能力|功能|表现)')
        shortfall = re.compile(
            r'短板|异常|偏低|偏弱|较弱|薄弱|下降|降低|升高|不均衡|失衡|缺乏|不足')
        first = None
        for clause in re.split(r'[，,。；;！？\n]', text):
            clause = clause.strip()
            if not clause or re.match(r'^(?:尽管|虽然|即使|即便)', clause):
                continue
            good, bad = positive.search(clause), shortfall.search(clause)
            if good and re.search(r'(?:并非|不是|不能称为|不属于|没有|未见|尚未达到)'
                    r'[^，,。；;！？\n]{0,12}正常', clause):
                good = None
            if good or bad:
                first = bool(good and (bad is None or good.start() <= bad.start()))
                break
        if first is not True:
            failures.append('有利因素的首个主要判断须说明有来源支持的正常或相对保留表现；'
                '整段异常、下降或短板分析不能仅因末尾提到单项较高而占用本栏。'
                '真实保留项可附必要的局部弱项限定，原值、原级及其来源仍按既有校验保留。')
    if re.search(r'(?:原报告|来源报告)[^。；;\n]{0,140}(?:病史|用药)|'
            r'(?:病史|用药)[^。；;\n]{0,60}(?:管理|随访|背景)', text):
        failures.append('有利因素只解释正常或保留结果，不以病史和用药清单作为优势依据')
    if (_SOURCE_ORDER_RECOMMENDATION.search(text) or _CROSS_CHAPTER_ACTION.search(text)
            or re.search(r'(?:管理|随访)(?:顺序|优先)|(?:管理|随访)[^。；;\n]{0,50}(?:为优先|先围绕)', text)):
        failures.append('有利因素不重排全局管理、随访或关注优先顺序')
    if re.search(r'(?:不|不能)(?:替代|代替)[^。；;\n]{0,30}(?:评估|检查|诊断|随访)', text):
        failures.append('有利因素不附加泛化临床评估免责声明')
    return failures


_SOURCE_PROCESS = re.compile(
    r'均按原报告归属保留|属于未核验的本人陈述[^。！？\n]*|'
    r'(?:需|须|应|需要)(?:要)?结合临床(?:情况|表现)?(?:进一步)?(?:核实|核对|复核)|'
    r'(?:健康管理|关注)(?:排序|顺序)[^。；;\n]{0,30}(?:总览|综合结论)[^。；;\n]{0,12}(?:安排|决定)|'
    r'(?:不宜|不能|不应)(?:仅凭|单凭)[^，,。；;！？\n]{0,20}(?:菌群|模型|检测)'
    r'[^，,。；;！？\n]{0,16}(?:判断|推断)[^，,。；;！？\n]{0,8}性质|'
    r'(?:保留|保持|维持)[^，,。；;！？\n]{0,16}(?:出处|来源归属|来源性质|自报性质|来源属性)|'
    r'(?:保留|展示|呈现|列示)(?:(?:该|本|原|这[一项条]?|上述|相应|来源|报告|检测|标记|项|条|的|原始)){0,12}'
    r'(?:记录|条目|标记|含义|数据|项目)|'
    r'(?:不|不能|不可|不应)[^，,。；;！？\n]{0,40}(?:升级|提升|改写|转写|写成|改为|转为)'
    r'(?:为|成)?(?:(?:新|新的|本次|当前|已|明确)(?:的)?)*(?:临床|疾病)?(?:诊断|确诊)')
_BODY_DIAGNOSTIC_BOUNDARY = re.compile(
    r'(?:并非|不是|不属于|不构成|不代表|不等同(?:于)?|不能(?:据此|直接|单独)?(?:作为|视为|认定为))'
    r'(?:(?:本次|当前|本轮|个人|本人|患者|新|新的|独立|直接|实际|已确认|已)(?:的)?)*'
    r'(?:临床|疾病|医学)?(?:诊断|确诊)(?:依据|结论|结果)?')
_NEGATIVE_INFERENCE = (r'(?:并非|不是|不属于|不构成|不代表|不等同(?:于)?|不等于|不意味着|'
    r'(?:不|不能|不应)(?:将|把)[^，,。；;！？\n]{0,35}(?:等同(?:于)?|视为|当作)|'
    r'(?:不能|不可|不应|无法|不足以)(?:单独|据此|直接|独立|被)*'
    r'(?:作为|用于|用来|视为|当作|判断|推断|证明|确定|证实|说明|表示|代表))')
_MEDICAL_STATE_OBJECT = r'(?:临床|医学|疾病|患病|诊断|确诊|骨密度|营养状态|营养缺乏|人体含量|体内含量|血液水平|判断依据|(?:已发生的?|临床)?感染)'
_NEGATIVE_STATE_BOUNDARY = re.compile(_NEGATIVE_INFERENCE + r'[^，,。；;！？\n]{0,45}' + _MEDICAL_STATE_OBJECT)
_CATEGORY_BOUNDARY = re.compile(_MEDICAL_STATE_OBJECT + r'[^，,。；;！？\n]{0,45}'
    r'(?:并非|不是|不属|不在)(?:同一|相同|一个|同个)[^，,。；;！？\n]{0,8}(?:层面|概念|维度|回事|等价)')
_SOURCE_ORDER_RECOMMENDATION = re.compile(
    r'(?:当前|本次|整体|总体)?关注顺序|优先关注|'
    r'(?:应|宜|需|建议)[^，,。；;！？\n]{0,16}优先|'
    r'(?:首先|其次)[^，,。；;！？\n]{0,30}(?:关注|管理|评估)|'
    r'(?:首先|先围绕)[^。；;！？\n]{1,80}(?:其次|再|同时)|'
    r'(?:关注|管理)(?:重点|方向)[^，,。；;！？\n]{0,8}(?:应|需|宜|建议)[^，,。；;！？\n]{0,8}(?:放在|围绕|转向)')
_RESEARCH_SCOPE = re.compile(r'研究|队列|受试者|入组|纳入人群|研究人群|体外|培养')
_SINGLE_CHAPTER_AGGREGATION = re.compile(
    r'(?:本章|本节|本组|整体|全组|全章)[^，,。；;！？\n]{0,35}(?:仅|只有|均|全部|未见|没有)'
    r'[^，,。；;！？\n]{0,18}(?:项|记录|条目|异常|信号|问题)')
_CROSS_CHAPTER_ACTION = re.compile(
    r'(?:健康管理|整体管理|全局管理|既有疾病)[^，,。；;！？\n]{0,35}(?:随访|持续观察|关注重点|为重点)|'
    r'(?:不改变|不影响|仍以|应以)[^，,。；;！？\n]{0,20}(?:关注顺序|全局优先|随访优先)')


def single_observation_path(path):
    path = tuple(path)
    return (len(path) == 4 and type(path[2]) is int and path[:2] in {
        ('microbiomeAssessment','antimicrobialResistance'),
        ('microbiomeAssessment','healthMetrics'), ('medicalAssessment','findings')}
        or len(path) == 6 and path[:2] == ('diseaseRiskAssessment','categories') and path[3] == 'items')


def _source_gene_function_failures(text, facts, assessment_context, *, evidence_refs=(), sentences=None):
    """A detected marker list does not establish the markers' biological roles.

    Source classifications and literal source explanations remain usable.
    Separately cited mechanism sentences go through the existing original-text
    reviews; a cited sibling cannot supply an uncited sentence's attribution.
    """
    known = facts if isinstance(facts, dict) else {fact.fact_id: fact for fact in facts}
    own = {fid for row in assessment_context for fid in row.get('factIds', [])
        if row.get('resultKind') == 'geneMarker' or fid in known
        and known[fid].kind in {'context', 'function'}
        and '检出基因' in known[fid].value}
    if not own:
        return []
    sources = [known[fid] for fid in own if fid in known]
    originals = [value for fact in sources for value in (fact.name, fact.value) if value]
    category_names = ' '.join(fact.name for fact in sources)
    function = (r'(?:多重耐药|耐药|抗性|外排|转运|生物膜|荚膜|黏附|毒素合成|毒素分泌|毒素产生|'
        r'毒性|屏障损伤|屏障受损|黏膜损伤|黏膜受损|免疫调节|炎症反应)')
    related = r'(?:与|同)[^，,。；;！？\n]{0,24}' + function + r'[^，,。；;！？\n]{0,10}(?:相关|有关|关联)'
    attribute = (r'(?:参与|介导|负责|编码|控制|促进|抑制|用于|具有|具备|执行|承担|属于|归为|归属|'
        r'功能(?:为|是)|作用(?:为|是))')
    assigned = attribute + r'[^，,。；;！？\n]{0,24}' + function
    named = r'(?:基因|基因信号|基因标记|这些基因|上述基因|本项)(?:是|为)[^，,。；;！？\n]{0,16}' + function
    attributed = function + r'[^，,。；;！？\n]{0,8}(?:相关|功能|作用)(?:的)?(?:基因|标记|信号)'
    items = sentences if isinstance(sentences, list) and sentences else [
        {'text': text, 'evidenceRefs': evidence_refs}]
    for item in items:
        if not isinstance(item, dict) or item.get('evidenceRefs') or not isinstance(item.get('text'), str):
            continue
        for clause in re.split(r'[，,。；;！？\n]', item['text']):
            clause = clause.strip()
            if not clause or any(clause in original for original in originals):
                continue
            # A gene marker and a separate species abundance do not identify
            # the same strain or establish that strain's properties. This
            # entity/predicate boundary does not depend on a function-name
            # vocabulary; a cited research item keeps its separate review.
            if re.search(r'(?:菌株|\bstrains?\b)[^，,。；;！？\n]{0,12}' + attribute
                    + r'[^，,。；;！？\n]+', clause, re.I):
                return ['基因标记与独立菌丰度不能建立同一菌株的性质或功能；'
                    '本句须有同条原事实明示支持，或独立作为有适用引文的研究句接受完整审核，'
                    '不能借兄弟句引文将源内状态判断升级为菌株级能力。']
            if not any(re.search(pattern, clause) for pattern in (related, assigned, named, attributed)):
                continue
            # The existing name can establish its classification (for example
            # "耐药基因"); it cannot add an unrecorded efflux/biofilm function.
            functions = {match.group() for match in re.finditer(function, clause)}
            if any(value not in category_names for value in functions):
                return ['基因名单或检出状态不能自行增加耐药、外排、转运等功能归属；'
                    '仅保留原记录明确说明或有本句适用原文独立支持的基因功能，源内AI判断仍可描述真实状态与具体对照。']
    return []


def _functional_editorial_failures(text, path, assessment_context, facts=()):
    """Keep actual study scope and compare only members that really exist."""
    if (not isinstance(text, str) or tuple(path[:2]) != ('functionalPathways', 'pathways')
            or tuple(path[-1:]) != ('healthMeaning',)):
        return []
    failures = []
    if path[2] == 8 and re.search(r'(?:其余|其他|其它|剩余)(?:的)?(?:功能(?:项|项目|指标|结果)?|代谢项目)'
            r'(?:均|皆|都|仍|保持|为|处于|表现|基本|是)*正常', text):
        known = {fact.fact_id: fact for fact in (facts.values() if isinstance(facts, dict) else facts)}
        observations = {}
        for row in assessment_context:
            for item in [row, *row.get('members', [])]:
                ids = set(item.get('factIds') or ())
                if (item.get('value') in (None, '') or not item.get('name') or not ids
                        or known and not any(fid in known and known[fid].kind == 'function' for fid in ids)):
                    continue
                key = tuple(sorted(ids))
                observations.setdefault(key, item)
        named = {key for key, row in observations.items() if row['name'] in text}
        remaining = [row for key, row in observations.items() if key not in named]
        if (len(observations) < 2 or not remaining or any(
                (row.get('assessmentLevel') if row.get('assessmentOrigin') in {'source','report','reference'}
                    else row.get('sourceStatus')) != '正常' for row in remaining)):
            failures.append('本通路“其余功能项正常”须有实际存在且已确认正常的剩余功能项目；'
                '单个功能项、菌种或基因背景不构成其余正常功能，不补写不存在的组内对照。')
    known = facts if isinstance(facts, dict) else {fact.fact_id: fact for fact in facts}
    exact_management = any(row.get('semanticType') == 'approvedFunctionalManagementContext'
        and row.get('text') == text and row.get('evidenceKeys') and row.get('evidenceRefs')
        and row.get('behaviorFactQuotes') and all(fid in known and isinstance(quote, str)
            and quote in known[fid].value for fid, quote in row['behaviorFactQuotes'].items())
        for row in assessment_context)
    if tuple(path) == ('functionalPathways', 'pathways', 2, 'healthMeaning') and not exact_management and (
            re.search(r'摄入|饮食|排便规律|补充', text)
            and re.search(r'维持|保持|选择|建议|强化|关注重点|后续关注|而非', text)):
        failures.append('没有同真实行为已审管理上下文时，短链脂肪酸功能意义不承担饮食或补充选择；该原始sentence item须完整重写为'
            '已确认功能状态或原文明示生理过程，不剪去管理尾句后保留未经支持的结论。')
    if re.search(r'(?:人体|人类)(?:层面|水平)?(?:的)?(?:实际|生理)?(?:意义|影响|作用|效应)'
            r'(?:仍|尚)?(?:需|需要|有待)(?:更多|进一步)?(?:研究|证据)(?:予以)?(?:确认|验证|证实|阐明)', text):
        failures.append('功能意义不添加泛化人体外推局限尾句；须按实际原文重写完整科学句，'
            '保留动物或体外研究的真实层级与有用发现，不直接删除限定后写成人体作用。')
    comparison = re.search(r'(?:高于|优先于)[^。；;！？\n]{0,12}本组(?:的)?其他(?:方向|项目|环节)', text)
    if comparison:
        whole = re.search(r'(?:这一组|本组|该组|本功能域)(?:中)?'
            r'(?P<count>\d+|[一二两三四五六七八九十]+)?项?(?:均|全部|都)(?:呈|为|处于)?'
            r'(?:偏弱|较弱|弱|极低|偏低)', text[:comparison.start()])
        if whole:
            observations = [row for row in assessment_context if row.get('value') not in (None, '')
                and row.get('factIds') and row.get('name')]
            observed_ids = {fid for row in observations for fid in row['factIds']}
            groups = [row for row in assessment_context if row.get('semanticType') == 'nutrientGroup'
                and len(row.get('members') or []) >= 2
                and {fid for member in row['members'] for fid in member.get('factIds', [])} == observed_ids]
            chinese = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
            count = whole['count']
            count = int(count) if count and count.isdecimal() else chinese.get(count)
            from modules.report.evaluation_catalog import LOW_FUNCTION_LEVELS
            if any((whole['count'] is None or count == len(group['members']))
                    and all(member.get('assessmentLevel') in LOW_FUNCTION_LEVELS for member in group['members'])
                    for group in groups):
                failures.append('本组已列成员全部构成上述弱项，不存在的组内对照不能承担优先比较；'
                    '保留真实整组短板判断，只有确有剩余成员及对应状态时才比较本组其他方向。')
    return failures


def _detection_named_rows(text, rows, *, scope_text=''):
    """Resolve source names, including a marker caption in explicit gene scope."""
    mentions = []
    for index, row in enumerate(rows):
        names = {part.strip() for field in ('name', 'scientificName')
            for part in str(row.get(field) or '').split(' / ') if len(part.strip()) >= 2}
        for name in tuple(names):
            names.update(re.findall(r'[（(]([A-Z][A-Z0-9 /-]{1,12})[）)]', name))
            short = re.sub(r'[（(][A-Za-z][A-Za-z0-9 _/-]*[）)]', '', name).strip()
            explicit_marker = re.search(re.escape(short) + r'(?:相关)?(?:基因|遗传标记|DNA)', text)
            same_gene_scope = (row.get('objectType') == 'geneMarker'
                and re.search(r'(?:本组|本项|这组|这些|上述|毒力)(?:[^。；;！？\n]{0,10})基因|基因标记', scope_text))
            if short != name and (explicit_marker or same_gene_scope):
                names.add(short)
        for name in names:
            pattern = re.escape(name)
            if name[0].isascii() and name[0].isalpha():
                pattern = r'(?<![A-Za-z])' + pattern
            if name[-1].isascii() and name[-1].isalpha():
                pattern += r'(?![A-Za-z])'
            mentions.extend((match.start(), match.end(), index)
                for match in re.finditer(pattern, text, re.I))
    named = set()
    for start, end, index in mentions:
        if not any(other_start <= start and end <= other_end
                and other_end - other_start > end - start for other_start, other_end, _ in mentions):
            named.add(index)
    return [row for index, row in enumerate(rows) if index in named]


def _detection_context_rows(text, rows, facts, bound_fact_ids, *, named_subject=False, scope_text=''):
    """Bind each claim to its own observations; a panel ND cannot cover members."""
    known = set(facts) if isinstance(facts, dict) else {
        fact.get('factId', fact.get('fact_id')) if isinstance(fact, dict)
        else getattr(fact, 'fact_id', None) for fact in facts}
    if (not isinstance(bound_fact_ids, (list, tuple, set, frozenset)) or not bound_fact_ids
            or any(not isinstance(fid, str) or fid not in known for fid in bound_fact_ids)):
        return rows
    bound = set(bound_fact_ids)
    selected = [row for row in rows if bound.intersection(row.get('factIds') or ())]
    if not selected:
        return rows
    named = _detection_named_rows(text, rows, scope_text=scope_text)
    if named_subject and named:
        return named
    return selected + [row for row in named if row not in selected]


def _quantified_detection_rows(text, rows, typed_members, facts, bound_fact_ids, *, scope_text=''):
    """A source-bound 'some members ND' asserts existence, never all members.

    This only scopes the existing absence check. Independent source review
    still has to approve every original sentence and its exact fact bindings.
    Explicitly named subjects always retain their own original observations.
    """
    known = set(facts) if isinstance(facts, dict) else {getattr(fact, 'fact_id', None) for fact in facts}
    if (not isinstance(bound_fact_ids, (list, tuple, set, frozenset)) or not bound_fact_ids
            or any(not isinstance(fid, str) or fid not in known for fid in bound_fact_ids)):
        return None
    if not re.search(r'未检出|未检测到|没有检测到|未查见|阴性', text):
        return None
    whole_group = re.search(r'(?:所有|全部|各个|每个|所有的)(?:[^，,。；;！？\n]{0,8})(?:成员|项目|基因)|'
        r'本组(?:均|全部|全都|都)', text)
    anonymous_universal = (re.search(r'(?:成员|项目|基因)(?:均|全|都)', text)
        and not _detection_named_rows(text, typed_members, scope_text=scope_text))
    if whole_group or anonymous_universal:
        # Quantifying the whole group must check every actual scoped member,
        # even if the writer only cited a convenient negative subset.
        return typed_members or None
    if _detection_named_rows(text, rows, scope_text=scope_text):
        return None
    if not re.search(r'(?:部分|一些|某些)(?:[^，,。；;！？\n]{0,10})(?:成员|项目|基因)|'
            r'(?:检出与未检出|未检出与检出)(?:[^，,。；;！？\n]{0,8})(?:并存|共存)', text):
        return None
    bound = set(bound_fact_ids)
    members = [row for row in typed_members if bound.intersection(row.get('factIds') or ())]
    # No inference from zero/normal or the aggregate's state is permitted.
    negative = [row for row in members if not detection_absence_failures(
        text, value=row.get('value'), source_status=row.get('sourceStatus'))]
    return negative[:1] or None


def function_display_rule_prose(text):
    """Classify grading provenance prose, never remove it from an assertion."""
    return isinstance(text, str) and bool(re.search(
        r'原始(?:升降)?方向|显示(?:分级|档位)|(?:评分|分数|能力)显示(?:为|成)|'
        r'(?:本报告|模板)[^。；;！？\n]{0,16}(?:分档|档位|显示分级)|'
        r'(?:不宜|不应|不能|不要)(?:把|将)[^。；;！？\n]{0,22}'
        r'(?:整组|本组|该组|这一组)[^。；;！？\n]{0,16}'
        r'(?:概括|归纳|描述|表述|写成|写为)', text))


def mixed_source_direction_list_failures(text, facts):
    """A final shared direction cannot silently own opposite-status members.

    Read only explicit source labels. A preceding member's independently
    stated direction remains its own; no values or new grades are inferred.
    """
    if not isinstance(text, str):
        return []
    values = facts.values() if isinstance(facts, dict) else facts
    directions = {}
    for fact in values:
        name = fact.get('name') if isinstance(fact, dict) else getattr(fact, 'name', '')
        status = fact.get('status') if isinstance(fact, dict) else getattr(fact, 'status', '')
        direction = ('up' if re.fullmatch(r'偏高|升高|增高|高', str(status or '')) else
            'down' if re.fullmatch(r'偏低|降低|下降|低', str(status or '')) else None)
        if direction and isinstance(name, str):
            for alias in name.split(' / '):
                if len(alias.strip()) >= 2:
                    directions.setdefault(alias.strip(), set()).add(direction)
    predicates = {'up': r'偏高|升高|增高|增加|↑', 'down': r'偏低|降低|下降|减少|↓'}
    for clause in re.split(r'[，,。；;！？\n]', text):
        if '、' not in clause:
            continue
        mentions = [(m.start(), m.end(), next(iter(ds))) for name, ds in directions.items()
            if len(ds) == 1 for m in re.finditer(re.escape(name), clause)]
        mentions = sorted(set(row for row in mentions if not any(
            start <= row[0] and row[1] <= end and end-start > row[1]-row[0]
            for start, end, _ in mentions)))
        if len(mentions) < 2 or {row[2] for row in mentions} != {'up', 'down'}:
            continue
        tail = clause[mentions[-1][1]:]
        shared = next((direction for direction, pattern in predicates.items()
            if re.search(pattern, tail)), None)
        if shared is None:
            continue
        for start, end, actual in mentions:
            if actual == shared:
                continue
            # An explicit local predicate before the next list delimiter
            # prevents the last member's predicate from owning this member.
            separators = list(re.finditer(r'、|和|与|及', clause))
            left = max((m.end() for m in separators if m.end() <= start), default=0)
            right = min((m.start() for m in separators if m.start() >= end), default=len(clause))
            own = clause[left:right]
            # A composition predicate before a list is ambiguous: the tail
            # can be read as belonging to either the last member or all of
            # them. Ask for a whole rewrite; do not choose a medical reading.
            composition = (re.search(r'(?:偏向|偏重于)\s*$', clause[left:start])
                or re.search(r'^\s*(?:为主|占优|占优势)', clause[end:right]))
            if not re.search(predicates[actual], own) and composition:
                actual_directions = '、'.join(clause[a:b] + ('偏高' if direction == 'up' else '偏低')
                    for a, b, direction in mentions)
                return ['ambiguous_direction_scope：组成判断与另一项目的方向用顿号连接，谓词作用域不明确；'
                    '本句实际来源方向为' + actual_directions + '。请将各项写成由逗号及而、分号或句号分隔的独立判断，'
                    '完整重写本段后重新审核；不能选择一种读法直接发布，也不删除连接词后沿用结论。']
            if not re.search(predicates[actual], own):
                return ['同一顿号列表中的来源方向不同，不能共用末尾的升高或降低谓语；'
                    '按实际项目分别明确方向，或用方向分化概括，不要求复抄数值。完整句须重新审核。']
    return []


def measurement_object_semantics_failures(text, facts=(), assessment_context=()):
    """A true value cannot authorize a different measurement or a new grade."""
    if not isinstance(text, str):
        return []
    rows = list(facts.values() if isinstance(facts, dict) else facts)
    get = lambda row, key: row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    measured = [row for row in rows if get(row, 'kind') in {'function', 'microbe'}]
    names = ' '.join(str(get(row, 'name') or '') for row in measured)
    failures = []
    functional = any(get(row, 'kind') == 'function' for row in measured)
    if functional:
        for sentence in re.split(r'[。；;\n]', text):
            if (re.search(r'(?:您的?|本次|本项|当前)[^。；;]{0,30}'
                    r'(?:抗炎|免疫|解毒|防御|营养吸收)[^。；;]{0,8}(?:能力|功能|力)'
                    r'[^。；;]{0,15}(?:正常|充足|良好|不足|低下|受损)', sentence)
                    and not re.search(r'评分|分值|合成分|模型|估计|\d+(?:\.\d+)?\s*分', sentence)):
                failures.append('菌群相关功能分不直接测量人体抗炎、免疫、解毒或吸收能力；'
                    '本次状态须明确为真实功能评分或模型估计，不能将原项目名扩写成人体功能正常/受损。')
    absolute = re.search(r'菌群总量|总菌量|绝对(?:菌群)?丰度|微生物负荷', names)
    if (not absolute and re.search(r'种类|物种|丰富度|多样性', names)
            and re.search(r'(?:问题不在|没有问题|正常|充足|不少)[^。；;]{0,12}(?:菌群总量|总菌量)|'
                r'(?:菌群总量|总菌量)[^。；;]{0,12}(?:正常|充足|没有问题|不是问题)', text)):
        failures.append('物种数、丰富度或多样性不测量绝对菌量；未提供总量测量时不能据此排除总菌量问题。')
    for row in measured:
        label = str(get(row, 'status') or '')
        reference = str(get(row, 'reference_range') or '')
        value = str(get(row, 'value') or '')
        if not re.match(r'偏向|分型|类型', label):
            continue
        bounds = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*[-~～—]\s*([+-]?\d+(?:\.\d+)?)\s*', reference)
        if not bounds or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value.strip()):
            continue
        if not float(bounds[1]) <= float(value) <= float(bounds[2]):
            continue
        aliases = [part.strip() for part in str(get(row, 'name') or '').split(' / ') if part.strip()]
        if any('F/B' in name or re.search(r'厚壁.*拟杆', name) for name in aliases):
            aliases.extend(['F/B', '厚壁菌门', '拟杆菌门'])
        for sentence in re.split(r'[。；;\n]', text):
            if not any(name in sentence for name in aliases):
                continue
            if re.search(r'偏离|短板|异常|失衡', sentence) and not re.search(
                    r'不是|并非|未(?:见|超)|不属|不等于|不代表|不能', sentence):
                failures.append('原分类或组成偏向且数值仍在参考范围内，不能仅据该标签新增异常、偏离或短板判断。')
                break
    return list(dict.fromkeys(failures))


def content_rule_failures(text, *, path=(), facts=(), evidence_refs=(), sentences=None,
                          assessment_context=(), bound_fact_ids=None):
    """Reject a generated source-only block, never strengthen it by deletion.

    Each cited sentence keeps the ordinary independent evidence review. Literal
    original records and concrete uncertainty/negative findings remain facts;
    this gate covers only method/process prose and cross-chapter ordering.
    """
    from modules.report.evaluation_priority import priority_boundary_failures
    failures = list(priority_boundary_failures(text, path=path, facts=facts,
        assessment_context=assessment_context))
    failures.extend(reported_symptom_attribution_failures(text, facts, assessment_context))
    failures.extend(measurement_object_semantics_failures(text, facts, assessment_context))
    failures.extend(_source_gene_function_failures(text, facts, assessment_context,
        evidence_refs=evidence_refs, sentences=sentences))
    failures.extend(_functional_editorial_failures(text, path, assessment_context, facts))
    if (content_role(path) == 'general_function' and function_display_rule_prose(text)
            and not measurement_scope_text_allowed(text, path=path, facts=facts,
                assessment_context=assessment_context)):
        failures.append('功能正文不展示原始方向、显示档位或模板分级等处理文案；'
            '整句重写为对象的实际升降状态及意义，原值和原级仍完整留表，不裁词后保留未经重审的句子。')
    if tuple(path) == ('microbiomeAssessment', 'structureInterpretation', 'text'):
        failures.extend(mixed_source_direction_list_failures(text, facts))
    if tuple(path) == ('microbiomeAssessment', 'scfaInterpretation', 'text') and isinstance(text, str):
        if len([part for part in re.split(r'[。！？!?]', text) if part.strip()]) > 3:
            failures.append('短链脂肪酸解读用至多三句说明实际功能状态和有适用原文的同对象生理作用，完整分值留原表。')
        source_only = ('\n'.join(item.get('text', '') for item in sentences
            if isinstance(item, dict) and not item.get('evidenceRefs'))
            if sentences else '' if evidence_refs else text)
        if re.search(r'日常饮食|饮食(?:安排|调整|关系)|排便|便秘|反酸|腹胀', source_only):
            failures.append('短链脂肪酸小结只采用本行已绑定的C结果，不从其他节点借饮食、排便或反酸关系；整句重新生成，不裁去未绑定前提。')
        if re.search(r'(?:建议|应当|需要|可以|可考虑|无需|不需要|不应)[^。；;\n]{0,12}'
                r'(?:补充|增补|提升|提高|服用)|(?:补充|增补|服用)[^。；;\n]{0,12}(?:剂|产品|益生菌)', text):
            failures.append('短链脂肪酸小结不提出补充、提升等策略或策略边界；整句改为真实共同状态和原方向，不切尾保留其判断。')
        if re.search(r'(?:丁酸|丙酸|乙酸)[^。；;\n]{0,20}(?:最高|最低|相近|接近|更高|更低|高于|低于)', text):
            failures.append('短链脂肪酸共同状态小结不再复述单项相对高低或相近；原始项目与分值已完整留表。')
    if tuple(path[:2]) == ('healthOverview','keyFindings') and isinstance(text,str):
        values = facts.values() if isinstance(facts, dict) else facts
        literal = any(text.strip() in getattr(fact, 'value', '') for fact in values if text.strip())
        if not literal and re.search(r'(?:现有|当前|本次)?资料(?:不足|不充分|有限|缺乏)[^。；;！？\n]{0,15}'
                r'(?:判断|确定|解释|证实)[^。；;！？\n]{0,15}(?:关联|因果|机制)', text):
            failures.append('核心提示不以资料不足判断关联机制的通用边界代替状态判断；包含它及前面关系断言的完整item须重新生成，不裁去尾句。')
        if (not literal and re.search(r'(?:关注|了解|核对|检查|筛查)[^。；;！？\n]{0,28}'
                r'是否[^。；;！？\n]{0,8}(?:报警|红旗)', text)):
            failures.append('核心提示不把未询问的报警/红旗扩为新增查核流程；只归纳已提供的本人状态。')
        if (re.search(r'偏高|升高', text) and re.search(r'偏低|降低', text)
                and re.search(r'(?:彼此|两者|各项|这些)[^。；;！？\n]{0,12}'
                    r'(?:(?:方向|趋势)(?:一致|相同)|同向)', text)):
            failures.append('本行同时有升高和降低的真实结果，不能将它们称为彼此方向一致；可判断共同存在异常或分布不均，保留各自原方向。')
        if re.search(r'(?:本次资料|全部资料|所有资料|本次健康评估|整份报告|全报告)'
                r'[^，,。；;！？\n]{0,18}唯一[^，,。；;！？\n]{0,14}(?:关注点|异常|风险|问题)', text):
            failures.append('本行只解释既定的局部风险或异常，不能将该项称为整份资料中唯一的关注点；保留本行实际状态与关注意义。')
        if re.search(r'(?:本章|全章|全报告|整份报告).{0,8}(?:首要|最(?:值得)?优先|第一位)', text):
            failures.append('本行指标比较只支持本组重点，不能据此重排整章或全报告的关注顺序。')
        if len(re.sub(r'\s+', '', text)) > 180 or len(re.findall(r'[。！？]', text)) > 2:
            failures.append('核心提示只写一至两句、180字内，解释本行重点；完整指标名册留原表，不增加整段评级过程或来源方法说明。')
        known = facts.values() if isinstance(facts,dict) else facts
        names = {str(getattr(fact,'name','')).split(' / ')[0] for fact in known
            if getattr(fact,'kind',None) in {'function','microbe','clinical'}
            and len(str(getattr(fact,'name','')).split(' / ')[0]) >= 2
            and str(getattr(fact,'name','')).split(' / ')[0] in text}
        if len(names) > 3:
            failures.append('核心提示不复述超过三个已列项目，只选少量依据说明差异或关注意义；其余真实明细继续完整保留主表。')
        if '按本报告评估标准' in text:
            failures.append('核心提示直接说明已确认状态与关注意义，不重复评级方法前缀。')
    if tuple(path) == ('healthOverview', 'summaries', 'medical', 'text'):
        failures.extend(medical_summary_comparison_failures(text, facts, assessment_context))
    if tuple(path) == ('comprehensiveConclusion', 'favorableFactors', 'text'):
        failures.extend(favorable_role_failures(text, facts, assessment_context))
    if tuple(path[:2]) == ('microbiomeAssessment', 'healthMetrics') and isinstance(text, str):
        if re.search(r'(?:不等同(?:于)?|不等于|(?:并)?不(?:表示|代表|意味着)|而非|并非|不是)'
                r'[^。；;！？\n]{0,45}(?:整菌|携带[^。；;！？\n]{0,16}菌株|菌株[^。；;！？\n]{0,8}检出)', text):
            failures.append('健康意义不展示模板式整菌或菌株边界声明；须保留该原始sentence item的全部限定整句重写，'
                '直接称本项基因标记并解释有来源的具体关系，不删除限定后保留更强结论。')
        known = facts if isinstance(facts, dict) else {fact.fact_id: fact for fact in facts}
        own = {fid for item in assessment_context if item.get('semanticType') == 'healthSummary'
            for fid in item.get('factIds', [])}
        gene_panel = any(fid in known and known[fid].kind == 'context'
            and re.search(r'基因|毒素', known[fid].name) for fid in own)
        if gene_panel and re.search(r'(?:该菌|此菌|这种菌|该菌种|该菌株|该细菌)(?:的)?(?:检出|被检出|检测到|检出了)|'
                r'(?:检出|检测到)(?:了)?(?:该菌|此菌|这种菌|该菌种|该菌株|该细菌)', text):
            failures.append('本行是基因标记检出，不是整菌或携带这些基因的菌株检出；保留本行基因与实际功能对照，不把独立菌丰度与基因记录连接成菌株携带结论')
    if tuple(path[:2]) == ('microbiomeAssessment', 'antimicrobialResistance') and isinstance(text,str):
        # This slot owns a metagenomic gene measurement, never a person's or
        # an isolated pathogen's antimicrobial susceptibility phenotype.
        for clause in re.split(r'[，,。；;！？\n]',text):
            if re.search(r'是否|能否|未来|如需|若需',clause):
                continue
            person=re.search(r'(?:本人|个人|受检者|人体|宿主|机体|您的|该患者)'
                r'[^，,。；;]{0,32}(?:耐药(?!基因)|药敏(?:结果|试验|检测)?(?:提示|显示|判定|为))',clause)
            pathogen=re.search(r'(?:提示|说明|证明|证实|显示|判定)[^，,。；;]{0,16}'
                r'(?:病原(?:体|菌)|致病菌|分离株|感染菌)[^，,。；;]{0,24}耐药(?!基因)|'
                r'(?:病原(?:体|菌)|致病菌|分离株|感染菌)[^，,。；;]{0,24}已(?:经)?耐药',clause)
            if person or pathogen:
                failures.append('耐药基因测量不能据此判定本人或病原菌的临床药敏耐药；保留基因项目、原值及原标记，不将其改成个人或分离株的耐药结论')
    if single_observation_path(path):
        rows = [row for row in assessment_context if 'value' in row and row.get('factIds')]
        typed_members = []
        for context in assessment_context:
            if context.get('semanticType') != 'healthStateSourceContext':
                continue
            states = [context.get('aggregateState'), *(context.get('memberStates') or []),
                *(context.get('geneGroupStates') or [])]
            rows.extend(state for state in states if isinstance(state, dict)
                and 'value' in state and state.get('factIds') and state not in rows)
            typed_members.extend(state for state in states if isinstance(state, dict)
                and state.get('objectType') in {'measuredMember', 'geneMarker'}
                and 'value' in state and state.get('factIds') and state not in typed_members)
        claims = [item for item in sentences if isinstance(item, dict) and isinstance(item.get('text'), str)] \
            if isinstance(sentences, list) and sentences else [{'text': text}]
        for claim in claims:
            claim_text = claim.get('text')
            if not isinstance(claim_text, str):
                continue
            ids = claim.get('factIds', bound_fact_ids)
            clauses = re.split(r'[，,；;。！？\n]', claim_text) if ids else [claim_text]
            for clause in clauses:
                own_rows = _quantified_detection_rows(clause, rows, typed_members, facts, ids, scope_text=claim_text)
                if own_rows is None:
                    own_rows = _detection_context_rows(clause, rows, facts, ids, named_subject=True, scope_text=claim_text)
                failures.extend(failure for row in own_rows for failure in detection_absence_failures(
                    clause, value=row.get('value'), source_status=row.get('sourceStatus')))
                named_rows = _detection_named_rows(clause, rows, scope_text=claim_text)
                if (named_rows and re.search(r'检出|检测到|查见|阳性', clause)
                        and not re.search(r'未检出|未检测到|没有检测到|未查见|阴性|'
                            r'不能|不可|不代表|不等于|不意味着|是否|未必|没有|不确定', clause)
                        and all(str(row.get('sourceStatus') or '').strip() not in {'检出', '阳性'}
                            and any(re.match(r'^\s*(?:未检出|未检测到|没有检测到|未查见|阴性)(?:[（(；;，,。:：\s]|$)',
                                str(part)) for part in (row.get('value'), row.get('sourceStatus'))
                                if part is not None) for row in named_rows)):
                    failures.append('明确原未检出/阴性的具名对象不能改写为已检出/阳性；保留该对象的真实来源状态')
    if isinstance(sentences, list) and sentences:
        failures.extend(failure for row in sentences if isinstance(row, dict)
            for failure in _source_content_failures(row.get('text'), path=path, facts=facts,
                evidence_refs=row.get('evidenceRefs') or (), assessment_context=assessment_context))
    else:
        failures.extend(_source_content_failures(text, path=path, facts=facts,
            evidence_refs=evidence_refs, assessment_context=assessment_context))
    return list(dict.fromkeys(failures))


def _source_content_failures(text, *, path, facts, evidence_refs, assessment_context=()):
    if not isinstance(text, str):
        return []
    values = facts.values() if isinstance(facts, dict) else facts
    originals = [getattr(fact, 'value', None) if not isinstance(fact, dict) else fact.get('value')
        for fact in values]
    failures = []
    clauses = [(part.strip(), sentence) for sentence in re.split(r'[。；;！？\n]', text)
        for part in re.split(r'[，,]', sentence)]
    for clause, sentence in clauses:
        if not clause or any(isinstance(value, str) and clause in value for value in originals):
            continue
        research_scope = bool(_RESEARCH_SCOPE.search(sentence) and not re.search(
            r'本次|本例|本人|个人|受检者|该患者|本报告|原报告', clause))
        if (_SOURCE_PROCESS.search(clause) or _CATEGORY_BOUNDARY.search(clause) or (
                not research_scope and (
                    _NEGATIVE_STATE_BOUNDARY.search(clause)
                    or _BODY_DIAGNOSTIC_BOUNDARY.search(clause)
                    or is_assessment_method_clause(clause)))):
            failures.append('来源综合正文含输出过程或泛化诊断边界说明；须整段重写为真实状态、关系和本章判断，不能删限定词后保留其医学结论')
        if (source_comparison_path(path) and not research_scope
                and re.search(r'(?:全局|整份报告|所有疾病|全部问题)[^。；]{0,18}(?:优先|排序|第一)', clause)):
            failures.append('类别解读围绕本类疾病的症状、诱因和改善方向，不在各类别重复整份报告的全局优先清单')
        if (single_observation_path(path) and not research_scope and (
                _SINGLE_CHAPTER_AGGREGATION.search(clause)
                    and not (normal_resistance_group_context(path, assessment_context)
                        and not re.search(r'本章|本节|整体|全章', clause))
                or _CROSS_CHAPTER_ACTION.search(clause))):
            failures.append('单项解释只说明本项实际状态和有依据的意义，不扩大为整章数量、整体正常结论或其他章节的随访与关注顺序')
    return list(dict.fromkeys(failures))


def native_function_judgment_failures(text, assessment_context):
    """Keep a favourable predicate with its own member/group, not a neighbour.

    This is a necessary source-direction guard, never approval of a subset's
    grade or health significance. Full source, group and semantic reviews still
    verify the claimed normal members and every new medical relationship.
    """
    groups = [row for row in assessment_context if row.get('semanticType') == 'nativeNutritionState']
    favourable = r'优势|有利|良好|稳定|相对保留|保留(?:项|的部分|表现|能力|功能|优势|正常)|保留.{0,5}(?:正常|能力|功能)|正常表现|正常功能|正常状态'
    failures = []
    for clause in re.split(r'[，,；;。！？!\n]|(?:但是|然而|不过|而(?=[^，,；;。]{0,30}(?:正常|保留)))', text):
        for group in groups:
            affected = [member for member in group.get('members', [])
                if member.get('direction') in {'up','down'} and member.get('name') in clause]
            for member in affected:
                direction = (r'升高|偏高|向上|↑' if member['direction'] == 'up'
                    else r'下降|降低|偏低|向下|↓|低于正常|未达(?:到)?正常')
                # The complete authored sentence may explicitly contrast its
                # grade with its original direction in a following clause.
                if re.search(r'正常(?:档|分级|水平|项目|项)?|合成能力强|强档', clause) and not re.search(direction, text):
                    failures.append('提及原始清单偏离项的正常/强评分分档时，须同时说明该项实际升高或降低方向；不能只列模板档位掩盖来源异常。')
            unlisted = [member['name'] for member in group.get('members', [])
                if member.get('direction') is None and member.get('name') in clause]
            group_name = re.sub(r'(?:合成|分解|代谢|能力|和|与|（.*?）|\(.*?\))', '', group.get('name', ''))
            explicit_whole = bool(re.search(r'整体|整组|全部|全组|所有|均(?:为|无|正常)|都(?:为|正常)', clause))
            partial = bool(re.search(r'部分|个别|少数|若干|一些|某些|多种|多项|多个|成员|单项|子项|中的|组内', clause))
            local = len(groups) == 1 and re.search(r'本组|本通路|本功能域|这一组|该组|组内', clause)
            whole_group = bool((group_name and group_name in clause or local)
                and group.get('sourceStatus') == '异常'
                and (explicit_whole or not partial and not unlisted))
            if (affected or whole_group) and re.search(
                    r'不(?:属于|是)[^。；;！？\n]{0,12}短板|(?:没有|未见|无)[^。；;！？\n]{0,8}(?:短板|弱项)|'
                    r'无(?:需|须)[^。；;！？\n]{0,8}关注|不需[^。；;！？\n]{0,8}关注', clause):
                failures.append('本组已有原始异常方向，不能因能力分档正常或强就判没有短板、无需关注；直接说明评分状态与实际异常方向。')
            positive = any(not re.search(r'(?:不|未|非|不能|不应).{0,12}$', clause[:match.start()])
                for match in re.finditer(favourable, clause))
            if (affected or whole_group) and positive:
                failures.append('同源原始异常清单含升高/降低方向，不能因模板正常或强档将该项或整组写成健康优势；分数档位和原始方向分别如实说明。')
            normal_claim = re.sub(r'(?<!不)(?<!非)(?:低于|未达(?:到)?)正常(?:档|水平|分级)?', '偏低', clause)
            if (affected or whole_group) and re.search(r'(?:均|全部|整体|均无|没有|未见).{0,12}(?:正常|无异常|无弱项|无短板)', normal_claim):
                failures.append('模板分档不能覆盖本组原始异常清单；按同源实际方向说明关注点。')
    return list(dict.fromkeys(failures))


def summary_roster_failure(path, text, observations, *, facts=()):
    if tuple(path) == ('comprehensiveConclusion','overallConclusion','text'):
        # The conclusion selects a few real microbial signals. Count bound
        # entities, not every spelling or a genus substring inside a species.
        if len([sentence for sentence in re.split(r'[。！？!?]+',text)if sentence.strip()]) > 6:
            return True
        if len(re.findall(r'(?:被)?来源(?:标记|标)为',text)) > 2:
            return True
        def linked_ids(node):
            if isinstance(node, list):
                return set().union(*(linked_ids(child) for child in node)) if node else set()
            if not isinstance(node, dict):
                return set()
            result = {fid for key in ('factIds','memberFactIds')
                for fid in node.get(key, []) if isinstance(fid, str)}
            if isinstance(node.get('recordId'), str):
                result.add(node['recordId'])
            for child in node.values():
                if isinstance(child, (dict,list)):
                    result.update(linked_ids(child))
            return result

        known = facts if isinstance(facts, dict) else {
            fact.fact_id: fact for fact in facts}
        entities = {}
        for fid in linked_ids(observations):
            fact = known.get(fid)
            kind = fact.get('kind') if isinstance(fact, dict) else getattr(fact,'kind',None)
            name = fact.get('name') if isinstance(fact, dict) else getattr(fact,'name',None)
            if kind != 'microbe' or not isinstance(name, str):
                continue
            aliases = {part.strip() for part in name.split(' / ') if len(part.strip()) >= 2}
            latin = [alias for alias in aliases if re.fullmatch(r'[A-Za-z][^\u4e00-\u9fff]*',alias)]
            taxonomy = max(latin,key=len) if latin else name
            core = re.match(r'^([A-Z][a-z]+ [a-z][A-Za-z_-]+)(?:\b|$)',taxonomy)
            if core and core[1].split()[-1] not in {'sp','spp','bacterium'}:
                taxonomy = core[1]
                aliases.add(taxonomy)
            key = re.sub(r'\s+',' ',taxonomy).casefold()
            entities.setdefault(key,set()).update(aliases)
        spans, found = [], set()
        aliases = sorted(((alias,key) for key,names in entities.items() for alias in names),
            key=lambda pair:len(pair[0]),reverse=True)
        for alias,key in aliases:
            pattern = re.escape(alias)
            if re.search(r'^[A-Za-z]',alias):
                pattern = r'(?<![A-Za-z])'+pattern
            if re.search(r'[A-Za-z]$',alias):
                pattern += r'(?![A-Za-z])'
            for match in re.finditer(pattern,text,re.I):
                if not any(match.start() < end and match.end() > start for start,end in spans):
                    spans.append(match.span());found.add(key)
        return len(found) > 3
    if tuple(path) not in {('functionalPathways','summary','text'),
            ('nutritionAssessment','nutritionInterpretation','text')}:
        return False
    names = {name.strip() for row in observations if isinstance(row.get('name'), str)
        for name in row['name'].split(' / ') if len(name.strip()) >= 2}
    spans, found = [], set()
    for name in sorted(names, key=len, reverse=True):
        for match in re.finditer(re.escape(name), text):
            if not any(match.start() < end and match.end() > start for start,end in spans):
                spans.append(match.span());found.add(name)
    return len(found) > 6


def nutritional_clinical_ids(facts):
    """Context selection is not a nutrient-deficiency diagnosis or an assay guess."""
    names = re.compile(r'营养|身高|体重|BMI|血糖|葡萄糖|糖化|血脂|胆固醇|甘油三酯|'
        r'维生素|铁蛋白|血清铁|锌|白蛋白|总蛋白|转氨酶', re.I)
    return {fact.fact_id for fact in facts if fact.kind == 'clinical' and names.search(fact.name)}


def scfa_clinical_ids(facts):
    """Actual gut/metabolic records can contextualize, never grade, SCFA."""
    theme = re.compile(r'肠|粪便|大便|便秘|腹泻|腹痛|血糖|葡萄糖|糖化|血脂|胆固醇|甘油三酯|'
        r'脂肪肝|体重|BMI', re.I)
    return {fact.fact_id for fact in facts if fact.kind == 'clinical'
        and (theme.search(fact.name) or re.search(r'脂肪肝|肠炎|结肠|便秘|腹泻', fact.value))}


def compact_questionnaire_projection(raw, facts):
    """Fold symptom mirrors into their one exact original Q/A, keeping sources.

    Independent answers, dates and typed values stay separate. Only a derived
    patient-report fact containing the complete original question and answer
    can be folded; a shared symptom or similar wording is not enough.
    """
    rows = raw.get('medicalAssessment', {}).get('questionnaireSummary', [])
    originals = {}
    for row in rows:
        if isinstance(row.get('name'), str) and isinstance(row.get('value'), str) \
                and not row['value'].startswith('问题：'):
            originals.setdefault((row['name'], row['value']), []).append(row)
    removed = set()
    for index, row in enumerate(rows):
        value = row.get('value')
        match = re.fullmatch(r'问题：(.*?)；回答：(.*)', value, re.S) if isinstance(value, str) else None
        if match is None:
            continue
        candidates = originals.get((match[1], match[2]), [])
        if len(candidates) != 1:
            continue
        derived = [facts[source['recordId']] for source in row.get('sources', [])
            if source.get('recordId') in facts]
        if not derived or not all(fact.kind == 'self_reported'
                and fact.source.startswith('用户自述：') and fact.value == value for fact in derived):
            continue
        original = candidates[0]
        for source in row.get('sources', []):
            if source not in original['sources']:
                original['sources'].append(deepcopy(source))
        removed.add(index)
    if removed:
        raw['medicalAssessment']['questionnaireSummary'] = [row for index, row in enumerate(rows)
            if index not in removed]


ROLE_PURPOSES = {
    'disease_meaning': '围绕这一疾病或健康风险形成个人判断：以实际医学检查、症状、病史为核心，菌群与功能关联作为辅助。'
        '回答当前风险与严重程度、可能有关的症状及个人诱因、日常改善方向；没有相关临床资料时按实际关联强度评判，不凭菌名把相关性改成患病。'
        '体检已经明确记录的疾病直接保留其已存在性质，原菌群模型较低不能把它改成尚未存在；'
        '没有临床诊断时写相关风险或关注，不补本人病史。原指数和原分级留在结果列，不在解读列重抄。'
        '健康关联检出只修饰相关菌群信号；腹泻表现同时存在、睡眠障碍是实际问题等本人状态，须有同主题真实医学、自述或原报告明确病史支持。'
        '已提供的个人事实与源内关注判断各自绑定真实factIds；新增菌群疾病关联、机制、典型表现或医学原则须有本句实际evidenceRefs并保留研究适用范围。'
        '不以疾病定义、研究摘要或规则说明替代本人解读，不在正文重复不能重算、不是诊断等操作边界。',
    'general_function': '解释这一功能域的真实状态、组内分布和有适用证据的健康意义；具体管理留在建议字段。'
        '合成、分解、利用、吸收分别采用真实过程；原始升降方向、能力评分与对健康的有利或不利不能相互替代。'
        '有害物生成较低不作为营养缺乏或需要补高的短板；不能从功能评分推导体内含量。'
        '来源状态可按已核实事实独立完成解释；有科学意义时另句引用实际原文。一般意义无需验证本人的分级阈值；'
        '只选择本主题所需的少量依据，不逐篇综述，不为凑意义或改善增加无关内容。'
        '不将基因推断分数写成人体含量、临床缺乏、个人病因或干预效果，不用无关单分子代替整个主题。'
        '正文直接说明实测差异及有依据的功能意义；必要测量边界可简述，但定义、边界或纯管理句不能独自完成结果解释。',
    'individual_synthesis': '综合实际来源判断本章主要问题、关键依据之间的关系及关注顺序；不复述完整结果清单。'
        'C原报告内症状、病史和用药只采用实际sourceReportedBackground记载；健康关联panel检出不称其临床症状。B问卷症状明确称问卷自述，分别写清归属，缺少的问卷不补写。'
        '同一条数据跨章展示不算相互印证；新增医学关联或原则仍需对应引用。',
    'population_association': '结合真实异常程度、临床意义和适用证据选择至多三个重点信号。'
        '解释当前方向时保留人群、标本、分类层级和关联性质，不推成本人疾病。'
        '原值表保留全部有效项目，摘要不按固定菌名或文献数量凑重点。',
    'clinical_principle': '保留实际健康问题及原始疑似程度，解释本次状态与有依据的医学意义。'
        '用一段自然中文围绕该具体问题形成判断；关注程度使用已审核的公开评估，诱因只用真实个人行为及适用关系。'
        '区分诊断原则、指南目标与实际效果，不推测个人检查已做或未做。'
        '具体转诊、复查、检查或生活管理行动留sourceAdvice，不能代替本栏结果解释；没有适用科学原文时真实状态仍可完成本栏。'
        '补剂或药物暴露的风险科普，只有本节点本人资料实际提供该暴露时才纳入，不借同张影像旁及其他干预主题。',
    'management': '只为本项真实疾病、发现或自述问题给出有来源且适用的行动。'
        '原报告逐字建议须有私有原始建议来源，生成输出不成为原文。新检查、随访或生活方式行动逐句独立审核，'
        '普通携报告咨询可有限说明；疾病特异随访、检查策略、新警示症状以及医学效益、频次或目标须由同对象适用原文与本人前提支持；不能从相邻解释借用引用，不补未知诊断、检查或处方。',
}

SOURCE_COMPARISON_PURPOSE = (
    '综合本类别实际已有的同主题资料，以临床检查、症状及病史为核心，菌群依据辅助解释真实健康问题、风险与诱因。'
    '全面考虑相反资料后形成最终判断；正文展示最终结论及其支持依据，不再逐项公开相反结果、排除过程或不能归因的规则。'
    '有实际同主题的多来源支持才说明相互支持，不能将单一来源改称跨来源印证。'
    '原始疑似、阴性和标本条件继续留在对应原结果字段，综合结论不把风险或疑似升级为确诊。'
    '解释本类问题可能影响哪些日常表现、与哪些实际行为相关以及相应改善方向；不在每类末段重复全局风险清单。'
    '直接写实际状态与关系，不写保留出处、不升级诊断等输出过程或泛化检测边界文案。'
)

SINGLE_OBSERVATION_PURPOSE = (
    '本节点解释本项具体健康问题或实测对象的真实状态；有适用证据时补充健康意义；'
    '原始数值、原分级和完整名册已在结果字段，不要求正文再次抄写。'
    '源内关注判断与日常观察可独立审核；新增病因、健康影响及饮食运动等实际管理仍使用相应真实适用原文。'
    '使用“本项”定位，不从一个项目推整章仅有一项或整章整体未见异常；不在单项解释中重排全局关注顺序或安排其他疾病的随访。'
    '原报告明确记载的疑似、阴性、历史、标本和具体冲突保持原义；必要测量边界与本项真实对象相符，不用边界或纯管理替代解释。'
)
