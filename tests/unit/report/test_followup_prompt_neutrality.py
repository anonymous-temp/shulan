"""Exercise actual follow-up user templates and their bound system prompt."""
import json
from types import SimpleNamespace

import pytest

from agents.graphs.report import create_report_followup_profile
from agents.llm import AionLLMGateway, LLMRequest
from modules.report import reports
from modules.report.fallback_policy import ReportFallbackPolicy


_OLD_FORCED_NORMAL = (
    "风险等级时一律取最轻一档", "无任何阳性发现时必须判为", "无阳性发现按“低风险”",
    "整体处于可维持稳态水平", "未提示需要即时临床干预的异常发现",
    "无任何阳性发现时必须取最轻一档", "任何情况下都不得输出空字符串、空数组或空对象",
    "绝对禁止提示缺失", "固定必填字段按上面的“无依据时的合法出口”照常生成实质内容",
    "可用信息通常仅限受检者姓名", "本次输入数据**不包含任何肠道菌群检测结果**",
    "固定字段必须全部给出实质内容", "不得输出空字符串或空数组",
)

_INVENTED_NORMALITY_OR_DOSES = (
    "低风险", "一度失衡", "可代偿", "未见异常", "整体正常", "整体平稳",
    "没有需要立即处理的事项", "1600-1900", "30-35g", "1000-2000 IU",
)

_SOURCE_CASES = [
    pytest.param({"subject": {"name": "测试", "age": 26}}, id="identity_only"),
    pytest.param({
        "subject": {"name": "测试"},
        "diagnostic_basis": {"abnormal_findings": [{
            "item_id": "ct", "finding_type": "disease", "problem": "胸部CT结果",
            "indicators": [{"name": "胸部CT", "value": "2026-09-24 两肺未见明显异常"}],
        }]},
    }, id="one_normal_exam"),
    pytest.param({
        "subject": {"name": "测试"},
        "diagnostic_basis": {"abnormal_findings": [{
            "item_id": "hs_crp", "finding_type": "disease", "problem": "hs-CRP待核对",
            "indicators": [{"name": "hs-CRP", "value": "4.2 mg/L"}],
        }]},
    }, id="one_lab_result"),
]


@pytest.fixture(autouse=True)
def fresh_prompt_cache(monkeypatch):
    monkeypatch.setattr(reports, "_FOLLOWUP_PROMPT_CACHE", {})
    monkeypatch.setattr(reports, "_FALLBACK_RULES_CACHE", None)
    monkeypatch.setattr(reports, "_EXPLAIN_PROMPT_CACHE", {})
    monkeypatch.setattr(reports, "_EXPLAIN_RULES_CACHE", None)


