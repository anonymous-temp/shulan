"""HTML projection checks, separate from clinical generation assertions."""


import pytest

from adapters.report.callback import CallbackClient
from adapters.report.dev_preview import render_report_html
from modules.report.clinical_report import (
    callback_sections, collect_source_facts,
    ensure_document_fallback, organize_problems,
)
from modules.report.schemas import (
    DiseaseRisksSection, FixedReportCallback,
)
from tests.unit.report.test_clinical_report import (
    Model, source,
)


def test_evaluate_callback_uses_v2_data_without_legacy_internal_fields():
    payload = {
        "userId": "u",
        "sessionId": "s",
        "finalReportId": "batch",
        "status": "partial_success",
        "partialReasons": ["DOCUMENT_SOURCE_INCOMPLETE"],
        "report": {
            "subject": {"name": "synthetic", "sex": "女", "age": 42},
            "sample": {"sampleId": "sample"},
            "evaluationData": {},
            "clinicalDocument": {"internal": "must stay private"},
        },
    }
    public = CallbackClient(url="https://callback.test/evaluate").build_body(payload)
    assert public["schemaVersion"] == "evaluate.v2.0"
    assert public["finalReportId"] == "batch"
    assert public["status"] == "partial_success"
    assert public["partialReasons"] == ["DOCUMENT_SOURCE_INCOMPLETE"]
    assert public["subject"] == {"name": "synthetic", "gender": "女", "age": 42}
    assert public["sample"]["sampleId"] == "sample"
    assert "medicalAssessment" in public["data"]
    assert "report" not in public and "diagnosis_summary" not in public


def test_auxiliary_table_projection_keeps_values_separate_from_clinical_measurements():
    doc = collect_source_facts(*source())
    ensure_document_fallback(doc)
    before = doc.model_dump()
    body = callback_sections(doc, DiseaseRisksSection(), [])
    context = body["diagnosis_summary"]["clinical_context"]
    assert context["report.auxiliary.version"] == "1"
    assert context["report.auxiliary.taxon-0.value"] == "0%"
    assert context["report.auxiliary.taxon-0.kind"] == "microbe"
    assert doc.model_dump() == before
    html = render_report_html(body)
    assert 'Synthetic taxon 0' in html and '<td>0%</td>' in html
    assert '功能检测参考资料' not in html


def test_preview_places_section_references_and_groups_exact_claims_without_rewriting():
    from copy import deepcopy
    body = {"diagnosis_summary": {"clinical_context": {"report.version": "clinical-v1", "report.section.diet": "已核验饮食说明"}},
        "diagnostic_advice": {"health_recommendations": [{"topic": "report.section.diet", "evidence_refs": [1, 2]}]},
        "evidence": [{"number": 1, "title": "标题一", "conclusion": "共同结论\n独立结论", "study_type": "队列研究", "patient_related_facts": ["来源一"]},
            {"number": 2, "title": "标题二", "conclusion": "共同结论", "study_type": "综述", "patient_related_facts": ["来源二"]}]}
    before = deepcopy(body)
    html = render_report_html(body)
    assert '已核验饮食说明</p><a href="#ref-1">[1]</a> <a href="#ref-2">[2]</a>' in html
    assert html.count('<td><p>共同结论</p>') == 1
    assert '共同结论<br>独立结论' in html and '来源一' in html and '来源二' in html
    assert '队列研究、综述' not in html  # No invented sentence-level cross-reference binding.
    assert body == before


@pytest.mark.asyncio
async def test_patient_reported_history_and_symptoms_survive_without_clinical_measurements():
    from modules.report.clinical_report import collect_reported_facts
    request, parsed = source()
    request.hospital_data = []
    parsed.taxa = []
    doc = collect_source_facts(request, parsed, session_context={"qaHistory": [
        {"question": "最近有腹胀吗？", "answer": "有"},
        {"question": "有何既往病史？", "answer": "2024年医生告诉我有胃炎。"}]})
    class Reported(Model):
        async def generate(self, payload):
            if payload["task"] == "extract_reported_clinical_facts":
                return {"items": [{"id": s["id"], "assertions": [{
                    "name": "腹胀" if s["text"] == "有" else "胃炎", "quote": s["text"],
                    "subject": "self", "assertion": "affirmed", "kind": "symptom" if s["text"] == "有" else "history",
                    "timeQuote": "2024年" if "2024" in s["text"] else ""}]} for s in payload["items"]]}
            if payload["task"] == "organize_clinical_problems":
                return {"problems": []}
            return await super().generate(payload)
    await organize_problems(Reported(), doc)
    assert len(doc.facts) == len(doc.problems) == 2
    assert {p.certainty for p in doc.problems} == {"self_reported"}
    assert {f.kind for f in doc.facts} == {"self_reported"}
    assert {f.date for f in doc.facts} == {None, "2024年"}
    assert all("用户自述" in p.title for p in doc.problems)
    before = [f.model_dump() for f in doc.facts]
    await collect_reported_facts(Reported(), doc)
    assert before == [f.model_dump() for f in doc.facts]
    ensure_document_fallback(doc)
    body = callback_sections(doc, DiseaseRisksSection(), [])
    context = body["diagnosis_summary"]["clinical_context"]
    assert len([k for k in context if k.startswith("report.fact.self_reported.")]) == 2
    assert not any(k.startswith("report.fact.clinical.") for k in context)
    html = render_report_html(body)
    assert "自述病史与症状（待核实）" in html and "2024年" in html
    assert "用户自述，未核验" in html


