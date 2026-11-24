# -*- coding: utf-8 -*-
"""测试最终报告生成接口。

测试流程：
1. 使用已完成的问答会话
2. 调用最终报告生成接口
3. 验证接口立即返回 processing 状态

注意：
- 在运行此测试之前，需要先启动回调服务器：
  python api_test_report_callback_server.py
- 回调服务器会自动接收并显示报告数据
"""
import json

import requests

# 测试服务器地址
BASE_URL = "http://192.168.20.128:9002"  # 根据实际端口修改

# 回调接口
CALLBACK_PORT = 10002
CALLBACK_URL = f"http://192.168.20.128:{CALLBACK_PORT}/callback"

# 测试用户ID和会话ID（替换为实际的已完成会话）
user_id = 'test_user_676d9431'
session_id = 'aecc4b8f-4f8a-453a-8429-a550d388ab4a'


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
