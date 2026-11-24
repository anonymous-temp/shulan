"""Offline PRIMARY presence and archive boundaries, including persisted BSON values."""

import asyncio
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
from bson import BSON
from pymongo import ReadPreference

from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceEventV1
from health_profile.contracts import ProfileSourceSnapshotRefV1
from health_profile.contracts import profile_content_digest
from health_profile.report_source import HealthReportSourceConflictError
from health_profile.report_source import MongoHealthReportStore
from health_profile.repository import MongoHealthProfileRepository
from health_profile.source_admission import ProfileFreshSourceAdmission
from health_profile.source_admission_contracts import ProfileFreshSourceAuthorizationV1
from health_profile.source_admission_contracts import ProfileOwnerAdmissionFenceV1
from health_profile.source_archive import ProfileSourceMaterialArchive
from health_profile.source_presence import MongoProfileSourcePresenceReader
from health_profile.source_publications import MongoProfilePublicationStore
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import ProfileSourceInbox
from health_profile.source_publications import source_key
from tests.unit._support.document_profile_composition.profile.source_admission import Collection as Collection
from tests.unit._support.document_profile_composition.profile.source_admission import Cursor as Cursor
from tests.unit._support.document_profile_composition.profile.source_admission import matches as matches
from tests.unit._support.document_profile_composition.profile.source_admission import nested as nested
from tests.unit._support.document_profile_composition.profile.source_admission import project as project

NOW = datetime(2026, 11, 10, tzinfo=UTC)
SCOPE = ProfileScope(user_id='u', subject_id='self')


