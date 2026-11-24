"""Locust 场景共享库：配置、身份、payload、SSE 解析与指标。

本目录与主项目完全隔离：不 import 任何项目模块，不读取 .env，
请求体与 SSE 事件格式以 docs/api/API.md 的契约为准内嵌为字面常量。
配置只通过 locust 命令行选项传入：被测地址用原生 --host，
--enable-real-llm / --session-turns 为本目录自定义选项，
用法见 tests/loadtest/README.md。
"""

from __future__ import annotations

import codecs
import json
import logging
import time
import uuid

from locust import events
from locust.exception import StopUser

logger = logging.getLogger(__name__)

DEFAULT_TARGET = 'http://127.0.0.1:9002'

# 基建层接口不触 LLM，超时可以收紧；LLM 接口按模型最长执行时间放宽。
CONNECT_TIMEOUT = 5
INFRA_READ_TIMEOUT = 30
LLM_READ_TIMEOUT = 300

# 聊天消息语料：模拟真实用户咨询，控制单轮 token 成本。
CHAT_MESSAGES = (
    '最近总是睡不好，容易半夜醒，有什么办法改善吗？',
    '体检说血压偏高，平时饮食上要注意什么？',
    '饭后容易胃胀，是不是消化有问题？',
    '想开始跑步锻炼，但膝盖以前受过伤，怎么安排比较安全？',
    '体检查出轻度脂肪肝，需要吃药吗？',
    '最近掉头发比以前多，正常吗？',
    '长期对着电脑，颈椎不舒服，有什么缓解方法？',
    '最近工作压力大，经常头疼，需要去医院检查吗？',
)

# 建档答案语料：模拟真实用户的自由文本回答。
QA_ANSWERS = (
    '没有明显不适',
    '平时血压正常，偶尔偏高',
    '睡眠一般，每天六七个小时',
    '不吸烟，偶尔少量饮酒',
    '饮食规律，口味偏清淡',
    '没有慢性病史',
    '偶尔运动，每周一两次',
    '家族没有遗传病史',
)


@events.init.add_listener
def _validate_host(environment, **_kwargs) -> None:
    """校验 --host 提供的服务地址格式，避免漏写协议导致整轮压测白跑。

    未传 --host 时 environment.host 为空，各场景 User 使用类属性
    DEFAULT_TARGET（本机默认值），由 locust 原生机制解析。
    """
    host = environment.host
    if host and not host.startswith(('http://', 'https://')):
        raise SystemExit(f'--host 必须以 http:// 或 https:// 开头，当前值: {host!r}')


@events.init.add_listener
def _drop_tag_filtered_classes(environment, **_kwargs) -> None:
    """把 --tags / --exclude-tags 下任务全部不命中的 User 类移出运行。

    locust 2.46 的标签过滤只剔除任务、不移除 User 类；任务被清空的类
    仍会被实例化并在启动时抛 "No tasks defined" 中断压测。init 事件
    先于用户派发，此处按与 locust 相同的规则（@tag 装饰器的
    locust_tag_set）改写 environment.user_classes。未传标签时不做
    任何事，保持原有混合运行行为。
    """
    if environment.tags is not None:
        tags = set(environment.tags)
    elif environment.parsed_options and getattr(environment.parsed_options, 'tags', False):
        tags = set(environment.parsed_options.tags)
    else:
        tags = None
    if environment.exclude_tags is not None:
        exclude_tags = set(environment.exclude_tags)
    elif environment.parsed_options and getattr(environment.parsed_options, 'exclude_tags', False):
        exclude_tags = set(environment.parsed_options.exclude_tags)
    else:
        exclude_tags = None
    if tags is None and exclude_tags is None:
        return

    def _task_passes(task_item) -> bool:
        tag_set = getattr(task_item, 'locust_tag_set', None)
        if tags is not None and not (tag_set and tag_set & tags):
            return False
        if exclude_tags is not None and tag_set and tag_set & exclude_tags:
            return False
        return True

    remaining = [uc for uc in environment.user_classes if any(_task_passes(t) for t in uc.tasks)]
    if not remaining:
        raise SystemExit('--tags/--exclude-tags 过滤后没有可运行的 User 类，请检查标签拼写')
    environment.user_classes = remaining


def real_llm_enabled(environment) -> bool:
    return bool(getattr(environment.parsed_options, 'enable_real_llm', False))


def get_session_turns(environment) -> int:
    options = environment.parsed_options
    value = getattr(options, 'session_turns', None)
    if value is None:
        value = 6
    if value < 1:
        raise SystemExit(f'--session-turns 必须为正整数，当前值: {value!r}')
    return value


def require_real_llm(environment) -> None:
    """成本熔断：未显式开启时，直接停用会消耗真实 LLM 的虚拟用户。"""
    if not real_llm_enabled(environment):
        logger.warning(
            '场景会调用真实 LLM（产生 token 费用）。如确认执行，请在命令中加 --enable-real-llm 重新启动 locust。'
        )
        raise StopUser()


