"""Internal question/coverage planning for the overall health assessment only.

Topics bind existing observations to reusable scientific questions. They are
neither new source facts nor approval of a medical assertion, and never enter
the confirmed callback DTO. Retrieval, original-text verification, population,
site, object, causal-strength and sentence reviews remain downstream gates.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from functools import lru_cache
import re
import unicodedata
from typing import Any, Literal

from .evidence import EvidenceQuery
from .schemas import EvidenceRecord
from .evaluation_originals import (is_trusted_guide_block, evaluation_original_text,
    evaluation_publication_identifiers, evaluation_publication_identity)
from .evaluation_result_rules import is_confirmed_negative_health, has_risk_index
from .evaluation_catalog import (fixed_disease_code, FIXED_DISEASE_ALIASES,
    FIXED_METRIC_ALIASES, NEUROTRANSMITTER_DEFINITIONS, RISK_CATEGORY_DEFINITIONS)


EvidenceRole = Literal['source_data', 'general_function', 'disease_meaning',
    'population_association', 'diagnostic_principle', 'individual_synthesis', 'management']


@dataclass(frozen=True)
class EvidenceTopic:
    topic_id: str
    kind: str
    title: str
    fact_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    source_objects: tuple[str, ...]
    allowed_roles: tuple[EvidenceRole, ...]
    required_roles: tuple[EvidenceRole, ...]
    consumer_paths: tuple[tuple[str | int, ...], ...]
    source_bindings: tuple[dict[str, Any], ...]
    query_term: str
    aliases: tuple[str, ...]
    priority: int
    source_support_ids: tuple[str, ...] = ()
    source_semantics: tuple[str, ...] = ()
    context_fact_ids: tuple[str, ...] = ()
    query_enabled: bool = True
    query_concepts: tuple[tuple[str, ...], ...] = ()
    query_taxa: tuple[str, ...] = ()
    query_background: tuple[str, ...] = ()
    query_measured_objects: tuple[str, ...] = ()
    query_relations: tuple[dict[str, Any], ...] = ()
    primary_consumer_paths: tuple[tuple[str | int, ...], ...] = ()
    optional_consumer_paths: tuple[tuple[str | int, ...], ...] = ()
    clinical_frame: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class EvaluationEvidencePlan:
    topics: tuple[EvidenceTopic, ...]
    queries: tuple[EvidenceQuery, ...]
    deferred_topic_ids: tuple[str, ...]
    # Append-only, request-local retrieval synonyms. Fact bindings, consumer
    # roles and original observations stay frozen; every selector and query
    # reads the same controlled alias view, never a serialized model flag.
    _concept_aliases: dict[str, dict[int, tuple[str, ...]]] = field(default_factory=dict, compare=False, repr=False)
    _alias_events: list[dict[str, Any]] = field(default_factory=list, compare=False, repr=False)
    subject: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    patient_facts: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class TopicEvidenceSelection:
    # One copy per original block/context of each selected publication, with
    # topic IDs added only as candidate supports for that particular original.
    # Bibliography limits count publications, not their independently retained
    # originals. Interpretations and source text are never joined or rewritten.
    records: tuple[Any, ...]
    by_topic: dict[str, tuple[str, ...]]
    contexts: dict[str, tuple[Any, ...]]
    coverage: tuple[dict[str, Any], ...]


def _retrieval_topic(plan, topic):
    additions = plan._concept_aliases.get(topic.topic_id, {})
    if not additions or not topic.query_concepts:
        return topic
    groups = tuple(_unique([*group, *additions.get(index, ())])
        for index, group in enumerate(topic.query_concepts))
    return replace(topic, query_concepts=groups, aliases=groups[0])


_DISEASE_TERMS = {
    '克罗恩病': ('Crohn disease', "Crohn's disease", 'Crohn'),
    '溃疡性结肠炎': ('ulcerative colitis',),
    '肠易激综合征': ('irritable bowel syndrome', 'IBS'),
    '2型糖尿病': ('type 2 diabetes', 'type 2 diabetes mellitus', 'T2DM'),
    '非酒精性脂肪肝': ('nonalcoholic fatty liver disease', 'non-alcoholic fatty liver disease', 'NAFLD'),
    '肝硬化': ('liver cirrhosis', 'cirrhosis'),
    '精神分裂症': ('schizophrenia',),
    '抑郁症': ('depression', 'depressive disorder'),
    '结直肠癌': ('colorectal cancer', 'colorectal carcinoma'),
    '胃癌': ('gastric cancer', 'stomach cancer'),
    '胃肠道感染': ('gastrointestinal infection', 'enteric infection'),
    '炎症性肠病': ('inflammatory bowel disease', 'IBD'),
    '腹胀排气': ('abdominal bloating', 'flatulence'),
    '腹痛': ('abdominal pain',),
    '腹泻': ('diarrhea', 'diarrhoea'),
    '胃胀': ('gastric bloating', 'stomach bloating'),
    '肝病': ('liver disease',),
    '心血管相关': ('cardiovascular health', 'cardiovascular'),
    '代谢相关脂肪性肝病': ('fatty liver disease',),
    '骨质疏松症': ('osteoporosis',),
    '情绪障碍': ('mood disorder',),
    '睡眠障碍': ('sleep disorder',),
    '过敏症': ('allergy',),
    '酒精使用障碍': ('alcohol use disorder',),
    '阿尔兹海默症': ('Alzheimer disease', "Alzheimer's disease"),
    '帕金森综合征': ('parkinsonism',),
    '睡眠呼吸暂停综合征': ('sleep apnea', 'sleep apnoea'),
}


def disease_meaning_names(name: str) -> tuple[str, ...]:
    """Name the source item with only its existing retrieval aliases."""
    code = fixed_disease_code(name)
    return tuple(dict.fromkeys((name, *_DISEASE_TERMS.get(name, ()),
        *FIXED_DISEASE_ALIASES.get(code, ()))))


def _disease_retrieval_terms(name):
    # Public search uses only the first bounded alternatives. Put complete
    # English names before Chinese spellings/acronyms; keep the source name
    # in the same topic and never infer a subtype (generic IBS stays generic).
    aliases = disease_meaning_names(name)
    english = tuple(alias for alias in aliases if re.search(r'[A-Za-z]{3}', alias)
        and not re.search(r'[\u4e00-\u9fff]', alias) and (' ' in alias or not alias.isupper()))
    return tuple(dict.fromkeys((*english, *aliases)))

# Retrieval concepts describe source-present domains. No dose, recipe,
# supplement recommendation, diagnostic threshold or provider-score validation.
_PATHWAY_TERMS = {
    'P1': ('carbohydrate metabolism', ('carbohydrate', 'carbohydrates', 'glycolysis', 'fermentation',
        'polysaccharide', 'polysaccharides', 'oligosaccharide', 'oligosaccharides', 'glycan', 'glycans',
        'glucan', 'glucans', '碳水化合物', '糖酵解', '多糖', '寡糖')),
    'P2': ('lipid metabolism', ('lipid', 'lipids', 'fat', 'fats', 'bile acid', 'bile acids', 'cholesterol', '脂质', '胆汁酸')),
    'P3': ('short-chain fatty acids', ('short-chain fatty acid', 'short-chain fatty acids', 'short chain fatty acid', 'short chain fatty acids', 'SCFA', 'SCFAs', 'butyrate', '短链脂肪酸', '丁酸')),
    'P4': ('amino acid biosynthesis', ('amino acid', 'amino acids', 'amino-acid', 'amino-acids', 'tryptophan', 'glutamate', '氨基酸', '色氨酸', '谷氨酸')),
    'P5': ('vitamin biosynthesis', ('vitamin', 'vitamins', 'folate', 'thiamine', 'riboflavin', '维生素', '叶酸')),
    'P6': ('microbial neurotransmitter production', ('neurotransmitter', 'serotonin', 'GABA', 'dopamine', 'neuroactive', '神经递质', '血清素', '多巴胺')),
    'P7': ('nucleotide biosynthesis', ('nucleotide', 'purine', 'pyrimidine', '核苷酸', '嘌呤', '嘧啶')),
    'P8': ('microbial carotenoid biosynthesis', ('carotenoid', 'flavonoid', '类胡萝卜素', '类黄酮')),
    'P9': ('bacterial virulence factors', ('pathogen', 'virulence', 'toxin', '致病', '毒素', '毒力')),
    'P10': ('microbial xenobiotic degradation', ('resistance gene', 'biofilm', 'xenobiotic', '耐药基因', '生物膜', '外源物')),
}

# Literal retrieval translations, not inferred diagnoses or mechanism claims.
# The shared function-domain selector must first admit the real A/B/C history.
_FUNCTION_BACKGROUND_TERMS = (
    (r'脂肪肝|脂肪性肝', ('fatty liver',)),
    (r'肥胖', ('obesity',)), (r'超重', ('overweight',)),
    (r'糖尿病', ('diabetes',)), (r'血糖|葡萄糖', ('blood glucose',)),
    (r'糖化', ('glycated hemoglobin',)), (r'高脂血症|血脂异常', ('dyslipidemia',)),
    (r'血脂', ('blood lipids',)), (r'胆固醇', ('cholesterol',)),
    (r'甘油三酯', ('triglycerides',)), (r'胆结石|胆石', ('gallstones',)),
    (r'便秘', ('constipation',)), (r'腹泻', ('diarrhea',)),
    (r'腹胀', ('bloating',)), (r'腹痛', ('abdominal pain',)),
    (r'营养不良', ('malnutrition',)), (r'低蛋白', ('hypoproteinemia',)),
    (r'白蛋白', ('albumin',)), (r'总蛋白', ('total protein',)),
    (r'肝功能|转氨酶', ('liver enzymes',)),
    (r'肾功能', ('kidney function',)), (r'肌酐', ('creatinine',)), (r'尿素', ('urea',)),
    (r'贫血', ('anemia',)), (r'血红蛋白', ('hemoglobin',)),
    (r'维生素', ('vitamin status',)), (r'叶酸', ('folate status',)),
    (r'铁蛋白|血清铁', ('iron status',)), (r'骨质|骨密度', ('bone density',)),
    (r'睡眠|失眠', ('sleep',)), (r'焦虑', ('anxiety',)), (r'抑郁', ('depression',)),
    (r'情绪', ('mood',)), (r'疼痛', ('pain',)),
    (r'尿酸', ('uric acid',)), (r'痛风', ('gout',)),
    (r'氧化应激|氧化胁迫|抗氧化', ('oxidative stress',)),
    (r'肠炎', ('intestinal inflammation',)), (r'感染性腹泻|肠道感染', ('enteric infection',)),
    (r'职业暴露|环境暴露', ('environmental exposure',)),
    (r'有机溶剂|化学物', ('solvent exposure',)), (r'农药|杀虫剂', ('pesticide exposure',)),
    (r'烟草|吸烟', ('smoking',)),
)


def _function_background_terms(code, ids, facts, background):
    """Ask about source-present context without turning it into a diagnosis."""
    from .evaluation_synthesis import _FUNCTION_BACKGROUND_THEMES
    theme = re.compile(_FUNCTION_BACKGROUND_THEMES.get(code, r'(?!)'), re.I)
    terms, admitted = [], []
    for fid in sorted(ids):
        fact = facts[fid]
        if fact.get('kind') not in {'clinical', 'self_reported'} and not (
                fid in background and background[fid].get('semanticType') == 'sourceReportedBackground'):
            continue
        name, value = str(fact.get('name') or ''), str(fact.get('value') or '')
        if re.search(r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹', name) or re.fullmatch(
                r'\s*(?:无|否|没有|否认|不详|正常|false)\s*[。.]?', value, re.I):
            continue
        text = str(background[fid].get('sourceDescription') or value) if fid in background else name + ' ' + value
        matched = []
        for clause in re.split(r'[，,。；;、！？\n]', text):
            if re.search(r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹|(?:未见|没有|否认|排除|不伴|^无)[^。；;]{0,12}', clause):
                continue
            for pattern, aliases in _FUNCTION_BACKGROUND_TERMS:
                if any(theme.search(match[0]) for match in re.finditer(pattern, clause)):
                    matched.extend(aliases)
        if matched:
            admitted.append(fid)
            terms.extend(matched)
    return _unique(terms), _unique(admitted)


def _function_query_members(pathway, facts, native_context):
    """Narrow retrieval to real abnormal members, without deriving a grade."""
    from .evaluation_catalog import (NATIVE_DETAILED_ITEM_DEFINITIONS,
        DETAILED_METABOLISM_ITEM_DEFINITIONS, fixed_metric_code)
    rows = [row for row in pathway.get('relatedMetrics', []) if _row_ids(row, facts)]
    ids = {fid for row in rows for fid in _row_ids(row, facts)}
    abnormal = {member['name'] for group in native_context for member in group['members']
        if member.get('direction') in {'up', 'down'} and ids.intersection(member['factIds'])}
    chosen = [row for row in rows if row.get('name') in abnormal]
    if not chosen:
        chosen = [row for row in rows if _LOW.search(str(row.get('sourceStatus') or ''))]
    definitions = _unique(pair for group in (*NATIVE_DETAILED_ITEM_DEFINITIONS.values(),
        *DETAILED_METABOLISM_ITEM_DEFINITIONS.values()) for pair in group)
    aliases = []
    for row in chosen[:3]:
        code = fixed_metric_code(row.get('name'), definitions)
        if code:
            english = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', code).lower()
            if english.isascii() and not english.startswith('toxin:'):
                aliases.extend((english, english[:-1]) if english.endswith('s') else (english,))
            aliases.extend(FIXED_METRIC_ALIASES.get(code, ()))
        aliases.append(row['name'])
    return _unique(aliases)
_HEALTH_TERMS = {
    'beneficialBacteria': ('gut commensal bacteria colonization resistance',
        ('commensal bacteria', 'beneficial bacteria', 'colonization resistance', '有益菌')),
    'scfaProducerBacteria': ('gut short-chain fatty acid producing bacteria',
        ('short-chain fatty acid', 'SCFA', 'butyrate', '产短链脂肪酸')),
    'pathogenicBacteria': ('gut enteric pathogens', ('enteric pathogen', 'pathogenic bacteria', '致病菌')),
    'opportunisticBacteria': ('gut pathobionts inflammation', ('pathobiont', 'pathobionts',
        'opportunistic bacteria', 'opportunistic pathogens', '条件致病菌')),
    'otherHealthBacteria': ('gut commensal bacteria inflammation', ('commensal bacteria', 'inflammation', '健康相关菌')),
    'pathogenicPotential': ('gut microbial virulence', ('virulence', 'pathogenic potential', '致病潜力')),
    'antiInflammatoryCapacity': ('gut microbiota anti-inflammatory function',
        ('anti-inflammatory', 'inflammation', '抗炎')),
    'gutImmunity': ('gut microbiota mucosal immunity', ('mucosal immunity', 'immune', '肠道免疫')),
    'dietaryFiberNeed': ('gut microbiota dietary fiber fermentation',
        ('dietary fiber', 'dietary fibre', 'fermentation', '膳食纤维')),
    'biofilmCapacity': ('gut microbial biofilms', ('biofilm', 'biofilms', '生物膜')),
    'oxidativeStressTolerance': ('gut microbial oxidative stress', ('oxidative stress', '氧化胁迫')),
    'virulenceGenes': ('gut microbial virulence genes',
        ('virulence gene', 'virulence genes', 'virulence factor', 'virulence factors', '毒力基因')),
    'antibioticResistanceGenes': ('gut antibiotic resistome', ('antibiotic resistance', 'resistome', '耐药基因')),
    'pathogenicFungi': ('gut fungal pathobionts', ('fungal', 'fungi', '真菌')),
    'pathogenicViruses': ('gut virome human health', ('virome', 'virus', 'viruses', '病毒')),
    'pathogenicParasites': ('intestinal parasites human health', ('parasite', 'parasites', '寄生虫')),
    'keystoneBacteria': ('human gut keystone bacteria microbial ecology',
        ('keystone bacteria', 'keystone species', 'microbial ecology', '基石菌')),
    'barrierRelatedBacteria': ('human gut bacteria intestinal barrier function',
        ('intestinal barrier', 'epithelial barrier', 'barrier integrity', '屏障')),
}
_MICROBIOME = ('gut microbiota', 'gut microbiome', 'intestinal microbiota',
    'intestinal microbiome', 'microbial', '肠道菌群', '肠道微生物')
_FUNCTION_OBJECTS = (*_MICROBIOME, 'bacteria', 'bacterial', 'microorganism', 'microorganisms')
_HEALTH_PROCESS_ALIASES = (
    'fermentation', 'biosynthesis', 'synthesis', 'metabolism', 'metabolic', 'metabolize', 'metabolise',
    'production', 'produce', 'produces', 'producing', 'regulate', 'regulates', 'regulation',
    'modulate', 'modulates', 'modulation', 'maintain', 'maintains', 'maintenance',
    'protect', 'protects', 'protection', 'inhibit', 'inhibits', 'inhibition',
    'barrier integrity', 'immunomodulatory', '发酵', '合成', '代谢', '生成', '调节', '维持', '保护', '抑制',
    'regulatory', 'immunomodulation', 'enhance', 'enhances', 'enhancement',
    'interaction', 'interactions', 'cross-feeding', 'tight junction', 'epithelial',
)
_GENERIC_HEALTH_OBJECTS = frozenset(('commensal bacteria', 'colonization resistance',
    'bacteria', 'bacterial', 'microorganism', 'microorganisms'))
_PATHOGEN_PROCESSES = ('invasion','invade','colonization','colonisation','infection',
    'pathogenesis','virulence','intracellular','translocation','侵袭','定植','感染','致病','胞内','移位')
_NONBACTERIAL_HEALTH_PANELS = {
    'pathogenicFungi': 'fungi_pathogenic', 'pathogenicViruses': 'virus',
    'pathogenicParasites': 'parasites',
}


def _health_function_concepts(latin, aliases):
    # A member or this indicator's own domain is different from generic
    # commensals/colonization resistance in an unrelated resistome abstract.
    domains = [alias for alias in aliases if alias not in latin and alias not in _GENERIC_HEALTH_OBJECTS]
    objects = _unique([*domains, *latin])
    return (objects, _FUNCTION_OBJECTS, _HEALTH_PROCESS_ALIASES)


def _health_function_topic(topic):
    return 'health_summary' in topic.source_semantics and len(topic.query_concepts) == 3


def _function_source_binding_required(topic):
    return _health_function_topic(topic) or 'measured_ecological_dimension' in topic.source_semantics or any(marker in topic.source_semantics for marker in (
        'measured_transmitter_role', 'measured_sugar_role', 'measured_metabolite_role'))


def _function_topic_source_match(topic, source, *, title=''):
    if 'measured_ecological_dimension' in topic.source_semantics:
        # A vitamin/chemotherapy trial that happens to report diversity is
        # not this dimension's metabolic-health association. This is only a
        # necessary original-object screen; human scope and claim strength
        # still require both independent sentence reviews.
        from .evaluation_function_evidence import ecological_association_priority
        return bool(len(topic.query_concepts) == 3 and all(_mentions(source, group)
            for group in topic.query_concepts) and ecological_association_priority(
                source,title,topic.query_concepts[0],topic.query_concepts[2]))
    if any(marker in topic.source_semantics for marker in ('measured_sugar_role', 'measured_metabolite_role')):
        from modules.report.clinical_report import _review_quote_options
        from modules.report.evaluation_reference_plan import measured_function_sentence_role
        code = 'P1' if 'measured_sugar_role' in topic.source_semantics else 'P9'
        if code=='P1':
            from modules.report.evaluation_content import measured_sugar_study_scope_failures
            if measured_sugar_study_scope_failures(source,title=title):
                return False
        return all(_mentions(source, concepts) for concepts in topic.query_concepts) and any(
            measured_function_sentence_role(code, quote, topic.query_concepts[0],source=source)
            for quote in _review_quote_options(source).values())
    if 'measured_transmitter_role' in topic.source_semantics:
        from modules.report.clinical_report import _review_quote_options
        from modules.report.evaluation_reference_plan import _transmitter_sentence_role
        return all(_mentions(source, concepts) for concepts in topic.query_concepts) and any(
            _transmitter_sentence_role(quote, topic.query_concepts[0])[0]
            for quote in _review_quote_options(source).values())
    if _health_function_topic(topic):
        from modules.report.evaluation_reference_plan import _source_object_in_quote
        return all(_mentions(source, concepts) or any(
            re.fullmatch(r'[A-Z][a-z]+ [a-z][a-z-]+', term)
            and _source_object_in_quote(term, source, source, title=title)
            for term in concepts) for concepts in topic.query_concepts)
    return _mentions(source, topic.aliases) and _mentions(source, _FUNCTION_OBJECTS)


_GUT_RESISTOME_OBJECTS = ('gut', 'intestinal', 'fecal', 'faecal', 'microflora', '肠道', '粪便')
_TRANSMITTER_GUT_OBJECTS = ('intestinal', 'colonic', 'gut', 'enteric', 'bowel', 'intestine', 'intestines', '肠道', '结肠')
_TRANSMITTER_ROLE_TERMS = {
    'serotonin': ('motility', 'secretion', 'secretory', 'physiological function', 'physiological role'),
    'dopamine': ('motility', 'secretion', 'receptor', 'signaling', 'synthesis', 'production', 'tyrosine'),
    'gaba': ('synthesis', 'production', 'biosynthesis', 'decarboxylase', 'glutamate', 'inhibitory', 'receptor'),
}


def _measured_transmitter_role(rows, facts):
    """One existing topic asks the role of a truly measured transmitter."""
    members = {}
    for row in rows:
        if not _present(row.get('value')) or not _row_ids(row, facts):
            continue
        name = str(row.get('name') or '')
        key = re.sub(r'\W+', '', unicodedata.normalize('NFKC', name)).casefold()
        for code, printed in NEUROTRANSMITTER_DEFINITIONS:
            aliases = FIXED_METRIC_ALIASES.get(code, (printed,))
            if any(key == re.sub(r'\W+', '', unicodedata.normalize('NFKC', alias)).casefold() for alias in aliases):
                members[code] = tuple(alias for alias in aliases if alias.isascii() and re.search(r'[A-Za-z]', alias))
    preferred = next((code for code in ('serotonin', 'dopamine', 'gaba') if code in members), next(iter(members), None))
    if preferred is None or not members[preferred]:
        return None
    aliases = members[preferred]
    roles = _TRANSMITTER_ROLE_TERMS.get(preferred, ('synthesis', 'production', 'receptor', 'physiological', 'signaling'))
    if preferred != 'serotonin':
        roles = _unique([*roles, *_BIOSYNTHESIS_DUTY_ALIASES])
    return aliases, roles
# Search aliases for organisms explicitly named in a supplier's gene-panel
# title. They narrow literature recall; they never establish organism detection.
_GENE_PANEL_SEARCH_TAXA = {
    '艰难梭菌': ('Clostridioides difficile', 'Clostridium difficile'),
    '阴沟肠杆菌': ('Enterobacter cloacae',),
    '志贺': ('Shigella',),
}
_LOW = re.compile(r'偏低|较弱|弱|极低|不足|高风险|异常|注意')


def _gene_panel_search_taxa(title):
    return _unique(alias for name, aliases in _GENE_PANEL_SEARCH_TAXA.items()
        if name in title for alias in aliases)


def _plain(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return value.model_dump(mode='json', by_alias=True) if hasattr(value, 'model_dump') else {}


def _key(value: Any) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or value.strip() not in {'', '--', 'N/A', '待提供', '未提供'})


def _unique(values):
    return tuple(dict.fromkeys(values))


def _topic_id(kind: str, identity: str) -> str:
    return 'eval-topic:' + kind + ':' + sha256(_key(identity).encode()).hexdigest()[:16]


def _row_ids(row: dict, facts: dict[str, dict], *, measured: bool = True) -> tuple[str, ...]:
    if measured and not _present(row.get('value')):
        return ()
    ids = []
    for source in row.get('sources', []):
        fid = source.get('recordId')
        fact = facts.get(fid)
        if fact is None or not _present(fact.get('value')):
            continue
        if measured and _key(row.get('value')) != _key(fact.get('value')):
            try:
                if Decimal(str(row['value'])) != Decimal(str(fact['value'])):
                    continue
            except (InvalidOperation, ValueError):
                continue
        observed = source.get('observedAt')
        if observed and fact.get('date') and observed != fact['date']:
            continue
        ids.append(fid)
    return _unique(ids)


def _source_bindings(ids, facts, source_codes):
    return tuple({**deepcopy(facts[fid]), 'sourceCode': source_codes.get(fid)} for fid in ids)


_TRIGGER_EXPOSURES = {
    'diet': (
        (r'蔬菜|膳食纤维|粗粮|全谷', 'dietary fiber', ('dietary fiber', 'dietary fibre',
            'dietary fibers', 'dietary fibres', 'vegetable', 'vegetables', '膳食纤维', '蔬菜')),
        (r'饮水|喝水|毫升', 'fluid intake', ('fluid intake', 'water intake', 'hydration', '饮水')),
        (r'夜宵|晚餐|进餐', 'meal timing', ('meal timing', 'late eating', 'meal regularity',
            'bedtime snacks', 'late night meals', 'late-night meals', 'dinner-to-bed',
            'dinner to bed', 'eating before bedtime', 'late evening meals',
            '进餐时间', '餐时', '晚餐', '夜宵')),
        (r'咖啡|咖啡因', 'caffeine intake', ('caffeine', 'coffee', '咖啡因', '咖啡'))),
    'exercise': ((r'步行|骑车|运动|锻炼|散步|走路', 'physical activity',
        ('physical activity', 'exercise', 'walking', '运动', '步行', '骑车', '锻炼', '散步', '走路')),
        (r'久坐|连续坐', 'sedentary behavior',
            ('sedentary behavior', 'sedentary behaviour', 'sitting time', '久坐', '连续坐'))),
    'sleep': (
        (r'手机|屏幕', 'evening screen use', ('screen use', 'screen time', 'electronic media', '屏幕', '手机')),
        (r'上床|入睡|睡眠|睡觉|起床', 'sleep regularity', ('sleep regularity', 'sleep timing', 'circadian', '睡眠节律', '作息'))),
    'stress': ((r'压力|紧张|焦虑', 'psychological stress',
        ('psychological stress', 'perceived stress', 'stress', '心理压力', '压力')),),
    'exposures': (
        (r'饮酒|啤酒|白酒|红酒', 'alcohol consumption', ('alcohol consumption', 'alcohol intake', 'alcohol', '饮酒')),
        (r'二手烟', 'secondhand smoke exposure', ('secondhand smoke', 'passive smoking', '二手烟')),
        (r'吸烟|抽烟', 'smoking', ('smoking', 'tobacco', '吸烟'))),
}
_TRIGGER_OUTCOMES = (
    (r'便秘|排便费力', 'constipation', ('constipation', '便秘', '排便困难')),
    (r'腹胀', 'abdominal bloating', ('abdominal bloating', 'bloating', '腹胀')),
    (r'入睡困难|睡眠困难|失眠', 'sleep onset difficulty', ('sleep onset', 'sleep latency', 'insomnia', '入睡困难', '失眠')),
    (r'反酸|烧心', 'gastroesophageal reflux symptoms',
        ('gastroesophageal reflux', 'acid reflux', 'reflux symptoms', 'heartburn', '反酸', '烧心')),
)
_FAMILY_CONDITIONS = (
    (r'冠心病|冠状动脉疾病', 'coronary heart disease', ('coronary heart disease', 'coronary artery disease', '冠心病')),
    (r'糖尿病', 'diabetes', ('diabetes', '糖尿病')),
    (r'结直肠癌|结肠癌|直肠癌', 'colorectal cancer', ('colorectal cancer', '结直肠癌', '结肠癌', '直肠癌')),
    (r'高血压', 'hypertension', ('hypertension', '高血压')),
)
_UNKNOWN_TRIGGER = re.compile(r'记不清|不清楚|不确定|未知|不详|不知道|无法补充|未提供')


def _health_literal_terms(row, ids, facts):
    """Retrieve literal source taxa/gene names, without translating a label.

    In particular gtrB cannot be silently renamed stx. A detected gene list
    and a panel's candidate list are different source observations.
    """
    genes=[]
    original='；'.join(str(row.get(key) or '') for key in ('value','details'))
    for match in re.finditer(r'(?:^|[；;])\s*检出基因[：:]\s*([^；;\n]+)',original):
        genes.extend(gene.strip() for gene in re.split(r'[,，、\s]+',match[1])
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',gene.strip()))
    latin=[]
    for fid in ids:
        fact=facts[fid]
        if fact.get('kind')!='microbe':
            continue
        names=str(fact.get('name') or '').split(' / ')
        latin.extend(name.strip() for name in names if re.fullmatch(
            r'[A-Z][a-z]+(?:\s+[a-z][a-z.-]*){0,3}',name.strip()))
    return _unique(genes),_unique(latin)


def health_summary_member_rows(row,rows,facts):
    """Expand explicit native panel members, never unrelated source taxa."""
    def files(item):
        return {source for ref in item.get('sources',[]) if ref.get('recordId') in facts
            and (source:=_plain(facts[ref['recordId']]).get('source'))}
    own=files(row)
    if panel := _NONBACTERIAL_HEALTH_PANELS.get(row.get('code')):
        # An explicit native summary can be negative and still has a medical
        # meaning. Its measured panel members are the search objects, not a
        # guessed infection or a neighbour's organisms. Require both the
        # exact panel identity and a shared adopted source and report date.
        def dates(item):
            return {date for ref in item.get('sources', []) if ref.get('recordId') in facts
                and (date := _plain(facts[ref['recordId']]).get('date'))}
        own_dates = dates(row)
        return [item for item in rows if item is not row
            and str(item.get('code') or '').startswith('panelResult:' + panel + ':')
            and _present(item.get('value')) and own.intersection(files(item))
            and (not own_dates or not dates(item) or own_dates.intersection(dates(item)))]
    if row.get('code')!='virulenceGenes' or not row.get('details'):
        return []
    return [item for item in rows if item is not row and item.get('name')
        and item['name'] in row['details'] and _present(item.get('value'))
        and not is_confirmed_negative_health(item) and own.intersection(files(item))]


def _trigger_question(code, own, symptoms, *, preferred_outcome=None, preferred_exposure=None):
    """Ask about a literal behaviour and an observed symptom, not its cause.

    A family record remains family context. Unknown exposure is not a positive
    exposure, and denial of smoking does not create a smoking-risk question.
    Missing cup/bottle volume does not erase an otherwise known behaviour.
    """
    if code == 'familyHistory':
        for fact in own:
            value = str(fact.get('value') or '')
            if _UNKNOWN_TRIGGER.search(value) or re.search(r'无.{0,8}(?:家族史|病史)|没有|否认', value):
                continue
            for pattern, term, aliases in _FAMILY_CONDITIONS:
                if re.search(pattern, value):
                    family = ('family history', 'familial', '家族史', '一级亲属')
                    return f'family history {term}', (*family, *aliases), (family, aliases)
        return None
    if code == 'sleep' and any(fact.get('kind') in {'clinical', 'self_reported'}
            and re.search(r'(?:夜间睡眠|每晚(?:睡眠|睡觉|睡)|睡眠(?:时长|时间)?)'
                r'[^。；;，,]{0,8}\d+(?:\.\d+)?(?:\s*[–—-]\s*\d+(?:\.\d+)?)?\s*(?:小时|h\b)',
                str(fact.get('value') or ''), re.I) for fact in own):
        # A real stated duration needs an age-appropriate reference, not a
        # forced bowel/GERD association. Retrieval never supplies a patient
        # duration, age, diagnosis or a threshold; normal review still checks
        # the actual guideline's population against this user's real data.
        duration = ('sleep duration', 'sleep amount', 'amount of sleep', '睡眠时长')
        recommendation = ('recommendation', 'recommended', 'consensus', 'guideline', '推荐', '共识')
        return ('recommended sleep duration consensus', (*duration, *recommendation),
            (duration, recommendation))
    if not symptoms:
        return None

    # Use only the symptom actually supplied, with sleep behaviours paired to
    # the supplied sleep complaint rather than an unrelated bowel symptom.
    preferred = preferred_outcome if preferred_outcome is not None else 2 if code in {'sleep', 'exposures'} else 0
    symptom = next((item for item in symptoms if item[0] == preferred),
        None if preferred_outcome is not None else symptoms[0])
    if symptom is None:
        return None
    if code=='diet'and symptom[0]==3 and preferred_exposure is None:
        preferred_exposure='meal timing'
    _, outcome, outcome_aliases = symptom
    if code == 'medications':
        # Keep the adopted report's literal drug names; do not guess a drug
        # class or turn medication context into permission to change therapy.
        names = [name.strip() for fact in own for name in re.split(r'[,，、;；]', str(fact.get('value') or ''))
            if name.strip() and not _UNKNOWN_TRIGGER.search(name)]
        if names:
            return f'{names[0]} {outcome}', tuple(dict.fromkeys(names)), (tuple(dict.fromkeys(names)), outcome_aliases)
        return None
    for pattern, term, aliases in _TRIGGER_EXPOSURES.get(code, ()):
        if preferred_exposure is not None and term!=preferred_exposure:
            continue
        for fact in own:
            value = str(fact.get('value') or '')
            if not re.search(pattern, value):
                continue
            if code=='diet'and symptom[0]==3 and term=='meal timing'and not _meal_timing_observation(value):
                continue
            if _UNKNOWN_TRIGGER.search(value) and not re.search(
                    r'[0-9一二三四五六七八九十两]+(?:次|分钟|小时|杯|瓶|毫升|点|天)|通常|经常|偶尔|每天|每周', value):
                continue
            if code == 'exposures' and re.search(r'二手烟|吸烟|抽烟', value) and (
                    _UNKNOWN_TRIGGER.search(value) or re.search(r'不吸烟|未吸烟|从不|不抽烟|否认', value)):
                continue
            return f'{term} {outcome}', aliases, (aliases, outcome_aliases)
    return None


def _meal_timing_observation(value):
    """A real meal-time/late-snack answer, not a vegetable mention at dinner."""
    return any(re.search(r'夜宵|(?:晚餐|进餐|用餐)[^。；;]{0,24}'
            r'(?:[0-9一二三四五六七八九十两]+点(?:钟|半|前|后|左右|吃|用餐|进餐|$)|时间|偏晚|较晚|很晚|不固定|不规律)',part)
        and not _UNKNOWN_TRIGGER.search(part)
        and not re.search(r'从不|从未|不吃|没有|否认|^无',part)
        for part in re.split(r'[。；;]',str(value or '')))


_RISK_OBJECT_ALIASES = (
    ('血压', 'blood pressure'),
    ('腰围', 'waist circumference'),
    ('体重', 'body weight'),
    ('糖化血红蛋白', 'HbA1c', 'hemoglobin A1c', 'glycated hemoglobin'),
    ('空腹血糖', 'fasting glucose', 'fasting plasma glucose'),
    ('甘油三酯', 'triglycerides', 'triglyceride', 'TG'),
    ('低密度脂蛋白胆固醇', 'LDL cholesterol', 'LDL-C', 'low-density lipoprotein cholesterol', 'low density lipoprotein cholesterol'),
    ('胆固醇', 'cholesterol', 'plasma cholesterol', 'blood cholesterol'),
    ('膳食纤维', '膳食纤维摄入', 'dietary fiber', 'dietary fibre', 'dietary fiber intake'),
    ('排便频率', 'stool frequency', 'bowel movement frequency'),
    ('硬便', 'hard stool', 'hard stools'),
    ('排便费力', '用力排便', 'straining'),
    ('晚餐', '餐时', 'meal timing', 'late dinner', 'late eating', 'late-night eating'),
    ('反酸', 'acid regurgitation', 'heartburn'),
    ('久坐', 'sedentary behavior', 'sedentary behaviour', 'sitting time'),
    ('身体活动', '运动', 'physical activity', 'exercise'),
    ('睡眠时长', 'sleep duration', 'short sleep duration'),
    ('菌群多样性', '肠道菌群多样性', 'gut microbiota diversity', 'microbiota diversity', 'microbial diversity'),
    ('肠道免疫力', 'gut immunity', 'intestinal immunity', 'intestinal mucosal immunity'),
    ('基石菌', '肠道基石菌构成', 'keystone bacteria', 'keystone species'),
)


def risk_retrieval_concepts(terms, *, source_names=()):
    """Separate a search object's name from its patient's value/direction.

    The immutable facts keep the original values, units and directions. A
    paper is searched for the medical concept, not for that patient's exact
    HbA1c/BP number. Strain IDs, gene/variant names and HDL/LDL identity must
    not be simplified by measurement cleanup.
    """
    return _risk_retrieval_concepts_cached(
        tuple(term for term in terms if isinstance(term, str)),
        tuple(term for term in source_names if isinstance(term, str)))


@lru_cache(maxsize=2048)
def _risk_retrieval_concepts_cached(terms, source_names):
    """Pure text normalization only; no patient decision or evidence verdict."""
    result = []
    for raw in [*terms, *source_names]:
        if not isinstance(raw, str) or not raw.strip():
            continue
        for part in re.split(r'[,，、；;]|\s+/\s+', unicodedata.normalize('NFKC', raw)):
            part = part.strip(' 。:：')
            part = re.sub(r'[（(](?:来源疾病模型指数|用户自述[^）)]*)[）)]', '', part)
            protected = bool(re.search(r'\b(?:ATCC|DSM|JCM|strain|subsp)\b|菌株|\b[cp]\.\d|\b(?:rs|HLA)[\d:-]', part, re.I)
                or re.fullmatch(r'[A-Z][a-z]+\s+[a-z][a-z.-]+\s+\d+', part))
            if not protected:
                # Only a trailing measurement is removed. IL-6, vitamin B12,
                # type 2 diabetes and a named microbial strain remain intact.
                part = re.sub(r'(?:(?<=\w)\s+|(?<=[\u4e00-\u9fff]))(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?'
                    r'(?:\s*[/：:]\s*\d+(?:\.\d+)?)?\s*'
                    r'(?:[%‰°]|[A-Za-zμµ][A-Za-zμµ0-9^/*×·().%-]*|分|岁)?\s*$', '', part, flags=re.I)
                part = re.sub(r'(?:偏高|偏低|异常|较低|较弱|很弱|不足|缺乏|升高|降低|过高|过低)$', '', part).strip()
                if not re.match(r'(?:low|high)[ -](?:density|fat|salt|protein|carbohydrate|carb|fiber|fibre|grade)\b', part, re.I):
                    part = re.sub(r'^(?:elevated|increased|decreased|reduced|abnormal|weak|low|high|'
                        r'mild|moderate|severe|occasional|frequent|prolonged|excessive|inadequate|insufficient)\s+', '', part, flags=re.I)
                part = re.sub(r'\s+(?:elevated|increased|decreased|reduced|abnormal)$', '', part, flags=re.I)
            part = part.strip(' 。:：')
            if len(part) >= 2:
                result.append(part)
    original = tuple(result)
    for aliases in _RISK_OBJECT_ALIASES:
        # A full actual concept can recall its translations. Do not infer an
        # unmeasured organ, specimen, organism or disease from numeric values.
        if any(_mentions(term, aliases) for term in original):
            result.extend(aliases)
    return _unique(result)


@lru_cache(maxsize=1)
def _clinical_retrieval_identity_map():
    from .evaluation_risk_assessment import _CLINICAL_ENTITY_ALIASES
    return {_key(alias): identifier for identifier, aliases in _CLINICAL_ENTITY_ALIASES.items()
        for alias in (identifier, *aliases)}


def _risk_search_disease_code(value):
    """Recognise existing disease identities; unknown aliases stay dynamic."""
    return _risk_search_disease_code_cached(str(value))


@lru_cache(maxsize=2048)
def _risk_search_disease_code_cached(value):
    code = fixed_disease_code(value)
    if code:
        return code
    clinical = _clinical_retrieval_identity_map().get(_key(value))
    if clinical:
        return clinical
    # These are named lipid disorders, not synonyms of fatty liver or
    # cardiovascular disease. Keep their parent identity for retrieval only;
    # never manufacture a patient diagnosis or alter a source disease row.
    if re.fullmatch(r'高甘油三酯血症|高胆固醇血症|hypertriglycerid(?:a?e)mia|'
            r'hypercholesterol(?:a?e)mia', str(value).strip(), re.I):
        return 'hyperlipidemia'
    return None


@lru_cache(maxsize=1)
def _risk_identity_patterns():
    from .evaluation_risk_assessment import _CLINICAL_ENTITY_ALIASES
    groups = tuple({**FIXED_DISEASE_ALIASES, **_CLINICAL_ENTITY_ALIASES}.items())
    patterns = []
    for code, aliases in groups:
        for alias in (code, *aliases):
            forms = [alias]
            if alias.endswith('疾病') and len(alias[:-2]) >= 3:
                forms.append(alias[:-2])
            for form in forms:
                if form:
                    pattern = (r'(?<![A-Za-z0-9])' + re.escape(form) + r'(?![A-Za-z0-9])'
                        if form.isascii() else re.escape(form))
                    patterns.append((re.compile(pattern, re.I), len(form), code))
    return groups, tuple(patterns)


@lru_cache(maxsize=2048)
def _risk_identity_matches(text):
    return tuple((match.end(), size, code) for pattern, size, code in _risk_identity_patterns()[1]
        for match in pattern.finditer(text))


def _risk_disease_aliases(name, terms):
    target = _risk_search_disease_code(name)
    return _unique([*_disease_retrieval_terms(name), *(term.strip() for term in terms
        if isinstance(term, str) and term.strip()
        and (not target or _risk_search_disease_code(term) in {None, target}))])


def _risk_outcome_aliases(candidate, relation=None):
    """Resolve a question's outcome independently of its measured exposure.

    This chooses retrieval terminology, never a patient diagnosis. Legacy
    questions may contain two illnesses (A -> B); the last explicitly named
    medical identity is their outcome. Display wrappers and search terms do
    not turn A into a synonym of B.
    """
    relation = relation or {}
    explicit = relation.get('outcomeTerms', relation.get('outcome_terms', []))
    groups = dict(_risk_identity_patterns()[0])

    def identities(text):
        return _risk_identity_matches(str(text))

    def compatible_aliases(code, supplied=()):
        # Keep supplied, plausible synonyms such as "chronic constipation"
        # or "coronary artery disease". Canonicalization must not erase
        # them merely because the catalog has only another English spelling.
        # Known *other diseases* and actual exposure objects remain separate.
        exposures = risk_retrieval_concepts([
            *candidate.get('exposureTerms', candidate.get('exposure_terms', [])),
            *relation.get('exposureTerms', relation.get('exposure_terms', []))])
        exposure_keys = {_key(term) for term in exposures}
        values = list(groups[code])
        for term in supplied:
            if not isinstance(term, str) or not term.strip():
                continue
            term = term.strip()
            matches = identities(term)
            other = max(matches)[2] if matches else _risk_search_disease_code(term)
            if other and other != code:
                continue
            if not other and _key(term) in exposure_keys:
                continue
            values.append(term)
        return _unique(values)

    if isinstance(explicit, list) and any(isinstance(term, str) and term.strip() for term in explicit):
        values = _unique(term.strip() for term in explicit if isinstance(term, str) and term.strip())
        known = {max(identities(term))[2] for term in values if identities(term)}
        # Explicit aliases cannot combine different known disease identities.
        if len(known) == 1:
            code = next(iter(known))
            return compatible_aliases(code, values)
        if not known:
            return values
    for text in (relation.get('question', ''), candidate.get('name', ''),
                 candidate.get('diseaseId', candidate.get('disease_id', ''))):
        matches = identities(text)
        if matches:
            code = max(matches)[2]
            return compatible_aliases(code,
                candidate.get('searchTerms', candidate.get('search_terms', [])))
    return _risk_disease_aliases(candidate.get('name', ''),
        candidate.get('searchTerms', candidate.get('search_terms', [])))


def _risk_function_search_concepts(terms, *, enabled, microbial=False):
    """Separate an existing function's molecule from its biological process.

    Report headings such as 'X synthesis capacity' are not exact phrases a
    paper must repeat. This changes retrieval grammar only: the underlying
    fact remains a predicted/measured function, never a human concentration.
    Molecules, strains, variants and pathway direction are not substituted.
    """
    if not enabled:
        return tuple(terms), ()
    processes = (
        (r'\b(?:biosynthesis|biosynthetic|synthesis|synthesi[sz]\w*|production|produc\w*)\b',
            ('synthesis', 'biosynthesis', 'biosynthetic', 'production', 'synthesized', 'synthesised', '合成', '生成')),
        (r'\b(?:degradation|catabolism|catabolic|breakdown|degrad\w*)\b',
            ('degradation', 'catabolism', 'catabolic', 'breakdown', 'degraded', 'degrading', 'degradative', '降解', '分解')),
        (r'\b(?:metabolism|metabolic|metaboli[sz]\w*)\b',
            ('metabolism', 'metabolic', 'metabolize', 'metabolise', '代谢')),
    )
    objects, actions = [], []
    for term in terms:
        if not term.isascii() or re.search(r'\b(?:ATCC|DSM|JCM|strain|subsp)\b|\b[cp]\.\d', term, re.I):
            continue
        matched = [aliases for pattern, aliases in processes if re.search(pattern, term, re.I)]
        report_score = bool(re.search(r'\b(?:function(?:al)?|biosynthetic|synthesis|metabolism)\b.*'
            r'\b(?:score|scoring|assessment|capacity|capability|potential)\b|'
            r'\b(?:score|scoring|assessment)\b', term, re.I))
        if not matched and not report_score:
            continue
        core = term
        for pattern, _ in processes:
            core = re.sub(pattern, ' ', core, flags=re.I)
        core = re.sub(r'\b(?:capacity|capability|capabilities|ability|potential|predicted|prediction|'
            r'functional|function|score|scores|scoring|assessment|estimated|and|of|for|microbial|'
            r'microbiome|microbiota|gut|intestinal)\b', ' ', core, flags=re.I)
        core = ' '.join(core.split()).strip(' -:')
        if len(core) < 2 or not re.search('[A-Za-z]', core):
            continue
        objects.append(core)
        if core.endswith((' acid', 'lipid', 'vitamin', 'metabolite', 'neurotransmitter')):
            objects.append(core + 's')
        for aliases in matched:
            actions.extend(aliases)
        microbial = microbial or bool(re.search(r'\bmicrobial|microbiome|microbiota|gut|intestinal\b', term, re.I))
    if not objects:
        return tuple(terms), ()
    qualifiers = (_unique(actions),) if actions else ()
    if microbial:
        qualifiers += (('microbial', 'microbiota', 'microbiome', 'bacterial', 'bacteria',
            'microorganisms', '菌群', '肠道细菌'),)
    return _unique(objects), qualifiers


def _current_management_problem(document, row, aliases, ids, facts):
    """Reuse a same-problem clinical finding, not every shared abnormal fact."""
    if row.get('certainty') in {'confirmed', 'current_problem', 'self_reported'}:
        return True
    codes = {_risk_search_disease_code(term) for term in aliases} - {None}
    raw = _plain(document)
    for problem in raw.get('problems', []):
        sources = problem.get('sources', [])
        bound = {source.get('factId') for source in sources} & set(ids)
        if not any(facts.get(fid, {}).get('kind') == 'clinical'
                and facts[fid].get('abnormal') is True for fid in bound):
            continue
        terms = [problem.get('title') or '', *problem.get('searchTerms', [])]
        problem_codes = {_risk_search_disease_code(term) for term in terms} - {None}
        if codes & problem_codes:
            return True
        # An unfamiliar disease/finding remains supported dynamically by its
        # literal same-problem identity; a shared LDL value alone is not one.
        if not codes and any(_mentions(term, aliases) for term in terms):
            return True
    return False


def _current_management_topic(topic):
    return ('current_management_problem' in topic.source_semantics or any(value in topic.source_semantics
        for value in ('risk_certainty:confirmed', 'risk_certainty:current_problem', 'risk_certainty:self_reported')))


def _optional_summary_consumer(topic, path):
    return (path[:2] in {('healthOverview', 'overallSummary'), ('healthOverview', 'summaries'),
        ('comprehensiveConclusion', 'overallConclusion'), ('nutritionAssessment', 'nutritionInterpretation')}
        or path[:2] == ('microbiomeAssessment', 'structureInterpretation')
        and 'measured_ecological_dimension' not in topic.source_semantics)


def _with_consumer_duties(topic):
    optional = _unique([*topic.optional_consumer_paths,
        *(path for path in topic.consumer_paths if _optional_summary_consumer(topic, path))])
    primary = _unique([*topic.primary_consumer_paths,
        *(path for path in topic.consumer_paths if path not in optional)])
    return replace(topic, primary_consumer_paths=primary, optional_consumer_paths=optional)


def _current_clinical_action(topic):
    return topic.kind == 'risk_management' and 'current_clinical_action' in topic.source_semantics


def _clinical_query_groups(topics):
    """One same-problem guideline search can recall meaning and action originals.

    Candidate selection and sentence approval remain separate per topic/role.
    Sharing requires the explicit problem identity, not merely a shared lab.
    """
    actions = {topic.source_support_ids[0]: topic for topic in topics
        if _current_clinical_action(topic) and topic.source_support_ids}
    groups = {}
    for topic in topics:
        if topic.kind == 'clinical' and topic.source_support_ids:
            action = actions.get(topic.source_support_ids[0])
            if action is not None:
                groups[topic.topic_id] = (topic, action)
    return groups


def _shared_clinical_query(topic, members, *, public=False, expansion=False):
    query = _query(topic, public=public, expansion=expansion)
    if len(members) < 2:
        return query
    return replace(query,
        supports=_unique([*query.supports, *(member.topic_id for member in members)]))


def _risk_retrieval_priority(topic):
    """Real personal questions before low-specificity source-screen loops."""
    if 'specific_personal_exposure' in topic.source_semantics:
        return 0
    if topic.priority == 1:
        return 1
    if topic.query_relations and topic.query_relations[0].get('kind') in {'microbiome', 'function'}:
        return 2
    return 3


def _risk_relation_semantics(relation, facts, certainty):
    ids = relation.get('factIds', []) if relation else []
    concrete = bool(relation and relation.get('kind') in {'clinical', 'behavior'} and any(
        facts.get(fid, {}).get('kind') in {'clinical', 'self_reported'}
        and str(facts[fid].get('value') or '').strip()
        and not re.fullmatch(r'否|无|没有|0|未检出|阴性', str(facts[fid].get('value') or '').strip())
        for fid in ids))
    return ('risk_certainty:' + str(certainty or 'potential_risk'),
        *(('specific_personal_exposure',) if concrete else ()))


def _risk_candidate_topics(document, facts, source_codes):
    """Ask about derived risks without turning a hypothesis into a source fact.

    Disease and exposure are separate required concepts. The latter comes
    from the candidate's reviewed source-bound terms, with literal source
    names as a compatibility fallback, not from a guessed disease mechanism.
    """
    registry = getattr(document, 'risk_registry', None)
    if registry is None:
        raw = _plain(document)
        registry = raw.get('riskRegistry', raw.get('risk_registry', {}))
    candidates = getattr(registry, 'candidates', None)
    if candidates is None:
        candidates = _plain(registry).get('candidates', [])
    category_order = tuple(code for code, _ in RISK_CATEGORY_DEFINITIONS)
    result = []
    for candidate in candidates or ():
        row = _plain(candidate)
        rid = row.get('riskId', row.get('risk_id'))
        name, category = row.get('name'), row.get('category')
        positive = row.get('supportFactIds', row.get('support_fact_ids', []))
        context = [*row.get('opposingFactIds', row.get('opposing_fact_ids', [])),
            *row.get('backgroundFactIds', row.get('background_fact_ids', []))]
        ids = _unique(fid for fid in positive if fid in facts)
        if not isinstance(rid, str) or not rid or not isinstance(name, str) or not name.strip() or not ids:
            continue
        if category not in category_order:
            continue
        aliases = _risk_outcome_aliases(row)
        exposure = risk_retrieval_concepts(row.get('exposureTerms', row.get('exposure_terms', [])),
            source_names=[facts[fid].get('name') for fid in ids
                if '来源疾病模型指数' not in str(facts[fid].get('name') or '')])
        exposure = tuple(term for term in exposure if _key(term) not in {_key(alias) for alias in aliases})
        if not exposure:
            literal = [part.strip() for fid in ids for part in str(facts[fid].get('name') or '').split(' / ')
                if part.strip() and _key(part) not in {_key(alias) for alias in aliases}]
            exposure = _unique(literal)
        relations = []
        for item in row.get('relations', []):
            relation = _plain(item)
            relation_ids = _unique(fid for fid in relation.get('factIds', relation.get('fact_ids', [])) if fid in facts)
            if relation.get('kind') == 'source_model' or not relation_ids:
                continue
            outcome = _risk_outcome_aliases(row, relation)
            terms = risk_retrieval_concepts(relation.get('exposureTerms', relation.get('exposure_terms', [])),
                source_names=[facts[fid].get('name') for fid in relation_ids])
            terms = tuple(term for term in terms if _key(term) not in {_key(alias) for alias in outcome})
            if terms:
                terms, function_concepts = _risk_function_search_concepts(terms,
                    enabled=relation.get('kind') == 'function'
                        or any(facts[fid].get('kind') == 'function' for fid in relation_ids),
                    microbial=any(source_codes.get(fid) == 'C' for fid in relation_ids))
                relations.append({'relationId': relation.get('relationId', relation.get('relation_id')),
                    'kind': relation.get('kind'), 'factIds': list(relation_ids),
                    'exposureTerms': list(terms), 'question': relation.get('question'),
                    'outcomeTerms': list(outcome),
                    'functionConcepts': function_concepts})
        # A source model with no separate exposure still has the ordinary
        # source-model topic. A current illness can itself be an exposure for
        # a different explicit outcome, so inspect relations before skipping.
        if not exposure and not relations:
            continue
        relations.sort(key=lambda relation: ('clinical', 'behavior', 'microbiome', 'function').index(relation['kind'])
            if relation['kind'] in {'clinical', 'behavior', 'microbiome', 'function'} else 5)
        fallback_exposure, fallback_function = _risk_function_search_concepts(exposure,
            enabled=not relations and any(facts[fid].get('kind') == 'function' for fid in ids)
                and not any(facts[fid].get('kind') in {'clinical', 'self_reported'} for fid in ids),
            microbial=any(source_codes.get(fid) == 'C' for fid in ids))
        first_exposure = tuple(relations[0]['exposureTerms']) if relations else fallback_exposure
        first_outcome = tuple(relations[0]['outcomeTerms']) if relations else aliases
        clinical_objects = risk_retrieval_concepts([], source_names=[facts[fid].get('name')
            for fid in [*ids, *context] if fid in facts and facts[fid].get('kind') == 'clinical'])
        term = next((alias for alias in first_outcome if alias.isascii() and re.search('[A-Za-z]', alias)), name)
        priority = row.get('priority', 2)
        priority = priority if type(priority) is int and 1 <= priority <= 3 else 2
        topic = EvidenceTopic('risk:' + rid, 'risk', name, ids,
            _unique(facts[fid]['kind'] for fid in ids), _unique(facts[fid]['name'] for fid in ids),
            ('source_data', 'population_association', 'individual_synthesis'),
            ('population_association',), (
                ('diseaseRiskAssessment', 'categories', category_order.index(category), 'clinicalCorrelation', 'text'),
                ('healthOverview', 'overallSummary', 'text'),
                ('comprehensiveConclusion', 'overallConclusion', 'text')),
            _source_bindings(ids, facts, source_codes), term, first_outcome, priority,
            (rid,), ('derived_risk_candidate', *_risk_relation_semantics(
                relations[0] if relations else None, facts, row.get('certainty'))),
            _unique(fid for fid in context if fid in facts and fid not in ids),
            True, (first_outcome, first_exposure, *(relations[0].get('functionConcepts', ()) if relations else fallback_function)),
            query_relations=tuple(relations))
        result.append(topic)
        for relation in relations[1:]:
            relation_id = relation.get('relationId')
            if not relation_id:
                continue
            relation_ids = tuple(relation['factIds'])
            outcome = tuple(relation['outcomeTerms'])
            result.append(replace(topic, topic_id='risk:' + rid + ':relation:' + relation_id,
                fact_ids=relation_ids, source_kinds=_unique(facts[fid]['kind'] for fid in relation_ids),
                source_objects=_unique(facts[fid]['name'] for fid in relation_ids),
                source_bindings=_source_bindings(relation_ids, facts, source_codes),
                source_support_ids=(rid, relation_id), source_semantics=('derived_risk_candidate', 'additional_risk_relation',
                    *_risk_relation_semantics(relation, facts, row.get('certainty'))),
                aliases=outcome, query_term=next((alias for alias in outcome
                    if alias.isascii() and re.search('[A-Za-z]', alias)), name),
                query_concepts=(outcome, tuple(relation['exposureTerms']), *relation.get('functionConcepts', ())),
                query_relations=(relation,)))
        # This is a distinct evidence duty, not permission to prescribe an
        # action for every candidate. The final assessor decides whether an
        # adjustment is appropriate for these actual facts and this person.
        result.append(replace(topic, topic_id='risk-management:' + rid, kind='risk_management',
            aliases=aliases, query_term=next((alias for alias in aliases
                if alias.isascii() and re.search('[A-Za-z]', alias)), name),
            allowed_roles=('source_data', 'management', 'individual_synthesis'), required_roles=('management',),
            source_semantics=('derived_risk_candidate', 'risk_management',
                'risk_certainty:' + str(row.get('certainty') or 'potential_risk'),
                *(('current_management_problem',) if _current_management_problem(
                    document, row, aliases, ids, facts) else ())), query_enabled=priority <= 2,
            # Measurements guide applicability and optional search hints;
            # they cannot replace the disease identity as OR synonyms.
            query_concepts=(aliases, _RISK_MANAGEMENT_TERMS),
            query_measured_objects=clinical_objects,
            consumer_paths=(('comprehensiveConclusion', 'overallConclusion', 'text'),)))
    return tuple(result)


def _management_query_groups(topics):
    """Coalesce identical retrieval duties, never their clinical ownership.

    Equal wording alone is insufficient: every required concept, exposure,
    population and retrieval scope must also agree. Original topics remain
    in the plan for separate selection, applicability and sentence review.
    """
    representatives, members, identities = [], {}, {}
    for topic in topics:
        if topic.kind != 'risk_management':
            representatives.append(topic)
            continue
        query = _query(topic, public=False)
        identity = (_key(query.query), tuple(tuple(sorted({_key(term) for term in group}))
                for group in query.required_concepts),
            query.intent, query.relation_disease_id, query.relation_disease_name,
            query.relation_source_taxon, query.matched_microbe_ids, query.channels,
            query.age_group, query.query_language, query.microbe_id, query.disease_id,
            query.relation_id, query.relation_type, query.screen_profile,
            topic.query_background, topic.query_taxa)
        representative = identities.get(identity)
        if representative is None:
            identities[identity] = topic.topic_id
            representatives.append(topic)
            members[topic.topic_id] = [topic]
        else:
            members[representative].append(topic)
    return representatives, {key: tuple(group) for key, group in members.items()}


def _shared_management_query(topic, members):
    query = _query(topic, public=False)
    if topic.kind != 'risk_management' or len(members) < 2:
        return query
    return replace(query, supports=_unique(support for member in members
        for support in _query(member, public=False).supports))


def build_evaluation_evidence_plan(data, document, *, max_queries: int = 32,
                                   max_taxon_queries: int = 8, subject=None) -> EvaluationEvidencePlan:
    """Plan only questions supported by adopted, nonempty real observations.

    Every source disease remains a separate topic, including zero/low values.
    An explicit small budget defers queries, never deletes source rows/topics.
    Multiple consumers share a topic without claiming one paper supports each
    individual molecule or validates the supplier's numeric model.
    """
    if type(max_queries) is not int or max_queries < 0 or type(max_taxon_queries) is not int or max_taxon_queries < 0:
        raise ValueError('query budgets must be nonnegative integers')
    raw = _plain(data)
    raw = raw.get('data', raw)
    doc = _plain(document)
    facts = {fact['factId']: fact for item in doc.get('facts', []) if (fact := _plain(item)).get('factId')}
    subject = dict(subject or {})
    focus_ids = set()
    if not isinstance(data, dict) and not isinstance(document, dict):
        from .evaluation_clinical_focus import selected_source_clinical_focus
        focus_ids = {identifier for frame in selected_source_clinical_focus(data, document)
            for identifier in frame['problemIds']}
    topics = {}
    source_codes = {}

    def scan(value):
        if isinstance(value, dict):
            for source in value.get('sources', []):
                if source.get('recordId') in facts and source.get('sourceCode'):
                    source_codes.setdefault(source['recordId'], source['sourceCode'])
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
    scan(raw)

    for topic in _risk_candidate_topics(document, facts, source_codes):
        topics[topic.topic_id] = topic

    def add(kind, identity, title, ids, consumers, term, aliases, required, priority=2,
            *, source_support_ids=(), semantics=(), query_enabled=True):
        ids = _unique(fid for fid in ids if fid in facts)
        if not ids:
            return
        topic_id = _topic_id(kind, identity)
        roles = (('source_data', 'individual_synthesis') if kind == 'context' else
            ('source_data', 'population_association', 'individual_synthesis') if kind == 'lifestyle' else
            ('source_data', 'disease_meaning', 'population_association', 'diagnostic_principle', 'individual_synthesis')
            if kind in {'disease', 'clinical', 'association'} else ('source_data', 'general_function', 'population_association', 'individual_synthesis'))
        topic = EvidenceTopic(topic_id, kind, title, ids, _unique(facts[fid]['kind'] for fid in ids),
            _unique(facts[fid]['name'] for fid in ids), roles, (required,) if required else (), _unique(consumers),
            _source_bindings(ids, facts, source_codes), term, _unique([*aliases, title]), priority,
            tuple(source_support_ids), tuple(semantics), (), query_enabled)
        previous = topics.get(topic_id)
        if previous:
            merged_ids = _unique([*previous.fact_ids, *ids])
            topic = replace(previous, fact_ids=merged_ids,
                source_kinds=_unique([*previous.source_kinds, *topic.source_kinds]),
                source_objects=_unique([*previous.source_objects, *topic.source_objects]),
                consumer_paths=_unique([*previous.consumer_paths, *topic.consumer_paths]),
                source_support_ids=_unique([*previous.source_support_ids, *topic.source_support_ids]),
                source_semantics=_unique([*previous.source_semantics, *topic.source_semantics]),
                source_bindings=_source_bindings(merged_ids, facts, source_codes))
        topics[topic_id] = topic
        return topic_id

    # Source model diseases require medical meaning, not a paper confirming
    # that the person's source model score/level is mathematically valid.
    association_ids = {source.get('recordId') for row in raw.get('appendices', {}).get('sourceItems', [])
        if row.get('kind') == 'context' and row.get('category') == 'healthAssociation'
        for source in row.get('sources', [])}
    for ci, category in enumerate(raw.get('diseaseRiskAssessment', {}).get('categories', [])):
        for ri, row in enumerate(category.get('items', [])):
            association = ((str(row.get('code') or '').startswith('association:')
                and _present(row.get('sourceStatus'))) or (not has_risk_index(row)
                and bool(association_ids.intersection(source.get('recordId') for source in row.get('sources', [])))))
            ids = _row_ids(row, facts, measured=not association)
            name = row.get('name')
            if not ids or not isinstance(name, str) or not name.strip():
                continue
            # The disease's actual measured taxa share its existing question;
            # their source sign is not itself scientific or causal approval.
            taxon_ids = _unique(fid for taxon in row.get('relatedTaxa', [])
                for fid in _row_ids(taxon, facts) if facts[fid].get('kind') == 'microbe')
            aliases = _disease_retrieval_terms(name)
            add('disease', name, name, ids, [
                ('diseaseRiskAssessment', 'categories', ci, 'items', ri, 'explanation'),
                ('diseaseRiskAssessment', 'categories', ci, 'clinicalCorrelation', 'text'),
                ('healthOverview', 'overallSummary', 'text'),
                ('comprehensiveConclusion', 'overallConclusion', 'text')],
                aliases[0], aliases, 'disease_meaning', 1 if _LOW.search(str(row.get('sourceStatus') or '')) else 2,
                semantics=('source_association' if association else 'source_model',))
            topic_id = _topic_id('disease', name)
            topics[topic_id] = replace(topics[topic_id], query_taxa=_unique([
                *topics[topic_id].query_taxa,
                *(name.strip() for fid in taxon_ids for name in str(facts[fid].get('name') or '').split(' / ')
                    if re.fullmatch(r'[A-Z][A-Za-z_.-]*(?:\s+[a-z][a-z_.-]*){0,3}', name.strip()))]))

    for hi, row in enumerate(raw.get('microbiomeAssessment', {}).get('healthMetrics', [])):
        ids = tuple(fid for fid in _row_ids(row, facts, measured=False) if fid in association_ids)
        name = row.get('name')
        if not ids or not name or not _present(row.get('value')):
            continue
        consumer = ('microbiomeAssessment', 'healthMetrics', hi, 'explanation')
        previous = next((topic for topic in topics.values() if topic.kind == 'disease'
            and (set(ids).intersection(topic.fact_ids) or _key(topic.title) == _key(name))), None)
        if previous:
            merged_ids = _unique([*previous.fact_ids, *ids])
            topics[previous.topic_id] = replace(previous,
                fact_ids=merged_ids,
                source_kinds=_unique([*previous.source_kinds, *(facts[fid]['kind'] for fid in ids)]),
                source_objects=_unique([*previous.source_objects, *(facts[fid]['name'] for fid in ids)]),
                source_semantics=_unique([*previous.source_semantics, 'source_association']),
                source_bindings=_source_bindings(merged_ids, facts, source_codes),
                consumer_paths=_unique([*previous.consumer_paths, consumer]))
            continue
        aliases = _disease_retrieval_terms(name)
        add('association', name, name, ids, [consumer, ('microbiomeAssessment', 'structureInterpretation', 'text')],
            aliases[0], aliases, 'disease_meaning', 3, semantics=('source_association',))

    # The native health summary is a separate scientific duty from a disease
    # association panel. Its real member details and sources remain intact.
    for hi, row in enumerate(raw.get('microbiomeAssessment', {}).get('healthMetrics', [])):
        ids = _row_ids(row, facts, measured=False)
        code, name = row.get('code'), row.get('name')
        if (not ids or not name or not _present(row.get('value'))
                or str(code or '').startswith(('panelSummary:', 'panelResult:'))
                or set(ids) & association_ids):
            continue
        term, aliases = _HEALTH_TERMS.get(code, (name, (name,)))
        members=health_summary_member_rows(row,raw['microbiomeAssessment']['healthMetrics'],facts)
        ids=_unique([*ids,*(fid for member in members for fid in _row_ids(member,facts,measured=False))])
        genes,latin=_health_literal_terms(row,ids,facts)
        for member in members:
            member_genes,_=_health_literal_terms(member,_row_ids(member,facts,measured=False),facts)
            genes=_unique([*genes,*member_genes])
        if genes:
            term = genes[0] + ' gene function annotation genomic context'
        elif latin:
            term += ' '+latin[0]
        if not genes and code in {'beneficialBacteria', 'opportunisticBacteria'}:
            term = 'human gut ' + (latin[0] if latin else 'intestinal bacteria') + (
                ' physiological function metabolism intestinal epithelial immune interaction')
        if code == 'pathogenicParasites' and latin:
            # The native panel may include systemic as well as enteric
            # organisms. Focus the gut-health query on a supplied intestinal
            # organism, without replacing any source taxon or its result.
            intestinal = {'Cryptosporidium parvum', 'Cryptosporidium hominis',
                'Entamoeba histolytica', 'Giardia duodenalis', 'Giardia intestinalis', 'Giardia lamblia'}
            latin = _unique([*(name for name in latin if name in intestinal), *latin])
            term = latin[0] + ' human intestinal epithelial infection pathogenesis'
        elif code == 'pathogenicFungi' and latin:
            term = latin[0] + ' human intestinal colonization translocation infection'
        aliases=_unique([*genes,*latin,*aliases])
        add('function', 'health:' + str(code or name), name, ids,
            [('microbiomeAssessment', 'healthMetrics', hi, 'explanation')],
            term, aliases, 'general_function', 1 if _LOW.search(str(row.get('sourceStatus') or row.get('value') or '')) else 2,
            semantics=('health_summary',))
        if genes:
            key=_topic_id('function','health:'+str(code or name))
            topics[key]=replace(topics[key],query_concepts=(genes,
                ('gene', 'genes', 'protein', 'annotation', 'function', 'locus', '基因', '功能', '注释')),
                source_semantics=(*topics[key].source_semantics, 'literal_gene_annotation'))
        else:
            key=_topic_id('function','health:'+str(code or name))
            topics[key]=replace(topics[key],query_concepts=_health_function_concepts(latin,aliases))
            if code in _NONBACTERIAL_HEALTH_PANELS:
                topics[key]=replace(topics[key],query_concepts=(latin or aliases,
                    _unique([*_FUNCTION_OBJECTS, *latin, *aliases]), _PATHOGEN_PROCESSES))
            if latin and code in {'beneficialBacteria', 'opportunisticBacteria', 'pathogenicBacteria',
                    'keystoneBacteria', 'barrierRelatedBacteria'}:
                topics[key]=replace(topics[key],query_concepts=(latin,
                    _unique([*_FUNCTION_OBJECTS,*latin]),
                    _PATHOGEN_PROCESSES if code=='pathogenicBacteria' else _HEALTH_PROCESS_ALIASES))

    resistance = [(index, row, _row_ids(row, facts))
        for index, row in enumerate(raw.get('microbiomeAssessment', {}).get('antimicrobialResistance', []))]
    resistance = [(index, row, ids) for index, row, ids in resistance if ids]
    if resistance:
        key = add('function', 'antimicrobialResistance', '肠道抗生素耐药基因',
            _unique(fid for _, _, ids in resistance for fid in ids),
            [('microbiomeAssessment', 'antimicrobialResistance', index, 'interpretation')
                for index, _, _ in resistance],
            'metagenomic antimicrobial resistance genes clinical interpretation gut',
            ('antibiotic resistance', 'antimicrobial resistance', 'resistome', '耐药基因'),
            'general_function', 2, semantics=('measured_resistome',))
        if key:
            topics[key] = replace(topics[key], query_concepts=(topics[key].aliases, _GUT_RESISTOME_OBJECTS))

    # Ecological interpretation is bound to the actual measured dimension.
    # Richness and diversity are distinct objects, never inferred from a taxon.
    structure_terms = {
        'speciesCount': ('肠道菌群丰富度', 'human gut bacterial richness metabolic markers adiposity insulin resistance dyslipidaemia',
            ('bacterial richness', 'species richness', 'microbial richness', 'microbiota richness', '菌群丰富度', '肠道菌群种类')),
        'diversity': ('肠道菌群多样性', 'human gut microbiome alpha diversity metabolic health population cohort insulin resistance',
            ('microbiome diversity', 'alpha diversity', 'alpha-diversity', 'microbial diversity',
                'microbiota diversity', 'gut microbiome diversity', 'Shannon diversity', '菌群多样性')),
    }
    for row in raw.get('microbiomeAssessment', {}).get('overallStructure', []):
        if row.get('code') not in structure_terms or not (ids := _row_ids(row, facts)):
            continue
        title, term, aliases = structure_terms[row['code']]
        add('function', 'structure:' + row['code'], title, ids,
            [('microbiomeAssessment', 'structureInterpretation', 'text')], term, aliases,
            'general_function', 2, semantics=('measured_ecological_dimension',))
        key = _topic_id('function', 'structure:' + row['code'])
        topics[key] = replace(topics[key], query_concepts=(aliases, _GUT_RESISTOME_OBJECTS,
            ('metabolic health', 'metabolic markers', 'metabolic variables', 'insulin resistance',
                'HOMA-IR', 'HbA1c', 'dyslipidaemia', 'dyslipidemia', 'adiposity', '代谢健康', '胰岛素抵抗')))

    from .evaluation_synthesis import _function_personal_context, native_function_source_context
    reported_background = {}
    for row in raw.get('appendices', {}).get('sourceItems', []):
        if (row.get('kind'), row.get('category'), row.get('sourceType')) != (
                'context', 'sourceReportedBackground', 'microbiome'):
            continue
        for source in row.get('sources', []):
            fid = source.get('recordId')
            if fid in facts and facts[fid].get('kind') == 'context':
                reported_background[fid] = {'semanticType': 'sourceReportedBackground',
                    'sourceDescription': row.get('description'), 'factIds': [fid]}
    # The existing source-context helper verifies native direction against its
    # own file/date/panel. A low numeric score alone never supplies direction.
    native_context = None
    pathways = raw.get('functionalPathways', {}).get('pathways', [])
    for pi, pathway in enumerate(pathways):
        rows = [row for row in pathway.get('relatedMetrics', []) if _row_ids(row, facts)]
        ids = _unique(fid for row in rows for fid in _row_ids(row, facts))
        code = pathway.get('code')
        if not ids or code not in _PATHWAY_TERMS:
            continue
        term, aliases = _PATHWAY_TERMS[code]
        transmitter = _measured_transmitter_role(rows, facts) if code == 'P6' else None
        named_members = []
        if code in {'P1', 'P9'}:
            from modules.report.evaluation_reference_plan import measured_function_members
            named_members = measured_function_members(code,
                [{**row, 'factIds': _row_ids(row, facts)} for row in rows], facts)
        if transmitter:
            aliases, roles = transmitter
            term = aliases[0] + ' intestinal ' + ' '.join(roles[:3]) + ' role'
        if named_members:
            aliases = _unique(alias for member in named_members for alias in member['aliases'] if alias.isascii())
            roles = (('degradation', 'utilization', 'fermentation', 'catabolism', 'hydrolysis',
                *_DEGRADATION_DUTY_ALIASES, *_FERMENTATION_DUTY_ALIASES, 'utilize', 'utilizes', 'utilized')
                if code == 'P1' else ('inflammation', 'barrier', 'immune', 'permeability', 'endotoxin',
                    'inflammatory', 'immunity', 'receptor', 'signaling', '炎症', '屏障', '免疫'))
            term = aliases[0] + (' intestinal bacterial utilization degradation' if code == 'P1'
                else ' intestinal inflammation barrier physiological role')
        semantics = ('measured_function', 'measured_transmitter_role') if transmitter else ('measured_function',)
        if named_members:
            semantics = ('measured_function', 'measured_sugar_role' if code == 'P1' else 'measured_metabolite_role')
        add('function', code, pathway['name'], ids, [
            ('functionalPathways', 'pathways', pi, 'healthMeaning'), ('functionalPathways', 'summary', 'text'),
            ('nutritionAssessment', 'nutritionInterpretation', 'text'),
            ('healthOverview', 'summaries', 'nutrition', 'text'),
            ('comprehensiveConclusion', 'overallConclusion', 'text')],
            term, aliases, 'general_function', 1 if _LOW.search(str(pathway.get('assessmentLevel') or '')) else 2,
            semantics=semantics)
        if code == 'P3':
            scfa_ids = {fid for row in raw.get('microbiomeAssessment', {}).get('shortChainFattyAcids', [])
                for fid in _row_ids(row, facts)}
            if scfa_ids.intersection(ids):
                key = _topic_id('function', code)
                topics[key] = replace(topics[key], consumer_paths=_unique([
                    *topics[key].consumer_paths, ('microbiomeAssessment', 'scfaInterpretation', 'text')]))
        if transmitter or named_members:
            key = _topic_id('function', code)
            topics[key] = replace(topics[key], query_concepts=(aliases, _TRANSMITTER_GUT_OBJECTS, roles))
        clinical_ids, background = _function_personal_context(
            ('functionalPathways', 'pathways', pi, 'healthMeaning'), raw, facts, reported_background)
        background_terms, background_ids = _function_background_terms(
            code, clinical_ids | background.keys(), facts, background)
        key = _topic_id('function', code)
        query_members = ()
        if background_terms:
            if native_context is None:
                from .schemas import ReportSourceFact
                typed_facts = {}
                for fid, fact in facts.items():
                    try:
                        typed_facts[fid] = ReportSourceFact.model_validate(fact)
                    except ValueError:
                        continue
                native_context = native_function_source_context(raw, typed_facts)
            query_members = _function_query_members(pathway, facts, native_context)
        topics[key] = replace(topics[key], query_background=background_terms,
            query_measured_objects=query_members,
            aliases=_unique([*topics[key].aliases, *query_members]),
            context_fact_ids=background_ids)

    # Match nutrition consumers by their actual shared observation IDs, never
    # by a guessed phenotype, copied score, row count or invented group value.
    nutrition = raw.get('nutritionAssessment', {})
    for collection in ('detailedMetabolism', 'vitaminsAndMinerals'):
        for gi, group in enumerate(nutrition.get(collection, [])):
            ids = _unique(fid for row in group.get('items', []) for fid in _row_ids(row, facts))
            if not ids:
                continue
            consumer = ('nutritionAssessment', collection, gi, 'explanation', 'text')
            matched = [topic for topic in topics.values() if topic.kind == 'function' and set(ids) & set(topic.fact_ids)]
            if matched:
                for topic in matched:
                    topics[topic.topic_id] = replace(topic, consumer_paths=_unique([*topic.consumer_paths, consumer]))
            else:
                name = group.get('name') or group.get('code')
                if name:
                    add('nutrition', name, name, ids, [consumer, ('nutritionAssessment', 'nutritionInterpretation', 'text')],
                        name, (name,), 'general_function', semantics=('measured_function',))
    neuro_ids = {fid for row in nutrition.get('neurotransmitters', []) for fid in _row_ids(row, facts)}
    for topic in list(topics.values()):
        if topic.kind == 'function' and neuro_ids & set(topic.fact_ids):
            topics[topic.topic_id] = replace(topic, consumer_paths=_unique([*topic.consumer_paths,
                ('nutritionAssessment', 'neurotransmitterInterpretation', 'text')]))

    for problem in doc.get('problems', []):
        sources = [source for source in problem.get('sources', []) if source.get('factId') in facts
            and source.get('quote') and source['quote'] in str(facts[source['factId']].get('value', ''))]
        if not sources:
            continue
        ids = _unique(source['factId'] for source in sources)
        name = problem.get('title')
        if not name:
            continue
        terms = [term for term in problem.get('searchTerms', []) if isinstance(term, str) and term.strip()]
        english = next((term for term in terms if re.search(r'[A-Za-z]{3}', term) and not re.search(r'[\u4e00-\u9fff]', term)), None)
        problem_id = problem.get('problemId')
        strip_display = lambda text: re.sub(r'检查异常待复核|（待核实）|（用户自述）', '', str(text or '')).strip()
        from .evaluation_schema import clinical_problem_projection_codes
        canonical_code = (clinical_problem_projection_codes(data).get(problem_id)
            if hasattr(data, '_clinical_problem_projection') else None)
        finding_rows = [ri for ri, row in enumerate(raw.get('medicalAssessment', {}).get('findings', []))
            if set(_row_ids(row, facts, measured=False)) & set(ids)
            and row.get('code') == (canonical_code or problem_id)]
        consumers = [('medicalAssessment', 'findings', ri, 'explanation') for ri in finding_rows]
        consumers += [('healthOverview', 'summaries', 'medical', 'text'), ('comprehensiveConclusion', 'overallConclusion', 'text')]
        required = 'diagnostic_principle'
        clinical_key = add('clinical', problem_id or name, name, ids, consumers,
            english or (terms[0] if terms else strip_display(name)), [*terms, strip_display(name)], required, 0,
            source_support_ids=(problem_id,) if problem_id else (),
            semantics=('clinical_record',))
        if clinical_key:
            own_facts = [facts[fid] for fid in ids]
            purpose = ('imaging_finding_evaluation' if any(fact.get('imaging') for fact in own_facts)
                else 'reported_problem_evaluation' if all(fact.get('kind') == 'self_reported' for fact in own_facts)
                else 'laboratory_interpretation' if any(fact.get('referenceRange') or fact.get('unit') for fact in own_facts)
                else 'source_condition_evaluation')
            primary = problem_id in focus_ids or any(re.search(r'主检|主要结论|临床诊断|主诉', str(fact.get('name') or '') + str(fact.get('source') or '')) for fact in own_facts)
            topics[clinical_key] = replace(topics[clinical_key], clinical_frame={
                'problemId': problem_id, 'purpose': purpose, 'certainty': problem.get('certainty'),
                'subject': subject, 'sourcePriority': 0 if primary else 1 if purpose == 'reported_problem_evaluation' else 2,
                'factIds': ids})
        if finding_rows and clinical_key:
            clinical = topics[clinical_key]
            action_paths = tuple(('medicalAssessment', 'findings', ri, 'sourceAdvice') for ri in finding_rows)
            action_key = 'clinical-management:' + str(problem_id or clinical_key)
            topics[action_key] = replace(clinical, topic_id=action_key, kind='risk_management',
                allowed_roles=('source_data', 'management', 'individual_synthesis'),
                required_roles=('management',), consumer_paths=action_paths,
                primary_consumer_paths=action_paths, optional_consumer_paths=(),
                source_semantics=('clinical_record', 'current_clinical_action', 'current_management_problem'),
                query_concepts=(clinical.aliases, _RISK_MANAGEMENT_TERMS))

    reported_ids = []
    for qi, row in enumerate(raw.get('medicalAssessment', {}).get('questionnaireSummary', [])):
        if not _present(row.get('value')):
            continue
        ids = _row_ids(row, facts, measured=False)
        if not ids:
            continue
        reported_ids.extend(ids)
        name = row.get('name') or row.get('code')
        if name:
            add('context', name, name, ids, [
                ('medicalAssessment', 'questionnaireSummary', qi, 'explanation'),
                ('healthOverview', 'overallSummary', 'text'), ('healthOverview', 'summaries', 'medical', 'text'),
                ('comprehensiveConclusion', 'overallConclusion', 'text')], name, (name,), None,
                semantics=('reported_context',), query_enabled=False)

    symptoms = []
    symptom_ids = []
    reflux_ids=[]
    for fid, fact in facts.items():
        if fact.get('kind') != 'self_reported' or not str(fact.get('source') or '').startswith('用户自述：'):
            continue
        for index, (pattern, term, aliases) in enumerate(_TRIGGER_OUTCOMES):
            if re.search(pattern, str(fact.get('name') or '')):
                if index==3 and (re.search(r'(?:没有|否认|未出现|不伴|^无)[^。；;，,]{0,8}(?:反酸|烧心)',
                        str(fact.get('value')or''))or re.fullmatch(r'\s*(?:否|无|没有|不|false)\s*[。.]?',str(fact.get('value')or''),re.I)):
                    continue
                symptoms.append((index, term, aliases))
                symptom_ids.append(fid)
                if index==3:
                    reflux_ids.append(fid)
    symptoms = list(dict.fromkeys(symptoms))
    for ri, row in enumerate(raw.get('medicalAssessment', {}).get('triggerAnalysis', [])):
        observations = row.get('observations')
        if not isinstance(observations, str) or not observations.strip():
            continue
        # Raw question/value text must actually be present in the adopted row.
        # A row label or a loose source pointer alone cannot invent behaviour.
        ids = tuple(fid for fid in _row_ids(row, facts, measured=False)
            if facts[fid].get('kind') in {'self_reported', 'context', 'clinical'}
            and str(facts[fid]['value']) in observations)
        if not ids or not row.get('code') or not row.get('name'):
            continue
        question = _trigger_question(row['code'], [facts[fid] for fid in ids], symptoms)
        term, aliases, concepts = question if question else (row['name'], (row['name'],), ())
        add('lifestyle', row['code'], row['name'], ids, [
            ('medicalAssessment', 'triggerAnalysis', ri, field)
            for field in ('association', 'evidenceStrength', 'modifiability')],
            term, aliases, 'population_association' if question else None, 1,
            semantics=('family_history' if row['code'] == 'familyHistory' else 'reported_behaviour',),
            query_enabled=bool(question))
        key = _topic_id('lifestyle', row['code'])
        topics[key] = replace(topics[key], query_concepts=concepts,
            context_fact_ids=tuple(symptom_ids) if row['code'] != 'familyHistory' else ())
        stated_ages = [int(str(fact.get('value')).strip()) for fact in facts.values()
            if re.fullmatch(r'年龄|age', str(fact.get('name') or ''), re.I)
            and re.fullmatch(r'\d{1,3}', str(fact.get('value') or '').strip())]
        if (row['code'] == 'sleep' and concepts and concepts[0][0] == 'sleep duration'
                and len(set(stated_ages)) == 1 and 18 <= stated_ages[0] <= 60):
            topics[key] = replace(topics[key],
                query_term='recommended amount of sleep healthy adult joint consensus statement',
                source_semantics=(*topics[key].source_semantics, 'adult_sleep_duration_reference'))
        meal_ids=tuple(fid for fid in ids if _meal_timing_observation(facts[fid]['value']))
        reflux_question=(_trigger_question('diet',[facts[fid]for fid in meal_ids],symptoms,
            preferred_outcome=3,preferred_exposure='meal timing')
            if row['code']=='diet'and meal_ids and reflux_ids else None)
        if reflux_question and reflux_question!=question:
            term,aliases,concepts=reflux_question
            add('lifestyle','diet:mealTimingReflux','餐时与反酸症状',meal_ids,
                [('medicalAssessment','triggerAnalysis',ri,'association')],term,aliases,
                'population_association',1,semantics=('reported_behaviour','reported_reflux_symptom'))
            key=_topic_id('lifestyle','diet:mealTimingReflux')
            topics[key]=replace(topics[key],query_concepts=concepts,
                context_fact_ids=tuple(reflux_ids))
    context_ids = _unique([*reported_ids, *(fid for topic in topics.values() if topic.kind == 'clinical' for fid in topic.fact_ids)])
    for ti, row in enumerate(raw.get('microbiomeAssessment', {}).get('taxa', [])):
        ids = _row_ids(row, facts)
        if not ids or not any(facts[fid]['kind'] == 'microbe' for fid in ids):
            continue
        scientific = row.get('scientificName')
        if not scientific:
            scientific = next((name.strip() for value in [row.get('name'), *(facts[fid].get('name') for fid in ids)]
                for name in str(value or '').split(' / ')
                if re.fullmatch(r'[A-Z][A-Za-z0-9_.-]*(?:\s+[A-Za-z0-9_.-]+){0,3}', name.strip())), None)
        name = scientific or row.get('name')
        if not name:
            continue
        add('taxon', name, row.get('name') or name, ids, [
            ('microbiomeAssessment', 'taxa', ti, 'explanation'),
            ('microbiomeAssessment', 'keyMicrobialSignals', 'text'),
            ('microbiomeAssessment', 'structureInterpretation', 'text')], name,
            (name, row.get('name') or name), 'population_association',
            3 if row.get('sourceStatus') not in {None, '', '正常', '未检出'} else 4,
            semantics=('measured_taxon', 'taxon_category:' + str(row.get('category') or '')))
    for topic in list(topics.values()):
        if topic.kind not in {'context', 'lifestyle', 'risk', 'risk_management'}:
            if any(path[:2] == ('functionalPathways', 'pathways') for path in topic.consumer_paths):
                continue  # Function questions retain only their own admitted domain context.
            topics[topic.topic_id] = replace(topic, context_fact_ids=tuple(fid for fid in context_ids if fid not in topic.fact_ids))

    # Coverage-first allocation reserves core disease/function questions before
    # the optional taxon expansion; excess topics stay visible as deferred.
    topics = {key: _with_consumer_duties(topic) for key, topic in topics.items()}
    order = {'clinical': -4, 'risk_management': -1, 'risk': 0, 'disease': 1, 'function': 2, 'lifestyle': 3,
        'association': 4, 'nutrition': 5, 'taxon': 6, 'context': 7}
    ordered = tuple(sorted(topics.values(), key=lambda topic: (order[topic.kind], topic.priority, topic.topic_id)))
    clinical_groups = _clinical_query_groups(ordered)
    shared_clinical_actions = {member.topic_id for members in clinical_groups.values() for member in members[1:]}
    selected, taxon_count = [], 0
    core = [topic for topic in ordered if topic.kind in
        {'risk', 'risk_management', 'function', 'clinical', 'disease'}]
    # Actual reported behaviours have direct row consumers; reserve their
    # questions before optional taxon/nutrition expansion, within the old cap.
    lifestyle = [topic for topic in ordered if topic.kind == 'lifestyle' and topic.query_enabled]
    optional = [[topic for topic in ordered if topic.kind == kind and topic not in core]
        for kind in ('disease', 'association', 'nutrition', 'taxon')]
    optional_order = [group[index] for index in range(max((len(group) for group in optional), default=0))
        for group in optional if index < len(group)]
    # A taxon cap alone reserved nothing once source-model questions filled
    # the queue. Reserve actual abnormal organisms, coalesced by their literal
    # scientific identity above. Categories are supplied observations, not a
    # new risk inferred from an organism's name or abundance.
    disease_taxa = {name.casefold() for topic in ordered for name in topic.query_taxa}
    abnormal_taxa = sorted((topic for topic in ordered if topic.kind == 'taxon' and topic.priority == 3),
        key=lambda topic: ('taxon_category:pathogenic' not in topic.source_semantics,
            'taxon_category:opportunistic' not in topic.source_semantics,
            topic.query_term.casefold() not in disease_taxa, topic.topic_id))
    reserved = abnormal_taxa[:min(max_taxon_queries, max_queries)]
    # Share remaining slots 1:2:2 between clinical, disease and function
    # duties. An expanded clinical inventory must not consume microbiome risk
    # questions. Exhausted groups yield their unused slots to the others.
    core_kinds = ('risk', 'risk_management', 'clinical', 'disease', 'function') if any(topic.kind == 'risk' for topic in core) else ('clinical', 'disease', 'function')
    core_groups = [[topic for topic in core if topic.kind == kind] for kind in core_kinds]
    shared_management = {}
    if len(core_groups) == 5:
        # Cover one actual relation per risk before additional mechanisms of
        # the same risk consume this report's finite query allocation.
        core_groups[0].sort(key=lambda topic: ('additional_risk_relation' in topic.source_semantics,
            _risk_retrieval_priority(topic), topic.priority, topic.topic_id))
        # The fixed management share first serves actual current problems.
        # Keep the same slots and all other groups' existing fair allocation.
        core_groups[1].sort(key=lambda topic: (not _current_management_topic(topic),
            topic.priority, topic.query_term.casefold(), topic.topic_id))
        # Several internal hypotheses can ask exactly the same management
        # question. Count that actual search once before the finite fair
        # allocation, retaining every independent fact/consumer topic below.
        # Otherwise aliases of abdominal obesity can occupy all management
        # slots and exclude a different current problem such as dyslipidemia.
        core_groups[1], shared_management = _management_query_groups(core_groups[1])
    fair_core, positions = [], [0] * len(core_groups)
    while any(positions[index] < len(group) for index, group in enumerate(core_groups)):
        for index in ((0, 0, 1, 2, 3, 4) if len(core_groups) == 5 else (0, 1, 1, 2, 2)):
            if positions[index] < len(core_groups[index]):
                fair_core.append(core_groups[index][positions[index]])
                positions[index] += 1
    # These actual row duties otherwise starve behind an unbounded risk
    # inventory. Reserve two existing-budget candidates, never new calls.
    # A real stated sleep duration has its own reference question; no
    # bowel/GERD relation is inferred from a bedtime or an unknown duration.
    trigger_reserved = [topic for code in ('exercise', 'sleep') for topic in lifestyle
        if topic.topic_id == _topic_id('lifestyle', code) and topic.query_concepts
        and (code != 'sleep' or topic.query_concepts[0][0] == 'sleep duration')]
    required_functions = [topic for topic in core if topic.kind == 'function' and any(
        path[:2] == ('functionalPathways', 'pathways') or path[:2] in {
            ('microbiomeAssessment', 'structureInterpretation'),
            ('microbiomeAssessment', 'scfaInterpretation')} for path in topic.consumer_paths)]
    # Keep these existing measured duties in the same bounded allocation.
    # Otherwise a larger risk roster displaces both the new behaviour slots
    # and the required pathway/structure originals into optional recall.
    current_clinical = sorted((topic for topic in ordered if topic.kind == 'clinical'),
        key=lambda topic: (topic.clinical_frame.get('sourcePriority', 2), topic.priority, topic.topic_id))
    for topic in [*current_clinical, *required_functions, *trigger_reserved[:min(2, max_queries)],
            *reserved, *fair_core, *lifestyle, *optional_order]:
        if not topic.query_enabled or topic in selected or topic.topic_id in shared_clinical_actions:
            continue
        if len(selected) >= max_queries or topic.kind == 'taxon' and taxon_count >= max_taxon_queries:
            continue
        selected.append(topic)
        taxon_count += topic.kind == 'taxon'
    selected_ids = {member.topic_id for topic in selected
        for member in clinical_groups.get(topic.topic_id, shared_management.get(topic.topic_id, (topic,)))}
    queries = tuple(_shared_clinical_query(topic, clinical_groups[topic.topic_id]) if topic.topic_id in clinical_groups
        else _shared_management_query(topic, shared_management.get(topic.topic_id, (topic,))) for topic in selected)
    return EvaluationEvidencePlan(ordered, queries, tuple(topic.topic_id for topic in ordered
        if topic.query_enabled and topic.topic_id not in selected_ids), subject=subject, patient_facts=facts)


def refresh_evaluation_evidence_consumers(plan, data, document):
    """Rebind candidates to current rows after report/questionnaire projection.

    Native row coalescing changes array indices without changing the original
    observations. Rebuild consumer addresses from the current typed inputs;
    retain controlled retrieval synonyms only for the exact same fact scope.
    This is not a new search or a transfer of sentence approval.
    """
    current = build_evaluation_evidence_plan(data, document, max_queries=48, max_taxon_queries=8,
        subject=plan.subject if isinstance(plan, EvaluationEvidencePlan) else None)
    if not isinstance(plan, EvaluationEvidencePlan):
        return current
    previous = {topic.topic_id: topic for topic in plan.topics}
    retained = set()
    for topic in current.topics:
        old = previous.get(topic.topic_id)
        if old is not None and (old.fact_ids, old.source_bindings, old.context_fact_ids,
                old.query_concepts) == (topic.fact_ids, topic.source_bindings,
                topic.context_fact_ids, topic.query_concepts):
            retained.add(topic.topic_id)
            if topic.topic_id in plan._concept_aliases:
                current._concept_aliases[topic.topic_id] = deepcopy(plan._concept_aliases[topic.topic_id])
    current._alias_events.extend(deepcopy(event) for event in plan._alias_events
        if event.get('topicId') in retained)
    return current


def _bounded_health_members(members, limit=6):
    """Spread one query over the actual panel without creating more queries."""
    return tuple(members[round(i*(len(members)-1)/(limit-1))]for i in range(limit)) if len(members)>limit else tuple(members)


def _disease_topic_taxa(topic):
    """Literal measured taxa for this disease, never genus/species expansion."""
    return topic.query_taxa


def _disease_query_member(taxa, *, expansion=False):
    """Rotate only explicitly supplied members; never invent a genus alias."""
    species = [name for name in taxa if ' ' in name]
    if not expansion:
        return next(iter(species), taxa[0])
    genera = [name for name in taxa if ' ' not in name and not name.casefold().endswith('aceae')]
    return next(iter(genera), species[1] if len(species) > 1 else taxa[-1])


_RISK_MANAGEMENT_TERMS = ('guideline', 'recommendation', 'recommendations', 'consensus',
    'intervention', 'randomized', 'randomised', 'lifestyle', 'management',
    'evaluation', 'assessment', 'screening', 'monitoring', 'follow-up', 'referral',
    '指南', '建议', '推荐', '共识', '干预', '生活方式', '评估', '筛查', '复查', '监测', '随访', '转诊')


def _management_behavior_scope(topic):
    """Reuse existing exposure aliases for an actually bound behaviour."""
    for relation in topic.query_relations:
        if relation.get('kind') != 'behavior':
            continue
        terms = relation.get('exposureTerms', [])
        for options in _TRIGGER_EXPOSURES.values():
            for pattern, name, aliases in options:
                if any(re.search(pattern, term, re.I) or _mentions(term, aliases) for term in terms):
                    return name, aliases
    return None


def _query(topic: EvidenceTopic, *, public: bool, expansion: bool = False) -> EvidenceQuery:
    term = topic.query_term
    if topic.kind in {'risk', 'risk_management'}:
        management = topic.kind == 'risk_management'
        exposure = topic.query_measured_objects if management else topic.query_concepts[1]
        object_term = next((alias for alias in exposure if alias.isascii() and re.search('[A-Za-z]', alias)),
            exposure[0] if exposure else topic.title)
        if public:
            concepts = topic.query_concepts[:1] if management else topic.query_concepts
            groups = [_unique([*(alias for alias in group if alias.isascii()), *group])[:12 if management else 6]
                for group in concepts]
            query = ' AND '.join('(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in group) + ')'
                for group in groups)
            query += (' AND (lifestyle OR dietary OR exercise OR "physical activity" OR evaluation '
                'OR assessment OR screening OR monitoring OR "follow-up" OR referral) '
                'AND (guideline OR recommendation OR consensus OR randomized OR randomised OR "clinical trial")'
                if management else ' AND (association OR risk OR incidence OR cohort OR "case-control" OR "systematic review")')
            if management and topic.query_background:
                query = ' AND '.join('(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in group) + ')'
                    for group in groups)
                query += {'management_lifestyle_action':' AND ("whole foods" OR food OR diet OR "dietary pattern" OR "lifestyle modification")',
                    'management_activity_action':' AND (exercise OR "physical activity" OR training)'}.get(
                        topic.query_background[0],' AND (screening OR "follow-up" OR referral OR reassessment)')
                query += ' AND (recommendation OR guideline OR randomized OR randomised OR "systematic review")'
        else:
            query = (f'{term} ' if management else f'{object_term} {term} ') + (
                'management guideline' if management else
                'risk association systematic review' if expansion else 'risk association')
            if management:
                behavior = _management_behavior_scope(topic)
                if _current_clinical_action(topic):
                    query = f'{term} clinical evaluation management follow-up guideline'
                elif expansion:
                    query = f'{term} clinical evaluation follow-up guideline'
                elif behavior:
                    query = f'{term} {behavior[0]} lifestyle management guideline'
                elif _current_management_topic(topic):
                    query = f'{term} ordinary food dietary pattern lifestyle recommendations'
                if topic.query_background:
                    query=f'{term} '+({'management_lifestyle_action':'ordinary food dietary pattern lifestyle recommendations',
                        'management_activity_action':'physical activity recommendations'}.get(
                            topic.query_background[0],'clinical follow-up recommendations'))
            if not management and len(topic.query_concepts) > 2:
                roles = [next((alias for alias in group if alias.isascii()), group[0])
                    for group in topic.query_concepts[2:] if group]
                query = ' '.join([object_term, *roles, term, 'risk association'])
        relation_kind=topic.query_relations[0].get('kind') if topic.query_relations else None
        if not management and relation_kind in {'microbiome','function'}:
            if public:
                query+=' AND (human OR adults OR patients) AND (stool OR fecal OR faecal OR "gut microbiome")'
                query+=(' AND (abundance OR enrichment OR depletion OR metagenomic OR "case-control")'
                    if relation_kind=='microbiome' else ' AND ("microbial function" OR metagenomic OR pathway OR synthesis OR metabolism)')
            else:
                query=f'{object_term} {term} human fecal '+('microbial abundance case-control association'
                    if relation_kind=='microbiome' else 'microbial functional potential pathway association')
        required = topic.query_concepts
        relation = next(iter(topic.query_relations), {}) if not management else {}
        supports = _unique([topic.topic_id, *topic.source_support_ids,
            *([relation['relationId']] if relation.get('relationId') else [])])
        return EvidenceQuery(query=query, supports=supports,
            required_concepts=required, intent='health_management', target=topic.title,
            screen_profile='clinical_report', query_language='mixed' if re.search(r'[\u4e00-\u9fff]', query) else 'english')
    if topic.kind == 'lifestyle':
        suffix = ('consensus guideline' if 'adult_sleep_duration_reference' in topic.source_semantics
            else 'association review' if expansion else 'association')
        intent = 'health_management'
    elif topic.kind in {'disease', 'clinical', 'association'}:
        suffix = ('clinical evaluation guideline' if expansion else 'clinical evaluation') if topic.kind == 'clinical' else (
            'clinical features' if expansion else 'definition')
        intent = 'diagnostic_guideline' if topic.kind == 'clinical' else 'disease_clinical_significance'
    elif topic.kind == 'taxon':
        suffix = 'human intestinal community physiological function abundance'
        intent = 'gut_function'
    else:
        # The same real EviMed client returned 0/15 vitamin-topic abstracts
        # for the former generic tail, versus 13/15 for the concise seed.
        suffix = ''
        term = (('gut ' if term.startswith('microbial ') else 'microbial ') + term) if expansion else term
        intent = 'gut_function'
    query = f'{term} {suffix}'.strip()
    if topic.kind == 'clinical':
        frame = topic.clinical_frame
        purpose = {'imaging_finding_evaluation': 'imaging finding evaluation follow-up guideline',
            'reported_problem_evaluation': 'symptom clinical evaluation review',
            'laboratory_interpretation': 'laboratory result interpretation guideline',
            'source_condition_evaluation': 'clinical evaluation guideline'}
        age = frame.get('subject', {}).get('age')
        population = 'adult ' if type(age) in (int, float) and age >= 18 else 'pediatric ' if type(age) in (int, float) else ''
        query = population + term + ' ' + purpose.get(frame.get('purpose'), 'clinical evaluation guideline')
        if expansion:
            # Broaden to the source concept; never add pathology to every
            # symptom, lab result or imaging observation.
            query = population + term + ' ' + ('clinical assessment review' if not public else 'evaluation guideline')
        if public:
            aliases = [alias for alias in topic.aliases if alias.isascii()] or list(topic.aliases)
            query = '(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in aliases[:4]) + ')'
            query += ' AND (evaluation OR interpretation OR guideline OR review OR "follow-up")'
            if population:
                query += ' AND (adult OR adults)' if population == 'adult ' else ' AND (child OR children OR pediatric)'
        return EvidenceQuery(query=query, supports=(topic.topic_id,), required_concepts=(topic.aliases,),
            intent=intent, target=topic.title, screen_profile='clinical_report',
            query_language='mixed' if re.search(r'[\u4e00-\u9fff]', term) else 'english',
            age_group='adult' if population == 'adult ' else 'pediatric' if population else 'unknown')
    if topic.topic_id in {_topic_id('function','health:'+code)
            for code in ('gutImmunity','antiInflammatoryCapacity')} and not public:
        query+=' commensal mucosal homeostasis physiological regulation'
    disease_taxa = _disease_topic_taxa(topic) if topic.kind == 'disease' else ()
    if disease_taxa:
        # EviMed semantic recall for long OR strings returned mostly disease
        # definitions. Compact literal member + disease probes found the
        # actual human originals; population/site checks remain downstream.
        query = f'{_disease_query_member(disease_taxa, expansion=expansion)} {term}'
    beneficial = (_health_function_topic(topic)
        and topic.topic_id == _topic_id('function', 'health:beneficialBacteria')
        and all(re.fullmatch(r'[A-Z][a-z]+(?:\s+[a-z][a-z.-]*){0,3}',name)for name in topic.query_concepts[0]))
    pathogenic = (_health_function_topic(topic)
        and topic.topic_id == _topic_id('function','health:pathogenicBacteria')
        and all(re.fullmatch(r'[A-Z][a-z]+(?:\s+[a-z][a-z.-]*){0,3}',name)for name in topic.query_concepts[0]))
    nonbacterial = (topic.topic_id in {_topic_id('function', 'health:' + code)
        for code in _NONBACTERIAL_HEALTH_PANELS} and _health_function_topic(topic))
    keystone = topic.topic_id == _topic_id('function', 'health:keystoneBacteria')
    barrier = topic.topic_id == _topic_id('function', 'health:barrierRelatedBacteria')
    if beneficial and not public:
        members=_bounded_health_members(topic.query_concepts[0])
        query='('+' OR '.join('"'+member.replace('"','')+'"'for member in members)+') '
        query+='human gut commensal physiological function metabolism fermentation'
    if pathogenic and not public:
        query=' OR '.join(_bounded_health_members(topic.query_concepts[0]))+' intestinal invasion colonization infection mechanism'
    if public:
        query = '(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in topic.aliases[:4]) + ')'
        literal_genes = (topic.query_concepts[0] if 'health_summary' in topic.source_semantics
            and len(topic.query_concepts) == 2 else ())
        if 'adult_sleep_duration_reference' in topic.source_semantics:
            # PubMed's phrase index does not contain the complete title even
            # though the consensus article is indexed. Match its literal
            # title terms independently rather than an unindexed phrase.
            query = '25979105[PMID] AND adult[Title]'
        elif disease_taxa:
            query += ' AND (' + ' OR '.join('"' + name + '"'
                for name in _bounded_health_members(disease_taxa, limit=3)) + ')'
            query += ' AND (gut OR intestinal OR stool) AND (human OR review OR cohort)'
        elif any(marker in topic.source_semantics for marker in (
                'measured_transmitter_role', 'measured_sugar_role', 'measured_metabolite_role')):
            query = '(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in topic.query_concepts[0]) + ')'
            query += ' AND (intestinal OR colonic OR gut) AND (' + ' OR '.join(
                '"' + term + '"' if ' ' in term else term for term in topic.query_concepts[2][:5]
                if term.isascii()) + ')'
        elif 'measured_ecological_dimension' in topic.source_semantics:
            dimension = ('richness' if topic.topic_id == _topic_id('function', 'structure:speciesCount')
                else 'diversity')
            query = dimension + '[Title]'
            query += (' AND (gut OR intestinal OR fecal OR faecal) AND (human OR adult) '
                'AND (cohort OR population) '
                'AND ("metabolic health" OR "metabolic markers" OR "insulin resistance" OR '
                'HOMA-IR OR HbA1c OR dyslipidaemia OR dyslipidemia OR adiposity)')
        elif expansion and topic.topic_id == _topic_id('function', 'health:scfaProducerBacteria'):
            query = ('("short-chain fatty acid-producing bacteria" OR "SCFA-producing bacteria") '
                'AND (gut OR intestinal) AND (physiology OR fermentation OR metabolism OR production)')
        elif _health_function_topic(topic):
            member_duty = expansion and topic.query_term.endswith('intestinal epithelial immune interaction')
            objects = _bounded_health_members(topic.query_concepts[0]) if beneficial else topic.query_concepts[0][:6]
            if member_duty:
                # One bounded query covers several actual members across the
                # panel instead of always narrowing to the first probiotic.
                members=topic.query_concepts[0]
                objects=_bounded_health_members(members)
            query = '(' + ' OR '.join('"' + alias.replace('"', '') + '"'
                + ('[Title]' if member_duty else '') for alias in objects) + ')'
            query += ' AND (gut OR intestinal OR microbiota OR microbiome)'
            query += ' AND (function OR metabolism OR fermentation OR synthesis OR production OR regulation OR barrier OR immunomodulation)'
            if topic.query_term.endswith('intestinal epithelial immune interaction'):
                query += ' AND (mechanism OR metabolism OR epithelial OR "tight junction" OR immune OR immunomodulation OR "cross-feeding")'
            if member_duty and topic.topic_id == _topic_id('function', 'health:beneficialBacteria'):
                query = '('+' OR '.join('"'+member.replace('"','')+'"[Title]'for member in objects)+') '
                query += ('AND (gut OR intestinal OR colonic) AND (human OR commensal) '
                    'AND ("cross-feeding" OR fermentation OR "butyrate production" '
                    'OR "colonization resistance" OR "carbohydrate metabolism") '
                    'NOT (ameliorat*[Title] OR induced[Title] OR autism[Title] OR conjunctival[Title])')
            elif member_duty and topic.topic_id == _topic_id('function', 'health:opportunisticBacteria'):
                query = '(' + '"' + objects[0].replace('"', '') + '"[Title]) '
                query += 'AND (opportunistic OR extraintestinal) AND (growth OR survival OR infection OR colonization)'
            elif pathogenic:
                query='('+' OR '.join('"'+member.replace('"','')+'"'for member in _bounded_health_members(topic.query_concepts[0]))+') '
                query+=('AND (intestinal OR gut OR enteric) '
                    'AND (invasion OR colonization OR colonisation OR infection OR pathogenesis OR virulence)')
            elif nonbacterial:
                objects = _bounded_health_members(topic.query_concepts[0])
                query = '('+' OR '.join('"'+member.replace('"','')+'"'for member in objects)+') '
                query += ('AND (intestinal OR gut OR enteric OR gastrointestinal) '
                    'AND (infection OR pathogenesis OR invasion OR colonization OR virulence OR epithelial)')
                if topic.topic_id == _topic_id('function', 'health:pathogenicParasites'):
                    query = '"' + topic.query_concepts[0][0].replace('"', '') + '" '
                    query += 'AND (human OR intestinal OR epithelial) AND (barrier OR diarrhea OR pathogenesis OR infection)'
                elif topic.topic_id == _topic_id('function', 'health:pathogenicFungi'):
                    # Ask for the supplied member's own intestinal invasion/
                    # translocation process; colonization-reduction trials of
                    # other administered organisms do not discharge this duty.
                    query = '"' + topic.query_concepts[0][0].replace('"', '') + '" '
                    query += 'AND (intestinal OR gut) AND (translocation OR invasion OR pathogenesis)'
            elif keystone or barrier:
                query = '('+' OR '.join('"'+member.replace('"','')+'"'
                    for member in _bounded_health_members(topic.query_concepts[0]))+') '
                query += 'AND (gut OR intestinal OR colonic) AND '
                query += ('(fermentation OR consumption OR utilization OR "cross-feeding" OR butyrate OR metabolism)'
                    if keystone else '("epithelial barrier" OR "intestinal barrier" OR permeability OR invasion)')
        elif literal_genes and all(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', gene) for gene in literal_genes):
            # Gene mechanism papers are often indexed by the literal gene
            # rather than "microbiome". Do not translate gtrB into stx or
            # infer an organism or a toxin from the supplier's panel title.
            query = '(' + ' OR '.join('"' + gene + '"' for gene in literal_genes) + ')'
            query += ' AND (gene OR protein OR function OR annotation OR locus)'
        elif topic.kind=='taxon':
            query+=' AND (human OR commensal) AND (gut OR intestinal OR stool) '
            query+='AND (abundance OR physiology OR fermentation OR metabolism OR "colonization resistance")'
        else:
            query += ' AND (review OR guideline OR human)' if topic.kind in {'disease', 'clinical', 'association', 'lifestyle'} else ' AND (microbiome OR microbiota)'
    required = ((topic.aliases, disease_taxa) if disease_taxa else topic.query_concepts
        or ((topic.aliases,) if topic.kind in {'disease', 'clinical', 'association', 'taxon'} else (topic.aliases, _FUNCTION_OBJECTS)))
    if topic.kind == 'function' and topic.query_background:
        # This remains the same function topic/role. The additional group asks
        # for an actual source-present bridge; it does not approve that bridge
        # or let a broad bile-acid paper stand in for measured steroid content.
        members = tuple(alias for alias in topic.query_measured_objects
            if alias.isascii() and re.search(r'[A-Za-z]', alias))
        if public:
            if members:
                query = '(' + ' OR '.join('"' + alias.replace('"', '') + '"' for alias in members[:4]) + ')'
                query += ' AND (gut OR intestinal OR microbial OR microbiota) AND (metabolism OR function OR synthesis OR degradation)'
            query += ' AND (' + ' OR '.join('"' + alias + '"' for alias in topic.query_background) + ')'
        else:
            if members:
                query = members[0] + ' intestinal microbial metabolism'
            query += ' (' + ' OR '.join(topic.query_background) + ')'
        required = (*required, *((topic.query_measured_objects,) if members else ()), topic.query_background)
    return EvidenceQuery(query=query, supports=(topic.topic_id,), required_concepts=required,
        intent=intent, target=topic.title,
        screen_profile=('evaluate_ecological_association' if 'measured_ecological_dimension' in topic.source_semantics
            else 'evaluate_general_function' if topic.kind == 'function' else 'clinical_report'),
        query_language='mixed' if re.search(r'[\u4e00-\u9fff]', term) else 'english')


def evaluation_plan_queries(plan: EvaluationEvidencePlan, *, public: bool = False,
        uncovered_topic_ids=None, max_queries: int | None = None) -> list[EvidenceQuery]:
    """Primary queries, or a bounded recall pass for explicitly uncovered topics."""
    if max_queries is not None and (type(max_queries) is not int or max_queries < 0):
        raise ValueError('max_queries must be a nonnegative integer')
    if uncovered_topic_ids is None:
        ids = {support for query in plan.queries for support in query.supports}
        topics = [topic for topic in plan.topics if topic.topic_id in ids and topic.query_enabled]
        expansion = False
    else:
        ids = set(uncovered_topic_ids)
        topics = [topic for topic in plan.topics if topic.topic_id in ids and topic.query_enabled]
        expansion = True
    clinical_groups = _clinical_query_groups(topics)
    paired_actions = {member.topic_id for members in clinical_groups.values() for member in members[1:]}
    queries = [_shared_clinical_query(topic, clinical_groups[topic.topic_id], public=public, expansion=expansion)
        if topic.topic_id in clinical_groups else _query(topic, public=public, expansion=expansion)
        for topic in topics if topic.topic_id not in paired_actions]
    limit = max_queries if max_queries is not None else len(plan.queries)
    # Several independently assessed risks may share one exact search
    # relation. Retrieve it once and retain every candidate binding; patient
    # applicability is still reviewed independently per risk afterwards.
    risk_ids = {topic.topic_id for topic in topics if topic.kind in {'risk', 'risk_management'}}
    merged, positions = [], {}
    for query in queries:
        if not risk_ids.intersection(query.supports):
            merged.append(query)
            continue
        stamp = (query.query, query.required_concepts, query.intent, query.screen_profile, query.channels)
        if stamp in positions:
            index = positions[stamp]
            merged[index] = replace(merged[index], supports=_unique([*merged[index].supports, *query.supports]))
        else:
            positions[stamp] = len(merged)
            merged.append(query)
    return merged[:limit]


def truncated_original_queries(plan, records, *, max_queries=2):
    """Exact publication lookups inside the existing bounded public recall.

    No EviMed parameter is invented and no excerpt is completed by a model.
    The existing public reader must verify identity/edition before retention.
    """
    if type(max_queries) is not int or max_queries < 0:
        raise ValueError('max_queries must be a nonnegative integer')
    if not isinstance(plan, EvaluationEvidencePlan) or not max_queries:
        return []
    topics = {topic.topic_id for topic in plan.topics}
    result, seen = [], set()
    for original in records:
        row = _plain(original)
        text = evaluation_original_text(row)
        if len(text) not in {1000, 4000, 6000, 12000} or re.search(r'[.!?。！？][\s\"\')\]]*$', text):
            continue
        supports = _unique(support for support in row.get('supports', []) if support in topics)
        if not supports:
            continue
        doi, pmid = evaluation_publication_identifiers(row)
        title = ' '.join(str(row.get('title') or '').replace('"', '').split())
        query = (str(pmid) + '[PMID]' if pmid else '"' + doi + '"[AID]' if doi
            else '"' + title + '"[Title]' if title else '')
        if not query or query in seen:
            continue
        seen.add(query)
        result.append(EvidenceQuery(query=query, supports=supports, required_concepts=(),
            target=title, intent='diagnostic_guideline', screen_profile='clinical_report', query_language='english'))
        if len(result) >= max_queries:
            break
    return result


# A functional score's duty is not interchangeable with a generic health or
# disease association. These recall domains do not create observed pathways,
# approve synthesis/degradation claims, or validate the supplier's score.
_BIOSYNTHESIS_DUTY_ALIASES = ('biosynthesis', 'biosynthetic', 'synthesis', 'synthesize', 'synthesise',
    'synthesized', 'synthesised', 'synthesizing', 'synthesising', 'production',
    'produce', 'produces', 'produced', 'producing', '合成', '产生')
_DEGRADATION_DUTY_ALIASES = ('degradation', 'degrade', 'degrades', 'degraded', 'degrading',
    'biodegradation', 'biodegrade', 'biodegrading', 'breakdown', 'break down', 'breaking down',
    'catabolism', 'catabolic', '降解', '分解')
_FERMENTATION_DUTY_ALIASES = ('fermentation', 'fermentative', 'ferment', 'fermented', 'fermenting', '发酵')
_FUNCTION_RECALL_DOMAINS = {
    'P1': ('human gut bacterial carbohydrate fermentation',
        (*_DEGRADATION_DUTY_ALIASES, *_FERMENTATION_DUTY_ALIASES)),
    'P2': ('bile acid metabolism',
        (*_BIOSYNTHESIS_DUTY_ALIASES, *_DEGRADATION_DUTY_ALIASES, 'metabolism', 'metabolic', '代谢',
            'deconjugation', 'deconjugate', 'biotransformation', 'transformation', '转化', '去结合')),
    'P3': ('short-chain fatty acid production',
        (*_BIOSYNTHESIS_DUTY_ALIASES, *_FERMENTATION_DUTY_ALIASES)),
    'P4': ('human gut microbial amino acid biosynthesis', _BIOSYNTHESIS_DUTY_ALIASES),
    'P5': ('microbial vitamin biosynthesis', _BIOSYNTHESIS_DUTY_ALIASES),
    'P6': ('microbial neurotransmitter biosynthesis', _BIOSYNTHESIS_DUTY_ALIASES),
    'P7': ('human gut microbial purine pyrimidine metabolism',
        (*_BIOSYNTHESIS_DUTY_ALIASES,'metabolism','metabolic','代谢')),
    'P8': ('human gut microbial flavonoid carotenoid metabolism',
        (*_BIOSYNTHESIS_DUTY_ALIASES,*_DEGRADATION_DUTY_ALIASES,'metabolism','biotransformation','代谢','转化')),
    'P9': ('microbial toxins',
        ('virulence', 'toxin', 'toxins', '毒力', '毒素', '致病')),
    'P10': ('xenobiotic biodegradation',
        (*_DEGRADATION_DUTY_ALIASES, 'detoxification', 'detoxify', 'detoxifies', 'detoxifying', '解毒',
            'biotransformation', 'biotransform', 'biotransforms', 'transformation', 'transform', 'transforms', '转化')),
    # Explain what a gut resistome represents before seeking clinical drug
    # susceptibility. Requiring a phenotype here discarded general human-gut
    # reservoir studies and recalled unrelated infection-site antibiograms.
    'antimicrobialResistance': ('human gut resistome antibiotic resistance reservoir',
        ('reservoir', 'resistome', 'resistance gene', '耐药组', '储存库')),
}


def evaluation_recall_queries(plan: EvaluationEvidencePlan, *, uncovered_topic_ids=(),
        max_queries: int = 16, public: bool = False) -> list[EvidenceQuery]:
    """One bounded pass: missing core questions, then measured function duties.

    A broad primary hit does not finish a function's scientific explanation.
    Each source-present function may receive one distinct duty-focused query
    within the same recall budget; candidates still need original reviews.
    """
    if type(max_queries) is not int or max_queries < 0:
        raise ValueError('max_queries must be a nonnegative integer')
    pending = set(uncovered_topic_ids)
    pending.update(topic.topic_id for topic in plan.topics if topic.topic_id in plan.deferred_topic_ids
        and topic.kind in {'risk', 'risk_management', 'clinical', 'function', 'disease'})
    missing = [topic for topic in plan.topics if topic.topic_id in pending and topic.query_enabled]
    domains = {_topic_id('function', code): value for code, value in _FUNCTION_RECALL_DOMAINS.items()}
    extra = [topic for topic in plan.topics if topic.kind == 'function' and topic.fact_ids
        and topic.query_enabled and topic.topic_id not in pending and (topic.topic_id in domains
            or _health_function_topic(topic)
            or 'health_summary' in topic.source_semantics and _gene_panel_search_taxa(topic.title)
                and topic.query_concepts)]
    extra.extend(topic for topic in plan.topics if topic.kind == 'lifestyle'
        and 'adult_sleep_duration_reference' in topic.source_semantics
        and topic.query_enabled and topic.fact_ids and topic.topic_id not in pending)
    core=[topic for topic in missing if topic.kind in
        {'risk','risk_management','function','clinical','disease'}]
    optional=[topic for topic in missing if topic not in core]
    reserved = [topic for topic in [*core, *extra]
        if 'health_summary' in topic.source_semantics and len(topic.query_concepts) == 2
        and _gene_panel_search_taxa(topic.title)]
    # Additional health duties must not displace the existing literal-gene
    # questions. Reserve those same queries, then fill the remaining budget.
    # A smaller budget that cannot hold the whole group keeps core-first order.
    if reserved and len(reserved) <= max_queries:
        reserved_ids = {topic.topic_id for topic in reserved}
        ordered = [*reserved, *(topic for topic in [*core, *extra, *optional]
            if topic.topic_id not in reserved_ids)]
    else:
        ordered = [*core, *extra, *optional]
    if public and max_queries >= 32:
        # If the primary service is unavailable the public pass is the only
        # recall opportunity. Keep one topic per clinical/health/disease duty
        # cycling before optional expansions can exhaust the bounded pass.
        # A generic primary hit does not discharge a native health meaning.
        # Reserve one public duty for every actual source-present health row,
        # including explicit negative panels. Otherwise late fungus/barrier
        # duties are displaced by the many disease aliases and pathway extras.
        health = [topic for topic in ordered if 'health_summary' in topic.source_semantics]
        health_ids = {topic.topic_id for topic in health}
        groups = [[topic for topic in ordered if topic.kind == kind and topic.topic_id not in health_ids]
            for kind in ('risk', 'risk_management', 'clinical', 'function', 'disease', 'association', 'lifestyle', 'nutrition', 'taxon')]
        ordered = [*health, *(group[index] for index in range(max((len(group) for group in groups), default=0))
            for group in groups if index < len(group))]
    failed_taxa = [topic for topic in missing if topic.kind == 'taxon' and topic.priority == 3]
    failed_taxon_ids = {topic.topic_id for topic in failed_taxa}
    ordered = [*failed_taxa, *(topic for topic in ordered if topic.topic_id not in failed_taxon_ids)]
    clinical_duties = [topic for topic in ordered if topic.kind == 'clinical' or _current_clinical_action(topic)]
    ordered = [*clinical_duties, *(topic for topic in ordered if topic not in clinical_duties)]
    clinical_groups = _clinical_query_groups(ordered)
    paired_actions = {member.topic_id for members in clinical_groups.values() for member in members[1:]}
    queries = []
    for topic in ordered:
        if len(queries) >= max_queries:
            break
        if topic.topic_id in paired_actions:
            continue
        if topic.topic_id in clinical_groups:
            queries.append(_shared_clinical_query(topic, clinical_groups[topic.topic_id], public=public, expansion=True))
            continue
        if topic.kind == 'function' and topic.query_background:
            # Primary retrieval already asks for the personal clinical bridge.
            # The independent recall duty is the measured function itself:
            # adding the patient's symptoms again suppressed basic physiology
            # originals and returned only narrow disease/treatment cohorts.
            # Keep the real object, consumer and fact scope; this query does
            # not establish that its general physiology happened in the user.
            topic = replace(topic, query_background=(), query_measured_objects=())
        if any(marker in topic.source_semantics for marker in (
                'measured_transmitter_role', 'measured_sugar_role', 'measured_metabolite_role')):
            queries.append(_query(topic, public=public, expansion=True))
            continue
        domain = domains.get(topic.topic_id) if topic.kind == 'function' else None
        if domain is None:
            queries.append(_query(topic, public=public, expansion=True))
            continue
        terms, duties = domain
        amr = topic.topic_id == _topic_id('function', 'antimicrobialResistance')
        catabolism = topic.topic_id == _topic_id('function', 'P10')
        bile = topic.topic_id == _topic_id('function', 'P2')
        if public:
            # PubMed uses Boolean topic aliases rather than EviMed's semantic
            # prose. The public adapter applies its human-study screen and
            # retrieves the original NCBI abstract/PMC context.
            terms = (
                '("gut microbiome" OR "gut microbiota" OR microflora OR "gut resistome") '
                'AND ("antibiotic resistance" OR resistome) '
                'AND (reservoir OR "healthy individuals" OR "healthy adults")'
                if amr else
                '("gut microbiota" OR "gut microbiome" OR "intestinal microbiota" OR "gut microbes") '
                'AND (xenobiotic OR toxin OR pollutant) '
                'AND (degradation OR biodegradation OR biotransformation OR transformation OR metabolism)'
                if catabolism else
                '("bile acid"[Title] OR "bile acids"[Title]) AND (microbial OR gut OR intestinal) '
                'AND (metabolism OR transformation OR deconjugation OR "bile salt hydrolase" OR "secondary bile acids")'
                if bile else _query(topic, public=True, expansion=True).query)
        queries.append(EvidenceQuery(query=terms,
            supports=(topic.topic_id,), required_concepts=(topic.aliases,
                _GUT_RESISTOME_OBJECTS if amr or catabolism else _FUNCTION_OBJECTS, duties),
            intent='gut_function', target=topic.title, screen_profile='evaluate_general_function', query_language='english'))
    return queries


def prioritize_evaluation_recalls(plan, adaptive, extras, previous_queries, *, max_queries, round_index=0):
    """Share one recall budget between actual chapter duties and risk gaps.

    One function explanation per supplied pathway is a required consumer,
    even when a low-quality primary hit exists. Reserve bounded slots for
    those duties and a reported behaviour before adaptive disease expansion;
    retain at least four slots for risk/clinical queries in a full round.
    No new query, source binding, approval or cross-run cache is created.
    """
    if type(max_queries) is not int or max_queries < 0:
        raise ValueError('max_queries must be a nonnegative integer')
    if not max_queries:
        return []
    known = {' '.join(query.query.casefold().split()) for query in previous_queries}
    available = []
    for query in extras:
        key = ' '.join(query.query.casefold().split())
        if key not in known:
            available.append(query)
    main_functions = [topic for topic in plan.topics if topic.kind == 'function' and topic.fact_ids
        and any(path[:2] == ('functionalPathways', 'pathways') for path in topic.consumer_paths)]
    main_functions.sort(key=lambda topic: next(path[2] for path in topic.consumer_paths
        if path[:2] == ('functionalPathways', 'pathways')))
    reserved = []
    for topic in main_functions:
        match = next((query for query in available if topic.topic_id in query.supports), None)
        if match is not None:
            reserved.append(match)
    behaviour_order = ('exercise', 'sleep', 'diet')
    offset = round_index % len(behaviour_order)
    for code in (*behaviour_order[offset:], *behaviour_order[:offset]):
        match = next((query for query in available
            if _topic_id('lifestyle', code) in query.supports), None)
        if match is not None:
            reserved.append(match)
            break
    reservation = max(0, max_queries - min(4, max_queries // 2))
    # The measured ecological topics also need a real public-original pass.
    # Primary EviMed execution does not discharge that duty. Only canonical
    # PubMed title queries get these two slots; semantic/EviMed recalls keep
    # their existing order. Keep all existing pathway/behaviour reservations,
    # trading at most two adaptive slots inside the unchanged round cap.
    ecology = [topic for topic in plan.topics if topic.kind == 'function' and topic.fact_ids
        and 'measured_ecological_dimension' in topic.source_semantics]
    ecological_public = [query for topic in ecology
        if (query := next((query for query in available if topic.topic_id in query.supports
            and re.search(r'\[Title\]', query.query, re.I)), None)) is not None][:2]
    if ecological_public:
        reserved = [*ecological_public, *reserved]
        reservation = min(max_queries - min(2, max_queries // 2), reservation + len(ecological_public))
    result, round_risks = [], set()
    risk_ids = {topic.topic_id for topic in plan.topics if topic.kind in {'risk', 'risk_management'}}
    clinical_ids = {topic.topic_id for topic in plan.topics
        if topic.kind == 'clinical' or _current_clinical_action(topic)}
    clinical = [query for query in [*adaptive, *extras] if clinical_ids.intersection(query.supports)]
    attempts = {identifier: sum(identifier in previous.supports for previous in previous_queries) for identifier in clinical_ids}
    clinical_topics = {topic.topic_id: topic for topic in plan.topics if topic.topic_id in clinical_ids}
    clinical.sort(key=lambda query: (min((attempts[identifier] for identifier in query.supports if identifier in attempts), default=0),
        min((clinical_topics[identifier].clinical_frame.get('sourcePriority', 2) for identifier in query.supports if identifier in clinical_topics), default=2)))
    for query in [*clinical, *reserved[:reservation], *adaptive, *extras]:
        key = ' '.join(query.query.casefold().split())
        own_risks = risk_ids.intersection(query.supports)
        if key in known or own_risks.intersection(round_risks):
            continue
        known.add(key)
        round_risks.update(own_risks)
        result.append(query)
        if len(result) >= max_queries:
            break
    return result


def _risk_gap_search_topic(topic, previous_queries, *, action_kind=None):
    """Probe one real object at a time, rather than a whole personal history.

    Legacy relations can contain several independent exposures. Their aliases
    form an OR scope, not an instruction to find a paper with every exposure.
    Rotate through the supplied objects inside the existing recall budget.
    The frozen plan and its factual/relation bindings remain unchanged.
    """
    if topic.kind == 'risk_management' and _current_management_topic(topic):
        history = [query.query for query in previous_queries
            if isinstance(query, EvidenceQuery) and topic.topic_id in query.supports
            and re.search(r'intervention|recommend',query.query,re.I)]
        options=(('nutrition','management_lifestyle_action'),('activity','management_activity_action'),
            ('clinical','management_clinical_action'))
        if action_kind=='lifestyle':
            options=options[:2]
        duty=min(enumerate(options),key=lambda item:(sum(item[1][0] in _management_query_dimensions(
            query,topic.aliases) for query in history),item[0]))[1][1]
        return replace(topic, query_background=(duty,))
    if topic.kind != 'risk' or len(topic.query_concepts) < 2:
        return topic
    objects = topic.query_concepts[1]
    preferred = [term for term in objects if term.isascii() and re.search('[A-Za-z]', term)] or list(objects)
    if not preferred:
        return topic
    history = [query.query for query in previous_queries
        if isinstance(query, EvidenceQuery) and topic.topic_id in query.supports]
    selected = min(enumerate(preferred), key=lambda item: (
        sum(_mentions(query, (item[1],)) for query in history), item[0]))[1]
    aliases = _unique([selected, *(alias for group in _RISK_OBJECT_ALIASES
        if _mentions(selected, group) for alias in group if alias in objects)])
    relations = tuple({**relation, 'exposureTerms': list(aliases),
        'question': f'{selected} and {topic.query_term}: population association'}
        for relation in topic.query_relations[:1])
    return replace(topic, query_concepts=(topic.query_concepts[0], aliases, *topic.query_concepts[2:]),
        query_relations=relations)


def _risk_rewrite_adds_exposure(query, topic, original):
    """Do not let an expansion turn independent observations into AND gates."""
    if topic.kind != 'risk' or len(original.query_concepts) < 2:
        return False
    chosen = topic.query_concepts[1]
    # Unselected aliases may be alternate names for this same object. Exact
    # containment and the existing bilingual groups preserve those names;
    # other observed objects belong in another bounded probe, not this one.
    for term in original.query_concepts[1]:
        if (term in chosen or _mentions(term, chosen) or _mentions(' '.join(chosen), (term,))
                or _mentions(term, original.aliases) or _mentions(' '.join(original.aliases), (term,))):
            continue
        if _mentions(query, (term,)):
            return True
    return False


def _management_query_dimensions(query, aliases=()):
    """Separate intervention duties, without counting the disease as one."""
    text=query.casefold()
    for alias in sorted(aliases,key=len,reverse=True):
        text=text.replace(alias.casefold(),' ')
    return tuple(name for name,pattern in (
        ('nutrition',r'\bdiet\w*|\bnutrition\w*|\bfood\w*|\bfibre?\b|饮食|营养|食物|纤维'),
        ('activity',r'physical activity|\bexercis\w*|\btraining\b|\bsedentary\b|运动|活动|久坐'),
        ('weight',r'weight loss|weight reduction|weight management|减重|体重管理'),
        ('posture',r'head.of.bed|bed elevation|\bpostur\w*|lying down|recumbent|床头|平躺|体位'),
        ('clinical',r'follow.up|\bscreen\w*|\breferral\b|\breassess\w*|随访|筛查|转诊'))
        if re.search(pattern,text,re.I))


def _fair_risk_gap_topics(rows, previous_queries):
    """Cover different risks before extra relations, across priority tiers."""
    history = [query for query in previous_queries if isinstance(query, EvidenceQuery)]
    groups = {}
    for topic, gap in rows:
        risk_id = next(iter(topic.source_support_ids), topic.topic_id)
        groups.setdefault((topic.priority, risk_id), []).append((topic, gap))
    for group in groups.values():
        group.sort(key=lambda item: (
            sum(item[0].topic_id in query.supports for query in history),
            _risk_retrieval_priority(item[0]),
            not (item[0].kind == 'risk_management' and _current_management_topic(item[0])),
            'additional_risk_relation' in item[0].source_semantics, item[0].topic_id))
    # Priority orders a coverage round, rather than reserving the entire
    # budget for every relation of its first tier. Previously attempted risks
    # yield to untried ones on the next bounded round, including lower tiers.
    keys = sorted(groups, key=lambda key: (
        min(_risk_retrieval_priority(topic) for topic, _ in groups[key]),
        sum(key[1] in query.supports for query in history), key[0], key[1]))
    # A real current problem gets one bounded management retry after its
    # initial search. At most two such duties enter a recall round; they do
    # not reserve every later round or displace all scientific questions.
    managers=[(key,item) for key in keys for item in groups[key]
        if item[0].kind=='risk_management' and _current_management_topic(item[0])
        and sum(item[0].topic_id in query.supports for query in history)<=1]
    managers.sort(key=lambda pair:(
        not any(source.get('kind')=='clinical' and source.get('abnormal') is True
            and source.get('sourceCode')!='C' for source in pair[1][0].source_bindings),
        not (bool(_risk_search_disease_code(pair[1][0].title)) or any(
            _key(source.get('name',''))==_key(pair[1][0].title)
            for source in pair[1][0].source_bindings if source.get('kind')=='clinical')),
        sum(pair[1][0].topic_id in query.supports for query in history),pair[0]))
    result=[];reserved=set()
    for key,item in managers:
        if key[1] in reserved:
            continue
        result.append(item);groups[key].remove(item);reserved.add(key[1])
        if len(result)==2:
            break
    first=True
    while any(groups[key] for key in keys):
        for key in keys:
            if groups[key] and (not first or key[1] not in reserved):
                result.append(groups[key].pop(0))
        first=False
    return result


def _controlled_concept_aliases(topic, proposed, *, audit=None):
    """Accept source-bound recall synonyms, never a replacement exposure.

    Semantic equivalence is an explicit expansion duty and is independently
    checked again against patient facts during coverage and claim review.
    Literal taxonomy/strain and specimen constraints are not expandable.
    """
    if not isinstance(proposed, list):
        return {}
    from .evidence import _taxon_aliases
    result = {}
    for row in proposed:
        if not isinstance(row, dict):
            continue
        original = row.get('originalTerms', [])
        supplied_index = row.get('groupIndex')
        matches = [index for index, group in enumerate(topic.query_concepts[:2])
            if isinstance(original, list) and original and all(isinstance(term, str)
                and _key(term) in {_key(value) for value in group} for term in original)]
        if len(matches) != 1:
            if audit is not None:
                audit.append({'topicId':topic.topic_id,'suppliedIndex':supplied_index,
                    'status':'rejected','reason':'original_terms_not_unique_exact_input_group'})
            continue
        index = matches[0]
        group = topic.query_concepts[index]
        original_keys = {_key(term) for term in group}
        if supplied_index != index and {_key(term) for term in original} != original_keys:
            if audit is not None:
                audit.append({'topicId':topic.topic_id,'suppliedIndex':supplied_index,
                    'status':'rejected','reason':'index_recovery_requires_complete_input_group'})
            continue
        scope = row.get('scopePreserved', {})
        if (row.get('sameConcept') is not True or not isinstance(original, list)
                or not original or not all(isinstance(term, str) and _key(term) in original_keys
                    for term in original)
                or not isinstance(scope, dict) or not all(scope.get(key) is True for key in
                    ('population', 'specimen', 'anatomicalSite', 'taxonLevel', 'measurementNature'))):
            continue
        taxa = index == 1 and topic.query_relations and topic.query_relations[0].get('kind') == 'microbiome'
        protected = bool(taxa or any(re.search(r'\b(?:ATCC|DSM|JCM|strain|subsp)\b|菌株|'
            r'\b[cp]\.\d|\b(?:rs|HLA)[\d:-]', term, re.I) for term in group))
        taxon_forms = {_key(alias) for term in group for alias in _taxon_aliases(term)}
        own_codes = {_risk_search_disease_code(term) for term in group} - {None} if index == 0 else set()
        group_text = ' '.join(group)
        values = []
        for alias in row.get('aliases', []) if isinstance(row.get('aliases'), list) else []:
            if (not isinstance(alias, str) or not 2 <= len(alias.strip()) <= 160
                    or re.search(r'https?://|\b(?:AND|OR|NOT)\b|[;；]|\d+\.\d+\s*(?:mg|ml|mmol|kg|cm)', alias)):
                continue
            alias = alias.strip()
            if protected and _key(alias) not in taxon_forms:
                continue
            relatives = r'\b(?:family|familial|parent(?:al|s)?|father|mother|sibling|first.degree)\b|家族|亲属|父亲|母亲|父母|兄弟|姐妹'
            if re.search(relatives, group_text, re.I) and not re.search(relatives, alias, re.I):
                continue
            # An expanded disease name cannot become another known disease.
            code = _risk_search_disease_code(alias) if index == 0 else None
            if code and code not in own_codes:
                continue
            # Vascular measurements are not blood-specimen substitutions.
            # Retain the matrix lock for blood analytes/concentrations while
            # permitting ordinary pressure/flow terminology as itself.
            blood_matrix = r'serum|plasma|\bblood\b(?!\s+(?:pressure|flow|circulation|volume)\b)|血清|血浆|血液'
            compartment = ((blood_matrix, blood_matrix),
                (r'fecal|faecal|stool|粪便', r'fecal|faecal|stool|粪便'),
                (r'oral|saliva|口腔|唾液', r'oral|saliva|口腔|唾液'))
            if any(re.search(pattern, alias, re.I) and not re.search(expected, group_text, re.I)
                    for pattern, expected in compartment):
                continue
            values.append(alias)
        if values:
            result[index] = _unique(values)
        if audit is not None:
            audit.append({'topicId':topic.topic_id,'suppliedIndex':supplied_index,'resolvedIndex':index,
                'status':'accepted' if values else 'rejected',
                'reason':('restored_unique_original_terms' if supplied_index != index else 'exact_input_group')
                    if values else 'no_scope_preserving_alias','acceptedAliasCount':len(result.get(index,()))})
    return result


async def evaluation_gap_queries(client, plan: EvaluationEvidencePlan, gaps, *,
        max_queries: int, public: bool, previous_queries=()) -> list[EvidenceQuery]:
    """Rewrite only unmet risk relations, keeping the original concept scope.

    The model proposes search text, never source facts, approvals or looser
    eligibility. An invalid response falls back to an ordinary, source-bound
    expansion. Previously attempted text is not sent again in this report.
    """
    if type(max_queries) is not int or max_queries < 0:
        raise ValueError('max_queries must be a nonnegative integer')
    if max_queries==0:
        return []
    attempted = {_key(query.query if isinstance(query, EvidenceQuery) else query)
        for query in previous_queries}
    by_risk = {}
    for original_topic in plan.topics:
        topic = _retrieval_topic(plan, original_topic)
        if topic.kind not in {'risk', 'risk_management'}:
            continue
        for support in (topic.topic_id, *topic.source_support_ids):
            by_risk.setdefault((topic.kind, support), topic)
    selected = {}
    for gap in gaps or ():
        if not isinstance(gap, dict):
            continue
        kind = 'risk_management' if gap.get('missingRole') in {'management', 'clinical_management'} else 'risk'
        topic = by_risk.get((kind, gap.get('relationId'))) or by_risk.get((kind, gap.get('riskId'))) or by_risk.get((kind, gap.get('topicId')))
        if topic is not None:
            selected.setdefault(topic.topic_id, (topic, gap))
    pending=[];selected_risks=set()
    for topic,gap in _fair_risk_gap_topics(selected.values(), previous_queries):
        risk_id=next(iter(topic.source_support_ids),topic.topic_id)
        if risk_id in selected_risks:
            continue
        selected_risks.add(risk_id);pending.append((topic,gap))
        if len(pending)>=max_queries:
            break
    if not pending:
        return []
    originals = {topic.topic_id: topic for topic, _ in pending}
    pending = [(replace(topic,query_background=('management_clinical_action',))
        if topic.kind=='risk_management' and gap.get('actionKind')=='clinical' else
        _risk_gap_search_topic(topic, previous_queries,action_kind=gap.get('actionKind')), gap) for topic, gap in pending]
    rewritten = {}
    if client is not None:
        try:
            response = await client.generate({
                'task': 'expand_evaluation_risk_evidence_queries',
                'provider': 'pubmed' if public else 'evimed',
                'items': [{'topicId': topic.topic_id, 'disease': topic.title,
                    'requiredConcepts': [list(group) for group in _query(topic, public=public).required_concepts],
                    'conceptGroups':[{'groupIndex':index,'role':'outcome' if index==0 else
                        'retrieval_duty' if topic.kind=='risk_management' and index==1 else
                        'exposure' if index==1 else 'required_scope','terms':list(group)}
                        for index,group in enumerate(_query(topic,public=public).required_concepts)],
                    'missingRole': gap.get('missingRole') or 'population_association',
                    'actionKind': gap.get('actionKind'),
                    'reason': str(gap.get('reason') or '')[:600],
                    'duty': '检索具体食物/饮食调整、活动/体重管理，或有本人适用前提的具体检查/随访建议；指南目的、范围及泛评估管理监测不完成行动缺口'
                        if topic.kind == 'risk_management' else '疾病风险关联',
                    'relation': topic.query_relations[0] if topic.query_relations and topic.kind == 'risk' else None,
                    'suggestedSearchTerms': [term[:160] for term in gap.get('searchTerms', [])
                        if isinstance(term, str) and term.strip()][:8],
                    'previousQueries': [query.query for query in previous_queries
                        if isinstance(query, EvidenceQuery) and topic.topic_id in query.supports]}
                    for topic, gap in pending],
                'rules': [
                    '仅改写尚未完成的真实异常/行为/菌群对象与疾病风险关系检索，不生成用户结论、诊断或评分。',
                    '每条query同时包含requiredConcepts每组至少一个准确术语；可改用同组中英文同义词、指南/综述/队列研究检索方式，不只查疾病定义。',
                    '只验证本条独立关系，不要求一篇论文同时解释全部资料；clinical/behavior关系不因为用户还上传菌群报告就附加gut microbiota条件。management只检索该疾病/真实健康目标的适用建议，不强制包含患者具体数值或其他来源的异常。',
                    '管理检索允许生活方式干预或临床评估/筛查/随访/转诊原文，不要求生活方式与诊断所有词同时出现；检索到检查流程不等于该用户已满足侵入检查、出口梗阻或治疗失败等适应条件。',
                    '管理缺口须寻找具体推荐段或适用干预结果；若仅有指南摘要范围，改查nutrition/dietary intervention/physical activity recommendations等实际推荐，不再查guideline scope。只选一类职责，不把diet、exercise和临床检查全部AND。',
                    'lifestyle缺口优先普通食物/饮食模式与基础日常行为原文；纤维制剂、益生菌菌株或药物治疗效果不能替代普通食物建议。不要再用诊断、量表MCID或研究方法文献关闭执行缺口。',
                    '风险关联每次仅检索本条requiredConcepts对象组中的同一个真实暴露。原始资料含多个并列暴露时分开查询，不追加其他暴露、症状、正常检查或个人条件作为AND前提。',
                    'microbiome/function职责优先人类肠道粪便自然群落、同一菌/分子/微生物功能测量的病例对照或队列关联。病例对照可提供相关特征，不要求本人先确诊才检索，但不能当未来发病或因果。菌株服用RCT不能证明本人自然缺乏致病，培养未分离不等于宏基因相对丰度0，人体血浓度不等于菌群预测合成能力。保留反向或不显著结果供内部判断。',
                    'requiredConcepts表示医学概念而非必须复抄的字符串。可返回conceptAliases，为每个groupIndex绑定输入originalTerms并列确切同义词，query可采用它；不是扩大到相关但不同对象。家族/亲代/一级亲属的准确同义表达仍保留原亲属条件，不能变为本人确诊。报告栏目或功能分值可检索同一真实生物学对象及作用，不把预测能力变成人体浓度。',
                    'groupIndex严格从0开始，使用conceptGroups给出的标签：0=outcome，1=exposure或retrieval_duty。originalTerms每个词完整取自该组，不能把第一组编号写1、第二组写2；不扩required_scope组。',
                    'conceptAliases必须逐项sameConcept=true并确认scopePreserved的人群、标本、部位、菌种层级、检测性质均保持。不能把菌种变属、换菌株、换分子，不能以疾病名本身替代家族史/行为暴露；不确定则不扩。返回的同义仅用于召回，医学引用另行独审。',
                    '不得删除或放宽患者人群、标本、部位、菌种层级等原始适用要求，不用其他疾病或其他菌替代；不得把阴性/无关联研究排除出检索。',
                    'query与previousQueries必须不同，不包含姓名、用户ID、文件URL或病历原文。最多每topic一条，返回items[{topicId,query}]。'],
                'outputSchema': {'items': [{'topicId': '输入主题ID', 'query': '简短医学检索式',
                    'conceptAliases':[{'groupIndex':0,'originalTerms':['输入组的完整原词列表；如编号错误，只能由完整且唯一匹配的原词组恢复'],
                        'aliases':['同一医学对象的确切同义名'],'sameConcept':True,
                        'scopePreserved':{'population':True,'specimen':True,'anatomicalSite':True,
                            'taxonLevel':True,'measurementNature':True}}]}]},
            })
            proposals = response.get('items', []) if isinstance(response, dict) else []
            allowed = {topic.topic_id: topic for topic, _ in pending}
            for row in proposals:
                if not isinstance(row, dict) or row.get('topicId') not in allowed:
                    continue
                topic, query = allowed[row['topicId']], row.get('query')
                added = _controlled_concept_aliases(topic, row.get('conceptAliases'), audit=plan._alias_events)
                if added:
                    stored = plan._concept_aliases.setdefault(topic.topic_id, {})
                    for index, aliases in added.items():
                        stored[index] = _unique([*stored.get(index, ()), *aliases])
                    topic = _retrieval_topic(plan, topic)
                    allowed[topic.topic_id] = topic
                    pending = [(topic if item.topic_id == topic.topic_id else item, gap)
                        for item, gap in pending]
                if (not isinstance(query, str) or not 8 <= len(query.strip()) <= 500
                        or _key(query) in attempted or not all(_mentions(query, group)
                            for group in _query(topic, public=public).required_concepts)
                        or re.search(r'https?://|@[A-Za-z]|\b(?:userId|sessionId|groupId)\b|\bNOT\b|排除阴性|仅阳性', query, re.I)):
                    continue
                # Search widening changes aliases/design, not the question.
                # An unrelated microbiome prerequisite must not creep into
                # an ordinary clinical or behavioural relation.
                microbiome = r'gut microbiot\w*|microbiome|intestinal microbiot\w*|菌群|dysbiosis'
                relation_kind = topic.query_relations[0].get('kind') if topic.query_relations else None
                microbial_duty = (relation_kind in {'microbiome', 'function'} or relation_kind is None
                    and topic.source_kinds and all(kind in {'microbe', 'function', 'context'} for kind in topic.source_kinds))
                if not microbial_duty and re.search(microbiome, query, re.I) and not any(
                        re.search(microbiome, alias, re.I) for group in _query(topic, public=public).required_concepts for alias in group):
                    continue
                if re.search(r'\b\d+(?:\.\d+)?\s*[/：:]\s*\d|(?<![A-Za-z])\d+\.\d+', query):
                    continue
                if _risk_rewrite_adds_exposure(query, topic, originals[topic.topic_id]):
                    continue
                if topic.kind == 'risk_management' and topic.query_background and not re.search(
                        r'lifestyle|diet\w*|food|nutrition|exercis\w*|physical activity|screen\w*|follow.up|referral|reassess\w*|饮食|食物|营养|运动|筛查|随访|转诊', query, re.I):
                    continue
                if topic.kind == 'risk_management' and len(_management_query_dimensions(query,topic.aliases))>1:
                    continue
                rewritten.setdefault(topic.topic_id, query.strip())
        except Exception:
            # A failed query rewrite is not permission to invent a relation.
            pass
    queries = []
    query_positions={}
    for topic, _ in pending:
        query = _query(topic, public=public, expansion=True)
        if topic.topic_id in rewritten:
            query = replace(query, query=rewritten[topic.topic_id])
        key=_key(query.query)
        if key in query_positions:
            index=query_positions[key];previous=queries[index]
            same_tail=previous.required_concepts[1:]==query.required_concepts[1:]
            shared_identity=bool(previous.required_concepts and query.required_concepts and
                {_risk_search_disease_code(term) for term in previous.required_concepts[0]} .intersection(
                    {_risk_search_disease_code(term) for term in query.required_concepts[0]})-{None})
            if (topic.kind=='risk_management' and previous.intent==query.intent and same_tail and shared_identity):
                # One actual search can serve the same management question
                # for two internal hypotheses. Preserve both query duties;
                # neither gets clinical approval from shared recall.
                queries[index]=replace(previous,supports=_unique([*previous.supports,*query.supports]),
                    required_concepts=(_unique([*previous.required_concepts[0],*query.required_concepts[0]]),
                        *previous.required_concepts[1:]))
            continue
        if key in attempted:
            continue
        attempted.add(key);query_positions[key]=len(queries)
        queries.append(query)
    return queries


def topic_bindings(plan: EvaluationEvidencePlan) -> dict[str, dict[str, Any]]:
    return {topic.topic_id: {'topicId': topic.topic_id, 'topicKind': topic.kind, 'title': topic.title,
        'factIds': list(topic.fact_ids), 'sourceKinds': list(topic.source_kinds),
        'sourceObjects': list(topic.source_objects), 'allowedRoles': list(topic.allowed_roles),
        'sourceSupportIds': list(topic.source_support_ids), 'sourceSemantics': list(topic.source_semantics),
        'contextFactIds': list(topic.context_fact_ids),
        'requiredRoles': list(topic.required_roles), 'consumerPaths': [list(path) for path in topic.consumer_paths],
        'sources': deepcopy(list(topic.source_bindings)),
        **({'queryTerm':topic.query_term,'queryConcepts':[list(group)for group in topic.query_concepts]}
                    if topic.kind=='lifestyle'else {})} for original in plan.topics
                        for topic in (_retrieval_topic(plan, original),)}


def _publication_identity(record: dict) -> str:
    doi, pmid = evaluation_publication_identifiers(record)
    if doi:
        return 'doi:' + doi
    if pmid:
        return 'pmid:' + pmid
    if record.get('title'):
        return 'title:' + _key(record['title']) + ':' + str(record.get('year') or '')
    return str(record.get('evidenceKey') or record.get('number'))


def _record_identity(record) -> str:
    row = _plain(record)
    identity = evaluation_publication_identity(record)
    if is_trusted_guide_block(row) or identity != evaluation_publication_identity(row):
        return 'guide:' + repr(evaluation_publication_identity(record))
    return _publication_identity(row)


def _original_record_identity(record: dict) -> tuple:
    """Keep candidate routing local to one literal original and its provenance."""
    row = _plain(record)
    return (_record_identity(record), _record_key(row), row.get('provider'),
        row.get('sourceId'), row.get('sourceEvidenceKey'),
        sha256(_original(row).encode('utf-8')).hexdigest())


def _record_key(record: dict) -> str:
    return str(record.get('evidenceKey') or 'number:' + str(record.get('number')))


def _original(record: dict) -> str:
    return next((text for key in ('sourceText', 'abstract')
        if (text:=evaluation_original_text({**record,'abstract':record.get(key)}))), '') or evaluation_original_text(record)


def _lifestyle_guideline_match(topic,record,source):
    """Reuse an adopted guideline only for its literal exposure and outcome."""
    return (topic.kind=='lifestyle'and record.get('category')=='guideline'
        and len(topic.query_concepts)==2 and bool(source)
        and all(_mentions(source,concepts)for concepts in topic.query_concepts))


def _adult_sleep_reference_original(topic, source, title=''):
    """A methods abstract or a children's test protocol is not a sleep norm."""
    if 'adult_sleep_duration_reference' not in topic.source_semantics:
        return True
    if re.search(r'children|pediatric|paediatric|multiple sleep latency|maintenance of wakefulness|MSLT',title,re.I):
        return False
    return bool(re.search(r'(?:recommend\w*|should|consensus)[^.。\n]{0,180}'
        r'\b\d+(?:\s*(?:or more|to|–|-)\s*\d*)?\s*hours?\b',source,re.I))