def setup():
    collections = {name: Collection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')}
    collections['subjects'].documents.append(
        {
            '_id': 'self',
            'userId': 'u',
            'subjectId': 'self',
            'status': 'active',
            'kind': 'self',
        }
    )
    reader = MongoProfileSourcePresenceReader(**collections, clock=lambda: NOW)
    return collections, reader


def read(reader, source_type='health_report', **kwargs):
    return asyncio.run(reader.read_legacy_presence(SCOPE, source_type=source_type, source_id='source', **kwargs))


@pytest.mark.parametrize('source_type', ['health_report', 'record', 'chat', 'questionnaire'])
def test_absence_is_bounded_primary_pure_and_time_independent(source_type):
    collections, reader = setup()
    first = read(reader, source_type)
    reader._clock = lambda: NOW.replace(hour=8)
    second = read(reader, source_type)
    assert first.status == second.status == 'absent'
    assert first.content_digest == second.content_digest
    assert first.read_at != second.read_at
    assert all(collection.preferences == [ReadPreference.PRIMARY] for collection in collections.values())
    assert all(not collection.writes for collection in collections.values())
    assert {section.kind for section in first.sections} == (
        {'reports', 'report_revisions', 'jobs', 'fact_contributions', 'publications'}
        if source_type == 'health_report'
        else {'jobs', 'fact_contributions', 'publications'}
    )


@pytest.mark.parametrize('status', ['pending', 'running', 'retry_wait', 'completed', 'failed', 'dead_letter'])
@pytest.mark.parametrize('subject_id', [None, 'self', 'relative'])
def test_all_job_history_blocks_fresh_including_null_anchor(status, subject_id):
    collections, reader = setup()
    collections['jobs'].documents.append(
        {
            '_id': 'job',
            'userId': 'u',
            'subjectId': subject_id,
            'sourceType': 'record',
            'sourceId': 'source',
            'status': status,
            'payload': {'secret': 'not returned'},
        }
    )
    result = read(reader, 'record')
    assert result.status == 'present'
    assert next(section for section in result.sections if section.kind == 'jobs').observed_count == 1
    assert 'secret' not in result.model_dump_json()


def test_canonical_alias_and_all_opaque_revisions_block_absence():
    collections, reader = setup()
    collections['reports'].documents.extend(
        [
            {'_id': 'source', 'reportId': 'source', 'canonicalReportId': 'source', 'userId': 'u', 'subjectId': 'self'},
            {'_id': 'alias', 'reportId': 'alias', 'canonicalReportId': 'source', 'userId': 'u', 'subjectId': 'self'},
        ]
    )
    collections['revisions'].documents.append(
        BSON(
            BSON.encode(
                {
                    '_id': 'alias:9000000000000',
                    'reportId': 'alias',
                    'sourceRevision': 9000000000000,
                    'userId': 'u',
                    'subjectId': 'self',
                    'profileIngestion': {'status': 'completed'},
                }
            )
        ).decode()
    )
    collections['jobs'].documents.append(
        {
            '_id': 'old',
            'userId': 'u',
            'sourceType': 'health_report',
            'sourceId': 'alias',
            'status': 'completed',
        }
    )
    result = read(reader)
    counts = {section.kind: section.observed_count for section in result.sections}
    assert result.status == 'present'
    assert counts['reports'] == 2 and counts['report_revisions'] == counts['jobs'] == 1


def test_fact_matches_same_source_element_and_all_target_subjects():
    collections, reader = setup()
    collections['facts'].documents.extend(
        [
            {'_id': 'foreign', 'userId': 'other', 'sources': [{'sourceType': 'chat', 'sourceId': 'source'}]},
            {
                '_id': 'split',
                'userId': 'u',
                'sources': [
                    {'sourceType': 'chat', 'sourceId': 'other'},
                    {'sourceType': 'record', 'sourceId': 'source'},
                ],
            },
        ]
    )
    assert read(reader, 'chat').status == 'absent'
    collections['facts'].documents.append(
        {
            '_id': 'family',
            'userId': 'u',
            'subjectId': 'relative',
            'status': 'rejected',
            'sources': [{'sourceType': 'chat', 'sourceId': 'source'}],
            'value': 'not selected',
        }
    )
    result = read(reader, 'chat')
    assert result.status == 'present'
    assert 'not selected' not in result.model_dump_json()


@pytest.mark.parametrize('kind', ['cursor', 'alias', 'publication', 'cutover'])
def test_existing_publication_intents_block_fresh(kind):
    collections, reader = setup()
    branch = {'sourceType': 'chat', 'sourceId': 'source'}
    row = {'_id': kind, 'recordKind': kind, 'scope': SCOPE.model_dump(by_alias=True)}
    if kind in {'alias', 'publication'}:
        row['event'] = branch
    elif kind == 'cutover':
        row['authorization'] = branch
    else:
        row.update(branch)
    collections['publications'].documents.append(row)
    assert read(reader, 'chat').status == 'present'


@pytest.mark.parametrize(
    'mutation,expected',
    [
        ('missing', 'missing'),
        ('inactive', 'missing'),
        ('foreign', 'missing'),
        ('read_error', 'unavailable'),
        ('ambiguous_history', 'unavailable'),
    ],
)
def test_missing_and_unknown_are_never_absent(mutation, expected):
    collections, reader = setup()
    if mutation == 'missing':
        collections['subjects'].documents.clear()
    elif mutation == 'inactive':
        collections['subjects'].documents[0]['status'] = 'inactive'
    elif mutation == 'foreign':
        collections['subjects'].documents[0]['userId'] = 'other'
    elif mutation == 'read_error':
        collections['jobs'].fail = True
    else:
        collections['reports'].documents.append(
            {
                '_id': 'source',
                'reportId': 'source',
                'userId': 'other',
                'subjectId': 'self',
            }
        )
    assert read(reader).status == expected
    assert all(not item.writes for item in collections.values())


def test_limit_is_explicit_unknown_and_never_false_absence():
    collections, reader = setup()
    collections['jobs'].documents.extend(
        [{'_id': str(index), 'userId': 'u', 'sourceType': 'chat', 'sourceId': 'source'} for index in range(3)]
    )
    result = read(reader, 'chat', limit=2)
    assert result.status == 'unavailable' and result.error_code == 'source.presence_limit_exceeded'
    assert next(section for section in result.sections if section.kind == 'jobs').truncated


def archive_setup(*, ready=True):
    collections, _ = setup()
    marker = {
        '_id': 'cutover_' + source_key(SCOPE, 'health_report', 'source'),
        'recordKind': 'cutover',
        'revision': 0,
        'status': 'ready' if ready else 'blocked',
        'scope': SCOPE.model_dump(by_alias=True),
        'authorization': {'sourceType': 'health_report', 'sourceId': 'source'},
    }
    collections['publications'].documents.append(marker)
    archive = ProfileSourceMaterialArchive(
        report_store=MongoHealthReportStore(collections['reports'], collections['revisions']),
        publication_store=MongoProfilePublicationStore(collections['publications']),
        clock=lambda: NOW,
    )
    arguments = dict(
        scope=SCOPE,
        canonical_report_id='source',
        report_id='source',
        source_fingerprint='fingerprint',
        document_id='document',
        result={'reportType': 'lab', 'observations': [{'itemName': 'ALT', 'resultValue': '12'}]},
    )
    return collections, archive, arguments


def test_archive_real_base_and_opaque_history_idempotently_without_any_delivery():
    async def run():
        collections, archive, arguments = archive_setup()
        first = await archive.archive_source_material(**arguments)
        again = await archive.archive_source_material(**arguments)
        revision = await archive.archive_source_material(**arguments, source_key_value='true-v16-key')
        replay = await archive.archive_source_material(**arguments, source_key_value='true-v16-key')
        assert first.created and not again.created
        assert first.legacy_source_revision == 1
        assert revision.created and not replay.created and revision.legacy_source_revision > 2**31
        assert revision.content_digest == replay.content_digest
        assert not collections['jobs'].documents and not collections['facts'].documents
        for row in collections['reports'].documents + collections['revisions'].documents:
            assert row['profileIngestion']['status'] == 'source_managed'
            assert row['profileIngestion']['jobId'] is None
            assert 'ingestionObservations' not in row
        saved = await archive._reports.get_revision('source', revision.legacy_source_revision)
        assert saved.result == arguments['result'] and saved.document_id == 'document'

    asyncio.run(run())


def test_archive_requires_ready_fence_and_does_not_enqueue():
    collections, archive, arguments = archive_setup(ready=False)
    with pytest.raises(HealthReportSourceConflictError, match='ready'):
        asyncio.run(archive.archive_source_material(**arguments))
    assert not collections['reports'].documents and not collections['revisions'].documents


@pytest.mark.parametrize('changed', ['result', 'fingerprint', 'document', 'scope'])
def test_archive_ack_retry_never_silently_replaces_frozen_payload(changed):
    async def run():
        collections, archive, arguments = archive_setup()
        await archive.archive_source_material(**arguments)
        if changed == 'result':
            arguments['result']['observations'][0]['resultValue'] = '99'
        elif changed == 'fingerprint':
            arguments['source_fingerprint'] = 'different'
        elif changed == 'document':
            arguments['document_id'] = 'different'
        else:
            arguments['scope'] = ProfileScope(user_id='other', subject_id='self')
        with pytest.raises(HealthReportSourceConflictError):
            await archive.archive_source_material(**arguments)
        assert len(collections['reports'].documents) == 1
        assert collections['reports'].documents[0]['observations'][0]['resultValue'] == '12'

    asyncio.run(run())


def test_archive_does_not_adopt_legacy_pending_or_create_supplement_before_canonical():
    async def run():
        collections, archive, arguments = archive_setup()
        with pytest.raises(HealthReportSourceConflictError, match='canonical'):
            await archive.archive_source_material(**arguments, source_key_value='v16')
        await archive.archive_source_material(**arguments)
        collections['reports'].documents[0]['profileIngestion']['status'] = 'pending'
        with pytest.raises(HealthReportSourceConflictError, match='convert'):
            await archive.archive_source_material(**arguments)
        assert collections['reports'].documents[0]['profileIngestion']['status'] == 'pending'

    asyncio.run(run())


class Owner:
    def __init__(self, scope=SCOPE, source_type='record', source_id='source'):
        self.proof = ProfileOwnerAdmissionFenceV1(
            scope=scope,
            source_type=source_type,
            source_id=source_id,
            fence_id='owner-fence',
            fence_generation=1,
            prepared_manifest_ref='fixed-publish-false-draft',
            formal_history_absent=True,
            legacy_intents_absent=True,
            active_legacy_writers=0,
            active_legacy_workers=0,
            legacy_admission_blocked=True,
            issued_at=NOW,
        ).model_dump(mode='json', by_alias=True)
        self.held = True
        self.reads = 0

    async def read_admission_fence(self, **kwargs):
        self.reads += 1
        return deepcopy(self.proof)

    async def is_admission_fence_held(self, **kwargs):
        return self.held


async def admission_setup(source_type='record'):
    collections, reader = setup()
    collections['subjects'].documents[0].update(createdAt=NOW, updatedAt=NOW, version=1)
    repository = MongoHealthProfileRepository(
        subjects=collections['subjects'],
        facts=collections['facts'],
        jobs=collections['jobs'],
        publications=collections['publications'],
        events=Collection(),
        views=Collection(),
    )
    inbox = ProfileSourceInbox(
        MongoProfilePublicationStore(collections['publications']), repository=repository, clock=lambda: NOW
    )
    owner = Owner(source_type=source_type)
    admission = ProfileFreshSourceAdmission(
        inbox=inbox, presence_reader=reader, owner_fence_readers={source_type: owner}
    )
    presence = await reader.read_legacy_presence(SCOPE, source_type=source_type, source_id='source')
    contract = {
        'record': 'profile.record-source.v2',
        'chat': 'profile.chat-source.v2',
        'questionnaire': 'profile.questionnaire-source.v1',
    }[source_type]
    auth = ProfileFreshSourceAuthorizationV1(
        scope=SCOPE,
        source_type=source_type,
        source_id='source',
        authorization_id='auth',
        owner_fence_ref=ProfileSourceSnapshotRefV1(
            provider=source_type,
            contract_version='profile-owner-admission-fence.v1',
            snapshot_id='fence-proof',
        ),
        owner_fence_digest=profile_content_digest(owner.proof),
        presence_proof_ref=presence.proof_ref,
        presence_proof_digest=presence.content_digest,
        material_contract_version=contract,
        material_snapshot_ref=ProfileSourceSnapshotRefV1(
            provider=source_type, contract_version=contract, snapshot_id='frozen-raw-snapshot'
        ),
        material_content_digest='a' * 64,
        authorized_at=NOW,
    )
    return SimpleNamespace(
        collections=collections, reader=reader, inbox=inbox, owner=owner, admission=admission, auth=auth
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('source_type', ['record', 'chat', 'questionnaire'])
async def test_reservation_blocks_old_admission_but_requires_explicit_ready(source_type):
    from health_profile.ingestion import HealthProfileJobConflictError
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.models import ProfileIngestionSourceType

    flow = await admission_setup(source_type)
    reserved = await flow.admission.reserve_fresh_source(flow.auth, scope=SCOPE)
    assert reserved.status == 'blocked'
    assert 'legacyPublishersQuiesced' not in reserved.model_dump_json()
    queue = HealthProfileJobQueue(flow.inbox.repository, clock=lambda: NOW)
    with pytest.raises(HealthProfileJobConflictError, match='fenced'):
        await queue.enqueue(
            source_type=ProfileIngestionSourceType(source_type),
            source_id='source',
            source_revision=1,
            user_id='u',
            subject_id=None,
        )
    event = ProfileSourceEventV1(
        event_id='first',
        scope=SCOPE,
        source_type=source_type,
        source_id='source',
        source_revision=1,
        publication_revision=1,
        event_kind='publish',
        occurred_at=NOW,
        content_digest='a' * 64,
        snapshot_ref=flow.auth.material_snapshot_ref,
    )
    with pytest.raises(ProfileSourceConflictError, match='ready'):
        await flow.inbox.receive(event, scope=SCOPE)
    ready = await flow.admission.authorize_fresh_source(flow.auth, scope=SCOPE)
    assert ready.status == 'ready' and ready.presence_proof.status == 'absent'
    receipt = await flow.inbox.receive(event, scope=SCOPE)
    assert receipt.status == 'received'
    # After first formal publication or archive, retry refers to saved admission.
    flow.owner.held = False
    flow.reader._collections['jobs'].fail = True
    replay = await flow.admission.authorize_fresh_source(flow.auth, scope=SCOPE)
    assert replay == ready
    assert (await flow.inbox.get_cutover(SCOPE, source_type, 'source')).status == 'ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner_digest', 'owner_scope', 'not_held', 'history', 'proof_digest'])
