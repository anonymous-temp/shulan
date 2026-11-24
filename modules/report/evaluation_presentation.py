"""Reader-facing wording of the evaluate report.

The person reading the report deals with one service provider. Where a
result came from (the testing vendor's model, the examining hospital's
summary, an "original report"), how it was captured (self-reported and not
verified) and internal workflow states are provenance for the integrator and
the audit, never content for the reader. Generated prose is also cleared of
the stock patterns that make text read as machine-written.

The report is a formal written assessment handed to the examinee: it states
findings and judgements impersonally (no "您"), and it does not narrate which
input a fact was read from ("问卷显示", "菌群检测显示"); the inputs are material
for the analysis, the report is its result.

Formatting preserves results and numbers. Urgent digestive warning signs
route readers to clinical care before a diagnostic procedure is arranged.
"""

from __future__ import annotations

import re

# Other parties and workflow sources -> neutral wording (order matters).
_ATTRIBUTION = (
    (re.compile(r'检测方(?![法案式面向])(?:的)?菌群(?:疾病)?(?:风险)?模型'), '菌群检测'),
    (re.compile(r'检测方(?![法案式面向])(?:的)?(?:风险)?模型'), '菌群检测'),
    (re.compile(r'检测方(?![法案式面向])(?:的)?'), ''),
    (re.compile(r'来源报告(?:提供的|给出的|中的?)?'), '检测给出的'),
    (re.compile(r'源模型'), '菌群检测'),
    (re.compile(r'(?:菌群|疾病|风险)模型(?:给出的|提示的|的)?'), '菌群检测'),
    (re.compile(r'模型(?:提示|给出|判定|认为)'), '检测提示'),
    (re.compile(r'检测关联提示'), '菌群关联'),
    (re.compile(r'中间结果(?:显示|写明|提示|如下)?\s*[：:]?\s*'), ''),
    (re.compile(r'拒检项目'), '本次未做的项目'),
    (re.compile(r'(?:被|都|均)?(?<![目指])标(?:注|记)?为'), '为'),
    (re.compile(r'给出的(?:是|为)'), '显示为'),
    (
        re.compile(
            r'(?:将|把)([^，。；,;]{1,20}?)(?:等)?列为(?:关联提示|关联项目|相关疾病|检出项|检出项目|阳性项|风险项)'
        ),
        r'显示与\1相关的菌群变化',
    ),
    (re.compile(r'(?:原|原始)报告(?:给出|提出|中给出)?的'), ''),
    (re.compile(r'(?:原|原始)报告(?:建议|提示)'), '建议'),
    (re.compile(r'按(?:原|原始)报告'), '按'),
    (re.compile(r'(?:原|原始)报告(?:中|里|上)?'), '检查报告'),
    (re.compile(r'主检(?:医生|医师)'), '医生'),
    (re.compile(r'主检(?:建议|意见)'), '医生建议'),
    (re.compile(r'主检(?:小结|结论|总结)'), '体检结论'),
    (re.compile(r'主检'), '体检'),
    (re.compile(r'(?:体检|检测|检验)(?:机构|中心|公司|方(?![法案式面向]))'), lambda match: match[0][:2]),
)
# Capture markers that are provenance, not results.
_MARKERS = (
    re.compile(r'[（(]\s*(?:原报告|原始报告)?\s*[)）]'),
    re.compile(
        r'[（(]\s*(?:均为|都是|为)?(?:用户|本人)?(?:自述|自报|报告)[，,、]?\s*(?:未经?(?:核验|核实|检查确认|证实))?\s*[)）]'
    ),
    re.compile(r'[：:，,；;]?\s*(?:均为|都是|为)?(?:用户|本人)?自述[，,]\s*未经?(?:核验|核实|检查确认|证实)'),
    re.compile(r'[：:，,；;]\s*未经?(?:核验|核实)(?=[；;。，,]|$)'),
    re.compile(
        r'[，,]?\s*(?:但|也)?(?:未|没有)(?:标|标注|给出|提供)(?:具体的?)?(?:风险)?(?:等级|分级|风险值|数值)(?=[，,。；;]|$)'
    ),
)
# A sentence that only disclaims (no number, short) carries nothing for the reader.
_DISCLAIMER = re.compile(
    r'未经?(?:核验|核实|检查确认|证实)|本人(?:报告|自报)|仅供参考|不能(?:替代|代替)(?:医生|诊断|面诊|就医)|'
    r'(?:不是|并非|不等于|不构成|不能当作|不能作为|不能视为)(?:临床)?(?:诊断|确诊|患病概率|病因|疾病)|'
    r'只是(?:统计)?关联|属于关联提示|仅为关联|'
    r'(?:资料|记录|报告)(?:中|里)?(?:没有|未)(?:给出|提供|说明)|可报告水平'
)
_SENTENCE = re.compile(r'[^。！？!?]*[。！？!?]|[^。！？!?]+$')
_ACUTE_DIGESTIVE_SIGN = re.compile(r'呕血|黑便|大量便血|消化道出血')
_DIRECT_URGENT_ENDOSCOPY = re.compile(
    r'(?:应|需|建议)?\s*(?:尽快|立即|及时|优先)(?:完成|安排|接受|进行|做|行)?\s*'
    r'(?:上消化道|消化道)?(?:胃镜|内镜|肠镜)(?:检查)?'
)
_DELAYED_BLEEDING_TRIAGE = re.compile(
    r'(?P<signs>呕血(?:或黑便)?|黑便)(?:伴|合并)(?:有)?(?:头晕(?:、)?乏力|头晕|晕厥)'
    r'(?:时)?(?:应|需)?(?:及时|立即|尽快)?(?:到)?急诊(?:就诊|评估)?'
)
_ACTION_HEADINGS = re.compile(
    r'(?m)^\s*(?:评估事项|完成时间|达标与就诊|达标标准|达标与转诊|目的与达标|事项|目的)[：:]\s*'
)
_UNIT_WORDS = (
    ('毫摩尔每升', 'mmol/L'),
    ('微摩尔每升', 'μmol/L'),
    ('毫单位每升', 'mU/L'),
    ('微单位每毫升', 'μIU/mL'),
    ('纳克每毫升', 'ng/mL'),
    ('皮克每毫升', 'pg/mL'),
    ('毫克每分升', 'mg/dL'),
    ('毫克每升', 'mg/L'),
    ('克每升', 'g/L'),
    ('单位每升', 'U/L'),
    ('毫米汞柱', 'mmHg'),
    ('平方厘米', 'cm²'),
    ('千卡', 'kcal'),
    ('厘米', 'cm'),
    ('毫米', 'mm'),
    ('公斤', 'kg'),
    ('千克', 'kg'),
)
# Laboratory abbreviations the prose should spell out (the glossary explains terms).
_ABBREVIATIONS = (
    ('HOMA-IR', '胰岛素抵抗指数'),
    ('HOMA-β', '胰岛β细胞功能指数'),
    ('non-HDL-C', '非高密度脂蛋白胆固醇'),
    ('LDL-C', '低密度脂蛋白胆固醇'),
    ('HDL-C', '高密度脂蛋白胆固醇'),
    ('HbA1c', '糖化血红蛋白'),
    ('hs-CRP', '超敏C反应蛋白'),
    ('FIB-4', '肝纤维化指数'),
    ('BMI', '体重指数'),
    ('TSH', '促甲状腺激素'),
    ('OGTT', '口服葡萄糖耐量试验'),
    ('GGT', 'γ-谷氨酰转移酶'),
    ('ALT', '丙氨酸氨基转移酶'),
    ('AST', '天门冬氨酸氨基转移酶'),
    ('CEA', '癌胚抗原'),
    ('AFP', '甲胎蛋白'),
    ('TG', '甘油三酯'),
    ('TC', '总胆固醇'),
)
# Stock connective and framing phrases. Removing them keeps the sentence intact.
_FILLER = (
    (re.compile(r'(?:值得注意的是|需要注意的是|需要说明的是|需要指出的是|值得一提的是|要说明的是)[，,：:]?'), ''),
    (re.compile(r'(?:换句话说|也就是说|总的来说|总体而言|综上所述|综合来看|简单来说|说白了)[，,：:]?'), ''),
)

