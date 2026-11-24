"""Compose the actual Document and Profile providers with offline storage."""

from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

from bson import BSON

from adapters.analysis.basic_info import create_document_basic_info_prepared_service
from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from adapters.analysis.basic_info import create_document_basic_info_source_readers
from adapters.analysis.publication import MongoDocumentPublicationLedger
from health_profile import application as profile_application
from health_profile.models import SubjectStatus
from health_profile.repository import MongoHealthProfileRepository
from health_profile.service import HealthProfileService
from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.application import ProfileCurrentDocumentSourceResolver
from modules.health_profile.application import create_application_profile_public_contracts
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import actual_analysis_preparation
from tests.unit._support.current_document import bson_collection
from tests.unit._support.current_document import environment
from tests.unit._support.document_bson import LedgerBsonCollection
from tests.unit._support.profile_mongo import BsonCollection


class ProfileBsonCollection(BsonCollection):
    """Persist the same BSON UTC/millisecond shape as the real Mongo driver."""

    async def insert_one(self, document):
        return await super().insert_one(BSON.encode(document).decode())

    async def find_one(self, query):
        return await super().find_one(BSON.encode(query).decode())

    def find(self, query):
        return super().find(BSON.encode(query).decode())

    async def find_one_and_replace(self, query, replacement, **kwargs):
        return await super().find_one_and_replace(
            BSON.encode(query).decode(), BSON.encode(replacement).decode(), **kwargs
        )

    async def find_one_and_update(self, query, update, **kwargs):
        return await super().find_one_and_update(BSON.encode(query).decode(), BSON.encode(update).decode(), **kwargs)


async def profile_scope(monkeypatch):
    names = ('subjects', 'facts', 'events', 'views', 'jobs', 'basic_info', 'publications', 'reports', 'revisions')
    cols = {name: ProfileBsonCollection() for name in names}
    for name, field in {
        'subjects': 'collection_health_subjects',
        'facts': 'collection_health_profile_facts',
        'events': 'collection_health_profile_events',
        'views': 'collection_health_profile_views',
        'jobs': 'collection_health_profile_jobs',
        'publications': 'collection_health_profile_source_publications',
        'reports': 'collection_health_reports',
        'revisions': 'collection_health_report_revisions',
    }.items():
        monkeypatch.setattr(profile_application, field, cols[name])
    repository = MongoHealthProfileRepository(**{k: cols[k] for k in names[:7]})
    bootstrap = await HealthProfileService(repository).ensure_self_profile('synthetic-user')
    return cols, repository, DocumentScope(bootstrap.subject.user_id, bootstrap.subject.subject_id)


async def composition(monkeypatch, *, status='complete', edit=None):
    cols, _, scope = await profile_scope(monkeypatch)

    def clinical(raw):
        raw['reportType'] = 'lab'
        raw['observations'] = [{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L'}]
        if edit:
            edit(raw)

    env = await environment(status=status, edit=clinical, scope=scope)
    records, preparations = bson_collection(), bson_collection()
    ledger = MongoDocumentPublicationLedger(sources=LedgerBsonCollection(), intents=LedgerBsonCollection())
    prepared = create_document_basic_info_prepared_service(understanding=env.api, preparation_records=preparations)
    original = await prepared.remember(replace(env.bundle, receipt=env.original.receipt))
    readers = create_document_basic_info_source_readers(prepared=prepared, ledger=ledger, admission_records=records)
    now = [datetime(2026, 11, 13, 12, tzinfo=UTC)]
    flow = SimpleNamespace(
        cols=cols,
        scope=scope,
        env=env,
        prepared=prepared,
        original=original,
        preparations=preparations,
        records=records,
        ledger=ledger,
        readers=readers,
        now=now,
    )
    install_profile(flow)
    info = await flow.profile.basic_info.read(scope.user_id, subject_id=scope.subject_id)
    flow.epoch = info.lifecycle_epoch
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=readers, profile_inbox=flow.profile.inbox, clock=lambda: now[0]
    )
    flow.plan_ref = await flow.runtime.admission.prepare_source_plan(
        scope=scope, prepared=original, request_id='first-source', lifecycle_epoch=flow.epoch
    )
    flow.capture = await flow.runtime.source.capture_source(scope=scope, verified_plan_ref=flow.plan_ref)
    return flow


def install_profile(flow):
    resolver = ProfileCurrentDocumentSourceResolver(flow.readers.profile)
    flow.profile = create_application_profile_public_contracts(
        resolvers=resolver.registrations(), basic_info_collection=flow.cols['basic_info']
    )
    flow.profile.inbox.clock = lambda: flow.now[0]
    flow.profile.basic_info.clock = lambda: flow.now[0]
    flow.repository = flow.profile.inbox.repository
    assert flow.profile.inbox.current_document_source is resolver


async def info(flow):
    return await flow.profile.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id)


async def clinical_facts(flow):
    snapshot = await flow.profile.reader.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    return tuple(row for row in snapshot.facts if row.category != 'demographic')


async def publish(flow):
    receipt = await flow.runtime.publisher.publish(
        scope=flow.scope,
        snapshot_id=flow.capture.source_snapshot_ref,
        request_id='publication-first',
        expected_prior_revision=None,
    )
    assert receipt.state == 'received'
    await settle(flow)
    return receipt


async def settle(flow):
    await flow.profile.worker.recover_pending()
    flow.now[0] += timedelta(seconds=60)
    return await flow.runtime.recovery.recover_pending()


async def cycle(flow):
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        await flow.repository.update_subject(
            subject.model_copy(update={'status': status, 'version': subject.version + 1, 'updated_at': flow.now[0]}),
            expected_version=subject.version,
        )


def managed(flow, *, understanding=None, retire=(), request='managed', prior=1, source=None):
    return DocumentCurrentManagedCommand(
        flow.scope,
        flow.env.bundle.material.source_id,
        request,
        flow.epoch,
        prior,
        source or flow.capture.source_snapshot_ref,
        understanding_snapshot_ref=understanding,
        retire_contribution_ids=tuple(retire),
    )


async def actual_analysis_composition(monkeypatch, replies, *, text=REPORT_TEXT, native_content=None):
    cols, repository, scope = await profile_scope(monkeypatch)
    flow = await actual_analysis_preparation(
        replies,
        scope=scope,
        text=text,
        native_content=native_content,
        health_profile_service=HealthProfileService(repository),
    )
    flow.cols = cols
    flow.ledger = MongoDocumentPublicationLedger(sources=LedgerBsonCollection(), intents=LedgerBsonCollection())
    flow.readers = create_document_basic_info_source_readers(
        prepared=flow.prepared, ledger=flow.ledger, admission_records=bson_collection()
    )
    flow.now = [datetime(2026, 11, 13, 12, tzinfo=UTC)]
    install_profile(flow)
    flow.epoch = (await info(flow)).lifecycle_epoch
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )
    return flow