async def test_unproved_fresh_admission_never_creates_marker(change):
    flow = await admission_setup()
    if change == 'owner_digest':
        flow.owner.proof['preparedManifestRef'] = 'changed'
    elif change == 'owner_scope':
        flow.owner.proof['scope']['userId'] = 'other'
    elif change == 'not_held':
        flow.owner.held = False
    elif change == 'history':
        flow.collections['jobs'].documents.append(
            {
                '_id': 'old',
                'userId': 'u',
                'sourceType': 'record',
                'sourceId': 'source',
                'status': 'completed',
            }
        )
    else:
        flow.auth = flow.auth.model_copy(update={'presence_proof_digest': 'b' * 64})
    with pytest.raises(ProfileSourceConflictError):
        await flow.admission.reserve_fresh_source(flow.auth, scope=SCOPE)
    assert not flow.collections['publications'].documents


@pytest.mark.asyncio
async def test_legacy_race_after_initial_scan_keeps_observable_blocked_fence():
    flow = await admission_setup()
    original_insert = flow.inbox.store.insert

    async def insert(row):
        result = await original_insert(row)
        flow.collections['jobs'].documents.append(
            {
                '_id': 'late-old-job',
                'userId': 'u',
                'sourceType': 'record',
                'sourceId': 'source',
                'status': 'pending',
            }
        )
        return result

    flow.inbox.store.insert = insert
    with pytest.raises(ProfileSourceConflictError, match='absence'):
        await flow.admission.authorize_fresh_source(flow.auth, scope=SCOPE)
    row = flow.collections['publications'].documents[0]
    assert row['status'] == 'blocked'
    assert await flow.inbox.repository.is_source_publication_managed('u', None, 'record', 'source')
    assert not flow.collections['facts'].documents


