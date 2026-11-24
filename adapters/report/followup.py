"""External HTTP adapter for the four follow-up reports.

模型调用已迁至 ``adapters/report/agent_followup.py``；本适配器只保留
产品目录与回调这两项非模型 I/O。
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from config.report_endpoints import validate_report_endpoint
from modules.report.ports import FollowupGatewayError


class FollowupReportHttpGateway:
    """Provide product-catalog and callback I/O to follow-up orchestration."""

    def __init__(
        self,
        *,
        callback_url: str,
        product_catalog_url: str | None = None,
    ) -> None:
        self.callback_url = callback_url
        # 产品目录地址只来自配置（.env 的 SHULAN_REPORT_PRODUCT_URL），代码里不写死任何环境的地址；
        # 未显式传入时读取 config.env.REPORT_PRODUCT_URL。
        if product_catalog_url is None:
            from config.env import REPORT_PRODUCT_URL  # noqa: PLC0415

            product_catalog_url = REPORT_PRODUCT_URL
        product_catalog_url = (product_catalog_url or "").strip()
        if not product_catalog_url:
            raise ValueError("REPORT_PRODUCT_URL 未配置：请在 .env 中设置 SHULAN_REPORT_PRODUCT_URL")
        self.product_catalog_url = product_catalog_url

    async def fetch_product_catalog(
        self,
        *,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        try:
            url = validate_report_endpoint(self.product_catalog_url, setting="REPORT_PRODUCT_URL")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=timeout_seconds,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise FollowupGatewayError("product catalog redirects are not accepted", retryable=False)
                    response.raise_for_status()
                    payload = await response.json()
        except FollowupGatewayError:
            raise
        except aiohttp.ClientResponseError as exc:
            raise FollowupGatewayError(
                f"follow-up HTTP {exc.status}",
                retryable=exc.status == 429 or exc.status >= 500,
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise FollowupGatewayError(
                "follow-up HTTP request failed",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise FollowupGatewayError(
                "follow-up response is invalid",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise FollowupGatewayError(
                "follow-up response must be a JSON object",
                retryable=False,
            )
        return payload

    async def deliver_json(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 300.0,
    ) -> int:
        try:
            url = validate_report_endpoint(self.callback_url)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=False,
                ) as response:
                    status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise FollowupGatewayError(
                "follow-up callback request failed",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise FollowupGatewayError(
                "follow-up callback failed",
                retryable=False,
            ) from exc
        if not 200 <= status < 300:
            raise FollowupGatewayError(
                f"follow-up callback HTTP {status}",
                retryable=status == 429 or status >= 500,
            )
        return status


# 兼容旧导入名；本适配器已不再承担模型调用。
AionFollowupReportGateway = FollowupReportHttpGateway
