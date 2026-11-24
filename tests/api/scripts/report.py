# -*- coding: utf-8 -*-
"""测试最终报告生成接口。

测试流程：
1. 使用已完成的问答会话
2. 调用最终报告生成接口
3. 验证接口立即返回 processing 状态

注意：
- 先启动页面服务 python -m tests.api.serve_pages（9080），自带 /callback 接收端；
  也可独立运行 python -m tests.api.scripts.report_callback_server（10002）。
- 将后端 SHULAN_REPORT_CALLBACK_URL 配置为对应可达地址并重启，再提交新报告。
- 模拟回调只确认接收；需要查看正文时使用报告详情接口或启用调试副本保存。
"""
import json

import requests

# 测试服务器地址
BASE_URL = "http://localhost:9002"  # 根据实际端口修改

# 测试用户ID和会话ID（替换为实际的已完成会话）
user_id = "test_user_001"
session_id = "001_0014"


def report_generation():
    url = f"{BASE_URL}/api/v1/report"
    data = {
        "userId": user_id,
        "sessionId": session_id,
    }
    response = requests.post(url, json=data, timeout=10)
    print(response)
    result = response.json()
    print(json.dumps(result, ensure_ascii=False, indent=4))


if __name__ == "__main__":
    report_generation()
