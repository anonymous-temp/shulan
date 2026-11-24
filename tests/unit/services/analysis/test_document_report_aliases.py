"""Durable report-ID ownership without clinical placeholder records."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.document_index import MongoDocumentIndexStore
from modules.health_profile.application import HealthReportSourceConflictError
from tests.unit.services.analysis.test_document_reuse import AtomicCollection


async def document(store, identity, *, user_id='user-1', subject_id='subject-1'):
    return await store.resolve(
        {
            '_id': identity,
            'userId': user_id,
            'subjectId': subject_id,
            'contentSha256': identity,
            'byteSize': 10,
            'status': 'pending',
        }
    )


@pytest.mark.asyncio
async def test_alias_binding_is_idempotent_without_a_lease_or_clinical_result():
    collection = AtomicCollection()
    store = MongoDocumentIndexStore(collection)
    first = await document(store, 'document-1')
    await store.bind_report_id(first, 'report-1')
    await store.bind_report_id(first, 'report-1')
    await store.bind_report_id(first, 'report-2')
    assert collection.documents['document-1']['reportAliases'] == ['report-1', 'report-2']
    assert collection.documents['document-1']['status'] == 'pending'
    assert not any(key in collection.documents['document-1'] for key in ('result', 'sourceRevision', 'leaseToken'))
    assert (
        [('reportAliases', 1)],
        {'unique': True, 'sparse': True, 'name': 'document_report_alias_unique'},
    ) in collection.indexes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'user_id,subject_id', [('user-1', 'subject-1'), ('user-2', 'subject-1'), ('user-1', 'subject-2')]
)
async def test_alias_cannot_be_reassigned_to_another_document_or_owner(user_id, subject_id):
    collection = AtomicCollection()
    store = MongoDocumentIndexStore(collection)
    first = await document(store, 'document-1')
    other = await document(store, 'document-2', user_id=user_id, subject_id=subject_id)
    await store.bind_report_id(first, 'report-1')
    with pytest.raises(HealthReportSourceConflictError):
        await store.bind_report_id(other, 'report-1')
    assert collection.documents['document-1']['reportAliases'] == ['report-1']
    assert 'reportAliases' not in collection.documents['document-2']


@pytest.mark.asyncio
async def test_independent_store_instances_contend_atomically_for_one_alias():
    collection = AtomicCollection()
    first_store, second_store = MongoDocumentIndexStore(collection), MongoDocumentIndexStore(collection)
    first, second = await document(first_store, 'document-1'), await document(second_store, 'document-2')
    results = await asyncio.gather(
        first_store.bind_report_id(first, 'shared-report'),
        second_store.bind_report_id(second, 'shared-report'),
        return_exceptions=True,
    )
    assert sum(isinstance(result, HealthReportSourceConflictError) for result in results) == 1
    assert results.count(None) == 1
    assert sum('shared-report' in item.get('reportAliases', []) for item in collection.documents.values()) == 1


@pytest.mark.asyncio
async def test_alias_binding_requires_the_exact_document_owner():
    collection = AtomicCollection()
    store = MongoDocumentIndexStore(collection)
    first = await document(store, 'document-1')
    with pytest.raises(RuntimeError, match='未可靠绑定'):
        await store.bind_report_id({**first, 'userId': 'wrong-owner'}, 'report-1')
    assert 'reportAliases' not in collection.documents['document-1']


@pytest.mark.asyncio
async def test_unacknowledged_alias_write_is_a_storage_error():
    collection = AtomicCollection()
    store = MongoDocumentIndexStore(collection)
    first = await document(store, 'document-1')
    collection.update_one = AsyncMock(return_value=SimpleNamespace(acknowledged=False, matched_count=1))
    with pytest.raises(RuntimeError, match='未可靠绑定'):
        await store.bind_report_id(first, 'report-1')