def _mentions(source: str, aliases) -> bool:
    source = unicodedata.normalize('NFKC', source).casefold()
    for alias in aliases:
        value = unicodedata.normalize('NFKC', alias).casefold()
        pattern = re.escape(value).replace(r'\ ', r'[\s-]+')
        left = r'(?<![a-z0-9])' if re.match(r'[a-z0-9]', value) else ''
        right = r'(?![a-z0-9])' if re.search(r'[a-z0-9]$', value) else ''
        if re.search(left + pattern + right, source):
            return True
    return False


_METHOD_EVALUATION_TITLE = re.compile(r'reporting quality|quality (?:evaluation|assessment)|psychometric|'
    r'bibliometric|adherence|clinical audit|decision support|(?:score|scale) validation|'
    r'指南质量|报告质量|量表验证|依从性', re.I)
_COHORT_SENTENCE = re.compile(r'\b(?:patients?|cohorts?|controls?|treated|treatment|therapy|'
    r'chemotherapy|trial|infection|infected|chronic)\b|患者|治疗|感染者|临床队列', re.I)
_FUNCTION_PROCESS = re.compile(r'\b(?:biosynth\w*|synthesi[sz]\w*|produce|produces|produced|producing|production|ferment\w*|'
    r'metaboli[sz]\w*|regulat\w*|suppl\w*|contribut\w*|participat\w*|cofactors?|'
    r'biological processes|precursors?)\b|合成|生成|发酵|分解|代谢|辅因子', re.I)


