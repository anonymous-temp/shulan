"""Evaluate-only distinctions between medical principles, source gaps and goals.

These are necessary checks, never medical-claim approval. Original author text,
patient/source quotations, procedure scope and both semantic reviews remain
independent. The shared clinical-report implementation is intentionally untouched.
"""

from __future__ import annotations

import re


# A negative quantifier about a method's diagnostic capability does not say
# that the patient did not undergo that examination. Match the capability
# predicate itself; words such as "provided", "results" or "performed" are
# deliberately not allowed between the modality and predicate.
_DIAGNOSTIC_CAPABILITY = re.compile(
    r"(?:没有|尚无|未见|不存在|无)(?:任何(?:一种)?|哪一种|一种|单一|现有|已知)?"
    r"(?:影像(?:学)?(?:检查|方法|手段|模态)?|CT(?:检查)?|MRI(?:检查)?|超声(?:检查)?)"
    r"(?:方法|手段|模态)?(?:能够|能|可以|可|足以)?"
    r"(?:明确(?:地)?|确定(?:地)?|无歧义地|独立|单独){0,3}(?:诊断|确诊|定性)|"
    r"\bno\s+(?:single\s+|available\s+|current\s+)?imaging\s+"
    r"(?:modalit(?:y|ies)|methods?|tests?|techniques?)\s+"
    r"(?:(?:can|could)\s+)?(?:unequivocally\s+)?(?:diagnose|confirm|establish)\b", re.I)
_PATIENT_CONTEXT = re.compile(r"本人|本例|患者|受检者|本次|资料|报告|记录|"
    r"\b(?:patient|subject|participant|provided|records?|reports?)\b", re.I)
_SOURCE_ABSENCE = re.compile(
    r"未提供|尚未提供|(?:没有|未见|尚无|未检测|未进行)[^。；\n]{0,40}(?:资料|记录|结果|检测|检查)|"
    r"缺少|缺乏[^。；\n]{0,24}(?:资料|结果|记录)|"
    r"(?:尚未|未曾|没有|未)(?:做|行|接受|完成|经过|经)[^，,。；;\n]{0,16}"
    r"(?:病理|组织学|免疫组化|活检|内镜|检查|检测|确诊)|"
    r"\b(?:not\s+(?:yet\s+)?(?:performed|provided|available|undergone)|"
    r"no\s+(?:available\s+)?(?:records?|reports?|results?))\b", re.I)

_GOAL = re.compile(
    r"旨在|目的|目标|以期|为了|致力于|力图|希望|"
    # Purpose '以降低' is distinct from ability/effect '可以/得以降低'.
    r"(?<![可得予难])以(?:便)?(?=降低|减少|预防|改善|减轻)|"
    r"\bpurpose\b|\baim(?:s|ed)?\b|\b(?:with\s+(?:the\s+)?)?(?:goal|objective)\b|"
    r"\bin order to\b|\b(?:intended|designed|developed|created) to\b", re.I)
_OUTCOME = re.compile(r"风险|复发|症状|发生率|患病率|结局|risk|recurren|symptom|incidence|outcome", re.I)
_RECURRENCE = re.compile(r"复发|recurren", re.I)
_BENEFIT = re.compile(
    r"(?:有助于|可以|能够|可|能|有效|得以)?(?:降低|减少|预防|改善|减轻)|"
    r"\b(?:reduces?|reduced|decreases?|decreased|lowers?|lowered|improves?|improved|"
    r"prevents?|prevented|reduction|improvement)\b", re.I)
_NEGATIVE_EFFECT = re.compile(
    r"(?:未|不|没有|无|难以)[^，,。；;]{0,16}(?:降低|减少|预防|改善|减轻)|"
    r"\b(?:no|not|never|without|failed to|unable to)\b[^,;.!?]{0,40}"
    r"\b(?:reduce|reduced|reduction|decrease|decreased|lower|lowered|improve|improved|"
    r"improvement|prevent|prevented)\b", re.I)


def source_absence_audit_text(text: str) -> str:
    """Return an internal absence-check view, never replacement report prose.

    A recognized general capability clause is masked only for the absence
    audit. Patient-specific histories, missing results and neighbouring source
    gaps stay visible. The full, unchanged claim still requires a supporting
    original and the usual diagnostic-action/procedure/semantic checks.
    """
    if not isinstance(text, str):
        return ""

    def replace(match: re.Match) -> str:
        prefix = re.split(r"[，,。；;！？.!?\n]", text[:match.start()])[-1]
        return match.group() if _PATIENT_CONTEXT.search(prefix) else "诊断能力原则"

    return _DIAGNOSTIC_CAPABILITY.sub(replace, text)


def needs_source_absence_check(text: str) -> bool:
    """Classify a source/history gap without confusing diagnostic limitations."""
    return bool(_SOURCE_ABSENCE.search(source_absence_audit_text(text)))


def _clauses(text: str) -> list[str]:
    return re.split(r"[，,。；;！？.!?\n]|但是|然而|但", text)


def clinical_goal_effect_failures(text: str, source_quotes: list[str]) -> list[str]:
    """Do not turn a guideline's purpose into a demonstrated clinical benefit.

    Faithfully stated purposes ('以降低…') remain purposes. An added effect
    ('有助于降低…') requires a separate effect statement from this same source.
    Scope, causal strength, intervention and individual applicability still
    need independent review, even when this necessary check returns no errors.
    """
    quotes = [quote for quote in source_quotes if isinstance(quote, str)]
    claims = [clause for clause in _clauses(text) if _OUTCOME.search(clause)
        and _BENEFIT.search(clause) and not _GOAL.search(clause)
        and not _NEGATIVE_EFFECT.search(clause)]
    if not claims or not any(_GOAL.search(quote) for quote in quotes):
        return []
    for claim in claims:
        supported = False
        for quote in quotes:
            # Keep whole source sentences: a purpose prefix before a comma
            # still governs its subordinate outcome after that comma.
            for sentence in re.split(r"[。！？.!?;；\n]", quote):
                if _GOAL.search(sentence) or _NEGATIVE_EFFECT.search(sentence):
                    continue
                if (_OUTCOME.search(sentence) and _BENEFIT.search(sentence)
                        and (not _RECURRENCE.search(claim) or _RECURRENCE.search(sentence))):
                    supported = True
                    break
            if supported:
                break
        if not supported:
            return ["所选原文仅将降低风险、减少复发等写为指南目的或管理目标，缺少本句实际声称的获益原句；"
                "可保留‘以降低／旨在降低’的原目标，不可改成‘有助于降低／能够减少’等效果结论。"]
    return []


_ORIGINAL_ANCHOR = r'(?:所选(?:原文|引文|文字|摘要)|原文|论文|文献|摘要|引文|证据|该篇|本篇|Q\d+)'
_UNSUPPORTED_ADMISSION = re.compile(
    _ORIGINAL_ANCHOR + r'[^。；;\n]{0,55}(?:未|没有|并未|并没有|不曾|不包含|不涉及)'
    r'(?:直接|明确|完整|独立|逐字|充分)?(?:明示|写出|写明|提出|提及|提到|陈述|表述|说明|描述|涉及|涵盖|支持|证明|验证|论证|给出|提供|包含|出现(?![^。；;\n]{0,10}(?:相反|冲突|矛盾|相悖))|阐述|报告|讨论)|'
    + _ORIGINAL_ANCHOR + r'[^。；;\n]{0,18}未(?:把|将)[^。；;\n]{0,40}(?:定义句|定义|描述)[^。；;\n]{0,12}(?:写出|写明|表述)|'
    r'\b(?:source|original|selected|quoted|abstract|paper|text)\b[^.;\n]{0,70}'
    r'\b(?:does\s+not|did\s+not|has\s+not|not\s+(?:explicitly|directly))\b[^.;\n]{0,25}'
    r'\b(?:state|mention|define|describe|support|address|provide|contain|establish|validate)\b|'
    r'\bnot\s+(?:explicitly|directly)\s+(?:stated|mentioned|defined|described|supported|provided)\b', re.I)
_COMMON_KNOWLEDGE = re.compile(
    r'(?:通行|公认|一般|基本|基础|标准)[\u3400-\u9fff]{0,12}?(?:定义|知识|常识|病理|医学背景|描述|背景(?:表述|说明|描述)?)|'
    r'医学常识|常识性(?:定义|事实|描述|表述)|常识(?:性)?(?:补充|补写|补入|推断)|'
    r'\b(?:common|standard|established|accepted|general)\s+(?:medical\s+)?'
    r'(?:knowledge|definition|background|description)\b', re.I)