# Formal reports are impersonal: the examinee is not addressed. Dropping the
# pronoun keeps a Chinese sentence intact in most positions; where a preposition
# or a copula needs an object or subject, "受检者" stands in.
_SECOND_PERSON = (
    (re.compile(r'(?:对|就)[您你](?:来说|而言)[，,]?'), ''),
    (re.compile(r'结合[您你]的(?:具体|实际|个人)?情况'), '结合本次评估结果'),
    (re.compile(r'[您你](?:本人)?的'), ''),
    (re.compile(r'(建议|请|提醒|需要|希望|如果|如|若|一旦|当|即使|虽然|因为|由于)[您你](?!们)'), r'\1'),
    (re.compile(r'(对|为|给|向|与|和|同|跟|让|使|供|帮助|帮|便于|告知|通知)[您你](?!们)'), r'\1受检者'),
    (re.compile(r'[您你]本人'), '受检者本人'),
    (re.compile(r'[您你](?=(?:是|为|属于|系|现年|今年|\d))'), '受检者'),
    (re.compile(r'您|你(?!们)'), ''),
)
# Lead-ins that narrate which input a fact came from. The fact stays.
_SOURCE_LEAD = (
    (
        re.compile(
            r'(^|[，。；：！？\n])\s*(?:根据|按照|结合)?问卷(?:结果|记录|内容)?(?:中|里)?(?:显示|提示|反映|填写|记录)?[，,：:]\s*'
        ),
        r'\1',
    ),
    (
        re.compile(
            r'(^|[，。；：！？\n])\s*(?:根据问卷|问卷(?:结果|记录)?(?:中|里)?(?:显示|提示|反映|记录)|问卷记录)(?:的是)?(?![的里中])'
        ),
        r'\1',
    ),
    (re.compile(r'(^|[，。；：！？\n])\s*(?:本次)?(?:体检|检查|化验)(?:结果)?(?:显示|发现)[，,：:]?\s*'), r'\1'),
    (
        re.compile(
            r'(^|[，。；：！？\n])\s*(?:本次|这次)?(?:肠道)?菌群检测(?:结果)?(?:显示|提示|发现|中)[，,：:]?\s*(?![与的])'
        ),
        r'\1肠道菌群方面，',
    ),
)

