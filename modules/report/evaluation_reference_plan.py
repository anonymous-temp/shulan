"""Bounded evidence packets for existing abnormal clinical imaging findings.

This is a candidate-ranking plan, not a medical-claim approval or a reference
count requirement. The caller supplies records already matched to independently
obtained original text; the existing sentence and selected-quotation reviews
still decide whether an authored claim can be published.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from typing import Any

from modules.report.clinical_report import _review_quote_options
from modules.report.evaluation_claim_guards import barrier_function_process
from modules.report.evaluation_function_evidence import (
    basic_metabolite_sentence_role, basic_pathogen_sentence_role,
    scfa_production_sentence_role,
)
from modules.report.evaluation_catalog import (AMINO_ACID_ALIASES, FIXED_METRIC_ALIASES,
    NEUROTRANSMITTER_DEFINITIONS, PATHWAY_DEFINITIONS)


_DIAGNOSTIC_INTENTS = {"diagnostic_guideline", "diagnostic_evaluation", "clinical_evaluation"}
_EVALUATION_INTENTS = {"health_management", "disease_clinical_significance"}
_GUIDELINE = re.compile(r"guideline|consensus|指南|共识", re.I)
_EVALUATION = re.compile(
    r"diagnos|evaluat|assess|histolog|patholog|immunohistochem|follow.up|surveillance|"
    r"诊断|评估|评价|病理|组织学|免疫组化|随访", re.I)
_SPECIFIC_PRINCIPLE = re.compile(
    r"histolog|immunohistochem|pathological diagnosis|病理诊断|组织学|免疫组化|"
    r"metabolic evaluation|risk.{0,16}recurrence|clinical evaluation|风险分层|代谢评估", re.I)
_SPECIALIZED_TITLE = re.compile(
    r"artificial intelligence|AI.assisted|prediction|molecular|risk stratification|"
    r"patients diagnosed|人工智能|预测|分子|风险分层", re.I)
_TREATMENT_SUBGROUP = re.compile(
    r"metasta|sarcomatosis|cytoreductive|radiotherap|chemotherap|adjuvant|"
    r"advanced disease|recurrence.free|postoperative|post.operativ|"
    r"effects of surgical|surgical management|转移|放疗|化疗|术后|减瘤|晚期", re.I)
_FINDING = re.compile(
    r"结节|结石|肿块|占位|囊肿|增厚|梗阻|骨折|积液|肿大|狭窄|扩张|实变|钙化|"
    r"病灶|肿瘤|间质瘤|异常|nodule|stone|mass|lesion|tumou?r|cyst|fracture|effusion", re.I)
_NEGATED = re.compile(r"(?:未见|未发现|未显示|无|没有|正常)[^，,。；;]{0,6}$|"
    r"(?:no|without|absence of)[a-z\s-]{0,12}$", re.I)
_UNCERTAIN = re.compile(r"可能|疑似|考虑|待排|不能排除|不排除|suspect|possible|cannot exclude", re.I)


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return {}


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")) if isinstance(value, str) else ""


def _finding_quote(text: str, certainty: str | None) -> bool:
    if certainty == "suspected" and _UNCERTAIN.search(text):
        return True
    for clause in re.split(r"[，,。；;\r\n]", text):
        for match in _FINDING.finditer(clause):
            if not _NEGATED.search(clause[:match.start()]):
                return True
    return False


def _bound_problems(record: dict, allowed: dict[str, dict]) -> list[dict]:
    supports = {item for field in ("supports", "diseaseIds")
        for item in (record.get(field) or []) if isinstance(item, str)}
    result = []
    for raw in (record.get("clinicalProblemBindings") or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("problemId"), str):
            continue
        if supports and raw["problemId"] not in supports:
            continue
        sources = []
        for source in (raw.get("sources") or []):
            if not isinstance(source, dict) or source.get("factId") not in allowed:
                continue
            fact = allowed[source["factId"]]
            quote = source.get("sourceQuote")
            if (not isinstance(quote, str) or not _compact(quote)
                    or _compact(quote) not in _compact(fact.get("value"))
                    or not _finding_quote(quote, raw.get("certainty"))):
                continue
            sources.append(deepcopy(source))
        if sources:
            result.append({**deepcopy(raw), "sources": sources})
    return result


def _rank(record: dict) -> tuple:
    intents = set(record.get("intents") or [])
    title, source = str(record.get("title", "")), record["sourceText"]
    return (not bool(_EVALUATION.search(title)), bool(_SPECIALIZED_TITLE.search(title)),
        not bool(_SPECIFIC_PRINCIPLE.search(source)),
        not bool(intents & _DIAGNOSTIC_INTENTS),
        not bool(record.get("category") == "guideline" or _GUIDELINE.search(title)),
        not bool(intents & _EVALUATION_INTENTS), record["number"])


def _append_unique(owner: list, values: list) -> None:
    for value in values:
        if value not in owner:
            owner.append(deepcopy(value))


def build_clinical_reference_plan(target_path, fact_ids, facts, records) -> dict | None:
    """Rank a small, source-bound clinical interpretation packet or return None.

    Subject selection is upstream: only the caller's exact allowed clinical
    facts can bind a problem here. Sharing one CT ID does not merge its separate
    problem scopes. No source value, grade, source text or bibliography identity
    is generated or changed by this function.
    """
    path = tuple(target_path)
    if not (len(path) == 4 and path[:2] == ("medicalAssessment", "findings")
            and type(path[2]) is int and path[-1] == "explanation"):
        return None
    fact_rows = facts.values() if isinstance(facts, dict) else facts
    allowed_ids = set(fact_ids)
    allowed = {row.get("factId", row.get("fact_id")): row for value in fact_rows
        if (row := _mapping(value)).get("kind") == "clinical"
        and row.get("imaging") is True
        and row.get("factId", row.get("fact_id")) in allowed_ids}
    if not allowed:
        return None

    candidates, problems, conflicts = {}, {}, set()
    union_fields = ("supports", "microbeIds", "diseaseIds", "intents", "targets", "relationBindings")
    for value in records:
        if not isinstance(value, dict):
            continue
        number, source = value.get("number"), value.get("sourceText")
        if (type(number) is not int or number <= 0 or not isinstance(source, str) or not source.strip()
                or not isinstance(value.get("title"), str) or not value["title"].strip()):
            continue
        intents = set(value.get("intents") or [])
        if not (intents & (_DIAGNOSTIC_INTENTS | _EVALUATION_INTENTS)
                or value.get("category") == "guideline" or _GUIDELINE.search(value["title"])):
            continue
        if not _EVALUATION.search(source) or _TREATMENT_SUBGROUP.search(value["title"]):
            continue
        bindings = _bound_problems(value, allowed)
        if not bindings:
            continue
        previous = candidates.get(number)
        if previous is not None and (previous["sourceText"] != source or previous["title"] != value["title"]):
            conflicts.add(number)
            continue
        context = {field: deepcopy(value[field]) for field in
            (*union_fields, "clinicalProblemBindings", "interpretation") if field in value}
        context["clinicalProblemBindings"] = deepcopy(bindings)
        if previous is None:
            candidate = deepcopy(value)
            candidate["clinicalProblemBindings"] = bindings
            candidate["referenceUseContexts"] = [context]
            candidates[number] = candidate
        else:
            candidate = previous
            for field in union_fields:
                _append_unique(candidate.setdefault(field, []), value.get(field) or [])
            _append_unique(candidate["clinicalProblemBindings"], bindings)
            _append_unique(candidate["referenceUseContexts"], [context])
        for problem in bindings:
            key = problem["problemId"]
            if key not in problems:
                problems[key] = {**deepcopy(problem), "numbers": []}
            _append_unique(problems[key]["sources"], problem["sources"])
            if number not in problems[key]["numbers"]:
                problems[key]["numbers"].append(number)

    for number in conflicts:
        candidates.pop(number, None)
    if not candidates:
        return None
    selected, preferred, plans = [], [], []
    for problem in problems.values():
        available = sorted((candidates[number] for number in problem["numbers"] if number in candidates), key=_rank)
        if not available:
            continue
        priority = [row["number"] for row in available[:2]]
        _append_unique(preferred, priority)
        _append_unique(selected, priority)
        plans.append({key: deepcopy(value) for key, value in problem.items() if key != "numbers"})
        plans[-1].update(factIds=list(dict.fromkeys(source["factId"] for source in problem["sources"])),
            preferredNumbers=priority)
    # Two candidate roles per actual problem are enough for a short explanation.
    # Do not fill unused space with increasingly remote treatment or test
    # performance studies just because the context budget permits eight.
    selected = selected[:8]
    preferred = [number for number in preferred if number in selected]
    plans = [{**plan, "preferredNumbers": [number for number in plan["preferredNumbers"] if number in selected]}
        for plan in plans if any(number in selected for number in plan["preferredNumbers"])]
    return {"evidenceRecords": [candidates[number] for number in selected],
        "preferredNumbers": preferred, "problemPlans": plans,
        "instruction": "先保留本节点实际影像发现及疑似程度，再以独立短句说明有原文支持的诊断或评估原则并引用对应编号。"
            "同一次影像的不同问题分别按problemPlans使用各自的来源短引文和文献；不要因共享factId借用其他问题的证据。"
            "preferredNumbers仅是适用候选优先顺序，不是引文审批。不得增加确诊、症状、病理已做或未做、分期、成分或治疗方案；"
            "原文和本句适用性仍须通过双轮审核，无支持的医学断言须重写，不能退回仅复述原结果来冒充有用的循证解释。"}


_BIOLOGICAL_PROCESS = re.compile(
    r"encod\w*|catalys\w*|catalyz\w*|transport\w*|transfer\w*|efflux|"
    r"regulat\w*|\bmodulat(?:e|es|ed|ing|ion)\b|metaboli\w*|ferment\w*|synthesi\w*|biosynth\w*|glucosylat\w*|mediat\w*|"
    r"\b(?:produce|produces|produced|producing|production)\b|protect\w*|inhibit\w*|maintain\w*|interact\w*|cross.feeding|"
    r"consum\w*|utili[sz]\w*|uptake|tight.junction|barrier.integrity|编码|催化|转移|转运|外排|调节|代谢|发酵|合成|生成|保护|抑制|相互作用|消耗|利用|摄取", re.I)
_HEALTH_DOMAINS = (
    (r"抗炎", ("anti-inflammatory", "inflammation")),
    (r"免疫", ("immune", "immunity", "immunomodulation")),
    (r"短链脂肪酸", ("short-chain fatty acid", "short-chain fatty acids",
        "short chain fatty acid", "short chain fatty acids", "SCFA", "SCFAs", "butyrate")),
    (r"耐药", ("resistance gene", "resistance genes", "resistome")),
    (r"真菌", ("fungi", "fungal", "mycobiome")),
    (r"病毒", ("virus", "viruses", "virome")),
    (r"寄生虫", ("parasite", "parasites")),
    (r"基石菌", ("keystone species", "keystone bacteria")),
    (r"屏障", ("intestinal barrier", "epithelial barrier", "barrier integrity")),
)
_PATHOGEN_HEALTH_PROCESS = re.compile(
    r'\b(?:infect\w*|invad\w*|invasion|pathogenesis|coloniz\w*|colonis\w*|'
    r'virulence|translocat\w*|replicat\w*|attach\w*|adher\w*|surviv\w*)\b|'
    r'感染|侵袭|致病|定植|移位|复制|黏附|粘附|存活', re.I)


def _literal_object_in_quote(term: str, quote: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(term)
        + r"(?![A-Za-z0-9_])", quote, re.I))


def _explicit_object_aliases(term,source):
    """Read a source's own full-taxon/strain alias, never infer taxonomy."""
    definitions={}
    pattern=(r'(?P<full>[A-Z][a-z]+ [a-z][a-z-]*(?:\s+subsp\.\s+[a-z-]+)?'
        r'(?:\s+[A-Z][A-Za-z0-9_-]*){0,2})\s*\('
        r'(?P<alias>[A-Z][A-Za-z0-9 .™®_-]{1,50})\)')
    for match in re.finditer(pattern,source):
        alias=re.sub(r'[™®]','',match['alias']).strip()
        full = match['full'].strip()
        # An AIEC/EPEC definition names a pathotype, not a new synonym for
        # every E. coli observation. Carry its literal qualifying phrase to
        # the writer so the research actor cannot silently become generic.
        prefix = source[max(0, match.start()-65):match.start()]
        qualifier = re.search(r'(adherent(?:[- ]and[- ]|[- ])invasive|enteropathogenic|'
            r'enteroha?emorrhagic|enteroaggregative|enterotoxigenic|uropathogenic|'
            r'Shiga[- ]toxin[- ]producing)\s+$', prefix, re.I)
        if qualifier:
            full = qualifier[1] + ' ' + full
        definitions.setdefault(alias,set()).add(full)
    return [(alias,next(iter(scopes)))for alias,scopes in definitions.items()if len(scopes)==1
        and _literal_object_in_quote(term,next(iter(scopes)))]


