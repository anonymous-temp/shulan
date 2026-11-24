"""Local template-bound diagrams; consume public display blocks, not medical rules.

This is intentionally not a new public block kind. A receiver needs the same
five-chapter template to reproduce these illustrations from the frozen JSON.
"""
from __future__ import annotations

import html
import math
import re
from types import MappingProxyType

from modules.gut_barrier.display_versions import (
    LEGACY_DISPLAY_TEMPLATE, PRE_XU_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE, REFINED_XU_DISPLAY_TEMPLATE,
    WORD_REVIEW_DISPLAY_TEMPLATE, CANONICAL_DISPLAY_TEMPLATE, PRODUCT_FEEDBACK_DISPLAY_TEMPLATE,
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, TEMPLATE_RISK_DISPLAY_TEMPLATE, PINNED_REFERENCE_DISPLAY_TEMPLATE,
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE, BARRIER_ONLY_DISPLAY_TEMPLATE,
    display_capabilities, resolve_display_template,
)
from modules.gut_barrier.overall_stage import OVERALL_STAGE_OPTIONS
from modules.gut_barrier.integration_content import ATTENTION_OVERVIEW_COLUMNS, ATTENTION_OVERVIEW_TITLE, QUALITATIVE_FOLLOWUP_LEVEL
from modules.gut_barrier.template_risk_review import RISK_OVERVIEW_COLUMNS, RISK_OVERVIEW_TITLE
from .frontend_template import uses_attention_index


CHAPTER_IDS = ("overview", "disease_risks", "repair_plan", "education", "appendix")
LEGACY_CHAPTER_IDS = ("overview", "education", "disease_risks", "repair_plan", "appendix")
CANONICAL_CHAPTER_IDS = ("overview", "disease_risks", "repair_plan", "appendix")
MARKER_COLUMNS = ("检测指标", "检测结果", "参考范围", "结果评价", "单项评分")
CANONICAL_MARKER_COLUMNS = ("检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度")
RISK_COLUMNS = ("疾病", "风险指数", "风险等级", "机制关联")
_MEASUREMENT = re.compile(r"^\s*((?:<=|>=|[<>≤≥＜＞=])?)\s*([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([^\d\s].*?)?\s*$")

# Immutable release snapshots, not imports from the current scoring engine.
# Replaying a frozen callback must not pick up a future clinical rule change.
_FIVE_GRADE_GRAPH_RULES = MappingProxyType({
    "D_LACTATE": ((14, 17, 22, 30), (0, 1, 2, 3, 4), "mg/L"),
    "DAO": ((8.5, 11, 16, 20), (0, 1, 2.5, 3.5, 5), "U/L"),
    "LPS": ((18, 22, 28, 35), (0, 1.5, 3, 4.5, 6), "U/L"),
})
_GRAPH_RELEASES = MappingProxyType({
    BARRIER_ONLY_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    PINNED_REFERENCE_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    TEMPLATE_RISK_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    ORDERED_REFERENCE_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    PRODUCT_FEEDBACK_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    CANONICAL_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    WORD_REVIEW_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    LEGACY_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    PRE_XU_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    XU_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    REFINED_XU_DISPLAY_TEMPLATE: _FIVE_GRADE_GRAPH_RULES,
    "gut-barrier-static.v5": _FIVE_GRADE_GRAPH_RULES,
    "gut-barrier-static.v6": _FIVE_GRADE_GRAPH_RULES,
})
_FIVE_GRADE_LABELS = ("正常", "亚健康", "轻度", "中度", "重度")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return _text(value.get("text", value.get("label")))
    if isinstance(value, (list, tuple)):
        return " / ".join(_text(item) for item in value)
    return str(value)


def _e(value):
    return html.escape(_text(value), quote=True)


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def _number(value):
    # A string that happens to contain a score is not an approved numeric field.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _measurement(value):
    match = _MEASUREMENT.fullmatch(_text(value))
    if not match:
        return None
    number = float(match.group(2))
    if not math.isfinite(number):
        return None
    return match.group(1).translate(str.maketrans("＜＞", "<>")), number, (match.group(3) or "").strip()


def _cells(row):
    return _list(row.get("cells")) if isinstance(row, dict) else _list(row)


def _figure(number, title, body, note):
    return (f'<figure class="report-figure" id="figure-{number}">'
            f'<figcaption><span class="figure-number">图 {number}</span>{_e(title)}</figcaption>'
            f'{body}<p class="figure-note">{_e(note)}</p></figure>')


def chapter_layout(sections):
    """Recognize frozen legacy callbacks without changing their chapter order."""
    identifiers = tuple(section.get("id") for section in _list(sections)
                        if isinstance(section, dict) and section.get("visible") is not False)
    if identifiers == CHAPTER_IDS:
        return "reordered"
    if identifiers == LEGACY_CHAPTER_IDS:
        return "legacy"
    return None


def is_five_chapter_report(sections):
    return chapter_layout(sections) is not None


def supports_report_graphics(sections, *, template_version=LEGACY_DISPLAY_TEMPLATE):
    """V8 keeps cover/diagrams with four chapters; historical gates stay frozen."""
    if display_capabilities(template_version).canonical_dynamic_layout:
        identifiers = tuple(section.get("id") for section in _list(sections)
                            if isinstance(section, dict) and section.get("visible") is not False)
        return identifiers == CANONICAL_CHAPTER_IDS
    return is_five_chapter_report(sections)


_PALETTE = ("#88baa3", "#bec985", "#edcc7b", "#dfa47b", "#bc7880")


def _bands(sections, title):
    for section in _list(sections):
        if not isinstance(section, dict) or section.get("id") != "overview" or section.get("visible") is False:
            continue
        for block in _list(section.get("blocks")):
            if isinstance(block, dict) and block.get("kind") == "bands" and block.get("title") == title:
                return [item for item in _list(block.get("items")) if isinstance(item, dict)]
    return []


