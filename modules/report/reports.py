# -*- coding: utf-8 -*-
# @Time    : 12.08.2026
# @File    : reports.py
# @Software: PyCharm

"""
shulan/report_api/reports.py - Description of the file/module

type=evaluate   时生成 explain:报告解读 / opinion:诊断意见 / instruction:临床医嘱 3份报告
type=gutBarrier 时生成 explain_gutBarrier:肠道与炎症报告解读 / opinion / instruction 3份报告
[已停用] intervene:干预方案 —— 产品暂不需要，相关代码已注释保留，后续可恢复
"""

import asyncio
import json
import random
import re
import string
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Awaitable
from typing import Callable

# TODO LLM调用后续需要统一
# 假设这些是你同事底层的基建，保持导入
from modules.report.artifacts import active_final_report_id
from modules.report.artifacts import bind_artifact_namespace
from modules.report.artifacts import callbacks_are_deferred
from modules.report.artifacts import is_deliverable
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.explain_context import build_explain_contexts
from modules.report.explain_context import dumps as explain_dumps
from modules.report.explain_defaults import SECTION_TITLES as EXPLAIN_SECTION_TITLES
from modules.report.explain_defaults import assemble_manual as assemble_explain_manual
from modules.report.explain_defaults import default_opening as default_explain_opening
from modules.report.explain_defaults import default_section as default_explain_section
from modules.report.explain_normalize import allowed_numbers as explain_allowed_numbers
from modules.report.explain_normalize import normalize_opening as normalize_explain_opening
from modules.report.explain_normalize import normalize_section as normalize_explain_section
from modules.report.gut_barrier_context import UNASSESSED_LABELS as GUT_BARRIER_UNASSESSED_LABELS
from modules.report.gut_barrier_context import build_gut_barrier_contexts, without_crp_content
from modules.report.gut_barrier_context import dumps as gut_barrier_dumps
from modules.report.gut_barrier_defaults import SECTION_TITLES as GUT_BARRIER_SECTION_TITLES
from modules.report.gut_barrier_defaults import assemble_manual as assemble_gut_barrier_manual
from modules.report.gut_barrier_defaults import build_top_three as build_gut_barrier_top_three
from modules.report.gut_barrier_defaults import default_opening as default_gut_barrier_opening
from modules.report.gut_barrier_defaults import default_section as default_gut_barrier_section
from modules.report.gut_barrier_normalize import normalize_opening as normalize_gut_barrier_opening
from modules.report.gut_barrier_normalize import normalize_section as normalize_gut_barrier_section
from modules.report.followup_defaults import FOLLOWUP_DEFAULTS
from modules.report.followup_defaults import default_followup_report
from modules.report.followup_defaults import RECOMMENDED_TEST_TIMEFRAMES
from modules.report.followup_defaults import RECOMMENDED_TEST_TIMEFRAME_DEFAULT
from modules.report.followup_defaults import attach_product_details
from modules.report.followup_defaults import SUPPLEMENT_FIELDS
from modules.report.followup_defaults import attach_supplement_evidence
from modules.report.followup_defaults import backfill_supplement_evidence
from modules.report.followup_defaults import supplement_evidence_pool
from modules.report.followup_defaults import format_supplement_evidence_candidates
from modules.report.followup_defaults import short_usage
from modules.report.followup_defaults import GUT_BARRIER_MARKERS
from modules.report.followup_defaults import LIFESTYLE_ITEM_TITLES
from modules.report.followup_defaults import non_drug_intervention_base
from modules.report.followup_defaults import RECOMMENDED_TESTS_MAX_ITEMS
from modules.report.followup_defaults import recommended_tests_base
from modules.report.followup_defaults import notes_base
from modules.report.followup_defaults import PFMT_USAGE, PFMT_USAGE_INSTRUCTIONS
from modules.report.followup_defaults import PFMT_TOTAL_VIABLE_COUNT_PER_BOX, PFMT_VIABLE_COUNT_PER_CAPSULE
from modules.report.ports import FollowupGatewayError
from modules.report.ports import FollowupModelGateway
from modules.report.ports import FollowupReportGateway
from modules.report.terminal import followup_terminal_payload
from utils.log import getLogger

logger = getLogger(__name__)

# ================= 核心基建：上下文与状态管理 =================
_ACTIVE_FOLLOWUP_GATEWAY: ContextVar[FollowupReportGateway | None] = ContextVar(
    "active_followup_report_gateway",
    default=None,
)

# 模型入口与非模型 I/O 分开持有：前者经 AgentHub，后者继续走业务端口。
_ACTIVE_FOLLOWUP_MODEL: ContextVar[FollowupModelGateway | None] = ContextVar(
    "active_followup_model_gateway",
    default=None,
)

# Shared only by children of one report generation attempt, never serialized.
_ACTIVE_FOLLOWUP_GAPS: ContextVar[set[str] | None] = ContextVar("followup_gaps", default=None)
_ACTIVE_FOLLOWUP_COMPONENT_POLICY: ContextVar[ReportFallbackPolicy] = ContextVar(
    "followup_component_policy", default=DEFAULT_REPORT_FALLBACK_POLICY,
)
_ACTIVE_FOLLOWUP_COMPONENT_CACHE: ContextVar[dict | None] = ContextVar(
    "followup_component_cache", default=None,
)


@dataclass(frozen=True)
class FollowupExecutionResult:
    """Internal generation/delivery outcome; never exposed to patients."""
    payloads: tuple[dict[str, Any], ...]
    module_statuses: dict[str, dict[str, str]]



# ================= 模型调用入口 =================
# 全部后续报告模型调用统一经 AgentHub -> report.followup Graph -> LLMGateway。
# 本模块不再创建模型 Client，也不读取 LLM 配置；固定系统 Prompt 已外置到
# prompts/report/followups/system.txt，由 ModelProfile 加载。

# 限制并发数（信号量），防止瞬间数十个请求打满带宽或被 API 网关拦截。
# 它是模块级的，因此上限在整个进程内共享，不随并发报告数量倍增。
llm_semaphore = asyncio.Semaphore(4)


async def complete_followup_text(
    *, model_gateway: FollowupModelGateway, task_type: str,
    user_prompt: str, timeout_seconds: float = 1800.0,
) -> str:
    """One pure model attempt through the shared follow-up concurrency gate.

    New report-specific orchestration can reuse the existing resource ceiling
    without installing legacy ContextVars, doing persistence, or multiplying
    its own bounded retry policy. Cancellation propagates to the model call.
    """
    async with llm_semaphore:
        return await asyncio.wait_for(
            model_gateway.complete_text(task_type=task_type, user_prompt=user_prompt),
            timeout=timeout_seconds,
        )


def _active_followup_model() -> FollowupModelGateway:
    gateway = _ACTIVE_FOLLOWUP_MODEL.get()
    if gateway is None:
        raise RuntimeError("follow-up model gateway is not configured")
    return gateway


async def safe_complete_async(prompt, task_type, retries=3, timeout=1800.0):
    """带并发控制和指数退避重试的安全 LLM 请求函数。

    ``task_type`` 由 report.followup Graph 按服务端允许列表校验；调用方
    不能传系统 Prompt、模型名或工具。返回模型正文文本。
    """
    for attempt in range(retries):
        try:
            async with llm_semaphore:
                return await asyncio.wait_for(
                    _active_followup_model().complete_text(
                        task_type=task_type,
                        user_prompt=prompt,
                    ),
                    timeout=timeout,
                )
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"LLM 请求彻底失败 (尝试 {retries} 次): {e}")
                raise
            logger.warning(f"LLM 请求异常: {e}，将在 {2 ** attempt} 秒后进行第 {attempt + 1} 次重试...")
            await asyncio.sleep(2 ** attempt)
# ===============================================



# ================= 固定模板加载 =================
# 15 个任务的固定 user prompt 模板存放在 prompts/report/followups/<task>.txt，
# 使用 $name 占位符：模板里的 JSON Schema 花括号因此无需任何转义。
# 条件与循环逻辑仍留在各任务函数中，模板只负责固定文本。
_FOLLOWUP_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "report" / "followups"
_FOLLOWUP_PROMPT_CACHE: dict[str, string.Template] = {}

# 统一缺失处理规则追加在每份任务模板末尾，与固定 system 规则一致。
# 保留可用的检验、影像和病史事实；缺失字段用类型兼容空值，不能把
# 没有数据转写为正常、低风险或推荐产品的依据。
_FALLBACK_RULES_FILE = "_fallback_rules.txt"
_FALLBACK_RULES_CACHE: str | None = None


def _fallback_rules() -> str:
    global _FALLBACK_RULES_CACHE
    if _FALLBACK_RULES_CACHE is None:
        _FALLBACK_RULES_CACHE = (
            _FOLLOWUP_PROMPT_DIR / _FALLBACK_RULES_FILE
        ).read_text(encoding="utf-8")
    return _FALLBACK_RULES_CACHE


# 「本次输入数据范围」是已知结构化依据未命中的保守提示，不是对原始
# JSON 所有临床字段的完整判空，也不表示所有实际检查结果均正常。
_ACTIVE_SCOPE_NOTE: ContextVar[str] = ContextVar(
    "active_followup_scope_note",
    default="",
)

_NO_MICROBIOME_SCOPE_NOTE = """

# 本次输入数据范围（程序辅助检查）
程序未在已知结构化条目中找到可直接支撑菌群解读的依据；该标记不代表全部临床资料缺失，
也不代表已有检查正常。仍需逐项核对实际 JSON 中的检验、影像、病史、症状及来源日期。

1. 只使用本次输入中可核对的事实。若有真实检验、影像或临床背景，应保留并说明其实际检查范围；
   不因没有菌群条目而丢弃这些资料，不把其自动归因于菌群失衡或肠道屏障受损。
2. 没有对应菌群原始结果时，不编写菌种丰度、失衡等级、代谢合成潜能或已完成菌群检测的结论。
   实际提供的炎症等检验仍可按其原始名称和结果描述，不能由此补造未测项目。
3. 固定字段名称全部保留：缺失标量返回 null，缺失或无可用项目的数组返回 []，
   声明了子字段的对象保留全部子字段，不以 {} 替代。只有身份资料时，允许使用简短的结果待补充摘要。
4. 缺乏评估依据时，风险和分级字段返回 null；不得默认正常、低风险、一度失衡或可代偿状态。
   真实正常结果只能描述其具体项目和时间范围，不外推为整体健康风险较低。
5. 缺乏个体依据时不生成产品选择、营养目标或补充剂剂量，也不能仅凭年龄套用默认剂量。
"""


def _build_scope_note(data: dict[str, Any]) -> str:
    """输入数据确实没有菌群依据时，返回一段硬约束；否则返回空串。

    这段判定读的是评估报告独有的字段（diagnostic_basis、healthAssessment），
    对肠道与炎症报告（gutBarrier）一律返回空串——否则会被误判成「没有菌群结果」，
    把这份报告该讲的屏障与炎症内容全禁掉。
    """
    if _source_type(data) != SOURCE_TYPE_EVALUATE:
        return ""
    assessment = data.get("healthAssessment") or data.get("health_assessment") or {}
    if (data.get("diagnostic_basis") or {}).get("abnormal_findings"):
        return ""
    if assessment.get("abnormalIndicators") or assessment.get("abnormal_indicators"):
        return ""
    for dimension in assessment.get("dimensions") or []:
        if isinstance(dimension, dict) and (
            dimension.get("evidenceUsed") or dimension.get("evidence_used")
        ):
            return ""
    return _NO_MICROBIOME_SCOPE_NOTE


# ================= 入参报告类型 =================
# 同事按类型下发不同的源报告：
#   evaluate   -> explain / opinion / instruction 三份；
#   gutBarrier -> explain_gutBarrier / opinion / instruction 三份。
# gutBarrier 的 opinion / instruction 先经 _gut_barrier_followup_input 转成评估报告
# 字段结构，再走与 evaluate 完全相同的生成与组装代码，对外报文结构保持一致。
SOURCE_TYPE_EVALUATE = "evaluate"
SOURCE_TYPE_GUT_BARRIER = "gutBarrier"
GUT_BARRIER_REPORT_TYPE = "explain_gutBarrier"


def _source_type(data: dict[str, Any]) -> str:
    """源报告类型；缺省按评估报告处理，保持历史行为不变。"""
    return str((data or {}).get("type") or SOURCE_TYPE_EVALUATE)


# gutBarrier 生成 opinion / instruction 时追加到提示词末尾的事实边界。
# 这两份报告的提示词和 system.txt 都是围绕菌群写的，而这份源报告里没有菌群测序明细；
# 不钉死边界，模型很容易顺着菌群导向编出「菌群失衡」「补足某菌缺口」之类的结论。
# 只对 gutBarrier 生效（见 _prompt_scope_note），evaluate 链路的提示词逐字不变；
# 《肠道与炎症报告解读》走自己的模板与专属规则，不追加这段。
#
# 用户同时上传了菌群评估报告时，肠道屏障报告第一章会带「辅助资料摘要」表（原报告肠型），
# 这时边界里如实写明肠型；没有时才写「没有肠型信息」。
_GUT_BARRIER_SCOPE_TEMPLATE = """

# 本次输入数据范围（程序判定，优先级高于上文一切表述）
本次输入来自《肠道屏障功能评估报告》，已由程序按评估报告的字段结构整理。
它只包含：肠道屏障三项检测指标（D-乳酸、二胺氧化酶、细菌脂多糖）、
肠道屏障功能评分（IBFC）与损伤分级、相关疾病风险评估条目、个体临床背景（既往检查）。
${enterotype_scope}

因此：
1. 严禁写出任何菌种名、菌属名、丰度、多样性、菌群失衡、短链脂肪酸合成潜能等菌群结论，
   也不得声称或暗示受检者做过菌群测序明细分析。
2. 结论只能基于输入中的检测指标、IBFC 评分与分级、疾病风险条目与临床背景；
   数值、单位、参考范围逐字引用，不得换算、取整或编造。
3. 不处理或生成 CRP、超敏C反应蛋白相关检测结果、评估、分层或复查建议，也不生成其缺失提示。
4. 个体临床背景里的既往检查只作背景，不得当成本次新测值。
5. 风险等级、损伤分级以输入原文为准，不得自行升级或降级。
6. 推荐产品或干预措施时，理由只能落在肠道屏障、炎症与生活方式上，不得以「补足某菌」
   「菌群缺口」等菌群结论作为理由。
7. 方案中须包含健康人源肠道菌群移植胶囊（PFMT）：有肠型时以肠型配型为主、性别为辅；
   缺少肠型或性别时同样要推荐，由程序按产品目录补全匹配，不得编造或推断肠型。
"""
_GUT_BARRIER_NO_ENTEROTYPE_SCOPE = "本次**不包含任何肠道菌群测序结果，也没有肠型信息**。"
_GUT_BARRIER_ENTEROTYPE_SCOPE = (
    "本次**不包含菌种、丰度等肠道菌群测序明细**；仅有随附菌群评估报告提供的肠型：${enterotype}"
    "（肠型只用于 PFMT 配型）。"
)


def _gut_barrier_scope_note(enterotype: str = "") -> str:
    """按是否有肠型生成 gutBarrier 的事实边界。"""
    scope = (
        string.Template(_GUT_BARRIER_ENTEROTYPE_SCOPE).substitute(enterotype=enterotype)
        if enterotype else _GUT_BARRIER_NO_ENTEROTYPE_SCOPE
    )
    return string.Template(_GUT_BARRIER_SCOPE_TEMPLATE).substitute(enterotype_scope=scope)


# 兼容旧引用：没有肠型时的边界
_GUT_BARRIER_SCOPE_NOTE = _gut_barrier_scope_note()


def _prompt_scope_note(data: dict[str, Any]) -> str:
    """本次执行要追加到后续报告提示词末尾的事实边界。

    gutBarrier 追加屏障报告边界（按是否随附肠型区分）；evaluate 沿用 _build_scope_note
    （只在评估报告没有任何菌群依据时追加），行为与改造前一致。
    """
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        part1 = build_gut_barrier_contexts(data if isinstance(data, dict) else {})["part1"]
        return _gut_barrier_scope_note(_gb_enterotype(part1))
    return _build_scope_note(data)


# 报告解读（explain）另有一套全局规则：手册要的是第一人称口语体话术，
# 与 _fallback_rules.txt 的「严谨客观循证表述、不得出现对话式口吻」直接冲突，
# 因此两份规则分开挂载，互不影响其余三份报告。
_EXPLAIN_RULES_FILE = "_explain_rules.txt"
_EXPLAIN_RULES_CACHE: str | None = None
_EXPLAIN_PROMPT_CACHE: dict[str, string.Template] = {}


def _explain_rules() -> str:
    global _EXPLAIN_RULES_CACHE
    if _EXPLAIN_RULES_CACHE is None:
        _EXPLAIN_RULES_CACHE = (
            _FOLLOWUP_PROMPT_DIR / _EXPLAIN_RULES_FILE
        ).read_text(encoding="utf-8")
    return _EXPLAIN_RULES_CACHE


def _render_explain_prompt(task_type: str, **values: Any) -> str:
    """渲染报告解读任务的模板；末尾追加解读手册专用规则。"""
    template = _EXPLAIN_PROMPT_CACHE.get(task_type)
    if template is None:
        path = _FOLLOWUP_PROMPT_DIR / f"{task_type}.txt"
        template = string.Template(
            path.read_text(encoding="utf-8") + "\n\n" + _explain_rules()
        )
        _EXPLAIN_PROMPT_CACHE[task_type] = template
    return template.substitute(**values)


# 肠道与炎症报告解读：在解读手册通用规则之上再追加一份专属规则
_GUT_BARRIER_RULES_FILE = "_gut_barrier_rules.txt"
_GUT_BARRIER_RULES_CACHE: str | None = None
_GUT_BARRIER_PROMPT_CACHE: dict[str, string.Template] = {}


def _gut_barrier_rules() -> str:
    global _GUT_BARRIER_RULES_CACHE
    if _GUT_BARRIER_RULES_CACHE is None:
        _GUT_BARRIER_RULES_CACHE = (
            _FOLLOWUP_PROMPT_DIR / _GUT_BARRIER_RULES_FILE
        ).read_text(encoding="utf-8")
    return _GUT_BARRIER_RULES_CACHE


