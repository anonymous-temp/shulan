"""Conservative holds from attributable hospital examination statements.

This is not a free-text diagnosis extractor. Only explicit condition names in
hospital EXAM findings can suppress a potentially unsuitable intervention.
These internal signals never establish diagnoses, deficiencies or indications.
"""
from __future__ import annotations

from copy import deepcopy
import re


_CONCEPTS = {
    "fiber_hold": r"肠梗阻|肠道?狭窄|肠麻痹|消化道梗阻|\b(?:bowel|intestinal) (?:obstruction|stricture)\b|\bileus\b",
    "kidney_problem": r"肾功能(?:不全|损害|障碍)|肾衰竭|慢性肾病|\b(?:renal|kidney) (?:impairment|failure|dysfunction)\b|\bchronic kidney disease\b",
    "liver_problem": r"肝功能(?:不全|损害|障碍)|肝衰竭|失代偿(?:性)?肝病|肝硬化失代偿|失代偿(?:期|性)肝硬化|\b(?:hepatic|liver) (?:impairment|failure|dysfunction)\b|\bdecompensated (?:liver disease|cirrhosis)\b",
    "immune_compromised": r"免疫抑制|免疫功能低下|免疫缺陷|\bimmunosuppressed\b|\bimmunosuppression\b|\bimmunocompromised\b|\bimmunocompromise\b|\bimmunodeficiency\b",
    "severe_immune_compromise": r"(?:重度|严重)免疫(?:抑制|功能低下|缺陷)|\bseverely immunocompromised\b|\bsevere (?:immunocompromise|immunodeficiency|immunosuppression)\b",
    "activity_hold": r"急性心肌梗死|不稳定(?:型)?心绞痛|失代偿(?:性)?心力衰竭|活动性(?:消化道|胃肠道)出血|\bacute myocardial infarction\b|\bunstable angina\b|\bdecompensated heart failure\b|\bactive gastrointestinal bleeding\b",
    "acute_infection": r"急性(?:细菌性?|病毒性?|真菌性?)?感染|急性肺炎|脓毒症|菌血症|\bacute (?:bacterial |viral |fungal )?infection\b|\bacute pneumonia\b|\bsepsis\b|\bbactera?emia\b",
    "acute_inflammation": r"急性炎症(?:反应)?|急性胰腺炎|\bacute inflammat(?:ion|ory (?:state|response))\b|\bacute pancreatitis\b",
}
_PATTERNS = {code: re.compile(pattern, re.I) for code, pattern in _CONCEPTS.items()}
_ACUTE = {"acute_infection", "acute_inflammation"}
_GUIDANCE_HEADING = re.compile(
    r"术后注意事项|检查后注意事项|注意事项|温馨提示|健康宣教|健康指导|术后须知|出院指导|"
    r"\b(?:post[- ]?procedure instructions|post[- ]?operative instructions|discharge instructions|patient education)\b", re.I)
_CONDITIONAL = re.compile(
    r"如果|如出现|如有|若(?:出现|有|发生|存在)?|一旦|假如|假设|"
    r"\b(?:if|should you|in case of|in the event of)\b", re.I)
_INSTRUCTION = re.compile(
    r"(?:预防|防止|避免|警惕|注意观察|宣教|科普|风险因素|会导致|可导致)|"
    r"\b(?:prevent|prevention|avoid|watch for|risk of|can cause|may cause)\b", re.I)
_UNCERTAIN = re.compile(
    r"可能|考虑|疑似|倾向|待排|待定|不除外|不能排除|不排除|尚未排除|未能排除|有待排除|"
    r"\b(?:possible|possibly|suspected|suspicion|probable|potential|rule out|cannot (?:be )?ruled? out|not ruled out)\b|\?", re.I)
_NEGATION = re.compile(
    r"(?:无|未见|没有|否认|未发现|未提示|未证实|不伴|不支持|未发生|不存在|未患|排除|已排除)[^，,；;。.!?！？]{0,16}$|"
    r"\b(?:no|not|denies|denied|without|negative for|no evidence of|absence of|ruled out)\b[^,;.!?]{0,60}$", re.I)
_RESOLVED = re.compile(
    r"已(?:经)?(?:治愈|痊愈|恢复|缓解|排除)|完全缓解|不再存在|已无|"
    r"\b(?:resolved|cured|recovered|ruled out|in remission|no longer present)\b", re.I)
_PAST = re.compile(r"既往|曾经|病史|过去|\b(?:history of|previous|previously|past|prior)\b", re.I)
_CLAUSE = re.compile(r"[，,]")