def _segments_from_public(items, maximum):
    """Use already-published intervals, never run a clinical classifier."""
    segments = []
    for item in items:
        numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", _text(item.get("range")))]
        if len(numbers) == 2:
            low, high = numbers
        elif len(numbers) == 1:
            low, high = (numbers[0], maximum) if re.search(r"[>＞]", _text(item.get("range"))) else (0, numbers[0])
        else:
            continue
        if not 0 <= low < high <= maximum:
            continue
        segments.append((low, high, item.get("label"), item.get("range"), item.get("selected") is True))
    return segments


def _scale(segments, maximum, *, unit="", class_name="interval-track", marker="", label=""):
    bands, legends = [], []
    for index, (low, high, name, span, selected) in enumerate(segments):
        color = _PALETTE[min(index, len(_PALETTE) - 1)]
        bands.append(f'<span class="scale-segment" style="left:{low / maximum * 100:.4f}%;width:{(high - low) / maximum * 100:.4f}%;background:{color}" title="{_e(name)} {_e(span)}"></span>')
        legends.append(f'<span class="scale-legend-item{" selected" if selected else ""}"><i style="background:{color}"></i><b>{_e(name)}</b><small>{_e(span)}</small></span>')
    ticks = sorted({0.0, maximum, *(edge for segment in segments for edge in segment[:2])})
    axis = ''.join(f'<span class="scale-tick" style="left:{value / maximum * 100:.4f}%">{value:g}</span>' for value in ticks)
    return (f'<div class="{class_name} classified-track" role="img" aria-label="{_e(label)}">{"".join(bands)}{marker}</div>'
            f'<div class="scale-axis">{axis}<span class="scale-unit">{_e(unit)}</span></div>'
            f'<div class="scale-legend">{"".join(legends)}</div>')


def _marker_code(name):
    name = _text(name).casefold()
    for code, aliases in (("D_LACTATE", ("d-乳酸", "d-lactate")), ("DAO", ("dao", "二胺氧化酶")),
                          ("LPS", ("lps", "细菌脂多糖", "细菌内毒素")), ("HS_CRP", ("hs-crp", "超敏c反应蛋白"))):
        if any(alias in name for alias in aliases):
            return code
    return None


def _hscrp_source_axis(name, result, reference, result_number, reference_number):
    """Label only supplied values; geometry padding is never a numeric tick.

    A censored result supplies a boundary, not an exact measurement. Open and
    closed endpoints retain that distinction without inventing an axis limit.
    """
    operator, value, unit = result_number
    _ref_operator, threshold, _ref_unit = reference_number
    lower = operator in {"<", "<=", "≤"}
    comparison = lower or operator in {">", ">=", "≥"}
    inclusive = operator in {"<=", ">=", "≤", "≥"}
    # The reference is positive and both numbers are finite before this call.
    # Reserve space after the largest supplied anchor for an open-ended arrow.
    span = max(value, threshold)
    position, reference_position = 42 + value / span * 238, 42 + threshold / span * 238
    anchor = lambda x: "start" if x < 120 else "end" if x > 240 else "middle"
    raw_value = _MEASUREMENT.fullmatch(_text(result)).group(2)
    if comparison:
        direction = "less" if lower else "greater"
        boundary_kind = "closed" if inclusive else "open"
        result_label = f"界值 {raw_value} {unit}（{'含' if inclusive else '不含'}界值）"
        end = 20 if lower else 338
        start = position - 7 if lower else position + 7
        head = end + 7 if lower else end - 7
        arrow = (f'<path d="M{start:.4f} 44H{end}M{head} 39L{end} 44L{head} 49" '
                 'fill="none" stroke="#915828" stroke-width="2"/>')
    else:
        direction, boundary_kind, arrow = "exact", "exact", ""
        result_label = f"结果 {raw_value} {unit}"
    fill = "#ffffff" if boundary_kind == "open" else "#1b5d4c"
    description = f"{_text(name)}：{_text(result)}；功能医学标准值 {_text(reference)}"
    return (
        f'<div class="hscrp-source-axis" data-result-direction="{direction}" data-boundary-kind="{boundary_kind}">'
        f'<svg viewBox="0 0 360 94" role="img" aria-label="{_e(description)}" style="width:100%;height:auto;display:block">'
        '<path d="M20 44H338" stroke="#d7e4dc" stroke-width="5" fill="none"/>'
        f'<path d="M{reference_position:.4f} 29V61" stroke="#476d61" stroke-dasharray="3 3"/>'
        f'<text x="{reference_position:.4f}" y="18" text-anchor="{anchor(reference_position)}" font-size="10" fill="#63786e">'
        f'功能医学标准值 {_e(reference)}</text>{arrow}'
        f'<circle cx="{position:.4f}" cy="44" r="5" fill="{fill}" stroke="#1b5d4c" stroke-width="2"/>'
        f'<text x="{position:.4f}" y="81" text-anchor="{anchor(position)}" font-size="11" fill="#344b44">'
        f'{_e(result_label)}</text></svg></div>'
    )


