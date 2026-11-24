"""Exact legacy report status is pure and cannot cross source/subject bindings."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from bson import BSON
from bson.int64 import Int64

from health_profile.models import HealthProfileJob
from health_profile.models import ProfileIngestionSourceType
from modules.health_profile import ProfileLegacyIngestionStatusReader
from modules.health_profile.contracts import ProfileLegacyIngestionStatusV1
from modules.health_profile.contracts import ProfileScope

from .test_fact_pages import NOW
from .test_fact_pages import environment
from .test_fact_pages import stored


def add_job(flow, *, revision=917332201, status='pending'):
    options = {}
    if status == 'running':
        options.update(
            attempts=1, lease_owner='worker', lease_token='private-token', lease_expires_at=NOW + timedelta(minutes=1)
        )
    if status == 'retry_wait':
        options.update(next_retry_at=NOW + timedelta(minutes=1), error_code='private-provider-error')
    if status == 'dead_letter':
        options['error_code'] = 'private-provider-error'
    if status == 'completed':
        options['completed_at'] = NOW
    job = HealthProfileJob(
        job_id=f'job-{revision}',
        idempotency_key=f'report:canonical-report:{revision}',
        idempotency_fingerprint='private-fingerprint',
        source_type='health_report',
        source_id='canonical-report',
        source_revision=revision,
        user_id='u',
        subject_id='self-u',
        status=status,
        created_at=NOW,
        updated_at=NOW,
        **options,
    )
    flow.collections['jobs'].documents.append(job.to_document(document_id=job.job_id))
    return job


def reader(flow):
    return ProfileLegacyIngestionStatusReader(flow.repository, clock=lambda: NOW)


async def read(flow, **kwargs):
    options = {'canonical_report_id': 'canonical-report', 'legacy_source_revision': 917332201, **kwargs}
    return await reader(flow).read_health_report(flow.scope, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'running', 'retry_wait', 'completed', 'dead_letter'])
async def test_exact_original_job_status_is_projected_without_private_fields_or_writes(status):
    flow = environment()
    add_job(flow, status=status)
    add_job(flow, revision=1, status='completed')
    before = stored(flow)
    result = await read(flow)
    assert isinstance(result, ProfileLegacyIngestionStatusV1)
    assert result.status == 'ready' and result.ingestion_status == status and result.job_present
    assert result.legacy_source_revision == 917332201
    assert 'private' not in result.model_dump_json() and 'jobId' not in result.model_dump_json(by_alias=True)
    assert stored(flow) == before
    assert (await read(flow, legacy_source_revision=1)).ingestion_status == 'completed'


@pytest.mark.asyncio
async def test_missing_job_for_active_subject_preserves_pending_without_creating_it():
    flow = environment()
    before = stored(flow)
    result = await read(flow)
    assert result.status == 'ready' and result.ingestion_status == 'pending' and not result.job_present
    assert stored(flow) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['absent', 'archived', 'foreign'])
async def test_missing_or_wrong_subject_never_queries_jobs_or_repairs_subject(state):
    flow = environment()
    if state == 'absent':
        flow.collections['subjects'].documents.clear()
    elif state == 'archived':
        flow.collections['subjects'].documents[0]['status'] = 'archived'
    else:
        flow.scope = ProfileScope(user_id='other', subject_id='self-u')
    flow.repository.get_job_by_idempotency = AsyncMock(side_effect=AssertionError('must not query another scope'))
    before = stored(flow)
    result = await read(flow)
    assert result.status == 'missing' and result.ingestion_status is None and not result.job_present
    assert stored(flow) == before
    flow.repository.get_job_by_idempotency.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'update',
    [
        {'subject_id': 'other-subject'},
        {'subject_id': None},
        {'user_id': 'other'},
        {'source_type': ProfileIngestionSourceType.CHAT},
        {'source_id': 'another-report'},
        {'source_revision': 1},
        {'idempotency_key': 'another-key'},
    ],
)
async def test_mismatching_stored_job_binding_does_not_expose_status(update):
    flow = environment()
    job = add_job(flow, status='completed').model_copy(update=update)
    flow.repository.get_job_by_idempotency = AsyncMock(return_value=job)
    result = await read(flow)
    assert result.status == 'missing' and result.error_code == 'legacy_job_binding_mismatch'
    assert result.ingestion_status is None and not result.job_present
    flow.repository.get_job_by_idempotency.assert_awaited_once_with('u', 'report:canonical-report:917332201')


@pytest.mark.asyncio
async def test_real_repository_match_for_same_user_wrong_subject_is_not_accepted():
    flow = environment()
    add_job(flow, status='completed')
    flow.collections['jobs'].documents[0]['subjectId'] = 'other-subject'
    result = await read(flow)
    assert result.status == 'missing' and result.ingestion_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['get_subject', 'get_job_by_idempotency'])
async def test_storage_errors_are_unavailable_instead_of_pending(method):
    flow = environment()
    setattr(flow.repository, method, AsyncMock(side_effect=RuntimeError('private URI and clinical data')))
    result = await read(flow)
    assert result.status == 'unavailable' and result.ingestion_status is None
    assert result.error_code == 'legacy_ingestion_read_unavailable'
    assert 'private' not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'options',
    [
        {'canonical_report_id': ''},
        {'legacy_source_revision': 0},
        {'legacy_source_revision': True},
        {'legacy_source_revision': '917332201'},
        {'legacy_source_revision': 917332201.0},
    ],
)
async def test_invalid_identity_is_rejected_before_io(options):
    flow = environment()
    flow.repository.get_subject = AsyncMock()
    with pytest.raises(ValueError):
        await read(flow, **options)
    flow.repository.get_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_factory_exposes_reader_without_constructor_side_effects(monkeypatch):
    from health_profile import application

    from .test_profile_read_and_subjects import FakeCollection

    flow = environment()
    mapping = {
        'collection_health_subjects': 'subjects',
        'collection_health_profile_facts': 'facts',
        'collection_health_profile_events': 'events',
        'collection_health_profile_views': 'views',
        'collection_health_profile_jobs': 'jobs',
    }
    for public_name, local_name in mapping.items():
        monkeypatch.setattr(application, public_name, flow.collections[local_name])
    for name in (
        'collection_health_profile_source_publications',
        'collection_health_reports',
        'collection_health_report_revisions',
    ):
        monkeypatch.setattr(application, name, FakeCollection())
    before = stored(flow)
    services = application.create_application_profile_public_contracts()
    assert isinstance(services.legacy_ingestion, ProfileLegacyIngestionStatusReader)
    result = await services.legacy_ingestion.read_health_report(
        flow.scope,
        canonical_report_id='canonical-report',
        legacy_source_revision=917332201,
    )
    assert result.status == 'ready' and result.ingestion_status == 'pending'
    assert stored(flow) == before


@pytest.mark.asyncio
async def test_bson_int64_legacy_hash_revision_is_normalized_and_reads_the_exact_job():
    flow = environment()
    revision = BSON.encode({'revision': 2 + int('abcdef123456', 16)}).decode()['revision']
    assert isinstance(revision, Int64)
    add_job(flow, revision=revision, status='completed')
    result = await read(flow, legacy_source_revision=revision)
    assert result.status == 'ready' and result.ingestion_status == 'completed' and result.job_present
    assert result.legacy_source_revision == revision and type(result.legacy_source_revision) is int
