"""Best-effort HTTP diagnostics, separate from wire serialization and business data."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from functools import wraps

from config import env
from utils.log import getLogger
from utils.log.context import request_log_context
from utils.log.preview import PreviewLimits
from utils.log.preview import preview
from web.stream_log import StreamLogSummary

logger = getLogger(__name__)
LIMITS = PreviewLimits(
    string_chars=env.HTTP_LOG_STRING_CHARS,
    list_items=env.HTTP_LOG_LIST_ITEMS,
    dict_items=env.HTTP_LOG_DICT_ITEMS,
    depth=env.HTTP_LOG_DEPTH,
    total_bytes=env.HTTP_LOG_TOTAL_BYTES,
)


def _observational(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            # Even a failing logging handler must not prevent response writes or
            # request-lease release. Do not include the offending payload here.
            try:
                logger.warning('HTTP 日志预览失败: operation=%s', function.__name__)
            except Exception:
                pass
            return None

    return guarded


def _quiet(handler) -> bool:
    return handler.request.method == 'OPTIONS'


def _body_enabled(handler) -> bool:
    """A route may suppress health payloads before prepare logs the request."""
    return env.HTTP_LOG_BODY_ENABLED and getattr(handler, 'log_body_enabled', True)


def _identity(handler) -> str:
    request = handler.request
    return (
        f'request_uuid={getattr(handler, "request_uuid", "-")} '
        f'method={request.method} path={request.path} remote_ip={request.remote_ip}'
    )


def _is_binary(content_type: str) -> bool:
    return content_type.startswith(('image/', 'audio/', 'video/')) or content_type in {
        'application/octet-stream',
        'application/pdf',
        'application/zip',
    }


def _raw_body(body: bytes, content_type: str):
    size = len(body)
    if _is_binary(content_type):
        return {'format': 'binary', 'bytes': size, 'content_type': content_type}
    sample = LIMITS.string_chars * 4
    if size > sample:
        half = sample // 2
        body = body[:half] + b'***' + body[-half:]
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        # UTF-8 slicing may split a character; textual bodies remain readable.
        if 'json' in content_type or content_type.startswith('text/'):
            text = body.decode('utf-8', errors='replace')
        else:
            return {'format': 'binary', 'bytes': size, 'content_type': content_type}
    return {
        'format': 'text',
        'bytes': size,
        'parse_skipped': size > env.HTTP_LOG_PARSE_BYTES,
        'preview': text,
    }


def _request_body(request):
    body = request.body
    if not body:
        return {'format': 'empty', 'bytes': 0}
    content_type = request.headers.get('Content-Type', '').split(';', 1)[0].lower()
    if content_type == 'multipart/form-data':
        files = []
        total = sum(len(items) for items in request.files.values())
        for field, items in request.files.items():
            for item in items:
                if len(files) >= LIMITS.list_items:
                    break
                files.append(
                    {
                        'field': field,
                        'filename': item.filename,
                        'content_type': item.content_type,
                        'bytes': len(item.body),
                    }
                )
            if len(files) >= LIMITS.list_items:
                break
        return {'format': 'multipart', 'fields': request.body_arguments, 'files': files, 'file_count': total}
    if content_type == 'application/x-www-form-urlencoded':
        return request.body_arguments
    if _is_binary(content_type):
        return _raw_body(body, content_type)
    if len(body) <= env.HTTP_LOG_PARSE_BYTES:
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            pass
    return _raw_body(body, content_type)


@_observational
@request_log_context
def log_request_start(handler) -> None:
    if _quiet(handler) or not logger.isEnabledFor(logging.INFO):
        return
    message = f'HTTP 请求开始: {_identity(handler)}'
    if _body_enabled(handler):
        data = {'query': handler.request.query_arguments, 'body': _request_body(handler.request)}
        message += f' request={preview(data, LIMITS, decode_bytes=True)}'
    logger.info(message)


def _level(status, code=None):
    effective = max(status, code if isinstance(code, int) else 0)
    return logging.ERROR if effective >= 500 else logging.WARNING if effective >= 400 else logging.INFO


@_observational
def capture_response(handler, chunk) -> None:
    """Called after Tornado accepts a write; keep only bounded preview strings."""
    if _quiet(handler) or handler._headers.get('Content-Type', '').startswith('text/event-stream'):
        return
    if isinstance(chunk, dict):
        code = chunk.get('code')
        handler._http_log_code = code if isinstance(code, int) else None
    if not _body_enabled(handler) or not logger.isEnabledFor(_level(handler.get_status(), handler._http_log_code)):
        return
    parts = handler._http_log_parts
    remaining = LIMITS.total_bytes - handler._http_log_bytes - 256
    handler._http_log_writes += 1
    if remaining < 320:
        return
    if isinstance(chunk, bytes) and handler._headers.get('Content-Type', '').startswith(('text/', 'application/json')):
        chunk = _raw_body(chunk, handler._headers.get('Content-Type', ''))
    part = preview(chunk, replace(LIMITS, total_bytes=remaining))
    parts.append(part)
    handler._http_log_bytes += len(part.encode('utf-8')) + 1


@_observational
@request_log_context
def log_access(handler, *, request_logger) -> None:
    if getattr(handler, '_http_log_access_logged', False):
        return
    if _is_stream(handler) and getattr(handler, '_http_log_stream_active', False):
        # Tornado calls its access hook before finish()'s flush future resolves.
        # The handler's finally block logs after its last delivery outcome is known.
        return
    status = handler.get_status()
    code = getattr(handler, '_http_log_code', None)
    level = max(_level(status, code), getattr(handler, '_http_log_stream_level', logging.INFO))
    if not request_logger.isEnabledFor(level):
        return
    message = (
        f'HTTP 访问 / 响应完成: {_identity(handler)} status_code={status} '
        f'code={code if code is not None else "-"} elapsed_ms={handler.request.request_time() * 1000:.2f}'
    )
    if _is_stream(handler):
        state = (
            'error'
            if getattr(handler, '_http_log_stream_failed', False)
            else 'empty'
            if not getattr(handler, '_http_log_stream_events', 0)
            else 'finished'
            if getattr(handler, '_http_log_stream_finished', False)
            else 'incomplete'
        )
        message += (
            f' stream_events={getattr(handler, "_http_log_stream_events", 0)}'
            f' flushed_events={getattr(handler, "_http_log_stream_flushed", 0)}'
            f' disconnected={getattr(handler, "_client_disconnected", False)}'
            f' stream_errors={getattr(handler, "_http_log_stream_errors", 0)}'
            f' stream_state={state}'
        )
        if _body_enabled(handler):
            summary = getattr(handler, '_http_log_stream_summary', None)
            body = summary.snapshot() if summary is not None else {}
            message += f' body_format=sse_summary body={preview(body, LIMITS)}'
    elif _body_enabled(handler) and not _quiet(handler):
        parts = getattr(handler, '_http_log_parts', [])
        writes = getattr(handler, '_http_log_writes', 0)
        if writes > 1:
            parts = list(parts)
            if writes > len(parts):
                parts.append('"***"')
            message += ' body_format=chunks body=[' + ','.join(parts) + ']'
        else:
            message += ' body=' + (parts[0] if parts else '"***empty/unavailable***"')
    # Keep the legacy logger hooks patchable by their callers.
    emit = (
        request_logger.error
        if level == logging.ERROR
        else request_logger.warning
        if level == logging.WARNING
        else request_logger.info
    )
    handler._http_log_access_logged = True
    emit(message)


def _is_stream(handler) -> bool:
    return bool(getattr(handler, '_http_log_stream_events', 0)) or getattr(handler, '_headers', {}).get(
        'Content-Type',
        '',
    ).startswith('text/event-stream')


@_observational
def ensure_stream_log(handler) -> None:
    """Complete deferred access logging, including sockets that cannot finish."""
    handler._http_log_stream_active = False
    if (
        _is_stream(handler)
        and not getattr(handler, '_http_log_access_logged', False)
        and (getattr(handler, '_client_disconnected', False) or getattr(handler, '_finished', False))
    ):
        handler.application.log_request(handler)


@_observational
@request_log_context
def log_stream_event(handler, payload, *, outcome: str) -> None:
    """Observe one yielded event without retaining it or changing delivery."""
    sequence = getattr(handler, '_http_log_stream_events', 0) + 1
    handler._http_log_stream_events = sequence
    handler._http_log_stream_flushed = getattr(handler, '_http_log_stream_flushed', 0) + (outcome == 'flushed')
    data = payload.get('data', payload) if isinstance(payload, dict) else {}
    event_type = data.get('type', '-') if isinstance(data, dict) else '-'
    code = payload.get('code') if isinstance(payload, dict) else None
    failed = event_type == 'error' or (isinstance(code, int) and code >= 400)
    if failed or not getattr(handler, '_http_log_stream_failed', False):
        if isinstance(code, int) or failed:
            handler._http_log_code = code if isinstance(code, int) else None
    if event_type == 'finish':
        handler._http_log_stream_finished = True
    if failed:
        handler._http_log_stream_failed = True
        error_level = _level(200, code) if isinstance(code, int) and code >= 400 else logging.ERROR
        handler._http_log_stream_level = max(getattr(handler, '_http_log_stream_level', logging.INFO), error_level)
        handler._http_log_stream_errors = getattr(handler, '_http_log_stream_errors', 0) + 1
        logger.log(
            error_level, 'HTTP 流式错误: request_uuid=%s seq=%s outcome=%s', handler.request_uuid, sequence, outcome
        )
    if (
        _body_enabled(handler)
        and logger.isEnabledFor(
            max(logging.INFO, getattr(handler, '_http_log_stream_level', logging.INFO)),
        )
        and isinstance(payload, dict)
    ):
        summary = getattr(handler, '_http_log_stream_summary', None)
        if summary is None:
            summary = handler._http_log_stream_summary = StreamLogSummary(LIMITS)
        summary.observe(payload, event_type=event_type, failed=failed)
    if not logger.isEnabledFor(logging.DEBUG):
        return
    message = (
        f'HTTP 流式事件: request_uuid={handler.request_uuid} seq={sequence} '
        f'event={preview(event_type, replace(LIMITS, string_chars=80, total_bytes=1024))} outcome={outcome}'
    )
    if _body_enabled(handler):
        message += f' body={preview(payload, LIMITS)}'
    logger.debug(message)