def _indicator_plot(block, sections=(), figure_number=1, *, template_version=LEGACY_DISPLAY_TEMPLATE,
                    clean_review_layout=False):
    capabilities = display_capabilities(template_version)
    columns = tuple(_list(block.get("columns")))
    canonical_columns = capabilities.canonical_dynamic_layout and columns == CANONICAL_MARKER_COLUMNS
    if columns != MARKER_COLUMNS and not canonical_columns:
        return ""
    rows = []
    template_version = resolve_display_template({}, template_version)
    xu = capabilities.xu_review
    graph_rules = _GRAPH_RELEASES[template_version]
    for row in _list(block.get("rows")):
        cells = _cells(row)
        if len(cells) != (6 if canonical_columns else 5):
            continue
        name, result, reference, evaluation = cells[1:5] if canonical_columns else cells[:4]
        code = _marker_code(name)
        result_number, reference_number = _measurement(result), _measurement(reference)
        graph = '<div class="plot-unavailable">未提供可绘制的定量区间</div>'
        if result_number and reference_number:
            operator, value, unit = result_number
            ref_operator, threshold, ref_unit = reference_number
            if ref_operator in {"<", "<=", "≤"} and threshold > 0 and unit.casefold() == ref_unit.casefold():
                maximum = max(threshold * 2, value * 1.2)
                boundary = threshold / maximum * 100
                position = value / maximum * 100
                comparison = operator in {"<", ">", "<=", ">=", "≤", "≥"}
                if comparison:
                    arrow = "←" if operator in {"<", "<=", "≤"} else "→"
                    point = f'<span class="range-bound" style="left:{position:.4f}%" aria-label="结果边界">{arrow}</span>'
                else:
                    point = f'<span class="result-point" style="left:{position:.4f}%" aria-label="本次结果"></span>'
                reference_label = (f"功能医学标准值 {_e(reference)}" if xu and code == "HS_CRP"
                                   else f"参考上界 {threshold:g} {_e(unit)}")
                graph = (f'<div class="interval-track" role="img" aria-label="{_e(name)}：{_e(result)}，参考范围 {_e(reference)}">'
                         f'<span class="reference-zone" style="width:{boundary:.4f}%"></span>'
                         f'<span class="reference-tick" style="left:{boundary:.4f}%"></span>{point}</div>'
                         f'<div class="interval-labels"><span>0</span><span>{reference_label}</span>'
                         f'<span>{maximum:g} {_e(unit)}</span></div>')
                segments = []
                if code in graph_rules and unit.casefold() == graph_rules[code][2].casefold():
                    frozen_boundaries, frozen_scores, _unit = graph_rules[code]
                    boundaries = [float(edge) for edge in frozen_boundaries]
                    maximum = max(boundaries[-1] * 1.25, value * 1.2)
                    edges = [0, *boundaries, maximum]
                    for index, level in enumerate(_FIVE_GRADE_LABELS):
                        if capabilities.closed_boundary_layout:
                            span = (f"＜{edges[1]:g}" if index == 0 else f"≥{edges[index]:g}" if index == len(_FIVE_GRADE_LABELS) - 1
                                    else f"≥{edges[index]:g}且＜{edges[index + 1]:g}")
                        else:
                            span = (f"＜{edges[1]:g}" if index == 0 else f"＞{edges[index]:g}" if index == len(_FIVE_GRADE_LABELS) - 1
                                    else f"{edges[index]:g}–{edges[index + 1]:g}")
                        label = f"评分区间 · {frozen_scores[index]:g}分" if xu else f"{level} · {frozen_scores[index]:g}分"
                        segments.append((edges[index], edges[index + 1], label, span, False))
                elif code == "HS_CRP" and not xu:
                    items = _bands(sections, "hs-CRP 项目功能分层")
                    endpoints = [float(number) for item in items for number in re.findall(r"\d+(?:\.\d+)?", _text(item.get("range")))]
                    if endpoints:
                        maximum = max(max(endpoints) * 1.4, value * 1.2)
                        segments = _segments_from_public(items, maximum)
                if segments:
                    position = value / maximum * 100
                    point = (f'<span class="range-bound" style="left:{position:.4f}%" aria-label="结果边界">{arrow}</span>' if comparison else
                             f'<span class="result-point" style="left:{position:.4f}%" aria-label="本次结果"></span>')
                    graph = _scale(segments, maximum, unit=unit, marker=point,
                                   label=f"{_text(name)}：{_text(result)}；评价：{_text(evaluation)}")
                if clean_review_layout and code == "HS_CRP":
                    graph = _hscrp_source_axis(name, result, reference, result_number, reference_number)
        rows.append(f'<div class="indicator-plot"><div class="plot-heading"><b>{_e(name)}</b>'
                    f'<span>{_e(result) or "未提供"}</span></div>{graph}'
                    f'<p class="plot-context">{"功能医学标准值" if xu and code == "HS_CRP" else "参考范围"}：{_e(reference) or "未提供"} · {_e(evaluation) or "待评估"}</p></div>')
    if not rows:
        return ""
    note = ("圆点表示检测结果或范围界值；箭头表示结果延伸方向，空心界点表示不包含界值。各项参考信息见对应图示。"
            if clean_review_layout else
            "圆点表示定量结果，箭头表示带比较符结果的边界；各指标使用独立量尺。色带为分档示意，分界值处的评价以本次评估结果为准。")
    title = ("三项检测结果与参考信息" if capabilities.barrier_only_layout else
             "四项检测结果与参考信息" if xu else "四项检测结果与分级区间")
    return _figure(figure_number, title, '<div class="indicator-grid">' + "".join(rows) + '</div>', note)