def _source_object_in_quote(term: str, quote: str, source: str, *, title: str = '') -> bool:
    if _literal_object_in_quote(term, quote):
        return True
    authority = title + '\n' + source
    if any(_literal_object_in_quote(alias,quote)for alias,_ in _explicit_object_aliases(term,authority)):
        return True
    if re.fullmatch(r'[A-Z][a-z]+', term):
        # A supplied genus can be explained by an explicitly named species in
        # this original. Preserve its exact species/experimental scope; this
        # only helps choose a sentence, never asserts that species was detected.
        species = set(re.findall(r'\b' + re.escape(term) + r' ([a-z][a-z-]+)\b', authority))
        return any(_source_object_in_quote(term + ' ' + name, quote, source, title=title) for name in species)
    taxon = re.fullmatch(r'([A-Z])[a-z]+ ([a-z][a-z-]+)', term)
    if not taxon or not _literal_object_in_quote(term, authority):
        return False
    # The abbreviation must have a unique full taxon in this original text.
    names = set(re.findall(r'\b' + taxon[1] + r'[a-z]+ ' + re.escape(taxon[2]) + r'\b', authority))
    return names == {term} and bool(re.search(r'\b' + taxon[1] + r'\.\s*'
        + re.escape(taxon[2]) + r'\b', quote))