# Patterns a rewrite should remove; used as hints and in tests, not as gates.
AI_TELLS = {
    '不是…而是': re.compile(r'不是[^。；，]{1,30}[，,]\s*而是'),
    '破折号': re.compile(r'——|--'),
    '套话连接词': re.compile(
        r'值得注意|需要说明|需要指出|换句话说|也就是说|总的来说|综上|总体而言|首先[，,].{0,40}其次|关键在于'
    ),
    '空泛大词': re.compile(r'核心|主线|抓手|一条线|落地|闭环|赋能|至关重要|不容忽视|深入|全面提升'),
    '免责声明': _DISCLAIMER,
    '口语': re.compile(r'其实|说白了|就行了?|挺[^拔立]|咱们|[吧呢][，。！]'),
    '第二人称': re.compile(r'您|你|我们|咱们'),
    '来源引出语': re.compile(
        r'根据问卷|问卷(?:结果|记录)?(?:中|里)?(?:显示|提示|反映|填写|记录)|(?:体检|化验)(?:结果)?(?:显示|发现)|'
        r'菌群检测(?:结果)?(?:显示|提示|发现)|检测结果(?:显示|提示)'
    ),
    '流程用语': re.compile(
        r'标注为|标为|给出的(?:是|为)|中间结果|被列为|列为提示|七个维度|(?<![\u4e00-\u9fff])本类|资料(?:中|里)?(?:没有|未)(?:给出|提供)|问卷记录里|功能预测里'
    ),
    '来源方': re.compile(
        r'检测方(?![法案式面向])|原报告|主检|体检机构|检测机构|来源报告|(?:检测方|菌群|疾病|风险|源)模型'
    ),
}


# An item whose text opens with a second generic heading ("总体结论：整体判断：……").
_DOUBLE_TITLE = re.compile(
    r'(^|\n\s*\n)([^：:，。；！？\s]{2,14})：\s*(?:总体|整体|综合|总的)?(?:结论|判断|评价|评估|概述|小结)：\s*'
)


def ai_tells(text: str) -> list[str]:
    """Names of the stock patterns found in ``text`` (for rewrite hints)."""
    return [name for name, pattern in AI_TELLS.items() if pattern.search(str(text or ''))]