def _source_stage_ibfc(items, score_item, value, figure_number, *, template_risk_layout=False):
    """Source-style stage band, with no score-to-health-stage conversion."""
    result = f"{value:g}" if value is not None and 0 <= value <= 15 else "未获得评分"
    stage_items = [item for item in items if item.get("label") == "总体阶段"]
    stage = stage_items[0].get("value") if len(stage_items) == 1 else None
    stage = stage if isinstance(stage, str) else None
    labels = []
    for code, label in OVERALL_STAGE_OPTIONS:
        selected = not template_risk_layout and stage in {code, label, label.removesuffix("人群")}
        class_name = "source-stage-label selected" if selected else "source-stage-label"
        current = ' aria-current="true"' if selected else ""
        display_label = label.removesuffix("人群") if template_risk_layout else label
        labels.append(f'<span class="{class_name}"{current}>{_e(display_label)}</span>')
    detail = _text(score_item.get("detail")).strip()
    # The numeric card is consumed by this graphic, not its clinical remarks.
    # Only the known fixed maximum-score caption is deliberately omitted.
    detail_html = (f'<p class="source-ibfc-detail">{_e(detail)}</p>'
                   if detail and not re.fullmatch(r"满分\s*15\s*分[。.]?", detail) else "")
    summary = ('<div class="source-ibfc-summary"><span>肠道屏障损伤评估指数（IBFC）</span>'
               f'<strong>{_e(result)}</strong></div>')
    if template_risk_layout:
        shares = [item for item in items if item.get("label") == "评分占满分比例"]
        share = shares[0].get("value") if len(shares) == 1 else None
        # The producer owns this arithmetic. Missing/invalid or repeated cards
        # cannot authorize a percentage computed by the preview renderer.
        valid_share = (isinstance(share, str) and re.fullmatch(r"\d+(?:\.\d+)?%", share)
                       and 0 <= float(share[:-1]) <= 100)
        share_text = share if valid_share else "未提供"
        summary = ('<div class="source-ibfc-summary template-risk-ibfc-summary">'
                   '<div><span>肠道屏障损伤评估指数（IBFC）</span>'
                   f'<strong>{_e(result)}</strong></div>'
                   f'<div><span>评分占满分比例</span><strong>{_e(share_text)}</strong></div></div>')
    band_description = ("健康、亚健康、疾病三阶段色带" if template_risk_layout else
                        "健康人群、亚健康人群、疾病人群三阶段色带")
    body = (
        '<div class="source-stage-ibfc">'
        f'{summary}'
        f'<div class="source-stage-band" role="img" aria-label="{band_description}"></div>'
        f'<div class="source-stage-labels" role="group" aria-label="健康阶段">{"".join(labels)}</div>'
        f'{detail_html}</div>'
    )
    return _figure(figure_number, "肠道屏障损伤评估指数（IBFC）综合评估", body,
                   "色带为健康阶段概念示意。" if template_risk_layout else "")


def _ibfc_plot(block, sections=(), figure_number=2, *, xu=False, refined=False, source_stage_layout=False,
               template_risk_layout=False):
    items = [item for item in _list(block.get("items")) if isinstance(item, dict)]
    scores = [item for item in items if item.get("label") == "IBFC评分"]
    if len(scores) != 1:
        return ""
    value = _number(scores[0].get("value"))
    if source_stage_layout:
        return _source_stage_ibfc(items, scores[0], value, figure_number,
                                 template_risk_layout=template_risk_layout)
    grade = next((_text(item.get("value")) for item in items if item.get("label") == "损伤等级"), "")
    if value is not None and 0 <= value <= 15:
        marker = f'<span class="result-point" style="left:{value / 15 * 100:.4f}%" aria-label="IBFC评分 {value:g}"></span>'
        result = f'{value:g} / 15'
    else:
        marker, result = "", "未获得可定位的总分"
    graph = (f'<div class="score-summary"><strong>{_e(result)}</strong><span>{_e(grade)}</span></div>'
             f'<div class="ibfc-track" role="img" aria-label="IBFC：{_e(result)}；{_e(grade)}">{marker}</div>'
             '<div class="interval-labels"><span>0</span><span>5</span><span>10</span><span>15</span></div>')
    if xu:
        if refined:
            # These are terminology concepts, not a classifier with an absent
            # selection. No IBFC value authorizes an overall-health diagnosis.
            concepts = " · ".join(label for _code, label in OVERALL_STAGE_OPTIONS)
            graph += (f'<p class="stage-concepts"><b>健康阶段概念</b><span>{_e(concepts)}</span></p>')
            return _figure(figure_number, "肠道屏障功能评分与阶段概念", graph,
                           "量尺展示本次 IBFC 评分；健康、亚健康与疾病为概念名称，不是由此总分推定的个人诊断。")
        stage_items = [item for item in items if item.get("label") == "总体阶段"]
        stage = stage_items[0].get("value") if len(stage_items) == 1 else None
        stage = stage if isinstance(stage, str) else None
        nodes = ''.join(
            f'<span class="overall-stage{" selected" if stage in {label, label.removesuffix("人群")} else ""}">'
            f'{label}{"<small>本次评估</small>" if stage in {label, label.removesuffix("人群")} else ""}</span>'
            for _code, label in OVERALL_STAGE_OPTIONS)
        detail = stage_items[0].get("detail") if len(stage_items) == 1 else None
        graph = ('<div class="overall-stages" role="group" aria-label="总体阶段">' + nodes + '</div>'
                 + (f'<p class="plot-context">{_e(detail)}</p>' if detail else '') + graph)
        return _figure(figure_number, "总体阶段与肠道屏障功能评分", graph,
                       "总体阶段与屏障损伤等级分别展示；量尺标示本次 IBFC 总分，不表示人群百分位。")
    segments = _segments_from_public(_bands(sections, "屏障评分分级"), 15)
    if segments:
        graph = (f'<div class="score-summary"><strong>{_e(result)}</strong><span>{_e(grade)}</span></div>'
                 + _scale(segments, 15, unit="分", class_name="ibfc-track", marker=marker,
                          label=f"IBFC：{result}；{grade}"))
        graph += '<p class="plot-context">0 分：未见评分异常。无定级区间及分界点不由图形重新判定，等级以本次评估结果为准。</p>'
    return _figure(figure_number, "肠道屏障功能综合评分", graph,
                   "量尺标示本次 IBFC 总分；损伤等级以评估结果为准。")