_PRODUCT_USE_TITLE = re.compile(
    r"clinical application|powder|capsule|tablets?|tetravaccine|live combined|"
    r"combination of|multi.strain|synbiotic combinations|临床应用|制剂|胶囊|片剂|联合活菌", re.I)
_CONDITIONED_EFFECT_TITLE = re.compile(r"\binduced\b|intergenerational|supplementation|\btherapy\b|\btreatment\b", re.I)
_ACTIVE_BENEFICIAL_PROCESS = re.compile(
    r'\b(?:upregulat\w*|downregulat\w*|maintain\w*|restor\w*|strengthen\w*|enhanc\w*|'
    r'protect\w*|support\w*|inhibit\w*|reduc\w*|increas\w*|regulat(?:e|es|ed|ing)|'
    r'modulat(?:e|es|ed|ing)|produc(?:e|es|ed|ing)|ferment(?:s|ed|ing)?|utili[sz]\w*|'
    r'cataboli\w*|consum\w*|synthesi[sz]\w*|compet\w*|exclud\w*)\b|cross.feeding|'
    r'维持|修复|增强|保护|抑制|降低|增加|生成|发酵|利用|代谢|合成|竞争|排除',re.I)
_BENEFICIAL_PROCESS_TARGET = re.compile(
    r'barrier|tight.junction|pathogens?|regulatory T|\bTreg|dendritic|macrophage|'
    r'cytokine|interleukin|\bIL[-‐]?\d|butyrate|acetate|propionate|short.chain fatty|'
    r'oligosaccharide|polysaccharide|carbohydrate|sugar|lactate|vitamin|'
    r'colonization resistance|屏障|紧密连接|病原|调节性T|树突|巨噬|细胞因子|'
    r'丁酸|乙酸|丙酸|短链脂肪酸|寡糖|多糖|碳水|糖|乳酸|维生素|生长',re.I)


def _concrete_beneficial_process(quote):
    return bool(_ACTIVE_BENEFICIAL_PROCESS.search(quote)and _BENEFICIAL_PROCESS_TARGET.search(quote))


def health_member_function_priority(objects, title: str, source: str, *, subject_age=None) -> int:
    """Rank member biology before product-use literature, never approve it."""
    own_title = any(_literal_object_in_quote(member, title) for member in objects)
    own_process = any(_literal_object_in_quote(member, sentence) and _BIOLOGICAL_PROCESS.search(sentence)
        for sentence in _review_quote_options(source).values() for member in objects)
    biology_title = own_title and bool(_BIOLOGICAL_PROCESS.search(title)) and own_process
    stage_specific = (type(subject_age) in (int, float) and subject_age >= 18
        and bool(re.search(r'\binfants?\b[^.!?\n]{0,55}\b(?:gut|fecal|faecal|microbiota|nutrition)\b|'
            r'early life|after birth|nursling|婴儿肠道|婴儿粪便|婴儿营养', source, re.I)))
    adult = type(subject_age) in (int, float) and subject_age >= 18
    experimental = bool(re.search(r'\bin vitro\b|reactors?\b|simulated digestion|cell.lines?|co.?culture|'
        r'\bcultures?\b|\b(?:mice|mouse|murine|rats?)\b|体外|反应器|小鼠|大鼠', title+' '+source, re.I))
    milk_model = experimental and bool(re.search(r'human milk|infant (?:milk|formula)|'
        r'milk digestion|人乳|婴儿配方|乳消化', source, re.I))
    disease_model_effect = experimental and bool(re.search(r'ameliorat\w*|attenuat\w*|alleviat\w*|'
        r'\brescue\w*|\bimprov\w*|therap\w*|treat\w*|改善|治疗|缓解',title,re.I))
    human_function = bool(re.search(r'\b(?:humans?|adults?|participants?|volunteers?)\b|人体|成人|受试者',source,re.I)
        and re.search(r'\b(?:gut|intestinal|colonic|fecal|faecal)\b|肠道|结肠|粪便',source,re.I)
        and own_process and not experimental)
    return (8 * bool(biology_title) - 6 * bool(_PRODUCT_USE_TITLE.search(title))
        - 4 * bool(_CONDITIONED_EFFECT_TITLE.search(title)) - 6 * bool(stage_specific)
        + (8 * human_function - 10 * milk_model - 3 * experimental - 12 * disease_model_effect if adult else 0))


def _scfa_group_sentence_priority(quote: str, domains: list[str]) -> int:
    # A functional group is explained by its metabolite/process, not by the
    # population size of a member merely mentioned in an experimental trial.
    if not any(_literal_object_in_quote(term, quote) for term in domains):
        return 0
    return 3 if scfa_production_sentence_role(quote) else 0


def _immune_effector_sentence_priority(quote):
    """A named mediator and actual immune-cell/receptor process beat a hypothesis."""
    if _RESEARCH_PURPOSE_SENTENCE.search(quote) or re.search(r'\bwe reasoned\b|研究假说|我们推测', quote, re.I):
        return 0
    mediator = re.search(r'\b(?:butyrate|propionate|acetate|SCFAs?|short.chain fatty acids?|'
        r'indoles?|metabolites?|metabolic by.products?|ligands?)\b|丁酸|丙酸|乙酸|代谢产物|配体', quote, re.I)
    target = re.search(r'\b(?:Treg|Foxp3|TH17|regulatory T|T.cells?|dendritic|macrophages?|'
        r'AhR|HDAC|cytokines?|interleukins?)\b|调节性T|树突细胞|巨噬细胞|细胞因子', quote, re.I)
    process = re.search(r'\b(?:facilitat\w*|potentiat\w*|induc\w*|activat\w*|inhibit\w*|'
        r'mediat\w*|regulat\w*|differentiat\w*|generat\w*)\b|促进|诱导|激活|抑制|介导|分化|生成', quote, re.I)
    return 3 if mediator and target and process else 0


_OPPORTUNISTIC_PROCESS = re.compile(
    r"\b(?:support\w*\s+growth|surviv\w*|coloniz\w*\b(?!\s+resistance))\b[^.;\n]{0,90}\b(?:infection\w*|host|intestinal)\b|"
    r"\b(?:infection\w*|host|intestinal)\b[^.;\n]{0,90}\b(?:surviv\w*|coloniz\w*\b(?!\s+resistance))\b|"
    r"\b(?:induc\w*|caus\w*)\b[^.;\n]{0,60}\b(?:necrosis|inflammation|cell.death)\b|"
    r"\b(?:isolat\w*|associat\w*|implicat\w*)\b[^.;\n]{0,90}\b(?:infection\w*|abscess\w*|bactera?emia)\b", re.I)


def opportunistic_health_priority(objects, source: str) -> int:
    return 6 * any(_OPPORTUNISTIC_PROCESS.search(quote) and any(
        _source_object_in_quote(term, quote, source) for term in objects)
        for quote in _review_quote_options(source).values())


