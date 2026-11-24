"""Offline CAS repository; production code has no in-memory execution fallback."""

import asyncio
from copy import deepcopy


class MemoryToolRunRepository:
    def __init__(self):
        self.documents = {}
        self.lock = asyncio.Lock()

    async def load(self, key):
        async with self.lock:
            return deepcopy(self.documents.get(key))

    async def create(self, document):
        async with self.lock:
            if document['_id'] in self.documents:
                return False
            self.documents[document['_id']] = deepcopy(dict(document))
            return True

    async def compare_save(self, key, revision, document):
        async with self.lock:
            if self.documents[key]['revision'] != revision:
                return False
            self.documents[key] = deepcopy(dict(document))
            return True
