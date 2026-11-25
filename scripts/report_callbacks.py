"""Inspect or redrive stored reports without invoking report generation.

Run ``python -m scripts.report_callbacks --help`` for examples. Database and
deployment settings are imported only after argument parsing.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json

DEFAULT_MIGRATION_REASON = '积压报告迁移到当前服务回调配置'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'retry', 'migrate-pending'],
                        help='migrate-pending 按报告类型迁移到当前配置的评估回调地址或公共回调地址')
    parser.add_argument('--user-id')
    parser.add_argument('--final-report-id')
    parser.add_argument('--target-url', help='retry 时将该批全部未确认类型统一改投到此地址，历史批次补投时必填')
    parser.add_argument('--reason', help='retry 必填；migrate-pending 可选，默认记录配置迁移原因')
    parser.add_argument('--operator', default=getpass.getuser())
    parser.add_argument('--dry-run', action='store_true', help='仅预览 migrate-pending 的范围，不修改任务')
    args = parser.parse_args(argv)
    if args.action == 'migrate-pending':
        if any(value is not None for value in (args.user_id, args.final_report_id, args.target_url)):
            parser.error('migrate-pending uses configured URLs and does not accept user/report/target filters')
        args.reason = (args.reason or '').strip() or DEFAULT_MIGRATION_REASON
    else:
        if not args.user_id or not args.final_report_id:
            parser.error(f'{args.action} requires --user-id and --final-report-id')
        if args.dry_run:
            parser.error('--dry-run is only valid for migrate-pending')
    if args.action == 'retry' and not (args.reason or '').strip():
        parser.error('retry requires --reason')
    if args.action == 'status' and args.target_url:
        parser.error('--target-url is only valid for retry')
    if args.target_url:
        from modules.report.durable import validate_callback_url
        try:
            validate_callback_url(args.target_url)
        except ValueError as error:
            parser.error(str(error))
    return args


async def run(args):
    from adapters.report.factory import create_application_report_artifact_store
    from db.mongodb import client
    from modules.report.artifacts import REPORT_TYPES

    store = create_application_report_artifact_store()
    workflow = store.workflow
    try:
        if args.action == 'migrate-pending':
            from config.env import REPORT_CALLBACK_URL
            from config.env import REPORT_EVALUATE_CALLBACK_URL
            from modules.report.durable import validate_callback_url

            result = await workflow.migrate_pending(
                target_url=validate_callback_url(REPORT_CALLBACK_URL),
                evaluate_callback_url=validate_callback_url(REPORT_EVALUATE_CALLBACK_URL or REPORT_CALLBACK_URL),
                operator=args.operator, reason=args.reason, dry_run=args.dry_run,
            )
        elif args.action == 'retry':
            result = await workflow.redrive(
                user_id=args.user_id, final_report_id=args.final_report_id,
                target_url=args.target_url, operator=args.operator, reason=args.reason,
            )
        else:
            # Scope both identity and batch; never expose the stored request or URL.
            batch = await workflow.batches.find_one({
                '_id': args.final_report_id, 'userId': args.user_id,
            })
            if batch is None:
                raise ValueError('report batch not found for user')
            result = {
                'finalReportId': args.final_report_id,
                'generationStatus': batch.get('generationStatus', 'unknown'),
                'deliveries': [{
                    'reportType': report_type,
                    **await store.get_delivery(user_id=args.user_id,
                                               final_report_id=args.final_report_id, report_type=report_type),
                } for report_type in REPORT_TYPES],
            }
        print(json.dumps(result, ensure_ascii=False, indent=4))
        if result.get('status') == 'partial_failure':
            raise SystemExit(1)
        return result
    finally:
        await client.close()


def main():
    args = parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        # Do not print database credentials or Python tracebacks in an ops command.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit(f'报告回调操作失败: {detail}') from None


if __name__ == '__main__':
    main()
