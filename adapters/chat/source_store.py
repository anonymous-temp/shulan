"""Chat-owned durable tasks and PRIMARY saved-message evidence; no index creation."""

from copy import deepcopy

from pymongo import ReadPreference
from pymongo.collation import Collation
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from modules.chat.profile_publication import TERMINAL
from modules.chat.profile_publication import ChatSourceConflict
from modules.chat.profile_source import source_digest
from modules.chat.profile_source import verified_saved_source

_BINDING_KEYS = (
    'messageId',
    'sourceRevision',
    'publicationRevision',
    'deliveryMode',
    'consumerRevision',
    'draft',
    'draftDigest',
    'deliveryPolicy',
    'createdAt',
    'deliveryManifestDigest',
)


def saved_binding(session, message_id):
    if not isinstance(session, dict) or session.get('type') != 'chat':
        raise ChatSourceConflict('the saved source is not a Chat session')
    state = verified_saved_source(session, message_id)
    if state is None:
        return None
    if state.get('jobId') is not None or state.get('status') != 'pending':
        raise ChatSourceConflict('a new Chat source contains legacy delivery evidence')
    return {key: deepcopy(state[key]) for key in _BINDING_KEYS}


class MongoChatSavedSourceReader:
    """Only exact user-message identity is fixed; the surrounding session may evolve."""

    def __init__(self, collection):
        self.collection = collection

    async def read_session(self, *, user_id, message_id):
        rows = (
            await self.collection.find(
                {
                    'userId': user_id,
                    '$or': [
                        {'messages': {'$elemMatch': {'messageId': message_id}}},
                        {'chatProfileIngestion': {'$elemMatch': {'messageId': message_id}}},
                    ],
                }
            )
            .max_time_ms(5000)
            .limit(2)
            .to_list(2)
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ChatSourceConflict('multiple saved sessions claim the Chat message identity')
        return deepcopy(rows[0])

    async def read_source(self, *, user_id, message_id):
        row = await self.read_session(user_id=user_id, message_id=message_id)
        return saved_binding(row, message_id) if row is not None else None

    async def discover(self, *, after, limit):
        fields = ('userId', 'sessionId', 'messageId')
        pipeline = [
            {'$match': {'type': 'chat'}},
            {'$unwind': '$chatProfileIngestion'},
            {'$match': {'chatProfileIngestion.deliveryMode': {'$exists': True, '$nin': [None, 'legacy']}}},
            {'$project': {'_id': 0, 'userId': 1, 'sessionId': 1, 'messageId': '$chatProfileIngestion.messageId'}},
            {'$match': {key: {'$type': 'string', '$ne': ''} for key in fields}},
        ]
        if after is not None:
            pipeline.append(
                {
                    '$match': {
                        '$or': [
                            {**{key: after[key] for key in fields[:index]}, field: {'$gt': after[field]}}
                            for index, field in enumerate(fields)
                        ]
                    }
                }
            )
        pipeline.extend([{'$sort': {key: 1 for key in fields}}, {'$limit': limit}])
        cursor = await self.collection.aggregate(pipeline, maxTimeMS=5000, collation=Collation(locale='simple'))
        return await cursor.to_list(limit)


class MongoChatSourceStore:
    def __init__(self, collection):
        self.collection = collection

    async def insert(self, row):
        try:
            result = await self.collection.insert_one(deepcopy(row))
            if not result.acknowledged:
                raise RuntimeError('Chat source write was not acknowledged')
            return deepcopy(row)
        except DuplicateKeyError:
            return await self.get(row['_id'])

    async def get(self, identity):
        return await self.collection.find_one({'_id': identity})

    async def replace(self, expected, replacement, *, now):
        query = {'_id': expected['_id'], 'revision': expected['revision']}
        if expected.get('leaseOwner'):
            query.update(
                leaseOwner=expected['leaseOwner'],
                leaseGeneration=expected['leaseGeneration'],
                leaseExpiresAt={'$gt': now},
            )
        else:
            query['$or'] = [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}]
        result = await self.collection.replace_one(query, deepcopy(replacement))
        if not result.acknowledged:
            raise RuntimeError('Chat source CAS was not acknowledged')
        return bool(result.modified_count)

    async def claim(self, identity, *, now, owner):
        for _ in range(4):
            row = await self.get(identity)
            if row is None or row.get('recordKind') != 'task' or row['status'] in TERMINAL:
                return None
            if row['nextAttemptAt'] > now or row.get('leaseOwner') and row.get('leaseExpiresAt', 0) > now:
                return None
            replacement = {
                **deepcopy(row),
                'revision': row['revision'] + 1,
                'leaseOwner': owner,
                'leaseGeneration': row.get('leaseGeneration', 0) + 1,
                'leaseExpiresAt': now + row['policy']['lease_seconds'],
                'attempts': row['attempts'] + 1,
            }
            if await self.replace({**row, 'leaseOwner': None}, replacement, now=now):
                return replacement
        return None

    async def pending(self, *, now, limit):
        query = {
            'recordKind': 'task',
            'status': {'$nin': sorted(TERMINAL)},
            'nextAttemptAt': {'$lte': now},
            '$or': [{'leaseOwner': None}, {'leaseExpiresAt': {'$lte': now}}],
        }
        return [
            row['_id']
            for row in await self.collection.find(query)
            .sort([('nextAttemptAt', 1), ('_id', 1)])
            .limit(limit)
            .to_list(limit)
        ]

    async def scan_state(self):
        return await self.insert(
            {'_id': 'chat_source_discovery_v1', 'recordKind': 'scan', 'revision': 1, 'after': None}
        )

    async def advance_scan(self, previous, after):
        replacement = {**previous, 'revision': previous['revision'] + 1, 'after': deepcopy(after)}
        result = await self.collection.replace_one(
            {'_id': previous['_id'], 'revision': previous['revision']}, replacement
        )
        if not result.acknowledged:
            raise RuntimeError('Chat discovery cursor was not acknowledged')
        # A competing scanner may have advanced this same page. It is safe to replay,
        # but this older observation must never move its cursor backwards.
        return bool(result.modified_count)

    async def record_discovery_error(self, source, code):
        await self.insert(
            {
                '_id': 'chat_source_discovery_error_' + source_digest(source),
                'recordKind': 'discovery_error',
                'source': deepcopy(source),
                'errorCode': code,
            }
        )


def primary_source_collection(collection):
    return collection.with_options(
        read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority', j=True, wtimeout=5000)
    )


def primary_session_collection(collection):
    return collection.with_options(read_preference=ReadPreference.PRIMARY)
