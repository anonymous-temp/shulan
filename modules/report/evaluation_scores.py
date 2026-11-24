"""Validate model-authored overview scores without manufacturing measurements.

The composer supplies the actual model response. This module performs no model
or network calls, calculates no health score and has no cross-request cache.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import unicodedata

from modules.report.schemas import ClinicalReportBlock
from modules.report import evaluation_catalog as catalog
from modules.report.evaluation_schema import SourceReference, TaxonSignal
from modules.report.evaluation_grades import grade_statement_failures
from modules.report.evaluation_clinical_scope import aging_clinical_facts
from modules.report.evaluation_result_rules import has_risk_index
from modules.report.evaluation_priority import is_harmful_generation_metric


AI_SCORE_PREFIX = "AI综合评估分"
AI_MAJOR_SCORE_PREFIX = "AI综合评分："
AI_LEVEL_PREFIX = "AI综合评级："
SCORE_BLOCK_CODE = "eval:assessmentScores"
LEVEL_BLOCK_CODE = "eval:assessmentLevels"
SCORE_LEVELS = {"正常", "注意", "中风险", "高风险"}
_LEGACY_PREFIXES = ("本报告医学状态覆盖分", "营养与功能综合状态覆盖分", "菌群固定指标综合状态覆盖分")
_MAJOR_SCORE_CODES = {"nutrition:" + code for code in ("carbohydrate", "fat", "protein")}
_MAJOR_PATHWAYS = {"nutrition:carbohydrate": "P1", "nutrition:fat": "P2", "nutrition:protein": "P4"}
_INDEX_RISK_LEVELS = ("低风险", "中低风险", "中风险", "中高风险", "高风险")


@dataclass(frozen=True)
class _AssessmentMetadataReview:
    """Request-private approval; never reconstructed from serialized rows."""
    fingerprint: str


def _metadata_review_fingerprint(document, row, target):
    from .evaluation_metadata_review import NUMERIC_OVERVIEW_CODES, requires_independent_assessment_review
    code = row.get('code') if isinstance(row, dict) else None
    if (code != target.get('code') or not isinstance(code, str)
            or not requires_independent_assessment_review(code)):
        return None
    if code in NUMERIC_OVERVIEW_CODES and any(row.get(key) not in (None, '')
            for key in ('judgment', 'explanation')):
        return None
    facts = {fact.fact_id: fact for fact in document.facts}
    allowed = target.get('allowedFactIds') or []
    if (not allowed or not set(allowed) <= facts.keys()
            or target.get('sourceFactDigest') != _source_fact_digest(allowed, facts)
            or not isinstance(target.get('sourceDigest'), str)):
        return None
    ids = row.get('factIds')
    if (not isinstance(ids, list) or not ids or not all(isinstance(fid,str) for fid in ids)
            or not set(ids) <= set(allowed)):
        return None
    decision = {key: row.get(key) for key in (
        'code','score','assessmentLevel','judgment','explanation')}
    decision['factIds'] = sorted(set(ids))
    decision['sourceDigest'] = target['sourceDigest']
    decision['sourceFactDigest'] = target['sourceFactDigest']
    try:
        return hashlib.sha256(json.dumps(decision,ensure_ascii=False,sort_keys=True,
            allow_nan=False).encode()).hexdigest()
    except (ValueError,TypeError):
        return None


def record_assessment_metadata_review(document, row, target):
    """Called only by the gateway after its actual independent source review.

    The source/decision fingerprint is stored outside the document schema.
    Model JSON, callback fields and saved metadata alone cannot grant it.
    """
    fingerprint = _metadata_review_fingerprint(document,row,target)
    if fingerprint is None:
        return False
    ledger = getattr(document,'_evaluation_metadata_reviews',None)
    if not isinstance(ledger,dict):
        ledger = {}
        object.__setattr__(document,'_evaluation_metadata_reviews',ledger)
    ledger[fingerprint] = _AssessmentMetadataReview(fingerprint)
    return True


def assessment_metadata_reviewed(document, row, target):
    fingerprint = _metadata_review_fingerprint(document,row,target)
    ledger = getattr(document,'_evaluation_metadata_reviews',{})
    proof = ledger.get(fingerprint) if fingerprint and isinstance(ledger,dict) else None
    return isinstance(proof,_AssessmentMetadataReview) and proof.fingerprint == fingerprint


def _saved_numeric_review_available(document, slot):
    """Recognise an AI card for revalidation, never authorize its publication.

    The existing request-private receipt prevents an optional empty caption
    from turning an AI score into a native measurement. Current source/target
    digests are still independently checked by apply_ai_scores below.
    """
    from .evaluation_metadata_review import NUMERIC_OVERVIEW_CODES
    if slot.code not in NUMERIC_OVERVIEW_CODES:
        return False
    ledger = getattr(document, '_evaluation_metadata_reviews', {})
    for row in _load_rows(document, SCORE_BLOCK_CODE, 'scores'):
        if (row.get('code') != slot.code or row.get('score') != slot.score
                or row.get('assessmentLevel') != slot.assessment_level
                or not row.get('sourceDigest') or not row.get('sourceFactDigest')):
            continue
        decision = {key: row.get(key) for key in ('code', 'score', 'assessmentLevel', 'judgment', 'explanation')}
        decision.update(factIds=sorted(set(row.get('factIds', []))),
            sourceDigest=row['sourceDigest'], sourceFactDigest=row['sourceFactDigest'])
        fingerprint = hashlib.sha256(json.dumps(decision, ensure_ascii=False, sort_keys=True,
            allow_nan=False).encode()).hexdigest()
        proof = ledger.get(fingerprint) if isinstance(ledger, dict) else None
        if isinstance(proof, _AssessmentMetadataReview) and proof.fingerprint == fingerprint:
            return True
    return False


def numeric_score_reviewed(data, document, code):
    """Check the present number against its live, full-source review receipt."""
    from .evaluation_metadata_review import NUMERIC_OVERVIEW_CODES
    if code not in NUMERIC_OVERVIEW_CODES:
        return False
    slot = next((slot for slot in data.health_overview.assessment_chart if slot.code == code), None)
    if slot is None or not _saved_numeric_review_available(document, slot):
        return False
    target = next((target for target in build_score_targets(data, document) if target['code'] == code), None)
    if target is None:
        return False
    rows = [row for row in _load_rows(document, SCORE_BLOCK_CODE, 'scores') if row.get('code') == code]
    return len(rows) == 1 and assessment_metadata_reviewed(document, rows[0], target)

_DISEASE_CONTEXT_PATTERNS = {
    'intestinal': r'肠|胃|消化|粪|大便|排便|便秘|腹胀|腹泻|腹痛|排气',
    'metabolic': r'代谢|体重|BMI|腰围|血糖|血脂|血压|葡萄糖|糖化|胰岛素|C肽|C-?peptide|胆固醇|甘油三酯|脂蛋白|尿酸|肝功能|转氨酶|糖尿病|脂肪肝|冠心病|心血管|甲状腺|营养|贫血',
    'mental': r'精神|情绪|心理|焦虑|抑郁|睡眠|入睡|失眠|躁狂|幻觉|自伤|自杀|孤独症|自闭症|发育|语言|交流|互动|游戏',
    'neurological': r'神经|反射|帕金森|阿尔|阿兹|认知|记忆|震颤|麻木|癫痫|脑|步态|颈椎',
    'autoimmune': r'免疫|类风湿|抗核|自身抗体|抗dsDNA|红斑狼疮|强直性|骶髂|关节|感染|过敏|哮喘|湿疹|紫癜|多发性硬化|(?<![A-Za-z])(?:IgE|T1D|T1DM)(?![A-Za-z])',
    'aging': r'骨质疏松|骨密度|骨量|骨代谢|衰老|衰弱|肌少|骨折|跌倒|握力|动脉粥样硬化|认知(?:功能)?下降|frailty|sarcopenia|atherosclerosis|cognitive\s+decline',
}


def disease_context_relevant(code, text):
    """Route explicit diseases first, then retain other actual theme clues.

    Removing a matched identity before the broad symptom search prevents,
    for example, type 1 diabetes becoming type 2 context merely because both
    names contain diabetes. No recognized name establishes a diagnosis.
    """
    remaining = str(text or '')
    routed = set()
    identities = {alias: item_code for item_code, aliases in catalog.FIXED_DISEASE_ALIASES.items()
        for alias in aliases if len(alias) >= 3 or re.search(r'[\u4e00-\u9fff]', alias)}
    for alias in sorted(identities, key=len, reverse=True):
        pattern = re.escape(alias)
        if re.search(r'[A-Za-z]', alias):
            pattern = r'(?<![A-Za-z0-9])' + pattern + r'(?![A-Za-z0-9])'
        if re.search(pattern, remaining, re.I):
            routed.add(catalog.disease_category(alias, code=identities[alias]))
            remaining = re.sub(pattern, ' ', remaining, flags=re.I)
    # A tumour's supplied organ controls its display route. This is context
    # selection, never a tumour diagnosis or a cross-organ risk inference.
    tumor_pattern = r'[^\s，,。；;：:\n]{1,20}(?:癌|肿瘤|间质瘤|结节|息肉|囊肿|腺瘤|肿块|占位)'
    for match in list(re.finditer(tumor_pattern, remaining)):
        routed.add(catalog.disease_category(match.group(), category='tumor'))
    remaining = re.sub(tumor_pattern, ' ', remaining)
    pattern = _DISEASE_CONTEXT_PATTERNS.get(code)
    return code in routed or bool(pattern and re.search(pattern, remaining, re.I))

# Shared by numeric and qualitative metadata requests. These authoring rules
# leave the typed source inventory and all validation gates unchanged.
ASSESSMENT_METADATA_POSITIVE_RULES = (
    "judgment用1至2句概括整体格局、真实组间关系和关注重点；综合分数或等级另填专用字段。",
    "依据分工为：basis选择1至3个代表factId，程序逐项照取项目名及原分级；可选basis.text只补充关系意义，也可省略。",
    "极低、偏低、正常等不同原级并存时，judgment采用弱项集中方向、弱项与保留项并存等上位判断，保持各原级的区别，不把不同原级压成统一单级。",
    "涉及多数、少数、全部、部分等分布描述时，逐个命名组核对assessmentGroupDistributions完整members及原分级计数；各组独立比较，代表basis不替代完整组内资料，计数不用于计算评分。",
    "C来源自报或原报告记载的病史按原出处作为关注背景；judgment和每条basis.text各自保留归属，不写未获独立医院记录支持的明确基础病或既有疾病。既有随访或复诊计划必须有真实原记录；未提供时只给本次关注判断，不补写用户已有随访。",
    "judgment与basis.text直接说明实际状态、组间关系和关注程度；依据的性质由真实项目名、来源属性与原分级表达。",
    "sourceHealthAssociation表示来源中的健康相关菌群信号；‘检出’只修饰该关联信号，不能改称本人病史、基础疾病、原报告记载的疾病背景或医院诊断。只有独立的真实病史条目才可支持病史表述。",
)


def _score_rubric(code):
    """One source-neutral authoring rubric, never a local scoring formula.

    The model still supplies every score. Source file format, tracing IDs and
    the number of duplicated display rows provide no assessment authority.
    """
    criteria = {
        "medical": ["已采用的客观异常程度与重要正常排除信息", "真实主诉、病史及其医学重点"],
        "nutrition": ["各完整营养功能组的保留方向与薄弱方向", "薄弱方向的程度、集中性及跨组关系"],
        "overall": ["重要医学发现及真实病史", "菌群结构与健康状态", "营养功能差异与来源疾病模型信号"],
    }.get(code, ["本类全部真实功能子项的保留方向与薄弱方向", "各子项功能差异及本类整体状态"])
    major = code in _MAJOR_SCORE_CODES
    anchors = ([{"assessmentLevel": level, "scoreAnchor": anchor, "illustrativeRange": interval,
        "stateDescription": description}
        for level, anchor, interval, description in (
            ("弱", 5, [2, 8], "本类主要方向普遍明显受限，整体保留功能很少"),
            ("较弱", 20, [17, 23], "本类薄弱方向主导整体，保留方向局限，整体代谢能力受限"),
            ("正常", 70, [67, 73], "本类主要方向保留；局部较弱或弱项独立提示，不把局部弱项等同整组受限"))] if major else [
        {"assessmentLevel": level, "scoreAnchor": anchor, "illustrativeRange": [anchor - 3, anchor + 3],
            "stateDescription": description}
        for level, anchor, description in (
            ("正常", 90, "已提供维度整体保留，未见需集中关注的明显薄弱方向"),
            ("注意", 65, "保留方向与薄弱方向并存，薄弱处有明确关注重点"),
            ("中风险", 40, "重要维度明显受限，或多个有实际关联依据的薄弱方向共同影响整体状态"),
            ("高风险", 15, "已采用资料支持严重或广泛的状态受损，需要最高关注程度"))])
    return {"version": "source-bound-assessment-v1", "criteria": criteria,
        "scope": "internal_ai_illustration_only", "anchors": anchors,
        "rules": ["仅评价本target完整inventory；先形成整体状态判断，再给出AI综合分数，分数须与该判断的关注程度一致。",
            "先选择本目标实际状态对应的assessmentLevel，再以对应scoreAnchor给出示意分；只有本目标真实差异支持时才在illustrativeRange内小幅调整，不跨档自由打分。相同状态结构使用同一锚点，不因代表basis或措辞变化另起评分尺度。",
            "这些锚点只统一内部AI示意评分，不是疾病风险阈值、原始实测值或临床标准；不写进正文或附录，不覆盖任何原生分数、原级或模板既定分级标准。",
            "相同实际项目、数值、原分级和参考范围采用相同判断准则；PDF或JSON形式、文件名、引用ID、项目排列及其他目标的评分不改变本目标评估。",
            "原始项目的多与少只用于核实分布文字，不按项目数、正常比例、均值、加权平均或分域分数计算综合分。",
            "没有医学事实的维度保持缺失；总体仍按实际已有维度综合评估，不推定缺失维度正常或异常。",
            "健康相关菌群信号和疾病模型结果只按其检测性质使用，不充当本人疾病史或临床疾病严重程度。"]}


def assessment_authoring_inputs(targets, facts):
    """A canonical writer view for pure C nutritional scoring only.

    The returned keys still bind the actual document IDs. Only irrelevant
    file names/dates are omitted from the model view; originals and source
    digests remain untouched for validation and callback provenance. Clinical,
    history and combined overall tasks retain their complete source context.
    """
    known = facts if isinstance(facts, dict) else {fact.fact_id: fact for fact in facts}
    ids = {fid for target in targets for fid in target.get('allowedFactIds', []) if fid in known}
    canonical = bool(targets and ids) and all(target.get('code') == 'nutrition'
        or target.get('kind') == 'major_nutrition' for target in targets) and all(
            known[fid].kind == 'function' for fid in ids) and all(
                source.get('sourceCode') == 'C' for target in targets for item in target.get('inventory', [])
                for source in item.get('sourceRefs', []))
    views = {fid: known[fid].model_dump(mode='json', by_alias=True) for fid in ids}
    if canonical:
        views = {fid: {key: value for key, value in row.items()
            if key not in {'source', 'date', 'abnormal', 'imaging'}} for fid, row in views.items()}
        def identity(fid):
            return json.dumps({key: value for key, value in views[fid].items() if key != 'factId'},
                ensure_ascii=False, sort_keys=True, allow_nan=False)
        ordered = sorted(ids, key=lambda fid: (identity(fid), fid))
    else:
        ordered = sorted(ids)
    return {'orderedFactIds': ordered, 'factsById': views, 'canonicalFunctionView': canonical}


def _score_anchor_failures(row, target):
    """Validate a new writer score; never alter an already stored result."""
    rubric = target.get('scoringRubric')
    if 'score' not in row or not isinstance(rubric, dict) or rubric.get('scope') != 'internal_ai_illustration_only':
        return []
    score, level = row.get('score'), row.get('assessmentLevel')
    anchor = next((item for item in rubric.get('anchors', []) if item.get('assessmentLevel') == level), None)
    if anchor is None:
        return ['新AI示意评分须选择本目标scoringRubric.anchors中的明确等级，不用介于两级的结果']
    bounds = anchor['illustrativeRange']
    if type(score) is not int or not bounds[0] <= score <= bounds[1]:
        return [f'新AI示意评分的{level}须在对应锚点范围{bounds[0]}至{bounds[1]}内自行给分；'
            '按完整本目标资料重新评估，不贴分级边界或借用其他等级评分尺度']
    return []


def _major_judgment_level_failures(row, target):
    if target.get('kind') != 'major_nutrition' or 'assessmentLevel' not in row:
        return []
    text = row.get('judgment')
    if not isinstance(text, str):
        return []
    if re.search(r'介于[^，,。；;]{0,16}(?:较弱|正常|弱)[^，,。；;]{0,8}(?:之间|之间状态)|'
            r'(?:较弱与正常|正常与较弱|弱与较弱)(?:之间|的中间)', text):
        return ['本类综合状态须按实际主要方向选择一个明确等级，不写介于两级；局部弱项另行说明']
    preserved = re.search(r'以正常(?:方向|项目|子项|功能)?为主|主要(?:功能)?方向(?:仍|整体)?保留|'
        r'(?:多数|大部分)(?:方向|项目|子项|功能)(?:仍|尚|保留|处于|为|是)*(?:正常|尚可)', text)
    if preserved and row.get('assessmentLevel') != '正常':
        return ['主判断已确认本类主要方向保留，应选择正常整体级并独立说明局部弱项；不能把局部弱项扩大为整组受限']
    return []

# Match only a complete generated metadata clause. The caller must keep the
# source record untouched and run every other content/source check as usual.
_ASSOCIATION_METHOD_CLAUSE = r"\A(?:属|属于)关联提示而非骨密度(?:或|及|和)临床诊断\Z"
METADATA_INDEPENDENT_METHOD_CLAUSE_PATTERNS = (
    r"\A模型低风险不能替代影像结论\Z",
    r"\A需以临床评估为准\Z",
    r"\A(?:缺少|缺乏|尚无|未提供)[\u4e00-\u9fff]{0,12}模型(?:分级|评分|总分)\Z",
    _ASSOCIATION_METHOD_CLAUSE,
)
_METADATA_PROTECTED_CLAUSE = re.compile(
    r'标本|待核|疑似|可能|阴性|阳性|冲突|不一致|研究|队列|人群|受试者|入组|'
    r'纳入(?!\s*(?:(?:既有|后续|持续|定期)的?)?(?:随访|关注|管理))|治疗|用药|'
    r'(?:不|不能|无法)(?:完全|明确|彻底)?排除')
_METADATA_BOUNDARY_OBJECT = (r'(?:(?:临床|医学|疾病|病理)?(?:诊断|确诊)(?:依据|结论|结果)?|骨密度(?:检查|测定|测量|水平|结果|诊断)?|'
    r'(?:疾病|患病)(?:概率|风险|检出))')
_METADATA_BOUNDARY_SUBJECT = r'[^，,。；;！？\n]*?'
_METADATA_BOUNDARY_CLAUSE = re.compile(_METADATA_BOUNDARY_SUBJECT
    + r'(?:并非|不是|而非|非|不(?:应|能|宜|可|得)?(?:直接|单独|据此|独立|被)*'
    + r'(?:代表|意味着|表示|说明|证明|证实|等于|等同(?:于)?|构成|作为|视为|视作|当作|用作|'
    + r'推定(?:为)?|认定(?:为)?|判定(?:为)?|判断(?:为)?|认为(?:是)?))'
    + r'(?P<boundary_object>(?:(?:本人|个人|患者|受检者|新|独立|直接|明确|实际|已确认|已经|已|当前|真实|人体)(?:的)?)*'
    + _METADATA_BOUNDARY_OBJECT + r'(?:(?:或|及|和|与)' + _METADATA_BOUNDARY_OBJECT + r')*'
    + r'(?:的)?(?:依据|判断|结论)?)')
_METADATA_MISSING_QUALIFIER = r'(?:(?:对应|相关|独立|直接|明确|实际|同向|同组|该方向|本方向|本项|可用|完整)(?:的)?)*'
_METADATA_MISSING_OBJECT = (_METADATA_MISSING_QUALIFIER
    + r'(?:(?:模型)?(?:分值|分数|数值|指数|等级|分级|评分|总分)(?:资料|数据|记录|依据|结果)?|'
    + r'骨密度(?:资料|数据|报告|结果|记录|依据|评估|检测|测量|检查|测定)(?:资料|记录|结果|依据)?)')
_METADATA_MISSING_CLAUSE = re.compile(r'[^，,。；;！？\n]*?'
    + r'(?:无|没有|缺少|缺乏|缺失|尚无|未提供|未给出|未记录)' + _METADATA_MISSING_OBJECT
    + r'(?:(?:或|及|和|与)' + _METADATA_MISSING_OBJECT + r')*')


def _assessment_method_analysis(text, facts):
    if not isinstance(text, str):
        return None
    clause = text.strip().rstrip('。；;！？.!?').strip()
    values = facts.values() if isinstance(facts, dict) else facts
    if (not clause or _METADATA_PROTECTED_CLAUSE.search(clause)
            or any(clause in fact.value for fact in values)):
        return None
    offset = text.find(clause)
    positions = [offset + index for index, char in enumerate(clause) if not char.isspace()]
    compact = ''.join(text[index] for index in positions)
    return compact, positions, _METADATA_BOUNDARY_CLAUSE.fullmatch(compact)


def assessment_method_object_span(text, *, facts=()):
    """Locate only the negated diagnostic object in a full method clause."""
    analysis = _assessment_method_analysis(text, facts)
    if analysis is None or analysis[2] is None:
        return None
    _, positions, match = analysis
    start, end = match.span('boundary_object')
    return positions[start], positions[end - 1] + 1


def is_assessment_method_clause(text, *, facts=()):
    """Classify a complete generated measurement/diagnostic-boundary clause.

    This is a text role, not a disease-specific blacklist. Literal original
    statements and concrete patient/study qualifications are never rewritten.
    Medical absence, uncertainty and an inability to exclude disease are not
    measurement-to-diagnosis boundary clauses.
    """
    analysis = _assessment_method_analysis(text, facts)
    if analysis is None:
        return False
    compact, _, match = analysis
    return bool(match or _METADATA_MISSING_CLAUSE.fullmatch(compact) or any(
        re.fullmatch(pattern, compact) for pattern in METADATA_INDEPENDENT_METHOD_CLAUSE_PATTERNS))


def source_history_affirmation_failures(text, target, *, facts=()):
    """Keep C-reported history attributed in each generated metadata clause.

    A preceding canonical source label cannot turn the next independent
    clause into an established clinical disease. Independent hospital facts
    may establish the same literal condition only through affirmative text.
    """
    if not isinstance(text, str):
        return []
    known = {fact.fact_id: fact for fact in (facts.values() if isinstance(facts, dict) else facts)}
    allowed = set(target.get('allowedFactIds') or [])
    associations = {item.get('name') for item in target.get('inventory', [])
        if item.get('resultKind') == 'sourceHealthAssociation' and item.get('name')
        and any(fid in allowed and fid in known and known[fid].kind == 'context'
            for fid in item.get('factIds') or [])}
    if associations:
        # Attribution does not turn a detected association into reported
        # history. An independent, affirmative A/B/history record can support
        # the same literal condition; a numeric disease model cannot.
        history_items = []
        for item in target.get('inventory', []):
            if item.get('resultKind') == 'sourceHealthAssociation':
                continue
            for fid in item.get('factIds') or []:
                fact = known.get(fid)
                if (fid not in allowed or fact is None or item.get('name') != fact.name
                        or item.get('value') != fact.value
                        or re.fullmatch(r'正常|阴性|未检出|无|未患|未确诊', fact.value.strip())):
                    continue
                codes = {ref.get('sourceCode') for ref in item.get('sourceRefs') or []
                    if isinstance(ref, dict) and ref.get('recordId') == fid and ref.get('observedAt') == fact.date}
                if (fact.kind == 'clinical' and 'A' in codes
                        or fact.kind == 'self_reported' and 'B' in codes
                        or fact.kind in {'context', 'clinical'} and 'C' in codes
                        and re.search(r'病史|疾病史', fact.name)):
                    history_items.append(item)
                    break
        for clause in re.split(r'[，,。；;！？\n]', text):
            if not re.search(r'病史|基础疾?病|既有疾病|疾病背景|健康背景|已确诊|诊断为|患有|已患|罹患', clause):
                continue
            if re.search(r'不是|并非|不代表|不能|未确诊|未诊断|无病史|否认', clause):
                continue
            if re.search(r'(?:缺乏|缺少|未提供|没有|尚无|无)(?:对应|相关|实际|明确|独立|临床|的){0,5}病史', clause):
                continue
            if not history_items and re.search(r'原报告[^，,。；;]{0,16}(?:病史|疾病背景|基础疾?病)', clause):
                return ['来源健康相关菌群“检出”记录不能写成本人病史、基础疾病或疾病背景；须按关联菌群信号解释']
            for name in associations:
                if name not in clause:
                    continue
                supports = any(name in str(item.get('name') or '') + ' ' + str(item.get('value') or '')
                    and not re.search(r'疑似|可能|考虑|倾向|否认|未确诊|未诊断|无病史|家族|父亲|母亲|亲属',
                        str(item.get('name') or '') + ' ' + str(item.get('value') or ''))
                    for item in history_items)
                if not supports:
                    return ['来源健康相关菌群“检出”记录不能写成本人病史、基础疾病或疾病背景；须按关联菌群信号解释']
    # A newly accepted model comparison must not reattribute an actual B
    # family answer to the source report. Its relationship remains self-report.
    family = r'家族|父亲|母亲|血亲|亲属|子女|兄弟|姐妹'
    patient_family, report_family = [], []
    for item in target.get('inventory', []):
        for fid in item.get('factIds') or []:
            fact = known.get(fid)
            if (fid not in allowed or fact is None or item.get('name') != fact.name
                    or item.get('value') != fact.value or not re.search(family, fact.name+' '+fact.value)):
                continue
            codes = {ref.get('sourceCode') for ref in (item.get('sourceRefs') or [])
                if isinstance(ref, dict) and ref.get('recordId') == fid and ref.get('observedAt') == fact.date}
            if fact.kind == 'self_reported' and item.get('useAs') == 'patient_report' and 'B' in codes:
                patient_family.append(fact)
            elif fact.kind in {'clinical', 'context'} and codes & {'A', 'C'}:
                report_family.append(fact)
    if patient_family:
        for clause in re.split(r'[，,。；;！？\n]', text):
            if (re.search(r'原报告(?:记载|记录|提及|列出|列有|提示|中)', clause)
                    and re.search(family, clause) and not re.search(r'问卷|自述|自报|本人称|用户称', clause)
                    and not any(fact.value.strip() and fact.value.strip() in clause for fact in report_family)):
                return ['B问卷中的亲属病史须保留问卷自述归属，不能改称原报告记载；亲属病史也不能写成本人疾病']
    histories = []
    for item in target.get('inventory', []):
        if item.get('useAs') != 'source_context' or not re.search(r'病史|疾病史', item.get('name') or ''):
            continue
        for fid in item.get('factIds') or []:
            fact = known.get(fid)
            if (fid in allowed and fact is not None and fact.kind == 'context'
                    and fact.name == item['name'] and fact.value == item.get('value')
                    and any(ref.get('sourceCode') == 'C' and ref.get('recordId') == fid
                        and ref.get('observedAt') == fact.date for ref in (item.get('sourceRefs') or [])
                        if isinstance(ref, dict))):
                histories.append(fact)
    if not histories:
        return []
    terms = {part.strip() for fact in histories for part in re.split(r'[,，、;；\r\n]+', fact.value)
        if len(part.strip()) >= 2}
    clinical = []
    for item in target.get('inventory', []):
        for fid in item.get('factIds') or []:
            fact = known.get(fid)
            if (fid not in allowed or fact is None or fact.kind != 'clinical'
                    or item.get('name') != fact.name or item.get('value') != fact.value
                    or re.search(r'疑似|可能|考虑|倾向|待核|待查|待定|否认|排除|未见|未发现|未确诊|未诊断|不详|家族|父亲|母亲|亲属', fact.name + ' ' + fact.value)
                    or re.fullmatch(r'正常|阴性|未检出|无|未患', fact.value.strip())
                    or not any(ref.get('sourceCode') == 'A' and ref.get('recordId') == fid
                        and ref.get('observedAt') == fact.date for ref in (item.get('sourceRefs') or [])
                        if isinstance(ref, dict))):
                continue
            clinical.append(fact)
    strong = re.compile(r'(?:已有|存在|具有)(?:明确|确切|已知)?[^，,。；;\n]{0,14}基础(?:疾)?病(?:史)?|'
        r'既有[^，,。；;\n]{0,12}(?:疾病|基础病)|(?:属|属于|是|为)[^，,。；;\n]{0,14}基础(?:疾)?病')
    attribution = re.compile(r'原报告(?:自报|记载|记录|病史)|原始(?:报告|资料)|(?:自报|自述)病史|来源(?:报告|记载)')

    def affirmative_condition(term, fact):
        in_value = any(not re.search(r'(?:无|未患|否认|未诊断(?:为)?|未确诊|排除|疑似|考虑|可能)[^，,。；;]{0,8}$',
            fact.value[:match.start()]) for match in re.finditer(re.escape(term), fact.value))
        return in_value or term in fact.name and re.search(r'已?确诊|诊断为|已患|患有|罹患|病史明确', fact.value)

    for clause in re.split(r'[，,。；;！？\n]', text):
        for match in strong.finditer(clause):
            if re.search(r'(?:不|未|并非|不是|不能认为|不应认定)$', clause[:match.start()].rstrip()):
                continue
            if attribution.search(clause):
                continue
            if any(term in clause and affirmative_condition(term, fact) for term in terms for fact in clinical):
                continue
            return ['C来源病史须在生成判断及各条依据中保留原报告记载或自报归属，不能写成未获独立临床记录支持的明确基础病或既有疾病']
    return []


def _major_distribution_failures(text, target):
    """Audit unnamed macro-group quantifiers against its complete inventory.

    A short representative basis is never the denominator. This also covers
    sentences such as "多数环节处于较弱档" that omit the group heading.
    """
    if target.get('kind') != 'major_nutrition' or not isinstance(text, str):
        return []
    members = target.get('inventory') or []
    if not members:
        return []
    failures = []
    pattern = re.compile(r'(?P<quantity>大多数|多数|大部分|大多|少数|少部分|全部|所有|各项|均)'
        r'(?:的)?(?:环节|项目|子项|条目|功能|代谢|合成|能力|结果|部分|均|皆|都|仍|保留|已|呈|处于|为|在|属|属于|是|档|级|\s){0,12}'
        r'(?P<grade>极低|偏低|偏高|较弱|偏弱|正常|弱|强)')
    for clause in re.split(r'[，,。；;！？\n]', text):
        for match in pattern.finditer(clause):
            if re.search(r'不能|并非|不是|是否', clause[:match.start()]):
                continue
            levels = {'弱', '较弱', '极低', '偏低', '偏弱'} if match['grade'] == '偏弱' else {match['grade']}
            following = re.match(r'(?:或|和|、)(极低|偏低|偏高|较弱|弱|正常|强)', clause[match.end():])
            if following:
                levels.add(following.group(1))
            matching = sum((item.get('assessmentLevel') or item.get('sourceStatus')) in levels for item in members)
            total = len(members)
            quantity = match['quantity']
            valid = (matching * 2 > total if quantity in {'大多数', '多数', '大部分', '大多'}
                else 0 < matching * 2 < total if quantity in {'少数', '少部分'} else matching == total)
            if not valid:
                failures.append(f'{target["name"]}完整{total}项中，{"/".join(sorted(levels))}为{matching}项；'
                    f'“{quantity}”描述与完整组内分布不符，不能以basis代表项目作为分母')
    return failures


def _pathway_comparison_failures(text, target):
    if target.get('kind') != 'pathway' or not isinstance(text, str):
        return []
    for clause in re.split(r'[，,。；;！？\n]', text):
        scope = re.search(r'(?:两|二|三|四|五|六|七|八|九|十|\d+)条通路|(?:全部|所有|其他|各个)通路', clause)
        ranking = re.search(r'最(?:高|低|弱|强|突出|需关注)|首要|第一|优先级最高', clause)
        if scope and ranking or re.search(r'本次[^，,。；;]{0,20}最突出[^，,。；;]{0,8}通路', clause):
            return ['单通路目标只包含本通路实际项目，不能借模型批次中的其他通路作最高/最低排序；保持本域的真实状态判断。']
    return []


def normalize_assessment_metadata(row, target, *, facts=()):
    """Remove whole generated method blocks, never repair a medical claim.

    Preflight every original component before removing anything. Returned
    components still require the ordinary assembly, source and scope gates.
    Representative IDs and the model's score/level are never changed.
    """
    copied = deepcopy(row)
    known = {fact.fact_id: fact for fact in (facts.values() if isinstance(facts, dict) else facts)}
    bound = [known[fid] for fid in target.get('allowedFactIds', []) if fid in known]
    if not bound:
        return copied, ['元数据清理缺少本目标真实来源，须重写原稿']
    basis = row.get('basis') if isinstance(row.get('basis'), list) else []
    components = [row.get('judgment'), *(item.get('text') for item in basis if isinstance(item, dict))]
    errors = []
    errors.extend(_score_anchor_failures(row, target))
    errors.extend(_major_judgment_level_failures(row, target))
    names = [item.get('name') for item in target.get('inventory', []) if isinstance(item.get('name'), str)]
    models = {fact.name.removesuffix('（来源疾病模型指数）'): fact.status for fact in bound
        if fact.kind == 'function' and fact.name.endswith('（来源疾病模型指数）') and fact.status}
    risk = re.compile(r'中高风险|中低风险|高风险|低风险|中风险')
    for text in components:
        if not isinstance(text, str) or not text.strip():
            continue
        errors.extend(_pathway_comparison_failures(text,target))
        errors.extend(source_history_affirmation_failures(text, target, facts=bound))
        errors.extend(_major_distribution_failures(text, target))
        errors.extend(grade_statement_failures(text, bound, tuple(target.get('inventory', []))))
        numeric = text
        for name in names:
            numeric = numeric.replace(name, '')
            for token in re.findall(r'[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*', name):
                numeric = re.sub(r'(?<![A-Za-z0-9_])' + re.escape(token) + r'(?![A-Za-z0-9_])', '', numeric)
        if re.search(r'\d', numeric):
            errors.append('原稿含检测数值或未允许的数字，不能通过删除方法段隐藏；须重写')
        for clause in re.split(r'[，,。；;！？\n]', text):
            literal = any(clause.strip() and clause.strip() in fact.value for fact in bound)
            if not literal:
                method_object = assessment_method_object_span(clause, facts=bound)
                for predicate in re.finditer(r'导致|引起|诱发|促进|造成|确诊|诊断为|患有|已患|罹患', clause):
                    if (method_object is not None and predicate.group() == '确诊'
                            and method_object[0] <= predicate.start() < predicate.end() <= method_object[1]):
                        continue
                    if not re.search(r'(?:不|非|未|没有|未经|不能|无法|不应|不足以|无证据)'
                            r'(?:直接|独立|单独|临床|明确|完成|得到|获得|进行|作出|据此|认为|判断|曾|已|被){0,4}$',
                            clause[:predicate.start()]):
                        errors.append('原稿含新的病因或确诊断言，不能通过删除限定段隐藏；须重写')
                if '模型' in clause:
                    previous_end = 0
                    for match in risk.finditer(clause):
                        named = [name for name in models if name in clause[previous_end:match.start()]]
                        if named and any(models[name] != match.group() for name in named):
                            errors.append('原稿所写来源模型原级与真实来源不一致，不能通过清理隐藏；须重写')
                        previous_end = match.end()
    if errors:
        return copied, list(dict.fromkeys(errors))

    protected = _METADATA_PROTECTED_CLAUSE
    missing = re.compile(r'(?:无|未见|缺少|缺乏|没有|未提供)(?:对应|相关|直接|明确|实际|该方向|其他|本次|同组){0,3}临床(?:记录|病史|依据|线索)')
    method = re.compile(r'(?:属|属于)(?:来源)?模型(?:估计|结果)|(?:并非|而非)[\u4e00-\u9fff]{0,8}确诊依据|'
        r'(?:不能|不可|无法|不足以)(?:据此)?(?:替代|排除)[\u4e00-\u9fff]{0,30}(?:临床关注|影像结论|临床|结论|诊断|疾病|风险)')

    def removable(block):
        return (not protected.search(block) and not any(block.strip() and block.strip() in fact.value for fact in bound)
            and bool(missing.search(block) or method.search(block) or any(
                is_assessment_method_clause(clause, facts=bound)
                for clause in re.split(r'[，,]', block))))

    def source_history_head(text):
        # A complete source-bound focus may stand independently. Discard its
        # entire generated suffix, never just a qualifying clause within it.
        name = target.get('name')
        if target.get('kind') != 'disease' or not isinstance(name, str) or not name.endswith('疾病'):
            return None
        match = re.fullmatch(re.escape(name.removesuffix('疾病') + '关注')
            + r'以原报告(?:自报|记载)的(?P<item>[^，,。；;！？\n]+?)病史为主'
            + r'(?P<separator>[，,])(?P<suffix>[^。；;！？\n]+)[。！？]?\s*', text)
        if not match or not removable(match['suffix']):
            return None
        selected = {item['factId'] for item in basis
            if isinstance(item, dict) and isinstance(item.get('factId'), str)}
        for item in target.get('inventory', []):
            if item.get('useAs') != 'source_context' or item.get('name') != '原报告记载的病史':
                continue
            for fid in set(item.get('factIds') or []) & selected:
                fact = known.get(fid)
                if (not fact or fid not in target.get('allowedFactIds', []) or fact.kind != 'context'
                        or fact.name != item['name'] or fact.value != item.get('value')
                        or not any(ref.get('sourceCode') == 'C' and ref.get('recordId') == fid
                            and ref.get('observedAt') == fact.date for ref in (item.get('sourceRefs') or [])
                            if isinstance(ref, dict))):
                    continue
                original_items = {part.strip() for part in re.split(r'[,，、;；\r\n]+', fact.value)}
                if match['item'] in original_items:
                    return text[:match.start('separator')]
        return None

    judgment = copied.get('judgment')
    if isinstance(judgment, str):
        judgment = source_history_head(judgment) or judgment
        parts = re.split(r'([。；;！？\n])', judgment)
        kept = []
        for index in range(0, len(parts), 2):
            part = parts[index]
            if not part:
                continue
            if not protected.search(part) and not any(part.strip() in fact.value for fact in bound):
                clauses = re.split(r'([，,])', part)
                part = ''.join(clause + (clauses[i + 1] if i + 1 < len(clauses) else '')
                    for i in range(0, len(clauses), 2)
                    if (clause := clauses[i]) and not is_assessment_method_clause(clause, facts=bound))
                part = part.strip().rstrip('，,')
            if part and not removable(part):
                kept.append(part + (parts[index + 1] if index + 1 < len(parts) else ''))
        cleaned = ''.join(kept).strip().rstrip('；;')
        if not cleaned:
            return deepcopy(row), ['删除完整方法或缺项段后没有独立主判断，须按真实来源重点重写']
        copied['judgment'] = cleaned
    for item in copied.get('basis', []) if isinstance(copied.get('basis'), list) else []:
        if isinstance(item, dict) and isinstance(item.get('text'), str) and any(
                removable(block) for block in re.split(r'[。；;！？\n]', item['text'])):
            # Keep the selected fact and its canonical source label. Only the
            # entire optional generated meaning is omitted.
            item['text'] = None
    return copied, []


def source_association_audit_text(text, target, *, facts=()):
    """Mask a verified source-listing clause only in the relationship audit.

    A typed nonnumeric C association must bind its own original context fact,
    literal name and positive source status. No actual facts means no masking.
    This never approves the original prose, a causal relation, a diagnosis or
    an inference from the report's association to an individual's disease.
    """
    if not isinstance(text, str) or target.get("kind") != "disease":
        return text
    values = facts.values() if isinstance(facts, dict) else facts
    known = {fact.fact_id: fact for fact in values}
    allowed = set(target.get("allowedFactIds") or [])
    verified = False
    for item in target.get("inventory", []):
        ids = set(item.get("factIds") or [])
        if (item.get("resultKind") != "sourceHealthAssociation" or item.get("value") is not None
                or item.get("sourceStatus") != "检出" or not ids or not ids <= allowed
                or not ids <= known.keys()):
            continue
        sources = item.get("sourceRefs") or []
        if all(known[fid].kind == "context" and known[fid].name == item.get("name")
                and known[fid].status == "检出"
                and re.match(r"\s*检出(?:[；;，,：:\s]|$)", known[fid].value)
                and any(source.get("sourceCode") == "C" and source.get("recordId") == fid
                    and source.get("observedAt") == known[fid].date for source in sources)
                for fid in ids):
            verified = True
            break
    name = target.get("name")
    if not verified or not isinstance(name, str) or not name.strip():
        return text
    # The scope comes from the actual target label, never from a hard-coded
    # disease, patient, evidence number or a same-name external observation.
    stems = {name.strip(), re.sub(r"疾病$", "", name.strip()),
        re.sub(r"(?:相关)?疾病$", "", name.strip())} - {""}
    scope = "(?:" + "|".join(re.escape(stem) for stem in sorted(stems, key=len, reverse=True)) + ")"
    listing = re.compile(scope + r"(?:相关)?(?:方向|方面|类别|组内)"
        r"存在(?:来源|原报告)(?:中)?(?:列出|列示|记载|记录)的菌群(?:关联|相关)信号")

    def audit_clause(match):
        clause = match.group()
        return "来源列示记录" if listing.fullmatch(clause.strip()) else clause

    return re.sub(r"[^，,。；;！？\n]+", audit_clause, text)


def clear_legacy_coverage_scores(data):
    """Remove only the retired generated coverage scores, never source scores."""
    for slot in data.health_overview.assessment_chart:
        if (slot.explanation or "").startswith(_LEGACY_PREFIXES):
            slot.score, slot.explanation = None, None
            if slot.assessment_level in {"全部已评估项达标", "全部已评估系统达标", "存在需改善项"}:
                slot.assessment_level = None


def retain_source_scores(data):
    """New fixed-template reports display source scores, never authored totals.

    Historical score blocks remain readable by their compatibility helpers;
    this projection prevents them from becoming new native observations.
    """
    clear_legacy_coverage_scores(data)
    for slot in data.health_overview.assessment_chart:
        if (slot.explanation or '').startswith(AI_SCORE_PREFIX):
            slot.score, slot.assessment_level, slot.explanation = None, None, None
            slot.sources, slot.availability = [], 'unavailable'
    for slot in data.nutrition_assessment.major_metabolism:
        _clear_major_score(slot)
    for row in data.functional_pathways.pathways:
        if (row.explanation or '').startswith(AI_MAJOR_SCORE_PREFIX) and not row.source_status:
            row.assessment_level, row.explanation = None, None
            row._assessment_origin = None
    return data


def _rows(data, code, *, include_harmful=False):
    if code == "overall":
        return [*_rows(data, "medical"), *_rows(data, "microbiome"), *_rows(data, "nutrition", include_harmful=True),
            *(item for category in data.disease_risk_assessment.categories for item in category.items),
            *data.medical_assessment.questionnaire_summary]
    if code == "medical":
        return [*data.medical_assessment.findings, *data.medical_assessment.normal_findings]
    if code == "microbiome":
        return [*data.microbiome_assessment.overall_structure, *data.microbiome_assessment.health_metrics]
    nutrition = data.nutrition_assessment
    # An authored major total is an output over these same observations, not a
    # new original input to the nutrition/overall card on the next rebuild.
    rows = [*(row for row in nutrition.major_metabolism
            if not (row.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX)),
        *nutrition.amino_acids_and_metabolites, *nutrition.neurotransmitters,
        *(row for group in nutrition.detailed_metabolism if group.code != "otherSourceFunctions" for row in group.items),
        *(row for group in nutrition.vitamins_and_minerals for row in group.items),
        *data.microbiome_assessment.short_chain_fatty_acids]
    return rows if include_harmful else [row for row in rows if not is_harmful_generation_metric(row)]


def _overall_domains(data):
    """Section identity supplies the domain; a context fact can be a real index."""
    return {id(row): domain for domain, rows in (
        ("medical", _rows(data, "medical")), ("microbiome", _rows(data, "microbiome")),
        ("nutrition", _rows(data, "nutrition", include_harmful=True)),
        ("diseaseRisk", [row for category in data.disease_risk_assessment.categories for row in category.items]),
        ("reportedBackground", data.medical_assessment.questionnaire_summary)) for row in rows}


def _source_use_as(row, ids, facts, *, domain=None):
    kinds = {facts[fid].kind for fid in ids}
    # A PDF's own reported history/medication is source context, including when
    # an upstream compatibility collector labelled it as a clinical fact.
    if domain == "reportedBackground":
        return ("source_context" if any(source.source_code == "C" for source in row.sources)
            or "context" in kinds else "patient_report")
    if "clinical" in kinds:
        return "clinical_record"
    if "microbe" in kinds:
        return "microbiome_result"
    if "function" in kinds:
        return "source_model_result"
    return "patient_report" if kinds == {"self_reported"} else "source_context"


def _health_association_ids(data, facts):
    """Use the typed source inventory, never a disease-name or detection guess."""
    return {source.record_id for row in data.appendices.source_items
        if row.kind == 'context' and row.source_type == 'microbiome' and row.category == 'healthAssociation'
        for source in row.sources if source.source_code == 'C' and source.record_id in facts
        and facts[source.record_id].kind == 'context'}


def _add_association_kind(item, association_ids):
    # A reviewed clinical problem may combine a patient's symptoms with a
    # source screening signal. The signal cannot retype the entire problem.
    if item['factIds'] and set(item['factIds']) <= association_ids:
        item['resultKind'] = 'sourceHealthAssociation'


def _source_fact_digest(ids, facts):
    return hashlib.sha256(json.dumps([facts[fid].model_dump(mode="json", by_alias=True)
        for fid in sorted(ids)], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _inventory_domains(item):
    return set(item.get("assessmentDomains") or [item.get("assessmentDomain")]) - {None}


def _append_measurement(inventory, item):
    """One bound original observation contributes once across display panels.

    Names and optional display ranges can differ across sections. A duplicate
    needs the same original fact, literal value/type, original grade and unit.
    Different source facts, dates or association topics are never name-merged.
    All provenance remains in the retained item; this computes no score/weight.
    """
    def identity(row):
        return json.dumps({key: row.get(key) for key in ("value", "sourceStatus", "unit", "resultKind", "useAs")}
            | {"valueType": type(row.get("value")).__name__,
                "associationName": row.get("name") if row.get("resultKind") == "sourceHealthAssociation" else None},
            ensure_ascii=False, sort_keys=True, allow_nan=False)
    key = identity(item)
    for previous in inventory:
        if not set(previous["factIds"]).intersection(item["factIds"]) or identity(previous) != key:
            continue
        previous["factIds"] = sorted(set(previous["factIds"]) | set(item["factIds"]))
        for source in item["sourceRefs"]:
            if source not in previous["sourceRefs"]:
                previous["sourceRefs"].append(source)
        domains = _inventory_domains(previous) | _inventory_domains(item)
        if len(domains) > 1:
            previous["assessmentDomains"] = sorted(domains)
        return
    inventory.append(item)


def _native(slot):
    return slot.score is not None and not (slot.explanation or "").startswith(AI_SCORE_PREFIX)


def _key(text):
    return re.sub(r"[\W_]+", "", str(text or "")).casefold()


def source_module_level(data, code):
    """Only the same source total's explicit grade can grade its native card."""
    if code != "microbiome":
        return None
    levels = {row.source_status for row in _rows(data, code)
        if row.code == "microbiomeHealthScore" and row.value is not None and row.sources
        and isinstance(row.source_status, str) and row.source_status.strip()}
    return next(iter(levels)) if len(levels) == 1 else None


