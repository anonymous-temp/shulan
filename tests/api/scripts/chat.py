import json

import requests

from sseclient import SSEClient

from config import env

base_url = f'http://localhost:{env.SERVER_PORT}'
# base_url = f'http://123.57.56.88:9012'

# user_id = "test_user_" + str(uuid.uuid4())[:8]
# SESSION_ID = str(uuid.uuid4())

USER_ID = 'test_user_001'
SESSION_ID = '001_0011'


def suggestions():
    url = f"{base_url}/api/v1/chat/suggestions"
    data = {
        "userId": USER_ID,
    }
    response = requests.get(url, params=data)
    result = response.json()
    print(result)
    print()


def send_complete():
    url = f"{base_url}/api/v1/chat/send/complete"

    while True:
        message = input('>>>')
        data = {
            "userId": USER_ID,
            "sessionId": SESSION_ID,
            "message": message,
            "thinking": True,
        }
        response = requests.post(url, json=data)
        result = response.json()
        print(repr(result['data'].get('reasoning')))
        print(repr(result['data'].get('reply')))
        print()


def send_stream():
    url = f"{base_url}/api/v1/chat/send/stream"

    while True:
        message = input('>>>')
        data = {
            "userId": USER_ID,
            "sessionId": SESSION_ID,
            "message": message,
            "thinking": True,
        }
        response = requests.post(url, json=data, stream=True)

        # content_flag = False
        # reasoning_flag = False
        # for chunk in response.iter_lines(decode_unicode=True):
        #     print(chunk)
        #     print(type(chunk))
        #     print()
        # print()

        response.raise_for_status()
        client = SSEClient(response)
        next_question = None
        content = 0
        for event in client.events():
            message_data = json.loads(event.data)
            data_type = message_data['data']['type']
            if data_type == 'reasoning':
                print(message_data['data']['content'], end='', flush=True)
            elif data_type == 'content' and content == 0:
                print('\n' * 2)
                content += 1
                print(message_data['data']['content'], end='', flush=True)
            elif data_type == 'content':
                content += 1
                print(message_data['data']['content'], end='', flush=True)
            elif data_type == 'finish':
                print()


def history():
    url = f"{base_url}/api/v1/chat/history"

    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID,
    }
    response = requests.post(url, json=data)
    result = response.json()
    print(json.dumps(result, ensure_ascii=False, indent=4))


def insert_custom_message():
    """向已结束建档并进入普通对话阶段的会话插入医生签发卡片。"""
    url = f"{base_url}/api/v1/chat/messages/custom"
    data = {
        "userId": USER_ID,
        "sessionId": SESSION_ID,
        "eventId": "test-doctor-signed-001",
        "displayType": "CARD",
        "customType": "HEALTH_REPORT_CARD",
        "title": "健康评估报告",
        "content": "您的健康报告已经由医生审核完成，请点击查看详情。",
        "payload": {
            "reportId": "test-report-001",
            "reportType": "health_report",
            "version": 1,
        },
    }
    response = requests.post(url, json=data)
    print(json.dumps(response.json(), ensure_ascii=False, indent=4))


if __name__ == "__main__":
    # suggestions()
    # send_complete()
    send_stream()
    # history()
    # insert_custom_message()
