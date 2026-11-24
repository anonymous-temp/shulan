"""多报告歧义选择的 Agent 适配与业务边界测试。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from modules.report.chat_context import build_report_chat_context
from modules.report.ports import ReportArtifactRecord
from modules.report.ports import ReportContextSelection


def _batch(final_report_id: str, created_at: str, *, session_id: str) -> dict:
    return {
        'finalReportId': final_report_id,
        'sessionId': session_id,
        'createdAt': created_at,
        'artifacts': [
            {'reportType': 'evaluate', 'state': 'saved'},
            {'reportType': 'opinion', 'state': 'saved'},
        ],
    }


class _Store:
    def __init__(self) -> None:
        self.items = [
            _batch('rpt-2', '2026-08-01T00:00:00Z', session_id='old-2'),
            _batch('rpt-1', '2026-07-01T00:00:00Z', session_id='old-1'),
        ]
        self.records = {
            ('rpt-2', 'opinion'): ReportArtifactRecord(
                final_report_id='rpt-2',
                user_id='user-1',
                session_id='old-2',
                report_type='opinion',
                generation_status='success',
                review_status='ai_unverified',
                content_hash='hash-2',
                body={'diagnosis': '诊断意见内容'},
            ),
            ('rpt-2', 'evaluate'): ReportArtifactRecord(
                final_report_id='rpt-2',
                user_id='user-1',
                session_id='old-2',
                report_type='evaluate',
                generation_status='success',
                review_status='ai_unverified',
                content_hash='hash-evaluate',
                body={'summary': '最新评估报告'},
            ),
        }

    async def list_batches(self, **_kwargs):
        return {'items': self.items, 'nextCursor': None}

    async def get_artifact(self, *, final_report_id, report_type, **_kwargs):
        return self.records.get((final_report_id, report_type))


@dataclass
class _Selector:
    selection: ReportContextSelection | None
    calls: int = 0
    candidates: list[dict] | None = None

    async def select(self, *, candidates, **_kwargs):
        self.calls += 1
        self.candidates = candidates
        return self.selection


@pytest.mark.asyncio
async def test_ambiguous_catalog_uses_agent_selection_and_reads_validated_record():
    selector = _Selector(ReportContextSelection('rpt-2', 'opinion'))

    context = await build_report_chat_context(
        _Store(),
        user_id='user-1',
        session_id='new-session',
        message='请解释8月报告的诊断意见',
        candidate_selector=selector,
    )

    assert context is not None
    assert context.reason == 'agent_selected'
    assert context.final_report_id == 'rpt-2'
    assert context.report_type == 'opinion'
    assert '诊断意见内容' in context.text
    assert selector.calls == 1
    assert selector.candidates is not None
    assert all('body' not in candidate for candidate in selector.candidates)


@pytest.mark.asyncio
async def test_latest_rule_does_not_call_agent_selector():
    selector = _Selector(ReportContextSelection('rpt-1', 'opinion'))

    context = await build_report_chat_context(
        _Store(),
        user_id='user-1',
        session_id='new-session',
        message='请解释最新报告',
        candidate_selector=selector,
    )

    assert context is not None
    assert context.reason == 'latest'
    assert context.final_report_id == 'rpt-2'
    assert selector.calls == 0


@pytest.mark.asyncio
async def test_unresolved_agent_selection_keeps_catalog_clarification():
    selector = _Selector(None)

    context = await build_report_chat_context(
        _Store(),
        user_id='user-1',
        session_id='new-session',
        message='请解释我的报告',
        candidate_selector=selector,
    )

    assert context is not None
    assert context.reason == 'ambiguous'
    assert '存在多份候选' in context.text
    assert selector.calls == 1