_NONCONTRADICTION = re.compile(
    r'(?:未|没有|并无|不)(?:出现)?[^。；;\n]{0,18}(?:冲突|矛盾|相反(?:表述|内容|证据|说法|结论))|'
    r'不存在[^。；;\n]{0,8}(?:冲突|矛盾)|'
    r'(?:未见|没有|无|缺乏)[^。；;\n]{0,6}反证|'
    r'\b(?:not\s+(?:in\s+)?(?:conflict|contradict\w*)|does\s+not\s+contradict|'
    r'no\s+(?:conflict|contradiction|contrary\s+(?:statement|evidence)))\b', re.I)
_SOURCE_WAIVER = re.compile(
    r'否定性(?:表述|陈述)|(?:属于|作为)[^。；;\n]{0,16}(?:背景|常识)[^。；;\n]{0,16}(?:无需|不需|不必)|'
    r'(?:无需|不需|不必)[^。；;\n]{0,14}(?:原文|引文|文献|证据)[^。；;\n]{0,14}(?:支持|依据|证明)|'
    r'\bnegative\s+(?:statement|assertion)\b|\b(?:need\s+not|does\s+not\s+need)\b[^.;\n]{0,25}'
    r'\b(?:source|citation|support)\b', re.I)
_DIRECT_DEFINITION_SUPPORT = re.compile(
    _ORIGINAL_ANCHOR + r'[^。；;\n]{0,18}(?:明确|直接|逐字)(?:给出|提供|写出|写明|陈述|说明|表述|界定|定义)'
    r'[^。；;\n]{0,18}(?:定义|含义|描述|为)|'
    + _ORIGINAL_ANCHOR + r'[^。；;\n]{0,18}明示[^。；;\n]{0,18}(?:定义|含义|描述)|'
    r'\b(?:source|original|selected|quoted|abstract|text)\b[^.;\n]{0,30}'
    r'\b(?:explicitly|directly)\s+(?:defines?|states?\s+the\s+definition)\b', re.I)
_PERSONAL_MEASUREMENT_GAP = re.compile(
    _ORIGINAL_ANCHOR + r'[^。；;\n]{0,15}(?:未|没有|并未|不)(?:直接|明确)?'
    r'(?:明示|提供|包含|出现|验证|评估|报告)[^。；;\n]{0,12}'
    r'(?:本人|本例|个人|本次|本报告|来源|供应商)[^。；;\n]{0,12}'
    r'(?:分值|分数|数值|阈值|等级|分级|概率|评分|测量|数据)', re.I)
_PERSONAL_VALIDATION_CLAIM = re.compile(
    r'(?:验证|证实|证明|确认)[^。；;\n]{0,24}(?:本人|本次|患者|来源|供应商)'
    r'[^。；;\n]{0,12}(?:分值|分数|阈值|分级|概率|评分)|'
    r'(?:本人|本次|来源|供应商)[^。；;\n]{0,16}(?:分值|分数|阈值|分级|概率|评分)'
    r'[^。；;\n]{0,10}(?:已获|得到|获得)(?:验证|证实)', re.I)
_CLAIM_RETAINS_NEGATIVE_SCOPE = re.compile(
    r'(?:本句|正文|该句|本表述)[^。；;\n]{0,10}(?:没有|并未|未|不)'
    r'(?:声称|主张|新增|宣称|写成|推断)[^。；;\n]{0,25}，?'
    r'[^。；;\n]{0,10}(?:仅|只)', re.I)


def _common_knowledge_waiver(reason: str) -> bool:
    for match in _COMMON_KNOWLEDGE.finditer(reason):
        before = re.split(r'[，,。；;\n]', reason[:match.start()])[-1][-30:]
        if (re.search(r'(?:不|未|无|不能|不得|没有)[^，,。；;\n]{0,8}$', before)
                or re.search(r'\b(?:not|without|never)\s+'
                    r'(?:(?:need|use|using|relying\s+on)\s+)?$', before, re.I)):
            continue
        return True
    return False


def unsupported_review_rationale_failures(review: dict | str, *, claim: str = '') -> list[str]:
    """Reject an approval that excuses a conceded original-support gap.

    A truthful general definition need not establish that the patient has the
    disease. That valid population distinction is not a waiver of original
    support. Likewise, a paper need not contain a person's numeric score when
    the claim explains a general function rather than validating that score.
    This check never approves a claim and does not infer source semantics from
    keywords; both original-text reviews remain necessary.
    """
    if isinstance(review, dict):
        if review.get('valid') is not True:
            return []
        reason = review.get('reason')
    else:
        reason = review
    if not isinstance(reason, str) or not reason.strip():
        return []
    if not _PERSONAL_VALIDATION_CLAIM.search(claim or ''):
        reason = _PERSONAL_MEASUREMENT_GAP.sub('', reason)
    common = _common_knowledge_waiver(reason)
    conflict = bool(_NONCONTRADICTION.search(reason))
    waiver = common or conflict or bool(_SOURCE_WAIVER.search(reason))
    admitted = bool(_UNSUPPORTED_ADMISSION.search(reason))
    # Faithfully reporting an actual negative finding, or explicitly not
    # making an unsupported causal/benefit claim, is not a source-gap waiver.
    faithful_scope = bool(_CLAIM_RETAINS_NEGATIVE_SCOPE.search(reason)) and not common
    if waiver and admitted and not faithful_scope:
        return ['审核理由承认所选原文未写出或未支持断言，却以通行定义、常识、否定表述或未与原文冲突豁免；'
            '每条保留的医学断言仍须有实际原文支持，不能把不矛盾等同于有支持。']
    # Some approvals avoid saying "missing" and offer only common knowledge
    # plus absence of contradiction for the definition half of a mixed claim.
    # Evaluate the purported exemption locally. A genuine Q-supported
    # definition elsewhere cannot validate another unsupported premise.
    parts = re.split(r'[。；;\n]', reason)
    definition_subject = re.match(r'^([^，,。；;]{1,45}?)(?:是|属于|为)', claim or '')
    linked_definition = bool(_DIRECT_DEFINITION_SUPPORT.search(reason)) if not claim else bool(
        definition_subject and any(_DIRECT_DEFINITION_SUPPORT.search(part)
            and definition_subject[1] in part for part in parts))
    local_exemption = any(_COMMON_KNOWLEDGE.search(part) and _NONCONTRADICTION.search(part)
        and not _DIRECT_DEFINITION_SUPPORT.search(part)
        and not linked_definition for part in parts)
    if local_exemption or common and conflict and not _DIRECT_DEFINITION_SUPPORT.search(reason):
        return ['审核理由只以通行定义或一般背景及未与原文冲突替代该断言的正面原文依据；'
            '应分别核对定义与研究结论，不能让后半句的证据代替前半句。']
    return []


_FUNCTION_INTERVENTION = re.compile(
    r'(?:可|能|可以|能够)?(?:被|用)?作为[^。；;\n]{0,60}'
    r'(?:策略|治疗(?:途径|方案|方向)?|干预(?:方向|途径|方案)?)|'
    r'(?:建议|应当|应|需要|需)[^。；;\n]{0,30}'
    r'(?:补充|服用|给予|增加摄入|提高[^。；;\n]{0,12}菌|增加[^。；;\n]{0,12}菌)|'
    r'\b(?:can|may|could)\s+be\s+used\b[^.;\n]{0,70}'
    r'\b(?:strategy|therapeutic|intervention)\b', re.I)
_INDUSTRIAL_DOMAIN = re.compile(
    r'工业|制造业|工业制造|农业|制药|化妆品|商业|市场|产业|行业|'
    r'\b(?:industr\w*|manufactur\w*|agricultur\w*|pharmaceutical\w*|'
    r'cosmetic\w*|commercial\w*|markets?)\b', re.I)