@pytest.mark.asyncio
async def test_lost_reservation_ack_resumes_same_proof_and_rejects_changed_authorization():
    flow = await admission_setup()
    first = await flow.admission.reserve_fresh_source(flow.auth, scope=SCOPE)
    assert await flow.admission.reserve_fresh_source(flow.auth, scope=SCOPE) == first
    changed = flow.auth.model_copy(update={'authorization_id': 'different'})
    with pytest.raises(ProfileSourceConflictError, match='authorization_conflict'):
        await flow.admission.reserve_fresh_source(changed, scope=SCOPE)
    assert len(flow.collections['publications'].documents) == 1
    assert (await flow.admission.authorize_fresh_source(flow.auth, scope=SCOPE)).status == 'ready'


@pytest.mark.asyncio
async def test_fresh_first_material_is_bound_before_any_publication():
    flow = await admission_setup('chat')
    await flow.admission.authorize_fresh_source(flow.auth, scope=SCOPE)
    event = ProfileSourceEventV1(
        event_id='first',
        scope=SCOPE,
        source_type='chat',
        source_id='source',
        source_revision=1,
        publication_revision=1,
        event_kind='publish',
        occurred_at=NOW,
        content_digest='b' * 64,
        snapshot_ref=flow.auth.material_snapshot_ref,
    )
    with pytest.raises(ProfileSourceConflictError, match='initial_material'):
        await flow.inbox.receive(event, scope=SCOPE)
    assert len(flow.collections['publications'].documents) == 1