_AMINO_BASIC_ROLE = re.compile(
    r'\bprotein\s+(?:synth\w*|biosynth\w*|production|formation)\b|'
    r'\b(?:synth\w*|biosynth\w*)\s+of\s+proteins?\b|'
    r'\b(?:incorporat\w*\s+into\s+proteins?|proteinogenic|precursors?)\b|'
    r'蛋白(?:质)?(?:合成|生物合成)|(?:合成|构成)蛋白(?:质)?|前体', re.I)


def build_amino_acid_function_reference_plan(target_path, assessment_context, facts, records, *,
        subject_age=None) -> dict | None:
    """Offer existing same-member basic role sentences for amino-acid synthesis.

    This is a bounded original-quotation priority, not a generated meaning or
    evidence approval. Unmeasured amino acids and immune/disease background
    cannot establish this pathway's role; every selected sentence still uses
    the ordinary author and two independent original/quotation reviews.
    """
    index = next(index for index, row in enumerate(PATHWAY_DEFINITIONS) if row[0] == 'P4')
    if tuple(target_path) != ('functionalPathways', 'pathways', index, 'healthMeaning'):
        return None
    contexts = [_mapping(row) for row in assessment_context]
    allowed = {fid for row in contexts for fid in row.get('factIds', []) if isinstance(fid, str)}
    if not allowed:
        return None
    rows = facts.values() if isinstance(facts, dict) else facts
    members = {}
    for raw in rows:
        row = _mapping(raw)
        if (row.get('factId', row.get('fact_id')) not in allowed or row.get('kind') != 'function'
                or row.get('value') is None or not str(row['value']).strip()):
            continue
        name = _compact(row.get('name')).casefold()
        for code, aliases in AMINO_ACID_ALIASES.items():
            if any(name == _compact(alias).casefold() for alias in aliases):
                members[code] = {'sourceName': row['name'], 'aliases': tuple(aliases)}
    if not members:
        return None
    candidates, originals, identities, conflicts = [], {}, {}, set()
    from modules.report.evaluation_content import function_research_use_failures
    for raw in records:
        record = _mapping(raw)
        number, source = record.get('number'), record.get('sourceText')
        if type(number) is not int or number <= 0 or not isinstance(source, str) or not source.strip():
            continue
        identity = (record.get('title'), source)
        if number in identities and identities[number] != identity:
            conflicts.add(number)
            continue
        identities[number] = identity
        for quote_id, quote in _review_quote_options(source).items():
            matched = [member['sourceName'] for member in members.values()
                if any(_literal_object_in_quote(alias, quote) for alias in member['aliases'])]
            if not matched or not _AMINO_BASIC_ROLE.search(quote):
                continue
            if function_research_use_failures(tuple(target_path), source, quote, subject_age=subject_age):
                continue
            candidates.append({'number': number, 'quoteId': quote_id, 'originalQuote': quote,
                'matchedSourceObjects': matched})
            originals[number] = deepcopy(record)
    candidates = [row for row in candidates if row['number'] not in conflicts]
    candidates.sort(key=lambda row: (
        -bool(re.search(r'protein\s+(?:synth\w*|biosynth\w*)|蛋白(?:质)?合成',
            row['originalQuote'], re.I)),
        -len(row['matchedSourceObjects']), row['number'], int(row['quoteId'][1:])))
    candidates = candidates[:8]
    if not candidates:
        return None
    preferred = list(dict.fromkeys(row['number'] for row in candidates))
    return {'kind': 'amino_acid_function', 'sourceMemberNames': [row['sourceName'] for row in members.values()],
        'originalSentenceCandidates': candidates, 'preferredNumbers': preferred,
        'evidenceRecords': [originals[number] for number in preferred],
        'instruction': '优先从本计划同一实际氨基酸成员的原句，选择一条蛋白合成、明确前体或其他已列基础作用，'
            '忠实写成一条独立有引用的功能意义。候选只提示原文位置，不是审批或固定正文。'
            '保留原研究对象、人群和实验范围，不把色氨酸的作用扩成所有氨基酸，也不把其代谢物作用反归母体。'
            '不以泛免疫、肠脑轴重要性、疾病相关或干预效果代替本通路的基础作用；没有适用原句时不补写。'
            '完整原文和quoteId保持原样，照常由作者与原文/选定引句两轮独立审核，不自动填入任何结论。'}


def _transmitter_sentence_role(quote, aliases):
    """Find a literal transmitter's own role, not a mixed metabolite list."""
    text = re.sub(r'\([^)]*[,、][^)]*\)|（[^）]*[,、][^）]*）', '', quote)
    physiological = re.compile(r'运动|蠕动|分泌|睡眠|情绪|疼痛|饥饿|受体|神经活动|信号传递|'
        r'\b(?:motility|secretory|secretion|sleep|emotion\w*|pain|hunger|appetite|'
        r'receptors?|neural|neuronal|signall?ing|immune)\b', re.I)
    for alias in aliases:
        for match in re.finditer(re.escape(alias), text, re.I):
            if not _literal_object_in_quote(alias, match[0]):
                continue
            if (alias.isascii() and (match.start() and text[match.start()-1].isascii() and text[match.start()-1].isalnum()
                    or match.end() < len(text) and text[match.end()].isascii() and text[match.end()].isalnum())):
                continue
            tail = text[match.end():]
            role = re.match(r'\s*(?:,\s*)?(?:(?:which|that)\s+)?'
                r'(?:(?:can|may|could)\s+)?(?:is|are|acts?|regulat\w*|modulat\w*|'
                r'bind\w*|inhibit\w*|stimulat\w*|activat\w*|mediates?|signals?)\b', tail, re.I)
            if role and physiological.search(tail[role.end():role.end()+260]):
                return 2, alias
            if re.match(r'(?:可|能够|能|在[^，,。；;]{0,15})?(?:参与|调节|调控|结合|抑制|激活|介导)', tail) \
                    and physiological.search(tail[:180]):
                return 2, alias
            production = r'synthesi[sz]\w*|biosynth\w*|produc\w*|convert\w*|decarboxyl\w*|合成|生成|产生|转化|脱羧'
            before = text[max(0, match.start()-90):match.start()]
            if re.search(r'(?:' + production + r')[^.;。；]{0,45}$', before, re.I) or re.match(
                    r'\s*(?:(?:is|can be|may be)\s+)?(?:' + production + r')[^.;。；]{0,35}'
                    r'(?:from|by|via|由|通过|底物|催化)', tail, re.I):
                return 1, alias
    return 0, None


