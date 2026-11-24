"""Dated imaging findings that need record reconciliation, never disease scores.

The accepted hospital EXAM ledger is the sole source. Original uncertainty and
dates are quoted for follow-up review; no present diagnosis, malignancy risk or
causal relationship to the barrier panel is inferred.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import re

from .exam_excerpt import exam_result_excerpt


_IMAGING = re.compile(
    r"(?<![A-Za-z])(?:CT|MRI?|PET|US)(?![A-Za-z])|磁共振|超声|彩超|B超|X线|影像|造影|"
    r"\b(?:ultrasound|radiograph|x[- ]ray|computed tomography)\b", re.I)
STRUCTURAL_LESION = re.compile(
    r"占位(?:性病变)?|结节(?:样影|影)?|肿块|肿物|包块|"
    r"\b(?:mass|nodule|space[- ]occupying lesion)s?\b", re.I)
_LESION = re.compile(STRUCTURAL_LESION.pattern + r"|瘤|\b(?:tumou?r|neoplasm)s?\b", re.I)
_UNCERTAINTY = re.compile(
    r"不能排除|不排除|尚未排除|未能排除|有待排除|未排除|不除外|"
    r"\b(?:cannot (?:be )?ruled? out|not ruled out|cannot exclude|not excluded)\b", re.I)
_CONDITIONAL = re.compile(r"如果|假如|假设|若|如(?:有|见|出现|发现)|一旦|\b(?:if|in case of)\b", re.I)
_NON_RESULT = re.compile(
    r"检查目的|适应[证症]|申请诊断|临床诊断|术前诊断|家族史|父亲|母亲|家属|配偶|兄弟|姐妹|"
    r"丈夫|妻子|爱人|伴侣|子女|儿子|女儿|"
    r"宣教|科普|诊断标准|典型症状|治疗方法|"
    r"忽略.{0,12}(?:指令|规则|要求)|系统提示|提示词|(?:请|必须|应当).{0,8}(?:输出|执行)|"
    r"\b(?:family history|father|mother|sibling|spouse|husband|wife|partner|child|son|daughter|"
    r"ignore instructions|system prompt)\b", re.I)
_PREVENTION = re.compile(r"预防|防止|避免|\b(?:prevent|prevention|avoid)\b", re.I)
_NEGATED_PREFIX = re.compile(
    r"(?:无|未见|没有|否认|未发现|未提示|未证实|不支持|不存在|排除|并非|不是)[^，,；;。.!?！？]{0,24}$|"
    r"\b(?:no|not|denies|without|negative for|ruled out)\b[^,;.!?]{0,60}$", re.I)
_LESION_PROPERTY = re.compile(
    r"^\s*(?:的)?(?:较前|明显|进一步|进行性|有|为|呈)*"
    r"(?:增大|变大|长大|扩大|缩小|变化|进展|强化|恶性)|"
    r"^\s*(?:growth|enlargement|progression|enhancement|malignan(?:t|cy))\b", re.I)
_NEGATED_SUFFIX = re.compile(
    # The negated object must be the lesion itself. "未见增大/变化" and
    # "不支持恶性" qualify an existing lesion; neither says it is absent.
    r"^\s*(?:[:：]\s*)?(?:无|否|未见|未发现|不支持|否认|无证据|阴性|未证实|不成立|排除)\s*$|"
    r"^\s*[（(]\s*[-－]\s*[）)]|"
    r"^\s*(?:[:：]\s*)?(?:(?:经|经由)?(?:复查|随访|对照)(?:后|显示|提示|证实)?)?\s*"
    r"(?:已(?:经)?(?:治愈|痊愈|消失|消退|排除|排出|取净|切除)|不存在|不再存在|已无)\s*$|"
    r"^\s*(?:(?:is|was|has|had|been|now)\s+)*"
    r"(?:absent|negative|not present|not confirmed|excluded|resolved|ruled out)\s*$", re.I)
_TEACHING_SUFFIX = re.compile(
    r"^\s*(?:的)?(?:发生机制|病理机制|诊断标准|定义|是指|患者应|患者需|可导致|会导致|时应|时需|标志物|标记物|预防)")
_RESULT_HEADING = re.compile(
    r"(?:^|[\r\n])\s*(?:检查结论|影像结论|检查所见|影像所见|诊断意见|结论|印象|findings|impression)\s*[:：]", re.I)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def imaging_result_sentences(value):
    """Shared attribution/conditional gate for source result sentences."""
    text = exam_result_excerpt(value)
    # Application diagnoses are not imaging results. An explicitly labelled
    # result section can follow them and supplies its own attribution.
    if heading := _RESULT_HEADING.search(text):
        text = text[heading.end():]
    for sentence in re.split(r"[。；;\r\n!?！？]+|(?<!\d)\.(?!\d)", text):
        sentence = sentence.strip()
        if not sentence or _NON_RESULT.search(sentence):
            continue
        if hypothetical := _CONDITIONAL.search(sentence):
            sentence = sentence[:hypothetical.start()].rstrip("，,：: ")
        if sentence:
            yield sentence


def finding_polarity(sentence, match, finding_pattern=_LESION):
    """Classify a mentioned finding, never treat advice as a positive result.

    Callers reconciling anatomy pass individual clauses so a different organ's
    normal finding cannot act as a detached resolution of this finding.
    """
    before, after = sentence[:match.start()], sentence[match.end():]
    prefix = re.split(r"[，,]", before)[-1]
    suffix, *continuation = re.split(r"[，,]", after, maxsplit=1)
    resolution = continuation[0] if continuation and not finding_pattern.search(continuation[0]) else ""
    if _PREVENTION.search(prefix) or _TEACHING_SUFFIX.search(suffix):
        return None
    if ((_NEGATED_PREFIX.search(_UNCERTAINTY.sub("", prefix)) and not _LESION_PROPERTY.search(suffix))
            or _NEGATED_SUFFIX.search(_UNCERTAINTY.sub("", suffix))
            or _NEGATED_SUFFIX.search(_UNCERTAINTY.sub("", resolution))):
        return "negative"
    return "positive"


def _excerpts(value):
    for sentence in imaging_result_sentences(value):
        for match in _LESION.finditer(sentence):
            if finding_polarity(sentence, match) == "positive":
                yield sentence
                break


def clinical_target_day(assessment):
    context = assessment.get("context") if isinstance(assessment, dict) else None
    if not isinstance(context, dict):
        return None
    sample_day, current_day = _day(context.get("sampleDate")), _day(context.get("currentDate"))
    target = sample_day or current_day
    if target is None:
        return None
    # A future or inconsistent sample date must not admit a future source row.
    if current_day is not None:
        target = min(target, current_day)
    return target


def dated_hospital_rows(assessment, *, data_type, imaging_only=False, include_conflicts=False):
    """Yield the shared dated/source-gated ledger without changing its facts."""
    target = clinical_target_day(assessment)
    if target is None:
        return
    rows = assessment["context"].get("clinicalFindings")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if not include_conflicts and (row.get("conflict") or row.get("conflictingResults")
                                     or row.get("status") in {"conflict", "needs_review"}):
            continue
        if row.get("temporalStatus") not in {"current", "historical"}:
            continue
        if str(row.get("dataType") or "").strip().upper() != data_type:
            continue
        name = row.get("itemName")
        if not isinstance(name, str) or (imaging_only and not _IMAGING.search(name)):
            continue
        sources = row.get("source")
        if not isinstance(sources, list) or not any(
                isinstance(source, dict) and source.get("kind") == "hospitalData" for source in sources):
            continue
        observed = _day(row.get("date"))
        if observed is None or observed > target:
            continue
        yield row, observed, target


def finding_record(row, observed, target, excerpt):
    """Retain exact attribution for qualitative follow-up consumers."""
    return {"date": observed.isoformat(), "itemName": row["itemName"], "excerpt": excerpt,
            "temporalStatus": "historical" if observed < target else "current",
            "source": deepcopy([source for source in row["source"]
                                if isinstance(source, dict) and source.get("kind") == "hospitalData"])}


def _reconciled_digestive_reminders(assessment):
    """Reuse the registered anatomy/date policy, without a second rule set.

    The local import avoids initialization recursion: historical_followup uses
    this module's source/negation primitives. Other-organ reminders retain their
    existing behavior; a mixed sentence must not lose an unrelated finding.
    """
    from .historical_followup import historical_followup_findings

    code = "digestive_lesion_followup"
    accepted = historical_followup_findings(assessment).get(code, {}).get("findings", [])
    retained = {(item["date"], item["itemName"], item["excerpt"]) for item in accepted}

    def belongs_to_route(row, excerpt):
        isolated = {"context": {**assessment["context"],
                               "clinicalFindings": [{**row, "examResult": excerpt}]}}
        return code in historical_followup_findings(isolated)

    def superseded(row, excerpt):
        if (row["date"], row["itemName"], excerpt) in retained or not belongs_to_route(row, excerpt):
            return False
        # A source sentence can describe both a bowel lesion and (for example)
        # a lung nodule. Only suppress an entirely reconciled digestive reminder.
        # Histology-only clauses such as "间质瘤可能" remain part of the original
        # bowel excerpt and are not interpreted as another structural lesion.
        for clause in re.split(r"[，,]", excerpt):
            if (STRUCTURAL_LESION.search(clause) and any(_excerpts(clause))
                    and not belongs_to_route(row, clause)):
                return False
        return True

    return superseded


def imaging_attention_findings(assessment):
    """Return attributable past/current lesion excerpts without changing facts."""
    selected = {}
    # Validate context first, including an absent/invalid report date.
    if clinical_target_day(assessment) is None:
        return []
    superseded = _reconciled_digestive_reminders(assessment)
    for row, observed, target in dated_hospital_rows(assessment, data_type="EXAM", imaging_only=True):
        name = row["itemName"]
        for excerpt in _excerpts(row.get("examResult")):
            if superseded(row, excerpt):
                continue
            key = (observed.isoformat(), name, excerpt)
            selected.setdefault(key, {"date": observed.isoformat(), "itemName": name, "excerpt": excerpt,
                                      "temporalStatus": "historical" if observed < target else "current"})
    return sorted(selected.values(), key=lambda item: (item["date"], item["itemName"], item["excerpt"]))


def imaging_attention_block(findings):
    """Quote source findings, keeping follow-up separate from scored directions."""
    lines = [f"{item['date']} {item['itemName']}原文：“{item['excerpt']}”。" for item in findings]
    return {"kind": "callout", "title": "影像检查待核实事项", "tone": "caution",
            "text": "\n".join([*lines, "请核对上述影像发现的后续检查与处理记录，并由医生确认当前情况。"])}


def imaging_attention_summary(findings):
    records = list(dict.fromkeys(f"{item['date']} {item['itemName']}" for item in findings))
    return f"影像记录复核：请核对{'、'.join(records)}中所述影像发现的后续检查与处理记录。"
