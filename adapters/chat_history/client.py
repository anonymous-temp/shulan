"""HTTP implementation of the public chat-history query port."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import httpx

from modules.chat_history.contracts import ChatHistoryQueryError
from modules.chat_history.contracts import require_group_id


def _reject_json_constant(value: str):
    raise ValueError('Nonstandard JSON constant')


class OpenIMChatHistoryReader:
    """Make one bounded POST per query, with no persistent HTTP resources."""

    def __init__(
        self,
        query_url: str,
        *,
        timeout_seconds: float = 20.0,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ):
        if not isinstance(query_url, str):
            raise ChatHistoryQueryError('invalid_configuration')
        if not query_url.strip():
            raise ChatHistoryQueryError('not_configured')
        try:
            url = httpx.URL(query_url.strip())
        except httpx.InvalidURL:
            raise ChatHistoryQueryError('invalid_configuration') from None
        if url.scheme not in {'http', 'https'} or not url.host or url.userinfo or url.fragment:
            raise ChatHistoryQueryError('invalid_configuration')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ChatHistoryQueryError('invalid_configuration')
        if client_factory is not None and not callable(client_factory):
            raise TypeError('client_factory must be callable')
        self.query_url = url
        self.timeout_seconds = float(timeout_seconds)
        self._client_factory = httpx.AsyncClient if client_factory is None else client_factory

    async def query(self, group_id: str) -> dict[str, Any]:
        group_id = require_group_id(group_id)
        try:
            async with self._client_factory(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.post(self.query_url, json={'groupId': group_id})
                response.raise_for_status()
        except httpx.TimeoutException:
            raise ChatHistoryQueryError('timeout') from None
        except httpx.HTTPStatusError as error:
            raise ChatHistoryQueryError('http_error', http_status=error.response.status_code) from None
        except httpx.RequestError:
            raise ChatHistoryQueryError('transport_error') from None

        try:
            payload = response.json(parse_constant=_reject_json_constant)
        except ValueError:
            raise ChatHistoryQueryError('invalid_json', http_status=response.status_code) from None
        return self._response_data(payload)

    @staticmethod
    def _response_data(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or type(payload.get('state')) is not int:
            raise ChatHistoryQueryError('malformed_response')
        if payload['state'] != 200:
            raise ChatHistoryQueryError('business_error', business_code=payload['state'])
        results = payload.get('results')
        if not isinstance(results, dict) or type(results.get('errCode')) is not int:
            raise ChatHistoryQueryError('malformed_response')
        if results['errCode'] != 0:
            raise ChatHistoryQueryError('business_error', business_code=results['errCode'])
        data = results.get('data')
        if (
            not isinstance(data, dict)
            or not isinstance(data.get('chatLogs'), list)
            or type(data.get('chatLogsNum')) is not int
            or data['chatLogsNum'] < 0
        ):
            raise ChatHistoryQueryError('malformed_response')
        for item in data['chatLogs']:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get('chatLog'), dict)
                or type(item.get('isRevoked')) is not bool
            ):
                raise ChatHistoryQueryError('malformed_response')
        return data
