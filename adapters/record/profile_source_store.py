"""Record-owned source intents use one document CAS and existing unique _id keys."""

from copy import deepcopy

from pymongo.errors import DuplicateKeyError

from modules.record.profile_publication import RecordSourceConflict


class MongoRecordSourceStore:
    def __init__(self, collection):
        self.collection = collection

    async def insert(self, row):
        frozen = deepcopy(row)
        try:
            result = await self.collection.insert_one(frozen)
            if not result.acknowledged:
                raise RuntimeError('Record source intent write was not acknowledged')
            return frozen
        except DuplicateKeyError:
            return await self.get(frozen['_id'])

    async def get(self, intent_id):
        return await self.collection.find_one({'_id': intent_id})

    async def replace(self, expected, replacement, *, now):
        query = {'_id': expected['_id'], 'revision': expected['revision']}
        if expected.get('leaseOwner'):
            query.update(
                {
                    'leaseOwner': expected['leaseOwner'],
                    'leaseGeneration': expected['leaseGeneration'],
                    'leaseExpiresAt': {'$gt': now},
                }
            )
        else:
            # Redrive cannot race a worker that acquired a lease after this read.
            query['$or'] = [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}]
        result = await self.collection.replace_one(query, deepcopy(replacement))
        return bool(result.modified_count)

    async def claim(self, intent_id, *, now, owner):
        for _ in range(4):
            row = await self.get(intent_id)
            if row is None or row['status'] in {'applied', 'partial', 'failed'}:
                return None
            if row.get('nextAttemptAt', 0) > now or (row.get('leaseOwner') and row.get('leaseExpiresAt', 0) > now):
                return None
            updated = {
                **deepcopy(row),
                'revision': row['revision'] + 1,
                'leaseOwner': owner,
                'leaseGeneration': row.get('leaseGeneration', 0) + 1,
                'leaseExpiresAt': now + row['policy']['lease_seconds'],
            }
            query = {'_id': intent_id, 'revision': row['revision']}
            result = await self.collection.replace_one(query, updated)
            if result.modified_count:
                return updated
        return None

    async def pending(self, *, now, limit):
        query = {
            'status': {'$nin': ['applied', 'partial', 'failed']},
            'nextAttemptAt': {'$lte': now},
            '$or': [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}],
        }
        return [
            row['_id'] for row in await self.collection.find(query).sort([('createdAt', 1), ('_id', 1)]).to_list(limit)
        ]

    async def find_snapshot(self, snapshot_id):
        rows = await self.collection.find({'snapshotRef.snapshotId': snapshot_id}).limit(2).to_list(2)
        if len(rows) > 1:
            raise RecordSourceConflict('multiple Record intents claim the same fixed source material')
        return rows[0] if rows else None


class MongoRecordCompletionEvidence:
    """Read preserved completion identity; never replace an active Chat session."""

    def __init__(self, collection):
        self.collection = collection

    async def is_committed(self, *, user_id, session_id, completion_id, draft_id):
        rows = await self.collection.find({'userId': user_id, 'sessionId': session_id}).limit(2).to_list(2)
        if len(rows) != 1:
            return False
        row = rows[0]
        complete = (row.get('type') == 'qa' and row.get('status') == 'completed') or (
            row.get('type') == 'chat' and bool(row.get('qaCompletedAt'))
        )
        marker = row.get('qaProfileIngestion', {})
        binding = marker.get('recordSourceIntent', {})
        return bool(
            complete
            and marker.get('deliveryMode') == 'profile-source-event.v1'
            and binding.get('draftId') == draft_id
            and binding.get('completionId') == completion_id
        )


def create_record_source_store(collection):
    """Use configured resources, majority durability and PRIMARY; construction has no I/O."""
    from pymongo import ReadPreference
    from pymongo.write_concern import WriteConcern

    return MongoRecordSourceStore(
        collection.with_options(
            read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority', j=True, wtimeout=5000)
        )
    )
