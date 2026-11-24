"""Multi-source evidence recall for the seven-chapter evaluate report.

Guideline/literature library (EviMed), PubMed and allow-listed web pages are
queried for the same report topics and merged into one candidate pool. Queries
are planned per topic in Chinese and English (a group label such as
"糖代谢风险（空腹C肽偏高…）" is not a search term), every abnormal taxon is
searched rather than the first four, and each structure index has its own
question.

Candidate selection is deterministic: a publication is linked to a topic only
when its literal text names the topic's object, and the best few publications
per topic are kept by source grade and recency. Whether a cited passage really
supports a sentence for this person is decided later by chapter review.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass
from dataclasses import replace
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from utils.log import getLogger

from .evaluation_catalog import FIXED_METRIC_ALIASES
from .evaluation_originals import evaluation_original_passages
from .evaluation_originals import evaluation_original_text
from .evaluation_originals import evaluation_publication_identity
from .evaluation_originals import reconcile_evaluation_guideline_representations
from .evaluation_report_evidence import _MANAGEMENT
from .evaluation_report_evidence import ReportEvidenceBundle
from .evaluation_report_evidence import _age_group
from .evaluation_report_evidence import _compile_bundle
from .evaluation_report_evidence import _emit
from .evaluation_report_evidence import _enabled
from .evaluation_report_evidence import _get
from .evaluation_report_evidence import _group_records
from .evaluation_report_evidence import _key
from .evaluation_report_evidence import _merge_pool
from .evaluation_report_evidence import _retrieve
from .evaluation_report_evidence import _strings
from .evaluation_report_evidence import _subject_scope_matches
from .evaluation_report_evidence import _unique
from .evaluation_report_evidence import build_report_evidence_plan
from .evaluation_report_evidence import report_target_accepts_external_evidence
from .evaluation_report_evidence import supports_human_gut_neurotransmitter_synthesis
from .evaluation_report_evidence import veterinary_host
from .evaluation_source_scope import source_candidate_scope_errors
from .evidence import EvidenceQuery
from .web_source_policy import evidence_document_title
from .web_source_policy import evidence_grade
from .web_source_policy import is_figure_or_table_caption

logger = getLogger(__name__)

_CLINICAL_KINDS = frozenset({'clinical', 'goal', 'clinical_decision'})
_PLANNED_KINDS = _CLINICAL_KINDS | {'patient_management', 'behavior'}

# One literature question per measured structure index. These are names of
# published indices and concepts, never patient values or thresholds.
_STRUCTURE_QUESTIONS = {
    'speciesCount': ('gut microbiome species richness health', ('species richness', 'richness', '物种丰富度')),
    'diversity': (
        'gut microbiome alpha diversity Shannon index health outcomes',
        ('Shannon', 'alpha diversity', 'α多样性', '菌群多样性'),
    ),
    'gutAge': (
        'gut microbiome age prediction biological aging',
        ('microbiome age', 'microbial age', 'gut age', '肠道年龄'),
    ),
    'gmwi2': (
        'Gut Microbiome Wellness Index 2 enhances health status prediction from gut microbiome taxonomic profiles',
        ('GMWI2', 'Gut Microbiome Wellness Index 2'),
    ),
    'beRatio': (
        'Bifidobacterium Enterobacteriaceae ratio colonization resistance',
        ('B/E', 'Bifidobacterium/Enterobacteriaceae', 'colonization resistance', '定植抗力'),
    ),
    'fbRatio': (
        'Firmicutes Bacteroidetes ratio obesity metabolic health',
        (
            'Firmicutes/Bacteroidetes',
            'F/B ratio',
            'Firmicutes to Bacteroidetes',
            'Firmicutes Bacteroidetes ratio',
            '厚壁菌门/拟杆菌门',
        ),
    ),
    'enterotype': ('enterotype Bacteroides Prevotella long-term diet', ('enterotype', 'enterotypes', '肠型')),
    'balanceStatus': ('gut microbiota dysbiosis definition health', ('dysbiosis', '菌群失调', '微生态失衡')),
    'keystoneBacteria': ('keystone taxa human gut microbiome', ('keystone', '核心菌', '基石菌')),
    'barrierRelatedBacteria': (
        'gut microbiota intestinal barrier mucus layer permeability',
        ('intestinal barrier', 'gut barrier', 'mucus layer', 'intestinal permeability', '肠道屏障', '肠黏膜屏障'),
    ),
    'beneficialBacteria': (
        'Lactobacillus Bifidobacterium gut abundance health probiotics',
        ('Lactobacillus', 'Bifidobacterium', 'probiotic', '益生菌', '乳杆菌'),
    ),
    'scfaProducerBacteria': (
        'butyrate-producing bacteria gut health',
        ('butyrate-producing', 'butyrate producers', 'butyrate producer', '产丁酸'),
    ),
    'pathogenicFungi': (
        'gut mycobiome fungi healthy adults stool sequencing',
        ('mycobiome', 'fungal', 'fungi', '真菌'),
    ),
    'antiInflammatoryCapacity': (
        'gut microbiota anti-inflammatory butyrate regulatory T cells',
        ('anti-inflammatory', 'regulatory T', '抗炎'),
    ),
    'gutImmunity': (
        'gut microbiota mucosal immunity IgA',
        ('mucosal immunity', 'immune homeostasis', '黏膜免疫', '肠道免疫'),
    ),
    'butyrate': ('butyrate gut microbiota health effects', ('butyrate', '丁酸')),
    'propionate': ('propionate gut microbiota health effects', ('propionate', '丙酸')),
    'acetate': ('acetate gut microbiota health effects', ('acetate', '乙酸')),
}
_NOT_EVIDENCE = re.compile(
    r'case report|case series|病例报告|个案|第\s*\d+\s*例|\d+例报告|erratum|corrigendum|retract', re.I
)
_DIRECTIVE = re.compile(
    r'\b(?:recommend\w*|should|suggest\w*|advis\w*|is indicated|are indicated|must|'
    r'requires? follow[- ]?up)\b|建议|推荐|应当|应予|需要|'
    # A bare 应/宜 counts only before an action, never inside 反应/对应/适宜 and similar words.
    r'(?<![反对相响供效适顺呼感])应(?=在|及时|尽快|尽早|定期|立即|进行|完成|接受|行|做|复查|随访|避免|控制|限制|减少|'
    r'增加|戒|保持|每|至少|于|根据|结合|由|首选|选择|采用|考虑|评估|筛查)|'
    r'(?<![适便权])宜(?=在|进行|选择|采用|控制|限制|减少|增加|避免|保持|每|至少|首选|定期)',
    re.I,
)
_UNFINISHED = re.compile(r'\b(?:study protocol|preprint|we hypothesi[sz]e|trial design)\b|研究方案|预印本', re.I)
_PREPRINT_URL = re.compile(r'/article/(?:ppr|medrxiv|biorxiv)/|\b(?:medrxiv|biorxiv)\.org', re.I)
_PROGNOSTIC_ENDPOINT = re.compile(
    r'\b(?:prognos\w*|survival|mortality|disease progression|treatment response)\b|预后|生存|死亡率|疗效', re.I,
)
_LAB_FINDING = re.compile(r'偏[高低]|升高|降低|\b(?:elevated|decreased|low|high)\b', re.I)
_POSITIVE_TEST = re.compile(
    r'\b(?:positive|abnormal|reactive)\b[^.!?。；]{0,90}\b(?:test|screen\w*|result|FIT|FOBT)\b|'
    r'\b(?:test|screen\w*|result|FIT|FOBT)\b[^.!?。；]{0,75}\b(?:positive|reactive)\b|'
    r'(?:检测|试验|筛查|检查|测定|标本)[^。；]{0,16}(?:弱阳性|阳性)|(?:弱阳性|阳性)(?:结果|筛查|检测)',
    re.I,
)
_DIAGNOSTIC_PATH = re.compile(
    r'\b(?:diagnos\w*|confirm\w*|evaluat\w*|work[- ]?up|follow[- ]?up|indicat\w*|referr\w*|'
    r'endoscop\w*|colonoscop\w*)\b|'
    r'查因|诊断|进一步检查|确诊|转诊|镜检查|评估|复核', re.I,
)
_POST_TEST_STEP = re.compile(
    r'\b(?:diagnostic|confirm\w*|work[- ]?up|follow[- ]?up|referr\w*|repeat\w*|endoscop\w*|colonoscop\w*)\b|'
    r'查因|确诊|进一步(?:检查|评估|诊断)|转诊|内镜|肠镜|复查|复核', re.I,
)
_STOOL_TEST = re.compile(
    r'\b(?:stool[- ]based (?:screening|tests?)|(?:fecal|faecal) (?:occult blood|immunochemical)|'
    r'FIT|[a-z]?FOBT)\b|(?:粪便|大便|便)隐血|(?:粪便|大便)免疫化学|粪便.{0,3}筛查', re.I,
)
_CEREBROVASCULAR_EXAM = re.compile(
    r'脑血管功能检测|脑血流速度|脑血管弹性|脑血管自身调节|'
    r'\bcerebr(?:al|ovascular) (?:blood flow velocity|elasticity|autoregulation)\b', re.I,
)
_EXAM_PROTOCOLS = (
    re.compile(r'经颅(?:彩色)?多普勒|\b(?:TCD|transcranial (?:color[- ]coded )?Doppler)\b', re.I),
    re.compile(r'(?:主动)?直立试验|主动站立|倾斜(?:床)?试验|'
        r'\b(?:active stand\w*|head[- ]up tilt|tilt[- ]table)\b', re.I),
)
_FATTY_LIVER = re.compile(r'脂肪(?:性)?肝(?:病)?|肝脂肪变|\b(?:fatty liver|hepatic steatosis|[MN]AFLD|MASLD)\b', re.I)
_INITIAL_SCREEN = re.compile(r'筛查|初筛|\b(?:screen(?:ed|ing)?|case[- ]finding)\b', re.I)
_OTHER_SCREENING_ENDPOINT = re.compile(
    r'(?:筛查|初筛)(?:有无|是否|潜在)?\s*(?:肝?纤维化|肝硬化|肝癌)|'
    r'\bscreen(?:ing)? for (?:advanced )?(?:fibrosis|cirrhosis|hepatocellular carcinoma)\b', re.I,
)
_UNCONFIRMED_FINDING = re.compile(r'未见|未发现|无|可能|疑似|待排|风险|筛查|\b(?:no|possible|suspected|risk)\b', re.I)
_MICROBIAL_PROCESS = re.compile(
    r'\b(?:synthesi[sz]\w*|biosynth\w*|metaboli[sz]\w*|ferment\w*|conver[ts]\w*|degrad\w*|'
    r'hydroly[sz]\w*|produc\w*|substrates?|energ\w*|utili[sz]\w*|activat\w*|modulat\w*|'
    r'signall?ing|transport\w*|absorp\w*|cross[- ]feed\w*)\b|生成|合成|水解|分解|发酵|转化|降解|'
    r'底物|供能|代谢|吸收|调节|交叉喂养',
    re.I,
)
_MICROBIAL_ACTOR = re.compile(
    r'\b(?:gut|colonic|intestinal|colon|fecal|faecal|microbial|microbiota|microbiome|'
    r'bacteri\w*|microbes?|strains?|co[- ]?cultur\w*)\b|肠道|结肠|微生物|细菌|菌群|共培养',
    re.I,
)
_MECHANISTIC_RELATION = re.compile(
    r'\b(?:synthesi[sz]e[sd]?|ferments?|converted?|converts|degrades?|metaboli[sz]e[sd]?|produc(?:e[sd]?|ing)|'
    r'hydroly[sz]e[sd]?|utili[sz]e[sd]?|requires?|depend(?:s|ed)? on|used as (?:an? )?(?:substrate|energy source)|'
    r'modulates?|activates?|transforms?|cross[- ]feeds?)\b|'
    r'(?:能够|可以|可|经|通过|由)(?:[^，。；]{0,24})(?:生成|合成|发酵|转化|代谢|降解|供能|利用|调节)', re.I,
)
_GUT_SITE = re.compile(r'\b(?:gut|colonic|intestinal|colon|fecal|faecal|gastrointestinal)\b|肠道|结肠|粪便', re.I)
_EXPLICIT_SCOPE = re.compile(
    r'\b(?:patients?|people|participants?|subjects?|individuals?|adults?|children|women|men)\s+'
    r'(?:(?:diagnosed|living)\s+)?(?:with|having|positive for|undergoing|receiving)\b|'
    r'纳入|入组|招募|受试者为|研究对象为|突变|\b(?:genotype|mutation|study protocol)\b', re.I,
)
_HUMAN_SETTING = re.compile(
    r'\b(?:(?:healthy )?adults?|human(?:s| subjects| participants)?|healthy volunteers|human gut|human colon\w*)\b'
    r'|成人|人体|人类|健康志愿者',
    re.I,
)
_METHOD_ONLY = re.compile(
    r'\b(?:we (?:aimed|wanted|sought|designed)|we hypothesi[sz]e\w*|study was designed|'
    r'the aim|the objective|to explore whether|methodology|further studies|future studies)\b|'
    r'旨在|研究目的|假设|拟验证|需要进一步研究', re.I,
)
_MEASUREMENT_METHOD = re.compile(
    r'\b(?:we (?:assessed|evaluated|measured|investigated|analy[sz]ed|examined)|'
    r'(?:was|were) (?:assessed|evaluated|measured|analy[sz]ed|determined|examined)|'
    r'experiments? (?:were|was) carried out)\b|(?:本研究|我们)(?:评估|测定|测量|研究)|进行了.{0,12}(?:测定|分析)', re.I,
)
_EXPERIMENTAL_SETTING = re.compile(
    r'\b(?:in vitro|in silico|co[- ]?cultures?|synthetic communit\w*|cultured strains?|mouse model|murine)\b|'
    r'实验模型|体外|共培养|计算模拟', re.I,
)
_ACTUAL_RESULT = re.compile(
    r'\b(?:we (?:found|observed|demonstrated|showed|detected|provide evidence)|'
    r'(?:was|were) (?:observed|detected|demonstrated|increased|reduced|seen|auxotrophic|prototrophic)|'
    r'(?:results?|data|experiments?|this|study) (?:showed|indicated|demonstrated|revealed)|'
    r'increased|decreased|reduced|enhanced|stimulated|inhibited|associated with|correlated with|'
    r'auxotrophy was widespread|(?:had|has|have) no detectable effect|(?:did|does) not predict)\b|'
    r'我们发现|研究显示|结果表明|实验发现|观察到|测得|实验证明|提供证据|显著(?:升高|降低|增加|减少)', re.I,
)
_PRODUCER_LABEL = re.compile(
    r'\b(?:butyrate|propionate|acetate|butyric acid|propionic acid|acetic acid)[-\s]+produc(?:ing|ers?)\b|'
    r'产(?:丁酸|丙酸|乙酸)(?:的)?(?:菌群|菌株|细菌|菌|微生物)', re.I,
)
_SCFA_DERIVATIVE = re.compile(
    r'\b(?:indole(?:[- ]?\d+)?[- ]?propion(?:ate|ic acid)|indolepropion(?:ate|ic acid)|'
    r'(?:acetyl|propionyl|butyryl)[ -]?(?:coenzyme A|CoA)|aminobutyr(?:ate|ic acid))\b|'
    r'吲哚(?:[-－]?3)?[-－]?丙酸|氨基丁酸',
    re.I,
)
_PLANT_ENVIRONMENT = re.compile(
    r'\b(?:rhizosphere|plant roots?|root exudates?|phyllosphere)\b|根际|植物根系|根系分泌', re.I,
)
_CHOLESTASIS = re.compile(r'\b(?:cholestasis|cholestatic)\b|胆汁淤积', re.I)
_SYNTHESIS = re.compile(
    r'\b(?:biosynth\w*|synthesi[sz]\w*|synthesis|production|produc(?:e[sd]?|ing)|generat\w*)\b|合成|生成|产生', re.I,
)
_BREAKDOWN = re.compile(r'\b(?:catabolism|cataboliz\w*|degrad\w*|breakdown|metaboli[sz]\w*)\b|分解|降解|代谢', re.I)
_DANGLING_TITLE = re.compile(r'[,，;；]$|^\(?[A-Z]\d+\)\s|\bsuch as?\b.+(?:\bor\b|\\gamma)', re.I)
_TITLE_FRAGMENT = re.compile(r'^In the (?:human )?(?:gut|colon|intestine|rhizosphere),\s+[a-z]')
_TREATMENT_QUESTION = re.compile(
    r'\b(?:treating|treatment of|management of|therapy for|interventions? for|in [^:]{1,35} management)\b|'
    r'(?:治疗|干预)(?:肥胖|[\u4e00-\u9fff]{2,14}(?:病|症))', re.I,
)
_TITLE_SCOPE = re.compile(
    r'\b(?:acute|chronic|advanced|moderate|severe)[ -]\w+ (?:injury|failure|disease|infection|disorder)\b|'
    r'(?:重症|恶性|慢性|急性)[\u4e00-\u9fff]{1,12}(?:病|症|损伤)', re.I,
)

# Public section 4.4 duties and their literal biological search objects.
# These are assay families, without source values, directions or case logic.
_FUNCTION_TOPICS = {
    'carbohydrate': (
        'human colonic microbial fermentation galactose starch sucrose',
        ('carbohydrate fermentation', 'galactose', 'starch', 'sucrose', '半乳糖', '淀粉', '蔗糖'),
    ),
    'scfa': (
        'human gut microbial short chain fatty acids butyrate propionate acetate colonocyte',
        ('short chain fatty acid', 'butyrate', 'propionate', 'acetate', '丁酸', '丙酸', '乙酸'),
    ),
    'lipid': (
        'human gut microbial secondary bile acid sphingolipid metabolism',
        ('secondary bile acid', 'sphingolipid', 'glyceride', 'steroid', '次级胆汁酸', '鞘脂', '甘油酯', '类固醇'),
    ),
    'neurotransmitter': (
        'human gut bacteria produce gamma aminobutyric acid serotonin dopamine biosynthesis',
        ('GABA', 'serotonin', 'dopamine', 'γ-氨基丁酸', '血清素', '多巴胺'),
    ),
    'aminoAcid': (
        'human gut bacteria de novo amino acid biosynthesis tryptophan histidine microbial synthesis',
        ('amino acid biosynthesis', 'tryptophan', 'histidine', 'branched-chain amino acid', '色氨酸', '组氨酸'),
    ),
    'vitamin': (
        'human gut bacteria vitamin biosynthesis vitamin B6 biotin folate',
        ('vitamin biosynthesis', 'vitamin B6', 'biotin', 'folate', '维生素B6', '生物素', '叶酸'),
    ),
    'nucleotidePlant': (
        'human gut bacterial biosynthesis purines pyrimidines carotenoids flavonoids',
        ('purine', 'pyrimidine', 'flavonoid', 'carotenoid', '嘌呤', '嘧啶', '类黄酮', '类胡萝卜素'),
    ),
    'harmfulMetabolism': (
        'human gut bacteria xenobiotic biodegradation lipopolysaccharide homocysteine microbial production',
        ('xenobiotic', 'lipopolysaccharide', 'homocysteine', 'polycyclic aromatic hydrocarbon',
         '脂多糖', '同型半胱氨酸'),
    ),
}
_FUNCTION_MEMBERS = (
    ('galactose', '半乳糖'), ('starch', '淀粉'), ('sucrose', '蔗糖'),
    ('butyrate', 'butyric acid', '丁酸'), ('propionate', 'propionic acid', '丙酸'),
    ('acetate', 'acetic acid', '乙酸'), ('secondary bile acid', 'secondary bile acids', '次级胆汁酸'),
    ('steroid', 'steroids', '类固醇'), ('vitamin B6', 'pyridoxine', '维生素B6'),
    ('vitamin B1', 'thiamine', 'thiamin', '维生素B1'), ('vitamin B2', 'riboflavin', '维生素B2'),
    ('vitamin B3', 'niacin', '维生素B3'), ('vitamin B5', 'pantothenate', '维生素B5'),
    ('biotin', 'vitamin B7', '生物素', '维生素B7'), ('pyrimidine', 'pyrimidines', '嘧啶'),
    ('lipopolysaccharide', 'lipopolysaccharides', 'LPS', '脂多糖'), ('homocysteine', '同型半胱氨酸'),
    ('polycyclic aromatic hydrocarbon', 'polycyclic aromatic hydrocarbons', '多环芳烃'),
    *FIXED_METRIC_ALIASES.values(),
)
_FUNCTION_NAME_SUFFIX = re.compile(r'(?:合成功能|合成能力|生成潜能|代谢潜能|代谢|评分|预测)$')
_STOP = frozenset(
    {
        'gut',
        'human',
        'microbial',
        'microbiome',
        'microbiota',
        'review',
        'and',
        'the',
        'for',
        'with',
        'of',
        'in',
        'to',
        'health',
        'adult',
        'adults',
        'a',
        'an',
        'on',
    }
)


@dataclass(frozen=True)
class RecallLimits:
    max_queries: int = 280
    concurrency: int = 8
    public_concurrency: int = 2
    max_publications: int = 90
    clinical_per_topic: int = 3
    default_per_topic: int = 2
    planning_timeout_seconds: float = 90.0


def _words(term: str) -> list[str]:
    return [word for word in re.findall(r'[a-z0-9]+(?:[-’\'][a-z0-9]+)*', term.casefold()) if word not in _STOP]


@lru_cache(maxsize=2048)
def _term_matcher(term, exact):
    term = str(term or '').strip()
    if len(term) < 2:
        return None
    if re.search(r'[一-鿿]', term):
        anchor = term.casefold()
        # A short-chain fatty acid is chemically distinct from aminobutyric acid.
        pattern = re.compile(r'(?<!氨基)丁酸') if anchor == '丁酸' else None
        return anchor, pattern, ()
    words = _words(term)
    if not words:
        return None
    phrase = r'[^a-z0-9]+'.join(re.escape(word) for word in words)
    phrase_pattern = re.compile(r'(?<![a-z0-9])' + phrase + r'(?![a-z0-9])')
    individual = (
        tuple(re.compile(r'(?<![a-z0-9])' + re.escape(word) + r's?(?![a-z0-9])') for word in words)
        if not exact and len(words) > 1 else ()
    )
    return words[0], phrase_pattern, individual


def _matches(folded, matcher):
    if matcher is None:
        return False
    anchor, phrase, individual = matcher
    return anchor in folded and (
        phrase is None or bool(phrase.search(folded)) or bool(individual and all(
            pattern.search(folded) for pattern in individual
        ))
    )


def _term_in(sentence: str, term: str, *, exact: bool = False) -> bool:
    """Literal object naming. Multi-word terms may be separated inside one sentence."""
    return _matches(sentence.casefold(), _term_matcher(term, exact))


def _sentences(text: str) -> list[str]:
    boundary = r'(?<!\b[A-Z]\.)(?<!\bsp\.)(?<!\bspp\.)(?<!\bssp\.)(?<=[.!?;])\s+|(?<=[。！？；])|\n+'
    return [piece.strip() for piece in re.split(boundary, text) if piece.strip()]


def topic_terms(topic) -> list[str]:
    """Literal names of the topic's object, Chinese and English."""
    return _unique(
        term
        for term in [*topic.get('objectTerms', ()), *topic.get('aliases', ()), topic.get('title')]
        if isinstance(term, str) and len(term.strip()) >= 2 and len(term) <= 80
    )