def build_neurotransmitter_function_reference_plan(target_path, assessment_context, facts, records, *,
        subject_age=None) -> dict | None:
    """Offer bounded existing originals for the owner's measured transmitters."""
    path = tuple(target_path)
    index = next(i for i, row in enumerate(PATHWAY_DEFINITIONS) if row[0] == 'P6')
    if path not in {('functionalPathways', 'pathways', index, 'healthMeaning'),
            ('nutritionAssessment', 'neurotransmitterInterpretation', 'text')}:
        return None
    contexts = [_mapping(row) for row in assessment_context]
    observed = [row for row in contexts if row.get('name') and row.get('value') not in (None, '')
        and row.get('factIds')]
    rows = facts.values() if isinstance(facts, dict) else facts
    known = {row.get('factId', row.get('fact_id')): row for raw in rows if (row := _mapping(raw))}
    members = {}
    for row in observed:
        if not any(fid in known and known[fid].get('kind') == 'function'
                and known[fid].get('value') not in (None, '') for fid in row['factIds']):
            continue
        for code, name in NEUROTRANSMITTER_DEFINITIONS:
            aliases = FIXED_METRIC_ALIASES.get(code, (name,))
            if any(_compact(row['name']).casefold() == _compact(alias).casefold() for alias in aliases):
                members[row['name']] = aliases
    if not members:
        return None
    from modules.report.evaluation_content import function_research_use_failures
    candidates, originals, identities, conflicts = [], {}, {}, set()
    for raw in records:
        record = _mapping(raw)
        number, source = record.get('number'), record.get('sourceText')
        if type(number) is not int or number <= 0 or not isinstance(source, str) or not source.strip():
            continue
        identity = (record.get('title'), source)
        if number in identities and identities[number] != identity:
            conflicts.add(number)
            continue
        identities[number] = identity
        for quote_id, quote in _review_quote_options(source).items():
            roles = [(name, *_transmitter_sentence_role(quote, aliases)) for name, aliases in members.items()]
            matched = [name for name, role, _ in roles if role]
            if not matched or function_research_use_failures(path, source, quote, subject_age=subject_age):
                continue
            candidates.append(dict(number=number, quoteId=quote_id, originalQuote=quote,
                matchedSourceObjects=matched, originalSubjectAliases=[alias for _, role, alias in roles if role],
                rolePriority=max(role for _, role, _ in roles)))
            originals[number] = deepcopy(record)
    candidates = [row for row in candidates if row['number'] not in conflicts]
    candidates.sort(key=lambda row: (-row['rolePriority'], row['number'], int(row['quoteId'][1:])))
    candidates = [{key: value for key, value in row.items() if key != 'rolePriority'} for row in candidates[:8]]
    if not candidates:
        return None
    preferred = list(dict.fromkeys(row['number'] for row in candidates))
    return dict(kind='neurotransmitter_function', sourceMemberNames=list(members),
        originalSentenceCandidates=candidates, preferredNumbers=preferred,
        evidenceRecords=[originals[number] for number in preferred],
        instruction='只选本栏实际递质成员的原文明示生成过程或独立生理作用。which/that从句明确以该递质为主语时，'
            '可忠实写成该递质的独立作用句，不必同时翻译同句上游前体；保持真实主语、研究范围及强度。'
            '不以泛色氨酸/混合代谢物免疫背景或仅括号名单替代该递质作用；不补个人效应。'
            '候选不是审批或固定正文；完整原文及选定引句仍分别独立审核。')


_MEASURED_FUNCTION_OBJECTS = {
    'P1': (('galactose', ('半乳糖', 'galactose')),
        ('starchSucrose', ('淀粉和蔗糖', '淀粉/蔗糖', '淀粉', '蔗糖', 'starch and sucrose', 'starch', 'sucrose'))),
    'P9': (('lipopolysaccharide', ('脂多糖', 'lipopolysaccharide', 'lipopolysaccharides', 'LPS')),
        ('homocysteine', ('同型半胱氨酸', 'homocysteine', 'HCY'))),
}


def measured_function_members(code, observations, facts):
    """Finite printed synonyms identify only actual nonempty owner objects."""
    rows = facts.values() if isinstance(facts, dict) else facts
    known = {row.get('factId', row.get('fact_id')): row for raw in rows if (row := _mapping(raw))}
    result = []
    for raw in observations:
        row = _mapping(raw)
        if not row.get('name') or row.get('value') in (None, '') or not any(
                fid in known and known[fid].get('kind') == 'function'
                and known[fid].get('value') not in (None, '') for fid in row.get('factIds', [])):
            continue
        for identity, aliases in _MEASURED_FUNCTION_OBJECTS.get(code, ()):
            if any(_compact(row['name']).casefold() == _compact(alias).casefold() for alias in aliases):
                result.append(dict(identity=identity, sourceName=row['name'], aliases=aliases))
    return result


def build_measured_function_component_bindings(target_path, assessment_context, facts, source_text, claim):
    """Describe literal identity only; never approve a claim or its physiology.

    The printed combined assay can contain one independently named component.
    Both the actual owner fact and this item's original must bind that component.
    Values/grades and previous review verdicts are deliberately absent.
    """
    path = tuple(target_path)
    if not (path == ('functionalPathways', 'pathways', 0, 'healthMeaning')
            or path[:2] == ('nutritionAssessment', 'detailedMetabolism')
            and path[-2:] == ('explanation', 'text')):
        return None
    if not isinstance(source_text, str) or not source_text.strip() or not isinstance(claim, str):
        return None
    members = measured_function_members('P1', assessment_context, facts)
    components = {'galactose': (('半乳糖', ('半乳糖', 'galactose')),),
        'starchSucrose': (('淀粉', ('淀粉', 'starch')), ('蔗糖', ('蔗糖', 'sucrose')))}
    quotes = _review_quote_options(source_text)
    bindings, unclaimed, unsupported = [], [], []
    for member in members:
        for name, aliases in components[member['identity']]:
            if not any(_literal_object_in_quote(alias, claim) for alias in aliases):
                unclaimed.append(name)
                continue
            original_aliases = [alias for alias in aliases if _literal_object_in_quote(alias, source_text)]
            quote_ids = [qid for qid, quote in quotes.items()
                if any(_literal_object_in_quote(alias, quote) for alias in original_aliases)]
            if not quote_ids:
                unsupported.append(name)
                continue
            bindings.append(dict(measuredSourceName=member['sourceName'], sourceComponentName=name,
                sourceComponentAliases=list(aliases), originalAliases=original_aliases,
                originalQuoteIds=quote_ids))
    if not bindings or unsupported:
        return None
    return dict(kind='measured_component_identity', bindings=bindings,
        unclaimedSourceComponents=list(dict.fromkeys(unclaimed)),
        coverageRule='这些字段只证明本栏实际非空测项与本句明确成分的字面身份，不批准任何医学断言。'
            '复合测项中的一个真实成分可以独立解释；本句未断言其他成分时，不要求同句覆盖未陈述成员。'
            '原文只支持淀粉时，仅核淀粉的实际作用，不能将其作用归给蔗糖、半乳糖或整组。'
            '每个实际谓语及范围仍须由本item原文独立支持，不能因本绑定、首审或同篇另句而豁免。')


_RESEARCH_PURPOSE_SENTENCE = re.compile(r'\b(?:we\s+(?:investigat\w*|examin\w*|question\w*|test\w*)|'
    r'(?:the\s+)?(?:aim|objective|purpose)\s+(?:of|is|was)|whether|'
    r'(?:are|is)\s+reviewed)\b|研究目的|旨在|考察.{0,30}(?:标志物|指标)', re.I)