def test_public_factory_construction_performs_no_reads_or_writes(monkeypatch):
    import health_profile.application as application
    from modules.health_profile.application import create_application_profile_source_admission

    collections, _ = setup()
    mappings = {
        'collection_health_subjects': 'subjects',
        'collection_health_reports': 'reports',
        'collection_health_report_revisions': 'revisions',
        'collection_health_profile_jobs': 'jobs',
        'collection_health_profile_facts': 'facts',
        'collection_health_profile_source_publications': 'publications',
    }
    for name, key in mappings.items():
        monkeypatch.setattr(application, name, collections[key])
    inbox = SimpleNamespace(store=MongoProfilePublicationStore(collections['publications']))
    bundle = create_application_profile_source_admission(inbox=inbox, owner_fence_readers={})
    assert bundle.admission.inbox is inbox
    assert all(not value.queries and not value.writes for value in collections.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['profile-source-event.v1', 'unknown.v99', {'unrecognized': True}])
async def test_record_owner_mode_cannot_fall_through_to_legacy_queue(mode):
    from health_profile.ingestion import HealthProfileJobQueue
    from health_profile.record_source import MongoRecordProfileStore
    from health_profile.record_source import RecordProfileSourceService

    from .test_record_profile_source import FakeSessionCollection
    from .test_record_profile_source import InMemoryJobRepository

    session = {
        'userId': 'u',
        'sessionId': 'record',
        'type': 'qa',
        'status': 'completed',
        'round': 1,
        'completedAt': NOW.isoformat(),
        'qaSourceRevision': 1,
        'qaProfileIngestion': {'status': 'source_managed', 'deliveryMode': mode},
    }
    store = MongoRecordProfileStore(FakeSessionCollection(session))
    jobs = InMemoryJobRepository()
    service = RecordProfileSourceService(store, HealthProfileJobQueue(jobs))
    result = await service.ensure_queued(user_id='u', session_id='record')
    assert result is not None and not result.ingestion_queued
    assert not jobs.jobs
    before = deepcopy(session)
    if mode == 'profile-source-event.v1':
        service.prepare_completed_snapshot(session)
        assert session == before
    else:
        with pytest.raises(ValueError, match='mode'):
            service.prepare_completed_snapshot(session)
        assert session == before


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['profile-source-event.v1', 'unknown.v99', {'unrecognized': True}])
async def test_chat_owner_mode_cannot_fall_through_to_legacy_queue(mode):
    from health_profile.chat_source import ChatProfileSourceService
    from health_profile.chat_source import MongoChatProfileStore
    from health_profile.ingestion import HealthProfileJobQueue

    from .test_chat_profile_source import FakeSessionCollection
    from .test_record_profile_source import InMemoryJobRepository

    message = {
        'messageId': 'message',
        'messageRevision': 1,
        'role': 'user',
        'content': ' unchanged ',
        'timestamp': NOW.isoformat(),
    }
    session = {
        'userId': 'u',
        'sessionId': 'chat',
        'type': 'chat',
        'messages': [message],
        'chatProfileIngestion': [
            {'messageId': 'message', 'sourceRevision': 1, 'status': 'pending', 'deliveryMode': mode}
        ],
    }
    store = MongoChatProfileStore(FakeSessionCollection(session))
    jobs = InMemoryJobRepository()
    service = ChatProfileSourceService(store, HealthProfileJobQueue(jobs))
    result = await service.ensure_queued(user_id='u', message_id='message')
    assert result is not None and not result.ingestion_queued and not jobs.jobs
    before = deepcopy(session)
    service.prepare_user_message(session, message)
    assert session == before