def _render_gut_barrier_prompt(task_type: str, **values: Any) -> str:
    """渲染肠道与炎症报告解读任务的模板；末尾依次追加通用规则与专属规则。"""
    template = _GUT_BARRIER_PROMPT_CACHE.get(task_type)
    if template is None:
        path = _FOLLOWUP_PROMPT_DIR / f"{task_type}.txt"
        template = string.Template(
            path.read_text(encoding="utf-8")
            + "\n\n" + _explain_rules()
            + "\n\n" + _gut_barrier_rules()
        )
        _GUT_BARRIER_PROMPT_CACHE[task_type] = template
    return template.substitute(**values)


def _render_followup_prompt(task_type: str, **values: Any) -> str:
    """渲染指定任务的固定模板；缺少占位符取值时直接失败，不静默留空。"""
    template = _FOLLOWUP_PROMPT_CACHE.get(task_type)
    if template is None:
        path = _FOLLOWUP_PROMPT_DIR / f"{task_type}.txt"
        template = string.Template(
            path.read_text(encoding="utf-8") + "\n\n" + _fallback_rules()
        )
        _FOLLOWUP_PROMPT_CACHE[task_type] = template
    # 范围提示在 substitute 之后拼，内容里的任何字符都不会被当成占位符。
    return template.substitute(**values) + _ACTIVE_SCOPE_NOTE.get()


