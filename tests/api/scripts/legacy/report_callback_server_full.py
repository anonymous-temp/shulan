# -*- coding: utf-8 -*-
"""回调服务器模拟器 - 用于接收最终报告的回调数据。

使用说明：
1. 运行此脚本启动回调服务器: python api_test_report_callback_server.py
2. 服务器将监听指定端口（默认8888）
3. 当收到报告回调时，会自动显示并保存到文件
4. 按 Ctrl+C 退出服务器
"""
import json
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir

import tornado.httpserver
import tornado.ioloop
import tornado.web

# 回调服务器配置
CALLBACK_PORT = 10002
SAVE_DIR = Path(gettempdir()) / "shulan-received-reports"

# 创建保存目录
SAVE_DIR.mkdir(parents=True, exist_ok=True)


class CallbackHandler(tornado.web.RequestHandler):
    """回调接口处理器"""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        try:
            # 解析请求体
            report = json.loads(self.request.body)

            # 显示接收信息
            print(f"\n{'=' * 60}")
            print(f"收到报告回调 [{datetime.now().strftime('%H:%M:%S')}]")
            print(f"{'=' * 60}")
            print(f"用户ID: {report.get('userId', 'N/A')}")
            print(f"会话ID: {report.get('sessionId', 'N/A')}")

            # 保存到文件
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_id = report.get('sessionId', 'unknown')
            prefix = session_id[:8] if session_id != 'unknown' else 'unknown'
            filename = SAVE_DIR / f"report_{prefix}_{timestamp}.json"

            with filename.open('w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

            print(f"已保存: {filename}")
            print(f"{'=' * 60}\n")

            # 返回成功响应
            self.write({
                "code": 200,
                "message": "success",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        except json.JSONDecodeError:
            print("\n解析 JSON 失败\n")
            self.set_status(400)
            self.write({"code": 400, "message": "Invalid JSON"})

        except Exception as e:
            print(f"\n处理失败: {e}\n")
            self.set_status(500)
            self.write({"code": 500, "message": "Internal server error"})


def make_app():
    """创建应用"""
    return tornado.web.Application([
        (r"/callback", CallbackHandler),
    ])


def main():
    """启动回调服务器"""
    print("=" * 60)
    print("最终报告回调服务器")
    print("=" * 60)
    print(f"监听端口: {CALLBACK_PORT}")
    print(f"回调路径: /callback")
    print(f"保存目录: {SAVE_DIR}")
    print(f"服务器地址: http://192.168.20.128:{CALLBACK_PORT}/callback")
    print(f"按 Ctrl+C 停止")
    print("=" * 60)

    app = make_app()
    server = tornado.httpserver.HTTPServer(app)
    server.listen(CALLBACK_PORT)

    print(f"\n服务器已启动，等待接收报告...\n")

    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        print("\n\n服务器已停止\n")


if __name__ == "__main__":
    main()