_INDUSTRIAL_PURPOSE = re.compile(
    r'领域[^。；;\n]{0,22}(?:作用|重要|应用)|'
    r'(?:发挥|具有)[^。；;\n]{0,18}(?:应用价值|商业价值|产业价值|经济价值)|'
    r'(?:市场|行业|产业)[^。；;\n]{0,12}(?:需求|前景|潜力|价值)|'
    r'(?:有|具有|存在|受到)[^。；;\n]{0,12}(?:需求|商业关注)|'
    r'广泛应用|工业应用|商业应用|产业应用|工业用途|商业用途|'
    r'(?:应用于|用于)[^。；;\n]{0,35}(?:工业|制造业|农业|制药|化妆品|商业|行业)|'
    r'(?:用作|作为)[^。；;\n]{0,25}(?:工业|产业|行业)[^。；;\n]{0,15}(?:原料|产品|材料)|'
    r'\b(?:demand\w*|commercialization|market\s+(?:value|potential|demand))\b|'
    r'\b(?:industr\w*|commercial)\s+(?:applications?|uses?|usage)\b|'
    r'\b(?:roles?|importance)\b[^.;\n]{0,35}\b(?:across|in)\b'
    r'[^.;\n]{0,35}\bfields?\b', re.I)
_REPORT_ESTIMATE = re.compile(
    r'(?:预测|检测|模型|估计|推断|评分)[^。；;\n]{0,30}'
    r'(?:能力|结果|指标|分值|分数|等级|分级|功能)|'
    r'\b(?:predict\w*|model|estimated|measured)\b[^.;\n]{0,70}'
    r'\b(?:abilit\w*|results?|scores?|functions?|biosynth\w*)\b', re.I)
_HUMAN_OUTCOME_STATE = re.compile(
    r'(?:人体|本人|个人|患者|临床)[^。；;\n]{0,18}'
    r'(?:营养状态|疾病状态|营养状况|健康状态|实际含量|缺乏|疾病诊断|患病)|'
    r'\b(?:human|patient|clinical|individual)\b[^.;\n]{0,45}'
    r'\b(?:nutri\w*\s+status|disease\s+(?:status|state)|deficien\w*|diagnos\w*)\b', re.I)
_OUTPUT_BOUNDARY_COMPARISON = re.compile(
    r'(?:并非|不是|不属)[^。；;\n]{0,12}同一(?:层面|层级|概念)|'
    r'不等同|不代表|不能[^。；;\n]{0,16}(?:推导|推断|等同|证明|代表)|'
    r'\b(?:not\s+(?:equivalent|the\s+same)|cannot\s+(?:infer|imply|establish|prove))\b', re.I)
_EXTREME_AGE_COHORT = re.compile(
    r'(?:半|超|极)?百岁|超高龄|(?:99|1\d{2})(?:\s*[-~–至到]\s*\d{2,3})?\s*岁|'
    r'\b(?:(?:semi(?:super)?|super)?centenarians?)\b', re.I)
_METABOLIC_DOMAIN = re.compile(
    r'代谢|通路|途径|宏基因组|菌群|微生物|'
    r'\b(?:metabol\w*|pathways?|metagenom\w*|microbiom\w*)\b', re.I)
_AGE_RELATED_FUNCTION = re.compile(
    r'年龄|增龄|衰老|老龄|寿命|\b(?:ag(?:e|ed|ing|eing)|age.related|longev\w*)\b', re.I)
_METABOLIC_REARRANGEMENT = re.compile(
    r'重排|重新(?:调整|排列)|重组|\b(?:rearrang\w*|remodel\w*)\b', re.I)
_TREATMENT_CONTEXT = re.compile(
    r'长期[^。；;\n]{0,14}治疗|维持治疗|停药|减药|抗精神病药|'
    r'\b(?:maintenance|long.term|discontinuation|antipsychotic)\b', re.I)
_TRIAL_METHODOLOGY = re.compile(
    r'(?:临床试验|试验|研究)[^。；;\n]{0,35}(?:定义|标准|一致|统一)|'
    r'(?:定义|标准)[^。；;\n]{0,25}(?:临床试验|试验)|'
    r'\b(?:trials?|studies)\b[^.;\n]{0,65}\b(?:definitions?|criteria|consisten\w*)\b|'
    r'\bdefinitions?\b[^.;\n]{0,65}\btrials?\b', re.I)
_DISEASE_MEANING = re.compile(
    r'(?:是|属于|以)[^。；;\n]{0,35}(?:疾病|症状|障碍|表现|特征)|'
    r'(?:病程|疾病)[^。；;\n]{0,15}(?:反复|复发|缓解)|'
    r'\b(?:characteri[sz]ed by|disease course|clinical features?)\b', re.I)
_NARROW_ETIOLOGY = re.compile(
    r'罕见|罕以|极罕|很少(?:以|表现为)|极少(?:以|表现为)|继发|'
    r'\b(?:rare\w*|secondar\w*)\b', re.I)


def _scope_name(text):
    return re.sub(r'[\s，,。；;（）()]', '', text).casefold()


def _literal_name_in(name, text):
    """Match supplied names, without guessing cross-language equivalence."""
    if re.fullmatch(r'[A-Za-z][A-Za-z\s\-\x27]*', name.strip()):
        return bool(re.search(r'(?<![A-Za-z])' + re.escape(name.strip())
            + r'(?![A-Za-z])', text, re.I))
    return _scope_name(name) in _scope_name(text)


def _first_literal_definition_subject(text):
    """Read the first explicit subject-is/are definition from the original.

    This is a necessary source-focus check, not inferred disease semantics.
    Pronouns and method statements do not supply a named disease subject.
    """
    for sentence in re.split(r'[。；;.!?\n]', text):
        match = re.match(r'^\s*([A-Za-z][A-Za-z\s()\-\x27]{1,110}?)\s+'
            r'(?:is|are)\s+(.+)', sentence, re.I)
        if match and match[1].strip().casefold() not in {
                'it', 'they', 'this', 'these', 'this study', 'this review',
                'the study', 'the review', 'the diagnosis'}:
            return match[1].strip()
    return ''


def _scope_cause_established(cause, quotes, established_conditions):
    return any(_literal_name_in(cause, value)
        for value in established_conditions if isinstance(value, str)) and (
            _literal_name_in(cause, quotes))


def _disease_subject_scope_failures(claim, target_names, source_quotes, established_conditions):
    """A rare secondary cause is not the generic definition of another item.

    The caller supplies actual target names and only positively established
    clinical condition names/values. This guard does not infer a diagnosis,
    translate disease aliases or treat a source-model association as clinical.
    """
    quotes = '\n'.join(q for q in source_quotes if isinstance(q, str))
    if not _NARROW_ETIOLOGY.search(claim + '\n' + quotes):
        return []
    targets = [name for name in target_names if isinstance(name, str) and name.strip()]
    if not targets:
        return []
    failure = ['罕见或继发病因及其机制不能代替本项目的一般医学含义；'
        '本句另一个疾病主语须同时有已核实的肯定临床来源及所选原文支持，'
        '不能因研究病例真实、注明罕见或未直接诊断本人而免除这个前提。']

    # A known original-language alias lets us compare the paper's actual
    # first disease-definition subject. A rare-cause paper cannot supply
    # generic target meaning merely by hiding its cause in a later sentence.
    # Without a supplied English alias, no Chinese/English translation is
    # inferred. Actual positive clinical exceptions must use literal names.
    english_targets = [name for name in targets if re.fullmatch(
        r'[A-Za-z][A-Za-z\s\-\x27]*', name.strip())]
    original_subject = _first_literal_definition_subject(quotes)
    if english_targets and original_subject and not any(
            _literal_name_in(name, original_subject) for name in english_targets):
        # Parenthetical original abbreviations are retained separately, so
        # either the literal full name or its explicit abbreviation can
        # establish the same already-recorded clinical condition.
        full_name = re.sub(r'\s*\([^)]*\)', '', original_subject).strip()
        names = [original_subject, full_name, *re.findall(r'\(([^)]+)\)', original_subject)]
        if not any(_scope_cause_established(name, quotes, established_conditions)
                for name in names if name):
            return failure

    # Independently reject an explicitly named foreign disease in the
    # authored claim, including when the target word is entirely absent.
    # Pronominal follow-up sentences remain governed by the source focus
    # check above rather than a guessed subject.
    definition = re.match(r'^(?:临床上[，,]?)?(.{2,40}?)(?:是|属于|为|很少以|罕以|'
        r'很少表现为|极少以|极少表现为)', claim)
    cause = definition[1].strip() if definition else ''
    if cause and cause not in {'该病', '本病', '这种疾病', '这一疾病', '其病程'} and not any(
            _literal_name_in(name, cause) for name in targets):
        # Do not use an unrelated Chinese phrase to infer an English alias.
        # Literal Chinese target names do permit this subject comparison.
        if any(re.search(r'[\u3400-\u9fff]', name) for name in targets) and not (
                _scope_cause_established(cause, quotes, established_conditions)):
            return failure
    for target in targets:
        position = claim.find(target)
        if position <= 0:
            continue  # No guessed synonym or inferred sentence subject.
        prefix = claim[:position].strip()
        secondary = re.fullmatch(r'(.{2,40}?)(?:所致的?|导致的|引起的|继发的|继发性)', prefix)
        definition = re.match(r'^(.{2,40}?)(?:是|属于|为)', claim)
        cause = secondary.group(1).strip() if secondary else (
            definition.group(1).strip() if definition and definition.end() <= position else '')
        if not cause or any(_literal_name_in(name, cause) for name in targets):
            continue
        # Both sides must explicitly establish this same cause. A true
        # patient's other diagnosis, a negative/suspected condition or a
        # paper's rare subgroup alone cannot establish the missing premise.
        if not _scope_cause_established(cause, quotes, established_conditions):
            return failure
    return []


