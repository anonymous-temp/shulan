"""Bounded streaming summaries preserve structure without replaying history."""

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from utils.log.preview import PreviewLimits
from utils.log.preview import PreviewText
from utils.log.preview import preview
from web import http_logging
from web.stream_log import StreamLogSummary


@pytest.mark.parametrize('limit', [1, 2, 31, 32, 33, 2000])
@pytest.mark.parametrize('chunk_size', [1, 7, 3000])
def test_rolling_text_matches_whole_text_preview_with_exact_single_truncation(limit, chunk_size):
    text = '开头' + 'abcdefghijklmnopqrstuvwxyz中文\n😀' * 100 + '结束'
    limits = replace(PreviewLimits(), string_chars=limit)
    rolling = PreviewText(limit)
    for offset in range(0, len(text), chunk_size):
        rolling.append(text[offset : offset + chunk_size])
        assert len(rolling.head) + len(rolling.tail) <= limit
    assert preview(rolling, limits) == preview(text, limits)


@pytest.mark.parametrize('text', ['', '一', '短文本', 'a' * 32])
def test_short_text_is_complete_without_duplicate_head_tail(text):
    rolling = PreviewText(32)
    for char in text:
        rolling.append(char)
    assert json.loads(preview(rolling)) == text


def test_thousands_of_events_use_bounded_state_and_latest_real_metadata():
    limits = replace(PreviewLimits(), string_chars=32)
    summary = StreamLogSummary(limits)
    for index in range(5000):
        for event_type, content in [('reasoning', '思'), ('content', str(index % 10))]:
            summary.observe(
                {
                    'code': 200,
                    'message': 'success',
                    'uuid': 'service-uuid',
                    'timestamp': index,
                    'elapsed_ms': index,
                    'data': {'type': event_type, 'userId': 'u', 'sessionId': 's', 'content': content},
                },
                event_type=event_type,
                failed=False,
            )
        assert summary.data.used <= summary.data.capacity
        assert len(summary.data.values) == 5
        assert len(summary.metadata.values) == 5
    summary.observe({'code': 200, 'timestamp': 5001, 'data': {'type': 'finish'}}, event_type='finish', failed=False)
    result = json.loads(preview(summary.snapshot(), limits))
    assert result['uuid'] == 'service-uuid'
    assert result['timestamp'] == 5001
    assert result['data']['type'] == 'finish'
    assert result['data']['reasoning'] == '思' * 16 + '***' + '思' * 16
    text = ''.join(str(index % 10) for index in range(5000))
    assert result['data']['content'] == text[:16] + '***' + text[-16:]


def test_question_fields_survive_finish_and_original_payload_is_not_retained():
    limits = replace(PreviewLimits(), list_items=2, string_chars=32)
    summary = StreamLogSummary(limits)
    payload = {
        'code': 200,
        'data': {
            'type': 'question',
            'questionId': 'q1',
            'question': '问题',
            'options': [{'id': str(i), 'label': str(i)} for i in range(5)],
            'assistantMessage': '说明' * 100,
            'password': 'never-log',
        },
    }
    summary.observe(payload, event_type='question', failed=False)
    payload['data']['options'][0]['label'] = 'MUTATED'
    summary.observe({'code': 200, 'data': {'type': 'finish'}}, event_type='finish', failed=False)
    result = json.loads(preview(summary.snapshot(), limits))['data']
    assert result['type'] == 'finish' and result['questionId'] == 'q1'
    assert result['options'] == [{'id': '0', 'label': '0'}, {'id': '1', 'label': '1'}, '***']
    assert result['assistantMessage'] == '说明' * 8 + '***' + '说明' * 8
    assert result['password'] == '[REDACTED]'


@pytest.mark.parametrize('bare', [False, True])
def test_error_metadata_and_details_cannot_be_overwritten_by_later_finish(bare):
    summary = StreamLogSummary(PreviewLimits())
    summary.observe({'code': 200, 'message': 'success', 'data': {'type': 'start'}}, event_type='start', failed=False)
    error = {'type': 'error', 'error': '失败'}
    payload = error if bare else {'code': 409, 'message': '冲突', 'data': error}
    summary.observe(payload, event_type='error', failed=True)
    summary.observe({'code': 200, 'message': 'success', 'data': {'type': 'finish'}}, event_type='finish', failed=False)
    result = json.loads(preview(summary.snapshot()))
    assert result['data'] == error
    if bare:
        assert 'code' not in result and 'message' not in result
    else:
        assert result['code'] == 409 and result['message'] == '冲突'


def test_large_fields_and_many_new_fields_stay_bounded_in_memory_and_output():
    limits = replace(PreviewLimits(), total_bytes=1024, dict_items=8, list_items=2, depth=3)
    summary = StreamLogSummary(limits)
    for index in range(100):
        summary.observe(
            {
                'data': {
                    'type': 'question',
                    f'field{index}': {'nested': {'text': 'x' * 10000}},
                }
            },
            event_type='question',
            failed=False,
        )
    assert summary.data.used <= summary.data.capacity
    assert len(summary.data.values) <= limits.dict_items
    output = preview(summary.snapshot(), limits)
    assert len(output.encode()) <= limits.total_bytes
    result = json.loads(output)
    assert '***' in output
    assert 'x' * 100 not in output
    assert isinstance(result, dict)


def test_disconnect_completion_fallback_uses_access_hook_once():
    logger = Mock()
    handler = SimpleNamespace(
        request=SimpleNamespace(method='POST', path='/chat', remote_ip='127.0.0.1', request_time=lambda: 0.1),
        request_uuid='HTTP-UUID',
        get_status=lambda: 200,
        _headers={'Content-Type': 'text/event-stream'},
        _client_disconnected=True,
    )
    handler.application = SimpleNamespace(
        log_request=lambda current: http_logging.log_access(current, request_logger=logger)
    )
    http_logging.ensure_stream_log(handler)
    http_logging.ensure_stream_log(handler)
    http_logging.log_access(handler, request_logger=logger)
    logger.info.assert_called_once()
    assert 'stream_state=empty' in logger.info.call_args.args[0]


def test_disabled_info_does_not_allocate_success_summary_but_error_still_logs_its_body():
    logger = logging.Logger('errors-only', logging.ERROR)
    handler = SimpleNamespace(request_uuid='HTTP-UUID')
    with (
        patch.object(http_logging, 'logger', logger),
        patch.object(http_logging.env, 'HTTP_LOG_BODY_ENABLED', True),
    ):
        http_logging.log_stream_event(
            handler, {'code': 200, 'data': {'type': 'content', 'content': 'success'}}, outcome='flushed'
        )
        assert not hasattr(handler, '_http_log_stream_summary')
        http_logging.log_stream_event(
            handler, {'code': 500, 'data': {'type': 'error', 'error': 'failed'}}, outcome='flushed'
        )
    result = json.loads(preview(handler._http_log_stream_summary.snapshot()))
    assert result == {'code': 500, 'data': {'type': 'error', 'error': 'failed'}}
