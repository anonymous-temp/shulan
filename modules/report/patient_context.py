"""Build a bounded, factual context for evidence applicability assessment."""

from __future__ import annotations

import re

from modules.report.health_assessment.extraction import extract_patient_evidence_facts
from modules.report.health_assessment.normalization import IndicatorFact, date_rank
from modules.report.ports import ReportProfileSnapshot
from modules.report.profile_snapshot import profile_context_partitions
from modules.report.profile_snapshot import profile_indicator_facts
from modules.report.schemas import (
    ParsedMicrobiomeReport,
    PatientEvidenceContext,
    ReportRequest,
)


def build_patient_evidence_context(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    session_context: dict,
    profile_snapshot: ReportProfileSnapshot | None = None,
) -> PatientEvidenceContext:
    facts, diagnoses, session_text = extract_patient_evidence_facts(
        request,
        parsed,
        session_context,
    )
    # 历史画像只补缺失背景：它使用最低优先级，当前请求与报告来源始终优先。
    # 注意必须在提取之后追加，评分链路另行调用同一提取器，不受影响。
    current_codes = {item.code for item in facts}
    facts = [*facts, *(item for item in profile_indicator_facts(profile_snapshot)
        if item.code not in current_codes)]
    partitions = profile_context_partitions(profile_snapshot)
    height = _latest_fact(facts, "height", unit="cm")
    weight = _latest_fact(facts, "weight", unit="kg")
    waist = _latest_fact(facts, "waist", unit="cm")
    bmi = None
    if (
        height is not None
        and height.numeric_value is not None
        and height.numeric_value > 0
        and weight is not None
        and weight.numeric_value is not None
    ):
        bmi = round(weight.numeric_value / ((height.numeric_value / 100) ** 2), 1)

    age = parsed.subject.age if parsed.subject.age is not None else request.age
    age_band = (
        "pediatric"
        if age is not None and age < 18
        else "adult_18_44"
        if age is not None and age < 45
        else "adult_45_64"
        if age is not None and age < 65
        else "older_adult"
        if age is not None
        else "unknown"
    )
    missing = []
    if height is None:
        missing.append("height")
    if weight is None:
        missing.append("weight")
    if waist is None:
        missing.append("waist")
    if not session_text.strip():
        missing.extend(("symptoms", "bowel_context", "medications", "dietary_context"))

    hospital_text = "\n".join(
        " ".join(
            str(part)
            for part in (
                item.item_name,
                item.exam_result,
                item.test_result_value,
                item.result_status_name,
            )
            if part not in (None, "")
        )
        for item in request.hospital_data
    )
    context_text = f"{session_text}\n{hospital_text}".strip()
    return PatientEvidenceContext(
        age=age,
        age_band=age_band,
        sex=parsed.subject.sex or request.sex,
        height_cm=(height.numeric_value if height is not None else None),
        weight_kg=(weight.numeric_value if weight is not None else None),
        bmi=bmi,
        bmi_group=_bmi_group(bmi),
        waist_cm=(waist.numeric_value if waist is not None else None),
        symptoms=_sentences_for(
            context_text,
            ("腹胀", "腹痛", "恶心", "呕吐", "乏力", "头晕", "失眠", "睡眠"),
        ),
        bowel_context=_sentences_for(
            context_text,
            ("排便", "便秘", "腹泻", "大便", "粪便", "布里斯托"),
        ),
        medications=_sentences_for(
            context_text,
            ("用药", "药物", "抗生素", "抗菌药", "益生菌", "泻药"),
        ),
        dietary_context=_sentences_for(
            context_text,
            ("饮食", "膳食", "高脂", "油腻", "纤维", "蔬菜", "水果"),
        ),
        confirmed_conditions=list(dict.fromkeys(item.name for item in diagnoses)),
        sample_type=parsed.sample.sample_type,
        sample_date=parsed.sample.sample_date or parsed.sample.report_date,
        missing_fields=list(dict.fromkeys(missing)),
        profile_background=partitions.background,
        profile_family_history=partitions.family_history,
        profile_unconfirmed=partitions.unconfirmed,
        profile_read_at=(
            None if profile_snapshot is None else profile_snapshot.read_at
        ),
        profile_version=(
            None if profile_snapshot is None else profile_snapshot.profile_version
        ),
        profile_truncated=partitions.truncated,
        profile_stale=(
            False if profile_snapshot is None else profile_snapshot.stale
        ),
    )


def _latest_fact(
    facts: list[IndicatorFact],
    code: str,
    *,
    unit: str,
) -> IndicatorFact | None:
    candidates = [
        item
        for item in facts
        if item.code == code
        and item.numeric_value is not None
        and (item.unit or "").casefold() == unit.casefold()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (date_rank(item.date), -item.priority, item.order))


def _bmi_group(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 18.5:
        return "underweight"
    if value < 24:
        return "normal"
    if value < 28:
        return "overweight"
    return "obesity"


def _sentences_for(text: str, markers: tuple[str, ...]) -> list[str]:
    if not text:
        return []
    output: list[str] = []
    for sentence in re.split(r"[\n。；;]+", text):
        compact = " ".join(sentence.split()).strip()
        if not compact or not any(marker in compact for marker in markers):
            continue
        # Context ranks evidence applicability only.  Keep it bounded and do
        # not feed full records or identifiers into evidence interpretation.
        output.append(compact[:240])
        if len(output) >= 8:
            break
    return list(dict.fromkeys(output))