def offline_gateway(captured, response="{}"):
    class Client:
        provider = "offline"
        model = "offline"

        def __init__(self, system):
            self.system = system

        async def complete_async(self, content, **kwargs):
            captured.append({"system": self.system, "user": content})
            return SimpleNamespace(content=response, reasoning_content=None, usage=None)

    def factory(provider, **kwargs):
        assert provider == "offline" and kwargs["context"] is False
        return Client(kwargs["system_content"])

    prompt_root = reports._FOLLOWUP_PROMPT_DIR.parents[1]
    return AionLLMGateway(provider="offline", api_key="unused", default_model="offline",
                          client_factory=factory,
                          prompt_renderer=lambda name, **kwargs: (prompt_root / name).read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"subject": {"name": "测试", "age": 26}},
    {"subject": {"name": "测试"}, "hospitalData": [{"itemName": "胸部CT", "examResult": "两肺未见明显异常", "reportTime": "2026-09-24"}]},
    {"subject": {"name": "测试"}, "hospitalData": [{"itemName": "hs-CRP", "testResultValue": "4.2", "unit": "mg/L"}]},
])
async def test_opinion_request_has_consistent_system_and_rendered_user_rules(monkeypatch, data):
    captured = []
    gateway = offline_gateway(captured)
    profile = create_report_followup_profile()

    async def model(prompt, *, task_type):
        assert task_type == "opinion_summary"
        return (await gateway.complete(LLMRequest(profile=profile, content=prompt))).content

    monkeypatch.setattr(reports, "safe_complete_async", model)
    token = reports._ACTIVE_SCOPE_NOTE.set(reports._build_scope_note(data))
    try:
        result = await reports.opinion_summary(data)
    finally:
        reports._ACTIVE_SCOPE_NOTE.reset(token)
    assert len(captured) == 1
    system, user = captured[0]["system"], captured[0]["user"]
    assert system == (reports._FOLLOWUP_PROMPT_DIR / "system.txt").read_text(encoding="utf-8")
    assert json.dumps(data, ensure_ascii=False) in user
    for text in (system, user):
        assert not any(forbidden in text for forbidden in _OLD_FORCED_NORMAL)
        assert "固定字段名称全部保留" in text
        assert "缺失标量返回 null" in text
        assert "数组返回 []" in text
        assert "不能仅凭年龄套用默认剂量" in text
    assert "该标记不代表全部临床资料缺失" in user
    assert "不代表已有检查正常" in user
    assert result["risk"] is None and result["evidence_refs"] == []
    # Parse the actual rendered structure example, not a separately retyped schema.
    example = json.JSONDecoder().raw_decode(user[user.rfind("\n{") + 1:])[0]
    assert set(example) == {"conclusion", "clinical_context", "risk", "evidence_refs"}
    assert example["conclusion"] is None and example["risk"] is None and example["evidence_refs"] == []
    assert example["clinical_context"] == {"临床定位": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("part_no", [None, 1, 2, 3, 4], ids=["opening", "part1", "part2", "part3", "part4"])
@pytest.mark.parametrize("data", _SOURCE_CASES)
async def test_manual_requests_use_current_contexts_and_neutral_rules(monkeypatch, part_no, data):
    captured = []
    # These cases inspect a rendered request and a valid neutral response.
    # Empty model output is exercised through the full report path below.
    response = ({"opening_script": {"self_intro": "我帮您逐项核对已有报告资料。"}} if part_no is None else
                {"script": {"body": ["我们按已有报告资料逐项核对，未展开的内容需进一步确认。"]}})
    gateway = offline_gateway(captured, json.dumps(response, ensure_ascii=False))
    profile = create_report_followup_profile()
    task = "explain_opening" if part_no is None else f"explain_part{part_no}"

    async def model(prompt, *, task_type):
        assert task_type == task
        return (await gateway.complete(LLMRequest(profile=profile, content=prompt))).content

    monkeypatch.setattr(reports, "safe_complete_async", model)
    contexts = reports.build_explain_contexts(data)
    kwargs = {"issues": [], "allowed": reports.explain_allowed_numbers(contexts)}
    result = (await reports._explain_opening(contexts, **kwargs) if part_no is None else
              await reports._explain_section(part_no, contexts, **kwargs))

    assert len(captured) == 1
    system, user = captured[0]["system"], captured[0]["user"]
    assert system == (reports._FOLLOWUP_PROMPT_DIR / "system.txt").read_text(encoding="utf-8")
    assert reports.explain_dumps(contexts["meta"]) in user
    assert reports.explain_dumps(contexts[f"part{part_no}"] if part_no else contexts["part1"]["top_three"]) in user
    for text in (system, user):
        assert not any(forbidden in text for forbidden in _OLD_FORCED_NORMAL)
        assert "固定字段名称全部保留" in text
        assert "数组返回 []" in text
        assert "不能仅凭年龄套用默认剂量" in text
        assert "真实正常结果" in text
        assert "第一人称口语体" in text
    assert "手册的话术字符串缺少可用内容时返回空字符串" in user
    assert "不足时不凑数" in user
    assert not {"diagnostic_summary", "diagnostic_basis", "diagnostic_advice", "risk"} & result.keys()

    # Inspect the actual task's schema example; retired explain_summary/basis/advice
    # templates are not part of the five model calls that construct a manual.
    example = json.JSONDecoder().raw_decode(user[user.rfind("\n{") + 1:])[0]
    if part_no is None:
        assert set(example) == {"opening_script", "wrap_up_script"}
        assert result["wrap_up_script"]["three_things"] == contexts["part1"]["top_three"]
    else:
        assert example["part_no"] == result["part_no"] == part_no
        assert example["title"] == result["title"] == reports.EXPLAIN_SECTION_TITLES[part_no]
        assert example["script"]["kind"] == result["script"]["kind"] == "script"
        if part_no == 2:
            assert [item["problem"] for item in result["problems"]] == [item["problem"] for item in contexts["part2"]["problems"]]
            assert all(item["evidence_refs"] == [] for item in result["problems"])


@pytest.mark.parametrize("name", ["_fallback_rules.txt", "system.txt", "opinion_summary.txt", "_explain_rules.txt"])
def test_revised_templates_do_not_require_normality_or_nonempty_fabrication(name):
    text = (reports._FOLLOWUP_PROMPT_DIR / name).read_text(encoding="utf-8")
    assert not any(forbidden in text for forbidden in _OLD_FORCED_NORMAL)
    assert ("null" in text or "空字符串" in text) and "[]" in text
    assert "真实正常结果" in text or "若仅有正常检查" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["opinion", "explain"])
