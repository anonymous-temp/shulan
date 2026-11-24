"""AI问答接口测试 - 通过Web服务"""
import uuid
import json

import requests
from sseclient import SSEClient

import settings

BASE_URL = f'http://localhost:{settings.SERVER_PORT}'

# 测试数据
USER_ID = f'test_user_{str(uuid.uuid4())[:8]}'
SESSION_ID = str(uuid.uuid4())

def create_qa():
    """不传 doctorProductPackages，创建问答会话并获取第一个问题。"""
    url = f'{BASE_URL}/api/v1/qa/create'
    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID,
    }
    response = requests.post(url, json=data)
    response.raise_for_status()
    return response.json()


def submit_answer_stream(question_id, answer, other=None):
    """提交答案并流式获取下一个问题"""
    url = f"{BASE_URL}/api/v1/qa/answer/stream"
    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID,
        "questionId": question_id,
        "answer": answer
    }
    if other:
        data["other"] = other

    response = requests.post(url, json=data, stream=True)
    response.raise_for_status()

    client = SSEClient(response)
    next_question = None

    for event in client.events():
        message_data = json.loads(event.data)
        data_type = message_data['data']['type']

        if data_type == 'reasoning':
            # 推理过程：逐字输出
            print(message_data['data']['content'], end='', flush=True)
        elif data_type == 'question':
            print()  # 推理结束后换行
            next_question = message_data
        elif data_type == 'finish':
            # 问答完成
            if message_data['data'].get('status') == 'finished':
                return message_data

    return next_question


def get_status():
    """获取会话状态"""
    url = f"{BASE_URL}/api/v1/qa/status"
    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID
    }
    response = requests.post(url, json=data)
    response.raise_for_status()
    return response.json()


def get_history():
    """获取问答历史"""
    url = f"{BASE_URL}/api/v1/qa/history"
    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID,
    }
    response = requests.post(url, json=data)
    response.raise_for_status()
    return response.json()


def print_question(data):
    """格式化输出问题"""
    print(f"\n{'='*60}")
    print(f"状态: {data['status']}")
    print(f"进度: {data['progress']}")

    if data['status'] == 'question':
        print(f"问题ID: {data['questionId']}")
        print(f"问题类型: {data['questionType']}")
        print(f"问题: {data['question']}")
        print(f"选项: {data['options']}")
    print(f"{'='*60}\n")


def main():
    print(f"\n测试用户: {USER_ID}")
    print(f"会话ID: {SESSION_ID}\n")

    # 1. 创建会话并获取第一个问题
    print(">>> 创建问答会话...")
    result = create_qa()
    print_question(result['data'])

    # 2. 回答第一个问题（基本信息）
    first_question_id = result['data']['questionId']
    first_answer = {
        "name": "张三",
        "gender": "男",
        "age": "30",
        "height": "175",
        "weight": "70",
    }
    print(f">>> 提交答案: {first_answer}")
    next_result = submit_answer_stream(first_question_id, first_answer)

    # 3. 循环回答后续问题
    question_count = 2
    while next_result and next_result['data']['status'] == 'question':
        print_question(next_result['data'])

        # 自动生成答案
        question_type = next_result['data']['questionType']
        if question_type == 'text':
            answer = next_result['data']['options']
        elif question_type == 'radio':
            answer = [next_result['data']['options'][0]]
        else:  # checkbox
            answer = [next_result['data']['options'][0]]

        print(f">>> 提交答案: {answer}")
        next_result = submit_answer_stream(next_result['data']['questionId'], answer)
        question_count += 1

        # 防止无限循环
        if question_count > 10:
            print(">>> 问答超过10轮，强制退出")
            break

    # 4. 问答完成
    if next_result and next_result['data']['status'] == 'finished':
        print(f"\n{'='*60}")
        print(f"问答完成！进度: {next_result['data']['progress']}")
        print(f"{'='*60}\n")

    # 5. 获取会话状态
    print(">>> 获取会话状态...")
    status_result = get_status()
    print(f"状态: {status_result['data']['status']}, 进度: {status_result['data']['progress']}\n")

    # 6. 获取问答历史
    print(">>> 获取问答历史...")
    history_result = get_history()
    print(f"问答历史 (共 {len(history_result['data']['history'])} 条):")
    for idx, qa in enumerate(history_result['data']['history'], 1):
        print(f"  {idx}. {qa['question']}")
        print(f"     答案: {qa['answer']}")

    print("\n测试完成！")


if __name__ == "__main__":
    main()