def _barrier_diagram(number=3):
    # Anatomical layers are authored vectors, not reused patient screenshots.
    panels = []
    for damaged, title in ((False, "完整屏障示意"), (True, "屏障改变示意")):
        cells, junctions, bacteria = [], [], []
        for i in range(6):
            x = 18 + i * 48 + (13 if damaged and i >= 3 else 0)
            cells.append(f'<rect x="{x}" y="116" width="43" height="64" rx="10" fill="#71aa96" stroke="#2c7562"/>'
                         f'<ellipse cx="{x + 22}" cy="155" rx="9" ry="12" fill="#d5e8df"/>'
                         f'<path d="M{x + 6} 116v-7m7 7v-7m7 7v-7m7 7v-7m7 7v-7" stroke="#2c7562" stroke-width="2"/>')
            if i < 5 and not (damaged and i == 2):
                junctions.append(f'<path d="M{x + 42} 129h7m-7 5h7" stroke="#314f77" stroke-width="3"/>')
            bacteria.append(f'<rect x="{26 + i * 47}" y="{48 + (i % 2) * 13}" width="16" height="7" rx="4" transform="rotate({(-1) ** i * 25} {34 + i * 47} {52 + (i % 2) * 13})" fill="{"#b8776a" if damaged else "#7b9e68"}"/>')
        mucus = ('<path d="M16 91Q60 79 100 91T145 91m34 0q50-11 144 0" stroke="#c7ddd0" stroke-width="9" fill="none"/>' if damaged else
                 '<path d="M16 88Q62 76 105 88T191 88T323 88" stroke="#c5e1d1" stroke-width="20" fill="none"/>')
        transfer = ('<path d="M164 77v119m-6-9 6 9 6-9" stroke="#b66d5c" stroke-width="3" fill="none"/>'
                    '<circle cx="160" cy="206" r="3" fill="#b66d5c"/><circle cx="175" cy="215" r="3" fill="#b66d5c"/>' if damaged else '')
        labels = (("黏液层变薄 / 菌群改变", "连接受扰", "微生物成分移位", "免疫激活") if damaged else
                  ("黏液与抗菌物质", "紧密连接", "选择性吸收", "免疫耐受"))
        panels.append(f'<div class="barrier-panel"><b>{title}</b><svg viewBox="0 0 410 285" role="img" aria-label="{title}：肠腔、黏液、紧密连接、肠上皮细胞、组织侧免疫细胞">'
                      '<rect x="8" y="35" width="327" height="68" rx="8" fill="#f5f1e4"/>'
                      '<rect x="8" y="184" width="327" height="64" rx="8" fill="#f7ede8"/>'
                      '<text x="17" y="25" fill="#556c60" font-size="12">肠腔侧：食物成分与微生物</text>'
                      f'{"".join(bacteria)}{mucus}{"".join(cells)}{"".join(junctions)}{transfer}'
                      '<path d="M64 202l10-6 10 6 2 13-12 8-12-8zM252 207l10-6 10 6 2 13-12 8-12-8z" fill="#bda0b9" stroke="#95728f"/>'
                      '<text x="17" y="243" fill="#6e6460" font-size="12">组织侧：免疫细胞、血管与代谢联系</text>'
                      f'<path d="M302 91h31M300 130h33M301 158h32" stroke="#869b91"/>'
                      '<text x="337" y="95" fill="#556c60" font-size="12">黏液</text><text x="337" y="135" fill="#556c60" font-size="12">连接</text>'
                      '<text x="337" y="163" fill="#556c60" font-size="12">上皮</text>'
                      f'<text x="17" y="274" fill="#556c60" font-size="12">{_e(labels[0])} · {_e(labels[3])}</text></svg>'
                      f'<p class="barrier-caption">{_e(labels[1])} · {_e(labels[2])}</p></div>')
    return _figure(number, "完整屏障与屏障改变", '<div class="barrier-panels">' + "".join(panels) + '</div>',
                   "机制示意，非受检者影像；菌群、黏液与抗菌物质、上皮及紧密连接、黏膜免疫相互协作。箭头表示可能的移位路径，不表示本次检测已证实移位。")


def _inflammation_diagram(number, *, barrier_only=False):
    chain = [("屏障改变", "黏液 / 紧密连接 / 上皮"), ("微生物成分移位", "LPS 等进入组织侧"),
             ("先天免疫识别", "TLR4 等受体信号"), ("炎症通路激活", "NF-κB 与炎症介质"),
             ("全身炎症信号", "炎症介质与免疫反应" if barrier_only else "IL-6 等 → 肝脏 CRP 合成")]
    nodes = ''.join(f'{"<span class=\"flow-arrow\" aria-hidden=\"true\">→</span>" if index else ""}'
                    f'<span class="flow-node"><b>{_e(title)}</b><small>{_e(detail)}</small></span>'
                    for index, (title, detail) in enumerate(chain))
    body = ('<div class="mechanism-flow detailed-flow">' + nodes + '</div>'
            '<div class="feedback-loop"><span aria-hidden="true">↶</span> 炎症介质可进一步影响上皮更新、细胞间连接与菌群环境，形成双向放大环路。</div>')
    return _figure(number, "屏障与炎症信号的联系", body,
                   "通用机制示意，非本次检测已证实的事件链。" if barrier_only else
                   "通用机制示意，非本次检测已证实的事件链；hs-CRP 反映炎症信号，不单独定位其来源。")


def _risk_tables(sections):
    for section in _list(sections):
        if not isinstance(section, dict) or section.get("id") != "disease_risks" or section.get("visible") is False:
            continue
        for block in _list(section.get("blocks")):
            if isinstance(block, dict) and block.get("kind") == "table" and tuple(_list(block.get("columns"))) == RISK_COLUMNS:
                yield block


def is_attention_overview(block):
    """Identify the current callback's ordinary table used by the overview plot."""
    return (isinstance(block, dict) and block.get("kind") == "table"
            and block.get("title") == ATTENTION_OVERVIEW_TITLE
            and tuple(_list(block.get("columns"))) == tuple(ATTENTION_OVERVIEW_COLUMNS))