def _published_function_topics(blueprint):
    facts = {_get(row, 'factId', 'fact_id'): row for row in _get(blueprint, 'facts', default=()) or ()}
    for chapter in _get(blueprint, 'chapters', default=()) or ():
        for target in _get(chapter, 'targets', default=()) or ():
            if not report_target_accepts_external_evidence(target):
                continue
            owner = _get(target, 'owner', default={}) or {}
            scope = str(_get(owner, 'contentScopeCode', default='') or '')
            family = scope.removeprefix('function.')
            if not scope.startswith('function.') or family not in _FUNCTION_TOPICS:
                continue
            ids = [fid for fid in _strings(_get(owner, 'primaryFactIds', default=()))
                if _get(facts.get(fid), 'kind') == 'function'
                and _get(facts[fid], 'value') not in (None, '')]
            if not ids or not target.get('publicationSlots'):
                continue
            query, objects = _FUNCTION_TOPICS[family]
            names = _strings(_get(facts[fid], 'name') for fid in ids)
            # Apply the bilingual search vocabulary only to components present
            # in this owner. The query may recall other members of a pathway.
            keys = {_key(part) for name in names for part in re.split(r'和|与|及', name)}
            keys.update(_FUNCTION_NAME_SUFFIX.sub('', key) for key in tuple(keys))
            owned = _unique(alias for group in _FUNCTION_MEMBERS
                if any(_key(label) in keys for label in group)
                for alias in group)
            anchor_terms = _unique([*(term for term in objects if term in owned), *owned, *names])
            yield {
                'topicId': f'report-function:{target["id"]}',
                'kind': 'pathway',
                'title': str(_get(owner, 'name', default=family)),
                'aliases': list(names),
                'sourceFactIds': ids,
                'chapterCodes': [chapter['code']],
                'targetIds': [target['id']],
                'analysisCellTargetIds': [target['id']],
                'allowedRoles': ['interpretation'],
                'priority': 3,
                'retrievalMode': 'external_meaning',
                'questionIntent': 'general_mechanism',
                'functionFamily': family,
                'queryTerms': [query],
                'objectTerms': anchor_terms,
            }


