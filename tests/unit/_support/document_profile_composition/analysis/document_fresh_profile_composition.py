"""Shared synthetic fixture builders for fresh Profile composition."""

from types import MethodType
from types import SimpleNamespace

from adapters.analysis.complete_source import ProfileDocumentCompleteSourceReader
from adapters.analysis.publication_receiver import ProfileDocumentPublicationReceiver
from adapters.analysis.source_admission import ProfileDocumentOwnerFenceReader
from adapters.analysis.source_admission import create_document_fresh_source_admission
from health_profile.document_source import ProfileDocumentSourceResolver
from health_profile.source_admission import ProfileFreshSourceAdmission
from health_profile.source_presence import MongoProfileSourcePresenceReader
from modules.analysis.complete_source_publication import DocumentCompleteSourcePublisher
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.publication_recovery import DocumentPublicationRecovery
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from tests.unit._support.document_profile_composition.analysis.document_complete_source import service
from tests.unit._support.document_profile_composition.analysis.document_material_reader import BsonCollection
from tests.unit._support.document_profile_composition.analysis.document_report_publication import environment
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCollection
from tests.unit._support.document_profile_composition.profile.public_contracts import setup_flow
from tests.unit._support.document_profile_composition.profile.source_admission import Collection


def options(collection):
    collection.options = []

    def with_options(current, **kwargs):
        current.options.append(kwargs)
        return current

    collection.with_options = MethodType(with_options, collection)
    return collection


async def composition():
    profile = await setup_flow(authorize=False)
    scope = DocumentScope(profile.scope.user_id, profile.scope.subject_id)
    env = environment()
    bundle = await env.api.prepare_bundle(
        scope=scope, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/fresh.pdf'
    )
    item = DocumentPreparedPublicationInput(
        bundle.material.snapshot_id,
        bundle.facts.snapshot_id,
        bundle.understanding.snapshot_id,
        UNDERSTANDING_SELECTION,
        bundle.receipt,
    )
    collections = {name: Collection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')}
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: profile.now[0])
    env.commands.find = MethodType(MemoryMongoCollection.find, env.commands)
    for value in (env.records, env.commands, env.flow.observations.collection):
        options(value)
    admission_records = options(BsonCollection())
    admission = create_document_fresh_source_admission(
        compatibility=env.api,
        report_publications=env.publisher,
        ledger=env.ledger,
        admission_records=admission_records,
        observation_collection=env.flow.observations.collection,
        profile_presence=presence,
        profile_inbox=profile.inbox,
    )
    api = service(env, admission)
    resolver = ProfileDocumentSourceResolver(ProfileDocumentCompleteSourceReader(api))
    profile.inbox.document_source = resolver
    profile.applier.resolvers[('document', COMPLETE_SOURCE_CONTRACT)] = resolver
    profile.applier.resolvers[('document', RETRACTION_CONTRACT)] = resolver
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox,
        presence_reader=presence,
        owner_fence_readers={'document': ProfileDocumentOwnerFenceReader(admission)},
    )
    recovery = DocumentPublicationRecovery(
        ledger=env.ledger, receiver=ProfileDocumentPublicationReceiver(profile.inbox), clock=lambda: profile.now[0]
    )
    publisher = DocumentCompleteSourcePublisher(
        source_reader=api, snapshots=api.snapshots, ledger=env.ledger, recovery=recovery
    )
    return SimpleNamespace(
        env=env,
        profile=profile,
        scope=scope,
        bundle=bundle,
        item=item,
        collections=collections,
        presence=presence,
        admission=admission,
        api=api,
        fresh=fresh,
        resolver=resolver,
        publisher=publisher,
        recovery=recovery,
        lifecycle=DocumentPublicationLifecycleReader(ledger=env.ledger, snapshots=api.snapshots),
        admission_records=admission_records,
    )
