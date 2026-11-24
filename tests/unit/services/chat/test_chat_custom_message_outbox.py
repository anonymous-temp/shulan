"""自定义消息 MongoDB Outbox 的无网络单元测试。"""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace

from services.chat_custom_message_outbox import ChatCustomMessageOutbox
from services.chat_custom_message_outbox import (
    ChatCustomMessageOutboxConflictError,
)


class FakeCursor:
    def __init__(self, documents) -> None:
        self.documents = list(documents)

    def sort(self, field, direction):
        self.documents.sort(key=lambda document: document.get(field))
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self

    def __aiter__(self):
        async def iterate():
            for document in self.documents:
                yield deepcopy(document)

        return iterate()


class FakeCollection:
    def __init__(self) -> None:
        self.documents = []
        self.indexes = []

    async def create_index(self, fields, **options):
        self.indexes.append((fields, options))
        return options['name']

    async def find_one(self, filters):
        for document in self.documents:
            if self._matches(document, filters):
                return deepcopy(document)
        return None

    async def insert_one(self, document):
        self.documents.append(deepcopy(document))
        return SimpleNamespace(acknowledged=True)

    def find(self, filters, projection=None):
        documents = []
        for document in self.documents:
            if not self._matches(document, filters):
                continue
            if projection is None:
                documents.append(deepcopy(document))
            else:
                documents.append({
                    key: deepcopy(document[key])
                    for key, included in projection.items()
                    if included and key in document
                })
        return FakeCursor(documents)

    async def update_one(self, filters, update):
        for document in self.documents:
            if not self._matches(document, filters):
                continue
            for field, increment in update.get('$inc', {}).items():
                document[field] = document.get(field, 0) + increment
            for field, value in update.get('$set', {}).items():
                document[field] = deepcopy(value)
            for field in update.get('$unset', {}):
                document.pop(field, None)
            return SimpleNamespace(acknowledged=True, matched_count=1)
        return SimpleNamespace(acknowledged=True, matched_count=0)

    @staticmethod
    def _matches(document, filters):
        return all(document.get(key) == value for key, value in filters.items())


class ChatCustomMessageOutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_is_idempotent_and_rejects_changed_content(self):
        collection = FakeCollection()
        outbox = ChatCustomMessageOutbox(collection)
        message = {
            'displayType': 'CARD',
            'customType': 'HEALTH_REPORT_CARD',
            'content': '报告已签发。',
            'payload': {'reportId': 'report-1'},
        }

        first, first_created = await outbox.enqueue(
            user_id='user-1',
            session_id='session-1',
            event_id='event-1',
            message=message,
        )
        second, second_created = await outbox.enqueue(
            user_id='user-1',
            session_id='session-1',
            event_id='event-1',
            message=message,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second, first)
        self.assertEqual(len(collection.documents), 1)
        self.assertEqual(len(collection.indexes), 2)

        with self.assertRaisesRegex(
                ChatCustomMessageOutboxConflictError,
                '消息内容不一致',
        ):
            await outbox.enqueue(
                user_id='user-1',
                session_id='session-1',
                event_id='event-1',
                message={**message, 'content': '不同内容'},
            )

    async def test_pending_event_can_be_recovered_and_marked_delivered(self):
        collection = FakeCollection()
        outbox = ChatCustomMessageOutbox(collection)
        event, _ = await outbox.enqueue(
            user_id='user-1',
            session_id='session-1',
            event_id='event-1',
            message={
                'displayType': 'CARD',
                'customType': 'INTERVESE_CARD',
                'content': '干预方案已签发。',
                'payload': {},
            },
        )

        self.assertEqual(
            await outbox.list_pending_sessions(),
            [('user-1', 'session-1')],
        )
        self.assertEqual(
            [item['eventId'] for item in await outbox.list_pending(
                'user-1',
                'session-1',
            )],
            ['event-1'],
        )

        await outbox.record_attempt(event)
        delivered_message = {
            'messageId': 'message-1',
            'eventId': 'event-1',
            'role': 'custom',
            'content': '干预方案已签发。',
        }
        await outbox.mark_delivered(event, delivered_message)
        saved = await outbox.get('user-1', 'session-1', 'event-1')

        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(saved['status'], 'delivered')
        self.assertEqual(saved['deliveredMessage'], delivered_message)
        self.assertEqual(
            await outbox.list_pending('user-1', 'session-1'),
            [],
        )


if __name__ == '__main__':
    unittest.main()