def _topic_definition(topic, source):
    for alias in topic.aliases:
        phrase = re.escape(alias).replace(r'\ ', r'[\s-]+')
        if re.search(phrase + r'[^.,;。；\n]{0,50}(?:\b(?:is|are)\s+(?:a|an|the|characteri[sz]ed|defined)\b|'
                r'\b(?:refers? to|defined as|characteri[sz]ed by)\b|是一种|是一类|指的是|定义为)', source, re.I):
            return True
    return False


def _disease_relation_candidate(topic, source, title=''):
    """A relation candidate must name an actual organism in literal text.

    This is recall/coverage screening, not approval of direction, causation or
    individual applicability. Both independent citation reviews still apply.
    """
    if not topic.query_taxa or not _mentions(source, topic.query_taxa):
        return False
    relation = re.compile(r'\b(?:associat\w*|correlat\w*|abundan\w*|enrich\w*|deplet\w*|'
            r'increas\w*|decreas\w*|higher|lower|prevalen\w*|detect\w*|presence)\b|'
            r'丰度|增加|降低|富集|减少|相关|关联|检出', re.I)
    if _mentions(title, topic.aliases) and relation.search(source):
        return True
    return any(_mentions(sentence, topic.query_taxa) and _mentions(sentence, topic.aliases)
        and relation.search(sentence) for sentence in re.split(r'[。；;\n]|(?<=[.!?])\s+', source))