def _prepare_topics(plan, blueprint):
    """Merge duplicate taxon topics and split the structure topic per index."""
    taxa, others = {}, []
    for topic in plan:
        if topic['kind'] != 'abnormal_flora':
            others.append(topic)
            continue
        latin = next((alias for alias in topic['aliases'] if re.search(r'[A-Za-z]{3}', alias)), None)
        keys = {_key(alias) for alias in topic['aliases']}
        match = next((row for row in taxa.values() if keys & row['_keys']), None)
        if match is None:
            if latin is None:
                others.append(topic)  # a panel label such as 有益菌, handled as a structure question
                continue
            topic = dict(topic, _keys=keys, objectTerms=[latin])
            taxa[_key(latin)] = topic
            continue
        match['_keys'] |= keys
        for field in ('aliases', 'sourceFactIds', 'chapterCodes', 'targetIds', 'allowedRoles', 'analysisCellTargetIds'):
            match[field] = _unique([*match.get(field, ()), *topic.get(field, ())])
    panel_taxa, kept = [], []
    for topic in others:
        if topic['kind'] == 'abnormal_flora':
            match = next((row for row in taxa.values() if {_key(a) for a in topic['aliases']} & row['_keys']), None)
            if match is not None:
                for field in (
                    'aliases',
                    'sourceFactIds',
                    'chapterCodes',
                    'targetIds',
                    'allowedRoles',
                    'analysisCellTargetIds',
                ):
                    match[field] = _unique([*match.get(field, ()), *topic.get(field, ())])
                continue
            panel_taxa.append(topic)
        kept.append(topic)
    for topic in taxa.values():
        topic.pop('_keys', None)
    targets = {
        str(_get(target, 'id', default='')): target
        for chapter in _get(blueprint, 'chapters', default=()) or ()
        for target in _get(chapter, 'targets', default=()) or ()
    }
    result = []
    for topic in kept:
        if topic['kind'] not in {'microbiome', 'abnormal_flora'}:
            result.append(topic)
            continue
        # Split by the owning row of every target: one index, one question.
        by_code = {}
        for target_id in topic['targetIds']:
            owner = _get(targets.get(target_id), 'owner', default={}) or {}
            code = str(_get(owner, 'code', default='') or '')
            if code == 'structure.gmwi2':
                by_code.setdefault('gmwi2', []).append((target_id, owner))
            elif code in _STRUCTURE_QUESTIONS and not _get(owner, 'aggregate'):
                by_code.setdefault(code, []).append((target_id, owner))
        if not by_code:
            result.append(dict(topic, kind='microbiome'))
            continue
        for code, rows in by_code.items():
            question, terms = _STRUCTURE_QUESTIONS[code]
            name = str(_get(rows[0][1], 'name', default=code))
            ids = _unique(fid for _, owner in rows for fid in _strings(_get(owner, 'primaryFactIds', default=())))
            existing = next((row for row in result if row.get('structureCode') == code), None)
            if existing is not None:
                existing['targetIds'] = _unique([*existing['targetIds'], *(tid for tid, _ in rows)])
                existing['analysisCellTargetIds'] = list(existing['targetIds'])
                existing['sourceFactIds'] = _unique([*existing['sourceFactIds'], *ids])
                continue
            result.append(
                {
                    **topic,
                    'topicId': topic['topicId'] + '-' + code,
                    'kind': 'microbiome',
                    'title': name,
                    'aliases': _unique([name, *terms]),
                    'objectTerms': list(terms),
                    'queryTerms': [question],
                    'structureCode': code,
                    'targetIds': [tid for tid, _ in rows],
                    'analysisCellTargetIds': [tid for tid, _ in rows],
                    'sourceFactIds': ids or list(topic['sourceFactIds']),
                }
            )
    functions = list(_published_function_topics(blueprint))
    function_targets = {tid for topic in functions for tid in topic['targetIds']}
    data = _get(blueprint, 'data')
    content_plan = _get(data, '_assessment_content_plan', default={}) or {}
    workup_owners = {code for cluster in content_plan.get('clusters', ())
        if cluster.get('priority') == 'P0' for code in cluster.get('problemCodes', ())}
    for topic in result:
        positive_test = bool(_POSITIVE_TEST.search(topic['title']))
        if topic['kind'] in _PLANNED_KINDS:
            bridges = [tid for tid in topic['targetIds'] if tid in function_targets]
            if bridges:
                topic['clinicalBridgeTargetIds'] = bridges
        if topic['kind'] in _CLINICAL_KINDS | {'patient_management'} and (
            topic.get('ownerCode') in workup_owners or positive_test
        ):
            topic['questionIntent'] = 'diagnostic_workup'
            topic['requiresPositiveTest'] = positive_test
        if topic['kind'] in {'nutrition', 'pathway', 'microbiome'}:
            for field in ('targetIds', 'analysisCellTargetIds'):
                topic[field] = [tid for tid in topic.get(field, ()) if tid not in function_targets]
    return [*functions, *result, *taxa.values()]


