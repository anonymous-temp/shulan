"""L2/L3 聊天场景：真实 LLM 调用，默认被成本熔断拦截。

默认关闭（不加 --enable-real-llm 时全部虚拟用户直接停止），
显式开启后才会产生真实模型调用与 token 费用。thinking 显式 false，
sessionId 每 N 轮轮换以控制上下文增长。三个 User 类分别对应
complete / stream / suggestions 三种流量，不传 --tags 时混合运行，
传 --tags 时只运行命中的类（见 _lib 的类过滤监听器）。运行示例::

    cd tests/loadtest
    # 完整回复（L2）
    python -m locust -f scenarios/chat.py --headless --tags complete -u 5 -r 1 -t 5m
    # SSE 流式（L3，采集 TTFT；换目标加 --host）
    python -m locust -f scenarios/chat.py --headless --tags stream -u 5 -r 1 -t 5m \
        --enable-real-llm --host http://192.168.1.10:9002
    # 推荐问题（轻 LLM 或缓存路径）
    python -m locust -f scenarios/chat.py --headless --tags suggestions -u 5 -r 1 -t 5m
"""

from __future__ import annotations

import itertools

from _lib import CHAT_MESSAGES
from _lib import CONNECT_TIMEOUT
from _lib import DEFAULT_TARGET
from _lib import LLM_READ_TIMEOUT
from _lib import chat_complete_payload
from _lib import consume_sse_stream
from _lib import envelope_ok
from _lib import fire_sse_metric
from _lib import get_session_turns
from _lib import new_session_id
from _lib import new_user_id
from _lib import require_real_llm
from locust import HttpUser
from locust import between
from locust import tag
from locust import task


class ChatCompleteUser(HttpUser):
    """L2：发送消息并等待完整回复。"""

    # 实际目标在 locust 启动时由 --host 覆盖（见 _lib）。
    host = DEFAULT_TARGET
    wait_time = between(3, 8)
    # 真实用户读完回复再提问；这里用较短的思考时间在成本与真实感之间取舍。
    weight = 1

    def on_start(self) -> None:
        require_real_llm(self.environment)
        self.client.trust_env = False
        self.user_id = new_user_id()
        self.session_id = new_session_id('chat')
        self.turns = 0
        self.messages = itertools.cycle(CHAT_MESSAGES)

    @tag('complete')
    @task
    def send_complete(self) -> None:
        if self.turns >= get_session_turns(self.environment):
            self.session_id = new_session_id('chat')
            self.turns = 0
        self.turns += 1
        message = next(self.messages)
        with self.client.post(
            '/api/v1/chat/send/complete',
            json=chat_complete_payload(self.user_id, self.session_id, message),
            name='/api/v1/chat/send/complete',
            timeout=(CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/chat/send/complete 失败: {body}')
            else:
                data = body.get('data')
                if not isinstance(data, dict) or not data.get('reply'):
                    response.failure('回复缺少 data.reply 字段')
                else:
                    response.success()


class ChatStreamUser(HttpUser):
    """L3：SSE 流式聊天，采集 TTFT（首个非空 content 事件）与流总时长。"""

    # 实际目标在 locust 启动时由 --host 覆盖（见 _lib）。
    host = DEFAULT_TARGET
    wait_time = between(3, 8)
    weight = 1

    def on_start(self) -> None:
        require_real_llm(self.environment)
        self.client.trust_env = False
        self.user_id = new_user_id()
        self.session_id = new_session_id('chat')
        self.turns = 0
        self.messages = itertools.cycle(CHAT_MESSAGES)

    @tag('stream')
    @task
    def send_stream(self) -> None:
        if self.turns >= get_session_turns(self.environment):
            self.session_id = new_session_id('chat')
            self.turns = 0
        self.turns += 1
        message = next(self.messages)
        payload = chat_complete_payload(self.user_id, self.session_id, message)
        with self.client.post(
            '/api/v1/chat/send/stream',
            json=payload,
            name='/api/v1/chat/send/stream',
            timeout=(CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            stream=True,
            catch_response=True,
        ) as response:
            try:
                stats = consume_sse_stream(response, first_types=('content',))
            except RuntimeError as error:
                response.failure(f'/api/v1/chat/send/stream 解析失败: {error}')
                return
            if stats['error']:
                response.failure(f'流式事件错误: {stats["error"]}')
                return
            if stats['ttft_ms'] is None:
                response.failure('整条流未收到任何非空 content 事件')
                return
            # 覆盖响应时间口径为流总时长；TTFT 单独上报。
            response.request_meta['response_time'] = stats['total_ms']
            response.success()
            fire_sse_metric(
                self,
                '/api/v1/chat/send/stream [first content]',
                stats['ttft_ms'],
            )


class SuggestionsUser(HttpUser):
    """推荐问题：走轻量建议路径（非多轮对话），作为低成本混合流量。"""

    host = DEFAULT_TARGET
    wait_time = between(1, 3)
    weight = 1

    def on_start(self) -> None:
        require_real_llm(self.environment)
        self.client.trust_env = False
        self.user_id = new_user_id()

    @tag('suggestions')
    @task
    def suggestions(self) -> None:
        with self.client.get(
            '/api/v1/chat/suggestions',
            params={'userId': self.user_id, 'count': 3},
            name='/api/v1/chat/suggestions',
            timeout=(CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/chat/suggestions 失败: {body}')
            else:
                response.success()