def _risk_relation_candidate(topic, source, title=''):
    """Require the actual exposure/disease relation, including inverse results.

    This remains only candidate screening. It does not decide whether the
    direction or patient applicability supports the proposed risk.
    """
    if topic.kind != 'risk' or len(topic.query_concepts) < 2:
        return False
    if not all(_mentions(source, group) for group in topic.query_concepts):
        return False
    relation = re.compile(r'\b(?:associat\w*|correlat\w*|risk(?:s|y)?|inciden\w*|'
        r'predict\w*|protect\w*|susceptib\w*|hazard|odds|risk factor|linked|contribut\w*|'
        r'increas\w*|decreas\w*|reduc\w*|lower\w*|higher)\b|'
        r'风险|关联|相关|发生率|易感|危险因素|保护因素|无关|未发现关联|增加|降低|减少', re.I)
    predicate = re.compile(r'\b(?:associat\w*(?:\s+with)?|correlat\w*(?:\s+with)?|'
        r'predict\w*|protect\w*|linked\s+(?:to|with)|contribut\w*\s+to|'
        r'increas\w*|decreas\w*|reduc\w*|lower\w*|higher)\b|与|和|相关于|关联于|导致|降低|增加', re.I)
    disease, exposure = topic.query_concepts[:2]
    # A list of two covariates for a third outcome is not a relation between
    # those covariates. Require the actual objects on opposite sides of a
    # relational predicate, or an explicit study-title relation naming both.
    for sentence in re.split(r'[。；;\n]|(?<=[.!?])\s+', source):
        if not relation.search(sentence) or not all(_mentions(sentence, group) for group in topic.query_concepts):
            continue
        for match in predicate.finditer(sentence):
            left, right = sentence[:match.start()], sentence[match.end():]
            if (_mentions(left, disease) and _mentions(right, exposure)
                    or _mentions(left, exposure) and _mentions(right, disease)):
                return True
        pair = re.search(r'\b(?:association|correlation|relationship|link)\s+(?:of|between)\s+'
            r'(.+?)\s+(?:and|with)\s+(.+)', sentence, re.I)
        if pair and (_mentions(pair[1], disease) and _mentions(pair[2], exposure)
                or _mentions(pair[1], exposure) and _mentions(pair[2], disease)):
            return True
    return (all(_mentions(title, group) for group in topic.query_concepts)
        and bool(relation.search(title)) and bool(relation.search(source)))