def _clinical_inventory_context(data):
    """Expose existing clinical table grouping without making new diagnoses."""
    return {id(row): {"system": row.system, "clinicalRole": role}
        for role, rows in (("finding", data.medical_assessment.findings),
            ("normal", data.medical_assessment.normal_findings))
        for row in rows if isinstance(row.system, str) and row.system.strip()}


def _add_clinical_context(item, row, ids, facts, context):
    if id(row) in context and any(facts[fid].kind == "clinical" for fid in ids):
        item.update(context[id(row)])


def _add_assessment_origin(item, row):
    origin = getattr(row, "_assessment_origin", None)
    if origin is not None:
        item["assessmentOrigin"] = origin


def _assessment_fact_ids(row, facts):
    """Bind an enriched health heading to its own native observation only.

    Its public source references also locate measured detail organisms. Those
    organisms retain independent inventory rows and cannot stand in as the
    heading's native value, or as another heading from the same PDF page.
    """
    ids = sorted({source.record_id for source in row.sources if source.record_id in facts})
    if isinstance(row, TaxonSignal):
        # A relation also locates its parent disease model. That context ID
        # is not a measurement of this organism and cannot merge two taxa.
        def normalized(value):
            return unicodedata.normalize('NFKC', str(value or '')).strip()
        def names(value):
            return {_key(part) for part in re.split(r'\s*/\s*', normalized(value)) if part.strip()}
        aliases = names(row.name) | names(row.scientific_name)
        def same_value(left, right):
            if normalized(left) == normalized(right):
                return True
            try:
                first, second = Decimal(str(left)), Decimal(str(right))
                return first.is_finite() and second.is_finite() and first == second
            except (InvalidOperation, ValueError, TypeError):
                return False
        return [fid for fid in ids if facts[fid].kind == 'microbe'
            and aliases.intersection(names(facts[fid].name))
            and same_value(row.value, facts[fid].value)
            and normalized(row.unit) == normalized(facts[fid].unit)
            and normalized(row.source_status) == normalized(facts[fid].status)]
    if not getattr(row, "details", None):
        return ids
    c_ids = {source.record_id for source in row.sources if source.source_code == "C"}
    return [fid for fid in ids if fid in c_ids and facts[fid].kind == "context"
        and facts[fid].name == row.name and facts[fid].value == row.value
        and facts[fid].unit == row.unit
        and facts[fid].status == row.source_status
        and (row.reference_range is None or facts[fid].reference_range == row.reference_range)]