def _sentences(text: str):
    """Drop template instructions before examining attributable result prose."""
    text = _GUIDANCE_HEADING.split(text, maxsplit=1)[0]
    for sentence in re.split(r"[。；;\r\n!?！？]+|(?<!\d)\.(?!\d)", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        # Preserve a preceding actual finding, but do not search the subsequent
        # hypothetical advice ("肠梗阻；若出现发热..." / "if symptoms occur...").
        hypothetical = _CONDITIONAL.search(sentence)
        if hypothetical:
            sentence = sentence[:hypothetical.start()].rstrip("，,：: ")
        if sentence:
            yield sentence


def _assertion(sentence: str, match: re.Match) -> tuple[bool, bool]:
    """Return (usable conservative hold, affirmative current-state wording)."""
    before, after = sentence[:match.start()], sentence[match.end():]
    prefix = _CLAUSE.split(before)[-1]
    suffix = _CLAUSE.split(after)[0]
    # A diagnosis may be followed by a short resolution clause separated by a
    # comma. Do not borrow a resolution from another named condition.
    continuation = after.split(",", 1)[1] if "," in after else after.split("，", 1)[1] if "，" in after else ""
    if continuation and not any(pattern.search(continuation) for pattern in _PATTERNS.values()):
        suffix += " " + continuation
    uncertainty = bool(_UNCERTAIN.search(prefix + match.group(0) + suffix))
    # "不能排除" is uncertainty, not an affirmative exclusion.
    definite_prefix = _UNCERTAIN.sub("", prefix)
    if _NEGATION.search(definite_prefix) or _INSTRUCTION.search(prefix):
        return False, False
    if re.search(r"家族史|父亲|母亲|兄弟|姐妹|\b(?:family history|father|mother|sibling)\b", prefix, re.I):
        return False, False
    if re.match(r"\s*(?:的)?(?:风险因素|发生机制|病理机制|定义|是指|可导致|会导致)|\s*(?:时应|时需|患者应|患者需)|\s*(?:can cause|may cause|is defined as)\b", suffix, re.I):
        return False, False
    if _RESOLVED.search(suffix) or _RESOLVED.search(prefix):
        return False, False
    if re.search(r"(?:已)?(?:排除|阴性|未证实|不成立)$", suffix.strip()):
        return False, False
    if re.match(r"\s*(?:不存在|未见|未发现|不支持|否认|无证据)", suffix):
        return False, False
    if re.search(r"\b(?:absent|negative|not present|not confirmed|excluded)\b", suffix, re.I):
        return False, False
    if re.match(r"\s*[（(]\s*[-－]\s*[）)]|\s*[:：]\s*(?:无|未见|未发现|阴性)", suffix):
        return False, False
    # "No obstruction or stricture": negation remains in the same clause;
    # opposing independent clauses should use punctuation and are parsed alone.
    affirmed = not uncertainty and not _PAST.search(prefix + match.group(0) + suffix)
    return True, affirmed


def hospital_safety_signals(findings) -> list[dict]:
    """Extract internal conservative safety holds from eligible EXAM findings.

    A historical condition can trigger caution, not a current acute state.
    Positive acute signals additionally require unambiguous affirmative wording
    in a current record. Non-hospital, conflicting and undated inputs are ignored.
    """
    result = []
    if not isinstance(findings, list):
        return result
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("conflictingResults") or finding.get("conflict"):
            continue
        if finding.get("status") == "conflict" or finding.get("temporalStatus") not in {"current", "historical"}:
            continue
        if not isinstance(finding.get("dataType"), str) or finding["dataType"].strip().upper() != "EXAM":
            continue
        sources = finding.get("source")
        sources = [s for s in sources if isinstance(s, dict) and s.get("kind") == "hospitalData"] if isinstance(sources, list) else []
        text = finding.get("examResult")
        if not sources or not isinstance(text, str):
            continue
        for sentence in _sentences(text):
            for code, pattern in _PATTERNS.items():
                for match in pattern.finditer(sentence):
                    usable, affirmative = _assertion(sentence, match)
                    if not usable or (code in _ACUTE and (finding["temporalStatus"] != "current" or not affirmative)):
                        continue
                    signal = {"code": code, "basis": sentence, "date": finding.get("date"),
                              "temporalStatus": finding["temporalStatus"], "source": deepcopy(sources)}
                    if signal not in result:
                        result.append(signal)
                    break
    return result
