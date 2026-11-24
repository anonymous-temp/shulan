"""Lossless report indexing and material-backed citations for factor analysis."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from urllib.parse import urlsplit

from .schemas import stable_hash

AI_FIELDS = frozenset(
    {
        'analysis',
        'interpretation',
        'riskInterpretation',
        'riskAssessment',
        'suggestions',
        'recommendations',
        'healthAdvice',
        'advice',
        'explanation',
        'aiSummary',
    }
)
IDENTITY_FIELDS = frozenset({'name', 'patientName', 'idCard', 'phone', 'mobile', 'address', 'patientId'})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pointer(value: str) -> str:
    return value.replace('~', '~0').replace('/', '~1')


def index_reports(reports: list) -> tuple[list[dict], dict]:
    """Index every extracted report record, keeping interpretation in another layer."""
    evidence, gaps = [], []
    for report_position, wrapper in enumerate(reports):
        if not isinstance(wrapper, dict):
            gaps.append({'reportIndex': report_position, 'reason': 'invalid_report'})
            continue
        staged = 'interpretation' in wrapper or 'fileIndex' in wrapper
        report = wrapper.get('interpretation') if staged else wrapper
        report_index = wrapper.get('fileIndex', wrapper.get('_questionnaireReportIndex', report_position))
        if not isinstance(report, dict):
            gaps.append({'reportIndex': report_index, 'reason': 'report_unavailable'})
            continue
        if wrapper.get('status') == 'partial':
            gaps.append({'reportIndex': report_index, 'reason': 'report_extraction_partial'})
        date = report.get('reportTime')
        children = report.get('sections') or []
        excluded_sections = {
            index
            for index, child in enumerate(children)
            if isinstance(child, dict) and child.get('reportType') == 'microbiome'
        }
        excluded_rows = {
            (row.get('itemName'), str(row.get('resultValue')), row.get('unit'))
            for index in excluded_sections
            for row in (children[index].get('observations') or [])
            if isinstance(row, dict)
        }

        def add(pointer, value, kind, label, observed_at=None):
            evidence.append(
                {
                    'id': 're_' + stable_hash([report_index, pointer, value])[:20],
                    'reportIndex': report_index,
                    'pointer': pointer,
                    'kind': kind,
                    'label': str(label),
                    'record': deepcopy(value),
                    'observedAt': observed_at or date,
                    'provenance': 'extracted_report_record' if kind == 'report_record' else kind,
                }
            )

        def walk(value, pointer='', label='', kind='nonclinical_metadata', observed_at=None):
            if isinstance(value, dict):
                observed_at = value.get('observedAt') or value.get('reportTime') or observed_at
                if value.get('reportType') == 'microbiome':
                    kind = 'excluded_report'
                if 'itemName' in value and ('resultValue' in value or 'result' in value):
                    record = {key: deepcopy(item) for key, item in value.items() if key not in AI_FIELDS}
                    row_kind = kind
                    if pointer.startswith('/observations/') and (
                        value.get('sourceSection') in excluded_sections
                        or (value.get('itemName'), str(value.get('resultValue')), value.get('unit')) in excluded_rows
                    ):
                        row_kind = 'excluded_report'
                    result_value = value.get('resultValue', value.get('result'))
                    if result_value is None or result_value == '' or isinstance(result_value, bool):
                        row_kind = 'unresolved_report'
                    add(pointer, record, row_kind, value['itemName'], observed_at)
                    for key in AI_FIELDS.intersection(value):
                        walk(value[key], pointer + '/' + _pointer(key), key, 'ai_interpretation', observed_at)
                    return
                for key, item in value.items():
                    if key.startswith('_') or (pointer.endswith('/subject') and key in IDENTITY_FIELDS):
                        continue
                    child_kind = kind
                    if key in AI_FIELDS:
                        child_kind = 'ai_interpretation'
                    elif kind not in {'excluded_report', 'ai_interpretation', 'unresolved_report'}:
                        if key == 'observations':
                            child_kind = 'report_record'
                        elif key == 'unresolvedItems':
                            child_kind = 'unresolved_report'
                        elif pointer.endswith('/expansion') and key in {'findings', 'diagnosis'}:
                            child_kind = (
                                'report_record'
                                if pointer != '/expansion' or not excluded_sections
                                else 'nonclinical_metadata'
                            )
                        elif pointer.endswith('/subject') and key in {'gender', 'sex', 'age', 'ageYears'}:
                            child_kind = 'report_background'
                        else:
                            child_kind = 'nonclinical_metadata'
                    walk(item, pointer + '/' + _pointer(key), key, child_kind, observed_at)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, pointer + '/' + str(index), label, kind, observed_at)
            elif value is not None and value != '':
                add(pointer, value, kind, label, observed_at)

        walk(report)
        gender_sources = wrapper.get('genderEvidence') or report.get('_questionnaireGenderEvidence') or []
        for index, source in enumerate(gender_sources if isinstance(gender_sources, list) else []):
            if not isinstance(source, dict) or source.get('value') not in {'male', 'female'}:
                continue
            quote = source.get('quote')
            if not isinstance(quote, str) or not re.search(r'性别|\bsex\b|\bgender\b', quote, re.IGNORECASE):
                continue
            add(
                f'/genderEvidence/{index}',
                {'itemName': 'gender', 'resultValue': source['value'], 'evidence': deepcopy(source)},
                'report_background',
                'gender',
            )
    return evidence, {
        'reportCount': len(reports),
        'evidenceCount': len(evidence),
        'indexedEvidenceIds': [row['id'] for row in evidence],
        'analyzedEvidenceIds': [],
        'unprocessedEvidenceIds': [],
        'gaps': gaps,
        'targetRetrieval': [],
    }


def batches(rows: list[dict], *, max_chars: int, max_items: int = 24) -> list[list[dict]]:
    """Partition whole records; an oversized record is a separate, untruncated batch."""
    result, current, size = [], [], 0
    for row in rows:
        length = len(json.dumps(row, ensure_ascii=False, allow_nan=False))
        if current and (size + length > max_chars or len(current) >= max_items):
            result.append(current)
            current, size = [], 0
        current.append(deepcopy(row))
        size += length
    if current:
        result.append(current)
    return result


def source_material(document, *, channel: str) -> dict | None:
    """Accept original source passages, never a model answer or an AI summary."""
    if channel == 'knowledge_base':
        metadata = dict(document.metadata)
        title, url = document.title, document.url
        raw_passages = metadata.get('sourceTexts', [])
        origin = str(document.source)
    else:
        if not isinstance(document, Mapping):
            return None
        metadata = dict(document)
        title, url = metadata.get('title'), metadata.get('url')
        raw_passages = metadata.get('sourceTexts') or metadata.get('passages') or []
        if not raw_passages and isinstance(metadata.get('text'), str):
            raw_passages = [{'text': metadata['text'], 'kind': 'retrieved_text', 'locator': metadata.get('locator')}]
        origin = 'aion_web_search'
    if not isinstance(raw_passages, (list, tuple)):
        return None
    if url:
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if parsed is None or parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                url = None
        except ValueError:
            url = None
    if channel == 'web_search' and not url:
        return None
    if not url and not metadata.get('id'):
        return None
    passages = []
    for index, row in enumerate(raw_passages):
        if not isinstance(row, Mapping) or not isinstance(row.get('text'), str) or not row['text'].strip():
            continue
        if channel == 'knowledge_base' and row.get('kind') not in {'abstract', 'fullText', 'guideBlock'}:
            continue
        passages.append(
            {
                'id': f'p{index + 1}',
                'text': row['text'],
                'kind': row.get('kind', 'retrieved_text'),
                'locator': deepcopy(row.get('locator') or {'field': row.get('kind', 'text'), 'index': index}),
            }
        )
    if not passages:
        return None
    content_hash = stable_hash(passages)
    return {
        'id': 'ks_' + stable_hash([channel, metadata.get('id'), url, content_hash])[:20],
        'channel': channel,
        'type': origin,
        'title': title or url or str(metadata['id']),
        'url': url,
        'sourceId': metadata.get('id'),
        'version': metadata.get('knowledgeBaseRevision') or metadata.get('version') or content_hash,
        'contentHash': content_hash,
        'retrievedAt': now_iso(),
        'publicationDate': metadata.get('publicationDate') or metadata.get('year'),
        'passages': passages,
    }


def citation(value: dict, sources: dict[str, dict]) -> dict:
    """Validate exact passage membership; semantic support is assessed separately."""
    if not isinstance(value, dict):
        raise ValueError('knowledge evidence must be an object')
    source = sources.get(value.get('sourceId'))
    if source is None:
        raise ValueError('knowledge evidence references an unknown source')
    quote = value.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError('knowledge evidence requires an original quote')
    normalized = ' '.join(quote.split())
    passages = [p for p in source['passages'] if p['id'] == value.get('passageId')]
    if len(passages) != 1 or normalized not in ' '.join(passages[0]['text'].split()):
        raise ValueError('knowledge quote is absent from the referenced material')
    return {
        'sourceId': source['id'],
        'passageId': passages[0]['id'],
        'quote': quote,
        'locator': deepcopy(passages[0]['locator']),
        'sourceVersion': source['version'],
    }