def _clinical_priorities(data, document, facts, allowed):
    priorities, focus_ids = [], set()
    for problem in document.problems:
        if problem.priority != "priority" or not problem.title.strip():
            continue
        # A specific problem title is an alternative to a long examination
        # name only when the supplied clinical quote proves that exact focus.
        ids = sorted({source.fact_id for source in problem.sources
            if source.fact_id in allowed and facts[source.fact_id].kind == "clinical"
            and source.quote.strip() and source.quote.strip() in facts[source.fact_id].value
            and problem.title.strip() in source.quote})
        if not ids:
            continue
        fragments = [problem.title.strip(), *re.split(r"[，,、；;。]", problem.title)]
        anchors = list(dict.fromkeys(part.strip() for part in fragments
            if len(_key(part)) >= 5 and not re.fullmatch(
                r"(?:CT|MRI|影像|检查|检验|医学)*(?:异常|需要关注|需关注|待核实|待复核|待核对)", part.strip())))
        if not anchors:
            continue
        priorities.append({"name": problem.title, "factIds": ids, "focusAnchors": anchors})
        focus_ids.update(ids)
    for row in data.medical_assessment.findings:
        if row.system == "影像科" and row.assessment_level == "异常":
            ids = sorted(({source.record_id for source in row.sources} & set(allowed)) - focus_ids)
            if ids:
                priorities.append({"name": row.name, "factIds": ids})
    return priorities