def reader_text(text, *, prose: bool = False):
    """Reader wording of one string; ``prose`` also drops pure disclaimer sentences."""
    if not isinstance(text, str) or not text:
        return text
    value = re.sub(r'(^|[；;\n])\s*主检(?:结论|建议|小结)[：:]\s*', r'\1', text)
    value = re.sub(r'\s*[（(]\s*计算值\s*[)）]\s*', ' ', value)
    for pattern in _MARKERS:
        value = pattern.sub('', value)
    for pattern, replacement in _ATTRIBUTION:
        value = pattern.sub(replacement, value)
    if prose:
        value = re.sub(r'[您你](?:本人)?(?:自述|自诉|自报|提到|填写|反映)(?:的)?', '', value)
        value = _ACTION_HEADINGS.sub('', value)
        value = _DOUBLE_TITLE.sub(r'\1\2：', value)
        for pattern, replacement in _SOURCE_LEAD:
            value = pattern.sub(replacement, value)
        for pattern, replacement in _SECOND_PERSON:
            value = pattern.sub(replacement, value)
        value = re.sub(r'肠道菌群方面，(?:肠道)?菌群(?=的|中)', '肠道菌群', value)
        # One unit convention: standard symbols after a number, as on lab sheets.
        for words, symbol in _UNIT_WORDS:
            value = re.sub(r'(\d)\s*' + words, r'\1 ' + symbol, value)
        # English abbreviations in brackets after a Chinese term ("总胆固醇(TC)").
        value = re.sub(r'(?<=[\u4e00-\u9fff])[（(][A-Za-z][A-Za-z0-9\-/·.\s]{0,12}[)）]', '', value)
        for short, name in _ABBREVIATIONS:
            value = re.sub(r'(?<![A-Za-z0-9/β\-])' + re.escape(short) + r'(?![A-Za-z0-9/β\-])', name, value)
            value = re.sub('(' + re.escape(name) + r')[（(]?\s*' + re.escape(name) + '[)）]?', r'\1', value)
        for pattern, replacement in _FILLER:
            value = pattern.sub(replacement, value)
        value = value.replace('——', '，').replace('--', '，')
        kept = []
        for sentence in _SENTENCE.findall(value):
            if _ACUTE_DIGESTIVE_SIGN.search(sentence) and re.search(r'(?:若|如|一旦|出现)', sentence):
                sentence = _DIRECT_URGENT_ENDOSCOPY.sub('应尽快就医，由接诊医生决定内镜时机', sentence)
                sentence = _DELAYED_BLEEDING_TRIAGE.sub(r'出现\g<signs>应尽快到急诊评估', sentence)
            if not sentence.strip() or not _DISCLAIMER.search(sentence):
                kept.append(sentence)
                continue
            # Keep every clause that says something; drop only the disclaiming ones.
            ending = sentence.rstrip()[-1] if sentence.rstrip()[-1:] in '。！？!?' else ''
            body = sentence.rstrip()[: -1 if ending else None]
            clauses = [part for part in re.split(r'[，,；;]', body) if part.strip()]
            useful, dropped, subject = [], False, ''
            for part in clauses:
                if _DISCLAIMER.search(part) and not re.search(r'\d', part):
                    dropped = True
                    lead = re.match(r'^(.*?)(?:不是|并非|不等于|不构成|只是)', part)
                    subject = lead[1].strip() if lead else ''
                    continue
                if dropped and part.lstrip().startswith('而是'):
                    # The other half of a dropped "不是…而是…" pair keeps its subject.
                    rest = part.lstrip()[2:]
                    dropped = False
                    if len(rest) <= 8:
                        continue
                    part = (subject + '是' + rest) if subject else rest
                dropped = False
                useful.append(part)
            if useful:
                kept.append('，'.join(useful) + (ending or '。'))
        value = ''.join(kept) if kept else value
        # One typographic convention: no spaces between Chinese and digits or Latin
        # text (the space between a number and its unit stays); squared units.
        value = re.sub(r'(?<=[\u4e00-\u9fff])[ \u00a0]+(?=[0-9A-Za-z<>≤≥±~～-])', '', value)
        value = re.sub(r'(?<=[0-9A-Za-z%²)）])[ \u00a0]+(?=[\u4e00-\u9fff])', '', value)
        value = re.sub(r'(?<![A-Za-z])(kg/m|cm|m)2(?![0-9A-Za-z])', r'\1²', value)
    value = re.sub(r'([，,；;：:])\s*\1+', r'\1', value)
    value = re.sub(r'[，,]\s*([。；;！？])', r'\1', value)
    value = re.sub(r'([（(])\s*[，,；;]\s*', r'\1', value)
    value = re.sub(r'^[，,；;：:\s]+', '', value)
    value = re.sub(r' +([，,。；;：:）)])', r'\1', re.sub(r' {2,}', ' ', value))
    return value.strip()