async def test_empty_actual_model_outputs_produce_failed_neutral_report(monkeypatch, kind):
    captured = []
    gateway = offline_gateway(captured)
    profile = create_report_followup_profile()

    tasks = []

    async def model(prompt, *, task_type):
        tasks.append(task_type)
        return (await gateway.complete(LLMRequest(profile=profile, content=prompt))).content

    monkeypatch.setattr(reports, "safe_complete_async", model)
    token = reports._ACTIVE_SCOPE_NOTE.set(reports._build_scope_note({}))
    try:
        body, status = await reports._generate_followup_module(
            kind, lambda: getattr(reports, f"{kind}_report")({}, "u", "s"),
            data={}, user_id="u", session_id="s",
            policy=ReportFallbackPolicy(llm_attempts=1, retry_backoff_seconds=()),
        )
    finally:
        reports._ACTIVE_SCOPE_NOTE.reset(token)
    expected_tasks = ({"opinion_summary", "opinion_basis", "opinion_advice"} if kind == "opinion" else
                      {"explain_opening", "explain_part1", "explain_part2", "explain_part3", "explain_part4"})
    assert set(tasks) == expected_tasks and len(tasks) == len(captured) == len(expected_tasks)
    assert body["status"] == "failed" and status == "fallback_empty"
    public_text = json.dumps(body, ensure_ascii=False)
    assert not any(value in public_text for value in _INVENTED_NORMALITY_OR_DOSES)
    assert body["evidence"] == []
    if kind == "opinion":
        assert body["diagnostic_basis"]["abnormal_findings"] == []
        assert body["diagnosis_summary"]["risk"] is None
        assert all("统一缺失处理与事实边界" in item["user"] for item in captured)
    else:
        assert not {"diagnostic_summary", "diagnostic_basis", "diagnostic_advice"} & body.keys()
        assert [section["part_no"] for section in body["sections"]] == [1, 2, 3, 4]
        assert body["sections"][1]["problems"] == []
        assert body["closing"]["wrap_up_script"]["three_things"] == []
        assert all("解读手册全局规则" in item["user"] for item in captured)
    assert all("统一缺失处理" in item["system"] for item in captured)


def test_scope_gate_logic_is_unchanged_and_does_not_deny_other_clinical_data():
    assert reports._build_scope_note({"diagnostic_basis": {"abnormal_findings": [{"problem": "existing"}]}}) == ""
    note = reports._build_scope_note({"hospitalData": [{"itemName": "hs-CRP", "testResultValue": 4.2}]})
    assert note
    assert "实际提供的炎症等检验仍可按其原始名称和结果描述" in note
    assert "不把其自动归因于菌群失衡或肠道屏障受损" in note