def _measured_sugar_alias_definitions(quote, aliases, source):
    """Read a unique abbreviation from at most two preceding source sentences.

    The returned original sentence remains a required evidence handle. No
    quote or source is expanded, and another paper cannot supply a definition.
    """
    if not source or not quote or quote not in source:
        return []
    options = list(_review_quote_options(source).items())
    current = next((index for index, (_, text) in enumerate(options) if text == quote),None)
    if current is None:
        return []
    definitions = []
    for qid, text in options[max(0,current-2):current]:
        for term in aliases:
            if not isinstance(term,str) or not term.isascii():
                continue
            pattern = r'(?<![A-Za-z0-9_])(?i:(?:resistant\s+)?'+re.escape(term)+r')\s*\(([A-Z][A-Z0-9]{1,5})\)'
            for match in re.finditer(pattern,text):
                abbreviation=match[1]
                # A different definition of the same short token in this
                # original is ambiguous, even when one expansion is familiar.
                expansions=re.findall(r'([A-Za-z][A-Za-z -]{0,65})\s*\('+re.escape(abbreviation)+r'\)',source)
                if not expansions or any(not any(re.search(re.escape(alias)+r'\s*$',expansion,re.I)
                        for alias in aliases) for expansion in expansions):
                    continue
                if _literal_object_in_quote(abbreviation,quote):
                    row=dict(quoteId=qid,originalQuote=text,alias=abbreviation,
                        sourceObject=match.group().rsplit('(',1)[0].strip())
                    if row not in definitions:
                        definitions.append(row)
    return definitions


def _measured_function_sentence_priority(code, quote, aliases, *, source=''):
    """Prefer the measured object's predicate, never purpose/list proximity."""
    if _RESEARCH_PURPOSE_SENTENCE.search(quote):
        return 0
    text = re.split(r'\bAbbreviations\s*:', quote, maxsplit=1, flags=re.I)[0]
    text = re.sub(r'\([^)]*[,、][^)]*\)|（[^）]*[,、][^）]*）', '', text)
    sugar = r'分解|水解|利用|转化|发酵|降解|代谢|\b(?:degrad\w*|hydrolys\w*|utili[sz]\w*|convert\w*|ferment\w*|cataboli\w*|metaboli[sz]\w*)\b'
    effect = r'激活|刺激|诱导|损伤|调节|抑制|结合|触发|增加|降低|\b(?:activat\w*|stimulat\w*|induc\w*|disrupt\w*|regulat\w*|inhibit\w*|bind\w*|trigger\w*|increas\w*|reduc\w*)\b'
    impact = r'炎症|屏障|通透性|免疫|受体|信号|\b(?:inflamm\w*|barrier|permeability|immune|receptors?|TLR\d*|signall?ing)\b'
    definitions=_measured_sugar_alias_definitions(quote,aliases,source) if code=='P1' else []
    literal_aliases=list(dict.fromkeys([*aliases,*(row['alias'] for row in definitions)]))
    for alias in literal_aliases:
        if not _literal_object_in_quote(alias, text):
            continue
        for clause in re.split(r'[。；;!?\n]|\.(?=\s+[A-Z]|$)', text):
            if not _literal_object_in_quote(alias, clause):
                continue
            for match in re.finditer(re.escape(alias), clause, re.I):
                before, tail = clause[max(0, match.start()-70):match.start()], clause[match.end():]
                if code == 'P1':
                    direct = re.match(r'\s*(?:\([^)]*\)\s*)?(?:(?:is|are|can be|may be)\s+)?(?:'
                        + sugar + ')', tail, re.I)
                    substrate = re.search(r'(?:' + sugar + r')\s+(?:of\s+)?(?:[\w-]+\s+){0,3}$', before, re.I)
                    # Literal parallel food subjects may share one passive
                    # predicate. Do not confuse a later sentence, a research
                    # aim, a noun list or another actor's action with it.
                    food=r'(?:protein|fib(?:er|re)|nonstarch polysaccharides?|polysaccharides?|starch|sucrose|galactose)'
                    coordinate=r'(?:\s*(?:,\s*)?(?:and|as well as)\s+'+food+r'\s*,?){1,3}'
                    passive=(r'(?:is|are|was|were|can be|may be)|remains?\s+unabsorbed'
                        r'(?:\s+in\s+(?:the\s+)?(?:small|large)\s+intestine)?\s+and\s+(?:is|are)')
                    parallel=re.match(coordinate+r'\s+(?:'+passive+r')\s+(?:'+sugar+r')',tail,re.I)
                    if direct or substrate or parallel:
                        return 3
                elif re.search(impact, clause, re.I):
                    # A cell's response to the actual metabolite is a valid
                    # process with that cell as actor. Prefer this explicit
                    # response over a coordinated "induce X and Y production"
                    # phrase; it does not make the metabolite produce cytokines.
                    cell_response = re.search(r'\b(?:cells?|epithelium)\b[^.;]{0,60}'
                        r'\brespond\w*\s+to\b[^.;]{0,35}$', before, re.I)
                    response_effect = re.match(r'\s*(?:in\s+[^,;]{0,45}\s+manner\s*,?\s*)?'
                        r'(?:,\s*)?(?:(?:by|thereby)\s+)?(?:' + effect + r'|produc\w*)', tail, re.I)
                    if cell_response and response_effect:
                        return 4
                    direct = re.match(r'\s*(?:\([^)]*\)\s*)?(?:from\s+[^,;]{1,55}?\s+)?'
                        r'(?:(?:is|are|can|may|could)\s+)?'
                        r'(?:(?:has|have|had)\s+(?:individual\s+)?(?:capacit(?:y|ies)|abilit(?:y|ies))\s+to\s+)?'
                        r'(?:' + effect + ')', tail, re.I)
                    if direct:
                        return 3
    return 0


def measured_function_sentence_role(code, quote, aliases, *, source=''):
    return bool(_measured_function_sentence_priority(code, quote, aliases,source=source))


def measured_function_source_priority(code, title, source, aliases):
    """Rank actual human sugar-role passages; never approve their use."""
    if code!='P1' or not any(measured_function_sentence_role(code,quote,aliases,source=source)
            for quote in _review_quote_options(source).values()):
        return 0
    from .evaluation_content import measured_sugar_study_scope_failures
    if measured_sugar_study_scope_failures(source,title=title):
        return -16
    human_process=any(re.search(r'\bhuman\b[^.!?\n]{0,45}\b(?:colonic|intestinal|gut)\b|'
        r'\bhuman\s+(?:colonic|intestinal|gut)\s+bacteria\b',quote,re.I)
        and _BIOLOGICAL_PROCESS.search(quote) for quote in _review_quote_options(source).values())
    human_scope=re.search(r'\bhumans?\b|人体|人类',title,re.I) or re.search(
        r'\b(?:ileostomy subjects|human subjects|human participants|healthy volunteers)\b',source,re.I)
    return 12*bool(human_process or human_scope)


def _original_scope_sentences(source):
    """Literal current-original scope handles, not inferred applicability."""
    scope = re.compile(r'\b(?:mice|mouse|murine|rats?|in vitro|cell (?:line|monolayer)s?|'
        r'cellular model|HEK[- ]?TLR4|Caco-?2|subjects?|volunteers?|participants?)\b|'
        r'小鼠|大鼠|体外|细胞系|受试者', re.I)
    return [dict(quoteId=qid, originalQuote=text) for qid, text in _review_quote_options(source).items()
        if scope.search(text)][:4]


