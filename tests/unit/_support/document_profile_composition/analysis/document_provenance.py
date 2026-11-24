"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_provenance."""

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from health_profile import HealthProfileJobQueue
from health_profile import HealthReportSourceService
from health_profile import MongoHealthReportStore
from modules.analysis.application.service import HealthReportInterpreter
from modules.analysis.application.service import HealthReportProcessingService
from tests.unit._support.document_profile_composition.analysis.analysis_service_agent import FakeDocMindClient
from tests.unit._support.document_profile_composition.analysis.analysis_service_agent import _build_runtime
from tests.unit._support.document_profile_composition.analysis.fakes import InMemoryDocumentTraceStore
from tests.unit._support.document_profile_composition.profile.health_report_source import FakeReportCollection
from tests.unit._support.document_profile_composition.profile.health_report_source import InMemoryJobRepository
from tests.unit._support.document_profile_composition.profile.report_ingestion import ReportIngestionFixture


class LocalDocMindClient(FakeDocMindClient):
    def submit_local_file(self, file_path, *, file_name, file_type):
        self.local_path = Path(file_path)
        self.local_content = self.local_path.read_bytes()
        self.submit_calls.append((file_path, file_name, file_type))
        return 'task-local-1'


def report_text(result):
    """Literal synthetic source text; fixture facts must exist before model extraction."""
    lines = [result.get('title') or '检验报告']
    if isinstance(result.get('reportTime'), str):
        lines.append(f'检测日期：{result["reportTime"]}')
    items = result.get('observations', result.get('expansion', []))
    if isinstance(items, list):
        for item in items:
            lines.append(
                f'本次检测结果：{item["itemName"]} {item["resultValue"]} {item.get("unit") or ""}；'
                f'参考范围：{item.get("referenceRange") or "未知"}；结果评价：{item.get("flag") or "未知"}'
            )
    expansion = result.get('expansion')
    if isinstance(expansion, dict):
        for field in ('findings', 'diagnosis'):
            if expansion.get(field):
                lines.append(f'{"影像所见" if field == "findings" else "影像诊断"}：{expansion[field]}')
    for section in result.get('sections', []):
        lines.append(report_text(section))
    return '\n'.join(lines)


def attach_source_evidence(value, text):
    """The mock model cites matching literal source lines, never the background."""
    value = deepcopy(value)
    sources = re.findall(r'【原文 ([^】]+)】\n?(.*?)(?=【|\Z)', text, re.DOTALL)

    def visit(node):
        for field in ('observations', 'expansion'):
            items = node.get(field)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict) or item.get('sourceId') or item.get('evidence'):
                        continue
                    for source_id, raw in sources:
                        quote = next(
                            (
                                line
                                for line in raw.splitlines()
                                if str(item.get('itemName')) in line and str(item.get('resultValue')) in line
                            ),
                            None,
                        )
                        if quote:
                            item['evidence'] = {'sourceId': source_id, 'quote': quote}
                            break
        expansion = node.get('expansion')
        if isinstance(expansion, dict):
            for field in ('findings', 'diagnosis'):
                if not isinstance(expansion.get(field), str):
                    continue
                for source_id, raw in sources:
                    quote = next((line for line in raw.splitlines() if expansion[field] in line), None)
                    if quote:
                        node.setdefault('expansionEvidence', {})[field] = {'sourceId': source_id, 'quote': quote}
                        break
        for section in node.get('sections', []):
            visit(section)

    visit(value)
    return value


def build_flow(*, raw_result=None, model_result=None):
    result = ReportIngestionFixture.record().result if model_result is None else model_result
    runtime, graph = _build_runtime(json.dumps(result, ensure_ascii=False))
    client = LocalDocMindClient(
        result=raw_result if raw_result is not None else {'success': True, 'text': report_text(result)}
    )
    invoke = graph.ainvoke

    async def source_backed_reply(input, config=None, *, context=None, **kwargs):
        output = await invoke(input, config=config, context=context, **kwargs)
        try:
            value = json.loads(output.get('content', ''))
        except (ValueError, TypeError):
            return output
        if isinstance(value, dict):
            output['content'] = json.dumps(attach_source_evidence(value, input['report_text']), ensure_ascii=False)
        return output

    graph.ainvoke = source_backed_reply
    traces = InMemoryDocumentTraceStore()
    collection = FakeReportCollection()
    reports = MongoHealthReportStore(collection)
    jobs = InMemoryJobRepository()
    source = HealthReportSourceService(reports, HealthProfileJobQueue(jobs))
    interpreter = HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=client)
    service = HealthReportProcessingService(
        interpreter=interpreter,
        report_source=source,
        health_profile_service=SimpleNamespace(
            ensure_self_profile=AsyncMock(
                return_value=SimpleNamespace(subject=SimpleNamespace(subject_id='subject-self'))
            )
        ),
        document_trace_store=traces,
    )
    return SimpleNamespace(
        service=service,
        source=source,
        reports=reports,
        collection=collection,
        jobs=jobs,
        client=client,
        graph=graph,
        traces=traces,
        interpreter=interpreter,
    )
