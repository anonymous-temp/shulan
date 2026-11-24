"""真实 Record SSE API 联调；调用 LLM，并写入测试 Redis/MongoDB。

先启动后端，再执行 python -m tests.api.scripts.record_stream。
使用 --supplement "饮食需要注意什么" 增加一批补充答疑，会增加模型调用费用。
"""

import json
import uuid

import requests
from sseclient import SSEClient

from tests.api.scripts.record_rounds import await_creation
from tests.api.scripts.record_rounds import parse_args
from tests.api.scripts.record_rounds import run_rounds

BASE_URL = 'http://127.0.0.1:9002'

# 测试数据
# USER_ID = f'test_user_{str(uuid.uuid4())[:8]}'
# SESSION_ID = str(uuid.uuid4())
USER_ID = f'test-record-user-{uuid.uuid4().hex[:8]}'
SESSION_ID = f'test-record-session-{uuid.uuid4().hex}'


def create_qa():
    """不传 doctorProductPackages，创建问答会话并获取第一个问题。"""
    url = f'{BASE_URL}/api/v1/qa/create'
    data = {
        'userId': USER_ID,
        'sessionId': SESSION_ID,
        'name': '测试用户',
        'basicInfo': {
            'gender': '男',
            'age': 30,
            'height': 175,
            'weight': 70,
        },
    }
    response = requests.post(url, json=data, timeout=180)
    print(response)
    print(response.status_code)
    print(response.json())
    response.raise_for_status()
    return response.json()


def submit_answer_stream(question_id, answer, other=None):
    """提交答案并流式获取下一个问题"""
    url = f'{BASE_URL}/api/v1/qa/answer/stream'
    data = {'userId': USER_ID, 'sessionId': SESSION_ID, 'questionId': question_id, 'answer': answer}
    if other:
        data['other'] = other

    response = requests.post(url, json=data, stream=True, timeout=180)
    response.raise_for_status()

    client = SSEClient(response)
    next_question = None

    for event in client.events():
        message_data = json.loads(event.data)
        if message_data.get('code') != 200 or message_data.get('data', {}).get('type') == 'error':
            raise RuntimeError(f'流式问答失败: {message_data}')
        data_type = message_data['data']['type']

        if data_type == 'reasoning':
            # 推理过程：逐字输出
            print(message_data['data']['content'], end='', flush=True)
        elif data_type == 'question':
            print()  # 推理结束后换行
            next_question = message_data
        elif data_type == 'finish':
            # finish 仅在 status=finished 时表示整个建档已完成
            if message_data['data'].get('status') == 'finished':
                return message_data

    return next_question


def get_status():
    """获取会话状态"""
    url = f'{BASE_URL}/api/v1/qa/status'
    data = {'userId': USER_ID, 'sessionId': SESSION_ID}
    response = requests.post(url, json=data, timeout=180)
    response.raise_for_status()
    return response.json()


def get_history():
    """获取问答历史"""
    url = f'{BASE_URL}/api/v1/qa/history'
    data = {
        'userId': USER_ID,
        'sessionId': SESSION_ID,
    }
    response = requests.post(url, json=data, timeout=180)
    response.raise_for_status()
    return response.json()


def print_question(data):
    """格式化输出问题"""
    print(f'\n{"=" * 60}')
    print(f'状态: {data["status"]}')
    print(f'进度: {data["progress"]}')

    if data['status'] == 'question':
        print(f'问题ID: {data["questionId"]}')
        print(f'问题类型: {data["questionType"]}')
        print(f'问题: {data["question"]}')
        print(f'选项: {data["options"]}')
    print(f'{"=" * 60}\n')


def main():
    global BASE_URL
    args = parse_args(description=__doc__)
    BASE_URL = args.base_url.rstrip('/')
    print(f'测试用户: {USER_ID}')
    print(f'会话ID: {SESSION_ID}')

    # 通过相同答案接口推进，直到服务端自动完成。
    result = run_rounds(
        await_creation(create_qa(), get_status=get_status),
        submit_answer=submit_answer_stream,
        supplements=args.supplement,
        intake_policy=args.intake_policy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 5. 获取会话状态
    print('>>> 获取会话状态...')
    status_result = get_status()
    print(f'状态: {status_result["data"]["status"]}, 进度: {status_result["data"]["progress"]}\n')

    # 6. 获取问答历史
    print('>>> 获取问答历史...')
    history_result = get_history()
    print(f'问答历史 (共 {len(history_result["data"]["history"])} 条):')
    for idx, qa in enumerate(history_result['data']['history'], 1):
        print(f'  {idx}. {qa["question"]}')
        print(f'     答案: {qa["answer"]}')

    print('\n测试完成！')


if __name__ == '__main__':
    main()