def evidence_role_boundary_failures(claim: str, role: str, *,
        target_names: tuple[str, ...] | list[str] = (),
        source_quotes: tuple[str, ...] | list[str] = (),
        established_condition_names: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Reject misplaced treatment/methods content, never approve its science."""
    if not isinstance(claim, str):
        return []
    if role == 'general_function' and _FUNCTION_INTERVENTION.search(claim):
        return ['一般功能意义不得采用增加菌水平等治疗或干预策略方向；'
            '本句应整句拒绝，不可删去研究限制后保留更强结论。']
    if (role == 'general_function' and _REPORT_ESTIMATE.search(claim)
            and _HUMAN_OUTCOME_STATE.search(claim) and _OUTPUT_BOUNDARY_COMPARISON.search(claim)):
        return ['预测、检测或模型能力与人体营养或疾病状态不能等同等输出边界声明，'
            '不能充当一般功能健康意义；本句完整不采用，不能只删限定词保留更强结论。'
            '应另选真实生物机制或功能原句；实际研究的人群、标本和实验范围限制仍须如实保留。']
    if (role == 'general_function' and _EXTREME_AGE_COHORT.search('\n'.join(
            quote for quote in source_quotes if isinstance(quote, str)))
            and _METABOLIC_DOMAIN.search(claim)
            and (_AGE_RELATED_FUNCTION.search(claim) or _METABOLIC_REARRANGEMENT.search(claim))
            and not _EXTREME_AGE_COHORT.search(claim)):
        return ['特殊年龄人群研究中的年龄相关代谢重排或调整，须保留实际研究人群和研究语境；'
            '不能省略百岁等人群后，改写为一般菌群代谢规律或本人饮食、年龄带来的功能变化。'
            '同篇与年龄无关的基础生化事实仍按实际原文独立审核，不因此禁用整篇来源。']
    if role == 'general_function' and any(
            _INDUSTRIAL_DOMAIN.search(sentence) and _INDUSTRIAL_PURPOSE.search(sentence)
            for sentence in re.split(r'[。；;.!?\n]', claim)):
        return ['工业、商业、农业或制药制造用途、行业市场需求不能充当一般功能健康意义；'
            '本句应整句不采用。应另选原文实际支持的生物合成、代谢过程、生物学职责或健康意义，'
            '不因同篇论文含有工业应用而拒绝其中独立可用的生物功能原句。']
    if (role == 'disease_meaning' and _TREATMENT_CONTEXT.search(claim)
            and (_TRIAL_METHODOLOGY.search(claim) or _GOAL.search(claim))
            and not _DISEASE_MEANING.search(claim)):
        return ['本句仅讨论长期治疗目标、维持或停药试验的复发定义等方法学，'
            '不能充当疾病定义、典型表现、病程或健康关联的医学含义。']
    if role == 'disease_meaning':
        return _disease_subject_scope_failures(claim, target_names, source_quotes,
            established_condition_names)
    return []


_IMMUNE_VULNERABILITY = re.compile(
    r'免疫(?:力|功能|系统)?(?:低下|缺陷|不全|受损|抑制)|'
    r'\b(?:immunocompromis\w*|immunosuppress\w*|immunodeficien\w*)\b|'
    r'\b(?:compromised|impaired|deficient|suppressed|weakened)\s+immune\s+(?:function|systems?)\b|'
    r'\bimmune\s+(?:deficien\w*|dysfunction|suppression)\b', re.I)
_POPULATION = re.compile(
    r'人群|成人|成年|婴儿|新生儿|患者|病人|'
    r'\b(?:populations?|patients?|adults?|neonat\w*|infants?)\b', re.I)
_DISEASE_EFFECT = re.compile(
    r'(?:更|较)(?:易|容易|常|多)[^。；;\n]{0,8}(?:导致|引起|引发|发生|造成|患|罹患)|'
    r'(?:更|较)(?:易|容易|常|多)[^。；;\n]{0,22}(?:致病|疾病|感染|侵袭)|'
    r'(?:致病|疾病|感染|侵袭)[^。；;\n]{0,15}(?:更强|更高|较高)|'
    r'\b(?:more\s+(?:commonly|frequently|often)|more\s+likely\s+to)\s+'
    r'(?:caus\w*|infect\w*|lead\w*|result\w*)\b|'
    r'\b(?:higher|increased)\s+(?:disease|infection|invas\w*)\s+risk\b', re.I)
_NAMED_SUBTYPE = re.compile(
    r'(?<![A-Za-z0-9])(?P<code>[A-Z]{1,8}\d{1,6}[A-Za-z0-9_-]*)(?![A-Za-z0-9])')
_ADULT = re.compile(r'成年人?|成人|\badults?\b', re.I)
_COMORBID = re.compile(
    r'(?<!无)(?<!未)(?<!没有)(?<!不)(?<!并非)'
    r'(?:基础(?:疾)?病|合并症|共病|伴随疾病|合并(?:其他|其它)疾病)|'
    r'\bco[- ]?morbid\w*\b', re.I)


def _affirmative_immune_vulnerability(source: str) -> bool:
    for match in _IMMUNE_VULNERABILITY.finditer(source):
        before = re.split(r'[，,。；;.!?\n]', source[:match.start()])[-1][-65:]
        after = source[match.end():match.end() + 65]
        if (re.search(r'(?:并非|不是|未见|没有|排除|未纳入|无|非)[^，,。；;\n]{0,12}$', before)
                or re.search(r'\b(?:not|without|no|non)[ -]*(?:severely\s+)?$', before, re.I)
                or re.match(r'(?:\s+(?:patients?|subjects?|participants?))?\s+'
                    r'(?:were|are|was)\s+excluded\b', after, re.I)
                or re.match(r'(?:患者|人群|者)?(?:均被|被|均)?(?:排除|未纳入)', after)):
            continue
        return True
    return False


def _subtype_outcome_scopes(source: str) -> list[tuple[str, str]]:
    """Read literal named subtype predicates, without guessing species/aliases."""
    result = []
    for sentence in re.split(r'[。；;!?\n]|\.(?=\s+[A-Z]|$)', source):
        matches = list(_NAMED_SUBTYPE.finditer(sentence))
        for index, match in enumerate(matches):
            # A later named subtype starts its own predicate; it is not an
            # extra restriction on a preceding independent conclusion.
            end = matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
            predicate = sentence[match.end():end]
            if _DISEASE_EFFECT.search(predicate):
                result.append((match['code'], predicate))
    return result


def _subtype_outcome_distribution_failures(claim, scopes):
    """Disease occurrence is not the distribution of its named lineage."""
    distribution = re.compile(r'(?:更|较)?多见(?:于)?|更常见(?:于)?|常见于|更易发现于|'
        r'\b(?:more\s+(?:commonly|frequently|often)\s+(?:seen|found)|'
        r'more\s+(?:common|frequent|prevalent)\s+in)\b', re.I)
    outcome = re.compile(r'侵袭性?(?:疾病|感染)|肺炎|败血症|脑膜炎|感染性疾病|'
        r'\b(?:diseases?|infections?|pneumonia|sepsis|meningitis)\b', re.I)
    matches = list(_NAMED_SUBTYPE.finditer(claim))
    for code, original_predicate in scopes:
        if not outcome.search(original_predicate):
            continue
        for index, match in enumerate(matches):
            if match['code'].casefold() != code.casefold():
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(claim)
            predicate = claim[match.end():end]
            if distribution.search(predicate) and _POPULATION.search(predicate) and not outcome.search(predicate):
                return ['原文描述特定谱系更常引起的疾病或感染结局，不能省略该结局后写成谱系在人群中更多见；'
                    '须由作者完整保留真实疾病结局及相同人群条件，或另选独立同对象作用，重新接受原文双审。']
    return []


def original_population_scope_failures(claim: str,
        source_quotes: tuple[str, ...] | list[str]) -> list[str]:
    """Necessary original population/subtype boundaries, never claim approval.

    Comorbidity does not establish immunodeficiency. A selected named
    subtype's outcome in adults with comorbidities also cannot be widened
    to unspecified strains or all patients. Independent general statements
    about colonization or differences between strains remain available.
    """
    if not isinstance(claim, str):
        return []
    source = '\n'.join(q for q in source_quotes if isinstance(q, str))
    failures = []
    if _affirmative_immune_vulnerability(claim) and not _affirmative_immune_vulnerability(source):
        failures.append('本句新增免疫低下、免疫抑制或免疫缺陷人群，但所选原文未明确支持该人群；'
            '有合并症、基础疾病或机会致病属性不能替代免疫状态依据，须保留原文真实人群。')
    scopes = _subtype_outcome_scopes(source)
    failures.extend(_subtype_outcome_distribution_failures(claim, scopes))
    if not (_POPULATION.search(claim) and _DISEASE_EFFECT.search(claim)):
        return failures
    if not scopes:
        return failures
    named = [(code, predicate) for code, predicate in scopes if re.search(
        r'(?<![A-Za-z0-9])' + re.escape(code) + r'(?![A-Za-z0-9])', claim, re.I)]
    if not named:
        failures.append('所选疾病结局明确限于原文命名的亚型或谱系，正文须保留该具体亚型或谱系；'
            '不能把其特定人群结局扩成未指定的部分菌株或一般人群。')
        return failures
    for _, predicate in named:
        # An earlier neonatal comparison does not make it an infant effect:
        # retain the population of the actual more-common-disease predicate.
        effect = _DISEASE_EFFECT.search(predicate)
        outcome = predicate[effect.start():] if effect else predicate
        leading_scope = re.split(r'[，,。；;\n]|\b(?:but|while|whereas)\b',
            predicate[:effect.start()] if effect else '', flags=re.I)[-1]
        if re.search(r'成人|成年|基础(?:疾)?病|合并症|共病', leading_scope):
            outcome = leading_scope + outcome
        if _ADULT.search(outcome) and not _ADULT.search(claim):
            failures.append('所选具体疾病结局限于成年人，正文不能省略年龄范围后扩大为一般人群。')
        if _COMORBID.search(outcome) and not _COMORBID.search(claim):
            failures.append('所选具体疾病结局限于合并基础疾病人群，正文须保留该实际条件。')
    return list(dict.fromkeys(failures))


_BARRIER_TISSUE = re.compile(
    r'肠(?:道)?(?:黏膜|粘膜|上皮|屏障|通透性)|黏膜|粘膜|上皮(?:细胞)?|紧密连接|'
    r'\b(?:epitheli\w*|mucos\w*|mucin)\b|'
    r'\b(?:gut|intestinal|colonic)\s+(?:barrier|integrity|permeability)\b|'
    r'\btight[ -]junctions?\b', re.I)
_BARRIER_PROCESS = re.compile(
    r'维持|保护|破坏|损伤|影响|改变|调节|侵入|侵袭|黏附|粘附|穿越|渗漏|'
    r'增加|降低|增强|削弱|分解|降解|'
    r'\b(?:maintain\w*|protect\w*|disrupt\w*|damag\w*|injur\w*|affect\w*|alter\w*|'
    r'regulat\w*|inva\w*|adher\w*|translocat\w*|permea\w*|leak\w*|strength\w*|'
    r'enhanc\w*|degrad\w*|penetrat\w*|surviv\w*)\b', re.I)


def barrier_function_process(text: str) -> bool:
    """A physical gut barrier process, not a barrier to providing treatment.

    This only checks the duty of a literal sentence. Its actual organism,
    experimental conditions, predicate and applicability still require both
    independent source reviews; no patient barrier state is created here.
    """
    return isinstance(text, str) and any(_BARRIER_TISSUE.search(sentence)
        and _BARRIER_PROCESS.search(sentence)
        for sentence in re.split(r'[。！？!?\n]|\.(?=\s+[A-Z]|$)', text))


def health_function_duty_failures(claim: str,
        source_quotes: tuple[str, ...] | list[str],
        assessment_context: tuple[dict, ...] | list[dict]) -> list[str]:
    barrier = any(row.get('semanticType') == 'healthSummary'
        and row.get('code') == 'barrierRelatedBacteria'
        for row in assessment_context if isinstance(row, dict))
    if not barrier:
        return []
    if not barrier_function_process(claim) or not any(
            barrier_function_process(quote) for quote in source_quotes if isinstance(quote, str)):
        return ['本栏为肠道屏障损伤相关菌的健康意义，声明及所选原句须各自具有真实的黏膜、上皮、'
            '通透性或紧密连接等屏障相关过程；同一菌种的维生素合成、化疗药物毒性或其他功能'
            '不能替代本栏职责。没有适用原句时保留空值，不添加屏障机制或改写成该作用。']
    return []


_MICROBIAL_SYNTHESIS_CLAIM = re.compile(
    r'(?:微生物|细菌|菌群)[^。；;\n]{0,22}(?:合成(?!功能|能力|潜力)|产生|生成|生产)|'
    r'\b(?:microb\w*|bacter\w*|microorganisms?)\b[^.;\n]{0,45}'
    r'\b(?:synth\w*|biosynth\w*|produc\w*|generat\w*)\b', re.I)
_MICROBIAL_OBJECT = re.compile(
    r'微生物|细菌|菌群|\b(?:microb\w*|bacter\w*|microorganisms?)\b', re.I)
_SYNTHESIS = re.compile(r'合成|产生|生成|生产|\b(?:synth\w*|biosynth\w*|produc\w*|generat\w*)\b', re.I)
_FOOD_NUTRITION_CLAIM = re.compile(
    r'(?:食品|营养)[^。；;\n]{0,12}(?:领域|行业|产业)|'
    r'\b(?:food|nutri\w*)\b[^.;\n]{0,25}\b(?:industr\w*|field|sector)\b', re.I)
_FOOD_NUTRITION = re.compile(r'食品|营养|\b(?:food|nutri\w*)\b', re.I)
_CELL_FUNCTION_CLAIM = re.compile(r'(?:多种|多个|各种|多项|多样)[^。；;\n]{0,5}细胞功能|细胞[^。；;\n]{0,5}多种功能|\b(?:many|multiple|various)\s+cell(?:ular)?\s+functions?\b', re.I)
_CELL_FUNCTION = re.compile(r'细胞功能|细胞[^。；;\n]{0,8}(?:功能|作用)|\bcell(?:ular)?\s+functions?\b', re.I)
_CHRONIC = re.compile(r'慢性|\bchronic\w*\b', re.I)
_CHRONIC_SUPPORT = re.compile(r'慢性|长期|持久|\b(?:chronic\w*|long.term|persistent|prolonged)\b', re.I)
_CHRONIC_NONDEFINITION = re.compile(
    r'危险因素|风险因素|致病因素|病因|患者|人群|语境|背景|'
    r'\b(?:risk factor|causes?|patients?|population|context|background)\b', re.I)


def _chronic_predications(text):
    """Find explicit X-is-chronic predicates, not a chronic-X modifier."""
    result = []
    for sentence in re.split(r'[。；;.!?\n]', text):
        for pattern in (
            r'(?P<subject>[^，,。；;:\n]{1,65}?)(?:是|属于|(?<!分)为)(?P<object>[^，,。；;\n]{0,40}慢性[^，,。；;\n]{0,45})',
            r'(?P<subject>[A-Za-z][A-Za-z\s()\-\x27]{0,100}?)\b(?:is|are|was|were)\s+'
            r'(?P<object>[^,;.!?\n]{0,45}\bchronic\b[^,;.!?\n]{0,65})',
        ):
            match = re.search(pattern, sentence, re.I)
            if match and not _CHRONIC_NONDEFINITION.search(match['object']):
                subject = match['subject'].strip()
                if subject.casefold() not in {'it', 'they', 'this', 'the disease', 'this disease'}:
                    result.append(subject)
    return result


def _chronic_assertion_support(claim, original):
    subjects = _chronic_predications(claim)
    if not subjects:
        return bool(_CHRONIC_SUPPORT.search(original))
    candidates = _chronic_predications(original)
    normalize = lambda value: re.sub(r'[^\w\u3400-\u9fff]', '', value).casefold().rstrip('s')
    def compatible(left, right):
        a, b = normalize(left), normalize(right)
        same_language = bool(re.search(r'[\u3400-\u9fff]', left)) == bool(re.search(r'[\u3400-\u9fff]', right))
        # Cross-language object equivalence still needs both semantic reviews;
        # syntax is only a necessary gate and never a translation/approval.
        return not same_language or a in b or b in a
    return all(any(compatible(subject, candidate) for candidate in candidates) for subject in subjects)


_SINGLE_MEMBER_CLASS = re.compile(
    r'(?P<member>[^，,。；;\n]{1,24}?)等(?P<group>[^，,。；;\n]{0,10}(?:心理状态|精神状态|疾病|障碍|病种))')
_CLASS_RELATION = re.compile(r'相关|关联|风险|预测|\b(?:associat\w*|related|risk|predict\w*)\b', re.I)


def _single_member_expansion(claim):
    return any(not re.search(r'、|和|及|与|以及', match['member'])
        for match in _SINGLE_MEMBER_CLASS.finditer(claim))


def _plural_class_relation_support(original):
    for sentence in re.split(r'[。；;.!?\n]', original):
        if not _CLASS_RELATION.search(sentence):
            continue
        if (re.search(r'(?:多种|多个|各类|不同)(?:心理|精神)?(?:状态|疾病|障碍|病种)', sentence)
                or re.search(r'[^，,]{1,12}、[^，,]{1,12}(?:心理状态|精神状态|疾病|障碍)', sentence)
                or re.search(r'\b(?:mental|psychological|psychiatric|affective)\s+(?:health\s+)?'
                    r'(?:states|conditions|disorders|factors)\b', sentence, re.I)
                or re.search(r'\b(?!it\b|the\b|this\b)[A-Za-z-]+\s+and\s+'
                    r'(?!it\b|the\b|this\b)[A-Za-z-]+\s+(?:are|were|have|show)\b', sentence, re.I)):
            return True
    return False
_DEFENSE_CLASSIFICATION_CLAIM = re.compile(
    r'(?:是|属于|归入|作为)[^。；;\n]{0,30}(?:防御与耐受|防御和耐受|防御|耐受)'
    r'[^。；;\n]{0,15}(?:功能|机制)|'
    r'\b(?:defen[cs]e|tolerance)\b[^.;\n]{0,25}\b(?:function|mechanism)\b', re.I)
_DEFENSE_FUNCTION_SUPPORT = re.compile(
    r'防御|耐受|解毒|\b(?:defen[cs]e|toleran\w*|detoxif\w*)\b|'
    r'\bprotect\w*\b[^.;\n]{0,35}\b(?:toxins?|xenobiotics?)\b', re.I)
_CHEMOTAXIS = re.compile(r'趋化|\bchemotax\w*\b', re.I)
_DEGRADATION_AVAILABILITY = re.compile(
    r'(?:降解|分解)(?:能力|过程|活动|作用)[^，,。；;\n]{0,12}'
    r'(?:影响|改变|提高|增加|降低|减少)[^，,。；;\n]{0,25}生物可利用性|'
    r'\b(?:degradation|biodegradation|breakdown)\s+(?:capacity|capabilit\w*|abilit\w*)'
    r'[^,.;\n]{0,35}\b(?:affect\w*|alter\w*|chang\w*|increas\w*|enhanc\w*|reduc\w*)\b'
    r'[^,.;\n]{0,40}\bbioavailability\b', re.I)


def _direct_degradation_availability_support(text):
    # Do not borrow a chemotaxis sentence's predicate for a neighbouring
    # degradation noun. This necessary check recognizes an explicit direct
    # relation only; the normal semantic/causal-strength review still follows.
    return any(_DEGRADATION_AVAILABILITY.search(sentence)
        and not _CHEMOTAXIS.search(sentence)
        for sentence in re.split(r'[。！？.!?\n]', text))


def _atomic_coverage_gaps(claim: str, quotes: list[str], full_text: str,
        source_names: tuple[str, ...] | list[str]) -> list[tuple[str, bool]]:
    if not isinstance(claim, str):
        return []
    selected = '\n'.join(q for q in quotes if isinstance(q, str))
    full = full_text if isinstance(full_text, str) else ''
    # A literal source item such as "慢性胃炎：未检出" is not a new
    # chronicity assertion. Source identity/state still has its own audit.
    description = claim
    for name in sorted((n for n in source_names if isinstance(n, str) and n.strip() and _CHRONIC.search(n)), key=len, reverse=True):
        description = re.sub(re.escape(name), '', description, flags=re.I)
    checks = [
        ('微生物合成', bool(_MICROBIAL_SYNTHESIS_CLAIM.search(claim)),
            lambda text: bool(_MICROBIAL_OBJECT.search(text) and _SYNTHESIS.search(text))),
        ('食品营养领域', bool(_FOOD_NUTRITION_CLAIM.search(claim)),
            lambda text: bool(_FOOD_NUTRITION.search(text))),
        ('多种细胞功能', bool(_CELL_FUNCTION_CLAIM.search(claim)),
            lambda text: bool(_CELL_FUNCTION.search(text))),
        ('新增慢性描述', bool(_CHRONIC.search(description)),
            lambda text: _chronic_assertion_support(description, text)),
        ('单一对象扩成“等”类群的关系', _single_member_expansion(claim),
            _plural_class_relation_support),
        ('防御与耐受的功能归属', bool(_DEFENSE_CLASSIFICATION_CLAIM.search(claim)),
            lambda text: bool(_DEFENSE_FUNCTION_SUPPORT.search(text))),
    ]
    gaps = [(label, supported(full)) for label, present, supported in checks
        if present and not supported(selected)]
    if (_DEGRADATION_AVAILABILITY.search(claim) and _CHEMOTAXIS.search(selected + '\n' + full)
            and not _direct_degradation_availability_support(selected)):
        gaps.append(('分解能力影响生物可利用性的主语与因果关系',
            _direct_degradation_availability_support(full)))
    return gaps


_PRECURSOR_PARALLEL_PREDICATE = re.compile(
    r'前体[，,；;]\s*(?:并且?|而且|且|还)(?P<predicate>[^。；;\n]+)')
_UPSTREAM_PARALLEL_ACTION = re.compile(
    r'^(?:(?:可|能|可以|能够|可能|直接|有助于)\s*)*'
    r'(?:参与|调节|影响|控制|促进|抑制|维持|决定|介导|造成|导致|引发|提高|降低|增加|减少|支持)')
_PRECURSOR_DOWNSTREAM_RELATIVE = re.compile(
    r'\bprecursors?\s+(?:for|of|to)\s+[^.;\n]{1,120}?,\s*'
    r'(?:which|that)\s+(?P<predicate>[^.;\n]+)|'
    r'前体[，,]\s*(?:后者|该(?:神经递质|下游产物|产物))(?P<chinese>[^。；;\n]+)', re.I)
# Cross-language effect families identify the predicate that changes actor;
# neither the chemical identity nor a paper/title/number is hard coded.
_ACTOR_EFFECT_FAMILIES = tuple(re.compile(pattern, re.I) for pattern in (
    r'运动|蠕动|\bmotility\b|\bperistals\w*\b',
    r'分泌|\bsecret\w*\b', r'免疫|\bimmun\w*\b',
    r'睡眠|\bsleep\w*\b', r'饥饿|食欲|\bhunger\b|\bappetite\b',
    r'情绪|\bemotion\w*\b|\bmood\b', r'疼痛|痛觉|\bpain\b',
    r'血压|\bblood\s+pressure\b',
    r'蛋白(?:质)?合成|\bprotein\s+(?:synth\w*|formation)\b',
))


def precursor_predicate_actor_failures(claim: str, source_quotes: list[str] | tuple[str, ...]) -> list[str]:
    """Do not move a product's selected relative-clause effect to its precursor.

    This necessary grammar/semantic-atom check leaves explicit downstream
    subjects or an explicit mediated relation to the ordinary reviewers. It
    does not rewrite the complete authored sentence or supply a missing role.
    """
    if not isinstance(claim, str):
        return []
    source = '\n'.join(q for q in source_quotes if isinstance(q, str))
    downstream = [match.group('predicate') or match.group('chinese')
        for match in _PRECURSOR_DOWNSTREAM_RELATIVE.finditer(source)]
    for match in _PRECURSOR_PARALLEL_PREDICATE.finditer(claim):
        predicate = match['predicate'].strip()
        if not _UPSTREAM_PARALLEL_ACTION.search(predicate):
            continue  # Explicit 后者/product/通过 subjects are not this ambiguity.
        for effect in _ACTOR_EFFECT_FAMILIES:
            if effect.search(predicate) and any(effect.search(part) for part in downstream):
                return ['前体关系后的并列作用谓语仍以其上游物质作主语，但所选原文的which/后者明确把该作用归于下游产物；'
                    '须由作者完整重写并保留真实作用主体或原文明示的介导关系，重新接受原文双审，'
                    '不能删掉尾句、替换词语或用已支持的前体关系豁免错接的健康作用。']
    return []


_UNCERTAIN_EFFECT_FAMILIES = (*_ACTOR_EFFECT_FAMILIES, *(re.compile(pattern, re.I) for pattern in (
    r'抗(?:肿瘤|癌)|抗肿瘤|\b(?:antineoplastic|antitumou?r|anticancer)\b',
    r'炎症|\binflamm\w*\b', r'屏障|\bbarrier\b',
    r'感染|\binfect\w*\b', r'糖尿病|\bdiabet\w*\b',
    r'心血管|\bcardiovascular\b', r'死亡|\bmortality\b',
    r'生成|产生|合成|\b(?:produc\w*|synthesi[sz]\w*|biosynth\w*)\b',
)))
_SOURCE_UNCERTAINTY = re.compile(
    r'\b(?:may|might|could|possibly|potentially|seems?\s+to|appears?\s+to|'
    r'(?:is|are)\s+(?:thought|believed|suggested)\s+to)\b|可能|或许|似乎|潜在', re.I)
_CLAIM_UNCERTAINTY = re.compile(
    r'可能|或许|似乎|潜在|有望|\b(?:may|might|could|potentially|seems?\s+to|appears?\s+to)\b', re.I)


def _effect_uncertainty_scoped(sentence, effect, hedge):
    """Read a preceding modal in the effect's own or coordinated clause."""
    clauses = list(re.finditer(r'[^，,；;。！？!?\n]+', sentence))
    for match in effect.finditer(sentence):
        index = next((i for i, part in enumerate(clauses)
            if part.start() <= match.start() < part.end()), None)
        if index is None:
            continue
        part = clauses[index]
        prefix = sentence[part.start():match.start()]
        if hedge.search(prefix):
            return True
        # "may have functions, including X" and "可能调节A，并影响B"
        # retain the same governing modal. A new named actor does not.
        current = part.group().strip()
        while index and re.match(r'^(?:包括|例如|如|并(?:且)?|且|及|以及|'
                r'(?i:including|such as|and)\b)', current):
            index -= 1
            current = clauses[index].group().strip()
            if hedge.search(current):
                return True
    return False


def original_uncertainty_strength_failures(claim, selected_quotes):
    """Reject a known hedged effect promoted to an unqualified assertion.

    This narrow necessary gate uses only actual selected original passages;
    ordinary reviews still own the actor, scope, direction and full support.
    An uncertain neighbouring effect never qualifies another assertion, and
    no text, modal, quotation or approval is inserted by this check.
    """
    if not isinstance(claim, str):
        return []
    parts = [part for quote in selected_quotes if isinstance(quote, str)
        for part in re.split(r'[。；;！？!?\n]|\.(?=\s+[A-Z]|$)|'
            r'\b(?:but|whereas|while)\b|但是|然而|但', quote, flags=re.I) if part.strip()]
    for effect in _UNCERTAIN_EFFECT_FAMILIES:
        if not effect.search(claim):
            continue
        relevant = [part for part in parts if effect.search(part)]
        if not relevant or not all(_effect_uncertainty_scoped(part, effect, _SOURCE_UNCERTAINTY)
                for part in relevant):
            continue
        assertions = [part for part in re.split(r'[。；;！？!?\n]|但是|然而|但', claim)
            if effect.search(part)]
        if any(not _effect_uncertainty_scoped(part, effect, _CLAIM_UNCERTAINTY) for part in assertions):
            return ['所选原句仅以may、seems等语气陈述该具体作用或关系，正文将其升级为确定结论；'
                '须完整重写并保留该谓语的实际不确定度、对象和研究范围，或不采用该医学延伸。'
                '不能在句末机械追加可能性，也不能让其他作用的模态词替本断言限定。']
    return []


def atomic_citation_coverage_failures(claim: str, selected_quotes: list[str], *,
        full_source_text: str = '', source_names: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Necessary lexical atoms for a few scoped assertions, not NLP approval.

    A full-text hit is only a reason to ask the existing reviewer to reselect
    its genuine QIDs. It never fills IDs or approves a missing quotation.
    Full support and all semantic/patient/causal gates remain independent.
    """
    failures = [f'所选原文未覆盖“{label}”断言；' + (
        '本篇完整原文存在该原子支持，须由审核重新选择实际支持的QID，不能自动补号或改写claim。'
        if repairable else '本篇完整原文也无该原子支持，须拒绝该新增断言，不能以常识或不冲突豁免。')
        for label, repairable in _atomic_coverage_gaps(claim, selected_quotes, full_source_text, source_names)]
    failures.extend(precursor_predicate_actor_failures(claim,
        [full_source_text] if full_source_text else selected_quotes))
    return failures


def atomic_citation_metadata_failures(item: dict, review: dict) -> list[str]:
    """Return only repairable selected-QID omissions for the bounded repair."""
    if not isinstance(item, dict) or not isinstance(review, dict) or review.get('valid') is not True:
        return []
    evidence = item.get('evidence')
    if not isinstance(evidence, dict):
        return []
    options = evidence.get('quoteOptions')
    identifiers = review.get('supportQuoteIds')
    if not isinstance(options, dict) or not isinstance(identifiers, list):
        return []
    selected = [options[key] for key in identifiers if isinstance(key, str)
        and isinstance(options.get(key), str)]
    facts = item.get('facts') if isinstance(item.get('facts'), list) else []
    names = [fact['name'] for fact in facts if isinstance(fact, dict)
        and isinstance(fact.get('name'), str)]
    if isinstance(item.get('sourceNames'), list):
        names.extend(name for name in item['sourceNames'] if isinstance(name, str))
    gaps = _atomic_coverage_gaps(item.get('claim', ''), selected, evidence.get('text', ''), names)
    return [f'supportQuoteIds: 所选QID未完整覆盖“{label}”；本篇完整原文存在该原子支持，'
        '请重新审核并选择真实QID。不得自动补号、改写原句或患者事实；无法支持时返回valid=false。'
        for label, repairable in gaps if repairable]


ORIGINAL_SUPPORT_RATIONALE_RULE = (
    '审核理由须为每个定义、机制、关联或临床原则分别指出本item实际Q原句及其支持的具体断言，'
    '并核对原句谓语、对象、人群和强度。may、might、seems、appears等限定属于对应作用谓语，'
    '不得升级为确定功能或关系；保留该作用的实际不确定度，不能借另一分句的可能性或句末泛化限制通过。'
    '疾病定义及通行知识同样需要正面原文依据；'
    '未出现相反说法、不冲突、否定表述、医学常识或一般背景均不能替代支持。'
    '任一断言没有原文支持即valid=false并列入unsupportedClaims，不能让后半句依据替前半句通过；'
    '作者可以拆句，仅保留确有支持的内容。原文明示定义时直接说明Q原句如何支持该定义，'
    '有合并症或基础疾病不证明免疫低下；免疫缺陷、免疫抑制或免疫受损人群须有原文明示。'
    '特定谱系或亚型在有合并症成人中的疾病结局，须保留实际谱系、成人及合并症范围，'
    '不得扩成一般菌株或未限定人群；同篇独立的一般定植或菌株差异陈述仍可按自身原文采用。'
    '不必添加常识或不冲突的豁免理由。一般科学解释不要求论文包含本人分数或预先确诊，'
    '但这一适用范围区别不免除所述生物学或医学断言本身的原文支持。'
    '慢性疾病的定义须由同一主语的定义谓语支持；研究在chronic某疾病背景下开展，不证明全部该病种都是慢性。'
    '单一对象的关系不得扩成该对象等一类状态；只有原文明示该复数类群或其他成员的相应关系才保留扩展。')

SINGLE_ORIGINAL_FACT_AUTHOR_RULE = (
    '每个有引文的句子只陈述实际原文完整支持的一条事实，保留其主语、谓语、对象和适用范围。'
    '没有对应原句时直接省去开头通用定义、重要慢性疾病等铺垫，也不把单一对象延伸成某对象等一类状态；'
    '一般背景、常识、与语境呼应或无反证不能补全没有正面依据的前提。'
    '有合并症不代表免疫低下；免疫受损人群须有明确原句。特定亚型或谱系的结局，'
    '保留实际亚型、成人等年龄及合并症范围，不概括为未限定菌株或一般人群。')


GENERAL_FUNCTION_AUTHOR_RULE = (
    SINGLE_ORIGINAL_FACT_AUTHOR_RULE +
    '以已有实测子项定位功能域，先保留来源状态，再用独立有引文的句子说明该功能或代谢物的一般生物学职责、'
    '生成或分解过程及健康意义。优先采用原文明示的一般功能和机制；其他疾病人群的差异或特定治疗效应结果'
    '留在其研究语境，不能代替本功能域的一般说明。一般意义与本人当前源状态分别表达，'
    '不把研究组的方向、治疗反应或生物标志物变化写成本人的状态。'
    '本节点不写增加菌水平、补充或治疗干预的策略方向；该类句子整句不采用，不能删去研究限制后留更强结论。'
    '特定菌株与底物组合的摄入试验结果不能代替本人所测菌组的基础作用；'
    '同篇明确独立陈述的基础过程仍可选择并重新双审，不因整篇包含试验而禁用其真实基础句。'
    'may、seems等原文模态须落在实际作用谓语，不能改成确定功能或在句末机械补可能。'
    '按所述用途选择句子：不以工业、商业、农业、制药制造应用或行业市场需求充当功能健康意义；'
    '同时含生物性质和市场用途的句子也整句不采用，另选原文独立支持的生物功能句。'
    '同文真实的生物合成、代谢机制、基础生化或体外生物功能可以使用，不按论文标题、产业词或实验类型禁用整篇来源。'
    '不以预测、检测或模型能力与人体营养或疾病状态不能等同、不能推导等输出边界声明充当健康意义；'
    '含该类提示的句子完整不采用，不能只删限定词保留更强结论。另选原文明示的生物功能，'
    '实际研究的人群、标本和实验范围限制继续保留。耐药基因等单项的解释限于本项真实结果与功能关系，'
    '不扩展成整章或其他医学问题的全局关注、随访或管理判断。'
    '逐个保留原文的作用主语、机制与方向：趋化使细菌接近污染物并提高可及性，不可改成分解能力改变生物可利用性。'
    '实际成员相同也不代表功能职责相同：肠屏障相关菌须解释原文明示的黏膜、上皮侵袭、通透性、'
    '紧密连接等屏障过程，不能用该成员的维生素合成或药物毒性作用代替；没有适用原句时如实留空。'
    '防御与耐受等报告栏目名仅用于组织来源结果，不能当论文已证明的功能归属；新增该归属也须有原文。')

DISEASE_MEANING_AUTHOR_RULE = (
    SINGLE_ORIGINAL_FACT_AUTHOR_RULE +
    '以diseaseMeaningTarget指定的具体疾病为主题，解释本人的菌群信号、已存在的同病临床问题及本次关注目标。'
    '采用clinicalProblemState和真实个人事实；已有脂肪肝、肥胖等明确记录不能只写成模型风险。'
    '无同病临床资料时围绕真实菌群风险解释，不用其他疾病的正常检查或不能印证等说明充数。'
    '仅在帮助理解个人风险时简短说明有原文支持的医学关联，不逐段复述研究或先重抄模型等级。'
    '菌名和论文所述另一疾病不是本项解释对象。另一疾病自身的定义不能充当本项目解释。'
    '真实的反复发作、缓解或复发性病程可以说明；仅讨论长期治疗目标、维持或停药试验中复发定义'
    '是否一致的方法学内容，不能代替本项疾病医学含义。'
    '罕见继发病因、特定病因病例及其机制不能代替本项通用定义；只有实际肯定临床来源和本句所选原文'
    '均已建立该具体病因时，才解释该病因语境，不因病例真实、注明罕见或没有直接诊断本人而免审。'
    '若原文明确以另一主病种的定义开篇，并讨论其罕见或继发表现，缺少该病种的实际肯定临床前提时，'
    '不采用该文其他句子换句式或隐去病因名称来填充本项通用医学含义。'
    '新增文献解释中，bloating规范译为主观感受的腹胀，visible abdominal distention规范译为可见腹围增加的腹部膨隆，'
    '不将distention译为腹胀气或据此推断气体增加；仅规范新增解释，原A/B/C项目名、原文引文及书目metadata标题保持原样。')

SOURCE_COMPARISON_AUTHOR_RULE = (
    'clinicalCorrelation专用于现有A/B/C资料中同一健康问题的源内对照：说明真实症状、病史、医学发现、'
    '菌群记录和来源模型之间的并存、相互印证及方向差异。本节点不另排全局关注顺序，不新增管理行动，'
    '不改变第二章与第八章已形成的全局优先关系；不另列关注重点，不自行安排随访，'
    '不用泛化诊断边界提示填充对照。耐药基因等单项结果仅解释本项并存或差异，'
    '不得扩展为整章或其他医学问题的全局判断。仅有一种来源时仍可解释该来源内已有关系，'
    '不补造其他来源。外部论文的医学意义放在项目explanation、医学发现explanation或功能通路healthMeaning；'
    '本节点不复述缺少本人前提的研究人群、生物标志物分期或治疗效应，也不以研究不代表本人之类边界句填充。')


EVIDENCE_ROLE_REVIEW_INSTRUCTIONS = {
    "disease_meaning": "本句职责为疾病医学含义。" + DISEASE_MEANING_AUTHOR_RULE,
    "general_function": (
        "本句职责为一般功能健康意义。" + GENERAL_FUNCTION_AUTHOR_RULE +
        "核对原文实际对象、功能、代谢环节和结局，保留人群研究或实验研究的层级。"
        "它可以解释该功能为何值得关注，不要求论文包含本人的分值或验证本次分级阈值。"
        "本人原始结果及分级由独立患者来源证明；不得把一般机制写成本人已发生的生理变化、疾病或干预效果。"),
    "population_association": (
        "本句职责为人群相关性：完整保留研究人群、标本、分类层级、方向与关联性质。"
        "准确介绍研究中的关联无需把本人假定为研究病例；将关联用于本人患病判断、风险数字或行动时，"
        "必须另审真实研究适用条件和本人前提，不把相关性改成因果，不把其他标本或亚型结果移用。"),
    "individual_synthesis": (
        "本句职责为个体综合：从真实菌群与功能发现出发，结合已绑定的个人医学和生活资料明确具体健康问题与关注顺序，"
        "保持原项目、数值、等级、日期语境及疑似程度，不为缺失的问卷补写答案。"
        "已记录的临床疾病与潜在风险分开表达；不将无关正常检查当成疾病排除或为不能印证添加段落。"
        "各类数据可以共同支持个人关注判断，不要求原文已经写出相同的综合评语；保留每条个人前提。"
        "其他综合节点中，论文不必出现本人的观测，但新增病因、机制、诊断、风险或效果断言仍须由本句实际适用原文支持；"
        "不能把这些新增断言豁免为评估意见。"),
    "clinical_principle": (
        "本句职责为临床诊断或评估原则：以原文实际陈述的诊断能力、确认依据及评估目的为限。"
        "疑似发现可用于说明为何关注诊断原则，不要求本人预先确诊才能解释确诊原则。"
        "确诊、分型、年龄或治疗阶段等真实研究适用条件仍独立审核；诊断重要性不自动授权检查行动，"
        "指南目标不等于已证明的获益，不推断患者检查已做或未做。"),
    "grade_validation": (
        "只有本句明确声称验证某项量表、阈值、概率或分级时，才要求同一测量对象与量纲的验证证据。"
        "一般功能意义、人群相关性和临床原则不因未验证本次来源分级而一律拒绝；"
        "它们也不能反过来冒充该分级或模型概率已获临床验证。"),
}
