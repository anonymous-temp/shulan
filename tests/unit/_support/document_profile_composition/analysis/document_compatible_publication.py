"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_compatible_publication."""

from types import SimpleNamespace

from bson import BSON

from adapters.analysis.compatibility import create_document_compatibility_service
from adapters.analysis.publication import MongoDocumentPublicationLedger
from tests.unit._support.document_profile_composition.analysis.document_workflow import extended_flow
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCollection


class BsonCollection(MemoryMongoCollection):
    async def insert_one(self, document):
        return await super().insert_one(BSON.encode(document).decode())


def environment():
    flow = extended_flow()
    flow.collection = BsonCollection()
    flow.reports._collection = flow.collection
    flow.reports._revisions = BsonCollection()
    ledger = MongoDocumentPublicationLedger(sources=MemoryMongoCollection(), intents=MemoryMongoCollection())
    flow.service._publication_ledger = ledger
    env = SimpleNamespace(
        flow=flow, ledger=ledger, snapshots=BsonCollection(), chunks=BsonCollection(), records=BsonCollection()
    )
    env.api = restart(env)
    return env


def restart(env):
    return create_document_compatibility_service(
        processing_service=env.flow.service,
        profile_service=env.flow.service._health_profile_service,
        snapshots=env.snapshots,
        snapshot_chunks=env.chunks,
        compatibility_records=env.records,
        publication_ledger=env.ledger,
    )
