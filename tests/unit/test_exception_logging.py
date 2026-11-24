"""Handled failures remain diagnosable without multiline Python tracebacks."""

import io
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tornado.web

from runtime import TaskSupervisor
from utils.exceptions import format_exception_summary
from web.handlers.base import BaseHandler


def recording_logger():
    output = io.StringIO()
    logger = logging.Logger('exception-summary-test')
    logger.addHandler(logging.StreamHandler(output))
    return logger, output


def chained_error():
    try:
        try:
            raise TimeoutError('submission response\r\nnot received')
        except TimeoutError as error:
            raise RuntimeError('document submission failed') from error
    except RuntimeError as error:
        return error


def test_summary_retains_root_cause_and_location_without_traceback_or_multiline_messages():
    summary = format_exception_summary(chained_error())
    assert 'error_type=RuntimeError' in summary
    assert 'error_chain=RuntimeError -> TimeoutError' in summary
    assert 'root_message=submission response not received' in summary
    assert 'test_exception_logging.py:' in summary
    assert len(summary.splitlines()) == 1
    assert 'Traceback' not in summary


def test_summary_respects_suppressed_context_and_handles_circular_causes():
    error = chained_error()
    error.__cause__.__cause__ = error
    assert 'RuntimeError -> TimeoutError' in format_exception_summary(error)
    try:
        try:
            raise error
        except RuntimeError:
            raise ValueError('public error') from None
    except ValueError as public:
        assert 'root_message' not in format_exception_summary(public)


@pytest.mark.parametrize('status', [400, 502, None])
def test_http_boundary_logs_one_line_for_expected_and_unexpected_errors(status):
    logger, output = recording_logger()
    handler = SimpleNamespace(
        request=SimpleNamespace(method='POST', path='/api/v1/qa/create', remote_ip='127.0.0.1'),
        request_uuid='REQUEST-LOG-1',
    )
    root = chained_error()
    error = root if status is None else tornado.web.HTTPError(status, log_message='document submission failed')
    if error is not root:
        error.__cause__ = root
    with patch('web.handlers.base.logger', logger):
        BaseHandler.log_exception(handler, type(error), error, error.__traceback__)
    logged = output.getvalue()
    assert len(logged.splitlines()) == 1
    assert 'request_uuid=REQUEST-LOG-1' in logged
    assert f'status_code={status or 500}' in logged
    assert 'path=/api/v1/qa/create' in logged
    assert 'TimeoutError' in logged
    assert 'test_exception_logging.py:' in logged
    assert 'Traceback' not in logged


@pytest.mark.asyncio
async def test_supervised_failure_is_consumed_and_logged_without_traceback():
    logger, output = recording_logger()
    supervisor = TaskSupervisor()

    async def fail():
        raise chained_error()

    with patch('runtime.supervisor.logger', logger):
        task = supervisor.create_task(fail(), name='document-processing')
        with pytest.raises(RuntimeError, match='document submission failed'):
            await task
        await supervisor.wait_idle()
    logged = output.getvalue()
    assert supervisor.active_task_count == 0
    assert len(logged.splitlines()) == 1
    assert 'name=document-processing' in logged
    assert 'RuntimeError -> TimeoutError' in logged
    assert 'submission response not received' in logged
    assert 'Traceback' not in logged
