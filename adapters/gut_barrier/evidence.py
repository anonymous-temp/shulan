"""Bounded candidate retrieval for the independent gut-barrier report.

Callers must supply reviewed, de-identified clinical queries, never an entire
assessment, questionnaire or patient narrative. Results are retrieval candidates,
not verified recommendations, patient diagnoses or numerical risk estimates.

An existing EviMedClient is injected by the composition root; this module does
not import config.env (nor adapters.report, whose package initializes config).
Europe PMC fallback uses its official metadata/abstract search API only:
https://europepmc.org/RestfulWebService . No publisher/full-text URL is fetched.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from modules.gut_barrier.rules import is_retired_marker_name


EVIDENCE_VERSION = "gut-barrier-candidate-evidence.v15-objective-context"
EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_STOPWORDS = frozenset("adult adults human humans guideline guidelines clinical practice consensus "
    "review reviews systematic trial trials cohort risk risks assessment association management "
    "diagnosis treatment prevention and the for with from of in or based health disease diseases".split())
_SYNONYMS = {
    "obesity": ("obesity", "obese", "overweight"),
    "diabetes": ("diabetes", "diabetic", "glycemic", "glycaemic"),
    "diarrhea": ("diarrhea", "diarrhoea"),
    "metabolic": ("metabolic", "cardiometabolic", "metabolism"),
    "intestinal": ("intestinal", "gut", "intestine"),
    "barrier": ("barrier", "permeability"),
}
_TOPICS = {
    "parkinsons": r"\bparkinson(?:['’]s)?\s+disease\b|帕金森病",
    "depression": r"\b(?:depression|depressive\s+disorder)\b|抑郁症|抑郁障碍",
    "rheumatoid_arthritis": r"\brheumatoid\s+arthritis\b|类风湿(?:性)?关节炎",
    "colorectal_cancer": r"\b(?:colorectal|colon|rectal)\s+cancer\b|结直肠癌|结肠癌|直肠癌",
    "cirrhosis": r"\bcirrhosis\b|肝硬化",
    "hyperuricemia": r"\b(?:hyperuric(?:emia|aemia)|gout)\b|高尿酸血症|痛风",
    "coronary_heart_disease": r"\b(?:chronic\s+coronary|coronary\s+heart)\s+disease\b|冠心病|冠状动脉粥样硬化性心脏病",
    "stroke": r"\bstroke\b|脑卒中|脑梗死",
    "alzheimers": r"\balzheimer(?:['’]s)?\s+disease\b|\bdementia\b|阿尔茨海默病|痴呆",
    "anxiety": r"\b(?:anxiety|panic)\s+disorder\b|焦虑症|焦虑障碍|惊恐障碍",
    "systemic_lupus": r"\bsystemic\s+lupus\s+erythematosus\b|系统性红斑狼疮",
    "liver_cancer": r"\bhepatocellular\s+carcinoma\b|肝细胞癌",
    "lung_cancer": r"\blung\s+cancer\b|肺癌",
    "gastritis": r"\bgastritis\b|胃炎",
    "cardiovascular": r"\b(?:cardiovascular|atherosclero\w*|coronary|ascvd)\b|心血管|动脉粥样硬化|冠状动脉|冠心病",
    "ibs": r"\birritable\s+bowel(?:\s+syndrome)?\b|\bibs(?:\s*d)?\b|肠易激(?:综合[征症])?",
    "ibd": r"(?<!non )\b(?:inflammatory\s+bowel\s+diseases?|ibd)\b|\bcrohn(?:['’]s)?\s+disease\b|\bulcerative\s+colitis\b|(?<!非)炎症性肠病|(?<!非)炎性肠病|克罗恩病|溃疡性结肠炎",
    "diarrhea": r"\bdiarrh(?:ea|oea)\b|腹泻",
    "calprotectin": r"\bcalprotectin\b|钙卫蛋白",
    "obesity": r"\b(?:obesity|obese|overweight)\b|肥胖|超重",
    "diabetes": r"\bdiabet(?:es|ic)\b|糖尿病",
    "fatty_liver": r"\b(?:fatty\s+liver|steatotic\s+liver|liver\s+steatosis|nafld|mafld|masld)\b|脂肪肝|脂肪性肝病",
    "intestinal_barrier": r"\b(?:intestinal|gut|mucosal)\s+(?:barrier|permeability)\b|肠(?:道|黏膜|粘膜)?屏障|肠(?:道)?通透性",
    "hypertension": r"\bhypertension\b|\bhigh\s+blood\s+pressure\b|高血压",
    "dyslipidemia": r"\b(?:dyslipid(?:emia|aemia)|hyperlipid(?:emia|aemia)|hypercholesterol(?:emia|aemia))\b|\b(?:blood\s+)?(?:lipid|cholesterol)\s+management\b|血脂(?:异常|管理)|高脂血症|高胆固醇血症",
    "h_pylori": r"\b(?:helicobacter|h\.?)\s+pylori\b|幽门螺杆菌|幽門螺桿菌",
    "sleep_apnea": r"\b(?:obstructive\s+)?sleep\s+apn(?:ea|oea)\b|\bosa\b|睡眠呼吸暂停|睡眠呼吸中止",
    "kidney": r"\bchronic\s+(?:kidney|renal)\s+disease\b|\bckd\b|慢性肾(?:脏)?病",
}
_HISTORY_TOPICS = (
    "parkinsons", "depression", "rheumatoid_arthritis", "colorectal_cancer",
    "cirrhosis", "hyperuricemia", "coronary_heart_disease", "stroke",
    "alzheimers", "anxiety", "systemic_lupus", "liver_cancer", "lung_cancer",
)
_GUIDANCE = re.compile(r"\bguidelines?\b|\bguidance\b|\bconsensus\b|\b(?:scientific|position)\s+statement\b|"
                       r"\bclinical\s+practice\b|\bappropriateness\s+criteria\b|指南|共识|临床实践", re.I)
_GUIDELINE_TITLE = re.compile(r"\bguidelines?\b|\bguidance\b|\bconsensus\b|指南|共识|共識", re.I)
_PROTOCOL_TITLE = re.compile(r"\bprotocol\b|研究方案|试验方案|試驗方案|研究计划|研究計劃", re.I)
_PEDIATRIC_TITLE = re.compile(r"\b(?:pediatric|paediatric|children|childhood|infants?|neonat\w*|adolescents?)\b|儿童|兒童|婴儿|嬰兒|新生儿|青少年", re.I)
_ADULT_TITLE = re.compile(r"\badults?\b|成年|成人", re.I)
_ANIMAL_TITLE = re.compile(r"\b(?:mice|mouse|rats?|murine|rodents?|rabbits?|porcine|swine|zebrafish|animals?)\b|小鼠|大鼠|动物模型|动物实验", re.I)
_HUMAN_TITLE = re.compile(r"\b(?:humans?|patients?|people)\b|人体|人群|患者|临床", re.I)
_DIAGNOSTIC_QUESTION = re.compile(r"\b(?:diagnos(?:is|tic)|screen(?:ing)?|evaluat(?:e|ion)|assessment|testing|workup)\b|诊断|筛查|评估|检查|检测|鉴别", re.I)
_DIAGNOSTIC_SCOPE = re.compile(r"\b(?:diagnos(?:is|tic)|screening)\b|诊断|筛查", re.I)
_FMT_TITLE = re.compile(r"\bfmt\b|\b(?:fecal|faecal)\s+microbi(?:ota|ome)\b|\bmicrobiota\s+(?:based\s+therap|transplant)|粪菌移植|粪便微生物|粪便菌群|菌群移植", re.I)
_TREATMENT_TITLE = re.compile(r"\b(?:therap(?:y|ies|eutics?)|treatments?|pharmacologic(?:al)?|pharmacotherap(?:y|ies)|prescribing|medications?|surgery|surgical|bariatric|chemotherapy|radiotherapy)\b|药物|用药|处方|手术|疗法|治疗|治療", re.I)
_DONOR_SCOPE = re.compile(r"\bdonors?\b|供体|供者", re.I)
_SECONDARY_GUIDELINE_TITLE = re.compile(
    r"(?:指南|共识|共識|指导意见|指导原则).{0,60}(?:解读|解讀|述评|述評|导读|評析|评析|摘译|摘譯|摘要)|"
    r"(?:解读|解讀)[《〈“].{0,100}(?:指南|共识|共識)|"
    r"\b(?:comments?|commentary|editorial|interpretation)\b.{0,200}\b(?:guidelines?|guidance|consensus)\b|"
    r"\b(?:comparison|review)\s+(?:of|between)\b.{0,200}\b(?:guidelines?|guidance|consensus)\b|"
    r"\b(?:guidelines?|guidance|consensus)\b.{0,100}\b(?:commentary|editorial)\b|"
    r"\bguideline\s+highlights?\b|\bgap\s+between\s+guidelines?\s+and\s+clinical\s+practice\b|"
    r"\b(?:implications|impact)\s+of\b.{0,100}\b(?:versus|vs\.?|compared\s+with)\b.{0,100}\bguidelines?\b", re.I)
_GUIDELINE_APPLICATION_TITLE = re.compile(
    r"\bguidelines?\s+directed\s+application\b|"
    r"\b(?:adherence|adhérence|compliance)\s+(?:to|with)\b.{0,120}\bguidelines?\b|"
    r"\bguidelines?\s+(?:adherence|adhérence|compliance)\b|"
    r"\b(?:implementation|application)\s+of\b.{0,120}\bguidelines?\b|"
    r"指南(?:依从性|遵循情况|实施效果|应用效果)|(?:指南实施|指南应用).{0,60}(?:队列|调查|横断面|观察性研究)", re.I)
_GUIDELINE_MODIFIER = re.compile(r"\bguidelines?\s+(?:directed|based|concordant|recommended)\b", re.I)
_EMPIRICAL_STUDY_TITLE = re.compile(
    r"\b(?:cohort|registry|cross\s+sectional|observational|randomi[sz]ed|retrospective|prospective|real\s+world)\b|"
    r"队列|横断面|观察性|随机对照|真实世界", re.I)
_EMPIRICAL_PARTICIPANTS = re.compile(
    r"\bmethods?\b[^.!?。！？]{0,650}\b(?:participants?|patients?|individuals?)\b"
    r"[^.!?。！？]{0,300}\bwere\s+(?:included|enrolled|recruited|selected|analy[sz]ed)\b|"
    r"\b(?:we|this\s+study)\s+(?:enrolled|recruited)\b[^.!?。！？]{0,160}\b(?:patients?|participants?)\b", re.I)
_TYPE2 = re.compile(r"\btype\s*(?:2|ii)\s+diabet|\bt2d(?:m)?\b|(?:2|二|Ⅱ)\s*型糖尿病", re.I)
_OTHER_DIABETES_SUBTYPE = re.compile(
    r"\bmody\b|\bmaturity\s+onset\s+diabetes\s+of\s+(?:the\s+)?young\b|"
    r"\b(?:monogenic|neonatal|gestational)\s+diabetes\b|\btype\s*(?:1|i)\s+diabet|"
    r"青少年起病的成人型糖尿病|单基因糖尿病|妊娠(?:期)?糖尿病|(?:1|一|Ⅰ)\s*型糖尿病", re.I)
_CENTRAL_APNEA = re.compile(r"\bcentral\s+sleep\s+apn(?:ea|oea)\b|\bcsa\b|中枢性睡眠呼吸暂停", re.I)
_OBSTRUCTIVE_APNEA = re.compile(r"\bobstructive\s+sleep\s+apn(?:ea|oea)\b|\bosa\b|阻塞性睡眠呼吸暂停", re.I)
_OSA_SPECIALIZED_SCOPE = re.compile(
    r"\b(?:telemedicine|telehealth|endotyp\w*|endophenotyp\w*)\b|"
    r"\bresearch\s+(?:priorit\w*|agenda|statement)\b|\bcomisa\b|\bcomorbid\s+insomnia\b|"
    r"远程医疗|远程医学|内表型|研究重点|研究议程|失眠共病|共病失眠", re.I)
_NUTRITION_SCOPE = re.compile(r"\b(?:dietary|dietetic|nutrition(?:al)?)\b|食养|食疗|膳食|营养干预|营养治疗", re.I)
_KIDNEY_SPECIALIZED_SCOPE = re.compile(
    r"\b(?:dialysis|h(?:ae|e)modialysis|transplant\w*|an(?:ae|e)mia|bone|nutrition\w*)\b|"
    r"\bblood\s+pressure\b|\bdiabetes\s+management\b|透析|移植|贫血|血压|矿物质|骨代谢|营养|糖尿病管理", re.I)
_GASTRITIS_SUBTYPES = (
    r"(?<!non )\batrophic\s+gastritis\b|(?<!非)萎缩性胃炎",
    r"\bautoimmune\s+gastritis\b|自身免疫性胃炎",
    _TOPICS["h_pylori"],
)
# This screen receives de-identified *clinical questions*, not patient context.
# A general question cannot inherit a condition-specific population from a
# retrieval result. Merely discussing obesity among cardiovascular risk factors
# does not restrict a comprehensive prevention guideline to obesity patients.
_RESTRICTED_POPULATIONS = {
    "obesity": _TOPICS["obesity"],
    "diabetes": _TOPICS["diabetes"],
    "kidney": _TOPICS["kidney"],
    "pregnancy": r"\b(?:pregnancy|pregnant|postpartum|puerperium)\b|妊娠|孕妇|产后",
    "hiv": r"\bhiv\b|\bhuman\s+immunodeficiency\s+virus\b|艾滋病",
    "cancer": r"\b(?:cancer|oncolog\w*)\b|癌症|恶性肿瘤",
}
_GENERAL_POPULATION = re.compile(
    r"\b(?:general|unselected)\s+(?:adult\s+)?population\b|"
    r"\ball\s+adults\b|一般(?:成人)?人群|普通(?:成人)?人群|所有成人", re.I)
_GENERAL_PREVENTION_TITLE = re.compile(
    r"\bprimary\s+prevention\s+of\s+(?:atherosclerotic\s+)?cardiovascular\s+disease\b|"
    r"\bcardiovascular\s+disease\s+prevention\s+in\s+clinical\s+practice\b|"
    r"心血管(?:疾病|病)?一级预防", re.I)
_SCOPE_CLAUSE = re.compile(
    r"\b(?:guidelines?|consensus|statement|guidance|document|recommendations?)\b[^.!?。！？]{0,200}"
    r"\b(?:appl(?:y|ies)|intended|restricted|limited|targeted|focus(?:es)?|addresses?|covers?|provide[sd]?\s+guidance)\b"
    r"[^.!?。！？]{0,240}|"
    r"(?:本|该)(?:指南|共识|文件|声明|建议)[^。！？]{0,120}(?:适用|面向|针对|聚焦|限于)[^。！？]{0,160}", re.I)
# These are server-owned synonyms, never generated from patient narrative.
_SEARCH_TERMS = {
    "parkinsons": ("Parkinson disease adults follow up guideline", "成人 帕金森病 随访 管理 指南"),
    "depression": ("depression adults follow up guideline", "成人 抑郁症 随访 管理 指南"),
    "rheumatoid_arthritis": ("rheumatoid arthritis adults follow up guideline", "成人 类风湿关节炎 随访 管理 指南"),
    "colorectal_cancer": ("colorectal cancer adults follow up guideline", "成人 结直肠癌 随访 管理 指南"),
    "cirrhosis": ("cirrhosis adults follow up guideline", "成人 肝硬化 随访 管理 指南"),
    "hyperuricemia": ("asymptomatic hyperuricemia adults follow up guideline", "成人 无症状高尿酸血症 随访 指南"),
    "coronary_heart_disease": ("chronic coronary disease adults follow up guideline", "成人 冠心病 随访 指南"),
    "stroke": ("stroke adults follow up guideline", "成人 脑卒中 随访 康复 指南"),
    "alzheimers": ("Alzheimer disease dementia adults follow up guideline", "成人 阿尔茨海默病 痴呆 随访 指南"),
    "anxiety": ("anxiety disorder adults follow up guideline", "成人 焦虑障碍 随访 指南"),
    "systemic_lupus": ("systemic lupus erythematosus adults follow up guideline", "成人 系统性红斑狼疮 随访 指南"),
    "liver_cancer": ("hepatocellular carcinoma adults follow up guideline", "成人 肝细胞癌 随访 指南"),
    "lung_cancer": ("lung cancer adults follow up guideline", "成人 肺癌 随访 指南"),
    "gastritis": ("chronic gastritis adults clinical guideline", "成人 慢性胃炎 诊断 指南 共识"),
    "ibs": ("irritable bowel syndrome adults clinical guideline", "成人 肠易激综合征 诊疗 指南 共识"),
    "ibd": ("inflammatory bowel disease adults diagnostic clinical guideline", "成人 炎症性肠病 诊断 鉴别 指南 共识"),
    "obesity": ("obesity adults clinical guideline", "成人 肥胖 管理 指南 共识"),
    "diabetes": ("type 2 diabetes adults clinical guideline", "成人 二型糖尿病 诊疗 指南 共识"),
    "fatty_liver": ("metabolic steatotic liver disease MASLD adults clinical guideline", "成人 代谢相关脂肪性肝病 脂肪肝 指南 共识"),
    "hypertension": ("hypertension adults clinical guideline", "成人 高血压 防治 指南 共识"),
    "dyslipidemia": ("dyslipidemia adults clinical guideline", "成人 血脂异常 管理 指南 共识"),
    "cardiovascular": ("cardiovascular prevention adults clinical guideline", "成人 心血管病 一级预防 指南 共识"),
    "intestinal_barrier": ("intestinal permeability systemic inflammation human clinical evidence", "肠道屏障 通透性 慢性炎症 人体 临床 研究"),
    "h_pylori": ("Helicobacter pylori adults clinical guideline", "成人 幽门螺杆菌 感染 治疗 指南 共识"),
    "sleep_apnea": ("obstructive sleep apnea adults clinical guideline", "成人 阻塞性睡眠呼吸暂停 诊疗 指南 共识"),
    "kidney": ("chronic kidney disease adults evaluation clinical guideline", "成人 慢性肾脏病 肾功能 尿白蛋白 评估 指南"),
}


def _text(value, limit):
    if not isinstance(value, str):
        return None
    # Metadata is untrusted data. Keep plain text, not embedded markup.
    value = html.unescape(value[:limit * 4])
    value = re.sub(r"</?(?:p|div|br|li|section|h[1-6])\b[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]*>", "", value)
    return " ".join(value.split())[:limit] or None


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        return None
    if is_retired_marker_name(value):
        return None
    if re.search(r"[\x00-\x1f{}\[\]<>:=@]|https?\b|\d{7,}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
                 r"\b(?:patient|name|userid|sessionid|phone|email)\b|姓名|身份证|电话|患者|受检者", value, re.I):
        return None
    if not re.fullmatch(r"[\w\s()/,.+%'\-]+", value, re.UNICODE):
        return None
    return " ".join(value.split())


def _anchors(query):
    words = re.findall(r"[a-z]{3,}|[\u3400-\u9fff]{2,}", query.lower())
    return list(dict.fromkeys(word for word in words if word not in _STOPWORDS))[:24]


def _normalized(value):
    return re.sub(r"[-‐‑–—]", " ", value.lower())


def _core(value):
    # Long excerpts often mention unrelated comorbidities. Limit the screen to
    # the introductory clinical focus, not an incidental term anywhere in 8k.
    sentences = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z\u3400-\u9fff])", value[:2000])
    return _normalized(" ".join(sentences[:2])[:1000])


def _topic(topic, value):
    return bool(re.search(_TOPICS[topic], value, re.I))


def _query_topics(query):
    normalized = _normalized(query)
    # Specific digestive etiologies must not fall back to their shared symptoms.
    for name in (*_HISTORY_TOPICS, "h_pylori", "gastritis", "ibd", "sleep_apnea", "kidney", "cardiovascular", "ibs", "intestinal_barrier"):
        if _topic(name, normalized):
            return [name]
    named = [name for name in ("obesity", "diabetes", "fatty_liver", "hypertension", "dyslipidemia")
             if _topic(name, normalized)]
    return named


def _search_variants(query):
    topics = _query_topics(query)
    # Mixed-topic legacy queries cannot silently become a search for just one.
    if len(topics) != 1:
        return None, query
    canonical, chinese = _SEARCH_TERMS[topics[0]]
    expansion = canonical if re.search(r"[\u3400-\u9fff]", query) else chinese
    if expansion.casefold() == query.casefold():
        expansion = None
    return expansion, canonical


def _clinical_question_mismatch(query, candidate):
    """Reject clear treatment-only scope for a diagnostic clinical question.

    This is a conservative metadata exclusion, not evidence adjudication. A
    broad disease guideline can still contain diagnosis recommendations; a
    treatment/donor-selection guideline cannot establish disease diagnosis just
    because the abstract lists that disease or mentions participant screening.
    """
    normalized_query = _normalized(query)
    diagnostic = bool(_DIAGNOSTIC_QUESTION.search(normalized_query)) or (
        _topic("calprotectin", normalized_query) and (_topic("ibs", normalized_query) or _topic("diarrhea", normalized_query)))
    if not diagnostic:
        return False
    title = _normalized(candidate.get("title") or "")
    if not (_FMT_TITLE.search(title) or _TREATMENT_TITLE.search(title)):
        return False
    # "Evaluation of FMT" evaluates an intervention, not the patient's disease.
    # Require an explicit disease diagnostic scope in a mixed-scope title, and
    # do not let donor screening masquerade as screening for the disease.
    disease_diagnosis = bool(_DIAGNOSTIC_SCOPE.search(title)) and any(
        _topic(topic, title) for topic in _query_topics(query))
    return not disease_diagnosis or bool(_DONOR_SCOPE.search(title))


def _direct_diagnostic_subject(title, topic):
    """A diagnostic word somewhere in a title is not its diagnostic object."""
    subject = _TOPICS[topic]
    return bool(re.search(r"\b(?:diagnosis|screening)\s+(?:and\s+management\s+)?(?:of|for)\s+(?:the\s+)?(?:" + subject + r")", title, re.I)
                or re.search(r"(?:" + subject + r")(?:综合[征症]|症)?\s*(?:的)?(?:诊断|筛查)", title, re.I))


def _population_scope_exclusion(query, candidate):
    """Do not let a restricted population satisfy a general prevention query.

    A clear title restriction or stated intended population is an exclusion.
    An ambiguous title pairing populations with the target needs an explicit
    general-population scope; missing abstracts do not establish that scope.
    This is retrieval screening only, not verification of recommendations.
    """
    if _query_topics(query) not in (["cardiovascular"], ["dyslipidemia"]):
        return None
    title, normalized_query = _normalized(candidate.get("title") or ""), _normalized(query)
    # The clinical scope may follow the introductory burden statements. Read a
    # bounded metadata excerpt rather than just the first two topic sentences.
    excerpt = " ".join(_normalized(candidate.get(key) or "")[:2400] for key in ("abstract", "summary"))
    scopes = [match.group() for match in _SCOPE_CLAUSE.finditer(excerpt)]
    ambiguous = False
    for population in _RESTRICTED_POPULATIONS.values():
        subject = "(?:" + population + ")"
        if re.search(subject, normalized_query, re.I):
            continue  # A reviewed question may explicitly request this group.
        restriction = (
            r"\b(?:in|for|among|to)\s+(?:only\s+)?(?:(?:adult|older|young)\s+)?"
            r"(?:(?:patients?|people|persons?|individuals?|adults?|women|men)\s+(?:living\s+)?with\s+)?" + subject
            + "|" + subject + r"\s+(?:related|associated|specific|patients?)\b"
            + r"|\b(?:related|secondary)\s+to\s+" + subject
            + "|" + subject + r"\s*(?:相关|合并|伴有?|人群|患者|与)"
        )
        # "Obesity and cardiovascular ..." names obesity as the primary scope,
        # unlike an incidental list of risk factors in a general guideline.
        primary_subject = re.search(r"^(?:\d{4}\s+)?" + subject + r"\s+and\s+", title, re.I)
        if primary_subject or re.search(restriction, title, re.I):
            return "population_scope_mismatch"
        if any(re.search(restriction, scope, re.I) for scope in scopes):
            return "population_scope_mismatch"
        ambiguous |= bool(re.search(subject, title, re.I))
    if ambiguous and not (_GENERAL_PREVENTION_TITLE.search(title) or _GENERAL_POPULATION.search(excerpt)):
        return "population_scope_unconfirmed"
    return None


def _guideline_application_study(candidate):
    """A study of using a guideline is not the guideline itself.

    Do not reject structured METHODS/RESULTS or the words cohort/GRADE in a
    guideline abstract: guideline panels also review cohorts and report methods.
    Empirical methods are used only together with an application-style title.
    """
    title = _normalized(candidate.get("title") or "")
    if _GUIDELINE_APPLICATION_TITLE.search(title):
        return True
    if not _GUIDELINE_MODIFIER.search(title):
        return False
    if _EMPIRICAL_STUDY_TITLE.search(title):
        return True
    excerpt = " ".join(_normalized(candidate.get(key) or "")[:4000] for key in ("abstract", "summary"))
    return bool(_EMPIRICAL_PARTICIPANTS.search(excerpt))


def _metadata_scope_exclusion(query, candidate):
    """Bounded exclusions from reviewed false positives; never a diagnosis.

    Keep comprehensive disease-management guidelines. Reject recognizable
    secondary publications, wrong subtypes and narrowly specialized questions
    rather than promoting every mention of the disease to full coverage.
    """
    title, normalized_query = _normalized(candidate.get("title") or ""), _normalized(query)
    if is_retired_marker_name(title) or is_retired_marker_name(normalized_query):
        return "excluded_biomarker_subject"
    topics = _query_topics(query)
    if any(topic in topics for topic in _HISTORY_TOPICS):
        if not all(_topic(topic, title) for topic in topics):
            return "disease_not_title_subject"
    if _GUIDANCE.search(query) and _SECONDARY_GUIDELINE_TITLE.search(title):
        return "secondary_guideline_publication"
    if _GUIDANCE.search(query) and _guideline_application_study(candidate):
        return "guideline_application_study"
    population_exclusion = _population_scope_exclusion(query, candidate)
    if population_exclusion:
        return population_exclusion
    # Gastric-cancer/dyspepsia guidance may discuss eradication, but that alone
    # does not make it primary guidance on H. pylori infection itself.
    if topics == ["h_pylori"] and not _topic("h_pylori", title):
        return "disease_not_title_subject"
    if topics == ["gastritis"]:
        # A gastric-cancer/HP guideline may mention gastritis as background.
        # Such a mention is not comprehensive chronic-gastritis follow-up, nor
        # evidence that this patient has atrophy, autoimmunity or HP infection.
        if not _topic("gastritis", title):
            return "disease_not_title_subject"
        if any(re.search(subtype, title, re.I) and not re.search(subtype, normalized_query, re.I)
               for subtype in _GASTRITIS_SUBTYPES):
            return "gastritis_subtype_not_requested"
    if _TYPE2.search(normalized_query) and _OTHER_DIABETES_SUBTYPE.search(title) and not _TYPE2.search(title):
        return "disease_subtype_mismatch"
    if _OBSTRUCTIVE_APNEA.search(normalized_query) and _CENTRAL_APNEA.search(title) and not _OBSTRUCTIVE_APNEA.search(title):
        return "disease_subtype_mismatch"
    diagnostic = bool(_DIAGNOSTIC_QUESTION.search(normalized_query))
    if diagnostic:
        if topics == ["kidney"] and _KIDNEY_SPECIALIZED_SCOPE.search(title):
            return "specialized_scope_not_initial_assessment"
        if topics == ["sleep_apnea"] and _OSA_SPECIALIZED_SCOPE.search(title):
            return "specialized_scope_not_initial_assessment"
        if topics == ["obesity"] and _NUTRITION_SCOPE.search(title) and not _direct_diagnostic_subject(title, "obesity"):
            return "nutrition_scope_not_disease_assessment"
    if _clinical_question_mismatch(query, candidate):
        return "clinical_question_mismatch"
    return None


def _matches(query, candidate):
    if _metadata_scope_exclusion(query, candidate):
        return []
    title = _normalized(candidate.get("title") or "")
    core = " ".join(_core(candidate.get(key) or "") for key in ("abstract", "summary"))
    text, normalized_query = title + " " + core, _normalized(query)
    for topic in _HISTORY_TOPICS:
        if _topic(topic, normalized_query):
            return [topic] if _topic(topic, title) else []
    if _topic("h_pylori", normalized_query):
        return ["h_pylori"] if _topic("h_pylori", text) else []
    if _topic("gastritis", normalized_query):
        return ["gastritis"] if _topic("gastritis", title) else []
    # Preserve IBD as a distinct fixed-catalog topic rather than treating its
    # inflammatory disease focus as nonspecific IBS or diarrhea.
    if _topic("ibd", normalized_query):
        return ["ibd"] if _topic("ibd", text) else []
    if _topic("cardiovascular", normalized_query):
        if not _topic("cardiovascular", text):
            return []  # A nonspecific marker alone is not the disease.
        return ["cardiovascular"]
    if _topic("ibs", normalized_query):
        if _topic("ibs", text):
            return ["irritable_bowel_syndrome"]
        if _topic("diarrhea", text) and _topic("calprotectin", text):
            return ["diarrhea", "calprotectin"]
        return []  # A different named syndrome is not an IBS reference.
    if _topic("intestinal_barrier", normalized_query):
        return ["intestinal_barrier"] if _topic("intestinal_barrier", text) else []
    if _topic("kidney", normalized_query):
        # Incidental kidney comorbidity in an unrelated guideline is not a
        # renal evaluation guideline; require the subject in the title.
        return ["kidney"] if _topic("kidney", title) else []
    requested = [name for name in ("obesity", "diabetes", "fatty_liver", "hypertension", "dyslipidemia",
                                   "h_pylori", "sleep_apnea") if _topic(name, normalized_query)]
    if requested:
        return [name for name in requested if _topic(name, text)]
    # Unmapped queries still require a combination, not one common noun from
    # the request. Single-subject queries must match that subject in the title.
    anchors = [word for word in _anchors(query) if word not in {
        "high", "low", "sensitivity", "reactive", "protein", "syndrome", "fecal", "faecal", "outcomes"}]
    matched = [word for word in anchors
               if any(re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text if len(anchors) > 1 else title)
                      for term in _SYNONYMS.get(word, (word,)))]
    return matched if len(matched) >= min(2, len(anchors)) else []


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in {None, 80, 443}):
            return None
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith((".local", ".localhost", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            pass
        # Avoid reflecting signed/tokenized provider links into evidence output.
        if re.search(r"(?:token|key|signature|authorization|password|secret)=", parsed.query, re.I):
            return None
    except ValueError:
        return None
    return value


def _doi(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
    return value if len(value) <= 256 and re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value, re.I) else None


def _candidate(row, *, provider, channel, query, query_id, exclusions=None):
    def excluded(reason):
        if exclusions is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        return None
    if hasattr(row, "model_dump"):
        row = row.model_dump()
    if not isinstance(row, dict):
        return excluded("invalid_record")
    title = _text(row.get("title"), 500)
    if title and _PROTOCOL_TITLE.search(title):
        return excluded("protocol_without_results")
    if title and _PEDIATRIC_TITLE.search(title) and not _ADULT_TITLE.search(title):
        return excluded("pediatric_only")
    if title and _ANIMAL_TITLE.search(title) and not _HUMAN_TITLE.search(title):
        return excluded("animal_only")
    if _GUIDANCE.search(query):
        # EviMed's guide channel and normalized study_types are not original
        # publication-type verification. Only Europe PMC's raw PubType can
        # independently qualify a title lacking a guideline label.
        pubtypes = row.get("_trusted_pubtypes") if provider == "europe_pmc" else None
        trusted_guideline = isinstance(pubtypes, list) and any(
            isinstance(item, str) and item.casefold().strip() == "practice guideline" for item in pubtypes)
        # Official titles sometimes omit the word guideline. Only these exact
        # reviewed NICE pages qualify; a provider's channel/type label cannot.
        official_nice = str(_safe_url(row.get("url")) or "").rstrip("/") in {
            "https://www.nice.org.uk/guidance/" + code + "/chapter/Recommendations"
            for code in ("ng71", "ng222", "ng100", "ng151", "ng236", "ng97", "cg113")
        } | {
            "https://www.nice.org.uk/guidance/ng50/chapter/1-Introduction-and-overview",
            "https://www.nice.org.uk/guidance/ng122/chapter/Management",
        }
        # This reviewed recommendation has no 'guideline' in its official
        # title. Require both identifiers; do not trust a guide-channel label.
        reviewed_recommendation = (
            _doi(row.get("doi")) == "10.1136/ard-2023-224762"
            and str(title or "").casefold() == "eular recommendations for the management of systemic lupus erythematosus: 2023 update")
        if not (title and _GUIDELINE_TITLE.search(title)) and not trusted_guideline and not official_nice and not reviewed_recommendation:
            return excluded("not_a_guideline")
    doi = _doi(row.get("doi"))
    pmid = row.get("pmid")
    pmid = str(pmid) if isinstance(pmid, (str, int)) and not isinstance(pmid, bool) else None
    pmid = pmid if pmid and re.fullmatch(r"[1-9]\d{0,9}", pmid) else None
    url = _safe_url(row.get("url"))
    if not url and pmid:
        url = "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/"
    if not url and doi:
        url = "https://doi.org/" + quote(doi, safe="/")
    if not title or not url:
        return excluded("untraceable_record")
    year = row.get("year")
    if isinstance(year, str) and len(year) == 4 and year.isdigit():
        year = int(year)
    if isinstance(year, bool) or not isinstance(year, int) or not 1800 <= year <= datetime.now(timezone.utc).year + 1:
        year = None
    study_types = row.get("study_types", [])
    if not isinstance(study_types, list):
        study_types = []
    value = {"id": "ev_" + hashlib.sha256((doi or pmid or url).lower().encode()).hexdigest()[:20],
             "title": title, "source": _text(row.get("source"), 300) or provider,
             "year": year, "url": url, "doi": doi, "pmid": pmid,
             "abstract": _text(row.get("abstract"), 8000),
             "summary": _text(row.get("summary") or row.get("evidence_text"), 8000),
             "provider": provider, "channel": channel,
             "studyTypes": [item for raw in study_types[:12] if (item := _text(raw, 100))],
             "queryIds": [query_id], "candidateOnly": True}
    scope_exclusion = _metadata_scope_exclusion(query, value)
    if scope_exclusion:
        return excluded(scope_exclusion)
    matched = _matches(query, value)
    if not matched:
        return excluded("off_topic")
    value["matchedTerms"] = matched
    return value


class GutBarrierEvidence:
    """Retrieve fixed clinical queries, with a process-local concurrency bound.

    ``retrieve(terms: list[str])`` returns an internal audit dict containing
    version/status/provider/queries/candidates/issues/partial. Each candidate has
    candidateOnly=True; no patient score or treatment decision is produced.
    The first ``max_queries`` valid distinct queries receive retrieval slots;
    callers supply priority order. Overflow is audited, not claimed as covered.
    The injected EviMed client remains caller-owned and is not closed here.
    """

    def __init__(self, *, evimed_client=None, transport=None, timeout_seconds=120,
                 request_timeout_seconds=10, concurrency=2, max_queries=12,
                 max_results=36, per_query_results=3):
        for name, value, maximum in (("concurrency", concurrency, 4), ("max_queries", max_queries, 12),
                                    ("max_results", max_results, 40), ("per_query_results", per_query_results, 8)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError("invalid evidence " + name)
        for value in (timeout_seconds, request_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 180:
                raise ValueError("invalid evidence timeout")
        self.evimed_client, self.transport = evimed_client, transport
        self.timeout_seconds, self.request_timeout_seconds = timeout_seconds, request_timeout_seconds
        self.max_queries, self.max_results, self.per_query_results = max_queries, max_results, per_query_results
        self._semaphore = asyncio.Semaphore(concurrency)
        self._public_client = None
        self._connection_limit = concurrency

    async def retrieve(self, terms):
        issues, queries = [], []
        if not isinstance(terms, list):
            return self._result([], [], [{"code": "EVIDENCE_QUERY_INVALID"}], partial=True)
        valid, seen = [], set()
        for index, term in enumerate(terms):
            query = _query(term)
            if not query or not _anchors(query):
                issues.append({"code": "EVIDENCE_QUERY_INVALID", "index": index})
                continue
            if query.lower() not in seen:
                seen.add(query.lower())
                audit = self._query_audit(query, "queued", None, 0)
                queries.append(audit)
                if len(valid) < self.max_queries:
                    valid.append((query, audit))
                else:
                    audit["status"] = "skipped_limit"
        if len(queries) > self.max_queries:
            issues.append({"code": "EVIDENCE_QUERY_LIMIT_REACHED"})
        tasks = [asyncio.create_task(self._retrieve_one(query, audit=audit)) for query, audit in valid]
        buckets = []
        try:
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self.timeout_seconds)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for (query, audit), task in zip(valid, tasks):
                    if task in pending:
                        audit["status"] = "timeout"
                        for attempt in audit["attempts"]:
                            if attempt["status"] in {"queued", "running", "cancelled"}:
                                attempt["status"] = "timeout"
                        issues.append({"code": "EVIDENCE_TIMEOUT", "queryId": audit["id"],
                                       "phase": audit["attempts"][-1].get("phase") if audit["attempts"] else "capacity_wait"})
                    else:
                        result = task.result()
                        buckets.append(result["candidates"])
                        issues.extend(result["issues"])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        deduplicated, dropped = self._fair_merge(buckets, self.max_results)
        if dropped:
            issues.append({"code": "EVIDENCE_RESULT_LIMIT_REACHED"})
        # Audit reflects the final retained set, not pre-dedup/pre-quota counts.
        for audit in queries:
            retained = [row for row in deduplicated if audit["id"] in row["queryIds"]]
            audit["candidateIds"] = [row["id"] for row in retained]
            audit["candidateCount"] = len(retained)
            audit["coveredTopics"] = [topic for topic in audit["requestedTopics"]
                                      if any(self._covers(topic, row["matchedTerms"]) for row in retained)]
            audit["uncoveredTopics"] = [topic for topic in audit["requestedTopics"] if topic not in audit["coveredTopics"]]
            if audit["status"] in {"success", "partial_coverage"}:
                if not retained:
                    audit["status"] = "skipped_result_limit"
                elif audit["uncoveredTopics"]:
                    audit["status"] = "partial_coverage"
                    issues.append({"code": "EVIDENCE_TOPIC_INCOMPLETE", "queryId": audit["id"],
                                   "topics": audit["uncoveredTopics"]})
            audit["provider"] = "+".join(sorted({row["provider"] for row in retained})) or None
        partial = bool(issues) and any(not issue.get("recovered") for issue in issues)
        return self._result(queries, deduplicated, issues, partial=partial)

    @staticmethod
    def _covers(topic, matched):
        return topic in matched or (topic == "ibs" and "irritable_bowel_syndrome" in matched)

    @staticmethod
    def _fair_merge(buckets, limit):
        retained, dropped = {}, False
        # One candidate from each topic/channel before any gets a second slot.
        for index in range(max((len(bucket) for bucket in buckets), default=0)):
            for bucket in buckets:
                if index >= len(bucket):
                    continue
                candidate = bucket[index]
                existing = retained.get(candidate["id"])
                if existing is not None:
                    existing["queryIds"] = list(dict.fromkeys(existing["queryIds"] + candidate["queryIds"]))
                    existing["matchedTerms"] = list(dict.fromkeys(existing["matchedTerms"] + candidate["matchedTerms"]))
                elif len(retained) < limit:
                    retained[candidate["id"]] = {**candidate, "queryIds": list(candidate["queryIds"]),
                                                  "matchedTerms": list(candidate["matchedTerms"])}
                else:
                    dropped = True
        return list(retained.values()), dropped

    @staticmethod
    def _result(queries, candidates, issues, *, partial):
        providers = sorted({item["provider"] for item in candidates})
        return {"version": EVIDENCE_VERSION,
                "status": "partial" if partial else "success" if candidates else "not_requested",
                "provider": "+".join(providers) or None,
                "queries": queries, "candidates": candidates, "issues": issues, "partial": partial}

    @staticmethod
    def _query_audit(query, status, provider, count):
        return {"id": "eq_" + hashlib.sha256(query.lower().encode()).hexdigest()[:16],
                "text": query, "status": status, "provider": provider, "candidateCount": count,
                "candidateIds": [], "acceptedCandidateCount": 0, "requestedTopics": _query_topics(query),
                "coveredTopics": [], "uncoveredTopics": _query_topics(query), "attempts": []}

    async def _retrieve_one(self, query, *, audit=None):
        audit = audit if audit is not None else self._query_audit(query, "queued", None, 0)
        query_id = audit["id"]
        issues, candidates = [], []
        expansion, canonical = _search_variants(query)
        if self.evimed_client is not None and getattr(self.evimed_client, "enabled", True):
            candidates, attempt_issues = await self._primary_attempt(query, query, audit, "primary")
            issues.extend(attempt_issues)
            if not candidates and expansion:
                candidates, attempt_issues = await self._primary_attempt(expansion, query, audit, "expanded")
                if candidates:
                    issues = [{**issue, "recovered": True} for issue in issues]
                issues.extend(attempt_issues)
        else:
            issues.append({"code": "EVIDENCE_PRIMARY_NOT_CONFIGURED", "queryId": query_id})
            audit["attempts"].append({"stage": "primary", "query": query, "provider": "evimed_v2",
                                      "status": "not_configured", "phase": None, "channels": []})
        if not candidates:
            attempt = {"stage": "public", "query": canonical, "provider": "europe_pmc",
                       "status": "queued", "phase": "capacity_wait", "channels": [], "exclusions": {}}
            audit["attempts"].append(attempt)
            try:
                async with self._semaphore:
                    attempt.update(status="running", phase="provider_request")
                    async with asyncio.timeout(self.request_timeout_seconds):
                        candidates = await self._europe_pmc(canonical, query_id=query_id, screening_query=query,
                                                            exclusions=attempt["exclusions"])
                attempt.update(status="success" if candidates else "nohit", candidateCount=len(candidates))
                if not candidates:
                    issues.append({"code": "EVIDENCE_PUBLIC_EMPTY_OR_UNSUITABLE", "queryId": query_id})
                else:
                    issues = [{**issue, "recovered": True} for issue in issues]
            except asyncio.CancelledError:
                attempt["status"] = "cancelled"
                raise
            except Exception as error:
                attempt.update(status="failure", errorType=type(error).__name__)
                issues.append({"code": "EVIDENCE_PUBLIC_UNAVAILABLE", "queryId": query_id,
                               "errorType": type(error).__name__})
        provider = candidates[0]["provider"] if candidates else None
        failure = any(attempt["status"] in {"failure", "partial"} for attempt in audit["attempts"])
        audit.update(status="success" if candidates else "failure" if failure else "nohit", provider=provider,
                     candidateCount=len(candidates), acceptedCandidateCount=len(candidates))
        return {"query": audit, "candidates": candidates, "issues": issues}

    async def _primary_attempt(self, query, screening_query, audit, stage):
        query_id, buckets, issues = audit["id"], [], []
        attempt = {"stage": stage, "query": query, "provider": "evimed_v2", "status": "queued",
                   "phase": "capacity_wait", "channels": []}
        audit["attempts"].append(attempt)
        try:
            async with self._semaphore:
                attempt.update(status="running", phase="provider_request")
                async with asyncio.timeout(self.request_timeout_seconds):
                    outcomes = await self.evimed_client.search_detailed(query, supports=[], channels=("guide", "literature"))
            if not isinstance(outcomes, list) or len(outcomes) > 4:
                raise ValueError("invalid evidence outcomes")
            seen_channels = set()
            for outcome in outcomes:
                channel, status, records = (getattr(outcome, key, None) for key in ("channel", "status", "records"))
                if channel not in {"guide", "literature"} or channel in seen_channels or status not in {"success", "failed", "not_requested"}:
                    raise ValueError("invalid evidence channel")
                seen_channels.add(channel)
                channel_audit = {"channel": channel, "status": status, "candidateCount": 0, "exclusions": {}}
                attempt["channels"].append(channel_audit)
                if status == "not_requested":
                    continue
                if status != "success" or not isinstance(records, list):
                    channel_audit["status"] = "failure"
                    issues.append({"code": "EVIDENCE_CHANNEL_UNAVAILABLE", "queryId": query_id, "channel": channel, "stage": stage})
                    continue
                rows = []
                for row in records[:self.per_query_results * 4]:
                    candidate = _candidate(row, provider="evimed_v2", channel=channel, query=screening_query,
                                           query_id=query_id, exclusions=channel_audit["exclusions"])
                    if candidate is not None:
                        rows.append(candidate)
                rows, _ = self._fair_merge([rows], self.per_query_results)
                buckets.append(rows)
                channel_audit.update(status="success" if rows else "nohit", candidateCount=len(rows))
            candidates, _ = self._fair_merge(buckets, self.per_query_results)
            attempt.update(status="partial" if candidates and issues else "success" if candidates else "failure" if issues else "nohit",
                           candidateCount=len(candidates))
            if not candidates and not issues:
                issues.append({"code": "EVIDENCE_PRIMARY_EMPTY_OR_UNSUITABLE", "queryId": query_id, "stage": stage})
            return candidates, issues
        except asyncio.CancelledError:
            attempt["status"] = "cancelled"
            raise
        except Exception as error:
            attempt.update(status="failure", errorType=type(error).__name__)
            issues.append({"code": "EVIDENCE_PRIMARY_UNAVAILABLE", "queryId": query_id, "stage": stage,
                           "errorType": type(error).__name__})
            return [], issues

    async def _europe_pmc(self, query, *, query_id, screening_query=None, exclusions=None):
        if self._public_client is None or self._public_client.is_closed:
            self._public_client = httpx.AsyncClient(timeout=self.request_timeout_seconds, transport=self.transport,
                follow_redirects=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                limits=httpx.Limits(max_connections=self._connection_limit, max_keepalive_connections=self._connection_limit))
        params = {"query": query, "format": "json", "resultType": "core", "pageSize": self.per_query_results}
        async with self._public_client.stream("GET", EUROPE_PMC_SEARCH_URL, params=params) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("encoded evidence response is not supported")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise ValueError("evidence response exceeds limit")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("evidence response exceeds limit")
                content.extend(chunk)
        value = json.loads(content)
        if not isinstance(value, dict) or not isinstance(value.get("resultList"), dict):
            raise ValueError("invalid Europe PMC response")
        rows = value["resultList"].get("result")
        if not isinstance(rows, list):
            raise ValueError("invalid Europe PMC records")
        candidates = []
        for row in rows[:self.per_query_results * 4]:
            if not isinstance(row, dict) or row.get("isRetracted") in {"Y", True}:
                continue
            pmid = row.get("pmid") or (row.get("id") if row.get("source") == "MED" else None)
            journal_info = row.get("journalInfo") if isinstance(row.get("journalInfo"), dict) else {}
            journal = journal_info.get("journal") if isinstance(journal_info.get("journal"), dict) else {}
            pubtypes = row.get("pubTypeList") if isinstance(row.get("pubTypeList"), dict) else {}
            adapted = {"title": row.get("title"), "doi": row.get("doi"), "pmid": pmid,
                       "year": row.get("pubYear"), "source": journal.get("title") or "Europe PMC",
                       "abstract": row.get("abstractText"), "study_types": pubtypes.get("pubType", []),
                       "_trusted_pubtypes": pubtypes.get("pubType", [])}
            candidate = _candidate(adapted, provider="europe_pmc", channel="literature", query=screening_query or query,
                                   query_id=query_id, exclusions=exclusions)
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= self.per_query_results:
                break
        return candidates

    async def aclose(self):
        if self._public_client is not None and not self._public_client.is_closed:
            await self._public_client.aclose()