def _priority_grounded(explanation, priority, facts):
    if priority.get("focusAnchors"):
        # Mentioning the examination or another lesion from the same record
        # does not explain its verified priority problem.
        return any(_key(anchor) in _key(explanation) for anchor in priority["focusAnchors"])
    return _grounded(explanation, priority["factIds"], facts)


def _major_slot(data, code):
    return next((row for row in data.nutrition_assessment.major_metabolism
        if "nutrition:" + str(row.code) == code), None)


def _clear_major_score(slot):
    if slot is not None and (slot.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX):
        slot.value, slot.explanation, slot.assessment_level = None, None, None
        slot.unit, slot.reference_range, slot.sources = None, None, []
        slot._assessment_origin, slot.availability = None, "unavailable"


def _amino_identity(row):
    """Match a finite amino-acid identity, never a substring such as cysteine."""
    names = {_key(row.code), _key(row.name)}
    for name in tuple(names):
        for suffix in ("synthesisability", "synthesisscore", "synthesis", "metabolismscore",
                "metabolism", "合成能力", "合成评分", "代谢能力", "代谢评分", "合成", "代谢", "score"):
            if name.endswith(suffix):
                names.add(name[:-len(suffix)])
    return any(_key(alias) in names or "l" + _key(alias) in names
        for aliases in catalog.AMINO_ACID_ALIASES.values() for alias in aliases)


def _major_score_targets(data, facts):
    """Request only supported missing totals; no estimate is made locally."""
    nutrition = data.nutrition_assessment
    domains = {
        "carbohydrate": [row for group in nutrition.detailed_metabolism
            if group.code == "carbohydrateMetabolism" for row in group.items],
        "fat": [row for group in nutrition.detailed_metabolism
            if group.code == "lipidMetabolism" for row in group.items],
        "protein": [row for row in nutrition.amino_acids_and_metabolites if _amino_identity(row)],
    }
    targets = []
    for code, rows in domains.items():
        slot = _major_slot(data, "nutrition:" + code)
        # Preserve any original total, including literal numeric zero. Only an
        # earlier total authored by this path may be recomposed or restored.
        if slot is None or slot.value is not None and not (slot.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX):
            continue
        inventory = []
        for row in rows:
            sources = [source for source in row.sources if source.source_code == "C"]
            ids = sorted({source.record_id for source in sources
                if source.record_id in facts and facts[source.record_id].kind == "function"})
            if not ids or (row.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX) or (
                    row.value is None and not row.source_status and not row.assessment_level):
                continue
            item = {"name": row.name, "value": row.value, "unit": row.unit,
                "sourceStatus": row.source_status, "assessmentLevel": row.assessment_level,
                "referenceRange": row.reference_range, "factIds": ids,
                "sourceRefs": [source.model_dump(mode="json", by_alias=True) for source in sources],
                "assessmentDomain": "nutrition", "useAs": "source_model_result"}
            _add_assessment_origin(item, row)
            _append_measurement(inventory, item)
        if not inventory:
            continue
        allowed = sorted({fid for row in inventory for fid in row["factIds"]})
        target = {"code": "nutrition:" + code, "kind": "major_nutrition", "name": slot.name,
            "role": "major_nutrition_synthesis", "scoreRequired": True,
            "allowedFactIds": allowed, "requiredFactIds": [], "clinicalPriorities": [],
            "inventory": inventory, "allowedLevels": ["弱", "较弱", "正常"],
            "gradingStandard": catalog.MAJOR_NUTRITION_SCORE_RANGE,
            "scale": {"minimum": 0, "maximum": 100, "direction": "higher_is_better"},
            "scoringRubric": _score_rubric("nutrition:" + code),
            "rules": ["仅依据本目标真实C来源功能子项，综合判断本类营养代谢能力；不借用饮食问卷、其他营养类别或样板分数。",
                "score为AI补充的综合分，不是原报告实测总分；不对原子项取平均、加权平均或按正常比例计算。",
                "assessmentLevel须与score对应：0至10为弱，大于10且不超过30为较弱，大于30至100为正常。",
                "综合等级先按scoringRubric中本类完整状态锚点选择：主要方向保留且有局部弱项时整体仍为正常，局部弱项单独强调；只有薄弱方向确实主导整体才判较弱或弱。不能因一个重要项目较弱就把整组降级，也不虚构某个子项能够决定整体的权重。",
                "不输出介于较弱和正常之间的折中状态，不贴着10或30等模板边界自由打分；score按已选明确等级的内部锚点范围给出，原生子项数值和原级不变。",
                "原子项的数值、单位、原分级和参考范围均保留；说明真实功能差异及其意义，不把代谢能力解释为摄入量或人体含量。",
                *ASSESSMENT_METADATA_POSITIVE_RULES]}
        target["sourceFactDigest"] = _source_fact_digest(allowed, facts)
        target["sourceDigest"] = hashlib.sha256(json.dumps(target, ensure_ascii=False,
            sort_keys=True, allow_nan=False).encode()).hexdigest()
        targets.append(target)
    return targets


def build_score_targets(data, document, *, include_major=False, source_only=False,
        include_components=False):
    """Return source-bound missing-score requests for the existing composer."""
    if source_only:
        retain_source_scores(data)
        if include_components:
            from .evaluation_component_scores import build_component_score_targets
            return build_component_score_targets(data,document)
        return []
    clear_legacy_coverage_scores(data)
    facts = {fact.fact_id: fact for fact in document.facts}
    from .evaluation_narrative import _nutrition_group_context, _assessment_projection
    source_groups = _nutrition_group_context(_assessment_projection(data), facts)
    from .evaluation_synthesis import native_function_source_context
    native_groups = native_function_source_context(data.model_dump(mode='json', by_alias=True), facts)
    group_labels = {}
    for group in source_groups:
        for member in group['members']:
            for fid in [member.get('boundFactId')]:
                if fid in facts and isinstance(group.get('name'), str):
                    group_labels.setdefault(fid, set()).update([group['name'], *member.get('sourceGroupNames', [])])
    # Reuse the existing positive self-report projection. Short symptom
    # names can be reader-facing anchors for a long compound answer, but
    # only when the exact source actually contains that positive symptom.
    # C association screens, goals, relatives and negative answers never
    # enter this projection or acquire a patient's symptom alias.
    from types import SimpleNamespace
    from .evaluation_fixed_assessment import _symptom_findings, _questionnaire_modules
    reported = SimpleNamespace(medical_assessment=SimpleNamespace(findings=[]))
    _symptom_findings(reported, [{**fact.model_dump(), 'sources': [SourceReference(
        source_code='B', record_id=fact.fact_id, observed_at=fact.date)]}
        for fact in facts.values() if fact.kind == 'self_reported'
        and not _questionnaire_modules(fact).intersection({'medications', 'familyHistory', 'healthGoals'})])
    for finding in reported.medical_assessment.findings:
        labels = [re.sub(r'(?:等[^/]*|症状)?[（(]自述[）)]$', '', name).strip()
            for name in finding.name.split('/')]
        for source in finding.sources:
            fact = facts.get(source.record_id)
            if fact is not None:
                group_labels.setdefault(fact.fact_id, set()).update(label for label in labels
                    if len(label) >= 2 and label in str(finding.value or ''))
    clinical_context = _clinical_inventory_context(data)
    association_ids = _health_association_ids(data, facts)
    domains = _overall_domains(data)
    targets = []
    for slot in data.health_overview.assessment_chart:
        if str(slot.code or '').startswith('component:'):
            continue  # Component illustrations have their own source-bound protocol.
        # Minimal citation fixtures and absent dimensions are not score inputs.
        if (_native(slot) and not _saved_numeric_review_available(document, slot)
                or not slot.sources or slot.availability == "unavailable"):
            continue
        inventory = []
        for row in _rows(data, slot.code):
            if (getattr(row, "code", None) or "").startswith("panelSummary:"):
                continue
            ids = _assessment_fact_ids(row, facts)
            usable = getattr(row, "assessment_level", None) or getattr(row, "source_status", None)
            domain = domains.get(id(row)) if slot.code == "overall" else None
            if not ids or row.value is None and not usable:
                continue
            if not usable and domain != "reportedBackground" and not (slot.code in {"medical", "overall"} and any(
                    len(re.findall(r"[\u4e00-\u9fff]", facts[fid].value)) >= 4 for fid in ids)):
                continue
            item = {"name": row.name, "value": row.value, "unit": getattr(row, "unit", None),
                "sourceStatus": getattr(row, "source_status", None), "assessmentLevel": getattr(row, "assessment_level", None),
                "referenceRange": getattr(row, "reference_range", None), "factIds": ids,
                "sourceRefs": [source.model_dump(mode="json", by_alias=True) for source in row.sources]}
            labels = sorted({label for fid in ids for label in group_labels.get(fid, ())})
            if labels:
                item['groupLabels'] = labels
            if slot.code == "overall":
                item["assessmentDomain"] = domain
                item["useAs"] = _source_use_as(row, ids, facts, domain=domain)
                if (row.code or "").startswith("association:"):
                    item["resultKind"] = "sourceHealthAssociation"
            _add_association_kind(item, association_ids)
            if is_harmful_generation_metric(row):
                item['resultKind'] = 'harmfulMetaboliteGeneration'
                item['direction'] = 'lower_generation_is_not_nutrient_deficiency'
            _add_clinical_context(item, row, ids, facts, clinical_context)
            _add_assessment_origin(item, row)
            _append_measurement(inventory, item)
        if not inventory:
            continue
        if slot.code in {'overall', 'medical', 'nutrition'}:
            measured = {fid for item in inventory for fid in item['factIds']}
            for group in native_groups:
                if not measured.intersection(fid for member in group['members'] for fid in member['factIds']):
                    continue
                # The reviewer and its fingerprint receive the same actual
                # original aggregate/direction observations as the writer.
                # They are context facts, not another measured score or an
                # inferred explanation of the group's health impact.
                for fid in dict.fromkeys([*group.get('aggregateFactIds', []), *group.get('directionFactIds', [])]):
                    if fid in measured or fid not in facts:
                        continue
                    fact = facts[fid]
                    item = {'name': fact.name, 'value': fact.value, 'unit': fact.unit,
                        'referenceRange': fact.reference_range, 'sourceStatus': fact.status,
                        'factIds': [fid], 'useAs': 'source_context' if fact.kind == 'context' else 'source_model_result',
                        'sourceRefs': [source for source in group.get('sources', [])
                            if source.get('recordId') == fid]}
                    if slot.code == 'overall':
                        item['assessmentDomain'] = 'nutrition'
                    inventory.append(item)
                    measured.add(fid)
        allowed = sorted({fid for row in inventory for fid in row["factIds"]})
        priorities = _clinical_priorities(data, document, facts, allowed) if slot.code in {"medical", "overall"} else []
        required = sorted({fid for row in priorities for fid in row["factIds"]})
        target = {"code": slot.code, "name": slot.name, "scoreRequired": True,
            "allowedLevels": sorted(SCORE_LEVELS),
            "allowedFactIds": allowed, "requiredFactIds": required,
            "inventory": inventory, "clinicalPriorities": priorities,
            "scale": {"minimum": 0, "maximum": 100, "direction": "higher_is_better"},
            "scoringRubric": _score_rubric(slot.code)}
        target["sourceFactDigest"] = _source_fact_digest(allowed, facts)
        if slot.code == "overall":
            available = sorted({domain for row in inventory for domain in _inventory_domains(row)})
            target.update(role="overall_synthesis", availableDomains=available,
                minimumBasisDomains=min(3, len(available)),
                basisDomainChoices=[{"assessmentDomain": domain,
                    "factIds": sorted({fid for item in inventory if domain in _inventory_domains(item)
                        for fid in item['factIds']})} for domain in available],
                rules=["整体分是AI基于全部已采纳原始资料的综合判断，不能平均分域分数或按正常项目比例计算。",
                    "先考虑实际重要医学发现和病史，再结合菌群组成、功能差异和真实生活背景解释整体状态。",
                    "阅读全部实际分域，以1至3项简洁basis覆盖至少minimumBasisDomains个实际分域；从basisDomainChoices不同分域分别选取factId。同一分域选多项仍只覆盖一个分域；主判断提到某域不能替代该域basis。重要临床优先事实仍必须选取。",
                    "只描述实际已提供维度；原报告背景保留自报性质，不升级确诊或虚构饮食用药细节。"])
        from .evaluation_metadata_review import NUMERIC_OVERVIEW_CODES, NUMERIC_OVERVIEW_RULES
        target["rules"] = [*target.get("rules", []), *(NUMERIC_OVERVIEW_RULES
            if slot.code in NUMERIC_OVERVIEW_CODES else ASSESSMENT_METADATA_POSITIVE_RULES)]
        target["sourceDigest"] = hashlib.sha256(json.dumps(target, ensure_ascii=False,
            sort_keys=True, allow_nan=False).encode()).hexdigest()
        targets.append(target)
    if include_major:
        targets.extend(_major_score_targets(data, facts))
    if include_components:
        from .evaluation_component_scores import build_component_score_targets
        targets.extend(build_component_score_targets(data,document))
    return targets


