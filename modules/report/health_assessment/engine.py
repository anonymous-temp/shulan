"""Deterministic fixed six-system scoring engine."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from modules.report.health_assessment.extraction import extract_assessment_facts
from modules.report.health_assessment.normalization import (
    DiagnosisFact,
    IndicatorFact,
    compact_name,
    date_rank,
    numeric_value,
    parse_reference_range,
)
from modules.report.health_assessment.rules.v1_0 import (
    DIMENSIONS as LEGACY_DIMENSIONS,
)
from modules.report.health_assessment.rules.v1_0 import (
    LEVEL_BANDS,
    RULE_SOURCES,
    STATUS_SCORE_PATTERNS,
)
from modules.report.health_assessment.rules.v2_0 import (
    DEFAULT_FILL_ORDER,
    DIMENSION_WEIGHT,
    DISEASE_RISK_BOUNDARY,
    DISEASE_RULES,
    RISK_LABELS,
    RISK_RELEVANCE,
    RISK_SCORES,
    RULE_VERSION,
    SYSTEM_DIMENSIONS,
    SYSTEM_KEYWORDS,
    SYSTEM_MEANINGS,
)
from modules.report.schemas import (
    AssessmentEvidenceUsed,
    AssessmentRuleBasis,
    AssessmentScoreCap,
    HealthAssessment,
    HealthAssessmentAbnormalIndicator,
    HealthAssessmentDimension,
    HealthAssessmentRelatedDisease,
    ParsedMicrobiomeReport,
    ReportRequest,
)


@dataclass
class _Resolved:
    current: dict[str, IndicatorFact] = field(default_factory=dict)
    histories: dict[str, list[IndicatorFact]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _ScoredFact:
    fact: IndicatorFact
    score: int
    rule_key: str
    boundary: str
    severe: bool = False


@dataclass(frozen=True)
class _DiseaseSignal:
    name: str
    primary_system: str
    secondary_systems: tuple[str, ...]
    risk_level: str
    source: str
    source_priority: int
    order: int


@dataclass(frozen=True)
class _SystemCandidate:
    dimension: HealthAssessmentDimension
    relevance: int
    definition_order: int


def build_health_assessment(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    session_context: dict,
    profile_facts: Sequence[IndicatorFact] = (),
) -> HealthAssessment:
    """Score the six active systems using the existing component rules.

    ``profile_facts`` 是长期画像映射出的最低优先级指标，只用于补齐本次
    报告与会话完全没有的指标。``_resolve_facts`` 先比日期再比优先级，一条
    更新的历史观测会静默压过本次报告，因此在进入仲裁之前就排除掉已有
    指标的画像事实：报告来源保持既有优先级，画像只补缺失背景。
    """

    facts, diagnoses = extract_assessment_facts(request, parsed, session_context)
    facts = [*facts, *_gap_filling_facts(facts, profile_facts)]
    resolved = _resolve_facts(facts)
    legacy_builders: dict[str, Callable[[], HealthAssessmentDimension]] = {
        "gut_microecology": lambda: _gut_microecology(resolved, diagnoses),
        "gut_barrier": lambda: _gut_barrier(resolved, diagnoses),
        "glucose_lipid_metabolism": lambda: _metabolism(
            resolved,
            diagnoses,
            request.sex,
        ),
        "cardiovascular_risk": lambda: _cardiovascular(
            resolved, diagnoses, request.age, request.sex
        ),
        "weight_body_composition": lambda: _body_composition(
            resolved, diagnoses, request.age, request.sex
        ),
        "kidney_uric_acid": lambda: _kidney_uric_acid(
            resolved, diagnoses, request.age, request.sex
        ),
    }
    legacy = {code: builder() for code, builder in legacy_builders.items()}
    signals = _disease_signals(request, parsed, diagnoses)
    candidates = [
        _system_candidate(
            code,
            legacy=legacy,
            signals=signals,
            definition_order=index,
        )
        for index, (code, _name) in enumerate(SYSTEM_DIMENSIONS)
    ]
    dimensions = _select_six_dimensions(candidates)
    available = all(item.score_available for item in dimensions)
    if available:
        overall = round(sum(item.score * item.weight for item in dimensions))
        level, label = _level(overall)
        summary = (
            f"本次健康管理评估指数为{overall}分，整体处于“{label}”状态。"
            "固定评估心脑血管、代谢、消化、免疫、泌尿和神经六个系统。"
        )
    else:
        overall, level, label = 0, "insufficient_data", "暂不评分"
        summary = _partial_assessment_summary(dimensions)
    return HealthAssessment(
        rule_version=RULE_VERSION,
        overall_score=overall,
        overall_score_available=available,
        display_overall_score=str(overall) if available else "--",
        level=level,
        level_label=label,
        summary=summary,
        dimensions=dimensions,
        abnormal_indicators=_abnormal_indicators(
            dimensions,
            resolved,
            sex=request.sex,
        ),
    )


def _partial_assessment_summary(
    dimensions: Sequence[HealthAssessmentDimension],
) -> str:
    """Summarize available dimensions without inventing a total score."""

    available = [item for item in dimensions if item.score_available]
    missing = [item.name for item in dimensions if not item.score_available]
    if available:
        attention = sorted(
            (item for item in available if item.score < 75),
            key=lambda item: item.score,
        )
        if attention:
            focus = "、".join(item.name for item in attention[:2])
            conclusion = (
                f"基于目前已获得的{len(available)}个维度信息，"
                f"本次阶段性评估显示{focus}是当前优先关注方向。"
            )
        else:
            conclusion = (
                f"基于目前已获得的{len(available)}个维度信息，"
                "本次阶段性评估显示已具备评分条件的维度整体相对稳定。"
            )
    else:
        conclusion = (
            "本次阶段性评估显示，当前资料尚不足以判断六个系统的"
            "整体健康水平。"
        )
    missing_text = "、".join(missing)
    return (
        conclusion
        + (f"因{missing_text}相关信息不完整，" if missing_text else "")
        + "暂不显示综合评分；待相关检测或体检信息补充后再更新评估。"
    )


def _disease_signals(
    request: ReportRequest,
    parsed: ParsedMicrobiomeReport,
    diagnoses: list[DiagnosisFact],
) -> list[_DiseaseSignal]:
    signals: list[_DiseaseSignal] = []
    order = 0
    for disease in parsed.disease_results:
        classified = _classify_disease(disease.name)
        if classified is None:
            continue
        canonical, primary, default_risk, secondary = classified
        risk = _risk_from_source_level(disease.source_level) or default_risk
        signals.append(
            _DiseaseSignal(
                name=canonical or disease.name,
                primary_system=primary,
                secondary_systems=secondary,
                risk_level=risk,
                source="菌群报告",
                source_priority=2,
                order=order,
            )
        )
        order += 1

    for diagnosis in diagnoses:
        classified = _classify_disease(diagnosis.name)
        if classified is None:
            continue
        canonical, primary, default_risk, secondary = classified
        signals.append(
            _DiseaseSignal(
                name=canonical or diagnosis.name,
                primary_system=primary,
                secondary_systems=secondary,
                risk_level=(
                    "high"
                    if diagnosis.control_poor or diagnosis.complication
                    else default_risk
                ),
                source="临床资料",
                source_priority=1,
                order=order,
            )
        )
        order += 1

    # General examination reports often state a diagnosis directly without a
    # surrounding "既往史/确诊" marker. Match the audited and keyword-based
    # catalogue conservatively, excluding explicit negative statements.
    for item in request.hospital_data:
        if not item.exam_result:
            continue
        for condition in _conditions_from_clinical_text(
            f"{item.item_name} {item.exam_result}"
        ):
            classified = _classify_disease(condition)
            if classified is None:
                continue
            canonical, primary, default_risk, secondary = classified
            signals.append(
                _DiseaseSignal(
                    name=canonical or condition,
                    primary_system=primary,
                    secondary_systems=secondary,
                    risk_level=default_risk,
                    source="临床资料",
                    source_priority=1,
                    order=order,
                )
            )
            order += 1
    return _dedupe_disease_signals(signals)


def _classify_disease(
    value: str,
) -> tuple[str, str, str, tuple[str, ...]] | None:
    normalized = compact_name(value)
    if not normalized:
        return None
    for canonical, primary, risk, aliases, secondary in DISEASE_RULES:
        if any(compact_name(alias) in normalized for alias in aliases):
            return canonical, primary, risk, tuple(secondary)
    # Unlisted source diseases still enter one of the eight fixed systems.
    # The absence of an audited/source risk grade leaves them black/pending.
    for code in (
        "tumor_risk",
        "mental_system",
        "nervous_system",
        "cardiovascular_system",
        "metabolic_system",
        "urinary_system",
        "digestive_system",
        "immune_system",
    ):
        if any(compact_name(keyword) in normalized for keyword in SYSTEM_KEYWORDS[code]):
            return value.strip(), code, "pending", ()
    return None


def _risk_from_source_level(value: str | None) -> str | None:
    normalized = compact_name(value or "")
    if not normalized:
        return None
    if any(marker in normalized for marker in ("高风险", "重度", "重点关注")):
        return "high"
    if any(marker in normalized for marker in ("中低风险", "较低风险")):
        return "low"
    if any(marker in normalized for marker in ("中风险", "中等风险", "建议关注")):
        return "medium"
    if any(marker in normalized for marker in ("低风险", "轻度")):
        return "low"
    return None


def _conditions_from_clinical_text(text: str) -> list[str]:
    compact = " ".join(text.split())
    if any(marker in compact for marker in ("未见异常", "无明显异常", "否认", "排除")):
        # Split first so an unrelated normal sentence does not hide a later
        # positive diagnosis in the same report cell.
        segments = re.split(r"[。；;\r\n]+", compact)
    else:
        segments = [compact]
    output: list[str] = []
    for segment in segments:
        if not segment or any(
            marker in segment for marker in ("未见", "无明显", "否认", "排除")
        ):
            continue
        for canonical, _primary, _risk, aliases, _secondary in DISEASE_RULES:
            if any(alias.casefold() in segment.casefold() for alias in aliases):
                output.append(canonical)
        # Keep a bounded disease phrase for terms outside the initial image.
        for match in re.finditer(
            r"[\u3400-\u9fffA-Za-z0-9]{1,16}"
            r"(?:癌|恶性肿瘤|综合征|硬化症|结石|糖尿病|高血压|高脂血症|"
            r"高尿酸血症|肥胖症|胃炎|肠炎|肝炎|肾病)",
            segment,
        ):
            if _classify_disease(match.group()) is not None:
                output.append(match.group())
    return list(dict.fromkeys(output))


def _dedupe_disease_signals(items: list[_DiseaseSignal]) -> list[_DiseaseSignal]:
    by_key: dict[tuple[str, str], _DiseaseSignal] = {}
    risk_rank = {"high": 0, "medium": 1, "low": 2, "pending": 3}
    for item in items:
        key = (compact_name(item.name), item.primary_system)
        existing = by_key.get(key)
        if existing is None or (
            item.source_priority,
            risk_rank.get(item.risk_level, 9),
            item.order,
        ) < (
            existing.source_priority,
            risk_rank.get(existing.risk_level, 9),
            existing.order,
        ):
            by_key[key] = item
    return sorted(by_key.values(), key=lambda item: item.order)


_SYSTEM_COMPONENTS = {
    "mental_system": (),
    "immune_system": (),
    "tumor_risk": (),
    "urinary_system": ("kidney_uric_acid",),
    "nervous_system": (),
    "cardiovascular_system": ("cardiovascular_risk",),
    "digestive_system": ("gut_microecology", "gut_barrier"),
    "metabolic_system": ("glucose_lipid_metabolism", "weight_body_composition"),
}


def _system_candidate(
    code: str,
    *,
    legacy: dict[str, HealthAssessmentDimension],
    signals: list[_DiseaseSignal],
    definition_order: int,
) -> _SystemCandidate:
    name = dict(SYSTEM_DIMENSIONS)[code]
    components = [legacy[item] for item in _SYSTEM_COMPONENTS[code]]
    available_components = [item for item in components if item.score_available]
    component_score = (
        round(sum(item.score for item in available_components) / len(available_components))
        if available_components
        else None
    )
    related = [
        (signal, True)
        for signal in signals
        if signal.primary_system == code
    ] + [
        (signal, False)
        for signal in signals
        if code in signal.secondary_systems
    ]
    primary_scored = [
        signal for signal, primary in related
        if primary and signal.risk_level in RISK_SCORES
    ]
    risk_score = _combined_disease_risk_score(primary_scored)
    score_candidates = [
        item for item in (component_score, risk_score) if item is not None
    ]
    score = min(score_candidates) if score_candidates else 0
    score_available = bool(score_candidates)
    level, label = _level(score) if score_available else ("insufficient_data", "暂不评分")
    evidence = _unique_assessment_evidence(
        evidence
        for component in components
        for evidence in component.evidence_used
    )
    evidence = _public_system_evidence(code, evidence)
    disease_risk_evidence = _disease_risk_summary_evidence(code, primary_scored)
    if disease_risk_evidence is not None:
        evidence.append(disease_risk_evidence)
    basis = _unique_assessment_rules(
        rule
        for component in components
        for rule in component.rule_basis
    )
    if primary_scored:
        basis.append(
            AssessmentRuleBasis(
                rule_id="assessment.system-disease-risk.v2",
                indicator="关联疾病风险等级",
                source="六维健康管理疾病风险分层规则",
                version=RULE_VERSION,
                boundary=DISEASE_RISK_BOUNDARY,
            )
        )
    conflicts = list(dict.fromkeys(
        conflict for component in components for conflict in component.conflicts
    ))
    related_models = [
        HealthAssessmentRelatedDisease(
            name=signal.name,
            risk_level=signal.risk_level,  # type: ignore[arg-type]
            risk_label=RISK_LABELS[signal.risk_level],  # type: ignore[arg-type]
            source=signal.source,  # type: ignore[arg-type]
            primary=primary,
        )
        for signal, primary in sorted(
            related,
            key=lambda pair: (
                0 if pair[1] else 1,
                {"high": 0, "medium": 1, "low": 2, "pending": 3}[pair[0].risk_level],
                pair[0].order,
            ),
        )
    ]
    relevance = _system_relevance(
        related,
        component_score=component_score,
    )
    if score_available:
        explanation = _system_attention_explanation(
            code,
            level=level,
            related=related_models,
            evidence=evidence,
        )
        missing: list[str] = []
    else:
        explanation = f"当前资料不足，暂不能评估{name}。"
        missing = list(dict.fromkeys(
            reason
            for component in components
            for reason in component.missing_evidence
        )) or ["可用于该系统评分的明确风险等级或检查指标"]
    applied_caps = [
        component.score_cap
        for component in components
        if component.score_cap.applied
    ]
    score_cap = min(applied_caps, key=lambda item: item.maximum) if applied_caps else AssessmentScoreCap()
    dimension = HealthAssessmentDimension(
        code=code,  # type: ignore[arg-type]
        name=name,
        weight=DIMENSION_WEIGHT,
        score=score,
        score_available=score_available,
        display_score=str(score) if score_available else "--",
        level=level,  # type: ignore[arg-type]
        level_label=label,
        confidence=(
            "high"
            if any(signal.source == "临床资料" for signal, _primary in related)
            else "moderate"
            if related or evidence
            else "unavailable"
        ),
        evidence_used=evidence,
        rule_basis=basis,
        missing_evidence=missing,
        conflicts=conflicts,
        trend_adjustment=min(
            (item.trend_adjustment for item in available_components),
            default=0,
        ),
        score_cap=score_cap,
        related_diseases=related_models,
        explanation=explanation,
    )
    return _SystemCandidate(dimension, relevance, definition_order)


def _combined_disease_risk_score(signals: list[_DiseaseSignal]) -> int | None:
    if not signals:
        return None
    scores = [RISK_SCORES[item.risk_level] for item in signals]
    score = min(scores)
    worst = min(signals, key=lambda item: RISK_SCORES[item.risk_level])
    for signal in signals:
        if signal is worst:
            continue
        score -= {"high": 8, "medium": 5, "low": 2}[signal.risk_level]
    return max(20, score)


def _disease_risk_summary_evidence(
    code: str,
    signals: list[_DiseaseSignal],
) -> AssessmentEvidenceUsed | None:
    """Expose the disease-risk input used by disease-only system scores.

    The related-disease chips show item-level detail, while this one concise
    derived input explains why a numerical system score exists.  Without it,
    mental, immune, tumour and nervous cards could display a score but no
    reference indicator.
    """

    if not signals:
        return None
    counts = {
        risk: sum(item.risk_level == risk for item in signals)
        for risk in ("high", "medium", "low")
    }
    summary = "、".join(
        f"{RISK_LABELS[risk]}{count}项"
        for risk, count in counts.items()
        if count
    )
    worst = min(signals, key=lambda item: RISK_SCORES[item.risk_level])
    sources = list(dict.fromkeys(item.source for item in signals))
    return AssessmentEvidenceUsed(
        indicator_code=f"disease_risk_summary:{code}",
        item="关联疾病风险",
        value=summary,
        source="、".join(sources),
        source_type="derived",
        status=RISK_LABELS[worst.risk_level],
    )


def _system_relevance(
    related: list[tuple[_DiseaseSignal, bool]],
    *,
    component_score: int | None,
) -> int:
    score = 0
    for signal, primary in related:
        if not primary:
            # Secondary classification is explanatory context only. It must
            # not crowd a different system with a primary patient match out
            # of the six public slots or deduct the same disease twice.
            continue
        value = RISK_RELEVANCE[signal.risk_level]
        if signal.source == "临床资料":
            value += 30
        score += value
    if component_score is not None and component_score < 85:
        score += 30 + (85 - component_score)
    return score


def _select_six_dimensions(
    candidates: list[_SystemCandidate],
) -> list[HealthAssessmentDimension]:
    by_code = {item.dimension.code: item for item in candidates}
    selected = sorted(
        (item for item in candidates if item.relevance > 0),
        key=lambda item: (-item.relevance, item.definition_order),
    )[:6]
    selected_codes = {item.dimension.code for item in selected}
    for code in DEFAULT_FILL_ORDER:
        if len(selected) >= 6:
            break
        if code not in selected_codes:
            selected.append(by_code[code])
            selected_codes.add(code)
    if len(selected) < 6:
        for item in candidates:
            if item.dimension.code in selected_codes:
                continue
            selected.append(item)
            selected_codes.add(item.dimension.code)
            if len(selected) == 6:
                break
    return [item.dimension for item in selected]


def _unique_assessment_evidence(
    items: Iterable[AssessmentEvidenceUsed],
) -> list[AssessmentEvidenceUsed]:
    output: list[AssessmentEvidenceUsed] = []
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    for item in items:
        key = (item.indicator_code, item.value, item.unit, item.date)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _public_system_evidence(
    code: str,
    items: list[AssessmentEvidenceUsed],
) -> list[AssessmentEvidenceUsed]:
    """Remove non-informative public inputs without changing scoring.

    A zero-percent microbiome entry is still available to the deterministic
    component scorer, but it does not help a patient understand the digestive
    system card and can create a long list of absent taxa.  Filter it only
    after the system score has been calculated so the public JSON and HTML
    stay concise without changing the result.
    """

    if code != "digestive_system":
        return items
    microbiome_prefixes = (
        "microbiome:",
        "microbe_composition:",
        "barrier_core:",
        "barrier_aux:",
    )
    return [
        item
        for item in items
        if not (
            item.indicator_code.startswith(microbiome_prefixes)
            and "%" in f"{item.value or ''}{item.unit or ''}".replace("％", "%")
            and numeric_value(item.value) == 0
        )
    ]


def _unique_assessment_rules(
    items: Iterable[AssessmentRuleBasis],
) -> list[AssessmentRuleBasis]:
    output: list[AssessmentRuleBasis] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.rule_id, item.boundary)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _system_attention_explanation(
    code: str,
    *,
    level: str,
    related: list[HealthAssessmentRelatedDisease],
    evidence: list[AssessmentEvidenceUsed],
) -> str:
    """Build a specific patient-facing explanation from scored inputs."""

    action = {
        "mental_system": (
            "建议观察情绪、睡眠和日常功能变化，明显影响生活时及时接受"
            "专业评估。"
        ),
        "immune_system": (
            "建议关注持续炎症表现、关节及其他免疫相关症状，并结合已有"
            "检查进行评估。"
        ),
        "tumor_risk": (
            "建议结合年龄、既往筛查结果和持续异常表现，由医生判断后续"
            "筛查重点。"
        ),
        "urinary_system": (
            "建议观察排尿和腰腹部不适，并结合肾功能及尿酸变化评估泌尿"
            "系统健康。"
        ),
        "nervous_system": (
            "建议观察记忆、运动协调和感觉变化，相关表现持续时接受神经"
            "专科评估。"
        ),
        "cardiovascular_system": (
            "建议结合血压、血脂及胸闷、胸痛或活动耐量变化，持续评估"
            "心脑血管健康。"
        ),
        "digestive_system": (
            "建议观察腹痛、腹胀、排便及肝胆相关检查变化，并结合已有检查"
            "评估消化道、肝胆及肠道微生态相关健康风险。"
        ),
        "metabolic_system": (
            "建议结合体重、血糖、血脂和尿酸变化，持续评估代谢健康。"
        ),
    }[code]
    primary = [item for item in related if item.primary]
    risk_groups = {
        risk: [item.name for item in primary if item.risk_level == risk]
        for risk in ("high", "medium", "low")
    }
    risk_parts = []
    for risk in ("high", "medium", "low"):
        names = risk_groups[risk]
        if not names:
            continue
        displayed = "、".join(names[:2])
        if len(names) > 2:
            displayed += "等"
        risk_parts.append(f"{displayed}为{RISK_LABELS[risk]}关注项")
    objective = [
        item
        for item in evidence
        if not item.indicator_code.startswith("disease_risk_summary:")
    ]
    objective_parts = []
    for item in objective[:2]:
        detail = item.item
        if item.status:
            detail += item.status
        objective_parts.append(detail)

    if objective_parts and risk_parts:
        basis = "本次评分主要参考" + "、".join(objective_parts)
        basis += "；同时，" + "，".join(risk_parts)
    elif risk_parts:
        basis = "本次关联信息中，" + "，".join(risk_parts)
    elif objective_parts:
        basis = "本次评分主要参考" + "、".join(objective_parts)
    else:
        basis = SYSTEM_MEANINGS[code]
    if level == "priority_attention":
        action = action.replace("建议", "建议优先", 1)
    return f"{basis}；{action}"


def _system_attention_meaning(code: str, level: str) -> str:
    """Legacy component fallback used before eight-system aggregation."""

    meaning = SYSTEM_MEANINGS[code]
    if level == "good":
        return f"{meaning}目前相对稳定。"
    if level == "priority_attention":
        return f"{meaning}需要优先关注。"
    return f"{meaning}需要持续管理。"


def _gap_filling_facts(
    existing: Sequence[IndicatorFact],
    candidates: Sequence[IndicatorFact],
) -> list[IndicatorFact]:
    """只保留本次报告与会话完全没有的指标，绝不覆盖已有来源。"""
    if not candidates:
        return []
    taken = {fact.code for fact in existing}
    return [fact for fact in candidates if fact.code not in taken]


def _resolve_facts(facts: Iterable[IndicatorFact]) -> _Resolved:
    by_code: dict[str, list[IndicatorFact]] = {}
    for fact in facts:
        by_code.setdefault(fact.code, []).append(fact)
    resolved = _Resolved()
    for code, items in by_code.items():
        dated = [item for item in items if date_rank(item.date)]
        if dated:
            latest_rank = max(date_rank(item.date) for item in dated)
            latest_date = [
                item for item in dated if date_rank(item.date) == latest_rank
            ]
        else:
            latest_date = list(items)
        best_priority = min(item.priority for item in latest_date)
        finalists = [item for item in latest_date if item.priority == best_priority]
        value_signatures = {
            (
                "numeric",
                round(item.numeric_value, 8),
            )
            if item.numeric_value is not None
            else ("text", compact_name(item.raw_value or ""))
            for item in finalists
        }
        explicit_units = {
            compact_name(item.unit or "") for item in finalists if item.unit
        }
        if len(value_signatures) > 1 or len(explicit_units) > 1:
            resolved.conflicts[code] = [
                f"{item.source}：{_display_value(item)}" for item in finalists
            ]
            resolved.histories[code] = sorted(items, key=_fact_sort_key, reverse=True)
            continue
        current = max(finalists, key=_fact_information_rank)
        resolved.current[code] = current
        resolved.histories[code] = sorted(items, key=_fact_sort_key, reverse=True)
    return resolved


def _fact_information_rank(item: IndicatorFact) -> tuple[int, int, int, int, int]:
    """Prefer the richer duplicate when date, priority and value are equal."""

    return (
        bool(item.status),
        bool(item.reference_range),
        bool(item.unit),
        bool(item.source),
        item.order,
    )


def _fact_sort_key(item: IndicatorFact) -> tuple[int, int, int]:
    return date_rank(item.date), -item.priority, item.order


def _gut_microecology(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
) -> HealthAssessmentDimension:
    source_total = resolved.current.get("microbiome:gut_overall_score")
    overall = _score_prefixes(
        resolved,
        (
            "microbiome:gmhi",
            "microbiome:gmwi2",
            "microbiome:diversity",
            "microbiome:species_count",
            "microbiome:be_ratio",
        ),
        rule_key="microbiome_provider",
    )
    composition = _score_prefixes(
        resolved,
        ("microbe_composition:", "microbiome:pathogenic_potential"),
        rule_key="microbiome_provider",
    )
    supporting = _score_prefixes(
        resolved,
        (
            "microbiome:dietary_fiber_need",
            "microbiome:biofilm_capacity",
            "microbiome:oxidative_stress_tolerance",
        ),
        rule_key="microbiome_provider",
    )
    if (
        source_total is not None
        and source_total.numeric_value is not None
        and 0 < source_total.numeric_value <= 100
    ):
        source_score = int(round(source_total.numeric_value))
        primary = _ScoredFact(
            source_total,
            source_score,
            "microbiome_provider",
            "直接采用原报告0–100分肠道总评分",
            source_score <= 30,
        )
        aggregate_composition = [
            item
            for item in composition
            if item.fact.code == "microbiome:pathogenic_potential"
        ]
        explanatory = _unique_scored_facts(
            [*overall, *aggregate_composition, *supporting]
        )
        conflicts = _conflicts_for(
            resolved,
            ("microbiome:", "microbe_composition:"),
        )
        dimension = _available_dimension(
            "gut_microecology",
            [(source_score, 1.0)],
            [primary],
            resolved,
            diagnoses,
            conflicts,
        )
        all_evidence = _unique_scored_facts([primary, *explanatory])
        dimension.evidence_used = [_evidence(item.fact) for item in all_evidence]
        dimension.rule_basis = [
            _rule_basis(item) for item in _unique_rules([primary, *explanatory])
        ]
        dimension.confidence = (
            "high"
            if len(dimension.evidence_used) >= 3 and not conflicts
            else "moderate"
        )
        dimension.explanation = _dimension_attention_meaning(
            dimension.code,
            dimension.level,
        )
        return dimension
    missing = []
    if not overall:
        missing.append("至少1项带判定等级的整体菌群指标")
    if not composition:
        missing.append("至少1项带判定等级的菌群组成或病原潜能指标")
    conflicts = _conflicts_for(resolved, ("microbiome:", "microbe_composition:"))
    if missing:
        return _unavailable(
            "gut_microecology",
            missing,
            conflicts=conflicts,
            facts=[*overall, *composition, *supporting],
        )
    dimension = _available_dimension(
        "gut_microecology",
        [(_average(overall), 0.60), (_average(composition), 0.40)],
        [*overall, *composition],
        resolved,
        diagnoses,
        conflicts,
    )
    if supporting:
        all_evidence = _unique_scored_facts([*overall, *composition, *supporting])
        dimension.evidence_used = [_evidence(item.fact) for item in all_evidence]
        dimension.rule_basis = [
            _rule_basis(item)
            for item in _unique_rules([*overall, *composition, *supporting])
        ]
    return dimension


def _gut_barrier(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
) -> HealthAssessmentDimension:
    core = _score_prefixes(resolved, ("barrier_core:",), rule_key="microbiome_provider")
    auxiliary = _score_prefixes(
        resolved, ("barrier_aux:",), rule_key="microbiome_provider"
    )
    missing = []
    if not core:
        missing.append("屏障功能、抗炎/免疫能力或肠黏膜炎症核心指标")
    if not auxiliary:
        missing.append("短链脂肪酸或其他肠屏障辅助指标")
    conflicts = _conflicts_for(resolved, ("barrier_core:", "barrier_aux:"))
    if missing:
        return _unavailable(
            "gut_barrier",
            missing,
            conflicts=conflicts,
            facts=[*core, *auxiliary],
        )
    return _available_dimension(
        "gut_barrier",
        [(_average(core), 0.70), (_average(auxiliary), 0.30)],
        [*core, *auxiliary],
        resolved,
        diagnoses,
        conflicts,
    )


def _metabolism(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
    sex: str | None,
) -> HealthAssessmentDimension:
    sugar = _score_codes(resolved, ("fasting_glucose", "hba1c", "ogtt_2h"))
    lipid = _score_codes(
        resolved,
        ("ldl_c", "non_hdl_c", "triglyceride", "total_cholesterol", "hdl_c"),
        sex=sex,
    )
    missing = []
    if not sugar:
        missing.append("空腹血糖、糖化血红蛋白或OGTT 2小时血糖中的至少1项")
    if not lipid:
        missing.append("LDL-C、非HDL-C或完整血脂结果中的至少1项")
    conflicts = _conflicts_for(resolved, tuple(code for code in _METABOLIC_CODES))
    if missing:
        return _unavailable(
            "glucose_lipid_metabolism",
            missing,
            conflicts=conflicts,
            facts=[*sugar, *lipid],
        )
    return _available_dimension(
        "glucose_lipid_metabolism",
        [(_average(sugar), 0.50), (_average(lipid), 0.50)],
        [*sugar, *lipid],
        resolved,
        diagnoses,
        conflicts,
    )


def _cardiovascular(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
    age: int | None,
    sex: str | None,
) -> HealthAssessmentDimension:
    bp = _blood_pressure_scores(resolved)
    background = _score_codes(resolved, ("ldl_c", "non_hdl_c", "smoking"))
    if any(item.code in {"diabetes", "cardiovascular_disease"} for item in diagnoses):
        diagnosis = next(
            item
            for item in diagnoses
            if item.code in {"diabetes", "cardiovascular_disease"}
        )
        background.append(
            _ScoredFact(
                _diagnosis_indicator(diagnosis),
                50 if diagnosis.control_poor or diagnosis.complication else 70,
                "blood_pressure",
                "已确认的心血管或代谢疾病背景",
                diagnosis.control_poor or diagnosis.complication,
            )
        )
    missing = []
    if age is None:
        missing.append("年龄")
    if sex not in {"男", "女"}:
        missing.append("性别")
    if len(bp) < 2:
        missing.append("收缩压和舒张压")
    if not background:
        missing.append("血脂、吸烟状态、糖尿病或心血管病史中的至少1项")
    conflicts = _conflicts_for(
        resolved, ("systolic_bp", "diastolic_bp", "ldl_c", "non_hdl_c", "smoking")
    )
    if missing:
        return _unavailable(
            "cardiovascular_risk",
            missing,
            conflicts=conflicts,
            facts=[*bp, *background],
        )
    return _available_dimension(
        "cardiovascular_risk",
        [(_average(bp), 0.45), (_average(background), 0.55)],
        [*bp, *background],
        resolved,
        diagnoses,
        conflicts,
    )


def _body_composition(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
    age: int | None,
    sex: str | None,
) -> HealthAssessmentDimension:
    if age is not None and age < 18:
        return _unavailable(
            "weight_body_composition",
            ["当前版本尚无适用于未成年人的BMI年龄-性别规则"],
            level="rule_not_applicable",
        )
    height = resolved.current.get("height")
    weight = resolved.current.get("weight")
    composition = _score_codes(
        resolved, ("waist", "body_fat", "visceral_fat", "skeletal_muscle"), sex=sex
    )
    missing = []
    if (
        not height
        or height.numeric_value is None
        or compact_name(height.unit or "") != "cm"
    ):
        missing.append("身高")
    if not weight or weight.numeric_value is None:
        missing.append("体重")
    elif compact_name(weight.unit or "") != "kg":
        missing.append("带明确单位的体重")
    if not composition:
        missing.append("腰围、体脂率、内脏脂肪或骨骼肌量中的至少1项")
    conflicts = _conflicts_for(
        resolved,
        ("height", "weight", "waist", "body_fat", "visceral_fat", "skeletal_muscle"),
    )
    if missing:
        return _unavailable(
            "weight_body_composition",
            missing,
            conflicts=conflicts,
            facts=composition,
        )
    assert height is not None and height.numeric_value is not None
    assert weight is not None and weight.numeric_value is not None
    bmi = weight.numeric_value / ((height.numeric_value / 100) ** 2)
    bmi_fact = IndicatorFact(
        code="bmi",
        name="BMI",
        raw_value=f"{bmi:.1f}",
        numeric_value=bmi,
        unit="kg/m²",
        date=max((height.date, weight.date), key=lambda value: date_rank(value)),
        source="由身高和体重计算",
        source_type="derived",
        priority=5,
        order=0,
    )
    bmi_score = _score_bmi(bmi_fact)
    return _available_dimension(
        "weight_body_composition",
        [(bmi_score.score, 0.50), (_average(composition), 0.50)],
        [bmi_score, *composition],
        resolved,
        diagnoses,
        conflicts,
    )


def _kidney_uric_acid(
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
    age: int | None,
    sex: str | None,
) -> HealthAssessmentDimension:
    kidney = _score_codes(resolved, ("egfr", "uacr"), sex=sex)
    creatinine = resolved.current.get("creatinine")
    if not any(item.fact.code == "egfr" for item in kidney) and creatinine:
        if age is not None and age >= 18 and sex in {"男", "女"}:
            derived = _derived_egfr(creatinine, age, sex)
            if derived:
                kidney.insert(0, _score_egfr(derived))
    uric = _score_codes(resolved, ("uric_acid",), sex=sex)
    missing = []
    if not kidney:
        if creatinine and (age is None or sex not in {"男", "女"}):
            missing.append("用于计算eGFR的年龄和性别，或直接提供eGFR")
        elif creatinine and age is not None and age < 18:
            missing.append("未成年人可直接使用的eGFR结果")
        elif creatinine and not _is_micromoles_per_litre(creatinine.unit):
            missing.append("带明确单位的血肌酐，或直接提供eGFR")
        else:
            missing.append("eGFR，或可配合年龄和性别计算eGFR的血肌酐")
    if not uric:
        missing.append("血尿酸")
    conflicts = _conflicts_for(resolved, ("egfr", "uacr", "creatinine", "uric_acid"))
    if missing:
        return _unavailable(
            "kidney_uric_acid",
            missing,
            conflicts=conflicts,
            facts=[*kidney, *uric],
        )
    return _available_dimension(
        "kidney_uric_acid",
        [(_average(kidney), 0.60), (_average(uric), 0.40)],
        [*kidney, *uric],
        resolved,
        diagnoses,
        conflicts,
    )


def _available_dimension(
    code: str,
    groups: list[tuple[float, float]],
    facts: list[_ScoredFact],
    resolved: _Resolved,
    diagnoses: list[DiagnosisFact],
    conflicts: list[str],
) -> HealthAssessmentDimension:
    raw_score = round(sum(score * weight for score, weight in groups))
    trend = _trend_adjustment(facts, resolved)
    score = max(0, raw_score + trend)
    cap = _score_cap(code, facts, diagnoses)
    if cap.applied:
        score = min(score, cap.maximum)
    level, label = _level(score)
    definition = _definition(code)
    evidence = [_evidence(item.fact) for item in _unique_scored_facts(facts)]
    basis = [_rule_basis(item) for item in _unique_rules(facts)]
    explanation = _dimension_attention_meaning(code, level)
    return HealthAssessmentDimension(
        code=code,
        name=definition[1],
        weight=definition[2],
        score=score,
        score_available=True,
        display_score=str(score),
        level=level,
        level_label=label,
        confidence="high" if len(evidence) >= 3 and not conflicts else "moderate",
        evidence_used=evidence,
        rule_basis=basis,
        missing_evidence=[],
        conflicts=conflicts,
        trend_adjustment=trend,
        score_cap=cap,
        explanation=explanation,
    )


def _dimension_attention_meaning(code: str, level: str) -> str:
    meanings = {
        "gut_microecology": "肠道菌群结构及相关代谢功能",
        "gut_barrier": "肠道屏障、免疫调节及短链脂肪酸合成功能",
        "glucose_lipid_metabolism": "血糖与血脂代谢相关的长期健康表现",
        "cardiovascular_risk": "心血管危险因素的综合管理表现",
        "weight_body_composition": "体重、脂肪分布与身体组成",
        "kidney_uric_acid": "肾脏功能与尿酸代谢相关表现",
    }
    meaning = meanings[code]
    if level == "good":
        return f"{meaning}目前相对稳定。"
    if level == "priority_attention":
        return f"{meaning}整体偏弱，建议优先改善相关健康表现。"
    return f"{meaning}需要持续管理，并关注相关长期健康风险。"


def _unavailable(
    code: str,
    missing: list[str],
    *,
    conflicts: list[str] | None = None,
    level: str = "insufficient_data",
    facts: list[_ScoredFact] | None = None,
) -> HealthAssessmentDimension:
    definition = _definition(code)
    missing_text = "、".join(missing)
    prefix = (
        "当前评分标准暂不适用" if level == "rule_not_applicable" else "相关信息暂不完整"
    )
    return HealthAssessmentDimension(
        code=code,
        name=definition[1],
        weight=definition[2],
        level=level,
        level_label="规则不适用" if level == "rule_not_applicable" else "暂不评分",
        evidence_used=[
            _evidence(item.fact) for item in _unique_scored_facts(facts or [])
        ],
        rule_basis=[_rule_basis(item) for item in _unique_rules(facts or [])],
        missing_evidence=missing,
        conflicts=conflicts or [],
        explanation=(
            f"{definition[1]}本次暂不评分：{prefix}。建议补充：{missing_text}。"
        ),
    )


def _abnormal_indicators(
    dimensions: list[HealthAssessmentDimension],
    resolved: _Resolved,
    *,
    sex: str | None,
) -> list[HealthAssessmentAbnormalIndicator]:
    """Expose only abnormal facts that actually participated in a dimension."""

    results: list[HealthAssessmentAbnormalIndicator] = []
    seen: set[str] = set()
    for dimension in dimensions:
        dimension_codes = {
            evidence.indicator_code for evidence in dimension.evidence_used
        }
        for evidence in dimension.evidence_used:
            code = evidence.indicator_code
            if (
                code in seen
                or code == "microbiome:gut_overall_score"
                or code.startswith("microbe_composition:")
                or code.startswith("diagnosis:")
                or (
                    code == "barrier_aux:scfa"
                    and any(
                        specific in dimension_codes
                        for specific in (
                            "barrier_aux:acetate",
                            "barrier_aux:propionate",
                            "barrier_aux:butyrate",
                        )
                    )
                )
            ):
                continue
            fact = resolved.current.get(code) or _fact_from_evidence(evidence)
            score = _abnormal_indicator_score(fact, sex=sex)
            if score is None or score >= 85:
                continue
            status, reference_range = _abnormal_indicator_presentation(
                fact,
                evidence,
                dimension,
                score=score,
            )
            # A management-score deduction alone is not a patient-facing
            # abnormal laboratory result. When the source report/reference
            # range did not classify the value as high, low or abnormal, keep
            # the score contribution internal instead of publishing the
            # implementation phrase "未达到健康管理评估标准".
            if status == "未达到健康管理评估标准":
                continue
            seen.add(code)
            results.append(
                HealthAssessmentAbnormalIndicator(
                    dimension_code=dimension.code,
                    dimension_name=dimension.name,
                    indicator_code=code,
                    item=evidence.item,
                    value=evidence.value,
                    unit=evidence.unit,
                    reference_range=reference_range,
                    status=status,
                    severity=("attention" if score >= 55 else "priority_attention"),
                    date=evidence.date,
                )
            )
    return results


def _abnormal_indicator_presentation(
    fact: IndicatorFact,
    evidence: AssessmentEvidenceUsed,
    dimension: HealthAssessmentDimension,
    *,
    score: int,
) -> tuple[str, str | None]:
    source_score = _score_status_or_reference(fact)
    rule = next(
        (item for item in dimension.rule_basis if item.indicator == evidence.item),
        None,
    )
    if source_score is not None and source_score < 85:
        status = evidence.status or _range_status(fact)
        reference_range = evidence.reference_range or (rule.boundary if rule else None)
    else:
        status = _range_status(fact)
        reference_range = evidence.reference_range
        if score < 85 and not status:
            status = "未达到健康管理评估标准"
            reference_range = rule.boundary if rule else reference_range
    if (
        fact.code.startswith(("barrier_core:", "barrier_aux:", "microbiome:"))
        and not evidence.reference_range
    ):
        # The internal provider-score mapping is a scoring rule, not a source
        # reference interval. Do not display it as though the uploaded report
        # supplied a numeric boundary for this particular metric.
        reference_range = "原报告未提供数值参考范围"
    return (
        status or dimension.level_label,
        reference_range or "原报告未提供数值参考范围",
    )


def _fact_from_evidence(evidence: AssessmentEvidenceUsed) -> IndicatorFact:
    return IndicatorFact(
        code=evidence.indicator_code,
        name=evidence.item,
        raw_value=evidence.value,
        numeric_value=numeric_value(evidence.value),
        unit=evidence.unit,
        date=evidence.date,
        source=evidence.source,
        source_type=evidence.source_type,
        priority=99,
        order=0,
        status=evidence.status,
        reference_range=evidence.reference_range,
    )


def _abnormal_indicator_score(
    fact: IndicatorFact,
    *,
    sex: str | None,
) -> int | None:
    if fact.code == "bmi" and fact.numeric_value is not None:
        return _score_bmi(fact).score
    if fact.code == "egfr" and fact.numeric_value is not None:
        return _score_egfr(fact).score
    known = _score_known_fact(fact, sex=sex)
    if known is not None:
        return known.score
    return _score_status_or_reference(fact)


def _range_status(fact: IndicatorFact) -> str | None:
    lower, upper = parse_reference_range(fact.reference_range)
    value = fact.numeric_value
    if value is None:
        return None
    if lower is not None and value < lower:
        return "低于参考范围"
    if upper is not None and value > upper:
        return "高于参考范围"
    return None


def _score_prefixes(
    resolved: _Resolved,
    prefixes: tuple[str, ...],
    *,
    rule_key: str,
) -> list[_ScoredFact]:
    results = []
    for code, fact in resolved.current.items():
        if not any(code == prefix or code.startswith(prefix) for prefix in prefixes):
            continue
        score = _score_status_or_reference(fact)
        if score is not None:
            results.append(
                _ScoredFact(
                    fact,
                    score,
                    rule_key,
                    "检测报告分级标准：优秀95、正常85、轻度70、中度50、重度30",
                    score <= 30,
                )
            )
    return results


_METABOLIC_CODES = {
    "fasting_glucose",
    "hba1c",
    "ogtt_2h",
    "total_cholesterol",
    "triglyceride",
    "ldl_c",
    "hdl_c",
    "non_hdl_c",
}


def _score_codes(
    resolved: _Resolved,
    codes: tuple[str, ...],
    *,
    sex: str | None = None,
) -> list[_ScoredFact]:
    results = []
    for code in codes:
        fact = resolved.current.get(code)
        if not fact:
            continue
        scored = _score_known_fact(fact, sex=sex)
        if scored:
            results.append(scored)
    return results


def _score_known_fact(
    fact: IndicatorFact, *, sex: str | None = None
) -> _ScoredFact | None:
    if fact.code == "smoking":
        score = {"never": 95, "former": 70, "current": 30}.get(fact.status or "")
        return (
            _ScoredFact(
                fact,
                score,
                "blood_pressure",
                "不吸烟95、已戒70、当前吸烟30",
                score == 30,
            )
            if score
            else None
        )
    value = fact.numeric_value
    if value is None:
        fallback = _score_status_or_reference(fact)
        if fallback is None:
            return None
        return _ScoredFact(
            fact,
            fallback,
            _rule_key(fact.code),
            "原报告状态或参考范围分层",
            fallback <= 30,
        )
    unit_required = {
        "fasting_glucose",
        "ogtt_2h",
        "total_cholesterol",
        "triglyceride",
        "ldl_c",
        "hdl_c",
        "non_hdl_c",
        "uric_acid",
        "waist",
        "body_fat",
        "creatinine",
        "uacr",
    }
    if fact.code in unit_required and not fact.unit:
        # A numeric value without a unit cannot safely use a universal medical
        # boundary.  Preserve the laboratory's own status/reference range.
        return _score_from_reference(fact)
    if fact.code == "fasting_glucose":
        score = 50 if value < 3.9 else 85 if value < 6.1 else 70 if value < 7.0 else 30
        boundary = "<3.9为50，3.9–6.0为85，6.1–6.9为70，≥7.0 mmol/L为30"
    elif fact.code == "hba1c":
        score = 85 if value < 5.7 else 70 if value < 6.5 else 30
        boundary = "<5.7%为85，5.7–6.4%为70，≥6.5%为30"
    elif fact.code == "ogtt_2h":
        score = 85 if value < 7.8 else 70 if value < 11.1 else 30
        boundary = "<7.8为85，7.8–11.0为70，≥11.1 mmol/L为30"
    elif fact.code == "ldl_c":
        score = 85 if value < 3.4 else 70 if value < 4.1 else 50 if value < 4.9 else 30
        boundary = "<3.4为85，3.4–4.0为70，4.1–4.8为50，≥4.9 mmol/L为30"
    elif fact.code == "non_hdl_c":
        score = 85 if value < 4.1 else 70 if value < 4.9 else 50
        boundary = "<4.1为85，4.1–4.8为70，≥4.9 mmol/L为50"
    elif fact.code == "triglyceride":
        score = 85 if value < 1.7 else 70 if value < 2.3 else 50 if value < 5.6 else 30
        boundary = "<1.7为85，1.7–2.2为70，2.3–5.5为50，≥5.6 mmol/L为30"
    elif fact.code == "total_cholesterol":
        score = 85 if value < 5.2 else 70 if value < 6.2 else 50
        boundary = "<5.2为85，5.2–6.1为70，≥6.2 mmol/L为50"
    elif fact.code == "hdl_c":
        threshold = 1.0 if sex == "男" else 1.3 if sex == "女" else None
        if threshold is None:
            return _score_from_reference(fact)
        score = 85 if value >= threshold else 50
        boundary = f"{sex}性≥{threshold:g} mmol/L为85，低于该值为50"
    elif fact.code == "egfr":
        return _score_egfr(fact)
    elif fact.code == "uacr":
        score = 95 if value < 30 else 70 if value <= 300 else 30
        boundary = "<30为95，30–300为70，>300 mg/g为30"
    elif fact.code == "uric_acid":
        threshold = 420 if sex == "男" else 360 if sex == "女" else None
        if threshold is None:
            return _score_from_reference(fact)
        score = 85 if value <= threshold else 50
        boundary = f"{sex}性≤{threshold} μmol/L为85，高于该值为50"
    elif fact.code == "waist":
        threshold = 90 if sex == "男" else 85 if sex == "女" else None
        if threshold is None:
            return _score_from_reference(fact)
        score = 85 if value < threshold else 50
        boundary = f"{sex}性<{threshold} cm为85，≥{threshold} cm为50"
    elif fact.code in {"body_fat", "visceral_fat", "skeletal_muscle"}:
        return _score_from_reference(fact)
    else:
        return _score_from_reference(fact)
    return _ScoredFact(fact, score, _rule_key(fact.code), boundary, score <= 30)


def _blood_pressure_scores(resolved: _Resolved) -> list[_ScoredFact]:
    systolic = resolved.current.get("systolic_bp")
    diastolic = resolved.current.get("diastolic_bp")
    if (
        not systolic
        or not diastolic
        or systolic.numeric_value is None
        or diastolic.numeric_value is None
    ):
        return []
    sbp, dbp = systolic.numeric_value, diastolic.numeric_value
    if sbp < 120 and dbp < 80:
        score = 95
    elif sbp < 130 and dbp < 85:
        score = 85
    elif sbp < 140 and dbp < 90:
        score = 70
    elif sbp < 160 and dbp < 100:
        score = 50
    else:
        score = 30
    boundary = "<120/80为95，<130/85为85，<140/90为70，<160/100为50，更高为30"
    return [
        _ScoredFact(systolic, score, "blood_pressure", boundary, score <= 30),
        _ScoredFact(diastolic, score, "blood_pressure", boundary, score <= 30),
    ]


def _score_bmi(fact: IndicatorFact) -> _ScoredFact:
    value = fact.numeric_value or 0
    score = 70 if value < 18.5 else 95 if value < 24 else 70 if value < 28 else 30
    return _ScoredFact(
        fact,
        score,
        "body_composition",
        "<18.5为70，18.5–23.9为95，24.0–27.9为70，≥28 kg/m²为30",
        score <= 30,
    )


def _score_egfr(fact: IndicatorFact) -> _ScoredFact:
    value = fact.numeric_value or 0
    score = (
        95
        if value >= 90
        else 85
        if value >= 60
        else 70
        if value >= 45
        else 50
        if value >= 30
        else 30
    )
    return _ScoredFact(
        fact,
        score,
        "kidney",
        "≥90为95，60–89为85，45–59为70，30–44为50，<30 mL/min/1.73m²为30",
        score <= 30,
    )


def _score_from_reference(fact: IndicatorFact) -> _ScoredFact | None:
    score = _score_status_or_reference(fact)
    if score is None:
        return None
    return _ScoredFact(
        fact,
        score,
        _rule_key(fact.code),
        "原报告参考范围或明确等级分层",
        score <= 30,
    )


def _score_status_or_reference(fact: IndicatorFact) -> int | None:
    text = (fact.status or "").replace(" ", "")
    for patterns, score in STATUS_SCORE_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return score
    lower, upper = parse_reference_range(fact.reference_range)
    value = fact.numeric_value
    if value is None or (lower is None and upper is None):
        return None
    if (lower is None or value >= lower) and (upper is None or value <= upper):
        return 85
    distance = 0.0
    if upper is not None and value > upper and upper:
        distance = (value - upper) / abs(upper)
    elif lower is not None and value < lower and lower:
        distance = (lower - value) / abs(lower)
    return 70 if distance <= 0.10 else 50 if distance <= 0.30 else 30


def _derived_egfr(
    creatinine: IndicatorFact, age: int, sex: str
) -> IndicatorFact | None:
    if (
        creatinine.numeric_value is None
        or creatinine.numeric_value <= 0
        or not _is_micromoles_per_litre(creatinine.unit)
    ):
        return None
    scr_mg_dl = creatinine.numeric_value / 88.4
    kappa = 0.7 if sex == "女" else 0.9
    alpha = -0.241 if sex == "女" else -0.302
    ratio = scr_mg_dl / kappa
    egfr = 142 * (min(ratio, 1) ** alpha) * (max(ratio, 1) ** -1.200) * (0.9938**age)
    if sex == "女":
        egfr *= 1.012
    if not math.isfinite(egfr):
        return None
    return IndicatorFact(
        code="egfr",
        name="eGFR",
        raw_value=f"{egfr:.1f}",
        numeric_value=egfr,
        unit="mL/min/1.73m²",
        date=creatinine.date,
        source="由血肌酐、年龄和性别按CKD-EPI 2021计算",
        source_type="derived",
        priority=5,
        order=0,
    )


def _is_micromoles_per_litre(unit: str | None) -> bool:
    return compact_name(unit or "") in {"μmoll", "umoll"}


def _diagnosis_indicator(diagnosis: DiagnosisFact) -> IndicatorFact:
    return IndicatorFact(
        code=f"diagnosis:{diagnosis.code}",
        name=diagnosis.name,
        raw_value="已确认疾病史",
        numeric_value=None,
        unit=None,
        date=diagnosis.date,
        source=diagnosis.source,
        source_type=diagnosis.source_type,
        priority=1 if diagnosis.source_type == "hospital_data" else 4,
        order=0,
        status="控制不佳" if diagnosis.control_poor else "已确认",
    )


def _trend_adjustment(facts: list[_ScoredFact], resolved: _Resolved) -> int:
    adjustment = 0
    seen = set()
    for scored in facts:
        code = scored.fact.code
        if code in seen or code.startswith("diagnosis:") or code == "bmi":
            continue
        seen.add(code)
        history = [
            item for item in resolved.histories.get(code, []) if date_rank(item.date)
        ]
        if len(history) < 2:
            continue
        latest, previous = history[0], history[1]
        latest_score = _score_known_fact(latest)
        previous_score = _score_known_fact(previous)
        if latest_score is None or previous_score is None:
            latest_value = _score_status_or_reference(latest)
            previous_value = _score_status_or_reference(previous)
        else:
            latest_value, previous_value = latest_score.score, previous_score.score
        if latest_value is None or previous_value is None or latest_value >= 85:
            continue
        if latest_value < 85 and previous_value < 85:
            adjustment -= 5
        if latest_value < previous_value:
            adjustment -= 5
        if adjustment <= -10:
            return -10
    return max(-10, adjustment)


_DIMENSION_DIAGNOSES = {
    "gut_microecology": set(),
    "gut_barrier": {"inflammatory_bowel_disease"},
    "glucose_lipid_metabolism": {"diabetes"},
    "cardiovascular_risk": {"hypertension", "cardiovascular_disease", "diabetes"},
    "weight_body_composition": {"obesity"},
    "kidney_uric_acid": {"chronic_kidney_disease", "gout"},
}


def _score_cap(
    code: str,
    facts: list[_ScoredFact],
    diagnoses: list[DiagnosisFact],
) -> AssessmentScoreCap:
    if any(item.severe for item in facts):
        return AssessmentScoreCap(
            applied=True,
            maximum=54,
            reason="存在明确重度异常",
        )
    relevant = [item for item in diagnoses if item.code in _DIMENSION_DIAGNOSES[code]]
    if not relevant:
        return AssessmentScoreCap()
    if any(item.control_poor or item.complication for item in relevant):
        names = "、".join(dict.fromkeys(item.name for item in relevant))
        return AssessmentScoreCap(
            applied=True,
            maximum=54,
            reason=f"已确认{names}且存在控制不佳、持续异常或并发症",
        )
    names = "、".join(dict.fromkeys(item.name for item in relevant))
    return AssessmentScoreCap(
        applied=True,
        maximum=74,
        reason=f"已确认{names}病史",
    )


def _average(items: list[_ScoredFact]) -> float:
    return sum(item.score for item in items) / len(items)


def _level(score: int) -> tuple[str, str]:
    for lower, code, label in LEVEL_BANDS:
        if score >= lower:
            return code, label
    return "priority_attention", "重点关注"


def _definition(code: str) -> tuple[str, str, float]:
    return next(item for item in LEGACY_DIMENSIONS if item[0] == code)


def _rule_key(code: str) -> str:
    if code in {"fasting_glucose", "hba1c", "ogtt_2h"}:
        return "glycemia"
    if code in {"total_cholesterol", "triglyceride", "ldl_c", "hdl_c", "non_hdl_c"}:
        return "lipid"
    if code in {"systolic_bp", "diastolic_bp", "smoking"}:
        return "blood_pressure"
    if code in {
        "height",
        "weight",
        "waist",
        "body_fat",
        "visceral_fat",
        "skeletal_muscle",
        "bmi",
    }:
        return "body_composition"
    if code in {"egfr", "uacr", "creatinine"}:
        return "kidney"
    if code == "uric_acid":
        return "uric_acid"
    return "level_mapping"


def _evidence(fact: IndicatorFact) -> AssessmentEvidenceUsed:
    return AssessmentEvidenceUsed(
        indicator_code=fact.code,
        item=fact.name,
        value=fact.raw_value,
        unit=fact.unit,
        date=fact.date,
        source=fact.source,
        source_type=fact.source_type,
        status=fact.status,
        reference_range=fact.reference_range,
    )


def _rule_basis(item: _ScoredFact) -> AssessmentRuleBasis:
    rule_id, source, version = RULE_SOURCES[item.rule_key]
    return AssessmentRuleBasis(
        rule_id=rule_id,
        indicator=item.fact.name,
        source=source,
        version=version,
        boundary=item.boundary,
    )


def _unique_scored_facts(items: list[_ScoredFact]) -> list[_ScoredFact]:
    result = []
    seen = set()
    for item in items:
        key = (item.fact.code, item.fact.raw_value, item.fact.date, item.fact.source)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _unique_rules(items: list[_ScoredFact]) -> list[_ScoredFact]:
    result = []
    seen = set()
    for item in items:
        key = (item.rule_key, item.boundary, item.fact.name)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _display_value(fact: IndicatorFact) -> str:
    value = fact.raw_value or ""
    unit = fact.unit or ""
    status = f"（{fact.status}）" if fact.status else ""
    display = value
    if unit and not value.casefold().endswith(unit.casefold()):
        display += unit
    return f"{display}{status}"


def _conflicts_for(resolved: _Resolved, codes: tuple[str, ...]) -> list[str]:
    output = []
    for code, values in resolved.conflicts.items():
        if any(code == token or code.startswith(token) for token in codes):
            output.append(f"{code}存在同日同优先级冲突：" + "；".join(values))
    return output
