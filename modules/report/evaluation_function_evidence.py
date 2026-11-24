"""Object-level evidence scope for evaluate's measured functional targets.

This is a necessary identity check, not a clinical-applicability review. A
paper's broad topic, generated interpretation, or inherited query supports
does not establish that it discusses each nutrient in a measured group.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from .evaluation_catalog import AMINO_ACID_ALIASES
from .schemas import ReportSourceFact


# Finite synonymous object names only: these aliases neither grade a result
# nor infer a concentration, deficiency, patient mechanism, or treatment.
_ALIASES = {
    **AMINO_ACID_ALIASES,
    'microbialRichness': ('肠道菌群种类', '菌群种类数', '物种丰富度', '菌群丰富度',
        'species richness', 'microbial richness', 'microbiota richness', 'bacterial richness'),
    'microbialDiversity': ('肠道菌群多样性', '菌群多样性', 'microbial diversity',
        'microbiota diversity', 'gut microbiome diversity', 'Shannon diversity', 'Shannon index',
        'microbiome diversity', 'alpha diversity', 'alpha-diversity'),
    'formate': ('甲酸', 'formate', 'formic acid'),
    'acetate': ('乙酸', '醋酸', 'acetate', 'acetic acid'),
    'propionate': ('丙酸', 'propionate', 'propionic acid'),
    'butyrate': ('丁酸', 'butyrate', 'butyric acid'),
    'valerate': ('戊酸', 'valerate', 'valeric acid'),
    'isobutyrate': ('异丁酸', 'isobutyrate', 'isobutyric acid'),
    'isovalerate': ('异戊酸', 'isovalerate', 'isovaleric acid'),
    'betaAlanine': ('β-丙氨酸', 'β丙氨酸', 'beta-alanine', 'beta alanine'),
    'vitaminA': ('维生素A', 'vitamin A', 'retinol'),
    'vitaminB1': ('维生素B1', 'vitamin B1', '硫胺素', 'thiamine', 'thiamin'),
    'vitaminB2': ('维生素B2', 'vitamin B2', '核黄素', 'riboflavin'),
    'vitaminB3': ('维生素B3', 'vitamin B3', '烟酸', '烟酰胺', 'niacin', 'nicotinamide'),
    'vitaminB5': ('维生素B5', 'vitamin B5', '泛酸', 'pantothenic acid', 'pantothenate'),
    'vitaminB6': ('维生素B6', 'vitamin B6', '吡哆醇', 'pyridoxine', 'pyridoxal'),
    'vitaminB7': ('维生素B7', 'vitamin B7', '生物素', 'biotin'),
    'folate': ('维生素B9', 'vitamin B9', '叶酸', 'folate', 'folates', 'folic acid'),
    'vitaminB12': ('维生素B12', 'vitamin B12', '钴胺素', 'cobalamin'),
    'vitaminC': ('维生素C', 'vitamin C', '抗坏血酸', 'ascorbic acid', 'ascorbate'),
    'vitaminD': ('维生素D', 'vitamin D'),
    'vitaminD2': ('维生素D2', 'vitamin D2', '麦角钙化醇', 'ergocalciferol'),
    'vitaminD3': ('维生素D3', 'vitamin D3', '胆钙化醇', 'cholecalciferol'),
    'vitaminE': ('维生素E', 'vitamin E', '生育酚', 'tocopherol', 'tocopherols'),
    'vitaminK': ('维生素K', 'vitamin K'),
    'vitaminK1': ('维生素K1', 'vitamin K1', '叶绿醌', 'phylloquinone'),
    'vitaminK2': ('维生素K2', 'vitamin K2', 'menaquinone', 'menaquinones'),
    'serotonin': ('5-羟色胺', '5羟色胺', '血清素', 'serotonin', '5-HT', '5HT'),
    'dopamine': ('多巴胺', 'dopamine', 'DA'),
    'gaba': ('γ-氨基丁酸', 'γ氨基丁酸', 'gamma-aminobutyric acid', 'gamma aminobutyric acid', 'GABA'),
    'acetylcholine': ('乙酰胆碱', 'acetylcholine'),
    'histamine': ('组胺', 'histamine'),
    'norepinephrine': ('去甲肾上腺素', 'norepinephrine', 'noradrenaline'),
    'epinephrine': ('肾上腺素', 'epinephrine', 'adrenaline'),
    'endorphins': ('内啡肽', 'endorphin', 'endorphins'),
    'glutathione': ('谷胱甘肽', '谷胱甘肽三肽', 'glutathione'),
    'bileAcids': ('胆汁酸', 'bile acid', 'bile acids'),
    'coenzymeQ': ('辅酶Q', 'coenzyme Q', 'ubiquinone'),
    'lipoicAcid': ('硫辛酸', 'lipoic acid'),
    'carotenoids': ('类胡萝卜素', 'carotenoid', 'carotenoids'),
    'flavonoids': ('类黄酮', '黄酮', 'flavonoid', 'flavonoids'),
    'anthocyanins': ('花青素', 'anthocyanin', 'anthocyanins'),
    'lycopene': ('番茄红素', 'lycopene'),
    'lutein': ('叶黄素', 'lutein'),
    'isoflavones': ('异黄酮素', '异黄酮', 'isoflavone', 'isoflavones'),
    'chlorophyll': ('叶绿素', 'chlorophyll'),
    'carbohydrate': ('碳水化合物', 'carbohydrate', 'carbohydrates'),
    'protein': ('蛋白质', 'protein', 'proteins'),
    'fat': ('脂肪', 'fat', 'fats', 'lipid', 'lipids'),
    'minerals': ('矿物质', 'mineral', 'minerals'),
    'vitamins': ('维生素', 'vitamin', 'vitamins'),
    'aminoAcids': ('氨基酸', 'amino acid', 'amino acids'),
    'shortChainFattyAcids': ('短链脂肪酸', 'short-chain fatty acid', 'short-chain fatty acids',
        'short chain fatty acid', 'short chain fatty acids', 'SCFA', 'SCFAs'),
    'neurotransmitters': ('神经递质', 'neurotransmitter', 'neurotransmitters'),
    'glycolysis': ('葡萄糖(糖酵解途径)', '糖酵解', 'glycolysis'),
    'pentosePhosphate': ('葡萄糖(磷酸戊糖途径)', '磷酸戊糖途径', 'pentose phosphate pathway'),
    'purine': ('嘌呤', '嘌呤代谢', 'purine', 'purines'),
    'pyrimidine': ('嘧啶', '嘧啶代谢', 'pyrimidine', 'pyrimidines'),
    'galactose': ('半乳糖', 'galactose'),
    'starchSucrose': ('淀粉和蔗糖', 'starch and sucrose', 'starch/sucrose'),
    'alphaLinolenicAcid': ('α-亚麻酸', 'α亚麻酸', 'alpha-linolenic acid', 'alpha linolenic acid'),
    'arachidonicAcid': ('花生四烯酸', 'arachidonic acid'),
    'unsaturatedFattyAcids': ('不饱和脂肪酸', 'unsaturated fatty acid', 'unsaturated fatty acids'),
    'saturatedFattyAcids': ('饱和脂肪酸', 'saturated fatty acid', 'saturated fatty acids'),
    'glycerides': ('甘油酯', 'glyceride', 'glycerides'),
    'phosphatidylglycerol': ('磷脂酰甘油', 'phosphatidylglycerol'),
    'linoleicAcid': ('亚油酸', 'linoleic acid'),
    'secondaryBileAcids': ('次级胆汁酸', 'secondary bile acid', 'secondary bile acids'),
    'sphingolipids': ('鞘脂', 'sphingolipid', 'sphingolipids'),
    'steroids': ('类固醇', 'steroid', 'steroids'),
    'steroidHormones': ('类固醇激素', 'steroid hormone', 'steroid hormones'),
    'homocysteine': ('同型半胱氨酸', 'homocysteine'),
    'lipopolysaccharides': ('脂多糖', 'lipopolysaccharide', 'lipopolysaccharides', 'LPS'),
}


def _text(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def _name_key(value: str) -> str:
    return re.sub(r'[\s()（）·_\-]+', '', _text(value))


_NAME_OBJECTS = {_name_key(alias): identity for identity, aliases in _ALIASES.items() for alias in aliases}
_FUNCTION_SUFFIX = re.compile(r'(?:的)?(?:合成|代谢|分解|降解|产生|生产)?(?:能力|功能|水平|评分|评估|潜力)$')
_ENGLISH_FUNCTION_SUFFIX = re.compile(r'\s+(?:biosynthesis|synthesis|production|metabolism|degradation)'
    r'(?:\s+(?:capacity|ability|score|assessment|level))?$')


def _object_name(fact: ReportSourceFact) -> tuple[str, str] | None:
    """Identify the exact printed object, without converting whole families."""
    if fact.kind not in {'function', 'context'}:
        return None
    name = fact.name.strip()
    key = _name_key(name)
    if key in _NAME_OBJECTS:
        return _NAME_OBJECTS[key], name
    stripped = _FUNCTION_SUFFIX.sub('', _name_key(_ENGLISH_FUNCTION_SUFFIX.sub('', _text(name))))
    if stripped in _NAME_OBJECTS:
        return _NAME_OBJECTS[stripped], name
    parts = [part for part in re.split(r'[()（）]', name) if part.strip()]
    identities = {_NAME_OBJECTS.get(_FUNCTION_SUFFIX.sub('', _name_key(part))) for part in parts}
    if len(identities) == 1 and None not in identities:
        return identities.pop(), name
    # A new source-present function keeps literal-name compatibility. Generic
    # context metadata (age, history, institution...) is outside this helper.
    if fact.kind == 'function' and len(stripped) >= 2:
        literal = _ENGLISH_FUNCTION_SUFFIX.sub('', _text(name)).strip()
        literal = re.sub(r'(?:的)?(?:合成|代谢|分解|降解|产生|生产)?(?:能力|功能|水平|评分|评估|潜力)$', '', literal)
        return 'literal:' + stripped, literal
    return None


def _alias_pattern(alias: str) -> re.Pattern:
    phrase = re.escape(_text(alias)).replace(r'\ ', r'[\s-]*')
    # B-12 and B 12 are common printed alternatives to B12, not B1.
    phrase = re.sub(r'([a-z])(?=[0-9])', lambda match:match[1] + r'[\s-]*', phrase)
    # Chinese chemical names do not have whitespace boundaries. ASCII names
    # do, preventing B1/B12, leucine/isoleucine and DA/DAT substring matches.
    left = r'(?<![a-z0-9])' if re.match(r'[a-z0-9]', _text(alias)) else ''
    right = r'(?![a-z0-9])' if re.search(r'[a-z0-9]$', _text(alias)) else ''
    return re.compile(left + phrase + right)


_PATTERNS = [(identity, _alias_pattern(alias)) for identity, aliases in _ALIASES.items() for alias in aliases]


def _source_objects(text: str) -> set[str]:
    original = text
    text = _text(text)
    found = [(match.start(), match.end(), identity) for identity, pattern in _PATTERNS
        for match in pattern.finditer(text)]
    # A longer distinct chemical object wins over its contained short name,
    # e.g. 异亮氨酸 versus 亮氨酸, and γ-氨基丁酸 versus 丁酸.
    objects = {identity for start, end, identity in found if not any(
        outer != identity and low <= start and end <= high and high - low > end - start
        for low, high, outer in found)}
    # Explicit lists share one printed "vitamins" prefix. Uppercase isolated
    # letter codes avoid mistaking the English article "a" for vitamin A.
    code = r'[ABCDEK](?:[\s-]?[0-9]{1,2})?(?![a-zA-Z0-9])'
    separator = r'\s*(?:,\s*(?:(?i:and|or)\s+)?|/|、|及|和|(?i:and|or)\s+)\s*'
    for match in re.finditer(r'(?:(?i:vitamins?)\s+|维生素)(?:' + code + r')(?:(?:' + separator + r')(?:' + code + r'))+', original):
        for item in re.findall(code, match.group()):
            identity = _NAME_OBJECTS.get(_name_key('vitamin ' + item))
            if identity:
                objects.add(identity)
    # DA is a source abbreviation only in a stated neuroactive context;
    # an unrelated strain/label called DA is not a dopamine measurement.
    if ('dopamine' in objects and not re.search(r'\bdopamine\b|多巴胺|'
            r'\b(?:neurotransmitters?|neuroactive|dopaminergic|catecholamines?|neuronal)\b|神经递质', text)
            and not objects.intersection({'serotonin','gaba','acetylcholine','norepinephrine','epinephrine','endorphins'})):
        objects.remove('dopamine')
    return objects


def _source_discusses_observation(identity, name, source, objects):
    """Keep a candidate about an explicit part of a combined assay name.

    This is retrieval identity only. A starch passage does not establish a
    sucrose process or a process of the complete starch-and-sucrose assay;
    the reference plan and both sentence reviews still retain that scope.
    Do not add these component names to the chemical-identity alias table.
    """
    if identity in objects or identity.startswith('literal:') and _alias_pattern(name).search(_text(source)):
        return True
    return identity == 'starchSucrose' and any(
        _alias_pattern(part).search(_text(source))
        for part in ('starch', 'sucrose', '淀粉', '蔗糖'))


def ecological_association_priority(source, title, object_aliases, outcome_aliases):
    """Rank an original human ecological association, never its applicability.

    Basic biochemical processes use other roles. Here a review, mouse/drug
    intervention or an isolated background mention cannot replace the measured
    richness/diversity's human metabolic association. No threshold or causal
    interpretation is transferred to the patient's assay.
    """
    source, title = _text(source or ''), _text(title or '')
    if (re.search(r'\breview\b|meta.analysis|综述|系统评价', title)
            or re.search(r'\b(?:this|we|our)\s+(?:narrative\s+)?review\b', source)
            or re.search(r'mouse|mice|murine|rat model|animal model|clinical trial|randomi[sz]ed|'
                r'supplementation|treatment|therap|alleviat|ameliorat|extract|intervention|动物|小鼠|干预|试验', title)):
        return 0
    if not re.search(r'\b(?:cohorts?|population|participants?|individuals?|subjects?|adults?|humans?)\b|人群|成人|受试者', source):
        return 0
    mentions = lambda text, terms: any(_alias_pattern(term).search(text) for term in terms)
    association = any(mentions(sentence, object_aliases) and mentions(sentence, outcome_aliases)
        and re.search(r'associat|correlat|characteri[sz]|(?:low|high|lower|higher)[\s-]|相关|关联', sentence)
        for sentence in re.split(r'(?<=[.!?。；;])\s+|[\n。；;]', source))
    if not association:
        return 0
    return 10 + 5 * bool(re.search(r'richness|diversity|丰富度|多样性', title)) + 5 * bool(
        mentions(title, outcome_aliases))


def _primary_text(record: dict) -> str:
    # interpretation/summary/targets can name unsupported members while
    # describing a mismatch. They must never manufacture an object match.
    body = record.get('sourceText')
    if not isinstance(body, str) or not body.strip():
        body = next((record[key] for key in ('abstract', 'evidenceText', 'evidence_text')
            if isinstance(record.get(key), str) and record[key].strip()), '')
    title = record.get('title') if isinstance(record.get('title'), str) else ''
    return unicodedata.normalize('NFKC', title + '\n' + body)


_PURPOSE_OR_HYPOTHESIS = re.compile(
    r'\b(?:hypothesi[sz]\w*|hypothesis|we reasoned|we speculat\w*|'
    r'objective|aimed?|research purpose|we investigated|we questioned|whether)\b|'
    r'研究假说|我们推测|旨在|研究目的', re.I)
_INTERVENTION_SENTENCE = re.compile(
    r'\b(?:supplementation|administ\w*|inhibitors?|compounds?|drug development|'
    r'anti[- ]?virulence|therapeutic|treatment|therapies)\b|'
    r'补充干预|补充剂|抑制剂|药物开发|药物治疗|治疗策略', re.I)


def scfa_production_sentence_role(quote: str, members=()) -> bool:
    """Find a microbial production predicate, never a trial's word production.

    This only locates an original candidate. The original research scope and
    the eventual new sentence still require both existing citation reviews.
    """
    if not isinstance(quote, str) or _PURPOSE_OR_HYPOTHESIS.search(quote) \
            or _INTERVENTION_SENTENCE.search(quote):
        return False
    actor = r'(?:microorganisms?|microbes?|microbiota|bacteria|bacterial|细菌|微生物|菌群)'
    names = [re.escape(name) for name in members if isinstance(name, str) and len(name) >= 2]
    if names:
        actor = '(?:' + actor + '|' + '|'.join(names) + ')'
    product = r'(?:short[- ]chain fatty acids?|SCFAs?|butyrate|propionate|acetate|短链脂肪酸|丁酸|丙酸|乙酸)'
    process = r'(?:produc\w*|ferment\w*|synthesi[sz]\w*|biosynth\w*|generat\w*|form(?:s|ed|ing)?|发酵|产生|生成|合成)'
    # Active and passive forms retain the actual microbial actor. A member
    # merely described as SCFA-producing, or a host Treg effect, is not enough.
    return bool(re.search(actor + r'[^.;。；\n]{0,90}(?:' + process + r')'
        + r'[^.;。；\n]{0,70}' + product, quote, re.I)
        or re.search(product + r'[^.;。；\n]{0,65}' + process
            + r'[^.;。；\n]{0,35}\bby\b[^.;。；\n]{0,40}' + actor, quote, re.I)
        or re.search(actor + r'[^.;。；\n]{0,40}fermentation[^.;。；\n]{0,45}'
            + product, quote, re.I)
        or re.search(product + r'[^.;。；\n]{0,35}\b(?:products?|metabolites?)\s+of\s+'
            r'(?:[^.;。；\n]{0,25})' + actor + r'[^.;。；\n]{0,20}fermentation', quote, re.I))


def basic_pathogen_sentence_role(quote: str) -> bool:
    """Separate an organism's own meaning from an inhibitor experiment."""
    if not isinstance(quote, str) or _PURPOSE_OR_HYPOTHESIS.search(quote) \
            or _INTERVENTION_SENTENCE.search(quote):
        return False
    return bool(re.search(r'\b(?:pathogens?|pathogenic|causative|infect\w*|invad\w*|'
        r'invasion|pathogenesis|coloniz\w*|colonis\w*|virulence|toxin|adher\w*)\b|'
        r'病原|致病|感染|侵袭|定植|毒素|黏附|粘附', quote, re.I))


