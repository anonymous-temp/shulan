"""Dynamic Record smoke client shared policy; never connects on import."""

import argparse
import json
import time


def parse_args(argv=None, *, description='真实 Record API 联调'):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--base-url', default='http://127.0.0.1:9002', help='正在运行的后端；不读取 .env')
    parser.add_argument(
        '--supplement', action='append', default=[], help='补充入口提交的实际疑问；可以多次指定，默认回复没有了'
    )
    parser.add_argument(
        '--intake-policy',
        choices=('auto', 'new', 'legacy'),
        default='auto',
        help='auto兼容新旧预算但不猜测策略；new验收新会话至多15题；legacy验收历史策略至多10题（含显式6题短策略）',
    )
    return parser.parse_args(argv)


def answer_for(question_data):
    options = question_data.get('options')
    if question_data['questionType'] == 'text':
        return {key: '测试场景：偶尔餐后腹胀，持续两周，无已知病史。' for key in (options or {'content': ''})}
    if question_data['questionType'] not in ('radio', 'checkbox') or not options:
        raise RuntimeError('问题类型或选项无效，停止联调')
    return [next(iter(options))]


def validate_opening(question_data):
    """Check newly created openings; cached legacy text questions remain valid server-side."""
    options = question_data.get('options')
    if (
        question_data.get('questionType') not in ('radio', 'checkbox')
        or not isinstance(options, list)
        or not 2 <= len(options) <= 12
        or any(not isinstance(option, str) or not option.strip() for option in options)
        or len({option.strip() for option in options}) != len(options)
    ):
        raise RuntimeError('新建首题需要至少两个、最多十二个非空且不重复的单选或多选项，停止联调')


def _response_data(response):
    if not isinstance(response, dict) or response.get('code') != 200:
        raise RuntimeError(f'问答接口调用失败: {response}')
    data = response.get('data')
    if not isinstance(data, dict) or data.get('type') == 'error':
        raise RuntimeError(f'问答接口未返回有效数据: {response}')
    return data


def await_creation(response, *, get_status, timeout=7200, clock=time.monotonic, sleep=time.sleep):
    """Poll the accepted session; retries never allocate a different identity."""
    deadline = clock() + timeout
    while True:
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f'创建接口未返回可查询状态: {response}')
        if data.get('status') in ('question', 'finished'):
            _response_data(response)
            return response
        if data.get('status') == 'failed' and not data.get('retryable'):
            raise RuntimeError(f'建档创建停止恢复: {data.get("errorCode")} {data.get("message")}')
        if data.get('status') not in ('processing', 'failed'):
            raise RuntimeError(f'未知建档创建状态: {data}')
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError('等待建档创建超时，请使用当前会话查询状态')
        sleep(min(remaining, 30, max(1, data.get('retryAfterSeconds') or 3)))
        response = get_status()


def run_rounds(first_response, *, submit_answer, supplements=(), intake_policy='auto'):
    """Legacy helper name retained; only the answer endpoint advances the flow."""
    if intake_policy not in ('auto', 'new', 'legacy'):
        raise ValueError('未知建档预算模式')
    max_intake, max_interactions = (10, 20) if intake_policy == 'legacy' else (15, 25)
    response = first_response
    additions = iter(supplements)
    previous_counts = (0, 0, 0)
    seen_questions = set()
    left_intake = False
    # Bound requests independently: invitations and empty replies are not QA rounds.
    for _ in range(64):
        data = _response_data(response)
        counts = tuple(data.get(key) for key in ('interactionCount', 'intakeQuestionCount', 'qaRoundCount'))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise RuntimeError('建档或答疑计数无效，停止联调')
        interaction_count, intake_count, qa_count = counts
        if any(not previous <= current <= previous + 1 for previous, current in zip(previous_counts, counts)):
            raise RuntimeError('一次提交不可跳增或回退交互计数')
        if not 0 <= intake_count <= max_intake or not 0 <= qa_count <= 5 or interaction_count > max_interactions:
            raise RuntimeError('超过建档预算或答疑五轮上限')
        previous_counts = counts
        if data.get('round') != 1 or not data.get('assistantMessage'):
            raise RuntimeError('缺少独立回应或返回了固定多轮')
        print(json.dumps(data, ensure_ascii=False))
        if data.get('status') == 'finished':
            return response
        if data.get('status') != 'question' or qa_count >= 5:
            raise RuntimeError('达到答疑五轮后未自动完成')
        question_id = data['questionId']
        if not seen_questions and intake_count == 0 and data.get('phase') == 'intake':
            validate_opening(data)
        if question_id in seen_questions:
            raise RuntimeError('服务端重复返回问题 ID')
        seen_questions.add(question_id)
        phase = data.get('phase')
        if phase not in ('intake', 'supplement', 'qa'):
            raise RuntimeError('未知建档阶段')
        if phase == 'intake':
            if left_intake or intake_count >= max_intake or qa_count:
                raise RuntimeError('建档阶段或题数不符合边界')
            answer = answer_for(data)
        else:
            left_intake = True
            # No public field identifies historical policy snapshots. Auto and
            # legacy accept genuine earlier exits; they never infer a policy
            # from the observed count. New sessions retain the three themes.
            if intake_count < (3 if intake_policy == 'new' else 1):
                raise RuntimeError('正常建档未完成固定主题就邀请提问')
            answer = next(additions, '没有了')
        response = submit_answer(question_id, answer)
    raise RuntimeError('消息数量异常，停止联调')
