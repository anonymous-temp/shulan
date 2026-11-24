"""Shared synthetic fixture closure extracted from tests.unit.health_profile.test_public_contracts."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceCutoverAuthorizationV1
from health_profile.repository import MongoHealthProfileRepository
from health_profile.service import HealthProfileService
from health_profile.snapshots import ProfileSnapshotReader
from health_profile.source_application import ProfilePublicationWorker
from health_profile.source_application import ProfileSourceApplier
from health_profile.source_material import LegacyHealthReportSnapshotResolver
from health_profile.source_material import ProfileRetractionSnapshotResolver
from health_profile.source_publications import MongoProfilePublicationStore
from health_profile.source_publications import ProfileSourceInbox
from tests.unit._support.document_profile_composition.profile.profile_read_and_subjects import FakeCollection


async def setup_flow(*, authorize=True):
    now = [datetime(2026, 11, 9, tzinfo=UTC)]
    collections = {name: FakeCollection() for name in ('subjects', 'facts', 'events', 'views', 'jobs', 'publications')}
    repository = MongoHealthProfileRepository(**collections)
    service = HealthProfileService(repository, clock=lambda: now[0])
    bootstrap = await service.ensure_self_profile('u')
    scope = ProfileScope(user_id='u', subject_id=bootstrap.subject.subject_id)
    source_records = {}

    class Source:
        async def get_owned_revision(self, report_id, source_revision, *, user_id, subject_id):
            record = source_records.get((report_id, source_revision))
            if record and (record.user_id != user_id or record.subject_id != subject_id):
                raise PermissionError('foreign source')
            return record

    inbox = ProfileSourceInbox(
        MongoProfilePublicationStore(collections['publications']),
        repository=repository,
        clock=lambda: now[0],
    )
    authorization = ProfileSourceCutoverAuthorizationV1(
        scope=scope,
        source_type='health_report',
        source_id='report-1',
        authorization_id='offline-cutover-1',
        legacy_publishers_quiesced=True,
        legacy_workers_quiesced=True,
        authorized_at=now[0],
    )
    if authorize:
        await inbox.authorize_cutover(authorization, scope=scope)
    applier = ProfileSourceApplier(
        inbox,
        resolvers={
            ('health_report', 'health-report-observations.v1'): LegacyHealthReportSnapshotResolver(Source()),
            ('profile', 'profile-source-retraction.v1'): ProfileRetractionSnapshotResolver(),
        },
    )
    return SimpleNamespace(
        now=now,
        collections=collections,
        repository=repository,
        scope=scope,
        source_records=source_records,
        inbox=inbox,
        applier=applier,
        authorization=authorization,
        worker=ProfilePublicationWorker(inbox, applier),
        reader=ProfileSnapshotReader(repository, publications=inbox, clock=lambda: now[0]),
    )
