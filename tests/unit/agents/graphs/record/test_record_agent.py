"""健康建档完整与流式 Agent 能力的无网络测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from adapters.persistence import MongoRecordSessionRepository
from adapters.persistence import RedisRecordSessionCache
from adapters.record import AionPromptRenderer
from adapters.record.documents import LegacyRecordDocumentAdapter
from adapters.record.documents import legacy_record_report_id
from agents import AgentEventType
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.record import RECORD_PLAN_CAPABILITY
from agents.graphs.record import RECORD_QUESTION_CAPABILITY
from agents.graphs.record import build_record_question_prompt_variables
from agents.graphs.record import create_record_plan_profile
from agents.graphs.record import create_record_question_profile
from agents.graphs.record import register_record_graphs
from agents.knowledge import KnowledgeDocument
from agents.knowledge import KnowledgeSource
from agents.llm import ContextPolicy
from agents.llm import LLMChunk
from agents.llm import LLMResult
from agents.llm import ModelProfileRegistry
from tests.unit._support.agents import FakeKnowledgeGateway
from tests.unit._support.agents import FakeLLMGateway

PROJECT_ROOT = Path(__file__).resolve().parents[5]
QUESTION_TOPICS = {
    'topics': [
        {'category': '睡眠', 'description': '睡眠质量和时长'},
        {'category': '运动', 'description': '运动频率和强度'},
    ],
    'rules': ['避免重复', '提供清晰选项'],
}


def _stub_module(name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


class FakeReplaceResult:
    acknowledged = True


class FakeCollection:
    def __init__(self, document=None) -> None:
        self.document = document

    async def find_one(self, filter, **kwargs):
        return self.document

    async def replace_one(self, filter, document, upsert=False):
        self.document = document
        return FakeReplaceResult()


class FakeRedis:
    def __init__(self, value=None):
        self.last_value = None
        self.delete_calls = []
        self.value = value

    async def get(self, key):
        return self.value

    async def setex(self, key, ttl, value):
        self.last_value = value
        self.value = value
        return True

    async def delete(self, key):
        self.delete_calls.append(key)
        self.value = None
        return 1


class FakeLeaseManager:
    def __init__(self) -> None:
        self.acquire_calls = []
        self.release_calls = []

    async def try_acquire(self, thread_id, owner_id):
        self.acquire_calls.append((thread_id, owner_id))
        return SimpleNamespace(key=f'lease:{thread_id}', owner_id=owner_id)

    async def renew(self, lease):
        return True

    async def release(self, lease):
        self.release_calls.append(lease)
        return True


class FakeRecordProfileSource:
    def __init__(self) -> None:
        self.prepared = []
        self.queued = []

    def prepare_completed_snapshot(self, session) -> None:
        session['qaSourceRevision'] = 1
        session['qaProfileIngestion'] = {'status': 'pending'}
        self.prepared.append(session)

    async def ensure_queued(self, *, user_id, session_id):
        self.queued.append((user_id, session_id))


class FakeHealthReportProcessingService:
    def __init__(self, results=None, *, errors=None, delay=0) -> None:
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            file_id = kwargs['file_id']
            if file_id in self.errors:
                raise self.errors[file_id]
            result = dict(
                self.results.get(
                    file_id,
                    {
                        'reportType': 'lab',
                        'title': '测试报告',
                        'reportTime': '2026-08-18',
                        'aiSummary': '未见明显异常',
                        'status': 'normal',
                        'expansion': [],
                    },
                )
            )
            result.update(
                {
                    'reportId': kwargs['report_id'],
                    'userId': kwargs['user_id'],
                    'subjectId': 'subject-self-1',
                }
            )
            return result
        finally:
            self.active -= 1


class FakeHealthProfileMemoryProvider:
    def __init__(self, context=None, *, error=None) -> None:
        self.context = context
        self.error = error
        self.calls = []

    async def build_context(self, user_id):
        self.calls.append(user_id)
        if self.error is not None:
            raise self.error
        return self.context


def _load_record_service(*, health_report=None):
    collection_sessions = FakeCollection()
    redis_client = FakeRedis()
    stubs = {
        'config.env': _stub_module(
            'config.env',
            LLM_API_KEY='unused-test-key',
            LLM_DEFAULT_MODEL='unused-test-model',
            LOG_LEVEL='CRITICAL',
        ),
    }
    spec = importlib.util.spec_from_file_location(
        '_shulan_record_agent_service_target',
        PROJECT_ROOT / 'modules' / 'record' / 'application' / 'service.py',
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    manager_type = module.QASessionManager

    class InjectedQASessionManager(manager_type):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault(
                'session_repository',
                MongoRecordSessionRepository(collection_sessions),
            )
            kwargs.setdefault(
                'session_cache',
                RedisRecordSessionCache(redis_client),
            )
            kwargs.setdefault('prompt_renderer', AionPromptRenderer())
            if 'health_report_processing_service' in kwargs or 'health_report_processing_service_factory' in kwargs:
                kwargs['record_document_port'] = LegacyRecordDocumentAdapter(
                    processor=kwargs.pop('health_report_processing_service', None),
                    processor_factory=kwargs.pop('health_report_processing_service_factory', None),
                )
            super().__init__(*args, **kwargs)

    module.QASessionManager = InjectedQASessionManager
    # Keep the existing assertions focused on raw persistence effects while
    # construction itself now goes through explicit application ports.
    module.collection_sessions = collection_sessions
    module.redis_client = redis_client
    return module


def _build_runtime(gateway, *, knowledge_gateway=None):
    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=FakeLeaseManager(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
        **({'knowledge_gateway': knowledge_gateway} if knowledge_gateway is not None else {}),
    )
    register_record_graphs(runtime, question_topics=QUESTION_TOPICS)
    return runtime


class RecordProfileTest(unittest.TestCase):
    def test_plan_and_question_profiles_are_stateless(self) -> None:
        variables = build_record_question_prompt_variables(QUESTION_TOPICS)
        plan = create_record_plan_profile(model=' model-1 ')
        question = create_record_question_profile(
            prompt_variables=variables,
            model=' model-1 ',
        )

        self.assertIs(plan.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(plan.prompt_name, 'record/plan.txt')
        self.assertFalse(plan.thinking)
        self.assertIs(question.context_policy, ContextPolicy.STATELESS)
        self.assertEqual(question.prompt_name, 'record/question.txt')
        self.assertFalse(question.thinking)
        self.assertEqual(question.model_kwargs, {'response_format': {'type': 'json_object'}})
        self.assertIn('- 睡眠: 睡眠质量和时长', variables['topics_text'])
        self.assertEqual(
            variables['rules_text'],
            '1. 避免重复\n2. 提供清晰选项',
        )


class RecordGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_graphs_forward_stateless_requests(self) -> None:
        gateway = FakeLLMGateway(
            complete_result=LLMResult(
                content='5',
                reasoning_content='规划推理',
            ),
        )
        runtime = _build_runtime(gateway)

        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-1',
            run_id='run-1',
        )
        result = await runtime.hub.invoke(
            RECORD_PLAN_CAPABILITY,
            AgentRequest(
                {
                    'prompt': '请规划问题数量',
                    'thinking': False,
                }
            ),
            context,
        )

        self.assertEqual(result.data['content'], '5')
        self.assertEqual(result.data['reasoning_content'], '')
        request = gateway.complete_requests[0]
        self.assertEqual(request.profile.name, 'record.plan')
        self.assertIsNone(request.conversation_key)
        self.assertFalse(request.effective_thinking)
        self.assertEqual(
            runtime.registry.capabilities(),
            ('record.plan', 'record.question'),
        )
        self.assertIsNone(
            runtime.registry.get(RECORD_PLAN_CAPABILITY).graph.checkpointer,
        )
        self.assertIsNone(
            runtime.registry.get(RECORD_QUESTION_CAPABILITY).graph.checkpointer,
        )

    async def test_question_graph_streams_through_gateway_events(self) -> None:
        gateway = FakeLLMGateway(
            stream_chunks=[
                LLMChunk(reasoning_content='推理一'),
                LLMChunk(reasoning_content='推理二'),
                LLMChunk(content='{"questionId":"q_sleep",'),
                LLMChunk(content='"questionType":"radio"}'),
            ]
        )
        runtime = _build_runtime(gateway)
        context = AgentRunContext.create(
            user_id='user-1',
            session_id='session-1',
            request_id='request-stream-1',
            run_id='run-stream-1',
        )

        events = [
            event
            async for event in runtime.hub.stream(
                RECORD_QUESTION_CAPABILITY,
                AgentRequest(
                    {
                        'prompt': '请生成下一题',
                        'thinking': False,
                        'stream': True,
                    }
                ),
                context,
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.RUN_STARTED,
                AgentEventType.CONTENT_DELTA,
                AgentEventType.CONTENT_DELTA,
                AgentEventType.RUN_COMPLETED,
            ],
        )
        self.assertEqual(
            [event.data.get('content') for event in events[1:3]],
            [
                '{"questionId":"q_sleep",',
                '"questionType":"radio"}',
            ],
        )
        self.assertEqual(gateway.complete_requests, [])
        request = gateway.stream_requests[0]
        self.assertEqual(request.profile.name, RECORD_QUESTION_CAPABILITY)
        self.assertIsNone(request.conversation_key)
        self.assertFalse(request.effective_thinking)

    async def test_question_graph_retrieves_evidence_when_router_requires_it(self) -> None:
        gateway = FakeLLMGateway(
            complete_results=[
                LLMResult(
                    content=('{"needRetrieval":true,"queries":[{"source":"guide","query":"血脂异常 生活方式"}]}')
                ),
                LLMResult(
                    content=(
                        '{"questionId":"q_diet","questionType":"radio",'
                        '"question":"您日常饮食中高脂食物的频率如何？",'
                        '"options":["很少","偶尔","经常"]}'
                    )
                ),
            ]
        )
        knowledge = FakeKnowledgeGateway(
            {
                KnowledgeSource.GUIDE: [
                    KnowledgeDocument(
                        source=KnowledgeSource.GUIDE,
                        title='血脂管理指南',
                        content='建议评估饮食结构和运动情况。',
                    )
                ],
            }
        )
        runtime = _build_runtime(
            gateway,
            knowledge_gateway=knowledge,
        )

        result = await runtime.hub.invoke(
            RECORD_QUESTION_CAPABILITY,
            AgentRequest(
                {
                    'prompt': '体检报告显示血脂异常，请生成下一题。',
                    'thinking': False,
                }
            ),
            AgentRunContext.create(
                user_id='user-1',
                session_id='session-kb',
                request_id='request-kb',
                run_id='run-kb',
            ),
        )

        self.assertIn('q_diet', result.data['content'])
        self.assertEqual(result.data['retrieval_status'], 'retrieved')
        self.assertEqual(len(gateway.complete_requests), 2)
        self.assertIn(
            '血脂管理指南',
            gateway.complete_requests[1].supplemental_context,
        )
        self.assertEqual(knowledge.queries[0].source, KnowledgeSource.GUIDE)

    async def test_question_graph_degrades_when_knowledge_is_unavailable(self) -> None:
        gateway = FakeLLMGateway(
            complete_results=[
                LLMResult(content=('{"needRetrieval":true,"queries":[{"source":"guide","query":"血脂异常 指南"}]}')),
                LLMResult(
                    content=(
                        '{"questionId":"q_diet","questionType":"radio",'
                        '"question":"您的饮食习惯如何？",'
                        '"options":["均衡","一般","高脂"]}'
                    )
                ),
            ]
        )
        runtime = _build_runtime(
            gateway,
            knowledge_gateway=FakeKnowledgeGateway(
                error=RuntimeError('temporary knowledge failure'),
            ),
        )

        result = await runtime.hub.invoke(
            RECORD_QUESTION_CAPABILITY,
            AgentRequest(
                {
                    'prompt': '体检报告显示血脂异常，请生成下一题。',
                    'thinking': False,
                }
            ),
            AgentRunContext.create(
                user_id='user-1',
                session_id='session-kb-failure',
                request_id='request-kb-failure',
                run_id='run-kb-failure',
            ),
        )

        self.assertEqual(result.data['retrieval_status'], 'unavailable')
        self.assertIn('q_diet', result.data['content'])
        self.assertIsNone(gateway.complete_requests[1].supplemental_context)


class OpeningGateway(FakeLLMGateway):
    """Valid model contract for retained report-input and provenance regressions."""

    def __init__(self, *args, **kwargs):
        super().__init__(
            complete_results=[
                LLMResult(
                    content=json.dumps(
                        {
                            'intent': 'answer',
                            'substantive': False,
                            'intakeSufficient': False,
                            'intake': {'complaints': [], 'priorityClarificationNeeded': False, 'facts': []},
                            'summary': '需要了解用户当前主要困扰',
                            'concerns': [],
                            'gaps': [],
                        },
                        ensure_ascii=False,
                    ),
                    reasoning_content='需要了解用户当前主要困扰',
                ),
                LLMResult(
                    content=json.dumps(
                        {
                            'assistantMessage': {
                                'acknowledgement': '先了解您目前的主要困扰。',
                                'rationale': '从实际感受了解当前关注方向。',
                                'evidence': [],
                            },
                            'responses': [],
                            'question': {
                                'question': '最近有哪些身体感受或变化让您在意？可多选。',
                                'questionType': 'checkbox',
                                'options': [
                                    '便秘',
                                    '反酸',
                                    '入睡困难',
                                    '容易疲劳',
                                    '减轻体重',
                                ],
                            },
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )

    async def complete(self, request):
        result = await super().complete(request)
        if request.profile.name != 'record.question':
            return result
        payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
        report = payload['patient']['本次报告']
        if not report:
            return result
        value = json.loads(result.content)
        quote = report[:100]
        value['assistantMessage'] = {
            'acknowledgement': '本次报告记录：' + quote,
            'rationale': '结合资料了解您当前的消化相关感受。',
            'evidence': [{'source': '本次报告', 'quote': quote}],
        }
        return LLMResult(content=json.dumps(value, ensure_ascii=False))


class RecordServiceAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_uses_fixed_existing_profile_context(self):
        gateway = OpeningGateway(complete_result=LLMResult(content='3'))
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        profile_context = '[用户长期健康画像记忆]\n{"factType":"condition.diagnosis","value":"高血压"}'
        memory = FakeHealthProfileMemoryProvider(profile_context)
        manager = module.QASessionManager(
            runtime,
            health_profile_memory_provider=memory,
        )

        await manager.createSession('user-1', 'session-profile-context')

        cached_session = json.loads(module.redis_client.last_value)
        self.assertEqual(
            cached_session['healthProfileContext'],
            profile_context,
        )
        self.assertEqual(memory.calls, ['user-1'])
        self.assertEqual(len(gateway.complete_requests), 2)
        self.assertIn('condition.diagnosis', gateway.complete_requests[0].content)
        self.assertIn('condition.diagnosis', gateway.complete_requests[1].content)

    async def test_profile_read_failure_degrades_to_record_without_memory(self):
        gateway = OpeningGateway(
            complete_result=LLMResult(
                content=(
                    '{"questionId":"q_sleep","questionType":"radio",'
                    '"question":"您的睡眠如何？","options":["良好","较差"]}'
                ),
            )
        )
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        memory = FakeHealthProfileMemoryProvider(
            error=RuntimeError('profile unavailable'),
        )
        manager = module.QASessionManager(
            runtime,
            health_profile_memory_provider=memory,
        )

        result = await manager.createSession(
            'user-1',
            'session-profile-unavailable',
        )

        self.assertEqual(result['status'], 'question')
        cached_session = json.loads(module.redis_client.last_value)
        self.assertIsNone(cached_session['healthProfileContext'])
        self.assertEqual(memory.calls, ['user-1'])

    async def test_attached_create_data_enters_plan_and_question_context(self):
        gateway = OpeningGateway(complete_result=LLMResult(content='3'))
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        manager = module.QASessionManager(runtime)

        await manager.createSession(
            'user-1',
            'session-attached-data',
            name='测试用户',
            hospital_data=[{'diagnosis': '高血压'}],
            we_run_data=[{'date': '2026-08-17', 'steps': 8200}],
            doctor_product_packages=[{'packageName': '健康管理包'}],
            smart_device_data={'restingHeartRate': 72},
        )

        cached_session = json.loads(module.redis_client.last_value)
        self.assertEqual(
            cached_session['healthData']['hospitalData'],
            [{'diagnosis': '高血压'}],
        )
        self.assertEqual(len(gateway.complete_requests), 2)
        for request in gateway.complete_requests:
            self.assertIn('高血压', request.content)
            self.assertIn('8200', request.content)
            self.assertIn('restingHeartRate', request.content)
            self.assertIn('健康管理包', request.content)
            self.assertIn('不能当作用户健康事实', request.content)

    async def test_create_session_does_not_select_latest_independent_report(self):
        gateway = OpeningGateway(
            complete_result=LLMResult(
                content=(
                    '{"questionId":"q_sleep","questionType":"radio",'
                    '"question":"您的睡眠如何？","options":["良好","较差"]}'
                ),
            )
        )
        runtime = _build_runtime(gateway)
        module = _load_record_service(
            health_report={
                'aiSummary': '这是一份不属于本次建档请求的历史报告',
                'status': 'abnormal',
            }
        )
        manager = module.QASessionManager(runtime)

        await manager.createSession('user-1', 'session-no-report')

        cached_session = json.loads(module.redis_client.last_value)
        self.assertFalse(cached_session['hasHealthReport'])
        self.assertEqual(cached_session['parsedHealthReports'], [])
        self.assertEqual(len(gateway.complete_requests), 2)
        self.assertEqual(
            gateway.complete_requests[0].profile.name,
            RECORD_PLAN_CAPABILITY,
        )
        self.assertNotIn(
            '这是一份不属于本次建档请求的历史报告',
            gateway.complete_requests[0].content,
        )

    async def test_create_session_runs_plan_capability_and_keeps_contract(self):
        gateway = OpeningGateway(
            complete_result=LLMResult(
                content='3',
                reasoning_content='规划推理',
            )
        )
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        processing_service = FakeHealthReportProcessingService(
            {
                'https://example.com/report.pdf': {
                    'reportType': 'lab',
                    'aiSummary': '血脂指标异常',
                    'status': 'abnormal',
                    'expansion': [],
                },
            }
        )
        manager = module.QASessionManager(
            runtime,
            health_report_processing_service=processing_service,
        )

        result = await manager.createSession(
            'user-1',
            'session-1',
            name='测试用户',
            file_list=[{'url': 'https://example.com/report.pdf'}],
            doctor_product_packages=['基础健康包'],
        )

        self.assertEqual(result['status'], 'question')
        self.assertTrue(result['questionId'].startswith('q_'))
        self.assertEqual(result['progress'], '0%')
        self.assertEqual(result['questionType'], 'checkbox')
        self.assertTrue({'便秘', '反酸', '入睡困难', '减轻体重'} <= set(result['options']))
        self.assertEqual(result['options'][-2:], ['暂无改善需求', '不清楚'])
        self.assertLessEqual(len(result['options']), 12)
        self.assertEqual(len(gateway.complete_requests), 2)
        self.assertEqual(
            gateway.complete_requests[0].profile.name,
            RECORD_PLAN_CAPABILITY,
        )
        self.assertEqual(
            gateway.complete_requests[1].profile.name,
            RECORD_QUESTION_CAPABILITY,
        )

    async def test_create_session_parses_multiple_reports_with_limit_two(self):
        gateway = OpeningGateway(complete_result=LLMResult(content='3'))
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        urls = [f'https://example.com/report-{index}.pdf' for index in range(4)]
        processing_service = FakeHealthReportProcessingService(
            {
                url: {
                    'reportType': 'lab',
                    'title': f'报告 {index}',
                    'aiSummary': f'摘要 {index}',
                    'status': 'normal',
                    'expansion': [],
                }
                for index, url in enumerate(urls)
            },
            delay=0.01,
        )
        manager = module.QASessionManager(
            runtime,
            health_report_processing_service=processing_service,
        )

        await manager.createSession(
            'user-1',
            'session-multiple-reports',
            file_list=[{'url': url, 'fileType': 'PDF'} for url in urls],
        )

        cached_session = json.loads(module.redis_client.last_value)
        self.assertTrue(cached_session['hasHealthReport'])
        self.assertEqual(
            [item['aiSummary'] for item in cached_session['parsedHealthReports']],
            ['摘要 0', '摘要 1', '摘要 2', '摘要 3'],
        )
        self.assertEqual(
            [item['fileType'] for item in cached_session['healthData']['fileList']],
            ['pdf', 'pdf', 'pdf', 'pdf'],
        )
        self.assertEqual(processing_service.max_active, 2)
        self.assertEqual(
            [call['file_id'] for call in processing_service.calls],
            urls,
        )
        for call in processing_service.calls:
            self.assertIsNone(call['subject_id'])
            self.assertEqual(call['source_type'], 'record')
            self.assertEqual(call['source_id'], 'session-multiple-reports')
            self.assertEqual(call['session_id'], 'session-multiple-reports')
            self.assertEqual(call['file_index'], urls.index(call['file_id']))
            self.assertEqual(
                call['report_id'],
                legacy_record_report_id(
                    user_id='user-1',
                    file_id=call['file_id'],
                ),
            )

    async def test_create_session_report_failure_does_not_save_qa_session(self):
        gateway = OpeningGateway()
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        failed_url = 'https://example.com/failed.pdf'
        processing_service = FakeHealthReportProcessingService(
            errors={failed_url: RuntimeError('upstream failure')},
        )
        manager = module.QASessionManager(
            runtime,
            health_report_processing_service=processing_service,
        )

        with self.assertRaisesRegex(RuntimeError, 'upstream failure'):
            await manager.createSession(
                'user-1',
                'session-report-failure',
                file_list=[{'url': failed_url}],
            )

        self.assertIsNone(module.redis_client.last_value)
        self.assertEqual(gateway.complete_requests, [])

    def test_file_list_validation_rejects_invalid_or_duplicate_sources(self):
        runtime = _build_runtime(OpeningGateway())
        module = _load_record_service()
        manager = module.QASessionManager(runtime)

        invalid_values = (
            {},
            ['https://example.com/report.pdf'],
            [{'url': ''}],
            [{'url': 'https://example.com/report.pdf'}] * 2,
            [{'url': f'https://example.com/report-{index}.pdf'} for index in range(6)],
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                manager._normalizeFileList(value)

    async def test_image_categories_are_persisted_and_passed_to_processing(self):
        runtime = _build_runtime(OpeningGateway(complete_result=LLMResult(content='3')))
        module = _load_record_service()
        processing = FakeHealthReportProcessingService()
        manager = module.QASessionManager(runtime, health_report_processing_service=processing)
        files = [
            {'url': 'https://example.com/photo.JPG', 'fileType': ' IMAGE '},
            {'url': 'https://example.com/download?objectName=scan.png'},
            {'url': 'https://example.com/download/opaque', 'fileType': 'image'},
        ]
        await manager.createSession('user-1', 'session-images', file_list=files)
        cached = json.loads(module.redis_client.last_value)
        self.assertEqual([item['fileType'] for item in cached['healthData']['fileList']], ['image'] * 3)
        self.assertEqual([call['file_type'] for call in processing.calls], ['image'] * 3)
        self.assertNotIn('fileType', files[1])

    def test_file_types_accept_categories_and_reject_extensions_or_conflicts(self):
        module = _load_record_service()
        normalize = module.QASessionManager._normalizeFileList
        for extension in ('jpg', 'jpeg', 'png', 'bmp', 'gif'):
            for url in (
                f'https://example.com/report.{extension}',
                f'https://example.com/download?objectName=report.{extension}',
            ):
                with self.subTest(url=url):
                    self.assertEqual(normalize([{'url': url}])[0]['fileType'], 'image')
                    self.assertEqual(normalize([{'url': url, 'fileType': 'image'}])[0]['fileType'], 'image')
        for file_type in ('jpg', 'jpeg', 'png', 'bmp', 'gif', 'docx', '', '  ', 1, [], {}):
            with self.subTest(file_type=file_type), self.assertRaises(ValueError):
                normalize([{'url': 'https://example.com/download', 'fileType': file_type}])
        for item in (
            {'url': 'https://example.com/report.pdf', 'fileType': 'image'},
            {'url': 'https://example.com/report.png', 'fileType': 'pdf'},
            {'url': 'https://example.com/download'},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                normalize([item])
        self.assertEqual(normalize([{'url': 'https://example.com/report.pdf'}])[0]['fileType'], 'pdf')
        self.assertEqual(
            normalize([{'url': 'https://example.com/report.png', 'fileType': None}])[0]['fileType'], 'image'
        )
        self.assertEqual(normalize(None), [])
        self.assertEqual(normalize([]), [])

    async def test_invalid_file_type_is_rejected_before_any_report_processing(self):
        runtime = _build_runtime(OpeningGateway())
        module = _load_record_service()
        processing = FakeHealthReportProcessingService()
        manager = module.QASessionManager(runtime, health_report_processing_service=processing)
        with self.assertRaisesRegex(ValueError, '仅支持 pdf 或 image'):
            await manager.createSession(
                'user-1',
                'session-invalid-image',
                file_list=[
                    {'url': 'https://example.com/report.pdf', 'fileType': 'pdf'},
                    {'url': 'https://example.com/report.jpg', 'fileType': 'jpg'},
                ],
            )
        self.assertEqual(processing.calls, [])
        self.assertIsNone(module.redis_client.last_value)

    async def test_create_session_without_products_stores_empty_array(self):
        gateway = OpeningGateway(
            complete_result=LLMResult(
                content='3',
                reasoning_content='规划推理',
            )
        )
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        manager = module.QASessionManager(runtime)

        await manager.createSession('user-1', 'session-without-products')

        cached_session = json.loads(module.redis_client.last_value)
        self.assertEqual(
            cached_session['healthData']['doctorProductPackages'],
            [],
        )

    async def test_create_session_merges_report_and_explicit_basic_info_into_prompts(self):
        gateway = OpeningGateway(
            complete_result=LLMResult(
                content='3',
                reasoning_content='规划推理',
            )
        )
        runtime = _build_runtime(gateway)
        module = _load_record_service()
        processing_service = FakeHealthReportProcessingService(
            {
                'https://example.com/basic-info.pdf': {
                    'reportType': 'checkup',
                    'aiSummary': '需要结合基本信息评估',
                    'status': 'normal',
                    'subject': {'sex': '女', 'age': 41},
                    'expansion': {},
                },
            }
        )
        manager = module.QASessionManager(
            runtime,
            health_report_processing_service=processing_service,
        )

        await manager.createSession(
            'user-1',
            'session-basic-info',
            file_list=[{'url': 'https://example.com/basic-info.pdf'}],
            basic_info={'age': 42, 'height': 165},
        )

        cached_session = json.loads(module.redis_client.last_value)
        self.assertEqual(cached_session['extractedInfo']['gender'], '女')
        self.assertEqual(cached_session['extractedInfo']['age'], 42)
        self.assertEqual(cached_session['extractedInfo']['height'], 165)
        self.assertEqual(
            cached_session['extractedInfoSources'],
            {
                'gender': 'health_report',
                'age': 'user_input',
                'height': 'user_input',
            },
        )
        self.assertIn('性别：女', gateway.complete_requests[0].content)
        self.assertIn('年龄：42', gateway.complete_requests[0].content)
        self.assertIn('身高：165', gateway.complete_requests[1].content)

    async def test_complete_save_writes_mongo_before_deleting_redis(self):
        runtime = _build_runtime(OpeningGateway())
        module = _load_record_service()
        manager = module.QASessionManager(runtime)
        events = []

        async def replace_one(filter, document, upsert=False):
            events.append('mongo')
            return FakeReplaceResult()

        async def delete(key):
            events.append('redis-delete')
            return 1

        module.collection_sessions.replace_one = replace_one
        module.redis_client.delete = delete

        saved = await manager._save_session(
            'user-1',
            'session-1',
            {
                'type': 'qa',
                'status': 'completed',
                'userId': 'user-1',
                'sessionId': 'session-1',
            },
            completed=True,
        )

        self.assertTrue(saved)
        self.assertEqual(events, ['mongo', 'redis-delete'])

    async def test_pending_gate_treats_awaiting_decision_as_unfinished(self):
        runtime = _build_runtime(OpeningGateway())
        module = _load_record_service()
        module.redis_client.value = json.dumps(
            {
                'type': 'qa',
                'status': 'awaiting_decision',
            }
        )
        manager = module.QASessionManager(runtime)

        self.assertTrue(await manager.hasPendingSession('user-1', 'session-1'))

    def test_extract_basic_info_keeps_reliable_report_fields(self):
        module = _load_record_service()

        extracted = module.QASessionManager._extractBasicInfo(
            {
                'subject': {'sex': '女', 'age': 42},
                'patientInfo': {'heightCm': 165, 'weightKg': 55},
            }
        )

        self.assertEqual(
            extracted,
            {
                'name': None,
                'gender': '女',
                'age': 42,
                'height': 165,
                'weight': 55,
            },
        )


if __name__ == '__main__':
    unittest.main()
