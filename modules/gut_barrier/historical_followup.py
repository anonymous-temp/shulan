"""Anatomy-specific historical review, never a current diagnosis or score.

Only attributed, dated hospital imaging can open either route. Urine records
can add record-reconciliation notes to an existing stone route, not another
diagnosis, a causal link, or another scored disease direction.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .clinical_attention import (
    STRUCTURAL_LESION, _NEGATED_SUFFIX, dated_hospital_rows, finding_polarity,
    finding_record, imaging_result_sentences,
)


FOLLOWUP_REFERENCES = {
    "Q36": {
        "citation": "Endoscopic management of subepithelial lesions including neuroendocrine neoplasms: ESGE Guideline",
        "year": 2022, "publisher": "Endoscopy", "doi": "10.1055/a-1751-5742",
        "url": "https://www.esge.com/assets/downloads/pdfs/guidelines/2022_a-1751-5742.pdf",
        "location": "建议1–2：消化道上皮下病变须明确部位、大小及性质；仅支持既往影像病灶的专科资料复核，不凭CT疑似描述确诊间质瘤，不将胃/食管随访间隔套用于结肠，不提供个体风险数值",
    },
    "Q37": {
        "citation": "EAU Guidelines on Urolithiasis",
        "year": 2026, "publisher": "European Association of Urology", "doi": None,
        "url": "https://uroweb.org/guidelines/urolithiasis/chapter/guidelines",
        "location": "3.3与3.4.10：泌尿结石的检查及肾结石随访需结合大小、部位、增长、症状及梗阻或感染；仅支持历史泌尿结石记录复核，缺少尺寸和现状时不设固定复查间隔，不推断CKD、手术指征或患病概率",
    },
    "Q38": {
        "citation": "Microhematuria: AUA/SUFU Guideline (2020, Amended 2025)",
        "year": 2025, "publisher": "American Urological Association", "doi": None,
        "url": "https://www.auanet.org/documents/Guidelines/PDF/2025%20Guidelines/MH%20Unabridged%20FINAL.pdf",
        "location": "条款1、2、6：尿试纸阳性不能单独定义微血尿，需规范尿镜检；仅支持历史尿检单位、标本、状态冲突及后续检查的资料复核，不把未注明单位的红细胞数当作RBC/HPF，不归因于结石或计算泌尿肿瘤风险",
    },
}

_DIGESTIVE = "digestive_lesion_followup"
_URINARY = "urinary_stone_followup"
_SITE_ALIASES = {
    "esophagus": ("食管",), "stomach/fundus": ("胃底",), "stomach/body": ("胃体",),
    "stomach/antrum": ("胃窦",), "stomach/cardia": ("贲门",), "stomach/pylorus": ("幽门",),
    "stomach": ("胃",),
    "bowel/small/duodenum": ("十二指肠",), "bowel/small/jejunum": ("空肠",),
    "bowel/small/ileum": ("回肠",), "bowel/small": ("小肠",),
    "bowel/colon/hepatic_flexure": ("结肠肝曲", "肠肝曲", "肝曲"),
    "bowel/colon/splenic_flexure": ("结肠脾曲", "肠脾曲", "脾曲"),
    "bowel/colon/ascending": ("升结肠",), "bowel/colon/transverse": ("横结肠",),
    "bowel/colon/descending": ("降结肠",), "bowel/colon/sigmoid": ("乙状结肠",),
    "bowel/colon/cecum": ("盲肠",), "bowel/colon": ("结肠", "大肠"),
    "bowel/rectum": ("直肠",), "bowel": ("肠道",),
    "kidney/left": ("左侧肾脏", "左侧肾", "左肾"),
    "kidney/right": ("右侧肾脏", "右侧肾", "右肾"),
    "kidney/both": ("双侧肾脏", "双侧肾", "双肾", "两肾"),
    "kidney": ("肾脏", "肾盂", "肾"),
    "ureter/left": ("左侧输尿管", "左输尿管"),
    "ureter/right": ("右侧输尿管", "右输尿管"),
    "ureter/both": ("双侧输尿管", "双输尿管", "两侧输尿管"),
    "ureter": ("输尿管",), "bladder": ("膀胱",), "urethra": ("尿道",),
}
_SITE_LOOKUP = {alias: site for site, aliases in _SITE_ALIASES.items() for alias in aliases}
# These organs break inherited location; e.g. a lung nodule following a colon
# clause is not a second colonic lesion. Biliary stones are never urinary stones.
_OTHER_SITES = ("肾上腺", "胆囊", "胆管", "肝脏", "肝", "胆", "胰腺", "胰", "脾脏", "脾", "肺", "甲状腺",
                "乳腺", "子宫", "卵巢", "前列腺", "脑", "骨", "皮肤")
_SITE = re.compile("|".join(re.escape(alias) for alias in sorted(
    [*_SITE_LOOKUP, *_OTHER_SITES], key=len, reverse=True)))
_STONE = re.compile(r"结石|\b(?:stones?|calcul(?:us|i))\b", re.I)
# Structural findings, not histologic labels: excluding an initially suspected
# GIST or malignancy does not establish that the documented nodule disappeared.
_FINDING = re.compile(_STONE.pattern + "|" + STRUCTURAL_LESION.pattern, re.I)
_NORMAL = re.compile(r"^(?:复查(?:后|显示|示)?|随访(?:后|显示|示)?)?\s*"
                     r"(?:未见(?:明显)?异常|无(?:明显)?异常|正常)\s*$")
_RESOLVED_PREFIX = re.compile(r"已(?:消失|消退|排出|取净|切除)(?:的)?[^，,]{0,12}$")
_URINE_PANEL = re.compile(r"^(?:尿(?:常规|比重|液比重|隐血|潜血|红细胞|白细胞|蛋白|胆原)|"
                         r"白细胞酯酶|亚硝酸盐|透明管型|病理管型)")
_URINE_RBC = re.compile(r"^(?:尿(?:液)?红细胞(?:总数|计数|数)?|红细胞(?:总数|计数)|RBC)$", re.I)
_URINE_BLOOD = re.compile(r"^(?:尿(?:液)?)?(?:隐血|潜血)$")
_POSITIVE_BLOOD = re.compile(r"^(?:[1-4]?\+|\+{1,4}|阳性|positive)$", re.I)
_NUMBER = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–—~～至]\s*(\d+(?:\.\d+)?)\s*$")


def _sites(token):
    site = _SITE_LOOKUP.get(token)
    if site is None:
        return []
    return [site.replace("/both", "/left"), site.replace("/both", "/right")] if site.endswith("/both") else [site]


def _is_urinary(site):
    return site.split("/")[0] in {"kidney", "ureter", "bladder", "urethra"}


def _events(row):
    """Yield finding existence/absence tied to its own anatomical scope."""
    for sentence in imaging_result_sentences(row.get("examResult")):
        inherited = []
        for clause in re.split(r"[，,]", sentence):
            mentions = list(_SITE.finditer(clause))
            matches = list(_FINDING.finditer(clause))
            for match in matches:
                preceding = [site for site in mentions if site.end() <= match.start()]
                sites = _sites(preceding[-1].group()) if preceding else ([] if mentions else inherited)
                polarity = finding_polarity(clause, match, _FINDING)
                if _RESOLVED_PREFIX.search(clause[:match.start()]):
                    polarity = "negative"
                if polarity is None:
                    continue
                for site in sites:
                    is_stone = bool(_STONE.fullmatch(match.group()))
                    if _is_urinary(site) != is_stone:
                        continue
                    yield (_URINARY if is_stone else _DIGESTIVE), site, polarity, sentence
            # A same-finding continuation can resolve the preceding lesion, but
            # a named other organ establishes a new scope before interpretation.
            if mentions:
                inherited = _sites(mentions[-1].group())
            remainder = clause[mentions[-1].end():].strip(" ：:") if mentions else clause.strip()
            if not matches and (_NORMAL.fullmatch(remainder) or _NEGATED_SUFFIX.search(remainder)):
                for site in inherited:
                    yield (_URINARY if _is_urinary(site) else _DIGESTIVE), site, "negative", sentence


def _record_reviews(assessment):
    rows = list(dated_hospital_rows(assessment, data_type="INSPECTION", include_conflicts=True))
    # Exact source timestamp, not merely a calendar date, supplies panel context.
    urine_groups = {row.get("rawDate") for row, _, _ in rows
                    if isinstance(row.get("rawDate"), str) and row["rawDate"].strip()
                    and _URINE_PANEL.search(row["itemName"].strip())}
    selected = {}
    normal_dates = {}
    for row, observed, _ in rows:
        name, raw_date = row["itemName"].strip(), row.get("rawDate")
        if not isinstance(raw_date, str) or raw_date not in urine_groups:
            continue
        if row.get("conflict") or row.get("conflictingResults") or row.get("status") in {"conflict", "needs_review"}:
            continue
        raw = str(row.get("value") if row.get("value") is not None else "").strip()
        high = str(row.get("resultStatusName") or "").strip().casefold() in {"偏高", "升高", "高", "h", "high", "↑"}
        interval = _RANGE.fullmatch(str(row.get("referenceRange") or ""))
        kind = ("blood" if _URINE_BLOOD.fullmatch(name) and raw.casefold() in {"-", "阴性", "negative"} else
                "rbc" if _URINE_RBC.fullmatch(name) and interval and _NUMBER.fullmatch(raw)
                and float(interval[1]) <= float(raw) <= float(interval[2]) else None)
        if kind and not high:
            normal_dates[kind] = max(observed, normal_dates.get(kind, observed))
    for row, observed, target in rows:
        name = row["itemName"].strip()
        raw_date = row.get("rawDate")
        if not isinstance(raw_date, str) or raw_date not in urine_groups:
            continue
        raw = str(row.get("value") if row.get("value") is not None else "").strip()
        is_blood = bool(_URINE_BLOOD.fullmatch(name))
        is_rbc = bool(_URINE_RBC.fullmatch(name))
        if not (is_blood or is_rbc):
            continue
        high = str(row.get("resultStatusName") or "").strip().casefold() in {"偏高", "升高", "高", "h", "high", "↑"}
        interval = _RANGE.fullmatch(str(row.get("referenceRange") or ""))
        above_source_range = bool(interval and _NUMBER.fullmatch(raw) and float(raw) > float(interval[2]))
        if not ((is_blood and _POSITIVE_BLOOD.fullmatch(raw)) or (is_rbc and (high or above_source_range))):
            continue
        # A documented newer normal result supersedes the old note; do not
        # present a historical abnormality as if it remained unexplained.
        normal_day = normal_dates.get("blood" if is_blood else "rbc")
        if normal_day is not None and normal_day > observed:
            continue
        reasons = ["urine_record_requires_confirmation", "urine_panel_context_not_specimen_confirmation"]
        if is_rbc and not str(row.get("unit") or "").strip():
            reasons.append("missing_unit")
        normal = row.get("resultStatus") == 0 or str(row.get("resultStatusName") or "").strip().casefold() in {"正常", "normal"}
        if normal:
            reasons.append("result_status_conflict")
        if row.get("conflict") or row.get("conflictingResults") or row.get("status") in {"conflict", "needs_review"}:
            reasons.append("source_result_conflict")
        excerpt = f"{name}：{raw}" + (f" {row['unit']}" if row.get("unit") else "")
        record = finding_record(row, observed, target, excerpt)
        record.update({key: deepcopy(row.get(key)) for key in (
            "rawDate", "value", "unit", "referenceRange", "resultStatus", "resultStatusName")})
        record["reasonCodes"] = reasons
        selected.setdefault((record["date"], name, raw_date, raw), record)
    return sorted(selected.values(), key=lambda row: (row["date"], row["itemName"]))


def historical_followup_findings(assessment):
    """Return only two possible qualitative routes; no scores or diagnoses.

    A later negative covers its own anatomical subtree, not a different organ
    or contralateral site. Same-day contradictory evidence cannot activate a
    finding. Accepted source records remain untouched and keep their dates.
    """
    events = []
    for row, observed, target in dated_hospital_rows(
            assessment, data_type="EXAM", imaging_only=True, include_conflicts=True):
        unavailable = (row.get("conflict") or row.get("conflictingResults")
                       or row.get("status") in {"conflict", "needs_review"})
        candidates = [row, *(row.get("alternatives") or [])] if unavailable else [row]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for code, site, polarity, excerpt in _events(candidate):
                events.append({"code": code, "site": site, "polarity": "unavailable" if unavailable else polarity,
                               "date": observed, "record": finding_record(row, observed, target, excerpt)})
    selected = {}
    for event in events:
        if event["polarity"] != "positive":
            continue
        related = [other for other in events if other["code"] == event["code"]
                   and (other["site"] == event["site"] or
                        (other["polarity"] != "positive" and event["site"].startswith(other["site"] + "/")))]
        latest = max(other["date"] for other in related)
        if event["date"] != latest or any(other["date"] == latest and other["polarity"] != "positive" for other in related):
            continue
        record = event["record"]
        key = (record["date"], record["itemName"], record["excerpt"])
        selected.setdefault(event["code"], {}).setdefault(key, record)
    result = {code: {"findings": sorted(records.values(), key=lambda row: (row["date"], row["itemName"], row["excerpt"])),
                     "recordReviews": []} for code, records in selected.items()}
    if _URINARY in result:
        result[_URINARY]["recordReviews"] = _record_reviews(assessment)
    return result
