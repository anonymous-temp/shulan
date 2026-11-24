"""Source-qualified facts for a historical gastric examination follow-up.

This deliberately narrow extractor does not diagnose current gastritis or
infer H. pylori, atrophy, symptoms or cancer risk. It consumes the accepted
clinicalFindings ledger, never raw questionnaires or unreviewed search text.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import re

from .assessment import _date as normalize_clinical_date
from .exam_excerpt import exam_result_excerpt


_GASTROSCOPY = re.compile(r"胃镜|上消化道内镜|\b(?:gastroscopy|esophagogastroduodenoscopy|EGD|upper\s+GI\s+endoscopy)\b", re.I)
_GASTRITIS = re.compile(r"慢性\s*非萎缩性\s*胃炎|\bchronic\s+non[-\s]?atrophic\s+gastritis\b", re.I)
_UNUSABLE = re.compile(
    r"可能|考虑|疑似|倾向|待排|待定|不除外|不能排除|不排除|尚未排除|未能排除|有待排除|[?？]|"
    r"如(?:果|有|出现)|若|假如|假设|预防|防止|避免|警惕|宣教|科普|定义|是指|风险因素|"
    r"家族史|父亲|母亲|兄弟|姐妹|家属|配偶|丈夫|妻子|爱人|伴侣|子女|儿子|女儿|"
    r"检查目的|适应[证症]|申请诊断|术前诊断|临床诊断|"
    r"诊断标准|典型症状|治疗方法|"
    r"\b(?:possible|possibly|suspected|probable|rule out|cannot be ruled out|"
    r"if|prevent|prevention|family history|father|mother|sibling|spouse|husband|wife|partner|child|son|daughter)\b", re.I)
_NEGATED_PREFIX = re.compile(
    r"(?:无|未见|没有|否认|未发现|未提示|未证实|不支持|不存在|排除|并非|不是)[^，,；;。.!?！？]{0,24}$|"
    r"\b(?:no|not|denies|without|negative for|ruled out)\b[^,;.!?]{0,60}$", re.I)
_NEGATED_SUFFIX = re.compile(
    r"^\s*(?:[:：]\s*)?(?:无|否)(?=$|[，,；;。.!?！？])|"
    r"^\s*(?:[:：]\s*)?(?:不存在|未见|未发现|不支持|否认|无证据|阴性|未证实|不成立|排除)|"
    r"^\s*[（(]\s*[-－]\s*[）)]|"
    r"已(?:经)?(?:治愈|痊愈|恢复|缓解|排除)|完全缓解|不再存在|已无|"
    r"\b(?:absent|negative|not present|not confirmed|excluded|resolved|cured|in remission)\b", re.I)
_TEACHING_SUFFIX = re.compile(r"^\s*(?:的)?(?:发生机制|病理机制|患者应|患者需|可导致|会导致|时应|时需)")


def _affirmed_excerpt(text):
    """Keep the attributable diagnosis phrase, not instructions or hypotheses."""
    accepted = []
    for sentence in re.split(r"[。；;\r\n]+|(?<!\d)\.(?!\d)", exam_result_excerpt(text)):
        if not _GASTRITIS.search(sentence):
            continue
        # The subject and modality of an uncertain/family/teaching sentence
        # cannot be safely changed merely by splitting one of its commas.
        if _UNUSABLE.search(sentence):
            return None
        for clause in re.split(r"[，,]", sentence):
            match = _GASTRITIS.search(clause)
            if not match:
                continue
            before, after = clause[:match.start()], clause[match.end():]
            if (_NEGATED_PREFIX.search(before) or _NEGATED_SUFFIX.search(after)
                    or _TEACHING_SUFFIX.search(after)):
                return None
            # A detached resolution still qualifies the immediately preceding
            # diagnosis. Do not resurrect it as an active follow-up finding.
            if _NEGATED_SUFFIX.search(sentence[sentence.find(clause) + match.end():].lstrip("，, ")):
                return None
            accepted.append(clause.strip())
    return accepted[0] if accepted else None


def gastritis_findings(assessment):
    """Return explicit dated hospital upper-endoscopy records, without scoring.

    Historical records support history review, not a current disease claim.
    Unknown/future dates, conflicted records and non-hospital sources do not
    enable the direction. Duplicate records retain source provenance.
    """
    context = assessment.get("context") or {}
    target_day = normalize_clinical_date(context.get("sampleDate")) or normalize_clinical_date(context.get("currentDate"))
    if target_day is None:
        return []
    target = date.fromisoformat(target_day)
    rows = context.get("clinicalFindings")
    findings = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("conflict") or row.get("conflictingResults"):
            continue
        if row.get("status") in {"conflict", "needs_review"} or row.get("temporalStatus") not in {"current", "historical"}:
            continue
        if str(row.get("dataType") or "").strip().upper() != "EXAM" or not _GASTROSCOPY.search(str(row.get("itemName") or "")):
            continue
        sources = row.get("source")
        sources = [source for source in sources if isinstance(source, dict) and source.get("kind") == "hospitalData"] if isinstance(sources, list) else []
        if not sources:
            continue
        try:
            observed = date.fromisoformat(str(row.get("date")))
        except (ValueError, TypeError):
            continue
        if observed > target:
            continue
        excerpt = _affirmed_excerpt(row.get("examResult"))
        if not excerpt:
            continue
        key = (observed.isoformat(), excerpt)
        finding = findings.setdefault(key, {
            "code": "documented_nonatrophic_gastritis", "date": observed.isoformat(),
            "itemName": row["itemName"], "excerpt": excerpt,
            "temporalStatus": "historical" if observed < target else "current",
            "source": [], "use": "history_followup_not_current_diagnosis",
        })
        for source in sources:
            if source not in finding["source"]:
                finding["source"].append(deepcopy(source))
    return sorted(findings.values(), key=lambda item: (item["date"], item["excerpt"]))
