"""Immutable completion publication into the shared business-session collection."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from pymongo.errors import DuplicateKeyError


def _same_completion(existing, proposed):
    # New completions carry the immutable commit identity through Chat takeover.
    if existing.get('recordCompletionId') is not None:
        return existing['recordCompletionId'] == proposed['recordCompletionId']
    # Legacy completions have no identity. Require exact preserved QA evidence;
    # no successful match based only on a reused user/session pair.
    if not (existing.get('status') == 'completed' or existing.get('qaCompletedAt')):
        return False
    if (existing.get('qaCompletedAt') or existing.get('completedAt')) != proposed.get('completedAt'):
        return False
    fields = (
        'qaFlow',
        'qaHistory',
        'answers',
        'parsedHealthReports',
        'documents',
        'documentProcessing',
        'extractedInfo',
        'extractedInfoSources',
        'healthData',
        'createdAt',
    )
    return all(existing.get(key) == proposed.get(key) for key in fields)


class MongoRecordCompletionStore:
    def __init__(self, collection):
        self.collection = collection
        self._ready = False
        self._lock = asyncio.Lock()

    async def _ensure_index(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                # Same definition used by the existing questionnaire owner gate.
                # Duplicate legacy rows fail this command; never delete or merge
                # them automatically, and do not create indexes at app startup.
                await self.collection.create_index(
                    [('userId', 1), ('sessionId', 1)],
                    unique=True,
                    partialFilterExpression={'userId': {'$type': 'string'}, 'sessionId': {'$type': 'string'}},
                    name='session_business_identity',
                )
                self._ready = True

    async def publish_once(self, user_id, session_id, session):
        if (
            session.get('userId') != user_id
            or session.get('sessionId') != session_id
            or session.get('type') != 'qa'
            or session.get('status') != 'completed'
            or not session.get('recordCompletionId')
        ):
            raise ValueError('invalid immutable Record completion')
        await self._ensure_index()
        query = {'userId': user_id, 'sessionId': session_id}
        existing = await self.collection.find_one(query)
        if existing is None:
            try:
                inserted = await self.collection.insert_one(deepcopy(session))
                return bool(inserted.acknowledged)
            except DuplicateKeyError:
                existing = await self.collection.find_one(query)
        if existing is None or not _same_completion(existing, session):
            raise ValueError('completed Record identity conflicts with the current session owner')
        return True


def create_application_record_completion_store():
    from pymongo.write_concern import WriteConcern

    from db.mongodb import collection_sessions

    collection = collection_sessions.with_options(write_concern=WriteConcern(w='majority', j=True, wtimeout=5000))
    return MongoRecordCompletionStore(collection)
