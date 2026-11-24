# -*- coding: utf-8 -*-
"""测试建档问答历史记录接口

测试新增的 /api/v1/qa/history 接口
"""
import json
import requests

BASE_URL = "http://localhost:9002"

def test_qa_history():
    """测试获取问答历史记录"""
    print("=" * 60)
    print("测试：获取问答历史记录接口")
    print("=" * 60)

    # 假设有一个已完成的会话
    user_id = "test_user_001"
    session_id = "test_session_001"

    # 1. 创建会话
    print("\n1. 创建会话...")
    create_url = f"{BASE_URL}/api/v1/qa/create"
    create_data = {
        "userId": user_id,
        "sessionId": session_id
    }

    try:
        response = requests.post(create_url, json=create_data)
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] != 200:
            print("创建会话失败，停止测试")
            return

        first_question = result['data']
    except Exception as e:
        print(f"创建会话异常: {e}")
        return

    # 2. 提交第一个问题的答案（基本信息）
    print("\n2. 提交第一个问题的答案（基本信息）...")
    answer_url = f"{BASE_URL}/api/v1/qa/answer"
    answer_data = {
        "userId": user_id,
        "sessionId": session_id,
        "questionId": first_question['questionId'],
        "answer": {
            "name": "张三",
            "gender": "男",
            "age": "30",
            "height": "175",
            "weight": "70"
        }
    }

    try:
        response = requests.post(answer_url, json=answer_data)
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result['code'] != 200:
            print("提交答案失败")
            return
    except Exception as e:
        print(f"提交答案异常: {e}")
        return

    # 3. 继续提交后续问题的答案，直到完成
    print("\n3. 继续提交后续问题...")
    max_rounds = 10  # 最多10轮
    for round_num in range(2, max_rounds + 1):
        # 获取当前状态
        status_url = f"{BASE_URL}/api/v1/qa/status"
        status_data = {
            "userId": user_id,
            "sessionId": session_id
        }

        try:
            response = requests.post(status_url, json=status_data)
            result = response.json()

            if result['data']['status'] == 'finished':
                print(f"\n问答已完成！总共 {round_num - 1} 个问题")
                break

            # 提交当前问题的答案
            current_question = result['data']
            question_type = current_question['questionType']

            # 根据问题类型构造答案
            if question_type == 'radio':
                # 单选：选第一个选项
                answer_value = current_question['options'][0] if current_question.get('options') else "选项1"
            elif question_type == 'checkbox':
                # 多选：选前两个选项
                options = current_question.get('options', [])
                answer_value = options[:2] if len(options) >= 2 else options
            else:
                # 文本：随机填写
                answer_value = f"这是第{round_num}个问题的答案"

            answer_data = {
                "userId": user_id,
                "sessionId": session_id,
                "questionId": current_question['questionId'],
                "answer": answer_value
            }

            response = requests.post(answer_url, json=answer_data)
            result = response.json()

            if result['code'] != 200:
                print(f"第 {round_num} 轮提交答案失败")
                break

            print(f"第 {round_num} 轮答案已提交")

        except Exception as e:
            print(f"第 {round_num} 轮异常: {e}")
            break

    # 4. 测试获取历史记录
    print("\n4. 获取问答历史记录...")

    history_data = {
        "userId": user_id,
        "sessionId": session_id
    }

    try:
        response = requests.post(history_url, json=history_data)
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"\n完整响应:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if result['code'] == 200:
            history_data = result['data']
            print(f"\n历史记录摘要:")
            print(f"  用户ID: {history_data['userId']}")
            print(f"  会话ID: {history_data['sessionId']}")
            print(f"  状态: {history_data['status']}")
            print(f"  创建时间: {history_data['createdAt']}")
            print(f"  完成时间: {history_data['completedAt']}")
            print(f"  问答记录数: {len(history_data['history'])}")
            print(f"\n问答记录详情:")
            for idx, qa in enumerate(history_data['history'], 1):
                print(f"\n  问题 {idx}:")
                print(f"    问题ID: {qa['questionId']}")
                print(f"    问题类型: {qa['questionType']}")
                print(f"    问题内容: {qa['question']}")
                print(f"    答案: {qa['answer']}")
                print(f"    回答时间: {qa['answeredAt']}")
        else:
            print(f"\n获取历史记录失败: {result['message']}")

    except Exception as e:
        print(f"获取历史记录异常: {e}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


def test_history_not_found():
    """测试获取不存在的会话历史"""
    print("\n" + "=" * 60)
    print("测试：获取不存在的会话历史")
    print("=" * 60)

    history_url = f"{BASE_URL}/api/v1/qa/history"
    history_data = {
        "userId": "nonexistent_user",
        "sessionId": "nonexistent_session"
    }

    try:
        response = requests.post(history_url, json=history_data)
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        assert response.status_code == 404, "应该返回404"
        assert result['code'] == 404, "code应该是404"
        print("\n✓ 测试通过：正确返回404")

    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
    except Exception as e:
        print(f"\n异常: {e}")


def test_history_incomplete_session():
    """测试获取未完成会话的历史"""
    print("\n" + "=" * 60)
    print("测试：获取未完成会话的历史")
    print("=" * 60)

    user_id = "test_user_incomplete"
    session_id = "test_session_incomplete"

    # 1. 创建会话但不完成
    print("\n1. 创建会话（不完成）...")
    create_url = f"{BASE_URL}/api/v1/qa/create"
    create_data = {
        "userId": user_id,
        "sessionId": session_id
    }

    try:
        response = requests.post(create_url, json=create_data)
        result = response.json()

        if result['code'] != 200:
            print("创建会话失败")
            return

        # 2. 尝试获取历史（应该失败）
        print("\n2. 尝试获取历史...")
        history_url = f"{BASE_URL}/api/v1/qa/history"
        history_data = {
            "userId": user_id,
            "sessionId": session_id
        }

        response = requests.post(history_url, json=history_data)
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"响应: {json.dumps(result, ensure_ascii=False, indent=2)}")

        assert response.status_code == 404, "应该返回404"
        assert result['code'] == 404, "code应该是404"
        print("\n✓ 测试通过：未完成的会话无法获取历史")

    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
    except Exception as e:
        print(f"\n异常: {e}")


if __name__ == '__main__':
    print("开始测试建档问答历史记录接口\n")

    # 测试1：完整流程
    test_qa_history()

    # 测试2：不存在的会话
    test_history_not_found()

    # 测试3：未完成的会话
    test_history_incomplete_session()

    print("\n所有测试完成！")