def management_principle_errors(source, *, human_trial=False):
    """Reject study/report nouns as instructions, without inventing advice.

    An actual intervention effect can ground an independently reviewed
    low-risk plan. A cross-sectional association or count of recommendations
    cannot supply the missing recommendation direction.
    """
    directive = re.compile(r'\b(?:should|must|advis(?:e|ed|es)|encourag\w*|'
        r'first[- ]line\s+(?:of\s+)?(?:treatment|therapy)|'
        r'recommend(?:ed|s)?\s+(?:that|to|for|increas\w*|reduc\w*|consum\w*|avoid\w*|'
        r'limit\w*|eat\w*|perform\w*|offer\w*|refer\w*))\b|建议|推荐(?:增加|减少|摄入|避免|进行)|应当|宜', re.I)
    enumeration = re.compile(r'\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|several)\s+'
        r'(?:recommendation\s+statements?|recommendations?)\b|'
        r'\b(?:recommendations?|recommendation statements?)\b.{0,55}'
        r'\b(?:developed|formulated|issued|produced|related\s+to|cover(?:ed|ing)?)\b|'
        r'\d+\s*(?:项|条)(?:建议|推荐)|(?:建议|推荐)(?:条目|数量)', re.I)
    sentences = [sentence for sentence in re.split(r'[。；;\n]|(?<=[.!?])\s+', source) if sentence.strip()]
    counted = [sentence for sentence in sentences if enumeration.search(sentence) and not directive.search(sentence)]
    if counted and len(counted) == len(sentences):
        return ['management_scope_only']
    if (not directive.search(source) and re.search(
            r'\b(?:modifiable\s+)?risk factors?\s+(?:include|are|comprise)|(?:可改变的)?危险因素包括|风险因素包括',
            source,re.I)):
        return ['management_risk_factor_list_not_recommendation']
    if (not human_trial and not directive.search(source)
            and re.search(r'\b(?:odds ratio|hazard ratio|cross[- ]sectional|observational|'
                r'associat\w*|correlat\w*)\b|\bOR\s*[,=:]?\s*\d|观察性|横断面|相关|关联', source, re.I)
            and re.search(r'\b(?:exercis\w*|physical activity|sedentary|diet\w*|sleep\w*|'
                r'water|fluid|food\w*|fruit\w*)\b|运动|活动|久坐|饮食|睡眠|饮水|水果', source, re.I)):
        return ['management_observational_association_not_recommendation']
    return []