def new_user_id() -> str:
    """每个虚拟用户使用独立 userId，避免 Redis 会话租约把同一会话串行化。"""
    return f'loadtest-{uuid.uuid4().hex[:12]}'


def new_session_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def chat_complete_payload(user_id: str, session_id: str, message: str) -> dict:
    # thinking 显式关闭以控制成本；与建档默认行为一致。
    return {
        'userId': user_id,
        'sessionId': session_id,
        'message': message,
        'thinking': False,
    }


def qa_create_payload(user_id: str, session_id: str) -> dict:
    return {'userId': user_id, 'sessionId': session_id}


def qa_answer_payload(user_id: str, session_id: str, question_id: str, answer: str) -> dict:
    return {
        'userId': user_id,
        'sessionId': session_id,
        'questionId': question_id,
        'answer': answer,
        'thinking': False,
    }


def subject_upsert_payload(user_id: str) -> dict:
    # 画像播种：创建一个非本人主体，使 subjects/list 等读接口有稳定数据形态。
    return {
        'userId': user_id,
        'requestId': f'loadtest-{uuid.uuid4().hex[:12]}',
        'kind': 'friend',
        'displayName': '压测用户',
        'relationship': {'relationType': 'friend', 'isBloodRelative': False},
    }


def envelope_ok(response) -> tuple[bool, dict]:
    """校验统一响应包络：HTTP 200 且 code==200；返回 (是否通过, 解析后的 body)。"""
    try:
        body = response.json()
    except ValueError:
        return False, {'raw': response.text[:200]}
    if response.status_code != 200:
        return False, body
    if not isinstance(body, dict) or body.get('code') != 200:
        return False, body
    return True, body


def fire_sse_metric(user: object, name: str, response_time_ms: float) -> None:
    """把 TTFT 等自定义指标发进 locust 统计表，报表独立成行。"""
    user.environment.events.request.fire(
        request_type='SSE',
        name=name,
        response_time=response_time_ms,
        response_length=0,
        exception=None,
        context={},
    )


def iter_sse_data_lines(response):
    """逐事件解析 SSE 流。

    线格式为 ``data: <JSON>\n\n``（契约见 docs/api/API.md）。按字节读取并使用
    增量 UTF-8 解码器，避免多字节字符被网络分块切断；忽略注释行（``:``）与
    事件分隔空行。每个 data 行 yield 一次（服务端每个事件只写一行 data）。
    """
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer = ''
    for chunk in response.iter_content(chunk_size=None):
        if not chunk:
            continue
        try:
            text = decoder.decode(chunk)
        except UnicodeDecodeError as error:
            raise RuntimeError(f'SSE 流出现非法 UTF-8 字节: {error}') from error
        if not text:
            continue
        buffer += text
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.rstrip('\r')
            if not line or line.startswith(':'):
                continue
            if line.startswith('data:'):
                yield line[len('data:') :].lstrip()
            # 未知行（如 event: 前缀）忽略，当前契约未使用。


def consume_sse_stream(response, first_types: tuple[str, ...]) -> dict:
    """消费一条 SSE 流并返回统计信息。

    ``first_types`` 是计入“首字”的事件类型（如 ('content',) 或 ('question',)），
    口径与 wiki/qa-response-latency.md 一致：排除 start、reasoning 与心跳。
    返回 dict：ttft_ms、total_ms、events、finish_reached、error。
    """
    start = time.perf_counter()
    ttft_ms = None
    events = 0
    finish_reached = False
    error = None
    for data in iter_sse_data_lines(response):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            error = f'SSE data 非合法 JSON: {data[:120]!r}'
            raise RuntimeError(error) from exc
        events += 1
        inner = payload.get('data')
        if not isinstance(inner, dict):
            error = 'SSE 事件缺少 data 对象'
            break
        event_type = inner.get('type')
        content = inner.get('content')
        if event_type in first_types and content and ttft_ms is None:
            ttft_ms = (time.perf_counter() - start) * 1000
        if event_type == 'finish':
            finish_reached = True
        if event_type == 'error':
            error = str(inner.get('error', '未知错误'))[:200]
            break
    total_ms = (time.perf_counter() - start) * 1000
    return {
        'ttft_ms': ttft_ms,
        'total_ms': total_ms,
        'events': events,
        'finish_reached': finish_reached,
        'error': error,
    }


@events.init_command_line_parser.add_listener
def _(parser) -> None:
    """在 locust CLI 注册本目录专用参数，便于脚本化运行。

    被测服务地址显式复用 locust 原生 --host，不另造等效选项。
    """
    parser.add_argument(
        '--loadtest-tag',
        default='adhoc',
        help='本次压测标签，写入结果文件名（仅提示用途）',
    )
    parser.add_argument(
        '--enable-real-llm',
        action='store_true',
        default=False,
        help='允许调用真实 LLM 的场景（产生 token 费用）；缺省时相关场景被熔断',
    )
    parser.add_argument(
        '--session-turns',
        type=int,
        default=None,
        metavar='N',
        help='聊天会话轮换阈值，默认 6（控制 AION 上下文增长）',
    )