def is_risk_overview(block):
    """Only the new publication's audited risk-result table opts in."""
    return (isinstance(block, dict) and block.get("kind") == "table"
            and block.get("title") == RISK_OVERVIEW_TITLE
            and tuple(_list(block.get("columns"))) == tuple(RISK_OVERVIEW_COLUMNS))


def _model_risk_plot(block, figure_number=3):
    """Display published model/rule values without inventing another scale.

    The publication layer admits audited model/rule results. Missing, Boolean and
    nonfinite values are not numbers, and this view never repopulates an empty
    overview from detail rows or from the earlier attention-index contract.
    """
    systems = {}
    for row in _list(block.get("rows")):
        cells = _cells(row)
        if len(cells) != 4:
            continue
        system, name, index, level = cells
        if _number(index) is None:
            continue
        systems.setdefault(_text(system), []).append(
            f'<div class="risk-plot-row model-risk-plot-row"><span>{_e(name)}</span>'
            f'<b class="risk-number">{_e(index)}</b><small>{_e(level)}</small></div>')
    if not systems:
        body = '<p class="plot-unavailable">暂无可量化的疾病风险结果。</p>'
    else:
        body = '<div class="risk-grid">' + ''.join(
            f'<div class="risk-system"><h4>{_e(system)}</h4>{"".join(rows)}</div>'
            for system, rows in systems.items()) + '</div>'
    return _figure(figure_number, RISK_OVERVIEW_TITLE, body,
                   "数值与风险等级对应各系统明细；评分方法、标尺及适用范围以正文说明为准。" if systems else "")


def _attention_overview(sections):
    for section in _list(sections):
        if not isinstance(section, dict) or section.get("id") != "disease_risks" or section.get("visible") is False:
            continue
        for block in _list(section.get("blocks")):
            if is_attention_overview(block):
                return block
    return None


def _overview_system_tables(block):
    """Group the published rows without deriving or reassessing any score."""
    systems = {}
    for row in _list(block.get("rows")):
        cells = _cells(row)
        if len(cells) != 4:
            continue
        system, name, index, level = cells
        systems.setdefault(_text(system), []).append([name, index, level, None])
    return [{"title": title, "rows": rows} for title, rows in systems.items()]


def _axis_diagram(sections, number=5, *, fixed=False):
    if fixed:
        # Education content must not shrink to the diseases supported in this
        # patient's assessment. All nodes are qualitative organ-axis concepts.
        pathways = (
            ("肝脏 · 肠–肝轴", "门静脉与胆汁酸循环", "肠源性产物、肝脏免疫与代谢的双向联系"),
            ("脑 · 肠–脑轴", "迷走神经、免疫与代谢信号", "应激、情绪及神经功能相关研究路径"),
            ("心脏与血管 · 肠–心轴", "循环炎症与微生物代谢物", "血管内皮、脂质代谢及心血管健康相关联系"),
            ("胰腺 · 肠–胰轴", "肠道激素、代谢与免疫信号", "胰岛功能、胰岛素反应及葡萄糖稳态相关联系"),
        )
        nodes = ''.join(
            f'<div class="axis-connection"><div class="axis-route">{_e(path)} '
            f'<span aria-hidden="true">↕</span></div><div class="axis-node">{_e(organ)}</div>'
            f'<p>{_e(target)}</p></div>' for organ, path, target in pathways)
        return _figure(number, "肠–X 轴：肠道与器官的双向联系",
                       '<div class="axis-hub">肠道屏障与菌群 ↔ 免疫、神经和代谢网络</div>'
                       '<div class="axis-grid fixed-axis-grid">' + nodes + '</div>',
                       "通用机制示意，不代表本次受检者的患病情况或个人风险；具体评估见第二章。")
    systems = [re.sub(r"^[23]\.\d+\s*", "", _text(block.get("title"))).removesuffix("疾病风险评估").removesuffix("风险评估")
               for block in _risk_tables(sections)]
    if not systems:
        return ""
    pathways = {
        "消化系统": ("门静脉与黏膜免疫", "肠–肝联系 / 消化道微环境"),
        "代谢系统": ("微生物代谢物与免疫信号", "肝、脂肪及肌肉的代谢调节"),
        "心脑血管系统": ("循环炎症与代谢信号", "血管内皮及动脉粥样硬化相关通路"),
        "精神系统": ("迷走神经 / 免疫 / 代谢物", "应激反应、情绪与脑肠交流"),
        "神经系统": ("免疫与神经信号交流", "神经炎症相关研究方向"),
        "免疫系统": ("黏膜免疫与抗原暴露", "免疫耐受与免疫激活平衡"),
        "肿瘤": ("局部微环境与代谢联系", "慢性炎症、细胞损伤相关研究"),
    }
    nodes = []
    for system in systems:
        path, target = pathways.get(system, ("免疫与代谢信号", "系统间双向联系"))
        nodes.append(f'<div class="axis-connection"><div class="axis-route">{_e(path)} <span aria-hidden="true">↓</span></div>'
                     f'<div class="axis-node">{_e(system)}</div><p>{_e(target)}</p></div>')
    return _figure(number, "肠道与多系统联系", '<div class="axis-hub">肠道屏障与菌群 ↔ 免疫、神经和代谢网络</div><div class="axis-grid">' + ''.join(nodes) + '</div>',
                   "肠-X 轴机制示意；所示为通用研究路径，不代表受检者已发生相应疾病或已确认因果关系。")