def _grounded(explanation, ids, facts, inventory=()):
    text = _key(explanation)
    # collect_source_facts adds this exact annotation; it is not part of the
    # report's disease name. Other source parentheses remain meaningful.
    if any((name := _key(re.sub(r"（来源疾病模型指数）$", "", facts[fid].name)))
            and name in text for fid in ids):
        return True
    # Typed display names are source-bound projections, e.g. a Chinese taxon
    # name displayed without the source's Latin suffix. Never infer an alias:
    # accept only this target's actual row name bound to a cited original ID.
    return any(set(ids).intersection(item.get("factIds") or [])
        and any((name := _key(label)) and name in text
            for label in [item.get('name'), *item.get('groupLabels', [])]) for item in inventory)


def _is_saved_code(code, base):
    return code == base or bool(re.fullmatch(re.escape(base) + r":\d{3}", code))


def _load_rows(document, base, key):
    blocks = [block for block in document.sections if _is_saved_code(block.code, base)]
    expected = [base, *(f"{base}:{index:03d}" for index in range(1, len(blocks)))]
    by_code = {block.code: block for block in blocks}
    if not blocks or len(by_code) != len(blocks) or set(by_code) != set(expected):
        return []
    try:
        return json.loads("".join(by_code[code].text for code in expected))[key]
    except (ValueError, KeyError, TypeError):
        return []


