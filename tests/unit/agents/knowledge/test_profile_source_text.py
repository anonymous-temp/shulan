"""原始证据适配保留的契约；不改变既有 content 或普通调用的返回行。"""

import pytest

from agents.knowledge import EviMedKnowledgeGateway
from agents.knowledge import KnowledgeQuery
from agents.knowledge import KnowledgeSource
from utils.knowledge import Knowledge


@pytest.mark.asyncio
async def test_guide_blocks_are_opt_in_and_bound_to_the_correct_guide(monkeypatch):
    client = Knowledge(api_key='synthetic-key', base_url='https://example.test')
    calls = []
    row = {'id': 'guide-1', 'title': '合成指南', 'summary': '原有摘要内容'}

    async def request(operation, params):
        calls.append(params)
        return {
            'guide': {
                'list': [row],
                'blocks': [
                    {'guideId': 'guide-1', 'text': '本指南的原始文本块'},
                    {'guideId': 'other', 'text': '不能混入的其他指南文本'},
                ],
            }
        }

    monkeypatch.setattr(client, '_requestAsync', request)
    assert await client.guideAsync(['关键词']) == [row]
    gateway = EviMedKnowledgeGateway(client)
    original = await gateway.retrieve(KnowledgeQuery(source=KnowledgeSource.GUIDE, query='关键词'))
    enriched = await gateway.retrieve(
        KnowledgeQuery(
            source=KnowledgeSource.GUIDE,
            query='关键词',
            include_source_text=True,
        )
    )
    assert original[0].content == enriched[0].content == '原有摘要内容'
    assert original[0].metadata['sourceTexts'] == []
    assert enriched[0].metadata['sourceTexts'] == [{'kind': 'guideBlock', 'text': '本指南的原始文本块'}]
    assert all('include_blocks' not in params for params in calls)


def test_literature_keeps_original_abstract_separate_from_ai_summary():
    document = EviMedKnowledgeGateway._normalizeRow(
        KnowledgeSource.LITERATURE,
        {
            'id': 'paper-1',
            'title': '合成文献',
            'aiSummary': '旧逻辑使用的 AI 摘要',
            'abstract': '可用于原文引用校验的真实摘要字段',
        },
    )
    assert document.content == '旧逻辑使用的 AI 摘要'
    assert document.metadata['sourceTexts'] == [{'kind': 'abstract', 'text': '可用于原文引用校验的真实摘要字段'}]


def test_ai_summary_is_never_promoted_to_original_source_text():
    document = EviMedKnowledgeGateway._normalizeRow(
        KnowledgeSource.LITERATURE,
        {
            'title': '只有二次摘要的文献',
            'aiSummary': 'AI 合成内容',
        },
    )
    assert document.metadata['sourceTexts'] == []