# ================= 辅助工具方法 =================
def _extract_json(text):
    """从模型返回文本中提取 JSON 对象或数组。

    兼容四种实际会遇到的形态：``` 代码块包裹、前后带解释性文字、纯 JSON，
    以及**顶层数组**（exception_handling 等任务的 Schema
    就是数组，旧实现只找 ``{}``，一旦模型加了代码块围栏就整份丢空）。
    解析彻底失败时打印首尾片段，不再静默返回 {} 让人无从排查。
    """
    if not text:
        return {}
    stripped = str(text).strip()
    candidates: list[str] = []
    candidates.extend(
        re.findall(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", stripped, re.DOTALL)
    )
    candidates.append(stripped)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if start != -1 and end > start:
            candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    logger.warning(
        "follow-up model output is not valid JSON: head=%r tail=%r",
        stripped[:160],
        stripped[-80:],
    )
    return {}


def _coalesce(parsed: Any, defaults: Any) -> Any:
    """Preserve supplied values, including 0/False, and fill only missing structure."""
    def fallback():
        gaps = _ACTIVE_FOLLOWUP_GAPS.get()
        if gaps is not None:
            gaps.add("missing_or_invalid_field")
        return deepcopy(defaults)

    if isinstance(defaults, list):
        return list(parsed) if isinstance(parsed, list) else fallback()
    if not isinstance(defaults, dict):
        # In the legacy follow-up templates every null leaf replaces a declared
        # string slot (risk, dose text, etc.), not an untyped scalar. Numeric and
        # boolean contracts use an explicit typed default; keep valid 0/False.
        if parsed is None or parsed == "" or isinstance(parsed, (dict, list)):
            return fallback()
        expected = str if defaults is None else type(defaults)
        if type(parsed) is not expected:
            return fallback()
        return parsed
    result = dict(parsed) if isinstance(parsed, dict) else {}
    for key, default in defaults.items():
        result[key] = _coalesce(result.get(key), default)
    return result


def _has_model_content(value: Any, shape: Any) -> bool:
    """Only declared, correctly typed content can count as a generated result.

    A nonempty object containing only metadata or unknown keys is not a
    clinical component. Preserve meaningful 0/False for explicitly typed slots.
    """
    if isinstance(shape, dict):
        metadata = {"part_no", "title", "kind", "anchor", "source", "evidence_refs"}
        return isinstance(value, dict) and any(
            key not in metadata and key in value and _has_model_content(value[key], default)
            for key, default in shape.items()
        )
    if isinstance(shape, list):
        if not isinstance(value, list) or not value:
            return False
        if shape:
            return any(_has_model_content(item, shape[0]) for item in value)
        return any(isinstance(item, (dict, str)) and bool(item) for item in value)
    expected = str if shape is None else type(shape)
    return type(value) is expected and (bool(value.strip()) if expected is str else True)


def _opinion_component_json(response: Any, defaults: dict) -> dict:
    parsed = _extract_json(response)
    if not _has_model_content(parsed, defaults):
        # Ignore unrelated fields: they must not turn a neutral fallback into
        # a different object that appears to be a usable partial report.
        # Explicit empty arrays are still valid (e.g. no generated findings);
        # do not manufacture a missing-field gap for those supplied arrays.
        parsed = {
            key: [] for key, default in defaults.items()
            if isinstance(default, list) and isinstance(parsed, dict) and parsed.get(key) == []
        }
    return _coalesce(parsed, defaults)


async def _followup_component(unit: str, factory: Callable[[], Awaitable[Any]], defaults: Any) -> Any:
    """Retry one component; preserve siblings and their missing-data markers.

    Cache and gaps are scoped to a report, never shared between patients. An
    interrupted owner cancels the component instead of publishing a fallback.
    """
    parent_gaps = _ACTIVE_FOLLOWUP_GAPS.get()
    cache = _ACTIVE_FOLLOWUP_COMPONENT_CACHE.get()
    if cache is not None and unit in cache:
        value, recorded_gaps = cache[unit]
        if parent_gaps is not None:
            parent_gaps.update(recorded_gaps)
        return deepcopy(value)
    policy = _ACTIVE_FOLLOWUP_COMPONENT_POLICY.get()
    local_gaps: set[str] = set()
    token = _ACTIVE_FOLLOWUP_GAPS.set(local_gaps)
    result = None
    try:
        attempts = policy.attempts_for(policy.llm_attempts)
        for attempt in range(attempts):
            local_gaps.clear()
            try:
                value = await factory()
                expected = list if isinstance(defaults, list) else dict
                if not isinstance(value, expected) or (isinstance(value, dict) and not value):
                    raise ValueError("invalid or empty component result")
                result = _coalesce(value, defaults)
                break
            except Exception as error:
                logger.warning("follow-up component %s attempt %d/%d failed: error_type=%s",
                               unit, attempt + 1, attempts, type(error).__name__)
                if attempt + 1 < attempts:
                    delay = policy.backoff_after(attempt)
                    if delay:
                        await asyncio.sleep(delay)
        else:
            local_gaps.clear()
            local_gaps.add("component_unavailable")
            result = deepcopy(defaults)
    finally:
        _ACTIVE_FOLLOWUP_GAPS.reset(token)
    recorded_gaps = frozenset(f"{unit}:{gap}" for gap in local_gaps)
    if parent_gaps is not None:
        parent_gaps.update(recorded_gaps)
    if cache is not None:
        cache[unit] = (deepcopy(result), recorded_gaps)
    return result


async def _gather_followup_components(*coroutines):
    """Join all children on cancellation or failure before leaving their owner."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _manual_component(unit: str, factory, defaults: dict, *, issues: list[str]) -> dict:
    """Cache normalized content together with its correction audit.

    Each attempt owns its audit list: failed attempts cannot pollute another
    component, and an envelope retry must replay corrections with cached text.
    """
    async def generate():
        corrections: list[str] = []
        content = await factory(corrections)
        return {"content": content, "corrections": corrections}

    result = await _followup_component(
        unit, generate, {"content": defaults, "corrections": []},
    )
    issues.extend(result["corrections"])
    return result["content"]


def _active_gateway() -> FollowupReportGateway:
    gateway = _ACTIVE_FOLLOWUP_GATEWAY.get()
    if gateway is None:
        raise RuntimeError("follow-up report gateway is not configured")
    return gateway




def collect_evidence_refs(obj):
    """递归收集字典或列表中包含的所有 evidence_refs"""
    refs = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "evidence_refs" and isinstance(v, list):
                refs.extend(v)
            else:
                refs.extend(collect_evidence_refs(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.extend(collect_evidence_refs(item))
    return refs


def renumber_evidence(modules_list, evidence_list):
    """把引用到的文献按顺序重新编号为 1..n，并原地替换 modules 中所有 evidence_refs。

    返回重新编号后的文献列表（浅拷贝，不修改评估报告原始 evidence）。
    正文里引用了但不在文献列表中的编号会被丢弃，避免出现指向不存在文献的 [n]。
    """
    mapping: dict[str, str] = {}
    renumbered = []
    for index, evidence in enumerate(evidence_list, start=1):
        mapping[str(evidence.get("number")).strip()] = str(index)
        renumbered.append({**evidence, "number": index})

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "evidence_refs" and isinstance(value, list):
                    new_refs = []
                    for ref in value:
                        new_ref = mapping.get(str(ref).strip())
                        if new_ref and new_ref not in new_refs:
                            new_refs.append(new_ref)
                    obj[key] = new_refs
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for module in modules_list:
        walk(module)
    return renumbered


def extract_unified_evidence(modules_list, report_data):
    """
    搜集所有模块引用的 evidence_refs，去重后从 report_data["evidence"] 中查找对应的完整对象。
    统一将文献编号转为字符串匹配，避免 int 和 str 类型不一致导致匹配丢失。
    """
    all_refs = []
    for module in modules_list:
        all_refs.extend(collect_evidence_refs(module))

    evidence_list = report_data.get("evidence", [])
    evidence_map = {
        str(e["number"]): e
        for e in evidence_list
        if isinstance(e, dict) and "number" in e
    }

    unique_refs = []
    for ref in all_refs:
        str_ref = str(ref).strip()
        if str_ref and str_ref not in unique_refs:
            unique_refs.append(str_ref)

    return [evidence_map[ref] for ref in unique_refs if ref in evidence_map]


# ================= 业务节点生成逻辑 =================
async def _fetch_catalog_once():
    """使用同事的 Gateway 调用产品目录，并保留你的精准数据解析逻辑"""
    mdf_code = ""
    fixed = []
    random_strains = []
    capsule = {}
    mdf_details = {}
    pfmt_details = {}
    pfmt_candidates = []

    res_data = await _active_gateway().fetch_product_catalog(timeout_seconds=300.0)

    if res_data.get("state") == 200:
        results = res_data.get("results", {})

        # 1. 解析靶向调节剂 (MDF)
        mdf_list = results.get("microecologicalTargetedRegulator", [])
        if mdf_list:
            mdf = mdf_list[0]
            mdf_code = mdf.get("productCode", "")
            fixed = mdf.get("requiredStrains", [])
            random_strains = mdf.get("optionalStrains", [])
            mdf_details = {
                "productSpecification": mdf.get("productSpecification", ""),
                "shelfLife": mdf.get("shelfLife", ""),
                "compositeEfficacyComponents": mdf.get("compositeEfficacyComponents", ""),
                "usageInstructions": mdf.get("usageInstructions", ""),
            }

        # 2. 解析移植胶囊 (PFMT)
        pfmt_list = results.get("fecalMicrobiotaTransplantationCapsule", [])
        for pfmt in pfmt_list:
            code = pfmt.get("productCode")
            name = pfmt.get("productName")
            status = pfmt.get("productStatus", "启用")
            if code and name:
                capsule[code] = name
                pfmt_details[code] = {
                    "viableCountPerCapsule": pfmt.get("viableCountPerCapsule", ""),
                    "totalViableCountPerBox": pfmt.get("totalViableCountPerBox", ""),
                    "usageInstructions": pfmt.get("usageInstructions", ""),
                }
                pfmt_candidates.append({
                    "productCode": code,
                    "productName": name,
                    "productStatus": status,
                    "applicableGender": pfmt.get("applicableGender", ""),
                    "applicableEnterotype": pfmt.get("applicableEnterotype", ""),
                    "applicableEnterotypeName": pfmt.get("applicableEnterotypeName", ""),
                    "applicableEnterotypeDesc": pfmt.get("applicableEnterotypeDesc", "")
                })

    return mdf_code, fixed, random_strains, capsule, mdf_details, pfmt_details, pfmt_candidates


async def fetch_catalog(policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY):
    """带重试的 Catalog 获取逻辑（同事架构）"""
    attempts = policy.attempts_for(policy.product_catalog_attempts)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await _fetch_catalog_once()
        except FollowupGatewayError as exc:
            last_error = exc
            if not exc.retryable:
                break
        except Exception as exc:
            last_error = exc
            break
        if attempt + 1 < attempts:
            delay = policy.backoff_after(attempt)
            if delay:
                await asyncio.sleep(delay)
    raise RuntimeError("product catalog is unavailable") from last_error


# ----------------- 子任务 -----------------
# 数据不足时「健康风险提示」末尾追加的固定引导语（程序追加，模型不写）。
DATA_INSUFFICIENT_ADVICE = "建议上传更多报告数据（如血常规、血生化、肠道菌群检测等），以获得更完善的个性化报告。"
# 描述「数据多少」的话术，出现在健康风险提示里会显得生硬，整句去掉。
_DATA_GAP_PHRASES = (
    "现有评估资料", "现有资料", "现有数据", "现有评估数据", "现有检测", "现有报告",
    "评估资料有限", "资料有限", "数据有限", "资料不足", "数据不足", "根据提供的", "根据现有",
)


# 「维持膳食均衡、规律作息与适度运动」这类泛泛的生活方式句：只在没有任何风险发现时才保留，
# 有异常时它会冲淡重点（生活方式由干预方案模块给出）。
_LIFESTYLE_FILLER = re.compile(r"^(?:维持|保持|坚持|注意|注重)[^。！；]*(?:膳食|饮食|作息|运动|睡眠)")


# ---- 指标异常统一写成「××指标异常」（产品要求，综合评估与健康风险提示共用）----
# 「伴二胺氧化酶（DAO）异常升高」->「伴二胺氧化酶（DAO）指标异常」；「hs-CRP中度升高」->「hs-CRP指标异常」。
# 只替换紧跟在指标名（可带缩写括注、数值单位）后面的程度词；「DAO、LPS未同步升高」这类否定说法不动。
_INDICATOR_NAMES = (
    r"D-?乳酸|D-Lactate|二胺氧化酶|DAO|细菌脂多糖|脂多糖|LPS|超敏C反应蛋白|hs-?CRP|C反应蛋白|CRP"
    r"|总胆固醇|甘油三酯|低密度脂蛋白胆固醇|LDL-?C|高密度脂蛋白胆固醇|HDL-?C|尿酸|空腹血糖|血糖"
    r"|糖化血红蛋白|HbA1c|谷丙转氨酶|ALT|谷草转氨酶|AST|肌酐|同型半胱氨酸|指标"
)
_ABNORMAL_DEGREE = r"(?:异常|轻度|中度|重度|显著|明显|轻微)?(?:升高|偏高|增高|上升|降低|偏低|下降)"
_ABNORMAL_WORDING = re.compile(
    rf"(?P<head>(?:{_INDICATOR_NAMES})(?:\s*[（(][A-Za-z\-]+[）)])?"
    r"(?:\s*[<>＜＞≥≤]?\s*\d+(?:\.\d+)?(?:\s*[A-Za-zμ%][A-Za-zμ%/\^·×\d]*)?)?"
    r"\s*[，,]?\s*[（(]?)"
    rf"(?P<neg>未同步|未见|未|无|不)?(?P<degree>{_ABNORMAL_DEGREE})"
)


def _normalize_abnormal_wording(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text

    def replace(match: re.Match) -> str:
        if match.group("neg"):
            return match.group(0)
        head = match.group("head").rstrip()
        # 「炎症相关指标升高」这类本身以「指标」结尾的，直接接「异常」
        return head + ("异常" if head.endswith("指标") else "指标异常")

    return _ABNORMAL_WORDING.sub(replace, text)


def _polish_risk_warning(text: str, data: dict[str, Any]) -> str:
    """去掉含「现有评估资料」等话术的句子；有风险发现时去掉泛泛的生活方式句；指标异常统一写成「××指标异常」；
    输入数据缺少可用事实时，末尾追加上传更多报告的引导语。evaluate 与 gutBarrier 共用。"""
    sentences = [seg for seg in re.split(r"(?<=[。！；])", str(text or "")) if seg.strip()]
    kept = [seg.strip() for seg in sentences if not any(p in seg for p in _DATA_GAP_PHRASES)]
    findings = [seg for seg in kept if not _LIFESTYLE_FILLER.match(seg)]
    if findings:
        kept = findings
    result = _normalize_abnormal_wording("".join(kept))
    if result.endswith("；"):
        result = result[:-1] + "。"
    if _build_scope_note(data) and DATA_INSUFFICIENT_ADVICE not in result:
        if result and not result.endswith(("。", "！", "；")):
            result += "。"
        result += DATA_INSUFFICIENT_ADVICE
    return result


# ---- instruction 的综合评估（body_mass）与健康风险提示（risk_warning）----
# 两段都以原报告里已有的结论为蓝本精简改写，保证与原报告对得上：
#   evaluate  ：综合评估 <- diagnosis_summary.conclusion
#              健康风险 <- diagnosis_summary.clinical_context["report.section.microbiome_analysis"]
#   gutBarrier：综合评估 <- 第一章「1.1 综合评估指数」正文
#              健康风险 <- 第二章「本章小结」（编号 2.x 是动态的，按标题关键词找）
#              （在 _gut_barrier_followup_input 里提取，放在 instruction_summary）
# 找不到原文时由提示词按兜底句式生成。
_EVALUATE_RISK_SOURCE_KEY = "report.section.microbiome_analysis"
_SUMMARY_SOURCE_LIMIT = 1200  # 参考原文过长时截断，避免挤占提示词


def _instruction_summary_sources(data: dict[str, Any]) -> dict[str, str]:
    """返回 {"body_mass": 综合评估参考原文, "risk_warning": 健康风险参考原文}，没有时为空串。"""
    data = data if isinstance(data, dict) else {}
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        summary = data.get("instruction_summary") if isinstance(data.get("instruction_summary"), dict) else {}
        body_mass, risk = summary.get("body_mass"), summary.get("risk_warning")
    else:
        diagnosis = data.get("diagnosis_summary") if isinstance(data.get("diagnosis_summary"), dict) else {}
        context = diagnosis.get("clinical_context") if isinstance(diagnosis.get("clinical_context"), dict) else {}
        body_mass, risk = diagnosis.get("conclusion"), context.get(_EVALUATE_RISK_SOURCE_KEY)
    return {
        "body_mass": _gb_text(body_mass)[:_SUMMARY_SOURCE_LIMIT],
        "risk_warning": _gb_text(risk)[:_SUMMARY_SOURCE_LIMIT],
    }


# ---- 个体体质特征（info.constitution）：evaluate 与 gutBarrier 共用同一模板，程序拼装 ----
#   evaluate：健康管理评估指数 + 整体状态 + 菌群失衡等级；结合您的评估问卷结果，存在××慢病风险。
#   gutBarrier：见 _gb_constitution（IBFC 分数 + 损伤类型 + 损伤分级）
#   第二句：结合您的评估问卷结果，存在××慢病风险。（见 _pick_diseases：有中 / 高风险取中高风险，最多 4 个；
#   没有中高风险时取报告里风险最高的 2 个，产品要求这里不能写「暂未见」）
_CONSTITUTION_MAX_DISEASES = 4
_LOW_RISK_FALLBACK_DISEASES = 2
_RISK_LABEL_ORDER = {"高": 0, "高风险": 0, "中": 1, "中风险": 1, "低": 2, "低风险": 2}
_UNBALANCE_LEVEL = re.compile(r"[一二三]度失衡")


def _pick_diseases(ranked: list[tuple[str, int]], limit: int) -> list[str]:
    """ranked 已按风险从高到低排好的 (疾病, 等级序号 0高/1中/2低)。有中高风险取中高风险（最多 limit 个），
    否则取风险最高的 2 个低风险疾病。"""
    mid_high = [name for name, rank in ranked if rank < 2]
    if mid_high:
        return mid_high[:limit]
    return [name for name, _ in ranked][:min(limit, _LOW_RISK_FALLBACK_DISEASES)]


def _constitution_text(first: str, ranked: list[tuple[str, int]]) -> str:
    diseases = _pick_diseases(ranked, _CONSTITUTION_MAX_DISEASES)
    # 报告里一个疾病风险都没有时不写这句（不输出「暂未见……」）
    second = f"结合您的评估问卷结果，存在{'、'.join(diseases)}慢病风险。" if diseases else ""
    return (f"{first}。" if first else "") + second


# ---- 健康风险提示（info.risk_warning）的固定结尾：机制说明 + 最严重的 2–3 个慢病风险 ----
#   模型按原报告写 1–2 句本次变化，程序在后面接上产品给的句式：
#   「肠道屏障受损，导致内毒素入血，可能引起全身性慢性低度炎症，会引发各类慢病的产生。其中××慢病风险较高。」
#   机制句按事实选：屏障有损伤等级 -> 屏障句；
#   evaluate 有菌群失衡等级 -> 菌群句；都没有就不写机制句。慢病取法同 _pick_diseases（最多 3 个）。
_RISK_TAIL_MAX_DISEASES = 3
_RISK_MECHANISM_BARRIER = "肠道屏障受损，导致内毒素入血，可能引起全身性慢性低度炎症，会引发各类慢病的产生"
_RISK_MECHANISM_MICROBIOTA = "肠道菌群失衡，可能引起全身性慢性低度炎症，会引发各类慢病的产生"


def _risk_tail(mechanism: str, ranked: list[tuple[str, int]]) -> str:
    top = _pick_diseases(ranked, _RISK_TAIL_MAX_DISEASES)
    risk = f"{'其中' if mechanism else ''}{'、'.join(top)}慢病风险较高。" if top else ""
    return (f"{mechanism}。" if mechanism else "") + risk


def _evaluate_risk_diseases(assessment: dict[str, Any]) -> list[tuple[str, int]]:
    """评估报告各维度 relatedDiseases，按风险从高到低排序后返回 (疾病, 等级序号)。
    同级时所在维度评分越低越靠前（低风险兜底时取问题最大的维度），再按出现顺序；同名去重取最高风险。"""
    found: dict[str, tuple[int, float, int]] = {}
    order = 0
    for dimension in assessment.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        dimension_score = _gb_float(_gb_text(dimension.get("score")))
        dimension_score = 999.0 if dimension_score is None else dimension_score
        for disease in dimension.get("relatedDiseases") or []:
            if not isinstance(disease, dict):
                continue
            name, label = _gb_text(disease.get("name")), _gb_text(disease.get("riskLabel"))
            rank = _RISK_LABEL_ORDER.get(label)
            if not name or rank is None or "复核" in name:
                continue
            key = (rank, dimension_score, order)
            if name not in found or key < found[name]:
                found[name] = key
            order += 1
    return [(name, found[name][0]) for name in sorted(found, key=found.get)]


def _evaluate_constitution(data: dict[str, Any], info: dict[str, Any]) -> str:
    assessment = data.get("healthAssessment") if isinstance(data.get("healthAssessment"), dict) else {}
    score = _gb_text(assessment.get("displayOverallScore")) or _gb_text(assessment.get("overallScore"))
    if assessment.get("overallScoreAvailable") is False:
        score = ""
    level = _gb_text(assessment.get("levelLabel"))
    parts = []
    if score:
        parts.append(f"您的健康管理评估指数为{score}分" + (f"，整体处于「{level}」状态" if level else ""))
    elif level:
        parts.append(f"您的健康管理评估整体处于「{level}」状态")
    unbalance = _UNBALANCE_LEVEL.search(_gb_text(info.get("unbalance_type")))
    if unbalance:
        parts.append(f"肠道菌群为{unbalance.group(0)}" if parts else f"您的肠道菌群为{unbalance.group(0)}")
    return _constitution_text("，".join(parts), _evaluate_risk_diseases(assessment))


def _instruction_constitution(data: dict[str, Any], info: dict[str, Any] | None = None) -> str:
    """个体体质特征（info.constitution）。gutBarrier 在 _gut_barrier_followup_input 里已拼好
    （放在 instruction_summary，需要原报告章节）；evaluate 用 healthAssessment + 模型给出的菌群失衡等级。"""
    data = data if isinstance(data, dict) else {}
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        summary = data.get("instruction_summary") if isinstance(data.get("instruction_summary"), dict) else {}
        return _gb_text(summary.get("constitution"))
    return _evaluate_constitution(data, info if isinstance(info, dict) else {})


def _instruction_risk_tail(data: dict[str, Any], info: dict[str, Any] | None = None) -> str:
    """健康风险提示的固定结尾。gutBarrier 已在 instruction_summary 里拼好；evaluate 用菌群失衡等级 + healthAssessment。"""
    data = data if isinstance(data, dict) else {}
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        summary = data.get("instruction_summary") if isinstance(data.get("instruction_summary"), dict) else {}
        return _gb_text(summary.get("risk_tail"))
    info = info if isinstance(info, dict) else {}
    assessment = data.get("healthAssessment") if isinstance(data.get("healthAssessment"), dict) else {}
    mechanism = _RISK_MECHANISM_MICROBIOTA if _UNBALANCE_LEVEL.search(_gb_text(info.get("unbalance_type"))) else ""
    return _risk_tail(mechanism, _evaluate_risk_diseases(assessment))


# 综合评估里模型偶尔仍会带上「（参考＜0.5 mg/L）」之类的参考范围，读起来晦涩，统一去掉。
_REFERENCE_RANGE_NOTE = re.compile(r"[（(]\s*参考(?:范围|区间|值)?[^（）()]*[）)]")


def _polish_body_mass(text: Any) -> Any:
    """综合评估（body_mass）后处理，evaluate 与 gutBarrier 共用：去掉参考范围括注，
    指标异常统一写成「××指标异常」，末尾分号改句号。"""
    if not isinstance(text, str) or not text:
        return text
    result = _normalize_abnormal_wording(_REFERENCE_RANGE_NOTE.sub("", text).strip())
    if result.endswith(("；", ";")):
        result = result[:-1] + "。"
    return result


def _source_fallback(source: str, sentences: int = 2) -> str:
    """模型没给出内容时，退回参考原文的前几句（去掉转诊类句子）。"""
    parts = [seg.strip() for seg in re.split(r"(?<=[。！；])", source or "") if seg.strip()]
    kept = [seg for seg in parts if not any(word in seg for word in _REFERRAL_WORDS)]
    return "".join(kept[:sentences])


# 综合评估 / 健康风险属于医生出具的医嘱，不保留原文里的转诊类句子
_REFERRAL_WORDS = ("就医", "就诊", "专科", "医疗机构", "医生建议", "按医嘱")


async def _info(data):
    sources = _instruction_summary_sources(data)
    user_prompt = _render_followup_prompt(
        "info",
        evaluate_json=json.dumps(data, ensure_ascii=False),
        body_mass_source=sources["body_mass"] or "（无）",
        risk_warning_source=sources["risk_warning"] or "（无）",
    )

    response = await safe_complete_async(prompt=user_prompt, task_type="info")
    parsed = _extract_json(response)
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        parsed = without_crp_content(parsed)
    info = _coalesce(parsed, FOLLOWUP_DEFAULTS["info"])
    info["body_mass"] = _polish_body_mass(info.get("body_mass") or _source_fallback(sources["body_mass"])) or info.get("body_mass")
    risk = info.get("risk_warning") or _source_fallback(sources["risk_warning"])
    # 模型写本次变化，程序接上「机制说明 + 最严重的 2–3 个慢病风险」
    risk = _gb_join_sentences([_gb_text(risk), _instruction_risk_tail(data, info)])
    info["risk_warning"] = _polish_risk_warning(risk, data)
    info["constitution"] = _instruction_constitution(data, info)
    return info


# ---- gutBarrier：PFMT 一定要出 ----
# 肠道屏障报告的受检者往往不做菌群检测，没有肠型；产品要求这种情况也必须给出 PFMT 推荐。
# 配型以肠型为主、性别为辅（产品《FMT 配型》文档）：
#   肠型 I（拟杆菌型）     -> 供体肠型 III（瘤胃球菌型）
#   肠型 II（普雷沃氏菌型） -> 避免同肠型，优选供体肠型 I（拟杆菌型）
#   肠型 III（瘤胃球菌型）  -> 供体肠型 I（拟杆菌型，高阿克曼菌 / 丁酸梭菌）
# 选品顺序见 _pfmt_pools；模型选的产品落在第一个非空档位里就保留，否则由程序纠正。只作用于 gutBarrier。
_PFMT_METHOD = "健康人源肠道菌群移植胶囊(PFMT)"
_MDF_METHOD = "肠道微生态靶向调节剂(MDF)"
# recommend 里临时携带的配型说明，instruction_report 读取后剔除，不进入输出结构
_PFMT_MATCH_NOTE_KEY = "_pfmt_match_note"


def _candidate_enterotype(candidate: dict[str, Any]) -> str:
    for key in ("applicableEnterotypeName", "applicableEnterotype", "productName"):
        code = _enterotype_code(candidate.get(key))
        if code:
            return code
    return ""


def _gender_fits(candidate: dict[str, Any], gender: str) -> bool:
    """产品未标性别视为不限性别。"""
    candidate_gender = _normalize_gender(candidate.get("applicableGender"))
    return not candidate_gender or candidate_gender == gender


def _pfmt_pools(enabled: list[dict[str, Any]], *, gender: str, enterotype: str):
    """按优先级依次产出 (候选档位, 按肠型匹配?, 按性别匹配?)：肠型优先，性别其次。"""
    donor = _PFMT_DONOR_ENTEROTYPE.get(enterotype, "")
    if donor:
        by_donor = [c for c in enabled if _candidate_enterotype(c) == donor]
        if gender:
            yield [c for c in by_donor if _gender_fits(c, gender)], True, True
        yield by_donor, True, False
        # 没有规则供体时至少避开同肠型（肠型 II 文档明确要求，其余肠型同样适用）
        other = [c for c in enabled if _candidate_enterotype(c) != enterotype]
        if gender:
            yield [c for c in other if _gender_fits(c, gender)], False, True
        yield other, False, False
    if gender:
        yield [c for c in enabled if _gender_fits(c, gender)], False, True
    yield enabled, False, False


def _select_pfmt(candidates, capsule, *, gender: str, enterotype: str,
                 preferred: str = "") -> tuple[str, tuple[bool, bool]]:
    """选一款启用的 PFMT，返回 (productCode, (按肠型匹配?, 按性别匹配?))；无可选产品时 code 为空。"""
    enabled = [
        candidate for candidate in candidates or []
        if isinstance(candidate, dict)
        and candidate.get("productCode") in capsule
        and (_gb_text(candidate.get("productStatus")) or "启用") == "启用"
    ]
    for pool, by_enterotype, by_gender in _pfmt_pools(enabled, gender=gender, enterotype=enterotype):
        if pool:
            codes = [candidate["productCode"] for candidate in pool]
            return (preferred if preferred in codes else codes[0]), (by_enterotype, by_gender)
    return "", (False, False)


# 产品卡片上 PFMT 推荐理由后的固定说明默认是「按您的肠型与性别一对一配型」，
# gutBarrier 按实际配型依据改写，避免说法与事实不符。
_PFMT_MATCH_NOTES = {
    (True, True): None,  # 保持默认文案
    (True, False): "（按您的肠型匹配）",
    (False, True): "（按您的性别匹配）",
    (False, False): "（按产品目录匹配）",
}


def _ensure_gut_barrier_pfmt(parsed: dict[str, Any], data: dict[str, Any], info: Any,
                             pfmt_candidates, capsule, *, has_mdf: bool) -> None:
    """gutBarrier：保证方案里有 PFMT，并按配型依据确定具体产品（原地修改 parsed）。"""
    subject = data.get("subject") if isinstance(data.get("subject"), dict) else {}
    gender = _normalize_gender(subject.get("gender"))
    enterotype = _enterotype_code(data.get("enterotype")) or _enterotype_code(
        info.get("intestine_type") if isinstance(info, dict) else "")
    code, basis = _select_pfmt(
        pfmt_candidates, capsule, gender=gender, enterotype=enterotype,
        preferred=_gb_text(parsed.get("pfmt_code")),
    )
    if not code:
        return  # 目录里没有可用的启用产品，无法推荐
    method = _gb_text(parsed.get("recommended_combination_method"))
    if _PFMT_METHOD not in method:
        parsed["recommended_combination_method"] = (
            f"{_MDF_METHOD} + {_PFMT_METHOD} + 生活方式"
            if has_mdf and _MDF_METHOD in method else f"{_PFMT_METHOD} + 生活方式"
        )
    parsed["pfmt_code"] = code
    parsed[_PFMT_MATCH_NOTE_KEY] = _PFMT_MATCH_NOTES[basis]
    logger.info(
        "gut barrier PFMT matched: code=%s enterotype=%s gender=%s by_enterotype=%s by_gender=%s",
        code, enterotype or "-", gender or "-", *basis,
    )


def _take_pfmt_match_note(recommend: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """取出 recommend 里临时携带的配型说明，返回不含该字段的副本（不改原对象，重试安全）。"""
    if not isinstance(recommend, dict) or _PFMT_MATCH_NOTE_KEY not in recommend:
        return recommend, None
    note = recommend[_PFMT_MATCH_NOTE_KEY]
    return {key: value for key, value in recommend.items() if key != _PFMT_MATCH_NOTE_KEY}, note


def _adjust_gut_barrier_pfmt_note(non_drug_intervention: dict[str, Any], note: str | None) -> None:
    if not note:
        return
    gut = (non_drug_intervention or {}).get("gut_microbiota_regulation") or {}
    for product in (gut.get("probiotics") or {}).get("products") or []:
        if isinstance(product, dict) and product.get("type") == "PFMT" and "rationale_note" in product:
            product["rationale_note"] = note


async def _recommend(data, mdf_code, fixed, random_strains, capsule, mdf_details, pfmt_details, info, pfmt_candidates):
    has_mdf = bool(fixed or random_strains)
    has_pfmt = bool(pfmt_candidates)

    # 1. 动态生成【个性化干预方案库】
    options_list = []
    if has_mdf and not has_pfmt:
        options_list.append("肠道微生态靶向调节剂(MDF) + 生活方式")
    elif has_pfmt and not has_mdf:
        options_list.append("健康人源肠道菌群移植胶囊(PFMT) + 生活方式")
    elif has_mdf and has_pfmt:
        options_list.append("肠道微生态靶向调节剂(MDF) + 生活方式")
        options_list.append("健康人源肠道菌群移植胶囊(PFMT) + 生活方式")
        options_list.append("肠道微生态靶向调节剂(MDF) + 健康人源肠道菌群移植胶囊(PFMT) + 生活方式")
    else:
        options_list.append("生活方式干预")

    options_text = "\n".join([f"{i + 1}. {opt}" for i, opt in enumerate(options_list)])

    # 2. 动态生成提取逻辑任务要求
    if has_mdf:
        step_1_mdf = f"1. MDF动态匹配：将该产品的专属代码 \"{mdf_code}\" 填入 `mdf_code` 字段。并结合评估数据，从候选库 {random_strains} 中挑选 **2种** 益生菌输出到 `MDF` 字段。"
    else:
        step_1_mdf = "1. MDF动态匹配：当前暂无MDF方案，请将 `mdf_code` 字段输出为空字符串 \"\"，`MDF` 字段输出为空列表 []。"

    # 3. 动态生成融入《FMT配型手册》核心规则的 PFMT 步骤（优化字段指向）
    if has_pfmt:
        step_2_pfmt = f"""2. PFMT胶囊配型与匹配：严格根据受检者的【肠型】与【性别】，执行以下核心配型规则，从【待匹配胶囊库】中匹配 **1种** 最合适胶囊的 `productCode` 输出到 `pfmt_code` 字段，并将对应产品名称输出到 `PFMT` 字段：
       【核心配型规则】：
       (1) 肠型 ➔ 供体方向映射：
           - 若受检者为【肠型 I（拟杆菌型）】：首选【肠型 III（瘤胃球菌型）】供体，即匹配 `applicableEnterotypeName` 为“瘤胃球菌型” / 产品名含“肠型III”的产品。
           - 若受检者为【肠型 II（普雷沃氏菌型）】：首选【肠型 I（拟杆菌型）】供体，即匹配 `applicableEnterotypeName` 为“拟杆菌型” / 产品名含“肠型I”的产品。
           - 若受检者为【肠型 III（瘤胃球菌型）】：首选【肠型 I（拟杆菌型）】供体，即匹配 `applicableEnterotypeName` 为“拟杆菌型” / 产品名含“肠型I”的产品。
       (2) 性别一致性：
           - 受检者为男性时，匹配 `applicableGender == "男"` 的产品；
           - 受检者为女性时，匹配 `applicableGender == "女"` 的产品。
       (3) 状态有效性：仅限匹配 `productStatus == "启用"` 的产品。

       【待匹配胶囊库 (fecalMicrobiotaTransplantationCapsule)】：
       {json.dumps(pfmt_candidates, ensure_ascii=False, indent=2)}"""
    else:
        step_2_pfmt = "2. PFMT胶囊匹配：当前暂无可用PFMT方案，请将 `pfmt_code` 和 `PFMT` 字段均输出为空字符串 \"\"。"

    # 4. 动态生成产品推荐理由 (拆分 MDF 和 PFMT)
    rationale_steps = []
    if has_mdf:
        rationale_steps.append(
            "- `mdf_product_rationale`: 一句话写明针对的菌群缺口与作用（如：“补足产丁酸菌不足，协助平衡肠道菌群，改善代谢状态”）。\n"
            "         【严格要求】：精简，控制在 30 字以内，只写一句；使用通俗易懂的“大白话”；严禁罗列具体菌株名称或数值；严禁出现“固定菌株”、“动态挑选”等字眼；"
            "不得关联结节、结石、疑似感染等需要就医处理的问题；末尾不要写“按您的菌群缺口个性化调配”（程序会自动追加）。"
        )
    if has_pfmt:
        rationale_steps.append(
            "- `pfmt_product_rationale`: 一句话写明作用（如：“重建菌群丰富度与群落稳定性，恢复肠道微生态平衡”）。\n"
            "         【严格要求】：精简，控制在 30 字以内，只写一句；使用通俗易懂的“大白话”；不写数值；"
            "不得关联结节、结石、疑似感染等需要就医处理的问题；不要写肠型、性别配型信息（程序会自动追加“按您的肠型与性别一对一配型”）。"
        )

    if rationale_steps:
        step_4_rationale = "4. 产品推荐理由 (Product Rationale)：分别解释挑选对应产品的原因（若未推荐该类产品，对应字段请输出为空字符串 \"\"）：\n       " + "\n       ".join(
            rationale_steps)
    else:
        step_4_rationale = "4. 产品推荐理由：当前暂无MDF或PFMT方案，请将 `mdf_product_rationale` 和 `pfmt_product_rationale` 字段均输出为空字符串 \"\"。"

    user_prompt = _render_followup_prompt(
        "recommend",
        evaluate_json=json.dumps(data, ensure_ascii=False),
        info_json=json.dumps(info, ensure_ascii=False),
        options_text=options_text,
        step_1_mdf=step_1_mdf,
        step_2_pfmt=step_2_pfmt,
        step_4_rationale=step_4_rationale,
    )

    response = await safe_complete_async(prompt=user_prompt, task_type="recommend")
    parsed = _extract_json(response)
    gut_barrier_pfmt = _source_type(data) == SOURCE_TYPE_GUT_BARRIER and has_pfmt
    if not isinstance(parsed, dict) or not isinstance(parsed.get("recommended_combination_method"), str) or not parsed["recommended_combination_method"].strip():
        if not gut_barrier_pfmt:
            raise ValueError("recommend model returned no usable selection")
        # gutBarrier 必须出 PFMT：模型没给出可用方案时，由下方程序兜底补全
        parsed = {"recommended_combination_method": ""}
    parsed_json = _coalesce(parsed, FOLLOWUP_DEFAULTS["recommend"])
    if gut_barrier_pfmt and isinstance(parsed_json, dict):
        _ensure_gut_barrier_pfmt(parsed_json, data, info, pfmt_candidates, capsule, has_mdf=has_mdf)

    # 4. Python层数据融合与安全校验
    if isinstance(parsed_json, dict):
        recommended_method = parsed_json.get("recommended_combination_method") or ""

        # 4.1 强制校准 MDF 数据
        if has_mdf:
            parsed_json["mdf_code"] = mdf_code
            dynamic_mdf = parsed_json.get("MDF", [])
            if isinstance(dynamic_mdf, list):
                combined_mdf = fixed + dynamic_mdf
            else:
                combined_mdf = list(fixed)
            random.shuffle(combined_mdf)
            parsed_json["MDF"] = combined_mdf
        else:
            parsed_json["mdf_code"] = ""
            parsed_json["MDF"] = []

        # 4.2 强制校准 PFMT 数据
        if has_pfmt:
            p_code = parsed_json.get("pfmt_code", "")
            if p_code in capsule:
                parsed_json["PFMT"] = capsule[p_code]
            else:
                parsed_json["pfmt_code"] = ""
                parsed_json["PFMT"] = ""
        else:
            parsed_json["pfmt_code"] = ""
            parsed_json["PFMT"] = ""

        # 4.3 动态注入产品详细属性，并严防作用/观察重点及推荐理由的幻觉
        if "肠道微生态靶向调节剂(MDF)" in recommended_method and mdf_code:
            parsed_json["mdf_productSpecification"] = mdf_details.get("productSpecification", "")
            parsed_json["mdf_shelfLife"] = mdf_details.get("shelfLife", "")
            parsed_json["mdf_compositeEfficacyComponents"] = mdf_details.get("compositeEfficacyComponents", "")
            parsed_json["mdf_usageInstructions"] = mdf_details.get("usageInstructions", "")
        else:
            parsed_json["mdf_action_focus"] = ""
            parsed_json["mdf_observation_focus"] = ""
            parsed_json["mdf_product_rationale"] = ""  # 方案中无MDF时强制清空

        if "健康人源肠道菌群移植胶囊(PFMT)" in recommended_method:
            p_code = parsed_json.get("pfmt_code", "")
            if p_code in pfmt_details:
                # 活菌量按产品固定规格，不取目录里的值
                parsed_json["pfmt_viableCountPerCapsule"] = PFMT_VIABLE_COUNT_PER_CAPSULE
                parsed_json["pfmt_totalViableCountPerBox"] = PFMT_TOTAL_VIABLE_COUNT_PER_BOX
                # 用法用量按产品固定方案，不取目录里的 usageInstructions
                parsed_json["pfmt_usageInstructions"] = PFMT_USAGE_INSTRUCTIONS
        else:
            parsed_json["pfmt_action_focus"] = ""
            parsed_json["pfmt_observation_focus"] = ""
            parsed_json["pfmt_product_rationale"] = ""  # 方案中无PFMT时强制清空

    return parsed_json



# 用法用量
def _pfmt_usage(recommend: dict[str, Any]) -> list[str]:
    """方案含 PFMT 时返回固定用法用量（逐条），否则空列表。"""
    return list(PFMT_USAGE) if recommend.get("pfmt_usageInstructions") else []


async def _usage(recommend):
    # PFMT 用法用量是产品固定方案，直接输出；只有 MDF 的目录用法交给模型润色
    pfmt_usage = _pfmt_usage(recommend)
    mdf_raw = recommend.get("mdf_usageInstructions", "")

    # 没有 MDF 用法时不调用模型
    if not mdf_raw:
        return {"pfmt_usage": pfmt_usage, "mdf_usage": []}

    user_prompt = _render_followup_prompt(
        "usage",
        pfmt_raw=repr(""),
        mdf_raw=repr(mdf_raw),
    )

    response = await safe_complete_async(prompt=user_prompt, task_type="usage")
    # 润色失败时回退到产品目录给出的原始用法用量，宁可不润色也不能丢内容。
    usage = _coalesce(_extract_json(response), {"pfmt_usage": [], "mdf_usage": [mdf_raw]})
    if isinstance(usage, dict):
        usage["pfmt_usage"] = pfmt_usage
    return usage


# 异常处置
async def _exception_handling(recommend):
    # 【业务逻辑防护】如果推荐方案中不包含 PFMT 胶囊，则无需这套冷冻异常处置，直接返回空列表
    recommended_method = recommend.get("recommended_combination_method") or ""
    if "健康人源肠道菌群移植胶囊(PFMT)" not in recommended_method:
        return []

    user_prompt = _render_followup_prompt("exception_handling")

    response = await safe_complete_async(prompt=user_prompt, task_type="exception_handling")
    return _coalesce(_extract_json(response), FOLLOWUP_DEFAULTS["exception_handling"])


# [已移除] 医嘱说明 _doctor_advice：由 non_drug_intervention.lifestyle_intervention 取代。


# 注意事项
async def _notes(recommend):
    recommended_method = recommend.get("recommended_combination_method") or ""
    has_pfmt = "健康人源肠道菌群移植胶囊(PFMT)" in recommended_method
    has_mdf = "肠道微生态靶向调节剂(MDF)" in recommended_method

    raw_notes = notes_base(has_mdf=has_mdf, has_pfmt=has_pfmt)
    input_data_json = json.dumps(raw_notes, ensure_ascii=False, indent=2)

    user_prompt = _render_followup_prompt(
        "notes",
        input_data_json=input_data_json,
    )

    response = await safe_complete_async(prompt=user_prompt, task_type="notes")
    # 润色失败就直接发未润色的注意事项，禁忌条款一条都不能丢。
    return _coalesce(_extract_json(response), raw_notes)


# ================= [已停用] 干预方案(intervene) 专用子任务 =================
# 产品暂不需要干预方案报告，以下 _diet / _exercise / _supplements 仅被 intervene_report 使用，
# 先注释保留，后续恢复时整段取消注释即可。
# # 饮食干预
# async def _diet(data, recommend):
#     user_prompt = _render_followup_prompt(
#         "diet",
#         evaluate_json=json.dumps(data, ensure_ascii=False),
#         recommend_json=json.dumps(recommend, ensure_ascii=False),
#     )

#     response = await safe_complete_async(prompt=user_prompt, task_type="diet")
#     return _coalesce(_extract_json(response), FOLLOWUP_DEFAULTS["diet"])


# # 运动与行为干预
# async def _exercise(data, recommend):
#     user_prompt = _render_followup_prompt(
#         "exercise",
#         evaluate_json=json.dumps(data, ensure_ascii=False),
#         recommend_json=json.dumps(recommend, ensure_ascii=False),
#     )
#     response = await safe_complete_async(prompt=user_prompt, task_type="exercise")
#     return _coalesce(_extract_json(response), FOLLOWUP_DEFAULTS["exercise"])


# # 营养与微量元素补剂干预
# async def _supplements(data, recommend):
#     user_prompt = _render_followup_prompt(
#         "supplements",
#         evaluate_json=json.dumps(data, ensure_ascii=False),
#         recommend_json=json.dumps(recommend, ensure_ascii=False),
#     )

#     response = await safe_complete_async(prompt=user_prompt, task_type="supplements")
#     return _coalesce(_extract_json(response), FOLLOWUP_DEFAULTS["supplements"])


# ================= 三位一体非药物干预方案 (non_drug_intervention) =================
# 组织顺序固定：生活方式干预 → 肠道菌群调节 → 营养素推荐 → 就医提示。
# 全部依据评估报告传入的源事实生成；与产品相关的禁忌条款由 followup_defaults
# 中的确定性文案覆盖，模型不得改写。

_MAX_POINTS = 3
_MAX_SUPPLEMENT_ITEMS = 3
# 营养素模块不得出现菌类产品，避免与 MDF/PFMT（肠道菌群调节）冲突。
_SUPPLEMENT_BLOCKED_WORDS = ("益生菌", "益生元", "合生元", "后生元", "菌")


def _method_flags(recommend: dict[str, Any]) -> tuple[str, bool, bool]:
    method = recommend.get("recommended_combination_method", "") or "生活方式干预"
    has_pfmt = "健康人源肠道菌群移植胶囊(PFMT)" in method
    has_mdf = "肠道微生态靶向调节剂(MDF)" in method
    return method, has_mdf, has_pfmt


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_points(value: Any, limit: int = _MAX_POINTS) -> list[str]:
    if not isinstance(value, list):
        return []
    return [p.strip() for p in value if isinstance(p, str) and p.strip()][:limit]


def _normalize_lifestyle(parsed: Any) -> dict[str, dict[str, Any]]:
    """模型输出 → {key: {summary, points}}；缺失或类型不对的条目直接丢弃，
    由 lifestyle_intervention_base 回退到通用文案。"""
    source = parsed if isinstance(parsed, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for key in LIFESTYLE_ITEM_TITLES:
        item = source.get(key)
        if not isinstance(item, dict):
            continue
        summary = _clean_text(item.get("summary"))
        if not summary:
            continue
        result[key] = {"summary": summary, "points": _clean_points(item.get("points"))}
    return result


def _normalize_supplement_items(parsed: Any) -> list[dict[str, Any]]:
    source = parsed.get("items") if isinstance(parsed, dict) else None
    items: list[dict[str, Any]] = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        row = {field: _clean_text(item.get(field)) for field in SUPPLEMENT_FIELDS}
        if not row["name"] or not row["reason"]:
            continue
        if any(word in row["name"] for word in _SUPPLEMENT_BLOCKED_WORDS):
            continue
        row["usage"] = short_usage(row["usage"])
        refs = item.get("evidence_refs")
        row["evidence_refs"] = [str(r).strip() for r in refs if str(r).strip()] if isinstance(refs, list) else []
        items.append(row)
        if len(items) >= _MAX_SUPPLEMENT_ITEMS:
            break
    return items  # 为空时由 supplements_base 给出基础建议


async def _lifestyle(data, method):
    user_prompt = _render_followup_prompt(
        "lifestyle",
        evaluate_json=json.dumps(data, ensure_ascii=False),
        recommend_method=method,
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="lifestyle")
    return _normalize_lifestyle(_extract_json(response))


def _clean_phrase(value: Any, limit: int = 40) -> str:
    text = _clean_text(value).rstrip("。，,；;、 ")
    return text if 0 < len(text) <= limit else ""


def _clean_block(value: Any) -> dict[str, Any] | None:
    """{summary, points} 块：summary 为空视为无效，交给兜底。"""
    if not isinstance(value, dict):
        return None
    summary = _clean_text(value.get("summary"))
    if not summary:
        return None
    return {"summary": summary, "points": _clean_points(value.get("points"))}


# 结节、结石、感染等需要医疗处理的问题，不能写进微生态调节的开头话术。
_GUT_RISK_BLOCKED_WORDS = ("结节", "结石", "占位", "间质瘤", "肿瘤", "结核", "感染", "息肉")


def _normalize_gut_microbiota(parsed: Any, data: dict[str, Any]) -> dict[str, Any]:
    """规整模型输出；与源事实不符或缺失的部分丢弃，由 gut_microbiota_regulation_base 兜底。"""
    source = parsed if isinstance(parsed, dict) else {}
    # 输入里没有任何异常事实时，菌群解读与开头问题一律走固定文案，杜绝虚构。
    # 肠道与炎症报告（gutBarrier）根本没有菌群检测结果，菌群解读同样只用固定文案；
    # 肠屏障修复、益生元等其余部分照常按模型输出处理。
    has_facts = not _build_scope_note(data) and _source_type(data) != SOURCE_TYPE_GUT_BARRIER
    result: dict[str, Any] = {}

    if has_facts:
        # 开头话术两段：菌群状况 + 可能导致或加重的疾病/风险/症状
        for key in ("microbiota_state", "linked_risks"):
            phrase = _clean_phrase(source.get(key), limit=30)
            # 需要医疗处理的问题不作为微生态调节的落点（兜底模型没遵守 prompt 的情况）
            if key == "linked_risks" and any(w in phrase for w in _GUT_RISK_BLOCKED_WORDS):
                phrase = ""
            if phrase:
                result[key] = phrase
        findings = _clean_block(source.get("microbiota_findings"))
        if findings:
            raw = source.get("microbiota_findings") or {}
            refs = raw.get("evidence_refs") if isinstance(raw, dict) else None
            findings["evidence_refs"] = (
                [str(r).strip() for r in refs if str(r).strip()] if isinstance(refs, list) else []
            )
            result["microbiota_findings"] = findings

    prebiotics = _clean_block(source.get("prebiotics"))
    if prebiotics:
        result["prebiotics"] = prebiotics

    # 肠屏障修复：模型判定适用 且 源数据里确实出现过屏障相关检测项，二者同时满足才输出。
    barrier = source.get("gut_barrier")
    data_text = json.dumps(data, ensure_ascii=False)
    if (
        isinstance(barrier, dict)
        and barrier.get("applicable") is True
        and any(marker in data_text for marker in GUT_BARRIER_MARKERS)
    ):
        block = _clean_block(barrier)
        if block:
            result["gut_barrier"] = {"applicable": True, **block}

    synergy = _clean_text(source.get("diet_synergy"))
    if synergy:
        result["diet_synergy"] = synergy
    return result


async def _gut_microbiota(data, recommend, method):
    # 产品推荐理由、MDF 营养底物均取自固定资料 / recommend，本任务不接收产品信息。
    user_prompt = _render_followup_prompt(
        "gut_microbiota",
        evaluate_json=json.dumps(data, ensure_ascii=False),
        recommend_method=method,
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="gut_microbiota")
    return _normalize_gut_microbiota(_extract_json(response), data)


async def _nutrient_supplements(data, method):
    # 循证依据只能来自源报告 evidence。整包丢过去模型挑不准，先由程序筛成候选短名单。
    user_prompt = _render_followup_prompt(
        "nutrient_supplements",
        evaluate_json=json.dumps(data, ensure_ascii=False),
        recommend_method=method,
        evidence_candidates=format_supplement_evidence_candidates(
            supplement_evidence_pool(data)
        ),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="nutrient_supplements")
    return _normalize_supplement_items(_extract_json(response))


async def _non_drug_intervention(data, recommend):
    """三位一体非药物干预方案：三个模型子任务并发，再按固定顺序组装。"""
    method, has_mdf, has_pfmt = _method_flags(recommend)
    lifestyle, gut, supplement_items = await _gather_followup_components(
        _followup_component("lifestyle", lambda: _lifestyle(data, method), {}),
        _followup_component("gut_microbiota", lambda: _gut_microbiota(data, recommend, method), {}),
        _followup_component("nutrient_supplements", lambda: _nutrient_supplements(data, method), []),
    )
    return non_drug_intervention_base(
        has_mdf=has_mdf,
        has_pfmt=has_pfmt,
        recommend=recommend,
        lifestyle=lifestyle,
        gut=gut,
        supplement_items=supplement_items,
    )


# ================= 推荐检测 (recommended_tests) =================
# 结合源数据中的缺口，推荐补充的检测项目；开头话术由程序按检测项名称拼装。
# 检测名只用于拼进「我们建议您补充完善{names}检测」，去掉模型自带的「检测/检查」后缀避免重复。
_TEST_NAME_SUFFIXES = ("检测", "检查")


def _normalize_recommended_tests(parsed: Any) -> list[dict[str, Any]]:
    source = parsed.get("items") if isinstance(parsed, dict) else None
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        name, reason = _clean_text(item.get("name")), _clean_text(item.get("reason"))
        for suffix in _TEST_NAME_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
        if not name or not reason or name in seen:
            continue
        seen.add(name)
        timeframe = _clean_text(item.get("timeframe")).replace(" ", "")
        if timeframe not in RECOMMENDED_TEST_TIMEFRAMES:
            timeframe = RECOMMENDED_TEST_TIMEFRAME_DEFAULT
        items.append({"name": name, "reason": reason, "timeframe": timeframe})
        if len(items) >= RECOMMENDED_TESTS_MAX_ITEMS:
            break
    return items  # 为空时由 recommended_tests_base 按源数据给出基础推荐


def _recommended_tests_source(data: dict[str, Any]) -> dict[str, Any] | None:
    """推荐检测兜底用来判断「源数据里有没有菌群检测」的数据。

    该判断是关键词匹配，肠道与炎症报告里「幽门螺杆菌」之类的字样会被误判成有菌群数据，
    所以 gutBarrier 直接按「无菌群数据」处理；evaluate 保持原样。
    """
    return None if _source_type(data) == SOURCE_TYPE_GUT_BARRIER else data


async def _recommended_tests(data):
    """组件重试与降级由报告装配层统一处理。"""
    user_prompt = _render_followup_prompt(
        "recommended_tests",
        evaluate_json=json.dumps(data, ensure_ascii=False),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="recommended_tests")
    items = _normalize_recommended_tests(_extract_json(response))
    if not items:
        gaps = _ACTIVE_FOLLOWUP_GAPS.get()
        if gaps is not None:
            gaps.add("missing_recommended_tests")
    return recommended_tests_base(items, evaluate=_recommended_tests_source(data))


###########################  报告解读  ###########################
# 手册共 5 个子任务：四组「五件套」+ 开场白/收尾脚本，互不依赖，全部并发。
# 模型只把评估报告的书面语改写成可照读的口语话术；数值、编号、速查卡等
# 一律由 explain_context 直接搬运，不经过模型（红线 1「数据逐字」）。

async def _explain_section(
    part_no: int,
    contexts: dict[str, Any],
    *,
    issues: list[str],
    allowed: frozenset[str],
) -> dict[str, Any]:
    """生成一组五件套。

    模型输出先按同一份上下文的确定性兜底补齐缺字段，再过一遍归一化：
    条数字数截断、anchor 与枚举校验、结构对齐、数值防篡改。
    编号与标题由 normalize_section 钉死，避免模型改写章节名导致前端对不上。
    """
    task_type = f"explain_part{part_no}"
    user_prompt = _render_explain_prompt(
        task_type,
        meta_json=explain_dumps(contexts["meta"]),
        part_json=explain_dumps(contexts[f"part{part_no}"]),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type=task_type)
    parsed = _extract_json(response)
    if not _has_model_content(parsed, default_explain_section(part_no, contexts)):
        raise ValueError("empty explain section")
    section = _coalesce(parsed, default_explain_section(part_no, contexts))
    return normalize_explain_section(part_no, section, contexts, issues=issues, allowed=allowed)


async def _explain_opening(
    contexts: dict[str, Any],
    *,
    issues: list[str],
    allowed: frozenset[str],
) -> dict[str, Any]:
    """开场白与收尾脚本。开场白不报具体指标，只给整体轻重。"""
    card = contexts["quick_card"]
    card_summary = {
        "overall": card.get("overall", {}),
        "dimension_scores": card.get("dimension_scores", []),
        "indicator_count": len(card.get("indicator_rows") or []),
        "imaging_count": len(card.get("imaging_rows") or []),
        "microbe_row_count": card.get("microbe_row_count", 0),
    }
    user_prompt = _render_explain_prompt(
        "explain_opening",
        meta_json=explain_dumps(contexts["meta"]),
        card_json=explain_dumps(card_summary),
        top_three_json=explain_dumps(contexts["part1"]["top_three"]),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="explain_opening")
    parsed = _extract_json(response)
    if not _has_model_content(parsed, default_explain_opening(contexts)):
        raise ValueError("empty explain opening")
    opening = _coalesce(parsed, default_explain_opening(contexts))
    # 归一化里会把「三件事」强制对齐第一部分，并做字数截断与数值核对
    return normalize_explain_opening(opening, contexts, issues=issues, allowed=allowed)


# ###########################  诊断意见  ###########################
# 诊断意见 - 诊断摘要
async def opinion_summary(data):
    user_prompt = _render_followup_prompt(
        "opinion_summary",
        evaluate_json=json.dumps(data, ensure_ascii=False),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="opinion_summary")
    return _opinion_component_json(response, FOLLOWUP_DEFAULTS["opinion_summary"])


async def opinion_basis(data):
    user_prompt = _render_followup_prompt(
        "opinion_basis",
        evaluate_json=json.dumps(data, ensure_ascii=False),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="opinion_basis")
    # Absence of a generated finding is not evidence of normality.
    return _opinion_component_json(response, FOLLOWUP_DEFAULTS["opinion_basis"])


# 诊断建议 (针对性干预与生活方式改善)
async def opinion_advice(temp_data_for_advice, data):
    user_prompt = _render_followup_prompt(
        "opinion_advice",
        advice_context_json=json.dumps(temp_data_for_advice, ensure_ascii=False),
        evaluate_json=json.dumps(data, ensure_ascii=False),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="opinion_advice")
    return _opinion_component_json(response, FOLLOWUP_DEFAULTS["opinion_advice"])


# ================= 报告组装层（当前 3 份：opinion / instruction / explain） =================

# ================= [已停用] 干预方案 报告 ====================
# async def intervene_report(data, recommend, user_id, session_id):
#     subject = data.get("subject") or {}
#     sample = data.get("sample") or {}
#     diet, exercise_and_behavior, supplements = await asyncio.gather(
#         _diet(data, recommend),
#         _exercise(data, recommend),
#         _supplements(data, recommend),
#     )
#     modules = [recommend]
#     unified_evidence = extract_unified_evidence(modules, data)
#     return {
#         "type": "intervene",
#         "userId": user_id,
#         "sessionId": session_id,
#         "subject": {
#             "name": subject.get("name"),
#             "gender": subject.get("gender"),
#             "age": subject.get("age"),
#             "report_date": sample.get("report_date")
#         },
#         "recommend": recommend,
#         "diet": diet,
#         "supplements": supplements,
#         "behavior": exercise_and_behavior.get("behavior", {}),
#         "exercise": exercise_and_behavior.get("exercise", {}),
#         "evidence": unified_evidence,
#     }



# ================= 临床医嘱(新干预医嘱处方) 报告 ====================
async def instruction_report(data, info, recommend, user_id, session_id):
    subject, sample = data.get("subject") or {}, data.get("sample") or {}
    recommend, pfmt_match_note = _take_pfmt_match_note(recommend)
    _, has_mdf, has_pfmt = _method_flags(recommend)
    usage_defaults = {
        "pfmt_usage": _pfmt_usage(recommend),
        "mdf_usage": [recommend["mdf_usageInstructions"]] if recommend.get("mdf_usageInstructions") else [],
    }
    usage, exception_handling, notes, non_drug_intervention, recommended_tests = await _gather_followup_components(
        _followup_component("usage", lambda: _usage(recommend), usage_defaults),
        _followup_component("exception_handling", lambda: _exception_handling(recommend),
                            FOLLOWUP_DEFAULTS["exception_handling"] if has_pfmt else []),
        _followup_component("notes", lambda: _notes(recommend), notes_base(has_mdf=has_mdf, has_pfmt=has_pfmt)),
        _non_drug_intervention(data, recommend),
        _followup_component("recommended_tests", lambda: _recommended_tests(data),
                            recommended_tests_base(evaluate=_recommended_tests_source(data))),
    )
    # 用法用量 / 存储与异常处置 / 注意事项并入微生态调节的产品卡片，产品信息集中在一处展示
    attach_product_details(
        non_drug_intervention,
        usage=usage,
        exception_handling=exception_handling,
        notes=notes,
    )
    # 营养素的循证依据只取评估报告 evidence 中的文献（按 evidence_refs），生成展示文字。
    # 模型没给编号（含降级走兜底文案）时，先按营养素名称从候选池回填，避免这一栏长期空白。
    backfill_supplement_evidence(non_drug_intervention, data)
    _adjust_gut_barrier_pfmt_note(non_drug_intervention, pfmt_match_note)
    attach_supplement_evidence(non_drug_intervention, data.get("evidence"))
    unified_evidence = extract_unified_evidence([info, recommend, non_drug_intervention], data)
    # 按原文献编号排序后重新编号为连续的 1..n，并同步替换正文中所有 evidence_refs
    unified_evidence.sort(key=lambda e: int(e["number"]) if str(e.get("number", "")).isdigit() else 10**6)
    info, recommend = deepcopy(info), deepcopy(recommend)  # 不改动其他报告共用的原对象
    unified_evidence = renumber_evidence([info, recommend, non_drug_intervention], unified_evidence)
    # 营养素的「《文献名》[n]」展示文字按新编号重新生成
    attach_supplement_evidence(non_drug_intervention, unified_evidence)
    return {
        "type": "instruction",
        "userId": user_id,
        "sessionId": session_id,
        "subject": {
            "name": subject.get("name"),
            "gender": subject.get("gender"),
            "age": subject.get("age"),
            "report_date": sample.get("report_date")
        },
        "info": info,
        "recommend": recommend,
        "usage": usage,  #   服用方法与用法说明
        "exception_handling": exception_handling,
        "notes": notes,  #  //  注意事项，
        # 三位一体非药物干预方案，内部顺序固定：
        # lifestyle_intervention → gut_microbiota_regulation(含完整产品信息) → supplements
        "non_drug_intervention": non_drug_intervention,
        "recommended_tests": recommended_tests,  # 推荐检测：补充完善哪些检测
        "evidence": unified_evidence,
    }


# ================= 肠道与炎症报告解读（解读参考手册） ====================
# 入参 type=gutBarrier，产出 type=explain_gutBarrier。只解读第一、二章：
# 第三章修复方案与第四章机制是固定文案，第五章附录只用参考文献。

async def _gut_barrier_section(
    part_no: int,
    contexts: dict[str, Any],
    *,
    issues: list[str],
    allowed: frozenset[str],
) -> dict[str, Any]:
    """生成一章的五件套 + 本章特有块。"""
    task_type = f"gut_barrier_part{part_no}"
    user_prompt = _render_gut_barrier_prompt(
        task_type,
        meta_json=gut_barrier_dumps(contexts["meta"]),
        part_json=gut_barrier_dumps(contexts[f"part{part_no}"]),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type=task_type)
    parsed = _extract_json(response)
    if not _has_model_content(parsed, default_gut_barrier_section(part_no, contexts)):
        raise ValueError("empty gut barrier section")
    section = _coalesce(parsed, default_gut_barrier_section(part_no, contexts))
    return normalize_gut_barrier_section(part_no, section, contexts, issues=issues, allowed=allowed)


async def _gut_barrier_opening(
    contexts: dict[str, Any],
    *,
    issues: list[str],
    allowed: frozenset[str],
) -> dict[str, Any]:
    """开场白与收尾脚本；开场白不报具体数值，只给整体轻重。"""
    card = contexts["quick_card"]
    card_summary = {
        "indicator_summary": [
            {"name": row.get("检测指标", ""), "evaluation": row.get("结果评价", "")}
            for row in card.get("indicator_rows", [])
        ],
        "ibfc": card.get("ibfc", {}),
        "pending_count": len(card.get("pending_indicators") or []),
        "gap_count": len(contexts["part1"].get("data_gaps") or []),
    }
    user_prompt = _render_gut_barrier_prompt(
        "gut_barrier_opening",
        meta_json=gut_barrier_dumps(contexts["meta"]),
        card_json=gut_barrier_dumps(card_summary),
        top_three_json=gut_barrier_dumps(build_gut_barrier_top_three(contexts)),
    )
    response = await safe_complete_async(prompt=user_prompt, task_type="gut_barrier_opening")
    parsed = _extract_json(response)
    if not _has_model_content(parsed, default_gut_barrier_opening(contexts)):
        raise ValueError("empty gut barrier opening")
    opening = _coalesce(parsed, default_gut_barrier_opening(contexts))
    return normalize_gut_barrier_opening(opening, contexts, issues=issues, allowed=allowed)


async def gut_barrier_report(data, user_id, session_id):
    """产出《肠道与炎症报告解读参考手册》。"""
    contexts = build_gut_barrier_contexts(data)
    issues: list[str] = []
    allowed = explain_allowed_numbers(contexts)
    results = await _gather_followup_components(
        *(_manual_component(
            f"gut_barrier_part{part_no}",
            lambda corrections, part_no=part_no: _gut_barrier_section(part_no, contexts, issues=corrections, allowed=allowed),
            default_gut_barrier_section(part_no, contexts),
            issues=issues,
        ) for part_no in (1, 2)),
        _manual_component(
            "gut_barrier_opening",
            lambda corrections: _gut_barrier_opening(contexts, issues=corrections, allowed=allowed),
            default_gut_barrier_opening(contexts),
            issues=issues,
        )
    )
    return assemble_gut_barrier_manual(
        contexts=contexts,
        sections=results[:2],
        opening=results[2],
        user_id=user_id,
        session_id=session_id,
        issues=issues,
    )


# ================= 肠道与炎症报告 -> 评估报告字段（供 opinion / instruction 复用） ====================
# opinion_report、instruction 依赖链（info / recommend / 非药物干预 / 推荐检测）及其兜底
# 都按评估报告（evaluate）的字段取数：subject / sample / healthAssessment /
# diagnosis_summary / diagnostic_basis / diagnostic_advice / evidence。
# gutBarrier 却是 report.sections[].blocks[] 的排版结构，原样传进去只能拿到空壳。
#
# 这里复用 build_gut_barrier_contexts 已经解析好的事实，搬成 evaluate 的字段形状：
# 只做确定性搬运，不调模型、不编造数值。opinion / instruction 的生成与组装代码
# 不区分来源，所以对外报文结构与 evaluate 来源的完全一致。

# 第二章各系统标题 -> 评估报告六维编码；匹配不上的用通用编码
_GUT_BARRIER_SYSTEM_CODES: tuple[tuple[str, str], ...] = (
    ("心脑血管", "cardiovascular_system"),
    ("心血管", "cardiovascular_system"),
    ("代谢", "metabolic_system"),
    ("消化", "digestive_system"),
    ("免疫", "immune_system"),
    ("泌尿", "urinary_system"),
    ("神经", "nervous_system"),
)
# 第二章「机制关联」列里的分段标签
_GUT_BARRIER_MECHANISM_PARTS: tuple[tuple[str, str], ...] = (
    ("机制关联", "mechanism"),
    ("本次依据", "basis"),
    ("随访重点", "action"),
)
# 复核优先级：只用于排序，取值与评估报告一致
_GUT_BARRIER_PRIORITIES: tuple[str, ...] = ("优先复核", "建议复核", "常规关注")
# [评估16]、[补19] 指向源报告内部的文献编号，转换后编号体系不同，去掉以免引用错位
_GUT_BARRIER_REF_MARK = re.compile(r"\[(?:评估|补)?\d+\]")


def _gb_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    return ""


def _gb_strip_refs(text: str) -> str:
    return _GUT_BARRIER_REF_MARK.sub("", text or "").strip()


def _gb_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gb_row_value(row: dict[str, Any], column: str, index: int) -> str:
    """二维表的一行：优先按列名取，列名改了措辞时退回按列序取。"""
    if column in row:
        return _gb_text(row.get(column))
    values = list(row.values())
    return _gb_text(values[index]) if len(values) > index else ""


def _gb_mechanism_parts(text: str) -> dict[str, str]:
    """把「机制关联：…\\n本次依据：…\\n随访重点：…」拆成 mechanism / basis / action。"""
    parts: dict[str, str] = {}
    unlabeled: list[str] = []
    for line in (text or "").splitlines():
        line = _gb_strip_refs(line)
        if not line:
            continue
        for label, key in _GUT_BARRIER_MECHANISM_PARTS:
            if line.startswith(label):
                body = line[len(label):].lstrip("：: ").strip()
                if body:
                    parts[key] = f"{parts[key]}\n{body}" if key in parts else body
                break
        else:
            unlabeled.append(line)
    if unlabeled and "mechanism" not in parts:
        parts["mechanism"] = "\n".join(unlabeled)
    return parts


def _gb_risk_priority(risk_level: str) -> str:
    """风险等级原文 -> 复核优先级（只用于排序）。"""
    if any(word in risk_level for word in ("高", "重")):
        return "优先复核"
    if any(word in risk_level for word in ("中", "复核")):
        return "建议复核"
    return "常规关注"


def _gb_system_name(title: str) -> str:
    """「2.1 消化系统疾病风险评估」-> 「消化系统」。"""
    name = re.sub(r"^\s*[\d.]+\s*", "", title or "")
    return name.replace("疾病风险评估", "").replace("风险评估", "").strip() or title


def _gb_system_code(title: str, index: int) -> str:
    for word, code in _GUT_BARRIER_SYSTEM_CODES:
        if word in (title or ""):
            return code
    return f"gut_barrier_system_{index}"


def _gb_evidence(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """参考文献 -> 评估报告 evidence 的字段形状（键与 evaluate 一致，编号为连续整数）。

    补充文献（源报告里带「补」前缀）接在主文献之后编号，用 topic 标注来源。
    """
    return [
        {
            "number": number,
            "evidence_type": "literature",
            "topic": "补充依据" if item.get("supplementary") else "肠道屏障与炎症评估",
            "title": _gb_text(item.get("citation")),
            "source": _gb_text(item.get("publisher")),
            "publication_date": _gb_text(item.get("year")),
            "url": _gb_text(item.get("url")),
            "evidence_level": "",
            "provider": "gut_barrier_report",
            "summary": "",
            "conclusion": "",
            "key_evidence": "",
            "patient_related_facts": [],
            "evidence_grade": "",
            "study_type": "",
            "authors": [],
        }
        for number, item in enumerate(references, start=1)
    ]


# ---- 肠型与性别：PFMT 配型依据 ----
# 肠型写法不统一：「肠型I（拟杆菌型）」「ET-B」「type_1」「拟杆菌型」都可能出现，统一成 I / II / III。
_ENTEROTYPE_NAMES: tuple[tuple[str, str], ...] = (
    ("瘤胃球菌", "III"), ("普雷沃", "II"), ("普氏", "II"), ("拟杆菌", "I"),
)
_ENTEROTYPE_CODE_PATTERN = re.compile(r"(?:肠型|type|ET)[\s_\-]*(III|II|I|Ⅲ|Ⅱ|Ⅰ|3|2|1)(?![a-z0-9])", re.I)
_ENTEROTYPE_CODE_ALIASES = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "1": "I", "2": "II", "3": "III"}
# 配型规则：受检者肠型 -> 首选供体肠型（产品《FMT 配型》文档；肠型 II 还要求避开同肠型）
_PFMT_DONOR_ENTEROTYPE = {"I": "III", "II": "I", "III": "I"}
_GENDER_ALIASES = {"男": "男", "男性": "男", "male": "男", "m": "男", "女": "女", "女性": "女", "female": "女", "f": "女"}


def _enterotype_code(text: Any) -> str:
    """任意肠型写法 -> I / II / III；识别不出返回空串。"""
    text = _gb_text(text)
    if not text:
        return ""
    for name, code in _ENTEROTYPE_NAMES:
        if name in text:
            return code
    match = _ENTEROTYPE_CODE_PATTERN.search(text)
    if match:
        value = match.group(1).upper()
        return _ENTEROTYPE_CODE_ALIASES.get(value, value)
    return ""


def _normalize_gender(value: Any) -> str:
    return _GENDER_ALIASES.get(_gb_text(value).lower(), "")


def _gb_enterotype(part1: dict[str, Any]) -> str:
    """从肠道屏障报告第一章取肠型原文。

    只有用户同时上传了菌群评估报告，报告里才有「辅助资料摘要」表（列名含「肠型」）；
    没上传时返回空串。识别不出肠型的占位文字（如「已纳入来源核对」）不算。
    """
    for block in part1.get("extra_blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "table":
            columns = [column for column in block.get("columns") or [] if "肠型" in column]
            for row in block.get("rows") or []:
                for column in columns:
                    value = _gb_text(row.get(column))
                    if _enterotype_code(value):
                        return value
        elif block.get("kind") in ("facts", "cards"):
            for item in block.get("items") or []:
                value = _gb_text(item.get("value"))
                if "肠型" in _gb_text(item.get("label")) and _enterotype_code(value):
                    return value
    return ""


# ---- gutBarrier：instruction 综合评估 / 健康风险的参考原文 ----
# 块的形态随报告版本变化：paragraph(text) / list(items) / cards(items) / table(rows)，统一拍平成一段文字。
def _gb_join_sentences(parts: list[str]) -> str:
    out = ""
    for part in parts:
        part = _gb_strip_refs(part)
        if not part:
            continue
        if out and not out.endswith(("。", "！", "；", "？")):
            out += "。"
        out += part
    return out


def _gb_block_text(block: dict[str, Any]) -> str:
    kind = block.get("kind")
    if kind == "table":
        columns = [_gb_text(column) for column in block.get("columns") or []]
        parts = []
        for row in block.get("rows") or []:
            cells = row.get("cells") if isinstance(row, dict) and "cells" in row else row
            if isinstance(cells, dict):
                cells = [cells.get(column) for column in columns]
            cells = [_gb_text(cell) for cell in cells or []] if isinstance(cells, list) else []
            # 「评价项目 | 综合结论」这类两列表只取结论列
            value = cells[-1] if len(columns) == 2 and len(cells) >= 2 else "，".join(c for c in cells if c)
            parts.append(value)
        return _gb_join_sentences(parts)
    parts = []
    text = block.get("text")
    for value in (text if isinstance(text, list) else [text]):
        parts.append(_gb_text(value))
    for item in block.get("items") or []:
        if isinstance(item, dict):
            label, value = _gb_text(item.get("label")), _gb_text(item.get("value") or item.get("text"))
            detail = _gb_text(item.get("detail"))
            head = f"{label}：{value}" if label and value else (label or value)
            parts.append("，".join(p for p in (head, detail) if p))
        else:
            parts.append(_gb_text(item))
    return _gb_join_sentences(parts)


def _gb_find_block_text(sections: list[Any], title_pattern: str, *, section_ids: tuple[str, ...],
                        section_title_keyword: str, prefer: str = "") -> str:
    """在指定章节里按标题关键词找块并取正文；prefer 命中的块优先（如「综合评估指数」优先于旧版「综合评估结论」）。"""
    pattern = re.compile(title_pattern)
    candidates = [
        section for section in sections
        if isinstance(section, dict) and (
            _gb_text(section.get("id")) in section_ids or section_title_keyword in _gb_text(section.get("title"))
        )
    ]
    blocks = [
        block for section in candidates for block in section.get("blocks") or []
        if isinstance(block, dict) and pattern.search(_gb_text(block.get("title")))
    ]
    blocks.sort(key=lambda block: 0 if prefer and prefer in _gb_text(block.get("title")) else 1)
    for block in blocks:
        text = _gb_block_text(block)
        if text:
            return text
    return ""


# ---- gutBarrier：instruction 的「个体体质特征」（固定句式，程序拼装）----
#   本次肠道屏障评分IBFC为1分，损伤类型：单纯应激性损伤，损伤分级为I级；炎症指标超标，整体为肠源性慢性低度炎症。
#   结合您的评估问卷结果，存在××慢病风险。
# 损伤类型由 D-乳酸（LaC）/ DAO / LPS 三项是否升高的组合决定（产品规则，见 _GB_DAMAGE_TYPES）。
# 三项状态优先从原报告文字（第二章「本章小结」、1.1 综合评估指数）分析，文字没提到的再看 1.2 指标表。
_GB_GRADE_CODE = re.compile(r"(IV|V|III|II|I|Ⅳ|Ⅴ|Ⅲ|Ⅱ|Ⅰ)\s*级")
_GB_TEXT_GRADE = re.compile(r"损伤(?:等级|分级)\s*(?:为|：|:)?\s*(IV|V|III|II|I|Ⅳ|Ⅴ|Ⅲ|Ⅱ|Ⅰ)\s*级")
_GB_TEXT_SCORE = re.compile(r"IBFC[）)]?\s*(?:评分)?\s*(?:为|：|:)?\s*(\d+(?:\.\d+)?)\s*分")
_GB_TEXT_DAMAGE_TYPE = re.compile(r"损伤类型\s*(?:为|：|:)\s*([^。；;\n，,]+)")
_GB_ROMAN_ALIASES = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V"}
_GB_RISK_LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}

# 损伤类型：(LaC 升高, DAO 升高, LPS 升高) -> 名称；三项均未升高时不写损伤类型
_GB_DAMAGE_TYPES: dict[tuple[bool, bool, bool], str] = {
    (True, True, True): "应激+炎症(最重)",
    (True, False, True): "炎症性损伤(伴移位)",
    (False, True, True): "应激性损伤(伴移位)",
    (False, False, True): "非肠源性移位",
    (True, False, False): "单纯炎症性损伤",
    (False, True, False): "单纯应激性损伤",
    (True, True, False): "炎症+应激(无移位)",
}
# 三项指标在文字里的写法
_GB_MARKER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LaC", ("D-乳酸", "D乳酸", "D-Lactate", "D-LA", "LaC", "Lac")),
    ("DAO", ("二胺氧化酶", "DAO")),
    ("LPS", ("细菌脂多糖", "脂多糖", "LPS")),
)
_GB_MARKER_NOT_UP = re.compile(r"未同步升高|未升高|未见升高|未见异常|无异常|不高|正常")
_GB_MARKER_UP = re.compile(r"升高|上升|增高|偏高|异常")


def _gb_marker_status_from_text(texts: list[str]) -> dict[str, bool]:
    """按分句判断三项是否升高：分句里出现指标名 + 否定词记为未升高，出现指标名 + 升高/异常记为升高。
    靠前的文字（本章小结）优先，已判定的指标不被后面的文字覆盖。"""
    status: dict[str, bool] = {}
    for text in texts:
        for clause in re.split(r"[，,。；;\n]", text or ""):
            for code, aliases in _GB_MARKER_ALIASES:
                if code in status or not any(alias.lower() in clause.lower() for alias in aliases):
                    continue
                if _GB_MARKER_NOT_UP.search(clause):
                    status[code] = False
                elif _GB_MARKER_UP.search(clause):
                    status[code] = True
    return status


def _gb_marker_status_from_table(sections: list[Any]) -> dict[str, bool]:
    """1.2 检测项目与结果总览：「结果评价」含升高 / 异常记为升高，其余记为未升高。"""
    status: dict[str, bool] = {}
    for section in sections:
        for block in (section.get("blocks") or []) if isinstance(section, dict) else []:
            if not isinstance(block, dict) or block.get("kind") != "table":
                continue
            columns = [_gb_text(column) for column in block.get("columns") or []]
            if "检测指标" not in columns or "结果评价" not in columns:
                continue
            name_at, eval_at = columns.index("检测指标"), columns.index("结果评价")
            for row in block.get("rows") or []:
                cells = row.get("cells") if isinstance(row, dict) else row
                if not isinstance(cells, list) or len(cells) <= max(name_at, eval_at):
                    continue
                name, evaluation = _gb_text(cells[name_at]), _gb_text(cells[eval_at])
                for code, aliases in _GB_MARKER_ALIASES:
                    if code not in status and any(alias.lower() in name.lower() for alias in aliases):
                        status[code] = bool(_GB_MARKER_UP.search(evaluation)) and not _GB_MARKER_NOT_UP.search(evaluation)
    return status


def _gb_damage_type(texts: list[str], sections: list[Any]) -> str:
    """损伤类型：报告文字里明确写了规则表中的类型就用它；否则按三项升高组合查表。"""
    labels = {label.replace("（", "(").replace("）", ")"): label for label in _GB_DAMAGE_TYPES.values()}
    for text in texts:
        match = _GB_TEXT_DAMAGE_TYPE.search(text or "")
        if match:
            stated = match.group(1).strip().replace("（", "(").replace("）", ")")
            if stated in labels:
                return labels[stated]
    status = {**_gb_marker_status_from_table(sections), **_gb_marker_status_from_text(texts)}
    return _GB_DAMAGE_TYPES.get((status.get("LaC", False), status.get("DAO", False), status.get("LPS", False)), "")


# IBFC fallback follows the main report: each upper boundary belongs to the previous grade.
_GB_GRADE_BOUNDS = ((2.0, "I"), (4.5, "II"), (6.0, "III"), (8.0, "IV"))


def _gb_grade_from_score(score: str) -> str:
    value = _gb_float(score)
    if value is None or value < 0:
        return ""
    if value == 0:
        return "normal"
    return next((grade for bound, grade in _GB_GRADE_BOUNDS if value <= bound), "V")


def _gb_format_score(value: str) -> str:
    number = _gb_float(value)
    if number is None:
        return ""
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _gb_barrier_facts(part1: dict[str, Any], sections: list[Any], texts: list[str]) -> dict[str, Any]:
    """个体体质特征 / 健康风险结尾共用的屏障事实：IBFC 分数、损伤分级、损伤类型。"""
    ibfc = part1.get("ibfc") or {}
    score = _gb_format_score(_gb_text(ibfc.get("score")))
    if not score:
        score = next((_gb_format_score(m.group(1)) for t in texts if (m := _GB_TEXT_SCORE.search(t or ""))), "")
    # 损伤分级：卡片 -> 报告文字 -> 按 IBFC 分值区间推出
    grade_text = _gb_text(ibfc.get("grade"))
    match = _GB_GRADE_CODE.search(grade_text) or next(
        (m for t in texts if (m := _GB_TEXT_GRADE.search(t or ""))), None)
    if match:
        grade = _GB_ROMAN_ALIASES.get(match.group(1), match.group(1))
    elif grade_text in ("normal", "未见评分异常"):
        grade = "normal"
    else:
        grade = _gb_grade_from_score(score)
    return {
        "score": score,
        "grade": grade,
        "damage_type": "" if grade == "normal" else _gb_damage_type(texts, sections),
    }


# 本章小结文字里的慢病风险写法：炎症性肠病（1.8分，低风险）
_GB_TEXT_RISK_ITEM = re.compile(r"([^、，,；;：:。（(\s]+)[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[，,、]\s*(高|中|低)风险\s*[）)]")


def _gb_risk_diseases(sections: list[Any], summary_text: str = "") -> list[tuple[str, int]]:
    """第二章疾病风险表，按风险从高到低（同级按风险指数降序）返回 (疾病, 等级序号)，不含「××复核」项。
    报告没有疾病风险表时，退回解析本章小结文字里的「疾病（x分，x风险）」。具体取几个见 _pick_diseases。"""
    found: dict[str, tuple[int, float, int]] = {}
    order = 0
    has_table = False
    for section in sections:
        if not isinstance(section, dict) or not (
            _gb_text(section.get("id")) == "disease_risks" or "疾病风险" in _gb_text(section.get("title"))
        ):
            continue
        for block in section.get("blocks") or []:
            if not isinstance(block, dict) or block.get("kind") != "table":
                continue
            columns = [_gb_text(column) for column in block.get("columns") or []]
            if "疾病" not in columns or "风险等级" not in columns:
                continue
            has_table = True
            name_at, level_at = columns.index("疾病"), columns.index("风险等级")
            index_at = columns.index("风险指数") if "风险指数" in columns else None
            for row in block.get("rows") or []:
                cells = row.get("cells") if isinstance(row, dict) else row
                if not isinstance(cells, list) or len(cells) <= max(name_at, level_at):
                    continue
                name, level = _gb_text(cells[name_at]), _gb_text(cells[level_at])
                if not name or level not in _GB_RISK_LEVEL_ORDER or "复核" in name or name in found:
                    continue
                risk_index = _gb_float(_gb_text(cells[index_at])) if index_at is not None and index_at < len(cells) else None
                found[name] = (_GB_RISK_LEVEL_ORDER[level], -(risk_index or 0.0), order)
                order += 1
    if not has_table or not found:
        for name, risk_index, level in _GB_TEXT_RISK_ITEM.findall(summary_text or ""):
            name = re.split(r"包括|包含", name)[-1].strip()  # 「需重点留意的慢病风险包括炎症性肠病」-> 炎症性肠病
            if name and level in _GB_RISK_LEVEL_ORDER and "复核" not in name and name not in found:
                found[name] = (_GB_RISK_LEVEL_ORDER[level], -float(risk_index), order)
                order += 1
    return [(name, found[name][0]) for name in sorted(found, key=found.get)]


def _gb_constitution(facts: dict[str, Any], diseases: list[tuple[str, int]]) -> str:
    """由屏障评分、分型和报告风险条目拼装体质描述。"""
    parts = [f"本次肠道屏障评分IBFC为{facts['score']}分"] if facts["score"] else []
    if facts["damage_type"]:
        parts.append(f"损伤类型：{facts['damage_type']}")
    if facts["grade"] == "normal":
        parts.append("肠道屏障未见损伤")
    elif facts["grade"]:
        parts.append(f"损伤分级为{facts['grade']}级")
    first = "，".join(parts)
    return _constitution_text(first, diseases)


def _gb_risk_tail(facts: dict[str, Any], sections: list[Any], chapter_summary: str = "") -> str:
    if facts["grade"] not in ("", "normal") or facts["damage_type"]:
        mechanism = _RISK_MECHANISM_BARRIER
    else:
        mechanism = ""
    return _risk_tail(mechanism, _gb_risk_diseases(sections, chapter_summary))


def _gb_instruction_summary(data: dict[str, Any]) -> dict[str, str]:
    data = without_crp_content(data)
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    part1 = build_gut_barrier_contexts(data)["part1"]
    sections = report.get("sections") if isinstance(report.get("sections"), list) else []
    # 第一章「1.1 综合评估指数（IBFC…）」；旧版为「1.1 检测综合评估结论」表
    overall = _gb_find_block_text(
        sections, r"综合评估", section_ids=("overview",), section_title_keyword="第一章", prefer="综合评估指数")
    # 第二章「2.x 本章小结」（编号会变，按标题关键词找）
    chapter_summary = _gb_find_block_text(
        sections, r"本章小结", section_ids=("disease_risks",), section_title_keyword="疾病风险")
    facts = _gb_barrier_facts(part1, sections, [chapter_summary, overall])
    return {
        "body_mass": overall,
        "risk_warning": chapter_summary,
        # 个体体质特征：IBFC 分数 + 损伤类型 + 损伤分级（固定句式）
        "constitution": _gb_constitution(facts, _gb_risk_diseases(sections, chapter_summary)),
        # 健康风险提示的固定结尾：机制说明 + 最严重的 2–3 个慢病风险
        "risk_tail": _gb_risk_tail(facts, sections, chapter_summary),
    }


def _gut_barrier_followup_input(data: dict[str, Any]) -> dict[str, Any]:
    """把 gutBarrier 源报告整理成 opinion / instruction 可直接消费的评估报告字段结构。"""
    data = without_crp_content(data) if isinstance(data, dict) else {}
    contexts = build_gut_barrier_contexts(data)
    meta, part1, part2 = contexts["meta"], contexts["part1"], contexts["part2"]
    ibfc = part1["ibfc"]
    test_date = meta.get("sample_date") or meta.get("report_date") or ""

    # 1.1 综合评估结论：评价项目 -> 综合结论
    summary_rows: dict[str, str] = {}
    for row in part1["summary_table"].get("rows") or []:
        label = _gb_row_value(row, "评价项目", 0)
        if label:
            summary_rows[label] = _gb_strip_refs(_gb_row_value(row, "综合结论", 1))
    conclusion = summary_rows.get("综合评价") or "\n".join(
        f"{label}：{text}" for label, text in summary_rows.items() if text
    )
    data_gaps = [gap for gap in part1.get("data_gaps") or [] if gap]
    enterotype = _gb_enterotype(part1)

    # 评估报告的 clinical_context 是「小标题 -> 一段话」，这里按同样形状给出综述
    clinical_context = {
        key: value
        for key, value in (
            ("综合评价", conclusion),
            ("肠道屏障功能", summary_rows.get("肠道屏障功能") or _gb_strip_refs(ibfc.get("paragraph", ""))),
            ("受损类型", _gb_strip_refs(part1.get("damage_paragraph", ""))),
            ("干预方向", summary_rows.get("干预方向", "")),
            ("资料缺口", "；".join(data_gaps)),
            # 随附菌群评估报告时才有；_info 从这里提取 intestine_type，_recommend 据此配型
            ("肠型", enterotype),
        )
        if value
    }

    # 1.2 本次三项屏障检测指标（含正常项），逐字搬运。
    barrier_used: list[dict[str, Any]] = []
    for index, row in enumerate(part1["indicator_table"].get("rows") or [], start=1):
        name = _gb_row_value(row, "检测指标", 0)
        item = {
            "indicatorCode": f"gut_barrier:indicator-{index}",
            "item": name,
            "value": _gb_row_value(row, "检测结果", 1),
            "unit": None,
            "date": test_date,
            "source": meta.get("report_title", ""),
            "sourceType": "gut_barrier_test",
            "status": _gb_row_value(row, "结果评价", 3),
            "referenceRange": _gb_text(row.get("参考区间", row.get("参考范围", ""))),
        }
        barrier_used.append(item)

    findings: list[dict[str, Any]] = []
    abnormal_indicators: list[dict[str, Any]] = []
    examinations: list[dict[str, Any]] = []

    def add_finding(item_id: str, problem: str, priority: str, indicators: list[dict[str, Any]],
                    mechanism: str, recommendations: list[str]) -> None:
        findings.append({
            "finding_type": "disease",
            "item_id": item_id,
            "problem": problem,
            "priority": priority,
            "indicators": indicators,
            "biological_mechanism": mechanism,
            "recommendations": [text for text in recommendations if text],
            "tips": {"special_reminder": None, "practical_goal": None},
            "evidence_refs": [],
        })

    # 1.3 IBFC 评分 > 0 才构成屏障问题；0 分 / 未见评分异常不硬造问题
    ibfc_score = _gb_float(ibfc.get("score", ""))
    if ibfc_score is not None and ibfc_score > 0:
        grade = ibfc.get("grade", "")
        add_finding(
            "gb-ibfc",
            f"肠道屏障功能{grade}" if grade else "肠道屏障功能受损",
            "优先复核" if ibfc_score > 4.5 else "建议复核",
            [{"name": "肠道屏障功能评分（IBFC）", "value": f"{ibfc['score']}分",
              "status": grade or ibfc.get("selected_band", "")}],
            _gb_strip_refs(part1.get("damage_paragraph", "")),
            [],
        )

    # 超出参考范围的检测项；区间型结果另记为待定量复测
    for index, (item, pending) in enumerate(
        [(row, False) for row in part1["abnormal_indicators"]]
        + [(row, True) for row in part1["pending_indicators"]],
        start=1,
    ):
        name, result = item.get("name", ""), item.get("result", "")
        evaluation, reference = item.get("evaluation", ""), item.get("reference", "")
        significance = ""
        add_finding(
            f"gb-abnormal-{index}",
            f"{name}{evaluation}" if evaluation else name,
            "建议复核",
            [{"name": name, "value": result, "status": evaluation}],
            significance,
            [],
        )
        abnormal_indicators.append({
            "dimensionCode": "gut_barrier",
            "dimensionName": "肠道屏障功能",
            "indicatorCode": f"gut_barrier:abnormal-{index}",
            "item": name,
            "value": result,
            "unit": None,
            "referenceRange": reference,
            "status": evaluation,
            "severity": "attention",
            "date": test_date,
            "clinicalSignificance": significance,
        })
        if pending:
            reason = f"本次为区间型结果（{result}），需补充定量结果后才能精确分层"
            examinations.append({
                "name": f"{name}定量复测",
                "purpose": "获得定量结果后再进行精确分层",
                "reason": f"{reason}；{evaluation}" if evaluation else reason,
                "evidence_refs": [],
            })

    # 第二章：已做出分层的疾病风险条目；未评估的行不当成问题
    dimensions: list[dict[str, Any]] = []
    risk_no = 0
    for system_index, system in enumerate(part2.get("systems") or [], start=1):
        related, levels = [], []
        for row in system.get("rows") or []:
            disease, risk_level = row.get("disease", ""), row.get("risk_level", "")
            if not disease:
                continue
            related.append({"name": disease, "riskLabel": risk_level})
            if not risk_level or any(label in risk_level for label in GUT_BARRIER_UNASSESSED_LABELS):
                continue
            levels.append(risk_level)
            risk_no += 1
            parts = _gb_mechanism_parts(row.get("mechanism", ""))
            indicators = [{"name": "风险等级", "value": risk_level, "status": risk_level}]
            if row.get("risk_index"):
                indicators.insert(0, {"name": "风险指数", "value": row["risk_index"], "status": risk_level})
            mechanism = "\n".join(text for text in (parts.get("mechanism"), parts.get("basis")) if text)
            add_finding(
                f"gb-risk-{risk_no}",
                disease,
                _gb_risk_priority(risk_level),
                indicators,
                mechanism,
                [parts.get("action", "")],
            )
        dimensions.append({
            "code": _gb_system_code(system.get("title", ""), system_index),
            "name": _gb_system_name(system.get("title", "")),
            "score": None,
            "scoreAvailable": False,
            "displayScore": "",
            # 同一系统多行时取最需要关注的那一档，原文照搬
            "levelLabel": min(
                levels, key=lambda level: _GUT_BARRIER_PRIORITIES.index(_gb_risk_priority(level)),
            ) if levels else "",
            "relatedDiseases": related,
            "evidenceUsed": [],
        })
    findings.sort(key=lambda finding: _GUT_BARRIER_PRIORITIES.index(finding["priority"]))

    dimensions[:0] = [
        {
            "code": "gut_barrier",
            "name": "肠道屏障功能",
            "score": None,
            "scoreAvailable": False,
            "displayScore": "",
            "levelLabel": ibfc.get("grade", ""),
            "explanation": _gb_strip_refs(ibfc.get("paragraph", "")),
            "relatedDiseases": [],
            "evidenceUsed": barrier_used,
        },
    ]

    health_recommendations = [
        {
            "category": "general",
            "topic": point.split("：", 1)[0] if "：" in point else "随访关注",
            "priority": "常规关注",
            "content": _gb_strip_refs(point),
            "evidence_refs": [],
        }
        for point in part2.get("summary_points") or [] if point
    ]

    return {
        # 保留源类型：日志排查时能分清来源，也让依赖来源的逻辑（如菌群解读）走对分支
        "type": SOURCE_TYPE_GUT_BARRIER,
        "userId": data.get("userId"),
        "sessionId": data.get("sessionId"),
        "finalReportId": data.get("finalReportId"),
        "status": data.get("status"),
        "generated_at": data.get("generated_at") or data.get("generatedAt"),
        "subject": {
            "name": meta.get("name", ""),
            "gender": meta.get("gender", ""),
            "age": meta.get("age", ""),
            "report_date": meta.get("report_date", ""),
        },
        "sample": {
            "sample_id": meta.get("report_id", ""),
            "sample_type": meta.get("sample_type", ""),
            "test_item": meta.get("test_item", "") or meta.get("report_title", ""),
            "sample_date": meta.get("sample_date", ""),
            "report_date": meta.get("report_date", ""),
        },
        "healthAssessment": {
            "name": meta.get("report_title", ""),
            "overallScore": None,
            "overallScoreAvailable": False,
            "displayOverallScore": "",
            "levelLabel": ibfc.get("grade", ""),
            "summary": conclusion,
            "dimensions": dimensions,
            "abnormalIndicators": abnormal_indicators,
        },
        "diagnosis_summary": {
            "conclusion": conclusion,
            "clinical_context": clinical_context,
            "risk": None,
            "evidence_refs": [],
        },
        "diagnostic_basis": {
            "abnormal_findings": findings,
            "evidence_refs": [],
        },
        "diagnostic_advice": {
            "report_interpretation": [],
            "Plan": {"未来1-2周": None, "未来1个月": None, "未来2-3个月": None, "未来6-12个月": None},
            "health_recommendations": health_recommendations,
            "recommended_examinations": examinations,
            "not_recommended_behaviors": {},
            "medical_alerts": [],
        },
        # 评估报告里没有这两项，只作为模型可读的背景素材，不进任何对外报文
        "clinical_background": part2.get("clinical_background"),
        "questionnaire_highlights": part1.get("questionnaire_highlights") or [],
        "evidence": _gb_evidence(contexts.get("references") or []),
        "partial_reasons": data_gaps,
        # PFMT 配型依据（只作输入，不进任何对外报文）；没有随附菌群评估报告时为空串
        "enterotype": enterotype,
        # instruction 综合评估 / 健康风险的参考原文（只作输入，见 _instruction_summary_sources）
        "instruction_summary": _gb_instruction_summary(data),
    }


# ================= 报告解读 报告（解读参考手册） ====================
async def explain_report(data, user_id, session_id):
    """产出《用户报告解读参考手册》。

    面向解读人（无医学背景的助理 / 业务员），不是给用户看的科普：
    正文是可照读的话术，配套速查卡、答疑与禁忌。
    """
    contexts = build_explain_contexts(data)
    # issues 记录程序对模型输出做过的删改，最终写进手册末尾的自检清单；
    # allowed 是上下文里出现过的全部数字，用于话术数值防篡改。
    issues: list[str] = []
    allowed = explain_allowed_numbers(contexts)
    results = await _gather_followup_components(
        *(_manual_component(
            f"explain_part{part_no}",
            lambda corrections, part_no=part_no: _explain_section(part_no, contexts, issues=corrections, allowed=allowed),
            default_explain_section(part_no, contexts),
            issues=issues,
        ) for part_no in (1, 2, 3, 4)),
        _manual_component(
            "explain_opening",
            lambda corrections: _explain_opening(contexts, issues=corrections, allowed=allowed),
            default_explain_opening(contexts),
            issues=issues,
        )
    )
    return assemble_explain_manual(
        contexts=contexts,
        sections=results[:4],
        opening=results[4],
        user_id=user_id,
        session_id=session_id,
        issues=issues,
    )


# ================= 诊断意见报告 ====================
async def opinion_report(data, user_id, session_id):
    subject, sample = data.get("subject") or {}, data.get("sample") or {}
    diag_summary, diag_basis = await _gather_followup_components(
        _followup_component("opinion_summary", lambda: opinion_summary(data), FOLLOWUP_DEFAULTS["opinion_summary"]),
        _followup_component("opinion_basis", lambda: opinion_basis(data), FOLLOWUP_DEFAULTS["opinion_basis"]),
    )
    diag_advice = await _followup_component("opinion_advice", lambda: opinion_advice({"diagnostic_basis": diag_basis}, data), FOLLOWUP_DEFAULTS["opinion_advice"])
    unified_evidence = extract_unified_evidence([diag_summary, diag_basis, diag_advice], data)
    return {
        "type": "opinion",
        "userId": user_id,
        "sessionId": session_id,
        "subject": {
            "name": subject.get("name"),
            "gender": subject.get("gender"),
            "age": subject.get("age"),
            "report_date": sample.get("report_date")
        },
        "diagnosis_summary": diag_summary,
        "diagnostic_basis": diag_basis,
        "diagnostic_advice": diag_advice,
        "evidence": unified_evidence,
    }





# ================= 同事的底层高可用引擎 (无损接入) =================
async def _retry_report_call(factory: Callable[[], Awaitable[Any]], *, policy: ReportFallbackPolicy,
                             unit: str = "follow-up") -> tuple[Any, str]:
    """重试一个生成单元。``unit`` 必须传，否则 15 个子任务共用一行降级日志，出事查不出是谁。"""
    attempts = policy.attempts_for(policy.llm_attempts)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            result = await factory()
            if not isinstance(result, (dict, list)) or not result:
                raise ValueError("empty or invalid model result")
            return result, "success" if attempt == 0 else "retried_success"
        except Exception as exc:
            last_error = exc
            logger.warning(
                "follow-up unit %s attempt %d/%d failed: %r",
                unit, attempt + 1, attempts, exc,
            )
            if attempt + 1 < attempts:
                delay = policy.backoff_after(attempt)
                if delay: await asyncio.sleep(delay)
    logger.warning("follow-up unit %s degraded to empty: %r", unit, last_error)
    return {}, "fallback_empty"


async def _generate_followup_module(report_type: str, factory: Callable[[], Awaitable[dict[str, Any]]], *,
                                    data: dict[str, Any], user_id: str, session_id: str,
                                    policy: ReportFallbackPolicy) -> tuple[dict[str, Any], str]:
    async def generate_validated() -> dict[str, Any]:
        gaps = _ACTIVE_FOLLOWUP_GAPS.get()
        if gaps is not None:
            gaps.clear()  # Failed earlier attempts cannot taint a complete retry.
        payload = await factory()
        missing = _missing_followup_fields(report_type, payload)
        if missing:
            raise ValueError(
                f"incomplete {report_type} report, empty fields: {', '.join(missing)}"
            )
        return payload

    gaps: set[str] = set()
    token = _ACTIVE_FOLLOWUP_GAPS.set(gaps)
    policy_token = _ACTIVE_FOLLOWUP_COMPONENT_POLICY.set(policy)
    cache_token = _ACTIVE_FOLLOWUP_COMPONENT_CACHE.set({})
    try:
        payload, status = await _retry_report_call(generate_validated, policy=policy, unit=report_type)
    finally:
        _ACTIVE_FOLLOWUP_COMPONENT_CACHE.reset(cache_token)
        _ACTIVE_FOLLOWUP_COMPONENT_POLICY.reset(policy_token)
        _ACTIVE_FOLLOWUP_GAPS.reset(token)
    projected_seven = data.get('_seven_chapter_projection') is True
    if projected_seven:
        from .evaluation_report_followups import empty_projected_followup
        neutral = empty_projected_followup(report_type, user_id=user_id, session_id=session_id,
            subject=deepcopy(data.get('subject')))
    else:
        neutral = default_followup_report(report_type, evaluate=data, user_id=user_id, session_id=session_id)
    if not projected_seven and payload and all(payload.get(key) == neutral.get(key) for key in _FOLLOWUP_REQUIRED_FIELDS[report_type]):
        status = "fallback_empty"
    if not payload:
        # Keep the declared shape, without inventing clinical findings.
        payload = neutral
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        payload = without_crp_content(payload)
    payload = await _archive_finished_followup(payload, status, data=data, partial=bool(gaps))
    return payload, status


async def _archive_finished_followup(payload, status, *, data, partial=False):
    """Freeze and persist each result before waiting for any sibling report."""
    if 'groupId' in data:
        payload = {**payload, 'groupId': data['groupId']}
    payload = followup_terminal_payload(payload, status,
        partial=partial or data.get("status") == "partial_success")
    payload = _stamp_final_report_id((payload,))[0]
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        # gutBarrier 的 opinion / instruction 与 evaluate 同名，存到子命名空间，
        # 与最终发布时的保存位置保持一致（见 _save_followup_artifacts）
        await _save_followup_artifacts(
            (payload,), {payload["type"]: status},
            namespaced_types=GUT_BARRIER_NAMESPACED_TYPES, namespace=GUT_BARRIER_ARTIFACT_NAMESPACE,
        )
    else:
        await _save_followup_artifacts((payload,), {payload["type"]: status})
    return payload


_FOLLOWUP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    # "intervene": ("recommend", "diet", "supplements", "behavior", "exercise"),
    "instruction": ("info", "recommend", "usage", "notes", "non_drug_intervention", "recommended_tests"),
    "opinion": ("diagnosis_summary", "diagnostic_basis", "diagnostic_advice"),
    # 解读手册：前置（速查卡+开场白）/ 四组五件套 / 后置（收尾+禁用语+自检）
    "explain": ("preface", "sections", "closing"),
    # 肠道与炎症报告解读：同一套手册骨架，主体是两章五件套
    GUT_BARRIER_REPORT_TYPE: ("preface", "sections", "closing"),
}


def _missing_followup_fields(report_type: str, payload: Any) -> tuple[str, ...]:
    """返回为空的必填字段名，用于重试日志；空元组表示这份报告是完整的。"""
    if not isinstance(payload, dict):
        return ("<not-a-dict>",)
    if payload.get("type") != report_type:
        return ("type",)
    required = _FOLLOWUP_REQUIRED_FIELDS.get(report_type)
    if not required:
        return ("<unknown-report-type>",)
    return tuple(key for key in required
                 if key not in payload or not isinstance(payload[key], list if key == "sections" else dict)
                 or not payload[key])


def _followup_payload_is_complete(report_type: str, payload: dict[str, Any]) -> bool:
    return not _missing_followup_fields(report_type, payload)


async def send_report_callback(result: dict[str, Any], report_name: str, *,
                               policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY) -> str:
    """分发重试逻辑（使用 Gateway，不直接使用 aiohttp）"""
    attempts = policy.attempts_for(policy.callback_attempts)
    last_error = None
    for attempt in range(attempts):
        try:
            status = await _active_gateway().deliver_json(result, timeout_seconds=300.0)
            logger.info("%s callback delivered: status=%s", report_name, status)
            return "success" if attempt == 0 else "retried_success"
        except FollowupGatewayError as exc:
            last_error, retryable = exc, exc.retryable
        except Exception as exc:
            last_error, retryable = exc, False
        if not retryable or attempt + 1 >= attempts: break
        delay = policy.backoff_after(attempt)
        if delay: await asyncio.sleep(delay)
    logger.warning("%s callback failed: %s", report_name, last_error)
    return "delivery_failed"


async def replay_followup_reports(payloads, *, policy=DEFAULT_REPORT_FALLBACK_POLICY, gateway=None):
    if callbacks_are_deferred():
        return {str(payload['type']): 'pending' for payload in payloads}
    token = _ACTIVE_FOLLOWUP_GATEWAY.set(gateway) if gateway is not None else None
    try:
        results = await asyncio.gather(
            *(send_report_callback(payload, str(payload.get("type") or "follow-up"), policy=policy) for payload in
              payloads))
        return {str(payload.get("type") or index): status for index, (payload, status) in
                enumerate(zip(payloads, results))}
    finally:
        if token is not None: _ACTIVE_FOLLOWUP_GATEWAY.reset(token)




def _stamp_final_report_id(payloads):
    """给三类后续报告打上批次引用；未绑定批次时保持原样。"""
    final_report_id = active_final_report_id()
    if not final_report_id:
        return tuple(payloads)
    return tuple({**payload, "finalReportId": final_report_id} for payload in payloads)


async def _save_followup_artifacts(payloads, generation_statuses, *, namespaced_types=(), namespace=""):
    """先保存再发布；保存失败的产物不发送成功回调（文档 §8.2）。

    返回允许投递的 payload 元组。保存失败只影响该产物，其余四类独立处理，
    也不会重跑解析或模型。

    ``namespaced_types`` 里的产物改存到 ``<finalReportId>:<namespace>`` 下：
    evaluate 与 gutBarrier 可能共用同一个 finalReportId，两边都会生成
    opinion / instruction，不分开存会互相判冲突、后到的一份不发回调。
    """
    deliverable = []
    for payload in payloads:
        report_type = str(payload.get("type") or "")
        status = generation_statuses.get(report_type, "success")
        token = bind_artifact_namespace(namespace) if report_type in namespaced_types else None
        try:
            result = await save_report_artifact(
                report_type,
                payload,
                generation_status=payload.get('status', status),
            )
        finally:
            reset_report_batch(token)
        if is_deliverable(result):
            deliverable.append(payload)
        else:
            logger.warning(
                "%s artifact not stored (%s); success callback withheld",
                report_type or "follow-up",
                result,
            )
    return tuple(deliverable)


async def _push_report(data: dict[str, Any], user_id: str, session_id: str, *,
                       policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
                       gateway: FollowupReportGateway | None = None,
                       model_gateway: FollowupModelGateway | None = None) -> FollowupExecutionResult:
    token = _ACTIVE_FOLLOWUP_GATEWAY.set(gateway) if gateway is not None else None
    model_token = _ACTIVE_FOLLOWUP_MODEL.set(model_gateway) if model_gateway is not None else None
    try:
        return await _push_report_with_active_gateway(data, user_id, session_id, policy=policy)
    finally:
        if model_token is not None:
            _ACTIVE_FOLLOWUP_MODEL.reset(model_token)
        if token is not None: _ACTIVE_FOLLOWUP_GATEWAY.reset(token)


async def _push_report_with_active_gateway(data: dict[str, Any], user_id: str, session_id: str, *,
                                           policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY) -> FollowupExecutionResult:
    tasks = []

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    # 给所有子任务钉死事实边界，防止模型顺着菌群导向编结论：
    # evaluate 无菌群依据时追加「无菌群结果」边界，gutBarrier 固定追加屏障报告边界。
    # ContextVar 在 create_task 时被子任务继承，所以必须先于建任务设置；
    # info / recommend 在本协程里直接 await，同样读得到。
    # set/reset 放在这一层，任务被取消时也不会漏掉 reset。
    from .evaluation_report_followups import is_seven_chapter_followup
    seven_chapter = _source_type(data) != SOURCE_TYPE_GUT_BARRIER and is_seven_chapter_followup(data)
    scope_note = '' if seven_chapter else _prompt_scope_note(data)
    if scope_note and _source_type(data) != SOURCE_TYPE_GUT_BARRIER:
        logger.warning(
            "follow-up inputs contain no microbiome facts; "
            "anti-fabrication scope note injected into all sub-task prompts",
        )
    scope_token = _ACTIVE_SCOPE_NOTE.set(scope_note)
    try:
        return await _execute_followup_modules(data, user_id, session_id, policy=policy, spawn=spawn)
    finally:
        # Opinion/explain can already be running while the owner awaits catalog
        # or recommendations. Lease loss must also stop those child tasks.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _ACTIVE_SCOPE_NOTE.reset(scope_token)


async def _spawn_instruction_chain(data: dict[str, Any], user_id: str, session_id: str, *,
                                   policy: ReportFallbackPolicy, spawn: Callable) -> tuple[asyncio.Task, dict[str, str]]:
    """临床医嘱的依赖链：info 与产品目录并发 → recommend → instruction。

    evaluate 与 gutBarrier 共用这一条链（gutBarrier 传入转换后的评估报告字段结构）。
    返回尚未 await 的 instruction 任务与依赖状态，由调用方与其他报告一起汇总。
    """
    info_task = spawn(
        _retry_report_call(lambda: _info(data), policy=policy, unit="info")
    )
    try:
        catalog = await fetch_catalog(policy)
        catalog_status = "success"
    except Exception as exc:
        logger.warning("optional product catalog unavailable: %s", exc)
        catalog = ("", [], [], {}, {}, {}, [])
        catalog_status = "fallback_empty"
    info, info_status = await info_task

    recommend = {}
    recommend_status = "fallback_empty"
    if info:
        recommend, recommend_status = await _retry_report_call(
            lambda: _recommend(data, *catalog[:6], info=info, pfmt_candidates=catalog[6]),
            policy=policy, unit="recommend",
        )

    # 依赖 resolve 后触发后续报告
    if recommend:
        # intervene_task = spawn(_generate_followup_module("intervene", lambda: intervene_report(data, recommend, user_id, session_id), data=data, user_id=user_id, session_id=session_id, policy=policy))
        instruction_task = spawn(_generate_followup_module("instruction", lambda: instruction_report(data, info, recommend, user_id, session_id), data=data, user_id=user_id, session_id=session_id, policy=policy))
    else:
        # intervene_task = spawn(_fallback_followup_module("intervene", data=data, user_id=user_id, session_id=session_id))
        instruction_task = spawn(_fallback_followup_module("instruction", data=data, user_id=user_id, session_id=session_id))

    dependencies = {
        "info": info_status,
        "catalog": catalog_status,
        "recommend": recommend_status,
    }
    return instruction_task, dependencies


async def _publish_followups(payloads, generation_statuses, dependencies, *,
                             policy: ReportFallbackPolicy, completed_log: str,
                             namespaced_types=(), namespace="") -> FollowupExecutionResult:
    """先幂等保存产物，再对保存成功的产物执行回调，最后汇总各模块状态。"""
    if namespaced_types:
        deliverable = await _save_followup_artifacts(
            payloads, generation_statuses, namespaced_types=namespaced_types, namespace=namespace,
        )
    else:
        # 不需要子命名空间时保持原调用形状，替换了本函数的测试桩照常可用
        deliverable = await _save_followup_artifacts(payloads, generation_statuses)
    delivery_statuses = await replay_followup_reports(deliverable, policy=policy)
    module_statuses = {
        report_type: {
            "generation": generation_statuses[report_type],
            "delivery": delivery_statuses.get(report_type, "delivery_failed")
        } for report_type in generation_statuses
    }
    module_statuses["dependencies"] = dependencies
    # 日志模板沿用原来的固定文案（便于按模板检索日志）
    logger.info(completed_log, module_statuses)
    return FollowupExecutionResult(payloads=payloads, module_statuses=module_statuses)


# gutBarrier 生成的 opinion / instruction 与 evaluate 同名，存储时放到这个子命名空间下
GUT_BARRIER_ARTIFACT_NAMESPACE = SOURCE_TYPE_GUT_BARRIER
GUT_BARRIER_NAMESPACED_TYPES = ("opinion", "instruction")


async def _execute_gut_barrier_module(data: dict[str, Any], user_id: str, session_id: str, *,
                                      policy: ReportFallbackPolicy, spawn: Callable) -> FollowupExecutionResult:
    """type=gutBarrier：生成 explain_gutBarrier / opinion / instruction 三份。

    - explain_gutBarrier 直接读源报告；
    - opinion / instruction 读转换后的评估报告字段结构（_gut_barrier_followup_input），
      生成、兜底与组装沿用 evaluate 的同一套代码，对外报文结构不变。
    """
    logger.info("starting gut barrier interpretation: %s", _source_fact_summary(data))
    followup_input = _gut_barrier_followup_input(data)

    # 无依赖项，立即执行
    gut_barrier_task = spawn(_generate_followup_module(
        GUT_BARRIER_REPORT_TYPE,
        lambda: gut_barrier_report(data, user_id, session_id),
        data=data, user_id=user_id, session_id=session_id, policy=policy,
    ))
    opinion_task = spawn(_generate_followup_module(
        "opinion",
        lambda: opinion_report(followup_input, user_id, session_id),
        data=followup_input, user_id=user_id, session_id=session_id, policy=policy,
    ))
    # 有依赖：info / 产品目录 → recommend → instruction
    instruction_task, dependencies = await _spawn_instruction_chain(
        followup_input, user_id, session_id, policy=policy, spawn=spawn,
    )

    (gut_barrier, gut_status), (opinion, opinion_status), (instruction, instruction_status) = await asyncio.gather(
        gut_barrier_task, opinion_task, instruction_task,
    )
    payloads = _stamp_final_report_id((gut_barrier, opinion, instruction))
    generation_statuses = {
        GUT_BARRIER_REPORT_TYPE: gut_status,
        "opinion": opinion_status,
        "instruction": instruction_status,
    }
    return await _publish_followups(
        payloads, generation_statuses, dependencies,
        policy=policy, completed_log="gut barrier workflow completed: %s",
        namespaced_types=GUT_BARRIER_NAMESPACED_TYPES,
        namespace=GUT_BARRIER_ARTIFACT_NAMESPACE,
    )

# async def _execute_gut_barrier_module(data: dict[str, Any], user_id: str, session_id: str, *,
#                                       policy: ReportFallbackPolicy, spawn: Callable) -> FollowupExecutionResult:
#     """type=gutBarrier 时只生成《肠道与炎症报告解读》一份，不触发其余三份。"""
#     logger.info("starting gut barrier interpretation: %s", _source_fact_summary(data))
#     task = spawn(_generate_followup_module(
#         GUT_BARRIER_REPORT_TYPE,
#         lambda: gut_barrier_report(data, user_id, session_id),
#         data=data, user_id=user_id, session_id=session_id, policy=policy,
#     ))
#     payload, status = await task
#     payloads = _stamp_final_report_id((payload,))
#     generation_statuses = {GUT_BARRIER_REPORT_TYPE: status}
#
#     deliverable = await _save_followup_artifacts(payloads, generation_statuses)
#     delivery_statuses = await replay_followup_reports(deliverable, policy=policy)
#     module_statuses = {
#         GUT_BARRIER_REPORT_TYPE: {
#             "generation": status,
#             "delivery": delivery_statuses.get(GUT_BARRIER_REPORT_TYPE, "delivery_failed"),
#         },
#     }
#     logger.info("gut barrier workflow completed: %s", module_statuses)
#     return FollowupExecutionResult(payloads=payloads, module_statuses=module_statuses)


async def _execute_followup_modules(data: dict[str, Any], user_id: str, session_id: str, *,
                                    policy: ReportFallbackPolicy, spawn: Callable) -> FollowupExecutionResult:
    """并发调度核心 (Dependency Graph)"""
    # 按源报告类型分流：
    #   gutBarrier -> explain_gutBarrier / opinion / instruction
    #   evaluate   -> explain / opinion / instruction（下面的依赖图）
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        return await _execute_gut_barrier_module(
            data, user_id, session_id, policy=policy, spawn=spawn,
        )
    from .evaluation_report_followups import is_seven_chapter_followup
    if is_seven_chapter_followup(data):
        return await _execute_reviewed_seven_chapter_followups(
            data, user_id, session_id, policy=policy, spawn=spawn,
        )
    # 不再按「评估报告是否含异常项」设门禁：没有异常不等于没有数据，
    # 一刀切短路会让四份报告同时变成空壳。空数据由两层兜底接住 ——
    # 提示词里的《全局兜底规则》负责让模型改用检验指标 / 影像所见 / 时间趋势
    # 或稳态维持型结论继续生成，_coalesce + FOLLOWUP_DEFAULTS 负责补齐模型
    # 仍然漏掉的字段。四份报告一律走完整生成流程。
    logger.info(
        "starting three isolated follow-up reports: %s",
        _source_fact_summary(data),
    )

    # 无依赖项，立即执行
    opinion_task = spawn(_generate_followup_module("opinion", lambda: opinion_report(data, user_id, session_id), data=data, user_id=user_id, session_id=session_id, policy=policy))
    explain_task = spawn(_generate_followup_module("explain", lambda: explain_report(data, user_id, session_id), data=data, user_id=user_id, session_id=session_id, policy=policy))

    # 有依赖：info / 产品目录 → recommend → instruction
    instruction_task, dependencies = await _spawn_instruction_chain(
        data, user_id, session_id, policy=policy, spawn=spawn,
    )

    # 汇总所有结果
    # [已停用] intervene：恢复时把 intervene_task / intervene 加回 gather 与 payloads
    # (intervene, intervene_status), (instruction, instruction_status), (opinion, opinion_status), (explain, explain_status) = await asyncio.gather(intervene_task, instruction_task, opinion_task, explain_task)
    # payloads = _stamp_final_report_id((intervene, instruction, opinion, explain))
    (instruction, instruction_status), (opinion, opinion_status), (explain, explain_status) = await asyncio.gather(instruction_task, opinion_task, explain_task)
    payloads = _stamp_final_report_id((instruction, opinion, explain))

    generation_statuses = {
        # "intervene": intervene_status,
        "instruction": instruction_status,
        "opinion": opinion_status,
        "explain": explain_status
    }
    return await _publish_followups(
        payloads, generation_statuses, dependencies,
        policy=policy, completed_log="follow-up workflow completed: %s",
    )


async def _execute_reviewed_seven_chapter_followups(data, user_id, session_id, *, policy, spawn):
    """Keep the original lifecycle; replace only new evaluate medical content."""
    from .evaluation_report_followups import (
        empty_projected_followup, followup_metadata, project_reviewed_followup,
        reviewed_followup_source_matches,
    )
    source = data.get('_reviewed_seven_chapter')
    envelope = followup_metadata(data)
    envelope['_seven_chapter_projection'] = True

    async def project(kind):
        try:
            body, gaps = project_reviewed_followup(kind, source,
                user_id=user_id, session_id=session_id, fallback=envelope)
        except Exception as error:
            # A projection failure never re-enters legacy clinical models,
            # product selection, inferred schedules or default examinations.
            logger.warning('seven chapter follow-up projection incomplete: type=%s error_type=%s',
                kind, type(error).__name__)
            body = empty_projected_followup(kind, user_id=user_id, session_id=session_id,
                subject=deepcopy(envelope.get('subject')))
            gaps = ('reviewed_projection_unavailable',)
        active_gaps = _ACTIVE_FOLLOWUP_GAPS.get()
        if active_gaps is not None:
            active_gaps.update(gaps)
        if gaps:
            logger.warning('seven chapter follow-up projection incomplete: type=%s gaps=%s',
                kind, ','.join(gaps))
        return body

    kinds = ('instruction', 'opinion', 'explain')
    jobs = [spawn(_generate_followup_module(kind, lambda kind=kind: project(kind),
        data=envelope, user_id=user_id, session_id=session_id, policy=policy)) for kind in kinds]
    results = await asyncio.gather(*jobs)
    payloads = _stamp_final_report_id(tuple(body for body, _ in results))
    statuses = {kind: status for kind, (_, status) in zip(kinds, results)}
    dependencies = {'reviewed_publication': 'success' if reviewed_followup_source_matches(source) else 'unavailable'}
    return await _publish_followups(payloads, statuses, dependencies,
        policy=policy, completed_log='seven chapter follow-up projection completed: %s')


async def _fallback_followup_module(
        report_type: str,
        *,
        data: dict[str, Any],
        user_id: str,
        session_id: str) -> tuple[dict[str, Any], str]:
    payload = default_followup_report(report_type, evaluate=data, user_id=user_id, session_id=session_id)
    return await _archive_finished_followup(payload, "fallback_empty", data=data), "fallback_empty"


def _gut_barrier_fact_summary(data: dict[str, Any]) -> str:
    """一行说明肠道与炎症报告带来了哪些源事实。"""
    contexts = build_gut_barrier_contexts(data)
    part1, part2 = contexts["part1"], contexts["part2"]
    return (
        f"type={_source_type(data)!r} status={data.get('status')!r} "
        f"indicators={len(part1['indicator_table'].get('rows') or [])} "
        f"abnormal={len(part1['abnormal_indicators'])} "
        f"pending={len(part1['pending_indicators'])} "
        f"ibfc={part1['ibfc'].get('score') or '-'} "
        f"risk_rows={part2['stats']['total']} assessed={part2['stats']['assessed']} "
        f"references={len(contexts['references'])} "
        f"data_gaps={len(part1['data_gaps'])}"
    )


def _source_fact_summary(data: dict[str, Any]) -> str:
    """一行说明源报告带来了哪些源事实。

    取代原来的 ``_has_followup_source_facts`` 门禁：这些计数只用于日志回溯
    「这份报告为什么这么写」，不再决定报告生不生成。
    """
    if _source_type(data) == SOURCE_TYPE_GUT_BARRIER:
        return _gut_barrier_fact_summary(data)
    assessment = data.get("healthAssessment") or data.get("health_assessment") or {}
    findings = (data.get("diagnostic_basis") or {}).get("abnormal_findings") or []
    indicators = (
        assessment.get("abnormalIndicators")
        or assessment.get("abnormal_indicators")
        or []
    )
    dimensions = [
        item for item in (assessment.get("dimensions") or [])
        if isinstance(item, dict)
        and (item.get("evidenceUsed") or item.get("evidence_used"))
    ]
    return (
        f"status={data.get('status')!r} "
        f"partial_reasons={data.get('partial_reasons') or data.get('partialReasons') or []} "
        f"abnormal_findings={len(findings)} "
        f"abnormal_indicators={len(indicators)} "
        f"dimensions_with_evidence={len(dimensions)} "
        f"evidence={len(data.get('evidence') or [])}"
    )


if __name__ == "__main__":
    data = {}
    user_id = "9527"
    session_id = "abc123xyz456"
    # 本地跑测试时需要初始化一个 Mock 的 Gateway，否则 _ACTIVE_FOLLOWUP_GATEWAY 是空的会报错
    asyncio.run(_push_report(data, user_id, session_id))
