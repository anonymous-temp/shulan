"""Health, liveness and readiness handlers."""

from __future__ import annotations

import asyncio

from config.env import ENVIRONMENT
from web.handlers.base import BaseHandler
from web.lifecycle import ApplicationLifecycle, LifecycleState, TaskSupervisor


def _lifecycle_health_data(application) -> dict:
    lifecycle = application.settings.get('lifecycle')
    task_supervisor = application.settings.get('task_supervisor')
    if lifecycle is None or not callable(getattr(lifecycle, 'snapshot', None)):
        return {
            'lifecycleState': LifecycleState.READY.value,
            'ready': True,
            'activeRequests': 0,
            'activeTasks': 0,
        }
    snapshot = lifecycle.snapshot()
    active_tasks = (
        task_supervisor.active_task_count
        if isinstance(task_supervisor, TaskSupervisor)
        else 0
    )
    return {
        'lifecycleState': snapshot.state.value,
        'ready': snapshot.ready,
        'activeRequests': snapshot.active_requests,
        'activeTasks': active_tasks,
    }


class HealthCheckHandler(BaseHandler):
    lifecycle_exempt = True

    async def get(self) -> None:
        data = await self.check()
        data.update(_lifecycle_health_data(self.application))
        self.write_response(200, 'success', data)

    @staticmethod
    async def check() -> dict:
        await asyncio.sleep(1)
        return {'status': 'healthy', 'environment': ENVIRONMENT}


class LivenessHandler(BaseHandler):
    lifecycle_exempt = True

    async def get(self) -> None:
        self.write_response(
            200,
            'success',
            {'status': 'alive', **_lifecycle_health_data(self.application)},
        )


class ReadinessHandler(BaseHandler):
    lifecycle_exempt = True

    async def get(self) -> None:
        data = _lifecycle_health_data(self.application)
        status_code = 200 if data['ready'] else 503
        if status_code == 503:
            self.set_status(status_code)
            self.set_header('Connection', 'close')
            self.set_header('Retry-After', '5')
        self.write_response(
            status_code,
            'success' if data['ready'] else 'service is not ready',
            data,
        )


__all__ = [
    'HealthCheckHandler',
    'LivenessHandler',
    'ReadinessHandler',
    '_lifecycle_health_data',
]
