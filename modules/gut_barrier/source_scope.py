"""Patient-relevant source limits, without exposing private audit records.

Only explicit collection states select these sentences. No identities, source
URLs, exception messages, or free-form attachment content are interpolated.
This neither rejects additional data nor changes an existing clinical result.
"""
from __future__ import annotations


_UNREADABLE = frozenset({
    'SOURCE_UNAVAILABLE', 'SOURCE_COLLECTION_UNAVAILABLE', 'OCR_UNAVAILABLE',
    'OCR_PAGE_LIMIT_REACHED', 'OCR_BUDGET_EXHAUSTED', 'OCR_CAPACITY_EXHAUSTED',
    'SOURCE_PAGE_LIMIT_EXCEEDED', 'SOURCES_SKIPPED_OFFLINE',
    'UNRECOGNIZED_SOURCE_CONTENT',
})


def source_scope_block(issues: list, sources: list) -> dict | None:
    codes = {item.get('code') for item in issues if isinstance(item, dict)}
    summaries = [item for item in sources if isinstance(item, dict)]
    sentences = []
    if codes.intersection(_UNREADABLE) or any(s.get('failedPages') for s in summaries):
        sentences.append('部分附件未能取得完整的可用内容，评估依据本次提供且可用的资料，不代表已完成原始检验单的独立核验。')
    if codes.intersection({'SOURCE_MARKER_VALUE_CONFLICT', 'AMBIGUOUS_MARKER_ROW'}):
        sentences.append('存在不一致或归属不明确的检测结果，相关项目需核对原始报告。')
    if 'AUXILIARY_METRIC_CONFLICT' in codes:
        sentences.append('不一致的菌群指标未用于个体判断。')
    if not sentences:
        return None
    return {'kind': 'paragraph', 'title': '本次评估资料范围', 'text': ''.join(sentences)}
