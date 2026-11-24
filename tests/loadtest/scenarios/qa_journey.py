"""建档全旅程场景：create → 多轮 answer/complete → 自动完成或 complete 收尾。

默认被成本熔断拦截；加 --enable-real-llm 显式开启后每个虚拟用户用独立
userId+sessionId 走完整建档多轮流程，模拟真实用户旅程（最贵的真实使用场景）。
运行示例（换目标加 --host）::

    cd tests/loadtest
    python -m locust -f scenarios/qa_journey.py --headless -u 3 -r 1 -t 10m --enable-real-llm
"""

from __future__ import annotations

import itertools

from _lib import CONNECT_TIMEOUT
from _lib import DEFAULT_TARGET
from _lib import LLM_READ_TIMEOUT
from _lib import QA_ANSWERS
from _lib import envelope_ok
from _lib import new_session_id
from _lib import new_user_id
from _lib import qa_answer_payload
from _lib import qa_create_payload
from _lib import require_real_llm
from locust import HttpUser
from locust import constant_pacing
from locust import task

# 建档单会话轮次上限：真实流程 3-5 次初始交互 + 补充答疑，最多 20 次交互。
MAX_TURNS = 20


class QaJourneyUser(HttpUser):
    # 实际目标在 locust 启动时由 --host 覆盖（见 _lib）。
    host = DEFAULT_TARGET
    # 建档每轮间隔：真实用户读题、思考、作答的时间。
    wait_time = constant_pacing(8)

    def on_start(self) -> None:
        require_real_llm(self.environment)
        self.client.trust_env = False
        self.user_id = new_user_id()
        self.answers = itertools.cycle(QA_ANSWERS)

    def _post(self, path: str, payload: dict, name: str):
        return self.client.post(
            path,
            json=payload,
            name=name,
            timeout=(CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            catch_response=True,
        )

    @task
    def journey(self) -> None:
        session_id = new_session_id('qa')
        with self._post(
            '/api/v1/qa/create',
            qa_create_payload(self.user_id, session_id),
            '/api/v1/qa/create',
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/qa/create 失败: {body}')
                return
            data = body.get('data') or {}
            if data.get('status') != 'question':
                # 幂等重放或准备状态：仍按失败暴露，压测身份必须全新。
                response.failure(f'创建未返回首题: status={data.get("status")!r}')
                return
            question_id = data.get('questionId')
            response.success()

        if not question_id:
            return

        for _turn in range(MAX_TURNS):
            answer = next(self.answers)
            with self._post(
                '/api/v1/qa/answer/complete',
                qa_answer_payload(self.user_id, session_id, question_id, answer),
                '/api/v1/qa/answer/complete',
            ) as response:
                ok, body = envelope_ok(response)
                if not ok:
                    response.failure(f'/api/v1/qa/answer/complete 失败: {body}')
                    return
                data = body.get('data') or {}
                status = data.get('status')
                if status == 'finished':
                    response.success()
                    return
                if status != 'question':
                    response.failure(f'意外的会话状态: {status!r}')
                    return
                question_id = data.get('questionId')
                response.success()
            if not question_id:
                return

        # 达到轮次上限仍未自动完成：显式收尾，避免会话悬挂占用租约。
        with self._post(
            '/api/v1/qa/complete',
            {'userId': self.user_id, 'sessionId': session_id, 'round': 1},
            '/api/v1/qa/complete',
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/qa/complete 失败: {body}')
            else:
                response.success()