@pytest.mark.asyncio
async def test_legacy_scanners_skip_new_modes_before_limit_and_keep_legacy_compatibility():
    from health_profile.chat_source import MongoChatProfileStore
    from health_profile.record_source import MongoRecordProfileStore

    records = Collection(
        [
            {
                '_id': '0',
                'userId': 'u',
                'sessionId': 'new',
                'type': 'qa',
                'status': 'completed',
                'qaProfileIngestion': {'status': 'pending', 'deliveryMode': 'profile-source-event.v1'},
            },
            {
                '_id': '1',
                'userId': 'u',
                'sessionId': 'legacy',
                'type': 'qa',
                'status': 'completed',
                'qaProfileIngestion': {'status': 'pending', 'deliveryMode': 'legacy'},
            },
        ]
    )
    record_refs = await MongoRecordProfileStore(records).list_pending(limit=1)
    assert len(record_refs) == 1 and record_refs[0].session_id == 'legacy'
    chats = Collection(
        [
            {
                '_id': '0',
                'userId': 'u',
                'sessionId': 'new',
                'type': 'chat',
                'chatProfileIngestion': [{'messageId': 'new', 'status': 'pending', 'deliveryMode': 'future'}],
            },
            {
                '_id': '1',
                'userId': 'u',
                'sessionId': 'legacy',
                'type': 'chat',
                'chatProfileIngestion': [
                    {'messageId': 'skip', 'status': 'pending', 'deliveryMode': 'profile-source-event.v1'},
                    {'messageId': 'legacy', 'status': 'pending'},
                ],
            },
        ]
    )
    chat_refs = await MongoChatProfileStore(chats).list_pending(limit=1)
    assert len(chat_refs) == 1 and chat_refs[0].message_id == 'legacy'