# Fields that are identifiers, provenance, enumerations or bibliography.
_SKIP_FIELDS = frozenset(
    {
        'code',
        'sources',
        'evidence_refs',
        'reference_literature',
        'source_items',
        'url',
        'doi',
        'scientific_name',
        'availability',
        'certainty',
        'category',
        'focus_label',
        # Catalog-locked sections: identities and wording are validated.
        'indicator_standards',
        'risk_index_standards',
        'matrix_levels',
        'reference_description',
        'unit',
        'reference_range',
    }
)
_PROSE_FIELDS = frozenset(
    {
        'text',
        'explanation',
        'summary',
        'description',
        'source_advice',
        'interpretation',
        'health_meaning',
        'clinical_correlation',
        'attention_basis',
        'followup_action',
        'resistance_notice',
        'association',
        'basis',
    }
)


def present_for_reader(data) -> int:
    """Apply reader wording to every displayed string of ``data`` in place.

    Returns the number of changed strings. Measured values, units and
    reference ranges are left exactly as recorded.
    """
    changed = 0
    seen = set()

    def visit(node, prose=False):
        nonlocal changed
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, str):
                    cleaned = reader_text(item, prose=prose)
                    if cleaned != item:
                        node[index] = cleaned
                        changed += 1
                else:
                    visit(item, prose)
            return
        fields = getattr(type(node), 'model_fields', None)
        if not fields:
            return
        for name in fields:
            if name in _SKIP_FIELDS:
                continue
            item = getattr(node, name, None)
            if isinstance(item, str):
                cleaned = reader_text(item, prose=name in _PROSE_FIELDS)
                if cleaned != item:
                    setattr(node, name, cleaned)
                    changed += 1
            elif item is not None:
                visit(item, prose=name in _PROSE_FIELDS)

    visit(data)
    return changed


# An itemised paragraph: a short noun heading, a full-width colon, the text.
ITEM_TITLE = re.compile(r'^(?P<title>[^：:，。；！？\s]{2,14})：(?P<text>\S.*)$', re.S)

_ENGLISH_CODE = re.compile(r'\b[a-z]+_[a-z_]+\b|\b(?:confirmed|suspected|partial|unavailable)\b')


def reader_quality(data) -> dict:
    """Reader-facing quality metrics of one report (for audit and monitoring).

    Counts what must never reach the reader (other parties, capture markers,
    English codes) and what makes text read as machine-written, plus prose
    density. It reads displayed strings only; identifiers and bibliography
    are excluded.
    """
    prose, labels = [], []

    def visit(node, field=''):
        if isinstance(node, list):
            for item in node:
                visit(item, field)
            return
        if isinstance(node, str):
            (prose if field in _PROSE_FIELDS else labels).append(node)
            return
        fields = getattr(type(node), 'model_fields', None)
        if not fields:
            return
        for name in fields:
            if name not in _SKIP_FIELDS or name == 'certainty':
                if name == 'certainty':
                    continue
                visit(getattr(node, name, None), name)

    visit(data)
    shown = prose + labels
    paragraphs = [part for text in prose for part in re.split(r'\n\s*\n', text) if part.strip()]
    lengths = sorted(len(part) for part in paragraphs)
    tells = {}
    for text in prose:
        for name in ai_tells(text):
            tells[name] = tells.get(name, 0) + 1
    return {
        'proseCharacters': sum(len(text) for text in prose),
        'paragraphs': len(paragraphs),
        'paragraphMedian': lengths[len(lengths) // 2] if lengths else 0,
        'paragraphMax': lengths[-1] if lengths else 0,
        'longParagraphs': sum(1 for length in lengths if length > 180),
        'titledParagraphs': sum(1 for part in paragraphs if ITEM_TITLE.match(part)),
        'secondPerson': sum(len(AI_TELLS['第二人称'].findall(text)) for text in shown),
        'sourceLeadIns': sum(len(AI_TELLS['来源引出语'].findall(text)) for text in prose),
        'otherPartyMentions': sum(len(AI_TELLS['来源方'].findall(text)) for text in shown),
        'captureMarkers': sum(len(re.findall(r'未经?核验|用户自述|本人报告', text)) for text in shown),
        'englishCodes': sum(len(_ENGLISH_CODE.findall(text)) for text in shown),
        'tellsByKind': tells,
    }


__all__ = ['AI_TELLS', 'ITEM_TITLE', 'ai_tells', 'present_for_reader', 'reader_quality', 'reader_text']