def management_actionable_content(source, *, aliases=(), human_trial=False):
    """Recall actual recommendations/effects, not a guideline's stated scope.

    A positive result is still a candidate, never approval of a personal
    action, disease indication, ingredient, frequency or dose.
    """
    directive = re.compile(r'\b(?:recommend(?:ed|s)?|should|advis(?:e|ed|es)|encourag\w*|'
        r'includes?|consists?|perform|obtain|consider|measure|check|refer|review)\b|建议|推荐|应当|宜|包括', re.I)
    change = re.compile(r'\b(?:reduc\w*|restrict\w*|limit\w*|avoid\w*|replac\w*|substitut\w*|'
        r'consum\w*|intake|eat\w*|increas\w*|exercis\w*|physical activity|training|weight loss|'
        r'weight reduction|dietary modification)\b|减少|限制|避免|替代|摄入|食用|增加|运动|活动|减重|饮食调整', re.I)
    clinical = re.compile(r'\b(?:screen\w*|measur\w*|monitor\w*|test\w*|refer\w*|reassess\w*|'
        r'assess\w*|evaluat\w*|follow.up|endoscop\w*)\b|筛查|测量|监测|检测|复查|转诊|评估|随访', re.I)
    effect = re.compile(r'\b(?:effective\w*|improv\w*|benefit\w*|reduc\w*|increas\w*|'
        r'no effect|not effective)\b|有效|改善|获益|无效|增加|减少', re.I)
    intervention = re.compile(r'\b(?:diet\w*|food\w*|fruit\w*|nutri\w*|fiber|fibre|'
        r'exercis\w*|physical activity|training|intervention\w*|supplement\w*)\b|饮食|食物|水果|营养|膳食|纤维|运动|干预|补充', re.I)
    for sentence in re.split(r'[。；;\n]|(?<=[.!?])\s+', source):
        if re.search(r'researchers? (?:clarify|should)|trial design|RCT design|methodological rigor|'
                r'minimal (?:clinically )?important (?:difference|change)|MCID|MIC thresholds|研究者|研究设计|方法学严谨',sentence,re.I):
            continue
        if management_principle_errors(sentence, human_trial=human_trial):
            continue
        if re.search(r'\b(?:aims?|objectives?|purpose|scope|focus)\b.*\b(?:study|guideline|review|address|develop)\b|'
                r'\b(?:guideline|review|study)\b.{0,60}\b(?:aims?|intends?|is intended|is designed|focuses?)\b|'
                r'\b(?:future|further)\s+(?:research|studies|trials)\b|旨在|研究目的|指南范围', sentence, re.I):
            continue
        from .evaluation_risk_assessment import _general_management_principle
        if (_general_management_principle(sentence) and re.search(
                r'critical|important|support|recommend|should|useful|first[- ]line|'
                r'建议|推荐|重要|关键|一线',sentence,re.I)):
            return True
        if directive.search(sentence) and change.search(sentence):
            return True
        if directive.search(sentence) and clinical.search(sentence):
            detail = sentence.casefold()
            for alias in sorted(aliases, key=len, reverse=True):
                detail = detail.replace(alias.casefold(), ' ')
            words = re.findall(r'[a-z][a-z-]*', detail)
            words = [word for word in words if not re.fullmatch(r'this|that|the|a|an|and|or|for|of|to|in|with|'
                r'by|as|be|is|are|on|at|adult\w*|patient\w*|individual\w*|clinical|chronic|regular\w*|'
                r'guideline\w*|recommend\w*|should|evaluation|assessment|reassessment|management|monitoring|'
                r'follow-up|follow|up|screening|advised|include\w*', word)]
            chinese = re.sub(r'临床|评估|评价|管理|监测|随访|复查|指南|建议|推荐|应当|进行|'
                r'患者|成人|成年人|人群|本病|疾病|相关|该|的|对|与|及|和|或|包括|予以', '', detail)
            if (len(words) >= 2 or any(len(word) >= 7 for word in words)
                    or re.search(r'[\u4e00-\u9fff]{2,}', chinese)
                    or re.search(r'\d|daily|weekly|monthly|annually|每天|每周|每月', detail)):
                return True
        if effect.search(sentence) and (human_trial or intervention.search(sentence)):
            if re.search(r'\b(?:result(?:s|ed|ing)?|conclu\w*|effective|improv(?:e[sd]?|ing)|no effect|not effective)\b|有效|改善|无效', sentence, re.I):
                return True
    return False