def basic_metabolite_sentence_role(quote: str) -> bool:
    """A normal source panel needs no experimental cytokine catalogue."""
    return bool(isinstance(quote, str) and not _PURPOSE_OR_HYPOTHESIS.search(quote)
        and not _INTERVENTION_SENTENCE.search(quote)
        and not re.search(r'\b(?:Caco[- ]?2|cell lines?|cell monolayers?|in vitro|'
            r'TNF[- ]?\w*|IL[- ]?\d+|interleukins?)\b|细胞系|单层实验|体外|炎症介质', quote, re.I)
        and re.search(r'\b(?:is|are|constitut\w*|components?|bind\w*|activat\w*|'
            r'regulat\w*)\b|组成成分|构成|结合|激活|调节', quote, re.I))


def _native_member_objects(context, known):
    """Identify measured native members, not a broad topic or clinical bridge."""
    members = []
    for group in context:
        if not isinstance(group, dict) or group.get('semanticType') != 'nativeNutritionState':
            continue
        for member in group.get('members') or []:
            if not isinstance(member, dict):
                continue
            for fid in member.get('factIds') or []:
                fact = known.get(fid)
                if (fact is not None and fact.kind == 'function'
                        and _name_key(fact.name) == _name_key(str(member.get('name') or ''))
                        and (identity := _object_name(fact)) is not None):
                    members.append(identity)
    return members