def test_public_callback_drops_unbound_references_and_escapes_source_html():
    from modules.report.schemas import ReportSourceFact
    doc = collect_source_facts(*source())
    doc.facts.append(ReportSourceFact(factId="x", kind="clinical", name="<script>", value="&value", source="synthetic", abnormal=True))
    ensure_document_fallback(doc)
    sections = callback_sections(doc, DiseaseRisksSection(), [{"number": 88, "title": "Unbound"}])
    assert sections["evidence"] == []
    body = FixedReportCallback.model_validate({"userId": "u", "sessionId": "s", "status": "partial_success", **sections}).model_dump(mode="json", by_alias=True)
    html = render_report_html(body)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Unbound" not in html


@pytest.mark.parametrize('problem_count', [0, 2, 5])
def test_preview_follows_fixed_outline_without_mutating_or_inventing_content(problem_count):
    from copy import deepcopy
    body = {"diagnosis_summary": {"conclusion": "真实总览", "clinical_context": {
        "report.version": "clinical-v1", "report.section.microbiome_analysis": "真实辅助说明",
    }}, "diagnostic_basis": {"abnormal_findings": [
        {"finding_type": "disease", "item_id": f"p-{i}", "problem": f"来源问题{i}",
         "priority": "建议复核", "recommendations": [f"已核验行动{i}"], "indicators": []}
        for i in range(problem_count)
    ]}, "evidence": [{"number": 1, "title": "真实文献标题", "conclusion": "真实结论",
        "patient_related_facts": ["真实依据"]}]}
    before = deepcopy(body)
    html = render_report_html(body)
    headings = [
        '第一部分 健康综合评估', '1.1 健康总览', '1.2 需要优先关注的事项',
        '1.3 异常指标总览', '1.4 问题分级清单（轻重缓急）', '第二部分 主要问题分析',
        *[f'2.{i + 1} 来源问题{i}' for i in range(problem_count)],
        f'2.{problem_count + 1} 肠道菌群分析（辅助说明）', '第三部分 干预方案',
        '3.1 总体思路', '3.2 支柱一：个性化生活方式干预', '3.3 支柱二：个性化肠道菌群与肠屏障调节',
        '3.4 支柱三：个性化营养素补充', '3.5 阶段化实施路线图', '3.6 医疗就医建议清单',
        '3.7 复查与监测计划', '第四部分 循证支持', '4.1 证据体系说明', '4.2 结论—证据对照表',
        '4.3 名词科学释义', '4.4 主要参考依据', '4.5 局限性与免责声明',
    ]
    offsets = [html.index(f'>{title}</h') for title in headings]
    assert offsets == sorted(offsets)
    assert '查看对应问题分析' not in html
    priority_part = html.split('1.2 需要优先关注的事项')[1].split('1.3 异常指标总览')[0]
    assert priority_part.count('<li>') == problem_count
    assert '大便隐血弱阳性' not in html and '混合型高脂血症' not in html
    assert '<th>结论与建议</th><th>该依据关联的来源事实</th><th>证据类型</th><th>参考依据</th>' in html
    assert '真实结论' in html and '真实依据' in html and '真实文献标题' in html
    assert '<details' not in html
    assert body == before


def test_priority_table_reuses_original_priorities_and_actions():
    from adapters.report.dev_preview import _clinical_priority_table
    problems = [{"priority": "建议复核", "problem": "检查发现",
        "indicators": [{"value": "原始结果 < 5"}], "recommendations": ["携带既往资料复核。"]}]
    html = _clinical_priority_table(problems)
    assert "建议复核" in html and "携带既往资料复核。" in html
    assert "&lt; 5" not in html
    assert '<th>级别</th><th>问题 / 事项</th><th>一句话说明</th>' in html
    assert "P1" not in html and "高风险" not in html


def test_observation_table_handles_missing_and_legacy_metadata_without_guessing():
    from adapters.report.dev_preview import _clinical_observation_tables
    assert "不代表所有检查均正常" in _clinical_observation_tables({"report.observation.version": "1"})
    assert "历史回调" in _clinical_observation_tables({"report.fact.clinical.a": "某检测：偏高"})
    assert "某检测" not in _clinical_observation_tables({"report.fact.clinical.a": "某检测：偏高"})
