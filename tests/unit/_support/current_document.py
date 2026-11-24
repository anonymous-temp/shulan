"""Synthetic inputs for Analysis, Document, and Report archive integration."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from adapters.analysis.basic_info import create_document_basic_info_prepared_service
from adapters.analysis.basic_info import create_document_basic_info_understanding_service
from adapters.analysis.document_index import MongoDocumentIndexStore
from adapters.analysis.document_trace import MongoDocumentTraceStore
from adapters.analysis.microbiome_source import read_microbiome_source_facts
from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from agents import AgentHub
from agents import AgentRuntime
from agents import GraphRegistry
from agents.graphs.analysis import register_analysis_health_report
from agents.graphs.analysis import register_current_analysis_health_report
from agents.llm import LLMResult
from agents.llm import ModelProfileRegistry
from modules.analysis.application.basic_info import empty_basic_info
from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from modules.analysis.application.service import HealthReportInterpreter
from modules.analysis.application.service import HealthReportProcessingService
from modules.analysis.basic_info_source import DocumentCurrentSourceService
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_understanding import DocumentCurrentUnderstandingService
from modules.analysis.preparation import DocumentPreparationBinding
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentPreparationReceipt
from tests.unit._support.agents import FakeLLMGateway
from tests.unit._support.document_bson import BsonCollection
from tests.unit.services.analysis.fakes import MemoryMongoCollection

SCOPE = DocumentScope('user-1', 'subject-self')
CURRENT_SELECTION = OutputSelection('health_report', 'current-shape', 'synthetic-current-extractor')


class SyntheticAdmission:
    """Protocol-only admission; use the real providers for application tests."""

    def __init__(self):
        self.plans = {}

    async def read_verified_plan(self, *, scope, plan_ref, memo=None):
        plan = self.plans[plan_ref]
        if plan.scope != scope:
            raise PermissionError('outside scope')
        return plan


TEXT = '姓名：张三\n性别：女\n年龄：42岁\n报告日期：2026-11-01\n'


def proof(quote):
    start = TEXT.index(quote)
    return {'quote': quote, 'start': start, 'end': start + len(quote)}


def extracted(value, quote, grade):
    return {
        'value': value,
        'meta': {
            'status': 'extracted',
            'sourceReliability': grade,
            'observedAt': '2026-11-01',
            'reason': '原文直接说明受检者信息',
            'evidence': proof(quote),
            'subjectStatus': 'single',
            'subjectRef': '张三',
            'subjectEvidence': proof('姓名：张三'),
            'candidates': [],
        },
    }


def basic_fields():
    fields = {
        'name': extracted('张三', '姓名：张三', 'medium'),
        'gender': extracted('女', '性别：女', 'low'),
        'age': extracted(42, '年龄：42岁', 'medium'),
    }
    return {
        'basicInfo': {k: v['value'] for k, v in fields.items()},
        'basicInfoMeta': {k: v['meta'] for k, v in fields.items()},
    }


def no_value(raw, field, status='not_found'):
    raw['basicInfo'][field] = None
    raw['basicInfoMeta'][field].update(
        status=status,
        sourceReliability=None,
        evidence=None,
        subjectStatus='unknown',
        subjectRef=None,
        subjectEvidence=None,
    )


async def environment(*, status='complete', edit=None, scope=SCOPE, text=None):
    if text is None:
        text = TEXT
    document = {
        '_id': 'synthetic-doc',
        **scope.as_dict(),
        'canonicalReportId': 'synthetic-source',
        'contentSha256': 'a' * 64,
        'fileName': 'synthetic.pdf',
        'fileType': 'pdf',
        'status': 'completed',
        'parserVersion': 'synthetic-parser',
        'interpretationVersion': CURRENT_SELECTION.implementation_revision,
        'interpretationValidatedVersion': 'synthetic-validator',
        'interpretationResultRound': 1,
        'payloadRefs': {'original_file': 'original', 'docmind_result': 'parse', 'interpretation': 'interpretation'},
    }
    parsed = {'text': text, 'pages': [{'text': text}]}
    raw = {
        'reportType': 'laboratory',
        'reportTime': '2026-11-01',
        'processing': {'status': status},
        'observations': [{'itemName': 'synthetic-test', 'resultValue': '2.5', 'unit': 'mmol/L'}],
        **basic_fields(),
    }
    if edit:
        edit(raw)
    rounds = {}

    async def read(**kwargs):
        return deepcopy(rounds[kwargs['interpretation_round']])

    port = SimpleNamespace(read_prepared_round=AsyncMock(side_effect=read), prepare_document=AsyncMock(), rounds=rounds)
    store = MongoDocumentSnapshotStore(snapshots=MemoryMongoCollection(), chunks=MemoryMongoCollection())
    api = DocumentCurrentUnderstandingService(snapshots=store, preparation=port)
    env = SimpleNamespace(scope=scope, document=document, raw=raw, parsed=parsed, api=api, port=port, store=store)
    env.bundle = await capture(env)
    env.original = replace(env.bundle, receipt=DocumentPreparationReceipt(document['_id'], 'synthetic-request', False))
    env.admission = SyntheticAdmission()
    env.source = DocumentCurrentSourceService(snapshots=store, understanding=api, admission=env.admission)
    return env


async def capture(env):
    binding = DocumentPreparationBinding.capture(env.document, env.parsed, env.raw)
    fixed = SimpleNamespace(
        user_id=env.scope.user_id,
        subject_id=env.scope.subject_id,
        document_id=env.document['_id'],
        preparation_binding=binding,
        parsed_document=deepcopy(env.parsed),
        interpretation=deepcopy(env.raw),
        output_selection=CURRENT_SELECTION,
    )
    env.port.rounds[binding.input_snapshot.interpretation_round] = fixed
    return await env.api.capture_bundle(
        scope=env.scope, document_id=env.document['_id'], expected_binding=binding, expected_selection=CURRENT_SELECTION
    )


def report(*, name='测试甲', gender='女', age=42, reliability='medium', subject='测试甲'):
    values, metadata = empty_basic_info(status='not_found', reason='原文未检索到来源。')
    for field, value, quote in (
        ('name', name, f'姓名：{name}'),
        ('gender', gender, f'性别：{gender}'),
        ('age', age, f'年龄：{age}岁'),
    ):
        if value is None:
            continue
        values[field] = value
        metadata[field].update(
            status='extracted',
            sourceReliability=reliability,
            reason='根据原文简单分析，该值明确且属于受检者。',
            evidence={'quote': quote},
            subjectStatus='single',
            subjectRef=subject,
            subjectEvidence={'quote': f'受检者：{subject}'},
        )
    return {
        'reportType': 'lab',
        'status': 'pending',
        'observations': [{'itemName': '指标', 'resultValue': '3'}],
        'basicInfo': values,
        'basicInfoMeta': metadata,
    }


REPORT_TEXT = '受检者：测试甲\n姓名：测试甲\n性别：女\n年龄：42岁\n报告日期：2026-11-12\nALT 12 U/L\n'
UPLOAD = {'file_id': 'https://example.test/synthetic.pdf', 'file_type': 'pdf'}


def model_report(*, incomplete=False):
    raw = report()
    raw['basicInfoMeta']['gender']['sourceReliability'] = 'low'
    raw['reportTime'] = '2026-11-12'
    raw['observations'] = [{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L', 'kind': 'measurement'}]
    if incomplete:
        raw['title'] = 123
    return raw


class ArchiveDocMindClient:
    def __init__(self):
        self.submit_calls = []
        self.result_calls = []
        self.result = None

    def submit_online_file(self, file_url, *, file_name=None, file_type=None):
        raise AssertionError('current preparation must submit the downloaded bytes')

    def submit_local_file(self, file_path, *, file_name, file_type):
        self.local_content = Path(file_path).read_bytes()
        self.submit_calls.append((file_path, file_name, file_type))
        return 'task-local-1'

    def get_result(self, task_id, *, poll_interval):
        self.result_calls.append((task_id, poll_interval))
        return deepcopy(self.result)


def preparation_upstream():
    index_collection = MemoryMongoCollection()
    return SimpleNamespace(
        client=ArchiveDocMindClient(),
        source=SimpleNamespace(
            resolve_report_id=lambda requested: requested or 'report_' + uuid4().hex,
            source_fingerprint=lambda *, user_id, subject_id, file_id: f'{user_id}/{subject_id}/{file_id}',
            save=AsyncMock(side_effect=AssertionError('current preparation must not publish')),
        ),
        index_collection=index_collection,
        index=MongoDocumentIndexStore(index_collection),
        downloader=SimpleNamespace(download=AsyncMock(return_value=b'%PDF-1.7\nsynthetic')),
    )


def bson_collection():
    value = BsonCollection()
    value.with_options = lambda **kwargs: value
    return value


async def actual_analysis_preparation(
    replies, *, scope=SCOPE, text=REPORT_TEXT, native_content=None, upstream=None, health_profile_service=None
):
    """Compose real Analysis and Document prepare/read with synthetic external I/O."""
    if upstream is None:
        upstream = preparation_upstream()
    upstream.client.result = {'success': True, 'text': text}
    if native_content is not None:
        upstream.downloader.download.return_value = native_content
    gateway = FakeLLMGateway(complete_results=[LLMResult(content=json.dumps(raw)) for raw in replies])
    registry = GraphRegistry()
    agents = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=object(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_analysis_health_report(agents)
    register_current_analysis_health_report(agents)
    traces, trace_chunks = bson_collection(), bson_collection()
    service = HealthReportProcessingService(
        interpreter=HealthReportInterpreter(agents, count_input_tokens=len, docmind_client=upstream.client),
        current_interpreter=CurrentHealthReportInterpreter(
            agents,
            count_input_tokens=len,
            docmind_client=upstream.client,
            source_fact_reader=read_microbiome_source_facts,
        ),
        report_source=upstream.source,
        health_profile_service=health_profile_service,
        document_trace_store=MongoDocumentTraceStore(traces, trace_chunks),
        document_index=upstream.index,
        document_downloader=upstream.downloader,
    )
    snapshots, snapshot_chunks = bson_collection(), bson_collection()
    understanding = create_document_basic_info_understanding_service(
        snapshots=snapshots, snapshot_chunks=snapshot_chunks, preparation=service
    )
    prepared = create_document_basic_info_prepared_service(
        understanding=understanding, preparation_records=bson_collection()
    )
    return SimpleNamespace(
        scope=scope,
        prepared=prepared,
        gateway=gateway,
        upstream=upstream,
        snapshots=snapshots,
        snapshot_chunks=snapshot_chunks,
        traces=traces,
        trace_chunks=trace_chunks,
        analysis=service,
    )
