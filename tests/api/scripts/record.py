"""真实 Record 完整响应联调；调用 LLM，并写入测试 Redis/MongoDB。

先启动后端，再执行 python -m tests.api.scripts.record。
使用 --supplement "饮食需要注意什么" 增加一批补充答疑，会增加模型调用费用。
"""

import json
import uuid

import requests
from sseclient import SSEClient

from tests.api.scripts.record_rounds import await_creation
from tests.api.scripts.record_rounds import parse_args
from tests.api.scripts.record_rounds import run_rounds

base_url = 'http://127.0.0.1:9002'

# user_id = f'test_user_{str(uuid.uuid4())[:8]}'
# SESSION_ID = str(uuid.uuid4())

user_id = f'test-record-user-{uuid.uuid4().hex[:8]}'
SESSION_ID = f'test-record-session-{uuid.uuid4().hex}'


def create_qa():
    """不传 doctorProductPackages，验证该可选参数缺失时仍可创建会话。"""
    url = f'{base_url}/api/v1/qa/create'
    data = {
        'userId': user_id,
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
    result = response.json()
    return result


def submit_answer(question_id, answer, other=None):
    url = f'{base_url}/api/v1/qa/answer/complete'
    data = {'userId': user_id, 'sessionId': SESSION_ID, 'questionId': question_id, 'answer': answer}
    if other:
        data['other'] = other

    response = requests.post(url, json=data, timeout=180)
    result = response.json()
    return result


def submit_answer_stream(question_id, answer, other=None):
    url = f'{base_url}/api/v1/qa/answer/stream'
    data = {'userId': user_id, 'sessionId': SESSION_ID, 'questionId': question_id, 'answer': answer}
    if other:
        data['other'] = other

    response = requests.post(url, json=data, stream=True, timeout=180)
    response.raise_for_status()
    client = SSEClient(response)
    result = None
    for event in client.events():
        message_data = json.loads(event.data)
        if message_data.get('code') != 200 or message_data.get('data', {}).get('type') == 'error':
            raise RuntimeError(f'流式问答失败: {message_data}')
        if message_data['data']['type'] == 'reasoning':
            print(message_data['data']['content'], end='')
            pass
        elif message_data['data']['type'] == 'question':
            print()
            result = message_data
        elif message_data['data']['type'] == 'finish' and message_data['data'].get('status') == 'finished':
            result = message_data
    return result


def status(session_id):
    url = f'{base_url}/api/v1/qa/status'
    data = {'userId': user_id, 'sessionId': session_id}
    response = requests.post(url, json=data, timeout=180)
    result = response.json()
    return result


def history():
    history_url = f'{base_url}/api/v1/qa/history'
    data = {
        'userId': user_id,
        'sessionId': SESSION_ID,
    }
    response = requests.post(history_url, json=data)
    print()
    print(json.dumps(response.json(), ensure_ascii=False, indent=4))
    print()


def main():
    global base_url
    args = parse_args(description=__doc__)
    base_url = args.base_url.rstrip('/')
    result = run_rounds(
        await_creation(create_qa(), get_status=lambda: status(SESSION_ID)),
        submit_answer=submit_answer,
        supplements=args.supplement,
        intake_policy=args.intake_policy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=4))

    result = status(SESSION_ID)
    print(json.dumps(result, ensure_ascii=False, indent=4))
    print()

    history()


if __name__ == '__main__':
    main()
    # history()
