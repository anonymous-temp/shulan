"""用户画像摄取任务内部运维命令。

示例：

    python -m health_profile.maintenance_cli list --status dead_letter
    python -m health_profile.maintenance_cli redrive \
        --job-id profile_job_xxx \
        --expected-status dead_letter \
        --reason-code fix.extractor-json \
        --actor-id operator.name
    python -m health_profile.maintenance_cli redrive ... --apply

`redrive` 默认只预览；只有显式传入 `--apply` 才会修改 MongoDB。
"""

from __future__ import annotations

import argparse
import asyncio
import json

from utils.log import getLogger
from utils.log import shutdown_logging
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import task_log_context

from .application import create_application_health_profile_job_maintenance_service
from .maintenance import HealthProfileJobMaintenanceService
from .models import ProfileIngestionSourceType
from .models import ProfileJobStatus

logger = getLogger(__name__)

_TERMINAL_STATUS_VALUES = (
    ProfileJobStatus.COMPLETED.value,
    ProfileJobStatus.DEAD_LETTER.value,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='查询或人工重驱用户画像摄取终态任务',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    list_command = commands.add_parser('list', help='查询终态任务')
    list_command.add_argument(
        '--status',
        action='append',
        choices=_TERMINAL_STATUS_VALUES,
        help='可重复指定；默认查询 completed 和 dead_letter',
    )
    list_command.add_argument(
        '--source-type',
        action='append',
        choices=tuple(item.value for item in ProfileIngestionSourceType),
        help='可重复指定；默认查询全部来源',
    )
    list_command.add_argument('--limit', type=int, default=100)

    inspect_command = commands.add_parser('inspect', help='只读查询单个任务的状态、数量及跳过原因')
    inspect_command.add_argument('--job-id', required=True)

    redrive_command = commands.add_parser(
        'redrive',
        help='预览或重驱单个终态任务',
    )
    redrive_command.add_argument('--job-id', required=True)
    redrive_command.add_argument(
        '--expected-status',
        required=True,
        choices=_TERMINAL_STATUS_VALUES,
    )
    redrive_command.add_argument('--reason-code', required=True)
    redrive_command.add_argument('--actor-id', required=True)
    redrive_command.add_argument(
        '--apply',
        action='store_true',
        help='确认修改；不提供时仅预览',
    )
    return parser


@task_log_context(clear=True)
async def run_command(
    arguments: argparse.Namespace,
    *,
    service: HealthProfileJobMaintenanceService | None = None,
) -> int:
    service = service if service is not None else create_application_health_profile_job_maintenance_service()
    if arguments.command == 'inspect':
        job = await service.get_job(arguments.job_id)
        print(
            json.dumps(
                {
                    'jobId': job.job_id,
                    'sourceType': job.source_type.value,
                    'sourceId': job.source_id,
                    'status': job.status.value,
                    'attempts': job.attempts,
                    'errorCode': job.error_code,
                    'processingSummary': (
                        job.processing_summary.model_dump(mode='json', by_alias=True)
                        if job.processing_summary is not None
                        else None
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if arguments.command == 'list':
        statuses = tuple(ProfileJobStatus(value) for value in (arguments.status or _TERMINAL_STATUS_VALUES))
        source_types = (
            None
            if not arguments.source_type
            else tuple(ProfileIngestionSourceType(value) for value in arguments.source_type)
        )
        jobs = await service.list_recoverable_jobs(
            statuses=statuses,
            source_types=source_types,
            limit=arguments.limit,
        )
        for job in jobs:
            with bind_log_context(LogContext(user_id=job.user_id, task_id=job.job_id)):
                logger.info(
                    '画像摄取终态任务: '
                    f'job_id={job.job_id}, '
                    f'source_type={job.source_type.value}, '
                    f'source_id={job.source_id}, status={job.status.value}, '
                    f'attempts={job.attempts}, '
                    f'error_code={job.error_code or "none"}, '
                    f'processing_summary={job.processing_summary}, '
                    f'updated_at={job.updated_at.isoformat()}',
                )
        logger.info(f'画像摄取终态任务查询数量: count={len(jobs)}')
        return 0

    expected_status = ProfileJobStatus(arguments.expected_status)
    current = await service.get_job(arguments.job_id)
    with bind_log_context(LogContext(user_id=current.user_id, task_id=current.job_id)):
        if not arguments.apply:
            logger.info(
                '画像摄取任务重驱预览（未修改）: '
                f'job_id={current.job_id}, '
                f'expected_status={expected_status.value}, '
                f'actual_status={current.status.value}, '
                f'attempts={current.attempts}, '
                f'error_code={current.error_code or "none"}, '
                f'reason_code={arguments.reason_code}, '
                f'actor_id={arguments.actor_id}',
            )
            return 0
        await service.redrive(
            job_id=arguments.job_id,
            expected_status=expected_status,
            reason_code=arguments.reason_code,
            actor_id=arguments.actor_id,
        )
        return 0


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return asyncio.run(run_command(arguments))
    except Exception as error:
        logger.error(
            f'画像摄取运维命令执行失败: command={arguments.command}, error_type={type(error).__name__}, error={error}',
        )
        return 1
    finally:
        shutdown_logging()


if __name__ == '__main__':
    raise SystemExit(main())