def _repair_diagram(number, note, *, xu=False, refined=False, word_review=False):
    principles = [("去除", "健康评估与诱因去除", "饮食、用药与疾病背景"),
                  ("改善", "肠道营养补充", "原料 · 能量 · 信号 · 抗炎"),
                  ("重建", "PFMT", "菌群重建及后续维持"),
                  ("修复", "MDF 靶向调节", "连接 · 上皮 · 内毒素相关环节"),
                  ("平衡", "生活方式干预", "饮食 · 运动 · 睡眠 · 压力")]
    if xu:
        principles = [("去除", "健康评估与诱因去除", "去除诱因，阻止持续性损伤。"),
                      ("改善", "肠道营养补充", "补充营养，改善肠上皮细胞能量代谢与黏膜微循环。"),
                      ("重建", "FMT", "重建肠道生态，黏液层快速修复与再生。"),
                      ("修复", "MDF 靶向调节", "靶向调节，巩固定植，修复紧密连接与免疫屏障。"),
                      ("平衡", "生活方式干预", "维持肠道稳态，实现机体整体健康。")]
    if refined and not word_review:
        principles = [("去除", "健康评估与诱因去除", "识别诱因，减少持续性损伤。"),
                      ("改善", "肠道营养补充", "支持上皮供能、合成更新与抗氧化防御。"),
                      ("重建", "FMT", "有适应证时评估菌群重建，支持生态恢复。"),
                      ("修复", "MDF 靶向调节", "结合菌株及底物证据，支持屏障功能。"),
                      ("平衡", "生活方式干预", "维护营养、活动与睡眠节律，持续随访。")]
    nodes = ''.join(f'<div class="principle-node"><span class="principle-number">0{index}</span>'
                    f'<strong>{_e(name)}</strong><b>{_e(action)}</b><small>{_e(detail)}</small></div>'
                    for index, (name, action, detail) in enumerate(principles, 1))
    return _figure(number, "肠道修复五阶段原则" if xu else "肠道修复五原则", '<div class="principle-grid">' + nodes + '</div>', note)


def _age_diagram(number, *, clean_review_layout=False):
    body = ('<div class="age-concept"><div class="age-context"><b>随年龄增长的研究背景</b>'
            '<span>菌群组成与代谢改变</span><span>免疫调节与修复能力变化</span><span>累计生活方式与疾病负担</span></div>'
            '<span class="age-arrow" aria-hidden="true">⇄</span><div class="age-outcome"><b>慢性低度炎症</b>'
            '<span>与组织稳态、代谢功能及衰弱相关的研究联系</span></div></div>'
            '<p class="age-modifiers">个体差异与调节因素：膳食质量、身体活动、睡眠、药物、疾病状态及遗传背景。</p>')
    note = ("研究背景示意：双向箭头表示相关因素可能相互影响，不表示确定的因果方向。"
            if clean_review_layout else
            "定性研究概念示意，不是受检者年龄趋势图；年龄不能直接换算为个人炎症水平，图中不设年龄倍数或未经核实的数值曲线。")
    return _figure(number, "年龄相关变化与炎症的联系", body, note)


def _risk_plot(sections, figure_number=7, *, xu=False, refined=False, clean_review_layout=False,
               canonical_dynamic_layout=False):
    systems = []
    qualitative_report = False
    overview = _attention_overview(sections) if canonical_dynamic_layout else None
    attention_mode = overview is not None or uses_attention_index(sections)
    if xu and not attention_mode:
        # A value alone carries no validated scale, units or denominator. Keep
        # independent/unknown model results in the unchanged four-column table;
        # only the explicit attention contract authorizes this 0–10 graphic.
        return ""
    # An explicit empty overview is meaningful: never repopulate it from
    # disease/model tables. Only callbacks without this block use the fallback.
    tables = _overview_system_tables(overview) if overview is not None else _risk_tables(sections)
    for block in tables:
        diseases = []
        for row in _list(block.get("rows")):
            cells = _cells(row)
            if len(cells) != 4:
                continue
            name, index, level, _mechanism = cells
            if attention_mode and (str(_mechanism or "").startswith("本项数值来自独立的已配置模型")
                                   or not (isinstance(level, str) and ("关注" in level or "优先评估" in level))):
                # Registered disease-model values use different semantics;
                # retain them in the table but never mix them into this scale.
                continue
            if level == "专项资料不足":
                qualitative_report = True
                continue  # The table retains the row; no empty attention card.
            number = _number(index)
            if number is not None and 0 <= number <= 10:
                index_label = "健康关注指数" if attention_mode else "指数"
                measure = (f'<span class="risk-track" role="img" aria-label="{_e(name)}：{index_label} {_e(index)} / 10">'
                           f'<span class="risk-fill" style="width:{number * 10:.4f}%"></span></span>'
                           f'<b class="risk-number">{_e(index)} / 10</b>')
            elif refined and index is None and level in {"随访关注", QUALITATIVE_FOLLOWUP_LEVEL}:
                # A documented qualitative follow-up is a result, not an
                # unperformed evaluation or an implicit zero-valued score.
                qualitative_report = True
                measure = '<span class="risk-qualitative">定性随访</span>'
            else:
                qualitative = "（定性）" in str(level) or level == "专项资料不足"
                qualitative_report |= qualitative
                measure = "" if qualitative else '<span class="risk-unassessed">未评估</span>'
            diseases.append(f'<div class="risk-plot-row"><span>{_e(name)}</span>{measure}<small>{_e(level)}</small></div>')
        if diseases:
            systems.append(f'<div class="risk-system"><h4>{_e(block.get("title"))}</h4>{"".join(diseases)}</div>')
    if not systems:
        return ""
    count = "一二三四五六七八九十"[len(systems) - 1] if 1 <= len(systems) <= 10 else str(len(systems))
    if attention_mode:
        note = ("横条与数字表示本次健康关注指数；“定性随访”项目不绘制分值横条。" if clean_review_layout else
                "自定义0–10分健康关注排序，尚未经过临床验证；不是患病概率，不能用于诊断、治疗决策或临床紧急分诊。"
                "本图与IBFC不是同一标尺；已注册模型结果仅在表格中单列，不混入本图。未列入不代表无风险。")
        return _figure(figure_number, "本次健康关注指数",
                       '<div class="risk-grid">' + ''.join(systems) + '</div>', note)
    return _figure(figure_number, "本次重点评估方向" if qualitative_report else f"{count}系统疾病风险评估总览",
                   '<div class="risk-grid">' + ''.join(systems) + '</div>',
                   "展示已有资料支持的关注方向；定性关注不等同患病概率，未列入本图的病种见下方逐项评估。" if qualitative_report else
                   "与下列表格使用同一风险指数；未评估项目不表示零风险，指数不等同患病概率。")


