"""Report-owned research input and original-evidence journal."""

from copy import deepcopy

from pymongo.errors import DuplicateKeyError

from modules.report.research import digest


class MongoResearchStore:
    def __init__(self, collection):
        self.collection = collection

    async def admit(self, key, plan, deadline_at):
        document = {
            '_id': key,
            'plan': deepcopy(plan),
            'planDigest': digest(plan),
            'deadlineAt': deadline_at,
            'calls': {},
        }
        try:
            result = await self.collection.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('Research admission was not durable')
            return document
        except DuplicateKeyError:
            existing = await self.collection.find_one({'_id': key})
            if existing is None or existing['planDigest'] != document['planDigest'] or existing['plan'] != plan:
                raise ValueError('Research run is already bound to another input or version')
            return existing

    async def save_evidence(self, key, call_id, arguments, rows):
        # Call keys cannot introduce Mongo dotted paths. Each native invocation
        # has at most eight calls, five bounded originals per call.
        call_key = digest(call_id)
        value = {'callId': call_id, 'arguments': deepcopy(arguments), 'rows': deepcopy(rows)}
        result = await self.collection.update_one(
            {'_id': key, f'calls.{call_key}': {'$exists': False}},
            {'$set': {f'calls.{call_key}': value}},
        )
        if not result.acknowledged:
            raise RuntimeError('Research evidence was not durable')
        if result.modified_count:
            return
        stored = await self.collection.find_one({'_id': key})
        if stored is None or stored.get('calls', {}).get(call_key) != value:
            raise ValueError('Research call ID conflicts with saved original evidence')

    async def read_evidence(self, key):
        stored = await self.collection.find_one({'_id': key})
        if stored is None:
            raise ValueError('Research original evidence journal is unavailable')
        return [deepcopy(row) for call in stored['calls'].values() for row in call['rows']]

    async def read_calls(self, key):
        stored = await self.collection.find_one({'_id': key})
        if stored is None:
            raise ValueError('Research query journal is unavailable')
        return [
            {'callId': call['callId'], **deepcopy(call['arguments']), 'originalCount': len(call['rows'])}
            for call in stored['calls'].values()
        ]