async def fresh_document_setup():
    from health_profile.document_source_contracts import DocumentContributionV1

    from .test_document_source_contract import event_for
    from .test_document_source_contract import setup_document

    flow = await setup_document(authorize=False)
    collections, _ = setup()
    for name in ('subjects', 'facts', 'jobs', 'publications'):
        collections[name].documents = flow.collections[name].documents
    flow.presence_collections = collections
    reader = MongoProfileSourcePresenceReader(**collections, clock=lambda: NOW)
    presence = await reader.read_legacy_presence(flow.scope, source_type='health_report', source_id='report-1')
    owner = Owner(flow.scope, 'health_report', 'report-1')
    owner_ref = ProfileSourceSnapshotRefV1(
        provider='document',
        contract_version='profile-owner-admission-fence.v1',
        snapshot_id='document-owner-proof',
    )
    fence = {
        'admissionKind': 'fresh',
        'evidenceRef': 'document-verified-plan',
        'ownerFenceRef': owner_ref.model_dump(by_alias=True),
        'ownerFenceDigest': profile_content_digest(owner.proof),
        'profilePresenceProofRef': presence.proof_ref,
        'profilePresenceProofDigest': presence.content_digest,
    }
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    row = DocumentContributionV1.model_validate(raw['payload']['contributions'][0]).model_copy(
        update={'legacy_source_revision': None}
    )
    row = row.model_copy(
        update={'content_digest': profile_content_digest(row.canonical(scope=flow.scope, source_id='report-1'))}
    )
    raw['payload']['contributions'] = [row.model_dump(mode='json', by_alias=True)]
    inventory = flow.inventories['inventory-1']
    inventory['payload'].update(
        fence=fence,
        legacyState='absent',
        legacySourceRevision=None,
        contributions=[row.reference().model_dump(mode='json', by_alias=True)],
    )
    raw['payload']['coverage'].update(
        legacySourceRevision=None,
        orderedContributionIds=['base'],
        inventoryDigest=profile_content_digest(inventory),
    )
    flow.event = event_for(flow, raw)
    flow.fresh_auth = ProfileFreshSourceAuthorizationV1(
        scope=flow.scope,
        source_type='health_report',
        source_id='report-1',
        authorization_id='fresh-document',
        owner_fence_ref=owner_ref,
        owner_fence_digest=profile_content_digest(owner.proof),
        presence_proof_ref=presence.proof_ref,
        presence_proof_digest=presence.content_digest,
        material_contract_version=flow.event.snapshot_ref.contract_version,
        material_snapshot_ref=flow.event.snapshot_ref,
        material_content_digest=flow.event.content_digest,
        processing_revision=flow.resolver.VERSION,
        authorized_at=NOW,
    )
    flow.owner = owner
    flow.admission = ProfileFreshSourceAdmission(
        inbox=flow.inbox,
        presence_reader=reader,
        owner_fence_readers={'document': owner},
    )
    return flow


