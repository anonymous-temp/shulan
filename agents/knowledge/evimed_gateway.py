"""EviMed AI 检索 API V2 的 KnowledgeGateway 适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from utils.knowledge import Knowledge

from .types import KnowledgeDocument
from .types import KnowledgeQuery
from .types import KnowledgeSource


@runtime_checkable
class EviMedKnowledgeClient(Protocol):
    """适配器依赖的最小 EviMed 异步客户端协议。"""

    @property
    def enabled(self) -> bool: ...

    async def literatureAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> list: ...

    async def guideAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> list: ...

    async def instructionAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> dict: ...

    async def clinicalTrialAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> list: ...


class EviMedKnowledgeGateway:
    """把 EviMed 四种响应结构归一为 KnowledgeDocument。"""

    def __init__(self, client: EviMedKnowledgeClient | None = None) -> None:
        client = client or Knowledge()
        if not isinstance(client, EviMedKnowledgeClient):
            raise TypeError('client must implement EviMedKnowledgeClient')
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client.enabled

    async def retrieve(
        self,
        query: KnowledgeQuery,
    ) -> tuple[KnowledgeDocument, ...]:
        if not isinstance(query, KnowledgeQuery):
            raise TypeError('query must be a KnowledgeQuery')
        if not self.enabled:
            return ()

        keywords = [query.query]
        if query.source is KnowledgeSource.LITERATURE:
            rows = await self._client.literatureAsync(
                keywords,
                count=query.count,
                raise_errors=True,
                useLlm=False,
            )
            return self._normalizeRows(query.source, rows)
        if query.source is KnowledgeSource.GUIDE:
            rows = await self._client.guideAsync(
                keywords,
                count=query.count,
                raise_errors=True,
                useLlm=False,
                searchBlock=True,
                **({'include_blocks': True} if query.include_source_text else {}),
            )
            return self._normalizeRows(query.source, rows)
        if query.source is KnowledgeSource.INSTRUCTION:
            grouped = await self._client.instructionAsync(
                keywords,
                count=query.count,
                raise_errors=True,
                useLlm=False,
            )
            rows = []
            if isinstance(grouped, Mapping):
                for source_name, values in grouped.items():
                    if not isinstance(values, list):
                        continue
                    rows.extend(
                        {**row, 'source': row.get('source', source_name)} for row in values if isinstance(row, Mapping)
                    )
            return self._normalizeRows(query.source, rows)
        if query.source is KnowledgeSource.CLINICAL_TRIAL:
            rows = await self._client.clinicalTrialAsync(
                keywords,
                count=query.count,
                raise_errors=True,
            )
            return self._normalizeRows(query.source, rows)
        raise ValueError(f'unsupported knowledge source: {query.source!r}')

    @classmethod
    def _normalizeRows(
        cls,
        source: KnowledgeSource,
        rows: Any,
    ) -> tuple[KnowledgeDocument, ...]:
        if not isinstance(rows, list):
            return ()
        documents = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            document = cls._normalizeRow(source, row)
            if document is not None:
                documents.append(document)
        return tuple(documents)

    @staticmethod
    def _normalizeRow(
        source: KnowledgeSource,
        row: Mapping[str, Any],
    ) -> KnowledgeDocument | None:
        title = _firstText(
            row,
            'title',
            'genericNames',
            'englishName',
            'tradeNames',
            'registrationNo',
        )
        if source is KnowledgeSource.LITERATURE:
            content = _firstText(row, 'aiSummary', 'abstract')
        elif source is KnowledgeSource.GUIDE:
            content = _firstText(row, 'summary', 'fullText')
        elif source is KnowledgeSource.INSTRUCTION:
            content = _joinText(
                row,
                'indication',
                'specifications',
                'enterpriseName',
            )
        else:
            content = _joinText(
                row,
                'conditions',
                'interventions',
                'status',
                'phase',
                'studyType',
            )
        source_texts = []
        original_fields = (
            ('abstract',)
            if source is KnowledgeSource.LITERATURE
            else ('fullText',)
            if source is KnowledgeSource.GUIDE
            else ()
        )
        for field in original_fields:
            original = _firstText(row, field)
            if original:
                source_texts.append({'kind': field, 'text': original})
        if source is KnowledgeSource.GUIDE:
            blocks = row.get('_sourceBlocks', [])
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, Mapping) and isinstance(block.get('text'), str) and block['text'].strip():
                    source_texts.append({'kind': 'guideBlock', 'text': block['text'].strip()})
        if not content and source_texts:
            content = source_texts[0]['text']
        if not title or not content:
            return None

        url_value = row.get('url')
        if isinstance(url_value, Mapping):
            url = next(
                (value.strip() for value in url_value.values() if isinstance(value, str) and value.strip()),
                None,
            )
        else:
            url = url_value.strip() if isinstance(url_value, str) and url_value.strip() else None
        metadata = {
            key: row[key]
            for key in (
                'id',
                'year',
                'journal',
                'publisher',
                'publicationDate',
                'authors',
                'studyType',
                'source',
                'registrationNo',
                'knowledgeBaseRevision',
            )
            if key in row
        }
        metadata['sourceTexts'] = source_texts
        return KnowledgeDocument(
            source=source,
            title=title,
            content=content,
            url=url,
            metadata=metadata,
        )


def _firstText(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _joinText(row: Mapping[str, Any], *names: str) -> str:
    parts = []
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            values = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if values:
                parts.append('、'.join(values))
    return '\n'.join(parts)
