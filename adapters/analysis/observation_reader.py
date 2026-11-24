"""Read the existing Document observation collection without mutation or DDL."""

from pymongo import ASCENDING
from pymongo import ReadPreference

from modules.analysis.observation_reader import DocumentObservationReader


class MongoDocumentObservationSource:
    def __init__(self, collection):
        self.collection = collection

    async def read_first(self, *, scope, limit):
        return await self.collection.find(scope.as_dict()).sort([('_id', ASCENDING)]).to_list(length=limit)

    async def read_after(self, *, scope, after_id, limit):
        query = scope.as_dict()
        if after_id is not None:
            query['_id'] = {'$gt': after_id}
        return await self.collection.find(query).sort([('_id', ASCENDING)]).to_list(length=limit)


def create_document_observation_reader(*, collection, ingestion_reader):
    return DocumentObservationReader(
        source=MongoDocumentObservationSource(collection.with_options(read_preference=ReadPreference.PRIMARY)),
        ingestion=ingestion_reader,
    )