@pytest.mark.asyncio
async def test_document_fresh_archive_correct_and_retract_keep_material_revision_identity():
    from health_profile.document_source_contracts import DocumentContributionV1

    from .test_document_source_contract import complete_source
    from .test_document_source_contract import event_for
    from .test_document_source_contract import retract
    from .test_public_contracts import visible

    flow = await fresh_document_setup()
    ready = await flow.admission.authorize_fresh_source(flow.fresh_auth, scope=flow.scope)
    assert ready.status == 'ready'
    await flow.inbox.receive(flow.event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert len(await visible(flow)) == 1
    archive = ProfileSourceMaterialArchive(
        report_store=MongoHealthReportStore(
            flow.presence_collections['reports'], flow.presence_collections['revisions']
        ),
        publication_store=flow.inbox.store,
        clock=lambda: NOW,
    )
    kwargs = dict(
        scope=flow.scope,
        canonical_report_id='report-1',
        report_id='report-1',
        source_fingerprint='real-content-fingerprint',
        document_id='document-1',
        result={'reportType': 'lab', 'reportTime': '2026-11-01', 'observations': []},
    )
    await archive.archive_source_material(**kwargs)
    legacy = await archive.archive_source_material(**kwargs, source_key_value='real-v16-output-key')
    assert legacy.legacy_source_revision > 2**31
    corrected = complete_source(flow, value='20', legacy_value='9', revision=2)
    raw = deepcopy(flow.materials[corrected.snapshot_ref.snapshot_id])
    rows = []
    for value in raw['payload']['contributions']:
        row = DocumentContributionV1.model_validate(value).model_copy(
            update={
                'legacy_source_revision': legacy.legacy_source_revision
                if value['kind'] == 'legacy_report_source'
                else None
            }
        )
        row = row.model_copy(
            update={'content_digest': profile_content_digest(row.canonical(scope=flow.scope, source_id='report-1'))}
        )
        rows.append(row)
    raw['payload']['contributions'] = [row.model_dump(mode='json', by_alias=True) for row in rows]
    inv = flow.inventories['inventory-2']
    inv['payload'].update(
        fence=deepcopy(flow.inventories['inventory-1']['payload']['fence']),
        legacySourceRevision=legacy.legacy_source_revision,
        contributions=[row.reference().model_dump(mode='json', by_alias=True) for row in rows],
    )
    raw['payload']['coverage'].update(
        legacySourceRevision=legacy.legacy_source_revision, inventoryDigest=profile_content_digest(inv)
    )
    corrected = event_for(flow, raw, revision=2)
    assert corrected.source_revision == flow.event.source_revision == 1
    assert corrected.content_digest != flow.event.content_digest
    # Historical reads need the frozen evidence, not a currently empty legacy history.
    flow.owner.held = False
    assert (await flow.admission.authorize_fresh_source(flow.fresh_auth, scope=flow.scope)) == ready
    await flow.inbox.receive(corrected, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert len(await visible(flow)) == 2
    await flow.inbox.receive(retract(flow, 3), scope=flow.scope)
    assert await flow.worker.recover_pending() == 1 and not await visible(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['proof', 'owner', 'legacy_fence', 'material_version'])
async def test_document_ready_rejects_wrong_fresh_inventory_binding(changed):
    from health_profile.source_material import ProfileSourceApplicationError

    from .test_document_source_contract import event_for

    flow = await fresh_document_setup()
    raw = deepcopy(flow.materials[flow.event.snapshot_ref.snapshot_id])
    inv = flow.inventories['inventory-1']
    if changed == 'proof':
        inv['payload']['fence']['profilePresenceProofDigest'] = 'f' * 64
    elif changed == 'owner':
        inv['payload']['fence']['ownerFenceRef']['snapshotId'] = 'other'
    elif changed == 'legacy_fence':
        inv['payload']['fence'] = {
            'evidenceRef': 'old',
            'publishersQuiesced': True,
            'workersQuiesced': True,
            'legacyIntentsRetired': True,
        }
    else:
        raw['payload']['materialSnapshotRefs'][0]['sourceRevision'] = 2
    raw['payload']['coverage']['inventoryDigest'] = profile_content_digest(inv)
    event = event_for(flow, raw)
    auth = flow.fresh_auth.model_copy(
        update={'material_snapshot_ref': event.snapshot_ref, 'material_content_digest': event.content_digest}
    )
    with pytest.raises(ProfileSourceApplicationError):
        await flow.admission.authorize_fresh_source(auth, scope=flow.scope)
    assert (await flow.inbox.get_cutover(flow.scope, 'health_report', 'report-1')).status == 'blocked'
    assert not flow.collections['facts'].documents
