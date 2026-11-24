"""L1 基建层读接口负载场景：不触 LLM，可高压验证 Tornado/MongoDB/Redis 容量。

每个虚拟用户独立 userId 并在 on_start 播种一个非本人主体，
使 subjects/list、facts/list 等读接口具有稳定数据形态。
运行示例（阶梯加压参数见 tests/loadtest/README.md；换目标加 --host）::

    cd tests/loadtest
    python -m locust -f scenarios/infra_read.py --headless -u 50 -r 5 -t 2m
"""

from __future__ import annotations

from _lib import CONNECT_TIMEOUT
from _lib import DEFAULT_TARGET
from _lib import INFRA_READ_TIMEOUT
from _lib import envelope_ok
from _lib import new_user_id
from _lib import subject_upsert_payload
from locust import HttpUser
from locust import between
from locust import task


class InfraReadUser(HttpUser):
    # 实际目标在 locust 启动时由 --host 覆盖（见 _lib）。
    host = DEFAULT_TARGET
    wait_time = between(0.5, 2)

    def on_start(self) -> None:
        self.client.trust_env = False
        self.user_id = new_user_id()
        self._seed_subject()

    def _seed_subject(self) -> None:
        with self.client.post(
            '/api/v1/health-profile/subjects/upsert',
            json=subject_upsert_payload(self.user_id),
            name='/api/v1/health-profile/subjects/upsert [seed]',
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, body = envelope_ok(response)
            if not ok:
                response.failure(f'主体播种失败: {body}')
            else:
                response.success()

    def _post_read(self, path: str, body: dict, name: str) -> None:
        with self.client.post(
            path,
            json=body,
            name=name,
            timeout=(CONNECT_TIMEOUT, INFRA_READ_TIMEOUT),
            catch_response=True,
        ) as response:
            ok, payload = envelope_ok(response)
            if not ok:
                response.failure(f'{name} 失败: {payload}')
            else:
                response.success()

    @task(3)
    def subjects_list(self) -> None:
        self._post_read(
            '/api/v1/health-profile/subjects/list',
            {'userId': self.user_id},
            '/api/v1/health-profile/subjects/list',
        )

    @task(3)
    def profile_get(self) -> None:
        self._post_read(
            '/api/v1/health-profile/get',
            {'userId': self.user_id},
            '/api/v1/health-profile/get',
        )

    @task(2)
    def facts_list(self) -> None:
        self._post_read(
            '/api/v1/health-profile/facts/list',
            {'userId': self.user_id, 'limit': 20},
            '/api/v1/health-profile/facts/list',
        )

    @task(1)
    def document_observations_list(self) -> None:
        self._post_read(
            '/api/v1/health-profile/document-observations/list',
            {'userId': self.user_id, 'limit': 20},
            '/api/v1/health-profile/document-observations/list',
        )

    @task(1)
    def changes_list(self) -> None:
        self._post_read(
            '/api/v1/health-profile/changes/list',
            {'userId': self.user_id, 'limit': 20},
            '/api/v1/health-profile/changes/list',
        )

    @task(2)
    def report_list(self) -> None:
        self._post_read(
            '/api/v1/report/list',
            {'userId': self.user_id, 'limit': 20},
            '/api/v1/report/list',
        )