def _store_rows(document, base, key, rows):
    document.sections = [block for block in document.sections if not _is_saved_code(block.code, base)]
    if not rows:
        return
    payload = json.dumps({key: rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fact_ids = sorted({fid for row in rows for fid in row["factIds"]})
    # Internal section text is capped at 4000 characters. Chunk the exact JSON,
    # including long source IDs, instead of truncating valid model reasoning.
    for index, offset in enumerate(range(0, len(payload), 3600)):
        code = base if index == 0 else f"{base}:{index:03d}"
        document.sections.append(ClinicalReportBlock(code=code,
            text=payload[offset:offset + 3600], factIds=fact_ids))


def _source_model_comparison(claim, ids, facts):
    """Distinguish a literal model-risk comparison from a patient diagnosis."""
    if re.search(r"本人|患者|受检者|该用户|确诊|患有|已患|罹患|患病|诊断为", claim):
        return False
    models = [facts[fid] for fid in ids if facts[fid].kind == "function"
        and facts[fid].name.endswith("（来源疾病模型指数）")]
    if not models:
        return False
    risky = [part.strip() for part in re.split(r"[，,]", claim)
        if re.search(r"(?:显示|证明|证实|表明).{0,8}(?:病|癌|肿瘤|综合征)", part)]
    if not risky:
        return False
    for part in risky:
        # Here the grammatical subject is explicitly a source model. A
        # disease-category noun after 显示 is not a patient diagnosis. Still
        # require a literal bound model name and its actual source grade.
        subject = re.fullmatch(r"(?:原报告|来源|原|菌群|疾病风险)模型(?:结果|指数|分级)?"
            r"(?:显示|提示|表明)(?P<scope>[^，,。；;！？\n]+)", part)
        if subject is not None:
            scope = re.sub(r"^[^，,。；;！？\n]{0,30}(?:疾病|模型|风险类别)中", "", subject["scope"])
            supported = False
            for fact in models:
                original = fact.name.removesuffix("（来源疾病模型指数）")
                names = {original, *(original[-length:] for length in range(3, len(original)))}
                names = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
                described = re.fullmatch(r"(?:" + names + r")(?:相关)?(?:方向)?"
                    r"(?:模型|来源模型)?(?:为|呈|处于|仍为)?"
                    r"(?P<state>中高风险|中低风险|高风险|低风险|中风险|偏高|偏低|"
                    r"风险突出|为?组内主要关注点)(?:状态)?", scope)
                if described is None:
                    focused = re.fullmatch(r"(?:[^，,。；;！？\n]{1,30}疾病)?风险集中于(?:" + names
                        + r")(?:相关)?(?:方向)?", scope)
                    if focused is not None and "高" in (fact.status or "") and "低" not in (fact.status or ""):
                        supported = True
                        break
                    continue
                status, state = fact.status or "", described["state"]
                supported = (state == status or state == "偏高" and "高" in status and "低" not in status
                    or state == "偏低" and "低" in status and "高" not in status
                    or state in {"风险突出", "组内主要关注点", "为组内主要关注点"}
                        and "高" in status and "低" not in status)
                if supported:
                    break
            if supported:
                continue
            return False
        match = re.fullmatch(r"(?:显示|表明)(?P<scope>[^，,。；;！？\n]{1,80}?)"
            r"(?:来源|疾病|菌群)?模型(?:指数|分级|等级|风险)?(?:为|呈|处于|仍为)?"
            r"(?P<level>中高风险|中低风险|高风险|低风险|中风险|偏高|偏低)(?:状态)?", part)
        if match is None:
            return False
        # The object must be a literal full name or terminal excerpt of an
        # actually supplied model name (e.g. its source's shorter wording).
        name = re.split(r"其中|中仅|中|仅", match["scope"])[-1].strip()
        if len(_key(name)) < 3:
            return False
        def supports(fact):
            original = fact.name.removesuffix("（来源疾病模型指数）")
            if not _key(original).endswith(_key(name)):
                return False
            status, level = fact.status or "", match["level"]
            return (level == status or level == "偏高" and "高" in status and "低" not in status
                or level == "偏低" and "低" in status and "高" not in status)
        if not any(supports(fact) for fact in models):
            return False
    return True


def _unsupported_reason(text, ids, facts):
    # Composite state explanations follow the same body-content contract as
    # narrative cells. Reject the whole authored statement; do not remove a
    # qualifier and retain a stronger conclusion. Literal source records are
    # exempted by the shared check and remain unchanged.
    from modules.report.evaluation_content import _source_content_failures
    if _source_content_failures(text, path=(), facts={fid: facts[fid] for fid in ids},
            evidence_refs=()):
        return True
    if re.search(r"达标率|覆盖率|状态覆盖|项达标|正常比例|正常项数|疾病概率|患病概率|评分.{0,8}(?:实测|临床标准)|"
            r"(?:分域|模块|系统|分数|评分|得分|综合分)[^。；;]{0,24}(?:取平均|平均值|均分|平均分|加权平均|等权)|"
            r"(?:算术平均|加权平均|取平均|平均值|平均分|均分)[^。；;]{0,24}(?:分|评分|得分)", text):
        return True
    # A bound original question is a source label, not an authored diagnosis.
    # In particular 明确诊断 in a literal question contains the substring 确诊.
    # Mask only the complete selected question name in this audit view; never
    # alter its public label, saved basis, answer, or a diagnosis added after it.
    diagnostic_view = text
    for fid in ids:
        fact = facts[fid]
        if fact.kind == "self_reported" and re.search(r"[？?]$", fact.name.strip()):
            diagnostic_view = diagnostic_view.replace(fact.name, " ")
    # A model assessment may discuss known state, not introduce a diagnosis.
    claims = re.findall(r"[^。；;\n]*(?:确诊|患有|诊断为|(?:显示|证明|证实|表明).{0,8}(?:病|癌|肿瘤|综合征))[^。；;\n]*", diagnostic_view)
    return any(not _source_model_comparison(claim, ids, facts)
        and not any(claim in facts[fid].value for fid in ids if facts[fid].kind == "clinical") for claim in claims)


def _target_sources(target, *, fact_ids=None):
    sources = []
    for item in target["inventory"]:
        if fact_ids is not None and not set(fact_ids).intersection(item["factIds"]):
            continue
        for raw in item["sourceRefs"]:
            source = SourceReference.model_validate(raw)
            if source not in sources:
                sources.append(source)
    return sources


def apply_ai_scores(data, document, rows=None, *, reason_validator=None, source_only=False):
    """Validate/apply response rows, or reapply the request-bound stored result.

    The internal section survives the pipeline's evaluation-data rebuild. Its
    digest is checked against current source values and grades on every apply.
    """
    from .evaluation_component_scores import project_component_scores, apply_component_score_rows
    if source_only:
        retain_source_scores(data)
        project_component_scores(data,document)
        component_rows = ([row for row in rows if isinstance(row,dict)
            and str(row.get('code')or'').startswith('component:')]
            if isinstance(rows,list) else None)
        return apply_component_score_rows(data,document,component_rows or None,
            reason_validator=reason_validator)
    if isinstance(rows,list):
        components=[row for row in rows if isinstance(row,dict)
            and str(row.get('code')or'').startswith('component:')]
        if components:
            errors=apply_component_score_rows(data,document,components,
                reason_validator=reason_validator)
            remaining=[row for row in rows if row not in components]
            return errors+(apply_ai_scores(data,document,remaining,
                reason_validator=reason_validator)if remaining else [])
    facts = {fact.fact_id: fact for fact in document.facts}
    restoring = rows is None
    if restoring:
        rows = _load_rows(document, SCORE_BLOCK_CODE, "scores")
    if not isinstance(rows, list):
        rows = []
    major_codes = {row.get("code") for row in rows if isinstance(row, dict)
        and isinstance(row.get("code"), str) and row["code"] in _MAJOR_SCORE_CODES}
    major_codes.update("nutrition:" + str(slot.code) for slot in data.nutrition_assessment.major_metabolism
        if (slot.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX)
        and "nutrition:" + str(slot.code) in _MAJOR_SCORE_CODES)
    targets = {target["code"]: target for target in build_score_targets(data, document,
        include_major=bool(major_codes)) if target["code"] not in _MAJOR_SCORE_CODES
        or target["code"] in major_codes}
    for code in major_codes - targets.keys():
        # A disappeared functional domain cannot leave a previous AI number
        # in place, even when no currently supported target can be built.
        _clear_major_score(_major_slot(data, code))
    errors, validated = [], []
    from .evaluation_metadata_review import NUMERIC_OVERVIEW_CODES
    for code, target in targets.items():
        matches = [row for row in rows if isinstance(row, dict) and row.get("code") == code]
        reason = None
        if len(matches) != 1:
            reason = "missing_or_duplicate"
        else:
            row = matches[0]
            score, explanation = row.get("score"), row.get("explanation")
            ids = row.get("factIds")
            numeric_only = code in NUMERIC_OVERVIEW_CODES
            if type(score) is not int or not 0 <= score <= 100:
                reason = "score_must_be_integer_0_to_100"
            elif code in _MAJOR_SCORE_CODES and row.get("assessmentLevel") != catalog.template_nutrition_grade(score):
                reason = "invalid_nutrition_level"
            elif code not in _MAJOR_SCORE_CODES and row.get("assessmentLevel") not in SCORE_LEVELS:
                reason = "invalid_overall_level"
            elif numeric_only and (explanation not in (None, '') or row.get('judgment') not in (None, '')):
                reason = "numeric_score_has_unreviewed_prose"
            elif not numeric_only and (not isinstance(explanation, str) or not 8 <= len(explanation.strip()) <= 1600):
                reason = "missing_explanation"
            elif not isinstance(ids, list) or not ids or any(type(fid) is not str for fid in ids):
                reason = "missing_fact_ids"
            elif not set(ids) <= set(target["allowedFactIds"]) or not set(target["requiredFactIds"]) <= set(ids):
                reason = "unbound_or_omitted_priority_fact"
            elif code == "overall" and len({domain for item in target["inventory"]
                    if set(ids).intersection(item["factIds"]) for domain in _inventory_domains(item)}) < target["minimumBasisDomains"]:
                reason = "overall_domain_not_acknowledged"
            elif not numeric_only and not _grounded(explanation, ids, facts, target["inventory"]) and not any(
                    priority.get("focusAnchors") and _priority_grounded(explanation, priority, facts)
                    for priority in target["clinicalPriorities"]):
                reason = "explanation_must_name_actual_item"
            elif not numeric_only and any(not _priority_grounded(explanation, priority, facts) for priority in target["clinicalPriorities"]):
                reason = "priority_not_explained"
            elif target["clinicalPriorities"] and row["assessmentLevel"] == "正常":
                reason = "normal_level_conflicts_with_priority_finding"
            elif not numeric_only and _unsupported_reason(explanation, ids, facts):
                reason = "unsupported_scoring_method_or_probability"
            elif not numeric_only and source_history_affirmation_failures(explanation, target, facts=facts):
                reason = "source_health_role_not_preserved"
            elif not numeric_only and _major_distribution_failures(explanation, target):
                reason = "group_distribution_not_supported"
            elif not numeric_only and reason_validator is not None and reason_validator(row, target):
                reason = "unsupported_explanation"
            elif restoring and row.get("sourceDigest") != target["sourceDigest"]:
                reason = "source_changed"
            elif numeric_only and not assessment_metadata_reviewed(document, row, target):
                reason = "independent_review_required"
        if reason:
            errors.append(f"AI_SCORE_INVALID:{code}:{reason}")
            slot = _major_slot(data, code) if code in _MAJOR_SCORE_CODES else next(
                slot for slot in data.health_overview.assessment_chart if slot.code == code)
            if code in _MAJOR_SCORE_CODES and (slot.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX):
                _clear_major_score(slot)
            elif code not in _MAJOR_SCORE_CODES and ((slot.explanation or "").startswith(AI_SCORE_PREFIX)
                    or code in NUMERIC_OVERVIEW_CODES and _saved_numeric_review_available(document, slot)):
                slot.score, slot.explanation, slot.assessment_level = None, None, None
            if code not in _MAJOR_SCORE_CODES and slot.score is None:
                slot.availability = 'partial' if slot.sources else 'unavailable'
            continue
        validated.append({"code": code, "score": row["score"], "assessmentLevel": row["assessmentLevel"],
            "explanation": None if code in NUMERIC_OVERVIEW_CODES else explanation.strip(),
            "factIds": sorted(set(ids)), "sourceDigest": target["sourceDigest"],
            **({'sourceFactDigest': target['sourceFactDigest']} if code in NUMERIC_OVERVIEW_CODES else {})})

    unknown = [row for row in rows if not isinstance(row, dict) or row.get("code") not in targets]
    if unknown:
        errors.append("AI_SCORE_INVALID:unknown_or_source_locked_module")
    if not restoring:
        _store_rows(document, SCORE_BLOCK_CODE, "scores", validated)
    for row in validated:
        if row["code"] in _MAJOR_SCORE_CODES:
            slot = _major_slot(data, row["code"])
            slot.value, slot.unit = row["score"], "分"
            slot.assessment_level = catalog.template_nutrition_grade(row["score"])
            slot.reference_range = catalog.MAJOR_NUTRITION_SCORE_RANGE
            slot.explanation = AI_MAJOR_SCORE_PREFIX + row["explanation"]
            slot._assessment_origin = "report"
            slot.sources = _target_sources(targets[row["code"]], fact_ids=row["factIds"])
            slot.availability = "available"
            continue
        slot = next(slot for slot in data.health_overview.assessment_chart if slot.code == row["code"])
        slot.score, slot.assessment_level = row["score"], row["assessmentLevel"]
        slot.explanation = (None if row['code'] in NUMERIC_OVERVIEW_CODES else
            AI_SCORE_PREFIX + "（0–100，越高表示本次综合状态越好）：" + row["explanation"])
        slot.sources = _target_sources(targets[row["code"]])
        slot.availability = "available"
    synchronize_major_pathway_levels(data, document)
    return errors


def _source_metric_identity(row):
    """Match an existing source row, including its complete provenance."""
    return json.dumps({"code": getattr(row, "code", None), "name": row.name,
        "value": row.value, "valueType": type(row.value).__name__, "unit": row.unit,
        "referenceRange": getattr(row, "reference_range", None), "sourceStatus": row.source_status,
        "sources": sorted((source.model_dump(mode="json", by_alias=True) for source in row.sources),
            key=lambda value: json.dumps(value, sort_keys=True))},
        ensure_ascii=False, sort_keys=True, allow_nan=False)


def _antimicrobial_identities(data, facts):
    identities = {_source_metric_identity(row)
        for row in data.microbiome_assessment.antimicrobial_resistance if row.sources}
    # The public table combines normal drug classes into one row. Other
    # assessments retain individual assays from the typed source inventory.
    identities.update(_source_metric_identity(row) for row in data.appendices.source_items
        if row.kind == "function" and row.category == "antimicrobialResistance"
        and row.source_type == "microbiome" and row.sources
        and all(source.source_code == "C" and source.record_id in facts for source in row.sources))
    return identities


def _level_inventory(rows, facts, clinical_context=None, antimicrobial_context=None, virulence_ids=None):
    result = []
    for row in rows:
        if (getattr(row, "code", None) or "").startswith("panelSummary:"):
            continue
        ids = _assessment_fact_ids(row, facts)
        if not ids or (row.value is None and not getattr(row, "source_status", None)):
            continue
        item = {"name": row.name, "value": row.value, "unit": getattr(row, "unit", None),
            "sourceStatus": getattr(row, "source_status", None), "assessmentLevel": getattr(row, "assessment_level", None),
            "referenceRange": getattr(row, "reference_range", None), "factIds": ids,
            "sourceRefs": [source.model_dump(mode="json", by_alias=True) for source in row.sources]}
        _add_clinical_context(item, row, ids, facts, clinical_context or {})
        _add_assessment_origin(item, row)
        if (getattr(row, "code", None) or "").startswith("association:"):
            item["resultKind"] = "sourceHealthAssociation"
        source_model_level = getattr(row, "source_model_level", None)
        if source_model_level is not None:
            item["sourceModelLevel"] = source_model_level
        if antimicrobial_context and _source_metric_identity(row) in antimicrobial_context:
            # Classification comes from the actual typed AMR section. A drug
            # class name or a normal source grade alone cannot establish it.
            item["resultKind"] = "antimicrobialResistanceGene"
            item["sourceSection"] = "microbiomeAssessment.antimicrobialResistance"
        elif virulence_ids and virulence_ids.intersection(ids):
            item["resultKind"] = "virulenceGene"
            item["sourceSection"] = "microbiomeAssessment.healthMetrics"
        _append_measurement(result, item)
    return result


def synchronize_disease_index_levels(data, *, integrated_codes=()):
    """Project the highest confirmed index grade into both category views.

    This selects already supplied/approved grades. It never interprets an
    unknown numerical scale, averages indexes or uses clinical severity to
    change a microbiome grade. Qualitative association panels are not indexes.
    Without a reviewed integrated judgement, the returned codes identify a
    source-index fallback. A separately reviewed category judgement is retained
    only for the explicitly supplied integrated_codes; source items are never
    rewritten.
    """
    from modules.report.evaluation_fixed_assessment import category_clinical_attention
    from modules.report.evaluation_disease_context import native_category_basis, clinical_attention_basis

    determined = set()
    matrix = {row.code: row for row in data.disease_risk_assessment.overview_matrix}
    for category in data.disease_risk_assessment.categories:
        from .evaluation_schema import _reviewed_disease_attention_matches
        if category.code in integrated_codes or _reviewed_disease_attention_matches(data, category):
            # A separately reviewed category judgement is not the maximum
            # source model grade. The per-disease original values stay intact.
            determined.add(category.code)
            continue
        indexed = [row for row in category.items if has_risk_index(row)
            and not (row.code or "").startswith("association:")]
        graded = [(row, row.assessment_level if row.assessment_level in _INDEX_RISK_LEVELS else row.source_status)
            for row in indexed]
        graded = [(row, level) for row, level in graded if level in _INDEX_RISK_LEVELS]
        overview = matrix.get(category.code)
        clinical = category_clinical_attention(data, category.code)
        clinical_sources = []
        for item in clinical:
            for source in item.sources:
                if source not in clinical_sources:
                    clinical_sources.append(source.model_copy(deep=True))
        clinical_basis = clinical_attention_basis(data, clinical)
        native_level, native_basis, native_sources = native_category_basis(data, category.code)
        if not indexed:
            category.attention_level = '注意' if clinical else native_level
            category.attention_basis = '关注线索：' + clinical_basis if clinical else native_basis
            if clinical and native_basis:
                category.attention_basis += '；' + native_basis
            combined_sources = [*clinical_sources, *(s for s in native_sources if s not in clinical_sources)]
            category.sources = combined_sources
            if overview is not None:
                overview.attention_level, overview.basis = category.attention_level, category.attention_basis
                if clinical or native_sources:
                    overview.sources = combined_sources
                    overview.availability = 'partial'
            if clinical or native_sources:
                category.availability = 'partial'
                determined.add(category.code)
            continue
        if not graded:
            continue
        level = max((level for _, level in graded), key=_INDEX_RISK_LEVELS.index)
        selected = [row for row, item_level in graded if item_level == level]
        basis = "；".join(f"{row.name}：{row.value}（{level}）" for row in selected)
        sources = []
        for row in selected:
            for source in row.sources:
                if source not in sources:
                    sources.append(source.model_copy(deep=True))
        # This is the category's integrated attention, not a rewrite of any C
        # disease index. Actual A/B abnormalities or symptoms remain relevant
        # even when every source microbiome model in the category is low.
        if clinical:
            if level in {'低风险', '中低风险'}:
                level = '注意'
            basis += '；关注线索：' + clinical_basis
            for source in clinical_sources:
                if source not in sources:
                    sources.append(source)
        if native_basis:
            basis += '；' + native_basis
            if native_level == '注意' and level in {'低风险', '中低风险'}:
                level = '注意'
            for source in native_sources:
                if source not in sources:
                    sources.append(source)
        from .evaluation_disease_catalog import source_catalog_family
        if source_catalog_family(data) in {'GM', 'G1', 'GS.meta'}:
            # Model grades remain literal on each disease row. Integrated
            # attention uses the report's separate four-level matrix legend.
            level = {'低风险': '正常', '中低风险': '注意', '中风险': '中风险',
                '中高风险': '高风险', '高风险': '高风险'}.get(level, level)
        category.attention_level, category.attention_basis = level, basis
        category.availability = "available" if len(graded) == len(category.items) else "partial"
        if overview is not None:
            overview.attention_level, overview.basis = level, basis
            overview.sources = sources
            overview.availability = category.availability
        determined.add(category.code)
    return determined


def _measurement_scope(inventory):
    """Literal source measurements and provenance, without authored role tags."""
    result = set()
    for item in inventory:
        fields = {key: item.get(key) for key in ("name", "value", "unit", "referenceRange", "sourceStatus")}
        fields["valueType"] = type(item.get("value")).__name__
        fields["factIds"] = sorted(set(item.get("factIds") or []))
        fields["sourceRefs"] = sorted({json.dumps(source, ensure_ascii=False, sort_keys=True)
            for source in item.get("sourceRefs") or []})
        result.add(json.dumps(fields, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return result


def _same_major_pathway_scope(target, pathway, facts):
    if pathway.value is not None or (pathway.source_status or "").strip():
        return False  # A native total/label, including zero, owns its own result.
    inventory = _level_inventory([*pathway.related_metrics, *pathway.related_taxa], facts)
    ids = sorted({fid for item in inventory for fid in item["factIds"]})
    return bool(inventory and ids == target["allowedFactIds"]
        and _source_fact_digest(ids, facts) == target["sourceFactDigest"]
        and _measurement_scope(inventory) == _measurement_scope(target["inventory"]))


def synchronize_major_pathway_levels(data, document):
    """Reuse a validated macro judgment only for the exact same pathway scope.

    No score is calculated here. A missing/invalid macro result leaves the
    corresponding pathway ungraded; different scope keeps independent review.
    """
    facts = {fact.fact_id: fact for fact in document.facts}
    targets = {target["code"]: target for target in _major_score_targets(data, facts)}
    saved = _load_rows(document, SCORE_BLOCK_CODE, "scores")
    linked = set()
    for code, path_code in _MAJOR_PATHWAYS.items():
        pathway = next((row for row in data.functional_pathways.pathways if row.code == path_code), None)
        metric = _major_slot(data, code)
        target = targets.get(code)
        rows = [row for row in saved if isinstance(row, dict) and row.get("code") == code]
        if pathway is None:
            continue
        row = rows[0] if len(rows) == 1 else None
        ids = row.get("factIds") if row else None
        valid = bool(target and row and metric and metric._assessment_origin == "report"
            and type(metric.value) is int and 0 <= metric.value <= 100
            and type(row.get("score")) is int
            and metric.value == row.get("score") and metric.unit == "分"
            and metric.reference_range == catalog.MAJOR_NUTRITION_SCORE_RANGE
            and metric.assessment_level == row.get("assessmentLevel") == catalog.template_nutrition_grade(metric.value)
            and metric.explanation == AI_MAJOR_SCORE_PREFIX + str(row.get("explanation") or "")
            and row.get("sourceDigest") == target["sourceDigest"]
            and isinstance(row.get("explanation"), str) and 8 <= len(row["explanation"].strip()) <= 1600
            and isinstance(ids, list) and ids and all(type(fid) is str for fid in ids)
            and set(ids) <= set(target["allowedFactIds"])
            and _grounded(row["explanation"], ids, facts, target["inventory"])
            and not _unsupported_reason(row["explanation"], ids, facts)
            and metric.sources == _target_sources(target, fact_ids=ids)
            and _same_major_pathway_scope(target, pathway, facts))
        if valid:
            pathway.assessment_level = metric.assessment_level
            pathway.explanation = metric.explanation
            pathway._assessment_origin = "report"
            linked.add(path_code)
        elif (pathway.explanation or "").startswith(AI_MAJOR_SCORE_PREFIX) and not (pathway.source_status or "").strip():
            pathway.assessment_level, pathway.explanation, pathway._assessment_origin = None, None, None
    return linked


def build_level_targets(data, document, *, major_score_targets=None, include_priority=False):
    """Request missing pathway and clinically integrated risk judgements."""
    facts = {fact.fact_id: fact for fact in document.facts}
    association_ids = _health_association_ids(data, facts)
    from .evaluation_risk_assessment import approved_assessments
    integrated = bool(approved_assessments(document))
    if not integrated:
        synchronize_disease_index_levels(data)
    linked_pathways = synchronize_major_pathway_levels(data, document)
    current_major = {target["code"]: target for target in _major_score_targets(data, facts)}
    for planned in major_score_targets or []:
        code = planned.get("code") if isinstance(planned, dict) else None
        actual = current_major.get(code)
        pathway = next((row for row in data.functional_pathways.pathways
            if row.code == _MAJOR_PATHWAYS.get(code)), None)
        if (actual and pathway and planned.get("sourceDigest") == actual["sourceDigest"]
                and _same_major_pathway_scope(actual, pathway, facts)):
            linked_pathways.add(pathway.code)
    antimicrobial_context = _antimicrobial_identities(data, facts)
    virulence_ids = {source.record_id for row in data.appendices.source_items
        if row.kind == "context" and row.category == "virulence" and row.source_type == "microbiome"
        for source in row.sources if source.source_code == "C" and source.record_id in facts}
    targets = []
    scored_modules = {row.get('code') for row in major_score_targets or []
        if isinstance(row, dict) and row.get('code') in {'medical', 'nutrition', 'overall'}}
    for slot in data.health_overview.assessment_chart:
        if str(slot.code or '').startswith('component:'):
            continue  # Never reinterpret a rule/AI illustration as a native module.
        if slot.code in scored_modules or numeric_score_reviewed(data, document, slot.code):
            # The pending score includes its own jointly reviewed grade. A
            # completed numeric-only AI card keeps the same joint decision:
            # its empty caption must not reclassify it as a native score and
            # clear its grade while an unrelated pathway batch is applied.
            continue
        qualitative_only = slot.score is None and slot.code in {"medical", "nutrition", "overall"}
        if (not (_native(slot) or qualitative_only)
                or source_module_level(data, slot.code) is not None):
            continue
        inventory = _level_inventory(_rows(data, slot.code), facts)
        if not inventory:
            continue
        targets.append({"code": "module:" + slot.code, "kind": "module", "name": slot.name,
            "sourceScore": slot.score, "allowedLevels": sorted(SCORE_LEVELS),
            "allowedFactIds": sorted({fid for row in inventory for fid in row["factIds"]}),
            "requiredFactIds": [], "inventory": inventory,
            "rules": ["原报告已有总分，保持原分数和来源说明，仅补AI综合评级及依据。" if not qualitative_only else
                "本模块没有原始总分，只给真实资料支持的定性综合评级及依据，不计算、填写或反推数字总分。",
                "结合真实各维度判断，不将某个来源指标原级直接复制为整体级，不制定新的医学阈值。"]})
    for pathway in data.functional_pathways.pathways:
        if pathway.code in linked_pathways:
            continue
        if pathway.source_status is not None and str(pathway.source_status).strip():
            continue
        panel_code = {'P9': 'virulenceGenes', 'P10': 'antibioticResistanceGenes'}.get(pathway.code)
        panel_rows = [row for row in data.microbiome_assessment.health_metrics
            if panel_code and row.code == panel_code and row.sources
            and (row.value is not None or row.source_status not in (None, ''))
            and _assessment_fact_ids(row, facts)]
        panel_keys = {(row.name, tuple(sorted(_assessment_fact_ids(row, facts)))) for row in panel_rows}
        inventory = _level_inventory([*pathway.related_metrics, *pathway.related_taxa, *panel_rows], facts,
            antimicrobial_context=antimicrobial_context, virulence_ids=virulence_ids)
        for item in inventory:
            if (item['name'], tuple(sorted(item['factIds']))) in panel_keys:
                item['resultKind'] = 'sourceGenePanelSummary'
                item['sourceSection'] = 'microbiomeAssessment.healthMetrics'
        if not pathway.sources or not inventory:
            continue
        if pathway.code == 'P9' and not any(item.get('sourceStatus') or item.get('assessmentLevel')
                for item in inventory):
            # An unlabeled generation score has no confirmed low/high health
            # meaning. Keep the original values without asking AI to invent
            # a grade. A real native aggregate owns its level above.
            continue
        levels = (["正常", "注意", "失衡", "超标"] if pathway.code == "P9" else
            ["正常", "注意", "较弱", "弱", "失衡"] if pathway.code == "P10" else ["强", "正常", "较弱", "弱"])
        targets.append({"code": "pathway:" + pathway.code, "kind": "pathway", "name": pathway.name,
            "allowedLevels": levels, "allowedFactIds": sorted({fid for row in inventory for fid in row["factIds"]}),
            "requiredFactIds": [], "inventory": inventory,
            "rules": ["按本通路真实子项的功能关系综合判断，不把子项数量或正常比例当综合等级。",
                "judgment、explanation与basis不展示未检出检测项，仅从实际检出、正常实测和异常功能中选取少量依据；保留原始零值和正常标签，不将其换写成未检出。",
                "以下来源与推断边界仅用于内部校验，不抄入judgment、explanation或basis；直接写本次功能格局与关注重点，不重复通用免责声明。",
                "本目标只判断当前一条通路，不将同一请求批次的其他通路当作全部通路，不作跨通路最高/最低排名。",
                "保留相关项目原值与原分级；总体等级为AI综合评估，不是来源原级或人体功能实测。",
                *(["sourceGenePanelSummary是本报告真实类别总项，成员原值与该总项分别保留。判断提到类别总项名称或总项状态时，basis须绑定该总项自己的factIds，不能仅引用同类成员替代。总项检出不能反推所有成员检出，成员正常也不能改写为未检出。"]
                    if panel_rows else []),
                *(["本通路同时包含实际有害代谢物功能与毒力基因检测时，分别解释两类结果；一种代谢功能的正常原级不能覆盖毒力基因信号，也不能反推所有子项正常。",
                    "检测对象为基因标记；只展示本次实际检出的基因信号，结合已采用功能状态给出关注判断与少量依据。感染诊断边界和泛化临床核实语属于内部校验内容，不写入正文；候选基因不作为检出。只有定性检出而无阈值时不得称数值超标。",
                    "有害代谢物只按真实原分级和明确参考标准解释高低；未分级数值不能自行推断负荷偏高、生成偏低或不足。"]
                    if pathway.code == 'P9' else []),
                *(["本通路中的菌群外源物降解能力与抗生素耐药基因检测分别解读；耐药基因正常不代表所有降解功能正常，也不代表人体抗生素防御、耐受性或药敏结果。",
                    "指出实际较弱的降解项目及保留的功能，原始耐药基因分级单独保留；不据此推断人体解毒能力或免疫缺陷，不补写未检测的生物膜、氧化应激结果。"]
                    if pathway.code == 'P10' else [])]})
    clinical = _level_inventory([*data.medical_assessment.findings, *data.medical_assessment.normal_findings], facts,
        _clinical_inventory_context(data))
    for category in data.disease_risk_assessment.categories:
        source_models = _level_inventory([row for row in category.items
            if has_risk_index(row) and not (row.code or "").startswith("association:")], facts)
        reported = _level_inventory(data.medical_assessment.questionnaire_summary, facts)
        for item in reported:
            row = next(row for row in data.medical_assessment.questionnaire_summary
                if row.name == item["name"] and set(item["factIds"]).intersection(
                    source.record_id for source in row.sources))
            item["useAs"] = _source_use_as(row, item["factIds"], facts, domain="reportedBackground")
        reported = [item for item in reported if item.get('useAs') != 'source_context'
            or disease_context_relevant(category.code, str(item.get('value') or ''))]
        relevant_ids = {fact.fact_id for fact in document.facts
            if fact.kind in {'clinical','self_reported'}
            and disease_context_relevant(category.code, fact.name if fact.value.startswith('问题：')
                else fact.name+' '+fact.value)}
        if category.code == 'neurological':
            # Brain-flow or cervical findings cannot become background
            # evidence for every neurodegenerative/epilepsy model.
            from .evaluation_narrative import _disease_clinical_facts
            relevant_ids = {fact.fact_id for item in category.items
                for fact in _disease_clinical_facts(category.code,
                    item.model_dump(mode='json', by_alias=True), document)}
        clinical_context = [row for row in clinical if relevant_ids.intersection(row['factIds'])]
        reported = [row for row in reported if row.get('useAs') != 'patient_report'
            or relevant_ids.intersection(row['factIds'])]
        if category.code == 'aging':
            relevant_ids = {fact.fact_id for fact in aging_clinical_facts(document.facts)}
            clinical_context = [row for row in clinical if relevant_ids.intersection(row['factIds'])]
            reported = [row for row in reported if row.get('useAs') != 'patient_report'
                or relevant_ids.intersection(row['factIds'])]
        covered = {fid for item in [*clinical_context, *reported] for fid in item['factIds']}
        for fact in document.facts:
            if fact.fact_id not in relevant_ids or fact.fact_id in covered:
                continue
            # Display grouping is not a prerequisite for using a real source.
            # These remain records/answers, not diagnoses inferred from a
            # question label, family-history answer or an affirmative flag.
            item = {'name': fact.name, 'value': fact.value, 'unit': fact.unit,
                'sourceStatus': fact.status if fact.kind == 'clinical' else None,
                'assessmentLevel': None, 'referenceRange': fact.reference_range,
                'factIds': [fact.fact_id], 'sourceRefs': [SourceReference(
                    source_code='A' if fact.kind == 'clinical' else 'B',
                    record_id=fact.fact_id, observed_at=fact.date).model_dump(mode='json', by_alias=True)],
                'useAs': 'clinical_record' if fact.kind == 'clinical' else 'patient_report'}
            (clinical_context if fact.kind == 'clinical' else reported).append(item)
        source_taxa = _level_inventory([taxon for item in category.items
            for taxon in item.related_taxa], facts)
        inventory = [*source_models, *source_taxa, *clinical_context, *reported]
        if not inventory:
            continue
        targets.append({"code": "disease:" + category.code, "kind": "disease", "name": category.name,
            "allowedLevels": ["正常", "注意", "中风险", "高风险"],
            "allowedFactIds": sorted({fid for row in inventory for fid in row["factIds"]}),
            "requiredFactIds": [], "inventory": inventory, "sourceModelItems": source_models,
            "clinicalContext": clinical_context, "reportedContext": reported,
            "sourceMicrobialSignals": source_taxa,
            "rules": ["判断临床综合关注等级，不直接复制组内最高来源模型等级，不把模型分数当患病概率。",
                "sourceHealthAssociation仅是来源列出的相关菌群信号，不是疾病检出、原模型数值或临床确诊。",
                "原报告内病史、症状和用药按原出处及自报性质参与判断，不当作独立新问卷或医院确诊。",
                "医学检查、真实问卷、其他报告及实际菌群模型共同参与综合关注判断，不要求固定来源。sourceModelItems为空时只依据本类真实医学与自述资料，不补造菌群指数或等级。",
                "问卷问题名称用于定位主题，不能当本人肯定患病事实；否认、家族史、历史与当前状态按实际回答分别保留，不将亲属疾病或健康目标变成本人已有疾病。",
                "只采用与该类别相关的实际临床依据，不将无资料写成无疾病；实际有来源模型时解释其组内差异和主要关注方向，不重复临床资料缺项说明。"]})
    for target in targets:
        for item in target['inventory']:
            _add_association_kind(item, association_ids)
        target["rules"] = [*target["rules"], *ASSESSMENT_METADATA_POSITIVE_RULES]
        target["sourceFactDigest"] = _source_fact_digest(target["allowedFactIds"], facts)
        target["sourceDigest"] = hashlib.sha256(json.dumps(target, ensure_ascii=False,
            sort_keys=True, allow_nan=False).encode()).hexdigest()
    if integrated:
        targets = [target for target in targets if target['kind'] != 'disease']
    if include_priority and not integrated:
        targets.extend(build_priority_level_targets(data,document))
    return targets


def _priority_medical_inventory(rows,facts,clinical_context):
    """Bind each original medical/report fact once, including shared answers.

    One questionnaire answer can support two symptom headings. The headings
    are relations over that answer, not two different source identities.
    """
    by_id={}
    for row in rows:
        for fid in _assessment_fact_ids(row,facts):
            by_id.setdefault(fid,[]).append(row)
    inventory=[]
    for fid,linked in by_id.items():
        fact=facts[fid]
        exact=[row for row in linked if fact.name==row.name or fact.value==row.value
            or fact.kind=='self_reported'and len(fact.name)>=2
                and fact.name in str(row.name or '')+' '+str(row.value or '')]
        selected=exact[0]if len(exact)==1 else linked[0]
        own=exact if len(exact)==1 else linked if len(linked)==1 else []
        levels={getattr(row, 'assessment_level', None) for row in own
            if getattr(row, 'assessment_level', None)}
        source_status=(None if fact.kind=='self_reported'else fact.status)
        item={'name':fact.name,'value':fact.value,'unit':fact.unit,
            'sourceStatus':source_status,
            'assessmentLevel':next(iter(levels))if len(levels)==1 else None,
            'referenceRange':fact.reference_range,'factIds':[fid],
            'sourceRefs':[source.model_dump(mode='json',by_alias=True)
                for row in linked for source in row.sources if source.record_id==fid],
            'useAs':_source_use_as(selected,[fid],facts)}
        _add_clinical_context(item,selected,[fid],facts,clinical_context)
        if own:
            _add_assessment_origin(item,selected)
        if fact.kind=='self_reported'and not exact:
            item['resultKind']='questionnaireContext'
        item['sourceFindings']=[{'name':row.name,'value':row.value,
            'assessmentLevel':getattr(row, 'assessment_level', None),'certainty':getattr(row,'certainty',None)}
            for row in linked]
        inventory.append(item)
    return inventory


def _priority_clinical_priorities(data,document,facts,allowed):
    """Retain source-exact priority foci when a problem also has workflow text."""
    priorities=[]
    for problem in document.problems:
        if problem.priority!='priority':
            continue
        fragments=[part.strip()for part in re.split(r'[，,、；;。]',problem.title)
            if len(_key(part))>=2]
        for fragment in fragments:
            ids=sorted({source.fact_id for source in problem.sources
                if source.fact_id in allowed and facts[source.fact_id].kind=='clinical'
                and source.quote.strip()and source.quote.strip()in facts[source.fact_id].value
                and fragment in source.quote})
            if not ids:
                continue
            priorities.append({'name':fragment,'factIds':ids,'focusAnchors':[fragment],
                'sourceProblemTitle':problem.title,'certainty':problem.certainty})
            break
    known={fid for item in priorities for fid in item['factIds']}
    for item in _clinical_priorities(data,document,facts,allowed):
        if not known.intersection(item['factIds']):
            priorities.append(item)
    return priorities


def build_priority_level_targets(data, document):
    from .evaluation_risk_assessment import approved_assessments
    if approved_assessments(document):
        return []
    """Independent composite attention over each complete actual theme."""
    from .evaluation_priority_focus import priority_topics, PRIORITY_LEVELS
    from .evaluation_synthesis import native_function_source_context
    facts={fact.fact_id:fact for fact in document.facts}
    clinical_context=_clinical_inventory_context(data)
    antimicrobial_context=_antimicrobial_identities(data,facts)
    native=native_function_source_context(data.model_dump(mode='json',by_alias=True),facts)
    result=[]
    for topic in priority_topics(data,facts=facts):
        medical=topic.get('kind')=='medical' or topic['code']=='priority:medical'
        inventory=(_priority_medical_inventory(topic['rows'],facts,clinical_context)if medical else [])
        for row in ([]if medical else topic['rows']):
            ids=_assessment_fact_ids(row,facts)
            value=getattr(row,'value',None)
            if value is None:
                value=getattr(row,'observations',None)
            if not ids or value is None:
                continue
            item={'name':row.name,'value':value,'unit':getattr(row,'unit',None),
                'sourceStatus':getattr(row,'source_status',None),
                'assessmentLevel':getattr(row,'assessment_level',None),
                'referenceRange':getattr(row,'reference_range',None),'factIds':ids,
                'sourceRefs':[source.model_dump(mode='json',by_alias=True)for source in row.sources],
                'useAs':_source_use_as(row,ids,facts)}
            if getattr(row, 'category', None) == 'sourceReportedBackground':
                item.update(resultKind='sourceReportedBackground', useAs='source_context')
            _add_assessment_origin(item,row)
            _add_clinical_context(item,row,ids,facts,clinical_context)
            if (hasattr(row, 'value') and hasattr(row, 'unit') and hasattr(row, 'source_status')
                    and _source_metric_identity(row) in antimicrobial_context):
                item['resultKind']='antimicrobialResistanceGene'
                item['sourceSection']='microbiomeAssessment.antimicrobialResistance'
            _append_measurement(inventory,item)
        allowed={fid for item in inventory for fid in item['factIds']}
        for group in native:
            if not allowed.intersection(fid for member in group['members']for fid in member['factIds']):
                continue
            for fid in group['factIds']:
                if fid not in facts:
                    continue
                fact=facts[fid]
                inventory.append({'name':fact.name,'value':fact.value,'unit':fact.unit,
                    'sourceStatus':fact.status,'assessmentLevel':None,'referenceRange':fact.reference_range,
                    'factIds':[fid],'sourceRefs':[dict(sourceCode='C',recordId=fid,observedAt=fact.date)],
                    'useAs':'source_context','resultKind':'nativeNutritionState'})
        if not inventory:
            continue
        allowed=sorted({fid for item in inventory for fid in item['factIds']})
        priorities=_priority_clinical_priorities(data,document,facts,allowed)if medical else []
        target={'code':topic['code'],'kind':'priority','name':topic['name'],
            'allowedLevels':list(PRIORITY_LEVELS),'allowedFactIds':allowed,
            'requiredFactIds':priorities[0]['factIds']if priorities else [],
            'clinicalPriorities':priorities,'inventory':inventory,
            'rules':['只综合当前完整主题，按实际状态和医学重点评高/中/低/注意，不串联原始偏高偏低或弱等标签。',
                '关注级表示本次管理优先程度：高为需要优先回应的重要异常或突出风险；中为需要持续管理的明确问题；注意为局部偏离或需观察的信号；低为目前稳定、常规观察即可的状态。根据实际性质判断，不按子项数量定级，也不因未有体检就预设低级。',
                '临床重要异常、持续代谢问题与医生只建议常规随访的偶然发现应分层，不把所有异常、囊肿或模型中等级一律判中；原诊断、原级和原建议完整保留。',
                'sourceReportedBackground是本人的原件病史背景，可用于同主题个体相关性；不升级为本次体检确诊，也不能否认该背景已提供。',
                'judgment直接说明具体问题、已有依据与关注程度，不使用来源模型、原模型记录、仅此一项或未见其他资料等处理说明作开头。',
                '不取最高子项、正常比例、均值或数量作为综合级；原值与每项原级在原表保留。',
                'judgment用一至两句说明本主题整体格局与关注程度，不出现具体菌属/菌种清单，不抄数值或诊断边界。',
                '医学主题有多项资料时优先本次真正重要的已采用异常，其他原始事实仍完整保留。',
                *(['judgment只用整体均衡、分布格局、少数偏离与关注程度作简短综合判断，不点名具体菌群，也不写产某代谢物菌类或菌群功能类别；真实原名、原值及原级完整留在原表与内部basis。不把同组菌群逐项改写成综合核心提示。']
                    if topic['code']in {'priority:beneficialBalance','priority:pathogenSignals'}else []),
                *(['本主题只评本次已测致病及条件致病菌信号的综合关注程度；按真实组内分布、偏离程度与集中方向给出等级，无需临床确诊感染作为给级前提。单项菌偏高或偏低不能直接复制为综合等级，也不代表临床感染严重度；judgment正向说明实际信号格局。']
                    if topic['code']=='priority:pathogenSignals'else []),
                *(['菌群外源物降解功能与耐药基因检测分别解释；正常耐药原级不能代表全部降解能力正常，也不能推断人体抗生素防御、耐受性或药敏结果。']
                    if topic['code']=='priority:otherFunction'else []),
                *ASSESSMENT_METADATA_POSITIVE_RULES]}
        target['sourceFactDigest']=_source_fact_digest(allowed,facts)
        if topic.get('concrete'):
            target['concreteProblem'] = True
            target['rules'] = [rule for rule in target['rules']
                if not rule.startswith('judgment用一至两句')]
            target['rules'].append(
                'judgment围绕本项具体健康问题写一至两句，简明连接实际异常、适用菌群证据与个人关注重点；'
                '只使用本项已绑定的菌名和事实，不抄写菌群清单，不将关联写成确诊。')
        target['sourceDigest']=hashlib.sha256(json.dumps(target,ensure_ascii=False,sort_keys=True,
            allow_nan=False).encode()).hexdigest()
        result.append(target)
    return result


def _priority_judgment_failures(row,target,facts):
    text=row.get('judgment')
    if not isinstance(text,str)or not 8<=len(text.strip())<=120:
        return ['priority_judgment_required：judgment须为8至120字的完整综合判断，说明整体格局与关注程度。']
    if re.match(r'\s*(?:来源(?:疾病)?模型|原模型|原报告.{0,12}(?:提示|标记|记载))', text):
        return ['priority_patient_judgment_required：直接说明菌群相关风险或当前问题、少量依据和关注程度；'
            '不以来源模型处理说明代替判断，原风险性质、原值与原级保持不变。']
    if (any(item.get('resultKind') == 'sourceReportedBackground' for item in target.get('inventory', []))
            and re.search(r'仅此一项|只有.{0,8}模型|未见其他.{0,12}(?:依据|背景|证据)', text)):
        return ['priority_existing_background_denied：当前同主题已有原件病史背景，应按病史性质纳入，'
            '不能说仅有模型或没有其他依据，也不改成此次确诊。']
    stated = re.findall(r'(?:整体|综合|本项|本组)?(?:的)?关注(?:程度|等级|水平)'
        r'(?:为|是|处于|判为|：|:)?\s*(高|中|低|注意)', text)
    if any(level != row.get('assessmentLevel') for level in stated):
        return ['priority_level_judgment_conflict：综合关注等级与judgment明确写出的关注程度不一致；'
            '请依据完整来源重新同时给出一致的assessmentLevel、judgment和basis，不能只改文字掩盖分歧。']
    matched=set()
    for fid in target['allowedFactIds']:
        fact=facts[fid]
        if fact.kind=='microbe':
            matched.update(alias.strip()for alias in fact.name.split(' / ')
                if alias.strip()and alias.strip()in text)
    if matched and not target.get('concreteProblem'):
        return ['priority_judgment_must_not_enumerate_taxa：judgment命中具体菌名“'
            +'、'.join(sorted(matched))+'”；本栏只概括整体分布、局部偏离与关注程度，'
            '不列单个或多个具体菌属/菌种名称。请按本target完整来源重写judgment，'
            '菌名与原值仍完整保留原表及内部basis，重新同时返回assessmentLevel和basis并接受独立审核。']
    return []


def apply_priority_levels(data,document,rows=None,*,reason_validator=None):
    from .evaluation_risk_assessment import approved_assessments
    if approved_assessments(document):
        return []
    """Restore reviewed theme metadata without replacing any source result."""
    from .evaluation_priority_focus import install_reviewed_priority,priority_identity
    facts={fact.fact_id:fact for fact in document.facts}
    targets={target['code']:target for target in build_priority_level_targets(data,document)}
    restoring=rows is None
    if restoring:
        rows=[row for row in _load_rows(document,LEVEL_BLOCK_CODE,'levels')
            if isinstance(row,dict)and str(row.get('code')or'').startswith('priority:')]
        if not rows:
            return []
    rows=rows if isinstance(rows,list)else []
    if any(not any(priority_identity(slot)==code for slot in data.health_overview.key_findings)
            for code in targets):
        from .evaluation_priority_focus import project_priority_focus
        project_priority_focus(data,facts=facts)
    errors,validated=[],[]
    for code,target in targets.items():
        matches=[row for row in rows if isinstance(row,dict)and row.get('code')==code]
        reason=None
        if len(matches)!=1:
            reason='missing_or_duplicate'
        else:
            row=matches[0];ids=row.get('factIds');text=row.get('explanation')
            if 'score'in row or 'value'in row:
                reason='qualitative_target_has_no_numeric_score'
            elif row.get('assessmentLevel')not in target['allowedLevels']:
                reason='invalid_priority_level'
            elif not isinstance(ids,list)or not ids or not all(isinstance(fid,str)for fid in ids)or not set(ids)<=set(target['allowedFactIds']):
                reason='unbound_fact'
            elif not set(target['requiredFactIds'])<=set(ids):
                reason='omitted_medical_priority'
            elif not isinstance(text,str)or not _grounded(text,ids,facts,target['inventory']):
                reason='unbound_explanation'
            elif (judgment_failures:=_priority_judgment_failures(row,target,facts)):
                reason='invalid_priority_judgment:'+ ';'.join(judgment_failures)
            elif _unsupported_reason(text,ids,facts)or source_history_affirmation_failures(text,target,facts=facts):
                reason='unsupported_explanation'
            elif reason_validator is not None and reason_validator(row,target):
                reason='unsupported_explanation'
            elif restoring and row.get('sourceDigest')!=target['sourceDigest']:
                reason='source_changed'
            elif not assessment_metadata_reviewed(document,row,target):
                reason='independent_review_required'
        if reason:
            errors.append('AI_LEVEL_INVALID:'+code+':'+reason)
            slot=next((slot for slot in data.health_overview.key_findings if priority_identity(slot)==code),None)
            if slot is not None:
                slot.assessment_level=None
                slot.availability='partial'
                if hasattr(slot,'_reviewed_priority_projection'):
                    object.__delattr__(slot,'_reviewed_priority_projection')
            continue
        saved={key:row[key]for key in ('code','assessmentLevel','judgment','explanation','factIds')}
        saved['factIds']=sorted(set(ids));saved['sourceDigest']=target['sourceDigest']
        validated.append(saved)
        install_reviewed_priority(data,saved,facts=facts)
    if any(not isinstance(row,dict)or row.get('code')not in targets for row in rows):
        errors.append('AI_LEVEL_INVALID:unknown_priority_target')
    if not restoring:
        legacy=[row for row in _load_rows(document,LEVEL_BLOCK_CODE,'levels')
            if isinstance(row,dict)and not str(row.get('code')or'').startswith('priority:')]
        _store_rows(document,LEVEL_BLOCK_CODE,'levels',[*legacy,*validated])
    return errors


def apply_ai_levels(data, document, rows=None, *, reason_validator=None):
    """Persist/reapply qualitative AI levels without altering native metrics."""
    if isinstance(rows,list):
        priorities=[row for row in rows if isinstance(row,dict)
            and str(row.get('code')or'').startswith('priority:')]
        if priorities:
            errors=apply_priority_levels(data,document,priorities,reason_validator=reason_validator)
            remaining=[row for row in rows if row not in priorities]
            return errors+(apply_ai_levels(data,document,remaining,reason_validator=reason_validator)
                if remaining else [])
    targets = {target["code"]: target for target in build_level_targets(data, document)}
    facts = {fact.fact_id: fact for fact in document.facts}
    restoring = rows is None
    if restoring:
        rows = _load_rows(document, LEVEL_BLOCK_CODE, "levels")
        priority_errors=apply_priority_levels(data,document,reason_validator=reason_validator)
        rows=[row for row in rows if not isinstance(row,dict)
            or not str(row.get('code')or'').startswith('priority:')]
    else:
        priority_errors=[]
    if not isinstance(rows, list):
        rows = []
    from .evaluation_risk_assessment import approved_assessments
    if approved_assessments(document):
        rows = [row for row in rows if not isinstance(row, dict)
            or not str(row.get('code') or '').startswith(('disease:', 'priority:'))]
    errors, validated = [], []
    for code, target in targets.items():
        matches = [row for row in rows if isinstance(row, dict) and row.get("code") == code]
        reason = None
        if len(matches) != 1:
            reason = "missing_or_duplicate"
        else:
            row = matches[0]
            text, ids = row.get("explanation"), row.get("factIds")
            if "score" in row or "value" in row:
                reason = "qualitative_target_has_no_numeric_score"
            elif row.get("assessmentLevel") not in target["allowedLevels"]:
                reason = "invalid_composite_level"
            elif not isinstance(text, str) or not 8 <= len(text.strip()) <= 1600:
                reason = "missing_explanation"
            elif not isinstance(ids, list) or not ids or any(type(fid) is not str for fid in ids) or not set(ids) <= set(target["allowedFactIds"]):
                reason = "unbound_fact"
            elif not _grounded(text, ids, facts, target["inventory"]):
                reason = "explanation_must_name_actual_item"
            elif _pathway_comparison_failures(text,target):
                reason = "comparison_outside_single_pathway"
            elif target["kind"] == "disease" and target["sourceModelItems"] and not set(ids) & {fid for item in target["sourceModelItems"] for fid in item["factIds"]}:
                reason = "source_model_not_acknowledged"
            elif target["kind"] == "disease" and not assessment_metadata_reviewed(document, row, target):
                reason = "independent_review_missing_or_source_changed"
            elif _unsupported_reason(text, ids, facts):
                reason = "unsupported_explanation"
            elif source_history_affirmation_failures(text, target, facts=facts):
                reason = "source_health_role_not_preserved"
            elif reason_validator is not None and reason_validator(row, target):
                reason = "unsupported_explanation"
            elif restoring and row.get("sourceDigest") != target["sourceDigest"]:
                reason = "source_changed"
        if reason:
            errors.append(f"AI_LEVEL_INVALID:{code}:{reason}")
            kind, identifier = code.split(":", 1)
            if kind == "pathway":
                item = next(item for item in data.functional_pathways.pathways if item.code == identifier)
                # Only the composite slot is cleared. Source measurements and
                # their individual grades never participate in this cleanup.
                item.assessment_level = None
                if (item.explanation or "").startswith("AI综合评估："):
                    item.explanation = None
            elif kind == "disease":
                matrix = next(item for item in data.disease_risk_assessment.overview_matrix if item.code == identifier)
                category = next(item for item in data.disease_risk_assessment.categories if item.code == identifier)
                category._reviewed_attention = None
                matrix.attention_level = category.attention_level = None
                matrix.basis = category.attention_basis = None
            else:
                item = next(item for item in data.health_overview.assessment_chart if item.code == identifier)
                item.assessment_level = None
                if AI_LEVEL_PREFIX in (item.explanation or ""):
                    item.explanation = item.explanation.split(AI_LEVEL_PREFIX, 1)[0].rstrip() or None
            continue
        validated.append({"code": code, "assessmentLevel": row["assessmentLevel"], "explanation": text.strip(),
            "factIds": sorted(set(ids)), "sourceDigest": target["sourceDigest"],
            **({"judgment": row.get("judgment")} if target["kind"] == "disease" else {})})
    if any(not isinstance(row, dict) or row.get("code") not in targets for row in rows):
        errors.append("AI_LEVEL_INVALID:unknown_or_source_locked_target")
    if not restoring:
        priorities=[row for row in _load_rows(document,LEVEL_BLOCK_CODE,'levels')
            if isinstance(row,dict)and str(row.get('code')or'').startswith('priority:')]
        _store_rows(document, LEVEL_BLOCK_CODE, "levels", [*validated,*priorities])
    for row in validated:
        kind, code = row["code"].split(":", 1)
        if kind == "pathway":
            target = next(item for item in data.functional_pathways.pathways if item.code == code)
            target.assessment_level = row["assessmentLevel"]
            target.explanation = "AI综合评估：" + row["explanation"]
        elif kind == "disease":
            matrix = next(item for item in data.disease_risk_assessment.overview_matrix if item.code == code)
            category = next(item for item in data.disease_risk_assessment.categories if item.code == code)
            matrix.attention_level = category.attention_level = row["assessmentLevel"]
            matrix.basis = category.attention_basis = "AI综合评估：" + row["explanation"]
            for source in _target_sources(targets[row['code']], fact_ids=row['factIds']):
                for owner in (category, matrix):
                    if source not in owner.sources:
                        owner.sources.append(source.model_copy(deep=True))
            from .evaluation_schema import _ReviewedDiseaseAttention, _disease_attention_scope
            ids = tuple(row['factIds'])
            category._reviewed_attention = _ReviewedDiseaseAttention(category.attention_level,
                category.attention_basis, ids, _disease_attention_scope(data, category, ids))
        else:
            slot = next(item for item in data.health_overview.assessment_chart if item.code == code)
            slot.assessment_level = row["assessmentLevel"]
            source_explanation = (slot.explanation or "").split(AI_LEVEL_PREFIX, 1)[0]
            slot.explanation = source_explanation + AI_LEVEL_PREFIX + row["explanation"]
            # A native card may share its initial list with the measured total.
            # Expanding the composite's scope must not mutate that source row.
            slot.sources = list(slot.sources)
            for source in _target_sources(targets[row["code"]]):
                if source not in slot.sources:
                    slot.sources.append(source)
    synchronize_disease_index_levels(data, integrated_codes={
        row['code'].split(':', 1)[1] for row in validated if row['code'].startswith('disease:')})
    synchronize_major_pathway_levels(data, document)
    return [*errors,*priority_errors]
