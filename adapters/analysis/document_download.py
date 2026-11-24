"""Bounded downloads of the exact file bytes, with pinned public DNS and TLS SNI."""

from __future__ import annotations

import asyncio
import ipaddress

import httpx

from adapters.analysis.image_probe import _resolve_host
from modules.analysis.application.documents import DocumentParsingError


class HttpDocumentDownloader:
    def __init__(self, *, timeout_seconds=60.0, max_bytes=50 * 1024 * 1024, transport=None, resolver=_resolve_host):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.transport = transport
        self.resolver = resolver

    async def download(self, file_url):
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._download(file_url)
        except (TimeoutError, OSError, httpx.HTTPError) as error:
            raise DocumentParsingError('文件下载失败或超时') from error

    async def _download(self, file_url):
        url = httpx.URL(file_url)
        if url.scheme not in {'http', 'https'} or not url.host or url.username or url.password or url.fragment:
            raise ValueError('文件 URL 必须是无凭据、无片段的 HTTP 或 HTTPS 地址')
        try:
            addresses = [str(ipaddress.ip_address(url.host))]
        except ValueError:
            addresses = await self.resolver(url.host)
        if not addresses:
            raise DocumentParsingError('文件地址无法解析')
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (
                not address.is_global
                or address.is_multicast
                or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
            ):
                raise ValueError('文件 URL 必须解析到公网地址')
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                'GET',
                url.copy_with(host=addresses[0]),
                headers={'Host': url.netloc.decode('ascii'), 'Accept-Encoding': 'identity'},
                extensions={'sni_hostname': url.host},
            ) as response:
                if response.is_redirect:
                    raise ValueError('文件下载不接受重定向，请提供直接下载地址')
                response.raise_for_status()
                if response.status_code != 200:
                    raise DocumentParsingError('文件下载未返回完整内容')
                if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise DocumentParsingError('文件下载不接受压缩传输')
                declared = response.headers.get('content-length', '')
                if declared.isdigit() and int(declared) > self.max_bytes:
                    raise DocumentParsingError('文件超过大小限制')
                parts, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > self.max_bytes:
                        raise DocumentParsingError('文件超过大小限制')
                    parts.append(part)
                if not size or (declared.isdigit() and size != int(declared)):
                    raise DocumentParsingError('文件下载内容为空或不完整')
                return b''.join(parts)
