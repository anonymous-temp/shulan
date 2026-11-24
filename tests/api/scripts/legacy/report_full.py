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

# 测试用户ID和会话ID（替换为实际的已完成会话）
TEST_USER_ID = "test_user_f9c03927"  # 替换为实际的用户ID
TEST_SESSION_ID = "75f95cb1-896b-4610-a3fc-dcaefc49a12d"  # 替换为实际的会话ID

# 回调服务器配置（需要与 api_test_report_callback_server.py 中的配置一致）
CALLBACK_PORT = 10002
CALLBACK_URL = f"http://192.168.20.128:{CALLBACK_PORT}/callback"


def test_report_generation():
    """测试报告生成接口 - 完整参数"""
    print("\n=== 测试1: 完整参数报告生成 ===")

    # 构建测试用的产品包数据
    doctor_product_packages = [
        {
            "id": 1,
            "packageName": "脂肪肝调理组方",
            "applicablePeople": "脂肪肝、血脂异常人群",
            "listingStatus": 1,
            "sortNo": 100,
            "products": [
                {
                    "id": 101,
                    "serviceName": "Omega-3鱼油软胶囊",
                    "forceRecommend": 0,
                    "functionAttributeTags": "护肝,降脂,抗炎",
                    "productEffectDescription": "辅助降低甘油三酯，改善脂质代谢，减轻肝脏脂肪沉积"
                },
                {
                    "id": 102,
                    "serviceName": "复合益生菌",
                    "forceRecommend": 1,
                    "functionAttributeTags": "肠道,菌群,代谢",
                    "productEffectDescription": "调节肠道菌群，改善肠-肝轴，辅助代谢健康"
                }
            ]
        },
        {
            "id": 2,
            "packageName": "粒子产品",
            "applicablePeople": "全人群适用",
            "listingStatus": 1,
            "sortNo": 90,
            "products": [
                {
                    "id": 201,
                    "serviceName": "镁元素补充剂",
                    "functionAttributeTags": "睡眠,代谢,放松",
                    "productEffectDescription": "支持糖代谢，改善睡眠质量，缓解肌肉紧张"
                }
            ]
        }
    ]

    url = f"{BASE_URL}/api/v1/report"
    data = {
        "userId": TEST_USER_ID,
        "sessionId": TEST_SESSION_ID,
        "callbackUrl": CALLBACK_URL,
        "name": "张三",
        "fileList": [
            {
                "url": "https://m.slmc.top/api/v1/file/preview?objectName=example.pdf",
                "fileType": "pdf"
            }
        ],
        "hospitalData": [],
        "weRunData": [],
        "doctorProductPackages": doctor_product_packages,
        "smartDeviceData": {
            "temperature": []
        }
    }

    print(f"请求: POST {url}")
    print(f"参数:")
    print(f"  - userId: {data['userId']}")
    print(f"  - sessionId: {data['sessionId']}")
    print(f"  - callbackUrl: {data['callbackUrl']}")
    print(f"  - 产品包数量: {len(data['doctorProductPackages'])}")

    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()

        print(f"\n响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] == 200:
            print("\n✓ 报告生成任务已启动")
            print(f"  状态: {result['data']['status']}")
            print(f"  消息: {result['data']['message']}")
            print(f"\n提示: 报告生成完成后会自动发送到回调服务器")
            print(f"      请查看回调服务器的输出")
            return True
        else:
            print(f"\n✗ 报告生成失败: {result['message']}")
            return False

    except requests.exceptions.Timeout:
        print("\n✗ 请求超时")
        return False
    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False


def test_report_with_minimal_params():
    """测试最小参数的报告生成"""
    print("\n=== 测试2: 最小参数报告生成 ===")

    url = f"{BASE_URL}/api/v1/report"
    data = {
        "userId": TEST_USER_ID,
        "sessionId": TEST_SESSION_ID,
        "callbackUrl": CALLBACK_URL
    }

    print(f"请求: POST {url}")
    print(f"参数: {json.dumps(data, ensure_ascii=False, indent=2)}")

    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()

        print(f"\n响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] == 200:
            print("\n✓ 最小参数报告生成任务已启动")
            return True
        else:
            print(f"\n✗ 报告生成失败: {result['message']}")
            return False

    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False


def test_report_with_invalid_session():
    """测试无效的会话ID"""
    print("\n=== 测试3: 无效会话ID ===")

    url = f"{BASE_URL}/api/v1/report"
    data = {
        "userId": TEST_USER_ID,
        "sessionId": "invalid_session_id",
        "callbackUrl": CALLBACK_URL
    }

    print(f"请求: POST {url}")
    print(f"参数: sessionId=invalid_session_id")

    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()

        print(f"\n响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] == 404:
            print("\n✓ 正确返回404错误（会话不存在）")
            return True
        else:
            print(f"\n✗ 应该返回404错误，但返回了: {result['code']}")
            return False

    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False


def test_report_missing_params():
    """测试缺少必填参数"""
    print("\n=== 测试4: 缺少必填参数 ===")

    url = f"{BASE_URL}/api/v1/report"

    # 测试缺少 userId
    data = {
        "sessionId": TEST_SESSION_ID,
        "callbackUrl": CALLBACK_URL
    }

    print(f"请求: POST {url}")
    print(f"参数: 缺少 userId")

    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()

        print(f"\n响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] == 400:
            print("\n✓ 正确返回400错误（缺少必填参数）")
            return True
        else:
            print(f"\n✗ 应该返回400错误，但返回了: {result['code']}")
            return False

    except Exception as e:
        print(f"\n✗ 请求失败: {e}")
        return False


def main():
    """主测试流程"""
    print("=" * 80)
    print("最终报告生成接口测试")
    print("=" * 80)
    print(f"\n测试配置:")
    print(f"  - API服务器: {BASE_URL}")
    print(f"  - 回调地址: {CALLBACK_URL}")
    print(f"  - 用户ID: {TEST_USER_ID}")
    print(f"  - 会话ID: {TEST_SESSION_ID}")
    print(f"\n重要提示:")
    print(f"  1. 请先启动回调服务器: python api_test_report_callback_server.py")
    print(f"  2. 确保 userId 和 sessionId 对应已完成的问答会话")
    print(f"  3. 报告生成需要30秒至2分钟，请在回调服务器查看结果")
    print("\n" + "=" * 80)

    # 测试1: 完整参数的报告生成
    test_report_generation()
    exit()

    # 测试2: 最小参数
    print("\n" + "=" * 80)
    test_report_with_minimal_params()

    # 测试3: 无效会话ID
    print("\n" + "=" * 80)
    test_report_with_invalid_session()

    # 测试4: 缺少必填参数
    print("\n" + "=" * 80)
    test_report_missing_params()

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)
    print("\n提示: 请查看回调服务器的输出以确认报告是否生成成功")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.ConnectionError:
        print("\n✗ 连接失败：请确保API服务器已启动（python server.py）")
    except Exception as e:
        print(f"\n✗ 测试出错: {e}")
        import traceback
        traceback.print_exc()
