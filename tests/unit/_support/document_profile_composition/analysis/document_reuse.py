"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_reuse."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymongo.errors import DuplicateKeyError

from adapters.analysis.document_index import MongoDocumentIndexStore
from modules.analysis.application.document_reuse import DocumentReuseCoordinator
from tests.unit._support.document_profile_composition.analysis.document_provenance import build_flow
from tests.unit._support.document_profile_composition.analysis.fakes import FakeDocumentCollection


class AtomicCollection(FakeDocumentCollection):
    """In-memory atomic collection implementing the exact predicates used by the Mongo adapter."""

    def matches(self, item, filters):
        for key, value in filters.items():
            if key == '$or':
                if not any(self.matches(item, branch) for branch in value):
                    return False
                continue
            actual = item.get(key)
            if isinstance(value, dict):
                for operator, operand in value.items():
                    if operator == '$in' and actual not in operand:
                        return False
                    if operator == '$gt' and (actual is None or actual <= operand):
                        return False
                    if operator == '$lte' and (actual is None or actual > operand):
                        return False
            elif actual != value:
                return False
        return True

    async def find_one(self, filters):
        return next((deepcopy(item) for item in self.documents.values() if self.matches(item, filters)), None)

    async def insert_one(self, document):
        for item in self.documents.values():
            if item['_id'] == document['_id'] or all(
                item.get(key) == document.get(key) for key in ('userId', 'subjectId', 'contentSha256')
            ):
                raise DuplicateKeyError('duplicate identity')
        return await super().insert_one(document)

    async def find_one_and_update(self, filters, update, **kwargs):
        found = await self.find_one(filters)
        if found is None:
            return None
        await self.update_one(filters, update)
        return await self.find_one({'_id': found['_id']})

    async def update_one(self, filters, update):
        alias = update.get('$addToSet', {}).get('reportAliases')
        found = await self.find_one(filters)
        if alias is not None and found is not None:
            for item in self.documents.values():
                if item['_id'] != found['_id'] and alias in item.get('reportAliases', []):
                    raise DuplicateKeyError('duplicate report alias')
        return await super().update_one(filters, update)


def reuse_flow():
    flow = build_flow()
    flow.index_collection = AtomicCollection()
    flow.index = MongoDocumentIndexStore(flow.index_collection)
    flow.downloader = SimpleNamespace(download=AsyncMock(return_value=b'%PDF-1.7\nsynthetic'))
    flow.coordinator = DocumentReuseCoordinator(flow.service, flow.index, flow.downloader)
    flow.coordinator.POLL_SECONDS = 0.005
    flow.service.supports_document_reuse = True
    flow.service._document_reuse = flow.coordinator
    return flow
