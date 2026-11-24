"""Patient-facing Chinese terminology and prose normalization.

Source labels, retrieval terms and bibliographic titles must remain unchanged.
Only text that is about to be published to the callback/preview should pass
through this module.
"""

from __future__ import annotations

import re

_PATIENT_TERM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:II|Ⅱ|2)\s*型糖尿病", re.IGNORECASE), "2型糖尿病"),
    (re.compile(r"肠易激综合症"), "肠易激综合征"),
    (re.compile(r"炎症性肠道疾病"), "炎症性肠病"),
    (re.compile(r"高血脂"), "高脂血症"),
    (re.compile(r"高尿酸(?!血症)"), "高尿酸血症"),
    # Do not rewrite names such as “帕金森-高热综合征” or duplicate 病.
    (re.compile(r"帕金森(?!病|[-—－]高热)"), "帕金森病"),
)


_LEADING_ORPHAN_CONNECTIVE = re.compile(
    r"^(?:(?:然而|但是|不过|可是|只是)[，,]\s*|(?:然而|但是|不过|可是|只是)\s+)"
)

_PROMOTIONAL_ABSOLUTES = (
    "被证实能预防",
    "已证实能预防",
    "可以预防",
    "能够预防",
    "一定能够",
    "确保改善",
    "保证改善",
)

_GENERIC_DISEASE_SUGGESTIONS = (
    "可以优先关注",
    "相关健康表现",
    "必要时由专业人员进一步评估",
)


def patient_facing_text(value: str | None) -> str | None:
    """Normalize one already-grounded patient-visible text field.

    The transformations are deliberately limited to terminology, punctuation
    and grammar.  They do not add medical claims or change evidence meaning.
    """

    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None
    for pattern, replacement in _PATIENT_TERM_PATTERNS:
        text = pattern.sub(replacement, text)
    # A preceding sentence may have been removed by the evidence/style gate.
    # A contrastive connective left at the start then has no discourse target.
    text = _LEADING_ORPHAN_CONNECTIVE.sub("", text).lstrip()
    text = text.replace(
        "增加有氧运动与抗阻训练相结合的运动方式",
        "将有氧运动与抗阻训练结合",
    )
    text = text.replace(
        "增加有氧运动和抗阻训练相结合的运动方式",
        "将有氧运动与抗阻训练结合",
    )
    text = re.sub(r"\s+([，。；：！？])", r"\1", text)
    text = re.sub(r"([，。；：！？])\1+", r"\1", text)
    return text or None


def patient_facing_name(value: str | None) -> str | None:
    """Normalize a patient-visible disease/health label."""

    return patient_facing_text(value)


def patient_topic_key(value: str | None) -> str:
    """Return a stable comparison key for patient-facing health topics.

    The key is used only to collapse wording variants such as ``血脂管理``
    and ``血脂异常管理``.  It never replaces the displayed topic.
    """

    text = patient_facing_text(value) or ""
    compact = re.sub(r"[\s、，,和与及/／]+", "", text)
    compact = re.sub(
        r"(?:健康|异常|管理|关注|方向|问题|状态|相关)",
        "",
        compact,
    )
    return compact or text.casefold()


def patient_topics_overlap(left: str | None, right: str | None) -> bool:
    """Whether two headings describe the same or a contained health topic."""

    left_key = patient_topic_key(left)
    right_key = patient_topic_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or (
        min(len(left_key), len(right_key)) >= 3
        and (left_key in right_key or right_key in left_key)
    )


def patient_health_topic_domain(value: str | None) -> str:
    """Map wording variants to a broad health-impact direction."""

    text = patient_facing_text(value) or ""
    # Public summaries combine cardiometabolic risk into one direction.  This
    # avoids listing wording variants such as "心血管与代谢健康" and
    # "糖脂代谢" as two separate patient recommendations.
    if (
        "心血管" in text
        or "心脑血管" in text
        or any(
            marker in text
            for marker in ("血脂", "高脂", "糖脂", "血糖", "代谢")
        )
    ):
        return "cardiometabolic"
    if any(marker in text for marker in ("肠道菌群", "微生态")):
        return "gut_microecology"
    if any(marker in text for marker in ("肠道屏障", "免疫调节")):
        return "gut_barrier"
    if any(marker in text for marker in ("排便", "胃肠", "消化道")):
        return "bowel_function"
    if any(marker in text for marker in ("体重", "体成分", "身体组成")):
        return "body_composition"
    if any(marker in text for marker in ("肾脏", "肾功能", "尿酸")):
        return "kidney_uric_acid"
    if "睡眠" in text:
        return "sleep"
    return patient_topic_key(text)


def patient_health_topic_is_generic(value: str | None) -> bool:
    """Reject headings that do not identify a concrete health direction."""

    text = patient_facing_text(value) or ""
    key = patient_topic_key(text)
    return (
        "多系统" in text
        or key in {"综合", "整体", "总体", "健康", "慢性疾病风险"}
        or text in {"综合健康影响", "总体健康风险", "整体健康管理"}
    )


def contains_promotional_absolute(value: str | None) -> bool:
    text = value or ""
    return any(marker in text for marker in _PROMOTIONAL_ABSOLUTES)


def patient_disease_suggestion(value: str | None) -> str | None:
    """Normalize a concrete disease suggestion and reject generic filler."""

    text = patient_facing_text(value)
    if not text or contains_promotional_absolute(text):
        return None
    if any(marker in text for marker in _GENERIC_DISEASE_SUGGESTIONS):
        return None
    return text
