# -*- coding: utf-8 -*-
"""开发联调回调接收端；不调用模型、业务后端或读取部署配置。

页面服务在 /callback 复用此 Handler。独立运行默认监听 10002 并保存
调试副本，可用 --no-save 仅接收；有效 JSON 对象始终确认 HTTP 200。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

import tornado.httpserver
import tornado.ioloop
import tornado.web

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from utils.artifact_paths import external_artifact_path

CALLBACK_PORT = 10002
SAVE_DIR = Path(gettempdir()) / "shulan-received-reports"
logger = logging.getLogger(__name__)


def save_report(report: dict, directory: Path) -> None:
    directory = external_artifact_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Repeated callbacks are harmless: archive each receipt separately without
    # using request data as a filename or overwriting an earlier receipt.
    filename = directory / f"report_{timestamp}_{uuid4().hex}.json"
    with filename.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=4)


class CallbackHandler(tornado.web.RequestHandler):
    """只接收和确认；业务失败报告也应得到成功的投递确认。"""

    def initialize(self, save_dir: Path | None = None) -> None:
        self.save_dir = Path(save_dir) if save_dir is not None else None

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.set_header("Cache-Control", "no-store")

    def get(self):
        self.write({"code": 200, "message": "模拟报告回调接收端已就绪"})

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        try:
            report = json.loads(self.request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.set_status(400)
            self.write({"code": 400, "message": "Invalid JSON"})
            return
        if not isinstance(report, dict):
            self.set_status(400)
            self.write({"code": 400, "message": "JSON object required"})
            return

        saved = False
        if self.save_dir is not None:
            try:
                await asyncio.to_thread(save_report, report, self.save_dir)
                saved = True
            except (OSError, ValueError) as error:
                # This is an optional debug copy, not the authoritative report
                # archive. Disk problems must not make the mock reject receipt.
                logger.warning('模拟回调已接收，本地副本保存失败: error_type=%s', type(error).__name__)
        logger.info('模拟报告回调已接收: bytes=%s saved=%s', len(self.request.body), saved)
        self.write({
            "code": 200, "message": "success",
            "data": {"received": True, "saved": saved},
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })


def make_app(*, save_dir: Path | None = SAVE_DIR):
    """创建应用"""
    save_dir = external_artifact_path(save_dir) if save_dir is not None else None
    return tornado.web.Application([
        (r"/callback", CallbackHandler, {"save_dir": save_dir}),
    ])


def main():
    """启动回调服务器"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=CALLBACK_PORT)
    parser.add_argument('--save-dir', type=Path, default=SAVE_DIR)
    parser.add_argument('--no-save', action='store_true', help='仅接收并确认，不保存调试副本')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    save_dir = None if args.no_save else args.save_dir
    app = make_app(save_dir=save_dir)
    server = tornado.httpserver.HTTPServer(app)
    server.listen(args.port, address=args.host)
    print(f'模拟回调: http://{args.host}:{args.port}/callback（后端请使用可达 IP）', flush=True)
    print(f'调试副本目录: {save_dir}' if save_dir else '仅接收，不保存调试副本', flush=True)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
