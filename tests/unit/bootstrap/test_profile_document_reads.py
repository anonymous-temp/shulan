"""Public Profile/Document composition preserves old observations with pure reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo import ReadPreference

from adapters.analysis.profile_ingestion import ProfileDocumentObservationIngestionReader
from bootstrap.document_contracts import build_document_observation_reader
from modules.analysis.observation_reader import DocumentObservationReadError
from modules.analysis.snapshots import DocumentScope
from modules.health_profile import ProfileLegacyIngestionStatusReader
from tests.unit.health_profile.test_fact_pages import environment
from tests.unit.health_profile.test_fact_pages import stored
from tests.unit.health_profile.test_legacy_ingestion_status import add_job
from tests.unit.services.analysis.test_document_observation_reader import Collection
from tests.unit.services.analysis.test_document_observation_reader import row


def setup(*, status=None):
    flow = environment()
    if status:
        add_job(flow, status=status, revision=7)
    scope = DocumentScope('u', 'self-u')
    collection = Collection([row(**scope.as_dict(), canonicalReportId='canonical-report')])
    reader = build_document_observation_reader(
        collection=collection, profile_legacy_ingestion_reader=ProfileLegacyIngestionStatusReader(flow.repository)
    )
    return flow, scope, collection, reader


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [None, 'pending', 'running', 'retry_wait', 'completed', 'dead_letter'])
async def test_actual_public_providers_preserve_status_without_creating_subject_job_or_indexes(status):
    flow, scope, collection, reader = setup(status=status)
    before = stored(flow)
    actual = await reader.read(scope=scope)
    assert actual.items[0]['ingestionStatus'] == (status or 'pending')
    assert actual.items[0]['sourceRevision'] == 7
    assert stored(flow) == before
    assert collection.options == {'read_preference': ReadPreference.PRIMARY}
    collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['foreign', 'unavailable'])
async def test_missing_ownership_or_failed_status_is_not_fabricated_as_pending(failure):
    flow, scope, collection, reader = setup(status='completed')
    if failure == 'foreign':
        flow.collections['jobs'].documents[0]['subjectId'] = 'someone-else'
    else:
        flow.repository.get_job_by_idempotency = AsyncMock(side_effect=OSError('offline storage error'))
    with pytest.raises(DocumentObservationReadError, match='unavailable'):
        await reader.read(scope=scope)
    collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change',
    [
        {'canonical_report_id': 'other'},
        {'legacy_source_revision': 8},
        {'contract_version': 'future.v2'},
        {'scope': {'user_id': 'other', 'subject_id': 'self-u'}},
    ],
)
async def test_consumer_boundary_rejects_foreign_or_changed_contract_even_if_provider_is_ready(change):
    flow, scope, collection, reader = setup(status='completed')
    real = ProfileLegacyIngestionStatusReader(flow.repository)
    result = await real.read_health_report(flow.scope, canonical_report_id='canonical-report', legacy_source_revision=7)
    fake = SimpleNamespace(read_health_report=AsyncMock(return_value=result.model_copy(update=change)))
    adapter = ProfileDocumentObservationIngestionReader(fake)
    with pytest.raises(DocumentObservationReadError, match='binding'):
        await adapter.read_status(scope=scope, source_id='canonical-report', source_revision=7)
