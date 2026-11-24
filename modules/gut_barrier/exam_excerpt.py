"""Separate an examination conclusion from explicitly headed aftercare text.

This is a display/source-boundary helper, not a diagnosis extractor. Negation,
uncertainty, patient wording and units in the result are left intact. The raw
record remains in the assessment/audit, including any removed instructions.
"""
from __future__ import annotations

import re


_AFTERCARE = re.compile(
    r"术后注意事项|检查后注意事项|术后须知|温馨提示|健康宣教|出院指导|"
    r"(?:^|[\r\n])\s*注意事项\s*[:：]|"
    r"\b(?:post[- ]?procedure instructions|post[- ]?operative instructions|"
    r"discharge instructions|patient education)\s*[:：]?", re.I,
)
_RESULT_HEADING = re.compile(
    r"(?:^|[\r\n])\s*(?:检查结论|内镜结论|病理结论|诊断意见|诊断结论|"
    r"检查所见|内镜所见|结论|印象|findings|impression|conclusion)\s*[:：]", re.I,
)


def exam_result_excerpt(value: object) -> str:
    """Keep result sections, excluding only explicitly labelled aftercare.

    A separate result heading following an instruction block resumes result
    text. Unlabelled symptoms, negative findings and clinical recommendations
    are not silently removed. No replacement diagnosis is generated.
    """
    if not isinstance(value, str):
        return ""
    kept, cursor = [], 0
    while cursor < len(value):
        start = _AFTERCARE.search(value, cursor)
        if start is None:
            kept.append(value[cursor:])
            break
        kept.append(value[cursor:start.start()])
        resume = _RESULT_HEADING.search(value, start.end())
        if resume is None:
            break
        cursor = resume.start()
    return "\n".join(part.strip() for part in kept if part.strip()).strip()
