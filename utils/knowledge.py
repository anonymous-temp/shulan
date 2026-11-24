"""提供 EviMed AI 检索 API V2 的同步与异步调用方法。

接口契约见 ``docs/AI-检索API-V2.md``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from utils.log import getLogger

logger = getLogger(__name__)


class KnowledgeAPIError(RuntimeError):
    """知识库响应无法按公开接口契约处理。"""

    def __init__(
        self,
        category: str,
        *,
        http_status: int | None = None,
        business_code: Any = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(f'knowledge API {category}')
        self.category = category
        self.http_status = http_status
        self.business_code = business_code
        self.content_type = content_type


class Knowledge:
    """EviMed 知识库客户端。

    现有公开方法继续在失败时返回空结果，以便业务层执行降级。
    构造时显式传入 ``api_key`` 可避免加载项目环境配置，便于无网络测试。
    """

    BASE_URL = 'https://www.evimed.com/api-evimed/medicine-api/ai-api/review/api/v2'
    DEFAULT_TIMEOUT_SECONDS = 20.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if api_key is None and base_url is None and timeout_seconds is None:
            # 延迟加载，避免仅导入客户端时读取应用配置。
            from config import env as env_config

            api_key = env_config.KNOWLEDGE_API_KEY
            base_url = env_config.KNOWLEDGE_API_BASE_URL
            timeout_seconds = env_config.KNOWLEDGE_API_TIMEOUT_SECONDS
        else:
            api_key = '' if api_key is None else api_key
            base_url = self.BASE_URL if base_url is None else base_url
            timeout_seconds = self.DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        if not isinstance(api_key, str):
            raise TypeError('api_key must be a string')
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds,
            (int, float),
        ):
            raise TypeError('timeout_seconds must be a number')
        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be greater than zero')

        self.api_key = api_key.strip()
        self.base_url = self._normalizeBaseUrl(base_url)
        self.timeout_seconds = float(timeout_seconds)
        self.headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }

    @property
    def enabled(self) -> bool:
        """是否配置了可用于发起请求的密钥。"""
        return bool(self.api_key)

    @staticmethod
    def _normalizeBaseUrl(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError('base_url must be a string')
        normalized = value.strip().rstrip('/')
        if not normalized:
            raise ValueError('base_url cannot be empty')
        root = normalized.split('/review/api', 1)[0]
        return f'{root}/review/api/v2'

    @staticmethod
    def _buildLiteratureGuideParams(
        keywords: list,
        doc_type: str,
        count: int,
        **kwargs: Any,
    ) -> dict:
        """构建文献/指南检索的请求参数。"""
        return {
            'query': Knowledge._joinKeywords(keywords),
            'type': doc_type,
            'count': count,
            **kwargs,
        }

    @staticmethod
    def _buildInstructionParams(
        keywords: list,
        count: int,
        **kwargs: Any,
    ) -> dict:
        """构建说明书检索的请求参数。"""
        return {
            'query': Knowledge._joinKeywords(keywords),
            'count': count,
            **kwargs,
        }

    @staticmethod
    def _buildClinicalTrialParams(
        keywords: list,
        count: int,
        **kwargs: Any,
    ) -> dict:
        """构建临床试验检索的请求参数。"""
        return {
            'query': Knowledge._joinKeywords(keywords),
            'count': count,
            **kwargs,
        }

    @staticmethod
    def _joinKeywords(keywords: list) -> str:
        if not isinstance(keywords, list):
            raise TypeError('keywords must be a list')
        normalized = []
        for keyword in keywords:
            if not isinstance(keyword, str):
                raise TypeError('keywords must contain only strings')
            value = keyword.strip()
            if value:
                normalized.append(value)
        if not normalized:
            raise ValueError('keywords cannot be empty')
        return ' '.join(normalized)

    def _request(self, path: str, data: Mapping[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('knowledge API is not configured')
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f'{self.base_url}/{path}',
                headers=self.headers,
                json=dict(data),
            )
            response.raise_for_status()
            return self._responseData(response)

    async def _requestAsync(
        self,
        path: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError('knowledge API is not configured')
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f'{self.base_url}/{path}',
                headers=self.headers,
                json=dict(data),
            )
            response.raise_for_status()
            return self._responseData(response)

    @staticmethod
    def _responseData(response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get('content-type')
        try:
            payload = response.json()
        except ValueError as error:
            raise KnowledgeAPIError(
                'invalid_json',
                http_status=response.status_code,
                content_type=content_type,
            ) from error
        if not isinstance(payload, Mapping):
            raise KnowledgeAPIError(
                'malformed_payload',
                http_status=response.status_code,
                content_type=content_type,
            )
        business_code = payload.get('code')
        if not isinstance(business_code, (str, int, float, bool, type(None))):
            business_code = type(business_code).__name__
        if business_code != 200:
            raise KnowledgeAPIError(
                'business_error',
                http_status=response.status_code,
                business_code=business_code,
                content_type=content_type,
            )
        data = payload.get('data')
        if not isinstance(data, Mapping):
            raise KnowledgeAPIError(
                'malformed_data',
                http_status=response.status_code,
                business_code=business_code,
                content_type=content_type,
            )
        return dict(data)

    @staticmethod
    def _listResult(data: Mapping[str, Any], group: str | None = None) -> list:
        value: Any = data
        if group is not None:
            value = data.get(group, {})
        if not isinstance(value, Mapping):
            return []
        rows = value.get('list', [])
        return list(rows) if isinstance(rows, list) else []

    @staticmethod
    def _logFailure(operation: str, error: Exception) -> None:
        # 不记录查询词、响应正文或密钥，避免泄露健康信息。
        if isinstance(error, KnowledgeAPIError):
            logger.warning(
                'Knowledge %s failed: %s category=%s http_status=%s business_code=%s content_type=%s',
                operation,
                type(error).__name__,
                error.category,
                error.http_status,
                error.business_code,
                error.content_type,
            )
            return
        if isinstance(error, httpx.HTTPStatusError):
            logger.warning(
                'Knowledge %s failed: %s http_status=%s',
                operation,
                type(error).__name__,
                error.response.status_code,
            )
            return
        logger.warning(
            'Knowledge %s failed: %s',
            operation,
            type(error).__name__,
        )

    def literature(self, keywords: list, count: int = 10, **kwargs: Any) -> list:
        try:
            data = self._request(
                'literature-guide',
                self._buildLiteratureGuideParams(
                    keywords,
                    'literature',
                    count,
                    **kwargs,
                ),
            )
            return self._listResult(data, 'literature')
        except Exception as error:
            self._logFailure('literature', error)
            return []

    def guide(self, keywords: list, count: int = 10, **kwargs: Any) -> list:
        try:
            data = self._request(
                'literature-guide',
                self._buildLiteratureGuideParams(
                    keywords,
                    'guide',
                    count,
                    **kwargs,
                ),
            )
            return self._listResult(data, 'guide')
        except Exception as error:
            self._logFailure('guide', error)
            return []

    def instruction(
        self,
        keywords: list,
        count: int = 10,
        **kwargs: Any,
    ) -> dict:
        try:
            return self._request(
                'instruction',
                self._buildInstructionParams(keywords, count, **kwargs),
            )
        except Exception as error:
            self._logFailure('instruction', error)
            return {}

    def clinicalTrial(
        self,
        keywords: list,
        count: int = 10,
        **kwargs: Any,
    ) -> list:
        try:
            data = self._request(
                'clinical-trial',
                self._buildClinicalTrialParams(keywords, count, **kwargs),
            )
            return self._listResult(data)
        except Exception as error:
            self._logFailure('clinical trial', error)
            return []

    async def literatureAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> list:
        try:
            data = await self._requestAsync(
                'literature-guide',
                self._buildLiteratureGuideParams(
                    keywords,
                    'literature',
                    count,
                    **kwargs,
                ),
            )
            return self._listResult(data, 'literature')
        except Exception as error:
            self._logFailure('literature async', error)
            if raise_errors:
                raise
            return []

    async def guideAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        include_blocks: bool = False,
        **kwargs: Any,
    ) -> list:
        try:
            data = await self._requestAsync(
                'literature-guide',
                self._buildLiteratureGuideParams(
                    keywords,
                    'guide',
                    count,
                    **kwargs,
                ),
            )
            rows = self._listResult(data, 'guide')
            if not include_blocks:
                return rows
            group = data.get('guide', {})
            blocks = group.get('blocks', []) if isinstance(group, Mapping) else []
            if not isinstance(blocks, list):
                blocks = []
            return [
                {
                    **row,
                    '_sourceBlocks': [
                        block
                        for block in blocks
                        if isinstance(block, Mapping)
                        and row.get('id') is not None
                        and str(block.get('guideId')) == str(row['id'])
                    ],
                }
                for row in rows
                if isinstance(row, Mapping)
            ]
        except Exception as error:
            self._logFailure('guide async', error)
            if raise_errors:
                raise
            return []

    async def instructionAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> dict:
        try:
            return await self._requestAsync(
                'instruction',
                self._buildInstructionParams(keywords, count, **kwargs),
            )
        except Exception as error:
            self._logFailure('instruction async', error)
            if raise_errors:
                raise
            return {}

    async def clinicalTrialAsync(
        self,
        keywords: list,
        count: int = 10,
        *,
        raise_errors: bool = False,
        **kwargs: Any,
    ) -> list:
        try:
            data = await self._requestAsync(
                'clinical-trial',
                self._buildClinicalTrialParams(keywords, count, **kwargs),
            )
            return self._listResult(data)
        except Exception as error:
            self._logFailure('clinical trial async', error)
            if raise_errors:
                raise
            return []
