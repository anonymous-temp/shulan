# -*- coding: utf-8 -*-
"""用一份「评估报告」JSON 直接驱动 modules/report/reports.py:_push_report()。

绕过 Tornado、评估报告生成、MongoDB 落库和真实回调，只跑后续报告：
- 输入 type=evaluate：报告解读 / 诊断意见 / 临床医嘱三份（干预方案 intervene 已停用）；
- 输入 type=gutBarrier：肠道与炎症报告解读 / 诊断意见 / 临床医嘱三份。

用法
----
    # 1) 不调模型，只验证编排和兜底是否把报告填满（最快，不花 token）
    python -m tests.unit.tools.run_followups api_test/report_zewen3.json --mode fake

    # 1b) 肠道与炎症报告（输入 type=gutBarrier），默认输出到 api_test/out/gutBarrier/，
    #     不会覆盖评估报告生成的 report__opinion.json / report__instruction.json
    python -m tests.unit.tools.run_followups api_test/callback.json --mode fake
    python -m tests.api.scripts.run_followups api_test/callback.json --mode api

    # 2) 模型全部抛异常，验证第三层兜底
    python -m tests.unit.tools.run_followups api_test/report_zewen3.json --mode dead

    # 3a) 真实调模型（经 aion，需要私有包 + 完整 .env）
    python -m tests.api.scripts.run_followups api_test/report_zewen3.json --mode llm

    # 3b) 真实调模型（走 OpenAI 兼容 HTTP 接口，只需要 httpx，没装 aion 也能用）
    python -m tests.api.scripts.run_followups api_test/report_zewen3.json --mode api

    # 4) 真实模型 + 真实产品目录 + 真实回调（等价于线上，慎用）
    python -m tests.api.scripts.run_followups evaluate.json --mode llm --catalog real --deliver http

    # 5) 在终端直接打印回调报文的 JSON（对接联调时用）
    python -m tests.api.scripts.run_followups api_test/evaluate_empty.json --mode api --print-json
    python -m tests.api.scripts.run_followups api_test/evaluate_empty.json --mode api --print-json instruction
    # 只看某一段：
    python -m tests.api.scripts.run_followups api_test/evaluate_empty.json --mode api \
        --print-json instruction --print-path non_drug_intervention.supplements

输入文件必须是报告的**回调正文**（type=evaluate 或 type=gutBarrier 的那个 JSON），
也就是同事那边 POST 给回调地址的 body，不是内部中间结构。

输出：每份报告写到 --out 目录，文件名 report__<type>.json。
gutBarrier 输入时还会把生成的 opinion / instruction 与 api_test/out/ 下评估报告的
同名样例逐字段比对，确认对外报文结构一致（只比字段路径与类型，不比内容）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from tests.unit._support.harnesses.followup_options import heartbeat

BASE_DIR = Path(__file__).resolve().parents[4]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _ensure_utils_log() -> None:
    """让本脚本在没装私有包 loongspears 的机器上也能跑起来。

    仓库的 ``utils/log.py`` 依赖 ``loongspears.utils.log``，而且会连带导入
    ``config.env``（需要完整的 .env）。跑 --mode fake / dead 时这两样都用不上，
    所以导入失败就用标准库 logging 顶替。

    只改当前进程的 sys.modules，不动仓库里的 utils/log.py；
    --mode llm / --catalog real / --deliver http 仍然会真实导入 config.env，
    配置有问题时照样会在那里报错，不会被这里掩盖。
    """
    import types

    try:
        import utils.log  # noqa: F401,PLC0415
        return
    except Exception as error:  # ModuleNotFoundError / EnvConfigError / ...
        shim = types.ModuleType("utils.log")
        shim.getLogger = logging.getLogger  # type: ignore[attr-defined]
        shim.global_logger = logging.getLogger("global")  # type: ignore[attr-defined]
        sys.modules["utils.log"] = shim
        if "utils" not in sys.modules:
            package = types.ModuleType("utils")
            package.__path__ = [str(BASE_DIR / "utils")]  # type: ignore[attr-defined]
            sys.modules["utils"] = package
        sys.modules["utils"].log = shim  # type: ignore[attr-defined]
        print(f"⚠️  utils.log 导入失败（{type(error).__name__}: {error}）")
        print("    已用标准库 logging 顶替，仅影响本脚本进程。")
        print("    要还原真实日志行为，在当前环境安装私有依赖：")
        print('      pip install "loongspears.utils @ git+https://gitee.com/loongspears/utils"')
        print()


_ensure_utils_log()

import modules.report.reports as reports_mod  # noqa: E402
from modules.report.reports import _push_report  # noqa: E402

# 本脚本依赖兜底改造后的 reports.py。旧版本跑起来只会在中途抛 AttributeError，
# 这里提前拦一下，直接说清楚要同步哪几个文件。
_REQUIRED_SYMBOLS = ("_source_fact_summary", "_coalesce", "_render_followup_prompt")
_MISSING_SYMBOLS = [n for n in _REQUIRED_SYMBOLS if not hasattr(reports_mod, n)]
if _MISSING_SYMBOLS:
    print("❌ modules/report/reports.py 是改造前的旧版本"
          f"（缺少 {', '.join(_MISSING_SYMBOLS)}）。")
    print(f"   实际加载路径: {reports_mod.__file__}")
    print("   请把下面这几个文件一起同步到本机再跑：")
    print("     modules/report/reports.py                     （改写）")
    print("     modules/report/followup_defaults.py           （新增）")
    print("     modules/report/empty_report_factory.py        （改写）")
    print("     prompts/report/followups/_fallback_rules.txt  （新增）")
    sys.exit(2)
if not (BASE_DIR / "prompts" / "report" / "followups" / "_fallback_rules.txt").is_file():
    print("❌ 缺少 prompts/report/followups/_fallback_rules.txt，"
          "所有子任务的提示词渲染都会失败。请一并同步。")
    sys.exit(2)

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "report" / "followups" / "system.txt"

# 源报告 type=evaluate 时生成 instruction / opinion / explain；
# type=gutBarrier 时生成 explain_gutBarrier / opinion / instruction。
EVALUATE_REPORT_TYPES = ("instruction", "opinion", "explain")  # intervene 已停用
GUT_BARRIER_REPORT_TYPES = ("explain_gutBarrier", "opinion", "instruction")
REPORT_TYPES = EVALUATE_REPORT_TYPES  # 兼容旧引用；实际按输入类型取 report_types_for()
CN_NAME = {
    "opinion": "诊断意见",
    "explain": "报告解读",
    "instruction": "临床医嘱",
    "intervene": "干预方案",
    "explain_gutBarrier": "肠道与炎症报告解读",
}
SOURCE_CN_NAME = {"evaluate": "评估报告", "gutBarrier": "肠道与炎症报告"}


def report_types_for(data) -> tuple[str, ...]:
    """按源报告类型决定这次应该产出哪几份。"""
    return (GUT_BARRIER_REPORT_TYPES
            if str((data or {}).get("type") or "") == "gutBarrier"
            else EVALUATE_REPORT_TYPES)


# ================= 模型网关的三种形态 =================

class FakeModelGateway:
    """不调模型，每次返回空串：用来验证「模型不给内容时三份报告仍然填满」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str:
        self.calls.append(task_type)
        # 三套规则文件任选其一：临床报告用《全局兜底规则》，
        # 解读手册用《解读手册全局规则》，肠道与炎症再追加一份专属规则。
        assert any(marker in user_prompt for marker in (
            "全局兜底规则", "解读手册全局规则", "肠道与炎症报告专属规则",
        )), f"{task_type} 的提示词没拼上规则"
        return ""