def _risk_management_candidate(topic, source, title=''):
    if topic.kind != 'risk_management' or not all(_mentions(source, group) for group in topic.query_concepts):
        return False
    # Trial or guideline text is retained only as a candidate for management;
    # the final review still verifies the proposed action and its exact dose.
    guidance = bool(re.search(r'\b(?:guideline\w*|recommend\w*|consensus)\b|指南|建议|推荐|共识',
        title + ' ' + source, re.I))
    human_trial = bool(re.search(r'\b(?:randomi[sz]ed|controlled trial|clinical trial|intervention)\b|随机|临床试验', source, re.I)
        and re.search(r'\b(?:participants?|subjects?|patients?|adults?|volunteers?)\b|受试者|患者|成人', source, re.I))
    if _METHOD_EVALUATION_TITLE.search(title) and not human_trial:
        return False
    return (guidance or human_trial) and management_actionable_content(source,
        aliases=topic.aliases, human_trial=human_trial)


def evaluation_management_gaps(plan, selection):
    """Schedule real current-problem action gaps inside the shared recall cap."""
    return [{'riskId': topic.source_support_ids[0], 'topicId': topic.topic_id,
        'missingRole': 'management', 'reason': 'no_actionable_management_candidate',
        'searchTerms': list(topic.aliases)}
        for topic in plan.topics if topic.kind == 'risk_management' and topic.query_enabled
        and not _current_clinical_action(topic)
        and topic.source_support_ids and _current_management_topic(topic)
        and not selection.by_topic.get(topic.topic_id)]


def _risk_scope_mismatch(topic, source, title='', *, subject_age=None, subject_sex=None):
    """Reject explicit different disorders/populations, never infer diagnosis.

    These checks are only for personal risk/management candidates. A paper
    can remain as a general biochemical-function source without inheriting a
    rejected patient's risk binding.
    """
    own = ' '.join(topic.aliases)
    qualifiers = (
        (r'portal hypertension|门脉高压|门静脉高压', r'portal hypertension|门脉高压|门静脉高压'),
        (r'pulmonary hypertension|肺动脉高压', r'pulmonary hypertension|肺动脉高压'),
        (r'intracranial hypertension|颅内高压', r'intracranial hypertension|颅内高压'),
        (r'pre[- ]?eclampsia|妊娠|先兆子痫|pregnan|maternal|postpartum|产后',
            r'pre[- ]?eclampsia|妊娠|先兆子痫|pregnan|maternal|postpartum|产后'),
        (r'familial hypercholestero?l[ae]mia|家族性高胆固醇血症',
            r'familial hypercholestero?l[ae]mia|家族性高胆固醇血症'),
    )
    if any(re.search(pattern, title, re.I) and not re.search(expected, own, re.I)
            for pattern, expected in qualifiers):
        return True
    female_scope = (r'\bwomen\b|\bfemales?\b|maternal|pregnan|'
        r'polycystic ovar(?:y|ian)|\bPCOS\b|ovarian insufficien|menopaus|endometriosis|女性|孕妇|多囊卵巢|绝经')
    if subject_sex in {'男', 'male', 'm'} and re.search(female_scope, title, re.I) \
            and not re.search(r'\bmen\b|\bmales?\b|both sexes|男性', title, re.I):
        return True
    if subject_sex in {'女', 'female', 'f'} and re.search(r'\bmen\b|\bmales?\b|男性', title, re.I) \
            and not re.search(r'\bwomen\b|\bfemales?\b|both sexes|女性', title, re.I):
        return True
    if type(subject_age) in (int, float):
        children = r'\b(?:child(?:ren|hood)?|pediatric|paediatric|infants?|neonat\w*)\b|儿童|婴幼儿|新生儿'
        adults = r'\b(?:adults?|adulthood|elderly|older adults?)\b|成人|老年'
        if subject_age >= 18 and re.search(children, title, re.I) and not re.search(adults, title, re.I):
            return True
        if subject_age < 18 and re.search(adults, title, re.I) and not re.search(children, title, re.I):
            return True
    return False


def _role_candidate_priority(topic, record, *, subject_age=None):
    """Rank literal candidates for the requested role, never approve them.

    Prefer a definition or general biochemical process over method-quality
    reports and treatment cohorts. A review's AI study-type label and a new
    publication date cannot substitute for suitable original content.
    """
    source, title = _original(record), str(record.get('title') or '')
    if 'measured_ecological_dimension' in topic.source_semantics:
        from .evaluation_function_evidence import ecological_association_priority
        score = ecological_association_priority(source,title,topic.query_concepts[0],topic.query_concepts[2])
        return score + 3 * bool(re.fullmatch(r'(?:pubmed:)?\d+', str(record.get('sourceId') or '')))
    review = bool(re.search(r'\breview\b|\bconsensus\b|\bguideline\b|综述|共识|指南', title, re.I)
        or re.search(r'\bthis review\b|\bwe review\b', source, re.I))
    definition = topic.kind in {'disease', 'association'} and _topic_definition(topic, source)
    process = topic.kind == 'function' and any(_mentions(sentence, topic.aliases)
        and _FUNCTION_PROCESS.search(sentence) and not _COHORT_SENTENCE.search(sentence)
        for sentence in re.split(r'[.!?。；;\n]', source))
    method_only = bool(_METHOD_EVALUATION_TITLE.search(title))
    conditional_study = bool(_COHORT_SENTENCE.search(source)) and not review
    diagnostic = topic.kind == 'clinical' and bool(re.search(r'diagnos|patholog|诊断|鉴别', title, re.I))
    priority = 5 * bool(definition) + 4 * bool(process) + 2 * review + 3 * diagnostic - 4 * method_only - 3 * conditional_study
    if 'measured_sugar_role' in topic.source_semantics:
        from .evaluation_reference_plan import measured_function_source_priority
        priority += measured_function_source_priority('P1',title,source,topic.query_concepts[0])
    if topic.kind == 'risk':
        priority += 12 if _risk_relation_candidate(topic, source, title) else -12
    elif topic.kind == 'risk_management':
        priority += 12 if _risk_management_candidate(topic, source, title) else -12
        behavior = _management_behavior_scope(topic)
        if behavior and _mentions(source, behavior[1]) and re.search(
                r'recommend|advis|effective|improv|increas|reduc|first.line|should|建议|推荐|有效|改善', source, re.I):
            # Prefer a source passage about this person's actual behaviour
            # over another block from the same guide that only outlines a
            # diagnostic flowchart. This is relevance, never action approval.
            priority += 12
        if re.search(r'(?:screen|monitor|follow.up|reassess|refer|evaluat)\w*|评估|复查|随访|转诊', source, re.I) \
                and re.search(r'guideline|recommend|should|advis|指南|推荐|建议', source, re.I):
            priority += 4
    if topic.kind == 'disease' and topic.query_taxa:
        relation = _disease_relation_candidate(topic, source, title)
        priority += (12 + 4 * _mentions(title, topic.query_taxa)) if relation else -8
    if topic.topic_id == _topic_id('function', 'P2'):
        # A low-fat diet/SCFA outcome is not the same scientific duty as an
        # actual lipid or bile conversion. Reuse the existing domain-process
        # distinction only for ranking; original claims still need both reviews.
        from modules.report.evaluation_content import _lipid_pathway_process
        direct_process = bool(_lipid_pathway_process(source))
        priority += 8 * direct_process
        if direct_process and re.search(r'\b(?:bile acids?|lipids?|cholesterol|steroids?|fatty acids?)\b|脂质|胆汁酸', title, re.I):
            priority += 4
    if _health_function_topic(topic):
        members = [term for term in topic.query_concepts[0]
            if re.fullmatch(r'[A-Z][a-z]+(?:\s+[a-z][a-z.-]*){0,3}', term)]
        if members:
            from modules.report.evaluation_reference_plan import health_member_function_priority, opportunistic_health_priority
            priority += health_member_function_priority(members, title, source,
                subject_age=subject_age if topic.topic_id == _topic_id('function', 'health:beneficialBacteria') else None)
            if topic.topic_id == _topic_id('function', 'health:opportunisticBacteria'):
                priority += opportunistic_health_priority(members, source)
    return priority