def before_section(section_id, sections, *, template_version=LEGACY_DISPLAY_TEMPLATE, clean_review_layout=False):
    capabilities = display_capabilities(template_version)
    if capabilities.template_risk_layout:
        return ""  # V11 renders only its explicit, publication-owned overview.
    if capabilities.canonical_dynamic_layout and _attention_overview(sections) is not None:
        # The current overview is rendered at its block position, after the
        # chapter introduction and before the first system subsection.
        return ""
    return _risk_plot(sections, 3 if chapter_layout(sections) == "reordered" else 7,
                      xu=capabilities.xu_review, refined=capabilities.refined_fixed_layout,
                      clean_review_layout=clean_review_layout) if section_id == "disease_risks" else ""


def after_block(section_id, block, sections, *, latest=False, template_version=None,
                clean_review_layout=False, source_stage_layout=False):
    """Exact template bindings; unknown block names get ordinary rendering."""
    title, kind = _text(block.get("title")), block.get("kind")
    selected = resolve_display_template({}, template_version) if template_version is not None else (
        PRE_XU_DISPLAY_TEMPLATE if latest else LEGACY_DISPLAY_TEMPLATE)
    capabilities = display_capabilities(selected)
    if template_version is not None:
        latest = capabilities.compact_layout
    xu = capabilities.xu_review
    refined = capabilities.refined_fixed_layout
    if capabilities.template_risk_layout and section_id == "disease_risks" and is_risk_overview(block):
        return _model_risk_plot(block)
    if (not capabilities.template_risk_layout and capabilities.canonical_dynamic_layout
            and section_id == "disease_risks" and is_attention_overview(block)):
        return _risk_plot(sections, 3, xu=xu, refined=refined,
                          clean_review_layout=clean_review_layout, canonical_dynamic_layout=True)
    if latest:
        if section_id == "overview":
            if kind == "table" and (tuple(_list(block.get("columns"))) == MARKER_COLUMNS
                    or capabilities.canonical_dynamic_layout
                    and tuple(_list(block.get("columns"))) == CANONICAL_MARKER_COLUMNS):
                return _indicator_plot(block, sections, 2, template_version=selected,
                                       clean_review_layout=clean_review_layout)
            if kind == "cards" and title == "肠道屏障功能评分（IBFC）":
                return _ibfc_plot(block, sections, 1, xu=xu, refined=refined,
                                  source_stage_layout=source_stage_layout,
                                  template_risk_layout=capabilities.template_risk_layout)
        if section_id == "education" and kind == "paragraph":
            for prefix, function, number in (("4.1", _barrier_diagram, 6), ("4.3", _inflammation_diagram, 7),
                                             ("4.6", _age_diagram, 9)):
                if title.startswith(prefix + " "):
                    return (function(number, clean_review_layout=capabilities.clean_review_layout)
                            if function is _age_diagram else
                            function(number, barrier_only=capabilities.barrier_only_layout)
                            if function is _inflammation_diagram else function(number))
            if title.startswith("4.5 "):
                return _axis_diagram(sections, 8, fixed=refined)
        if section_id == "repair_plan" and title.startswith("3.1 ") and kind in {"list", "paragraph"}:
            note = ("图示为五阶段干预目标，不表示本次已经实现相应修复效果；具体应用结合适应证及临床审核。"
                    if capabilities.word_review_layout else "修复原则总览；具体应用结合个体状况、适应证及临床审核。")
            return _repair_diagram(5, note, xu=xu, refined=refined,
                                   word_review=capabilities.word_review_layout)
        return ""
    reordered = chapter_layout(sections) == "reordered"
    if section_id == "overview":
        if kind == "table" and title in {"四项检测指标", "三项检测指标", "1.2 检测指标分项阐述"}:
            return _indicator_plot(block, sections)
        if kind == "cards" and title in {"肠道屏障功能评分（IBFC）", "1.3 肠道屏障功能评分（IBFC）与损伤分级"}:
            return _ibfc_plot(block, sections)
    if section_id == "education" and kind == "paragraph":
        chapter = "4" if reordered else "2"
        if re.match(rf"^{chapter}\.1(?:\s|[、.])", title):
            return _barrier_diagram(5 if reordered else 3)
        if re.match(rf"^{chapter}\.3(?:\s|[、.])", title):
            return _inflammation_diagram(6 if reordered else 4)
        if re.match(rf"^{chapter}\.5(?:\s|[、.])", title):
            return _axis_diagram(sections, 7 if reordered else 5)
        if re.match(rf"^{chapter}\.6(?:\s|[、.])", title):
            return _age_diagram(8 if reordered else 6)
    repair_heading = "3.1 肠道屏障修复 5 原则总览" if reordered else "4.1 修复总原则：五原则框架"
    if section_id == "repair_plan" and kind == "list" and title == repair_heading:
        note = "修复原则总览；具体应用应结合第一章评估结论与临床情况。" if reordered else "原则框架；具体措施及实施条件见本章个体化建议。"
        return _repair_diagram(4 if reordered else 8, note)
    return ""