async def _plan_queries(client, topics, *, blueprint, subject, deadline, audit, timeout):
    """One model call turns problem labels into bilingual search questions."""
    planned = [topic for topic in topics if topic['kind'] in _PLANNED_KINDS]
    if not planned or not callable(getattr(client, 'generate', None)) or time.monotonic() >= deadline:
        return
    facts = {_get(fact, 'factId', 'fact_id'): fact for fact in _get(blueprint, 'facts', default=()) or ()}
    rows = []
    for index, topic in enumerate(planned, 1):
        witnesses = []
        for fid in topic['sourceFactIds'][:8]:
            fact = facts.get(fid)
            if fact is None or _get(fact, 'kind') not in {'clinical', 'self_reported'}:
                continue
            text = ' '.join(
                str(value)
                for value in (_get(fact, 'name'), _get(fact, 'value'), _get(fact, 'unit'), _get(fact, 'status'))
                if value
            )
            witnesses.append(text[:160])
        rows.append({'id': f'Q{index:02d}', 'kind': topic['kind'], 'label': topic['title'], 'records': witnesses[:6]})
    by_id = {row['id']: topic for row, topic in zip(rows, planned, strict=True)}
    payload = {
        'task': 'plan_report_evidence_queries',
        'instructions': [
            '为每个健康问题或行为写文献/指南检索问题。label是报告里的分组名，常含括号说明，不能直接当检索词；请还原成规范的医学概念。',
            'zh给1至2条中文检索式（疾病规范名+“指南”或“共识”或具体问题）。'
            'en给1至2条英文检索式（规范英文术语+guideline/management/screening等）。每条不超过80个字符。',
            'objectTerms给3至8个会出现在文献标题和正文中的规范名称与常用同义词，中英文都要。'
            '对已经阳性或异常的检查，首选初筛阳性后的处理，不用平均风险人群筛查起始年龄代替查因。'
            'objectTerms保留原实际检查名称与结果方向，只写名称，不写句子。'
            '原始记录没有明确检查设备、方法或试验条件时，不把具体设备、方法或试验条件当成同义词。',
            '检索问题要对应这个人此刻真正需要回答的医学问题：阳性筛查后的查因路径、已确诊慢病的管理目标、未确诊风险的筛查与预防、偶然影像发现的随访间隔、生活方式的推荐量。',
            '不新增病名，不把风险写成确诊；records只是帮助你理解label的原始记录。',
            '只返回JSON：{"topics":[{"id":"Q01","zh":["..."],"en":["..."],"objectTerms":["..."]}]}',
        ],
        'subject': {'age': _get(subject, 'age'), 'sex': _get(subject, 'sex', 'gender')},
        'topics': rows,
    }
    try:
        async with asyncio.timeout(max(0.001, min(timeout, deadline - time.monotonic()))):
            response = await client.generate(payload)
        answered = 0
        for answer in response.get('topics', ()) if isinstance(response, dict) else ():
            topic = by_id.get(answer.get('id')) if isinstance(answer, dict) else None
            if topic is None:
                continue
            zh = [term.strip()[:100] for term in _strings(answer.get('zh')) if re.search(r'[一-鿿]', term)][:2]
            en = [term.strip()[:100] for term in _strings(answer.get('en')) if re.search(r'[A-Za-z]{3}', term)][:2]
            terms = [term.strip() for term in _strings(answer.get('objectTerms')) if 2 <= len(term.strip()) <= 60][:8]
            if zh or en:
                topic['plannedQueries'] = {'zh': zh, 'en': en}
                answered += 1
            if terms:
                topic['objectTerms'] = _unique([*terms, *topic.get('objectTerms', ())])
        await _emit(audit, 'evidence_query_planning', {'topicCount': len(planned), 'plannedCount': answered})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        await _emit(
            audit,
            'evidence_query_planning_unavailable',
            {'topicCount': len(planned), 'errorType': type(error).__name__},
        )