def _detected_gene_symbols(context):
    """Read the explicit detected column; candidate-panel genes stay separate."""
    symbols = []
    for row in context:
        if not isinstance(row, dict) or row.get('semanticType') not in {'virulence', 'healthSummary'}:
            continue
        for text in (row.get('sourceDescription'), row.get('details')):
            if not isinstance(text, str):
                continue
            for match in re.finditer(r'(?:^|[；;\n])\s*检出基因\s*[:：]\s*([^；;\n]*)', text):
                detected = re.split(r'(?:检测面板)?候选(?:基因)?\s*[:：]', match[1], maxsplit=1)[0]
                if re.sub(r'\s+', '', detected).casefold() in {'none', 'negative', 'n/a', 'notdetected', 'undetected', 'false'}:
                    continue
                symbols.extend(re.findall(r'(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_])', detected))
    return list(dict.fromkeys(symbols))


def filter_function_evidence(record: dict, facts: list[ReportSourceFact], *,
                             target_path=None, target_context=()) -> dict | None:
    """Narrow original supports to this target's actual discussed objects.

    Call only for evaluate targets, with their complete source-fact DTOs.
    Clinical-problem bridges and known nonfunctional facts remain untouched;
    the caller continues to perform its own problem/applicability checks.
    No support IDs, claims, numbers, or source data are added or rewritten.
    """
    known = {fact.fact_id: fact for fact in facts}
    comparable = {fact.fact_id: item for fact in facts if (item := _object_name(fact)) is not None}
    result = deepcopy(record)
    source = _primary_text(record)
    objects = _source_objects(source)
    if target_path:
        # A native module's measured molecules own its scientific explanation.
        # An inherited topic or an unrelated clinical support cannot lend a
        # paper on a downstream product to an otherwise different module.
        function_path = (target_path[0] in {'functionalPathways', 'nutritionAssessment'}
            or tuple(target_path[:2]) == ('microbiomeAssessment', 'scfaInterpretation'))
        members = _native_member_objects(target_context, known) if function_path else []
        if members and not any(_source_discusses_observation(identity, name, source, objects)
                for identity, name in members):
            return None
        normal_harmful = any(isinstance(row, dict)
            and row.get('semanticType') == 'nativeNutritionState'
            and row.get('panel') == 'yingyang2_yhdxw2' and row.get('sourceStatus') == '正常'
            for row in target_context)
        if normal_harmful:
            from .clinical_report import _review_quote_options
            own = {identity for identity, _ in members}
            if not any(own.intersection(_source_objects(quote)) and basic_metabolite_sentence_role(quote)
                    for quote in _review_quote_options(record.get('sourceText') or '').values()):
                return None
        if tuple(target_path[:2]) == ('microbiomeAssessment', 'healthMetrics'):
            summaries = [row for row in target_context if isinstance(row, dict)
                and row.get('semanticType') == 'healthSummary']
            from .clinical_report import _review_quote_options
            quotes = _review_quote_options(record.get('sourceText') or '').values()
            if any(row.get('code') == 'scfaProducerBacteria' for row in summaries):
                members = [fact.name.split(' / ')[-1] for fact in facts if fact.kind == 'microbe']
                if not any(scfa_production_sentence_role(quote, members) for quote in quotes):
                    return None
            if any(row.get('code') in {'pathogenicBacteria', 'opportunisticBacteria'} for row in summaries) \
                    and not any(basic_pathogen_sentence_role(quote) for quote in quotes):
                return None
            genes = _detected_gene_symbols(target_context)
            if genes and not any(_alias_pattern(symbol).search(_text(source)) for symbol in genes):
                return None
            # These named whole-health summaries are not measured chemical
            # objects. Their scope is checked by the separate health review;
            # literal Chinese headings must not reject every English paper.
            summary_ids = {fid for row in target_context if isinstance(row, dict)
                and row.get('semanticType') == 'healthSummary' for fid in row.get('factIds') or []}
            comparable = {fid: item for fid, item in comparable.items()
                if fid not in summary_ids or not item[0].startswith('literal:')}
    if not comparable:
        return result
    clinical_ids = {binding.get('problemId') for binding in record.get('clinicalProblemBindings') or []
        if isinstance(binding, dict) and isinstance(binding.get('problemId'), str)
        and binding['problemId'].strip()}
    clinical_ids.update(value for key in ('supports', 'diseaseIds') for value in record.get(key) or []
        if isinstance(value, str) and value.startswith('clinical-problem-'))
    matched = {fid for fid, (identity, name) in comparable.items()
        if _source_discusses_observation(identity, name, source, objects)}
    # Evaluation topics are another existing support form, not evidence that
    # every member is covered. Retain their original IDs only after narrowing
    # their copied member metadata to source-discussed, known measurements.
    topic_matches = matched | {fid for fid, fact in known.items()
        if fact.kind not in {'function', 'context'}}
    topics = []
    for topic in record.get('evaluationTopics') or []:
        if not isinstance(topic, dict):
            continue
        covered = [fid for fid in topic.get('factIds') or [] if fid in topic_matches]
        if not covered:
            continue
        item = deepcopy(topic)
        item['factIds'] = covered
        if 'sources' in item:
            item['sources'] = [source for source in item['sources']
                if isinstance(source, dict) and source.get('factId') in covered]
        if 'sourceSupportIds' in item:
            item['sourceSupportIds'] = [fid for fid in item['sourceSupportIds'] if fid in covered]
        if 'sourceObjects' in item:
            names = {_name_key(known[fid].name) for fid in covered}
            item['sourceObjects'] = [name for name in item['sourceObjects']
                if isinstance(name, str) and _name_key(name) in names]
        topics.append(item)
    topic_ids = {topic.get('topicId') for topic in topics}
    selected = []
    for support in record.get('supports') or []:
        if not isinstance(support, str):
            continue
        if support in comparable:
            if support in matched:
                selected.append(support)
        elif support in topic_ids:
            selected.append(support)
        elif support in clinical_ids or support in known and known[support].kind not in {'function', 'context'}:
            selected.append(support)
    if not selected and not clinical_ids and not topics:
        return None
    result['supports'] = list(dict.fromkeys(selected))
    if 'evaluationTopics' in result:
        result['evaluationTopics'] = topics
    return result
