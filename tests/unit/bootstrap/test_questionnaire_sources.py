"""Questionnaire assembly uses public providers, fixed namespaces and one recovery owner."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from pymongo import AsyncMongoClient
from pymongo import ReadPreference

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from adapters.questionnaire_factory import build_questionnaire_service
from bootstrap.questionnaire_sources import build_questionnaire_source_delivery
from bootstrap.questionnaire_sources import build_questionnaire_source_resources
from health_profile.ingestion import HealthFactPolicy
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfileUserSourceResolver
from modules.questionnaire.submitted_material import QuestionnaireSubmittedMaterialReader
from tests.unit.health_profile.test_multi_source_publications import SELF_TEXT
from tests.unit.health_profile.test_multi_source_publications import candidate
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection
from tests.unit.questionnaire.source_fakes import Collection
from tests.unit.questionnaire.source_fakes import add_attachment
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import submission
from tests.unit.questionnaire.source_fakes import task
from tests.unit.questionnaire.test_factor_composition import composed as composed
from tests.unit.services.analysis.test_document_compatible_publication import environment as document_environment


class NamedCollection(Collection):
    def __init__(self, database, name):
        super().__init__()
        self.database, self.name = database, name


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = NamedCollection(self, name)
        return self.collections[name]


class NoIO:
    def __getattr__(self, name):
        raise AssertionError('construction accessed external provider: ' + name)


def test_both_stages_are_pure_and_keep_separate_permanent_namespaces():
    database = Database()
    group = build_questionnaire_source_resources(questionnaire_collection=database['custom_q'])
    assert set(database.collections) == {'custom_q', 'custom_q_profile_sources', 'custom_q_document_publications'}
    assert group.sources.store.collection is database['custom_q_profile_sources']
    assert group.sources.owner.collection is database['custom_q']
    assert group.snapshot_reader is group.sources.snapshot_reader
    assert group.fence_reader is group.sources.fence_reader
    provider = NoIO()
    router = build_questionnaire_source_delivery(
        group,
        inbox=provider,
        presence=provider,
        fresh_admission=provider,
        submitted_reader=provider,
        health_report_source=provider,
        publication_ledger=provider,
    )
    publication = router.publication
    publisher = publication.attachments.publisher
    assert publication.store is group.sources.store and publication.owner is group.sources.owner
    assert publisher.source_reader is group.snapshot_reader and publisher.submitted_reader is provider
    assert publisher.legacy_publication.source is provider and publisher.legacy_publication.ledger is provider
    assert publisher.records.collection is group.attachment_commands
    for name, collection in database.collections.items():
        assert not collection.documents
        assert all(value['read_preference'] == ReadPreference.PRIMARY for value in collection.options)
        if name != 'custom_q':
            assert all(value['write_concern'].document['w'] == 'majority' for value in collection.options)
    assert not hasattr(router, 'start')


@pytest.mark.asyncio
async def test_real_pymongo_handles_derive_same_database_without_connecting():
    client = AsyncMongoClient(connect=False)
    try:
        group = build_questionnaire_source_resources(questionnaire_collection=client['synthetic']['custom_q'])
        assert group.sources.store.collection.full_name == 'synthetic.custom_q_profile_sources'
        assert group.attachment_commands.full_name == 'synthetic.custom_q_document_publications'
        assert group.sources.owner.collection.full_name == 'synthetic.custom_q'
        assert group.sources.store.collection.database.client is client
        assert group.attachment_commands.read_preference == ReadPreference.PRIMARY
        assert group.attachment_commands.write_concern.document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
    finally:
        await client.close()


def test_missing_shared_document_gate_never_constructs_an_unguarded_delivery():
    group = build_questionnaire_source_resources(questionnaire_collection=Database()['q'])
    with pytest.raises(ValueError, match='shared Document publication ledger'):
        build_questionnaire_source_delivery(
            group,
            inbox=NoIO(),
            presence=NoIO(),
            fresh_admission=NoIO(),
            submitted_reader=NoIO(),
            health_report_source=NoIO(),
            publication_ledger=None,
        )


@pytest.mark.parametrize('explicit', [False, True])
def test_questionnaire_factory_keeps_legacy_delivery_and_guard_with_optional_new_router(composed, explicit):
    guard, router = AsyncMock(), Mock()
    options = {'profile_source_delivery': router} if explicit else {}
    service = build_questionnaire_service(
        runtime=composed.runtime,
        interpreter=object(),
        session_repository=object(),
        session_cache=None,
        task_supervisor=composed.supervisor,
        profile_source_guard=guard,
        **options,
    )
    assert service.profile_source_delivery is (router if explicit else None)
    assert service.profile_source_guard is guard
    assert service.profile_delivery == composed.profile.ensure_queued
    guard.assert_not_called()
    assert not router.mock_calls
    composed.profile.ensure_queued.assert_not_awaited()
    composed.runtime.llm_gateway.complete.assert_not_awaited()


async def composition():
    database = Database()
    source = database['custom_q']
    group = build_questionnaire_source_resources(questionnaire_collection=source)
    # The read-only registration resources exist before Profile is assembled.
    profile = await setup_flow(authorize=False)
    profile.now[0] += timedelta(days=1)
    now = [profile.now[0].timestamp()]
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: profile.now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=presence, owner_fence_readers={'questionnaire': group.fence_reader}
    )
    profile.applier.resolvers[('questionnaire', 'profile.questionnaire-source.v2')] = ProfileUserSourceResolver(
        group.snapshot_reader
    )
    runtime, gateway = _runtime({'candidates': [candidate(answer_index=1)]})
    profile.applier.extractor = DurableProfileSourceExtractor(runtime.hub, policy_version=HealthFactPolicy.VERSION)
    documents = document_environment()
    submitted = QuestionnaireSubmittedMaterialReader(store=MongoQuestionnaireStore(source), archive=AsyncMock())
    env = SimpleNamespace(
        group=group,
        resources=group.sources,
        source=source,
        now=now,
        profile=profile,
        presence=presence,
        fresh=fresh,
        documents=documents,
        submitted=submitted,
        raw=submission(label=SELF_TEXT),
        gateway=gateway,
    )
    add_attachment(env)
    restart(env)
    return env


def restart(env):
    env.router = build_questionnaire_source_delivery(
        env.group,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
        submitted_reader=env.submitted,
        health_report_source=env.documents.flow.source,
        publication_ledger=env.documents.ledger,
        clock=lambda: env.now[0],
    )
    env.service = env.router.publication
    env.publisher = env.service.attachments.publisher


@pytest.mark.asyncio
@pytest.mark.parametrize('queue_available', [False, True])
async def test_composed_public_providers_durably_accept_attachments_before_answer_event(queue_available):
    env = await composition()
    await save(env)
    if not queue_available:
        env.documents.flow.source._job_queue.enqueue = AsyncMock(side_effect=OSError('queue unavailable'))
    order = []
    actual_save, actual_receive = env.publisher.legacy_publication.save, env.profile.inbox.receive

    async def archive(**kwargs):
        assert (await env.profile.inbox.get_cutover(env.profile.scope, 'questionnaire', 'q')).status == 'ready'
        assert not (await task(env)).get('receipt')
        order.append('attachment')
        return await actual_save(**kwargs)

    async def receive(*args, **kwargs):
        assert (await task(env))['attachmentStatus'] == 'accepted'
        assert len(env.documents.flow.collection.documents) == 1
        order.append('answer')
        return await actual_receive(*args, **kwargs)

    env.publisher.legacy_publication.save, env.profile.inbox.receive = archive, receive
    await env.router.ensure_queued(user_id='u', questionnaire_id='q')
    assert not order and not env.profile.collections['publications'].documents
    assert not env.documents.flow.collection.documents
    await env.router.recover_pending()
    row = await task(env)
    assert row['status'] == 'waiting_receipt' and order == ['attachment', 'answer']
    assert row['attachmentReceipt']['items'][0]['ingestionStatus'] == ('queued' if queue_available else 'pending')
    assert row['receipt']['watermark']['appliedPublicationRevision'] == 0
    assert not env.gateway.complete_requests
    assert await env.profile.worker.recover_pending() == 1
    env.now[0] += 31
    env.profile.now[0] += timedelta(seconds=31)
    await env.router.recover_pending()
    assert (await task(env))['status'] == 'applied'
    assert order == ['attachment', 'answer']
    assert len(env.gateway.complete_requests) == 1
    assert not env.documents.flow.client.submit_calls and not env.documents.flow.graph.calls


@pytest.mark.asyncio
async def test_restart_reuses_dedicated_attachment_command_after_lost_ack():
    env = await composition()
    await save(env)
    original = env.publisher.publish

    async def lost(**kwargs):
        await original(**kwargs)
        raise OSError('attachment ACK lost')

    env.publisher.publish = lost
    await env.router.recover_pending()
    before = await task(env)
    assert before['status'] == 'retry_wait' and not before.get('receipt')
    assert len(env.documents.flow.collection.documents) == 1
    fixed = deepcopy(before['attachmentCommand'])
    commands = deepcopy(env.group.attachment_commands.documents)
    restart(env)
    env.now[0] += 31
    env.profile.now[0] += timedelta(seconds=31)
    await env.router.recover_pending()
    after = await task(env)
    assert after['status'] == 'waiting_receipt' and after['attachmentCommand'] == fixed
    assert env.group.attachment_commands.documents == commands
    assert len(env.documents.flow.collection.documents) == len(env.documents.flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_profile_unavailable_does_not_undo_saved_questionnaire_or_publish_attachments():
    env = await composition()
    await save(env)
    original = deepcopy(env.source.documents)
    env.fresh.reserve_fresh_source = AsyncMock(side_effect=OSError('Profile offline'))
    await env.router.ensure_queued(user_id='u', questionnaire_id='q')
    env.fresh.reserve_fresh_source.assert_not_awaited()
    await env.router.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    assert env.source.documents == original
    assert not env.documents.flow.collection.documents and not env.profile.collections['publications'].documents
