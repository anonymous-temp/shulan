"""Opt-in Document lifecycle assembly; no worker launch or source activation."""

from dataclasses import dataclass

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.observation_reader import MongoDocumentObservationSource
from adapters.analysis.publication import MongoDocumentPublicationLedger
from adapters.analysis.publication_receiver import ProfileDocumentPublicationReceiver
from modules.analysis.observation_lifecycle import DocumentLifecycleLegacyIngestionGuard
from modules.analysis.observation_lifecycle import DocumentLifecycleObservationReader
from modules.analysis.observation_reader import DocumentObservationReader
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.publication_recovery import DocumentPublicationRecovery


@dataclass(frozen=True, slots=True)
class DocumentPublicationRuntime:
    ledger: MongoDocumentPublicationLedger
    recovery: DocumentPublicationRecovery
    lifecycle: DocumentPublicationLifecycleReader
    observation_v1: DocumentObservationReader
    observation_v2: DocumentLifecycleObservationReader


def create_document_publication_runtime(
    *,
    source_collection=None,
    intent_collection=None,
    ledger=None,
    observation_collection,
    snapshots,
    profile_inbox,
    legacy_ingestion_reader,
    policy=None,
    clock=None,
):
    """Use existing collections and the exact shared legacy gate ledger.

    Deployment owns collections/indexes: built-in unique _id, scoped source scan
    (userId, subjectId, _id), source repair (mode, outboxPending, _id), and intent
    retry (recoveryRoute, recoveryState, nextAttemptAtMs, _id). Use majority writes, PRIMARY reads,
    no TTL on source/intents/snapshots; no DDL, clients, workers or IO run here.

    Pass either both collections (the original construction path), or the exact
    existing ledger already shared with compatibility and the Profile readers. An
    injected ledger must already use PRIMARY/majority; it is never cloned or rebound.

    This factory does not authorize cutover or admit base Facts as complete source
    material. A complete-source publisher/resolver must be delivered separately.
    """
    options = {'read_preference': ReadPreference.PRIMARY, 'write_concern': WriteConcern(w='majority')}
    if ledger is None:
        if source_collection is None or intent_collection is None:
            raise ValueError('both source and intent collections are required without a ledger')
        ledger = MongoDocumentPublicationLedger(
            sources=source_collection.with_options(**options),
            intents=intent_collection.with_options(**options),
        )
    else:
        if source_collection is not None or intent_collection is not None:
            raise ValueError('pass an existing ledger or collections, not both')
        if not isinstance(ledger, MongoDocumentPublicationLedger):
            raise TypeError('ledger must be a MongoDocumentPublicationLedger')
        for collection in (ledger.sources, ledger.intents):
            if getattr(collection, 'read_preference', None) != ReadPreference.PRIMARY:
                raise ValueError('existing ledger collections must use PRIMARY reads')
            concern = getattr(collection, 'write_concern', None)
            if not isinstance(concern, WriteConcern) or concern.document.get('w') != 'majority':
                raise ValueError('existing ledger collections must use majority writes')
    lifecycle = DocumentPublicationLifecycleReader(ledger=ledger, snapshots=snapshots)
    receiver = ProfileDocumentPublicationReceiver(profile_inbox)
    source = MongoDocumentObservationSource(observation_collection.with_options(read_preference=ReadPreference.PRIMARY))
    guard = DocumentLifecycleLegacyIngestionGuard(lifecycle=lifecycle, ingestion=legacy_ingestion_reader)
    return DocumentPublicationRuntime(
        ledger=ledger,
        recovery=DocumentPublicationRecovery(
            ledger=ledger, receiver=receiver, policy=policy, clock=clock, recovery_route='default'
        ),
        lifecycle=lifecycle,
        observation_v1=DocumentObservationReader(source=source, ingestion=guard, clock=clock),
        observation_v2=DocumentLifecycleObservationReader(
            source=source,
            ingestion=legacy_ingestion_reader,
            lifecycle=lifecycle,
            clock=clock,
        ),
    )