def _publication_notice(record):
    """A correction/retraction notice is not an independent scientific claim.

    Keep the provider's raw record in retrieval audit. This only excludes
    explicit notice titles/types from candidate interpretation; criticism,
    negative results and ordinary research on error correction stay eligible.
    """
    title = unicodedata.normalize('NFKC', str(record.get('title') or '')).strip()
    types = record.get('studyTypes', record.get('study_types', [])) or []
    notices = {'published erratum', 'erratum', 'corrigendum', 'correction',
        'retracted publication', 'retraction of publication', 'retraction',
        'expression of concern', '撤稿', '勘误', '更正声明', '撤稿声明'}
    if any(str(value).strip().casefold() in notices for value in types):
        return True
    return bool(re.search(r'^(?:(?:publisher|author)\s+)?'
        r'(?:correction|corrigendum|erratum|retraction(?:\s+(?:notice|note))?|expression of concern)'
        r'(?:\s*[:：.(]|\s+(?:to|for|in)\b)|'
        r'^\[?(?:retracted|withdrawn)\]?\s*[:：.]|'
        r'\[(?:retracted|withdrawn)\]\s*$|'
        r'^(?:勘误|更正|撤稿|撤回)(?:声明|通知)?\s*[:：]|[（(](?:勘误|撤稿|已撤回)[）)]\s*$', title, re.I))


def select_topic_evidence(plan: EvaluationEvidencePlan, records, *, per_topic_limit: int = 3,
                          max_records: int = 40, subject_age=None, subject_sex=None,
                          attempted_queries=()) -> TopicEvidenceSelection:
    """Deduplicate/reuse already retrieved, bound candidates with fair limits.

    A candidate is not a verified claim. Unknown supports, an AI-only summary,
    or a title mention without a source binding cannot create coverage. The
    caller still independently verifies originals and approves each sentence.
    Quotas count publications; every matched original block of a selected
    publication survives into the existing semantic-record/summary contract.
    """
    if any(type(value) is not int or value < 0 for value in (per_topic_limit, max_records)):
        raise ValueError('evidence budgets must be nonnegative integers')
    plan = replace(plan, topics=tuple(_retrieval_topic(plan, topic) for topic in plan.topics))
    topic_index = {topic.topic_id: topic for topic in plan.topics}
    original_supports = {support for topic in plan.topics
        for support in (*topic.fact_ids, *topic.context_fact_ids, *topic.source_support_ids, topic.topic_id)}
    grouped, candidate_topics = {}, {}
    def clinical_scope_matches(topic, record):
        from .evaluation_source_scope import source_candidate_scope_errors
        from .evaluation_originals import evaluation_source_packet
        packet = evaluation_source_packet(record, query=topic.query_term)
        return not source_candidate_scope_errors(source_text=_original(record), title=str(record.get('title') or ''),
            selected_quotes=[part['text'] for part in packet['selectedPassages']],
            patient_facts=plan.patient_facts or {row.get('factId'): row for row in topic.source_bindings},
            target_context={'subject': {**plan.subject,
                **({'age': subject_age} if subject_age is not None else {}),
                **({'gender': subject_sex} if subject_sex is not None else {})},
                'problem_fact_ids': (*topic.fact_ids, *topic.context_fact_ids),
                'source_concepts': topic.aliases})
    for original in records:
        record = _plain(original)
        if _publication_notice(record) or not _original(record):
            continue
        supports = set(record.get('supports', []))
        matched = {topic.topic_id for topic in plan.topics if topic.topic_id in supports
            or supports.intersection([*topic.fact_ids, *topic.source_support_ids])}
        matched = {topic_id for topic_id in matched if not _function_source_binding_required(topic_index[topic_id])
            or _function_topic_source_match(topic_index[topic_id], _original(record), title=str(record.get('title') or ''))}
        # Reuse a source already bound to an adopted observation for a clearly
        # named scientific domain. This adds candidate theme supports only,
        # never support for every molecule, a person's score or a new diagnosis.
        if supports & original_supports:
            source = _original(record)
            for topic in plan.topics:
                if topic.kind == 'function' and _function_topic_source_match(topic, source, title=str(record.get('title') or '')):
                    matched.add(topic.topic_id)
                elif topic.kind in {'disease', 'association'} and _mentions(source, topic.aliases):
                    matched.add(topic.topic_id)
                elif topic.kind == 'risk' and _risk_relation_candidate(topic, source, str(record.get('title') or '')):
                    matched.add(topic.topic_id)
                elif topic.kind == 'risk_management' and _risk_management_candidate(topic, source, str(record.get('title') or '')):
                    matched.add(topic.topic_id)
                elif _lifestyle_guideline_match(topic,record,source):
                    matched.add(topic.topic_id)
        matched = {topic_id for topic_id in matched if not (
            topic_index[topic_id].kind == 'disease' and topic_index[topic_id].query_taxa)
            or _disease_relation_candidate(topic_index[topic_id], _original(record), str(record.get('title') or ''))}
        matched = {topic_id for topic_id in matched if topic_index[topic_id].kind != 'risk'
            or _risk_relation_candidate(topic_index[topic_id], _original(record), str(record.get('title') or ''))}
        matched = {topic_id for topic_id in matched if topic_index[topic_id].kind != 'risk_management'
            or _risk_management_candidate(topic_index[topic_id], _original(record), str(record.get('title') or ''))}
        matched = {topic_id for topic_id in matched if topic_index[topic_id].kind not in {'risk', 'risk_management'}
            or not _risk_scope_mismatch(topic_index[topic_id], _original(record), str(record.get('title') or ''),
                subject_age=subject_age, subject_sex=subject_sex)}
        matched = {topic_id for topic_id in matched if topic_index[topic_id].kind != 'clinical'
            or clinical_scope_matches(topic_index[topic_id], record)}
        matched = {topic_id for topic_id in matched if _adult_sleep_reference_original(
            topic_index[topic_id], _original(record), str(record.get('title') or ''))}
        if not matched:
            continue
        identity = _record_identity(original)
        grouped.setdefault(identity, []).append((original, matched))
        candidate_topics.setdefault(identity, set()).update(matched)
    by_topic = {topic.topic_id: [] for topic in plan.topics}
    priorities = {(topic_id, identity): max(_role_candidate_priority(topic_index[topic_id], _plain(row),
        **({'subject_age': subject_age} if subject_age is not None else {}))
        for row, matched in originals if topic_id in matched)
        for identity, originals in grouped.items() for topic_id in candidate_topics[identity]}
    chosen = []
    # A shared paper uses one bibliography/budget position while covering more
    # than one question. Its different originals remain separate records.
    for _ in range(per_topic_limit):
        for topic in plan.topics:
            if len(by_topic[topic.topic_id]) >= per_topic_limit:
                continue
            candidates = [identity for identity in grouped if topic.topic_id in candidate_topics[identity]
                and identity not in by_topic[topic.topic_id]]
            # Reuse is a tie-breaker after role quality. Otherwise a niche
            # disease study selected for P7 can displace a physiology review
            # in P1/P3 merely because it already occupies a budget position.
            candidates = [identity for identity in candidates if identity in chosen or len(chosen) < max_records]
            candidates.sort(key=lambda identity: priorities[(topic.topic_id, identity)], reverse=True)
            best = priorities[(topic.topic_id, candidates[0])] if candidates else None
            equivalent = [identity for identity in candidates if priorities[(topic.topic_id, identity)] == best]
            candidate = next((identity for identity in equivalent if identity in chosen), next(iter(equivalent), None))
            if candidate is None:
                continue
            if candidate not in chosen:
                chosen.append(candidate)
            by_topic[topic.topic_id].append(candidate)
    retained, contexts = [], {}
    mapped = {topic_id: [] for topic_id in by_topic}
    personal_bindings = {}
    for topic in plan.topics:
        if topic.kind in {'risk', 'risk_management'}:
            for support in (topic.topic_id, *topic.source_support_ids):
                personal_bindings.setdefault(support, set()).add(topic.topic_id)
    for identity in chosen:
        selected_topics = {topic_id for topic_id, identities in by_topic.items() if identity in identities}
        for original, matched in grouped[identity]:
            # A second original may have earned the publication's place. Do
            # not transfer its topic supports or query metadata to the first.
            added = [topic_id for topic_id in topic_index if topic_id in selected_topics & matched]
            if not added:
                continue
            raw = _plain(original)
            supports = list(dict.fromkeys([*raw.get('supports', []), *added,
                *(support for topic_id in added for support in topic_index[topic_id].source_support_ids)]))
            supports = [support for support in supports if support not in topic_index or support in added]
            supports = [support for support in supports if support not in personal_bindings
                or personal_bindings[support].intersection(added)]
            if isinstance(original, EvidenceRecord):
                copied = original.model_copy(deep=True, update={'supports': supports})
            else:
                copied = {**deepcopy(original), 'supports': supports}
            key = _record_key(_plain(copied))
            if copied not in retained:
                retained.append(copied)
            contexts.setdefault(key, []).append(deepcopy(original))
            for topic_id in added:
                if key not in mapped[topic_id]:
                    mapped[topic_id].append(key)
    mapped = {topic_id: tuple(keys) for topic_id, keys in mapped.items()}
    contexts = {key: tuple(originals) for key, originals in contexts.items()}
    queried = {support for query in (*plan.queries, *attempted_queries) for support in query.supports}
    coverage = tuple({'topicId': topic.topic_id, 'title': topic.title, 'topicKind': topic.kind,
        'status': 'candidate_available' if mapped[topic.topic_id] else
            'source_context' if not topic.query_enabled else
            'candidate_deferred_budget' if any(topic.topic_id in values for values in candidate_topics.values()) else
            'not_queried_budget' if topic.topic_id not in queried else 'no_candidate',
        'retrievedCandidateCount': sum(topic.topic_id in values for values in candidate_topics.values()),
        'evidenceKeys': list(mapped[topic.topic_id]), 'requiredRoles': list(topic.required_roles),
        'approvedRoles': [], 'consumerPaths': [list(path) for path in topic.consumer_paths]}
        for topic in topic_index.values())
    return TopicEvidenceSelection(tuple(retained), mapped, contexts, coverage)


def reuse_retained_topic_evidence(plan: EvaluationEvidencePlan, records):
    """Route retained originals to explicit function themes before authoring.

    Retrieval quotas decide which papers to retain, not whether a retained
    paper's own original can supply another named function question. Reuse the
    ordinary candidate selector without adding records or merging their prose.
    This adds no approval: both original-text and applicability reviews remain.
    """
    originals = list(records)
    selected = select_topic_evidence(plan, originals,
        per_topic_limit=len(originals), max_records=len(originals))
    candidate_supports = {}
    for record in selected.records:
        row = _plain(record)
        candidate_supports.setdefault(_original_record_identity(record), set()).update(row.get('supports', []))
    known_supports = {support for topic in plan.topics
        for support in (*topic.fact_ids, *topic.context_fact_ids, *topic.source_support_ids, topic.topic_id)}
    plan_topics = {topic.topic_id: topic for topic in plan.topics}
    result = []
    for original in originals:
        record = _plain(original)
        source = _original(record)
        supports = [support for support in record.get('supports', [])
            if support not in plan_topics or not _function_source_binding_required(plan_topics[support])
            or _function_topic_source_match(plan_topics[support], source, title=str(record.get('title') or ''))]
        candidates = candidate_supports.get(_original_record_identity(original), set())
        if source and set(supports) & known_supports:
            supports = list(dict.fromkeys([*supports, *(topic.topic_id for topic in plan.topics
                if topic.topic_id in candidates and (
                    topic.kind=='function'and _mentions(source,_FUNCTION_OBJECTS)
                        and _function_topic_source_match(topic,source,title=str(record.get('title')or''))
                    or topic.kind == 'disease' and _disease_relation_candidate(topic, source,
                        str(record.get('title') or ''))
                    or _lifestyle_guideline_match(topic,record,source)))]))
        copied = deepcopy(original)
        if isinstance(copied, EvidenceRecord):
            copied = copied.model_copy(update={'supports': supports})
        else:
            copied['supports'] = supports
        result.append(copied)
    return tuple(result)


def evaluation_evidence_coverage(plan: EvaluationEvidencePlan, selection: TopicEvidenceSelection,
                                 accepted_claims=(), *, role_resolver=None) -> list[dict[str, Any]]:
    """Credit only explicitly approved role/consumer/record bindings.

    This audits upstream approvals; it does not create one from retrieved
    candidates, or require each paper to validate a person's source score.
    Required roles are tracked separately for each consumer that owns that
    role. A summary may reuse a topic without repeating its basic mechanism.
    """
    from .evaluation_content import content_role

    topics = {topic.topic_id: topic for topic in plan.topics}
    roles, consumer_roles = {}, {}
    aliases = {'clinical_principle': 'diagnostic_principle', 'disease_definition': 'disease_meaning'}
    for claim in accepted_claims:
        if not isinstance(claim, dict) or claim.get('approved') is not True:
            continue
        topic = topics.get(claim.get('topicId'))
        if (topic is None or claim.get('role') not in topic.allowed_roles
                or claim.get('evidenceKey') not in selection.by_topic.get(topic.topic_id, ())
                or tuple(claim.get('consumerPath', ())) not in topic.consumer_paths):
            continue
        ids = claim.get('factIds')
        if not isinstance(ids, list) or not ids or not set(ids) <= set([*topic.fact_ids, *topic.context_fact_ids]):
            continue
        roles.setdefault(topic.topic_id, set()).add(claim['role'])
        consumer_roles.setdefault((topic.topic_id, tuple(claim['consumerPath'])), set()).add(claim['role'])
    result = deepcopy(list(selection.coverage))
    for row in result:
        topic = topics[row['topicId']]
        approved = roles.get(topic.topic_id, set())
        row['approvedRoles'] = sorted(approved)
        row['requiredRoles'] = list(topic.required_roles)
        consumers = []
        for path in topic.consumer_paths:
            role = (role_resolver or content_role)(path)
            role = aliases.get(role, role)
            duties = _with_consumer_duties(topic)
            optional = path in duties.optional_consumer_paths
            required = set(topic.required_roles) if path in duties.primary_consumer_paths else set()
            local_approved = consumer_roles.get((topic.topic_id, path), set())
            missing = required - local_approved
            consumers.append({'consumerPath': list(path), 'requiredRoles': sorted(required),
                'approvedRoles': sorted(local_approved), 'missingRequiredRoles': sorted(missing),
                'consumerDuty': 'optional_summary' if optional else 'primary' if required else 'source_context',
                'resolvedRole': role,
                'roleRoutingMismatch': bool(required and role not in required),
                'reasonCodes': (['optional_summary_reuses_primary_duty'] if optional else
                    ['primary_consumer_role_mismatch'] if required and role not in required else
                    ['required_role_not_approved'] if missing else []),
                'status': 'not_required' if not required else 'covered' if not missing
                    else 'partially_covered' if local_approved else 'uncovered'})
        row['consumerCoverage'] = consumers
        duties = [consumer for consumer in consumers if consumer['requiredRoles']]
        row['missingRequiredRoles'] = sorted({role for consumer in duties
            for role in consumer['missingRequiredRoles']})
        if topic.required_roles and not duties:
            row['missingRequiredRoles'] = list(topic.required_roles)
            row['primaryDutyStatus'] = 'required_consumer_missing'
            row['auditReasons'] = ['required_primary_consumer_not_materialized']
        else:
            row['primaryDutyStatus'] = 'covered' if duties and all(
                consumer['status'] == 'covered' for consumer in duties) else 'uncovered' if duties else 'source_context'
        if duties and all(consumer['status'] == 'covered' for consumer in duties):
            row['status'] = 'covered'
        elif approved:
            row['status'] = 'partially_covered'
    return result


def final_evaluation_evidence_coverage(plan: EvaluationEvidencePlan, selection: TopicEvidenceSelection,
        final_data: Any, reviewed_blocks, *, reference_keys: dict[int, str] | None = None,
        role_resolver=None, diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Audit surviving, twice-reviewed sentences; never approve candidates.

    ``reviewed_blocks`` are the in-process reviewed blocks, retaining their
    existing private sentence/path provenance. ``reference_keys`` maps their
    *current* citation numbers to stable candidate keys, before/after retention
    as appropriate. The final bibliography is independently matched by source
    identity, so final citation renumbering cannot transfer an approval.
    No model calls, callback fields, or per-measurement literature quotas.
    """
    from .evaluation_content import content_role

    raw = _plain(final_data)
    records = {_record_key(_plain(record)): _plain(record) for record in selection.records}
    numbered = {row.get('number'): key for key, row in records.items()
        if type(row.get('number')) is int}
    if reference_keys is not None:
        numbered = {int(number): key for number, key in reference_keys.items()
            if str(number).isdigit() and key in records}
    library = raw.get('appendices', {}).get('referenceLiterature', [])
    final_numbers: dict[str, list[int]] = {}
    number_counts: dict[int, int] = {}
    for row in library:
        if not isinstance(row, dict) or type(row.get('number')) is not int:
            continue
        number_counts[row['number']] = number_counts.get(row['number'], 0) + 1
        final_numbers.setdefault(_publication_identity(row), []).append(row['number'])

    # These are content-role aliases, not additional medical permissions.
    aliases = {'clinical_principle': 'diagnostic_principle', 'disease_definition': 'disease_meaning'}
    audit = diagnostics if diagnostics is not None else {}
    audit.update(reviewedBlockCount=0, acceptedClaimCount=0, rejectionCounts={}, consumerRejections=[])
    def reject(reason, path):
        audit['rejectionCounts'][reason] = audit['rejectionCounts'].get(reason, 0) + 1
        audit['consumerRejections'].append({'consumerPath': list(path) if isinstance(path, tuple) else [], 'reason': reason})
    normalize = lambda text: re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()
    accepted, seen = [], set()
    for block in reviewed_blocks:
        audit['reviewedBlockCount'] += 1
        path = getattr(block, '_reviewed_path', ())
        sentences = getattr(block, '_sentence_sources', ())
        if not isinstance(path, tuple) or not path or not isinstance(sentences, tuple) or not sentences:
            reject('private_reviewed_path_or_sentence_provenance_missing', path)
            continue
        owner = raw
        try:
            for key in path[:-1]:
                owner = owner[key]
            text = owner[path[-1]]
        except (KeyError, IndexError, TypeError):
            reject('reviewed_consumer_absent_from_final_document', path)
            continue
        if not isinstance(owner, dict) or not isinstance(text, str):
            reject('reviewed_consumer_has_no_final_text', path)
            continue
        serialized = _plain(block)
        if not serialized.get('evidenceClaims'):
            reject('reviewed_block_has_no_evidence_claims', path)
        if (any(not isinstance(row, tuple) or len(row) != 4 or not isinstance(row[0], str)
                or not isinstance(row[1], frozenset) or type(row[2]) is not bool
                or type(row[3]) is not bool for row in sentences)
                or '\n'.join(row[0] for row in sentences) != serialized.get('text')):
            reject('private_sentence_provenance_does_not_match_reviewed_text', path)
            continue
        role = (role_resolver or content_role)(path)
        role = aliases.get(role, role)
        general_proofs = getattr(block, '_general_explanations', frozenset())
        first = sentences[0]
        source_first_ids = (set(first[1]) if first[3] and not first[2]
            and set(first[1]) <= set(serialized.get('factIds', [])) else set())
        final_refs = owner.get('evidenceRefs', [])
        for number, joined_claims in serialized.get('evidenceClaims', {}).items():
            key = numbered.get(int(number)) if str(number).isdigit() else None
            record = records.get(key)
            if record is None or not isinstance(joined_claims, str):
                reject('reviewed_reference_has_no_retained_original', path)
                continue
            matches = final_numbers.get(_publication_identity(record), [])
            if (len(matches) != 1 or number_counts.get(matches[0]) != 1
                    or matches[0] not in final_refs):
                reject('reviewed_publication_or_final_reference_mapping_missing', path)
                continue
            quote = serialized.get('evidenceQuotes', {}).get(str(number))
            ids = serialized.get('evidenceFactIds', {}).get(str(number))
            if (not isinstance(quote, str) or not quote.strip() or not isinstance(ids, list)
                    or not all(isinstance(fid, str) for fid in ids)):
                reject('reviewed_quote_or_fact_binding_missing', path)
                continue
            original = normalize(_original(record))
            excerpts = [normalize(line) for line in quote.splitlines() if line.strip()]
            if not original or not excerpts or any(excerpt not in original for excerpt in excerpts):
                reject('reviewed_quote_not_in_retained_original', path)
                continue
            for claim in (line.strip() for line in joined_claims.splitlines() if line.strip()):
                general = (not ids and role in {'general_function', 'disease_meaning'}
                    and isinstance(general_proofs, frozenset) and (claim, quote) in general_proofs
                    and bool(source_first_ids))
                # Block bindings are a per-reference union across sentences;
                # intersect with this already-reviewed sentence's own facts.
                provenance = [row for row in sentences if row[0] == claim and row[2] and not row[3]
                    and (set(ids) & row[1] or general and not row[1])]
                if not provenance or normalize(claim) not in normalize(text):
                    reject('reviewed_claim_or_sentence_not_surviving_publication', path)
                    continue
                claim_ids = sorted(set(ids) & set().union(*(row[1] for row in provenance)))
                for topic in plan.topics:
                    # The source-first facts locate the observed domain. They
                    # do not imply that this paper validates a person's score.
                    bound_ids = sorted(source_first_ids & set(topic.fact_ids)) if general else claim_ids
                    if (path not in topic.consumer_paths or role not in topic.allowed_roles
                            or key not in selection.by_topic.get(topic.topic_id, ())
                            or not set(bound_ids) & set(topic.fact_ids)
                            or not set(bound_ids) <= set([*topic.fact_ids, *topic.context_fact_ids])):
                        continue
                    identity = (topic.topic_id, role, key, path, claim)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    accepted.append(dict(topicId=topic.topic_id, role=role, approved=True,
                        evidenceKey=key, factIds=bound_ids, consumerPath=list(path)))
    audit['acceptedClaimCount'] = len(accepted)
    coverage = evaluation_evidence_coverage(plan, selection, accepted, role_resolver=role_resolver)
    topic_lookup = {topic.topic_id: topic for topic in plan.topics}
    for row in coverage:
        claims = [claim for claim in accepted if claim['topicId'] == row['topicId']]
        row['approvedClaimCount'] = len(claims)
        row['approvedEvidenceKeys'] = sorted({claim['evidenceKey'] for claim in claims})
        row['approvedConsumerPaths'] = [list(path) for path in dict.fromkeys(
            tuple(claim['consumerPath']) for claim in claims)]
        row['auditReasons'] = sorted({*row.get('auditReasons', []), *(entry['reason'] for entry in audit['consumerRejections']
            if tuple(entry['consumerPath']) in topic_lookup[row['topicId']].consumer_paths)})
        if not claims and row.get('retrievedCandidateCount', 0):
            row['auditReasons'].append('no_surviving_twice_reviewed_bound_claim')
        if claims and row['status'] != 'covered':
            row['status'] = 'partially_covered'
    return coverage
