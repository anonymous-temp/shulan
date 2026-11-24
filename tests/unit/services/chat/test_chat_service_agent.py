"""ChatSessionManager 非流式路径接入 AgentHub 的无网络测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver

from adapters.persistence import MongoChatSessionRepository
from agents import AgentEventType
from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.llm import ModelProfileRegistry
from agents.persistence import LLMContextSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _stub_module(name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


def _load_chat_service(collection):
    """不加载真实配置和数据库地导入 Chat 应用服务实现。"""
    stubs = {
        'config.env': _stub_module(
            'config.env',
            LLM_API_KEY='unused-test-key',
            LLM_DEFAULT_MODEL='unused-test-model',
            LOG_LEVEL='CRITICAL',
        ),
    }
    spec = importlib.util.spec_from_file_location(
        '_shulan_chat_agent_service_target',
        PROJECT_ROOT / 'modules' / 'chat' / 'application' / 'service.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class FakeQASessionGate:
    def __init__(
        self,
        *,
        lease_manager,
        pending=False,
        error=None,
    ) -> None:
        self.lease_manager = lease_manager
        self.pending = pending
        self.error = error
        self.calls = []

    async def assertNoPendingSession(self, user_id, session_id):
        self.calls.append((user_id, session_id))
        if self.error is not None:
            raise self.error
        if self.pending:
            raise ValueError('问答会话未完成，无法进行普通对话')

    async def acquireSessionLease(self, user_id, session_id):
        lease_id = f'business-session:{len(user_id)}:{user_id}:{session_id}'
        lease = await self.lease_manager.try_acquire(lease_id, 'qa-gate-owner')
        if lease is None:
            raise ValueError('当前会话正在处理另一个请求，请稍后重试')
        return lease_id, lease

    async def releaseSessionLease(self, lease_id, lease):
        await self.lease_manager.release(lease)


class FakeSessionCollection:
    def __init__(self, document=None) -> None:
        self.document = deepcopy(document)
        self.replace_calls = []

    def find(self, filter, projection=None):
        async def documents():
            if self.document is None:
                return
            if all(self.document.get(key) == value for key, value in filter.items()):
                if projection == {'sessionId': 1}:
                    yield {'sessionId': self.document['sessionId']}
                else:
                    yield deepcopy(self.document)

        return documents()

    async def find_one(self, filter):
        if self.document is None:
            return None
        if all(self.document.get(key) == value for key, value in filter.items()):
            return deepcopy(self.document)
        return None

    async def replace_one(self, filter, document, *, upsert):
        self.replace_calls.append(
            {
                'filter': deepcopy(filter),
                'document': deepcopy(document),
                'upsert': upsert,
            }
        )
        self.document = deepcopy(document)
        return SimpleNamespace(acknowledged=True)


class FakeCustomMessageOutbox:
    def __init__(self) -> None:
        self.events = []

    async def enqueue(
        self,
        *,
        user_id,
        session_id,
        event_id,
        message,
    ):
        existing = await self.get(user_id, session_id, event_id)
        if existing is not None:
            if existing['message'] != message:
                from services.chat_custom_message_outbox import ChatCustomMessageOutboxConflictError

                raise ChatCustomMessageOutboxConflictError(
                    'eventId 已存在，但消息内容不一致',
                )
            return existing, False
        event = {
            'userId': user_id,
            'sessionId': session_id,
            'eventId': event_id,
            'message': deepcopy(message),
            'status': 'pending',
            'attempts': 0,
        }
        self.events.append(event)
        return deepcopy(event), True

    async def get(self, user_id, session_id, event_id):
        for event in self.events:
            if event['userId'] == user_id and event['sessionId'] == session_id and event['eventId'] == event_id:
                return deepcopy(event)
        return None

    async def list_pending_sessions(self, *, limit=100):
        sessions = []
        for event in self.events:
            key = (event['userId'], event['sessionId'])
            if event['status'] == 'pending' and key not in sessions:
                sessions.append(key)
        return sessions[:limit]

    async def list_pending(self, user_id, session_id):
        return [
            deepcopy(event)
            for event in self.events
            if event['userId'] == user_id and event['sessionId'] == session_id and event['status'] == 'pending'
        ]

    async def record_attempt(self, event):
        stored = self._stored(event)
        stored['attempts'] += 1

    async def mark_delivered(self, event, message):
        stored = self._stored(event)
        stored['status'] = 'delivered'
        stored['deliveredMessage'] = deepcopy(message)

    async def mark_failed(self, event, error):
        stored = self._stored(event)
        stored['status'] = 'failed'
        stored['lastError'] = error

    def _stored(self, expected):
        for event in self.events:
            if all(event[field] == expected[field] for field in ('userId', 'sessionId', 'eventId')):
                return event
        raise AssertionError('outbox event not found')


class FakeChatProfileSource:
    def __init__(self) -> None:
        self.prepare_calls = []
        self.queue_calls = []

    def prepare_user_message(self, session, message):
        self.prepare_calls.append((session['sessionId'], message['messageId']))
        session.setdefault('chatProfileIngestion', []).append(
            {
                'messageId': message['messageId'],
                'sourceRevision': message['messageRevision'],
                'status': 'pending',
                'jobId': None,
            }
        )

    async def ensure_queued(self, *, user_id, message_id):
        self.queue_calls.append((user_id, message_id))
        return SimpleNamespace(ingestion_queued=True)


class FakeHealthProfileMemoryProvider:
    def __init__(self, context=None, error=None) -> None:
        self.context = context
        self.error = error
        self.calls = []

    async def build_context(self, user_id):
        self.calls.append(user_id)
        if self.error is not None:
            raise self.error
        return self.context


class FakeReportContextProvider:
    def __init__(self) -> None:
        self.calls = []

    async def build_context(
        self,
        *,
        user_id,
        session_id,
        message,
        final_report_id=None,
        report_type=None,
        session_reference=None,
    ):
        self.calls.append(
            {
                'user_id': user_id,
                'session_id': session_id,
                'message': message,
                'final_report_id': final_report_id,
                'report_type': report_type,
                'session_reference': session_reference,
            }
        )
        return (
            '当前报告上下文：仅用于本轮追问',
            {
                'finalReportId': final_report_id or 'rpt-auto',
                'reportType': report_type or 'evaluate',
            },
        )


class RecordingChatGraph:
    def __init__(self) -> None:
        self.calls = []
        self.stream_calls = []

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        self.calls.append(
            {
                'input': deepcopy(input),
                'config': deepcopy(config),
                'context': context,
            }
        )
        return {
            **input,
            'assistant_content': 'Agent 模型回复',
            'reasoning_content': 'Agent 推理',
            'content': ('1. 如何改善睡眠？\n2、饮食方面需要注意什么？\n如何安排日常运动？'),
            'status': 'completed',
        }

    async def astream(self, input, config=None, *, context=None, **kwargs):
        self.stream_calls.append(
            {
                'input': deepcopy(input),
                'config': deepcopy(config),
                'context': context,
                'kwargs': deepcopy(kwargs),
            }
        )
        for event_type, content in (
            (AgentEventType.RUN_STARTED, None),
            (AgentEventType.REASONING_DELTA, 'Agent 推理一'),
            (AgentEventType.REASONING_DELTA, 'Agent 推理二'),
            (AgentEventType.CONTENT_DELTA, 'Agent 正文一'),
            (AgentEventType.CONTENT_DELTA, 'Agent 正文二'),
            (AgentEventType.RUN_COMPLETED, None),
        ):
            data = {}
            if content is not None:
                data['content'] = content
            yield {
                'type': event_type.value,
                'data': data,
            }


class FakeLeaseManager:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.acquire_calls = []
        self.release_calls = []

    async def try_acquire(self, thread_id, owner_id):
        self.acquire_calls.append((thread_id, owner_id))
        if not self.available:
            return None
        return SimpleNamespace(
            key=f'lease:{thread_id}',
            owner_id=owner_id,
        )

    async def release(self, lease):
        self.release_calls.append(lease)
        return True


class FakeContextRepository:
    def __init__(self, snapshots=()) -> None:
        self.snapshots = {snapshot.key: snapshot for snapshot in snapshots}
        self.load_calls = []

    async def load(self, key):
        self.load_calls.append(key)
        return self.snapshots.get(key)


def _build_runtime(*, lease_available: bool = True, context_snapshots=()):
    graph = RecordingChatGraph()
    registry = GraphRegistry()
    registry.register('chat.reply', graph, version='chat-reply-v5')
    registry.register(
        'chat.suggestions',
        graph,
        version='chat-suggestions-v1',
    )
    lease_manager = FakeLeaseManager(available=lease_available)
    context_repository = FakeContextRepository(context_snapshots)
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=object(),
        context_repository=context_repository,
        lease_manager=lease_manager,
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    return runtime, graph, lease_manager, context_repository


class ChatSessionManagerAgentTest(unittest.IsolatedAsyncioTestCase):
    def _manager(
        self,
        collection,
        *,
        lease_available=True,
        context_snapshots=(),
        qa_pending=False,
        qa_gate_error=None,
        custom_message_outbox=None,
        chat_profile_source=None,
        health_profile_memory_provider=None,
        report_context_provider=None,
    ):
        module = _load_chat_service(collection)
        runtime, graph, lease_manager, context_repository = _build_runtime(
            lease_available=lease_available,
            context_snapshots=context_snapshots,
        )
        manager = module.ChatSessionManager(
            runtime,
            session_repository=MongoChatSessionRepository(collection),
            qa_session_gate=FakeQASessionGate(
                lease_manager=lease_manager,
                pending=qa_pending,
                error=qa_gate_error,
            ),
            custom_message_outbox=(custom_message_outbox or FakeCustomMessageOutbox()),
            chat_profile_source=chat_profile_source,
            health_profile_memory_provider=health_profile_memory_provider,
            report_context_provider=report_context_provider,
        )
        return manager, graph, lease_manager, context_repository

    async def test_qa_conversion_preserves_profile_ingestion_source(self):
        manager, _graph, _lease_manager, _context_repository = self._manager(
            FakeSessionCollection(),
        )
        ingestion = {
            'status': 'queued',
            'jobId': 'profile-job-1',
        }
        parsed_reports = [
            {
                'reportId': 'qa-report-1',
                'reportType': 'lab',
                'status': 'abnormal',
                'expansion': [{'itemName': '尿酸', 'resultValue': '468'}],
            }
        ]

        converted = manager._convertQASessionToChat(
            {
                'sessionId': 'session-1',
                'userId': 'user-1',
                'type': 'qa',
                'status': 'completed',
                'createdAt': '2026-08-18T09:00:00',
                'completedAt': '2026-08-18T10:00:00',
                'round': 2,
                'qaHistory': [],
                'qaSourceRevision': 1,
                'qaProfileIngestion': ingestion,
                'extractedInfoSources': {'gender': 'user_input'},
                'parsedHealthReports': parsed_reports,
            }
        )

        self.assertEqual(converted['qaSourceRevision'], 1)
        self.assertEqual(converted['qaProfileIngestion'], ingestion)
        self.assertIsNot(converted['qaProfileIngestion'], ingestion)
        self.assertEqual(
            converted['extractedInfoSources'],
            {'gender': 'user_input'},
        )
        self.assertEqual(converted['parsedHealthReports'], parsed_reports)
        self.assertIsNot(converted['parsedHealthReports'], parsed_reports)

    async def test_custom_message_is_saved_without_calling_agent(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:00:00',
                'messages': [],
            }
        )
        manager, graph, lease_manager, context_repository = self._manager(
            collection,
        )

        result = await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'report-signed-1',
            '您的健康报告已经由医生审核完成，请点击查看详情。',
            {
                'reportId': 'report-1',
            },
            display_type='CARD',
            custom_type='HEALTH_REPORT_CARD',
            title='健康评估报告',
        )

        self.assertTrue(result['created'])
        message = result['message']
        UUID(message['messageId'])
        self.assertEqual(message['eventId'], 'report-signed-1')
        self.assertEqual(message['role'], 'custom')
        self.assertEqual(message['displayType'], 'CARD')
        self.assertEqual(message['customType'], 'HEALTH_REPORT_CARD')
        self.assertEqual(message['title'], '健康评估报告')
        self.assertEqual(
            message['content'],
            '您的健康报告已经由医生审核完成，请点击查看详情。',
        )
        self.assertEqual(message['aiContextMode'], 'none')
        self.assertEqual(message['payload']['reportId'], 'report-1')
        self.assertEqual(collection.document['messages'], [message])
        self.assertEqual(
            collection.document['lastMessageAt'],
            message['timestamp'],
        )
        self.assertEqual(graph.calls, [])
        self.assertEqual(graph.stream_calls, [])
        self.assertEqual(context_repository.load_calls, [])
        self.assertEqual(len(lease_manager.acquire_calls), 1)
        self.assertEqual(len(lease_manager.release_calls), 1)

    async def test_custom_message_event_is_idempotent(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:00:00',
                'messages': [],
            }
        )
        manager, graph, lease_manager, _ = self._manager(collection)
        arguments = (
            'user-1',
            'session-1',
            'report-signed-1',
            '您的健康报告已由医生审核完成。',
            {'reportId': 'report-1'},
        )

        first = await manager.insertCustomMessage(*arguments)
        second = await manager.insertCustomMessage(*arguments)

        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(second['message'], first['message'])
        self.assertEqual(len(collection.document['messages']), 1)
        self.assertEqual(len(collection.replace_calls), 1)
        self.assertEqual(len(lease_manager.release_calls), 1)
        self.assertEqual(graph.calls, [])

    async def test_custom_message_rejects_conflicting_event_fields(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:00:00',
                'messages': [],
            }
        )
        manager, _, lease_manager, _ = self._manager(collection)
        await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'report-signed-1',
            '您的健康报告已由医生审核完成。',
            {'reportId': 'report-1'},
        )

        conflicting_fields = (
            {'payload': {'reportId': 'report-2'}},
            {
                'payload': {'reportId': 'report-1'},
                'custom_type': 'HEALTH_REPORT_CARD',
            },
            {
                'payload': {'reportId': 'report-1'},
                'title': '健康评估报告',
            },
        )
        for fields in conflicting_fields:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, '消息内容不一致'):
                    await manager.insertCustomMessage(
                        'user-1',
                        'session-1',
                        'report-signed-1',
                        '您的健康报告已由医生审核完成。',
                        **fields,
                    )

        self.assertEqual(len(collection.document['messages']), 1)
        self.assertEqual(len(collection.replace_calls), 1)
        self.assertEqual(len(lease_manager.release_calls), 1)

    async def test_custom_message_defaults_and_validates_optional_fields(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'messages': [],
            }
        )
        manager, _, _, _ = self._manager(collection)

        result = await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'doctor-message-1',
            '医生提醒您按时复诊。',
        )

        self.assertEqual(result['message']['displayType'], 'CARD')
        self.assertEqual(
            result['message']['customType'],
            'DOCTOR_MESSAGE_CARD',
        )
        self.assertNotIn('title', result['message'])

        with self.assertRaisesRegex(ValueError, 'displayType 必须为以下值之一'):
            await manager.insertCustomMessage(
                'user-1',
                'session-1',
                'doctor-message-2',
                '这条消息不应写入。',
                display_type='TEXT',
            )

        custom_result = await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'doctor-message-3',
            '这是一条自定义业务消息。',
            custom_type='CUSTOM_BUSINESS_TYPE',
        )
        self.assertEqual(
            custom_result['message']['customType'],
            'CUSTOM_BUSINESS_TYPE',
        )

        with self.assertRaisesRegex(ValueError, 'customType 必须为非空字符串'):
            await manager.insertCustomMessage(
                'user-1',
                'session-1',
                'doctor-message-4',
                '这条消息不应写入。',
                custom_type=' ',
            )

        with self.assertRaisesRegex(ValueError, 'title 必须为非空字符串'):
            await manager.insertCustomMessage(
                'user-1',
                'session-1',
                'doctor-message-5',
                '这条消息不应写入。',
                title=' ',
            )

        self.assertEqual(len(collection.document['messages']), 2)

    async def test_custom_message_converts_completed_qa_to_chat(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'qa',
                'status': 'completed',
                'createdAt': '2026-08-14T00:00:00',
                'completedAt': '2026-08-14T00:10:00',
                'qaHistory': [
                    {
                        'question': {'question': '您的睡眠如何？'},
                        'answer': ['较差'],
                    }
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        result = await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'report-signed-1',
            '您的健康报告已由医生审核完成。',
        )

        self.assertTrue(result['created'])
        self.assertEqual(collection.document['type'], 'chat')
        self.assertEqual(
            [message['role'] for message in collection.document['messages']],
            ['system', 'custom'],
        )
        self.assertEqual(graph.calls, [])

    async def test_custom_message_rejects_pending_or_missing_session(self):
        pending_collection = FakeSessionCollection()
        pending_manager, _, pending_lease, _ = self._manager(
            pending_collection,
            qa_pending=True,
        )

        with self.assertRaisesRegex(ValueError, '问答会话未完成'):
            await pending_manager.insertCustomMessage(
                'user-1',
                'session-1',
                'report-signed-1',
                '您的健康报告已由医生审核完成。',
            )
        self.assertEqual(len(pending_lease.release_calls), 0)
        self.assertEqual(pending_collection.replace_calls, [])

        missing_collection = FakeSessionCollection()
        missing_manager, _, missing_lease, _ = self._manager(
            missing_collection,
        )
        with self.assertRaisesRegex(LookupError, '对话会话不存在'):
            await missing_manager.insertCustomMessage(
                'user-1',
                'session-1',
                'report-signed-1',
                '您的健康报告已由医生审核完成。',
            )
        self.assertEqual(len(missing_lease.release_calls), 0)
        self.assertEqual(missing_collection.replace_calls, [])

    async def test_custom_message_is_persisted_when_session_is_busy(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'messages': [
                    {'role': 'user', 'content': '正在咨询'},
                    {'role': 'assistant', 'content': '上一轮回答'},
                ],
            }
        )
        outbox = FakeCustomMessageOutbox()
        manager, graph, lease_manager, _ = self._manager(
            collection,
            lease_available=False,
            custom_message_outbox=outbox,
        )

        result = await manager.insertCustomMessage(
            'user-1',
            'session-1',
            'report-signed-1',
            '您的健康报告已经由医生审核完成。',
            custom_type='HEALTH_REPORT_CARD',
        )

        self.assertTrue(result['created'])
        self.assertTrue(result['queued'])
        self.assertEqual(result['status'], 'pending')
        self.assertIsNone(result['message'])
        self.assertEqual(len(outbox.events), 1)
        self.assertEqual(outbox.events[0]['status'], 'pending')
        self.assertEqual(len(collection.document['messages']), 2)
        self.assertEqual(graph.calls, [])

        lease_manager.available = True
        delivered = await manager.drainPendingCustomMessages()

        self.assertEqual(delivered, 1)
        self.assertEqual(
            [message['role'] for message in collection.document['messages']],
            ['user', 'assistant', 'custom'],
        )
        self.assertEqual(outbox.events[0]['status'], 'delivered')
        self.assertEqual(
            outbox.events[0]['deliveredMessage'],
            collection.document['messages'][-1],
        )

    async def test_completed_ai_turn_drains_card_after_assistant_message(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:00:00',
                'messages': [],
            }
        )
        outbox = FakeCustomMessageOutbox()
        await outbox.enqueue(
            user_id='user-1',
            session_id='session-1',
            event_id='report-signed-1',
            message={
                'displayType': 'CARD',
                'customType': 'HEALTH_REPORT_CARD',
                'content': '您的健康报告已经由医生审核完成。',
                'payload': {'reportId': 'report-1'},
            },
        )
        manager, _, _, _ = self._manager(
            collection,
            custom_message_outbox=outbox,
        )

        await manager.sendMessage('user-1', 'session-1', '请分析我的情况')

        self.assertEqual(
            [message['role'] for message in collection.document['messages']],
            ['user', 'assistant', 'custom'],
        )
        self.assertEqual(
            collection.document['messages'][-1]['eventId'],
            'report-signed-1',
        )
        self.assertEqual(outbox.events[0]['status'], 'delivered')

    async def test_custom_message_is_excluded_when_rebuilding_llm_context(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:01:00',
                'agentThreadId': 'agent-thread-1',
                'agentContextInitialized': False,
                'messages': [
                    {'role': 'user', 'content': '旧问题'},
                    {'role': 'assistant', 'content': '旧回答'},
                    {
                        'role': 'custom',
                        'displayType': 'CARD',
                        'customType': 'HEALTH_REPORT_CARD',
                        'title': '健康评估报告',
                        'content': '您的健康报告已由医生审核完成。',
                        'eventId': 'report-signed-1',
                        'payload': {'reportId': 'report-1'},
                    },
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        await manager.sendMessage('user-1', 'session-1', '新的追问')

        agent_input = graph.calls[0]['input']['user_message']
        self.assertIn('用户: 旧问题', agent_input)
        self.assertIn('助手: 旧回答', agent_input)
        self.assertIn('用户: 新的追问', agent_input)
        self.assertNotIn('您的健康报告已由医生审核完成。', agent_input)
        self.assertNotIn('健康评估报告', agent_input)
        self.assertNotIn('report-1', agent_input)

    async def test_personalized_suggestions_use_agent_hub_and_keep_parsing(
        self,
    ):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'qa-session-1',
                'type': 'qa',
                'status': 'completed',
                'extractedInfo': {'gender': '女', 'age': 42},
                'qaHistory': [
                    {
                        'question': {'question': '您的睡眠如何？'},
                        'answer': ['较差'],
                    }
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        suggestions = await manager.getSuggestions('user-1', count=2)

        self.assertEqual(
            suggestions,
            [
                '如何改善睡眠？',
                '饮食方面需要注意什么？',
            ],
        )
        call = graph.calls[0]
        self.assertIn('性别：女、年龄：42岁', call['input']['prompt'])
        self.assertIn('您的睡眠如何？ → 较差', call['input']['prompt'])
        self.assertIn('请生成2个推荐问题', call['input']['prompt'])
        self.assertEqual(call['context'].user_id, 'user-1')
        self.assertEqual(call['context'].session_id, 'qa-session-1')
        self.assertEqual(
            call['config'],
            {'configurable': {'thread_id': call['context'].run_id}},
        )

    async def test_user_without_completed_qa_uses_random_fallback(self):
        collection = FakeSessionCollection()
        manager, graph, _, _ = self._manager(collection)

        with patch.object(
            manager,
            '_getRandomSuggestions',
            return_value=['通用问题一', '通用问题二'],
        ) as random_suggestions:
            suggestions = await manager.getSuggestions('user-1', count=2)

        self.assertEqual(suggestions, ['通用问题一', '通用问题二'])
        random_suggestions.assert_called_once_with(2)
        self.assertEqual(graph.calls, [])

    async def test_suggestions_agent_failure_uses_random_fallback(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'qa-session-1',
                'type': 'qa',
                'status': 'completed',
                'qaHistory': [],
            }
        )
        manager, _, _, _ = self._manager(collection)

        with (
            patch.object(
                manager,
                '_generateSuggestionsByAI',
                side_effect=RuntimeError('model unavailable'),
            ),
            patch.object(
                manager,
                '_getRandomSuggestions',
                return_value=['回退问题'],
            ) as random_suggestions,
        ):
            suggestions = await manager.getSuggestions('user-1', count=1)

        self.assertEqual(suggestions, ['回退问题'])
        random_suggestions.assert_called_once_with(1)

    async def test_new_session_uses_agent_hub_and_preserves_api_result(self):
        collection = FakeSessionCollection()
        manager, graph, lease_manager, _ = self._manager(collection)

        result = await manager.sendMessage(
            'user-1',
            'session-1',
            '我最近睡眠不好',
            thinking=False,
            reasoning_effort='max',
        )

        self.assertEqual(result['userId'], 'user-1')
        self.assertEqual(result['sessionId'], 'session-1')
        self.assertEqual(result['message'], '我最近睡眠不好')
        self.assertEqual(result['reply'], 'Agent 模型回复')
        self.assertEqual(result['reasoning'], 'Agent 推理')

        saved = collection.document
        UUID(saved['agentThreadId'])
        self.assertTrue(saved['agentContextInitialized'])
        self.assertEqual(
            [message['role'] for message in saved['messages']],
            ['user', 'assistant'],
        )
        UUID(saved['messages'][0]['messageId'])
        UUID(saved['messages'][1]['messageId'])
        self.assertEqual(
            [message['messageRevision'] for message in saved['messages']],
            [1, 1],
        )

        call = graph.calls[0]
        self.assertEqual(
            call['input'],
            {
                'agent_thread_id': saved['agentThreadId'],
                'user_message': '我最近睡眠不好',
                'retrieval_query': '我最近睡眠不好',
                'thinking': False,
                'reasoning_effort': 'max',
                'stream': False,
            },
        )
        self.assertEqual(
            call['config'],
            {'configurable': {'thread_id': saved['agentThreadId']}},
        )
        self.assertEqual(call['context'].user_id, 'user-1')
        self.assertEqual(call['context'].session_id, 'session-1')
        self.assertEqual(
            lease_manager.acquire_calls,
            [('business-session:6:user-1:session-1', 'qa-gate-owner')],
        )
        self.assertEqual(len(lease_manager.release_calls), 1)

    async def test_saved_user_message_is_queued_for_profile_ingestion(self):
        collection = FakeSessionCollection()
        profile_source = FakeChatProfileSource()
        manager, _graph, _lease_manager, _repository = self._manager(
            collection,
            chat_profile_source=profile_source,
        )

        await manager.sendMessage(
            'user-1',
            'session-1',
            '我最近经常头痛',
        )

        user_message = collection.document['messages'][0]
        self.assertEqual(
            profile_source.prepare_calls,
            [
                ('session-1', user_message['messageId']),
            ],
        )
        self.assertEqual(
            profile_source.queue_calls,
            [
                ('user-1', user_message['messageId']),
            ],
        )
        self.assertEqual(
            collection.document['chatProfileIngestion'][0]['status'],
            'pending',
        )

    async def test_complete_injects_profile_memory_only_through_run_context(
        self,
    ):
        collection = FakeSessionCollection()
        memory = FakeHealthProfileMemoryProvider('长期画像：已确认身高 168cm')
        manager, graph, _, _ = self._manager(
            collection,
            health_profile_memory_provider=memory,
        )

        result = await manager.sendMessage(
            'user-1',
            'session-new',
            '请给我运动建议',
        )

        self.assertEqual(memory.calls, ['user-1'])
        self.assertEqual(
            graph.calls[0]['context'].health_profile_context,
            '长期画像：已确认身高 168cm',
        )
        self.assertNotIn('health_profile_context', graph.calls[0]['input'])
        self.assertNotIn('长期画像', str(result))
        self.assertNotIn('长期画像', str(collection.document))

    async def test_complete_injects_report_only_through_run_context(self):
        collection = FakeSessionCollection()
        reports = FakeReportContextProvider()
        manager, graph, _, _ = self._manager(
            collection,
            report_context_provider=reports,
        )

        result = await manager.sendMessage(
            'user-1',
            'session-new',
            '请解释评估报告',
            final_report_id='rpt-1',
            report_type='evaluate',
        )

        self.assertEqual(len(reports.calls), 1)
        self.assertEqual(reports.calls[0]['final_report_id'], 'rpt-1')
        self.assertEqual(reports.calls[0]['report_type'], 'evaluate')
        self.assertEqual(
            graph.calls[0]['context'].report_context,
            '当前报告上下文：仅用于本轮追问',
        )
        self.assertNotIn('report_context', graph.calls[0]['input'])
        self.assertEqual(
            collection.document['reportReference'],
            {
                'finalReportId': 'rpt-1',
                'reportType': 'evaluate',
            },
        )
        self.assertNotIn('当前报告上下文', str(result))
        self.assertNotIn('当前报告上下文', str(collection.document))

    async def test_profile_memory_failure_degrades_to_normal_chat(self):
        collection = FakeSessionCollection()
        memory = FakeHealthProfileMemoryProvider(
            error=RuntimeError('profile database unavailable'),
        )
        manager, graph, _, _ = self._manager(
            collection,
            health_profile_memory_provider=memory,
        )

        result = await manager.sendMessage(
            'user-1',
            'session-1',
            '继续聊天',
        )

        self.assertEqual(result['reply'], 'Agent 模型回复')
        self.assertEqual(memory.calls, ['user-1'])
        self.assertIsNone(
            graph.calls[0]['context'].health_profile_context,
        )

    async def test_legacy_session_seeds_first_agent_context_from_history(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-01T00:00:00',
                'lastMessageAt': '2026-08-01T00:01:00',
                'messages': [
                    {'role': 'user', 'content': '旧问题'},
                    {'role': 'assistant', 'content': '旧回答'},
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        await manager.sendMessage('user-1', 'session-1', '新的追问')

        agent_input = graph.calls[0]['input']['user_message']
        self.assertIn('用户: 旧问题', agent_input)
        self.assertIn('助手: 旧回答', agent_input)
        self.assertIn('用户: 新的追问', agent_input)
        self.assertTrue(collection.document['agentContextInitialized'])

    async def test_initialized_agent_context_only_sends_current_turn(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-01T00:00:00',
                'lastMessageAt': '2026-08-01T00:01:00',
                'agentThreadId': 'agent-thread-1',
                'agentContextInitialized': True,
                'messages': [
                    {'role': 'user', 'content': '旧问题'},
                    {'role': 'assistant', 'content': '旧回答'},
                ],
            }
        )
        manager, graph, _, context_repository = self._manager(
            collection,
            context_snapshots=[
                LLMContextSnapshot(
                    key='chat.reply:agent-thread-1',
                    revision=1,
                    payload={'version': 1},
                )
            ],
        )

        await manager.sendMessage('user-1', 'session-1', '只发送这一轮')

        self.assertEqual(
            graph.calls[0]['input']['user_message'],
            '只发送这一轮',
        )
        self.assertEqual(
            context_repository.load_calls,
            ['chat.reply:agent-thread-1'],
        )

    async def test_missing_agent_context_is_rebuilt_from_business_history(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-01T00:00:00',
                'lastMessageAt': '2026-08-01T00:01:00',
                'agentThreadId': 'agent-thread-1',
                'agentContextInitialized': True,
                'messages': [
                    {'role': 'user', 'content': '可重建的旧问题'},
                    {'role': 'assistant', 'content': '可重建的旧回答'},
                ],
            }
        )
        manager, graph, _, context_repository = self._manager(collection)

        await manager.sendMessage('user-1', 'session-1', 'Context 丢失后的追问')

        agent_input = graph.calls[0]['input']['user_message']
        self.assertIn('用户: 可重建的旧问题', agent_input)
        self.assertIn('助手: 可重建的旧回答', agent_input)
        self.assertIn('用户: Context 丢失后的追问', agent_input)
        self.assertEqual(
            context_repository.load_calls,
            ['chat.reply:agent-thread-1'],
        )

    async def test_busy_agent_thread_rejects_overlapping_run(self):
        collection = FakeSessionCollection()
        manager, graph, lease_manager, _ = self._manager(
            collection,
            lease_available=False,
        )

        with self.assertRaisesRegex(ValueError, '正在处理另一个请求'):
            await manager.sendMessage('user-1', 'session-1', '并发消息')

        self.assertEqual(graph.calls, [])
        self.assertEqual(lease_manager.release_calls, [])
        self.assertEqual(collection.replace_calls, [])

    async def test_pending_qa_session_cannot_become_new_chat(self):
        collection = FakeSessionCollection()
        manager, graph, _, _ = self._manager(collection, qa_pending=True)

        with self.assertRaisesRegex(ValueError, '问答会话未完成'):
            await manager.sendMessage('user-1', 'session-1', '并发进入 Chat')

        self.assertEqual(graph.calls, [])
        self.assertEqual(collection.replace_calls, [])

    async def test_stream_pending_qa_session_emits_error_without_chat_write(self):
        collection = FakeSessionCollection()
        manager, _, lease_manager, _ = self._manager(
            collection,
            qa_pending=True,
        )

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'session-1',
                '并发进入流式 Chat',
            )
        ]

        self.assertEqual(events[0]['code'], 400)
        self.assertIn('问答会话未完成', events[0]['data']['error'])
        self.assertEqual(collection.replace_calls, [])
        self.assertEqual(len(lease_manager.release_calls), 1)

    async def test_stream_uses_agent_hub_and_preserves_sse_contract(self):
        collection = FakeSessionCollection()
        manager, graph, lease_manager, _ = self._manager(collection)

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'session-1',
                '请流式回答',
                thinking=False,
                reasoning_effort='max',
            )
        ]

        self.assertEqual(
            [event['data']['type'] for event in events],
            ['start', 'reasoning', 'reasoning', 'content', 'content', 'finish'],
        )
        self.assertEqual(
            [event['data']['content'] for event in events if event['data']['type'] == 'reasoning'],
            ['Agent 推理一', 'Agent 推理二'],
        )
        self.assertEqual(
            [event['data']['content'] for event in events if event['data']['type'] == 'content'],
            ['Agent 正文一', 'Agent 正文二'],
        )

        saved = collection.document
        UUID(saved['agentThreadId'])
        self.assertTrue(saved['agentContextInitialized'])
        self.assertEqual(
            [message['role'] for message in saved['messages']],
            ['user', 'assistant'],
        )
        self.assertEqual(
            saved['messages'][1]['content'],
            'Agent 正文一Agent 正文二',
        )
        self.assertEqual(
            saved['messages'][1]['reasoning'],
            'Agent 推理一Agent 推理二',
        )

        call = graph.stream_calls[0]
        self.assertEqual(
            call['input'],
            {
                'agent_thread_id': saved['agentThreadId'],
                'user_message': '请流式回答',
                'retrieval_query': '请流式回答',
                'thinking': False,
                'reasoning_effort': 'max',
                'stream': True,
            },
        )
        self.assertEqual(
            call['config'],
            {'configurable': {'thread_id': saved['agentThreadId']}},
        )
        self.assertEqual(call['context'].request_id, events[0]['uuid'])
        self.assertEqual(
            call['kwargs'],
            {
                'stream_mode': 'custom',
                'version': 'v2',
            },
        )
        self.assertEqual(len(lease_manager.release_calls), 1)

    async def test_stream_injects_report_only_through_run_context(self):
        collection = FakeSessionCollection()
        reports = FakeReportContextProvider()
        manager, graph, _, _ = self._manager(
            collection,
            report_context_provider=reports,
        )

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'session-new',
                '请流式解释评估报告',
                final_report_id='rpt-1',
                report_type='evaluate',
            )
        ]

        self.assertEqual(events[-1]['data']['type'], 'finish')
        self.assertEqual(len(reports.calls), 1)
        self.assertEqual(
            graph.stream_calls[0]['context'].report_context,
            '当前报告上下文：仅用于本轮追问',
        )
        self.assertNotIn('report_context', graph.stream_calls[0]['input'])
        self.assertEqual(
            collection.document['reportReference'],
            {
                'finalReportId': 'rpt-1',
                'reportType': 'evaluate',
            },
        )
        self.assertNotIn(
            '当前报告上下文',
            str(collection.document),
        )

    async def test_stream_saved_user_message_is_queued_for_profile(self):
        collection = FakeSessionCollection()
        profile_source = FakeChatProfileSource()
        manager, _graph, _lease_manager, _repository = self._manager(
            collection,
            chat_profile_source=profile_source,
        )

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'session-1',
                '我最近睡眠很差',
            )
        ]

        self.assertEqual(events[-1]['data']['type'], 'finish')
        user_message = collection.document['messages'][0]
        self.assertEqual(
            profile_source.prepare_calls,
            [
                ('session-1', user_message['messageId']),
            ],
        )
        self.assertEqual(
            profile_source.queue_calls,
            [
                ('user-1', user_message['messageId']),
            ],
        )

    async def test_stream_injects_the_same_profile_memory_context(self):
        collection = FakeSessionCollection()
        memory = FakeHealthProfileMemoryProvider('长期画像：待确认睡眠不佳')
        manager, graph, _, _ = self._manager(
            collection,
            health_profile_memory_provider=memory,
        )

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'another-session',
                '今天应该怎么安排？',
            )
        ]

        self.assertEqual(events[-1]['data']['type'], 'finish')
        self.assertEqual(memory.calls, ['user-1'])
        self.assertEqual(
            graph.stream_calls[0]['context'].health_profile_context,
            '长期画像：待确认睡眠不佳',
        )
        self.assertNotIn(
            'health_profile_context',
            graph.stream_calls[0]['input'],
        )

    async def test_stream_completion_drains_card_after_assistant_message(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'session-1',
                'type': 'chat',
                'status': 'active',
                'createdAt': '2026-08-14T00:00:00',
                'lastMessageAt': '2026-08-14T00:00:00',
                'messages': [],
            }
        )
        outbox = FakeCustomMessageOutbox()
        await outbox.enqueue(
            user_id='user-1',
            session_id='session-1',
            event_id='report-signed-stream-1',
            message={
                'displayType': 'CARD',
                'customType': 'HEALTH_REPORT_CARD',
                'content': '医生已签发健康报告。',
                'payload': {'reportId': 'report-stream-1'},
            },
        )
        manager, _, _, _ = self._manager(
            collection,
            custom_message_outbox=outbox,
        )

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'session-1',
                '请流式分析',
            )
        ]

        self.assertEqual(events[-1]['data']['type'], 'finish')
        self.assertEqual(
            [message['role'] for message in collection.document['messages']],
            ['user', 'assistant', 'custom'],
        )
        self.assertEqual(
            collection.document['messages'][-1]['eventId'],
            'report-signed-stream-1',
        )
        self.assertEqual(outbox.events[0]['status'], 'delivered')

    async def test_qa_gate_storage_error_fails_closed(self):
        collection = FakeSessionCollection()
        manager, graph, _, _ = self._manager(
            collection,
            qa_gate_error=RuntimeError('redis unavailable'),
        )

        with self.assertRaisesRegex(RuntimeError, 'redis unavailable'):
            await manager.sendMessage('user-1', 'session-1', '不能绕过门禁')

        self.assertEqual(graph.calls, [])

    def test_qa_summary_keeps_first_formal_question(self):
        collection = FakeSessionCollection()
        manager, _, _, _ = self._manager(collection)

        summary = manager._buildQASummary(
            {
                'qaHistory': [
                    {'question': {'question': '第一道正式问题'}, 'answer': ['答案一']},
                    {'question': {'question': '第二道正式问题'}, 'answer': ['答案二']},
                ],
            }
        )

        self.assertIn('第一道正式问题', summary)
        self.assertIn('第二道正式问题', summary)

    def test_qa_handoff_contains_reports_and_attached_data_without_file_url(
        self,
    ):
        collection = FakeSessionCollection()
        manager, _, _, _ = self._manager(collection)

        summary = manager._buildQASummary(
            {
                'extractedInfo': {'name': '测试用户', 'gender': '女', 'age': 42},
                'parsedHealthReports': [
                    {
                        'reportId': 'qa-report-1',
                        'title': '血液生化检验报告',
                        'reportTime': '2026-08-18',
                        'reportType': 'lab',
                        'status': 'abnormal',
                        'aiSummary': '尿酸高于参考范围',
                        'expansion': [
                            {
                                'itemName': '尿酸',
                                'resultValue': '468',
                                'unit': 'μmol/L',
                                'flag': '偏高',
                            }
                        ],
                        'fileId': 'https://example.test/private-report.pdf',
                    }
                ],
                'healthData': {
                    'fileList': [
                        {
                            'url': 'https://example.test/private-report.pdf',
                        }
                    ],
                    'hospitalData': [{'diagnosis': '高血压'}],
                    'weRunData': [{'date': '2026-08-17', 'steps': 8200}],
                    'smartDeviceData': {'restingHeartRate': 72},
                    'doctorProductPackages': [{'packageName': '随访服务'}],
                },
                'qaHistory': [
                    {
                        'question': {'question': '近期是否有头晕？'},
                        'answer': '偶尔头晕',
                    }
                ],
            }
        )

        self.assertIn('血液生化检验报告', summary)
        self.assertIn('尿酸', summary)
        self.assertIn('高血压', summary)
        self.assertIn('8200', summary)
        self.assertIn('restingHeartRate', summary)
        self.assertIn('仅表示服务配置，不代表健康事实', summary)
        self.assertIn('近期是否有头晕', summary)
        self.assertNotIn('private-report.pdf', summary)

    def test_qa_handoff_summary_has_a_hard_total_limit(self):
        collection = FakeSessionCollection()
        manager, _, _, _ = self._manager(collection)

        summary = manager._buildQASummary(
            {
                'extractedInfo': {'note': '基本信息' * 2000},
                'parsedHealthReports': [
                    {
                        'reportId': f'report-{index}',
                        'title': f'报告{index}',
                        'expansion': [{'content': '报告内容' * 5000}],
                    }
                    for index in range(5)
                ],
                'healthData': {
                    'hospitalData': [{'content': '院内数据' * 5000}],
                },
                'qaHistory': [
                    {
                        'question': {'question': f'第{index}道正式问题'},
                        'answer': '回答内容' * 1000,
                    }
                    for index in range(10)
                ],
            }
        )

        self.assertLessEqual(len(summary), 16 * 1024)
        self.assertIn('报告0', summary)
        self.assertIn('报告4', summary)
        self.assertIn('第0道正式问题', summary)

    async def test_completed_qa_seeds_first_chat_turn_with_handoff_summary(
        self,
    ):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'qa-session-1',
                'type': 'qa',
                'status': 'completed',
                'createdAt': '2026-08-18T09:00:00',
                'completedAt': '2026-08-18T10:00:00',
                'extractedInfo': {'gender': '女', 'age': 42},
                'parsedHealthReports': [
                    {
                        'reportId': 'qa-report-1',
                        'title': '血液生化检验报告',
                        'reportType': 'lab',
                        'status': 'abnormal',
                        'expansion': [
                            {
                                'itemName': '尿酸',
                                'resultValue': '468',
                                'unit': 'μmol/L',
                            }
                        ],
                    }
                ],
                'healthData': {
                    'weRunData': [{'date': '2026-08-17', 'steps': 8200}],
                },
                'qaHistory': [
                    {
                        'question': {'question': '近期是否有头晕？'},
                        'answer': '偶尔头晕',
                    }
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        await manager.sendMessage(
            'user-1',
            'qa-session-1',
            '请结合刚才的建档继续分析',
        )

        agent_input = graph.calls[0]['input']['user_message']
        self.assertIn('本次问答建档的交接信息', agent_input)
        self.assertIn('血液生化检验报告', agent_input)
        self.assertIn('尿酸', agent_input)
        self.assertIn('8200', agent_input)
        self.assertIn('近期是否有头晕', agent_input)
        self.assertIn('请结合刚才的建档继续分析', agent_input)
        self.assertEqual(collection.document['type'], 'chat')
        self.assertTrue(collection.document['agentContextInitialized'])
        self.assertEqual(
            [message['role'] for message in collection.document['messages']],
            ['system', 'user', 'assistant'],
        )

    async def test_completed_qa_stream_uses_the_same_handoff_summary(self):
        collection = FakeSessionCollection(
            {
                'userId': 'user-1',
                'sessionId': 'qa-session-stream',
                'type': 'qa',
                'status': 'completed',
                'createdAt': '2026-08-18T09:00:00',
                'completedAt': '2026-08-18T10:00:00',
                'parsedHealthReports': [
                    {
                        'reportId': 'qa-report-stream',
                        'title': '流式交接报告',
                        'reportType': 'lab',
                        'status': 'abnormal',
                        'expansion': [
                            {
                                'itemName': '总胆固醇',
                                'resultValue': '6.2',
                                'unit': 'mmol/L',
                            }
                        ],
                    }
                ],
                'qaHistory': [
                    {
                        'question': {'question': '近期饮食是否油腻？'},
                        'answer': '经常外卖',
                    }
                ],
            }
        )
        manager, graph, _, _ = self._manager(collection)

        events = [
            event
            async for event in manager.sendMessageStream(
                'user-1',
                'qa-session-stream',
                '请继续说明',
            )
        ]

        self.assertEqual(events[-1]['data']['type'], 'finish')
        agent_input = graph.stream_calls[0]['input']['user_message']
        self.assertIn('流式交接报告', agent_input)
        self.assertIn('总胆固醇', agent_input)
        self.assertIn('近期饮食是否油腻', agent_input)
        self.assertIn('请继续说明', agent_input)
        self.assertEqual(collection.document['type'], 'chat')
        self.assertTrue(collection.document['agentContextInitialized'])


if __name__ == '__main__':
    unittest.main()
