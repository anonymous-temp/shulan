"""L0 冒烟场景：验证被测服务在线、脚本与契约正确。

不调用 LLM、不写业务数据。运行示例（服务在其它机器时加 --host）::

    cd tests/loadtest
    python -m locust -f scenarios/smoke.py --headless -u 1 -r 1 -t 30s
    python -m locust -f scenarios/smoke.py --headless -u 1 -r 1 -t 30s \
        --host http://192.168.1.10:9002

注意：/health 处理器固定 sleep 1 秒，冒烟与探活请使用 /health/live 与 /health/ready。
"""

from __future__ import annotations

from _lib import CONNECT_TIMEOUT
from _lib import DEFAULT_TARGET
from _lib import INFRA_READ_TIMEOUT
from _lib import envelope_ok
from _lib import new_user_id
from locust import HttpUser
from locust import between
from locust import task


class SmokeUser(HttpUser):
    # 实际目标在 locust 启动时由 --host 覆盖（见 _lib）。
    host = DEFAULT_TARGET
    wait_time = between(1, 2)

    def on_start(self) -> None:
        # 阻止 requests 读取系统代理环境变量，避免压测流量被本机代理劫持。
        self.client.trust_env = False
        self.user_id = new_user_id()

    @task
    def health_and_read_checks(self) -> None:
        with self.client.get(
            '/health/live',
            name='/health/live',
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, _body = envelope_ok(response)
            if not ok:
                response.failure(f'/health/live 状态异常: HTTP {response.status_code}')
            else:
                response.success()

        with self.client.get(
            '/health/ready',
            name='/health/ready',
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, _body = envelope_ok(response)
            if not ok:
                # 排空期间 ready 返回 503 属预期状态，冒烟阶段视为失败以便发现问题。
                response.failure(f'/health/ready 未就绪: HTTP {response.status_code}')
            else:
                response.success()

        with self.client.post(
            '/api/v1/report/list',
            json={'userId': self.user_id, 'limit': 20},
            name='/api/v1/report/list',
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/report/list 失败: {body}')
            else:
                response.success()

        with self.client.post(
            '/api/v1/health-profile/subjects/list',
            json={'userId': self.user_id},
            name='/api/v1/health-profile/subjects/list',
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'/api/v1/health-profile/subjects/list 失败: {body}')
            else:
                response.success()