class DeadModelGateway:
    """每次调用都抛异常：用来验证模型完全不可达时的第三层兜底。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_text(self, *, task_type: str, user_prompt: str) -> str:
        self.calls.append(task_type)
        raise RuntimeError("simulated LLM outage")


# ---- 等待提示：真实模型一次调用可能要几分钟，没有输出时容易误以为卡住 ----


# ================= 业务网关（产品目录 + 回调） =================

MOCK_CATALOG = {
    "state": 200,
    "results": {
        "microecologicalTargetedRegulator": [
            {
                "productCode": "MDF-TEST-001",
                "requiredStrains": ["固定菌株A"],
                "optionalStrains": ["候选菌株B", "候选菌株C", "候选菌株D"],
                "productSpecification": "2g/袋",
                "shelfLife": "12个月",
                "compositeEfficacyComponents": "修吉泰1号复合营养补充剂、后生元(嗜粘蛋白阿克曼菌AKM-Lab-01)、抗性膳食纤维",
                "usageInstructions": "每日一次，每次1包，温水冲服。",
            }
        ],
        "fecalMicrobiotaTransplantationCapsule": [
            {
                "productCode": "PFMT-TEST-M-1",
                "productName": "健康人源肠道菌群移植胶囊（肠型I·男）",
                "productStatus": "启用",
                "applicableGender": "男",
                "applicableEnterotype": "type_1",
                "applicableEnterotypeName": "拟杆菌型",
                "viableCountPerCapsule": "1×10^11 CFU",
                "totalViableCountPerBox": "3×10^12 CFU",
                "usageInstructions": "单次10粒，连续3天，冷水送服。",
            },
            {
                "productCode": "PFMT-TEST-F-3",
                "productName": "健康人源肠道菌群移植胶囊（肠型III·女）",
                "productStatus": "启用",
                "applicableGender": "女",
                "applicableEnterotype": "type_3",
                "applicableEnterotypeName": "瘤胃球菌型",
                "viableCountPerCapsule": "1×10^11 CFU",
                "totalViableCountPerBox": "3×10^12 CFU",
                "usageInstructions": "单次10粒，连续3天，冷水送服。",
            },
        ],
    },
}


class LocalGateway:
    """产品目录和回调都可以在 mock / real 之间切换。"""

    def __init__(self, *, catalog: str, deliver: str, out_dir: Path, http_gateway=None) -> None:
        self.catalog_mode = catalog
        self.deliver_mode = deliver
        self.out_dir = out_dir
        self.delivered: list[str] = []
        self._http = http_gateway

    async def fetch_product_catalog(self, *, timeout_seconds: float = 300.0) -> dict[str, Any]:
        if self.catalog_mode == "real":
            return await self._http.fetch_product_catalog(timeout_seconds=timeout_seconds)
        if self.catalog_mode == "empty":
            # 目录不可用：验证 MDF/PFMT 都没有时方案退化为「生活方式干预」
            raise RuntimeError("simulated product catalog outage")
        return MOCK_CATALOG

    async def deliver_json(self, payload: dict[str, Any], *, timeout_seconds: float = 300.0) -> int:
        report_type = str(payload.get("type") or "unknown")
        path = self.out_dir / f"report__{report_type}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.delivered.append(report_type)
        if self.deliver_mode == "http":
            status = await self._http.deliver_json(payload, timeout_seconds=timeout_seconds)
            print(f"   ✅ [{CN_NAME.get(report_type, report_type)}] 已 POST，HTTP {status}；副本 {path.name}")
            return status
        print(f"   ✅ [{CN_NAME.get(report_type, report_type)}] 已写入 {path.name}")
        return 200


# ================= 跳过落库 =================

async def _skip_save_artifacts(payloads, generation_statuses, **_kwargs):
    print(f"🧪 跳过产物落库: {[p.get('type') for p in payloads]}")
    return tuple(payloads)


# ================= 结果体检 =================

# 与 reports.py 的必填字段保持同一份，避免两处不同步。
CORE_FIELDS = reports_mod._FOLLOWUP_REQUIRED_FIELDS


def inspect(payloads, report_types=EVALUATE_REPORT_TYPES) -> int:
    """逐份检查核心字段是否有内容，返回空字段总数。"""
    by_type = {p.get("type"): p for p in payloads}
    empty_total = 0
    print(f"\n📋 {len(report_types)} 份报告体检：")
    for report_type in report_types:
        payload = by_type.get(report_type)
        if payload is None:
            print(f"  ❌ {CN_NAME[report_type]}({report_type}) 根本没生成")
            empty_total += 99
            continue
        empty = [k for k in CORE_FIELDS[report_type] if not payload.get(k)]
        filled = [k for k in CORE_FIELDS[report_type] if payload.get(k)]
        mark = "✅" if not empty else "❌"
        print(f"  {mark} {CN_NAME[report_type]}({report_type})")
        print(f"       有内容: {filled}")
        if empty:
            print(f"       ⚠️ 空字段: {empty}")
        empty_total += len(empty)
    return empty_total


def _short(value: Any, limit: int = 70) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"{text[:limit]}{'…' if len(text) > limit else ''}"


def preview_gut_barrier(payload) -> None:
    """肠道与炎症报告解读手册抽样。"""
    print("\n🧪 肠道与炎症报告解读手册")
    meta = payload.get("manual_meta") or {}
    print(f"   {meta.get('title')} | 源报告状态={meta.get('source_status')} "
          f"| 资料缺口{len(meta.get('data_gaps') or [])}条")
    card = (payload.get("preface") or {}).get("quick_card") or {}
    for row in card.get("indicator_rows") or []:
        values = list(row.values())
        print(f"     · {values[0]}: {row.get('检测结果', '')} "
              f"（参考 {row.get('参考范围', '')}）→ {row.get('结果评价', '')}")
    ibfc = card.get("ibfc") or {}
    print(f"     IBFC: {ibfc.get('score')} 分 | {ibfc.get('grade')} | {ibfc.get('damage_type')}")
    for section in payload.get("sections") or []:
        print(f"  ▸ {section.get('title')}")
        print(f"     要点{len(section.get('key_points') or [])}条 | "
              f"话术{len((section.get('script') or {}).get('body') or [])}段 | "
              f"指引{len(section.get('visual_guide') or [])}条 | "
              f"问答{len(section.get('faq') or [])}条")
        if section.get("part_no") == 1:
            items = (section.get("indicator_talk") or {}).get("items") or []
            print(f"     指标逐项讲: {len(items)} 项")
            print(f"     炎症口径: {_short((section.get('inflammation_talk') or {}).get('must_say', ''), 50)}")
        else:
            overview = section.get("risk_overview") or {}
            print(f"     风险统计: {overview.get('stats')}")
            print(f"     未评估口径: {_short(overview.get('must_say', ''), 40)}")
    wrap = (payload.get("closing") or {}).get("wrap_up_script") or {}
    print(f"     三件事: {'、'.join(wrap.get('three_things') or []) or '（无）'}")
    print(f"     参考文献: {len(payload.get('references') or [])} 篇")
    for check in (payload.get("closing") or {}).get("self_check") or []:
        print(f"     自检 {check.get('item')}: {check.get('result')}")


def preview(payloads) -> None:
    by_type = {p.get("type"): p for p in payloads}
    gut = by_type.get("explain_gutBarrier")
    if gut is not None:
        preview_gut_barrier(gut)
    print("\n🔎 正文抽样：")
    op = by_type.get("opinion") or {}
    ex = by_type.get("explain")
    ins = by_type.get("instruction")
    samples = [
        ("诊断意见.受检者", op.get("subject")),
        ("诊断意见.结论", (op.get("diagnosis_summary") or {}).get("conclusion")),
        ("诊断意见.异常项数", len((op.get("diagnostic_basis") or {}).get("abnormal_findings") or [])),
        ("诊断意见.文献数", len(op.get("evidence") or [])),
    ]
    if ex is not None:
        samples += [
            ("报告解读.手册标题", (ex.get("manual_meta") or {}).get("title")),
            ("报告解读.章节数", len(ex.get("sections") or [])),
        ]
    if ins is not None:
        samples += [
            ("临床医嘱.受检者", ins.get("subject")),
            ("临床医嘱.推荐方案", (ins.get("recommend") or {}).get("recommended_combination_method")),
            ("临床医嘱.PFMT", (ins.get("recommend") or {}).get("PFMT")),
            ("临床医嘱.文献数", len(ins.get("evidence") or [])),
        ]
    for label, value in samples:
        print(f"  {label:18} = {_short(str(value))}")

    for finding in (op.get("diagnostic_basis") or {}).get("abnormal_findings") or []:
        print(f"         · [{finding.get('priority')}] {finding.get('problem')}")

    if ex is not None:
        preview_explain(ex)
    if ins is not None:
        preview_instruction(ins)


def preview_explain(ex) -> None:
    """报告解读：手册结构抽样。"""
    sections = ex.get("sections") or []
    if sections:
        card = (ex.get("preface") or {}).get("quick_card") or {}
        print("\n📘 报告解读.解读参考手册")
        overall = card.get("overall") or {}
        print(f"     速查卡: 总评={overall.get('score') or '-'} {overall.get('level_label') or ''} | "
              f"维度{len(card.get('dimension_scores') or [])}项 | 指标{len(card.get('indicator_rows') or [])}行 | "
              f"文献{len(ex.get('evidence') or [])}篇")
        for row in card.get("indicator_rows") or []:
            print(f"         · {row.get('name')}: {row.get('value')}（参考 {row.get('reference')}）→ {row.get('status')}")
        opening = (ex.get("preface") or {}).get("opening_script") or {}
        print(f"     开场: {_short(opening.get('role_statement') or '', 50)}")
        for section in sections:
            points = section.get("key_points") or []
            print(f"  ▸ {section.get('title')}")
            print(f"     要点{len(points)}条 | 话术{len((section.get('script') or {}).get('body') or [])}段 | "
                  f"指引{len(section.get('visual_guide') or [])}条 | 问答{len(section.get('faq') or [])}条")
            if points:
                print(f"     首条要点: {_short(points[0].get('text', ''), 50)}")
            if section.get("part_no") == 2:
                for problem in section.get("problems") or []:
                    print(f"         · [{problem.get('priority')}] {problem.get('problem')}")
        wrap = (ex.get("closing") or {}).get("wrap_up_script") or {}
        print(f"     三件事: {'、'.join(wrap.get('three_things') or []) or '（无）'}")
        for check in (ex.get("closing") or {}).get("self_check") or []:
            print(f"     自检 {check.get('item')}: {check.get('result')}")


def preview_instruction(ins) -> None:
    """临床医嘱：非药物干预方案与推荐检测抽样。"""
    ndi = ins.get("non_drug_intervention") or {}
    print(f"\n🧩 临床医嘱.非药物干预方案（顺序: {list(ndi)}）")

    lifestyle = ndi.get("lifestyle_intervention") or {}
    print("  ① 配套生活方式干预")
    print(f"     开头: {lifestyle.get('intro')}")
    for item in lifestyle.get("items") or []:
        print(f"     - {item.get('title')}: {_short(item.get('summary'), 50)}")
        for point in item.get("points") or []:
            print(f"         · {point}")

    gut = ndi.get("gut_microbiota_regulation") or {}
    print("  ② 肠道菌群调节")
    print(f"     开头: {gut.get('intro')}")
    findings = gut.get("microbiota_findings") or {}
    print(f"     菌群解读: {_short(findings.get('summary'), 60)}")
    for point in findings.get("points") or []:
        print(f"         · {point}")
    probiotics = gut.get("probiotics") or {}
    print(f"     益生菌: {probiotics.get('summary')}")
    if probiotics.get("combination_rationale"):
        print(f"     联合干预依据: {_short(probiotics.get('combination_rationale'), 60)}")
    for product in probiotics.get("products") or []:
        print(f"         · [{product.get('type')}] {product.get('name')} | "
              f"菌株={product.get('strains', '-')} | 理由={_short(product.get('rationale'), 40)}")
        if product.get("substrates"):
            print(f"           营养底物: {'、'.join((x.get('name', '') if isinstance(x, dict) else x)[:12] for x in product['substrates'])}")
        print(f"           用法{len(product.get('usage') or [])}条 | 存储{len(product.get('storage') or [])}条 | 注意事项{len(product.get('notes') or [])}条 | 作用={_short(product.get('action_focus') or '', 20)}")
    prebiotics = gut.get("prebiotics") or {}
    if prebiotics.get("summary"):
        print(f"     益生元与底物（无 MDF 时）: {_short(prebiotics.get('summary'), 60)}")
    for point in prebiotics.get("points") or []:
        print(f"         · {point}")
    barrier = gut.get("gut_barrier") or {}
    print(f"     肠屏障修复: applicable={barrier.get('applicable')} {_short(barrier.get('summary') or '', 50)}")
    print(f"     饮食-菌群协同: {_short(gut.get('diet_synergy'), 60)}")

    supplements = ndi.get("supplements") or {}
    print("  ③ 营养素推荐")
    print(f"     开头: {supplements.get('intro')}")
    for item in supplements.get("items") or []:
        print(f"         · {item.get('name')} | {item.get('daily_dose', '-')} | {item.get('dosage_form', '-')} | {item.get('reason')}")
        print(f"           服用: {item.get('usage', '-')} | 依据: {item.get('evidence', '-')} | 注意: {item.get('caution', '-')}")

    tests = ins.get("recommended_tests") or {}
    print("\n🧪 临床医嘱.推荐检测")
    print(f"     开头: {tests.get('intro')}")
    for item in tests.get("items") or []:
        print(f"         · {item.get('name')}：{item.get('reason')}")


# ================= 主流程 =================

def _resolve_input(raw: str) -> Path | None:
    """定位输入文件：先按当前工作目录解析，找不到再到 api_test/ 和仓库根下找同名文件。"""
    given = Path(raw).expanduser()
    candidates = [given]
    if not given.is_absolute():
        candidates += [
            BASE_DIR / "api_test" / given,
            BASE_DIR / given,
        ]
    for candidate in candidates:
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved != given.resolve():
                print(f"ℹ️  按当前目录找不到 {raw}，改用 {resolved}")
            return resolved

    print(f"❌ 找不到输入文件: {raw}")
    print(f"   当前工作目录: {Path.cwd()}")
    print("   已尝试: " + ", ".join(str(c) for c in candidates))
    sample_dir = BASE_DIR / "api_test"
    samples = sorted(p.name for p in sample_dir.glob("*.json")) if sample_dir.is_dir() else []
    if samples:
        print(f"   {sample_dir} 下现有的 JSON: {samples}")
        print(f"   例如: python -m tests.unit.tools.run_followups api_test/{samples[0]} --mode fake")
    return None


def _dig(payload: dict[str, Any], path: str) -> Any:
    """按 a.b.c 或 a.items.0.name 取子结构，取不到就返回提示字符串。"""
    node: Any = payload
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return f"<找不到路径: {path}（断在 {part}）>"
            node = node[part]
        elif isinstance(node, list):
            if not part.lstrip("-").isdigit():
                return f"<找不到路径: {path}（{part} 不是数组下标）>"
            index = int(part)
            if not -len(node) <= index < len(node):
                return f"<找不到路径: {path}（下标 {part} 越界，长度 {len(node)}）>"
            node = node[index]
        else:
            return f"<找不到路径: {path}（{part} 之前已经不是对象）>"
    return node


def dump_json(payloads, which: str, path: str | None = None) -> None:
    """把回调报文原样打印到终端，便于对接方核对结构。"""
    by_type = {p.get("type"): p for p in payloads}
    targets = tuple(by_type) if which == "all" else (which,)
    for report_type in targets:
        payload = by_type.get(report_type)
        print("\n" + "=" * 78)
        if payload is None:
            print(f"📄 {CN_NAME.get(report_type, report_type)}({report_type}) 没有生成")
            continue
        node = _dig(payload, path) if path else payload
        title = f"📄 {CN_NAME.get(report_type, report_type)}({report_type}) 回调报文"
        print(f"{title}{'  →  ' + path if path else ''}")
        print("=" * 78)
        print(json.dumps(node, ensure_ascii=False, indent=2))


def _field_types(value: Any, path: str = "", out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """收集 {字段路径: 取值类型集合}；数组元素统一记成 path[]，只看结构不看内容。"""
    out = {} if out is None else out
    if isinstance(value, dict):
        out.setdefault(path or "$", set()).add("dict")
        for key, child in value.items():
            _field_types(child, f"{path}.{key}", out)
    elif isinstance(value, list):
        out.setdefault(path, set()).add("list")
        for child in value:
            _field_types(child, f"{path}[]", out)
    else:
        out.setdefault(path, set()).add("null" if value is None else type(value).__name__)
    return out


# 报文里键名本身就是动态内容的字段（模型自由生成的小标题等），不参与路径比对
_DYNAMIC_KEY_PARENTS = (
    ".diagnosis_summary.clinical_context.",
    ".diagnostic_advice.not_recommended_behaviors.",
)


def check_structure(payloads, sample_dir: Path) -> int:
    """gutBarrier 生成的 opinion / instruction 与评估报告样例比结构，返回问题数。

    「新增字段」= 样例里从没出现过的字段路径（前端没对接过）；
    「类型冲突」= 同一路径两边都有值但类型不同。样例里有而这次没有的字段
    多半只是本次数据为空（数组为空就没有元素路径），只提示不计入问题。
    """
    by_type = {p.get("type"): p for p in payloads}
    problems = 0
    print(f"\n📐 结构比对（样例目录: {sample_dir}）：")
    for report_type in ("opinion", "instruction"):
        payload = by_type.get(report_type)
        sample_path = sample_dir / f"report__{report_type}.json"
        if payload is None:
            continue
        if not sample_path.is_file():
            print(f"  ⚠️ {CN_NAME[report_type]}: 找不到评估报告样例 {sample_path.name}，跳过比对。"
                  "先用评估报告跑一次本脚本即可生成。")
            continue
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        if sample.get("type") != report_type:
            print(f"  ⚠️ {CN_NAME[report_type]}: 样例 type={sample.get('type')!r}，不是 {report_type}，跳过。")
            continue

        def keep(path: str) -> bool:
            return not any(path.startswith(parent) for parent in _DYNAMIC_KEY_PARENTS)

        mine = {k: v for k, v in _field_types(payload).items() if keep(k)}
        theirs = {k: v for k, v in _field_types(sample).items() if keep(k)}
        # 样例里是空数组的字段（如 points: []），数组元素的路径在样例里本来就不存在，
        # 本次有元素时无从比对，单独列出、不算结构差异。
        empty_lists = {
            path for path, kinds in theirs.items()
            if "list" in kinds and not any(other.startswith(path + "[]") for other in theirs)
        }

        def under_empty_list(path: str) -> bool:
            return any(path.startswith(parent + "[]") for parent in empty_lists)

        unverifiable = sorted(path for path in set(mine) - set(theirs) if under_empty_list(path))
        added = sorted(path for path in set(mine) - set(theirs) if not under_empty_list(path))
        conflicts = {
            path: (sorted(theirs[path]), sorted(mine[path]))
            for path in set(mine) & set(theirs)
            if (mine[path] - {"null"}) and (theirs[path] - {"null"})
            and not (mine[path] - {"null"}) & (theirs[path] - {"null"})
        }
        missing = sorted(set(theirs) - set(mine))
        mark = "✅" if not added and not conflicts else "❌"
        print(f"  {mark} {CN_NAME[report_type]}({report_type}): 顶层字段 "
              f"{'一致' if list(payload) == list(sample) else '不一致'}，"
              f"新增字段 {len(added)} 个，类型冲突 {len(conflicts)} 个，"
              f"本次为空的字段 {len(missing)} 个")
        if list(payload) != list(sample):
            print(f"       样例顶层: {list(sample)}")
            print(f"       本次顶层: {list(payload)}")
        for path in added[:20]:
            print(f"       + 新增 {path}")
        for path in unverifiable[:20]:
            print(f"       · 样例里是空数组、无法比对元素结构（不算差异）: {path}")
        for path, (expected, actual) in list(conflicts.items())[:20]:
            print(f"       ≠ 类型 {path}: 样例 {expected} / 本次 {actual}")
        problems += len(added) + len(conflicts) + (list(payload) != list(sample))
    return problems


async def async_main(args: argparse.Namespace, *, model_gateway_factory=None, gateway_factory=None) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s | %(message)s",
    )

    source = _resolve_input(args.evaluate_json)
    if source is None:
        return 2
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("❌ 输入文件不是 JSON 对象")
        return 2

    source_type = str(data.get("type") or "")
    sample_dir = BASE_DIR / "api_test" / "out"
    if args.out:
        out_dir = Path(args.out).expanduser()
    elif source_type == "gutBarrier":
        # gutBarrier 也会产出 opinion / instruction，单独放一个目录，
        # 不覆盖评估报告生成的同名文件（它们是结构比对的样例）
        out_dir = sample_dir / "gutBarrier"
    else:
        out_dir = sample_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"🚀 输入: {source}")
    print(f"   输出: {out_dir}")
    report_types = report_types_for(data)
    if source_type in SOURCE_CN_NAME:
        print(f"   源报告: {SOURCE_CN_NAME[source_type]}({source_type}) "
              f"→ 生成 {'、'.join(CN_NAME.get(t, t) for t in report_types)}")
    else:
        print(f"   ⚠️ 这份 JSON 的 type = {source_type!r}，不是 'evaluate' 也不是 'gutBarrier'。"
              f"请确认用的是报告的**回调正文**，而不是内部中间结构。")
    if source_type != "gutBarrier" and "report" in data and "diagnostic_basis" not in data:
        print("   ⚠️ 检测到顶层 'report' 键且没有 'diagnostic_basis'，"
              "这看起来是回调构造前的内部结构，后续报告读不到字段。")
    print(f"🔍 源事实概览: {reports_mod._source_fact_summary(data)}")
    print(f"⚙️  mode={args.mode} catalog={args.catalog} deliver={args.deliver}")

    if args.concurrency > 0:
        reports_mod.llm_semaphore = asyncio.Semaphore(args.concurrency)
        print(f"⚙️  并发上限覆盖为 {args.concurrency}")

    # 必须 patch 模块全局名：调用方按模块属性查找，避免触碰 MongoDB。
    reports_mod._save_followup_artifacts = _skip_save_artifacts

    if model_gateway_factory is None:
        model_gateway = {"fake": FakeModelGateway, "dead": DeadModelGateway}[args.mode]()
    else:
        model_gateway = model_gateway_factory(args)
    gateway = (LocalGateway(catalog=args.catalog, deliver=args.deliver, out_dir=out_dir)
               if gateway_factory is None else gateway_factory(args, out_dir))

    beat = asyncio.create_task(heartbeat(model_gateway))
    try:
        result = await _push_report(
            data,
            args.user_id,
            args.session_id,
            gateway=gateway,
            model_gateway=model_gateway,
        )
    finally:
        beat.cancel()
        closer = getattr(model_gateway, "aclose", None)
        if closer is not None:
            await closer()

    print("\n🎉 各模块状态：")
    print(json.dumps(result.module_statuses, ensure_ascii=False, indent=2))
    print(f"\n📨 已下发 {len(gateway.delivered)} 份: {gateway.delivered}")
    print(f"🤖 模型调用 {len(model_gateway.calls)} 次: {sorted(set(model_gateway.calls))}")

    empty_total = inspect(result.payloads, report_types)
    preview(result.payloads)
    structure_problems = 0
    if source_type == "gutBarrier" and out_dir.resolve() != sample_dir.resolve():
        structure_problems = check_structure(result.payloads, sample_dir)

    if args.print_json:
        dump_json(result.payloads, args.print_json, args.print_path)

    if empty_total:
        print(f"\n❌ 仍有 {empty_total} 个核心字段为空，兜底没生效，查上面的 "
              f"'follow-up unit ... failed' 日志定位是哪个子任务。")
        return 1
    if structure_problems:
        print(f"\n❌ opinion / instruction 与评估报告样例有 {structure_problems} 处结构差异，见上面的结构比对。")
        return 1
    print(f"\n✅ {len(report_types)} 份报告的核心字段全部有内容。")
    return 0
