"""Shared synthetic fixture closure extracted from tests.unit.health_profile.test_report_ingestion."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from health_profile import HealthProfileJob
from health_profile import HealthReportSourceRecord
from health_profile import HealthSubject
from health_profile import ProfileIngestionSourceType
from health_profile import ProfileJobStatus
from health_profile import SubjectKind


class ReportIngestionFixture:
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)

    @classmethod
    def record(cls, **changes):
        values = {
            'report_id': 'report-1',
            'user_id': 'user-1',
            'subject_id': 'subject-self',
            'source_revision': 1,
            'source_fingerprint': 'fingerprint-1',
            'result': {
                'reportType': 'lab',
                'title': '血液生化检验报告',
                'reportTime': '2026-08-16',
                'status': 'abnormal',
                'expansion': [
                    {
                        'itemName': '尿酸',
                        'resultValue': '468',
                        'referenceRange': '208-428',
                        'unit': 'μmol/L',
                        'flag': '偏高',
                    },
                    {
                        'itemName': '尚未注册的指标',
                        'resultValue': '1.2',
                        'referenceRange': '0-1',
                        'unit': 'U/L',
                        'flag': '偏高',
                    },
                ],
            },
            'ingestion_status': 'queued',
            'ingestion_job_id': 'job-1',
            'created_at': cls.now,
            'updated_at': cls.now,
        }
        values.update(changes)
        return HealthReportSourceRecord(**values)

    @classmethod
    def subject(cls):
        return HealthSubject(
            subject_id='subject-self',
            user_id='user-1',
            kind=SubjectKind.SELF,
            display_name='测试用户',
            created_at=cls.now,
            updated_at=cls.now,
        )

    @classmethod
    def job(cls, **changes):
        values = {
            'job_id': 'job-1',
            'idempotency_key': 'user-1:report:report-1:1',
            'idempotency_fingerprint': 'job-fingerprint-1',
            'source_type': ProfileIngestionSourceType.HEALTH_REPORT,
            'source_id': 'report-1',
            'source_revision': 1,
            'user_id': 'user-1',
            'subject_id': 'subject-self',
            'status': ProfileJobStatus.RUNNING,
            'attempts': 1,
            'lease_owner': 'worker-1',
            'lease_token': 'lease-1',
            'lease_expires_at': cls.now + timedelta(minutes=5),
            'created_at': cls.now,
            'updated_at': cls.now,
        }
        values.update(changes)
        return HealthProfileJob(**values)
