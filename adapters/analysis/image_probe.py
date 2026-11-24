"""Bounded, public-address-only format probing for extensionless images."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable
from collections.abc import Callable

import httpx

from modules.analysis.ports import ImageProbeError
from modules.analysis.ports import UnsupportedImageFormatError


async def _resolve_host(hostname: str) -> list[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in rows))


class HttpImageFormatProbe:
    """Read at most 32 decoded bytes; never trust a MIME header as the format."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_file_bytes: int = 50 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] = _resolve_host,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_file_bytes = max_file_bytes
        self.transport = transport
        self.resolver = resolver

    async def detect(self, file_url: str) -> str:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._detect(file_url)
        except (TimeoutError, OSError, httpx.HTTPError) as error:
            raise ImageProbeError('图片文件获取失败或超时') from error

    async def _detect(self, file_url: str) -> str:
        try:
            url = httpx.URL(file_url)
        except httpx.InvalidURL as error:
            raise ValueError('图片 URL 无效') from error
        if url.scheme not in {'http', 'https'} or not url.host:
            raise ValueError('图片 URL 必须为 HTTP 或 HTTPS 地址')
        if url.username or url.password or url.fragment:
            raise ValueError('图片 URL 不得包含凭据或片段')
        try:
            addresses = [str(ipaddress.ip_address(url.host))]
        except ValueError:
            addresses = await self.resolver(url.host)
        if not addresses:
            raise ImageProbeError('图片文件地址无法解析')
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (
                not address.is_global
                or address.is_multicast
                or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
            ):
                raise ValueError('图片 URL 必须解析到公网地址')

        # Pin the checked address to prevent a second DNS lookup/rebinding.
        # Preserve both HTTP Host and TLS SNI/certificate verification.
        target = url.copy_with(host=addresses[0])
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                'GET',
                target,
                headers={
                    'Host': url.netloc.decode('ascii'),
                    'Range': 'bytes=0-31',
                    'Accept-Encoding': 'identity',
                },
                extensions={'sni_hostname': url.host},
            ) as response:
                if response.is_redirect:
                    raise ValueError('图片文件探测不接受重定向，请提供直接下载地址')
                response.raise_for_status()
                if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise ImageProbeError('图片文件探测不接受压缩传输')
                declared_size = response.headers.get('content-length', '')
                total_size = response.headers.get('content-range', '').rsplit('/', 1)[-1]
                if any(size.isdigit() and int(size) > self.max_file_bytes for size in (declared_size, total_size)):
                    raise ImageProbeError('图片文件超过大小限制')
                async for prefix in response.aiter_bytes(chunk_size=32):
                    return _image_extension(prefix)
        raise ImageProbeError('图片文件内容为空')


def _image_extension(prefix: bytes) -> str:
    if prefix.startswith(b'%PDF-'):
        raise ValueError('fileType=image 与实际 PDF 文件不一致')
    signatures = (
        (b'\xff\xd8\xff', 'jpg'),
        (b'\x89PNG\r\n\x1a\n', 'png'),
        (b'BM', 'bmp'),
        (b'GIF87a', 'gif'),
        (b'GIF89a', 'gif'),
    )
    for signature, extension in signatures:
        if prefix.startswith(signature):
            return extension
    raise UnsupportedImageFormatError('不支持的图片格式（仅支持 JPG、JPEG、PNG、BMP、GIF）')