def build_measured_object_function_reference_plan(target_path, assessment_context, facts, records, *,
        subject_age=None) -> dict | None:
    """Bounded original-role candidates for measured sugars or metabolites."""
    path = tuple(target_path)
    harmful_context = next((row for row in assessment_context if isinstance(row, dict)
        and row.get('semanticType') == 'nativeNutritionState'
        and row.get('panel') == 'yingyang2_yhdxw2'), None)
    code = ('P9' if harmful_context is not None
        and path[:2] == ('nutritionAssessment', 'detailedMetabolism')
        and path[-2:] == ('explanation', 'text') else 'P1' if path == ('functionalPathways', 'pathways', 0, 'healthMeaning')
        or path[:2] == ('nutritionAssessment', 'detailedMetabolism') and path[-2:] == ('explanation', 'text')
        else 'P9' if path == ('functionalPathways', 'pathways', 8, 'healthMeaning') else None)
    if code is None:
        return None
    members = measured_function_members(code, assessment_context, facts)
    if not members:
        return None
    from modules.report.evaluation_content import function_research_use_failures
    candidates, originals, identities, conflicts = [], {}, {}, set()
    for raw in records:
        record = _mapping(raw)
        number, source = record.get('number'), record.get('sourceText')
        if type(number) is not int or number <= 0 or not isinstance(source, str) or not source.strip():
            continue
        identity = (record.get('title'), source)
        if number in identities and identities[number] != identity:
            conflicts.add(number)
            continue
        identities[number] = identity
        for quote_id, quote in _review_quote_options(source).items():
            if (harmful_context is not None and harmful_context.get('sourceStatus') == '正常'
                    and not basic_metabolite_sentence_role(quote)):
                continue
            matched = [member['sourceName'] for member in members
                if measured_function_sentence_role(code, quote, member['aliases'],source=source)]
            if not matched or function_research_use_failures(path, str(record.get('title')or'')+'\n'+source,
                    quote, facts=facts, subject_age=subject_age):
                continue
            definitions=_measured_sugar_alias_definitions(quote,
                [alias for member in members for alias in member['aliases']],source) if code=='P1' else []
            candidates.append(dict(number=number, quoteId=quote_id, originalQuote=quote,
                rolePriority=max(_measured_function_sentence_priority(code, quote, member['aliases'],source=source)
                    for member in members)+measured_function_source_priority(code,
                        str(record.get('title')or''),source,[alias for member in members for alias in member['aliases']]),
                originalScopeSentences=_original_scope_sentences(source),
                **({'originalObjectDefinitions':definitions} if definitions else {}),
                matchedSourceObjects=matched, originalObjectAliases=list(dict.fromkeys(alias
                    for member in members if member['sourceName'] in matched for alias in member['aliases']
                    if _literal_object_in_quote(alias, quote)))))
            originals[number] = deepcopy(record)
    candidates = [row for row in candidates if row['number'] not in conflicts]
    candidates.sort(key=lambda row: (-row['rolePriority'], row['number'], int(row['quoteId'][1:])))
    candidates = [{key: value for key, value in row.items() if key != 'rolePriority'} for row in candidates[:8]]
    if not candidates:
        return None
    preferred = list(dict.fromkeys(row['number'] for row in candidates))
    return dict(kind='measured_sugar_function' if code == 'P1' else 'measured_metabolite_function',
        sourceMemberNames=list(dict.fromkeys(row['sourceName'] for row in members)),
        originalSentenceCandidates=candidates, preferredNumbers=preferred,
        evidenceRecords=[originals[number] for number in preferred],
        instruction=('仅忠实写一条实际糖项的利用、分解或发酵过程及作用；不以其他纤维或短链脂肪酸的作用代替。'
            if code == 'P1' else '仅忠实写实际同型半胱氨酸或脂多糖其中一项的独立生理作用；不借其他菌种或毒素。')
            + '只选一个同对象作用即可，无需覆盖所有成员；原句研究范围、主语和强度须保留。'
            '复合测项仅解释原句实际涉及的成分，不把其中一个成分的作用扩成并列全部成分。'
            'originalScopeSentences是同篇真实研究范围句，动物、体外及细胞模型范围不得省略成人体结论。'
            'originalObjectDefinitions只列本篇邻近原句明确的简称定义；使用简称的作用句时须同时选其定义原句，保留具体底物范围，不将抗性淀粉扩成所有糖。'
            '不从原聚合正常或分数推断个体高低、人体含量、疾病或干预效果。'
            '候选仅定位当前取回的原文，不是审批或固定正文，仍须作者新写及原文首审、选句复审。')


