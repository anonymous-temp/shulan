"""Local-only JSON to HTML preview.  This module is never registered as a route."""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def render_report_html(payload: dict[str, Any]) -> str:
    """Render the fixed callback JSON without changing report content.

    The callback JSON is the sole source of truth. The preview may add HTML
    labels, escaping and empty-state markers, but it must not infer, rewrite,
    filter, truncate or supplement medical content.
    """

    if (payload.get("diagnosis_summary") or {}).get("clinical_context", {}).get("report.version") == "clinical-v1":
        return _clinical_report_html(payload)
    subject = payload.get("subject") or {}
    sample = payload.get("sample") or {}
    assessment = payload.get("healthAssessment") or {}
    summary = payload.get("diagnosis_summary") or {}
    basis = payload.get("diagnostic_basis") or {}
    advice = payload.get("diagnostic_advice") or {}
    findings = basis.get("abnormal_findings") or []
    microbes = [item for item in findings if item.get("finding_type") == "microbe"]
    diseases = [item for item in findings if item.get("finding_type") == "disease"]
    references = payload.get("evidence") or []
    clinical_context = summary.get("clinical_context") or {}

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>肠道菌群医学辅助解读报告</title>
<style>
:root{{--ink:#263431;--muted:#61706c;--line:#d9e1df;--accent:#0b887d;--panel:#f5f7f7}}
*{{box-sizing:border-box}}html,body{{max-width:100%;overflow-x:hidden}}body{{margin:0;background:#edf1f0;color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1120px;margin:28px auto;background:#fff;padding:42px 48px;box-shadow:0 8px 30px #17352d14}}
h1{{margin:0 0 6px;font-size:28px}}h2{{font-size:20px;margin:38px 0 16px;padding-left:12px;border-left:4px solid var(--accent)}}h3{{font-size:16px;margin:22px 0 10px}}
.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:24px 0}}.meta div,.card{{background:var(--panel);border:1px solid var(--line);padding:12px 14px;border-radius:6px}}
    .label{{color:var(--muted);font-size:13px}}.table-scroll{{width:100%;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}}table{{width:100%;min-width:940px;border-collapse:collapse;table-layout:fixed}}th,td{{border:1px solid var(--line);padding:10px;vertical-align:top;text-align:left;overflow-wrap:anywhere}}th{{background:#f1f4f3;font-weight:600}}
    .refs{{white-space:normal;color:#0b6d65}}.risk{{margin:12px 0;padding:14px;border-left:3px solid #6a9f99;background:var(--panel)}}ul{{margin:8px 0;padding-left:22px}}a{{color:#0b6d65;word-break:break-all}}
    .assessment-head{{display:grid;grid-template-columns:minmax(280px,420px) minmax(0,1fr);gap:24px;align-items:center}}.assessment-head>*{{min-width:0}}.assessment-score{{font-size:42px;color:var(--accent);font-weight:700;line-height:1}}.assessment-score small{{font-size:15px;color:var(--muted)}}.radar{{width:100%;max-width:420px;height:auto;display:block;margin:auto}}.dimension-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:20px}}.dimension-card{{min-width:0;border:1px solid var(--line);border-radius:8px;padding:16px;background:#fff;overflow:hidden}}.dimension-card p,.dimension-card li{{max-width:100%;overflow-wrap:anywhere;word-break:break-word}}.dimension-title{{display:flex;justify-content:space-between;gap:12px;align-items:baseline}}.dimension-title>*{{min-width:0}}.dimension-value{{font-size:24px;color:var(--accent);font-weight:700;flex:none}}.dimension-value.unavailable{{color:var(--muted)}}.pill{{display:inline-block;padding:2px 9px;border-radius:999px;background:#e5f5f2;color:#0b6d65;font-size:12px}}.missing{{color:#9a5d00}}.basis-list{{font-size:13px;color:var(--muted)}}
    .assessment-abnormal{{margin-top:24px}}.assessment-abnormal table{{min-width:760px}}.indicator-status{{display:inline-block;min-width:76px;padding:3px 9px;border-radius:4px;text-align:center;font-size:13px}}.indicator-status.attention{{background:#fff0cf;color:#996000}}.indicator-status.priority_attention{{background:#ffe0d6;color:#a43b1b}}
    .related-diseases{{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding-left:0}}.related-disease{{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;background:#f2f4f4;font-size:13px}}.risk-dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}.risk-dot.high{{background:#d62828}}.risk-dot.medium{{background:#ef8b2c}}.risk-dot.low{{background:#3776b9}}.risk-dot.pending{{background:#333}}
    .evidence-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.evidence-card{{min-width:0;border:1px solid #79cfc7;border-radius:9px;padding:14px;background:#fff}}.evidence-card-head{{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start}}.evidence-number{{display:inline-grid;place-items:center;min-width:28px;height:28px;padding:0 7px;border-radius:4px;background:#17b9aa;color:#fff;font-weight:700}}.evidence-grade{{white-space:nowrap;font-size:13px;font-weight:600}}.evidence-grade.strong{{color:#d62828}}.evidence-grade.medium{{color:#d97706}}.evidence-grade.weak{{color:#1d71b8}}.evidence-line{{margin:10px 0 0;overflow-wrap:anywhere}}.evidence-label{{color:#0b887d;font-weight:600}}.evidence-reference{{font-size:13px;color:var(--muted)}}
@media(max-width:760px){{main{{margin:0;padding:24px 16px}}.meta{{grid-template-columns:1fr}}.assessment-head{{grid-template-columns:1fr}}.dimension-grid{{grid-template-columns:1fr}}table{{font-size:13px}}}}
@media(max-width:900px){{.evidence-grid{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>肠道菌群医学辅助解读报告</h1>
<div class="label">受检者与样本信息</div>
<div class="meta">{_meta(subject, sample, payload.get("generated_at"))}</div>
{_section("健康管理评估指数", _health_assessment(assessment))}
{_section("疾病关联风险", _disease_table(diseases) or '<div class="card">当前检测数据未提供可展示的疾病或健康关联项目。</div>')}
{_section("异常菌群解读", (f'<div class="card">{_e(clinical_context.get("菌群关联判定说明"))}</div>' if clinical_context.get("菌群关联判定说明") else "") + _microbe_table(microbes) + (f'<div class="card">{_e(clinical_context.get("异常菌群概况"))}</div>' if clinical_context.get("异常菌群概况") else ""))}
{_section("异常指标", _assessment_abnormal_indicators(assessment.get("abnormalIndicators") or []))}
{_section("健康总结", _health_management(summary, advice))}
{_section("证据依据", _references(references))}
    </main></body></html>"""


def _meta(
    subject: dict[str, Any],
    sample: dict[str, Any],
    generated_at: Any = None,
    *, show_generated: bool = True,
) -> str:
    rows = [
        ("姓名", subject.get("name")),
        ("年龄", subject.get("age")),
        ("性别", subject.get("gender")),
        ("采样日期", sample.get("sample_date")),
        ("菌群报告日期", sample.get("report_date")),
    ]
    if show_generated:
        rows.append(("本次评估生成时间", _calendar_date(generated_at)))
    return "".join(
        f'<div><span class="label">{_e(label)}</span><br>{_display(value)}</div>'
        for label, value in rows
    )


def _calendar_date(value: Any) -> str | None:
    """Keep preview output aligned with the callback's YYYY-MM-DD contract."""

    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if not match:
        return text or None
    return (
        f"{int(match.group(1)):04d}-"
        f"{int(match.group(2)):02d}-"
        f"{int(match.group(3)):02d}"
    )


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_e(title)}</h2>{body}</section>" if body else ""


def _health_assessment(assessment: dict[str, Any], *, show_technical_details: bool = True) -> str:
    dimensions = assessment.get("dimensions") or []
    if not dimensions:
        return ""
    overall_score = assessment.get("displayOverallScore")
    overall = (
        str(overall_score)
        if assessment.get("overallScoreAvailable") and overall_score not in (None, "")
        else "--"
    )
    head = (
        '<div class="assessment-head">'
        + _radar_svg(dimensions)
        + '<div class="card">'
        + f'<div class="assessment-score">{_e(overall)}<small> / 100</small></div>'
        + f'<p><span class="pill">{_display(assessment.get("levelLabel"))}</span></p>'
        + f"<p>{_display(assessment.get('summary'))}</p>"
        + "</div></div>"
    )
    cards = []
    for item in dimensions:
        available = bool(item.get("scoreAvailable"))
        score = item.get("displayScore") if available else "--"
        evidence = item.get("evidenceUsed") or []
        rules = item.get("ruleBasis") or []
        missing = item.get("missingEvidence") or []
        conflicts = item.get("conflicts") or []
        related_diseases = item.get("relatedDiseases") or []
        evidence_html = "".join(
            "<li>"
            + _e(
                f"{value.get('item')}："
                + _value_with_unit(value.get("value"), value.get("unit"))
            )
            + (_e(f"（{value.get('date')}）") if value.get("date") else "")
            + "</li>"
            for value in evidence
        )
        rule_html = _grouped_rule_basis(rules)
        missing_html = "".join(f"<li>{_e(value)}</li>" for value in missing)
        conflict_html = "".join(f"<li>{_e(value)}</li>" for value in conflicts)
        related_disease_html = "".join(
            '<li class="related-disease">'
            + f'<span class="risk-dot {_e(value.get("riskLevel") or "pending")}"></span>'
            + f'<span>{_display(value.get("name"))}</span>'
            + f'<span class="label">{_display(value.get("riskLabel"))}</span>'
            + "</li>"
            for value in related_diseases
        )
        cards.append(
            '<article class="dimension-card">'
            + '<div class="dimension-title">'
            + f"<strong>{_display(item.get('name'))}</strong>"
            + f'<span class="dimension-value{"" if available else " unavailable"}">{_display(score)}</span>'
            + "</div>"
            + f'<p><span class="pill">{_display(item.get("levelLabel"))}</span></p>'
            + f"<p>{_display(item.get('explanation'))}</p>"
            + (
                f'<h3>关联疾病</h3><ul class="related-diseases">{related_disease_html}</ul>'
                if related_disease_html
                else ""
            )
            + (
                f"<h3>本次评估参考指标</h3><ul>{evidence_html}</ul>"
                if evidence_html
                else ""
            )
            + (
                f'<h3>评分标准说明</h3><ul class="basis-list">{rule_html}</ul>'
                if rule_html and show_technical_details
                else ""
            )
            + (
                f'<h3 class="missing">建议补充的信息</h3><ul class="missing">{missing_html}</ul>'
                if missing_html
                else ""
            )
            + (
                f'<h3 class="missing">建议核实的信息</h3><ul class="missing">{conflict_html}</ul>'
                if conflict_html and show_technical_details
                else ""
            )
            + "</article>"
        )
    return head + '<div class="dimension-grid">' + "".join(cards) + "</div>"


def _assessment_abnormal_indicators(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        severity = str(item.get("severity") or "")
        rows.append(
            "<tr>"
            + f"<td>{_display(item.get('item'))}</td>"
            + f"<td>{_display(_value_with_unit(item.get('value'), item.get('unit')))}</td>"
            + f"<td>{_display(item.get('referenceRange'))}</td>"
            + '<td><span class="indicator-status '
            + _e(severity)
            + '">'
            + _display(item.get("status"))
            + "</span></td>"
            + f"<td>{_display(item.get('clinicalSignificance'))}</td>"
            + "</tr>"
        )
    return (
        '<div class="assessment-abnormal">'
        '<div class="table-scroll"><table><thead><tr>'
        "<th>检测项目</th><th>评估值</th>"
        "<th>参考范围</th><th>结果评价</th><th>临床意义</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>"
    )


def _radar_svg(dimensions: list[dict[str, Any]]) -> str:
    import math

    size, center, radius = 440, 220, 132
    angles = [-math.pi / 2 + index * 2 * math.pi / 6 for index in range(6)]

    def point(angle: float, scale: float) -> tuple[float, float]:
        return center + math.cos(angle) * radius * scale, center + math.sin(
            angle
        ) * radius * scale

    polygons = []
    for level in (0.25, 0.5, 0.75, 1.0):
        points = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (point(angle, level) for angle in angles)
        )
        polygons.append(
            f'<polygon points="{points}" fill="none" stroke="#d9e1df" stroke-width="1"/>'
        )
    axes = "".join(
        f'<line x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}" stroke="#d9e1df"/>'
        for x, y in (point(angle, 1) for angle in angles)
    )
    values = []
    labels = []
    for index, angle in enumerate(angles):
        item = dimensions[index] if index < len(dimensions) else {}
        raw_score = item.get("score") or 0
        score = max(0, min(100, float(raw_score)))
        values.append(point(angle, score / 100))
        label_x, label_y = point(angle, 1.25)
        anchor = (
            "middle"
            if abs(math.cos(angle)) < 0.2
            else "start"
            if math.cos(angle) > 0
            else "end"
        )
        label = str(item.get("name") or "")
        display_value = item.get("displayScore")
        display = (
            str(display_value)
            if item.get("scoreAvailable") and display_value not in (None, "")
            else "--"
        )
        labels.append(
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="{anchor}" font-size="12" fill="#445450">'
            + _e(label)
            + f'<tspan x="{label_x:.1f}" dy="15">{_e(display)}</tspan></text>'
        )
    value_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in values)
    return (
        f'<svg class="radar" viewBox="0 0 {size} {size}" role="img" aria-label="六维健康管理评估雷达图">'
        + "".join(polygons)
        + axes
        + f'<polygon points="{value_points}" fill="#10a99a26" stroke="#0b887d" stroke-width="2"/>'
        + "".join(labels)
        + "</svg>"
    )


def _clinical_observation_tables(context: dict[str, str]) -> str:
    """Read fixed column keys, never infer abnormalities from prose or names."""
    prefix = "report.observation."
    rows = []
    for key in context:
        if key.startswith(prefix) and key.endswith(".kind"):
            base = key.removesuffix("kind")
            rows.append({column: context.get(base + column, "") for column in (
                "kind", "name", "value", "unit", "reference", "status", "date", "source", "problems",
            )})
    indicators = []
    imaging = []
    for row in rows:
        if row["kind"] == "indicator":
            indicators.append('<tr>' + ''.join(f'<td>{_e(value)}</td>' for value in (
                row["name"], _value_with_unit(row["value"], row["unit"]),
                row["reference"] or "原资料未提供", row["status"] or "原资料未标注",
            )) + '</tr>')
        elif row["kind"] == "imaging":
            imaging.append('<tr>' + ''.join(f'<td>{_e(value)}</td>' for value in (
                row["name"], row["value"],
            )) + '</tr>')

    def table(headers: tuple[str, ...], items: list[str]) -> str:
        return '<div class="table-scroll"><table><thead><tr>' + ''.join(
            f'<th>{_e(header)}</th>' for header in headers
        ) + '</tr></thead><tbody>' + ''.join(items) + '</tbody></table></div>'

    result = '<h4>异常指标检测结果</h4>'
    if indicators:
        result += table(("指标", "本次结果", "参考范围", "判定"), indicators)
    elif context.get("report.observation.version") == "1":
        result += '<p>当前资料未提取出可单列的临床异常指标；这不代表所有检查均正常。</p>'
    else:
        result += '<p>这份历史回调尚未提供异常指标分栏数据，需重新生成后查看；不从历史记录文字推断异常。</p>'
    if imaging:
        result += '<h4>影像与功能检查发现</h4>' + table(("检查项目", "检查结果"), imaging)
    return result


def _clinical_priority_table(problems: list[dict[str, Any]], context: dict[str, Any] | None = None) -> str:
    """Reuse the approved problem priorities and actions without regrading."""
    if not problems:
        return '<p>现有资料暂未形成明确的临床问题列表，不自动判为低风险。</p>'
    rows = []
    for problem in problems:
        priority_color = {"优先复核": "#a72f31", "建议复核": "#946000", "常规关注": "#326491"}.get(problem.get("priority"), "#48646a")
        explanation = (context or {}).get(f"report.problem.{problem.get('item_id')}.significance") or (problem.get("tips") or {}).get("practical_goal") or '\n'.join(problem.get("recommendations") or [])
        rows.append('<tr>'
            + f'<td><span class="badge" style="color:{priority_color}">{_e(problem.get("priority") or "待复核")}</span></td>'
            + f'<td>{_e(problem.get("problem"))}</td>'
            + f'<td>{_e(explanation) or "当前尚未形成有支持的行动说明。"}</td></tr>')
    return ('<div class="table-scroll"><table class="priority-table"><thead><tr>'
        '<th>级别</th><th>问题 / 事项</th><th>一句话说明</th>'
        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def _clinical_report_html(payload: dict[str, Any]) -> str:
    """Four-part preview: every medical paragraph comes from the callback."""
    from modules.report.clinical_report import ANALYSIS_TITLES, SECTION_TITLES

    summary = payload.get("diagnosis_summary") or {}
    context = summary.get("clinical_context") or {}
    advice = payload.get("diagnostic_advice") or {}
    findings = (payload.get("diagnostic_basis") or {}).get("abnormal_findings") or []
    problems = [f for f in findings if f.get("finding_type") == "disease"]
    microbes = [f for f in findings if f.get("finding_type") == "microbe"]
    assessment = payload.get("healthAssessment") or {}
    section_refs = {r.get("topic"): r.get("evidence_refs") or []
        for r in advice.get("health_recommendations") or []}

    def paragraph(text: Any) -> str:
        return '<p>' + _e(text or "当前没有可展示的内容。 ").replace('\n', '<br>') + '</p>'

    def node(code: str, title: str | None = None, *, level: int = 3) -> str:
        return f'<div class="node"><h{level}>{_e(title or SECTION_TITLES[code])}</h{level}>{paragraph(context.get("report.section." + code))}{citations(section_refs.get("report.section." + code, []))}</div>'

    def citations(numbers: list[int]) -> str:
        return ' '.join(f'<a href="#ref-{n}">[{n}]</a>' for n in numbers if isinstance(n, int))

    def records(values: list[str]) -> str:
        return '<ul class="facts">' + ''.join(f'<li>{_e(v)}</li>' for v in values) + '</ul>' if values else paragraph("目前没有对应的检测记录。")

    def auxiliary_table(kind: str, legacy: list[str]) -> str:
        if context.get("report.auxiliary.version") != "1":
            return records(legacy)
        rows = []
        for key, value in context.items():
            if key.startswith("report.auxiliary.") and key.endswith(".kind") and value == kind:
                base = key.removesuffix("kind")
                values = (context.get(base + "name"),
                    _value_with_unit(context.get(base + "value", ""), context.get(base + "unit", "")),
                    context.get(base + "reference") or "原资料未提供", context.get(base + "status") or "原资料未标注")
                rows.append('<tr>' + ''.join(f'<td>{_e(value)}</td>' for value in values) + '</tr>')
        if not rows:
            return paragraph("目前没有对应的检测记录。")
        return ('<div class="table-scroll"><table><thead><tr>' + ''.join(f'<th>{label}</th>' for label in
            ("项目", "本次结果", "参考范围", "判定"))
            + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')

    reported_values = [v for k, v in context.items() if k.startswith("report.fact.self_reported.")]
    reported_html = '<h4>自述病史与症状（待核实）</h4>' + records(reported_values) if reported_values else ''
    microbe_values = [i.get("value") or "" for m in microbes for i in m.get("indicators") or []]
    analyses = []
    priorities = []
    for index, problem in enumerate(problems, start=1):
        pid = problem.get("item_id")
        content = '<h4>一、现状是什么</h4>' + records([i.get("value") or "" for i in problem.get("indicators") or []])
        for code, title in ANALYSIS_TITLES.items():
            content += f'<h4>{_e(title)}</h4>' + paragraph(context.get(f"report.problem.{pid}.{code}"))
        content += citations(problem.get("evidence_refs") or [])
        analyses.append(f'<article class="issue" id="problem-{index}"><h3>2.{index} {_e(problem.get("problem"))}</h3>{content}</article>')
        # The generator owns prioritization. Display all returned priorities;
        # a renderer must neither cap at three nor invent a second ranking.
        actions = (problem.get("tips") or {}).get("practical_goal") or '\n'.join(problem.get("recommendations") or [])
        priorities.append(f'<li><strong>{_e(problem.get("problem"))}</strong>' + paragraph(actions) + '</li>')
    priority_overview = '<ol>' + ''.join(priorities) + '</ol>' if priorities else paragraph('现有资料尚未形成可排序的优先关注事项。')
    plan_values = advice.get("Plan") or {}
    phase_labels = ("未来1-2周", "未来1个月", "未来2-3个月", "未来6-12个月")
    # Unavailable plans are omitted from advice, but their truthful section
    # explanation remains in the callback. Keep the fixed roadmap visible.
    plans = ''.join(f'<div class="node"><h4>{_e(label)}</h4>'
        + paragraph(plan_values.get(label) or context.get(f"report.section.phase_{index}")) + '</div>'
        for index, label in enumerate(phase_labels, 1))
    exams = ''.join(f'<li><strong>{_e(e.get("name"))}</strong> · {_e(e.get("purpose"))}{paragraph(e.get("reason"))}{citations(e.get("evidence_refs") or [])}</li>' for e in advice.get("recommended_examinations") or [])
    mapping = []
    claims = {}
    for ref in payload.get("evidence") or []:
        # The public DTO binds facts to a reference, not to each sentence.
        # Preserve this granularity instead of multiplying its entire fact
        # set onto every individual claim, which invents finer associations.
        for claim in [str(ref.get("conclusion") or "")]:
            if not claim.strip():
                continue
            row = claims.setdefault(claim, {"refs": [], "facts": [], "types": []})
            row["refs"].append(ref["number"])
            row["facts"].extend(ref.get("patient_related_facts") or [])
            if ref.get("study_type") or ref.get("evidence_type"):
                row["types"].append(ref.get("study_type") or ref["evidence_type"])
    for claim, row in claims.items():
        related = list(dict.fromkeys(row["facts"]))
        mapping.append(f'<tr><td>{paragraph(claim)}</td>'
            + f'<td>{records(related) if related else paragraph("一般说明，未绑定个人检测结果。")}</td>'
            + f'<td>{_e("、".join(dict.fromkeys(row["types"])) or "来源未注明")}</td>'
            + f'<td>{citations(list(dict.fromkeys(row["refs"])))}</td></tr>')
    evidence_mapping = ('<div class="table-scroll"><table><thead><tr><th>结论与建议</th><th>该依据关联的来源事实</th><th>证据类型</th><th>参考依据</th></tr></thead><tbody>'
        + ''.join(mapping) + '</tbody></table></div>') if mapping else paragraph('尚无可用于医学扩展解释的外部引用；原始检测事实保留在第一部分。')
    references = []
    for ref in payload.get("evidence") or []:
        title = _e(ref.get("title") or "标题未提供")
        url = str(ref.get("url") or "")
        if url.startswith(("https://", "http://")):
            title = f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{title}</a>'
        references.append(f'<li id="ref-{ref["number"]}"><span>[{ref["number"]}]</span> {title}</li>')
    # Structured, evidence-bound nutrition rows stay plain text in the fixed
    # callback. Only this explicit grammar becomes a table; prose is not guessed.
    nutrition_lines, nutrition_rows = [], []
    labels = ("营养素：", "每日剂量：", "剂型/理由：", "服用方式：", "注意事项：")
    for line in str(context.get("report.section.nutrition") or "").splitlines():
        cells = line.split("｜")
        numbers = [r["number"] for r in payload.get("evidence") or [] if line in str(r.get("conclusion") or "").splitlines()]
        if len(cells) == len(labels) and all(c.startswith(label) for c, label in zip(cells, labels)) and numbers:
            values = [_e(c.removeprefix(label)) for c, label in zip(cells, labels)]
            values.insert(4, citations(numbers))
            nutrition_rows.append('<tr>' + ''.join(f'<td>{v}</td>' for v in values) + '</tr>')
        else:
            nutrition_lines.append(line)
    nutrition_html = paragraph('\n'.join(nutrition_lines))
    if nutrition_rows:
        nutrition_html += '<div class="table-scroll"><table><thead><tr>' + ''.join(f'<th>{label}</th>' for label in
            ("营养素", "每日剂量", "剂型 / 理由", "服用方式", "循证依据", "注意事项")) + '</tr></thead><tbody>' + ''.join(nutrition_rows) + '</tbody></table></div>'
    nutrition_html += citations(section_refs.get("report.section.nutrition", []))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e((payload.get('subject') or {}).get('name') or '')}个性化健康评估报告</title>
<style>
:root{{--accent:#0b887d;--panel:#f3f8f7}}.assessment-head{{display:grid;grid-template-columns:minmax(280px,420px) minmax(0,1fr);gap:24px;align-items:center}}.assessment-head>*{{min-width:0}}.assessment-score{{font-size:42px;color:var(--accent);font-weight:700;line-height:1}}.assessment-score small{{font-size:15px;color:var(--muted)}}.card{{background:var(--panel);border:1px solid var(--line);padding:20px;border-radius:12px}}.dimension-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:20px}}.dimension-card{{min-width:0;border:1px solid var(--line);border-radius:12px;padding:20px}}.dimension-card p,.dimension-card li{{overflow-wrap:anywhere}}.dimension-title{{display:flex;justify-content:space-between;gap:12px;align-items:baseline}}.dimension-value{{font-size:28px;color:var(--accent);font-weight:700}}.dimension-value.unavailable,.label{{color:var(--muted)}}.pill{{display:inline-block;padding:2px 9px;border-radius:999px;background:#e5f5f2;color:#0b6d65;font-size:13px}}.missing{{color:#9a5d00}}.basis-list{{font-size:13px;color:var(--muted)}}.related-diseases{{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding-left:0}}.related-disease{{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;background:#f2f4f4;font-size:13px}}.risk-dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}.risk-dot.high{{background:#d62828}}.risk-dot.medium{{background:#ef8b2c}}.risk-dot.low{{background:#3776b9}}.risk-dot.pending{{background:#333}}.table-scroll{{max-width:100%;overflow-x:auto;margin:16px 0}}table{{width:100%;min-width:760px;border-collapse:collapse;table-layout:fixed;font-size:14px}}th,td{{border:1px solid var(--line);padding:12px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:var(--pale)}}.priority-table th:first-child{{width:13%}}.priority-table th:nth-child(2){{width:20%}}@media(max-width:720px){{.assessment-head,.dimension-grid{{grid-template-columns:1fr}}}}
:root{{--teal:#0b887d;--ink:#243d42;--muted:#69818a;--line:#dae7e7;--pale:#f3f8f7}}*{{box-sizing:border-box}}body{{margin:0;background:#eef4f3;color:var(--ink);font:16px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}main{{max-width:1160px;margin:32px auto;padding:42px 48px;background:white;border:1px solid var(--line);border-radius:18px}}h1{{font-size:30px;margin:0}}h2{{font-size:24px;border-left:5px solid var(--teal);padding-left:15px;margin:12px 0 24px}}h3{{font-size:19px;margin:12px 0}}h4{{font-size:16px;margin:18px 0 4px;color:var(--teal)}}p{{margin:8px 0;white-space:normal;overflow-wrap:anywhere}}a{{color:var(--teal);overflow-wrap:anywhere}}nav{{display:flex;flex-wrap:wrap;gap:10px;margin:26px 0}}nav a{{text-decoration:none;padding:7px 18px;background:var(--pale);border-radius:30px}}section{{border-top:1px solid var(--line);padding:30px 0}}.meta,.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}}.meta{{grid-template-columns:repeat(3,minmax(0,1fr));margin:20px 0;color:var(--muted)}}.hero,.node,.issue{{padding:20px 24px;margin:14px 0;border:1px solid var(--line);border-radius:12px;min-width:0}}.hero{{background:var(--pale);font-size:18px;border-left:4px solid var(--teal)}}.node h3:first-child{{margin-top:0}}.badge{{display:inline-block;background:#e7f4f1;color:var(--teal);padding:1px 10px;border-radius:20px;font-size:13px}}.radar{{width:100%;max-width:460px;display:block;margin:auto}}.facts{{padding-left:22px;margin:10px 0;color:#48646a}}li{{margin:8px 0;overflow-wrap:anywhere}}.muted{{color:var(--muted)}}.ref-list{{padding-left:22px}}.ref-list li{{padding:10px 0;border-bottom:1px solid var(--line)}}@media(max-width:720px){{main{{margin:0;padding:24px 18px;border-radius:0}}.grid,.meta{{grid-template-columns:1fr}}h1{{font-size:25px}}h2{{font-size:21px}}.node,.issue,.hero{{padding:16px}}}}@media print{{body{{background:white}}main{{border:0;padding:0;margin:0;max-width:none}}nav{{display:none}}.node,.issue{{break-inside:avoid}}}}
.priority-table{{min-width:0}}@media(max-width:720px){{.priority-table{{font-size:13px}}.priority-table th,.priority-table td{{padding:8px}}.priority-table th:first-child{{width:23%}}.priority-table th:nth-child(2){{width:30%}}.priority-table .badge{{padding:1px 5px}}}}
</style></head><body><main><h1>{_e((payload.get('subject') or {}).get('name') or '')}个性化健康评估报告</h1>
<div class="meta">{_meta(payload.get('subject') or {}, payload.get('sample') or {}, show_generated=False)}</div>
<nav><a href="#health">健康综合评估</a><a href="#causes">主要问题分析</a><a href="#management">干预方案</a><a href="#evidence">循证支持</a></nav>
<section id="health"><h2>第一部分 健康综合评估</h2>
<h3>1.1 健康总览</h3><div class="hero">{paragraph(summary.get('conclusion'))}</div><h4>六维健康管理评估</h4>{_health_assessment(assessment, show_technical_details=False)}
<h3>1.2 需要优先关注的事项</h3>{priority_overview}
<h3>1.3 异常指标总览</h3>{_clinical_observation_tables(context)}{reported_html}<h4>肠道菌群检测结果</h4>{auxiliary_table('microbe', microbe_values)}
<h3>1.4 问题分级清单（轻重缓急）</h3>{_clinical_priority_table(problems, context)}</section>
<section id="causes"><h2>第二部分 主要问题分析</h2>{''.join(analyses) or paragraph('目前缺少可逐项分析的临床问题。')}{node('microbiome_analysis', f'2.{len(problems) + 1} 肠道菌群分析（辅助说明）')}</section>
<section id="management"><h2>第三部分 干预方案</h2>{node('strategy', '3.1 总体思路')}
<h3>3.2 支柱一：个性化生活方式干预</h3>{node('diet', level=4)}{node('exercise', level=4)}{node('sleep', level=4)}
<h3>3.3 支柱二：个性化肠道菌群与肠屏障调节</h3>{node('microbiome_rationale', level=4)}{node('probiotics', level=4)}{node('prebiotics', level=4)}{node('barrier', level=4)}{node('microbiome_followup', level=4)}
<div class="node"><h3>3.4 支柱三：个性化营养素补充</h3>{nutrition_html}</div>
<h3>3.5 阶段化实施路线图</h3>{plans or paragraph('当前尚未形成分阶段安排。')}
{node('clinical_followup', '3.6 医疗就医建议清单')}<ul>{exams or '<li>当前未形成适用的优先补充检查项目。</li>'}</ul>
{node('monitoring', '3.7 复查与监测计划')}{node('alerts', level=4)}{node('avoid', level=4)}</section>
<section id="evidence"><h2>第四部分 循证支持</h2>{node('evidence_reading', '4.1 证据体系说明')}<h3>4.2 结论—证据对照表</h3>{evidence_mapping}{node('glossary', '4.3 名词科学释义')}<h3>4.4 主要参考依据</h3><ul class="ref-list" style="list-style:none;padding-left:0">{''.join(references) or '<li>当前没有可公开展示的参考文献。</li>'}</ul>{node('limits', '4.5 局限性与免责声明')}</section></main></body></html>'''


def _microbe_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        risks = item.get("related_diseases_or_adverse_effects") or []
        tips = item.get("tips") or {}
        association_refs = [
            number for risk in risks for number in risk.get("evidence_refs") or []
        ]
        evidence_content = _labeled_refs(
            (
                ("医学解读依据", item.get("interpretation_evidence_refs")),
                ("相关影响依据", association_refs),
            ),
            fallback=item.get("evidence_refs"),
        )
        if evidence_content == "—":
            evidence_content = _display(tips.get("practical_goal"))
        rows.append(
            "<tr>"
            f"<td>{_display(item.get('problem'))}</td>"
            f"<td>{_indicators(item.get('indicators') or [])}</td>"
            f"<td>{_display(item.get('biological_mechanism'))}</td>"
            f'<td class="refs">{evidence_content}</td></tr>'
        )
    if not rows:
        return ""
    return (
        '<div class="table-scroll"><table><thead><tr><th>异常菌名称</th><th>本次检测结果</th><th>医学解读</th><th>循证依据</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _disease_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        tips = item.get("tips") or {}
        recommendations = item.get("recommendations") or []
        # Completed results are represented by the assessment and abnormal-
        # indicator sections. This column renders only recommendation strings
        # already present in the callback JSON.
        evaluation_content = "<br><br>".join(
            _display(value) for value in recommendations
        )
        if not evaluation_content:
            evaluation_content = _display(tips.get("practical_goal"))
        rows.append(
            "<tr>"
            f"<td>{_display(item.get('problem'))}</td>"
            f"<td>{_display(tips.get('special_reminder'))}</td>"
            f"<td>{_display(item.get('biological_mechanism'))}</td>"
            f"<td>{evaluation_content}</td>"
            f'<td class="refs">{_labeled_refs((("医学解读依据", item.get("interpretation_evidence_refs")), ("检查建议依据", item.get("recommendation_evidence_refs"))), fallback=item.get("evidence_refs"))}</td></tr>'
        )
    if not rows:
        return ""
    return (
        '<div class="table-scroll"><table><thead><tr><th>风险</th><th>建议</th><th>医学解释与关联风险</th><th>建议补充检查</th><th>循证依据</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _health_management(summary: dict[str, Any], advice: dict[str, Any]) -> str:
    parts = []
    # The callback keeps the conclusion, enterotype and health-level summary
    # for API consumers.  The patient preview deliberately avoids repeating
    # them here: the score section and the dedicated gut sections already
    # provide that context.  Only the comprehensive portrait is shown once.
    clinical_context = summary.get("clinical_context") or {}
    health_profile = clinical_context.get("综合健康画像")
    if health_profile not in (None, ""):
        parts.append(
            f'<div class="card"><strong>综合健康画像</strong><br>{_e(health_profile)}</div>'
        )

    recommendations = advice.get("health_recommendations") or []
    if recommendations:
        parts.append(
            "<h3>建议关注方向</h3><ul>"
            + "".join(
                "<li>"
                + (
                    f'<span class="pill">{_display(item.get("priority"))}</span> '
                    if item.get("priority")
                    else ""
                )
                + (
                    f"<strong>{_display(item.get('topic'))}</strong>："
                    if item.get("topic")
                    else ""
                )
                + _display(item.get("content"))
                + (
                    f' <span class="refs">{_ref_text(item.get("evidence_refs"))}</span>'
                    if item.get("evidence_refs")
                    else ""
                )
                + "</li>"
                for item in recommendations
            )
            + "</ul>"
        )

    # The callback JSON is the single source of truth.  The preview only
    # renders the evidence-backed, canonical examinations already selected by
    # the report pipeline; it must never infer or append an examination here.
    examinations = advice.get("recommended_examinations") or []
    if examinations:
        parts.append(
            "<h3>建议补充检查</h3><ul>"
            + "".join(
                "<li>"
                + f"<strong>{_display(item.get('name'))}</strong>"
                + (
                    f"（{_display(item.get('purpose'))}）"
                    if item.get("purpose")
                    else ""
                )
                + (
                    f"：{_display(item.get('reason'))}"
                    if item.get("reason")
                    else ""
                )
                + (
                    f' <span class="refs">{_ref_text(item.get("evidence_refs"))}</span>'
                    if item.get("evidence_refs")
                    else ""
                )
                + "</li>"
                for item in examinations
                if item.get("name")
            )
            + "</ul>"
        )
    return "".join(parts)


def _references(items: list[dict[str, Any]]) -> str:
    cards = []
    for item in items:
        link = item.get("url")
        title = _e(item.get("title"))
        if link:
            title = f'<a href="{_e(link)}">{title}</a>'
        grade = item.get("evidence_grade")
        grade_class = {"强": "strong", "中": "medium", "弱": "weak"}.get(
            str(grade or ""), ""
        )
        reference = "；".join(
            str(value)
            for value in (
                item.get("source"),
                item.get("publication_date"),
                item.get("study_type"),
            )
            if value not in (None, "")
        )
        patient_facts = "；".join(
            _e(value) for value in item.get("patient_related_facts") or []
        )
        cards.append(
            '<article class="evidence-card">'
            '<div class="evidence-card-head">'
            f'<span class="evidence-number">{_e(item.get("number"))}</span>'
            f'<strong>{_display(item.get("topic"))}</strong>'
            f'<span class="evidence-grade {grade_class}">证据等级：{_display(grade)}</span>'
            "</div>"
            f'<p class="evidence-line"><span class="evidence-label">结论：</span>{_display(item.get("conclusion"))}</p>'
            f'<p class="evidence-line"><span class="evidence-label">证据：</span>{patient_facts or "—"}</p>'
            f'<p class="evidence-line evidence-reference"><span class="evidence-label">参考：</span>{title}'
            + (f"；{_e(reference)}" if reference else "")
            + "</p></article>"
        )
    return f'<div class="evidence-grid">{"".join(cards)}</div>' if cards else ""


def _ref_text(values: Any) -> str:
    return (
        " ".join(f"[{_e(item)}]" for item in dict.fromkeys(values or []))
        or "—"
    )


def _labeled_refs(
    groups: tuple[tuple[str, Any], ...],
    *,
    fallback: Any,
) -> str:
    parts = [f"{_e(label)}：{_ref_text(values)}" for label, values in groups if values]
    return "<br>".join(parts) if parts else _ref_text(fallback)


def _grouped_rule_basis(values: list[dict[str, Any]]) -> str:
    """Show each rule source once while retaining every indicator boundary."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for value in values:
        source = str(value.get("source") or "")
        version = str(value.get("version") or "")
        key = (source or str(value.get("ruleId") or ""), version)
        groups.setdefault(key, []).append(value)
    rendered = []
    for (source, version), items in groups.items():
        heading = "；".join(part for part in (source, version) if part)
        boundaries = "".join(
            "<li>"
            + _display(item.get("indicator"))
            + (
                "：" + _display(item.get("boundary"))
                if item.get("boundary") not in (None, "")
                else ""
            )
            + "</li>"
            for item in items
        )
        rendered.append(
            "<li>"
            + (f"<strong>{_e(heading)}</strong>" if heading else "评分依据")
            + (f"<ul>{boundaries}</ul>" if boundaries else "")
            + "</li>"
        )
    return "".join(rendered)


def _indicators(items: list[dict[str, Any]]) -> str:
    if not items:
        return "—"
    rendered = []
    for item in items:
        details = [
            _value_with_unit(item.get("value"), item.get("unit")),
            item.get("status"),
        ]
        name = item.get("name")
        rendered.append(
            (f"<strong>{_display(name)}</strong><br>" if name not in (None, "") else "")
            + "<br>".join(
                _display(value) for value in details if value not in (None, "")
            )
        )
    return "<br><br>".join(rendered)


def _value_with_unit(value: Any, unit: Any) -> str:
    if value in (None, ""):
        return ""
    value_text = str(value)
    unit_text = "" if unit in (None, "") else str(unit)
    compact = lambda text: re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()
    if unit_text and not compact(value_text).endswith(compact(unit_text)):
        value_text += unit_text
    return value_text


def _display(value: Any) -> str:
    return "—" if value in (None, "") else _e(value)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def external_preview_path(output: Path) -> Path:
    """Resolve a preview path and reject writes inside the source tree."""

    resolved = output.expanduser().resolve()
    project_root = Path(__file__).resolve().parents[2]
    if resolved.is_relative_to(project_root):
        raise ValueError(
            "preview output must be outside the project directory; "
            "use a temporary or dedicated artifact directory"
        )
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a local report JSON preview")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    try:
        output = external_preview_path(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report_html(payload), encoding="utf-8")


if __name__ == "__main__":
    main()
