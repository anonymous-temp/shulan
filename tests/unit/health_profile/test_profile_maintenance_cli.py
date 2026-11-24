"""用户画像摄取内部运维命令测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from datetime import timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from health_profile import ProfileIngestionSourceType
from health_profile import ProfileJobStatus
from health_profile.maintenance_cli import build_parser
from health_profile.maintenance_cli import run_command


class FakeMaintenanceService:
    def __init__(self) -> None:
        self.list_calls = []
        self.redrive_calls = []
        self.job = SimpleNamespace(
            job_id='profile-job-1',
            user_id='profile-user-1',
            source_type=ProfileIngestionSourceType.CHAT,
            source_id='message-1',
            status=ProfileJobStatus.DEAD_LETTER,
            attempts=5,
            error_code='extractor.invalid_output',
            processing_summary=None,
            updated_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )

    async def get_job(self, job_id):
        self.asserted_job_id = job_id
        return self.job

    async def list_recoverable_jobs(self, **values):
        self.list_calls.append(values)
        return (self.job,)

    async def redrive(self, **values):
        self.redrive_calls.append(values)
        return self.job


class HealthProfileMaintenanceCliTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.service = FakeMaintenanceService()

    async def test_inspect_is_read_only_and_legacy_summary_is_unknown(self):
        arguments = self.parser.parse_args(['inspect', '--job-id', 'profile-job-1'])
        with patch('sys.stdout', new_callable=StringIO) as output:
            self.assertEqual(await run_command(arguments, service=self.service), 0)
        data = json.loads(output.getvalue())
        self.assertIsNone(data['processingSummary'])
        self.assertEqual(data['status'], 'dead_letter')
        self.assertEqual(self.service.redrive_calls, [])

    async def test_list_parses_terminal_and_source_filters(self) -> None:
        arguments = self.parser.parse_args(
            [
                'list',
                '--status',
                'dead_letter',
                '--source-type',
                'chat',
                '--limit',
                '25',
            ]
        )

        result = await run_command(arguments, service=self.service)

        self.assertEqual(result, 0)
        self.assertEqual(len(self.service.list_calls), 1)
        values = self.service.list_calls[0]
        self.assertEqual(
            values['statuses'],
            (ProfileJobStatus.DEAD_LETTER,),
        )
        self.assertEqual(
            values['source_types'],
            (ProfileIngestionSourceType.CHAT,),
        )
        self.assertEqual(values['limit'], 25)

    async def test_redrive_defaults_to_preview(self) -> None:
        arguments = self.parser.parse_args(
            [
                'redrive',
                '--job-id',
                'profile-job-1',
                '--expected-status',
                'dead_letter',
                '--reason-code',
                'fix.extractor-json',
                '--actor-id',
                'operator:test',
            ]
        )

        result = await run_command(arguments, service=self.service)

        self.assertEqual(result, 0)
        self.assertEqual(self.service.asserted_job_id, 'profile-job-1')
        self.assertEqual(self.service.redrive_calls, [])

    async def test_redrive_requires_apply_to_mutate(self) -> None:
        arguments = self.parser.parse_args(
            [
                'redrive',
                '--job-id',
                'profile-job-1',
                '--expected-status',
                'dead_letter',
                '--reason-code',
                'fix.extractor-json',
                '--actor-id',
                'operator:test',
                '--apply',
            ]
        )

        result = await run_command(arguments, service=self.service)

        self.assertEqual(result, 0)
        self.assertEqual(
            self.service.redrive_calls,
            [
                {
                    'job_id': 'profile-job-1',
                    'expected_status': ProfileJobStatus.DEAD_LETTER,
                    'reason_code': 'fix.extractor-json',
                    'actor_id': 'operator:test',
                }
            ],
        )


if __name__ == '__main__':
    unittest.main()