def build_health_function_reference_plan(target_path, assessment_context, facts, records, *, subject_age=None) -> dict | None:
    """Prioritize exact original sentences, without approving a medical claim.

    Only literal supplied members/genes (or this indicator's own domain) are
    search anchors. Q handles come from the same lossless catalog as the two
    existing citation reviews. Full original records remain in the packet so
    study organism, strain, population and experiment are still reviewable.
    """
    path = tuple(target_path)
    if not (len(path) == 4 and path[0] == "microbiomeAssessment"
            and path[1] in {"healthMetrics", "antimicrobialResistance"}
            and type(path[2]) is int and path[-1] in {"explanation", "interpretation"}):
        return None

    contexts = [_mapping(row) for row in assessment_context]
    rows = facts.values() if isinstance(facts, dict) else facts
    fact_rows = [_mapping(row) for row in rows]
    allowed_ids = {fid for row in contexts for key in ("factIds", "memberFactIds")
        for fid in row.get(key, []) if isinstance(fid, str)}
    if allowed_ids:
        fact_rows = [row for row in fact_rows if row.get("factId", row.get("fact_id")) in allowed_ids]
    texts = [str(row.get("value") or "") for row in [*contexts, *fact_rows]]
    genes = []
    for text in texts:
        for match in re.finditer(r"(?:^|[；;])\s*检出基因[：:]\s*([^；;\n]+)", text):
            _append_unique(genes, [gene for gene in re.split(r"[,，、\s]+", match[1])
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", gene)])
    members = []
    for row in fact_rows:
        if row.get("kind") != "microbe":
            continue
        _append_unique(members, [name.strip() for name in str(row.get("name") or "").split(" / ")
            if re.fullmatch(r"[A-Z][a-z]+(?:\s+[a-z][a-z.-]*){0,3}", name.strip())])
    names = " ".join(str(row.get("name") or "") for row in contexts)
    domains = [alias for pattern, aliases in _HEALTH_DOMAINS if re.search(pattern, names) for alias in aliases]
    scfa_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "scfaProducerBacteria" for row in contexts)
    opportunistic_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "opportunisticBacteria" for row in contexts)
    beneficial_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "beneficialBacteria" for row in contexts)
    anti_inflammatory_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "antiInflammatoryCapacity" for row in contexts)
    immune_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "gutImmunity" for row in contexts)
    pathogen_panel = any(row.get("semanticType") == "healthSummary"
        and row.get("code") in {"pathogenicBacteria", "pathogenicFungi", "pathogenicViruses",
            "pathogenicParasites", "barrierRelatedBacteria"}
        for row in contexts)
    barrier_group = any(row.get("semanticType") == "healthSummary"
        and row.get("code") == "barrierRelatedBacteria" for row in contexts)
    objects = genes or (list(dict.fromkeys([*domains, *members])) if scfa_group else members or domains)
    if not objects:
        return None

    candidates, originals, identities, conflicts = [], {}, {}, set()
    for raw in records:
        record = _mapping(raw)
        number, source = record.get("number"), record.get("sourceText")
        if type(number) is not int or number <= 0 or not isinstance(source, str) or not source.strip():
            continue
        identity = (record.get("title"), source)
        if number in identities and identities[number] != identity:
            conflicts.add(number)
            continue
        identities[number] = identity
        for quote_id, quote in _review_quote_options(source).items():
            if re.search(r'\bstarvat\w*|glucose.{0,16}(?:deplet\w*|exhaust\w*)|'
                    r'nutrient.{0,12}(?:limit\w*|deplet\w*)|饥饿|葡萄糖耗尽|营养耗竭', quote, re.I):
                continue  # Culture survival is not this user's health implication.
            if re.search(r'\b(?:brain|cerebral|CNS)\b|脑组织|入脑|脑内', quote, re.I) and re.search(
                    r'\b(?:mice|mouse|murine|germ.free)\b|小鼠|无菌鼠', source, re.I):
                continue  # An unrelated animal-organ outcome cannot fill a gut panel.
            matched = [term for term in objects if _source_object_in_quote(term, quote, source,
                title=str(record.get('title') or ''))]
            if scfa_group and not scfa_production_sentence_role(quote, members):
                continue
            if (pathogen_panel or opportunistic_group) and not basic_pathogen_sentence_role(quote):
                continue
            if immune_group:
                if not _immune_effector_sentence_priority(quote):
                    continue
                # A specific Treg/Foxp3/receptor process is an immune-domain
                # object even when the sentence does not repeat "immune".
                matched = matched or [term for term in domains if re.search(r'immune|immunity|immunomodulation', term)]
            if not matched or not (_BIOLOGICAL_PROCESS.search(quote)
                    or immune_group and _immune_effector_sentence_priority(quote)
                    or opportunistic_group and _OPPORTUNISTIC_PROCESS.search(quote)
                    or pathogen_panel and (basic_pathogen_sentence_role(quote)
                        or _PATHOGEN_HEALTH_PROCESS.search(quote))):
                continue
            if barrier_group and not barrier_function_process(quote):
                # Generic colonization resistance, research background or
                # AIEC-health association is not the physical epithelial/
                # mucosal-barrier duty of this particular native panel.
                continue
            if beneficial_group and not _concrete_beneficial_process(quote):continue
            if beneficial_group:
                # Do not repeatedly offer a clinical-benefit sentence which
                # the ordinary purpose gate will reject later. Other basic
                # biological statements from that same original stay usable.
                from modules.report.evaluation_content import function_research_use_failures
                if function_research_use_failures(path, source, quote, subject_age=subject_age):
                    continue
            candidate={"number": number, "quoteId": quote_id, "originalQuote": quote,
                "matchedSourceObjects": matched}
            if immune_group or scfa_group:
                candidate['originalScopeSentences'] = _original_scope_sentences(source)
            bindings=[{'targetObject':member,'sourceObject':scope,'quoteAlias':alias}
                for member in matched for alias,scope in _explicit_object_aliases(member,source)
                if _literal_object_in_quote(alias,quote)]
            if bindings:candidate['sourceObjectBindings']=bindings
            candidates.append(candidate)
            originals[number] = deepcopy(record)
    candidates = [row for row in candidates if row["number"] not in conflicts]
    candidates.sort(key=lambda row: (-bool(re.search(r'intestinal inflammation|gut inflammation|肠道炎症',
        row["originalQuote"], re.I)) if anti_inflammatory_group else 0,
        -_immune_effector_sentence_priority(row['originalQuote']) if immune_group else 0,
        -_scfa_group_sentence_priority(row["originalQuote"], domains)
        if scfa_group else 0, -bool(_OPPORTUNISTIC_PROCESS.search(row["originalQuote"]))
        if opportunistic_group else 0, -health_member_function_priority(objects,
        str(originals[row["number"]].get("title") or ""), originals[row["number"]]["sourceText"],
        subject_age=subject_age if beneficial_group else None),
        -bool(re.search(r'\b(?:produce|produces|produced|producing|'
        r'ferment\w*|cataboli\w*|consum\w*|utili[sz]\w*|metaboli\w*|catalys\w*|catalyz\w*)\b',
            row["originalQuote"], re.I)) if beneficial_group else 0,
        -len(row["matchedSourceObjects"]), row["number"],
        int(row["quoteId"][1:])))
    candidates = candidates[:8]
    if not candidates:
        return None
    preferred = list(dict.fromkeys(row["number"] for row in candidates))
    return {"kind": "health_function", "originalSentenceCandidates": candidates, "preferredNumbers": preferred,
        **({'immuneOriginalSentenceCandidates': deepcopy(candidates),
            'immuneFunctionInstruction': '优先忠实译述所选原句明确的作用介质与免疫靶细胞、受体或分化过程，'
                '不以研究假说或泛说调节免疫平衡替代具体作用。originalScopeSentences只包含同篇真实范围句；'
                '原文明确mice、小鼠、体外、细胞系等范围时，整句明确保留该范围，不省略成普遍人体作用，'
                '不将实验现象推成本人免疫状态或干预效果。仍须新作者完整写句并接受原文首审与选句复审。'}
            if immune_group else {}),
        **({'productionOriginalSentenceCandidates': [deepcopy(row) for row in candidates
            if scfa_production_sentence_role(row['originalQuote'], members)],
            'productionInstruction': '优先忠实译述一条原文明示的底物发酵、代谢生成过程或同成员作用即可。'
                '同一原句含发酵生成过程与小鼠Treg等下游作用时，只依据原文重新写生产过程，'
                '仍保留同篇原文明确的研究范围，不拼产物免疫效应或补充干预假说。'
                '逐字保持原句的菌类和产物粒度：细菌及短链脂肪酸不能擅自具体化为产丁酸菌及丁酸。'
                '无需再拼接另一条产物的人体作用，不要求一句覆盖所有成员；仍接受完整原文与选句双审。'}
            if scfa_group else {}),
        "evidenceRecords": [originals[number] for number in preferred],
        "instruction": "优先从originalSentenceCandidates选择与本项真实成员或基因同句的具体生物学过程，忠实译述为独立完整的中文句。"
            "sourceObjectBindings仅解释同篇原文明示的别名，sourceObject中的菌株/亚种限定必须保留，不将其作用扩成整菌种或本人实测功能。"
            "quoteId与originalQuote是现有原文目录的句柄，不是适用性或引文审批；仍须结合完整sourceText保留原研究对象、菌株与实验范围，"
            "并通过原文及选定引句双轮审核。优先使用本计划实际列出的基础作用句；同篇其他临床补充干预疗效句不得代替该基础作用。"
            "不得将一般研究机制改成本人感染、毒株性质、健康损伤或药敏结论。"}