def _queries(topic) -> dict[str, list[str]]:
    """Search strings by language for one topic."""
    kind = topic['kind']
    planned = topic.get('plannedQueries') or {}
    english = [value for value in topic.get('aliases', ()) if re.search(r'[A-Za-z]{3}', value)]
    if kind == 'abnormal_flora':
        name = (topic.get('objectTerms') or english or [topic['title']])[0].replace('"', '')
        return {
            'en': [f'"{name}" gut microbiota human health disease association'],
            'pubmed': [f'"{name}" human gut microbiome'],
        }
    if kind == 'gene':
        name = topic['title'].replace('"', '')
        return {'en': [f'"{name}" gene function bacteria virulence'], 'pubmed': [f'"{name}" gene function']}
    if topic.get('structureCode') == 'gmwi2':
        return {'en': topic['queryTerms'], 'pubmed': ['"Gut Microbiome Wellness Index 2"[Title]']}
    if topic.get('functionFamily') == 'neurotransmitter':
        return {
            'en': ['human gut bacteria directly produce serotonin or GABA in vitro human fecal isolates'],
            'pubmed': ['("gut bacteria" OR "gut microbiota" OR fecal) AND (GABA OR serotonin OR dopamine) '
                'AND (production OR synthesis)'],
        }
    if topic.get('functionFamily'):
        term = topic['queryTerms'][0]
        return {'en': [term], 'pubmed': [term]}
    if kind == 'microbiome' and topic.get('queryTerms'):
        return {'en': [topic['queryTerms'][0]], 'pubmed': [topic['queryTerms'][0] + ' review']}
    if kind in {'nutrition', 'pathway', 'microbiome'}:
        term = (english or [topic['title']])[0]
        return {'en': [term + ' human health review'], 'pubmed': [term + ' review']}
    zh = list(planned.get('zh') or ())
    en = list(planned.get('en') or topic.get('queryTerms') or ())
    if not zh and re.search(r'[一-鿿]', topic['title']):
        zh = [re.sub(r'[（(].*?[）)]', '', topic['title']).strip() + ' 指南']
    if not en and english:
        en = [english[0] + ' guideline']
    if topic.get('questionIntent') == 'diagnostic_workup':
        def workup_query(text):
            return bool(_DIAGNOSTIC_PATH.search(text)
                and (not topic.get('requiresPositiveTest') or _POSITIVE_TEST.search(text)))

        en.sort(key=workup_query, reverse=True)
        zh.sort(key=workup_query, reverse=True)
    return {'zh': zh[:2], 'en': en[:2], 'pubmed': en[:1]}


def _jobs(topics, library, public, web, limits: RecallLimits, age_group):
    """Interleave topics so that a tight budget still covers every kind."""
    order = (
        'clinical',
        'patient_management',
        'goal',
        'clinical_decision',
        'abnormal_flora',
        'microbiome',
        'nutrition',
        'pathway',
        'gene',
        'behavior',
    )
    lanes = []
    for kind in order:
        lane = []
        for topic in (row for row in topics if row['kind'] == kind):
            queries = _queries(topic)
            clinical = kind in _PLANNED_KINDS
            intent = (
                'diagnostic_guideline'
                if kind in _CLINICAL_KINDS | {'patient_management'}
                else 'health_management'
                if kind == 'behavior'
                else 'microbe_clinical_significance'
                if kind == 'abnormal_flora'
                else 'gut_function'
            )
            profile = 'evaluate_general_function' if intent == 'gut_function' else 'clinical_report'

            def make(text, channels=('guide', 'literature')):
                return EvidenceQuery(
                    query=text[:128],
                    supports=(topic['topicId'],),
                    required_concepts=(),
                    intent=intent,
                    target=topic['title'],
                    age_group=age_group,
                    screen_profile=profile,
                    channels=channels,
                )

            steps = []
            if _enabled(web):
                steps.extend((web, make(text)) for text in (*queries.get('zh', ()), *queries.get('en', ())))
            if _enabled(library):
                if clinical:
                    steps.extend((library, make(text, ('guide',))) for text in queries.get('zh', ())[:1])
                    steps.extend((library, make(text)) for text in queries.get('en', ())[:1])
                else:
                    steps.extend((library, make(text, ('literature',))) for text in queries.get('en', ())[:1])
            if _enabled(public):
                steps.extend((public, make(text)) for text in queries.get('pubmed', ())[:1])
            lane.append(steps)
        lanes.append(lane)
    topic_jobs = []
    depth = max((len(lane) for lane in lanes), default=0)
    for index in range(depth):
        for lane in lanes:
            if index < len(lane):
                topic_jobs.append(lane[index])
    # Queue each provider's first result for a topic before repeating that
    # provider's searches. A slower alternative search does not block all
    # other topics' first attempt behind multiple translations of one query.
    jobs = {}
    waves = []
    for steps in topic_jobs:
        by_provider = {}
        for client, query in steps:
            by_provider.setdefault(id(client), []).append((client, query))
        waves.append(by_provider)
    width = max((len(rows) for group in waves for rows in group.values()), default=0)
    for step in range(width):
        for group in waves:
            for rows in group.values():
                if step >= len(rows):
                    continue
                client, query = rows[step]
                key = (id(client), query.query_id)
                if key in jobs:
                    previous = jobs[key][1]
                    query = replace(previous, supports=tuple(_unique([*previous.supports, *query.supports])))
                elif len(jobs) >= limits.max_queries:
                    continue
                jobs[key] = (client, query)
    return list(jobs.values())


