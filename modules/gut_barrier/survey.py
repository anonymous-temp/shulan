"""Normalize whitelisted questionnaire answers as attributable self-report.

This adapter does not extract diagnoses, interpret free-text instructions, or
turn questionnaire answers into laboratory findings. Operational metadata and
identity documents are deliberately not copied into report context.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


_TEXT_LIMIT = 2048
_UNKNOWN = {"其他", "其它", "不详", "未知", "不清楚", "未填写", "未提供", "未回答", "不确定", "n/a", "na", "null", "unknown"}
_NONE = {"无", "没有", "否认", "none", "no"}
_FIELD_NONE = {
    "digestion": {"良好"},
    "bowelMovement": {"排便规律"},
    "allergy": {"无过敏", "无过敏史", "没有过敏史"},
    "currentMedication": {"无用药", "无服药", "未服药", "未用药", "没有用药"},
    "specialMedication": {"无特殊用药", "无特殊药物", "未使用"},
    "pastDisease": {"无既往病史", "无病史"},
    "familyDisease": {"无家族史", "无家族病史"},
    "fourHigh": {"无四高"},
}
_LABELS = {
    "diet": "饮食偏好", "digestion": "消化情况", "lifestyle": "生活习惯",
    "skin": "皮肤情况", "sleep": "睡眠情况", "fourHigh": "四高病史（自述）",
    "pastDisease": "既往病史（自述）", "familyDisease": "家族病史（自述）",
    "allergy": "过敏史（自述）", "bowelMovement": "排便情况",
    "currentMedication": "当前用药（自述）", "specialMedication": "特殊用药（自述）",
    "wantImprove": "希望改善的方向",
}


def _issue(issues: list, code: str, field: str) -> None:
    item = {"code": code, "field": f"healthSurvey.{field}", "source": "healthSurvey"}
    if item not in issues:
        issues.append(item)


def _text(value, field: str, issues: list, *, limit: int = _TEXT_LIMIT) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        _issue(issues, "HEALTH_SURVEY_INVALID_FIELD", field)
        return None
    value = value.strip()
    if len(value) > limit or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        _issue(issues, "HEALTH_SURVEY_INVALID_FIELD", field)
        return None
    return value or None


def _answer(value, field: str, issues: list) -> dict:
    raw = _text(value, field, issues)
    result = {"raw": raw, "status": "unknown", "values": [], "complete": False}
    if raw is None:
        return result
    parts = list(dict.fromkeys(part.strip() for part in re.split(r"[┋|；;\r\n]+", raw) if part.strip()))
    known, unknown, absent = [], [], []
    for part in parts:
        normalized = part.casefold()
        # This combined option does not distinguish an unspecified history
        # from no history. Preserve it as unknown, never as a condition.
        if normalized in _UNKNOWN or (field == "fourHigh" and re.fullmatch(r"其他\s*/\s*无", normalized)):
            unknown.append(part)
        elif normalized in _NONE | _FIELD_NONE.get(field, set()):
            absent.append(part)
        else:
            known.append(part)
    if absent and known:
        _issue(issues, "HEALTH_SURVEY_CONTRADICTORY_ANSWER", field)
        # Preserve the positive candidate for conservative allergy/interaction
        # holds. A conflicting "none" must not disable those holds, but the
        # answer remains incomplete and is never presented as confirmed.
        result.update(status="contradictory", values=known, complete=False)
        return result
    if unknown:
        _issue(issues, "HEALTH_SURVEY_UNSPECIFIED_ANSWER", field)
    if known:
        result.update(status="known", values=known, complete=not unknown)
    elif absent and not unknown:
        result.update(status="explicit_none", complete=True)
    return result


def _number(value, field: str, issues: list, *, minimum: str, maximum: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        _issue(issues, "HEALTH_SURVEY_INVALID_NUMBER", field)
        return None
    raw = str(value).strip()
    if len(raw) > 128 or not re.fullmatch(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
        _issue(issues, "HEALTH_SURVEY_INVALID_NUMBER", field)
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        number = None
    if number is None or not number.is_finite() or not Decimal(minimum) <= number <= Decimal(maximum):
        _issue(issues, "HEALTH_SURVEY_INVALID_NUMBER", field)
        return None
    return number


def _shown(answer: dict) -> str:
    raw = answer["raw"]
    if answer["status"] == "contradictory":
        return f"{raw}（自述答案存在矛盾，待核实）"
    if answer["status"] == "explicit_none":
        return f"{raw}（自述）"
    if answer["status"] == "unknown":
        return f"{raw or '未提供'}（具体情况待补充）"
    return f"{raw}（自述{'；另有未说明项目' if not answer['complete'] else ''}）"


def normalize_health_survey(survey, *, evaluation_date=None) -> dict:
    """Return ``context``, safe audit ``issues``, and existing-table ``rows``.

    Answers are unknown, explicitly absent, known, or contradictory; incomplete
    and contradictory answers retain positive safety candidates without
    asserting a confirmed condition or complete history.
    Questionnaire demographics do not gate use of the submitted answers.
    Age and gender remain self-reported metadata, without replacing registration
    demographics or the report name. Submission time is source metadata only;
    neither its format nor its relation to evaluation time gates the answers.
    Physical measurements and derived BMI remain display-only questionnaire rows;
    they are never promoted into the clinical context used by risk calculations.
    Physical ranges are broad input sanity checks, not clinical classifications.
    ``submitTime`` never becomes a laboratory sample date.
    """
    result = {"context": {}, "issues": [], "rows": []}
    context, issues, rows = result["context"], result["issues"], result["rows"]
    if survey is None or survey == {}:
        return result
    if not isinstance(survey, dict):
        _issue(issues, "HEALTH_SURVEY_INVALID", "")
        return result

    survey_age = _number(survey.get("age"), "age", issues, minimum="0", maximum="150")
    if survey_age is not None and survey_age != survey_age.to_integral_value():
        _issue(issues, "HEALTH_SURVEY_INVALID_NUMBER", "age")
        survey_age = None
    survey_sex = _text(survey.get("gender"), "gender", issues, limit=32)
    raw_time = survey.get("submitTime")
    submitted = (raw_time.strip() or None) if isinstance(raw_time, str) else None
    context["healthSurvey"] = {
        "source": "healthSurvey", "sourceType": "self_reported", "submittedAt": submitted,
        "temporalStatus": "date_provided" if submitted else "date_unknown", "age": int(survey_age) if survey_age is not None else None,
        "gender": survey_sex, "answers": {},
    }
    if submitted:
        rows.append(["问卷提交时间", submitted + "（自述资料填写时间）"])

    height = _number(survey.get("heightCm"), "heightCm", issues, minimum="20", maximum="300")
    weight = _number(survey.get("weightKg"), "weightKg", issues, minimum="0.3", maximum="700")
    for key, value, label, unit in (("heightCm", height, "身高", "cm"), ("weightKg", weight, "体重", "kg")):
        if value is not None:
            rows.append([label, f"{value.normalize():f} {unit}（自述）"])
    if height is not None and weight is not None:
        bmi = weight / ((height / Decimal("100")) ** 2)
        if Decimal("1") <= bmi <= Decimal("200"):
            rows.append(["BMI", f"{bmi:.1f} kg/m²（按自述身高、体重计算）"])
        else:
            _issue(issues, "HEALTH_SURVEY_INVALID_BODY_MEASUREMENTS", "heightCm/weightKg")

    answers = context["healthSurvey"]["answers"]
    for field, label in _LABELS.items():
        answer = _answer(survey.get(field), field, issues)
        answers[field] = answer
        if answer["raw"] is not None:
            rows.append([label, _shown(answer)])

    for field, target in (("diet", "diet"), ("lifestyle", "habits"), ("sleep", "sleep"),
                          ("skin", "skinConcerns"), ("familyDisease", "familyHistory"),
                          ("pastDisease", "medicalHistory"), ("fourHigh", "conditions"),
                          ("allergy", "allergies"), ("wantImprove", "improvementGoals")):
        answer = answers[field]
        context[target] = list(answer["values"]) if answer["status"] != "unknown" else None
    symptoms = [value for field in ("digestion", "bowelMovement") for value in answers[field]["values"]]
    if symptoms:
        context["symptoms"] = list(dict.fromkeys(symptoms))
    elif all(answers[field]["status"] == "explicit_none" for field in ("digestion", "bowelMovement")):
        context["symptoms"] = []
    medicines = [value for field in ("currentMedication", "specialMedication") for value in answers[field]["values"]]
    medication_complete = all(answers[field]["complete"] for field in ("currentMedication", "specialMedication"))
    context["medications"] = list(dict.fromkeys(medicines)) if medicines or medication_complete else None
    context["medicationHistoryComplete"] = medication_complete
    context["allergyHistoryComplete"] = answers["allergy"]["complete"]
    return result
