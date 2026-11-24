"""Shared synthetic fixture closure extracted from tests.unit.health_profile.test_health_report_source."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError


class FakeReportCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.indexes: list[tuple[list, dict]] = []

    async def create_index(self, fields, **options):
        self.indexes.append((deepcopy(fields), deepcopy(options)))
        return options['name']

    async def insert_one(self, document):
        if any(
            item.get('_id') == document.get('_id') or item.get('reportId') == document.get('reportId')
            for item in self.documents
        ):
            raise DuplicateKeyError('duplicate report')
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=True)

    async def find_one(self, filters):
        for document in self.documents:
            if all(document.get(key) == value for key, value in filters.items()):
                return deepcopy(document)
        return None

    def find(self, filters, projection=None):
        documents = [
            document
            for document in self.documents
            if document.get('profileIngestion', {}).get('status') == 'pending'
            and isinstance(document.get('reportId'), str)
        ]
        return FakeCursor(documents)

    async def update_one(self, filters, update):
        for document in self.documents:
            if not all(document.get(key) == value for key, value in filters.items()):
                continue
            for path, value in update.get('$set', {}).items():
                _set_path(document, path, deepcopy(value))
            return SimpleNamespace(acknowledged=True, matched_count=1)
        return SimpleNamespace(acknowledged=True, matched_count=0)


class FakeCursor:
    def __init__(self, documents):
        self.documents = [deepcopy(item) for item in documents]

    def sort(self, fields):
        return self

    async def to_list(self, length=None):
        return deepcopy(self.documents[:length])


def _set_path(document: dict, path: str, value) -> None:
    target = document
    parts = path.split('.')
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


class InMemoryJobRepository:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.jobs = {}

    async def create_job(self, job):
        if self.fail_create:
            raise RuntimeError('queue unavailable')
        existing = self.jobs.get(job.idempotency_key)
        if existing is not None:
            return existing, False
        self.jobs[job.idempotency_key] = job
        return job, True

    async def get_job_by_idempotency(self, user_id, idempotency_key):
        return self.jobs.get(idempotency_key)

    async def claim_next_job(self, **kwargs):
        raise AssertionError('本测试不消费任务')

    async def complete_job(self, **kwargs):
        raise AssertionError('本测试不完成任务')

    async def fail_job(self, **kwargs):
        raise AssertionError('本测试不处理失败任务')