async def _run(jobs, *, public, web, deadline, limits: RecallLimits):
    grouped: dict[int, tuple[Any, list]] = {}
    for client, query in jobs:
        grouped.setdefault(id(client), (client, []))[1].append(query)
    gate = asyncio.Semaphore(max(1, limits.concurrency) * 2)
    results = await asyncio.gather(
        *(
            _retrieve(
                client,
                queries,
                deadline=deadline,
                concurrency=limits.public_concurrency if client is public else limits.concurrency,
                provider=(
                    'public_literature' if client is public else 'deepseek_web' if client is web else 'evimed_v2'
                ),
                shared_gate=gate,
                chapter_recall=True,
            )
            for client, queries in grouped.values()
        )
    )
    return [record for records, _ in results for record in records], [row for _, rows in results for row in rows]


async def enrich_report_reference_metadata(bundle, web_client, numbers, *, deadline):
    """Add verified bibliographic fields only to citations used by final prose."""
    lookup = getattr(web_client, 'reference_metadata', None)
    report = {'requested': 0, 'enriched': 0, 'lookupErrors': 0, 'deadlineExhausted': False}
    if not callable(lookup) or deadline <= time.monotonic():
        return report
    used = set(numbers)
    references = [row for row in bundle.references if row.number in used and (not row.doi or row.year is None)]
    report['requested'] = len(references)
    if not references:
        return report

    async def enrich(row):
        try:
            lookup_url = f'https://doi.org/{row.doi}' if row.doi else row.url
            fields = await lookup(title=row.title, year=row.year, url=lookup_url)
            doi = fields.get('doi')
            if not isinstance(doi, str) or not re.fullmatch(r'10\.\d{4,9}/[^\s<>?#]+', doi, re.I):
                return
            if row.doi and row.doi.casefold() != doi.casefold():
                return
            year = fields.get('year')
            changed = False
            if not row.doi:
                row.doi = doi
                changed = True
            if row.year is None and type(year) is int:
                row.year = year
                changed = True
            report['enriched'] += int(changed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A metadata lookup has no authority over adopted original text
            # or publication eligibility, even if the registry is offline.
            report['lookupErrors'] += 1

    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await asyncio.gather(*(enrich(row) for row in references))
    except TimeoutError:
        report['deadlineExhausted'] = True
    return report


def _public_reference_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parts = urlsplit(url)
        uptodate = parts.hostname in {'uptodate.com', 'www.uptodate.com'} and parts.path.startswith('/contents/')
    except ValueError:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = [(key, value) for key, value in query if not (
        key.casefold().startswith(('utm_', '__cf_chl_')) or key.casefold() in {'fbclid', 'gclid'}
        or uptodate and key.casefold() in {'search', 'search_result', 'selectedtitle', 'usage_type', 'display_rank',
            'searchcorrelationterm', 'searchcorrelationid', 'source'})]
    if values == query:
        return url
    return urlunsplit(parts._replace(query=urlencode(values)))


def _article(blocks):
    """Normalize a publication once before testing it against all report topics."""
    full = '\n\n'.join(_unique(evaluation_original_text(row) for row in blocks))
    if not full:
        return None
    def admitted(row):
        title = str(_get(row, 'title', default='') or '')
        if is_figure_or_table_caption(title):
            return False
        if _get(row, 'provider') == 'deepseek_web' and (
            _DANGLING_TITLE.search(title.rstrip()) or _TITLE_FRAGMENT.search(title)
            or not evidence_document_title(title, evaluation_original_text(row))
        ):
            return False
        types = ' '.join(str(value) for value in _get(row, 'study_types', 'studyTypes', default=()) or ())
        url = str(_get(row, 'url', default='') or '')
        return not (_NOT_EVIDENCE.search(title) or _UNFINISHED.search(title + ' ' + types)
            or _PREPRINT_URL.search(url) or re.search(r'case reports?|editorial|comment|news|letter', types, re.I))

    representative = next((row for row in blocks if admitted(row)), None)
    if representative is None:
        return None
    title = str(_get(representative, 'title', default='') or '')
    category = _get(representative, 'category')
    grade, _ = evidence_grade(category, _get(representative, 'study_types', 'studyTypes', default=()), title)
    passages = [row for row in evaluation_original_passages(full) if row['eligibleForObjectMatch']]
    sentences = [(text, text.casefold(), row['citationRole'], row['text']) for row in passages
        for text in _sentences(row['text'])]
    eligible_text = '\n\n'.join(row['text'] for row in passages)
    return title, eligible_text, eligible_text.casefold(), sentences, category, grade


def _topic_matchers(topic):
    kind = topic['kind']
    exact = kind in {'abnormal_flora', 'gene'} or topic.get('structureCode') == 'gmwi2' or topic.get('functionFamily')
    terms = (
        ([term for term in topic.get('objectTerms', ()) if term] or topic['aliases'][:1])
        if exact
        else topic_terms(topic)
    )
    if kind == 'gene':
        terms = [topic['title']]
    return terms, tuple(matcher for term in terms if (matcher := _term_matcher(term, exact)))


def _clinical_source_text(fact):
    return ' '.join(str(_get(fact, field, default='') or '') for field in ('name', 'value', 'method', 'status'))


def _confirmed_fatty_liver(facts):
    return any(
        _FATTY_LIVER.search(finding) and not _UNCONFIRMED_FINDING.search(finding)
        for fact in facts if _get(fact, 'kind') == 'clinical'
        for finding in re.split(r'[，。；、,;\n]', str(_get(fact, 'value', default='') or ''))
    )


def _is_primary_liver_screen(unit):
    return any(_INITIAL_SCREEN.search(sentence) and _FATTY_LIVER.search(sentence)
        and not _OTHER_SCREENING_ENDPOINT.search(sentence) for sentence in _sentences(unit))


def _function_endpoints(text, unit, title, family, matchers):
    # Apply site and direction to the literal result, even if a mixed-source
    # paragraph discusses human gut processes elsewhere.
    if (_PLANT_ENVIRONMENT.search(text) or _PLANT_ENVIRONMENT.search(unit)) and not _GUT_SITE.search(text):
        return False
    if family == 'scfa':
        compound_text = _SCFA_DERIVATIVE.sub(' ', _PRODUCER_LABEL.sub(' ', text)).casefold()
        return any(_matches(compound_text, matcher) for matcher in matchers)
    if family == 'neurotransmitter':
        return supports_human_gut_neurotransmitter_synthesis(text, unit, title)
    if family == 'aminoAcid':
        return bool(_SYNTHESIS.search(text) or not _BREAKDOWN.search(title + ' ' + text))
    if family == 'lipid':
        return not _CHOLESTASIS.search(title + ' ' + unit)
    return True


def _match_article(article, topic, terms, matchers, *, facts=(), known_fatty_liver=False, subject=None):
    title, full, folded_full, sentences, category, grade = article
    folded_title = title.casefold()
    # A sentence cannot match a term whose first literal token is absent from
    # the whole article. Title matches still use the full set of terms.
    candidates = [matcher for matcher in matchers if matcher[0] in folded_full]
    if not candidates:
        return None
    kind = topic['kind']
    clinical = kind in _PLANNED_KINDS
    family = topic.get('functionFamily')
    mechanism = topic.get('questionIntent') == 'general_mechanism'
    workup = topic.get('questionIntent') == 'diagnostic_workup'
    if clinical and grade == 'D':
        return None
    if clinical and _LAB_FINDING.search(topic['title']) and _PROGNOSTIC_ENDPOINT.search(title):
        # An outcome study in a disease cohort does not explain the meaning
        # of a direction-specific lab reading outside that endpoint.
        return None
    title_match = any(_matches(folded_title, matcher) for matcher in matchers)
    matched = [(text, role, unit) for text, folded, role, unit in sentences
        if not _UNFINISHED.search(text) and not _METHOD_ONLY.search(text)
        and any(_matches(folded, matcher) for matcher in candidates)]
    if clinical:
        source_text = ' '.join(_clinical_source_text(fact) for fact in facts if _get(fact, 'kind') == 'clinical')
        if _CEREBROVASCULAR_EXAM.search(source_text) and _CEREBROVASCULAR_EXAM.search(topic['title']):
            unknown_protocols = [pattern for pattern in _EXAM_PROTOCOLS if not pattern.search(source_text)]
            matched = [(text, role, unit) for text, role, unit in matched
                if not any(pattern.search(title + '\n' + unit) for pattern in unknown_protocols)]
        if _FATTY_LIVER.search(topic['title']) and (known_fatty_liver or _confirmed_fatty_liver(facts)):
            matched = [(text, role, unit) for text, role, unit in matched if not _is_primary_liver_screen(unit)]
    if workup:
        after_positive = topic.get('requiresPositiveTest')
        stool_test = bool(_STOOL_TEST.search(topic['title']))
        matched = [(text, role, unit) for text, role, unit in matched
            if _DIAGNOSTIC_PATH.search(text)
            and (not after_positive or (_POSITIVE_TEST.search(text) and _POST_TEST_STEP.search(text)))
            and (not stool_test or _STOOL_TEST.search(text))]
    if mechanism:
        if not _GUT_SITE.search(title + ' ' + full) or _TREATMENT_QUESTION.search(title):
            return None
        matched = [(text, role, unit) for text, role, unit in matched
            if not _MEASUREMENT_METHOD.search(text)
            and not re.match(r'(?:[-*]\s*)?[•●]', unit)
            and _function_endpoints(text, unit, title, family, candidates)]
        if family != 'neurotransmitter':
            matched = [(text, role, unit) for text, role, unit in matched
                if (role != 'secondary_background' or _ACTUAL_RESULT.search(text))
                and (_MICROBIAL_PROCESS.search(text) or _ACTUAL_RESULT.search(text))
                and _MICROBIAL_ACTOR.search(unit)
                and (_ACTUAL_RESULT.search(text) or _MECHANISTIC_RELATION.search(text))
                and (not _EXPERIMENTAL_SETTING.search(title + ' ' + unit) or _ACTUAL_RESULT.search(text))]
    if not matched:
        return None
    matched.sort(key=lambda item: (
        item[1] != 'secondary_background',
        bool(_ACTUAL_RESULT.search(item[0])) if mechanism else False,
        bool(_DIRECTIVE.search(item[0])) if clinical else bool(_HUMAN_SETTING.search(item[0])),
    ), reverse=True)
    selected_quotes = [text for text, _, _ in matched[:2]]
    scope_material = full[:6000]
    if (clinical or mechanism) and _EXPLICIT_SCOPE.search(
        title + ' ' + scope_material + ' ' + ' '.join(selected_quotes)
    ):
        # Check the title and recruitment language before handing an original
        # to the writer. For a mechanism, a cohort restriction is still
        # governing and cannot establish a general host mechanism by itself.
        scope_material = '\n'.join([scope_material, *(text for text in selected_quotes if text not in scope_material)])
        scope_errors = source_candidate_scope_errors(
            source_text=scope_material, title=title, patient_facts=facts,
            target_context={'subject': {'age': _get(subject, 'age'),
                'sex': _get(subject, 'sex', 'gender')}},
            selected_quotes=selected_quotes,
        )
        if {'unfinished_study', 'source_condition_not_in_patient_context',
                'source_genetic_not_in_patient_context'} & set(scope_errors):
            return None
    if mechanism and _TITLE_SCOPE.search(title) and not _ACTUAL_RESULT.search(' '.join(selected_quotes)):
        return None
    hits = [text for text, _, _ in matched]
    if kind in _CLINICAL_KINDS | {'patient_management', 'behavior'}:
        directive = any(_DIRECTIVE.search(sentence) for sentence in hits)
        if not (workup or title_match or len(hits) >= 3 or (category == 'guideline' and directive)):
            # Mentioned in passing, not a document about this problem. A
            # guideline that gives a recommendation on the object is kept even
            # when its title names the broader condition.
            return None
    management = [
        text for text, role, _ in matched if role != 'secondary_background' and _DIRECTIVE.search(text)
        and (workup or _MANAGEMENT.search(text))
    ]
    allowed = set(topic.get('allowedRoles') or ('interpretation',))
    action = clinical and 'action' in allowed and category == 'guideline' and grade == 'A' and bool(management)
    if kind == 'patient_management' and not action:
        return None
    if action:
        # A literal recommendation governs only its own passage, not every
        # definition or screening paragraph in a matching article.
        hits = management
    roles = [role for role in ('interpretation', 'action') if role in allowed and (role != 'action' or action)]
    if not roles:
        return None
    grade_priority = {'A': 2.0, 'B': 1.25, 'C': 0.5, 'D': 0.0}.get(grade, 0.0)
    score = grade_priority + (1.5 if title_match else 1.0) + min(0.5, len(hits) * 0.05)
    if workup or action:
        score += 2.0
    elif mechanism and _HUMAN_SETTING.search(title + ' ' + ' '.join(hits[:2])):
        score += 1.0
    if family:
        # The writer caps sources per target/role. Exact function findings
        # must reach their own cell ahead of generic clinical context.
        score += 2.0 + float(any(_ACTUAL_RESULT.search(text) for text in hits[:2]))
    source_scope = []
    if mechanism:
        source_scope.append('Use the stated microbial endpoint only and keep its source conditions. '
            'This is not evidence of the person\'s concentration, symptom cause, diet effect or treatment.')
        if family == 'neurotransmitter':
            source_scope.append('State the specific named transmitter, native human-gut producer and study setting. '
                'Reported microbial production has no authority over the person\'s fatigue, sleep or host levels.')
        if _EXPERIMENTAL_SETTING.search(title + ' ' + ' '.join(unit for _, _, unit in matched[:2])):
            source_scope.append('Experimental or model evidence. State the original setting and endpoint.')
    return {
        'topicId': topic['topicId'],
        'targetIds': list(topic['targetIds']),
        'clinicalBridgeTargetIds': topic.get('clinicalBridgeTargetIds', ()),
        'sourceFactIds': list(topic['sourceFactIds']),
        'ownerCode': topic.get('ownerCode'),
        'objectName': topic['title'],
        'objectKind': kind,
        'questionIntent': topic.get('questionIntent'),
        'objectTerms': list(terms),
        'sourcePanelIds': list(topic.get('sourcePanelIds', ())),
        'matchBasis': 'literal_title_and_object' if title_match else 'literal_object_in_text',
        'selectedQuotes': _unique(hits)[:2],
        **({'passageSelection': 'mechanism_sentence', 'functionFamily': family} if family else {}),
        'eligibleRoles': roles,
        'sourceRole': 'secondary_background' if any(role == 'secondary_background' for _, role, _ in matched[:2])
            else 'primary_body',
        **({'recommendationDomain': 'patient_management'} if action else {}),
        'scopeNotes': source_scope or ['Candidate only; applicability is confirmed by chapter review.'],
        'candidateUse': 'clinical_context' if kind in _PLANNED_KINDS else 'general_function',
        'score': score,
    }


def _match(blocks, topic):
    """Link a publication to a topic when its literal text names the object."""
    article = _article(blocks)
    return _match_article(article, topic, *_topic_matchers(topic)) if article else None


def _title_key(title) -> frozenset:
    text = unicodedata.normalize('NFKC', str(title or '')).casefold()
    words = re.findall(r'[a-z0-9]{3,}', text)
    words = [word[:-1] if word.endswith('s') and len(word) > 4 else word for word in words]
    return frozenset(words + re.findall(r'[\u4e00-\u9fff]', text))


def _same_title(first: frozenset, second: frozenset) -> bool:
    if len(first) < 5 or len(second) < 5:
        return False
    return len(first & second) / len(first | second) >= 0.8


def _select(pool, topics, *, subject, facts, limits: RecallLimits):
    grouped = _group_records(pool)
    links: dict[Any, list[dict]] = {}
    known = {_get(fact, 'factId', 'fact_id'): fact for fact in facts}
    known_fatty_liver = _confirmed_fatty_liver(facts)
    prepared = [(topic, *_topic_matchers(topic),
        [known[fid] for fid in topic['sourceFactIds'] if fid in known]) for topic in topics]
    for identity, blocks in grouped.items():
        if not (article := _article(blocks)):
            continue
        title = article[0]
        applicable = _subject_scope_matches(title, subject, facts)
        # Gene topics may cite work on the organism rather than on people,
        # but never work on a veterinary or aquaculture host.
        host_free = not veterinary_host(title)
        for topic, terms, matchers, witnesses in prepared:
            if not (applicable or topic['kind'] == 'gene' and host_free):
                continue
            edge = _match_article(article, topic, terms, matchers, facts=witnesses,
                known_fatty_liver=known_fatty_liver, subject=subject)
            if edge is not None:
                links.setdefault(identity, []).append(edge)

    def rank(identity, topic_id):
        edge = next(edge for edge in links[identity] if edge['topicId'] == topic_id)
        year = max((_get(row, 'year', default=0) or 0 for row in grouped[identity]), default=0)
        return (edge['score'], year)

    chosen: dict[Any, list[dict]] = {}
    order = (
        'clinical',
        'patient_management',
        'goal',
        'clinical_decision',
        'abnormal_flora',
        'microbiome',
        'nutrition',
        'pathway',
        'gene',
        'behavior',
    )
    ordered = [topic for kind in order for topic in topics if topic['kind'] == kind]
    ordered.sort(key=lambda topic: 0 if topic.get('questionIntent') == 'diagnostic_workup'
        else 1 if topic.get('functionFamily') else 2)
    quota = {
        topic['topicId']: (
            limits.clinical_per_topic
            if topic['kind'] in _CLINICAL_KINDS | {'patient_management'}
            else 1
            if topic['kind'] == 'gene'
            else limits.default_per_topic
        )
        for topic in ordered
    }
    by_topic = {
        topic['topicId']: sorted(
            (
                identity
                for identity, edges in links.items()
                if any(edge['topicId'] == topic['topicId'] for edge in edges)
            ),
            key=lambda identity, tid=topic['topicId']: rank(identity, tid),
            reverse=True,
        )
        for topic in ordered
    }
    # Round-robin so each topic can receive its best proved source first.
    # The grouped publication identity, not title similarity, determines editions.
    for round_index in range(max(quota.values(), default=0)):
        for topic in ordered:
            topic_id = topic['topicId']
            candidates = by_topic[topic_id]
            if round_index >= min(quota[topic_id], len(candidates)):
                continue
            if len(chosen) < limits.max_publications:
                chosen.setdefault(candidates[round_index], [])
    # A chosen publication keeps every literal edge it has, so one guideline
    # can serve all the problems it actually discusses.
    for identity in chosen:
        chosen[identity] = links[identity]
    return chosen, links


async def recall_report_evidence_sources(
    library_client,
    public_client=None,
    web_client=None,
    model_client=None,
    *,
    blueprint,
    subject,
    deadline,
    audit=None,
    limits: RecallLimits | None = None,
) -> ReportEvidenceBundle:
    """Recall, merge and select candidate originals from all configured sources."""
    limits = limits or RecallLimits()
    bundle = ReportEvidenceBundle(plan=[], _selector_client=lambda: model_client)
    topics = _prepare_topics(build_report_evidence_plan(blueprint), blueprint)
    bundle.plan = topics
    await _plan_queries(
        model_client,
        topics,
        blueprint=blueprint,
        subject=subject,
        deadline=deadline,
        audit=audit,
        timeout=limits.planning_timeout_seconds,
    )
    jobs = _jobs(topics, library_client, public_client, web_client, limits, _age_group(subject))
    await _emit(
        audit,
        'plan',
        {
            'topicCount': len(topics),
            'queryCount': len(jobs),
            'sources': {
                'library': _enabled(library_client),
                'pubmed': _enabled(public_client),
                'web': _enabled(web_client),
            },
        },
    )
    if not jobs:
        bundle.gaps.append({'reason': 'evidence_provider_unavailable'})
    started = time.monotonic()
    logger.info('report evidence recall: topics=%s queries=%s', len(topics), len(jobs))
    records, rows = await _run(jobs, public=public_client, web=web_client, deadline=deadline, limits=limits)
    retrieved_at = time.monotonic()
    bundle.query_audits.extend(rows)
    bundle.recalled_records.extend(records)
    pool = _merge_pool(bundle, [], records)
    reconcile_evaluation_guideline_representations(pool)
    facts = list(_get(blueprint, 'facts', default=()) or ())
    chosen, links = await asyncio.to_thread(_select, pool, topics, subject=subject, facts=facts, limits=limits)
    bundle._topic_links = chosen
    bundle._priority_topic_ids = {
        topic['topicId'] for topic in topics
        if topic.get('questionIntent') == 'diagnostic_workup' or topic.get('functionFamily')
    }
    bundle._max_publications = limits.max_publications
    bundle._per_topic = limits.clinical_per_topic
    selected = [record for record in pool if evaluation_publication_identity(record) in chosen]
    _compile_bundle(bundle, selected, topics)
    for reference in bundle.references:
        reference.url = _public_reference_url(reference.url)
    providers: dict[str, int] = {}
    for record in bundle.records:
        providers[str(_get(record, 'provider'))] = providers.get(str(_get(record, 'provider')), 0) + 1
    logger.info(
        'report evidence recall complete: recalled=%s publications=%s by_provider=%s seconds=%.1f '
        'retrieval_seconds=%.1f selection_seconds=%.1f',
        len(bundle.recalled_records),
        len(bundle.references),
        providers,
        time.monotonic() - started,
        retrieved_at - started,
        time.monotonic() - retrieved_at,
    )
    await _emit(
        audit,
        'retrieval_complete',
        {
            'queryCount': len(bundle.query_audits),
            'recalledRecordCount': len(bundle.recalled_records),
            'literalPublicationCount': len(links),
            'publicationCount': len(bundle.references),
            'gapCount': len(bundle.gaps),
            'selectedByProvider': providers,
            'elapsedSeconds': round(time.monotonic() - started, 1),
            'webUsage': getattr(web_client, 'usage', None) if _enabled(web_client) else None,
            'topicsWithoutEvidence': [
                topic['title']
                for topic in topics
                if not any(topic['topicId'] in row.get('topicIds', ()) for row in bundle.summaries)
            ][:60],
        },
    )
    return bundle


__all__ = ['RecallLimits', 'enrich_report_reference_metadata', 'recall_report_evidence_sources', 'topic_terms']
