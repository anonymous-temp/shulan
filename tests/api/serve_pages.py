"""为 Shulan 真实接口调试页面提供静态文件和同源反向代理。

运行方式（项目根目录、shulan conda 环境）：

    conda activate shulan
    python -m tests.api.serve_pages

然后打开 http://127.0.0.1:9080/。页面通过本服务的
``/proxy`` 转发请求到用户填写的真实 Shulan 后端；``/callback`` 提供开发用
报告回调接收端，只确认接收，不模拟报告生成。本服务不读取 .env。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.web import Application
from tornado.web import HTTPError as TornadoHTTPError
from tornado.web import RequestHandler
from tornado.web import StaticFileHandler

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.api.scripts.report_callback_server import CallbackHandler
from utils.artifact_paths import external_artifact_path

PAGES_DIR = Path(__file__).resolve().parent / 'pages'
ALLOWED_SCHEMES = {'http', 'https'}
FORWARDED_RESPONSE_HEADERS = {
    'content-type',
    'cache-control',
}


class IndexHandler(RequestHandler):
    def get(self) -> None:
        self.redirect('/index.html')


class ProxyHandler(RequestHandler):
    """把浏览器请求转发到明确指定的真实 HTTP(S) 后端。"""

    async def get(self) -> None:
        await self._proxy()

    async def post(self) -> None:
        await self._proxy()

    async def _proxy(self) -> None:
        targets = parse_qs(urlparse(self.request.uri).query).get('target', [])
        if not targets:
            raise TornadoHTTPError(400, reason='Missing target URL')
        target = targets[0]
        parsed = urlparse(target)
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
            raise TornadoHTTPError(400, reason='Invalid target URL')

        headers = {
            'Accept': self.request.headers.get('Accept', 'application/json'),
        }
        content_type = self.request.headers.get('Content-Type')
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(
            target,
            data=self.request.body if self.request.method != 'GET' else None,
            headers=headers,
            method=self.request.method,
        )

        loop = IOLoop.current()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: urlopen(request, timeout=180),
            )
        except HTTPError as error:
            self.set_status(error.code)
            self._copy_headers(error.headers)
            self.finish(error.read())
            return
        except URLError as error:
            raise TornadoHTTPError(
                502,
                reason=f'Cannot reach backend: {error.reason}',
            ) from error

        with response:
            self.set_status(response.status)
            self._copy_headers(response.headers)
            while True:
                chunk = await loop.run_in_executor(
                    None,
                    lambda: response.read1(4096),
                )
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()
            self.finish()

    def _copy_headers(self, headers) -> None:
        for name, value in headers.items():
            if name.lower() in FORWARDED_RESPONSE_HEADERS:
                self.set_header(name, value)


def make_app(
    *,
    callback_save_dir: Path | None = None,
) -> Application:
    callback_save_dir = external_artifact_path(callback_save_dir) if callback_save_dir is not None else None
    return Application(
        [
            (r'/', IndexHandler),
            (r'/proxy', ProxyHandler),
            (r'/callback', CallbackHandler, {'save_dir': callback_save_dir}),
            (
                r'/(.*)',
                StaticFileHandler,
                {'path': str(PAGES_DIR), 'default_filename': 'index.html'},
            ),
        ],
        static_hash_cache=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Shulan API browser pages')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=9080, type=int)
    parser.add_argument('--callback-save-dir', type=Path, help='可选：保存报告回调的调试副本，默认仅接收')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = HTTPServer(make_app(callback_save_dir=args.callback_save_dir))
    server.listen(args.port, address=args.host)
    print(f'API Lab: http://{args.host}:{args.port}/')
    print(f'模拟报告回调: http://{args.host}:{args.port}/callback（后端请使用可达 IP）', flush=True)
    try:
        IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == '__main__':
    main()
